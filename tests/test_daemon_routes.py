"""Routes through the daemon (AGENT-TOOLS §18.5): record one, drive it, use it as a way, replay it."""
import json

import pytest
from mapfakes import APP, MapSession, screen
from test_daemon import finish, make, run, started

from tapwright import mapkeeper
from tapwright.control import Refused

BUNDLE = APP[1]
GOES = {("welcome", "continue-btn"): "login", ("login", "sign-in-btn"): "home", ("home", "to-welcome"): "welcome"}


def screens():
    return {"welcome": screen("Welcome", ids=("continue-btn", "w1", "w2")),
            "login": screen("Login", ids=("sign-in-btn", "l1", "l2"), inputs=("email",)),
            "home": screen("Home", ids=("to-welcome", "h1", "h2"), tab="home"),
            "elsewhere": screen("Elsewhere", ids=("e1", "e2", "e3", "e4"), tab="settings")}


def mapped(tmp_path, goes=None):
    session = MapSession(screens(), "welcome", {**GOES, **(goes or {})})
    d = make(session)
    d.maps = mapkeeper.Maps(str(tmp_path))
    return d


def folder(tmp_path):
    return tmp_path / BUNDLE


async def explore(d):
    """Walk welcome > login > home once, naming the places, and leave the app at welcome."""
    await d.run_tool("a", "look", {"screenshot": False})
    await d.run_tool("a", "map", {"name": "welcome"})
    await d.run_tool("a", "tap", {"target": {"id": "continue-btn"}})
    await d.run_tool("a", "map", {"name": "login"})
    await d.run_tool("a", "tap", {"target": {"id": "sign-in-btn"}})
    await d.run_tool("a", "map", {"name": "home"})
    d.session.at = "welcome"
    await d.run_tool("a", "look", {"screenshot": False})


async def record_sign_in(d):
    await d.run_tool("a", "record", {"start": True, "route": "sign_in"})
    await d.run_tool("a", "tap", {"target": {"id": "continue-btn"}})
    await d.run_tool("a", "type_text", {"text": "tester@example.com", "target": {"id": "email"}, "as": "email"})
    await d.run_tool("a", "tap", {"target": {"id": "sign-in-btn"}})
    return await d.run_tool("a", "record", {"stop": True})


def test_recording_a_route_saves_it_in_the_map_folder_from_place_to_place(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await explore(d)
        r = await record_sign_in(d)
        assert r["text"].startswith("Saved route sign_in (3 steps, welcome > home) to ")
        saved = json.loads((folder(tmp_path) / "routes" / "sign_in.json").read_text("utf-8"))
        assert (saved["version"], saved["start"], saved["end"], saved["variables"], saved["app"]) == (2, "welcome", "home", ["email"], BUNDLE)
        assert saved["steps"] == [{"tool": "tap", "target": {"id": "continue-btn"}},
                                  {"tool": "type_text", "text": "${email}", "target": {"id": "email"}},
                                  {"tool": "tap", "target": {"id": "sign-in-btn"}}]
        assert "tester@example.com" not in (folder(tmp_path) / "routes" / "sign_in.json").read_text("utf-8")
        listed = (await d.run_tool("a", "map", {}))["text"]
        assert "Routes: sign_in (welcome → home; email)" in listed
        await finish(stop, task)
    run(go)


def test_a_typed_text_without_as_is_saved_as_typed_and_a_dollar_brace_is_escaped(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await explore(d)
        await d.run_tool("a", "record", {"start": True, "route": "plain"})
        await d.run_tool("a", "tap", {"target": {"id": "continue-btn"}})
        await d.run_tool("a", "type_text", {"text": "cost ${5}", "target": {"id": "email"}})
        await d.run_tool("a", "record", {"stop": True})
        saved = json.loads((folder(tmp_path) / "routes" / "plain.json").read_text("utf-8"))
        assert saved["steps"][1]["text"] == "cost $${5}" and saved["variables"] == []
        await finish(stop, task)
    run(go)


def test_a_route_name_and_a_variable_name_are_checked(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await explore(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "record", {"start": True, "route": "Bad Name"})
        assert e.value.code == "bad_params"
        await d.run_tool("a", "record", {"start": True, "route": "ok_name"})
        await d.run_tool("a", "tap", {"target": {"id": "continue-btn"}})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "type_text", {"text": "x", "target": {"id": "email"}, "as": "bad name"})
        assert e.value.code == "bad_params"
        await finish(stop, task)
    run(go)


def test_a_route_name_that_differs_from_a_saved_one_only_in_case_is_refused_naming_it(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await explore(d)
        await record_sign_in(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "record", {"start": True, "route": "SIGN_IN"})
        assert e.value.code == "bad_params"
        assert 'differs only in case from the existing route "sign_in"' in str(e.value)
        with pytest.raises(Refused) as e:  # no twin: the plain rule
            await d.run_tool("a", "record", {"start": True, "route": "Sign In"})
        assert "lower-case ASCII" in str(e.value)
        assert sorted(p.name for p in (folder(tmp_path) / "routes").iterdir()) == ["sign_in.json"]
        await finish(stop, task)
    run(go)


def test_driving_a_route_runs_its_steps_with_the_values_given(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await explore(d)
        await record_sign_in(d)
        d.session.at = "welcome"
        r = await d.run_tool("a", "drive", {"route": "sign_in", "with": {"email": "other@example.com"}})
        lines = r["text"].splitlines()
        assert lines[0].startswith("Arrived at home by route sign_in in ") and lines[1] == "Place: home."
        assert d.session.typed[-1][0] == "other@example.com" and d.session.at == "home"
        await finish(stop, task)
    run(go)


def test_a_route_is_asked_for_its_variables_before_anything_is_touched(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await explore(d)
        await record_sign_in(d)
        d.session.at = "welcome"
        before = d.session.calls.count("tap")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "drive", {"route": "sign_in"})
        assert e.value.code == "bad_params" and "needs email" in str(e.value) and d.session.calls.count("tap") == before
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "drive", {"route": "nope"})
        assert e.value.code == "not_found" and "sign_in" in str(e.value)
        await finish(stop, task)
    run(go)


def test_a_route_drives_to_its_start_first(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await explore(d)
        await d.run_tool("a", "tap", {"target": {"id": "continue-btn"}})
        await d.run_tool("a", "tap", {"target": {"id": "sign-in-btn"}})
        await d.run_tool("a", "tap", {"target": {"id": "to-welcome"}})  # a road home > welcome
        d.session.at = "welcome"
        await record_sign_in(d)
        d.session.at = "home"
        await d.run_tool("a", "look", {"screenshot": False})
        r = await d.run_tool("a", "drive", {"route": "sign_in", "with": {"email": "a@b.c"}})
        assert r["text"].splitlines()[0].startswith("Arrived at home by 1 tap then route sign_in in ")
        await finish(stop, task)
    run(go)


def test_a_route_that_doesnt_end_where_it_says_is_off_route(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await explore(d)
        await record_sign_in(d)
        d.session.goes[("login", "sign-in-btn")] = "elsewhere"
        d.session.at = "welcome"
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "drive", {"route": "sign_in", "with": {"email": "a@b.c"}})
        assert e.value.code == "off_route" and "the route ended at a screen the map doesn't know, not home" in str(e.value)
        await finish(stop, task)
    run(go)


def test_a_failing_step_stops_the_drive_with_the_steps_own_error(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await explore(d)
        await record_sign_in(d)
        d.session.at = "welcome"
        d.session.screens["login"] = screen("Login", ids=("sign-in-btn", "l1", "l2"), inputs=("mail",))
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "drive", {"route": "sign_in", "with": {"email": "a@b.c"}})
        assert e.value.code == "not_found" and "route sign_in: Ran 1 of 3 steps; stopped at step 2" in str(e.value)
        await finish(stop, task)
    run(go)


def test_a_dry_run_of_a_route(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await explore(d)
        await record_sign_in(d)
        d.session.at = "welcome"
        before = d.session.calls.count("tap")
        r = await d.run_tool("a", "drive", {"route": "sign_in", "with": {"email": "a@b.c"}, "dry_run": True})
        assert r["text"] == "Would run route sign_in (3 steps), from welcome to home." and d.session.calls.count("tap") == before
        await finish(stop, task)
    run(go)


def test_a_route_is_a_way_for_drive_to_a_place_and_counts_one_move_per_step(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await explore(d)
        await record_sign_in(d)
        await d.maps.flush()
        d.maps.data(None, BUNDLE)["roads"].clear()  # no roads: only the route leads from welcome to home
        d.session.at = "welcome"
        r = await d.run_tool("a", "map", {"to": "home"})
        assert r["text"] == "Would drive to home by route sign_in (3 steps): welcome > home (all proven)."
        r = await d.run_tool("a", "drive", {"to": "home", "with": {"email": "a@b.c"}})
        assert r["text"].splitlines()[0].startswith("Arrived at home by route sign_in in ")
        await finish(stop, task)
    run(go)


def test_replaying_a_version_2_script_drives_to_its_start_first_and_never_writes_the_map(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await explore(d)
        await d.run_tool("a", "tap", {"target": {"id": "continue-btn"}})
        await d.run_tool("a", "tap", {"target": {"id": "sign-in-btn"}})
        await d.run_tool("a", "tap", {"target": {"id": "to-welcome"}})
        d.session.at = "welcome"
        await d.run_tool("a", "record", {"start": True, "route": "sign_in"})
        await d.run_tool("a", "tap", {"target": {"id": "continue-btn"}})
        await d.run_tool("a", "tap", {"target": {"id": "sign-in-btn"}})
        await d.run_tool("a", "record", {"stop": True})
        await d.maps.flush()
        path = folder(tmp_path) / "routes" / "sign_in.json"
        script = json.loads(path.read_text("utf-8"))
        script["fresh"] = True
        path.write_text(json.dumps(script), "utf-8")
        real = d.session.launch

        async def launch(bundle_id, arguments=None):
            d.session.at = "home"  # a fresh start comes up on the home screen, not at the route's start
            return await real(bundle_id, arguments)
        d.session.launch = launch
        before = (folder(tmp_path) / "map.json").read_bytes()
        r = await d.run_tool("a", "replay", {"path": str(path)})
        assert r["text"].startswith("Ran 2 of 2 steps") and d.session.at == "home"
        await d.maps.flush()
        assert (folder(tmp_path) / "map.json").read_bytes() == before
        await finish(stop, task)
    run(go)


def test_a_version_2_replay_with_variables_asks_for_them_and_a_version_1_script_still_runs(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await explore(d)
        await record_sign_in(d)
        path = folder(tmp_path) / "routes" / "sign_in.json"
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "replay", {"path": str(path)})
        assert e.value.code == "bad_params" and "email" in str(e.value)
        v1 = tmp_path / "v1.json"
        v1.write_text(json.dumps({"format": "agent-tools-script", "version": 1, "name": "x", "app": None, "fresh": False,
                                  "steps": [{"tool": "tap", "target": {"id": "continue-btn"}}]}), "utf-8")
        d.session.at = "welcome"
        assert (await d.run_tool("a", "replay", {"path": str(v1)}))["text"].startswith("Ran 1 of 1 steps")
        await finish(stop, task)
    run(go)

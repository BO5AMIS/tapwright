"""The map as the daemon learns it (AGENT-TOOLS §18.1, §18.2): the `Place:` line, and the roads an agent's moves make."""
import json

import pytest
from mapfakes import APP, MapSession, screen
from test_daemon import finish, make, run, started

from tapwright import daemon as daemon_module
from tapwright import mapkeeper
from tapwright.control import Refused

BUNDLE = APP[1]
GOES = {("home", "settings-btn"): "settings", ("settings", "language-row"): "language", ("settings", "delete-btn"): "language",
        ("language", "point:20,70"): "settings", ("settings", "swipe"): "language", ("home", "wayfare://settings?token=X"): "settings",
        ("home", "wayfare://invite/abc?token=SECRET"): "language", ("list", "Chat with Sara"): "chat"}


def screens():
    return {"home": screen("Home", ids=("settings-btn",), inputs=("search",), tab="home"),
            "settings": screen("Settings", ids=("language-row", "about-row", "delete-btn")),
            "language": screen("Language", ids=("english", "arabic")),
            "list": screen("Inbox", rows=("Chat with Sara", "Order #10432")),
            "chat": screen("Chat with Sara", ids=("send",))}


def mapped(tmp_path, start="home", goes=None):
    session = MapSession(screens(), start, {**GOES, **(goes or {})})
    session.has_nav_bar, session.back_at = True, (20.0, 70.0)
    d = make(session)
    d.maps = mapkeeper.Maps(str(tmp_path))
    return d


def file_of(tmp_path) -> dict:
    return json.loads((tmp_path / BUNDLE / "map.json").read_text("utf-8"))


async def tap(d, target, **more):
    return await d.run_tool("a", "tap", {"target": target, **more})


def test_no_place_line_until_the_app_has_a_map_then_new_places_say_so(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        first = await d.run_tool("a", "look", {"screenshot": False})
        assert "Place:" not in first["text"]  # the app had no map: this look saved its first place
        r = await tap(d, {"id": "settings-btn"})
        lines = r["text"].splitlines()
        assert lines[0].startswith("Tapped") and lines[1] == "Place: new (call it with map name)." and lines[2].startswith("Screen ")
        r = await d.run_tool("a", "look", {"screenshot": False})
        lines = r["text"].splitlines()
        assert lines[0].startswith("Screen ") and lines[1].startswith("App: ") and lines[2] == "Place: place_2."
        await finish(stop, task)
    run(go)


def test_a_title_seen_twice_does_not_name_a_place(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await tap(d, {"id": "settings-btn"})  # place_2, first visit
        d.session.at = "home"
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert "Place: place_1." in r["text"]  # the same title on a second visit: still numbered (1.13.2)
        r = await tap(d, {"id": "settings-btn"})
        assert r["text"].splitlines()[1] == "Place: place_2."
        await d.maps.flush()
        assert list(file_of(tmp_path)["places"]) == ["place_1", "place_2"]
        assert [(r["from"], r["to"]) for r in file_of(tmp_path)["roads"]] == [("place_1", "place_2")]
        await finish(stop, task)
    run(go)


def test_a_same_screen_answer_carries_no_place_line(tmp_path):
    async def go():
        d = mapped(tmp_path, start="settings", goes={("settings", "about-row"): "settings"})
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await tap(d, {"id": "language-row"})  # somewhere else: the app has a map by now
        d.session.at = "settings"
        await d.run_tool("a", "look", {"screenshot": False})
        r = await tap(d, {"id": "about-row"})
        assert "Place:" not in r["text"] and ("No visible change." in r["text"] or "Same screen." in r["text"])
        await finish(stop, task)
    run(go)


def test_a_look_at_the_same_place_again_is_not_an_arrival(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        for _ in range(3):
            await d.run_tool("a", "look", {"screenshot": False})
        text = (await d.run_tool("a", "map", {}))["text"]
        assert "Now at: place_1." in text and "1 place," in text
        await finish(stop, task)
    run(go)


def test_a_move_between_two_places_is_a_road_as_it_was_seen(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await tap(d, {"id": "settings-btn"})
        await d.maps.flush()
        [road] = file_of(tmp_path)["roads"]
        assert (road["from"], road["to"], road["by"], road["seen"], road["failed"], road["build"]) == \
            ("place_1", "place_2", "tapwright", 1, 0, "1.0 (7)")
        assert road["move"] == {"tool": "tap", "target": {"id": "settings-btn"}}
        assert road["names"] == {"id": "settings-btn", "label": "Label of settings-btn", "text": "Label of settings-btn"}
        assert isinstance(road["ms"], int) and road["last"].endswith("Z")
        await finish(stop, task)
    run(go)


def test_a_target_by_ref_is_saved_by_id_and_a_road_driven_again_is_counted(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        look = await d.run_tool("a", "look", {"screenshot": False})
        ref = next(line.split()[0] for line in look["text"].splitlines() if "settings-btn" in line)
        await tap(d, {"ref": ref})
        d.session.at = "home"
        await d.run_tool("a", "look", {"screenshot": False})
        await tap(d, {"id": "settings-btn"})
        await d.maps.flush()
        [road] = file_of(tmp_path)["roads"]
        assert road["seen"] == 2 and road["move"]["target"] == {"id": "settings-btn"} and ref not in json.dumps(road["move"])
        await finish(stop, task)
    run(go)


def test_a_list_row_without_an_id_is_a_row_and_no_row_text_is_saved(tmp_path):
    async def go():
        d = mapped(tmp_path, start="list")
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        r = await tap(d, {"label": "Chat with Sara"})
        assert "Sara" in r["text"]  # the agent sees it
        await d.maps.flush()
        text = (tmp_path / BUNDLE / "map.json").read_text("utf-8")
        assert "Sara" not in text and "10432" not in text
        [road] = json.loads(text)["roads"]
        assert road["move"] == {"tool": "tap", "target": {"row": True}} and road["names"] == {}
        await finish(stop, task)
    run(go)


def test_typed_text_is_never_saved_and_makes_no_road_until_the_place_changes(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        d.session.focused = "search"
        await d.run_tool("a", "type_text", {"text": "hunter2 search", "target": {"id": "search"}})
        await tap(d, {"id": "settings-btn"})  # a move whose effect may depend on what was typed
        await tap(d, {"id": "language-row"})  # a new place: roads are saved again
        await d.maps.flush()
        text = (tmp_path / BUNDLE / "map.json").read_text("utf-8")
        assert "hunter2" not in text
        assert [(r["from"], r["to"]) for r in json.loads(text)["roads"]] == [("place_2", "place_3")]
        await finish(stop, task)
    run(go)


def test_a_dry_run_and_a_look_false_action_make_no_road(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        r = await tap(d, {"id": "settings-btn"}, dry_run=True)
        assert r["text"].startswith("Would tap") and d.session.at == "home"
        await tap(d, {"id": "settings-btn"}, look=False)  # arrived somewhere unseen: no way to say where
        await tap(d, {"id": "language-row"})  # from where? unknown: no road either
        await d.maps.flush()
        assert file_of(tmp_path)["roads"] == []
        await finish(stop, task)
    run(go)


def test_a_move_that_needed_a_persons_ok_is_not_a_road_and_the_answer_says_so(tmp_path):
    async def go():
        d = mapped(tmp_path, start="settings")
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        with pytest.raises(Refused) as e:
            await tap(d, {"id": "delete-btn"})
        assert e.value.code == "not_allowed" and "needs_confirmation" in str(e.value)
        [ask] = list(d._confirms)
        await d.viewer_message({"type": "confirm_answer", "id": ask, "allow": True})
        r = await tap(d, {"id": "delete-btn"})
        assert "Note: not saved as a road (it needed a person's OK)." in r["text"] and r["text"].endswith("Note: allowed by a person (in the live view).")
        await d.maps.flush()
        assert file_of(tmp_path)["roads"] == []
        await finish(stop, task)
    run(go)


def test_a_replay_reads_the_map_and_never_writes_it(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.maps.flush()
        before = (tmp_path / BUNDLE / "map.json").read_text("utf-8")
        script = tmp_path / "go.json"
        script.write_text(json.dumps({"format": "agent-tools-script", "version": 1, "app": BUNDLE, "fresh": False,
                                      "steps": [{"tool": "tap", "target": {"id": "settings-btn"}}]}), "utf-8")
        r = await d.run_tool("a", "replay", {"path": str(script)})
        assert d.session.at == "settings" and "Place:" not in r["text"]
        await d.maps.flush()
        assert (tmp_path / BUNDLE / "map.json").read_text("utf-8") == before
        await finish(stop, task)
    run(go)


STEPS = {"steps": [{"tool": "tap", "target": {"id": "settings-btn"}}, {"tool": "tap", "target": {"id": "language-row"}}]}


def test_a_run_takes_its_steps_without_reading_so_it_saves_no_road_but_its_end_is_a_place(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        r = await d.run_tool("a", "run", STEPS)
        assert r["text"].splitlines()[:2] == ["Ran 2 of 2 steps in 0.0 s.", "Place: new (call it with map name)."]
        await d.maps.flush()
        assert file_of(tmp_path)["roads"] == [] and len(file_of(tmp_path)["places"]) == 2
        await finish(stop, task)
    run(go)


def test_with_a_read_after_each_step_the_steps_of_a_run_are_roads_too(tmp_path, monkeypatch):
    async def go():
        monkeypatch.setattr(daemon_module, "READ_RUN_STEPS", True)
        d = mapped(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "run", STEPS)
        await d.maps.flush()
        assert [(r["from"], r["to"]) for r in file_of(tmp_path)["roads"]] == [("place_1", "place_2"), ("place_2", "place_3")]
        await finish(stop, task)
    run(go)


def test_what_a_person_did_in_between_means_the_start_is_not_known(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.viewer_message({"type": "gesture", "points": [[0, 0.5, 0.5], [60, 0.5, 0.5]]})
        await tap(d, {"id": "settings-btn"})
        await d.maps.flush()
        assert file_of(tmp_path)["roads"] == [] and len(file_of(tmp_path)["places"]) == 2
        await finish(stop, task)
    run(go)


def test_back_and_swipe_are_roads(tmp_path):
    async def go():
        d = mapped(tmp_path, start="language")
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "back", {})
        await d.run_tool("a", "swipe", {"x0": 200.0, "y0": 600.0, "x1": 200.0, "y1": 200.0})
        await d.maps.flush()
        moves = [r["move"] for r in file_of(tmp_path)["roads"]]
        assert {"tool": "back"} in moves
        assert {"tool": "swipe", "x0": 200.0, "y0": 600.0, "x1": 200.0, "y1": 200.0, "seconds": 0.35} in moves \
            or {"tool": "swipe", "x0": 200.0, "y0": 600.0, "x1": 200.0, "y1": 200.0} in moves
        await finish(stop, task)
    run(go)


def test_a_link_is_kept_only_as_a_pattern_and_a_road_by_link_follows_it(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await tap(d, {"id": "settings-btn"})
        await d.run_tool("a", "map", {"name": "settings"})
        await d.run_tool("a", "map", {"link": "wayfare://settings"})
        d.session.at = "home"
        await d.run_tool("a", "look", {"screenshot": False})
        r = await d.run_tool("a", "open_url", {"url": "wayfare://settings?token=X"})
        assert "Link not saved" not in r["text"] and r["text"].splitlines()[1] == "Place: settings."
        d.session.at = "home"
        await d.run_tool("a", "look", {"screenshot": False})
        r = await d.run_tool("a", "open_url", {"url": "wayfare://invite/abc?token=SECRET"})
        assert "Note: Link not saved: it may hold a value; add it with map link." in r["text"]
        await d.maps.flush()
        text = (tmp_path / BUNDLE / "map.json").read_text("utf-8")
        assert "SECRET" not in text and "token" not in text and "invite" not in text
        by_link = [r for r in json.loads(text)["roads"] if r["move"]["tool"] == "open_url"]
        assert [r["move"] for r in by_link] == [{"tool": "open_url", "link": "wayfare://settings"}]
        await finish(stop, task)
    run(go)


def test_a_road_that_leads_somewhere_else_twice_is_dropped_and_the_answer_says_so(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await tap(d, {"id": "settings-btn"})  # home -> settings
        d.session.goes[("home", "settings-btn")] = "language"  # the app changed: the same tap now leads to language
        said = []
        for _ in range(2):
            d.session.at = "home"
            await d.run_tool("a", "look", {"screenshot": False})
            said.append((await tap(d, {"id": "settings-btn"}))["text"])
        assert "road dropped" not in said[0] and "Note: road dropped: place_1 > place_2 (it failed twice)." in said[1]
        await d.maps.flush()
        assert [(r["from"], r["to"]) for r in file_of(tmp_path)["roads"]] == [("place_1", "place_3")]
        await finish(stop, task)
    run(go)


def test_a_journey_in_a_users_eyes_saves_and_says_nothing_of_places(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "journey", {"start": True, "goal": "see the settings"})
        r = await tap(d, {"text": "Label of settings-btn"})
        assert "Place:" not in r["text"]
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert "Place:" not in r["text"]
        await finish(stop, task)
    run(go)


def test_an_app_the_agent_was_not_given_is_never_mapped(tmp_path):
    async def go():
        from tapwright.daemon import Scope
        d = mapped(tmp_path)
        stop, task = await started(d)
        with pytest.raises(Refused):
            await d.run_tool("a", "look", {"screenshot": False}, scope=Scope(apps=("com.other.app",)))
        await d.maps.flush()
        assert not (tmp_path / BUNDLE).exists()
        await finish(stop, task)
    run(go)


def test_open_app_remembers_the_language_the_app_was_started_in(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": BUNDLE, "language": "ar-SA", "look": False})
        await d.run_tool("a", "look", {"screenshot": False})
        data = d.maps.data(None, BUNDLE)
        [place] = data["places"].values()
        assert list(place["titles"]) == ["ar"]  # kept under the language it was started with, as a hash
        await finish(stop, task)
    run(go)

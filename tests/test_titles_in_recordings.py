"""A navigation title is never stored in plain text: not in a map, not in a recorded script (version 1), not in a recorded
route (version 2). A native Back button is labelled with the title of the screen under it; it is saved as `back`
(AGENT-TOOLS §18.1; Codex, PR #10 cycle 2)."""
import json

from mapfakes import MapSession, screen
from test_daemon import finish, make, run, started
from test_daemon_places import BUNDLE, TITLED, tap

from tapwright import mapkeeper

SARA = "Chat with Sara"


def titled(tmp_path, goes, screens=None):
    session = MapSession(dict(screens or TITLED), "chat", {("chat", "open-detail"): "detail", ("detail", SARA): "chat", **goes})
    session.has_nav_bar, session.back_at = True, (20.0, 70.0)
    d = make(session)
    d.maps = mapkeeper.Maps(str(tmp_path / "maps"))
    return d


def ref_of(text: str, what: str) -> str:
    return next(line for line in text.splitlines() if what in line).split()[0]


async def at_detail(d):
    await d.run_tool("a", "look", {"screenshot": False})  # the first screen is a place now: its title is a hash
    await tap(d, {"id": "open-detail"})
    return (await d.run_tool("a", "look", {"screenshot": False}))["text"]


def everything(tmp_path) -> str:
    return "\n".join(p.read_text("utf-8", errors="replace") for p in tmp_path.rglob("*") if p.is_file())


def test_a_ref_tap_on_the_native_back_is_recorded_as_back_in_a_script(tmp_path):
    async def go():
        d = titled(tmp_path, {})
        stop, task = await started(d)
        looked = await at_detail(d)
        await d.run_tool("a", "record", {"start": True, "name": "back_script"})
        await tap(d, {"ref": ref_of(looked, SARA)})
        script = tmp_path / "back.json"
        await d.run_tool("a", "record", {"stop": True, "path": str(script)})
        saved = json.loads(script.read_text("utf-8"))
        assert saved["steps"] == [{"tool": "back"}]
        assert "Sara" not in script.read_text("utf-8")
        await finish(stop, task)
    run(go)


def test_a_ref_tap_on_the_native_back_is_recorded_as_back_in_a_route(tmp_path):
    async def go():
        d = titled(tmp_path, {})
        stop, task = await started(d)
        looked = await at_detail(d)
        await d.run_tool("a", "record", {"start": True, "route": "back_route"})
        await tap(d, {"ref": ref_of(looked, SARA)})
        await d.run_tool("a", "record", {"stop": True})
        await d.maps.flush()
        text = everything(tmp_path)
        assert "back_route" in text and "Sara" not in text
        route = json.loads(next((tmp_path / "maps" / BUNDLE / "routes").glob("back_route*.json")).read_text("utf-8"))
        assert [s["tool"] if "tool" in s else s["move"]["tool"] for s in route["steps"]] == ["back"]
        await finish(stop, task)
    run(go)


def test_a_non_back_tap_whose_label_is_a_title_is_recorded_without_it(tmp_path):
    async def go():
        screens = dict(TITLED)
        screens["detail"] = screen("Detail", ids=("title-btn",))  # no Back button
        button = next(e for e in screens["detail"].items if e.identifier == "title-btn")
        button.label, button.identifier, button.name = SARA, "", ""  # only the title names it
        d = titled(tmp_path, {}, screens)
        stop, task = await started(d)
        looked = await at_detail(d)
        await d.run_tool("a", "record", {"start": True, "name": "body"})
        await tap(d, {"ref": ref_of(looked, SARA)})
        await d.run_tool("a", "wait_for", {"text": SARA})  # an expectation that is a title is not saved either
        await d.run_tool("a", "tap", {"target": {"label": SARA}})
        script = tmp_path / "body.json"
        await d.run_tool("a", "record", {"stop": True, "path": str(script)})
        text = script.read_text("utf-8")
        assert "Sara" not in text
        steps = json.loads(text)["steps"]
        assert steps and all(set(s["target"]) == {"x", "y"} for s in steps if s["tool"] == "tap")
        await finish(stop, task)
    run(go)


def test_a_title_that_changes_before_the_back_tap_is_in_no_map_and_the_road_is_back(tmp_path):
    async def go():
        d = titled(tmp_path, {})
        stop, task = await started(d)
        await at_detail(d)
        d.session.screens["chat"] = screen("Chat with Omar", ids=("open-detail", "info-btn"))  # the same screen, renamed
        r = await tap(d, {"label": SARA})
        assert "Omar" in r["text"]
        await d.maps.flush()
        text = (tmp_path / "maps" / BUNDLE / "map.json").read_text("utf-8")
        assert "Sara" not in text and "Omar" not in text
        by = {(r["from"], r["to"]): r for r in json.loads(text)["roads"]}
        assert by[("place_2", "place_1")]["move"] == {"tool": "back"} and by[("place_2", "place_1")]["names"] == {}
        await finish(stop, task)
    run(go)


async def on_detail_first(d):
    """The app is already on Detail (a deep link, a restored screen): the map knows no title yet."""
    d.session.at = "detail"
    return (await d.run_tool("a", "look", {"screenshot": False}))["text"]


def test_a_native_back_on_a_screen_first_seen_is_recorded_as_back_in_a_script(tmp_path):
    async def go():
        for how in ("ref", "label"):
            d = titled(tmp_path / how, {})
            stop, task = await started(d)
            looked = await on_detail_first(d)
            await d.run_tool("a", "record", {"start": True, "name": "deep"})
            await tap(d, {"ref": ref_of(looked, SARA)} if how == "ref" else {"label": SARA})
            script = tmp_path / f"deep_{how}.json"
            await d.run_tool("a", "record", {"stop": True, "path": str(script)})
            assert json.loads(script.read_text("utf-8"))["steps"] == [{"tool": "back"}]
            assert "Sara" not in script.read_text("utf-8")
            await finish(stop, task)
    run(go)


def test_a_native_back_on_a_screen_first_seen_is_recorded_as_back_in_a_route(tmp_path):
    async def go():
        d = titled(tmp_path, {})
        stop, task = await started(d)
        looked = await on_detail_first(d)
        await d.run_tool("a", "record", {"start": True, "route": "deep_route"})
        await tap(d, {"ref": ref_of(looked, SARA)})
        await d.run_tool("a", "record", {"stop": True})
        await d.maps.flush()
        text = everything(tmp_path)
        assert "deep_route" in text and "Sara" not in text
        route = json.loads(next((tmp_path / "maps" / BUNDLE / "routes").glob("deep_route*.json")).read_text("utf-8"))
        assert [s["tool"] if "tool" in s else s["move"]["tool"] for s in route["steps"]] == ["back"]
        await finish(stop, task)
    run(go)


def test_a_leading_bar_button_with_an_id_of_its_own_is_no_native_back(tmp_path):
    async def go():
        screens = dict(TITLED)
        screens["detail"] = screen("Detail", back="Profile", ids=("title-btn",))
        button = next(e for e in screens["detail"].items if e.label == "Profile")
        button.identifier = "open-profile"  # the app's own button, not iOS's Back
        d = titled(tmp_path, {}, screens)
        stop, task = await started(d)
        await on_detail_first(d)
        await d.run_tool("a", "record", {"start": True, "name": "own"})
        await tap(d, {"id": "open-profile"})
        script = tmp_path / "own.json"
        await d.run_tool("a", "record", {"stop": True, "path": str(script)})
        assert json.loads(script.read_text("utf-8"))["steps"] == [{"tool": "tap", "target": {"id": "open-profile"}}]
        await finish(stop, task)
    run(go)


def test_a_title_wait_after_a_performance_step_is_not_saved(tmp_path):
    async def go():
        d = titled(tmp_path, {})
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})  # Chat with Sara is a place's title now
        await d.run_tool("a", "record", {"start": True, "name": "perf"})
        await d.run_tool("a", "performance", {"seconds": 0.5})
        await d.run_tool("a", "wait_for", {"text": SARA, "look": False})
        script = tmp_path / "perf.json"
        await d.run_tool("a", "record", {"stop": True, "path": str(script)})
        text = script.read_text("utf-8")
        assert "Sara" not in text and all(s["tool"] != "wait_for" for s in json.loads(text)["steps"])
        await finish(stop, task)
    run(go)


def test_an_explicit_ignore_selector_that_is_a_title_is_not_saved(tmp_path):
    async def go():
        d = titled(tmp_path, {})
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "record", {"start": True, "name": "ign"})
        for ignore in ({"label": SARA}, {"text": SARA}, {"label": " Chat  with Sara "}):
            try:
                await d.run_tool("a", "compare_screen", {"name": "home", "ignore": [ignore]})
            except Exception as e:  # refused with a message that doesn't echo the title
                assert "Sara" not in str(e)
        script = tmp_path / "ign.json"
        await d.run_tool("a", "record", {"stop": True, "path": str(script)})
        assert "Sara" not in script.read_text("utf-8")
        await finish(stop, task)
    run(go)

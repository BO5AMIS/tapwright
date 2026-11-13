"""Codex, PR #10 cycle 5: a recording keeps no navigation title: a title learned after the step that names it (the screen the
step opened), a performance tap, a long press on the native Back (never `back`), at save time over the whole recording."""
import json

import pytest
from mapfakes import MapSession
from test_back_in_recordings import node, view
from test_daemon import finish, make, run, started
from test_daemon_accounts import BUNDLE
from test_diagnostics import tuned

from tapwright import mapkeeper, ui


# ---- 5, 6, 7: titles in a recording ----
def home_chat(label: str = "Chat with Sara"):
    home = ui.read(_xml("Home", [node("Button", label=label, name="", x=10, y=300, width=200, height=40)]), mapfake_screen())
    chat = ui.read(_xml(label, [node("Button", label="Reply", name="reply", x=10, y=300, width=200, height=40)]), mapfake_screen())
    return home, chat


def mapfake_screen():
    from mapfakes import SCREEN
    return SCREEN


def _xml(title, buttons):
    from xml.etree import ElementTree as ET

    from mapfakes import APP
    app = node("Application", bundleId=BUNDLE, label=APP[0], x=0, y=0, width=400, height=800)
    bar = node("NavigationBar", name=title, x=0, y=40, width=400, height=60)
    bar.append(node("StaticText", label=title, name=title, value=title, x=120, y=50, width=200, height=30, traits="StaticText,Header"))
    app.append(bar)
    for b in buttons:
        app.append(b)
    return ET.tostring(app, encoding="unicode")


def saved_steps(tmp_path, d, how: str, act):
    async def go():
        d.maps = mapkeeper.Maps(str(tmp_path / "maps"))
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        if how == "script":
            await d.run_tool("a", "record", {"start": True, "name": "s"})
            await act(d)
            script = tmp_path / "s.json"
            await d.run_tool("a", "record", {"stop": True, "path": str(script)})
            steps = json.loads(script.read_text("utf-8"))["steps"]
        else:
            await d.run_tool("a", "record", {"start": True, "route": "r"})
            await act(d)
            await d.run_tool("a", "record", {"stop": True})
            await d.maps.flush()
            route = json.loads(next((tmp_path / "maps" / BUNDLE / "routes").glob("r*.json")).read_text("utf-8"))
            steps = [s.get("move", s) for s in route["steps"]]
        await finish(stop, task)
        return steps
    out: list = []

    async def wrapper():
        out.append(await go())
    run(wrapper)
    return out[0]


@pytest.mark.parametrize("how", ["script", "route"])
def test_a_label_that_turns_out_to_be_the_destinations_title_is_saved_by_its_point(tmp_path, how):
    home, chat = home_chat()
    d = make(MapSession({"home": home, "chat": chat}, "home", {("home", "Chat with Sara"): "chat"}))

    async def act(d):
        await d.run_tool("a", "tap", {"target": {"label": "Chat with Sara"}, "look": False})
    steps = saved_steps(tmp_path, d, how, act)
    assert [s["tool"] for s in steps] == ["tap"]
    assert set(steps[0]["target"]) == {"x", "y"}
    assert "Sara" not in json.dumps(steps)


def test_a_wait_for_the_destination_title_and_an_expectation_on_it_are_dropped_at_save(tmp_path):
    home, chat = home_chat()
    d = make(MapSession({"home": home, "chat": chat}, "home", {("home", "Chat with Sara"): "chat"}))

    async def act(d):
        await d.run_tool("a", "tap", {"target": {"label": "Chat with Sara"}, "look": False})
        await d.run_tool("a", "wait_for", {"text": "Reply"})
    steps = saved_steps(tmp_path, d, "script", act)
    assert "Sara" not in json.dumps(steps) and steps[0]["expect"] == {"text": "Reply"}


def native_back_screens():
    prev = view("Chat with Sara", buttons=("open",))
    detail = view("Detail", leading=("Chat with Sara", "BackButton"))
    return prev, detail


def test_a_performance_tap_on_a_native_back_is_saved_by_its_id_not_its_title(tmp_path):
    from tapwright.performance import Measurement
    detail = native_back_screens()[1]
    d = tuned(make(MapSession({"only": detail}, "only")))
    d.session.device.measurement = Measurement(memory_mb=[100.0])

    async def act(d):
        await d.run_tool("a", "performance", {"tap": {"label": "Chat with Sara"}, "seconds": 0.5})
    steps = saved_steps(tmp_path, d, "script", act)
    assert [s["tool"] for s in steps] == ["performance"]
    assert steps[0]["tap"] == {"id": "BackButton"}
    assert "Sara" not in json.dumps(steps)


def test_a_performance_tap_by_a_title_learned_later_is_replaced_at_save(tmp_path):
    from tapwright.performance import Measurement
    home, chat = home_chat()
    d = tuned(make(MapSession({"home": home, "chat": chat}, "home", {("home", "Chat with Sara"): "chat"})))
    d.session.device.measurement = Measurement(memory_mb=[100.0])

    async def act(d):
        await d.run_tool("a", "tap", {"target": {"label": "Chat with Sara"}, "look": False})
        await d.run_tool("a", "performance", {"tap": {"label": "Reply"}, "seconds": 0.5})
    steps = saved_steps(tmp_path, d, "script", act)
    assert "Sara" not in json.dumps(steps)


@pytest.mark.parametrize("how", ["script", "route"])
def test_a_long_press_on_a_native_back_is_never_saved_as_back(tmp_path, how):
    detail = native_back_screens()[1]
    d = make(MapSession({"only": detail}, "only"))

    async def act(d):
        await d.run_tool("a", "long_press", {"target": {"label": "Chat with Sara"}, "look": False})
    steps = saved_steps(tmp_path, d, how, act)
    assert [s["tool"] for s in steps] == ["long_press"]
    assert steps[0]["target"] == {"id": "BackButton"} and "Sara" not in json.dumps(steps)


def test_a_long_press_on_a_native_back_is_not_learned_as_a_back_road(tmp_path):
    prev, detail = native_back_screens()
    d = make(MapSession({"prev": prev, "detail": detail}, "prev", {("prev", "open"): "detail", ("detail", "BackButton"): "prev"}))
    d.maps = mapkeeper.Maps(str(tmp_path))

    async def go():
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "tap", {"target": {"id": "open"}, "look": False})
        await d.run_tool("a", "long_press", {"target": {"label": "Chat with Sara"}, "look": False})
        await d.maps.flush()
        await finish(stop, task)
    run(go)
    text = (tmp_path / BUNDLE / "map.json").read_text("utf-8")
    moves = [r["move"] for r in json.loads(text)["roads"]]
    assert {"tool": "back"} not in moves and "Sara" not in text
    assert all(m.get("target", {}).get("id") in ("open", "BackButton") for m in moves)


def test_save_time_check_replaces_nested_selectors_by_their_fallback_and_drops_title_waits():
    from tapwright import batch
    aimed = {"label": "Chat with Sara"}
    steps = [{"tool": "tap", "target": aimed, "expect": {"text": "Chat with Sara", "matches": "shot"}},
             {"tool": "wait_for", "label": "Chat with Sara"},
             {"tool": "compare_screen", "name": "n", "ignore": [{"label": "Chat with Sara"}, {"id": "x"}]}]
    held = [(aimed, {"x": 1.0, "y": 2.0})]
    out = batch.without_titles(steps, held, lambda text: text == "Chat with Sara")
    assert out == [{"tool": "tap", "target": {"x": 1.0, "y": 2.0}, "expect": {"matches": "shot"}},
                   {"tool": "compare_screen", "name": "n", "ignore": [{"id": "x"}]}]

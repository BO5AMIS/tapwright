"""1.9.0, CodeRabbit's second cycle on PR #22: each test fails on 8f13fd3."""
import json
from xml.etree import ElementTree as ET

import pytest
from fakes import button
from mapfakes import APP, MapSession, screen
from test_daemon import KEYBOARD_BAR, finish, keyboard_up, make, run, started
from test_tokens_190 import multiline, saved

from tapwright import mapfile, mapkeeper, routes as routelib, ui
from tapwright.control import Refused

BUNDLE = APP[1]


def node(kind, **attrs):
    return ET.Element(f"XCUIElementType{kind}", {"type": f"XCUIElementType{kind}", "enabled": "true",
                                                  **{k: str(v) for k, v in attrs.items()}})


# ---- 4628971656: a toolbar counts only in the keyboard's own window ----

def test_a_toolbar_of_the_apps_window_on_the_keys_edge_is_no_accessory():
    app = node("Application", bundleId=BUNDLE, label="Example", x=0, y=0, width=400, height=800)
    win = node("Window", x=0, y=0, width=400, height=800)  # the app's window, the keyboard drawn in it
    host = node("Other", x=0, y=0, width=400, height=800)
    bar = node("Toolbar", x=0, y=456, width=400, height=44)
    bar.append(node("Button", label="Done", name="app-done", x=330, y=458, width=60, height=40))
    holder = node("Other", x=0, y=500, width=400, height=300)
    holder.append(node("Keyboard", x=0, y=500, width=400, height=300))
    host.extend([bar, holder])
    win.append(host)
    app.append(win)
    tree = ui.read(ET.tostring(app, encoding="unicode"), (400.0, 800.0))
    assert [e.by_keyboard for e in tree.items if e.label == "Done"] == [False]


# ---- 4628971698: one edge for every Done check ----

def test_an_accessory_done_counts_when_the_keyboards_frame_holds_a_suggestion_strip():
    async def go():
        d, stop, task = await keyboard_up(blank_tap_closes=False, dismiss_ok=False)
        view = d.control.client("a")
        done = button("e9", "Done", "", rect=(330, 430, 60, 40))  # on the keys' edge; the keyboard's frame (500) is lower
        done.by_keyboard = True  # (ui.read measured it against the keys' top)
        view.items = [*view.items, done]
        assert d._done_on_keyboard(view) is done
        await finish(stop, task)
    run(go)


# ---- 4628971679: a person's route saved meanwhile is never overwritten ----

def test_an_automatic_route_never_overwrites_a_persons_route_saved_meanwhile(tmp_path):
    file = mapfile.MapFile(str(tmp_path), BUNDLE)
    person = routelib.route("plan_trip", BUNDLE, "place_1", "place_2", [], [{"tool": "tap", "target": {"id": "mine"}}])
    file.write_route("plan_trip", person)  # saved after the worker chose the free name
    auto = {**routelib.route("plan_trip", BUNDLE, "place_1", "place_2", [], [{"tool": "tap", "target": {"id": "x"}}]),
            "auto": True}
    assert file.write_auto_route("plan_trip", auto, None) is False
    kept = json.loads((tmp_path / BUNDLE / "routes" / "plan_trip.json").read_text("utf-8"))
    assert kept["steps"][0]["target"]["id"] == "mine"


def test_an_automatic_route_replaces_only_the_version_it_read(tmp_path):
    file = mapfile.MapFile(str(tmp_path), BUNDLE)
    first = {**routelib.route("open_list", BUNDLE, "place_1", "place_2", [], [{"tool": "tap", "target": {"id": "a"}}]),
             "auto": True}
    assert file.write_auto_route("open_list", first, None) is True
    stamp = file.route_stamp(file.load(), "open_list")
    second = {**first, "steps": [{"tool": "tap", "target": {"id": "b"}}]}
    assert file.write_auto_route("open_list", second, (stamp or 0) + 1) is False  # another version is there now
    assert file.write_auto_route("open_list", second, stamp) is True
    assert file.load()["routes"]["open_list"] == "routes/open_list.json"


# ---- 4628971682: an action that isn't navigation breaks the way with look: false too ----

def test_an_action_that_isnt_navigation_with_look_false_breaks_the_way(tmp_path):
    async def go():
        screens = {"home": screen("Home", ids=("open-form", "h1"), tab="home"),
                   "form": screen("Options", ids=("go-on", "mode", "f1")),
                   "inside": screen("Results", ids=("i1", "i2"))}
        d = make(MapSession(screens, "home", {("home", "open-form"): "form", ("form", "go-on"): "inside"}))
        d.maps = mapkeeper.Maps(str(tmp_path))
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": BUNDLE})
        await d.run_tool("a", "tap", {"target": {"id": "open-form"}})
        await d.run_tool("a", "long_press", {"target": {"id": "mode"}, "look": False})  # changes a setting, unseen
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "tap", {"target": {"id": "go-on"}})
        assert "go_on" not in saved(tmp_path)
        await finish(stop, task)
    run(go)


# ---- 4628971690: the multiline Done is for a tap only ----

def test_a_long_press_on_a_covered_save_never_presses_done():
    async def go():
        d, stop, task = await keyboard_up(blank_tap_closes=False, dismiss_ok=False, done_key=True)
        s = d.session
        multiline(s)  # Notes focused, Save trip under the keyboard
        await d.run_tool("a", "look", {"screenshot": False})
        with pytest.raises(Refused):
            await d.run_tool("a", "long_press", {"target": {"id": "low"}})
        assert ("Done",) not in s.dismiss_keys
        await finish(stop, task)
    run(go)

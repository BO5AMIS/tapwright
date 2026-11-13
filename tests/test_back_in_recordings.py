"""ui.is_back: the very button `back` would press (a button of a real NavigationBar, by ui.back_button's own choice). A
road learned from a tap on it is `back` (AGENT-TOOLS §18.1); a recording (script, route) keeps the tap as recorded, its
label too (PR #10, title scope: titles are kept out of learned map data only)."""
import json
from xml.etree import ElementTree as ET

import pytest
from mapfakes import APP, SCREEN, MapSession
from test_account_values_raw_inside import node
from test_daemon import finish, make, run, started
from test_daemon_accounts import BUNDLE

from tapwright import mapkeeper, ui


def view(title: str, bar: str = "NavigationBar", leading: tuple[str, str] | None = None, buttons=("b1",)) -> ui.Tree:
    app = node("Application", bundleId=BUNDLE, label=APP[0], x=0, y=0, width=400, height=800)
    top = node(bar, name=title, x=0, y=40, width=400, height=60)
    top.append(node("StaticText", label=title, name=title, value=title, x=120, y=50, width=200, height=30, traits="StaticText,Header"))
    if leading is not None:
        label, name = leading
        top.append(node("Button", label=label, name=name, x=10, y=50, width=60, height=30))
    app.append(top)
    for n, ident in enumerate(buttons):
        app.append(node("Button", label=f"Label of {ident}", name=ident, x=10, y=620 + 44 * n, width=200, height=40))
    return ui.read(ET.tostring(app, encoding="unicode"), SCREEN)


def recorded(tmp_path, tree: ui.Tree, target: dict, how: str):
    """Record one tap on `target` of a screen the map has never seen, as a script or a route; the steps saved."""
    async def go():
        d = make(MapSession({"only": tree}, "only"))
        d.maps = mapkeeper.Maps(str(tmp_path / "maps"))
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        if how == "script":
            await d.run_tool("a", "record", {"start": True, "name": "s"})
            await d.run_tool("a", "tap", {"target": target, "look": False})
            script = tmp_path / "s.json"
            await d.run_tool("a", "record", {"stop": True, "path": str(script)})
            steps = json.loads(script.read_text("utf-8"))["steps"]
        else:
            await d.run_tool("a", "record", {"start": True, "route": "r"})
            await d.run_tool("a", "tap", {"target": target, "look": False})
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
@pytest.mark.parametrize("target", [{"label": "Chat with Sara"}, {"id": "BackButton"}])
def test_a_tap_on_the_native_back_is_recorded_as_it_was_made(tmp_path, how, target):
    tree = view("Detail", leading=("Chat with Sara", "BackButton"))
    steps = recorded(tmp_path, tree, target, how)
    assert steps == [{"tool": "tap", "target": target}]  # (what was recorded: replay taps the same button)


@pytest.mark.parametrize("how", ["script", "route"])
def test_a_toolbars_leading_profile_button_is_not_recorded_as_back(tmp_path, how):
    tree = view("Toolbar title", bar="Toolbar", leading=("Profile", "Profile"))
    steps = recorded(tmp_path, tree, {"label": "Profile"}, how)
    assert steps == [{"tool": "tap", "target": {"label": "Profile"}}]  # (what the tap did, not `back`)


@pytest.mark.parametrize("how", ["script", "route"])
def test_a_menu_button_on_a_screen_titled_menu_is_recorded_by_its_label(tmp_path, how):
    tree = view("Menu", leading=("Menu", "Menu"))
    steps = recorded(tmp_path, tree, {"label": "Menu", "index": 1}, how)
    assert steps == [{"tool": "tap", "target": {"label": "Menu", "index": 1}}]


def test_is_back_is_the_element_back_would_press_in_a_real_navigation_bar():
    real = view("Detail", leading=("Chat with Sara", "BackButton"))
    assert ui.is_back(next(e for e in real.items if e.type == "Button" and e.x < 100), real.back)
    other = next(e for e in real.items if e.label == "Label of b1")
    assert not ui.is_back(other, real.back)
    bar = view("T", bar="Toolbar", leading=("Profile", "Profile"))
    assert bar.back is not None and bar.back.point is None
    assert not any(ui.is_back(e, bar.back) for e in bar.items)

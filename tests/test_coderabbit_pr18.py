"""1.7.8, CodeRabbit's review of PR #18: each test fails on c861a53 and passes after."""
import asyncio
import dataclasses
import pathlib
import xml.etree.ElementTree as ET

import pytest
from fakes import button, el
from test_daemon import finish, make, run, started
from test_hotfix_178 import _hit_of, _hosted_session, _node, _one, _photo_picker, _share_sheet
from test_review_178_opus1 import APP_HEAD, APP_TAIL, _phone, _sent, _text_view, quick  # noqa: F401 (a fixture)

from tapwright import daemon as daemon_module, ui
from tapwright.control import Refused
from tapwright.session import Session

ROOT = pathlib.Path(__file__).resolve().parent.parent


# 1: the guide never says `submit` only closes a one-line field: Return may send or submit

def test_1_the_guides_say_return_is_up_to_the_app():
    texts = [(ROOT / p).read_text("utf-8") for p in ("AGENTS.md", "skills/tapwright/SKILL.md", "tapwright/skill/SKILL.md")]
    for text in texts:
        assert "closes a one-line field" not in text
        assert "what Return does is up to the app" in text and "don't use it just to close the keyboard" in text
    assert texts[1] == texts[2]


# 2, 3: typing without a target goes only to the app the last touch went to

def _files_then_safari():
    d = make()
    s = d.session
    s.app = ("Files", "com.apple.DocumentsApp")
    s.tree = [el("e1", "input", "Search", "search", rect=(20, 67, 259, 48)),
              button("e2", "Close", "close", rect=(362, 67, 48, 48))]
    return d, s


def _safari_now(s, snapshot_too: bool) -> None:
    s.tree_app = ("Safari", "com.apple.mobilesafari")  # what the next read names at its root
    if snapshot_too:
        s.app = s.tree_app
    s.tree = [el("e1", "input", "Address", "TabBarItemTitle", rect=(145, 861, 140, 26))]


def test_2_the_app_is_compared_from_the_typing_read_itself_not_the_last_snapshot():
    async def go():
        d, s = _files_then_safari()
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            await d.run_tool("a", "tap", {"target": {"label": "Search"}, "look": False})
            _safari_now(s, snapshot_too=False)  # no snapshot since: the session's app still says Files
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "type_text", {"text": "tapwright-square"})
            assert e.value.code == "stale" and "Safari" in str(e.value)
            assert s.typed == []
        finally:
            await finish(stop, task)
    run(go)


def test_3_a_stale_typing_refusal_keeps_the_app_the_touch_went_to():
    async def go():
        d, s = _files_then_safari()
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            await d.run_tool("a", "tap", {"target": {"label": "Search"}})
            _safari_now(s, snapshot_too=True)
            for _ in range(2):  # the same call again is refused again: nothing was typed into Files' place
                with pytest.raises(Refused) as e:
                    await d.run_tool("a", "type_text", {"text": "tapwright-square"})
                assert e.value.code == "stale"
            assert s.typed == []
            await d.run_tool("a", "look", {"screenshot": False})  # the agent sees Safari now
            await d.run_tool("a", "type_text", {"text": "x"})
            assert len(s.typed) == 1
        finally:
            await finish(stop, task)
    run(go)


# 4: each later drag of a scroll in a hosted view is proven where it starts

def test_4_a_hosted_scroll_stops_when_a_later_drag_is_not_proven():
    async def go():
        s = _hosted_session(_photo_picker())
        grid = next(e for e in s.tree if e.role == "scroll" and e.placed == "moved")
        s.hit_answer = _hit_of(grid)
        real = s.scroll

        async def scroll(*a):
            out = await real(*a)
            s.hit_answer = None  # the drag moved the picker: the phone confirms nothing at the start point now
            return out
        s.scroll = scroll
        d = make(s)
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "scroll", {"target": {"x": 200, "y": 500}, "to": "end",
                                                 "look": False})
            assert e.value.code == "not_actionable" and str(e.value).startswith("covered: ")
            assert len(s.scrolls) == 1
        finally:
            await finish(stop, task)
    run(go)


# 5: a cover-sheet date past the right edge proves nothing

def test_5_a_date_past_the_right_edge_is_no_proof():
    from test_x_items_178 import _springboard

    from tapwright import notifications as nl
    date = ('<XCUIElementTypeOther type="XCUIElementTypeOther" name="lockscreen-date-view" label="Tuesday 17 November" '
            'x="{x}" y="120" width="430" height="80"/>')
    assert nl.is_center(_springboard(date.format(x=0)))
    assert not nl.is_center(_springboard(date.format(x=430)))
    assert not nl.is_center(_springboard(date.format(x=900)))


# 6: the saved focus is reused only while the focused element is still where the field is listed

def test_6_saved_focus_on_a_field_scrolled_away_is_not_reused_for_another_with_its_key(quick):
    # field A was typed into at y 125; an automatic scroll carried it off (it keeps the focus), and field B, with the
    # same identifier, now sits where A was
    tree = APP_HEAD + _text_view(125, "", name="postText") + APP_TAIL
    phone = _phone(tree, "pA", {("pA", "name"): "postText"}, {"pA": (56, -400, 334, 100)})
    s = Session(phone)
    b = ui.Element("e1", "input", "", "postText", rect=(56, 125, 334, 100), actions=("tap", "type"), name="postText")

    async def go():
        await s.ensure()
        s.tw_version = 5
        s.items = [b]
        s.typed_into, s.typed_place = ("input:postText", "pA"), (56, 125, 334)
        with pytest.raises(Refused) as e:
            await s.type_text("hello", target=ui.Target(ref="e1"), no_tap=True)
        assert e.value.code == "stale"
    asyncio.run(go())
    assert _sent(phone) == []


# 7: a strict replace after a focusing tap proves the place, even for a unique key

def _title(y: float) -> str:
    return (f'<XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="Title" enabled="true" x="16" y="{y}" '
            'width="200" height="30"/>')


LONG = "old text " * 10


def _replace_after_tap(after_y: float, title_after: float):
    before = APP_HEAD + _title(250) + _text_view(125, LONG, name="postText") + APP_TAIL
    after = APP_HEAD + _title(title_after) + _text_view(after_y, LONG, name="postText") + APP_TAIL
    phone = _phone(after, "pX", {("pX", "name"): "postText"}, {"pX": (56, after_y, 334, 100)})
    s = Session(phone)

    async def go():
        await s.ensure()
        s.tw_version = 5
        s.items = ui.read(before, (430.0, 932.0)).items
        field = next(e for e in s.items if e.role == "input")
        await s.type_text("WORLD", replace=True, target=ui.Target(ref=field.ref))
    return phone, go


def test_7_a_strict_replace_never_clears_another_input_with_the_key_elsewhere(quick):
    phone, go = _replace_after_tap(600, 250)  # the app put another input with the key lower down; the title stayed
    with pytest.raises(Refused) as e:
        asyncio.run(go())
    assert e.value.code == "stale"
    assert _sent(phone) == []


@pytest.mark.parametrize("after_y, title_after", [(125, 250), (25, 150)])
def test_7_a_strict_replace_still_clears_the_field_in_place_or_scrolled_with_the_form(quick, after_y, title_after):
    phone, go = _replace_after_tap(after_y, title_after)
    asyncio.run(go())
    assert [p for p, _ in _sent(phone)] == ["/session/S/element/pX/clear", "/session/S/wda/keys"]


# 8: a hosted Back button inside an ordinary bar is never pressed by its frame

@pytest.mark.parametrize("name, label", [("BackButton", "Back"), ("", "Library")])
def test_8_a_hosted_back_button_in_an_ordinary_bar_is_refused(name, label):
    attrs = (f' name="{name}"' if name else "") + f' label="{label}"'
    xml = ('<AppiumAUT><XCUIElementTypeApplication type="XCUIElementTypeApplication" x="0" y="0" width="430" '
           'height="932"><XCUIElementTypeNavigationBar type="XCUIElementTypeNavigationBar" x="0" y="60" width="430" '
           f'height="100"><XCUIElementTypeButton type="XCUIElementTypeButton"{attrs} x="8" y="70" width="60" '
           f'height="40" {ui.PLACED_ATTR}="unknown" {ui.RAW_ATTR}="8,10,60,40"/></XCUIElementTypeNavigationBar>'
           '</XCUIElementTypeApplication></AppiumAUT>')
    back = ui._back_of(ET.fromstring(xml), (430.0, 932.0))
    assert back.point is None and back.hosted
    plain = ET.fromstring(xml.replace(f' {ui.PLACED_ATTR}="unknown" {ui.RAW_ATTR}="8,10,60,40"', ""))
    assert ui._back_of(plain, (430.0, 932.0)).point == (38, 90)  # an ordinary one still is


# 9: the keyboard reflex never drags a scroll view another process draws

@pytest.mark.parametrize("placed", ["unknown", "moved"])
def test_9_the_reflex_drag_leaves_hosted_scroll_views_out(placed):
    view = el("e1", "scroll", "", "list", rect=(0, 60, 400, 440))
    assert ui.dismiss_drag([view], (400.0, 800.0), 0, 500) is not None
    hosted = dataclasses.replace(view, placed=placed, raw_rect=(0, 0, 400, 440))
    assert ui.dismiss_drag([hosted], (400.0, 800.0), 0, 500) is None


# 10: a double tap's second touch on a hosted element is proven again

def test_10_a_double_taps_second_touch_on_a_hosted_element_is_proven_again():
    async def go():
        s = _hosted_session(_share_sheet())
        s.hit_answer = _hit_of(_one(s.tree, "View More"))
        real = s.tap

        async def tap(*a, **kw):
            out = await real(*a, **kw)
            s.hit_answer = None  # the first touch moved the sheet: nothing is confirmed there now
            return out
        s.tap = tap
        d = make(s)
        stop, task = await started(d)
        token = daemon_module._DOUBLE.set(True)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "tap", {"target": {"label": "View More"}, "look": False})
            assert e.value.code == "not_actionable"
            assert len(s.tap_points) == 1 and len(s.hit_asks) == 2
        finally:
            daemon_module._DOUBLE.reset(token)
            await finish(stop, task)
    run(go)


# 11: an off-screen element keeps its placement mark

def test_11_an_off_screen_hosted_element_keeps_its_mark():
    far = _node("Button", (300, 1600, 100, 44), label="Far")
    stray = _node("Button", (300, 600, 100, 44), label="Stray")
    tree = ui.read(_share_sheet(host_child_extra=stray + far), (430.0, 932.0))
    assert _one(tree.items, "Copy").placed == "unknown"
    [off] = [e for e in tree.offscreen if e.label == "Far"]
    assert off.placed == "unknown"


# ---- cycle 2 (at 2203451) ----

# c2-1: the keyboard reflex's drag never turns the agent's passive point into a control's touch

def _reflex_drag_brings(new):
    from test_daemon import keyboard_up

    async def go(args):
        d, stop, task = await keyboard_up(blank_tap_closes=False)
        s = d.session
        s.tree = [*s.tree, el("e8", "scroll", "", "form", rect=(0, 60, 400, 440))]
        await d.run_tool("a", "look", {"screenshot": False})
        real = s.swipe

        async def swipe(*a, **kw):
            out = await real(*a, **kw)
            s.tree, s.last_keyboard = [*s.tree, *new], False  # the drag scrolled the form a little
            return out
        s.swipe = swipe
        try:
            return d, await d.run_tool("a", "tap", args)
        finally:
            await finish(stop, task)
    return go


def test_c2_1_a_control_the_reflex_drag_moved_under_the_point_is_not_touched():
    go = _reflex_drag_brings([button("e9", "Delete", "delete", rect=(300, 220, 80, 40))])

    async def refused():
        with pytest.raises(Refused) as e:
            await go({"target": {"x": 340, "y": 240}})
        assert e.value.code == "stale" and "Delete" in str(e.value)
    run(refused)

    async def forced():
        d, _ = await go({"target": {"x": 340, "y": 240}, "force": True})
        assert (340, 240) in d.session.tap_points
    run(forced)


def test_c2_1_a_point_that_stays_passive_is_touched_after_the_reflex():
    go = _reflex_drag_brings([el("e9", "text", "Note", rect=(300, 220, 80, 40), type="StaticText")])

    async def touched():
        d, r = await go({"target": {"x": 340, "y": 240}})
        assert (340, 240) in d.session.tap_points
    run(touched)


# c2-2: the touch's app is the one the newest read before it named

def test_c2_2_the_touch_is_meant_for_the_app_its_own_read_named():
    async def go():
        d, s = _files_then_safari()
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            await d.run_tool("a", "tap", {"target": {"label": "Search"}, "look": False})
            _safari_now(s, snapshot_too=False)  # the snapshot still says Files; the tap's own read names Safari
            await d.run_tool("a", "tap", {"target": {"label": "Address"}, "look": False})
            await d.run_tool("a", "type_text", {"text": "x"})  # the agent's touch went to Safari: typed there
            assert len(s.typed) == 1
        finally:
            await finish(stop, task)
    run(go)


# c2-3: a date partly above the top of the screen still shows

def test_c2_3_a_partly_visible_date_proves_the_sheet():
    from test_x_items_178 import DATE, _springboard

    from tapwright import notifications as nl
    assert nl.is_center(_springboard(DATE.format(y=-20, h=80)))
    assert not nl.is_center(_springboard(DATE.format(y=-80, h=80)))

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


# (another input stays put: with a single input on the screen before and after, the last Opus review's M1 takes the
# one there for the tapped one)
SUBJECT = ('<XCUIElementTypeTextField type="XCUIElementTypeTextField" label="Subject" value="s" enabled="true" x="16" '
           'y="780" width="398" height="36"/>')


def _replace_after_tap(after_y: float, title_after: float):
    before = APP_HEAD + _title(250) + _text_view(125, LONG, name="postText") + SUBJECT + APP_TAIL
    after = APP_HEAD + _title(title_after) + _text_view(after_y, LONG, name="postText") + SUBJECT + APP_TAIL
    phone = _phone(after, "pX", {("pX", "name"): "postText"}, {"pX": (56, after_y, 334, 100)})
    s = Session(phone)

    async def go():
        await s.ensure()
        s.tw_version = 5
        s.items = ui.read(before, (430.0, 932.0)).items
        field = next(e for e in s.items if e.identifier == "postText")
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


# ---- the last Opus review (at 703e50c) ----

def _n(kind, rect, label="", value="", name=""):
    x, y, w, h = rect
    a = (f' label="{label}"' if label else "") + (f' name="{name}"' if name else "") + (f' value="{value}"' if value else "")
    return f'<XCUIElementType{kind} type="XCUIElementType{kind}"{a} enabled="true" x="{x}" y="{y}" width="{w}" height="{h}"/>'


def _typed_after_lift(before: str, after: str, rect, attrs=None, replace=False):
    """type_text through the focusing tap at the field's centre; the keyboard moved the field to `rect`."""
    phone = _phone(APP_HEAD + after + APP_TAIL, "f1", attrs or {}, {"f1": rect})
    s = Session(phone)

    async def go():
        await s.ensure()
        s.tw_version = 5
        s.items = ui.read(APP_HEAD + before + APP_TAIL, (430.0, 932.0)).items
        field = max((e for e in s.items if e.role == "input"), key=lambda e: e.y)  # the composer at the bottom
        return await s.type_text("hello", replace=replace, target=ui.Target(ref=field.ref), at=field.center)
    return phone, go


# M1 a: an unlabelled composer the keyboard lifts takes the first type_text

@pytest.mark.parametrize("replace", [False, True])
def test_m1_an_unlabelled_composer_the_keyboard_lifts_is_typed_into(quick, replace):
    before = (_n("StaticText", (16, 200, 200, 30), "Hi there") + _n("TextView", (16, 860, 330, 40))
              + _n("Button", (356, 860, 60, 40), "Send"))
    after = (_n("StaticText", (16, 200, 200, 30), "Hi there") + _n("TextView", (16, 520, 330, 40))
             + _n("Button", (356, 520, 60, 40), "Send"))
    phone, go = _typed_after_lift(before, after, (16, 520, 330, 40), replace=replace)
    asyncio.run(go())
    assert [p for p, _ in _sent(phone)][-1] == "/session/S/wda/keys"


def test_m1_with_two_unlabelled_inputs_a_lifted_field_is_still_refused(quick):
    other = _n("TextField", (16, 100, 398, 36))
    before = _n("TextView", (16, 860, 330, 40)) + other  # (the composer is the target)
    after = _n("TextView", (16, 520, 330, 40)) + other
    phone, go = _typed_after_lift(before, after, (16, 520, 330, 40))
    with pytest.raises(Refused):
        asyncio.run(go())
    assert _sent(phone) == []


# M1 b: replace into a field the keyboard moves, with nothing named moving along

@pytest.mark.parametrize("before, after, rect, label", [
    (_n("StaticText", (16, 200, 300, 30), "Hi there") + _n("TextView", (16, 860, 330, 40), "Message", "draft")
     + _n("Button", (356, 860, 60, 40)),
     _n("StaticText", (16, 200, 300, 30), "Hi there") + _n("TextView", (16, 520, 330, 40), "Message", "draft")
     + _n("Button", (356, 520, 60, 40)), (16, 520, 330, 40), "Message"),
    (_n("StaticText", (16, 100, 200, 40), "Settings") + _n("SearchField", (16, 150, 398, 36), "Search", "wifi")
     + _n("Cell", (16, 220, 398, 44), "Wi-Fi"),
     _n("SearchField", (16, 98, 330, 36), "Search", "wifi") + _n("Button", (354, 98, 60, 36), "Cancel")
     + _n("Cell", (16, 168, 398, 44), "Wi-Fi"), (16, 98, 330, 36), "Search"),
], ids=["composer with an unnamed send icon", "search field narrows and rises"])
def test_m1_replace_into_the_only_field_the_keyboard_moved_clears_it(quick, before, after, rect, label):
    phone, go = _typed_after_lift(before, after, rect, {("f1", "label"): label}, replace=True)
    asyncio.run(go())
    assert [p.rsplit("/", 1)[-1] for p, _ in _sent(phone)] == ["clear", "keys"]


def test_m1_replace_with_another_input_on_screen_stays_refused(quick):
    other = _n("TextField", (16, 300, 398, 36), "Subject", "x")
    before = _n("TextView", (16, 860, 330, 40), "Message", "draft") + other  # (the composer is the target)
    after = _n("TextView", (16, 520, 330, 40), "Message", "draft") + other
    phone, go = _typed_after_lift(before, after, (16, 520, 330, 40), {("f1", "label"): "Message"}, replace=True)
    with pytest.raises(Refused) as e:
        asyncio.run(go())
    assert e.value.code == "stale" and _sent(phone) == []


# M2 a: back never works out a sheet's box from frames that can't be placed

def test_m2_back_refuses_on_a_sheet_whose_frames_cant_be_placed():
    from fakes import FakeSession

    async def go():
        s = FakeSession()
        d = make(s)
        grid = el("e2", "scroll", "", "grid", rect=(0, 0, 430, 663), placed="unknown", raw_rect=(0, 0, 430, 663))
        bar = el("e1", "other", "Photos", "bar", rect=(0, 20, 430, 96), placed="unknown", raw_rect=(0, 20, 430, 96))
        s.items = s.front_items = [bar, grid]
        s.behind = [(button("e9", "Post", "post", rect=(16, 100, 80, 40)), bar)]

        async def still():
            return ui.Back(False, None)
        d._still_back = still
        view = type("V", (), {"items": [bar, grid]})()
        with pytest.raises(Refused) as e:
            await d._back(view, {})
        assert e.value.code == "not_actionable" and str(e.value).startswith("covered: ")
        assert "tap" not in s.calls and "swipe" not in s.calls
    run(go)


# M2 b: the keyboard reflex never touches by frames that can't be placed

def _hosted_button(label, placed, rect=(300, 230, 80, 40)):
    return button("e9", label, label.lower(), rect=rect, placed=placed, raw_rect=(10, 10, 80, 40))


def test_m2_a_tap_on_an_unknown_hosted_target_with_the_keyboard_up_touches_nothing():
    from test_daemon import keyboard_up

    async def go():
        d, stop, task = await keyboard_up()
        s = d.session
        s.tree = [*s.tree, _hosted_button("Done", "unknown")]
        await d.run_tool("a", "look", {"screenshot": False})
        try:
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "tap", {"target": {"label": "Done"}})
            assert e.value.code == "not_actionable" and "nothing was touched" in str(e.value)
            assert s.tap_points == [] and "swipe" not in s.calls
        finally:
            await finish(stop, task)
    run(go)


def test_m2_with_an_unknown_frame_listed_the_reflex_uses_only_the_dismiss_key():
    from test_daemon import keyboard_up

    async def go():
        d, stop, task = await keyboard_up()
        s = d.session
        s.tree = [*s.tree, _hosted_button("Done", "unknown")]
        await d.run_tool("a", "look", {"screenshot": False})
        try:
            await d.run_tool("a", "tap", {"target": {"id": "save"}})
            assert s.tap_points == [(60.0, 320.0)] and "swipe" not in s.calls and "dismiss_keyboard" in s.calls
        finally:
            await finish(stop, task)
    run(go)


def test_m2_a_refusal_after_a_reflex_touch_never_says_nothing_was_touched():
    from test_daemon import keyboard_up

    async def go():
        d, stop, task = await keyboard_up(calls_touch_hook=True)
        s = d.session
        s.tree = [*s.tree, _hosted_button("Done", "moved")]  # the phone's hit test confirms nothing (hit_answer None)
        await d.run_tool("a", "look", {"screenshot": False})
        try:
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "tap", {"target": {"label": "Done"}})
            assert s.tap_points  # the reflex's blank touch went out
            assert "nothing was touched" not in str(e.value) and "nothing else was touched" in str(e.value)
        finally:
            await finish(stop, task)
    run(go)


# L: the targetless-typing app check after a look and after open_url

def test_l_after_a_look_typing_never_goes_into_an_app_that_came_forward():
    async def go():
        d, s = _files_then_safari()
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            _safari_now(s, snapshot_too=True)
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "type_text", {"text": "x"})
            assert e.value.code == "stale" and s.typed == []
        finally:
            await finish(stop, task)
    run(go)


def test_l_after_open_url_typing_goes_into_the_app_the_link_opened():
    async def go():
        d, s = _files_then_safari()
        stop, task = await started(d)
        real = s.open_url

        async def open_url(url):
            out = await real(url)
            _safari_now(s, snapshot_too=True)  # the link opened in Safari
            return out
        s.open_url = open_url
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            await d.run_tool("a", "tap", {"target": {"label": "Search"}})
            await d.run_tool("a", "open_url", {"url": "https://example.com"})
            await d.run_tool("a", "type_text", {"text": "x"})
            assert len(s.typed) == 1
        finally:
            await finish(stop, task)
    run(go)

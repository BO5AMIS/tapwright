"""2.0, seen on the phone 2026-11-26 (TikTok's feed): `wait_for {"text": "Home"}` found the text in the picture, and the
`tap {"text": "Home"}` after it answered not_found. A tap or a long press aimed by text on a screen whose elements
weren't read now finds the text in the picture too, and touches the centre of the line that shows it."""
import pathlib

import pytest

from fakes import button
from test_daemon import finish, make, started
from test_vision_200 import feed_session, run

from tapwright import daemon as daemon_module
from tapwright import ocr, ui
from tapwright.control import Refused

TABS = [ocr.Seen("Home", (28.0, 883.0, 30.0, 8.0), 1.0), ocr.Seen("Friends", (108.0, 883.0, 39.0, 9.0), 1.0),
        ocr.Seen("Profile", (369.0, 883.0, 34.0, 8.0), 1.0), ocr.Seen("Edit profile", (150.0, 300.0, 90.0, 16.0), 1.0),
        ocr.Seen("Home", (42.0, 19.0, 30.0, 12.0), 1.0)]  # (the last one is in the status bar: the phone's, not the app's)
NOTE = ("Note: this screen's elements weren't read; \"Friends\" was found in the text read from its picture, and the touch "
        "went to its centre.")


@pytest.fixture(autouse=True)
def no_pc_ocr(monkeypatch):
    monkeypatch.setattr(ocr, "reader", None)
    monkeypatch.setattr(ocr, "available", lambda: False)


def test_a_tap_by_text_finds_it_in_the_picture_of_a_screen_with_no_elements():
    async def go():
        s = feed_session(text=TABS)
        d = make(s)
        stop, task = await started(d)
        r = await d.run_tool("a", "tap", {"target": {"text": "Friends"}, "screenshot": False})
        lines = r["text"].splitlines()
        assert lines[0] == "Tapped (128, 888)." and s.calls.count("tap") == 1 and NOTE in lines
        await finish(stop, task)
    run(go)


def test_the_text_itself_comes_before_a_longer_line_that_holds_it():
    async def go():
        s = feed_session(text=TABS)
        d = make(s)
        stop, task = await started(d)
        r = await d.run_tool("a", "tap", {"target": {"text": "profile"}, "screenshot": False})  # ("Edit profile" holds it too)
        assert r["text"].startswith("Tapped (386, 887).")
        r = await d.run_tool("a", "long_press", {"target": {"text": "Edit prof"}, "screenshot": False})  # a part of a line
        assert "(195, 308)" in r["text"].splitlines()[0]
        await finish(stop, task)
    run(go)


def test_several_lines_that_show_it_are_listed_as_o_refs_and_index_picks_one():
    async def go():
        both = [*TABS, ocr.Seen("Home", (200.0, 500.0, 40.0, 14.0), 1.0)]
        s = feed_session(text=both)
        d = make(s)
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"text": "Home"}})
        assert e.value.code == "ambiguous" and "tap" not in s.calls
        listed = [line for line in str(e.value).splitlines() if line.startswith("o")]
        assert len(listed) == 2 and all('"Home"' in line for line in listed)  # never the status bar's
        first = listed[0].split()[0]
        r = await d.run_tool("a", "tap", {"target": {"ref": first}, "screenshot": False})  # the refs are of this reading
        assert r["text"].startswith("Tapped (")
        r = await d.run_tool("a", "tap", {"target": {"text": "Home", "index": 0}, "screenshot": False})
        assert r["text"].startswith("Tapped (") and s.calls.count("tap") == 2
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"text": "Home", "index": 2}})
        assert e.value.code == "not_found" and "index 2" in str(e.value)
        # (CodeRabbit, PR #26) a not_found only because the screen wasn't read: no failed route, no break to repair
        assert daemon_module.cut_refusal(e.value) and daemon_module.HINT_NO_ELEMENTS in str(e.value)
        await finish(stop, task)
    run(go)


def test_a_text_the_picture_doesnt_show_is_not_found_with_the_hint():
    async def go():
        s = feed_session(text=TABS)
        d = make(s)
        stop, task = await started(d)
        for target in ({"text": "Settings"}, {"label": "Home"}, {"id": "Home"}):  # (only {text} is looked for in the picture)
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "tap", {"target": target})
            assert e.value.code == "not_found" and daemon_module.HINT_NO_ELEMENTS in str(e.value), target
        with pytest.raises(Refused) as e:  # a switch is an element, never a point of the picture
            await d.run_tool("a", "toggle", {"target": {"text": "Home"}})
        assert e.value.code == "not_found" and "tap" not in s.calls
        await finish(stop, task)
    run(go)


def test_a_dry_run_says_where_the_touch_would_go_and_touches_nothing():
    async def go():
        s = feed_session(text=TABS)
        d = make(s)
        stop, task = await started(d)
        r = await d.run_tool("a", "tap", {"target": {"text": "Friends"}, "dry_run": True})
        assert r["text"].splitlines() == ["Would tap at 127.5,887.5.", NOTE.replace("went", "would go")]
        assert "tap" not in s.calls
        await finish(stop, task)
    run(go)


def test_a_move_of_explore_or_a_repair_is_never_aimed_by_the_picture():
    async def go():
        s = feed_session(text=TABS)
        d = make(s)
        stop, task = await started(d)
        token = daemon_module._TOUCH_FLOOR.set(object())
        try:
            assert await d._by_picture_text("a", d.control.client("a"), "tap", {"target": {"text": "Friends"}},
                                            ui.Target(text="Friends")) is None
        finally:
            daemon_module._TOUCH_FLOOR.reset(token)
        assert "tap" not in s.calls
        await finish(stop, task)
    run(go)


def test_a_text_no_element_carries_is_found_in_the_picture_of_a_screen_read_whole_too():
    """(2.0.1, seen on a sign-in sheet) The look listed `o4 "Your email address"`, the screen had read whole with no
    element carrying it, and `tap {"text": ...}` answered not_found."""
    async def go():
        s = feed_session(tier="full", text=TABS)
        d = make(s)
        stop, task = await started(d)
        r = await d.run_tool("a", "tap", {"target": {"text": "Friends"}, "screenshot": False})
        lines = r["text"].splitlines()
        assert lines[0] == "Tapped (128, 888)." and s.calls.count("tap") == 1
        assert NOTE.replace("this screen's elements weren't read", "no element of this screen shows it") in lines
        with pytest.raises(Refused) as e:  # a text the picture doesn't show either: not_found, as before
            await d.run_tool("a", "tap", {"target": {"text": "Settings"}})
        assert e.value.code == "not_found" and daemon_module.HINT_NO_ELEMENTS not in str(e.value)
        with pytest.raises(Refused) as e:  # only tap and long_press: a switch is an element
            await d.run_tool("a", "toggle", {"target": {"text": "Friends"}})
        assert e.value.code == "not_found" and s.calls.count("tap") == 1
        await finish(stop, task)
    run(go)


def test_on_a_screen_read_whole_the_refusals_dont_say_its_elements_werent_read():
    async def go():
        both = [*TABS, ocr.Seen("Home", (200.0, 500.0, 40.0, 14.0), 1.0)]
        s = feed_session(tier="full", text=both)
        d = make(s)
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"text": "Home"}})
        said = str(e.value)
        assert e.value.code == "ambiguous" and "no element of this screen shows it" in said and "tap" not in s.calls
        assert len([line for line in said.splitlines() if line.startswith("o")]) == 2
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"text": "Home", "index": 2}})
        said = str(e.value)
        assert e.value.code == "not_found" and "index 2" in said and "weren't read" not in said
        assert not daemon_module.cut_refusal(e.value)  # (no hint of a screen that wasn't read: it was)
        await finish(stop, task)
    run(go)


# ---- review R201 (2.0.1): what a {text} may match in the picture of a screen that was read whole ----
KEY = ocr.Seen("Send", (330.0, 860.0, 60.0, 30.0), 1.0)  # the keyboard's return key: in the picture, in no list
DRAWN = ocr.Seen("Send", (320.0, 400.0, 40.0, 16.0), 1.0)  # drawn by the app, no element


def whole_session(text, keyboard=None):
    s = feed_session(tier="full", text=text)
    s.screen = (430.0, 932.0)
    if keyboard is not None:
        s.last_keyboard, s.last_insets = True, (None, keyboard)
    return s


def test_on_a_screen_read_whole_a_key_of_the_keyboard_is_never_what_a_text_means():
    """(R201 M1) The app's send button is an icon with no label, the keyboard's return key reads "Send": a tap by
    {"text": "Send"} answered not_found in 2.0.0 and must not press the key now."""
    async def go():
        s = whole_session([KEY], keyboard=336.0)  # the keys start at y 596
        d = make(s)
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"text": "Send"}})
        assert e.value.code == "not_found" and "tap" not in s.calls
        s.runner_text = [KEY, DRAWN]  # the same word drawn above the keyboard is the app's
        r = await d.run_tool("a", "tap", {"target": {"text": "Send"}, "screenshot": False})
        # (CodeRabbit, PR #28) one touch, where the text was read: the keyboard is not closed first, which could move
        # the form and leave the point on the text's old place
        assert r["text"].startswith("Tapped (340, 408).") and s.calls.count("tap") == 1
        assert not any(c == "dismiss_keyboard" or (isinstance(c, tuple) and c[0] == "dismiss_keyboard") for c in s.calls)
        # the keyboard is up and the read doesn't say where: nothing is matched
        s.last_keyboard, s.last_insets = True, (None, None)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"text": "Send"}})
        assert e.value.code == "not_found" and s.calls.count("tap") == 1
        await finish(stop, task)
    run(go)


def test_an_index_past_the_elements_that_match_is_not_counted_in_the_pictures_lines():
    """(R201 L1) One element shows "Home" and the agent passes index 1: the elements' own not_found, as in 2.0.0."""
    async def go():
        s = whole_session(TABS)
        s.tree = [button("e1", "Home", rect=(10, 300, 100, 40))]
        d = make(s)
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"text": "Home", "index": 1}})
        assert e.value.code == "not_found" and "picture" not in str(e.value) and "tap" not in s.calls
        await finish(stop, task)
    run(go)


def test_a_step_of_a_recorded_script_is_not_aimed_by_the_picture_on_a_screen_read_whole():
    """(R201 L2) Its not_found is a break to report or repair, not a word to find somewhere in the picture."""
    async def go():
        s = whole_session(TABS)
        d = make(s)
        stop, task = await started(d)
        token = daemon_module.SCRIPT.set({"path": pathlib.Path("flow.json"), "update": False})
        try:
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "tap", {"target": {"text": "Friends"}})
        finally:
            daemon_module.SCRIPT.reset(token)
        assert e.value.code == "not_found" and "tap" not in s.calls
        await finish(stop, task)
    run(go)


@pytest.mark.parametrize("tier", ["full", "root"])
def test_a_banners_own_words_are_never_a_match_and_the_touch_waits_a_banner_out(tier, monkeypatch):
    """(R201 M1) A banner over the top reads "Reply when you can": tap {"text": "Reply"} must not open its app."""
    async def go():
        banner = ocr.Seen("Reply when you can", (60.0, 90.0, 200.0, 18.0), 1.0)
        s = whole_session([banner]) if tier == "full" else feed_session(text=[banner])
        d = make(s)
        stop, task = await started(d)
        shown = [True]

        async def banner_shown():
            return shown[0]
        monkeypatch.setattr(d, "_banner_shown", banner_shown)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"text": "Reply"}})
        assert e.value.code == "not_found" and "tap" not in s.calls
        shown[0] = False  # no banner: the line is the app's own
        guarded = []
        original = d._guards_banners

        def guards(name, target):
            guarded.append(original(name, target))
            return guarded[-1]
        monkeypatch.setattr(d, "_guards_banners", guards)
        s.tw_version = 11
        r = await d.run_tool("a", "tap", {"target": {"text": "Reply"}, "screenshot": False})
        assert r["text"].startswith("Tapped (") and s.calls.count("tap") == 1
        assert guarded and guarded[-1] is True  # the point is the text's, not the agent's own: held for a banner
        assert original("tap", ui.Target(x=160.0, y=99.0)) is False  # (a point the agent chose itself is not)
        await finish(stop, task)
    run(go)

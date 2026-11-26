"""Tapwright 2.0.3, round H: review RG's findings (its probes, moved here) and what the phone showed about the hit test
on slow apps (2026-11-27): a flick has a length and a time; another process at the top of the app the agent saw is
unsure, a call's banner over; the home screen's own elements; landscape asked at the portrait point; "no element
answered" asked again and, still none, refused with words of its own; never more than one hit question out, and none
for HIT_QUIET after a silent one; what a shallow answer must hold to count."""
import asyncio

import pytest
from fakes import button
from test_banner_hit_203 import BANNER, _home, _looked, banner_up, springboard
from test_daemon import finish
from test_touch_other_app_203 import SHOP, quick_banner, refused, run, unread  # noqa: F401 (fixture)

from tapwright import session as session_module
from tapwright.daemon import _flick_up
from tapwright.device import DeviceError, Unanswered
from tapwright.session import _has_elements

NOTHING = DeviceError(f"unsupported operation: {session_module.HIT_NOTHING}")


def other(bundle, rect=(8, 11, 414, 76), kind="XCUIElementTypeOther"):
    return {"t": kind, "r": list(rect), "b": bundle, "identity": "verified"}


# ---- H1: a flick has a length and a time (review RG's probes) ----

@pytest.mark.parametrize("swipe", [
    {"x0": 215, "y0": 16, "x1": 215, "y1": 9},  # 7 pt: a tap on the banner
    {"x0": 215, "y0": 12, "x1": 215, "y1": 10},  # 2 pt
    {"x0": 215, "y0": 45, "x1": 215, "y1": 8},  # 37 pt: short of FLICK_MIN
    {"x0": 215, "y0": 70, "x1": 215, "y1": 2, "seconds": 10},  # a long press first
    {"x0": 215, "y0": 70, "x1": 215, "y1": 2, "seconds": 0.6},
])
def test_rg_h1_a_tap_shaped_or_slow_flick_on_the_banner_is_held(swipe):
    assert not _flick_up(swipe)

    async def go():
        s = unread()
        d, stop, task = await _looked(s)
        banner_up(s)
        e = await refused(d, "swipe", {**swipe, "look": False})
        assert str(e) == session_module.COVERED_BANNER and s.sent == []
        await finish(stop, task)
    run(go)


def test_a_flick_of_40_pt_in_half_a_second_goes():
    assert _flick_up({"x0": 215, "y0": 50, "x1": 215, "y1": 10, "seconds": 0.5})
    assert _flick_up({"x0": 215, "y0": 70, "x1": 215, "y1": 2})  # (the swipe's own 0.35 s)


# ---- H2: other processes at the top of the app the agent saw ----

def test_rg_h2_a_calls_banner_over_the_app_is_over():
    async def go():
        s = unread()
        d, stop, task = await _looked(s)
        s.hit = lambda x, y: other(session_module.IN_CALL) if y < 87 else other(s.app[1], (0, 0, 10, 10))
        e = await refused(d, "tap", {"target": {"x": 200, "y": 60}, "look": False})
        assert str(e) == session_module.COVERED_BANNER and s.sent == []
        await finish(stop, task)
    run(go)


def test_rg_h2_another_processs_element_over_the_app_is_unsure_and_an_element_goes():
    async def go():
        s = unread()
        s.tree, s.tier = [button("e1", "Menu", rect=(20, 70, 60, 30))], "full"
        d, stop, task = await _looked(s)
        s.hit = lambda x, y: other("com.apple.GameOverlayUI", (81, 79, 268, 56), "XCUIElementTypeButton")
        e = await refused(d, "tap", {"target": {"x": 200, "y": 85}, "look": False})
        assert str(e) == session_module.COVERED_UNSURE and s.sent == []
        await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False})
        assert s.sent == [("tap", (50.0, 85.0))]
        await finish(stop, task)
    run(go)


@pytest.mark.parametrize("bundle", ["com.apple.WebKit.WebContent", "com.apple.InputUI", SHOP[1]])
def test_a_web_pages_content_the_keyboard_and_the_app_itself_are_clear(bundle):
    async def go():
        s = unread()
        d, stop, task = await _looked(s)
        s.hit = lambda x, y: other(bundle, (0, 60, 430, 40), "XCUIElementTypeStaticText")
        await d.run_tool("a", "tap", {"target": {"x": 200, "y": 85}, "look": False})
        assert s.sent == [("tap", (200, 85))]
        await finish(stop, task)
    run(go)


# ---- M1: the home screen's own elements ----

def test_rg_m1_an_element_of_springboards_own_screen_in_the_zone_goes_on_the_home_screen():
    async def go():
        s = _home()
        s.tree, s.tier = [button("e1", "Module", rect=(20, 110, 170, 70))], "full"
        d, stop, task = await _looked(s)
        s.hit = lambda x, y: springboard([20, 110, 170, 70], "XCUIElementTypeButton")  # (the element itself)
        await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False})
        s.hit = lambda x, y: springboard([30, 120, 40, 40], "XCUIElementTypeImage")  # (inside it)
        await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False})
        assert s.sent == [("tap", (105.0, 145.0))] * 2
        await finish(stop, task)
    run(go)


def test_on_the_home_screen_an_element_with_a_banner_over_it_waits_the_banner_out():
    async def go():
        s = _home()
        s.tree, s.tier = [button("e1", "Module", rect=(20, 110, 170, 70))], "full"
        d, stop, task = await _looked(s)
        s.hit = lambda x, y: springboard(BANNER)
        e = await refused(d, "tap", {"target": {"ref": "e1"}, "look": False})
        assert str(e) == session_module.COVERED_BANNER and s.sent == [] and len(s.hit_questions) >= 2
        await finish(stop, task)
    run(go)


# ---- M2: landscape ----

def test_rg_m2_landscape_with_the_turn_unknown_refuses_a_point_in_the_zone():
    async def go():
        s = unread()
        d, stop, task = await _looked(s)
        banner_up(s)
        s.screen = (932.0, 430.0)  # (turned, which side up not said: turn stays 0)
        e = await refused(d, "tap", {"target": {"x": 200, "y": 85}, "look": False})
        assert str(e) == session_module.COVERED_UNSURE and s.sent == [] and s.hit_questions == []
        await finish(stop, task)
    run(go)


@pytest.mark.parametrize("turn,want", [(90, (85.0, 732.0)), (270, (345.0, 200.0))])
def test_landscape_asks_the_hit_test_at_the_portrait_point(turn, want):
    async def go():
        s = unread()
        d, stop, task = await _looked(s)
        s.screen, s.turn = (932.0, 430.0), turn
        s.hit = lambda x, y: springboard([300, 11, 120, 600])  # (a banner along that side, in portrait points)
        e = await refused(d, "tap", {"target": {"x": 200, "y": 85}, "look": False})
        assert str(e) == session_module.COVERED_BANNER and s.hit_questions[0] == want
        await finish(stop, task)
    run(go)


# ---- M6 and M5: "no element answered", a silence, one question out ----

def test_rg_m6_no_element_answered_below_the_status_bar_three_times_is_refused_as_a_slow_app():
    async def go():
        s = unread()
        d, stop, task = await _looked(s)
        s.hit = lambda x, y: NOTHING
        e = await refused(d, "tap", {"target": {"x": 200, "y": 85}, "look": False})
        assert e.code == "not_actionable" and str(e) == session_module.COVERED_SLOW and s.sent == []
        assert len(s.hit_questions) == session_module.HIT_TRIES == 3
        await finish(stop, task)
    run(go)


def test_the_phones_feed_two_no_answers_then_the_apps_own_the_touch_goes():
    async def go():
        s = unread()
        d, stop, task = await _looked(s)
        s.hit = lambda x, y: NOTHING if len(s.hit_questions) <= 2 else other(s.app[1], (0, 0, 430, 932))
        await d.run_tool("a", "tap", {"target": {"x": 200, "y": 85}, "look": False})
        assert s.sent == [("tap", (200, 85))] and len(s.hit_questions) == 3
        await finish(stop, task)
    run(go)


def test_no_element_answered_at_the_top_edge_is_an_answer():
    async def go():
        s = unread()
        d, stop, task = await _looked(s)
        s.hit = lambda x, y: NOTHING
        await d.run_tool("a", "swipe", {"x0": 400, "y0": 2, "x1": 400, "y1": 400, "look": False})
        assert s.sent == [("swipe", (400, 2))] and len(s.hit_questions) == 1
        await finish(stop, task)
    run(go)


def test_after_a_silent_hit_question_none_is_sent_for_hit_quiet(monkeypatch):
    monkeypatch.setattr(session_module, "HIT_QUIET", 0.3)

    async def go():
        s = unread()
        d, stop, task = await _looked(s)
        s.hit = lambda x, y: Unanswered("no answer in 1.5 s")
        e = await refused(d, "tap", {"target": {"x": 200, "y": 85}, "look": False})
        assert str(e) == session_module.COVERED_SLOW and len(s.hit_questions) == 1
        e = await refused(d, "tap", {"target": {"x": 200, "y": 85}, "look": False})  # within HIT_QUIET: not asked
        assert str(e) == session_module.COVERED_SLOW and len(s.hit_questions) == 1
        await asyncio.sleep(0.35)
        s.hit = lambda x, y: other(s.app[1], (0, 0, 430, 932))
        await d.run_tool("a", "tap", {"target": {"x": 200, "y": 85}, "look": False})
        assert len(s.hit_questions) == 2 and s.sent == [("tap", (200, 85))]
        await finish(stop, task)
    run(go)


def test_never_more_than_one_hit_question_out():
    async def go():
        s = unread()
        out = {"now": 0, "most": 0}

        async def call(method, path, payload=None, timeout=None, again=True):
            out["now"] += 1
            out["most"] = max(out["most"], out["now"])
            await asyncio.sleep(0.02)
            out["now"] -= 1
            return {"value": other(s.app[1], (0, 0, 430, 932))}
        s._call = call
        said = await asyncio.gather(*(s.banner_at((200.0, 85.0), SHOP[1]) for _ in range(6)))
        assert said == [session_module.BANNER_CLEAR] * 6 and out["most"] == 1
    run(go)


# ---- M3: what a shallow answer must hold (review RG's second probe) ----

def test_rg_m3_an_application_and_a_window_alone_is_not_an_answer_with_elements():
    window = ('<XCUIElementTypeApplication name="Shop"><XCUIElementTypeWindow x="0" y="0" width="430" '
              'height="932"/></XCUIElementTypeApplication>')
    assert not _has_elements(window)
    named = ('<XCUIElementTypeApplication name="Shop"><XCUIElementTypeWindow><XCUIElementTypeButton name="search-bar"/>'
             '</XCUIElementTypeWindow></XCUIElementTypeApplication>')
    assert _has_elements(named)
    many = ('<XCUIElementTypeApplication name="Shop">' + "<XCUIElementTypeOther/>" * (session_module.PARTIAL_MIN + 1)
            + "</XCUIElementTypeApplication>")
    assert _has_elements(many) and not _has_elements(many.replace("<XCUIElementTypeOther/>", "", 1))
    assert not _has_elements("not xml <")


# ---- L4: no picture since the kept one ----

def test_no_frame_since_the_kept_one_is_unseen_not_same():
    from test_slow_readable_203 import Stream

    from tapwright.daemon import StillMark
    stream = Stream()
    stream.idle(0.2)
    kept = StillMark(stream.d.hub.latest)
    assert stream.d._other_screen(kept) is None  # (no frame since: the stream sends none for a while after a read)
    stream.idle(0.2)
    assert stream.d._other_screen(kept) is False

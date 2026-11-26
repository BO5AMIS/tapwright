"""Tapwright 2.0.3, round G (phone 2026-11-27, iOS 26.5): while a timer's banner showed, the runner's front route and
activeAppInfo named the app the whole time, so the banner check let a point tap at (200, 85) go onto the banner. The
runner's hit test names SpringBoard at the banner's points (frame [11, 11, 408, 490]) and the app below it; the status
bar is SpringBoard's at all times (frames ending above 64 pt). Rule A now asks the hit test at a touch's start in the
banner zone; a flick up (the swipe tool) is the way to send a banner away."""
import pytest
from test_daemon import finish, make, started
from test_touch_other_app_203 import (SHOP, PointSession, _Hands, quick_banner, refused, run,  # noqa: F401 (fixture)
                                      unread)

from tapwright import session as session_module
from tapwright.control import Refused
from tapwright.device import DeviceError
from tapwright.session import SPRINGBOARD, Session

BANNER = [11, 11, 408, 490]  # the timer's banner as the hit test framed it


def springboard(rect, kind="XCUIElementTypeOther"):
    return {"t": kind, "r": list(rect), "b": SPRINGBOARD, "identity": "verified"}


def app_hit(s):
    return {"t": "XCUIElementTypeOther", "r": [0, 0, 430, 932], "b": s.app[1], "identity": "verified"}


def banner_up(s: PointSession, leaves_after: int | None = None) -> None:
    """The timer's banner: the route keeps naming the app (as on iOS 26.5), the hit test finds SpringBoard at the
    banner's points. `leaves_after`: the banner is gone from that many questions on."""
    def hit(x, y):
        gone = leaves_after is not None and len(s.hit_questions) > leaves_after
        inside = BANNER[0] <= x <= BANNER[0] + BANNER[2] and BANNER[1] <= y <= BANNER[1] + BANNER[3]
        return springboard(BANNER) if inside and not gone else app_hit(s)
    s.hit = hit


async def _looked(s):
    d = make(s)
    stop, task = await started(d)
    await d.run_tool("a", "look", {"screenshot": False})
    return d, stop, task


# ---- the incident ----

def test_the_incident_a_point_tap_on_a_timers_banner_the_route_doesnt_see_is_refused_covered():
    async def go():
        s = unread()
        d, stop, task = await _looked(s)
        banner_up(s)
        e = await refused(d, "tap", {"target": {"x": 200, "y": 85}, "look": False})
        assert e.code == "not_actionable" and str(e) == session_module.COVERED_BANNER
        assert s.sent == [] and s.front == (SHOP[1], 7)  # (the route named the app the whole time)
        assert (200.0, 85.0) in s.hit_questions and len(s.hit_questions) >= 2  # (asked again while it waited)
        await finish(stop, task)
    run(go)


def test_the_touch_goes_once_the_banner_has_left():
    async def go():
        s = unread()
        d, stop, task = await _looked(s)
        banner_up(s, leaves_after=2)
        await d.run_tool("a", "tap", {"target": {"x": 200, "y": 85}, "look": False})
        assert s.sent == [("tap", (200, 85))] and len(s.hit_questions) == 3
        await finish(stop, task)
    run(go)


def test_an_element_aimed_touch_in_the_zone_waits_the_banner_out_too():
    from fakes import button

    async def go():
        s = PointSession()
        s.tree = [button("e1", "Menu", rect=(20, 70, 60, 30))]
        d, stop, task = await _looked(s)
        banner_up(s)
        e = await refused(d, "tap", {"target": {"ref": "e1"}, "look": False})
        assert str(e) == session_module.COVERED_BANNER and s.sent == []
        await finish(stop, task)
    run(go)


def test_below_the_banner_zone_nothing_is_asked():
    async def go():
        s = unread()
        d, stop, task = await _looked(s)
        banner_up(s)
        await d.run_tool("a", "tap", {"target": {"x": 200, "y": 400}, "look": False})
        assert s.sent == [("tap", (200, 400))] and s.hit_questions == []
        await finish(stop, task)
    run(go)


@pytest.mark.parametrize("who", ["pause", "person"])
def test_a_pause_or_a_persons_touch_while_the_hit_tests_banner_is_waited_out_stops_the_touch(who):
    state = {"stop": False, "lock_held_in_wait": None}

    async def go():
        s = PointSession()
        s.touch_rules = session_module.TouchRules(session_module.Front(session_module.FRONT_APP, SHOP[1]))
        hands = _Hands()

        def hit(x, y):
            if len(s.hit_questions) == 2:  # the owner presses pause, or a person touches the phone, during the wait
                state["stop"] = True
                state["lock_held_in_wait"] = hands.lock.locked()
            return springboard(BANNER)
        s.hit = hit
        s.hands, s.hands_on = hands, (lambda: True)
        s.before_input, s.front_apps, s.hands_connection = None, None, None

        async def ensure():
            return None
        s.ensure = ensure

        async def guard():
            if state["stop"]:
                raise Refused("paused" if who == "pause" else "busy", "stopped")
        s.execution_guard = guard
        await s._touching(60, (200, 85))
        with pytest.raises(Refused):
            await Session._by_hands(s, "tap", lambda before: hands.tap(200, 85, 60, before=before))
        assert not hands.sent and state["lock_held_in_wait"] is False
    run(go)


# ---- the status bar, and when the hit test can't say or isn't asked ----

@pytest.mark.parametrize("at,frame,kind", [((200, 20), [122, 11, 156, 37], "XCUIElementTypeOther"),
                                           ((60, 30), [46, 11, 40, 22], "XCUIElementTypeStaticText")])
def test_the_status_bar_is_springboards_and_no_banner(at, frame, kind):
    async def go():
        s = unread()
        d, stop, task = await _looked(s)
        s.hit = lambda x, y: springboard(frame, kind)
        await d.run_tool("a", "tap", {"target": {"x": at[0], "y": at[1]}, "look": False})
        assert s.sent == [("tap", at)]
        await finish(stop, task)
    run(go)


@pytest.mark.parametrize("answer,words", [
    # (round H) no answer in its wait: the app in front is slow, with words of its own
    (TimeoutError(), session_module.COVERED_SLOW),
    (DeviceError("tapwright hit: busy"), session_module.COVERED_UNSURE),
    ({"t": "XCUIElementTypeOther", "r": [0, 0, 1, 1], "b": SHOP[1]}, session_module.COVERED_UNSURE),  # (not verified)
    ({"nonsense": True}, session_module.COVERED_UNSURE)])
def test_when_the_hit_test_cant_say_a_point_is_refused_and_an_element_goes(answer, words):
    from fakes import button

    async def go():
        s = unread()
        s.tree, s.tier = [button("e1", "Menu", rect=(20, 70, 60, 30))], "full"
        d, stop, task = await _looked(s)
        s.hit = lambda x, y: answer
        e = await refused(d, "tap", {"target": {"x": 200, "y": 85}, "look": False})
        assert e.code == "not_actionable" and str(e) == words
        await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False})
        assert s.sent == [("tap", (50.0, 85.0))]
        await finish(stop, task)
    run(go)


@pytest.mark.parametrize("how", ["check_off", "old_runner", "no_route"])  # (landscape is asked since round H)
def test_when_the_hit_test_isnt_asked_everything_is_as_before(how, monkeypatch):
    async def go():
        s = unread()
        d, stop, task = await _looked(s)
        banner_up(s)
        if how == "check_off":
            monkeypatch.setattr(session_module, "HIT_CHECK", "0")
        elif how == "old_runner":
            s.tw_version = 5  # (below the hit test's route version 6; below the front route's 11 too: no check)
        else:
            s.hit = lambda x, y: DeviceError("unknown command: GET /wda/tw/hit")
        await d.run_tool("a", "tap", {"target": {"x": 200, "y": 85}, "look": False})
        assert s.sent == [("tap", (200, 85))]
        await finish(stop, task)
    run(go)


# ---- the flick up ----

def test_a_flick_up_from_the_banner_goes_at_once():
    async def go():
        s = unread()
        d, stop, task = await _looked(s)
        banner_up(s)
        await d.run_tool("a", "swipe", {"x0": 215, "y0": 70, "x1": 215, "y1": 2, "look": False})
        assert s.sent == [("swipe", (215, 70))] and len(s.hit_questions) == 1
        await finish(stop, task)
    run(go)


@pytest.mark.parametrize("end", [(215, 400), (420, 60), (400, 5), (215, 30)])
def test_a_swipe_down_or_sideways_or_short_of_the_top_from_the_banner_is_held(end):
    async def go():
        s = unread()
        d, stop, task = await _looked(s)
        banner_up(s)
        e = await refused(d, "swipe", {"x0": 215, "y0": 70, "x1": end[0], "y1": end[1], "look": False})
        assert str(e) == session_module.COVERED_BANNER and s.sent == []
        await finish(stop, task)
    run(go)


def test_the_flick_is_the_swipe_tools_only():
    from tapwright.daemon import _flick_up
    assert _flick_up({"x0": 215, "y0": 70, "x1": 215, "y1": 2})
    assert _flick_up({"x0": 215, "y0": 70, "x1": 230, "y1": 10})
    assert not _flick_up({"x0": 215, "y0": 70, "x1": 215, "y1": 11})
    assert not _flick_up({"x0": 100, "y0": 70, "x1": 400, "y1": 2})
    assert not _flick_up({"x0": 215, "y0": 5, "x1": 215, "y1": 2})  # (3 pt: a tap, not a flick: review RG L3)
    d = make()
    view = d.control.client("a")
    flick = {"x0": 215, "y0": 70, "x1": 215, "y1": 2}
    assert d._touch_rules(view, "swipe", None, flick).flick
    for tool in ("scroll", "drag", "tap", "long_press", "back"):
        rules = d._touch_rules(view, tool, None, flick)
        assert rules is None or not rules.flick


def test_a_tap_on_the_banner_never_goes():
    async def go():
        s = unread()
        d, stop, task = await _looked(s)
        banner_up(s)
        e = await refused(d, "tap", {"target": {"x": 215, "y": 70}, "look": False})
        assert str(e) == session_module.COVERED_BANNER and s.sent == []
        await finish(stop, task)
    run(go)


# ---- the picture-text aim's banner check ----

def test_banner_shown_asks_the_hit_test_at_the_zones_probe_point():
    async def go():
        s = unread()
        s.read_app = SHOP
        s.screen = (430.0, 932.0)
        d = make(s)
        assert not await d._banner_shown()
        banner_up(s)
        assert await d._banner_shown() and s.hit_questions[-1] == (215.0, 80.0)
    run(go)


# ---- round G, the phone's additions: one definition of "over", the home screen, "nothing at the point" ----

def test_nothing_at_the_point_is_an_answer_and_the_touch_goes():
    async def go():
        s = unread()
        d, stop, task = await _looked(s)
        s.hit = lambda x, y: DeviceError(f"unsupported operation: {session_module.HIT_NOTHING}")
        await d.run_tool("a", "tap", {"target": {"x": 400, "y": 2}, "look": False})
        assert s.sent == [("tap", (400, 2))]
        await finish(stop, task)
    run(go)


@pytest.mark.parametrize("frame,kind", [([0, 0, 430, 54], "XCUIElementTypeOther"),  # the status bar, in an app
                                        ([370, 21, 31, 15], "XCUIElementTypeOther"),
                                        ([0, 0, 430, 932], "XCUIElementTypeOther"),  # the home screen's whole screen
                                        ([209, 91, 110, 111], "XCUIElementTypeIcon")])  # an icon
def test_what_isnt_over_by_the_one_definition(frame, kind):
    async def go():
        s = unread()
        s.screen = (430.0, 932.0)
        d, stop, task = await _looked(s)
        s.hit = lambda x, y: springboard(frame, kind)
        await d.run_tool("a", "tap", {"target": {"x": 215, "y": 30}, "look": False})
        assert s.sent == [("tap", (215, 30))]
        await finish(stop, task)
    run(go)


def _home():
    from test_touch_other_app_203 import HOME
    s = unread(HOME)
    s.screen = (430.0, 932.0)
    return s


def _home_hits(s, banner: bool):
    """The home screen as the hit test answered it on the phone, with or without the timer's banner."""
    def hit(x, y):
        if banner and BANNER[0] <= x <= BANNER[0] + BANNER[2] and BANNER[1] <= y <= BANNER[1] + BANNER[3] and y > 8:
            return springboard(BANNER)
        if y <= 8:
            return springboard([0, 0, 430, 932])  # (the top edge: SpringBoard's whole screen)
        if y < 50:
            return springboard([122, 11, 156, 37])  # (the island)
        return {"t": "XCUIElementTypeButton", "r": [20, 60, 390, 160], "b": "com.example.widgets",
                "identity": "verified"}  # (a widget)
    s.hit = hit


def test_on_the_home_screen_a_pull_from_the_top_edge_and_a_widget_go():
    async def go():
        s = _home()
        d, stop, task = await _looked(s)
        _home_hits(s, banner=False)
        await d.run_tool("a", "swipe", {"x0": 400, "y0": 2, "x1": 400, "y1": 400, "look": False})  # Control Center
        await d.run_tool("a", "tap", {"target": {"x": 200, "y": 85}, "look": False})  # a widget
        assert s.sent == [("swipe", (400, 2)), ("tap", (200, 85))]
        await finish(stop, task)
    run(go)


def test_on_the_home_screen_a_point_on_a_banner_is_refused_with_the_home_screens_words():
    async def go():
        s = _home()
        d, stop, task = await _looked(s)
        _home_hits(s, banner=True)
        e = await refused(d, "tap", {"target": {"x": 200, "y": 85}, "look": False})
        # (round H, review RG M1: SpringBoard's own screen can't be told from a banner's for a point)
        assert str(e) == session_module.COVERED_HOME and s.sent == [] and len(s.hit_questions) == 1
        await d.run_tool("a", "swipe", {"x0": 215, "y0": 70, "x1": 215, "y1": 2, "look": False})  # sent away
        assert s.sent == [("swipe", (215, 70))]
        await finish(stop, task)
    run(go)


def test_on_the_home_screen_when_the_hit_test_cant_say_a_point_is_refused_as_before():
    async def go():
        s = _home()
        d, stop, task = await _looked(s)
        s.hit = lambda x, y: DeviceError("tapwright hit: busy")  # (round H: a silence is "slow", with its own words)
        e = await refused(d, "tap", {"target": {"x": 200, "y": 100}, "look": False})
        assert str(e) == session_module.COVERED_HOME and s.sent == []
        await finish(stop, task)
    run(go)


# ---- round G: `back` on a sheet uses its own whole read ----

def _sheet_back(tier: str, frames_after: bool) -> dict:
    from fakes import FrameFeeder, button, el
    from test_203q_own_back import UPRIGHT
    from test_daemon import looked, on_loop_clock, run_virtual
    out = {}

    async def go():
        d = make()
        on_loop_clock(d)
        stop, task = await started(d)
        d.session.screen = UPRIGHT
        d.hub.publish(b"A")
        feeder = FrameFeeder(d.hub).start(then=b"A")
        sheet = el("e1", "other", "", "lab-sheet", rect=(0, 647, 430, 285))
        await looked(d, [sheet])
        d.session.behind = [(button("", "Row", "row", rect=(0, 300, 430, 80)), sheet)]
        d.session.front_items = [sheet]
        d.session.tier = tier
        reads = []
        point = d.session.back_point

        async def counted():
            reads.append(1)
            if not frames_after:
                await feeder.stop()  # (the runner sends no frames for a while after a long read)
            return await point()
        d.session.back_point = counted
        real = d.session.tap

        async def tap(target, hold_ms=60):
            d.session.behind = []  # the tap beside it closes the sheet
            d.hub.publish(b"B")
            return await real(target, hold_ms)
        d.session.tap = tap
        try:
            r = await d.run_tool("a", "back", {})
            out["text"] = r["text"]
        except Refused as e:
            out["refused"] = str(e)
        out["reads"], out["taps"] = len(reads), list(d.session.tap_points)
        await feeder.stop()
        await finish(stop, task)
    run_virtual(go)
    return out


def test_back_on_a_sheet_after_a_whole_read_and_no_fresh_frame_reads_once():
    out = _sheet_back("full", frames_after=False)
    assert out["reads"] == 1 and len(out["taps"]) == 1 and out.get("text", "").startswith("Closed the sheet.")


def test_back_on_a_sheet_after_a_cut_read_and_no_fresh_frame_reads_again_as_before():
    out = _sheet_back("shallow", frames_after=False)
    assert out["reads"] >= 2

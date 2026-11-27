"""Tapwright 2.0.3, round I: review RH's findings (its nine failing probes, moved here) and the lead's decision on RH-M2.
The aimed-element rule only on the home screen; once a banner was seen in a touch's wait only "clear" ends it; the hit
question's silence is "slow", never a dropped line; "slow" refuses an element in the zone too; one deadline for the
tries; a hit with no bundle is unsure on the home screen; hit_at under the same lock and quiet."""
import asyncio
import time

import pytest
from fakes import button
from test_banner_hit_203 import BANNER, _home, _looked
from test_daemon import finish
from test_touch_other_app_203 import SHOP, quick_banner, refused, run, unread  # noqa: F401 (fixture)

from tapwright import session as session_module
from tapwright.control import Refused
from tapwright.device import ConnectionLost, DeviceError, Unanswered
from tapwright.session import Session

CALL_OR_BANNER = [8, 11, 414, 76]  # a compact banner's frame (review RG's H2 probe)


def hit_of(bundle, rect, kind="XCUIElementTypeOther"):
    return {"t": kind, "r": list(rect), "b": bundle, "identity": "verified"}


# ---- RH-H1: the aimed-element rule on the home screen only ----

@pytest.mark.parametrize("bundle", [session_module.SPRINGBOARD, session_module.IN_CALL])
def test_rh_h1_a_banner_inside_a_large_aimed_element_of_the_app_is_not_clear(bundle):
    async def go():
        s = unread()
        s.tree, s.tier = [button("e1", "Header", rect=(0, 0, 430, 150))], "full"
        d, stop, task = await _looked(s)
        s.hit = lambda x, y: hit_of(bundle, CALL_OR_BANNER) if y < 87 else hit_of(s.app[1], (0, 0, 430, 932))
        e = await refused(d, "tap", {"target": {"ref": "e1"}, "look": False})
        assert e.code == "not_actionable" and str(e) == session_module.COVERED_BANNER and s.sent == []
        await finish(stop, task)
    run(go)


def test_rh_h1_the_same_rule_direct():
    s = unread()
    said = s._banner_of({"value": hit_of(session_module.SPRINGBOARD, CALL_OR_BANNER)}, (215.0, 75.0), SHOP[1],
                        (0.0, 0.0, 430.0, 150.0), False)
    assert said == session_module.BANNER_OVER
    home = s._banner_of({"value": hit_of(session_module.SPRINGBOARD, CALL_OR_BANNER)}, (215.0, 75.0), None,
                        (0.0, 0.0, 430.0, 150.0), False)
    assert home == session_module.BANNER_CLEAR  # (the home screen: the element the agent aimed at)


# ---- RH-H2: a banner seen, then a silence ----

def test_rh_h2_a_banner_seen_then_a_silence_does_not_let_an_element_touch_go():
    async def go():
        s = unread()
        s.tree, s.tier = [button("e1", "Menu", rect=(20, 70, 60, 30))], "full"
        d, stop, task = await _looked(s)
        s.hit = lambda x, y: (hit_of(session_module.SPRINGBOARD, BANNER) if len(s.hit_questions) <= 1
                              else Unanswered("no answer in 1.5 s"))
        e = await refused(d, "tap", {"target": {"ref": "e1"}, "look": False})
        assert e.code == "not_actionable" and str(e) == session_module.COVERED_BANNER and s.sent == []
        await finish(stop, task)
    run(go)


def test_rh_h2_the_hands_wait_ends_on_a_silence_too():
    async def go():
        s = unread()
        s.touch_rules = session_module.TouchRules(session_module.Front(session_module.FRONT_APP, SHOP[1]), True)
        s._gate_at = (50.0, 85.0)
        s.hit = lambda x, y: Unanswered("no answer in 1.5 s")
        with pytest.raises(Refused) as e:
            await s._wait_banner()
        assert str(e.value) == session_module.COVERED_BANNER
    run(go)


def test_a_banner_seen_then_clear_lets_the_touch_go():
    async def go():
        s = unread()
        s.tree, s.tier = [button("e1", "Menu", rect=(20, 70, 60, 30))], "full"
        d, stop, task = await _looked(s)
        s.hit = lambda x, y: (hit_of(session_module.SPRINGBOARD, BANNER) if len(s.hit_questions) <= 1
                              else hit_of(s.app[1], (0, 0, 430, 932)))
        await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False})
        assert s.sent == [("tap", (50.0, 85.0))]
        await finish(stop, task)
    run(go)


# ---- RH-M1: the hit question's silence through the real request path ----

class _Dev:
    rsd = object()

    def __init__(self, status_answers: bool):
        self.status_answers = status_answers
        self.asked: list[str] = []

    def log(self, *a):
        pass

    async def http(self, method, path, payload=None, timeout=30.0):
        self.asked.append(path)
        raise ConnectionLost(f"WebDriverAgent didn't answer {method} {path} within {timeout:.0f} s")

    async def wda_alive(self):
        self.asked.append("/status")
        if not self.status_answers:
            await asyncio.sleep(0.05)
            return False
        return True


def _real_session(status_answers: bool):
    s = object.__new__(Session)
    s.device = _Dev(status_answers)
    s.sid, s.front_apps, s.tw_version, s.turn, s.screen = "s1", None, 11, 0, (430.0, 932.0)
    s.execution_guard, s.before_input, s._gate_at, s.input_point = None, None, None, None

    async def ensure():
        return None
    s.ensure = ensure
    return s


@pytest.mark.parametrize("status_answers", [False, True])
def test_rh_m1_a_silent_hit_is_slow_never_a_dropped_line(status_answers):
    async def go():
        s = _real_session(status_answers)
        said = await s.banner_at((200.0, 85.0), SHOP[1])
        assert said == session_module.BANNER_SLOW
        assert s.sid == "s1" and getattr(s, "_hit_quiet_until", 0) > time.monotonic()
        assert "/status" not in s.device.asked and len(s.device.asked) == 1
    run(go)


# ---- RH-M2 (the lead): "slow" refuses an element in the zone too ----

def test_a_silence_refuses_an_element_in_the_zone_with_the_slow_words():
    async def go():
        s = unread()
        s.tree, s.tier = [button("e1", "Menu", rect=(20, 70, 60, 30))], "full"
        d, stop, task = await _looked(s)
        s.hit = lambda x, y: TimeoutError()
        e = await refused(d, "tap", {"target": {"ref": "e1"}, "look": False})
        assert str(e) == session_module.COVERED_SLOW and "aim at an element" not in str(e) and s.sent == []
        e = await refused(d, "tap", {"target": {"x": 200, "y": 85}, "look": False})  # (the quiet: no question)
        assert str(e) == session_module.COVERED_SLOW and len(s.hit_questions) == 1
        await finish(stop, task)
    run(go)


def test_another_reason_to_be_unsure_still_lets_an_element_go():
    async def go():
        s = unread()
        s.tree, s.tier = [button("e1", "Menu", rect=(20, 70, 60, 30))], "full"
        d, stop, task = await _looked(s)
        s.hit = lambda x, y: hit_of("com.apple.GameOverlayUI", (81, 79, 268, 56))
        await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False})
        assert s.sent == [("tap", (50.0, 85.0))]
        await finish(stop, task)
    run(go)


# ---- RH-L4: the home screen's silence ----

def test_the_home_screens_silence_refuses_a_point_and_an_element():
    async def go():
        s = _home()
        s.tree, s.tier = [button("e1", "Module", rect=(20, 110, 170, 70))], "full"
        d, stop, task = await _looked(s)
        s.hit = lambda x, y: TimeoutError()
        e = await refused(d, "tap", {"target": {"x": 200, "y": 100}, "look": False})
        assert str(e) == session_module.COVERED_SLOW
        e = await refused(d, "tap", {"target": {"ref": "e1"}, "look": False})
        assert str(e) == session_module.COVERED_SLOW and s.sent == []
        await finish(stop, task)
    run(go)


# ---- RH-L1: one deadline for the tries ----

def test_rh_l1_three_tries_stay_near_a_second():
    async def go():
        s = unread()
        waits = iter([0.9, 1.4, 0.3])

        async def call(method, path, payload=None, timeout=None, again=True, once=False):
            await asyncio.sleep(next(waits))
            raise DeviceError(f"unsupported operation: {session_module.HIT_NOTHING}")
        s._call = call
        began = time.monotonic()
        said = await s.banner_at((200.0, 85.0), SHOP[1])
        took = time.monotonic() - began
        assert said == session_module.BANNER_SLOW and took < 1.6, f"{took:.2f} s"
    run(go)


# ---- RH-L2: the home screen, a hit with no bundle ----

def test_rh_l2_home_screen_a_hit_without_a_bundle_is_not_clear():
    s = _home()
    said = s._banner_of({"value": {"t": "XCUIElementTypeOther", "r": CALL_OR_BANNER, "identity": "verified"}},
                        (200.0, 60.0), None, None, False)
    assert said == session_module.BANNER_UNSURE


# ---- RH-L5: landscape's top edge is the portrait panel's ----

def test_landscape_no_element_at_a_low_landscape_y_is_not_the_top_edge():
    async def go():
        s = unread()
        s.screen, s.turn = (932.0, 430.0), 90  # (landscape y 5 maps to portrait y 932 - x)
        s.hit = lambda x, y: DeviceError(f"unsupported operation: {session_module.HIT_NOTHING}")
        assert await s.banner_at((200.0, 5.0), SHOP[1]) == session_module.BANNER_SLOW
        assert s.hit_questions == [(5.0, 732.0)]  # (round K: one try, never repeated; no witness in landscape)
    run(go)


# ---- RH-L3: hit_at under the same lock and quiet ----

def test_hit_at_sends_nothing_during_the_quiet_and_shares_the_lock():
    async def go():
        s = unread()
        s.hit = lambda x, y: Unanswered("silent")
        assert await s.banner_at((200.0, 85.0), SHOP[1]) == session_module.BANNER_SLOW
        asked = len(s.hit_questions)

        async def ensure():
            return None
        s.ensure = ensure

        async def routes(strict=False):
            return None
        s._fast_routes = routes
        with pytest.raises(Refused) as e:  # (route version 11: a proof that can't be made refuses the touch)
            await Session.hit_at(s, 100.0, 300.0)
        assert getattr(e.value, "hit_check", {}).get("gate") == "hit_quiet" and len(s.hit_questions) == asked
    run(go)

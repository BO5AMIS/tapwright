"""Tapwright 2.0.3, round L: review RK's findings on round K. A "no element answered" is judged against the clock's own
round trip, and the clock's clear needs the hold confirmed by asking the clock once more (H1); hit_at's slow "nothing"
sets the hold (H2); under the hands' lock nothing waits for a held runner, and the gate after a banner wait takes the
wait's clear (M1); no witness-clear for a touch through the runner (M2); and the Lows."""
import asyncio
import time

import pytest
from test_banner_hit_203 import _home, _looked
from test_daemon import finish
from test_round_i_203 import hit_of
from test_round_k_203 import (APP, CLOCK, PLATE, WITNESS, _phone_session, answers, busy_app, nothing, scaled,  # noqa: F401
                              status_bar)
from test_touch_other_app_203 import SHOP, PointSession, quick_banner, refused, run, unread  # noqa: F401 (fixture)

from tapwright import session as session_module
from tapwright.control import Refused
from tapwright.device import DeviceError, Unanswered
from tapwright.session import SPRINGBOARD, Session


# ---- RK-H1: the link's time is no "slow" ----

class _LateDev:
    """The phone answers at once; the link adds `latency` to every question (or to the point's alone)."""
    rsd = object()

    def __init__(self, latency: float, point_only: bool = False):
        self.latency, self.point_only, self.asked = latency, point_only, []

    def log(self, *a):
        pass

    async def http(self, method, path, payload=None, timeout=30.0):
        q = path.split("?")[1] if "?" in path else ""
        witness = "x=66.00&y=22.00" in q
        if not (self.point_only and witness):
            await asyncio.sleep(self.latency)
        self.asked.append(q)
        if witness:
            return {"value": {"t": "XCUIElementTypeStaticText", "r": CLOCK, "b": SPRINGBOARD, "identity": "verified"}}
        raise DeviceError(f"unsupported operation: {session_module.HIT_NOTHING}")  # (SpringBoard's own: fast)


def _late_session(latency: float, point_only: bool = False):
    s = object.__new__(Session)
    s.device = _LateDev(latency, point_only)
    s.sid, s.front_apps, s.tw_version, s.turn, s.screen = "s1", None, 11, 0, (430.0, 932.0)
    s.execution_guard, s.before_input, s._gate_at, s.input_point = None, None, None, None

    async def ensure():
        return None
    s.ensure = ensure
    return s


@pytest.mark.parametrize("latency", [0.0, 0.2, 0.26, 0.4])
@pytest.mark.parametrize("point_only", [False, True])
def test_rk_h1_a_fast_nothing_late_on_the_link_is_never_clear(latency, point_only):
    async def go():
        s = _late_session(latency, point_only)
        said = await s.banner_at((215.0, 80.0), SHOP[1], hands=True)
        assert said == session_module.BANNER_OVER, (latency, point_only, said, s.device.asked)
        assert not s._held()
    run(go)


@pytest.mark.parametrize("confirm,want,held", [
    (nothing(), session_module.BANNER_CLEAR, True),  # the runner held: the app didn't answer the point
    (status_bar(), session_module.BANNER_OVER, False),  # the runner isn't held: the point's "nothing" was SpringBoard's
    (hit_of(SPRINGBOARD, PLATE), session_module.BANNER_OVER, False),
    (nothing(fast=True), session_module.BANNER_SLOW, False),
    (Unanswered("no answer"), session_module.BANNER_SLOW, False),
])
def test_rk_h1_the_hold_confirmed_by_the_clock_asked_once_more(confirm, want, held):
    async def go():
        s = unread()
        answers(s, lambda x, y: status_bar() if len(s.hit_questions) == 1 else confirm, nothing())
        assert await s.banner_at((215.0, 80.0), SHOP[1], hands=True) == want
        assert s.hit_questions == [WITNESS, (215.0, 80.0), WITNESS] and s._held() is held
        if isinstance(confirm, Unanswered):
            assert s._hit_quiet_until > time.monotonic()
    run(go)


# ---- RK-H2: hit_at's slow "no element answered" ----

class _HitAtDev:
    rsd = object()

    def __init__(self):
        self.asked: list[tuple[float, str]] = []
        self.t0 = time.monotonic()
        self.free_at = 0.0

    def log(self, *a):
        pass

    async def http(self, method, path, payload=None, timeout=30.0):
        bare = path.split("?")[0]
        self.asked.append((round(time.monotonic() - self.t0, 2), bare))
        if bare == session_module.HIT_PATH:
            if "x=66.00" in path and time.monotonic() >= self.free_at:
                return {"value": status_bar()}
            await asyncio.sleep(0.31)  # (the runner's 0.3 s wait for a process that doesn't answer)
            if "x=66.00" not in path:
                self.free_at = time.monotonic() + 0.6  # (the runner held until the process answers)
            raise DeviceError(f"unsupported operation: {session_module.HIT_NOTHING}")
        if bare == session_module.FRONT_PATH:
            return {"value": {"b": SHOP[1], "pid": 9}}
        raise AssertionError(path)


def test_rk_h2_hit_ats_slow_nothing_sets_the_hold_and_the_front_route_waits(monkeypatch):
    monkeypatch.setattr(session_module, "HIT_FREE_EVERY", 0.1)

    async def go():
        s = object.__new__(Session)
        s.device = _HitAtDev()
        s.sid, s.front_apps, s.tw_version, s.turn, s.screen = "s1", None, 15, 0, (430.0, 932.0)
        s.execution_guard, s.before_input, s._gate_at, s.input_point = None, None, None, None
        s.fast_tap = True

        async def ensure():
            return None
        s.ensure = ensure

        async def routes(strict=False):
            return None
        s._fast_routes = routes
        with pytest.raises(Refused) as e:
            await s.hit_at(215.0, 600.0)
        assert e.value.hit_check["gate"] == "hit_request_failed" and s._held()
        await s.front_app()
        hit_done = s.device.asked[0][0] + 0.31
        front_at = next(t for t, p in s.device.asked if p == session_module.FRONT_PATH)
        assert front_at - hit_done >= 0.5  # (it waited for the runner, at the clock, before the front route)
        assert any(p == session_module.HIT_PATH for t, p in s.device.asked[1:])
    run(go)


# ---- RK-M1: the hands' path, the gate after a banner wait ----

def _hands_regate_session(hold_for: float, plate_questions: int):
    """The reviewer's probe as a test: a plate at the point for the first `plate_questions` questions there, then the
    busy app, which holds the runner `hold_for` seconds after its question; the hands' path; a front route that waits
    for the runner (Session.front_app's channel_free)."""
    s = unread()
    s.by_hands, s.nothing_takes = True, 0.03
    runner = {"held_until": 0.0}
    log: list[str] = []
    at_point = {"n": 0}

    def hit(x, y):
        now = time.monotonic()
        if now < runner["held_until"]:
            return nothing()
        if (x, y) == WITNESS:
            return status_bar()
        at_point["n"] += 1
        if at_point["n"] <= plate_questions:
            return hit_of(SPRINGBOARD, PLATE)
        runner["held_until"] = now + hold_for
        log.append("APP ASKED")
        return nothing()
    s.hit = hit
    real_front = PointSession.front_app

    async def front_app():
        await s.channel_free()
        return await real_front(s)
    s.front_app = front_app
    return s, log


@pytest.mark.parametrize("hold_for", [0.25, 0.98])  # (2.5 s and 9.8 s on the phone, at a tenth)
def test_rk_m1_after_a_banner_wait_the_touch_goes_within_its_bound_the_app_asked_once(scaled, monkeypatch, hold_for):
    monkeypatch.setattr(session_module, "BANNER_WAIT", 0.8)
    monkeypatch.setattr(session_module, "BANNER_POLL", 0.05)

    async def go():
        s, log = _hands_regate_session(hold_for, plate_questions=3)
        d, stop, task = await _looked(s)
        began = time.monotonic()
        await d.run_tool("a", "tap", {"target": {"x": 215, "y": 80}, "look": False})
        took = time.monotonic() - began
        assert s.sent == [("tap", (215, 80))] and log == ["APP ASKED"]
        assert took < session_module.BANNER_WAIT + 0.3, f"{took:.2f} s"
        await finish(stop, task)
    run(go)


def test_rk_m1_a_stale_wait_clear_under_the_hands_lock_is_refused_with_nothing_asked():
    async def go():
        s = unread()
        s.touch_rules = session_module.TouchRules(session_module.Front(session_module.FRONT_APP, SHOP[1]))
        s._held_until = time.monotonic() + 5.0
        s._banner_until = time.monotonic() + 5.0
        s._wait_clear = ((215.0, 80.0), session_module.Front(session_module.FRONT_APP, SHOP[1]), time.monotonic() - 1.5)
        with pytest.raises(Refused) as e:
            await Session._front_rules(s, (215.0, 80.0), wait=False)
        assert str(e.value) == session_module.COVERED_SLOW and s.hit_questions == []
    run(go)


def test_rk_m1_a_hands_touch_with_the_runner_held_waits_for_it_outside_the_hands_lock(scaled):
    async def go():
        s = unread()
        s.by_hands = True
        s.touch_rules = session_module.TouchRules(session_module.Front(session_module.FRONT_APP, SHOP[1]))
        s._held_until = time.monotonic() + session_module.HIT_HELD
        free_at = time.monotonic() + 0.3
        s.hit = lambda x, y: nothing() if time.monotonic() < free_at else APP
        s.nothing_takes = 0.03
        with pytest.raises(session_module._HeldUp):
            await Session._front_rules(s, (200.0, 600.0), wait=False)  # (under the hands' lock: nothing waits)
        assert s.hit_questions == []
        await s._goes("tap", 60, (200.0, 600.0))  # (the hands' loop: channel_free with the lock let go, then again)
        assert s.sent == [("tap", (200.0, 600.0))] and not s._held() and time.monotonic() >= free_at
    run(go)


# ---- RK-M2: no witness-clear through the runner ----

def test_rk_m2_a_busy_app_through_the_runner_is_refused_and_by_the_hands_goes():
    async def go():
        s = unread()
        d, stop, task = await _looked(s)
        busy_app(s)
        e = await refused(d, "tap", {"target": {"x": 383, "y": 168}, "look": False})
        assert str(e) == session_module.COVERED_SLOW and s.sent == [] and s._held()
        assert s.hit_questions == [WITNESS, (383.0, 168.0)]  # (one question at the point, no confirm)
        s._held_until = 0.0
        s.hit_questions.clear()
        s.by_hands = True
        busy_app(s)
        await d.run_tool("a", "tap", {"target": {"x": 383, "y": 168}, "look": False})
        assert s.sent == [("tap", (383, 168))]
        await finish(stop, task)
    run(go)


# ---- the Lows ----

@pytest.mark.parametrize("case", ["home", "landscape", "status_bar_point"])
def test_rk_l1_a_fast_nothing_with_no_witness_is_over(case):
    async def go():
        s = _home() if case == "home" else unread()
        app = None if case == "home" else SHOP[1]
        at = (215.0, 40.0) if case == "status_bar_point" else (200.0, 100.0)
        if case == "landscape":
            s.screen, s.turn = (932.0, 430.0), 90
        s.hit = lambda x, y: nothing(fast=True)
        assert await s.banner_at(at, app) == session_module.BANNER_OVER
        assert len(s.hit_questions) == 1 and not s._held()
    run(go)


def test_rk_l2_a_slow_nothing_at_the_top_edge_is_clear_and_sets_the_hold():
    async def go():
        s = unread()
        s.hit = lambda x, y: nothing()
        assert await s.banner_at((400.0, 2.0), SHOP[1]) == session_module.BANNER_CLEAR
        assert s._held()
        f = unread()
        f.hit = lambda x, y: nothing(fast=True)
        assert await f.banner_at((400.0, 2.0), SHOP[1]) == session_module.BANNER_CLEAR and not f._held()
    run(go)


@pytest.mark.parametrize("first,ends", [(DeviceError("tapwright hit: busy"), False), (Unanswered("x"), False),
                                        (APP, True)])
def test_rk_l3_only_a_real_answer_ends_the_hold(scaled, first, ends):
    async def go():
        s = unread()
        s._held_until = time.monotonic() + session_module.HIT_HELD
        s.hit = lambda x, y: first
        assert await s._free_step() is ends
        assert s._held() is (not ends)
    run(go)


def test_rk_l3_channel_free_keeps_asking_after_an_error_or_a_silence_until_an_answer(scaled):
    async def go():
        s = unread()
        s._held_until = time.monotonic() + session_module.HIT_HELD
        answers_ = iter([DeviceError("tapwright hit: busy"), nothing(), APP])
        s.nothing_takes = 0.03
        s.hit = lambda x, y: next(answers_)
        await s.channel_free()
        assert not s._held() and len(s.hit_questions) == 3
    run(go)


def test_rk_l4_a_hold_set_while_waiting_for_the_lock_asks_nothing():
    async def go():
        s = unread()
        lock = s._hit_lock = asyncio.Lock()
        await lock.acquire()
        s.hit = lambda x, y: status_bar()
        queued = asyncio.create_task(s.banner_at((215.0, 80.0), SHOP[1]))
        await asyncio.sleep(0.05)
        s._held_until = time.monotonic() + 5.0  # (the lock's holder found the runner held)
        lock.release()
        assert await queued == session_module.BANNER_SLOW and s.hit_questions == []
    run(go)


def test_rk_l5_a_banner_wait_with_the_runner_held_goes_once_the_app_is_back(scaled, monkeypatch):
    monkeypatch.setattr(session_module, "BANNER_WAIT", 0.8)
    monkeypatch.setattr(session_module, "BANNER_POLL", 0.05)

    async def go():
        s = unread()
        d, stop, task = await _looked(s)
        # (a banner in motion at the point, then the clock says nothing slowly: held until the app is back at 0.2 s)
        back_at = {"t": None}

        def hit(x, y):
            if back_at["t"] is None:
                back_at["t"] = time.monotonic() + 0.2
                return nothing(fast=True)
            if time.monotonic() < back_at["t"]:
                return nothing()
            return status_bar() if (x, y) == WITNESS else APP
        s.hit = hit
        s.nothing_takes = 0.03
        await d.run_tool("a", "tap", {"target": {"x": 215, "y": 80}, "look": False})
        assert s.sent == [("tap", (215, 80))]
        await finish(stop, task)
    run(go)


def test_rk_l5_a_wait_that_only_ever_saw_a_banner_in_motion_ends_with_the_slow_words(scaled, monkeypatch):
    monkeypatch.setattr(session_module, "BANNER_WAIT", 0.4)
    monkeypatch.setattr(session_module, "BANNER_POLL", 0.05)

    async def go():
        s = unread()
        d, stop, task = await _looked(s)
        first = {"done": False}

        def hit(x, y):
            if not first["done"]:
                first["done"] = True
                return nothing(fast=True)  # (a banner in motion)
            return nothing()  # (then the runner held, never back)
        s.hit = hit
        s.nothing_takes = 0.03
        began = time.monotonic()
        e = await refused(d, "tap", {"target": {"x": 215, "y": 80}, "look": False})
        assert str(e) == session_module.COVERED_SLOW and s.sent == []
        assert time.monotonic() - began < session_module.BANNER_WAIT + 0.4
        await finish(stop, task)
    run(go)

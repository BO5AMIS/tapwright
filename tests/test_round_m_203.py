"""Tapwright 2.0.3, round M: review RL's findings on round L. Two lines (RL-H1): any "no element answered" of
HIT_NOTHING_SLOW or more sets the hold, whatever the verdict; clear needs a prompt first answer at the clock and the
point's and the confirm's "nothing" each HIT_NOTHING_MARGIN slower than it. A clock that answered the status bar, but
not promptly, still lets the point decide (the lead's precision). The known limit of RL-H2 documented. The wait for a
held runner before a hands touch is inside the touch's single deadline (RL-L2)."""
import asyncio
import time

import pytest
from test_round_k_203 import CLOCK, WITNESS, busy_app, nothing, status_bar  # noqa: F401
from test_round_i_203 import hit_of
from test_touch_other_app_203 import SHOP, quick_banner, run, unread  # noqa: F401 (fixture)

from tapwright import session as session_module
from tapwright.control import Refused
from tapwright.device import DeviceError
from tapwright.session import SPRINGBOARD, Front, Session, TouchRules, _BannerUp

APP_ELEMENT = {"t": "XCUIElementTypeOther", "r": [0, 0, 430, 932], "b": SHOP[1], "identity": "verified"}


class _TimedDev:
    """A phone whose hit test answers by a script: `script(n, path)` gives (seconds, answer or exception) for the n-th
    hit question; the front route answers at once and notes whether the runner was held then (`held_until`)."""
    rsd = object()

    def __init__(self, script):
        self.script, self.n = script, 0
        self.asked: list[tuple[float, str]] = []
        self.held_until = 0.0
        self.t0 = time.monotonic()
        self.front_into_held = False

    def log(self, *a):
        pass

    async def http(self, method, path, payload=None, timeout=30.0):
        bare = path.split("?")[0]
        now = time.monotonic()
        self.asked.append((round(now - self.t0, 3), path.split("?")[-1] if bare == session_module.HIT_PATH else bare))
        if bare == session_module.FRONT_PATH:
            self.front_into_held = self.front_into_held or now < self.held_until
            return {"value": {"b": SHOP[1], "pid": 9}}
        self.n += 1
        took, answer = self.script(self.n, path, self)
        await asyncio.sleep(took)
        if isinstance(answer, BaseException):
            raise answer
        return {"value": answer}


def _session(script):
    s = object.__new__(Session)
    s.device = _TimedDev(script)
    s.sid, s.front_apps, s.tw_version, s.turn, s.screen = "s1", None, 15, 0, (430.0, 932.0)
    s.execution_guard, s.before_input, s.input_point = None, None, None
    s.fast_tap, s.app_names = True, {}
    s._gate_at = None

    async def ensure():
        return None
    s.ensure = ensure
    return s


NOTHING = DeviceError(f"unsupported operation: {session_module.HIT_NOTHING}")


def busy(t_clock: float, t_point: float, hold: float = 2.5):
    """A busy app: the clock answers the status bar in `t_clock`; the point "no element answered" in `t_point` and the
    runner is then held `hold` s, every question in it "no element answered" after the runner's wait."""
    def script(n, path, dev):
        if time.monotonic() < dev.held_until:
            return t_point, NOTHING
        if "x=66.00&y=22.00" in path:
            return t_clock, status_bar()
        dev.held_until = time.monotonic() + t_point + hold
        return t_point, NOTHING
    return script


# ---- RL-H1: the phone's own numbers (the reviewer's probe_measured_margin) ----

@pytest.mark.parametrize("t_clock", [0.026, 0.039, 0.060])
@pytest.mark.parametrize("t_point", [0.304, 0.342])
def test_rl_h1_a_busy_app_at_the_phones_measured_times_is_clear_every_time(t_clock, t_point):
    async def go():
        s = _session(busy(t_clock, t_point))
        assert await s.banner_at((383.0, 168.0), SHOP[1], hands=True) == session_module.BANNER_CLEAR
        assert s._held() and len(s.device.asked) == 3
    run(go)


@pytest.mark.parametrize("t_clock", [0.08, 0.12])
def test_rl_h1_a_clock_slower_than_usual_never_leaves_the_runner_held_without_a_hold(t_clock):
    # (round L read this as a banner in motion with no hold: the front route after it went into the held runner)
    async def go():
        s = _session(busy(t_clock, 0.31))
        said = await s.banner_at((383.0, 168.0), SHOP[1], hands=True)
        assert said in (session_module.BANNER_CLEAR, session_module.BANNER_OVER) and s._held()
        await s.front_app()
        assert not s.device.front_into_held  # (the front route waited for the runner)
    run(go)


# ---- RL-H1 end to end: the reviewer's probe_slow_witness_e2e ----

@pytest.mark.parametrize("wake,extra", [(0.12, 0.0), (0.1, 0.06)])
def test_rl_h1_the_front_question_after_the_hands_gate_never_goes_into_the_held_runner(monkeypatch, wake, extra):
    monkeypatch.setattr(session_module, "BANNER_WAIT", 1.2)
    monkeypatch.setattr(session_module, "BANNER_POLL", 0.05)
    monkeypatch.setattr(session_module, "HIT_HELD", 3.0)

    async def go():
        woke = {"done": False}

        def script(n, path, dev):
            if time.monotonic() < dev.held_until:
                return 0.31, NOTHING
            if "x=66.00&y=22.00" in path:
                first = not woke["done"]
                woke["done"] = True
                return (wake if first else 0.02), status_bar()
            woke["target"] = True  # (CodeRabbit, PR #30: the touch's point was asked, and held the runner)
            dev.held_until = time.monotonic() + 0.31 + extra + 2.5
            return 0.31 + extra, NOTHING
        s = _session(script)
        s.touch_rules = TouchRules(Front(session_module.FRONT_APP, SHOP[1]))
        s._banner_seen, s._banner_until, s._banner_real, s._banner_moving, s._wait_clear = False, None, False, False, None
        s._gate_at = (383.0, 168.0)
        try:
            await s._front_rules((383.0, 168.0), wait=False)
        except _BannerUp:
            try:
                await s._wait_banner()
            except Refused:
                pass
        except Refused:
            pass
        assert woke.get("target") and s.device.held_until > 0.0  # (the point's question set a hold on the runner)
        assert s._held() or s.device.held_until <= time.monotonic()
        await s.front_app()  # (the agent's next look)
        assert not s.device.front_into_held
    run(go)


# ---- the lead's precision: a status bar that answered late lets the point decide ----

def test_a_late_status_bar_and_the_app_at_the_point_is_clear_with_two_questions():
    async def go():
        s = _session(lambda n, path, dev: (0.16, status_bar()) if "x=66.00" in path else (0.02, APP_ELEMENT))
        assert await s.banner_at((383.0, 168.0), SHOP[1], hands=True) == session_module.BANNER_CLEAR
        assert len(s.device.asked) == 2 and not s._held()
    run(go)


def test_a_late_status_bar_and_a_slow_nothing_at_the_point_is_slow_with_the_hold_and_no_confirm():
    async def go():
        s = _session(lambda n, path, dev: (0.16, status_bar()) if "x=66.00" in path else (0.31, NOTHING))
        assert await s.banner_at((383.0, 168.0), SHOP[1], hands=True) == session_module.BANNER_SLOW
        assert len(s.device.asked) == 2 and s._held()
    run(go)


# ---- RL-H2: the known limit, documented ----

def test_rl_h2_known_limit_a_banner_in_motion_with_two_late_answers_after_a_prompt_one_reads_as_clear():
    """KNOWN LIMIT (accepted for 2.0.3 by the lead, review RL H2; the runner timing its own lookup in 2.0.4 closes it).
    While a banner comes or goes the clock too says a fast "no element answered" (BRIEF-K F2). If the clock's first
    answer is prompt and the link (or the event loop) then delays the next two answers by HIT_NOTHING_MARGIN or more, in
    that half second, AND SpringBoard says a fast "nothing" at a point below the status bar (not seen on the phone in
    about a dozen comings and goings at 20 points), the three questions read as a busy app: clear. This test pins that
    the limit is where the docs say it is, not that it is wanted."""
    async def go():
        def script(n, path, dev):
            if n == 1:
                return 0.0, status_bar()  # (prompt: the banner not moving yet)
            return 0.26, NOTHING  # (SpringBoard's fast "nothing", 0.26 s late on the link)
        s = _session(script)
        assert await s.banner_at((215.0, 80.0), SHOP[1], hands=True) == session_module.BANNER_CLEAR
    run(go)


# ---- RL-L5: the hold on every outcome that isn't clear, after a "nothing" of HIT_NOTHING_SLOW or more ----

def _hold_outcome(case):
    if case == "point_over":  # the point's slow "nothing" not slow enough against a late clock: over
        return dict(script=lambda n, p, d: (0.12, status_bar()) if "x=66.00" in p else (0.26, NOTHING),
                    hands=True, at=(383.0, 168.0), app=SHOP[1], want=session_module.BANNER_OVER)
    if case == "runner_path":
        return dict(script=busy(0.02, 0.31), hands=False, at=(383.0, 168.0), app=SHOP[1],
                    want=session_module.BANNER_SLOW)
    if case == "no_witness":  # the clock answered the app's own element: no status bar there
        return dict(script=lambda n, p, d: (0.02, APP_ELEMENT) if "x=66.00" in p else (0.31, NOTHING),
                    hands=True, at=(383.0, 168.0), app=SHOP[1], want=session_module.BANNER_SLOW)
    if case.startswith("confirm_"):
        confirm = {"confirm_answer": (0.02, status_bar()), "confirm_fast": (0.01, NOTHING),
                   "confirm_error": (0.02, DeviceError("tapwright hit: busy"))}[case]
        return dict(script=lambda n, p, d: (0.02, status_bar()) if n == 1 else (0.31, NOTHING) if n == 2 else confirm,
                    hands=True, at=(383.0, 168.0), app=SHOP[1],
                    want=session_module.BANNER_OVER if case == "confirm_answer" else session_module.BANNER_SLOW)
    if case == "witness":
        return dict(script=lambda n, p, d: (0.31, NOTHING), hands=True, at=(383.0, 168.0), app=SHOP[1],
                    want=session_module.BANNER_SLOW)
    if case == "top_edge":
        return dict(script=lambda n, p, d: (0.31, NOTHING), hands=True, at=(400.0, 2.0), app=SHOP[1],
                    want=session_module.BANNER_CLEAR)
    if case == "landscape":
        return dict(script=lambda n, p, d: (0.31, NOTHING), hands=True, at=(200.0, 85.0), app=SHOP[1],
                    want=session_module.BANNER_SLOW, turn=90)
    raise AssertionError(case)


@pytest.mark.parametrize("case", ["point_over", "runner_path", "no_witness", "confirm_answer", "confirm_fast",
                                  "confirm_error", "witness", "top_edge", "landscape"])
def test_rl_l5_every_outcome_after_a_slow_nothing_sets_the_hold(case):
    async def go():
        o = _hold_outcome(case)
        s = _session(o["script"])
        if o.get("turn"):
            s.screen, s.turn = (932.0, 430.0), o["turn"]
        assert await s.banner_at(o["at"], o["app"], hands=o["hands"]) == o["want"]
        assert s._held()
    run(go)


# ---- RL-L2: the wait for a held runner inside the touch's single deadline ----

def test_rl_l2_a_hands_touchs_wait_for_a_held_runner_ends_at_the_touchs_deadline(monkeypatch):
    monkeypatch.setattr(session_module, "BANNER_WAIT", 0.5)
    monkeypatch.setattr(session_module, "HIT_FREE_EVERY", 0.05)
    monkeypatch.setattr(session_module, "HIT_FREE_WAIT", 0.1)

    async def go():
        s = unread()
        s.by_hands, s.nothing_takes = True, 0.03
        s.touch_rules = TouchRules(Front(session_module.FRONT_APP, SHOP[1]))
        s._held_until = time.monotonic() + 30.0  # (the app never answers)
        s.hit = lambda x, y: nothing()

        async def another_caller():  # (two agents on one hung app: another hold set meanwhile)
            await asyncio.sleep(0.2)
            s._held_until = time.monotonic() + 30.0
        asyncio.create_task(another_caller())
        began = time.monotonic()
        with pytest.raises(Refused) as e:
            await s._goes("tap", 60, (200.0, 600.0))
        took = time.monotonic() - began
        assert str(e.value) == session_module.COVERED_SLOW and s.sent == []
        assert 0.45 <= took < 0.8, f"{took:.2f} s"  # (BANNER_WAIT and one question, not restarted)
    run(go)

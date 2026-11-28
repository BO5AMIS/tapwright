"""Tapwright 2.0.3, round T: review RS on rounds S and S2.

RS-H1: a hold carried while another call waited in channel_free trapped that call: its loop asked the clock back to
back with no end, inside the queue's lock. Now every wait for a held runner ends at the hold's limit (HIT_HELD from the
hold's start), raises the given-up state another call set meanwhile at its next step, and never asks twice without
HIT_FREE_MIN between; the wait for a carried hold takes the queue one question at a time.
RS-H2: a carried hold became the given-up state at once, so a hold of about 4 s gave the next call not_answering. Now
it stays a hold: the next call waits out the rest of it, and only a hold HIT_HELD old with no real answer is given up.
RS-M1: a busy long cut kept its screen's block at its first span; it doubles as for any cut long read.
RS-M2: the failed pictures of the last LONG_PICS screens are kept, so two cut screens of a readable app don't each pay a
long read at every switch.
RS-M3: only a slow "no element answered" after a silent front route is the held state.
RS-L1: left_by_call is set on the held open_app path and when recording the front fails.
RS-L4: "invalid session" (a restarted runner) is not taken for a hold."""
import asyncio
import time

import pytest
from test_banner_hit_203 import _looked
from test_daemon import finish
from test_round_k_203 import WITNESS, _phone_session, scaled  # noqa: F401 (fixture)
from test_round_n_203 import POINT, HeldSession
from test_round_s2_203 import _StalledFront
from test_touch_other_app_203 import CHAT, SHOP, _real, refused, run

from tapwright import session as session_module
from tapwright.device import ConnectionLost

HELD_WORDS = ("Shop (com.example.shop) is in front and isn't answering, so nothing on the phone can be read. close_app "
              "can still close it; then open_app it again.")


class CarriedSession(HeldSession):
    """Round N's held phone, with the carry of round S as Session's."""

    hold_standing = _real("hold_standing")
    carry_hold = _real("carry_hold")
    hold_carried = _real("hold_carried")
    hold_left = _real("hold_left")
    give_up_carried = _real("give_up_carried")


async def _carried(holds_for: float, age: float = 0.0):
    """A looked daemon whose last call left a hold standing: the app holds the runner `holds_for` more seconds; the hold
    was set `age` seconds ago (its first slow answer), and the call's end carried it."""
    s = CarriedSession(hold_for=0.0)
    d, stop, task = await _looked(s)
    d.heartbeat = 3600.0  # (the heartbeat's own questions have their own tests: a call's are counted)
    s.held_until = time.monotonic() + holds_for
    s._hold("point", POINT)
    d._carry_hold()
    assert s.hold_carried() and d.state == "live" and d.stuck is None
    s._hold_at -= age  # (as if the call that set it had ended `age` seconds ago, nothing asked since)
    s._held_until -= age
    s.hit_questions.clear()
    return s, d, stop, task


# ---- RS-H1: no wait for the runner spins or stands forever ----

def test_rs_h1_a_carry_while_another_call_waits_in_channel_free_ends_that_wait_at_the_holds_limit(scaled):
    """The reviewer's probe: a read waiting in channel_free when another call's end carries the hold."""
    async def go():
        s = _phone_session(free_at=time.monotonic() + 60.0)
        s._hold("point", (215.0, 117.0))
        held_from = time.monotonic()
        task = asyncio.create_task(s.channel_free())
        await asyncio.sleep(0.3)
        assert s.hold_standing()
        s.carry_hold()  # (what another call's _carry_hold does at its end)
        with pytest.raises(session_module.RunnerHeld):
            await asyncio.wait_for(task, 4.0)
        ended = time.monotonic() - held_from
        asks = [t for t, p in s.device.asked if p == session_module.HIT_PATH]
        # the limit plus one step and one question at most; about one question every HIT_FREE_EVERY, never more
        assert session_module.HIT_HELD - 0.02 <= ended < session_module.HIT_HELD + 0.2, ended
        assert len(asks) <= session_module.HIT_HELD / session_module.HIT_FREE_EVERY + 2, len(asks)
        assert s.runner_held is not None and not s.hold_carried()
    run(go)


def test_rs_h1_the_given_up_state_another_call_sets_meanwhile_ends_the_wait_at_the_next_step(scaled):
    async def go():
        s = _phone_session(free_at=time.monotonic() + 60.0)
        s._hold("point", (215.0, 117.0))
        task = asyncio.create_task(s.channel_free())
        await asyncio.sleep(0.3)
        s.runner_held = session_module.RunnerHeld(SHOP[1], SHOP[0])  # (another call's read gave it up)
        set_at = time.monotonic()
        with pytest.raises(session_module.RunnerHeld):
            await asyncio.wait_for(task, 2.0)
        assert time.monotonic() - set_at < session_module.HIT_FREE_EVERY + session_module.HIT_FREE_WAIT + 0.05
    run(go)


def test_rs_h1_two_clock_questions_are_never_back_to_back(scaled, monkeypatch):
    """A clock question that takes longer than the step (the runner's wait, a slow link) is followed by HIT_FREE_MIN of
    rest, not by the next question at once."""
    monkeypatch.setattr(session_module, "HIT_FREE_MIN", 0.1)

    async def go():
        s = _phone_session(free_at=time.monotonic() + 60.0)
        s.device.nothing_takes = 0.09  # (longer than the step, 0.07 s; within a question's 0.1 s)
        s._hold("point", (215.0, 117.0))
        with pytest.raises(session_module.RunnerHeld):
            await asyncio.wait_for(s.channel_free(), 4.0)
        asks = [t for t, p in s.device.asked if p == session_module.HIT_PATH]
        gaps = [b - a for a, b in zip(asks, asks[1:])]
        assert len(asks) >= 3 and min(gaps) >= 0.09 + 0.1 - 0.01, gaps
    run(go)


def test_rs_h1_a_call_waiting_out_a_carried_hold_leaves_the_queue_free_and_close_app_gets_out(scaled):
    async def go():
        s, d, stop, task = await _carried(holds_for=60.0)
        held_from = s._hold_at
        look = asyncio.create_task(refused(d, "look", {"screenshot": False}))
        await asyncio.sleep(0.2)
        assert not look.done()

        async def nothing():
            return True
        # between two clock questions the queue is free: another read (the heartbeat's) gets its turn at once
        for _ in range(3):
            assert await asyncio.wait_for(d.control.read(nothing), session_module.HIT_FREE_WAIT + 0.05)
            await asyncio.sleep(0.05)
        r = await asyncio.wait_for(d.run_tool("b", "close_app", {"app": "com.example.shop"}), 3.0)
        e = await asyncio.wait_for(look, 1.0)
        ended = time.monotonic() - held_from
        assert e.code == "not_answering" and str(e) == HELD_WORDS
        assert r["text"].startswith("Closed com.example.shop.") and s.device.kill_asks == [SHOP[1]]
        # both by the hold's limit (from its first slow answer) plus a step, a question and the close
        assert ended < session_module.HIT_HELD + 0.4, ended
        assert set(s.hit_questions) == {WITNESS} and [k for k, t in s.runner_asks if t > held_from] == []
        assert d.state == "live" and d.stuck is None  # (the close freed the runner; the clock answered)
        await finish(stop, task)
    run(go)


# ---- RS-H2: a carried hold is waited out; not_answering only once it is HIT_HELD old ----

def test_rs_h2_a_carried_hold_that_frees_at_4_s_is_waited_out_and_the_next_look_answers(scaled):
    async def go():
        s, d, stop, task = await _carried(holds_for=0.4)  # (about 4 s on the phone: a shopping app's first seconds)
        freed = s.held_until
        began = time.monotonic()
        r = await d.run_tool("a", "look", {"screenshot": False})
        took = time.monotonic() - began
        assert r["text"].startswith("Screen ") and "isn't answering" not in r["text"]
        assert d.state == "live" and d.stuck is None and getattr(s, "runner_held", None) is None
        assert not s.hold_carried() and not s._held()
        assert 0.38 <= took < 0.4 + 0.25, took
        assert [t for k, t in s.runner_asks if t > began and t < freed] == []  # (no front, no tree into the hold)
        assert "front" in [k for k, t in s.runner_asks if t >= freed]
        await finish(stop, task)
    run(go)


def test_rs_h2_a_carried_hold_that_lasts_answers_not_answering_once_it_is_hit_held_old(scaled):
    async def go():
        s, d, stop, task = await _carried(holds_for=60.0, age=0.5)  # (60 s on the phone; set 5 s before)
        attempts, sid = d._attempts, s.sid
        left = s.hold_left()
        began = time.monotonic()
        e = await refused(d, "look", {"screenshot": False})
        took = time.monotonic() - began
        assert e.code == "not_answering" and str(e) == HELD_WORDS
        # (from the hold's first slow answer, not from the look: 0.7 s here, not HIT_HELD)
        assert left - 0.02 <= took < left + 0.25, (took, left)
        assert set(s.hit_questions) == {WITNESS} and [k for k, t in s.runner_asks if t > began] == []
        assert isinstance(d.stuck, session_module.RunnerHeld) and d.state == "not-answering"
        assert d._attempts == attempts and s.sid == sid and not d._lost.is_set()
        await finish(stop, task)
    run(go)


@pytest.mark.parametrize("still_held", [True, False])
def test_rs_h2_a_call_after_the_carried_holds_limit_asks_the_clock_once_first(scaled, still_held):
    async def go():
        s, d, stop, task = await _carried(holds_for=60.0 if still_held else 0.1, age=1.5)  # (past HIT_HELD)
        began = time.monotonic()
        if still_held:
            e = await refused(d, "look", {"screenshot": False})
            assert e.code == "not_answering" and s.hit_questions == [WITNESS]
            assert time.monotonic() - began < 0.3 and isinstance(d.stuck, session_module.RunnerHeld)
        else:  # (the app answered meanwhile, unasked: the clock says so, and the look goes on)
            await asyncio.sleep(0.1)
            r = await d.run_tool("a", "look", {"screenshot": False})
            assert r["text"].startswith("Screen ") and s.hit_questions[0] == WITNESS and d.state == "live"
            assert not s.hold_carried()
        await finish(stop, task)
    run(go)


def test_rs_h2_open_app_after_a_carried_hold_is_given_up_goes_by_ioss_process_control(scaled):
    async def go():
        s, d, stop, task = await _carried(holds_for=60.0, age=1.1)
        s.frees_on_launch = {CHAT[1]}
        r = await d.run_tool("a", "open_app", {"app": "com.example.chat"})
        assert s.launched == [(CHAT[1], None)] and r["text"].startswith("Opened")
        assert d.state == "live" and d.stuck is None
        await finish(stop, task)
    run(go)


# ---- RS-M1, RS-M2: the long read's block ----

def test_rs_m1_a_busy_cut_long_read_keeps_doubling_its_screens_block():
    t = [0.0]
    b = session_module.ReadBudget(clock=lambda: t[0])
    spans = []
    for _ in range(6):
        b.long_sent("app.x")
        b.answered("app.x", True, b.long_ms, 8000, sent_for="app.x", busy=True)
        spans.append(b.block_span("app.x"))
        assert b.misses("app.x") == 1 and b.block_state("app.x") != "app"  # (still no row toward the app-wide stop)
        t[0] += 10000
    first = session_module.LONG_MEMORY
    assert spans == [min(session_module.LONG_MEMORY_MAX, first * 2 ** i) for i in range(6)], spans
    b.long_sent("app.x")
    b.answered("app.x", False, b.long_ms, 8000, sent_for="app.x")  # (a whole read resets it)
    b.long_sent("app.x")
    b.answered("app.x", True, b.long_ms, 8000, sent_for="app.x", busy=True)
    assert b.block_span("app.x") == session_module.LONG_RETRY  # (readable now: the first span again)


def test_rs_m1_a_long_read_answered_by_another_app_doesnt_grow_the_block():
    t = [0.0]
    b = session_module.ReadBudget(clock=lambda: t[0])
    b.long_sent("app.x")
    b.answered("app.x", True, b.long_ms, 8000, sent_for="app.x", busy=True)
    span = b.block_span("app.x")
    b.long_sent("app.x")
    b.answered(session_module.SPRINGBOARD, False, b.long_ms, 100, sent_for="app.x")  # (a banner's tree answered)
    assert b.block_span("app.x") == span


def test_rs_m2_two_cut_screens_of_a_readable_app_dont_each_pay_a_long_read_at_every_switch():
    t = [0.0]
    b = session_module.ReadBudget(clock=lambda: t[0])
    now = ["home"]
    b.other = lambda picture: picture != now[0]
    b.long_sent("app.x", "home")
    b.answered("app.x", False, b.long_ms, 5000, sent_for="app.x")  # (read whole once: readable)
    for screen in ("search", "grid"):
        now[0] = screen
        assert b.block_state("app.x") in ("", "other") and b.slow("app.x")  # (a new screen: a long read may go)
        b.long_sent("app.x", screen)
        b.answered("app.x", True, b.long_ms, 8000, sent_for="app.x")
        t[0] += 5
    for screen in ("search", "grid", "search"):  # (back and forth: both blocked, no long read)
        now[0] = screen
        assert b.block_state("app.x") == "same" and not b.slow("app.x") and not b.long_try("app.x")
    now[0] = "cart"
    assert b.block_state("app.x") == "other"  # (a screen not tried)
    for screen in ("cart", "profile", "orders"):  # (LONG_PICS screens newer than "search")
        now[0] = screen
        b.long_sent("app.x", screen)
        b.answered("app.x", True, b.long_ms, 8000, sent_for="app.x")
    now[0] = "search"
    assert b.block_state("app.x") == "other" and len(b._long_pics["app.x"]) == session_module.LONG_PICS
    now[0] = "grid"
    assert b.block_state("app.x") == "same"
    b.long_sent("app.x", "grid")  # (a whole read lifts every screen's block)
    b.answered("app.x", False, b.long_ms, 5000, sent_for="app.x")
    assert b.block_state("app.x") == "" and "app.x" not in b._long_pics


def test_rs_m2_each_kept_screen_blocks_for_its_own_span():
    t = [0.0]
    b = session_module.ReadBudget(clock=lambda: t[0])
    now = ["a"]
    b.other = lambda picture: picture != now[0]
    span = session_module.LONG_MEMORY
    b.long_sent("app.x", "a")
    b.answered("app.x", True, b.long_ms, 8000, sent_for="app.x")  # (at 0)
    t[0] += span * 2 / 3
    now[0] = "b"
    b.long_sent("app.x", "b")
    b.answered("app.x", True, b.long_ms, 8000, sent_for="app.x")  # (at 2/3 of a span; the row's span is 2 now)
    t[0] += span * 2 / 3
    now[0] = "a"
    assert b.block_state("app.x") == "same"  # (a's, at 4/3 of a span: within the doubled span)
    t[0] += span * 5 / 6  # (13/6 of a span: a's own block, from 0, has run out; b's, from 2/3, hasn't)
    assert b.block_state("app.x") == "other"
    now[0] = "b"
    assert b.block_state("app.x") == "same"
    t[0] += span
    assert b.block_state("app.x") == ""  # (b's, the newest, ran out too: no block)


# ---- RS-M3, RS-L4: the front route with no answer ----

@pytest.mark.parametrize("slow", [True, False])
def test_rs_m3_only_a_slow_nothing_at_the_clock_after_a_silent_front_route_is_the_held_state(slow):
    async def go():
        s = _phone_session(free_at=time.monotonic() + 60.0)
        s.device.__class__ = _StalledFront
        s.device.nothing_takes = 0.3 if slow else 0.02  # (the runner's wait for the app, or SpringBoard redrawing)
        s.app, s.app_names = ("Shop", SHOP[1]), {SHOP[1]: "Shop"}

        async def in_front():
            return [SHOP[1]]
        s.device.foreground_apps = in_front
        if slow:
            with pytest.raises(session_module.RunnerHeld):
                await s.front_app()
            assert s.runner_held is not None and s.sid == "s1"
        else:  # (a banner in motion says nothing of a hold: the silence is a dropped line, as before)
            with pytest.raises(ConnectionLost) as e:
                await s.front_app()
            assert not isinstance(e.value, session_module.RunnerHeld) and getattr(s, "runner_held", None) is None
        assert [p for _, p in s.device.asked].count(session_module.HIT_PATH) == 1
    asyncio.run(go())

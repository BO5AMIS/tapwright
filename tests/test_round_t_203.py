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
from test_round_k_203 import _phone_session, scaled  # noqa: F401 (fixture)
from test_touch_other_app_203 import SHOP, run

from tapwright import session as session_module

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
        carried = time.monotonic()
        with pytest.raises(session_module.RunnerHeld):
            await asyncio.wait_for(task, 4.0)
        ended = time.monotonic() - held_from
        asks = [t for t, p in s.device.asked if p == session_module.HIT_PATH]
        # the given-up state the carry set, at the wait's next step: well within the limit, a few questions
        assert time.monotonic() - carried < session_module.HIT_FREE_EVERY + session_module.HIT_FREE_WAIT + 0.05
        assert ended < session_module.HIT_HELD and len(asks) <= 0.5 / session_module.HIT_FREE_EVERY + 2, len(asks)
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

"""2.0, seen on the phone 2026-11-26: for one whole daemon session every touch sent through CoreDevice HID was acked in
60-110 ms and did nothing (26 touches; the buttons, another service, worked; the next start was fine). The phone acks a
report as it is written, so only the screen can say that touches land. A touch connection none of whose touches has
changed the screen yet is doubted after DOUBT_AFTER touches in a row that changed nothing: opened again once, and left
for the runner when the new one goes the same way."""
import asyncio

import pytest
from test_daemon import finish, make as make_daemon, started
from test_hands import H, W, make
from test_vision_200 import run

from tapwright import daemon as daemon_module
from tapwright.hands import DOUBT_AFTER, HandsUnavailable


async def tap(hands):
    await hands.tap(W / 2, H / 2)


def test_touches_that_change_nothing_on_a_connection_never_proven_reopen_it_once_then_leave_it():
    async def go():
        hands, wire, phone = make()
        for n in range(DOUBT_AFTER):
            await tap(hands)
            verdict = await hands.judged(False)
            assert verdict == ("reopened" if n == DOUBT_AFTER - 1 else None)
        assert wire.opened == 1 and wire.closed == 1 and any("opening CoreDevice HID again" in line for line in phone.lines)
        for n in range(DOUBT_AFTER):
            await tap(hands)  # (the first of these opens the new connection)
            verdict = await hands.judged(False)
            assert verdict == ("runner" if n == DOUBT_AFTER - 1 else None)
        assert wire.opened == 2
        with pytest.raises(HandsUnavailable) as e:  # the caller sends the touch through the runner
            await tap(hands)
        assert "acknowledged but changed nothing" in e.value.reason and wire.opened == 2
    asyncio.run(go())


def test_one_touch_that_changes_the_screen_proves_the_connection_for_good():
    async def go():
        hands, wire, _ = make()
        await tap(hands)
        assert await hands.judged(False) is None
        await tap(hands)
        assert await hands.judged(True) is None  # it lands
        for _ in range(DOUBT_AFTER * 3):  # (taps on the status bar, on a disabled row: ordinary)
            await tap(hands)
            assert await hands.judged(False) is None
        assert wire.opened == 1 and wire.closed == 0
    asyncio.run(go())


def test_a_reopened_connection_that_lands_is_kept_and_a_new_tunnel_starts_afresh():
    async def go():
        hands, wire, phone = make()
        for _ in range(DOUBT_AFTER):
            await tap(hands)
            await hands.judged(False)
        await tap(hands)
        assert await hands.judged(True) is None and wire.opened == 2
        for _ in range(DOUBT_AFTER + 1):
            await tap(hands)
            assert await hands.judged(False) is None  # proven: never doubted again
        phone.rsd = type(phone.rsd)()  # the phone reconnected: another tunnel, another connection, its own doubt
        for n in range(DOUBT_AFTER):
            await tap(hands)
            assert await hands.judged(False) == ("reopened" if n == DOUBT_AFTER - 1 else None)
    asyncio.run(go())


def test_nothing_is_judged_without_an_open_connection():
    async def go():
        hands, wire, _ = make()
        assert await hands.judged(False) is None and wire.opened == 0
    asyncio.run(go())


def test_the_answer_says_when_the_touch_connection_was_opened_again_and_when_the_runner_took_over():
    """The daemon tells the hands after each touchscreen gesture they played whether the screen changed, and passes
    their doubt on as a note; a button press (another service) and a touch the runner made are never judged."""
    async def go():
        d = make_daemon()
        stop, task = await started(d)

        class Judge:
            def __init__(self):
                self.heard, self.says = [], None

            async def judged(self, changed):
                self.heard.append(changed)
                return self.says
        judge = d.session.hands = Judge()
        d.session.hands_at = None
        assert await d._judge_hands("tap", 10.0, False) is None and judge.heard == []  # the runner made this touch
        d.session.hands_at = 9.0
        assert await d._judge_hands("tap", 10.0, False) is None and judge.heard == []  # (an earlier call's touch)
        d.session.hands_at = 11.0
        assert await d._judge_hands("press_button", 10.0, False) is None and judge.heard == []
        assert await d._judge_hands("swipe", 10.0, True) is None and judge.heard == [True]
        judge.says = "reopened"
        assert await d._judge_hands("tap", 10.0, False) == daemon_module.NOTE_HANDS_REOPENED
        judge.says = "runner"
        assert await d._judge_hands("back", 10.0, False) == daemon_module.NOTE_HANDS_RUNNER

        async def broken(changed):
            raise RuntimeError("no")
        judge.judged = broken
        assert await d._judge_hands("tap", 10.0, False) is None  # a nicety: never a failed call
        await finish(stop, task)
    run(go)

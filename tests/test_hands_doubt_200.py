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
from tapwright import hands as hands_module
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


def test_a_doubt_that_waited_for_the_lock_is_checked_again_under_it():
    # (CodeRabbit, PR #26) judged() counted before it waited for the lock; meanwhile a new connection was opened (its
    # count starts again). The doubt must not close that one
    async def go():
        hands, wire, phone = make()
        for _ in range(DOUBT_AFTER - 1):
            await tap(hands)
            assert await hands.judged(False) is None
        await tap(hands)
        async with hands._lock:
            judging = asyncio.ensure_future(hands.judged(False))  # counts to DOUBT_AFTER, then waits for the lock
            await asyncio.sleep(0.01)
            hands._unchanged, hands._proven = 0, False  # a new connection was opened meanwhile
        assert await judging is None
        assert wire.closed == 0 and hands._touch is not None
    asyncio.run(go())


def test_a_touch_that_changes_the_screen_starts_the_count_again_and_a_proven_connection_is_doubted_too():
    """(2.0.2) A proven connection was never doubted. One that stops reaching the screen, or was "proven" by a screen
    that changed by itself, then answered "No visible change" for as long as the service ran (the phone, 2026-11-26)."""
    async def go():
        hands, wire, phone = make()
        await tap(hands)
        assert await hands.judged(False) is None
        await tap(hands)
        assert await hands.judged(True) is None  # it lands: the count starts again
        for n in range(DOUBT_AFTER):
            await tap(hands)
            assert await hands.judged(False) == ("reopened" if n == DOUBT_AFTER - 1 else None)
        assert wire.opened == 1 and wire.closed == 1
        assert any("earlier touches of this connection did" in line for line in phone.lines)
        await tap(hands)  # the new connection
        assert await hands.judged(True) is None and wire.opened == 2  # it lands: all is as after a first proof
        for n in range(DOUBT_AFTER):
            await tap(hands)
            assert await hands.judged(False) == ("reopened" if n == DOUBT_AFTER - 1 else None)  # never "runner" yet
    asyncio.run(go())


def test_only_a_touch_aimed_at_an_element_proves_or_doubts_a_connection():
    """(2.0.2) A scroll or a swipe after which the screen changed is no proof: the screen may have changed by itself."""
    async def go():
        hands, wire, _ = make()
        await tap(hands)
        assert await hands.judged(True, meant=False) is None and not hands._proven
        for _ in range(DOUBT_AFTER * 2):
            assert await hands.judged(False, meant=False) is None  # nor is one that changed nothing held against it
        assert hands._unchanged == 0 and wire.closed == 0
        for n in range(DOUBT_AFTER):
            await tap(hands)
            if n == 1:
                assert await hands.judged(True, meant=False) is None  # a list moved meanwhile: the count goes on
            assert await hands.judged(False) == ("reopened" if n == DOUBT_AFTER - 1 else None)
    asyncio.run(go())


def test_the_runner_takes_over_for_a_time_and_the_hands_are_tried_again_after_it(monkeypatch):
    """(2.0.2) Was for the life of the tunnel: on an always-on service, for good."""
    async def go():
        hands, wire, phone = make()
        for _ in range(DOUBT_AFTER):
            await tap(hands)
            await hands.judged(False)
        for n in range(DOUBT_AFTER):
            await tap(hands)
            verdict = await hands.judged(False)
        assert verdict == "runner" and wire.opened == 2
        with pytest.raises(HandsUnavailable):
            await tap(hands)
        loop = asyncio.get_running_loop()
        first = hands._failed[2] - loop.time()
        assert hands_module.RUNNER_FOR - 5 < first <= hands_module.RUNNER_FOR
        hands._failed = (hands._failed[0], hands._failed[1], loop.time() - 1.0)  # the time is up
        await tap(hands)
        assert wire.opened == 3 and hands._touch is not None
        # that connection has one touch to land: it changes nothing, and the runner has the touches twice as long
        assert await hands.judged(False) == "runner" and wire.closed == 3
        second = hands._failed[2] - loop.time()
        assert 2 * hands_module.RUNNER_FOR - 5 < second <= 2 * hands_module.RUNNER_FOR
        assert sum("touches go through the runner" in line for line in phone.lines) == 2
        hands._failed = (hands._failed[0], hands._failed[1], loop.time() - 1.0)
        await tap(hands)
        assert await hands.judged(True) is None and hands._spells == 0  # it lands: all is as after a first proof
        for n in range(DOUBT_AFTER):
            await tap(hands)
            assert await hands.judged(False) == ("reopened" if n == DOUBT_AFTER - 1 else None)
    asyncio.run(go())


def test_a_reopened_connection_that_lands_is_kept_and_a_new_tunnel_starts_afresh():
    async def go():
        hands, wire, phone = make()
        for _ in range(DOUBT_AFTER):
            await tap(hands)
            await hands.judged(False)
        await tap(hands)
        assert await hands.judged(True) is None and wire.opened == 2
        for _ in range(DOUBT_AFTER - 1):
            await tap(hands)
            assert await hands.judged(False) is None  # (fewer than DOUBT_AFTER: ordinary)
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
                return self.says if not changed else None
        judge = d.session.hands = Judge()
        row = daemon_module.ui.Target(label="General")
        d.session.hands_at = None
        assert await d._judge_hands("tap", 10.0, False, row) is None and judge.heard == []  # the runner made this touch
        d.session.hands_at = 9.0
        assert await d._judge_hands("tap", 10.0, False, row) is None and judge.heard == []  # (an earlier call's touch)
        d.session.hands_at = 11.0
        assert await d._judge_hands("press_button", 10.0, False) is None and judge.heard == []
        # (review L-L3) what changes nothing by right is never held against the hands: a scroll at a list's end, a
        # `back` with nothing to go back to, a tap at a point the agent chose itself
        for name, target in (("scroll", None), ("back", None), ("swipe", None),
                             ("tap", daemon_module.ui.Target(x=215.0, y=12.0))):
            assert await d._judge_hands(name, 10.0, False, target) is None and judge.heard == [], name
        # (2.0.2) nor does a change after a scroll or a swipe prove them: the screen may have changed by itself
        assert await d._judge_hands("swipe", 10.0, True) is None and judge.heard == []
        assert await d._judge_hands("scroll", 10.0, True) is None and judge.heard == []
        assert await d._judge_hands("tap", 10.0, True, row) is None and judge.heard == [True]  # an element's tap does
        judge.heard.clear()
        judge.says = "reopened"
        assert await d._judge_hands("tap", 10.0, False, row) == daemon_module.NOTE_HANDS_REOPENED
        assert judge.heard == [False]
        judge.says = "runner"
        assert await d._judge_hands("toggle", 10.0, False, row) == daemon_module.NOTE_HANDS_RUNNER

        async def broken(changed):
            raise RuntimeError("no")
        judge.judged = broken
        assert await d._judge_hands("tap", 10.0, False, row) is None  # a nicety: never a failed call
        await finish(stop, task)
    run(go)

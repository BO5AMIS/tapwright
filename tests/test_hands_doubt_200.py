"""2.0, seen on the phone 2026-11-26: for one whole daemon session every touch sent through CoreDevice HID was acked in
60-110 ms and did nothing (26 touches; the buttons, another service, worked; the next start was fine). The phone acks a
report as it is written, so only the screen can say that touches land. A touch connection whose touches aimed at
something change nothing DOUBT_AFTER times in a row is doubted: opened again, and left to the runner for a time when
the new one goes the same way.

2.0.2, seen the same day at 12:41: the doubt never fired. A gesture on a screen that changed by itself had proven the
connection, and a proven connection was never doubted."""
import asyncio

import pytest
from test_daemon import finish, make as make_daemon, started
from test_hands import H, W, make
from test_vision_200 import run

from tapwright import daemon as daemon_module
from tapwright import hands as hands_module
from tapwright.daemon import Settled
from tapwright.hands import DOUBT_AFTER, HandsUnavailable

LANDED = Settled(0.4, True, False, True)  # the frames changed, then stood still
UNCHANGED = Settled(0.8, False, False, True)  # the frames stood unchanged
CAPPED = Settled(2.0, True, True)  # the cap ended the wait with frames still changing
STALLED = Settled(1.0, True, False)  # a stall, or no frames: "changed" for want of an answer


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


def test_a_touch_that_lands_starts_the_count_again_and_a_proven_connection_is_doubted_too():
    """(2.0.2) A proven connection was never doubted. One that stops reaching the screen, or was "proven" by a screen
    that changed by itself, then answered "No visible change" for as long as the service ran (the phone, 2026-11-26)."""
    async def go():
        hands, wire, phone = make()
        await tap(hands)
        assert await hands.judged(False, "a") is None
        await tap(hands)
        assert await hands.judged(True, "b") is None  # it lands: the count starts again
        for n in range(DOUBT_AFTER):
            await tap(hands)
            assert await hands.judged(False, f"c{n}") == ("reopened" if n == DOUBT_AFTER - 1 else None)
        assert wire.opened == 1 and wire.closed == 1
        assert any("earlier touches of this connection did" in line for line in phone.lines)
        await tap(hands)  # the new connection
        assert await hands.judged(True, "f") is None and wire.opened == 2  # it lands: all is as after a first proof
        for n in range(DOUBT_AFTER):
            await tap(hands)
            assert await hands.judged(False, f"g{n}") == ("reopened" if n == DOUBT_AFTER - 1 else None)  # never "runner" yet
    asyncio.run(go())


def test_on_a_connection_that_landed_the_same_dead_button_pressed_again_and_again_counts_once():
    """(review R202 M2) A Refresh that shows nothing new, pressed again and again, is no sign of a connection that
    died. On a connection that never landed a touch every such touch counts: there the agent's retries are the
    evidence."""
    async def go():
        hands, wire, _ = make()
        await tap(hands)
        assert await hands.judged(True, "row") is None
        for _ in range(DOUBT_AFTER * 3):
            await tap(hands)
            assert await hands.judged(False, "refresh") is None
        assert hands._unchanged == 1 and wire.closed == 0
        await tap(hands)
        assert await hands.judged(False, "other") is None and hands._unchanged == 2
        hands2, _, _ = make()
        for n in range(DOUBT_AFTER):
            await tap(hands2)
            assert await hands2.judged(False, "refresh") == ("reopened" if n == DOUBT_AFTER - 1 else None)
    asyncio.run(go())


def test_the_runner_takes_over_for_a_time_and_the_hands_are_tried_again_after_it():
    """(2.0.2) Was for the life of the tunnel: on an always-on service, for good."""
    async def go():
        hands, wire, phone = make()
        verdict = None
        for _ in range(DOUBT_AFTER * 2):
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
        # no touch of this tunnel ever landed: that connection has one touch to land. It changes nothing, and the
        # runner has the touches twice as long
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


def test_a_tunnel_that_landed_touches_gets_the_full_count_after_a_time_on_the_runner_and_the_time_has_an_end():
    """(review R202 M2, L4, L7) Six taps that change nothing after one that landed end on the runner, and the hands
    come back with three touches to land, not one. The times on the runner double up to RUNNER_MAX and no further."""
    async def go():
        hands, wire, _ = make()
        await tap(hands)
        assert await hands.judged(True, "row") is None
        verdict = None
        for n in range(DOUBT_AFTER * 2):
            await tap(hands)
            verdict = await hands.judged(False, f"k{n}")
        assert verdict == "runner" and hands._landed
        loop = asyncio.get_running_loop()
        hands._failed = (hands._failed[0], hands._failed[1], loop.time() - 1.0)
        for n in range(DOUBT_AFTER):
            await tap(hands)
            assert await hands.judged(False, f"m{n}") == ("runner" if n == DOUBT_AFTER - 1 else None)
        hands._spells = 5000  # (far past where the doubling would overflow a float)
        hands._failed = (hands._failed[0], hands._failed[1], loop.time() - 1.0)
        for n in range(DOUBT_AFTER):
            await tap(hands)
            verdict = await hands.judged(False, f"z{n}")
        assert verdict == "runner"
        assert hands_module.RUNNER_MAX - 5 < hands._failed[2] - loop.time() <= hands_module.RUNNER_MAX
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


def test_a_new_tunnel_during_a_time_on_the_runner_starts_afresh():
    """(review R202 L7) The phone reconnected while the runner had the touches: the hands are used at once, with a
    first doubt's count and a first time on the runner."""
    async def go():
        hands, wire, phone = make()
        for _ in range(DOUBT_AFTER * 2):
            await tap(hands)
            await hands.judged(False)
        with pytest.raises(HandsUnavailable):
            await tap(hands)
        phone.rsd = type(phone.rsd)()
        await tap(hands)
        assert hands._touch is not None and hands._spells == 0 and not hands._landed
        for n in range(DOUBT_AFTER):
            if n:
                await tap(hands)
            assert await hands.judged(False) == ("reopened" if n == DOUBT_AFTER - 1 else None)
    asyncio.run(go())


def test_nothing_is_judged_without_an_open_connection():
    async def go():
        hands, wire, _ = make()
        assert await hands.judged(False) is None and wire.opened == 0
    asyncio.run(go())


class Judge:
    def __init__(self):
        self.heard, self.says = [], None

    async def judged(self, changed, key=None):
        self.heard.append((changed, key))
        return self.says if not changed else None


def test_the_answer_says_when_the_touch_connection_was_opened_again_and_when_the_runner_took_over():
    """The daemon tells the hands after each touch they played that should change the screen whether it did, and
    passes their doubt on as a note; a button press (another service) and a touch the runner made are never judged."""
    async def go():
        d = make_daemon()
        stop, task = await started(d)
        judge = d.session.hands = Judge()
        row = daemon_module.ui.Target(label="General")
        d.session.hands_at = None
        assert await d._judge_hands("tap", 10.0, UNCHANGED, row) is None and judge.heard == []  # the runner made this touch
        d.session.hands_at = 9.0
        assert await d._judge_hands("tap", 10.0, UNCHANGED, row) is None and judge.heard == []  # (an earlier call's touch)
        d.session.hands_at = 11.0
        assert await d._judge_hands("press_button", 10.0, UNCHANGED) is None and judge.heard == []
        # (review L-L3) what changes nothing by right is never held against the hands: a scroll at a list's end, a
        # `back` with nothing to go back to, a tap at a point the agent chose itself
        for name, target in (("scroll", None), ("back", None), ("swipe", None),
                             ("tap", daemon_module.ui.Target(x=215.0, y=12.0))):
            assert await d._judge_hands(name, 10.0, UNCHANGED, target) is None and judge.heard == [], name
        # (2.0.2) nor does a change after a scroll or a swipe prove them: the screen may have changed by itself
        for name in ("swipe", "scroll", "back"):
            assert await d._judge_hands(name, 10.0, LANDED) is None and judge.heard == [], name
        assert await d._judge_hands("tap", 10.0, LANDED, row) is None  # an element's tap that landed does
        assert judge.heard == [(True, ("tap", "label", "General", None))]
        judge.heard.clear()
        judge.says = "reopened"
        assert await d._judge_hands("tap", 10.0, UNCHANGED, row) == daemon_module.NOTE_HANDS_REOPENED
        assert judge.heard == [(False, ("tap", "label", "General", None))]
        judge.says = "runner"
        assert await d._judge_hands("long_press", 10.0, UNCHANGED, row) == daemon_module.NOTE_HANDS_RUNNER

        async def broken(changed, key=None):
            raise RuntimeError("no")
        judge.judged = broken
        assert await d._judge_hands("tap", 10.0, UNCHANGED, row) is None  # a nicety: never a failed call
        await finish(stop, task)
    run(go)


def test_only_what_the_screen_showed_for_certain_counts_either_way():
    """(review R202 H1, M1) A wait that ended on its cap, on a stall or with no frames says "changed" for want of an
    answer: on a screen that never stands still it proved a connection that reaches nothing with every tap. A stalled
    AirPlay mirror repeats its last picture: "unchanged" from it is no answer. A switch is read from the tree."""
    async def go():
        d = make_daemon()
        stop, task = await started(d)
        judge = d.session.hands = Judge()
        d.session.hands_at = 11.0
        row = daemon_module.ui.Target(id="row")
        for done in (CAPPED, STALLED):
            assert await d._judge_hands("tap", 10.0, done, row) is None and judge.heard == [], done
        assert await d._judge_hands("toggle", 10.0, UNCHANGED, row) is None and judge.heard == []
        assert await d._judge_hands("toggle", 10.0, LANDED, row) is None
        assert judge.heard == [(True, ("toggle", "id", "row", None))]
        judge.heard.clear()
        d._fed_by_video = lambda: True  # the AirPlay video is the picture's source
        assert await d._judge_hands("tap", 10.0, UNCHANGED, row) is None and judge.heard == []
        assert await d._judge_hands("tap", 10.0, LANDED, row) is None and len(judge.heard) == 1  # (a change is a change)
        await finish(stop, task)
    run(go)


def test_a_tap_at_a_text_of_the_picture_is_judged_like_one_at_an_element():
    """(review R202 M3) An o ref and a {text} found in the picture become points: a screen with no elements (a game, a
    canvas) had no touch to judge its connection by."""
    async def go():
        d = make_daemon()
        stop, task = await started(d)
        judge = d.session.hands = Judge()
        d.session.hands_at = 11.0
        point = daemon_module.ui.Target(x=127.6, y=887.4)
        assert await d._judge_hands("tap", 10.0, UNCHANGED, point) is None and judge.heard == []  # the agent's own point
        token = daemon_module._PICTURE_AIM.set(7)
        try:
            assert await d._judge_hands("tap", 10.0, UNCHANGED, point) is None
        finally:
            daemon_module._PICTURE_AIM.reset(token)
        assert judge.heard == [(False, ("tap", "point", 128, 887))]
        await finish(stop, task)
    run(go)

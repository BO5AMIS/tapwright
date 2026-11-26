"""Review R200 (hands): a stale turn, a cancelled gesture, a finger that may still be down. Each fails on a9b4893."""
import asyncio

import pytest

from test_hands import W, H, FakeRsd, make, touches
from test_session import Recorder

from tapwright import hands as hands_module, session as session_module
from tapwright.device import ConnectionLost, DeviceError, Unanswered
from tapwright.hands import HandsError, HandsUnavailable
from tapwright.session import TURN_UNKNOWN, Session


@pytest.fixture
def phone(monkeypatch):
    monkeypatch.setattr(session_module.store, "load_state", lambda: {"runner_id": "runner"})
    recorder = Recorder()
    recorder.locked = False
    return recorder


# ---- H1: the turn ----

def test_h1_a_reconnect_into_portrait_forgets_a_landscape_turn(phone):
    s = Session(phone)
    s.turn = 90  # the phone was in landscape when the line dropped
    asyncio.run(s.ensure())
    assert s.screen == (430.0, 932.0) and s.turn == 0


def test_h1_an_upright_read_always_sets_turn_0(phone):
    s = Session(phone)
    asyncio.run(s.ensure())
    s.turn = 270
    asyncio.run(s._follow_orientation('<XCUIElementTypeApplication type="XCUIElementTypeApplication" x="0" y="0" '
                                      'width="430" height="932"/>'))
    assert s.turn == 0


def test_h1_a_failed_rotation_read_is_unknown_not_a_guess(phone):
    s = Session(phone)
    asyncio.run(s.ensure())
    phone.fail["/rotation"] = DeviceError("WebDriverAgent: unknown error")
    asyncio.run(s._follow_orientation('<XCUIElementTypeApplication type="XCUIElementTypeApplication" x="0" y="0" '
                                      'width="932" height="430"/>'))
    assert s.screen == (932.0, 430.0) and s.turn == TURN_UNKNOWN


def test_h1_rotate_leaves_the_side_unknown_until_the_next_read(phone):
    s = Session(phone)
    asyncio.run(s.ensure())
    s.turn = 90
    asyncio.run(s.rotate("landscape_left"))
    assert s.turn == TURN_UNKNOWN


@pytest.mark.parametrize("screen,turn", [((W, H), 90), ((W, H), 270), ((H, W), TURN_UNKNOWN), ((H, W), 0)])
def test_h1_hands_refuse_a_turn_that_doesnt_fit_the_screen(screen, turn):
    hands, wire, _ = make()
    with pytest.raises(HandsUnavailable):
        asyncio.run(hands.tap(100, 800, 20, screen=screen, turn=turn))
    assert wire.sent == []


# ---- M1: a cancelled gesture never plays ----

def test_m1_a_gesture_cancelled_while_an_earlier_release_goes_out_sends_nothing():
    async def go():
        hands, wire, _ = make()
        wire.hang.add(1)  # A's release (sent when A is cancelled mid-hold) never gets through
        a = asyncio.create_task(hands.tap(10, 10, 500))
        await asyncio.sleep(0.05)
        a.cancel()  # A lifts its finger in `finally`, shielded...
        await asyncio.sleep(0.05)
        a.cancel()  # ...and a second cancel leaves that release still going out
        with pytest.raises(asyncio.CancelledError):
            await a
        sent = len(wire.sent)
        b = asyncio.create_task(hands.tap(200, 200, 5))
        await asyncio.sleep(0.05)  # B waits for A's release
        b.cancel()
        with pytest.raises(asyncio.CancelledError):
            await b
        assert len(wire.sent) == sent  # nothing of B reached the phone
    asyncio.run(go())


# ---- M2: while something may be down, no fallback to the runner ----

def test_m2_while_a_finger_may_be_down_an_unreachable_hid_is_no_fallback(monkeypatch):
    async def go():
        hands, wire, _ = make()
        wire.fail[1] = ConnectionResetError("reset")  # the release fails: the finger may still be down
        with pytest.raises(Unanswered):
            await hands.tap(10, 10, 5)
        assert hands.dirty
        wire.connect_error = OSError("no HID")
        with pytest.raises(HandsError) as e:  # not HandsUnavailable: the runner must not touch as a second finger
            await hands.tap(20, 20, 5)
        assert "may still be down" in str(e.value) and "nothing was touched" in str(e.value)
        # after the hold the phone is taken to have dropped it: the fallback is allowed again
        hands._dirty_at -= hands_module.DIRTY_HOLD + 1
        hands._failed = None
        with pytest.raises(HandsUnavailable):
            await hands.tap(20, 20, 5)
        assert hands.dirty == {}
    asyncio.run(go())


@pytest.mark.parametrize("screen,turn", [((H, W), TURN_UNKNOWN), ((W, H), 90)])
def test_m2_while_a_finger_may_be_down_a_turn_the_hands_cant_map_is_no_fallback_either(screen, turn):
    # (CodeRabbit, PR #26) _portrait refused before _ready's hold was looked at: the session took that for "use the
    # runner", which then touched as a second finger
    async def go():
        hands, wire, _ = make()
        wire.fail[1] = ConnectionResetError("reset")
        with pytest.raises(Unanswered):
            await hands.tap(10, 10, 5)
        assert hands.dirty
        sent = len(wire.sent)
        with pytest.raises(HandsError) as e:
            await hands.tap(20, 20, 5, screen=screen, turn=turn)
        assert not isinstance(e.value, HandsUnavailable) and "may still be down" in str(e.value)
        assert len(wire.sent) == sent
        hands._dirty_at -= hands_module.DIRTY_HOLD + 1  # after the hold: the fallback is allowed again
        with pytest.raises(HandsUnavailable):
            await hands.tap(20, 20, 5, screen=screen, turn=turn)
    asyncio.run(go())


def test_m2_the_release_goes_out_first_when_hid_is_back():
    async def go():
        hands, wire, _ = make()
        wire.fail[1] = ConnectionResetError("reset")
        with pytest.raises(Unanswered):
            await hands.tap(10, 10, 5)
        before = len(touches(wire))
        await hands.tap(200, 300, 5)
        first = touches(wire)[before]
        assert all(not c[3] for c in first)  # a lift, before the new tap's contact
        assert hands.dirty == {}
    asyncio.run(go())


def test_m2_a_gesture_cancelled_while_its_open_lets_go_leaves_the_release_owed():
    # (CodeRabbit, PR #26) the open published its connection, then was cancelled while it let go of the old finger:
    # the next gesture took that connection as ready and put a new finger down with the old one still down
    async def go():
        hands, wire, _ = make()
        wire.fail[1] = ConnectionResetError("reset")
        with pytest.raises(Unanswered):
            await hands.tap(10, 10, 5)
        wire.hang.add(2)  # the release, first thing on the new connection, doesn't get through
        b = asyncio.create_task(hands.tap(200, 300, 5))
        await asyncio.sleep(0.05)
        b.cancel()
        with pytest.raises(asyncio.CancelledError):
            await b
        assert hands.dirty
        before = len(touches(wire))
        await hands.tap(200, 300, 5)
        assert all(not c[3] for c in touches(wire)[before])  # a lift first, then the new tap's contact
        assert hands.dirty == {}
    asyncio.run(go())


# ---- L4: a stream that starts later ----

def test_l4_setting_stream_ok_lets_ios_27_try_again_on_the_same_tunnel():
    async def go():
        hands, wire, _ = make(FakeRsd(version="27.2"))
        with pytest.raises(HandsUnavailable):
            await hands.tap(10, 10, 5)
        hands.stream_ok = True
        await hands.tap(10, 10, 5)
        assert len(touches(wire)) == 2
    asyncio.run(go())


def test_a_dropped_connection_error_type_is_never_connection_lost():
    hands, wire, _ = make()
    wire.fail[0] = ConnectionResetError("reset")
    with pytest.raises(Unanswered) as e:
        asyncio.run(hands.tap(10, 10, 20))
    assert not isinstance(e.value, ConnectionLost)

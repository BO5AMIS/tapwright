"""Hands (2.0): touches, buttons and keys through CoreDevice HID, against fake services that record every report."""
import asyncio
import random

import pytest

from tapwright import hands as hands_module
from tapwright import session as session_module
from tapwright.control import TAP_MS, Refused
from tapwright.device import ConnectionLost, DeviceError, Unanswered
from tapwright.hands import (INDIGO_SERVICE, TOUCHSCREEN, UNIVERSAL_SERVICE, Hands, HandsError, HandsUnavailable,
                             Untypable, multi_frames, timeline, to_hid)
from tapwright.session import Session

W, H = 430.0, 932.0  # the test phone's portrait screen, in points
CONTACT, RELEASE = True, False


class FakeRsd:
    def __init__(self, version="26.5", services=(UNIVERSAL_SERVICE, INDIGO_SERVICE)):
        self.product_version = version
        self.peer_info = {"Services": {name: {"Port": "1"} for name in services}}


class Wire:
    """What reached the phone, in order: ("touch", [(identity, x, y, touching), ...]), ("key", (usages)), ("button",
    usage, state). `fail` maps a report's number (0-based, counted over every send) to the exception it raises;
    `hang` holds that report until cancelled."""

    def __init__(self):
        self.sent: list[tuple] = []
        self.times: list[float] = []
        self.count = 0
        self.fail: dict[int, BaseException] = {}
        self.hang: set[int] = set()
        self.opened = 0
        self.closed = 0
        self.listed = [{"_ServiceID": TOUCHSCREEN}, {"_ServiceID": 512}, {"_ServiceID": 1281}]
        self.connect_error: BaseException | None = None

    async def report(self, entry: tuple) -> None:
        n = self.count
        self.count += 1
        if n in self.hang:
            await asyncio.Event().wait()
        if n in self.fail:
            raise self.fail.pop(n)
        self.sent.append(entry)
        self.times.append(asyncio.get_running_loop().time())


class FakeTouch:
    def __init__(self, wire: Wire):
        self.wire = wire

    async def connect(self):
        if self.wire.connect_error is not None:
            raise self.wire.connect_error
        self.wire.opened += 1

    async def close(self):
        self.wire.closed += 1

    async def list_connected_services(self):
        return {"connectedServices": list(self.wire.listed)}

    async def send_multitouch(self, contacts, service_id):
        assert service_id == TOUCHSCREEN
        await self.wire.report(("touch", [(c.identity, c.x, c.y, c.touching) for c in contacts]))

    async def send_keyboard(self, service_id, usages=()):
        await self.wire.report(("key", service_id, tuple(usages)))

    async def create_keyboard_service(self):
        return 0x100002001


class FakeButtons:
    def __init__(self, wire: Wire):
        self.wire = wire

    async def connect(self):
        pass

    async def close(self):
        pass

    async def send_button(self, page, usage, state):
        await self.wire.report(("button", page, usage, state))


class Phone:
    def __init__(self, rsd=None):
        self.rsd = rsd if rsd is not None else FakeRsd()
        self.lines: list[str] = []

    def log(self, line):
        self.lines.append(line)


def make(rsd=None, **kw) -> tuple[Hands, Wire, Phone]:
    wire = Wire()
    phone = Phone(rsd)
    hands = Hands(phone, log=phone.log, services=lambda rsd: (FakeTouch(wire), FakeButtons(wire)), **kw)
    hands.screen = (W, H)
    return hands, wire, phone


def touches(wire: Wire) -> list:
    return [e[1] for e in wire.sent if e[0] == "touch"]


# ---- coordinates ----

def test_portrait_points_map_onto_the_panel():
    assert to_hid(0, 0, (W, H)) == (0, 0)
    assert to_hid(W, H, (W, H)) == (65535, 65535)
    assert to_hid(W / 2, H / 2, (W, H)) == (32768, 32768)
    assert to_hid(-5, H + 40, (W, H)) == (0, 65535)  # off the screen: clamped to its edge


def test_landscape_left_as_measured_on_the_phone():
    """turn 270 (landscape_left, runner z = 270): px = W - uy, py = ux (Calculator's "7", 2026-11-26)."""
    ux, uy = 100.0, 300.0  # a point on the 932 x 430 landscape screen
    assert to_hid(ux, uy, (H, W), 270) == (round((W - uy) / W * 65535), round(ux / H * 65535))


def test_landscape_right_as_measured_on_the_phone():
    """turn 90 (landscape_right, runner z = 90): px = uy, py = H - ux."""
    ux, uy = 100.0, 300.0
    assert to_hid(ux, uy, (H, W), 90) == (round(uy / W * 65535), round((H - ux) / H * 65535))


def test_an_unknown_turn_is_refused():
    with pytest.raises(ValueError):
        to_hid(1, 1, (W, H), 180)


# ---- timing ----

def test_a_path_gets_a_report_at_least_every_step():
    filled = timeline([(100, 0, 0), (200, 100, 0), (230, 100, 50)])
    times = [t for t, _, _ in filled]
    assert times[0] == 0 and times[-1] == 130
    assert all(0 < b - a <= hands_module.STEP_MS for a, b in zip(times, times[1:]))
    assert filled[-1][1:] == (100, 50)


def test_multi_finger_frames_lift_every_finger_once():
    frames = multi_frames([[(0, 10, 10), (60, 0, 10)], [(0, 20, 10), (100, 40, 10)]])
    lifts = [(i, t) for t, slots in frames for i, _, _, touching in slots if not touching]
    assert sorted(i for i, _ in lifts) == [0, 1]
    # the first finger is lifted right after its last point; the second at the end
    assert dict(lifts)[0] > 60 and dict(lifts)[1] == 100
    assert frames[0][1] == [(0, 10, 10, True), (1, 20, 10, True)]


def test_tap_reports_contact_hold_release_at_one_point():
    hands, wire, _ = make()

    async def go():
        began = asyncio.get_running_loop().time()
        await hands.tap(215, 466, 60)
        return asyncio.get_running_loop().time() - began
    took = asyncio.run(go())
    x, y = to_hid(215, 466, (W, H))
    assert touches(wire) == [[(2, x, y, CONTACT)], [(2, x, y, RELEASE)]]
    assert wire.times[1] - wire.times[0] >= 0.055 and took < 0.5


def test_path_moves_through_its_points_on_time_and_lifts_at_the_last():
    hands, wire, _ = make()
    asyncio.run(hands.path([(0, 100, 800), (120, 100, 400)]))
    sent = touches(wire)
    assert sent[0] == [(2, *to_hid(100, 800, (W, H)), CONTACT)]
    assert sent[-1] == [(2, *to_hid(100, 400, (W, H)), RELEASE)]
    assert sent[-2] == [(2, *to_hid(100, 400, (W, H)), CONTACT)]
    assert len(sent) >= 12  # a report about every 12 ms over 120 ms
    assert wire.times[-1] - wire.times[0] >= 0.11  # played at its own pace, not all at once


def test_multi_sends_every_finger_in_every_report():
    hands, wire, _ = make()
    asyncio.run(hands.multi([[(0, 200, 466), (60, 150, 466)], [(0, 230, 466), (60, 280, 466)]]))
    sent = touches(wire)
    assert all(len(r) == 2 for r in sent)
    assert [c[0] for c in sent[0]] == [1, 2]  # identities 1 and 2 (0 is used last)
    assert all(not c[3] for c in sent[-1]) and all(c[3] for r in sent[:-1] for c in r)


# ---- safety ----

def test_the_finger_is_lifted_when_the_path_is_cancelled():
    hands, wire, _ = make()

    async def go():
        task = asyncio.create_task(hands.path([(0, 100, 800), (2000, 100, 100)]))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(go())
    sent = touches(wire)
    assert sent[-1][0][3] is RELEASE and all(r[0][3] for r in sent[:-1])
    assert len(sent) < 30 and not hands.dirty


def test_a_failed_send_mid_path_is_not_sent_again_and_leaves_hands_dirty():
    hands, wire, _ = make()
    wire.fail[3] = RuntimeError("dtuhidd went away")

    async def go():
        with pytest.raises(HandsError) as e:
            await hands.path([(0, 100, 800), (100, 100, 400)])
        return e.value
    error = asyncio.run(go())
    assert isinstance(error, Unanswered)  # the session's "may have been played" contract
    assert len(touches(wire)) == 3  # nothing after the failure: no second try, no release on a broken link
    # the finger as the failed report put it (it may have arrived), to be lifted first on the next open
    (left,) = hands.dirty["touch"]
    assert left.identity == 2 and left.touching is False and not hands.available
    assert (left.x, left.y) != (touches(wire)[-1][0][1], touches(wire)[-1][0][2])


def test_a_dropped_hid_connection_is_unanswered_and_never_resets_the_runner():
    hands, wire, _ = make()
    wire.fail[0] = ConnectionResetError("reset")
    with pytest.raises(Unanswered) as e:
        asyncio.run(hands.tap(10, 10, 20))
    assert not isinstance(e.value, ConnectionLost) and "may have been played" in str(e.value)


def test_a_send_that_never_acks_is_unanswered(monkeypatch):
    monkeypatch.setattr(hands_module, "SEND_WAIT", 0.05)
    hands, wire, _ = make()
    wire.hang.add(1)  # the release
    with pytest.raises(HandsError):
        asyncio.run(hands.tap(10, 10, 20))
    assert hands.dirty["touch"]


def test_a_dirty_release_goes_out_first_on_the_next_open():
    hands, wire, _ = make()
    wire.fail[1] = RuntimeError("lost the release")
    with pytest.raises(HandsError):
        asyncio.run(hands.tap(10, 10, 20))
    assert hands.dirty["touch"]
    before = len(wire.sent)
    asyncio.run(hands.tap(300, 300, 20))
    after = touches(wire)[-3:]
    assert after[0] == [(2, *to_hid(10, 10, (W, H)), RELEASE)]  # the old finger, let go first
    assert after[1][0][3] is CONTACT and after[2][0][3] is RELEASE
    assert len(wire.sent) == before + 3 and not hands.dirty and wire.opened == 2


def test_a_release_cancelled_while_it_is_sent_still_finishes():
    hands, wire, _ = make()

    async def go():
        task = asyncio.create_task(hands.tap(10, 10, 200))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await hands.tap(20, 20, 10)  # waits for the release first
    asyncio.run(go())
    states = [r[0][3] for r in touches(wire)]
    assert states == [CONTACT, RELEASE, CONTACT, RELEASE]


def test_a_refusal_before_the_first_report_sends_nothing():
    hands, wire, _ = make()

    async def no():
        raise Refused("paused", "paused")
    with pytest.raises(Refused):
        asyncio.run(hands.tap(10, 10, 20, before=no))
    assert wire.sent == []


def test_one_gesture_at_a_time():
    hands, wire, _ = make()

    async def go():
        await asyncio.gather(hands.tap(10, 10, 40), hands.tap(20, 20, 40))
    asyncio.run(go())
    states = [r[0][3] for r in touches(wire)]
    assert states == [CONTACT, RELEASE, CONTACT, RELEASE]


# ---- availability ----

@pytest.mark.parametrize("rsd, why", [
    (FakeRsd("17.5"), "older than iOS 18"),
    (FakeRsd("27.0"), "display stream"),
    (FakeRsd("26.5", services=(INDIGO_SERVICE,)), "no HID touch service"),
    (FakeRsd("beta"), "unknown"),
])
def test_phones_that_cant_take_hid_are_unavailable(rsd, why):
    hands, wire, phone = make(rsd)
    with pytest.raises(HandsUnavailable) as e:
        asyncio.run(hands.tap(10, 10, 20))
    assert why in e.value.reason and wire.sent == [] and wire.opened == 0
    assert len(phone.lines) == 1 and "hands: not used" in phone.lines[0]


def test_ios_27_works_once_a_stream_runs():
    hands, wire, _ = make(FakeRsd("27.0"))
    hands.stream_ok = True
    asyncio.run(hands.tap(10, 10, 20))
    assert len(touches(wire)) == 2


def test_no_touchscreen_listed_is_unavailable_and_closes_what_it_opened():
    hands, wire, _ = make()
    wire.listed = [{"_ServiceID": 1281}]
    with pytest.raises(HandsUnavailable):
        asyncio.run(hands.tap(10, 10, 20))
    assert wire.closed == 1 and wire.sent == []


def test_comes_up_once_and_says_so():
    hands, wire, phone = make()
    asyncio.run(hands.tap(10, 10, 20))
    asyncio.run(hands.tap(10, 10, 20))
    assert phone.lines == ["hands: CoreDevice HID (touchscreen 257)"] and wire.opened == 1 and hands.available


def test_a_rebuilt_tunnel_opens_again():
    hands, wire, phone = make()
    asyncio.run(hands.tap(10, 10, 20))
    phone.rsd = FakeRsd()  # the Device reconnected: a new rsd
    assert not hands.available
    asyncio.run(hands.tap(10, 10, 20))
    assert wire.opened == 2 and wire.closed == 1  # (the old touch connection was closed first)


def test_an_open_that_fails_is_retried_later_not_at_once(monkeypatch):
    hands, wire, _ = make()
    wire.connect_error = OSError("no route")
    with pytest.raises(HandsUnavailable):
        asyncio.run(hands.tap(10, 10, 20))
    wire.connect_error = None
    with pytest.raises(HandsUnavailable):  # within RETRY_OPEN: not tried again
        asyncio.run(hands.tap(10, 10, 20))
    monkeypatch.setattr(hands_module, "RETRY_OPEN", 0.0)
    hands._failed = None
    asyncio.run(hands.tap(10, 10, 20))
    assert len(touches(wire)) == 2


def test_landscape_can_be_left_to_the_runner(monkeypatch):
    hands, wire, _ = make()
    monkeypatch.setattr(hands_module, "LANDSCAPE", False)
    with pytest.raises(HandsUnavailable):
        asyncio.run(hands.tap(10, 10, 20, screen=(H, W), turn=90))
    monkeypatch.setattr(hands_module, "LANDSCAPE", True)
    asyncio.run(hands.tap(100, 300, 20, screen=(H, W), turn=270))
    assert touches(wire)[0] == [(2, *to_hid(100, 300, (H, W), 270), CONTACT)]


# ---- buttons and keys ----

def test_lock_is_down_then_up_on_the_consumer_page():
    hands, wire, _ = make()
    asyncio.run(hands.button("lock"))
    assert wire.sent == [("button", 0x0C, 0x30, 1), ("button", 0x0C, 0x30, 2)]
    assert wire.times[1] - wire.times[0] >= 0.045


def test_a_button_left_down_is_let_go_on_the_next_open():
    hands, wire, _ = make()
    wire.fail[1] = RuntimeError("lost")
    with pytest.raises(HandsError):
        asyncio.run(hands.button("volume_up"))
    assert hands.dirty["buttons"] == [(0x0C, 0xE9)]
    asyncio.run(hands.button("home"))
    assert wire.sent[-3:] == [("button", 0x0C, 0xE9, 2), ("button", 0x0C, 0x40, 1), ("button", 0x0C, 0x40, 2)]


def test_keys_type_with_shift_for_capitals_and_symbols():
    hands, wire, _ = make()
    asyncio.run(hands.keys("aB!"))
    keys = [e[2] for e in wire.sent]
    assert keys == [(0x04,), (), (0xE1,), (0xE1, 0x05), (0xE1,), (), (0xE1,), (0xE1, 0x1E), (0xE1,), ()]
    assert {e[1] for e in wire.sent} == {512}  # the keyboard surface the phone listed


def test_untypable_names_the_first_character_before_anything_is_sent():
    hands, wire, _ = make()
    with pytest.raises(Untypable) as e:
        asyncio.run(hands.keys("café ☃"))
    assert e.value.char == "é" and e.value.index == 3 and wire.sent == []


def test_a_key_by_name_and_a_keyboard_made_when_none_is_listed():
    hands, wire, _ = make()
    wire.listed = [{"_ServiceID": TOUCHSCREEN}]
    asyncio.run(hands.key("enter"))
    assert wire.sent == [("key", 0x100002001, (0x28,)), ("key", 0x100002001, ())]


# ---- the session ----

class Recorder:
    """test_session.py's recording device, with a CoreDevice tunnel the hands can use."""

    def __init__(self):
        self.rsd, self.tunnel, self.runner, self.via = FakeRsd(), None, None, "USB"
        self.requests: list = []
        self.lines: list[str] = []

    def log(self, line):
        self.lines.append(line)

    async def connect(self):
        self.rsd = FakeRsd()

    async def mount(self):
        pass

    async def start_wda(self, runner):
        pass

    async def close(self):
        pass

    async def http(self, method, path, payload=None, timeout=30.0):
        self.requests.append((method, path, payload))
        if path == "/session":
            return {"sessionId": "S"}
        if path.endswith("/window/size"):
            return {"value": {"width": 430, "height": 932}}
        if path.endswith("/wda/locked"):
            return {"value": False}
        if path.endswith("/wda/tw/version"):
            raise DeviceError("WebDriverAgent: unknown command: Unhandled endpoint")
        return {"value": None}


@pytest.fixture
def phone(monkeypatch):
    monkeypatch.setattr(session_module.store, "load_state", lambda: {"runner_id": "runner"})
    monkeypatch.setattr(session_module, "HANDS", "auto")
    return Recorder()


def with_hands(phone) -> tuple[Session, Wire]:
    wire = Wire()
    s = Session(phone)
    s.hands = Hands(phone, log=phone.log, services=lambda rsd: (FakeTouch(wire), FakeButtons(wire)))
    s.motion_rng = random.Random(7)
    return s, wire


def posts(phone) -> list[str]:
    return [p for m, p, _ in phone.requests if m == "POST" and p != "/session" and not p.endswith("/appium/settings")]


def test_a_tap_goes_through_the_hands_with_a_human_hold(phone):
    s, wire = with_hands(phone)
    said = asyncio.run(s.tap(session_module.ui.Target(x=215, y=466)))
    sent = touches(wire)
    assert said.startswith("Tapped") and len(sent) == 2 and posts(phone) == []
    (_, x, y, _), = sent[0]
    assert abs(x - to_hid(215, 466, (W, H))[0]) <= 1.5 / W * 65535 + 1
    assert wire.times[1] - wire.times[0] >= 0.055  # 60-110 ms, not the runner's 20


def test_a_long_press_keeps_its_hold(phone):
    s, wire = with_hands(phone)
    asyncio.run(s.tap(session_module.ui.Target(x=100, y=100), hold_ms=400))
    (_, x, y, _), = touches(wire)[0]
    assert (x, y) == to_hid(100, 100, (W, H)) and wire.times[1] - wire.times[0] >= 0.39


def test_the_runner_takes_over_when_hands_are_unavailable(phone):
    phone.rsd = FakeRsd("16.7")
    s, wire = with_hands(phone)
    asyncio.run(s.tap(session_module.ui.Target(x=10, y=10)))
    assert wire.sent == [] and posts(phone) == ["/session/S/actions"]


def test_tapwright_hands_runner_keeps_everything_on_the_runner(phone, monkeypatch):
    monkeypatch.setattr(session_module, "HANDS", "runner")
    s, wire = with_hands(phone)
    asyncio.run(s.tap(session_module.ui.Target(x=10, y=10)))
    asyncio.run(s.swipe(10, 500, 300, 500))
    assert wire.sent == [] and posts(phone) == ["/session/S/actions", "/session/S/actions"]


def test_a_tap_checking_the_app_in_front_stays_on_the_runner(phone):
    s, wire = with_hands(phone)
    asyncio.run(s.ensure())
    s.front_apps = ("com.example.lab",)
    with pytest.raises(session_module.FrontUnchecked):  # only the runner's tap checks the app in front: this one can't
        asyncio.run(s.tap(session_module.ui.Target(x=10, y=10), hold_ms=TAP_MS))
    assert wire.sent == [] and posts(phone) == []


def test_guards_run_before_the_first_report_and_the_one_use_check_is_spent(phone):
    s, wire = with_hands(phone)
    order: list = []

    async def guard():
        order.append(("guard", len(wire.sent)))

    async def validate(point):
        order.append(("validate", point, len(wire.sent)))
    s.execution_guard, s.before_input = guard, validate

    async def hook(ms):
        order.append(("hook", len(wire.sent)))
    s.touch_hook = hook
    asyncio.run(s.tap(session_module.ui.Target(x=50, y=60)))
    assert order == [("hook", 0), ("guard", 0), ("validate", (50, 60), 0)]
    assert s.before_input is None and len(wire.sent) == 2


def test_a_guard_refusal_touches_nothing_on_either_path(phone):
    s, wire = with_hands(phone)

    async def guard():
        raise Refused("busy", "a person is using the phone")
    s.execution_guard = guard
    with pytest.raises(Refused):
        asyncio.run(s.tap(session_module.ui.Target(x=50, y=60)))
    assert wire.sent == [] and posts(phone) == []


def test_a_gesture_that_may_have_been_played_is_never_retried_on_the_runner(phone):
    s, wire = with_hands(phone)
    wire.fail[1] = RuntimeError("lost the release")
    with pytest.raises(Unanswered):
        asyncio.run(s.tap(session_module.ui.Target(x=50, y=60)))
    assert posts(phone) == []


def test_a_free_swipe_is_shaped_and_a_scroll_keeps_its_rest(phone):
    s, wire = with_hands(phone)
    asyncio.run(s.swipe(380, 520, 60, 520, 200))
    swipe = touches(wire)
    ys = {c[2] for r in swipe for c in r}
    assert len(ys) > 1  # a slight arc off the straight line
    wire.sent.clear()
    asyncio.run(s.scroll(215, 700, 215, 300))
    scroll = touches(wire)
    end = to_hid(215, 300, (W, H))
    rest = [r for r in scroll if (r[0][1], r[0][2]) == end and r[0][3]]
    assert len(rest) >= session_module.ui.SCROLL_HOLD_MS // hands_module.STEP_MS  # it rests at the end, then lifts
    assert {c[1] for r in scroll for c in r} == {end[0]}  # straight: no arc on a scroll
    assert posts(phone) == []


def test_press_button_lock_and_home_through_the_hands(phone):
    s, wire = with_hands(phone)
    assert asyncio.run(s.press_button("lock")) == "pressed lock"
    asyncio.run(s.press_button("volumeDown"))
    assert [e[2:] for e in wire.sent] == [(0x30, 1), (0x30, 2), (0xEA, 1), (0xEA, 2)] and posts(phone) == []


def test_press_button_lock_without_hands_is_unsupported_and_home_falls_back(phone):
    s = Session(phone)
    with pytest.raises(Refused) as e:
        asyncio.run(s.press_button("lock"))
    assert e.value.code == "unsupported" and posts(phone) == []
    asyncio.run(s.press_button("home"))
    assert posts(phone) == ["/wda/homescreen"]


def test_pinch_spreads_two_fingers_about_the_point(phone):
    s, wire = with_hands(phone)
    said = asyncio.run(s.pinch(215, 466, 2.0, 100))
    sent = touches(wire)
    first, last_down = sent[0], sent[-2]
    assert [c[3] for c in first] == [True, True] and all(not c[3] for c in sent[-1])
    gap = lambda r: abs(r[1][1] - r[0][1]) / 65535 * W  # noqa: E731
    assert gap(first) == pytest.approx(60, abs=0.5) and gap(last_down) == pytest.approx(120, abs=0.5)
    assert {c[2] for r in sent for c in r} == {to_hid(0, 466, (W, H))[1]}
    assert "60 -> 120" in said


def test_pinch_without_hands_is_unsupported(phone):
    s = Session(phone)
    with pytest.raises(Refused) as e:
        asyncio.run(s.pinch(215, 466, 0.5))
    assert e.value.code == "unsupported"


def test_pinch_near_an_edge_spreads_less_than_asked():
    assert session_module.pinch_spread(40, 400, 4.0, (W, H)) == (30.0, 30.0)
    assert session_module.pinch_spread(215, 400, 0.5, (W, H)) == (60.0, 30.0)


def test_hands_keys_types_and_untypable_comes_first(phone):
    s, wire = with_hands(phone)
    assert asyncio.run(s.hands_keys("Hi")) is True
    assert [e[2] for e in wire.sent] == [(0xE1,), (0xE1, 0x0B), (0xE1,), (), (0x0C,), ()]
    with pytest.raises(Untypable):
        asyncio.run(s.hands_keys("ü"))


def test_a_session_without_hands_is_as_before(phone):
    s = Session(phone)
    assert s.hands is None
    asyncio.run(s.tap(session_module.ui.Target(x=10, y=10)))
    assert posts(phone) == ["/session/S/actions"]


# ---- the daemon's tools ----

def _daemon():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_daemon import finish, make as make_daemon, started
    return make_daemon, started, finish


def test_pinch_tool_reaches_the_session_and_checks_its_arguments():
    make_daemon, started, finish = _daemon()

    async def go():
        d = make_daemon()
        stop, task = await started(d)
        calls = []

        async def pinch(x, y, scale, ms):
            calls.append((x, y, scale, ms))
            return "spread two fingers"
        d.session.pinch = pinch
        r = await d.run_tool("a", "pinch", {"x": 200, "y": 400, "scale": 2, "look": False})
        assert r["text"].startswith("spread two fingers") and calls == [(200.0, 400.0, 2.0, 500)]
        await d.run_tool("a", "pinch", {"x": 200, "y": 400, "scale": 0.5, "seconds": 1.2, "look": False})
        assert calls[-1] == (200.0, 400.0, 0.5, 1200)
        for bad in ({"x": 200, "y": 400, "scale": 1}, {"x": 200, "y": 400, "scale": 9},
                    {"x": 200, "y": 400, "scale": "big"}, {"x": 200, "y": 400, "scale": 2, "seconds": 7},
                    {"x": 9000, "y": 400, "scale": 2}, {"x": 200, "scale": 2}):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "pinch", bad)
            assert e.value.code == "bad_params", bad
        assert len(calls) == 2
        await finish(stop, task)
    asyncio.run(go())


def test_pinch_tool_on_a_session_without_hands_is_unsupported():
    make_daemon, started, finish = _daemon()

    async def go():
        d = make_daemon()  # (the fake session has no pinch at all)
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "pinch", {"x": 200, "y": 400, "scale": 2})
        assert e.value.code == "unsupported"
        await finish(stop, task)
    asyncio.run(go())


def test_press_button_takes_lock_and_names_the_buttons_when_refusing():
    make_daemon, started, finish = _daemon()

    async def go():
        d = make_daemon()
        stop, task = await started(d)
        await d.run_tool("a", "press_button", {"name": "lock", "look": False})
        assert "button:lock" in d.session.actions()
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "press_button", {"name": "power"})
        assert e.value.code == "bad_params" and "lock" in str(e.value)
        await finish(stop, task)
    asyncio.run(go())


def test_lazy_listing_keeps_pinch_and_lock_out():
    from tapwright import mcp_server
    try:
        mcp_server.set_lazy_tools(True)
        tools = {t.name: t for t in asyncio.run(mcp_server.server.list_tools())}
    finally:
        mcp_server.set_lazy_tools(False)
    assert "pinch" not in tools and "lock" not in tools["press_button"].input_schema["properties"]["name"]["enum"]
    eager = {t.name: t for t in asyncio.run(mcp_server.server.list_tools())}
    assert "pinch" in eager and "lock" in eager["press_button"].input_schema["properties"]["name"]["enum"]

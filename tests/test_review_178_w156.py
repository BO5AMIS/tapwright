"""1.7.8, the last Opus review of the unlock and of Home over Notification Center (REVIEW-1.7.8-OPUS3: H1, M1, M2, L1-L9).

The owner's rule of 2026-11-18: an agent may unlock the phone with the vault's passcode; locking the phone no longer stops
an agent that has one; Pause in the live view does. All values are synthetic."""
import asyncio

import pytest
from fakes import FakeSession
from test_daemon import finish, make, run, started
from test_phone_178 import (CLEAR, DATE, HEADER, CAMERA, _cell, _cover, _flashlight, _home_daemon, _node, center_tree,
                            lock_tree)
from test_unlock import PASSCODE, Clock, Screen, unlocker
from test_unlock_daemon import SessionScreen, vault

from tapwright import daemon as daemon_module, journey, notifications, session as session_module, trace, unlock
from tapwright.actionlog import ActionLog
from tapwright.auditlog import AuditLog
from tapwright.ceiling import Ceiling
from tapwright.control import Refused
from tapwright.device import ConnectionLost, DeviceError


# ---- H1: the proof of Notification Center over an unlocked phone ----

def test_H1_a_notification_titled_notification_center_proves_nothing():
    """Fails on 883d54a: any non-button node saying "Notification Center" counted as the header, a notification's own
    title too (here on the Clear button's row)."""
    title = _node("StaticText", 78, 375, 200, 23, "NotificationTitle", "Notification Center")
    look = _node("Other", 14, 360, 300, 60, "NotificationShortLookView", "EXAMPLE APP, now, Notification Center",
                 inner=title)
    cell = _node("Cell", 14, 360, 300, 60, "ListCell", "EXAMPLE APP, now, Notification Center", inner=look)
    tree = _cover(cell, CLEAR, _cell(2, 519), DATE, _flashlight(True), CAMERA)
    assert not notifications.center_over_unlocked(tree)
    # the same words as the sheet's own header, on screen and on the row: proven (the control)
    assert notifications.center_over_unlocked(_cover(cell, HEADER, CLEAR, DATE, _flashlight(True), CAMERA))


@pytest.mark.parametrize("where", ["above", "below", "right", "no_size"])
def test_H1_a_header_off_the_screen_proves_nothing(where):
    """Fails on 883d54a: the header counted wherever it sat in the tree."""
    moved = {"above": 'y="-300"', "below": 'y="1200"', "right": 'x="600"', "no_size": 'height="0"'}[where]
    original = {"above": 'y="368"', "below": 'y="368"', "right": 'x="24"', "no_size": 'height="44"'}[where]
    header = HEADER.replace(original, moved)
    clear = CLEAR if where != "above" else CLEAR  # (the Clear button stays where it is)
    tree = center_tree().replace(HEADER, header).replace(CLEAR, clear)
    assert not notifications.center_over_unlocked(tree)


def test_H1_a_clear_button_off_the_screen_proves_nothing():
    tree = center_tree().replace(CLEAR, CLEAR.replace('y="373"', 'y="1300"'))
    assert not notifications.center_over_unlocked(tree)


def test_H1_a_clear_button_on_another_row_or_left_of_the_header_proves_nothing():
    """Fails on 883d54a: the header and the Clear button could be anywhere in the tree."""
    other_row = center_tree().replace(CLEAR, CLEAR.replace('y="373"', 'y="600"'))
    assert not notifications.center_over_unlocked(other_row)
    left = center_tree().replace(CLEAR, CLEAR.replace('x="351"', 'x="0"')).replace(
        HEADER, HEADER.replace('x="24"', 'x="120"'))
    assert not notifications.center_over_unlocked(left)
    # centres 9.5 points apart are still one row; 19.5 apart are not
    near = center_tree().replace(CLEAR, CLEAR.replace('y="373"', 'y="380"'))
    assert notifications.center_over_unlocked(near)
    apart = center_tree().replace(CLEAR, CLEAR.replace('y="373"', 'y="390"'))
    assert not notifications.center_over_unlocked(apart)


def test_H1_a_lit_lock_screen_with_its_notifications_is_never_pressed():
    """A lock screen lit by a notification (enabled flashlight, cells, date) without the sheet's header and Clear
    button: not proven, and Home presses nothing. (Holds on 883d54a too: the regression guard for the lit screen.)"""
    lit = _cover(_cell(1, 219), _cell(2, 419), DATE, _flashlight(True), CAMERA)
    assert notifications.is_center(lit) and not notifications.center_over_unlocked(lit)

    async def go():
        d, sent = _home_daemon(lit)
        stop, task = await started(d, until=("locked",))
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "press_button", {"name": "home"})
        assert e.value.code == "locked" and sent == []
        await finish(stop, task)
    run(go)


def test_L9_when_the_proof_fails_the_answer_does_not_claim_a_lock():
    """Fails on 883d54a: it said "the phone is locked", sending the user to unlock a phone that may be unlocked."""
    async def go():
        d, sent = _home_daemon(lock_tree())
        stop, task = await started(d, until=("locked",))
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "press_button", {"name": "home"})
        assert e.value.code == "locked" and str(e.value) == daemon_module.HOME_NOT_PROVEN
        assert str(e.value).startswith("the phone reads as locked (or Notification Center is open where Tapwright "
                                       "can't prove it)")
        assert "Nothing was pressed" in str(e.value) and "call unlock" in str(e.value) and sent == []
        await finish(stop, task)
    run(go)


# ---- the daemons the rest use ----

async def started_locked(d):
    """With a slow heartbeat nothing reads the lock: the phone connects, then reads as locked."""
    stop, task = await started(d, until=("live", "locked"))
    d._set_state("locked", "unlock the phone")
    return stop, task


def unlock_daemon(tmp_path, screen_type=SessionScreen, phone=False, heartbeat=None, **screen):
    """A daemon on a locked phone with a passcode saved (`phone`: serve --phone). `heartbeat`: a slow one, so the state
    the tool sets is the one a test reads (no heartbeat sets it meanwhile)."""
    s = FakeSession()
    s.is_locked = True
    options = {} if heartbeat is None else {"heartbeat": heartbeat}
    d = make(s, audit=AuditLog(tmp_path / "audit"), actions=ActionLog(tmp_path / "log"), **options)
    d.lock_screen = screen_type(s, **screen)
    d.ceiling = Ceiling(dedicated=not phone, phone=phone, accounts=vault(tmp_path))
    return d, s


# ---- M1: no frame of the keypad reaches an agent or a recording ----

class Watched(SessionScreen):
    """While the keys go in: `during(self)` runs (another agent's calls, a frame from the stream)."""
    during = None

    async def press_keys(self, points):
        if self.during is not None:
            await self.during(self)
        await super().press_keys(points)


def test_M1_filmstrip_compare_screen_and_screenshots_are_refused_while_the_phone_is_being_unlocked(tmp_path):
    """Fails on 883d54a: filmstrip (and the others) ran during the unlock, from the stream, while the keypad lit up."""
    async def go():
        d, s = unlock_daemon(tmp_path, Watched)
        got: dict = {}

        async def during(screen):
            for name, args in (("filmstrip", {}), ("compare_screen", {"name": "home"}), ("look", {}),
                               ("look", {"screenshot": True, "width": 400}), ("look", {"screenshot": False, "ocr": True})):
                try:
                    await asyncio.wait_for(d.run_tool("b", name, args), 3)
                    got[(name, str(args))] = "answered"
                except Refused as e:
                    got[(name, str(args))] = (e.code, str(e))
            assert d.unlocking()
        d.lock_screen.during = during
        stop, task = await started(d, until=("locked",))
        r = await d.run_tool("a", "unlock", {})
        assert r["text"].startswith("Unlocked the phone")
        assert got and all(v == ("busy", daemon_module.UNLOCK_BUSY) for v in got.values()), got
        assert "the phone is being unlocked" in daemon_module.UNLOCK_BUSY
        assert not d.unlocking()
        await finish(stop, task)
    run(go)


def test_M1_frames_of_the_unlock_are_left_out_of_recordings_and_agents_answers(tmp_path):
    """Fails on 883d54a: only Notification Center's frames were left out of `?for=recording`."""
    async def go():
        d, s = unlock_daemon(tmp_path, Watched)
        now = [1000.0]
        d.hub._mono = lambda: now[0]
        frames: dict = {}
        before = d.hub.publish(b"\xff\xd8before")
        now[0] += 1

        async def during(screen):
            frames["keys"] = d.hub.publish(b"\xff\xd8keypad")
            assert d.recording_hides(d.hub, frames["keys"])  # (while it runs, every frame is left out)
        d.lock_screen.during = during
        stop, task = await started(d, until=("locked",))
        await d.run_tool("a", "unlock", {})
        assert d.recording_hides(d.hub, frames["keys"]) and d.frame_private(frames["keys"])
        assert d.recording_covers(now[0])  # (the recorder's keepalive says frames are left out)
        assert not d.recording_hides(d.hub, before) and not d.frame_private(before)
        now[0] += 30
        later = d.hub.publish(b"\xff\xd8later")
        assert not d.recording_hides(d.hub, later) and not d.frame_private(later)
        again = d.hub.publish(b"\xff\xd8keypad")  # (bytes, not the same picture: a new frame long after is recorded)
        assert not d.recording_hides(d.hub, again)
        await finish(stop, task)
    run(go)


def test_M1_a_screenshot_of_a_call_that_raced_the_unlock_is_left_out(tmp_path):
    """A look whose frame came while the phone was being unlocked answers without the picture, with a note."""
    async def go():
        d, s = unlock_daemon(tmp_path, Watched)
        now = [1000.0]
        d.hub._mono = lambda: now[0]
        stop, task = await started(d, until=("locked",))

        async def during(screen):
            d.hub.publish(b"\xff\xd8keypad")
        d.lock_screen.during = during
        await d.run_tool("a", "unlock", {})
        view = d.control.client("a")
        view.read_frame = d.hub.latest
        answer = await d._add_shot({"text": "x"}, view, (400, False))
        assert answer.get("image") is None and daemon_module.UNLOCK_BUSY in answer["text"]
        await finish(stop, task)
    run(go)


def test_M1_a_journey_keeps_no_picture_while_the_phone_is_being_unlocked(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "reports_dir", lambda: tmp_path / "reports")

    async def go():
        d, s = unlock_daemon(tmp_path)
        s.is_locked = False
        stop, task = await started(d, until=("live",))
        await d.run_tool("j", "journey", {"start": True, "goal": "g"})
        jr = d._journeys["j"]
        span = d._unlock_begin()
        try:
            left, kept = jr.left_out, len(jr.frames)
            await d._journey_frame(jr, "tap", "Tapped e1", None)
            assert jr.left_out == left + 1 and len(jr.frames) == kept
        finally:
            d._unlock_end(span)
        await d.run_tool("j", "journey", {"stop": True, "reached": False})
        await finish(stop, task)
    run(go)


# ---- L4: no trace line per key ----

class TraceDevice:
    rsd = object()

    def __init__(self):
        self.lines: list[str] = []
        self.sent: list[str] = []

    def log(self, line):
        self.lines.append(line)

    async def http(self, method, path, payload=None, timeout=30.0):
        self.sent.append(path)
        return {"value": None}


def test_L4_the_keypad_writes_no_trace_line_per_key(tmp_path, monkeypatch):
    """Fails on 883d54a: with TAPWRIGHT_TRACE=1 each key wrote `trace POST /session/{sid}/actions N ms` (their count is
    the passcode's length), and a traced call counted each key as a touch."""
    monkeypatch.setattr(session_module, "TRACE", True)
    device = TraceDevice()
    s = session_module.Session(device)
    s.sid = "S"

    async def nothing(_):
        return None
    phone = unlock.PhoneLockScreen(s, sleep=nothing)
    trace.enable(tmp_path / "trace.jsonl")
    try:
        async def go():
            started_rec = trace.begin("tap")
            await phone.press_keys([(10.0, 10.0)] * len(PASSCODE))
            rec = started_rec[0]
            assert rec.touches == 0
            trace.end(started_rec)
            await s._call("GET", "/session/{sid}/wda/locked")  # (the trace is on: other requests still write theirs)
        asyncio.run(go())
    finally:
        trace.disable()
    assert device.sent.count("/session/S/actions") == len(PASSCODE)
    assert not any("actions" in line for line in device.lines), device.lines
    assert any("trace GET /session/{sid}/wda/locked" in line for line in device.lines)


# ---- M2: no stale `locked` ----

class WakeOpens(SessionScreen):
    """Notification Center over an unlocked phone: WebDriverAgent's unlock presses Home, which closes it."""

    async def wake(self):
        await super().wake()
        self.session.is_locked = False


def test_M2_home_under_serve_phone_after_the_automatic_unlock_pressed_is_not_nothing_was_pressed(tmp_path):
    """Fails on 883d54a: the automatic unlock's wake pressed Home (the sheet closed), the state stayed `locked`, and the
    answer was `locked: ... Nothing was pressed.`"""
    async def go():
        d, s = unlock_daemon(tmp_path, WakeOpens, phone=True, heartbeat=3600)
        stop, task = await started_locked(d)
        assert d.state == "locked"
        r = await d.run_tool("a", "press_button", {"name": "home"})
        assert "Nothing was pressed" not in r["text"] and r["text"].casefold().startswith("pressed home")
        assert d.state == "live" and d.lock_screen.pressed == []  # (nothing typed: it wasn't locked)
        await finish(stop, task)
    run(go)


def test_M2_unlock_that_finds_the_phone_not_locked_sets_it_live_at_once(tmp_path):
    """Fails on 883d54a: `The phone isn't locked.`, then the next action was refused `locked ... call unlock` until the
    heartbeat ran."""
    async def go():
        d, s = unlock_daemon(tmp_path, WakeOpens, heartbeat=3600)
        stop, task = await started_locked(d)
        r = await d.run_tool("a", "unlock", {})
        assert r["text"].splitlines()[0] == "The phone isn't locked."
        assert d.state == "live"
        r = await d.run_tool("a", "tap", {"target": {"id": "ok-button"}})
        assert r["text"].startswith("Tapped")
        await finish(stop, task)
    run(go)


def test_M2_a_successful_unlock_sets_it_live_at_once(tmp_path):
    async def go():
        d, s = unlock_daemon(tmp_path, heartbeat=3600)
        stop, task = await started_locked(d)
        await d.run_tool("a", "unlock", {})
        assert d.state == "live" and d.lock_screen.pressed == [PASSCODE]
        await finish(stop, task)
    run(go)


# ---- L2: Pause and a person, right before the first key ----

class PausedOnWake(SessionScreen):
    daemon = None
    how = "pause"

    async def wake(self):
        await super().wake()
        if self.how == "pause":
            self.daemon.control.set_paused(True)
        else:
            self.daemon.control.person_generation += 1


@pytest.mark.parametrize("how,code", [("pause", "paused"), ("person", "busy")])
def test_L2_pause_or_a_person_during_the_wake_stops_it_before_the_first_key(tmp_path, how, code):
    """Fails on 883d54a: pause and a person were checked only at the start; the passcode was typed after a Pause."""
    async def go():
        d, s = unlock_daemon(tmp_path, PausedOnWake)
        d.lock_screen.daemon, d.lock_screen.how = d, how
        stop, task = await started(d, until=("locked",))
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "unlock", {})
        assert e.value.code == code
        assert d.lock_screen.pressed == [] and s.is_locked
        assert d.unlocker.tries_left() == unlock.LIMIT  # (no attempt counted, no refusal latched)
        d.control.set_paused(False)
        await finish(stop, task)
    run(go)


# ---- L3: wakes that type nothing ----

def test_L3_at_most_three_wakes_in_ten_minutes_that_type_nothing(tmp_path):
    """Fails on 883d54a: every call woke the lock screen (Home, then a swipe up: a Face ID try each)."""
    u, clock = unlocker(path=tmp_path / "record.json")
    phone = Screen(items=[])  # (no keypad found: the Face ID / iOS 26 case)

    async def go():
        for _ in range(unlock.WAKE_LIMIT):
            r = await u.unlock(phone, PASSCODE)
            assert r.code == "not_proven"
        r = await u.unlock(phone, PASSCODE)
        assert r.code == "wake_limit" and r.message == unlock.WOKEN_TOO_OFTEN
        assert "tried to wake the lock screen 3 times in 10 minutes; ask the user to unlock it" in r.message
        assert phone.calls.count("wake") == unlock.WAKE_LIMIT and phone.pressed == []
        clock.t += unlock.WAKE_WINDOW
        r = await u.unlock(phone, PASSCODE)
        assert r.code == "not_proven" and phone.calls.count("wake") == unlock.WAKE_LIMIT + 1
    asyncio.run(go())


def test_L3_a_wake_that_typed_is_not_counted_as_one_that_typed_nothing(tmp_path):
    u, clock = unlocker(path=tmp_path / "record.json")

    async def go():
        for _ in range(unlock.LIMIT):
            phone = Screen()
            assert (await u.unlock(phone, PASSCODE)).code == "unlocked"
            clock.t += 1
        empty = Screen(items=[])
        for _ in range(unlock.WAKE_LIMIT):
            assert (await u.unlock(empty, PASSCODE)).code in ("not_proven", "limit")
    asyncio.run(go())


def test_L3_through_the_tool(tmp_path):
    async def go():
        d, s = unlock_daemon(tmp_path, items=[])
        stop, task = await started(d, until=("locked",))
        said = []
        for _ in range(unlock.WAKE_LIMIT + 1):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "unlock", {})
            said.append((e.value.code, str(e.value)))
        assert said[-1] == ("locked", unlock.WOKEN_TOO_OFTEN)
        assert d.lock_screen.calls.count("wake") == unlock.WAKE_LIMIT
        await finish(stop, task)
    run(go)


# ---- L6, L7: the right code and words ----

class Breaks(SessionScreen):
    async def locked(self):
        raise ConnectionLost("the line dropped")


def test_L6_an_unlock_that_could_not_run_says_nothing_was_typed(tmp_path):
    """Fails on 883d54a: it answered FAILED ("typing the passcode on the lock screen didn't finish")."""
    async def go():
        d, s = unlock_daemon(tmp_path, Breaks)
        stop, task = await started(d, until=("locked",))
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "unlock", {})
        assert e.value.code == "locked" and str(e.value) == unlock.NOT_RUN and "nothing was typed" in str(e.value)
        assert d.lock_screen.pressed == []
        await finish(stop, task)
    run(go)


def test_L6_a_reconnect_between_the_checks_answers_reconnecting(tmp_path):
    """Fails on 883d54a: `locked: the phone isn't connected`."""
    async def go():
        d, s = unlock_daemon(tmp_path)
        stop, task = await started(d, until=("locked",))

        async def changed():
            return unlock.Result(False, "not_connected", "the phone isn't connected")
        d.unlock_phone = changed
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "unlock", {})
        assert e.value.code == "reconnecting"
        await finish(stop, task)
    run(go)


def test_L7_a_failed_look_after_the_unlock_is_not_internal(tmp_path):
    """Fails on 883d54a: only Refused was caught around the follow-up look; a DeviceError became `internal`."""
    async def go():
        d, s = unlock_daemon(tmp_path)
        stop, task = await started(d, until=("locked",))

        async def broken(screenshot=True):
            raise DeviceError("WebDriverAgent: unknown error")
        s.look = broken
        r = await d.run_tool("a", "unlock", {})
        assert r["text"].splitlines()[0] == "Unlocked the phone with the passcode saved in the vault. Look before acting."
        assert d.state == "live"
        await finish(stop, task)
    run(go)


# ---- L8: the top-level-only branch, reached for real ----

def test_L8_unlock_as_a_tools_own_nested_call_is_refused_by_the_top_level_check(tmp_path):
    """The `_top_level()` branch itself (a tool's own call is _NESTED), not batch.check_steps. (Passes on 883d54a too:
    a missing test, not a bug.)"""
    async def go():
        d, s = unlock_daemon(tmp_path)
        stop, task = await started(d, until=("locked",))
        token = daemon_module._NESTED.set(True)
        try:
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "unlock", {})
        finally:
            daemon_module._NESTED.reset(token)
        assert e.value.code == "bad_params" and str(e.value) == daemon_module.UNLOCK_TOP_ONLY
        assert d.lock_screen.pressed == [] and s.is_locked
        await finish(stop, task)
    run(go)


def test_L8_unlock_inside_a_real_journey_is_refused(tmp_path, monkeypatch):
    """Through run_tool, the journey gate and all. (Passes on 883d54a too: a missing test, not a bug.)"""
    monkeypatch.setattr(journey, "reports_dir", lambda: tmp_path / "reports")

    async def go():
        d, s = unlock_daemon(tmp_path)
        s.is_locked = False
        stop, task = await started(d, until=("live",))
        await d.run_tool("a", "journey", {"start": True, "goal": "g"})
        s.is_locked = True
        d._set_state("locked", "unlock the phone")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "unlock", {})
        assert e.value.code == "bad_params" and str(e.value) == daemon_module.UNLOCK_TOP_ONLY
        assert d.lock_screen.pressed == []
        s.is_locked = False
        d._set_state("live")
        await d.run_tool("a", "journey", {"stop": True, "reached": False})
        await finish(stop, task)
    run(go)


def test_L8_personal_mode_with_apps_unlocks_too(tmp_path):
    """The "personal" mode of test_unlock_types_the_saved_passcode_in_every_mode set no apps; here a scope is given."""
    from tapwright.daemon import Scope

    async def go():
        d, s = unlock_daemon(tmp_path)
        d.ceiling = Ceiling(phone=False, accounts=vault(tmp_path))
        stop, task = await started(d, until=("locked",))
        r = await d.run_tool("a", "unlock", {}, scope=Scope(("com.example.app",)))
        assert r["text"].startswith("Unlocked the phone") and d.lock_screen.pressed == [PASSCODE]
        await finish(stop, task)
    run(go)


# ---- L5: the docstrings say the 1.7.8 rule ----

def test_L5_the_rules_name_the_unlock_tool_and_pause():
    """Fails on 883d54a: unlock.py's rule 1 said personal mode and --dedicated answer `locked` as in 1.6."""
    doc = unlock.__doc__
    assert "`unlock` tool" in doc and "Pause in the live view is the owner's stop" in doc
    assert "a locked\n    phone answers `locked` as in 1.6" not in doc
    assert "Pause" in daemon_module.Daemon._unlocks_itself.__doc__

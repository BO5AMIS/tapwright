"""Review R191 (REVIEW-1.9.1-OPUS1.md) of plug and forget, now 1.9.2: M1 the update rule narrowed to the update screen
itself, M2 the lock-state read capped as a whole, M3 the restart gate only with a passcode and with an off switch, L1 the
log says whether the runner was kept, L3 many unlocks are noted. Each test failed on 078fad4."""
import asyncio
import time

import pytest
from fakes import FakeSession
from test_daemon import make, run
from test_device import _fake_phone_side, _Service
from test_unlock import PASSCODE, SB, SCREEN, Screen, keypad, unlocker, window

from tapwright import device as device_module
from tapwright import ui, unlock
from tapwright.device import ConnectionLost, LockState, NotUnlockedSinceRestart


# ---- M1: notification and Live Activity words never refuse; an unknown window does ----
def _text(label: str, role: str = "text", kind: str = "StaticText") -> ui.Element:
    return ui.Element(f"t-{label[:8]}", role, label, rect=(128.0, 11.0, 168.0, 37.0), type=kind, name=label)


@pytest.mark.parametrize("words", ["Order update: arriving in 5 min", "Score updated: 2-1", "App Store: 3 updates",
                                   "iOS 26.6 Update", "Remind me later"])
def test_a_notification_or_live_activity_saying_update_is_still_the_lock_screen(words):
    """(the 08:14 layout: the Dynamic Island's words are on-screen items next to the cover sheet)"""
    tree = ui.Tree(keypad(extra=(_text(words),)))
    assert unlock.foreign_kind(tree) is None and unlock.find_entry(tree, SB) is not None


def test_a_live_activity_saying_update_does_not_stop_an_unlock():
    u, _ = unlocker()
    screen = Screen(items=keypad(extra=(_text("Delivery update: 2 stops away"),)))
    r = run_unlock(u, screen)
    assert (r.ok, r.code) == (True, "unlocked") and screen.pressed == [PASSCODE]


def run_unlock(u, screen):
    return asyncio.run(u.unlock(screen, PASSCODE))


@pytest.mark.parametrize("name", ["SBTransientOverlayWindow", "SBSomeNewOverlayWindow"])  # (unnamed: test_review_192, from XML)
def test_any_window_but_the_lock_screens_refuses_and_names_no_button(name):
    tree = ui.Tree(keypad(extra=(window(name),)))
    assert unlock.foreign_kind(tree) == "window" and unlock.find_entry(tree, SB) is None
    u, _ = unlocker()
    screen = Screen(items=keypad(extra=(window(name),)))
    r = run_unlock(u, screen)
    assert (r.code, r.message) == ("not_proven", unlock.OTHER_KEYPAD) and screen.pressed == []
    assert "Remind Me Later" not in r.message and "tap" not in r.message


def test_update_text_without_its_button_asks_the_user_to_close_it_and_names_no_button():
    u, _ = unlocker()
    screen = Screen(items=keypad(extra=(_text("Enter Passcode to Update Tonight"),)))
    r = run_unlock(u, screen)
    assert (r.code, r.message) == ("update_prompt", unlock.UPDATE_SCREEN) and screen.pressed == []
    assert "Remind Me Later" not in r.message and "ask the user" in r.message.casefold()


def test_the_remind_me_later_button_is_named_only_as_the_users_to_tap():
    u, _ = unlocker()
    screen = Screen(items=keypad(extra=(ui.Element("b", "button", "Remind Me Later", type="Button"),)))
    r = run_unlock(u, screen)
    assert (r.code, r.message) == ("update_prompt", unlock.UPDATE_PROMPT) and screen.pressed == []
    assert "Ask the user to tap" in r.message


def test_remind_me_later_as_plain_text_is_not_the_button():
    tree = ui.Tree(keypad(extra=(_text("Remind Me Later"),)))
    assert unlock.find_entry(tree, SB) is not None


# ---- M2: the whole lock-state read under the cap ----
def test_a_lock_state_service_whose_connect_hangs_is_given_up_at_the_cap(monkeypatch):
    import pymobiledevice3.remote.core_device.device_info as device_info

    class Hanging:
        def __init__(self, rsd):
            pass

        async def __aenter__(self):
            await asyncio.sleep(3600)  # the connect never returns (a stalled phone)

        async def __aexit__(self, *a):
            return False
    monkeypatch.setattr(device_info, "DeviceInfoService", Hanging)
    monkeypatch.setattr(device_module, "LOCK_STATE_WAIT", 0.2)

    async def go():
        d = device_module.Device(log=lambda *a: None)
        d.rsd = object()
        began = time.monotonic()
        assert await d.lock_state() is None
        return time.monotonic() - began
    assert asyncio.run(go()) < 2.0


# ---- M3: the restart gate only with a passcode, and an off switch ----
def _phone(lock: dict):
    d = device_module.Device(log=lambda *a: None)
    d.rsd = object()

    async def lock_state():
        return LockState.of(lock)

    async def alive():
        return True
    d.lock_state, d.wda_alive = lock_state, alive
    return d


def test_a_phone_without_a_passcode_not_unlocked_since_boot_still_starts(monkeypatch):
    """A phone with no passcode reads unlockedSinceBoot false until its first swipe: 1.9.0 started a runner there."""
    _fake_phone_side(monkeypatch, _Service)

    async def go():
        d = _phone({"passcodeRequired": False, "unlockedSinceBoot": False})
        await d.start_wda("runner.id", wait=5)
        assert d.runner is not None
        await d.close()
    asyncio.run(go())


def test_the_restart_gate_can_be_turned_off(monkeypatch):
    _fake_phone_side(monkeypatch, _Service)
    monkeypatch.setenv("TAPWRIGHT_RESTART_GATE", "0")

    async def go():
        d = _phone({"passcodeRequired": True, "unlockedSinceBoot": False})
        await d.start_wda("runner.id", wait=5)
        assert d.runner is not None
        await d.close()
    asyncio.run(go())


def test_the_restart_gate_is_on_by_default_with_a_passcode(monkeypatch):
    _fake_phone_side(monkeypatch, _Service)
    monkeypatch.delenv("TAPWRIGHT_RESTART_GATE", raising=False)

    async def go():
        d = _phone({"passcodeRequired": True, "unlockedSinceBoot": False})
        with pytest.raises(NotUnlockedSinceRestart):
            await d.start_wda("runner.id", wait=5)
        assert d.runner is None
        await d.close()
    asyncio.run(go())


# ---- L1: the log says whether the second try kept the runner ----
class _Swapping(FakeSession):
    """The first ensure times out; the second succeeds, and (`swap`) replaces the runner on the way."""

    def __init__(self, swap: bool):
        super().__init__()
        self.failed = False
        self.swap = swap
        self.device.runner = object()
        self.device.runner_alive = lambda: True

        async def wda_alive():
            return True
        self.device.wda_alive = wda_alive

    async def ensure(self):
        if not self.failed:
            self.failed = True
            self._record("ensure")
            raise ConnectionLost("WebDriverAgent didn't answer POST /session within 30 s")
        if self.swap:
            self.device.runner = object()
        await super().ensure()


@pytest.mark.parametrize("swap, said", [(False, "kept the running runner"), (True, "replaced the runner")])
def test_the_log_says_whether_the_runner_was_kept_or_replaced(swap, said):
    logged: list[str] = []

    async def go():
        s = _Swapping(swap)
        d = make(s, log=logged.append)
        await d._connect()
        assert "reset" not in s.calls
    run(go)
    assert said in logged and ("kept the running runner" in logged) == (not swap)


# ---- L3: unlocks aren't limited, but many in an hour are noted ----
def test_unlocks_in_the_last_hour_are_counted():
    u, clock = unlocker()
    screen = Screen()
    for _ in range(3):
        screen.is_locked = True
        assert run_unlock(u, screen).code == "unlocked"
    assert u.unlocks_last_hour() == 3
    clock.t += 3601
    assert u.unlocks_last_hour() == 0


def test_the_daemon_notes_a_phone_that_keeps_locking(tmp_path):
    from test_unlock_daemon import locked_daemon
    from test_daemon import finish, started
    logged: list[str] = []

    async def go():
        d, s = locked_daemon(tmp_path, said=logged, phone=False)
        stop, task = await started(d, until=("locked",))
        d.unlocker.unlocks_last_hour = lambda: 11
        result = await d.unlock_phone()
        assert result.code == "unlocked"
        await finish(stop, task)
    run(go)
    assert any("11 unlocks in the last hour" in line for line in logged)

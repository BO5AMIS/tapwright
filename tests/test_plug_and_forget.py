"""Plug and forget (W185): no runner is launched where it could only put iOS's XCTest passcode prompt over a lock
screen, and the time of iOS's last consent to UI automation is kept for the next start."""
import asyncio
import json
import time

import pytest

from tapwright import device as device_module
from tapwright.device import (GRANT_HORIZON, LockedBeforeStart, LockState, NotUnlockedSinceRestart, PasscodeNeeded,
                              PhoneLocked)
from tests.test_device import _fake_phone_side, _Info, _PromptInfo, _Service


async def _value(v):
    return v


class _Counting(_Service):
    launched: list = []

    async def run(self, cfg, listener=None):
        self.launched.append(1)
        await asyncio.sleep(3600)


def _phone(lock: dict | None, alive: bool = False) -> device_module.Device:
    d = device_module.Device(log=lambda *a: None)
    d.rsd = object()
    d.lock_state = lambda: _value(LockState.of(lock))
    d.wda_alive = lambda: _value(alive)
    return d


def test_lock_state_reads_only_the_known_shape():
    assert LockState.of({"passcodeRequired": True, "unlockedSinceBoot": False}) == LockState(True, False)
    assert LockState.of({"passcodeRequired": False, "unlockedSinceBoot": True, "extra": 1}) == LockState(False, True)
    for odd in (None, [], "locked", {}, {"passcodeRequired": 1, "unlockedSinceBoot": True},
                {"passcodeRequired": True}, {"passcodeRequired": True, "unlockedSinceBoot": "yes"}):
        assert LockState.of(odd) is None


def test_the_grant_is_kept_on_disk_and_ages():
    assert device_module.read_grant() is None and device_module.grant_stale() is None
    device_module.record_grant(1000.0)
    assert device_module.read_grant() == 1000.0
    assert device_module.grant_stale(now=1000.0 + GRANT_HORIZON - 1) is False
    assert device_module.grant_stale(now=1000.0 + GRANT_HORIZON + 1) is True


def test_a_broken_grant_file_is_not_known(tmp_path, monkeypatch):
    path = tmp_path / "g.json"
    monkeypatch.setattr(device_module, "GRANT_FILE", path)
    for text in ("not json", "[]", json.dumps({"granted_at": "soon"}), json.dumps({"granted_at": True})):
        path.write_text(text)
        assert device_module.read_grant() is None


def test_nothing_is_launched_on_a_phone_not_unlocked_since_its_restart(monkeypatch):
    _Counting.launched = []
    _fake_phone_side(monkeypatch, _Counting)

    async def scenario():
        d = _phone({"passcodeRequired": True, "unlockedSinceBoot": False})
        with pytest.raises(NotUnlockedSinceRestart, match="restarted") as e:
            await d.start_wda("runner.id", wait=5)
        assert isinstance(e.value, PhoneLocked) and not isinstance(e.value, PasscodeNeeded)
        assert _Counting.launched == [] and d.runner is None
        await d.close()
    asyncio.run(scenario())


def test_a_phone_that_cannot_say_its_lock_state_is_started_as_before(monkeypatch):
    _fake_phone_side(monkeypatch, _Service)

    async def scenario():
        d = _phone(None, alive=True)
        await d.start_wda("runner.id", wait=5)
        assert d.runner is not None and not d.runner.done()
        await d.close()
    asyncio.run(scenario())


def test_a_locked_phone_is_started_without_the_gate(monkeypatch):
    """Off by default: a locked phone within the consent's hours starts (the vault unlock needs a runner)."""
    monkeypatch.delenv("TAPWRIGHT_LOCK_GATE", raising=False)
    _fake_phone_side(monkeypatch, _Service)

    async def scenario():
        d = _phone({"passcodeRequired": True, "unlockedSinceBoot": True}, alive=True)
        await d.start_wda("runner.id", wait=5)
        assert d.runner is not None
        await d.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("granted_ago, held", [(None, True), (GRANT_HORIZON + 60, True), (60.0, False)])
def test_the_gate_holds_a_locked_phone_only_when_the_consent_is_spent(monkeypatch, granted_ago, held):
    monkeypatch.setenv("TAPWRIGHT_LOCK_GATE", "1")
    _fake_phone_side(monkeypatch, _Service)
    if granted_ago is not None:
        device_module.record_grant(time.time() - granted_ago)

    async def scenario():
        d = _phone({"passcodeRequired": True, "unlockedSinceBoot": True})
        old = asyncio.create_task(asyncio.sleep(3600))  # a runner task whose WebDriverAgent stopped answering
        d.runner = old
        try:
            if held:
                with pytest.raises(LockedBeforeStart, match="unlock the phone"):
                    await d.start_wda("runner.id", wait=5)
                assert d.runner is old and not old.done()  # the gate stops nothing that runs
            else:
                answers = iter([False] * device_module.REUSE_ASKS + [True])
                d.wda_alive = lambda: _value(next(answers))
                await d.start_wda("runner.id", wait=5)
                assert d.runner is not old
        finally:
            old.cancel()
            await asyncio.gather(old, return_exceptions=True)
            await d.close()
    asyncio.run(scenario())


def test_the_gate_lets_an_unlocked_phone_start(monkeypatch):
    monkeypatch.setenv("TAPWRIGHT_LOCK_GATE", "1")
    _fake_phone_side(monkeypatch, _Service)

    async def scenario():
        d = _phone({"passcodeRequired": False, "unlockedSinceBoot": True}, alive=True)
        await d.start_wda("runner.id", wait=5)
        assert d.runner is not None
        await d.close()
    asyncio.run(scenario())


def test_a_start_that_goes_on_after_the_prompt_records_the_consent(monkeypatch):
    import pymobiledevice3.services.dvt.instruments.device_info as device_info
    _fake_phone_side(monkeypatch, _Service)
    monkeypatch.setattr(device_info, "DeviceInfo", _PromptInfo)
    monkeypatch.setattr(device_module, "PROMPT_AFTER", 0.2)

    async def scenario():
        d = _phone(None)
        with pytest.raises(PasscodeNeeded):
            await d.start_wda("runner.id", wait=30)
        assert device_module.read_grant() is None
        monkeypatch.setattr(device_info, "DeviceInfo", _Info)  # the person typed it: the screen is gone
        d.wda_alive = lambda: _value(True)
        before = time.time()
        await d.start_wda("runner.id", wait=30)
        granted = device_module.read_grant()
        assert granted is not None and granted >= before
        await d.close()
    asyncio.run(scenario())


def test_a_start_with_no_prompt_records_nothing(monkeypatch):
    _fake_phone_side(monkeypatch, _Service)

    async def scenario():
        d = _phone(None, alive=True)
        await d.start_wda("runner.id", wait=5)
        assert device_module.read_grant() is None
        await d.close()
    asyncio.run(scenario())


def test_the_lock_state_of_a_phone_with_no_tunnel_is_not_known():
    d = device_module.Device(log=lambda *a: None)
    assert asyncio.run(d.lock_state()) is None
    d.rsd = object()  # (no CoreDevice service behind it)
    assert asyncio.run(d.lock_state()) is None

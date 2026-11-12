"""The "needs a person" list (1.3.1): doctor's heading, and the first answer an agent gets."""
import asyncio

import pytest
from pymobiledevice3.exceptions import NotPairedError, PasswordRequiredError

from tapwright import cli, needs
from tapwright.device import PasscodeNeeded
from fakes import FakeSession
from test_daemon import finish, make, started


def run(go):
    return asyncio.run(go())


def test_the_daemons_state_names_the_passcode_prompt_and_the_locked_phone():
    assert needs.from_state("live", None) == []
    assert needs.from_state("locked", "unlock the phone") == [needs.LOCKED]
    assert needs.from_state("reconnecting", str(PasscodeNeeded())) == [needs.PASSCODE]
    assert needs.PASSCODE.startswith("Enter the passcode on the phone (the XCTest prompt is up).")
    assert "Enable UI Automation" in needs.PASSCODE


def test_the_phone_on_usb_is_asked_only_what_it_can_say():
    class Phone:
        def __init__(self, mode):
            self.mode = mode

        async def get_developer_mode_status(self):
            return self.mode

    def gives(value):
        async def connect(udid):
            if isinstance(value, Exception):
                raise value
            return value
        return connect

    def found(value):
        return run(lambda: cli.lockdown_needs("udid", gives(value)))
    assert found(Phone(True)) == [] and found(Phone(None)) == []
    assert found(Phone(False)) == [needs.DEVELOPER_MODE]
    assert found(NotPairedError()) == [needs.NOT_TRUSTED]
    assert found(PasswordRequiredError()) == [needs.LOCKED]
    assert found(OSError("no answer")) == []


def test_doctor_lists_them_under_a_heading(capsys):
    cli.print_needs([])
    assert capsys.readouterr().out == ""
    cli.print_needs([needs.LOCKED, needs.DEVELOPER_MODE])
    out = capsys.readouterr().out
    assert "Needs a person:\n  - Unlock the phone.\n  - Turn on Developer Mode" in out


def test_the_first_answer_to_an_agent_carries_them_once():
    async def go():
        s = FakeSession()
        s.is_locked = True
        d = make(s)
        stop, task = await started(d, until=("locked",))
        first = await d.run_tool("a", "look", {"screenshot": False})
        assert first["text"].splitlines()[-1] == f"Note: {needs.LOCKED}"
        second = await d.run_tool("a", "look", {"screenshot": False})
        assert needs.LOCKED not in second["text"]
        other = await d.run_tool("b", "look", {"screenshot": False})
        assert f"Note: {needs.LOCKED}" in other["text"]
        # a refusal says its own (`locked`), and doesn't double it
        with pytest.raises(Exception) as e:
            await d.run_tool("c", "tap", {"target": {"x": 1, "y": 1}})
        assert str(e.value) == "the phone is locked; ask the user to unlock it"
        await finish(stop, task)
    run(go)


def test_a_phone_with_nothing_to_clear_adds_nothing():
    async def go():
        d = make()
        stop, task = await started(d)
        assert "Note:" not in (await d.run_tool("a", "look", {"screenshot": False}))["text"]
        await finish(stop, task)
    run(go)

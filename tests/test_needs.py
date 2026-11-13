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
        def __init__(self, mode, paired=True):
            self.mode = mode
            self.paired = paired

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
    # (Codex P2, PR #5) autopair=False hands back an unpaired client instead of raising: that phone needs Trust
    assert found(Phone(True, paired=False)) == [needs.NOT_TRUSTED]
    assert found(Phone(None, paired=False)) == [needs.NOT_TRUSTED]


def test_doctor_inspects_the_configured_phone_when_several_are_on_usb(monkeypatch):
    """CodeRabbit, PR #5: the first phone of the list was checked, not the one doctor reports as configured."""
    from tapwright import client, store
    asked = []

    async def lockdown(udid, connect=None):
        asked.append(udid)
        return []
    monkeypatch.setattr(client, "healthy", lambda info: None)
    monkeypatch.setattr(store, "read_serve_info", lambda: None)
    monkeypatch.setattr(cli, "lockdown_needs", lockdown)
    monkeypatch.setattr(store, "load_state", lambda: {"udid": "B"})
    run(lambda: cli.needs_person(["A", "B"]))
    monkeypatch.setattr(store, "load_state", lambda: {"udid": "Z"})  # not plugged in: the first, as doctor --fix does
    run(lambda: cli.needs_person(["A", "B"]))
    monkeypatch.setattr(store, "load_state", lambda: {})
    run(lambda: cli.needs_person(["A", "B"]))
    assert asked == ["B", "A", "A"]


def test_doctor_checks_where_it_runs_from_before_it_loads_the_signing_modules(monkeypatch, capsys):
    """CodeRabbit, PR #5: a signing.py with merge markers raised SyntaxError at doctor's import, before the check
    that names the conflict could run. A failed check ends doctor there."""
    from types import SimpleNamespace
    seen = []

    def checkout(package=None):
        seen.append("checkout")
        cli.fail("unresolved merge markers in tapwright/signing.py")
        return 1
    monkeypatch.setattr(cli, "report_checkout", checkout)
    assert run(lambda: cli.doctor(SimpleNamespace(fix=False))) == 1
    out = capsys.readouterr().out
    assert seen == ["checkout"] and "unresolved merge markers" in out and "device service" not in out


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


def test_a_phone_locked_at_connect_is_told_on_the_first_answer_even_though_the_state_is_connecting(tmp_path):
    """CodeRabbit, PR #5: a PhoneLocked at connect leaves the state connecting/reconnecting, not locked, and the first
    answer (a `record` start succeeds in that state) said nothing about it. The passcode prompt keeps its own note."""
    from tapwright.device import PhoneLocked

    async def go():
        for stuck, note, other in ((PhoneLocked("the phone is locked"), needs.LOCKED, needs.PASSCODE),
                                   (PasscodeNeeded(), needs.PASSCODE, needs.LOCKED)):
            s = FakeSession()
            s.stuck = stuck
            d = make(s)
            stop, task = await started(d, until=())
            for _ in range(300):
                if d.stuck is not None:
                    break
                await asyncio.sleep(0.01)
            assert d.state != "locked" and d.stuck is not None
            answer = await d.run_tool("a", "record", {"start": True})
            lines = answer["text"].splitlines()
            assert f"Note: {note}" in lines and f"Note: {other}" not in lines, answer["text"]
            await finish(stop, task)
    run(go)


def test_a_phone_with_nothing_to_clear_adds_nothing():
    async def go():
        d = make()
        stop, task = await started(d)
        assert "Note:" not in (await d.run_tool("a", "look", {"screenshot": False}))["text"]
        await finish(stop, task)
    run(go)

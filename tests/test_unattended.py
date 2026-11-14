"""`doctor --unattended` (1.6, Part J4): what would stop a run without a person, and how to fix each. The phone's facts
are faked: a phone is never reached here."""
import asyncio
import json

import pytest
from pymobiledevice3.exceptions import NotPairedError, PasswordRequiredError

from tapwright import cli, needs

OK = needs.Facts(trusted=True, locked=False, developer_mode=True, ui_automation=True, passcode_given=True, battery=80)


def whats(facts: needs.Facts) -> list[str]:
    return [stop.what for stop in needs.unattended(facts)]


def test_a_phone_that_is_ready_has_nothing_in_the_way():
    assert needs.unattended(OK) == []


def test_a_locked_phone_stops_a_run_and_says_how_to_fix_it():
    (stop,) = needs.unattended(OK._replace(locked=True, passcode_given=False))
    assert stop.what.startswith("The phone is locked, and no passcode is given")
    assert stop.fix.startswith(needs.LOCKED) and "phone.passcode" not in stop.fix and "--accounts" in stop.fix


def test_an_unlocked_phone_with_no_passcode_given_is_still_a_risk_for_a_long_run():
    (stop,) = needs.unattended(OK._replace(passcode_given=False))
    assert stop.what.startswith("No passcode is given: if the phone locks during a run")


def test_a_passcode_given_lifts_the_lock_because_the_tool_types_it_on_the_lock_screen():
    """1.7 (§18.6, §24.5): with `phone.passcode` in the vault the tool unlocks the phone itself, so a locked phone is no
    longer a stop; without it, the fix says how to give one (never the value)."""
    assert needs.unattended(OK._replace(locked=True, passcode_given=True)) == []
    (stop,) = needs.unattended(OK._replace(passcode_given=False))
    assert "--accounts" in stop.fix and "skipped" in stop.what
    ready = needs.unattended_ready(OK)
    assert any(line.startswith("The vault gives phone.passcode (never shown)") for line in ready)


def test_enable_ui_automation_unknown_is_listed_and_off_is_listed_harder():
    (unknown,) = needs.unattended(OK._replace(ui_automation=None))
    assert unknown.what.startswith("Enable UI Automation can't be read from the computer")
    assert "Settings > Developer > Enable UI Automation" in unknown.fix
    (off,) = needs.unattended(OK._replace(ui_automation=False))
    assert off.what.startswith("Enable UI Automation is off") and "Settings > Developer > Enable UI Automation" in off.fix


def test_an_untrusted_computer_and_developer_mode_off_have_their_own_fixes():
    (stop,) = needs.unattended(OK._replace(trusted=False))
    assert stop.what.startswith("This computer isn't trusted by the phone") and stop.fix == needs.NOT_TRUSTED
    (stop,) = needs.unattended(OK._replace(developer_mode=False))
    assert stop.what == "Developer Mode is off." and stop.fix == needs.DEVELOPER_MODE
    assert needs.unattended(OK._replace(developer_mode=None)) == []  # can't be read: not listed


@pytest.mark.parametrize("battery, listed", [(19, True), (20, False), (5, True), (100, False), (None, False)])
def test_a_battery_under_twenty_percent_is_listed(battery, listed):
    found = [w for w in whats(OK._replace(battery=battery)) if w.startswith("The battery")]
    assert bool(found) is listed
    if listed:
        assert found == [f"The battery is at {battery}%: the phone may switch itself off in a long run."]


def test_the_stops_come_in_the_order_a_person_would_fix_them():
    every = needs.Facts(trusted=False, locked=True, developer_mode=False, ui_automation=False, passcode_given=False, battery=10)
    got = whats(every)
    assert [w.split(" ")[0:3] for w in got] == [
        ["This", "computer", "isn't"], ["Developer", "Mode", "is"], ["Enable", "UI", "Automation"],
        ["The", "phone", "is"], ["The", "battery", "is"]]


# ---- reading the facts ----
def run(go):
    return asyncio.run(go())


class Phone:
    def __init__(self, mode=True, battery=80, fail=False):
        self.mode, self.battery, self.fail = mode, battery, fail

    async def get_developer_mode_status(self):
        return self.mode

    async def get_value(self, domain=None, key=None):
        if self.fail:
            raise OSError("no answer")
        assert (domain, key) == ("com.apple.mobile.battery", "BatteryCurrentCapacity")
        return self.battery


def gives(value):
    async def connect(udid):
        if isinstance(value, Exception):
            raise value
        return value
    return connect


def facts_of(value) -> dict:
    return run(lambda: cli.lockdown_facts("udid", gives(value)))


def test_the_phone_on_usb_gives_its_facts_and_never_more():
    assert facts_of(Phone(True, 80)) == {"trusted": True, "locked": False, "developer_mode": True, "battery": 80}
    assert facts_of(Phone(False, 12)) == {"trusted": True, "locked": False, "developer_mode": False, "battery": 12}
    assert facts_of(Phone(None, fail=True)) == {"trusted": True, "locked": False, "developer_mode": None, "battery": None}
    assert facts_of(NotPairedError()) == {"trusted": False}
    assert facts_of(PasswordRequiredError()) == {"trusted": True, "locked": True}
    assert facts_of(OSError("no answer")) == {}  # can't be read: nothing is claimed


def test_a_battery_that_is_not_a_percentage_is_not_believed():
    assert facts_of(Phone(True, "full"))["battery"] is None
    assert facts_of(Phone(True, True))["battery"] is None
    assert facts_of(Phone(True, 140))["battery"] is None


def test_the_passcode_is_given_only_by_an_accounts_file_with_a_phone_passcode(tmp_path):
    good = tmp_path / "accounts.json"
    good.write_text(json.dumps({"version": 1, "accounts": {"phone": {"passcode": "123456"}}}), encoding="utf-8")
    assert cli.passcode_given(str(good)) is True
    for text in (json.dumps({"version": 1, "accounts": {"phone": {}}}), json.dumps({"version": 1, "accounts": {"phone": {"passcode": ""}}}),
                 json.dumps({"version": 1, "accounts": {"tester": {"username": "a", "password": "b"}}}),
                 json.dumps({"accounts": {"phone": {"passcode": 123456}}}), "not json", "[]"):
        bad = tmp_path / "bad.json"
        bad.write_text(text, encoding="utf-8")
        assert cli.passcode_given(str(bad)) is False, text
    assert cli.passcode_given(str(tmp_path / "missing.json")) is False
    assert cli.passcode_given(None) is False


def test_the_report_names_each_stop_with_its_fix_and_never_prints_the_passcode(tmp_path, capsys):
    accounts = tmp_path / "accounts.json"
    accounts.write_text(json.dumps({"version": 1, "accounts": {"phone": {"passcode": "246810"}}}), encoding="utf-8")
    found = run(lambda: cli.unattended_stops(["udid"], str(accounts), connect=gives(Phone(True, 9)), status=None))
    assert [s.what for s in found][-1] == "The battery is at 9%: the phone may switch itself off in a long run."
    cli.print_unattended(found)
    out = capsys.readouterr().out
    assert out.startswith("\nWhat would stop an unattended run:\n") and "Fix: " in out and "246810" not in out


def test_the_running_daemon_says_the_phone_is_locked_or_asking_for_its_passcode(tmp_path):
    status = {"state": "locked", "detail": "unlock the phone"}
    found = run(lambda: cli.unattended_stops(["udid"], None, connect=gives(Phone(True, 80)), status=status))
    assert any(s.what.startswith("The phone is locked") for s in found)
    asking = {"state": "locked", "detail": "the phone is asking for its passcode to allow UI automation"}
    found = run(lambda: cli.unattended_stops(["udid"], None, connect=gives(Phone(True, 80)), status=asking))
    assert any(s.what.startswith("Enable UI Automation is off") for s in found)


def test_no_phone_on_usb_still_lists_what_the_computer_can_tell(capsys):
    found = run(lambda: cli.unattended_stops([], None, status=None))
    assert [s.what.split(" ")[0] for s in found] == ["Enable", "No"]  # UI Automation unknown; no passcode given
    cli.print_unattended([])
    assert "Nothing doctor can see would stop an unattended run." in capsys.readouterr().out


def test_doctor_takes_unattended_and_an_accounts_file():
    import subprocess
    import sys
    out = subprocess.run([sys.executable, "-m", "tapwright", "doctor", "--help"], capture_output=True, text=True, timeout=60)
    assert "--unattended" in out.stdout and "--accounts" in out.stdout


def test_an_unpaired_client_that_does_not_raise_is_not_trusted_and_the_configured_phone_is_the_one_read():
    class Unpaired(Phone):
        paired = False
    assert facts_of(Unpaired(True, 80)) == {"trusted": False}
    asked = []

    async def connect(udid):
        asked.append(udid)
        return Phone(True, 80)
    run(lambda: cli.unattended_stops(["first", "second"], None, connect=connect, status=None))
    assert asked == [cli.configured_phone(["first", "second"])]

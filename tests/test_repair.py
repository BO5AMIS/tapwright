"""Signing expiry and `doctor --fix`: the decisions and the order of repairs, with a fake phone."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from tapwright import mcp_server, repair
from tapwright.repair import Expiry, WdaFacts, decide_wda

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


def days(n: int) -> datetime:
    return NOW + timedelta(days=n)


def facts(**kw) -> WdaFacts:
    base = dict(installed=True, installed_expiry=None, profile=Expiry("profile 'p'", days(200)),
                profile_has_phone=True, certificate=Expiry("certificate", days(300)))
    base.update(kw)
    return WdaFacts(**base)


def test_expiry_lines_name_the_date_and_stay_quiet_when_far():
    lines = repair.expiry_lines([Expiry("profile 'p'", days(20)), Expiry("certificate", days(90)),
                                 Expiry("old", days(-3))], NOW)
    assert lines == ["the old expired on 2026-11-08", "the profile 'p' expires on 2026-12-01 (in 20 days)"]
    assert repair.expiry_lines([Expiry("certificate", days(1))], NOW) == [
        "the certificate expires on 2026-11-12 (in 1 day)"]


def test_the_warning_is_none_when_all_is_far_and_names_the_date_when_near(monkeypatch):
    monkeypatch.undo()  # the shared fixture silenced it
    monkeypatch.setattr(repair, "profile_expiry", lambda: Expiry("profile 'p'", days(200)))
    monkeypatch.setattr(repair, "certificate_expiry", lambda: Expiry("certificate", days(100)))
    assert repair.expiry_warning(NOW) is None
    monkeypatch.setattr(repair, "profile_expiry", lambda: Expiry("profile 'p'", days(10)))
    text = repair.expiry_warning(NOW)
    assert "2026-11-21" in text and "tapwright setup" in text


def test_the_first_answer_of_a_session_carries_the_warning_once(monkeypatch):
    monkeypatch.setattr(repair, "expiry_warning", lambda now=None: "Tapwright signing: soon.")
    monkeypatch.setattr(mcp_server, "_notice_given", False)
    assert mcp_server._first_answer_notice() == "Tapwright signing: soon."
    assert mcp_server._first_answer_notice() is None


def test_a_failing_warning_never_breaks_a_call(monkeypatch):
    def boom(now=None):
        raise OSError("disk")
    monkeypatch.setattr(repair, "expiry_warning", boom)
    monkeypatch.setattr(mcp_server, "_notice_given", False)
    assert mcp_server._first_answer_notice() is None


@pytest.mark.parametrize("kw, verdict", [
    (dict(), "ok"),
    (dict(installed=False), "reinstall"),
    (dict(installed=False, profile=Expiry("p", days(5))), "reinstall"),  # a short profile still installs
    (dict(profile=None), "setup"),
    (dict(profile_has_phone=False), "setup"),
    (dict(profile=Expiry("p", days(-1))), "setup"),
    (dict(installed=False, profile=Expiry("p", days(-1))), "setup"),
    (dict(certificate=Expiry("certificate", days(-1))), "setup"),
    (dict(profile=Expiry("p", days(10))), "setup"),  # ends in 10 days and nothing newer is stored
    (dict(installed_expiry=days(5)), "reinstall"),  # the phone's copy ends soon, the stored profile is newer
    (dict(installed_expiry=days(-5)), "reinstall"),
    (dict(installed_expiry=days(60)), "ok"),
    (dict(installed_expiry=days(5), profile=Expiry("p", days(8))), "setup"),
])
def test_what_to_do_about_webdriveragent(kw, verdict):
    assert decide_wda(facts(**kw), NOW).verdict == verdict


def test_setup_is_never_a_silent_new_certificate():
    d = decide_wda(facts(profile=Expiry("p", days(-1))), NOW)
    assert "tapwright setup" in d.reason and "2026-11-10" in d.reason


class FakePhone(repair.Phone):
    def __init__(self, mounted=True, installed=True, found=5555, saved=4444, fail_mount=False):
        self.mounted, self.installed, self.found, self.saved = mounted, installed, found, saved
        self.fail_mount = fail_mount
        self.did: list[str] = []

    async def image_mounted(self):
        return self.mounted

    async def mount_image(self):
        self.did.append("mount")
        self.mounted = not self.fail_mount

    async def wda_installed(self):
        return self.installed

    async def install_wda(self):
        self.did.append("install")
        self.installed = True

    async def refresh_pairing_port(self):
        self.did.append("pairing")
        return self.found, self.saved


@pytest.fixture
def local(monkeypatch):
    monkeypatch.setattr(repair, "profile_expiry", lambda: Expiry("profile 'p'", days(200)))
    monkeypatch.setattr(repair, "certificate_expiry", lambda: Expiry("certificate", days(300)))
    monkeypatch.setattr(repair, "profile_includes_phone", lambda: True)
    monkeypatch.setattr(repair, "recorded_install_expiry", lambda: None)


def fix(phone: FakePhone) -> tuple[int, list[tuple[str, str]]]:
    said: list[tuple[str, str]] = []
    left = asyncio.run(repair.run_fixes(phone, lambda kind, msg: said.append((kind, msg)), NOW))
    return left, said


def test_everything_fine_changes_nothing_but_the_pairing_check(local):
    phone = FakePhone(found=4444)
    left, said = fix(phone)
    assert left == 0 and phone.did == ["pairing"]
    assert [k for k, _ in said] == ["ok", "ok", "ok"]


def test_it_mounts_installs_and_refreshes_in_order(local):
    phone = FakePhone(mounted=False, installed=False)
    left, said = fix(phone)
    assert left == 0 and phone.did == ["mount", "install", "pairing"]
    text = "\n".join(m for _, m in said)
    assert "mounted now" in text and "installed again" in text and "refreshed to 5555 (was 4444)" in text


def test_a_mount_that_does_not_take_is_a_problem_and_the_rest_still_runs(local):
    phone = FakePhone(mounted=False, fail_mount=True)
    left, said = fix(phone)
    assert left == 1 and phone.did == ["mount", "pairing"]
    assert any(k == "fail" and "still not mounted" in m for k, m in said)


def test_an_expired_profile_is_not_replaced_here(local, monkeypatch):
    monkeypatch.setattr(repair, "profile_expiry", lambda: Expiry("profile 'p'", days(-2)))
    phone = FakePhone(installed=True)
    left, said = fix(phone)
    assert left == 1 and "install" not in phone.did
    assert any("tapwright setup" in m for k, m in said if k == "fail")


def test_a_phone_that_raises_is_reported_not_raised(local):
    class Broken(FakePhone):
        async def wda_installed(self):
            raise RuntimeError("lockdown said no")
    left, said = fix(Broken())
    assert left == 1 and any("lockdown said no" in m for _, m in said)


def test_no_pairing_found_is_a_note_not_a_problem(local):
    left, said = fix(FakePhone(found=None, saved=4444))
    assert left == 0 and any(k == "note" and "saved port is 4444" in m for k, m in said)

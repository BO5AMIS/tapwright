"""A second `tapwright setup` skips WebDriverAgent when it is installed and its signature lasts."""
import asyncio
from datetime import datetime, timedelta, timezone

from tapwright import cli, repair, store
from tapwright.repair import Expiry

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


def days(n: int) -> datetime:
    return NOW + timedelta(days=n)


def test_a_second_setup_skips_webdriveragent_only_when_it_is_current(monkeypatch):
    runner = "com.x.WebDriverAgentRunner.xctrunner"
    monkeypatch.setattr(store, "load_state", lambda: {"runner_id": runner})
    monkeypatch.setattr(repair, "now_utc", lambda: NOW)
    monkeypatch.setattr(repair, "profile_expiry", lambda: Expiry("profile 'p'", days(200)))
    monkeypatch.setattr(repair, "certificate_expiry", lambda: Expiry("certificate", days(300)))
    monkeypatch.setattr(repair, "recorded_install_expiry", lambda: days(100))
    on_phone = {"yes": True}

    async def installed(lockdown, runner_id):
        return on_phone["yes"]
    monkeypatch.setattr(cli, "wda_on_phone", installed)

    assert asyncio.run(cli.wda_current(None, runner)) is True
    on_phone["yes"] = False
    assert asyncio.run(cli.wda_current(None, runner)) is False  # gone from the phone: install it
    on_phone["yes"] = True
    assert asyncio.run(cli.wda_current(None, "com.other.xctrunner")) is False  # another prefix
    monkeypatch.setattr(repair, "recorded_install_expiry", lambda: days(9))
    assert asyncio.run(cli.wda_current(None, runner)) is False  # its signature ends soon
    monkeypatch.setattr(repair, "recorded_install_expiry", lambda: None)
    assert asyncio.run(cli.wda_current(None, runner)) is False  # never recorded


def test_setup_ends_with_the_next_commands():
    assert "tapwright smoke --app" in cli.NEXT_STEPS and "tapwright install --client claude --apps" in cli.NEXT_STEPS

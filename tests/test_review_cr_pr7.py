"""CodeRabbit's review of PR #7 (1.5.0): four findings, one regression test each."""
import asyncio

import pytest
from fakes import DescribingSession, button, el

from tapwright import journey, session as session_module, store, uxcheck
from tapwright.control import Refused
from tapwright.daemon import Scope
from tapwright.device import DeviceError, Unanswered
from tapwright.session import Session
from test_daemon import finish, make, run, started

APP = "com.example.app"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "HOME", tmp_path / "home")
    monkeypatch.setattr(journey, "reports_dir", lambda: tmp_path / "reports")


# ---- 1. ux_check: another app comes forward after the personal-mode check ----
def test_ux_check_refuses_a_screen_of_an_app_that_came_forward_during_the_read_and_files_nothing(monkeypatch):
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        s.tree = [button("e1", "", "gear", rect=(340, 60, 44, 44))]
        s.foreground = APP
        kept, filed = [], []
        real_check = uxcheck.check
        monkeypatch.setattr(uxcheck, "check", lambda *a, **k: kept.append(a) or real_check(*a, **k))
        monkeypatch.setattr(uxcheck, "tell_journey", lambda *a, **k: filed.append(a))
        real_refresh = s.refresh

        async def refresh_then_switch(timeout=None):
            tree = await real_refresh(timeout)
            s.foreground, s.app = "com.apple.mobilesafari", ("Safari", "com.apple.mobilesafari")  # after _within asked
            return tree
        s.refresh = refresh_then_switch
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "ux_check", {}, scope=Scope((APP,)))
        assert e.value.code == "not_allowed" and "outside_apps" in str(e.value) and "mobilesafari" in str(e.value)
        assert kept == [] and filed == []  # nothing of the other app was checked, remembered or filed
        await finish(stop, task)
    run(go)



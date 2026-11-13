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


# ---- 2. double_tap: the second touch is refused after the first went out ----
def test_a_double_tap_refused_at_the_second_touch_still_advances_the_screen_generation():
    class Linking(DescribingSession):
        async def tap(self, target, hold_ms=60):
            done = await super().tap(target, hold_ms)
            self.foreground, self.app = "com.apple.mobilesafari", ("Safari", "com.apple.mobilesafari")
            return done

    async def go():
        session = Linking()
        session.tree = [button("e1", "Log meal", "log-meal")]
        session.foreground = APP
        d = make(session)
        stop, task = await started(d)
        scope = Scope((APP,))
        await d.run_tool("a", "journey", {"start": True, "goal": "g"}, scope=scope)
        session.tap_points.clear()
        before = d.control.generation
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "journey", {"stress": "double_tap", "target": {"x": 200, "y": 500}}, scope=scope)
        assert e.value.code == "not_allowed" and len(session.tap_points) == 1  # one touch went out
        assert d.control.generation > before  # so every client's refs from before it are stale
        await finish(stop, task)
    run(go)


# ---- 3. a failed version probe is not "an older runner" ----
class Calls:
    def __init__(self, answer):
        self.answer, self.sent = answer, []

    async def __call__(self, method, path, payload=None, timeout=30, again=None):
        self.sent.append(path)
        if isinstance(self.answer, Exception):
            raise self.answer
        return {"value": self.answer}


def probing(answer) -> tuple[Session, Calls]:
    s = Session(None)
    calls = Calls(answer)

    async def ensure():
        return None
    s.ensure, s._call = ensure, calls
    return s, calls


def test_a_version_probe_that_failed_is_raised_and_asked_again_not_cached_as_version_zero():
    s, calls = probing(DeviceError("WebDriverAgent: timeout: no answer"))
    with pytest.raises(DeviceError):
        asyncio.run(s.runner_version())
    assert s.fast_tap is None  # nothing was decided
    calls.answer = {"tapwright": 3}
    assert asyncio.run(s.runner_version()) == 3 and s.fast_tap is True
    s, _ = probing(DeviceError("WebDriverAgent: unknown command: Unhandled endpoint"))  # the route is not there
    assert asyncio.run(s.runner_version()) == 0 and s.fast_tap is False


def test_an_ordinary_touch_still_falls_back_to_w3c_when_the_probe_failed(monkeypatch):
    monkeypatch.setattr(session_module, "FAST_TAP", "1")
    s, _ = probing(DeviceError("WebDriverAgent: timeout: no answer"))
    assert asyncio.run(s._fast_tap(30, 40, 60)) is False  # (no raise: the tap goes on as W3C actions)
    assert s.fast_tap is None  # and the next one asks again


def test_media_answers_a_retryable_error_when_the_version_probe_failed_not_an_older_runner(tmp_path):
    async def go():
        d = make()
        stop, task = await started(d)
        pic = tmp_path / "a.png"
        pic.write_bytes(bytes([0x89]) + b"PNG" + b"x")

        async def failing():
            raise DeviceError("timeout: no answer")
        d.session.runner_version = failing
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "media", {"add": [str(pic)], "confirm": True})
        assert e.value.code == "internal" and "try again" in str(e.value) and "older" not in str(e.value)
        await finish(stop, task)
    run(go)


# ---- 4. media: a write the phone did not answer may have happened ----
def test_media_add_that_got_no_answer_says_it_may_have_happened_and_where_to_look(tmp_path):
    async def go():
        d = make()
        stop, task = await started(d)
        pic = tmp_path / "a.png"
        pic.write_bytes(bytes([0x89]) + b"PNG" + b"x")

        async def unanswered(pictures):
            raise Unanswered("the phone didn't answer within 120 s")
        d.session.photos_add = unanswered
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "media", {"add": [str(pic)], "confirm": True})
        assert e.value.code == "timeout"
        assert "may or may not have happened" in str(e.value) and "Photos > Recently Added" in str(e.value)
        assert "couldn't do it" not in str(e.value)
        await finish(stop, task)
    run(go)

"""Session without a phone: a recording device answers WebDriverAgent's requests."""
import asyncio

import pytest

from tapwright import session as session_module
from tapwright.device import NotSetUp
from tapwright.session import Session


class Recorder:
    def __init__(self):
        self.rsd, self.tunnel, self.runner, self.via = "tunnel", None, None, "USB"
        self.requests, self.started, self.closed = [], [], False

    async def connect(self):
        self.rsd = "tunnel"

    async def mount(self):
        pass

    async def start_wda(self, runner):
        self.started.append(runner)

    async def close(self):
        self.closed = True

    async def http(self, method, path, payload=None, timeout=30.0):
        self.requests.append((method, path, payload))
        if path == "/session":
            return {"sessionId": "S"}
        if path.endswith("/window/size"):
            return {"value": {"width": 430, "height": 932}}
        if path.endswith("/wda/locked"):
            return {"value": True}
        return {"value": None}


@pytest.fixture
def phone(monkeypatch):
    monkeypatch.setattr(session_module.store, "load_state", lambda: {"runner_id": "runner"})
    return Recorder()


def test_session_starts_once_then_costs_nothing(phone):
    s = Session(phone)
    asyncio.run(s.ensure())
    assert s.sid == "S" and s.screen == (430.0, 932.0) and phone.started == ["runner"]
    count = len(phone.requests)
    asyncio.run(s.ensure())
    assert len(phone.requests) == count  # no /status round trip before every call


def test_not_set_up(monkeypatch):
    monkeypatch.setattr(session_module.store, "load_state", lambda: {})
    with pytest.raises(NotSetUp):
        asyncio.run(Session(Recorder()).ensure())


def test_keys_paths_settings_and_lock(phone):
    s = Session(phone)

    async def go():
        await s.key("backspace")
        await s.key("enter")
        await s.path([(0, 10.0, 20.0), (120, 10.0, 220.0)])
        await s.settings({"mjpegServerFramerate": 30})
        return await s.locked()
    assert asyncio.run(go()) is True
    keys = [p for _, path, p in phone.requests if path == "/session/S/wda/keys"]
    assert keys == [{"value": ["\b"]}, {"value": ["\n"]}]
    actions = next(p for _, path, p in phone.requests if path == "/session/S/actions")
    steps = actions["actions"][0]["actions"]
    assert [st["type"] for st in steps] == ["pointerMove", "pointerDown", "pointerMove", "pointerUp"]
    assert steps[2]["duration"] == 120 and (steps[2]["x"], steps[2]["y"]) == (10, 220)
    assert ("POST", "/session/S/appium/settings", {"settings": {"mjpegServerFramerate": 30}}) in phone.requests
    assert s.last_point == (10.0, 20.0)
    with pytest.raises(ValueError):
        asyncio.run(s.key("escape"))


def test_reset_forgets_the_connection(phone):
    s = Session(phone)
    asyncio.run(s.ensure())
    asyncio.run(s.reset())
    assert phone.closed and phone.rsd is None and s.sid is None

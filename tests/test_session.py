"""Session without a phone: a recording device answers WebDriverAgent's requests."""
import asyncio

import pytest

from tapwright import session as session_module
from tapwright.device import ConnectionLost, DeviceError, NotSetUp
from tapwright.session import Session


class Recorder:
    def __init__(self):
        self.rsd, self.tunnel, self.runner, self.via = "tunnel", None, None, "USB"
        self.requests, self.started, self.closed = [], [], False
        self.fail = {}  # path suffix -> exception to raise once
        self.close_error = None

    async def connect(self):
        self.rsd = "tunnel"

    async def mount(self):
        pass

    async def start_wda(self, runner):
        self.started.append(runner)

    async def close(self):
        self.closed = True
        if self.close_error:
            raise self.close_error

    async def http(self, method, path, payload=None, timeout=30.0):
        self.requests.append((method, path, payload))
        for suffix in list(self.fail):
            if path.endswith(suffix):
                raise self.fail.pop(suffix)
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


def test_dead_line_starts_a_new_session_on_the_next_call(phone):
    s = Session(phone)
    asyncio.run(s.ensure())
    phone.fail["/wda/locked"] = ConnectionLost("line is dead")
    with pytest.raises(ConnectionLost):
        asyncio.run(s.locked())
    assert s.sid is None
    assert asyncio.run(s.locked()) is True
    assert phone.started == ["runner", "runner"] and s.sid == "S"


def test_invalid_session_counts_as_a_dead_line(phone):
    s = Session(phone)
    asyncio.run(s.ensure())
    phone.fail["/wda/locked"] = DeviceError("HTTP 404: invalid session id")
    with pytest.raises(ConnectionLost):
        asyncio.run(s.locked())
    assert s.sid is None
    phone.fail["/wda/locked"] = DeviceError("HTTP 500: something else")
    with pytest.raises(DeviceError) as e:
        asyncio.run(s.locked())
    assert not isinstance(e.value, ConnectionLost)


def test_failed_window_size_leaves_no_session(phone):
    s = Session(phone)
    phone.fail["/window/size"] = ConnectionLost("gone")
    with pytest.raises(ConnectionLost):
        asyncio.run(s.ensure())
    assert s.sid is None
    asyncio.run(s.ensure())
    assert s.sid == "S"


def test_reset_clears_everything_even_when_close_fails(phone):
    s = Session(phone)
    asyncio.run(s.ensure())
    phone.tunnel, phone.runner = object(), object()
    phone.close_error = RuntimeError("boom")
    s.items, s.last_point = [object()], (1.0, 2.0)
    asyncio.run(s.reset())
    assert phone.rsd is None and phone.tunnel is None and phone.runner is None
    assert s.sid is None and s.items == [] and s.last_point is None


def test_tap_and_swipe_record_last_point(phone):
    s = Session(phone)
    asyncio.run(s.tap(x=30, y=40))
    assert s.last_point == (30.0, 40.0)
    asyncio.run(s.swipe(5.0, 6.0, 5.0, 200.0))
    assert s.last_point == (5.0, 6.0)

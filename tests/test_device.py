"""device.py without a phone: what counts as a dead line, and the stream connection."""
import asyncio
import importlib
import json
import time

import pytest

import pymobiledevice3.remote.tunnel_service as tunnel_service
import pymobiledevice3.usbmux as usbmux

from tapwright import device as device_module
from tapwright.device import MJPEG_PORT, ConnectionLost, Device, DeviceError, PhoneNotFound


class Conn:
    """A scripted service connection."""

    def __init__(self, response: bytes = b"", fail: Exception | None = None, hang: bool = False):
        self.response, self.fail, self.hang, self.sent, self.closed = response, fail, hang, b"", False

    async def sendall(self, data):
        if self.fail:
            raise self.fail
        self.sent += data

    async def recv_any(self, n=65536):
        if self.hang:
            await asyncio.sleep(3600)
        chunk, self.response = self.response[:n], self.response[n:]
        return chunk

    async def recvall(self, n):
        chunk, self.response = self.response[:n], self.response[n:]
        return chunk

    async def close(self):
        self.closed = True


class Rsd:
    def __init__(self, conns: dict):
        self.conns = conns

    async def create_service_connection(self, port):
        return self.conns[port]


def device(conns: dict) -> Device:
    d = Device(log=lambda *a: None)
    d.rsd = Rsd(conns)
    return d


def test_no_answer_is_a_dead_line():
    conn = Conn(b"")
    with pytest.raises(ConnectionLost):
        asyncio.run(device({8100: conn}).http("GET", "/status"))
    assert conn.closed


def test_a_reset_is_a_dead_line():
    conn = Conn(fail=ConnectionResetError("reset by peer"))
    with pytest.raises(ConnectionLost):
        asyncio.run(device({8100: conn}).http("GET", "/status"))
    assert conn.closed


def test_a_zero_or_negative_timeout_still_gives_the_request_a_chance():
    """A computed deadline of 0 (or less) must not turn a healthy request into a 'dead line'."""
    body = json.dumps({"value": {"ok": True}}).encode()
    conn = Conn(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
    assert asyncio.run(device({8100: conn}).http("GET", "/status", timeout=0))["value"] == {"ok": True}
    conn = Conn(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
    assert asyncio.run(device({8100: conn}).http("GET", "/status", timeout=-0.01))["value"] == {"ok": True}


def test_silence_is_a_dead_line():
    with pytest.raises(ConnectionLost, match="didn't answer"):
        asyncio.run(device({8100: Conn(hang=True)}).http("GET", "/status", timeout=0.1))


def test_an_error_reply_is_not_a_dead_line():
    err = json.dumps({"value": {"error": "no such alert", "message": "none"}}).encode()
    conn = Conn(b"HTTP/1.1 404 Not Found\r\nContent-Length: %d\r\n\r\n" % len(err) + err)
    with pytest.raises(DeviceError, match="no such alert") as e:
        asyncio.run(device({8100: conn}).http("GET", "/alert/text"))
    assert not isinstance(e.value, ConnectionLost) and conn.closed


def test_open_stream_asks_for_the_mjpeg_stream():
    conn = Conn()
    got = asyncio.run(device({MJPEG_PORT: conn}).open_stream())
    assert got is conn and conn.sent == b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n"


def test_failing_to_connect_is_a_dead_line():
    with pytest.raises(ConnectionLost, match="can't reach"):
        asyncio.run(device({}).http("GET", "/status"))


def test_a_non_json_error_reply_is_not_a_dead_line():
    conn = Conn(b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 5\r\n\r\noops!")
    with pytest.raises(DeviceError, match="HTTP 500") as e:
        asyncio.run(device({8100: conn}).http("GET", "/status"))
    assert not isinstance(e.value, ConnectionLost) and conn.closed


def test_a_short_body_is_a_dead_line():
    conn = Conn(b"HTTP/1.1 200 OK\r\nContent-Length: 50\r\n\r\n0123456789")
    with pytest.raises(ConnectionLost, match="short"):
        asyncio.run(device({8100: conn}).http("GET", "/status"))


def test_open_stream_closes_the_connection_when_the_request_fails():
    conn = Conn(fail=ConnectionResetError("reset by peer"))
    with pytest.raises(ConnectionResetError):
        asyncio.run(device({MJPEG_PORT: conn}).open_stream())
    assert conn.closed


class _Ctx:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Info(_Ctx):
    async def proclist(self):
        return []


class _Control(_Ctx):
    async def kill(self, pid):
        pass


class _Config:
    @staticmethod
    async def create_for(rsd, runner_bundle_id):
        return None


class _Service:
    def __init__(self, rsd):
        pass

    async def run(self, cfg, listener=None):
        await asyncio.sleep(3600)


def test_a_new_runner_replaces_the_old_one_cleanly(monkeypatch):
    """iOS can kill the runner (measured: too much CPU) before its test-plan task notices; the
    restart must stop that task, not leave it to fail later with nobody listening."""
    import pymobiledevice3.services.dvt.instruments.device_info as device_info
    import pymobiledevice3.services.dvt.instruments.dvt_provider as dvt_provider
    import pymobiledevice3.services.dvt.instruments.process_control as process_control
    import pymobiledevice3.services.dvt.testmanaged.xcuitest as xcuitest
    monkeypatch.setattr(dvt_provider, "DvtProvider", _Ctx)
    monkeypatch.setattr(device_info, "DeviceInfo", _Info)
    monkeypatch.setattr(process_control, "ProcessControl", _Control)
    monkeypatch.setattr(xcuitest, "TestConfig", _Config)
    monkeypatch.setattr(xcuitest, "XCUITestService", _Service)

    async def scenario():
        d = Device(log=lambda *a: None)
        d.rsd = object()
        answers = iter([False, True])  # the old WebDriverAgent is gone; the new one answers

        async def wda_alive():
            return next(answers)
        d.wda_alive = wda_alive
        old = asyncio.create_task(asyncio.sleep(3600))  # its task hasn't noticed yet
        d.runner = old
        await d.start_wda("runner.id", wait=5)
        assert old.done() and d.runner is not old and not d.runner.done()
        await d.close()

    asyncio.run(scenario())


def test_no_phone_anywhere(monkeypatch):
    async def none(*a, **k):
        return []
    monkeypatch.setattr(usbmux, "list_devices", none)
    monkeypatch.setattr(tunnel_service, "get_remote_pairing_tunnel_services", none)
    with pytest.raises(PhoneNotFound):
        asyncio.run(Device(log=lambda *a: None).connect())


def _fake_phone_side(monkeypatch, service):
    import pymobiledevice3.services.dvt.instruments.device_info as device_info
    import pymobiledevice3.services.dvt.instruments.dvt_provider as dvt_provider
    import pymobiledevice3.services.dvt.instruments.process_control as process_control
    import pymobiledevice3.services.dvt.testmanaged.xcuitest as xcuitest
    monkeypatch.setattr(dvt_provider, "DvtProvider", _Ctx)
    monkeypatch.setattr(device_info, "DeviceInfo", _Info)
    monkeypatch.setattr(process_control, "ProcessControl", _Control)
    monkeypatch.setattr(xcuitest, "TestConfig", _Config)
    monkeypatch.setattr(xcuitest, "XCUITestService", service)


class _Error:
    domain, code = "XCTestErrorDomain", 7


def _failing(method: str):
    class _Failing:
        """A test service that reports `method` of the listener after a moment, then hangs."""
        def __init__(self, rsd):
            pass

        async def run(self, cfg, listener=None):
            await asyncio.sleep(0.05)
            await getattr(listener, method)(_Error())
            await asyncio.sleep(3600)
    return _Failing


async def _start(wait: float, alive=lambda: False) -> float:
    d = Device(log=lambda *a: None)
    d.rsd = object()

    async def wda_alive():
        return alive()
    d.wda_alive = wda_alive
    began = time.monotonic()
    try:
        await d.start_wda("runner.id", wait=wait)
    finally:
        took = time.monotonic() - began
        await d.close()
    return took


@pytest.mark.parametrize("method", ["initialization_for_ui_testing_did_fail", "did_fail_to_bootstrap"])
def test_ui_testing_failing_to_start_ends_the_wait_at_once(monkeypatch, method):
    """The phone's test service can answer initializationForUITestingDidFailWithError on a first start;
    the 60 s wait for /status must not be what notices."""
    _fake_phone_side(monkeypatch, _failing(method))
    began = time.monotonic()
    with pytest.raises(DeviceError, match=r"UI testing failed to start.*XCTestErrorDomain.*7"):
        asyncio.run(_start(30))
    assert time.monotonic() - began < 5


def test_without_the_keyword_the_old_path_still_starts(monkeypatch):
    """If pymobiledevice3's run() stops taking `listener`, WebDriverAgent still starts, just without the hook."""
    class _Plain:
        def __init__(self, rsd):
            pass

        async def run(self, cfg):
            await asyncio.sleep(3600)
    _fake_phone_side(monkeypatch, _Plain)
    asyncio.run(_start(5, alive=lambda: True))


def test_without_the_listener_class_the_old_path_still_starts(monkeypatch):
    import pymobiledevice3.services.dvt.testmanaged.dtx_services as dtx_services
    importlib.import_module("pymobiledevice3.services.dvt.testmanaged.xcuitest")  # while the name still exists
    monkeypatch.delattr(dtx_services, "XCUITestListener")

    class _Plain:
        calls = []

        def __init__(self, rsd):
            pass

        def run(self, cfg, **kwargs):
            self.calls.append(kwargs)
            return asyncio.sleep(3600)
    _fake_phone_side(monkeypatch, _Plain)
    asyncio.run(_start(5, alive=lambda: True))
    assert _Plain.calls == [{}]


def test_a_runner_that_ignores_cancel_does_not_hang_the_stop(monkeypatch):
    monkeypatch.setattr(device_module, "STOP_WAIT", 0.1)

    async def scenario():
        async def stubborn():
            while True:
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    await asyncio.sleep(0.5)
                    return
        d = Device(log=lambda *a: None)
        d.runner = asyncio.create_task(stubborn())
        await asyncio.sleep(0)
        began = time.monotonic()
        await d._stop_runner()
        assert time.monotonic() - began < 0.4 and d.runner is None
    asyncio.run(scenario())


class _PromptInfo(_Ctx):
    """The phone's process list while iOS shows its passcode screen."""
    async def proclist(self):
        return [{"name": "SpringBoard", "foregroundRunning": False},
                {"name": "LocalAuthenticationUIService", "foregroundRunning": True, "isApplication": True,
                 "bundleIdentifier": "com.apple.LocalAuthenticationUIService"}]


def test_a_start_that_waits_for_the_passcode_says_so(monkeypatch):
    """2026-11-10: iOS showed "Enter iPhone Passcode for XCTest / Enable UI Automation"; the start hung for a
    minute, failed, and all anyone was told was "reconnecting"."""
    import pymobiledevice3.services.dvt.instruments.device_info as device_info
    from tapwright import device as device_module
    from tapwright.device import PasscodeNeeded, PhoneLocked
    _fake_phone_side(monkeypatch, _Service)  # the runner starts and never answers
    monkeypatch.setattr(device_info, "DeviceInfo", _PromptInfo)
    monkeypatch.setattr(device_module, "PROMPT_AFTER", 0.2)

    async def scenario():
        d = Device(log=lambda *a: None)
        d.rsd = object()

        async def wda_alive():
            return False
        d.wda_alive = wda_alive
        began = time.monotonic()
        with pytest.raises(PasscodeNeeded, match="asking for its passcode") as e:
            await d.start_wda("runner.id", wait=30)
        assert time.monotonic() - began < 5 and isinstance(e.value, PhoneLocked)
        pending = d.runner
        assert pending is not None and not pending.done()  # left running: it goes on once the passcode is in
        with pytest.raises(PasscodeNeeded):  # the screen is still up: nothing is started over it
            await d.start_wda("runner.id", wait=30)
        assert d.runner is pending and not pending.done()
        pending.cancel()  # the runner gave up (iOS failed the start), and the screen is still up (seen on the phone)
        await asyncio.gather(pending, return_exceptions=True)
        with pytest.raises(PasscodeNeeded):
            await d.start_wda("runner.id", wait=30)
        assert d.runner is pending  # still nothing new was started over it
        monkeypatch.setattr(device_module, "PROMPT_RETRY", 0.0)  # ...until the long retry: the list may be wrong
        with pytest.raises(PasscodeNeeded):
            await d.start_wda("runner.id", wait=30)
        assert d.runner is not pending and not d.runner.done()
        pending = d.runner
        monkeypatch.setattr(device_module, "PROMPT_RETRY", 300.0)

        async def now_alive():
            return True
        monkeypatch.setattr(device_info, "DeviceInfo", _Info)  # the person typed it: the screen is gone
        d.wda_alive = now_alive
        await d.start_wda("runner.id", wait=30)
        assert d.runner is pending and not d.runner.done() and not d.awaiting_passcode  # the waiting start finished
        await d.close()

    asyncio.run(scenario())


def test_a_failed_start_with_no_passcode_screen_keeps_its_own_reason(monkeypatch):
    _fake_phone_side(monkeypatch, _failing("initialization_for_ui_testing_did_fail"))
    with pytest.raises(DeviceError, match="UI testing failed to start") as e:
        asyncio.run(_start(30))
    assert type(e.value) is DeviceError


def test_a_failed_start_behind_the_passcode_screen_is_named(monkeypatch):
    import pymobiledevice3.services.dvt.instruments.device_info as device_info
    from tapwright.device import PasscodeNeeded
    _fake_phone_side(monkeypatch, _failing("initialization_for_ui_testing_did_fail"))
    monkeypatch.setattr(device_info, "DeviceInfo", _PromptInfo)
    with pytest.raises(PasscodeNeeded):
        asyncio.run(_start(30))


def test_the_passcode_screen_is_never_the_app_in_front(monkeypatch):
    import pymobiledevice3.services.dvt.instruments.device_info as device_info
    import pymobiledevice3.services.dvt.instruments.dvt_provider as dvt_provider
    monkeypatch.setattr(dvt_provider, "DvtProvider", _Ctx)
    monkeypatch.setattr(device_info, "DeviceInfo", _PromptInfo)
    d = Device(log=lambda *a: None)
    d.rsd = object()
    assert asyncio.run(d.foreground_apps()) == []
    assert asyncio.run(d.passcode_prompt_up()) is True


def test_a_phone_that_cannot_say_whether_the_passcode_screen_is_up_is_left_waiting(monkeypatch):
    """Not known is not gone: a new start would take the screen away from under the person's fingers."""
    import pymobiledevice3.services.dvt.instruments.device_info as device_info
    from tapwright import device as device_module
    from tapwright.device import PasscodeNeeded
    _fake_phone_side(monkeypatch, _Service)
    monkeypatch.setattr(device_info, "DeviceInfo", _PromptInfo)
    monkeypatch.setattr(device_module, "PROMPT_AFTER", 0.2)

    class _Broken(_Ctx):
        async def proclist(self):
            raise ConnectionResetError("the developer service dropped")

    async def scenario():
        d = Device(log=lambda *a: None)
        d.rsd = object()

        async def wda_alive():
            return False
        d.wda_alive = wda_alive
        with pytest.raises(PasscodeNeeded):
            await d.start_wda("runner.id", wait=30)
        pending = d.runner
        monkeypatch.setattr(device_info, "DeviceInfo", _Broken)
        assert await d.passcode_prompt_up() is None
        with pytest.raises(PasscodeNeeded):
            await d.start_wda("runner.id", wait=30)
        assert d.runner is pending and not pending.done()
        await d.close()

    asyncio.run(scenario())


def test_starts_behind_the_passcode_screen_thin_out_unless_someone_asks_for_the_phone(monkeypatch):
    """Each start launches the test runner, and that cost the app in front a frame gap (Tapline measured 0.25 to
    0.36 s against 0.23 s). After iOS takes the screen away, the first new start comes at once, the next ones
    after a growing pause; a tool call or a viewer (`hurry`) gets one at once."""
    import pymobiledevice3.services.dvt.instruments.device_info as device_info
    from tapwright import device as device_module
    from tapwright.device import PasscodeNeeded
    _fake_phone_side(monkeypatch, _Service)
    monkeypatch.setattr(device_module, "PROMPT_AFTER", 0.05)
    monkeypatch.setattr(device_module, "PROMPT_EVERY", 0.05)
    monkeypatch.setattr(device_module, "PROMPT_PAUSES", (0.0, 60.0, 120.0))

    async def scenario():
        d = Device(log=lambda *a: None)
        d.rsd = object()

        async def wda_alive():
            return False
        d.wda_alive = wda_alive

        async def start(screen_up: bool):
            monkeypatch.setattr(device_info, "DeviceInfo", _PromptInfo)  # each real start puts the screen up
            if not screen_up:
                monkeypatch.setattr(device_info, "DeviceInfo", _GoneThenUp)
            with pytest.raises(PasscodeNeeded):
                await d.start_wda("runner.id", wait=30)
            return d.runner

        class _GoneThenUp(_Ctx):
            """The screen is gone when a start asks first; a start that is made puts it up again."""
            asked = 0

            async def proclist(self):
                type(self).asked += 1
                return [] if type(self).asked <= 1 else await _PromptInfo().proclist()

        first = await start(screen_up=True)
        _GoneThenUp.asked = 0
        second = await start(screen_up=False)  # iOS took the screen away: the first new start comes at once
        assert second is not first
        _GoneThenUp.asked = 0
        monkeypatch.setattr(device_info, "DeviceInfo", _Info)  # gone again, and nobody asks
        with pytest.raises(PasscodeNeeded):
            await d.start_wda("runner.id", wait=30)
        assert d.runner is second  # no new start yet: a minute's pause before the second
        d.hurry()  # a tool call or a viewer
        _GoneThenUp.asked = 0
        third = await start(screen_up=False)
        assert third is not second
        await d.close()

    asyncio.run(scenario())

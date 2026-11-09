"""device.py without a phone: what counts as a dead line, and the stream connection."""
import asyncio
import json

import pytest

import pymobiledevice3.remote.tunnel_service as tunnel_service
import pymobiledevice3.usbmux as usbmux

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

    async def run(self, cfg):
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

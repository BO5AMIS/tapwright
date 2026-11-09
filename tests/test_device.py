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


def test_no_phone_anywhere(monkeypatch):
    async def none(*a, **k):
        return []
    monkeypatch.setattr(usbmux, "list_devices", none)
    monkeypatch.setattr(tunnel_service, "get_remote_pairing_tunnel_services", none)
    with pytest.raises(PhoneNotFound):
        asyncio.run(Device(log=lambda *a: None).connect())

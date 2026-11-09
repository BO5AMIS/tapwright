"""device.py without a phone: what counts as a dead line, and the stream connection."""
import asyncio
import json

import pytest

from tapwright.device import MJPEG_PORT, ConnectionLost, Device, DeviceError


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
    with pytest.raises(ConnectionLost):
        asyncio.run(device({8100: Conn(b"")}).http("GET", "/status"))


def test_a_reset_is_a_dead_line():
    with pytest.raises(ConnectionLost):
        asyncio.run(device({8100: Conn(fail=ConnectionResetError("reset by peer"))}).http("GET", "/status"))


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

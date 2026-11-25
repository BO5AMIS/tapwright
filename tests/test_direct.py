"""The runner's ports (HTTP 8100, MJPEG 9100) reached straight through usbmuxd over USB, the tunnel for the rest
(Device._dial). pymobiledevice3.usbmux is replaced by a fake whose `connect` hands out one end of a socketpair; a
thread on the other end plays WebDriverAgent."""
import asyncio
import json
import socket
import threading

import pytest
from pymobiledevice3 import usbmux
from pymobiledevice3.exceptions import BadDevError, ConnectionFailedError, ConnectionFailedToUsbmuxdError

from tapwright import device as device_module
from tapwright.device import ConnectionLost, Device, MJPEG_PORT, WDA_PORT


def reply(value) -> bytes:
    body = json.dumps({"value": value}).encode()
    return b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body


class Runner:
    """WebDriverAgent as the far end of one socket: answers each request it reads, until the client closes."""

    def __init__(self, sock: socket.socket, value="direct", silent: bool = False, stream: bytes = b""):
        self.sock, self.value, self.silent, self.stream = sock, value, silent, stream
        self.requests: list[bytes] = []
        self.ended = threading.Event()  # the client closed its end
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self) -> None:
        try:
            while True:
                raw = b""
                while b"\r\n\r\n" not in raw:
                    chunk = self.sock.recv(65536)
                    if not chunk:
                        return
                    raw += chunk
                head, _, rest = raw.partition(b"\r\n\r\n")
                length = next((int(h.split(b":")[1]) for h in head.split(b"\r\n") if h.lower().startswith(b"content-length")), 0)
                while len(rest) < length:
                    rest += self.sock.recv(65536)
                self.requests.append(head + b"\r\n\r\n" + rest)
                if self.stream:
                    self.sock.sendall(self.stream)
                elif not self.silent:
                    self.sock.sendall(reply(self.value))
        except OSError:
            pass
        finally:
            self.ended.set()
            self.sock.close()


class FakeMux:
    """One entry of usbmuxd's device list."""

    def __init__(self, devid: int, world: "World"):
        self.devid, self.world = devid, world

    async def connect(self, port: int, usbmux_address=None) -> socket.socket:
        return self.world.connect(self, port)


class World:
    """The fake usbmuxd: what select_device finds and what a connect does."""

    def __init__(self):
        self.devid = 1
        self.lookups: list[tuple] = []
        self.connects: list[tuple[int, int]] = []  # (device id, port)
        self.clients: list[socket.socket] = []
        self.runners: list[Runner] = []
        self.refuse: Exception | None = None  # raised by every connect
        self.lookup_fails: Exception | None = None
        self.runner_args: dict = {}

    async def select_device(self, udid=None, connection_type=None, usbmux_address=None):
        self.lookups.append((udid, connection_type))
        if self.lookup_fails is not None:
            raise self.lookup_fails
        return FakeMux(self.devid, self)

    def connect(self, mux: FakeMux, port: int) -> socket.socket:
        self.connects.append((mux.devid, port))
        if self.refuse is not None:
            raise self.refuse
        ours, theirs = socket.socketpair()
        self.clients.append(ours)
        self.runners.append(Runner(theirs, **self.runner_args))
        return ours


@pytest.fixture
def world(monkeypatch):
    monkeypatch.delenv("TAPWRIGHT_DIRECT", raising=False)
    w = World()
    monkeypatch.setattr(usbmux, "select_device", w.select_device)
    return w


class TunnelConn:
    def __init__(self, answer: bytes):
        self.response, self.sent, self.closed = answer, b"", False

    async def sendall(self, data):
        self.sent += data

    async def recv_any(self, n=65536):
        chunk, self.response = self.response[:n], self.response[n:]
        return chunk

    async def recvall(self, n):
        chunk, self.response = self.response[:n], self.response[n:]
        return chunk

    async def close(self):
        self.closed = True


class Tunnel:
    """The developer tunnel: the connections it opens, by port."""

    def __init__(self, value="tunnel"):
        self.value, self.opened = value, []

    async def create_service_connection(self, port):
        conn = TunnelConn(reply(self.value) if port == WDA_PORT else b"--frame--")
        self.opened.append((port, conn))
        return conn


def phone(via="USB", keep=None) -> tuple[Device, list[str]]:
    lines: list[str] = []
    d = Device(udid="UDID-1", log=lines.append)
    d.via, d.rsd = via, Tunnel()
    if keep is not None:
        d.keep = keep
    return d, lines


def test_over_usb_the_runner_is_reached_through_usbmuxd_not_the_tunnel(world):
    d, lines = phone()
    got = asyncio.run(d.http("GET", "/status"))
    assert got["value"] == "direct" and d.rsd.opened == []
    assert world.connects == [(1, WDA_PORT)]
    assert world.lookups == [("UDID-1", "USB")]  # the USB entry only: a cabled phone on Wi-Fi is listed twice
    assert lines == ["runner reached over usbmuxd directly"]


def test_the_way_is_logged_once_not_for_every_request(world):
    d, lines = phone(keep=False)

    async def go():
        for _ in range(3):
            await d.http("GET", "/status")
    asyncio.run(go())
    assert len(world.connects) == 3 and lines == ["runner reached over usbmuxd directly"]


def test_over_a_network_the_tunnel_carries_it(world):
    d, lines = phone(via="network 10.0.0.2:49152")
    assert asyncio.run(d.http("GET", "/status"))["value"] == "tunnel"
    assert world.lookups == [] and world.connects == []
    assert len(d.rsd.opened) == 1
    assert len(lines) == 1 and lines[0].startswith("runner reached over the tunnel (") and "not USB" in lines[0]


def test_tapwright_direct_zero_keeps_the_tunnel(world, monkeypatch):
    monkeypatch.setenv("TAPWRIGHT_DIRECT", "0")
    d, lines = phone()
    assert asyncio.run(d.http("GET", "/status"))["value"] == "tunnel"
    assert world.lookups == [] and "TAPWRIGHT_DIRECT=0" in lines[0]


@pytest.mark.parametrize("failure", [
    ConnectionFailedToUsbmuxdError("usbmuxd isn't running"),
    OSError("socket broke"),
    BadDevError("unknown device"),
])
def test_when_usbmuxd_fails_the_tunnel_takes_over_and_usbmuxd_is_not_asked_again_at_once(world, failure):
    world.refuse = failure
    d, lines = phone(keep=False)

    async def go():
        return [(await d.http("GET", "/status"))["value"], (await d.http("GET", "/status"))["value"]]
    assert asyncio.run(go()) == ["tunnel", "tunnel"]
    first_round = len(world.connects)
    assert first_round >= 1 and len(d.rsd.opened) == 2  # the second request didn't try usbmuxd again
    assert len(lines) == 1 and "over the tunnel (usbmuxd" in lines[0]


def test_when_the_listing_fails_the_tunnel_takes_over(world):
    world.lookup_fails = OSError("no usbmuxd")
    d, lines = phone()
    assert asyncio.run(d.http("GET", "/status"))["value"] == "tunnel"
    assert "over the tunnel" in lines[0]


def test_a_phone_missing_from_the_listing_falls_back(world, monkeypatch):
    async def none(udid=None, connection_type=None, usbmux_address=None):
        return None
    monkeypatch.setattr(usbmux, "select_device", none)
    d, _ = phone()
    assert asyncio.run(d.http("GET", "/status"))["value"] == "tunnel"


def test_usbmuxd_is_tried_again_after_a_while(world, monkeypatch):
    world.refuse = OSError("broke")
    d, lines = phone(keep=False)

    async def go():
        await d.http("GET", "/status")
        world.refuse = None
        monkeypatch.setattr(device_module, "DIRECT_RETRY", -1.0)
        return (await d.http("GET", "/status"))["value"]
    assert asyncio.run(go()) == "direct"
    assert lines[0].startswith("runner reached over the tunnel") and lines[-1] == "runner reached over usbmuxd directly"


def test_a_refusal_by_the_phone_is_a_lost_connection_not_a_fallback(world):
    """The runner isn't listening yet: start_wda keeps asking (wda_alive) until it answers."""
    world.refuse = ConnectionFailedError("usbmuxd could not connect to port 8100: CONNREFUSED")
    d, lines = phone()
    with pytest.raises(ConnectionLost, match="can't reach WebDriverAgent"):
        asyncio.run(d.http("GET", "/status"))
    assert d.rsd.opened == [] and lines == []
    assert not asyncio.run(d.wda_alive())


def test_a_kept_connection_is_reused_over_the_direct_socket(world):
    d, _ = phone()
    assert d.keep

    async def go():
        return [(await d.http("GET", "/status"))["value"], (await d.http("POST", "/tap", {"x": 1}))["value"],
                (await d.http("GET", "/status"))["value"]]
    assert asyncio.run(go()) == ["direct"] * 3
    assert world.connects == [(1, WDA_PORT)]
    assert len(world.runners[0].requests) == 3
    assert all(b"Connection: keep-alive" in r for r in world.runners[0].requests)


def test_the_kept_direct_connection_is_closed_with_the_device(world):
    d, _ = phone()
    d.tunnel = None

    async def go():
        await d.http("GET", "/status")
        await d.close()
    asyncio.run(go())
    assert world.clients[0].fileno() == -1 and world.runners[0].ended.wait(2)


def test_a_new_tunnel_closes_the_kept_connection(world):
    d, _ = phone()

    async def go():
        await d.http("GET", "/status")
        await d._drop_kept()  # what connect() does first
    asyncio.run(go())
    assert world.clients[0].fileno() == -1 and d._kept is None


def test_a_cancelled_read_closes_the_socket(world):
    world.runner_args = {"silent": True}  # the runner reads the request and never answers
    d, _ = phone(keep=False)
    with pytest.raises(ConnectionLost, match="didn't answer"):
        asyncio.run(d.http("GET", "/status", timeout=0.6))
    assert world.clients[0].fileno() == -1  # not left half-read
    assert world.runners[0].ended.wait(2)


def test_a_cancelled_read_on_the_kept_connection_closes_it_too(world):
    world.runner_args = {"silent": True}
    d, _ = phone()
    with pytest.raises(ConnectionLost):
        asyncio.run(d.http("GET", "/status", timeout=0.6))
    assert world.clients[0].fileno() == -1 and d._kept is None


def test_a_replug_looks_the_phone_up_again(world):
    d, _ = phone(keep=False)

    async def go():
        await d.http("GET", "/status")
        world.devid = 2  # replugged: usbmuxd gave the phone a new id; the old one is refused as unknown
        original = world.connect

        def connect(mux, port):
            if mux.devid == 1:
                raise BadDevError("no such device")
            return original(mux, port)
        world.connect = connect
        return (await d.http("GET", "/status"))["value"]
    assert asyncio.run(go()) == "direct"
    assert [c[0] for c in world.connects] == [1, 2]  # (the refused try with id 1 never reached the recorder)
    assert len(world.lookups) == 2  # the first request's; the stale entry failed, so one more, in the same request
    assert d.rsd.opened == []


def test_the_entry_is_not_looked_up_for_every_request(world):
    d, _ = phone(keep=False)

    async def go():
        for _ in range(4):
            await d.http("GET", "/status")
    asyncio.run(go())
    assert len(world.lookups) == 1


def test_the_stream_opens_over_usbmuxd(world):
    world.runner_args = {"stream": b"--frame--bytes"}
    d, lines = phone()

    async def go():
        conn = await d.open_stream()
        try:
            return await conn.recv_any(262144)
        finally:
            await d._close(conn)
    assert asyncio.run(go()) == b"--frame--bytes"
    assert world.connects == [(1, MJPEG_PORT)] and d.rsd.opened == []
    assert world.runners[0].requests[0].startswith(b"GET / HTTP/1.1")
    assert world.clients[0].fileno() == -1


def test_the_stream_falls_back_to_the_tunnel(world):
    world.refuse = OSError("broke")
    d, _ = phone()

    async def go():
        conn = await d.open_stream()
        return await conn.recv_any(100)
    assert asyncio.run(go()) == b"--frame--"
    assert [p for p, _ in d.rsd.opened] == [MJPEG_PORT]


def test_a_big_answer_comes_whole(world):
    big = "x" * 300_000
    world.runner_args = {"value": big}
    d, _ = phone(keep=False)
    assert asyncio.run(d.http("GET", "/screenshot"))["value"] == big

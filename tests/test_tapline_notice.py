"""Tapwright tells Tapline's hosts that a touch is the agent's (tapline.py; Tapline's `POST /agent-touch`)."""
import asyncio
import json
import socket
import time

from fakes import FakeSession
from tapwright import tapline
from tapwright.control import Controller
from tapwright.daemon import Daemon


class Host:
    """A stand-in for one Tapline host: records each request's headers and body, answers 204."""

    def __init__(self) -> None:
        self.requests: list[tuple[dict, dict]] = []
        self.server = None
        self.port = 0

    async def start(self) -> "Host":
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def _serve(self, reader, writer) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        lines = head.split("\r\n")
        headers = {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)}
        body = await reader.readexactly(int(headers.get("content-length", "0")))
        self.requests.append(({"line": lines[0], **headers}, json.loads(body)))
        writer.write(b"HTTP/1.1 204 No Content\r\n\r\n")
        await writer.drain()
        writer.close()

    async def stop(self) -> None:
        self.server.close()
        await self.server.wait_closed()


def free_port() -> int:
    """A port with nothing listening on it (taken and released at once)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_a_touch_is_announced_to_every_host_with_the_token(tmp_path, monkeypatch):
    (tmp_path / "token").write_text("secret-token\n", encoding="utf-8")
    monkeypatch.setenv("TAPLINE_HOME", str(tmp_path))

    async def go():
        a, b = await Host().start(), await Host().start()
        monkeypatch.setenv("TAPLINE_PORTS", f"{a.port},{free_port()},{b.port}")  # the middle one: no host there
        began = time.monotonic()
        await tapline.AgentTouches().notify(60)
        took = time.monotonic() - began
        await a.stop()
        await b.stop()
        return a, b, took
    a, b, took = asyncio.run(go())
    for host in (a, b):
        (headers, body), = host.requests
        assert headers["line"] == "POST /agent-touch HTTP/1.1"
        assert headers["authorization"] == "Bearer secret-token"
        assert body == {"ms": 60, "by": "tapwright"}
    assert took < tapline.NOTICE_WAIT + 0.2  # a port with no host costs nothing


def test_without_a_token_nothing_is_sent(tmp_path, monkeypatch):
    monkeypatch.setenv("TAPLINE_HOME", str(tmp_path))  # no token file: no Tapline on this computer

    async def go():
        host = await Host().start()
        monkeypatch.setenv("TAPLINE_PORTS", str(host.port))
        await tapline.AgentTouches().notify(60)
        await host.stop()
        return host
    assert asyncio.run(go()).requests == []


def test_a_host_that_never_answers_holds_the_touch_up_no_longer_than_the_wait(tmp_path, monkeypatch):
    (tmp_path / "token").write_text("t", encoding="utf-8")
    monkeypatch.setenv("TAPLINE_HOME", str(tmp_path))

    async def go():
        async def silent(reader, writer):
            await asyncio.sleep(5)
        server = await asyncio.start_server(silent, "127.0.0.1", 0)
        monkeypatch.setenv("TAPLINE_PORTS", str(server.sockets[0].getsockname()[1]))
        began = time.monotonic()
        await tapline.AgentTouches().notify(60)
        took = time.monotonic() - began
        server.close()
        return took
    assert asyncio.run(go()) < tapline.NOTICE_WAIT + 0.2


def test_the_length_is_kept_within_what_tapline_accepts(tmp_path, monkeypatch):
    (tmp_path / "token").write_text("t", encoding="utf-8")
    monkeypatch.setenv("TAPLINE_HOME", str(tmp_path))

    async def go():
        host = await Host().start()
        monkeypatch.setenv("TAPLINE_PORTS", str(host.port))
        notice = tapline.AgentTouches()
        await notice.notify(-5)
        await notice.notify(60_000)
        await host.stop()
        return [body["ms"] for _, body in host.requests]
    assert asyncio.run(go()) == [0, tapline.MAX_MS]


def test_the_agents_touches_are_announced_and_a_persons_are_not(tmp_path, monkeypatch):
    """Inside an agent's action every touch is announced, the keyboard-closing one included; a person's touch from
    the live view never is (it is a person's, and Tapline must say so)."""
    (tmp_path / "token").write_text("t", encoding="utf-8")
    monkeypatch.setenv("TAPLINE_HOME", str(tmp_path))

    async def go():
        host = await Host().start()
        monkeypatch.setenv("TAPLINE_PORTS", str(host.port))
        s = FakeSession()
        real_tap = s.tap

        async def tap(target, hold_ms=60):  # the fake session announces as the real one does
            if s.touch_hook is not None:
                await s.touch_hook(hold_ms)
            return await real_tap(target, hold_ms)
        s.tap, s.touch_hook = tap, None
        clock_controller = Controller()
        d = Daemon(s, control=clock_controller, log=lambda *a: None, heartbeat=0.02, backoff=(0.01,), ready_wait=2.0,
                   stream_linger=0, stream_retry=0.01, settle_fallback=0.0, pace_tick=None)
        stop = asyncio.Event()
        task = asyncio.create_task(d.supervise(stop))
        for _ in range(300):
            if d.state == "live":
                break
            await asyncio.sleep(0.01)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False})
        assert len(host.requests) == 1 and s.touch_hook is None  # set only while the agent acts
        before = len(s.actions())
        assert await d.viewer_message({"type": "gesture", "points": [[0, 0.5, 0.5], [60, 0.5, 0.5]]}) is None
        await asyncio.sleep(0.05)
        assert len(s.actions()) > before and len(host.requests) == 1  # a person's tap reached the phone, unannounced
        stop.set()
        await asyncio.wait_for(task, 2)
        await host.stop()
    asyncio.run(go())

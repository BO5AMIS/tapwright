"""Working beside Tapline: before each of an agent's real touches, Tapwright tells every Tapline host on this computer
that the touch is the agent's, so Tapline neither says "a person used the app" nor makes its own refs stale.

Tapline's side (its README, "for other tools"): `POST http://127.0.0.1:<port>/agent-touch` with `Authorization: Bearer
<token>` and `{"ms": N}`, N the touch's own length in ms (0 to 10000). Touch reports from the app that arrive from
then until N ms + 1.5 s later are the agent's. Each host listens on the first free port of 7210-7219 (TAPLINE_PORTS
overrides the list); the token is `~/.tapline/token` or `$TAPLINE_HOME/token`, one per computer. It is the token
that pairs a phone: it is read here, sent only to 127.0.0.1, and never printed or logged.

Without a token file there is no Tapline on this computer and nothing is sent. A port that refuses the connection
has no host. The notices go out together and are waited for at most NOTICE_WAIT seconds: a touch is never held up
longer than that, and a host that answers late still got the notice in time."""
import asyncio
import json
import os
from pathlib import Path

PORTS = range(7210, 7220)
NOTICE_WAIT = 0.3  # seconds, at most, before the touch goes out anyway (Tapline asks for about 300 ms)
MAX_MS = 10_000


def ports() -> list[int]:
    """The ports to tell: TAPLINE_PORTS (comma-separated) when set, else Tapline's 7210-7219."""
    given = os.environ.get("TAPLINE_PORTS", "").strip()
    if given:
        return [int(p) for p in given.split(",") if p.strip().isdigit()]
    return list(PORTS)


def token_path() -> Path:
    home = os.environ.get("TAPLINE_HOME")
    return (Path(home) if home else Path.home() / ".tapline") / "token"


class AgentTouches:
    """Tells Tapline's hosts about the agent's touches. The token is read once and again when its file changes."""

    def __init__(self) -> None:
        self._token: str | None = None
        self._stamp: tuple[float, int] | None = None  # the token file's (mtime, size) when it was read

    def token(self) -> str | None:
        path = token_path()
        try:
            stat = path.stat()
        except OSError:
            self._token, self._stamp = None, None
            return None
        stamp = (stat.st_mtime, stat.st_size)
        if stamp != self._stamp:
            try:
                self._token = path.read_text(encoding="utf-8").strip() or None
            except OSError:
                self._token = None
            self._stamp = stamp
        return self._token

    async def notify(self, ms: float) -> None:
        """Tell every host that the agent touches the phone now, for `ms` milliseconds. Never raises."""
        token = self.token()
        if token is None:
            return
        body = json.dumps({"ms": int(max(0, min(MAX_MS, ms))), "by": "tapwright"}).encode()
        request = (f"POST /agent-touch HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {token}\r\n"
                   f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                   ).encode() + body
        tasks = [asyncio.create_task(_send(port, request)) for port in ports()]
        if not tasks:
            return
        _, late = await asyncio.wait(tasks, timeout=NOTICE_WAIT)
        for task in late:  # a host that hasn't answered yet has the request; its answer isn't needed
            task.cancel()


async def _send(port: int, request: bytes) -> None:
    writer = None
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(request)
        await writer.drain()
        await reader.read(64)  # the status line: the request was taken
    except (OSError, asyncio.IncompleteReadError):
        pass  # no host on this port
    finally:
        if writer is not None:
            writer.close()

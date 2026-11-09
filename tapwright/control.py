"""Who acts on the phone, and when: one action at a time, a person before an agent, a pause
switch, and screen generations that tell an agent its element refs went stale. Also turns a
person's pointer path from the live view into a tap, long press or drag. Pure logic: no
phone, no network."""
import asyncio
import math
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

PERSON_IDLE_S = 3.0  # a person counts as driving until idle this long
AGENT_MAX_WAIT_S = 30.0  # an agent action waits at most this long for a person to finish
LONG_PRESS_MS = 500
TAP_MOVE_PT = 10.0
PATH_POINTS = 20  # a drag is replayed through at most this many points


class Refused(Exception):
    """An action not run, with a machine-readable code (docs/VIEWER-PROTOCOL.md)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class ClientView:
    """What one agent client knows: the generation of its last look or own action, and its elements."""
    seen: int = -1
    items: list = field(default_factory=list)
    read_at: float = float("-inf")  # the clock when `items` was read
    moved: bool = False  # an action that returned no list ran since: the screen has probably changed
    dropped: bool = False  # the connection dropped since `items` was read
    shown: list = field(default_factory=list)  # the list as of the agent's last answer: what changes are measured against
    top_ref: int = 0  # the highest ref number this agent was given: numbers are never reused until a full list
    app: tuple[str, str] | None = None  # (name, bundle id) of the foreground app at the last read
    alert: tuple[str, tuple[str, ...]] | None = None  # (title, buttons) of the OS alert at the last answer
    keyboard: bool = False  # whether the keyboard was shown at the last answer


class Controller:
    def __init__(self, clock=time.monotonic, sleep=asyncio.sleep) -> None:
        self.clock, self.sleep = clock, sleep
        self.paused = False
        self.generation = 0  # +1 after every action that may have changed the screen
        self.person_generation = -1  # the generation a person's last action produced
        self.clients: dict[str, ClientView] = {}
        self._lock = asyncio.Lock()
        self._person_at: float | None = None
        self._people_waiting = 0  # people queued for the lock: agents step aside while any wait
        self._agent_at: float | None = None
        self._agent_running = False

    def client(self, client_id: str) -> ClientView:
        return self.clients.setdefault(client_id, ClientView())

    def _recent(self, at: float | None) -> bool:
        return at is not None and self.clock() - at < PERSON_IDLE_S

    def person_active(self) -> bool:
        return self._people_waiting > 0 or self._recent(self._person_at)

    @property
    def driver(self) -> str | None:
        if self.person_active():
            return "person"
        if self._agent_running or self._recent(self._agent_at):
            return "agent"
        return None

    def set_paused(self, on: bool) -> None:
        self.paused = bool(on)

    async def person(self, fn):
        """A person's action: runs as soon as the current action ends, ahead of waiting agents."""
        self._person_at = self.clock()
        self._people_waiting += 1
        try:
            await self._lock.acquire()
        finally:
            self._people_waiting -= 1
        try:
            return await fn()
        finally:
            self.generation += 1
            self.person_generation = self.generation
            self._person_at = self.clock()
            self._lock.release()

    async def agent(self, fn):
        """An agent's action: refused while paused, waits while a person drives."""
        start = self.clock()
        while True:
            if self.paused:
                raise Refused("paused", "paused by the user in the live view; ask them to resume")
            if self.person_active():
                if self.clock() - start >= AGENT_MAX_WAIT_S:
                    raise Refused("busy", "a person is using the phone; try again in a moment")
                await self.sleep(0.25)
                continue
            async with self._lock:
                if self.paused or self.person_active():
                    continue  # a person got in first: let go of the queue and wait again
                self._agent_running = True
                refused = False
                try:
                    return await fn()
                except Refused:
                    refused = True
                    raise
                finally:
                    self._agent_running = False
                    if not refused:
                        self.generation += 1
                        self._agent_at = self.clock()

    @asynccontextmanager
    async def exclusive(self):
        """Work that must not overlap any action or read: reconnecting."""
        async with self._lock:
            yield

    async def read(self, fn):
        """A read (a look, the app list, settings): queued with actions, never refused or delayed."""
        async with self._lock:
            return await fn()


@dataclass(frozen=True)
class Gesture:
    kind: str  # "tap", "longPress" or "drag"
    points: tuple  # ((t_ms from 0, x, y), ...) in points
    hold_ms: int = 0


def to_points(path, screen: tuple[float, float]) -> list[tuple[int, float, float]]:
    """[[t_ms, x, y], ...] with x, y in 0..1 of the frame -> (t_ms from 0, x, y) in points."""
    if not path:
        raise ValueError("a gesture needs at least one point")
    w, h = screen
    out: list[tuple[float, float, float]] = []
    for point in path:
        try:
            t, x, y = (float(v) for v in point)
        except (TypeError, ValueError) as e:
            raise ValueError("each gesture point must be [t_ms, x, y] numbers") from e
        if not all(math.isfinite(v) for v in (t, x, y)):
            raise ValueError("gesture numbers must be finite")
        if out:
            t = max(t, out[-1][0])  # times never go backwards
        out.append((t, min(max(x, 0.0), 1.0) * w, min(max(y, 0.0), 1.0) * h))
    t0 = out[0][0]
    return [(int(round(t - t0)), x, y) for t, x, y in out]


def thin(points: list, limit: int) -> list:
    """At most `limit` points, the first and last kept, evenly spread between."""
    if len(points) <= limit:
        return list(points)
    step = (len(points) - 1) / (limit - 1)
    return [points[round(i * step)] for i in range(limit)]


def classify(path, screen: tuple[float, float]) -> Gesture:
    """A pointer path from the live view as a tap, a long press, or a drag along the path."""
    pts = to_points(path, screen)
    _, x0, y0 = pts[0]
    moved = max(math.hypot(x - x0, y - y0) for _, x, y in pts)
    duration = pts[-1][0]
    if moved < TAP_MOVE_PT:
        if duration >= LONG_PRESS_MS:
            return Gesture("longPress", (pts[0],), hold_ms=duration)
        return Gesture("tap", (pts[0],), hold_ms=60)
    return Gesture("drag", tuple(thin(pts, PATH_POINTS)))

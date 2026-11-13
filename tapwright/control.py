"""Who acts on the phone, and when: one action at a time, a person before an agent, a pause
switch, and screen generations that tell an agent its element refs went stale. Also turns a
person's pointer path from the live view into a tap, long press or drag. Pure logic: no
phone, no network."""
import asyncio
import contextvars
import math
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import ui
    from .stream import Frame

PERSON_IDLE_S = 3.0  # a person counts as driving until idle this long
AGENT_MAX_WAIT_S = 30.0  # an agent action waits at most this long for a person to finish
LONG_PRESS_MS = 500
# A tap's finger rests this long: the result showed 75 ms sooner than with 60 ms, and the call took as long
# (an iPhone 14 Pro Max, iOS 26.5, 2026-11-09). A person's tap is over when it reaches the phone: replayed quick.
# Through the live view a 20 ms tap showed in 0.27-0.30 s and 60 ms in 0.30-0.38 s; the button's press highlight
# landed in a frame about as often (6 and 4 of 10), so persons get 20 ms too.
TAP_MS = 20
TAP_MOVE_PT = 10.0
PATH_POINTS = 20  # a drag is replayed through at most this many points


@dataclass
class _Turn:
    """One agent action's hold on the queue: `held` is False while it has let go (Controller.aside)."""
    held: bool = True


# the running agent action's turn (set by Controller.agent for the call it runs)
_TURN: contextvars.ContextVar["_Turn | None"] = contextvars.ContextVar("tapwright_turn", default=None)
# Is the call's caller still waiting for its answer? Set by the HTTP API for the call it runs (server.py: the client has
# not disconnected); None (MCP over stdio, the daemon's own calls): always. An action that waited for its turn checks it
# as the turn comes, and does nothing for a caller that left (it gave up waiting: nobody would see what was done).
STILL_WANTED: contextvars.ContextVar["Callable[[], Awaitable[bool]] | None"] = contextvars.ContextVar(
    "tapwright_still_wanted", default=None)
CALLER_LEFT = "the caller left before the phone was free; nothing was done"


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
    back_before: "tuple | None" = None  # `back` with look false: (the newest frame, the tree's signature) before its touch
    read_at: float = float("-inf")  # the clock when `items` was read
    moved: bool = False  # an action that returned no list ran since: the screen has probably changed
    ocr: dict[str, tuple[float, float]] = field(default_factory=dict)  # ref (o1…) -> centre, from the last OCR reading
    unlooked: int = 0  # how many answers since the last list or change block carried none (`look: false`)
    shown_off: list = field(default_factory=list)  # what lay off screen at the agent's last answer (for `~` lines)
    dropped: bool = False  # the connection dropped since `items` was read
    opened: bool = False  # open_app made the last list: a screen still drawing its last parts (a floating search field)
    shown: list = field(default_factory=list)  # the list as of the agent's last answer: what changes are measured against
    top_ref: int = 0  # the highest ref number this agent was given: numbers are never reused until a full list
    app: tuple[str, str] | None = None  # (name, bundle id) of the foreground app at the last read
    alert: tuple[str, tuple[str, ...]] | None = None  # (title, buttons) of the OS alert at the last answer
    keyboard: bool = False  # whether the keyboard was shown at the last answer
    read_keyboard: bool = False  # whether the keyboard showed on the newest read of any kind (answers and silent reads)
    alert_now: tuple[str, tuple[str, ...]] | None = None  # (title, buttons) of the OS alert on the newest read of any kind
    read_frame: "Frame | None" = None  # the stream frame at that moment: the picture that matches the list
    read_mark: tuple[int, "Frame | None"] = (0, None)  # (seq, the newest stream frame) when `items` was read
    place: tuple[str, str] | None = None  # (bundle id, place name) the agent's last answer put the app at (AGENT-TOOLS §18.1)
    typed_at: tuple[str, str] | None = None  # the place the agent typed text at: a move from there isn't a road (§18.2)
    place_seen: int = -1  # the screen generation `place` was worked out at: a person's input since makes it unknown
    tree: "ui.Tree | None" = None  # the tree of the agent's newest answer's read, for the place it shows


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

    async def _take_turn(self) -> None:
        """The queue's turn for an agent: refused while paused, waits while a person drives (busy after
        AGENT_MAX_WAIT_S). Returns holding the lock."""
        start = self.clock()
        while True:
            if self.paused:
                raise Refused("paused", "paused by the user in the live view; ask them to resume")
            if self.person_active():
                if self.clock() - start >= AGENT_MAX_WAIT_S:
                    raise Refused("busy", "a person is using the phone; try again in a moment")
                await self.sleep(0.25)
                continue
            await self._lock.acquire()
            if self.paused or self.person_active():
                self._lock.release()  # a person got in first: let go of the queue and wait again
                continue
            wanted = STILL_WANTED.get()
            try:
                left = wanted is not None and not await wanted()
            except BaseException:
                self._lock.release()
                raise
            if left:
                self._lock.release()
                raise Refused("timeout", CALLER_LEFT)
            return

    async def agent(self, fn):
        """An agent's action: refused while paused, waits while a person drives."""
        await self._take_turn()
        turn = _Turn()
        token = _TURN.set(turn)
        self._agent_running = True
        refused = False
        try:
            return await fn()
        except Refused:
            refused = True
            raise
        finally:
            _TURN.reset(token)
            if turn.held:  # (an action that let go of its turn and couldn't take it back holds nothing: see aside)
                self._agent_running = False
                if not refused:
                    self.generation += 1
                    self._agent_at = self.clock()
                self._lock.release()

    def holds_turn(self) -> bool:
        """Is the code running now inside an agent action that holds the queue's turn? (False outside any action.)"""
        turn = _TURN.get()
        return turn is not None and turn.held

    async def aside(self, wait):
        """Inside an agent's action: let go of the queue's turn while `wait()` waits for a person's answer (the
        policy's OK, AGENT-TOOLS 11.3), so the live view can pause the phone or use it meanwhile, then take the turn
        back the way an action takes it: refused (`paused`) while paused, after a person who drives (`busy` after
        AGENT_MAX_WAIT_S). Anything may have happened on the phone meanwhile, so the caller checks its touch again
        before it makes it. When `wait` raises, or the turn can't be taken back, the action ends without a turn.
        Outside an action (nothing to let go of), `wait()` just runs."""
        turn = _TURN.get()
        if turn is None or not turn.held:
            return await wait()
        turn.held = False
        self._agent_running = False
        self._lock.release()
        result = await wait()
        await self._take_turn()
        turn.held = True
        self._agent_running = True
        return result

    def touched(self) -> None:
        """A touch was delivered inside the running agent action, and the action may still end as refused (a journey's
        second touch of a double tap): `agent` then skips its own +1, so the screen's generation moves here, and
        every client's refs from before the touch are stale (CodeRabbit, PR #7)."""
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
        return Gesture("tap", (pts[0],), hold_ms=TAP_MS)
    return Gesture("drag", tuple(thin(pts, PATH_POINTS)))

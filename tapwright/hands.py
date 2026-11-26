"""Hands: touches, hardware buttons and keys sent from the computer through CoreDevice HID (SPEC-2.0 D1).

The phone's `dtuhidd` (on the developer disk image) takes HID reports over the daemon's own tunnel (`Device.rsd`):
the touchscreen surface (`_ServiceID` 257) for fingers, the Indigo service for buttons, a keyboard surface for keys.
No XCTest, no runner and no display stream: a report is acked in about 1.3 ms, a 50 ms tap in 51 ms (Phase 0, iOS 26.5).
On iOS 27 and later the phone drops reports unless a display stream runs, so there hands count as unavailable until the
(future) eyes lane sets `stream_ok`.

Safety rules, all here:
- A finger, key or button that went down is always let go: the release goes out in `finally`, shielded from
  cancellation. If it can't be sent, the Hands are `dirty`: the next open sends that release before anything else.
- Nothing is ever sent twice: a report whose send failed is not sent again, and after a failed send the connection is
  dropped (the next gesture opens a new one).
- One gesture at a time (a lock). Timing is by `asyncio.sleep` against the loop's monotonic clock, so a long path doesn't
  drift.
- `HandsUnavailable` is raised only before anything was sent: the caller may then use the runner instead. Once a report
  may have gone out, a failure is a `HandsError` (an `Unanswered`: sent, outcome unknown), also when the HID connection
  dropped (never a `ConnectionLost`: that would rebuild the runner too), and never a reason to touch again some other
  way. Reports get no answer from the phone: "sent" means written to the tunnel, and a report the phone drops silently
  is not noticed here (the daemon's read afterwards shows what happened).

Coordinates: callers give screen points as the session has them (`screen` is its size now, `turn` is `Session.turn`);
`to_hid` maps them onto the portrait panel's 0..65535 (measured on the phone for both landscape sides, 2026-11-26).
`TAPWRIGHT_HANDS_LANDSCAPE=0` leaves landscape to the runner."""
import asyncio
import contextlib
import math
import os
from typing import Any, Awaitable, Callable, NamedTuple, Sequence

from . import device as device_module
from .device import Unanswered

LANDSCAPE = os.environ.get("TAPWRIGHT_HANDS_LANDSCAPE", "1") != "0"
UNIVERSAL_SERVICE = "com.apple.coredevice.hid.universalhidservice"
INDIGO_SERVICE = "com.apple.coredevice.hid.indigo"
TOUCHSCREEN = 257  # the main touchscreen surface's _ServiceID
KEYBOARD_READY = 512  # a keyboard surface already registered on the phone (listed on the test phone); else one is made
MIN_IOS = 18  # CoreDevice's HID services come with iOS 17's developer tunnel; proven on 26.5, trusted from 18
STREAM_IOS = 27  # from here on the phone drops HID reports unless a display stream runs (see stream_ok)
STEP_MS = 12.0  # a contact report goes out at least this often while a finger moves (8-16 ms is a real digitizer's pace)
OPEN_WAIT = 5.0  # seconds opening the services may take (a second tunnel's services opened in well under one)
SEND_WAIT = 2.0  # seconds one report may take to be acked (1.3 ms measured)
# Seconds a finger, key or button that may still be down keeps touches off the runner too (R200 M2). Short: measured on
# the phone (2026-11-26, Calculator) the phone lifts a finger by itself when its HID connection closes (the key it was
# on was entered), and a failed release closes the connection.
DIRTY_HOLD = 5.0
RETRY_OPEN = 30.0  # seconds before an open that failed for a passing reason is tried again on the same tunnel
# Touches aimed at an element, in a row, that changed nothing before the connection is doubted (Hands.judged). Seen on
# the phone (2026-11-26, twice): a whole session of touches acked in 60-110 ms that did nothing, with nothing logged;
# the start after it worked, and so did the next 8.
DOUBT_AFTER = 3
# Seconds touches go through the runner after a connection that was opened again for doubt changed nothing either
# (2.0.2: was for the life of the tunnel, which on an always-on service is for good; a row of touches that change
# nothing by right, a button that does nothing, must not cost the hands for days)
RUNNER_FOR = 600.0
RUNNER_MAX = 6 * 3600.0  # each time on the runner is twice the one before, up to this
BUTTON_MS = 50  # how long a button is held (Phase 0: 52 ms a press)
KEY_GAP_MS = 12  # between two keyboard reports (15 characters in 396 ms, measured)
LEFT_SHIFT = 0xE1
CONSUMER = 0x0C  # HID usage page of the phone's buttons
BUTTON_DOWN, BUTTON_UP = 1, 2  # IndigoButtonEvent states
# name -> (usage page, usage, hold ms)
BUTTONS = {"home": (CONSUMER, 0x40, BUTTON_MS), "lock": (CONSUMER, 0x30, BUTTON_MS),
           "volume_up": (CONSUMER, 0xE9, BUTTON_MS), "volume_down": (CONSUMER, 0xEA, BUTTON_MS),
           "mute": (CONSUMER, 0xE2, BUTTON_MS), "siri": (CONSUMER, 0xCF, 1000)}
KEYS = {"enter": 0x28, "escape": 0x29, "backspace": 0x2A, "tab": 0x2B,
        "right": 0x4F, "left": 0x50, "down": 0x51, "up": 0x52}
SINGLE = 2  # the contact identity of a one-finger touch (what Xcode's own reports use: state 0xC2)
FINGER_IDS = (1, 2, 3, 4, 0)  # identities of a multi-finger touch, in order (0 last: its release slot is all zeros)
TURNS = (0, 90, 270)


class HandsUnavailable(Exception):
    """Hands can't be used for this (nothing was sent): the caller uses the runner instead. `reason` says why."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class HandsError(Unanswered):
    """Reports went out (or may have), then sending failed without the link dropping: whether the gesture was played is
    not known. Never touched again another way; the agent looks first (the Unanswered contract)."""


class Untypable(ValueError):
    """A character the HID keyboard can't type (US-ASCII only). Raised before any key is sent."""

    def __init__(self, char: str, index: int) -> None:
        super().__init__(f"can't type {char!r} (character {index + 1}) with the HID keyboard: US-ASCII only")
        self.char, self.index = char, index


class Contact(NamedTuple):
    """One finger in a touchscreen report (pymobiledevice3's TouchscreenContact has the same fields)."""
    identity: int
    x: int
    y: int
    touching: bool = True


class _SendFailed(Exception):
    def __init__(self, cause: BaseException, timed_out: bool = False) -> None:
        super().__init__(str(cause))
        self.cause, self.timed_out = cause, timed_out


Before = Callable[[], Awaitable[None]] | None
Point = tuple[float, float, float]  # (t_ms, x, y) in screen points


def to_hid(x: float, y: float, screen: tuple[float, float], turn: int = 0) -> tuple[int, int]:
    """A point on the screen as the session has it now (`screen` = its size in points now, `turn` = Session.turn) ->
    the portrait panel's HID coordinates, 0..65535 each (top-left origin of the panel held upright).

    HID coordinates always address the physical portrait panel (W x H points, W < H). In landscape the screen is H x W:
      turn 0:   px = x,     py = y
      turn 90:  px = y,     py = H - x   (landscape_right, runner rotation z = 90)
      turn 270: px = W - y, py = x       (landscape_left,  runner rotation z = 270)
    Both landscape cases were checked on the phone (2026-11-26, Calculator's "7" key, each side). Points off the screen
    are clamped to its edge."""
    if turn not in TURNS:
        raise ValueError(f"turn must be one of {TURNS}, not {turn!r}")
    w, h = min(screen), max(screen)
    if w <= 0:
        raise ValueError(f"screen size must be positive, not {screen!r}")
    if turn == 0:
        px, py = x, y
    elif turn == 90:
        px, py = y, h - x
    else:
        px, py = w - y, x

    def scale(v: float, size: float) -> int:
        return round(min(1.0, max(0.0, v / size)) * 65535)
    return scale(px, w), scale(py, h)


def timeline(points: Sequence[Point], step: float = STEP_MS) -> list[Point]:
    """A finger path [(t_ms, x, y), ...] filled in so no two points are more than `step` ms apart (straight lines
    between the given points, at their own pace), with its times counted from its first point. A point at or before the
    one before it comes right after it."""
    if not points:
        raise ValueError("a path needs at least one point")
    t0 = float(points[0][0])
    out: list[Point] = [(0.0, float(points[0][1]), float(points[0][2]))]
    for (ta, xa, ya), (tb, xb, yb) in zip(points, points[1:]):
        dt = float(tb) - float(ta)
        n = max(1, math.ceil(dt / step)) if dt > 0 else 1
        for i in range(1, n + 1):
            f = i / n
            out.append((float(ta) - t0 + max(dt, 0.0) * f, xa + (xb - xa) * f, ya + (yb - ya) * f))
    return out


def multi_frames(paths: Sequence[Sequence[Point]], step: float = STEP_MS) -> list[tuple[float, list[tuple[int, float, float, bool]]]]:
    """The reports of a several-finger gesture: [(t_ms, [(finger, x, y, touching), ...]), ...]. Each finger goes down at
    its first point, moves through its points at their times and lifts at its last (reported once more, not touching,
    in the next report); the last report lifts every finger still down. Times count from the earliest first point."""
    if not 1 <= len(paths) <= len(FINGER_IDS):
        raise ValueError(f"1 to {len(FINGER_IDS)} fingers, not {len(paths)}")
    start = min(float(p[0][0]) for p in paths)
    filled = [[(t + float(p[0][0]) - start, x, y) for t, x, y in timeline(p, step)] for p in paths]
    times = sorted({t for f in filled for t, _, _ in f})
    state = ["up"] * len(filled)  # up (not down yet), down, lifted
    frames: list[tuple[float, list[tuple[int, float, float, bool]]]] = []
    for t in times:
        slots: list[tuple[int, float, float, bool]] = []
        for i, f in enumerate(filled):
            first, last = f[0][0], f[-1][0]
            if t < first or state[i] == "lifted":
                continue
            if t <= last:
                state[i] = "down"
                slots.append((i, *_at(f, t), True))
            elif state[i] == "down":
                state[i] = "lifted"
                slots.append((i, f[-1][1], f[-1][2], False))
        if slots:
            frames.append((t, slots))
    still = [(i, f[-1][1], f[-1][2], False) for i, f in enumerate(filled) if state[i] == "down"]
    if still:
        frames.append((times[-1], still))
    return frames


def _at(path: Sequence[Point], t: float) -> tuple[float, float]:
    """Where a filled path's finger is at time t (straight between its points)."""
    for (ta, xa, ya), (tb, xb, yb) in zip(path, path[1:]):
        if ta <= t <= tb:
            f = 0.0 if tb == ta else (t - ta) / (tb - ta)
            return xa + (xb - xa) * f, ya + (yb - ya) * f
    return path[-1][1], path[-1][2]


def plan_keys(text: str) -> list[tuple[int, bool]]:
    """(usage, shift) for each character of `text` on a US keyboard; Untypable names the first one there isn't."""
    from pymobiledevice3.remote.core_device.hid_service import ASCII_TO_HID
    plan = []
    for i, ch in enumerate(text):
        if ch not in ASCII_TO_HID:
            raise Untypable(ch, i)
        plan.append(ASCII_TO_HID[ch])
    return plan


def ios_major(version: object) -> int | None:
    try:
        return int(str(version).split(".")[0])
    except (TypeError, ValueError):
        return None


def _services(rsd: Any) -> list[Any]:
    """The phone's (universal HID, Indigo) services for this tunnel (pymobiledevice3's classes)."""
    from pymobiledevice3.remote.core_device.hid_service import IndigoHIDService, UniversalHIDServiceService
    return [UniversalHIDServiceService(rsd), IndigoHIDService(rsd)]


class Hands:
    """The two HID service connections on the phone's tunnel. Opened lazily on the first gesture, and again after the
    tunnel was rebuilt (the Device has a new rsd). All gestures take `before`: awaited right before the first report,
    after the Hands are open (the session's guards). If it raises, nothing was sent."""

    def __init__(self, device: Any, log: Callable[[str], Any] = print,
                 services: Callable[[Any], Sequence[Any]] = _services) -> None:
        self.device = device
        self.log = log
        self._make = services
        self.screen: tuple[float, float] = (390.0, 844.0)  # the session's screen in points (a call may give its own)
        self._stream_ok = False
        self._lock = asyncio.Lock()
        self._rsd: Any = None  # the tunnel the open services belong to
        self._touch: Any = None  # UniversalHIDServiceService, connected
        self._buttons: Any = None  # IndigoHIDService, connected (None: the phone has none)
        self._keyboard: int | None = None  # the keyboard surface's _ServiceID (None: made when first typed)
        self._failed: tuple[Any, str, float] | None = None  # (rsd, why, when to try again) of the last failed open
        self._told: tuple[Any, str] | None = None  # what was last logged about which tunnel (logged once each)
        # what may still be down on the phone, to let go of first thing on the next open
        self.dirty: dict[str, Any] = {}
        self._dirty_at = 0.0  # loop time when `dirty` was first set (DIRTY_HOLD counts from it)
        self._pending: asyncio.Future | None = None  # a release still going out after its gesture was cancelled
        self._proven = False  # a touch of the open connection changed the screen (judged)
        self._unchanged = 0  # touches in a row of the open connection that changed nothing, while it isn't proven
        self._doubted: Any = None  # the tunnel whose touch connection was opened again for doubt, once
        self._spells = 0  # times on the runner in a row for this tunnel (judged), with no touch landed between
        self._landed = False  # a touch of this tunnel has landed (judged): some connection of it reached the screen
        self._tunnel: Any = None  # the tunnel _doubted, _spells and _landed are of (CodeRabbit, PR #29)
        # How many touch connections these hands have opened: the one a gesture went through is told from the one
        # open when its verdict comes (judged), which may be another (CodeRabbit, PR #29; review R202 L1)
        self.connection = 0
        self._last_key: object = None  # what the last touch that changed nothing was aimed at (judged)

    # ---- state ----
    @property
    def stream_ok(self) -> bool:
        """Whether a display stream runs (iOS 27+ drops HID touches without one); the eyes lane sets it."""
        return self._stream_ok

    @stream_ok.setter
    def stream_ok(self, value: bool) -> None:
        if bool(value) != self._stream_ok:
            self._stream_ok = bool(value)
            self._failed = None  # (review R200 L4) "needs a stream" was remembered for the tunnel: ask again

    @property
    def available(self) -> bool:
        """Open on the device's current tunnel: the next gesture goes out without opening anything."""
        rsd = getattr(self.device, "rsd", None)
        return rsd is not None and self._rsd is rsd and self._touch is not None

    def why_not(self, rsd: Any) -> str | None:
        """Why this tunnel's phone can't take HID at all (None: it can try)."""
        major = ios_major(getattr(rsd, "product_version", None))
        if major is None:
            return "the phone's iOS version is unknown"
        if major < MIN_IOS:
            return f"iOS {major} is older than iOS {MIN_IOS}"
        if major >= STREAM_IOS and not self.stream_ok:
            return f"iOS {major} drops HID touches unless a display stream runs, and none does"
        services = (getattr(rsd, "peer_info", None) or {}).get("Services") or {}
        if UNIVERSAL_SERVICE not in services:
            return "the phone offers no HID touch service (is the developer disk image mounted?)"
        return None

    def _say(self, rsd: Any, line: str) -> None:
        if self._told != (rsd, line):
            self._told = (rsd, line)
            try:
                self.log(line)
            except Exception:
                pass

    # ---- opening ----
    async def _ready(self) -> None:
        """Open the services (see _open_now). While something from an earlier gesture may still be down on the phone
        (`dirty`) and HID can't be opened to let go of it, no HandsUnavailable is raised for DIRTY_HOLD seconds: that
        would send the touch through the runner as a second finger (review R200 M2). The caller hears a HandsError
        (nothing was touched). After DIRTY_HOLD the phone is taken to have dropped it with the lost connection."""
        try:
            await self._open_now()
        except HandsUnavailable as e:
            self._hold(e)
            raise

    def _hold(self, e: HandsUnavailable) -> None:
        """Before a HandsUnavailable sends a gesture to the runner: while something from an earlier gesture may still
        be down (`dirty`), raise a HandsError instead for DIRTY_HOLD seconds (see _ready); after that, forget it."""
        if not self.dirty:
            return
        if asyncio.get_running_loop().time() - self._dirty_at >= DIRTY_HOLD:
            self._say(getattr(self.device, "rsd", None),
                      f"hands: what an earlier gesture left down ({', '.join(self.dirty)}) couldn't be let go of "
                      f"for {DIRTY_HOLD:g} s; given up")
            self.dirty = {}
            return
        raise HandsError(f"a finger, key or button from an earlier gesture may still be down on the phone, and this "
                         f"touch can't go through CoreDevice HID to let go of it first ({e.reason}); nothing was "
                         "touched: wait a few seconds, then look") from None

    async def judged(self, changed: bool, key: object = None, connection: int | None = None) -> str | None:
        """The caller's word on a touch these hands just played that should change the screen (a tap, long press or
        toggle aimed at something: the caller leaves every other gesture out, and every touch whose outcome the screen
        didn't show for certain): did it? The phone acks a report as it is written, whether or not it does anything,
        so only the screen can say that touches land. (2.0.2: a scroll or a swipe after which the screen changed
        counted as proof. A screen that changes by itself said "it lands" of a connection that reached nothing, and
        that connection was never doubted after it: the phone, 2026-11-26 12:41, taps answered "No visible change" for
        as long as the service ran.)

        `key`: what the touch was aimed at. On a connection that has landed a touch, touches in a row at the same
        thing that change nothing count once: a button that does nothing, pressed again and again, is no sign of a
        connection that died (review R202 M2). On one that hasn't, each counts.

        DOUBT_AFTER touches in a row that changed nothing close the connection: the next touch opens a new one
        ("reopened"). A connection that had landed a touch is doubted the same way (2.0.2: it was never doubted): the
        cost is the opening and the touches that count up to it. When the connection opened for doubt changes nothing
        either, touches go through the runner for RUNNER_FOR ("runner"), then the hands are tried again, and each
        time on the runner is twice the one before (up to RUNNER_MAX). A tunnel none of whose touches ever landed
        gives the connection tried after a time on the runner one touch to land, so a connection that never reaches
        the screen costs one touch now and then, not three every ten minutes. None otherwise.

        `connection`: the one the touch went through (Hands.connection as it was sent). The verdict comes after the
        screen settled, and another connection may be open by then (opened again for doubt, or by the next call):
        a verdict for a connection that is gone says nothing of this one, and is dropped (CodeRabbit, PR #29)."""
        if self._touch is None or (connection is not None and connection != self.connection):
            return None
        if changed:
            self._proven, self._unchanged, self._landed, self._last_key = True, 0, True, None
            # it lands: the next doubt of this tunnel starts with an opening again, and a first time on the runner
            self._doubted, self._spells = None, 0
            return None
        if self._proven and key is not None and key == self._last_key:
            return None
        self._last_key = key
        self._unchanged += 1
        after = 1 if self._spells and not self._landed else DOUBT_AFTER
        if self._unchanged < after:
            return None
        async with self._lock:
            rsd = self._rsd
            # (CodeRabbit, PR #26) asked again under the lock: a connection opened while this call waited counts anew
            if rsd is None or self._unchanged < after or (connection is not None and connection != self.connection):
                return None
            landed = self._proven
            await self._drop()
            if landed or self._doubted is not rsd:
                self._doubted = rsd
                self.log(f"hands: {DOUBT_AFTER} touches aimed at an element changed nothing in a row"
                         + (" (earlier touches of this connection did)" if landed else
                            " and none of this connection's has changed the screen yet")
                         + ": opening CoreDevice HID again")
                return "reopened"
            why = "touches through CoreDevice HID were acknowledged but changed nothing, also after it was opened again"
            spell = min(RUNNER_FOR * 2 ** min(self._spells, 16), RUNNER_MAX)  # (review R202 L4: the exponent is bounded)
            self._spells += 1
            self._failed = (rsd, why, asyncio.get_running_loop().time() + spell)
            self._say(rsd, f"hands: not used for {spell / 60:.0f} minutes ({why}); touches go through the runner")
            return "runner"

    async def warm(self) -> bool:
        """Open the services now, ahead of the first touch (the phone just connected): opening them took 0.4-1.7 s of
        the first tap after a start. True when they are open. Never raises: a first touch opens them as before, and
        says why when it can't."""
        try:
            async with self._lock:
                await self._open_now()
            return True
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - HandsUnavailable, a dropped line: the first touch finds out again
            return False

    async def _open_now(self) -> None:
        """Open the services on the device's current tunnel (under the lock). HandsUnavailable when they can't be."""
        rsd = getattr(self.device, "rsd", None)
        if rsd is None:
            raise HandsUnavailable("no tunnel to the phone")
        if self._rsd is rsd and self._touch is not None:
            return
        if self._rsd is not None and self._rsd is not rsd:
            await self._drop()  # the tunnel was rebuilt: the old connections went with it
        loop = asyncio.get_running_loop()
        if self._failed is not None and self._failed[0] is rsd and loop.time() < self._failed[2]:
            raise HandsUnavailable(self._failed[1])
        why = self.why_not(rsd)
        if why is not None:
            self._failed = (rsd, why, math.inf)
            self._say(rsd, f"hands: not used ({why}); touches go through the runner")
            raise HandsUnavailable(why)
        try:
            touch, buttons, keyboard = await asyncio.wait_for(self._open(rsd), OPEN_WAIT)
        except HandsUnavailable as e:
            self._failed = (rsd, e.reason, math.inf)
            self._say(rsd, f"hands: not used ({e.reason}); touches go through the runner")
            raise
        except Exception as e:  # noqa: BLE001 - whatever stopped the open, the runner still works
            why = f"couldn't open CoreDevice HID: {type(e).__name__}: {e}"
            self._failed = (rsd, why, loop.time() + RETRY_OPEN)
            self._say(rsd, f"hands: not used for now ({why}); touches go through the runner")
            raise HandsUnavailable(why) from None
        self._rsd, self._touch, self._buttons, self._keyboard = rsd, touch, buttons, keyboard
        self._failed = None
        self._proven, self._unchanged, self._last_key = False, 0, None
        self.connection += 1
        if self._tunnel is not rsd:
            # another tunnel: a new connection altogether, with no history. Told by the tunnel itself, not by _doubted
            # (CodeRabbit, PR #29: a tunnel whose touches had landed left _doubted empty, and the next tunnel kept
            # its "has landed")
            self._tunnel = rsd
            self._doubted, self._spells, self._landed = None, 0, False
        self._say(rsd, f"hands: CoreDevice HID (touchscreen {TOUCHSCREEN})")
        if self.dirty:
            try:
                await self._recover()
            except asyncio.CancelledError:
                # (CodeRabbit, PR #26) a connection whose release wasn't sent is no ready one: the next gesture opens
                # again and lets go first (what wasn't sent stays in dirty)
                await asyncio.shield(self._drop())
                raise

    async def _open(self, rsd: Any) -> tuple[Any, Any, int | None]:
        """Connect the touch service (and the buttons' one when the phone has it); check the touchscreen is listed."""
        touch, buttons = self._make(rsd)
        opened: list[Any] = []
        try:
            await touch.connect()
            opened.append(touch)
            listed = await touch.list_connected_services()
            ids = set()
            for entry in (listed or {}).get("connectedServices", []) or []:
                with contextlib.suppress(KeyError, TypeError, ValueError):
                    ids.add(int(entry["_ServiceID"]))
            if TOUCHSCREEN not in ids:
                raise HandsUnavailable(f"the phone lists no touchscreen surface {TOUCHSCREEN}")
            services = (getattr(rsd, "peer_info", None) or {}).get("Services") or {}
            if INDIGO_SERVICE in services:
                await buttons.connect()
                opened.append(buttons)
            else:
                buttons = None
            return touch, buttons, KEYBOARD_READY if KEYBOARD_READY in ids else None
        except BaseException:
            for service in opened:
                with contextlib.suppress(BaseException):
                    await service.close()
            raise

    async def _drop(self) -> None:
        """Close the connections (quietly: after a failure or with a dead tunnel they may already be gone)."""
        touch, buttons = self._touch, self._buttons
        self._touch = self._buttons = self._rsd = None
        self._keyboard = None
        for service in (touch, buttons):
            if service is not None:
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(service.close(), 2.0)

    async def _recover(self) -> None:
        """Let go of what a failed release left down (dirty), first thing on a fresh open. If that fails too, the
        Hands stay dirty and unavailable for now."""
        try:
            contacts = self.dirty.get("touch")
            if contacts:
                await self._send(self._touch.send_multitouch(contacts, TOUCHSCREEN))
                del self.dirty["touch"]
            keyboard = self.dirty.get("keys")
            if keyboard is not None:
                await self._send(self._touch.send_keyboard(keyboard, ()))
                del self.dirty["keys"]
            for page, usage in list(self.dirty.get("buttons", [])):
                if self._buttons is None:
                    break
                await self._send(self._buttons.send_button(page, usage, BUTTON_UP))
                self.dirty["buttons"].remove((page, usage))
            if not self.dirty.get("buttons"):
                self.dirty.pop("buttons", None)
            else:  # (CodeRabbit, PR #26) this connection has no buttons' service to let go of one: no touch goes out
                raise _SendFailed(RuntimeError("the phone's buttons service isn't there to let go of a button"))
            self._say(self._rsd, "hands: let go of what an earlier failed release left down")
        except _SendFailed as e:
            rsd = self._rsd
            await self._drop()
            why = f"couldn't let go of what an earlier gesture left down: {e}"
            self._failed = (rsd, why, asyncio.get_running_loop().time() + RETRY_OPEN)
            raise HandsUnavailable(why) from None

    async def close(self) -> None:
        async with self._lock:
            await self._settle()
            await self._drop()

    # ---- sending ----
    async def _send(self, report: Awaitable[None]) -> None:
        try:
            await asyncio.wait_for(report, SEND_WAIT)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as e:
            raise _SendFailed(e, timed_out=True) from None
        except Exception as e:  # noqa: BLE001 - mapped by the caller
            raise _SendFailed(e) from None

    async def _settle(self) -> None:
        """Wait for a release that kept going after its gesture was cancelled."""
        pending, self._pending = self._pending, None
        if pending is not None and not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                # (review R200 M1) the caller was cancelled while waiting: it must not go on and play its gesture
                if not pending.done():
                    self._pending = pending
                raise
            except BaseException:  # noqa: BLE001 - the release's own failure was dealt with where it ran
                pass

    async def _let_go(self, kind: str, send: Callable[[], Awaitable[None]], left: Any) -> None:
        """Send a release, shielded from cancellation: a cancelled caller leaves it running (the next gesture waits for
        it). When it can't be sent, `left` is kept in `dirty[kind]` and the connection is dropped, so the next open
        sends it first."""
        async def release() -> None:
            try:
                await self._send(send())
            except _SendFailed as e:
                self._mark_dirty(kind, left)
                await self._drop()
                self._say(self.device.rsd if self.device is not None else None,
                          f"hands: a release couldn't be sent ({e}); it goes out first on the next open")
        task = asyncio.ensure_future(release())
        self._pending = task
        await asyncio.shield(task)
        if self._pending is task:
            self._pending = None

    def _mark_dirty(self, kind: str, left: Any) -> None:
        if not self.dirty:
            self._dirty_at = asyncio.get_running_loop().time()
        if kind == "buttons":
            self.dirty.setdefault("buttons", [])
            if left not in self.dirty["buttons"]:
                self.dirty["buttons"].append(left)
        else:
            self.dirty[kind] = left

    async def _failed_send(self, e: _SendFailed, kind: str, left: Any) -> BaseException:
        """After a send failed mid-gesture: keep what may be down as dirty, drop the connection, and say what to raise
        (the report may have gone out, so never HandsUnavailable)."""
        if left:
            self._mark_dirty(kind, left)
        await self._drop()
        if e.timed_out:
            return HandsError(f"the phone didn't ack a HID report within {SEND_WAIT:g} s; the {kind} may have been "
                              "played: look before acting again")
        if device_module._line_dropped(e.cause) or isinstance(e.cause, OSError):
            # Not a ConnectionLost (lead's decision): that would rebuild the whole connection, runner included, and a
            # new runner can cost iOS's passcode prompt. Only the HID connection is known to be gone: it was dropped
            # above and is opened again on the same tunnel by the next gesture; a tunnel that is really dead shows
            # in the runner's own requests.
            return HandsError(f"the HID connection to the phone dropped ({e}); the {kind} may have been played: look "
                              "before acting again")
        return HandsError(f"sending a HID report failed ({type(e.cause).__name__}: {e}); the {kind} may have been "
                          "played: look before acting again")

    async def _portrait(self, screen: tuple[float, float] | None, turn: int) -> tuple[tuple[float, float], int]:
        """The screen and turn a touch is mapped with; HandsUnavailable (the runner plays it) when they can't be
        mapped, unless something may still be down (_hold: CodeRabbit, PR #26). A release still going out after its
        gesture was cancelled is waited for first (the same review's follow-up): until it has ended nothing says
        whether that finger is down, and the runner must not add a second one."""
        try:
            return self._mapped(screen or self.screen, turn)
        except HandsUnavailable as e:
            async with self._lock:
                await self._settle()
            self._hold(e)
            raise

    @staticmethod
    def _mapped(screen: tuple[float, float], turn: int) -> tuple[tuple[float, float], int]:
        if turn not in TURNS:
            raise HandsUnavailable(f"unknown turn {turn!r}")
        if turn != 0 and not LANDSCAPE:
            raise HandsUnavailable("landscape is left to the runner (TAPWRIGHT_HANDS_LANDSCAPE=0)")
        if turn == 0 and screen[0] > screen[1]:
            raise HandsUnavailable("the screen is landscape but which side is up isn't known yet")
        if turn != 0 and screen[0] <= screen[1]:
            raise HandsUnavailable("the screen is upright but the turn says landscape")  # (review R200 H1)
        return screen, turn

    # ---- touches ----
    async def tap(self, x: float, y: float, hold_ms: float = 50, *, screen: tuple[float, float] | None = None,
                  turn: int = 0, before: Before = None) -> None:
        """One finger down at (x, y) in screen points, held `hold_ms`, lifted at the same point."""
        screen, turn = await self._portrait(screen, turn)
        hx, hy = to_hid(x, y, screen, turn)
        await self._play([(0.0, [(SINGLE, hx, hy, True)]), (float(hold_ms), [(SINGLE, hx, hy, False)])], before, "tap")

    async def path(self, points: Sequence[Point], *, screen: tuple[float, float] | None = None, turn: int = 0,
                   before: Before = None) -> None:
        """One finger along [(t_ms, x, y), ...] in screen points: pressed at the first, moved through each at its time
        (a contact report at least every STEP_MS), lifted at the last."""
        screen, turn = await self._portrait(screen, turn)
        filled = timeline(points)
        frames: list[tuple[float, list[tuple[int, int, int, bool]]]] = []
        for t, x, y in filled:
            frames.append((t, [(SINGLE, *to_hid(x, y, screen, turn), True)]))
        t, x, y = filled[-1]
        frames.append((t, [(SINGLE, *to_hid(x, y, screen, turn), False)]))
        await self._play(frames, before, "drag")

    async def multi(self, paths: Sequence[Sequence[Point]], *, screen: tuple[float, float] | None = None,
                    turn: int = 0, before: Before = None) -> None:
        """Up to 5 fingers, each along its own [(t_ms, x, y), ...] on one clock (see multi_frames)."""
        screen, turn = await self._portrait(screen, turn)
        frames = [(t, [(FINGER_IDS[i], *to_hid(x, y, screen, turn), touching) for i, x, y, touching in slots])
                  for t, slots in multi_frames(paths)]
        await self._play(frames, before, "gesture")

    async def _play(self, frames: Sequence[tuple[float, Sequence[tuple[int, int, int, bool]]]], before: Before,
                    kind: str) -> None:
        """Send touchscreen reports at their times (ms from the first). Every finger that may be down when this ends,
        however it ends, is lifted (see the module's safety rules)."""
        async with self._lock:
            await self._settle()
            await self._ready()
            if before is not None:
                await before()  # (nothing sent yet: its refusal stands as it is)
            touch = self._touch
            down: dict[int, Contact] = {}  # fingers the phone may have down, at their last reported place
            loop = asyncio.get_running_loop()
            start = loop.time()
            healthy = True
            try:
                for t, slots in frames:
                    wait = start + t / 1000 - loop.time()
                    if wait > 0:
                        await asyncio.sleep(wait)
                    contacts = [Contact(i, x, y, touching) for i, x, y, touching in slots]
                    for c in contacts:
                        if c.touching:
                            down[c.identity] = c  # (before the send: a failed one may still have arrived)
                    try:
                        await self._send(touch.send_multitouch(contacts, TOUCHSCREEN))
                    except _SendFailed as e:
                        healthy = False
                        left = [c._replace(touching=False) for c in down.values()]
                        raise await self._failed_send(e, "touch", left) from None
                    for c in contacts:
                        if not c.touching:
                            down.pop(c.identity, None)
            finally:
                if down and healthy:
                    lift = [c._replace(touching=False) for c in down.values()]
                    await self._let_go("touch", lambda: touch.send_multitouch(lift, TOUCHSCREEN), lift)

    # ---- buttons ----
    async def button(self, name: str, *, before: Before = None) -> None:
        """Press and release a hardware button: home, lock, volume_up, volume_down, mute or siri (held 1 s)."""
        if name not in BUTTONS:
            raise ValueError(f"button must be one of {', '.join(BUTTONS)}, not {name!r}")
        page, usage, hold = BUTTONS[name]
        async with self._lock:
            await self._settle()
            await self._ready()
            buttons = self._buttons
            if buttons is None:
                raise HandsUnavailable("the phone offers no HID button service")
            if before is not None:
                await before()
            pressed = False
            healthy = True
            try:
                pressed = True  # (before the send: a failed one may still have arrived)
                try:
                    await self._send(buttons.send_button(page, usage, BUTTON_DOWN))
                except _SendFailed as e:
                    healthy = False
                    raise await self._failed_send(e, "buttons", (page, usage)) from None
                await asyncio.sleep(hold / 1000)
                try:
                    await self._send(buttons.send_button(page, usage, BUTTON_UP))
                except _SendFailed as e:
                    healthy = False
                    raise await self._failed_send(e, "buttons", (page, usage)) from None
                pressed = False
            finally:
                if pressed and healthy:
                    await self._let_go("buttons", lambda: buttons.send_button(page, usage, BUTTON_UP), (page, usage))

    # ---- keys ----
    async def keys(self, text: str, *, before: Before = None) -> None:
        """Type US-ASCII `text` through the keyboard surface (shift for capitals and symbols). Untypable, naming the
        first character that can't be typed, is raised before anything is sent."""
        plan = plan_keys(text)
        reports: list[tuple[int, ...]] = []
        for usage, shift in plan:
            if shift:
                reports += [(LEFT_SHIFT,), (LEFT_SHIFT, usage), (LEFT_SHIFT,), ()]
            else:
                reports += [(usage,), ()]
        await self._type(reports, before)

    async def key(self, name: str, *, before: Before = None) -> None:
        """One key: enter, backspace, tab, escape, left, right, up or down."""
        if name not in KEYS:
            raise ValueError(f"key must be one of {', '.join(KEYS)}, not {name!r}")
        await self._type([(KEYS[name],), ()], before)

    async def _type(self, reports: Sequence[tuple[int, ...]], before: Before) -> None:
        if not reports:
            return
        async with self._lock:
            await self._settle()
            await self._ready()
            touch = self._touch
            if self._keyboard is None:
                try:
                    self._keyboard = int(await asyncio.wait_for(touch.create_keyboard_service(), OPEN_WAIT))
                except Exception as e:  # noqa: BLE001 - no keyboard: the runner types instead
                    raise HandsUnavailable(f"couldn't make a HID keyboard: {type(e).__name__}: {e}") from None
            keyboard = self._keyboard
            if before is not None:
                await before()
            held = False  # a key may be down on the phone
            healthy = True
            loop = asyncio.get_running_loop()
            start = loop.time()
            try:
                for n, usages in enumerate(reports):
                    wait = start + n * KEY_GAP_MS / 1000 - loop.time()
                    if wait > 0:
                        await asyncio.sleep(wait)
                    if usages:
                        held = True
                    try:
                        await self._send(touch.send_keyboard(keyboard, usages))
                    except _SendFailed as e:
                        healthy = False
                        raise await self._failed_send(e, "keys", keyboard if held else None) from None
                    if not usages:
                        held = False
            finally:
                if held and healthy:
                    await self._let_go("keys", lambda: touch.send_keyboard(keyboard, ()), keyboard)

"""The phone side of `tapwright serve`: one Session (tunnel and WebDriverAgent), the control
rules, the stream, the agent tools, a person's input from the live view, and a supervisor
that keeps it all connected. server.py only turns HTTP into calls here."""
import asyncio
import base64
import io
import json
import dataclasses
import math
import re
import time
import traceback
from contextlib import nullcontext
from typing import NamedTuple

from PIL import Image, UnidentifiedImageError

from . import changes, ui
from . import shot as shotlib
from .control import TAP_MS, Controller, Refused, classify
from .device import AppNotAnswering, ConnectionLost, DeviceError, NotSetUp, PhoneLocked, PhoneNotFound
from .session import Snapshot
from .stream import NO_IDLE_WAIT, PRESETS, Frame, FrameHub, StreamPump, alike

PROTOCOL = 1
ACCEPTS = ["tap", "longPress", "drag", "scroll", "text", "enter", "backspace", "home", "volume", "quality"]
BUTTONS = ("home", "volumeUp", "volumeDown")
STILL = 0.1  # seconds without a changed frame before a screen counts as still (docs/AGENT-TOOLS.md §6)
RESOLVE_SETTLE = 1.0  # the longest a silent re-read waits for the screen to be still
CAP = 2.0  # the longest an action's answer waits for the screen
LONG_CAP = 4.0  # the same after launching an app or opening a URL
LIVE_AGE = 2.0  # seconds a list stays current for id, label and text targets (docs/AGENT-TOOLS.md §2)
FRESH_FRAME = 2.0  # seconds a stream frame counts as "the screen now"
MIN_QUIET = 0.3  # seconds a screen must stay as it was before "No visible change." is believed
NOTE_CAPPED = ("the screen was still changing after {cap:g} s (an animation or video); "
               "positions may be mid-change.")


class Scope(NamedTuple):
    """Personal mode: the apps an agent may read and touch, as bundle ids or exact names. Empty: none, so
    nothing runs (it fails closed). A dedicated device has no Scope at all."""
    apps: tuple[str, ...] = ()


NO_APPS = ("no_apps: this agent was started without an app list, so it may not use the phone. Start it with "
           "`tapwright mcp --apps <bundle id>` for the apps under test, or with `--dedicated` on a device "
           "kept for testing.")
SCOPE_FREE = ("open_app", "close_app", "open_url", "press_button")  # how an agent gets back to its app
APP_LINE = re.compile(r"^App: (.+) \(([^()\s]+)\)$", re.M)  # the header line of a full list: name, bundle id


def _outside(bundle: str | None, name: str | None, given: str) -> str:
    front = "the phone can't say which app" if not bundle else f"{name} ({bundle})" if name else bundle
    return (f"outside_apps: {front} is in front and isn't one of the apps you were given ({given}), so nothing of it is shown or "
            "touched. Go back with open_app.")


class Settled(NamedTuple):
    seconds: float
    changed: bool  # False only when the stream showed the screen unchanged for certain
    capped: bool  # the cap ended the wait with frames still changing
WAIT_MIN, WAIT_MAX = 0.1, 30.0  # seconds, for `wait`
WAIT_FOR_MIN, WAIT_FOR_DEFAULT = 0.5, 10.0  # `wait_for`'s timeout in seconds (its upper end is WAIT_MAX)
WAIT_FOR_KINDS = ("text", "id", "label")
WAIT_FOR_POLL = 0.3  # seconds between tree reads while waiting
SCROLL_MIN, SCROLL_MAX = 0.1, 1.0  # `scroll`'s amount: a share of the scroll area
MAX_SCROLL_SWIPES = 15  # `scroll to start|end` gives up after this many swipes
SCROLL_CAP = 12.0  # ... or after this many seconds (a spinner or a bounce can keep the screen moving)
SCROLL_TO = {"end": ("down", "right"), "start": ("up", "left")}  # the directions each `to` can mean; the first is the default
SCROLL_TOP_INSET, SCROLL_BOTTOM_INSET = 60.0, 40.0  # points a swipe keeps off the screen's edges when no bar says more
WAIT_FOR_READ_MAX = 5.0  # the longest one tree read in `wait_for` may take
WAIT_FOR_MIN_LEFT = 0.1  # with less time than this left, `wait_for` gives up instead of reading again
# The stream's pace. Every frame WebDriverAgent captures costs the phone, mostly in iOS's testmanagerd: about 88% of
# a core at balanced's ~27 fps and 38% at light's ~10 fps (DVT sysmontap, the lab animating, 2026-11-10). So the
# stream runs at its preset's rate while something happens, and at STILL_FPS once nothing has: no agent call for
# the first quiet span (the stream's own linger, so an agent between steps never waits for the rate to rise), no
# person's input for the second, no change on the screen for the third. STILL_FPS keeps the longest gap
# between frames at 0.2 s (V3: at most 0.25 s).
STILL_FPS = 5
PACE_QUIET = (30.0, 2.0, 1.0)  # seconds without an agent call, a person's input, a change on the screen
PACE_TICK = 0.25  # how often the pace is checked
NOTE_PERSON = "a person used the phone since your last look."
NOTE_KEYBOARD = "closed the keyboard first."
KEYBOARD_TOOLS = ("tap", "long_press", "toggle", "type_text")  # the touches that close the keyboard before them
NOTE_COVERED = "something outside the app (a system sheet or browser) may cover it."


def _size(jpeg: bytes | None) -> list[int] | None:
    """[width, height] in pixels of a JPEG, or None when there is no readable picture."""
    if not jpeg:
        return None
    try:
        with Image.open(io.BytesIO(jpeg)) as img:
            return [img.width, img.height]
    except (UnidentifiedImageError, OSError):
        return None


def _answer(lines: list[str], notes: list[str] | None = None, image: bytes | None = None) -> dict:
    """One tool answer (docs/AGENT-TOOLS.md §3): the lines as one text, a `Note: ` line per note,
    and the picture, if any, with its size."""
    text = "\n".join([*lines, *(f"Note: {n}" for n in notes or [])])
    return {"text": text, "image": base64.b64encode(image).decode() if image else None, "image_size": _size(image)}


def settled_line(seconds: float) -> str:
    return f"Settled in {seconds:.2f} s."


def _alert_key(alert: ui.Alert | None) -> tuple[str, tuple[str, ...]] | None:
    return (alert.title, alert.buttons) if alert else None


def _furniture(view, alert: ui.Alert | None, keyboard: bool) -> list[str]:
    """What an alert or the keyboard did since the agent's last answer, as the first lines of a change block."""
    lines = []
    if _alert_key(alert) != view.alert:
        lines.append(ui.alert_line(alert) if alert else "Alert: gone")
    if keyboard != view.keyboard:
        lines.append(f"Keyboard: {'shown' if keyboard else 'hidden'}")
    return lines


def _number(args: dict, key: str) -> float | None:
    """args[key] as a finite float; None when it isn't given. Anything else is bad_params."""
    value = args.get(key)
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = math.nan
    if not math.isfinite(number):
        raise Refused("bad_params", f"{key} must be a number, not {value!r}")
    return number


def _checked(name: str, args: dict) -> dict:
    """The agent's arguments with numbers coerced, or Refused(bad_params) naming the one that's wrong.
    This runs before anything reaches the phone, so a ValueError later is never the agent's fault."""
    checked = dict(args)
    for key in ("x0", "y0", "x1", "y1", "seconds", "amount"):
        if (number := _number(args, key)) is not None:
            checked[key] = number
    if args.get("target") is not None:
        checked["target"] = ui.parse_target(args["target"])  # bad_params, with an example of a target
    if name == "scroll":
        if checked.get("direction") is None:  # null (the MCP server sends every argument) is left out
            checked.pop("direction", None)
        if checked.get("direction", "down") not in ("up", "down", "left", "right"):
            raise Refused("bad_params", f"direction must be up, down, left or right, not {args['direction']!r}")
        if args.get("to") not in (None, "start", "end"):
            raise Refused("bad_params", f"to must be start or end, not {args['to']!r}")
        if args.get("to") and "direction" in checked and checked["direction"] not in SCROLL_TO[args["to"]]:
            raise Refused("bad_params", f"to {args['to']} scrolls {' or '.join(SCROLL_TO[args['to']])}, not "
                                        f"{args['direction']}; leave direction out, or make them agree")
        if "amount" in checked and not SCROLL_MIN <= checked["amount"] <= SCROLL_MAX:
            raise Refused("bad_params", f"amount must be from {SCROLL_MIN:g} to {SCROLL_MAX:g}, not {args['amount']!r}")
    if name == "type_text":
        if not isinstance(args.get("text"), str):
            raise Refused("bad_params", f"text must be a string, not {args.get('text')!r}")
        for key in ("replace", "submit"):
            if key in args and not isinstance(args[key], bool):
                raise Refused("bad_params", f"{key} must be true or false, not {args[key]!r}")
    if name == "wait_for":
        given = [k for k in WAIT_FOR_KINDS if args.get(k) is not None]
        if len(given) != 1:
            raise Refused("bad_params", "wait_for needs exactly one of text, id or label")
        if not isinstance(args[given[0]], str) or not args[given[0]]:
            raise Refused("bad_params", f"{given[0]} must be a non-empty string, not {args[given[0]]!r}")
        if "gone" in args and not isinstance(args["gone"], bool):
            raise Refused("bad_params", f"gone must be true or false, not {args['gone']!r}")
        timeout = args.get("timeout", WAIT_FOR_DEFAULT)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not WAIT_FOR_MIN <= timeout <= WAIT_MAX:
            raise Refused("bad_params", f"timeout must be a number from {WAIT_FOR_MIN:g} to {WAIT_MAX:g}, not {timeout!r}")
    for key in ("look", "screenshot", "labels"):
        if key in args and not isinstance(args[key], bool):
            raise Refused("bad_params", f"{key} must be true or false, not {args[key]!r}")
    if name == "wait":
        seconds = args.get("seconds")  # required (REQUIRED); a bool is a number to Python, a string isn't one to us
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not WAIT_MIN <= seconds <= WAIT_MAX:
            raise Refused("bad_params", f"seconds must be a number from {WAIT_MIN:g} to {WAIT_MAX:g}, not {seconds!r}")
    if name in ("open_app", "close_app") and (not isinstance(args["app"], str) or not args["app"].strip()):
        raise Refused("bad_params", f"app must be a bundle id or an app's name, not {args['app']!r}")
    if name == "toggle" and args.get("value") is not None and not isinstance(args["value"], bool):
        raise Refused("bad_params", f"value must be true or false, not {args['value']!r}")
    return checked


class Unchanged(str):
    """A handler's result that did nothing on the phone (`toggle` to the state it's already in): it is
    the whole answer, with no change block."""


class Toggled(str):
    """`toggle`'s result, `Tapped <line>.` until the tree is read: then the first line says what the switch did."""
    ref: str = ""
    before: ui.Element = ui.Element("", "switch")  # the switch as it was tapped
    want: bool | None = None  # the `value` asked for
    flipped: bool | None = None  # where a tap should have taken it

    def verdict(self, now: ui.Element, alert: bool) -> str:
        """The first line from the switch as it reads after the tap. Raises when a wanted value wasn't reached
        (unless an alert is up: it may be asking to confirm)."""
        line = now.line(actions=False)
        if now.checked == self.flipped:
            return f"Toggled {line}."
        if self.want is not None and not alert:
            raise Refused("not_actionable", f"no_action: {line} didn't change")
        return f"Tapped {line}, but it is still checked={'true' if now.checked else 'false'}."


class Typed(str):
    """`type_text`'s result, `Typed into <line>.` until the tree is read: then the first line also gives the value."""
    field: ui.Element = ui.Element("", "input")  # the input as it was when typed into

    def same_field(self, now: ui.Element) -> bool:
        """Is `now` the field typed into (the ref may have gone to another input since)? The same ref and
        role, and the same identifier, or without one the same label."""
        old = self.field
        return (now.ref, now.role, now.identifier) == (old.ref, old.role, old.identifier) \
            and (bool(old.identifier) or now.label == old.label)

    def verdict(self, now: ui.Element) -> str:
        line = now.line(actions=False)
        if now.secure:
            return f"Typed into {line}. The value is hidden (secure field)."
        return f"Typed into {line}. Value is now {json.dumps(now.value, ensure_ascii=False)}."


class Scrolled(str):
    """`scroll`'s result, with a note for the answer when it stopped early."""
    note: str = ""


# What `open_app "Settings"` means: the built-in apps aren't in the installed list.
SYSTEM_APPS = {"Settings": "com.apple.Preferences", "Safari": "com.apple.mobilesafari",
               "Calculator": "com.apple.calculator", "Camera": "com.apple.camera",
               "Photos": "com.apple.mobileslideshow", "Messages": "com.apple.MobileSMS",
               "Mail": "com.apple.mobilemail", "Maps": "com.apple.Maps", "App Store": "com.apple.AppStore",
               "Clock": "com.apple.mobiletimer", "Notes": "com.apple.mobilenotes"}


def app_line(a: dict) -> str:
    return f"{a['bundle_id']}  {a['name']} {a['version']} ({a['build']})"


def _target(a: dict) -> ui.Target:
    return a["target"]  # parsed by _checked; tap and long_press require it


# Agent actions: tool -> (what it does on the session, seconds to settle before the look; None: no look)
ACTIONS = {
    "open_app": (None, LONG_CAP),  # None: the handler is a Daemon method (Daemon._handlers), which needs the agent's list
    "close_app": (None, None),
    "toggle": (None, CAP),
    "back": (None, CAP),
    "tap": (None, CAP),
    "long_press": (None, CAP),
    "type_text": (None, CAP),
    "scroll": (None, CAP),
    "swipe": (lambda s, a: s.swipe(float(a["x0"]), float(a["y0"]), float(a["x1"]), float(a["y1"]),
                                   int(float(a.get("seconds", 0.35)) * 1000)), CAP),
    "press_button": (lambda s, a: s.press_button(a["name"]), CAP),
    "alert": (lambda s, a: s.alert(a["action"]), CAP),
    "open_url": (lambda s, a: s.open_url(a["url"]), LONG_CAP),
}
MIN_VISIBLE = 10.0  # points: less of an element showing than this (high or wide) is no place to touch
MOVES_CONTENT = ("scroll", "swipe", "back", "open_app", "open_url", "press_button")  # identical elements can't be told apart after these
READS = ("look", "apps")
BUNDLE_ID = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
SPECIAL = ("wait", "wait_for")  # sleep before they answer, so they don't hold the phone's queue meanwhile
REQUIRED = {"open_app": ("app",), "close_app": ("app",), "toggle": ("target",), "wait": ("seconds",),
            "type_text": ("text",), "tap": ("target",), "long_press": ("target",),
            "swipe": ("x0", "y0", "x1", "y1"), "press_button": ("name",), "alert": ("action",),
            "open_url": ("url",)}


async def _first(timeout: float, *events: asyncio.Event) -> None:
    """Wait until any of the events is set, or `timeout` seconds."""
    waits = [asyncio.create_task(e.wait()) for e in events]
    try:
        await asyncio.wait(waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waits:
            w.cancel()


class Daemon:
    def __init__(self, session, control: Controller | None = None, log=print, heartbeat: float = 5.0,
                 backoff: tuple = (1.0, 2.0, 5.0, 10.0), ready_wait: float = 60.0,
                 stream_linger: float = 30.0, stream_retry: float = 1.0, settle_still: float = STILL,
                 settle_first_frame: float = 1.0, settle_fallback: float = 0.6,
                 settle_quiet: float = MIN_QUIET, shot_wait: float = 1.0, shot_fresh: float = 1.0,
                 pace_tick: float | None = PACE_TICK, pace_quiet: tuple[float, float, float] = PACE_QUIET) -> None:
        self.session = session
        # the stream's pace (see STILL_FPS): how often it's checked (None: never), and the frame rate the phone
        # streams at now (None until connected)
        self.pace_tick, self.pace_quiet = pace_tick, pace_quiet
        self._fps: int | None = None
        self._person_at = self._changed_at = -math.inf  # monotonic: a person's last input, the screen's last change
        self._pace_seen: Frame | None = None  # the newest frame at the last check
        # screenshots: how long to wait for a stream frame before asking the phone, and how old one may be
        self.shot_wait, self.shot_fresh = shot_wait, shot_fresh
        self.unsettled = False  # the last action returned without a settle: the screen may still be moving, for every client
        # settle: how long a still screen is quiet, how long to wait for a first frame, and the
        # blind sleep used when the stream gives no frames at all
        self.settle_still, self.settle_first_frame, self.settle_fallback = settle_still, settle_first_frame, settle_fallback
        self.settle_quiet = settle_quiet
        self.control = control or Controller()
        self.hub = FrameHub()
        self.pump = StreamPump(self._open_stream, self.hub, linger=stream_linger, retry=stream_retry, log=log)
        self.log = log
        self.heartbeat, self.backoff, self.ready_wait = heartbeat, backoff, ready_wait
        self.state, self.detail = "connecting", None
        self.preset = "balanced"
        self.ws_clients = 0
        self.video = None  # a tapwright.video.VideoHub while the H.264 live view is set up (see start_video)
        self.want_video = False  # `tapwright serve --video`: set the video up once the phone is connected
        self._receiver = None  # the AirPlay receiver process that feeds `video`
        self._video_task: asyncio.Task | None = None
        self.last_request = time.monotonic()
        self._ever_connected = False
        # why the last connect couldn't finish, when the phone said: a PhoneLocked, or an AppNotAnswering
        self.stuck: PhoneLocked | AppNotAnswering | None = None
        self._connected = asyncio.Event()
        self._lost = asyncio.Event()
        self._wake = asyncio.Event()  # connect again now, without waiting out the backoff
        self._scopes: dict[tuple[str, ...], dict[str, str]] = {}  # each app list seen, as {bundle id: shown}
        self._queues: set = set()
        self._handlers = {"open_app": self._open_app, "close_app": self._close_app, "toggle": self._toggle,
                          "tap": self._tap, "long_press": self._long_press, "back": self._back, "type_text": self._type_text, "scroll": self._scroll}

    # ---- status and events ----
    def status(self) -> dict:
        w, h = self.session.screen
        via = (getattr(self.session.device, "via", None) or "").lower()
        via = ("usb" if via == "usb" else "wifi" if via == "local network"
               else "tailscale" if via.startswith("network") else None)
        return {"type": "status", "protocol": PROTOCOL, "tool": "Tapwright", "state": self.state, "detail": self.detail, "via": via,
                "fps": self.hub.fps(), "driver": self.control.driver, "paused": self.control.paused,
                "size": {"w": w, "h": h}, "quality": self.preset,
                "accepts": [*ACCEPTS, "video"] if self.video is not None and self.video.live else ACCEPTS}

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._queues.discard(queue)

    def broadcast(self, message: dict) -> None:
        """To every live view; one that can't keep up loses its oldest messages."""
        for queue in list(self._queues):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(message)

    def _set_state(self, state: str, detail: str | None = None) -> None:
        changed = (state, detail) != (self.state, self.detail)
        self.state, self.detail = state, detail
        if state in ("live", "locked"):
            self._connected.set()
        else:
            self._connected.clear()
        if changed:
            self.broadcast(self.status())

    def _point(self, point) -> dict | None:
        if not point:
            return None
        w, h = self.session.screen
        return {"x": round(point[0] / w, 4), "y": round(point[1] / h, 4)}

    def _announce(self, by: str, kind: str, label: str | None = None, point=None, ok: bool = True,
                  code: str | None = None) -> None:
        message = {"type": "action", "by": by, "kind": kind, "at": int(time.time() * 1000), "ok": ok}
        if label:
            message["label"] = label
        if point:
            message["point"] = point
        if code:
            message["code"] = code
        self.broadcast(message)

    # ---- the connection ----
    def lost(self, reason: "str | Exception") -> None:
        """A request found the line dead: the supervisor reconnects. When the request was cut because the app in
        front doesn't answer (AppNotAnswering), that is what agents and the live view are told meanwhile."""
        stuck = reason if isinstance(reason, AppNotAnswering) else None
        if stuck is not None:
            self.stuck = stuck
        if self.state in ("live", "locked"):
            self.log(f"connection lost: {reason}")
            self._set_state("not-answering" if stuck is not None else "reconnecting", str(reason)[:200])
        for view in self.control.clients.values():
            view.dropped = True  # every agent's list is from before the drop
        self._lost.set()

    def _not_connected(self) -> Refused:
        """No phone found or not set up: the agent can't fix that by waiting. Otherwise the link
        is coming back."""
        if self.state == "no-device":
            return Refused("not_connected", f"no phone to drive ({self.detail}); fix that, then try again")
        if isinstance(self.stuck, PhoneLocked):
            return Refused("locked", "the phone is locked; ask the user to unlock it")
        if isinstance(self.stuck, AppNotAnswering):
            return Refused("not_answering", f"{self.stuck}. close_app can still close it; then open_app it again.")
        return Refused("reconnecting", f"not connected to the phone ({self.detail or self.state}); "
                                       "wait a few seconds, then look")

    def _require_connected(self) -> None:
        if not self._connected.is_set():
            raise self._not_connected()

    async def _connected_only(self, fn):
        """Run `fn` only if the line is up when its turn comes: a reconnect holds the queue,
        and whatever waited for it must not reconnect on its own."""
        self._require_connected()
        return await fn()

    async def wait_connected(self) -> None:
        if self.state == "no-device" or self.stuck is not None:  # waiting won't fix either
            raise self._not_connected()
        try:
            await asyncio.wait_for(self._connected.wait(), self.ready_wait)
        except asyncio.TimeoutError:
            raise self._not_connected() from None

    async def _connect(self) -> None:
        """A working WebDriverAgent session: a new WebDriverAgent if the tunnel still works,
        otherwise everything from scratch."""
        async with self.control.exclusive():
            s = self.session
            if s.device.rsd is not None:
                s.sid = None
                try:
                    await s.ensure()
                except (NotSetUp, PhoneLocked, AppNotAnswering):  # the tunnel is fine: rebuilding it won't help
                    raise
                except Exception as e:
                    self.log(f"restarting WebDriverAgent didn't work ({e}); reconnecting")
                    await s.reset()
                    await s.ensure()
            else:
                await s.ensure()
            await s.settings({**PRESETS[self.preset], **NO_IDLE_WAIT})
            self._fps = self._preset_fps()
            await s.read_scale(refresh=True)  # once per connection, so every header says @3x, not only SpringBoard's
            await s.read_app_names(refresh=True)  # so the header's App: line says "Settings", not a bundle id

    async def supervise(self, stop: asyncio.Event) -> None:
        """Connect, then keep checking; reconnect whenever the line drops. Runs until `stop`."""
        pacer = asyncio.create_task(self._pace(stop), name="pace") if self.pace_tick else None
        try:
            await self._supervise(stop)
        finally:
            if pacer is not None:
                pacer.cancel()
                await asyncio.gather(pacer, return_exceptions=True)

    # ---- the stream's pace ----
    def _preset_fps(self) -> int:
        return int(PRESETS[self.preset]["mjpegServerFramerate"])

    async def _set_fps(self, fps: int) -> None:
        await self.session.settings({"mjpegServerFramerate": fps})
        self._fps = fps

    async def _quicken(self) -> None:
        """Back to the preset's rate before an action, so its settle (and the person's picture) has every frame.
        Only after a quiet spell; a failure to change the rate never stops the action."""
        if self._fps is not None and self._fps < self._preset_fps():
            try:
                await self._set_fps(self._preset_fps())
            except ConnectionLost:
                raise
            except DeviceError as e:
                self.log(f"pace: couldn't raise the frame rate: {e}")

    async def _pace(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await _first(self.pace_tick, stop)
            if stop.is_set():
                return
            try:
                await self._pace_once()
            except ConnectionLost as e:
                self.lost(e)
            except Refused:
                pass  # not connected, or paused: the next check tries again
            except Exception as e:
                self.log(f"pace: {e}")

    async def _pace_once(self) -> None:
        """Slow the stream when nothing has happened for a while, and quicken it when the screen changes."""
        now = time.monotonic()
        frame = self.hub.latest
        if frame is not None and frame is not self._pace_seen:
            if not alike(frame, self._pace_seen):  # a first frame counts as a change: nothing says it's still yet
                self._changed_at = now
            self._pace_seen = frame
        if self.state != "live" or self._fps is None or frame is None or self.hub.fps() == 0:
            return  # nothing streams, so the pace costs nothing
        agent, person, still = self.pace_quiet
        busy = (now - self.last_request < agent or now - self._person_at < person or now - self._changed_at < still)
        want = self._preset_fps() if busy else min(STILL_FPS, self._preset_fps())
        if want != self._fps:
            await self.control.read(lambda: self._connected_only(lambda: self._set_fps(want)))

    async def _supervise(self, stop: asyncio.Event) -> None:
        attempt = failures = 0
        while not stop.is_set():
            if self.state not in ("live", "locked"):
                try:
                    await self._connect()
                except NotSetUp as e:
                    self._set_state("no-device", f"needs setup: {e}")
                except PhoneNotFound:
                    self._set_state("no-device", "phone not found: plug it in and unlock it")
                except (PhoneLocked, AppNotAnswering) as e:
                    if self.stuck is None:
                        self.log(f"connected, but {e}")
                    self.stuck = e
                    state = "reconnecting" if self._ever_connected else "connecting"
                    self._set_state("not-answering" if isinstance(e, AppNotAnswering) else state, str(e)[:200])
                except Exception as e:
                    self.stuck = None
                    self._set_state("reconnecting" if self._ever_connected else "connecting", str(e)[:200])
                else:
                    attempt = failures = 0
                    self.stuck = None
                    self._ever_connected = True
                    self._lost.clear()  # a drop noticed while reconnecting is already dealt with
                    self._set_state("live")
                    self.log(f"connected over {self.session.device.via}")
                    if self.want_video and self._video_task is None:
                        self._video_task = asyncio.create_task(self.start_video(), name="video")
                    continue
                await _first(self.backoff[min(attempt, len(self.backoff) - 1)], stop, self._wake)
                self._wake.clear()
                attempt += 1
                continue
            await _first(self.heartbeat, stop, self._lost)
            if stop.is_set():
                break
            if self._lost.is_set():
                self._lost.clear()
                if self.state in ("live", "locked"):
                    self._set_state("reconnecting", "connection lost")
                continue
            try:
                locked = await self.control.read(lambda: self._connected_only(self.session.locked))
            except ConnectionLost as e:
                self.lost(e)
            except Refused:
                pass
            except Exception as e:
                failures += 1
                self.log(f"heartbeat: {e}")
                if failures >= 3:
                    failures = 0
                    self.lost(f"WebDriverAgent keeps failing: {e}")
            else:
                failures = 0
                if not self._lost.is_set() and self.state in ("live", "locked"):
                    if locked:
                        self._set_state("locked", "unlock the phone")
                    else:
                        self._set_state("live")

    # ---- agent tools ----
    async def run_tool(self, client_id: str, name: str, args: dict, scope: "Scope | None" = None) -> dict:
        """One agent tool call: {"text": str, "image": base64 JPEG or None, "image_size": [w, h] or None}.
        Raises Refused with a code from errors.CODES; any other failure is `internal` (logged).
        `scope` is the apps this agent was given (personal mode); None is a dedicated device: no limit."""
        self.last_request = time.monotonic()
        try:
            # The stream is held open for the length of every tool call; the pump's own linger keeps
            # it open for 30 s after the last one, so the settle always has frames to read.
            # Not when there is no phone: the pump would only retry opening it.
            async with self.pump.watching() if self._connected.is_set() else nullcontext():
                if scope is None:
                    return await self._run_tool(client_id, name, args)
                return await self._within(scope, client_id, name, args)
        except Refused:
            raise
        except DeviceError as e:  # WebDriverAgent or the link answered with an error; not our bug
            self.log(f"{name}: the phone side failed: {e}")
            raise Refused("internal", f"the phone side failed: {e}") from e
        except Exception as e:  # one failed step never takes the daemon down
            self.log(f"{name} failed unexpectedly:\n{traceback.format_exc()}")
            raise Refused("internal", f"{name} failed unexpectedly: {e}. This is a bug; report it.") from e

    # ---- personal mode: the agent stays in the apps it was given ----
    async def _given(self, scope: "Scope") -> dict[str, str]:
        """{bundle id: how to show it} of the apps in a scope. An entry that looks like a bundle id is taken as
        it is; a name is looked up among the installed apps, once."""
        if scope.apps not in self._scopes:
            given: dict[str, str] = {}
            for entry in scope.apps:
                if BUNDLE_ID.match(entry):
                    given[entry] = entry
                    continue
                await self.wait_connected()
                try:
                    bundle, shown = await self._app(entry, partial=False)
                except Refused as e:
                    raise Refused("unsupported", f'the app list this agent was started with names "{entry}", which '
                                                 f"can't be used: {e}") from None
                given[bundle] = shown
            self._scopes[scope.apps] = given
        return self._scopes[scope.apps]

    async def _within(self, scope: "Scope", client_id: str, name: str, args: dict) -> dict:
        """One tool call in personal mode, failing closed. Nothing runs without an app list. Opening or closing an
        app outside the list is refused. Reading or touching is refused while another app is in front (asked of
        the phone before the call), and an answer never shows another app's screen."""
        if not scope.apps:
            raise Refused("unsupported", NO_APPS)
        given = await self._given(scope)
        names = ", ".join(given.values())
        if name in ("open_app", "close_app") and isinstance(args.get("app"), str) and args["app"].strip():
            if name == "close_app" and isinstance(self.stuck, AppNotAnswering):
                bundle, shown = self.stuck.bundle_id, f"{self.stuck.name} ({self.stuck.bundle_id})"
            else:
                await self.wait_connected()
                bundle, shown = await self._app(args["app"], partial=name == "open_app")
            if bundle not in given:
                raise Refused("unsupported", f"outside_apps: {shown} isn't one of the apps you were given ({names}).")
        elif name in ACTIONS and name not in SCOPE_FREE or name == "wait_for":
            # A look needs no question first: its own read says which app is in front, and an answer from
            # another app is withheld below. Touches, and wait_for (it reveals whether a text is there), ask.
            await self.wait_connected()
            front = await self._read(self.session.foreground_bundle_id)
            if front not in given:
                raise Refused("unsupported", _outside(front, self.session.app_names.get(front or ""), names))
        answer = await self._run_tool(client_id, name, args)
        if name == "apps":  # only its own apps: the rest of the phone is the owner's business
            lines = answer["text"].splitlines()
            kept = [ln for ln in lines if ln.split(" ", 1)[0] in given or not BUNDLE_ID.match(ln.split(" ", 1)[0])]
            return {**answer, "text": "\n".join(kept)}
        # The answer's own App: line says whose screen it lists; without one, the app at the last read does.
        shown = APP_LINE.search(answer["text"])
        app = (shown.group(1), shown.group(2)) if shown else self.session.app
        if app is not None and app[1] not in given and ("\n" in answer["text"] or answer.get("image")):
            if name == "look":  # nothing was done, so there is nothing to report but the refusal
                raise Refused("unsupported", _outside(app[1], app[0], names))
            first = answer["text"].splitlines()[0]
            first = "" if first.startswith("Screen ") else first + "\n"  # a look's first line is the screen itself
            return {"text": f"{first}Note: {_outside(app[1], app[0], names)}", "image": None, "image_size": None}
        return answer

    async def _run_tool(self, client_id: str, name: str, args: dict) -> dict:
        if name not in ACTIONS and name not in READS and name not in SPECIAL:
            raise Refused("unsupported", f"no tool named {name!r}; the tools are "
                                         f"{', '.join([*READS, *SPECIAL, *ACTIONS])}")
        missing = [k for k in REQUIRED.get(name, ()) if args.get(k) is None]
        if missing:
            raise Refused("bad_params", f"{name} needs {', '.join(missing)}")
        args = _checked(name, args)
        shot = self._shot_args(args, default=name == "look", custom=name == "look")  # bad width: refused here
        # A bad target was refused by _checked, before anything is queued or sent. Any tool whose
        # arguments carry `target` goes through here, so the live-read rule in act() covers it too.
        target = args.get("target")
        view = self.control.client(client_id)
        notes = [NOTE_PERSON] if view.seen < self.control.person_generation else []
        if name == "close_app" and isinstance(self.stuck, AppNotAnswering):
            return await self._close_stuck(self.stuck, args["app"], notes)
        await self.wait_connected()
        if name == "apps":
            return await self._apps(notes)
        if name == "wait":
            return await self._wait(view, args, notes, shot)
        if name == "wait_for":
            return await self._wait_for(view, args, notes, shot)
        if name == "look":
            await self._settle_pending()
            return await self._look(view, shot, [], notes)
        if self.state == "locked":
            raise Refused("locked", "the phone is locked; ask the user to unlock it")
        call, settle = ACTIONS[name]
        by_ref = target is not None and target.kind == "ref"
        # these tools at a point act on the element under it, so they need a current list too
        by_lookup = target is not None and (target.kind in ("id", "label", "text")
                                            or (name in ("type_text", "toggle", "scroll") and target.kind == "point"))

        fresh, full, box = [True], [False], {}

        async def act():
            self._require_connected()
            await self._quicken()
            s = self.session
            # a list read mid-redraw can miss elements while the screen looks unchanged since: a target missing
            # from it is read for again before it is not_found
            live = by_lookup and (not self._current(view) or (target.kind != "point" and self._missing(view.items, target)))
            if not live:  # a silent re-read settles for itself
                await self._settle_pending()
            s.items = view.items
            fresh[0] = view.seen == self.control.generation
            # never looked, a person acted, or the line dropped: the agent's list may be anything
            full[0] = not fresh[0] or view.dropped or view.read_at == float("-inf")
            if by_ref and view.dropped:
                raise Refused("stale", "the connection to the phone dropped since your last look; look again")
            if by_ref and not fresh[0]:
                raise Refused("stale", "the screen changed since your last look (someone else used the "
                                       "phone); look again")
            if live:
                # Resolve on the live tree. The agent's refs follow their elements onto it, so `e3` still
                # means the same element when it moved. The view's freshness doesn't change: a person's
                # action still makes refs stale, and `view.shown` (what the agent was told) stays put.
                if await self._silent_read(view) and self._missing(s.items, target):
                    # the app may have been mid-redraw: one more look, once it is still, before saying so
                    await self._silent_read(view)
            if by_lookup and target.kind != "point" and self._missing(s.items, target):
                self._refuse_behind_alert(view, force=False)  # an alert hides the app's elements: say so, not "not found"
                self._refuse_behind_sheet(target)  # before off_screen: scrolling can't help until the sheet is closed
                self._refuse_off_screen(target)
            box["note"] = await self._close_keyboard(view, name, target)  # a person would close it before the touch
            s.last_point = None
            box["mark"] = self._mark()  # where the stream stands as the gesture goes out
            handler = self._handlers.get(name)
            return await (handler(view, args) if handler else call(s, args))

        try:
            result = await self.control.agent(act)
        except ConnectionLost as e:
            self.lost(e)
            if isinstance(e, AppNotAnswering):  # the action went out, then the app stopped answering
                self._announce("agent", name, ok=False, code="not_answering")
                raise self._not_connected() from None
            self._announce("agent", name, ok=False, code="reconnecting")
            raise Refused("reconnecting", "the connection dropped during the action; look to check what "
                                          "happened") from None
        if isinstance(result, Scrolled) and result.note:
            notes = [*notes, result.note]
        if box.get("note"):  # tap, long_press, toggle and type_text carry theirs here, whatever they return
            notes = [*notes, box["note"]]
        if fresh[0]:  # its own action keeps what it knew current; after someone else's, it must look
            view.seen = self.control.generation
        if isinstance(result, Unchanged):  # nothing was done: nothing to settle or diff
            return _answer([str(result)], notes)
        reads_only = name == "alert" and args.get("action") == "text"  # it changes nothing on the phone
        looks = settle is not None and args.get("look", True) and not reads_only
        view.moved = view.moved or not looks
        if not reads_only:
            self.unsettled = not looks
        self._announce("agent", name, label=str(result), point=self._point(self.session.last_point))
        if not looks:
            return _answer([str(result)], notes)
        mark = box["mark"]  # the stream as the gesture went out; the settle only trusts frames after the return
        # frames during the call count for actions with the normal cap; a launch may show a still splash first
        done = await self._settle(self._mark()[0], cap=settle, since=mark[1], early=mark[0] if settle == CAP else None)
        if done.capped:
            notes = [*notes, NOTE_CAPPED.format(cap=settle)]
        if name == "back" and not done.changed:
            raise Refused("not_actionable", "no_action: nothing changed after back.")
        # a toggle that changed nothing on the stream is not trusted: the tree says whether the switch flipped
        if (not done.changed and name != "toggle" and not (full[0] or view.moved)
                and (mark[1] is view.read_mark[1] or alike(mark[1], view.read_mark[1]))):
            # no frame changed after the call returned, and the picture at its start is the one the agent's
            # list was read against (nothing changed silently before it): no tree read, the list stays
            view.read_at, view.read_mark = self.control.clock(), self._mark()
            return await self._add_shot(_answer([str(result), changes.SAME_SCREEN, changes.NO_CHANGE,
                                                 settled_line(done.seconds)], notes), view, shot, at_read=False)
        answer = await self._after(view, str(result), notes, full[0], name in MOVES_CONTENT, done.seconds)
        if isinstance(result, Toggled):
            # say only what the new list shows; if the switch can't be found again, claim nothing
            old = result.before
            now = next((e for e in view.items if e.ref == result.ref and (e.role, e.label, e.identifier)
                        == (old.role, old.label, old.identifier)), None)
            if now is not None:
                answer["text"] = answer["text"].replace(str(result), result.verdict(now, bool(view.alert)), 1)
        if isinstance(result, Typed):  # the value as the new list shows it, not as it was typed
            now = next((e for e in view.items if result.same_field(e)), None)
            if now is not None:
                answer["text"] = answer["text"].replace(str(result), result.verdict(now), 1)
        return await self._add_shot(answer, view, shot)

    async def _apps(self, notes: list) -> dict:
        apps = await self._read(self.session.apps)
        if not apps:
            return _answer(["No apps installed by you."], notes)
        try:
            front = await self._read(self.session.foreground_bundle_id)
        except Refused:
            raise
        except Exception:
            front = None  # the list is still right without the marker
        lines = [app_line(a) + (" *" if a["bundle_id"] == front else "") for a in apps]
        return _answer([*lines, "* = in front."], notes)

    async def _app(self, query: str, partial: bool = True) -> tuple[str, str]:
        """(bundle id, how to show it) for what the agent named, in this order: an installed bundle id; a
        name, ignoring case, among the installed apps (exact, then, if `partial`, the start of a name); a
        built-in app's name; a bundle-id-looking string as given (built-in apps aren't in `apps`). Parts
        of bundle ids and of later words never match, so "Notes" doesn't hit "Sticky Notes"."""
        q = query.strip()
        apps = await self.session.apps()
        low = q.lower()
        for a in apps:
            if a["bundle_id"] == q:
                return q, f"{a['name']} ({q})"
        hits = [a for a in apps if a["name"].lower() == low]
        builtin = next((n for n in SYSTEM_APPS if n.lower() == low), None)
        if not hits and builtin:
            return SYSTEM_APPS[builtin], f"{builtin} ({SYSTEM_APPS[builtin]})"
        if not hits and partial:
            hits = [a for a in apps if a["name"].lower().startswith(low)]
        if not hits:
            if BUNDLE_ID.match(q):
                return q, q  # launching it says whether it exists
            raise Refused("not_found", f'no installed app is named "{q}"; call apps to see them')
        if len(hits) > 1:
            raise Refused("ambiguous", f'{len(hits)} installed apps match "{q}". Pick one by its bundle id:\n'
                                       + "\n".join(app_line(a) for a in hits[:10]))
        return hits[0]["bundle_id"], f"{hits[0]['name']} ({hits[0]['bundle_id']})"

    async def _open_app(self, view, a: dict) -> str:
        bundle_id, shown = await self._app(a["app"])
        try:
            await self.session.launch(bundle_id)
        except ConnectionLost:
            raise
        except DeviceError as e:
            raise Refused("not_found", f"couldn't open {bundle_id}: {e}; call apps for the installed apps") from None
        return f"Opened {shown}."

    async def _close_app(self, view, a: dict) -> str:
        bundle_id, shown = await self._app(a["app"], partial=False)
        await self.session.terminate(bundle_id)
        return f"Closed {shown}."

    async def _close_stuck(self, stuck: AppNotAnswering, query: str, notes: list) -> dict:
        """close_app while the app in front is frozen: end its processes through iOS (WebDriverAgent's own
        terminate would wait on the app), then connect again at once. Only that app: the rest can wait."""
        if self.control.paused:
            raise Refused("paused", "paused by the user in the live view; ask them to resume")
        if query.strip().lower() not in (stuck.bundle_id.lower(), stuck.name.lower()):
            raise self._not_connected()
        device = self.session.device
        if device.rsd is None:
            raise self._not_connected()
        await device.kill_app(stuck.bundle_id)
        self.log(f"closed {stuck.bundle_id}, which wasn't answering")
        self.stuck = None
        self._wake.set()
        self._announce("agent", "close_app", label=stuck.name)
        return _answer([f"Closed {stuck.name} ({stuck.bundle_id}), which wasn't answering."], notes)

    async def _wait(self, view, args: dict, notes: list, shot) -> dict:
        """Sleep outside the phone's queue (a person may act meanwhile), then answer with what changed."""
        seconds = args["seconds"]
        await self.control.sleep(seconds)
        result = f"Waited {seconds:.1f} s."
        if not args.get("look", True):
            return _answer([result], notes)
        full = view.seen != self.control.generation or view.dropped or view.read_at == float("-inf")
        answer = await self._after(view, result, notes, full)  # no `Settled in`: the app did nothing
        return await self._add_shot(answer, view, shot)

    async def _wait_for(self, view, args: dict, notes: list, shot) -> dict:
        """Poll the fast tree (outside the phone's queue between reads) until the text, id or label is
        on the screen (or, with `gone`, no longer is), then answer with what changed. No `Settled in`."""
        kind = next(k for k in WAIT_FOR_KINDS if args.get(k) is not None)
        target = ui.parse_target({kind: args[kind]})
        gone, timeout = bool(args.get("gone", False)), float(args.get("timeout", WAIT_FOR_DEFAULT))
        clock = self.control.clock
        began = clock()
        timed_out = Refused("timeout", f'Waited {timeout:g} s for {kind} "{args[kind]}" to '
                                       f"{'disappear' if gone else 'appear'}; it didn't happen.")
        while True:
            left = timeout - (clock() - began)
            if left < WAIT_FOR_MIN_LEFT:
                raise timed_out
            # The read gets its own full deadline, so a slow phone isn't taken for a dead one; wait_for's
            # deadline is kept by cancelling the read, which answers `timeout` and leaves the link alone.
            try:
                tree = await asyncio.wait_for(
                    self._read(lambda: self.session.refresh(timeout=WAIT_FOR_READ_MAX)), left)
            except asyncio.TimeoutError:
                raise timed_out from None
            if self._missing(tree.items, target) == gone:
                break
            left = timeout - (clock() - began)
            if left < WAIT_FOR_MIN_LEFT:
                raise timed_out
            await self.control.sleep(min(WAIT_FOR_POLL, left))
        result = f"Done after {clock() - began:.1f} s."
        if not args.get("look", True):
            return _answer([result], notes)
        full = view.seen != self.control.generation or view.dropped or view.read_at == float("-inf")
        # the last poll is the answer's tree: no read of its own, only the app lookup the polls left out
        snap = await self._read(lambda: self.session.snapshot_from(tree))
        return await self._add_shot(await self._after(view, result, notes, full, snap=snap), view, shot)

    async def _type_text(self, view, a: dict) -> Typed:
        """Type into the target, or into the only input on screen (the fast tree has no focus attribute).
        The target is tapped first by the session, which waits for the keyboard and for focus to arrive."""
        target = a.get("target")
        if target is None:
            await self._silent_read(view)  # which inputs there are must be read now
            inputs = [e for e in view.items if e.role == "input"]
            if not inputs:
                raise Refused("not_found", "No input on screen")
            if len(inputs) > 1:
                shown = "\n".join([*(f"  {e.line()}" for e in inputs[:ui.MAX_CANDIDATES]),
                                   *ui.more_candidates(len(inputs), ui.MAX_CANDIDATES),
                                   'Retry with one of the refs, or add "index" (0-based).'])
                raise Refused("ambiguous", f"{len(inputs)} inputs on screen; pass a target to pick one:\n"
                                           f"Candidates:\n{shown}")
            field = inputs[0]
        elif target.point is not None:
            x, y = target.point
            field = ui.at_point(view.items, target.point, "input")
            if field is None:
                raise Refused("not_actionable", f"no_action: Nothing at ({round(x)}, {round(y)}) takes typing.")
        else:
            field = ui.resolve(view.items, target)
            if field.role != "input":
                raise Refused("not_actionable", f"no_action: {field.line(actions=False)} is not an input")
        self._guard(view, field)  # the focusing tap is a touch: refused where tap would be, and made where it shows
        await self.session.type_text(a["text"], replace=bool(a.get("replace", True)), submit=bool(a.get("submit", False)),
                                     target=ui.Target(ref=field.ref), at=self._touch_point(field))
        done = Typed(f"Typed into {field.line(actions=False)}.")
        done.field = field
        return done

    async def _scroll(self, view, a: dict) -> Scrolled:
        """Swipe through the centre of the scroll area (the innermost one around the target, the point or the
        screen's centre), `amount` of its height (or width), between the bars. `to` repeats that until a swipe
        leaves the settled screen as the one before left it (a rubber band or a spinner moves the screen
        meanwhile, so "no change at all" would never come), at most MAX_SCROLL_SWIPES times or SCROLL_CAP seconds."""
        s = self.session
        target, to = a.get("target"), a.get("to")
        if target is None:
            area = ui.scroll_container(view.items, s.screen)
        elif target.point is not None:
            x, y = target.point
            area = ui.scroll_at(view.items, target.point)
            if area is None:
                raise Refused("not_actionable", f"no_action: Nothing at ({round(x)}, {round(y)}) takes a scroll.")
        else:
            hit = ui.resolve(view.items, target)
            area = ui.scroll_of(view.items, hit)
            if area is None:
                raise Refused("not_actionable", f"no_action: {hit.line(actions=False)} is not inside a scroll view")
        direction = a.get("direction") or (SCROLL_TO[to][0] if to else "down")  # _checked made `to` and it agree
        top, bottom = s.last_insets
        band = (SCROLL_TOP_INSET if top is None else top,
                s.screen[1] - (SCROLL_BOTTOM_INSET if bottom is None else bottom))
        x0, y0, x1, y1 = ui.scroll_area_points(direction, area.rect if area else (0.0, 0.0, *s.screen),
                                               a.get("amount", 0.8), band)
        loop = asyncio.get_running_loop()
        began, previous, note = loop.time(), self._mark()[1], ""
        for swipes in range(1, MAX_SCROLL_SWIPES + 1):
            seq, since = self._mark()
            await s.scroll(x0, y0, x1, y1)
            if not to or swipes == MAX_SCROLL_SWIPES:
                break
            await self._settle(seq, cap=RESOLVE_SETTLE, since=since)
            settled = self._mark()[1]
            if alike(settled, previous):
                break  # the swipe left the screen as the one before did: the end of the list
            previous = settled  # (with no stream there is nothing to compare: every swipe is made)
            if loop.time() - began >= SCROLL_CAP:
                note = f"stopped after {SCROLL_CAP:g} s; the list may keep loading."
                break
        done = Scrolled(f"Scrolled {area.line(actions=False) if area else 'the screen'} {direction}.")
        done.note = note
        return done

    async def _outside_focus(self, view, name: str, target: ui.Target, keyboard_top: float) -> bool:
        """Is the touch's target something other than the focused input, so the keyboard is in the way of it
        (AGENT-TOOLS v1.4)? False for the focused input itself, an element in an OS alert, a point on the
        keyboard (a key), and whatever the tool is about to refuse (nothing is touched then)."""
        items = view.items
        if target.point is not None:
            x, y = target.point
            if name != "type_text" and y >= keyboard_top:
                return False
            if any(e.in_alert and e.x <= x < e.x + e.width and e.y <= y < e.y + e.height for e in items):
                return False
            field = ui.at_point(items, target.point, "input")
        else:
            try:
                field = ui.resolve(items, target)
            except Refused:
                return False
            if isinstance(field, tuple) or field.in_alert:
                return False
        if name == "type_text" and (field is None or field.role != "input"):
            return False  # it refuses typing into this itself
        return field is None or not await self.session.is_focused(field)  # a non-input is outside by definition

    async def _close_keyboard(self, view, name: str, target: ui.Target | None) -> str:
        """Before a touch (or a focusing tap) on something other than the focused input while the keyboard is up:
        close it the way a person does, since the first tap outside the field only closes it and the keyboard
        covers what is near the bottom. One touch where nothing responds, then WebDriverAgent's own dismissal
        if it is still up; neither presses return. Gives the note when it ended up closed, else leaves the
        screen to the usual `covered` rules."""
        s = self.session
        if name not in KEYBOARD_TOOLS or target is None or not view.read_keyboard or view.alert_now is not None:
            return ""  # (an OS alert in front is modal: a touch outside it closes nothing, and behind_alert decides)
        top, bottom = s.last_insets
        top = SCROLL_TOP_INSET if top is None else top
        bottom = s.screen[1] - (SCROLL_BOTTOM_INSET if bottom is None else bottom)
        if not await self._outside_focus(view, name, target, bottom):
            return ""
        if (blank := ui.blank_point(view.items, s.screen, top, bottom)) is not None:
            await s.tap(ui.Target(x=blank[0], y=blank[1]))
            await self._silent_read(view)
        if view.read_keyboard and await s.dismiss_keyboard():
            await self._silent_read(view)
        return "" if view.read_keyboard else NOTE_KEYBOARD

    @staticmethod
    def _refuse_behind_alert(view, force: bool = True) -> None:
        """When the newest read shows an OS alert: `behind_alert`. `force` says whether passing it could help
        (it can't touch an element that isn't found)."""
        if view.alert_now is not None:
            title, buttons = view.alert_now
            alert = " ".join([json.dumps(title, ensure_ascii=False), *(f"[{b}]" for b in buttons)])
            raise Refused("not_actionable", f"behind_alert: an alert covers it: {alert}; answer it with alert, "
                                            + ("or pass force: true." if force else "or ask the user."))

    def _refuse_behind_sheet(self, target: ui.Target) -> None:
        """A target on no listed element but on one an open sheet (or another layer) hides: `covered`, naming what
        is in front, not `not_found`. It isn't on the list, so `force` has nothing to touch."""
        behind = getattr(self.session, "behind", [])
        try:
            hit = ui.resolve([e for e, _ in behind], dataclasses.replace(target, index=0))
        except Refused:
            return
        front = next((line for e, line in behind if e is hit), None)
        if front is not None:
            raise Refused("not_actionable", f"covered: {hit.line(ref=False, actions=False)} is behind {front}; "
                                            "close that first.")

    def _refuse_off_screen(self, target: ui.Target) -> None:
        """A target on no listed element but on one the last read found past the screen (or its scroll view's
        edge): `off_screen`, not `not_found`. A touch can't reach it, so `force` doesn't either."""
        try:
            hit = ui.resolve(getattr(self.session, "offscreen", []), dataclasses.replace(target, index=0))
        except Refused:
            return
        if isinstance(hit, ui.Element):
            raise Refused("not_actionable", f"off_screen: {hit.line(ref=False, actions=False)} is off screen; "
                                            "scroll it into view first.")

    @staticmethod
    def _refuse_disabled(element: ui.Element) -> None:
        if not element.enabled:
            raise Refused("not_actionable", f"disabled: {element.line(actions=False)} is disabled.")

    def _guard(self, view, element: ui.Element) -> None:
        """A9: refuse what a real touch couldn't do. The reason word comes first in the message."""
        self._refuse_disabled(element)
        if not element.in_alert:
            self._refuse_behind_alert(view)

    def _touch_point(self, element: ui.Element) -> tuple[float, float] | None:
        """Where a finger would touch an element a bar (or the keyboard) is drawn over its centre: the middle of
        the part that shows after every overlapping bar is cut away, nearest the centre. None when no bar
        covers the centre. Refuses when too little shows."""
        centre = element.center
        over = [b for b in element.covered_by if b.rect[0] <= centre[0] < b.rect[0] + b.rect[2]
                and b.rect[1] <= centre[1] < b.rect[1] + b.rect[3]]
        if not over:
            return None
        x, y, w, h = ui.visible_part(element.rect, element.covered_by, self.session.screen)
        if w >= MIN_VISIBLE and h >= MIN_VISIBLE:
            return x + w / 2, y + h / 2
        line = element.line(actions=False)
        if over[0].kind == "TabBar":
            raise Refused("not_actionable", f"under_tab_bar: {line} is under the tab bar iOS draws; scroll it up first.")
        if over[0].kind in ui.COVERING_BARS:
            raise Refused("not_actionable", f"covered: {line} is covered by {over[0].line}; scroll it into view first.")
        raise Refused("not_actionable", f"covered: {line} is covered by {over[0].line}; move or close it first.")

    async def _touch(self, view, a: dict, hold_ms: int) -> str:
        """`tap` and `long_press`: refuse a disabled element or one behind an alert, and touch the part of an
        element under a bar that shows. `force` skips all of it. A point is a raw touch."""
        s, target = self.session, _target(a)
        verb = "Long-pressed" if hold_ms > 300 else "Tapped"
        found = ui.resolve(s.items, target)
        if isinstance(found, tuple):
            return await s.tap(target, hold_ms=hold_ms)
        self._refuse_disabled(found)  # even `force` doesn't touch what the app has switched off
        if a.get("force"):
            return await s.tap(target, hold_ms=hold_ms)
        self._guard(view, found)
        if (point := self._touch_point(found)) is None:
            return await s.tap(target, hold_ms=hold_ms)
        await s.tap(ui.Target(x=point[0], y=point[1]), hold_ms=hold_ms)
        return f"{verb} {found.line(actions=False)} at its visible part ({round(point[0])}, {round(point[1])})."

    async def _tap(self, view, a: dict) -> str:
        return await self._touch(view, a, TAP_MS)

    async def _long_press(self, view, a: dict) -> str:
        return await self._touch(view, a, int(float(a.get("seconds", 1.0)) * 1000))

    async def _toggle(self, view, a: dict) -> str:
        target = _target(a)
        if target.point is not None:
            x, y = target.point
            found = ui.at_point(view.items, target.point, "switch")
            if found is None:
                raise Refused("not_actionable", f"no_action: Nothing at ({round(x)}, {round(y)}) takes a toggle.")
        else:
            found = self._switch_first(view.items, target)
        line = found.line(actions=False)
        if found.role != "switch" or found.checked is None:
            raise Refused("not_actionable", f"no_action: {line} is not a switch")
        self._refuse_disabled(found)
        point = None
        if not a.get("force"):
            self._guard(view, found)
            point = self._touch_point(found)
        want = a.get("value")
        if want is not None and found.checked == want:
            return Unchanged(f"{line} is already {'true' if want else 'false'}.")
        await self.session.tap(ui.Target(ref=found.ref) if point is None else ui.Target(x=point[0], y=point[1]))
        # Until the tree is read, claim no new state: the first line is the verdict's to write
        where = "" if point is None else f" at its visible part ({round(point[0])}, {round(point[1])})"
        done = Toggled(f"Tapped {dataclasses.replace(found, checked=None, value='').line(actions=False)}{where}.")
        done.ref, done.before, done.want, done.flipped = found.ref, found, want, not found.checked
        return done

    @staticmethod
    def _switch_first(items: list[ui.Element], target: ui.Target) -> ui.Element:
        """The element a target names, preferring a switch: a row and its switch often share a label."""
        if target.ref is None:
            try:
                return ui.resolve([e for e in items if e.role == "switch"], target)
            except Refused as e:
                if e.code != "not_found":
                    raise  # several switches match: the agent picks one
        return ui.resolve(items, target)

    async def _back(self, view, a: dict) -> str:
        """Close an open popover by a touch outside it (AGENT-TOOLS v1.4); else tap the navigation bar's back
        button. A bar without one is a root screen (or has only Cancel,
        Done and the like): nothing is tapped. The edge swipe is only for screens with no bar at all."""
        s = self.session
        back = await s.back_point()
        if back.dismiss is not None:  # AGENT-TOOLS v1.4: what is open on top closes first, as "up one screen"
            await s.tap(ui.Target(x=back.dismiss[0], y=back.dismiss[1]))
            return "Closed the sheet."
        if back.point is not None:
            await s.tap(ui.Target(x=back.point[0], y=back.point[1]))
            return "Tapped the back button."
        if back.bar:
            raise Refused("not_actionable", "no_action: the navigation bar has no back button")
        width, height = s.screen
        await s.swipe(2, height / 2, 0.6 * width, height / 2, 300)
        return "Swiped from the left edge."

    async def _settle_pending(self) -> None:
        """XCTest no longer waits for the app to go idle, so after an action that wasn't settled (`look: false`)
        nothing may act or read until the screen is still (cap RESOLVE_SETTLE)."""
        if self.unsettled:
            await self._settle(self._mark()[0], cap=RESOLVE_SETTLE)
            self.unsettled = False

    async def _silent_read(self, view) -> bool:
        """Read the live tree for target resolution, once the screen is still (cap RESOLVE_SETTLE; with no
        frames that is a `settle_fallback` wait, 0.6 s). Read mid-redraw, an element can be briefly missing.
        True when a re-read could show something else: the stream moved, ended capped, or has no frames."""
        s = self.session
        done = await self._settle(self._mark()[0], cap=RESOLVE_SETTLE)
        self.unsettled = False
        mark = self._mark()  # taken before the read: a frame that changes meanwhile counts against the list
        tree = await s.refresh()
        view.alert_now = _alert_key(tree.alert)  # the alert as this read shows it, not as the last answer did
        view.read_keyboard = tree.keyboard
        known = {e.ref for e in view.items}  # what came back may get its old ref: look in both lists
        held = sorted([*view.items, *(e for e in view.shown if e.ref not in known)], key=lambda e: (e.y, e.x))
        view.items = changes.carry_refs(held, s.items, view.top_ref, moves_content=True)  # the screen may be anything
        view.top_ref = max(view.top_ref, changes.highest(view.items))
        view.read_at, view.read_mark = self.control.clock(), mark  # the next target in the same window needn't read again
        s.items = view.items
        return done.capped or mark[1] is None or not alike(self._mark()[1], mark[1])

    @staticmethod
    def _missing(items: list[ui.Element], target: ui.Target) -> bool:
        """Does the target resolve to `not_found` (not `ambiguous`) on this list?"""
        try:
            ui.resolve(items, target)
        except Refused as e:
            return e.code == "not_found"
        return False

    async def settle(self, after_seq: int, still: float | None = None, cap: float = CAP,
                     since: Frame | None = None, early: int | None = None) -> tuple[float, bool]:
        """(seconds waited, changed): see _settle."""
        done = await self._settle(after_seq, still, cap, since, early)
        return done.seconds, done.changed

    async def _settle(self, after_seq: int, still: float | None = None, cap: float = CAP,
                      since: Frame | None = None, early: int | None = None) -> Settled:
        """Wait for the screen to be still, as the stream shows it. `after_seq` is the newest frame when the
        phone call returned: the settle ends only on a later frame, since earlier ones may predate the effect.
        WebDriverAgent streams continuously even when nothing changes, so a silence is a stall, not stillness.

        `early` is the newest frame when the gesture went out. A tap's call returns about 200 ms after its effect
        shows, so the frames in between count too, but only once one of them is unlike `since`: they can then
        already hold the still run, and the settle ends at the first frame after the return that continues it.

        Still: at least 2 frames alike (stream.alike) spanning `still` seconds (at least 2.5 median frame gaps,
        so a slow stream isn't called still too soon). Each frame is compared with the first of its run, so a
        slow fade, close to the last frame every time, is not still. `changed`: a frame is unlike the one at the
        start of the action (`since`, from _mark; None means there was no frame, so any frame counts).
        `changed` is False only when the answer is certain: 3 frames, all like `since`, and `quiet` seconds.
        A stall, no frames at all (it sleeps `settle_fallback` in total, first-frame wait included), too
        few frames, or the cap all report changed=True, so the tree gets read."""
        gap = self.hub.gap()
        still = max(self.settle_still if still is None else still, 2.5 * gap if gap else 0.0)
        stall = max(3 * gap if gap else 0.0, still)
        loop = asyncio.get_running_loop()
        began = loop.time()
        deadline = began + cap
        seq, differs, run, run_start, first_of_run = after_seq, False, 0, began, None
        if early is not None:
            last = None
            for arrived, frame in self.hub.since(early, upto=after_seq):
                differs = differs or not alike(frame, since)
                if alike(frame, first_of_run):
                    run += 1
                else:
                    run, run_start, first_of_run = 1, arrived, frame
                last = arrived
            if not differs:  # nothing showed during the call: only frames after the return count
                run, run_start, first_of_run = 0, began, None
            elif (gap and last is not None and run >= 2 and began - run_start >= still
                  and began - last <= 1.5 * gap):
                return Settled(0.0, True, False)  # changed, then still up to a fresh frame at the return
        while (left := deadline - loop.time()) > 0:
            first = seq == after_seq
            wait = min(left, self.settle_first_frame if first else stall)
            frame = await self.hub.next_after(seq, timeout=wait)
            now = loop.time()
            if frame is None:
                if not first and wait == left:
                    break  # the cap came before the stall did
                if first:  # no frames at all: the stream is down or still opening
                    await asyncio.sleep(max(0.0, self.settle_fallback - (now - began)))
                return Settled(loop.time() - began, True, False)  # or a stall
            seq = frame.seq
            differs = differs or not alike(frame, since)
            if alike(frame, first_of_run):
                run += 1
            else:
                run, run_start, first_of_run = 1, now, frame
            if run >= 2 and now - run_start >= still:
                if differs or (seq - after_seq >= 3 and now - began >= self.settle_quiet):
                    return Settled(now - began, differs, False)
        return Settled(loop.time() - began, True, differs)

    def _mark_read(self, view) -> None:
        """A tree read starts: note the stream's picture (its SHA-1 and, if it's recent, its bytes), so a
        screenshot shows the moment the list is of, not a later one."""
        view.read_mark = self._mark()
        frame = self.hub.latest
        fresh = frame is not None and self.hub.now_ms() - frame.at <= self.shot_fresh * 1000
        view.read_frame = frame.jpeg if fresh else None

    def _mark(self) -> tuple[int, Frame | None]:
        """(seq, the newest frame): where the stream stood at this moment."""
        frame = self.hub.latest
        return (frame.seq, frame) if frame else (0, None)

    def _shot_args(self, args: dict, *, default: bool, custom: bool) -> tuple[int, bool] | None:
        """(width, labels) when a screenshot is wanted, else None. Only `look` takes width and labels;
        an action's screenshot uses look's defaults (AGENT-TOOLS §3.5)."""
        if not custom:
            return (shotlib.DEFAULT_WIDTH, False) if args.get("screenshot", default) else None
        width = args.get("width", shotlib.DEFAULT_WIDTH)
        if isinstance(width, bool) or not isinstance(width, (int, float)) or width != int(width):
            raise Refused("bad_params", "width must be a whole number of pixels")
        if not shotlib.MIN_WIDTH <= width <= shotlib.MAX_WIDTH:
            raise Refused("bad_params", f"width must be {shotlib.MIN_WIDTH}-{shotlib.MAX_WIDTH} pixels, not {width:g}")
        if not args.get("screenshot", default):
            return None
        return int(width), bool(args.get("labels", False))

    async def _newest_frame(self) -> bytes | None:
        """The newest stream frame if it's recent, else the next one if it comes soon, else None."""
        latest = self.hub.latest
        if latest is not None and self.hub.now_ms() - latest.at <= self.shot_fresh * 1000:
            return latest.jpeg
        frame = await self.hub.next_after(latest.seq if latest else 0, timeout=self.shot_wait)
        return frame.jpeg if frame else None

    async def _add_shot(self, answer: dict, view, shot: tuple[int, bool] | None, at_read: bool = True) -> dict:
        """Add the picture to an answer: from the stream (full resolution), else the phone's /screenshot.
        A picture that can't be had is a Note line, never an error. Decoding and resizing happen only here,
        so only when a picture was asked for. The picture is the frame from when the tree was read (`at_read`),
        else the newest one (the no-change shortcut read no tree); /screenshot only when there is no frame."""
        if shot is None:
            return answer
        width, labels = shot
        try:
            source = view.read_frame if at_read else await self._newest_frame()
            if source is None:
                source = await self._read(self.session.screenshot_png)
            boxes = [(e.ref, *e.rect) for e in view.items if e.actions] if labels else None
            jpeg, size, skipped = await asyncio.to_thread(shotlib.render, source, self.session.screen, width, boxes)
        except Exception as e:
            answer["text"] += f"\nNote: no screenshot: {str(e) or type(e).__name__}"
            return answer
        if skipped:
            answer["text"] += "\nNote: labels skipped: the screen orientation changed."
        answer["image"], answer["image_size"] = base64.b64encode(jpeg).decode(), [size[0], size[1]]
        return answer

    async def _look(self, view, shot: tuple[int, bool] | None, lines: list, notes: list) -> dict:
        async def read():
            self._mark_read(view)
            text, image = await self.session.look(screenshot=False)
            view.items = view.shown = self.session.items
            view.top_ref, view.app, view.seen = changes.highest(view.items), self.session.app, self.control.generation
            view.alert, view.keyboard = _alert_key(self.session.last_alert), self.session.last_keyboard
            view.read_keyboard = view.keyboard
            view.alert_now = view.alert
            view.read_at, view.moved, view.dropped = self.control.clock(), False, False
            return text, image
        try:
            text, image = await self._read(read)
        except Exception as e:
            if not lines:
                raise
            view.moved = True  # no list came back: the screen has probably changed
            return _answer(lines, [*notes, f"couldn't look afterwards: {e}."])
        return await self._add_shot(_answer([*lines, text], notes, image), view, shot)

    async def _after(self, view, result: str, notes: list, full: bool, moves_content: bool = False,
                     seconds: float | None = None, snap: Snapshot | None = None) -> dict:
        """An action's answer: its result line, then what changed on screen since the agent's last answer
        (`view.shown`), or the full list when the app changed or `full`, then `Settled in` (left out when
        `seconds` is None: `wait` did nothing to settle), then the notes. Refs follow their elements."""
        s = self.session
        tail = [] if seconds is None else [settled_line(seconds)]

        async def read():
            self._mark_read(view)
            read_now = snap or await s.snapshot()  # `snap`: a read the caller already made (wait_for's last poll)
            s.items = read_now.tree.items
            app_changed = read_now.app is not None and view.app is not None and read_now.app != view.app
            head = _furniture(view, read_now.tree.alert, read_now.tree.keyboard)
            up = changes.update(view.items, read_now.tree.items, app_changed, view.top_ref, view.shown, full, head,
                                moves_content)
            return read_now, up
        try:
            snap, up = await self._read(read)
        except Exception as e:
            view.moved = True  # no list came back: the screen has probably changed
            return _answer([result, *tail], [*notes, f"couldn't look afterwards: {e}."])
        covered = not snap.tree.items and bool(view.shown)  # the agent saw elements; now there are none
        s.items = view.items = view.shown = up.items
        view.top_ref, view.app = up.top, snap.app or view.app
        view.alert, view.keyboard = _alert_key(snap.tree.alert), snap.tree.keyboard
        view.read_keyboard = view.keyboard
        view.alert_now = view.alert
        view.seen, view.read_at, view.moved, view.dropped = (self.control.generation, self.control.clock(),
                                                             False, False)
        if up.full:
            tree = snap.tree
            head = ui.describe(up.items, s.screen, snap.scale, snap.app, tree.alert, tree.keyboard, tree.more)
            return _answer([result, head, *tail], [*notes, *([NOTE_COVERED] if covered else [])])
        return _answer([result, *up.lines, *tail], notes)

    def _current(self, view) -> bool:
        """Is the agent's last list still the screen? Not if a person or another agent acted since
        (a different generation), if an action returned no list since, or if it is older than
        LIVE_AGE seconds and the screen may have changed meanwhile."""
        if view.seen != self.control.generation or view.moved or view.dropped:
            return False
        return self.control.clock() - view.read_at < LIVE_AGE or self._screen_unchanged(view)

    def _screen_unchanged(self, view) -> bool:
        """Does the stream show the picture the list was read against? Only a fresh newest frame that
        matches the one at read time says so; with no stream the answer is no, and the list is read again."""
        frame, (_, was) = self.hub.latest, view.read_mark
        if frame is None or was is None or self.hub.now_ms() - frame.at > FRESH_FRAME * 1000:
            return False
        return alike(frame, was)

    async def _read(self, fn):
        """A read through the queue; after a dropped line, once more when reconnected."""
        for attempt in (1, 2):
            try:
                return await self.control.read(lambda: self._connected_only(fn))
            except ConnectionLost as e:
                self.lost(e)
                if attempt == 2:
                    raise Refused("reconnecting", "lost the connection to the phone; try again") from None
                await self.wait_connected()

    # ---- a person, from the live view ----
    async def viewer_message(self, message: dict) -> dict | None:
        """One message from a live view (docs/VIEWER-PROTOCOL.md). Returns an error message for
        that viewer, or None. Unknown types are ignored."""
        kind = message.get("type")
        try:
            if kind == "pause":
                self.control.set_paused(bool(message.get("on")))
                self.broadcast(self.status())
                return None
            if kind == "quality":
                await self.set_quality(str(message.get("preset")))
                return None
            if kind not in ("gesture", "scroll", "text", "key", "button"):
                return None  # "overlay" comes in part 2; anything else is unknown
            if self.state != "live":
                return {"type": "error", "code": {"locked": "locked", "not-answering": "not_answering"}.get(self.state, "reconnecting"),
                        "message": f"not connected to the phone ({self.detail or self.state})"}
            run, action, point = self._person_action(kind, message)
            self._person_at = time.monotonic()

            async def quickened():
                await self._quicken()  # so the picture after the input comes at full rate (V1)
                return await run()
            await self.control.person(lambda: self._connected_only(quickened))
            self._announce("person", action, point=self._point(point))
            return None
        except Refused as e:
            return {"type": "error", "code": e.code, "message": str(e)}
        except ConnectionLost as e:
            self.lost(e)
            return {"type": "error", "code": "reconnecting",
                    "message": "the connection dropped; your input may not have reached the phone"}
        except (ValueError, KeyError, TypeError, IndexError) as e:
            return {"type": "error", "code": "unsupported", "message": f"bad {kind} message: {e}"}
        except Exception as e:
            return {"type": "error", "message": str(e)[:300]}

    def _person_action(self, kind: str, m: dict):
        """(coroutine function to run, kind for the action message, start point in points)."""
        s = self.session
        if kind == "gesture":
            g = classify(m["points"], s.screen)
            if g.kind == "drag":
                return (lambda: s.path(list(g.points))), "drag", g.points[0][1:]
            _, x, y = g.points[0]
            return (lambda: s.tap(ui.Target(x=x, y=y), hold_ms=g.hold_ms)), g.kind, (x, y)
        if kind == "scroll":
            x0, y0, x1, y1 = ui.scroll_swipe(float(m["x"]), float(m["y"]), float(m.get("dx", 0)),
                                             float(m.get("dy", 0)), s.screen)
            return (lambda: s.scroll(x0, y0, x1, y1)), "scroll", (x0, y0)  # a wheel stops where it should
        if kind == "text":
            text = str(m["text"])
            if not text:
                raise ValueError("no text")
            return (lambda: s.type_text(text)), "text", None
        if kind == "key":
            name = str(m["key"])
            if name not in ("backspace", "enter"):
                raise ValueError(f"unknown key {name!r}")
            return (lambda: s.key(name)), name, None
        name = str(m["name"])
        if name not in BUTTONS:
            raise ValueError(f"unknown button {name!r}")
        return (lambda: s.press_button(name)), name, None

    async def set_quality(self, preset: str) -> None:
        if preset not in PRESETS:
            raise ValueError(f"preset must be one of {', '.join(PRESETS)}")
        if self.state in ("live", "locked"):
            await self._read(lambda: self.session.settings({**PRESETS[preset], **NO_IDLE_WAIT}))
            self._fps = int(PRESETS[preset]["mjpegServerFramerate"])  # full rate; the pace slows it again when still
        self.preset = preset
        if self.video is not None:
            self.video.set_quality(preset)
        self.broadcast(self.status())

    # ---- the stream and shutting down ----
    async def _open_stream(self):
        try:
            await asyncio.wait_for(self._connected.wait(), 30)
        except asyncio.TimeoutError:
            raise ConnectionLost("not connected to the phone yet") from None
        return await self.session.device.open_stream()

    async def start_video(self, tries: int = 3, gap: float = 3.0) -> str:
        """Set up the H.264 live view: the hub, the AirPlay receiver, and Screen Mirroring on the phone, started
        the way the owner would (mirror.py). The app that was in front comes back afterwards. Returns what
        happened in words; it never raises, since the JPEG stream stays as the picture."""
        from . import airplay, mirror, video
        s = self.session
        if not video.available():
            return self._video_note("PyAV isn't installed (pip install tapwright[video])")
        uxplay = airplay.find()
        if uxplay is None:
            return self._video_note("no AirPlay receiver found (install UxPlay, or set TAPWRIGHT_UXPLAY)")
        try:
            scale = s.scale or 3.0
            pixels = (round(s.screen[0] * scale), round(s.screen[1] * scale))
            receiver = airplay.Receiver(uxplay, 0, pixels, self.log)
            hub = video.VideoHub(port=0, screen_width=lambda: s.screen[0], mirroring=lambda: receiver.mirroring,
                                 log=self.log)
            receiver.args = airplay.command(uxplay, hub.port, pixels)
            receiver.start()
            self.video, self._receiver = hub, receiver
            hub.set_quality(self.preset)
            said = ""
            for attempt in range(tries):
                await asyncio.sleep(gap)  # the receiver takes a moment to show up in the phone's list
                async with self.control.exclusive():
                    self._require_connected()
                    front = await s.foreground_bundle_id()
                    try:
                        said = await mirror.start(s, airplay.NAME)
                    except mirror.MirrorError as e:
                        said = str(e)
                        continue
                    finally:
                        if front and front != "com.apple.springboard":
                            await s.launch(front)  # back to the app that was in front
                break
            for view in self.control.clients.values():
                view.dropped = True  # the phone left the app for a moment: every agent's list is old
            self.broadcast(self.status())
            return self._video_note(said, ok=receiver.mirroring or said.startswith(("Mirroring", "Already")))
        except Exception as e:  # the JPEG stream stays: video is an extra
            return self._video_note(f"couldn't start: {e}")

    def _video_note(self, text: str, ok: bool = False) -> str:
        self.log(f"video: {text}" if ok else f"video: {text}; the live view stays on the JPEG stream")
        return text

    async def close(self) -> None:
        await self.pump.stop()
        self.hub.close()
        if self._video_task is not None:
            self._video_task.cancel()
        if self._receiver is not None:
            await asyncio.to_thread(self._receiver.stop)
        if self.video is not None:
            await asyncio.to_thread(self.video.close)  # joins its threads
        try:
            await asyncio.wait_for(self.session.close(), 10)
        except Exception as e:
            self.log(f"closing: {e}")

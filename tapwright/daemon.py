"""The phone side of `tapwright serve`: one Session (tunnel and WebDriverAgent), the control
rules, the stream, the agent tools, a person's input from the live view, and a supervisor
that keeps it all connected. server.py only turns HTTP into calls here."""
import asyncio
import base64
import hashlib
import io
import math
import time
import traceback

from PIL import Image, UnidentifiedImageError

from . import changes, ui
from .control import Controller, Refused, classify
from .device import ConnectionLost, DeviceError, NotSetUp, PhoneNotFound
from .stream import PRESETS, FrameHub, StreamPump

PROTOCOL = 1
ACCEPTS = ["tap", "longPress", "drag", "scroll", "text", "enter", "backspace", "home", "volume", "quality"]
BUTTONS = ("home", "volumeUp", "volumeDown")
STILL = 0.1  # seconds without a changed frame before a screen counts as still (docs/AGENT-TOOLS.md §6)
CAP = 2.0  # the longest an action's answer waits for the screen
LONG_CAP = 4.0  # the same after launching an app or opening a URL
LIVE_AGE = 2.0  # seconds a list stays current for id, label and text targets (docs/AGENT-TOOLS.md §2)
FRESH_FRAME = 2.0  # seconds a stream frame counts as "the screen now"
NOTE_PERSON = "a person used the phone since your last look."
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
    if name == "scroll" and checked.get("direction", "down") not in ("up", "down", "left", "right"):
        raise Refused("bad_params", f"direction must be up, down, left or right, not {args['direction']!r}")
    return checked


def _target(a: dict) -> ui.Target:
    return a["target"]  # parsed by _checked; tap and long_press require it


def _field(a: dict) -> ui.Target | None:
    """type_text's target: an element to tap first, or none for the field that has focus."""
    return a.get("target")


# Agent actions: tool -> (what it does on the session, seconds to settle before the look; None: no look)
ACTIONS = {
    "launch_app": (lambda s, a: s.launch(a["bundle_id"]), LONG_CAP),
    "terminate_app": (lambda s, a: s.terminate(a["bundle_id"]), None),
    "tap": (lambda s, a: s.tap(_target(a)), CAP),
    "long_press": (lambda s, a: s.tap(_target(a), hold_ms=int(float(a.get("seconds", 1.0)) * 1000)), CAP),
    "type_text": (lambda s, a: s.type_text(a["text"], replace=bool(a.get("replace")), submit=bool(a.get("submit")),
                                           target=_field(a)), CAP),
    "scroll": (lambda s, a: s.scroll(a.get("direction", "down"), float(a.get("amount", 0.5))), CAP),
    "swipe": (lambda s, a: s.swipe(float(a["x0"]), float(a["y0"]), float(a["x1"]), float(a["y1"]),
                                   int(float(a.get("seconds", 0.35)) * 1000)), CAP),
    "press_button": (lambda s, a: s.press_button(a["name"]), CAP),
    "alert": (lambda s, a: s.alert(a["action"]), CAP),
    "open_url": (lambda s, a: s.open_url(a["url"]), LONG_CAP),
}
MOVES_CONTENT = ("scroll", "swipe", "back", "open_app", "launch_app", "open_url", "press_button")  # identical elements can't be told apart after these
READS = ("look", "list_apps", "wait")
REQUIRED = {"launch_app": ("bundle_id",), "terminate_app": ("bundle_id",), "type_text": ("text",),
            "tap": ("target",), "long_press": ("target",),
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
                 settle_first_frame: float = 1.0, settle_fallback: float = 0.6) -> None:
        self.session = session
        # settle: how long a still screen is quiet, how long to wait for a first frame, and the
        # blind sleep used when the stream gives no frames at all
        self.settle_still, self.settle_first_frame, self.settle_fallback = settle_still, settle_first_frame, settle_fallback
        self.control = control or Controller()
        self.hub = FrameHub()
        self.pump = StreamPump(self._open_stream, self.hub, linger=stream_linger, retry=stream_retry, log=log)
        self.log = log
        self.heartbeat, self.backoff, self.ready_wait = heartbeat, backoff, ready_wait
        self.state, self.detail = "connecting", None
        self.preset = "balanced"
        self.ws_clients = 0
        self.last_request = time.monotonic()
        self._ever_connected = False
        self._connected = asyncio.Event()
        self._lost = asyncio.Event()
        self._queues: set = set()

    # ---- status and events ----
    def status(self) -> dict:
        w, h = self.session.screen
        via = (getattr(self.session.device, "via", None) or "").lower()
        via = ("usb" if via == "usb" else "wifi" if via == "local network"
               else "tailscale" if via.startswith("network") else None)
        return {"type": "status", "protocol": PROTOCOL, "state": self.state, "detail": self.detail, "via": via,
                "fps": self.hub.fps(), "driver": self.control.driver, "paused": self.control.paused,
                "size": {"w": w, "h": h}, "accepts": ACCEPTS, "quality": self.preset}

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
    def lost(self, reason: str) -> None:
        """A request found the line dead: the supervisor reconnects."""
        if self.state in ("live", "locked"):
            self.log(f"connection lost: {reason}")
            self._set_state("reconnecting", reason[:200])
        for view in self.control.clients.values():
            view.dropped = True  # every agent's list is from before the drop
        self._lost.set()

    def _not_connected(self) -> Refused:
        """No phone found or not set up: the agent can't fix that by waiting. Otherwise the link
        is coming back."""
        if self.state == "no-device":
            return Refused("not_connected", f"no phone to drive ({self.detail}); fix that, then try again")
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
        if self.state == "no-device":
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
                except NotSetUp:
                    raise
                except Exception as e:
                    self.log(f"restarting WebDriverAgent didn't work ({e}); reconnecting")
                    await s.reset()
                    await s.ensure()
            else:
                await s.ensure()
            await s.settings(PRESETS[self.preset])
            await s.read_scale(refresh=True)  # once per connection, so every header says @3x, not only SpringBoard's

    async def supervise(self, stop: asyncio.Event) -> None:
        """Connect, then keep checking; reconnect whenever the line drops. Runs until `stop`."""
        attempt = failures = 0
        while not stop.is_set():
            if self.state not in ("live", "locked"):
                try:
                    await self._connect()
                except NotSetUp as e:
                    self._set_state("no-device", f"needs setup: {e}")
                except PhoneNotFound:
                    self._set_state("no-device", "phone not found: plug it in and unlock it")
                except Exception as e:
                    self._set_state("reconnecting" if self._ever_connected else "connecting", str(e)[:200])
                else:
                    attempt = failures = 0
                    self._ever_connected = True
                    self._lost.clear()  # a drop noticed while reconnecting is already dealt with
                    self._set_state("live")
                    self.log(f"connected over {self.session.device.via}")
                    continue
                await _first(self.backoff[min(attempt, len(self.backoff) - 1)], stop)
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
                self.lost(str(e))
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
    async def run_tool(self, client_id: str, name: str, args: dict) -> dict:
        """One agent tool call: {"text": str, "image": base64 JPEG or None, "image_size": [w, h] or None}.
        Raises Refused with a code from errors.CODES; any other failure is `internal` (logged)."""
        self.last_request = time.monotonic()
        try:
            # The stream is held open for the length of every tool call; the pump's own linger keeps
            # it open for 30 s after the last one, so the settle always has frames to read.
            async with self.pump.watching():
                return await self._run_tool(client_id, name, args)
        except Refused:
            raise
        except DeviceError as e:  # WebDriverAgent or the link answered with an error; not our bug
            self.log(f"{name}: the phone side failed: {e}")
            raise Refused("internal", f"the phone side failed: {e}") from e
        except Exception as e:  # one failed step never takes the daemon down
            self.log(f"{name} failed unexpectedly:\n{traceback.format_exc()}")
            raise Refused("internal", f"{name} failed unexpectedly: {e}. This is a bug; report it.") from e

    async def _run_tool(self, client_id: str, name: str, args: dict) -> dict:
        if name not in ACTIONS and name not in READS:
            raise Refused("unsupported", f"no tool named {name!r}; the tools are {', '.join([*READS, *ACTIONS])}")
        missing = [k for k in REQUIRED.get(name, ()) if args.get(k) is None]
        if missing:
            raise Refused("bad_params", f"{name} needs {', '.join(missing)}")
        args = _checked(name, args)
        # A bad target was refused by _checked, before anything is queued or sent. Any tool whose
        # arguments carry `target` goes through here, so the live-read rule in act() covers it too.
        target = args.get("target")
        view = self.control.client(client_id)
        notes = [NOTE_PERSON] if view.seen < self.control.person_generation else []
        await self.wait_connected()
        if name == "list_apps":
            apps = await self._read(self.session.apps)
            text = "\n".join(f"{a['bundle_id']}  {a['name']} {a['version']} ({a['build']})" for a in apps)
            return _answer([text or "no apps"], notes)
        if name in ("look", "wait"):
            if name == "wait":
                await asyncio.sleep(min(max(args.get("seconds", 1.0), 0.2), 30.0))
            return await self._look(view, bool(args.get("screenshot", True)), [], notes)
        if self.state == "locked":
            raise Refused("locked", "the phone is locked; ask the user to unlock it")
        call, settle = ACTIONS[name]
        by_ref = target is not None and target.kind == "ref"
        # type_text at a point acts on the input under it, so it needs a current list too
        by_lookup = target is not None and (target.kind in ("id", "label", "text")
                                            or (name == "type_text" and target.kind == "point"))

        fresh, full, box = [True], [False], {}

        async def act():
            self._require_connected()
            s = self.session
            s.items = view.items
            fresh[0] = view.seen == self.control.generation
            # never looked, a person acted, or the line dropped: the agent's list may be anything
            full[0] = not fresh[0] or view.dropped or view.read_at == float("-inf")
            if by_ref and view.dropped:
                raise Refused("stale", "the connection to the phone dropped since your last look; look again")
            if by_ref and not fresh[0]:
                raise Refused("stale", "the screen changed since your last look (someone else used the "
                                       "phone); look again")
            if by_lookup and not self._current(view):
                # Resolve on the live tree. The agent's refs follow their elements onto it, so `e3` still
                # means the same element when it moved. The view's freshness doesn't change: a person's
                # action still makes refs stale, and `view.shown` (what the agent was told) stays put.
                mark = self._mark()  # taken before the read: a frame that changes meanwhile counts against the list
                await s.refresh()
                known = {e.ref for e in view.items}  # what came back may get its old ref: look in both lists
                held = sorted([*view.items, *(e for e in view.shown if e.ref not in known)], key=lambda e: (e.y, e.x))
                view.items = changes.carry_refs(held, s.items, view.top_ref, moves_content=True)  # the screen may be anything
                view.top_ref = max(view.top_ref, changes.highest(view.items))
                view.read_at, view.read_mark = self.control.clock(), mark  # the next target in the same window needn't read again
                s.items = view.items
            s.last_point = None
            box["mark"] = self._mark()  # where the stream stands as the gesture goes out
            return await call(s, args)

        try:
            result = await self.control.agent(act)
        except ConnectionLost as e:
            self.lost(str(e))
            self._announce("agent", name, ok=False, code="reconnecting")
            raise Refused("reconnecting", "the connection dropped during the action; look to check what "
                                          "happened") from None
        if fresh[0]:  # its own action keeps what it knew current; after someone else's, it must look
            view.seen = self.control.generation
        looks = settle is not None and args.get("look", True) and not (name == "alert" and args.get("action") == "text")
        view.moved = view.moved or not looks
        self._announce("agent", name, label=str(result), point=self._point(self.session.last_point))
        if not looks:
            return _answer([str(result)], notes)
        mark = box["mark"]
        seconds, changed = await self.settle(mark[0], cap=settle, since=mark[1])
        if not changed and not (full[0] or view.moved) and mark[1] == view.read_mark[1]:
            # no frame changed during the action, and the picture at its start is the one the agent's
            # list was read against (nothing changed silently before it): no tree read, the list stays
            view.read_at, view.read_mark = self.control.clock(), self._mark()
            return _answer([str(result), changes.SAME_SCREEN, changes.NO_CHANGE, settled_line(seconds)], notes)
        return await self._after(view, str(result), notes, full[0], name in MOVES_CONTENT, seconds)

    async def settle(self, after_seq: int, still: float | None = None, cap: float = CAP,
                     since: bytes | None = None) -> tuple[float, bool]:
        """Wait until the screen is still, as the stream shows it: `still` seconds without a frame whose
        bytes differ from the one before, but at most `cap`. Returns (seconds waited, changed).
        `changed` means a frame differs from the one at `after_seq` (`since`, its SHA-1 from _mark;
        None means there was no frame then, so any frame counts). With no frames at all (the stream
        is down or still opening) it sleeps `settle_fallback` and reports changed=True."""
        still = self.settle_still if still is None else still
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline, last_change = started + cap, started
        seq, previous, changed, seen = after_seq, since, False, False
        while True:
            now = loop.time()
            left = deadline - now
            if left <= 0:
                break
            if seen:
                wait = min(left, still - (now - last_change))
                if wait <= 0:
                    break
            else:
                wait = min(left, self.settle_first_frame)
            frame = await self.hub.next_after(seq, timeout=wait)
            if frame is None:
                if not seen:
                    # the wait for a first frame already counts towards the fallback: never both
                    await asyncio.sleep(max(0.0, self.settle_fallback - (loop.time() - started)))
                    return loop.time() - started, True
                break  # the stream went quiet: the picture is still
            seen, seq = True, frame.seq
            digest = hashlib.sha1(frame.jpeg).digest()
            if digest != previous:
                changed, previous, last_change = True, digest, loop.time()
        return loop.time() - started, changed

    def _mark(self) -> tuple[int, bytes | None]:
        """(seq, SHA-1 of the newest frame): where the stream stood at this moment."""
        frame = self.hub.latest
        return (frame.seq, hashlib.sha1(frame.jpeg).digest()) if frame else (0, None)

    async def _look(self, view, screenshot: bool, lines: list, notes: list) -> dict:
        async def read():
            view.read_mark = self._mark()
            text, image = await self.session.look(screenshot=screenshot)
            view.items = view.shown = self.session.items
            view.top_ref, view.app, view.seen = changes.highest(view.items), self.session.app, self.control.generation
            view.alert, view.keyboard = _alert_key(self.session.last_alert), self.session.last_keyboard
            view.read_at, view.moved, view.dropped = self.control.clock(), False, False
            return text, image
        try:
            text, image = await self._read(read)
        except Exception as e:
            if not lines:
                raise
            view.moved = True  # no list came back: the screen has probably changed
            return _answer(lines, [*notes, f"couldn't look afterwards: {e}."])
        return _answer([*lines, text], notes, image)

    async def _after(self, view, result: str, notes: list, full: bool, moves_content: bool = False,
                     seconds: float | None = None) -> dict:
        """An action's answer: its result line, then what changed on screen since the agent's last answer
        (`view.shown`), or the full list when the app changed or `full`, then `Settled in` (left out when
        `seconds` is None: `wait` did nothing to settle), then the notes. Refs follow their elements."""
        s = self.session
        tail = [] if seconds is None else [settled_line(seconds)]

        async def read():
            view.read_mark = self._mark()
            snap = await s.snapshot()
            app_changed = snap.app is not None and view.app is not None and snap.app != view.app
            head = _furniture(view, snap.tree.alert, snap.tree.keyboard)
            up = changes.update(view.items, snap.tree.items, app_changed, view.top_ref, view.shown, full, head,
                                moves_content)
            return snap, up
        try:
            snap, up = await self._read(read)
        except Exception as e:
            view.moved = True  # no list came back: the screen has probably changed
            return _answer([result, *tail], [*notes, f"couldn't look afterwards: {e}."])
        covered = not snap.tree.items and bool(view.shown)  # the agent saw elements; now there are none
        s.items = view.items = view.shown = up.items
        view.top_ref, view.app = up.top, snap.app or view.app
        view.alert, view.keyboard = _alert_key(snap.tree.alert), snap.tree.keyboard
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
        if frame is None or was is None or time.time() * 1000 - frame.at > FRESH_FRAME * 1000:
            return False
        return hashlib.sha1(frame.jpeg).digest() == was

    async def _read(self, fn):
        """A read through the queue; after a dropped line, once more when reconnected."""
        for attempt in (1, 2):
            try:
                return await self.control.read(lambda: self._connected_only(fn))
            except ConnectionLost as e:
                self.lost(str(e))
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
                return {"type": "error", "code": "locked" if self.state == "locked" else "reconnecting",
                        "message": f"not connected to the phone ({self.detail or self.state})"}
            run, action, point = self._person_action(kind, message)
            await self.control.person(lambda: self._connected_only(run))
            self._announce("person", action, point=self._point(point))
            return None
        except Refused as e:
            return {"type": "error", "code": e.code, "message": str(e)}
        except ConnectionLost as e:
            self.lost(str(e))
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
            return (lambda: s.swipe(x0, y0, x1, y1, 300)), "scroll", (x0, y0)
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
            await self._read(lambda: self.session.settings(PRESETS[preset]))
        self.preset = preset
        self.broadcast(self.status())

    # ---- the stream and shutting down ----
    async def _open_stream(self):
        try:
            await asyncio.wait_for(self._connected.wait(), 30)
        except asyncio.TimeoutError:
            raise ConnectionLost("not connected to the phone yet") from None
        return await self.session.device.open_stream()

    async def close(self) -> None:
        await self.pump.stop()
        self.hub.close()
        try:
            await asyncio.wait_for(self.session.close(), 10)
        except Exception as e:
            self.log(f"closing: {e}")

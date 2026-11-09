"""The phone side of `tapwright serve`: one Session (tunnel and WebDriverAgent), the control
rules, the stream, the agent tools, a person's input from the live view, and a supervisor
that keeps it all connected. server.py only turns HTTP into calls here."""
import asyncio
import base64
import time

from . import ui
from .control import Controller, Refused, classify
from .device import ConnectionLost, NotSetUp, PhoneNotFound
from .stream import PRESETS, FrameHub, StreamPump

PROTOCOL = 1
ACCEPTS = ["tap", "longPress", "drag", "scroll", "text", "enter", "backspace", "home", "volume", "quality"]
BUTTONS = ("home", "volumeUp", "volumeDown")
SETTLE = 0.6  # seconds a screen gets to settle after an action, before the look that follows


def _target(a: dict) -> dict:
    return {"ref": a.get("ref"), "id": a.get("id"), "label": a.get("label"), "x": a.get("x"), "y": a.get("y"),
            "index": int(a.get("index") or 0)}


def _field(a: dict) -> dict:
    """type_text's target: an element to tap first, or none for the field that has focus."""
    t = {"ref": a.get("ref"), "id": a.get("id"), "label": a.get("label"), "x": None, "y": None, "index": 0}
    return t if any((t["ref"], t["id"], t["label"])) else {}


# Agent actions: tool -> (what it does on the session, seconds to settle before the look; None: no look)
ACTIONS = {
    "launch_app": (lambda s, a: s.launch(a["bundle_id"]), 2.0),
    "terminate_app": (lambda s, a: s.terminate(a["bundle_id"]), None),
    "tap": (lambda s, a: s.tap(**_target(a)), SETTLE),
    "long_press": (lambda s, a: s.tap(hold_ms=int(float(a.get("seconds", 1.0)) * 1000), **_target(a)), SETTLE),
    "type_text": (lambda s, a: s.type_text(a["text"], replace=bool(a.get("replace")), submit=bool(a.get("submit")),
                                           **_field(a)), SETTLE),
    "scroll": (lambda s, a: s.scroll(a.get("direction", "down"), float(a.get("amount", 0.5))), SETTLE),
    "swipe": (lambda s, a: s.swipe(float(a["x0"]), float(a["y0"]), float(a["x1"]), float(a["y1"]),
                                   int(float(a.get("seconds", 0.35)) * 1000)), SETTLE),
    "press_button": (lambda s, a: s.press_button(a["name"]), SETTLE),
    "alert": (lambda s, a: s.alert(a["action"]), SETTLE),
    "open_url": (lambda s, a: s.open_url(a["url"]), 1.5),
}
READS = ("look", "list_apps", "wait")
REQUIRED = {"launch_app": ("bundle_id",), "terminate_app": ("bundle_id",), "type_text": ("text",),
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
                 stream_linger: float = 30.0, stream_retry: float = 1.0) -> None:
        self.session = session
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
        self._lost.set()

    def _require_connected(self) -> None:
        if not self._connected.is_set():
            raise Refused("reconnecting", f"not connected to the phone ({self.detail or self.state}); try again")

    async def _connected_only(self, fn):
        """Run `fn` only if the line is up when its turn comes: a reconnect holds the queue,
        and whatever waited for it must not reconnect on its own."""
        self._require_connected()
        return await fn()

    async def wait_connected(self) -> None:
        if self.state == "no-device":
            raise Refused("reconnecting", f"no phone to use: {self.detail}")
        try:
            await asyncio.wait_for(self._connected.wait(), self.ready_wait)
        except asyncio.TimeoutError:
            raise Refused("reconnecting", f"not connected to the phone ({self.detail or self.state}); "
                                          "try again") from None

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
            await s.settings({**PRESETS[self.preset], "mjpegFixOrientation": True})

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
        """One agent tool call: {"texts": [...], "image": base64 JPEG or None, "notes": [...]}.
        Raises Refused: busy, paused, stale, reconnecting, locked, unsupported."""
        self.last_request = time.monotonic()
        if name not in ACTIONS and name not in READS:
            raise Refused("unsupported", f"no tool named {name!r}")
        missing = [k for k in REQUIRED.get(name, ()) if args.get(k) is None]
        if missing:
            raise Refused("unsupported", f"{name} needs {', '.join(missing)}")
        view = self.control.client(client_id)
        notes = ["a person used the phone since your last look"] if view.seen < self.control.person_generation else []
        await self.wait_connected()
        if name == "list_apps":
            apps = await self._read(self.session.apps)
            text = "\n".join(f"{a['bundle_id']}  {a['name']} {a['version']} ({a['build']})" for a in apps)
            return {"texts": [text or "no apps"], "image": None, "notes": notes}
        if name in ("look", "wait"):
            if name == "wait":
                try:
                    seconds = float(args.get("seconds", 1.0))
                except (TypeError, ValueError):
                    raise Refused("unsupported", "seconds must be a number") from None
                await asyncio.sleep(min(max(seconds, 0.2), 30.0))
            return await self._look(view, bool(args.get("screenshot", True)), [], notes)
        if self.state == "locked":
            raise Refused("locked", "the phone is locked; ask the user to unlock it")
        call, settle = ACTIONS[name]
        by_ref, by_lookup = bool(args.get("ref")), bool(args.get("id") or args.get("label"))

        fresh = [True]

        async def act():
            self._require_connected()
            s = self.session
            s.items = view.items
            fresh[0] = view.seen == self.control.generation
            if not fresh[0]:
                if by_ref:
                    raise Refused("stale", "the screen changed since your last look (someone else used the "
                                           "phone); look again")
                if by_lookup:
                    await s.look(screenshot=False)
                    fresh[0] = True
            s.last_point = None
            result = await call(s, args)
            view.items = s.items
            return result

        try:
            result = await self.control.agent(act)
        except ConnectionLost as e:
            self.lost(str(e))
            self._announce("agent", name, ok=False, code="reconnecting")
            raise Refused("reconnecting", "the connection dropped during the action; look to check what "
                                          "happened") from None
        except Refused:
            raise
        except ui.NotFound as e:
            return {"texts": [f"not found: {e}"], "image": None, "notes": notes}
        except Exception as e:  # one failed step never takes the daemon down
            return {"texts": [f"error: {e}"], "image": None, "notes": notes}
        if fresh[0]:  # its own action keeps what it knew current; after someone else's, it must look
            view.seen = self.control.generation
        self._announce("agent", name, label=str(result), point=self._point(self.session.last_point))
        look = settle is not None and args.get("look", True) and not (name == "alert" and args.get("action") == "text")
        if not look:
            return {"texts": [str(result)], "image": None, "notes": notes}
        await asyncio.sleep(settle)
        return await self._look(view, True, [str(result)], notes)

    async def _look(self, view, screenshot: bool, texts: list, notes: list) -> dict:
        async def read():
            text, image = await self.session.look(screenshot=screenshot)
            view.items, view.seen = self.session.items, self.control.generation
            return text, image
        try:
            text, image = await self._read(read)
        except Exception as e:
            if not texts:
                if isinstance(e, Refused):
                    raise
                return {"texts": [f"error: {e}"], "image": None, "notes": notes}
            return {"texts": texts + [f"(couldn't look afterwards: {e})"], "image": None, "notes": notes}
        return {"texts": texts + [text], "image": base64.b64encode(image).decode() if image else None,
                "notes": notes}

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
            return (lambda: s.tap(x=x, y=y, hold_ms=g.hold_ms)), g.kind, (x, y)
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

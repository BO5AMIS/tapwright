"""What an agent does with the phone: look, tap, type, swipe, launch apps. One Session
holds the connection and WebDriverAgent's session, connecting on first use."""
import asyncio
import base64
import io
from dataclasses import dataclass

from . import store, ui
from .control import TAP_MS, Refused
from .device import AppNotAnswering, ConnectionLost, Device, DeviceError, NotSetUp, PasscodeNeeded, PhoneLocked

FOCUS_POLL, FOCUS_WAIT = 0.1, 1.5  # seconds: how often, and for how long, typing waits for focus to arrive
LOCK_WAIT = 5.0  # seconds: /wda/locked answers in about 10 ms, since it doesn't read the app (measured)
FIRST_READ_WAIT = 10.0  # seconds: the window size, the first read of the app; past this the app is stuck
READ_AGAIN = 5.0  # seconds: the second try of a read that got no answer (see Session._source)
READ_CUT = 10.0  # seconds: the longest a tree read is waited for before asking why (50 times its usual time)
BACKSPACE_MAX = 64  # retyping into the field just typed into deletes up to this many characters with backspaces


def _field_key(field: ui.Element) -> str | None:
    """What says two reads show the same input: its identifier, else its label (None when it has neither)."""
    name = field.identifier or field.label
    return f"{field.role}:{name}" if name else None
KEYS = {"backspace": "\b", "enter": "\n"}  # what /wda/keys types for these keys


@dataclass
class Snapshot:
    """One read of the screen: the tree, the foreground app, and the pixel scale."""
    tree: ui.Tree
    app: tuple[str, str] | None
    scale: float | None


class Session:
    def __init__(self, device: Device):
        self.device = device
        self.sid = None
        self.screen = (390.0, 844.0)
        self.scale: float | None = None  # pixels per point, read once per connection; None while unknown
        self.scale_read = False  # whether that read has happened, so an unknown scale isn't asked for again
        self.items: list[ui.Element] = []
        self.app: tuple[str, str] | None = None  # (name, bundle id) at the last read
        self.last_alert: ui.Alert | None = None  # the OS alert and keyboard state at the last snapshot
        self.last_keyboard = False
        self.last_insets: tuple[float | None, float | None] = (None, None)  # where the top and bottom bars end (y)
        self.last_point: tuple[float, float] | None = None  # where the last tap, swipe or drag began, in points
        self.app_names: dict[str, str] = {}  # bundle id -> display name, from the installed apps
        self.not_installed: set[str] = set()  # bundle ids already looked for in the installed apps and not found
        # (field key, element id) of the input our last type_text verified and typed into, while nothing else has
        # touched the phone since: typing there again needs no tap. Every other action clears it.
        self.typed_into: tuple[str, str] | None = None
        self.offscreen: list[ui.Element] = []  # what the last read found outside the screen (for `off_screen`)
        # what a sheet hides, each with the element in front that names the sheet (for `covered`)
        self.behind: list[tuple[ui.Element, ui.Element | None]] = []
        self.front_items: list[ui.Element] = []  # the list as back_point last read it

    async def ensure(self) -> None:
        """Connect, mount and start WebDriverAgent when needed. Once there is a session this is
        free: a dead line shows up as ConnectionLost on the next request instead."""
        if self.device.rsd is None:
            await self.device.connect()
            await self.device.mount()
        if self.sid is not None:
            return
        runner = store.load_state().get("runner_id")
        if not runner:
            raise NotSetUp("WebDriverAgent isn't set up on this phone yet: run `tapwright setup`")
        await self.device.start_wda(runner)
        sid = (await self.device.http("POST", "/session", {"capabilities": {"alwaysMatch": {}}}))["sessionId"]
        # Asked before anything reads the app: once a read hangs behind a frozen app, nothing else gets an answer.
        locked = (await self.device.http("GET", f"/session/{sid}/wda/locked", timeout=LOCK_WAIT)).get("value")
        try:
            size = (await self.device.http("GET", f"/session/{sid}/window/size", timeout=FIRST_READ_WAIT))["value"]
        except ConnectionLost as e:
            raise await self._why_stuck(bool(locked), e) from e
        self.screen = (float(size["width"]), float(size["height"]))
        self.sid = sid  # committed last: a failure above leaves no half-started session

    async def _why_stuck(self, locked: bool, lost: ConnectionLost) -> ConnectionLost:
        """The first read of a fresh WebDriverAgent hung: say why when the phone can tell. A locked phone, or
        the app iOS has in front (from its process list), else the dead line it looked like."""
        if locked:
            return PhoneLocked("the phone is locked")
        try:
            if await self.device.passcode_prompt_up():
                return PasscodeNeeded()  # iOS asks again now and then, also with WebDriverAgent already up
        except Exception:
            pass
        try:
            in_front = await self.device.foreground_apps()
        except Exception:
            return lost
        if not in_front:
            return lost
        bundle = in_front[0]
        if bundle not in self.app_names:
            try:
                await self.read_app_names()
            except Exception:
                pass  # the bundle id names it well enough
        return AppNotAnswering(bundle, self.app_names.get(bundle) or bundle)

    async def reset(self) -> None:
        """Drop the connection; the next request reconnects and starts WebDriverAgent again."""
        try:
            await asyncio.wait_for(self.device.close(), 10)
        except Exception:
            pass
        self.device.rsd = self.device.tunnel = self.device.runner = None
        self.sid = None
        self.scale = self.app = None
        self.scale_read = False
        self.items, self.offscreen, self.behind = [], [], []
        self.typed_into = None
        self.last_point = None

    async def _call(self, method: str, path: str, payload: dict | None = None, timeout: float = 30) -> dict:
        await self.ensure()
        try:
            return await self.device.http(method, path.replace("{sid}", self.sid), payload, timeout)
        except ConnectionLost:
            self.sid = None  # the next call starts WebDriverAgent again
            raise
        except DeviceError as e:
            if "invalid session" in str(e).lower():  # WebDriverAgent restarted: the session is gone
                self.sid = None
                raise ConnectionLost(str(e)) from e
            raise

    # ---- seeing ----
    async def _source(self, timeout: float = 60) -> str:
        """The tree as WebDriverAgent's XML. A read normally takes about 0.2 s; one that hasn't answered within
        READ_CUT seconds is cut, and the phone is asked why: an app that is in front and doesn't answer
        accessibility queries is named (AppNotAnswering), so the caller can say so instead of waiting on it."""
        sid = self.sid
        try:
            return (await self._call("GET", ui.SOURCE_PATH, timeout=min(timeout, READ_CUT))).get("value")
        except (PhoneLocked, AppNotAnswering):
            raise
        except ConnectionLost as e:
            # One read that got no answer is not yet a frozen app: over Wi-Fi a single connection can stall while
            # the phone and the app are fine (seen 2026-11-10: four such stalls in eight minutes on a network
            # losing 4% of its packets, each one answered as "JLD isn't answering"). Every request has its own
            # connection, so the read is asked once more; a frozen app doesn't answer that one either.
            if sid is not None and "didn't answer" in str(e) and self.device.rsd is not None:
                try:
                    value = (await self.device.http("GET", ui.SOURCE_PATH.replace("{sid}", sid), None, READ_AGAIN)).get("value")
                except (ConnectionLost, DeviceError):
                    pass
                else:
                    self.sid = sid  # the session is alive: nothing needs starting again
                    return value
            raise await self._why_stuck(False, e) from e

    async def foreground_bundle_id(self) -> str | None:
        try:
            info = (await self._call("GET", "/wda/activeAppInfo")).get("value") or {}
        except ConnectionLost:
            raise
        except DeviceError:
            return None
        return str(info.get("bundleId") or "") or None

    async def read_app_names(self, refresh: bool = False) -> None:
        """Bundle id -> display name for the apps you installed: what the header's `App:` line shows,
        since WebDriverAgent's own name is the bundle id for most apps. Read once per connection
        (`refresh`), and again when an unknown bundle id shows up."""
        if refresh:
            self.not_installed = set()
        try:
            self.app_names = {a["bundle_id"]: a["name"] for a in await self.device.user_apps() if a["name"]}
        except ConnectionLost:
            raise
        except Exception:  # a nicety: without names the header shows what WebDriverAgent says
            pass

    async def _app(self, known: tuple[str, str] | None = None) -> tuple[str, str] | None:
        """(name, bundle id) of the foreground app, or None when WebDriverAgent can't say. `known` is the tree
        root's (label, bundle id): the same answer without a request (activeAppInfo cost 70 ms an answer)."""
        if known is not None:
            info = {"name": known[0], "bundleId": known[1]}
        else:
            try:
                info = (await self._call("GET", "/wda/activeAppInfo")).get("value") or {}
            except ConnectionLost:
                raise
            except DeviceError:
                return None
        bundle = str(info.get("bundleId") or "")
        if not bundle:
            return None
        if bundle == "com.apple.springboard":
            return "Home screen", bundle
        if bundle not in self.app_names and bundle not in self.not_installed:
            await self.read_app_names()  # perhaps installed since; if not, don't ask again for this one
            if bundle not in self.app_names:
                self.not_installed.add(bundle)
        return self.app_names.get(bundle) or str(info.get("name") or bundle), bundle

    async def read_scale(self, refresh: bool = False) -> float | None:
        """Pixels per point, read once: from WebDriverAgent's screen info, else (it only answers in some
        apps) from a screenshot's width against the screen's width in points (1290 / 430 = 3)."""
        if refresh or not self.scale_read:
            self.scale_read = True  # the outcome stays, unknown included: snapshots never ask again
            self.scale = None
            try:
                value = (await self._call("GET", "/session/{sid}/wda/screen")).get("value") or {}
                self.scale = float(value["scale"])
            except ConnectionLost:
                raise
            except (DeviceError, KeyError, TypeError, ValueError):
                self.scale = await self._scale_from_screenshot()
        return self.scale

    async def _scale_from_screenshot(self) -> float | None:
        try:
            png = base64.b64decode((await self._call("GET", "/screenshot", timeout=60))["value"])
            from PIL import Image
            with Image.open(io.BytesIO(png)) as img:
                scale = img.width / self.screen[0]
        except ConnectionLost:
            raise
        except Exception:
            return None  # the header just leaves out "@3x"
        return float(round(scale)) if scale >= 1 else None

    async def snapshot(self, keyboard: bool = False, timeout: float = 60) -> Snapshot:
        """Read the tree, the foreground app and the scale; `items` is the new list, numbered from e1."""
        source = await self._source(timeout)
        return await self.snapshot_from(ui.read(source, self.screen, keyboard=keyboard))

    async def snapshot_from(self, tree: ui.Tree) -> Snapshot:
        """A snapshot of a tree that was already read (`refresh`): this adds the foreground app, the one
        request a tree-only read leaves out."""
        self.items, self.offscreen, self.behind = tree.items, tree.offscreen, tree.behind
        self.last_insets = (tree.top_inset, tree.bottom_inset)
        self.app = await self._app(tree.app)
        self.last_alert, self.last_keyboard = tree.alert, tree.keyboard
        return Snapshot(tree, self.app, self.scale)

    async def screenshot_png(self) -> bytes:
        """WebDriverAgent's /screenshot (PNG, full resolution): the fallback when the stream has no frame."""
        return base64.b64decode((await self._call("GET", "/screenshot", timeout=60))["value"])

    async def look(self, screenshot: bool = True, keyboard: bool = False, max_width: int = 400):
        """(text, jpeg bytes or None): the screen as elements, and as a picture."""
        snap = await self.snapshot(keyboard)
        tree, app, scale = snap.tree, snap.app, snap.scale
        image = None
        if screenshot:
            png = base64.b64decode((await self._call("GET", "/screenshot", timeout=60))["value"])
            from PIL import Image
            img = Image.open(io.BytesIO(png)).convert("RGB")
            if img.width > max_width:
                img = img.resize((max_width, round(img.height * max_width / img.width)), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=80)
            image = buf.getvalue()
        return ui.describe(tree.items, self.screen, scale, app, tree.alert, tree.keyboard, tree.more,
                           (tree.above, tree.below)), image

    async def back_point(self) -> ui.Back:
        """The top navigation bar: whether there is one, and where its back button is, in points."""
        source = await self._source()
        # what is open on top, as this read shows it: `back` names it when it could close nothing
        tree = ui.read(source, self.screen)
        self.behind, self.front_items = tree.behind, tree.items  # (and what the layer in front shows, for `back`)
        return ui.back_button(source, self.screen)

    async def refresh(self, timeout: float = 60) -> ui.Tree:
        """Read the tree into `items` and nothing else (one request): what resolving a target needs."""
        source = await self._source(timeout)
        tree = ui.read(source, self.screen)
        self.items, self.offscreen, self.behind = tree.items, tree.offscreen, tree.behind
        self.last_insets = (tree.top_inset, tree.bottom_inset)
        self.last_alert, self.last_keyboard = tree.alert, tree.keyboard
        return tree

    def _target(self, target: ui.Target) -> tuple[float, float, str]:
        """(x, y, what to say) for a target, on the elements of the last look."""
        hit = ui.resolve(self.items, target)
        if isinstance(hit, tuple):
            return hit[0], hit[1], f"({round(hit[0])}, {round(hit[1])})"
        cx, cy = hit.center
        return cx, cy, hit.line(actions=False)

    # ---- acting ----
    async def tap(self, target: ui.Target, hold_ms: int = TAP_MS) -> str:
        self.typed_into = None
        x, y, what = self._target(target)
        await self._call("POST", "/session/{sid}/actions", ui.tap(x, y, hold_ms))
        self.last_point = (x, y)
        return f"{'Long-pressed' if hold_ms > 300 else 'Tapped'} {what}."

    async def type_text(self, text: str, replace: bool = False, submit: bool = False,
                        target: ui.Target | None = None, at: tuple[float, float] | None = None) -> str:
        """Type into the target (tapped first to focus it, at `at` when given: the part of it that shows),
        or into whatever has focus. A point names the input under it, not a touch: with none there,
        nothing is typed."""
        if target is not None and target.point is not None:
            field = ui.at_point(self.items, target.point, "input")
            if field is None:
                x, y = target.point
                raise Refused("not_actionable", f"no_action: Nothing at ({round(x)}, {round(y)}) takes typing.")
            target = ui.Target(ref=field.ref)
        element, hit, again = None, None, False
        if target is not None:
            what = self._target(target)[2]
            hit = ui.resolve(self.items, target)
            key = _field_key(hit) if not isinstance(hit, tuple) else None
            focused, self.typed_into = self.typed_into, None
            if key and focused and focused[0] == key and await self._active_element() == focused[1]:
                element, again = focused[1], True  # still focused by our own typing, nothing done since: no tap
            else:
                await self.tap(target if at is None else ui.Target(x=at[0], y=at[1]))
                # focus is what counts: with a hardware keyboard no software one shows
                element = await self._has_focus(hit) if not isinstance(hit, tuple) else await self._active_element()
                if element is None and not isinstance(hit, tuple):
                    raise Refused("not_actionable", f"no_action: {what} didn't take focus.")
        note, keys = "", text + ("\n" if submit else "")
        field = hit if isinstance(hit, ui.Element) else None
        if replace and field is not None and not field.secure and field.value in ("", field.placeholder):
            pass  # empty: nothing to clear (WebDriverAgent reports an empty field's placeholder as its value)
        elif replace and again and field is not None and not field.secure and len(field.value) <= BACKSPACE_MAX:
            keys = "\b" * len(field.value) + keys  # the cursor is at the end of the text we typed: delete it
        elif replace:
            element = element or await self._active_element()
            if element:
                await self._call("POST", f"/session/{{sid}}/element/{element}/clear", {})
            else:
                note = " (couldn't find the focused field to clear, so the old text may remain)"
        await self._call("POST", "/session/{sid}/wda/keys", {"value": list(keys)})
        if field is not None and element and not submit:  # return may move focus on; keys never do
            self.typed_into = (_field_key(field), element)
        return f"typed {len(text)} characters" + (" and pressed return" if submit else "") + note

    async def is_focused(self, field: ui.Element) -> bool:
        """Is this listed input the one WebDriverAgent says has focus (the keyboard's target)?"""
        if field.role != "input":
            return False
        active = await self._active_element()
        return bool(active) and await self._is_field(active, field)

    async def dismiss_keyboard(self) -> bool:
        """WebDriverAgent's own way of closing the keyboard, which presses no return. False when it refuses (the
        keyboard has no key to dismiss it with); a dead line still raises ConnectionLost."""
        self.typed_into = None
        try:
            await self._call("POST", "/session/{sid}/wda/keyboard/dismiss", {})
        except ConnectionLost:
            raise
        except DeviceError:
            return False
        return True

    async def _active_element(self) -> str | None:
        focused = (await self._call("GET", "/session/{sid}/element/active")).get("value") or {}
        return focused.get("ELEMENT") or next(iter(focused.values()), None)

    async def _attribute(self, element: str, name: str) -> str | None:
        try:
            value = (await self._call("GET", f"/session/{{sid}}/element/{element}/attribute/{name}")).get("value")
        except ConnectionLost:
            raise
        except DeviceError:
            return None
        return None if value is None else str(value)

    async def _has_focus(self, field: ui.Element) -> str | None:
        """Wait until the active element is the target; that element's id, or None when it never is. The keyboard
        can scroll the field after the tap, so the pre-tap rect says nothing: the element's identifier (else its
        label) is compared first, then its rect against where a fresh read of the tree shows the field now.
        Without this, keys would land in the old field."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + FOCUS_WAIT
        while True:
            element = await self._active_element()
            if element and await self._is_field(element, field):
                return element
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(FOCUS_POLL)

    async def _is_field(self, element: str, field: ui.Element) -> bool:
        wanted = field.identifier or field.label
        if wanted:
            got = await self._attribute(element, "name") if field.identifier else None
            if got is None and not field.identifier:
                got = await self._attribute(element, "label")
            if got == wanted:
                return True
        now = await self._field_now(field)
        if now is None:
            return False
        box = (await self._call("GET", f"/session/{{sid}}/element/{element}/rect")).get("value") or {}
        got_rect = tuple(box.get(k, float("inf")) for k in ("x", "y", "width", "height"))
        return all(abs(a - b) <= 2 for a, b in zip(got_rect, now))

    async def _field_now(self, field: ui.Element) -> tuple[float, float, float, float] | None:
        """Where the tree shows the field now: the one input with its identifier (else its label), or None."""
        source = await self._source()
        if not isinstance(source, str):
            return None
        same = [e for e in ui.read(source, self.screen).items if e.role == field.role
                and (e.identifier == field.identifier if field.identifier else e.label == field.label)]
        return same[0].rect if len(same) == 1 else None

    async def swipe(self, x0: float, y0: float, x1: float, y1: float, duration_ms: int = 350, hold_ms: int = 0) -> str:
        self.typed_into = None
        await self._call("POST", "/session/{sid}/actions", ui.swipe(x0, y0, x1, y1, duration_ms, hold_ms))
        self.last_point = (x0, y0)
        return f"swiped ({round(x0)}, {round(y0)}) -> ({round(x1)}, {round(y1)})"

    async def scroll(self, x0: float, y0: float, x1: float, y1: float) -> str:
        """A scroll drag that stops where the finger does (ui.scroll_drag)."""
        self.typed_into = None
        await self._call("POST", "/session/{sid}/actions", ui.scroll_drag(x0, y0, x1, y1))
        self.last_point = (x0, y0)
        return f"scrolled ({round(x0)}, {round(y0)}) -> ({round(x1)}, {round(y1)})"

    async def path(self, points: list) -> str:
        """Replay a finger path: [(t_ms from 0, x, y), ...] in points."""
        self.typed_into = None
        await self._call("POST", "/session/{sid}/actions", ui.path(points))
        self.last_point = (points[0][1], points[0][2])
        return f"dragged through {len(points)} points"

    async def press_button(self, name: str) -> str:
        self.typed_into = None
        if name == "home":  # pressButton "home" does nothing on iOS 26 (measured); this one waits for the home screen
            await self._call("POST", "/wda/homescreen", {})
        else:
            await self._call("POST", "/session/{sid}/wda/pressButton", {"name": name})
        return f"pressed {name}"

    async def key(self, name: str) -> str:
        """backspace or enter, into whatever field has focus."""
        if name not in KEYS:
            raise Refused("bad_params", f"key must be one of {', '.join(KEYS)}, not {name!r}")
        self.typed_into = None  # a backspace moves the cursor's text; enter may move focus
        await self._call("POST", "/session/{sid}/wda/keys", {"value": [KEYS[name]]})
        return f"pressed {name}"

    async def settings(self, values: dict) -> None:
        """WebDriverAgent settings, e.g. the MJPEG stream's frame rate and size."""
        await self._call("POST", "/session/{sid}/appium/settings", {"settings": values})

    async def locked(self) -> bool:
        return bool((await self._call("GET", "/session/{sid}/wda/locked")).get("value"))

    async def alert(self, action: str) -> str:
        """accept, dismiss, or text: the system alert on screen (permission prompts and the like)."""
        if action != "text":
            self.typed_into = None
        try:
            if action == "text":
                return (await self._call("GET", "/session/{sid}/alert/text")).get("value") or "(no alert)"
            await self._call("POST", f"/session/{{sid}}/alert/{action}", {})
        except DeviceError as e:
            if "no such alert" in str(e).lower() and not isinstance(e, ConnectionLost):
                raise Refused("not_found", "no alert on screen.") from e
            raise
        return f"alert {action}ed"

    # ---- apps ----
    async def apps(self) -> list[dict]:
        await self.ensure()
        apps = await self.device.user_apps()
        self.app_names = {a["bundle_id"]: a["name"] for a in apps if a["name"]}
        return apps

    async def launch(self, bundle_id: str) -> str:
        self.typed_into = None
        await self._call("POST", "/session/{sid}/wda/apps/launch", {"bundleId": bundle_id}, timeout=60)
        return f"launched {bundle_id}"

    async def terminate(self, bundle_id: str) -> str:
        self.typed_into = None
        await self._call("POST", "/session/{sid}/wda/apps/terminate", {"bundleId": bundle_id})
        return f"closed {bundle_id}"

    async def open_url(self, url: str) -> str:
        self.typed_into = None
        await self._call("POST", "/session/{sid}/url", {"url": url})
        return f"opened {url}"

    async def close(self) -> None:
        await self.device.close()

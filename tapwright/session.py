"""What an agent does with the phone: look, tap, type, swipe, launch apps. One Session
holds the connection and WebDriverAgent's session, connecting on first use."""
import asyncio
import base64
import io
import os
import re
import time
from dataclasses import dataclass

from . import store, ui
from .control import TAP_MS, Refused
from .device import AppNotAnswering, ConnectionLost, Device, DeviceError, NotSetUp, PasscodeNeeded, PhoneLocked, Unanswered

FOCUS_POLL, FOCUS_WAIT = 0.1, 1.5  # seconds: how often, and for how long, typing waits for focus to arrive
AUDIT_TIMEOUT = 60.0  # seconds: XCTest's audit walks the whole screen, element by element
LOCK_WAIT = 5.0  # seconds: /wda/locked answers in about 10 ms, since it doesn't read the app (measured)
FIRST_READ_WAIT = 10.0  # seconds: the window size, the first read of the app; past this the app is stuck
BANNER_ZONE = 200.0  # points from the top: where a notification banner can lie (a touch lower down is never held)
SPRINGBOARD = "com.apple.springboard"
KEY_MS = 60  # ms a typed key counts for in the notice to Tapline (see tapline.py)
SCROLL_MS = 1200  # ms a scroll drag counts for there (an eased drag that ends slowly)
ACTION_WAIT = 10.0  # seconds a touch may take to be answered beyond its own length (a tap answers in about 0.5 s)
BANNER_ID = "NotificationShortLookView"  # the identifier of a notification banner in SpringBoard's tree
BANNER_WAIT, BANNER_POLL = 8.0, 0.5  # seconds: how long a read waits for a banner to leave, and how often it looks
# TAPWRIGHT_TRACE=1: every request to WebDriverAgent is logged with how long it took (for profiling, e.g. over Wi-Fi)
TRACE = os.environ.get("TAPWRIGHT_TRACE") == "1"
READ_AGAIN = 5.0  # seconds: the second try of a question that got no answer (see Session._call)
READ_CUT = 10.0  # seconds: the longest a tree read is waited for before asking why (50 times its usual time)
ORIENTATIONS = {"portrait": "PORTRAIT", "landscape_left": "LANDSCAPE",
                "landscape_right": "UIA_DEVICE_ORIENTATION_LANDSCAPERIGHT"}  # WebDriverAgent's names
ROOT_SIZE = re.compile(r'<XCUIElementTypeApplication[^>]*?\bwidth="([\d.]+)" height="([\d.]+)"')
# How far to turn a captured frame clockwise for each WebDriverAgent rotation z (checked on the phone 2026-11-11 with
# Calculator in each landscape side: the frame turned this way reads upright)
TURN_FOR_Z = {90: 90, 270: 270}
TURN_RECHECK = 1.0  # seconds: in landscape, how often which side is asked again (the tree can't tell the sides apart)
# Tapwright's own WebDriverAgent build (TAPWRIGHT_WDA=fork) has a fast tap route: one touch, with no element lookup and
# no idle wait (WebDriverAgentLib/Commands/TWFastTapCommands.m in the fork). Asked for once per session; Appium's
# build answers the probe with an error, and taps keep going through W3C actions there. TAPWRIGHT_FAST_TAP=0 turns the
# route off. The route's answer waits until the touch has been played (`wait`): answering as soon as it was queued was
# no faster per tap on the phone (0.466 s against 0.469 s, 2026-11-12), and what came next raced the touch (typing
# looked for the focused field before the tap had focused it, and its keys hit "only one gesture at a time").
FAST_TAP = os.environ.get("TAPWRIGHT_FAST_TAP", "1")
FAST_VERSION_PATH = "/session/{sid}/wda/tw/version"
FAST_TAP_PATH = "/session/{sid}/wda/tw/tap"
# The fork's route version 2 adds a fast drag: one touch path [[t_ms, x, y], ...] played the same way as the fast tap
# (pressed at the first point, moved through each later one at its time, lifted at the last), answering once played.
# Scrolls, swipes and drags send it the very path their W3C actions describe, so a scroll still eases and rests at
# its end and never flings. TAPWRIGHT_FAST_DRAG=0 (or TAPWRIGHT_FAST_TAP=0) keeps drags on W3C actions.
FAST_DRAG = os.environ.get("TAPWRIGHT_FAST_DRAG", "1")
FAST_DRAG_PATH = "/session/{sid}/wda/tw/drag"
END_INSET = 40.0  # points from a field's right edge where typing's tap puts the caret at the end of short text
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
        self.retyped = False  # the last type_text cleared the field and typed once more (see type_text)
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
        # Degrees the stream's frames are turned clockwise to stand upright: 0 in portrait. In landscape the tree
        # and window are 932x430 while WebDriverAgent's frames stay as captured (measured 2026-11-11).
        self.turn = 0
        self._turn_asked = -float("inf")
        # Awaited with a touch's length in ms just before each touch while it is set: the daemon sets it for the
        # agent's actions (Tapline is told the touch is the agent's), never for a person's input from the live view
        self.touch_hook = None
        self.banner_guard = False  # set by the daemon for the agent's actions: see _clear_of_banner
        # whether one of the agent's touches since the daemon last cleared it began where a banner comes down: a
        # banner landing between the check and the touch takes it (Daemon._taken_away)
        self.top_touch = False
        self.fast_tap: bool | None = None  # whether this session's runner has the fast tap route (None: not asked yet)
        self.fast_drag: bool | None = None  # and the fast drag route (asked with the tap's)

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
        self.fast_tap = self.fast_drag = None  # a new session may be a new runner: ask again
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

    async def _call(self, method: str, path: str, payload: dict | None = None, timeout: float = 30,
                    again: bool | None = None) -> dict:
        await self.ensure()
        full = path.replace("{sid}", self.sid)
        try:
            try:
                if not TRACE:
                    return await self.device.http(method, full, payload, timeout)
                began = time.monotonic()
                try:
                    return await self.device.http(method, full, payload, timeout)
                finally:
                    getattr(self.device, "log", lambda *a: None)(
                        f"trace {method} {path.split('?')[0]} {(time.monotonic() - began) * 1000:.0f} ms")
            except ConnectionLost as e:
                # A question that got no answer is asked once more before the line counts as dead. Every request
                # has its own connection, and over Wi-Fi one of them can stall while the phone, WebDriverAgent
                # and the app are fine (seen 2026-11-10: `wda/locked`, which answers in 10 ms, got none in 30 s;
                # tree reads stalled the same way and were answered as a frozen app, with a restart of
                # WebDriverAgent each time). Only questions, and what may be said twice (`again`: settings): an
                # action asked twice could happen twice.
                if "didn't answer" not in str(e) or self.device.rsd is None:
                    raise
                if not (method == "GET" if again is None else again):
                    # An action: it can't be sent again. If WebDriverAgent answers a new connection, only this
                    # one request stalled: the session stays, and the caller hears that it isn't known whether
                    # the action happened (before, 30 s of silence and then WebDriverAgent started again).
                    if await self.device.wda_alive():
                        raise Unanswered(f"the phone didn't answer within {timeout:.0f} s") from None
                    raise
                getattr(self.device, "log", lambda *a: None)(f"no answer to {method} {path.split('?')[0]} in {timeout:.0f} s: asked again")
                return await self.device.http(method, full, payload, READ_AGAIN)
        except ConnectionLost:
            self.sid = None  # the next call starts WebDriverAgent again
            raise
        except DeviceError as e:
            if "invalid session" in str(e).lower():  # WebDriverAgent restarted: the session is gone
                self.sid = None
                raise ConnectionLost(str(e)) from e
            raise

    async def _follow_orientation(self, source: str | None) -> None:
        """Keep `screen` and `turn` with the phone's orientation: the tree's root says the screen's size in points
        now (932x430 in landscape), and WebDriverAgent's rotation says which way it turned (z 90 or 270)."""
        if not isinstance(source, str):
            return
        root = ROOT_SIZE.search(source[:2000])
        if root is None:
            return
        w, h = float(root.group(1)), float(root.group(2))
        if w <= 0 or h <= 0:
            return
        same = (w > h) == (self.screen[0] > self.screen[1])
        if same and (w <= h or time.monotonic() - self._turn_asked < TURN_RECHECK):
            return  # (in landscape the phone may have turned to the other side: asked again now and then)
        self.screen = (w, h)
        if w <= h:
            self.turn = 0
            return
        self._turn_asked = time.monotonic()
        try:
            z = int((await self._call("GET", "/session/{sid}/rotation", timeout=LOCK_WAIT)).get("value", {}).get("z", 0))
        except (DeviceError, TypeError, ValueError):
            z = 90
        self.turn = TURN_FOR_Z.get(z % 360, 90)

    async def _banner_over_an_app(self, source: str | None) -> bool:
        """Is this tree only a notification banner sliding over the app in front? While one shows, WebDriverAgent
        answers with SpringBoard's tree: the banner and nothing of the app (seen on the phone 2026-11-10: the
        answers said "Home screen", the app's elements were `not_found`, and the banner's words, which are the
        owner's, were what the agent got). The app is still in front (the phone's process list says so), and the
        banner leaves by itself in a few seconds: the read waits for that. Not for an alert, which is the
        agent's to answer, nor on the real home screen, where no app is in front."""
        if not source or BANNER_ID not in source or ui.SPRINGBOARD_ROOT not in source[:600] or "XCUIElementTypeAlert" in source:
            return False
        try:
            return bool(await asyncio.wait_for(self.device.foreground_apps(), 5))
        except Exception:
            return False

    # ---- seeing ----
    async def _source(self, timeout: float = 60) -> str:
        """The tree as WebDriverAgent's XML. A read normally takes about 0.2 s; one that hasn't answered within
        READ_CUT seconds is cut, and the phone is asked why: an app that is in front and doesn't answer
        accessibility queries is named (AppNotAnswering), so the caller can say so instead of waiting on it."""
        try:
            # (asked a second time by _call when the first got no answer: a frozen app answers neither)
            source = (await self._call("GET", ui.SOURCE_PATH, timeout=min(timeout, READ_CUT))).get("value")
            await self._follow_orientation(source)
            waited = 0.0
            while await self._banner_over_an_app(source) and waited < min(BANNER_WAIT, timeout):
                await asyncio.sleep(BANNER_POLL)
                waited += BANNER_POLL
                source = (await self._call("GET", ui.SOURCE_PATH, timeout=min(timeout, READ_CUT))).get("value")
            if waited:
                still = " and it is still there" if await self._banner_over_an_app(source) else ""
                getattr(self.device, "log", lambda *a: None)(
                    f"a notification banner was over the app: the read waited {waited:.1f} s{still}")
            return source
        except (PhoneLocked, AppNotAnswering):
            raise
        except ConnectionLost as e:
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

    async def audit(self, types: list[str]) -> list[dict]:
        """XCTest's accessibility audit of the app in front (iOS 17+), through WebDriverAgent: one dict per issue
        with `auditType`, `compactDescription`, `detailedDescription` and `elementAttributes` (type, label, name,
        rect). `types` are XCUIAccessibilityAuditType names; empty is all of them."""
        found = (await self._call("POST", "/session/{sid}/wda/performAccessibilityAudit", {"auditTypes": types},
                                  timeout=AUDIT_TIMEOUT)).get("value")
        return [i for i in found if isinstance(i, dict)] if isinstance(found, list) else []

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
    async def _touching(self, ms: float, at: tuple[float, float] | None = None) -> None:
        """Just before a touch that starts at `at`: wait out a notification banner over the top of the screen (for
        the agent's touches), then tell Tapline the touch is the agent's."""
        if self.banner_guard and at is not None:
            await self._clear_of_banner(at)
            self.top_touch = self.top_touch or at[1] < BANNER_ZONE
        if self.touch_hook is not None:
            await self.touch_hook(ms)

    async def _clear_of_banner(self, at: tuple[float, float]) -> None:
        """A touch near the top of the screen while a notification banner is there lands on the banner: a tap opens
        the notification's app, a drag down opens the notification out (seen on the phone 2026-11-10: a scroll to
        a list's start pulled a Mail banner open). While a banner shows, WebDriverAgent names SpringBoard as the
        app in front though the phone's process list still has the app: then this waits for it to leave, and
        refuses the touch if it doesn't. Touches lower down, and SpringBoard's own screens with no app in front
        (the home screen), are not held up."""
        if at[1] >= BANNER_ZONE:
            return
        waited = 0.0
        while await self.foreground_bundle_id() == SPRINGBOARD:
            try:
                in_front = await asyncio.wait_for(self.device.foreground_apps(), 5)
            except Exception:
                return  # the phone can't say: WebDriverAgent's answer alone is no reason to refuse
            if not in_front:
                return  # the home screen itself: nothing lies over an app
            if waited >= BANNER_WAIT:
                raise Refused("not_actionable", "covered: a notification is over the top of the screen, so nothing "
                                                "was touched; wait a few seconds, then try again.")
            await asyncio.sleep(BANNER_POLL)
            waited += BANNER_POLL

    async def tap(self, target: ui.Target, hold_ms: int = TAP_MS) -> str:
        self.typed_into = None
        x, y, what = self._target(target)
        await self._touching(hold_ms, (x, y))
        if not await self._fast_tap(x, y, hold_ms):
            await self._call("POST", "/session/{sid}/actions", ui.tap(x, y, hold_ms), timeout=hold_ms / 1000 + ACTION_WAIT)
        self.last_point = (x, y)
        return f"{'Long-pressed' if hold_ms > 300 else 'Tapped'} {what}."

    async def _fast_tap(self, x: float, y: float, hold_ms: int) -> bool:
        """Touch through the fork's fast tap route when this runner has it (see FAST_TAP). False when the touch is
        left to W3C actions: no route, not portrait (the route's points are portrait screen points), or an error
        that says nothing was played. An error that may come after the touch was played is raised, never retried."""
        if FAST_TAP == "0" or self.turn != 0 or self.screen[0] > self.screen[1]:
            return False
        await self.ensure()
        await self._fast_routes()
        if not self.fast_tap:
            return False
        body = {"x": x, "y": y, "duration": hold_ms / 1000, "wait": True}
        try:
            await self._call("POST", FAST_TAP_PATH, body, timeout=hold_ms / 1000 + ACTION_WAIT)
        except DeviceError as e:
            said = str(e).lower()
            if "unknown command" in said or "unknown method" in said:
                self.fast_tap = False  # not this runner's route after all
            elif not ("invalid argument" in said or "too many touches" in said):
                raise  # it may have been played (a timeout, XCTest's own error): touching again could do it twice
            getattr(self.device, "log", lambda *a: None)(f"fast tap refused ({e}): W3C actions instead")
            return False
        return True

    async def _fast_routes(self) -> None:
        """Ask the runner once per session which of the fork's fast routes it has (Appium's build: none)."""
        if self.fast_tap is not None:
            return
        try:
            answer = await self._call("GET", FAST_VERSION_PATH, timeout=LOCK_WAIT)
        except DeviceError:
            answer = {}
        value = answer.get("value")
        version = value.get("tapwright") if isinstance(value, dict) else None
        version = version if isinstance(version, int) and not isinstance(version, bool) else 0
        self.fast_tap, self.fast_drag = version >= 1, version >= 2

    async def _drag(self, points: list, w3c: dict) -> None:
        """Play a finger path, [(t_ms from the press, x, y), ...] in points: through the fork's fast drag route when
        this runner has it (see FAST_DRAG), else as the W3C actions `w3c`, which describe the same path."""
        if not await self._fast_drag(points):
            await self._call("POST", "/session/{sid}/actions", w3c, timeout=points[-1][0] / 1000 + ACTION_WAIT)

    async def _fast_drag(self, points: list) -> bool:
        """False when the drag is left to W3C actions: no route, not portrait, or an error that says nothing was
        played. An error that may come after the drag was played is raised, never retried: it would scroll twice."""
        if FAST_DRAG == "0" or FAST_TAP == "0" or self.turn != 0 or self.screen[0] > self.screen[1]:
            return False
        await self.ensure()
        await self._fast_routes()
        if not self.fast_drag:
            return False
        body = {"points": [[round(t), round(x), round(y)] for t, x, y in points]}  # W3C actions round them too
        try:
            await self._call("POST", FAST_DRAG_PATH, body, timeout=points[-1][0] / 1000 + ACTION_WAIT)
        except DeviceError as e:
            said = str(e).lower()
            if "unknown command" in said or "unknown method" in said:
                self.fast_drag = False  # not this runner's route after all
            elif not ("invalid argument" in said or "too many touches" in said):
                raise  # it may have been played (a timeout, XCTest's own error): dragging again could do it twice
            getattr(self.device, "log", lambda *a: None)(f"fast drag refused ({e}): W3C actions instead")
            return False
        return True

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
        element, hit, again, at_end = None, None, False, False
        # with the keyboard already up, the focusing tap moves focus from another field, and iOS may swap in that
        # field's keyboard as the keys go out: some went missing on the phone ("abc" landed as "a", 2026-11-11)
        switched = self.last_keyboard
        self.retyped = False
        if target is not None:
            what = self._target(target)[2]
            hit = ui.resolve(self.items, target)
            key = _field_key(hit) if not isinstance(hit, tuple) else None
            focused, self.typed_into = self.typed_into, None
            if key and focused and focused[0] == key and await self._active_element() == focused[1]:
                element, again = focused[1], True  # still focused by our own typing, nothing done since: no tap
            else:
                # Old text to replace: the tap goes near the field's right end, where iOS puts the caret at the end
                # of short text, so backspaces can delete it in the same call as the new keys (0.45 s less than
                # WebDriverAgent's clear, measured on the phone 2026-11-11). Only where the field's middle shows
                # (no `at`), there is room, and nothing is pressed after the text; the value is read back after.
                at_end = (replace and not submit and at is None and isinstance(hit, ui.Element) and not hit.secure
                          and hit.value not in ("", hit.placeholder) and len(hit.value) <= BACKSPACE_MAX
                          and hit.width >= 2 * END_INSET)
                if at_end:
                    await self.tap(ui.Target(x=hit.x + hit.width - END_INSET, y=hit.y + hit.height / 2))
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
        elif replace and (again or at_end) and field is not None and not field.secure and len(field.value) <= BACKSPACE_MAX:
            keys = "\b" * len(field.value) + keys  # the cursor is at the end of the old text: delete it
        elif replace:
            element = element or await self._active_element()
            if element:
                await self._call("POST", f"/session/{{sid}}/element/{element}/clear", {})
            else:
                note = " (couldn't find the focused field to clear, so the old text may remain)"
        await self._touching(KEY_MS * len(keys))
        await self._call("POST", "/session/{sid}/wda/keys", {"value": list(keys)})
        if at_end and element and await self._attribute(element, "value") != text:
            # the caret wasn't at the end (the old text ran past the tap): the field holds a mix. Clear it and
            # type again, the long way; the answer reads the value afterwards either way
            await self._call("POST", f"/session/{{sid}}/element/{element}/clear", {})
            await self._touching(KEY_MS * len(text))
            await self._call("POST", "/session/{sid}/wda/keys", {"value": list(text)})
        elif (replace and not submit and switched and not again and element and field is not None
              and not field.secure and await self._attribute(element, "value") != text):
            # keys went missing as focus moved between fields: the field holds something else. Cleared and typed
            # once more; the answer says so (Daemon, NOTE_RETYPED). Typing again into the same field isn't checked:
            # its keyboard is already the field's own (and that path is the fast one)
            await self._call("POST", f"/session/{{sid}}/element/{element}/clear", {})
            await self._touching(KEY_MS * len(text))
            await self._call("POST", "/session/{sid}/wda/keys", {"value": list(text)})
            self.retyped = True
        if field is not None and element and not submit:  # return may move focus on; keys never do
            self.typed_into = (_field_key(field), element)
        return f"typed {len(text)} characters" + (" and pressed return" if submit else "") + note

    async def retype(self, text: str) -> bool:
        """Clear the focused field and type `text` again, once: the first keys went missing (the daemon saw the
        field holding something else). False when no field has focus."""
        element = await self._active_element()
        if not element:
            return False
        await self._call("POST", f"/session/{{sid}}/element/{element}/clear", {})
        await self._touching(KEY_MS * len(text))
        await self._call("POST", "/session/{sid}/wda/keys", {"value": list(text)})
        return True

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
        await self._touching(duration_ms + hold_ms, (x0, y0))
        await self._drag(ui.swipe_points(x0, y0, x1, y1, duration_ms, hold_ms),
                         ui.swipe(x0, y0, x1, y1, duration_ms, hold_ms))
        self.last_point = (x0, y0)
        return f"swiped ({round(x0)}, {round(y0)}) -> ({round(x1)}, {round(y1)})"

    async def scroll(self, x0: float, y0: float, x1: float, y1: float) -> str:
        """A scroll drag that stops where the finger does (ui.scroll_drag)."""
        self.typed_into = None
        await self._touching(SCROLL_MS, (x0, y0))
        await self._drag(ui.scroll_drag_points(x0, y0, x1, y1), ui.scroll_drag(x0, y0, x1, y1))
        self.last_point = (x0, y0)
        return f"scrolled ({round(x0)}, {round(y0)}) -> ({round(x1)}, {round(y1)})"

    async def path(self, points: list) -> str:
        """Replay a finger path: [(t_ms from 0, x, y), ...] in points."""
        self.typed_into = None
        await self._touching(points[-1][0], (points[0][1], points[0][2]))
        await self._drag(ui.path_points(points), ui.path(points))
        self.last_point = (points[0][1], points[0][2])
        return f"dragged through {len(points)} points"

    async def press_button(self, name: str) -> str:
        self.typed_into = None
        if name == "home":  # pressButton "home" does nothing on iOS 26 (measured); this one waits for the home screen
            await self._call("POST", "/wda/homescreen", {})
        else:
            await self._call("POST", "/session/{sid}/wda/pressButton", {"name": name})
        return f"pressed {name}"

    async def rotate(self, to: str) -> str:
        """Turn the phone (XCUIDevice's orientation, no touch). The screen's new size comes with the next read."""
        await self._call("POST", "/session/{sid}/orientation", {"orientation": ORIENTATIONS[to]})
        return f"rotated to {to.replace('_', ' ')}"

    async def key(self, name: str) -> str:
        """backspace or enter, into whatever field has focus."""
        if name not in KEYS:
            raise Refused("bad_params", f"key must be one of {', '.join(KEYS)}, not {name!r}")
        self.typed_into = None  # a backspace moves the cursor's text; enter may move focus
        await self._touching(KEY_MS)
        await self._call("POST", "/session/{sid}/wda/keys", {"value": [KEYS[name]]})
        return f"pressed {name}"

    async def settings(self, values: dict) -> None:
        """WebDriverAgent settings, e.g. the MJPEG stream's frame rate and size."""
        await self._call("POST", "/session/{sid}/appium/settings", {"settings": values}, timeout=READ_CUT, again=True)

    async def locked(self) -> bool:
        return bool((await self._call("GET", "/session/{sid}/wda/locked", timeout=LOCK_WAIT)).get("value"))

    async def alert(self, action: str) -> str:
        """accept, dismiss, or text: the system alert on screen (permission prompts and the like)."""
        if action != "text":
            self.typed_into = None
        try:
            if action == "text":
                return (await self._call("GET", "/session/{sid}/alert/text")).get("value") or "(no alert)"
            await self._touching(TAP_MS)
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

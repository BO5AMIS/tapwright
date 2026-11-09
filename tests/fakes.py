"""Stand-ins for tests: a clock that runs only when told, and (Task 5) a fake Session."""
import asyncio
import io

from PIL import Image

from tapwright import ui
from tapwright.device import ConnectionLost, DeviceError, NotSetUp
from tapwright.session import Snapshot


def image_bytes(size, color, fmt="PNG") -> bytes:
    """A solid picture, for the stream frame or `/screenshot` of a test."""
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, fmt)
    return buf.getvalue()


class Clock:
    """Fake time for Controller: sleep() moves it forward instantly."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds
        await asyncio.sleep(0)


class FrameFeeder:
    """Publishes fake frames into a FrameHub from a background task, a few ms apart.
    `frames` go out once; then `then` repeats forever, or (churn=True) a new picture every time."""

    def __init__(self, hub, every: float = 0.004) -> None:
        self.hub, self.every, self._task = hub, every, None

    def start(self, frames=(), then=None, churn: bool = False) -> "FrameFeeder":
        async def feed():
            for frame in frames:
                self.hub.publish(frame)
                await asyncio.sleep(self.every)
            i = 0
            while then is not None or churn:
                self.hub.publish(b"churn%d" % i if churn else then)
                i += 1
                await asyncio.sleep(self.every)
        self._task = asyncio.create_task(feed())
        return self

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)


class FakeDevice:
    def __init__(self) -> None:
        self.rsd = "tunnel"
        self.via = "USB"

    async def open_stream(self):
        raise ConnectionLost("no stream in tests")


def button(ref: str, label: str, identifier: str = "", rect=(10, 10, 100, 40), **fields) -> ui.Element:
    """An element for a fake screen."""
    return ui.Element(ref, "button", label, identifier, rect=rect, actions=("tap",), type="Button", **fields)


def el(ref: str, role: str, label: str = "", identifier: str = "", rect=(10, 10, 100, 40), **fields) -> ui.Element:
    """An element of any role, with the actions its role gets."""
    return ui.Element(ref, role, label, identifier, rect=rect, actions=ui.ROLE_ACTIONS.get(role, ()), **fields)


class FakeSession:
    """Session's interface, no phone. `fail[name] = exc` makes the next call of that name raise.
    `tree` is the screen: what look() and refresh() read into `items`."""
    SCREEN_TEXT = 'Screen 400x800 pt · 1 elements\ne1 button "OK" id=ok-button @10,10 100x40 [tap]'

    def __init__(self) -> None:
        self.device = FakeDevice()
        self.screen = (400.0, 800.0)
        self.items: list = []
        self.last_point = None
        self.sid = None
        self.calls: list = []
        self.fail: dict = {}
        self.is_locked = False
        self.not_set_up = False
        self.ensure_delay = 0.0
        self.scale_reads = 0
        self.last_alert: ui.Alert | None = None
        self.last_keyboard = False
        self.locked_errors = 0  # the next N locked() calls raise a DeviceError
        self.tree: list[ui.Element] = [button("e1", "OK", "ok-button")]
        self.app: tuple[str, str] | None = ("Example", "com.example.app")
        self.installed = [{"bundle_id": "com.example.app", "name": "Example", "version": "1.0", "build": "7"}]
        self.app_names: dict[str, str] = {}  # filled by read_app_names, as Session does at connect
        self.foreground: str | None = None  # the bundle id the phone shows
        self.back_at: tuple[float, float] | None = None  # where the navigation bar's back button is (None: none)
        self.has_nav_bar = False  # a NavigationBar without a back button (back_at: one with it)
        self.launched: list = []
        self.closed: list = []
        self.read_timeouts: list = []  # the per-read timeout each refresh() was given
        self.last_insets: tuple[float | None, float | None] = (None, None)  # where the top and bottom bars end
        self.typed: list = []  # (text, replace, submit, target) of each type_text
        self.holds: list = []  # the hold at the end of each swipe, in ms

    def _record(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fail:
            raise self.fail.pop(name)

    def actions(self) -> list:
        """The calls that act on the phone, without heartbeats and settings."""
        return [c for c in self.calls if isinstance(c, str) and c not in ("locked", "ensure", "reset", "close")]

    async def ensure(self):
        self._record("ensure")
        if self.not_set_up:
            raise NotSetUp("WebDriverAgent isn't set up on this phone yet: run `tapwright setup`")
        if self.ensure_delay:
            await asyncio.sleep(self.ensure_delay)
        self.device.rsd = "tunnel"
        self.sid = "S"

    async def reset(self):
        self.calls.append("reset")
        self.device.rsd, self.sid = None, None

    async def settings(self, values):
        self.calls.append(("settings", dict(values)))

    async def locked(self):
        self._record("locked")
        if self.locked_errors:
            self.locked_errors -= 1
            raise DeviceError("HTTP 500: boom")
        return self.is_locked

    async def look(self, screenshot=True):
        self._record("look")
        self.items = list(self.tree)
        return self.SCREEN_TEXT, (b"JPEG" if screenshot else None)

    async def screenshot_png(self):
        self._record("screenshot")
        return image_bytes((800, 1600), (0, 255, 0), "PNG")  # green: tells it apart from the blue stream frame

    async def snapshot(self, keyboard=False, timeout=None):
        self._record("snapshot")
        self.items = list(self.tree)
        return Snapshot(ui.Tree(self.items, 0, self.last_alert, self.last_keyboard), self.app, 3.0)

    async def read_scale(self, refresh=False):
        self.scale_reads += 1
        return 3.0

    async def refresh(self, timeout=None):
        self._record("refresh")
        self.read_timeouts.append(timeout)
        self.items = list(self.tree)
        return ui.Tree(self.items, 0, self.last_alert, self.last_keyboard)

    async def snapshot_from(self, tree):
        """The foreground app for a tree that was already read: the one extra request a final answer needs."""
        self._record("app")
        return Snapshot(tree, self.app, 3.0)

    async def apps(self):
        self._record("apps")
        self.app_names = {a["bundle_id"]: a["name"] for a in self.installed}
        return list(self.installed)

    async def read_app_names(self, refresh=False):
        self.app_names = {a["bundle_id"]: a["name"] for a in self.installed}

    async def foreground_bundle_id(self):
        return self.foreground

    async def back_point(self):
        return ui.Back(self.has_nav_bar or self.back_at is not None, self.back_at)

    async def tap(self, target, hold_ms=60):
        hit = ui.resolve(self.items, target)  # a refused target never reaches the phone
        self._record("tap")
        if isinstance(hit, tuple):
            self.last_point = hit
            return f"Tapped ({round(hit[0])}, {round(hit[1])})."
        self.last_point = hit.center
        return f"Tapped {hit.line(actions=False)}."

    async def type_text(self, text, replace=False, submit=False, target=None, at=None):
        if target is not None:
            hit = ui.resolve(self.items, target)  # a missing or ambiguous field is refused, as in Session
            self.last_point = at or (hit if isinstance(hit, tuple) else hit.center)  # where the focusing tap went
        self._record("type_text")
        self.typed.append((text, replace, submit, target))
        return f"typed {len(text)} characters"

    async def swipe(self, x0, y0, x1, y1, duration_ms=350, hold_ms=0):
        self._record("swipe")
        self.holds.append(hold_ms)
        self.last_point = (x0, y0)
        return f"swiped ({round(x0)}, {round(y0)}) -> ({round(x1)}, {round(y1)})"

    async def path(self, points):
        self._record("path")
        self.last_point = (points[0][1], points[0][2])
        return f"dragged through {len(points)} points"

    async def key(self, name):
        self._record(f"key:{name}")
        return f"pressed {name}"

    async def press_button(self, name):
        self._record(f"button:{name}")
        return f"pressed {name}"

    async def launch(self, bundle_id):
        self._record("launch")
        self.launched.append(bundle_id)
        return f"launched {bundle_id}"

    async def terminate(self, bundle_id):
        self._record("terminate")
        self.closed.append(bundle_id)
        return f"closed {bundle_id}"

    async def alert(self, action):
        self._record("alert")
        return f"alert {action}ed"

    async def open_url(self, url):
        self._record("open_url")
        return f"opened {url}"

    async def close(self):
        self.calls.append("close")


class _VirtualSelector:
    """Selector whose waits advance the loop's clock instead of sleeping."""

    def __init__(self, inner, loop) -> None:
        self._inner, self._loop = inner, loop

    def select(self, timeout=None):
        if timeout:
            self._loop.t += timeout
        return self._inner.select(0)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class VirtualLoop(asyncio.SelectorEventLoop):
    """An event loop on fake time: timers fire in order, instantly. Pass as asyncio.run's loop_factory."""

    def __init__(self) -> None:
        super().__init__()
        self.t = 0.0
        self._selector = _VirtualSelector(self._selector, self)

    def time(self) -> float:
        return self.t

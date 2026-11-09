"""Stand-ins for tests: a clock that runs only when told, and (Task 5) a fake Session."""
import asyncio

from tapwright import ui
from tapwright.device import ConnectionLost, DeviceError, NotSetUp


class Clock:
    """Fake time for Controller: sleep() moves it forward instantly."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds
        await asyncio.sleep(0)


class FakeDevice:
    def __init__(self) -> None:
        self.rsd = "tunnel"
        self.via = "USB"

    async def open_stream(self):
        raise ConnectionLost("no stream in tests")


class FakeSession:
    """Session's interface, no phone. `fail[name] = exc` makes the next call of that name raise."""
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
        self.locked_errors = 0  # the next N locked() calls raise a DeviceError

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
        self.items = [ui.Element("e1", "button", "OK", "ok-button", rect=(10, 10, 100, 40), actions=("tap",),
                                 type="Button")]
        return self.SCREEN_TEXT, (b"JPEG" if screenshot else None)

    async def apps(self):
        self._record("apps")
        return [{"bundle_id": "com.example.app", "name": "Example", "version": "1.0", "build": "7"}]

    async def tap(self, hold_ms=60, ref=None, id=None, label=None, x=None, y=None, index=0):
        self._record("tap")
        if x is not None and y is not None:
            self.last_point = (float(x), float(y))
            return f"tapped ({round(x)}, {round(y)})"
        e = ui.find(self.items, ref=ref, id=id, label=label, index=index)
        self.last_point = e.center
        return f"tapped {e.line()}"

    async def type_text(self, text, replace=False, submit=False, **target):
        self._record("type_text")
        return f"typed {len(text)} characters"

    async def swipe(self, x0, y0, x1, y1, duration_ms=350):
        self._record("swipe")
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

    async def scroll(self, direction="down", amount=0.5):
        self._record("scroll")
        return f"scrolled {direction}"

    async def launch(self, bundle_id):
        self._record("launch")
        return f"launched {bundle_id}"

    async def terminate(self, bundle_id):
        self._record("terminate")
        return f"closed {bundle_id}"

    async def alert(self, action):
        self._record("alert")
        return f"alert {action}ed"

    async def open_url(self, url):
        self._record("open_url")
        return f"opened {url}"

    async def close(self):
        self.calls.append("close")

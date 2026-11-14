"""Stand-ins for tests: a clock that runs only when told, and (Task 5) a fake Session."""
import dataclasses
import asyncio
import io
import time

from PIL import Image

from tapwright import ui
from tapwright.device import ConnectionLost, DeviceError, NotSetUp
from tapwright.session import BANNER_ZONE, FrontUnchecked, Snapshot


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
    `frames` go out once; then `then` repeats forever, or (churn=True) a new picture every time.
    `then` may be set while the feeder runs: a test that changes the screen on a touch sets it there, so the
    change doesn't depend on when the feeder's first frame goes out against the action's mark."""

    def __init__(self, hub, every: float = 0.004) -> None:
        self.hub, self.every, self._task = hub, every, None
        self.then: bytes | None = None

    def start(self, frames=(), then=None, churn: bool = False) -> "FrameFeeder":
        self.then = then

        async def feed():
            for frame in frames:
                self.hub.publish(frame)
                await asyncio.sleep(self.every)
            i = 0
            while self.then is not None or churn:
                self.hub.publish(b"churn%d" % i if churn else self.then)
                i += 1
                await asyncio.sleep(self.every)
        self._task = asyncio.create_task(feed())
        return self

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)


class FakeVideo:
    """A video hub's `live` and `newest()`, for the feeder: `show` makes a picture the newest one."""

    def __init__(self) -> None:
        self.live = True
        self.current = None
        self.qualities: list[str] = []

    def show(self, picture, arrived: float | None = None) -> None:
        self.current = (picture, time.monotonic() if arrived is None else arrived)

    def newest(self):
        return self.current

    def set_quality(self, preset: str) -> None:
        self.qualities.append(preset)

    def close(self) -> None:
        pass


@dataclasses.dataclass
class FakeFrame:
    image_name: str | None
    symbol: str | None = None
    symbol_offset: int | None = None
    image_offset: int | None = None


class FakeReport:
    """A parsed crash report, with the fields of pycrashreport's UserModeCrashReport that Tapwright reads."""

    def __init__(self, name="Example", when=None, kind="EXC_BAD_ACCESS (SIGSEGV)", subtype="KERN_INVALID_ADDRESS at 0x0",
                 frames=None, filename="/Example-2026-11-12-101010.ips", info=None, bug_type="309", bundle_id=None,
                 stamp=None) -> None:
        import datetime
        self.name, self.timestamp = name, when or datetime.datetime.now()
        self.bug_type_str = bug_type  # 309 and 109 are crashes; 385 and the like are resource reports
        # the .ips header (its first JSON line): `bundleID`, and `timestamp` with the phone's UTC offset
        self._metadata = {"name": name, "bug_type": bug_type, **({"bundleID": bundle_id} if bundle_id else {}),
                          **({"timestamp": stamp} if stamp else {})}
        self._data = {}
        self.exception_type, self.exception_subtype, self.filename = kind, subtype, filename
        self.application_specific_information = info
        app = "/private/var/containers/Bundle/Application/X/Example.app/Example"
        self.frames = frames if frames is not None else [
            FakeFrame("/usr/lib/system/libsystem_kernel.dylib", "__pthread_kill", 8),
            FakeFrame(app, "-[HomeController crash]", 40), FakeFrame(app, "main", 12),
            FakeFrame("/System/Library/Frameworks/UIKitCore.framework/UIKitCore", "UIApplicationMain", 340)]


class FakeMeter:
    """device.AppMeter's interface: start(), then stop() gives the Measurement a test set on the device."""

    def __init__(self, device, bundle_id: str) -> None:
        self.device, self.bundle_id = device, bundle_id

    async def start(self) -> None:
        self.device.metered.append(("start", self.bundle_id))
        if self.device.meter_error is not None:
            raise self.device.meter_error

    async def begin_window(self, fresh: bool = False) -> None:
        self.device.windows.append(fresh)
        if self.device.on_window is not None:
            self.device.on_window(fresh)

    async def stop(self):
        self.device.metered.append(("stop", self.bundle_id))
        return self.device.measurement


class FakeDevice:
    def __init__(self) -> None:
        from tapwright.performance import Measurement
        self.measurement = Measurement()  # what a meter's stop() returns (tests set it)
        self.metered: list = []  # ("start" | "stop", bundle id) in order
        self.meter_error: Exception | None = None
        self.windows: list[bool] = []  # one per begin_window of a meter (True: a launch's)
        self.on_window = None  # called with `fresh` when a window begins (tests look at what has happened by then)
        self.rsd = "tunnel"
        self.via = "USB"
        self.killed: list[str] = []  # bundle ids whose processes kill_app ended
        self.kill_asks: list[str] = []  # bundle ids kill_app was asked for, ended or not
        self.kill_error: Exception | None = None  # kill_app raises this
        self.kill_hangs = False  # kill_app never answers
        self.kill_found = 1  # how many processes kill_app says it ended (0: the app had none to end)
        self.opened = 0  # how often the MJPEG stream was asked for
        # bundle id -> the pids of its running processes; an app not listed here is running (as pid 1)
        self.pids: dict[str, list[int]] = {}
        self.streams: dict[int, asyncio.Queue] = {}  # pid -> entries its log stream will give (see emit)
        self.streamed: list[int] = []  # the pids a log stream was asked for
        self.crashes: dict[str, list] = {}  # executable -> reports, newest first
        self.crash_files: dict[str, bytes] = {}  # path on the phone -> the whole report
        self.crash_asks = 0
        self.stream_error: Exception | None = None  # app_log_stream raises this when asked
        self.stream_closed: list[int] = []  # the pids whose log streams have ended
        self.replay: dict[int, list] = {}  # pid -> entries every new stream of it gives first (the phone's replay)
        self.replay_delay = 0.0  # seconds a new stream takes before it gives its replay (a slow phone)
        self.phone_ms: int | None = 0  # the phone's clock, epoch ms (None: it can't be read)
        self.crash_error: Exception | None = None  # crash_reports / crash_file raise this
        self.crash_unreadable = 0  # how many reports crash_reports says it passed over
        self.crash_too_big = 0  # how many it says were over the byte cap
        self.app_pids_calls = 0
        self.network_calls: list = []  # ("set", mode) | ("clear",) | ("drop",) in order
        self.network_error: Exception | None = None  # set_network raises this
        self.starts: dict[int, float] = {}  # pid -> when its process started (epoch s); a pid not listed has no start time
        self.holders: dict[int, tuple[str, float | None]] = {}  # pid -> (bundle id, start) of whatever holds it now, if
        #                                                         that is not what `pids` says (a pid taken by another app)
        self.proc_error: Exception | None = None  # the process list can't be read

    async def app_pids(self, bundle_id: str) -> list[int]:
        self.app_pids_calls += 1
        return list(self.pids.get(bundle_id, [1]))

    async def app_processes(self, bundle_id: str):
        from tapwright.device import Proc
        self.app_pids_calls += 1
        if self.proc_error is not None:
            raise self.proc_error
        return [Proc(pid, self.starts.get(pid), bundle_id) for pid in self.pids.get(bundle_id, [1])]

    async def phone_time_ms(self) -> int | None:
        return self.phone_ms

    async def process_holder(self, pid: int):
        """Who has that pid now (a Proc), or None when nobody does."""
        from tapwright.device import Proc
        if self.proc_error is not None:
            raise self.proc_error
        if pid in self.holders:
            bundle, start = self.holders[pid]
            return Proc(pid, start, bundle)
        for bundle, pids in self.pids.items():
            if pid in pids:
                return Proc(pid, self.starts.get(pid), bundle)
        return None

    def emit(self, pid: int, at: int, text: str, raw: int = 0, from_pid: int | None = None) -> None:
        """An entry the log stream of `pid` gives (os_trace level byte `raw`); `from_pid` is the pid the entry says
        it comes from (another process's, to prove it is dropped)."""
        self.streams.setdefault(pid, asyncio.Queue()).put_nowait((at, raw, text, pid if from_pid is None else from_pid))

    async def app_log_stream(self, pid: int):
        """The pid's stream: what the phone replays first (`replay`), then what `emit` gives; a None put in the queue
        ends it, as a relay that closes does. `stream_error` makes it fail at once, as a phone without the service."""
        self.streamed.append(pid)
        if self.stream_error is not None:
            raise self.stream_error
        queue = self.streams.setdefault(pid, asyncio.Queue())
        try:
            if self.replay_delay:
                await asyncio.sleep(self.replay_delay)
            for entry in self.replay.get(pid, []):
                yield entry
            while True:
                entry = await queue.get()
                if entry is None:
                    return
                yield entry
        finally:
            self.stream_closed.append(pid)

    async def crash_reports(self, executable: str, count: int, bundle_id: str | None = None):
        from tapwright.diagnostics import CrashSearch
        self.crash_asks += 1
        if self.crash_error is not None:
            raise self.crash_error
        return CrashSearch(list(self.crashes.get(executable, []))[:count], self.crash_unreadable, self.crash_too_big)

    async def crash_file(self, filename: str) -> bytes:
        if self.crash_error is not None:
            raise self.crash_error
        return self.crash_files[filename]

    async def open_stream(self):
        self.opened += 1
        raise ConnectionLost("no stream in tests")

    async def kill_app(self, bundle_id: str) -> int:
        self.kill_asks.append(bundle_id)
        if self.kill_error is not None:
            raise self.kill_error
        if self.kill_hangs:
            await asyncio.sleep(3600)  # never answers; the caller's timeout ends it
        if self.kill_found:
            self.killed.append(bundle_id)
        return self.kill_found

    def meter(self, bundle_id: str) -> FakeMeter:
        return FakeMeter(self, bundle_id)

    async def set_network(self, mode: str) -> str:
        if self.network_error is not None:
            raise self.network_error
        self.network_calls.append(("set", mode))
        return f"profile-{mode}"

    async def clear_network(self) -> None:
        self.network_calls.append(("clear",))

    def drop_network(self) -> None:
        self.network_calls.append(("drop",))

    async def clear_leftover(self) -> None:
        self.network_calls.append(("leftover",))


def button(ref: str, label: str, identifier: str = "", rect=(10, 100, 100, 40), **fields) -> ui.Element:
    """An element for a fake screen."""
    return ui.Element(ref, "button", label, identifier, rect=rect, actions=("tap",), type="Button", **fields)


def el(ref: str, role: str, label: str = "", identifier: str = "", rect=(10, 100, 100, 40), **fields) -> ui.Element:
    """An element of any role, with the actions its role gets."""
    return ui.Element(ref, role, label, identifier, rect=rect, actions=ui.ROLE_ACTIONS.get(role, ()), **fields)


class FakeSession:
    """Session's interface, no phone. `fail[name] = exc` makes the next call of that name raise.
    `tree` is the screen: what look() and refresh() read into `items`."""
    SCREEN_TEXT = 'Screen 400x800 pt · 1 elements\ne1 button "OK" id=ok-button @10,100 100x40 [tap]'

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
        self.installed = [{"bundle_id": "com.example.app", "name": "Example", "version": "1.0", "build": "7",
                          "executable": "Example"}]
        self.app_names: dict[str, str] = {}  # filled by read_app_names, as Session does at connect
        self.foreground: str | None = None  # the bundle id the phone shows
        self.tree_app: tuple[str, str] | None = None  # the root Application a read names (None: it names none)
        self.front_asks = 0  # how many times foreground_bundle_id (the separate activeAppInfo question) was asked
        self.back_at: tuple[float, float] | None = None  # where the navigation bar's back button is (None: none)
        self.has_nav_bar = False  # a NavigationBar without a back button (back_at: one with it)
        self.dismiss_at: tuple[float, float] | None = None  # where a touch closes an open popover (None: none open)
        self.launched: list = []
        self.launch_args: list = []  # the launch arguments of each launch ([] for none)
        self.closed: list = []
        self.terminate_hangs = False  # terminate never answers, as WebDriverAgent waiting on a slow app
        self.terminate_delay = 0.0  # seconds terminate takes before it answers
        self.terminate_error: Exception | None = None  # terminate raises this (after the delay)
        self.terminated: list = []  # bundle ids whose terminate ran to its end (a cancelled one is not here)
        self.read_timeouts: list = []  # the per-read timeout each refresh() was given
        self.last_insets: tuple[float | None, float | None] = (None, None)  # where the top and bottom bars end
        self.typed: list = []  # (text, replace, submit, target) of each type_text
        self.scrolls: list = []  # (x0, y0, x1, y1) of each eased scroll drag (the raw swipe tool's aren't recorded)
        self.offscreen: list = []  # elements the last read found outside the screen (tests set it)
        self.behind: list = []  # (element, what is in front of it) for what an open sheet hides (tests set it)
        self.focused = ""  # the identifier or label of the focused input (empty: none)
        self.tap_points: list = []  # where each tap went (the element's centre, or the point)
        self.blank_tap_closes = True  # a tap on a point no element holds closes the keyboard
        self.dismiss_ok = True  # whether WebDriverAgent's keyboard dismissal is accepted (else a refusal)
        self.dismiss_closes = False  # whether an accepted dismissal closes the keyboard
        # what every connect raises: a PhoneLocked, or an AppNotAnswering until kill_app ends that app
        self.stuck: Exception | None = None
        # (name, bundle id) of an app a notification banner brings to the front when a tap lands near the top
        self.taken_by: tuple[str, str] | None = None
        self.top_touch = False
        # a runner whose tap checks the app in front itself (route version 4, Session.front_by_touch): with
        # `front_apps` set, a tap touches only while `foreground` is one of them, else FrontUnchecked and no touch
        self.touch_checks_front = False
        self.front_apps: tuple[str, ...] | None = None
        self.checked_taps: list = []  # the given apps each checking tap was sent with
        # with this on, tap and swipe call `touch_hook` (set by the daemon for an agent's action) before they touch,
        # as Session._touching does; a hook that raises touches nothing. Off: the hook is never called (as before)
        self.calls_touch_hook = False
        self.touch_hook = None
        # the runner's hit test (route version 5, Session.hit_at): what it answers (None: no check, as an older runner)
        # and each point it was asked at; `last_source` is the XML the relation is judged in
        self.hit_answer: ui.Hit | None = None
        self.hit_asks: list = []
        self.last_source: str | None = None
        # Notification Center (center_tool.py): with `center_source` set (SpringBoard's XML), a swipe down from the top
        # edge opens it (SpringBoard in front), a swipe up from the bottom edge or home closes it, and a tap while it is
        # open opens `center_opens` (a bundle id). `read_center` answers its XML while it is open.
        self.center_source: str | None = None
        self.center_open = False
        self.center_opens: str | None = None
        self._center_before: str | None = None
        self.center_reads = 0

    async def read_center(self, timeout=10.0):
        self._record("read_center")
        self.center_reads += 1
        if self.center_open and self.center_source is not None:
            return self.center_source
        return '<AppiumAUT><XCUIElementTypeApplication bundleId="com.example.app"/></AppiumAUT>'

    def front_by_touch(self) -> bool:
        return self.touch_checks_front

    async def hit_at(self, x, y):
        self.hit_asks.append((x, y))
        return self.hit_answer

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
        if self.stuck is not None and getattr(self.stuck, "bundle_id", None) not in self.device.killed:
            raise self.stuck
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

    audit_issues: list = []  # what `audit` answers: WebDriverAgent's issue dicts
    tw_version = 3  # the runner's route version (media needs 3)
    photo_access: dict = {"add": "authorized", "full": "authorized"}
    photo_deletes = 1  # how many a delete reports (0: the person declined); an Exception is raised instead

    async def runner_version(self):
        self._record("runner_version")
        return self.tw_version

    async def photos_access(self):
        self._record("photos_access")
        return dict(self.photo_access)

    photos_gate: "asyncio.Event | None" = None  # while set and not set(): photos_add waits, as the runner for iOS's prompt

    async def peek_alert(self, timeout=5.0):
        """The system alert on screen now, read apart from `items` (what media's watch asks while it waits)."""
        self._record("peek_alert")
        return self.last_alert

    async def photos_add(self, pictures):
        self._record(("photos_add", tuple(name for name, _ in pictures)))
        if self.photos_gate is not None:
            await self.photos_gate.wait()
        self.photos = getattr(self, "photos", [])
        ids = [f"ID-{len(self.photos) + n}/L0/001" for n, _ in enumerate(pictures)]
        self.photos += ids
        return ids

    async def photos_delete(self, identifiers):
        self._record(("photos_delete", tuple(identifiers)))
        if isinstance(self.photo_deletes, Exception):
            raise self.photo_deletes
        return len(identifiers) if self.photo_deletes else 0

    async def audit(self, types):
        self._record(("audit", tuple(types)))
        return list(self.audit_issues)

    async def peek_tree(self, timeout=None):
        """A read that keeps nothing: `items` stay as they are (Session.peek_tree)."""
        self._record("peek_tree")
        return ui.Tree(list(self.tree), 0, self.last_alert, self.last_keyboard, app=self.tree_app)

    async def refresh(self, timeout=None):
        self._record("refresh")
        self.read_timeouts.append(timeout)
        self.items = list(self.tree)
        tree = ui.Tree(self.items, 0, self.last_alert, self.last_keyboard, app=self.tree_app)
        if getattr(self, "on_tree", None) is not None:  # (as the real Session's reads do)
            self.on_tree(tree)
        return tree

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
        self.front_asks += 1
        return self.foreground

    async def back_point(self):
        if self.dismiss_at is not None:
            return ui.Back(False, None, self.dismiss_at)
        return ui.Back(self.has_nav_bar or self.back_at is not None, self.back_at)

    async def is_focused(self, field):
        self._record("is_focused")
        return field.role == "input" and bool(self.focused) and self.focused in (field.identifier, field.label)

    async def dismiss_keyboard(self):
        self._record("dismiss_keyboard")
        if self.dismiss_ok and self.dismiss_closes:
            self.last_keyboard = False
        return self.dismiss_ok

    async def _touching(self, ms):
        if self.calls_touch_hook and self.touch_hook is not None:
            await self.touch_hook(ms)

    async def tap(self, target, hold_ms=60):
        if self.center_open and getattr(target, "x", None) is not None:
            self._record("tap")
            self.tap_points.append((target.x, target.y))
            if getattr(self, "center_tap_stays", False):  # the tap opened nothing: Notification Center is still down
                spot = getattr(self, "center_open_spot", None)  # iOS 26: the row's Open action, shown after a first tap
                if spot is not None and (target.x, target.y) == spot:
                    self.center_open, self.foreground = False, self.center_opens
                elif getattr(self, "center_after_tap", None) is not None:
                    self.center_source = self.center_after_tap
                return f"Tapped ({round(target.x)}, {round(target.y)})."
            self.center_open, self.foreground = False, self.center_opens
            return f"Tapped ({round(target.x)}, {round(target.y)})."
        hit = ui.resolve(self.items, target)  # a refused target never reaches the phone
        await self._touching(hold_ms)
        if self.front_apps is not None:
            self.checked_taps.append(self.front_apps)
            if self.foreground not in self.front_apps:
                raise FrontUnchecked(f"tapwright front: {self.foreground} is in front; nothing was touched")
            self.front_apps = None
        self._record("tap")
        self.tap_points.append(hit if isinstance(hit, tuple) else hit.center)
        if isinstance(hit, tuple):
            if self.blank_tap_closes and not any(e.actions and e.role != "scroll" and e.x <= hit[0] < e.x + e.width
                                                 and e.y <= hit[1] < e.y + e.height for e in self.items):
                self.last_keyboard = False  # a touch where nothing responds takes focus away: the keyboard goes
            self.last_point = hit
            return f"Tapped ({round(hit[0])}, {round(hit[1])})."
        self.last_point = hit.center
        self._maybe_taken(hit.center)
        return f"Tapped {hit.line(actions=False)}."

    async def type_text(self, text, replace=False, submit=False, target=None, at=None):
        if target is not None:
            hit = ui.resolve(self.items, target)  # a missing or ambiguous field is refused, as in Session
            self.last_point = at or (hit if isinstance(hit, tuple) else hit.center)  # where the focusing tap went
        self._record("type_text")
        self.typed.append((text, replace, submit, target))
        return f"typed {len(text)} characters"

    async def retype(self, text):
        """Session.retype: clear the focused field and type again. With `retype_into` set (an element in `tree`),
        the field then holds the text; without it there is no focused field to retype into."""
        self._record("retype")
        field = getattr(self, "retype_into", None)
        if field is None:
            return False
        self.tree = [dataclasses.replace(e, value=text) if e is field else e for e in self.tree]
        return True

    async def swipe(self, x0, y0, x1, y1, duration_ms=350, hold_ms=0):
        await self._touching(duration_ms + hold_ms)
        self._record("swipe")
        if self.center_source is not None and y0 < 5 and y1 > y0 and not self.center_open:
            self.center_open, self._center_before, self.foreground = True, self.foreground, "com.apple.springboard"
        elif self.center_open and y0 > self.screen[1] - 10 and y1 < y0:
            self.center_open, self.foreground = False, self._center_before
        self.last_point = (x0, y0)
        return f"swiped ({round(x0)}, {round(y0)}) -> ({round(x1)}, {round(y1)})"

    async def scroll(self, x0, y0, x1, y1):
        """Recorded, then as a swipe, so tests that watch swipes (MovingPicture) see scrolls too."""
        self.scrolls.append((x0, y0, x1, y1))
        return await self.swipe(x0, y0, x1, y1)

    async def path(self, points):
        self._record("path")
        self.last_point = (points[0][1], points[0][2])
        return f"dragged through {len(points)} points"

    async def key(self, name):
        self._record(f"key:{name}")
        return f"pressed {name}"

    async def press_button(self, name):
        self._record(f"button:{name}")
        if name == "home" and self.center_open:
            self.center_open, self.foreground = False, None
        return f"pressed {name}"

    def _maybe_taken(self, at) -> None:
        """A touch near the top: the daemon's banner window; with `taken_by` set, a banner takes it."""
        if at[1] < BANNER_ZONE:
            self.top_touch = True
            if self.taken_by is not None:
                self.app, self.foreground, self.taken_by = self.taken_by, self.taken_by[1], None

    async def launch(self, bundle_id, arguments=None):
        self._record("launch")
        self.launched.append(bundle_id)
        self.launch_args.append(list(arguments or []))
        self.foreground = bundle_id
        self.app = (self.app_names.get(bundle_id, bundle_id), bundle_id)
        return f"launched {bundle_id}"

    async def terminate(self, bundle_id):
        self._record("terminate")
        self.closed.append(bundle_id)
        if self.terminate_hangs:
            await asyncio.sleep(3600)  # WebDriverAgent waiting on the app; the caller's timeout ends it
        if self.terminate_delay:
            await asyncio.sleep(self.terminate_delay)
        if self.terminate_error is not None:
            raise self.terminate_error
        self.terminated.append(bundle_id)
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
    """An event loop on fake time: timers fire in order, instantly. Run with test_daemon.run_virtual."""

    def __init__(self) -> None:
        super().__init__()
        self.t = 0.0
        # the loop fires every timer due within one clock tick at once; before Python 3.13 that tick is 15.6 ms on
        # Windows, which put frames 4 ms apart at the same instant. Fake time ticks as finely as it is asked.
        self._clock_resolution = 1e-9
        self._selector = _VirtualSelector(self._selector, self)

    def time(self) -> float:
        return self.t


class DescribingSession(FakeSession):
    """A FakeSession whose look() prints the real header and element lines (ui.describe), with `above` and `below`
    counts of what lies past the screen, and whose snapshots carry them: for tests of what an answer shows."""

    def __init__(self) -> None:
        super().__init__()
        self.above = 0
        self.below = 0

    async def look(self, screenshot=True):
        self._record("look")
        self.items = list(self.tree)
        text = ui.describe(self.items, self.screen, 3.0, self.app, self.last_alert, self.last_keyboard, 0,
                           (self.above, self.below))
        return text, (b"JPEG" if screenshot else None)

    async def snapshot(self, keyboard=False, timeout=None):
        self._record("snapshot")
        self.items = list(self.tree)
        tree = ui.Tree(self.items, 0, self.last_alert, self.last_keyboard, above=self.above, below=self.below)
        return Snapshot(tree, self.app, 3.0)

    async def rotate(self, to):
        self._record(f"rotate:{to}")
        return f"rotated to {to}"

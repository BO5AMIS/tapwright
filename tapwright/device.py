"""The phone: the developer tunnel to it, Apple's developer disk image, and
WebDriverAgent running on it, reached over HTTP through the tunnel.

The tunnel is pymobiledevice3's user-space tunnel (no admin rights, any OS). It can run
over:
  * USB (the phone on a cable);
  * the local network, found by mDNS (the phone on the same Wi-Fi, paired once over USB);
  * any address you give it, such as the phone's Tailscale IP: the same network pairing,
    without mDNS. Whether iOS accepts that over a VPN is what `tapwright probe` measures.
"""
import asyncio
import contextvars
import dataclasses
import functools
import json
import os
import posixpath
import re
import socket
import sys
import time
import weakref
from pathlib import Path

from . import aio, store, trace
from . import diagnostics
from .control import Refused
from .diagnostics import CRASH_MAX_MB, CrashSearch, crash_pattern, only_mine

# Request-local ownership check, carried through connection setup without changing
# device/fake method signatures or sharing a guard with viewer/background requests.
BEFORE_SEND = contextvars.ContextVar("before_device_send", default=None)
TRACE = os.environ.get("TAPWRIGHT_TRACE") == "1"  # (the runner's requests are traced in session.py)


def _traced(method):
    """With TAPWRIGHT_TRACE=1: log how long this call to one of the phone's own services took. Each opens a service on
    the tunnel, which costs from 50 ms to over a second, and none of them shows in the runner's trace."""
    if not TRACE:
        return method

    @functools.wraps(method)
    async def timed(self, *args, **kwargs):
        began = time.monotonic()
        try:
            return await method(self, *args, **kwargs)
        finally:
            self.log(f"trace device {method.__name__} {(time.monotonic() - began) * 1000:.0f} ms")
    return timed


async def check_before_send() -> None:
    guard = BEFORE_SEND.get()
    if guard is not None:
        await guard()


WDA_PORT = 8100
MJPEG_PORT = 9100  # WebDriverAgent's screen stream (MJPEG)
MIN_TIMEOUT = 0.5  # seconds: the least any one request to WebDriverAgent is given
KEEP_IDLE = 2.0  # seconds a kept connection may sit idle and still carry an action (see Device._on_kept)
DIRECT_RETRY = 30.0  # seconds the tunnel carries the runner's ports after a direct dial failed, before usbmuxd is tried again
STOP_WAIT = 5.0  # seconds _stop_runner waits for the runner task to end after cancelling it
STAGE_CHUNK = 4 * 1024 * 1024  # bytes of a video copied to the phone at a time (AFC reads in 4 MB too)
_CHANGED = ("{name} changed on this computer since media checked it (it is still being written, or another file was put "
            "in its place): nothing was added; call media again once the file is complete")
TEST_SERVICE = "com.apple.dt.testmanagerd.remote"  # offered once the developer disk image is mounted


class DeviceError(RuntimeError):
    pass


class ConnectionLost(DeviceError):
    """The tunnel or WebDriverAgent stopped answering: a dead line, not an error reply."""


class Unanswered(DeviceError):
    """One action got no answer, and WebDriverAgent answers other requests: the line is alive, and whether the
    action happened isn't known. Not a ConnectionLost: nothing needs starting again."""


class RunnerTimedOut(DeviceError):
    """WebDriverAgent answered that its own wait ran out ("timeout"): the work it waited on may still be going on (the
    photo library's change of a media add), so it is no refusal (review R179 M1)."""


def _line_dropped(e: BaseException) -> bool:
    """Whether an error from pymobiledevice3 (or its socket) means the connection to the phone is gone, not a refusal:
    a media copy cut short by it answers `reconnecting`, not `unsupported` (review R179b L6)."""
    return (isinstance(e, (ConnectionError, EOFError, TimeoutError))
            or type(e).__name__ in LINE_ERRORS)


class PhoneLocked(ConnectionLost):
    """WebDriverAgent runs, but the phone is locked, so nothing on the screen can be read until it's unlocked."""


AUTH_PROMPT = "LocalAuthenticationUIService"  # the process that draws iOS's passcode screen
# The processes that draw widgets (chronod's renderers, e.g. com.apple.chrono.WidgetRenderer-Default): with an
# interactive widget on the home screen iOS lists one as a foreground application (phone, 2026-11-26). Never the app in
# front: the home screen is. (The runner says the same from route version 15.)
WIDGET_HOSTS = "com.apple.chrono."
PROMPT_AFTER = 6.0  # seconds a start may take before the phone is asked whether that screen is up (a start takes 3)
PROMPT_EVERY = 3.0
PROMPT_RETRY = 300.0  # seconds after which a start is tried anyway, in case the process list is wrong about the screen
# iOS fails a start that waits at the passcode screen after a minute or a few, and takes the screen away; only a
# new start puts it back. Each start launches the test runner, which costs the app in front a longer frame gap
# (measured by Tapline: 0.25 to 0.36 s where 0.23 s is usual). So with nobody asking for the phone, the starts
# thin out: the pause before the 1st, 2nd, 3rd... new start after the screen went. A tool call or a viewer asks
# for one at once (`hurry`).
PROMPT_PAUSES = (0.0, 30.0, 60.0, 120.0, 300.0)


PASSCODE_WORDS = "asking for its passcode"  # in PasscodeNeeded's message; needs.py reads the daemon's detail by it


class PasscodeNeeded(PhoneLocked):
    """iOS shows "Enter iPhone Passcode for "XCTest" / Enable UI Automation" and starts no UI test until a person
    types the passcode. It asks on a first run, and again now and then unless Settings > Developer > Enable UI
    Automation is on (2026-11-10: it came up at midday on a phone that had run tests since the morning, and
    all the daemon said was "reconnecting"). The phone isn't locked, but the cure is the same: its owner."""

    def __init__(self):
        super().__init__(f"the phone is {PASSCODE_WORDS} to allow UI automation: enter it on the phone "
                         "(Settings > Developer > Enable UI Automation stops it asking)")


class NotUnlockedSinceRestart(PhoneLocked):
    """The phone restarted and nobody has unlocked it since (CoreDevice's lock state: unlockedSinceBoot false). UI
    automation can't start before the first unlock, and its first start after a restart asks for the passcode too, so
    nothing is launched: a launch now would only put iOS's XCTest prompt over a lock screen (plug and forget, W185)."""

    def __init__(self):
        super().__init__("the phone restarted and hasn't been unlocked since: unlock it once with its passcode, and "
                         "enter the passcode again if it then asks to allow UI automation (once after each restart)")


class LockedBeforeStart(PhoneLocked):
    """The phone is locked and iOS's consent to UI automation is old enough that a new start would ask for the
    passcode (GRANT_HORIZON): nothing is launched until a person unlocks it, so the prompt comes up in front of them,
    not over a lock screen nobody is looking at. Only with TAPWRIGHT_LOCK_GATE=1 until proven on the phone (W185)."""

    def __init__(self):
        super().__init__("the phone is locked, and starting UI automation now would ask for its passcode: unlock the "
                         "phone and it starts by itself (then enter the passcode once if it asks)")


@dataclasses.dataclass(frozen=True)
class LockState:
    """CoreDevice's answer to getlockstate (`devicectl device info lockState`), read without WebDriverAgent."""
    passcode_required: bool  # the phone asks for its passcode to be unlocked now (to be proven: W185 design, P2)
    unlocked_since_boot: bool  # False: restarted and not unlocked since (BFU)

    @classmethod
    def of(cls, answer: object) -> "LockState | None":
        """Only the known shape, both fields booleans; anything else is not known (None), never a guess."""
        if not isinstance(answer, dict):
            return None
        required, since = answer.get("passcodeRequired"), answer.get("unlockedSinceBoot")
        if not isinstance(required, bool) or not isinstance(since, bool):
            return None
        return cls(required, since)


# iOS's consent to UI automation (the XCTest passcode prompt) is asked for only when a test runner starts, never while
# one runs: in serve.log (2026-11-13 to 11-19) a runner kept running served actions 10.2 h after the consent, and the
# next start asked; starts up to 7.7 h after a consent never asked, and from 8.1 h on they did (one start at 21.9 h
# didn't: another tool's prompt may have been answered in between). Typing the passcode on the lock screen doesn't
# renew it (an unlock at 11:23 on 11-19, a prompt at 14:00). So a consent older than this is taken as spent.
GRANT_HORIZON = 7.5 * 3600
GRANT_FILE: Path | None = None  # when iOS last gave its consent; None: ~/.tapwright/automation.json (tests set their own)
LOCK_STATE_WAIT = 5.0  # seconds the lock state may take to come back; then it is not known
REUSE_ASKS = 3  # times a running runner's /status is asked before it is replaced (each wda_alive waits up to 3 s) ...
REUSE_GAP = 1.0  # ... this many seconds apart
ANSWERED_FRESH = 1.0  # seconds a runner's answer to _still_answers stands for the start right after it


def _grant_file() -> Path:
    return GRANT_FILE if GRANT_FILE is not None else store.HOME / "automation.json"


def read_grant() -> float | None:
    """When (epoch seconds) a start last went on after iOS's XCTest passcode prompt was answered; None: not known."""
    try:
        value = json.loads(_grant_file().read_text()).get("granted_at")
    except (OSError, ValueError, AttributeError):
        return None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def record_grant(at: float | None = None) -> None:
    path = _grant_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"granted_at": time.time() if at is None else at}))
        os.replace(tmp, path)
    except OSError:
        pass  # (only a hint for the next start: never a reason for this one to fail)


def grant_stale(now: float | None = None) -> bool | None:
    """Would a new runner start probably ask for the passcode? None: no consent on record."""
    granted = read_grant()
    if granted is None:
        return None
    return (time.time() if now is None else now) - granted > GRANT_HORIZON


def lock_gate_on() -> bool:
    return os.environ.get("TAPWRIGHT_LOCK_GATE") == "1"


def restart_gate_on() -> bool:
    """The restart check (NotUnlockedSinceRestart): on unless TAPWRIGHT_RESTART_GATE=0 (review R191 M3: the way out if
    a phone reports its lock state otherwise than the phone check P2 found)."""
    return os.environ.get("TAPWRIGHT_RESTART_GATE") != "0"


class AppNotAnswering(ConnectionLost):
    """WebDriverAgent runs, but the app in front never answers it, so every read of the screen hangs behind
    that app (2026-11-10: a development build froze mid-reload and stayed in front for hours)."""

    def __init__(self, bundle_id: str, name: str):
        super().__init__(f"{name} ({bundle_id}) is in front and isn't answering, so nothing on the phone can be read")
        self.bundle_id, self.name = bundle_id, name


class PhoneNotFound(DeviceError):
    """No phone on USB, and none paired on the local network."""


class NotSetUp(DeviceError):
    """WebDriverAgent isn't set up on this phone yet (`tapwright setup`)."""


def run(fn, *args):
    """Run an async function on the event loop pymobiledevice3 needs. On Windows that is a
    selector loop: the default (proactor) loop breaks some of its connections (its issue
    #1217) and never receives mDNS replies, so the phone isn't found on Wi-Fi (measured)."""
    import anyio
    options = {"loop_factory": asyncio.SelectorEventLoop} if sys.platform == "win32" else None
    return anyio.run(fn, *args, backend_options=options)


def _failure_listener(on_failure):
    """A pymobiledevice3 listener that calls `on_failure(error)` when the test service says UI
    testing couldn't start, or None if its interface has moved (the wait for /status still catches it)."""
    try:
        from pymobiledevice3.services.dvt.testmanaged.dtx_services import XCUITestListener
    except ImportError:
        return None

    class Listener(XCUITestListener):
        async def initialization_for_ui_testing_did_fail(self, error) -> None:
            on_failure(error)

        async def did_fail_to_bootstrap(self, error) -> None:
            on_failure(error)
    return Listener()


def _takes_factory(userspace_tunnel) -> bool:
    """pymobiledevice3 11.57 and later: UserspaceRsdTunnel takes a `provider_factory` returning a TunnelProvider
    (`.service`, `.lockdown`). Before 11.57 it always calls the module's `_create_no_root_tunnel_provider`,
    which returns a `(service, lockdown)` tuple."""
    import inspect
    if not hasattr(userspace_tunnel, "TunnelProvider"):
        return False
    try:
        return "provider_factory" in inspect.signature(userspace_tunnel.UserspaceRsdTunnel).parameters
    except (TypeError, ValueError):
        return False


def _new_tunnel(userspace_tunnel, serial, make):
    """The tunnel to the phone. `make` (async, no arguments) gives the tunnel service of a phone reached over a
    network; None leaves the choice to the library (USB). On 11.57+ `make` goes in as the provider factory, so
    nothing outside this tunnel changes."""
    if make is None or not _takes_factory(userspace_tunnel):
        return userspace_tunnel.UserspaceRsdTunnel(serial=serial)

    async def factory():
        return userspace_tunnel.TunnelProvider(await make(), None)
    return userspace_tunnel.UserspaceRsdTunnel(serial=serial, provider_factory=factory)


_swap_locks: "dict[int, tuple[weakref.ref, asyncio.Lock]]" = {}


def _swap_lock() -> asyncio.Lock:
    """One lock per event loop, made inside the loop that uses it. Keyed by the loop's id with only a weak reference
    to the loop: a lock that once had to wait holds its loop, so a loop-keyed weak dictionary would never let either
    go (review 1.9.1, Opus 2 L1). Such a lock still holds its loop itself, so the reference stays alive: entries of
    closed loops are dropped here too, which leaves at most the last closed loop's until the next open. A dead
    reference, or another loop, under an id means the id was reused."""
    loop = asyncio.get_running_loop()
    entry = _swap_locks.get(id(loop))
    if entry is not None and entry[0]() is loop:
        return entry[1]
    for key, (ref, _) in list(_swap_locks.items()):
        held = ref()
        if held is None or held.is_closed():
            del _swap_locks[key]
    lock = asyncio.Lock()
    _swap_locks[id(loop)] = (weakref.ref(loop), lock)
    return lock


async def _open_tunnel(userspace_tunnel, tunnel, make):
    """Open `tunnel`. Before 11.57 the tunnel always calls the module's provider, so a network service can only be
    handed in by replacing it: for this one open, undone after it, so a later open over USB gets the library's own
    provider again (1.9.0 left it replaced). The swap, the open and the undo are one step under a lock: a second
    open meanwhile would take this open's provider for its original, or have its own undone before it runs. A USB
    open takes the lock too: the tunnel reads the module's provider only once it holds its own lifecycle lock, so a
    USB open queued there (behind a closing tunnel) would otherwise read a network open's swapped-in provider
    (CodeRabbit, PR #24)."""
    if _takes_factory(userspace_tunnel):
        return await tunnel.aopen()
    if make is None:
        async with _swap_lock():
            return await tunnel.aopen()

    async def provider(serial, autopair, remotepairing_fallback=True):
        return await make(), None
    async with _swap_lock():
        original = userspace_tunnel._create_no_root_tunnel_provider
        userspace_tunnel._create_no_root_tunnel_provider = provider
        try:
            return await tunnel.aopen()
        finally:
            if userspace_tunnel._create_no_root_tunnel_provider is provider:  # never undo someone else's
                userspace_tunnel._create_no_root_tunnel_provider = original


class _Direct:
    """A port on the phone reached straight through usbmuxd (no tunnel), with the methods a tunnel connection has:
    sendall, recv_any, recvall, close. Measured on the test phone: /status 1 ms on a kept connection, a PNG 243 ms
    (about 400 ms through the tunnel), the MJPEG stream 22 fps."""

    def __init__(self, sock) -> None:
        sock.setblocking(False)
        try:  # usbmuxd is a TCP socket on Windows: small requests mustn't wait for Nagle's timer
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:  # a unix socket (macOS, Linux)
            pass
        self.sock = sock

    async def sendall(self, data: bytes) -> None:
        await asyncio.get_running_loop().sock_sendall(self.sock, data)

    async def recv_any(self, n: int = 65536) -> bytes:
        return await asyncio.get_running_loop().sock_recv(self.sock, n)

    async def recvall(self, n: int) -> bytes:
        """Exactly n bytes, or fewer when the phone closes the connection first."""
        data = b""
        while len(data) < n:
            chunk = await self.recv_any(n - len(data))
            if not chunk:
                break
            data += chunk
        return data

    async def close(self) -> None:
        self.sock.close()


class Device:
    # class-level defaults: a Device built without __init__ (tests do) reaches the runner through the tunnel
    via: str | None = None
    _mux = None
    _route: tuple | None = None
    _direct_off: tuple | None = None

    def __init__(self, udid: str | None = None, host: str | None = None, port: int | None = None, log=print):
        state = store.load_state()
        self.udid = udid or state.get("udid")
        self.host, self.port = host, port or (state.get("pairing_port") if host else None)
        self.log = log
        self.tunnel = None
        self.rsd = None
        self.via = None
        self._network_hold: "NetworkHold | None" = None  # keeps a network condition on (see set_network)
        self._network_holds: "set[NetworkHold]" = set()  # every hold with a live task, the current one included
        self._network_lock = asyncio.Lock()  # one set or clear at a time: two calls must not leak a hold
        # called with the reason when a hold ends by itself or with the tunnel (not when the daemon asked to clear)
        self.on_network_end = None  # Callable[[str], None] | None
        # One connection kept open to WebDriverAgent (see _on_kept). On over USB, where the reliability suite
        # passed with it (2026-11-11); off over a network until it has been measured there. TAPWRIGHT_KEEP=1 or
        # 0 overrides.
        forced = os.environ.get("TAPWRIGHT_KEEP")
        self._keep_forced = None if forced not in ("0", "1") else forced == "1"
        self._kept: tuple | None = None
        self._kept_lock = asyncio.Lock()
        # the runner's ports over USB go straight through usbmuxd (see _dial)
        self._mux = None  # the usbmux entry of the cabled phone; looked up again after any failure (a replug changes its id)
        self._route: tuple | None = None  # (tunnel, "direct" | "tunnel"): what _dial last chose, logged when it changes
        self._direct_off: tuple | None = None  # (tunnel, when): a direct dial failed; the tunnel carries it until then + DIRECT_RETRY
        self.runner = None  # the XCUITest task that keeps WebDriverAgent alive
        self.awaiting_passcode = False  # the last start stopped at iOS's passcode screen (see PasscodeNeeded)
        self._launched_at = 0.0  # when the test runner was last started (monotonic clock)
        self._prompt_rounds = 0  # starts in a row that stopped at the passcode screen
        self._gone_at: float | None = None  # when that screen was first seen gone, for the pause before the next start
        self._hurry = False  # see hurry()
        self._answered_at: float | None = None  # when the running runner last answered _still_answers (monotonic)

    # ---- the tunnel ----
    async def connect(self) -> None:
        # a new tunnel is a new phone session: the old condition went with the old connection
        self.drop_network("the phone's connection was rebuilt")
        await self._drop_kept()  # the runner may have restarted with the tunnel; a new phone session starts clean
        self._mux = self._direct_off = None
        from pymobiledevice3 import usbmux
        from pymobiledevice3.remote import userspace_tunnel
        on_usb = [d for d in await usbmux.list_devices() if d.connection_type == "USB"
                  and (self.udid is None or d.serial == self.udid)]
        unclaimed: list = []  # services found here that no tunnel has taken and nobody has closed yet
        extras: list = []
        if self.host:
            if not self.port:
                raise DeviceError("no pairing port known for a direct address: run `tapwright probe` once on the "
                                  "same Wi-Fi as the phone (it learns the port), or pass --port")
            identifier = self._pairing_identifier()
            host, port = self.host, self.port

            async def make():
                from pymobiledevice3.remote.tunnel_service import create_core_device_tunnel_service_using_remotepairing
                return await create_core_device_tunnel_service_using_remotepairing(identifier, host, port)
            # pymobiledevice3 picks USB or mDNS; to dial a given address, it is handed this service instead.
            self.via = f"network {host}:{port}"
        elif on_usb:
            self.udid = self.udid or on_usb[0].serial
            make = None  # the library's own choice: USB first
            self.via = "USB"
        else:
            from pymobiledevice3.remote.tunnel_service import get_remote_pairing_tunnel_services
            services = await get_remote_pairing_tunnel_services(udid=self.udid)
            if not services:
                raise PhoneNotFound("no phone on USB, and none paired on this network (same Wi-Fi, unlocked, "
                                    "Developer Mode on, `tapwright setup` done over USB once)")
            found, extras = services[0], services[1:]
            unclaimed.extend(services)  # every one is ours to close until the tunnel takes `found`

            async def make():
                unclaimed.clear()  # the tunnel owns it from here, and closes it with itself
                return found
            self.via = "local network"
        self.tunnel = _new_tunnel(userspace_tunnel, self.udid, make)
        try:
            # other paired phones that answered: closed one by one, so a failing close leaves none of the rest open
            # (review 1.9.1, Opus 2 L3)
            for extra in extras:
                unclaimed.remove(extra)
                try:
                    await extra.close()
                except Exception as closing:
                    self.log(f"closing another phone's network service: {closing}")
            self.rsd = await _open_tunnel(userspace_tunnel, self.tunnel, make)
        except BaseException as e:
            # an open that failed before taking the found service (a tunnel still active, say) leaves it to us
            for service in unclaimed:
                try:
                    await service.close()
                except Exception as closing:  # the open's own failure is the one to report
                    self.log(f"closing the unused network service: {closing}")
                # a cancellation while closing (CodeRabbit, PR #24) is not caught: it goes up in place of the failure
            if isinstance(e, Exception):
                raise DeviceError(f"couldn't open the developer tunnel over {self.via}: {e}") from e
            raise
        self.udid = self.rsd.udid
        store.save_state(udid=self.udid)
        self.log(f"connected over {self.via}: {self.rsd.product_type} iOS {self.rsd.product_version}")

    def _pairing_identifier(self) -> str:
        from pymobiledevice3.pair_records import iter_remote_pair_records_by_identifier
        ids = [i for i, _, _ in iter_remote_pair_records_by_identifier()]
        if self.udid in ids:
            return self.udid
        if len(ids) == 1:
            return ids[0]
        raise DeviceError("no single network pairing record for this phone: run `tapwright setup` over USB")

    async def mount(self) -> None:
        """Apple's developer disk image: needed for UI automation, mounted until the phone restarts.
        It adds the test services, which the phone lists only on a new connection."""
        from pymobiledevice3.services.mobile_image_mounter import auto_mount
        if TEST_SERVICE in self.rsd.peer_info["Services"]:
            return
        try:
            await auto_mount(self.rsd)
        except Exception as e:  # already mounted is the common case
            if "already" not in str(e).lower():
                self.log(f"developer disk image: {e}")
        await self.tunnel.aclose()
        await self.connect()

    # ---- WebDriverAgent ----
    async def start_wda(self, runner_id: str, wait: float = 60.0) -> None:
        """Start our own WebDriverAgent. One left by a process that didn't shut down cleanly
        answers for about 15 s and then hangs (measured), so it's stopped, not reused."""
        from pymobiledevice3.services.dvt.instruments.device_info import DeviceInfo
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        from pymobiledevice3.services.dvt.instruments.process_control import ProcessControl
        from pymobiledevice3.services.dvt.testmanaged.xcuitest import TestConfig, XCUITestService
        if self.runner and not self.runner.done() and (self._answered_recently() or await self._still_answers()):
            if self.awaiting_passcode or self._prompt_rounds:
                record_grant()  # the start that waited at the prompt went on: the passcode was entered just now
            self.awaiting_passcode, self._prompt_rounds = False, 0
            return
        up = await self.passcode_prompt_up() if self.awaiting_passcode else False
        if self.awaiting_passcode and up is False and not self._hurry:
            # the screen is gone (iOS failed the start, or Cancel was tapped) and nobody is asking for the phone:
            # the next start waits its turn (PROMPT_PAUSES)
            now = time.monotonic()
            self._gone_at = now if self._gone_at is None else self._gone_at
            if now - self._gone_at < PROMPT_PAUSES[min(max(self._prompt_rounds, 1), len(PROMPT_PAUSES)) - 1]:
                raise PasscodeNeeded()
        self._hurry = False
        if self.awaiting_passcode and time.monotonic() - self._launched_at < PROMPT_RETRY and up is not False:
            # Still up (or the phone couldn't say): start nothing. A new start takes the screen away and puts a
            # new one up, under the person's fingers; seen on the phone as the app behind it coming to the front
            # and going again with every retry. The screen stays up without a runner (measured), so this waits,
            # still, until the passcode is in or Cancel is tapped; the start after that finds out which. (And
            # every PROMPT_RETRY seconds one start is tried regardless: the process list is the only witness.)
            raise PasscodeNeeded()
        self.awaiting_passcode = False
        held = await self._lock_gate()
        if held is not None:
            raise held  # nothing launched: the runner already running (if any) is left as it is
        await self._stop_runner()  # iOS may have killed it before its task noticed (measured)
        async with DvtProvider(self.rsd) as dvt, DeviceInfo(dvt) as info, ProcessControl(dvt) as control:
            for p in await info.proclist():
                if p.get("bundleIdentifier") == runner_id:
                    await control.kill(p["pid"])
        cfg = await TestConfig.create_for(self.rsd, runner_bundle_id=runner_id)
        failed = asyncio.Event()
        reason: list[str] = []

        def on_failure(error) -> None:
            reason.append(f"{getattr(error, 'domain', '?')} code {getattr(error, 'code', '?')}")
            failed.set()
        service = XCUITestService(self.rsd)
        listener = _failure_listener(on_failure)
        try:
            run = service.run(cfg, listener=listener) if listener else service.run(cfg)
        except TypeError:  # run() no longer takes a listener: start without the hook
            run = service.run(cfg)
        self.runner = asyncio.create_task(run, name="webdriveragent")
        began = self._launched_at = time.monotonic()
        self._gone_at = None
        deadline, ask_at = began + wait, began + PROMPT_AFTER
        while time.monotonic() < deadline:
            if failed.is_set():  # the phone said so: don't wait out the minute
                await self._stop_runner()
                raise await self._or_passcode(DeviceError(f"UI testing failed to start: {reason[0]}"))
            if self.runner.done():
                exc = self.runner.exception()
                raise await self._or_passcode(DeviceError(f"WebDriverAgent stopped while starting: {exc}"))
            if await self.wda_alive():
                self.log("WebDriverAgent is running")
                if self._prompt_rounds:
                    record_grant()  # a start after the prompt went through: the consent is new
                self._prompt_rounds = 0
                return
            if time.monotonic() >= ask_at:
                # slower than any start: is iOS waiting for the passcode? The runner is left as it is, so the
                # start goes on by itself once the passcode is in
                ask_at = time.monotonic() + PROMPT_EVERY
                if await self.passcode_prompt_up():
                    self.awaiting_passcode = True
                    self._prompt_rounds += 1
                    raise PasscodeNeeded()
            try:
                await asyncio.wait_for(failed.wait(), 0.5)
            except asyncio.TimeoutError:
                pass
        raise await self._or_passcode(
            DeviceError("WebDriverAgent didn't answer within a minute (is it installed? `tapwright doctor`)"))

    def runner_alive(self) -> bool:
        """Is the task that keeps this process's test runner alive still running (its test session not ended)?"""
        return self.runner is not None and not self.runner.done()

    async def _still_answers(self) -> bool:
        """(1.10.0) Does the running runner's WebDriverAgent answer /status? Asked up to REUSE_ASKS times, REUSE_GAP apart:
        a runner busy with one long request (an audit's read of a big screen) can miss one /status, and replacing it costs
        iOS's passcode prompt once its consent is about 8 h old. A runner whose task ended isn't asked (see runner_alive)."""
        for ask in range(REUSE_ASKS):
            if ask:
                await asyncio.sleep(REUSE_GAP)
                if not self.runner_alive():
                    return False
            if await self.wda_alive():
                self._answered_at = time.monotonic()
                return True
        return False

    def _answered_recently(self) -> bool:
        """Did the running runner answer _still_answers within ANSWERED_FRESH? Then a start doesn't ask again: one probe
        chain per reuse (the daemon's _session_again_on_runner has just asked; review R192 L3). Spent once used."""
        at, self._answered_at = self._answered_at, None
        return at is not None and time.monotonic() - at < ANSWERED_FRESH

    @_traced
    async def lock_state(self) -> LockState | None:
        """The phone's lock state from CoreDevice (iOS 17+), without WebDriverAgent: answers before any runner is
        launched. None when the phone couldn't say (an older iOS, no tunnel, another shape of answer)."""
        if self.rsd is None:
            return None
        rsd = self.rsd

        async def read() -> object:
            from pymobiledevice3.remote.core_device.device_info import DeviceInfoService
            async with DeviceInfoService(rsd) as service:
                return await service.get_lockstate()
        try:
            # the whole read, the service's connect included, under the cap (review R191 M2: a phone whose services
            # stall hung the connect, and with it the reconnect that holds the daemon's exclusive control)
            answer = await asyncio.wait_for(read(), LOCK_STATE_WAIT)
        except Exception:
            return None
        return LockState.of(answer)

    async def _lock_gate(self) -> "PhoneLocked | None":
        """Why no runner may be launched now, or None. Not after a restart before the first unlock, when the phone also
        says its passcode is needed (a phone without a passcode reads "not unlocked since boot" until its first swipe:
        review R191 M3), unless TAPWRIGHT_RESTART_GATE=0; with TAPWRIGHT_LOCK_GATE=1, also not on a locked phone whose
        consent to UI automation is spent or not on record (a launch would put the passcode prompt over the lock
        screen). A phone that can't say is never held."""
        state = await self.lock_state()
        if state is None:
            return None
        if not state.unlocked_since_boot and state.passcode_required and restart_gate_on():
            return NotUnlockedSinceRestart()
        if state.passcode_required and lock_gate_on() and grant_stale() is not False:
            return LockedBeforeStart()
        return None

    @_traced
    async def passcode_prompt_up(self) -> bool | None:
        """Is iOS's passcode screen in front (from the process list, which answers without WebDriverAgent)?
        None when the phone couldn't be asked: not known, which is not the same as gone."""
        from pymobiledevice3.services.dvt.instruments.device_info import DeviceInfo
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        try:
            async with DvtProvider(self.rsd) as dvt, DeviceInfo(dvt) as info:
                procs = await asyncio.wait_for(info.proclist(), 10)
        except Exception:
            return None
        return any(p.get("foregroundRunning") and p.get("name") == AUTH_PROMPT for p in procs)

    async def _or_passcode(self, error: DeviceError) -> DeviceError:
        """The reason a start failed: the passcode screen when it is up, else `error`."""
        if await self.passcode_prompt_up():
            self.awaiting_passcode = True
            self._prompt_rounds += 1
            return PasscodeNeeded()
        return error

    @_traced
    async def battery(self) -> tuple[int | None, bool | None]:
        """(percent, charging) from lockdown's battery domain, for the watchdog (§24.5); (None, None) when the phone
        doesn't say. Asks over the open tunnel only: never connects."""
        if self.rsd is None:
            return None, None
        try:
            values = await self.rsd.get_value("com.apple.mobile.battery")
        except Exception:
            return None, None
        if not isinstance(values, dict):
            return None, None
        level = values.get("BatteryCurrentCapacity")
        charging = values.get("BatteryIsCharging")
        percent = level if isinstance(level, int) and not isinstance(level, bool) and 0 <= level <= 100 else None
        return percent, charging if isinstance(charging, bool) else None

    def hurry(self) -> None:
        """Someone is asking for the phone now (a tool call, a viewer): the next start doesn't wait its turn."""
        self._hurry = True

    @_traced
    async def foreground_apps(self) -> list[str]:
        """Bundle ids of the apps iOS has in the foreground, from its process list: that answers while
        WebDriverAgent is stuck behind a frozen app. SpringBoard isn't an application there (measured)."""
        from pymobiledevice3.services.dvt.instruments.device_info import DeviceInfo
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        async with DvtProvider(self.rsd) as dvt, DeviceInfo(dvt) as info:
            procs = await info.proclist()
        # iOS calls its passcode screen an application too: it is never "the app in front" (closing it as a
        # frozen app would take the screen away from the person typing)
        return [p["bundleIdentifier"] for p in procs
                if p.get("foregroundRunning") and p.get("isApplication") and p.get("bundleIdentifier")
                and p.get("name") != AUTH_PROMPT and not str(p["bundleIdentifier"]).startswith(WIDGET_HOSTS)]

    @_traced
    async def kill_app(self, bundle_id: str) -> int:
        """End every process of that app, as swiping it away in the app switcher does; how many there were.
        Works while the app is frozen, unlike WebDriverAgent's terminate, which waits on it."""
        from pymobiledevice3.services.dvt.instruments.device_info import DeviceInfo
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        from pymobiledevice3.services.dvt.instruments.process_control import ProcessControl
        async with DvtProvider(self.rsd) as dvt, DeviceInfo(dvt) as info, ProcessControl(dvt) as control:
            pids = [p["pid"] for p in await info.proclist() if p.get("bundleIdentifier") == bundle_id]
            for pid in pids:
                await check_before_send()
                await control.kill(pid)
        return len(pids)

    # ---- diagnostics (AGENT-TOOLS §15) ----
    @_traced
    async def processes(self) -> list["Proc"]:
        """Every process iOS lists: its pid, its bundle id and when it started (a pid is reused, so a pid alone says
        nothing about which process it is)."""
        from pymobiledevice3.services.dvt.instruments.device_info import DeviceInfo
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        async with DvtProvider(self.rsd) as dvt, DeviceInfo(dvt) as info:
            raw = await info.proclist()
        return [Proc(p["pid"], _started_of(p), p.get("bundleIdentifier") or None)
                for p in raw if isinstance(p.get("pid"), int)]

    async def app_processes(self, bundle_id: str) -> list["Proc"]:
        """That app's running processes, each with its start time (empty: it isn't running)."""
        return [p for p in await self.processes() if p.bundle == bundle_id]

    async def processes_of(self, bundle_ids: list[str]) -> dict[str, list["Proc"]]:
        """(2.0.3) The running processes of each of those apps, from one read of iOS's process list: the daemon's log
        watchers share it (one question for all of them, where each asked the whole list for its own app)."""
        out: dict[str, list[Proc]] = {b: [] for b in bundle_ids}
        for p in await self.processes():
            if p.bundle in out:
                out[p.bundle].append(p)
        return out

    @_traced
    async def phone_time_ms(self) -> int | None:
        """The phone's own clock now, epoch ms: the log entries' times are on it, not on this computer's."""
        value = await self.rsd.get_value(key="TimeIntervalSince1970")
        return int(float(value) * 1000) if value is not None else None

    async def process_holder(self, pid: int) -> "Proc | None":
        """Whatever process has that pid now, whoever's it is (None: nothing does)."""
        return next((p for p in await self.processes() if p.pid == pid), None)

    async def app_pids(self, bundle_id: str) -> list[int]:
        """The pids of that app's running processes, from iOS's process list (empty: it isn't running)."""
        return [p.pid for p in await self.app_processes(bundle_id)]

    async def app_log_stream(self, pid: int):
        """The unified log entries of one process, as they come: (epoch ms, os_trace level byte, text, pid, own). The
        stream is asked for that pid only, and an entry of any other pid is dropped here as well. `own`: the line was
        logged by code of the app's own bundle (`own_line`), not by a system framework loaded into it (XCTest's
        accessibility server, WebDriverAgent's queries, preferences, power logging)."""
        from pymobiledevice3.services.os_trace import OsTraceService
        service = OsTraceService(lockdown=self.rsd)
        try:
            async for entry in service.syslog(pid=pid):
                if entry.pid == pid:
                    own = own_line(getattr(entry, "image_name", ""), getattr(entry, "filename", ""))
                    yield int(entry.timestamp.timestamp() * 1000), int(entry.level), entry.message, entry.pid, own
        finally:
            await self._close(service)

    @_traced
    async def crash_reports(self, executable: str, count: int, bundle_id: str | None = None) -> CrashSearch:
        """The newest `count` crash reports of the app (parsed; newest first): its process is named `executable`
        and, when a report names one, its bundle id is `bundle_id`. Each report is read on its own: one that can't be
        parsed (half written, as the app has just died) is counted in `unreadable` and passed over, never taken for
        "no crash". Errors of the phone's file service are not caught here."""
        from pycrashreport.crash_report import get_crash_report_from_buf
        from pymobiledevice3.services.crash_reports import CrashReportsManager
        pattern = re.compile(crash_pattern(executable))
        found: list = []
        unreadable = too_big = scanned = 0
        incomplete = False
        async with CrashReportsManager(self.rsd) as manager:
            candidates = []
            async for entry in manager.afc.dirlist("/", depth=1):
                if entry == "/" or not pattern.search(posixpath.basename(entry)):
                    continue
                stat = await manager.afc.stat(entry)
                if stat.get("st_ifmt") == "S_IFREG":
                    candidates.append((stat["st_mtime"].timestamp(), entry, _size_of(stat)))
            # Two installed apps can share an executable name, so the count is of this app's own reports: the search
            # goes on past other apps' files, up to CRASH_SCAN files, and says so when that was not enough
            ordered = sorted(candidates, reverse=True)
            for _, path, size in ordered[:CRASH_SCAN]:
                if len(found) >= count:
                    break
                scanned += 1
                data = None
                try:
                    data = await _read_capped(manager.afc, path, size)
                    report = get_crash_report_from_buf(data.decode(), filename=path)
                except CrashTooBig:
                    # skipped counts are of this app's reports only: another app's file is not mentioned, so the
                    # file's own header has to name the app (a report that can't be identified stays silent)
                    if bundle_id and diagnostics.names_app(await _head_of(manager.afc, path), bundle_id):
                        too_big += 1
                    continue
                except (ValueError, KeyError, TypeError, AttributeError, IndexError):  # not a readable report
                    if bundle_id and data is not None and diagnostics.names_app(data[:CRASH_HEAD], bundle_id):
                        unreadable += 1
                    continue
                found += only_mine([report], executable, bundle_id)
            incomplete = len(found) < count and len(ordered) > CRASH_SCAN
        return CrashSearch(found, unreadable, too_big, incomplete, scanned)

    @_traced
    async def crash_file(self, filename: str) -> bytes:
        """The whole of one crash report, as it is on the phone (CrashTooBig when it is over the byte cap)."""
        from pymobiledevice3.services.crash_reports import CrashReportsManager
        async with CrashReportsManager(self.rsd) as manager:
            stat = await manager.afc.stat(filename)
            return await _read_capped(manager.afc, filename, _size_of(stat))

    # ---- staging videos for `media` (1.7.9) ----
    async def _container(self, bundle_id: str):
        """AFC rooted at the app's own container (house_arrest VendContainer). iOS vends it for an app signed with
        get-task-allow, as Tapwright signs the runner (signing.py); the words of a refusal say what to do."""
        from pymobiledevice3.exceptions import PyMobileDevice3Exception
        from pymobiledevice3.services.house_arrest import HouseArrestService
        try:
            return await HouseArrestService.create(self.rsd, bundle_id)
        except (PyMobileDevice3Exception, OSError) as e:
            if _line_dropped(e):
                raise ConnectionLost(f"the line to the phone dropped while opening the runner's container ({e})") from None
            raise DeviceError(f"the phone wouldn't open the runner's container to copy the video into it ({e}); run "
                              "`tapwright setup --force` to install the runner again, then try again") from None

    async def stage_media(self, bundle_id: str, folder: str, files: list[tuple[Path, str]],
                          stamps: "list | None" = None) -> None:
        """Copy each local file to its path (relative to the app's container) in a new `folder` there, in chunks: a
        clip is never held in memory whole. `stamps` (media.Stamp, one per file, or None): what each file was when it
        was checked and hashed; a file that changed since (it grew, or another file was put in its place) is refused,
        and only its checked size is ever copied (review R179 L5)."""
        from pymobiledevice3.exceptions import PyMobileDevice3Exception
        from .media import Stamp
        afc = await self._container(bundle_id)
        async with afc:
            try:
                await afc.makedirs(folder)
                for n, (local, remote) in enumerate(files):
                    stamp = stamps[n] if stamps is not None and n < len(stamps) else None
                    with open(local, "rb") as f:
                        if stamp is not None and Stamp.of(os.fstat(f.fileno())) != stamp:
                            raise DeviceError(_CHANGED.format(name=Path(local).name))
                        left = stamp.size if stamp is not None else None
                        handle = await afc.fopen(remote, "w")
                        try:
                            while left is None or left > 0:
                                want = STAGE_CHUNK if left is None else min(STAGE_CHUNK, left)
                                chunk = await asyncio.to_thread(f.read, want)
                                if not chunk:
                                    break
                                await afc.fwrite(handle, chunk)
                                if left is not None:
                                    left -= len(chunk)
                        finally:
                            await _close_file(afc, handle)
                        if stamp is not None and (left or Stamp.of(os.fstat(f.fileno())) != stamp):
                            raise DeviceError(_CHANGED.format(name=Path(local).name))
            except PyMobileDevice3Exception as e:  # (before OSError: AFC's own errors are both)
                if _line_dropped(e):  # USB pulled, Wi-Fi gone: a drop, not a phone that can't take videos (R179b L6)
                    raise ConnectionLost(f"the line to the phone dropped while copying the video ({e})") from None
                raise DeviceError(f"copying the video to the phone failed ({e})") from None
            except (ConnectionError, EOFError, TimeoutError) as e:  # the AFC socket (a local file read raises none)
                raise ConnectionLost(f"the line to the phone dropped while copying the video ({e})") from None
            except OSError as e:
                raise DeviceError(f"the video couldn't be read on this computer ({e.strerror or e})") from None

    async def unstage_media(self, bundle_id: str, folder: str) -> None:
        """Remove a staged folder and what is left in it (the library moved what it added)."""
        from pymobiledevice3.exceptions import PyMobileDevice3Exception
        afc = await self._container(bundle_id)
        async with afc:
            try:
                await afc.rm(folder, force=True)
            except (PyMobileDevice3Exception, OSError) as e:
                raise DeviceError(f"the staged video couldn't be removed ({e})") from None

    async def sweep_media(self, bundle_id: str, older_than: float) -> None:
        """Remove staged folders older than `older_than` seconds: leftovers of adds whose end wasn't known."""
        from pymobiledevice3.exceptions import PyMobileDevice3Exception
        from .media import STAGE_ROOT
        afc = await self._container(bundle_id)
        async with afc:
            try:
                if not await afc.exists(STAGE_ROOT):
                    return
                now = time.time()
                for name in await afc.listdir(STAGE_ROOT):
                    path = f"{STAGE_ROOT}/{name}"
                    stat = await afc.stat(path)
                    made = stat.get("st_mtime")
                    if made is not None and now - made.timestamp() > older_than:
                        await afc.rm(path, force=True)
            except (PyMobileDevice3Exception, OSError) as e:
                raise DeviceError(f"old staged videos couldn't be listed ({e})") from None

    # ---- network conditions (AGENT-TOOLS §16.4) ----
    async def set_network(self, mode: str) -> str:
        """Put the phone's network in `mode` (offline, 3g or lossy; network.py) and keep it there: Apple's condition
        holds only while the instruments connection that set it stays open, so a task here keeps that connection
        until clear_network, a reconnect or close. The identifier of the profile used. DeviceError when the phone
        can't."""
        async with self._network_lock:
            replaced = bool(self._network_holds)
            await self._stop_holds()
            hold = NetworkHold(self, mode)
            self._network_hold = hold
            self._network_holds.add(hold)
            try:
                return await hold.start()
            except BaseException:
                if self._network_hold is hold:
                    self._network_hold = None
                self._network_holds.discard(hold)
                if replaced:  # the condition that was on is gone with nothing in its place: the daemon is told
                    self._network_ended(f"{mode} could not replace it, so the phone is back to normal")
                raise

    async def clear_leftover(self) -> None:
        """Ask the phone to end a condition that an earlier process left (the daemon was killed or crashed, and its
        marker file says so). A condition ends with the connection that holds it, so this is a safety net: a new
        instruments connection says `clear`, whoever set the condition. Raises when the phone can't be asked."""
        from pymobiledevice3.services.dvt.instruments.condition_inducer import ConditionInducer
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        async with self._network_lock:
            async with DvtProvider(self.rsd) as dvt, ConditionInducer(dvt) as inducer:
                await inducer.clear()

    async def clear_network(self) -> None:
        """The phone's network back to normal: every connection that holds a condition is closed. Never raises."""
        async with self._network_lock:
            await self._stop_holds()

    async def _stop_holds(self) -> None:
        holds, self._network_hold = list(self._network_holds), None
        self._network_holds.clear()
        for hold in holds:
            await hold.stop()

    def drop_network(self, reason: str | None = None) -> None:
        """The tunnel is gone or going: its instruments connection can't be closed politely, and the phone has
        already dropped the condition with it. Stop the tasks that held it, without waiting. With a `reason`, and a
        hold that was on, the daemon is told (on_network_end)."""
        holds, self._network_hold = list(self._network_holds), None
        self._network_holds.clear()
        for hold in holds:
            hold.abandon()
        if holds and reason is not None:
            self._network_ended(reason)

    def _network_ended(self, reason: str) -> None:
        if self.on_network_end is not None:
            try:
                self.on_network_end(reason)
            except Exception as e:  # a callback must not break the connection's own work
                self.log(f"network: {e}")

    def _hold_died(self, hold: "NetworkHold", reason: str) -> None:
        """A hold's task ended without being asked to: the phone has dropped its condition with the connection."""
        if hold not in self._network_holds:
            return
        self._network_holds.discard(hold)
        if self._network_hold is hold:
            self._network_hold = None
            self._network_ended(reason)

    def meter(self, bundle_id: str) -> "AppMeter":
        """A meter for that app's own process (`performance`): start() it, do the thing, stop() it."""
        return AppMeter(self, bundle_id)

    async def wda_alive(self) -> bool:
        try:
            await self.http("GET", "/status", timeout=3)
            return True
        except Exception:
            return False

    async def http(self, method: str, path: str, payload: dict | None = None, timeout: float = 30.0) -> dict:
        """One request to WebDriverAgent: straight through usbmuxd over USB, through the tunnel otherwise (see _dial).
        Raises DeviceError for an error reply, ConnectionLost when nothing usable comes back.
        A deadline shorter than MIN_TIMEOUT is raised to it: running out of the caller's own time is
        not a dead line, and a 0 s deadline once made the daemon reconnect (2026-11-09)."""
        timeout = max(timeout, MIN_TIMEOUT)
        try:
            return await asyncio.wait_for(self._http(method, path, payload), timeout)
        except asyncio.TimeoutError:
            raise ConnectionLost(f"WebDriverAgent didn't answer {method} {path} within {timeout:.0f} s") from None

    async def _http(self, method: str, path: str, payload: dict | None) -> dict:
        body = json.dumps(payload).encode() if payload is not None else b""

        def request(keep: bool) -> bytes:
            head = [f"{method} {path} HTTP/1.1", "Host: localhost", f"Connection: {'keep-alive' if keep else 'close'}",
                    f"Content-Length: {len(body)}"] + (["Content-Type: application/json"] if payload is not None else [])
            return ("\r\n".join(head) + "\r\n\r\n").encode() + body
        kept = await self._on_kept(request(True), again=method == "GET") if self.keep else None
        if kept is not None:
            status, rest = kept
        else:
            try:
                conn = await self._dial(WDA_PORT)
            except ConnectionLost:
                raise
            except Exception as e:
                raise ConnectionLost(f"can't reach WebDriverAgent: {e}") from e
            try:
                status, rest = await self._exchange(conn, request(False))
            except (DeviceError, Refused):
                raise
            except Exception as e:  # reset, closed, or cut off mid-answer
                raise ConnectionLost(f"the connection to WebDriverAgent dropped: {e}") from e
            finally:
                await self._close(conn)
        trace.note_bytes(len(rest or b""))
        try:
            data = json.loads(rest or b"{}")
        except ValueError:
            if status >= 400:
                raise DeviceError(f"WebDriverAgent: HTTP {status}") from None
            raise ConnectionLost("WebDriverAgent sent an answer that isn't JSON") from None
        if status >= 400 or (isinstance(data.get("value"), dict) and data["value"].get("error")):
            err = data.get("value", {}) if isinstance(data.get("value"), dict) else {}
            kind = RunnerTimedOut if err.get("error") == "timeout" else DeviceError
            raise kind(f"WebDriverAgent: {err.get('error', status)}: {err.get('message', '')[:300]}")
        return data

    @property
    def keep(self) -> bool:
        return self._keep_forced if self._keep_forced is not None else self.via == "USB"

    @keep.setter
    def keep(self, value: bool) -> None:
        self._keep_forced = value

    async def _on_kept(self, request: bytes, again: bool) -> tuple[int, bytes] | None:
        """One request on the connection kept open to WebDriverAgent (it keeps one open, measured 2026-11-10: a
        small request takes 6 ms over USB against 17 ms with a new connection each time, and over Wi-Fi the phone's
        dozing radio is paid once, not once more for the handshake). None when this request should go the old way,
        on a connection of its own: another request is using the kept one, or the kept one has been idle for
        KEEP_IDLE seconds and this is an action (a connection the phone has dropped meanwhile would lose it, and an
        action can't be sent twice; a question (`again`) simply is asked on a new connection).
        Whatever goes wrong, the kept connection is closed: its state isn't known any more."""
        if self._kept_lock.locked():
            return None
        async with self._kept_lock:
            held = self._kept  # (the tunnel it belongs to, the connection, when it last answered)
            if held is not None and held[0] is not self.rsd:
                await self._close(held[1])
                held = self._kept = None
            if held is not None and not again and time.monotonic() - held[2] > KEEP_IDLE:
                return None
            reused = held is not None
            try:
                if held is None:
                    try:
                        held = (self.rsd, await self._dial(WDA_PORT), time.monotonic())
                    except ConnectionLost:
                        raise
                    except Exception as e:
                        raise ConnectionLost(f"can't reach WebDriverAgent: {e}") from e
                self._kept = None  # in use: nothing else finds it half-read
                status, rest = await self._exchange(held[1], request, keep=True)
            except BaseException as e:  # also a timeout's cancellation: the answer may be half-read
                if held is not None:
                    await asyncio.shield(self._close(held[1]))
                if isinstance(e, Refused) or (isinstance(e, DeviceError) and not isinstance(e, ConnectionLost)):
                    raise
                if reused and again and isinstance(e, Exception):
                    return None  # the phone had dropped it: ask on a connection of its own
                if isinstance(e, (ConnectionLost, asyncio.CancelledError)) or not isinstance(e, Exception):
                    raise
                raise ConnectionLost(f"the connection to WebDriverAgent dropped: {e}") from e
            self._kept = (held[0], held[1], time.monotonic())
            return status, rest

    @staticmethod
    async def _exchange(conn, request: bytes, keep: bool = False) -> tuple[int, bytes]:
        """Send one HTTP request; the status code and the body. With `keep` the answer must say how long it is
        (one read to the end of the connection would never return)."""
        if request.startswith(b"POST "):
            await check_before_send()
        await conn.sendall(request)
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = await conn.recv_any(65536)
            if not chunk:
                break
            raw += chunk
        if b"\r\n\r\n" not in raw:
            raise ConnectionLost("WebDriverAgent closed the connection without an answer")
        header, _, rest = raw.partition(b"\r\n\r\n")
        lines = header.decode("iso-8859-1").split("\r\n")
        status = int(lines[0].split(" ", 2)[1])
        length = next((int(ln.split(":", 1)[1]) for ln in lines[1:] if ln.lower().startswith("content-length:")), None)
        if length is not None:
            if length > len(rest):
                rest += await conn.recvall(length - len(rest))
                if len(rest) < length:
                    raise ConnectionLost("WebDriverAgent cut the answer short")
        elif keep:
            raise ConnectionLost("WebDriverAgent sent an answer without its length on a kept connection")
        else:
            while chunk := await conn.recv_any(65536):
                rest += chunk
        return status, rest

    async def open_stream(self):
        """A connection to WebDriverAgent's screen stream, request sent; read it with recv_any."""
        conn = await self._dial(MJPEG_PORT)
        try:
            await conn.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
        except BaseException:
            await self._close(conn)
            raise
        return conn

    async def _dial(self, port: int):
        """A connection to a port of the runner on the phone, with sendall / recv_any / recvall / close.
        Over USB (and unless TAPWRIGHT_DIRECT=0) it goes straight through usbmuxd: no tunnel, no userspace TCP stack
        in this process's loop. Otherwise, or when usbmuxd can't do it, through the developer tunnel as before.
        A refusal by the phone (the runner isn't listening yet) is a ConnectionLost, never a reason to fall back:
        the tunnel would be refused the same."""
        why: str | None = None
        if os.environ.get("TAPWRIGHT_DIRECT") == "0":
            why = "TAPWRIGHT_DIRECT=0"
        elif self.via != "USB":
            why = f"the phone is on {self.via or 'no connection'}, not USB"
        off = self._direct_off
        if why is None and off is not None and off[0] is self.rsd:
            if time.monotonic() - off[1] < DIRECT_RETRY:
                why = "usbmuxd failed a moment ago"
            else:
                self._direct_off = None
        if why is None:
            try:
                conn = await self._dial_usbmux(port)
            except ConnectionLost:
                raise
            except Exception as e:
                why = f"usbmuxd: {type(e).__name__}: {e}"
                self._direct_off = (self.rsd, time.monotonic())
            else:
                self._note_route("direct", "runner reached over usbmuxd directly")
                return conn
        conn = await self.rsd.create_service_connection(port)
        self._note_route("tunnel", f"runner reached over the tunnel ({why})")
        return conn

    async def _dial_usbmux(self, port: int) -> _Direct:
        from pymobiledevice3 import usbmux
        from pymobiledevice3.exceptions import ConnectionFailedError, ConnectionFailedToUsbmuxdError
        last: Exception | None = None
        for attempt in (0, 1):
            if self._mux is None:  # after a failure the entry is looked up again: a replug gives the phone a new id
                self._mux = await usbmux.select_device(self.udid, connection_type="USB")
                if self._mux is None:
                    raise DeviceError("usbmuxd doesn't list the phone on USB")
            try:
                return _Direct(await self._mux.connect(port))
            except ConnectionFailedToUsbmuxdError:
                self._mux = None
                raise  # usbmuxd itself isn't there: the tunnel's way
            except ConnectionFailedError as e:  # the phone refused: nothing is listening on the port
                raise ConnectionLost(f"can't reach WebDriverAgent: the phone refused port {port} ({e})") from e
            except Exception as e:
                self._mux, last = None, e
        assert last is not None
        raise last

    def _note_route(self, kind: str, line: str) -> None:
        """Log once per tunnel and kind which way the runner is reached."""
        if self._route != (self.rsd, kind):
            self._route = (self.rsd, kind)
            log = getattr(self, "log", None)  # (a Device built without __init__ has none)
            if log is not None:
                log(line)

    async def _drop_kept(self) -> None:
        held, self._kept = self._kept, None
        if held is not None:
            await self._close(held[1])

    @staticmethod
    async def _close(conn) -> None:
        """Close a connection, but never let a dead line hang the close."""
        try:
            await asyncio.wait_for(conn.close(), 2)
        except Exception:
            pass

    # ---- apps ----
    @_traced
    async def user_apps(self) -> list[dict]:
        """Apps you installed (App Store, TestFlight, development builds)."""
        from pymobiledevice3.services.installation_proxy import InstallationProxyService
        async with InstallationProxyService(lockdown=self.rsd) as proxy:
            apps = await proxy.get_apps(application_type="User")
        return sorted(({"bundle_id": bid, "name": info.get("CFBundleDisplayName") or info.get("CFBundleName", ""),
                        "version": info.get("CFBundleShortVersionString", ""),
                        "build": info.get("CFBundleVersion", ""),
                        "executable": info.get("CFBundleExecutable", "")} for bid, info in apps.items()),
                      key=lambda a: a["name"].lower())

    async def _stop_runner(self) -> None:
        """Stop the task that ran WebDriverAgent and collect how it ended."""
        runner, self.runner = self.runner, None
        if runner:
            runner.cancel()
            await asyncio.wait({runner}, timeout=STOP_WAIT)  # a task that ignores the cancel mustn't hang us
            if runner.done() and not runner.cancelled():
                runner.exception()  # collected, so it isn't logged as never retrieved

    async def close(self) -> None:
        had = bool(self._network_holds)
        await self.clear_network()  # first: the phone is never left offline
        if had:
            self._network_ended("the phone's connection was closed")
        await self._stop_runner()
        await self._drop_kept()
        if self.tunnel:
            await self.tunnel.aclose()


START_SLACK = 1.0  # seconds two readings of one process's start time may differ


def own_line(image: object, executable: object) -> bool:
    """Was a log line logged by the app's own code (W177)? Its sender image lies in the app's bundle (the `.app` folder its
    executable is in: the app itself, its frameworks, Hermes). A line from a system framework loaded into the process
    (XCTest's accessibility server answering WebDriverAgent, CFPreferences, power logging) isn't. When the phone doesn't
    say, the line counts as the app's."""
    if not isinstance(image, str) or not isinstance(executable, str) or not image or not executable:
        return True
    at = executable.find(".app/")
    if at < 0:
        return True

    def plain(path: str) -> str:
        return path[len("/private"):] if path.startswith("/private/") else path
    return plain(image).startswith(plain(executable[:at + len(".app/")]))


@dataclasses.dataclass(frozen=True)
class Proc:
    """One process of the phone: the pid, when it started (epoch seconds; None when iOS didn't say) and its bundle id."""
    pid: int
    started: float | None = None
    bundle: str | None = None

    def same_life(self, other: "Proc") -> bool:
        """Is `other` this very process (not another that got its pid later)? The pid and, when both start times are
        known, the start time. When one isn't known the pid alone has to do."""
        if self.pid != other.pid:
            return False
        return self.started is None or other.started is None or abs(self.started - other.started) <= START_SLACK


def _started_of(proc: dict) -> float | None:
    """The `startDate` of an entry of iOS's process list, as epoch seconds (None when it has none or can't be read)."""
    when = proc.get("startDate")
    try:
        return float(when.timestamp()) if hasattr(when, "timestamp") else (float(when) if when is not None else None)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


CRASH_MAX_BYTES = CRASH_MAX_MB * 1024 * 1024  # no crash file is read beyond this (AGENT-TOOLS §15.2 words it in MB)
CRASH_CHUNK = 256 * 1024  # bytes asked of the phone's file service at a time
CRASH_SCAN = 40  # crash files of one executable name looked at in one search
CRASH_HEAD = 64 * 1024  # bytes of a report's start that say whose it is (its header line, or the old `Identifier:` line)
CRASH_CLOSE_WAIT = 2.0  # seconds a close of a file on the phone may take: a service that stopped answering is left


class CrashTooBig(DeviceError):
    """A crash file over CRASH_MAX_BYTES: it is not read."""


def _size_of(stat: dict) -> int:
    try:
        return int(stat.get("st_size", 0))
    except (TypeError, ValueError):
        return CRASH_MAX_BYTES + 1  # a size that can't be read is not trusted to be small


async def _close_file(afc, handle) -> None:
    """Close a file of the phone, waiting at most CRASH_CLOSE_WAIT: this runs when a read was cancelled too, and a file
    service that stopped answering would hold the cancel (and whoever joins it) for ever. A close that fails is no
    matter. The abandoned close is cancelled and its error read."""
    closing = asyncio.ensure_future(afc.fclose(handle))
    try:
        await asyncio.wait({closing}, timeout=CRASH_CLOSE_WAIT)
    finally:
        if not closing.done():
            closing.cancel()
        closing.add_done_callback(lambda t: t.cancelled() or t.exception())


async def _head_of(afc, path: str) -> bytes:
    """The first CRASH_HEAD bytes of a file of the phone (nothing when they can't be read)."""
    try:
        handle = await afc.fopen(path)
        if not handle:
            return b""
        try:
            return await afc.fread(handle, CRASH_HEAD)
        finally:
            await _close_file(afc, handle)
    except asyncio.CancelledError:
        raise
    except Exception:
        return b""


async def _read_capped(afc, path: str, size: int) -> bytes:
    """A file of the phone, read in chunks and never beyond CRASH_MAX_BYTES: the size from the listing is checked
    first, and the reads are counted too, since a file can grow after it was looked at."""
    if size > CRASH_MAX_BYTES:
        raise CrashTooBig(f"{posixpath.basename(path)} is {size} bytes, over {CRASH_MAX_BYTES}")
    handle = await afc.fopen(path)
    if not handle:
        raise DeviceError(f"couldn't open {path}")
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            ask = min(CRASH_CHUNK, CRASH_MAX_BYTES + 1 - total)
            chunk = await afc.fread(handle, ask)
            total += len(chunk)
            if total > CRASH_MAX_BYTES:
                raise CrashTooBig(f"{posixpath.basename(path)} grew over {CRASH_MAX_BYTES} bytes while it was read")
            chunks.append(chunk)
            if len(chunk) < ask:  # the end of the file
                break
    finally:
        await _close_file(afc, handle)
    return b"".join(chunks)


PID_REFRESH = 2.0 # seconds between looks at which process ids the app has (it has new ones after a launch)
PID_RETRY = 0.25  # the same while no sample has shown the app's process (it is starting)
METER_START = 15.0  # seconds the instruments service may take to start sampling
METER_TEARDOWN = 3.0  # seconds one instrument may take to close before it is abandoned
METER_STOP = 15.0  # seconds stop() waits for the sampling task to end
PROCLIST_WAIT = 5.0  # seconds the phone's process list may take to answer for one batch of samples
FINAL_WAIT = 2.0  # seconds stop() lets batches waiting for the process list finish before it counts them as lost
MB = 1024 * 1024


class AppMeter:
    """Samples one app's own process over the developer tunnel (`performance`): memory (physical footprint) and CPU
    from sysmontap, and the display's frame rate from graphics. The process is found by bundle id from the phone's
    process list, and again after a launch (a new process has a new id). Every number comes from a process whose
    bundle id is the app's: nothing of another process is kept. A service that can't start leaves its numbers
    out (an iOS without the graphics service, say), and the answer says so."""

    def __init__(self, device: "Device", bundle_id: str) -> None:
        from .performance import Measurement
        self.device, self.bundle_id = device, bundle_id
        self.measurement = Measurement()
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._failed: Exception | None = None
        self._arrived = 0  # how many batches of samples have come, in order (a clock's turns are too coarse to tell them apart)
        self._boundary = 0  # how many had come when the window began: those, and no later one, are from before it
        self._skip_frame = False  # the first frame rate after the window began covers a second that began before it
        self._rows: asyncio.Queue = asyncio.Queue()  # (its number in the order of arrival, the rows) of each batch
        self._fresh_after: float | None = None  # a launch's window keeps only a process started at or after this (epoch s)
        self._seen: set[int] = set()  # the pids of the app's processes sampled so far
        self._stale: set[int] = set()  # ... as the window began: not the new process, whatever their start times say
        self._info = None  # the instruments' process list, once they are open
        self._done = 0  # the arrival number of the last batch that was dealt with (kept, dropped or counted lost)

    async def start(self) -> None:
        """Begin sampling. However this ends (too slow, a service that won't start, or the call cancelled while it
        waits), no sampling task is left running with its instrument sessions open."""
        self._task = asyncio.create_task(self._run(), name="app-meter")
        try:
            try:
                await aio.wait_for(self._ready.wait(), METER_START)  # (not asyncio.wait_for: on 3.10 it can drop a cancel)
            except asyncio.TimeoutError:
                raise DeviceError("the phone's instruments service didn't start sampling in time") from None
            if self._failed is not None:
                raise DeviceError(f"couldn't sample the app: {self._failed}") from self._failed
        except BaseException:
            await asyncio.shield(self.stop())
            raise

    async def begin_window(self, fresh: bool = False) -> None:
        """The measured window begins now: what the instruments gave until now (their set-up, the app that was running
        before a launch) is dropped. A batch is kept only if it arrived after this moment, whatever was waiting for the
        process list or queued behind it (each arrival is stamped by the reader, which never waits for the list), and
        the first frame rate after it is not kept (it covers a second that began before it).
        `fresh`: the window is a cold start, so only a process shown to have started after this moment is the app's."""
        from .performance import Measurement
        old: set[int] | None = set()
        if fresh:  # the app's processes listed now are the old ones, whatever their start times say
            old = await self._listed_pids()
        for _ in range(3):  # what the instruments have already delivered is read and stamped before the boundary
            await asyncio.sleep(0)
        self._boundary = self._arrived
        self.measurement = Measurement()
        self._fresh_after = time.time() - START_SLACK if fresh else None
        if old is None:  # the list couldn't be had: only what was sampled is known to be old, and the numbers say so
            self._stale = set(self._seen)
            self.measurement.lost += 1
        else:
            self._stale = old | set(self._seen)
        self._skip_frame = True

    async def _listed_pids(self) -> set[int] | None:
        """The pids of the app's processes the phone lists now (None when the list can't be had)."""
        if self._info is None:
            return None
        try:
            listed = await aio.wait_for(self._info.proclist(), PROCLIST_WAIT)
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
        return {p["pid"] for p in listed if p.get("bundleIdentifier") == self.bundle_id and isinstance(p.get("pid"), int)}

    async def stop(self):
        """End the sampling; the Measurement of what was seen. Bounded: an instrument that can't close (a tunnel that
        can't drain) doesn't hold the call."""
        task, self._task = self._task, None
        if task is not None:
            await self._finish_pending(task)
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=METER_STOP)
            if not done:
                self.device.log("performance: the instruments didn't close in time; leaving them")
            elif not task.cancelled():
                task.exception()  # collected, so it isn't logged as never retrieved
            pending = self._arrived - max(self._done, self._boundary)  # in the window, and never dealt with
            if pending > 0 and self.measurement.failed is None:
                self.measurement.lost += pending  # not sampled: the numbers cover only part of the window
        return self.measurement

    async def _finish_pending(self, task: asyncio.Task) -> None:
        """Batches that came in the window and wait for the process list (or behind one that does) get FINAL_WAIT
        seconds to be dealt with; what is still waiting then is counted as lost by stop()."""
        for _ in range(3):  # what the instruments have already delivered is read and stamped first
            await asyncio.sleep(0)
        deadline = time.monotonic() + FINAL_WAIT
        while not task.done() and self._arrived > max(self._done, self._boundary) and time.monotonic() < deadline:
            await asyncio.sleep(0.01)

    async def _enter(self, stack, cm):
        """Open an instrument and register its close, which waits at most METER_TEARDOWN (a close that hangs is
        abandoned, cancelled and said so, and the next instrument is closed all the same)."""
        opened = await cm.__aenter__()

        async def close() -> None:
            try:
                await aio.wait_for(cm.__aexit__(None, None, None), METER_TEARDOWN)
            except asyncio.TimeoutError:
                self.device.log(f"performance: closing {type(cm).__name__} took over {METER_TEARDOWN:g} s; abandoned")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # a close that fails: the rest are still closed
                self.device.log(f"performance: closing {type(cm).__name__} failed: {e}")
        stack.push_async_callback(close)
        return opened

    async def _run(self) -> None:
        from contextlib import AsyncExitStack
        from pymobiledevice3.services.dvt.instruments.device_info import DeviceInfo
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        from pymobiledevice3.services.dvt.instruments.graphics import Graphics
        from pymobiledevice3.services.dvt.instruments.sysmontap import Sysmontap
        from .performance import SAMPLE_MS
        try:
            async with AsyncExitStack() as stack:
                dvt = await self._enter(stack, DvtProvider(self.device.rsd))
                info = await self._enter(stack, DeviceInfo(dvt))
                self._info = info
                tap = await Sysmontap.create(dvt, SAMPLE_MS)
                await self._enter(stack, tap)
                jobs = [asyncio.create_task(self._rows_in(tap)), asyncio.create_task(self._processes(info))]
                try:
                    graphics = Graphics(dvt)
                    await self._enter(stack, graphics)
                    jobs.append(asyncio.create_task(self._frames(graphics)))
                except Exception as e:  # frames are left out; memory and CPU still count
                    self.device.log(f"performance: no frame rate ({e})")
                self._ready.set()
                try:
                    await asyncio.gather(*jobs)
                finally:
                    for job in jobs:
                        job.cancel()
                    await asyncio.gather(*jobs, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._failed = e
            self.measurement.failed = str(e) or type(e).__name__  # stop() hands this on: the numbers are partial
            self.device.log(f"performance: sampling stopped: {e}")
        finally:
            self._ready.set()

    def _is_new(self, pid: int, started: float | None) -> bool:
        """In a launch's window: is this process one that started after the window began?"""
        if self._fresh_after is None:
            return True
        if pid in self._stale:
            return False
        return started is not None and started >= self._fresh_after  # no start time: nothing shows it is a new one

    async def _rows_in(self, tap) -> None:
        """Take each batch from sysmontap the moment it comes and number it, whatever the process list is doing: a batch
        that waits behind a slow list keeps its place in the order of arrival, and so can be told from the window's."""
        async for rows in tap.iter_processes():
            self._arrived += 1
            self._rows.put_nowait((self._arrived, rows))

    async def _processes(self, info) -> None:
        """Each batch of rows is kept only for processes the phone's list shows as the app's right after the batch came
        (pid, bundle id and start time as listed then), so a pid that went to another process is never sampled as the
        app, and in a launch's window only for a process that started in it. A batch that arrived before the window
        is dropped. A list that can't be had drops the batch, and it is counted (`lost`): the numbers are then only
        part of the window."""
        while True:
            arrived, rows = await self._rows.get()
            await self._take(info, arrived, rows)
            self._done = arrived  # (a cancel in the middle leaves it as it was: stop() counts that batch as lost)

    async def _take(self, info, arrived: int, rows: list) -> None:
        """One batch of rows: kept, dropped (from before the window) or counted as lost."""
        if arrived <= self._boundary:
            return  # from before the window
        try:
            listed = await aio.wait_for(info.proclist(), PROCLIST_WAIT)
        except Exception:
            if arrived > self._boundary:  # (a batch from before the window, lost late, is not the window's loss)
                self.measurement.lost += 1  # whose pid it is can't be said: this batch isn't kept
            return
        if arrived <= self._boundary:
            return  # the window began while the list was asked for: the batch is from before it
        mine = {p["pid"]: _started_of(p) for p in listed
                if p.get("bundleIdentifier") == self.bundle_id and isinstance(p.get("pid"), int)}
        ours = [r for r in rows if r.get("pid") in mine and self._is_new(r["pid"], mine[r["pid"]])]
        self._seen.update(r["pid"] for r in rows if r.get("pid") in mine)
        memory = [r["physFootprint"] for r in ours if isinstance(r.get("physFootprint"), (int, float))]
        cpu = [r["cpuUsage"] for r in ours if isinstance(r.get("cpuUsage"), (int, float))]
        if memory:
            self.measurement.memory_mb.append(sum(memory) / MB)
        if cpu:
            self.measurement.cpu_pct.append(float(sum(cpu)))

    async def _frames(self, graphics) -> None:
        async for fps in graphics.fps():
            if self._skip_frame:  # covers the second before it came: part of it was before the window
                self._skip_frame = False
                continue
            self.measurement.fps.append(float(fps))


NETWORK_START = 20.0  # seconds the instruments service may take to set a condition
NETWORK_STOP = 5.0  # seconds a condition may take to clear before its connection is simply cut
# pymobiledevice3's errors for a connection that is gone (by class name: the library is imported only when used)
LINE_ERRORS = frozenset({"ConnectionTerminatedError", "ConnectionFailedError", "StreamClosedError", "TimeoutError"})


class NetworkHold:
    """One network condition on the phone (`network`), held by a task that keeps the instruments connection open: the
    condition is Apple's developer condition inducer, and it ends when that connection closes. So the connection is
    opened here, the profile set, and the task waits; stop() clears the condition and closes the connection (the
    phone is back to normal either way)."""

    def __init__(self, device: "Device", mode: str) -> None:
        self.device, self.mode = device, mode
        self._task: asyncio.Task | None = None
        self._release = asyncio.Event()
        self._ready: asyncio.Future = asyncio.get_running_loop().create_future()

    async def start(self) -> str:
        self._task = asyncio.create_task(self._run(), name="network-condition")
        try:
            return await asyncio.wait_for(asyncio.shield(self._ready), NETWORK_START)
        except asyncio.TimeoutError:
            await asyncio.shield(self.stop())
            raise DeviceError("the phone's condition service didn't answer in time") from None
        except BaseException:
            await asyncio.shield(self.stop())
            raise

    async def stop(self) -> None:
        self._release.set()
        task, self._task = self._task, None
        if task is None:
            return
        done, _ = await asyncio.wait({task}, timeout=NETWORK_STOP)
        if not done:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def abandon(self) -> None:
        """Stop without waiting (the tunnel is gone)."""
        self._release.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()

    async def _wait_for_release_or_loss(self, dvt) -> None:
        """Wait until stop() is asked for, or until the condition's own connection closes. pymobiledevice3 closes it
        in a background reader (the phone ended the session, the tunnel under it dropped): nothing raises here by
        itself, and the condition is gone with it, so the wait ends in an error that says so."""
        waiter = getattr(getattr(dvt, "_dtx", None), "wait_disconnected", None)
        if waiter is None:  # a library without the signal: stop() is the only way out, and the idle clear the backstop
            await self._release.wait()
            return
        released = asyncio.ensure_future(self._release.wait())
        lost = asyncio.ensure_future(waiter())
        try:
            await asyncio.wait({released, lost}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for pending in (released, lost):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(released, lost, return_exceptions=True)
        if lost.done() and not lost.cancelled() and not self._release.is_set():
            raise ConnectionLost("the connection closed")

    async def _run(self) -> None:
        from pymobiledevice3.services.dvt.instruments.condition_inducer import ConditionInducer
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        from .network import pick_profile
        try:
            async with DvtProvider(self.device.rsd) as dvt, ConditionInducer(dvt) as inducer:
                groups = await inducer.list()
                profile = pick_profile(self.mode, groups)
                if profile is None:
                    raise DeviceError(f"this phone offers no network condition for {self.mode}")
                await inducer.set(profile)
                if not self._ready.done():
                    self._ready.set_result(profile)
                await self._wait_for_release_or_loss(dvt)
                try:
                    await inducer.clear()
                except Exception as e:  # the connection closing ends the condition anyway
                    self.device.log(f"network: clearing: {e}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if not self._ready.done():
                if not isinstance(e, DeviceError):
                    # a dead line is ConnectionLost (the daemon reconnects); anything else the phone said is an error
                    lost = isinstance(e, (OSError, EOFError)) or type(e).__name__ in LINE_ERRORS
                    e = (ConnectionLost if lost else DeviceError)(str(e) or type(e).__name__)
                self._ready.set_exception(e)
            else:
                self.device.log(f"network: condition lost: {e}")
                if not self._release.is_set():  # nobody asked it to stop: it died
                    self.device._hold_died(self, f"the phone's condition connection closed ({str(e) or type(e).__name__})")
        finally:
            if not self._ready.done():
                self._ready.cancel()

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
import json
import os
import posixpath
import re
import sys
import time

from . import aio, store
from .diagnostics import CRASH_MAX_MB, CrashSearch, crash_pattern, only_mine

WDA_PORT = 8100
MJPEG_PORT = 9100  # WebDriverAgent's screen stream (MJPEG)
MIN_TIMEOUT = 0.5  # seconds: the least any one request to WebDriverAgent is given
KEEP_IDLE = 2.0  # seconds a kept connection may sit idle and still carry an action (see Device._on_kept)
STOP_WAIT = 5.0  # seconds _stop_runner waits for the runner task to end after cancelling it
TEST_SERVICE = "com.apple.dt.testmanagerd.remote"  # offered once the developer disk image is mounted


class DeviceError(RuntimeError):
    pass


class ConnectionLost(DeviceError):
    """The tunnel or WebDriverAgent stopped answering: a dead line, not an error reply."""


class Unanswered(DeviceError):
    """One action got no answer, and WebDriverAgent answers other requests: the line is alive, and whether the
    action happened isn't known. Not a ConnectionLost: nothing needs starting again."""


class PhoneLocked(ConnectionLost):
    """WebDriverAgent runs, but the phone is locked, so nothing on the screen can be read until it's unlocked."""


AUTH_PROMPT = "LocalAuthenticationUIService"  # the process that draws iOS's passcode screen
PROMPT_AFTER = 6.0  # seconds a start may take before the phone is asked whether that screen is up (a start takes 3)
PROMPT_EVERY = 3.0
PROMPT_RETRY = 300.0  # seconds after which a start is tried anyway, in case the process list is wrong about the screen
# iOS fails a start that waits at the passcode screen after a minute or a few, and takes the screen away; only a
# new start puts it back. Each start launches the test runner, which costs the app in front a longer frame gap
# (measured by Tapline: 0.25 to 0.36 s where 0.23 s is usual). So with nobody asking for the phone, the starts
# thin out: the pause before the 1st, 2nd, 3rd... new start after the screen went. A tool call or a viewer asks
# for one at once (`hurry`).
PROMPT_PAUSES = (0.0, 30.0, 60.0, 120.0, 300.0)


class PasscodeNeeded(PhoneLocked):
    """iOS shows "Enter iPhone Passcode for "XCTest" / Enable UI Automation" and starts no UI test until a person
    types the passcode. It asks on a first run, and again now and then unless Settings > Developer > Enable UI
    Automation is on (2026-11-10: it came up at midday on a phone that had run tests since the morning, and
    all the daemon said was "reconnecting"). The phone isn't locked, but the cure is the same: its owner."""

    def __init__(self):
        super().__init__("the phone is asking for its passcode to allow UI automation: enter it on the phone "
                         "(Settings > Developer > Enable UI Automation stops it asking)")


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


class Device:
    def __init__(self, udid: str | None = None, host: str | None = None, port: int | None = None, log=print):
        state = store.load_state()
        self.udid = udid or state.get("udid")
        self.host, self.port = host, port or (state.get("pairing_port") if host else None)
        self.log = log
        self.tunnel = None
        self.rsd = None
        self.via = None
        # One connection kept open to WebDriverAgent (see _on_kept). On over USB, where the reliability suite
        # passed with it (2026-11-11); off over a network until it has been measured there. TAPWRIGHT_KEEP=1 or
        # 0 overrides.
        forced = os.environ.get("TAPWRIGHT_KEEP")
        self._keep_forced = None if forced not in ("0", "1") else forced == "1"
        self._kept: tuple | None = None
        self._kept_lock = asyncio.Lock()
        self.runner = None  # the XCUITest task that keeps WebDriverAgent alive
        self.awaiting_passcode = False  # the last start stopped at iOS's passcode screen (see PasscodeNeeded)
        self._launched_at = 0.0  # when the test runner was last started (monotonic clock)
        self._prompt_rounds = 0  # starts in a row that stopped at the passcode screen
        self._gone_at: float | None = None  # when that screen was first seen gone, for the pause before the next start
        self._hurry = False  # see hurry()

    # ---- the tunnel ----
    async def connect(self) -> None:
        from pymobiledevice3 import usbmux
        from pymobiledevice3.remote import userspace_tunnel
        on_usb = [d for d in await usbmux.list_devices() if d.connection_type == "USB"
                  and (self.udid is None or d.serial == self.udid)]
        if self.host:
            if not self.port:
                raise DeviceError("no pairing port known for a direct address: run `tapwright probe` once on the "
                                  "same Wi-Fi as the phone (it learns the port), or pass --port")
            identifier = self._pairing_identifier()
            host, port = self.host, self.port

            async def provider(serial, autopair, remotepairing_fallback=True):
                from pymobiledevice3.remote.tunnel_service import create_core_device_tunnel_service_using_remotepairing
                return await create_core_device_tunnel_service_using_remotepairing(identifier, host, port), None
            # pymobiledevice3 picks USB or mDNS; to dial a given address, swap in this provider.
            userspace_tunnel._create_no_root_tunnel_provider = provider
            self.via = f"network {host}:{port}"
        elif on_usb:
            self.udid = self.udid or on_usb[0].serial
            self.via = "USB"
        else:
            from pymobiledevice3.remote.tunnel_service import get_remote_pairing_tunnel_services
            services = await get_remote_pairing_tunnel_services(udid=self.udid)
            if not services:
                raise PhoneNotFound("no phone on USB, and none paired on this network (same Wi-Fi, unlocked, "
                                    "Developer Mode on, `tapwright setup` done over USB once)")
            for extra in services[1:]:
                await extra.close()
            found = services[0]

            async def provider(serial, autopair, remotepairing_fallback=True):
                return found, None
            userspace_tunnel._create_no_root_tunnel_provider = provider
            self.via = "local network"
        self.tunnel = userspace_tunnel.UserspaceRsdTunnel(serial=self.udid)
        try:
            self.rsd = await self.tunnel.aopen()
        except Exception as e:
            raise DeviceError(f"couldn't open the developer tunnel over {self.via}: {e}") from e
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
        if self.runner and not self.runner.done() and await self.wda_alive():
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

    def hurry(self) -> None:
        """Someone is asking for the phone now (a tool call, a viewer): the next start doesn't wait its turn."""
        self._hurry = True

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
                and p.get("name") != AUTH_PROMPT]

    async def kill_app(self, bundle_id: str) -> int:
        """End every process of that app, as swiping it away in the app switcher does; how many there were.
        Works while the app is frozen, unlike WebDriverAgent's terminate, which waits on it."""
        from pymobiledevice3.services.dvt.instruments.device_info import DeviceInfo
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        from pymobiledevice3.services.dvt.instruments.process_control import ProcessControl
        async with DvtProvider(self.rsd) as dvt, DeviceInfo(dvt) as info, ProcessControl(dvt) as control:
            pids = [p["pid"] for p in await info.proclist() if p.get("bundleIdentifier") == bundle_id]
            for pid in pids:
                await control.kill(pid)
        return len(pids)

    # ---- diagnostics (AGENT-TOOLS §15) ----
    async def app_pids(self, bundle_id: str) -> list[int]:
        """The pids of that app's running processes, from iOS's process list (empty: it isn't running)."""
        from pymobiledevice3.services.dvt.instruments.device_info import DeviceInfo
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        async with DvtProvider(self.rsd) as dvt, DeviceInfo(dvt) as info:
            procs = await info.proclist()
        return [p["pid"] for p in procs if p.get("bundleIdentifier") == bundle_id and isinstance(p.get("pid"), int)]

    async def app_log_stream(self, pid: int):
        """The unified log entries of one process, as they come: (epoch ms, os_trace level byte, text, pid). The
        stream is asked for that pid only, and an entry of any other pid is dropped here as well."""
        from pymobiledevice3.services.os_trace import OsTraceService
        service = OsTraceService(lockdown=self.rsd)
        try:
            async for entry in service.syslog(pid=pid):
                if entry.pid == pid:
                    yield int(entry.timestamp.timestamp() * 1000), int(entry.level), entry.message, entry.pid
        finally:
            await self._close(service)

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
                try:
                    data = await _read_capped(manager.afc, path, size)
                    report = get_crash_report_from_buf(data.decode(), filename=path)
                except CrashTooBig:
                    too_big += 1
                    continue
                except (ValueError, KeyError, TypeError, AttributeError, IndexError):  # not a readable report
                    unreadable += 1
                    continue
                found += only_mine([report], executable, bundle_id)
            incomplete = len(found) < count and len(ordered) > CRASH_SCAN
        return CrashSearch(found, unreadable, too_big, incomplete, scanned)

    async def crash_file(self, filename: str) -> bytes:
        """The whole of one crash report, as it is on the phone (CrashTooBig when it is over the byte cap)."""
        from pymobiledevice3.services.crash_reports import CrashReportsManager
        async with CrashReportsManager(self.rsd) as manager:
            stat = await manager.afc.stat(filename)
            return await _read_capped(manager.afc, filename, _size_of(stat))

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
        """One request to WebDriverAgent, through the tunnel (works the same over USB and network).
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
                conn = await self.rsd.create_service_connection(WDA_PORT)
            except Exception as e:
                raise ConnectionLost(f"can't reach WebDriverAgent: {e}") from e
            try:
                status, rest = await self._exchange(conn, request(False))
            except DeviceError:
                raise
            except Exception as e:  # reset, closed, or cut off mid-answer
                raise ConnectionLost(f"the connection to WebDriverAgent dropped: {e}") from e
            finally:
                await self._close(conn)
        try:
            data = json.loads(rest or b"{}")
        except ValueError:
            if status >= 400:
                raise DeviceError(f"WebDriverAgent: HTTP {status}") from None
            raise ConnectionLost("WebDriverAgent sent an answer that isn't JSON") from None
        if status >= 400 or (isinstance(data.get("value"), dict) and data["value"].get("error")):
            err = data.get("value", {}) if isinstance(data.get("value"), dict) else {}
            raise DeviceError(f"WebDriverAgent: {err.get('error', status)}: {err.get('message', '')[:300]}")
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
                        held = (self.rsd, await self.rsd.create_service_connection(WDA_PORT), time.monotonic())
                    except Exception as e:
                        raise ConnectionLost(f"can't reach WebDriverAgent: {e}") from e
                self._kept = None  # in use: nothing else finds it half-read
                status, rest = await self._exchange(held[1], request, keep=True)
            except BaseException as e:  # also a timeout's cancellation: the answer may be half-read
                if held is not None:
                    await asyncio.shield(self._close(held[1]))
                if isinstance(e, DeviceError) and not isinstance(e, ConnectionLost):
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
        conn = await self.rsd.create_service_connection(MJPEG_PORT)
        try:
            await conn.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
        except BaseException:
            await self._close(conn)
            raise
        return conn

    @staticmethod
    async def _close(conn) -> None:
        """Close a connection, but never let a dead line hang the close."""
        try:
            await asyncio.wait_for(conn.close(), 2)
        except Exception:
            pass

    # ---- apps ----
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
        await self._stop_runner()
        if self.tunnel:
            await self.tunnel.aclose()


CRASH_MAX_BYTES = CRASH_MAX_MB * 1024 * 1024  # no crash file is read beyond this (AGENT-TOOLS §15.2 words it in MB)
CRASH_CHUNK = 256 * 1024  # bytes asked of the phone's file service at a time
CRASH_SCAN = 40  # crash files of one executable name looked at in one search


class CrashTooBig(DeviceError):
    """A crash file over CRASH_MAX_BYTES: it is not read."""


def _size_of(stat: dict) -> int:
    try:
        return int(stat.get("st_size", 0))
    except (TypeError, ValueError):
        return CRASH_MAX_BYTES + 1  # a size that can't be read is not trusted to be small


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
        await afc.fclose(handle)
    return b"".join(chunks)


PID_REFRESH = 2.0 # seconds between looks at which process ids the app has (it has new ones after a launch)
PID_RETRY = 0.25  # the same while no sample has shown the app's process (it is starting)
METER_START = 15.0  # seconds the instruments service may take to start sampling
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

    async def stop(self):
        """End the sampling; the Measurement of what was seen."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return self.measurement

    async def _run(self) -> None:
        from contextlib import AsyncExitStack
        from pymobiledevice3.services.dvt.instruments.device_info import DeviceInfo
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        from pymobiledevice3.services.dvt.instruments.graphics import Graphics
        from pymobiledevice3.services.dvt.instruments.sysmontap import Sysmontap
        from .performance import SAMPLE_MS
        try:
            async with AsyncExitStack() as stack:
                dvt = await stack.enter_async_context(DvtProvider(self.device.rsd))
                info = await stack.enter_async_context(DeviceInfo(dvt))
                tap = await Sysmontap.create(dvt, SAMPLE_MS)
                await stack.enter_async_context(tap)
                jobs = [asyncio.create_task(self._processes(tap, info))]
                try:
                    graphics = Graphics(dvt)
                    await stack.enter_async_context(graphics)
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

    async def _processes(self, tap, info) -> None:
        pids: set[int] = set()
        looked = -PID_REFRESH
        async for rows in tap.iter_processes():
            now = time.monotonic()
            ours = [r for r in rows if r.get("pid") in pids]  # only the app's own process, whatever else is listed
            if now - looked >= (PID_REFRESH if ours else PID_RETRY):  # new process ids come with a launch
                pids = {p["pid"] for p in await info.proclist() if p.get("bundleIdentifier") == self.bundle_id}
                looked = now
                ours = [r for r in rows if r.get("pid") in pids]
            memory = [r["physFootprint"] for r in ours if isinstance(r.get("physFootprint"), (int, float))]
            cpu = [r["cpuUsage"] for r in ours if isinstance(r.get("cpuUsage"), (int, float))]
            if memory:
                self.measurement.memory_mb.append(sum(memory) / MB)
            if cpu:
                self.measurement.cpu_pct.append(float(sum(cpu)))

    async def _frames(self, graphics) -> None:
        async for fps in graphics.fps():
            self.measurement.fps.append(float(fps))

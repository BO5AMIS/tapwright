"""tapwright: let LLM agents test apps on your real iPhone, from Windows, Linux or macOS.

  tapwright setup     one time, phone on USB: pair, sign and install WebDriverAgent,
                     pair for network use. Signing: --asc-key-id/--asc-issuer-id/--asc-key-file
                     with --bundle-prefix (App Store Connect API key), or --p12/--profile
  tapwright doctor [--fix]
                     check the setup and say what's missing; --fix repairs what it can (the
                     developer disk image, WebDriverAgent and its signature, network pairing)
  tapwright smoke --app BUNDLE_ID
                     open an app, read its first screen, save a screenshot, close it
  tapwright probe [--host IP] [--watch]
                     does it work over the network? Learns the pairing port on the local
                     Wi-Fi; with --host (e.g. the phone's Tailscale IP) tries that address
  tapwright serve [--phone ADDRESS] [--port N] [--keep-alive] [--new-token] [--stop]
                     the background process that owns the phone: the live view in your
                     browser, the agent tools and an HTTP API. Agent tools start it by
                     themselves; it stops after 30 minutes unused (--keep-alive: never)
  tapwright view     open the live view in your browser (starts the daemon if needed)
  tapwright mcp [--screenshots DIR]  the MCP server for agents (stdio); it talks to the daemon
  tapwright install --client claude|codex|cursor --apps ID[,ID] [--dedicated] [--project DIR]
                     connect an agent in one go: register the MCP server and install the skill

Connection: USB when the phone is plugged in, else the local network; serve --phone dials an
address such as the phone's Tailscale IP (experimental).
"""
import argparse
import asyncio
import logging
import socket
import sys
import time
from pathlib import Path

from . import store
from .repair import Phone, run_fixes


def ok(msg):
    print(f"  ok    {msg}", flush=True)


def note(msg):
    print(f"  note  {msg}", flush=True)


def fail(msg):
    print(f"  FAIL  {msg}", flush=True)
    return 1


def _daemon_owns_phone() -> bool:
    from . import client
    return client.healthy(store.read_serve_info()) is not None


DAEMON_RUNNING = "tapwright serve is running and owns the phone: stop it first with `tapwright serve --stop`"
INSTALL_WAIT = 180.0  # seconds the phone gets to install WebDriverAgent (it takes about 20)


# ---------- setup ----------

async def wda_on_phone(lockdown, runner_id: str) -> bool:
    """Is the WebDriverAgent runner app installed on the phone under this bundle id?"""
    from pymobiledevice3.services.installation_proxy import InstallationProxyService
    async with InstallationProxyService(lockdown=lockdown) as proxy:
        apps = await proxy.get_apps(application_type="User", bundle_identifiers=[runner_id])
    return runner_id in apps


async def install_wda(lockdown, udid: str) -> str:
    """Sign WebDriverAgent with the stored identity and install it. Returns the runner's bundle id. Raises
    RuntimeError with a sentence for the user (signing failed, or the phone didn't finish)."""
    from pymobiledevice3.services.installation_proxy import InstallationProxyService

    from . import repair, signing
    ipa, runner_id = signing.sign_wda(store.HOME / "build")
    try:
        async with InstallationProxyService(lockdown=lockdown) as proxy:
            await asyncio.wait_for(proxy.install_from_local(ipa), INSTALL_WAIT)
    except asyncio.TimeoutError:
        # iOS keeps an install open while the app it replaces still runs (seen 2026-11-11, IXErrorDomain 48)
        raise RuntimeError(f"the phone didn't finish installing WebDriverAgent in {INSTALL_WAIT:.0f} s. If a test "
                           "runner is still running on the phone (another tool, or Xcode), stop it, then run "
                           "this again.") from None
    store.save_state(udid=udid, runner_id=runner_id)
    repair.record_install(repair.profile_expiry())
    return runner_id


def network_paired(udid: str) -> bool:
    """Is there a saved network pairing record for this phone? False when it can't tell."""
    try:
        from pymobiledevice3.pair_records import iter_remote_pair_records_by_identifier
        return any(ident == udid for ident, _, _ in iter_remote_pair_records_by_identifier())
    except Exception:
        return False


NEXT_STEPS = ("\nNext:\n"
              "  1. tapwright smoke --app <bundle id of one of your apps>\n"
              "  2. tapwright install --client claude --apps <bundle id>     (or --client codex, --client cursor)\n"
              "Keep it working later with: tapwright doctor --fix")


async def wda_current(lockdown, runner_id: str) -> bool:
    """Is WebDriverAgent installed under `runner_id`, signed with a profile that lasts beyond REINSTALL_DAYS?"""
    from . import repair
    if store.load_state().get("runner_id") != runner_id or repair.recorded_install_expiry() is None:
        return False
    facts = repair.WdaFacts(installed=await wda_on_phone(lockdown, runner_id),
                            installed_expiry=repair.recorded_install_expiry(), profile=repair.profile_expiry(),
                            profile_has_phone=True, certificate=repair.certificate_expiry())
    return repair.decide_wda(facts, repair.now_utc()).verdict == "ok"


async def setup(a) -> int:
    """One time, and again whenever something needs redoing: a second run skips the steps already done
    (WebDriverAgent installed and signed for another 14 days, network pairing saved) and ends with the
    commands to run next. `--force` does every step again."""
    from pymobiledevice3 import usbmux
    from pymobiledevice3.exceptions import RemotePairingCompletedError
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.remote.tunnel_service import RemotePairingLockdownService

    from . import repair, signing
    if _daemon_owns_phone():  # its WebDriverAgent is running: iOS would hold the new one's install open
        return fail(DAEMON_RUNNING)
    print("1. The iPhone, on USB")
    usb = [d for d in await usbmux.list_devices() if d.connection_type == "USB"]
    if not usb:
        return fail("no iPhone on USB. Plug it in and unlock it. (Windows: install Apple Devices from the "
                    "Microsoft Store; Linux: usbmuxd.)")
    udid = a.udid or usb[0].serial
    lockdown = await create_using_usbmux(serial=udid, autopair=True)  # the phone asks "Trust This Computer?"
    name = lockdown.all_values.get("DeviceName") or "iPhone"
    ok(f"{name} ({lockdown.display_name or lockdown.product_type}, iOS {lockdown.product_version}) {udid}")

    print("2. Developer Mode")
    if not await lockdown.get_developer_mode_status():
        from pymobiledevice3.services.amfi import AmfiService
        await AmfiService(lockdown).reveal_developer_mode_option_in_ui()
        return fail("Developer Mode is off. On the iPhone: Settings > Privacy & Security > Developer Mode, turn "
                    "it on, restart, confirm Turn On. Then run setup again.")
    ok("on")

    print("3. Signing identity")
    new_identity = bool(a.asc_key_id or a.p12)
    if a.asc_key_id:
        if not a.bundle_prefix:
            return fail("--bundle-prefix is needed with an API key, e.g. com.yourname.tapwright "
                        "(the App ID com.yourname.tapwright.* is created for WebDriverAgent)")
        signing.create_identity(signing.AppStoreConnect(a.asc_key_id, a.asc_issuer_id, a.asc_key_file),
                                udid, a.bundle_prefix)
        ok("development certificate and profile from App Store Connect")
    elif a.p12:
        signing.import_identity(a.p12, a.p12_password or "", a.profile)
        ok("imported your certificate and profile")
    elif not signing.PROFILE_FILE.exists():
        return fail("no signing identity yet: pass an App Store Connect API key (--asc-key-id, --asc-issuer-id, "
                    "--asc-key-file, --bundle-prefix) or --p12 and --profile. See docs/setup.md.")
    else:
        ok("already have one (kept)")
    profile = signing.read_profile()
    if udid not in profile.get("ProvisionedDevices", []):
        return fail("the profile doesn't include this iPhone; make a new one that does (with an API key, "
                    "setup does it)")
    ok(f"profile '{profile['Name']}', expires {profile['ExpirationDate']:%Y-%m-%d}")
    stored = repair.profile_expiry()
    if not new_identity and stored and repair.expiry_lines([stored], repair.now_utc(), repair.REINSTALL_DAYS):
        return fail(f"the stored profile ends {stored.date()}. Run setup again with your App Store Connect key "
                    "(--asc-key-id, --asc-issuer-id, --asc-key-file, --bundle-prefix): it makes a new profile.")

    print("4. WebDriverAgent")
    runner_id = signing.runner_ids(profile)[1]
    if not new_identity and not a.force and await wda_current(lockdown, runner_id):
        ok(f"already installed and signed ({runner_id}); skipped (--force installs it again)")
    else:
        try:
            runner_id = await install_wda(lockdown, udid)
        except RuntimeError as e:
            return fail(str(e))
        ok(f"installed as {runner_id}")

    print("5. Network pairing (Wi-Fi and remote use)")
    if network_paired(udid) and not a.force:
        ok("already paired; skipped")
    else:
        service = await RemotePairingLockdownService.create(lockdown)
        try:
            await service.connect(autopair=True)
        except RemotePairingCompletedError:
            pass
        finally:
            await service.close()
        ok("paired")
    store.save_state(udid=udid, runner_id=runner_id, phone=name)
    print("\nDone." + NEXT_STEPS)
    return 0


# ---------- doctor ----------

class UsbPhone(Phone):
    """The repairs of `doctor --fix`, done on the phone over USB."""

    def __init__(self, udid: str):
        from .device import Device
        self.udid = udid
        self.device = Device(udid=udid, log=lambda msg: None)
        self._lockdown = None

    async def _connected(self):
        if self.device.rsd is None:
            await self.device.connect()
        return self.device

    async def _lock(self):
        from pymobiledevice3.lockdown import create_using_usbmux
        if self._lockdown is None:
            self._lockdown = await create_using_usbmux(serial=self.udid, autopair=True)
        return self._lockdown

    async def image_mounted(self) -> bool:
        from .device import TEST_SERVICE
        device = await self._connected()
        return TEST_SERVICE in device.rsd.peer_info["Services"]

    async def mount_image(self) -> None:
        await (await self._connected()).mount()

    async def wda_installed(self) -> bool:
        from . import signing
        try:
            runner_id = signing.runner_ids(signing.read_profile())[1]
        except (OSError, RuntimeError, KeyError, ValueError):
            return False  # no usable profile: the decision says so
        return await wda_on_phone(await self._lock(), runner_id)

    async def install_wda(self) -> None:
        await install_wda(await self._lock(), self.udid)

    async def refresh_pairing_port(self) -> tuple[int | None, int | None]:
        from pymobiledevice3.bonjour import browse_remotepairing
        saved = store.load_state().get("pairing_port")
        found = await browse_remotepairing(timeout=6)
        if not found:
            return None, saved
        port = found[0].port
        if port != saved:
            store.save_state(pairing_port=port)
        return port, saved

    async def close(self) -> None:
        await self.device.close()


async def doctor_fix(problems: int) -> int:
    """The repairs of `doctor --fix`, with the phone on USB. Returns the problems found so far plus those left."""
    from pymobiledevice3 import usbmux
    if _daemon_owns_phone():
        return problems + fail(DAEMON_RUNNING)
    print("\nRepairs (phone on USB)")
    usb = [d for d in await usbmux.list_devices() if d.connection_type == "USB"]
    if not usb:
        return problems + fail("--fix needs the iPhone on USB. Plug it in and unlock it, then run it again.")
    udid = store.load_state().get("udid")
    udid = udid if udid in [d.serial for d in usb] else usb[0].serial
    phone = UsbPhone(udid)
    try:
        return problems + await run_fixes(phone, lambda kind, msg: {"ok": ok, "note": note, "fail": fail}[kind](msg))
    finally:
        await phone.close()


async def doctor(a) -> int:
    """Check the setup. With `--fix`, repair what can be repaired with the signing material already stored."""
    from importlib.metadata import version
    from pymobiledevice3 import usbmux

    from . import repair, signing
    problems = 0
    ok(f"Python {sys.version.split()[0]}, pymobiledevice3 {version('pymobiledevice3')}")
    try:
        devices = await usbmux.list_devices()
        ok(f"device service reachable; on USB: {[d.serial for d in devices if d.connection_type == 'USB'] or 'none'}")
    except Exception as e:
        problems += fail(f"device service unreachable ({e}). Windows: install Apple Devices (Microsoft Store); "
                         "Linux: usbmuxd")
    if signing.PROFILE_FILE.exists():
        p = signing.read_profile()
        expired = p["ExpirationDate"].timestamp() < time.time()
        (fail if expired else ok)(f"profile '{p['Name']}' expires {p['ExpirationDate']:%Y-%m-%d}, "
                                  f"{len(p.get('ProvisionedDevices', []))} device(s)")
        problems += expired
    else:
        problems += fail("no signing identity: run tapwright setup")
    now = repair.now_utc()
    for e in (repair.profile_expiry(), repair.certificate_expiry()):
        for line in repair.expiry_lines([e], now) if e else []:
            if e.when > now:  # an expired profile has failed above
                note(f"{line}. Renew it before then: tapwright setup (App Store Connect key, phone on USB)")
            elif e.what == "certificate":
                problems += fail(f"{line}. Run tapwright setup again with your App Store Connect key")
    state = store.load_state()
    if state.get("runner_id"):
        ok(f"WebDriverAgent set up for {state.get('phone') or state.get('udid')}")
    elif a.fix:
        note("WebDriverAgent isn't recorded as installed; checking the phone below")
    else:
        problems += fail("WebDriverAgent not installed yet: run tapwright setup (phone on USB), or tapwright "
                         "doctor --fix if it is set up before")
    if state.get("pairing_port"):
        ok(f"network pairing port known: {state['pairing_port']} (tapwright probe refreshes it)")
    else:
        note("network pairing port unknown: run tapwright probe with the phone on the same Wi-Fi")
    if a.fix:
        problems = await doctor_fix(problems)
    elif problems:
        note("tapwright doctor --fix repairs what it can (phone on USB)")
    print(f"\n{problems} problem(s)." if problems else "\nAll set.")
    return 1 if problems else 0


# ---------- probe: does it work over the network / a VPN? ----------

def _tcp(host: str, port: int, timeout: float = 3.0) -> str:
    s = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
    s.settimeout(timeout)
    try:
        s.connect((host.split("%")[0], port))
        return "open"
    except socket.timeout:
        return "no answer"
    except OSError:
        return "refused"
    finally:
        s.close()


async def probe(a) -> int:
    if _daemon_owns_phone():
        return fail(DAEMON_RUNNING)
    from pymobiledevice3.bonjour import browse_remotepairing

    from .device import Device
    from .session import Session
    print("1. The phone's network pairing service on this Wi-Fi (mDNS)")
    found = await browse_remotepairing(timeout=6)
    if found:
        port = found[0].port
        store.save_state(pairing_port=port)
        ok(f"port {port} at {[x.full_ip for x in found[0].addresses]}")
    else:
        port = store.load_state().get("pairing_port")
        note(f"not found on this network; using the last known port {port}" if port else
             "not found, and no known port: put the phone on the same Wi-Fi once")
        if not port:
            return 1
    if a.host:
        print(f"2. Reachable at {a.host}?")
        reach = _tcp(a.host, port)
        if reach != "open":
            return fail(f"{a.host}:{port} {reach}: the phone doesn't offer its developer service at that address")
        ok(f"{a.host}:{port} open")
    print("3. Developer tunnel, WebDriverAgent, a screenshot")
    session = Session(Device(host=a.host, port=port if a.host else None))
    try:
        t0 = time.time()
        text, image = await session.look()
        ok(f"over {session.device.via} in {time.time() - t0:.1f} s; {text.splitlines()[0]}")
        shot = Path(f"tapwright-probe-{int(time.time())}.jpg")
        shot.write_bytes(image)
        ok(f"screenshot: {shot}")
        if a.watch:
            print("4. Watching (Ctrl+C stops). Now change the phone's network (e.g. Wi-Fi off).")
            while True:
                alive = await session.device.wda_alive()
                print(f"  {time.strftime('%H:%M:%S')}  {'alive' if alive else 'LOST'}", flush=True)
                await asyncio.sleep(2)
        return 0
    finally:
        await session.close()


# ---------- smoke test ----------

async def smoke(a) -> int:
    if _daemon_owns_phone():
        return fail(DAEMON_RUNNING)
    from .device import Device
    from .session import Session
    session = Session(Device(host=a.host))
    try:
        print(f"Opening {a.app} ...")
        t0 = time.time()
        await session.launch(a.app)
        await asyncio.sleep(a.wait)
        text, image = await session.look()
        print(text)
        shot = Path(f"tapwright-smoke-{int(time.time())}.jpg")
        shot.write_bytes(image)
        ok(f"screen read in {time.time() - t0:.1f} s; screenshot {shot}")
        if not a.keep_open:
            await session.terminate(a.app)
        return 0
    finally:
        await session.close()


# ---------- the daemon ----------

def stop_daemon() -> int:
    from . import client
    info = store.read_serve_info()
    if not client.healthy(info):
        print("tapwright serve isn't running")
        return 0
    client.shutdown(info)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if client.healthy(store.read_serve_info()) is None:
            print("stopped")
            return 0
        time.sleep(0.3)
    print("asked it to stop; it's still running, see ~/.tapwright/serve.log")
    return 1


def view() -> int:
    import webbrowser

    from . import client
    try:
        url = client.viewer_url(client.find_or_start())
    except client.DaemonError as e:
        print(f"couldn't start the live view: {e}")
        return 1
    print(f"Tapwright live view: {url}")
    webbrowser.open(url)
    return 0


def skill(a) -> int:
    """`tapwright skill install`: put the agent skill where Claude Code, Codex and other agents look for skills."""
    from . import skills
    if a.action == "path":
        print(skills.SOURCE)
        return 0
    try:
        written = skills.install(a.project)
    except OSError as e:
        print(f"couldn't install the skill: {e}")
        return 1
    print(skills.installed_text(written))
    return 0


def install(a) -> int:
    """`tapwright install --client …`: register the MCP server with an agent and install the skill."""
    from . import connect
    apps = [x.strip() for x in (a.apps or "").split(",") if x.strip()]
    try:
        lines = connect.install(a.client, apps, a.dedicated, a.project)
    except connect.ConnectError as e:
        print(f"tapwright install: {e}")
        return 1
    print("\n".join(lines))
    return 0


def main() -> int:
    # pymobiledevice3's test-runner channel logs every message it drops while the runner shuts down as a warning
    # ("received connection termination: <DTXMessage …>", "No channel registered …"): expected, and only noise on a
    # new user's console (seen in a clean install's `tapwright smoke`). Its errors still show.
    logging.getLogger("pymobiledevice3.dtx").setLevel(logging.ERROR)
    ap = argparse.ArgumentParser(prog="tapwright", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("setup")
    p.add_argument("--udid")
    p.add_argument("--asc-key-id")
    p.add_argument("--asc-issuer-id")
    p.add_argument("--asc-key-file")
    p.add_argument("--bundle-prefix")
    p.add_argument("--p12")
    p.add_argument("--p12-password")
    p.add_argument("--profile")
    p.add_argument("--force", action="store_true",
                   help="do every step again, even those already done (WebDriverAgent, network pairing)")
    p = sub.add_parser("doctor")
    p.add_argument("--fix", action="store_true",
                   help="repair what it can with the phone on USB: mount the developer disk image, install or "
                        "re-sign WebDriverAgent with the stored profile, refresh the network pairing")
    p = sub.add_parser("smoke")
    p.add_argument("--app", required=True, help="bundle id, e.g. com.example.app")
    p.add_argument("--wait", type=float, default=3.0, help="seconds to let the app start (default 3)")
    p.add_argument("--keep-open", action="store_true")
    p.add_argument("--host")
    p = sub.add_parser("probe")
    p.add_argument("--host", help="an address of the phone to try, e.g. its Tailscale IP")
    p.add_argument("--watch", action="store_true")
    p = sub.add_parser("serve")
    p.add_argument("--port", type=int, help="default 8417; a free port if that one is taken")
    p.add_argument("--phone", help="reach the phone at this address (e.g. its Tailscale IP) instead of USB or Wi-Fi")
    p.add_argument("--keep-alive", action="store_true", help="don't stop after 30 minutes without use")
    p.add_argument("--new-token", action="store_true", help="replace the live view's access token")
    p.add_argument("--stop", action="store_true", help="stop the running daemon")
    p.add_argument("--settle-on-video", action="store_true",
                   help="experimental, with --video: actions watch the video's frames and the JPEG stream stays "
                        "closed (faster over Wi-Fi; a stalled mirror can make an action answer 'No visible change')")
    p.add_argument("--video", action="store_true",
                   help="show the live view as video: start the phone's Screen Mirroring to this computer (same "
                        "Wi-Fi; needs UxPlay and `pip install tapwright[video]`)")
    sub.add_parser("view")
    p = sub.add_parser("skill", help="the agent skill: how to test with Tapwright, for Claude Code, Codex and others")
    p.add_argument("action", choices=("install", "path"),
                   help="install: copy it to .claude/skills and .agents/skills; path: print where it ships")
    p.add_argument("--project", metavar="DIR", help="install into this project's folder instead of your home folder")
    p = sub.add_parser("install", help="connect an agent: register the MCP server with it and install the skill")
    p.add_argument("--client", required=True, choices=("claude", "codex", "cursor"),
                   help="the agent to connect: Claude Code, Codex or Cursor")
    p.add_argument("--apps", metavar="APP[,APP]",
                   help="the apps the agent may read and touch, as bundle ids or exact names (personal mode)")
    p.add_argument("--dedicated", action="store_true",
                   help="the phone is kept for testing: the agent may use every app on it")
    p.add_argument("--project", metavar="DIR",
                   help="for this project only: its .mcp.json (Claude Code) or .cursor/mcp.json (Cursor), and its skill folders")
    p = sub.add_parser("mcp")
    p.add_argument("--screenshots", metavar="DIR",
                   help="save screenshots in DIR (the newest 50) and answer with their path, instead of sending images")
    scope = p.add_mutually_exclusive_group()
    scope.add_argument("--apps", metavar="APP[,APP]",
                       help="the apps the agent may read and touch, as bundle ids or exact names (personal mode, the "
                            "default: without this list the agent can do nothing)")
    scope.add_argument("--dedicated", action="store_true",
                       help="the phone is kept for testing: the agent may use every app on it")
    a = ap.parse_args()
    if a.cmd == "mcp":
        from . import mcp_server
        apps = [x.strip() for x in (a.apps or "").split(",") if x.strip()]
        mcp_server.run(screenshots=a.screenshots, apps=apps, dedicated=a.dedicated)
        return 0
    if a.cmd == "view":
        return view()
    if a.cmd == "skill":
        return skill(a)
    if a.cmd == "install":
        return install(a)
    if a.cmd == "serve" and a.stop:
        return stop_daemon()
    from .device import run
    if a.cmd == "serve":
        from . import server
        port = a.port if a.port is not None else server.DEFAULT_PORT
        return run(lambda: server.serve(port=port, keep_alive=a.keep_alive, new_token=a.new_token, phone=a.phone,
                                        video=a.video, settle_on_video=a.settle_on_video))
    return run({"setup": setup, "doctor": doctor, "smoke": smoke, "probe": probe}[a.cmd], a)


if __name__ == "__main__":
    sys.exit(main())

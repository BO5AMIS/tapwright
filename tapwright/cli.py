"""tapwright: let LLM agents test apps on your real iPhone, from Windows, Linux or macOS.

  tapwright setup     one time, phone on USB: pair, sign and install WebDriverAgent,
                     pair for network use. Signing: --asc-key-id/--asc-issuer-id/--asc-key-file
                     with --bundle-prefix (App Store Connect API key), or --p12/--profile
  tapwright doctor    check the setup and say what's missing
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
  tapwright mcp      the MCP server for agents (stdio); it talks to the daemon

Connection: USB when the phone is plugged in, else the local network; serve --phone dials an
address such as the phone's Tailscale IP (experimental).
"""
import argparse
import asyncio
import socket
import sys
import time
from pathlib import Path

from . import store


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


# ---------- setup ----------

async def setup(a) -> int:
    from pymobiledevice3 import usbmux
    from pymobiledevice3.exceptions import RemotePairingCompletedError
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.remote.tunnel_service import RemotePairingLockdownService
    from pymobiledevice3.services.installation_proxy import InstallationProxyService

    from . import signing
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
    profile = signing.read_profile()
    if udid not in profile.get("ProvisionedDevices", []):
        return fail("the profile doesn't include this iPhone; make a new one that does (with an API key, "
                    "setup does it)")
    ok(f"profile '{profile['Name']}', expires {profile['ExpirationDate']:%Y-%m-%d}")

    print("4. WebDriverAgent")
    ipa, runner_id = signing.sign_wda(store.HOME / "build")
    async with InstallationProxyService(lockdown=lockdown) as proxy:
        await proxy.install_from_local(ipa)
    ok(f"installed as {runner_id}")

    print("5. Network pairing (Wi-Fi and remote use)")
    service = await RemotePairingLockdownService.create(lockdown)
    try:
        await service.connect(autopair=True)
    except RemotePairingCompletedError:
        pass
    finally:
        await service.close()
    ok("paired")
    store.save_state(udid=udid, runner_id=runner_id, phone=name)
    print("\nDone. Try: tapwright smoke --app <bundle id of one of your apps>")
    return 0


# ---------- doctor ----------

async def doctor(a) -> int:
    from importlib.metadata import version
    from pymobiledevice3 import usbmux

    from . import signing
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
    state = store.load_state()
    if state.get("runner_id"):
        ok(f"WebDriverAgent set up for {state.get('phone') or state.get('udid')}")
    else:
        problems += fail("WebDriverAgent not installed yet: run tapwright setup (phone on USB)")
    if state.get("pairing_port"):
        ok(f"network pairing port known: {state['pairing_port']} (tapwright probe refreshes it)")
    else:
        note("network pairing port unknown: run tapwright probe with the phone on the same Wi-Fi")
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
    print("stopped")
    return 0


def view() -> int:
    import webbrowser

    from . import client
    url = client.viewer_url(client.find_or_start())
    print(f"Tapwright live view: {url}")
    webbrowser.open(url)
    return 0


def main() -> int:
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
    sub.add_parser("doctor")
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
    sub.add_parser("view")
    sub.add_parser("mcp")
    a = ap.parse_args()
    if a.cmd == "mcp":
        from . import mcp_server
        mcp_server.run()
        return 0
    if a.cmd == "view":
        return view()
    if a.cmd == "serve" and a.stop:
        return stop_daemon()
    from .device import run
    if a.cmd == "serve":
        from . import server
        port = a.port if a.port is not None else server.DEFAULT_PORT
        return run(lambda: server.serve(port=port, keep_alive=a.keep_alive, new_token=a.new_token, phone=a.phone))
    return run({"setup": setup, "doctor": doctor, "smoke": smoke, "probe": probe}[a.cmd], a)


if __name__ == "__main__":
    sys.exit(main())

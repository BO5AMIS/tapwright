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
import sys
import time

from . import store

WDA_PORT = 8100
TEST_SERVICE = "com.apple.dt.testmanagerd.remote"  # offered once the developer disk image is mounted


class DeviceError(RuntimeError):
    pass


def run(fn, *args):
    """Run an async function on the event loop pymobiledevice3 needs. On Windows that is a
    selector loop: the default (proactor) loop breaks some of its connections (its issue
    #1217) and never receives mDNS replies, so the phone isn't found on Wi-Fi (measured)."""
    import anyio
    options = {"loop_factory": asyncio.SelectorEventLoop} if sys.platform == "win32" else None
    return anyio.run(fn, *args, backend_options=options)


class Device:
    def __init__(self, udid: str | None = None, host: str | None = None, port: int | None = None, log=print):
        state = store.load_state()
        self.udid = udid or state.get("udid")
        self.host, self.port = host, port or (state.get("pairing_port") if host else None)
        self.log = log
        self.tunnel = None
        self.rsd = None
        self.via = None
        self.runner = None  # the XCUITest task that keeps WebDriverAgent alive

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
            udid = self.udid

            async def provider(serial, autopair, remotepairing_fallback=True):
                from pymobiledevice3.remote.tunnel_service import get_remote_pairing_tunnel_services
                services = await get_remote_pairing_tunnel_services(udid=udid)
                if not services:
                    raise DeviceError("no phone on USB, and none paired on this network (same Wi-Fi, unlocked, "
                                      "Developer Mode on, `tapwright setup` done over USB once)")
                for extra in services[1:]:
                    await extra.close()
                return services[0], None
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
            return
        async with DvtProvider(self.rsd) as dvt, DeviceInfo(dvt) as info, ProcessControl(dvt) as control:
            for p in await info.proclist():
                if p.get("bundleIdentifier") == runner_id:
                    await control.kill(p["pid"])
        cfg = await TestConfig.create_for(self.rsd, runner_bundle_id=runner_id)
        self.runner = asyncio.create_task(XCUITestService(self.rsd).run(cfg), name="webdriveragent")
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if self.runner.done():
                exc = self.runner.exception()
                raise DeviceError(f"WebDriverAgent stopped while starting: {exc}")
            if await self.wda_alive():
                self.log("WebDriverAgent is running")
                return
            await asyncio.sleep(0.5)
        raise DeviceError("WebDriverAgent didn't answer within a minute (is it installed? `tapwright doctor`)")

    async def wda_alive(self) -> bool:
        try:
            await self.http("GET", "/status", timeout=3)
            return True
        except Exception:
            return False

    async def http(self, method: str, path: str, payload: dict | None = None, timeout: float = 30.0) -> dict:
        """One request to WebDriverAgent, through the tunnel (works the same over USB and network)."""
        return await asyncio.wait_for(self._http(method, path, payload), timeout)

    async def _http(self, method: str, path: str, payload: dict | None) -> dict:
        body = json.dumps(payload).encode() if payload is not None else b""
        head = [f"{method} {path} HTTP/1.1", "Host: localhost", "Connection: close",
                f"Content-Length: {len(body)}"] + (["Content-Type: application/json"] if payload is not None else [])
        conn = await self.rsd.create_service_connection(WDA_PORT)
        try:
            await conn.sendall(("\r\n".join(head) + "\r\n\r\n").encode() + body)
            raw = b""
            while b"\r\n\r\n" not in raw:
                chunk = await conn.recv_any(65536)
                if not chunk:
                    break
                raw += chunk
            header, _, rest = raw.partition(b"\r\n\r\n")
            lines = header.decode("iso-8859-1").split("\r\n")
            status = int(lines[0].split(" ", 2)[1])
            length = next((int(ln.split(":", 1)[1]) for ln in lines[1:] if ln.lower().startswith("content-length:")),
                          None)
            if length is not None:
                if length > len(rest):
                    rest += await conn.recvall(length - len(rest))
            else:
                while chunk := await conn.recv_any(65536):
                    rest += chunk
        finally:
            await conn.close()
        data = json.loads(rest or b"{}")
        if status >= 400 or (isinstance(data.get("value"), dict) and data["value"].get("error")):
            err = data.get("value", {}) if isinstance(data.get("value"), dict) else {}
            raise DeviceError(f"WebDriverAgent: {err.get('error', status)}: {err.get('message', '')[:300]}")
        return data

    # ---- apps ----
    async def user_apps(self) -> list[dict]:
        """Apps you installed (App Store, TestFlight, development builds)."""
        from pymobiledevice3.services.installation_proxy import InstallationProxyService
        async with InstallationProxyService(lockdown=self.rsd) as proxy:
            apps = await proxy.get_apps(application_type="User")
        return sorted(({"bundle_id": bid, "name": info.get("CFBundleDisplayName") or info.get("CFBundleName", ""),
                        "version": info.get("CFBundleShortVersionString", ""),
                        "build": info.get("CFBundleVersion", "")} for bid, info in apps.items()),
                      key=lambda a: a["name"].lower())

    async def close(self) -> None:
        if self.runner:
            self.runner.cancel()
        if self.tunnel:
            await self.tunnel.aclose()

"""1.9.1: how connect() hands a network-reached phone to pymobiledevice3's user-space tunnel, on both of its contracts.

Before 11.57 the tunnel always calls the module's `_create_no_root_tunnel_provider`, which returns a
`(service, lockdown)` tuple; from 11.57 it takes a `provider_factory` returning a `TunnelProvider` and reads
`.service`. 1.9.0 replaced the module's provider with a tuple-returning one and never put it back, so on 11.57+ every
network open failed, and so did every USB open after one ("'tuple' object has no attribute 'service'", 2026-11-20)."""
import asyncio
import sys
import types
from typing import NamedTuple

import pytest

import pymobiledevice3.remote
import pymobiledevice3.remote.tunnel_service as tunnel_service
import pymobiledevice3.usbmux as usbmux

from tapwright import device as device_module
from tapwright import store
from tapwright.device import Device, DeviceError

UDID = "00008110-000A11112222801E"


class Rsd:
    udid, product_type, product_version = UDID, "iPhone14,5", "26.1"


class Phone:
    def __init__(self, serial=UDID):
        self.connection_type, self.serial = "USB", serial


def _new_contract():
    """pymobiledevice3 11.57+ (11.61 as installed for 1.9.0)."""
    mod = types.ModuleType("pymobiledevice3.remote.userspace_tunnel")
    mod.opened = []

    class TunnelProvider(NamedTuple):
        service: object
        lockdown: object = None

    async def library_provider(serial, autopair, remotepairing_fallback=True):
        return TunnelProvider("usb-service", "usb-lockdown")

    class UserspaceRsdTunnel:
        def __init__(self, serial=None, autopair=True, remotepairing_fallback=True, provider_factory=None,
                     remotepairing=False):
            self.serial, self.provider_factory = serial, provider_factory

        async def aopen(self):
            if self.provider_factory is not None:
                provider = await self.provider_factory()
            else:
                provider = await mod._create_no_root_tunnel_provider(self.serial, True, True)
            mod.opened.append(provider.service)  # 1.9.0's tuple failed right here
            return Rsd()

        async def aclose(self):
            pass

    mod.TunnelProvider, mod.UserspaceRsdTunnel = TunnelProvider, UserspaceRsdTunnel
    mod._create_no_root_tunnel_provider = library_provider
    mod.library_provider = library_provider
    return mod


def _old_contract():
    """pymobiledevice3 before 11.57 (11.50 to 11.56): no TunnelProvider, the module's provider returns a tuple."""
    mod = types.ModuleType("pymobiledevice3.remote.userspace_tunnel")
    mod.opened = []

    async def library_provider(serial, autopair, remotepairing_fallback=True):
        return "usb-service", "usb-lockdown"

    class UserspaceRsdTunnel:
        def __init__(self, serial=None, autopair=True, remotepairing_fallback=True):
            self.serial = serial

        async def aopen(self):
            service, lockdown = await mod._create_no_root_tunnel_provider(self.serial, True, True)
            mod.opened.append(service)
            return Rsd()

        async def aclose(self):
            pass

    mod.UserspaceRsdTunnel = UserspaceRsdTunnel
    mod._create_no_root_tunnel_provider = library_provider
    mod.library_provider = library_provider
    return mod


@pytest.fixture
def world(monkeypatch):
    """A phone that is on USB or not (`usb`), on the local network, and a direct address; the state file faked."""
    monkeypatch.setattr(store, "load_state", lambda: {})
    monkeypatch.setattr(store, "save_state", lambda **kw: None)
    state = types.SimpleNamespace(usb=False)

    async def list_devices():
        return [Phone()] if state.usb else []

    async def mdns(udid=None, **kw):
        return ["wifi-service"]

    async def direct(identifier, host, port):
        return f"direct-service {host}:{port}"
    monkeypatch.setattr(usbmux, "list_devices", list_devices)
    monkeypatch.setattr(tunnel_service, "get_remote_pairing_tunnel_services", mdns)
    monkeypatch.setattr(tunnel_service, "create_core_device_tunnel_service_using_remotepairing", direct, raising=False)
    monkeypatch.setattr(Device, "_pairing_identifier", lambda self: UDID)

    def use(mod):
        monkeypatch.setitem(sys.modules, "pymobiledevice3.remote.userspace_tunnel", mod)
        monkeypatch.setattr(pymobiledevice3.remote, "userspace_tunnel", mod, raising=False)
    state.use = use
    return state


def _connect(**kw):
    d = Device(log=lambda *a: None, **kw)
    asyncio.run(d.connect())
    return d


@pytest.mark.parametrize("contract", [_new_contract, _old_contract], ids=["11.57+", "before 11.57"])
def test_usb_after_a_network_fallback_uses_the_librarys_provider(world, contract):
    """The 2026-11-20 failure: the phone off USB (local network), then back on USB in the same process."""
    mod = contract()
    world.use(mod)
    d = _connect()
    assert d.via == "local network" and mod.opened == ["wifi-service"]
    assert mod._create_no_root_tunnel_provider is mod.library_provider  # left as found
    world.usb = True
    d = _connect()
    assert d.via == "USB" and mod.opened == ["wifi-service", "usb-service"]
    assert mod._create_no_root_tunnel_provider is mod.library_provider


@pytest.mark.parametrize("contract", [_new_contract, _old_contract], ids=["11.57+", "before 11.57"])
def test_direct_address_then_usb(world, contract):
    mod = contract()
    world.use(mod)
    world.usb = True  # --host wins even with the phone on USB
    d = _connect(host="100.64.0.7", port=49152)
    assert d.via == "network 100.64.0.7:49152" and mod.opened == ["direct-service 100.64.0.7:49152"]
    assert mod._create_no_root_tunnel_provider is mod.library_provider
    d = _connect()
    assert d.via == "USB" and mod.opened[-1] == "usb-service"


def test_network_path_gives_a_tunnel_provider_on_the_new_contract(world):
    mod = _new_contract()
    world.use(mod)
    d = _connect()
    provider = asyncio.run(d.tunnel.provider_factory())
    assert isinstance(provider, mod.TunnelProvider)
    assert provider.service == "wifi-service" and provider.lockdown is None


def test_network_path_gives_a_tuple_on_the_old_contract(world):
    mod = _old_contract()
    world.use(mod)
    seen = []
    library = mod.UserspaceRsdTunnel.aopen

    async def aopen(self):
        seen.append(await mod._create_no_root_tunnel_provider(self.serial, True, True))
        return await library(self)
    mod.UserspaceRsdTunnel.aopen = aopen
    _connect()
    assert seen == [("wifi-service", None)]
    assert mod._create_no_root_tunnel_provider is mod.library_provider


def test_the_old_contracts_provider_is_put_back_when_the_open_fails(world):
    mod = _old_contract()
    world.use(mod)

    async def fail(self):
        await mod._create_no_root_tunnel_provider(self.serial, True, True)
        raise OSError("the phone went away")
    mod.UserspaceRsdTunnel.aopen = fail
    with pytest.raises(DeviceError, match="local network: the phone went away"):
        _connect()
    assert mod._create_no_root_tunnel_provider is mod.library_provider


def test_contract_detection():
    assert device_module._takes_factory(_new_contract())
    assert not device_module._takes_factory(_old_contract())
    half = _old_contract()
    half.TunnelProvider = tuple  # a TunnelProvider without the factory argument is still the old contract
    assert not device_module._takes_factory(half)


def test_the_installed_library_is_one_of_the_two_contracts():
    """Whatever pymobiledevice3 this runs on: either it takes a factory and has TunnelProvider, or it has the
    module provider the old path replaces for one open."""
    from pymobiledevice3.remote import userspace_tunnel
    if device_module._takes_factory(userspace_tunnel):
        assert {"service", "lockdown"} <= set(userspace_tunnel.TunnelProvider._fields)
    else:
        assert callable(userspace_tunnel._create_no_root_tunnel_provider)


class Svc:
    """A network tunnel service that says how far an open got with it."""

    def __init__(self, name, closed):
        self.name, self.closed = name, closed

    def start_tcp_tunnel(self):
        raise RuntimeError(f"start_tcp_tunnel reached with {self.name}")

    async def close(self):
        self.closed.append(self.name)


def test_two_network_opens_at_once_on_the_old_contract_each_get_their_own(world, monkeypatch):
    """(review 1.9.1 L1) Before 11.57 the provider is swapped per open. Without one lock over swap, open and undo, a
    second open took the first one's provider for its original, ran on the library's own (USB) provider, and left the
    first one's in place."""
    mod = _old_contract()
    world.use(mod)
    library = mod.UserspaceRsdTunnel.aopen
    locks = {}

    async def aopen(self):  # the library serializes opens on one lock, as the real one does
        lock = locks.setdefault("lifecycle", asyncio.Lock())
        async with lock:
            return await library(self)
    mod.UserspaceRsdTunnel.aopen = aopen

    async def scenario():
        gate = asyncio.Event()

        async def direct(identifier, host, port):
            await gate.wait()
            return f"direct {host}"
        monkeypatch.setattr(tunnel_service, "create_core_device_tunnel_service_using_remotepairing", direct)
        a = asyncio.ensure_future(Device(log=lambda *x: None, host="a.host", port=1).connect())
        b = asyncio.ensure_future(Device(log=lambda *x: None, host="b.host", port=1).connect())
        for _ in range(20):
            await asyncio.sleep(0)
        gate.set()
        await asyncio.gather(a, b)
    asyncio.run(scenario())
    assert sorted(mod.opened) == ["direct a.host", "direct b.host"]
    assert mod._create_no_root_tunnel_provider is mod.library_provider


@pytest.mark.parametrize("contract", [_new_contract, _old_contract], ids=["11.57+", "before 11.57"])
def test_a_found_service_no_tunnel_took_is_closed(world, monkeypatch, contract):
    """(review 1.9.1 L2) An open that fails before it asks for the service (a tunnel still active in the process)
    left the service found on the local network open, one more with every retry."""
    mod = contract()
    world.use(mod)
    closed = []

    async def mdns(udid=None, **kw):
        return [Svc("wifi", closed)]
    monkeypatch.setattr(tunnel_service, "get_remote_pairing_tunnel_services", mdns)

    async def busy(self):
        raise RuntimeError("A userspace tunnel is already active in this process")
    mod.UserspaceRsdTunnel.aopen = busy
    with pytest.raises(DeviceError, match="already active"):
        _connect()
    assert closed == ["wifi"]


def test_a_found_service_the_tunnel_took_is_left_to_the_tunnel(world, monkeypatch):
    """The tunnel closes a service it took (its exit stack): connect() doesn't close it a second time."""
    mod = _new_contract()
    world.use(mod)
    closed = []

    async def mdns(udid=None, **kw):
        return [Svc("wifi", closed)]
    monkeypatch.setattr(tunnel_service, "get_remote_pairing_tunnel_services", mdns)

    async def takes_then_fails(self):
        await self.provider_factory()
        raise RuntimeError("handshake failed")
    mod.UserspaceRsdTunnel.aopen = takes_then_fails
    with pytest.raises(DeviceError, match="handshake failed"):
        _connect()
    assert closed == []


def test_against_the_installed_library(world, monkeypatch):
    """(review 1.9.1 L3) The real UserspaceRsdTunnel of whatever pymobiledevice3 is installed, with only the transports
    faked: a fake that kept an old contract is how 1.9.0 shipped. Network opens reach the service, USB opens reach the
    library's own provider, the module's provider is left as found, and a cancelled open puts it back too."""
    try:
        from pymobiledevice3.remote import userspace_tunnel
    except Exception as e:  # the tunnel's own dependencies missing here
        pytest.skip(f"pymobiledevice3's user-space tunnel doesn't import: {e}")
    if (not hasattr(userspace_tunnel, "create_using_usbmux") or getattr(userspace_tunnel, "_active_tunnel", None)
            is not None or not hasattr(userspace_tunnel, "_create_no_root_tunnel_provider")):
        pytest.skip("this pymobiledevice3's tunnel can't be driven without a phone")
    world.use(userspace_tunnel)
    monkeypatch.setattr(tunnel_service, "USE_USERSPACE_TUNNEL", getattr(tunnel_service, "USE_USERSPACE_TUNNEL", False),
                        raising=False)
    original = userspace_tunnel._create_no_root_tunnel_provider
    closed = []

    async def mdns(udid=None, **kw):
        return [Svc("wifi", closed)]
    gate = {}

    async def direct(identifier, host, port):
        if "event" in gate:
            await gate["event"].wait()
        return Svc(f"direct {host}:{port}", closed)

    async def usb_lockdown(serial=None, autopair=True, **kw):
        raise RuntimeError("the library's USB provider was reached")
    monkeypatch.setattr(tunnel_service, "get_remote_pairing_tunnel_services", mdns)
    monkeypatch.setattr(tunnel_service, "create_core_device_tunnel_service_using_remotepairing", direct,
                        raising=False)
    monkeypatch.setattr(userspace_tunnel, "create_using_usbmux", usb_lockdown)

    def fails(match, **kw):
        with pytest.raises(DeviceError, match=match):
            _connect(**kw)
        assert userspace_tunnel._create_no_root_tunnel_provider is original
        assert userspace_tunnel._active_tunnel is None

    fails("over local network: start_tcp_tunnel reached with wifi")
    world.usb = True
    fails("over USB: the library's USB provider was reached")  # the 2026-11-20 00:49 sequence
    fails("start_tcp_tunnel reached with direct 100.64.0.7:49152", host="100.64.0.7", port=49152)
    fails("over USB: the library's USB provider was reached")
    assert closed == ["wifi", "direct 100.64.0.7:49152"]  # the tunnel closed what it took

    async def cancelled():
        gate["event"] = asyncio.Event()
        task = asyncio.ensure_future(Device(log=lambda *a: None, host="100.64.0.7", port=49152).connect())
        for _ in range(20):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(cancelled())
    assert userspace_tunnel._create_no_root_tunnel_provider is original
    assert userspace_tunnel._active_tunnel is None

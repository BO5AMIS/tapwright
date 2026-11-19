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

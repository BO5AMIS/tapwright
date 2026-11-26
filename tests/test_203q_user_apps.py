"""(2.0.3, brief Q fix 5) `user_apps` asks the installation proxy for the six attributes it uses, not every attribute of
every app (megabytes of plist parsed on the event loop)."""
import asyncio
import sys
import types

from tapwright import device as device_module


class Proxy:
    asked: list = []

    def __init__(self, lockdown=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def lookup(self, options):
        Proxy.asked.append(options)
        return {"com.example.b": {"CFBundleName": "Bee", "CFBundleShortVersionString": "2", "CFBundleVersion": "9",
                                  "CFBundleExecutable": "Bee"},
                "com.example.a": {"CFBundleDisplayName": "Ant", "CFBundleExecutable": "Ant"}}


def test_user_apps_asks_only_what_it_uses(monkeypatch):
    fake = types.ModuleType("pymobiledevice3.services.installation_proxy")
    fake.InstallationProxyService = Proxy
    monkeypatch.setitem(sys.modules, "pymobiledevice3.services.installation_proxy", fake)
    d = device_module.Device.__new__(device_module.Device)
    d.rsd = "tunnel"
    apps = asyncio.run(d.user_apps())
    assert Proxy.asked == [{"ApplicationType": "User", "ReturnAttributes": list(device_module.APP_ATTRIBUTES)}]
    assert [a["name"] for a in apps] == ["Ant", "Bee"]
    assert apps[1] == {"bundle_id": "com.example.b", "name": "Bee", "version": "2", "build": "9", "executable": "Bee"}

"""Explicit, Cancel-only system recovery through production Session HTTP dispatch, with a synthetic phone."""
import asyncio
import dataclasses
import types

import pytest

from fakes import button, el
from test_daemon import finish, make, run, started
from tapwright import daemon as daemon_module, policy, ui
from tapwright.control import Refused
from tapwright.device import ConnectionLost, DeviceError
from tapwright.session import Session, SPRINGBOARD


APP = "com.example.allowed"
SCOPE = daemon_module.Scope((APP,))
PRIVATE = "Private account user@example.invalid"


def synthetic():
    d = make()
    s = d.session
    s.app = s.tree_app = ("Home screen", SPRINGBOARD)
    s.foreground, s.running_apps = SPRINGBOARD, [APP]
    s.last_alert = ui.Alert("Account password reminder", ("Edit Settings", "Cancel"), (50, 300, 330, 190), PRIVATE)
    cancel = button("e2", "Cancel", "cancel", rect=(225, 420, 140, 44))
    cancel.in_alert = True
    settings = button("e1", "Edit Settings", "settings", rect=(65, 420, 140, 44))
    settings.in_alert = True
    s.tree = [settings, cancel]
    s.input_point, s.before_input = None, None
    s.dispatched, s.cancel_reads = [], 0
    for method in ("alert", "_call", "_timed", "_touching"):
        setattr(s, method, types.MethodType(getattr(Session, method), s))

    async def peek():
        s.cancel_reads += 1
        return ui.Tree(list(s.tree), alert=s.last_alert, app=s.tree_app)

    async def foreground():
        return s.running_apps

    async def http(method, path, payload=None, timeout=30):
        if method == "POST":
            s.dispatched.append((path, payload))
        if getattr(s, "cancel_error", None):
            raise s.cancel_error
        return {"value": None}

    s.peek_tree, s.device.foreground_apps, s.device.http = peek, foreground, http
    return d, s


async def request(d, *, scope=SCOPE, **kwargs):
    return await d.run_tool("agent", "alert", {"action": "cancel", "screenshot": True}, scope=scope, **kwargs)

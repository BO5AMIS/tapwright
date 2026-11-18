"""Final-review regressions through real Session dispatch, with a synthetic runner only."""
import dataclasses
import json
import types

import pytest

from fakes import button, el
from test_daemon import finish, make, run, started
from test_review_17_c1 import APP, daemon_with, grant
from test_runner_v5 import node
from tapwright import daemon as daemon_module, session as session_module, ui
from tapwright.control import Refused
from tapwright.session import Session


def real_dispatch(s, *, change=None, typing=False, delay="banner"):
    """Use the real banner guard, tap, fast route and HTTP dispatch; fake only phone responses."""
    s.sid, s.turn, s.fast_tap, s.fast_drag, s.tw_version = "synthetic", 0, True, True, 5
    s.typed_into, s.retyped = None, False
    s.typed_place, s._placed_proof = None, None
    s.dispatched, s.banner_checks = [], 0
    s.input_point, s.before_input = None, None
    for name in ("_target", "_touching", "_keys", "_clear_of_banner", "tap", "_fast_tap", "_fast_routes", "_call", "_timed"):
        setattr(s, name, types.MethodType(getattr(Session, name), s))
    if typing:
        s.type_text = types.MethodType(Session.type_text, s)
    old_front = s.foreground_bundle_id
    checking = [False]
    clear = s._clear_of_banner

    async def guarded(at):
        checking[0] = True
        try:
            await clear(at)
        finally:
            checking[0] = False

    async def front():
        if checking[0] and change is not None and delay == "banner":
            s.banner_checks += 1
            if s.banner_checks == 1:
                return session_module.SPRINGBOARD
        return await old_front()

    async def foreground_apps():
        change()
        return [APP]

    async def http(method, path, payload=None, timeout=30):
        if method == "POST":
            s.dispatched.append((path, payload))
        return {"value": None}

    async def has_focus(field, **where):
        s._placed_proof = "input"  # (proven by its place, as the real one does when it reads the tree)
        return "input"

    async def active():
        return "input"

    async def attribute(element, name):
        return "New draft"

    s._clear_of_banner, s.foreground_bundle_id = guarded, front
    s.device.foreground_apps, s.device.http = foreground_apps, http
    s._has_focus, s._active_element, s._attribute = has_focus, active, attribute
    if change is not None and delay == "runner_probe":
        routes = s._fast_routes

        async def probing(strict=False):
            change()
            return await routes(strict)
        s._fast_routes = probing
    return s






def test_m1_real_typing_uses_exact_checked_focusing_point():
    async def go():
        d = make()
        field = el("e1", "input", "Message", "message", value="Old draft", rect=(20, 300, 390, 44))
        post = button("e2", "Post", "post", rect=(350, 300, 60, 44))
        d.session.tree = [field, post]
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session, typing=True)

            async def hit_at(x, y):
                s.hit_asks.append((x, y))
                e = post if x >= 350 else field
                return ui.Hit(e.type, e.name, e.label, e.rect, APP)
            s.hit_at = hit_at
            await d.run_tool("a", "type_text", {"target": {"id": "message"}, "text": "New draft", "look": False})
            taps = [p for path, p in s.dispatched if path.endswith("/wda/tw/tap")]
            assert len(taps) == 1
            assert (taps[0]["x"], taps[0]["y"]) == field.center
            assert not s.hit_asks  # input uses resolved geometry, not accessibility hit identity
        finally:
            await finish(stop, task)
    run(go)

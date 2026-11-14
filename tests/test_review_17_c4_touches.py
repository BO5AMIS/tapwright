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
    s.dispatched, s.banner_checks = [], 0
    s.input_point, s.before_input = None, None
    for name in ("_target", "_touching", "_clear_of_banner", "tap", "_fast_tap", "_fast_routes", "_call", "_timed"):
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

    async def has_focus(field):
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


@pytest.mark.parametrize("drafts", [False, True])
@pytest.mark.parametrize("recipient", ["ancestor", "inside"])
def test_h1_actionable_related_hit_is_never_authorized_as_harmless_target(drafts, recipient):
    async def go():
        d = make()
        child = node("StaticText", (30, 300, 200, 40), n="hint", l="Ready")
        parent = node("Button", (20, 290, 300, 60), [child], n="post", l="Post")
        if recipient == "inside":
            child = node("Button", (30, 300, 200, 40), n="post", l="Post")
            parent = node("Other", (20, 290, 300, 60), [child], n="hint", l="Ready")
        source = ui.xml_of_tree(node("Application", (0, 0, 400, 800), [
            node("Window", (0, 0, 400, 800), [parent])], n="Example", b=APP))
        d.session.tree = ui.read(source, d.session.screen).items
        d.session.last_source = source
        hit = parent if recipient == "ancestor" else child
        d.session.hit_answer = ui.Hit(hit["t"], hit["n"], hit["l"], tuple(hit["r"]), APP)
        stop, task = await started(d)
        token = daemon_module._DRAFTS_ROUTE.set(drafts)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            with pytest.raises(Refused) as refused:
                await d.run_tool("a", "tap", {"target": {"id": "hint"}, "look": False})
            assert refused.value.code in ("not_actionable", "not_allowed")
            assert "tap" not in d.session.calls
        finally:
            daemon_module._DRAFTS_ROUTE.reset(token)
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("change_draft", [True, False])
@pytest.mark.parametrize("delay", ["banner", "runner_probe", "touch_hook"])
def test_h2_banner_wait_revalidates_draft_or_claimed_grant(tmp_path, monkeypatch, change_draft, delay):
    monkeypatch.setattr(session_module, "BANNER_POLL", 0.001)

    async def go():
        d, rules = daemon_with(tmp_path, {"version": 2, "confirm": ["Post"], "block": [],
                                         "grants": [] if change_draft else [grant(words=["Post"])]})
        d.session.tree = [button("e1", "Post", "post", rect=(10, 55, 150, 40)),
                          el("e2", "input", "Message", "message", value="Approved draft A", rect=(10, 250, 300, 40))]
        stop, task = await started(d)
        token = daemon_module._DRAFTS_ROUTE.set(change_draft)
        try:
            await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
            if change_draft:
                with pytest.raises(Refused):
                    await d.run_tool("a", "tap", {"target": {"id": "post"}, "look": False}, policy=rules)
                [asked] = list(d._confirms.values())
                await d.viewer_message({"type": "confirm_answer", "id": asked.id, "allow": True})

            def change():
                if change_draft:
                    d.session.tree[1] = dataclasses.replace(d.session.tree[1], value="Unapproved draft B")
                else:
                    path = tmp_path / "policy.json"
                    data = json.loads(path.read_text("utf-8"))
                    data["grants"][0]["revoked"] = True
                    path.write_text(json.dumps(data), encoding="utf-8")

            s = real_dispatch(d.session, change=change, delay=delay)
            if delay == "touch_hook":
                async def notify(ms):
                    change()
                monkeypatch.setattr(d._agent_touches, "notify", notify)
            with pytest.raises(Refused) as refused:
                await d.run_tool("a", "tap", {"target": {"id": "post"}, "look": False}, policy=rules)
            assert refused.value.code == "not_allowed"
            assert not s.dispatched
            assert delay != "banner" or s.banner_checks >= 2
            assert getattr(s, "before_input", None) is None
        finally:
            daemon_module._DRAFTS_ROUTE.reset(token)
            await finish(stop, task)
    run(go)


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
            assert (taps[0]["x"], taps[0]["y"]) in s.hit_asks
        finally:
            await finish(stop, task)
    run(go)

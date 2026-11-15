"""1.7.5 contract: authorization belongs to the agent, execution ownership to Tapwright.

Synthetic dispatch tests do not establish hardware usability. The reported JLD
geometry is reproduced without inventing successful exact-Button hit responses.
"""
import pytest
import types

from fakes import DescribingSession, button
from test_daemon import finish, make, run, started
from test_171_button_content import SHAPES, setup
from test_review_17_c4_touches import real_dispatch
from tapwright import policy, ui
from tapwright.control import Refused
from tapwright.daemon import Scope
from tapwright.session import Session


@pytest.mark.parametrize("shape", SHAPES, ids=[s[0] for s in SHAPES])
@pytest.mark.parametrize("placement", ["inside", "sibling", "omitted"])
def test_normal_buttons_dispatch_once_without_hit_identity_proof(shape, placement):
    async def go():
        d, target, _, _ = setup(shape, placement=placement)
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            # Even a broken hit endpoint must not participate in ordinary targeting.
            async def unavailable(*args):
                raise AssertionError("normal input must not ask for accessibility hit proof")
            s.hit_at = unavailable
            answer = await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False})
            assert answer["text"].startswith("Tapped")
            [(_, payload)] = s.dispatched
            assert (payload["x"], payload["y"]) == target.center
            assert s.before_input is None
        finally:
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("scope", [None, Scope(()), Scope(("com.unrelated.app",))])
@pytest.mark.parametrize("label", ["Continue", "Delete", "Buy", "Send", "Post", "Sign out", "Erase All Content"])
def test_legacy_scope_policy_and_dialog_never_gate_requested_input(scope, label):
    async def go():
        d = make()
        d.session.tree = [button("e1", label, "requested")]
        stop, task = await started(d)
        try:
            rules = policy.Policy(block=(label,), confirm=(label,))
            await d.run_tool("a", "look", {"screenshot": False}, scope=scope, policy=rules)
            s = real_dispatch(d.session)
            answer = await d.run_tool("a", "tap", {"target": {"id": "requested"}, "look": False},
                                      scope=scope, policy=rules, dialog=True)
            assert answer["text"].startswith("Tapped") and len(s.dispatched) == 1
            assert not d.open_confirms()
        finally:
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("pause_at_dispatch", [False, True])
def test_pause_still_prevents_input(pause_at_dispatch, monkeypatch):
    async def go():
        d, _, _, _ = setup()
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            if pause_at_dispatch:
                async def notify(ms):
                    d.control.paused = True
                monkeypatch.setattr(d._agent_touches, "notify", notify)
            else:
                d.control.paused = True
            with pytest.raises(Refused) as error:
                await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False})
            assert error.value.code == "paused" and not s.dispatched
        finally:
            await finish(stop, task)
    run(go)


def test_dry_run_never_dispatches_or_requests_confirmation():
    async def go():
        d, _, _, _ = setup(SHAPES[2], placement="sibling")
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            result = await d.run_tool("a", "tap", {"target": {"id": "control"}, "dry_run": True})
            assert "Would tap" in result["text"]
            assert not s.dispatched and not d.open_confirms()
        finally:
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("change, code", [("pause", "paused"), ("person", "busy")])
def test_ownership_is_checked_for_later_requests_of_the_same_action(change, code):
    async def go():
        d = make()
        stop, task = await started(d)
        try:
            s = real_dispatch(d.session)
            async def multi_request(view, args):
                await s._call("POST", "/wda/homescreen", {})
                if change == "pause":
                    d.control.paused = True
                else:
                    d.control.person_generation += 1
                await s._call("POST", "/session/{sid}/wda/keys", {"value": ["never sent"]})
            d._handlers["press_button"] = multi_request
            with pytest.raises(Refused) as error:
                await d.run_tool("a", "press_button", {"name": "home", "look": False})
            assert error.value.code == code
            assert s.dispatched == [("/wda/homescreen", {})]
            assert s.execution_guard is None
        finally:
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("action", ["text", "accept", "dismiss", "cancel"])
def test_unrelated_alert_is_available_to_normal_calls_and_batches(action):
    async def go():
        d = make()
        d.session.foreground = "com.apple.springboard"
        d.session.app = d.session.tree_app = ("Home screen", "com.apple.springboard")
        d.session.last_alert = ui.Alert("Unrelated reminder", ("Cancel", "OK"), (50, 300, 300, 190))
        stop, task = await started(d)
        try:
            answer = await d.run_tool("a", "alert", {"action": action, "look": False}, scope=Scope(()))
            assert "alert" in answer["text"] and "alert" in d.session.calls
            answer = await d.run_tool("a", "run", {"steps": [{"tool": "alert", "action": action}], "look": False})
            assert "stopped" not in answer["text"].lower()
        finally:
            await finish(stop, task)
    run(go)


def test_home_and_other_apps_are_visible_without_allowlist():
    async def go():
        for app in [("Home screen", "com.apple.springboard"), ("Photos", "com.apple.mobileslideshow")]:
            d = make(DescribingSession())
            d.session.foreground = app[1]
            d.session.app = d.session.tree_app = app
            d.session.tree = [button("e1", "Requested screen", "visible")]
            stop, task = await started(d)
            try:
                result = await d.run_tool("a", "look", {"screenshot": True}, scope=Scope(()))
                assert "Requested screen" in result["text"] and result["image"]
            finally:
                await finish(stop, task)
    run(go)


def test_alert_dry_run_sends_no_input():
    async def go():
        d = make()
        stop, task = await started(d)
        try:
            answer = await d.run_tool("a", "alert", {"action": "dismiss", "name": "Not Now", "dry_run": True})
            assert "Would dismiss" in answer["text"] and "alert" not in d.session.calls
        finally:
            await finish(stop, task)
    run(go)


def test_browser_cancel_is_a_normal_element_tap():
    from test_guessed_button_cover import navbar

    async def go():
        d, target, *_ = navbar()
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            result = await d.run_tool("a", "tap", {"target": {"ref": target.ref}, "look": False})
            assert result["text"].startswith("Tapped")
            [(_, payload)] = s.dispatched
            assert (payload["x"], payload["y"]) == target.center
            assert not s.hit_asks
        finally:
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("action,name,path", [
    ("accept", "Allow", "accept"), ("dismiss", "Not Now", "dismiss"),
    ("cancel", "Annuler", "dismiss"), ("cancel", None, "dismiss"),
])
def test_named_alert_dispatch_uses_exact_requested_label(action, name, path):
    async def go():
        d = make()
        stop, task = await started(d)
        try:
            s = real_dispatch(d.session)
            s.alert = types.MethodType(Session.alert, s)
            args = {"action": action, "look": False, **({"name": name} if name else {})}
            await d.run_tool("a", "alert", args)
            [(route, payload)] = s.dispatched
            assert route.endswith("/alert/" + path)
            assert payload == {"name": name or "Cancel"}
        finally:
            await finish(stop, task)
    run(go)


def test_http_legacy_headers_and_daemon_policy_do_not_limit_access():
    from starlette.testclient import TestClient
    from test_ceiling import BARE, app_for
    from tapwright.ceiling import Ceiling

    app, seen = app_for(Ceiling(apps=("com.old.scope",), policy=policy.Policy(block=("Delete",))))
    with TestClient(app) as client:
        response = client.post("/api/look", headers={**BARE, "X-Tapwright-Apps": "com.other.app",
                               "X-Tapwright-Policy": "legacy metadata"}, json={})
        assert response.status_code == 200
        assert seen.calls[-1]["scope"] is None and seen.calls[-1]["policy"] is None
        assert client.post("/api/look", json={}).status_code == 401


@pytest.mark.parametrize("change,code", [("pause", "paused"), ("person", "busy")])
def test_second_tap_refusal_invalidates_first_taps_refs(change, code, monkeypatch):
    from tapwright import daemon as daemon_module

    async def go():
        d, target, _, _ = setup()
        stop, task = await started(d)
        token = daemon_module._DOUBLE.set(True)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            before = d.control.generation
            notices = 0

            async def notify(ms):
                nonlocal notices
                notices += 1
                if notices == 2:
                    if change == "pause":
                        d.control.paused = True
                    else:
                        d.control.person_generation += 1
            monkeypatch.setattr(d._agent_touches, "notify", notify)
            with pytest.raises(Refused) as error:
                await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False})
            assert error.value.code == code
            assert len(s.dispatched) == 1
            assert d.control.generation > before
            assert s.execution_guard is None
        finally:
            daemon_module._DOUBLE.reset(token)
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("change", ["pause_after_clear", "person_before_retype"])
def test_automatic_retype_retains_original_turn_ownership(change):
    from fakes import el
    from test_daemon import looked

    async def go():
        d = make()
        stop, task = await started(d)
        try:
            field = el("e1", "input", "Email", "email", value="a", rect=(25, 450, 380, 50))
            await looked(d, [field])
            s = real_dispatch(d.session)
            s.retype = types.MethodType(Session.retype, s)
            s.focused = "email"
            original_http = s.device.http

            async def http(method, path, payload=None, timeout=30):
                result = await original_http(method, path, payload, timeout)
                if path.endswith("/clear"):
                    d.control.paused = True
                return result

            async def wrong(result, text):
                if change == "person_before_retype":
                    d.control.person_generation += 1
                return True

            async def landed(*args):
                return None

            s.device.http, d._typed_wrong, d._typed_landed = http, wrong, landed
            with pytest.raises(Refused) as error:
                await d.run_tool("a", "type_text", {"target": {"id": "email"}, "text": "abc"})
            assert error.value.code == ("paused" if change == "pause_after_clear" else "busy")
            assert [path.rsplit("/", 1)[-1] for path, _ in s.dispatched] == (
                ["clear"] if change == "pause_after_clear" else [])
            assert s.execution_guard is None
        finally:
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("native_refuses", [False, True])
def test_native_close_preserves_pause_refusal_without_wda_fallback(native_refuses):
    from tapwright import device as device_module

    async def go():
        d = make()
        stop, task = await started(d)
        try:
            async def guard():
                raise Refused("paused", "paused before kill")

            async def kill(bundle):
                raise Refused("busy", "person took over during native setup")

            if native_refuses:
                d.session.device.kill_app = kill
            else:
                d.session.execution_guard = guard
            with pytest.raises(Refused) as error:
                await d._end_app("com.example.app")
            assert error.value.code == ("busy" if native_refuses else "paused")
            assert not d.session.device.killed and not d.session.closed
            assert device_module.BEFORE_SEND.get() is None
        finally:
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("change,code", [("pause", "paused"), ("person", "busy")])
def test_frozen_app_close_keeps_ownership_during_native_setup(change, code):
    from tapwright import device as device_module
    from tapwright.device import AppNotAnswering

    async def go():
        d = make()
        d.stuck = AppNotAnswering("com.example.lab", "Lab")
        sent = []

        async def kill(bundle):
            assert d.control.holds_turn()
            if change == "pause":
                d.control.paused = True
            else:
                d.control.person_generation += 1
            await device_module.check_before_send()
            sent.append(bundle)

        d.session.device.kill_app = kill
        with pytest.raises(Refused) as error:
            await d._close_stuck(d.stuck, "Lab", [])
        assert error.value.code == code and not sent
        assert device_module.BEFORE_SEND.get() is None
    run(go)


@pytest.mark.parametrize("new_focus", ["email", "other", None])
def test_retype_checks_original_field_after_another_agent_turn(new_focus):
    from fakes import el
    from test_daemon import looked

    async def go():
        d = make()
        stop, task = await started(d)
        try:
            field = el("e1", "input", "Email", "email", value="a", rect=(25, 450, 380, 50))
            other = el("e2", "input", "Other", "other", value="keep me", rect=(25, 550, 380, 50))
            await looked(d, [field, other])
            s = real_dispatch(d.session)
            s.retype = types.MethodType(Session.retype, s)
            s.is_focused = types.MethodType(Session.is_focused, s)
            focus = "email"
            reads = []

            async def active():
                return focus

            async def is_field(element, expected):
                reads.append(expected.identifier)
                return element == expected.identifier

            async def wrong(result, text):
                async def other_agent():
                    nonlocal focus
                    focus = new_focus
                await d.control.agent(other_agent)
                return True

            async def landed(*args):
                return None

            s._active_element, s._is_field = active, is_field
            d._typed_wrong, d._typed_landed = wrong, landed
            if new_focus == "email":
                await d.run_tool("a", "type_text", {"target": {"id": "email"}, "text": "abc"})
                assert [path.rsplit("/", 1)[-1] for path, _ in s.dispatched] == ["clear", "keys"]
                assert "/element/email/clear" in s.dispatched[0][0]
            else:
                with pytest.raises(Refused) as error:
                    await d.run_tool("a", "type_text", {"target": {"id": "email"}, "text": "abc"})
                assert error.value.code == "stale"
                assert not s.dispatched
            assert reads == (["email"] if new_focus else [])
            assert s.execution_guard is None
        finally:
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("to", ["end", "target"])
@pytest.mark.parametrize("refuse_drag", [1, 2])
@pytest.mark.parametrize("code", ["paused", "busy"])
def test_scroll_refusal_invalidates_other_clients_only_after_delivery(to, refuse_drag, code, monkeypatch):
    from test_daemon import LongList

    async def go():
        d = make(settle_fallback=0.001)
        stop, task = await started(d)
        try:
            s = real_dispatch(d.session)
            for name in ("_drag", "_fast_drag"):
                setattr(s, name, types.MethodType(getattr(Session, name), s))
            rows = LongList(d)

            async def scroll(x0, y0, x1, y1):
                done = await Session.scroll(s, x0, y0, x1, y1)
                rows.offset = min(0.0, max(rows.lowest, rows.offset + y1 - y0))
                rows.lay()
                return done

            s.scroll = scroll
            await d.run_tool("a", "look", {"screenshot": False})
            await d.run_tool("observer", "look", {"screenshot": False})
            observer = d.control.client("observer")
            before = d.control.generation
            assert observer.seen == before
            notices = 0

            async def notify(ms):
                nonlocal notices
                notices += 1
                if notices == refuse_drag:
                    if code == "paused":
                        d.control.paused = True
                    else:
                        d.control.person_generation += 1

            monkeypatch.setattr(d._agent_touches, "notify", notify)
            args = {"to": to, "look": False}
            if to == "target":
                args["target"] = {"id": "row-30"}
            with pytest.raises(Refused) as error:
                await d.run_tool("a", "scroll", args)
            assert error.value.code == code
            assert len(s.dispatched) == refuse_drag - 1
            assert d.control.generation == before + (refuse_drag == 2)
            assert (observer.seen != d.control.generation) == (refuse_drag == 2)
            assert s.execution_guard is None
        finally:
            await finish(stop, task)
    run(go)

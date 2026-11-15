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


def test_cancel_uses_exact_named_runner_action_and_never_exposes_the_foreign_screen():
    async def go():
        d, s = synthetic()
        stop, task = await started(d)
        try:
            answer = await request(d)
            assert answer["text"] == "Cancelled system alert. Go back to your app with open_app."
            assert answer.get("image") is None and PRIVATE not in str(answer)
            assert s.dispatched == [("/session/S/alert/dismiss", {"name": "Cancel"})]
            assert s.cancel_reads >= 2 and s.before_input is None
            assert "screenshot" not in s.calls
        finally:
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("change", ["title", "message", "buttons", "rect", "button_rect", "button_name",
                                  "disabled", "root", "foreground", "underneath", "no_alert"])
def test_every_changed_alert_or_scope_proof_is_rechecked_after_the_touch_hook(change):
    async def go():
        d, s = synthetic()
        stop, task = await started(d)
        try:
            async def mutate(ms):
                if change == "title":
                    s.last_alert = dataclasses.replace(s.last_alert, title="A different alert")
                elif change == "message":
                    s.last_alert = dataclasses.replace(s.last_alert, message="Different private account")
                elif change == "buttons":
                    s.last_alert = dataclasses.replace(s.last_alert, buttons=("Cancel", "Delete"))
                elif change == "rect":
                    s.last_alert = dataclasses.replace(s.last_alert, rect=(51, 300, 330, 190))
                elif change == "button_rect":
                    s.tree[1] = dataclasses.replace(s.tree[1], rect=(226, 420, 140, 44))
                elif change == "button_name":
                    s.tree[1] = dataclasses.replace(s.tree[1], name="other-cancel")
                elif change == "disabled":
                    s.tree[1].enabled = False
                elif change == "root":
                    s.tree_app = ("Another app", "com.example.other")
                elif change == "foreground":
                    s.foreground = "com.example.other"
                elif change == "underneath":
                    s.running_apps = ["com.example.other"]
                else:
                    s.last_alert = None
            d._agent_touches.notify = mutate
            with pytest.raises(Refused) as error:
                await request(d)
            assert error.value.code in ("stale", "not_allowed")
            assert not s.dispatched and s.before_input is None
            assert PRIVATE not in str(error.value)
        finally:
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("state", ["no_cancel", "duplicate", "disabled", "runner_photo", "passcode", "input",
                                 "foreign_root", "foreign_front", "no_underlying", "recording", "journey", "paused",
                                 "offscreen", "zero_size", "covered"])
def test_unsafe_or_unattended_context_is_refused_without_any_input(state):
    async def go():
        d, s = synthetic()
        stop, task = await started(d)
        try:
            if state == "no_cancel":
                s.last_alert = dataclasses.replace(s.last_alert, buttons=("Edit Settings", "OK"))
                s.tree[1].label = "OK"
            elif state == "duplicate":
                s.last_alert = dataclasses.replace(s.last_alert, buttons=("Cancel", "Cancel"))
                s.tree[0].label = "Cancel"
            elif state == "disabled":
                s.tree[1].enabled = False
            elif state == "runner_photo":
                s.last_alert = dataclasses.replace(s.last_alert, title="WebDriverAgentRunner-Runner wants photo access")
            elif state == "passcode":
                s.last_alert = dataclasses.replace(s.last_alert, title="Enter your passcode for XCTest")
            elif state == "input":
                s.tree.append(dataclasses.replace(el("e3", "input", "Password", "password"), in_alert=True))
            elif state == "foreign_root":
                s.tree_app = ("Another app", "com.example.other")
            elif state == "foreign_front":
                s.foreground = "com.example.other"
            elif state == "no_underlying":
                s.running_apps = []
            elif state == "recording":
                d._recording["agent"] = object()
            elif state == "journey":
                d._journeys["agent"] = object()
            elif state == "offscreen":
                s.tree[1].rect = (225, 850, 140, 44)
            elif state == "zero_size":
                s.tree[1].rect = (225, 420, 0, 0)
            elif state == "covered":
                s.tree[1].covered_by = (ui.Bar("Other", s.tree[1].rect, PRIVATE),)
            else:
                d.control.set_paused(True)
            with pytest.raises(Refused):
                await request(d)
            assert not s.dispatched
        finally:
            d._recording.pop("agent", None)
            d._journeys.pop("agent", None)
            await finish(stop, task)
    run(go)


def test_empty_scope_and_a_policy_block_still_refuse_cancel():
    async def go():
        d, s = synthetic()
        stop, task = await started(d)
        try:
            with pytest.raises(Refused, match="no_apps"):
                await request(d, scope=daemon_module.Scope(()))
            rules = policy.loads('{"version":1,"block":["Cancel"],"confirm":[]}')
            with pytest.raises(Refused, match="blocked"):
                await request(d, policy=rules)
            assert not s.dispatched
        finally:
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("kind", [DeviceError, ConnectionLost, lambda message: Refused("not_actionable", message)])
def test_named_runner_failure_never_returns_the_foreign_alert_text_or_retries(kind):
    async def go():
        d, s = synthetic()
        s.cancel_error = kind("Failed to find Cancel: " + PRIVATE)
        stop, task = await started(d)
        try:
            with pytest.raises(Refused) as error:
                await request(d)
            assert PRIVATE not in str(error.value)
            assert s.dispatched == [("/session/S/alert/dismiss", {"name": "Cancel"})]
        finally:
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("when", ["ensure", "foreground_scope", "peek", "pause", "person", "locked"])
def test_dispatch_checks_after_delayed_connection_or_scope_reads(when):
    async def go():
        d, s = synthetic()
        stop, task = await started(d)
        try:
            original_ensure, original_scope, original_peek = s.ensure, s.device.foreground_apps, s.peek_tree
            changed = False

            def mutate():
                nonlocal changed
                if changed:
                    return
                changed = True
                if when == "pause":
                    d.control.set_paused(True)
                elif when == "person":
                    d.control.person_generation += 1
                elif when == "locked":
                    d.state = "locked"
                else:
                    s.last_alert = dataclasses.replace(s.last_alert, message="Changed private alert")

            async def ensure():
                if s.cancel_reads and when == "ensure":
                    mutate()
                return await original_ensure()

            async def scope():
                if s.cancel_reads and when == "foreground_scope":
                    mutate()
                return await original_scope()

            async def peek():
                if s.cancel_reads and when in ("peek", "pause", "person", "locked"):
                    mutate()
                return await original_peek()

            s.ensure, s.device.foreground_apps, s.peek_tree = ensure, scope, peek
            with pytest.raises(Refused):
                await request(d)
            assert changed and not s.dispatched and s.before_input is None
        finally:
            d.control.set_paused(False)
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("context", ["batch", "nested", "script", "drafts", "schedule"])
def test_automated_contexts_never_reach_alert_reads_or_input(context):
    async def go():
        d, s = synthetic()
        stop, task = await started(d)
        setting = {"batch": daemon_module.BATCH, "nested": daemon_module._NESTED,
                   "script": daemon_module.SCRIPT, "drafts": daemon_module._DRAFTS_ROUTE}.get(context)
        token = setting.set(True) if setting is not None else None
        try:
            with pytest.raises(Refused):
                if context == "schedule":
                    await d.run_tool(daemon_module.schedulelib.CLIENT + "test", "alert", {"action": "cancel"}, scope=SCOPE)
                else:
                    await request(d)
            assert not s.dispatched and s.cancel_reads == 0
        finally:
            if token is not None:
                setting.reset(token)
            await finish(stop, task)
    run(go)


def test_custom_cancel_confirmation_keeps_private_context_out_and_binds_alert_identity():
    async def go():
        d, s = synthetic()
        s.tree[1].identifier = s.tree[1].value = PRIVATE
        stop, task = await started(d)
        rules = policy.loads('{"version":1,"block":[],"confirm":["Cancel"]}')
        try:
            with pytest.raises(Refused, match="needs_confirmation") as error:
                await request(d, policy=rules)
            assert PRIVATE not in str(error.value)
            first = next(iter(d._confirms.values()))
            assert PRIVATE not in str(first)
            first.state = "allowed"
            s.last_alert = dataclasses.replace(s.last_alert, message="Another private account")
            with pytest.raises(Refused, match="needs_confirmation"):
                await request(d, policy=rules)
            assert not s.dispatched
            assert all(PRIVATE not in str(c) for c in d._confirms.values())
        finally:
            await finish(stop, task)
    run(go)


def test_custom_cancel_dialog_does_not_offer_a_grant_or_show_private_context():
    async def go():
        d, s = synthetic()
        s.tree[1].identifier = s.tree[1].value = PRIVATE
        stop, task = await started(d)
        call = None
        try:
            rules = policy.loads('{"version":1,"block":[],"confirm":["Cancel"]}')
            call = asyncio.create_task(request(d, policy=rules, dialog=True))
            events = await d.dialog_poll("agent", 0, 2.0)
            ask = events["events"][0]
            assert ask["message"] == "Allow Cancel on this system alert?"
            assert PRIVATE not in str(events)
            held = d._confirms[ask["id"]]
            assert held.offer is None and PRIVATE not in str(held)
            assert d.dialog_answer("agent", ask["id"], "allow")
            answer = await asyncio.wait_for(call, 2)
            assert PRIVATE not in str(answer)
            assert s.dispatched == [("/session/S/alert/dismiss", {"name": "Cancel"})]
        finally:
            if call is not None and not call.done():
                call.cancel()
                await asyncio.gather(call, return_exceptions=True)
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("action", ["text", "accept", "dismiss"])
def test_default_alert_operations_do_not_gain_access(action):
    async def go():
        d, s = synthetic()
        stop, task = await started(d)
        try:
            with pytest.raises(Refused) as error:
                await d.run_tool("agent", "alert", {"action": action}, scope=SCOPE)
            assert error.value.code == "not_allowed" and "outside_apps" in str(error.value)
            assert PRIVATE not in str(error.value) and not s.dispatched
        finally:
            await finish(stop, task)
    run(go)

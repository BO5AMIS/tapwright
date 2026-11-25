"""1.7.8, from the phone check of 2026-11-18 and the owner's decision of that day.

Part A: iOS sets SpringBoard's lock state while Notification Center is pulled down over an UNLOCKED phone, so
WebDriverAgent's `/wda/locked` says true and every call was refused `locked`, `press_button home` too, the one call that
closes it. Home is now pressed when a fresh tree proves that sheet over an unlocked phone; a real lock is never pressed.
The two trees are rebuilt from the real captures (iOS 26.5), with generic notification text.

Part B: the `unlock` tool. "Make it so the agent can unlock the phone if they have the passcode. If they don't, they ask
for it." The passcode comes from serve's own vault only; the agent never sees or sends it. All values are synthetic."""
import json

import pytest
from test_daemon import finish, make, run, started
from test_unlock import PASSCODE
from test_unlock_daemon import FORMS, everything, locked_daemon, vault

from tapwright import daemon as daemon_module, errors, needs, notifications, unlock
from tapwright.ceiling import Ceiling
from tapwright.control import Refused
from tapwright.device import PASSCODE_WORDS


# ---- the two trees, as WebDriverAgent's XML ----

def _node(kind: str, x, y, w, h, name: str = "", label: str = "", value: str | None = None, enabled: bool = True,
          traits: str = "", inner: str = "") -> str:
    attrs = [f'type="XCUIElementType{kind}"']
    if name:
        attrs.append(f'name="{name}"')
    if label:
        attrs.append(f'label="{label}"')
    if value is not None:
        attrs.append(f'value="{value}"')
    if traits:
        attrs.append(f'traits="{traits}"')
    attrs.append(f'enabled="{"true" if enabled else "false"}" visible="true" x="{x}" y="{y}" width="{w}" height="{h}"')
    return f'<XCUIElementType{kind} {" ".join(attrs)}>{inner}</XCUIElementType{kind}>'


def _cell(n: int, y: int) -> str:
    """One notification as iOS 26 lists it on the cover sheet: a ListCell holding its short look."""
    said = f"EXAMPLE APP, {n} minutes ago, Reminder {n}, Something to do."
    look = _node("Other", 14, y, 402, 100, "NotificationShortLookView", said,
                 inner=_node("StaticText", 78, y + 20, 321, 23, "NotificationTitle", f"Reminder {n}"))
    return _node("Cell", 14, y, 402, 100, "ListCell", said, inner=look)


def _cover(*inner: str) -> str:
    body = "".join(inner)
    return ('<?xml version="1.0" encoding="UTF-8"?><AppiumAUT>'
            '<XCUIElementTypeApplication type="XCUIElementTypeApplication" name=" " label=" " enabled="true" '
            'visible="true" bundleId="com.apple.springboard" x="0" y="0" width="430" height="932">'
            + _node("Window", 0, 0, 430, 932, "SBCoverSheetWindow", inner=_node("ScrollView", 0, 0, 430, 932, inner=body))
            + '</XCUIElementTypeApplication></AppiumAUT>')


DATE = _node("Other", 0, -11, 430, 932, "lockscreen-date-view",
             inner=_node("StaticText", 24, 67, 382, 40, label="Wednesday, 18 November")
             + _node("StaticText", 60, 82, 310, 156, label="5:54 AM"))
HEADER = _node("StaticText", 24, 368, 275, 44, "Notification Center", "Notification Center", traits="Header")
CLEAR = _node("Button", 351, 373, 65, 39, "clear-button", "Clear", value="Notification Center")
CAMERA = _node("Button", 300, 798, 94, 94, "system-orb-button-com.apple.camera.deeplink.button", "Camera")


def _flashlight(enabled: bool = True) -> str:
    return _node("Button", 36, 798, 94, 94, "flashlight-orb-button", "Flashlight", value="Off", enabled=enabled)


def center_tree(header: bool = True, clear: bool = True, flashlight: bool = True, lit: bool = True) -> str:
    """Notification Center pulled down over an unlocked phone (tree-nc-unlocked): its header, its Clear button (value
    "Notification Center"), an enabled flashlight. Each can be left out to check the proof needs all three."""
    parts = [_cell(1, 219), *([HEADER] if header else []), *([CLEAR] if clear else []), _cell(2, 419), _cell(3, 516),
             DATE, *([_flashlight(lit)] if flashlight else []), CAMERA]
    return _cover(*parts)


def lock_tree() -> str:
    """The phone locked with the side button (tree-real-lock): the same cover sheet, cells and date, but no header, no
    Clear button, and a disabled flashlight."""
    return _cover(_cell(1, 679), DATE, _flashlight(False), CAMERA)


# ---- Part A: the proof ----

def test_the_proof_holds_on_notification_center_over_an_unlocked_phone():
    tree = center_tree()
    assert notifications.is_center(tree)  # (is_center alone can't tell the two apart: the lock screen is one too)
    assert notifications.center_over_unlocked(tree)


def test_the_proof_fails_on_the_real_lock_screen():
    assert notifications.is_center(lock_tree())
    assert not notifications.center_over_unlocked(lock_tree())


@pytest.mark.parametrize("missing", ["header", "clear", "flashlight", "lit"])
def test_the_proof_fails_when_any_one_of_the_three_is_missing(missing):
    assert not notifications.center_over_unlocked(center_tree(**{missing: False}))


def test_the_proof_fails_outside_springboard_and_on_a_broken_tree():
    tree = center_tree()
    assert not notifications.center_over_unlocked(tree.replace("com.apple.springboard", "com.example.app"))
    assert not notifications.center_over_unlocked(tree[:-40])
    # a Clear button whose value is another list's, or a "Notification Center" that is itself a button, proves nothing
    assert not notifications.center_over_unlocked(tree.replace('value="Notification Center"', 'value="Recent"'))
    assert not notifications.center_over_unlocked(tree.replace(HEADER, HEADER.replace("StaticText", "Button")))


# ---- Part A: the daemon ----

def _home_daemon(tree: str):
    """A daemon whose phone reads as locked, with `tree` on the screen; the home press closes the sheet."""
    d = make()
    d.session.is_locked = True
    sent: list = []

    async def source(timeout=60):
        return tree

    async def http(method, path, payload=None, timeout=30.0):
        sent.append((method, path))
        if path == "/wda/homescreen":
            d.session.is_locked = False
        return {}
    d.session._source = source
    d.session.device.http = http
    return d, sent


def test_home_closes_notification_center_while_the_phone_reads_as_locked():
    async def go():
        d, sent = _home_daemon(center_tree())
        stop, task = await started(d, until=("locked",))
        assert d.state == "locked"
        answer = await d.run_tool("a", "press_button", {"name": "home"})
        assert answer["text"].startswith("Pressed home: Notification Center was open")
        assert sent == [("POST", "/wda/homescreen")]
        assert d.state == "live"  # (the next call needn't wait for the heartbeat)
        await finish(stop, task)
    run(go)


def test_home_on_the_real_lock_screen_presses_nothing():
    async def go():
        d, sent = _home_daemon(lock_tree())
        stop, task = await started(d, until=("locked",))
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "press_button", {"name": "home"})
        assert e.value.code == "locked" and "Nothing was pressed" in str(e.value)
        assert sent == [] and d.state == "locked"
        # the other buttons stay refused while locked, as before
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "press_button", {"name": "volumeUp"})
        assert e.value.code == "locked" and sent == []
        await finish(stop, task)
    run(go)


def test_home_over_notification_center_still_refuses_while_paused_or_a_person_is_at_the_phone():
    async def go():
        d, sent = _home_daemon(center_tree())
        stop, task = await started(d, until=("locked",))
        d.control.set_paused(True)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "press_button", {"name": "home"})
        assert e.value.code == "paused" and sent == []
        d.control.set_paused(False)

        async def person_meanwhile(timeout=60):  # a person's input lands while the tree is read
            d.control.person_generation += 1
            return center_tree()
        d.session._source = person_meanwhile
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "press_button", {"name": "home"})
        assert e.value.code == "busy" and sent == []
        await finish(stop, task)
    run(go)


# ---- Part B: the `unlock` tool ----

MODES = {"personal": dict(phone=False), "dedicated": dict(phone=False, dedicated=True), "phone": dict(phone=True)}


def _mode(d, tmp_path, mode: str, passcode=PASSCODE):
    d.ceiling = Ceiling(**MODES[mode], accounts=vault(tmp_path, passcode=passcode))


@pytest.mark.parametrize("mode", list(MODES))
def test_unlock_types_the_saved_passcode_in_every_mode(tmp_path, mode):
    async def go():
        said: list = []
        d, s = locked_daemon(tmp_path, said=said)
        _mode(d, tmp_path, mode)
        stop, task = await started(d, until=("locked",))
        r = await d.run_tool("a", "unlock", {})
        assert r["text"].startswith("Unlocked the phone with the passcode saved in the vault.\nScreen ")
        assert d.lock_screen.pressed == [PASSCODE] and not s.is_locked and d.state == "live"
        await finish(stop, task)
        audit = [json.loads(x) for p in (tmp_path / "audit").glob("*.jsonl") for x in p.read_text("utf-8").splitlines()]
        assert [(a["kind"], a["name"]) for a in audit] == [("vault", "phone.passcode")]
        logged = [json.loads(x) for p in (tmp_path / "log").rglob("*.jsonl") for x in p.read_text("utf-8").splitlines()]
        assert any(line.get("tool") == "unlock" and line.get("ok") for line in logged)
        out = everything(tmp_path, said, d, [r["text"]])
        for form in FORMS:
            assert form not in out, form
    run(go)


def test_unlock_without_a_saved_passcode_asks_for_it_and_types_nothing(tmp_path):
    async def go():
        d, s = locked_daemon(tmp_path, passcode=None, phone=False)
        stop, task = await started(d, until=("locked",))
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "unlock", {})
        assert e.value.code == "locked" and str(e.value) == daemon_module.UNLOCK_NO_PASSCODE
        assert "ask the user to unlock it" in str(e.value) and "phone.passcode" in str(e.value)
        assert "--accounts FILE" in str(e.value)
        assert d.lock_screen.pressed == [] and d.lock_screen.calls == [] and s.is_locked
        await finish(stop, task)
    run(go)


def test_unlock_without_any_vault_asks_for_it(tmp_path):
    async def go():
        d, s = locked_daemon(tmp_path, phone=False)
        d.ceiling = Ceiling()
        stop, task = await started(d, until=("locked",))
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "unlock", {})
        assert e.value.code == "locked" and str(e.value) == daemon_module.UNLOCK_NO_PASSCODE
        assert d.lock_screen.pressed == []
        await finish(stop, task)
    run(go)


def test_unlock_on_an_unlocked_phone_says_so_and_types_nothing(tmp_path):
    async def go():
        d, s = locked_daemon(tmp_path, phone=False)
        s.is_locked = False
        stop, task = await started(d, until=("live",))
        r = await d.run_tool("a", "unlock", {})
        assert r["text"].splitlines()[0] == "The phone isn't locked."
        assert d.lock_screen.pressed == [] and d.lock_screen.calls == []
        await finish(stop, task)
    run(go)


def test_unlock_is_refused_as_a_step_and_inside_a_journey(tmp_path):
    async def go():
        d, s = locked_daemon(tmp_path, phone=False)
        stop, task = await started(d, until=("locked",))
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": [{"tool": "unlock"}]})
        assert e.value.code == "bad_params"
        d._journeys["b"] = object()  # (a journey running for agent b: its calls never unlock)
        with pytest.raises(Refused) as e:
            await d._unlock_tool("b")
        assert e.value.code == "bad_params" and str(e.value) == daemon_module.UNLOCK_TOP_ONLY
        del d._journeys["b"]
        assert d.lock_screen.pressed == [] and s.is_locked
        await finish(stop, task)
    run(go)


def test_unlock_takes_no_arguments(tmp_path):
    async def go():
        d, s = locked_daemon(tmp_path, phone=False)
        stop, task = await started(d, until=("locked",))
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "unlock", {"passcode": "123456"})
        assert e.value.code == "bad_params" and d.lock_screen.pressed == []
        await finish(stop, task)
    run(go)


def test_unlock_refuses_while_paused_or_a_person_is_at_the_phone(tmp_path):
    async def go():
        d, s = locked_daemon(tmp_path, phone=False)
        stop, task = await started(d, until=("locked",))
        d.control.set_paused(True)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "unlock", {})
        assert e.value.code == "paused"
        d.control.set_paused(False)
        d.control._person_at = d.control.clock()
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "unlock", {})
        assert e.value.code == "busy"
        assert d.lock_screen.pressed == [] and s.is_locked
        await finish(stop, task)
    run(go)


def test_unlock_never_types_on_the_xctest_prompt(tmp_path):
    async def go():
        d, s = locked_daemon(tmp_path, phone=False)
        stop, task = await started(d, until=("locked",))
        d._set_state("locked", f"the phone is {PASSCODE_WORDS} to allow UI automation")
        with pytest.raises(Refused) as e:
            await d._unlock_tool("a")
        assert e.value.code == "locked" and str(e.value) == unlock.XCTEST
        assert d.lock_screen.pressed == [] and d.lock_screen.calls == []
        await finish(stop, task)
    run(go)


def test_a_refused_passcode_is_typed_once_through_the_tool(tmp_path):
    async def go():
        said: list = []
        d, s = locked_daemon(tmp_path, right="000000", said=said, phone=False)
        stop, task = await started(d, until=("locked",))
        errors = []
        for _ in range(3):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "unlock", {})
            assert e.value.code == "locked"
            errors.append(str(e.value))
        assert errors == [unlock.REFUSED] * 3  # (the same file stays refused until a person runs unlock-reset)
        assert d.lock_screen.pressed == [PASSCODE]
        await finish(stop, task)
        out = everything(tmp_path, said, d, errors)
        for form in FORMS:
            assert form not in out, form
    run(go)


def test_unlocks_that_work_are_not_limited_through_the_tool(tmp_path):
    """(1.10.0, the owner's decision) Only attempts that don't unlock count toward the hourly limit (test_unlock.py has
    the limit itself): a phone that keeps locking is unlocked every time."""
    async def go():
        d, s = locked_daemon(tmp_path, phone=False)
        stop, task = await started(d, until=("locked",))
        for _ in range(unlock.LIMIT + 3):
            r = await d.run_tool("a", "unlock", {})
            assert r["text"].startswith("Unlocked the phone")
            s.is_locked = True  # (locked again with the side button)
            d._set_state("locked", "unlock the phone")
        assert d.lock_screen.pressed == [PASSCODE] * (unlock.LIMIT + 3)
        await finish(stop, task)
    run(go)


# ---- Part B: the new wording of a locked phone ----

def test_a_locked_phone_now_names_unlock():
    assert errors.PHONE_LOCKED == ("the phone is locked; call unlock (it types the passcode saved for this phone), or "
                                   "ask the user to unlock it")


def test_an_action_on_a_locked_phone_says_call_unlock(tmp_path):
    async def go():
        d, s = locked_daemon(tmp_path, passcode=None, phone=False)
        stop, task = await started(d, until=("locked",))
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "ok-button"}})
        assert e.value.code == "locked" and str(e.value) == errors.PHONE_LOCKED
        assert d.lock_screen.pressed == []
        await finish(stop, task)
    run(go)


def test_the_first_answer_on_a_locked_phone_names_unlock(tmp_path):
    async def go():
        d, s = locked_daemon(tmp_path, phone=False)
        stop, task = await started(d, until=("locked",))
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert f"Note: {needs.LOCKED_AGENT}" in r["text"] and "call unlock" in needs.LOCKED_AGENT
        assert d.lock_screen.pressed == [] and s.is_locked  # (a look never unlocks)
        await finish(stop, task)
    run(go)


def test_doctor_still_tells_a_person_to_unlock_the_phone():
    assert needs.from_state("locked", "unlock the phone") == [needs.LOCKED]
    assert needs.from_state("locked", "unlock the phone", agent=True) == [needs.LOCKED_AGENT]


def test_the_tool_is_registered_everywhere():
    import asyncio

    from tapwright import descriptions, mcp_server, params
    assert params.ALLOWED["unlock"] == ()
    assert "unlock" in daemon_module.LOGGED
    assert "never" in descriptions.describe("unlock") and "call unlock" in descriptions.INSTRUCTIONS
    listed = asyncio.run(mcp_server.server.list_tools())
    tool = next(t for t in listed if t.name == "unlock")
    assert tool.input_schema.get("properties", {}) == {}


def test_home_over_notification_center_asks_no_person_first():
    """A host with a dialog: a locked phone would ask a person to unlock it before an action, for up to 120 s. Home
    decides from the tree at once instead (the phone may not be locked at all)."""
    import asyncio

    async def go():
        d, sent = _home_daemon(center_tree())
        stop, task = await started(d, until=("locked",))
        answer = await asyncio.wait_for(d.run_tool("a", "press_button", {"name": "home"}, dialog=True), 2)
        assert answer["text"].startswith("Pressed home") and sent == [("POST", "/wda/homescreen")]
        assert not d._needs
        await finish(stop, task)
    run(go)


def test_unlock_asks_the_phone_when_the_heartbeat_hasnt_seen_the_lock_yet(tmp_path):
    """(2.0, phone test 2026-11-26) `press_button lock`, then `unlock` 2.5 s later, answered "The phone isn't locked."
    on a locked phone: the daemon's state was still `live` until its next heartbeat."""
    async def go():
        d, s = locked_daemon(tmp_path, phone=False)
        s.is_locked = False
        d.heartbeat = 60  # the heartbeat won't notice in time
        stop, task = await started(d, until=("live",))
        s.is_locked = True  # the phone locks; the state still says live
        assert d.state == "live"
        r = await d.run_tool("a", "unlock", {})
        assert r["text"].splitlines()[0] != "The phone isn't locked."
        assert d.lock_screen.calls or d.lock_screen.pressed  # the unlock was tried
        await finish(stop, task)
    run(go)

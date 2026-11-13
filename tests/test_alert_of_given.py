"""PLAN-1.6 Part C: in personal mode `alert` answers an iOS alert that another app draws about a given app (Settings'
`Delete “Wayfare”?`), under the policy. It removes the dead end a tester's run hit: Settings is in front, so every call was
`outside_apps`. Reading the alert's text and dismissing it are open; accepting a button with a policy word needs a person's OK."""
import pytest
from fakes import FakeSession, button
from test_daemon import finish, make, run, started

from tapwright import ui
from tapwright.control import Refused
from tapwright.daemon import Scope

MINE = Scope(("com.example.app",))
SETTINGS = ("Settings", "com.apple.Preferences")


def settings_with(alert_title: str, buttons=("Cancel", "Delete")):
    """The phone: Settings in front, with an alert it raised."""
    s = FakeSession()
    s.app_names["com.example.app"] = "Wayfare"
    s.installed = [{"bundle_id": "com.example.app", "name": "Wayfare", "version": "1.0", "build": "7", "executable": "Wayfare"}]
    s.foreground, s.app = SETTINGS[1], SETTINGS
    s.tree = [button("e1", "General", "general"), button("e2", "Storage", "storage")]
    s.last_alert = ui.Alert(alert_title, tuple(buttons), (60.0, 300.0, 300.0, 200.0))
    return s


def test_the_alerts_text_can_be_read_while_settings_is_in_front():
    async def go():
        s = settings_with("Delete “Wayfare”?")
        d = make(s)
        stop, task = await started(d)

        async def text(action):
            return "Delete “Wayfare”?\nDeleting this app also deletes its data." if action == "text" else f"alert {action}ed"
        s.alert = text
        r = await d.run_tool("a", "alert", {"action": "text"}, scope=MINE)
        assert "Delete “Wayfare”?" in r["text"] and "outside_apps" not in r["text"]
        assert "General" not in r["text"] and "Storage" not in r["text"]  # nothing of Settings' screen
        await finish(stop, task)
    run(go)


def test_dismiss_answers_it_without_a_persons_ok_and_says_where_the_app_is():
    async def go():
        s = settings_with("Delete “Wayfare”?")
        d = make(s)
        stop, task = await started(d)
        r = await d.run_tool("a", "alert", {"action": "dismiss"}, scope=MINE)
        lines = r["text"].splitlines()
        assert lines[0] == "alert dismissed" and "alert" in s.calls
        assert lines[1].startswith("Note: the alert was another app's, about one of yours")
        assert "open_app" in lines[1] and len(lines) == 2  # no other app's screen
        assert "General" not in r["text"] and r["image"] is None
        await finish(stop, task)
    run(go)


def test_accepting_a_delete_needs_a_persons_ok_and_then_goes_through():
    async def go():
        s = settings_with("Delete “Wayfare”?")
        d = make(s)
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "alert", {"action": "accept"}, scope=MINE)
        assert e.value.code == "not_allowed" and "needs_confirmation" in str(e.value) and "alert" not in s.calls
        [asked] = d.open_confirms()
        d._answer_confirm({"id": asked["id"], "allow": True})  # the person allows it in the live view
        r = await d.run_tool("a", "alert", {"action": "accept"}, scope=MINE)
        assert r["text"].splitlines()[0] == "alert accepted" and "alert" in s.calls
        assert "General" not in r["text"]
        await finish(stop, task)
    run(go)


def test_a_policy_block_still_blocks():
    async def go():
        from tapwright import policy
        s = settings_with("Delete “Wayfare”?", ("Cancel", "Wipe"))
        d = make(s)
        stop, task = await started(d)
        rules = policy.loads('{"block": ["Wipe"], "confirm": []}')
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "alert", {"action": "accept"}, scope=MINE, policy=rules)
        assert e.value.code == "not_allowed" and str(e.value).startswith("blocked") and "alert" not in s.calls
        await finish(stop, task)
    run(go)


def test_an_alert_that_names_another_app_is_not_a_given_apps():
    async def go():
        s = settings_with("Delete “Mail”?")
        d = make(s)
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "alert", {"action": "dismiss"}, scope=MINE)
        assert e.value.code == "not_allowed" and "outside_apps" in str(e.value) and "alert" not in s.calls
        await finish(stop, task)
    run(go)


def test_without_an_alert_settings_in_front_is_refused_as_before():
    async def go():
        s = settings_with("Delete “Wayfare”?")
        s.last_alert = None
        d = make(s)
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "alert", {"action": "dismiss"}, scope=MINE)
        assert e.value.code == "not_allowed" and "outside_apps" in str(e.value)
        await finish(stop, task)
    run(go)


@pytest.mark.parametrize("tool,args", [("look", {"screenshot": False}), ("tap", {"target": {"id": "general"}}),
                                       ("wait_for", {"text": "General", "timeout": 0.5}), ("map", {})])
def test_only_alert_gets_through_every_other_call_is_still_outside_apps(tool, args):
    async def go():
        s = settings_with("Delete “Wayfare”?")
        d = make(s)
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", tool, args, scope=MINE)
        assert e.value.code == "not_allowed" and "outside_apps" in str(e.value)
        assert "tap" not in s.calls
        await finish(stop, task)
    run(go)


def test_a_dedicated_device_is_unchanged():
    async def go():
        s = settings_with("Delete “Wayfare”?")
        d = make(s)
        stop, task = await started(d)
        r = await d.run_tool("a", "alert", {"action": "dismiss"})
        assert r["text"].startswith("alert dismissed") and "Note: the alert was another app's" not in r["text"]
        await finish(stop, task)
    run(go)

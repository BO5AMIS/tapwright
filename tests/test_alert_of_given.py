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














def test_a_dedicated_device_is_unchanged():
    async def go():
        s = settings_with("Delete “Wayfare”?")
        d = make(s)
        stop, task = await started(d)
        r = await d.run_tool("a", "alert", {"action": "dismiss"})
        assert r["text"].startswith("alert dismissed") and "Note: the alert was another app's" not in r["text"]
        await finish(stop, task)
    run(go)

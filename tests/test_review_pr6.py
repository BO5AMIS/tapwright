"""Fixes from Codex's review of PR #6 (1.4.0): secrets in recordings, network owners, policy on the live front app,
launch overrides, indexed reach, the hold's own death."""
import asyncio
import json

import pytest
from fakes import button, el
from test_daemon import LongList, finish, make, run, started

from tapwright import daemon as daemon_module, policy
from tapwright.control import Refused


# ---- P1: a secure field below the screen is never recorded in plaintext ----
def test_recording_never_saves_text_typed_into_a_secure_field_that_was_off_screen(tmp_path):
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        s.screen = (400.0, 800.0)
        s.tree = [el("e1", "scroll", "", "form", rect=(0, 0, 400, 800)), el("e2", "input", "Email", "email",
                                                                                 rect=(10, 100, 300, 44))]
        s.offscreen = [el("", "input", "Password", "pw", rect=(10, 1000, 300, 44), secure=True)]
        real = s.scroll

        async def scroll(x0, y0, x1, y1):  # the form moves: the password field comes up
            s.tree = [s.tree[0], el("e2", "input", "Password", "pw", rect=(10, 300, 300, 44), secure=True)]
            s.offscreen = []
            return await real(x0, y0, x1, y1)
        s.scroll = scroll
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "record", {"start": True, "name": "sign in"})
        await d.run_tool("a", "type_text", {"target": {"id": "pw"}, "text": "hunter2", "look": False})
        assert s.typed and s.typed[-1][0] == "hunter2"  # it was typed, after the scroll
        path = tmp_path / "s.json"
        r = await d.run_tool("a", "record", {"stop": True, "path": str(path)})
        saved = path.read_text("utf-8")
        assert "hunter2" not in saved and "hunter2" not in r["text"]
        assert json.loads(saved)["steps"] == [{"tool": "type_text", "text": "", "secure": True,
                                               "target": {"id": "pw"}}]
        assert "Step 1 typed into a secure field" in r["text"]
        await finish(stop, task)
    run(go)


# ---- P1: `network` checks the policy against the app in front now ----
def test_network_checks_the_per_app_policy_against_the_app_in_front_now():
    async def go():
        d = make()
        stop, task = await started(d)
        p = policy.parse({"apps": {"com.blocked.app": {"block": ["network"]}}})
        await d.run_tool("a", "look", {"screenshot": False})  # the last read: com.example.app
        d.session.foreground = "com.blocked.app"  # a person put the blocked app in front since
        d.session.app_names["com.blocked.app"] = "Blocked"
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "network", {"mode": "offline"}, policy=p)
        assert e.value.code == "not_allowed" and "blocks network for" in str(e.value)
        assert d.session.device.network_calls == [] and d._network is None
        d.session.foreground = "com.example.app"
        assert (await d.run_tool("a", "network", {"mode": "3g"}, policy=p))["text"].startswith("Network: 3g")
        await finish(stop, task)
    run(go)


def test_network_fails_closed_when_the_front_app_cannot_be_read_and_a_policy_blocks_it_per_app():
    async def go():
        d = make()
        stop, task = await started(d)
        p = policy.parse({"apps": {"com.blocked.app": {"block": ["network"]}}})

        async def broken():
            raise RuntimeError("no answer")
        d.session.foreground_bundle_id = broken
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "network", {"mode": "3g"}, policy=p)
        assert e.value.code == "not_allowed" and d.session.device.network_calls == []
        # no per-app word anywhere: nothing depends on the front app, so it still works
        assert (await d.run_tool("a", "network", {"mode": "3g"}))["text"].startswith("Network: 3g")
        await finish(stop, task)
    run(go)


# ---- P2: a plain open_app after one with overrides starts the app again with the defaults ----
def test_a_plain_open_app_after_launch_overrides_relaunches_with_the_defaults():
    async def go():
        d = make()
        stop, task = await started(d)
        app = {"app": "com.example.app", "look": False}
        await d.run_tool("a", "open_app", {**app, "language": "ar", "text_size": "ax5"})
        d.session.closed.clear()
        r = await d.run_tool("a", "open_app", app)
        assert d.session.closed == ["com.example.app"]  # the running app is closed: WebDriverAgent would bring it forward
        assert d.session.launch_args[-1] == []
        assert r["text"] == "Opened Example (com.example.app), back to the phone's own language and text size."
        d.session.closed.clear()
        await d.run_tool("a", "open_app", app)  # defaults already: left alone, as before
        assert d.session.closed == []
        await d.run_tool("a", "open_app", {**app, "region": "QA"})
        d.session.closed.clear()
        await d.run_tool("a", "open_app", {**app, "text_size": "system"})  # `system` is the phone's own setting
        assert d.session.closed == ["com.example.app"] and d.session.launch_args[-1] == []
        await d.run_tool("a", "open_app", {**app, "language": "ar"})
        await d.run_tool("a", "close_app", {"app": "com.example.app"})  # closing forgets the overrides
        d.session.closed.clear()
        await d.run_tool("a", "open_app", app)
        assert d.session.closed == []
        await finish(stop, task)
    run(go)


# ---- P2: an indexed target that is off screen is acted on after the scroll brings it on ----
def test_a_tap_on_an_indexed_off_screen_match_taps_that_element_after_the_scroll():
    async def go():
        d = make()
        stop, task = await started(d)
        rows = LongList(d, rows=40, ids=False, labels=lambda i: "Account")
        await d.run_tool("a", "look", {"screenshot": False})
        r = await d.run_tool("a", "tap", {"target": {"label": "Account", "index": 30}, "look": False})
        assert r["text"].startswith("Scrolled to it, then tapped") or "Tapped" in r["text"], r["text"]
        x, y = d.session.tap_points[-1]
        assert y == 30 * rows.pitch + rows.offset + rows.height / 2  # row 30's centre, not another Account's
        await finish(stop, task)
    run(go)


def test_type_text_into_an_indexed_off_screen_input_types_into_that_input():
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        s.tree = [el("e1", "scroll", "", "form", rect=(0, 0, 400, 800)),
                  el("e2", "input", "Note", "", rect=(10, 100, 300, 44))]
        s.offscreen = [el("", "input", "Note", "", rect=(10, 1000, 300, 44)),
                       el("", "input", "Note", "", rect=(10, 1500, 300, 44))]
        real = s.scroll

        async def scroll(x0, y0, x1, y1):  # the form moves up by 700: only the second Note is on screen now
            s.tree = [s.tree[0], el("e2", "input", "Note", "", rect=(10, 300, 300, 44))]
            s.offscreen = [el("", "input", "Note", "", rect=(10, -600, 300, 44)),
                           el("", "input", "Note", "", rect=(10, 800, 300, 44))]
            return await real(x0, y0, x1, y1)
        s.scroll = scroll
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "type_text", {"target": {"label": "Note", "index": 1}, "text": "hi", "look": False})
        assert s.typed and s.typed[-1][0] == "hi"
        assert next(e for e in s.items if e.ref == s.typed[-1][3].ref).y == 300  # the one that came on screen
        await finish(stop, task)
    run(go)

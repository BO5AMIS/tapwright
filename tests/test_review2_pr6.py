"""Fixes from Codex's second review of PR #6 (1.4.0): an unreadable front app, a goodbye that races the setting of a
condition, an index that must survive a scroll, the crash leftover cleared by the real device, and the policy's
second look at the element a scroll brings on screen."""
import asyncio
import json

import pytest
from fakes import el
from test_daemon import finish, make, run, started
from test_review_pr6 import _fake_instruments

from tapwright import policy
from tapwright.control import Refused


# ---- P1: a front app that can't be named is not an app the policy lets through ----
def test_network_refuses_when_the_front_app_is_unknown_and_a_policy_blocks_it_per_app():
    async def go():
        d = make()
        stop, task = await started(d)
        p = policy.parse({"apps": {"com.blocked.app": {"block": ["network"]}}})
        await d.run_tool("a", "look", {"screenshot": False})  # the last read named an allowed app

        async def unknown():  # WebDriverAgent's error, or no bundle id in its answer: the real session says None
            return None
        d.session.foreground_bundle_id = unknown
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "network", {"mode": "offline"}, policy=p)
        assert e.value.code == "not_allowed"
        assert d.session.device.network_calls == [] and d._network is None
        # nothing in the policy depends on the app in front: an unknown one changes nothing
        assert (await d.run_tool("a", "network", {"mode": "3g"}))["text"].startswith("Network: 3g")
        await finish(stop, task)
    run(go)


# ---- P1: a goodbye that arrives while the condition is being set ----
def test_a_goodbye_during_the_setting_of_a_condition_leaves_none_behind():
    async def go():
        d = make()
        stop, task = await started(d)
        device = d.session.device
        inside, release = asyncio.Event(), asyncio.Event()
        real = device.set_network

        async def slow(mode):
            profile = await real(mode)
            inside.set()
            await release.wait()  # the phone has the condition; the answer is on its way
            return profile
        device.set_network = slow
        d.agent_alive("a")
        d.agent_alive("b")
        setting = asyncio.create_task(d.run_tool("a", "network", {"mode": "3g"}))
        await inside.wait()
        await d.agent_ended("a")  # its stdio closed meanwhile
        release.set()
        with pytest.raises(Refused):
            await setting
        assert d._network is None and d._network_by is None
        assert device.network_calls[-1] == ("clear",)  # the phone is back to normal
        from tapwright import store
        assert not store.NETWORK_FILE.exists()
        assert "a" not in d._agent_alive and "b" in d._agent_alive  # the neighbour is untouched
        # a call from the ended agent that arrives late sets nothing
        device.network_calls.clear()
        device.set_network = real
        with pytest.raises(Refused):
            await d.run_tool("a", "network", {"mode": "lossy"})
        assert d._network is None and device.network_calls == []
        d.agent_alive("a")  # nor does a late heartbeat bring it back
        assert "a" not in d._agent_alive
        # another agent is not affected by it
        assert (await d.run_tool("b", "network", {"mode": "3g"}))["text"].startswith("Network: 3g")
        assert d._network == "3g" and d._network_by == "b"
        await finish(stop, task)
    run(go)


# ---- P2: an indexed target keeps its place through the scroll ----
def test_an_indexed_off_screen_tap_hits_the_element_it_named_when_two_matches_come_on_screen():
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        s.tree = [el("e1", "scroll", "", "form", rect=(0, 0, 400, 800)),
                  el("e2", "button", "Account", "", rect=(10, 100, 100, 40))]
        s.offscreen = [el("", "text", "Billing", "", rect=(10, 950, 100, 30)),  # a heading that moves with the form
                       el("", "button", "Account", "", rect=(10, 1000, 100, 40)),
                       el("", "button", "Account", "", rect=(10, 1050, 100, 40))]
        real = s.scroll

        async def scroll(x0, y0, x1, y1):  # the form moves up by 700: the 2nd and the 3rd Account are on screen
            s.tree = [s.tree[0], el("", "text", "Billing", "", rect=(10, 250, 100, 30)), el("e3", "button", "Account", "", rect=(10, 300, 100, 40)),
                      el("e4", "button", "Account", "", rect=(10, 350, 100, 40))]
            s.offscreen = [el("", "button", "Account", "", rect=(10, -600, 100, 40))]
            return await real(x0, y0, x1, y1)
        s.scroll = scroll
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "tap", {"target": {"label": "Account", "index": 1}, "look": False})
        x, y = s.tap_points[-1]
        assert y == 320  # the second Account (300 to 340), not the third (350 to 390)
        await finish(stop, task)
    run(go)


# ---- P2: the daemon's start clears a leftover through the real device ----
def test_the_real_device_clears_a_condition_left_by_a_crash_and_the_daemon_start_calls_it(monkeypatch):
    from tapwright import store
    from tapwright.device import Device

    assert hasattr(Device, "clear_leftover")  # the production operation exists, not only the fake's

    async def go():
        class Dtx:
            async def wait_disconnected(self):
                await asyncio.Event().wait()
        inducer = _fake_instruments(monkeypatch, Dtx())
        d = make()
        real = Device(log=lambda *a: None)
        real.rsd = "tunnel"
        d.session.device = real
        store.NETWORK_FILE.parent.mkdir(parents=True, exist_ok=True)
        store.NETWORK_FILE.write_text(json.dumps({"mode": "lossy", "pid": 1}), encoding="utf-8")
        await d._clear_leftover_network()  # what the connect loop calls at the daemon's first line to the phone
        assert inducer.cleared == 1  # a new instruments connection asked the phone to clear its condition
        assert not store.NETWORK_FILE.exists()
        await d._clear_leftover_network()  # no marker any more: nothing more is asked
        assert inducer.cleared == 1
    run(go)


def test_a_device_without_a_way_to_clear_a_leftover_keeps_the_marker(monkeypatch):
    from tapwright import store

    async def go():
        d = make()

        class Bare:  # a device that can't clear (not the real one)
            rsd = None
        d.session.device = Bare()
        store.NETWORK_FILE.parent.mkdir(parents=True, exist_ok=True)
        store.NETWORK_FILE.write_text("{}", encoding="utf-8")
        await d._clear_leftover_network()
        assert store.NETWORK_FILE.exists()
    run(go)


# ---- the policy looks again at the element a scroll brings on screen ----
@pytest.mark.parametrize("rule", ["block", "confirm"])
def test_a_scroll_that_brings_another_element_on_screen_is_checked_by_the_policy_again(rule):
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        s.tree = [el("e1", "scroll", "", "form", rect=(0, 0, 400, 800))]
        s.offscreen = [el("", "button", "Item", "old", rect=(10, 1000, 100, 40))]
        real = s.scroll

        async def scroll(x0, y0, x1, y1):  # the list refreshes while it moves: what is there now is another row
            s.tree = [s.tree[0], el("e2", "button", "Delete Item", "new", rect=(10, 300, 100, 40))]
            s.offscreen = []
            return await real(x0, y0, x1, y1)
        s.scroll = scroll
        rules = policy.parse({rule: ["Delete"]})
        await d.run_tool("a", "look", {"screenshot": False})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"text": "Item"}, "look": False}, policy=rules)
        assert e.value.code == "not_allowed"
        assert str(e.value).startswith("blocked:" if rule == "block" else "needs_confirmation:")
        assert s.tap_points == []  # nothing was touched
        await finish(stop, task)
    run(go)


# ---- CodeRabbit: index 2 of four matches keeps its place while the others come on screen ----
def test_an_indexed_target_among_four_matches_is_the_same_element_after_the_scroll():
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        s.tree = [el("e1", "scroll", "", "form", rect=(0, 0, 400, 800)),
                  el("e2", "button", "Account", "", rect=(10, 100, 100, 40))]
        s.offscreen = [el("", "text", "Billing", "", rect=(10, 950, 100, 30)), *(el("", "button", "Account", "", rect=(10, y, 100, 40))
                                                                for y in (1000, 1050, 1100))]
        real = s.scroll

        async def scroll(x0, y0, x1, y1):  # B, C and D are on screen now (A above it)
            s.tree = [s.tree[0], el("", "text", "Billing", "", rect=(10, 250, 100, 30))] + [el(f"e{i}", "button", "Account", "", rect=(10, y, 100, 40))
                                   for i, y in ((3, 300), (4, 350), (5, 400))]
            s.offscreen = [el("", "button", "Account", "", rect=(10, -600, 100, 40))]
            return await real(x0, y0, x1, y1)
        s.scroll = scroll
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "tap", {"target": {"label": "Account", "index": 2}, "look": False})
        assert s.tap_points[-1][1] == 370  # C (350 to 390), not D
        await finish(stop, task)
    run(go)


# ---- CodeRabbit: a replacement that fails after it stopped the old condition ends it for the daemon ----
def test_a_failed_replacement_tells_the_daemon_the_old_condition_has_ended(monkeypatch):
    from tapwright.device import Device

    async def go():
        class Dtx:
            async def wait_disconnected(self):
                await asyncio.Event().wait()
        _fake_instruments(monkeypatch, Dtx())  # it offers a 3G profile and nothing for `lossy`
        d = make()
        stop, task = await started(d)
        real = Device(log=lambda *a: None)
        real.rsd, real.via = "tunnel", "USB"
        real.on_network_end = d._network_over
        d.session.device = real
        await d.run_tool("a", "network", {"mode": "3g"})
        assert d._network == "3g"
        with pytest.raises(Refused) as e:
            await d.run_tool("b", "network", {"mode": "lossy"})
        assert e.value.code == "unsupported"
        assert d._network is None and not real._network_holds  # the phone is back to normal, and the daemon says so
        text = (await d.run_tool("a", "look", {"screenshot": False}))["text"]
        assert "the network condition ended" in text
        await real.clear_network()
        await finish(stop, task)
    run(go)


# ---- CodeRabbit: a look that doesn't say `screenshot` takes one, and the log says so ----
def test_a_look_without_the_screenshot_argument_is_logged_as_a_screenshot_look(tmp_path):
    from tapwright import report
    from tapwright.actionlog import ActionLog

    async def go():
        d = make(actions=ActionLog(tmp_path))
        stop, task = await started(d)
        await d.run_tool("a", "look", {})
        await d.run_tool("a", "look", {"screenshot": False})
        await finish(stop, task)
        looks = [e for e in report.read_entries(tmp_path) if e["tool"] == "look"]
        assert [bool(e.get("screenshot")) for e in looks] == [True, False]
    run(go)


# ---- CodeRabbit: BCP 47 extension and private-use sequences ----
@pytest.mark.parametrize("code", ["en-US-u-ca-gregory", "en-x-test", "de-DE-u-co-phonebk-x-priv", "ar", "zh-Hans-CN"])
def test_language_accepts_extension_and_private_use_sequences(code):
    from tapwright import launch
    assert launch.check({"language": code}) == {"language": code}


@pytest.mark.parametrize("code", ["en-u", "en-x", "en-US-u-", "en-u-c", "e", "en--GB"])
def test_language_still_refuses_a_singleton_with_nothing_after_it(code):
    from tapwright import launch
    with pytest.raises(Refused):
        launch.check({"language": code})

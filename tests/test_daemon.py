"""The daemon's phone side with a fake session: connecting, tools, control rules, viewer input."""
import asyncio
import base64
import io
import re

import pytest
from fakes import Clock, FakeDevice, FakeSession, FrameFeeder, VirtualLoop, button, el, image_bytes
from PIL import Image

from tapwright import daemon as daemon_module
from tapwright import ui
from tapwright.control import Controller, Refused
from tapwright.daemon import Daemon, Scope
from tapwright.device import AppNotAnswering, ConnectionLost, DeviceError, PhoneLocked
from tapwright.stream import NO_IDLE_WAIT, PRESETS, Frame

SETTLED = re.compile(r"Settled in \d+\.\d\d s\.")


def unsettled(answer: dict) -> list[str]:
    """The answer's lines without its `Settled in` line, which varies with timing."""
    return [line for line in answer["text"].splitlines() if not SETTLED.fullmatch(line)]


def make(session=None, **options) -> Daemon:
    clock = Clock()
    settings = dict(control=Controller(clock=clock, sleep=clock.sleep), log=lambda *a: None, heartbeat=0.02,
                    backoff=(0.01,), ready_wait=2.0, stream_linger=0, stream_retry=0.01,
                    settle_still=0.03, settle_first_frame=0.01, settle_fallback=0.0, settle_quiet=0.03,
                    shot_wait=0.01, shot_fresh=1.0, pace_tick=None)  # pacing has its own tests
    settings.update(options)
    return Daemon(session or FakeSession(), **settings)


async def started(d: Daemon, until=("live", "locked", "no-device")):
    stop = asyncio.Event()
    task = asyncio.create_task(d.supervise(stop))
    for _ in range(300):
        if d.state in until:
            break
        await asyncio.sleep(0.01)
    return stop, task


async def finish(stop, task):
    stop.set()
    await asyncio.wait_for(task, 2)


def run(scenario):
    asyncio.run(scenario())


def test_connects_and_reports_status():
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.status()
        assert (s["type"], s["protocol"], s["state"], s["via"]) == ("status", 1, "live", "usb")
        assert s["size"] == {"w": 400.0, "h": 800.0} and "drag" in s["accepts"] and s["quality"] == "balanced"
        assert any(isinstance(c, tuple) and c[0] == "settings" and c[1] == {**PRESETS["balanced"], **NO_IDLE_WAIT}
                   for c in d.session.calls)  # every setting, so a reused runner can't keep old ones
        await finish(stop, task)
    run(go)


def test_look_then_act_by_ref():
    async def go():
        d = make()
        stop, task = await started(d)
        r = await d.run_tool("a", "look", {})
        assert r["text"].startswith("Screen 400x800") and r["image"]
        assert r["image_size"] == [600, 1200]  # no stream frame here: the phone's screenshot, resized
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False})
        assert r == {"text": 'Tapped e1 button "OK" id=ok-button @10,10 100x40.', "image": None, "image_size": None}
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False})  # its own taps don't make refs stale
        assert r["text"].startswith("Tapped e1")
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}})  # with what changed after it
        # v1.6: the block is measured from the last list, two answers without one ago, and says so
        assert unsettled(r)[1:] == ["Same screen. Changes:", "No visible change.",
                                    "Note: changes are since your last list, 3 actions ago."] and r["image"] is None
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert "Note:" not in r["text"]  # that block was since the last one
        await finish(stop, task)
    run(go)


def test_a_person_makes_refs_stale_and_ids_are_looked_up_again():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {})
        assert await d.viewer_message({"type": "gesture", "points": [[0, 0.5, 0.5], [60, 0.5, 0.5]]}) is None
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False})
        assert e.value.code == "stale"
        r = await d.run_tool("a", "tap", {"target": {"id": "ok-button"}, "look": False})
        assert r["text"].startswith("Tapped e1 button") and d.session.calls.count("refresh") == 1
        assert r["text"].endswith("\nNote: a person used the phone since your last look.")
        with pytest.raises(Refused) as e:  # the silent re-read isn't a look: the agent's refs are still stale
            await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False})
        assert e.value.code == "stale"
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert "Note:" in r["text"]  # and a look is what clears it
        assert "Note:" not in (await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False}))["text"]
        await finish(stop, task)
    run(go)


def test_pause_refuses_agents():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.viewer_message({"type": "pause", "on": True})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"x": 5, "y": 5}, "look": False})
        assert e.value.code == "paused" and d.status()["paused"] is True
        await d.viewer_message({"type": "pause", "on": False})
        assert (await d.run_tool("a", "tap", {"target": {"x": 5, "y": 5}, "look": False}))["text"] == "Tapped (5, 5)."
        await finish(stop, task)
    run(go)


def test_a_dropped_action_is_reported_not_retried():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.fail["tap"] = ConnectionLost("reset by peer")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"x": 5, "y": 5}, "look": False})
        assert e.value.code == "reconnecting" and d.session.calls.count("tap") == 1
        for _ in range(300):
            if d.state == "live":
                break
            await asyncio.sleep(0.01)
        assert d.state == "live" and d.session.calls.count("ensure") >= 2
        await finish(stop, task)
    run(go)


def test_a_read_is_retried_once_after_reconnecting():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.fail["look"] = ConnectionLost("gone")
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert r["text"].startswith("Screen") and d.session.calls.count("look") == 2
        await finish(stop, task)
    run(go)


def test_needs_setup():
    async def go():
        s = FakeSession()
        s.not_set_up = True
        d = make(s, ready_wait=0.05)
        stop, task = await started(d)
        assert d.state == "no-device" and d.detail.startswith("needs setup")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "look", {})
        assert e.value.code == "not_connected" and "tapwright setup" in str(e.value)
        await finish(stop, task)
    run(go)


def test_locked_phone():
    async def go():
        s = FakeSession()
        s.is_locked = True
        d = make(s)
        stop, task = await started(d, until=("locked",))
        assert d.state == "locked" and d.detail == "unlock the phone"
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"x": 1, "y": 1}})
        assert e.value.code == "locked"
        assert (await d.run_tool("a", "look", {"screenshot": False}))["text"].startswith("Screen")
        assert (await d.viewer_message({"type": "button", "name": "home"}))["code"] == "locked"
        await finish(stop, task)
    run(go)


def test_personal_mode_without_an_app_list_runs_nothing():
    """LAUNCH-PLAN D5: on the owner's everyday phone the agent may only use the apps it was given. It fails closed."""
    async def go():
        d = make()
        stop, task = await started(d)
        for tool, args in (("look", {}), ("tap", {"target": {"x": 5, "y": 5}}), ("apps", {}),
                           ("open_app", {"app": "com.example.app"}), ("open_url", {"url": "https://example.com"})):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", tool, args, scope=Scope(()))
            assert e.value.code == "not_allowed" and str(e.value).startswith("no_apps: "), tool
            assert "--apps" in str(e.value) and "--dedicated" in str(e.value)
        assert d.session.actions() == []
        await finish(stop, task)
    run(go)


def test_a_moment_of_springboard_from_webdriveragent_is_asked_again_not_refused():
    """Seen on the phone 2026-11-10: twice in forty calls WebDriverAgent named SpringBoard as the app in front
    while the app under test showed its own alert. The phone's process list had the app in the foreground."""
    async def go():
        s = FakeSession()
        s.app_names = {"com.example.app": "Example"}
        s.tree = [button("e1", "OK", "ok-button", rect=(10, 400, 100, 40))]  # below the banners (not what is tested)
        d = make(s)
        stop, task = await started(d)
        mine = Scope(("com.example.app",))
        s.foreground = "com.example.app"
        await d.run_tool("a", "look", {"screenshot": False}, scope=mine)
        daemon_module.FRONT_RETRY = 0.001  # (the pause between two questions, not what is under test)
        answers = ["com.apple.springboard", "com.apple.springboard", "com.example.app"]

        async def flaky():
            return answers.pop(0) if answers else "com.example.app"

        async def running():
            return ["com.example.app"]
        s.foreground_bundle_id, s.device.foreground_apps = flaky, running
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False}, scope=mine)
        assert r["text"].startswith("Tapped") and answers == []
        # the real home screen: the process list has no given app in the foreground, so nothing is asked twice
        asked: list[int] = []

        async def home():
            asked.append(1)
            return "com.apple.springboard"

        async def nothing():
            return []
        s.foreground_bundle_id, s.device.foreground_apps = home, nothing
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False}, scope=mine)
        assert e.value.code == "not_allowed" and len(asked) == 1
        # and SpringBoard that stays, with the app still listed as running in front: refused after the retries
        asked.clear()
        s.device.foreground_apps = running
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False}, scope=mine)
        assert e.value.code == "not_allowed" and len(asked) == 1 + daemon_module.FRONT_RETRIES
        await finish(stop, task)
    pause = daemon_module.FRONT_RETRY
    try:
        run(go)
    finally:
        daemon_module.FRONT_RETRY = pause


def test_personal_mode_keeps_the_agent_in_the_apps_it_was_given():
    async def go():
        s = FakeSession()
        s.installed.append({"bundle_id": "com.example.mail", "name": "Mail box", "version": "1", "build": "1"})
        s.app_names = {"com.example.app": "Example", "com.example.mail": "Mail box"}
        d = make(s)
        stop, task = await started(d)
        mine = Scope(("com.example.app",))
        s.foreground = "com.example.app"
        assert (await d.run_tool("a", "look", {"screenshot": False}, scope=mine))["text"].startswith("Screen")
        assert (await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False}, scope=mine))["text"].startswith("Tapped")
        assert (await d.run_tool("a", "apps", {}, scope=mine))["text"].splitlines()[0].startswith("com.example.app")
        assert "com.example.mail" not in (await d.run_tool("a", "apps", {}, scope=mine))["text"]
        for app in ("com.example.mail", "Mail box"):  # by bundle id or by name: not its app
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "open_app", {"app": app}, scope=mine)
            assert e.value.code == "not_allowed" and str(e.value).startswith("outside_apps: ")
        assert s.launched == []
        before = len(s.actions())
        s.foreground = "com.example.mail"  # something took the phone elsewhere
        s.app = ("Mail box", "com.example.mail")  # what a read there would say
        with pytest.raises(Refused) as e:  # a look reads, learns where it is, and shows nothing of it
            await d.run_tool("a", "look", {}, scope=mine)
        assert e.value.code == "not_allowed" and str(e.value).startswith("outside_apps: Mail box (com.example.mail)")
        before = len(s.actions())
        for tool, args in (("tap", {"target": {"x": 5, "y": 5}}), ("type_text", {"text": "x"}),
                           ("swipe", {"x0": 1, "y0": 1, "x1": 9, "y1": 9}), ("wait_for", {"text": "OK"})):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", tool, args, scope=mine)
            assert e.value.code == "not_allowed", tool  # AGENT-TOOLS v1.5 (it was `unsupported`)
            assert str(e.value).startswith("outside_apps: Mail box (com.example.mail) is in front and isn't one of the apps")
        assert len(s.actions()) == before  # nothing was read or touched there
        r = await d.run_tool("a", "open_app", {"app": "Example"}, scope=mine)  # the way back always works
        s.app = ("Example", "com.example.app")
        assert r["text"].startswith("Opened Example") and s.launched == ["com.example.app"]
        s.foreground = None  # the phone can't say: closed, not open
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"x": 5, "y": 5}}, scope=mine)
        assert "can't say which app" in str(e.value)
        await finish(stop, task)
    run(go)


def test_an_answer_in_personal_mode_never_shows_another_apps_screen():
    async def go():
        s = FakeSession()
        s.tree = [button("e1", "OK", "ok-button", rect=(10, 400, 100, 40))]  # below the banners (not what is tested)
        d = make(s)
        stop, task = await started(d)
        mine = Scope(("com.example.app",))
        s.foreground = "com.example.app"
        await d.run_tool("a", "look", {"screenshot": False}, scope=mine)
        s.app = ("Mail box", "com.example.mail")  # the tap opens another app: the answer would list its screen
        s.tree = [button("e1", "Private thing", "private-row")]
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "screenshot": True}, scope=mine)
        assert r["text"].startswith("Tapped e1") and "Private thing" not in r["text"] and r["image"] is None
        assert r["text"].splitlines()[1].startswith("Note: outside_apps: Mail box (com.example.mail) is in front")
        r = await d.run_tool("a", "tap", {"target": {"x": 1, "y": 1}}, scope=None)  # a dedicated device: no limit
        assert "Private thing" in r["text"] or "No visible change" in r["text"]
        await finish(stop, task)
    run(go)


def rates(session) -> list[int]:
    """The stream frame rates the daemon set, in order (connect and quality settings included)."""
    return [c[1]["mjpegServerFramerate"] for c in session.calls
            if isinstance(c, tuple) and c[0] == "settings" and "mjpegServerFramerate" in c[1]]


async def until(check, tries: int = 300) -> None:
    for _ in range(tries):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("never happened")


def paced(**options) -> Daemon:
    """A daemon whose stream pace reacts within tens of ms: quiet after 50 ms of no agent, person or change."""
    return make(pace_tick=0.01, pace_quiet=(0.05, 0.05, 0.05), **options)


def test_a_still_screen_slows_the_stream_and_an_agent_call_quickens_it_first():
    """The stream costs the phone about 2.6% of a core per frame a second (88% at balanced, DVT, 2026-11-10)."""
    async def go():
        d = paced()
        stop, task = await started(d)
        feeder = FrameFeeder(d.hub).start(then=b"same picture")
        await until(lambda: rates(d.session)[-1:] == [daemon_module.STILL_FPS])
        r = await d.run_tool("a", "tap", {"target": {"x": 5, "y": 5}, "look": False})
        assert r["text"] == "Tapped (5, 5)."
        calls = d.session.calls
        quick = max(i for i, c in enumerate(calls) if isinstance(c, tuple) and c[0] == "settings"
                    and c[1].get("mjpegServerFramerate") == 30)
        assert quick < calls.index("tap")  # back to the preset's rate before the gesture, for the settle
        await until(lambda: rates(d.session)[-1:] == [daemon_module.STILL_FPS])  # quiet again afterwards
        await feeder.stop()
        await finish(stop, task)
    run(go)


def test_a_persons_input_quickens_the_stream_before_it_reaches_the_phone():
    async def go():
        d = paced()
        stop, task = await started(d)
        feeder = FrameFeeder(d.hub).start(then=b"same picture")
        await until(lambda: rates(d.session)[-1:] == [daemon_module.STILL_FPS])
        assert await d.viewer_message({"type": "gesture", "points": [[0, 0.5, 0.5], [60, 0.5, 0.5]]}) is None
        calls = d.session.calls
        quick = max(i for i, c in enumerate(calls) if isinstance(c, tuple) and c[0] == "settings"
                    and c[1].get("mjpegServerFramerate") == 30)
        assert quick < calls.index("tap")  # so the picture after the tap comes at full rate (V1)
        await feeder.stop()
        await finish(stop, task)
    run(go)


def test_a_changing_screen_keeps_the_stream_fast_and_no_stream_sends_nothing():
    async def go():
        d = paced()
        stop, task = await started(d)
        await asyncio.sleep(0.2)
        assert rates(d.session) == [30]  # no frames, so no pacing: only the connect's own rate
        feeder = FrameFeeder(d.hub).start(churn=True)  # a new picture every frame: an animation
        await asyncio.sleep(0.3)
        assert daemon_module.STILL_FPS not in rates(d.session)
        await feeder.stop()
        await finish(stop, task)
    run(go)


def test_a_quality_change_sets_its_rate_and_the_still_rate_never_exceeds_it():
    async def go():
        d = paced()
        stop, task = await started(d)
        feeder = FrameFeeder(d.hub).start(then=b"same picture")
        await until(lambda: rates(d.session)[-1:] == [daemon_module.STILL_FPS])
        await d.set_quality("light")
        assert 10 in rates(d.session)
        await until(lambda: rates(d.session)[-1:] == [min(daemon_module.STILL_FPS, 10)])
        await feeder.stop()
        await finish(stop, task)
    run(go)


async def until_stuck(d: Daemon) -> None:
    for _ in range(300):
        if d.stuck is not None:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"never stuck: {d.state} {d.detail}")


def test_a_frozen_app_is_named_at_once_and_close_app_ends_it():
    """2026-11-10: Lab froze in front. Agents were told 'reconnecting… wait a few seconds' for hours."""
    async def go():
        s = FakeSession()
        s.stuck = AppNotAnswering("com.example.lab", "Lab")
        d = make(s, ready_wait=2.0)
        stop, task = await started(d, until=())
        await until_stuck(d)
        assert d.state == "not-answering" and d.detail.startswith("Lab (com.example.lab) is in front")  # the viewer says so too
        began = asyncio.get_running_loop().time()
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "look", {"screenshot": False})
        assert asyncio.get_running_loop().time() - began < 1.0  # not after the 2 s wait for a connection
        assert e.value.code == "not_answering"  # AGENT-TOOLS v1.4
        assert str(e.value) == ("Lab (com.example.lab) is in front and isn't answering, so nothing on the phone can be read. "
                                "close_app can still close it; then open_app it again.")
        assert (await d.viewer_message({"type": "button", "name": "home"}))["code"] == "not_answering"
        assert "wait a few seconds" not in str(e.value)
        with pytest.raises(Refused) as e:  # only the stuck app can be closed while it blocks the phone
            await d.run_tool("a", "close_app", {"app": "Settings"})
        assert e.value.code == "not_answering" and s.device.killed == []
        r = await d.run_tool("a", "close_app", {"app": "lab"})
        assert r["text"] == "Closed Lab (com.example.lab), which wasn't answering."
        assert s.device.killed == ["com.example.lab"]
        r = await d.run_tool("a", "look", {"screenshot": False})  # waits for the reconnect the close starts
        assert r["text"].startswith("Screen") and d.state == "live" and d.stuck is None
        assert "reset" not in s.calls  # a stuck app is no reason to rebuild the tunnel
        await finish(stop, task)
    run(go)


def test_an_app_that_freezes_mid_session_answers_not_answering_and_then_recovers():
    """The lab's 20 s freeze: a look during it must end with a code, and the next one must work by itself."""
    async def go():
        s = FakeSession()
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        frozen = AppNotAnswering("com.example.lab", "Lab")
        s.stuck = frozen  # the reconnect finds it still frozen
        s.fail["look"] = s.fail["snapshot"] = frozen  # the read is cut and the app named
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "look", {"screenshot": False})
        assert e.value.code == "not_answering" and str(e.value).startswith("Lab (com.example.lab) is in front")
        assert d.state == "not-answering" and d.stuck is not None
        s.stuck = None  # the app answers again: nobody has to do anything
        for _ in range(300):
            if d.state == "live":
                break
            await asyncio.sleep(0.01)
        assert d.state == "live" and d.stuck is None
        assert (await d.run_tool("a", "look", {"screenshot": False}))["text"].startswith("Screen")
        await finish(stop, task)
    run(go)


def test_a_phone_locked_at_connect_answers_locked():
    async def go():
        s = FakeSession()
        s.stuck = PhoneLocked("the phone is locked")
        d = make(s)
        stop, task = await started(d, until=())
        await until_stuck(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"x": 1, "y": 1}})
        assert e.value.code == "locked" and "unlock" in str(e.value)
        with pytest.raises(Refused) as e:  # nothing to close: the phone itself is locked
            await d.run_tool("a", "close_app", {"app": "Example"})
        assert e.value.code == "locked" and s.device.killed == []
        s.stuck = None  # the user unlocks it; the next connect (within the backoff) finds it so
        for _ in range(300):
            if d.state == "live":
                break
            await asyncio.sleep(0.01)
        assert d.stuck is None
        assert (await d.run_tool("a", "look", {"screenshot": False}))["text"].startswith("Screen")
        await finish(stop, task)
    run(go)


def test_viewer_input_reaches_the_phone_and_is_announced():
    async def go():
        d = make()
        stop, task = await started(d)
        queue = d.subscribe()
        for message in ({"type": "scroll", "x": 0.5, "y": 0.5, "dx": 0, "dy": 0.25}, {"type": "text", "text": "hi"},
                        {"type": "key", "key": "enter"}, {"type": "button", "name": "home"},
                        {"type": "gesture", "points": [[0, 0.1, 0.5], [300, 0.9, 0.5]]}):
            assert await d.viewer_message(message) is None
        assert d.session.actions()[-5:] == ["swipe", "type_text", "key:enter", "button:home", "path"]
        announced = []
        while not queue.empty():
            m = queue.get_nowait()
            if m["type"] == "action":
                announced.append((m["by"], m["kind"]))
        assert announced == [("person", "scroll"), ("person", "text"), ("person", "enter"), ("person", "home"),
                             ("person", "drag")]
        assert await d.viewer_message({"type": "overlay", "on": True}) is None  # part 2: ignored for now
        assert await d.viewer_message({"type": "something-new"}) is None
        await finish(stop, task)
    run(go)


def test_bad_viewer_messages_and_quality():
    async def go():
        d = make()
        stop, task = await started(d)
        assert (await d.viewer_message({"type": "gesture", "points": []}))["code"] == "unsupported"
        assert (await d.viewer_message({"type": "button", "name": "power"}))["code"] == "unsupported"
        assert (await d.viewer_message({"type": "quality", "preset": "ultra"}))["code"] == "unsupported"
        assert await d.viewer_message({"type": "quality", "preset": "light"}) is None
        assert d.preset == "light" and d.status()["quality"] == "light"
        await finish(stop, task)
    run(go)


def test_other_tools():
    async def go():
        d = make()
        stop, task = await started(d)
        assert (await d.run_tool("a", "apps", {}))["text"] == "com.example.app  Example 1.0 (7)\n* = in front."
        assert "Closed com.example.x." in (await d.run_tool("a", "close_app", {"app": "com.example.x"}))["text"]
        assert (await d.run_tool("a", "alert", {"action": "text"}))["text"] == "alert texted"
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "format_phone", {})
        assert e.value.code == "unsupported" and "look" in str(e.value)  # it names the tools there are
        await finish(stop, task)
    run(go)


def test_a_drop_noticed_while_reconnecting_does_not_spin():
    async def go():
        s = FakeSession()
        s.ensure_delay = 0.1
        d = make(s)
        stop, task = await started(d)
        s.ensure_delay = 0.1
        d.lost("first")
        await asyncio.sleep(0.03)  # the reconnect is running
        d.lost("second")
        for _ in range(300):
            if d.state == "live":
                break
            await asyncio.sleep(0.01)
        assert d.state == "live"
        ensures = s.calls.count("ensure")
        before = s.calls.count("locked")
        await asyncio.sleep(0.2)
        assert s.calls.count("locked") > before + 2 and s.calls.count("ensure") == ensures
        await finish(stop, task)
    run(go)


def test_an_agent_look_during_a_reconnect_waits_for_it():
    async def go():
        s = FakeSession()
        d = make(s)
        stop, task = await started(d)
        s.ensure_delay = 0.1
        d.lost("gone")
        await asyncio.sleep(0.03)
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert r["text"].startswith("Screen")
        calls = s.calls
        assert max(i for i, c in enumerate(calls) if c == "ensure") < max(i for i, c in enumerate(calls) if c == "look")
        await finish(stop, task)
    run(go)


def test_a_persons_tap_during_a_reconnect_is_refused_at_once():
    async def go():
        s = FakeSession()
        d = make(s)
        stop, task = await started(d)
        s.ensure_delay = 0.1
        d.lost("gone")
        await asyncio.sleep(0.03)
        e = await d.viewer_message({"type": "gesture", "points": [[0, 0.5, 0.5], [60, 0.5, 0.5]]})
        assert e["type"] == "error" and e["code"] == "reconnecting"
        await asyncio.sleep(0.3)
        assert "tap" not in s.calls
        await finish(stop, task)
    run(go)


def test_a_coordinate_action_does_not_refresh_stale_refs():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {})
        await d.viewer_message({"type": "gesture", "points": [[0, 0.5, 0.5], [60, 0.5, 0.5]]})
        r = await d.run_tool("a", "tap", {"target": {"x": 5, "y": 5}, "look": False})
        assert r["text"] == "Tapped (5, 5).\nNote: a person used the phone since your last look."
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False})
        assert e.value.code == "stale"
        await finish(stop, task)
    run(go)


def test_busy_while_a_person_drives():
    async def go():
        d = make()
        stop, task = await started(d)
        d.control.person_active = lambda: True
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"x": 5, "y": 5}, "look": False})
        assert e.value.code == "busy"
        await finish(stop, task)
    run(go)


def test_three_failing_heartbeats_lead_to_a_reconnect():
    async def go():
        s = FakeSession()
        d = make(s)
        stop, task = await started(d)
        ensures = s.calls.count("ensure")
        s.locked_errors = 3
        for _ in range(300):
            if s.calls.count("ensure") > ensures:
                break
            await asyncio.sleep(0.01)
        assert s.calls.count("ensure") > ensures
        await finish(stop, task)
    run(go)


def test_a_missing_argument_is_refused_up_front():
    async def go():
        d = make()
        stop, task = await started(d)
        generation = d.control.generation
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "open_app", {})
        assert e.value.code == "bad_params" and "app" in str(e.value)
        assert d.control.generation == generation and "launch" not in d.session.calls
        await finish(stop, task)
    run(go)


class OneFrameConnection:
    FRAME = b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 4\r\n\r\nJPEG\r\n"

    def __init__(self) -> None:
        self.sent = False

    async def recv_any(self, n):
        if not self.sent:
            self.sent = True
            return self.FRAME
        await asyncio.sleep(30)

    async def close(self):
        pass


class StreamingDevice(FakeDevice):
    async def open_stream(self):
        return OneFrameConnection()


def test_a_viewer_who_starts_during_the_connect_gets_a_frame_soon_after_live():
    async def go():
        s = FakeSession()
        s.device = StreamingDevice()
        s.ensure_delay = 0.3
        d = make(s, stream_retry=5.0)  # a pump that gave up and backed off would miss the bound
        stop = asyncio.Event()
        task = asyncio.create_task(d.supervise(stop))
        try:
            async with d.pump.watching() as hub:
                assert d.state != "live"
                while d.state != "live":
                    await asyncio.sleep(0.01)
                t_live = asyncio.get_running_loop().time()
                frame = await hub.next_after(0, timeout=5)
                assert frame is not None and frame.jpeg == b"JPEG"
                assert asyncio.get_running_loop().time() - t_live < 1.0
        finally:
            await d.pump.stop()
            await finish(stop, task)
    run(go)


def test_no_phone_fails_fast():
    async def go():
        s = FakeSession()
        s.not_set_up = True
        d = make(s, ready_wait=2.0)
        stop, task = await started(d)
        t0 = asyncio.get_running_loop().time()
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "look", {})
        assert e.value.code == "not_connected" and asyncio.get_running_loop().time() - t0 < 0.5
        await finish(stop, task)
    run(go)


def test_a_target_that_is_not_there_is_an_error_and_keeps_refs_current():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {})
        generation = d.control.generation
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"ref": "e9"}, "look": False})
        assert e.value.code == "not_found" and "e9" in str(e.value)
        assert d.control.generation == generation  # nothing happened on the phone
        assert (await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False}))["text"].startswith("Tapped e1")
        await finish(stop, task)
    run(go)


def test_bad_arguments_are_bad_params():
    async def go():
        d = make()
        stop, task = await started(d)
        generation = d.control.generation
        for tool, args, field in (("tap", {}, "target"), ("tap", {"target": {"id": "a", "index": "abc"}}, "index"),
                                  ("swipe", {"x0": "abc", "y0": 0, "x1": 1, "y1": 1}, "x0"),
                                  ("swipe", {"x0": 0, "y0": 0, "x1": 1, "y1": float("nan")}, "y1"),
                                  ("wait", {"seconds": "soon"}, "seconds"),
                                  ("toggle", {"target": {"ref": "e1"}, "value": "yes"}, "value"),
                                  ("open_app", {"app": 5}, "app"),
                                  ("close_app", {"app": ""}, "app"),
                                  ("long_press", {"target": {"x": 1, "y": 1}, "seconds": "long"}, "seconds"),
                                  ("scroll", {"direction": "sideways"}, "direction"),
                                  ("scroll", {"amount": "lots"}, "amount")):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", tool, args)
            assert e.value.code == "bad_params" and field in str(e.value), (tool, args)
        assert d.control.generation == generation and d.session.actions() == []  # nothing reached the phone
        await finish(stop, task)
    run(go)


def test_a_crash_inside_a_tool_is_internal_and_logged():
    async def go():
        logs = []
        d = make(log=logs.append)
        stop, task = await started(d)
        d.session.fail["tap"] = RuntimeError("boom")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"x": 5, "y": 5}, "look": False})
        assert e.value.code == "internal" and "boom" in str(e.value)
        assert any("Traceback" in m and "boom" in m for m in logs)
        d.session.fail["look"] = RuntimeError("bang")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "look", {})
        assert e.value.code == "internal" and "bang" in str(e.value)
        assert (await d.run_tool("a", "look", {"screenshot": False}))["text"].startswith("Screen")  # still up
        await finish(stop, task)
    run(go)


def test_a_value_error_from_phone_data_is_internal_not_bad_params():
    async def go():
        logs = []
        d = make(log=logs.append)
        stop, task = await started(d)
        d.session.fail["look"] = ValueError("could not convert string to float: 'x'")  # an unparsable rect
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "look", {})
        assert e.value.code == "internal" and any("Traceback" in m for m in logs)
        d.session.fail["tap"] = ValueError("bad data")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"x": 5, "y": 5}, "look": False})
        assert e.value.code == "internal"
        await finish(stop, task)
    run(go)


def test_a_phone_side_error_is_internal_but_not_called_a_bug():
    async def go():
        logs = []
        d = make(log=logs.append)
        stop, task = await started(d)
        d.session.fail["tap"] = DeviceError("HTTP 500: no such element")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"x": 5, "y": 5}, "look": False})
        assert e.value.code == "internal" and "the phone side failed" in str(e.value)
        assert "HTTP 500" in str(e.value) and "bug" not in str(e.value)
        assert not any("Traceback" in m for m in logs)
        await finish(stop, task)
    run(go)


def test_a_look_that_fails_after_an_action_is_a_note():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.fail["snapshot"] = RuntimeError("boom")
        r = await d.run_tool("a", "tap", {"target": {"x": 5, "y": 5}})
        assert unsettled(r) == ["Tapped (5, 5).", "Note: couldn't look afterwards: boom."]
        assert SETTLED.fullmatch(r["text"].splitlines()[1])  # the settle line sits before the notes
        await finish(stop, task)
    run(go)


def test_a_target_is_checked_before_anything_is_sent():
    async def go():
        d = make()
        stop, task = await started(d)
        generation, calls = d.control.generation, list(d.session.calls)
        for args in ({"target": {"ref": "e1", "id": "x"}}, {"target": {"x": 1}}, {"target": "e1"},
                     {"target": {"id": "ok-button", "index": "0"}}):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "tap", args)
            assert e.value.code == "bad_params"
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "type_text", {"text": "hi", "target": {"label": "a", "text": "a"}})
        assert e.value.code == "bad_params"
        assert d.control.generation == generation and d.session.calls == calls  # nothing queued or sent
        await finish(stop, task)
    run(go)


def test_an_ambiguous_target_lists_candidates_and_nothing_is_tapped():
    async def go():
        d = make()
        d.session.tree = [button("e1", "7"), button("e2", "7", "seven-again", rect=(10, 60, 100, 40)),
                          button("e3", "8")]
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"label": "7"}, "look": False})
        assert e.value.code == "ambiguous" and str(e.value) == (
            '2 elements match label "7".\nCandidates:\n  e1 button "7" @10,10 100x40 [tap]\n'
            '  e2 button "7" id=seven-again @10,60 100x40 [tap]\nRetry with one of the refs, or add "index" (0-based).')
        assert "tap" not in d.session.calls
        r = await d.run_tool("a", "tap", {"target": {"label": "7", "index": 1}, "look": False})
        assert r["text"].startswith('Tapped e2 button "7" id=seven-again')
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"label": "7", "index": 5}, "look": False})
        assert e.value.code == "not_found"
        r = await d.run_tool("a", "tap", {"target": {"text": "8"}, "look": False})  # text, exact: the one "8"
        assert r["text"].startswith('Tapped e3 button "8"')  # the re-reads in between moved nothing: its ref stays
        await finish(stop, task)
    run(go)


def test_id_label_and_text_use_the_agents_list_while_it_is_current():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        for target in ({"id": "ok-button"}, {"label": "ok"}, {"text": "o"}):
            await d.run_tool("a", "tap", {"target": target})  # with a look after it: the list stays current
        assert d.session.calls.count("refresh") == 0
        await finish(stop, task)
    run(go)


def test_a_target_missing_from_a_current_list_is_read_again_before_not_found():
    """A list read mid-redraw can miss elements while the stream says nothing changed since; trusted as it was,
    every later id target was not_found until the screen moved: 43 calls in a row on the lab (2026-11-09)."""
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.tree = []  # a read that caught nothing
        await d.run_tool("a", "look", {"screenshot": False})
        d.session.tree = [button("e1", "OK", "ok-button")]  # what the screen shows
        r = await d.run_tool("a", "tap", {"target": {"id": "ok-button"}, "look": False})
        assert r["text"].startswith('Tapped e1 button "OK"') and d.session.calls.count("refresh") == 1
        await d.run_tool("a", "look", {"screenshot": False})
        with pytest.raises(Refused) as e:  # what truly isn't there is still not_found, after one read
            await d.run_tool("a", "tap", {"target": {"id": "gone"}, "look": False})
        assert e.value.code == "not_found" and d.session.calls.count("refresh") > 1  # read again first
        await finish(stop, task)
    run(go)


def test_a_list_that_may_be_out_of_date_is_read_again_for_id_label_and_text():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        d.session.tree = [button("e1", "Done", "done-button")]  # the screen changed behind the agent's back
        d.control.clock.t += 5.0  # older than LIVE_AGE
        r = await d.run_tool("a", "tap", {"target": {"id": "done-button"}, "look": False})
        assert r["text"].startswith('Tapped e2 button "Done"') and d.session.calls.count("refresh") == 1  # e1 is gone
        # an action that returned no list: the screen has probably changed
        d.session.tree = [button("e1", "Back", "back-button")]
        r = await d.run_tool("a", "tap", {"target": {"label": "back"}, "look": False})
        assert r["text"].startswith('Tapped e3 button "Back"') and d.session.calls.count("refresh") == 2
        assert (await d.run_tool("a", "tap", {"target": {"ref": "e3"}, "look": False}))["text"]  # refs don't re-read
        assert d.session.calls.count("refresh") == 2
        await d.run_tool("a", "look", {"screenshot": False})  # a look makes it current again
        await d.run_tool("a", "tap", {"target": {"id": "back-button"}, "look": False})
        assert d.session.calls.count("refresh") == 2
        await finish(stop, task)
    run(go)


def test_a_points_tap_never_reads_the_screen():
    async def go():
        d = make()
        stop, task = await started(d)
        d.control.clock.t += 10.0
        await d.run_tool("a", "tap", {"target": {"x": 5, "y": 5}, "look": False})
        assert d.session.calls.count("refresh") == 0 and "look" not in d.session.calls
        await finish(stop, task)
    run(go)


def test_a_person_makes_a_ref_stale_but_not_an_id_text_or_label():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.viewer_message({"type": "gesture", "points": [[0, 0.5, 0.5], [60, 0.5, 0.5]]})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False})
        assert e.value.code == "stale"
        for target in ({"id": "ok-button"}, {"label": "OK"}, {"text": "ok"}):
            await d.viewer_message({"type": "gesture", "points": [[0, 0.5, 0.5], [60, 0.5, 0.5]]})
            before = d.session.calls.count("refresh")
            r = await d.run_tool("a", "tap", {"target": target, "look": False})
            assert r["text"].startswith("Tapped e1 button") and d.session.calls.count("refresh") == before + 1
        await finish(stop, task)
    run(go)


def test_type_text_takes_the_same_target_or_none():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.tree = [ui.Element("e1", "input", "Name", "name-field", rect=(10, 10, 100, 40), actions=("tap", "type"))]
        await d.run_tool("a", "look", {"screenshot": False})
        r = await d.run_tool("a", "type_text", {"text": "hi", "look": False})  # the only input
        assert r["text"] == 'Typed into e1 input "Name" id=name-field @10,10 100x40.'
        r = await d.run_tool("a", "type_text", {"text": "hi", "target": {"id": "name-field"}, "look": False})
        assert r["text"] == 'Typed into e1 input "Name" id=name-field @10,10 100x40.'
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "type_text", {"text": "hi", "target": {"ref": "e9"}, "look": False})
        assert e.value.code == "not_found" and "e9" in str(e.value)
        await finish(stop, task)
    run(go)


def test_a_silent_re_read_keeps_the_agents_refs_on_the_same_elements():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})  # the agent's list: e1 = OK at (10,10)
        d.session.tree = [button("e1", "Other", "other-button"), button("e2", "OK", "ok-button", rect=(10, 300, 100, 40))]
        d.control.clock.t += 5.0
        r = await d.run_tool("a", "tap", {"target": {"id": "ok-button"}, "look": False})
        assert r["text"].startswith('Tapped e1 button "OK" id=ok-button @10,300') and d.session.calls.count("refresh") == 1
        assert [e.ref for e in d.control.client("a").items] == ["e2", "e1"]  # Other is new: e2
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False})  # the old ref taps OK's new place
        assert r["text"].startswith('Tapped e1 button "OK"') and d.session.last_point == (60.0, 320.0)
        await finish(stop, task)
    run(go)


def test_a_ref_whose_element_is_gone_after_a_re_read_is_not_found():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        d.session.tree = [button("e1", "Other", "other-button")]
        d.control.clock.t += 5.0
        await d.run_tool("a", "tap", {"target": {"id": "other-button"}, "look": False})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False})
        assert e.value.code == "not_found" and str(e.value) == "e1 isn't on screen any more; look again."
        await finish(stop, task)
    run(go)


def test_a_point_for_type_text_is_a_lookup_on_a_current_list():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.tree = [ui.Element("e1", "input", "Name", rect=(10, 10, 100, 40), actions=("tap", "type"))]
        await d.run_tool("a", "look", {"screenshot": False})
        d.control.clock.t += 5.0
        await d.run_tool("a", "type_text", {"text": "hi", "target": {"x": 20, "y": 20}, "look": False})
        assert d.session.calls.count("refresh") == 1
        await finish(stop, task)
    run(go)


def test_a_failed_look_after_an_action_makes_the_next_lookup_read_again():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        d.session.fail["snapshot"] = DeviceError("boom")
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert "couldn't look afterwards" in r["text"]
        await d.run_tool("a", "tap", {"target": {"id": "ok-button"}, "look": False})
        assert d.session.calls.count("refresh") == 1
        await finish(stop, task)
    run(go)


def test_a_dropped_connection_makes_refs_stale_with_its_own_message():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        d.session.fail["tap"] = ConnectionLost("gone")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False})
        assert e.value.code == "reconnecting"
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False})
        assert e.value.code == "stale" and "connection to the phone dropped" in str(e.value)
        await d.run_tool("a", "look", {"screenshot": False})  # a look makes refs good again
        assert (await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False}))["text"]
        await finish(stop, task)
    run(go)


def test_a_list_is_current_until_exactly_live_age():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        d.control.clock.t += 1.9
        await d.run_tool("a", "tap", {"target": {"id": "ok-button"}, "look": False})
        assert d.session.calls.count("refresh") == 0
        await d.run_tool("a", "look", {"screenshot": False})
        d.control.clock.t += 2.0  # exactly LIVE_AGE: no longer current
        await d.run_tool("a", "tap", {"target": {"id": "ok-button"}, "look": False})
        assert d.session.calls.count("refresh") == 1
        await finish(stop, task)
    run(go)


def test_after_a_re_read_the_lines_carry_the_agents_refs():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})  # e1 = OK
        d.session.tree = [button("e1", "7"), button("e2", "7", rect=(10, 60, 100, 40)), button("e3", "Done", "done")]
        d.control.clock.t += 5.0
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"label": "7"}, "look": False})
        assert e.value.code == "ambiguous" and 'e2 button "7"' in str(e.value) and "Retry with one of the refs" in str(e.value)
        assert 'e3 button "7"' in str(e.value)  # e1 (OK) is gone; the new elements count on from it
        r = await d.run_tool("a", "tap", {"target": {"ref": "e3"}, "look": False})  # the refs in the candidates work
        assert r["text"].startswith('Tapped e3 button "7"')
        await finish(stop, task)
    run(go)


def test_a_dropped_connection_makes_an_id_target_re_read():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        d.control.clients["a"].dropped = True
        await d.run_tool("a", "tap", {"target": {"id": "ok-button"}, "look": False})
        assert d.session.calls.count("refresh") == 1
        await finish(stop, task)
    run(go)


def test_an_action_answers_with_what_changed_and_the_refs_stay():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        d.session.tree = [button("e1", "OK", "ok-button"), button("e2", "Done", "done-button", rect=(10, 60, 100, 40))]
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert unsettled(r) == ['Tapped e1 button "OK" id=ok-button @10,10 100x40.', "Same screen. Changes:",
                                '+ e2 button "Done" id=done-button @10,60 100x40 [tap]']
        assert SETTLED.fullmatch(r["text"].splitlines()[-1])
        assert r["image"] is None  # a screenshot only when asked
        d.session.tree = [button("e1", "OK", "ok-button")]
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert unsettled(r)[1:] == ["Same screen. Changes:", '- button "Done"']
        d.session.tree = [button("e1", "OK", "ok-button"), button("e2", "More", "more", rect=(10, 60, 100, 40))]
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert '+ e3 button "More"' in r["text"]  # e2 went away once: its number is not reused
        await finish(stop, task)
    run(go)


def test_a_new_app_or_a_screen_the_agent_has_not_seen_gets_the_full_list():
    async def go():
        d = make()
        stop, task = await started(d)
        r = await d.run_tool("a", "tap", {"target": {"x": 5, "y": 5}})  # no look yet
        assert unsettled(r)[1:4] == ["Screen 400x800 pt @3x · 1 elements", "App: Example (com.example.app)",
                                               'e1 button "OK" id=ok-button @10,10 100x40 [tap]']
        d.session.app = ("Settings", "com.apple.Preferences")
        d.session.tree = [button("e1", "Wi-Fi"), button("e2", "Bluetooth", rect=(10, 60, 100, 40))]
        r = await d.run_tool("a", "tap", {"target": {"text": "OK"}})
        assert unsettled(r)[1:3] == ["Screen 400x800 pt @3x · 2 elements",
                                               "App: Settings (com.apple.Preferences)"]
        d.session.tree = [button("e1", "Wi-Fi")]  # the app didn't change: a change list, e2 is gone
        assert [x.ref for x in d.control.client("a").items] == ["e2", "e3"]  # the app changed: nothing carried
        r = await d.run_tool("a", "tap", {"target": {"ref": "e2"}})  # e1 (OK) is not on this screen
        assert unsettled(r)[1:] == ["Same screen. Changes:", '- button "Bluetooth"']
        assert [x.ref for x in d.control.client("a").items] == ["e2"]  # Wi-Fi kept its ref
        await finish(stop, task)
    run(go)


def test_after_a_person_acted_the_answer_is_the_full_list_with_the_note():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.viewer_message({"type": "gesture", "points": [[0, 0.5, 0.5], [60, 0.5, 0.5]]})
        r = await d.run_tool("a", "tap", {"target": {"x": 5, "y": 5}})
        lines = unsettled(r)
        assert lines[1].startswith("Screen 400x800") and lines[-1] == "Note: a person used the phone since your last look."
        await finish(stop, task)
    run(go)


def test_an_agent_that_has_not_looked_yet_gets_no_note_about_a_person():
    """Seen on the phone 2026-11-10: a new agent's first call ended with "a person used the phone since your
    last look", because someone had used the live view before it connected."""
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.viewer_message({"type": "gesture", "points": [[0, 0.5, 0.5], [60, 0.5, 0.5]]})
        first = await d.run_tool("b", "look", {"screenshot": False})
        assert "Note:" not in first["text"]
        await d.viewer_message({"type": "gesture", "points": [[0, 0.5, 0.5], [60, 0.5, 0.5]]})
        again = await d.run_tool("b", "look", {"screenshot": False})
        assert again["text"].endswith("\nNote: a person used the phone since your last look.")
        await finish(stop, task)
    run(go)


def test_a_touch_the_phone_did_not_answer_is_a_timeout_and_the_line_stays():
    """Over Wi-Fi one request can stall while WebDriverAgent answers the others: no reconnect, and the agent is
    told the action may have happened."""
    from tapwright.device import Unanswered

    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        lost = []
        d.lost = lambda reason: lost.append(reason)
        d.session.fail["tap"] = Unanswered("the phone didn't answer within 10 s")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert e.value.code == "timeout" and not lost and d.state == "live"
        assert str(e.value) == "the phone didn't answer within 10 s; the action may have happened: look before you retry."
        r = await d.run_tool("a", "tap", {"target": {"id": "ok-button"}, "look": False})  # the next call works
        assert r["text"].startswith("Tapped")
        await finish(stop, task)
    run(go)


def test_after_a_dropped_connection_the_answer_is_the_full_list():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        d.control.clients["a"].dropped = True
        r = await d.run_tool("a", "tap", {"target": {"id": "ok-button"}})
        assert unsettled(r)[1].startswith("Screen 400x800")
        assert d.control.clients["a"].dropped is False
        await finish(stop, task)
    run(go)


def test_a_look_renumbers_and_a_live_reread_keeps_the_refs():
    async def go():
        d = make()
        d.session.tree = [button("e1", "A", "a"), button("e2", "B", "b", rect=(10, 60, 100, 40))]
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        view = d.control.client("a")
        assert [e.ref for e in view.items] == ["e1", "e2"] and view.top_ref == 2
        d.session.tree = [button("e1", "C", "c"), button("e2", "B", "b", rect=(10, 60, 100, 40))]
        d.control.clock.t += 5.0
        r = await d.run_tool("a", "tap", {"target": {"id": "b"}, "look": False})
        assert r["text"].startswith('Tapped e2 button "B"')  # the read numbers it e2 again; C is the new e3
        assert [e.ref for e in view.items] == ["e3", "e2"] and view.top_ref == 3
        d.session.tree = [button("e1", "D", "d")]
        await d.run_tool("a", "look", {"screenshot": False})  # a look renumbers from e1
        assert [e.ref for e in view.items] == ["e1"] and view.top_ref == 1
        await finish(stop, task)
    run(go)


def test_changes_are_measured_against_the_last_answer_not_the_last_full_list():
    """Tapline's bug: scroll down answered a move, scroll back answered "No visible change."."""
    async def go():
        d = make()
        d.session.tree = [button("e1", "A", "a"), button("e2", "B", "b", rect=(10, 60, 100, 40))]
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        d.session.tree = [button("e1", "A", "a"), button("e2", "B", "b", rect=(10, 160, 100, 40))]
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert unsettled(r)[1:] == ["Same screen. Changes:", '↕ e2 button "B" id=b @10,160 100x40 [tap]']
        d.session.tree = [button("e1", "A", "a"), button("e2", "B", "b", rect=(10, 60, 100, 40))]  # back where the look had it
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert unsettled(r)[1:] == ["Same screen. Changes:", '↕ e2 button "B" id=b @10,60 100x40 [tap]']
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}})  # and now nothing moved
        assert unsettled(r)[1:] == ["Same screen. Changes:", "No visible change."]
        await finish(stop, task)
    run(go)


def test_a_silent_re_read_does_not_hide_what_changed_from_the_agent():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})  # the agent knows: e1 = OK
        d.session.tree = [button("e1", "OK", "ok-button"), button("e2", "Late", "late", rect=(10, 60, 100, 40))]
        d.control.clock.t += 5.0
        r = await d.run_tool("a", "tap", {"target": {"id": "ok-button"}})  # re-reads, then answers
        assert unsettled(r)[1:] == ["Same screen. Changes:", '+ e2 button "Late" id=late @10,60 100x40 [tap]']
        await finish(stop, task)
    run(go)


def test_an_action_that_leaves_nothing_on_screen_answers_with_the_full_list_and_a_note():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        d.session.tree = []
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert unsettled(r)[1:] == [
            "Screen 400x800 pt @3x · 0 elements", "App: Example (com.example.app)",
            "(nothing visible; the app may still be loading)",
            "Note: something outside the app (a system sheet or browser) may cover it."]
        view = d.control.client("a")
        assert view.items == [] and view.top_ref == 1  # e1 was given, and is never handed out again
        await finish(stop, task)
    run(go)


def test_an_empty_screen_before_and_after_is_no_change_not_a_note():
    async def go():
        d = make()
        d.session.tree = []
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        r = await d.run_tool("a", "tap", {"target": {"x": 5, "y": 5}})
        assert unsettled(r)[1:] == ["Same screen. Changes:", "No visible change."]
        await finish(stop, task)
    run(go)


def test_a_full_list_is_never_a_change_block_even_when_the_screen_is_empty():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.viewer_message({"type": "gesture", "points": [[0, 0.5, 0.5], [60, 0.5, 0.5]]})
        d.session.tree = []
        r = await d.run_tool("a", "tap", {"target": {"x": 5, "y": 5}})
        assert unsettled(r)[1:] == [
            "Screen 400x800 pt @3x · 0 elements", "App: Example (com.example.app)",
            "(nothing visible; the app may still be loading)",
            "Note: a person used the phone since your last look.",
            "Note: something outside the app (a system sheet or browser) may cover it."]
        d.session.tree = [button("e1", "OK", "ok-button")]
        await d.viewer_message({"type": "gesture", "points": [[0, 0.5, 0.5], [60, 0.5, 0.5]]})
        r = await d.run_tool("a", "tap", {"target": {"x": 5, "y": 5}})
        assert "Same screen." not in r["text"] and unsettled(r)[1].startswith("Screen 400x800")
        assert "may cover it" not in r["text"]  # the list wasn't non-empty -> empty
        await finish(stop, task)
    run(go)


def test_the_scale_is_read_once_per_connection_at_connect():
    async def go():
        d = make()
        stop, task = await started(d)
        assert d.session.scale_reads == 1
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "tap", {"target": {"x": 5, "y": 5}})
        assert d.session.scale_reads == 1
        await finish(stop, task)
    run(go)


def _alert(title="Allow Notifications?", buttons=("Allow", "Don't Allow")):
    from tapwright import ui
    return ui.Alert(title, tuple(buttons), (50, 300, 300, 200))


def test_an_alert_appearing_changing_and_going_heads_the_change_block():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        d.session.last_alert = _alert()
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "force": True})
        assert unsettled(r)[1:] == ["Same screen. Changes:", 'Alert: "Allow Notifications?" [Allow] [Don\'t Allow]']
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "force": True})
        assert unsettled(r)[1:3] == ["Same screen. Changes:", "No visible change."]  # the same alert: no news
        # v1.6: the tool knew of the alert this time, and `force` went past it
        assert unsettled(r)[3:] == ["Note: forced (behind_alert): the touch may have landed on what is in front."]
        d.session.last_alert = _alert("Allow Location?", ("OK",))
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "force": True})
        assert unsettled(r)[2] == 'Alert: "Allow Location?" [OK]'
        d.session.last_alert = None
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "force": True})
        assert unsettled(r)[1:3] == ["Same screen. Changes:", "Alert: gone"]  # (and the forced note: it was up as the touch went)
        await finish(stop, task)
    run(go)


def test_the_keyboard_showing_and_hiding_heads_the_change_block_before_the_element_lines():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        d.session.last_keyboard = True
        d.session.tree = [button("e1", "OK", "ok-button"), button("e2", "Done", "done", rect=(10, 60, 100, 40))]
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert unsettled(r)[1:4] == ["Same screen. Changes:", "Keyboard: shown",
                                               '+ e2 button "Done" id=done @10,60 100x40 [tap]']
        d.session.last_keyboard = False
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert unsettled(r)[1:3] == ["Same screen. Changes:", "Keyboard: hidden"]
        await finish(stop, task)
    run(go)


def test_a_look_records_the_alert_and_keyboard_so_they_are_not_news_afterwards():
    async def go():
        d = make()
        d.session.last_alert, d.session.last_keyboard = _alert(), True
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "force": True})
        assert unsettled(r)[1:3] == ["Same screen. Changes:", "No visible change."]
        await finish(stop, task)
    run(go)


def test_a_silent_re_read_counts_as_a_read_for_the_next_target():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        d.session.tree = [button("e1", "7"), button("e2", "7", rect=(10, 60, 100, 40))]
        d.control.clock.t += 5.0
        with pytest.raises(Refused):  # read the screen, then refused: nothing was done
            await d.run_tool("a", "tap", {"target": {"label": "7"}, "look": False})
        view = d.control.client("a")
        assert view.read_at == d.control.clock() and view.seen == d.control.generation
        await d.run_tool("a", "tap", {"target": {"label": "7", "index": 1}, "look": False})
        assert d.session.calls.count("refresh") == 1  # the second target is inside LIVE_AGE of that read
        await finish(stop, task)
    run(go)


def test_a_silent_re_read_gives_an_element_that_came_back_its_old_ref():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})  # e1 = OK
        d.session.tree = [button("e1", "Other", "other-button")]
        d.control.clock.t += 5.0
        await d.run_tool("a", "tap", {"target": {"id": "other-button"}, "look": False})  # OK is gone: Other = e2
        d.session.tree = [button("e1", "OK", "ok-button"), button("e2", "Other", "other-button", rect=(10, 60, 100, 40))]
        d.control.clock.t += 5.0
        await d.run_tool("a", "tap", {"target": {"id": "other-button"}, "look": False})
        view = d.control.client("a")
        assert [(e.ref, e.label) for e in view.items] == [("e1", "OK"), ("e2", "Other")]
        await finish(stop, task)
    run(go)


def test_after_a_scroll_the_old_refs_of_identical_buttons_are_not_found():
    async def go():
        def rows(labels):
            out = [button(f"e{i + 1}", f"Row {c}", rect=(0, 90 + 100 * i, 100, 40)) for i, c in enumerate(labels)]
            out += [button(f"e{i + 4}", "Edit", "", rect=(200, 100 + 100 * i, 100, 40)) for i in range(3)]
            return out + [button(f"e{i + 7}", f"Fixed {i}", "", rect=(300, 700 + i, 50, 5)) for i in range(20)]
        d = make()
        d.session.tree = rows("ABC")
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        d.session.tree = rows("BCD")  # one row up: the buttons sit where they sat
        r = await d.run_tool("a", "scroll", {"direction": "down"})
        assert unsettled(r)[1].startswith("Screen")  # nothing is known to carry over: the full list
        view = d.control.client("a")
        assert [x.ref for x in view.items if x.label == "Edit"] == ["e30", "e31", "e32"]  # new, above the 26 given
        assert not {"e4", "e5", "e6"} & {x.ref for x in view.items}  # the old Edit refs land on nothing
        await finish(stop, task)
    run(go)


TAP = {"target": {"x": 5, "y": 5}}


class WatchingSession(FakeSession):
    """Records how many stream watchers the pump had while a tap ran."""

    def __init__(self) -> None:
        super().__init__()
        self.daemon = None
        self.viewers: list = []

    async def tap(self, *args, **kwargs):
        self.viewers.append(self.daemon.pump.viewers)
        return await super().tap(*args, **kwargs)


def test_mark_is_where_the_stream_stands():
    async def go():
        d = make()
        assert d._mark() == (0, None)
        d.hub.publish(b"A")
        assert d._mark() == (1, d.hub.latest)
    run(go)


def grey_jpeg(grey: int = 255, spot: int = 0) -> bytes:
    """A plain grey frame, with a 3x3 px spot `spot` levels darker: iOS 26's faint shimmer on a still screen."""
    im = Image.new("L", (160, 320), grey)
    if spot:
        im.paste(grey - spot, (40, 40, 43, 43))
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=85)
    return buf.getvalue()


def test_a_faint_flicker_does_not_keep_a_still_screen_from_settling():
    """The lab on iOS 26 shimmered in a few pixels: no two frames were byte-identical for long, so every
    settle ran to its cap and `scroll to: start` swiped for 12 s at the top (2026-11-09)."""
    async def go():
        d = make(settle_still=daemon_module.STILL, settle_quiet=daemon_module.MIN_QUIET)
        on_loop_clock(d)
        calm, shimmer = grey_jpeg(), grey_jpeg(spot=85)
        d.hub.publish(calm)
        seq, since = d._mark()
        feeder = FrameFeeder(d.hub).start([shimmer, calm] * 200)
        seconds, changed = await d.settle(seq, cap=2.0, since=since)
        await feeder.stop()
        assert not changed and seconds == pytest.approx(daemon_module.MIN_QUIET, abs=0.02)
    run_virtual(go)


def test_a_change_and_stillness_seen_during_the_call_end_the_settle_at_the_return():
    """The tap call returns about 200 ms after its effect shows on the stream; waiting for a fresh still run
    after the return cost about 0.15 s an action (2026-11-09, the lab)."""
    async def go():
        d = make(settle_still=daemon_module.STILL, settle_first_frame=1.0)
        on_loop_clock(d)
        d.hub.publish(b"A")
        early, since = d._mark()  # the gesture goes out
        await asyncio.sleep(0.05)
        for _ in range(8):  # the effect, then still, while the call runs
            d.hub.publish(b"B")
            await asyncio.sleep(0.03)
        returned = d._mark()[0]
        feeder = feed_later(d, b"B")
        seconds, changed = await d.settle(returned, cap=2.0, since=since, early=early)
        await feeder.stop()
        assert changed and seconds == 0.0  # still for 0.2 s up to the return, with a fresh frame: nothing to wait for
    run_virtual(go)


def feed_later(d, frame: bytes, delay: float = 0.02) -> FrameFeeder:
    """A feeder whose first frame comes `delay` s from now (the next frame after a call returns)."""
    feeder = FrameFeeder(d.hub)

    async def start():
        await asyncio.sleep(delay)
        feeder.start(then=frame)
    asyncio.create_task(start())
    return feeder


def test_a_still_run_seen_during_the_call_but_gone_quiet_waits_for_a_frame_after_the_return():
    async def go():
        d = make(settle_still=daemon_module.STILL, settle_first_frame=1.0)
        on_loop_clock(d)
        d.hub.publish(b"A")
        early, since = d._mark()
        for _ in range(8):
            d.hub.publish(b"B")
            await asyncio.sleep(0.03)
        await asyncio.sleep(0.3)  # the stream went quiet before the return: its last frame is old
        returned = d._mark()[0]
        feeder = feed_later(d, b"B")
        seconds, changed = await d.settle(returned, cap=2.0, since=since, early=early)
        await feeder.stop()
        assert changed and seconds == pytest.approx(0.02, abs=0.005)  # the first fresh frame
    run_virtual(go)


def test_frames_during_the_call_count_only_once_they_show_a_change():
    async def go():
        d = make(settle_still=daemon_module.STILL, settle_quiet=daemon_module.MIN_QUIET)
        on_loop_clock(d)
        d.hub.publish(b"A")
        early, since = d._mark()
        for _ in range(8):  # nothing changed during the call: no answer from before the return
            d.hub.publish(b"A")
            await asyncio.sleep(0.03)
        returned = d._mark()[0]
        feeder = FrameFeeder(d.hub).start(then=b"A")
        seconds, changed = await d.settle(returned, cap=2.0, since=since, early=early)
        await feeder.stop()
        assert not changed and seconds == pytest.approx(daemon_module.MIN_QUIET, abs=0.01)
    run_virtual(go)


def test_a_change_just_before_the_return_still_needs_its_full_still_time():
    async def go():
        d = make(settle_still=daemon_module.STILL)
        on_loop_clock(d)
        d.hub.publish(b"A")
        early, since = d._mark()
        await asyncio.sleep(0.2)
        d.hub.publish(b"B")  # the effect shows 20 ms before the call returns
        await asyncio.sleep(0.02)
        returned = d._mark()[0]
        feeder = FrameFeeder(d.hub).start(then=b"B")
        seconds, changed = await d.settle(returned, cap=2.0, since=since, early=early)
        await feeder.stop()
        assert changed and seconds == pytest.approx(daemon_module.STILL - 0.02, abs=0.01)
    run_virtual(go)


def test_an_action_settles_from_the_frames_since_its_gesture_went_out():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        seen = {}
        original = d._settle

        async def settle(after_seq, still=None, cap=daemon_module.CAP, since=None, early=None):
            seen["early"], seen["cap"] = early, cap
            return await original(after_seq, still, cap, since, early)
        d._settle = settle
        await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert seen["early"] is not None and seen["cap"] == daemon_module.CAP
        await finish(stop, task)
    run(go)


def test_a_slow_fade_is_not_still_though_each_frame_is_close_to_the_last():
    async def go():
        d = make(settle_still=daemon_module.STILL)
        on_loop_clock(d)
        d.hub.publish(grey_jpeg(250))
        seq, since = d._mark()
        fade = [grey_jpeg(250 - 6 * i) for i in range(1, 41)]  # 6 levels a frame, 160 ms in all
        feeder = FrameFeeder(d.hub).start(fade, then=fade[-1])
        seconds, changed = await d.settle(seq, cap=2.0, since=since)
        await feeder.stop()
        # still once the fade is within the tolerance of its end (step 36, 0.14 s); frame-to-frame it'd be 0.1 s
        assert changed and seconds >= 0.14 + daemon_module.STILL - 0.01
    run_virtual(go)


def test_settle_ends_once_the_picture_is_still():
    async def go():
        d = make()
        d.hub.publish(b"A")
        seq, since = d._mark()
        feeder = FrameFeeder(d.hub).start([b"B", b"C"], then=b"C")
        seconds, changed = await d.settle(seq, still=0.05, cap=1.0, since=since)
        assert changed and 0.04 <= seconds < 0.5
        await feeder.stop()
    run(go)


def test_settle_reports_no_change_when_every_frame_is_the_same():
    async def go():
        d = make()
        d.hub.publish(b"A")
        seq, since = d._mark()
        feeder = FrameFeeder(d.hub).start(then=b"A")
        seconds, changed = await d.settle(seq, still=0.05, cap=1.0, since=since)
        assert not changed and seconds < 0.5
        await feeder.stop()
    run(go)


def test_settle_gives_up_at_the_cap_while_frames_keep_changing():
    async def go():
        d = make()
        d.hub.publish(b"A")
        seq, since = d._mark()
        feeder = FrameFeeder(d.hub).start(churn=True)
        seconds, changed = await d.settle(seq, still=0.05, cap=0.2, since=since)
        assert changed and 0.15 <= seconds < 0.6
        await feeder.stop()
    run(go)


def test_settle_without_frames_falls_back_to_a_short_sleep():
    async def go():
        d = make(settle_first_frame=0.02, settle_fallback=0.03)
        seconds, changed = await d.settle(0, cap=2.0)
        assert changed and 0.03 <= seconds < 0.5  # the first-frame wait counts towards the sleep
    run(go)


def test_an_unchanged_screen_is_answered_without_reading_the_tree():
    async def go():
        d = make()
        stop, task = await started(d)
        d.hub.publish(b"A")
        await d.run_tool("a", "look", {"screenshot": False})
        feeder = FrameFeeder(d.hub).start(then=b"A")
        reads = d.session.calls.count("snapshot")
        r = await d.run_tool("a", "tap", TAP)
        text = r["text"].splitlines()
        assert text[1:3] == ["Same screen. Changes:", "No visible change."] and SETTLED.fullmatch(text[-1])
        assert d.session.calls.count("snapshot") == reads and d.session.calls.count("refresh") == 0
        assert [e.ref for e in d.control.client("a").items] == ["e1"]  # the agent's list stays current
        await feeder.stop()
        await finish(stop, task)
    run(go)


def test_a_changed_screen_reads_the_tree_and_answers_with_changes():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        d.hub.publish(b"A")
        feeder = FrameFeeder(d.hub).start([b"B"], then=b"B")
        d.session.tree = [button("e1", "OK", "ok-button"), button("e2", "Done", "done", rect=(10, 60, 100, 40))]
        reads = d.session.calls.count("snapshot")
        r = await d.run_tool("a", "tap", TAP)
        text = r["text"].splitlines()
        assert text[1] == "Same screen. Changes:" and text[2].startswith('+ e2 button "Done"')
        assert SETTLED.fullmatch(text[-1]) and d.session.calls.count("snapshot") == reads + 1
        await feeder.stop()
        await finish(stop, task)
    run(go)


def test_the_settle_line_comes_after_the_changes_and_before_the_notes():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.viewer_message({"type": "gesture", "points": [[0, 0.5, 0.5], [60, 0.5, 0.5]]})
        r = await d.run_tool("a", "tap", TAP)
        text = r["text"].splitlines()
        assert text[-1].startswith("Note:") and SETTLED.fullmatch(text[-2])
        await finish(stop, task)
    run(go)


def test_a_full_list_is_followed_by_the_settle_line():
    async def go():
        d = make()
        stop, task = await started(d)
        r = await d.run_tool("a", "tap", TAP)  # no look yet: the full list
        text = r["text"].splitlines()
        assert text[1].startswith("Screen 400x800") and SETTLED.fullmatch(text[-1])
        await finish(stop, task)
    run(go)


def test_an_unchanged_screen_still_reads_the_tree_when_the_agent_holds_no_current_list():
    async def go():
        d = make()
        stop, task = await started(d)
        d.hub.publish(b"A")
        feeder = FrameFeeder(d.hub).start(then=b"A")
        r = await d.run_tool("a", "tap", TAP)  # the agent never looked: it needs the list
        assert r["text"].splitlines()[1].startswith("Screen 400x800")
        await feeder.stop()
        await finish(stop, task)
    run(go)


def test_look_false_has_no_settle_line():
    async def go():
        d = make()
        stop, task = await started(d)
        r = await d.run_tool("a", "tap", {**TAP, "look": False})
        assert "Settled" not in r["text"]
        await finish(stop, task)
    run(go)


def test_the_tail_can_leave_out_the_settle_line():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        view = d.control.client("a")
        r = await d._after(view, "Waited 1.0 s.", [], False, seconds=None)
        assert r["text"] == "Waited 1.0 s.\nSame screen. Changes:\nNo visible change."
        await finish(stop, task)
    run(go)


def test_the_list_is_current_while_the_stream_shows_the_same_picture():
    async def go():
        d = make()
        stop, task = await started(d)
        d.hub.publish(b"A")
        await d.run_tool("a", "look", {"screenshot": False})
        view = d.control.client("a")
        d.control.clock.t += 5.0  # older than LIVE_AGE
        assert d._current(view)  # the newest frame is the one the list was read against
        d.hub.publish(b"B")
        assert not d._current(view)
        await finish(stop, task)
    run(go)


def test_the_stream_is_held_while_a_tool_runs_and_kept_for_its_linger():
    async def go():
        s = WatchingSession()
        d = make(s, stream_linger=5.0)
        s.daemon = d
        stop, task = await started(d)
        await d.run_tool("a", "tap", {**TAP, "look": False})
        assert s.viewers == [1] and d.pump.viewers == 0
        pump_task = d.pump._task
        assert pump_task is not None and not pump_task.done()  # the pump's own linger keeps it open
        await d.run_tool("a", "tap", {**TAP, "look": False})
        assert d.pump._task is pump_task  # the next tool reuses the connection
        await d.pump.stop()
        await finish(stop, task)
    run(go)


def test_the_stream_closes_after_its_linger_once_tools_stop():
    async def go():
        d = make(stream_linger=0.05)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await asyncio.wait_for(d.pump._task, 2)
        await finish(stop, task)
    run(go)


def test_the_fixed_sleeps_are_gone():
    assert not hasattr(daemon_module, "SETTLE")
    assert (daemon_module.STILL, daemon_module.CAP, daemon_module.LONG_CAP) == (0.1, 2.0, 4.0)
    assert daemon_module.ACTIONS["tap"][1] == daemon_module.CAP
    assert daemon_module.ACTIONS["open_app"][1] == daemon_module.LONG_CAP
    assert daemon_module.ACTIONS["open_url"][1] == daemon_module.LONG_CAP


def test_the_list_is_not_current_without_a_stream_to_say_so():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})  # no frame at all
        d.control.clock.t += 5.0
        assert not d._current(d.control.client("a"))
        await finish(stop, task)
    run(go)


def run_virtual(scenario):
    asyncio.run(scenario(), loop_factory=VirtualLoop)


def test_a_silent_change_before_the_action_is_not_reported_as_no_visible_change():
    async def go():
        d = make()
        stop, task = await started(d)
        d.hub.publish(b"A")
        await d.run_tool("a", "look", {"screenshot": False})  # the list is read against picture A
        d.hub.publish(b"B")  # a spinner finished: the screen changed with no action
        d.session.tree = [button("e1", "OK", "ok-button"), button("e2", "Loaded", "loaded", rect=(10, 60, 100, 40))]
        feeder = FrameFeeder(d.hub).start(then=b"B")  # the tap itself changes nothing
        r = await d.run_tool("a", "tap", TAP)
        assert "No visible change." not in r["text"]
        assert any(line.startswith('+ e2 button "Loaded"') for line in r["text"].splitlines())
        await feeder.stop()
        await finish(stop, task)
    run(go)


def test_the_fallback_never_adds_to_the_wait_for_a_first_frame():
    async def go():
        d = make(settle_first_frame=1.0, settle_fallback=0.6)
        seconds, changed = await d.settle(0, cap=2.0)
        assert changed and seconds == pytest.approx(1.0, abs=0.01)  # max(0.6, 1.0), not 1.6
        d = make(settle_first_frame=0.2, settle_fallback=0.6)
        seconds, changed = await d.settle(0, cap=2.0)
        assert changed and seconds == pytest.approx(0.6, abs=0.01)  # the sleep tops up to 0.6, not 0.8
    run_virtual(go)


def test_at_the_real_default_the_screen_is_still_100_ms_after_the_last_change():
    async def go():
        d = make(settle_still=daemon_module.STILL, settle_first_frame=1.0)
        d.hub.publish(b"A")
        seq, since = d._mark()

        async def late():
            await asyncio.sleep(0.09)
            d.hub.publish(b"B")
        asyncio.create_task(late())
        seconds, changed = await d.settle(seq, cap=2.0, since=since)
        assert changed and seconds == pytest.approx(0.19, abs=0.005)  # 90 ms of nothing is not yet still
        seq, since = d._mark()
        feeder = FrameFeeder(d.hub).start(then=b"B")  # the same picture, over and over
        seconds, changed = await d.settle(seq, cap=2.0, since=since)
        await feeder.stop()
        assert not changed and seconds == pytest.approx(0.1, abs=0.005)
    run_virtual(go)


def on_loop_clock(d: Daemon) -> None:
    """The hub's arrival times and frame times follow the (virtual) loop clock."""
    loop = asyncio.get_running_loop()
    d.hub._mono = d.hub._clock = loop.time


def sha(frame: bytes) -> Frame:
    """The mark of a picture: what `since` compares against."""
    return Frame(0, 0, frame)


def test_frames_from_before_the_call_returned_do_not_count():
    async def go():
        d = make()
        on_loop_clock(d)
        for _ in range(6):
            d.hub.publish(b"A")  # the stream's lag: the old picture, all before the call returned
        seq = d._mark()[0]
        seconds, changed = await d.settle(seq, cap=2.0, since=sha(b"A"))
        assert changed  # no frame since the return, so nothing says the screen is unchanged
    run_virtual(go)


def test_a_stall_after_the_call_returned_is_not_stillness():
    async def go():
        d = make()
        on_loop_clock(d)
        d.hub.publish(b"A")
        seq = d._mark()[0]
        feeder = FrameFeeder(d.hub).start([b"A"] * 5)  # five frames, then the stream goes quiet
        seconds, changed = await d.settle(seq, cap=2.0, since=sha(b"A"))
        assert changed and seconds < 0.3
        await feeder.stop()
    run_virtual(go)


def test_a_change_arriving_200_ms_after_the_return_is_not_missed():
    async def go():
        d = make(settle_quiet=daemon_module.MIN_QUIET)
        on_loop_clock(d)
        d.hub.publish(b"A")
        seq = d._mark()[0]
        feeder = FrameFeeder(d.hub).start([b"A"] * 50, then=b"B")  # 50 frames x 4 ms, then the new screen
        seconds, changed = await d.settle(seq, cap=2.0, since=sha(b"A"))
        assert changed
        await feeder.stop()
    run_virtual(go)


def test_no_change_needs_three_identical_frames_and_300_ms_after_the_return():
    async def go():
        d = make(settle_quiet=daemon_module.MIN_QUIET)
        on_loop_clock(d)
        d.hub.publish(b"A")
        seq = d._mark()[0]
        feeder = FrameFeeder(d.hub).start(then=b"A")
        seconds, changed = await d.settle(seq, cap=2.0, since=sha(b"A"))
        assert not changed and seconds == pytest.approx(0.3, abs=0.01)
        await feeder.stop()
        d.hub.publish(b"A")  # one lone frame is not enough
        seq = d._mark()[0]
        feeder = FrameFeeder(d.hub).start([b"A", b"A"])
        seconds, changed = await d.settle(seq, cap=2.0, since=sha(b"A"))
        assert changed
        await feeder.stop()
    run_virtual(go)


def test_the_still_period_adapts_to_a_slow_stream():
    async def go():
        d = make(settle_still=daemon_module.STILL, settle_first_frame=1.0)
        on_loop_clock(d)
        feeder = FrameFeeder(d.hub, every=0.1).start([b"A"] * 5, then=b"B")  # 10 fps
        await asyncio.sleep(0.55)
        seq = d._mark()[0]
        start = asyncio.get_running_loop().time()
        seconds, changed = await d.settle(seq, cap=2.0, since=sha(b"A"))
        assert changed and seconds == pytest.approx(0.35, abs=0.02)  # 2.5 gaps = 0.25 s of B, not 0.1 s
        await feeder.stop()
        assert start == pytest.approx(0.55)
    run_virtual(go)


def test_hub_gap_is_the_median_arrival_gap():
    async def go():
        d = make()
        on_loop_clock(d)
        assert d.hub.gap() is None
        feeder = FrameFeeder(d.hub, every=0.05).start(then=b"A")
        await asyncio.sleep(1.0)
        assert d.hub.gap() == pytest.approx(0.05, abs=0.001)
        await feeder.stop()
    run_virtual(go)


def test_a_settle_that_hits_the_cap_says_so(monkeypatch):
    async def go():
        d = make()
        stop, task = await started(d)
        d.hub.publish(b"A")
        await d.run_tool("a", "look", {"screenshot": False})
        feeder = FrameFeeder(d.hub).start(churn=True)
        r = await d.run_tool("a", "tap", TAP)
        text = r["text"].splitlines()
        assert text[-1] == ("Note: the screen was still changing after 0.2 s (an animation or video); "
                            "positions may be mid-change.")
        await feeder.stop()
        await finish(stop, task)
    monkeypatch.setitem(daemon_module.ACTIONS, "tap", (daemon_module.ACTIONS["tap"][0], 0.2))
    run(go)


def test_a_call_with_no_phone_does_not_hold_the_stream_open():
    async def go():
        s = FakeSession()
        s.not_set_up = True
        d = make(s, stream_linger=5.0)
        stop, task = await started(d)
        assert d.state == "no-device"
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "look", {})
        assert e.value.code == "not_connected" and d.pump._task is None
        await finish(stop, task)
    run(go)


def test_the_list_is_current_by_the_hubs_clock_not_the_wall_clock():
    async def go():
        d = make()
        d.hub._clock = lambda: 5000.0  # not the wall clock
        stop, task = await started(d)
        d.hub.publish(b"A")
        await d.run_tool("a", "look", {"screenshot": False})
        d.control.clock.t += 5.0
        assert d._current(d.control.client("a"))
        await finish(stop, task)
    run(go)


BLUE = (0, 0, 255)


def blue_frame() -> bytes:
    return image_bytes((800, 1600), BLUE, "JPEG")


def pixel(answer: dict, xy):
    return Image.open(io.BytesIO(base64.b64decode(answer["image"]))).convert("RGB").getpixel(xy)


def test_look_takes_its_picture_from_the_newest_stream_frame():
    async def go():
        d = make()
        stop, task = await started(d)
        d.hub.publish(blue_frame())
        r = await d.run_tool("a", "look", {"width": 300})
        assert r["image_size"] == [300, 600]
        assert Image.open(io.BytesIO(base64.b64decode(r["image"]))).size == (300, 600)
        b = pixel(r, (150, 300))
        assert b[2] > 200 and b[0] < 60  # the stream's blue frame
        assert "screenshot" not in d.session.calls
        await finish(stop, task)
    run(go)


def test_without_a_frame_it_asks_the_phone_for_a_screenshot():
    async def go():
        d = make()
        stop, task = await started(d)
        r = await d.run_tool("a", "look", {})
        assert "screenshot" in d.session.calls and r["image_size"] == [600, 1200]
        g = pixel(r, (300, 600))
        assert g[1] > 200 and g[0] < 60  # the /screenshot's green
        await finish(stop, task)
    run(go)


def test_a_stale_frame_is_not_used():
    async def go():
        d = make(shot_fresh=0.02, shot_wait=0.05)
        stop, task = await started(d)
        d.hub.publish(blue_frame())
        await asyncio.sleep(0.06)
        r = await d.run_tool("a", "look", {})
        assert "screenshot" in d.session.calls and pixel(r, (300, 600))[1] > 200
        await finish(stop, task)
    run(go)


def test_labels_draw_the_actionable_elements():
    async def go():
        d = make()
        stop, task = await started(d)
        d.hub.publish(blue_frame())
        plain_look = await d.run_tool("a", "look", {"width": 400})
        labelled = await d.run_tool("a", "look", {"width": 400, "labels": True})
        assert pixel(plain_look, (11, 30))[2] > 200            # e1 is at 10,10 100x40
        p = pixel(labelled, (11, 30))
        assert p[0] > 180 and p[1] < 80 and p[2] < 80          # its left edge
        assert pixel(labelled, (60, 30))[2] > 200              # its inside is untouched
        await finish(stop, task)
    run(go)


def test_width_must_be_160_to_1320():
    async def go():
        d = make()
        stop, task = await started(d)
        for bad in (100, 1400, "wide"):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "look", {"width": bad})
            assert e.value.code == "bad_params" and "width" in str(e.value)
        await finish(stop, task)
    run(go)


def test_look_without_a_screenshot_has_no_image():
    async def go():
        d = make()
        stop, task = await started(d)
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert r["image"] is None and r["image_size"] is None
        await finish(stop, task)
    run(go)


def test_an_action_adds_a_picture_only_when_asked():
    async def go():
        d = make()
        stop, task = await started(d)
        d.hub.publish(blue_frame())
        await d.run_tool("a", "look", {"screenshot": False})
        assert (await d.run_tool("a", "tap", {**TAP, "look": False}))["image"] is None
        r = await d.run_tool("a", "tap", {**TAP, "look": False, "screenshot": True})
        assert r["image"] is None  # look: false never has an image
        r = await d.run_tool("a", "tap", {**TAP, "screenshot": True})
        assert r["image_size"] == [600, 1200]
        assert (await d.run_tool("a", "tap", TAP))["image"] is None
        await finish(stop, task)
    run(go)


def test_a_failed_screenshot_is_a_note_not_an_error():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.fail["screenshot"] = DeviceError("boom")
        r = await d.run_tool("a", "look", {})
        assert r["image"] is None and r["image_size"] is None
        assert r["text"].splitlines()[-1].startswith("Note: no screenshot: ")
        assert d.session.SCREEN_TEXT.splitlines()[0] in r["text"]  # the list still came
        await finish(stop, task)
    run(go)


def test_the_picture_shows_the_moment_the_list_was_read():
    async def go():
        class Moving(FakeSession):
            async def look(self, screenshot=True):
                result = await super().look(screenshot)
                self.hub.publish(image_bytes((800, 1600), (255, 0, 0), "JPEG"))  # the screen changes during the read
                return result
        s = Moving()
        d = make(s)
        s.hub = d.hub
        stop, task = await started(d)
        d.hub.publish(blue_frame())
        r = await d.run_tool("a", "look", {})
        b = pixel(r, (300, 600))
        assert b[2] > 200 and b[0] < 60 and "screenshot" not in d.session.calls
        await finish(stop, task)
    run(go)


def test_the_no_change_shortcut_uses_the_newest_frame_after_the_settle():
    async def go():
        d = make()
        stop, task = await started(d)
        d.hub.publish(blue_frame())
        await d.run_tool("a", "look", {"screenshot": False})
        feeder = FrameFeeder(d.hub).start(then=blue_frame())
        r = await d.run_tool("a", "tap", {**TAP, "screenshot": True})
        await feeder.stop()
        assert "No visible change." in r["text"] and pixel(r, (300, 600))[2] > 200
        assert "screenshot" not in d.session.calls
        await finish(stop, task)
    run(go)


def test_labels_are_skipped_with_a_note_when_the_orientation_changed():
    async def go():
        d = make()
        stop, task = await started(d)
        d.hub.publish(image_bytes((1600, 800), BLUE, "JPEG"))  # the phone turned; the list is for portrait
        r = await d.run_tool("a", "look", {"width": 400, "labels": True})
        assert r["image_size"] == [400, 200] and pixel(r, (11, 20))[2] > 200
        assert "Note: labels skipped: the screen orientation changed." in r["text"].splitlines()
        await finish(stop, task)
    run(go)


def test_a_failure_with_no_message_still_says_what_it_was():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.fail["screenshot"] = DeviceError("")
        r = await d.run_tool("a", "look", {})
        assert r["text"].splitlines()[-1] == "Note: no screenshot: DeviceError"
        await finish(stop, task)
    run(go)


# ---- apps, waiting, toggle and back (Task 7a) ----
def switch(checked, ref="e1"):
    return ui.Element(ref, "switch", "Wi-Fi", value="1" if checked else "0", rect=(300, 100, 60, 30),
                      checked=checked, actions=("toggle",), type="Switch")


async def looked(d, tree):
    """The agent has looked once at `tree`."""
    d.session.tree = tree
    await d.run_tool("a", "look", {"screenshot": False})


def test_the_old_tool_names_are_gone():
    async def go():
        d = make()
        stop, task = await started(d)
        for old in ("list_apps", "launch_app", "terminate_app"):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", old, {"bundle_id": "x"})
            assert e.value.code == "unsupported"
        await finish(stop, task)
    run(go)


def test_apps_marks_the_foreground_app_and_explains_the_mark():
    async def go():
        d = make()
        d.session.installed.append({"bundle_id": "com.example.two", "name": "Two Words", "version": "2.1", "build": "9"})
        stop, task = await started(d)
        d.session.foreground = "com.example.two"
        r = await d.run_tool("a", "apps", {})
        assert r["text"].splitlines() == ["com.example.app  Example 1.0 (7)", "com.example.two  Two Words 2.1 (9) *",
                                          "* = in front."]
        d.session.installed.clear()
        assert (await d.run_tool("a", "apps", {}))["text"] == "No apps installed by you."
        await finish(stop, task)
    run(go)


def test_open_app_takes_a_bundle_id_even_for_a_system_app():
    async def go():
        d = make()
        stop, task = await started(d)
        r = await d.run_tool("a", "open_app", {"app": "com.apple.Preferences"})
        assert d.session.launched == ["com.apple.Preferences"] and r["text"].startswith("Opened com.apple.Preferences.")
        await finish(stop, task)
    run(go)


def test_open_app_takes_a_name_ignoring_case_and_a_bundle_id_wins():
    async def go():
        d = make()
        stop, task = await started(d)
        r = await d.run_tool("a", "open_app", {"app": "example"})
        assert d.session.launched == ["com.example.app"] and r["text"].startswith("Opened Example (com.example.app).")
        await d.run_tool("a", "open_app", {"app": "com.example.app"})
        assert d.session.launched[-1] == "com.example.app"
        await finish(stop, task)
    run(go)


def test_open_app_ambiguous_and_unknown_names():
    async def go():
        d = make()
        d.session.installed.append({"bundle_id": "com.example.beta", "name": "Example Beta", "version": "1", "build": "1"})
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "open_app", {"app": "exam"})
        assert e.value.code == "ambiguous"
        assert str(e.value).splitlines() == ['2 installed apps match "exam". Pick one by its bundle id:',
                                             "com.example.app  Example 1.0 (7)", "com.example.beta  Example Beta 1 (1)"]
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "open_app", {"app": "nothing like it"})
        assert e.value.code == "not_found" and not d.session.launched
        assert (await d.run_tool("a", "open_app", {"app": "Example"}))["text"].startswith("Opened Example")  # exact beats a part
        await finish(stop, task)
    run(go)


def test_a_bundle_id_that_does_not_exist_is_not_found():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.fail["launch"] = DeviceError("HTTP 500: unable to launch")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "open_app", {"app": "com.nope.nothing"})
        assert e.value.code == "not_found" and "com.nope.nothing" in str(e.value)
        await finish(stop, task)
    run(go)


def test_open_app_waits_up_to_four_seconds_and_close_app_looks_at_nothing():
    assert daemon_module.ACTIONS["open_app"][1] == daemon_module.LONG_CAP
    assert daemon_module.ACTIONS["close_app"][1] is None
    assert {"back", "open_app"} <= set(daemon_module.MAY_MOVE_CONTENT) and "launch_app" not in daemon_module.MAY_MOVE_CONTENT


def test_opening_the_screen_that_is_already_open_keeps_every_ref():
    """Seen on the phone 2026-11-10: open_url to the lab while the lab was open answered `+ e30 header "Lab"`
    and `- header "Lab"` for each element without an identifier: the same things, renumbered."""
    async def go():
        s = FakeSession()
        s.tree = [button("e1", "OK", "ok-button"), el("e2", "header", "Lab", rect=(199, 70, 32, 22)),
                  el("e3", "text", "Switch", rect=(16, 640, 57, 22)), el("e4", "image", "gear", rect=(376, 93, 26, 26))]
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        r = await d.run_tool("a", "open_url", {"url": "app://lab"})
        assert "+ " not in r["text"] and "- " not in r["text"] and "No visible change." in r["text"]
        await finish(stop, task)
    run(go)


def test_close_app_by_name():
    async def go():
        d = make()
        stop, task = await started(d)
        r = await d.run_tool("a", "close_app", {"app": "Example"})
        assert d.session.closed == ["com.example.app"] and r["text"] == "Closed Example (com.example.app)."
        await finish(stop, task)
    run(go)


def test_the_app_names_are_read_at_connect():
    async def go():
        d = make()
        stop, task = await started(d)
        assert d.session.app_names == {"com.example.app": "Example"}
        await finish(stop, task)
    run(go)


def test_wait_checks_its_seconds():
    async def go():
        d = make()
        stop, task = await started(d)
        for bad in (0, 0.05, 31, "x", True, None, float("inf")):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "wait", {"seconds": bad})
            assert e.value.code == "bad_params" and "seconds" in str(e.value), bad
        await finish(stop, task)
    run(go)


def test_wait_answers_with_the_changes_and_no_picture_unless_asked():
    async def go():
        d = make()
        stop, task = await started(d)
        await looked(d, [button("e1", "OK", "ok-button")])
        d.session.tree = [button("e1", "OK", "ok-button"), button("e2", "Done", rect=(10, 60, 100, 40))]
        r = await d.run_tool("a", "wait", {"seconds": 1})
        lines = r["text"].splitlines()
        assert lines[0] == "Waited 1.0 s." and lines[1] == "Same screen. Changes:"
        assert any(line.startswith('+ e2 button "Done"') for line in lines)
        assert not any(line.startswith("Settled in") for line in lines) and r["image"] is None
        r = await d.run_tool("a", "wait", {"seconds": 0.5, "screenshot": True})
        assert r["image"] and r["text"].splitlines()[0] == "Waited 0.5 s."
        r = await d.run_tool("a", "wait", {"seconds": 0.5, "look": False})
        assert r["text"] == "Waited 0.5 s."
        await finish(stop, task)
    run(go)


def test_wait_sleeps_outside_the_phones_queue():
    async def go():
        d = make()
        stop, task = await started(d)
        await looked(d, [button("e1", "OK", "ok-button")])
        slept = []
        original = d.control.sleep

        async def sleep(seconds):
            slept.append(seconds)
            await original(seconds)
        d.control.sleep = sleep
        await d.run_tool("a", "wait", {"seconds": 3})
        assert slept == [3.0]
        await finish(stop, task)
    run(go)


def test_toggle_flips_a_switch_and_shows_its_new_state():
    async def go():
        d = make()
        stop, task = await started(d)
        await looked(d, [switch(False)])
        d.session.tree = [switch(True)]
        r = await d.run_tool("a", "toggle", {"target": {"ref": "e1"}})
        assert d.session.last_point == (330.0, 115.0)
        lines = r["text"].splitlines()
        assert lines[0].startswith("Toggled e1 switch") and "checked=true" in lines[0] and "[toggle]" not in lines[0]
        assert any(line.startswith("~ e1 switch") and "checked" in line for line in lines)
        await finish(stop, task)
    run(go)


def test_toggle_with_a_value_taps_only_when_it_differs():
    async def go():
        d = make()
        stop, task = await started(d)
        await looked(d, [switch(False)])
        r = await d.run_tool("a", "toggle", {"target": {"ref": "e1"}, "value": False})
        assert r["text"] == 'e1 switch "Wi-Fi" checked=false @300,100 60x30 is already false.'
        assert d.session.last_point is None and "tap" not in d.session.calls
        d.session.tree = [switch(True)]
        r = await d.run_tool("a", "toggle", {"target": {"ref": "e1"}, "value": True, "force": True})
        assert "tap" in d.session.calls and "checked=true" in r["text"].splitlines()[0]
        r = await d.run_tool("a", "toggle", {"target": {"ref": "e1"}, "value": True, "force": True})
        assert r["text"].endswith("is already true.")
        await finish(stop, task)
    run(go)


def test_toggle_refuses_what_is_not_a_switch():
    async def go():
        d = make()
        stop, task = await started(d)
        await looked(d, [button("e1", "OK", "ok-button")])
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "toggle", {"target": {"ref": "e1"}})
        assert e.value.code == "not_actionable"
        assert str(e.value) == 'no_action: e1 button "OK" id=ok-button @10,10 100x40 is not a switch'
        assert "tap" not in d.session.calls
        await finish(stop, task)
    run(go)


def test_toggle_at_a_point_uses_the_switch_under_it():
    async def go():
        d = make()
        stop, task = await started(d)
        await looked(d, [button("e1", "OK", "ok-button"), switch(False, "e2")])
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "toggle", {"target": {"x": 20, "y": 20}})  # over a button, not a switch
        assert e.value.code == "not_actionable" and str(e.value) == "no_action: Nothing at (20, 20) takes a toggle."
        assert "tap" not in d.session.calls
        d.session.tree = [button("e1", "OK", "ok-button"), switch(True, "e2")]
        r = await d.run_tool("a", "toggle", {"target": {"x": 310, "y": 110}})
        assert d.session.last_point == (330.0, 115.0) and r["text"].startswith("Toggled e2 switch")
        await finish(stop, task)
    run(go)


def test_back_taps_the_navigation_bars_back_button():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.back_at = (48.0, 81.0)
        r = await d.run_tool("a", "back", {})
        assert d.session.last_point == (48.0, 81.0) and r["text"].startswith("Tapped the back button.")
        await finish(stop, task)
    run(go)


def test_back_closes_what_is_open_on_top_before_anything_else():
    """AGENT-TOOLS v1.4, item 6: a popover closes by a touch outside it, and the answer says so."""
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.back_at = (48.0, 81.0)  # the bar behind the sheet has a back button: it must not be tapped
        d.session.dismiss_at = (215.0, 255.0)
        r = await d.run_tool("a", "back", {})
        assert d.session.last_point == (215.0, 255.0) and r["text"].startswith("Closed the sheet.")
        assert "swipe" not in d.session.calls
        await finish(stop, task)
    run(go)


def test_back_swipes_from_the_left_edge_only_when_there_is_no_navigation_bar():
    async def go():
        d = make()
        stop, task = await started(d)
        r = await d.run_tool("a", "back", {})
        assert "swipe" in d.session.calls and d.session.last_point == (2, 400.0)
        assert r["text"].startswith("Swiped from the left edge.") and "went back" not in r["text"].lower()
        assert "Same screen. Changes:" in r["text"] or "Screen" in r["text"]
        await finish(stop, task)
    run(go)


def test_back_does_nothing_when_the_bar_has_no_back_button():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.has_nav_bar = True  # a root screen, or a Cancel on the left
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "back", {})
        assert e.value.code == "not_actionable" and str(e.value).startswith("no_action:")
        assert "swipe" not in d.session.calls and "tap" not in d.session.calls
        await finish(stop, task)
    run(go)


def test_back_with_no_visible_change_says_a_gesture_was_sent():
    async def go():
        d = make()
        stop, task = await started(d)
        d.hub.publish(b"A")
        feeder = FrameFeeder(d.hub).start(then=b"A")  # the swipe changes nothing on the screen
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "back", {})
        assert e.value.code == "not_actionable" and str(e.value) == "no_action: nothing changed after back."
        await feeder.stop()
        await finish(stop, task)
    run(go)


def test_toggle_that_did_not_flip_says_so_and_a_wanted_value_not_reached_is_refused():
    async def go():
        d = make()
        stop, task = await started(d)
        off = ui.Element("e1", "switch", "Wi-Fi", value="0", rect=(300, 100, 60, 30), checked=False,
                         actions=("toggle",), type="Switch")
        await looked(d, [off])  # a switch whose tap changes nothing
        r = await d.run_tool("a", "toggle", {"target": {"ref": "e1"}})
        assert r["text"].splitlines()[0] == 'Tapped e1 switch "Wi-Fi" checked=false @300,100 60x30, but it is still checked=false.'
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "toggle", {"target": {"ref": "e1"}, "value": True})
        assert e.value.code == "not_actionable"
        assert str(e.value) == 'no_action: e1 switch "Wi-Fi" checked=false @300,100 60x30 didn\'t change'
        await finish(stop, task)
    run(go)


def test_toggle_with_no_frame_change_still_reads_the_tree():
    async def go():
        d = make()
        stop, task = await started(d)
        d.hub.publish(b"A")
        await looked(d, [switch(False)])
        feeder = FrameFeeder(d.hub).start(then=b"A")
        reads = d.session.calls.count("snapshot")
        r = await d.run_tool("a", "toggle", {"target": {"ref": "e1"}})
        assert d.session.calls.count("snapshot") > reads  # the tree was read, not skipped
        assert "but it is still checked=false." in r["text"].splitlines()[0]
        await feeder.stop()
        await finish(stop, task)
    run(go)


def test_a_toggle_answers_as_soon_as_the_switch_reads_flipped():
    """Measured on the phone 2026-11-11: waiting for the knob's slide to end cost 0.33 to 0.65 s a toggle. The
    first read that shows the switch flipped is the answer; no wait for a still screen."""
    async def go():
        d = make(settle_still=5.0, settle_quiet=5.0)  # a still-screen wait would take seconds here
        stop, task = await started(d)
        await looked(d, [switch(False)])
        real = d.session.tap

        async def tap(target, hold_ms=60):
            d.session.tree = [switch(True)]
            return await real(target, hold_ms)
        d.session.tap = tap
        feeder = FrameFeeder(d.hub).start(then=b"B")
        began = asyncio.get_running_loop().time()
        r = await d.run_tool("a", "toggle", {"target": {"ref": "e1"}})
        assert asyncio.get_running_loop().time() - began < 1.0
        assert r["text"].splitlines()[0].startswith("Toggled e1 switch")
        await feeder.stop()
        await finish(stop, task)
    run(go)


def test_a_toggle_that_is_an_agents_first_call_still_says_what_the_switch_did():
    """Seen on the phone 2026-11-11: with no list before it, the answer is a full list that numbers the switch anew,
    and the first line stayed "Tapped …" instead of "Toggled … checked=true"."""
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.tree = [switch(False)]
        real = d.session.tap

        async def tap(target, hold_ms=60):
            d.session.tree = [switch(True)]
            return await real(target, hold_ms)
        d.session.tap = tap
        r = await d.run_tool("b", "toggle", {"target": {"label": "Wi-Fi"}})
        assert r["text"].splitlines()[0].startswith("Toggled ") and "checked=true" in r["text"].splitlines()[0]
        await finish(stop, task)
    run(go)


def test_typing_answers_as_soon_as_the_field_reads_the_text():
    """The field holds the text as soon as the keys go out; the wait for a still screen after it (0.2 to 0.4 s on the
    phone) only saw the caret blink."""
    async def go():
        d = make(settle_still=5.0, settle_quiet=5.0)  # a still-screen wait would take seconds here
        stop, task = await started(d)
        field = el("e1", "input", "Email", "email", rect=(10, 100, 300, 40))
        await looked(d, [field])
        real = d.session.type_text

        async def type_text(text, **kw):
            d.session.tree = [el("e1", "input", "Email", "email", rect=(10, 100, 300, 40), value=text)]
            return await real(text, **kw)
        d.session.type_text = type_text
        feeder = FrameFeeder(d.hub).start(then=b"B")
        began = asyncio.get_running_loop().time()
        r = await d.run_tool("a", "type_text", {"target": {"id": "email"}, "text": "hi"})
        assert asyncio.get_running_loop().time() - began < 1.0
        assert 'Value is now "hi"' in r["text"].splitlines()[0]
        await feeder.stop()
        await finish(stop, task)
    run(go)


def test_toggle_followed_by_a_confirm_alert_is_not_refused():
    async def go():
        d = make()
        stop, task = await started(d)
        await looked(d, [switch(True)])
        d.session.last_alert = ui.Alert("Turn off Wi-Fi?", ("Cancel", "Turn Off"), (50, 300, 300, 150))
        r = await d.run_tool("a", "toggle", {"target": {"ref": "e1"}, "value": False})
        lines = r["text"].splitlines()
        assert lines[0].endswith("but it is still checked=true.") and any(line.startswith("Alert:") for line in lines)
        await finish(stop, task)
    run(go)


def test_toggle_without_a_look_claims_no_new_state():
    async def go():
        d = make()
        stop, task = await started(d)
        await looked(d, [switch(False)])
        r = await d.run_tool("a", "toggle", {"target": {"ref": "e1"}, "look": False})
        assert r["text"] == 'Tapped e1 switch "Wi-Fi" @300,100 60x30.'
        await finish(stop, task)
    run(go)


def test_toggle_prefers_the_switch_when_a_row_shares_its_label():
    async def go():
        d = make()
        stop, task = await started(d)
        row = ui.Element("e1", "cell", "Wi-Fi", rect=(0, 90, 400, 50), actions=("tap",), type="Cell")
        await looked(d, [row, switch(False, "e2")])
        d.session.tree = [row, switch(True, "e2")]
        r = await d.run_tool("a", "toggle", {"target": {"label": "Wi-Fi"}})
        assert d.session.last_point == (330.0, 115.0) and r["text"].startswith("Toggled e2 switch")
        await finish(stop, task)
    run(go)


def test_open_app_falls_back_from_an_uninstalled_bundle_id_to_a_name():
    async def go():
        d = make()
        d.session.installed.append({"bundle_id": "com.example.notes", "name": "com.example.old", "version": "1", "build": "1"})
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": "com.example.old"})  # looks like an id, is a name
        assert d.session.launched == ["com.example.notes"]
        await finish(stop, task)
    run(go)


def test_open_app_partial_names_are_prefixes_of_names_only():
    async def go():
        d = make()
        d.session.installed += [{"bundle_id": "com.example.sticky", "name": "Sticky Notes", "version": "1", "build": "1"},
                                {"bundle_id": "com.example.nb", "name": "Notebook", "version": "1", "build": "1"}]
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": "note"})  # a prefix of Notebook, not of Sticky Notes
        assert d.session.launched == ["com.example.nb"]
        d.session.installed[0]["bundle_id"] = "com.example.zzz-demo"
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "open_app", {"app": "zzz"})
        assert e.value.code == "not_found"
        await finish(stop, task)
    run(go)


def test_close_app_needs_an_exact_name_or_bundle_id():
    async def go():
        d = make()
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "close_app", {"app": "Exam"})
        assert e.value.code == "not_found" and not d.session.closed
        await d.run_tool("a", "close_app", {"app": "example"})
        assert d.session.closed == ["com.example.app"]
        await finish(stop, task)
    run(go)


def test_system_apps_open_by_name():
    async def go():
        d = make()
        stop, task = await started(d)
        table = {"Settings": "com.apple.Preferences", "Safari": "com.apple.mobilesafari",
                 "Calculator": "com.apple.calculator", "Camera": "com.apple.camera",
                 "Photos": "com.apple.mobileslideshow", "Messages": "com.apple.MobileSMS",
                 "Mail": "com.apple.mobilemail", "Maps": "com.apple.Maps", "App Store": "com.apple.AppStore",
                 "Clock": "com.apple.mobiletimer", "Notes": "com.apple.mobilenotes"}
        for name, bundle in table.items():
            r = await d.run_tool("a", "open_app", {"app": name.lower()})
            assert d.session.launched[-1] == bundle and r["text"].startswith(f"Opened {name} ({bundle}).")
        await d.run_tool("a", "close_app", {"app": "Settings"})
        assert d.session.closed == ["com.apple.Preferences"]
        await finish(stop, task)
    run(go)


def test_wait_wants_a_number_and_flags_must_be_booleans():
    async def go():
        d = make()
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "wait", {"seconds": "5"})
        assert e.value.code == "bad_params" and "a number" in str(e.value)
        for tool, args in (("wait", {"seconds": 1, "look": "no"}), ("wait", {"seconds": 1, "screenshot": 1}),
                           ("tap", {"target": {"x": 1, "y": 1}, "look": "yes"}),
                           ("look", {"screenshot": "false"}), ("look", {"labels": "yes"})):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", tool, args)
            assert e.value.code == "bad_params" and "true or false" in str(e.value), (tool, args)
        await finish(stop, task)
    run(go)


def _redrawing(d, *trees):
    """The session's next refresh() reads these trees in turn (the last one repeats), noting each in `events`."""
    events, queue = [], list(trees)
    real = d.session.refresh

    async def refresh():
        d.session.tree = queue.pop(0) if len(queue) > 1 else queue[0]
        events.append("refresh")
        d.hub.publish(b"redraw%d" % len(events))  # the screen moved while the tree was read
        return await real()
    d.session.refresh = refresh

    async def settle(after_seq, still=None, cap=daemon_module.CAP, since=None):
        events.append(("settle", cap))
        return daemon_module.Settled(0.0, False, False)
    d._settle = settle
    d.hub.publish(b"A")  # there is a stream, so the settle has something to wait on
    return events


def test_the_connect_settings_turn_xctests_idle_wait_off_and_a_quality_change_keeps_them():
    async def go():
        d = make()
        stop, task = await started(d)
        sent = [c[1] for c in d.session.calls if isinstance(c, tuple) and c[0] == "settings"]
        assert sent[0] == {**PRESETS["balanced"], "waitForIdleTimeout": 0, "animationCoolOffTimeout": 0}
        await d.set_quality("light")
        sent = [c[1] for c in d.session.calls if isinstance(c, tuple) and c[0] == "settings"]
        assert sent[-1]["waitForIdleTimeout"] == 0 and sent[-1]["animationCoolOffTimeout"] == 0
        assert sent[-1]["mjpegServerScreenshotQuality"] == PRESETS["light"]["mjpegServerScreenshotQuality"]
        await finish(stop, task)
    run(go)


def test_a_silent_re_read_waits_for_the_screen_to_be_still_first():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        events = _redrawing(d, [button("e1", "OK", "ok-button")])
        d.control.clock.t += 5.0
        await d.run_tool("a", "tap", {"target": {"id": "ok-button"}, "look": False})
        assert events == [("settle", 1.0), "refresh"]
        await finish(stop, task)
    run(go)


def test_a_target_missing_from_a_mid_redraw_read_is_found_on_the_second_read():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        events = _redrawing(d, [button("e1", "Other", "other-button")], [button("e1", "OK", "ok-button")])
        d.control.clock.t += 5.0
        r = await d.run_tool("a", "tap", {"target": {"id": "ok-button"}, "look": False})
        assert r["text"].startswith('Tapped e1 button "OK"')
        assert events == [("settle", 1.0), "refresh", ("settle", 1.0), "refresh"]
        await finish(stop, task)
    run(go)


def test_a_target_missing_both_times_is_not_found():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        events = _redrawing(d, [button("e1", "Other", "other-button")])
        d.control.clock.t += 5.0
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "ok-button"}, "look": False})
        assert e.value.code == "not_found" and events.count("refresh") == 2
        await finish(stop, task)
    run(go)


def _recording_settles(d):
    """Replace the settle: the caps it was asked for, in order."""
    caps = []

    async def settle(after_seq, still=None, cap=daemon_module.CAP, since=None, early=None):
        caps.append(cap)
        return daemon_module.Settled(0.0, True, False)
    d._settle = settle
    return caps


def test_every_tool_settles_first_after_an_action_that_was_not_settled():
    async def go():
        for tool, args in (("alert", {"action": "accept"}), ("look", {"screenshot": False}),
                           ("back", {}), ("press_button", {"name": "home"})):
            d = make()
            stop, task = await started(d)
            await d.run_tool("a", "look", {"screenshot": False})
            await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False})
            caps = _recording_settles(d)
            await d.run_tool("a", tool, args)
            assert caps[0] == daemon_module.RESOLVE_SETTLE, tool
            await finish(stop, task)
    run(go)


def test_an_action_after_a_settled_answer_does_not_settle_first():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "tap", {"target": {"ref": "e1"}})  # settled
        caps = _recording_settles(d)
        await d.run_tool("a", "alert", {"action": "accept", "look": False})
        await d.run_tool("a", "look", {"screenshot": False})  # the alert wasn't settled: this one settles
        assert caps == [daemon_module.RESOLVE_SETTLE]
        await d.run_tool("a", "look", {"screenshot": False})  # and now it has been
        assert caps == [daemon_module.RESOLVE_SETTLE]
        await finish(stop, task)
    run(go)


def test_a_target_after_an_unsettled_action_settles_once_not_twice():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False})
        d.control.clock.t += 5.0
        caps = _recording_settles(d)
        await d.run_tool("a", "tap", {"target": {"id": "ok-button"}, "look": False})
        assert caps == [daemon_module.RESOLVE_SETTLE]
        await finish(stop, task)
    run(go)


def test_a_missing_target_on_a_still_screen_is_not_read_twice():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        d.control.clock.t += 5.0
        d.hub.publish(b"A")
        feeder = FrameFeeder(d.hub).start(then=b"A")  # the stream shows one picture throughout
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "nope"}, "look": False})
        assert e.value.code == "not_found" and d.session.calls.count("refresh") == 1
        await feeder.stop()
        await finish(stop, task)
    run(go)


def test_an_unsettled_action_by_one_client_makes_every_client_settle_first():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("b", "look", {"screenshot": False})
        await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False})
        caps = _recording_settles(d)
        await d.run_tool("b", "look", {"screenshot": False})
        assert caps[0] == daemon_module.RESOLVE_SETTLE
        await finish(stop, task)
    run(go)


def test_reading_alert_text_does_not_leave_the_screen_unsettled():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "alert", {"action": "text"})
        caps = _recording_settles(d)
        await d.run_tool("a", "look", {"screenshot": False})
        assert caps == []
        await finish(stop, task)
    run(go)


# ---- type_text, scroll and wait_for (Task 7b) ----
def inp(ref, label, value="", y=100):
    return el(ref, "input", label, value=value, rect=(10, y, 300, 44))


def test_type_text_into_a_target_replaces_by_default_and_reads_the_value_back():
    async def go():
        d = make()
        stop, task = await started(d)
        await looked(d, [inp("e1", "Name")])
        d.session.tree = [inp("e1", "Name", value="Ada")]
        r = await d.run_tool("a", "type_text", {"text": "Ada", "target": {"ref": "e1"}})
        assert d.session.typed == [("Ada", True, False, ui.Target(ref="e1"))]
        lines = r["text"].splitlines()
        assert lines[0] == 'Typed into e1 input "Name" value="Ada" @10,100 300x44. Value is now "Ada".'
        assert lines[1] == "Same screen. Changes:" and lines[2].startswith('~ e1 input "Name" value="Ada"')
        assert "(was " in lines[2] and SETTLED.fullmatch(lines[3])
        await finish(stop, task)
    run(go)


def test_the_change_block_is_a_literal_diff_that_includes_the_field_typed_into():
    """v1.3: "eggs" into a field holding "egg" shows the field's own `~` line; the same text again changes nothing."""
    async def go():
        d = make()
        stop, task = await started(d)
        await looked(d, [inp("e1", "Food", value="egg")])
        d.session.tree = [inp("e1", "Food", value="eggs")]
        r = await d.run_tool("a", "type_text", {"text": "eggs", "target": {"ref": "e1"}})
        lines = r["text"].splitlines()
        assert lines[0].endswith('Value is now "eggs".')
        assert lines[1] == "Same screen. Changes:"
        assert lines[2] == '~ e1 input "Food" value="eggs" @10,100 300x44 [tap, type] (was "egg")'
        r = await d.run_tool("a", "type_text", {"text": "eggs", "target": {"ref": "e1"}})
        assert r["text"].splitlines()[1:3] == ["Same screen. Changes:", "No visible change."]
        await finish(stop, task)
    run(go)


def test_type_text_passes_replace_false_and_submit_on():
    async def go():
        d = make()
        stop, task = await started(d)
        await looked(d, [inp("e1", "Name")])
        await d.run_tool("a", "type_text", {"text": "x", "target": {"ref": "e1"}, "replace": False, "submit": True})
        assert d.session.typed[-1][1:3] == (False, True)
        await finish(stop, task)
    run(go)


def test_type_text_without_a_target_uses_the_only_input():
    async def go():
        d = make()
        stop, task = await started(d)
        await looked(d, [el("e1", "button", "OK"), inp("e2", "Name", y=160)])
        r = await d.run_tool("a", "type_text", {"text": "hi", "look": False})
        ref = d.session.typed[-1][3].ref  # a silent re-read numbers an id-less list afresh; the answer says which
        assert re.fullmatch(r"e\d+", ref) and r["text"].startswith(f'Typed into {ref} input "Name"')
        await finish(stop, task)
    run(go)


def test_type_text_without_a_target_and_several_inputs_is_ambiguous():
    async def go():
        d = make()
        stop, task = await started(d)
        await looked(d, [inp("e1", "Name"), inp("e2", "Email", y=160)])
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "type_text", {"text": "x"})
        message = str(e.value)
        assert e.value.code == "ambiguous"
        assert message.startswith("2 inputs on screen; pass a target to pick one:\nCandidates:\n")
        assert message.endswith('\nRetry with one of the refs as the target.')  # no target, so no index to add
        assert re.search(r'\n  e\d+ input "Name"', message) and re.search(r'\n  e\d+ input "Email"', message)
        assert not d.session.typed
        await finish(stop, task)
    run(go)


def test_type_text_without_any_input_is_not_found():
    async def go():
        d = make()
        stop, task = await started(d)
        await looked(d, [el("e1", "button", "OK")])
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "type_text", {"text": "x"})
        assert e.value.code == "not_found" and str(e.value) == "No input on screen" and not d.session.typed
        await finish(stop, task)
    run(go)


def test_type_text_at_a_point_uses_the_input_under_it_or_refuses():
    async def go():
        d = make()
        stop, task = await started(d)
        await looked(d, [inp("e1", "Name"), el("e2", "button", "OK", rect=(10, 300, 100, 40))])
        await d.run_tool("a", "type_text", {"text": "x", "target": {"x": 20, "y": 120}, "look": False})
        assert d.session.typed[-1][3] == ui.Target(ref="e1")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "type_text", {"text": "x", "target": {"x": 20, "y": 320}, "look": False})
        assert e.value.code == "not_actionable" and str(e.value) == "no_action: Nothing at (20, 320) takes typing."
        await finish(stop, task)
    run(go)


def test_type_text_into_something_that_is_not_an_input_is_no_action():
    async def go():
        d = make()
        stop, task = await started(d)
        await looked(d, [el("e1", "button", "OK")])
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "type_text", {"text": "x", "target": {"ref": "e1"}})
        assert e.value.code == "not_actionable" and str(e.value) == 'no_action: e1 button "OK" @10,10 100x40 is not an input'
        assert not d.session.typed
        await finish(stop, task)
    run(go)


def test_type_text_checks_its_arguments():
    async def go():
        d = make()
        stop, task = await started(d)
        for bad in ({"text": 5}, {"text": "a", "replace": "yes"}, {"text": "a", "submit": 1}):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "type_text", bad)
            assert e.value.code == "bad_params", bad
        await finish(stop, task)
    run(go)


AREA = el("e1", "scroll", "List", rect=(0, 100, 400, 600))


def test_scroll_moves_a_share_of_the_screen_by_default():
    async def go():
        d = make()
        stop, task = await started(d)
        r = await d.run_tool("a", "scroll", {"look": False})
        assert d.session.last_point == (200.0, 690.0)  # no scroll element: the screen, minus the fixed 60/40 pt insets
        assert r["text"] == "Scrolled the screen down."
        await finish(stop, task)
    run(go)


def test_scroll_stays_inside_the_scroll_area_over_the_screen_centre():
    async def go():
        d = make()
        stop, task = await started(d)
        await looked(d, [AREA])
        r = await d.run_tool("a", "scroll", {"direction": "down", "amount": 0.5, "look": False})
        assert d.session.last_point == (200.0, 550.0)
        assert r["text"] == 'Scrolled e1 scroll "List" @0,100 400x600 down.'
        await finish(stop, task)
    run(go)


def test_scroll_inside_a_target_and_inside_a_scroll_around_it():
    async def go():
        d = make()
        stop, task = await started(d)
        carousel = el("e2", "scroll", "Carousel", rect=(0, 600, 400, 100))
        card = el("e3", "button", "Card", rect=(10, 610, 80, 80))
        await looked(d, [AREA, carousel, card])
        await d.run_tool("a", "scroll", {"direction": "left", "amount": 0.5, "target": {"ref": "e2"}, "look": False})
        assert d.session.last_point == (100.0, 650.0)  # scrolling left: the finger starts on the left
        r = await d.run_tool("a", "scroll", {"direction": "left", "amount": 0.5, "target": {"ref": "e3"}, "look": False})
        assert d.session.last_point == (100.0, 650.0) and r["text"].startswith('Scrolled e2 scroll "Carousel"')
        await finish(stop, task)
    run(go)


def test_scroll_with_a_target_outside_any_scroll_view_is_no_action():
    async def go():
        d = make()
        stop, task = await started(d)
        await looked(d, [AREA, el("e2", "button", "Back", rect=(10, 20, 100, 40))])
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "scroll", {"target": {"ref": "e2"}})
        assert e.value.code == "not_actionable"
        assert str(e.value) == 'no_action: e2 button "Back" @10,20 100x40 is not inside a scroll view'
        assert "swipe" not in d.session.calls
        await finish(stop, task)
    run(go)


def test_scroll_at_a_point_uses_the_scroll_view_under_it():
    async def go():
        d = make()
        stop, task = await started(d)
        await looked(d, [AREA])
        await d.run_tool("a", "scroll", {"target": {"x": 50, "y": 300}, "amount": 0.5, "look": False})
        assert d.session.last_point == (200.0, 550.0)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "scroll", {"target": {"x": 50, "y": 50}})
        assert e.value.code == "not_actionable" and str(e.value) == "no_action: Nothing at (50, 50) takes a scroll."
        await finish(stop, task)
    run(go)


class MovingPicture:
    """The stream of a list that moves on each swipe, `moves` times. A frame goes out every few ms."""

    def __init__(self, d, moves: int) -> None:
        self.moves, self.swipes, self.picture = moves, 0, b"P0"
        d.hub.publish(self.picture)
        original = d.session.swipe

        async def swipe(*a, **k):
            self.swipes += 1
            if self.swipes <= self.moves:
                self.picture = b"P%d" % self.swipes
            return await original(*a, **k)
        d.session.swipe = swipe

        async def feed():
            while True:
                d.hub.publish(self.picture)
                await asyncio.sleep(0.004)
        self.task = asyncio.create_task(feed())

    async def stop(self) -> None:
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


def test_scroll_to_the_end_swipes_until_the_stream_stops_changing():
    async def go():
        d = make()
        stop, task = await started(d)
        picture = MovingPicture(d, moves=3)
        r = await d.run_tool("a", "scroll", {"to": "end", "look": False})
        assert picture.swipes == 4 and r["text"] == "Scrolled the screen down."
        await picture.stop()
        await finish(stop, task)
    run(go)


def test_scroll_to_start_goes_up_and_stops_at_the_cap_with_a_note_if_it_never_ends(monkeypatch):
    monkeypatch.setattr(daemon_module, "SCROLL_CAP", 0.3)  # 12 s in life
    async def go():
        d = make()
        stop, task = await started(d)
        picture = MovingPicture(d, moves=1000)  # endless loading: every swipe brings something new
        ends = []
        original = d.session.swipe

        async def swipe(x0, y0, x1, y1, duration_ms=350, hold_ms=0):
            ends.append((y0, y1))
            return await original(x0, y0, x1, y1, duration_ms, hold_ms)
        d.session.swipe = swipe
        r = await d.run_tool("a", "scroll", {"to": "start", "look": False})
        assert 1 <= picture.swipes < 15 and ends[0][0] < ends[0][1]  # the finger moves down: content moves up
        assert r["text"].splitlines() == ["Scrolled the screen up.",
                                          "Note: the start wasn't reached in 0.3 s; scroll again to go on."]
        await picture.stop()
        await finish(stop, task)
    run(go)


class BouncingPicture:
    """A list with a rubber band: each swipe shows a transient picture for a moment, then settles on the
    `finals` (the last repeats). The settle sees the screen change every time; the settled frames match at the end."""

    def __init__(self, d, *finals: bytes) -> None:
        self.finals, self.swipes, self.picture = list(finals), 0, b"START"
        d.hub.publish(self.picture)
        original = d.session.swipe

        async def swipe(*a, **k):
            self.swipes += 1
            self.picture = b"bounce%d" % self.swipes
            final = self.finals[min(self.swipes, len(self.finals)) - 1]
            asyncio.get_running_loop().call_later(0.01, lambda: setattr(self, "picture", final))
            return await original(*a, **k)
        d.session.swipe = swipe

        async def feed():
            while True:
                d.hub.publish(self.picture)
                await asyncio.sleep(0.004)
        self.task = asyncio.create_task(feed())

    async def stop(self) -> None:
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


def test_a_bouncing_end_of_the_list_stops_the_scroll_when_the_settled_pictures_match():
    async def go():
        d = make()
        stop, task = await started(d)
        picture = BouncingPicture(d, b"END", b"END")  # swipe 1 reaches the end; swipe 2 only bounces
        r = await d.run_tool("a", "scroll", {"to": "end", "look": False})
        assert picture.swipes == 2 and r["text"] == "Scrolled the screen down."
        await picture.stop()
        await finish(stop, task)
    run(go)


def test_a_scroll_to_the_end_that_already_is_there_stops_after_one_swipe():
    async def go():
        d = make()
        stop, task = await started(d)
        picture = BouncingPicture(d, b"START")  # the bounce settles where it began
        await d.run_tool("a", "scroll", {"to": "end", "look": False})
        assert picture.swipes == 1
        await picture.stop()
        await finish(stop, task)
    run(go)


def test_a_sideways_scroll_to_the_end_goes_right():
    async def go():
        d = make()
        stop, task = await started(d)
        picture = MovingPicture(d, moves=0)
        r = await d.run_tool("a", "scroll", {"to": "end", "direction": "right", "look": False})
        assert r["text"] == "Scrolled the screen right." and picture.swipes == 1
        r = await d.run_tool("a", "scroll", {"to": "start", "direction": "left", "look": False})
        assert r["text"] == "Scrolled the screen left."
        await picture.stop()
        await finish(stop, task)
    run(go)


def test_a_direction_that_contradicts_to_is_bad_params():
    async def go():
        d = make()
        stop, task = await started(d)
        for bad in ({"to": "end", "direction": "up"}, {"to": "end", "direction": "left"},
                    {"to": "start", "direction": "down"}, {"to": "start", "direction": "right"}):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "scroll", bad)
            assert e.value.code == "bad_params" and "to" in str(e.value), bad
        assert "swipe" not in d.session.calls
        await finish(stop, task)
    run(go)


def test_scrolls_use_the_eased_drag_so_the_content_does_not_fling():
    async def go():
        d = make()
        stop, task = await started(d)
        await looked(d, [el("e1", "scroll", "Page", rect=(0, 0, 400, 800))])
        await d.run_tool("a", "scroll", {"direction": "down", "look": False})
        assert len(d.session.scrolls) == 1  # ui.scroll_drag: fast, then slow over the last stretch, then a rest
        assert await d.viewer_message({"type": "scroll", "x": 0.5, "y": 0.5, "dx": 0, "dy": 0.25}) is None
        assert len(d.session.scrolls) == 2  # a person's wheel stops where it should too
        await d.run_tool("a", "swipe", {"x0": 10, "y0": 300, "x1": 10, "y1": 100, "look": False})
        assert len(d.session.scrolls) == 2  # the raw swipe tool keeps its fling
        await finish(stop, task)
    run(go)


def test_the_swipe_stays_between_the_bars_read_from_the_tree():
    async def go():
        d = make()
        stop, task = await started(d)
        page = el("e1", "scroll", "Page", rect=(0, 0, 400, 800))
        await looked(d, [page])
        d.session.last_insets = (100.0, 80.0)  # a navigation bar ends at y 100; a tab bar starts at y 720
        for direction in ("down", "up"):
            seen = []
            original = d.session.swipe

            async def swipe(x0, y0, x1, y1, duration_ms=350, hold_ms=0):
                seen.append((y0, y1))
                return await original(x0, y0, x1, y1, duration_ms, hold_ms)
            d.session.swipe = swipe
            await d.run_tool("a", "scroll", {"direction": direction, "amount": 1.0, "look": False})
            d.session.swipe = original
            assert all(100 < y < 720 for y in seen[0]), (direction, seen)
        await finish(stop, task)
    run(go)


def test_the_screen_fallback_keeps_off_the_bars_too():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.last_insets = (100.0, 80.0)
        await d.run_tool("a", "scroll", {"direction": "down", "amount": 1.0, "look": False})
        y0 = d.session.last_point[1]
        assert 100 < y0 < 720
        await finish(stop, task)
    run(go)


def test_the_innermost_scroll_view_is_the_area():
    async def go():
        d = make()
        stop, task = await started(d)
        outer = el("e1", "scroll", "Page", rect=(0, 0, 400, 800))
        inner = el("e2", "scroll", "Feed", rect=(0, 300, 400, 200))
        await looked(d, [outer, inner])
        r = await d.run_tool("a", "scroll", {"look": False})  # both hold the screen's centre
        assert r["text"].startswith('Scrolled e2 scroll "Feed"')
        r = await d.run_tool("a", "scroll", {"target": {"x": 50, "y": 350}, "look": False})
        assert re.match(r'Scrolled e\d+ scroll "Feed"', r["text"])  # a point reads the screen again: new refs
        await finish(stop, task)
    run(go)


def test_a_null_direction_is_a_direction_left_out():
    """The MCP server sends every argument, `direction: null` when the agent gave none: with `to: start` that
    once came as "down" and was refused, so the shared runner's lab reset never scrolled up (2026-11-09)."""
    async def go():
        d = make()
        stop, task = await started(d)
        ends = []
        original = d.session.swipe

        async def swipe(x0, y0, x1, y1, duration_ms=350, hold_ms=0):
            ends.append((y0, y1))
            return await original(x0, y0, x1, y1, duration_ms, hold_ms)
        d.session.swipe = swipe
        r = await d.run_tool("a", "scroll", {"direction": None, "to": "start", "look": False})
        assert r["text"].splitlines()[0] == "Scrolled the screen up." and ends[0][0] < ends[0][1]
        r = await d.run_tool("a", "scroll", {"direction": None, "look": False})
        assert r["text"] == "Scrolled the screen down." and ends[-1][0] > ends[-1][1]
        await finish(stop, task)
    run(go)


def test_scroll_checks_its_arguments():
    async def go():
        d = make()
        stop, task = await started(d)
        for bad in ({"direction": "sideways"}, {"amount": 0.05}, {"amount": 1.5}, {"to": "middle"},
                    {"to": "start", "direction": "down"}):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "scroll", bad)
            assert e.value.code == "bad_params", bad
        assert "swipe" not in d.session.calls
        await finish(stop, task)
    run(go)


def test_a_scroll_counts_as_moving_content_for_the_refs_it_carries():
    async def go():
        d = make()
        stop, task = await started(d)
        await looked(d, [el("e1", "button", "OK")])
        await d.run_tool("a", "scroll", {"look": False})
        assert d.control.client("a").moved  # no list came back: the next lookup reads the tree again
        await finish(stop, task)
    run(go)


def scripted(d, *trees):
    """The session's refresh() (the tree-only read) gives these trees in turn; the last one stays."""
    queue, real = list(trees), type(d.session).refresh.__get__(d.session)

    async def refresh(timeout=None):
        d.session.tree = queue.pop(0) if len(queue) > 1 else queue[0]
        return await real(timeout)
    d.session.refresh = refresh


def test_wait_for_text_returns_once_it_shows_up():
    async def go():
        d = make()
        stop, task = await started(d)
        loading = [el("e1", "text", "Loading")]
        done = [el("e1", "text", "Loading"), el("e2", "text", "All done")]
        await looked(d, loading)
        scripted(d, loading, loading, loading, done)
        r = await d.run_tool("a", "wait_for", {"text": "all done", "timeout": 10})
        lines = r["text"].splitlines()
        assert lines[0] == "Done after 0.9 s." and lines[1] == "Same screen. Changes:" and lines[2].startswith("+ e2 text")
        assert not any(line.startswith("Settled") for line in lines)
        await finish(stop, task)
    run(go)


def test_wait_for_gone_by_id_and_by_label():
    async def go():
        d = make()
        stop, task = await started(d)
        spinner = [el("e1", "text", "Loading", identifier="spinner")]
        await looked(d, spinner)
        scripted(d, spinner, spinner, [el("e1", "button", "OK")])
        r = await d.run_tool("a", "wait_for", {"id": "spinner", "gone": True})
        assert r["text"].splitlines()[0] == "Done after 0.6 s."
        scripted(d, [el("e1", "button", "Save")])
        r = await d.run_tool("a", "wait_for", {"label": "SAVE"})  # exact, ignoring case
        assert r["text"].splitlines()[0] == "Done after 0.0 s."
        await finish(stop, task)
    run(go)


def test_wait_for_times_out_with_the_timeout_code():
    async def go():
        d = make()
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "wait_for", {"text": "never", "timeout": 1})
        assert e.value.code == "timeout"
        assert str(e.value) == "Waited 1 s for text \"never\" to appear; it didn't happen."
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "wait_for", {"label": "OK", "gone": True, "timeout": 0.5})
        assert str(e.value) == "Waited 0.5 s for label \"OK\" to disappear; it didn't happen."
        await finish(stop, task)
    run(go)


def test_wait_for_never_reads_with_no_time_left():
    """On the phone a read was given `min(5, left)` with left at 0: it timed out at once, counted as a lost
    connection, and the daemon reconnected (2026-11-09). Every read gets a real deadline."""
    async def go():
        d = make()
        stop, task = await started(d)
        given = []
        real = type(d.session).refresh.__get__(d.session)

        async def refresh(timeout=None):
            given.append(timeout)
            return await real(timeout)
        d.session.refresh = refresh
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "wait_for", {"text": "never", "timeout": 1})
        assert e.value.code == "timeout"
        assert given and min(given) > 0.1
        await finish(stop, task)
    run(go)


def test_a_slow_read_in_wait_for_is_a_timeout_not_a_lost_phone():
    """wait_for's own deadline running out mid-read answers `timeout`; the link to the phone is fine."""
    async def go():
        d = make()
        stop, task = await started(d)
        lost = []
        d.lost = lambda reason: lost.append(reason)

        async def slow_refresh(timeout=None):
            await asyncio.sleep(2.0)  # longer than the whole wait_for, and than the first read's own time
        real, d.session.refresh = d.session.refresh, slow_refresh
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "wait_for", {"text": "never", "timeout": 0.5})
        assert e.value.code == "timeout" and not lost and d.state == "live"
        # no read came back: the answer says so, and doesn't claim the text wasn't there
        assert str(e.value) == ("The phone didn't answer within 0.7 s, so it isn't known whether text \"never\" is on "
                                "screen; look, then wait again.")

        # a read slower than the wait, but inside the first read's own time: the screen is seen once, and the
        # answer is about the screen ("it didn't happen"), 12 of 12 times "not known" over Wi-Fi before
        async def slowish(timeout=None):
            await asyncio.sleep(0.6)
            return await real(timeout)
        d.session.refresh = slowish
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "wait_for", {"text": "never", "timeout": 0.5})
        assert str(e.value) == 'Waited 0.5 s for text "never" to appear; it didn\'t happen.'
        r = await d.run_tool("a", "wait_for", {"text": "OK", "timeout": 0.5, "look": False})
        assert r["text"].startswith("Done after")
        await finish(stop, task)
    first = daemon_module.WAIT_FOR_FIRST_READ
    daemon_module.WAIT_FOR_FIRST_READ = 0.7
    try:
        run(go)
    finally:
        daemon_module.WAIT_FOR_FIRST_READ = first


def test_wait_for_checks_its_arguments():
    async def go():
        d = make()
        stop, task = await started(d)
        for bad in ({}, {"text": "a", "id": "b"}, {"text": ""}, {"text": 5}, {"text": "a", "timeout": 0.1},
                    {"text": "a", "timeout": 31}, {"text": "a", "timeout": "soon"}, {"text": "a", "gone": "yes"}):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "wait_for", bad)
            assert e.value.code == "bad_params", bad
        assert d.session.calls.count("refresh") == 0
        await finish(stop, task)
    run(go)


def test_wait_for_answers_from_its_last_poll_without_reading_again():
    async def go():
        d = make()
        stop, task = await started(d)
        loading = [el("e1", "text", "Loading")]
        done = [el("e1", "text", "Loading"), el("e2", "text", "All done")]
        await looked(d, loading)
        scripted(d, loading, loading, done)
        before = d.session.calls.count("refresh")
        r = await d.run_tool("a", "wait_for", {"text": "all done"})
        assert d.session.calls.count("refresh") - before == 3 and d.session.calls.count("snapshot") == 0
        assert d.session.calls.count("app") == 1  # the foreground app is looked up once, for the answer
        assert r["text"].splitlines()[2].startswith("+ e2 text")
        assert [e.ref for e in d.control.client("a").items] == ["e1", "e2"]
        await finish(stop, task)
    run(go)


def test_each_wait_for_read_gets_its_own_deadline():
    """wait_for's own deadline is kept by cancelling a read, never by shrinking the read's deadline:
    a read with almost no time once failed at once and made the daemon reconnect (2026-11-09)."""
    async def go():
        d = make()
        stop, task = await started(d)
        with pytest.raises(Refused):
            await d.run_tool("a", "wait_for", {"text": "never", "timeout": 2})
        timeouts = d.session.read_timeouts
        assert timeouts and set(timeouts) == {5.0}
        d.session.read_timeouts.clear()
        await d.run_tool("a", "wait_for", {"label": "OK"})
        assert d.session.read_timeouts == [5.0]  # never more than 5 s for one read
        await finish(stop, task)
    run(go)


def test_waiting_for_something_to_go_that_was_never_there_succeeds_at_once():
    async def go():
        d = make()
        stop, task = await started(d)
        r = await d.run_tool("a", "wait_for", {"text": "ghost", "gone": True})
        assert r["text"].splitlines()[0] == "Done after 0.0 s." and d.session.calls.count("refresh") == 1 and d.session.calls.count("app") == 1
        await finish(stop, task)
    run(go)


def test_type_text_does_not_take_the_value_from_another_input_that_got_the_ref():
    async def go():
        d = make()
        stop, task = await started(d)
        await looked(d, [inp("e1", "Name"), inp("e2", "Email", y=160)])
        d.session.tree = [inp("e1", "Email", value="zzz")]  # after the submit, a new screen: e1 is another field
        d.session.app = ("Other", "com.example.other")  # a new app renumbers the list from e1
        r = await d.run_tool("a", "type_text", {"text": "Ada", "target": {"ref": "e1"}, "submit": True})
        first = r["text"].splitlines()[0]
        assert first == 'Typed into e1 input "Name" @10,100 300x44.' and "zzz" not in first
        await finish(stop, task)
    run(go)


def test_a_secure_field_never_has_its_value_shown():
    async def go():
        d = make()
        stop, task = await started(d)
        secret = el("e1", "input", "Password", rect=(10, 100, 300, 44), secure=True)
        await looked(d, [secret])
        d.session.tree = [el("e1", "input", "Password", value="••••", rect=(10, 100, 300, 44), secure=True)]
        r = await d.run_tool("a", "type_text", {"text": "hunter2", "target": {"ref": "e1"}})
        first = r["text"].splitlines()[0]
        assert first == 'Typed into e1 input "Password" value="••••" @10,100 300x44. The value is hidden (secure field).'
        assert "Value is now" not in r["text"]
        await finish(stop, task)
    run(go)


def test_type_text_ambiguity_says_how_many_more_inputs_there_are():
    async def go():
        d = make()
        stop, task = await started(d)
        await looked(d, [inp(f"e{i}", f"Field {i}", y=50 * i) for i in range(1, 8)])
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "type_text", {"text": "x"})
        lines = str(e.value).splitlines()
        assert lines[0].startswith("7 inputs on screen;") and len(lines) == 2 + 5 + 2
        assert lines[-2] == '  (+2 more; use a ref or "index")'
        assert lines[-1] == 'Retry with one of the refs as the target.'
        await finish(stop, task)
    run(go)


# ---- refusals before acting (Task 8a) ----
def test_a_disabled_element_is_refused_before_the_touch():
    async def go():
        d = make()
        stop, task = await started(d)
        await looked(d, [el("e1", "button", "Save", enabled=False)])
        for tool in ("tap", "long_press"):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", tool, {"target": {"ref": "e1"}})
            assert e.value.code == "not_actionable"
            assert str(e.value) == 'disabled: e1 button "Save" disabled @10,10 100x40 is disabled.'
        assert "tap" not in d.session.calls
        await finish(stop, task)
    run(go)


def test_force_never_overrides_disabled():
    async def go():
        d = make()
        stop, task = await started(d)
        await looked(d, [el("e1", "button", "Save", enabled=False),
                         el("e2", "switch", "Wi-Fi", checked=False, enabled=False, rect=(300, 100, 60, 30))])
        for tool, ref in (("tap", "e1"), ("long_press", "e1"), ("toggle", "e2")):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", tool, {"target": {"ref": ref}, "force": True, "look": False})
            assert e.value.code == "not_actionable" and str(e.value).endswith(" is disabled.")
            assert "force" not in str(e.value)
        assert "tap" not in d.session.calls
        await finish(stop, task)
    run(go)


ALERT = ui.Alert("Allow notifications?", ("Allow", "Don't Allow"), (50.0, 300.0, 300.0, 200.0))


def alerted():
    return [el("e1", "button", "Allow", in_alert=True, rect=(100, 400, 100, 44)),
            el("e2", "button", "Save", rect=(10, 100, 100, 44))]


def test_an_element_behind_an_alert_is_refused_but_the_alerts_buttons_work():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.last_alert = ALERT
        await looked(d, alerted())
        for tool in ("tap", "long_press"):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", tool, {"target": {"ref": "e2"}})
            assert e.value.code == "not_actionable"
            assert str(e.value) == ('behind_alert: an alert covers it: "Allow notifications?" [Allow] [Don\'t Allow]; '
                                    'answer it with alert, or pass force: true.')
        assert "tap" not in d.session.calls
        await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False})
        assert "tap" in d.session.calls
        await finish(stop, task)
    run(go)


def test_force_overrides_behind_alert_and_a_point_is_never_checked():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.last_alert = ALERT
        await looked(d, alerted())
        await d.run_tool("a", "tap", {"target": {"ref": "e2"}, "force": True, "look": False})
        assert d.session.calls.count("tap") == 1
        await d.run_tool("a", "tap", {"target": {"x": 20, "y": 120}, "look": False})  # a real touch lands where it lands
        assert d.session.calls.count("tap") == 2
        await finish(stop, task)
    run(go)


def test_type_text_takes_force_as_tap_does():
    """The focusing tap is a touch: behind an alert it is refused, and `force` makes it anyway (2026-11-10)."""
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.last_alert = ALERT
        name = el("e2", "input", "Name", "name-field", rect=(10, 100, 300, 44))
        await looked(d, [alerted()[0], name])
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "type_text", {"target": {"ref": "e2"}, "text": "x", "look": False})
        assert str(e.value).startswith("behind_alert: ") and "type_text" not in d.session.calls
        r = await d.run_tool("a", "type_text", {"target": {"ref": "e2"}, "text": "x", "force": True, "look": False})
        assert r["text"].startswith("Typed into e2 input") and d.session.calls.count("type_text") == 1
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "type_text", {"target": {"ref": "e2"}, "text": "x", "force": "yes"})
        assert e.value.code == "bad_params"
        await finish(stop, task)
    run(go)


def test_back_that_changed_nothing_names_what_is_still_open():
    """A first-time agent called `back` twice on a sheet it can't close: the answer now says what to do."""
    async def go():
        d = make()
        stop, task = await started(d)
        d.hub.publish(b"A")
        sheet = el("e1", "other", "", "lab-sheet", rect=(0, 647, 430, 285))
        await looked(d, [sheet, el("e2", "button", "Close", "lab-sheet-close", rect=(20, 844, 390, 49))])
        hidden = el("", "button", "Tap me", "lab-taps", rect=(16, 197, 398, 120))
        d.session.behind = [(hidden, el("e9", "other", "", "lab-sheet", rect=(0, 647, 430, 285)))]
        feeder = FrameFeeder(d.hub).start(then=b"A")  # the swipe changes nothing on the screen
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "back", {})
        assert str(e.value) == ("no_action: nothing changed after back; e1 other id=lab-sheet @0,647 430x285 "
                                "is still open: tap its close control.")
        await feeder.stop()
        await finish(stop, task)
    run(go)


async def sheet_open(d, front_rect=(0, 647, 430, 285), back_at=None):
    """The lab's sheet in front of a screen whose "Tap me" it hides; the agent has looked."""
    d.session.screen = (430.0, 932.0)
    d.hub.publish(b"A")
    sheet = el("e1", "other", "", "lab-sheet", rect=front_rect)
    close = el("e2", "button", "Close", "lab-sheet-close", rect=(20, front_rect[1] + front_rect[3] - 88, 390, 49))
    await looked(d, [sheet, close])
    hidden = el("", "button", "Back", "BackButton", rect=(19, 59, 106, 44))
    d.session.behind = [(hidden, el("e9", "other", "", "lab-sheet", rect=front_rect))]
    d.session.front_items, d.session.back_at = [sheet, close], back_at


def test_back_closes_a_sheet_by_a_touch_beside_it():
    """What a person does first with a sheet that has no back button: taps what it dims."""
    async def go():
        d = make()
        stop, task = await started(d)
        await sheet_open(d, back_at=(72.0, 81.0))  # the screen behind has a back button: the sheet hides it
        real = d.session.tap

        async def tap(target, hold_ms=60):
            d.session.behind, d.session.tree = [], [button("e5", "Tap me", "lab-taps")]  # the backdrop closes it
            return await real(target, hold_ms)
        d.session.tap = tap
        feeder = FrameFeeder(d.hub).start(then=b"B")
        r = await d.run_tool("a", "back", {})
        assert r["text"].startswith("Closed the sheet.")
        assert d.session.tap_points == [(215.0, (60.0 + 647.0) / 2)] and "swipe" not in d.session.calls
        await feeder.stop()
        await finish(stop, task)
    run(go)


def test_back_pulls_a_sheet_down_when_the_touch_beside_it_closed_nothing():
    async def go():
        d = make()
        stop, task = await started(d)
        await sheet_open(d)
        real = d.session.swipe

        async def swipe(x0, y0, x1, y1, duration_ms=350, hold_ms=0):
            d.session.behind, d.session.tree = [], [button("e5", "Tap me", "lab-taps")]
            d.session.pulled = (x0, y0, x1, y1)
            return await real(x0, y0, x1, y1, duration_ms, hold_ms)
        d.session.swipe = swipe
        feeder = FrameFeeder(d.hub).start(then=b"B")
        r = await d.run_tool("a", "back", {})
        assert r["text"].startswith("Closed the sheet.") and len(d.session.tap_points) == 1
        assert d.session.pulled == (215.0, 661.0, 215.0, 911.0)  # from just inside its top edge, straight down
        await feeder.stop()
        await finish(stop, task)
    run(go)


def test_back_finds_the_sheet_inside_a_backdrop_that_fills_the_screen():
    """As the phone lists the lab's second sheet: the backdrop first, full screen, then the panel. The first
    build took the backdrop for the sheet, found no room beside it and tried nothing."""
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.screen = (430.0, 932.0)
        d.hub.publish(b"A")
        backdrop = el("e1", "other", "", "lab-sheet2-backdrop", rect=(0, 0, 430, 932))
        panel = el("e2", "other", "", "lab-sheet2", rect=(0, 690, 430, 242))
        inside = button("e3", "Sheet 2 button", "lab-sheet2-button", rect=(20, 844, 390, 49))
        await looked(d, [backdrop, panel, inside])
        hidden = el("", "button", "Open sheet 2", "lab-open-sheet2", rect=(16, 835, 398, 49))
        d.session.behind, d.session.front_items = [(hidden, backdrop)], [backdrop, panel, inside]
        real = d.session.tap

        async def tap(target, hold_ms=60):
            d.session.behind, d.session.tree = [], [button("e5", "Open sheet 2", "lab-open-sheet2")]
            return await real(target, hold_ms)
        d.session.tap = tap
        feeder = FrameFeeder(d.hub).start(then=b"B")
        r = await d.run_tool("a", "back", {})
        assert r["text"].startswith("Closed the sheet.")
        assert d.session.tap_points == [(215.0, (60.0 + 690.0) / 2)]  # on the backdrop, above the panel
        await feeder.stop()
        await finish(stop, task)
    run(go)


def test_back_touches_nothing_while_the_sheet_is_still_sliding_in():
    """Seen on the phone 2026-11-10 over Wi-Fi: the share sheet was read while it slid up, the middle of the room
    above it was tapped half a second later, and by then that point was an app's icon in the sheet. Mail opened."""
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        places = [(200.0, 400.0), (200.0, 250.0), (200.0, 165.0), (200.0, 165.0)]  # the room above shrinks, then holds

        async def back_point():
            return ui.Back(False, None, places.pop(0) if len(places) > 1 else places[0])
        d.session.back_point = back_point
        r = await d.run_tool("a", "back", {"look": False})
        assert r["text"].startswith("Closed the sheet.") and d.session.tap_points == [(200.0, 165.0)]
        # one that never stands still: nothing is touched
        d.session.tap_points.clear()
        moving = iter(range(1000))

        async def sliding():
            return ui.Back(False, None, (200.0, 400.0 - 10 * next(moving)))
        d.session.back_point = sliding
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "back", {"look": False})
        assert e.value.code == "not_actionable" and "still moving" in str(e.value) and d.session.tap_points == []
        await finish(stop, task)
    pause = daemon_module.BACK_STILL_PAUSE
    daemon_module.BACK_STILL_PAUSE = 0.001
    try:
        run(go)
    finally:
        daemon_module.BACK_STILL_PAUSE = pause


def test_back_never_drags_from_the_top_of_the_screen():
    """A layer that fills the screen is no sheet to pull, and a drag from the top edge opens Notification Center."""
    async def go():
        d = make()
        stop, task = await started(d)
        await sheet_open(d, front_rect=(0, 0, 430, 932))
        feeder = FrameFeeder(d.hub).start(then=b"A")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "back", {})
        assert e.value.code == "not_actionable" and "is still open: tap its close control." in str(e.value)
        assert d.session.tap_points == [] and "swipe" not in d.session.calls
        await feeder.stop()
        await finish(stop, task)
    run(go)


def test_a_target_not_found_while_an_alert_is_up_says_the_alert_hides_it():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.last_alert = ALERT
        await looked(d, [el("e1", "button", "Allow", in_alert=True, rect=(100, 400, 100, 44))])
        for target in ({"id": "save"}, {"label": "Save"}, {"text": "Sav"}):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "tap", {"target": target})
            assert e.value.code == "not_actionable"
            assert str(e.value) == ('behind_alert: an alert covers it: "Allow notifications?" [Allow] [Don\'t Allow]; '
                                    'answer it with alert, or ask the user.')  # force can't touch what isn't there
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"ref": "e9"}})
        assert e.value.code == "not_found"  # only id, label and text say so
        d.session.last_alert = None
        await looked(d, [el("e1", "button", "Allow", rect=(100, 400, 100, 44))])
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"label": "Save"}})
        assert e.value.code == "not_found"  # no alert any more: an ordinary miss
        await finish(stop, task)
    run(go)


def test_disabled_comes_before_behind_alert_and_toggle_is_guarded_too():
    async def go():
        d = make()
        stop, task = await started(d)
        off = el("e2", "switch", "Wi-Fi", checked=False, enabled=False, rect=(300, 100, 60, 30))
        d.session.last_alert = ALERT
        await looked(d, [alerted()[0], off])
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "toggle", {"target": {"ref": "e2"}})
        assert e.value.code == "not_actionable" and str(e.value).startswith("disabled: e2 switch")
        on = el("e2", "switch", "Wi-Fi", checked=False, rect=(300, 100, 60, 30))
        await looked(d, [alerted()[0], on])
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "toggle", {"target": {"ref": "e2"}})
        assert str(e.value).startswith("behind_alert: ")
        assert "tap" not in d.session.calls
        await finish(stop, task)
    run(go)


def test_force_never_overrides_no_action():
    async def go():
        d = make()
        stop, task = await started(d)
        await looked(d, [el("e1", "button", "Save", enabled=False)])
        for tool, args in (("toggle", {"target": {"ref": "e1"}}), ("type_text", {"text": "x", "target": {"ref": "e1"}}),
                           ("scroll", {"target": {"ref": "e1"}})):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", tool, {**args, "force": True})
            assert e.value.code == "not_actionable" and str(e.value).startswith("no_action: ")
        assert "tap" not in d.session.calls
        await finish(stop, task)
    run(go)


def test_a_refusal_does_not_touch_the_phone_or_the_generation():
    async def go():
        d = make()
        stop, task = await started(d)
        await looked(d, [el("e1", "button", "Save", enabled=False)])
        generation = d.control.generation
        with pytest.raises(Refused):
            await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert d.control.generation == generation
        await finish(stop, task)
    run(go)


TAB = ui.Bar("TabBar", (0.0, 849.0, 430.0, 83.0), "tabbar @0,849 430x83")


def test_a_card_under_the_tab_bar_is_tapped_where_it_shows():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.screen = (430.0, 932.0)
        card = el("e1", "cell", "Card", rect=(20, 808, 390, 90), covered_by=(TAB,))
        await looked(d, [card])
        for target in ({"ref": "e1"}, {"label": "Card"}, {"text": "Card"}):
            r = await d.run_tool("a", "tap", {"target": target, "look": False})
            assert d.session.last_point == (215.0, 828.5)  # the middle of y 808-849, not of the whole card
            assert re.fullmatch(r'Tapped e\d cell "Card" @20,808 390x90 at its visible part \(215, 828\)\.', r["text"])
        await looked(d, [card])
        r = await d.run_tool("a", "long_press", {"target": {"ref": "e1"}, "look": False})
        assert d.session.last_point == (215.0, 828.5) and r["text"].startswith("Long-pressed e")
        await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "force": True, "look": False})
        assert d.session.last_point == (215.0, 853.0)  # forced: the card's own centre
        await d.run_tool("a", "tap", {"target": {"x": 215, "y": 853}, "look": False})
        assert d.session.last_point == (215.0, 853.0)  # a raw point stays raw
        await finish(stop, task)
    run(go)


def test_an_element_with_too_little_showing_is_refused():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.screen = (430.0, 932.0)
        await looked(d, [el("e1", "cell", "Card", rect=(20, 845, 390, 60), covered_by=(TAB,)),
                         el("e2", "cell", "Draft", rect=(20, 100, 390, 60),
                            covered_by=(ui.Bar("Toolbar", (0.0, 90.0, 430.0, 100.0), "toolbar @0,90 430x100"),))])
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert e.value.code == "not_actionable"
        assert str(e.value) == ('under_tab_bar: e1 cell "Card" @20,845 390x60 is under the tab bar iOS draws; '
                                'scroll to it first (scroll with this target and to: "target").')
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "long_press", {"target": {"ref": "e2"}})
        assert str(e.value) == ('covered: e2 cell "Draft" @20,100 390x60 is covered by toolbar @0,90 430x100; '
                                'scroll to it first (scroll with this target and to: "target").')
        assert "tap" not in d.session.calls
        await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "force": True, "look": False})
        assert d.session.last_point == (215.0, 875.0)
        await finish(stop, task)
    run(go)


# ---- later overlapping elements cover like bars (Task 8c) ----
def covered_card(cover_rect):
    x, y, w, h = (round(v) for v in cover_rect)
    cover = ui.Bar("Other", cover_rect, f"other id=lab-overlay @{x},{y} {w}x{h}")
    return el("e1", "button", "Covered", "lab-covered", rect=(20, 100, 390, 60), covered_by=(cover,))


def test_a_button_fully_under_a_later_element_is_refused():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.screen = (430.0, 932.0)
        await looked(d, [covered_card((10.0, 90.0, 410.0, 80.0))])
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert e.value.code == "not_actionable"
        assert str(e.value) == ('covered: e1 button "Covered" id=lab-covered @20,100 390x60 is covered by '
                                'other id=lab-overlay @10,90 410x80; move or close it first.')
        assert "tap" not in d.session.calls
        await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "force": True, "look": False})
        assert d.session.last_point == (215.0, 130.0)  # forced: its own centre
        await finish(stop, task)
    run(go)


def test_a_button_partly_under_a_later_element_is_touched_where_it_shows():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.screen = (430.0, 932.0)
        await looked(d, [covered_card((20.0, 100.0, 390.0, 40.0))])  # the top 40 of 60 points
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False})
        assert d.session.last_point == (215.0, 150.0) and "at its visible part (215, 150)" in r["text"]
        await finish(stop, task)
    run(go)


def test_a_badge_over_a_corner_leaves_the_centre_tap_alone():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.screen = (430.0, 932.0)
        await looked(d, [covered_card((390.0, 100.0, 20.0, 20.0))])
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False})
        assert d.session.last_point == (215.0, 130.0) and "visible part" not in r["text"]
        await finish(stop, task)
    run(go)


def test_a_target_that_is_only_off_screen_is_off_screen_not_not_found():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.screen = (430.0, 932.0)
        await looked(d, [el("e1", "button", "Top", "lab-top", rect=(16, 300, 398, 48))])
        d.session.offscreen = [el("", "button", "Bottom", "lab-bottom", rect=(16, 1500, 398, 48))]
        for target in ({"id": "lab-bottom"}, {"label": "Bottom"}, {"text": "bott"}):
            for force in (False, True):  # a touch can't reach it, forced or not
                with pytest.raises(Refused) as e:
                    await d.run_tool("a", "tap", {"target": target, "force": force})
                assert e.value.code == "not_actionable", target
                assert str(e.value) == ('off_screen: button "Bottom" id=lab-bottom @16,1500 398x48 is off screen; '
                                        'scroll to it first (scroll with this target and to: "target").')
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "nowhere"}})
        assert e.value.code == "not_found"
        assert "tap" not in d.session.calls
        await finish(stop, task)
    run(go)


def test_a_target_behind_an_open_sheet_is_covered_naming_the_sheet():
    """AGENT-TOOLS v1.4: not `not_found`, and not `off_screen` either when it is also scrolled away."""
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.screen = (430.0, 932.0)
        await looked(d, [el("e1", "other", "dismiss popup", "PopoverDismissRegion", rect=(0, 0, 430, 932))])
        save = el("", "button", "Save", "lab-save", rect=(16, -40, 193, 49))
        front = 'e1 other "dismiss popup" id=PopoverDismissRegion @0,0 430x932'
        # the read that found the layer numbered it for itself: the message carries the ref of the agent's list
        d.session.behind = [(save, el("e7", "other", "dismiss popup", "PopoverDismissRegion", rect=(0, 0, 430, 932)))]
        d.session.offscreen = [save]  # behind the sheet wins: scrolling can't help until it is closed
        for target in ({"id": "lab-save"}, {"label": "Save"}, {"text": "sav"}):
            for force in (False, True):
                with pytest.raises(Refused) as e:
                    await d.run_tool("a", "tap", {"target": target, "force": force})
                assert e.value.code == "not_actionable", target
                assert str(e.value) == (f'covered: button "Save" id=lab-save @16,-40 193x49 is behind {front}; '
                                        'close that first.')
        assert "tap" not in d.session.calls
        await finish(stop, task)
    run(go)


KEYBOARD = ui.Bar("Keyboard", (0.0, 614.0, 430.0, 318.0), "keyboard @0,614 430x318")


def test_toggle_touches_the_control_of_a_switch_that_is_a_whole_row():
    """iOS Settings' "Allow Notifications" is one switch element 350x29 from x 40, label and control together;
    toggle touched its middle, the label, and nothing changed (2026-11-11). It touches the control at the right."""
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.screen = (430.0, 932.0)
        await looked(d, [el("e1", "switch", "Allow Notifications", rect=(40, 145, 350, 29), checked=True),
                         el("e2", "switch", "Notify", rect=(351, 612, 63, 28), checked=False)])
        r = await d.run_tool("a", "toggle", {"target": {"ref": "e1"}, "look": False})
        assert d.session.last_point == (358.0, 159.5)
        assert r["text"].startswith('Tapped e1 switch "Allow Notifications" @40,145 350x29.')
        await d.run_tool("a", "toggle", {"target": {"ref": "e2"}, "look": False})
        assert d.session.calls[-1] == "tap" and d.session.last_point in (None, (382.5, 626.0))  # a switch-sized one: as before
        await finish(stop, task)
    run(go)


def test_toggle_finds_a_row_switchs_control_on_the_left_in_right_to_left_and_stays_in_the_visible_part():
    """Tapline's review of 932dbc4: in Arabic iOS puts the control at the left end and the label at the right; and
    a partly covered row must not be touched where it is covered."""
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.screen = (430.0, 932.0)
        await looked(d, [el("e1", "switch", "السماح", rect=(40, 145, 350, 29), checked=True),
                         el("e2", "button", "السماح", rect=(224, 146, 166, 26))])
        await d.run_tool("a", "toggle", {"target": {"ref": "e1"}, "look": False})
        assert d.session.last_point == (72.0, 159.5)  # 32 pt in from the left end, away from the label
        bar = ui.Bar("Keyboard", (300, 0, 130, 932), "keyboard")  # covers the row's right end, where the control is
        await looked(d, [el("e1", "switch", "Allow", rect=(40, 145, 350, 29), checked=True, covered_by=(bar,))])
        taps = d.session.calls.count("tap")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "toggle", {"target": {"ref": "e1"}, "look": False})
        assert e.value.code == "not_actionable" and str(e.value).startswith("covered: e1 switch")
        assert d.session.calls.count("tap") == taps  # nothing touched
        low = ui.Bar("Keyboard", (0, 152, 430, 780), "keyboard")  # covers the row from y 152, centre (154.5) included
        await looked(d, [el("e1", "switch", "Allow", rect=(40, 140, 350, 29), checked=True, covered_by=(low,))])
        await d.run_tool("a", "toggle", {"target": {"ref": "e1"}, "look": False})
        assert d.session.last_point == (358.0, 146.0)  # the control's end, at the middle of the 12 pt that show
        await finish(stop, task)
    run(go)


def test_toggle_refuses_a_switch_under_the_keyboard_and_touches_one_where_it_shows():
    """toggle touched a switch's centre with the keyboard over it, so the keyboard got the touch and the shared
    runner's toggles did nothing (2026-11-09). It now checks covers as tap does."""
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.screen = (430.0, 932.0)
        await looked(d, [el("e1", "switch", "Switch", "lab-switch", rect=(351, 612, 63, 28), checked=False,
                            covered_by=(KEYBOARD,)),
                         el("e2", "switch", "Tall", rect=(351, 590, 63, 50), checked=False, covered_by=(KEYBOARD,))])
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "toggle", {"target": {"ref": "e1"}})
        assert e.value.code == "not_actionable"
        assert str(e.value) == ('covered: e1 switch "Switch" id=lab-switch checked=false @351,612 63x28 is covered by '
                                'keyboard @0,614 430x318; scroll to it first (scroll with this target and to: "target").')
        assert "tap" not in d.session.calls
        r = await d.run_tool("a", "toggle", {"target": {"ref": "e2"}, "look": False})
        assert d.session.last_point == (382.5, 602.0)  # its centre (615) is under the keyboard: y 590-614 shows
        assert r["text"] == 'Tapped e2 switch "Tall" @351,590 63x50 at its visible part (382, 602).'
        await d.run_tool("a", "toggle", {"target": {"ref": "e1"}, "force": True, "look": False})
        assert d.session.last_point == (382.5, 626.0)  # forced: its own centre
        await finish(stop, task)
    run(go)


def test_typing_refuses_a_field_it_cannot_touch_and_focuses_one_where_it_shows():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.screen = (430.0, 932.0)
        await looked(d, [el("e1", "input", "Low", rect=(17, 590, 396, 50), covered_by=(KEYBOARD,)),
                         el("e2", "input", "Lower", rect=(17, 700, 396, 46), covered_by=(KEYBOARD,)),
                         el("e3", "input", "Off", rect=(17, 100, 396, 46), enabled=False)])
        await d.run_tool("a", "type_text", {"target": {"ref": "e1"}, "text": "hi", "look": False})
        assert d.session.last_point == (215.0, 602.0)  # the middle of y 590-614, above the keyboard
        for ref, reason in (("e2", "covered: e2 input"), ("e3", "disabled: e3 input")):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "type_text", {"target": {"ref": ref}, "text": "hi"})
            assert e.value.code == "not_actionable" and str(e.value).startswith(reason), ref
        assert len(d.session.typed) == 1
        await finish(stop, task)
    run(go)


# ---- review fixes (Task 8a, follow-up) ----
def test_an_alert_with_text_only_still_covers_the_app():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.last_alert = ui.Alert("Low battery", (), (50.0, 300.0, 300.0, 100.0))  # no button in the list
        await looked(d, [el("e1", "button", "Save", rect=(10, 100, 100, 44))])
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert str(e.value) == ('behind_alert: an alert covers it: "Low battery"; '
                                'answer it with alert, or pass force: true.')
        await finish(stop, task)
    run(go)


def test_an_alert_that_closed_covers_nothing_even_when_an_element_remembers_it():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.last_alert = ALERT
        await looked(d, alerted())
        d.session.last_alert = None
        await looked(d, alerted())  # the alert is gone; the list still has an element flagged in_alert
        await d.run_tool("a", "tap", {"target": {"ref": "e2"}, "look": False})
        assert "tap" in d.session.calls
        await finish(stop, task)
    run(go)


def test_an_element_under_several_bars_is_touched_in_the_strip_nearest_its_centre():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.screen = (430.0, 932.0)
        bars = (ui.Bar("NavigationBar", (0.0, 0.0, 430.0, 100.0), "navigationbar @0,0 430x100"),
                ui.Bar("Toolbar", (0.0, 430.0, 430.0, 100.0), "toolbar @0,430 430x100"),
                TAB)
        await looked(d, [el("e1", "cell", "Tall", rect=(20, 80, 390, 830), covered_by=bars),
                         el("e2", "cell", "Wide", rect=(20, 80, 390, 830), covered_by=bars[::2])])
        r = await d.run_tool("a", "tap", {"target": {"label": "Tall"}, "look": False})
        assert d.session.last_point == (215.0, 689.5) and "at its visible part (215, 690)" in r["text"]
        await d.run_tool("a", "tap", {"target": {"label": "Wide"}, "look": False})
        assert d.session.last_point == (215.0, 495.0)  # centre 495 is in no bar: the element's own centre
        await finish(stop, task)
    run(go)


def test_a_tab_inside_the_tab_bar_is_touched_at_its_own_centre():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.screen = (430.0, 932.0)
        await looked(d, [el("e1", "tab", "Home", rect=(0, 849, 215, 83))])  # its bar is its ancestor: not covered_by
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False})
        assert d.session.last_point == (107.5, 890.5) and "visible part" not in r["text"]
        await finish(stop, task)
    run(go)


# ---- a notification banner that takes a touch near the top (owner's choice 2026-11-11: detect, go back, report) ----
TAKEN = "Mail (com.apple.mobilemail) came to the front after the touch"


@pytest.mark.parametrize("look", [True, False])
def test_a_banner_that_takes_a_touch_near_the_top_is_reported_and_the_app_opened_again(look):
    """On the phone, twice on 2026-11-11, a Mail banner landed between the banner check and a touch near the top,
    and the touch opened Mail. The app the touch was meant for is opened again, and the answer says so."""
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        s.app_names["com.apple.mobilemail"] = "Mail"  # as Session reads them at connect
        s.foreground, s.tree = "com.example.app", [button("e1", "Top", "top", rect=(10, 120, 100, 40))]
        await d.run_tool("a", "look", {"screenshot": False})
        s.taken_by = ("Mail", "com.apple.mobilemail")
        r = await d.run_tool("a", "tap", {"target": {"id": "top"}, "look": look})
        assert s.launched == ["com.example.app"] and s.foreground == "com.example.app"
        assert f"Note: {TAKEN}" in r["text"] and "Went back to Example (com.example.app)" in r["text"]
        assert r["text"].startswith('Tapped e1 button "Top"')
        if look:
            assert "\nScreen " in r["text"]  # a whole list, read after the app came back to the front
        await finish(stop, task)
    run(go)


def test_an_app_that_comes_to_the_front_a_moment_after_a_look_false_touch_is_caught_at_the_next_call():
    """With look: false the answer asks the phone once, at once; Mail came to the front a moment later on the phone
    (2026-11-11). The agent's next call asks again: an action is refused stale with the note, a look carries it."""
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        s.app_names["com.apple.mobilemail"] = "Mail"
        s.foreground, s.tree = "com.example.app", [button("e1", "Top", "top", rect=(10, 120, 100, 40))]
        await d.run_tool("a", "look", {"screenshot": False})
        r = await d.run_tool("a", "tap", {"target": {"id": "top"}, "look": False})
        assert "came to the front" not in r["text"]  # nothing yet
        s.foreground, s.app = "com.apple.mobilemail", ("Mail", "com.apple.mobilemail")  # a moment later
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "top"}, "look": False})
        assert e.value.code == "stale" and TAKEN in str(e.value) and "This call did nothing." in str(e.value)
        assert s.launched == ["com.example.app"]
        r = await d.run_tool("a", "look", {"screenshot": False})  # asked once only: nothing more to say
        assert "came to the front" not in r["text"]
        await d.run_tool("a", "tap", {"target": {"id": "top"}, "look": False})
        s.foreground, s.app = "com.apple.mobilemail", ("Mail", "com.apple.mobilemail")
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert f"Note: {TAKEN}" in r["text"] and s.launched == ["com.example.app", "com.example.app"]
        await finish(stop, task)
    run(go)


def _top_screen(s, app_names=True):
    if app_names:
        s.app_names["com.apple.mobilemail"] = "Mail"
    s.foreground, s.tree = "com.example.app", [button("e1", "Top", "top", rect=(10, 120, 100, 40))]


def test_a_view_over_the_app_from_another_process_is_not_taken_for_a_theft():
    """Tapline's review: Sign in with Apple, Apple Pay, a Safari view are other processes over the app; WebDriverAgent
    names them in front while the app still runs in the foreground. Reopening the app would close them."""
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        _top_screen(s)
        await d.run_tool("a", "look", {"screenshot": False})

        async def still_running():
            return ["com.example.app"]
        s.device.foreground_apps = still_running
        s.taken_by = ("Sign in with Apple", "com.apple.AuthKitUIService")
        for look in (True, False):
            r = await d.run_tool("a", "tap", {"target": {"id": "top"}, "look": look})
            assert s.launched == [] and "came to the front" not in r["text"]
            s.foreground, s.taken_by = "com.example.app", ("Sign in with Apple", "com.apple.AuthKitUIService")
            await d.run_tool("a", "look", {"screenshot": False})
        await finish(stop, task)
    run(go)


def test_a_shade_pulled_down_over_the_app_is_a_theft():
    """Tapline's review: a drag from the top edge pulls Notification Center over the app: SpringBoard in front, the app
    still running under it, and it stays (a banner would leave). The app is opened again."""
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        _top_screen(s)
        await d.run_tool("a", "look", {"screenshot": False})

        async def still_running():
            return ["com.example.app"]
        s.device.foreground_apps = still_running
        s.taken_by = ("Home screen", "com.apple.springboard")
        r = await d.run_tool("a", "tap", {"target": {"id": "top"}, "look": True})
        assert s.launched == ["com.example.app"]
        assert "Note: the home screen, Notification Center or Control Center came to the front" in r["text"]
        await finish(stop, task)
    run(go)


def test_the_next_call_check_is_skipped_after_a_person_acted_or_after_a_while(monkeypatch):
    """Tapline's review: a person may open another app on purpose between the agent's calls, and a theft shows within
    about a second; neither is reopened over."""
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        _top_screen(s)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "tap", {"target": {"id": "top"}, "look": False})
        d.control.person_generation = d.control.generation + 1  # a person acted since
        s.foreground, s.app = "com.apple.mobilemail", ("Mail", "com.apple.mobilemail")
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert s.launched == [] and "came to the front" not in r["text"]
        s.foreground, s.app = "com.example.app", ("Example", "com.example.app")
        await d.run_tool("a", "look", {"screenshot": False})
        monkeypatch.setattr(daemon_module, "RECHECK_WITHIN", 0.0)
        await d.run_tool("a", "tap", {"target": {"id": "top"}, "look": False})
        s.foreground, s.app = "com.apple.mobilemail", ("Mail", "com.apple.mobilemail")
        await asyncio.sleep(0.01)
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert s.launched == [] and "came to the front" not in r["text"]
        await finish(stop, task)
    run(go)


def test_an_answer_with_a_read_also_leaves_the_check_for_the_next_call():
    """Tapline's review: the answer's own read can come before the other app reaches the front, look true too."""
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        _top_screen(s)
        await d.run_tool("a", "look", {"screenshot": False})
        r = await d.run_tool("a", "tap", {"target": {"id": "top"}, "look": True})
        assert "came to the front" not in r["text"]
        s.foreground, s.app = "com.apple.mobilemail", ("Mail", "com.apple.mobilemail")  # a moment later
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "top"}, "look": False})
        assert e.value.code == "stale" and TAKEN in str(e.value) and s.launched == ["com.example.app"]
        await finish(stop, task)
    run(go)


def test_in_personal_mode_the_agent_hears_of_the_banner_not_not_allowed():
    """Tapline's review: personal mode's own check of the app in front came first and answered not_allowed; the check
    for a theft now runs before it, and reopening the given app is inside the scope."""
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        _top_screen(s)
        mine = Scope(("com.example.app",))
        await d.run_tool("a", "look", {"screenshot": False}, scope=mine)
        await d.run_tool("a", "tap", {"target": {"id": "top"}, "look": False}, scope=mine)
        s.foreground, s.app = "com.apple.mobilemail", ("Mail", "com.apple.mobilemail")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "top"}, "look": False}, scope=mine)
        assert e.value.code == "stale" and TAKEN in str(e.value) and s.launched == ["com.example.app"]
        await finish(stop, task)
    run(go)


def test_apps_leaves_the_next_call_check_for_the_call_after():
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        _top_screen(s)
        s.installed.append({"bundle_id": "com.apple.mobilemail", "name": "Mail", "version": "1", "build": "1"})
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "tap", {"target": {"id": "top"}, "look": False})
        s.foreground, s.app = "com.apple.mobilemail", ("Mail", "com.apple.mobilemail")
        await d.run_tool("a", "apps", {})
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert f"Note: {TAKEN}" in r["text"] and s.launched == ["com.example.app"]
        await finish(stop, task)
    run(go)


def test_the_home_screen_is_never_the_app_a_touch_was_meant_for():
    """A read while a banner slides over an app names SpringBoard; the next touch near the top then "went back" to
    the home screen, over Settings (on the phone 2026-11-11). Nor is a home screen icon that opens an app a theft."""
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        s.foreground, s.app = "com.apple.springboard", ("Home screen", "com.apple.springboard")
        s.tree = [button("e1", "Settings", "settings", rect=(10, 120, 100, 40))]
        await d.run_tool("a", "look", {"screenshot": False})
        s.taken_by = ("Settings", "com.apple.Preferences")
        for look in (False, True):
            await d.run_tool("a", "tap", {"target": {"id": "settings"}, "look": look})
            await d.run_tool("a", "look", {"screenshot": False})
        assert s.launched == []
        await finish(stop, task)
    run(go)


def test_a_touch_lower_down_that_opens_another_app_is_left_alone():
    """Banners come down over the top only: a link lower down that opens another app is the app's own doing."""
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        s.foreground, s.tree = "com.example.app", [button("e1", "Link", "link", rect=(10, 500, 100, 40))]
        await d.run_tool("a", "look", {"screenshot": False})

        async def opens_safari(target, hold_ms=60, _tap=s.tap):
            out = await _tap(target, hold_ms)
            s.foreground, s.app = "com.apple.mobilesafari", ("Safari", "com.apple.mobilesafari")
            return out
        s.tap = opens_safari
        r = await d.run_tool("a", "tap", {"target": {"id": "link"}, "look": False})
        assert s.launched == [] and "came to the front" not in r["text"]
        await finish(stop, task)
    run(go)


def test_an_alert_the_touch_raised_is_not_taken_for_a_banner():
    """A permission prompt puts SpringBoard in front; it is the app's own business, answered with `alert`."""
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        s.foreground, s.tree = "com.example.app", [button("e1", "Allow photos", "ask", rect=(10, 120, 100, 40))]
        await d.run_tool("a", "look", {"screenshot": False})
        s.taken_by = ("SpringBoard", "com.apple.springboard")
        s.last_alert = ui.Alert("Allow access to your photos?", ("Allow", "Don't Allow"), (40, 300, 350, 200))
        r = await d.run_tool("a", "tap", {"target": {"id": "ask"}, "look": True})
        assert s.launched == [] and "came to the front" not in r["text"]
        await finish(stop, task)
    run(go)


def test_a_row_under_a_translucent_navigation_bar_is_touched_below_it():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.screen = (430.0, 932.0)
        nav = ui.Bar("NavigationBar", (0.0, 0.0, 430.0, 100.0), "navigationbar @0,0 430x100")
        await looked(d, [el("e1", "cell", "Row 0", rect=(0, 40, 430, 100), covered_by=(nav,))])
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False})
        assert d.session.last_point == (215.0, 120.0) and "at its visible part (215, 120)" in r["text"]
        await finish(stop, task)
    run(go)

# ---- closing the keyboard first (AGENT-TOOLS v1.4) ----
KEYBOARD_NOTE = "Note: closed the keyboard first."
KEYBOARD_BAR = ui.Bar("Keyboard", (0, 500, 400, 300), "keyboard")


async def keyboard_up(shown: bool = True, focused: str = "a", **session_options):
    """A daemon whose screen has the keyboard up over its lower part, field A focused, and the agent has looked."""
    d = make()
    s = d.session
    s.tree = [
        el("e1", "text", "Title", rect=(10, 70, 100, 20), type="StaticText"),
        el("e2", "input", "Field A", "a", rect=(10, 100, 300, 40)),
        el("e3", "input", "Field B", "b", rect=(10, 160, 300, 40)),
        button("e4", "Save", "save", rect=(10, 300, 100, 40)),
        el("e5", "switch", "Notify", "notify", rect=(10, 360, 100, 40), checked=False),
        button("e6", "Low", "low", rect=(10, 600, 100, 40), covered_by=(KEYBOARD_BAR,)),
    ]
    s.last_keyboard, s.last_insets, s.focused = shown, (60.0, 300.0), focused
    for key, value in session_options.items():
        setattr(s, key, value)
    stop, task = await started(d)
    await d.run_tool("a", "look", {"screenshot": False})
    return d, stop, task


@pytest.mark.parametrize("tool,args,end", [
    ("tap", {"target": {"id": "save"}}, (60.0, 320.0)),
    ("long_press", {"target": {"id": "save"}}, (60.0, 320.0)),
    ("toggle", {"target": {"id": "notify"}}, (60.0, 380.0)),
])
def test_a_touch_outside_the_focused_input_closes_the_keyboard_first(tool, args, end):
    async def go():
        d, stop, task = await keyboard_up()
        r = await d.run_tool("a", tool, args)
        # one touch where nothing responds (the centre of the label "Title"), then the target's own touch
        assert d.session.tap_points == [(60.0, 80.0), end]
        assert "dismiss_keyboard" not in d.session.calls  # the blank touch was enough
        assert r["text"].endswith(KEYBOARD_NOTE)
        assert "Keyboard: hidden" in r["text"]
        await finish(stop, task)
    run(go)


def test_type_text_into_another_input_above_the_keyboard_leaves_the_keyboard_up():
    """Measured on the phone 2026-11-10: closing the keyboard between two fields made the second type_text take
    3.4 s against 1.7 s. A person filling a form taps the next field; so does the tool."""
    async def go():
        d, stop, task = await keyboard_up()
        r = await d.run_tool("a", "type_text", {"target": {"id": "a"}, "text": "x"})
        assert d.session.tap_points == [] and KEYBOARD_NOTE not in r["text"]  # field A: the fast path, no blank tap
        r = await d.run_tool("a", "type_text", {"target": {"id": "b"}, "text": "y"})
        assert d.session.tap_points == [] and "dismiss_keyboard" not in d.session.calls  # nothing closed it
        assert KEYBOARD_NOTE not in r["text"]
        assert [t[0] for t in d.session.typed] == ["x", "y"]
        await finish(stop, task)
    run(go)


@pytest.mark.parametrize("top,closes", [(483, False), (495, True)])
def test_type_text_into_an_input_partly_under_the_keyboard(top, closes):
    """AGENT-TOOLS §4 (v1.6): with at least 10 pt of it above the keyboard, a person taps that part and focus moves;
    with less, the keyboard is closed first. The lab's second field shows 17 pt (closing first cost 2 s there)."""
    async def go():
        d, stop, task = await keyboard_up()
        part = el("e7", "input", "Field C", "c", rect=(10, top, 300, 40), covered_by=(KEYBOARD_BAR,))
        d.session.tree = [*d.session.tree, part]
        await d.run_tool("a", "look", {"screenshot": False})
        d.session.tap_points.clear()
        blank = (60.0, 80.0)  # the touch on the label "Title" that closes the keyboard
        real = d.session.tap

        async def tap(target, hold_ms=60):
            if (target.x, target.y) == blank:  # the keyboard goes, and with it what it covered
                d.session.tree = [*d.session.tree[:-1], el("e7", "input", "Field C", "c", rect=(10, top, 300, 40))]
            return await real(target, hold_ms)
        d.session.tap = tap
        await d.run_tool("a", "type_text", {"target": {"id": "c"}, "text": "x"})
        assert (blank in d.session.tap_points) == closes
        await finish(stop, task)
    run(go)


def test_type_text_closes_the_keyboard_after_all_when_the_other_input_did_not_take_focus():
    """Some apps give the first tap outside the focused field to the keyboard: then the old way, and a second tap."""
    async def go():
        d, stop, task = await keyboard_up()
        d.session.fail["type_text"] = Refused("not_actionable", "no_action: input \"Field B\" didn't take focus.")
        r = await d.run_tool("a", "type_text", {"target": {"id": "b"}, "text": "y"})
        assert d.session.tap_points == [(60.0, 80.0)]  # the touch where nothing responds, which closes it
        assert [t[0] for t in d.session.typed] == ["y"] and KEYBOARD_NOTE not in r["text"]
        await finish(stop, task)
    run(go)


def test_a_tap_on_the_focused_input_itself_closes_nothing():
    async def go():
        d, stop, task = await keyboard_up()
        r = await d.run_tool("a", "tap", {"target": {"id": "a"}})
        assert d.session.tap_points == [(160.0, 120.0)] and KEYBOARD_NOTE not in r["text"]
        r = await d.run_tool("a", "tap", {"target": {"x": 100, "y": 120}})  # a point on it is the same
        assert d.session.tap_points[1:] == [(100.0, 120.0)] and KEYBOARD_NOTE not in r["text"]
        await finish(stop, task)
    run(go)


def test_no_keyboard_means_no_extra_tap_and_no_note():
    async def go():
        d, stop, task = await keyboard_up(shown=False)
        r = await d.run_tool("a", "tap", {"target": {"id": "save"}})
        assert d.session.tap_points == [(60.0, 320.0)] and KEYBOARD_NOTE not in r["text"]
        assert "dismiss_keyboard" not in d.session.calls and "is_focused" not in d.session.calls
        await finish(stop, task)
    run(go)


def test_the_dismissal_closes_it_when_the_blank_touch_does_not():
    async def go():
        d, stop, task = await keyboard_up(blank_tap_closes=False, dismiss_closes=True)
        r = await d.run_tool("a", "tap", {"target": {"id": "save"}})
        assert d.session.calls.count("dismiss_keyboard") == 1
        assert d.session.tap_points == [(60.0, 80.0), (60.0, 320.0)] and r["text"].endswith(KEYBOARD_NOTE)
        await finish(stop, task)
    run(go)


def test_when_nothing_closes_it_there_is_no_note_and_covered_still_refuses():
    async def go():
        d, stop, task = await keyboard_up(blank_tap_closes=False)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "low"}})
        assert e.value.code == "not_actionable" and str(e.value).startswith("covered:")
        assert d.session.calls.count("dismiss_keyboard") == 1  # tried once
        r = await d.run_tool("a", "tap", {"target": {"id": "save"}})
        assert KEYBOARD_NOTE not in r["text"]  # still touched, as before
        await finish(stop, task)
    run(go)


def test_a_point_on_the_keyboard_is_a_key_and_closes_nothing():
    async def go():
        d, stop, task = await keyboard_up()
        r = await d.run_tool("a", "tap", {"target": {"x": 100, "y": 600}})
        assert d.session.tap_points == [(100.0, 600.0)] and KEYBOARD_NOTE not in r["text"]
        await finish(stop, task)
    run(go)


def test_force_does_not_switch_the_closing_off():
    async def go():
        d, stop, task = await keyboard_up()
        r = await d.run_tool("a", "tap", {"target": {"id": "save"}, "force": True})
        assert d.session.tap_points == [(60.0, 80.0), (60.0, 320.0)] and r["text"].endswith(KEYBOARD_NOTE)
        await finish(stop, task)
    run(go)


def test_an_element_in_an_os_alert_is_touched_without_closing():
    async def go():
        d, stop, task = await keyboard_up()
        d.session.tree = [*d.session.tree, button("e7", "Allow", "allow", rect=(10, 400, 100, 40), in_alert=True)]
        r = await d.run_tool("a", "tap", {"target": {"id": "allow"}})
        assert d.session.tap_points == [(60.0, 420.0)] and KEYBOARD_NOTE not in r["text"]
        await finish(stop, task)
    run(go)


def test_a_phone_asking_for_its_passcode_answers_locked_and_says_which_screen():
    """iOS's "Enable UI Automation" passcode screen: the agent's code is `locked` (ask the user), and the
    message and the live view's detail name the screen, not a lock."""
    from tapwright.device import PasscodeNeeded

    async def go():
        s = FakeSession()
        s.stuck = PasscodeNeeded()
        d = make(s)
        stop, task = await started(d, until=())
        await until_stuck(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "look", {"screenshot": False})
        assert e.value.code == "locked"
        assert str(e.value) == ("the phone is asking for its passcode to allow UI automation; ask the user to "
                                "enter it on the phone")
        assert "Enable UI Automation" in d.detail
        s.stuck = None  # the user typed it
        for _ in range(300):
            if d.state == "live":
                break
            await asyncio.sleep(0.01)
        assert d.stuck is None and d.state == "live"
        await finish(stop, task)
    run(go)


def test_a_call_after_the_passcode_was_typed_gets_the_phone_not_the_last_starts_answer():
    """Seen on the phone 2026-11-10: the passcode was in, nothing had been started since (the starts thin
    out), and a tap answered `locked` 20 s before the phone was back. The call's own start is what counts."""
    from tapwright.device import PasscodeNeeded

    async def go():
        s = FakeSession()
        hurried: list[int] = []
        s.device.hurry = lambda: hurried.append(1)
        s.stuck = PasscodeNeeded()
        d = make(s, backoff=(30.0,))  # nothing more is started by itself while this test runs
        stop, task = await started(d, until=())
        await until_stuck(d)
        tried = s.calls.count("ensure")
        with pytest.raises(Refused) as e:  # the screen is still up: one more start says so, then `locked`
            await d.run_tool("a", "look", {"screenshot": False})
        assert e.value.code == "locked" and s.calls.count("ensure") == tried + 1 and hurried == [1]
        s.stuck = None  # the user typed it; no start has noticed
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert r["text"].startswith("Screen ") and d.state == "live" and d.stuck is None
        await finish(stop, task)
    run(go)


# ---- scroll with to: "target" (AGENT-TOOLS v1.6) ----
class LongList:
    """A list of rows in a scroll view that fills the screen. A drag moves the rows by the finger's distance,
    between the list's two ends; the tree and the off-screen list follow, as the phone's would."""

    def __init__(self, d, rows: int = 40, pitch: float = 100.0, screen=(400.0, 800.0), ids: bool = True,
                 height: float = 40.0, labels=None) -> None:
        self.s, self.screen, self.pitch, self.rows, self.offset = d.session, screen, pitch, rows, 0.0
        self.ids, self.height, self.labels = ids, height, labels or (lambda i: f"Row {i}")
        self.lowest = -(rows * pitch - screen[1])
        real = self.s.scroll

        async def scroll(x0, y0, x1, y1):
            self.offset = min(0.0, max(self.lowest, self.offset + (y1 - y0)))
            self.lay()
            return await real(x0, y0, x1, y1)
        self.s.scroll = scroll
        self.lay()

    def lay(self) -> None:
        shown = [el("e1", "scroll", "", "list", rect=(0, 0, *self.screen))]
        off = []
        for i in range(self.rows):
            row = el("", "button", self.labels(i), f"row-{i}" if self.ids else "",
                     rect=(10, i * self.pitch + self.offset, 380, self.height))
            (shown if 0 <= row.center[1] < self.screen[1] else off).append(row)
        for n, e in enumerate(shown[1:], 2):
            e.ref = f"e{n}"
        self.s.tree, self.s.offscreen = shown, off


def test_scroll_to_a_text_goes_to_the_exact_match_off_screen_before_a_partial_one_on_screen():
    """Seen on the phone 2026-11-10: `{text: "Alert"}` found the state line on screen, which reads
    "… alert=none …", answered "already on screen", and the Alert button below was never reached."""
    async def go():
        d = make()
        stop, task = await started(d)
        LongList(d, ids=False, labels=lambda i: "state alert=none" if i == 1 else "Alert" if i == 30 else f"Row {i}")
        await d.run_tool("a", "look", {"screenshot": False})
        r = await d.run_tool("a", "scroll", {"target": {"text": "Alert"}, "to": "target"})
        first = r["text"].splitlines()[0]
        assert first.startswith("Scrolled ") and 'button "Alert"' in first and "already on screen" not in first
        await d.run_tool("a", "scroll", {"to": "start", "look": False})
        r = await d.run_tool("a", "scroll", {"target": {"text": "state"}, "to": "target"})  # only a partial match exists
        assert "is already on screen" in r["text"].splitlines()[0]
        await finish(stop, task)
    run(go)


def test_scroll_to_the_end_stops_as_soon_as_the_tree_shows_nothing_past_the_edge():
    """Over Wi-Fi waiting for the pictures to repeat took 4 to 14 s. Where the tree has the rows past the edge (React
    Native renders the whole list), the read after a drag says when the end is reached."""
    async def go():
        d = make(settle_still=0.03, settle_quiet=0.03)
        stop, task = await started(d)
        rows = LongList(d, rows=12)  # 12 rows of 100 pt in an 800 pt screen: two drags of 0.8 reach the end
        await d.run_tool("a", "look", {"screenshot": False})
        r = await d.run_tool("a", "scroll", {"to": "end", "look": False})
        assert r["text"].startswith("Scrolled") and rows.offset == rows.lowest
        assert len(d.session.scrolls) <= 2  # no extra drag to see the pictures repeat
        await finish(stop, task)
    run(go)


def test_rotate_turns_the_phone_and_checks_its_argument():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        turned = []
        d.session.rotate = lambda to: turned.append(to) or asyncio.sleep(0, f"rotated to {to.replace('_', ' ')}")
        r = await d.run_tool("a", "rotate", {"to": "landscape_left", "look": False})
        assert r["text"] == "rotated to landscape left" and turned == ["landscape_left"]
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "rotate", {"to": "sideways"})
        assert e.value.code == "bad_params"
        await finish(stop, task)
    run(go)


def test_scroll_to_the_start_of_a_list_already_there_answers_at_once():
    """Over Wi-Fi a scroll to the start of a list already at its start took 13 s (one drag, then waiting for the
    pictures to repeat). The tree shows rows past the bottom edge and none past the top: no drag at all."""
    async def go():
        d = make()
        stop, task = await started(d)
        LongList(d, rows=20)
        await d.run_tool("a", "look", {"screenshot": False})
        r = await d.run_tool("a", "scroll", {"to": "start", "look": False})
        assert r["text"].splitlines()[0] == "e1 scroll id=list @0,0 400x800 is already at its start."
        assert d.session.scrolls == []
        await finish(stop, task)
    run(go)


def test_scroll_to_the_end_doesnt_stop_while_the_last_row_straddles_the_edge():
    """Seen over Wi-Fi 2026-11-11: Bottom lay at y 915 to 963 on a 932 pt screen, its middle past the edge; the
    scroll to the end stopped there and the next tap on Bottom answered off_screen."""
    d = make()
    d.session.screen = (430.0, 932.0)
    straddling = button("", "Bottom", "lab-bottom", rect=(16, 915, 398, 48))  # its middle at 939, past 932
    d.session.offscreen = [straddling]
    assert d._more_beyond("down", (0.0, 0.0, 430.0, 932.0))
    d.session.offscreen = [button("", "Above", "a", rect=(16, -30, 398, 48))]  # its middle at -6, past the top
    assert d._more_beyond("up", (0.0, 0.0, 430.0, 932.0))
    assert not d._more_beyond("down", (0.0, 0.0, 430.0, 932.0))


def test_scroll_to_a_target_far_below_drags_until_it_is_clear_of_the_bars():
    """A first-time agent needed five calls to reach one button: off_screen, two scrolls by guess, covered under
    a bar, a third scroll. One call now, and the off_screen message says which."""
    async def go():
        d = make()
        stop, task = await started(d)
        LongList(d)
        await d.run_tool("a", "look", {"screenshot": False})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "row-30"}})
        assert str(e.value).endswith('is off screen; scroll to it first (scroll with this target and to: "target").')
        r = await d.run_tool("a", "scroll", {"target": {"id": "row-30"}, "to": "target"})
        first = r["text"].splitlines()[0]
        assert first.startswith("Scrolled e1 scroll id=list @0,0 400x800 to e") and 'button "Row 30" id=row-30' in first
        row = next(e for e in d.session.tree if e.identifier == "row-30")
        assert 60 <= row.y and row.y + row.height <= 760  # between the bars' insets, not only on the screen
        assert len(d.session.scrolls) == 4  # 2288 points at 588 a drag: three full drags and the rest
        assert all(y1 < y0 and y0 - y1 <= 700 * ui.SCROLL_USABLE + 0.01 for _, y0, _, y1 in d.session.scrolls)
        r = await d.run_tool("a", "tap", {"target": {"id": "row-30"}, "look": False})
        assert r["text"].startswith("Tapped e") and "row-30" in r["text"]
        await finish(stop, task)
    run(go)


def test_scroll_to_a_target_goes_up_as_well_and_does_nothing_when_it_is_already_clear():
    async def go():
        d = make()
        stop, task = await started(d)
        world = LongList(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "scroll", {"target": {"id": "row-30"}, "to": "target"})
        before = len(d.session.scrolls)
        r = await d.run_tool("a", "scroll", {"target": {"id": "row-30"}, "to": "target"})  # already there
        assert len(d.session.scrolls) == before and "No visible change." in r["text"]
        # nothing was dragged, so it doesn't say "Scrolled" (v1.6, at Tapline's ask)
        assert r["text"].splitlines()[0].endswith('button "Row 30" id=row-30 @10,712 380x40 is already on screen.')
        await d.run_tool("a", "scroll", {"target": {"id": "row-2"}, "to": "target"})  # back up
        row = next(e for e in d.session.tree if e.identifier == "row-2")
        assert 60 <= row.y and row.y + row.height <= 760
        assert all(y1 > y0 for _, y0, _, y1 in d.session.scrolls[before:])  # the finger went down
        assert world.offset <= 0
        await finish(stop, task)
    run(go)


def test_scroll_to_a_target_stops_at_the_end_of_the_list_and_says_so():
    """Row 0 sits at the top, under where a bar would be (above the 60-point inset): the list can't bring it lower."""
    async def go():
        d = make()
        stop, task = await started(d)
        LongList(d)
        await d.run_tool("a", "look", {"screenshot": False})
        r = await d.run_tool("a", "scroll", {"target": {"id": "row-0"}, "to": "target"})
        assert len(d.session.scrolls) == 1  # one try, the row didn't move, no more
        assert "Note: it doesn't scroll further: the target is as far into view as it gets." in r["text"]
        await finish(stop, task)
    run(go)


def test_scroll_to_a_target_needs_an_element_and_no_direction():
    async def go():
        d = make()
        stop, task = await started(d)
        LongList(d)
        await d.run_tool("a", "look", {"screenshot": False})
        for args in ({"to": "target"}, {"to": "target", "target": {"x": 5, "y": 5}},
                     {"to": "target", "target": {"id": "row-30"}, "direction": "down"}):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "scroll", args)
            assert e.value.code == "bad_params", args
        with pytest.raises(Refused) as e:  # a row the list hasn't made: it isn't anywhere in the tree
            await d.run_tool("a", "scroll", {"to": "target", "target": {"id": "row-99"}})
        assert e.value.code == "not_found" and d.session.scrolls == []
        await finish(stop, task)
    run(go)


# ---- changes off screen (AGENT-TOOLS v1.6) ----
def test_a_tap_whose_effect_is_off_screen_shows_it_instead_of_no_visible_change():
    """Tapline's second first-time agent spent 12 calls reading a state line after taps that answered "No visible
    change.": the effect was real, and scrolled out of view."""
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        d.hub.publish(b"A")
        s.offscreen = [el("", "text", "taps=1", "lab-state", rect=(16, 1400, 398, 60))]
        await d.run_tool("a", "look", {"screenshot": False})
        feeder = FrameFeeder(d.hub).start(then=b"A")  # nothing changes on the picture
        real = s.tap

        async def tap(*a, **k):
            s.offscreen = [el("", "text", "taps=2", "lab-state", rect=(16, 1400, 398, 60))]
            return await real(*a, **k)
        s.tap = tap
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert unsettled(r)[1:] == ["Same screen. Changes:",
                                    '~ text "taps=2" id=lab-state @16,1400 398x60 (off screen; was "taps=1")']
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}})  # the same state again: now nothing changed
        assert unsettled(r)[1:] == ["Same screen. Changes:", "No visible change."]
        await feeder.stop()
        await finish(stop, task)
    run(go)


def test_what_an_open_sheet_hides_off_screen_is_not_reported():
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        d.hub.publish(b"A")
        hidden = el("", "text", "taps=1", "lab-state", rect=(16, 1400, 398, 60))
        s.offscreen, s.behind = [hidden], [(hidden, None)]
        await d.run_tool("a", "look", {"screenshot": False})
        feeder = FrameFeeder(d.hub).start(then=b"A")
        changed = el("", "text", "taps=2", "lab-state", rect=(16, 1400, 398, 60))
        s.offscreen, s.behind = [changed], [(changed, None)]
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert unsettled(r)[1:] == ["Same screen. Changes:", "No visible change."]
        await feeder.stop()
        await finish(stop, task)
    run(go)


# ---- a forced touch says so (AGENT-TOOLS v1.6) ----
def test_a_forced_touch_past_a_check_says_where_it_may_have_landed():
    """With a real touch `force` doesn't make the hidden element act: the touch goes to its centre and lands on
    whatever is drawn there. The answer says so; a forced touch nothing was in the way of says nothing."""
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.screen = (430.0, 932.0)
        tabs = ui.Bar("TabBar", (0.0, 849.0, 430.0, 83.0), "tabbar @0,849 430x83")
        nav = ui.Bar("NavigationBar", (0.0, 59.0, 430.0, 54.0), "navigationbar @0,59 430x54")
        await looked(d, [el("e1", "button", "Low", "low", rect=(20, 860, 390, 60), covered_by=(tabs,)),
                         el("e2", "button", "High", "high", rect=(20, 62, 390, 44), covered_by=(nav,)),
                         el("e3", "switch", "Notify", "notify", rect=(20, 870, 390, 40), checked=False, covered_by=(tabs,)),
                         el("e4", "button", "Free", "free", rect=(20, 400, 390, 44))])
        r = await d.run_tool("a", "tap", {"target": {"id": "low"}, "force": True, "look": False})
        assert r["text"].endswith("Note: forced (under_tab_bar): the touch may have landed on what is in front.")
        r = await d.run_tool("a", "long_press", {"target": {"id": "high"}, "force": True, "look": False})
        assert r["text"].endswith("Note: forced (covered): the touch may have landed on what is in front.")
        r = await d.run_tool("a", "toggle", {"target": {"id": "notify"}, "force": True, "look": False})
        assert r["text"].endswith("Note: forced (under_tab_bar): the touch may have landed on what is in front.")
        r = await d.run_tool("a", "tap", {"target": {"id": "free"}, "force": True, "look": False})
        assert "Note:" not in r["text"]
        await finish(stop, task)
    run(go)


def test_off_screen_changes_follow_a_full_list_that_came_because_most_of_the_screen_changed():
    """Tapline's third first-time agent: a tap after a scroll made with look: false answered the full list, the
    state line's change (off screen) was dropped, and it needed two more calls to read it."""
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        s.tree = [button(f"e{i + 1}", f"Old {i}", f"old-{i}", rect=(10, 10 + 50 * i, 100, 40)) for i in range(6)]
        s.offscreen = [el("", "text", "taps=1", "lab-state", rect=(16, 1400, 398, 60))]
        await d.run_tool("a", "look", {"screenshot": False})
        s.tree = [button(f"e{i + 1}", f"New {i}", f"new-{i}", rect=(10, 10 + 50 * i, 100, 40)) for i in range(6)]
        s.offscreen = [el("", "text", "taps=2", "lab-state", rect=(16, 1400, 398, 60))]
        r = await d.run_tool("a", "tap", {"target": {"x": 5, "y": 5}})
        lines = unsettled(r)
        assert lines[1].startswith("Screen ") and lines[-1] == ('~ text "taps=2" id=lab-state @16,1400 398x60 '
                                                               '(off screen; was "taps=1")')
        assert lines[-2] == "Changes off screen:"  # a heading: bare ~ lines after a list read as a broken block
        # not when the full list was forced: a person acted, so the old off-screen list is no baseline
        s.offscreen = [el("", "text", "taps=3", "lab-state", rect=(16, 1400, 398, 60))]
        assert await d.viewer_message({"type": "gesture", "points": [[0, 0.5, 0.5], [60, 0.5, 0.5]]}) is None
        r = await d.run_tool("a", "tap", {"target": {"x": 5, "y": 5}})
        assert unsettled(r)[1].startswith("Screen ") and "off screen" not in r["text"]
        await finish(stop, task)
    run(go)


# ---- what a review of scroll-to-target and the off-screen lines found (2026-11-10) ----
def test_scroll_to_a_target_by_label_keeps_the_ref_its_first_line_gave():
    """The answer's first line named the row with one ref and the list under it gave every row without an
    identifier another: the list had been read after each drag, and the read after the last one counted as a move."""
    async def go():
        d = make()
        stop, task = await started(d)
        LongList(d, ids=False)
        await d.run_tool("a", "look", {"screenshot": False})
        r = await d.run_tool("a", "scroll", {"target": {"label": "Row 30"}, "to": "target"})
        lines = r["text"].splitlines()
        ref = re.search(r" to (e\d+) button \"Row 30\"", lines[0]).group(1)
        assert any(line.startswith(f'{ref} button "Row 30"') for line in lines[1:]), r["text"]
        r = await d.run_tool("a", "tap", {"target": {"ref": ref}, "look": False})
        assert r["text"].startswith(f'Tapped {ref} button "Row 30"')
        r = await d.run_tool("a", "scroll", {"target": {"label": "Row 30"}, "to": "target"})  # there already
        assert r["text"].splitlines()[0].startswith(f'{ref} button "Row 30"') and "No visible change." in r["text"]
        await finish(stop, task)
    run(go)


def test_scroll_to_a_target_that_several_elements_off_screen_match_is_ambiguous_and_takes_an_index():
    async def go():
        d = make()
        stop, task = await started(d)
        LongList(d, ids=False, labels=lambda i: "Delete" if i in (20, 30) else f"Row {i}")
        await d.run_tool("a", "look", {"screenshot": False})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "scroll", {"target": {"label": "Delete"}, "to": "target"})
        assert e.value.code == "ambiguous" and d.session.scrolls == []
        await d.run_tool("a", "scroll", {"target": {"label": "Delete", "index": 1}, "to": "target"})
        rows = [e for e in d.session.tree if e.label == "Delete"]
        assert len(rows) == 1 and 60 <= rows[0].y and rows[0].y + rows[0].height <= 760  # the second: row 30
        assert any(e.label == "Row 29" for e in d.session.tree)
        await finish(stop, task)
    run(go)


def test_scroll_to_a_target_nearly_as_tall_as_the_view_does_not_go_back_and_forth():
    """690 points in a view of 700: the last 5 points of margin can't be had with a drag of at least 20."""
    async def go():
        d = make()
        stop, task = await started(d)
        LongList(d, rows=12, pitch=800.0, height=690.0)
        await d.run_tool("a", "look", {"screenshot": False})
        r = await d.run_tool("a", "scroll", {"target": {"id": "row-5"}, "to": "target"})
        row = next(e for e in d.session.tree if e.identifier == "row-5")
        assert 60 <= row.y and row.y + row.height <= 760 and len(d.session.scrolls) < 12
        assert "Note:" not in r["text"] and r["text"].startswith("Scrolled ") and " to e" in r["text"].splitlines()[0]
        await finish(stop, task)
    run(go)

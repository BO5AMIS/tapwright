"""The daemon's phone side with a fake session: connecting, tools, control rules, viewer input."""
import asyncio
import base64

import pytest
from fakes import Clock, FakeDevice, FakeSession

from tapwright.control import Controller, Refused
from tapwright.daemon import Daemon
from tapwright.device import ConnectionLost


def make(session=None, **options) -> Daemon:
    clock = Clock()
    settings = dict(control=Controller(clock=clock, sleep=clock.sleep), log=lambda *a: None, heartbeat=0.02,
                    backoff=(0.01,), ready_wait=2.0, stream_linger=0, stream_retry=0.01)
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
        assert any(isinstance(c, tuple) and c[0] == "settings" and "mjpegFixOrientation" in c[1]
                   for c in d.session.calls)
        await finish(stop, task)
    run(go)


def test_look_then_act_by_ref():
    async def go():
        d = make()
        stop, task = await started(d)
        r = await d.run_tool("a", "look", {})
        assert r["texts"][0].startswith("screen 400x800") and r["image"] == base64.b64encode(b"JPEG").decode()
        r = await d.run_tool("a", "tap", {"ref": "e1", "look": False})
        assert r == {"texts": ['tapped e1 Button "OK" id=ok-button @10,10 100x40'], "image": None, "notes": []}
        r = await d.run_tool("a", "tap", {"ref": "e1", "look": False})  # its own taps don't make refs stale
        assert r["texts"][0].startswith("tapped e1")
        r = await d.run_tool("a", "tap", {"ref": "e1"})  # with the look after it
        assert len(r["texts"]) == 2 and r["texts"][1].startswith("screen") and r["image"]
        await finish(stop, task)
    run(go)


def test_a_person_makes_refs_stale_and_ids_are_looked_up_again():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {})
        assert await d.viewer_message({"type": "gesture", "points": [[0, 0.5, 0.5], [60, 0.5, 0.5]]}) is None
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"ref": "e1", "look": False})
        assert e.value.code == "stale"
        looks = d.session.calls.count("look")
        r = await d.run_tool("a", "tap", {"id": "ok-button", "look": False})
        assert r["texts"][0].startswith("tapped e1") and d.session.calls.count("look") == looks + 1
        assert r["notes"] == ["a person used the phone since your last look"]
        assert (await d.run_tool("a", "tap", {"ref": "e1", "look": False}))["notes"] == []
        await finish(stop, task)
    run(go)


def test_pause_refuses_agents():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.viewer_message({"type": "pause", "on": True})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"x": 5, "y": 5, "look": False})
        assert e.value.code == "paused" and d.status()["paused"] is True
        await d.viewer_message({"type": "pause", "on": False})
        assert (await d.run_tool("a", "tap", {"x": 5, "y": 5, "look": False}))["texts"] == ["tapped (5, 5)"]
        await finish(stop, task)
    run(go)


def test_a_dropped_action_is_reported_not_retried():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.fail["tap"] = ConnectionLost("reset by peer")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"x": 5, "y": 5, "look": False})
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
        assert r["texts"][0].startswith("screen") and d.session.calls.count("look") == 2
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
        assert e.value.code == "reconnecting"
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
            await d.run_tool("a", "tap", {"x": 1, "y": 1})
        assert e.value.code == "locked"
        assert (await d.run_tool("a", "look", {"screenshot": False}))["texts"][0].startswith("screen")
        assert (await d.viewer_message({"type": "button", "name": "home"}))["code"] == "locked"
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
        assert (await d.run_tool("a", "list_apps", {}))["texts"] == ["com.example.app  Example 1.0 (7)"]
        assert (await d.run_tool("a", "terminate_app", {"bundle_id": "x"}))["texts"] == ["closed x"]
        assert (await d.run_tool("a", "alert", {"action": "text"}))["texts"] == ["alert texted"]
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "format_phone", {})
        assert e.value.code == "unsupported"
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
        assert r["texts"][0].startswith("screen")
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
        r = await d.run_tool("a", "tap", {"x": 5, "y": 5, "look": False})
        assert r["notes"] == ["a person used the phone since your last look"]
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"ref": "e1", "look": False})
        assert e.value.code == "stale"
        await finish(stop, task)
    run(go)


def test_busy_while_a_person_drives():
    async def go():
        d = make()
        stop, task = await started(d)
        d.control.person_active = lambda: True
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"x": 5, "y": 5, "look": False})
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
            await d.run_tool("a", "launch_app", {})
        assert e.value.code == "unsupported" and "bundle_id" in str(e.value)
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
        assert e.value.code == "reconnecting" and asyncio.get_running_loop().time() - t0 < 0.5
        await finish(stop, task)
    run(go)

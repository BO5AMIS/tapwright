"""The daemon's phone side with a fake session: connecting, tools, control rules, viewer input."""
import asyncio
import base64

import pytest
from fakes import Clock, FakeDevice, FakeSession

from tapwright.control import Controller, Refused
from tapwright.daemon import Daemon
from tapwright.device import ConnectionLost, DeviceError
from tapwright.stream import PRESETS


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
        assert any(isinstance(c, tuple) and c[0] == "settings" and c[1] == PRESETS["balanced"]
                   for c in d.session.calls)  # every setting, so a reused runner can't keep old ones
        await finish(stop, task)
    run(go)


def test_look_then_act_by_ref():
    async def go():
        d = make()
        stop, task = await started(d)
        r = await d.run_tool("a", "look", {})
        assert r["text"].startswith("Screen 400x800") and r["image"] == base64.b64encode(b"JPEG").decode()
        assert r["image_size"] is None  # b"JPEG" isn't a picture
        r = await d.run_tool("a", "tap", {"ref": "e1", "look": False})
        assert r == {"text": 'tapped e1 button "OK" id=ok-button @10,10 100x40 [tap]', "image": None, "image_size": None}
        r = await d.run_tool("a", "tap", {"ref": "e1", "look": False})  # its own taps don't make refs stale
        assert r["text"].startswith("tapped e1")
        r = await d.run_tool("a", "tap", {"ref": "e1"})  # with the look after it
        assert r["text"].splitlines()[1].startswith("Screen") and r["image"]
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
        assert r["text"].startswith("tapped e1") and d.session.calls.count("look") == looks + 1
        assert r["text"].endswith("\nNote: a person used the phone since your last look.")
        assert "Note:" not in (await d.run_tool("a", "tap", {"ref": "e1", "look": False}))["text"]
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
        assert (await d.run_tool("a", "tap", {"x": 5, "y": 5, "look": False}))["text"] == "tapped (5, 5)"
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
            await d.run_tool("a", "tap", {"x": 1, "y": 1})
        assert e.value.code == "locked"
        assert (await d.run_tool("a", "look", {"screenshot": False}))["text"].startswith("Screen")
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
        assert (await d.run_tool("a", "list_apps", {}))["text"] == "com.example.app  Example 1.0 (7)"
        assert (await d.run_tool("a", "terminate_app", {"bundle_id": "x"}))["text"] == "closed x"
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
        r = await d.run_tool("a", "tap", {"x": 5, "y": 5, "look": False})
        assert r["text"] == "tapped (5, 5)\nNote: a person used the phone since your last look."
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
        assert e.value.code == "bad_params" and "bundle_id" in str(e.value)
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
            await d.run_tool("a", "tap", {"ref": "e9", "look": False})
        assert e.value.code == "not_found" and "e9" in str(e.value)
        assert d.control.generation == generation  # nothing happened on the phone
        assert (await d.run_tool("a", "tap", {"ref": "e1", "look": False}))["text"].startswith("tapped e1")
        await finish(stop, task)
    run(go)


def test_bad_arguments_are_bad_params():
    async def go():
        d = make()
        stop, task = await started(d)
        generation = d.control.generation
        for tool, args, field in (("tap", {}, "ref"), ("tap", {"x": 1, "y": 1, "index": "abc"}, "index"),
                                  ("swipe", {"x0": "abc", "y0": 0, "x1": 1, "y1": 1}, "x0"),
                                  ("swipe", {"x0": 0, "y0": 0, "x1": 1, "y1": float("nan")}, "y1"),
                                  ("wait", {"seconds": "soon"}, "seconds"),
                                  ("long_press", {"x": 1, "y": 1, "seconds": "long"}, "seconds"),
                                  ("scroll", {"direction": "sideways"}, "direction"),
                                  ("scroll", {"amount": "lots"}, "amount")):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", tool, args)
            assert e.value.code == "bad_params" and field in str(e.value), (tool, args)
        assert d.control.generation == generation and d.session.actions() == ["tap"]  # only the target-less tap got through, and failed in ui.find
        await finish(stop, task)
    run(go)


def test_a_crash_inside_a_tool_is_internal_and_logged():
    async def go():
        logs = []
        d = make(log=logs.append)
        stop, task = await started(d)
        d.session.fail["tap"] = RuntimeError("boom")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"x": 5, "y": 5, "look": False})
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
            await d.run_tool("a", "tap", {"x": 5, "y": 5, "look": False})
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
            await d.run_tool("a", "tap", {"x": 5, "y": 5, "look": False})
        assert e.value.code == "internal" and "the phone side failed" in str(e.value)
        assert "HTTP 500" in str(e.value) and "bug" not in str(e.value)
        assert not any("Traceback" in m for m in logs)
        await finish(stop, task)
    run(go)


def test_a_look_that_fails_after_an_action_is_a_note():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.fail["look"] = RuntimeError("boom")
        r = await d.run_tool("a", "tap", {"x": 5, "y": 5})
        assert r["text"] == "tapped (5, 5)\nNote: couldn't look afterwards: boom."
        await finish(stop, task)
    run(go)


def test_a_real_picture_comes_with_its_size():
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (30, 20)).save(buf, "JPEG")

    class Pictured(FakeSession):
        async def look(self, screenshot=True):
            text, _ = await super().look(screenshot)
            return text, buf.getvalue()

    async def go():
        d = make(Pictured())
        stop, task = await started(d)
        r = await d.run_tool("a", "look", {})
        assert r["image_size"] == [30, 20] and base64.b64decode(r["image"]) == buf.getvalue()
        await finish(stop, task)
    run(go)

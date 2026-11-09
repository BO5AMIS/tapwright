"""The daemon's phone side with a fake session: connecting, tools, control rules, viewer input."""
import asyncio
import base64
import hashlib
import re

import pytest
from fakes import Clock, FakeDevice, FakeSession, FrameFeeder, button

from tapwright import daemon as daemon_module
from tapwright.control import Controller, Refused
from tapwright.daemon import Daemon
from tapwright.device import ConnectionLost, DeviceError
from tapwright.stream import PRESETS

SETTLED = re.compile(r"Settled in \d+\.\d\d s\.")


def unsettled(answer: dict) -> list[str]:
    """The answer's lines without its `Settled in` line, which varies with timing."""
    return [line for line in answer["text"].splitlines() if not SETTLED.fullmatch(line)]


def make(session=None, **options) -> Daemon:
    clock = Clock()
    settings = dict(control=Controller(clock=clock, sleep=clock.sleep), log=lambda *a: None, heartbeat=0.02,
                    backoff=(0.01,), ready_wait=2.0, stream_linger=0, stream_retry=0.01,
                    settle_still=0.03, settle_first_frame=0.01, settle_fallback=0.0)
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
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False})
        assert r == {"text": 'Tapped e1 button "OK" id=ok-button @10,10 100x40.', "image": None, "image_size": None}
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False})  # its own taps don't make refs stale
        assert r["text"].startswith("Tapped e1")
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}})  # with what changed after it
        assert unsettled(r)[1:] == ["Same screen. Changes:", "No visible change."] and r["image"] is None
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
        assert e.value.code == "ambiguous" and str(e.value).endswith('e2 button "7" id=seven-again @10,60 100x40 [tap]')
        assert "tap" not in d.session.calls
        r = await d.run_tool("a", "tap", {"target": {"label": "7", "index": 1}, "look": False})
        assert r["text"].startswith('Tapped e2 button "7" id=seven-again')
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"label": "7", "index": 5}, "look": False})
        assert e.value.code == "not_found"
        r = await d.run_tool("a", "tap", {"target": {"text": "8"}, "look": False})  # text, exact: the one "8"
        assert r["text"].startswith('Tapped e7 button "8"')  # the re-read after a no-look action: id-less refs are new
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
        await d.run_tool("a", "look", {"screenshot": False})
        assert (await d.run_tool("a", "type_text", {"text": "hi", "look": False}))["text"] == "typed 2 characters"
        r = await d.run_tool("a", "type_text", {"text": "hi", "target": {"id": "ok-button"}, "look": False})
        assert r["text"] == "typed 2 characters"
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
        assert e.value.code == "not_found" and str(e.value).startswith("e1 is no longer on the screen; look again")
        await finish(stop, task)
    run(go)


def test_a_point_for_type_text_is_a_lookup_on_a_current_list():
    async def go():
        d = make()
        stop, task = await started(d)
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
        assert e.value.code == "ambiguous" and 'e2 button "7"' in str(e.value) and "use one of their refs" in str(e.value)
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
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert unsettled(r)[1:] == ["Same screen. Changes:", '- button "Bluetooth"']
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
        assert view.items == [] and view.top_ref == 0
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
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert unsettled(r)[1:] == ["Same screen. Changes:", 'Alert: "Allow Notifications?" [Allow] [Don\'t Allow]']
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert unsettled(r)[1:] == ["Same screen. Changes:", "No visible change."]  # the same alert: no news
        d.session.last_alert = _alert("Allow Location?", ("OK",))
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert unsettled(r)[2] == 'Alert: "Allow Location?" [OK]'
        d.session.last_alert = None
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert unsettled(r)[1:] == ["Same screen. Changes:", "Alert: gone"]
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
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert unsettled(r)[1:] == ["Same screen. Changes:", "No visible change."]
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
        assert [x.ref for x in view.items if x.label == "Edit"] == ["e4", "e5", "e6"]  # renumbered, and shown
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
        assert d._mark() == (1, hashlib.sha1(b"A").digest())
    run(go)


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
        assert changed and 0.04 <= seconds < 0.5
    run(go)


def test_an_unchanged_screen_is_answered_without_reading_the_tree():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        d.hub.publish(b"A")
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
    assert daemon_module.ACTIONS["launch_app"][1] == daemon_module.LONG_CAP
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

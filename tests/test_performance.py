"""`performance` (PLAN-1.3 item 3, AGENT-TOOLS §15.3): the sums, the answer, the launch time, the safety rules, and
`expect: {"under": ...}` in `run` and scripts. The phone is a fake: its meter gives the Measurement a test sets."""
import asyncio
import json

import pytest
from fakes import FakeSession, button
from test_daemon import finish, make, started

from tapwright import device as device_module
from tapwright import performance
from tapwright.control import Refused
from tapwright.daemon import Scope
from tapwright.performance import Measurement

# the plan's example: memory 182 (peak 190), CPU 34 (peak 88), 57 fps (lowest 41), 3 seconds under 45 fps
EXAMPLE = Measurement(memory_mb=[174.0, 190.0, 182.0], cpu_pct=[10.0, 88.0, 4.0], fps=[41.0, 43.0, 44.0, 100.0])


def run(go):
    asyncio.run(go())


def test_the_sums_are_averages_peaks_and_seconds_under_45_fps():
    r = performance.summarize(EXAMPLE, 3.0)
    assert (r.memory_avg, r.memory_peak, r.cpu_avg, r.cpu_peak) == (182.0, 190.0, 34.0, 88.0)
    assert (r.fps_avg, r.fps_min, r.drops) == (57.0, 41.0, 3)
    assert performance.answer_lines(r) == [
        "Measured 3.0 s.",
        "Memory 182 MB (peak 190). CPU 34% (peak 88%). Frames 57 fps (lowest 41); 3 seconds under 45 fps."]
    one = performance.summarize(Measurement(fps=[44.0, 60.0]), 1.0)
    assert "1 second under 45 fps" in performance.answer_lines(one)[1]


def test_the_launch_line_and_what_could_not_be_measured():
    r = performance.summarize(Measurement(memory_mb=[100.0]), 1.4, opened_s=1.42)
    assert performance.answer_lines(r) == ["Opened in 1.42 s (to the first settled screen).",
                                           "Memory 100 MB (peak 100).", "Not measured here: CPU, frames."]
    r.unsettled = True
    assert performance.answer_lines(r)[0] == "Opened in 1.42 s (the screen was still changing when the wait ended)."
    nothing = performance.summarize(Measurement(), 3.0)
    assert performance.answer_lines(nothing) == ["Measured 3.0 s.", "Nothing could be measured.",
                                                 "Not measured here: memory, CPU, frames."]


def test_a_still_screen_is_not_a_slow_one():
    r = performance.summarize(Measurement(memory_mb=[50.0], cpu_pct=[1.0], fps=[0.0, 0.0]), 2.0)
    lines = performance.answer_lines(r)
    assert lines[1].endswith("Frames: none drawn (the screen didn't change).") and len(lines) == 2
    mixed = performance.summarize(Measurement(fps=[0.0, 60.0, 30.0]), 3.0)  # the 0 is left out of every number
    assert (mixed.fps_avg, mixed.fps_min, mixed.drops) == (45.0, 30.0, 1)


def test_limits_fail_with_every_reason_and_an_unmeasured_number_fails_too():
    r = performance.summarize(Measurement(memory_mb=[190.0], cpu_pct=[50.0], fps=[40.0, 50.0, 30.0, 20.0, 60.0]),
                              3.0, opened_s=2.6)
    assert performance.failures(r, {"launch_s": 2.0, "drops": 0, "memory_mb": 300, "cpu_percent": 60}) == [
        "opened in 2.6 s, over 2.0 s", "3 drops, over 0"]
    assert performance.failures(r, {"memory_mb": 150, "cpu_percent": 40, "fps_min": 25}) == [
        "memory peaked at 190 MB, over 150 MB", "CPU averaged 50%, over 40%", "frames dipped to 20 fps, under 25 fps"]
    assert performance.failures(r, {"launch_s": 2.6, "memory_mb": 190, "cpu_percent": 50, "fps_min": 20, "drops": 3}) == []
    bare = performance.summarize(Measurement(), 1.0)
    assert performance.failures(bare, {"launch_s": 2.0, "memory_mb": 1}) == [
        "the launch (the step has no launch: true) wasn't measured, so it can't be checked",
        "memory wasn't measured, so it can't be checked"]


def test_a_limit_is_not_passed_on_a_partial_window_an_unsettled_launch_or_a_number_that_is_not_one():
    """CodeRabbit, PR #4: partial sampling, a launch whose wait hit its cap, and NaN samples must not pass a check."""
    partial = performance.summarize(Measurement(memory_mb=[100.0], cpu_pct=[10.0], fps=[60.0], failed="service stopped"),
                                    2.0, opened_s=1.0)
    assert partial.partial
    assert performance.failures(partial, {"memory_mb": 300, "cpu_percent": 50, "fps_min": 30, "drops": 5}) == [
        "memory: only part of the window was measured, so it can't be checked",
        "CPU: only part of the window was measured, so it can't be checked",
        "frames: only part of the window was measured, so it can't be checked",
        "frames: only part of the window was measured, so it can't be checked"]
    assert performance.failures(partial, {"launch_s": 2.0}) == []  # the launch time comes from the wait, not the samples
    over = performance.summarize(Measurement(memory_mb=[400.0], failed="x"), 2.0)
    assert performance.failures(over, {"memory_mb": 300}) == ["memory peaked at 400 MB, over 300 MB"]  # already over: said so
    capped = performance.summarize(Measurement(), 8.0, opened_s=8.0, unsettled=True)
    assert performance.failures(capped, {"launch_s": 10.0}) == [
        "the screen was still changing when the launch wait ended, so the launch time can't be checked"]
    assert performance.failures(capped, {"launch_s": 2.0}) == ["opened in 8.0 s, over 2.0 s"]
    nan = float("nan")
    r = performance.summarize(Measurement(memory_mb=[nan, 120.0], cpu_pct=[nan, float("inf")], fps=[nan, 60.0]), 1.0)
    assert (r.memory_avg, r.memory_peak, r.cpu_avg, r.cpu_peak, r.fps_avg) == (120.0, 120.0, None, None, 60.0)
    assert performance.failures(r, {"cpu_percent": 50}) == ["CPU wasn't measured, so it can't be checked"]
    assert "CPU" in performance.answer_lines(r)[-1]  # listed under what was not measured


def test_under_is_checked_before_anything_runs():
    from tapwright import batch
    ok = [{"tool": "performance", "launch": True, "expect": {"under": {"launch_s": 2.0, "js_heap_mb": 50}}}]
    assert batch.check_steps(ok) == ok
    for step, words in (
            ({"tool": "performance", "expect": {"under": {"bogus": 1}}}, "under has no 'bogus'"),
            ({"tool": "performance", "expect": {"under": {"drops": "0"}}}, "under's drops must be a number"),
            ({"tool": "performance", "expect": {"under": {"drops": True}}}, "under's drops must be a number"),
            ({"tool": "performance", "expect": {"under": {"memory_mb": -1}}}, "of 0 or more"),
            ({"tool": "performance", "expect": {"under": {}}}, "under takes launch_s"),
            ({"tool": "performance", "expect": {"under": [1]}}, "under takes launch_s"),
            ({"tool": "performance", "expect": {"text": "Saved"}}, "goes with a performance step"),
            ({"tool": "performance", "expect": {"under": {"drops": 0}, "text": "Saved"}}, "goes with a performance step"),
            ({"tool": "tap", "target": {"id": "x"}, "expect": {"under": {"drops": 0}}}, "goes with a performance step")):
        with pytest.raises(Refused) as e:
            batch.check_steps([step])
        assert e.value.code == "bad_params" and words in str(e.value), (step, str(e.value))
    with pytest.raises(Refused):  # a script can't aim a tap by ref
        batch.check_steps([{"tool": "performance", "tap": {"ref": "e1"}}], script=True)
    assert batch.summary({"tool": "performance", "launch": True}) == "performance launch"


def test_seconds_measures_the_app_in_front_and_samples_only_between_start_and_stop():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.device.measurement = EXAMPLE
        r = await d.run_tool("a", "performance", {"seconds": 3})
        assert r["text"].splitlines() == [
            "Measured 3.0 s.",
            "Memory 182 MB (peak 190). CPU 34% (peak 88%). Frames 57 fps (lowest 41); 3 seconds under 45 fps."]
        assert r["image"] is None
        assert d.session.device.metered == [("start", "com.example.app"), ("stop", "com.example.app")]
        await d.run_tool("a", "performance", {})  # the default is 3 s
        assert d.session.actions().count("look") == 0  # it reads no screen
        await finish(stop, task)
    run(go)


def test_the_apps_own_process_is_the_only_one_sampled():
    """AppMeter keeps rows whose pid is a process of the app's bundle id, and finds the new process after a launch."""
    class Info:
        def __init__(self):
            self.lists = [[], [{"pid": 7, "bundleIdentifier": "com.example.app"},
                              {"pid": 8, "bundleIdentifier": "com.example.other"}]]

        async def proclist(self):
            return self.lists.pop(0) if len(self.lists) > 1 else self.lists[0]

    class Tap:
        def __init__(self, samples):
            self.samples = samples

        async def iter_processes(self):
            for rows in self.samples:
                yield rows

    mb = device_module.MB
    samples = [[{"pid": 8, "physFootprint": 900 * mb, "cpuUsage": 99.0}],  # before the app runs: only another process
               [{"pid": 8, "physFootprint": 900 * mb, "cpuUsage": 99.0},
                {"pid": 7, "physFootprint": 120 * mb, "cpuUsage": 12.5},
                {"pid": 1, "physFootprint": 5 * mb, "cpuUsage": 80.0}],
               [{"pid": 7, "physFootprint": 140 * mb, "cpuUsage": None}, {"pid": 8, "physFootprint": 901 * mb}]]

    async def go():
        device_module.PID_RETRY = 0
        meter = device_module.AppMeter(FakeSession().device, "com.example.app")
        worker = asyncio.create_task(meter._processes(Info()))
        await meter._rows_in(Tap(samples))
        await asyncio.sleep(0.05)  # the batches are taken one by one
        worker.cancel()
        assert meter.measurement.memory_mb == [120.0, 140.0]
        assert meter.measurement.cpu_pct == [12.5]
    saved = device_module.PID_RETRY
    try:
        run(go)
    finally:
        device_module.PID_RETRY = saved


def test_launch_closes_the_app_then_times_its_open_to_the_settled_screen():
    async def go():
        s = FakeSession()
        d = make(s)
        stop, task = await started(d)
        s.device.measurement = Measurement(memory_mb=[150.0], cpu_pct=[70.0])
        real, seen = s.launch, []

        async def slow(bundle_id):
            seen.append(list(s.device.metered))  # the meter was running when the app was asked to open
            d.control.clock.t += 1.42
            return await real(bundle_id)
        s.launch = slow
        r = await d.run_tool("a", "performance", {"launch": True})
        assert r["text"].splitlines() == ["Opened in 1.42 s (to the first settled screen).",
                                          "Memory 150 MB (peak 150). CPU 70% (peak 70%).", "Not measured here: frames."]
        assert s.closed == ["com.example.app"] and s.launched == ["com.example.app"]
        assert s.calls.index("terminate") < s.calls.index("launch")
        assert seen == [[("start", "com.example.app")]]
        assert s.device.metered[-1] == ("stop", "com.example.app")
        await finish(stop, task)
    run(go)


def test_the_measured_window_begins_after_the_close_and_before_the_open():
    """Codex P2, PR #4: samples from the set-up and from the app that was running before the launch were kept."""
    async def go():
        s = FakeSession()
        d = make(s)
        stop, task = await started(d)
        s.device.measurement = Measurement(memory_mb=[100.0])
        order = []
        s.device.on_window = lambda fresh: order.append(("window", fresh, list(s.closed), list(s.launched)))
        await d.run_tool("a", "performance", {"launch": True})
        assert order == [("window", True, ["com.example.app"], [])]  # closed already, not yet opened
        order.clear()
        await d.run_tool("a", "performance", {"seconds": 1})
        assert order == [("window", False, ["com.example.app"], ["com.example.app"])]  # (no close or open of its own)
        await finish(stop, task)
    run(go)


def test_a_tap_goes_through_the_ordinary_tool_and_so_through_the_policy():
    async def go():
        s = FakeSession()
        s.tree = [button("e1", "OK", "ok-button"), button("e2", "Delete trip", "delete", rect=(10, 200, 100, 40))]
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        s.device.measurement = EXAMPLE
        r = await d.run_tool("a", "performance", {"seconds": 1, "tap": {"id": "ok-button"}})
        assert r["text"].startswith("Measured 1.0 s.") and s.calls.count("tap") == 1
        with pytest.raises(Refused) as e:  # Delete needs a person's OK, as it does for `tap` itself
            await d.run_tool("a", "performance", {"seconds": 1, "tap": {"id": "delete"}})
        assert e.value.code == "not_allowed" and "needs_confirmation" in str(e.value)
        assert s.calls.count("tap") == 1
        assert s.device.metered[-2:] == [("start", "com.example.app"), ("stop", "com.example.app")]  # sampling ended
        await finish(stop, task)
    run(go)


def test_personal_mode_measures_only_an_app_it_was_given():
    async def go():
        s = FakeSession()
        d = make(s)
        stop, task = await started(d)
        mine = Scope(("com.example.app",))
        s.device.measurement = EXAMPLE
        s.foreground = "com.example.mail"  # another app is in front
        s.app = ("Mail box", "com.example.mail")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "performance", {"seconds": 1}, scope=mine)
        assert e.value.code == "not_allowed" and str(e.value).startswith("outside_apps: ")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "performance", {"launch": True}, scope=mine)
        assert e.value.code == "not_allowed" and s.closed == [] and s.launched == []
        with pytest.raises(Refused) as e:  # no app list at all
            await d.run_tool("a", "performance", {"seconds": 1}, scope=Scope(()))
        assert e.value.code == "not_allowed"
        assert s.device.metered == []  # nothing was sampled
        s.foreground, s.app = "com.example.app", ("Example", "com.example.app")
        r = await d.run_tool("a", "performance", {"seconds": 1}, scope=mine)
        assert r["text"].startswith("Measured 1.0 s.")
        await finish(stop, task)
    run(go)


def test_no_app_in_front_and_a_meter_that_cannot_start_are_told_plainly():
    async def go():
        s = FakeSession()
        d = make(s)
        stop, task = await started(d)
        s.app = None
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "performance", {"seconds": 1})
        assert e.value.code == "not_found" and "no app is in front" in str(e.value)
        s.app = ("Example", "com.example.app")
        s.device.meter_error = device_module.DeviceError("no instruments")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "performance", {"seconds": 1})
        assert e.value.code == "unsupported" and "no instruments" in str(e.value)
        with pytest.raises(Refused) as e:  # a launch that can't be measured leaves the app open (review 2026-11-12)
            await d.run_tool("a", "performance", {"launch": True})
        assert e.value.code == "unsupported" and s.closed == []
        await finish(stop, task)
    run(go)


def test_bad_parameters_are_refused_before_the_phone_is_touched():
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        for args, words in (({"seconds": 0.1}, "seconds must be from 0.5 to 30"), ({"seconds": 31}, "seconds must be from"),
                            ({"seconds": "soon"}, "seconds must be a number"), ({"launch": "yes"}, "launch must be true or false"),
                            ({"launch": True, "seconds": 2}, "leave seconds and tap out"),
                            ({"launch": True, "tap": {"id": "x"}}, "leave seconds and tap out"),
                            ({"tap": {"ref": "e1", "id": "x"}}, "exactly one")):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "performance", args)
            assert e.value.code == "bad_params" and words in str(e.value), (args, str(e.value))
        assert s.actions() == [] and s.device.metered == []
        await finish(stop, task)
    run(go)


def test_a_run_step_passes_or_fails_its_limits_and_says_every_reason():
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        s.device.measurement = Measurement(memory_mb=[190.0], cpu_pct=[20.0], fps=[40.0, 30.0, 60.0, 60.0, 60.0])
        ok = {"tool": "performance", "seconds": 1, "expect": {"under": {"memory_mb": 300, "cpu_percent": 50, "drops": 2,
                                                                         "fps_min": 30, "js_heap_mb": 10}}}
        r = await d.run_tool("a", "run", {"steps": [ok], "look": False})
        assert r["text"].splitlines()[0].startswith("Ran 1 of 1 steps")
        assert "Note (step 1): js_heap_mb isn't measured by Tapwright; not checked." in r["text"]
        bad = {"tool": "performance", "launch": True, "expect": {"under": {"launch_s": 2.0, "drops": 0, "busy_ms": 5}}}
        s.device.metered.clear()
        real = s.launch

        async def slow(bundle_id):
            d.control.clock.t += 2.6
            return await real(bundle_id)
        s.launch = slow
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": [ok, bad, {"tool": "performance", "seconds": 1}], "look": False})
        assert e.value.code == "expect_failed"
        assert str(e.value).splitlines()[0] == ("Ran 1 of 3 steps; stopped at step 2 (performance launch): "
                                                "opened in 2.6 s, over 2.0 s; 2 drops, over 0.")
        assert "Note (step 2): busy_ms isn't measured by Tapwright; not checked." in str(e.value)
        assert s.device.metered.count(("start", "com.example.app")) == 2  # steps 1 and 2; step 3 never ran
        await finish(stop, task)
    run(go)


def test_a_bad_under_stops_a_run_before_any_step_runs():
    async def go():
        d = make()
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": [{"tool": "tap", "target": {"label": "OK"}},
                                                    {"tool": "performance", "expect": {"under": {"fps": 1}}}]})
        assert e.value.code == "bad_params" and "step 2" in str(e.value)
        assert d.session.actions() == []
        await finish(stop, task)
    run(go)


def test_record_saves_a_performance_call_as_one_step_without_under_and_without_its_tap(tmp_path):
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.tree = [button("e1", "OK", "ok-button")]
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "record", {"start": True, "name": "perf"})
        d.session.device.measurement = EXAMPLE
        await d.run_tool("a", "performance", {"seconds": 2, "tap": {"ref": "e1"}})
        await d.run_tool("a", "performance", {"launch": True})
        path = tmp_path / "perf.json"
        await d.run_tool("a", "record", {"stop": True, "path": str(path)})
        steps = json.loads(path.read_text("utf-8"))["steps"]
        assert steps == [{"tool": "performance", "seconds": 2.0, "tap": {"id": "ok-button"}},
                         {"tool": "performance", "launch": True}]  # the tap, close and open inside are not steps of their own
        await finish(stop, task)
    run(go)


def test_the_action_log_has_the_numbers_and_nothing_personal(tmp_path):
    from tapwright.actionlog import ActionLog

    async def go():
        d = make(actions=ActionLog(tmp_path))
        stop, task = await started(d)
        d.session.device.measurement = EXAMPLE
        await d.run_tool("a", "performance", {"seconds": 1})
        await finish(stop, task)
        entries = [json.loads(line) for f in tmp_path.glob("actions-*.jsonl") for line in f.read_text("utf-8").splitlines()]
        got = [e for e in entries if e["tool"] == "performance"]
        assert len(got) == 1 and got[0]["ok"] is True
        assert got[0]["measured"]["memory_peak"] == 190.0 and got[0]["measured"]["drops"] == 3
    run(go)


def test_a_still_screen_of_a_partial_window_fails_its_frame_limits_and_a_fully_sampled_one_passes():
    """CodeRabbit and Codex, PR #4 cycle 2: fps_min and drops took the still-screen shortcut before the partial check."""
    stopped = performance.summarize(Measurement(fps=[0.0], failed="service stopped"), 2.0)
    assert stopped.still and stopped.partial
    assert performance.failures(stopped, {"fps_min": 30, "drops": 0}) == [
        "frames: only part of the window was measured, so it can't be checked"] * 2
    whole = performance.summarize(Measurement(fps=[0.0, 0.0]), 2.0)
    assert whole.still and not whole.partial and performance.failures(whole, {"fps_min": 30, "drops": 0}) == []


def test_samples_lost_to_a_process_list_that_could_not_be_read_fail_memory_and_cpu_limits_only():
    """Codex, PR #4 cycle 2: batches the phone couldn't say whose they were were skipped without a trace, and the one
    sample left passed memory_mb."""
    r = performance.summarize(Measurement(memory_mb=[100.0], cpu_pct=[10.0], fps=[60.0], lost=3), 2.0)
    assert r.samples_lost and not r.partial
    assert performance.failures(r, {"memory_mb": 300, "cpu_percent": 50, "fps_min": 30, "drops": 0}) == [
        "memory: only part of the window was measured, so it can't be checked",
        "CPU: only part of the window was measured, so it can't be checked"]
    over = performance.summarize(Measurement(memory_mb=[400.0], lost=1), 2.0)
    assert performance.failures(over, {"memory_mb": 300}) == ["memory peaked at 400 MB, over 300 MB"]
    assert not performance.summarize(Measurement(memory_mb=[1.0]), 1.0).samples_lost


def test_lost_samples_are_said_in_the_answer():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.device.measurement = Measurement(memory_mb=[100.0], cpu_pct=[5.0], lost=2)
        r = await d.run_tool("a", "performance", {"seconds": 1})
        assert "Note: " + performance.SAMPLES_LOST.format(n=2) in r["text"].splitlines()
        await finish(stop, task)
    run(go)

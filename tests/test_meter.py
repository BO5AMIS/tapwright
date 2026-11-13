"""device.AppMeter, the real orchestration (start, window, sampling, stop) over fake instruments: the phone's process
list, sysmontap and graphics are scripted by a World. Codex and CodeRabbit, PR #4."""
import asyncio
import datetime
import sys
import time
import types

import pytest

from tapwright import device as device_module

APP = "com.example.app"
MB = device_module.MB


def run(go):
    asyncio.run(go())


async def settle(check, tries=300):
    for _ in range(tries):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("never happened")


def when(seconds_ago: float) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(time.time() - seconds_ago)


class World:
    """What the fake instruments know: the process list, the sysmontap batches and the frame rates still to come."""

    def __init__(self) -> None:
        self.procs: list[dict] = []
        self.proc_error: Exception | None = None
        self.batches: asyncio.Queue = asyncio.Queue()
        self.frames: asyncio.Queue = asyncio.Queue()
        self.hang_on_close = False  # graphics never finishes closing, as a tunnel that can't drain
        self.gate: asyncio.Event | None = None  # while set up, proclist waits for it
        self.waiting = False  # proclist is waiting at the gate
        self.closed: list[str] = []

    def row(self, pid: int, mb: float, cpu: float = 10.0) -> dict:
        return {"pid": pid, "physFootprint": mb * MB, "cpuUsage": cpu}


@pytest.fixture
def world(monkeypatch):
    w = World()

    class Dvt:
        def __init__(self, rsd):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            w.closed.append("dvt")

    class Info:
        def __init__(self, dvt):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            w.closed.append("info")

        async def proclist(self):
            if w.gate is not None:
                w.waiting = True
                await w.gate.wait()
                w.waiting = False
            if w.proc_error is not None:
                raise w.proc_error
            return [dict(p) for p in w.procs]

    class Tap:
        @classmethod
        async def create(cls, dvt, ms):
            return cls()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            w.closed.append("tap")

        async def iter_processes(self):
            while True:
                yield await w.batches.get()

    class Graphics:
        def __init__(self, dvt):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            if w.hang_on_close:
                await asyncio.sleep(3600)
            w.closed.append("graphics")

        async def fps(self):
            while True:
                yield await w.frames.get()

    base = "pymobiledevice3.services.dvt.instruments."
    for module, cls_name, cls in (("dvt_provider", "DvtProvider", Dvt), ("device_info", "DeviceInfo", Info),
                                  ("sysmontap", "Sysmontap", Tap), ("graphics", "Graphics", Graphics)):
        fake = types.ModuleType(base + module)
        setattr(fake, cls_name, cls)
        monkeypatch.setitem(sys.modules, base + module, fake)
    return w


class Quiet:
    rsd = None

    def log(self, text):
        pass


def test_a_launch_measures_the_new_process_not_the_old_one_or_the_setup(world):
    """Codex P2, PR #4: the running app (500 MB) was sampled before the window; the cold start uses 100 MB."""
    async def go():
        world.procs = [{"pid": 7, "bundleIdentifier": APP, "startDate": when(100)}]
        meter = device_module.AppMeter(Quiet(), APP)
        await meter.start()
        world.batches.put_nowait([world.row(7, 500, 90.0)])  # before the close: the old process
        world.frames.put_nowait(5.0)
        await settle(lambda: meter.measurement.memory_mb and meter.measurement.fps)
        # the app is closed and the window begins; the old process is still listed for a moment, the new one starts
        world.procs = [{"pid": 7, "bundleIdentifier": APP, "startDate": when(100)}]
        await meter.begin_window(fresh=True)
        world.batches.put_nowait([world.row(7, 520, 95.0)])  # a late sample of the old process
        world.frames.put_nowait(5.0)  # and a late frame rate of the set-up, which arrives after the window began
        world.procs = [{"pid": 7, "bundleIdentifier": APP, "startDate": when(100)},
                       {"pid": 8, "bundleIdentifier": APP, "startDate": when(0)}]
        world.batches.put_nowait([world.row(7, 530, 99.0), world.row(8, 100, 20.0)])
        world.batches.put_nowait([world.row(8, 110, 30.0)])
        world.frames.put_nowait(60.0)
        await settle(lambda: len(meter.measurement.memory_mb) == 2 and meter.measurement.fps)
        found = await meter.stop()
        # (the first frame rate after the window began covers a second that began before it: it is not kept)
        assert found.memory_mb == [100.0, 110.0] and found.cpu_pct == [20.0, 30.0] and found.fps == [60.0]
    run(go)


def test_a_window_without_a_launch_still_drops_what_was_sampled_before_it(world):
    async def go():
        world.procs = [{"pid": 7, "bundleIdentifier": APP, "startDate": when(100)}]
        meter = device_module.AppMeter(Quiet(), APP)
        await meter.start()
        world.batches.put_nowait([world.row(7, 800)])  # while the instruments were set up
        await settle(lambda: meter.measurement.memory_mb)
        await meter.begin_window(fresh=False)
        world.batches.put_nowait([world.row(7, 150)])
        await settle(lambda: meter.measurement.memory_mb == [150.0])
        assert (await meter.stop()).memory_mb == [150.0]  # the same process counts: it was not started for the window
    run(go)


def test_a_pid_taken_by_another_app_is_not_sampled_as_the_app(world):
    """Codex P1, PR #4: the meter kept its pids for 2 s, so a replacement process was sampled until the next refresh."""
    async def go():
        world.procs = [{"pid": 7, "bundleIdentifier": APP, "startDate": when(100)}]
        meter = device_module.AppMeter(Quiet(), APP)
        await meter.start()
        world.batches.put_nowait([world.row(7, 120)])
        await settle(lambda: meter.measurement.memory_mb == [120.0])
        world.procs = [{"pid": 7, "bundleIdentifier": "com.private.app", "startDate": when(1)}]  # pid 7, another app
        world.batches.put_nowait([world.row(7, 900, 99.0)])
        world.proc_error = RuntimeError("process list unavailable")  # and then the phone can't be asked at all
        world.batches.put_nowait([world.row(7, 901, 99.0)])
        await asyncio.sleep(0.1)
        world.proc_error = None
        world.procs = [{"pid": 7, "bundleIdentifier": APP, "startDate": when(0)}]  # the app itself, started again
        world.batches.put_nowait([world.row(7, 130)])
        await settle(lambda: len(meter.measurement.memory_mb) == 2)
        assert (await meter.stop()).memory_mb == [120.0, 130.0]
    run(go)


def test_stop_returns_when_an_instrument_cannot_close(world, monkeypatch):
    """CodeRabbit, PR #4: a tunnel that can't drain left graphics' close waiting, and stop() with it."""
    monkeypatch.setattr(device_module, "METER_TEARDOWN", 0.1)
    monkeypatch.setattr(device_module, "METER_STOP", 2.0)

    async def go():
        world.procs = [{"pid": 7, "bundleIdentifier": APP, "startDate": when(100)}]
        world.hang_on_close = True
        meter = device_module.AppMeter(Quiet(), APP)
        await meter.start()
        world.batches.put_nowait([world.row(7, 120)])
        await settle(lambda: meter.measurement.memory_mb)
        began = time.monotonic()
        found = await meter.stop()
        assert time.monotonic() - began < 1.5 and found.memory_mb == [120.0]
        assert "tap" in world.closed and "dvt" in world.closed  # the others were closed all the same
    run(go)


def test_batches_queued_or_waiting_for_the_process_list_when_the_window_begins_are_not_the_windows(world):
    """Codex, PR #4 cycle 2: two set-up batches (800 MB) were queued, the first held in the process list; the first was
    dropped by its epoch, but the second was taken afterwards under the new epoch and failed memory_mb=300."""
    async def go():
        world.procs = [{"pid": 7, "bundleIdentifier": APP, "startDate": when(100)}]
        meter = device_module.AppMeter(Quiet(), APP)
        await meter.start()
        world.gate = asyncio.Event()
        world.batches.put_nowait([world.row(7, 800)])
        world.batches.put_nowait([world.row(7, 800)])
        await settle(lambda: world.waiting)  # the first is held in the process list, the second is queued behind it
        await meter.begin_window(fresh=False)
        world.gate.set()
        await asyncio.sleep(0.05)
        world.batches.put_nowait([world.row(7, 150)])
        await settle(lambda: meter.measurement.memory_mb)
        assert (await meter.stop()).memory_mb == [150.0]
    run(go)


def test_a_set_up_batch_whose_process_list_fails_after_the_window_began_is_not_lost_to_the_window(world):
    """Codex, PR #4 cycle 4: setup batch 1 waited for the process list, the window began, then that lookup raised; the
    failure was counted against the new window, and valid 150 MB samples failed memory_mb=300 as partial."""
    async def go():
        from tapwright import performance
        world.procs = [{"pid": 7, "bundleIdentifier": APP, "startDate": when(100)}]
        meter = device_module.AppMeter(Quiet(), APP)
        await meter.start()
        world.gate = asyncio.Event()
        world.batches.put_nowait([world.row(7, 800)])
        await settle(lambda: world.waiting)
        await meter.begin_window(fresh=False)
        world.proc_error = RuntimeError("process list unavailable")
        world.gate.set()
        await asyncio.sleep(0.05)
        world.proc_error = None
        for _ in range(2):
            world.batches.put_nowait([world.row(7, 150, 10.0)])
        await settle(lambda: len(meter.measurement.memory_mb) == 2)
        found = await meter.stop()
        assert found.memory_mb == [150.0, 150.0] and found.lost == 0
        assert not performance.summarize(found, 2.0).samples_lost
    run(go)


def test_a_process_without_a_start_time_that_was_not_sampled_before_the_window_is_not_a_new_one(world):
    """CodeRabbit, PR #4: an old process the phone lists without a startDate, and that no sample had shown before the
    launch's window, was taken for the new process."""
    async def go():
        world.procs = [{"pid": 7, "bundleIdentifier": APP}]  # the old process: no startDate, never sampled
        meter = device_module.AppMeter(Quiet(), APP)
        await meter.start()
        await meter.begin_window(fresh=True)
        world.batches.put_nowait([world.row(7, 500)])
        await asyncio.sleep(0.1)
        world.procs = [{"pid": 7, "bundleIdentifier": APP}, {"pid": 8, "bundleIdentifier": APP, "startDate": when(0)}]
        world.batches.put_nowait([world.row(7, 510), world.row(8, 100)])
        await settle(lambda: meter.measurement.memory_mb)
        assert (await meter.stop()).memory_mb == [100.0]  # only the process that is shown to have started in the window
    run(go)


def test_an_old_process_listed_when_the_window_begins_is_never_the_new_one_whatever_its_start_time_says(world):
    """Codex and CodeRabbit, PR #4 cycle 3: an old process never sampled during the set-up, with a start time inside
    the one second of slack (or ahead of this computer's clock), was taken for the launch's new process."""
    async def go():
        world.procs = [{"pid": 7, "bundleIdentifier": APP, "startDate": when(0.5)},
                       {"pid": 9, "bundleIdentifier": APP, "startDate": when(-30)}]  # a clock ahead of ours
        meter = device_module.AppMeter(Quiet(), APP)
        await meter.start()
        await meter.begin_window(fresh=True)
        world.batches.put_nowait([world.row(7, 500), world.row(9, 600)])
        await asyncio.sleep(0.1)
        world.procs += [{"pid": 8, "bundleIdentifier": APP, "startDate": when(0)}]
        world.batches.put_nowait([world.row(7, 510), world.row(9, 610), world.row(8, 100)])
        await settle(lambda: meter.measurement.memory_mb)
        found = await meter.stop()
        assert found.memory_mb == [100.0] and found.cpu_pct == [10.0]  # only the process that was not there at the start
    run(go)


def test_a_launch_window_whose_process_list_cannot_be_had_at_the_start_says_so(world):
    """The old processes can't be told then, so the numbers are not claimed to be the launch's alone."""
    async def go():
        world.procs = [{"pid": 7, "bundleIdentifier": APP, "startDate": when(100)}]
        meter = device_module.AppMeter(Quiet(), APP)
        await meter.start()
        world.proc_error = RuntimeError("process list unavailable")
        await meter.begin_window(fresh=True)
        world.proc_error = None
        assert meter.measurement.lost >= 1
        await meter.stop()
    run(go)


def test_batches_still_waiting_when_the_window_stops_are_counted_as_lost(world, monkeypatch):
    """Codex, PR #4 cycle 3: one 100 MB sample was taken, then a 900 MB batch waited for the process list and more were
    queued behind it; stop() cancelled them without a trace and memory_mb=300 passed on the first sample alone."""
    monkeypatch.setattr(device_module, "FINAL_WAIT", 0.2)

    async def go():
        from tapwright import performance
        world.procs = [{"pid": 7, "bundleIdentifier": APP, "startDate": when(100)}]
        meter = device_module.AppMeter(Quiet(), APP)
        await meter.start()
        await meter.begin_window(fresh=False)
        world.batches.put_nowait([world.row(7, 100, 10.0)])
        await settle(lambda: meter.measurement.memory_mb == [100.0])
        world.gate = asyncio.Event()  # the process list never answers
        for _ in range(3):
            world.batches.put_nowait([world.row(7, 900, 90.0)])
        await settle(lambda: world.waiting)
        began = time.monotonic()
        found = await meter.stop()
        assert time.monotonic() - began < 2.0
        assert found.memory_mb == [100.0] and found.lost == 3
        assert performance.summarize(found, 2.0).samples_lost
    run(go)


def test_a_batch_that_is_nearly_done_when_the_window_stops_is_counted_not_lost(world, monkeypatch):
    """The bounded wait at the end lets a batch that was only a moment from its process list finish."""
    monkeypatch.setattr(device_module, "FINAL_WAIT", 2.0)

    async def go():
        world.procs = [{"pid": 7, "bundleIdentifier": APP, "startDate": when(100)}]
        meter = device_module.AppMeter(Quiet(), APP)
        await meter.start()
        await meter.begin_window(fresh=False)
        world.gate = asyncio.Event()
        world.batches.put_nowait([world.row(7, 120)])
        await settle(lambda: world.waiting)
        asyncio.get_running_loop().call_later(0.1, world.gate.set)
        found = await meter.stop()
        assert found.memory_mb == [120.0] and found.lost == 0
    run(go)


def test_batches_lost_to_a_process_list_that_cannot_be_read_are_counted(world):
    """Codex, PR #4 cycle 2: they were skipped without a trace, so one surviving sample passed memory_mb."""
    async def go():
        world.procs = [{"pid": 7, "bundleIdentifier": APP, "startDate": when(100)}]
        meter = device_module.AppMeter(Quiet(), APP)
        await meter.start()
        world.batches.put_nowait([world.row(7, 100)])
        await settle(lambda: meter.measurement.memory_mb == [100.0])
        world.proc_error = RuntimeError("process list unavailable")
        world.batches.put_nowait([world.row(7, 300)])
        world.batches.put_nowait([world.row(7, 310)])
        await settle(lambda: meter.measurement.lost == 2)
        found = await meter.stop()
        assert found.memory_mb == [100.0] and found.lost == 2
    run(go)

"""(2.0.3, brief Q fix 4) The log watchers share one question of the phone's process list: at most one a second while
busy, one every 30 s when quiet, none once every watched app has been gone a while. Measured on the phone before: 73
questions in 30 s with about a dozen apps opened that day, the phone idle (each watcher asked the whole list for its own
app every 3 s). Time here is a simulated minute, every span scaled down by K."""
import asyncio
import math
import time

from test_daemon import finish, make, started

from tapwright.daemon import SPRINGBOARD

K = 0.02  # real seconds per simulated second: the minute is 1.2 s
APPS = [f"com.example.app{i}" for i in range(10)]


def run(go):
    return asyncio.run(go())


def scaled(d):
    d.proc_every, d.proc_quiet, d.proc_busy_for = 1 * K, 30 * K, 60 * K
    d.log_check, d.log_retry, d.log_idle, d.log_drain, d.log_warmup = 3 * K, 2 * K, 60 * K, 1 * K, 0.0
    return d


async def watching(d):
    """Ten apps watched, each running with its own process, each with its log stream open."""
    device = d.session.device
    for i, bundle in enumerate(APPS):
        device.pids[bundle] = [100 + i]
        d.ensure_logs(bundle)
    for _ in range(500):
        if all(100 + i in device.streamed for i in range(len(APPS))):
            return
        await asyncio.sleep(0.005)
    raise AssertionError("the watchers never opened their streams")


async def a_minute(d) -> int:
    device = d.session.device
    before = device.proc_list_asks
    await asyncio.sleep(60 * K)
    return device.proc_list_asks - before


def test_ten_watched_apps_busy_ask_at_most_once_a_second():
    async def go():
        d = scaled(make())
        stop, task = await started(d)
        await watching(d)
        d.session.app = ("App 0", APPS[0])  # a watched app in front, and an agent calling
        d._called_at = time.monotonic()
        d.proc_busy_for = 1e9  # (busy for the whole minute)
        asks = await a_minute(d)
        await finish(stop, task)
        return asks
    asks = run(go)
    # one each 3 s per watcher before: 200 a minute; now one a second for all of them (timers may run late, never early)
    assert 10 <= asks <= 62


def test_ten_watched_apps_quiet_ask_every_thirty_seconds():
    async def go():
        d = scaled(make())
        stop, task = await started(d)
        await watching(d)
        d.session.app = ("Home screen", SPRINGBOARD)  # nothing watched in front, nobody calling
        d._called_at = d._diag_at = -math.inf
        await asyncio.sleep(2 * K)  # (a question already out finishes)
        asks = await a_minute(d)
        await finish(stop, task)
        return asks
    assert run(go) <= 3


def test_a_call_in_front_of_another_app_is_quiet_but_a_logs_call_is_busy():
    async def go():
        d = scaled(make())
        stop, task = await started(d)
        await watching(d)
        d.session.app = ("Other", "com.example.unwatched")
        d._called_at, d._diag_at = time.monotonic(), -math.inf
        quiet = d._proc_gap()
        d._diag_at = time.monotonic()
        busy = d._proc_gap()
        await finish(stop, task)
        return quiet, busy
    quiet, busy = run(go)
    assert quiet == 30 * K and busy == 1 * K


def test_once_every_app_has_gone_the_watchers_end_and_nothing_is_asked():
    async def go():
        d = scaled(make())
        stop, task = await started(d)
        await watching(d)
        device = d.session.device
        d.session.app = ("Home screen", SPRINGBOARD)
        d._called_at = d._diag_at = -math.inf
        for bundle in APPS:
            device.pids[bundle] = []
        for _ in range(400):
            if all(t.done() for t in d._log_tasks.values()):
                break
            await asyncio.sleep(0.01)
        ended = all(t.done() for t in d._log_tasks.values())
        before = device.proc_list_asks
        await asyncio.sleep(60 * K)
        after = device.proc_list_asks
        await finish(stop, task)
        return ended, after - before
    ended, asks = run(go)
    assert ended and asks == 0


def test_a_logs_call_on_a_quiet_phone_asks_at_once():
    async def go():
        d = scaled(make())
        d.proc_quiet = 1e6  # (a quiet phone's next question would be days away)
        d.log_idle = 1e6
        stop, task = await started(d)
        await watching(d)
        d.session.app = ("App 0", APPS[0])
        d._opened = APPS[0]
        d._called_at = d._diag_at = -math.inf
        await asyncio.sleep(2 * K)
        device = d.session.device
        device.emit(100, 1_700_000_000_000, "the app's own line")
        before = device.proc_list_asks
        began = time.monotonic()
        text = (await d.run_tool("a", "logs", {}))["text"]
        took = time.monotonic() - began
        await finish(stop, task)
        return text, took, device.proc_list_asks - before
    text, took, asks = run(go)
    assert "the app's own line" in text and took < 2.0 and asks >= 1


def test_one_question_serves_every_watcher_and_vouches_each_ring_up_to_its_count_as_it_went_out():
    async def go():
        d = scaled(make())
        stop, task = await started(d)
        await watching(d)
        answer = await d._shared_procs(APPS[3], d._procs_n, fresh=True)
        assert set(answer.of) == set(APPS) and answer.of[APPS[3]][0].pid == 103
        assert answer.marks == {b: d._rings[b].added for b in APPS} or all(
            answer.marks[b] <= d._rings[b].added for b in APPS)
        again = await d._shared_procs(APPS[7], answer.n - 1)
        assert again is answer  # (asked after the moment wanted: no second question)
        await finish(stop, task)
    run(go)


def test_a_question_that_fails_is_every_askers_error():
    async def go():
        d = scaled(make())
        stop, task = await started(d)
        await watching(d)
        d.session.device.proc_error = RuntimeError("the process list can't be read")
        answer = await d._shared_procs(APPS[0], d._procs_n, fresh=True)
        try:
            answer.of_or_raise(APPS[5])
        except RuntimeError as e:
            failed = str(e)
        else:
            failed = ""
        d.session.device.proc_error = None
        await finish(stop, task)
        return failed
    assert run(go) == "the process list can't be read"

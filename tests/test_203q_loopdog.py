"""(2.0.3, brief Q fix 5) When the daemon's event loop is held for more than a second, a watchdog thread writes where the
loop's thread is (file, line, function; no values), once per stall."""
import asyncio
import threading
import time

from test_daemon import make

from tapwright import loopdog


def held_here_for_a_while(seconds: float, secret: str) -> None:
    """Something synchronous on the loop (a big parse, a blocking socket call): `secret` is a local that must not show."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        len(secret)


def test_a_held_loop_is_written_once_with_where_it_is():
    lines: list[str] = []
    lock = threading.Lock()

    def log(text: str) -> None:
        with lock:
            lines.append(text)

    async def go():
        dog = loopdog.LoopDog(log, stall=0.15, every=0.02).start()
        try:
            for _ in range(5):
                await asyncio.sleep(0.01)
                dog.beat()
            held_here_for_a_while(0.5, "hunter2-the-account-password")
            dog.beat()
            for _ in range(5):
                await asyncio.sleep(0.01)
                dog.beat()
        finally:
            dog.stop()
    asyncio.run(go())
    assert len(lines) == 1  # once per stall, however many looks fell inside it
    text = lines[0]
    assert text.startswith("the event loop has been held for ")
    assert "test_203q_loopdog.py" in text and "in held_here_for_a_while" in text and "in go" in text
    assert "hunter2" not in text and "len(secret)" not in text  # no value, no line of source
    assert len(text.splitlines()) <= 1 + loopdog.FRAMES


def test_two_stalls_are_two_reports_and_a_ticking_loop_none():
    lines: list[str] = []

    async def go():
        dog = loopdog.LoopDog(lines.append, stall=0.1, every=0.02).start()
        try:
            for _ in range(20):
                await asyncio.sleep(0.01)
                dog.beat()
            assert lines == []
            held_here_for_a_while(0.3, "x")
            dog.beat()
            await asyncio.sleep(0.01)
            dog.beat()
            held_here_for_a_while(0.3, "x")
            dog.beat()
        finally:
            dog.stop()
    asyncio.run(go())
    assert len(lines) == 2


def test_the_stack_is_bounded_to_the_innermost_frames():
    lines: list[str] = []

    def deep(n: int) -> None:
        if n:
            deep(n - 1)
        else:
            dog.check()
    dog = loopdog.LoopDog(lines.append, stall=-1.0, frames=5)
    dog._loop_thread = threading.get_ident()
    deep(30)
    where = lines[0].splitlines()[1:]
    assert len(where) == 5 and where[-1].strip().endswith("in stack_of") and "in deep" in where[0]


def test_a_check_that_isnt_held_writes_nothing():
    lines: list[str] = []
    dog = loopdog.LoopDog(lines.append, stall=10.0)
    dog._loop_thread = threading.get_ident()
    assert dog.check() is False and lines == []


def test_the_daemons_loop_watch_runs_the_watchdog_and_stops_it():
    lines: list[str] = []

    async def go():
        d = make(log=lines.append, pace_tick=0.05)
        stop = asyncio.Event()
        before = {t.name for t in threading.enumerate()}
        watch = asyncio.create_task(d._watch_loop(stop))
        await asyncio.sleep(0.1)
        running = "tapwright-loop-dog" in {t.name for t in threading.enumerate()} - before
        held_here_for_a_while(1.4, "x")
        await asyncio.sleep(0.1)
        stop.set()
        await asyncio.wait_for(watch, 2)
        await asyncio.sleep(0.4)
        gone = "tapwright-loop-dog" not in {t.name for t in threading.enumerate()} - before
        return running, gone
    running, gone = asyncio.run(go())
    assert running and gone
    assert any(line.startswith("the event loop has been held for") and "in held_here_for_a_while" in line for line in lines)

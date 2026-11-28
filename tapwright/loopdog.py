"""(2.0.3) Which code holds the daemon's event loop. The tunnel to the phone is a TCP/IP stack in pure Python on that loop,
so while anything holds it no packet moves; the daemon's own check (Daemon._watch_loop) can only say afterwards how long
it was held ("the event loop was held for 15.63 s", the phone, 2026-11-26, during a look of a simple screen), never by
what. A thread can: when the loop hasn't ticked for STALL seconds, this one writes the loop thread's Python stack to the
log, once per stall. File, line and function names only: no local value, no line of source (a value could be an account's
or a person's)."""
from __future__ import annotations

import pathlib
import sys
import threading
import time
import traceback
from collections.abc import Callable

STALL = 1.0  # seconds without a tick of the loop before its stack is written
EVERY = 0.25  # seconds between the thread's looks
FRAMES = 15  # the innermost frames written
THREAD = "tapwright-loop-dog"  # the thread's name: the log's account scrub lets its lines pass (server.RedactAccounts)


def stack_of(thread_id: int, frames: int = FRAMES) -> list[str]:
    """The innermost `frames` frames of that thread's Python stack, outermost first, as `dir/file.py:line in function`
    (an empty list when the thread has gone). Nothing of a frame's values, and no source line, is read."""
    frame = sys._current_frames().get(thread_id)
    if frame is None:
        return []
    summary = traceback.StackSummary.extract(traceback.walk_stack(frame), limit=frames, lookup_lines=False)
    lines = [f"{'/'.join(pathlib.PurePath(f.filename).parts[-2:])}:{f.lineno} in {f.name}" for f in summary]
    return lines[::-1]


class LoopDog:
    """The watchdog: `start()` from the loop's own thread, `beat()` from the loop at every tick, `stop()` at the end."""

    def __init__(self, log: Callable[[str], None], stall: float = STALL, every: float = EVERY, frames: int = FRAMES,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.log, self.stall, self.every, self.frames, self.clock = log, stall, every, frames, clock
        self._beat = clock()
        self._beats = 0  # ticks so far: a stall is told once, for the tick it began after
        self._told = -1
        self._loop_thread: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "LoopDog":
        self._loop_thread = threading.get_ident()
        self.beat()
        self._thread = threading.Thread(target=self._run, name=THREAD, daemon=True)
        self._thread.start()
        return self

    def beat(self) -> None:
        self._beat = self.clock()
        self._beats += 1

    def stop(self, wait: bool = True) -> None:
        self._stop.set()
        if wait and self._thread is not None:
            self._thread.join(timeout=2 * self.every + 1)

    def check(self) -> bool:
        """One look (the thread's, or a test's): write the stack when the loop has been held past `stall` since its last
        tick and this stall wasn't written yet. True when it wrote."""
        beats, held = self._beats, self.clock() - self._beat
        if held <= self.stall or beats == self._told or self._loop_thread is None:
            return False
        self._told = beats
        where = stack_of(self._loop_thread, self.frames)
        self.log(f"the event loop has been held for {held:.1f} s so far; where its thread is (innermost last):\n  "
                 + ("\n  ".join(where) if where else "(no Python stack: the thread has ended)"))
        return True

    def _run(self) -> None:
        while not self._stop.wait(self.every):
            try:
                self.check()
            except Exception:  # noqa: BLE001 - the watchdog never takes the daemon down; a missed report is all it costs
                pass

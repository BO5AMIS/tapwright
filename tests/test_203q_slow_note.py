"""(2.0.3, brief Q fix 3) The "this screen took long to read" note: once per app, on the answer whose own read needed the
long try, with the time the whole read took (never rounded up to a second); again only after a long read of the app was
cut and a later one came back whole."""
import asyncio
import re
import time

import pytest
from test_slow_readable_203 import CUT, SHOP, Stream, look, shop

from tapwright import daemon as daemon_module
from tapwright import session as session_module


@pytest.fixture
def state(monkeypatch):
    """test_slow_readable_203's: the same fake runner's world, whatever the environment says."""
    monkeypatch.setattr(session_module.store, "load_state", lambda: {"runner_id": "runner"})
    monkeypatch.setattr(session_module, "FAST_READ", "1")
    monkeypatch.setattr(daemon_module, "STILL_AFTER_READ", 0.05, raising=False)
    monkeypatch.setattr(daemon_module, "STREAM_WAIT", 0.05, raising=False)
    monkeypatch.setattr(session_module, "LONG_PATH", "xctest", raising=False)
    monkeypatch.setattr(session_module, "TREE_PATH_FORCED", "")
    monkeypatch.setattr(session_module, "TREE_ATTRS", "lean")
    monkeypatch.setattr(daemon_module, "STILL_BOX", 0.6, raising=False)

ROOT = f'<XCUIElementTypeApplication bundleId="{SHOP}" name="Shop"></XCUIElementTypeApplication>'


def run(go):
    return asyncio.run(go())


def _noted(s, holder, began, read_began=None):
    token = session_module.READ_OWNER.set(holder)
    try:
        s.last_tier = "full"
        s._note_long(ROOT, began, read_began=read_began)
    finally:
        session_module.READ_OWNER.reset(token)


def test_said_once_until_a_long_read_of_the_app_is_cut(state):
    async def go():
        stream = Stream()
        s, runner = shop(stream)
        holder: list[str] = []
        _noted(s, holder, time.monotonic())
        _noted(s, holder, time.monotonic())  # the next answer that needed the long try: not again
        assert len(holder) == 1 and SHOP in s.slow_told
        runner.answers[8000] = CUT
        await s._long_read(60.0, SHOP, 3000)  # a long read of it is cut
        assert SHOP not in s.slow_told
        _noted(s, holder, time.monotonic())  # ...and a later one came back whole: said again
        assert len(holder) == 2
    run(go)


def test_a_long_read_with_no_answer_counts_as_cut(state):
    async def go():
        stream = Stream()
        s, runner = shop(stream)
        s.slow_told.add(SHOP)

        async def silent(*args, **kwargs):
            raise session_module.Unanswered("no answer in its time")
        s._compact_tree = silent
        assert await s._long_read(60.0, SHOP, 3000) == (None, True)
        assert SHOP not in s.slow_told
    run(go)


def test_the_note_says_the_whole_reads_time_unrounded(state):
    async def go():
        stream = Stream()
        s, runner = shop(stream)
        holder: list[str] = []
        now = time.monotonic()
        _noted(s, holder, now - 0.1, read_began=now - 0.4)  # a quick read: never "about 1 s"
        took = float(re.search(r"took about ([\d.]+) s", holder[0]).group(1))
        assert 0.4 <= took < 1.0
        s.slow_told.clear()
        holder.clear()
        _noted(s, holder, now - 3.0, read_began=now - 6.24)  # the cut normal read counts: the agent waited for both
        took = float(re.search(r"took about ([\d.]+) s", holder[0]).group(1))
        assert 6.2 <= took < 7.0
    run(go)


def test_a_read_of_no_call_neither_says_it_nor_uses_it_up(state):
    async def go():
        stream = Stream()
        s, runner = shop(stream)
        s.last_tier = "full"
        s._note_long(ROOT, time.monotonic())  # the live view's read: no call to tell
        assert SHOP not in s.slow_told
        holder: list[str] = []
        _noted(s, holder, time.monotonic())
        assert len(holder) == 1
    run(go)


def test_a_first_look_says_it_and_the_next_cut_try_does_not(state):
    """End to end on the fake runner: the first look of a slow app needs the long try and says so; a second call whose
    read needs the long try again (its planned long read wasn't given: here the slow mark is dropped) says nothing."""
    async def go():
        stream = Stream()
        s, runner = shop(stream)
        first: list[str] = []
        token = session_module.READ_OWNER.set(first)
        try:
            await look(s, stream)
        finally:
            session_module.READ_OWNER.reset(token)
        assert len(first) == 1 and "took about" in first[0]
        s.budgets._readable.discard(SHOP)  # (so the next read is a cut normal read and a long try, as on the phone)
        second: list[str] = []
        token = session_module.READ_OWNER.set(second)
        try:
            await look(s, stream)
        finally:
            session_module.READ_OWNER.reset(token)
        last = runner.paths("/wda/tw/tree")[-2:]
        assert "budget=3000" in last[0] and "budget=8000" in last[1]  # the second look needed the long try too
        assert second == []
    run(go)

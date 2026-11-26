"""Tapwright 2.0.3: a screen that is slow to read but does read. A shopping app's home screen (about 1,000 elements)
reads whole every time in 3.5-7.3 s cold, never within the normal budget (3 s): it answered "0 elements" for as long as
the agent stayed. A cut normal read is asked once more with the long budget (8 s) unless the picture changed across the
read inside a box of more than STILL_BOX (0.6) of it; an app whose long read came back whole keeps it unless its two
newest frames say something plays; every long read blocks the next one until a whole answer lifts that (5 minutes,
doubling each time in a row up to 6 hours; 1 minute for an app that read whole before), through reconnects. A screen
whose picture changes by more than the box across the read costs exactly what it cost in 2.0.2.

Stillness comes from a fake stream on the daemon's own hub and checks (Stream): like the runner's MJPEG it sends a frame
every GAP seconds while the runner isn't reading and none while it reads, so the newest frame ages by the length of the
read (review R203 H1: asked right after a 3 s read, "still?" always said no)."""
import asyncio
from urllib.parse import parse_qs, urlparse

import pytest

from fakes import DescribingSession
from test_daemon import finish, make, started
from test_latency_paths_200 import _v11
from test_vision_200 import TIKTOK, V9Runner, _app

from tapwright import daemon as daemon_module
from tapwright import session as session_module
from tapwright.control import Refused
from tapwright.device import ConnectionLost, DeviceError
from tapwright.session import ReadBudget, Session

SHOP = "com.example.shop"
OTHER = "com.example.other"
ITEM = {"t": "Button", "n": "deal-1", "l": "Deal of the day", "r": [16, 197, 398, 120]}
CUT = {"token": "c", "tree": _app(SHOP, "Shop"), "tier": "root", "truncated": True,
       "why": "full: kAXErrorIPCTimeout (after 2700 ms)"}
WHOLE = {"token": "w", "tree": _app(SHOP, "Shop", [ITEM]), "tier": "full", "truncated": False}
TOOK = {1500: 1.3, 3000: 2.8, 8000: 4.1}  # seconds each budget's read takes on the fake runner (the phone's numbers)


@pytest.fixture
def state(monkeypatch):
    monkeypatch.setattr(session_module.store, "load_state", lambda: {"runner_id": "runner"})
    monkeypatch.setattr(session_module, "FAST_READ", "1")
    # (no frame after a read: not a whole second here; raising=False: the file also runs against ee2b930)
    monkeypatch.setattr(daemon_module, "STILL_AFTER_READ", 0.05, raising=False)
    monkeypatch.setattr(daemon_module, "STREAM_WAIT", 0.05, raising=False)
    # (CodeRabbit, PR #30) what the environment may set at import: the defaults, whatever the shell running pytest has
    monkeypatch.setattr(session_module, "LONG_PATH", "xctest", raising=False)
    monkeypatch.setattr(session_module, "TREE_PATH_FORCED", "")
    monkeypatch.setattr(session_module, "TREE_ATTRS", "lean")
    monkeypatch.setattr(daemon_module, "STILL_BOX", 0.6, raising=False)


def budget(**kw) -> ReadBudget:
    """A ReadBudget with 3000 / 8000 ms whatever TAPWRIGHT_TREE_BUDGET and TAPWRIGHT_TREE_LONG_BUDGET say (its defaults
    are read at import)."""
    return ReadBudget(**{"normal_ms": 3000, "short_ms": 1500, "long_ms": 8000, **kw})


def run(go):
    return asyncio.run(go())


class Stream:
    """A daemon's frame hub and its stillness checks (_still_for_budget, _still_mark, _still_across) on a clock the test
    moves. `idle` sends `picture` every GAP seconds; a read (`reading`) moves the clock with no frame, and after it the
    stream goes on with `after` (None: no frame in time, the stream reopening after a stall). An agent's call holds the
    stream (`pump.viewers`); when its newest frame is stale (the stream closed and is opening again) it comes back with
    `picture`, unless `reopens` is False. Frames that come back come only while someone waits for one (the hub's
    `next_after`): the same every run, where real-time timers, or frames scheduled for a wait that never came, landed
    in a later read's check."""
    GAP = 0.04

    def __init__(self) -> None:
        self.d = make(DescribingSession())
        self.now = [100.0]
        self.d.hub._mono = lambda: self.now[0]
        self.picture: bytes = b"A"
        self.after: bytes | None = b"A"
        self.pending: bytes | None = None  # the picture the stream comes back with after the newest read
        self.reopens = True
        self.shown = 0  # (video frames shown so far)
        self.read_last = False  # (a read came after the newest idle frames: what follows it is `pending`, if anything)
        self.during: bytes | None = None  # (the video source) a frame every GAP while the runner reads
        self.d.pump.viewers = 1  # (as run_tool's `pump.watching()`)
        real = self.d.hub.next_after

        async def next_after(seq: int, timeout: float | None = None):
            picture, self.pending = self.pending, None
            fresh = getattr(self.d, "_frame_fresh", lambda: True)
            opening = picture is None and not self.read_last and self.reopens and not fresh()
            if opening:
                picture = self.picture  # (the stream opening again)
            if picture is not None:
                def comes() -> None:
                    for _ in range(4):
                        # (frames after a read come without moving the clock: the budget's windows, on the same
                        # clock, are then where 2.0.2's are, and the comparison with it holds at equal timing)
                        self.now[0] += self.GAP if opening else 0.0
                        self.d.hub.publish(picture)
                asyncio.get_running_loop().call_soon(comes)  # (once the waiter has taken the newest frame)
            return await real(seq, timeout)
        self.d.hub.next_after = next_after

    def clock(self) -> float:
        return self.now[0]

    def idle(self, seconds: float = 0.5, pictures: tuple[bytes, ...] | None = None) -> None:
        """`seconds` of frames: `picture` each time, or `pictures` in turn (a screen that moves)."""
        self.read_last = False
        for n in range(max(4, round(seconds / self.GAP))):
            self.now[0] += self.GAP
            self.d.hub.publish(pictures[n % len(pictures)] if pictures else self.picture)

    def video(self, seconds: float) -> None:
        """`seconds` of a video: another picture in every frame (byte frames, so no two compare alike)."""
        self.read_last = False
        for _ in range(max(4, round(seconds / self.GAP))):
            self.now[0] += self.GAP
            self.shown += 1
            self.d.hub.publish(b"video frame %d" % self.shown)

    def reading(self, seconds: float) -> None:
        if self.during is not None:
            for _ in range(round(seconds / self.GAP)):
                self.now[0] += self.GAP
                self.d.hub.publish(self.during)
        else:
            self.now[0] += seconds
        self.pending, self.read_last = self.after, True

    def attach(self, s: Session) -> None:
        """The session's stillness, as the daemon gives it (getattr: so this file runs against 2.0.3's first commit,
        ee2b930, which had only `screen_still` and asked it after the read: there the first test fails)."""
        s.screen_still = self.d._still_for_budget
        s.still_mark = getattr(self.d, "_still_mark", None)
        s.still_across = getattr(self.d, "_still_across", None)


class ShopRunner(V9Runner):
    """A runner whose tree answer depends on the budget asked (`answers`, else `other`), each read taking TOOK seconds of
    the stream's clock; `fail[budget]`: an exception raised (once) instead; `hold`: budgets whose read never ends."""

    def __init__(self, stream: Stream, answers=None, other=CUT, version=9):
        super().__init__(version=version)
        self.stream = stream
        self.answers = {3000: CUT, 8000: WHOLE} if answers is None else answers
        self.other = other
        self.timeouts: list[tuple[int | None, float]] = []
        self.raise_for: dict[int, Exception] = {}
        self.hold: set[int] = set()
        self.held = asyncio.Event()  # set when a read of `hold` went out
        self.long_at: list[float] = []

    async def http(self, method, path, payload=None, timeout=30.0):
        bare = path.replace("/session/S", "")
        if bare.startswith("/wda/tw/front"):
            self.requests.append((method, path, payload))
            return {"value": {"b": SHOP, "pid": 9, "ms": 4}}
        if bare.startswith("/wda/tw/tree"):
            self.requests.append((method, path, payload))
            budget = parse_qs(urlparse(bare).query).get("budget")
            ms = int(budget[0]) if budget else None
            self.timeouts.append((ms, timeout))
            if ms == 8000:
                self.long_at.append(self.stream.clock())
            if ms in self.hold:
                self.held.set()
                await asyncio.Event().wait()
            self.stream.reading(TOOK.get(ms or 0, 0.2))
            if ms in self.raise_for:
                raise self.raise_for.pop(ms)
            return {"value": dict(self.answers.get(ms, self.other))}
        return await super().http(method, path, payload, timeout)

    def budgets(self) -> list[int]:
        return [int(parse_qs(urlparse(p).query)["budget"][0]) for p in self.paths("/wda/tw/tree")]


def shop(stream: Stream, runner=None, long_ms=8000, app=("Shop", SHOP)):
    runner = runner or ShopRunner(stream)
    s = Session(runner)
    s.budgets = budget(clock=stream.clock, long_ms=long_ms)
    s.read_app = app
    stream.attach(s)
    return s, runner


async def look(s: Session, stream: Stream, timeout: float = 60.0, moving: bool = False):
    """A look after half a second of a still stream, or (`moving`) after 1.2 s of a video: a new picture every frame."""
    stream.pending = None
    if moving:
        stream.video(1.2)
    else:
        stream.idle(0.5)
    return await s.refresh(timeout=timeout)


# ---- H1: stillness across the read ----

def test_the_long_try_goes_out_when_the_screen_stood_still_across_the_read(state):
    # (fails on ee2b930: it asked "still?" straight after the read, when the newest frame was 2.8 s old)
    async def go():
        stream = Stream()
        s, runner = shop(stream)
        tree = await look(s, stream)
        assert runner.paths("/wda/tw/tree") == ["/wda/tw/tree?budget=3000&path=xctest",
                                                "/wda/tw/tree?budget=8000&path=xctest"]
        assert tree.tier == "full" and [e.label for e in tree.items] == ["Deal of the day"]
        assert s.last_tier == "full" and s.last_compact is not None  # (a whole tree: the "unchanged" baseline)
        assert s.budgets.slow(SHOP) and s.budget_app == SHOP
    run(go)


def test_a_screen_whose_picture_changed_across_the_read_gets_no_long_try(state):
    async def go():
        stream = Stream()
        stream.after = b"B"  # (not a picture: it can't be compared, so all of it counts as changed)
        s, runner = shop(stream)
        tree = await look(s, stream)
        assert runner.budgets() == [3000] and tree.tier == "root" and not s.budgets.blocked(SHOP)
    run(go)


def test_no_fresh_frame_after_the_read_gets_the_long_try(state):
    # (the lead, after the phone test: the try is the default; nothing to compare is no evidence of a video)
    async def go():
        stream = Stream()
        stream.after = None
        s, runner = shop(stream)
        tree = await look(s, stream)
        assert runner.budgets() == [3000, 8000] and tree.tier == "full"
    run(go)


def _requests(long_ms: int, script) -> list:
    """Every request a session makes for `script` [(moving before, picture after the read)], with `long_ms`. (A moving
    look's frames are all different, a still one's are b"A": the scripts change the picture across each read.)"""
    async def go():
        stream = Stream()
        s, runner = shop(stream, ShopRunner(stream, answers={}), long_ms=long_ms)
        for moving, after in script:
            stream.after = after
            await look(s, stream, moving=moving)
            stream.now[0] += 5.0
        return [(m, p) for m, p, _ in runner.requests]
    return run(go)


@pytest.mark.parametrize("script", [
    [(True, b"A")] * 6,  # moving before every read, and changed across it
    [(False, b"B")] * 6,  # still before, changed across it
    [(True, b"A"), (False, b"B"), (False, b"B"), (True, b"A"), (True, b"A"), (False, b"B")],
])
def test_a_screen_that_moves_asks_exactly_what_it_asks_with_the_long_budget_off(state, script):
    # (a screen whose picture changes across the read by more than STILL_BOX: these byte frames can't be compared,
    # so the change counts as 100 %)
    assert _requests(8000, script) == _requests(0, script)  # (long_ms 0: 2.0.2's ReadBudget in every branch)
    assert not any("budget=8000" in p for _, p in _requests(8000, script))


MOVING = [(0.0, False), (1.0, False), (8.0, False), (0.5, None), (30.0, False), (61.0, False), (0.2, False),
          (8.1, False), (400.0, False)]


def test_a_playing_feed_asks_what_2_0_2_asked(state):
    # (checked once against 2.0.2's own session.py, f9a229a, on this scenario and 200 random moving ones: the same
    # requests; pinned here)
    clock = [100.0]
    runner = _v11()
    s = Session(runner)
    s.budgets = budget(clock=lambda: clock[0])
    s.read_app = ("TikTok", TIKTOK)
    async def never(check: bool) -> tuple[None, bool]:  # (no frame is kept, and there is no still_across: no try)
        return None, False
    s.still_mark = never
    for advance, still in MOVING:
        clock[0] += advance
        s.screen_still = (lambda still=still: still) if still is not None else None
        asyncio.run(s.refresh())
    short, normal = "/wda/tw/tree?budget=1500&path=direct&attrs=lean", "/wda/tw/tree?budget=3000&path=xctest"
    assert [p for _, p, _ in runner.requests if "/wda/tw/" in p] == [
        "/wda/tw/version", "/wda/tw/screen", normal, "/wda/tw/front", "/wda/tw/screen", short, "/wda/tw/front",
        "/wda/tw/screen", short, normal, "/wda/tw/front", "/wda/tw/screen", short, normal]


# ---- slow but readable ----

def test_a_slow_app_reads_its_still_screens_with_the_long_budget_and_is_never_left_unasked(state):
    async def go():
        stream = Stream()
        s, runner = shop(stream, ShopRunner(stream, version=11))
        await look(s, stream)
        for _ in range(3):  # warm reads of the still screen
            stream.now[0] += 1.0
            await look(s, stream)
        assert runner.budgets() == [3000, 8000, 8000, 8000, 8000]
        assert runner.paths("/wda/tw/front") == []  # never the unasked root
    run(go)


def test_a_slow_apps_moving_screen_gets_2_0_2s_rules_and_keeps_the_mark(state):
    async def go():
        stream = Stream()
        s, runner = shop(stream)
        await look(s, stream)
        runner.answers = {}  # an autoplay page of the same app: nothing answers in time
        await look(s, stream, moving=True)
        await look(s, stream, moving=True)
        # no 8 s for a moving screen (review R203 M1), and 2.0.2's short budget at once: the whole long read was fast
        # (a warm read), which keeps the normal read's cut for moving screens (CodeRabbit, PR #30; 3000 before)
        assert runner.budgets() == [3000, 8000, 1500, 1500]
        assert s.budgets.slow(SHOP)
        runner.answers = {3000: CUT, 8000: WHOLE}  # back on the home screen, still: its long budget at once
        stream.now[0] += 1.0
        await look(s, stream)
        assert runner.budgets()[-1] == 8000
    run(go)


def test_a_cut_long_read_of_a_readable_app_blocks_it_for_long_retry_then_reads_with_the_long_budget(state):
    async def go():
        stream = Stream()
        s, runner = shop(stream)
        await look(s, stream)
        runner.answers = {}  # a paused video page of the same app, still: its planned long read is cut
        await look(s, stream)
        assert runner.budgets() == [3000, 8000, 8000] and s.budgets.blocked(SHOP) and not s.budgets.slow(SHOP)
        stream.now[0] += 20.0
        await look(s, stream)
        assert 8000 not in runner.budgets()[3:]  # blocked: 2.0.2's rules
        runner.answers = {3000: CUT, 8000: WHOLE}  # the home screen again
        stream.now[0] += session_module.LONG_RETRY
        await look(s, stream)
        assert runner.budgets()[-1] == 8000  # (as the read's own budget, not a second try: review R203 M2)
    run(go)


# ---- H2: the block ----

def test_an_app_that_never_read_whole_is_blocked_for_long_memory(state):
    async def go():
        stream = Stream()
        s, runner = shop(stream, ShopRunner(stream, answers={}))
        await look(s, stream)
        assert runner.budgets() == [3000, 8000]
        stream.now[0] += session_module.LONG_MEMORY - 10.0
        await look(s, stream)
        assert 8000 not in runner.budgets()[2:] and s.budgets.blocked(SHOP)
        stream.now[0] += 10.0
        assert not s.budgets.blocked(SHOP)
        # (2.0.2's rule still holds: the still screen's cut normal read keeps the short budget for BUDGET_MEMORY)
        stream.now[0] += session_module.BUDGET_MEMORY
        await look(s, stream)
        assert runner.budgets()[-2:] == [3000, 8000]
    run(go)


def test_a_screen_that_never_answers_costs_one_long_read_then_ever_fewer(state):
    # (a paused video, a photo post: its picture passes the gate, and each 8 s read is cut)
    async def go():
        stream = Stream()
        s, runner = shop(stream, ShopRunner(stream, answers={}))
        while stream.clock() < 100.0 + 3600.0:
            await look(s, stream)
            stream.now[0] += 10.0
        gaps = [b - a for a, b in zip(runner.long_at, runner.long_at[1:])]
        assert len(runner.long_at) == 4  # at 0, then 5, 10 and 20 minutes apart at least, in an hour
        assert all(g >= want for g, want in zip(gaps, (300.0, 600.0, 1200.0)))
    run(go)


def test_the_block_of_an_app_that_never_read_whole_doubles_up_to_six_hours_and_a_whole_read_ends_it():
    clock = [100.0]
    b = budget(clock=lambda: clock[0])
    spans = []
    for _ in range(9):
        b.long_sent(SHOP)
        b.answered(SHOP, True, 8000)
        spans.append(b.block_span(SHOP))
        assert b.blocked(SHOP) and b.blocked_for(SHOP) == spans[-1]
        clock[0] += spans[-1] - 1.0
        assert b.blocked(SHOP)
        clock[0] += 1.0
        assert not b.blocked(SHOP)
    assert spans == [300.0, 600.0, 1200.0, 2400.0, 4800.0, 9600.0, 19200.0, 21600.0, 21600.0]
    b.forget()  # (a reconnect: kept)
    b.long_sent(SHOP)
    assert b.block_span(SHOP) == 21600.0
    b.answered(SHOP, False, 8000)  # whole: the block and its count end, and the app read whole: 60 s from now on
    assert not b.blocked(SHOP)
    for want in (60.0, 120.0, 240.0):  # (review R203c H1: it grows from 60 s too)
        b.long_sent(SHOP)
        b.answered(SHOP, True, 8000)
        assert b.block_span(SHOP) == want
        clock[0] += want


@pytest.mark.parametrize("failure", ["unanswered", "refused", "dropped"])
def test_a_long_read_that_fails_counts_as_cut_and_keeps_the_block(state, failure):
    async def go():
        stream = Stream()
        s, runner = shop(stream)
        runner.raise_for[8000] = {
            "unanswered": ConnectionLost("WebDriverAgent didn't answer GET /wda/tw/tree within 13 s"),
            "refused": DeviceError("WebDriverAgent: something went wrong"),
            "dropped": ConnectionLost("WebDriverAgent didn't answer GET /wda/tw/tree within 13 s")}[failure]
        if failure == "dropped":
            runner.rsd = None  # (no tunnel to ask again on: today's raise)
        tree = await look(s, stream)
        # never asked again, the normal read's cut answer stands, no `not_answering`, the session is kept
        assert runner.budgets() == [3000, 8000] and tree.tier == "root" and s.last_tier == "root"
        assert s.sid == "S" and s.budgets.blocked(SHOP) and not s.budgets.slow(SHOP)
        await look(s, stream)
        assert 8000 not in runner.budgets()[2:]
    run(go)


def test_a_cancelled_long_read_keeps_the_block(state):
    async def go():
        stream = Stream()
        s, runner = shop(stream)
        runner.hold.add(8000)
        task = asyncio.create_task(look(s, stream))
        held = asyncio.create_task(runner.held.wait())
        try:
            # (CodeRabbit, PR #30) bounded: a look that never sends the long read ends the test, not hangs it
            done, _ = await asyncio.wait({task, held}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
            assert held in done and task not in done, runner.budgets()
        finally:
            for t in (task, held):
                t.cancel()
            results = await asyncio.gather(task, held, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError) and runner.budgets() == [3000, 8000]
        assert s.budgets.blocked(SHOP)
    run(go)


def test_a_reconnect_keeps_the_block_and_the_mark(state):
    async def go():
        stream = Stream()
        s, runner = shop(stream)
        await look(s, stream)  # SHOP reads whole with the long budget
        s.read_app = ("Other", OTHER)
        runner.answers = {}
        runner.other = {**CUT, "tree": _app(OTHER, "Other")}
        await look(s, stream)  # OTHER: a cut long try, blocked
        s.sid = None  # the runner restarted: a new session, ensure() forgets the budgets
        await look(s, stream)
        assert runner.budgets()[-1] != 8000 and s.budgets.blocked(OTHER) and s.budgets.slow(SHOP)
    run(go)


def test_a_long_read_answered_by_another_app_marks_that_one_and_blocks_the_first(state):
    async def go():
        stream = Stream()
        s, runner = shop(stream, ShopRunner(stream, answers={3000: CUT, 8000: {**WHOLE, "tree": _app(OTHER, "Other",
                                                                                                    [ITEM])}}))
        await look(s, stream)
        assert s.budgets.slow(OTHER) and not s.budgets.slow(SHOP) and s.budgets.blocked(SHOP)
    run(go)


# ---- the waits and the way ----

def test_a_long_read_is_waited_for_its_budget_and_its_grace(state):
    async def go():
        stream = Stream()
        s, runner = shop(stream)
        await look(s, stream)
        assert runner.timeouts == [(3000, 8.0), (8000, 13.0)]
    run(go)


def test_a_caller_that_cant_wait_for_a_long_read_gets_none(state):
    async def go():
        stream = Stream()
        s, runner = shop(stream)
        await look(s, stream, timeout=12.9)  # less than the long read's 13 s
        assert runner.budgets() == [3000] and not s.budgets.blocked(SHOP)
        await look(s, stream, timeout=60.0)
        assert s.budgets.slow(SHOP)
        await look(s, stream, timeout=5.0)  # (wait_for's usual cap) a slow app's read planned as in 2.0.2: the
        # normal read's cut is fresh (the whole long reads were fast), so the short budget
        assert runner.budgets()[-1] == 1500 and s.budgets.slow(SHOP)  # (a cut short read doesn't end it)
    run(go)


@pytest.mark.parametrize("forced, long_path, want", [
    ("", "xctest", "budget=8000&path=xctest"), ("", "direct", "budget=8000&path=direct&attrs=lean"),
    ("xctest", "direct", "budget=8000&path=xctest"), ("lean", "xctest", "budget=8000&path=direct&attrs=lean")])
def test_the_long_read_goes_by_tapwright_tree_long_path_unless_every_read_is_forced(state, monkeypatch, forced,
                                                                                     long_path, want):
    monkeypatch.setattr(session_module, "TREE_PATH_FORCED", forced)
    monkeypatch.setattr(session_module, "LONG_PATH", long_path)

    async def go():
        stream = Stream()
        s, runner = shop(stream, ShopRunner(stream, version=11))  # (the lean set: route version 10 and later)
        await look(s, stream)
        assert runner.paths("/wda/tw/tree")[-1] == "/wda/tw/tree?" + want
    run(go)


def test_the_long_budget_and_its_path_are_read_from_the_environment(monkeypatch):
    for raw, want in (("0", 0), ("0.0", 0), ("9000", 9000), ("abc", 8000), ("-5", 8000), ("inf", 8000)):
        monkeypatch.setenv("TAPWRIGHT_TREE_LONG_BUDGET", raw)
        assert session_module._env_ms_or_off("TAPWRIGHT_TREE_LONG_BUDGET", 8000) == want, raw
    for long_ms in (0, 2000, 3000):  # off, and not above the normal budget: off too
        assert not budget(long_ms=long_ms).long_on
    for raw, want in (("direct", "direct"), (" Direct ", "direct"), ("xctest", "xctest"), ("", "xctest"),
                      ("lean", "xctest")):
        assert session_module._long_path_of(raw) == want, raw


@pytest.mark.parametrize("long_ms", [0, 2500])
def test_with_the_long_budget_off_a_still_slow_screen_reads_as_in_2_0_2(state, long_ms):
    async def go():
        stream = Stream()
        s, runner = shop(stream, long_ms=long_ms)
        tree = await look(s, stream)
        await look(s, stream, moving=True)
        assert runner.budgets() == [3000, 1500] and tree.tier == "root"
    run(go)


def test_notification_centers_read_gets_no_long_try_and_feeds_no_memory(state):
    async def go():
        stream = Stream()
        s, runner = shop(stream)
        stream.idle()
        await s.read_center()
        assert runner.budgets() == [3000] and not s.budgets.slow(SHOP) and s.budgets.long_try(SHOP)
    run(go)


def test_a_quick_cut_is_asked_again_within_its_own_budget_and_is_not_the_long_try(state, monkeypatch):
    monkeypatch.setattr(session_module, "RETRY_GAP", 0.0)

    async def go():
        stream = Stream()
        quick = {**CUT, "why": "full: kAXErrorServerNotFound (after 8 ms)"}
        s, runner = shop(stream, ShopRunner(stream, answers={3000: quick, 8000: WHOLE}))
        await look(s, stream)
        got = runner.budgets()
        assert got[0] == 3000 and 2000 < got[1] < 3000 and got[2:] == [8000]  # the retry, then the long try
    run(go)


# ---- wait_for (review R203 M4, L3) ----

def test_wait_for_gives_its_reads_the_long_budget_only_when_its_deadline_has_room():
    d = make(DescribingSession())
    assert d._wait_for_read_cap(30.0) == daemon_module.WAIT_FOR_READ_MAX  # (a fake session: no ReadBudget)
    d.session.budgets = budget()
    assert d._wait_for_read_cap(10.0) == daemon_module.WAIT_FOR_READ_MAX  # (the default timeout: never a long read)
    assert d._wait_for_read_cap(13.4) == daemon_module.WAIT_FOR_READ_MAX  # (13 s and the margin don't fit)
    assert d._wait_for_read_cap(13.5) == 13.0 and d._wait_for_read_cap(60.0) == 8.0 + 13.0
    d.session.budgets = budget(long_ms=0)
    assert d._wait_for_read_cap(60.0) == daemon_module.WAIT_FOR_READ_MAX  # off: 2.0.2's cap


@pytest.mark.parametrize("budgets, timeout, turn, first", [(None, 30.0, 0.0, 5.0), (0, 30.0, 0.0, 5.0),
                                                           (8000, 10.0, 0.0, 5.0), (8000, 30.0, 0.0, 21.0),
                                                           (8000, 30.0, 16.5, 13.0), (8000, 30.0, 17.0, 5.0)])
def test_wait_for_decides_its_reads_time_once_the_turn_is_taken(budgets, timeout, turn, first):
    async def go():
        s = DescribingSession()
        if budgets is not None:
            s.budgets = budget(long_ms=budgets)
        d = make(s)
        stop, task = await started(d)
        s.read_timeouts.clear()
        real = d._read

        async def late_turn(fn):  # another holder kept the turn `turn` seconds of wait_for's clock
            d.control.clock.t += turn
            return await real(fn)
        d._read = late_turn
        with pytest.raises(Refused):
            await d.run_tool("a", "wait_for", {"text": "nothing like it", "timeout": timeout, "screenshot": False})
        await finish(stop, task)
        return s.read_timeouts
    assert run(go)[0] == pytest.approx(first)


# ---- the note (review R203 L1) ----

async def _looked_for(s: Session, stream: Stream, holder: list[str], **kw):
    """A look made by the call whose note holder (READ_OWNER) is `holder`."""
    token = session_module.READ_OWNER.set(holder)
    try:
        return await look(s, stream, **kw)
    finally:
        session_module.READ_OWNER.reset(token)


def test_the_note_is_for_the_call_whose_read_made_it_and_is_said_once(state):
    async def go():
        stream = Stream()
        s, runner = shop(stream)
        mine: list[str] = []
        await _looked_for(s, stream, mine)
        assert s.take_slow_note([]) is None and s.take_slow_note(None) is None
        note = s.take_slow_note(mine)
        assert note is not None and "took about" in note and "elements are listed" in note
        assert s.take_slow_note(mine) is None
        await _looked_for(s, stream, mine)  # a warm read with the long budget: no note
        assert mine == []
        s.budgets = budget(clock=stream.clock)
        await look(s, stream)  # a read of no call (the live view's): no note anywhere
        assert mine == [] and not hasattr(s, "slow_note")
    run(go)


def test_two_calls_that_overlap_each_keep_their_note(state):
    # (CodeRabbit, PR #30) call A's long try answered; before A built its answer, call B's did too. With one slot in
    # the session, B's note replaced A's and A's answer said nothing
    async def go():
        stream = Stream()
        s, runner = shop(stream)
        a: list[str] = []
        b: list[str] = []
        await _looked_for(s, stream, a)
        s.budgets = budget(clock=stream.clock)  # (another app's first look, for B)
        await _looked_for(s, stream, b)
        assert s.take_slow_note(a) is not None and s.take_slow_note(b) is not None
    run(go)


class NotingSession(DescribingSession):
    """A fake session whose looks make the long read's note as Session._note_long does (for the call they run in)."""

    def __init__(self):
        super().__init__()
        self.note_next = False

    async def look(self, screenshot=True):
        if self.note_next:
            self.note_next = False
            holder = session_module.READ_OWNER.get()
            if holder is not None:
                holder.append(session_module.LONG_NOTE.format(seconds=4.1))
        return await super().look(screenshot)

    take_slow_note = Session.take_slow_note


def test_the_daemon_adds_the_note_to_the_answer_of_the_call_whose_read_made_it():
    async def go():
        s = NotingSession()
        d = make(s)
        stop, task = await started(d)
        s.note_next = True
        mine = await d.run_tool("a", "look", {"screenshot": False})
        theirs = await d.run_tool("b", "look", {"screenshot": False})
        again = await d.run_tool("a", "look", {"screenshot": False})
        await finish(stop, task)
        return mine["text"], theirs["text"], again["text"]
    mine, theirs, again = run(go)
    assert mine.count("Note: this screen took about 4 s to read") == 1
    assert "took about" not in theirs and "took about" not in again


# ---- re-check R203b N1: an unchanged answer to a long read is a whole one ----

class UnchangedRunner(ShopRunner):
    """As the real runner: a read that sends the token of a tree that hasn't changed is answered `unchanged`."""

    async def http(self, method, path, payload=None, timeout=30.0):
        bare = path.replace("/session/S", "")
        if bare.startswith("/wda/tw/tree"):
            query = parse_qs(urlparse(bare).query)
            if query.get("since") == ["w"]:
                self.requests.append((method, path, payload))
                self.timeouts.append((int(query["budget"][0]), timeout))
                self.stream.reading(0.1)
                return {"value": {"token": "w", "unchanged": True}}
        return await super().http(method, path, payload, timeout)


def test_an_unchanged_answer_to_a_long_read_lifts_the_block_and_keeps_the_mark(state):
    # (the re-check's probe 1: [3000, 8000, 8000, 3000] on 0955e9e, the app blocked by its own unchanged read)
    async def go():
        stream = Stream()
        s, runner = shop(stream, UnchangedRunner(stream, version=11))
        await look(s, stream)  # 3000 cut, 8000 whole: slow but readable
        stream.now[0] += 1.0
        tree = await look(s, stream)  # planned 8000, `since=w`: unchanged
        assert "since=w" in runner.paths("/wda/tw/tree")[-1] and [e.label for e in tree.items] == ["Deal of the day"]
        assert not s.budgets.blocked(SHOP) and s.budgets.slow(SHOP) and s.last_tier == "full"
        stream.now[0] += 1.0
        s.last_compact = None  # (another screen of the app: no token)
        await look(s, stream)
        assert runner.budgets() == [3000, 8000, 8000, 8000]
    run(go)


def test_a_quick_cut_retry_inside_a_long_read_is_the_long_read(state, monkeypatch):
    monkeypatch.setattr(session_module, "RETRY_GAP", 0.0)

    async def go():
        stream = Stream()
        quick = {**CUT, "why": "full: kAXErrorServerNotFound (after 8 ms)"}
        s, runner = shop(stream, ShopRunner(stream, answers={3000: CUT, 8000: quick}, other=WHOLE))
        await look(s, stream)
        got = runner.budgets()
        assert got[:2] == [3000, 8000] and 5000 < got[2] < 8000  # the retry within what is left of the 8 s
        assert runner.timeouts[2][1] == pytest.approx(got[2] / 1000 + 5.0)  # (waited as a long read)
        assert s.budgets.slow(SHOP) and not s.budgets.blocked(SHOP)  # (its whole answer lifted the block)
    run(go)


# ---- re-check R203b N2: a stream that is opening again ----

def test_a_look_on_a_stream_that_is_opening_again_waits_for_it_and_makes_the_long_try(state):
    # (the re-check's probe 2: [3000] on 0955e9e, the newest frame 40 s old)
    async def go():
        stream = Stream()
        s, runner = shop(stream)
        stream.idle(0.5)
        stream.now[0] += 40.0  # the agent thought for 40 s; the stream closed after 30
        tree = await s.refresh(timeout=60)
        assert runner.budgets() == [3000, 8000] and tree.tier == "full"
    run(go)


def test_a_stream_that_doesnt_open_costs_one_wait_and_the_read_goes_out_as_before(state):
    async def go():
        stream = Stream()
        stream.reopens = False
        s, runner = shop(stream)
        stream.idle(0.5)
        stream.now[0] += 40.0
        waits = []
        real = stream.d.hub.next_after

        async def counted(seq, timeout=None):
            waits.append(timeout)
            return await real(seq, timeout)
        stream.d.hub.next_after = counted
        await s.refresh(timeout=60)
        await s.refresh(timeout=60)
        # (no frame kept: the try; it read whole, so the second read is the slow app's own long read)
        assert runner.budgets() == [3000, 8000, 8000]
        assert len(waits) == 1  # (not again until the stream sends a frame)
        stream.d.pump.viewers = 0  # no stream open at all: nothing is waited for
        stream.d._stream_wait_failed = None
        assert await stream.d._stream_ready() == "no stream is open" and len(waits) == 1
    run(go)


# ---- the lead's addition: a banner may change across the read (STILL_BOX) ----

def _screen(band: bool = False, clock: bool = False, black: bool = False, top: bool = False,
            spot: bool = False) -> bytes:
    """A 400x800 px screen (DescribingSession's 400x800 pt at 1x): white with three dark tiles, or all black; `band`:
    a banner band (rows 300-420) of another picture; `clock`: the status bar's minute changed (rows 10-30); `top`: a
    banner at the top (rows 60-320, about 36 % of the compared picture, the phone's look 2); `spot`: a spinner of 21 x 5
    thumb pixels (168 x 40 px, the phone's look 1)."""
    import io

    from PIL import Image, ImageDraw
    im = Image.new("RGB", (400, 800), "black" if black else "white")
    draw = ImageDraw.Draw(im)
    if not black:
        for y in (120, 480, 640):
            draw.rectangle([40, y, 360, y + 100], fill=(30, 60, 90))
    if band:
        draw.rectangle([0, 300, 399, 420], fill=(200, 40, 40))
    if clock:
        draw.rectangle([170, 10, 230, 30], fill="black")
    if top:
        draw.rectangle([0, 60, 399, 320], fill=(40, 160, 90))
    if spot:
        draw.rectangle([120, 440, 287, 479], fill=(220, 120, 0))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()


WHITE, BLACK = _screen(), _screen(black=True)
WHITE_BANNER, WHITE_CLOCK = _screen(band=True), _screen(clock=True)
WHITE_TOP, WHITE_SPOT = _screen(top=True), _screen(spot=True)


@pytest.mark.parametrize("after, box, tried", [
    (WHITE_BANNER, 0.6, True),  # a banner band changed during the read
    (WHITE_TOP, 0.6, True),  # the phone's look 2: a band at the top, 36 % of the picture
    (BLACK, 0.6, False),  # the whole picture changed (a feed): 100 %
    (WHITE_TOP, 0.35, False),  # (the first gate's 35 %: one point short on the phone)
    (WHITE_BANNER, 0.0, False),  # TAPWRIGHT_STILL_BOX=0: any change stops it
    (WHITE_CLOCK, 0.0, True),  # the status bar's clock is left out, whatever the box
    (BLACK, 1.0, True),  # TAPWRIGHT_STILL_BOX=1: nothing stops it
])
def test_the_frames_across_the_read_may_differ_inside_a_small_box(state, monkeypatch, after, box, tried):
    monkeypatch.setattr(daemon_module, "STILL_BOX", box)

    async def go():
        stream = Stream()
        stream.picture, stream.after = WHITE, after
        s, runner = shop(stream)
        await look(s, stream)
        assert (8000 in runner.budgets()) is tried
    run(go)


def test_a_banner_band_is_a_small_share_and_a_feed_all_of_it():
    from tapwright.stream import Frame
    a, banner, black = (Frame(n, 0, pic) for n, pic in enumerate((WHITE, WHITE_BANNER, BLACK)))
    share, box = daemon_module.unlike_share(a, banner, (400.0, 800.0))
    assert 0.10 < share < 0.25 and box is not None
    assert daemon_module.unlike_share(a, black, (400.0, 800.0))[0] > 0.9
    assert daemon_module.unlike_share(a, Frame(3, 0, WHITE_CLOCK), (400.0, 800.0)) == (0.0, None)
    assert 0.33 < daemon_module.unlike_share(a, Frame(5, 0, WHITE_TOP), (400.0, 800.0))[0] < 0.42  # (36 % on the phone)
    assert daemon_module.unlike_share(a, Frame(6, 0, WHITE_SPOT), (400.0, 800.0))[0] < 0.05
    assert daemon_module.unlike_share(a, Frame(4, 0, b"not a picture"), (400.0, 800.0)) is None
    for raw, want in (("", 0.6), ("0", 0.0), ("0.5", 0.5), ("1", 1.0), ("2", 0.6), ("-1", 0.6), ("x", 0.6),
                      ("nan", 0.6)):
        assert daemon_module._still_box_of(raw) == want, raw


@pytest.mark.parametrize("moving", [True, False])
def test_a_picture_that_changes_all_over_asks_what_it_asks_with_the_long_budget_off(state, moving):
    # (a feed changes the whole picture: white to black is 100 %, far past STILL_BOX; before the read, or during it)
    def requests(long_ms: int) -> list:
        async def go():
            stream = Stream()
            stream.picture = WHITE
            stream.after = WHITE if moving else BLACK  # (the other picture than the newest before the read: 100 %)
            s, runner = shop(stream, ShopRunner(stream, answers={}), long_ms=long_ms)
            for _ in range(4):
                stream.pending = None
                stream.idle(0.5, (WHITE, BLACK) if moving else None)
                await s.refresh(timeout=60)
                stream.now[0] += 5.0
            return [(m, p) for m, p, _ in runner.requests]
        return run(go)
    assert requests(8000) == requests(0)
    assert not any("budget=8000" in p for _, p in requests(8000))


# ---- re-check R203b M-b: why no long try, in the trace ----

@pytest.mark.parametrize("case, want", [
    ("off", "trace no long try: the long budget is off"),
    ("blocked", "is blocked for 29"),  # (297 s more: the read took its time of the 300)
    ("time", "s left of the read's time, a long read needs 13 s"),
    ("differs", "trace no long try: the picture changed across the read (fresh frame after"),
])
def test_every_reason_for_no_long_try_is_in_the_trace(state, monkeypatch, case, want):
    monkeypatch.setattr(session_module, "TRACE", True)

    async def go():
        stream = Stream()
        s, runner = shop(stream, long_ms=0 if case == "off" else 8000)
        lines: list[str] = []
        stream.d.log = lines.append
        runner.log = lines.append
        if case == "blocked":
            s.budgets.long_sent(SHOP)
        if case == "differs":
            stream.picture, stream.after = WHITE, BLACK
        await look(s, stream, timeout=12.0 if case == "time" else 60.0)
        assert 8000 not in runner.budgets()
        assert any(want in line for line in lines), lines
        if case == "differs":
            assert any("= 100 % of the picture, allowed 60 %)" in line for line in lines), lines
    run(go)


@pytest.mark.parametrize("case, want", [
    ("alike", "trace long try: yes (fresh frame after"),
    ("band", "% of the picture, allowed 60 %)"),
    ("no frame after", "trace long try: yes (no fresh frame within"),
    ("no frame kept", "trace long try: yes (no frame kept before the read)"),
])
def test_why_the_long_try_went_out_is_in_the_trace(state, monkeypatch, case, want):
    monkeypatch.setattr(session_module, "TRACE", True)

    async def go():
        stream = Stream()
        s, runner = shop(stream)
        lines: list[str] = []
        stream.d.log = lines.append
        runner.log = lines.append
        stream.picture = WHITE
        stream.after = {"alike": WHITE, "band": WHITE_TOP, "no frame after": None, "no frame kept": WHITE}[case]
        if case == "no frame kept":
            stream.reopens = False
            stream.idle(0.5)
            stream.now[0] += 40.0
            await s.refresh(timeout=60)
            assert any("trace no frame kept before the read (no fresh frame; waited" in line for line in lines), lines
        else:
            await look(s, stream)
        assert runner.budgets() == [3000, 8000]
        assert any(want in line for line in lines), lines
    run(go)


def test_a_spinner_before_the_read_doesnt_stop_the_long_try(state):
    # (the phone's look 1: a 21 x 5 thumb-pixel spot changing before the read made the first gate say "not still")
    async def go():
        stream = Stream()
        stream.picture, stream.after = WHITE, WHITE_SPOT
        s, runner = shop(stream)
        stream.idle(0.5, (WHITE, WHITE_SPOT))
        await s.refresh(timeout=60)
        assert runner.budgets() == [3000, 8000]
    run(go)


@pytest.mark.parametrize("before, planned", [
    ((WHITE, WHITE_SPOT), 8000),  # a spinner: the long budget
    ((WHITE_TOP, WHITE), 8000),  # a banner moving on (36 %): the long budget
    ((WHITE, BLACK), 1500),  # something plays (100 %): 2.0.2's rules (its cut history: the short budget)
    (None, 8000),  # one frame only: too few to tell, the long budget
])
def test_a_slow_apps_planned_read_is_given_up_only_when_its_two_newest_frames_say_something_plays(state, before,
                                                                                                 planned):
    async def go():
        stream = Stream()
        stream.picture = stream.after = WHITE
        s, runner = shop(stream)
        await look(s, stream)  # 3000 cut, 8000 whole: slow but readable
        stream.now[0] += 1.0
        if before is None:
            stream.d.hub._recent.clear()  # (the history: one frame)
            stream.d.hub.publish(WHITE)
            stream.read_last = False
        else:
            stream.idle(0.5, before)
        await s.refresh(timeout=60)
        assert runner.budgets()[2] == planned and s.budgets.slow(SHOP)
    run(go)


# ---- CodeRabbit, PR #30 ----

SHALLOW = {"token": "s", "tree": _app(SHOP, "Shop", [ITEM]), "tier": "shallow", "truncated": True,
           "why": "full: kAXErrorIPCTimeout (after 1800 ms)"}


def test_a_long_try_that_reads_less_keeps_the_normal_reads_tree(state):
    # (4709753259) the normal read gave part of the tree, with the element a wait_for looks for; the long try only the
    # root: the root replaced the part, and the element was gone
    async def go():
        stream = Stream()
        s, runner = shop(stream, ShopRunner(stream, answers={3000: SHALLOW, 8000: CUT}))
        tree = await look(s, stream)
        assert runner.budgets() == [3000, 8000]
        assert tree.tier == "shallow" and [e.label for e in tree.items] == ["Deal of the day"]
        assert s.last_tier == "shallow" and s.budgets.blocked(SHOP)  # (the long read's block stays)
    run(go)


def test_a_slow_apps_long_read_that_gets_no_answer_sends_nothing_after_it(state):
    # (4709753249) its fallback read queued behind the long read the runner was still reading, got no answer either,
    # was asked again, and the line was taken for dropped
    async def go():
        stream = Stream()
        s, runner = shop(stream)
        await look(s, stream)  # 3000 cut, 8000 whole: slow but readable
        runner.raise_for[8000] = ConnectionLost("WebDriverAgent didn't answer GET /wda/tw/tree within 13 s")
        stream.now[0] += 1.0
        tree = await look(s, stream)
        assert runner.budgets() == [3000, 8000, 8000]  # (no 3000 after the silent 8000)
        assert tree.tier == "root" and tree.app == ("Shop", SHOP) and s.sid == "S"
        assert s.last_why.startswith("not read: a read of com.example.shop with the long budget got no answer")
        assert s.budgets.blocked(SHOP)
    run(go)


def test_a_banners_reread_gets_what_is_left_of_the_reads_time(state, monkeypatch):
    # (4709753183) each reread was given the whole time again
    monkeypatch.setattr(session_module, "BANNER_POLL", 0.01)

    async def go():
        s = Session(V9Runner())
        timeouts: list[float] = []
        banners = [True]  # (a banner at the first read, then none)

        async def read_tree(timeout, center=False):
            timeouts.append(timeout)
            await asyncio.sleep(0.2)
            return "<XCUIElementTypeApplication/>"

        async def banner(source):
            return banners.pop() if banners else False

        async def nothing(source):
            return None
        s._read_tree, s._banner_over_an_app, s._follow_orientation = read_tree, banner, nothing
        await s._source(10.0)
        assert timeouts[0] == 10.0 and timeouts[1] < 10.0 - 0.2
    run(go)


def test_a_stream_that_never_sent_a_frame_is_waited_for_once(state):
    # (4709753195) with no frame at all the failed wait was recorded as 0 and never matched: every read waited again
    async def go():
        stream = Stream()
        stream.reopens = False
        waits = []
        real = stream.d.hub.next_after

        async def counted(seq, timeout=None):
            waits.append(timeout)
            return await real(seq, timeout)
        stream.d.hub.next_after = counted
        first = await stream.d._stream_ready()
        second = await stream.d._stream_ready()
        assert len(waits) == 1 and "waited" in first and "sent nothing since the last wait" in second
        stream.idle(0.5)  # a frame came: a wait may be made again later
        assert await stream.d._stream_ready() == ""
    run(go)


def test_frames_during_the_read_dont_undo_the_proof_from_before_it(state):
    # (4709753203) the video source sends frames while the runner reads: a 2.8 s read at 25 fps outlasts the hub's 64
    # frames, the mark's frame was gone, and the screen was taken for not still before the read
    async def go():
        stream = Stream()
        stream.during = b"A"
        s, runner = shop(stream)
        await look(s, stream)
        assert runner.budgets() == [3000, 8000]
    run(go)


# ---- review R203c ----

NONAME = {"token": "n", "tree": {k: v for k, v in _app(SHOP, "Shop").items() if k != "b"}, "tier": "root",
          "truncated": True, "why": "full: kAXErrorIPCTimeout (after 2700 ms)"}


def test_no_long_read_goes_out_for_an_app_that_isnt_known(state, monkeypatch):
    # (M1, the reviewer's probe: [3000, 8000] x 5 on 08a2f5b, and nothing blocked; after a reconnect the session knows
    # no app, and a cut root that names none was given an 8 s read that no block could count, again and again)
    monkeypatch.setattr(session_module, "TRACE", True)

    async def go():
        stream = Stream()
        s, runner = shop(stream, ShopRunner(stream, answers={3000: NONAME, 8000: NONAME}))
        lines: list[str] = []
        runner.log = lines.append
        for _ in range(5):
            s.read_app, s.budget_app = None, None  # (as a reconnect, or Session.reset(), leaves them)
            await look(s, stream)
            stream.now[0] += 10.0
        assert 8000 not in runner.budgets() and runner.budgets()[0] == 3000
        assert any("trace no long try: the app isn't known" in line for line in lines), lines
    run(go)


def test_a_long_read_that_gets_no_answer_and_a_reset_after_it_cant_loop(state):
    # (M1: the loop R203 H2 closed, through the unknown app: a silent long read, the session reset, the next look's
    # root names no app; no second long read follows)
    async def go():
        stream = Stream()
        s, runner = shop(stream, ShopRunner(stream, answers={3000: CUT, 8000: CUT}))
        runner.raise_for[8000] = ConnectionLost("WebDriverAgent didn't answer GET /wda/tw/tree within 13 s")
        await look(s, stream)
        assert runner.budgets() == [3000, 8000] and s.budgets.blocked(SHOP)
        runner.answers = {3000: NONAME, 8000: NONAME}
        for _ in range(3):
            s.read_app, s.budget_app = None, None
            await look(s, stream)
            stream.now[0] += 10.0
        assert runner.budgets().count(8000) == 1
    run(go)


def test_no_picture_at_all_gets_the_try_and_its_block(state):
    # (the reviewer's third probe, kept as decided: no frame kept is no evidence of a video; the block bounds it)
    async def go():
        stream = Stream()
        s, runner = shop(stream, ShopRunner(stream, answers={}))
        stream.d.pump.viewers = 0
        stream.now[0] += 40.0  # stale, and no stream open
        await s.refresh(timeout=60)
        assert runner.budgets() == [3000, 8000] and s.budgets.blocked_for(SHOP) == pytest.approx(300.0, abs=10.0)
    run(go)


def _band(y: int):
    """A 400x800 picture: white with a black band 300 px high at `y` (a video: the band moves 15 px a frame, so two
    neighbouring frames differ in a thin strip and two a second apart in most of the picture)."""
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (400, 800), "white")
    ImageDraw.Draw(im).rectangle([0, y, 399, y + 299], fill="black")
    return im


def _jpeg_of(im) -> bytes:
    import io
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()


@pytest.mark.parametrize("source", ["airplay", "mjpeg"])
def test_a_slow_apps_planned_read_sees_a_video_that_changes_little_from_frame_to_frame(state, source):
    # (H1: the check compared the two newest frames; the AirPlay feeder publishes one picture again at 30 Hz when a
    # 24 fps video has no new one, and two neighbouring frames of a video differ by a small box: it said "nothing plays"
    # and an 8 s read went out. Now: the newest frame against another picture at least 1 s older)
    from tapwright.stream import Picture

    async def go():
        stream = Stream()
        s, runner = shop(stream)
        await look(s, stream)  # 3000 cut, 8000 whole: readable
        stream.now[0] += 1.0
        hub = stream.d.hub
        shown, video_at = None, 0.0
        for tick in range(45):  # 1.5 s at 30 Hz of a 24 fps video
            t = tick / 30
            if shown is None or video_at <= t:
                y = round(video_at * 24) * 15
                shown = Picture(lambda y=y: _band(y)) if source == "airplay" else _jpeg_of(_band(y))
                video_at += 1 / 24
                fresh = True
            else:
                fresh = False
            stream.now[0] += 1 / 30
            if source == "airplay":
                hub.publish_frame(shown, stream.now[0] if fresh else None)
            elif fresh:
                hub.publish(shown)
        stream.read_last = False
        two = hub.recent()[-2:]
        from tapwright.daemon import unlike_share
        neighbours = 0.0 if stream.d._alike(two[0][1], two[1][1]) else \
            (unlike_share(two[0][1], two[1][1], (400.0, 800.0)) or (1.0, None))[0]
        assert neighbours < 0.6  # (what the old check saw: nothing plays)
        await s.refresh(timeout=60)
        assert runner.budgets()[2] != 8000 and s.budgets.slow(SHOP)
    run(go)


def test_a_picture_that_cant_be_judged_counts_as_a_video(state, monkeypatch):
    # (L3: comparing the pictures failed; it meant "try" across the read, and "nothing plays" before a planned read)
    async def go():
        stream = Stream()
        s, runner = shop(stream)

        def broken(a, b):
            raise ValueError("a picture that can't be decoded")
        monkeypatch.setattr(stream.d, "_change", broken)
        stream.picture, stream.after = WHITE, BLACK  # (not alike: the comparison is asked)
        tree = await look(s, stream)
        assert runner.budgets() == [3000] and tree.tier == "root"
    run(go)


@pytest.mark.parametrize("failure", ["unanswered", "refused"])
def test_the_block_doubles_after_long_reads_that_get_no_answer_or_are_refused(state, failure):
    async def go():
        stream = Stream()
        s, runner = shop(stream, ShopRunner(stream, answers={}))
        spans = []
        for _ in range(3):
            runner.raise_for[8000] = (ConnectionLost("WebDriverAgent didn't answer GET /wda/tw/tree within 13 s")
                                      if failure == "unanswered" else DeviceError("WebDriverAgent: refused"))
            stream.now[0] += s.budgets.blocked_for(SHOP) + session_module.BUDGET_MEMORY + 1.0
            await look(s, stream)
            spans.append(s.budgets.block_span(SHOP))
        assert runner.budgets().count(8000) == 3 and spans == [300.0, 600.0, 1200.0]
    run(go)


def test_an_hour_on_a_screen_that_never_answers_in_an_app_that_read_whole_once(state):
    # (H1: a feed in an app that read whole once, whose picture changes little across a read (paused, a photo): with a
    # fixed 60 s block, about 50 cut 8 s reads an hour; growing from 60 s, at most 6)
    async def go():
        stream = Stream()
        s, runner = shop(stream)
        await look(s, stream)  # readable
        runner.answers = {}
        runner.long_at.clear()
        began = stream.clock()
        while stream.clock() < began + 3600.0:
            await look(s, stream)
            stream.now[0] += 5.0
        assert 5 <= len(runner.long_at) <= 6
        gaps = [b - a for a, b in zip(runner.long_at, runner.long_at[1:])]
        assert all(g >= want for g, want in zip(gaps, (60.0, 120.0, 240.0, 480.0, 960.0)))
    run(go)


def test_the_block_s_length_is_in_the_trace_when_a_long_read_goes_out(state, monkeypatch):
    # (L4)
    monkeypatch.setattr(session_module, "TRACE", True)

    async def go():
        stream = Stream()
        s, runner = shop(stream, ShopRunner(stream, answers={}))
        lines: list[str] = []
        runner.log = lines.append
        await look(s, stream)
        assert any("trace long read of com.example.shop: if it isn't whole, com.example.shop is blocked for 300 s "
                   "(long read 1 in a row that may not be)" in line for line in lines), lines
    run(go)


def test_with_the_wait_on_the_clock_the_reads_are_2_0_2s_given_the_same_wait(state):
    # (M2) after each cut normal read of an app that isn't blocked, the answer waits for one fresh frame (about 0.27 s
    # on the phone). With that time on the clock, a screen whose picture changes across every read sends the reads
    # 2.0.2 sends when it is given the same 0.27 s after each cut normal read: the wait is the only difference
    WAIT = 0.27

    def requests(long_ms: int) -> list:
        async def go():
            stream = Stream()
            runner = ShopRunner(stream, answers={})
            real = runner.http

            async def http(method, path, payload=None, timeout=30.0):
                answer = await real(method, path, payload, timeout)
                if long_ms == 0 and "budget=3000" in path:
                    stream.now[0] += WAIT  # (2.0.2 given the same wait)
                return answer
            runner.http = http
            s, runner = shop(stream, runner, long_ms=long_ms)
            real_across = stream.d._still_across

            async def across(mark):
                stream.now[0] += WAIT  # (the fresh frame's wait, on the clock)
                return await real_across(mark)
            s.still_across = across
            for n in range(12):
                stream.pending = None
                stream.after = b"A" if n % 2 else b"B"
                if n % 2:
                    stream.video(1.2)
                else:
                    stream.idle(0.5)
                await s.refresh(timeout=60)
                stream.now[0] += (5.0, 8.0, 0.5, 30.0, 61.0, 2.0)[n % 6]
            return [(m, p) for m, p, _ in runner.requests]
        return run(go)
    with_wait = requests(8000)
    assert with_wait == requests(0) and not any("budget=8000" in p for _, p in with_wait)

"""Tapwright 2.0.3: a screen that is slow to read but does read. A shopping app's home screen (about 1,000 elements)
reads whole every time in 3.5-7.3 s cold, never within the normal budget (3 s): it answered "0 elements" for as long as
the agent stayed. A screen that stood still across a cut normal read is asked once more with the long budget (8 s); an
app whose long read came back whole keeps it on its still screens; every long read blocks the next one until a whole
answer lifts that (5 minutes, 1 for an app that read whole before), through reconnects. A screen that moves costs
exactly what it cost in 2.0.2.

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


def run(go):
    return asyncio.run(go())


class Stream:
    """A daemon's frame hub and its stillness checks (_still_for_budget, _still_mark, _still_across) on a clock the test
    moves. `idle` sends `picture` every GAP seconds; a read (`reading`) moves the clock with no frame, and after it the
    stream goes on with `after` (None: no frame in time, the stream reopening after a stall). Those frames come when
    the session waits for them (`_across`): the same every run, where real-time timers raced the test's clock."""
    GAP = 0.04

    def __init__(self) -> None:
        self.d = make(DescribingSession())
        self.now = [100.0]
        self.d.hub._mono = lambda: self.now[0]
        self.picture: bytes = b"A"
        self.after: bytes | None = b"A"
        self.pending: bytes | None = None  # the picture the stream comes back with after the newest read

    def clock(self) -> float:
        return self.now[0]

    def idle(self, seconds: float = 0.5, pictures: tuple[bytes, ...] | None = None) -> None:
        """`seconds` of frames: `picture` each time, or `pictures` in turn (a screen that moves)."""
        for n in range(max(4, round(seconds / self.GAP))):
            self.now[0] += self.GAP
            self.d.hub.publish(pictures[n % len(pictures)] if pictures else self.picture)

    def reading(self, seconds: float) -> None:
        self.now[0] += seconds
        self.pending = self.after

    async def _across(self, mark: object) -> bool:
        """The daemon's _still_across, with the frames that come once the runner has answered."""
        picture, self.pending = self.pending, None
        if picture is not None:
            def comes_back() -> None:
                for _ in range(3):
                    self.now[0] += self.GAP
                    self.d.hub.publish(picture)
            asyncio.get_running_loop().call_soon(comes_back)  # (after the check has taken the newest frame)
        return await self.d._still_across(mark)

    def attach(self, s: Session) -> None:
        """The session's stillness, as the daemon gives it (getattr: so this file runs against 2.0.3's first commit,
        ee2b930, which had only `screen_still` and asked it after the read: there the first test fails)."""
        s.screen_still = self.d._still_for_budget
        s.still_mark = getattr(self.d, "_still_mark", None)
        s.still_across = self._across if hasattr(self.d, "_still_across") else None


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
    s.budgets = ReadBudget(clock=stream.clock, long_ms=long_ms)
    s.read_app = app
    stream.attach(s)
    return s, runner


async def look(s: Session, stream: Stream, timeout: float = 60.0, moving: bool = False):
    """A look after half a second of the stream: still, or (`moving`) changing on every frame."""
    stream.pending = None
    stream.idle(0.5, (b"A", b"B") if moving else None)
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


def test_a_screen_that_moved_during_the_read_or_sent_no_frame_after_it_gets_no_long_try(state):
    for after in (b"B", None):
        async def go():
            stream = Stream()
            stream.after = after
            s, runner = shop(stream)
            tree = await look(s, stream)
            assert runner.budgets() == [3000] and tree.tier == "root" and not s.budgets.blocked(SHOP)
        run(go)


def _requests(long_ms: int, script) -> list:
    """Every request a session makes for `script` [(moving before, picture after the read)], with `long_ms`."""
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
    [(True, b"A")] * 6,  # moving before every read
    [(False, b"B")] * 6,  # still before, moved during
    [(True, b"A"), (False, b"B"), (True, b"B"), (False, None), (True, b"A"), (False, b"B")],
])
def test_a_screen_that_moves_asks_exactly_what_it_asks_with_the_long_budget_off(state, script):
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
    s.budgets = ReadBudget(clock=lambda: clock[0])
    s.read_app = ("TikTok", TIKTOK)
    s.still_mark = lambda: None  # (the stream never proves it still)
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
        assert runner.budgets() == [3000, 8000, 3000, 1500]  # no 8 s for a moving screen (review R203 M1)
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


def test_a_still_screen_that_never_answers_costs_one_long_try_per_long_memory(state):
    async def go():
        stream = Stream()
        s, runner = shop(stream, ShopRunner(stream, answers={}))
        while stream.clock() < 100.0 + 2.5 * session_module.LONG_MEMORY:
            await look(s, stream)
            stream.now[0] += 10.0
        gaps = [b - a for a, b in zip(runner.long_at, runner.long_at[1:])]
        assert len(runner.long_at) == 3 and all(g >= session_module.LONG_MEMORY for g in gaps)
    run(go)


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
        while runner.budgets() != [3000, 8000]:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
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
        await look(s, stream, timeout=5.0)  # (wait_for's usual cap) a slow app's read planned as in 2.0.2
        assert runner.budgets()[-1] == 3000 and s.budgets.slow(SHOP)  # (a cut normal read doesn't end it)
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
        assert not ReadBudget(long_ms=long_ms).long_on
    for raw, want in (("direct", "direct"), (" Direct ", "direct"), ("xctest", "xctest"), ("", "xctest"),
                      ("lean", "xctest")):
        assert session_module._long_path_of(raw) == want, raw
    assert session_module.LONG_PATH == "xctest"  # (the default)


@pytest.mark.parametrize("long_ms", [0, 2500])
def test_with_the_long_budget_off_a_still_slow_screen_reads_as_in_2_0_2(state, long_ms):
    async def go():
        stream = Stream()
        s, runner = shop(stream, long_ms=long_ms)
        tree = await look(s, stream)
        await look(s, stream, moving=True)
        assert runner.budgets() == [3000, 1500] and tree.tier == "root" and s.slow_note is None
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
    d.session.budgets = ReadBudget()
    assert d._wait_for_read_cap(10.0) == daemon_module.WAIT_FOR_READ_MAX  # (the default timeout: never a long read)
    assert d._wait_for_read_cap(13.4) == daemon_module.WAIT_FOR_READ_MAX  # (13 s and the margin don't fit)
    assert d._wait_for_read_cap(13.5) == 13.0 and d._wait_for_read_cap(60.0) == 8.0 + 13.0
    d.session.budgets = ReadBudget(long_ms=0)
    assert d._wait_for_read_cap(60.0) == daemon_module.WAIT_FOR_READ_MAX  # off: 2.0.2's cap


@pytest.mark.parametrize("budgets, timeout, turn, first", [(None, 30.0, 0.0, 5.0), (0, 30.0, 0.0, 5.0),
                                                           (8000, 10.0, 0.0, 5.0), (8000, 30.0, 0.0, 21.0),
                                                           (8000, 30.0, 16.5, 13.0), (8000, 30.0, 17.0, 5.0)])
def test_wait_for_decides_its_reads_time_once_the_turn_is_taken(budgets, timeout, turn, first):
    async def go():
        s = DescribingSession()
        if budgets is not None:
            s.budgets = ReadBudget(long_ms=budgets)
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

def test_the_note_is_for_the_call_whose_read_made_it_and_is_said_once(state):
    async def go():
        stream = Stream()
        s, runner = shop(stream)
        mine, other = object(), object()
        token = session_module.READ_OWNER.set(mine)
        try:
            await look(s, stream)
        finally:
            session_module.READ_OWNER.reset(token)
        assert s.take_slow_note(other) is None and s.take_slow_note(None) is None
        note = s.take_slow_note(mine)
        assert note is not None and "took about" in note and "elements are listed" in note
        assert s.take_slow_note(mine) is None
        token = session_module.READ_OWNER.set(mine)
        try:
            await look(s, stream)  # a warm read with the long budget: no note
        finally:
            session_module.READ_OWNER.reset(token)
        assert s.take_slow_note(mine) is None
        s.budgets = ReadBudget(clock=stream.clock)
        await look(s, stream)  # a read of no call (the live view's): its note is dropped
        assert s.slow_note is None
    run(go)


class NotingSession(DescribingSession):
    """A fake session whose looks make the long read's note as Session._note_long does (for the call they run in)."""

    def __init__(self):
        super().__init__()
        self.slow_note = None
        self.note_next = False

    async def look(self, screenshot=True):
        if self.note_next:
            self.note_next = False
            owner = session_module.READ_OWNER.get()
            self.slow_note = (owner, session_module.LONG_NOTE.format(seconds=4.1)) if owner is not None else None
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
        s.slow_note = (object(), "a note of another call")  # made by a call that never took it
        later = await d.run_tool("b", "look", {"screenshot": False})
        await finish(stop, task)
        return mine["text"], theirs["text"], again["text"], later["text"]
    mine, theirs, again, later = run(go)
    assert mine.count("Note: this screen took about 4 s to read") == 1
    assert "took about" not in theirs and "took about" not in again and "another call" not in later

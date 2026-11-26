"""Tapwright 2.0.3: a screen that is slow to read but does read. A shopping app's home screen (about 1,000 elements)
reads whole every time in about 4 s cold, never within the normal budget (3 s): it answered "0 elements" for as long as
the agent stayed. A still screen whose normal read is cut is asked once more with the long budget (8 s); an app whose
long read came back whole keeps it; a cut long read ends that and blocks another try for LONG_MEMORY. A screen that
moves (a playing feed that never answers) costs exactly what it cost in 2.0.2."""
import asyncio
from urllib.parse import parse_qs, urlparse

import pytest

from fakes import DescribingSession
from test_daemon import finish, make, started
from test_latency_paths_200 import _Clock, _v11
from test_vision_200 import TIKTOK, V9Runner, _app

from tapwright import daemon as daemon_module
from tapwright import session as session_module
from tapwright.session import ReadBudget, Session

SHOP = "com.example.shop"
ITEM = {"t": "Button", "n": "deal-1", "l": "Deal of the day", "r": [16, 197, 398, 120]}
CUT = {"token": "c", "tree": _app(SHOP, "Shop"), "tier": "root", "truncated": True,
       "why": "full: kAXErrorIPCTimeout (after 2700 ms)"}
WHOLE = {"token": "w", "tree": _app(SHOP, "Shop", [ITEM]), "tier": "full", "truncated": False}


@pytest.fixture
def state(monkeypatch):
    monkeypatch.setattr(session_module.store, "load_state", lambda: {"runner_id": "runner"})
    monkeypatch.setattr(session_module, "FAST_READ", "1")


class ShopRunner(V9Runner):
    """A runner whose tree answer depends on the budget asked: `answers[budget]` (else `other`), each request's
    budget and the time the client waits for it (`timeouts`) kept."""

    def __init__(self, answers=None, other=CUT):
        super().__init__()
        self.answers = {3000: CUT, 8000: WHOLE} if answers is None else answers
        self.other = other
        self.timeouts: list[tuple[int | None, float]] = []

    async def http(self, method, path, payload=None, timeout=30.0):
        bare = path.replace("/session/S", "")
        if bare.startswith("/wda/tw/tree"):
            self.requests.append((method, path, payload))
            budget = parse_qs(urlparse(bare).query).get("budget")
            ms = int(budget[0]) if budget else None
            self.timeouts.append((ms, timeout))
            return {"value": dict(self.answers.get(ms, self.other))}
        return await super().http(method, path, payload, timeout)

    def budgets(self):
        return [int(parse_qs(urlparse(p).query)["budget"][0]) for p in self.paths("/wda/tw/tree")]


def shop(runner, clock=None, still=True, budgets=None):
    s = Session(runner)
    s.budgets = budgets or ReadBudget(clock=clock or _Clock())
    s.read_app = ("Shop", SHOP)
    s.screen_still = (lambda: still) if isinstance(still, bool) else still
    return s


def read(s, timeout=60):
    return asyncio.run(s.refresh(timeout=timeout))


# ---- the long try ----

def test_a_still_screen_cut_on_the_normal_budget_is_read_whole_with_the_long_one_in_the_same_read(state):
    runner = ShopRunner()
    s = shop(runner)
    tree = read(s)
    assert runner.paths("/wda/tw/tree") == ["/wda/tw/tree?budget=3000&path=xctest", "/wda/tw/tree?budget=8000&path=xctest"]
    assert tree.tier == "full" and [e.label for e in tree.items] == ["Deal of the day"]
    assert s.last_tier == "full" and s.last_compact is not None  # (a whole tree: the "unchanged" baseline)
    assert s.budgets.slow(SHOP) and s.budget_app == SHOP


def test_a_slow_but_readable_app_reads_with_the_long_budget_and_is_never_left_unasked(state):
    runner = _v11()  # (route version 11: a read may be left unasked, by the app in front)
    runner.trees = [dict(CUT), dict(WHOLE)]
    runner.front = {"b": SHOP, "pid": 9, "ms": 4}
    clock = _Clock()
    s = shop(runner, clock)
    read(s)
    for still in (True, False, False):  # warm reads, on a screen that moves too: asked, with the long budget
        s.screen_still = lambda still=still: still
        clock.t += 1.0
        read(s)
    assert [p.split("&")[0] for p in runner.paths("/wda/tw/tree")] == [
        "/wda/tw/tree?budget=3000", "/wda/tw/tree?budget=8000", *["/wda/tw/tree?budget=8000"] * 3]
    assert runner.paths("/wda/tw/front") == []  # never the unasked root
    assert s.budgets.plan(SHOP, still=lambda: False) == (8000, False)


def test_a_cut_long_read_ends_it_and_blocks_another_long_try_for_long_memory(state):
    clock = _Clock()
    runner = ShopRunner()
    s = shop(runner, clock)
    read(s)
    assert s.budgets.slow(SHOP)
    runner.answers = {}  # from now on nothing answers in time: a paused video in the same app
    read(s)  # the app's long read: cut
    assert runner.budgets()[-1] == 8000 and not s.budgets.slow(SHOP)
    for _ in range(12):  # five minutes of looks at the still screen: no long try
        clock.t += 24.0
        read(s)
    assert 8000 not in runner.budgets()[3:]
    clock.t += 300.0 - 12 * 24.0  # LONG_MEMORY after the cut long read: one more try
    read(s)
    assert runner.budgets()[-2:] == [3000, 8000]


def test_after_the_block_a_cut_normal_read_follows_todays_rules(state):
    # the block ended: today's memory (short budget, unasked) is untouched by the long try that was cut
    clock = _Clock()
    runner = ShopRunner(answers={})
    s = shop(runner, clock)
    read(s)
    assert runner.budgets() == [3000, 8000]
    s.screen_still = lambda: False
    clock.t += 1.0
    read(s)
    assert runner.budgets()[-1] == 1500  # the short budget, as in 2.0.2 after a cut normal read


def test_a_quick_cut_is_asked_again_within_its_own_budget_and_is_not_the_long_try(state, monkeypatch):
    quick = {**CUT, "why": "full: kAXErrorServerNotFound (after 8 ms)"}
    runner = ShopRunner(answers={3000: quick, 8000: WHOLE})
    s = shop(runner)
    monkeypatch.setattr(session_module, "RETRY_GAP", 0.0)
    read(s)
    got = runner.budgets()
    assert got[0] == 3000 and 2000 < got[1] < 3000 and got[2:] == [8000]  # the quick cut's retry, then the long try


def test_the_read_tells_the_agent_once(state):
    runner = ShopRunner()
    s = shop(runner)
    read(s)
    note = s.take_slow_note()
    assert note is not None and "elements are listed" in note and "took about" in note
    assert s.take_slow_note() is None
    read(s)  # a warm read with the long budget: no note
    assert s.take_slow_note() is None


def test_another_app_named_by_a_short_reads_answer_gets_its_own_long_try(state):
    # a short read of the app the budget was for names another app: read again with the normal budget, and that one,
    # cut on a still screen, gets the long try under its own name
    clock = _Clock()
    runner = ShopRunner()
    s = shop(runner, clock)
    asyncio.run(s.ensure())  # (a session's start forgets every budget)
    s.budgets.answered(TIKTOK, True, 3000)  # the feed's memory: a short budget
    s.read_app = ("TikTok", TIKTOK)
    runner.answers = {1500: {**CUT}, 3000: CUT, 8000: WHOLE}
    calls = iter([False, True])  # the first read's plan (moving), then the long try's question (still)
    s.screen_still = lambda: next(calls)
    read(s)
    assert runner.budgets() == [1500, 3000, 8000] and s.budgets.slow(SHOP) and not s.budgets.slow(TIKTOK)


# ---- never on a screen that moves ----

def _feed_requests(long_ms, steps):
    """Every request a session makes for `steps` (clock advance, still) on a feed that never answers (2.0.2's test
    runner), with a ReadBudget whose long budget is `long_ms`."""
    clock = _Clock()
    runner = _v11()
    s = Session(runner)
    s.budgets = ReadBudget(clock=clock, long_ms=long_ms)
    s.read_app = ("TikTok", TIKTOK)
    for advance, still in steps:
        clock.t += advance
        s.screen_still = (lambda still=still: still) if still is not None else None
        asyncio.run(s.refresh())
    return [(m, p) for m, p, _ in runner.requests]


MOVING = [(0.0, False), (1.0, False), (8.0, False), (0.5, None), (30.0, False), (61.0, False), (0.2, False),
          (8.1, False), (400.0, False)]


def test_a_moving_screen_gets_no_long_try_and_asks_exactly_what_2_0_2_asked(state):
    with_long = _feed_requests(8000, MOVING)
    assert with_long == _feed_requests(0, MOVING)  # (long_ms 0: 2.0.2's ReadBudget, every branch)
    assert not any("budget=8000" in p for _, p in with_long)
    # (checked against 2.0.2's own session.py, f9a229a, on this scenario and 200 random moving ones: the same requests)
    short, normal = "/wda/tw/tree?budget=1500&path=direct&attrs=lean", "/wda/tw/tree?budget=3000&path=xctest"
    assert [p for _, p in with_long if "/wda/tw/" in p] == [
        "/wda/tw/version", "/wda/tw/screen", normal, "/wda/tw/front", "/wda/tw/screen", short, "/wda/tw/front",
        "/wda/tw/screen", short, normal, "/wda/tw/front", "/wda/tw/screen", short, normal]


def test_a_still_feed_that_never_answers_costs_one_long_try_per_long_memory(state):
    clock = _Clock()
    runner = _v11()  # never answers: root every time
    s = Session(runner)
    s.budgets = ReadBudget(clock=clock)
    s.read_app, s.screen_still = ("TikTok", TIKTOK), (lambda: True)
    for _ in range(60):  # ten minutes of looks every 10 s at a paused video
        asyncio.run(s.refresh())
        clock.t += 10.0
    budgets = [p.split("budget=")[1].split("&")[0] for p in runner.paths("/wda/tw/tree")]
    assert budgets.count("8000") == 2  # at the start and once LONG_MEMORY later
    assert budgets.count("3000") <= 12  # the still screen's own rule (BUDGET_MEMORY) is as in 2.0.2


# ---- off ----

def test_the_long_budget_is_parsed_like_the_normal_one_and_zero_turns_it_off(monkeypatch):
    for raw, want in (("0", 0), ("0.0", 0), ("9000", 9000), ("abc", 8000), ("-5", 8000), ("inf", 8000)):
        monkeypatch.setenv("TAPWRIGHT_TREE_LONG_BUDGET", raw)
        assert session_module._env_ms_or_off("TAPWRIGHT_TREE_LONG_BUDGET", 8000) == want, raw
    monkeypatch.delenv("TAPWRIGHT_TREE_LONG_BUDGET")
    assert session_module._env_ms_or_off("TAPWRIGHT_TREE_LONG_BUDGET", 8000) == 8000
    assert session_module.LONG_BUDGET_MS <= (session_module.READ_CUT - session_module.BUDGET_GRACE_MIN) * 1000
    for long_ms in (0, 2000, 3000):  # off, and not above the normal budget: off too
        assert not ReadBudget(long_ms=long_ms).long_on


@pytest.mark.parametrize("long_ms", [0, 2500])
def test_with_the_long_budget_off_a_still_slow_screen_reads_as_in_2_0_2(state, long_ms):
    clock = _Clock()
    runner = ShopRunner()
    s = shop(runner, clock, budgets=ReadBudget(clock=clock, long_ms=long_ms))
    tree = read(s)
    s.screen_still = lambda: False
    clock.t += 1.0
    read(s)
    assert runner.budgets() == [3000, 1500] and tree.tier == "root" and s.take_slow_note() is None


def test_notification_centers_read_gets_no_long_try_and_feeds_no_memory(state):
    runner = ShopRunner()
    s = shop(runner)
    asyncio.run(s.read_center())
    assert runner.budgets() == [3000] and not s.budgets.slow(SHOP) and s.budgets.long_try(SHOP)


# ---- the waits ----

def test_a_long_read_is_waited_for_its_budget_and_asked_again_with_as_much(state):
    runner = ShopRunner()
    s = shop(runner)
    read(s)
    assert runner.timeouts == [(3000, 8.0), (8000, session_module.read_wait(8000))]
    assert session_module.read_wait(8000) == session_module.READ_CUT >= 8.0 + session_module.BUDGET_GRACE_MIN
    seen = []
    real = s._call

    async def call(method, path, payload=None, timeout=30, again=None, **kw):
        seen.append((path.split("?")[1].split("&")[0] if "?" in path else path, timeout, kw.get("again_wait")))
        return await real(method, path, payload, timeout, again, **kw)
    s._call = call
    read(s)  # the slow app's planned long read
    assert ("budget=8000", 10.0, 10.0) in seen  # a stalled request is asked again with the long read's time, not 5 s


def test_a_stalled_long_read_asked_again_gets_its_time(state):
    from tapwright.device import ConnectionLost
    runner = ShopRunner()
    s = shop(runner)
    read(s)
    stalls = []
    real_http = runner.http

    async def http(method, path, payload=None, timeout=30.0):
        if "budget=8000" in path and not stalls:
            stalls.append(timeout)
            raise ConnectionLost("the phone didn't answer")
        if "budget=8000" in path:
            stalls.append(timeout)
        return await real_http(method, path, payload, timeout)
    runner.http = http
    tree = read(s)
    assert stalls == [10.0, 10.0] and tree.tier == "full"


def test_a_caller_that_cant_wait_for_a_long_read_gets_none(state):
    clock = _Clock()
    runner = ShopRunner()
    s = shop(runner, clock)
    read(s, timeout=9.9)  # less than read_wait(8000) is left for a long try
    assert runner.budgets() == [3000] and not s.budgets.slow(SHOP)
    read(s, timeout=60)
    assert s.budgets.slow(SHOP)
    clock.t += 1.0
    read(s, timeout=5.0)  # (wait_for's 2.0.2 cap) a slow app's read planned as before: the normal budget
    assert runner.budgets()[-1] == 3000 and s.budgets.slow(SHOP)  # (a cut normal read doesn't end it)
    read(s, timeout=10.0)
    assert runner.budgets()[-1] == 8000


def test_wait_for_gives_its_reads_room_for_the_long_budget_while_its_wait_has_it():
    s = DescribingSession()
    d = make(s)
    assert d._wait_for_read_cap(30.0) == daemon_module.WAIT_FOR_READ_MAX  # (a fake session: no ReadBudget)
    s.budgets = ReadBudget()
    room = session_module.read_wait(3000) + session_module.read_wait(8000)
    assert d._wait_for_read_cap(30.0) == room == 18.0
    assert d._wait_for_read_cap(10.0) == 10.0 and d._wait_for_read_cap(2.0) == daemon_module.WAIT_FOR_READ_MAX
    s.budgets = ReadBudget(long_ms=0)
    assert d._wait_for_read_cap(30.0) == daemon_module.WAIT_FOR_READ_MAX  # off: 2.0.2's cap


@pytest.mark.parametrize("budgets, timeout, first", [(None, 12.0, 5.0), (0, 12.0, 5.0), (8000, 12.0, 12.0),
                                                     (8000, 1.0, 5.0)])
def test_wait_for_passes_its_cap_to_the_read(budgets, timeout, first):
    from tapwright.control import Refused

    async def go():
        s = DescribingSession()
        if budgets is not None:
            s.budgets = ReadBudget(long_ms=budgets)
        d = make(s)
        stop, task = await started(d)
        s.read_timeouts.clear()
        with pytest.raises(Refused):
            await d.run_tool("a", "wait_for", {"text": "nothing like it", "timeout": timeout, "screenshot": False})
        await finish(stop, task)
        return s.read_timeouts
    assert asyncio.run(go())[0] == first


# ---- the note ----

def test_the_daemon_adds_the_note_once_to_the_answer_whose_read_took_long():
    async def go():
        s = DescribingSession()
        notes = [session_module.LONG_NOTE.format(seconds=4.1)]
        s.take_slow_note = lambda: notes.pop() if notes else None
        d = make(s)
        stop, task = await started(d)
        first = await d.run_tool("a", "look", {"screenshot": False})
        second = await d.run_tool("a", "look", {"screenshot": False})
        await finish(stop, task)
        return first["text"], second["text"]
    first, second = asyncio.run(go())
    assert first.count("Note: this screen took about 4 s to read") == 1
    assert "took about" not in second

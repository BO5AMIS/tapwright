"""Tapwright 2.0, worker H: latency and correctness after the phone tests (2026-11-26). The settle after a hands touch,
`back` without a needless read, no tree reads on a screen that doesn't answer (runner route version 11's `front`), the
settle cap from the first action, pictures by the runner's JPEG route, and a wording fix."""
import asyncio
import time

import pytest

from fakes import FrameFeeder  # noqa: F401  (kept for the helpers' imports below)
from test_daemon import make, on_loop_clock, run_virtual

from tapwright import daemon as daemon_module


# ---- 1. a settle after a hands touch can't end on the first flicker ----

async def _settings_push(d, began: float) -> None:
    """A tap on a Settings row as the stream shows it, from `began` (loop time): the row's highlight at 50 ms, still
    until 250 ms, the push animation from 250 to 800 ms, then still."""
    loop = asyncio.get_running_loop()
    step = 0
    while True:
        t = loop.time() - began
        if t < 0.05:
            frame = b"A"
        elif t < 0.25:
            frame = b"highlight"
        elif t < 0.8:
            step += 1
            frame = b"push%d" % step
        else:
            frame = b"General"
        d.hub.publish(frame)
        await asyncio.sleep(0.02)


def _settle_after_tap(hands: bool) -> tuple[float, bool]:
    out = {}

    async def go():
        d = make(settle_still=daemon_module.STILL, settle_quiet=daemon_module.MIN_QUIET)
        on_loop_clock(d)
        d.hub.publish(b"A")
        seq, since = d._mark()
        if hands:
            d.session.hands_at = time.monotonic()  # the hands' tap just ended (acked at once)
        feeder = asyncio.create_task(_settings_push(d, asyncio.get_running_loop().time()))
        out["done"] = await d.settle(seq, cap=2.0, since=since)
        feeder.cancel()
    run_virtual(go)
    return out["done"]


def test_a_settle_after_a_hands_tap_waits_out_the_highlight_and_ends_after_the_push():
    seconds, changed = _settle_after_tap(hands=True)
    assert changed and seconds >= 0.8 + daemon_module.STILL - 0.03  # after the animation, not on the highlight


def test_without_a_hands_touch_the_settle_still_ends_on_the_first_still_run():
    seconds, changed = _settle_after_tap(hands=False)
    # (the runner's tap answers ~200 ms after its effect: a still run then is the end, as before)
    assert changed and seconds < 0.25


def test_the_floor_doesnt_hold_a_change_that_ends_after_it():
    async def go():
        d = make(settle_still=daemon_module.STILL, settle_quiet=daemon_module.MIN_QUIET)
        on_loop_clock(d)
        d.hub.publish(b"A")
        seq, since = d._mark()
        d.session.hands_at = time.monotonic()
        loop = asyncio.get_running_loop()
        began = loop.time()

        async def feed():
            while True:
                d.hub.publish(b"A" if loop.time() - began < 0.05 else b"B")
                await asyncio.sleep(0.02)
        feeder = asyncio.create_task(feed())
        seconds, changed = await d.settle(seq, cap=2.0, since=since)
        feeder.cancel()
        # changed at 50 ms and still since: it ends at the first frame past the floor, not later
        assert changed and daemon_module.HANDS_SETTLE_MIN <= seconds < daemon_module.HANDS_SETTLE_MIN + 0.05
    run_virtual(go)


def test_no_change_after_a_hands_tap_is_still_believed_only_after_hands_quiet():
    async def go():
        d = make(settle_still=daemon_module.STILL, settle_quiet=daemon_module.MIN_QUIET)
        on_loop_clock(d)
        d.hub.publish(b"A")
        seq, since = d._mark()
        d.session.hands_at = time.monotonic()

        async def feed():
            while True:
                d.hub.publish(b"A")
                await asyncio.sleep(0.02)
        feeder = asyncio.create_task(feed())
        seconds, changed = await d.settle(seq, cap=2.0, since=since)
        feeder.cancel()
        assert not changed and seconds == pytest.approx(daemon_module.HANDS_QUIET, abs=0.03)  # (unchanged rule)
    run_virtual(go)


def test_frames_seen_during_a_hands_call_dont_end_the_settle_before_the_floor():
    async def go():
        d = make(settle_still=daemon_module.STILL, settle_first_frame=1.0)
        on_loop_clock(d)
        d.hub.publish(b"A")
        early, since = d._mark()
        for _ in range(8):  # a highlight, then still, while the call ran
            d.hub.publish(b"B")
            await asyncio.sleep(0.03)
        returned = d._mark()[0]
        d.session.hands_at = time.monotonic()
        loop = asyncio.get_running_loop()

        async def feed():
            while True:
                d.hub.publish(b"B")
                await asyncio.sleep(0.02)
        feeder = asyncio.create_task(feed())
        began = loop.time()
        seconds, changed = await d.settle(returned, cap=2.0, since=since, early=early)
        feeder.cancel()
        assert changed and seconds >= daemon_module.HANDS_SETTLE_MIN - 0.01 and loop.time() - began < 0.45
    run_virtual(go)


# ---- 6. the app another came in front of is named once ----

def test_the_stale_typing_refusal_names_the_opened_app_with_its_bundle_id_once():
    from fakes import el
    from test_daemon import finish, run, started

    from tapwright.control import Refused

    async def go():
        d = make()
        s = d.session
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": "com.example.app"})
        assert d.control.client("a").aimed_app == ("Example", "com.example.app")
        s.app = ("Spotlight", "com.apple.Spotlight")  # Spotlight came in front after the launch
        s.tree = [el("e1", "input", "Search", "search", rect=(20, 67, 259, 48))]
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "type_text", {"text": "x"})
        assert e.value.code == "stale"
        assert str(e.value).startswith("Spotlight (com.apple.Spotlight) is in front now, not Example (com.example.app), "
                                       "where your last touch went.")
        assert s.typed == []
        await finish(stop, task)
    run(go)


def test_an_app_named_only_by_its_bundle_id_is_said_once():
    assert daemon_module._app_words(("com.example.x", "com.example.x")) == "com.example.x"
    assert daemon_module._app_words(("Wayfare (com.example.w)", "com.example.w")) == "Wayfare (com.example.w)"
    assert daemon_module._bare_name("Wayfare (com.example.w)", "com.example.w") == "Wayfare"
    assert daemon_module._bare_name("Wayfare", "com.example.w") == "Wayfare"


# ---- 4. the settle cap from the first action ----

def test_one_settle_still_moving_past_the_short_cap_on_a_screen_that_doesnt_read_switches():
    from tapwright.session import SettleCap
    caps = SettleCap(short=0.5)
    # the phone, 2026-11-26: the first swipe's settle ended still after 0.77 s (the next video starting), the tree root
    assert caps.settled("tt", capped=False, cut=True, seconds=0.77) and caps.cap("tt", 2.0) == 0.5
    caps.forget()
    assert not caps.settled("tt", capped=False, cut=True, seconds=0.4)  # it stood still within the short cap
    assert not caps.settled("tt", capped=False, cut=False, seconds=0.9)  # a long animation on a screen that reads
    assert caps.cap("tt", 2.0) == 2.0


def test_the_daemon_switches_on_the_first_swipe_of_the_feed_and_a_launch_uses_the_apps_cap():
    from test_vision_200 import TIKTOK, feed_session

    from tapwright import session as session_module
    from tapwright.daemon import Settled

    async def go():
        s = feed_session()
        s.settles = session_module.SettleCap()
        s.read_app = ("TikTok", TIKTOK)
        s.last_tier = "root"
        logged = []
        d = make(s, log=logged.append)
        caps_asked, ends = [], [Settled(0.77, True, False, True), Settled(0.5, True, True), Settled(0.4, True, False, True)]

        async def frames(after_seq, still=None, cap=2.0, since=None, early=None):
            caps_asked.append(cap)
            return ends.pop(0)
        d._settle_frames = frames
        await d._settle(0, cap=2.0)  # the first swipe
        await d._settle(0, cap=2.0)  # the second: already short
        # Settings is read now; TikTok is launched again: its launch settle waits the short cap, and teaches nothing
        s.read_app, s.last_tier = ("Settings", "com.apple.Preferences"), "full"
        await d._settle(0, cap=4.0, bundle=TIKTOK)
        assert caps_asked == [2.0, 0.5, 0.5]
        assert s.settles.restless(TIKTOK) and not s.settles.restless("com.apple.Preferences")
        assert sum("never stands still" in str(x) for x in logged) == 1
    run_virtual(go)


# ---- 2. back without a needless read ----

def _listing_session():
    """A FakeSession whose reads keep their tree as `last_tree` with the bar's back button in it (as Session's do), and
    which counts back_point's reads."""
    from fakes import FakeSession

    from tapwright import ui
    from tapwright.session import Snapshot

    class Listing(FakeSession):
        def __init__(self):
            super().__init__()
            self.back_at = (20.0, 60.0)
            self.back_reads = 0
            self.last_tree = None

        def _tree(self):
            tree = ui.Tree(list(self.tree), 0, self.last_alert, self.last_keyboard, tier=self.tier,
                           back=ui.Back(True, self.back_at))
            self.last_tree = tree
            return tree

        async def look(self, screenshot=True):
            text, image = await super().look(screenshot)
            self._tree()
            return text, image

        async def snapshot(self, keyboard=False, timeout=None):
            self._record("snapshot")
            tree = self._tree()
            self.items, self.read_app = list(tree.items), self.app
            self.last_tier = self.tier
            return Snapshot(tree, self.app, 3.0)

        async def refresh(self, timeout=None):
            tree = await super().refresh(timeout)
            self.last_tree = tree
            return tree

        async def back_point(self):
            self.back_reads += 1
            return await super().back_point()
    return Listing()


def _back_after_look(between=None, frame_after=b"A", look=True):
    from fakes import FrameFeeder
    from test_daemon import finish, run, started
    out = {}

    async def go():
        s = _listing_session()
        d = make(s)
        stop, task = await started(d)
        feeder = FrameFeeder(d.hub).start(then=b"A")
        await asyncio.sleep(0.1)
        await d.run_tool("a", "look", {"screenshot": False})
        if between is not None:
            await between(d)
        feeder.then = frame_after
        await asyncio.sleep(0.1)  # the stream shows the screen still for a while
        reads = s.back_reads

        def moved(*_):
            feeder.then = b"B"  # the back button pops the page
        s.taken_by = None
        real_tap = s.tap

        async def tap(target, hold_ms=60):
            moved()
            return await real_tap(target, hold_ms)
        s.tap = tap
        r = await d.run_tool("a", "back", {"look": look})
        out["reads"], out["text"], out["taps"] = s.back_reads - reads, r["text"], list(s.tap_points)
        await feeder.stop()
        await finish(stop, task)
    run(go)
    return out


def test_back_on_a_current_list_and_a_still_screen_reads_nothing_before_its_touch():
    out = _back_after_look()
    assert out["reads"] == 0 and out["text"].startswith("Tapped the back button.")
    assert out["taps"][-1] == (20.0, 60.0)


def test_back_reads_when_the_picture_changed_since_the_list():
    out = _back_after_look(frame_after=b"C")  # something moved in after the look
    assert out["reads"] == 1 and out["text"].startswith("Tapped the back button.")


def test_back_reads_when_another_read_came_after_the_agents_list():
    async def peek(d):
        await d.session.refresh()  # (another agent's read, say: the session's newest tree isn't the agent's list)
    out = _back_after_look(between=peek)
    assert out["reads"] == 1


def test_back_reads_after_an_action_with_look_false():
    async def unlooked(d):
        await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False})
    out = _back_after_look(between=unlooked)
    assert out["reads"] == 1


def test_back_with_look_false_takes_its_before_state_from_the_list_it_proved():
    out = _back_after_look(look=False)
    assert out["reads"] == 0 and out["text"].splitlines()[0] == "Tapped the back button."


# ---- 3. no tree reads on a screen that doesn't answer and keeps moving (route version 11) ----

class _Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_the_read_plan_leaves_a_read_unasked_only_on_a_moving_screen_read_moments_ago():
    from tapwright.session import ReadBudget
    clock = _Clock()
    b = ReadBudget(normal_ms=3000, short_ms=1500, memory=20.0, clock=clock, unasked_for=8.0)
    assert b.plan("tt") == (3000, False) and b.plan(None) == (3000, False)
    b.tried("tt")
    b.answered("tt", True, 3000)  # its first read was cut short
    assert b.plan("tt") == (1500, True)  # moving (no `still`), read 0 s ago
    assert b.plan("tt", still=lambda: True) == (3000, False)  # a still screen: read whole, as before
    assert b.plan("other") == (3000, False)
    clock.t += 8.0
    assert b.plan("tt") == (1500, False)  # one real short read every 8 s
    b.tried("tt")
    clock.t += 7.9
    assert b.plan("tt") == (1500, True) and b.tried_ago("tt") == pytest.approx(7.9)
    clock.t += 4.2  # 20 s since the normal read was cut: the memory is gone, a normal read goes out
    assert b.plan("tt") == (3000, False)
    b.answered("tt", False, 3000)
    b.forget()
    assert b.tried_ago("tt") is None


TT = "com.zhiliaoapp.musically"


def _v11(version=11, front=None):
    from test_vision_200 import V9Runner, _app

    class V11Runner(V9Runner):
        """V9Runner with route version 11's `front` (what it answers: `front`, an Exception is raised) and `shot`."""

        def __init__(self):
            super().__init__(version=version)
            self.front = {"b": TT, "pid": 8496, "ms": 6} if front is None else front
            self.trees = [{"token": "r1", "tree": _app(TT, "TikTok"), "tier": "root", "truncated": True,
                           "why": "full: timeout"}]

        async def http(self, method, path, payload=None, timeout=30.0):
            bare = path.replace("/session/S", "")
            if bare.startswith("/wda/tw/front"):
                self.requests.append((method, path, payload))
                if version < 11:
                    raise daemon_module.DeviceError("WebDriverAgent: unknown command: Unhandled endpoint")
                if isinstance(self.front, Exception):
                    raise self.front
                return {"value": dict(self.front)}
            if bare.startswith("/wda/activeAppInfo"):
                self.requests.append((method, path, payload))
                return {"value": {"bundleId": TT, "name": "TikTok"}}
            return await super().http(method, path, payload, timeout)
    return V11Runner()


@pytest.fixture
def state(monkeypatch):
    from tapwright import session as session_module
    monkeypatch.setattr(session_module.store, "load_state", lambda: {"runner_id": "runner"})


def _feed_reads(runner, clock=None, reads=2):
    from tapwright.session import ReadBudget, Session
    s = Session(runner)
    if clock is not None:
        s.budgets = ReadBudget(clock=clock)
    s.read_app = ("TikTok", TT)
    trees = [asyncio.run(s.refresh()) for _ in range(reads)]
    return s, trees


def test_a_moving_screen_read_moments_ago_is_answered_as_root_without_asking_for_the_tree(state):
    runner = _v11()
    s, (first, second) = _feed_reads(runner)
    assert runner.paths("/wda/tw/tree") == ["/wda/tw/tree?budget=3000&path=direct&attrs=lean"]  # the first only
    assert runner.paths("/wda/tw/front") == ["/wda/tw/front"]
    assert second.tier == "root" and second.items == [] and second.app == ("TikTok", TT)
    assert s.last_tier == "root" and s.last_why.startswith(f"not asked: {TT} is still in front")
    assert 'processId="8496"' in s.last_source and s.budget_app == TT and s.last_compact is None


def test_a_snapshot_of_the_unasked_root_asks_nothing_slow(state):
    runner = _v11()
    s, _ = _feed_reads(runner, reads=1)
    snap = asyncio.run(s.snapshot())
    assert snap.tree.tier == "root" and s.app == ("TikTok", TT)
    assert runner.paths("/wda/activeAppInfo") == [] and len(runner.paths("/wda/tw/tree")) == 1


def test_another_app_in_front_or_no_answer_or_an_older_runner_reads_as_before(state):
    for runner in (_v11(front={"b": "com.apple.Preferences", "pid": 3, "ms": 4}), _v11(front={"ms": 512}),
                   _v11(front=daemon_module.DeviceError("boom")), _v11(version=10)):
        _feed_reads(runner)
        assert len(runner.paths("/wda/tw/tree")) == 2, runner.front
        assert runner.paths("/wda/tw/tree")[1].startswith("/wda/tw/tree?budget=1500")
    runner = _v11(version=10)  # (a v10 runner is never asked which app is in front)
    _feed_reads(runner)
    assert runner.paths("/wda/tw/front") == []


def test_one_real_short_read_goes_out_every_eight_seconds_and_a_still_screen_is_read_whole(state):
    clock = _Clock()
    runner = _v11()
    s, _ = _feed_reads(runner, clock=clock, reads=2)
    assert len(runner.paths("/wda/tw/tree")) == 1
    clock.t += 8.0
    asyncio.run(s.refresh())  # 8 s on: a real short read, in case a screen of the app that reads came up
    assert runner.paths("/wda/tw/tree")[-1].startswith("/wda/tw/tree?budget=1500")
    clock.t += 1.0
    asyncio.run(s.refresh())
    assert len(runner.paths("/wda/tw/tree")) == 2  # unasked again
    s.screen_still = lambda: True  # the stream shows the screen still: the normal budget, asked
    asyncio.run(s.refresh())
    assert runner.paths("/wda/tw/tree")[-1].startswith("/wda/tw/tree?budget=3000")


def test_a_root_naming_no_app_on_a_v11_runner_asks_front_not_active_app_info(state):
    from test_vision_200 import _app
    runner = _v11()
    bare = _app(TT, "TikTok")
    del bare["b"], bare["pid"], bare["l"]
    runner.trees = [{"token": "r1", "tree": bare, "tier": "root", "truncated": True}]
    from tapwright.session import Session
    s = Session(runner)
    snap = asyncio.run(s.snapshot())
    assert snap.app == (TT, TT) and runner.paths("/wda/activeAppInfo") == []
    runner.front = {"ms": 512}  # it can't say either: the app isn't known, and activeAppInfo still isn't asked
    snap = asyncio.run(s.snapshot())
    assert snap.app is None and runner.paths("/wda/activeAppInfo") == []


def test_a_session_whose_runner_gave_the_scale_needs_no_wda_screen_read(state):
    from test_session import Recorder

    from tapwright.session import Session
    s = Session(_v11())
    asyncio.run(s.ensure())
    assert s.scale_by_runner and s.scale == 3
    asyncio.run(s.read_scale(refresh=not s.scale_by_runner))
    assert s.device.paths("/wda/screen") == []
    old = Session(Recorder())  # (Appium's build: no screen route; the connect reads /wda/screen as before)
    asyncio.run(old.ensure())
    assert not old.scale_by_runner

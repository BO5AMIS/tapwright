"""Tapwright 2.0.3: a touch at an element that acts is answered "No visible change." only once EXPECT_CHANGE_WAIT has
passed with nothing changed (phone, 2026-11-26: a React Native app started its navigation a few hundred ms after the
settle had said "nothing changed" in 0.38 s, and the note said "try once more"). A point, a scroll or a swipe keep
today's times, and a change that comes at once is not waited for."""
import asyncio
import re
import time

import pytest
from fakes import FakeSession, FrameFeeder, button
from test_daemon import finish, make, on_loop_clock, run_virtual, started

from tapwright import daemon as daemon_module
from tapwright import ui
from tapwright.daemon import Settled


class _LoopTime:
    """`time` for the daemon whose monotonic() is the running (virtual) loop's clock."""
    def __getattr__(self, name):
        return getattr(time, name)

    def monotonic(self):
        try:
            return asyncio.get_running_loop().time()
        except RuntimeError:
            return time.monotonic()


@pytest.fixture(autouse=True)
def loop_time(monkeypatch):
    # (the touch's time, the settle's and the note's seconds all on the virtual clock the settle runs on)
    monkeypatch.setattr(daemon_module, "time", _LoopTime())


def _real_daemon(s=None):
    """A daemon with the phone's settle (STILL, MIN_QUIET), so EXPECT_CHANGE_WAIT is on as it is in the service."""
    d = make(s, settle_still=daemon_module.STILL, settle_quiet=daemon_module.MIN_QUIET, settle_first_frame=1.0)
    assert d.settle_expect == daemon_module.EXPECT_CHANGE_WAIT
    return d


def _touch(args: dict, change_after: float | None, tool: str = "tap") -> dict:
    """`tool` with `args` after a look; the screen (frames and tree) changes `change_after` s after the touch went out,
    or never. Returns the answer's text, its settle seconds and the time from the call to the answer."""
    out: dict = {}

    async def go():
        s = FakeSession()
        s.tree = [button("e1", "Continue", "continue", rect=(50, 400, 200, 44)),
                  ui.Element("e2", "text", "Welcome", rect=(50, 100, 200, 30))]
        d = _real_daemon(s)
        on_loop_clock(d)
        stop, task = await started(d)
        feeder = FrameFeeder(d.hub, every=0.03).start(then=b"A")
        await asyncio.sleep(0.1)
        await d.run_tool("a", "look", {"screenshot": False})
        loop = asyncio.get_running_loop()

        async def later():
            await asyncio.sleep(change_after)
            feeder.then = b"B"  # the next screen
            s.tree = [button("e9", "Order placed", "done", rect=(50, 300, 200, 44))]

        def touched():
            if change_after is not None:
                asyncio.get_running_loop().create_task(later())
        real_tap, real_swipe = s.tap, s.swipe

        async def tap(target, hold_ms=60):
            touched()
            return await real_tap(target, hold_ms)

        async def swipe(*a, **k):
            touched()
            return await real_swipe(*a, **k)
        s.tap, s.swipe = tap, swipe
        began = loop.time()
        r = await d.run_tool("a", tool, args)
        out["took"], out["text"] = loop.time() - began, r["text"]
        settled = re.search(r"Settled in (\d+\.\d+) s", r["text"])
        out["settled"] = float(settled.group(1)) if settled else None
        await feeder.stop()
        await finish(stop, task)
    run_virtual(go)
    return out


def test_a_change_that_starts_0_7_s_after_the_tap_is_reported_as_the_change():
    out = _touch({"target": {"ref": "e1"}}, change_after=0.7)
    assert "No visible change." not in out["text"] and "Order placed" in out["text"], out["text"]
    assert "was delivered" not in out["text"]
    assert out["took"] < 1.2  # it settled on the new screen, not after the whole wait


def test_a_dead_button_answers_after_about_1_5_s_with_the_note_that_says_look_first():
    out = _touch({"target": {"id": "continue"}}, change_after=None)
    assert "No visible change." in out["text"].splitlines(), out["text"]
    assert 1.45 <= out["took"] < 1.8
    note = next(line for line in out["text"].splitlines() if "was delivered" in line)
    assert re.search(r"nothing on screen changed within 1\.[5-7] s\. ", note), note
    assert "look, or wait_for what you expect, next" in note and "try once more" not in note


def test_a_late_change_after_a_long_press_is_reported_too():
    out = _touch({"target": {"label": "Continue"}}, change_after=0.9, tool="long_press")
    assert "No visible change." not in out["text"] and "Order placed" in out["text"], out["text"]


def test_a_point_tap_keeps_todays_timing():
    out = _touch({"target": {"x": 300, "y": 700}}, change_after=None)
    assert "No visible change." in out["text"].splitlines(), out["text"]
    assert out["took"] < 0.7  # (the quiet span, MIN_QUIET: no wait for a change at a point the agent chose)


def test_a_tap_on_an_element_that_does_nothing_keeps_todays_timing():
    out = _touch({"target": {"ref": "e2"}}, change_after=None)  # a static text: no action
    assert "No visible change." in out["text"].splitlines() and out["took"] < 0.7, out


def test_a_swipe_keeps_todays_timing():
    out = _touch({"x0": 200, "y0": 700, "x1": 200, "y1": 300}, change_after=None, tool="swipe")
    assert "No visible change." in out["text"].splitlines(), out["text"]
    assert out["took"] < 1.0


def test_a_change_within_0_2_s_is_not_slower_than_before():
    out = _touch({"target": {"ref": "e1"}}, change_after=0.15)
    assert "Order placed" in out["text"] and out["took"] < 0.6, out


# ---- what counts, and what keeps its times ----

def test_only_a_tap_long_press_or_toggle_at_an_element_that_acts_expects_a_change():
    async def go():
        s = FakeSession()
        s.items = [button("e1", "Continue", "continue"), ui.Element("e2", "text", "Welcome"),
                   ui.Element("e3", "switch", "Wi-Fi", actions=("toggle",))]
        d = _real_daemon(s)
        t = ui.Target
        assert d._expects_change("tap", {"target": t(ref="e1")})
        assert d._expects_change("long_press", {"target": t(label="Continue")})
        assert d._expects_change("toggle", {"target": t(ref="e3")})
        assert not d._expects_change("tap", {"target": t(ref="e2")})  # no action
        assert not d._expects_change("tap", {"target": t(x=10.0, y=10.0)})  # a point (an o ref is one by then)
        assert not d._expects_change("tap", {"target": t(text="Not listed")})  # picture text: not in the list
        assert not d._expects_change("scroll", {"target": t(ref="e1")})
        assert not d._expects_change("swipe", {})
        token = daemon_module._PICTURE_TEXT.set(True)
        try:
            assert not d._expects_change("tap", {"target": t(ref="e1")})
        finally:
            daemon_module._PICTURE_TEXT.reset(token)
        assert make(s)._expects_change("tap", {"target": t(ref="e1")}) is False  # (a test's shortened quiet: off)
    run_virtual(go)


class _ShortCap:
    """SettleCap of an app whose screen never stands still: the short cap."""
    def cap(self, bundle, cap, cut=False, launch=False):
        return 0.5

    def settled(self, *a, **k):
        return False


def test_a_screen_that_never_stands_still_keeps_its_short_cap_and_no_wait():
    async def go():
        s = FakeSession()
        s.settles = _ShortCap()
        d = _real_daemon(s)
        asked = []

        async def frames(after_seq, still=None, cap=2.0, since=None, early=None, expect=None):
            asked.append((cap, expect))
            return Settled(0.5, True, True)
        d._settle_frames = frames
        await d._settle(0, cap=daemon_module.CAP, expect=time.monotonic() + 1.5)
        assert asked == [(0.5, None)]
    run_virtual(go)


def test_the_settle_drops_a_wait_longer_than_its_cap():
    async def go():
        d = _real_daemon()
        on_loop_clock(d)
        d.hub.publish(b"A")
        seq, since = d._mark()

        async def feed():
            while True:
                d.hub.publish(b"A")
                await asyncio.sleep(0.03)
        feeder = asyncio.create_task(feed())
        loop = asyncio.get_running_loop()
        done = await d._settle_frames(seq, cap=0.5, since=since, expect=loop.time() + 1.5)
        feeder.cancel()
        assert not done.changed and done.seconds < 0.5  # "nothing changed" as before, never a capped "changed"
    run_virtual(go)


def test_the_owed_settle_of_a_look_false_tap_on_a_button_waits_for_a_late_change():
    async def go():
        d = _real_daemon()
        on_loop_clock(d)
        loop = asyncio.get_running_loop()
        d.hub.publish(b"root")
        await asyncio.sleep(0.05)
        mark = d._mark()
        touched = loop.time()

        async def feed():
            while True:
                d.hub.publish(b"root" if loop.time() - touched < 1.2 else b"next")
                await asyncio.sleep(0.03)
        feeder = asyncio.create_task(feed())
        await asyncio.sleep(0.9)  # the next call comes 0.9 s after the touch: past HANDS_QUIET, the screen still
        d.session.hands_at = touched
        d.unsettled = True
        d.unsettled_from = (mark, daemon_module.CAP, touched, touched + daemon_module.EXPECT_CHANGE_WAIT)
        done = await d._settle_unsettled()
        feeder.cancel()
        assert done.changed and d.hub.latest.jpeg == b"next"  # not read on the old screen
    run_virtual(go)

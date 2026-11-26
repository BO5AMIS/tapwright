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

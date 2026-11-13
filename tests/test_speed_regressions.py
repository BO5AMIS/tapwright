"""1.6 speed regressions measured on the phone (2026-11-13): the read's stillness proof with a realistic stream
(about 27 frames a second, JPEG noise on a still screen, a transition that ended a short while ago), and the scope
check that is taken from the read."""
import io
import random

import pytest
from fakes import FakeSession
from PIL import Image
from test_daemon import finish, make, personal_stale, run, started, timed_hub

from tapwright import daemon as daemon_module
from tapwright import trace
from tapwright.stream import alike, Frame


def noisy(base: int, seed: int, level: int = 6) -> bytes:
    """A JPEG of a screen with `base` as its main grey, a few pixels shimmering by up to `level` levels, as a still
    screen on iOS 26 does (stream.ALIKE_TOLERANCE)."""
    rnd = random.Random(seed)
    im = Image.new("L", (160, 320), base)
    for _ in range(40):
        im.putpixel((rnd.randrange(160), rnd.randrange(320)), min(255, base + rnd.randrange(level + 1)))
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=50)
    return buf.getvalue()


PHONE = dict(settle_still=0.1, settle_quiet=0.3)  # the real thresholds (daemon.STILL, MIN_QUIET)
FRAME = 1 / 27  # balanced preset's rate while an agent works


def test_the_noise_is_within_the_alike_tolerance():
    assert alike(Frame(1, 0, noisy(120, 1)), Frame(2, 0, noisy(120, 2)))
    assert not alike(Frame(1, 0, noisy(120, 1)), Frame(2, 0, noisy(200, 2)))


def test_a_transition_that_ended_150_ms_ago_is_proven_still_at_the_real_thresholds():
    """Regression: the proof asked for the quiet window (0.3 s) that only the no-change belief needs, so a read right
    after a tap (its transition ended ~150 ms before) was never skipped: 288 ms median wait on the phone."""
    async def go():
        d = make(**PHONE)
        stop, task = await started(d)
        publish, _ = timed_hub(d)
        publish(*[noisy(60, i) for i in range(30)], every=FRAME)  # the screen before
        publish(*[noisy(60 + 30 * k, k) for k in range(1, 5)], every=FRAME)  # a transition, frame by frame
        publish(*[noisy(250, 100 + i) for i in range(6)], every=FRAME)  # then still for 6 frames (~220 ms)
        assert d._proven_still()
        await finish(stop, task)
    run(go)


def test_a_transition_that_ended_60_ms_ago_is_not_proven():
    async def go():
        d = make(**PHONE)
        stop, task = await started(d)
        publish, _ = timed_hub(d)
        publish(*[noisy(60, i) for i in range(30)], every=FRAME)
        publish(*[noisy(60 + 30 * k, k) for k in range(1, 5)], every=FRAME)
        publish(*[noisy(250, 100 + i) for i in range(2)], every=FRAME)  # ~75 ms of stillness: under the 0.1 s
        assert not d._proven_still()
        await finish(stop, task)
    run(go)


def test_a_slow_fade_is_not_proven_still():
    async def go():
        d = make(**PHONE)
        stop, task = await started(d)
        publish, _ = timed_hub(d)
        publish(*[noisy(20 + 9 * k, k) for k in range(25)], every=FRAME)  # every frame close to the last, drifting on
        assert not d._proven_still()
        await finish(stop, task)
    run(go)


def test_a_stream_at_the_still_rate_is_proven_with_two_frames_in_the_window():
    async def go():
        d = make(**PHONE)
        stop, task = await started(d)
        publish, _ = timed_hub(d)
        publish(*[noisy(90, i) for i in range(12)], every=0.2)  # 5 frames a second: gap 0.2, window 0.5 s
        assert d._proven_still()
        await finish(stop, task)
    run(go)


def test_a_read_after_a_tap_skips_the_wait_and_says_so_in_the_trace(tmp_path):
    async def go():
        trace.enable(tmp_path / "t.jsonl")
        try:
            d = make(**PHONE)
            stop, task = await started(d)
            publish, _ = timed_hub(d)
            publish(*[noisy(60, i) for i in range(20)], every=FRAME)
            publish(*[noisy(150, 50 + i) for i in range(8)], every=FRAME)
            waited: list[int] = []

            async def settle(*a, **k):
                waited.append(1)
                return daemon_module.Settled(0.0, False, False)
            d._settle = settle  # type: ignore[method-assign]
            started_trace = trace.begin("tap")
            assert started_trace is not None
            await d._silent_read(d.control.client("a"))
            assert waited == []
            rec = started_trace[0]
            assert rec.extra["still_proven"] is True and "proof_ms" in rec.extra and rec.extra["frame_gap_ms"] > 0
            trace.end(started_trace)
            await finish(stop, task)
        finally:
            trace.disable()
    run(go)


def test_the_proof_stays_cheap_with_a_full_history_of_unseen_frames():
    """Every frame compared is a decode; a proof that fails on the newest frame decodes nothing more than that."""
    async def go():
        d = make(**PHONE)
        stop, task = await started(d)
        publish, _ = timed_hub(d)
        publish(*[noisy(60, i) for i in range(60)], every=FRAME)
        publish(noisy(200, 999), every=FRAME)  # the screen just changed: the newest frame differs
        decoded: list[int] = []
        original = daemon_module.alike

        def counting(a, b):
            decoded.append(1)
            return original(a, b)
        daemon_module.alike = counting
        try:
            assert not d._proven_still()
        finally:
            daemon_module.alike = original
        assert len(decoded) == 1
        await finish(stop, task)
    run(go)


# ---- personal mode's scope check from the read (E1a saving 2): it must really skip the separate question ----

def test_a_tap_by_id_in_personal_mode_asks_the_front_app_only_once_after_the_touch():
    """Regression: the precedence in _within let the separate question through whenever the tool was a touch, so the
    read's scope check was made on top of it (+75 ms per tap): the relative test in test_daemon passed vacuously."""
    async def go():
        s, mine, d, stop, task = await personal_stale()
        s.tree_app = ("Example", "com.example.app")
        asks = s.front_asks
        await d.run_tool("a", "tap", {"target": {"id": "ok-button"}, "look": False}, scope=mine)
        assert s.tap_points
        assert s.front_asks - asks == 1  # only _taken_away's, after the touch
        await finish(stop, task)
    run(go)


def test_a_tap_whose_read_names_no_app_asks_once_before_and_once_after():
    async def go():
        s, mine, d, stop, task = await personal_stale()
        asks = s.front_asks
        await d.run_tool("a", "tap", {"target": {"id": "ok-button"}, "look": False}, scope=mine)
        assert s.front_asks - asks == 2
        await finish(stop, task)
    run(go)

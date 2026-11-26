"""Tapwright 2.0, worker I: the ordinary (readable) app path on the phone (2026-11-26). The settle a `look: false` action
owes is measured from the frame before its touch, so a push that starts late isn't read halfway."""
import asyncio
import time

from test_daemon import make, on_loop_clock, run_virtual

from tapwright import daemon as daemon_module


async def _late_push(d, began: float, start: float = 0.5, end: float = 1.0) -> None:
    """Settings' General row as the stream showed it on the phone: nothing for `start` s after the touch (the page
    loads), then the push animation until `end`, then still on the new page."""
    loop = asyncio.get_running_loop()
    step = 0
    while True:
        t = loop.time() - began
        if t < start:
            frame = b"root"
        elif t < end:
            step += 1
            frame = b"push%d" % step
        else:
            frame = b"General"
        d.hub.publish(frame)
        await asyncio.sleep(0.03)


def _daemon():
    d = make(settle_still=daemon_module.STILL, settle_quiet=daemon_module.MIN_QUIET, settle_first_frame=1.0,
             settle_fallback=0.6)
    on_loop_clock(d)
    return d


def _owed_settle(after: float, owed: bool = True) -> dict:
    """A `look: false` tap whose push starts 0.5 s after the touch; the next call settles `after` s after it returned."""
    out = {}

    async def go():
        d = _daemon()
        loop = asyncio.get_running_loop()
        d.hub.publish(b"root")
        await asyncio.sleep(0.05)
        mark = d._mark()  # the stream as the gesture went out
        began = loop.time()
        feeder = asyncio.create_task(_late_push(d, began))
        await asyncio.sleep(after)
        # (the loop's clock is virtual, time.monotonic isn't: the touch and the return are dated `after` s back)
        d.session.hands_at = time.monotonic() - after
        d.unsettled = True
        d.unsettled_from = (mark, daemon_module.CAP, time.monotonic() - after) if owed else None
        done = await d._settle_unsettled()
        out["at"], out["done"], out["frame"] = loop.time() - began, done, d.hub.latest.jpeg
        out["unsettled"], out["from"] = d.unsettled, d.unsettled_from
        feeder.cancel()
    run_virtual(go)
    return out


def test_the_owed_settle_of_a_look_false_tap_waits_for_a_push_that_starts_late():
    out = _owed_settle(after=0.1)
    # still frames of the old page before the push proved nothing: it ends on the new page, after the animation
    assert out["at"] >= 1.0 and out["frame"] == b"General" and out["done"].changed
    assert not out["unsettled"] and out["from"] is None


def test_without_the_frame_before_the_touch_the_first_still_moment_ended_it():
    # (the old behaviour, still taken when nothing was kept, a timeout's: the bug's mechanism, for the record)
    out = _owed_settle(after=0.1, owed=False)
    assert out["at"] < 0.5 and out["frame"] == b"root"


def test_an_owed_settle_long_after_the_action_on_a_still_screen_waits_nothing():
    out = _owed_settle(after=1.6)
    assert out["done"].seconds == 0.0 and out["at"] < 1.65 and out["frame"] == b"General"


def test_a_look_false_action_leaves_its_mark_and_the_next_read_settles_against_it():
    from fakes import FrameFeeder
    from test_daemon import finish, run, started

    async def go():
        d = make()
        stop, task = await started(d)
        feeder = FrameFeeder(d.hub).start(then=b"A")
        await asyncio.sleep(0.05)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False})
        assert d.unsettled and d.unsettled_from is not None
        (seq, frame), cap, returned = d.unsettled_from
        assert frame is not None and cap == daemon_module.CAP and returned <= time.monotonic()
        owed = []
        real = d._settle_unsettled

        async def counted():
            owed.append(d.unsettled_from)
            return await real()
        d._settle_unsettled = counted
        await d.run_tool("a", "tap", {"target": {"ref": "e1"}})  # resolves on a live read: the owed settle first
        assert len(owed) == 1 and owed[0] is not None and not d.unsettled and d.unsettled_from is None
        await feeder.stop()
        await finish(stop, task)
    run(go)


def test_a_silent_read_with_a_settle_owed_doesnt_skip_it_on_a_still_picture():
    async def go():
        d = _daemon()
        loop = asyncio.get_running_loop()
        d.hub.publish(b"root")
        await asyncio.sleep(0.05)
        mark = d._mark()
        began = loop.time()
        feeder = asyncio.create_task(_late_push(d, began))
        await asyncio.sleep(0.3)
        d.session.hands_at = time.monotonic() - 0.3  # the stream has shown the old page still for a while: _proven_still holds
        assert d._proven_still(noted=False)
        d.unsettled, d.unsettled_from = True, (mark, daemon_module.CAP, time.monotonic() - 0.3)
        view = d.control.client("a")
        await d._silent_read(view)
        assert loop.time() - began >= 1.0 and d.hub.latest.jpeg == b"General"
        feeder.cancel()
    run_virtual(go)


# ---- what changes at the screen's edges alone is no content ----

def _jpeg(marks=()) -> bytes:
    """A white 430x932 pt screen at 3x, with dark boxes at `marks` (points: x, y, w, h)."""
    import io

    from PIL import Image, ImageDraw
    im = Image.new("RGB", (1290, 2796), "white")
    draw = ImageDraw.Draw(im)
    for x, y, w, h in marks:
        draw.rectangle([x * 3, y * 3, (x + w) * 3, (y + h) * 3], fill="black")
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    return buf.getvalue()


def test_a_change_only_in_an_edge_strip_is_alike_on_glass_and_anywhere_else_is_not():
    from tapwright.stream import Frame, alike
    screen = (430.0, 932.0)
    plain = Frame(1, 0, _jpeg())
    indicator = Frame(2, 0, _jpeg([(425, 130, 3, 400)]))  # a scroll indicator, flashed at the right edge
    shadow = Frame(3, 0, _jpeg([(0, 190, 2, 360)]))  # the pushed-away page's shadow at the left edge
    home = Frame(4, 0, _jpeg([(140, 920, 150, 5)]))  # the home indicator, re-tinted
    row = Frame(5, 0, _jpeg([(20, 400, 390, 44)]))  # a row: content
    switch = Frame(6, 0, _jpeg([(350, 400, 51, 31)]))  # a switch near the right edge: content
    tab = Frame(7, 0, _jpeg([(200, 860, 30, 30)]))  # a tab bar icon, above the home indicator's strip
    for edge in (indicator, shadow, home):
        assert not alike(plain, edge) and daemon_module.alike_on_glass(plain, edge, screen)
    for content in (row, switch, tab):
        assert not daemon_module.alike_on_glass(plain, content, screen)
    assert not daemon_module.alike_on_glass(plain, indicator, screen, turn=90)  # landscape: as alike says
    assert not daemon_module.alike_on_glass(plain, indicator, None)  # the screen's size unknown: as alike says
    assert not daemon_module.alike_on_glass(Frame(8, 0, b"x"), Frame(9, 0, b"y"), screen)  # no pictures: as alike


def test_back_takes_its_list_when_only_the_scroll_indicator_faded_since_the_read():
    from test_daemon import run
    out = {}

    async def go():
        d = make()
        d.session.screen = (430.0, 932.0)
        d.hub.publish(_jpeg([(425, 130, 3, 400)]))
        view = d.control.client("a")
        view.read_mark = d._mark()
        d.hub.publish(_jpeg())  # the indicator faded out
        out["edge"] = d._screen_unchanged(view)
        d.hub.publish(_jpeg([(20, 400, 390, 44)]))
        out["row"] = d._screen_unchanged(view)
    run(go)
    assert out == {"edge": True, "row": False}


# ---- a swipe reads the tree only when it is a touch ----

def test_a_full_swipe_reads_nothing_before_its_drag_and_a_tiny_one_still_does():
    from test_daemon import finish, run, started

    async def go():
        d = make()
        s = d.session
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        before = s.calls.count("refresh")
        await d.run_tool("a", "swipe", {"x0": 215, "y0": 600, "x1": 215, "y1": 400, "look": False})
        assert s.calls.count("refresh") == before and "swipe" in s.calls
        await d.run_tool("a", "swipe", {"x0": 215, "y0": 600, "x1": 217, "y1": 603, "look": False})
        assert s.calls.count("refresh") > before  # (a touch where it starts: its element is judged on a fresh read)
        await finish(stop, task)
    run(go)


# ---- back waits for fresh frames rather than reading again after its list's read held the stream ----

def _back_after_a_stalled_read(resumes: bool) -> int:
    from fakes import FrameFeeder
    from test_daemon import finish, run, started
    from test_latency_paths_200 import _listing_session
    out = {}

    async def go():
        s = _listing_session()
        d = make(s)
        stop, task = await started(d)
        feeder = FrameFeeder(d.hub).start(then=b"A")
        await asyncio.sleep(0.1)
        await d.run_tool("a", "look", {"screenshot": False})
        await feeder.stop()  # the runner sends no frames while it reads: the newest is as old as the read
        await asyncio.sleep(0.5)
        assert not d._proven_still(noted=False)
        if resumes:
            async def later():
                await asyncio.sleep(0.05)
                feeder.start(then=b"A")
            asyncio.create_task(later())
        real_tap = s.tap

        async def tap(target, hold_ms=60):
            feeder.then = b"B"  # the back button pops the page
            if not resumes:
                feeder.start(then=b"B")
            return await real_tap(target, hold_ms)
        s.tap = tap
        reads = s.back_reads
        r = await d.run_tool("a", "back", {})
        assert r["text"].startswith("Tapped the back button.")
        out["reads"] = s.back_reads - reads
        await feeder.stop()
        await finish(stop, task)
    run(go)
    return out["reads"]


def test_back_waits_for_fresh_frames_to_prove_its_list_instead_of_reading_again():
    assert _back_after_a_stalled_read(resumes=True) == 0


def test_back_reads_when_no_fresh_frame_comes_within_its_wait():
    assert _back_after_a_stalled_read(resumes=False) == 1


# ---- the agent's own home press is no crash sign ----

def test_the_agents_own_home_press_asks_the_phone_nothing_about_a_crash_and_a_tap_that_lands_home_still_does():
    from test_daemon import finish, run, started
    from test_diagnostics import SPRINGBOARD, die, opened, tuned

    async def go():
        d = tuned(make())
        d.crash_polls = 1
        s = d.session
        stop, task = await started(d)
        await opened(d)
        await d.run_tool("a", "look", {"screenshot": False})
        asked = []
        real_pids = s.device.app_pids

        async def app_pids(bundle_id):
            asked.append(bundle_id)
            return await real_pids(bundle_id)
        s.device.app_pids = app_pids
        real_press = s.press_button

        async def press_button(name):
            text = await real_press(name)
            s.app, s.foreground = ("Home screen", SPRINGBOARD), SPRINGBOARD  # the app went to the background
            return text
        s.press_button = press_button
        r = await d.run_tool("a", "press_button", {"name": "home"})
        assert r["text"].startswith("pressed home") and asked == []  # (iOS's process list took 0.56 s on the phone)
        await d.run_tool("a", "open_app", {"app": "Example", "look": False})
        await d.run_tool("a", "look", {"screenshot": False})
        die(d)  # a tap the app answers by going away: that is looked into as before
        await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert asked
        await finish(stop, task)
    run(go)

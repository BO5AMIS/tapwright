"""The `filmstrip` tool (AGENT-TOOLS §14.4): frames from the live stream in one numbered image."""
import asyncio
import base64
import io
import json
import re

import pytest
from fakes import FakeSession, button, image_bytes
from PIL import Image

from tapwright import batch, client, mcp_server
from tapwright.actionlog import ActionLog
from tapwright.control import Refused
from tapwright.daemon import Daemon, Scope
from tapwright.descriptions import describe
from test_daemon import finish, make, run, started

BLUE, RED, GREEN = (0, 0, 255), (255, 0, 0), (0, 255, 0)
GAP = 8
FRAME = (80, 160)  # the stream's picture; at frame_width 100 a frame is 100x200
HEAD = re.compile(r"Filmstrip: (\d+) frames about (\d+) ms apart, numbered in order, left to right and top to bottom"
                  r"(; frame 1 is just before the tap, the rest follow it)?\.")


def reel(d: Daemon, colour=lambda: BLUE, every: float = 0.01) -> asyncio.Task:
    """A stream: a solid frame every `every` s, of the colour `colour()` gives at that moment."""
    async def feed():
        while True:
            d.hub.publish(image_bytes(FRAME, colour(), "JPEG"))
            await asyncio.sleep(every)
    return asyncio.create_task(feed())


def picture(answer: dict) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(answer["image"]))).convert("RGB")


def cell(strip: Image.Image, n: int, width: int = 100, height: int = 200) -> tuple[int, int]:
    """The top-left corner of frame n (from 1) in a strip of three columns."""
    row, col = divmod(n - 1, 3)
    return GAP + col * (width + GAP), GAP + row * (height + GAP)


def near(pixel, colour, slack: int = 60) -> bool:
    return all(abs(a - b) <= slack for a, b in zip(pixel, colour))


def centre(strip: Image.Image, n: int, width: int = 100, height: int = 200):
    x, y = cell(strip, n, width, height)
    return strip.getpixel((x + width // 2, y + height // 2))


def test_a_strip_has_the_frames_laid_out_three_to_a_row_and_numbered():
    async def go():
        d = make(shot_wait=0.5)
        stop, task = await started(d)
        feed = reel(d)
        r = await d.run_tool("a", "filmstrip", {"frames": 5, "interval_ms": 100, "frame_width": 100})
        head = HEAD.fullmatch(r["text"])
        assert head and head.group(1) == "5" and head.group(3) is None  # no tap: no "just before the tap" clause
        # three columns, two rows: 3 frames and 4 gaps across; two frames 200 high and 3 gaps down
        assert r["image_size"] == [3 * 100 + 4 * GAP, 2 * 200 + 3 * GAP] == list(picture(r).size)
        strip = picture(r)
        for n in range(1, 6):
            assert near(centre(strip, n), BLUE), n
            x, y = cell(strip, n)
            box = strip.crop((x, y, x + 30, y + 30))
            assert near(box.getpixel((1, 1)), (20, 20, 20), 40), n  # the number's box in the corner
            assert box.convert("L").getextrema()[1] > 200, n  # and white strokes of its digit
        assert near(strip.getpixel((cell(strip, 6)[0] + 50, cell(strip, 6)[1] + 100)), (255, 255, 255))  # no sixth frame
        await feed_stop(feed)
        await finish(stop, task)
    run(go)


async def feed_stop(feed: asyncio.Task) -> None:
    feed.cancel()
    await asyncio.gather(feed, return_exceptions=True)


def test_two_frames_make_a_row_of_two_and_the_smallest_size_is_allowed():
    async def go():
        d = make(shot_wait=0.5)
        stop, task = await started(d)
        feed = reel(d)
        r = await d.run_tool("a", "filmstrip", {"frames": 2, "interval_ms": 100, "frame_width": 80})
        assert r["image_size"] == [2 * 80 + 3 * GAP, 160 + 2 * GAP]
        await feed_stop(feed)
        await finish(stop, task)
    run(go)


def test_the_spacing_is_measured_from_when_the_frames_arrived():
    async def go():
        d = make(shot_wait=0.5)
        stop, task = await started(d)
        feed = reel(d, every=0.01)
        r = await d.run_tool("a", "filmstrip", {"frames": 3, "interval_ms": 200})
        assert 160 <= int(HEAD.fullmatch(r["text"]).group(2)) <= 320
        await feed_stop(feed)
        await finish(stop, task)
    run(go)


def test_the_spacing_says_what_happened_when_the_stream_is_slower_than_asked():
    async def go():
        d = make(shot_wait=1.0)  # (as in service: a frame is waited for before the phone is asked for a screenshot)
        stop, task = await started(d)
        feed = reel(d, every=0.3)  # a frame every 300 ms; 100 ms were asked for
        await asyncio.sleep(0.05)
        r = await d.run_tool("a", "filmstrip", {"frames": 3, "interval_ms": 100})
        assert int(HEAD.fullmatch(r["text"]).group(2)) >= 250
        await feed_stop(feed)
        await finish(stop, task)
    run(go)


def test_with_tap_frame_one_is_before_the_touch_and_the_rest_follow_it():
    async def go():
        d = make(shot_wait=0.5)
        stop, task = await started(d)
        feed = reel(d, lambda: RED if d.session.tap_points else BLUE)
        await asyncio.sleep(0.05)
        r = await d.run_tool("a", "filmstrip", {"frames": 4, "interval_ms": 100, "frame_width": 100,
                                                "tap": {"label": "OK"}})
        head = HEAD.fullmatch(r["text"])
        assert head and head.group(3) == "; frame 1 is just before the tap, the rest follow it"
        assert d.session.tap_points == [(60.0, 120.0)]  # one tap, the OK button's centre
        strip = picture(r)
        assert near(centre(strip, 1), BLUE)
        assert all(near(centre(strip, n), RED) for n in (2, 3, 4)), [centre(strip, n) for n in (1, 2, 3, 4)]
        await feed_stop(feed)
        await finish(stop, task)
    run(go)


def test_the_tap_goes_through_the_ordinary_tool_and_is_logged_as_a_tap(tmp_path):
    async def go():
        d = make(actions=ActionLog(tmp_path), shot_wait=0.5)
        stop, task = await started(d)
        feed = reel(d)
        await d.run_tool("a", "filmstrip", {"frames": 3, "interval_ms": 100, "tap": {"label": "OK"}})
        await feed_stop(feed)
        await finish(stop, task)
    run(go)
    lines = [json.loads(line) for f in tmp_path.glob("actions-*.jsonl") for line in f.read_text().splitlines()]
    assert [(e["tool"], e["ok"]) for e in lines] == [("tap", True), ("filmstrip", True)]
    assert lines[1]["frames"] == 3 and lines[1]["target"] == {"label": "OK"} and "frames" not in lines[0]


def test_without_a_tap_nothing_is_touched_and_the_strip_is_logged(tmp_path):
    async def go():
        d = make(actions=ActionLog(tmp_path), shot_wait=0.5)
        stop, task = await started(d)
        feed = reel(d)
        await d.run_tool("a", "filmstrip", {"frames": 2, "interval_ms": 100})
        assert d.session.tap_points == []
        await feed_stop(feed)
        await finish(stop, task)
    run(go)
    (entry,) = [json.loads(line) for f in tmp_path.glob("actions-*.jsonl") for line in f.read_text().splitlines()]
    assert entry["tool"] == "filmstrip" and entry["frames"] == 2 and "target" not in entry


def test_a_tap_the_policy_holds_back_answers_its_own_error_and_no_strip(tmp_path):
    async def go():
        s = FakeSession()
        s.tree = [button("e1", "Delete trip", "trip-delete")]
        d = make(s, actions=ActionLog(tmp_path), shot_wait=0.5)
        stop, task = await started(d)
        feed = reel(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "filmstrip", {"frames": 3, "interval_ms": 100, "tap": {"label": "Delete trip"}})
        assert e.value.code == "not_allowed" and str(e.value).startswith("needs_confirmation: ")
        assert s.tap_points == []
        await feed_stop(feed)
        await finish(stop, task)
    run(go)
    entries = [json.loads(line) for f in tmp_path.glob("actions-*.jsonl") for line in f.read_text().splitlines()]
    assert [(e["tool"], e["ok"]) for e in entries] == [("tap", False), ("filmstrip", False)]


def test_a_missing_tap_target_is_the_taps_error():
    async def go():
        d = make(shot_wait=0.5)
        stop, task = await started(d)
        feed = reel(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "filmstrip", {"tap": {"label": "Nothing like it"}, "interval_ms": 100})
        assert e.value.code == "not_found"
        await feed_stop(feed)
        await finish(stop, task)
    run(go)


def test_personal_mode_shows_the_strip_only_while_a_given_app_is_in_front():
    async def go():
        s = FakeSession()
        s.app_names = {"com.example.app": "Example", "com.example.mail": "Mail box"}
        d = make(s, shot_wait=0.5)
        stop, task = await started(d)
        feed = reel(d)
        mine = Scope(("com.example.app",))
        s.foreground = "com.example.app"
        r = await d.run_tool("a", "filmstrip", {"frames": 2, "interval_ms": 100}, scope=mine)
        assert r["image"] and HEAD.fullmatch(r["text"])
        s.foreground = "com.example.mail"  # another app in front: nothing is filmed
        shots = s.calls.count("screenshot")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "filmstrip", {"frames": 2, "interval_ms": 100}, scope=mine)
        assert e.value.code == "not_allowed" and str(e.value).startswith("outside_apps: com.example.mail is in front")
        s.foreground = "com.example.app"
        asked = []

        async def leaves():  # in front when asked first, gone to another app by the time it was filmed
            asked.append(1)
            return "com.example.app" if len(asked) == 1 else "com.example.mail"
        s.foreground_bundle_id = leaves
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "filmstrip", {"frames": 2, "interval_ms": 100}, scope=mine)
        assert e.value.code == "not_allowed" and "image" not in str(e.value) and "outside_apps" in str(e.value)
        assert s.calls.count("screenshot") == shots
        with pytest.raises(Refused) as e:  # no app list: nothing runs
            await d.run_tool("a", "filmstrip", {"frames": 2, "interval_ms": 100}, scope=Scope(()))
        assert e.value.code == "not_allowed"
        await feed_stop(feed)
        await finish(stop, task)
    run(go)


def test_an_app_that_comes_and_goes_inside_the_strip_stops_it():
    """Review 2026-11-12 (M2): the front app is watched while filming, so another app that is in front for a moment
    in the middle (and gone again by the end) never shows in the strip."""
    async def go():
        s = FakeSession()
        s.app_names = {"com.example.app": "Example", "com.example.mail": "Mail box"}
        d = make(s, shot_wait=0.5)
        stop, task = await started(d)
        feed = reel(d)
        asked = []

        async def visits():  # Mail is in front for the 3rd to 5th questions only
            asked.append(1)
            return "com.example.mail" if 3 <= len(asked) <= 5 else "com.example.app"
        s.foreground_bundle_id = visits
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "filmstrip", {"frames": 12, "interval_ms": 200}, scope=Scope(("com.example.app",)))
        assert e.value.code == "not_allowed" and "outside_apps: com.example.mail" in str(e.value)
        await feed_stop(feed)
        await finish(stop, task)
    run(go)


def test_in_personal_mode_the_tap_inside_has_its_own_checks():
    async def go():
        s = FakeSession()
        s.tree = [button("e1", "Delete trip", "trip-delete")]
        d = make(s, shot_wait=0.5)
        stop, task = await started(d)
        feed = reel(d)
        s.foreground = "com.example.app"
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "filmstrip", {"interval_ms": 100, "tap": {"label": "Delete trip"}},
                             scope=Scope(("com.example.app",)))
        assert str(e.value).startswith("needs_confirmation: ") and s.tap_points == []
        await feed_stop(feed)
        await finish(stop, task)
    run(go)


def test_without_a_stream_each_frame_is_a_screenshot_and_nothing_more_is_said():
    async def go():
        d = make()  # no feeder: the hub stays empty
        stop, task = await started(d)
        r = await d.run_tool("a", "filmstrip", {"frames": 3, "interval_ms": 100, "frame_width": 100})
        assert d.session.calls.count("screenshot") == 3
        assert HEAD.fullmatch(r["text"])  # one line: no Note
        strip = picture(r)
        assert near(centre(strip, 1, 100, 200), GREEN) and near(centre(strip, 3, 100, 200), GREEN)
        await finish(stop, task)
    run(go)


def test_a_stream_that_went_quiet_falls_back_to_screenshots():
    async def go():
        d = make()
        stop, task = await started(d)
        d.hub.publish(image_bytes(FRAME, BLUE, "JPEG"))
        d.hub.latest = type(d.hub.latest)(d.hub.latest.seq, d.hub.now_ms() - 60_000, d.hub.latest.jpeg)  # a minute old
        r = await d.run_tool("a", "filmstrip", {"frames": 2, "interval_ms": 100, "frame_width": 100})
        assert d.session.calls.count("screenshot") == 2 and near(centre(picture(r), 2), GREEN)
        await finish(stop, task)
    run(go)


@pytest.mark.parametrize("args", [
    {"frames": 1}, {"frames": 13}, {"frames": 2.5}, {"frames": True}, {"frames": "6"}, {"frames": float("nan")},
    {"interval_ms": 99}, {"interval_ms": 2001}, {"interval_ms": "fast"},
    {"frame_width": 79}, {"frame_width": 401}, {"tap": {"zzz": 1}}, {"tap": "OK"}, {"dry_run": True}])
def test_parameters_out_of_range_are_bad_params_and_nothing_is_filmed(args):
    async def go():
        d = make(shot_wait=0.5)
        stop, task = await started(d)
        feed = reel(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "filmstrip", args)
        assert e.value.code == "bad_params"
        assert "screenshot" not in d.session.calls and d.session.tap_points == []
        await feed_stop(feed)
        await finish(stop, task)
    run(go)


def test_the_bounds_name_the_range():
    async def go():
        d = make(shot_wait=0.5)
        stop, task = await started(d)
        for args, said in (({"frames": 13}, "frames must be a whole number from 2 to 12, not 13"),
                           ({"interval_ms": 50}, "interval_ms must be a whole number from 100 to 2000, not 50"),
                           ({"frame_width": 500}, "frame_width must be a whole number from 80 to 400, not 500")):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "filmstrip", args)
            assert str(e.value) == said
        await finish(stop, task)
    run(go)


def test_a_filmstrip_is_not_a_step():
    assert "filmstrip" not in batch.STEP_TOOLS
    with pytest.raises(Refused) as e:
        batch.check_steps([{"tool": "filmstrip"}])
    assert e.value.code == "bad_params" and "step 1: 'filmstrip' can't be a step; a step is one of" in str(e.value)
    assert "filmstrip" not in str(e.value).split("a step is one of", 1)[1]

    async def go():
        d = make(shot_wait=0.5)
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": [{"tool": "filmstrip", "frames": 2}]})
        assert e.value.code == "bad_params"
        await finish(stop, task)
    run(go)


def test_the_mcp_tool_carries_the_shared_description_and_the_parameters(monkeypatch):
    assert describe("filmstrip") == ("Several screenshots in one numbered image, for animations and loading states; "
                                     "cheaper than separate shots. `tap` first taps an element, then films what it "
                                     "sets off.")
    seen = []

    def fake_call(tool, args, client_id, timeout=120.0):
        seen.append((tool, args))
        return {"text": "ok", "image": None, "image_size": None}
    monkeypatch.setattr(client, "call", fake_call)
    asyncio.run(mcp_server.filmstrip())
    asyncio.run(mcp_server.filmstrip(frames=4, tap=mcp_server.Target(id="go")))
    assert seen == [("filmstrip", {"frames": 6, "interval_ms": 250, "frame_width": 200}),
                    ("filmstrip", {"frames": 4, "interval_ms": 250, "frame_width": 200, "tap": {"id": "go"}})]

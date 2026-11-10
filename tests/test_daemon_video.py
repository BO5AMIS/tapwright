"""The daemon takes its frames from the AirPlay video hub while it is live, and from the MJPEG stream otherwise."""
import asyncio
import base64
import io

from fakes import FakeSession, FakeVideo
from PIL import Image
from test_daemon import SETTLED, finish, make, run, started, until

from tapwright import daemon as daemon_module
from tapwright.stream import Picture, PictureFrame

SIZE = (800, 1600)  # not WebDriverAgent's size: the screenshot must still come out right


def screen(color: tuple[int, int, int], size=SIZE) -> Picture:
    return Picture(lambda: Image.new("RGB", size, color))


class TappingSession(FakeSession):
    """A phone whose screen changes once, right after a tap, and then stays as it is."""

    def __init__(self, video: FakeVideo, after: Picture) -> None:
        super().__init__()
        self.video, self.after = video, after

    async def tap(self, *args, **kwargs):
        result = await super().tap(*args, **kwargs)
        self.video.show(self.after)
        return result


def fed(d, video: FakeVideo) -> None:
    d.video = video
    d._start_feeder()


def test_a_tap_settles_on_the_video_frames_and_the_mjpeg_stream_is_never_opened():
    async def go():
        video = FakeVideo()
        video.show(screen((255, 0, 0)))
        d = make(TappingSession(video, screen((0, 0, 255))), settle_still=0.08, settle_quiet=0.08,
                 settle_first_frame=0.3)
        settled = []
        original = d._settle

        async def settle(*args, **kwargs):
            settled.append(await original(*args, **kwargs))
            return settled[-1]
        d._settle = settle
        fed(d, video)
        stop, task = await started(d)
        await until(lambda: d._fed_by_video() and d.hub.latest is not None)
        await d.run_tool("a", "look", {"screenshot": False})
        before = d.hub.latest.seq
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert SETTLED.search(r["text"]), r["text"]
        # the picture changed once after the tap and then stayed: the fed frames showed both, and a still run
        assert settled[-1].changed and not settled[-1].capped and 0.05 < settled[-1].seconds < 1.0
        assert d.hub.latest.seq >= before + 3
        assert d.session.device.opened == 0  # the phone was never asked for its MJPEG stream
        assert d.pump._task is not None  # the pump is held for the call, and stays closed
        assert isinstance(d.hub.latest, PictureFrame) and d.hub.latest.picture is video.current[0]
        await d.close()
        await finish(stop, task)
    run(go)


def test_an_action_that_changes_nothing_says_so_from_the_video_frames():
    async def go():
        video = FakeVideo()
        video.show(screen((255, 0, 0)))
        d = make(FakeSession(), settle_still=0.08, settle_quiet=0.08, settle_first_frame=0.3)
        fed(d, video)
        stop, task = await started(d)
        await until(lambda: d._fed_by_video() and d.hub.latest is not None)
        await d.run_tool("a", "look", {"screenshot": False})
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert "Settled in" in r["text"] and d.session.device.opened == 0
        await d.close()
        await finish(stop, task)
    run(go)


def test_a_look_with_a_screenshot_is_a_jpeg_made_from_the_picture():
    async def go():
        video = FakeVideo()
        pic = screen((255, 0, 0))
        video.show(pic)
        d = make()
        fed(d, video)
        stop, task = await started(d)
        await until(lambda: d._fed_by_video() and d.hub.latest is not None)
        r = await d.run_tool("a", "look", {"width": 400})
        shot = Image.open(io.BytesIO(base64.b64decode(r["image"])))
        assert shot.format == "JPEG" and shot.size == (400, 800) and r["image_size"] == [400, 800]
        red, green, blue = shot.convert("RGB").getpixel((200, 400))
        assert red > 200 and green < 60 and blue < 60  # the picture's colour, not the phone's /screenshot (green)
        assert pic.encoded and "screenshot" not in d.session.calls
        await d.close()
        await finish(stop, task)
    run(go)


def test_a_picture_of_another_size_than_the_phones_still_gives_the_requested_width():
    async def go():
        video = FakeVideo()
        video.show(screen((0, 255, 0), size=(1290, 2796)))
        d = make()
        fed(d, video)
        stop, task = await started(d)
        await until(lambda: d._fed_by_video() and d.hub.latest is not None)
        r = await d.run_tool("a", "look", {"width": 600, "labels": True})
        assert r["image_size"] == [600, 1300]  # the height follows the picture's own shape
        assert "labels skipped" in r["text"]  # 1290x2796 is not the fake screen's 400x800 shape
        await d.close()
        await finish(stop, task)
    run(go)


def test_the_mjpeg_stream_takes_over_when_mirroring_stops_and_video_takes_back_when_it_starts_again():
    async def go():
        said, video = [], FakeVideo()
        video.show(screen((255, 0, 0)))
        d = make(log=said.append, stream_linger=5.0)  # the pump stays up between calls, as it does for a person
        fed(d, video)
        stop, task = await started(d)
        await until(lambda: d._fed_by_video() and d.hub.latest is not None)
        await d.run_tool("a", "look", {"screenshot": False})
        assert d.session.device.opened == 0
        video.live = False
        await until(lambda: not d._fed_by_video())
        await d.run_tool("a", "look", {"screenshot": False})
        await until(lambda: d.session.device.opened > 0)  # the next action works through MJPEG as before
        video.live = True
        await until(lambda: d._fed_by_video() and d.hub.latest is not None)
        opened = d.session.device.opened
        await d.run_tool("a", "look", {"screenshot": False})
        await asyncio.sleep(0.05)
        assert d.session.device.opened == opened
        assert len([m for m in said if m.startswith("video:")]) == 3  # on, off, on
        await d.close()
        await finish(stop, task)
    run(go)


def test_pacing_does_nothing_while_video_feeds_the_hub():
    async def go():
        video = FakeVideo()
        video.show(screen((255, 0, 0)))
        d = make(pace_tick=None, pace_quiet=(0.0, 0.0, 0.0))
        stop, task = await started(d)
        d.hub.publish(b"mjpeg")
        d._fps = 30
        d.last_request = d._changed_at = -1e9
        await d._pace_once()
        assert d._fps == daemon_module.STILL_FPS  # without video a quiet screen slows the stream
        d._fps = daemon_module.STILL_FPS
        fed(d, video)
        await until(lambda: d._fed_by_video() and d.hub.latest is not None)
        settings = [c for c in d.session.calls if isinstance(c, tuple)]
        await d._pace_once()
        await d._quicken()
        assert d._fps == daemon_module.STILL_FPS and [c for c in d.session.calls if isinstance(c, tuple)] == settings
        video.live = False
        await until(lambda: not d._fed_by_video())
        await d._quicken()  # and as before once the stream is MJPEG again
        assert d._fps == 30
        await d.close()
        await finish(stop, task)
    run(go)


def test_close_stops_the_feeder():
    async def go():
        video = FakeVideo()
        video.show(screen((255, 0, 0)))
        d = make()
        fed(d, video)
        await until(lambda: d._fed_by_video() and d.hub.latest is not None)
        task = d._feeder_task
        await d.close()
        assert task.done()
    run(go)

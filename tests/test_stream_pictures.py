"""Frames made from decoded pictures (the AirPlay video): lazy JPEGs, alike without encoding, the feeder that
keeps a hub supplied from an on-change source, and a pump that stays closed while another source feeds."""
import asyncio
import io
import time

import pytest
from fakes import FakeVideo
from PIL import Image

from tapwright.stream import Frame, FrameHub, Picture, PictureFeeder, PictureFrame, PictureGone, StreamPump, alike
from test_stream import JPEGS, Chunks, mjpeg, picture as jpeg_picture


def image(spot: int = 0, square: bool = False, grey: int = 255, size=(160, 320)) -> Image.Image:
    im = Image.new("RGB", size, (grey,) * 3)
    if spot:
        im.paste((grey - spot,) * 3, (40, 40, 43, 43))
    if square:
        im.paste((0, 0, 0), (80, 160, 96, 176))
    return im


class Counting:
    """A Picture's pixel source that counts how often its image and its thumb were asked for."""

    def __init__(self, im: Image.Image) -> None:
        self.im, self.images, self.thumbs = im, 0, 0

    def image(self) -> Image.Image:
        self.images += 1
        return self.im

    def thumb(self) -> Image.Image:
        self.thumbs += 1
        return self.im.reduce(8).convert("L")

    def picture(self) -> Picture:
        return Picture(self.image, self.thumb)


def test_frames_of_the_same_picture_are_alike_without_any_encoding():
    src = Counting(image())
    pic = src.picture()
    a, b = PictureFrame(1, 0, pic), PictureFrame(2, 5, pic)
    assert alike(a, b)
    assert src.images == 0 and src.thumbs == 0 and not pic.encoded  # not even a thumb was looked at


def test_different_pictures_compare_by_their_thumbs_and_still_encode_nothing():
    calm, shimmer, drawn = Counting(image()), Counting(image(spot=85)), Counting(image(square=True))
    a, b, c = (PictureFrame(i, 0, s.picture()) for i, s in enumerate((calm, shimmer, drawn)))
    assert alike(a, b) and not alike(a, c)
    assert calm.images == shimmer.images == drawn.images == 0


def test_a_turned_picture_is_another_picture():
    assert not alike(PictureFrame(1, 0, Counting(image()).picture()),
                     PictureFrame(2, 0, Counting(image(size=(320, 160))).picture()))


def test_the_jpeg_is_made_once_and_decodes_to_the_pictures_size():
    src = Counting(image(size=(161, 321)))
    frame = PictureFrame(1, 0, src.picture())
    first = frame.jpeg
    assert frame.jpeg is first and PictureFrame(2, 0, frame.picture).jpeg is first  # also for a repeat of it
    assert src.images == 1
    assert Image.open(io.BytesIO(first)).size == (161, 321)


def test_a_picture_frame_and_a_plain_frame_of_the_same_image_are_alike():
    for size in ((160, 320), (161, 321)):  # the thumbs have the size a JPEG decodes to at 1/8 either way
        buf = io.BytesIO()
        image(size=size).save(buf, "JPEG", quality=85)
        plain = Frame(1, 0, buf.getvalue())
        assert alike(PictureFrame(2, 0, Counting(image(size=size)).picture()), plain)
        assert alike(plain, PictureFrame(2, 0, Counting(image(size=size, spot=85)).picture()))
        assert not alike(plain, PictureFrame(2, 0, Counting(image(size=size, square=True)).picture()))
    assert alike(PictureFrame(3, 0, Picture(lambda: image())), Frame(4, 0, jpeg_picture()))  # default thumb too


def test_a_picture_without_a_thumb_is_like_nothing_but_itself():
    broken = Picture(lambda: (_ for _ in ()).throw(ValueError("no picture")))
    a, b = PictureFrame(1, 0, broken), PictureFrame(2, 0, broken)
    assert alike(a, b) and not alike(a, PictureFrame(3, 0, Picture(lambda: image()))) and a.thumb is None


def test_a_picture_whose_pixels_are_let_go_keeps_what_was_made():
    src = Counting(image())
    kept, gone = src.picture(), Counting(image()).picture()
    kept.thumb(), kept.jpeg()
    kept.release(), gone.release()
    assert kept.thumb() is not None and kept.jpeg()
    with pytest.raises(PictureGone):
        gone.jpeg()


def test_the_hub_publishes_picture_frames_like_any_other():
    async def go():
        hub = FrameHub()
        pic = Counting(image()).picture()
        waiter = asyncio.create_task(hub.next_after(0, timeout=1))
        await asyncio.sleep(0)
        frame = hub.publish_frame(pic)
        assert (await waiter) is frame and isinstance(frame, PictureFrame) and frame.seq == 1
        hub.publish(b"plain")
        again = hub.publish_frame(pic)
        assert [f.seq for _, f in hub.since(0)] == [1, 2, 3] and hub.latest is again
        assert hub.fps() > 0
        assert not pic.encoded
    asyncio.run(go())


def test_a_pictures_arrival_time_is_kept_but_never_goes_back_before_an_earlier_frame():
    async def go():
        hub = FrameHub()
        hub.publish_frame(Counting(image()).picture())
        before = time.monotonic()
        await asyncio.sleep(0.05)
        arrived = time.monotonic() - 0.02
        frame = hub.publish_frame(Counting(image(square=True)).picture(), arrived)
        stamped = [t for t, f in hub.since(0) if f is frame][0]
        assert stamped == pytest.approx(arrived, abs=0.005) and hub.now_ms() - frame.at == pytest.approx(20, abs=10)
        late = hub.publish_frame(Counting(image()).picture(), before)  # claims to be older than the last frame
        assert [t for t, f in hub.since(0) if f is late][0] >= stamped
    asyncio.run(go())


# ---- the feeder ----
def feeder_for(source, hub, log=None, **kw) -> PictureFeeder:
    return PictureFeeder(source, hub, log=(log.append if log is not None else lambda m: None), **kw)


def test_the_feeder_publishes_at_a_steady_pace_when_the_picture_does_not_change():
    async def go():
        hub, video, said = FrameHub(), FakeVideo(), []
        src = Counting(image())
        pic = src.picture()
        video.show(pic)
        feeder = feeder_for(video, hub, said, rate=100)
        task = asyncio.create_task(feeder.run())
        await asyncio.sleep(0.4)
        task.cancel()
        frames = [f for _, f in hub.since(0)]
        assert len(frames) >= 10 and feeder.feeding
        assert all(f.picture is pic for f in frames) and all(alike(frames[0], f) for f in frames)
        assert [f.seq for f in frames] == list(range(1, len(frames) + 1))
        assert hub.gap() is not None and hub.gap() < 0.1  # frames keep coming: this is no stall
        assert src.images == 0 and not pic.encoded  # a still screen costs no conversion, no encode
        assert src.thumbs == 1  # the thumb was made once, for the picture
        assert len(said) == 1 and "AirPlay" in said[0]
    asyncio.run(go())


def test_a_new_picture_is_published_with_its_own_arrival_time():
    async def go():
        hub, video = FrameHub(), FakeVideo()
        feeder = feeder_for(video, hub)
        video.show(Counting(image()).picture())
        await feeder.step()
        await asyncio.sleep(0.05)
        await feeder.step()  # the same picture again
        seq = hub.latest.seq
        await asyncio.sleep(0.05)  # the picture arrives between two ticks
        arrived = time.monotonic() - 0.015
        new = Counting(image(square=True)).picture()
        video.show(new, arrived)
        await feeder.step()
        frame = hub.latest
        assert frame.seq == seq + 1 and frame.picture is new
        stamped = [t for t, f in hub.since(seq) if f is frame][0]
        assert stamped == pytest.approx(arrived, abs=0.005)
        await feeder.step()
        assert hub.latest.seq == seq + 2 and hub.latest.picture is new and not alike(hub.since(0)[0][1], hub.latest)
    asyncio.run(go())


def test_the_feeder_stops_and_the_pump_is_free_when_the_source_is_no_longer_live():
    async def go():
        hub, video, said = FrameHub(), FakeVideo(), []
        feeder = feeder_for(video, hub, said)
        pump = StreamPump(None, hub, suspended=lambda: feeder.feeding, log=lambda m: None)
        await feeder.step()
        assert not feeder.feeding  # nothing shown yet
        video.show(Counting(image()).picture())
        await feeder.step()
        assert feeder.feeding and pump.suspended()
        video.live = False
        count = hub.latest.seq
        await feeder.step()
        await feeder.step()
        assert not feeder.feeding and not pump.suspended() and hub.latest.seq == count
        video.live = True
        await feeder.step()
        assert feeder.feeding and hub.latest.seq == count + 1  # and it resumes
        assert len(said) == 3 and "JPEG stream" in said[1]  # one line per switch
    asyncio.run(go())


def test_the_feeders_task_survives_a_source_that_fails_and_runs_until_cancelled():
    async def go():
        class Flaky(FakeVideo):
            calls = 0

            def newest(self):
                Flaky.calls += 1
                if Flaky.calls == 1:
                    raise RuntimeError("boom")
                return super().newest()
        hub, video, said = FrameHub(), Flaky(), []
        video.show(Counting(image()).picture())
        task = asyncio.create_task(feeder_for(video, hub, said, rate=200, idle=0.01).run())
        await asyncio.sleep(0.2)
        task.cancel()
        assert hub.latest is not None and any("boom" in m for m in said)
    asyncio.run(go())


def test_only_the_newest_pictures_keep_their_pixels():
    async def go():
        hub, video = FrameHub(), FakeVideo()
        feeder = feeder_for(video, hub, kept=3)
        pics = [Counting(image(grey=200 + i)).picture() for i in range(5)]
        for p in pics:
            video.show(p)
            await feeder.step()
        with pytest.raises(PictureGone):
            pics[0].jpeg()
        assert pics[4].jpeg() and pics[2].jpeg()
    asyncio.run(go())


# ---- the pump ----
def test_a_suspended_pump_opens_no_stream_and_opens_one_when_it_is_free_again():
    async def go():
        hub, opened, state = FrameHub(), [], {"suspended": True}

        async def open_stream():
            opened.append(1)
            data = mjpeg(*JPEGS[:2])
            return Chunks(data[:50], data[50:])
        pump = StreamPump(open_stream, hub, linger=0.05, retry=0.01, log=lambda *a: None,
                          suspended=lambda: state["suspended"])
        async with pump.watching():
            assert pump.viewers == 1  # watchers are still counted
            await asyncio.sleep(0.25)
            assert opened == [] and hub.latest is None
            state["suspended"] = False
            assert await hub.next_after(0, timeout=1) is not None
            assert opened
        await asyncio.wait_for(pump._task, 2)
    asyncio.run(go())


def test_a_pump_closes_its_connection_when_it_becomes_suspended():
    async def go():
        hub, state, conns = FrameHub(), {"suspended": False}, []

        class Endless:
            closed = False

            async def recv_any(self, n=65536):
                await asyncio.sleep(0.005)
                return mjpeg(JPEGS[0])

            async def close(self):
                self.closed = True

        async def open_stream():
            conns.append(Endless())
            return conns[-1]
        pump = StreamPump(open_stream, hub, linger=5, retry=0.01, log=lambda *a: None,
                          suspended=lambda: state["suspended"])
        async with pump.watching():
            await hub.next_after(0, timeout=1)
            state["suspended"] = True
            await asyncio.sleep(0.1)
            assert conns[0].closed
            seq = hub.latest.seq
            await asyncio.sleep(0.1)
            assert hub.latest.seq == seq and len(conns) == 1  # nothing published, nothing reopened
        await pump.stop()
    asyncio.run(go())


def test_a_picture_that_cannot_be_read_never_takes_the_feed():
    """A review of the feeder: it called itself feeding before it had read the picture, so one unreadable picture
    kept the JPEG stream closed with nothing published, and logged 30 times a second."""
    async def go():
        def broken():
            raise RuntimeError("plane has no buffer")
        said = []
        video, hub = FakeVideo(), FrameHub()
        feeder = PictureFeeder(video, hub, log=said.append)
        video.show(Picture(lambda: image(), broken))
        for _ in range(3):
            await feeder.step()
        assert not feeder.feeding and hub.latest is None and len(said) == 1
        good = Counting(image())
        video.show(good.picture())
        await feeder.step()
        assert feeder.feeding and hub.latest is not None
    asyncio.run(go())

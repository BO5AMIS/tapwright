"""The screen stream without a phone: parsing, newest-frame-wins, the on-demand pump, alike pictures."""
import asyncio
import io
import random

from PIL import Image

from tapwright.stream import PRESETS, Frame, FrameHub, MjpegParser, StreamPump, alike

JPEGS = [b"\xff\xd8" + bytes([i]) * (1000 + 37 * i) + b"\xff\xd9" for i in range(5)]


def picture(spot: int = 0, square: bool = False, grey: int = 255, size=(160, 320)) -> bytes:
    """A JPEG like a stream frame: plain `grey`, with a 3x3 px spot `spot` levels darker (the faint shimmer
    iOS 26 draws on a still screen), or a 16x16 black square (something drawn: a glyph, an icon)."""
    im = Image.new("L", size, grey)
    if spot:
        im.paste(grey - spot, (40, 40, 43, 43))
    if square:
        im.paste(0, (80, 160, 96, 176))
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=85)
    return buf.getvalue()


def frame(jpeg: bytes) -> Frame:
    return Frame(1, 0, jpeg)


def test_a_faint_flicker_is_the_same_picture():
    """On iOS 26 a still screen is rarely byte-identical twice: a few pixels shimmer (2026-11-09, the lab)."""
    assert alike(frame(picture()), frame(picture(spot=85)))


def test_something_drawn_is_another_picture():
    assert not alike(frame(picture()), frame(picture(square=True)))


def test_a_turned_screen_is_another_picture():
    assert not alike(frame(picture()), frame(picture(size=(320, 160))))


def test_frames_that_are_no_pictures_compare_by_their_bytes():
    assert alike(frame(b"A"), frame(b"A")) and not alike(frame(b"A"), frame(b"B"))
    assert not alike(frame(b"A"), frame(picture()))


def test_no_frame_is_like_no_other():
    assert not alike(None, frame(b"A")) and not alike(frame(b"A"), None) and not alike(None, None)


def mjpeg(*jpegs: bytes) -> bytes:
    """Shaped like WebDriverAgent's MJPEG server output."""
    head = b"HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=--BoundaryString\r\n\r\n"
    return head + b"".join(b"--BoundaryString\r\nContent-type: image/jpg\r\nContent-Length: %d\r\n\r\n" % len(j)
                           + j + b"\r\n\r\n" for j in jpegs)


def test_parser_finds_every_frame_wherever_the_stream_splits():
    data = mjpeg(*JPEGS)
    for seed in range(25):
        cuts = sorted(random.Random(seed).sample(range(1, len(data)), 12))
        parser, out = MjpegParser(), []
        for a, b in zip([0] + cuts, cuts + [len(data)]):
            out += parser.feed(data[a:b])
        assert out == JPEGS


def test_hub_hands_out_the_newest_frame():
    async def go():
        hub = FrameHub()
        for j in JPEGS:
            hub.publish(j)
        frame = await hub.next_after(0, timeout=0.1)
        assert frame.seq == 5 and frame.jpeg == JPEGS[-1]  # a late viewer skips straight to the newest
        assert await hub.next_after(5, timeout=0.05) is None  # nothing newer yet
        waiter = asyncio.create_task(hub.next_after(5, timeout=1))
        await asyncio.sleep(0)
        hub.publish(b"\xff\xd8next")
        assert (await waiter).seq == 6
        hub.close()
        assert await hub.next_after(6, timeout=1) is None
        assert hub.fps() > 0
    asyncio.run(go())


class Chunks:
    """A stream connection that delivers some chunks, then closes."""

    def __init__(self, *chunks: bytes):
        self.chunks = list(chunks)

    async def recv_any(self, n=65536):
        return self.chunks.pop(0) if self.chunks else b""

    async def close(self):
        pass


def test_pump_reads_reopens_and_stops_when_nobody_watches():
    async def go():
        hub, opened = FrameHub(), []

        async def open_stream():
            opened.append(1)
            data = mjpeg(*JPEGS[:2])
            return Chunks(data[:50], data[50:])
        pump = StreamPump(open_stream, hub, linger=0.05, retry=0.01, log=lambda *a: None)
        async with pump.watching():
            assert await hub.next_after(0, timeout=1) is not None
            for _ in range(100):
                if len(opened) >= 2:
                    break
                await asyncio.sleep(0.01)
        assert len(opened) >= 2  # the stream ended and was opened again
        await asyncio.wait_for(pump._task, 2)  # nobody watching any more: the connection is gone
    asyncio.run(go())


class Silent:
    """A stream connection that never delivers anything."""

    async def recv_any(self, n=65536):
        await asyncio.sleep(3600)

    async def close(self):
        pass


def test_pump_survives_failing_and_stalled_opens():
    async def go():
        hub, calls = FrameHub(), []

        async def open_stream():
            calls.append(1)
            if len(calls) <= 2:
                raise OSError("phone not ready")
            return Silent()
        pump = StreamPump(open_stream, hub, linger=0.05, retry=0.01, stall=0.05, log=lambda *a: None)
        async with pump.watching():
            for _ in range(200):
                if len(calls) >= 4:
                    break
                await asyncio.sleep(0.01)
            assert len(calls) >= 4  # failed opens and a stalled stream both lead to a reopen
        await asyncio.wait_for(pump._task, 2)
    asyncio.run(go())


def test_stop_cancels_the_pump():
    async def go():
        async def open_stream():
            return Silent()
        pump = StreamPump(open_stream, FrameHub(), log=lambda *a: None)
        async with pump.watching():
            await asyncio.sleep(0.05)
            await pump.stop()
            assert pump._task.done()
    asyncio.run(go())


def test_presets_are_wda_settings():
    assert set(PRESETS) == {"sharp", "balanced", "light"}
    assert PRESETS["balanced"] == {"mjpegScalingFactor": 100, "mjpegFixOrientation": False,
                                   "mjpegServerScreenshotQuality": 50, "mjpegServerFramerate": 30}


def test_presets_never_make_the_phone_redraw_frames():
    """Resizing or rotating frames on the phone costs WebDriverAgent's runner 58-104% of a core,
    and iOS kills it above 80% over a minute; frames sent as captured cost about 15% (measured)."""
    for preset in PRESETS.values():
        assert preset["mjpegScalingFactor"] == 100 and preset["mjpegFixOrientation"] is False


def test_a_landscape_frame_is_turned_upright():
    """In landscape WebDriverAgent's frames stay as captured (1290x2796): turned here, they are as wide as tall
    the other way round, and the turn goes the asked way."""
    import io

    from PIL import Image

    from tapwright.stream import turn_jpeg
    img = Image.new("RGB", (40, 80), "white")
    img.paste((255, 0, 0), (0, 0, 40, 10))  # a red band along the top of the captured frame
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=95)
    for degrees, red_side in ((90, "right"), (270, "left")):
        out = Image.open(io.BytesIO(turn_jpeg(buf.getvalue(), degrees))).convert("RGB")
        assert out.size == (80, 40)
        left, right = out.getpixel((2, 20)), out.getpixel((77, 20))
        red = right if red_side == "right" else left
        assert red[0] > 200 and red[1] < 80, (degrees, left, right)

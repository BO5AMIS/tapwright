"""The screen stream without a phone: parsing, newest-frame-wins, the on-demand pump."""
import asyncio
import random

from tapwright.stream import PRESETS, FrameHub, MjpegParser, StreamPump

JPEGS = [b"\xff\xd8" + bytes([i]) * (1000 + 37 * i) + b"\xff\xd9" for i in range(5)]


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
        await asyncio.sleep(0.3)
        assert pump._task.done()  # nobody watching any more: the connection is gone
    asyncio.run(go())


def test_presets_are_wda_settings():
    assert set(PRESETS) == {"sharp", "balanced", "light"}
    assert PRESETS["balanced"] == {"mjpegScalingFactor": 50, "mjpegServerScreenshotQuality": 50,
                                   "mjpegServerFramerate": 30}

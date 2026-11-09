"""WebDriverAgent's screen stream: one MJPEG connection to the phone while anyone watches,
and the newest frame for every viewer. A viewer that falls behind skips frames; nothing
queues up."""
import asyncio
import io
import re
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import cached_property

from PIL import Image, ImageChops

# WebDriverAgent settings per quality preset (POST /session/{sid}/appium/settings).
# Frames go out as the phone captures them: full size, portrait. Resizing or rotating them on
# the phone costs WebDriverAgent's runner 58-104% of a core, and iOS kills it above 80% over a
# minute (measured on an iPhone 14 Pro Max, iOS 26.5, 2026-11-09). As captured, every preset
# costs it 9-16%. Measured over USB: sharp ~22 fps at ~250 KB a frame, balanced ~22 fps at
# ~150 KB, light ~10 fps at ~105 KB. JPEG quality barely changes the runner's load.
_AS_CAPTURED = {"mjpegScalingFactor": 100, "mjpegFixOrientation": False}
PRESETS = {
    "sharp": {**_AS_CAPTURED, "mjpegServerScreenshotQuality": 85, "mjpegServerFramerate": 30},
    "balanced": {**_AS_CAPTURED, "mjpegServerScreenshotQuality": 50, "mjpegServerFramerate": 30},
    "light": {**_AS_CAPTURED, "mjpegServerScreenshotQuality": 25, "mjpegServerFramerate": 10},
}

# XCTest waits for the app to go idle after every action (measured 0.73 s per tap, 1.20 s against 0.47 s).
# Tapwright's own stream settle replaces that wait, so it's off. Sent with every preset, never reset by one.
NO_IDLE_WAIT = {"waitForIdleTimeout": 0, "animationCoolOffTimeout": 0}

_LENGTH = re.compile(rb"content-length:[ \t]*(\d+)\r\n", re.IGNORECASE)

# Two frames show the same picture when, at 1/ALIKE_SCALE size in grey, no spot differs by more than
# ALIKE_TOLERANCE levels. On iOS 26 a still screen is rarely byte-identical twice: a few pixels shimmer, up to
# 14 levels at that size, while anything drawn (a glyph, an icon, a moved row) differs by 100 or more (the lab,
# an iPhone 14 Pro Max, 2026-11-09). Decoding at that size costs about 2.5 ms a frame.
ALIKE_SCALE = 8
ALIKE_TOLERANCE = 24


@dataclass(frozen=True)
class Frame:
    seq: int
    at: int  # epoch ms on this computer's clock, when the frame arrived (WDA gives no capture time)
    jpeg: bytes

    @cached_property
    def thumb(self) -> Image.Image | None:
        """The picture at 1/ALIKE_SCALE size in grey, decoded once; None when it is no picture."""
        try:
            im = Image.open(io.BytesIO(self.jpeg))
            im.draft("L", (im.width // ALIKE_SCALE, im.height // ALIKE_SCALE))
            return im.convert("L")
        except (OSError, ValueError, Image.DecompressionBombError):
            return None


def alike(a: Frame | None, b: Frame | None) -> bool:
    """Do two frames show the same picture (see ALIKE_TOLERANCE)? Frames that are no pictures are alike only
    when their bytes are; no frame is like no other."""
    if a is None or b is None:
        return False
    if a.jpeg == b.jpeg:
        return True
    x, y = a.thumb, b.thumb
    if x is None or y is None or x.size != y.size:
        return False
    return ImageChops.difference(x, y).getextrema()[1] <= ALIKE_TOLERANCE


class MjpegParser:
    """Cuts a multipart MJPEG byte stream into JPEGs, wherever the chunks split."""

    def __init__(self) -> None:
        self.buf = b""

    def feed(self, data: bytes) -> list[bytes]:
        self.buf += data
        frames = []
        while True:
            m = _LENGTH.search(self.buf)
            if not m:
                break
            end = self.buf.find(b"\r\n\r\n", m.start())
            if end < 0:
                break
            start, size = end + 4, int(m.group(1))
            if len(self.buf) < start + size:
                break
            frames.append(self.buf[start:start + size])
            self.buf = self.buf[start + size:]
        return frames


class FrameHub:
    """The newest frame, and a way to wait for a newer one."""

    def __init__(self, clock=time.time, mono=time.monotonic) -> None:
        self.latest: Frame | None = None
        self.closed = False
        self._clock, self._mono = clock, mono
        self._seq = 0
        self._new = asyncio.Event()
        self._arrivals: deque[float] = deque(maxlen=120)

    def publish(self, jpeg: bytes) -> Frame:
        self._seq += 1
        self.latest = Frame(self._seq, self.now_ms(), jpeg)
        self._arrivals.append(self._mono())
        self._new.set()
        self._new = asyncio.Event()
        return self.latest

    def close(self) -> None:
        self.closed = True
        self._new.set()

    async def next_after(self, seq: int, timeout: float | None = None) -> Frame | None:
        """The newest frame with a higher seq than `seq`, waiting for one if needed. None when
        nothing newer came within `timeout` seconds, or the hub is closed with nothing newer."""
        while True:
            if self.latest is not None and self.latest.seq > seq:
                return self.latest
            if self.closed:
                return None
            try:
                await asyncio.wait_for(self._new.wait(), timeout)
            except asyncio.TimeoutError:
                return None

    def now_ms(self) -> int:
        """Epoch ms on the clock that stamps `Frame.at`."""
        return int(self._clock() * 1000)

    def gap(self, recent: int = 20) -> float | None:
        """The median seconds between the last `recent` frames; None until there are a few.
        The phone sends frames continuously even when nothing changes, so a longer silence is a stall."""
        arrivals = list(self._arrivals)[-recent:]
        gaps = sorted(b - a for a, b in zip(arrivals, arrivals[1:]))
        return gaps[len(gaps) // 2] if len(gaps) >= 3 else None

    def fps(self, window: float = 2.0) -> float:
        now = self._mono()
        return round(sum(1 for t in self._arrivals if now - t <= window) / window, 1)


class StreamPump:
    """Keeps one stream connection to the phone open while anyone watches, and closes it
    `linger` seconds after the last viewer leaves (streaming costs the phone battery)."""

    def __init__(self, open_stream, hub: FrameHub, linger: float = 30.0, retry: float = 1.0,
                 stall: float = 5.0, log=print, open_timeout: float = 35.0) -> None:
        self.open_stream = open_stream  # async () -> a connection with recv_any(n) and close()
        self.hub, self.linger, self.retry, self.stall, self.log = hub, linger, retry, stall, log
        self.open_timeout = open_timeout  # opening may wait for a cold connect
        self.viewers = 0
        self._left_at = time.monotonic()
        self._task: asyncio.Task | None = None

    @asynccontextmanager
    async def watching(self):
        self.viewers += 1
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="stream")
        try:
            yield self.hub
        finally:
            self.viewers -= 1
            if self.viewers == 0:
                self._left_at = time.monotonic()

    def _unwatched(self) -> bool:
        return self.viewers == 0 and time.monotonic() - self._left_at >= self.linger

    async def _run(self) -> None:
        delay = self.retry
        while not self._unwatched():
            try:
                conn = await asyncio.wait_for(self.open_stream(), self.open_timeout)
            except Exception as e:
                self.log(f"stream: can't open it yet ({e})")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 10.0)
                continue
            parser = MjpegParser()
            try:
                while not self._unwatched():
                    chunk = await asyncio.wait_for(conn.recv_any(262144), self.stall)
                    if not chunk:
                        break
                    for jpeg in parser.feed(chunk):
                        self.hub.publish(jpeg)
                        delay = self.retry
            except asyncio.TimeoutError:
                self.log(f"stream: no frame for {self.stall:.0f} s; opening it again")
            except Exception as e:
                self.log(f"stream: dropped ({e}); opening it again")
            finally:
                try:
                    await asyncio.wait_for(conn.close(), 2)
                except Exception:
                    pass
            if self._unwatched():
                return
            await asyncio.sleep(delay)

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

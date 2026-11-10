"""WebDriverAgent's screen stream: one MJPEG connection to the phone while anyone watches,
and the newest frame for every viewer. A viewer that falls behind skips frames; nothing
queues up."""
import asyncio
import io
import re
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import cached_property
from typing import Callable

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
PICTURE_QUALITY = 80  # JPEG quality of a picture that came as video (a screenshot or a `/stream` frame)


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


class PictureGone(LookupError):
    """A Picture's pixels were let go (see Picture.release) before anyone asked for its JPEG."""


class Picture:
    """One decoded screen picture from a source that doesn't send JPEGs (the AirPlay video). The grey thumb
    that `alike` compares and the JPEG are made only when asked for, once each, and are safe to ask for from any
    thread. `make_image` gives the full picture as a PIL image; `make_thumb` may give the thumb more cheaply."""

    def __init__(self, make_image: Callable[[], Image.Image],
                 make_thumb: Callable[[], Image.Image] | None = None) -> None:
        self._make_image: Callable[[], Image.Image] | None = make_image
        self._make_thumb = make_thumb
        self._thumb: Image.Image | None = None
        self._thumbed = False
        self._jpeg: bytes | None = None
        self._thumb_lock, self._jpeg_lock = threading.Lock(), threading.Lock()  # a slow encode never holds up a thumb

    @property
    def encoded(self) -> bool:
        return self._jpeg is not None

    def thumb(self) -> Image.Image | None:
        """The picture at 1/ALIKE_SCALE size in grey (the size a JPEG decodes to at that scale), or None."""
        with self._thumb_lock:
            if not self._thumbed:
                try:
                    if self._make_thumb is not None:
                        self._thumb = self._make_thumb()
                    elif self._make_image is not None:
                        self._thumb = self._make_image().reduce(ALIKE_SCALE).convert("L")
                except (OSError, ValueError):
                    self._thumb = None
                self._thumbed = True
            return self._thumb

    def jpeg(self) -> bytes:
        with self._jpeg_lock:
            if self._jpeg is None:
                make = self._make_image
                if make is None:
                    raise PictureGone("the picture is no longer kept")
                buf = io.BytesIO()
                make().convert("RGB").save(buf, "JPEG", quality=PICTURE_QUALITY)
                self._jpeg = buf.getvalue()
            return self._jpeg

    def release(self) -> None:
        """Let the pixels go (a decoded 1290x2796 picture is 5 MB); the thumb and a JPEG already made stay."""
        self._make_image = self._make_thumb = None


class PictureFrame(Frame):
    """A frame made from a decoded Picture: it has a thumb at once, a JPEG only when something reads `jpeg`.
    Frames of the same Picture are alike without looking at a pixel."""

    def __init__(self, seq: int, at: int, picture: Picture) -> None:
        object.__setattr__(self, "seq", seq)  # Frame is frozen
        object.__setattr__(self, "at", at)
        object.__setattr__(self, "picture", picture)

    @property
    def jpeg(self) -> bytes:  # type: ignore[override]
        return self.picture.jpeg()

    @cached_property
    def thumb(self) -> Image.Image | None:  # type: ignore[override]
        return self.picture.thumb()

    def __eq__(self, other: object) -> bool:  # Frame's compares the JPEG, which would encode it
        return self is other

    __hash__ = object.__hash__

    def __repr__(self) -> str:
        return f"PictureFrame(seq={self.seq}, at={self.at})"


async def jpeg_of(frame: Frame) -> bytes:
    """The frame's JPEG; a picture that isn't encoded yet is encoded off the event loop."""
    if isinstance(frame, PictureFrame) and not frame.picture.encoded:
        return await asyncio.to_thread(lambda: frame.jpeg)
    return frame.jpeg


def alike(a: Frame | None, b: Frame | None) -> bool:
    """Do two frames show the same picture (see ALIKE_TOLERANCE)? Frames that are no pictures are alike only
    when their bytes are; no frame is like no other. Frames of one Picture are alike without any encoding."""
    if a is None or b is None:
        return False
    pa, pb = getattr(a, "picture", None), getattr(b, "picture", None)
    if pa is not None and pa is pb:
        return True
    if pa is None and pb is None and a.jpeg == b.jpeg:
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


HISTORY = 64  # recent frames kept (about 2.5 s at 25 fps), so a settle can see what happened during an action


class FrameHub:
    """The newest frame, the last few with their arrival times, and a way to wait for a newer one."""

    def __init__(self, clock=time.time, mono=time.monotonic) -> None:
        self.latest: Frame | None = None
        self.closed = False
        self._clock, self._mono = clock, mono
        self._seq = 0
        self._new = asyncio.Event()
        self._arrivals: deque[float] = deque(maxlen=120)
        self._recent: deque[tuple[float, Frame]] = deque(maxlen=HISTORY)

    def publish(self, jpeg: bytes) -> Frame:
        return self._put(lambda seq, at: Frame(seq, at, jpeg))

    def publish_frame(self, picture: Picture, arrived: float | None = None) -> PictureFrame:
        """A frame of a decoded picture (no JPEG is made until something reads it). `arrived` is when the picture
        came, on the monotonic clock; the same picture may be published again, with its arrival left out."""
        ago = 0.0 if arrived is None else max(0.0, self._mono() - arrived)
        if self._arrivals:
            ago = min(ago, max(0.0, self._mono() - self._arrivals[-1]))  # arrivals never go back in time
        return self._put(lambda seq, at: PictureFrame(seq, at, picture), ago)  # type: ignore[return-value]

    def _put(self, make: Callable[[int, int], Frame], ago: float = 0.0) -> Frame:
        self._seq += 1
        self.latest = make(self._seq, self.now_ms() - round(ago * 1000))
        arrived = self._mono() - ago
        self._arrivals.append(arrived)
        self._recent.append((arrived, self.latest))
        self._new.set()
        self._new = asyncio.Event()
        return self.latest

    def since(self, seq: int, upto: int | None = None) -> list[tuple[float, Frame]]:
        """(arrival on the monotonic clock, frame) of the kept frames after `seq`, up to `upto`, oldest first."""
        return [(t, f) for t, f in self._recent if f.seq > seq and (upto is None or f.seq <= upto)]

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


class PictureFeeder:
    """Feeds a FrameHub from a source that sends a picture only when the screen changes (the AirPlay video).
    The settle logic expects frames to keep coming while the screen is still (see FrameHub.gap), so about
    `rate` times a second this publishes the source's newest picture: a new one with its own arrival time, else
    the same one again, which `alike` knows without looking at pixels. `feeding` is true while the source is
    live; the MJPEG pump stays closed then. `source` has `.live` and `.newest()`, which gives
    (Picture, monotonic arrival of the newest decoded picture) or None."""

    def __init__(self, source, hub: FrameHub, rate: float = 30.0, idle: float = 0.25, kept: int = 12,
                 log=print) -> None:
        self.source, self.hub, self.log = source, hub, log
        self.tick, self.idle = 1.0 / rate, idle  # seconds between frames while feeding, and between checks if not
        self.feeding = False
        self._last: Picture | None = None
        self._kept: deque[Picture] = deque(maxlen=kept)  # only the newest few keep their pixels (5 MB each)

    async def run(self) -> None:
        while True:
            try:
                await self.step()
            except Exception as e:
                self.log(f"video: feeding the frames failed ({e})")
            await asyncio.sleep(self.tick if self.feeding else self.idle)

    async def step(self) -> None:
        if not self.source.live:
            if self.feeding:
                self.feeding = False
                self.log("video: mirroring stopped; frames come from the JPEG stream again")
            return
        newest = self.source.newest()
        if newest is None:
            return
        picture, arrived = newest
        if not self.feeding:
            self.feeding = True
            self.log("video: frames come from the phone's AirPlay video; the JPEG stream stays closed")
        if picture is self._last:
            self.hub.publish_frame(picture)
            return
        await asyncio.to_thread(picture.thumb)  # the grey thumb, off the event loop, before anyone compares it
        if len(self._kept) == self._kept.maxlen:
            self._kept[0].release()
        self._kept.append(picture)
        self._last = picture
        self.hub.publish_frame(picture, arrived)


class StreamPump:
    """Keeps one stream connection to the phone open while anyone watches, and closes it
    `linger` seconds after the last viewer leaves (streaming costs the phone battery). While `suspended()` is
    true another source feeds the hub: no connection is opened, and an open one is closed."""

    def __init__(self, open_stream, hub: FrameHub, linger: float = 30.0, retry: float = 1.0,
                 stall: float = 5.0, log=print, open_timeout: float = 35.0,
                 suspended: Callable[[], bool] = lambda: False) -> None:
        self.open_stream = open_stream  # async () -> a connection with recv_any(n) and close()
        self.hub, self.linger, self.retry, self.stall, self.log = hub, linger, retry, stall, log
        self.open_timeout = open_timeout  # opening may wait for a cold connect
        self.suspended = suspended
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
            if self.suspended():
                await asyncio.sleep(0.1)
                continue
            try:
                conn = await asyncio.wait_for(self.open_stream(), self.open_timeout)
            except Exception as e:
                self.log(f"stream: can't open it yet ({e})")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 10.0)
                continue
            parser = MjpegParser()
            try:
                while not self._unwatched() and not self.suspended():
                    chunk = await asyncio.wait_for(conn.recv_any(262144), self.stall)
                    if not chunk:
                        break
                    for jpeg in parser.feed(chunk):
                        if not self.suspended():  # the other source's frames must not mix with these
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

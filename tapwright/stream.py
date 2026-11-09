"""WebDriverAgent's screen stream: one MJPEG connection to the phone while anyone watches,
and the newest frame for every viewer. A viewer that falls behind skips frames; nothing
queues up."""
import asyncio
import re
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass

# WebDriverAgent settings per quality preset (POST /session/{sid}/appium/settings).
# Measured over USB on 2026-11-09: balanced ~25 fps at ~117 KB a frame, light ~27 fps at ~63 KB.
PRESETS = {
    "sharp": {"mjpegScalingFactor": 100, "mjpegServerScreenshotQuality": 50, "mjpegServerFramerate": 30},
    "balanced": {"mjpegScalingFactor": 50, "mjpegServerScreenshotQuality": 50, "mjpegServerFramerate": 30},
    "light": {"mjpegScalingFactor": 33, "mjpegServerScreenshotQuality": 25, "mjpegServerFramerate": 30},
}

_LENGTH = re.compile(rb"content-length:[ \t]*(\d+)\r\n", re.IGNORECASE)


@dataclass(frozen=True)
class Frame:
    seq: int
    at: int  # epoch ms on this computer's clock, when the frame arrived (WDA gives no capture time)
    jpeg: bytes


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

    def __init__(self, clock=time.time) -> None:
        self.latest: Frame | None = None
        self.closed = False
        self._clock = clock
        self._seq = 0
        self._new = asyncio.Event()
        self._arrivals: deque[float] = deque(maxlen=120)

    def publish(self, jpeg: bytes) -> Frame:
        self._seq += 1
        self.latest = Frame(self._seq, int(self._clock() * 1000), jpeg)
        self._arrivals.append(time.monotonic())
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

    def fps(self, window: float = 2.0) -> float:
        now = time.monotonic()
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

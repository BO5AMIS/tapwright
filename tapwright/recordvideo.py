"""`tapwright record-video`: the live view's screen as an H.264 MP4, in real time, with a sidecar of frame times.

It reads the running daemon's `GET /stream` (multipart MJPEG: each part has `X-Seq`, `X-Captured-At` in epoch ms on
this computer's clock, and `X-Coverage`) and encodes the pictures with PyAV (`pip install "tapwright[video]"`).

Timing: variable frame rate. Each picture is shown from its own capture time, so a still screen (the stream sends
nothing while the screen does not change) lasts as long as it really did, and the last picture is held until the
recording stops. `FILE.frames.json` lists every frame with its capture time in epoch ms and in the action log's ISO
UTC form, so a frame lines up with a line of ~/.tapwright/log/actions-*.jsonl.

Privacy: it records what the live view shows and nothing more. It asks the daemon to leave out the frames of the moments
Notification Center was open for a read (`?for=recording`), and refuses a daemon that does not say it does. The token
is read from the daemon's address file, goes only to a process that proved it holds it (client._prove), and is never
printed, logged, passed on a command line or written to the sidecar.

Exit codes: 0 saved, 1 nothing was saved (no daemon, no frames), 2 a mistake in the command or PyAV missing."""
from __future__ import annotations

import datetime
import http.client
import io
import json
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable

from PIL import Image

from . import video

NEED_PYAV = 'tapwright record-video needs PyAV to write the MP4: pip install "tapwright[video]"'
MAX_SECONDS = 24 * 3600.0
MAX_FPS = 60
MAX_HEAD = 64 * 1024  # bytes: a part's header block is never this long
MAX_PART = 64 * 1024 * 1024  # bytes: nor a picture
HIDES = "notification-center"  # the daemon's X-Hides answer: it leaves out the frames of a Notification Center read
NO_FRAME_NOTE = 20.0  # seconds without a first frame before a note is printed (the phone may not be connected)
TIME_BASE = Fraction(1, 1000)  # video time is in ms, as the stream's capture times are

# per encoder, for a file (the live view's own options in video.ENCODERS are for zero latency and key frames on request)
FILE_OPTIONS: dict[str, dict[str, str]] = {
    "h264_nvenc": {"preset": "p4", "rc": "vbr", "cq": "21"},
    "libx264": {"preset": "veryfast", "crf": "20"},
    "h264_mf": {},
}
GOP = 60  # frames between key frames, so a player can seek


@dataclass
class Part:
    """One picture of the stream."""
    jpeg: bytes
    seq: int | None
    at: int | None  # epoch ms
    coverage: str | None


class StreamParser:
    """The stream's parts as they come: header lines, a blank line, `Content-Length` bytes of JPEG, the next boundary."""

    def __init__(self) -> None:
        self.buf = b""

    def feed(self, chunk: bytes) -> list[Part]:
        self.buf += chunk
        parts: list[Part] = []
        while True:
            end = self.buf.find(b"\r\n\r\n")
            if end < 0:
                if len(self.buf) > MAX_HEAD:
                    raise ValueError("the stream's header block is too long")
                return parts
            fields: dict[str, str] = {}
            for line in self.buf[:end].decode("latin-1").split("\r\n"):
                if ":" in line and not line.startswith("--"):
                    k, v = line.split(":", 1)
                    fields[k.strip().lower()] = v.strip()
            start = end + 4
            try:
                size = int(fields["content-length"])
            except (KeyError, ValueError):  # a block with no length (the preamble): nothing to read in it
                self.buf = self.buf[start:]
                continue
            if size < 0 or size > MAX_PART:
                raise ValueError("a picture of the stream is too big")
            if len(self.buf) < start + size:
                return parts
            jpeg = self.buf[start:start + size]
            self.buf = self.buf[start + size:]
            parts.append(Part(jpeg, _int(fields.get("x-seq")), _int(fields.get("x-captured-at")),
                              fields.get("x-coverage")))


def _int(text: str | None) -> int | None:
    try:
        return int(text) if text is not None else None
    except ValueError:
        return None


def iso(ms: int) -> str:
    """Epoch ms as the action log writes its times: `2026-11-17T15:23:31.916Z`."""
    when = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(milliseconds=ms)
    return when.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Recorder:
    """Puts the stream's parts on the video's clock. `writer.write(jpeg, video_ms)` takes a picture at a video time and
    says whether it could be used. `fps`: the most frames written per second (None: every frame); a frame that
    comes sooner than that after the last one waits, a newer one replaces it, and it is written when the gap has passed
    (or at the end), so the final picture of a burst is never lost."""

    def __init__(self, writer, start_ms: int, fps: int | None = None) -> None:
        self.writer, self.start_ms = writer, start_ms
        self.gap = 1000 / fps if fps else 0.0
        self.entries: list[dict] = []
        self.last_ms: int | None = None
        self.pending: tuple[Part, int, int] | None = None  # part, seq, capture ms
        self.dropped = 0  # frames left out by --fps
        self.unreadable = 0  # parts that were not a picture

    def add(self, part: Part, now_ms: int) -> None:
        if part.coverage not in (None, "screen"):
            return  # only the screen's own pictures
        at = part.at if part.at is not None else now_ms
        vms = max(0, at - self.start_ms)  # a picture that was already there when the recording began starts it
        if self.last_ms is not None:
            vms = max(vms, self.last_ms + 1)  # times only go forward
            if self.gap and vms < self.last_ms + self.gap:
                if self.pending is not None:
                    self.dropped += 1
                self.pending = (part, part.seq if part.seq is not None else -1, at)
                return
        if self.pending is not None:
            self.dropped += 1  # this one is newer and a whole gap after the last
            self.pending = None
        self._write(part, at, vms)

    def tick(self, now_ms: int) -> None:
        """A waiting frame is written once its gap has passed."""
        if self.pending is not None and self.last_ms is not None:
            due = int(self.last_ms + self.gap + 0.999)
            if now_ms - self.start_ms >= due:
                part, _, at = self.pending
                self.pending = None
                self._write(part, at, due)

    def finish(self) -> None:
        if self.pending is not None and self.last_ms is not None:
            part, _, at = self.pending
            self.pending = None
            self._write(part, at, int(self.last_ms + self.gap + 0.999))

    def _write(self, part: Part, at: int, vms: int) -> None:
        if not self.writer.write(part.jpeg, vms):
            self.unreadable += 1
            return
        self.last_ms = vms
        self.entries.append({"seq": part.seq, "captured_ms": at, "captured_at": iso(at), "video_ms": vms})

    @property
    def pending_wait(self) -> bool:
        return self.pending is not None


def _fit(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """The picture at the video's size: as it is, or (a rotated screen) scaled to fit on black."""
    if image.size == size:
        return image
    scale = min(size[0] / image.width, size[1] / image.height)
    inner = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))))
    canvas = Image.new("RGB", size, (0, 0, 0))
    canvas.paste(inner, ((size[0] - inner.width) // 2, (size[1] - inner.height) // 2))
    return canvas


class Mp4Writer:
    """H.264 in an MP4 with PyAV. The size is the first picture's (cut to even numbers); a later picture of another size
    (a rotation) is fitted into it. Written to `FILE.part` and renamed when it is finished."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.part = path.with_name(path.name + ".part")
        self.container = None
        self.stream = None
        self.size: tuple[int, int] | None = None
        self.last_pts: int | None = None
        self._held = None  # the newest packet: its duration is known only when the next one (or the end) comes

    def _open(self, size: tuple[int, int]) -> None:
        import av
        enc = video.open_encoder(*size)  # proves which encoder works here, in the live view's order
        name = enc.name
        del enc
        self.container = av.open(str(self.part), "w", format="mp4")
        stream = self.container.add_stream(name, rate=30)
        ctx = stream.codec_context
        ctx.width, ctx.height, ctx.pix_fmt = size[0], size[1], "yuv420p"
        ctx.time_base = TIME_BASE
        ctx.gop_size = GOP
        ctx.max_b_frames = 0
        ctx.options = dict(FILE_OPTIONS.get(name, {}))
        self.stream, self.size = stream, size

    def write(self, jpeg: bytes, video_ms: int) -> bool:
        import av
        try:
            image = Image.open(io.BytesIO(jpeg))
            image.load()
        except (OSError, ValueError, Image.DecompressionBombError):
            return False
        image = image.convert("RGB")
        if self.size is None:
            self._open((max(2, image.width // 2 * 2), max(2, image.height // 2 * 2)))
        assert self.size is not None and self.stream is not None and self.container is not None
        picture = _fit(image, self.size)
        frame = av.VideoFrame.from_image(picture)
        frame.pts, frame.time_base = video_ms, TIME_BASE
        self._mux(self.stream.encode(frame))
        self.last_pts = video_ms
        return True

    def _mux(self, packets) -> None:
        for packet in packets:
            if self._held is not None:
                held = self._held
                if packet.pts is not None and held.pts is not None:
                    held.duration = max(1, int(packet.pts - held.pts))  # (in the stream's time base)
                self.container.mux(held)
            self._held = packet

    def close(self, end_ms: int) -> None:
        """Finish the file: the last picture lasts until `end_ms` on the video's clock."""
        if self.container is None:
            return
        try:
            self._mux(self.stream.encode(None))
            if self._held is not None:
                held = self._held
                if self.last_pts is not None and end_ms > self.last_pts:
                    seconds = (end_ms - self.last_pts) / 1000
                    held.duration = max(1, int(seconds / held.time_base)) if held.time_base else held.duration
                self.container.mux(held)
                self._held = None
        finally:
            self.container.close()
            self.container = None
        os.replace(self.part, self.path)

    def abandon(self) -> None:
        if self.container is not None:
            try:
                self.container.close()
            except Exception:  # noqa: BLE001 - already failing
                pass
            self.container = None
        try:
            self.part.unlink()
        except OSError:
            pass


def sidecar_path(out: Path) -> Path:
    return out.with_suffix(".frames.json")


def write_sidecar(out: Path, recorder: Recorder, *, started_ms: int, ended_ms: int, size: tuple[int, int] | None) -> Path:
    path = sidecar_path(out)
    data = {
        "format": 1,
        "video": out.name,
        "clock": "epoch ms on this computer's clock, the clock of ~/.tapwright/log/actions-*.jsonl; captured_at is "
                 "the same moment in that log's form",
        "started_ms": started_ms, "started_at": iso(started_ms),
        "ended_ms": ended_ms, "ended_at": iso(ended_ms),
        "duration_ms": max(0, ended_ms - started_ms),
        "frame_count": len(recorder.entries),
        "width": size[0] if size else None, "height": size[1] if size else None,
        "frames": recorder.entries,
    }
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return path


def now_ms() -> int:
    return int(time.time() * 1000)


# ---- the connection ----
class Source:
    """The daemon's stream, read by a thread that puts the parts in a queue (None at the end, an exception when it broke)."""

    def __init__(self, conn: http.client.HTTPConnection, reply: http.client.HTTPResponse) -> None:
        self.conn, self.reply = conn, reply
        self.items: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="record-video-reader", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        parser = StreamParser()
        try:
            while not self._stop.is_set():
                chunk = self.reply.read1(65536)
                if not chunk:
                    break
                for part in parser.feed(chunk):
                    self.items.put(part)
        except Exception as e:  # noqa: BLE001 - handed to the reader of the queue
            if not self._stop.is_set():
                self.items.put(e)
                return
        self.items.put(None)

    def close(self) -> None:
        self._stop.set()
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass


def connect(timeout: float = 10.0) -> Source:
    """A proved connection to the daemon's `/stream?for=recording`. Raises RuntimeError with a plain message."""
    from . import client, store
    info = store.read_serve_info()
    if not isinstance(info.get("port"), int) or not isinstance(info.get("token"), str) or not info["token"]:
        raise RuntimeError("tapwright serve isn't running (or its address file is unreadable): start it with "
                           "`tapwright serve`.")
    try:
        conn = client._prove(info, timeout)
    except client.DaemonError as e:
        raise RuntimeError(str(e)) from None
    except (OSError, http.client.HTTPException) as e:
        raise RuntimeError(f"can't reach tapwright serve ({type(e).__name__}); start it with `tapwright serve`.") from None
    try:
        conn.request("GET", "/stream?for=recording", headers={"Authorization": f"Bearer {info['token']}"})
        reply = conn.getresponse()
    except (OSError, http.client.HTTPException) as e:
        conn.close()
        raise RuntimeError(f"tapwright serve closed the stream ({type(e).__name__})") from None
    if reply.status != 200:
        conn.close()
        raise RuntimeError(f"tapwright serve refused the stream (HTTP {reply.status})")
    if reply.getheader("X-Hides") != HIDES:
        conn.close()
        raise RuntimeError("this tapwright serve can't leave Notification Center out of a recording (it is older): "
                           "stop it with `tapwright serve --stop` and start it again, then record")
    if conn.sock is not None:
        conn.sock.settimeout(30.0)
    return Source(conn, reply)


# ---- the command ----
def check_args(out: str, seconds: float | None, fps: int | None) -> tuple[Path, str | None]:
    """The output path and a mistake in the arguments (None when there is none)."""
    path = Path(out).expanduser()
    if path.suffix.lower() != ".mp4":
        return path, "--out must end in .mp4"
    if seconds is not None and not (0 < seconds <= MAX_SECONDS):
        return path, f"--seconds must be more than 0 and at most {MAX_SECONDS:g}"
    if fps is not None and not (1 <= fps <= MAX_FPS):
        return path, f"--fps must be from 1 to {MAX_FPS}"
    if path.exists():
        return path, f"{path} already exists: choose another name, or delete it"
    if sidecar_path(path).exists():
        return path, f"{sidecar_path(path)} already exists: choose another name, or delete it"
    return path, None


def run(out: str, seconds: float | None = None, fps: int | None = None, *,
        connect_fn: Callable[[], Source] = connect, writer_factory: Callable[[Path], Mp4Writer] = Mp4Writer,
        clock: Callable[[], int] = now_ms, say: Callable[[str], None] | None = None) -> int:
    say = say or (lambda text: print(text, flush=True))
    path, problem = check_args(out, seconds, fps)
    if problem:
        print(f"tapwright record-video: {problem}", file=sys.stderr)
        return 2
    if not video.available():
        print(NEED_PYAV, file=sys.stderr)
        return 2
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        source = connect_fn()
    except RuntimeError as e:
        print(f"tapwright record-video: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"tapwright record-video: can't make the folder for {path} ({e.strerror or e})", file=sys.stderr)
        return 1
    started = clock()
    writer = writer_factory(path)
    recorder = Recorder(writer, started, fps)
    source.start()
    say("Recording the live view" + (f" for {seconds:g} s" if seconds else "") + ". Press Ctrl+C to stop. "
        "It shows whatever is on the phone's screen, so use test data.")
    deadline = time.monotonic() + seconds if seconds else None
    first_note = time.monotonic() + NO_FRAME_NOTE
    note_given, failure = False, None
    try:
        while True:
            wait = 0.02 if recorder.pending_wait else 0.25
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                wait = min(wait, left)
            try:
                item = source.items.get(timeout=wait)
            except queue.Empty:
                recorder.tick(clock())
                if not note_given and not recorder.entries and time.monotonic() > first_note:
                    note_given = True
                    print("tapwright record-video: no picture yet (a still screen sends none; is the phone connected?)",
                          file=sys.stderr, flush=True)
                continue
            if item is None:
                break
            if isinstance(item, Exception):
                failure = item
                break
            recorder.add(item, clock())
            recorder.tick(clock())
    except KeyboardInterrupt:
        pass
    finally:
        source.close()
    ended = clock()
    try:
        recorder.finish()
        if not recorder.entries:
            writer.abandon()
            print("tapwright record-video: nothing was recorded (no picture came from the live view)", file=sys.stderr)
            return 1
        writer.close(max(ended - started, (recorder.last_ms or 0) + 1))
        sidecar = write_sidecar(path, recorder, started_ms=started, ended_ms=ended, size=writer.size)
    except Exception as e:  # noqa: BLE001 - a file that can't be finished is reported, never half-saved
        writer.abandon()
        print(f"tapwright record-video: couldn't finish the file ({type(e).__name__}: {e})", file=sys.stderr)
        return 1
    if failure is not None:
        print(f"tapwright record-video: the stream broke ({type(failure).__name__}); what came before is saved",
              file=sys.stderr)
    say(f"Saved {path}")
    say(f"Frame times: {sidecar}")
    say(f"Length {(ended - started) / 1000:.1f} s, {len(recorder.entries)} frames"
        + (f" ({recorder.dropped} left out by --fps)" if recorder.dropped else "") + ".")
    return 0


def main(a) -> int:
    return run(a.out, a.seconds, a.fps)

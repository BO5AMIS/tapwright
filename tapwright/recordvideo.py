"""`tapwright record-video`: the live view's screen as an H.264 MP4, in real time, with a sidecar of frame times.

It reads the running daemon's `GET /stream?for=recording` (multipart MJPEG: each part has `X-Seq`, `X-Captured-At` in
epoch ms on this computer's clock, `X-Mono-Ms` on a clock a wall-clock step can't move, and `X-Coverage`) and encodes the
pictures with PyAV (`pip install "tapwright[video]"`).

Timing: variable frame rate. Each picture is shown from its own arrival time, so a still moment lasts as long as it
really did, and the last picture is held until the recording stops. Video time follows the monotonic clock (anchored to
the wall clock once, at the first frame), so a clock step during a recording can't squeeze or stretch it. The sidecar
`FILE.frames.json` lists every frame with its wall-clock time in epoch ms and in the action log's ISO UTC form, so a
frame lines up with a line of ~/.tapwright/log/actions-*.jsonl.

Privacy: it records what the live view shows and nothing more. It asks the daemon to leave out the frames of the moments
Notification Center may have been open for a read (`?for=recording`, which fails closed: center_tool.py), and refuses a
daemon that does not say it does. The token is read from the daemon's address file, goes only to a process that proved
it holds it (client._prove), and is never printed, logged, passed on a command line or written to the sidecar.

Safety: Ctrl+C only asks the loop to stop (the signal never lands inside the encoder), the frames still queued are
encoded, and the video and the sidecar are written under temporary names and renamed together at the end. A file that
can't be finished is deleted, with the reason; one that was cut short by an error is finished with what it holds.

Exit codes: 0 saved, 1 nothing was saved (no daemon, no frames, an error), 2 a mistake in the command or PyAV missing."""
from __future__ import annotations

import datetime
import errno
import http.client
import io
import json
import os
import queue
import secrets
import signal
import socket
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
QUEUE_MAX = 60  # parts waiting for the encoder; when it falls behind the oldest are dropped (and counted)
HIDES = "notification-center"  # the daemon's X-Hides answer: it leaves out the frames of a Notification Center read
SILENCE_LIMIT = 30.0  # seconds the line may give nothing, not even a keep-alive (every 5 s), before it counts as broken
NO_FRAME_NOTE = 20.0  # seconds without a first frame before a note is printed (the phone may not be connected)
TIME_BASE = Fraction(1, 1000)  # video time is in ms, as the stream's times are

# per encoder, for a file (the live view's own options in video.ENCODERS are for zero latency and key frames on request)
FILE_OPTIONS: dict[str, dict[str, str]] = {
    "h264_nvenc": {"preset": "p4", "rc": "vbr", "cq": "21"},
    "libx264": {"preset": "veryfast", "crf": "20"},
    "h264_mf": {},
}
GOP = 60  # frames between key frames, so a player can seek


@dataclass
class Part:
    """One part of the stream: a picture, or (empty `jpeg`) a keep-alive."""
    jpeg: bytes
    seq: int | None
    at: int | None  # epoch ms, this computer's wall clock
    coverage: str | None
    mono_ms: int | None = None  # arrival on the daemon's monotonic clock
    keepalive: bool = False
    hidden: bool = False  # the daemon says it is leaving frames out (Notification Center may be open)


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
                              fields.get("x-coverage"), _int(fields.get("x-mono-ms")),
                              keepalive=fields.get("x-keepalive") == "1" or size == 0,
                              hidden=fields.get("x-hidden") == HIDES))


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
    says whether it could be used. `sink(entry)` gets each frame's sidecar entry (without one they are kept in
    `entries`). `fps`: the most frames written per second (None: every frame); a frame that comes sooner than that after
    the last one waits, a newer one replaces it, and it is written when the gap has passed (or at the end), so the final
    picture of a burst is never lost."""

    def __init__(self, writer, start_ms: int, fps: int | None = None, sink: Callable[[dict], None] | None = None,
                 start_mono_ms: int | None = None) -> None:
        self.writer, self.start_ms, self.sink = writer, start_ms, sink
        self.start_mono_ms = start_mono_ms  # this computer's monotonic clock (ms) when the recording began
        self.gap = 1000 / fps if fps else 0.0
        self.entries: list[dict] = []
        self.count = 0
        self.last_ms: int | None = None
        self.pending: tuple[Part, int, int] | None = None  # part, video ms it would have, wall ms
        self.dropped = 0  # frames left out by --fps
        self.unreadable = 0  # parts that were not a picture
        self.hidden_seen = False  # the daemon has said it left frames out

    def _video_ms(self, part: Part, now_ms: int) -> int:
        """Where the part's picture goes in the video, before the zero clamp. With the daemon's monotonic arrival it is
        measured from the recording's own monotonic start, so no step of the wall clock, before the first picture or
        after, reaches video time; without it, from the wall clock."""
        if part.mono_ms is not None and self.start_mono_ms is not None:
            return part.mono_ms - self.start_mono_ms
        return (part.at if part.at is not None else now_ms) - self.start_ms

    def add(self, part: Part, now_ms: int) -> None:
        if part.hidden:
            self.hidden_seen = True
        if part.keepalive or not part.jpeg:
            return
        if part.coverage not in (None, "screen"):
            return  # only the screen's own pictures
        vms = max(0, self._video_ms(part, now_ms))  # a picture that was already there when the recording began starts it
        if self.last_ms is not None:
            vms = max(vms, self.last_ms + 1)  # times only go forward
            if self.gap and vms < self.last_ms + self.gap:
                if self.pending is not None:
                    self.dropped += 1
                self.pending = (part, vms, 0)
                return
        if self.pending is not None:
            self.dropped += 1  # this one is newer and a whole gap after the last
            self.pending = None
        self._write(part, part.at if part.at is not None else self.start_ms + vms, vms)

    def _due(self) -> int:
        assert self.last_ms is not None
        return int(self.last_ms + self.gap + 0.999)

    def tick(self, now_ms: int) -> None:
        """A waiting frame is written once its gap has passed."""
        if self.pending is not None and self.last_ms is not None:
            due = self._due()
            if now_ms - self.start_ms >= due:
                part, _, _ = self.pending
                self.pending = None
                self._write(part, part.at if part.at is not None else self.start_ms + due, due)

    def finish(self, end_ms: int | None = None) -> None:
        """The waiting frame is written at its time, or at the end of the recording when that comes first, so it lasts
        until the end rather than 1 ms."""
        if self.pending is not None and self.last_ms is not None:
            part, _, _ = self.pending
            self.pending = None
            at = self._due()
            if end_ms is not None:
                at = min(at, end_ms)
            at = max(self.last_ms + 1, at)
            self._write(part, part.at if part.at is not None else self.start_ms + at, at)

    def _write(self, part: Part, at: int, vms: int) -> None:
        if not self.writer.write(part.jpeg, vms):
            self.unreadable += 1
            return
        self.last_ms = vms
        entry = {"seq": part.seq, "captured_ms": at, "captured_at": iso(at), "video_ms": vms}
        self.count += 1
        if self.sink is not None:
            self.sink(entry)
        else:
            self.entries.append(entry)

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


def part_name(path: Path) -> Path:
    """A temporary name of its own for this run, next to the file it becomes."""
    return path.with_name(f"{path.name}.{os.getpid()}-{secrets.token_hex(3)}.part")


class Mp4Writer:
    """H.264 in an MP4 with PyAV. The size is the first picture's (cut to even numbers); a later picture of another size
    (a rotation) is fitted into it. Written to a `.part` file; `commit()` renames it."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.part = part_name(path)
        self.container = None
        self.stream = None
        self.size: tuple[int, int] | None = None
        self.last_pts: int | None = None
        self.encoder: str | None = None
        self._held = None  # the newest packet: its duration is known only when the next one (or the end) comes

    def _open_with(self, name: str, size: tuple[int, int]) -> None:
        """One encoder, opened here with the file's own options. Raises when it can't be used for a file."""
        import av
        container = av.open(str(self.part), "w", format="mp4")
        try:
            stream = container.add_stream(name, rate=30)
            ctx = stream.codec_context
            ctx.width, ctx.height, ctx.pix_fmt = size[0], size[1], "yuv420p"
            ctx.time_base = TIME_BASE
            ctx.gop_size = GOP
            ctx.max_b_frames = 0
            ctx.options = dict(FILE_OPTIONS.get(name, {}))
            ctx.open()  # here, not at the first picture: an encoder that can't be opened is skipped
        except BaseException:
            container.close()
            raise
        self.container, self.stream, self.size, self.encoder = container, stream, size, name

    def _open(self, size: tuple[int, int]) -> None:
        errors = []
        for name in video.ENCODERS:  # the live view's order: hardware first
            try:
                self._open_with(name, size)
                return
            except Exception as e:  # noqa: BLE001 - any failure to open means: try the next one
                errors.append(f"{name}: {e}")
        raise RuntimeError("no H.264 encoder works for a file: " + "; ".join(errors))

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
                self._held = packet
                self.container.mux(held)
            else:
                self._held = packet

    def close(self, end_ms: int) -> None:
        """Finish the file: the last picture lasts until `end_ms` on the video's clock."""
        if self.container is None:
            return
        try:
            self._mux(self.stream.encode(None))
            if self._held is not None:
                held = self._held
                if self.last_pts is not None and end_ms > self.last_pts and held.time_base:
                    held.duration = max(1, int((end_ms - self.last_pts) / 1000 / held.time_base))
                self._held = None
                self.container.mux(held)
        finally:
            container, self.container = self.container, None
            container.close()

    def commit(self) -> None:
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


class Sidecar:
    """`FILE.frames.json`, written a frame at a time to a `.part` file, so a long recording keeps no list in memory."""

    CLOCK = ("epoch ms on this computer's clock, the clock of ~/.tapwright/log/actions-*.jsonl; captured_at is the "
             "same moment in that log's form; video_ms is the place in the video")

    def __init__(self, final: Path, video_name: str, started_ms: int) -> None:
        self.final, self.part, self.count = final, part_name(final), 0
        self.file = open(self.part, "w", encoding="utf-8")
        head = {"format": 1, "video": video_name, "clock": self.CLOCK, "started_ms": started_ms,
                "started_at": iso(started_ms)}
        self.file.write(json.dumps(head, indent=1)[:-2] + ',\n "frames": [')

    def add(self, entry: dict) -> None:
        self.file.write(("," if self.count else "") + "\n  " + json.dumps(entry))
        self.count += 1

    def close(self, ended_ms: int, duration_ms: int, size: tuple[int, int] | None, dropped: int = 0) -> None:
        tail = {"ended_ms": ended_ms, "ended_at": iso(ended_ms), "duration_ms": duration_ms, "frame_count": self.count,
                "frames_dropped_when_encoding_fell_behind": dropped,
                "width": size[0] if size else None, "height": size[1] if size else None}
        self.file.write("\n ],\n " + json.dumps(tail, indent=1)[2:-2].strip() + "\n}\n")
        self.file.close()

    def commit(self) -> None:
        os.replace(self.part, self.final)

    def abandon(self) -> None:
        try:
            self.file.close()
        except OSError:
            pass
        try:
            self.part.unlink()
        except OSError:
            pass


def now_ms() -> int:
    return int(time.time() * 1000)


# ---- the connection ----
class Source:
    """The daemon's stream, read by a thread that puts the parts in a bounded queue: when the encoder falls behind, the
    oldest are dropped and counted (None at the end, an exception when the line broke)."""

    def __init__(self, conn: http.client.HTTPConnection, reply: http.client.HTTPResponse,
                 sock: socket.socket | None = None) -> None:
        self.conn, self.reply, self.sock = conn, reply, sock  # (conn.sock is None once a response took the line over)
        self.items: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
        self.dropped = 0
        self._stop = threading.Event()
        self.last_data = time.monotonic()  # when the line last gave anything, a keep-alive too
        self._thread = threading.Thread(target=self._run, name="record-video-reader", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _put(self, item) -> None:
        while True:
            try:
                self.items.put_nowait(item)
                return
            except queue.Full:
                try:
                    old = self.items.get_nowait()
                except queue.Empty:
                    continue
                if isinstance(old, Part) and old.jpeg:
                    self.dropped += 1

    def _run(self) -> None:
        parser = StreamParser()
        try:
            while not self._stop.is_set():
                chunk = self.reply.read1(65536)  # (no socket timeout: see connect)
                if not chunk:
                    break
                self.last_data = time.monotonic()
                for part in parser.feed(chunk):
                    self._put(part)
        except Exception as e:  # noqa: BLE001 - handed to the reader of the queue
            if not self._stop.is_set():
                self._put(e)
                return
        self._put(None)

    def close(self) -> None:
        """Stop reading, even a read that is blocked in the reader thread. A shutdown wakes it on Linux and macOS; on
        Windows only really closing the socket does, and socket.close() waits while the response's reader still holds it
        (hence the private _real_close)."""
        self._stop.set()
        if self.sock is not None:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                getattr(self.sock, "_real_close", self.sock.close)()
            except OSError:
                pass
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
    sock = conn.sock  # (kept: a response that ends with the line closing takes it from the connection)
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
    if sock is not None:
        # No read timeout. On Python 3.10 a buffered HTTP response whose read timed out can never be read again, so a
        # timeout can't be used to poll: the daemon's keep-alives show the line is alive (Source.last_data, and the
        # silence limit in run), and closing the line stops a read that is blocked.
        sock.settimeout(None)
    return Source(conn, reply, sock)


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


def _reserve(*paths: Path) -> list[Path]:
    """Make each path exist, exclusively: of two runs with the same name, only one gets it. Raises FileExistsError."""
    made: list[Path] = []
    try:
        for p in paths:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "x"):
                pass
            made.append(p)
    except BaseException:
        for p in made:
            _unlink(p)
        raise
    return made


def _unlink(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def _why(e: BaseException) -> str:
    if isinstance(e, OSError) and e.errno == errno.ENOSPC:
        return "the disk is full"
    return f"{type(e).__name__}: {e}"


def run(out: str, seconds: float | None = None, fps: int | None = None, *,
        connect_fn: Callable[[], Source] = connect, writer_factory: Callable[[Path], Mp4Writer] = Mp4Writer,
        clock: Callable[[], int] = now_ms, mono: Callable[[], float] = time.monotonic,
        say: Callable[[str], None] | None = None) -> int:
    say = say or (lambda text: print(text, flush=True))
    path, problem = check_args(out, seconds, fps)
    if problem:
        print(f"tapwright record-video: {problem}", file=sys.stderr)
        return 2
    if not video.available():
        print(NEED_PYAV, file=sys.stderr)
        return 2
    side = sidecar_path(path)
    try:
        reserved = _reserve(path, side)
    except FileExistsError:
        print(f"tapwright record-video: {path} (or its frame list) was just made by another run: choose another name",
              file=sys.stderr)
        return 2
    except OSError as e:
        print(f"tapwright record-video: can't make {path} ({e.strerror or e})", file=sys.stderr)
        return 1
    committed = False
    writer = sidecar = source = None
    previous = None
    stop = threading.Event()
    on_main = threading.current_thread() is threading.main_thread()
    try:
        try:
            source = connect_fn()
        except RuntimeError as e:
            print(f"tapwright record-video: {e}", file=sys.stderr)
            return 1
        started, started_mono = clock(), mono()

        def elapsed_ms() -> int:
            return max(0, round((mono() - started_mono) * 1000))

        try:
            writer = writer_factory(path)
            sidecar = Sidecar(side, path.name, started)
        except OSError as e:
            print(f"tapwright record-video: can't start the files ({_why(e)}); nothing was saved", file=sys.stderr)
            return 1
        recorder = Recorder(writer, started, fps, sink=sidecar.add, start_mono_ms=round(started_mono * 1000))
        if on_main:  # Ctrl+C asks the loop to stop: it never lands inside the encoder, nor in the finishing
            previous = signal.signal(signal.SIGINT, lambda *_: stop.set())
        source.start()
        say("Recording the live view" + (f" for {seconds:g} s" if seconds else "") + ". Press Ctrl+C to stop. "
            "It shows whatever is on the phone's screen, so use test data.")
        first_note = mono() + NO_FRAME_NOTE
        told_hidden = told_none = False
        failure: BaseException | None = None
        try:
            while not stop.is_set():
                wait = 0.02 if recorder.pending_wait else 0.1
                if seconds:
                    left = seconds - elapsed_ms() / 1000
                    if left <= 0:
                        break
                    wait = min(wait, left)
                try:
                    item = source.items.get(timeout=wait)
                except queue.Empty:
                    recorder.tick(started + elapsed_ms())
                    heard = getattr(source, "last_data", None)
                    if heard is not None and time.monotonic() - heard > SILENCE_LIMIT:
                        failure = TimeoutError("the live view's stream went silent")
                        break
                    if not told_none and not recorder.count and mono() > first_note:
                        told_none = True
                        print("tapwright record-video: no picture yet (is the phone connected, and the screen shown?)",
                              file=sys.stderr, flush=True)
                    continue
                if item is None:
                    break
                if isinstance(item, Exception):
                    failure = item
                    break
                recorder.add(item, clock())
                recorder.tick(started + elapsed_ms())
                if recorder.hidden_seen and not told_hidden:
                    told_hidden = True
                    print("tapwright record-video: the live view is leaving frames out (Notification Center may be "
                          "open); the video holds the last picture until it is shown again", file=sys.stderr, flush=True)
        except KeyboardInterrupt:  # (a thread that can't take the signal handler)
            pass
        except Exception as e:  # noqa: BLE001 - an encoder or disk error: finish what is written
            failure = e
        source.close()
        ended = clock()
        end_ms = elapsed_ms()
        try:
            while True:  # the parts still queued arrived before the stop: they go in the video too
                try:
                    item = source.items.get_nowait()
                except queue.Empty:
                    break
                if isinstance(item, Part) and failure is None:
                    recorder.add(item, ended)
            recorder.finish(end_ms)
            if not recorder.count:
                print("tapwright record-video: nothing was recorded"
                      + (f" ({_why(failure)})" if failure is not None else " (no picture came from the live view)"),
                      file=sys.stderr)
                return 1
            end_ms = max(end_ms, (recorder.last_ms or 0) + 1)
            writer.close(end_ms)
            sidecar.close(ended, end_ms, writer.size, source.dropped)
            writer.commit()
            sidecar.commit()
            committed = True
        except Exception as e:  # noqa: BLE001 - a file that can't be finished is deleted, never half-saved
            print(f"tapwright record-video: couldn't finish the file ({_why(e)}); nothing was saved", file=sys.stderr)
            return 1
        if failure is not None:
            print(f"tapwright record-video: stopped early ({_why(failure)}); what came before is saved", file=sys.stderr)
        say(f"Saved {path}")
        say(f"Frame times: {side}")
        say(f"Length {end_ms / 1000:.1f} s, {recorder.count} frames"
            + (f" ({recorder.dropped} left out by --fps)" if recorder.dropped else "")
            + (f" ({source.dropped} dropped because encoding fell behind)" if source.dropped else "") + ".")
        return 0
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)
        if source is not None:
            source.close()
        if not committed:
            for part in (writer, sidecar):
                if part is not None:
                    part.abandon()
            for p in reserved:
                _unlink(p)


def main(a) -> int:
    return run(a.out, a.seconds, a.fps)

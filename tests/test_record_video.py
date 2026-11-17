"""`tapwright record-video` (1.7.7): the stream's parts, their times on the video's clock, the MP4 and its sidecar, the
stop after --seconds and on Ctrl+C, no token anywhere, no PyAV, and no frame from while Notification Center was open."""
import errno
import hashlib
import hmac
import io
import json
import queue
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fakes import Clock, FakeSession
from PIL import Image
from starlette.testclient import TestClient

from tapwright import center_tool, cli, recordvideo, store, video
from tapwright.control import Controller
from tapwright.daemon import Daemon
from tapwright.recordvideo import Part, Recorder, StreamParser, iso
from tapwright.server import create_app

needs_av = pytest.mark.skipif(not video.available(), reason="PyAV is not installed")
SECRET = "S" * 43


def jpeg(color=(200, 30, 30), size=(64, 96)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "JPEG")
    return out.getvalue()


def part_bytes(data: bytes, seq: int, at: int, coverage: str = "screen") -> bytes:
    head = (f"Content-Type: image/jpeg\r\nContent-Length: {len(data)}\r\nX-Seq: {seq}\r\nX-Captured-At: {at}\r\n"
            f"X-Coverage: {coverage}\r\nX-Source: wda\r\nX-Size: 64x96\r\n\r\n")
    return head.encode() + data + b"\r\n--frame\r\n"


def stream_bytes(parts: list[tuple[bytes, int, int]]) -> bytes:
    return b"--frame\r\n" + b"".join(part_bytes(d, s, a) for d, s, a in parts)


class FakeWriter:
    size = (64, 96)

    def __init__(self, fail_at: int | None = None, fail_close: bool = False, signal_at: int | None = None) -> None:
        self.written: list[tuple[bytes, int]] = []
        self.fail_at, self.fail_close, self.signal_at = fail_at, fail_close, signal_at
        self.abandoned = self.committed = False
        self.completed = 0

    def write(self, data: bytes, video_ms: int) -> bool:
        n = len(self.written) + 1
        if self.fail_at == n:
            raise OSError(errno.ENOSPC, "No space left on device")
        if self.signal_at == n:
            signal.raise_signal(signal.SIGINT)  # a Ctrl+C in the middle of the encoder
        self.written.append((data, video_ms))
        self.completed += 1
        return data != b"bad"

    def close(self, end_ms: int) -> None:
        if self.fail_close:
            raise OSError(errno.ENOSPC, "No space left on device")
        if self.signal_at:
            signal.raise_signal(signal.SIGINT)  # and a second one while it finishes
        self.closed_at = end_ms

    def commit(self) -> None:
        self.committed = True

    def abandon(self) -> None:
        self.abandoned = True


# ---- arguments ----
def test_arguments(tmp_path):
    out = tmp_path / "a.mp4"
    assert recordvideo.check_args(str(out), 5, 15)[1] is None
    assert "mp4" in recordvideo.check_args(str(tmp_path / "a.mov"), None, None)[1]
    assert "--seconds" in recordvideo.check_args(str(out), 0, None)[1]
    assert "--seconds" in recordvideo.check_args(str(out), -3, None)[1]
    assert "--fps" in recordvideo.check_args(str(out), None, 0)[1]
    assert "--fps" in recordvideo.check_args(str(out), None, 500)[1]
    out.write_bytes(b"x")
    assert "already exists" in recordvideo.check_args(str(out), None, None)[1]
    out.unlink()
    recordvideo.sidecar_path(out).write_text("{}")
    assert "already exists" in recordvideo.check_args(str(out), None, None)[1]


def test_the_command_is_wired_and_takes_no_token(monkeypatch, tmp_path, capsys):
    seen = {}
    monkeypatch.setattr(recordvideo, "run", lambda out, seconds, fps: seen.update(out=out, seconds=seconds, fps=fps) or 0)
    monkeypatch.setattr("sys.argv", ["tapwright", "record-video", "--out", "x.mp4", "--seconds", "3", "--fps", "10"])
    assert cli.main() == 0 and seen == {"out": "x.mp4", "seconds": 3.0, "fps": 10}
    monkeypatch.setattr("sys.argv", ["tapwright", "record-video"])
    with pytest.raises(SystemExit):
        cli.main()
    monkeypatch.setattr("sys.argv", ["tapwright", "record-video", "--out", "x.mp4", "--token", "abc"])
    with pytest.raises(SystemExit):
        cli.main()  # there is no way to pass the token


def test_bad_arguments_exit_2_before_anything_connects(tmp_path, capsys):
    def never():
        raise AssertionError("connected")
    assert recordvideo.run(str(tmp_path / "a.avi"), connect_fn=never) == 2
    assert "--out must end in .mp4" in capsys.readouterr().err


# ---- the stream ----
def test_the_parser_reads_parts_split_anywhere():
    a, b = jpeg((1, 2, 3)), jpeg((9, 9, 9))
    raw = stream_bytes([(a, 5, 1000), (b, 6, 1250)])
    for size in (1, 7, 100, len(raw)):
        parser = StreamParser()
        got = []
        for i in range(0, len(raw), size):
            got += parser.feed(raw[i:i + size])
        assert [(p.jpeg, p.seq, p.at, p.coverage) for p in got] == [(a, 5, 1000, "screen"), (b, 6, 1250, "screen")]


def test_the_parser_refuses_a_giant_part():
    with pytest.raises(ValueError):
        StreamParser().feed(b"Content-Length: 99999999999\r\n\r\n")
    with pytest.raises(ValueError):
        StreamParser().feed(b"x" * (recordvideo.MAX_HEAD + 1))


def test_iso_is_the_action_logs_form():
    assert iso(1_763_393_011_916) == "2025-11-17T15:23:31.916Z"
    assert iso(0) == "1970-01-01T00:00:00.000Z"


# ---- times ----
def parts(*spec):
    return [Part(b"j%d" % i, i + 1, at, "screen") for i, at in enumerate(spec)]


def test_video_times_follow_the_capture_times():
    w = FakeWriter()
    r = Recorder(w, start_ms=10_000)
    for p in parts(10_000, 10_250, 12_250):  # a still moment of 2 s in the middle
        r.add(p, now_ms=p.at)
    assert [t for _, t in w.written] == [0, 250, 2250]
    assert [(e["seq"], e["captured_ms"], e["video_ms"]) for e in r.entries] == [(1, 10_000, 0), (2, 10_250, 250),
                                                                                 (3, 12_250, 2250)]
    assert r.entries[0]["captured_at"] == iso(10_000)


def test_a_picture_from_before_the_recording_starts_it_and_times_never_go_back():
    w = FakeWriter()
    r = Recorder(w, start_ms=10_000)
    for p in parts(4_000, 10_100, 10_100, 10_050):  # the still screen's old frame, then a repeat and a late one
        r.add(p, now_ms=10_200)
    assert [t for _, t in w.written] == [0, 100, 101, 102]
    assert r.entries[0]["captured_ms"] == 4_000  # the sidecar keeps the real capture time


def test_a_part_without_a_time_gets_the_time_it_came_and_other_coverage_is_left_out():
    w = FakeWriter()
    r = Recorder(w, start_ms=0)
    r.add(Part(b"a", 1, None, "screen"), now_ms=300)
    r.add(Part(b"b", 2, 400, "something-else"), now_ms=400)
    assert [t for _, t in w.written] == [300] and r.entries[0]["captured_ms"] == 300


def test_an_unreadable_picture_is_counted_not_listed():
    w = FakeWriter()
    r = Recorder(w, start_ms=0)
    r.add(Part(b"bad", 1, 10, "screen"), now_ms=10)
    r.add(Part(b"ok", 2, 20, "screen"), now_ms=20)
    assert r.unreadable == 1 and [e["seq"] for e in r.entries] == [2]


def test_fps_keeps_the_last_picture_of_a_burst():
    w = FakeWriter()
    r = Recorder(w, start_ms=0, fps=10)  # at most one frame every 100 ms
    for p in parts(0, 20, 40, 60, 80):
        r.add(p, now_ms=p.at)
    assert [t for _, t in w.written] == [0]  # the rest wait, the newest replacing the one before
    r.tick(now_ms=99)
    assert len(w.written) == 1
    r.tick(now_ms=100)
    assert [(d, t) for d, t in w.written] == [(b"j0", 0), (b"j4", 100)]  # the last one of the burst, a gap later
    assert r.dropped == 3
    r.add(Part(b"late", 9, 500, "screen"), now_ms=500)
    assert w.written[-1] == (b"late", 500)


def test_fps_writes_a_waiting_picture_when_the_recording_ends():
    w = FakeWriter()
    r = Recorder(w, start_ms=0, fps=2)
    for p in parts(0, 100):
        r.add(p, now_ms=p.at)
    r.finish()
    assert [t for _, t in w.written] == [0, 500]


# ---- the file ----
class InterruptingQueue(queue.Queue):
    """Gives what it holds, then a Ctrl+C where a wait would be."""

    def get(self, block=True, timeout=None):
        try:
            return super().get(block=False)
        except queue.Empty:
            if not block:
                raise
            raise KeyboardInterrupt from None


class FakeSource:
    """Stands in for the daemon's stream: the parts, then the end (or never an end, or a Ctrl+C)."""

    def __init__(self, items, end=True, interrupt=False) -> None:
        self.items = InterruptingQueue() if interrupt else queue.Queue()
        for item in items:
            self.items.put(item)
        if end:
            self.items.put(None)
        self.closed = False
        self.dropped = 0

    def start(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class Now:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self):
        return self.t


def read_back(path: Path) -> tuple[int, float, list[int]]:
    import av
    with av.open(str(path)) as c:
        stream = c.streams.video[0]
        times = [int(f.pts * f.time_base * 1000) for f in c.decode(stream)]
        return stream.codec_context.width, float(c.duration) / 1_000_000, times


@needs_av
def test_a_recording_has_real_time_frames_and_a_sidecar(tmp_path, capsys):
    t0 = 1_763_393_000_000
    now, mono = Now(t0), Now(0.0)
    pics = [(jpeg((200, 30, 30)), 11, t0 + 100), (jpeg((30, 200, 30)), 12, t0 + 350), (jpeg((30, 30, 200)), 13, t0 + 2350)]
    items = StreamParser().feed(stream_bytes(pics))

    def end_at():
        now.t = t0 + 3000
        mono.t = 3.0

    out = tmp_path / "run.mp4"
    source = FakeSource(items)
    real_get = source.items.get

    def get(block=True, timeout=None):
        item = real_get(block=block, timeout=timeout)
        if item is None:
            end_at()
        return item
    source.items.get = get
    code = recordvideo.run(str(out), connect_fn=lambda: source, clock=now, mono=mono)
    assert code == 0 and source.closed
    text = capsys.readouterr().out
    assert str(out) in text and "3.0 s" in text and "3 frames" in text
    width, duration, times = read_back(out)
    assert width == 64 and abs(duration - 3.0) < 0.2 and len(times) == 3
    assert times == [100, 350, 2350]  # a still moment lasts as long as it did
    side = json.loads(recordvideo.sidecar_path(out).read_text("utf-8"))
    assert side["frame_count"] == 3 and side["video"] == "run.mp4" and side["duration_ms"] == 3000
    assert [f["seq"] for f in side["frames"]] == [11, 12, 13]
    assert [f["video_ms"] for f in side["frames"]] == [100, 350, 2350]
    assert side["frames"][0]["captured_ms"] == t0 + 100 and side["frames"][0]["captured_at"] == iso(t0 + 100)
    assert not list(tmp_path.glob("*.part"))


@needs_av
def test_it_stops_after_seconds_and_finishes_the_file(tmp_path, capsys):
    out = tmp_path / "timed.mp4"
    now = Now(5_000_000)
    items = StreamParser().feed(stream_bytes([(jpeg(), 1, 5_000_100)]))
    source = FakeSource(items, end=False)  # the stream never ends
    began = time.monotonic()
    assert recordvideo.run(str(out), seconds=0.4, connect_fn=lambda: source, clock=now) == 0
    assert 0.3 < time.monotonic() - began < 5
    assert out.exists() and recordvideo.sidecar_path(out).exists() and source.closed
    assert read_back(out)[2] == [100]


@needs_av
def test_ctrl_c_finishes_the_file(tmp_path, capsys):
    out = tmp_path / "ctrlc.mp4"
    items = StreamParser().feed(stream_bytes([(jpeg(), 1, 7_000_050), (jpeg((1, 200, 1)), 2, 7_000_150)]))
    source = FakeSource(items, end=False, interrupt=True)
    assert recordvideo.run(str(out), connect_fn=lambda: source, clock=Now(7_000_000)) == 0
    assert source.closed and out.exists()
    assert len(read_back(out)[2]) == 2
    assert json.loads(recordvideo.sidecar_path(out).read_text("utf-8"))["frame_count"] == 2
    assert not list(tmp_path.glob("*.part"))


@needs_av
def test_a_rotated_picture_is_fitted_into_the_first_size(tmp_path):
    out = tmp_path / "turn.mp4"
    items = StreamParser().feed(stream_bytes([(jpeg(size=(64, 96)), 1, 100), (jpeg(size=(96, 64)), 2, 200)]))
    assert recordvideo.run(str(out), connect_fn=lambda: FakeSource(items), clock=Now(0)) == 0
    assert len(read_back(out)[2]) == 2 and read_back(out)[0] == 64


def test_no_picture_saves_nothing(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(video, "available", lambda: True)
    out = tmp_path / "none.mp4"
    w = FakeWriter()
    code = recordvideo.run(str(out), connect_fn=lambda: FakeSource([]), writer_factory=lambda p: w, clock=Now(0))
    assert code == 1 and w.abandoned and not out.exists() and not recordvideo.sidecar_path(out).exists()
    assert "nothing was recorded" in capsys.readouterr().err


# ---- no PyAV ----
def test_without_pyav_it_says_how_to_install_it(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(video, "available", lambda: False)

    def never():
        raise AssertionError("connected")
    assert recordvideo.run(str(tmp_path / "a.mp4"), connect_fn=never) == 2
    err = capsys.readouterr().err
    assert 'pip install "tapwright[video]"' in err and "Traceback" not in err
    assert not (tmp_path / "a.mp4").exists()


# ---- the token ----
class FakeDaemon:
    """A server that proves the token as tapwright serve does, and streams two parts."""

    def __init__(self, hides: bool = True, hold: float = 0.0, late: bytes = b"") -> None:
        self.seen: list[tuple[str, str, str | None]] = []  # method, path, Authorization
        daemon = self
        pics = stream_bytes([(jpeg(), 1, int(time.time() * 1000)), (jpeg((10, 90, 10)), 2, int(time.time() * 1000) + 120)])

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                daemon.seen.append(("POST", self.path, self.headers.get("Authorization")))
                challenge = bytes.fromhex(body["challenge"])
                signed = (b"tapwright-proof-v2\x00" + challenge + b"\x00127.0.0.1\x00" + str(daemon.port).encode())
                proof = hmac.new(SECRET.encode(), signed, hashlib.sha256).hexdigest()
                data = json.dumps({"proof": proof}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                daemon.seen.append(("GET", self.path, self.headers.get("Authorization")))
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                if hides:
                    self.send_header("X-Hides", "notification-center")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(pics)
                self.wfile.flush()
                if hold:
                    time.sleep(hold)  # a quiet screen
                    try:
                        self.wfile.write(late)
                        self.wfile.flush()
                    except OSError:
                        pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@needs_av
def test_the_token_goes_only_in_the_proved_header_and_is_never_shown_or_saved(tmp_path, monkeypatch, capsys):
    fake = FakeDaemon()
    try:
        monkeypatch.setattr(store, "read_serve_info", lambda: {"port": fake.port, "token": SECRET})
        out = tmp_path / "t.mp4"
        assert recordvideo.run(str(out), seconds=5) == 0
    finally:
        fake.stop()
    shown = capsys.readouterr()
    assert SECRET not in shown.out and SECRET not in shown.err
    assert SECRET not in recordvideo.sidecar_path(out).read_text("utf-8")
    assert SECRET.encode() not in out.read_bytes()
    assert not any(SECRET in str(p) for p in tmp_path.rglob("*"))
    posts = [s for s in fake.seen if s[0] == "POST"]
    gets = [s for s in fake.seen if s[0] == "GET"]
    assert posts[0][2] is None  # the proof is asked without the token
    assert gets == [("GET", "/stream?for=recording", f"Bearer {SECRET}")]  # a header, never the address
    assert fake.seen.index(posts[0]) < fake.seen.index(gets[0])


def test_an_older_daemon_that_cant_leave_notification_center_out_is_refused(tmp_path, monkeypatch, capsys):
    fake = FakeDaemon(hides=False)
    monkeypatch.setattr(video, "available", lambda: True)
    try:
        monkeypatch.setattr(store, "read_serve_info", lambda: {"port": fake.port, "token": SECRET})
        assert recordvideo.run(str(tmp_path / "old.mp4"), seconds=2) == 1
    finally:
        fake.stop()
    err = capsys.readouterr().err
    assert "Notification Center" in err and SECRET not in err
    assert not (tmp_path / "old.mp4").exists()


def test_no_daemon_is_said_plainly(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(video, "available", lambda: True)
    monkeypatch.setattr(store, "read_serve_info", lambda: {})
    assert recordvideo.run(str(tmp_path / "n.mp4")) == 1
    assert "tapwright serve" in capsys.readouterr().err


def test_a_daemon_that_cannot_prove_the_token_never_gets_it(tmp_path, monkeypatch, capsys):
    fake = FakeDaemon()
    monkeypatch.setattr(video, "available", lambda: True)
    try:
        monkeypatch.setattr(store, "read_serve_info", lambda: {"port": fake.port, "token": "W" * 43})  # another token
        assert recordvideo.run(str(tmp_path / "w.mp4")) == 1
    finally:
        fake.stop()
    assert not [s for s in fake.seen if s[0] == "GET"]
    assert all(s[2] is None for s in fake.seen)


# ---- Notification Center (fails closed) ----
TOKEN = "t" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}", "X-Tapwright-Client": "agent-1", "X-Tapwright-Mode": "dedicated"}


def app_and_daemon():
    clock = Clock()
    d = Daemon(FakeSession(), control=Controller(clock=clock, sleep=clock.sleep), log=lambda *a: None,
               stream_linger=0, stream_retry=0.01)
    d._set_state("live")
    now = [1_000.0]  # the hub's monotonic seconds
    d.hub._mono = lambda: now[0]
    d.hub._clock = lambda: 1_763_393_000.0 + now[0]
    return create_app(d, TOKEN, hosts={"testserver"}), d, now


def stream_parts(app, path: str):
    with TestClient(app) as c:
        r = c.get(path, headers=AUTH)
    assert r.status_code == 200
    return StreamParser().feed(r.content), r.headers


def pictures(parts: list[Part]) -> list[bytes]:
    return [p.jpeg for p in parts if p.jpeg]


def a_picture(color=(9, 9, 9)):
    from tapwright.stream import Picture
    return Picture(lambda: Image.new("RGB", (16, 16), color), lambda: Image.new("L", (2, 2)))


def test_the_recording_stream_has_no_frame_from_while_notification_center_was_open():
    app, d, now = app_and_daemon()
    d.hub.publish(b"\xff\xd8before")
    d.hub.close()
    got, headers = stream_parts(app, "/stream?for=recording")
    assert pictures(got) == [b"\xff\xd8before"] and headers["x-hides"] == "notification-center"

    app, d, now = app_and_daemon()
    d._center_begin()
    now[0] += 0.5
    d.hub.publish(b"\xff\xd8center")  # arrived while it was open
    now[0] += 0.5
    d._center_end(True)
    d.hub.close()
    got, headers = stream_parts(app, "/stream?for=recording")
    assert pictures(got) == [] and headers["x-hides"] == "notification-center"
    got, headers = stream_parts(app, "/stream")  # the live view itself is unchanged
    assert pictures(got) == [b"\xff\xd8center"] and "x-hides" not in headers and not [p for p in got if p.keepalive]

    app, d, now = app_and_daemon()
    d._center_begin()
    d._center_end(True)
    now[0] += center_tool.CENTER_HIDE_AFTER_S + 1.0  # past the margin
    d.hub.publish(b"\xff\xd8after")
    d.hub.close()
    got, _ = stream_parts(app, "/stream?for=recording")
    assert pictures(got) == [b"\xff\xd8after"]


def test_H1_a_late_frame_inside_the_margin_is_left_out_whatever_its_stamp():
    """Fails on a2eb644: its span ended 400 ms after the close, so a frame that arrived 2 s after it (a slow link's
    queue) was recorded."""
    app, d, now = app_and_daemon()
    d._center_begin()
    d._center_end(True)
    now[0] += 2.0
    d.hub.publish(b"\xff\xd8queued")
    d.hub.close()
    assert pictures(stream_parts(app, "/stream?for=recording")[0]) == []


def test_H1_a_picture_sent_again_after_the_span_is_left_out():
    """Fails on a2eb644: a feeder that sends the newest picture again gives it a new arrival time, past the span."""
    app, d, now = app_and_daemon()
    shade, later = a_picture((1, 1, 1)), a_picture((200, 200, 200))
    d._center_begin()
    now[0] += 0.3
    d.hub.publish_frame(shade)  # the picture of Notification Center half open
    d._center_end(True)
    now[0] += 10.0
    d.hub.publish_frame(shade)  # sent again long after
    d.hub.close()
    assert pictures(stream_parts(app, "/stream?for=recording")[0]) == []
    app, d, now = app_and_daemon()
    d._center_begin()
    d.hub.publish_frame(shade)
    d._center_end(True)
    now[0] += 10.0
    d.hub.publish_frame(later)  # a different picture, long after the close: recorded
    d.hub.close()
    assert len(pictures(stream_parts(app, "/stream?for=recording")[0])) == 1


def test_H1_a_frame_whose_arrival_the_hub_cannot_tell_is_dropped():
    app, d, now = app_and_daemon()
    f = d.hub.publish(b"\xff\xd8x")
    assert d.recording_hides(d.hub, f) is False
    for _ in range(300):  # it ages out of what the hub keeps
        d.hub.publish(b"\xff\xd8y")
    assert d.recording_hides(d.hub, f) is True


def test_frames_stay_out_while_it_may_still_be_open():
    app, d, now = app_and_daemon()
    d._center_begin()
    d._center_end(False)  # closing it was not shown to work
    now[0] += 60
    d.hub.publish(b"\xff\xd8maybe")
    d.hub.close()
    assert pictures(stream_parts(app, "/stream?for=recording")[0]) == []
    assert d.center_covers(now[0])
    d._center_begin()  # the next read goes on in the same span
    d._center_end(True)
    now[0] += center_tool.CENTER_HIDE_AFTER_S + 1
    assert not d.center_covers(now[0])
    assert d.center_covers(now[0] - 30)  # (the whole span stays out)


def test_a_notification_read_marks_its_span(tmp_path, monkeypatch):
    from test_notifications import BUNDLE, Scope, center_daemon, finish, run, started
    for name in ("CENTER_OPEN_WAIT", "CENTER_CLOSE_WAIT", "OPEN_WAIT"):
        monkeypatch.setattr(center_tool, name, 0.0)

    async def go():
        d, s = center_daemon(tmp_path, [])
        stop, task = await started(d)
        before = d.hub.mono()
        await d.run_tool("a", "notifications", {}, scope=Scope((BUNDLE,)))
        after = d.hub.mono()
        await finish(stop, task)
        assert len(d._center_spans) == 1 and d._center_spans[0][1] is not None
        start, end = d._center_spans[0]
        assert before <= start <= end and d.center_covers(start) and d.center_covers(end)
        assert not d.center_covers(after + center_tool.CENTER_HIDE_AFTER_S + 1)
        assert not d.center_covers(before - 1)
    run(go)


# ---- M1, L5: a silent stream, and what the recorder is told ----
def idle_hub(d, rounds=2):
    """The hub answers `rounds` waits with nothing (a quiet screen), then closes."""
    calls = [0]

    async def next_after(seq, timeout=None):
        calls[0] += 1
        if calls[0] > rounds:
            d.hub.close()
        return None

    d.hub.next_after = next_after


def test_M1_a_quiet_recording_stream_sends_keep_alives():
    """Fails on a2eb644: the stream sent nothing while still, and the recorder's read timed out after 30 s."""
    app, d, now = app_and_daemon()
    idle_hub(d)
    got, _ = stream_parts(app, "/stream?for=recording")
    assert len(got) == 2 and all(p.keepalive and not p.jpeg and not p.hidden for p in got)
    app, d, now = app_and_daemon()
    idle_hub(d)
    assert stream_parts(app, "/stream")[0] == []  # the live view's stream has none


def test_L5_keep_alives_say_when_frames_are_being_left_out():
    app, d, now = app_and_daemon()
    d._center_begin()
    d._center_end(False)  # it may be open still
    idle_hub(d)
    got, _ = stream_parts(app, "/stream?for=recording")
    assert got and all(p.hidden for p in got)
    app, d, now = app_and_daemon()
    d._center_begin()
    d._center_end(True)
    d.hub.publish(b"\xff\xd8x")  # a frame inside the margin is left out, and the stream says so
    d.hub.close()
    got, _ = stream_parts(app, "/stream?for=recording")
    assert pictures(got) == [] and any(p.hidden for p in got)
    app, d, now = app_and_daemon()
    d.hub.publish(b"\xff\xd8x")  # no span: nothing is hidden
    d.hub.close()
    got, _ = stream_parts(app, "/stream?for=recording")
    assert not any(p.hidden for p in got)


def test_the_recorder_tells_the_user_when_frames_are_left_out(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(video, "available", lambda: True)
    parts_ = [Part(b"", None, None, None, keepalive=True, hidden=True), Part(b"ok", 1, 50, "screen")]
    w = FakeWriter()
    assert recordvideo.run(str(tmp_path / "h.mp4"), connect_fn=lambda: FakeSource(parts_), writer_factory=lambda p: w,
                           clock=Now(0), mono=Now(0.0)) == 0
    assert "leaving frames out" in capsys.readouterr().err and len(w.written) == 1  # (a keep-alive is no picture)


# ---- M2: a bounded queue, and the backlog on stop ----
def test_M2_the_queue_is_bounded_and_keeps_the_newest():
    """Fails on a2eb644: an unbounded queue."""
    pics = [(jpeg(), n, 1000 + n) for n in range(1, 151)]

    class Reply:
        def __init__(self):
            self.data = [stream_bytes(pics), b""]

        def read1(self, n):
            return self.data.pop(0)

    class Conn:
        def close(self):
            pass

    source = recordvideo.Source(Conn(), Reply())
    source._run()  # (nobody takes parts from the queue)
    assert source.items.qsize() <= recordvideo.QUEUE_MAX
    left = []
    while not source.items.empty():
        left.append(source.items.get_nowait())
    assert left[-1] is None and left[-2].seq == 150  # the newest are kept
    assert source.dropped >= 150 - recordvideo.QUEUE_MAX


def test_M2_the_recorder_keeps_no_list_when_it_has_a_sink():
    seen = []
    r = Recorder(FakeWriter(), 0, sink=seen.append)
    for p in parts(0, 10, 20):
        r.add(p, p.at)
    assert r.entries == [] and r.count == 3 and [e["video_ms"] for e in seen] == [0, 10, 20]


def test_M4_ctrl_c_inside_the_encoder_is_deferred_and_the_queued_frames_are_written(tmp_path, capsys, monkeypatch):
    """Fails on a2eb644: the interrupt landed inside write (the frame never completed) and the queue was dropped."""
    monkeypatch.setattr(video, "available", lambda: True)
    before = signal.getsignal(signal.SIGINT)
    w = FakeWriter(signal_at=1)
    items = [Part(b"p%d" % n, n, 100 * n, "screen") for n in range(1, 6)]
    code = recordvideo.run(str(tmp_path / "c.mp4"), connect_fn=lambda: FakeSource(items, end=False),
                           writer_factory=lambda p: w, clock=Now(0), mono=Now(0.0))
    assert code == 0 and w.completed == 5 and w.committed  # the interrupted write finished, and the backlog went in
    assert signal.getsignal(signal.SIGINT) is before  # the handler is put back
    assert "Saved" in capsys.readouterr().out


# ---- M3: errors while recording ----
def test_M3_a_full_disk_finishes_what_was_written(tmp_path, capsys, monkeypatch):
    """Fails on a2eb644: the OSError escaped run() with a traceback and left the .part file."""
    monkeypatch.setattr(video, "available", lambda: True)
    w = FakeWriter(fail_at=3)
    items = [Part(b"p%d" % n, n, 100 * n, "screen") for n in range(1, 6)]
    out = tmp_path / "full.mp4"
    code = recordvideo.run(str(out), connect_fn=lambda: FakeSource(items), writer_factory=lambda p: w, clock=Now(0),
                           mono=Now(0.0))
    err = capsys.readouterr().err
    assert code == 0 and "the disk is full" in err and "what came before is saved" in err
    assert w.committed and len(w.written) == 2
    side = json.loads(recordvideo.sidecar_path(out).read_text("utf-8"))
    assert side["frame_count"] == 2 and not list(tmp_path.glob("*.part"))


def test_M3_a_file_that_cannot_be_finished_is_deleted_and_says_so(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(video, "available", lambda: True)
    w = FakeWriter(fail_close=True)
    out = tmp_path / "gone.mp4"
    code = recordvideo.run(str(out), connect_fn=lambda: FakeSource([Part(b"a", 1, 5, "screen")]),
                           writer_factory=lambda p: w, clock=Now(0), mono=Now(0.0))
    assert code == 1 and "the disk is full" in capsys.readouterr().err
    assert w.abandoned and not out.exists() and not recordvideo.sidecar_path(out).exists()
    assert not list(tmp_path.iterdir())


@needs_av
def test_M3_an_encoder_that_cannot_open_for_a_file_is_skipped(tmp_path, monkeypatch):
    """Fails on a2eb644: only the live view's probe chose the encoder, and a failure at the file's own open escaped."""
    real = recordvideo.Mp4Writer._open_with
    tried = []

    def open_with(self, name, size):
        tried.append(name)
        if name != "libx264":
            raise RuntimeError(f"{name} crashed on open")  # as h264_mf does in an MP4 stream
        return real(self, name, size)

    monkeypatch.setattr(recordvideo.Mp4Writer, "_open_with", open_with)
    out = tmp_path / "enc.mp4"
    items = StreamParser().feed(stream_bytes([(jpeg(), 1, 100), (jpeg((5, 5, 5)), 2, 300)]))
    assert recordvideo.run(str(out), connect_fn=lambda: FakeSource(items), clock=Now(0), mono=Now(0.5)) == 0
    assert tried == ["h264_nvenc", "libx264"] and len(read_back(out)[2]) == 2


@needs_av
def test_M3_when_no_encoder_opens_nothing_is_left_and_the_reason_is_said(tmp_path, monkeypatch, capsys):
    def open_with(self, name, size):
        raise RuntimeError(f"{name} crashed on open")

    monkeypatch.setattr(recordvideo.Mp4Writer, "_open_with", open_with)
    out = tmp_path / "none.mp4"
    items = StreamParser().feed(stream_bytes([(jpeg(), 1, 100)]))
    assert recordvideo.run(str(out), connect_fn=lambda: FakeSource(items), clock=Now(0), mono=Now(0.0)) == 1
    err = capsys.readouterr().err
    assert "no H.264 encoder works" in err and "h264_mf" in err and "Traceback" not in err
    assert not list(tmp_path.iterdir())


# ---- L1, L2: the files appear together, and only for the run that reserved them ----
@needs_av
def test_L1_a_sidecar_that_cannot_be_finished_leaves_no_mp4(tmp_path, monkeypatch, capsys):
    """Fails on a2eb644: the MP4 was already renamed, so a complete-looking file stayed with exit 1."""
    def broken(self, *a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(recordvideo.Sidecar, "close", broken)
    out = tmp_path / "side.mp4"
    items = StreamParser().feed(stream_bytes([(jpeg(), 1, 100)]))
    assert recordvideo.run(str(out), connect_fn=lambda: FakeSource(items), clock=Now(0), mono=Now(0.0)) == 1
    assert "nothing was saved" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())


def test_L2_a_second_run_with_the_same_name_does_not_touch_the_first(tmp_path, monkeypatch, capsys):
    """Fails on a2eb644: both runs passed the exists check and the later rename replaced the first file."""
    monkeypatch.setattr(video, "available", lambda: True)
    out = tmp_path / "same.mp4"
    out.write_bytes(b"the first run's video")
    monkeypatch.setattr(recordvideo, "check_args", lambda o, s, f: (out, None))  # (both passed the check)
    code = recordvideo.run(str(out), connect_fn=lambda: FakeSource([Part(b"a", 1, 5, "screen")]),
                           writer_factory=lambda p: FakeWriter(), clock=Now(0), mono=Now(0.0))
    assert code == 2 and "another run" in capsys.readouterr().err
    assert out.read_bytes() == b"the first run's video" and not recordvideo.sidecar_path(out).exists()
    assert recordvideo.part_name(out) != recordvideo.part_name(out)  # each run has its own temporary name


# ---- L3: video time follows the monotonic clock ----
def test_L3_a_step_of_the_wall_clock_does_not_move_video_time():
    """Fails on a2eb644: after the wall clock stepped back every frame was one ms after the one before."""
    w = FakeWriter()
    r = Recorder(w, start_ms=10_000, start_mono_ms=5_000)
    spec = [(10_000, 5_000), (10_100, 5_100), (4_000, 7_000), (4_100, 7_100)]  # wall ms stepped back; monotonic ms
    for i, (at, mono_ms) in enumerate(spec):
        r.add(Part(b"j", i + 1, at, "screen", mono_ms), now_ms=at)
    assert [t for _, t in w.written] == [0, 100, 2000, 2100]
    assert [e["captured_ms"] for e in r.entries] == [10_000, 10_100, 4_000, 4_100]  # the sidecar keeps the wall clock


def test_L3_the_stream_carries_the_monotonic_arrival():
    app, d, now = app_and_daemon()
    d.hub.publish(b"\xff\xd8x")
    d.hub.close()
    got, _ = stream_parts(app, "/stream?for=recording")
    assert got[0].mono_ms == 1_000_000
    plain, _ = stream_parts(app, "/stream")
    assert plain[0].mono_ms is None


# ---- L4: the last picture of a burst lasts to the end ----
def test_L4_a_waiting_picture_is_written_at_the_end_not_a_gap_later():
    """Fails on a2eb644: finish() took no end time, so the picture came a whole gap after the recording stopped."""
    w = FakeWriter()
    r = Recorder(w, start_ms=0, fps=2)
    for p in parts(0, 100):
        r.add(p, now_ms=p.at)
    r.finish(end_ms=150)
    assert [t for _, t in w.written] == [0, 150]
    w = FakeWriter()
    r = Recorder(w, start_ms=0, fps=2)
    for p in parts(0, 100):
        r.add(p, now_ms=p.at)
    r.finish(end_ms=0)  # never before the one it follows
    assert [t for _, t in w.written] == [0, 1]


# ---- CodeRabbit, PR #17 ----
def test_CR1_center_center_a_picture_stays_out_when_a_feeder_publishes_it_again():
    """center_tool.py. Fails on e831a96: only the one picture that was newest at the end was remembered, so an earlier
    picture of the read, published again after the margin, was recorded."""
    app, d, now = app_and_daemon()
    before, a, b = a_picture((7, 7, 7)), a_picture((1, 1, 1)), a_picture((2, 2, 2))
    d.hub.publish_frame(before)  # on the screen before the read
    now[0] += 1
    d._center_begin()
    now[0] += 0.1
    d.hub.publish_frame(a)
    now[0] += 0.1
    d.hub.publish_frame(b)  # the newest at the end
    d._center_end(True)
    now[0] += 10.0
    d.hub.publish_frame(a)  # the feeder sends the older picture of the read again
    d.hub.close()
    assert pictures(stream_parts(app, "/stream?for=recording")[0]) == []
    assert d.recording_hides(d.hub, d.hub.latest)
    now[0] += 1
    again = d.hub.publish_frame(b)
    assert d.recording_hides(d.hub, again)  # however often, whenever
    again = d.hub.publish_frame(before)  # a picture from before the read is not Notification Center
    assert not d.recording_hides(d.hub, again)
    fresh = d.hub.publish_frame(a_picture((3, 3, 3)))  # a new picture after the margin is recorded
    assert not d.recording_hides(d.hub, fresh)


def test_CR1_a_picture_that_came_while_it_was_open_is_excluded_when_seen_hidden():
    app, d, now = app_and_daemon()
    shade = a_picture((5, 5, 5))
    d._center_begin()
    f = d.hub.publish_frame(shade)
    assert d.recording_hides(d.hub, f)  # inside the span
    d._center_end(False)
    d._center_begin()
    d._center_end(True)
    now[0] += 20
    assert d.recording_hides(d.hub, d.hub.publish_frame(shade))


def test_CR2_a_clock_step_before_the_first_picture_does_not_stretch_the_video():
    """recordvideo.py. Fails on e831a96: the first frame anchored on the wall clock, so +1 h gave video_ms 3,601,000
    (and the signature had no monotonic start)."""
    w = FakeWriter()
    r = Recorder(w, start_ms=1_000_000, start_mono_ms=500_000)
    hour = 3_600_000
    r.add(Part(b"a", 1, 1_000_000 + hour + 50, "screen", 500_050), now_ms=1_000_000 + hour)  # the wall clock stepped +1 h
    r.add(Part(b"b", 2, 1_000_000 + hour + 400, "screen", 500_400), now_ms=1_000_000 + hour)
    r.add(Part(b"c", 3, 1_000_000 - 30, "screen", 500_900), now_ms=1_000_000)  # and back again
    assert [t for _, t in w.written] == [50, 400, 900]
    old = Recorder(FakeWriter(), start_ms=1_000_000, start_mono_ms=500_000)
    old.add(Part(b"z", 1, 1_000_000, "screen", 499_000), now_ms=1_000_000)  # a picture from before the recording
    assert old.writer.written[0][1] == 0  # the zero clamp stays


def test_CR3_the_line_has_no_read_timeout_and_a_quiet_screen_is_not_a_break(monkeypatch):
    """recordvideo.py. Fails on e831a96: connect() set a 30 s socket timeout, and on Python 3.10 a buffered response
    whose read timed out can never be read again. The model below fails the same way: once a timeout fires, every
    read after it raises."""
    late = part_bytes(jpeg((3, 3, 3)), 3, int(time.time() * 1000) + 900)
    fake = FakeDaemon(hold=1.0, late=late)
    try:
        monkeypatch.setattr(store, "read_serve_info", lambda: {"port": fake.port, "token": SECRET})
        source = recordvideo.connect()
        assert source.sock.gettimeout() is None
        real, poisoned = source.reply.read1, [False]

        def read1(n):
            if poisoned[0]:
                raise OSError("cannot read from timed out object")
            if source.sock.gettimeout() is not None:  # a timeout would fire while the screen is quiet
                time.sleep(0.3)
                poisoned[0] = True
                raise TimeoutError("timed out")
            return real(n)

        source.reply.read1 = read1
        source.start()
        seqs = []
        while True:
            item = source.items.get(timeout=10)
            assert not isinstance(item, Exception) and item is not None
            if item.jpeg:
                seqs.append(item.seq)
            if 3 in seqs:
                break
        assert seqs == [1, 2, 3]
        source.close()
    finally:
        fake.stop()


def test_CR3_stopping_interrupts_a_blocked_read(monkeypatch):
    fake = FakeDaemon(hold=5.0)
    try:
        monkeypatch.setattr(store, "read_serve_info", lambda: {"port": fake.port, "token": SECRET})
        source = recordvideo.connect()
        source.start()
        assert source.items.get(timeout=5).seq == 1
        time.sleep(0.2)  # the reader is blocked in a read now
        began = time.monotonic()
        source.close()
        source._thread.join(3)
        assert not source._thread.is_alive() and time.monotonic() - began < 3
    finally:
        fake.stop()


def test_CR3_a_line_that_gives_nothing_for_too_long_ends_the_recording_with_what_it_has(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(video, "available", lambda: True)
    source = FakeSource([Part(b"a", 1, 5, "screen")], end=False)
    source.last_data = time.monotonic() - recordvideo.SILENCE_LIMIT - 1
    w = FakeWriter()
    assert recordvideo.run(str(tmp_path / "q.mp4"), connect_fn=lambda: source, writer_factory=lambda p: w,
                           clock=Now(0), mono=Now(0.0)) == 0
    assert "went silent" in capsys.readouterr().err and w.committed and len(w.written) == 1


def test_CR4_a_still_airplay_picture_that_is_sent_again_still_gets_keep_alives(monkeypatch):
    """server.py. Fails on e831a96: the copies of a still picture (new seqs, the same picture) came faster than the idle
    wait ended, the duplicate branch sent nothing, and the recorder starved."""
    from tapwright import server
    monkeypatch.setattr(server, "RECORDING_KEEPALIVE", 0.02)
    app, d, now = app_and_daemon()
    still = a_picture((120, 120, 120))
    frames = [d.hub.publish_frame(still) for _ in range(6)]
    queue_ = list(frames)

    async def next_after(seq, timeout=None):
        if not queue_:
            d.hub.close()
            return None
        time.sleep(0.03)  # longer than the keep-alive interval, shorter than the idle wait
        return queue_.pop(0)

    d.hub.next_after = next_after
    got, _ = stream_parts(app, "/stream?for=recording")
    assert len(pictures(got)) == 1  # the copies are not sent again
    assert len([p for p in got if p.keepalive]) >= 3  # but the line is kept alive
    app, d, now = app_and_daemon()
    frames = [d.hub.publish_frame(still) for _ in range(3)]
    queue_ = list(frames)
    d.hub.next_after = next_after
    got, _ = stream_parts(app, "/stream")  # the live view's own stream is as it was
    assert len(pictures(got)) == 1 and not [p for p in got if p.keepalive]

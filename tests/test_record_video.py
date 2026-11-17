"""`tapwright record-video` (1.7.7): the stream's parts, their times on the video's clock, the MP4 and its sidecar, the
stop after --seconds and on Ctrl+C, no token anywhere, no PyAV, and no frame from while Notification Center was open."""
import hashlib
import hmac
import io
import json
import queue
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

    def __init__(self) -> None:
        self.written: list[tuple[bytes, int]] = []

    def write(self, data: bytes, video_ms: int) -> bool:
        self.written.append((data, video_ms))
        return data != b"bad"

    def close(self, end_ms: int) -> None:
        self.closed_at = end_ms

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

    def start(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class Now:
    def __init__(self, t: int) -> None:
        self.t = t

    def __call__(self) -> int:
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
    now = Now(t0)
    pics = [(jpeg((200, 30, 30)), 11, t0 + 100), (jpeg((30, 200, 30)), 12, t0 + 350), (jpeg((30, 30, 200)), 13, t0 + 2350)]
    items = StreamParser().feed(stream_bytes(pics))

    def end_at():
        now.t = t0 + 3000

    out = tmp_path / "run.mp4"
    source = FakeSource(items)
    real_get = source.items.get

    def get(timeout=None):
        item = real_get(timeout=timeout)
        if item is None:
            end_at()
        return item
    source.items.get = get
    code = recordvideo.run(str(out), connect_fn=lambda: source, clock=now)
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

    def __init__(self, hides: bool = True) -> None:
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

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
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


# ---- Notification Center ----
TOKEN = "t" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}", "X-Tapwright-Client": "agent-1", "X-Tapwright-Mode": "dedicated"}


def app_and_daemon():
    clock = Clock()
    d = Daemon(FakeSession(), control=Controller(clock=clock, sleep=clock.sleep), log=lambda *a: None,
               stream_linger=0, stream_retry=0.01)
    d._set_state("live")
    now = [1_000_000.0]
    d.hub._clock = lambda: now[0]
    return create_app(d, TOKEN, hosts={"testserver"}), d, now


def stream_parts(app, path: str):
    with TestClient(app) as c:
        r = c.get(path, headers=AUTH)
    assert r.status_code == 200
    return StreamParser().feed(r.content), r.headers


def test_the_recording_stream_has_no_frame_from_while_notification_center_was_open():
    app, d, now = app_and_daemon()
    d.hub.publish(b"\xff\xd8before")
    d.hub.close()
    got, headers = stream_parts(app, "/stream?for=recording")
    assert [p.jpeg for p in got] == [b"\xff\xd8before"] and headers["x-hides"] == "notification-center"

    app, d, now = app_and_daemon()
    d._center_begin()
    now[0] += 0.5
    d.hub.publish(b"\xff\xd8center")  # captured while it was open
    now[0] += 0.5
    d._center_end(True)
    d.hub.close()
    got, headers = stream_parts(app, "/stream?for=recording")
    assert got == [] and headers["x-hides"] == "notification-center"
    got, headers = stream_parts(app, "/stream")  # the live view itself is unchanged
    assert [p.jpeg for p in got] == [b"\xff\xd8center"] and "x-hides" not in headers

    app, d, now = app_and_daemon()
    d._center_begin()
    d._center_end(True)
    now[0] += 1.0  # past the moment it takes to slide up
    d.hub.publish(b"\xff\xd8after")
    d.hub.close()
    got, _ = stream_parts(app, "/stream?for=recording")
    assert [p.jpeg for p in got] == [b"\xff\xd8after"]


def test_frames_stay_out_while_it_may_still_be_open():
    app, d, now = app_and_daemon()
    d._center_begin()
    d._center_end(False)  # closing it was not shown to work
    now[0] += 60
    d.hub.publish(b"\xff\xd8maybe")
    d.hub.close()
    assert stream_parts(app, "/stream?for=recording")[0] == []
    assert d.center_hidden(int(now[0] * 1000))
    d._center_begin()  # the next read goes on in the same span
    d._center_end(True)
    now[0] += 1.0
    assert not d.center_hidden(int(now[0] * 1000))
    assert d.center_hidden(int((now[0] - 30) * 1000))  # (the whole span stays out)


def test_a_notification_read_marks_its_span(tmp_path, monkeypatch):
    from test_notifications import BUNDLE, Scope, center_daemon, finish, run, started
    for name in ("CENTER_OPEN_WAIT", "CENTER_CLOSE_WAIT", "OPEN_WAIT"):
        monkeypatch.setattr(center_tool, name, 0.0)

    async def go():
        d, s = center_daemon(tmp_path, [])
        stop, task = await started(d)
        before = d.hub.now_ms()
        await d.run_tool("a", "notifications", {}, scope=Scope((BUNDLE,)))
        after = d.hub.now_ms()
        await finish(stop, task)
        assert len(d._center_spans) == 1 and d._center_spans[0][1] is not None
        start, end = d._center_spans[0]
        assert before <= start <= end and d.center_hidden(start) and d.center_hidden(end)
        assert not d.center_hidden(after + center_tool.CENTER_HIDE_AFTER_MS + 1)
        assert not d.center_hidden(before - 1)
    run(go)

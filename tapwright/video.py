# adapted from phoneuse (MIT), Aleksey Smolenchuk
"""The live view's H.264 video hub (`WS /video`, docs/VIEWER-PROTOCOL.md).

UxPlay takes the phone's AirPlay screen mirror and forwards its H.264 as RTP to a UDP port on
localhost. iOS sends a frame only when the screen changes and a key frame only when mirroring starts,
so a viewer that joined later could not decode the phone's own stream. The hub therefore decodes once,
keeps the last frame, and encodes a fresh stream (no B-frames, key frames only on request) that every
viewer can join at any time with a key frame of the last picture.

Threads: one receives RTP and decodes, one encodes the newest frame (latest wins). Viewers are read
from asyncio through `Subscription`; the threads hand data over with `loop.call_soon_threadsafe`, so
nothing here ever blocks the event loop.

PyAV (`av`) is optional: it is imported inside functions only, and `available()` says whether it is there.
"""
import base64
import collections
import importlib
import math
import socket
import struct
import threading
import time
from fractions import Fraction
from typing import Callable

from .rtp import Depacketizer, split_annexb

LIVE_WINDOW = 3.0  # seconds: frames this recent mean the phone is mirroring
BACKLOG = 0.25  # seconds of pictures a slow viewer may have queued before it is resynced
BACKLOG_ITEMS = 60  # and a cap on their number, whatever the clock says
HEADER = struct.Struct("!B3xI")  # flags, 3 zero bytes, ms since the config's `at`
DENSITY = {"sharp": 2.0, "balanced": 1.5, "light": 0.75}  # minimum encoded pixels per screen point of width
HIGH_PROFILES = frozenset({100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135})  # SPS carries chroma/depth
DROPPED_NALS = frozenset({6, 7, 8, 9})  # SEI and parameter sets and delimiters stay out of pictures

ENCODERS: dict[str, dict[str, str]] = {
    # Constant quality, low enough to keep small text sharp; each frame leaves the encoder as soon as it is encoded.
    "h264_nvenc": {"preset": "p2", "tune": "ull", "zerolatency": "1", "delay": "0", "rc": "vbr",
                   "cq": "19", "forced-idr": "1", "bf": "0"},
    "libx264": {"preset": "veryfast", "tune": "zerolatency", "crf": "18", "forced-idr": "1",
                "x264-params": "keyint=infinite:scenecut=0:bframes=0"},
    "h264_mf": {},
}


def available() -> bool:
    """PyAV can be imported (it is an optional dependency: `pip install tapwright[video]`)."""
    try:
        importlib.import_module("av")
    except ImportError:
        return False
    return True


# ---- parameter sets: SPS and PPS into an avcC record and a WebCodecs codec string ----
class _Bits:
    def __init__(self, data: bytes) -> None:
        self.data, self.pos = data, 0

    def bit(self) -> int:
        byte = self.data[self.pos >> 3]
        self.pos += 1
        return (byte >> (7 - ((self.pos - 1) & 7))) & 1

    def ue(self) -> int:
        zeros = 0
        while self.bit() == 0:
            zeros += 1
            if zeros > 32:
                raise ValueError("bad Exp-Golomb code")
        value = 0
        for _ in range(zeros):
            value = (value << 1) | self.bit()
        return (1 << zeros) - 1 + value


def _sps_format(sps: bytes) -> tuple[int, int, int]:
    """chroma_format_idc, luma bit depth and chroma bit depth from an SPS NAL unit (4:2:0, 8 bit if unreadable)."""
    try:
        rbsp = sps[1:].replace(b"\x00\x00\x03", b"\x00\x00")  # drop emulation prevention bytes
        bits = _Bits(rbsp)
        bits.pos = 24  # profile, constraint flags and level
        bits.ue()  # sps id
        chroma = bits.ue()
        if chroma == 3:
            bits.bit()  # separate_colour_plane_flag
        return chroma, 8 + bits.ue(), 8 + bits.ue()
    except (ValueError, IndexError):
        return 1, 8, 8


def make_avcc(sps: bytes, pps: bytes) -> bytes:
    """The avcC record (ISO 14496-15) for one SPS and one PPS NAL unit, with NAL units 4 bytes long."""
    record = bytes([1, sps[1], sps[2], sps[3], 0xFF, 0xE1]) + struct.pack("!H", len(sps)) + sps
    record += bytes([1]) + struct.pack("!H", len(pps)) + pps
    if sps[1] in HIGH_PROFILES:
        chroma, luma, depth = _sps_format(sps)
        record += bytes([0xFC | chroma, 0xF8 | (luma - 8), 0xF8 | (depth - 8), 0])
    return record


def codec_string(sps: bytes) -> str:
    """`avc1.PPCCLL`: profile, constraint flags and level bytes of the SPS, in hex."""
    return "avc1." + sps[1:4].hex()


def to_avcc(nals: list[bytes]) -> bytes:
    """NAL units as one access unit in AVCC form: each preceded by its 4-byte big-endian length."""
    return b"".join(struct.pack("!I", len(n)) + n for n in nals)


# ---- PyAV codecs ----
def open_encoder(width: int, height: int, skip: frozenset[str] | set[str] = frozenset()):
    """The first encoder that opens, in ENCODERS order, leaving out `skip` (encoders that opened but
    then failed to encode, such as an NVENC session limit)."""
    import av
    names = [n for n in ENCODERS if n not in skip] or list(ENCODERS)
    errors = []
    for name in names:
        try:
            enc = av.CodecContext.create(name, "w")
            enc.width, enc.height, enc.pix_fmt = width, height, "yuv420p"
            enc.time_base = Fraction(1, 1000)
            enc.framerate = Fraction(60, 1)
            enc.gop_size = 1 << 30  # key frames only when asked for
            enc.max_b_frames = 0
            enc.options = dict(ENCODERS[name])
            enc.open()
            return enc
        except Exception as e:  # any failure to open means: try the next one
            errors.append(f"{name}: {e}")
    raise RuntimeError("no H.264 encoder works: " + "; ".join(errors))


def open_decoder():
    import av
    import av.codec.context
    d = av.CodecContext.create("h264", "r")
    # Hand out each frame as soon as it is decoded: iOS doesn't declare that frames never need
    # reordering, and a decoder waiting for a reorder would hold every frame until the next one
    # arrives (the phone sends frames only on change).
    d.flags |= av.codec.context.Flags.low_delay
    d.thread_type = "SLICE"  # frame threading would add a frame of delay per thread
    d.thread_count = 4
    return d


def _even(n: float) -> int:
    return max(2, int(n) // 2 * 2)


class Subscription:
    """One viewer's feed, read from asyncio: `await get()` gives the config (a dict) first, then pictures
    (bytes) starting with a key frame, or None once closed. A viewer that falls behind by more than about
    a quarter of a second loses its backlog and continues with a new key frame."""

    def __init__(self, hub: "VideoHub", loop) -> None:
        import asyncio
        self._hub, self._loop = hub, loop
        self._items: collections.deque = collections.deque()  # (loop time, config dict | picture bytes)
        self._event = asyncio.Event()
        self._synced = False  # has had a key frame since the last config
        self.closed = False
        self.dropped = 0

    # runs on the loop thread
    def _push_config(self, config: dict) -> None:
        if self.closed:
            return
        self._items.clear()  # pictures of an older config would not decode with this one
        self._items.append((self._loop.time(), config))
        self._synced = False
        self._event.set()

    def _push_picture(self, picture: bytes, key: bool) -> None:
        if self.closed:
            return
        now = self._loop.time()
        if self._items and (now - self._items[0][0] > BACKLOG or len(self._items) > BACKLOG_ITEMS):
            self.dropped += sum(1 for _, x in self._items if isinstance(x, bytes))
            self._items = collections.deque(x for x in self._items if isinstance(x[1], dict))
            self._synced = False
            self._hub.request_key()
        if not self._synced:
            if not key:
                return
            self._synced = True
        self._items.append((now, picture))
        self._event.set()

    def _shutdown(self) -> None:
        self.closed = True
        self._event.set()

    async def get(self):
        while True:
            if self._items:
                return self._items.popleft()[1]
            if self.closed:
                return None
            self._event.clear()
            await self._event.wait()

    def close(self) -> None:
        self.closed = True
        self._hub._unsubscribe(self)
        self._event.set()


class VideoHub:
    def __init__(self, port: int = 0, screen_width: float | Callable[[], float] | None = None,
                 mirroring: Callable[[], bool] | None = None, log: Callable[[str], None] | None = None) -> None:
        """Listen for the phone's RTP on 127.0.0.1:`port` (0: any; see `.port`). `screen_width` is the phone
        screen's width in points (a number or a callable) and `mirroring` says whether the source reports
        mirroring as on; by default the hub is live while frames keep arriving."""
        self._screen_width = screen_width
        self._mirroring = mirroring
        self._log = log or (lambda message: None)
        self._decoder = open_decoder()
        self._depay = Depacketizer()
        self._cv = threading.Condition()
        self._stop = threading.Event()
        self._last = None  # the last decoded frame (av.VideoFrame)
        self._pending = None  # the newest decoded frame that is not encoded yet
        self._pending_at = 0.0
        self._last_frame_at = -math.inf
        self._key_wanted = False
        self._preset = "balanced"
        self._subs: list[Subscription] = []
        self._encoder = None
        self._encoder_name: str | None = None
        self._failures: dict[str, int] = {}  # encoder name -> encode failures in a row
        self._enc_pts = -1
        self._t0 = time.monotonic()
        self._config: dict | None = None
        self._config_key: tuple | None = None  # (width, height, sps, pps) of the config
        self._config_mono = 0.0
        self._size: tuple[int, int] | None = None
        self._counts = {"received": 0, "decoded": 0, "encoded": 0, "errors": 0}
        self._restarts = 0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 << 20)
            self._sock.bind(("127.0.0.1", port))
        except OSError:
            self._sock.close()
            raise
        self._sock.settimeout(0.2)  # so the receiver notices `close`
        self.port: int = self._sock.getsockname()[1]
        self._threads = [threading.Thread(target=self._receive_loop, name="video-rx", daemon=True),
                         threading.Thread(target=self._encode_loop, name="video-enc", daemon=True)]
        self._closed = False
        for t in self._threads:
            t.start()

    # ---- state ----
    @property
    def live(self) -> bool:
        """Frames arrived within the last 3 s, or a last frame exists and the source says mirroring is on."""
        if time.monotonic() - self._last_frame_at < LIVE_WINDOW:
            return True
        return self._last is not None and self._mirroring is not None and bool(self._mirroring())

    def stats(self) -> dict:
        return {**self._counts, "lost": self._depay.lost, "restarts": self._depay.restarts,
                "viewers": len(self._subs), "encoder": self._encoder_name,
                "size": list(self._size) if self._size else None, "quality": self._preset}

    # ---- viewers ----
    def subscribe(self) -> Subscription:
        """A feed for one viewer; call from the event loop. It starts with the config, then a key frame."""
        import asyncio
        sub = Subscription(self, asyncio.get_running_loop())
        with self._cv:
            self._subs.append(sub)
            if self._config is not None:
                sub._push_config(self._config)  # a later config reaches it through _publish_config
            self._key_wanted = True
            self._cv.notify()
        return sub

    def _unsubscribe(self, sub: Subscription) -> None:
        with self._cv:
            if sub in self._subs:
                self._subs.remove(sub)

    def request_key(self) -> None:
        """Encode the last frame again as a key frame."""
        with self._cv:
            self._key_wanted = True
            self._cv.notify()

    def set_quality(self, preset: str) -> None:
        """"sharp", "balanced" or "light": the encoded width becomes at least 2.0 / 1.5 / 0.75 px per screen
        point, never more than the phone's own; a new size means a new config."""
        if preset not in DENSITY:
            raise ValueError(f"preset must be one of {', '.join(DENSITY)}")
        with self._cv:
            self._preset = preset
            self._key_wanted = True
            self._cv.notify()

    def _target_size(self, width: int, height: int) -> tuple[int, int]:
        points = self._screen_width() if callable(self._screen_width) else self._screen_width
        src_w, src_h = _even(width), _even(height)
        if not points or points <= 0:
            return src_w, src_h
        out_w = min(src_w, _even(math.ceil(points * DENSITY[self._preset] / 2) * 2))
        if out_w >= src_w:
            return src_w, src_h
        return out_w, _even(round(out_w * height / width))

    # ---- in: RTP, decoded once ----
    def _receive_loop(self) -> None:
        import av
        while not self._stop.is_set():
            try:
                pkt = self._sock.recv(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                for au in self._depay.push(pkt):
                    self._decode(au, av)
            except Exception as e:  # one bad packet must not end the hub's only input
                self._log(f"video receive error: {e}")
            if self._depay.restarts != self._restarts:
                self._restarts = self._depay.restarts
                self._log("video: a new stream started (the phone started mirroring again)")

    def _decode(self, au: bytes, av) -> None:
        self._counts["received"] += 1
        try:
            frames = self._decoder.decode(av.Packet(au))
        except av.error.FFmpegError as e:
            self._counts["errors"] += 1
            if self._counts["errors"] in (1, 10, 100) or self._counts["errors"] % 1000 == 0:
                self._log(f"video decode error ({self._counts['errors']} so far): {e}")
            return
        for frame in frames:
            self._counts["decoded"] += 1
            now = time.monotonic()
            with self._cv:
                self._last = self._pending = frame
                self._pending_at = self._last_frame_at = now
                self._cv.notify()

    # ---- out: encoded again, to every viewer ----
    def _encode_loop(self) -> None:
        while True:
            with self._cv:
                while not self._stop.is_set() and self._pending is None and not (
                        self._key_wanted and self._last is not None):
                    self._cv.wait()
                if self._stop.is_set():
                    return
                if self._pending is not None:
                    frame, at = self._pending, self._pending_at
                else:
                    frame, at = self._last, time.monotonic()
                key = self._key_wanted
                self._pending, self._key_wanted = None, False
            try:
                self._encode(frame, key, at)
                if self._encoder_name is not None:
                    self._failures.pop(self._encoder_name, None)
            except Exception as e:
                name = self._encoder_name or "?"
                self._failures[name] = self._failures.get(name, 0) + 1
                self._log(f"video encode error ({name}, {self._failures[name]} in a row): {e}")
                self._encoder = None  # open a fresh one with the next frame
                if self._stop.wait(0.5):  # don't spin on an error that repeats
                    return
                with self._cv:
                    self._key_wanted = True

    def _encode(self, frame, key: bool, at: float) -> None:
        from av.video.frame import PictureType
        size = self._target_size(frame.width, frame.height)
        if self._encoder is None or (self._encoder.width, self._encoder.height) != size:
            bad = {n for n, count in self._failures.items() if count >= 3}
            self._encoder = open_encoder(*size, skip=bad)
            self._encoder_name = self._encoder.name
            self._log(f"video: encoding with {self._encoder_name} at {size[0]}x{size[1]}")
            key = True
        if (frame.width, frame.height) != size or frame.format.name != "yuv420p":
            frame = frame.reformat(width=size[0], height=size[1], format="yuv420p")
        self._enc_pts = max(int((time.monotonic() - self._t0) * 1000), self._enc_pts + 1)
        frame.pts = self._enc_pts
        frame.pict_type = PictureType.I if key else PictureType.NONE
        for packet in self._encoder.encode(frame):
            self._counts["encoded"] += 1
            self._emit(bytes(packet), size, at)

    def _emit(self, data: bytes, size: tuple[int, int], at: float) -> None:
        """One encoder packet (Annex B) to every viewer: a new config first if the parameters changed."""
        nals = split_annexb(data)
        types = [n[0] & 0x1F for n in nals]
        key = 5 in types
        if key:
            sps = next((n for n in nals if n[0] & 0x1F == 7), None)
            pps = next((n for n in nals if n[0] & 0x1F == 8), None)
            if sps is None or pps is None:  # an encoder that keeps them in its extradata only
                extra = split_annexb(bytes(self._encoder.extradata or b""))
                sps = sps or next((n for n in extra if n[0] & 0x1F == 7), None)
                pps = pps or next((n for n in extra if n[0] & 0x1F == 8), None)
            if sps is not None and pps is not None and (*size, sps, pps) != self._config_key:
                self._publish_config(size, sps, pps)
        picture = [n for n in nals if n[0] & 0x1F not in DROPPED_NALS]
        if not picture or self._config is None:
            return
        ms = max(0, int((at - self._config_mono) * 1000)) & 0xFFFFFFFF
        payload = HEADER.pack(1 if key else 0, ms) + to_avcc(picture)
        with self._cv:
            for sub in list(self._subs):
                self._send(sub, sub._push_picture, payload, key)

    def _publish_config(self, size: tuple[int, int], sps: bytes, pps: bytes) -> None:
        config = {"type": "config", "codec": codec_string(sps), "width": size[0], "height": size[1],
                  "description": base64.b64encode(make_avcc(sps, pps)).decode("ascii"),
                  "at": int(time.time() * 1000), "coverage": "screen", "source": "airplay"}
        with self._cv:
            self._config, self._config_key, self._config_mono = config, (*size, sps, pps), time.monotonic()
            self._size = size
            for sub in list(self._subs):
                self._send(sub, sub._push_config, config)

    def _send(self, sub: Subscription, fn, *args) -> None:
        try:
            sub._loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:  # its event loop is gone
            if sub in self._subs:
                self._subs.remove(sub)

    # ---- shutting down ----
    def close(self) -> None:
        """Stop the threads and the socket; safe to call twice."""
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(timeout=3)
        self._sock.close()
        with self._cv:
            subs, self._subs = self._subs, []
        for sub in subs:
            self._send(sub, sub._shutdown)

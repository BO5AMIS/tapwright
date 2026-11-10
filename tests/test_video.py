"""The H.264 video hub: avcC and codec strings, and end to end offline (a test pattern encoded with
PyAV, sent as RTP over UDP, decoded and encoded again by the hub, read through a subscription)."""
import asyncio
import base64
import socket
import struct

import pytest

from tapwright import video
from tapwright.rtp import split_annexb
from tapwright.video import make_avcc, codec_string, to_avcc

SPS_MAIN = bytes([0x67, 0x4D, 0x40, 0x1F, 0xAC, 0xD9])
SPS_HIGH = bytes([0x67, 0x64, 0x00, 0x1F, 0xAC, 0xD9])
PPS = bytes([0x68, 0xEE, 0x3C, 0x80])


def test_codec_string_and_avcc_from_the_parameter_sets():
    assert codec_string(SPS_MAIN) == "avc1.4d401f"
    record = make_avcc(SPS_MAIN, PPS)
    assert record[:6] == bytes([1, 0x4D, 0x40, 0x1F, 0xFF, 0xE1])
    assert struct.unpack_from("!H", record, 6)[0] == len(SPS_MAIN)
    assert record[8:8 + len(SPS_MAIN)] == SPS_MAIN
    assert record.endswith(bytes([1, 0, len(PPS)]) + PPS)  # no extension for the main profile


def test_a_high_profile_avcc_carries_the_chroma_and_bit_depth_fields():
    record = make_avcc(SPS_HIGH, PPS)
    assert codec_string(SPS_HIGH) == "avc1.64001f"
    assert record[-4:] == bytes([0xFD, 0xF8, 0xF8, 0])  # 4:2:0, 8 bit, 8 bit, no extension SPS


def test_pictures_are_length_prefixed():
    assert to_avcc([b"\x65ab", b"\x41c"]) == b"\x00\x00\x00\x03\x65ab\x00\x00\x00\x02\x41c"


def test_available_says_whether_pyav_imports():
    import importlib.util
    assert video.available() is (importlib.util.find_spec("av") is not None)


# ---- end to end ----
W, H = 360, 640
RAMP = bytes(range(256)) * 4


def make_frame(av, t: int):
    """A moving pattern (diagonal ramps shifting every frame) built without numpy."""
    frame = av.VideoFrame(W, H, "yuv420p")
    for i, (w, h) in enumerate(((W, H), (W // 2, H // 2), (W // 2, H // 2))):
        plane = frame.planes[i]
        rows = []
        for y in range(h):
            off = (y * (2 if i == 0 else 1) + t * 9 + i * 40) % 256
            row = RAMP[off:off + w] if i == 0 else bytes([(RAMP[off] + 64 * i) & 255]) * w
            rows.append(row + b"\x00" * (plane.line_size - w))
        plane.update(b"".join(rows))
    frame.pts = t
    return frame


def encode_pattern(av, frames: int) -> list[bytes]:
    """Annex B access units of a short clip: one key frame, then P frames, as the phone would send."""
    from fractions import Fraction
    enc = av.CodecContext.create("libx264", "w")
    enc.width, enc.height, enc.pix_fmt = W, H, "yuv420p"
    enc.time_base, enc.framerate = Fraction(1, 30), Fraction(30, 1)
    enc.options = {"preset": "ultrafast", "tune": "zerolatency", "x264-params": "keyint=1000:bframes=0"}
    enc.open()
    aus = []
    for t in range(frames):
        aus += [bytes(p) for p in enc.encode(make_frame(av, t))]
    aus += [bytes(p) for p in enc.encode(None)]
    return aus


def rtp(seq: int, ts: int, payload: bytes, marker: bool, ssrc: int = 1) -> bytes:
    return struct.pack("!BBHII", 0x80, (0x80 if marker else 0) | 96, seq, ts, ssrc) + payload


def packetize(au: bytes, seq: int, ts: int, mtu: int = 1000) -> list[bytes]:
    """RTP packets for one access unit: SPS and PPS together in a STAP-A, small NAL units as they are,
    large ones in FU-A fragments; the marker on the last packet."""
    nals = split_annexb(au)
    payloads = []
    if len(nals) >= 2 and nals[0][0] & 0x1F == 7 and nals[1][0] & 0x1F == 8:
        parts = [nals.pop(0), nals.pop(0)]
        payloads.append(bytes([0x78]) + b"".join(struct.pack("!H", len(n)) + n for n in parts))
    for nal in nals:
        if len(nal) <= mtu:
            payloads.append(nal)
            continue
        body = nal[1:]
        for i in range(0, len(body), mtu):
            flags = (0x80 if i == 0 else 0) | (0x40 if i + mtu >= len(body) else 0) | (nal[0] & 0x1F)
            payloads.append(bytes([(nal[0] & 0xE0) | 28, flags]) + body[i:i + mtu])
    return [rtp(seq + n, ts, p, marker=n == len(payloads) - 1) for n, p in enumerate(payloads)]


class Source:
    """Sends access units as RTP to the hub, the way UxPlay would."""

    def __init__(self, port: int, aus: list[bytes]) -> None:
        self.sock, self.port, self.aus = socket.socket(socket.AF_INET, socket.SOCK_DGRAM), port, aus
        self.seq, self.ts = 100, 90000

    async def play(self, count: int | None = None, pause: float = 0.02) -> None:
        for au in self.aus[:count]:
            for pkt in packetize(au, self.seq, self.ts):
                self.sock.sendto(pkt, ("127.0.0.1", self.port))
                self.seq += 1
            self.ts += 3000
            await asyncio.sleep(pause)

    def close(self) -> None:
        self.sock.close()


async def take(sub, count: int, timeout: float = 10.0) -> list:
    """The next `count` items of a subscription."""
    out = []
    for _ in range(count):
        out.append(await asyncio.wait_for(sub.get(), timeout))
    return out


async def next_config(sub, timeout: float = 10.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        item = await asyncio.wait_for(sub.get(), max(0.1, deadline - asyncio.get_running_loop().time()))
        if isinstance(item, dict):
            return item


def avcc_nals(picture: bytes) -> list[bytes]:
    body, out = picture[8:], []
    while body:
        n = struct.unpack_from("!I", body)[0]
        out.append(body[4:4 + n])
        body = body[4 + n:]
    return out


def is_key(picture: bytes) -> bool:
    return bool(picture[0] & 1)


def test_the_hub_re_encodes_the_phones_stream_for_any_viewer():
    av = pytest.importorskip("av")
    aus = encode_pattern(av, 20)

    async def run():
        hub = video.VideoHub(screen_width=180)  # points: balanced = 1.5 px/pt = 270 px wide
        source = Source(hub.port, aus)
        sub = hub.subscribe()
        try:
            assert not hub.live
            playing = asyncio.create_task(source.play())  # the viewer reads while the phone "streams"
            config = await asyncio.wait_for(sub.get(), 10)
            assert isinstance(config, dict) and config["type"] == "config"
            assert (config["width"], config["height"]) == (270, 480)
            assert config["coverage"] == "screen" and config["source"] == "airplay"
            avcc = base64.b64decode(config["description"])
            assert avcc[0] == 1 and "avc1." + avcc[1:4].hex() == config["codec"]
            assert avcc[4] & 3 == 3  # NAL units are length-prefixed with 4 bytes
            sps_len = struct.unpack_from("!H", avcc, 6)[0]
            assert avcc[8] & 0x1F == 7 and avcc[8:11] == bytes([0x67]) + avcc[1:3]
            assert avcc[8 + sps_len] == 1 and avcc[11 + sps_len] & 0x1F == 8  # one PPS

            first = await asyncio.wait_for(sub.get(), 10)
            pictures = [first] + await take(sub, 3)
            await playing
            assert all(isinstance(p, bytes) for p in pictures)
            assert is_key(first), "the first picture is a key frame"
            assert hub.live and hub.stats()["decoded"] >= 15 and hub.stats()["lost"] == 0
            for p in pictures:
                assert p[0] & 0xFE == 0 and p[1:4] == b"\x00\x00\x00"  # other flag bits and the spare bytes are 0
                assert not {n[0] & 0x1F for n in avcc_nals(p)} & {6, 7, 8, 9}  # no parameter sets, SEI or AUD

            # a fresh decoder, fed config and pictures the way WebCodecs would be
            decoder = av.CodecContext.create("h264", "r")
            decoder.extradata = avcc
            frames = []
            for p in pictures:
                frames += decoder.decode(av.Packet(p[8:]))
            assert frames and all((f.width, f.height) == (270, 480) for f in frames)

            # a viewer that joins late gets the config and a key frame of the last picture
            late = hub.subscribe()
            late_config, late_first = await take(late, 2)
            assert late_config["type"] == "config" and late_config["description"] == config["description"]
            assert is_key(late_first)
            late.close()

            # a key frame on request
            while sub._items:  # drain what the viewer has not read
                await sub.get()
            hub.request_key()
            while True:
                picture = await asyncio.wait_for(sub.get(), 10)
                if isinstance(picture, bytes) and is_key(picture):
                    break
            assert hub.stats()["viewers"] == 1
        finally:
            source.close()
            hub.close()
            hub.close()  # twice is fine

    asyncio.run(run())


def test_a_lower_quality_makes_a_new_smaller_config():
    av = pytest.importorskip("av")
    aus = encode_pattern(av, 6)

    async def run():
        hub = video.VideoHub(screen_width=180)
        hub.set_quality("sharp")  # 2.0 px/pt: 360 px, the source's own size (never upscaled)
        source = Source(hub.port, aus)
        sub = hub.subscribe()
        try:
            await source.play()
            big = await next_config(sub)
            assert (big["width"], big["height"]) == (360, 640)
            hub.set_quality("light")  # 0.75 px/pt: 135, rounded up to an even 136
            small = await next_config(sub)
            assert (small["width"], small["height"]) == (136, 242)
            assert small["at"] >= big["at"] and small["description"] != big["description"]
            picture = await asyncio.wait_for(sub.get(), 10)
            assert isinstance(picture, bytes) and is_key(picture)
            with pytest.raises(ValueError):
                hub.set_quality("ultra")
        finally:
            source.close()
            hub.close()

    asyncio.run(run())


def test_a_viewer_that_falls_behind_is_resynced_with_a_key_frame():
    av = pytest.importorskip("av")
    aus = encode_pattern(av, 40)

    async def run():
        hub = video.VideoHub(screen_width=180)
        source = Source(hub.port, aus)
        slow = hub.subscribe()
        try:
            await source.play(3)
            await asyncio.sleep(0.5)  # `slow` reads nothing: more than a quarter of a second of backlog
            await source.play(None, pause=0.03)
            await asyncio.sleep(0.3)
            items = []
            while slow._items:
                items.append(await slow.get())
            pictures = [i for i in items if isinstance(i, bytes)]
            assert slow.dropped > 0
            assert items[0]["type"] == "config" and (not pictures or is_key(pictures[0]))
            assert len(pictures) < 40
        finally:
            source.close()
            hub.close()

    asyncio.run(run())


def test_a_hub_without_frames_is_not_live_and_close_ends_subscriptions():
    pytest.importorskip("av")

    async def run():
        hub = video.VideoHub()
        mirroring = {"on": False}
        assert hub.port > 0 and not hub.live and hub.stats()["viewers"] == 0
        sub = hub.subscribe()
        hub.close()
        assert await asyncio.wait_for(sub.get(), 2) is None
        assert hub.stats()["encoder"] is None
        hub2 = video.VideoHub(mirroring=lambda: mirroring["on"])
        mirroring["on"] = True
        assert not hub2.live  # mirroring is on but no frame has arrived yet
        hub2.close()

    asyncio.run(run())

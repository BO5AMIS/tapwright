"""H.264 RTP depacketizing: single NAL units, STAP-A, FU-A, lost packets and stream restarts."""
import struct

from tapwright.rtp import START, Depacketizer, nal_types, split_annexb


def rtp(seq: int, ts: int, payload: bytes, marker: bool = True, ssrc: int = 7) -> bytes:
    return struct.pack("!BBHII", 0x80, (0x80 if marker else 0) | 96, seq, ts, ssrc) + payload


IDR = bytes([0x65]) + b"\x11" * 20
SPS = bytes([0x67, 0x64, 0x00, 0x1F, 0xAC])
PPS = bytes([0x68, 0xEE, 0x3C, 0x80])
SLICE = bytes([0x41]) + b"\x22" * 10


def test_single_nal_units_make_one_access_unit_each():
    d = Depacketizer()
    assert d.push(rtp(1, 1000, SLICE)) == [START + SLICE]
    assert d.push(rtp(2, 2000, IDR)) == [START + IDR]
    assert d.lost == 0 and d.restarts == 0


def test_several_packets_of_one_frame_wait_for_the_marker():
    d = Depacketizer()
    assert d.push(rtp(1, 1000, SPS, marker=False)) == []
    assert d.push(rtp(2, 1000, PPS, marker=False)) == []
    assert d.push(rtp(3, 1000, IDR)) == [START + SPS + START + PPS + START + IDR]


def test_stap_a_carries_several_nal_units():
    stap = bytes([0x78]) + b"".join(struct.pack("!H", len(n)) + n for n in (SPS, PPS))
    d = Depacketizer()
    assert d.push(rtp(1, 1000, stap, marker=False)) == []
    (au,) = d.push(rtp(2, 1000, IDR))
    assert nal_types(au) == [7, 8, 5]


def fragments(nal: bytes, size: int) -> list[bytes]:
    body, out = nal[1:], []
    for i in range(0, len(body), size):
        flags = (0x80 if i == 0 else 0) | (0x40 if i + size >= len(body) else 0) | (nal[0] & 0x1F)
        out.append(bytes([(nal[0] & 0xE0) | 28, flags]) + body[i:i + size])
    return out


def test_fu_a_fragments_reassemble_the_nal_unit():
    big = bytes([0x65]) + bytes(range(256)) * 4
    parts = fragments(big, 300)
    assert len(parts) > 2
    d = Depacketizer()
    out = []
    for n, part in enumerate(parts):
        out += d.push(rtp(10 + n, 5000, part, marker=n == len(parts) - 1))
    assert out == [START + big]


def test_a_lost_packet_is_counted_and_drops_the_unit_it_belonged_to():
    big = bytes([0x65]) + bytes(range(256)) * 4
    parts = fragments(big, 300)
    d = Depacketizer()
    out = d.push(rtp(1, 1000, SLICE))
    out += d.push(rtp(2, 2000, parts[0], marker=False))
    out += d.push(rtp(4, 2000, parts[2], marker=False))  # sequence number 3 never arrives
    out += d.push(rtp(5, 2000, parts[3], marker=len(parts) == 4))
    assert d.lost == 1
    assert out == [START + SLICE]  # the broken NAL unit is not delivered
    out += d.push(rtp(6, 3000, SLICE))
    assert out[-1] == START + SLICE


def test_a_late_duplicate_is_ignored():
    d = Depacketizer()
    d.push(rtp(5, 1000, SLICE))
    assert d.push(rtp(4, 900, SLICE)) == []
    assert d.lost == 0


def test_a_new_ssrc_is_a_restart_and_drops_what_was_half_done():
    d = Depacketizer()
    d.push(rtp(1, 1000, SPS, marker=False))
    assert d.push(rtp(500, 9000, SLICE, ssrc=99)) == [START + SLICE]  # the SPS of the old stream is gone
    assert d.restarts == 1 and d.lost == 0


def test_a_frame_whose_marker_went_missing_is_flushed_by_the_next_timestamp():
    d = Depacketizer()
    assert d.push(rtp(1, 1000, SLICE, marker=False)) == []
    assert d.push(rtp(2, 2000, IDR)) == [START + SLICE, START + IDR]


def test_garbage_is_ignored():
    d = Depacketizer()
    assert d.push(b"") == [] and d.push(b"\x00" * 20) == [] and d.push(rtp(1, 1, b"")) == []


def test_split_annexb_handles_both_start_code_lengths():
    data = b"\x00\x00\x00\x01" + SPS + b"\x00\x00\x01" + PPS + b"\x00\x00\x00\x01" + IDR
    assert split_annexb(data) == [SPS, PPS, IDR]
    assert split_annexb(b"") == []

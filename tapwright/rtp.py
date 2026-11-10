# adapted from phoneuse (MIT), Aleksey Smolenchuk
"""H.264 over RTP (RFC 6184) back into access units, for the live view's video hub.

UxPlay forwards the phone's AirPlay stream as RTP to localhost. This reassembles single NAL unit
packets, STAP-A aggregates and FU-A fragments into Annex B access units (a start code before each
NAL unit), one per frame. A packet lost on the way drops the NAL unit it belonged to and is counted;
the decoder conceals the rest.
"""
import struct

START = b"\x00\x00\x00\x01"


class Depacketizer:
    def __init__(self) -> None:
        self.nals: list[bytes] = []  # NAL units of the access unit being collected
        self.ts: int | None = None  # its RTP timestamp
        self.ssrc: int | None = None
        self.seq: int | None = None
        self.fu: bytearray | None = None  # the FU-A NAL unit being reassembled
        self.lost = 0  # packets missing (sequence gaps)
        self.restarts = 0  # new streams (SSRC changes: UxPlay or the phone started over)

    def push(self, pkt: bytes) -> list[bytes]:
        """Feed one RTP packet; returns the access units it completed (Annex B)."""
        out: list[bytes] = []
        if len(pkt) < 12 or pkt[0] >> 6 != 2:
            return out
        cc, ext, pad, marker = pkt[0] & 0x0F, pkt[0] & 0x10, pkt[0] & 0x20, pkt[1] & 0x80
        seq, ts, ssrc = struct.unpack_from("!HII", pkt, 2)
        off = 12 + 4 * cc
        if ext:
            if len(pkt) < off + 4:
                return out
            off += 4 + 4 * struct.unpack_from("!H", pkt, off + 2)[0]
        end = len(pkt) - (pkt[-1] if pad else 0)
        payload = pkt[off:end]
        if not payload:
            return out

        if ssrc != self.ssrc:
            if self.ssrc is not None:
                self.restarts += 1
            self.ssrc, self.seq, self.ts, self.nals, self.fu = ssrc, None, None, [], None
        if self.seq is not None:
            gap = (seq - self.seq - 1) & 0xFFFF
            if gap and gap < 0x8000:
                self.lost += gap
                self.fu = None  # the fragment in progress is incomplete
            elif gap >= 0x8000:
                return out  # a late duplicate or reordered packet: too late for it
        self.seq = seq
        if self.ts is not None and ts != self.ts and self.nals:
            out.append(self._flush())  # the previous frame's marker went missing
        self.ts = ts

        kind = payload[0] & 0x1F
        if 1 <= kind <= 23:
            self.nals.append(bytes(payload))
        elif kind == 24:  # STAP-A: 16-bit size, NAL unit, repeated
            i = 1
            while i + 2 <= len(payload):
                n = struct.unpack_from("!H", payload, i)[0]
                i += 2
                if n == 0 or i + n > len(payload):
                    break
                self.nals.append(bytes(payload[i:i + n]))
                i += n
        elif kind == 28 and len(payload) >= 2:  # FU-A
            start, stop = payload[1] & 0x80, payload[1] & 0x40
            if start:
                self.fu = bytearray([(payload[0] & 0xE0) | (payload[1] & 0x1F)])
            if self.fu is not None:
                self.fu += payload[2:]
                if stop:
                    self.nals.append(bytes(self.fu))
                    self.fu = None
        if marker and self.nals:
            out.append(self._flush())
        return out

    def _flush(self) -> bytes:
        au = b"".join(START + n for n in self.nals)
        self.nals = []
        return au


def split_annexb(data: bytes) -> list[bytes]:
    """The NAL units (without start codes) of an Annex B byte string."""
    marks: list[int] = []
    i = 0
    while True:
        i = data.find(b"\x00\x00\x01", i)
        if i < 0:
            break
        marks.append(i)
        i += 3
    nals = []
    for n, start in enumerate(marks):
        stop = marks[n + 1] if n + 1 < len(marks) else len(data)
        nal = data[start + 3:stop].rstrip(b"\x00")  # the zero of a 4-byte start code belongs to the next one
        if nal:
            nals.append(nal)
    return nals


def nal_types(au: bytes) -> list[int]:
    """NAL unit types in an Annex B access unit (for tests and logs)."""
    return [n[0] & 0x1F for n in split_annexb(au)]

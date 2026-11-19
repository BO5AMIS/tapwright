"""The shared contract AGENT-TOOLS 1.17 (§30, token efficiency) is kept byte for byte under its header."""
import hashlib
from pathlib import Path

FROZEN = "880e3db31e164fd36fbc4847a8f078517f821c45d7ac50e93b896a2291b48d20"
MARK = b"<!-- frozen text below -->\n"


def test_the_1_17_copy_is_the_frozen_text_byte_for_byte():
    raw = (Path(__file__).resolve().parent.parent / "docs" / "history" / "AGENT-TOOLS-1.17.md").read_bytes()
    assert hashlib.sha256(raw.split(MARK, 1)[1]).hexdigest() == FROZEN

"""Codex, PR #10 cycle 16: a surrogate pair decoded across passes, and the Arabic Letter Mark."""
import json
import logging

from tapwright import accounts as accountlib


def _loaded(password):
    return accountlib.Loaded(path="x", accounts={"t": accountlib.Account("t", None, password)})


def test_a_surrogate_pair_whose_halves_are_decoded_in_different_passes_is_one_character_again():
    loaded = _loaded("pw\U0001F600abc")
    text = "token=pw\ud83d%5Cude00abc;"
    assert loaded.holds(text)
    out = loaded.scrub(text)
    assert "abc" not in out and out.startswith("token=<") and out.endswith(">;")


def test_the_arabic_letter_mark_is_a_direction_mark():
    loaded = _loaded("abc123")
    for text in ("abc؜123", json.dumps({"token": "abc؜123"}), json.dumps({"token": "abc؜123"}, ensure_ascii=False)):
        assert loaded.holds(text), text
        assert "123" not in loaded.scrub(text)
    assert "؜" not in accountlib.Loaded(path="x", accounts={}).scrub("x") and ord("؜") in accountlib.BIDI_MARKS


def test_ordinary_emoji_text_is_left_alone():
    loaded = _loaded("pw\U0001F600abc")
    assert loaded.scrub("smile \U0001F600 please") == "smile \U0001F600 please"
    assert not loaded.holds("\ud83d\ude00 only")


def test_joining_surrogate_halves_stays_linear():
    """Codex, PR #10 cycle 17: the overlap check scanned every escape for each pair (5.65 s for this line)."""
    import time
    loaded = _loaded("pw\U0001F600abc")
    line = "\ud83d%5Cude00%252520" * 8000
    began = time.perf_counter()
    loaded.scrub(line)
    assert time.perf_counter() - began < 1.0


def test_a_four_byte_percent_escape_can_take_in_up_to_eleven_characters_of_a_value():
    """Codex, PR #10 cycle 18: `abc%F0%9F%98` shown as `a%62c%F0%9F%98%80` (the last escape takes in 9 of its characters)."""
    import json
    loaded = _loaded("abc%F0%9F%98")
    for text in ("token=a%62c%F0%9F%98%80;", json.dumps({"token": "a%62c%F0%9F%98%80"})):
        assert loaded.holds(text), text
        assert "%62c" not in loaded.scrub(text) and "F0%9F" not in loaded.scrub(text)
    assert accountlib.SEAM >= 11


def test_many_escapes_at_one_place_are_looked_up_not_scanned():
    """Codex, PR #10 cycle 19: dropped direction marks all land on one slot; scanning them took 9.98 s for this line."""
    import time
    loaded = _loaded("Ex%4")
    line = "%E2%80%8E" * 16000 + "x" + "%E2%80%8E" * 16000
    began = time.perf_counter()
    loaded.scrub(line)
    assert time.perf_counter() - began < 1.5

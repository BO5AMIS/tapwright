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

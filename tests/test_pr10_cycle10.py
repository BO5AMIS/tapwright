"""Codex, PR #10 cycle 10: one per-character matcher finds an account's value in any encoding, however the encodings are
composed (an apostrophe left as it is beside a percent-encoded slash, as encodeURIComponent writes it), and a recording's
typed text is checked again at the save against the accounts as they are then."""
import json
import random
import time
import urllib.parse
from io import StringIO

import pytest
from test_account_values_at_ingest import assert_absent
from test_account_values_raw_inside import USERNAME, daemon
from test_daemon import finish, make, run, started
from test_pr10_cycle4 import logging_to, with_accounts
from test_pr10_cycle7 import HEADER, pull_with

from tapwright import accounts as accountlib
from tapwright import diagnostics
from tapwright.control import Refused

TRACE = "synth'part/42"  # (synthetic, the review's)
URI_SAFE = "-_.!~*'()"  # what JavaScript's encodeURIComponent leaves as it is


def encode_uri_component(text: str) -> str:
    return urllib.parse.quote(text, safe=URI_SAFE)


def redaction(*values: str) -> accountlib.Redaction:
    return accountlib.Redaction(tuple((f"t{n}", "password", v) for n, v in enumerate(values)))


# ---- 1: the review's trace ----
def test_the_trace_is_what_encodeURIComponent_writes():
    assert encode_uri_component(TRACE) == "synth'part%2F42"


@pytest.mark.parametrize("shown", ["synth'part%2F42", "synth'part%2f42", "synth%27part%2F42", "synth%27part/42",
                                   "synth\\'part\\/42", "synth%5C'part%2F42", "synth'part%252F42"])
def test_the_value_with_an_apostrophe_left_as_it_is_is_held_and_scrubbed(shown):
    loaded = accountlib.Loaded(path="x", accounts={"t": accountlib.Account("t", None, TRACE)})
    text = f'{{"url":"https://example.invalid/?token={shown}"}}'
    assert loaded.holds(text)
    assert loaded.scrub(text) == '{"url":"https://example.invalid/?token=<t\'s password>"}'


def test_the_log_filter_scrubs_the_trace(tmp_path):
    out = StringIO()
    d = with_accounts(make(), tmp_path, password=TRACE)
    log = logging_to(out, d)
    log.info("GET https://example.invalid/?token=synth'part%2F42")
    log.info("token=%s", encode_uri_component(TRACE))
    text = out.getvalue()
    assert "part%2F42" not in text and "synth'part" not in text
    assert text.count("<tester2's password>") == 2


def test_the_crash_export_scrubs_the_trace_and_its_final_check_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "crash_folder", lambda bundle: tmp_path / "crashes" / bundle)
    body = '{"url":"https://example.invalid/?token=synth\'part%2F42"}'
    answer = pull_with(tmp_path, f"{HEADER}{body}\n".encode(), "tester2@example.com", TRACE)
    path = next((tmp_path / "crashes").rglob("*.ips"))
    raw = path.read_text("utf-8")
    assert f"Saved: {path}" in answer
    assert "part%2F42" not in raw and "synth'part" not in raw
    assert json.loads(raw.splitlines()[1]) == {"url": "https://example.invalid/?token=<tester2's password>"}


# ---- 1: every encoding, composed per character and nested ----
VALUES = [
    TRACE,
    "synth/secret?42",
    "secret'\"account-42",
    "pw é‎ 'x\" back\\slash-7",
    "tab\there now-9",
    "emoji\U0001F600-pw+1",
    "a&b=c%20d-31",
    "two\\\\back-slashes",
    "\\\\lead-back\\",
    "línea\nnueva-5",
    "Abc-Secret",
]
BIDI = "‎‏‪‫‬‭‮⁦⁧⁨⁩"


def _pct(c: str, rng: random.Random) -> str:
    out = "".join(f"%{b:02X}" for b in c.encode("utf-8"))
    return out.lower() if rng.random() < 0.5 else out


def per_character(value: str, rng: random.Random) -> str:
    """The value with each character written its own way: itself, percent-encoded, or escaped (JSON or Python), a space
    as `+` or %20, a direction mark now and then between two characters."""
    out = []
    for n, c in enumerate(value):
        if c in BIDI:
            continue  # (the value's own marks: dropped or kept, as ui._text does)
        if n and rng.random() < 0.15:
            out.append(rng.choice(BIDI))
        o = ord(c)
        # (a backslash among escapes is written as an escape: JSON and Python read a bare `\\` or a `%5c` under a JSON
        # layer, right before an escape, as an escaped backslash. Codex, PR #10 cycle 15: a literal backslash is kept)
        choices = (["U"] if c == "\\" else ["lit", "pct", "U"]) + (["u"] if o < 0x10000 else ["pair"]) + \
            (["x"] if o < 0x100 else [])
        if c.isspace():
            choices += ["plus", "spaces"]
        how = rng.choice(choices)
        if how == "lit":
            out.append(c)
        elif how == "pct":
            out.append(_pct(c, rng))
        elif how == "U":
            out.append(f"\\U{o:08x}")
        elif how == "u":
            out.append(f"\\u{o:04x}" if rng.random() < 0.5 else f"\\u{o:04X}")
        elif how == "pair":
            out.append(json.dumps(c)[1:-1])
        elif how == "x":
            out.append(f"\\x{o:02x}")
        elif how == "plus":
            out.append("+")
        else:
            out.append(" \t ")
    return "".join(out)


NESTED = [
    lambda t: json.dumps(t)[1:-1],
    lambda t: json.dumps(t, ensure_ascii=False)[1:-1],
    lambda t: repr(t)[1:-1],
    lambda t: ascii(t)[1:-1],
    lambda t: urllib.parse.quote(t, safe=""),
    lambda t: urllib.parse.quote(t),
    lambda t: urllib.parse.quote_plus(t),
    encode_uri_component,
    lambda t: " ".join(t.split()),
]
PERCENT = {4, 5, 6, 7}  # (the ones that percent-encode: at most two of them, so no character is encoded past three times)


def composed(value: str, rng: random.Random) -> str:
    text = per_character(value, rng) if rng.random() < 0.7 else value.translate(accountlib.BIDI_MARKS)
    percent = 0
    for _ in range(rng.randint(0, 3)):
        pick = rng.randrange(len(NESTED))
        if pick in PERCENT:
            if percent == 2:
                continue
            percent += 1
        text = NESTED[pick](text)
    return text


@pytest.mark.parametrize("value", VALUES)
def test_random_compositions_are_held_and_scrubbed_exactly(value):
    rng = random.Random(f"cycle10-{value}")
    found = redaction(value, "other-value-77")
    for _ in range(150):
        shown = composed(value, rng)
        text = f"left [{shown}] right"
        assert found.holds(text), shown
        assert found.scrub(text) == "left [<t0's password>] right", shown


def test_the_review_and_earlier_cycles_compositions_are_still_found():
    value = "synth/secret?42"
    q = urllib.parse.quote
    for shown in (q("synth%2fsecret%3f42", safe=""), q(q(q(value, safe=""), safe=""), safe=""),
                  "synth%2Fsecret%3F42", json.dumps({"frames": [f"call {value!r}"]}),
                  q(json.dumps({"q": value})), repr(json.dumps([value])), ascii(json.dumps(value, ensure_ascii=False))):
        assert redaction(value).holds(shown), shown
        assert "secret" not in redaction(value).scrub(shown), shown


# ---- 1: no false positives ----
@pytest.mark.parametrize("text", [
    "synth part 42", "synth'part-42", "synthpart/42", "SYNTH'PART/42", "synth'par t/42", "synth'part%2G42",
    "synth'part%2F43", "s y n t h ' p a r t / 4 2", "synth'part\\n/42", "synth'part%%2F42", "the synth part of 42",
    "synth'part", "part/42",
])
def test_ordinary_text_that_shares_characters_is_left_alone(text):
    found = redaction(TRACE)
    assert not found.holds(text)
    assert found.scrub(text) == text


def test_only_escapes_are_case_insensitive_and_scrub_stays_idempotent():
    found = redaction("Abc-Secret")
    assert found.scrub("abc-secret ABC-SECRET Abc%2dSecret Abc\\u002DSecret") == \
        "abc-secret ABC-SECRET <t0's password> <t0's password>"
    tester = accountlib.Redaction((("tester", "password", "test"),))
    once = tester.scrub("a test b")
    assert once == "a <tester's password> b" and tester.scrub(once) == once


def test_a_value_that_holds_another_is_replaced_whole():
    found = accountlib.Redaction((("a", "username", "short-name"), ("b", "password", "x-short-name%2F-y")))
    assert found.scrub("1 x-short-name%252F-y 2 short-name") == "1 <b's password> 2 <a's username>"


# ---- 1: speed ----
def test_a_log_line_is_scrubbed_in_microseconds_with_four_accounts_and_long_passwords():
    entries = []
    for n in range(4):
        entries += [(f"t{n}", "username", f"tester{n}@example.com"),
                    (f"t{n}", "password", (f"synth'pw/{n}-é\"x\\y" * 10)[:64])]
    found = accountlib.Redaction(tuple(entries))
    line = ("2026-11-14 12:00:01 INFO daemon: tap {'target': {'id': 'submit'}} -> settled in 0.4 s; "
            "GET https://example.invalid/api/v1/items?page=2&q=hello%20world 200 OK")
    found.scrub(line)
    began = time.perf_counter()
    for _ in range(200):
        found.scrub(line)
    assert (time.perf_counter() - began) / 200 < 0.002  # (measured about 12 us; the bound leaves room for a slow runner)
    for value in ("two\\\\back-slashes", "\\\\lead-back", "pw-%41-x"):
        found = redaction(value)
        found.scrub("warm")
        pathological = "\\" * 20000 + "%25" * 5000 + "%5C" * 5000
        began = time.perf_counter()
        assert found.scrub(pathological) == pathological
        assert time.perf_counter() - began < 0.5, value  # (measured a few ms: linear in the text)


def test_a_value_escaped_four_times_over_is_still_found():
    value = "q\"uote\\back-8"
    shown = value
    for _ in range(4):
        shown = json.dumps(shown)[1:-1]
    assert redaction(value).scrub(f"[{shown}]") == "[<t0's password>]"


# ---- 2: typed text checked again at the save ----
NEW_USER = "newly-added@example.net"


def record_then_reload(tmp_path, text):
    """The review's trace: type, change the account's username in the file, look (reloads it), stop."""
    out = {}

    async def go():
        d = daemon(tmp_path)
        d.session.field = ""
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "record", {"start": True, "name": "ty"})
        await d.run_tool("a", "type_text", {"target": {"id": "email"}, "text": text, "submit": True, "look": False})
        path = d.ceiling.accounts
        data = json.loads(open(path, encoding="utf-8").read())
        data["accounts"]["tester2"]["username"] = NEW_USER
        open(path, "w", encoding="utf-8").write(json.dumps(data, indent=2) + "\n")
        await d.run_tool("a", "look", {"screenshot": False})  # loads the changed file
        script = tmp_path / "ty.json"
        try:
            out["said"] = (await d.run_tool("a", "record", {"stop": True, "path": str(script)}))["text"]
        except Refused as e:
            out["error"] = e
        out["script"] = script
        await finish(stop, task)
    run(go)
    return out


@pytest.mark.parametrize("text", [f"hi {NEW_USER}", f"{NEW_USER} again", "x" + NEW_USER])
def test_typed_text_that_holds_a_value_after_a_reload_refuses_the_save_naming_the_step(tmp_path, text):
    out = record_then_reload(tmp_path, text)
    assert "error" in out, out
    err = out["error"]
    assert err.code == "bad_params" and "step 1" in str(err)
    assert_absent(NEW_USER, str(err))
    assert not out["script"].exists()


def test_typed_text_that_is_exactly_a_current_value_after_a_reload_is_saved_as_the_variable(tmp_path):
    out = record_then_reload(tmp_path, NEW_USER)
    assert out.get("said", "").startswith("Saved 1 steps"), out
    raw = out["script"].read_text("utf-8")
    assert json.loads(raw)["steps"][0]["text"] == "${account.username}"
    assert_absent(NEW_USER, raw)


def test_typed_text_with_no_value_is_saved_as_typed(tmp_path):
    out = record_then_reload(tmp_path, "hello there")
    assert out.get("said", "").startswith("Saved 1 steps"), out
    assert json.loads(out["script"].read_text("utf-8"))["steps"][0]["text"] == "hello there"


def test_the_old_value_typed_before_the_reload_is_still_refused(tmp_path):
    out = record_then_reload(tmp_path, f"hi {USERNAME}")
    assert "error" in out and "step 1" in str(out["error"])
    assert not out["script"].exists()

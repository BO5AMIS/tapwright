"""Codex, PR #10 cycle 11: direction marks inside whitespace runs, literal `${...}` in version 1 scripts checked, and a
linear overlap sweep."""
import json
import random
import time
from io import StringIO

import pytest
from test_account_values_at_ingest import assert_absent
from test_account_values_raw_inside import daemon
from test_daemon import finish, make, run, started
from test_pr10_cycle4 import logging_to, with_accounts
from test_pr10_cycle7 import HEADER, pull_with

from tapwright import accounts as accountlib
from tapwright import batch, diagnostics
from tapwright.control import Refused

MARK = "‎"  # U+200E, a direction mark
SPACED = f"synth {MARK} secret42"  # (the review's: a mark between two spaces)
DOUBLE = "synth  secret42"  # (two spaces; the mark shown between them)
SHOWN = [SPACED, f"synth {MARK}secret42", f"synth{MARK} secret42", f"synth {MARK} {MARK} secret42",
         "synth \\u200e secret42", "synth %E2%80%8E secret42", "synth\\u200e\\u200e secret42",
         f"synth {MARK}+{MARK} secret42", "synth  secret42"]


def redaction(*values: str) -> accountlib.Redaction:
    return accountlib.Redaction(tuple((f"t{n}", "password", v) for n, v in enumerate(values)))


# ---- 1: direction marks inside whitespace runs ----
@pytest.mark.parametrize("value", [SPACED, DOUBLE])
@pytest.mark.parametrize("shown", SHOWN)
def test_a_mark_inside_a_whitespace_run_is_held_and_scrubbed(value, shown):
    found = redaction(value)
    text = f"token={shown};"
    assert found.holds(text), shown
    assert found.scrub(text) == "token=<t0's password>;", shown


def test_the_value_with_a_mark_between_spaces_matches_itself():
    loaded = accountlib.Loaded(path="x", accounts={"t": accountlib.Account("t", None, SPACED)})
    assert loaded.holds(SPACED)
    assert loaded.scrub(SPACED) == "<t's password>"


def test_marks_inside_whitespace_runs_in_random_compositions():
    rng = random.Random("cycle11")
    marks = "‎‏‪‫‬‭‮⁦⁧⁨⁩"
    for value in (SPACED, DOUBLE, "a   b\tc  d-77", "x \n y-88"):
        found = redaction(value, "other-value-77")
        core = value.translate(accountlib.BIDI_MARKS)
        for _ in range(150):
            shown = "".join(c + (rng.choice(marks) if n < len(core) - 1 and rng.random() < 0.4 else "")
                            + (" " if c.isspace() and rng.random() < 0.3 else "") for n, c in enumerate(core))
            assert found.scrub(f"[{shown}]") == "[<t0's password>]", shown


def test_the_log_filter_scrubs_a_mark_inside_whitespace(tmp_path):
    out = StringIO()
    d = with_accounts(make(), tmp_path, password=SPACED)
    log = logging_to(out, d)
    log.info("token=%s", SPACED)
    log.info("token=%s", f"synth {MARK} {MARK}secret42")
    assert "secret42" not in out.getvalue()
    assert out.getvalue().count("<tester2's password>") == 2


def test_the_crash_export_scrubs_a_mark_inside_whitespace_and_its_final_check_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "crash_folder", lambda bundle: tmp_path / "crashes" / bundle)
    body = json.dumps({"token": SPACED}, ensure_ascii=False)
    answer = pull_with(tmp_path, f"{HEADER}{body}\n".encode(), "tester2@example.com", SPACED)
    path = next((tmp_path / "crashes").rglob("*.ips"))
    raw = path.read_text("utf-8")
    assert f"Saved: {path}" in answer
    assert "secret42" not in raw
    assert json.loads(raw.splitlines()[1]) == {"token": "<tester2's password>"}


# ---- 2: `${...}` in a version 1 script is literal text ----
def test_a_variable_is_kept_only_where_it_is_one():
    text = "a ${account.username} b ${name} c $${ d"
    assert batch.kept(None).split(text)[1::2] == ["${account.username}"]
    assert batch.kept(["name"]).split(text)[1::2] == ["${account.username}", "${name}", "$${"]
    assert batch.kept(["other"]).split(text)[1::2] == ["${account.username}", "$${"]


def test_a_version_1_script_redacts_text_that_looks_like_a_variable():
    data = batch.script("s", None, [{"tool": "type_text", "text": "hi ${secret42}"}])
    out = batch.redacted_content(data, lambda t: t.replace("secret42", "<x>"))
    assert out["steps"][0]["text"] == "hi ${<x>}"
    route = {"format": "agent-tools-script", "version": 2, "variables": ["secret42"],
             "steps": [{"tool": "type_text", "text": "hi ${secret42}"}]}
    assert batch.redacted_content(route, lambda t: t.replace("secret42", "<x>"))["steps"][0]["text"] == "hi ${secret42}"


FRESH = "fresh_login42"


def record_then_reload(tmp_path, text):
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
        data["accounts"]["tester2"]["username"] = FRESH
        open(path, "w", encoding="utf-8").write(json.dumps(data, indent=2) + "\n")
        await d.run_tool("a", "look", {"screenshot": False})
        script = tmp_path / "ty.json"
        try:
            out["said"] = (await d.run_tool("a", "record", {"stop": True, "path": str(script)}))["text"]
        except Refused as e:
            out["error"] = e
        out["script"] = script
        await finish(stop, task)
    run(go)
    return out


@pytest.mark.parametrize("text", [f"hi ${{{FRESH}}}", f"${{{FRESH}}}", f"x ${{{FRESH}}} y"])
def test_a_value_named_inside_a_dollar_brace_in_a_script_refuses_the_save_naming_the_step(tmp_path, text):
    out = record_then_reload(tmp_path, text)
    assert "error" in out, out
    assert "step 1" in str(out["error"])
    assert_absent(FRESH, str(out["error"]))
    assert not out["script"].exists()


def test_a_dollar_brace_that_names_no_value_is_saved_as_typed(tmp_path):
    out = record_then_reload(tmp_path, "hi ${nothing_here}")
    assert out.get("said", "").startswith("Saved 1 steps"), out
    assert json.loads(out["script"].read_text("utf-8"))["steps"][0]["text"] == "hi ${nothing_here}"


# ---- 3: a linear overlap sweep ----
def test_many_matches_in_one_line_are_filtered_in_linear_time():
    found = redaction("abc")
    line = "abc " * 16000
    found.scrub("warm abc")
    began = time.perf_counter()
    out = found.scrub(line)
    took = time.perf_counter() - began
    print(f"many-matches scrub of {len(line)} characters: {took:.3f} s")
    assert out == "<t0's password> " * 16000
    assert took < 0.5, took


def test_the_longest_value_still_wins_an_overlap():
    found = accountlib.Redaction((("a", "username", "short-name"), ("b", "password", "x-short-name-y")))
    assert found.scrub("1 x-short-name-y 2 short-name") == "1 <b's password> 2 <a's username>"


def test_overlapping_values_leave_no_piece_of_either_behind():
    """Lead, after cycle 11: with the earlier match winning, the later value's tail stayed in the text."""
    loaded = accountlib.Loaded(path="x", accounts={"t": accountlib.Account("t", "abcdef", "cdefgh")})
    out = loaded.scrub("token=abcdefgh;")
    assert out.startswith("token=<") and out.endswith(">;")
    assert "gh" not in out and "ab" not in out and "cdef" not in out

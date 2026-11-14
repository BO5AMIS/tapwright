"""Codex, PR #10 cycle 13: overlapping occurrences of one value, and an account's placeholder is literal outside the
fields that are typed."""
import json
import time

import pytest
from test_pr10_cycle12 import recheck
from test_pr10_cycle7 import HEADER, pull_with

from tapwright import accounts as accountlib
from tapwright import batch, diagnostics
from tapwright.control import Refused

USER = "tester2@example.com"


def loaded(password):
    return accountlib.Loaded(path="x", accounts={"tester2": accountlib.Account("tester2", USER, password)})


# ---- 1: overlapping occurrences ----
def test_overlapping_occurrences_of_one_value_are_one_span():
    found = loaded("abcabc")
    assert found.scrub("token=abcabcabc;") == "token=<tester2's password>;"
    assert found.scrub("abcabcabcabc") == "<tester2's password>"
    assert found.scrub("abcabc abcabc") == "<tester2's password> <tester2's password>"


def test_redact_accounts_leaves_no_tail_of_overlapping_occurrences(tmp_path):
    out = accountlib.Redaction((("tester2", "password", "abcabc"),)).scrub("token=abcabcabc;")
    assert out == "token=<tester2's password>;"
    assert "abc;" not in out


def test_the_crash_export_scrubs_overlapping_occurrences_and_its_final_check_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "crash_folder", lambda bundle: tmp_path / "crashes" / bundle)
    body = json.dumps({"token": "abcabcabc"})
    answer = pull_with(tmp_path, f"{HEADER}{body}\n".encode(), USER, "abcabc")
    path = next((tmp_path / "crashes").rglob("*.ips"))
    raw = path.read_text("utf-8")
    assert f"Saved: {path}" in answer
    assert json.loads(raw.splitlines()[1]) == {"token": "<tester2's password>"}


def test_overlapping_occurrences_stay_fast():
    each = accountlib.Redaction((("t", "password", "abc "),))
    text = "abc " * 16000
    start = time.perf_counter()
    each.scrub(text)
    assert time.perf_counter() - start < 0.5
    many = accountlib.Redaction((("t", "password", "aaaa"),))
    start = time.perf_counter()
    assert many.scrub("a" * 20000) == "<t's password>"
    assert time.perf_counter() - start < 0.5


# ---- 2: literal outside typed fields ----
LABEL = "prefix${account.username}suffix"


def test_a_selector_holding_the_whole_value_is_refused_naming_the_step(tmp_path):
    rec = recheck(tmp_path, [{"tool": "tap", "target": {"label": LABEL}}], changed_to=LABEL)
    assert rec.get("unsaved") == [1]
    d_rec = rec
    assert LABEL not in json.dumps({k: v for k, v in d_rec.items() if k == "unsaved"})


@pytest.mark.parametrize("step", [
    {"tool": "tap", "target": {"text": LABEL}},
    {"tool": "tap", "target": {"id": LABEL}},
    {"tool": "tap", "target": {"label": "ok"}, "expect": {"text": LABEL}},
    {"tool": "compare_screen", "name": "x", "ignore": [{"label": LABEL}]},
    {"tool": "tap", "target": {"label": "ok"}, "if": {"text": LABEL}},
])
def test_every_non_typed_field_is_checked_as_a_whole_string(tmp_path, step):
    rec = recheck(tmp_path, [step], changed_to=LABEL)
    assert rec.get("unsaved") == [1]


def test_type_text_with_the_variable_is_still_saved_as_is(tmp_path):
    rec = recheck(tmp_path, [{"tool": "type_text", "text": "${account.username}"}], changed_to=LABEL)
    assert "unsaved" not in rec and "unsaved_text" not in rec
    assert rec["steps"][0]["text"] == "${account.username}"


def test_the_persistence_redactor_keeps_the_variable_only_in_typed_text():
    script = {"format": "agent-tools-script", "version": 1,
              "steps": [{"tool": "tap", "target": {"label": LABEL}},
                        {"tool": "type_text", "text": "${account.username}"}]}
    out = batch.redacted_content(script, lambda t: t.replace(LABEL, "<x>"))
    assert out["steps"][0]["target"]["label"] == "<x>"
    assert out["steps"][1]["text"] == "${account.username}"


def test_the_persistence_redactor_redacts_a_route_selector_whole():
    route = {"format": "agent-tools-script", "version": 2, "variables": ["name"],
             "steps": [{"tool": "tap", "target": {"label": "a${name}b"}},
                       {"tool": "type_text", "text": "${name}"}]}
    out = batch.redacted_content(route, lambda t: t.replace("a${name}b", "<x>"))
    assert out["steps"][0]["target"]["label"] == "<x>"
    assert out["steps"][1]["text"] == "${name}"


def test_the_refusal_names_the_step(tmp_path):
    from test_account_values_raw_inside import daemon
    d = daemon(tmp_path)
    rec = recheck(tmp_path, [{"tool": "tap", "target": {"label": LABEL}}], changed_to=LABEL)
    d._recording["c"] = rec
    with pytest.raises(Refused) as e:
        d._refuse_unsaved("c", rec)
    assert "step 1" in str(e.value) and LABEL not in str(e.value)

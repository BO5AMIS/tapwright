"""Codex, PR #10 cycle 12: a value that holds placeholder-shaped text, `$${` decoded in route literals, and one pass over
the current and the past values."""
import json
import json as _json
from io import StringIO

import pytest
from test_account_values_raw_inside import daemon
from test_pr10_cycle4 import logging_to, with_accounts
from test_pr10_cycle7 import HEADER, pull_with
from test_daemon import make

from tapwright import accounts as accountlib
from tapwright import batch, diagnostics

USER = "tester2@example.com"
PW = "prefix<tester2's username>suffix"


def loaded(username=USER, password=PW):
    return accountlib.Loaded(path="x", accounts={"tester2": accountlib.Account("tester2", username, password)})


# ---- 1: a value that holds a placeholder ----
def test_a_password_holding_a_placeholder_is_held_and_scrubbed_whole():
    found = loaded()
    assert found.holds(PW)
    assert found.scrub(PW) == "<tester2's password>"
    assert found.scrub(f"token={PW};") == "token=<tester2's password>;"


@pytest.mark.parametrize("text", [
    "hi <tester2's username> there", "<tester2's password>", f"a {PW} b <tester2's username>",
    "<tester2's username><tester2's username>", "plain text", f"x<tester2's password>y {USER}"])
def test_scrub_is_idempotent(text):
    found = loaded()
    once = found.scrub(text)
    assert found.scrub(once) == once
    assert accountlib.Redaction((("t", "password", "test"), ("tester", "username", "tester"))).scrub(
        "x <tester's username> <t's password> test") == "x <tester's username> <t's password> <t's password>"


def test_a_standalone_placeholder_is_left_alone():
    found = loaded()
    assert found.scrub("see <tester2's username> now") == "see <tester2's username> now"


def test_the_log_filter_scrubs_a_password_holding_a_placeholder(tmp_path):
    out = StringIO()
    d = with_accounts(make(), tmp_path, password=PW)
    log = logging_to(out, d)
    log.info("token=%s", PW)
    assert "prefix" not in out.getvalue() and "suffix" not in out.getvalue()
    assert "token=<tester2's password>" in out.getvalue()


def test_the_crash_export_scrubs_a_password_holding_a_placeholder_and_its_final_check_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "crash_folder", lambda bundle: tmp_path / "crashes" / bundle)
    body = json.dumps({"token": PW}, ensure_ascii=False)
    answer = pull_with(tmp_path, f"{HEADER}{body}\n".encode(), USER, PW)
    path = next((tmp_path / "crashes").rglob("*.ips"))
    raw = path.read_text("utf-8")
    assert f"Saved: {path}" in answer
    assert "prefix" not in raw
    assert json.loads(raw.splitlines()[1]) == {"token": "<tester2's password>"}


# ---- 2: `$${` is a literal `${` ----
FRESH = "hi ${fresh_login42}"


def recheck(tmp_path, steps, variables=None, *, changed_to=None):
    d = daemon(tmp_path)
    if changed_to is not None:
        path = d.ceiling.accounts
        d.accounts.get(path)
        data = _json.loads(open(path, encoding="utf-8").read())
        data["accounts"]["tester2"]["username"] = changed_to
        open(path, "w", encoding="utf-8").write(_json.dumps(data, indent=2) + "\n")
        d.accounts.get(path)
    rec = {"steps": steps, "variables": variables if variables is not None else []}
    if variables is not None:
        rec["route"] = "literal"
    d._recheck_typed(rec)
    return rec


def test_an_escaped_dollar_brace_that_is_exactly_a_value_is_never_saved_as_text(tmp_path):
    """The review's trace: the username becomes `hi ${fresh_login42}` after the typing. The text decodes to exactly it, so
    the step is saved as the account's variable, never as the literal."""
    rec = recheck(tmp_path, [{"tool": "type_text", "text": "hi $${fresh_login42}"}], [], changed_to=FRESH)
    assert rec["steps"][0]["text"] == "${account.username}"
    assert "fresh_login42" not in json.dumps(rec)
    assert "unsaved_text" not in rec


def test_an_escaped_dollar_brace_that_holds_a_value_with_more_refuses_the_step(tmp_path):
    rec = recheck(tmp_path, [{"tool": "type_text", "text": "hi $${fresh_login42} and more"}], [], changed_to=FRESH)
    assert rec.get("unsaved_text") == [1]
    assert "fresh_login42" not in json.dumps(rec)


def test_an_escaped_dollar_brace_beside_a_declared_variable_is_still_checked(tmp_path):
    rec = recheck(tmp_path, [{"tool": "type_text", "text": "hi $${fresh_login42} ${name}"}], ["name"],
                  changed_to="hi ${fresh_login42} ")
    assert rec.get("unsaved_text") == [1]


def test_an_escaped_dollar_brace_that_holds_no_value_is_saved_as_typed(tmp_path):
    rec = recheck(tmp_path, [{"tool": "type_text", "text": "hi $${other_thing}"}], [], changed_to=FRESH)
    assert "unsaved_text" not in rec
    assert rec["steps"][0]["text"] == "hi $${other_thing}"


def test_the_persistence_redactor_decodes_the_escape():
    route = {"format": "agent-tools-script", "version": 2, "variables": [],
             "steps": [{"tool": "type_text", "text": "hi $${fresh_login42}"}]}
    out = batch.redacted_content(route, lambda t: t.replace(FRESH, "<x>"))
    assert out["steps"][0]["text"] == "<x>"
    other = {"format": "agent-tools-script", "version": 2, "variables": ["name"],
             "steps": [{"tool": "type_text", "text": "a $${b} ${name} c"}]}
    assert batch.redacted_content(other, lambda t: t.replace("zzz", "<x>"))["steps"][0]["text"] == "a $${b} ${name} c"


def test_literal_runs_decode_the_escape():
    assert batch.literal_runs("a $${b} ${name} c", ["name"]) == (["a ${b} ", " c"], True)
    assert batch.literal_runs("a $${b}", None) == (["a $${b}"], False)


# ---- 3: one pass over the current and the past values ----
def reloaded(tmp_path):
    d = with_accounts(make(), tmp_path, username="abcdef", password="cdefgh")
    d.scrub_everywhere("x")  # (the file is read: both values are remembered)
    import os
    path = d.ceiling.accounts
    d.accounts.get(path)
    data = _json.loads(open(path, encoding="utf-8").read())
    data["accounts"]["tester2"]["password"] = "replacement-password-42"
    open(path, "w", encoding="utf-8").write(_json.dumps(data, indent=2) + "\n")
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000_000))
    return d


def test_scrub_everywhere_merges_a_current_and_a_past_value(tmp_path):
    d = reloaded(tmp_path)
    out = d.scrub_everywhere("token=abcdefgh;")
    assert "gh" not in out and "abcdef" not in out
    assert out.startswith("token=<") and out.endswith(">;")


def test_the_log_filter_leaves_no_piece_of_a_current_and_a_past_value(tmp_path):
    d = reloaded(tmp_path)
    out = StringIO()
    log = logging_to(out, d)
    log.info("token=%s", "abcdefgh")
    assert "gh" not in out.getvalue().replace("<tester2's", "")


def test_the_crash_sanitizer_leaves_no_piece_of_a_current_and_a_past_value(tmp_path):
    d = reloaded(tmp_path)
    clean = diagnostics.sanitized_report(b'{"token": "abcdefgh"}', d.scrub_everywhere).decode()
    assert "gh" not in clean.replace("<tester2's", "") and "abcdef" not in clean

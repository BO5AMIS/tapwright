"""Codex, PR #10 cycle 9: URL-encoded values found by decoding whole tokens, and values from before a reload refused in
recordings."""
import json
from io import StringIO
from urllib.parse import quote

import pytest
from test_account_values_at_ingest import assert_absent
from test_account_values_raw_inside import USERNAME, daemon
from test_daemon import finish, make, started
from test_daemon import run
from test_diagnostics import tuned
from test_pr10_cycle4 import logging_to, with_accounts
from test_pr10_cycle7 import HEADER, pull_with

from tapwright import accounts as accountlib
from tapwright import diagnostics
from tapwright.control import Refused

SECRET = "synth/secret?42"


def loaded():
    return accountlib.Loaded(path="x", accounts={"t": accountlib.Account("t", None, SECRET)})


COMPOSED = quote("synth%2fsecret%3f42", safe="")


def test_the_review_trace_is_what_it_says():
    assert COMPOSED == "synth%252fsecret%253f42"


@pytest.mark.parametrize("token", [
    COMPOSED,
    quote(quote(quote(SECRET, safe=""), safe=""), safe=""),  # three layers
    "synth%2Fsecret%3F42", "synth%2fsecret%3F42", "synth%252Fsecret%253f42",  # mixed-case escapes
    quote(quote("synth%2fsecret%3F42", safe=""), safe=""),
])
def test_a_token_whose_decoding_holds_the_value_is_replaced_whole(token):
    assert loaded().holds(token)
    assert loaded().scrub(f"a {token} b") == "a <t's password> b"


def test_a_url_with_the_value_in_a_query_parameter_keeps_the_text_around_it():
    text = f"GET https://example.invalid/cb?redirect={COMPOSED}&x=1 done"
    assert loaded().scrub(text) == "GET <t's password> done"
    wrapped = f'{{"url":"https://example.invalid/?r={COMPOSED}&x=1","n":1}}'
    assert loaded().scrub(wrapped) == '{"url":"<t\'s password>","n":1}'


def test_a_token_with_a_percent_and_no_value_is_left_alone():
    text = "GET /a%20b?q=100%25 ok"
    assert loaded().scrub(text) == text and not loaded().holds(text)


def test_the_log_filter_scrubs_a_composed_encoding(tmp_path):
    out = StringIO()
    d = with_accounts(make(), tmp_path, password=SECRET)
    logging_to(out, d).info(f"GET /?redirect={COMPOSED}")
    assert "secret" not in out.getvalue() and "<tester2's password>" in out.getvalue()


def test_the_crash_export_scrubs_the_review_trace(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "crash_folder", lambda bundle: tmp_path / "crashes" / bundle)
    body = f'{{"url":"https://example.invalid/?redirect={COMPOSED}"}}'
    answer = pull_with(tmp_path, f"{HEADER}{body}\n".encode(), "tester2@example.com", SECRET)
    path = next((tmp_path / "crashes").rglob("*.ips"))
    raw = path.read_text("utf-8")
    assert f"Saved: {path}" in answer
    assert "252f" not in raw and "secret" not in raw
    assert "<tester2's password>" in raw


def test_redact_accounts_scrubs_the_review_trace(tmp_path):
    out = StringIO()
    d = with_accounts(make(), tmp_path, password=SECRET)
    log = logging_to(out, d)
    log.info("redirect=%s", COMPOSED)
    assert COMPOSED not in out.getvalue()


# ---- values from before a reload ----
OLD_USER = USERNAME
NEW_USER = "new-user99@example.net"


def reload_then_record(tmp_path, text, stop_it=True):
    out = {}

    async def go():
        d = daemon(tmp_path)
        d.session.field = ""
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        path = d.ceiling.accounts
        data = json.loads(open(path, encoding="utf-8").read())
        data["accounts"]["tester2"]["username"] = NEW_USER
        data["accounts"]["tester2"]["password"] = "other-pass-7788"
        open(path, "w", encoding="utf-8").write(json.dumps(data, indent=2))
        await d.run_tool("a", "look", {"screenshot": False})  # loads the new file
        await d.run_tool("a", "record", {"start": True, "name": "ty"})
        await d.run_tool("a", "type_text", {"target": {"id": "email"}, "text": text, "submit": True, "look": False})
        script = tmp_path / "ty.json"
        try:
            out["said"] = (await d.run_tool("a", "record", {"stop": True, "path": str(script)}))["text"]
        except Refused as e:
            out["error"] = e
        out["script"] = script
        await finish(stop, task)
    run(go)
    return out


@pytest.mark.parametrize("text", [OLD_USER, f"hi {OLD_USER}", "pw-secret-0042"])
def test_a_value_from_before_a_reload_refuses_the_save_naming_the_step(tmp_path, text):
    out = reload_then_record(tmp_path, text)
    assert "error" in out, out
    err = out["error"]
    assert err.code == "bad_params" and "step 1" in str(err)
    assert_absent(OLD_USER, str(err))
    assert not out["script"].exists()


def test_a_current_value_is_still_saved_as_the_variable(tmp_path):
    out = reload_then_record(tmp_path, NEW_USER)
    assert out["said"].startswith("Saved 1 steps"), out
    raw = out["script"].read_text("utf-8")
    assert json.loads(raw)["steps"][0]["text"] == "${account.username}"
    assert_absent(NEW_USER, raw)
    assert_absent(OLD_USER, raw)

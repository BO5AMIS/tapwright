"""Codex, PR #10 cycle 6 (account values): composed forms (a repr inside a JSON document) are redacted everywhere and held
by `holds`, a crash report is scrubbed as decoded JSON, and a recording saves a selector that holds a value from the element
the touch really reached, or refuses the save naming the step, never the value, and never drops a step without a word."""
import json
import time
import urllib.parse
from io import StringIO

import pytest
from test_account_values_at_ingest import assert_absent
from test_account_values_raw_inside import USERNAME, daemon
from test_daemon import finish, make, run, started
from test_pr10_cycle4 import logging_to, with_accounts
from test_pr10_cycle5 import IPS_PW, pulled

from tapwright import accounts as accountlib
from tapwright import diagnostics
from tapwright.control import Refused

PW = "secret'\"account-42"  # (synthetic)


def loaded(value: str = PW) -> accountlib.Loaded:
    return accountlib.Loaded(path="x", accounts={"t": accountlib.Account("t", None, value)})


def framed(value: str) -> str:
    """Codex's trace: a repr in a log line, written into a JSON document."""
    return json.dumps({"frames": [f"call {value!r}"]})


# ---- finding 1: composed forms ----
@pytest.mark.parametrize("shown", [
    framed,
    lambda v: json.dumps({"frames": [f"call {v!r}"]}, ensure_ascii=False),
    lambda v: json.dumps(json.dumps(v)),  # a JSON string inside a JSON string
    lambda v: urllib.parse.quote(json.dumps({"q": v})),  # a JSON body in a URL
    lambda v: repr(json.dumps([v])),
    lambda v: json.dumps(f"{v!r}"),
    lambda v: ascii(json.dumps(v, ensure_ascii=False)),
])
@pytest.mark.parametrize("value", [PW, f"pw é{chr(0x200e)} 'x\" back\\slash-7"])
def test_a_composed_form_is_redacted_and_held(shown, value):
    text = shown(value)
    redaction = loaded(value)
    assert redaction.holds(text)
    out = redaction.scrub(text)
    assert "<t's password>" in out
    for core in ("account-42", "slash-7"):
        assert core not in out


def test_the_matcher_of_one_value_is_quick_to_build_and_cached():
    # (cycle 10: one per-character expression per value replaced the list of whole forms; cycle 14: the value's decoded
    # forms replaced the expression, and the Loaded keeps its Redaction)
    worst = f"secret'\"account-42 é{chr(0x200e)}/x\\y\tz"
    began = time.perf_counter()
    made = accountlib.canon(worst)[0]
    accountlib.Redaction((("t", "password", worst),))
    took = time.perf_counter() - began
    assert made and took < 0.5
    held = loaded(worst)
    assert held._redaction is held._redaction  # (cached)


def test_the_daemon_log_redacts_a_repr_inside_a_json_line(tmp_path):
    out = StringIO()
    d = with_accounts(make(), tmp_path, password=PW)
    log = logging_to(out, d)
    log.info("crash frames %s", framed(PW))
    log.info("%r", {"frames": [f"call {PW!r}"]})
    text = out.getvalue()
    assert "crash frames" in text and "account-42" not in text


def test_a_crash_report_whose_bytes_hold_the_value_only_escaped_is_sanitized(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "crash_folder", lambda bundle: tmp_path / "crashes" / bundle)
    header = json.dumps({"app_name": "Example", "bug_type": "309"}, separators=(",", ":"))
    body = json.dumps({"frames": [f"call {IPS_PW!r}"], "note": "é"}, separators=(",", ":"))  # (non-ASCII escaped)
    report = f"{header}\n{body}\n".encode()
    pulled(tmp_path, report)
    raw = next((tmp_path / "crashes").rglob("*.ips")).read_text("utf-8")
    assert_absent(IPS_PW, raw)
    header_line, body_line = raw.strip().split("\n")
    assert header_line == header  # (unchanged: byte for byte)
    assert json.loads(body_line) == {"frames": ["call <tester2's password>"], "note": "é"}


def test_sanitized_report_scrubs_decoded_json_and_json_lines():
    redact = loaded().scrub
    lines = (framed(PW) + "\n" + json.dumps({"k": PW}, ensure_ascii=True) + "\n").encode()
    out = diagnostics.sanitized_report(lines, redact)
    docs = [json.loads(line) for line in out.decode().splitlines()]
    assert docs == [{"frames": ["call <t's password>"]}, {"k": "<t's password>"}]
    assert diagnostics.sanitized_report(b'{"clean": "words"}\n', redact) == b'{"clean": "words"}\n'
    assert diagnostics.sanitized_report(b"plain text\n", redact) == b"plain text\n"


# ---- finding 3, for values: the element the touch reached; else the save is refused ----
def test_a_tap_by_the_value_before_any_look_is_saved_by_the_element_it_reached(tmp_path):
    async def go():
        d = daemon(tmp_path)
        d.session.buttons = [("profile-btn", USERNAME), ("l1", "Other")]
        stop, task = await started(d)
        await d.run_tool("a", "record", {"start": True, "name": "first"})  # (no look yet: the agent's list is empty)
        await d.run_tool("a", "tap", {"target": {"label": "<tester2's username>"}, "look": False})
        await d.run_tool("a", "tap", {"target": {"id": "l1"}, "look": False})
        script = tmp_path / "first.json"
        said = (await d.run_tool("a", "record", {"stop": True, "path": str(script)}))["text"]
        assert said.startswith("Saved 2 steps"), said
        saved = script.read_text("utf-8")
        assert json.loads(saved)["steps"] == [{"tool": "tap", "target": {"id": "profile-btn"}},
                                              {"tool": "tap", "target": {"id": "l1"}}]
        assert_absent(USERNAME, saved + said)
        await finish(stop, task)
    run(go)


def test_a_value_selector_with_no_id_is_saved_by_the_point_it_reached(tmp_path):
    async def go():
        d = daemon(tmp_path)
        d.session.buttons = [("", USERNAME), ("l1", "Other")]
        stop, task = await started(d)
        await d.run_tool("a", "record", {"start": True, "name": "pt"})
        await d.run_tool("a", "tap", {"target": {"label": USERNAME}, "look": False})  # (the raw value, as an old script has it)
        script = tmp_path / "pt.json"
        await d.run_tool("a", "record", {"stop": True, "path": str(script)})
        steps = json.loads(script.read_text("utf-8"))["steps"]
        assert steps == [{"tool": "tap", "target": {"x": 160.0, "y": 220.0}}]
        await finish(stop, task)
    run(go)


@pytest.mark.parametrize("how", ["script", "route"])
def test_a_wait_on_a_value_refuses_the_save_naming_the_step_not_the_value(tmp_path, how):
    async def go():
        d = daemon(tmp_path)
        d.session.buttons = [("l1", "Other")]
        d.session.texts = [f"Signed in as {USERNAME}"]
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "record", {"start": True, **({"name": "w"} if how == "script" else {"route": "w"})})
        await d.run_tool("a", "tap", {"target": {"id": "l1"}, "look": False})
        await d.run_tool("a", "wait_for", {"text": "Signed in as <tester2's username>", "look": False})
        script = tmp_path / "w.json"
        with pytest.raises(Refused) as caught:
            await d.run_tool("a", "record", {"stop": True, **({"path": str(script)} if how == "script" else {})})
        message = str(caught.value)
        assert caught.value.code == "bad_params" and "step 1 " in message and "Nothing was saved" in message
        assert_absent(USERNAME, message)
        assert "username>" not in message
        assert not script.exists()
        with pytest.raises(Refused):  # the recording has ended
            await d.run_tool("a", "record", {"stop": True})
        await finish(stop, task)
    run(go)


def test_a_step_aimed_by_a_value_that_reached_no_element_refuses_the_save(tmp_path):
    async def go():
        d = daemon(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "record", {"start": True, "name": "lost"})
        d._record_step("a", "tap", {"target": {"id": "l1"}}, {"id": "l1"}, resolved=[])
        d._record_step("a", "tap", {"target": {"label": "<tester2's username>"}}, None, resolved=[])
        with pytest.raises(Refused) as caught:
            await d.run_tool("a", "record", {"stop": True, "path": str(tmp_path / "lost.json")})
        assert "step 2 " in str(caught.value) and "username>" not in str(caught.value)
        await finish(stop, task)
    run(go)

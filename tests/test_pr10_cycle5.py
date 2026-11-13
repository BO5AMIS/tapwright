"""Codex, PR #10 cycle 5: every form of an account value is redacted (Python repr, URL, escapes), what was ever loaded stays
redacted, the exported crash report is sanitized, and the map writer's backstop is schema-aware."""
import json
import logging
import urllib.parse
from io import StringIO

import pytest
from test_account_values_at_ingest import assert_absent
from test_daemon import finish, make, run, started
from test_daemon_accounts import BUNDLE, write_accounts
from test_diagnostics import FakeReport, opened, tuned
from test_pr10_cycle4 import logging_to, with_accounts

from tapwright import accounts as accountlib
from tapwright import cli, diagnostics, mapfile, places

QUOTED = "secret'\"account-42"


# ---- 1: one forms generator ----
@pytest.mark.parametrize("shown", [
    lambda v: repr(v), lambda v: repr(v)[1:-1], lambda v: ascii(v), lambda v: json.dumps(v), lambda v: json.dumps(v)[1:-1],
    lambda v: urllib.parse.quote(v), lambda v: urllib.parse.quote_plus(v), lambda v: repr([v]), lambda v: f"{v!r}"])
def test_every_representation_of_a_value_is_redacted(shown):
    value = f"secret'\"account-42 é{chr(0x200e)}"
    loaded = accountlib.Loaded(path="x", accounts={"t": accountlib.Account("t", None, value)})
    assert_absent(value, loaded.scrub(shown(value)))
    for part in (value, value.translate(accountlib.BIDI_MARKS)):
        assert "secret" not in loaded.scrub(shown(part)).replace("<t's password>", "")


def test_a_password_with_both_quotes_is_redacted_in_a_log_record_a_traceback_and_an_answer(tmp_path):
    out = StringIO()
    d = with_accounts(make(), tmp_path, password=QUOTED)
    log = logging_to(out, d)
    log.info("repr=%r", QUOTED)
    try:
        raise KeyError(QUOTED)
    except KeyError:
        log.exception("failed")
    text = out.getvalue()
    assert "repr=" in text and "Traceback" in text
    assert_absent(QUOTED, text)
    assert "account-42" not in text
    assert "account-42" not in d.scrub_everywhere(f"{QUOTED!r} and {QUOTED}")


# ---- 2: what was ever loaded stays redacted ----
def test_a_value_replaced_in_the_file_is_still_redacted_while_only_the_new_one_is_typed(tmp_path):
    old, new = "secret-account-0123456789", "new-password-abcdefghijklmnop"
    path = write_accounts(tmp_path, {"version": 1, "accounts": {"t": {"password": old}}}, "r.json")
    d = make()
    d.ceiling = type("C", (), {"accounts": path, "brief": False, "maps": None})()
    out = StringIO()
    log = logging_to(out, d)
    log.info("first %s", old)
    write_accounts(tmp_path, {"version": 1, "accounts": {"t": {"password": new, "username": "somebody-else"}}}, "r.json")
    log.info("delayed %s and %s", old, new)
    assert_absent(old, out.getvalue())
    assert_absent(new, out.getvalue())
    assert d._loaded().account("t", None).password == new  # (typing uses the current values only)
    assert not d._loaded().holds(old)


def test_a_file_that_stops_loading_keeps_what_it_once_held_redacted(tmp_path):
    path = write_accounts(tmp_path, {"version": 1, "accounts": {"t": {"password": QUOTED}}}, "g.json")
    store = accountlib.Store()
    assert store.get(path).problem is None
    (tmp_path / "private" / "g.json").write_text("not json", "utf-8")
    assert store.get(path).problem is not None
    assert_absent(QUOTED, store.scrub(f"{QUOTED!r}"))


# ---- 3: the exported crash report ----
IPS_PW = "secret-account-0123456789"


def ips(password: str) -> bytes:
    header = json.dumps({"app_name": "Example", "bug_type": "309"}, separators=(",", ":"))
    body = json.dumps({"exception": {"reason": password, "type": "EXC_CRASH"}, "frames": [f"call {password!r}"]}, separators=(",", ":"))
    return f"{header}\n{body}\n".encode()


def pulled(tmp_path, report: bytes, password: str = IPS_PW):
    out = {}

    async def go():
        d = with_accounts(tuned(make()), tmp_path / "acc", password=password)
        stop, task = await started(d)
        await opened(d)
        await d.run_tool("a", "look", {"screenshot": False})
        d.session.device.crashes["Example"] = [FakeReport(filename="/Example-new.ips")]
        d.session.device.crash_files["/Example-new.ips"] = report
        out["answer"] = (await d.run_tool("a", "crash", {"pull": True}))["text"]
        await finish(stop, task)
    (tmp_path / "acc").mkdir()
    run(go)
    return out["answer"]


def test_the_pulled_crash_report_is_a_sanitized_copy_in_the_same_format(tmp_path, monkeypatch):
    from tapwright import daemon
    monkeypatch.setattr(diagnostics, "crash_folder", lambda bundle: tmp_path / "crashes" / bundle)
    answer = pulled(tmp_path, ips(IPS_PW))
    path = tmp_path / "crashes" / BUNDLE / "Example-new.ips"
    assert f"Saved: {path}" in answer
    raw = path.read_bytes().decode()
    assert_absent(IPS_PW, raw)
    header, body = raw.strip().split("\n")
    assert json.loads(header) == {"app_name": "Example", "bug_type": "309"}  # (the unchanged line is as it was)
    assert json.loads(body)["exception"] == {"reason": "<tester2's password>", "type": "EXC_CRASH"}
    assert daemon is not None


def test_a_sanitized_report_keeps_a_pretty_document_and_a_text_report_valid():
    redact = accountlib.Loaded(path="x", accounts={"t": accountlib.Account("t", None, QUOTED)}).scrub
    pretty = json.dumps({"a": {"reason": QUOTED}, "b": [QUOTED]}, indent=2).encode()
    out = diagnostics.sanitized_report(pretty, redact)
    assert json.loads(out) == {"a": {"reason": "<t's password>"}, "b": ["<t's password>"]} and b"\n  " in out
    assert diagnostics.sanitized_report(f"Exception: {QUOTED!r}\n".encode(), redact) == b"Exception: <t's password>\n"
    assert diagnostics.sanitized_report(b'{"clean": 1}', redact) == b'{"clean": 1}'


def test_replay_copies_beside_a_failing_script_are_the_sanitized_file(tmp_path, monkeypatch):
    from tapwright import client
    monkeypatch.setattr(diagnostics, "crash_folder", lambda bundle: tmp_path / "crashes" / bundle)
    answer = pulled(tmp_path, ips(IPS_PW))
    monkeypatch.setattr(client, "call", lambda *a, **k: {"text": answer})
    script = tmp_path / "s" / "flow.json"
    script.parent.mkdir()
    saved = cli.pull_crash_beside(str(script))
    assert saved is not None
    assert_absent(IPS_PW, (script.parent / "flow.diff" / "flow.crash.ips").read_text("utf-8"))


# ---- 4: the map writer's backstop ----
def test_an_account_named_t_with_the_username_agent_leaves_the_maps_schema_and_an_empty_map_readable(tmp_path):
    loaded = accountlib.load(write_accounts(tmp_path, {"version": 1, "accounts": {"t": {"username": "agent", "password": "tools-map"}}}, "t.json"))
    file = mapfile.MapFile(tmp_path / "maps", BUNDLE, loaded.scrub)
    file.save(places.empty_map(BUNDLE))
    data, why = file.read()
    assert why is None and data["format"] == places.FORMAT and data["app"] == BUNDLE


def test_only_content_fields_are_redacted_and_a_road_holding_a_value_is_dropped(tmp_path):
    loaded = accountlib.load(write_accounts(tmp_path, {"version": 1, "accounts": {"t": {"username": "home", "password": "settings"}}}, "t2.json"))
    data = places.empty_map(BUNDLE)
    data["places"] = {"home": {"key": {"signature": "settings", "parts": {"ids": ["go"], "kinds": [], "tab": None, "layer": None}},
                               "titles": {"latin": "settings"}, "link": "wayfare://settings"}}
    data["roads"] = [{"from": "home", "to": "home", "move": {"tool": "tap", "target": {"id": "go"}}, "names": {"label": "settings"}}]
    data["routes"] = {"settings": "routes/settings.json"}
    file = mapfile.MapFile(tmp_path / "maps", BUNDLE, loaded.scrub)
    file.save(data)
    written, why = file.read()
    assert why is None
    assert written["places"]["home"]["key"]["signature"] == "settings" and written["places"]["home"]["titles"] == {"latin": "settings"}
    assert "settings" in written["routes"] and written["routes"]["settings"] == "routes/settings.json"
    assert written["places"]["home"]["link"] == "wayfare://<t's password>"
    assert written["roads"] == []  # (the road's label held the value: it is no road)


def test_a_redacted_map_that_does_not_read_never_replaces_the_file(tmp_path, caplog):
    loaded = accountlib.load(write_accounts(tmp_path, {"version": 1, "accounts": {"t": {"username": "agent"}}}, "t3.json"))
    good = mapfile.MapFile(tmp_path / "maps", BUNDLE)
    good.save(places.empty_map(BUNDLE))
    before = good.path.read_text("utf-8")
    file = mapfile.MapFile(tmp_path / "maps", BUNDLE, lambda text: loaded.scrub(text))
    broken = places.empty_map(BUNDLE)
    broken["places"] = "agent"  # (a map the reader refuses, whatever the redaction does)
    with caplog.at_level(logging.ERROR, logger="tapwright.mapfile"):
        file._write(broken)
    assert good.path.read_text("utf-8") == before
    assert "not saved" in caplog.text and "agent" not in caplog.text.replace("map.json", "")

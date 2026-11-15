"""The vault through the daemon (docs/AGENT-TOOLS.md §20.1, §20.3, §20.4, §23): a reference typed by name into the right
field, a card only with a person's OK every time (never a grant, none offered), a one-time code once, the plain file's
note, the audit's `vault` and `otp` lines, and no value in any answer. Synthetic values only."""
import asyncio
import json

import pytest
from test_daemon import finish, make, run, started
from test_daemon_accounts import BUNDLE, TypingSession, code_screens, screens, GOES

from tapwright import mapkeeper
from tapwright.actionlog import ActionLog
from tapwright.auditlog import AuditLog
from tapwright.control import Refused

STREET, CARD, CVC, CODE = "12 Synthetic Lane", "4111 1111 1111 1111", "737", "834201"
VAULT = {"version": 2, "vault": {
    "home": {"kind": "address", "street": STREET, "city": "Testville"},
    "visa": {"kind": "card", "number": CARD, "cvc": CVC},
    "trip": {"kind": "fixture", "destination": "Lisbon"},
    "elsewhere": {"kind": "note", "text": "only for another app", "apps": ["com.other.app"]}},
    "otp": {BUNDLE: {"from": ["com.apple.MobileSMS"], "match": "Wayfare"}}}


def vaulted(tmp_path, data=VAULT, code_field=False):
    path = tmp_path / "private" / "vault.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data), "utf-8")
    d = make(TypingSession(code_screens() if code_field else screens(), "login", GOES), audit=AuditLog(tmp_path / "audit"))
    d.maps = mapkeeper.Maps(str(tmp_path / "maps"))
    d.actions = ActionLog(tmp_path / "log")
    d.ceiling = type("C", (), {"accounts": str(path), "brief": False, "maps": None})()
    return d, path


def audit_lines(tmp_path):
    return [json.loads(line) for f in sorted((tmp_path / "audit").glob("*.jsonl"))
            for line in f.read_text("utf-8").splitlines()]


def test_a_vault_reference_is_typed_by_name_and_shown_nowhere(tmp_path):
    async def go():
        d, path = vaulted(tmp_path)
        stop, task = await started(d)
        first = await d.run_tool("a", "look", {"screenshot": False})
        assert f"Note: the vault file {path} is not encrypted; keep test data in it only." in first["text"]
        r = await d.run_tool("a", "type_text", {"target": {"id": "email"}, "text": "${vault.home.street}"})
        assert d.session.typed[-1][0] == STREET and STREET not in r["text"]
        look = await d.run_tool("a", "look", {"screenshot": False})
        assert "<home's street>" in look["text"] and STREET not in look["text"]
        assert [(x["kind"], x.get("name"), x.get("detail")) for x in audit_lines(tmp_path)] == [
            ("vault", "home", "street into text field")]
        await d.run_tool("a", "type_text", {"target": {"id": "password"}, "text": "${vault.trip.destination}"})
        await d.run_tool("a", "type_text", {"target": {"id": "email"}, "text": "${vault.elsewhere.text}"})
        assert len(d.session.typed) == 3
        await finish(stop, task)
    run(go)


def test_a_recording_keeps_the_reference_never_the_value(tmp_path):
    async def go():
        d, _ = vaulted(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "record", {"start": True, "name": "fill"})
        await d.run_tool("a", "type_text", {"target": {"id": "email"}, "text": "${vault.home.street}"})
        script = tmp_path / "fill.json"
        await d.run_tool("a", "record", {"stop": True, "path": str(script)})
        saved = script.read_text("utf-8")
        assert "${vault.home.street}" in saved and STREET not in saved
        await finish(stop, task)
    run(go)


def card_call(d, text="${vault.visa.number}", dialog=True):
    return asyncio.create_task(d.run_tool("a", "type_text", {"target": {"id": "email"}, "text": text, "look": False},
                                          dialog=dialog))






@pytest.mark.parametrize("custom_label", [False, True])
def test_a_one_time_code_is_typed_once_and_shown_nowhere(tmp_path, custom_label):
    async def go():
        d, _ = vaulted(tmp_path, code_field=True)
        if custom_label:
            import dataclasses
            tree = d.session.screens["login"]
            d.session.screens["login"] = dataclasses.replace(tree, items=[
                dataclasses.replace(e, label="Custom input", identifier="custom-input")
                if e.identifier == "otp-code" else e for e in tree.items])
        target_id = "custom-input" if custom_label else "otp-code"
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        with pytest.raises(Refused) as e:  # notifications aren't read yet: no code
            await d.run_tool("a", "type_text", {"target": {"id": target_id}, "text": "${otp." + BUNDLE + "}"})
        assert e.value.code == "not_found" and "no one-time code" in str(e.value)
        d.otp_codes.put(BUNDLE, CODE, "com.apple.MobileSMS", __import__("time").time())
        r = await d.run_tool("a", "type_text", {"target": {"id": target_id}, "text": "${otp." + BUNDLE + "}"})
        assert d.session.typed[-1][0] == CODE and CODE not in r["text"]
        look = await d.run_tool("a", "look", {"screenshot": False})
        assert CODE not in look["text"] and "<one-time code>" in look["text"]
        with pytest.raises(Refused):  # used once
            await d.run_tool("a", "type_text", {"target": {"id": target_id}, "text": "${otp." + BUNDLE + "}"})
        assert [(x["kind"], x.get("app"), x.get("detail")) for x in audit_lines(tmp_path)] == [
            ("otp", BUNDLE, "from com.apple.MobileSMS")]
        await finish(stop, task)
    run(go)


def test_map_and_session_report_list_entry_names_never_values(tmp_path):
    async def go():
        d, _ = vaulted(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        report = (await d.run_tool("a", "session_report", {}))["text"]
        assert "Vault: home (address), visa (card), trip (fixture), elsewhere (note)." in report
        listed = (await d.run_tool("a", "map", {}))["text"]
        assert "Vault: home (address)" in listed
        for value in (STREET, CARD, CVC, "Lisbon", "Testville"):
            assert value not in report and value not in listed
        await finish(stop, task)
    run(go)

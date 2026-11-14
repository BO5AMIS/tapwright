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
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "type_text", {"target": {"id": "password"}, "text": "${vault.trip.destination}"})
        assert e.value.code == "bad_params" and "never goes into a password field" in str(e.value)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "type_text", {"target": {"id": "email"}, "text": "${vault.elsewhere.text}"})
        assert e.value.code == "not_allowed" and "Nothing was typed" in str(e.value)
        assert len(d.session.typed) == 1
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


def test_a_card_is_typed_only_with_a_persons_ok_every_time_and_never_offered_as_a_grant(tmp_path):
    async def go():
        d, _ = vaulted(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        for round_ in range(2):  # every time: the first OK doesn't cover the second
            call = card_call(d)
            out = await d.dialog_poll("a", 0 if round_ == 0 else seq, 2.0)
            ask, seq = out["events"][0], out["seq"]
            assert ask["kind"] == "confirm" and ask["message"].startswith('Allow: type card "visa" number into "')
            assert ask["message"].endswith("?") and CARD not in json.dumps(ask)
            assert not call.done()
            assert d.dialog_answer("a", ask["id"], "allow") is True
            r = await asyncio.wait_for(call, 5)
            assert CARD not in r["text"] and d.session.typed[-1][0] == CARD
            after = await d.dialog_poll("a", seq, 0.15)
            assert after["events"] == [], "no grant is offered for a card"
            seq = after["seq"]
        call = card_call(d, "${vault.visa.cvc}")
        out = await d.dialog_poll("a", seq, 2.0)
        ask = out["events"][0]
        assert 'type card "visa" cvc into' in ask["message"]
        d.dialog_answer("a", ask["id"], "decline")
        with pytest.raises(Refused) as e:
            await asyncio.wait_for(call, 5)
        assert e.value.code == "not_allowed" and "They declined." in str(e.value) and CVC not in str(e.value)
        assert [t[0] for t in d.session.typed] == [CARD, CARD]
        lines = [(x["kind"], x.get("name"), x.get("detail")) for x in audit_lines(tmp_path)]
        assert lines.count(("vault", "visa", "number into text field")) == 2
        await finish(stop, task)
    run(go)


def test_without_a_dialog_a_card_asks_the_live_view_and_types_nothing(tmp_path):
    async def go():
        d, _ = vaulted(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        with pytest.raises(Refused) as e:
            await card_call(d, dialog=False)
        assert e.value.code == "not_allowed" and str(e.value).startswith("needs_confirmation: typing card \"visa\" number")
        assert "(a card is typed only with a person's OK, every time)" in str(e.value) and CARD not in str(e.value)
        assert d.session.typed == []
        await finish(stop, task)
    run(go)


def test_a_one_time_code_is_typed_once_and_shown_nowhere(tmp_path):
    async def go():
        d, _ = vaulted(tmp_path, code_field=True)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        with pytest.raises(Refused) as e:  # notifications aren't read yet: no code
            await d.run_tool("a", "type_text", {"target": {"id": "otp-code"}, "text": "${otp." + BUNDLE + "}"})
        assert e.value.code == "not_found" and "no one-time code" in str(e.value)
        d.otp_codes.put(BUNDLE, CODE, "com.apple.MobileSMS", __import__("time").time())
        r = await d.run_tool("a", "type_text", {"target": {"id": "otp-code"}, "text": "${otp." + BUNDLE + "}"})
        assert d.session.typed[-1][0] == CODE and CODE not in r["text"]
        look = await d.run_tool("a", "look", {"screenshot": False})
        assert CODE not in look["text"] and "<one-time code>" in look["text"]
        with pytest.raises(Refused):  # used once
            await d.run_tool("a", "type_text", {"target": {"id": "otp-code"}, "text": "${otp." + BUNDLE + "}"})
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

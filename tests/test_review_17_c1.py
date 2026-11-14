"""Codex's review of PR #12, cycle 1: H1 (an account-deletion alert's Delete is never covered by a grant), H2 (a route's
draftsOnly stop comes before any grant), H3 (a draft approval is for the draft the person saw). No phone."""
import asyncio
import datetime
import json

import pytest
from fakes import button, el
from test_daemon import finish, make, run, started

from tapwright import daemon as daemon_module
from tapwright import policy, ui
from tapwright.actionlog import ActionLog
from tapwright.auditlog import AuditLog
from tapwright.control import Refused

APP = "com.example.app"  # the fake session's app
TZ = datetime.timezone(datetime.timedelta(hours=3))
NOW = datetime.datetime(2026, 11, 14, 10, 0, tzinfo=TZ)


def daemon_with(tmp_path, obj: dict):
    audit = AuditLog(tmp_path / "audit", now=lambda: NOW.astimezone(datetime.timezone.utc))
    d = make(audit=audit, grant_clock=lambda: NOW, actions=ActionLog(tmp_path / "log"))
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(obj), encoding="utf-8")
    return d, policy.load(path)


def grant(**over):
    g = {"name": "wayfare_posts", "app": APP, "words": ["Delete"], "until": "2026-11-21", "perDay": 3,
         "draftsOnly": False}
    g.update(over)
    return g


def calls_of(d, tool):
    return [c for c in d.session.calls if (c if isinstance(c, str) else c[0]) == tool]


def uses(tmp_path):
    return sorted(p.name for p in (tmp_path / "policy.json.uses").rglob("*") if p.is_file()) \
        if (tmp_path / "policy.json.uses").exists() else []


async def ask_of(d, since=0):
    out = await d.dialog_poll("a", since, 2.0)
    assert out["events"], "the daemon asked the host nothing"
    return out["events"][0], out["seq"]


# ---- H1: an account-deletion alert's Delete is never covered ----------------------------------------------------------
ALERT = ui.Alert("Delete your account?", ("Cancel", "Delete"), (60.0, 300.0, 300.0, 200.0),
                 message="All your trips go with it.")


def test_h1_alert_accept_in_an_account_deletion_alert_asks_a_person_whatever_the_delete_grant(tmp_path):
    async def go():
        d, rules = daemon_with(tmp_path, {"version": 2, "confirm": ["Delete"], "block": [], "grants": [grant()]})
        d.session.tree = [button("e1", "OK", "ok")]
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
        d.session.last_alert = ALERT
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "alert", {"action": "accept"}, policy=rules)
        assert e.value.code == "not_allowed" and "needs_confirmation" in str(e.value)
        assert calls_of(d, "alert") == [] and uses(tmp_path) == []
        await finish(stop, task)
    run(go)


def test_h1_a_tap_on_the_delete_button_of_an_account_deletion_alert_asks_a_person(tmp_path):
    async def go():
        d, rules = daemon_with(tmp_path, {"version": 2, "confirm": ["Delete"], "block": [], "grants": [grant()]})
        d.session.tree = [el("e1", "text", "Delete your account?", rect=(70, 320, 280, 20), in_alert=True),
                          button("e2", "Cancel", "cancel", rect=(70, 440, 130, 44), in_alert=True),
                          button("e3", "Delete", "delete", rect=(220, 440, 130, 44), in_alert=True)]
        d.session.last_alert = ALERT
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "delete"}}, policy=rules)
        assert e.value.code == "not_allowed" and "needs_confirmation" in str(e.value)
        assert calls_of(d, "tap") == [] and uses(tmp_path) == []
        await finish(stop, task)
    run(go)


def test_h1_an_account_deletion_screen_keeps_its_confirm_from_any_grant_and_from_the_offer(tmp_path):
    async def go():
        d, rules = daemon_with(tmp_path, {"version": 2, "confirm": ["Delete"], "block": [], "grants": [grant()]})
        d.session.tree = [el("e1", "header", "Delete account", rect=(10, 60, 300, 30)),
                          el("e2", "text", "This deletes your account and every trip.", rect=(10, 100, 300, 40)),
                          button("e3", "Delete", "delete", rect=(10, 300, 300, 44))]
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
        call = asyncio.create_task(d.run_tool("a", "tap", {"target": {"id": "delete"}, "look": False}, policy=rules,
                                              dialog=True))
        ask, seq = await ask_of(d)
        assert ask["kind"] == "confirm" and calls_of(d, "tap") == [] and uses(tmp_path) == []
        d.dialog_answer("a", ask["id"], "allow")
        await asyncio.wait_for(call, 2)
        assert len(calls_of(d, "tap")) == 1
        out = await d.dialog_poll("a", seq, 0.2)  # no grant is offered for it (§19.6: never for §19.2)
        assert [x for x in out["events"] if x.get("type") == "ask"] == []
        await finish(stop, task)
    run(go)


def test_h1_an_ordinary_delete_alert_is_still_covered(tmp_path):
    async def go():
        d, rules = daemon_with(tmp_path, {"version": 2, "confirm": ["Delete"], "block": [], "grants": [grant()]})
        d.session.tree = [button("e1", "OK", "ok")]
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
        d.session.last_alert = ui.Alert("Delete trip?", ("Cancel", "Delete"), (60.0, 300.0, 300.0, 200.0))
        r = await d.run_tool("a", "alert", {"action": "accept"}, policy=rules)
        assert 'allowed by grant "wayfare_posts"' in r["text"] and len(calls_of(d, "alert")) == 1
        await finish(stop, task)
    run(go)


# ---- H2: a draftsOnly route stops at the draft, whatever the grants ---------------------------------------------------
def chat(d, text="Hello there"):
    d.session.tree = [button("e1", "Post", "post"),
                      el("e2", "input", "Message", "message", value=text, rect=(10, 200, 300, 44))]


def test_h2_an_unrestricted_grant_never_lifts_a_routes_draftsonly(tmp_path):
    async def go():
        d, rules = daemon_with(tmp_path, {"version": 2, "confirm": ["Post"], "block": [],
                                          "grants": [grant(words=["Post"])]})
        chat(d)
        stop, task = await started(d)
        token = daemon_module._DRAFTS_ROUTE.set(True)
        try:
            await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
            dry = await d.run_tool("a", "tap", {"target": {"id": "post"}, "dry_run": True}, policy=rules)
            assert "It would stop at a draft" in dry["text"] and "allowed by grant" not in dry["text"]
            call = asyncio.create_task(d.run_tool("a", "tap", {"target": {"id": "post"}, "look": False}, policy=rules,
                                                  dialog=True))
            ask, _ = await ask_of(d)
            assert ask["kind"] == "draft" and ask["message"] == 'Post this in Example? "Hello there"'
            assert calls_of(d, "tap") == [] and uses(tmp_path) == []
            d.dialog_answer("a", ask["id"], "allow")
            r = await asyncio.wait_for(call, 2)
            assert r["text"].splitlines()[-1] == "Note: sent by a person (in chat)." and len(calls_of(d, "tap")) == 1
            assert uses(tmp_path) == []  # a draft a person sent claims no use of the grant
        finally:
            daemon_module._DRAFTS_ROUTE.reset(token)
        await finish(stop, task)
    run(go)


# ---- H3: an approval is for the draft the person saw ------------------------------------------------------------------
def test_h3_a_draft_changed_after_the_live_view_allow_is_asked_again(tmp_path):
    async def go():
        d, rules = daemon_with(tmp_path, {"version": 2, "confirm": ["Post"], "block": [], "grants": []})
        chat(d)
        stop, task = await started(d)
        token = daemon_module._DRAFTS_ROUTE.set(True)
        try:
            await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "tap", {"target": {"id": "post"}, "look": False}, policy=rules)
            assert e.value.draft is True
            [first] = list(d._confirms.values())
            assert first.message["caption"] == 'Post this in Example? "Hello there"'
            await d.viewer_message({"type": "confirm_answer", "id": first.id, "allow": True})
            chat(d, "Something else entirely")  # the composer's text changes after the person's OK
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "tap", {"target": {"id": "post"}, "look": False}, policy=rules)
            assert e.value.code == "not_allowed" and e.value.draft is True and calls_of(d, "tap") == []
            [again] = list(d._confirms.values())
            assert again is not first and again.message["caption"] == 'Post this in Example? "Something else entirely"'
            # the person allows this one, and the same text goes
            await d.viewer_message({"type": "confirm_answer", "id": again.id, "allow": True})
            r = await d.run_tool("a", "tap", {"target": {"id": "post"}, "look": False}, policy=rules)
            assert r["text"].splitlines()[-1] == "Note: sent by a person (in the live view)."
            assert len(calls_of(d, "tap")) == 1
        finally:
            daemon_module._DRAFTS_ROUTE.reset(token)
        await finish(stop, task)
    run(go)


def test_h3_a_draft_changed_while_the_dialog_waits_is_not_sent(tmp_path):
    async def go():
        d, rules = daemon_with(tmp_path, {"version": 2, "confirm": ["Post"], "block": [], "grants": []})
        chat(d)
        stop, task = await started(d)
        token = daemon_module._DRAFTS_ROUTE.set(True)
        try:
            await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
            call = asyncio.create_task(d.run_tool("a", "tap", {"target": {"id": "post"}, "look": False}, policy=rules,
                                                  dialog=True))
            ask, _ = await ask_of(d)
            assert ask["message"] == 'Post this in Example? "Hello there"'
            chat(d, "Hello thera")  # another client changes one letter while the person reads
            d.dialog_answer("a", ask["id"], "allow")
            with pytest.raises(Refused) as e:
                await asyncio.wait_for(call, 2)
            assert e.value.code == "not_allowed" and e.value.draft is True and "draft changed" in str(e.value)
            assert calls_of(d, "tap") == []
        finally:
            daemon_module._DRAFTS_ROUTE.reset(token)
        await finish(stop, task)
    run(go)


# ---- H4, H5: what the notifications answer shows -----------------------------------------------------------------------
from test_notifications import cell, center, otp_daemon, SMS  # noqa: E402
from test_daemon_accounts import BUNDLE as CODE_APP  # noqa: E402

SECRET_NOTE = "syntheticSECRET_value_1234567890"


@pytest.mark.parametrize("shown", ["123 456", "123-456"])
def test_h4_a_split_code_is_hidden_in_the_notifications_answer(tmp_path, shown):
    async def go():
        source = center(cell("Messages", "now", "+1 555 0100", f"Your Wayfare code is {shown}."))
        d, s, said = otp_daemon(tmp_path, source=source)
        stop, task = await started(d)
        r = await d.run_tool("a", "notifications", {})
        await finish(stop, task)
        assert "Your Wayfare code is <one-time code>." in r["text"], r["text"]
        for form in ("123 456", "123-456", "123456"):
            assert form not in r["text"] and form not in d.scrub_everywhere(f"again {form}")
    run(go)


@pytest.mark.parametrize("where", ["title", "body"])
def test_h5_a_vault_value_across_the_cut_is_redacted_before_it(tmp_path, where):
    async def go():
        data = {"version": 2, "vault": {"n": {"kind": "note", "text": SECRET_NOTE}},
                "otp": {CODE_APP: {"from": [SMS], "match": "Wayfare"}}}
        pad = "pad " * (15 if where == "title" else 45)  # 60 or 180 characters: the value crosses the 80 or 200 cut
        title, body = (pad + SECRET_NOTE, "short body") if where == "title" else ("Trip", pad + SECRET_NOTE)
        source = center(cell("Messages", "now", title, body))
        d, s, said = otp_daemon(tmp_path, data=data, source=source)
        stop, task = await started(d)
        r = await d.run_tool("a", "notifications", {})
        await finish(stop, task)
        text = r["text"]
        assert "n1 Messages" in text
        for i in range(len(SECRET_NOTE) - 11):  # no 12-character piece of it (§20.3's test)
            assert SECRET_NOTE[i:i + 12] not in text, (SECRET_NOTE[i:i + 12], text)
    run(go)

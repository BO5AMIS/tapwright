"""The grant dialog (docs/AGENT-TOOLS.md §19.6) in the daemon: after a person's OK for a policy word, the host is
asked a follow-up question, with the wording and limits of the spec, and the grant is written only from the person's
answers, under the edit lock, with an `approval` audit line. Only a person creates it: the model has no path."""
import asyncio
import datetime
import json

import pytest
from fakes import button
from test_daemon import finish, make, run, started

from tapwright import daemon as daemon_module
from tapwright import dialog, grants, mcp_server, pidlock, policy
from tapwright.auditlog import AuditLog

TZ = datetime.timezone(datetime.timedelta(hours=3))
APP = "com.example.app"
NOW = datetime.datetime(2026, 11, 14, 10, 0, tzinfo=TZ)
CAPTION = 'Allow tapping "Post" in Example up to 3 times a day, until 21 Nov (drafts shown to you first)?'


def setup(tmp_path, *, confirm=("Post", "Delete", "Pay"), block=(), grants_=(), version=2, hooks=None):
    path = tmp_path / "policy.json"
    obj = {"version": version, "confirm": list(confirm), "block": list(block)}
    if version == 2:
        obj["grants"] = list(grants_)
    path.write_text(json.dumps(obj), encoding="utf-8")
    audit = AuditLog(tmp_path / "audit", now=lambda: NOW.astimezone(datetime.timezone.utc))
    d = make(audit=audit, grant_clock=lambda: NOW)
    d.session.tree = [button("e1", "Post", "post"), button("e2", "Delete account", "del-acct"),
                      button("e3", "Pay now", "pay"), button("e4", "Delete draft", "del-draft")]
    rules = policy.load(path)
    if hooks:
        d._books[rules.file] = grants.Book(rules.file, audit, now=lambda: NOW, hooks=hooks)
    return d, rules, path


def audit_lines(tmp_path):
    f = tmp_path / "audit" / "2026-11.jsonl"
    return [json.loads(x) for x in f.read_text("utf-8").splitlines()] if f.exists() else []


def written(path):
    return json.loads(path.read_text("utf-8"))


async def next_ask(d, since=0, client="a"):
    out = await d.dialog_poll(client, since, 2.0)
    assert out["events"], "the daemon asked the host nothing"
    return out["events"][0], out["seq"]


async def tap(d, rules, target="post"):
    await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
    return asyncio.create_task(d.run_tool("a", "tap", {"target": {"id": target}, "look": False}, policy=rules,
                                          dialog=True))


async def quiet(d, seq, client="a"):
    """No further question reaches the host."""
    out = await d.dialog_poll(client, seq, 0.15)
    assert out["events"] == [], out["events"]


def test_after_a_persons_ok_in_chat_the_host_is_offered_a_grant_in_the_specs_words(tmp_path):
    async def go():
        d, rules, path = setup(tmp_path)
        stop, task = await started(d)
        call = await tap(d, rules)
        ok, seq = await next_ask(d)
        assert ok["kind"] == "confirm"
        assert d.dialog_answer("a", ok["id"], "allow") is True
        offer, seq = await next_ask(d, seq)
        assert offer["kind"] == "grant" and offer["message"] == CAPTION
        assert offer["choices"] == ["Allow", "Don't allow"] and offer["id"].startswith("g")
        assert not call.done()  # the touch waits for the follow-up, and no grant exists yet
        assert "grants" in written(path) and written(path)["grants"] == []
        assert d.dialog_answer("a", offer["id"], "allow") is True
        drafts, seq = await next_ask(d, seq)  # Post reaches other people: the separate second question
        assert drafts["kind"] == "drafts" and drafts["message"] == "Send these without showing you each one first?"
        assert drafts["choices"] == ["No", "Yes"]
        assert d.dialog_answer("a", drafts["id"], "no") is True
        r = await asyncio.wait_for(call, 3)
        assert r["text"].splitlines()[-1] == "Note: allowed by a person (in chat)." and "tap" in d.session.calls
        [g] = written(path)["grants"]
        assert g == {"name": "example_post", "app": APP, "words": ["Post"], "perDay": 3, "until": "2026-11-21"}
        assert "draftsOnly" not in g  # No: the file says nothing, and a word that reaches people shows drafts first
        await finish(stop, task)
        assert [(a["kind"], a["name"], a["detail"]) for a in audit_lines(tmp_path)] == [
            ("approval", "Post", "allowed by a person (in chat)"), ("approval", "Post", "grant created")]
    run(go)


def test_yes_to_the_second_question_writes_drafts_only_false_and_nothing_else_does(tmp_path):
    async def go():
        d, rules, path = setup(tmp_path)
        stop, task = await started(d)
        call = await tap(d, rules)
        ok, seq = await next_ask(d)
        d.dialog_answer("a", ok["id"], "allow")
        offer, seq = await next_ask(d, seq)
        d.dialog_answer("a", offer["id"], "allow")
        drafts, seq = await next_ask(d, seq)
        d.dialog_answer("a", drafts["id"], "yes")
        await asyncio.wait_for(call, 3)
        assert written(path)["grants"][0]["draftsOnly"] is False
        loaded = grants.Book(path, None, now=lambda: NOW).load()
        assert [g.drafts_only for g in loaded.grants] == [False]
        await finish(stop, task)
    run(go)


def test_a_word_that_reaches_nobody_has_one_question_and_the_caption_has_no_drafts_part(tmp_path):
    async def go():
        d, rules, path = setup(tmp_path)
        stop, task = await started(d)
        call = await tap(d, rules, "del-draft")
        ok, seq = await next_ask(d)
        d.dialog_answer("a", ok["id"], "allow")
        offer, seq = await next_ask(d, seq)
        assert offer["message"] == 'Allow tapping "Delete" in Example up to 3 times a day, until 21 Nov?'
        d.dialog_answer("a", offer["id"], "allow")
        await asyncio.wait_for(call, 3)
        await quiet(d, seq)
        assert written(path)["grants"][0]["name"] == "example_delete"
        await finish(stop, task)
    run(go)


def test_the_live_views_ok_is_followed_by_the_offer_too(tmp_path):
    async def go():
        d, rules, path = setup(tmp_path)
        stop, task = await started(d)
        call = await tap(d, rules)
        ok, seq = await next_ask(d)
        await d.viewer_message({"type": "confirm_answer", "id": ok["id"], "allow": True})
        close, seq = await next_ask(d, seq)
        assert close["type"] == "close"
        offer, seq = await next_ask(d, seq)
        assert offer["kind"] == "grant"
        d.dialog_answer("a", offer["id"], "decline")
        r = await asyncio.wait_for(call, 3)
        assert r["text"].splitlines()[-1] == "Note: allowed by a person (in the live view)."
        assert written(path)["grants"] == []
        await finish(stop, task)
    run(go)


def test_no_offer_without_a_persons_ok(tmp_path):
    async def go():
        d, rules, path = setup(tmp_path)
        stop, task = await started(d)
        call = await tap(d, rules)
        ok, seq = await next_ask(d)
        d.dialog_answer("a", ok["id"], "decline")
        with pytest.raises(daemon_module.Refused):
            await asyncio.wait_for(call, 3)
        await quiet(d, seq)
        assert written(path)["grants"] == [] and [a["detail"] for a in audit_lines(tmp_path)] == ["declined"]
        await finish(stop, task)
    run(go)


def test_no_offer_when_the_host_has_no_dialog(tmp_path):
    async def go():
        d, rules, path = setup(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
        with pytest.raises(daemon_module.Refused):  # the answer is needs_confirmation, as before
            await d.run_tool("a", "tap", {"target": {"id": "post"}, "look": False}, policy=rules)
        [ask] = list(d._confirms)
        await d.viewer_message({"type": "confirm_answer", "id": ask, "allow": True})
        await d.run_tool("a", "tap", {"target": {"id": "post"}, "look": False}, policy=rules)
        await quiet(d, 0)
        assert written(path)["grants"] == []
        await finish(stop, task)
    run(go)


@pytest.mark.parametrize("target, word", [("pay", "Pay"), ("del-acct", "Delete")])
def test_never_grantable_words_are_never_offered(tmp_path, target, word):
    async def go():
        d, rules, path = setup(tmp_path)
        stop, task = await started(d)
        call = await tap(d, rules, target)
        ok, seq = await next_ask(d)
        assert ok["kind"] == "confirm"
        d.dialog_answer("a", ok["id"], "allow")
        await asyncio.wait_for(call, 3)
        await quiet(d, seq)
        assert written(path)["grants"] == []
        assert [a["detail"] for a in audit_lines(tmp_path)] == ["allowed by a person (in chat)"]
        await finish(stop, task)
    run(go)


def test_a_settings_touch_is_never_offered(tmp_path):
    async def go():
        d, rules, path = setup(tmp_path)
        d.session.app = ("Settings", "com.apple.Preferences")
        stop, task = await started(d)
        call = await tap(d, rules)
        ok, seq = await next_ask(d)
        d.dialog_answer("a", ok["id"], "allow")
        await asyncio.wait_for(call, 3)
        await quiet(d, seq)
        assert written(path)["grants"] == []
        await finish(stop, task)
    run(go)


def test_once_per_app_and_word_in_a_session_whatever_the_answer(tmp_path):
    async def go():
        d, rules, path = setup(tmp_path)
        stop, task = await started(d)
        call = await tap(d, rules)
        ok, seq = await next_ask(d)
        d.dialog_answer("a", ok["id"], "allow")
        offer, seq = await next_ask(d, seq)
        d.dialog_answer("a", offer["id"], "decline")
        await asyncio.wait_for(call, 3)
        call = await tap(d, rules)  # the same word in the same app again: asked for the OK, not for a grant
        ok, seq = await next_ask(d, seq)
        assert ok["kind"] == "confirm"
        d.dialog_answer("a", ok["id"], "allow")
        await asyncio.wait_for(call, 3)
        await quiet(d, seq)
        assert written(path)["grants"] == []
        await finish(stop, task)
    run(go)


def test_inside_a_route_the_grant_is_scoped_to_it_and_the_caption_says_so(tmp_path):
    async def go():
        d, rules, path = setup(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
        token = daemon_module._ROUTES.set(("post_update",))
        try:
            call = asyncio.create_task(d.run_tool("a", "tap", {"target": {"id": "post"}, "look": False},
                                                  policy=rules, dialog=True))
        finally:
            daemon_module._ROUTES.reset(token)
        ok, seq = await next_ask(d)
        d.dialog_answer("a", ok["id"], "allow")
        offer, seq = await next_ask(d, seq)
        assert offer["message"] == ('Allow tapping "Post" in Example up to 3 times a day, only in route post_update, '
                                    'until 21 Nov (drafts shown to you first)?')
        d.dialog_answer("a", offer["id"], "allow")
        drafts, seq = await next_ask(d, seq)
        d.dialog_answer("a", drafts["id"], "no")
        await asyncio.wait_for(call, 3)
        assert written(path)["grants"][0]["route"] == "post_update"
        await finish(stop, task)
    run(go)


def test_no_new_grant_is_offered_where_a_grant_matched_and_ran_out(tmp_path):
    async def go():
        used = {"name": "ex", "app": APP, "words": ["Post"], "until": "2026-11-21", "perDay": 1, "draftsOnly": False}
        d, rules, path = setup(tmp_path, grants_=[used])
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
        await d.run_tool("a", "tap", {"target": {"id": "post"}, "look": False}, policy=rules)  # the grant's one use
        call = asyncio.create_task(d.run_tool("a", "tap", {"target": {"id": "post"}, "look": False}, policy=rules,
                                              dialog=True))
        ok, seq = await next_ask(d)
        d.dialog_answer("a", ok["id"], "allow")
        r = await asyncio.wait_for(call, 3)
        assert "doesn't cover this (1 of 1 today)" in r["text"]
        await quiet(d, seq)
        assert len(written(path)["grants"]) == 1
        await finish(stop, task)
    run(go)


def test_the_policy_file_is_written_under_the_edit_lock_and_a_name_in_use_is_not_taken(tmp_path):
    async def go():
        seen = []
        taken = {"name": "Example_Post", "app": "com.other.app", "words": ["Post"], "until": "2026-11-21"}
        hooks = {"before_write": lambda: seen.append(pidlock.state(tmp_path / "policy.json.edit.lock")[0])}
        d, rules, path = setup(tmp_path, grants_=[taken], hooks=hooks)
        stop, task = await started(d)
        call = await tap(d, rules)
        ok, seq = await next_ask(d)
        d.dialog_answer("a", ok["id"], "allow")
        offer, seq = await next_ask(d, seq)
        d.dialog_answer("a", offer["id"], "allow")
        drafts, seq = await next_ask(d, seq)
        d.dialog_answer("a", drafts["id"], "no")
        await asyncio.wait_for(call, 3)
        assert seen == ["live"]  # this process held the lock while the file was written
        assert not (tmp_path / "policy.json.edit.lock").exists()  # and let go of it
        assert [g["name"] for g in written(path)["grants"]] == ["Example_Post", "example_post_2"]
        await finish(stop, task)
    run(go)


def test_a_held_lock_writes_nothing_and_the_touch_still_goes_through(tmp_path):
    async def go():
        d, rules, path = setup(tmp_path)
        d._books[rules.file] = grants.Book(rules.file, d.audit, now=lambda: NOW, lock_wait=0.1)
        stop, task = await started(d)
        holder = pidlock.take(tmp_path / "policy.json.edit.lock")
        call = await tap(d, rules, "del-draft")
        ok, seq = await next_ask(d)
        d.dialog_answer("a", ok["id"], "allow")
        offer, seq = await next_ask(d, seq)
        d.dialog_answer("a", offer["id"], "allow")
        r = await asyncio.wait_for(call, 3)
        assert "tap" in d.session.calls and r["text"].splitlines()[-1] == "Note: allowed by a person (in chat)."
        assert written(path)["grants"] == []
        assert [a["detail"] for a in audit_lines(tmp_path)] == ["allowed by a person (in chat)"]
        pidlock.release(tmp_path / "policy.json.edit.lock", holder)
        await finish(stop, task)
    run(go)


# ---- only a person creates one ----
def test_only_the_agent_whose_call_waits_can_answer_the_question(tmp_path):
    async def go():
        d, rules, path = setup(tmp_path)
        stop, task = await started(d)
        call = await tap(d, rules, "del-draft")
        ok, seq = await next_ask(d)
        d.dialog_answer("a", ok["id"], "allow")
        offer, seq = await next_ask(d, seq)
        assert d.dialog_answer("b", offer["id"], "allow") is False  # another agent's host
        assert d.dialog_answer("a", "g99-nothing", "allow") is False  # an id nobody was asked
        assert not call.done() and written(path)["grants"] == []
        d.dialog_answer("a", offer["id"], "decline")
        await asyncio.wait_for(call, 3)
        assert d.dialog_answer("a", offer["id"], "allow") is False  # too late, and an answer counts once
        assert written(path)["grants"] == []
        await finish(stop, task)
    run(go)


def test_only_a_plain_allow_creates_anything(tmp_path):
    async def go():
        d, rules, path = setup(tmp_path)
        stop, task = await started(d)
        call = await tap(d, rules, "del-draft")
        ok, seq = await next_ask(d)
        d.dialog_answer("a", ok["id"], "allow")
        offer, seq = await next_ask(d, seq)
        assert d.dialog_answer("a", offer["id"], "yes") is True  # not the word of this question: it is a refusal
        await asyncio.wait_for(call, 3)
        assert written(path)["grants"] == []
        await finish(stop, task)
    run(go)


def test_no_tool_argument_or_header_creates_edits_or_lifts_a_grant():
    tools = {t.name: t for t in mcp_server.server._tool_manager.list_tools()}
    for name, tool in tools.items():
        assert "grant" not in name.lower(), name
        text = json.dumps(tool.parameters).lower()
        assert "grant" not in text and "draftsonly" not in text and "policy" not in text, name
    # a thin call's header can name a policy file, never carry grants: the daemon reads them from the file itself
    sent = {"version": 2, "confirm": [], "block": [], "file": "x.json",
            "grants": [{"name": "g", "app": APP, "words": ["Post"], "until": "2026-11-21"}]}
    carried = policy.parse(sent, header=True)
    assert not hasattr(carried, "grants") and "grants" not in carried.to_obj() and "grants" not in carried.dumps()
    assert dialog.CHOICES[dialog.GRANT] == ["Allow", "Don't allow"] and dialog.CHOICES[dialog.DRAFTS] == ["No", "Yes"]


def test_the_wording_of_the_caption_says_everything_the_grant_allows():
    g = grants.Grant("x", APP, ("Post", "Send"), datetime.date(2026, 11, 21), 3, 1, "post_update", ("22:00", "07:00"), True)
    assert grants.caption(g, "Instagram") == (
        'Allow tapping "Post" or "Send" in Instagram up to 3 times a day and 1 an hour, not between 22:00 and 07:00, '
        'only in route post_update, until 21 Nov (drafts shown to you first)?')
    one = grants.Grant("x", APP, ("Post",), datetime.date(2026, 12, 1), 1)
    assert grants.caption(one, "A" * 100 + '\x07"b"') == (
        f'Allow tapping "Post" in {"A" * 79}… up to 1 time a day, until 1 Dec?')
    three = grants.Grant("x", APP, ("A", "B", "C"), datetime.date(2026, 11, 21))
    assert grants.caption(three, "App").startswith('Allow tapping "A", "B" or "C" in App')

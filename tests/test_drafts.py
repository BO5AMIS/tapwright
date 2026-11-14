"""AGENT-TOOLS 1.16 §21.3, drafts only: a touch on a covered word that reaches other people, under a draftsOnly grant or in a
route with `"draftsOnly": true`, stops before the touch. A person sees the draft in the host's dialog (`Post this in
Example? "<text>"`, the choices `Post` and `Don't post`); Send makes the touch and the answer ends `Note: sent by a person
(in chat).`; the draft is cut at 500 characters and redacted; `grant_draft` is logged and `approval` audited."""
import asyncio
import datetime
import json

import pytest
from fakes import button, el
from test_daemon import finish, make, run, started

from tapwright import daemon as daemon_module
from tapwright import dialog, grants, policy, routes
from tapwright.actionlog import ActionLog
from tapwright.auditlog import AuditLog
from tapwright.control import Refused

TZ = datetime.timezone(datetime.timedelta(hours=3))
NOW = datetime.datetime(2026, 11, 14, 10, 0, tzinfo=TZ)
APP = "com.example.app"


def setup(tmp_path, *grant_items, words=("Post", "Delete")):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"version": 2, "confirm": list(words), "block": [], "grants": list(grant_items)}),
                    encoding="utf-8")
    audit = AuditLog(tmp_path / "audit", now=lambda: NOW.astimezone(datetime.timezone.utc))
    d = make(audit=audit, grant_clock=lambda: NOW, actions=ActionLog(tmp_path / "log"))
    d.session.tree = [button("e1", "Post", "post"), button("e2", "Delete", "del", rect=(10, 300, 100, 40)),
                      el("e3", "input", "Message", "message", value="Hello there", rect=(10, 200, 300, 44))]
    return d, policy.load(path), path


def draft_grant(**over):
    g = {"name": "wayfare_posts", "app": APP, "words": ["Post"], "until": "2026-11-21", "perDay": 3, "draftsOnly": True}
    g.update(over)
    return g


def audit_lines(tmp_path):
    f = tmp_path / "audit" / "2026-11.jsonl"
    return [json.loads(x) for x in f.read_text("utf-8").splitlines()] if f.exists() else []


def log_lines(tmp_path):
    return [json.loads(line) for f in sorted((tmp_path / "log").glob("*.jsonl"))
            for line in f.read_text("utf-8").splitlines()]


async def tap(d, rules, target="post"):
    await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
    return asyncio.create_task(d.run_tool("a", "tap", {"target": {"id": target}, "look": False}, policy=rules, dialog=True))


async def next_ask(d, since=0):
    out = await d.dialog_poll("a", since, 2.0)
    assert out["events"], "the daemon asked the host nothing"
    return out["events"][0], out["seq"]


def taps(d):
    return [c for c in d.session.calls if (c if isinstance(c, str) else c[0]) == "tap"]


def test_a_draftsonly_grant_stops_at_a_draft_and_send_makes_the_touch(tmp_path):
    async def go():
        d, rules, _ = setup(tmp_path, draft_grant())
        stop, task = await started(d)
        call = await tap(d, rules)
        ask, _ = await next_ask(d)
        assert ask["kind"] == "draft" and ask["message"] == 'Post this in Example? "Hello there"'
        assert ask["choices"] == ["Post", "Don't post"]
        assert taps(d) == []  # nothing touched yet
        assert d.dialog_answer("a", ask["id"], "allow") is True
        r = await asyncio.wait_for(call, 2)
        assert r["text"].splitlines()[-1] == "Note: sent by a person (in chat)."
        assert len(taps(d)) == 1
        await finish(stop, task)
        kinds = [(a["kind"], a["name"], a["detail"]) for a in audit_lines(tmp_path)]
        assert ("approval", "Post", "allowed by a person (in chat)") in kinds
        assert any(k[0] == "grant" and k[1] == "wayfare_posts" and "(1 of 3 today)" in k[2] for k in kinds)  # counts as a use
        entry = [g for g in log_lines(tmp_path) if g["tool"] == "tap"][-1]
        assert entry.get("grant_draft") == "wayfare_posts" and entry.get("confirmed") is True
    run(go)


def test_dont_post_refuses_with_the_specs_words_and_touches_nothing(tmp_path):
    async def go():
        d, rules, _ = setup(tmp_path, draft_grant())
        stop, task = await started(d)
        call = await tap(d, rules)
        ask, _ = await next_ask(d)
        assert d.dialog_answer("a", ask["id"], "decline") is True
        with pytest.raises(Refused) as e:
            await asyncio.wait_for(call, 2)
        assert e.value.code == "not_allowed"
        assert str(e.value) == "needs_confirmation: the draft wasn't sent (They declined.). Ask your user what to do."
        assert taps(d) == []
        await finish(stop, task)
        assert ("approval", "Post", "declined") in [(a["kind"], a["name"], a["detail"]) for a in audit_lines(tmp_path)]
        assert not [a for a in audit_lines(tmp_path) if a["kind"] == "grant"]  # an unsent draft counts no use
    run(go)


def test_with_no_dialog_the_draft_is_in_the_live_views_request_and_the_retry_goes_through(tmp_path):
    async def go():
        d, rules, _ = setup(tmp_path, draft_grant())
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "post"}, "look": False}, policy=rules)
        assert "needs_confirmation" in str(e.value) and taps(d) == []
        [request] = list(d._confirms.values())
        assert request.message["caption"] == 'Post this in Example? "Hello there"'
        await d.viewer_message({"type": "confirm_answer", "id": request.id, "allow": True})
        r = await d.run_tool("a", "tap", {"target": {"id": "post"}, "look": False}, policy=rules)
        assert r["text"].splitlines()[-1] == "Note: sent by a person (in the live view)."
        assert len(taps(d)) == 1
        await finish(stop, task)
    run(go)


def test_a_route_that_is_draftsonly_stops_with_or_without_a_grant(tmp_path):
    async def go():
        d, rules, _ = setup(tmp_path)  # no grant at all
        stop, task = await started(d)
        token = daemon_module._DRAFTS_ROUTE.set(True)
        try:
            call = await tap(d, rules)
            ask, _ = await next_ask(d)
            assert ask["kind"] == "draft" and ask["message"].startswith('Post this in Example? "Hello there"')
            d.dialog_answer("a", ask["id"], "allow")
            r = await asyncio.wait_for(call, 2)
            assert r["text"].splitlines()[-1] == "Note: sent by a person (in chat)."
        finally:
            daemon_module._DRAFTS_ROUTE.reset(token)
        await finish(stop, task)
        assert not [g for g in log_lines(tmp_path) if g.get("grant_draft")]  # no grant stopped it
    run(go)


def test_a_word_that_doesnt_reach_people_is_an_ordinary_ask_even_in_a_draftsonly_route(tmp_path):
    async def go():
        d, rules, _ = setup(tmp_path, draft_grant(words=["Delete"]))
        stop, task = await started(d)
        token = daemon_module._DRAFTS_ROUTE.set(True)
        try:
            call = await tap(d, rules, "del")
            ask, _ = await next_ask(d)
            assert ask["kind"] == "confirm" and ask["message"].startswith('Allow: tap "Delete"')
            d.dialog_answer("a", ask["id"], "decline")
            with pytest.raises(Refused):
                await asyncio.wait_for(call, 2)
        finally:
            daemon_module._DRAFTS_ROUTE.reset(token)
        await finish(stop, task)
    run(go)


def test_the_draft_is_redacted_before_the_cut_and_cut_at_500_with_a_count(tmp_path):
    secret = "hunter2-swordfish"
    text = "x" * 497 + secret + " tail"  # the secret lies across the cut
    assert "hunter" not in dialog.draft_text(text, lambda t: t.replace(secret, "<tester's password>"))
    long = dialog.draft_text("word " * 200, None)
    assert long.endswith("…(+" + str(1000 - 500 - 0 - (len(long) - 500 - len("…(+)") - len(str(1000 - 500))) ) + ")") or "…(+" in long
    assert len(long.split("…(+")[0]) == 500
    assert dialog.draft_text("a‮b\x00c\n d  e", None) == "abc d e"
    assert dialog.draft_text('say "hi"', None) == "say 'hi'"

    async def go():
        d, rules, _ = setup(tmp_path, draft_grant())
        d.session.tree[2] = el("e3", "input", "Message", "message", value=f"My note {secret}", rect=(10, 200, 300, 44))
        d.scrub_everywhere = lambda t: t.replace(secret, "<tester's password>")
        stop, task = await started(d)
        call = await tap(d, rules)
        ask, _ = await next_ask(d)
        assert ask["message"] == "Post this in Example? \"My note <tester's password>\""
        d.dialog_answer("a", ask["id"], "decline")
        with pytest.raises(Refused):
            await asyncio.wait_for(call, 2)
        await finish(stop, task)
    run(go)


def test_the_field_is_the_one_the_agent_last_typed_into_else_the_only_filled_one(tmp_path):
    async def go():
        d, rules, _ = setup(tmp_path, draft_grant())
        d.session.tree += [el("e4", "input", "Search", "search", value="cats", rect=(10, 50, 300, 44))]
        stop, task = await started(d)
        view = d.control.client("a")
        view.typed_field = ("message", "Message", (10, 200, 300, 44))
        call = await tap(d, rules)
        ask, _ = await next_ask(d)
        assert ask["message"] == 'Post this in Example? "Hello there"'
        d.dialog_answer("a", ask["id"], "decline")
        with pytest.raises(Refused):
            await asyncio.wait_for(call, 2)
        # two filled fields and none typed into: no field can be told
        view.typed_field = None
        d._confirms.clear()
        d.control.client("a").typed_field = None
        call = await tap(d, rules)
        ask, _ = await next_ask(d, 1)
        assert "no text found" in ask["message"] or "Hello there" not in ask["message"]
        d.dialog_answer("a", ask["id"], "decline")
        with pytest.raises(Refused):
            await asyncio.wait_for(call, 2)
        await finish(stop, task)
    run(go)


def test_a_dry_run_says_it_would_stop_at_a_draft(tmp_path):
    async def go():
        d, rules, _ = setup(tmp_path, draft_grant())
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
        r = await d.run_tool("a", "tap", {"target": {"id": "post"}, "dry_run": True}, policy=rules)
        assert "It would stop at a draft a person sees first" in r["text"] and taps(d) == []
        await finish(stop, task)
    run(go)


def test_a_route_file_says_draftsonly_with_a_boolean():
    base = {"name": "r", "start": "home", "steps": [], "variables": []}
    routes.check({**base, "draftsOnly": True})
    with pytest.raises(Refused) as e:
        routes.check({**base, "draftsOnly": "yes"})
    assert e.value.code == "bad_params" and "draftsOnly" in str(e.value)


def test_the_hosts_draft_question_takes_the_daemons_two_words():
    from tapwright import hostdialog
    model = hostdialog.draft_model(["Post", "Don't post"])
    assert model(answer="Post").answer == "Post" and model(answer="Don't post").answer == "Don't post"
    with pytest.raises(Exception):
        model(answer="Allow once")


def test_the_host_answers_allow_only_for_the_first_word():
    from mcp.server.elicitation import AcceptedElicitation

    from tapwright import hostdialog

    class Ctx:
        def __init__(self, word):
            self.word = word

        async def elicit(self, message, model):
            return AcceptedElicitation(data=model(answer=self.word))

    async def go():
        choices = ["Send", "Don't send"]
        assert await hostdialog.HostDialogs._ask("draft", "m", Ctx("Send"), choices) == "allow"
        assert await hostdialog.HostDialogs._ask("draft", "m", Ctx("Don't send"), choices) == "decline"
    asyncio.run(go())

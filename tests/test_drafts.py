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

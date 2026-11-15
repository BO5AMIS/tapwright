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




























# ---- only a person creates one ----

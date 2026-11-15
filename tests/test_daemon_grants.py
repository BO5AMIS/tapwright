"""Grants in the daemon (docs/AGENT-TOOLS.md §19.3, §19.5, §12, §23): a covered touch goes through with its note, a
matching grant that can't cover it says why before the person's OK, a dry run counts nothing, the action log carries
grant, grant_use and grant_asked, and --revoke-all stops a running batch between moves while a pause revokes nothing."""
import asyncio
import datetime
import json

import pytest
from fakes import button
from test_daemon import finish, make, run, started

from tapwright import daemon as daemon_module
from tapwright import grants, policy
from tapwright.actionlog import ActionLog
from tapwright.auditlog import AuditLog
from tapwright.control import Refused

TZ = datetime.timezone(datetime.timedelta(hours=3))
APP = "com.example.app"  # the fake session's app


def clock_at(text="2026-11-14T10:00:00"):
    box = [datetime.datetime.fromisoformat(text).replace(tzinfo=TZ)]
    return box


def setup(tmp_path, *items, at="2026-11-14T10:00:00"):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"version": 2, "confirm": ["Post", "Delete"], "block": [], "grants": list(items)}),
                    encoding="utf-8")
    box = clock_at(at)
    audit = AuditLog(tmp_path / "audit", now=lambda: box[0].astimezone(datetime.timezone.utc))
    d = make(audit=audit, grant_clock=lambda: box[0], actions=ActionLog(tmp_path / "log"))
    d.session.tree = [button("e1", "Post", "post"), button("e2", "OK", "ok-button", rect=(10, 200, 100, 40))]
    return d, policy.load(path), path, box


def grant(**over):
    g = {"name": "wayfare_posting", "app": APP, "words": ["Post"], "until": "2026-11-21", "perDay": 2,
         "draftsOnly": False}
    g.update(over)
    return {k: v for k, v in g.items() if v is not None}


def log_lines(tmp_path):
    return [json.loads(line) for f in sorted((tmp_path / "log").glob("*.jsonl"))
            for line in f.read_text("utf-8").splitlines()]


def audit_lines(tmp_path):
    return [json.loads(line) for line in (tmp_path / "audit" / "2026-11.jsonl").read_text("utf-8").splitlines()]

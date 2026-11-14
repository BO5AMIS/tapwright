"""`tapwright grants` (AGENT-TOOLS §19.5, §19.7): what a person runs in a terminal to see the grants of a policy file,
revoke them all, or remove a stuck edit lock. They are commands, not MCP tools: the model can neither list the grants'
history, revoke, nor unlock through the tool."""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Callable

from . import auditlog, grants, pidlock

LAST_USES = 3


def _lines_of(audit_folder: Path | None) -> dict[tuple[str, str], list[tuple[str, str]]]:
    """The audit log's `grant` lines: (app, grant name) -> [(time, detail)], oldest first."""
    used: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for line in auditlog.read_lines(audit_folder):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("kind") == "grant" and isinstance(entry.get("name"), str):
            used.setdefault((str(entry.get("app") or ""), entry["name"]), []).append(
                (str(entry.get("t") or ""), str(entry.get("detail") or "")))
    return used


def _standing(grant: grants.Grant, today: datetime.date) -> str:
    if grant.revoked is not None:
        return f"revoked {grant.revoked}"
    return f"ended {grant.until.isoformat()}" if today > grant.until else "active"


def run_grants(policy: str | None, *, revoke_all: bool = False, audit_folder: Path | None = None,
               now: Callable[[], datetime.datetime] | None = None) -> tuple[str, int]:
    """`tapwright grants --policy FILE [--revoke-all]`: every grant, today's counts and the last uses (from the audit
    log). `--revoke-all` revokes every grant in the file first (a `revoke` line each) and lists them as revoked.
    (the text, the exit code)."""
    if not policy:
        return "grants needs the policy file: tapwright grants --policy FILE (the file you give `tapwright mcp --policy`)", 2
    path = Path(policy).expanduser().resolve()
    audit = auditlog.AuditLog(audit_folder)
    book = grants.Book(path, audit, now=now)
    lines: list[str] = []
    if revoke_all:
        done = book.revoke_all()
        if isinstance(done, str):
            return f"Couldn't revoke: {done}", 1
        revoked, already = done
        if revoked:
            lines += [f"Revoked {len(revoked)} {'grant' if len(revoked) == 1 else 'grants'}: {', '.join(revoked)}. "
                      "A running route stops at its next move.", ""]
        else:
            lines += ["Every grant was already revoked ({}).".format(len(already)) if already else "No grants to revoke.",
                      ""]
    loaded = book.load()
    if not loaded.ok:
        return f"Couldn't read the policy file {path}.", 1
    at = book.now()
    today = at.date()
    used = _lines_of(audit.folder)
    if not loaded.grants and not loaded.refused:
        lines.append("No grants.")
    for grant in loaded.grants:
        counts = book.counts(grant, at)
        lines.append(f"{grant.name}: {_standing(grant, today)}")
        lines.append("  " + "; ".join([
            f"app {grant.app}",
            "words " + ", ".join(json.dumps(w) for w in grant.words),
            *([f"route {grant.route}"] if grant.route else []),
            f"until {grant.until.isoformat()}",
            *([f"quiet {grant.quiet[0]} to {grant.quiet[1]}"] if grant.quiet else []),
            *(["drafts shown first"] if grant.drafts_only else [])]))
        if counts is None:
            lines.append("  today unknown (its uses folder can't be read)")
        else:
            lines.append(f"  today {counts[0]} of {grant.per_day}"
                         + (f", this hour {counts[1]} of {grant.per_hour}" if grant.per_hour is not None else ""))
        last = used.get((grant.app, grant.name), [])[-LAST_USES:][::-1]
        lines.append("  last uses: " + ("; ".join(f"{t} {d}".strip() for t, d in last) if last else "none"))
    for refusal in loaded.refused:
        lines.append(refusal.line().replace("Note: ", "Refused: ", 1))
    return "\n".join(lines), 0


def run_unlock(policy: str | None, *, audit_folder: Path | None = None,
               deps: pidlock.Deps | None = None) -> tuple[str, int]:
    """`tapwright grants unlock --policy FILE`: remove a stuck edit lock (empty, unreadable, or held by a process that
    is gone). Refused while a live holder is proven (1.14.2). Audited as an `approval`, since it is a person's own
    decision about the policy file and not a grant's use or a revocation."""
    if not policy:
        return "grants unlock needs the policy file: tapwright grants unlock --policy FILE", 2
    lock = Path(f"{Path(policy).expanduser().resolve()}.edit.lock")
    result = pidlock.unlock(lock, deps)
    if result.status == "none":
        return "The policy file has no lock; nothing to remove.", 0
    if result.status == "live":
        return (f"The policy file's lock is held by a live process (pid {result.pid}); nothing was removed. Wait for "
                "it to finish, or stop that process first."), 1
    done = [f"Removed {p} ({why})." for p, why in result.removed]
    if result.removed:
        auditlog.AuditLog(audit_folder).write(
            "approval", name="policy edit lock", detail=f"unlocked ({'; '.join(why for _, why in result.removed)})")
    if result.status == "failed":
        return "\n".join([*done, f"Couldn't remove {result.failed or 'the lock'}: it changed or can't be deleted. "
                                 "Look at it and try again."]), 1
    return "\n".join(done), 0


def stuck_lock(policy: str | None, deps: pidlock.Deps | None = None) -> list[tuple[str, str]]:
    """For `doctor`: the stuck lock files of a policy file, (path, why), each stuck only on proof (never a live one)."""
    if not policy:
        return []
    base = f"{Path(policy).expanduser().resolve()}.edit.lock"
    found = []
    for path in (base, f"{base}.takeover"):
        kind, pid = pidlock.state(path, deps)
        if kind in ("dead", "empty", "unreadable"):
            found.append((path, pidlock.stuck_words(kind, pid)))
    return found

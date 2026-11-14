"""A scheduled run (AGENT-TOOLS §22.2, §22.3): `tapwright run-schedule NAME`, no model involved.

The run drives the schedule's saved route with its `with` values through the daemon, as a client that never asks anyone:
the grants of the schedule's policy file are in force (and their counts are the agents' own, §19.4), the vault is in force
(the `accounts` file), and a touch that needs a person's OK and has no grant stops the run. It stops at the first failed
`expect` or `off_route`, and writes a report, `~/.tapwright/runs/<name>-<UTC time>.json` and `.md`. Every run and every
skip is a `schedule` line in the audit log (§23).

Before the route starts, the run checks what could stop it, in this order (§22.3): another run of this schedule going
(the run lock, pidlock.py: `skipped: the previous run is still going`), the phone locked (`skipped: the phone is locked`,
unless a passcode unlock is given), the phone in use by a person or an agent (it waits up to 10 minutes, then `skipped:
the phone was in use`; a run is never queued behind another).
"""
from __future__ import annotations

import base64
import dataclasses
import datetime
import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

from . import auditlog, mapfile, pidlock, schedules, store
from .errors import DRAFT_STOPPED

WAIT_SECONDS = 600.0  # how long a run waits for a busy phone (§22.3)
POLL_SECONDS = 5.0
TIMEOUT = 900.0  # a drive of a route may take this long

PREVIOUS = "skipped: the previous run is still going"
IN_USE = "skipped: the phone was in use"
LOCKED = "skipped: the phone is locked"
DRAFT = "stopped: a draft is waiting for a person."
NEEDS_PERSON = "stopped: a touch needs a person's OK, and no grant covers it."


@dataclasses.dataclass
class Outcome:
    """`status`: done, failed, stopped, skipped, or idle (a file trigger found nothing new: nothing is written)."""
    status: str
    result: str  # the words: `done`, `failed at step 3 (expect)`, `skipped: the phone was in use`, ...
    text: str = ""  # what the run printed
    code: int = 0  # the exit code: 0 done or idle, 1 failed or stopped, 2 couldn't start, 3 skipped
    report: str | None = None  # the report's .json path


@dataclasses.dataclass
class Deps:
    """What a run needs from outside; tests replace any of it."""
    client: ModuleType | None = None  # tapwright.client: call, set_scope, set_policy, set_accounts, set_maps, status
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    now: Callable[[], datetime.datetime] = lambda: datetime.datetime.now(datetime.timezone.utc)
    where: Path | None = None  # the schedules folder
    runs: Path | None = None
    audit_folder: Path | None = None
    lock: pidlock.Deps | None = None
    unlock_phone: Callable[[schedules.Schedule], bool] | None = None  # the passcode unlock (Tapwright 1.7, a later part)
    wait: float = WAIT_SECONDS
    poll: float = POLL_SECONDS

    def api(self) -> ModuleType:
        if self.client is None:
            from . import client
            self.client = client
        return self.client


def stamp(at: datetime.datetime) -> str:
    return at.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def iso(at: datetime.datetime) -> str:
    return at.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_route(s: schedules.Schedule) -> str:
    """The bundle id of the app that has the saved route (the run may use that app only). ScheduleError otherwise."""
    root = mapfile.root(s.maps)
    found = sorted(p.parent.parent.name for p in root.glob(f"*/routes/{s.route}.json")) if root.is_dir() else []
    if not found:
        raise schedules.ScheduleError(f'no saved route called "{s.route}" in {root}')
    if len(found) > 1:
        raise schedules.ScheduleError(f'the route "{s.route}" is saved for several apps ({", ".join(found)}); a schedule '
                                      "names one route")
    return found[0]


def classify(code: str, message: str) -> tuple[str, str]:
    """(status, result) of a refusal that ended the drive."""
    first = message.strip().splitlines()[0][:200] if message.strip() else ""
    if code == DRAFT_STOPPED:  # the daemon's own code for a stop at a draft (21.3), never the words of the message
        return "stopped", DRAFT
    if code == "not_allowed" and "needs_confirmation" in message or code == "needs_confirmation":
        return "stopped", NEEDS_PERSON
    if code in ("expect_failed", "off_route"):
        step = re.search(r"\bstep (\d+)", message)
        return "failed", f"failed at step {step.group(1)} ({code.removesuffix('_failed')})" if step else \
            f"failed ({code.removesuffix('_failed')})"
    return "failed", f"failed: {code}: {first}"


def run(name: str, *, if_triggered: bool = False, deps: Deps | None = None) -> Outcome:
    """One run of schedule `name`, now. With `if_triggered` (the poll of a file trigger) it runs only when a file appeared
    in the trigger's folder since the last check, and does nothing, silently, otherwise."""
    deps = deps or Deps()
    audit = auditlog.AuditLog(deps.audit_folder, now=deps.now)
    try:
        s = schedules.load(name, deps.where)
    except schedules.ScheduleError as e:
        audit.write("schedule", name=name, detail=f"failed: {e}"[:300])
        return finish(name, deps, "failed", f"failed: {e}", None, deps.now(), code=2)
    if if_triggered:
        if not s.trigger or "file" not in s.trigger:
            return Outcome("failed", "failed: --if-triggered is for a schedule with a file trigger", code=2,
                           text=f'The schedule "{name}" has no file trigger.')
        if not schedules.new_files(s, deps.where):
            return Outcome("idle", "nothing new")
    began = deps.now()
    token = pidlock.take(schedules.lock_of(name, deps.where), deps.lock)
    if token is None:
        return end(s, deps, audit, "skipped", PREVIOUS, began, 3)
    try:
        return attempt(s, deps, audit, began)
    finally:
        pidlock.release(schedules.lock_of(name, deps.where), token)


def end(s: schedules.Schedule, deps: Deps, audit: auditlog.AuditLog, status: str, result: str, began: datetime.datetime,
        code: int, text: str = "", picture: bytes | None = None, bundle: str | None = None, used: bool = True) -> Outcome:
    audit.write("schedule", name=s.name, app=bundle, detail=result)
    return finish(s.name, deps, status, result, s, began, text=text, code=code, picture=picture, used=used,
                  scrub=scrubber(s))


def scrubber(s: schedules.Schedule) -> Callable[[str], str]:
    """Takes the vault's values out of a text (§20.3); the daemon's answers are redacted already, this is the second line."""
    if s.accounts:
        try:
            from . import accounts
            loaded = accounts.load(s.accounts)
            if loaded.problem is None and loaded.redacts:
                return loaded.scrub
        except Exception:
            pass
    return lambda text: text


def used_since(deps: Deps, began: datetime.datetime) -> tuple[dict[str, int], list[str]]:
    """(grants used with their counts, vault names used) by the daemon's audit lines written since the run began. Names
    only: an audit line never holds a value."""
    grants: dict[str, int] = {}
    vault: list[str] = []
    for line in auditlog.read_lines(deps.audit_folder, since=began):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        name = entry.get("name")
        if not isinstance(name, str):
            continue
        if entry.get("kind") == "grant":
            grants[name] = grants.get(name, 0) + 1
        elif entry.get("kind") == "vault" and name not in vault:
            vault.append(name)
    return grants, vault


def finish(name: str, deps: Deps, status: str, result: str, s: schedules.Schedule | None, began: datetime.datetime, *,
           text: str = "", code: int = 1, picture: bytes | None = None, used: bool = True,
           scrub: Callable[[str], str] | None = None) -> Outcome:
    """Write the report (`.json` and `.md`) and return what to print."""
    scrub = scrub or (lambda t: t)
    ended = deps.now()
    grants, vault = used_since(deps, began) if used else ({}, [])
    base = (deps.runs or store.HOME / "runs")
    stem = f"{name}-{stamp(began)}"
    grants_line = "Grants used: " + (", ".join(f"{n} ({c})" for n, c in grants.items()) or "none")
    vault_line = "Vault names used: " + (", ".join(vault) or "none")
    body = scrub(text).strip()
    data = {"version": 1, "tool": "tapwright", "schedule": name, "route": s.route if s else None, "status": status,
            "result": scrub(result), "started": iso(began), "finished": iso(ended), "report": body,
            "grants_used": grants, "vault_names_used": vault, "picture": f"{stem}.png" if picture else None}
    lines = [f"# Scheduled run: {name}", "", f"Result: {scrub(result)}", f"Route: {s.route if s else '-'}",
             f"Started: {iso(began)}   Finished: {iso(ended)}", "", *([body, ""] if body else []), grants_line, vault_line,
             *([f"Picture: {stem}.png"] if picture else [])]
    path = base / f"{stem}.json"
    try:
        base.mkdir(parents=True, exist_ok=True)
        if picture:
            (base / f"{stem}.png").write_bytes(picture)
        (base / f"{stem}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        where = str(path)
    except OSError:
        where = None
    shown = [f"{name}: {scrub(result)}", *([body] if body else []), grants_line, vault_line] + ([f"Report: {where}"] if where else [])
    return Outcome(status, result, "\n".join(shown), code, where)


def attempt(s: schedules.Schedule, deps: Deps, audit: auditlog.AuditLog, began: datetime.datetime) -> Outcome:
    api = deps.api()
    try:
        values = schedules.read_values(s)
        bundle = find_route(s)
    except schedules.ScheduleError as e:
        return end(s, deps, audit, "failed", f"failed: {e}", began, 2, used=False)
    # the phone: locked, or in use (§22.3)
    waited = deps.monotonic()
    unlocked = False
    while True:
        try:
            status = api.status()
        except Exception as e:
            return end(s, deps, audit, "failed", f"failed: the phone's server didn't start ({str(e)[:150]})", began, 2,
                       bundle=bundle, used=False)
        if status is None:
            return end(s, deps, audit, "failed", "failed: the phone's server didn't answer", began, 2, bundle=bundle,
                       used=False)
        state = status.get("state")
        if state == "locked":
            if deps.unlock_phone is not None and not unlocked:
                unlocked = True  # one try
                if deps.unlock_phone(s):
                    continue
            return end(s, deps, audit, "skipped", LOCKED, began, 3, bundle=bundle, used=False)
        if state != "live":
            detail = status.get("detail") or state
            return end(s, deps, audit, "failed", f"failed: the phone isn't connected ({str(detail)[:120]})", began, 2,
                       bundle=bundle, used=False)
        if not (status.get("driver") in ("person", "agent") or status.get("paused")):
            break
        if deps.monotonic() - waited >= deps.wait:
            return end(s, deps, audit, "skipped", IN_USE, began, 3, bundle=bundle, used=False)
        deps.sleep(deps.poll)
    # the route, as a client that asks nobody
    args: dict = {"route": s.route, "screenshot": True}
    given = dict(values)
    if (account := given.pop("account", None)) is not None:
        args["account"] = account
    if given:
        args["with"] = given
    from . import policy
    try:
        api.set_scope([bundle], False)
        api.set_policy(policy.load(s.policy) if s.policy else None)
        api.set_accounts(s.accounts)
        api.set_maps(s.maps)
    except ValueError as e:
        return end(s, deps, audit, "failed", f"failed: {e}", began, 2, bundle=bundle, used=False)
    audit.write("schedule", name=s.name, app=bundle, detail="started")
    try:
        answer = api.call("drive", args, f"schedule-{s.name}", timeout=TIMEOUT)
    except api.DaemonError as e:
        state, result = classify(e.code, str(e))
        return end(s, deps, audit, state, result, began, 1, text=str(e), bundle=bundle)
    except Exception as e:  # the server went away mid-run
        return end(s, deps, audit, "failed", f"failed: {type(e).__name__}: {str(e)[:150]}", began, 1, bundle=bundle)
    image = answer.get("image")
    picture = None
    if isinstance(image, str) and image:
        try:
            picture = base64.b64decode(image, validate=False)
        except ValueError:
            picture = None
    elif isinstance(image, (bytes, bytearray)):
        picture = bytes(image)
    return end(s, deps, audit, "done", "done", began, 0, text=str(answer.get("text", "")), picture=picture, bundle=bundle)

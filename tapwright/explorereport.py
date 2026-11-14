"""`explore`'s report and answer (AGENT-TOOLS §25.2, §25.10, §25.11): one folder per run,
`~/.tapwright/explore/<app id>/<UTC time as YYYYMMDDTHHMMSSZ>/`, with `report.json`, `report.md`, a picture per finding in
`findings/` (private unless a test app, §27.2) and a script per finding in `repro/` (§27.1); the newest 20 folders of an app
are kept. Every string that is written, and every string in the answer, goes through `scrub` (§20.3) whole, before any cut.

Tapwright has no places known up front (§25.11), so its coverage holds `places` and `frontier_left` only, and it has no
`Coverage:` line and no `unreachable` finding. Pure functions over an `explore.Result` and the disk.
"""
from __future__ import annotations

import dataclasses
import datetime
import json
import os
import re
import shutil
from pathlib import Path
from typing import Callable

from . import explore, findings

FORMAT, VERSION = "agent-tools-explore-report", 1
KEEP = 20  # the newest folders of an app that are kept (§25.10)
FOLDER = re.compile(r"^\d{8}T\d{6}Z$")
STOP_WORDS = {
    "budget_minutes": "the budget (minutes) is used up.",
    "budget_steps": "the budget (steps) is used up.",
    "frontier_empty": "the frontier is empty.",
    "crash": "the app crashed.",
    "toggle_not_restored": "a toggle could not be set back",
    "cant_get_back": "it can't get back to a known place.",
    "app_closed": "the app is closed.",
}
ERRORS = ("paused", "busy", "locked")  # a stop that is an error (§25.9)
ERROR_TEXT = {
    "paused": "paused by the user in the live view; ask them to resume.",
    "busy": "a person is using the phone; try again in a moment.",
    "locked": "the phone is locked; ask the user to unlock it.",
}


def stamp(when: datetime.datetime) -> str:
    return when.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def iso(when: datetime.datetime) -> str:
    return when.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def minutes(seconds: float) -> float:
    return round(seconds / 60, 1)


def stop_line(result: explore.Result) -> str:
    """`the frontier is empty.`; a toggle that could not be set back names it: `(id=notify-switch).`"""
    text = STOP_WORDS.get(result.stopped, result.stopped)
    if result.stopped == "toggle_not_restored":
        return f"{text} ({result.detail})." if result.detail else f"{text}."
    return text


def finding_counts(found: tuple[findings.Finding, ...]) -> str:
    """`1 crash, 2 js_error, 1 a11y` in the order the kinds are listed in §25.10 (a repeat counts once: it is one finding)."""
    counts = {k: sum(1 for f in found if f.kind == k) for k in findings.KINDS}
    return ", ".join(f"{n} {k}" for k, n in counts.items() if n)


def coverage(result: explore.Result) -> dict:
    """§25.11: a tool with no places known up front has `places` and `frontier_left` only."""
    return {"places": len(result.places), "frontier_left": result.frontier_left}


def with_repro(finding: findings.Finding) -> tuple[findings.Finding, dict | None]:
    """The finding with its script's name, and the script (None when no script can reach it: no start place or no step)."""
    if not finding.path_from or not finding.path:
        return finding, None
    try:
        script = findings.repro_script(finding)
    except Exception:  # noqa: BLE001 - a script that can't be made is no reason to lose the report
        return finding, None
    return dataclasses.replace(finding, repro=f"repro/{finding.id}.json"), script


def document(result: explore.Result, who: explore.Identity, budget: explore.Budget, started: datetime.datetime,
             ended: datetime.datetime, scrub: Callable[[str], str]) -> tuple[dict, list[tuple[findings.Finding, dict | None]]]:
    """`report.json` (§25.10) and the findings with their scripts. Every string is redacted whole."""
    made = [with_repro(f) for f in result.findings]
    body = {
        "format": FORMAT, "version": VERSION, "app": who.app, "build": who.build, "tool": who.tool,
        "tool_version": who.tool_version, "started": iso(started), "ended": iso(ended), "stopped": result.stopped,
        "start_place": result.start_place,
        "budget": {"minutes": budget.minutes, "steps": budget.steps, "depth": budget.depth},
        "used": {"minutes": minutes(result.seconds), "steps": result.steps},
        "coverage": coverage(result),
        "findings": [findings.to_dict(f) for f, _ in made],
        "skipped": [{"place": s.place, "sig": s.sig, "why": s.why, **({"word": s.word} if s.word else {})}
                    for s in result.skipped],
        "notes": list(result.notes),
        "resets": [dict(r) for r in result.resets],
    }
    if result.stopped == "toggle_not_restored" and result.detail:
        body["stopped_detail"] = result.detail
    return findings.redact(body, scrub), made  # type: ignore[return-value]


def markdown(body: dict, found: tuple[findings.Finding, ...], scrub: Callable[[str], str]) -> str:
    """`report.md`: the same content for a person, the findings first. A private picture is named, not shown (§27.2)."""
    lines = [f"# Explore report: {body['app']}", ""]
    used = body["used"]
    lines.append(f"Explored {body['coverage']['places']} places in {used['minutes']} min ({used['steps']} steps), "
                 f"{len(found)} finding{'s' if len(found) != 1 else ''}. Stopped: {body['stopped']}.")
    lines += ["", f"Build {body['build']}, {body['tool']} {body['tool_version']}, {body['started']} to {body['ended']}.", ""]
    lines.append("## Findings")
    if not found:
        lines.append("None.")
    for f, shown in zip(found, body["findings"]):
        lines += ["", f"### {f.id} {f.kind}" + (f" (x{f.count})" if f.count > 1 else ""),
                  f"- Place: {f.place or 'unknown'}", f"- Expected: {f.expected}"]
        got = f.got.split("\n")
        lines.append(f"- Got: {got[0]}")
        lines += [f"  {extra}" for extra in got[1:]]
        if f.path:
            lines.append(f"- From {f.path_from}: " + "; ".join(_step(s) for s in f.path))
        if f.picture is not None:
            lines.append(f"- Picture: {f.picture.file} (private: shown only when a person asks)" if f.picture.private
                         else f"- Picture: ![{f.id}]({f.picture.file})")
        if shown.get("repro"):
            lines.append(f"- Script: {shown['repro']}")
    lines += ["", "## Skipped (not findings)"]
    lines += [f"- {s['place']} {s['sig']}: {s['why']}" + (f" ({s['word']})" if s.get("word") else "") for s in body["skipped"]] \
        or ["None."]
    lines += ["", "## Notes"] + ([f"- {n}" for n in body["notes"]] or ["None."])
    lines += ["", "## Resets"] + ([f"- reset: {r['how']} only, data {'kept' if r['data_kept'] else 'cleared'} (step {r['at_step']})"
                                   for r in body["resets"]] or ["None."])
    lines += ["", f"Places: {body['coverage']['places']}; untried actions left: {body['coverage']['frontier_left']}.", ""]
    return scrub("\n".join(lines))


def _step(step: dict) -> str:
    if step.get("row"):
        return f"row {step.get('index')}"
    target = step.get("target") or {}
    named = next(((k, target[k]) for k in ("id", "label", "text") if k in target), None)
    return f"{step.get('tool')} {named[0]}={named[1]}" if named else str(step.get("tool"))


def prune(app_folder: Path, keep: int = KEEP) -> None:
    """The newest `keep` run folders of an app stay (§25.10); the others are removed. Only folders named as ours."""
    try:
        runs = sorted(p for p in app_folder.iterdir() if p.is_dir() and FOLDER.match(p.name))
    except OSError:
        return
    for old in runs[:max(0, len(runs) - keep)]:
        shutil.rmtree(old, ignore_errors=True)


def folder_for(root: Path, app: str, when: datetime.datetime) -> Path:
    """The run's folder: unique even for two runs in one second."""
    base = root / app
    path = base / stamp(when)
    n = 1
    while path.exists():
        n += 1
        path = base / f"{stamp(when)}-{n}"  # (not one of ours for prune: it is never removed by it, and keeps the older run)
    return path


def _write(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    if isinstance(data, bytes):
        temporary.write_bytes(data)
    else:
        temporary.write_text(data, encoding="utf-8")
    os.replace(temporary, path)


def write(root: Path, result: explore.Result, who: explore.Identity, budget: explore.Budget, started: datetime.datetime,
          ended: datetime.datetime, scrub: Callable[[str], str]) -> Path:
    """Write the run's folder and return its path (the answer's `Report:` is `<folder>/report.md`)."""
    folder = folder_for(root, who.app, started)
    prune(folder.parent, KEEP - 1)  # (room for this run: the newest 20 stay)
    body, made = document(result, who, budget, started, ended, scrub)
    kept = tuple(f for f, _ in made)
    folder.mkdir(parents=True, exist_ok=True)
    for f, script in made:
        if f.picture is not None and f.id in result.pictures:
            _write(folder / f.picture.file, result.pictures[f.id])
        if script is not None and f.repro is not None:
            _write(folder / f.repro, json.dumps(findings.redact(script, scrub), indent=2, ensure_ascii=False) + "\n")
    _write(folder / "report.json", json.dumps(body, indent=2, ensure_ascii=False) + "\n")
    _write(folder / "report.md", markdown(body, kept, scrub))
    return folder


def answer(result: explore.Result, folder: Path | None, scrub: Callable[[str], str]) -> str:
    """The summary (§25.2): not the screen. Without a Coverage line (no places known up front)."""
    n = len(result.findings)
    lines = [f"Explored {len(result.places)} places in {minutes(result.seconds)} min ({result.steps} steps), "
             f"{n} finding{'s' if n != 1 else ''}. Stopped: {stop_line(result)}"]
    if n:
        lines.append(f"Findings: {finding_counts(result.findings)}.")
    if folder is not None:
        lines.append(f"Report: {folder / 'report.md'}")
    return scrub("\n".join(lines))


def done_line(result: explore.Result, folder: Path | None) -> str:
    """`Done: 41 steps, 9 places. Left at: settings. Report: <path>` (§25.9)."""
    left = f" Left at: {result.left_at}." if result.left_at else ""
    where = f" Report: {folder / 'report.md'}" if folder is not None else ""
    return f"Done: {result.steps} steps, {len(result.places)} places.{left}{where}"


def error_text(result: explore.Result, folder: Path | None, scrub: Callable[[str], str]) -> str:
    """A stop by `paused`, `busy` or `locked` (§25.9): the error's message ends `Done: 41 steps, 9 places. Left at: settings.
    Report: <path>`."""
    return scrub(f"{ERROR_TEXT[result.stopped]} {done_line(result, folder)}")

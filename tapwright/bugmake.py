"""Making a bug (AGENT-TOOLS 1.16.2 §29.3) from its three sources: a finding of an explore report, the last failed check,
and what the session's own touches were (`from: "now"`). The daemon's tool (bugtool.py) and the command line
(`tapwright bug`) both use these; everything they need comes in as arguments (`Context`), so no phone is needed here.

§29.6 at every sink: the finding is read again through the single-pass redactor when the bug is made (a value added to the
vault since counts), paths are cut to file names, and only then is anything cut. A redactor that fails hides the text
(findings.safe_scrub). Run folders and their files are a trust boundary: a folder is read only when it lies, resolved,
inside the explore home (realpath containment), and each file it names only inside it (Tapline's lesson).
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
from pathlib import Path
from typing import Callable

from . import bug, bugstore, explorefloor, findings, policy as policylib, routes

RUN_NAME = re.compile(r"^\d{8}T\d{6}Z(?:-\d+)?$")
REPRO_FILE = re.compile(r"^repro/[\w.-]+\.json$")
PICTURE_FILE = re.compile(r"^findings/[\w.-]+\.jpg$")
NOW_TOUCHES = ("tap", "long_press", "toggle", "type_text", "scroll", "swipe", "back", "open_url")
NOW_STEPS = 10
NOTE_MAX = 300
ELEMENT_TOOLS = ("tap", "long_press", "toggle", "type_text")


class BugError(Exception):
    """A bug that can't be made: `code` is the tool's error code (`not_found`, `bad_params`, `unsupported`, `not_allowed`)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclasses.dataclass
class Context:
    """What making a bug needs from its caller. `scrub` is the bug report's single-pass redactor (vault values, codes,
    typed values: accounts.Store.bug_scrub); `gate` the grep gate over the files to write; `map_data(app)` the app's map
    (its places' keys and its roads), or None; `rules` the policy whose words mark a step `(needs a person)`."""
    store: bugstore.BugStore
    scrub: Callable[[str], str]
    gate: Callable[[dict[str, str]], str | None]
    map_data: Callable[[str], dict | None]
    rules: policylib.Policy = policylib.NONE
    # (review R185b H1) what the tool's own labels (versions, build, device) are checked with: the vault's values and codes,
    # not the typed values. None: the vault redactor of the store `scrub` belongs to (`Store.bug_scrub`), else `scrub`
    labels: Callable[[str], str] | None = None

    def safe(self) -> Callable[[str], str]:
        return findings.safe_scrub(self.scrub)

    def label_check(self) -> Callable[[str], str]:
        if self.labels is not None:
            return findings.safe_scrub(self.labels)
        owner = getattr(self.scrub, "__self__", None)
        vault = getattr(owner, "scrub", None) if getattr(self.scrub, "__name__", "") == "bug_scrub" else None
        return findings.safe_scrub(vault if callable(vault) else self.scrub)


# ---- the finding, made safe ----

_PRIVATE_ONLY = re.compile(r"^(?:\S+\s+){0,3}<private>\s*$")  # "04:49:22.373 info <private>": a line iOS hid whole


def clean(raw: dict, scrub: Callable[[str], str], named: bool = True, labels: Callable[[str], str] | None = None) -> dict:
    """A finding (§27 JSON, or `observed`) checked and redacted whole, then its paths cut to file names (its `got`,
    `expected`, logs and crash: review R185 M2), then cut; a `system` that isn't one of §29.8's kinds is `other`.
    `FindingError` for what isn't a finding.

    `named`: is the app one of the daemon's test apps (`serve --apps`)? A bug of any other app (review R185 H2) keeps no
    screen text and no log lines beyond its own normalized `got`: no logs, no component names, `got` and `expected`
    normalized (§29.5), a step only when it is aimed by an id (one aimed by words cuts the path after it), and its picture
    is private."""
    made = findings.parse(raw, scrub, bug.BUG_KINDS, labels)
    out = findings.to_dict(made)
    out["got"] = "\n".join(bug.strip_paths(line) for line in out["got"].split("\n"))
    out["expected"] = bug.strip_paths(out["expected"])
    out["logs"] = [bug.strip_paths(line)[:findings.LOG_CUT] for line in out["logs"]
                   if not _PRIVATE_ONLY.search(line)][-findings.MAX_LOGS:]  # (iOS's hidden lines tell a reader nothing)
    if "crash" in out:
        out["crash"] = {"reason": bug.strip_paths(out["crash"]["reason"]),
                        "frames": [bug.strip_paths(f) for f in out["crash"]["frames"]][:findings.MAX_FRAMES]}
    system = raw.get("system")
    if system is not None:
        out["system"] = system if system in bug.SYSTEM_SHEETS else "other"
    if out.get("system") is not None and isinstance(out.get("picture"), dict):
        out["picture"] = {**out["picture"], "private": True}  # (§29.8) a picture with a system sheet in front: always private
    if not named:
        out = _bare(out)
    return out


_QUOTED = re.compile("([\"'\u2018\u201c])[^\"'\u2019\u201d]*([\"'\u2019\u201d])")
_BARE_ARGS = ("tool", "direction", "to", "x0", "y0", "x1", "y1", "url", "app", "seconds", "timeout", "action", "button",
              "orientation", "mode", "gone", "force", "optional", "name", "under")


def _bare(out: dict) -> dict:
    """A finding of an app that isn't one of the daemon's test apps (review R185 H2): its own normalized `got` and no other
    text of the screen or the log."""
    out = dict(out)
    out["got"] = "\n".join(bug.normalize_got(line) for line in out["got"].split("\n") if line.strip()) or "#"
    out["expected"] = bug.normalize_got(_QUOTED.sub(lambda m: f"{m.group(1)}\u2026{m.group(2)}", out["expected"])) or "#"
    out["logs"] = []
    out.pop("component_stack", None)
    if "crash" in out:
        out["crash"] = {"reason": bug.normalize_got(out["crash"]["reason"]) or "#", "frames": out["crash"]["frames"]}
    if isinstance(out.get("picture"), dict):
        out["picture"] = {**out["picture"], "private": True}
    path = [bare_step(s) for s in out.get("path") or []]
    last_bad = max((i for i, s in enumerate(path) if s is None), default=-1)
    if last_bad >= 0:
        out["path"] = path[last_bad + 1:]
        out["path_from"] = None
        out["steps_cut"] = True
    else:
        out["path"] = path
    return out


def bare_step(step: dict) -> dict | None:
    """A step as a bug of an app that isn't a test app keeps it: aimed by an id only, no words of the screen; None when it
    can't be written down so (aimed by a label or a text, or holding any other words)."""
    if step.get("row") is True:
        return step
    out: dict = {}
    for key, value in step.items():
        if key == "text" and step.get("tool") == "type_text" and value == findings.TYPED:
            out[key] = value
        elif key in ("target", "expect", "if"):
            if not isinstance(value, dict):
                return None
            kept = {k: v for k, v in value.items() if k in ("id", "index", "gone", "timeout", "row", "x", "y")}
            if any(k not in kept for k in value) and not (key == "target" and isinstance(value.get("id"), str)):
                return None
            out[key] = kept
        elif key in _BARE_ARGS or not isinstance(value, (str, dict, list)):
            out[key] = value
        else:
            return None
    return out


def place_key_of(ctx: Context, app: str, place: str | None, known: dict | None = None) -> dict | None:
    """The place's key (§18.1) as the signature uses it: the one the report kept for it (a place renamed since keeps its
    key), else the map's for that name, else None. A Tapwright key is a signature key: only `signature` is kept."""
    key = (known or {}).get(place) if place else None
    if not isinstance(key, dict) and place:
        data = ctx.map_data(app) or {}
        key = ((data.get("places") or {}).get(place) or {}).get("key")
    if isinstance(key, dict) and isinstance(key.get("signature"), str):
        return {"signature": key["signature"]}
    if isinstance(key, dict) and isinstance(key.get("route"), list):
        return {k: key[k] for k in ("route", "tab", "layer") if key.get(k) is not None}
    return None


def _same_move(step: dict, move: dict) -> bool:
    if move.get("tool") != step.get("tool"):
        return False
    a, b = step.get("target") or {}, move.get("target") or {}
    return any(isinstance(a.get(k), str) and a.get(k) == b.get(k) for k in ("id", "label", "text"))


def road_names(ctx: Context, app: str, path: list[dict]) -> list[str | None]:
    """For each step, the name the map's road with the same move gives its element (its label, else its text), redacted;
    None where there is none (§29.1)."""
    roads = ((ctx.map_data(app) or {}).get("roads") or []) if path else []
    safe = ctx.safe()
    out: list[str | None] = []
    for step in path:
        name = None
        if isinstance(step.get("target"), dict) and not step.get("row"):
            for road in roads:
                names = road.get("names") or {}
                if isinstance(road.get("move"), dict) and _same_move(step, road["move"]):
                    name = next((names[k] for k in ("label", "text") if isinstance(names.get(k), str) and names[k]), None)
                    if name:
                        break
        out.append(safe(name) if name else None)
    return out


def needs_person(ctx: Context, app: str, path: list[dict]) -> list[int]:
    """The steps whose target carries a word the policy or the explorer's floor holds (§11.1, §25.5): `(needs a person)`."""
    floor = explorefloor.Floor(ctx.rules, app)
    return [i for i, step in enumerate(path) if person_word(floor, step) is not None]


def person_word(floor: explorefloor.Floor, step: dict) -> str | None:
    """The policy or floor word a step's target holds (its label, its text, its id as words), or None."""
    target = step.get("target") if isinstance(step.get("target"), dict) else {}
    texts = [target.get("label"), target.get("text"),
             explorefloor.id_words(target["id"]) if isinstance(target.get("id"), str) else None]
    for text in texts:
        if isinstance(text, str) and text and (hit := floor.named(text)) is not None:
            return hit.word
    return None


def script_for(finding: dict, script: dict | None, scrub: Callable[[str], str]) -> dict:
    """`repro.json` (§29.2): the finding's own script, redacted, else one made from its path; never a value (`${typed}`)."""
    if not isinstance(script, dict):
        steps = [s if s.get("row") else _escaped(s) for s in finding.get("path") or []]
        typed = any(s.get("tool") == "type_text" for s in finding.get("path") or [])
        script = routes.route(finding["id"], finding["app"], finding.get("path_from") or "", None,
                              ["typed"] if typed else [], steps)
    script = json.loads(json.dumps(script))
    for step in script.get("steps") or []:
        if isinstance(step, dict) and step.get("tool") == "type_text":
            step["text"] = findings.TYPED
    return bugstore.redact_kept({}, {}, script, scrub)[2] or {}  # (its app text: review R185b H1)


def _escaped(step: dict) -> dict:
    return routes.escaped(step)  # type: ignore[return-value]


def _make(ctx: Context, finding: dict, *, place_key: dict | None, how: str, key: str, run: str | None,
          script: dict | None, picture: bytes | None, with_picture: bool, reproduced: dict, extras: dict | None = None,
          picture_source: str | None = None, named: bool = False) -> bugstore.Made:
    finding = dict(finding)
    cut = finding.pop("steps_cut", False)  # (a bug of an app that isn't a test app: a step aimed by words was left out)
    path = list(finding.get("path") or [])
    more = {"names": road_names(ctx, finding["app"], path) if named else [],
            "needs_person": needs_person(ctx, finding["app"], path), **(extras or {})}
    if cut:
        more["steps_cut"] = True
        more.pop("links_cut", None)  # (the indices were the path's before the cut)
    if not any(more["names"]):
        more.pop("names")
    if not named and isinstance(script, dict):
        script = None  # (the report's own script holds the words: the script is made from the bare path)
    return ctx.store.make(finding=finding, place_key=place_key,
                          source={"how": how, "key": key, "run": run, "finding": finding["id"] if how != "now" else None},
                          script=script_for(finding, script, ctx.safe()), picture=picture, with_picture=with_picture,
                          extras=more, reproduced=reproduced, gate=ctx.gate, picture_source=picture_source,
                          test_app=named, scrub=ctx.safe())


# ---- from an explore report (§29.3 `finding` and `run`) ----

def inside(root: Path, path: Path) -> bool:
    try:
        base, full = os.path.realpath(root), os.path.realpath(path)
    except (OSError, ValueError):
        return False
    return full == base or full.startswith(base.rstrip(os.sep) + os.sep)


def newest_run(explore_home: Path, app: str) -> Path | None:
    """The newest explore report folder of an app (§25.10), or None."""
    folder = explore_home / app
    try:
        runs = sorted(p.name for p in folder.iterdir() if RUN_NAME.match(p.name) and (p / "report.json").is_file())
    except OSError:
        return None
    return folder / runs[-1] if runs else None


@dataclasses.dataclass(frozen=True)
class RunFinding:
    raw: dict
    run: str  # the run folder's name
    script: dict | None
    picture: bytes | None
    picture_source: str | None  # where the picture's file is, relative to the explore home
    place_keys: dict


def read_run(explore_home: Path, run_dir: Path, finding_id: str) -> RunFinding:
    """One finding of an explore report folder, with its script and picture. The folder must lie inside the explore home,
    and every file it names inside it. A journey's report folder holds no `findings.json` in §27's shape in Tapwright:
    `bad_params`."""
    if not re.match(r"^f[0-9]{1,6}$", finding_id or ""):
        raise BugError("bad_params", f'finding is a finding\'s id like "f3", not {finding_id!r}')
    if not run_dir.is_dir():
        raise BugError("not_found", f"no report folder {run_dir.name}.")
    if not inside(explore_home, run_dir) or not RUN_NAME.match(run_dir.name):
        raise BugError("bad_params", "run is an explore report folder (under ~/.tapwright/explore/) or \"last\"; a journey's "
                                     "report holds no findings in §27's shape, so it can't be made into a bug.")
    report_path = run_dir / "report.json"
    try:
        if not inside(run_dir, report_path):
            raise OSError("outside")
        report = json.loads(report_path.read_text("utf-8"))
    except (OSError, ValueError, RecursionError):
        raise BugError("bad_params", f"{run_dir.name} holds no explore report that can be read.") from None
    found = report.get("findings") if isinstance(report, dict) else None
    if not isinstance(report, dict) or report.get("format") != "agent-tools-explore-report" or not isinstance(found, list):
        raise BugError("bad_params", f"{run_dir.name} is not an explore report.")
    raw = next((f for f in found if isinstance(f, dict) and f.get("id") == finding_id), None)
    if raw is None:
        ids = ", ".join(str(f.get("id")) for f in found if isinstance(f, dict)) or "none"
        raise BugError("not_found", f'no finding "{finding_id}" in {run_dir.name}; its findings are {ids}.')
    if raw.get("app") != report.get("app") or run_dir.parent.name != report.get("app"):
        raise BugError("bad_params", f"{run_dir.name}: the finding's app isn't the report's.")
    script = None
    repro = raw.get("repro")
    if isinstance(repro, str) and REPRO_FILE.match(repro) and inside(run_dir, run_dir / repro):
        try:
            loaded = json.loads((run_dir / repro).read_text("utf-8"))
            script = loaded if isinstance(loaded, dict) else None
        except (OSError, ValueError, RecursionError):
            script = None
    picture = source = None
    pic = raw.get("picture")
    if isinstance(pic, dict) and isinstance(pic.get("file"), str) and PICTURE_FILE.match(pic["file"]) \
            and inside(run_dir, run_dir / pic["file"]):
        try:
            picture = (run_dir / pic["file"]).read_bytes()
            source = "/".join((run_dir.parent.name, run_dir.name, pic["file"]))
        except OSError:
            picture = None
    keys = report.get("place_keys") if isinstance(report.get("place_keys"), dict) else {}
    return RunFinding(raw, run_dir.name, script, picture, source, keys)


def from_run(ctx: Context, got: RunFinding, *, with_picture: bool, reproduced: dict, named: bool = False) -> bugstore.Made:
    finding = clean(got.raw, ctx.safe(), named, ctx.label_check())
    key = place_key_of(ctx, finding["app"], finding.get("place"), got.place_keys)
    return _make(ctx, finding, place_key=key, how="explore", key=f"{got.run}/{finding['id']}", run=got.run,
                 script=got.script, picture=got.picture, with_picture=with_picture, reproduced=reproduced,
                 picture_source=got.picture_source, named=named)


def picture_again(explore_home: Path, source: str | None) -> bytes | None:
    """A kept bug's picture from its source file, while that file still exists inside the explore home (§29.2)."""
    if not isinstance(source, str) or ".." in source.split("/"):
        return None
    path = explore_home.joinpath(*source.split("/"))
    if not inside(explore_home, path) or not PICTURE_FILE.match("/".join(source.split("/")[-2:])):
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


# ---- from the session's touches (§29.3 `from: "now"`) ----

def check_note(value: object, what: str, required: bool) -> str | None:
    if value is None:
        if required:
            raise BugError("bad_params", f'{what} is required with from "now": one line, what is wrong.')
        return None
    if not isinstance(value, str) or not value.strip():
        raise BugError("bad_params", f"{what} is one line of text")
    if len(value) > NOTE_MAX or "\n" in value or "\r" in value:
        raise BugError("bad_params", f"{what} is one line of at most {NOTE_MAX} characters")
    return value.strip()


def step_of(entry: dict) -> dict | None:
    """One logged touch as a step, aimed as a road is (id, else label, else text, §18.2), or None when it can't be written
    down (a ref, a point). A typed step keeps no value."""
    tool = entry.get("tool")
    target = entry.get("target") if isinstance(entry.get("target"), dict) else None
    named = None
    if target is not None:
        for k in ("id", "label", "text"):
            if isinstance(target.get(k), str) and target[k]:
                named = {k: target[k]}
                if isinstance(target.get("index"), int) and not isinstance(target.get("index"), bool):
                    named["index"] = target["index"]
                break
        if named is None and target.get("row") is True:
            return {"row": True, "index": target.get("index") if isinstance(target.get("index"), int) else 1}
    if tool in ELEMENT_TOOLS:
        if named is None:
            return None
        return {"tool": tool, "target": named, **({"text": findings.TYPED} if tool == "type_text" else {})}
    if tool == "scroll":
        if target is not None and named is None:
            return None
        return {"tool": "scroll", **{k: entry[k] for k in ("direction", "to") if isinstance(entry.get(k), str)},
                **({"target": named} if named else {})}
    if tool == "swipe":
        keys = ("x0", "y0", "x1", "y1")
        if all(isinstance(entry.get(k), (int, float)) and not isinstance(entry.get(k), bool) for k in keys):
            return {"tool": "swipe", **{k: entry[k] for k in keys}}
        return None
    if tool == "back":
        return {"tool": "back"}
    if tool == "open_url":
        # (review R185 H1, §18.3) never the link as it was opened: only the pattern of a place's link it matched (the
        # daemon cut it down when it was kept); one that matched none can't be written down
        link = entry.get("link")
        return {"tool": "open_url", "url": link} if isinstance(link, str) and link else None
    return None


@dataclasses.dataclass(frozen=True)
class Observed:
    path: list[dict]
    path_from: str | None
    cut: bool  # earlier steps couldn't be written down
    links_cut: tuple[int, ...] = ()  # the steps (from 0) whose link had a query or a fragment, which isn't kept


def observed_path(trail: list[dict], app: str, place_now: str | None) -> Observed | None:
    """The last 10 touches of this app that succeeded and weren't dry runs, as steps; None with no touch at all. A step that
    can't be written down cuts the path after it. `path_from` is the place the first kept step ran on when it was known
    then (its own, else the place the touch before it left), else None."""
    every = [e for e in trail if e.get("tool") in NOW_TOUCHES and e.get("ok") and not e.get("dry_run")
             and e.get("app") in (None, app)]
    touches = every[-NOW_STEPS:]
    if not touches:
        return None
    # (W177) where each touch ran: its own place, else the place the touch before it left (its read after the move)
    offset = len(every) - len(touches)
    ran_on = [e.get("place") or (every[offset + n - 1].get("after") if offset + n > 0 else None)
              for n, e in enumerate(touches)]
    steps = [step_of(e) for e in touches]
    last_bad = max((i for i, s in enumerate(steps) if s is None), default=-1)
    kept: list[int] = []
    for i in range(last_bad + 1, len(touches)):
        # (W177) the same link opened twice in a row is one step for a person: the second open changes nothing. Not when
        # either open lost part of its link (a query, a fragment: PR #21): the two may have been different links, and the
        # page must say the saved link may not show it
        if kept and steps[i] == steps[kept[-1]] and (steps[i] or {}).get("tool") == "open_url" \
                and not touches[i].get("link_cut") and not touches[kept[-1]].get("link_cut"):
            continue
        kept.append(i)
    path = [steps[i] for i in kept]  # type: ignore[misc]
    # (PR #21) the start is where the FIRST kept step ran; a later step's place is never named as it (the first step may
    # have run on another screen): unknown, and the check then says so
    start = ran_on[kept[0]] if kept else None
    cut = tuple(n for n, i in enumerate(kept) if touches[i].get("link_cut"))
    return Observed(path, start, last_bad >= 0, cut)  # type: ignore[arg-type]


def from_now(ctx: Context, *, app: str, note: str, expected: str | None, identity: dict, when: str, observed: Observed,
             place: str | None, place_key: dict | None, logs: list[str], picture: bytes | None, system: str | None,
             with_picture: bool, reproduced: dict | None = None, named: bool = False) -> bugstore.Made:
    """The `observed` finding of what a person or an agent saw (§29.3), made into a bug. `identity`: app_version, build,
    tool_version, device {model, os}. `named`: the app is one of the daemon's test apps (see `clean`)."""
    raw: dict = {"id": "f1", "kind": "observed", "app": app, "app_version": identity["app_version"],
                 "build": identity["build"], "tool": "tapwright", "tool_version": identity["tool_version"], "when": when,
                 "device": identity["device"], "place": place, "path_from": observed.path_from, "path": observed.path,
                 "expected": expected or "(not given)", "got": note,
                 "picture": {"file": "findings/f1.jpg", "private": True} if picture is not None else None,
                 "logs": logs[-findings.MAX_LOGS:], "count": 1}
    if system is not None:
        raw["system"] = system
    finding = clean(raw, ctx.safe(), named, ctx.label_check())
    return _make(ctx, finding, place_key=place_key, how="now", key=f"now/{when}", run=None, script=None, picture=picture,
                 with_picture=with_picture, reproduced=reproduced or {"checked": False, "why": bug.NOT_ASKED},
                 extras={"steps_cut": observed.cut, "links_cut": list(observed.links_cut)}, named=named)


# ---- from the last failed check (§29.3 `from: "failure"`) ----

# a run step's arguments that are the batch's notes, never kept; `with` (a drive's values) holds values: such a step can't be
# written down
_NOTE_KEYS = ("place", "seen", "repaired", "secure", "look", "screenshot", "brief")


def failed_step(raw: object, aimed: dict | None, link_of: Callable[[str], str | None]) -> tuple[dict | None, bool]:
    """One step of a failed run as the bug keeps it (review R185 M4): the step as it was replayed, its `expect`, `if` and
    `optional` kept, with only what can hold a value changed: a typed text is `${typed}`, a link only its place's pattern
    (§18.3, H1), a touch aimed as the touch itself was named (`aimed`, the session's trail entry: a content row is the n-th
    row, M1). (the step, its link had a query or a fragment); None when it can't be written down."""
    if not isinstance(raw, dict) or not isinstance(raw.get("tool"), str):
        return None, False
    tool = raw["tool"]
    if tool == "drive" and raw.get("with"):
        return None, False  # (its values aren't kept, and it can't run without them)
    dropped = {"type_text": ("text",), "open_url": ("url",)}.get(tool, ())
    step: dict = {k: v for k, v in raw.items() if k not in _NOTE_KEYS and k not in dropped
                  and not (k == "target" and tool in NOW_TOUCHES)}
    link_cut = False
    if tool in NOW_TOUCHES:
        if isinstance(aimed, dict) and aimed.get("tool") == tool:
            entry = aimed
        elif tool == "open_url":
            url = raw.get("url")
            entry = {"tool": "open_url", "link": link_of(url) if isinstance(url, str) else None,
                     "link_cut": isinstance(url, str) and ("?" in url or "#" in url)}
        else:
            entry = {**raw, "target": raw.get("target")}
        base = step_of(entry)
        if base is None:
            return None, False
        if base.get("row"):
            return base, False  # (a row is written for a person: no expect; the check says it can't aim at it)
        link_cut = tool == "open_url" and bool(entry.get("link_cut"))
        step.update(base)
    elif tool == "type_text":
        step["text"] = findings.TYPED
    return step, link_cut


def failure_record(*, app: str, identity: dict, when: str, steps: list[dict], failed_at: int, expected: str, got: str,
                   path_from: str | None, place: str | None, logs: list[str], aims: dict[int, dict] | None = None,
                   link_of: Callable[[str], str | None] = lambda url: None) -> dict:
    """What the daemon keeps of a `run` or `replay` that stopped `expect_failed` at step `failed_at` (from 1): the finding
    §25.10 would make (`got` what the screen showed instead, `path` the steps that ran up to and including the failed one,
    as they were replayed: `failed_step`), kept until the agent's next run or replay. `aims`: the session's trail entry of
    each touch step that ran (by its number from 1). A step that can't be written down cuts the path after it. A typed
    value is never kept: a typed step is `${typed}`."""
    path: list[tuple[dict | None, bool]] = [failed_step(raw, (aims or {}).get(n), link_of)
                                            for n, raw in enumerate(steps[:failed_at], 1)]
    last_bad = max((i for i, (s, _) in enumerate(path) if s is None), default=-1)
    kept = path[last_bad + 1:]
    finding = {"id": "f1", "kind": "expect_failed", "app": app, "app_version": identity["app_version"],
               "build": identity["build"], "tool": "tapwright", "tool_version": identity["tool_version"], "when": when,
               "device": identity["device"], "place": place, "path_from": path_from if last_bad < 0 else None,
               "path": [s for s, _ in kept], "expected": expected, "got": got, "picture": None,
               "logs": logs[-findings.MAX_LOGS:], "count": 1}
    return {"finding": finding, "cut": last_bad >= 0, "links_cut": [i for i, (_, c) in enumerate(kept) if c]}


def from_failure(ctx: Context, failure: dict, *, with_picture: bool, reproduced: dict, named: bool = False) -> bugstore.Made:
    """The `expect_failed` finding of the session's last failed `run` or `replay` (`failure` as the daemon kept it). Its
    script is its path: the steps as they were replayed (review R185 M4)."""
    finding = clean(failure["finding"], ctx.safe(), named, ctx.label_check())
    key = place_key_of(ctx, finding["app"], finding.get("place"))
    return _make(ctx, finding, place_key=key, how="failure", key=f"failure/{finding['when']}", run=None,
                 script=None, picture=failure.get("picture"), with_picture=with_picture, reproduced=reproduced,
                 extras={"steps_cut": failure.get("cut", False), "links_cut": list(failure.get("links_cut") or [])},
                 named=named)


# ---- the answer (§29.3) ----

def answer(made: bugstore.Made, scrub: Callable[[str], str], *, reason_shown: bool = True, home: Path | None = None) -> str:
    """The tool's answer. Only the app's text in it (the title's `got`, a check's reason) is read through the redactor
    (review R185b H1): the id, the dates, the builds and the folder are the tool's own and shown as they are."""
    safe = findings.safe_scrub(scrub)
    seen = made.meta["seen"]
    times = f"{seen['count']} {'time' if seen['count'] == 1 else 'times'}"
    builds = seen["builds"]
    shown_builds = f"build {builds[0]}" if len(builds) == 1 else f"builds {', '.join(builds)}"
    title = bug.title(made.finding["kind"], made.finding.get("place"), safe(made.finding["got"]))
    head = f"New bug: {title}" if made.new else f"Known bug (seen {times}): {title}"
    r = made.meta["reproduced"]
    if r.get("checked") is False:
        shown = reason_shown and r.get("why") != bug.NOT_ASKED  # (no check asked for: no reason to give, §29.3; W177)
        line = f"Reproduced: not checked ({safe(str(r['why']))})." if shown else "Reproduced: not checked."
    else:
        n, of = r["n"], r["of"]
        line = f"Reproduced: {n} of {of} runs" + (": not reproduced." if n == 0 else " (flaky)." if n < of else ".")
    lines = [head, line, f"Seen: {times}, {str(seen['last'])[:10]}, {shown_builds}.", f"Folder: {shown_path(made.folder, home)}"]
    return "\n".join(lines)


def shown_path(path: Path, home: Path | None = None) -> str:
    """A path as a person reads it: `~/.tapwright/...` under the home folder, so no user name is shown. A folder
    outside home (a `--bugs` or temporary folder the user chose) is shown whole: the caller has to be able to open it,
    and these answers and audit lines stay on this computer (never in a page, a zip or an issue)."""
    base = str(home if home is not None else Path.home())
    text = str(path)
    if text.startswith(base):
        return ("~" + text[len(base):]).replace("\\", "/")
    return text

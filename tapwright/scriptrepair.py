"""Self-repairing tests (docs/AGENT-TOOLS.md §26), the pure part: what may be repaired and what never is, the candidates of a
break and their score, the files a repair makes (`<script>.repaired.json`, `<script>.repair.md`) and adopting one. No phone and
no daemon: the run, the trials and the acceptance rule are scriptrepairrun.py's.

The score (§26.3) only orders the candidates; it never decides what is accepted. Its parts are the shared ones (`role`,
`id_stem`, `words`, `position`, `component`, each 0 to 1) and Tapwright's weights are

    role 0.15, id_stem 0.35, words 0.35, position 0.10, component 0.05

(the weighted mean of the parts that exist on both sides). Without a step's `seen` (§9.4) Tapwright knows the old target only:
`id_stem` (from an id target) and `words` (from a label or text target), and `role` when the matches of an `ambiguous` all
share one; `position` and `component` need `seen`.
"""
from __future__ import annotations

import datetime
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import batch, explorefloor, ui

MAX_CANDIDATES = 3  # tried per step (§26.4)
MAX_REPAIRS = 5  # made in one script (§26.4)
MAX_SCROLLS = 5  # a scroll candidate scrolls down at most this often (§26.1)
NEAR_TIE = 0.02  # the best two scores closer than this can't be told apart (§26.3)
WEIGHTS = {"role": 0.15, "id_stem": 0.35, "words": 0.35, "position": 0.10, "component": 0.05}
REPAIRABLE = ("tap", "long_press", "toggle", "scroll")  # the tools a step may be repaired in (§26.1)
TRAILING = frozenset({"btn", "button", "link", "tab", "row", "item", "cell", "switch", "toggle", "field", "input"})  # §9.4 id_stem
_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+|[^\W\d_]+")
_REFERENCE = re.compile(r"\$\{(?:account|vault|otp)\.")  # an account, vault or one-time code reference (§18.6, §20.1, §20.4)
_BACK_WORDS = frozenset({"back", "header", "nav", "navigation", "bar", "button", "btn", "arrow", "chevron", "left", "item"})


# ---- the facts of an element (§9.4 `seen`) and the parts of the score (§26.3) ----

def words_of(identifier: str) -> list[str]:
    """An id as lower-case words: split on `-`, `_` and case changes (`loginBtn` is `login`, `btn`)."""
    return [w.lower() for w in _WORD.findall(identifier)]


def id_stem(identifier: str) -> str | None:
    """§9.4: the id lower-cased, with trailing digits and trailing words from `btn`, `button`, `link`, `tab`, `row`, `item`,
    `cell`, `switch`, `toggle`, `field`, `input` removed again and again, with their separators (`save-button` gives `save`,
    `trip-row-1` gives `trip`). The whole id when nothing would be left; None for no id."""
    if not identifier:
        return None
    spans = [(m.group().lower(), m.end()) for m in _WORD.finditer(identifier)]
    keep = len(spans)
    while keep and (spans[keep - 1][0].isdigit() or spans[keep - 1][0] in TRAILING):
        keep -= 1
    return identifier[:spans[keep - 1][1]].lower() if keep else identifier.lower()


def bucket_of(rect: tuple[float, float, float, float], screen: tuple[float, float]) -> str | None:
    """§9.4: the element's centre on a grid of 3 columns and 3 rows over the screen, `c1r1` (top left) to `c3r3`."""
    width, height = screen
    if width <= 0 or height <= 0:
        return None
    cx, cy = rect[0] + rect[2] / 2, rect[1] + rect[3] / 2
    col, row = min(2, max(0, int(cx / (width / 3)))), min(2, max(0, int(cy / (height / 3))))
    return f"c{col + 1}r{row + 1}"


def seen_of(element: ui.Element, items: list[ui.Element], screen: tuple[float, float], private: Callable[[str], bool] = ui.private) -> dict:
    """The `seen` of a step that aims at `element` (§9.4, v1.16): its role, its bucket (left out inside a scroll view), the
    accessibility class as its component and its id's stem. It never holds text. An id that holds an account's value gives
    no stem."""
    out: dict = {"role": element.role}
    inside = ui.scroll_of(items, element) is not None
    if not inside and (bucket := bucket_of(element.rect, screen)) is not None:
        out["bucket"] = bucket
    if element.type:
        out["component"] = element.type
    if element.identifier and not private(element.identifier) and (stem := id_stem(element.identifier)):
        out["id_stem"] = stem
    return out


def clean_seen(value: object) -> dict:
    """A step's `seen` as far as it is well formed: strings only, the four keys only (a hand-written one may be anything)."""
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if k in ("role", "bucket", "component", "id_stem") and isinstance(v, str) and v}


def _match(a: str, b: str) -> bool:
    """Two words match when equal, or when the shorter has at least 3 characters and its letters appear in order in the
    longer with the same first letter (`btn`, `button`)."""
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) < 3 or short[0] != long[0]:
        return False
    rest = iter(long)
    return all(c in rest for c in short)


def id_part(old: list[str], new: list[str]) -> float | None:
    """The matching words over the larger word count (`login-btn` and `login-button`: 1.0); None when either has no words."""
    if not old or not new:
        return None
    free = list(range(len(new)))
    hits = 0
    for word in old:
        for j in free:
            if _match(word, new[j]):
                free.remove(j)
                hits += 1
                break
    return hits / max(len(old), len(new))


def edit_similarity(a: str, b: str) -> float:
    """1 minus the edit distance over the longer length, ignoring case and the space around."""
    a, b = " ".join(a.casefold().split())[:200], " ".join(b.casefold().split())[:200]
    if not a or not b:
        return 0.0
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, row = row, [i]
        for j, cb in enumerate(b, 1):
            row.append(min(prev[j] + 1, row[j - 1] + 1, prev[j - 1] + (ca != cb)))
    return 1 - row[-1] / max(len(a), len(b))


def _neighbours(a: str, b: str) -> float:
    if a == b:
        return 1.0
    try:
        (ca, ra), (cb, rb) = (map(int, (x[1], x[3])) for x in (a, b))
    except (ValueError, IndexError):
        return 0.0
    return 0.5 if abs(ca - cb) <= 1 and abs(ra - rb) <= 1 else 0.0


@dataclass(frozen=True)
class Old:
    """What is known of the element a step aimed at: the old id (a stem when it comes from `seen`), label and text, and the
    facts `seen` gives. A fact that isn't known is left out of the score."""
    id: str | None = None
    id_is_stem: bool = False
    label: str | None = None
    text: str | None = None
    role: str | None = None
    bucket: str | None = None
    component: str | None = None


def old_of(step: dict, ambiguous_roles: set[str] | None = None) -> Old:
    """The old facts of a step: its target, and its `seen` where it has one (§26.3). `ambiguous_roles`: the roles of the
    elements an `ambiguous` target matched; one role is the old role when `seen` doesn't say."""
    target = step.get("target") if isinstance(step.get("target"), dict) else {}
    seen = clean_seen(step.get("seen"))
    role = seen.get("role") or (next(iter(ambiguous_roles)) if ambiguous_roles and len(ambiguous_roles) == 1 else None)
    stem = seen.get("id_stem")
    return Old(id=stem or (target.get("id") if isinstance(target.get("id"), str) else None), id_is_stem=stem is not None,
               label=target.get("label") if isinstance(target.get("label"), str) else None,
               text=target.get("text") if isinstance(target.get("text"), str) else None,
               role=role, bucket=seen.get("bucket"), component=seen.get("component"))


def parts_of(old: Old, element: ui.Element, screen: tuple[float, float]) -> dict[str, float]:
    """The parts of the score that exist on both sides (§26.3)."""
    parts: dict[str, float] = {}
    if old.role is not None:
        parts["role"] = 1.0 if element.role == old.role else 0.0
    if old.id and element.identifier:
        mine = id_stem(element.identifier) if old.id_is_stem else element.identifier
        got = id_part(words_of(old.id), words_of(mine or ""))
        if got is not None:
            parts["id_stem"] = got
    olds = [s for s in (old.label, old.text) if s]
    news = [s for s in (element.label, element.text) if s]
    if olds and news:
        parts["words"] = max(edit_similarity(a, b) for a in olds for b in news)
    if old.bucket is not None and (bucket := bucket_of(element.rect, screen)) is not None:
        parts["position"] = _neighbours(old.bucket, bucket)
    if old.component and element.type:
        parts["component"] = 1.0 if element.type == old.component else 0.0
    return parts


def weighted(parts: dict[str, float]) -> float | None:
    """The weighted mean of the parts that exist; None when none does."""
    total = sum(WEIGHTS[k] for k in parts)
    return sum(WEIGHTS[k] * v for k, v in parts.items()) / total if total else None


def format_parts(parts: dict[str, float]) -> str:
    return ", ".join(f"{k} {parts[k]:.2f}" for k in WEIGHTS if k in parts)


@dataclass
class Scored:
    element: ui.Element
    parts: dict[str, float]
    score: float


def rank(scored: list[Scored]) -> list[Scored]:
    """Best score first; equal scores in reading order (top to bottom, then left to right)."""
    return sorted(scored, key=lambda s: (-round(s.score, 9), s.element.y, s.element.x))


def near_tie(ranked: list[Scored]) -> bool:
    """Are the best two scores less than 0.02 apart? Then they can't be told apart, and the step isn't repaired (§26.3)."""
    return len(ranked) >= 2 and ranked[0].score - ranked[1].score < NEAR_TIE


# ---- what is never repaired (§26.1, §26.2, and the shared edit B1) ----

def can_take(tool: str, element: ui.Element, step: dict) -> bool:
    """Can the element take the step's action? Their line lists the action (§3.3): `tap` and `long_press` tap, `toggle`
    toggles, and a `scroll` that brings its target on screen takes any element (else the scroll view)."""
    if not element.enabled:
        return False
    if tool in ("tap", "long_press"):
        return "tap" in element.actions
    if tool == "toggle":
        return "toggle" in element.actions
    if tool == "scroll":
        return True if step.get("to") == "target" else "scroll" in element.actions
    return False


def aim_names(target: object) -> list[str]:
    """What a policy word is looked for in on a target: its text, label, and its id as words (§25.5)."""
    if not isinstance(target, dict):
        return []
    out = [v for k, v in target.items() if k in ("label", "text") and isinstance(v, str)]
    out += [explorefloor.id_words(v) for k, v in target.items() if k == "id" and isinstance(v, str)]
    return out


def is_back_target(target: object) -> bool:
    """Was the old target a native Back (the navigation bar's back button, §18.2, or the `back` tool)? A target named `Back`,
    or an id made only of `back` and filler words (`nav-back-button`)."""
    if not isinstance(target, dict):
        return False
    for key in ("label", "text"):
        if isinstance(target.get(key), str) and target[key].strip().casefold() == "back":
            return True
    ident = target.get("id")
    if isinstance(ident, str):
        words = set(words_of(ident))
        return "back" in words and words <= _BACK_WORDS
    return False


def holds_reference(step: object) -> bool:
    """Does any string of the step hold an account, vault or one-time code reference? (§18.6, §20.1, §20.4)"""
    if isinstance(step, str):
        return _REFERENCE.search(step) is not None
    if isinstance(step, dict):
        return any(holds_reference(v) for v in step.values())
    if isinstance(step, list):
        return any(holds_reference(v) for v in step)
    return False


def never_repaired(step: dict, floor: explorefloor.Floor, drafts_only: bool = False) -> str | None:
    """Why this step is never repaired, or None (§26.2, with the shared edit B1). The reason is a clause that reads after
    `Repair: none.`"""
    if drafts_only:
        return "a step in a route marked draftsOnly is never repaired"
    tool = step.get("tool")
    if tool not in REPAIRABLE:
        return f"a {tool} step is never repaired (only a tap, long_press, toggle or scroll with a target is)"
    if holds_reference(step) or step.get("secure"):
        return "a step with a typed value, an account reference or a vault reference is never repaired"
    target = step.get("target")
    if not isinstance(target, dict):
        return "a step with no target has nothing to repair"
    if target.get("x") is not None:
        return "a point target names no element, so there is nothing to repair it to"
    if is_back_target(target):
        return "a native Back is never repaired"
    for name in aim_names(target):
        if floor.named(name) is not None:
            return "a step whose target carries a policy word is never repaired"
    return None


# ---- candidates ----

@dataclass
class Candidate:
    """One thing to try for a break: a new target for the step, a road in front of it, or a scroll in front of it."""
    kind: str  # "target" | "road" | "scroll"
    summary: str
    shown: str  # the element, or the inserted moves, as `.repair.md` names them
    why: str
    score: float | None = None
    parts: dict[str, float] | None = None
    target: dict | None = None
    seen: dict | None = None
    moves: list[dict] = field(default_factory=list)
    place: str | None = None
    place_from: str | None = None  # a road: where the app was
    names: dict = field(default_factory=dict)  # a target: every name the element has (id, label, text), for the map


@dataclass
class Analysis:
    """`ok`: candidates to try, best first; `none`: nothing is repaired (`reason`); `tie`: the best two can't be told apart."""
    kind: str
    list: list[Candidate] = field(default_factory=list)
    reason: str = ""


def step_name(step: dict, target: dict | None = None) -> str:
    """How an answer names a step or a candidate: `tap id=login-btn`."""
    return batch.summary({**step, "target": target} if target is not None else step)


def aim_of(target: dict | None) -> str:
    if not isinstance(target, dict):
        return ""
    base = batch.summary({"tool": "tap", "target": target})[4:]
    return f"{base}, index {target['index']}" if "index" in target else base


def element_label(element: ui.Element) -> str:
    """`button "Log in"`: the role and what it shows."""
    text = ui.clip(element.text, 60) if element.text else ""
    return f"{element.role} {text}".strip()


def phrase(record: "Repair") -> str:
    """`tap id=login-btn to id=login-button (score 0.93)`, as the audit line and the answer say a repair (§26.7)."""
    if record.kind == "target":
        was, now = aim_of(record.was), aim_of(record.target)
        score = f" (score {record.score:.2f})" if record.score is not None else ""
        return f"{record.tool} {was} to {now}{score}"
    count = len(record.inserted)
    if record.kind == "scroll":
        return f"{record.tool} {aim_of(record.was)} with scroll down {count} time{'' if count == 1 else 's'} before it"
    moves = ", ".join(step_name(m) for m in record.inserted)
    return f"{record.tool} {aim_of(record.was)} with {moves} before it ({record.place_from} to {record.place})"


@dataclass
class Repair:
    """A repair that was made: the original step (1-based), what it was and what it is now."""
    step: int
    tool: str
    kind: str
    was: dict | None
    target: dict | None = None
    seen: dict | None = None
    score: float | None = None
    parts: dict[str, float] | None = None
    place: str | None = None
    place_from: str | None = None
    shown: str = ""
    why: str = ""
    accepted: str = ""
    inserted: list[dict] = field(default_factory=list)
    names: dict = field(default_factory=dict)


def suggestion(analysis: Analysis, place: str | None) -> list[str]:
    """The lines `replay` with `repair: "suggest"` adds after the usual failure text (§26.5). Nothing was touched, and it
    says so: only `apply` checks."""
    if analysis.kind == "none":
        return [f"Repair (suggest): none. {analysis.reason[:1].upper()}{analysis.reason[1:]}."]
    if analysis.kind == "tie":
        return [f"Repair (suggest, not checked): ambiguous. {analysis.reason[:1].upper()}{analysis.reason[1:]}."]
    lines = ["Repair (suggest, not checked):"]
    for n, cand in enumerate(analysis.list[:MAX_CANDIDATES], 1):
        score = f": score {cand.score:.2f} ({format_parts(cand.parts or {})})" if cand.score is not None and cand.parts else ""
        lines.append(f"{n}. {cand.summary}{score}. Why: {cand.why}")
    lines.append('To check it: replay with repair: "apply".')
    return lines


# ---- the files (§26.6) ----

def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def stamp(at: datetime.datetime) -> str:
    return at.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sibling(script: Path, suffix: str) -> Path:
    """`sign_in.json` -> `sign_in.<suffix>` in the same folder."""
    return script.with_name(f"{script.stem}.{suffix}")


@dataclass
class Work:
    """A step of the run: what runs now, the original step of the file it stands for (None for an inserted one) and what
    was done to it."""
    step: dict
    orig: dict | None
    index: int | None  # the 1-based number in the original script
    repair: dict | None = None  # the `repaired` key it carries in the file


def repaired_script(data: dict, work: list[Work], script: Path, at: datetime.datetime, build: str | None, count: int,
                    version: str, scrub: Callable[[str], str]) -> dict:
    """The repaired script (§26.6): the original's keys and steps, with `repaired` at the top and on each repaired or inserted
    step. A repaired step carries the `seen` of the element that was accepted. Text added here goes through `scrub`; the
    original's own text is kept as it is."""
    top = {"from": script.name, "at": stamp(at), "tool": "tapwright", "build": build or "unknown", "count": count}
    steps: list[dict] = []
    for item in work:
        if item.orig is None:  # an inserted step: a move that is a road, or a scroll
            steps.append({**_file_step(item.step, data, scrub), "repaired": {"inserted": True}})
            continue
        step = _file_step(item.step, data, scrub) if item.repair is not None else dict(item.orig)
        if item.repair is not None:
            step["repaired"] = item.repair
        steps.append(step)
    out: dict = {} if "name" in data else {"repaired": top}
    for key, value in data.items():
        if key == "repaired":
            continue  # (a script that was repaired and adopted before: this run's note replaces the old one)
        out[key] = steps if key == "steps" else value
        if key == "name":
            out["repaired"] = top
    return out


def _file_step(step: dict, data: dict, scrub: Callable[[str], str]) -> dict:
    """A step as the file keeps it. In a route (version 2) every string is a template, so a literal `${` is written `$${`."""
    from . import routes
    out = {k: v for k, v in step.items() if k != "repaired"}
    return routes.escaped(out) if data.get("version") == 2 else out  # type: ignore[return-value]


def repair_markdown(name: str, repairs: list[Repair], at: datetime.datetime, version: str, build: str | None) -> str:
    """`<script>.repair.md` (§26.6): for each repair the step, the old target, the new target, why and the evidence. It holds
    no typed text and no value."""
    lines = [f"# Repairs for {name} ({at.astimezone(datetime.timezone.utc).date().isoformat()}, tapwright {version}, "
             f"build {build or 'unknown'})", ""]
    for n, rec in enumerate(repairs, 1):
        was = aim_of(rec.was)
        if rec.kind == "target":
            now = f"{aim_of(rec.target)} ({rec.shown}{f', place {rec.place}' if rec.place else ''})"
            lines.append(f"{n}. Step {rec.step} ({rec.tool}). Was {was}; now {now}.")
        else:
            moves = ", ".join(step_name(m) for m in rec.inserted)
            lines.append(f"{n}. Step {rec.step} ({rec.tool}). Aimed at {was}; now {moves} first, then the step as it was.")
        lines.append(f"   - Why: {rec.why}")
        if rec.score is not None and rec.parts:
            lines.append(f"   - Score {rec.score:.2f}: {format_parts(rec.parts)}.")
        lines.append(f"   - Accepted: {rec.accepted}")
    return "\n".join(lines) + "\n"


def write_atomic(path: Path, text: str) -> None:
    """A temporary file in the same folder, then a rename: a reader never sees half a file."""
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def write_files(script: Path, repaired: dict, markdown: str) -> list[str]:
    """Write `<script>.repaired.json` and `<script>.repair.md` beside the script, never over it. Their names."""
    out_json, out_md = sibling(script, "repaired.json"), sibling(script, "repair.md")
    if script in (out_json, out_md):
        raise OSError("the repaired file would replace the script itself")
    write_atomic(out_json, json.dumps(repaired, indent=2, ensure_ascii=False) + "\n")
    write_atomic(out_md, markdown)
    return [out_json.name, out_md.name]


# ---- adopt: a person's choice, on the command line only (§26.5) ----

def adopt(script: Path, audit: Callable[[str], bool]) -> tuple[bool, str]:
    """Adopt `<script>.repaired.json`: the `adopted` audit line first (`audit(detail)`; when it can't be written nothing is
    adopted), the original kept as `<script>.orig.json` once (only when that file isn't there yet, so the first original is
    never lost), then the original replaced by the repaired script (a temporary file and a rename) and
    `<script>.repaired.json` removed. `<script>.repair.md` stays. (adopted?, a line for the person)."""
    repaired, orig = sibling(script, "repaired.json"), sibling(script, "orig.json")
    if not repaired.is_file():
        return False, f"nothing to adopt: {repaired.name} isn't there (a run with --repair apply writes it when it passes)"
    try:
        text = repaired.read_text(encoding="utf-8")
        json.loads(text)
    except (OSError, ValueError) as e:
        return False, f"can't adopt {repaired.name}: {getattr(e, 'strerror', None) or e}"
    if not audit("adopted"):
        return False, "the audit log couldn't be written, so nothing was adopted"
    kept = False
    try:
        try:
            with open(orig, "xb") as f:
                f.write(script.read_bytes())
            kept = True
        except FileExistsError:
            pass
        write_atomic(script, text)
        repaired.unlink(missing_ok=True)
    except OSError as e:
        return False, f"couldn't adopt {repaired.name}: {getattr(e, 'strerror', None) or e}"
    first = f"kept the original as {orig.name}" if kept else f"{orig.name} already holds the first original"
    return True, f"Adopted {repaired.name}: {script.name} is the repaired script now; {first}."

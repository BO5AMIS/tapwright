"""The map's explored record (AGENT-TOOLS §28) and the action signature it is keyed by (§25.3).

Each place in the map may carry `explored`: `{"last", "actions": [...], "frontier": [...]}`. An action is `{"sig",
"result", "last"}` with `to` (a `road`), `code` (a `refused`), `why` and `word` (a `skipped`), and `build` (all but a
skip). The functions work on the map as plain JSON, like places.py and roads.py, so they are testable without a phone or a
file; mapfile.py merges the record (`merge_record`) and the `map` tool shows `coverage_line`.

Only `explore` writes the record (§28.2): a `replay`, `suggest` or a person's driving never calls the `mark_*` functions.
Nothing here allows anything (§28.3): a record is never read as a permission, and the floor and the policy are checked at
every touch by the caller.
"""
import copy
import re
from dataclasses import dataclass
from typing import Callable

RESULTS = ("road", "same", "refused", "skipped", "crash", "left")
WHYS = ("policy", "floor", "skip_list", "app_mark", "may_submit", "system")
FRONTIER_MAX = 50  # signatures kept in a frontier, then MORE (§28.1)
MORE = "…"
TEXT_MAX = 80  # characters of a label or text in a signature, after redaction (§25.3)
SIG_MAX = 400  # a longer signature is not one (an id has no cut)
COLUMNS, ROWS = 4, 8  # the position grid of §25.3 (not the 3 by 3 of a script's `seen`)
_PREFIXES = ("id:", "label:", "text:", "role:", "row:")
_CODE = re.compile(r"^[a-z_]{1,40}$")
_NAME = re.compile(r"^[a-z0-9_]{1,60}$")


# ---- the action signature (§25.3) ----

def bucket(x: float, y: float, width: float, height: float, screen_width: float, screen_height: float) -> str:
    """The element's centre on a grid of 4 columns and 8 rows over the screen: `c<col>r<row>` from 0 at the top left."""
    col = int((x + width / 2) / screen_width * COLUMNS) if screen_width > 0 else 0
    row = int((y + height / 2) / screen_height * ROWS) if screen_height > 0 else 0
    return f"c{min(max(col, 0), COLUMNS - 1)}r{min(max(row, 0), ROWS - 1)}"


def signature(*, element_id: str | None = None, label: str | None = None, text: str | None = None, role: str | None = None,
              position: str | None = None, index: int | None = None, scrub: Callable[[str], str] = lambda s: s) -> str | None:
    """The first of `id:`, `label:`, `text:`, `role:<role>@<bucket>` the element has (§25.3), with `#<index>` when the target
    matches several elements. Label and text are redacted, then cut at 80 characters. None when the element has none of the
    four (no `role` and `position`): the signature never uses a point. A list row is `row_signature`, never named by text."""
    if element_id:
        base = f"id:{element_id}"
    elif label:
        base = f"label:{scrub(label)[:TEXT_MAX]}"
    elif text:
        base = f"text:{scrub(text)[:TEXT_MAX]}"
    elif role and position:
        base = f"role:{role}@{position}"
    else:
        return None
    return base if index is None else f"{base}#{index}"


def row_signature(list_signature: str, n: int) -> str:
    """A list row: `row:<signature of the list>#<n>`, n from 1. Never by the row's text (§18.1)."""
    if n < 1:
        raise ValueError("a row's number counts from 1")
    return f"row:{list_signature}#{n}"


def valid_signature(sig: object) -> bool:
    return isinstance(sig, str) and 0 < len(sig) <= SIG_MAX and sig.startswith(_PREFIXES) and sig != MORE


# ---- reading a record ----

def record(data: dict, place: str) -> dict | None:
    """The place's explored record, or None."""
    rec = data.get("places", {}).get(place, {}).get("explored")
    return rec if isinstance(rec, dict) else None


def action(data: dict, place: str, sig: str) -> dict | None:
    rec = record(data, place)
    return next((a for a in rec["actions"] if a["sig"] == sig), None) if rec else None


def is_tried(data: dict, place: str, sig: str, build: str | None) -> bool:
    """Has the explorer no need to try it? Only an action tried on the app's current build counts, so a new build tries
    everything again; a skip holds for every build (§28.1)."""
    held = action(data, place, sig)
    return held is not None and (held["result"] == "skipped" or held.get("build") == build)


def frontier(data: dict, place: str, build: str | None) -> list[str]:
    """What the explorer still has to try on a place: the stored frontier (without its `…`), then the actions tried on
    another build (not skips). A place with no record has no list: the explorer reads the screen."""
    rec = record(data, place)
    if rec is None:
        return []
    out = [s for s in rec["frontier"] if s != MORE]
    out += [a["sig"] for a in rec["actions"] if a["result"] != "skipped" and a.get("build") != build and a["sig"] not in out]
    return out


def cut_short(data: dict, place: str) -> bool:
    """Was the frontier cut at 50 (it ends in `…`)? Then there is more to try than it lists."""
    rec = record(data, place)
    return bool(rec) and MORE in rec["frontier"]


def is_explored(data: dict, place: str, build: str | None) -> bool:
    """A record for the current build and an empty frontier (§28.3). A place with no record is a frontier place."""
    return record(data, place) is not None and not frontier(data, place, build) and not cut_short(data, place)


@dataclass(frozen=True)
class Coverage:
    places: int
    explored: int
    frontier: int


def has_record(data: dict) -> bool:
    return any(record(data, name) is not None for name in data.get("places", {}))


def coverage(data: dict, build: str | None) -> Coverage:
    names = list(data.get("places", {}))
    done = sum(1 for n in names if is_explored(data, n, build))
    return Coverage(len(names), done, len(names) - done)


def coverage_line(data: dict, build: str | None, not_reached: int | None = None) -> str | None:
    """`Coverage: 14 places (12 explored, 2 frontier); known up front, not reached: 3.` (§28.3), or None when no place has
    a record. The last part is left out for a tool with no places known up front (`not_reached` None)."""
    if not has_record(data):
        return None
    c = coverage(data, build)
    tail = "" if not_reached is None else f"; known up front, not reached: {not_reached}"
    return f"Coverage: {c.places} places ({c.explored} explored, {c.frontier} frontier){tail}."


# ---- writing a record (the explorer only) ----

def _ensure(data: dict, place: str, now: str) -> dict | None:
    held = data.get("places", {}).get(place)
    if held is None:
        return None
    rec = held.get("explored")
    if not isinstance(rec, dict):
        rec = held["explored"] = {"last": now, "actions": [], "frontier": []}
    return rec


def _put(rec: dict, entry: dict) -> None:
    rec["actions"] = [a for a in rec["actions"] if a["sig"] != entry["sig"]] + [entry]
    rec["frontier"] = [s for s in rec["frontier"] if s != entry["sig"]]
    rec["last"] = max(str(rec.get("last", "")), entry["last"])


def mark_tried(data: dict, place: str, sig: str, result: str, build: str | None, now: str, *, to: str | None = None,
               code: str | None = None) -> bool:
    """The explorer tried `sig` on `place` (§28.1). `result` is road (`to` names the place it arrived at), same, refused
    (`code` says how), crash or left. False, and nothing written, for an unknown place, a signature or result that isn't
    one, or a result missing what it needs."""
    if result not in RESULTS or result == "skipped" or not valid_signature(sig):
        return False
    if (result == "road") != (to is not None) or (to is not None and not _NAME.match(to)):
        return False
    if (result == "refused") != (code is not None) or (code is not None and not _CODE.match(code)):
        return False
    rec = _ensure(data, place, now)
    if rec is None:
        return False
    entry: dict = {"sig": sig, "result": result}
    if to is not None:
        entry["to"] = to
    if code is not None:
        entry["code"] = code
    if build is not None:
        entry["build"] = build
    entry["last"] = now
    _put(rec, entry)
    return True


def mark_skipped(data: dict, place: str, sig: str, why: str, now: str, word: str | None = None) -> bool:
    """`sig` was skipped by policy (§28.3): kept as such, so the next explore doesn't test it again. It allows nothing."""
    if why not in WHYS or not valid_signature(sig):
        return False
    rec = _ensure(data, place, now)
    if rec is None:
        return False
    entry: dict = {"sig": sig, "result": "skipped", "why": why}
    if word:
        entry["word"] = word
    entry["last"] = now
    _put(rec, entry)
    return True


def mark_seen(data: dict, place: str, sigs: list[str], now: str) -> bool:
    """These signatures are on the place's screen: those neither tried nor skipped nor already listed join the frontier
    (at most 50, then `…`). A signature that is an action's leaves it (§28.2), whatever its build: another build's actions
    come back through `frontier`."""
    rec = _ensure(data, place, now)
    if rec is None:
        return False
    tried = {a["sig"] for a in rec["actions"]}
    listed = [s for s in rec["frontier"] if s != MORE]
    more = MORE in rec["frontier"]
    for sig in sigs:
        if valid_signature(sig) and sig not in tried and sig not in listed:
            listed.append(sig)
    rec["frontier"] = _capped(listed, more)
    rec["last"] = max(str(rec.get("last", "")), now)
    return True


def _capped(sigs: list[str], more: bool) -> list[str]:
    return sigs[:FRONTIER_MAX] + [MORE] if more or len(sigs) > FRONTIER_MAX else sigs


# ---- loading, merging, renaming ----

def clean_record(raw: object) -> dict | None:
    """A record read from a file, as far as it is one: actions that aren't (a bad signature, an unknown result, a result
    without what it needs) are dropped, a signature is held once (the newest), keys this code doesn't know are kept (§28.1:
    a tool keeps the keys it doesn't know). None when it isn't a record at all, so a bad record never makes the map
    unreadable. The frontier is recomputed: an action's signature leaves it."""
    if not isinstance(raw, dict) or not isinstance(raw.get("actions", []), list) or not isinstance(raw.get("frontier", []), list):
        return None
    by_sig: dict[str, dict] = {}
    for a in raw.get("actions", []):
        if not isinstance(a, dict) or not valid_signature(a.get("sig")) or a.get("result") not in RESULTS \
                or not isinstance(a.get("last"), str):
            continue
        if a["result"] == "road" and not (isinstance(a.get("to"), str) and _NAME.match(a["to"])):
            continue
        if a["result"] == "refused" and not (isinstance(a.get("code"), str) and _CODE.match(a["code"])):
            continue
        if a["result"] == "skipped" and a.get("why") not in WHYS:
            continue
        held = by_sig.get(a["sig"])
        if held is None or a["last"] >= held["last"]:
            by_sig[a["sig"]] = a
    sigs: list[str] = []
    for s in raw.get("frontier", []):
        if valid_signature(s) and s not in by_sig and s not in sigs:
            sigs.append(s)
    more = MORE in raw.get("frontier", [])
    last = raw.get("last")
    out = {**raw, "last": last if isinstance(last, str) else max((a["last"] for a in by_sig.values()), default=""),
           "actions": list(by_sig.values()), "frontier": _capped(sigs, more)}
    return out


def rename_targets(rec: dict, mapping: dict[str, str]) -> dict:
    """The record with each action's `to` that is a renamed place under its new name (a place was renamed, §18.1)."""
    if not mapping:
        return rec
    return {**rec, "actions": [{**a, "to": mapping.get(a["to"], a["to"])} if isinstance(a.get("to"), str) else a
                               for a in rec["actions"]]}


def rename_place(data: dict, old: str, new: str) -> None:
    """A place was renamed in a map: every action that arrived at it follows (places._move calls this)."""
    for held in data.get("places", {}).values():
        rec = held.get("explored")
        if isinstance(rec, dict):
            for a in rec.get("actions", []):
                if isinstance(a, dict) and a.get("to") == old:
                    a["to"] = new


def merge_record(disk: dict | None, memory: dict | None) -> dict | None:
    """Two records of one place as one (§28.2): by `sig`, the newest `last` wins (a tie goes to `memory`, the tool's own),
    and the record's `last` is the newer. A side with no record never drops the other's. The frontier is the two lists
    joined without any signature that is an action; it ends in `…` when either was cut or it would pass 50."""
    if disk is None or memory is None:
        held = disk if memory is None else memory
        return copy.deepcopy(held) if held is not None else None
    actions: dict[str, dict] = {a["sig"]: a for a in disk["actions"]}
    for a in memory["actions"]:
        held = actions.get(a["sig"])
        if held is None or a["last"] >= held["last"]:
            actions[a["sig"]] = a
    sigs: list[str] = []
    for s in [*disk["frontier"], *memory["frontier"]]:
        if s != MORE and s not in actions and s not in sigs:
            sigs.append(s)
    more = MORE in disk["frontier"] or MORE in memory["frontier"]
    newer = memory if str(memory.get("last", "")) >= str(disk.get("last", "")) else disk
    other = disk if newer is memory else memory
    return copy.deepcopy({**other, **newer, "last": max(str(disk.get("last", "")), str(memory.get("last", ""))),
                          "actions": list(actions.values()), "frontier": _capped(sigs, more)})

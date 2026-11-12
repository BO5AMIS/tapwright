"""`session_report` / `tapwright report` (docs/AGENT-TOOLS.md §16.1): where one agent's calls went, built from the
action log. Counts per tool, failures by code, and four kinds of waste, each with the call numbers (1 is the first
call in the report). Pure: it reads entries, and the log's folder, nothing else. It never sees typed text, pictures or
log text, because the log holds none."""
from __future__ import annotations

import datetime
import json
import re
from pathlib import Path

from .actionlog import PREFIX, SUFFIX

MAX_NUMBERS = 5  # call numbers shown per flag, then `…`
MAX_TOOLS = 6  # tools named in the first line, then `…`
HUNT = 3  # scrolls in a row that make a hunt
RUN = 3  # actions in a row that make a run
# actions that change the phone and answer with the changes: three of them with nothing read between are one `run`.
# A scroll isn't one (a hunt is its own flag), nor are `run` itself, `record` and `replay`.
RUNNABLE = frozenset({"tap", "long_press", "type_text", "toggle", "back", "swipe", "open_url", "open_app",
                      "close_app", "alert", "press_button", "rotate", "wait"})


def read_entries(folder: Path | None) -> list[dict]:
    """Every entry of the day files in the log's folder, oldest first. A line that isn't an object is skipped."""
    if folder is None or not folder.is_dir():
        return []
    found: list[dict] = []
    for f in sorted(folder.glob(f"{PREFIX}*{SUFFIX}")):
        try:
            text = f.read_text("utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and isinstance(entry.get("t"), str):
                found.append(entry)
    found.sort(key=lambda e: e["t"])  # ISO times in one zone sort as text
    return found


def epoch_ms(stamp: str) -> int | None:
    try:
        return round(datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


_RELATIVE = re.compile(r"(\d+(?:\.\d+)?)\s*([smhd])", re.I)


def parse_since(text: str, now_ms: int) -> int:
    """`--since`: epoch milliseconds, an ISO time ("2026-11-12T10:00"), or how long ago ("30m", "2h", "1d")."""
    text = text.strip()
    if re.fullmatch(r"\d{10,}", text):
        return int(text)
    if (m := _RELATIVE.fullmatch(text)) is not None:
        unit = {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2).lower()]
        return now_ms - round(float(m.group(1)) * unit * 1000)
    try:
        moment = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{text!r} isn't a time: use epoch milliseconds, an ISO time or how long ago (30m, 2h, 1d)") from None
    if moment.tzinfo is None:
        moment = moment.astimezone()  # the computer's own zone
    return round(moment.timestamp() * 1000)


def last_client(entries: list[dict]) -> str | None:
    """The agent that called last."""
    return next((e["client"] for e in reversed(entries) if isinstance(e.get("client"), str)), None)


def client_start(entries: list[dict], client: str | None) -> int | None:
    """The start of a client's session: its first entry."""
    for e in entries:
        if client is None or e.get("client") == client:
            return epoch_ms(e["t"])
    return None


def calls(entries: list[dict], client: str | None, since_ms: int | None) -> list[dict]:
    """The agent's own calls in the window: no step inside a `run` and no call the daemon made for another one."""
    out = []
    for e in entries:
        if client is not None and e.get("client") != client:
            continue
        if e.get("batch") or e.get("nested") or e.get("by") == "person":
            continue
        at = epoch_ms(e["t"])
        if since_ms is not None and (at is None or at < since_ms):
            continue
        out.append(e)
    return out


def _numbers(items: list[str]) -> str:
    shown = ", ".join(items[:MAX_NUMBERS])
    return shown + (", …" if len(items) > MAX_NUMBERS else "")


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def _is_read(e: dict) -> bool:
    return e.get("kind") == "read"


def _target_key(e: dict) -> str:
    return json.dumps(e.get("target"), sort_keys=True)


def looks_after_answers(cs: list[dict]) -> list[int]:
    """A plain look right after an action whose answer already listed the changes."""
    found = []
    for i in range(1, len(cs)):
        prev, e = cs[i - 1], cs[i]
        if (e.get("tool") == "look" and e.get("ok") and not e.get("labels") and not e.get("ocr")
                and not _is_read(prev) and prev.get("ok") and prev.get("listed")):
            found.append(i + 1)
    return found


def retries_after_not_found(cs: list[dict]) -> list[int]:
    """A call that failed `not_found`, then the same tool a call later with another target that worked."""
    found = []
    for i in range(len(cs) - 1):
        e, nxt = cs[i], cs[i + 1]
        if (e.get("code") == "not_found" and e.get("target") and nxt.get("tool") == e.get("tool") and nxt.get("ok")
                and nxt.get("target") and _target_key(nxt) != _target_key(e)):
            found.append(i + 1)
    return found


def scroll_hunts(cs: list[dict]) -> list[tuple[int, int, bool]]:
    """Runs of HUNT or more plain scrolls (looks between them don't end one), as (first call, last call, sure). It is
    sure when the action after the run aimed at an element the tree had off screen before the first scroll (the
    daemon logs `known_off` on that action); otherwise it may only have been a search for something unknown."""
    hunts, i = [], 0
    while i < len(cs):
        if not (cs[i].get("tool") == "scroll" and not cs[i].get("to")):
            i += 1
            continue
        first = last = i
        count, j = 1, i + 1
        while j < len(cs):
            e = cs[j]
            if e.get("tool") == "scroll" and not e.get("to"):
                last, count = j, count + 1
            elif not (e.get("tool") == "look" and e.get("ok")):
                break
            j += 1
        if count >= HUNT:
            after = next((e for e in cs[last + 1:] if e.get("tool") != "look"), None)
            hunts.append((first + 1, last + 1, bool(after and after.get("known_off"))))
        i = last + 1
    return hunts


def action_runs(cs: list[dict]) -> list[tuple[int, int]]:
    """RUN or more actions that worked in a row with no read between them."""
    runs, start = [], None
    for i, e in enumerate(cs + [{}]):
        if e.get("tool") in RUNNABLE and e.get("ok") and not _is_read(e):
            start = i if start is None else start
            continue
        if start is not None and i - start >= RUN:
            runs.append((start + 1, i))
        start = None
    return runs


def build(entries: list[dict], client: str | None, since_ms: int | None) -> str:
    """The answer of `session_report`."""
    cs = calls(entries, client, since_ms)
    if not cs:
        return "Session: 0 calls.\nWaste: none."
    counts: dict[str, int] = {}
    failed: dict[str, int] = {}
    for e in cs:
        counts[e.get("tool", "?")] = counts.get(e.get("tool", "?"), 0) + 1
        if e.get("ok") is False:
            code = e.get("code") or "error"
            failed[code] = failed.get(code, 0) + 1
    first, last = epoch_ms(cs[0]["t"]), epoch_ms(cs[-1]["t"])
    minutes = max(1, round((last - first) / 60000)) if first is not None and last is not None else 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    tools = ", ".join(f"{t} {n}" for t, n in ranked[:MAX_TOOLS]) + (", …" if len(ranked) > MAX_TOOLS else "")
    bad = sorted(failed.items(), key=lambda kv: (-kv[1], kv[0]))
    head = (f"Session: {len(cs)} {_plural(len(cs), 'call', 'calls')} in {minutes} min ({tools}). "
            f"Failed: {sum(failed.values())}" + (f" ({', '.join(f'{c} {n}' for c, n in bad)})." if bad else "."))
    flags = []
    if looks := looks_after_answers(cs):
        n = len(looks)
        flags.append(f"- {n} {_plural(n, 'look', 'looks')} right after an action whose answer already had the changes "
                     f"(calls {_numbers([str(x) for x in looks])})")
    if retries := retries_after_not_found(cs):
        n = len(retries)
        flags.append(f"- {n} {_plural(n, 'retry', 'retries')} after not_found that found the same element by another "
                     f"target (calls {_numbers([str(x) for x in retries])}): use its id first")
    if hunts := scroll_hunts(cs):
        n = len(hunts)
        spans = [f"{a}–{b}" + ("" if sure else " (maybe)") for a, b, sure in hunts]
        flags.append(f"- {n} scroll {_plural(n, 'hunt', 'hunts')} of {HUNT} or more scrolls for an element already in "
                     f"the list (calls {_numbers(spans)}): let the action scroll")
    if runs := action_runs(cs):
        n = len(runs)
        flags.append(f"- {n} {_plural(n, 'run', 'runs')} of {RUN} or more actions with no answer read in between "
                     f"(calls {_numbers([f'{a}–{b}' for a, b in runs])}): one run each")
    return "\n".join([head, "Waste:", *flags] if flags else [head, "Waste: none."])

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
IDLE_S = 120  # a gap between one call's answer and the next call longer than this is idle: left out of the thinking time
TIMED_TOOLS = 3  # tools named with their median time in the Time: line, the slowest by total time, then `…`
NEW_PROCESS = 5  # thin calls in a row that make a flag (1.13.1, item 11: five, as Tapline's)
RUNNABLE = frozenset({"tap", "long_press", "type_text", "toggle", "back", "swipe", "open_url", "open_app",
                      "close_app", "alert", "press_button", "rotate", "wait"})


def read_entries(folder: Path | None, since_ms: int | None = None) -> list[dict]:
    """Every entry of the day files in the log's folder, oldest first. A line that isn't an object is skipped. With
    `since_ms`, only the day files that can hold an entry from then on are read (a day file is named for its UTC
    date; one day more is kept for the edge)."""
    if folder is None or not folder.is_dir():
        return []
    oldest = None
    if since_ms is not None:
        try:
            oldest = (datetime.datetime.fromtimestamp(since_ms / 1000, datetime.timezone.utc)
                      - datetime.timedelta(days=1)).date()
        except (OverflowError, OSError, ValueError):
            oldest = None
    found: list[dict] = []
    for f in sorted(folder.glob(f"{PREFIX}*{SUFFIX}")):
        if oldest is not None:
            try:
                if datetime.date.fromisoformat(f.name[len(PREFIX):-len(SUFFIX)]) < oldest:
                    continue
            except ValueError:
                pass  # not named for a day: read it as before
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
SECONDS_BELOW = 100_000_000_000  # an epoch number below this is in seconds (as ms it would be before March 1973)


def to_epoch_ms(value: float) -> int:
    """An epoch time given in seconds (10 digits, like 1762950000) or milliseconds (13), as milliseconds."""
    return int(value * 1000) if value < SECONDS_BELOW else int(value)


def parse_since(text: str, now_ms: int) -> int:
    """`--since`: epoch seconds or milliseconds, an ISO time ("2026-11-12T10:00"), or how long ago ("30m", "2h", "1d")."""
    text = text.strip()
    if re.fullmatch(r"\d{10,}", text):
        return to_epoch_ms(int(text))
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
                and not e.get("screenshot")  # a look that asked for the picture adds something
                and not _is_read(prev) and prev.get("ok") and prev.get("listed")):
            found.append(i + 1)
    return found


def retries_after_not_found(cs: list[dict]) -> list[int]:
    """A call that failed `not_found`, then the same tool in the next call (or the one after, with one `look`
    between) with another kind of target (text, then id) that worked: likely the same element."""
    found = []
    for i in range(len(cs) - 1):
        e = cs[i]
        if e.get("code") != "not_found" or not e.get("target"):
            continue
        j = i + 1
        if cs[j].get("tool") == "look" and e.get("tool") != "look" and j + 1 < len(cs):
            j += 1
        nxt = cs[j]
        if (nxt.get("tool") == e.get("tool") and nxt.get("ok") and nxt.get("target")
                and _target_kind(nxt) != _target_kind(e)):
            found.append(i + 1)
    return found


def _target_kind(e: dict) -> str:
    """How the call named its target: id, label, text, ref or point."""
    t = e.get("target") or {}
    return next((k for k in ("id", "label", "text", "ref") if k in t), "point")


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


def new_process_runs(cs: list[dict]) -> list[tuple[int, int]]:
    """NEW_PROCESS or more calls in a row that came from `tapwright call`, each from a process of its own.

    How the daemon can tell (chosen 2026-11-12): not by the client id changing every call, because this report is one
    client's calls (a host that starts a new process per call and doesn't pass --client gives each call its own client,
    so no report holds two of them); and not by each call opening a new connection, because client.py (the MCP server's
    way in) opens one for every call too. So the thin call says so itself: it sends `X-Tapwright-Via: call`, and the
    daemon logs `"via": "call"`. A call that carries none (the MCP server, or an older client) is never counted."""
    runs, start = [], None
    for i, e in enumerate(cs + [{}]):
        if e.get("via") == "call":
            start = i if start is None else start
            continue
        if start is not None and i - start >= NEW_PROCESS:
            runs.append((start + 1, i))
        start = None
    return runs


def _half_up(x: float) -> int:
    return int(x + 0.5)


def _span(ms: float) -> str:
    """A length of time for the Time: line: `31 min`, `19 s`, or `2.4 s` under ten seconds."""
    if ms >= 60_000:
        return f"{_half_up(ms / 60_000)} min"
    return f"{ms / 1000:.1f} s" if ms < 10_000 else f"{_half_up(ms / 1000)} s"


def _median_ms(values: list[float]) -> float:
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def timing(cs: list[dict]) -> str | None:
    """The `Time:` line (AGENT-TOOLS §16.1, v1.13), or None when no call in the window has `ms` (a log from before it).
    Thinking time is the gap between one call's answer and this client's next call: an entry is written when its call
    ends, so the next call began at its `t` minus its `ms`; a gap over IDLE_S is idle and left out, a next call with no
    `ms` has no gap we can measure. Times in the tool are per call, medians per tool."""
    timed = [(e, e["ms"]) for e in cs if isinstance(e.get("ms"), (int, float)) and not isinstance(e["ms"], bool)
             and e["ms"] >= 0]
    if not timed:
        return None
    by_tool: dict[str, list[float]] = {}
    for e, ms in timed:
        by_tool.setdefault(e.get("tool", "?"), []).append(ms)
    gaps: list[float] = []
    for prev, e in zip(cs, cs[1:]):
        ms, ended, now = e.get("ms"), epoch_ms(prev["t"]), epoch_ms(e["t"])
        if not isinstance(ms, (int, float)) or isinstance(ms, bool) or ms < 0 or ended is None or now is None:
            continue
        gap = max(0.0, now - ms - ended)
        if gap <= IDLE_S * 1000:
            gaps.append(gap)
    tool_total = sum(ms for _, ms in timed)
    ranked = sorted(by_tool.items(), key=lambda kv: (-sum(kv[1]), kv[0]))
    named = ", ".join(f"{t} {_median_ms(v) / 1000:.1f} s" for t, v in ranked[:TIMED_TOOLS])
    named += ", …" if len(ranked) > TIMED_TOOLS else ""
    in_tool = f"in the tool {_span(tool_total)} (median {_median_ms([ms for _, ms in timed]) / 1000:.1f} s; {named})"
    if not gaps:
        return f"Time: {_span(tool_total)}: {in_tool}."
    thinking = f"thinking between calls {_span(sum(gaps))} (median {_median_ms(gaps) / 1000:.1f} s)"
    return f"Time: {_span(sum(gaps) + tool_total)}: {thinking}, {in_tool}."


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
    retried = retries_after_not_found(cs)
    for ids, advice in ((False, "use its id first"),  # (1.13.1 §4: when the id was what failed, that is no advice)
                        (True, "target what the latest look shows (its id there, when it has one)")):
        if retries := [i for i in retried if (_target_kind(cs[i - 1]) == "id") == ids]:
            n = len(retries)
            flags.append(f"- {n} {_plural(n, 'retry', 'retries')} after not_found then worked with another kind "
                         f"of target, likely the same element (calls {_numbers([str(x) for x in retries])}): {advice}")
    if hunts := scroll_hunts(cs):
        n = len(hunts)
        spans = [f"{a}–{b}" + ("" if sure else " (maybe)") for a, b, sure in hunts]
        flags.append(f"- {n} scroll {_plural(n, 'hunt', 'hunts')} of {HUNT} or more scrolls for an element already in "
                     f"the list (calls {_numbers(spans)}): let the action scroll")
    if runs := action_runs(cs):
        n = len(runs)
        flags.append(f"- {n} {_plural(n, 'run', 'runs')} of {RUN} or more actions with no answer read in between "
                     f"(calls {_numbers([f'{a}–{b}' for a, b in runs])}): one run each")
    if fresh := new_process_runs(cs):
        n = sum(b - a + 1 for a, b in fresh)
        flags.append(f"- {n} calls each from a new process (calls {_numbers([f'{a}–{b}' for a, b in fresh])}): load "
                     "the MCP server once; each call paid about 2 s")
    if named := [str(i) for i, e in enumerate(cs, 1) if e.get("tool") == "compare_screen" and e.get("ok")
                 and isinstance(e.get("named"), int) and e["named"] > 0]:
        n = len(named)
        flags.append(f"- {n} picture {_plural(n, 'check', 'checks')} matched with named changes under the threshold "
                     f"(calls {_numbers(named)}): look at what changed")
    time_line = timing(cs)
    top = [head, time_line] if time_line else [head]
    if okays := [e["confirmed_by"] for e in cs if e.get("confirmed") and isinstance(e.get("confirmed_by"), str)]:
        # where each person's OK came from (AGENT-TOOLS §11.3): chat is the host's dialog
        top.append(f"Approvals: {len(okays)} by a person ("
                   + ", ".join(f"{okays.count(w)} in {w}" for w in dict.fromkeys(okays)) + ").")
    return "\n".join([*top, "Waste:", *flags] if flags else [*top, "Waste: none."])

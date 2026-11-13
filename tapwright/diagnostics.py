"""Diagnostics (docs/AGENT-TOOLS.md §15): the app's own log, its crash reports, and the lines a failure carries.

Everything an agent reads is worded here, in constants, so the words are changed in one place. Log lines are the
app's own process's only, and are kept in memory (a ring per app); they are never written to a file. The phone side
(os_trace and the crash reports) is in device.py; the rules for what may be shown are in daemon.py."""
from __future__ import annotations

import dataclasses
import datetime
import json
import posixpath
import re
from collections import deque
from pathlib import Path
from typing import Callable, Iterable

# ---- logs ----
LOG_RING = 2000  # entries kept per app while the daemon runs
LOG_LEVELS = ("info", "warning", "error")  # weakest first: a level asks for itself and everything above
LOG_DEFAULT_LIMIT, LOG_MAX_LIMIT = 100, 500
LOG_TEXT_CUT = 300  # characters of one entry's text shown
LOG_TEXT_KEPT = 2000  # characters of one entry's text kept (os_trace lines can be 64 KB; 2000 of them per app would be too much)
FAILURE_LOGS = 20  # entries a failed step adds
# os_log has no "warning": its levels are default/info/debug, error and fault. Errors and faults are `error`. A line
# logged at the default level (NSLog, print, React Native's console in a release build) is `warning` when its own words
# say so, else `info`.
WARNING_WORDS = re.compile(r"\b(warn(ing)?|error|exception|fatal|failed|failure)\b", re.IGNORECASE)
LEVEL_ERROR = (0x10, 0x11)  # SyslogLogLevel.ERROR, FAULT
NO_ENTRIES = "No log entries"
LATEST = "Latest: {ms}"
FAILURE_HEAD = "Logs (since step {n}):"
LOGS_NO_APP = "no app under test yet: open_app one first, then logs shows its log."
LOGS_NO_SOURCE = "this phone can't stream an app's log (no log service)."
LOG_STREAM_FAILS = 5  # tries in a row to start an app's log stream, then the daemon stops trying and logs says so

# ---- crashes ----
CRASH_RECENT = 5  # `all: true` shows this many
CRASH_FRAMES = 8
CRASH_REASON_CUT = 200
CRASH_SLACK = 30.0  # seconds: the phone's clock and this computer's can differ a little
CRASHED = "the app crashed"  # how both notes begin
NOTE_CRASHED = CRASHED + ": {what}. logs and crash show more."
NOTE_CRASHED_AT = CRASHED + " at {time}: {what}. logs and crash show more."  # a crash only known on a later call
NONE_SINCE = "No crash report for {app} since the session began."
CRASH_NO_EXECUTABLE = ("{app} has no executable name on record (only apps you installed have one), so its crash "
                       "reports can't be told from other apps'.")
CRASH_UNREADABLE = "{n} crash report(s) of this app couldn't be read (half written, or a format this version doesn't know) and were skipped."
CRASH_MAX_MB = 8  # a crash report is read only up to this size (real ones are 0.1 to 2 MB); a bigger file is skipped, said so
CRASH_TOO_BIG = "{n} crash report(s) of this app were too big to read (over {mb} MB) and were skipped."
CRASH_INCOMPLETE = ("Only the newest {n} crash reports with this app's file name were looked at, so an older one of "
                    "this app may exist.")
SAVED = "Saved: {path}"
CRASH_MARK = "*"  # in front of a frame of the app's own code
FAILURE_CRASH_FILE = "Crash report: {path}"


@dataclasses.dataclass(frozen=True)
class LogEntry:
    at: int  # epoch ms
    level: str  # error, warning or info
    text: str


def level_of(raw: int, text: str) -> str:
    """error, warning or info for an os_trace level byte and the line's text (see WARNING_WORDS)."""
    if raw in LEVEL_ERROR:
        return "error"
    return "warning" if WARNING_WORDS.search(text) else "info"


class LogRing:
    """The last LOG_RING entries of one app, oldest first."""

    def __init__(self, size: int = LOG_RING) -> None:
        self.entries: deque[LogEntry] = deque(maxlen=size)
        self.added = 0  # how many entries were ever added: a mark for discard_since
        self.unvouched = 0  # how many of the newest entries aren't yet shown to be of the app's own process: no answer has them

    def add(self, entry: LogEntry, vouched: bool = True) -> None:
        """`vouched=False`: the entry's process isn't yet shown to be the app's (a pid can be reused by another app), so
        it is kept but not shown by any answer until vouch() says it is the app's."""
        if len(entry.text) > LOG_TEXT_KEPT:
            entry = dataclasses.replace(entry, text=entry.text[:LOG_TEXT_KEPT])
        self.entries.append(entry)
        self.added += 1
        if not vouched or self.unvouched:
            self.unvouched = min(self.unvouched + 1, len(self.entries))

    def vouch(self, upto: int) -> None:
        """The process was shown to be the app's when `added` was `upto`: what was added until then is shown."""
        self.unvouched = min(self.unvouched, max(0, self.added - upto))

    def shown(self) -> list[LogEntry]:
        """The entries an answer may use: all but the newest `unvouched`."""
        kept = list(self.entries)
        return kept[:len(kept) - self.unvouched] if self.unvouched else kept

    def discard_since(self, mark: int, from_at: int | None = None) -> int:
        """Take back what was added after `mark` (the `added` count then): all of it, or only the entries at `from_at`
        (epoch ms) or later. For lines whose process was found not to be the app's. How many were taken back."""
        tail = min(self.added - mark, len(self.entries))
        if tail <= 0:
            return 0
        kept = list(self.entries)
        recent = kept[len(kept) - tail:]
        keep = [e for e in recent if from_at is not None and e.at < from_at]
        self.entries.clear()
        self.entries.extend([*kept[:len(kept) - tail], *keep])
        self.unvouched = min(self.unvouched, len(keep))
        return tail - len(keep)

    def discard_unvouched(self, from_at: int | None = None) -> int:
        """Take back the entries no answer may use yet (all, or only those at `from_at` or later): they were read while
        another process held the pid, or nothing shows whose they are. (Exact even after an earlier discard, which
        leaves `added` counting what it took back.)"""
        return self.discard_since(self.added - self.unvouched, from_at)

    def newest(self) -> tuple[int, frozenset[str]] | None:
        """(the newest entry's time, the texts of the entries at that time): where a stream that starts again for the
        same process must pick up, so the phone's replay of what it already gave isn't added twice."""
        if not self.entries:
            return None
        at = self.entries[-1].at
        return at, frozenset(e.text[:LOG_TEXT_KEPT] for e in self.entries if e.at == at)

    def add_unseen(self, entry: LogEntry, after: tuple[int, frozenset[str]] | None, vouched: bool = True) -> bool:
        """Add `entry` unless `after` (from newest()) says it is already here: older than the newest entry, or the
        same time and text as one. True when it was added."""
        if after is not None and (entry.at < after[0] or (entry.at == after[0] and entry.text[:LOG_TEXT_KEPT] in after[1])):
            return False
        self.add(entry, vouched)
        return True

    def latest(self, shown: bool = False) -> int | None:
        """The newest entry's time (`shown`: of the entries an answer may use)."""
        entries = self.shown() if shown else self.entries
        return entries[-1].at if entries else None

    def select(self, since: int | None = None, level: str = "info", grep: str | None = None,
               limit: int = LOG_DEFAULT_LIMIT) -> list[LogEntry]:
        """The newest `limit` entries newer than `since`, at `level` or above, containing `grep`; oldest first."""
        floor = LOG_LEVELS.index(level)
        needle = grep.casefold() if grep else None
        found = [e for e in self.shown() if (since is None or e.at > since) and LOG_LEVELS.index(e.level) >= floor
                 and (needle is None or needle in e.text.casefold())]
        return found[-limit:]


def cut(text: str, limit: int = LOG_TEXT_CUT) -> str:
    """Text cut at `limit` characters, with how many are missing (as element text is cut: `…(+57)`). A test account's
    value is redacted first (this call's accounts): cut first, a value half inside the limit would no longer match."""
    from . import ui  # (ui reads device, which reads this module)
    flat = " ".join(ui.shown(text).split())
    missing = len(flat) - limit
    return flat[:limit] + (f"…(+{missing})" if missing > 0 else "")


def clock_of(ms: int) -> str:
    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%H:%M:%S.%f")[:-3]


def line_of(entry: LogEntry) -> str:
    return f"{clock_of(entry.at)} {entry.level} {cut(entry.text)}"


def no_entries(since: int | None, level: str, grep: str | None) -> str:
    asked = []
    if level != "info":
        asked.append(f"at {level} or above")
    if grep:
        asked.append(f'matching "{grep}"')
    if since is not None:
        asked.append(f"since {clock_of(since)}")
    return " ".join([NO_ENTRIES, *asked]) + "."


def logs_answer(ring: LogRing | None, since: int | None, level: str, grep: str | None, limit: int) -> tuple[str, int]:
    """(the answer's text, the number of entry lines) for a `logs` call."""
    entries = ring.select(since, level, grep, limit) if ring is not None else []
    latest = (ring.latest(shown=True) if ring is not None else None) or since or 0
    lines = [line_of(e) for e in entries] or [no_entries(since, level, grep)]
    return "\n".join([*lines, LATEST.format(ms=latest)]), len(entries)


def failure_logs(ring: LogRing | None, step: int, since: int) -> list[str]:
    """The lines a failed step adds: its heading and up to FAILURE_LOGS entries at warning or above since it began.
    Empty when there are none."""
    if ring is None:
        return []
    entries = ring.select(since - 1, "warning", None, FAILURE_LOGS)
    return [FAILURE_HEAD.format(n=step), *(line_of(e) for e in entries)] if entries else []


LOG_LINE = re.compile(r"^\d\d:\d\d:\d\d\.\d{3} (error|warning|info) ")


def junit_output(message: str) -> str:
    """What a failed replay's JUnit <system-out> holds: the answer's `Logs (since step N):` block and its crash lines."""
    out: list[str] = []
    lines = message.splitlines()
    for i, line in enumerate(lines):
        if line.startswith(FAILURE_HEAD.split("{")[0]):
            out.append(line)
            for follow in lines[i + 1:]:
                if not LOG_LINE.match(follow):
                    break
                out.append(follow)
        elif line.startswith("Note: " + CRASHED) or line.startswith(FAILURE_CRASH_FILE.split("{")[0]):
            out.append(line)
    return "\n".join(out)


# ---- crash reports ----
@dataclasses.dataclass(frozen=True)
class CrashInfo:
    when: datetime.datetime | None
    kind: str  # the exception type, e.g. EXC_BAD_ACCESS (SIGSEGV)
    reason: str
    frames: tuple[str, ...]  # the crashed thread's top frames, each `<n> [*] <image> <symbol>`
    top: str  # where, in one phrase: the top frame of the app's own code, else the top frame
    filename: str  # the report's name on the phone, no folders
    remote: str = ""  # its path on the phone, for pull


@dataclasses.dataclass
class CrashSearch:
    """What a look at the phone's crash folder found: the app's reports (newest first) and how many reports of its
    file name couldn't be read (they are passed over, and the answer says so)."""
    reports: list
    unreadable: int = 0
    too_big: int = 0  # files over CRASH_MAX_MB, never read
    incomplete: bool = False  # the search stopped at its budget with reports of the file name still unlooked at
    scanned: int = 0  # how many files the search looked at


def skipped_notes(search: "CrashSearch") -> list[str]:
    """What a crash answer says was left out of the search, one line each."""
    notes = []
    if search.unreadable:
        notes.append(CRASH_UNREADABLE.format(n=search.unreadable))
    if search.too_big:
        notes.append(CRASH_TOO_BIG.format(n=search.too_big, mb=CRASH_MAX_MB))
    if search.incomplete:
        notes.append(CRASH_INCOMPLETE.format(n=search.scanned))
    return notes


def _safely(fn: Callable[[], object], default=None):
    try:
        return fn()
    except Exception:  # a report this version of the parser can't read in part still says what it can
        return default


def _reason_of(report, redact: Callable[[str], str] = str) -> str:
    sub = _safely(lambda: report.exception_subtype)
    if sub:
        return redact(str(sub))
    info = _safely(lambda: report.application_specific_information)
    if isinstance(info, dict):
        info = " ".join(str(x) for v in info.values() for x in (v if isinstance(v, (list, tuple)) else [v]))
    elif isinstance(info, (list, tuple)):
        info = " ".join(str(x) for x in info)
    return " ".join(redact(str(info)).split())[:CRASH_REASON_CUT] if info else ""  # (redacted before the cut)


def _is_app_frame(image: str | None, executable: str) -> bool:
    """The app's own code: the main executable (not a framework inside the bundle)."""
    if not image:
        return False
    return posixpath.basename(image) == executable and (".app/" in image or image == executable)


def _frame_text(frame) -> str:
    symbol = getattr(frame, "symbol", None)
    if symbol:
        offset = getattr(frame, "symbol_offset", None)
        return f"{symbol} +{offset}" if offset is not None else str(symbol)
    offset = getattr(frame, "image_offset", None)
    return f"+{offset}" if offset is not None else "?"


def _when_of(report) -> datetime.datetime | None:
    """The crash's time with the report's own UTC offset when the header has one (`2026-11-12 10:10:10.00 +0300`):
    pycrashreport drops the offset, and the phone's zone isn't this computer's. Without an offset, the naive time."""
    meta = _safely(lambda: report._metadata)
    raw = meta.get("timestamp") if isinstance(meta, dict) else None
    if isinstance(raw, str):
        text, _, zone = raw.strip().rpartition(" ")
        if re.fullmatch(r"[+-]\d{4}", zone):
            try:
                parsed = datetime.datetime.strptime(f"{text} {zone}", "%Y-%m-%d %H:%M:%S.%f %z")
            except ValueError:
                parsed = None
            if parsed is not None:
                return parsed
    return _safely(lambda: report.timestamp)


def describe_crash(report, executable: str, redact: Callable[[str], str] = str) -> CrashInfo:
    """A parsed crash report (pycrashreport's) as the few facts `crash` shows. `redact` takes a test account's value out
    of every text taken from the report, before any cut."""
    kind = redact(str(_safely(lambda: report.exception_type) or "unknown exception"))
    frames = list(_safely(lambda: report.frames, []) or [])
    shown: list[str] = []
    top_app = top_any = None
    for n, frame in enumerate(frames):
        image = posixpath.basename(getattr(frame, "image_name", None) or "?")
        mine = _is_app_frame(getattr(frame, "image_name", None), executable)
        text = redact(f"{image} {_frame_text(frame)}")
        top_any = top_any or text
        top_app = top_app or (text if mine else None)
        if n < CRASH_FRAMES:
            shown.append(f"{n:>2} {CRASH_MARK if mine else ' '} {text}")
    where = top_app or top_any
    return CrashInfo(_when_of(report), kind, _reason_of(report, redact), tuple(shown),
                     f"{kind} in {where}" if where else kind,
                     posixpath.basename(str(_safely(lambda: report.filename) or "")),
                     str(_safely(lambda: report.filename) or ""))


def note_of(info: CrashInfo, late: bool = False) -> str:
    """The note an answer ends with when the app stopped: `late` for a crash found on a later call (with its time)."""
    if late and info.when is not None:
        return NOTE_CRASHED_AT.format(time=info.when.strftime("%H:%M:%S"), what=info.top)
    return NOTE_CRASHED.format(what=info.top)


def crash_lines(info: CrashInfo, app: str) -> list[str]:
    when = info.when.strftime("%Y-%m-%d %H:%M:%S") if info.when else "unknown time"
    lines = [f"Crash of {app} at {when}.", f"Exception: {info.kind}" + (f" ({info.reason})" if info.reason else "")]
    if info.frames:
        lines.append(f"Crashed thread, top {min(CRASH_FRAMES, len(info.frames))} frames ({CRASH_MARK} = the app's own code):")
        lines += [f"  {f}" for f in info.frames]
    else:
        lines.append("No frames in the report.")
    lines.append(f"File: {info.filename}")
    return lines


def crash_pattern(executable: str) -> str:
    """The reports' file names for an executable: `Name-2026-11-12-101010.ips` (a retired report has no folder here).
    The name is followed by the date and nothing else: `Name-Dev-…` is another process's, and `Name.cpu_resource-…` is
    a resource report, not a crash."""
    return rf"^{re.escape(executable)}-\d{{4}}-\d\d-\d\d-\d{{6}}(?:[-.]\d+)?\.ips$"


def newer(info: CrashInfo, than: datetime.datetime) -> bool:
    """Is the crash at `than` or later (a little earlier counts: the clocks differ)? A time with the report's own
    offset is compared as the moment it is; a naive one is taken as this computer's local time."""
    if info.when is None:
        return False
    when = info.when
    if (when.tzinfo is None) != (than.tzinfo is None):
        when, than = when.astimezone(), than.astimezone()  # a naive time is local time to astimezone
    return when >= than - datetime.timedelta(seconds=CRASH_SLACK)


class Unsanitizable(Exception):
    """A report that can't be made free of the test accounts' values."""


CRASH_UNSAFE = "the crash report couldn't be saved safely (it holds a test account's value that couldn't be removed), so nothing was saved"


def sanitized_report(data: bytes, redact: Callable[[str], str]) -> bytes:
    """A crash report as it may be saved: a test account's value (`redact`) replaced in its content, its format kept
    (Codex, PR #10 cycles 5 and 6). An .ips is JSON documents one after another (a header line, then the body, each on
    one line or several; JSON lines too). Each is parsed, and every string in it, key or value, is redacted as decoded:
    a value the raw bytes hold only escaped (a `repr` inside a JSON string, a unicode escape) is found too. A document
    that needs no change stays byte for byte; one that does is written as JSON again (one line stays one line). A report
    that isn't JSON (a .crash text) is redacted as text. Bytes that aren't UTF-8 pass through the same way."""
    text = data.decode("utf-8", errors="surrogateescape")

    def walk(value):
        if isinstance(value, str):
            return redact(value)
        if isinstance(value, dict):
            return {redact(k): walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        shown = json.dumps(value)  # a number, true, false or null: its JSON text can be an account value (a PIN, a user id)
        return value if redact(shown) == shown else redact(shown)  # (becomes the placeholder text: the JSON stays valid)

    parts: list[str] = []
    changed = False
    decoder, at = json.JSONDecoder(), 0
    try:
        while at < len(text):
            if text[at].isspace():
                end = at
                while end < len(text) and text[end].isspace():
                    end += 1
                parts.append(text[at:end])
                at = end
                continue
            parsed, end = decoder.raw_decode(text, at)
            raw = text[at:end]
            clean = walk(parsed)
            if clean == parsed and redact(raw) == raw:
                parts.append(raw)
            else:
                changed = True
                many = chr(10) in raw
                parts.append(json.dumps(clean, ensure_ascii=False, indent=2 if many else None,
                                        separators=None if many else (",", ":")))
            at = end
    except ValueError:
        parts, changed = [redact(text)], True  # not JSON: a text report
    final = "".join(parts)
    if redact(final) != final:  # a value form is still in it (a number written as 8.36291e5, a split form): nothing is saved
        raise Unsanitizable(CRASH_UNSAFE)
    return final.encode("utf-8", errors="surrogateescape") if changed and final != text else data


def crash_folder(bundle_id: str) -> Path:
    """Where `crash` with pull puts the full report. A function, so tests can move it."""
    from .store import HOME
    return HOME / "crashes" / bundle_id


def safe_name(name: str) -> str:
    """A report's file name for a local path: its last part, with nothing that leaves the folder."""
    base = posixpath.basename(name.replace("\\", "/"))
    return re.sub(r"[^A-Za-z0-9._-]", "_", base).lstrip(".") or "crash.ips"


CRASH_BUG_TYPES = ("109", "309")  # iOS's user-mode crash report types; cpu_resource, wakeups_resource and the like aren't crashes


def _bundle_ids_in(report) -> list[str]:
    """The bundle ids a report names: `bundleID` in the first JSON line of an .ips file, and `bundleInfo` inside it."""
    found: list[str] = []
    meta = _safely(lambda: report._metadata)
    if isinstance(meta, dict) and isinstance(meta.get("bundleID"), str):
        found.append(meta["bundleID"])
    data = _safely(lambda: report._data)
    info = data.get("bundleInfo") if isinstance(data, dict) else None
    if isinstance(info, dict) and isinstance(info.get("CFBundleIdentifier"), str):
        found.append(info["CFBundleIdentifier"])
    if isinstance(data, str):  # the old text body: `Identifier:          com.example.app`
        for line in data.splitlines():
            if line.startswith("Identifier:") and line[len("Identifier:"):].strip():
                found.append(line[len("Identifier:"):].strip())
                break
    return found


def names_app(head: bytes, bundle_id: str) -> bool:
    """Does the start of a report file (as much as it was read) name the app: its header line's `bundleID`, or an old
    report's `Identifier:` line? For a file that can't be parsed or wasn't read whole: only a file that says it is
    the app's is counted as the app's skipped report (another app's is not mentioned, not even by a count)."""
    text = head.decode("utf-8", errors="replace")
    first, _, _ = text.partition("\n")
    try:
        meta = json.loads(first)
    except ValueError:
        meta = None
    if isinstance(meta, dict) and isinstance(meta.get("bundleID"), str):
        return meta["bundleID"] == bundle_id
    for line in text.splitlines():
        if line.startswith("Identifier:") and line[len("Identifier:"):].strip():
            return line[len("Identifier:"):].strip() == bundle_id
    return False


def only_mine(reports: Iterable, executable: str, bundle_id: str | None = None) -> list:
    """The crash reports of this app: its process is named exactly `executable` (a name that can't be read is not
    this app's), the report is a crash and not a resource report, and the bundle id inside the report (its header, its
    `bundleInfo`, or an old report's `Identifier:` line) is affirmatively the app's and no id in it is another's.
    Two installed apps can share an executable name, so a report that names no bundle id proves nothing and is left
    out, as is every report when there is no bundle id to prove it with (AGENT-TOOLS §15.2: only the app's own)."""
    if bundle_id is None:
        return []
    mine = []
    for r in reports:
        ids = _bundle_ids_in(r)
        if (_safely(lambda: r.name) == executable and _safely(lambda: r.bug_type_str) in CRASH_BUG_TYPES
                and ids and all(b == bundle_id for b in ids)):
            mine.append(r)
    return mine

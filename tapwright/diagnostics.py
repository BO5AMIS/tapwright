"""Diagnostics (docs/AGENT-TOOLS.md §15): the app's own log, its crash reports, and the lines a failure carries.

Everything an agent reads is worded here, in constants, so the words are changed in one place. Log lines are the
app's own process's only, and are kept in memory (a ring per app); they are never written to a file. The phone side
(os_trace and the crash reports) is in device.py; the rules for what may be shown are in daemon.py."""
from __future__ import annotations

import dataclasses
import datetime
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

    def add(self, entry: LogEntry) -> None:
        if len(entry.text) > LOG_TEXT_KEPT:
            entry = dataclasses.replace(entry, text=entry.text[:LOG_TEXT_KEPT])
        self.entries.append(entry)

    def newest(self) -> tuple[int, frozenset[str]] | None:
        """(the newest entry's time, the texts of the entries at that time): where a stream that starts again for the
        same process must pick up, so the phone's replay of what it already gave isn't added twice."""
        if not self.entries:
            return None
        at = self.entries[-1].at
        return at, frozenset(e.text[:LOG_TEXT_KEPT] for e in self.entries if e.at == at)

    def add_unseen(self, entry: LogEntry, after: tuple[int, frozenset[str]] | None) -> bool:
        """Add `entry` unless `after` (from newest()) says it is already here: older than the newest entry, or the
        same time and text as one. True when it was added."""
        if after is not None and (entry.at < after[0] or (entry.at == after[0] and entry.text[:LOG_TEXT_KEPT] in after[1])):
            return False
        self.add(entry)
        return True

    def latest(self) -> int | None:
        return self.entries[-1].at if self.entries else None

    def select(self, since: int | None = None, level: str = "info", grep: str | None = None,
               limit: int = LOG_DEFAULT_LIMIT) -> list[LogEntry]:
        """The newest `limit` entries newer than `since`, at `level` or above, containing `grep`; oldest first."""
        floor = LOG_LEVELS.index(level)
        needle = grep.casefold() if grep else None
        found = [e for e in self.entries if (since is None or e.at > since) and LOG_LEVELS.index(e.level) >= floor
                 and (needle is None or needle in e.text.casefold())]
        return found[-limit:]


def cut(text: str, limit: int = LOG_TEXT_CUT) -> str:
    """Text cut at `limit` characters, with how many are missing (as element text is cut: `…(+57)`)."""
    flat = " ".join(text.split())
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
    latest = (ring.latest() if ring is not None else None) or since or 0
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


def _safely(fn: Callable[[], object], default=None):
    try:
        return fn()
    except Exception:  # a report this version of the parser can't read in part still says what it can
        return default


def _reason_of(report) -> str:
    sub = _safely(lambda: report.exception_subtype)
    if sub:
        return str(sub)
    info = _safely(lambda: report.application_specific_information)
    if isinstance(info, dict):
        info = " ".join(str(x) for v in info.values() for x in (v if isinstance(v, (list, tuple)) else [v]))
    elif isinstance(info, (list, tuple)):
        info = " ".join(str(x) for x in info)
    return " ".join(str(info).split())[:CRASH_REASON_CUT] if info else ""


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


def describe_crash(report, executable: str) -> CrashInfo:
    """A parsed crash report (pycrashreport's) as the few facts `crash` shows."""
    kind = str(_safely(lambda: report.exception_type) or "unknown exception")
    frames = list(_safely(lambda: report.frames, []) or [])
    shown: list[str] = []
    top_app = top_any = None
    for n, frame in enumerate(frames):
        image = posixpath.basename(getattr(frame, "image_name", None) or "?")
        mine = _is_app_frame(getattr(frame, "image_name", None), executable)
        text = f"{image} {_frame_text(frame)}"
        top_any = top_any or text
        top_app = top_app or (text if mine else None)
        if n < CRASH_FRAMES:
            shown.append(f"{n:>2} {CRASH_MARK if mine else ' '} {text}")
    where = top_app or top_any
    return CrashInfo(_when_of(report), kind, _reason_of(report), tuple(shown),
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
    return found


def only_mine(reports: Iterable, executable: str, bundle_id: str | None = None) -> list:
    """The crash reports of this app: its process is named exactly `executable` (a name that can't be read is not
    this app's), the report is a crash and not a resource report, and the bundle id inside it, when it has one, is the
    app's (two installed apps can share an executable name). Anything else is dropped."""
    return [r for r in reports
            if _safely(lambda: r.name) == executable
            and _safely(lambda: r.bug_type_str) in CRASH_BUG_TYPES
            and all(b == bundle_id for b in _bundle_ids_in(r) if bundle_id is not None)]

"""Automations (AGENT-TOOLS §22.1): the schedule file, the commands `schedule add|list|remove|unlock`, and the system's own
scheduler that starts `tapwright run-schedule NAME` (schedulerun.py is the run itself).

One file per schedule, `~/.tapwright/schedules/<name>.json`, in the format both tools share. Beside it:
`<name>.lock` (the run lock, §22.3, published whole by pidlock) and `<name>.seen` (what a file trigger's folder held at
its last check). Neither ends in `.json`, so neither is taken for a schedule.

On Windows a time schedule is a Task Scheduler task that wakes the PC (`WakeToRun`, which only an XML definition can
set). A file trigger is polled by a task that does not wake the PC: it runs while the PC is awake (1.14.1). So is
a notification trigger: its poll asks the running daemon for the digests of that app's notifications, never their text
(`new_notifications`), and `<name>.seen` keeps what its last check saw. The digests are HMACs under a random key kept
beside the schedules (`notification-trigger.key`), so one can't be tried against every one-time code. Other systems get the
launchd or cron entry to add, printed, since 1.14 doesn't say how a schedule starts there.
"""
from __future__ import annotations

import dataclasses
import datetime
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from xml.sax.saxutils import escape

from . import auditlog, pidlock, routes, store

VERSION = 1
TOOL = "tapwright"
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,59}$")
DEVICES = {"con", "prn", "aux", "nul", *(f"com{n}" for n in range(1, 10)), *(f"lpt{n}" for n in range(1, 10))}
TIME = re.compile(r"^(\d{2}):(\d{2}) (daily|weekdays)$")
VARIABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
POLL_MINUTES = 2  # a file trigger's folder, or a notification trigger's app, is looked at this often while the PC is awake
TASK_FOLDER = "Tapwright"
SETTLE_SECONDS = 2.0  # a file touched this recently may still be being written: it counts at the next check


class ScheduleError(ValueError):
    """A schedule file or a request that can't be used; the text says why."""


# ---- the file ---------------------------------------------------------------------------------------------------------
def folder() -> Path:
    return store.HOME / "schedules"


def file_of(name: str, where: Path | None = None) -> Path:
    return (where or folder()) / f"{name}.json"


def lock_of(name: str, where: Path | None = None) -> Path:
    return (where or folder()) / f"{name}.lock"


def seen_of(name: str, where: Path | None = None) -> Path:
    return (where or folder()) / f"{name}.seen"


def check_name(name: object) -> str:
    if not isinstance(name, str) or not NAME.match(name) or name.rstrip(".").lower() in DEVICES or name.endswith("."):
        raise ScheduleError(f"a schedule's name is letters, digits, '_', '-' and '.' (at most 60, starting with a letter "
                            f"or a digit), not {name!r}")
    return name


@dataclasses.dataclass(frozen=True)
class Schedule:
    name: str
    route: str
    values: dict[str, str]  # the file's `with`; a value `@file` is read when a run starts
    at: str | None
    trigger: dict[str, str] | None
    policy: str | None = None
    accounts: str | None = None
    maps: str | None = None
    file: str = ""  # where it was read from (not in the file)

    def to_json(self) -> dict:
        return {"version": VERSION, "tool": TOOL, "route": self.route, "with": dict(self.values), "at": self.at,
                "trigger": dict(self.trigger) if self.trigger else None, "policy": self.policy,
                "accounts": self.accounts, "maps": self.maps}

    def when(self) -> str:
        if self.at:
            return self.at
        assert self.trigger
        key, value = next(iter(self.trigger.items()))
        return f"when a file appears in {value}" if key == "file" else f"on a notification from {value}"


def parse_at(text: object, now: datetime.datetime | None = None) -> str:
    """`HH:MM daily`, `HH:MM weekdays` or an ISO date-time (one run, which is not in the past), as written."""
    if not isinstance(text, str):
        raise ScheduleError(f"at must be text like \"09:00 daily\", not {text!r}")
    text = text.strip()
    if m := TIME.match(text):
        if int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise ScheduleError(f"at: {m.group(1)}:{m.group(2)} is not a time of day (00:00 to 23:59)")
        return text
    try:
        moment = datetime.datetime.fromisoformat(text.replace("Z", "+00:00") if text.endswith("Z") else text)
    except ValueError:
        raise ScheduleError(f'at is "HH:MM daily", "HH:MM weekdays" or an ISO date-time like 2026-11-20T09:00, not '
                            f"{text!r}") from None
    if now is not None and local(moment) <= now:
        raise ScheduleError(f"at: {text} has passed; a single run needs a time still to come")
    return text


def local(moment: datetime.datetime) -> datetime.datetime:
    """A naive local date-time (an ISO time without an offset is local time already)."""
    return moment.astimezone().replace(tzinfo=None) if moment.tzinfo is not None else moment


def parse_trigger(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or len(value) != 1:
        raise ScheduleError('trigger is {"file": "<folder>"} or {"notification": "<app>"}, one of them')
    [(key, target)] = value.items()
    if key not in ("file", "notification"):
        raise ScheduleError(f'trigger is {{"file": "<folder>"}} or {{"notification": "<app>"}}, not "{key}"')
    if not isinstance(target, str) or not target.strip():
        raise ScheduleError(f"trigger's {key} must be a non-empty text, not {target!r}")
    return {key: target}


def parse(name: str, text: str, path: str = "", now: datetime.datetime | None = None) -> Schedule:
    """The schedule in `text` (ScheduleError otherwise)."""
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ScheduleError(f"it isn't JSON ({e})") from None
    if not isinstance(data, dict):
        raise ScheduleError("it must be a JSON object")
    if data.get("version") != VERSION or isinstance(data.get("version"), bool):
        raise ScheduleError(f"version must be {VERSION}, not {data.get('version')!r}")
    tool = data.get("tool")
    if tool != TOOL:
        raise ScheduleError(f"tool must be \"{TOOL}\"" + (f"; this schedule is for {tool}" if isinstance(tool, str) else ""))
    route = data.get("route")
    if not isinstance(route, str) or not routes.NAME.match(route):
        raise ScheduleError(f"route must be a saved route's name (lower-case letters, digits and _), not {route!r}")
    given = data.get("with")
    values: dict[str, str] = {}
    if given is not None:
        if not isinstance(given, dict):
            raise ScheduleError('with must be an object of values, like {"caption": "@captions.txt"}')
        for key, value in given.items():
            if not VARIABLE.match(str(key)):
                raise ScheduleError(f"with: {key!r} isn't a variable name")
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise ScheduleError(f"with's {key} must be a text or a number, not {value!r}")
            values[str(key)] = str(value)
    at, trigger = data.get("at"), data.get("trigger")
    if (at is None) == (trigger is None):
        raise ScheduleError("exactly one of at and trigger must be set")
    paths: dict[str, str | None] = {}
    for key in ("policy", "accounts", "maps"):
        value = data.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ScheduleError(f"{key} must be a file path or null, not {value!r}")
        paths[key] = value
    return Schedule(check_name(name), route, values, parse_at(at, now) if at is not None else None,
                    parse_trigger(trigger) if trigger is not None else None, paths["policy"], paths["accounts"],
                    paths["maps"], path)


def load(name: str, where: Path | None = None) -> Schedule:
    check_name(name)
    path = file_of(name, where)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        known = ", ".join(names(where)) or "none yet"
        raise ScheduleError(f'no schedule called "{name}" (schedules: {known})') from None
    except (OSError, UnicodeDecodeError) as e:
        raise ScheduleError(f"can't read {path}: {getattr(e, 'strerror', None) or e}") from None
    try:
        return parse(name, text, str(path))
    except ScheduleError as e:
        raise ScheduleError(f"the schedule file {path}: {e}") from None


def names(where: Path | None = None) -> list[str]:
    base = where or folder()
    return sorted(p.stem for p in base.glob("*.json")) if base.is_dir() else []


def write(s: Schedule, where: Path | None = None) -> Path:
    base = where or folder()
    base.mkdir(parents=True, exist_ok=True)
    path = file_of(s.name, base)
    tmp = base / f"{s.name}.json.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(s.to_json(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_values(s: Schedule) -> dict[str, str]:
    """`with`, with each `@file` read now (a relative path is relative to the schedule file's folder)."""
    out: dict[str, str] = {}
    base = Path(s.file).parent if s.file else folder()
    for key, value in s.values.items():
        if value.startswith("@") and len(value) > 1:
            path = Path(value[1:]).expanduser()
            path = path if path.is_absolute() else base / path
            try:
                out[key] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                raise ScheduleError(f"with's {key}: can't read {path} ({getattr(e, 'strerror', None) or e})") from None
        else:
            out[key] = value
    return out


# ---- a file trigger -----------------------------------------------------------------------------------------------------
def listing(directory: str) -> dict[str, int]:
    """The files in a trigger's folder, name to modification time (ns); folders aren't files."""
    try:
        return {e.name: e.stat().st_mtime_ns for e in os.scandir(directory) if e.is_file()}
    except OSError:
        return {}


def write_seen(name: str, held: dict[str, int], where: Path | None = None) -> None:
    base = where or folder()
    base.mkdir(parents=True, exist_ok=True)
    seen_of(name, base).write_text(json.dumps(held), encoding="utf-8")


def new_files(s: Schedule, where: Path | None = None, now_ns: int | None = None) -> list[str]:
    """The files that appeared in the trigger's folder since the last check; the check is remembered. The first check
    of a schedule with no record only notes what is there. A file touched in the last SETTLE_SECONDS waits for the next
    check (it may still be being written)."""
    import time
    assert s.trigger and "file" in s.trigger
    now_ns = time.time_ns() if now_ns is None else now_ns
    now_held = listing(s.trigger["file"])
    try:
        before = json.loads(seen_of(s.name, where).read_text(encoding="utf-8"))
        if not isinstance(before, dict):
            before = None
    except (OSError, ValueError):
        before = None
    if before is None:
        write_seen(s.name, now_held, where)
        return []
    settled = {n: t for n, t in now_held.items() if now_ns - t >= SETTLE_SECONDS * 1e9}
    found = sorted(n for n in settled if n not in before)
    write_seen(s.name, {**{n: t for n, t in before.items() if n in now_held}, **settled}, where)
    return found


def new_notifications(s: Schedule, keys: list[str], where: Path | None = None) -> bool:
    """Has a notification from the trigger's app appeared since the last check (§22.1)? `keys` are the digests of its
    notifications now (center_tool.notification_keys), newest first. The first check of a schedule with no record only
    notes what is there. Each new notification fires ONE run: when several are new, the oldest of them is marked seen and
    the rest wait for the next check (POLL_MINUTES later), so a burst never runs the route at once twice. A notification
    the person cleared drops out of the record; one with the same words as one before counts again (by how many)."""
    assert s.trigger and "notification" in s.trigger
    now: dict[str, int] = {}
    for k in keys:
        now[k] = now.get(k, 0) + 1
    try:
        before = json.loads(seen_of(s.name, where).read_text(encoding="utf-8"))
        if not isinstance(before, dict) or before.get("notification") is not True or not isinstance(before.get("seen"), dict):
            before = None
        elif before.get("keyed") != KEYED:  # (digests of an older version, unkeyed: noted again, never a run)
            before = None
    except (OSError, ValueError):
        before = None
    if before is None:
        _write_seen_keys(s.name, now, where)
        return False
    seen = {k: min(int(n), now[k]) for k, n in before["seen"].items() if k in now and isinstance(n, int)}
    fresh = [k for k in reversed(keys) if now[k] > seen.get(k, 0)]  # oldest first
    if fresh:
        seen[fresh[0]] = seen.get(fresh[0], 0) + 1
    _write_seen_keys(s.name, seen, where)
    return bool(fresh)


def _write_seen_keys(name: str, seen: dict[str, int], where: Path | None) -> None:
    base = where or folder()
    base.mkdir(parents=True, exist_ok=True)
    seen_of(name, base).write_text(json.dumps({"notification": True, "keyed": KEYED, "seen": seen}), encoding="utf-8")


# ---- the notification trigger's key (review 1.7, L5) ----
KEYED = 1  # the seen record's digests are HMACs under the trigger's key (an older record's aren't: it is noted again)
TRIGGER_KEY_FILE: Path | None = None  # None: <schedules folder>/notification-trigger.key (the tests set their own)


def trigger_key_path() -> Path:
    return TRIGGER_KEY_FILE if TRIGGER_KEY_FILE is not None else folder() / "notification-trigger.key"


def trigger_secret() -> bytes | None:
    """The notification trigger's random key, kept beside the schedules (made once, readable by this user only): its
    digests (Notification.key) are HMACs under it, so a digest can't be tried against every code. Made by publishing it
    whole (pidlock.publish_new), so two processes making it at once agree on one. None when it can't be read or made:
    the check then reads nothing."""
    path = trigger_key_path()
    for _ in range(2):
        try:
            text = path.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            text = None
        except (OSError, UnicodeDecodeError):
            return None
        if text is not None:
            # (a key file that isn't one is never replaced here: a person removes it, and the trigger notes anew)
            return bytes.fromhex(text) if re.fullmatch(r"[0-9a-f]{64}", text) else None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        key = secrets.token_hex(32)
        made = pidlock.publish_new(pidlock.Deps(), str(path), key + "\n")
        if made == "published":
            return bytes.fromhex(key)
        if made == "error":
            return None
    return None


# ---- the system's scheduler ---------------------------------------------------------------------------------------------
class SchedulerError(RuntimeError):
    pass


Runner = Callable[[list[str]], "tuple[int, str]"]


def system_runner(argv: list[str]) -> tuple[int, str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return done.returncode, ((done.stdout or "") + (done.stderr or "")).strip()


def default_command() -> list[str]:
    """What starts tapwright from the scheduler: this Python, without a console window on Windows (pythonw)."""
    python = sys.executable
    if sys.platform == "win32":
        quiet = Path(python).with_name("pythonw.exe")
        if quiet.exists():
            python = str(quiet)
    return [python, "-m", "tapwright"]


class Scheduler:
    """Starts a schedule's runs. `runner` runs `schtasks` (tests inject one); `platform` is `sys.platform`'s."""

    def __init__(self, runner: Runner | None = None, platform: str | None = None,
                 command: list[str] | None = None, now: Callable[[], datetime.datetime] | None = None) -> None:
        self.runner = runner or system_runner
        self.platform = platform or sys.platform
        self.command = command or default_command()
        self.now = now or datetime.datetime.now

    @property
    def windows(self) -> bool:
        return self.platform == "win32"

    @staticmethod
    def task_name(name: str) -> str:
        return f"{TASK_FOLDER}\\{name}"

    # -- the task's definition (Windows)
    def xml(self, s: Schedule) -> str:
        """The Task Scheduler definition. `WakeToRun` only for a time schedule: a file or notification trigger doesn't wake
        the PC."""
        wake = s.at is not None
        start = self.now().replace(microsecond=0)
        if s.at is not None:
            if m := TIME.match(s.at):
                first = start.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0)
                if m.group(3) == "daily":
                    trigger = (f"<CalendarTrigger><StartBoundary>{first.isoformat()}</StartBoundary><Enabled>true</Enabled>"
                               "<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger>")
                else:
                    days = "".join(f"<{d}/>" for d in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday"))
                    trigger = (f"<CalendarTrigger><StartBoundary>{first.isoformat()}</StartBoundary><Enabled>true</Enabled>"
                               f"<ScheduleByWeek><DaysOfWeek>{days}</DaysOfWeek><WeeksInterval>1</WeeksInterval>"
                               "</ScheduleByWeek></CalendarTrigger>")
            else:
                when = local(datetime.datetime.fromisoformat(s.at.replace("Z", "+00:00") if s.at.endswith("Z") else s.at))
                trigger = (f"<TimeTrigger><StartBoundary>{when.replace(microsecond=0).isoformat()}</StartBoundary>"
                           "<Enabled>true</Enabled></TimeTrigger>")
            arguments = f"run-schedule {s.name}"
        else:
            trigger = (f"<TimeTrigger><StartBoundary>{start.isoformat()}</StartBoundary><Enabled>true</Enabled>"
                       f"<Repetition><Interval>PT{POLL_MINUTES}M</Interval></Repetition></TimeTrigger>")
            arguments = f"run-schedule {s.name} --if-triggered"
        exe, *lead = self.command
        description = f"Tapwright schedule {s.name}: route {s.route}, {s.when()}"
        return (
            '<?xml version="1.0" encoding="UTF-16"?>\n'
            '<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
            f"<RegistrationInfo><Description>{escape(description)}</Description></RegistrationInfo>"
            f"<Triggers>{trigger}</Triggers>"
            '<Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel>'
            "</Principal></Principals>"
            "<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>"
            "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>"
            "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><AllowHardTerminate>true</AllowHardTerminate>"
            "<StartWhenAvailable>false</StartWhenAvailable><RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>"
            "<AllowStartOnDemand>true</AllowStartOnDemand><Enabled>true</Enabled><Hidden>false</Hidden>"
            f"<WakeToRun>{'true' if wake else 'false'}</WakeToRun><ExecutionTimeLimit>PT2H</ExecutionTimeLimit>"
            "<Priority>7</Priority></Settings>"
            f'<Actions Context="Author"><Exec><Command>{escape(exe)}</Command>'
            f"<Arguments>{escape(' '.join([*lead, arguments]))}</Arguments></Exec></Actions></Task>")

    # -- install and remove
    def install(self, s: Schedule) -> str:
        """Start the schedule's runs: a task on Windows, what to add elsewhere. Returns the words for the person."""
        if not self.windows:
            return self.instructions(s)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"{s.name}.xml"
            path.write_text(self.xml(s), encoding="utf-16")
            code, out = self.runner(["schtasks", "/Create", "/TN", self.task_name(s.name), "/XML", str(path), "/F"])
        if code != 0:
            raise SchedulerError(f"Task Scheduler refused the task ({out or f'schtasks exit {code}'})")
        if s.at is not None:
            return f'Task Scheduler task "{self.task_name(s.name)}" created: it wakes the PC and runs `tapwright run-schedule {s.name}`.'
        what = "the folder" if "file" in (s.trigger or {}) else f"{(s.trigger or {}).get('notification')}'s notifications"
        return (f'Task Scheduler task "{self.task_name(s.name)}" created: it looks at {what} every {POLL_MINUTES} minutes '
                "while the PC is awake (it doesn't wake the PC), and never while the phone is in use.")

    def remove(self, name: str) -> str | None:
        """Remove the task. None when there was none (or no task system); a SchedulerError when it can't be removed."""
        if not self.windows:
            return None
        code, out = self.runner(["schtasks", "/Delete", "/TN", self.task_name(name), "/F"])
        if code == 0:
            return f'Removed the Task Scheduler task "{self.task_name(name)}".'
        if self.exists(name) is False:
            return None
        raise SchedulerError(f"Task Scheduler couldn't remove \"{self.task_name(name)}\" ({out or f'schtasks exit {code}'})")

    def exists(self, name: str) -> bool | None:
        """Whether the task is there (None: this system has no Task Scheduler to ask)."""
        if not self.windows:
            return None
        code, _ = self.runner(["schtasks", "/Query", "/TN", self.task_name(name)])
        return code == 0

    # -- other systems: what to add
    def instructions(self, s: Schedule) -> str:
        exe = shutil.which("tapwright") or " ".join(self.command)
        run = f"{exe} run-schedule {s.name}"
        if s.trigger is not None:
            check = f"{run} --if-triggered"
            if self.platform == "darwin":
                return plist(s, exe, ["run-schedule", s.name, "--if-triggered"], None)
            what = "the folder" if "file" in s.trigger else "the app's notifications"
            return f"Add this line to your crontab (crontab -e); it looks at {what} every {POLL_MINUTES} minutes:\n" \
                   f"*/{POLL_MINUTES} * * * * {check}"
        assert s.at is not None
        if self.platform == "darwin":
            return plist(s, exe, ["run-schedule", s.name], s.at)
        if m := TIME.match(s.at):
            days = "*" if m.group(3) == "daily" else "1-5"
            return f"Add this line to your crontab (crontab -e):\n{int(m.group(2))} {int(m.group(1))} * * {days} {run}"
        when = local(datetime.datetime.fromisoformat(s.at.replace("Z", "+00:00") if s.at.endswith("Z") else s.at))
        return (f"Add this line to your crontab (crontab -e); cron has no year, so remove the line after the run:\n"
                f"{when.minute} {when.hour} {when.day} {when.month} * {run}")


def plist(s: Schedule, exe: str, args: list[str], at: str | None) -> str:
    """A launchd job to save as ~/Library/LaunchAgents/com.tapwright.schedule.<name>.plist, then `launchctl load` it."""
    label = f"com.tapwright.schedule.{s.name}"
    words = "".join(f"<string>{escape(a)}</string>" for a in [exe, *args])
    if s.trigger is not None:
        when = f"<key>StartInterval</key><integer>{POLL_MINUTES * 60}</integer>"
    else:
        assert at is not None
        if m := TIME.match(at):
            hour, minute = int(m.group(1)), int(m.group(2))
            weekdays = range(1, 6) if m.group(3) == "weekdays" else [None]
            entries = "".join("<dict>" + (f"<key>Weekday</key><integer>{d}</integer>" if d else "") +
                              f"<key>Hour</key><integer>{hour}</integer><key>Minute</key><integer>{minute}</integer></dict>"
                              for d in weekdays)
            when = f"<key>StartCalendarInterval</key><array>{entries}</array>"
        else:
            moment = local(datetime.datetime.fromisoformat(at.replace("Z", "+00:00") if at.endswith("Z") else at))
            when = (f"<key>StartCalendarInterval</key><dict><key>Month</key><integer>{moment.month}</integer>"
                    f"<key>Day</key><integer>{moment.day}</integer><key>Hour</key><integer>{moment.hour}</integer>"
                    f"<key>Minute</key><integer>{moment.minute}</integer></dict>")
    return (f"Save this as ~/Library/LaunchAgents/{label}.plist, then run: launchctl load ~/Library/LaunchAgents/{label}.plist\n"
            '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>'
            f"<key>Label</key><string>{label}</string><key>ProgramArguments</key><array>{words}</array>{when}</dict></plist>")


# ---- the commands -------------------------------------------------------------------------------------------------------
def absolute(path: str | None) -> str | None:
    return str(Path(path).expanduser().resolve()) if path else None


def parse_with(items: list[str] | None) -> dict[str, str]:
    """`--with k=v` and `--with k=@file`. A relative `@file` that exists here is saved as its absolute path (the file
    would otherwise be looked for beside the schedule file)."""
    out: dict[str, str] = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep or not VARIABLE.match(key):
            raise ScheduleError(f"--with takes name=value or name=@file, not {item!r}")
        if key in out:
            raise ScheduleError(f"--with {key} is given twice")
        if value.startswith("@") and len(value) > 1:
            target = Path(value[1:]).expanduser()
            if not target.is_file():
                raise ScheduleError(f"--with {key}: the file {value[1:]} doesn't exist")
            value = "@" + str(target.resolve())
        out[key] = value
    return out


def add(route: str, *, at: str | None = None, on_file: str | None = None, on_notification: str | None = None,
        with_: list[str] | None = None, name: str | None = None, policy: str | None = None, accounts: str | None = None,
        maps: str | None = None, scheduler: Scheduler | None = None, where: Path | None = None,
        now: datetime.datetime | None = None) -> tuple[str, int]:
    """`tapwright schedule add ROUTE --at … | --on-file DIR | --on-notification APP`: the file, then the system's task."""
    scheduler = scheduler or Scheduler()
    try:
        if sum(x is not None for x in (at, on_file, on_notification)) != 1:
            raise ScheduleError("give exactly one of --at, --on-file (--trigger-file) and --on-notification")
        if not routes.NAME.match(route or ""):
            raise ScheduleError(f"{route!r} isn't a route's name (lower-case letters, digits and _)")
        name = check_name(name or route)
        if (other := next((n for n in names(where) if n.lower() == name.lower()), None)) is not None:
            raise ScheduleError(f'a schedule called "{other}" exists already; remove it first (tapwright schedule remove {other})')
        paths: dict[str, str | None] = {}
        for key, value in (("policy", policy), ("accounts", accounts), ("maps", maps)):
            paths[key] = absolute(value)
            if value and not Path(paths[key] or "").exists():
                raise ScheduleError(f"--{key} {value}: no such {'folder' if key == 'maps' else 'file'}")
        trigger = ({"file": absolute(on_file) or on_file} if on_file is not None
                   else {"notification": on_notification} if on_notification is not None else None)
        s = Schedule(name, route, parse_with(with_), parse_at(at, now or datetime.datetime.now()) if at is not None else None,
                     parse_trigger(trigger) if trigger else None, paths["policy"], paths["accounts"], paths["maps"])
    except ScheduleError as e:
        return f"Error: {e}", 1
    notes: list[str] = []
    if s.trigger and "file" in s.trigger and not Path(s.trigger["file"]).is_dir():
        notes.append(f"Note: the folder {s.trigger['file']} doesn't exist yet; the trigger fires when files appear in it.")
    path = write(s, where)
    s = dataclasses.replace(s, file=str(path))
    if s.trigger and "file" in s.trigger:
        write_seen(s.name, listing(s.trigger["file"]), where)  # what is there now isn't new
    try:
        said = scheduler.install(s)
    except SchedulerError as e:
        for p in (path, seen_of(s.name, where)):
            p.unlink(missing_ok=True)
        return f"Error: the schedule \"{s.name}\" was not added: {e}", 1
    return "\n".join([f'Added the schedule "{s.name}" ({s.route}, {s.when()}): {path}', said, *notes]), 0


def listing_text(scheduler: Scheduler | None = None, where: Path | None = None, runs: Path | None = None) -> tuple[str, int]:
    scheduler = scheduler or Scheduler()
    found = names(where)
    if not found:
        return "No schedules. Add one with: tapwright schedule add ROUTE --at \"09:00 daily\"", 0
    lines: list[str] = []
    for n in found:
        try:
            s = load(n, where)
        except ScheduleError as e:
            lines.append(f"{n}: can't be used ({e})")
            continue
        task = scheduler.exists(n)
        shown = "" if task is None else "; task installed" if task else "; NO TASK: add it again"
        last = last_run(n, runs)
        lines.append(f"{n}: route {s.route}, {s.when()}{shown}" + (f"; last: {last}" if last else ""))
    return "\n".join(lines), 0


def last_run(name: str, runs: Path | None = None) -> str | None:
    base = runs or store.HOME / "runs"
    reports = sorted(base.glob(f"{name}-????????T??????Z.json")) if base.is_dir() else []
    if not reports:
        return None
    try:
        data = json.loads(reports[-1].read_text(encoding="utf-8"))
        return f"{data.get('result', data.get('status'))} ({reports[-1].stem.removeprefix(name + '-')})"
    except (OSError, ValueError, AttributeError):
        return None


def remove(name: str, scheduler: Scheduler | None = None, where: Path | None = None) -> tuple[str, int]:
    scheduler = scheduler or Scheduler()
    try:
        check_name(name)
    except ScheduleError as e:
        return f"Error: {e}", 1
    path = file_of(name, where)
    if not path.exists():
        return f'Error: no schedule called "{name}" (schedules: {", ".join(names(where)) or "none yet"})', 1
    try:
        said = scheduler.remove(name)
    except SchedulerError as e:
        return f"Error: {e}; the schedule file was kept", 1
    path.unlink(missing_ok=True)
    seen_of(name, where).unlink(missing_ok=True)
    return "\n".join([f'Removed the schedule "{name}".', *([said] if said else [])]), 0


def unlock(name: str, where: Path | None = None, audit_folder: Path | None = None,
           deps: pidlock.Deps | None = None) -> tuple[str, int]:
    """`tapwright schedule unlock NAME`: remove a stuck run lock (empty, unreadable, or held by a process that is gone).
    Refused while a live holder is proven (1.14.2)."""
    try:
        check_name(name)
    except ScheduleError as e:
        return f"Error: {e}", 1
    result = pidlock.unlock(lock_of(name, where), deps)
    if result.status == "none":
        return f'The schedule "{name}" has no lock; nothing to remove.', 0
    if result.status == "live":
        return (f'The run lock of "{name}" is held by a live process (pid {result.pid}); nothing was removed. Wait for the '
                "run to finish, or stop that process first."), 1
    done = [f"Removed {p} ({why})." for p, why in result.removed]
    if result.removed:
        auditlog.AuditLog(audit_folder).write(
            "approval", name=f"schedule {name} lock", detail=f"unlocked ({'; '.join(why for _, why in result.removed)})")
    if result.status == "failed":
        return "\n".join([*done, f"Couldn't remove {result.failed or 'the lock'}: it changed or can't be deleted. "
                                 "Look at it and try again."]), 1
    return "\n".join(done), 0


def stuck_locks(where: Path | None = None, deps: pidlock.Deps | None = None) -> list[tuple[str, str, str]]:
    """For `doctor`: (schedule, lock path, why) for each run lock that is stuck (never a live one)."""
    found = []
    for n in names(where):
        base = str(lock_of(n, where))
        for path in (base, f"{base}.takeover"):
            kind, pid = pidlock.state(path, deps)
            if kind in ("dead", "empty", "unreadable"):
                found.append((n, path, pidlock.stuck_words(kind, pid)))
    return found

"""`tapwright service install|uninstall|status` (1.9.2, plug and forget): Tapwright always on.

iOS asks for the passcode ("Enter iPhone Passcode for XCTest") only when a new test runner starts, and only once its last
consent is about 8 hours old; a runner kept running is never asked (serve.log 11-13 to 11-19). Every start of the daemon
starts a runner. So the daemon should start once, when the person logs on, and keep running: on Windows a Task Scheduler
task with a logon trigger runs `tapwright serve --keep-alive` with the serve flags given here, without a console window
(pythonw), at normal priority and with no time limit. Other systems get the launchd agent or systemd user unit to add.

The task is the person's own (interactive, least privilege), like a schedule's task (schedules.py). `--accounts` is
written into the task as an absolute path: the vault's passcode is read from that file by the daemon, never put into the
task. The flags are also saved in ~/.tapwright/service.json (SERVICE_FILE): a daemon that an agent's call starts later
(after `serve --stop`, or before the logon task ran) is started with them too (client.start_daemon), and `service status`
says when the running daemon was started without them (review R191 M4).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from xml.sax.saxutils import escape

from . import store
from .schedules import TASK_FOLDER, Runner, default_command, system_runner

TASK_NAME = f"{TASK_FOLDER}\\serve"
LABEL = "com.tapwright.serve"  # the launchd agent's label, and the systemd unit's name
START_WAIT = 10.0  # seconds `install` waits for the task's daemon to answer after starting it
START_POLL = 0.5

# serve's flags a service may carry: (flag, takes a value, a file or folder whose path is made absolute)
SERVE_FLAGS = (("--apps", True, False), ("--dedicated", False, False), ("--phone", False, False),
               ("--accounts", True, True), ("--maps", True, True), ("--policy", True, True), ("--port", True, False),
               ("--address", True, False), ("--no-log", False, False), ("--brief", False, False),
               ("--no-hints", False, False), ("--no-dialog", False, False), ("--video", False, False),
               ("--settle-on-video", False, False))


class ServiceError(ValueError):
    """Flags `serve` would refuse at every logon (silently, under pythonw): refused at install instead."""


def service_file() -> Path:
    return store.HOME / "service.json"


def serve_args(values: dict[str, object]) -> list[str]:
    """`serve --keep-alive` and the flags given (by their dest name: apps, accounts, no_log...), in a fixed order. A path
    is made absolute: the task starts in another folder. Raises ServiceError for what serve would refuse: --apps,
    --dedicated and --phone together, --settle-on-video without --video, a vault or policy file that isn't there, and a
    `%` in a value (Task Scheduler expands %NAME% in a task's arguments)."""
    chosen = [f for f in ("apps", "dedicated", "phone") if values.get(f)]
    if len(chosen) > 1:
        raise ServiceError(f"--{chosen[0]} and --{chosen[1]} can't be used together (serve takes one of --apps, "
                           "--dedicated, --phone)")
    if values.get("settle_on_video") and not values.get("video"):
        raise ServiceError("--settle-on-video needs --video")
    args = ["serve", "--keep-alive"]
    for flag, takes, path in SERVE_FLAGS:
        value = values.get(flag[2:].replace("-", "_"))
        if value is None or value is False:
            continue
        if not takes:
            args.append(flag)
            continue
        text = str(value)
        if path:
            resolved = Path(text).expanduser().resolve()
            if flag in ("--accounts", "--policy") and not resolved.is_file():
                raise ServiceError(f"{flag} {text}: no such file")
            text = str(resolved)
        if "%" in text:
            raise ServiceError(f"{flag} {text}: a value with % can't go into a logon task (Task Scheduler would read "
                               "%NAME% as a variable)")
        args += [flag, text]
    return args


def saved_args() -> list[str] | None:
    """The serve flags `service install` saved (["serve", "--keep-alive", ...]); None: no service installed, or a file
    that can't be read."""
    try:
        data = json.loads(service_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    args = data.get("args") if isinstance(data, dict) else None
    if not isinstance(args, list) or not args or args[0] != "serve" or not all(isinstance(a, str) for a in args):
        return None
    return args


def _save(args: list[str]) -> None:
    path = service_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"args": args}), encoding="utf-8")
    os.replace(tmp, path)


def _forget() -> None:
    try:
        service_file().unlink()
    except OSError:
        pass


def flags_of(args: list[str]) -> dict:
    """What `status` compares between the service and the running daemon: --keep-alive and the vault file."""
    accounts = args[args.index("--accounts") + 1] if "--accounts" in args[:-1] else None
    return {"keep_alive": "--keep-alive" in args, "accounts": accounts}


class Service:
    """The always-on daemon's start at logon. `runner` runs `schtasks` (tests inject one); `platform`: sys.platform's.
    `running()` gives the running daemon's serve info (store.read_serve_info) when it answers, else None."""

    def __init__(self, runner: Runner | None = None, platform: str | None = None, command: list[str] | None = None,
                 running: Callable[[], dict | None] | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        self.runner = runner or system_runner
        self.platform = platform or sys.platform
        self.command = command or default_command()
        self.running = running or _running_daemon
        self.sleep, self.clock = sleep, clock

    @property
    def windows(self) -> bool:
        return self.platform == "win32"

    def xml(self, args: list[str]) -> str:
        """The Task Scheduler definition: at this person's logon, hidden, normal priority (5; 7 would be below normal,
        and the daemon's tunnel is a TCP stack on its own event loop: review R191 M5), no time limit (the default 72 h
        would stop the daemon), restarted a few times if it fails, never a second copy (serve itself allows one)."""
        exe, *lead = self.command
        user = os.environ.get("USERDOMAIN", "") + ("\\" if os.environ.get("USERDOMAIN") else "") + \
            os.environ.get("USERNAME", "")
        who = f"<UserId>{escape(user)}</UserId>" if user.strip("\\") else ""
        return (
            '<?xml version="1.0" encoding="UTF-16"?>\n'
            '<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
            "<RegistrationInfo><Description>Tapwright: the daemon that owns the iPhone, started at logon and kept "
            "running, so its test runner (and iOS's consent to UI automation) is kept</Description></RegistrationInfo>"
            f"<Triggers><LogonTrigger><Enabled>true</Enabled>{who}</LogonTrigger></Triggers>"
            '<Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel>'
            "</Principal></Principals>"
            "<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>"
            "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>"
            "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><AllowHardTerminate>true</AllowHardTerminate>"
            "<StartWhenAvailable>true</StartWhenAvailable><RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>"
            "<IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>"
            "<AllowStartOnDemand>true</AllowStartOnDemand><Enabled>true</Enabled><Hidden>true</Hidden>"
            "<RunOnlyIfIdle>false</RunOnlyIfIdle><WakeToRun>false</WakeToRun>"
            "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit><Priority>5</Priority>"
            "<RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure></Settings>"
            f'<Actions Context="Author"><Exec><Command>{escape(exe)}</Command>'
            f"<Arguments>{escape(subprocess.list2cmdline([*lead, *args]))}</Arguments></Exec></Actions></Task>")

    def install(self, args: list[str]) -> tuple[str, int]:
        if not self.windows:
            _save(args)
            return self.instructions(args), 0
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "serve.xml"
            path.write_text(self.xml(args), encoding="utf-16")
            code, out = self.runner(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(path), "/F"])
        if code != 0:
            return f"Error: Task Scheduler refused the task ({out or f'schtasks exit {code}'})", 1
        _save(args)
        said = [f'Task Scheduler task "{TASK_NAME}" created: `tapwright {subprocess.list2cmdline(args)}` starts at each '
                "logon, without a window, and keeps running. A daemon an agent starts uses the same flags."]
        if self.running() is not None:
            said.append("A daemon is running now; it keeps its flags until it stops (`tapwright serve --stop`; the next "
                        "start, by the task or by an agent, uses the service's).")
        else:
            code, out = self.runner(["schtasks", "/Run", "/TN", TASK_NAME])
            if code != 0:
                said.append(f"It starts at the next logon (starting it now didn't work: {out or f'schtasks exit {code}'}).")
            elif self._came_up():
                said.append("Started it now: the daemon answers.")
            else:
                said.append(f"Asked Task Scheduler to start it, but no daemon answered within {START_WAIT:.0f} s: see "
                            "~/.tapwright/serve.log.")
        said.append("After the phone restarts, unlock it once and enter the passcode once more when it asks to allow UI "
                    "automation. A daemon restart (serve --stop, an upgrade) more than about 8 hours after the last "
                    "passcode entry asks once too, and a phone that locks needs the vault (--accounts) to be unlocked.")
        return "\n".join(said), 0

    def _came_up(self) -> bool:
        deadline = self.clock() + START_WAIT
        while True:
            if self.running() is not None:
                return True
            if self.clock() >= deadline:
                return False
            self.sleep(START_POLL)

    def uninstall(self) -> tuple[str, int]:
        if not self.windows:
            _forget()
            return self.removal(), 0
        code, out = self.runner(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
        if code == 0:
            _forget()
            self._drop_empty_folder()
            return (f'Removed the Task Scheduler task "{TASK_NAME}". A daemon that is running keeps running until '
                    "`tapwright serve --stop`."), 0
        if self.exists() is False:
            _forget()
            return "There was no Tapwright service task.", 0
        return f"Error: Task Scheduler couldn't remove \"{TASK_NAME}\" ({out or f'schtasks exit {code}'})", 1

    def _drop_empty_folder(self) -> None:
        """schtasks /Delete leaves the \\Tapwright folder; remove it when no task (a schedule's) is left in it (review
        R191 L7). Best effort: a folder left behind is harmless."""
        script = ("$s = New-Object -ComObject Schedule.Service; $s.Connect(); $root = $s.GetFolder('\\'); "
                  f"$f = $root.GetFolder('\\{TASK_FOLDER}'); "
                  f"if ($f.GetTasks(1).Count -eq 0 -and $f.GetFolders(0).Count -eq 0) {{ $root.DeleteFolder('{TASK_FOLDER}', 0) }}")
        try:
            self.runner(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
        except Exception:
            pass

    def exists(self) -> bool | None:
        if not self.windows:
            return None
        code, _ = self.runner(["schtasks", "/Query", "/TN", TASK_NAME])
        return code == 0

    def status(self) -> tuple[str, int]:
        task = self.exists()
        info = self.running()
        saved = saved_args()
        if task is None:
            where = ("this system has no Task Scheduler: the launchd or systemd entry from `tapwright service install` "
                     "starts it" + ("" if saved else " (none saved)"))
        else:
            where = f'the logon task "{TASK_NAME}" is {"installed" if task else "not installed"}'
        lines = [f"Service: {where}; the daemon is {'running' if info is not None else 'not running'}."]
        if info is not None and saved is not None:
            want, have = flags_of(saved), {"keep_alive": info.get("keep_alive"), "accounts": info.get("accounts")}
            if want != have:
                lines.append("The running daemon wasn't started with the service's flags (" + _differs(want, have) +
                             "): stop it with `tapwright serve --stop`; the next start (the task, or an agent's call) "
                             "uses them.")
        return "\n".join(lines), 0

    # -- other systems: what to add
    def instructions(self, args: list[str]) -> str:
        command = [*self.command, *args]  # this interpreter, not whatever `tapwright` comes first on PATH (R191 L6)
        if self.platform == "darwin":
            words = "".join(f"<string>{escape(a)}</string>" for a in command)
            # KeepAlive only on a failure: serve exits 0 when a daemon is already running, and a plain KeepAlive would
            # start it again every 10 s forever (R191 L6)
            return (f"Save this as ~/Library/LaunchAgents/{LABEL}.plist, then run: launchctl load "
                    f"~/Library/LaunchAgents/{LABEL}.plist\n"
                    '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>'
                    f"<key>Label</key><string>{LABEL}</string><key>ProgramArguments</key><array>{words}</array>"
                    "<key>RunAtLoad</key><true/><key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>"
                    "</dict></plist>")
        line = " ".join(_quote(a) for a in command)
        return (f"Save this as ~/.config/systemd/user/{LABEL}.service, then run: systemctl --user enable --now "
                f"{LABEL}.service\n[Unit]\nDescription=Tapwright daemon\n\n[Service]\nExecStart={line}\nRestart=on-failure\n\n"
                "[Install]\nWantedBy=default.target")

    def removal(self) -> str:
        if self.platform == "darwin":
            return f"Run: launchctl unload ~/Library/LaunchAgents/{LABEL}.plist, then delete that file."
        return f"Run: systemctl --user disable --now {LABEL}.service, then delete ~/.config/systemd/user/{LABEL}.service."


def _differs(want: dict, have: dict) -> str:
    said = []
    if want["keep_alive"] != have["keep_alive"]:
        said.append("--keep-alive " + ("missing" if want["keep_alive"] else "extra"))
    if want["accounts"] != have["accounts"]:
        said.append("--accounts " + ("missing" if have["accounts"] is None else "another file"))
    return ", ".join(said) or "different flags"


def _quote(word: str) -> str:
    """A word of a systemd ExecStart line: quoted with double quotes when it holds anything but plain characters."""
    if word and all(c.isalnum() or c in "-_./:=,@" for c in word):
        return word
    return '"' + word.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _running_daemon() -> dict | None:
    from .client import healthy
    info = store.read_serve_info()
    return info if healthy(info) is not None else None

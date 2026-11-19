"""`tapwright service install|uninstall|status` (1.9.1, plug and forget): Tapwright always on.

iOS asks for the passcode ("Enter iPhone Passcode for XCTest") only when a new test runner starts, and only once its last
consent is about 8 hours old; a runner kept running is never asked (serve.log 11-13 to 11-19). Every start of the daemon
starts a runner. So the daemon should start once, when the person logs on, and keep running: on Windows a Task Scheduler
task with a logon trigger runs `tapwright serve --keep-alive` with the serve flags given here, without a console window
(pythonw) and with no time limit. Other systems get the launchd agent or systemd user unit to add.

The task is the person's own (interactive, least privilege), like a schedule's task (schedules.py). `--accounts` is
written into the task as an absolute path: the vault's passcode is read from that file by the daemon, never put into the
task.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from xml.sax.saxutils import escape

from . import store
from .schedules import TASK_FOLDER, Runner, default_command, system_runner

TASK_NAME = f"{TASK_FOLDER}\\serve"
LABEL = "com.tapwright.serve"  # the launchd agent's label, and the systemd unit's name

# serve's flags a service may carry: (flag, takes a value, a file or folder whose path is made absolute)
SERVE_FLAGS = (("--apps", True, False), ("--dedicated", False, False), ("--phone", False, False),
               ("--accounts", True, True), ("--maps", True, True), ("--policy", True, True), ("--port", True, False),
               ("--address", True, False), ("--no-log", False, False), ("--brief", False, False),
               ("--no-hints", False, False))


def serve_args(values: dict[str, object]) -> list[str]:
    """`serve --keep-alive` and the flags given (by their dest name: apps, accounts, no_log...), in a fixed order. A path
    is made absolute: the task starts in another folder."""
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
            text = str(Path(text).expanduser().resolve())
        args += [flag, text]
    return args


class Service:
    """The always-on daemon's start at logon. `runner` runs `schtasks` (tests inject one); `platform`: sys.platform's."""

    def __init__(self, runner: Runner | None = None, platform: str | None = None, command: list[str] | None = None,
                 healthy: Callable[[], bool] | None = None) -> None:
        self.runner = runner or system_runner
        self.platform = platform or sys.platform
        self.command = command or default_command()
        self.healthy = healthy or _daemon_answers

    @property
    def windows(self) -> bool:
        return self.platform == "win32"

    def xml(self, args: list[str]) -> str:
        """The Task Scheduler definition: at this person's logon, hidden, no time limit (the default 72 h would stop the
        daemon), restarted a few times if it fails, never a second copy (serve itself also allows only one)."""
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
            "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit><Priority>7</Priority>"
            "<RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure></Settings>"
            f'<Actions Context="Author"><Exec><Command>{escape(exe)}</Command>'
            f"<Arguments>{escape(subprocess.list2cmdline([*lead, *args]))}</Arguments></Exec></Actions></Task>")

    def install(self, args: list[str]) -> tuple[str, int]:
        if not self.windows:
            return self.instructions(args), 0
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "serve.xml"
            path.write_text(self.xml(args), encoding="utf-16")
            code, out = self.runner(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(path), "/F"])
        if code != 0:
            return f"Error: Task Scheduler refused the task ({out or f'schtasks exit {code}'})", 1
        said = [f'Task Scheduler task "{TASK_NAME}" created: `tapwright {subprocess.list2cmdline(args)}` starts at each '
                "logon, without a window, and keeps running."]
        if self.healthy():
            said.append("A daemon is running now; it keeps its flags until it stops (tapwright serve --stop, then "
                        f'schtasks /Run /TN "{TASK_NAME}" starts the task\'s).')
        else:
            code, out = self.runner(["schtasks", "/Run", "/TN", TASK_NAME])
            said.append("Started it now." if code == 0 else
                        f"It starts at the next logon (starting it now didn't work: {out or f'schtasks exit {code}'}).")
        said.append("After the phone restarts, unlock it once and enter the passcode once more when it asks to allow UI "
                    "automation; otherwise it needs no one while it stays plugged in.")
        return "\n".join(said), 0

    def uninstall(self) -> tuple[str, int]:
        if not self.windows:
            return self.removal(), 0
        code, out = self.runner(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
        if code == 0:
            return (f'Removed the Task Scheduler task "{TASK_NAME}". A daemon that is running keeps running until '
                    "`tapwright serve --stop`."), 0
        if self.exists() is False:
            return "There was no Tapwright service task.", 0
        return f"Error: Task Scheduler couldn't remove \"{TASK_NAME}\" ({out or f'schtasks exit {code}'})", 1

    def exists(self) -> bool | None:
        if not self.windows:
            return None
        code, _ = self.runner(["schtasks", "/Query", "/TN", TASK_NAME])
        return code == 0

    def status(self) -> tuple[str, int]:
        task = self.exists()
        running = self.healthy()
        if task is None:
            where = "this system has no Task Scheduler: see `tapwright service install` for the launchd or systemd entry"
        else:
            where = f'the logon task "{TASK_NAME}" is {"installed" if task else "not installed"}'
        return f"Service: {where}; the daemon is {'running' if running else 'not running'}.", 0

    # -- other systems: what to add
    def instructions(self, args: list[str]) -> str:
        found = shutil.which("tapwright")
        command = [found, *args] if found else [*self.command, *args]
        if self.platform == "darwin":
            words = "".join(f"<string>{escape(a)}</string>" for a in command)
            return (f"Save this as ~/Library/LaunchAgents/{LABEL}.plist, then run: launchctl load "
                    f"~/Library/LaunchAgents/{LABEL}.plist\n"
                    '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>'
                    f"<key>Label</key><string>{LABEL}</string><key>ProgramArguments</key><array>{words}</array>"
                    "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/></dict></plist>")
        line = " ".join(_quote(a) for a in command)
        return (f"Save this as ~/.config/systemd/user/{LABEL}.service, then run: systemctl --user enable --now "
                f"{LABEL}.service\n[Unit]\nDescription=Tapwright daemon\n\n[Service]\nExecStart={line}\nRestart=on-failure\n\n"
                "[Install]\nWantedBy=default.target")

    def removal(self) -> str:
        if self.platform == "darwin":
            return (f"Run: launchctl unload ~/Library/LaunchAgents/{LABEL}.plist, then delete that file.")
        return f"Run: systemctl --user disable --now {LABEL}.service, then delete ~/.config/systemd/user/{LABEL}.service."


def _quote(word: str) -> str:
    """A word of a systemd ExecStart line: quoted with double quotes when it holds anything but plain characters."""
    if word and all(c.isalnum() or c in "-_./:=,@" for c in word):
        return word
    return '"' + word.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _daemon_answers() -> bool:
    from .client import healthy
    return healthy(store.read_serve_info()) is not None

"""1.10.0, plug and forget: the daemon is always on. `tapwright service` starts it at logon (service.py), an automatic start
uses the service's flags (client.daemon_args), and it doesn't stop for idleness while a phone has been there within the
idle limit (server._idle_watch). No real Task Scheduler call: the runner is faked."""
import asyncio
import json
import subprocess
import time
import types
from pathlib import Path

import pytest

from tapwright import client, server, service, store


# ---- the idle watch (review R191 L4) ----
def _daemon(seen_ago: float | None, now: float | None = None):
    """A daemon whose phone was last there `seen_ago` seconds ago (0: now; None: never)."""
    logged: list[str] = []
    d = types.SimpleNamespace(pump=types.SimpleNamespace(viewers=0), ws_clients=0, last_request=time.monotonic() - 3600,
                              log=logged.append, seen=seen_ago)
    d.phone_last_seen = lambda: None if d.seen is None else time.monotonic() - d.seen
    return d, logged


async def _watch(d, ticks: float = 0.1, limit: float = 1.0) -> bool:
    stop = asyncio.Event()
    task = asyncio.create_task(server._idle_watch(d, stop, limit=limit, every=0.01))
    try:
        await asyncio.wait_for(stop.wait(), ticks)
    except asyncio.TimeoutError:
        pass
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return stop.is_set()


@pytest.mark.parametrize("seen_ago", [0.0, 0.5])
def test_no_idle_stop_while_a_phone_was_there_within_the_limit(seen_ago):
    """0: live, locked or stuck at a lock now; 0.5 s ago (within the 1 s limit): a short USB drop."""
    d, logged = _daemon(seen_ago)
    assert asyncio.run(_watch(d)) is False and logged == []


@pytest.mark.parametrize("seen_ago", [None, 5.0])
def test_the_idle_stop_comes_when_no_phone_was_there_for_the_limit(seen_ago):
    """Never seen, or gone for longer than the limit (an `--address` phone away reads `reconnecting`: the state's name
    doesn't matter)."""
    d, logged = _daemon(seen_ago)
    assert asyncio.run(_watch(d)) is True and "no phone for as long" in logged[0]


def test_a_daemon_without_phone_last_seen_stops_as_before():
    d, _ = _daemon(None)
    del d.phone_last_seen
    assert asyncio.run(_watch(d)) is True


def test_a_recent_request_still_keeps_a_daemon_without_a_phone():
    d, _ = _daemon(None)
    d.last_request = time.monotonic()
    assert asyncio.run(_watch(d)) is False


def test_the_daemon_remembers_when_its_phone_was_last_there():
    from fakes import FakeSession
    from tapwright.daemon import Daemon
    from tapwright.device import PasscodeNeeded
    d = Daemon(FakeSession(), log=lambda *a: None)
    assert d.phone_last_seen() is None  # never connected
    d._set_state("live")
    assert time.monotonic() - d.phone_last_seen() < 1
    d._set_state("reconnecting", "connection lost")
    gone_at = d.phone_last_seen()
    assert gone_at is not None and time.monotonic() - gone_at < 1
    time.sleep(0.05)
    assert d.phone_last_seen() == gone_at  # not renewed while it's away
    d.stuck = PasscodeNeeded()  # waiting at the prompt: the phone is there
    assert d.phone_last_seen() > gone_at


# ---- serve's arguments (review R191 L5) ----
def test_serve_args_carry_the_given_flags_with_absolute_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "vault.json").write_text("{}")
    args = service.serve_args({"apps": "com.example.app", "accounts": "vault.json", "maps": "maps", "port": 8417,
                               "no_log": True, "brief": False, "dedicated": False, "phone": None, "cmd": "service",
                               "service_cmd": "install", "no_dialog": True})
    assert args[:2] == ["serve", "--keep-alive"]
    assert args[2:] == ["--apps", "com.example.app", "--accounts", str((tmp_path / "vault.json").resolve()),
                        "--maps", str((tmp_path / "maps").resolve()), "--port", "8417", "--no-log", "--no-dialog"]


def test_serve_args_with_nothing_given():
    assert service.serve_args({}) == ["serve", "--keep-alive"]


@pytest.mark.parametrize("values, words", [
    ({"apps": "x", "dedicated": True}, "can't be used together"),
    ({"phone": True, "apps": "x"}, "can't be used together"),
    ({"settle_on_video": True}, "needs --video"),
    ({"accounts": "no-such-vault.json"}, "no such file"),
    ({"maps": "%USERPROFILE%/maps"}, "% can't go into a logon task"),
])
def test_serve_args_refuse_what_serve_would_refuse_at_every_logon(values, words, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(service.ServiceError, match=words):
        service.serve_args(values)


# ---- Windows: the logon task ----
class Tasks:
    def __init__(self, codes: dict | None = None):
        self.calls: list[list[str]] = []
        self.xml: str | None = None
        self.codes = codes or {}

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[1] == "/Create":
            self.xml = Path(argv[argv.index("/XML") + 1]).read_text(encoding="utf-16")
        return self.codes.get(argv[1], 0), ""


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _svc(tasks, running=None, platform="win32", answers=None):
    """`running`: the serve info of a daemon that answers (None: none). `answers`: a list of what running() gives, in
    turn (the daemon coming up after /Run)."""
    clock = Clock()
    seq = list(answers) if answers is not None else None

    def run():
        if seq is not None:
            return seq.pop(0) if len(seq) > 1 else seq[0]
        return running
    return service.Service(runner=tasks, platform=platform, command=[r"C:\Py Env\pythonw.exe", "-m", "tapwright"],
                           running=run, sleep=clock.sleep, clock=clock)


def test_install_creates_a_hidden_normal_priority_logon_task_with_no_time_limit_and_starts_it():
    tasks = Tasks()
    args = ["serve", "--keep-alive", "--accounts", r"C:\Users\Someone\My Vault\v.json"]
    text, code = _svc(tasks, answers=[None, None, {"port": 1}]).install(args)
    assert code == 0 and [c[1] for c in tasks.calls] == ["/Create", "/Run"]
    assert tasks.calls[0][:3] == ["schtasks", "/Create", "/TN"] and tasks.calls[0][3] == service.TASK_NAME
    x = tasks.xml
    assert "<LogonTrigger>" in x and "<Hidden>true</Hidden>" in x and "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in x
    assert "<Priority>5</Priority>" in x  # normal (review R191 M5: 7 is below normal)
    assert "<RunLevel>LeastPrivilege</RunLevel>" in x and "<MultipleInstancesPolicy>IgnoreNew" in x
    assert r"<Command>C:\Py Env\pythonw.exe</Command>" in x
    expected = subprocess.list2cmdline(["-m", "tapwright", *args])
    assert f"<Arguments>{expected}</Arguments>" in x and '"C:\\Users\\Someone\\My Vault\\v.json"' in x
    assert "the daemon answers" in text and "After the phone restarts" in text
    # the flags are saved for automatic starts, not the command line (review R192 M2)
    assert json.loads(service.service_file().read_text()) == {"flags": {"accounts": r"C:\Users\Someone\My Vault\v.json"}}


def test_install_says_so_when_the_started_daemon_never_answers():
    text, code = _svc(Tasks(), running=None).install(["serve", "--keep-alive"])
    assert code == 0 and "still starting (no answer within 30 s)" in text and "the daemon answers" not in text


def test_install_with_a_daemon_running_does_not_start_another():
    tasks = Tasks()
    text, code = _svc(tasks, running={"port": 1}).install(["serve", "--keep-alive"])
    assert code == 0 and [c[1] for c in tasks.calls] == ["/Create"] and "keeps its flags" in text


def test_a_refused_task_is_an_error_and_saves_nothing():
    text, code = _svc(Tasks({"/Create": 1})).install(["serve", "--keep-alive"])
    assert code == 1 and text.startswith("Error: Task Scheduler refused") and service.saved_args() is None


def test_uninstall_removes_the_task_its_flags_and_the_empty_folder():
    tasks = Tasks()
    service._save({})
    text, code = _svc(tasks).uninstall()
    assert code == 0 and "Removed" in text and tasks.calls[0][:2] == ["schtasks", "/Delete"]
    assert tasks.calls[1][0] == "powershell" and "DeleteFolder('Tapwright'" in tasks.calls[1][-1]
    assert "GetTasks(1).Count -eq 0" in tasks.calls[1][-1]  # only an empty folder (a schedule's task keeps it)
    assert service.saved_args() is None
    gone = Tasks({"/Delete": 1, "/Query": 1})
    assert _svc(gone).uninstall() == ("There was no Tapwright service task.", 0)


def test_status_says_when_the_running_daemon_lacks_the_services_flags(tmp_path):
    vault = str((tmp_path / "v.json").resolve())
    (tmp_path / "v.json").write_text("{}")
    service._save({"accounts": vault})
    text, _ = _svc(Tasks(), running={"port": 1, "keep_alive": True, "accounts": vault}).status()
    assert "is installed" in text and "daemon is running" in text and "wasn't started" not in text
    text, _ = _svc(Tasks(), running={"port": 1, "keep_alive": False, "accounts": None}).status()
    assert "wasn't started with the service's flags (--keep-alive missing, --accounts missing)" in text
    text, _ = _svc(Tasks({"/Query": 1}), running=None).status()
    assert "not installed" in text and "not running" in text


# ---- an automatic start uses the service's flags (review R191 M4) ----
def test_an_automatic_start_uses_the_saved_service_flags(monkeypatch, tmp_path):
    monkeypatch.setattr(client, "_SERVE_ARGS", ["--accounts", "schedule-vault.json"])
    assert client.daemon_args() == ["serve", "--accounts", "schedule-vault.json"]  # no service: as before
    vault = tmp_path / "v.json"
    vault.write_text("{}")
    service._save({"accounts": str(vault.resolve())})
    assert client.daemon_args() == ["serve", "--keep-alive", "--accounts", str(vault.resolve())]
    seen = {}

    class Proc:
        pass

    def popen(cmd, **kw):
        seen["cmd"] = cmd
        return Proc()
    monkeypatch.setattr(client.subprocess, "Popen", popen)
    client.start_daemon()
    assert seen["cmd"][-4:] == ["serve", "--keep-alive", "--accounts", str(vault.resolve())]


@pytest.mark.parametrize("text", ["not json", "[]", json.dumps({"args": []}), json.dumps({"args": ["--keep-alive"]}),
                                  json.dumps({"args": ["serve", 3]}), json.dumps({"flags": {"new_token": True}}),
                                  json.dumps({"flags": {"apps": ["x"]}}), json.dumps({"flags": {"no_log": False}}),
                                  json.dumps({"args": ["serve", "--keep-alive", "--new-token"]}), json.dumps({})])
def test_a_broken_service_file_is_never_run(text):
    path = service.service_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    with pytest.raises(service.ServiceError):
        service.saved_args()


def test_the_serve_info_carries_the_flags_status_compares():
    import inspect
    source = inspect.getsource(server.serve)
    assert '"keep_alive": bool(keep_alive)' in source and '"accounts":' in source


# ---- other systems: what to add (review R191 L6) ----
def test_macos_gets_a_launch_agent_kept_alive_only_after_a_failure():
    tasks = Tasks()
    text, code = _svc(tasks, platform="darwin").install(["serve", "--keep-alive", "--apps", "com.example.app"])
    assert code == 0 and tasks.calls == []
    assert "<key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>" in text
    assert "<key>KeepAlive</key><true/>" not in text
    # this interpreter, not whatever `tapwright` is first on PATH
    assert "<string>C:\\Py Env\\pythonw.exe</string><string>-m</string><string>tapwright</string><string>serve</string>" in text
    assert "launchctl unload" in _svc(tasks, platform="darwin").uninstall()[0]


def test_linux_gets_a_systemd_user_unit():
    text, _ = _svc(Tasks(), platform="linux").install(["serve", "--keep-alive", "--accounts", "/home/a b/v.json"])
    assert "systemctl --user enable --now com.tapwright.serve.service" in text
    assert 'ExecStart="C:\\\\Py Env\\\\pythonw.exe" -m tapwright serve --keep-alive --accounts "/home/a b/v.json"' in text
    assert "Restart=on-failure" in text and "WantedBy=default.target" in text


# ---- the command line ----
def test_the_cli_passes_serve_flags_to_the_service(monkeypatch, capsys):
    from tapwright import cli
    seen = {}

    class Fake:
        def install(self, args, flags=None):
            seen["args"] = args
            return "done", 0
    monkeypatch.setattr(service, "Service", Fake)
    monkeypatch.setattr("sys.argv", ["tapwright", "service", "install", "--apps", "com.example.app", "--no-log",
                                     "--video"])
    assert cli.main() == 0
    assert seen["args"] == ["serve", "--keep-alive", "--apps", "com.example.app", "--no-log", "--video"]
    monkeypatch.setattr("sys.argv", ["tapwright", "service", "install", "--accounts", "no-such-file.json"])
    assert cli.main() == 2 and "no such file" in capsys.readouterr().out


def test_the_cli_refuses_two_modes_as_serve_does(monkeypatch):
    from tapwright import cli
    monkeypatch.setattr("sys.argv", ["tapwright", "service", "install", "--apps", "x", "--dedicated"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 2


def test_service_file_lives_in_the_tools_home():
    assert service.service_file() == store.HOME / "service.json"

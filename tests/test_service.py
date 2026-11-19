"""1.9.1, plug and forget: the daemon is always on. `tapwright service` starts it at logon (service.py), and it never stops
for idleness while a phone is connected (server._idle_watch). No real Task Scheduler call: the runner is faked."""
import asyncio
import subprocess
import time
import types
from pathlib import Path

import pytest

from tapwright import server, service


# ---- the idle watch ----
def _daemon(state: str | None):
    logged: list[str] = []
    d = types.SimpleNamespace(pump=types.SimpleNamespace(viewers=0), ws_clients=0, last_request=time.monotonic() - 3600,
                              log=logged.append)
    if state is not None:
        d.state = state
    return d, logged


async def _watch(d, ticks: float = 0.1) -> bool:
    stop = asyncio.Event()
    task = asyncio.create_task(server._idle_watch(d, stop, limit=1.0, every=0.01))
    try:
        await asyncio.wait_for(stop.wait(), ticks)
    except asyncio.TimeoutError:
        pass
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return stop.is_set()


@pytest.mark.parametrize("state", ["live", "locked", "connecting", "reconnecting", "not-answering"])
def test_no_idle_stop_while_a_phone_is_connected(state):
    d, logged = _daemon(state)
    assert asyncio.run(_watch(d)) is False and logged == []


@pytest.mark.parametrize("state", ["no-device", None])
def test_with_no_phone_the_idle_stop_is_as_before(state):
    d, logged = _daemon(state)
    assert asyncio.run(_watch(d)) is True and "no phone" in logged[0]


def test_the_idle_stop_comes_once_the_phone_is_gone():
    async def go():
        d, _ = _daemon("live")
        stop = asyncio.Event()
        task = asyncio.create_task(server._idle_watch(d, stop, limit=1.0, every=0.01))
        await asyncio.sleep(0.05)
        assert not stop.is_set()
        d.state = "no-device"  # unplugged
        await asyncio.wait_for(stop.wait(), 1.0)
        await asyncio.gather(task, return_exceptions=True)
    asyncio.run(go())


def test_a_recent_request_still_keeps_a_daemon_without_a_phone():
    d, _ = _daemon("no-device")
    d.last_request = time.monotonic()
    assert asyncio.run(_watch(d)) is False


# ---- serve's arguments ----
def test_serve_args_carry_the_given_flags_with_absolute_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = service.serve_args({"apps": "com.example.app", "accounts": "vault.json", "maps": "maps", "port": 8417,
                               "no_log": True, "brief": False, "dedicated": False, "phone": None, "cmd": "service",
                               "service_cmd": "install"})
    assert args[:2] == ["serve", "--keep-alive"]
    assert args[2:] == ["--apps", "com.example.app", "--accounts", str((tmp_path / "vault.json").resolve()),
                        "--maps", str((tmp_path / "maps").resolve()), "--port", "8417", "--no-log"]


def test_serve_args_with_nothing_given():
    assert service.serve_args({}) == ["serve", "--keep-alive"]


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


def _svc(tasks, running=False, platform="win32"):
    return service.Service(runner=tasks, platform=platform, command=[r"C:\Py Env\pythonw.exe", "-m", "tapwright"],
                           healthy=lambda: running)


def test_install_creates_a_hidden_logon_task_with_no_time_limit_and_starts_it():
    tasks = Tasks()
    args = ["serve", "--keep-alive", "--accounts", r"C:\Users\Someone\My Vault\v.json"]
    text, code = _svc(tasks).install(args)
    assert code == 0 and [c[1] for c in tasks.calls] == ["/Create", "/Run"]
    assert tasks.calls[0][:3] == ["schtasks", "/Create", "/TN"] and tasks.calls[0][3] == service.TASK_NAME
    x = tasks.xml
    assert "<LogonTrigger>" in x and "<Hidden>true</Hidden>" in x and "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in x
    assert "<RunLevel>LeastPrivilege</RunLevel>" in x and "<MultipleInstancesPolicy>IgnoreNew" in x
    assert r"<Command>C:\Py Env\pythonw.exe</Command>" in x
    expected = subprocess.list2cmdline(["-m", "tapwright", *args])
    assert f"<Arguments>{expected}</Arguments>" in x and '"C:\\Users\\Someone\\My Vault\\v.json"' in x
    assert "Started it now." in text and "After the phone restarts" in text


def test_install_with_a_daemon_running_does_not_start_another():
    tasks = Tasks()
    text, code = _svc(tasks, running=True).install(["serve", "--keep-alive"])
    assert code == 0 and [c[1] for c in tasks.calls] == ["/Create"] and "keeps its flags" in text


def test_a_refused_task_is_an_error():
    text, code = _svc(Tasks({"/Create": 1})).install(["serve", "--keep-alive"])
    assert code == 1 and text.startswith("Error: Task Scheduler refused")


def test_uninstall_and_status():
    tasks = Tasks()
    text, code = _svc(tasks).uninstall()
    assert code == 0 and "Removed" in text and tasks.calls[-1][:2] == ["schtasks", "/Delete"]
    gone = Tasks({"/Delete": 1, "/Query": 1})
    assert _svc(gone).uninstall() == ("There was no Tapwright service task.", 0)
    text, _ = _svc(Tasks(), running=True).status()
    assert "is installed" in text and "daemon is running" in text
    text, _ = _svc(Tasks({"/Query": 1})).status()
    assert "not installed" in text and "not running" in text


# ---- other systems: what to add ----
def test_macos_gets_a_launch_agent_that_runs_at_load_and_is_kept_alive(monkeypatch):
    monkeypatch.setattr(service.shutil, "which", lambda name: "/usr/local/bin/tapwright")
    tasks = Tasks()
    text, code = _svc(tasks, platform="darwin").install(["serve", "--keep-alive", "--apps", "com.example.app"])
    assert code == 0 and tasks.calls == []
    assert "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>" in text
    assert "<string>/usr/local/bin/tapwright</string><string>serve</string><string>--keep-alive</string>" in text
    assert "launchctl unload" in _svc(tasks, platform="darwin").uninstall()[0]


def test_linux_gets_a_systemd_user_unit(monkeypatch):
    monkeypatch.setattr(service.shutil, "which", lambda name: None)
    text, _ = _svc(Tasks(), platform="linux").install(["serve", "--keep-alive", "--accounts", "/home/a b/v.json"])
    assert "systemctl --user enable --now com.tapwright.serve.service" in text
    assert 'ExecStart="C:\\\\Py Env\\\\pythonw.exe" -m tapwright serve --keep-alive --accounts "/home/a b/v.json"' in text
    assert "Restart=on-failure" in text and "WantedBy=default.target" in text


def test_the_cli_passes_serve_flags_to_the_service(monkeypatch, capsys):
    from tapwright import cli
    seen = {}

    class Fake:
        def install(self, args):
            seen["args"] = args
            return "done", 0
    monkeypatch.setattr(service, "Service", Fake)
    monkeypatch.setattr("sys.argv", ["tapwright", "service", "install", "--apps", "com.example.app", "--no-log"])
    assert cli.main() == 0
    assert seen["args"] == ["serve", "--keep-alive", "--apps", "com.example.app", "--no-log"]
    monkeypatch.setattr("sys.argv", ["tapwright", "service", "install", "--phone", "--apps", "x"])
    assert cli.main() == 2

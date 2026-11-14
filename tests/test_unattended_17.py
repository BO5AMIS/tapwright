"""Tapwright 1.7 unattended running (AGENT-TOOLS §24.4, §24.5): `--phone` mode as a ceiling, the runner restarted with
its settings, the watchdog's one message per event, and `doctor --unattended`'s new lines. Fakes only: no phone."""
import asyncio
import json

import pytest
from fakes import FakeSession
from starlette.testclient import TestClient
from test_daemon import finish, make, run, started

from tapwright import cli, client, connect, needs, thin, watchdog
from tapwright.ceiling import Ceiling
from tapwright.control import Refused
from tapwright.device import ConnectionLost
from tapwright.server import create_app
from tapwright.stream import NO_IDLE_WAIT, PRESETS

TOKEN = "t" * 43
BARE = {"Authorization": f"Bearer {TOKEN}", "X-Tapwright-Client": "agent-1"}


class Seen:
    def __init__(self):
        self.calls: list[dict] = []

    async def run_tool(self, client_id, name, args, scope=None, policy=None, ux_rules=None, **more):
        self.calls.append({"scope": scope})
        return {"text": "ok", "image": None, "image_size": None}

    def status(self):
        return {"protocol": 1}


def post(ceiling, headers):
    seen = Seen()
    with TestClient(create_app(seen, TOKEN, hosts={"testserver"}, ceiling=ceiling)) as c:
        r = c.post("/api/look", headers={**BARE, **headers}, json={})
    return r, seen


# ---- --phone mode (§24.4) ----

def test_phone_mode_is_every_app_like_dedicated():
    r, seen = post(None, {"X-Tapwright-Mode": "phone"})
    assert r.status_code == 200 and seen.calls == [{"scope": None}]


def test_phone_mode_is_a_ceiling_a_call_may_narrow_but_never_widen():
    for headers in ({"X-Tapwright-Mode": "phone"}, {"X-Tapwright-Mode": "dedicated"}, {"X-Tapwright-Apps": "com.a"}):
        r, seen = post(Ceiling(phone=True), headers)
        assert r.status_code == 200, headers
    r, seen = post(Ceiling(phone=True), {"X-Tapwright-Apps": "com.a"})
    assert seen.calls[0]["scope"].apps == ("com.a",)  # narrowed to one app
    r, seen = post(Ceiling(apps=("com.a",)), {"X-Tapwright-Mode": "phone"})
    assert r.status_code == 403 and "can't ask for phone mode" in r.json()["error"]["message"] and seen.calls == []
    assert "--phone" in r.json()["error"]["message"]


def test_personal_mode_stays_the_default():
    r, seen = post(None, {})
    assert seen.calls[0]["scope"].apps == ()  # no apps named: none, as before
    client.set_scope(["com.a"])
    assert client.SCOPE == {"X-Tapwright-Apps": "com.a"}
    client.set_scope(None, phone=True)
    assert client.SCOPE == {"X-Tapwright-Mode": "phone"}
    client.set_scope([])
    assert client.SCOPE == {}


def run_serve(monkeypatch, *argv):
    from tapwright import device, server
    got: dict = {}
    monkeypatch.setattr("sys.argv", ["tapwright", "serve", *argv])
    monkeypatch.setattr(server, "serve", lambda **kw: got.update(kw) or 0)
    monkeypatch.setattr(device, "run", lambda fn, *a: fn(*a))
    return cli.main(), got


def test_serve_phone_is_the_mode_and_phone_address_still_dials(monkeypatch, capsys):
    _, got = run_serve(monkeypatch, "--phone")
    assert got["ceiling"].phone and got["ceiling"].is_set and got["phone"] is None
    _, got = run_serve(monkeypatch, "--phone", "--keep-alive")
    assert got["ceiling"].phone and got["keep_alive"]
    _, got = run_serve(monkeypatch, "--phone", "100.64.0.9")  # the address of earlier versions
    assert not got["ceiling"].phone and got["phone"] == "100.64.0.9" and "--address" in capsys.readouterr().err
    _, got = run_serve(monkeypatch, "--address", "100.64.0.9", "--phone")
    assert got["ceiling"].phone and got["phone"] == "100.64.0.9"
    _, got = run_serve(monkeypatch)
    assert not got["ceiling"].is_set  # never the default
    with pytest.raises(SystemExit):
        run_serve(monkeypatch, "--phone", "--apps", "com.a")
    with pytest.raises(SystemExit):
        run_serve(monkeypatch, "--phone", "--dedicated")


def test_mcp_install_and_thin_call_carry_phone_mode():
    assert connect.mcp_args([], False, phone=True)[:2] == ["mcp", "--phone"]
    with pytest.raises(connect.ConnectError):
        connect.check_scope(["com.a"], False, True)
    with pytest.raises(connect.ConnectError):
        connect.check_scope([], True, True)
    connect.check_scope([], False, True)
    a = thin._parser().parse_args(["look", "--phone"])
    assert thin._headers(a, TOKEN)["X-Tapwright-Mode"] == "phone"
    with pytest.raises(SystemExit):
        thin._parser().parse_args(["look", "--phone", "--apps", "com.a"])


def test_never_grantable_screens_still_ask_a_person_in_phone_mode(tmp_path):
    """§19.2: the scope is every app, but a grant never covers Settings' panes, Passwords or Wallet."""
    from fakes import button
    from tapwright import policy
    from tapwright.auditlog import AuditLog
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"version": 2, "confirm": ["Remove"], "block": [], "grants": [
        {"name": "settings_remove", "app": "com.apple.Preferences", "words": ["Remove"], "until": "2099-01-01",
         "perDay": 5, "draftsOnly": False}]}), encoding="utf-8")

    async def go():
        d = make(audit=AuditLog(tmp_path / "audit"))
        d.session.app = ("Settings", "com.apple.Preferences")
        d.session.tree = [button("e1", "Remove", "remove")]
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False}, scope=None, policy=policy.load(path))
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "remove"}, "look": False}, scope=None, policy=policy.load(path))
        assert e.value.code == "not_allowed" and "needs_confirmation" in str(e.value)
        assert "tap" not in d.session.actions()
        await finish(stop, task)
    run(go)


# ---- the runner kept alive (§24.5) ----

def test_a_runner_ios_ended_is_restarted_with_backoff_and_the_session_settings_again():
    async def go():
        d = make(backoff=(0.01, 0.02))
        stop, task = await started(d)
        d.preset = "light"  # a quality the viewer chose since
        for _ in range(3):  # iOS ends the runner, again and again (days of running)
            d.session.calls.clear()
            d.lost(ConnectionLost("WebDriverAgent stopped"))
            for _ in range(300):
                await asyncio.sleep(0.01)
                if d.state == "live" and any(isinstance(c, tuple) for c in d.session.calls):
                    break
            assert d.state == "live"
            sent = [c[1] for c in d.session.calls if isinstance(c, tuple) and c[0] == "settings"]
            assert {**PRESETS["light"], **NO_IDLE_WAIT} in sent  # re-applied on the new session (1.6.2 fix A)
            assert d.session.session_settings == {**PRESETS["light"], **NO_IDLE_WAIT}
        await finish(stop, task)
    run(go)


def test_a_runner_that_keeps_failing_is_retried_at_the_longest_backoff_forever():
    async def go():
        s = FakeSession()
        d = make(s, backoff=(0.005, 0.01))
        stop, task = await started(d)


        boom = ConnectionLost("can't reach WebDriverAgent")
        original = s.ensure

        async def failing():
            s.calls.append("ensure")
            raise boom
        s.ensure = failing
        d.lost(boom)
        for _ in range(100):
            await asyncio.sleep(0.01)
        tries = s.calls.count("ensure")
        assert tries >= 5 and d.state == "reconnecting"  # it never gives up
        s.ensure = original
        for _ in range(200):
            await asyncio.sleep(0.01)
            if d.state == "live":
                break
        assert d.state == "live"
        await finish(stop, task)
    run(go)


# ---- the watchdog (§24.5) ----

class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def dog():
    said: list = []
    clock = Clock()
    return watchdog.Watchdog(lambda kind, text: said.append((kind, text)), clock=clock, grace=60), said, clock


def test_one_message_per_event_and_a_short_drop_says_nothing():
    w, said, clock = dog()
    w.state("live", "USB")
    w.state("reconnecting")
    clock.t = 30
    w.state("live", "USB")  # back within the grace: a reconnect, not an event
    assert said == []
    w.state("no-device")
    for t in (61, 90, 600, 6000):
        clock.t = 30 + t
        w.state("no-device")
    assert said == [("gone", watchdog.GONE_USB)]  # once, however long it stays gone
    w.state("live", "local network")
    assert said[-1] == ("back", "Watchdog: the phone is back (over local network).") and len(said) == 2
    w.state("reconnecting")
    clock.t += 61
    w.state("reconnecting")
    assert said[-1] == ("gone", watchdog.GONE_WIFI) and len(said) == 3


def test_nothing_is_said_before_the_phone_was_ever_there():
    w, said, clock = dog()
    w.state("no-device")
    clock.t = 1000
    w.state("no-device")
    assert said == []


def test_a_low_battery_is_said_once_until_it_is_charged_again():
    w, said, _ = dog()
    for level in (25, 19, 15, 10, 19, 25):
        w.battery(level)
    assert said == [("battery", watchdog.BATTERY.format(percent=19))]
    w.battery(35)
    w.battery(12)
    assert said[-1] == ("battery", watchdog.BATTERY.format(percent=12)) and len(said) == 2
    w.battery(None)
    w.battery(5, charging=True)  # (on the charger: nothing to do)
    assert len(said) == 2


def test_the_daemon_says_it_in_the_log_the_hosts_channel_and_each_agents_next_answer():
    async def go():
        logged: list = []
        d = make(log=logged.append)
        d.watchdog = watchdog.Watchdog(d._watchdog_say, clock=Clock(), grace=0)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("b", "look", {"screenshot": False})
        channel = d.dialog_channel("a")
        d._set_state("no-device", "phone not found: plug it in and unlock it")
        d._set_state("no-device", "phone not found: plug it in and unlock it")
        await d.watch_once()
        assert [line for line in logged if line.startswith("Watchdog:")] == [watchdog.GONE_USB]
        notices = [e for e in channel.after(0) if e["type"] == "notice"]
        assert [(e["kind"], e["message"]) for e in notices] == [("gone", watchdog.GONE_USB)]
        for agent in ("a", "b"):
            with pytest.raises(Refused) as e:
                await d.run_tool(agent, "look", {"screenshot": False})
            assert str(e.value).count(f"Note: {watchdog.GONE_USB}") == 1
            with pytest.raises(Refused) as e:  # said once: not in the answer after
                await d.run_tool(agent, "look", {"screenshot": False})
            assert watchdog.GONE_USB not in str(e.value)
        stop.set()
        await asyncio.wait_for(task, 2)
    run(go)


def test_the_battery_is_read_now_and_then_while_the_phone_is_up():
    async def go():
        d = make()
        levels = [(15, False)]

        async def battery():
            return levels[0]
        d.session.device.battery = battery
        logged: list = []
        d.log = logged.append
        stop, task = await started(d)
        await d.watch_once()
        await d.watch_once()  # within battery_every: not read again, nothing said again
        assert [line for line in logged if line.startswith("Watchdog:")] == [watchdog.BATTERY.format(percent=15)]
        await finish(stop, task)
    run(go)


# ---- doctor --unattended (§24.5) ----

OK = needs.Facts(trusted=True, locked=False, developer_mode=True, ui_automation=True, passcode_given=True, battery=80,
                 runner="ok", runner_why="signed until 2027-01-01", reachable=True, wake_timers=True, tasks=2)


def test_a_machine_ready_for_a_long_run_lists_each_check_as_ready():
    assert needs.unattended(OK) == []
    ready = needs.unattended_ready(OK)
    assert ready[0].startswith("The runner (WebDriverAgent) is installed and current")
    assert "The phone is paired and reachable." in ready and "Wake timers are allowed: the PC wakes for a scheduled run." in ready
    assert "Scheduled tasks: 2 schedule(s), each with its task." in ready


@pytest.mark.parametrize("change, starts, fix", [
    ({"runner": "missing", "runner_why": "setup hasn't installed it"}, "The runner (WebDriverAgent) isn't installed",
     "tapwright setup"),
    ({"runner": "reinstall", "runner_why": "the phone has build 1; this Tapwright uses 2"}, "The runner", "doctor --fix"),
    ({"reachable": False}, "The phone can't be reached", "tapwright probe"),
    ({"passcode_given": False}, "No passcode is given", "--accounts"),
    ({"wake_timers": False}, "Wake timers are off", "RTCWAKE 1"),
    ({"tasks_missing": ("daily_post",)}, 'The schedule "daily_post" has no Task Scheduler task', "schedule remove daily_post"),
    ({"vault_problem": "it isn't JSON"}, "The vault file can't be used: it isn't JSON", "--accounts"),
    ({"ui_automation": None}, "Enable UI Automation can't be read", "Enable UI Automation"),
])
def test_each_problem_is_one_line_with_a_clear_fix(change, starts, fix):
    (stop,) = needs.unattended(OK._replace(**change))
    assert stop.what.startswith(starts) and fix in stop.fix


def test_wake_timers_are_read_from_powercfg_and_only_on_windows():
    out = ("    Power Setting GUID: bd3b718a-0680-4d9d-8ab2-e1d2b4ac806d  (Allow wake timers)\n"
           "      Possible Setting Index: 000\n      Possible Setting Index: 001\n"
           "    Current AC Power Setting Index: 0x{ac:08x}\n    Current DC Power Setting Index: 0x00000000\n")
    asked = []

    def runner(ac):
        def go(argv):
            asked.append(argv)
            return 0, out.format(ac=ac)
        return go
    assert cli.wake_timers(runner(1), "win32") is True
    assert cli.wake_timers(runner(0), "win32") is False
    assert cli.wake_timers(runner(2), "win32") is False  # important wake timers only: a task's isn't one
    assert asked[0] == ["powercfg", "/query", "SCHEME_CURRENT", "SUB_SLEEP", "RTCWAKE"]  # a read, never a set
    assert cli.wake_timers(lambda argv: (1, ""), "win32") is None
    assert cli.wake_timers(runner(1), "darwin") is None


def test_the_scheduled_tasks_are_checked_when_schedules_exist():
    class Schedules:
        @staticmethod
        def names():
            return ["daily_post", "weekly"]

        class Scheduler:
            def exists(self, name):
                return name != "weekly"
    assert cli.scheduled_tasks(Schedules) == {"tasks": 2, "tasks_missing": ("weekly",)}


def test_the_runner_check_reads_what_setup_recorded(monkeypatch):
    from tapwright import store
    monkeypatch.setattr(store, "load_state", lambda: {})
    assert cli.runner_facts()["runner"] == "missing"


def test_doctor_prints_ready_and_stops_and_never_the_passcode(tmp_path, capsys):
    path = tmp_path / "vault.json"
    path.write_text(json.dumps({"version": 2, "vault": {"phone": {"kind": "phone", "passcode": "482913"}}}),
                    encoding="utf-8")
    facts = needs.Facts(**asyncio.run(cli.unattended_facts([], str(path), status={"state": "live"})))
    assert facts.passcode_given and facts.vault_problem is None
    cli.print_unattended(needs.unattended(facts._replace(wake_timers=False)), needs.unattended_ready(facts))
    out = capsys.readouterr().out
    assert "Ready for an unattended run:" in out and "phone.passcode (never shown)" in out and "Fix: " in out
    assert "482913" not in out


def test_the_mcp_server_shows_a_notice_in_chat_and_asks_nothing():
    from tapwright import hostdialog
    sent: list = []

    class Session:
        async def send_log_message(self, level, data, logger=None):
            sent.append((level, data, logger))

    class Ctx:
        session = Session()

        async def elicit(self, *a, **kw):
            raise AssertionError("a notice is never a dialog")

    async def go():
        dialogs = hostdialog.HostDialogs(poll=lambda since, wait: {}, answer=lambda i, v: None)
        dialogs._calls.append(Ctx())
        dialogs._handle({"type": "notice", "id": "w1", "kind": "gone", "message": watchdog.GONE_USB})
        await asyncio.sleep(0.05)
        assert sent == [("warning", watchdog.GONE_USB, "tapwright")] and dialogs._shown == {}
    run(go)

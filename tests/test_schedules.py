"""Automations (docs/AGENT-TOOLS.md §22.1 to §22.3, §23): the schedule file, the commands, the Task Scheduler definition,
a run, each reason a run can't start, the run lock, and audit lines with no values. No phone, no real task."""
import argparse
import datetime
import json
import os
import time
import xml.etree.ElementTree as ET

import pytest

from tapwright import auditlog, cli, pidlock, schedulerun, schedules, unlock

NOW = datetime.datetime(2026, 11, 14, 8, 0, 0)
UTC = datetime.timezone.utc
T1 = 1_763_013_612_481
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
BUNDLE = "com.example.wayfare"


class Tasks:
    """A fake `schtasks`: records each call, keeps the created tasks."""

    def __init__(self, fail_create=False):
        self.calls, self.tasks, self.fail_create = [], {}, fail_create

    def __call__(self, argv):
        self.calls.append(argv)
        verb, name = argv[1], argv[argv.index("/TN") + 1]
        if verb == "/Create":
            if self.fail_create:
                return 1, "ERROR: Access is denied."
            with open(argv[argv.index("/XML") + 1], encoding="utf-16") as f:
                self.tasks[name] = f.read()
            return 0, "SUCCESS"
        if verb == "/Delete":
            return (0, "SUCCESS") if self.tasks.pop(name, None) is not None else (1, "ERROR: The system cannot find the file")
        return (0, "") if name in self.tasks else (1, "ERROR")


def sched(tasks=None, platform="win32"):
    return schedules.Scheduler(tasks or Tasks(), platform, ["C:\\Py\\python.exe", "-m", "tapwright"], lambda: NOW)


@pytest.fixture
def home(tmp_path, monkeypatch):
    from tapwright import store
    monkeypatch.setattr(store, "HOME", tmp_path / "home")
    return tmp_path / "home"


def make_route(home, name="post_update", bundle=BUNDLE):
    path = home / "maps" / bundle / "routes"
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{name}.json").write_text("{}", encoding="utf-8")


# ---- the file -------------------------------------------------------------------------------------------------------
def test_the_file_round_trips_exactly_as_the_spec_writes_it(tmp_path):
    (tmp_path / "captions.txt").write_text("Hello", encoding="utf-8")
    s = schedules.Schedule("post_update", "post_update", {"caption": "@captions.txt"}, "09:00 daily", None,
                           "C:\\me\\wayfare-policy.json", "C:\\me\\vault.json", None)
    path = schedules.write(s, tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"version": 1, "tool": "tapwright", "route": "post_update", "with": {"caption": "@captions.txt"},
                    "at": "09:00 daily", "trigger": None, "policy": "C:\\me\\wayfare-policy.json",
                    "accounts": "C:\\me\\vault.json", "maps": None}
    back = schedules.load("post_update", tmp_path)
    assert back.to_json() == data and back.file == str(path)
    assert schedules.read_values(back) == {"caption": "Hello"}  # a relative @file is relative to the schedule file's folder


def test_a_file_the_spec_shows_loads_with_both_triggers(tmp_path):
    for trigger in ({"file": "C:\\inbox"}, {"notification": "com.example.wayfare"}):
        text = json.dumps({"version": 1, "tool": "tapwright", "route": "r", "with": {}, "at": None, "trigger": trigger})
        assert schedules.parse("x", text).trigger == trigger


@pytest.mark.parametrize("change,why", [
    ({"version": 2}, "version must be 1"), ({"tool": "tapline"}, 'tool must be "tapwright"'),
    ({"at": None}, "exactly one of at and trigger"), ({"trigger": {"file": "x"}}, "exactly one of at and trigger"),
    ({"at": "25:00 daily"}, "not a time of day"), ({"at": "9am"}, "at is"),
    ({"at": None, "trigger": {"file": ""}}, "non-empty"), ({"at": None, "trigger": {"sms": "x"}}, "trigger is"),
    ({"route": "Not A Route"}, "route must be"), ({"with": {"a b": "c"}}, "isn't a variable name"),
    ({"with": {"a": True}}, "text or a number"), ({"policy": 3}, "policy must be"),
])
def test_a_bad_file_is_refused_with_its_reason(change, why):
    data = {"version": 1, "tool": "tapwright", "route": "r", "with": {}, "at": "09:00 daily", "trigger": None, **change}
    with pytest.raises(schedules.ScheduleError, match=why):
        schedules.parse("x", json.dumps(data))


def test_a_single_run_in_the_past_is_refused():
    with pytest.raises(schedules.ScheduleError, match="has passed"):
        schedules.parse_at("2026-11-13T09:00", NOW)
    assert schedules.parse_at("2026-11-20T09:00", NOW) == "2026-11-20T09:00"
    assert schedules.parse_at("09:00 weekdays") == "09:00 weekdays"


def test_names_are_safe_file_names():
    for bad in ("", "a/b", "..", "con", "nul.", "-x", "a" * 61):
        with pytest.raises(schedules.ScheduleError):
            schedules.check_name(bad)


# ---- the commands ----------------------------------------------------------------------------------------------------
def test_add_list_remove_with_a_time_schedule(tmp_path):
    tasks, where = Tasks(), tmp_path / "s"
    (tmp_path / "captions.txt").write_text("hi", encoding="utf-8")
    text, code = schedules.add("post_update", at="09:00 daily", with_=[f"caption=@{tmp_path / 'captions.txt'}", "tag=x"],
                               scheduler=sched(tasks), where=where, now=NOW)
    assert code == 0 and "wakes the PC" in text and "Tapwright\\post_update" in tasks.tasks
    data = json.loads((where / "post_update.json").read_text(encoding="utf-8"))
    assert data["with"] == {"caption": "@" + str((tmp_path / "captions.txt").resolve()), "tag": "x"}
    listed, code = schedules.listing_text(sched(tasks), where, tmp_path / "runs")
    assert code == 0 and "post_update: route post_update, 09:00 daily; task installed" in listed
    assert schedules.add("post_update", at="10:00 daily", scheduler=sched(tasks), where=where, now=NOW)[1] == 1  # a name is taken
    said, code = schedules.remove("post_update", sched(tasks), where)
    assert code == 0 and not tasks.tasks and not (where / "post_update.json").exists() and "Removed the Task" in said
    assert schedules.remove("post_update", sched(tasks), where)[1] == 1
    assert schedules.listing_text(sched(tasks), where)[0].startswith("No schedules")


def test_add_refuses_what_it_cannot_use(tmp_path):
    where, s = tmp_path / "s", sched()
    cases = [dict(), dict(at="09:00 daily", on_file=str(tmp_path)), dict(at="soon"), dict(at="09:00 daily", with_=["nope"]),
             dict(at="09:00 daily", with_=["a=@missing.txt"]), dict(at="09:00 daily", policy=str(tmp_path / "none.json")),
             dict(at="2020-01-01T09:00")]
    for kw in cases:
        text, code = schedules.add("r", scheduler=s, where=where, now=NOW, **kw)
        assert code == 1 and text.startswith("Error: "), kw
    assert not where.exists() or not list(where.glob("*.json"))
    assert schedules.add("Bad Route", at="09:00 daily", scheduler=s, where=where, now=NOW)[1] == 1


def test_a_task_scheduler_refusal_leaves_no_schedule_behind(tmp_path):
    where = tmp_path / "s"
    text, code = schedules.add("r", at="09:00 daily", scheduler=sched(Tasks(fail_create=True)), where=where, now=NOW)
    assert code == 1 and "was not added" in text and "Access is denied" in text and not list(where.glob("*.json"))


def test_remove_keeps_the_file_when_the_task_cannot_be_removed(tmp_path):
    where, tasks = tmp_path / "s", Tasks()
    schedules.add("r", at="09:00 daily", scheduler=sched(tasks), where=where, now=NOW)

    def stuck(argv):
        return (1, "ERROR: Access is denied.") if argv[1] in ("/Delete", "/Query") and False else \
            ((1, "ERROR: Access is denied.") if argv[1] == "/Delete" else (0, ""))
    text, code = schedules.remove("r", schedules.Scheduler(stuck, "win32"), where)
    assert code == 1 and "the schedule file was kept" in text and (where / "r.json").exists()


def test_a_notification_trigger_is_polled_by_a_task(tmp_path):
    """(W134: the notification trigger fires now; tests/test_notifications.py has the check itself.)"""
    tasks, where = Tasks(), tmp_path / "s"
    text, code = schedules.add("r", on_notification=BUNDLE, scheduler=sched(tasks), where=where)
    assert code == 0 and f"{BUNDLE}'s notifications every 2 minutes" in text and "Tapwright\\r" in tasks.tasks
    assert schedules.load("r", where).trigger == {"notification": BUNDLE}
    assert "task installed" in schedules.listing_text(sched(tasks), where)[0]


def test_the_cli_commands_call_through(tmp_path, monkeypatch, home, capsys):
    real = schedules.Scheduler
    monkeypatch.setattr(schedules, "Scheduler", lambda *a, **k: real(Tasks(), "win32", ["py"], lambda: NOW))
    monkeypatch.setattr("sys.argv", ["tapwright", "schedule", "add", "post_update", "--at", "09:00 daily", "--with", "a=b"])
    assert cli.main() == 0 and (home / "schedules" / "post_update.json").exists()
    monkeypatch.setattr("sys.argv", ["tapwright", "schedule", "list"])
    assert cli.main() == 0 and "post_update" in capsys.readouterr().out
    monkeypatch.setattr("sys.argv", ["tapwright", "schedule", "remove", "post_update"])
    assert cli.main() == 0 and not (home / "schedules" / "post_update.json").exists()


# ---- the Task Scheduler definition -------------------------------------------------------------------------------------
def wake(xml):
    root = ET.fromstring(xml.split("?>", 1)[1])
    return root.find("t:Settings/t:WakeToRun", NS).text, root


def test_a_time_schedule_wakes_the_pc_and_runs_run_schedule():
    for at in ("09:00 daily", "09:00 weekdays", "2026-11-20T09:00"):
        s = schedules.Schedule("post_update", "post_update", {}, at, None)
        flag, root = wake(sched().xml(s))
        assert flag == "true"
        assert root.find("t:Actions/t:Exec/t:Command", NS).text == "C:\\Py\\python.exe"
        assert root.find("t:Actions/t:Exec/t:Arguments", NS).text == "-m tapwright run-schedule post_update"
    daily = ET.fromstring(sched().xml(schedules.Schedule("a", "r", {}, "09:00 daily", None)).split("?>", 1)[1])
    assert daily.find("t:Triggers/t:CalendarTrigger/t:StartBoundary", NS).text == "2026-11-14T09:00:00"
    week = sched().xml(schedules.Schedule("a", "r", {}, "09:00 weekdays", None))
    assert "<Friday/>" in week and "<Saturday/>" not in week


def test_a_file_trigger_polls_without_waking_the_pc():
    s = schedules.Schedule("inbox", "r", {}, None, {"file": "C:\\inbox"})
    flag, root = wake(sched().xml(s))
    assert flag == "false"
    assert root.find("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", NS).text == "PT2M"
    assert root.find("t:Actions/t:Exec/t:Arguments", NS).text == "-m tapwright run-schedule inbox --if-triggered"


def test_the_task_is_created_from_an_xml_file(tmp_path):
    tasks = Tasks()
    schedules.add("r", at="09:00 daily", scheduler=sched(tasks), where=tmp_path, now=NOW)
    argv = tasks.calls[0]
    assert argv[:4] == ["schtasks", "/Create", "/TN", "Tapwright\\r"] and "/XML" in argv and argv[-1] == "/F"
    assert "<WakeToRun>true</WakeToRun>" in tasks.tasks["Tapwright\\r"]


def test_other_systems_get_what_to_add_and_nothing_is_run(tmp_path):
    ran = Tasks()
    cron = schedules.add("r", at="09:30 weekdays", scheduler=sched(ran, "linux"), where=tmp_path / "a", now=NOW)[0]
    assert "30 9 * * 1-5" in cron and "run-schedule r" in cron
    mac = schedules.add("r", at="09:30 daily", scheduler=sched(ran, "darwin"), where=tmp_path / "b", now=NOW)[0]
    assert "LaunchAgents/com.tapwright.schedule.r.plist" in mac and "<key>Hour</key><integer>9</integer>" in mac
    file_cron = schedules.add("i", on_file=str(tmp_path), scheduler=sched(ran, "linux"), where=tmp_path / "c")[0]
    assert "*/2 * * * *" in file_cron and "--if-triggered" in file_cron
    assert ran.calls == []


# ---- a file trigger --------------------------------------------------------------------------------------------------------
def test_a_file_trigger_sees_only_files_that_appeared_and_settled(tmp_path):
    inbox, where = tmp_path / "inbox", tmp_path / "s"
    inbox.mkdir()
    (inbox / "old.txt").write_text("x")
    s = schedules.Schedule("i", "r", {}, None, {"file": str(inbox)})
    assert schedules.new_files(s, where) == []  # the first look only notes what is there
    (inbox / "new.txt").write_text("y")
    mtime = (inbox / "new.txt").stat().st_mtime_ns
    assert schedules.new_files(s, where, now_ns=mtime + 100) == []  # still being written
    assert schedules.new_files(s, where, now_ns=mtime + 5 * 10**9) == ["new.txt"]
    assert schedules.new_files(s, where, now_ns=mtime + 6 * 10**9) == []  # seen


# ---- a run -----------------------------------------------------------------------------------------------------------------
class Phone:
    """A fake client module: a status, and what `drive` answers."""

    class DaemonError(RuntimeError):
        def __init__(self, code, message, draft=False):
            super().__init__(message)
            self.code = code
            self.draft = draft

    def __init__(self, states=None, answer=None, error=None, between=None):
        self.states = list(states or [{"state": "live", "driver": None, "paused": False}])
        self.answer, self.error, self.between = answer or {"text": "Arrived at home by route post_update in 3.2 s."}, error, between
        self.calls, self.scope = [], []

    def status(self):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    def set_scope(self, apps, dedicated=False):
        self.scope.append((apps, dedicated))

    def set_policy(self, rules=None):
        self.scope.append(("policy", rules.file if rules else None))

    def set_accounts(self, file=None):
        self.scope.append(("accounts", file))

    def set_maps(self, folder=None):
        self.scope.append(("maps", folder))

    def call(self, tool, args, client_id, timeout=120.0, dialog=False):
        self.calls.append((tool, args, client_id, dialog))
        if self.between:
            self.between()
        if self.error:
            raise self.error
        return self.answer


class Clock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds

    def monotonic(self):
        return self.t


def setup(tmp_path, home, phone, name="post_update", **over):
    make_route(home)
    where = tmp_path / "sched"
    schedules.write(schedules.Schedule(name, "post_update", {"caption": "Hello", "account": "tester2"}, "09:00 daily", None,
                                       over.pop("policy", None), over.pop("accounts", None), None), where)
    clock = Clock()
    over.setdefault("unlock_phone", None)  # the default hook talks to a real daemon; the tests that want it pass it
    deps = schedulerun.Deps(client=phone, sleep=clock.sleep, monotonic=clock.monotonic, where=where, runs=tmp_path / "runs",
                            audit_folder=tmp_path / "audit", now=lambda: datetime.datetime(2026, 11, 14, 6, 0, tzinfo=UTC),
                            lock=pidlock.Deps(pid=4242, alive=lambda p: True, start_of=lambda p: T1), **over)
    return deps, clock


def audit_lines(tmp_path):
    return [json.loads(x) for x in auditlog.read_lines(tmp_path / "audit")]


def test_a_run_drives_the_route_with_its_values_and_writes_the_report(tmp_path, home):
    phone = Phone()
    deps, _ = setup(tmp_path, home, phone)
    out = schedulerun.run("post_update", deps=deps)
    assert (out.status, out.code, out.result) == ("done", 0, "done")
    [(tool, args, client_id, dialog)] = phone.calls
    assert tool == "drive" and args["route"] == "post_update" and args["with"] == {"caption": "Hello"}
    assert args["account"] == "tester2" and client_id == "schedule-post_update" and dialog is False  # asks nobody
    assert (phone.scope[0]) == ([BUNDLE], False)  # the run may use the route's app only
    report = json.loads(open(out.report, encoding="utf-8").read())
    assert out.report.endswith("post_update-20260614T060000Z.json".replace("20260614", "20261114"))
    assert report["status"] == "done" and report["started"] == "2026-11-14T06:00:00Z" and "Arrived at home" in report["report"]
    md = open(out.report[:-5] + ".md", encoding="utf-8").read()
    assert "Grants used: none" in md and "Vault names used: none" in md
    assert [(x["kind"], x["name"], x["detail"]) for x in audit_lines(tmp_path)] == [
        ("schedule", "post_update", "started"), ("schedule", "post_update", "done")]
    assert not schedules.lock_of("post_update", deps.where).exists()  # released


def test_a_run_reports_the_grants_it_used_and_the_vault_names_never_values(tmp_path, home):
    audit = auditlog.AuditLog(tmp_path / "audit")

    def during():  # the daemon's lines, written while the route drives
        audit.write("grant", app=BUNDLE, name="wayfare_posting", detail='tap "Post" (1 of 3 today)')
        audit.write("grant", app=BUNDLE, name="wayfare_posting", detail='tap "Post" (2 of 3 today)')
        audit.write("vault", name="tester2", detail="password into secure field")
    audit.write("grant", name="old_one", detail="before the run")  # not this run's: written before it began
    phone = Phone(between=during)
    deps, _ = setup(tmp_path, home, phone)
    time.sleep(0.05)
    deps.now = lambda: datetime.datetime.now(UTC)
    out = schedulerun.run("post_update", deps=deps)
    report = json.loads(open(out.report, encoding="utf-8").read())
    assert report["grants_used"] == {"wayfare_posting": 2} and report["vault_names_used"] == ["tester2"]
    assert "Grants used: wayfare_posting (2)" in out.text and "Vault names used: tester2" in out.text
    assert "hunter2" not in open(out.report, encoding="utf-8").read()


def test_the_vault_and_grants_files_reach_the_daemon_as_this_clients_own(tmp_path, home):
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"version": 2, "confirm": ["Post"], "block": [], "grants": []}), encoding="utf-8")
    vault = tmp_path / "vault.json"
    phone = Phone()
    deps, _ = setup(tmp_path, home, phone, policy=str(policy), accounts=str(vault))
    schedulerun.run("post_update", deps=deps)
    assert ("policy", str(policy.resolve())) in phone.scope and ("accounts", str(vault)) in phone.scope


@pytest.mark.parametrize("code,message,result,status,draft", [
    ("expect_failed", "Ran 2 of 5 steps; stopped at step 3 (tap id=save): text \"x\" didn't appear within 2 s.",
     "failed at step 3 (expect)", "failed", False),
    ("off_route", "Stopped at step 2 of 4: the app is at a screen the route doesn't know.", "failed at step 2 (off_route)", "failed", False),
    ("not_allowed", "needs_confirmation: the draft wasn't sent (They declined.). Ask your user what to do.",
     "stopped: a draft is waiting for a person.", "stopped", True),
    # the signal is the daemon's internal flag: the text decides nothing
    ("not_allowed", "something else entirely", "stopped: a draft is waiting for a person.", "stopped", True),
    ("not_allowed", "needs_confirmation: Allow deleting the draft \"Trip notes\"?", schedulerun.NEEDS_PERSON, "stopped", False),
    ("not_allowed", "needs_confirmation: the draft wasn't sent. Ask your user what to do.", schedulerun.NEEDS_PERSON, "stopped", False),
    ("not_allowed", "needs_confirmation: Allow tapping \"Post\"?", schedulerun.NEEDS_PERSON, "stopped", False),
])
def test_a_run_stops_at_the_first_failure_and_reports_it(tmp_path, home, code, message, result, status, draft):
    phone = Phone(error=Phone.DaemonError(code, message, draft=draft))
    deps, _ = setup(tmp_path, home, phone)
    out = schedulerun.run("post_update", deps=deps)
    assert (out.status, out.result, out.code) == (status, result, 1) and len(phone.calls) == 1  # nothing more is done
    assert audit_lines(tmp_path)[-1]["detail"] == result


# ---- when a run can't start ---------------------------------------------------------------------------------------------------
def test_a_busy_phone_is_waited_for_then_the_run_goes(tmp_path, home):
    busy = {"state": "live", "driver": "person", "paused": False}
    phone = Phone(states=[busy, busy, {"state": "live", "driver": None, "paused": False}])
    deps, clock = setup(tmp_path, home, phone)
    assert schedulerun.run("post_update", deps=deps).status == "done" and len(clock.slept) == 2


@pytest.mark.parametrize("driver", ["person", "agent"])
def test_a_phone_busy_for_ten_minutes_is_skipped_and_never_queued(tmp_path, home, driver):
    phone = Phone(states=[{"state": "live", "driver": driver, "paused": False}])
    deps, clock = setup(tmp_path, home, phone)
    out = schedulerun.run("post_update", deps=deps)
    assert (out.status, out.result, out.code) == ("skipped", "skipped: the phone was in use", 3)
    assert clock.t >= 600 and phone.calls == []
    assert [x["detail"] for x in audit_lines(tmp_path)] == ["skipped: the phone was in use"]
    assert json.loads(open(out.report, encoding="utf-8").read())["status"] == "skipped"
    assert not schedules.lock_of("post_update", deps.where).exists()


def test_a_locked_phone_is_skipped_without_a_passcode_route(tmp_path, home):
    phone = Phone(states=[{"state": "locked", "driver": None, "paused": False}])
    deps, clock = setup(tmp_path, home, phone)
    out = schedulerun.run("post_update", deps=deps)
    assert (out.result, out.code) == ("skipped: the phone is locked", 3) and phone.calls == [] and clock.slept == []
    assert audit_lines(tmp_path)[-1]["detail"] == "skipped: the phone is locked"


def test_a_locked_phone_is_unlocked_only_through_the_passcode_route_once(tmp_path, home):
    phone = Phone(states=[{"state": "locked", "driver": None, "paused": False},
                          {"state": "live", "driver": None, "paused": False}])
    asked = []
    deps, _ = setup(tmp_path, home, phone, unlock_phone=lambda s: asked.append(s.name) or True)
    assert schedulerun.run("post_update", deps=deps).status == "done" and asked == ["post_update"]
    locked = Phone(states=[{"state": "locked", "driver": None, "paused": False}])
    deps, _ = setup(tmp_path, home, locked, unlock_phone=lambda s: False)
    assert schedulerun.run("post_update", deps=deps).result == "skipped: the phone is locked"


def test_the_default_unlock_is_the_passcode_hook(tmp_path, home):
    assert schedulerun.Deps().unlock_phone is unlock.schedule_hook


def test_a_locked_phone_with_a_passcode_in_the_vault_is_unlocked_by_the_default_hook(tmp_path, home, monkeypatch):
    from tapwright import client
    asked = []
    monkeypatch.setattr(client, "unlock_phone", lambda accounts=None, timeout=60.0: asked.append(accounts) or
                        {"ok": True, "code": "unlocked", "message": "ok"})
    vault = str(tmp_path / "vault.json")
    phone = Phone(states=[{"state": "locked", "driver": None, "paused": False},
                          {"state": "live", "driver": None, "paused": False}])
    deps, _ = setup(tmp_path, home, phone, unlock_phone=schedulerun.Deps().unlock_phone, accounts=vault)
    assert schedulerun.run("post_update", deps=deps).status == "done" and asked == [vault] and len(phone.calls) == 1


def test_a_locked_phone_with_no_passcode_is_skipped_by_the_default_hook(tmp_path, home, monkeypatch):
    from tapwright import client
    asked = []
    monkeypatch.setattr(client, "unlock_phone", lambda accounts=None, timeout=60.0: asked.append(accounts) or
                        {"ok": False, "code": "no_passcode", "message": "no passcode"})
    phone = Phone(states=[{"state": "locked", "driver": None, "paused": False}])
    deps, _ = setup(tmp_path, home, phone, unlock_phone=schedulerun.Deps().unlock_phone)
    out = schedulerun.run("post_update", deps=deps)
    assert (out.status, out.result, out.code) == ("skipped", "skipped: the phone is locked", 3) and len(asked) == 1
    assert phone.calls == []


def test_the_previous_run_still_going_is_skipped(tmp_path, home):
    phone = Phone()
    deps, _ = setup(tmp_path, home, phone)
    holder = pidlock.Deps(pid=7777, alive=lambda p: True, start_of=lambda p: T1)  # a live process
    token = pidlock.take(schedules.lock_of("post_update", deps.where), holder)
    out = schedulerun.run("post_update", deps=deps)
    assert (out.status, out.result) == ("skipped", "skipped: the previous run is still going") and phone.calls == []
    assert [x["detail"] for x in audit_lines(tmp_path)] == ["skipped: the previous run is still going"]
    assert schedules.lock_of("post_update", deps.where).exists()  # the live holder's lock was not touched
    pidlock.release(schedules.lock_of("post_update", deps.where), token)


def test_a_dead_holders_lock_is_taken_over(tmp_path, home):
    phone = Phone()
    deps, _ = setup(tmp_path, home, phone)
    lock = schedules.lock_of("post_update", deps.where)
    lock.write_text(json.dumps({"owner": "x", "pid": 999999, "started": T1}), encoding="utf-8")
    deps.lock = pidlock.Deps(pid=4242, alive=lambda p: p != 999999, start_of=lambda p: T1)
    assert schedulerun.run("post_update", deps=deps).status == "done" and not lock.exists()


def test_an_unreadable_lock_is_never_taken_over(tmp_path, home):
    deps, _ = setup(tmp_path, home, Phone())
    schedules.lock_of("post_update", deps.where).write_text("", encoding="utf-8")
    assert schedulerun.run("post_update", deps=deps).result == schedulerun.PREVIOUS


def test_a_paused_phone_counts_as_in_use(tmp_path, home):
    phone = Phone(states=[{"state": "live", "driver": None, "paused": True}])
    deps, _ = setup(tmp_path, home, phone)
    assert schedulerun.run("post_update", deps=deps).result == schedulerun.IN_USE


def test_a_phone_that_is_not_connected_fails_the_run_and_says_so(tmp_path, home):
    phone = Phone(states=[{"state": "reconnecting", "detail": "no tunnel", "driver": None}])
    deps, _ = setup(tmp_path, home, phone)
    out = schedulerun.run("post_update", deps=deps)
    assert out.status == "failed" and out.code == 2 and "isn't connected (no tunnel)" in out.result


def test_a_route_that_is_not_saved_fails_before_the_phone_is_touched(tmp_path, home):
    phone = Phone()
    deps, _ = setup(tmp_path, home, phone)
    for f in (home / "maps").rglob("*.json"):
        f.unlink()
    out = schedulerun.run("post_update", deps=deps)
    assert out.status == "failed" and "no saved route" in out.result and phone.calls == []


def test_a_missing_at_file_fails_the_run(tmp_path, home):
    deps, _ = setup(tmp_path, home, Phone())
    s = schedules.load("post_update", deps.where)
    schedules.write(schedules.Schedule(s.name, s.route, {"caption": "@gone.txt"}, s.at, None), deps.where)
    assert "can't read" in schedulerun.run("post_update", deps=deps).result


def test_if_triggered_runs_only_when_a_file_appeared(tmp_path, home):
    make_route(home)
    inbox, where = tmp_path / "inbox", tmp_path / "sched"
    inbox.mkdir()
    schedules.write(schedules.Schedule("inbox", "post_update", {}, None, {"file": str(inbox)}), where)
    phone = Phone()
    deps = schedulerun.Deps(client=phone, where=where, runs=tmp_path / "runs", audit_folder=tmp_path / "audit",
                            lock=pidlock.Deps(pid=4242, alive=lambda p: True, start_of=lambda p: T1))
    assert schedulerun.run("inbox", if_triggered=True, deps=deps).status == "idle"  # the first look
    assert phone.calls == [] and not (tmp_path / "runs").exists()
    old = (datetime.datetime.now() - datetime.timedelta(minutes=5)).timestamp()
    (inbox / "a.jpg").write_text("x")
    os.utime(inbox / "a.jpg", (old, old))
    assert schedulerun.run("inbox", if_triggered=True, deps=deps).status == "done" and len(phone.calls) == 1
    assert schedulerun.run("inbox", if_triggered=True, deps=deps).status == "idle" and len(phone.calls) == 1


def test_an_unknown_schedule_is_reported_not_raised(tmp_path, home):
    deps, _ = setup(tmp_path, home, Phone())
    out = schedulerun.run("nope", deps=deps)
    assert out.code == 2 and "no schedule called" in out.result


# ---- vault values never reach a report or an audit line ---------------------------------------------------------------------------
def test_a_value_in_the_daemons_answer_is_redacted_from_the_report_and_the_audit(tmp_path, home):
    vault = tmp_path / "vault.json"
    vault.write_text(json.dumps({"version": 1, "accounts": {"tester2": {"username": "tester2@example.com",
                                                                        "password": "hunter2-synthetic"}}}), encoding="utf-8")
    phone = Phone(answer={"text": "typed hunter2-synthetic into the field"})
    deps, _ = setup(tmp_path, home, phone, accounts=str(vault))
    out = schedulerun.run("post_update", deps=deps)
    assert out.status == "done"
    everything = open(out.report, encoding="utf-8").read() + open(out.report[:-5] + ".md", encoding="utf-8").read() + \
        out.text + "".join(auditlog.read_lines(tmp_path / "audit"))
    assert "hunter2-synthetic" not in everything and "<tester2's password>" in everything


# ---- unlock -------------------------------------------------------------------------------------------------------------------------
def test_unlock_refuses_a_live_holder_and_clears_a_dead_one(tmp_path):
    where = tmp_path / "s"
    where.mkdir()
    lock = schedules.lock_of("r", where)
    schedules.write(schedules.Schedule("r", "r", {}, "09:00 daily", None), where)
    assert "no lock" in schedules.unlock("r", where, tmp_path / "a")[0]
    live = pidlock.Deps(pid=os.getpid())
    token = pidlock.take(lock, live)
    text, code = schedules.unlock("r", where, tmp_path / "a")
    assert code == 1 and f"pid {os.getpid()}" in text and lock.exists()
    pidlock.release(lock, token)
    lock.write_text(json.dumps({"owner": "x", "pid": 999999, "started": T1}), encoding="utf-8")
    dead = pidlock.Deps(alive=lambda p: False)
    assert schedules.stuck_locks(where, dead) == [("r", str(lock), "held by pid 999999, which is not running")]
    text, code = schedules.unlock("r", where, tmp_path / "a", dead)
    assert code == 0 and "Removed" in text and not lock.exists()
    assert json.loads(auditlog.read_lines(tmp_path / "a")[-1])["kind"] == "approval"
    lock.write_text("", encoding="utf-8")
    assert schedules.stuck_locks(where) and schedules.unlock("r", where, tmp_path / "a")[1] == 0


def test_the_cli_runs_a_schedule_and_unlocks(tmp_path, monkeypatch, home, capsys):
    monkeypatch.setattr("sys.argv", ["tapwright", "schedule", "unlock", "ghost"])
    assert cli.main() == 0 and "no lock" in capsys.readouterr().out
    monkeypatch.setattr("sys.argv", ["tapwright", "run-schedule", "ghost"])
    assert cli.main() == 2 and "no schedule called" in capsys.readouterr().out

"""Diagnostics (AGENT-TOOLS §15): the app's log, its crash reports, the crash note and a failure's own lines."""
import argparse
import asyncio
import datetime
import json
import re
from xml.etree import ElementTree as ET

import pytest
from fakes import FakeFrame, FakeReport
from test_daemon import finish, make, run, started

from tapwright import cli, client, diagnostics
from tapwright.actionlog import ActionLog
from tapwright.control import Refused
from tapwright.daemon import SPRINGBOARD, Daemon, Scope
from tapwright.device import ConnectionLost
from tapwright.diagnostics import LogEntry, LogRing

APP = "com.example.app"
BASE = 1_700_000_000_000  # epoch ms of a made-up moment


def clock(ms: int) -> str:
    return diagnostics.clock_of(ms)


def tuned(d: Daemon) -> Daemon:
    """Short waits, the app running as pid 42."""
    d.log_retry, d.log_idle, d.log_warmup, d.crash_poll, d.log_check = 0.01, 0.3, 0.0, 0.01, 0.01
    d.log_drain = 0.05
    d.session.device.pids[APP] = [42]
    return d


async def settle(check, tries=300):
    for _ in range(tries):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("never happened")


async def opened(d: Daemon) -> None:
    await d.run_tool("a", "open_app", {"app": "Example", "look": False})
    await settle(lambda: 42 in d.session.device.streamed)


def die(d: Daemon, report=None) -> None:
    """The app goes away on the next tap: SpringBoard is in front, no process is left, and (with `report`) a report."""
    session = d.session
    real = session.tap

    async def tap(target, hold_ms=60):
        text = await real(target, hold_ms)
        session.app, session.foreground = ("Home screen", SPRINGBOARD), SPRINGBOARD
        session.device.pids[APP] = []
        if report is not None:
            session.device.crashes["Example"] = [report]
        return text
    session.tap = tap


# ---- logs: the ring ----
def test_the_ring_keeps_the_last_2000_entries_of_an_app():
    ring = LogRing()
    for n in range(2500):
        ring.add(LogEntry(BASE + n, "info", f"line {n}"))
    assert diagnostics.LOG_RING == 2000 and len(ring.entries) == 2000
    assert ring.entries[0].text == "line 500" and ring.latest() == BASE + 2499


def test_levels_since_grep_and_limit_pick_the_newest_oldest_first():
    ring = LogRing()
    for n, level in enumerate(["info", "warning", "error", "info", "warning", "error"]):
        ring.add(LogEntry(BASE + n, level, f"Timeout {n}" if n % 2 else f"fine {n}"))
    assert [e.text for e in ring.select(level="error")] == ["fine 2", "Timeout 5"]
    assert [e.level for e in ring.select(level="warning")] == ["warning", "error", "warning", "error"]
    assert [e.text for e in ring.select(since=BASE + 3)] == ["fine 4", "Timeout 5"]  # only newer
    assert [e.text for e in ring.select(grep="TIMEOUT")] == ["Timeout 1", "Timeout 3", "Timeout 5"]  # ignoring case
    assert [e.text for e in ring.select(limit=2)] == ["fine 4", "Timeout 5"]  # the newest two, oldest first


def test_the_level_of_a_line():
    assert diagnostics.level_of(0x10, "x") == diagnostics.level_of(0x11, "x") == "error"
    assert diagnostics.level_of(0x00, "[javascript] WARN something odd") == "warning"
    assert diagnostics.level_of(0x00, "Request failed with status 500") == "warning"
    assert diagnostics.level_of(0x01, "Started") == diagnostics.level_of(0x02, "Started") == "info"


def test_long_text_is_cut_with_how_much_is_missing():
    assert diagnostics.cut("a" * 300) == "a" * 300
    assert diagnostics.cut("b" * 357) == "b" * 300 + "…(+57)"
    line = diagnostics.line_of(LogEntry(BASE, "error", "c" * 400))
    assert line == f"{clock(BASE)} error " + "c" * 300 + "…(+100)"


def test_the_answer_lists_entries_then_latest_and_says_what_was_asked_when_there_are_none():
    ring = LogRing()
    ring.add(LogEntry(BASE, "info", "hello"))
    ring.add(LogEntry(BASE + 5, "error", "boom"))
    text, count = diagnostics.logs_answer(ring, None, "info", None, 100)
    assert text == f"{clock(BASE)} info hello\n{clock(BASE + 5)} error boom\nLatest: {BASE + 5}" and count == 2
    text, count = diagnostics.logs_answer(ring, BASE + 5, "warning", "timeout", 100)
    assert text == (f'No log entries at warning or above matching "timeout" since {clock(BASE + 5)}.\n'
                    f"Latest: {BASE + 5}") and count == 0
    assert diagnostics.logs_answer(None, None, "info", None, 100) == ("No log entries.\nLatest: 0", 0)


# ---- logs: the daemon ----
def test_only_the_apps_own_process_is_kept_and_logs_answers_with_it():
    async def go():
        d = tuned(make())
        stop, task = await started(d)
        await opened(d)
        device = d.session.device
        device.emit(42, BASE + 1, "app says hello")
        device.emit(42, BASE + 2, "another process's line", from_pid=7)  # a stream that leaks one is not believed
        device.emit(42, BASE + 3, "app failed to load", raw=0x10)
        await settle(lambda: len(d._rings[APP].entries) == 2)
        assert device.streamed == [42]  # asked for the app's pid alone
        r = await d.run_tool("a", "logs", {})
        assert r["text"] == (f"{clock(BASE + 1)} info app says hello\n{clock(BASE + 3)} error app failed to load\n"
                             f"Latest: {BASE + 3}")
        assert "another process" not in r["text"] and r["image"] is None
        r = await d.run_tool("a", "logs", {"level": "error", "since": BASE + 1, "grep": "FAILED", "limit": 5})
        assert r["text"] == f"{clock(BASE + 3)} error app failed to load\nLatest: {BASE + 3}"
        for bad in ({"level": "loud"}, {"limit": 0}, {"limit": 501}, {"since": "x"}, {"grep": 3}):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "logs", bad)
            assert e.value.code == "bad_params"
        await finish(stop, task)
    run(go)


def test_logs_is_first_used_without_open_app_and_the_reader_stops_with_the_daemon():
    async def go():
        d = tuned(make())
        d.session.app = None
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "logs", {})
        assert e.value.code == "not_found" and "open_app" in str(e.value)
        d.session.app = ("Example", APP)  # in front at the last read: logs is the first use
        r = await d.run_tool("a", "logs", {})
        assert r["text"].startswith("No log entries") and APP in d._log_tasks
        reader = d._log_tasks[APP]
        await finish(stop, task)
        assert reader.done() and not d._log_tasks
    run(go)


def test_a_restarted_app_is_read_by_its_new_process():
    async def go():
        d = tuned(make())
        stop, task = await started(d)
        await opened(d)
        d.session.device.pids[APP] = [43]  # the app was started again
        d.session.device.emit(43, BASE + 9, "second life")
        await d.run_tool("a", "logs", {})
        await settle(lambda: 43 in d.session.device.streamed)
        await settle(lambda: d._rings[APP].latest() == BASE + 9)
        await finish(stop, task)
    run(go)


def test_personal_mode_reads_only_a_given_apps_log():
    async def go():
        d = tuned(make())
        d.session.installed.append({"bundle_id": "com.bank.app", "name": "Bank", "version": "1", "build": "1",
                                    "executable": "Bank"})
        stop, task = await started(d)
        await opened(d)
        d._rings["com.bank.app"] = LogRing()
        d._rings["com.bank.app"].add(LogEntry(BASE, "error", "account 1234 overdrawn"))
        d.session.device.emit(42, BASE + 1, "app line")
        await settle(lambda: len(d._rings[APP].entries) == 1)
        with pytest.raises(Refused) as e:  # no app list: nothing runs
            await d.run_tool("a", "logs", {}, scope=Scope(()))
        assert e.value.code == "not_allowed" and "no_apps" in str(e.value)
        # another app in front, two given, none opened by the agent: nothing is read, and the refusal says why
        d.session.app, d.session.foreground, d._opened = ("Bank", "com.bank.app"), "com.bank.app", None
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "logs", {}, scope=Scope((APP, "com.other.app")))
        assert e.value.code == "not_allowed" and "outside_apps" in str(e.value)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "crash", {}, scope=Scope((APP, "com.other.app")))
        assert e.value.code == "not_allowed"
        # the only given app is read, and the other app's ring never appears, whatever is in front
        r = await d.run_tool("a", "logs", {}, scope=Scope((APP,)))
        assert "app line" in r["text"] and "overdrawn" not in r["text"]
        await finish(stop, task)
    run(go)


def test_logs_and_crash_go_in_the_action_log_as_counts_only(tmp_path):
    async def go():
        d = tuned(make(actions=ActionLog(tmp_path)))
        d.session.device.crashes["Example"] = [FakeReport()]
        stop, task = await started(d)
        await opened(d)
        d.session.device.emit(42, BASE + 1, "secret token abc123")
        await settle(lambda: len(d._rings[APP].entries) == 1)
        await d.run_tool("a", "logs", {"grep": "secret"})
        await d.run_tool("a", "crash", {})
        await finish(stop, task)
    run(go)
    lines = [json.loads(ln) for f in tmp_path.glob("actions-*.jsonl") for ln in f.read_text("utf-8").splitlines()]
    mine = {ln["tool"]: ln for ln in lines if ln["tool"] in ("logs", "crash")}
    assert mine["logs"]["count"] == 1 and mine["crash"]["count"] == 1
    assert "secret" not in "".join(f.read_text("utf-8") for f in tmp_path.glob("actions-*.jsonl"))
    assert "grep" not in mine["logs"]


# ---- crash ----
def test_a_crash_report_is_parsed_with_the_apps_frames_marked():
    info = diagnostics.describe_crash(FakeReport(when=datetime.datetime(2026, 11, 12, 10, 42, 7)), "Example")
    assert info.kind == "EXC_BAD_ACCESS (SIGSEGV)" and info.reason == "KERN_INVALID_ADDRESS at 0x0"
    assert info.top == "EXC_BAD_ACCESS (SIGSEGV) in Example -[HomeController crash] +40"
    assert info.filename == "Example-2026-11-12-101010.ips"
    assert [f[3] for f in info.frames] == [" ", "*", "*", " "]  # the app's own code is marked
    assert info.frames[1].endswith("Example -[HomeController crash] +40")
    lines = diagnostics.crash_lines(info, "Example (com.example.app)")
    assert lines[:2] == ["Crash of Example (com.example.app) at 2026-11-12 10:42:07.",
                         "Exception: EXC_BAD_ACCESS (SIGSEGV) (KERN_INVALID_ADDRESS at 0x0)"]
    assert lines[-1] == "File: Example-2026-11-12-101010.ips"


def test_only_8_frames_and_the_top_frame_when_none_is_the_apps():
    frames = [FakeFrame(f"/usr/lib/lib{n}.dylib", f"fn{n}", n) for n in range(12)]
    info = diagnostics.describe_crash(FakeReport(frames=frames, subtype=None, info={"x": ["abort() called"]}), "Example")
    assert len(info.frames) == 8 and info.top == "EXC_BAD_ACCESS (SIGSEGV) in lib0.dylib fn0 +0"
    assert info.reason == "abort() called" and not any(" * " in f for f in info.frames)


def test_reports_of_other_processes_are_dropped():
    reports = [FakeReport(name="Example"), FakeReport(name="ExampleHelper"), FakeReport(name="Other")]
    reports = [FakeReport(name=r.name, bundle_id=APP) for r in reports]
    assert [r.name for r in diagnostics.only_mine(reports, "Example", APP)] == ["Example"]
    pattern = re.compile(diagnostics.crash_pattern("Example"))
    assert pattern.search("Example-2026-11-12-101010.ips") and pattern.search("Example-2026-11-12-101010-1.ips")
    for other in ("Example-Dev-2026-11-12-101010.ips", "Example.cpu_resource-2026-11-12-101010.ips",
                  "Example.wakeups_resource-2026-11-12-101010.ips", "ExampleHelper-2026-11-12-101010.ips"):
        assert not pattern.search(other)


def test_crash_answers_the_newest_report_since_the_session_began(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "crash_folder", lambda bundle: tmp_path / "crashes" / bundle)

    async def go():
        d = tuned(make())
        stop, task = await started(d)
        await opened(d)
        shown = "Example (com.example.app)"
        r = await d.run_tool("a", "crash", {})
        assert r["text"] == f"No crash report for {shown} since the session began."
        old = FakeReport(when=datetime.datetime.now() - datetime.timedelta(days=2), filename="/Example-old.ips")
        new = FakeReport(filename="/Example-new.ips")
        d.session.device.crashes["Example"] = [new, old]
        d.session.device.crash_files["/Example-new.ips"] = b'{"full": "report"}'
        r = await d.run_tool("a", "crash", {})
        assert r["text"].count("Crash of ") == 1 and "File: Example-new.ips" in r["text"] and "Saved:" not in r["text"]
        # AGENT-TOOLS §15.2: the reports "since the session began, and only the app's own": `all` shows up to 5 of
        # those, never one from before the session (Codex, PR #4)
        r = await d.run_tool("a", "crash", {"all": True})
        assert r["text"].count("Crash of ") == 1 and "Example-old.ips" not in r["text"]
        r = await d.run_tool("a", "crash", {"all": True, "pull": True})
        assert "Example-old.ips" not in r["text"] and not (tmp_path / "crashes" / APP / "Example-old.ips").exists()
        r = await d.run_tool("a", "crash", {"pull": True})  # copies the whole report, and names it
        path = tmp_path / "crashes" / APP / "Example-new.ips"
        assert f"Saved: {path}" in r["text"] and path.read_bytes() == b'{"full": "report"}'
        d.session.device.crashes["Example"] = []
        r = await d.run_tool("a", "crash", {"all": True})
        assert r["text"] == f"No crash report for {shown} since the session began."
        d.session.device.crashes["Example"] = [FakeReport(filename=f"/Example-{n}.ips") for n in range(7)]
        r = await d.run_tool("a", "crash", {"all": True})  # the last 5 of this session
        assert r["text"].count("Crash of ") == diagnostics.CRASH_RECENT == 5
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "crash", {"pull": "yes"})
        assert e.value.code == "bad_params"
        await finish(stop, task)
    run(go)


def test_crash_without_a_known_executable_is_unsupported():
    async def go():
        d = tuned(make())
        d.session.installed[0]["executable"] = ""
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "crash", {})
        assert e.value.code == "unsupported"
        await finish(stop, task)
    run(go)


# ---- the crash note ----
NOTE = "Note: the app crashed: EXC_BAD_ACCESS (SIGSEGV) in Example -[HomeController crash] +40. logs and crash show more."


def test_an_app_that_dies_during_a_call_adds_the_note():
    async def go():
        d = tuned(make())
        stop, task = await started(d)
        await opened(d)
        await d.run_tool("a", "look", {"screenshot": False})
        die(d, FakeReport())
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert r["text"].splitlines()[-1] == NOTE
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert NOTE not in r["text"]  # told once
        await finish(stop, task)
    run(go)


def test_the_note_is_on_an_error_answer_too():
    async def go():
        d = tuned(make())
        stop, task = await started(d)
        await opened(d)
        await d.run_tool("a", "look", {"screenshot": False})
        d.session.device.pids[APP] = []
        d.session.device.crashes["Example"] = [FakeReport()]
        d.session.fail["tap"] = ConnectionLost("the connection dropped")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert e.value.code == "reconnecting" and str(e.value).splitlines()[-1] == NOTE
        await finish(stop, task)
    run(go)


def test_an_app_that_keeps_running_asks_the_phone_for_no_report():
    async def go():
        d = tuned(make())
        stop, task = await started(d)
        await opened(d)
        await d.run_tool("a", "look", {"screenshot": False})
        d.session.device.crashes["Example"] = [FakeReport()]  # (an old one, perhaps)
        asks = d.session.device.crash_asks
        await d.run_tool("a", "tap", {"target": {"ref": "e1"}})  # the app stays in front
        d.session.app, d.session.foreground = ("Home screen", SPRINGBOARD), SPRINGBOARD  # home: the app runs on
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert "crashed" not in r["text"] and d.session.device.crash_asks == asks
        r = await d.run_tool("a", "close_app", {"app": "Example"})  # closing it is no crash
        assert "crashed" not in r["text"]
        await finish(stop, task)
    run(go)


def test_a_crash_that_reaches_the_phone_later_is_told_once_with_its_time():
    async def go():
        d = tuned(make())
        d.crash_polls = 1
        stop, task = await started(d)
        await opened(d)
        await d.run_tool("a", "look", {"screenshot": False})
        die(d)  # no report yet
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert "crashed" not in r["text"] and d._crash_pending
        when = datetime.datetime.now().replace(microsecond=0)
        d.session.device.crashes["Example"] = [FakeReport(when=when)]
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert r["text"].splitlines()[-1] == (f"Note: the app crashed at {when:%H:%M:%S}: EXC_BAD_ACCESS (SIGSEGV) in "
                                              "Example -[HomeController crash] +40. logs and crash show more.")
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert "crashed" not in r["text"] and not d._crash_pending
        await finish(stop, task)
    run(go)


def test_a_report_never_comes_and_the_looking_stops():
    async def go():
        d = tuned(make())
        d.crash_polls = 1
        stop, task = await started(d)
        await opened(d)
        await d.run_tool("a", "look", {"screenshot": False})
        die(d)
        await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        for _ in range(3):
            await d.run_tool("a", "look", {"screenshot": False})
        assert not d._crash_pending
        await finish(stop, task)
    run(go)


# ---- failures carry their diagnostics ----
def during_tap(d: Daemon, emit) -> None:
    """`emit()` runs when the next tap does: the log lines of a step, arriving while it runs."""
    session = d.session
    real = session.tap

    async def tap(target, hold_ms=60):
        emit()
        return await real(target, hold_ms)
    session.tap = tap


def test_a_failed_run_step_adds_the_logs_since_it_began_at_warning_or_above():
    async def go():
        d = tuned(make())
        stop, task = await started(d)
        await opened(d)
        device = d.session.device
        device.emit(42, BASE - 500, "before the step", raw=0x10)
        await settle(lambda: len(d._rings[APP].entries) == 1)

        def lines():
            for n in range(25):  # more than 20: the newest 20 show
                device.emit(42, BASE + 10 + n, f"problem {n}", raw=0x10)
            device.emit(42, BASE + 40, "just info")
        during_tap(d, lines)
        steps = [{"tool": "tap", "target": {"id": "ok-button"}, "expect": {"text": "Never there", "timeout": 0.5}}]
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": steps})
        assert e.value.code == "expect_failed"
        lines = str(e.value).splitlines()
        at = lines.index("Logs (since step 1):")
        shown = lines[at + 1:at + 21]
        assert shown[0].endswith("error problem 5") and shown[-1].endswith("error problem 24")
        assert "before the step" not in str(e.value) and "just info" not in str(e.value)
        assert lines[0].startswith("Ran 0 of 1 steps; stopped at step 1") and at > 0  # after the change block
        await finish(stop, task)
    run(go)


def test_a_failure_without_warnings_adds_no_logs_and_a_crash_adds_its_note():
    async def go():
        d = tuned(make())
        stop, task = await started(d)
        await opened(d)
        steps = [{"tool": "tap", "target": {"id": "ok-button"}, "expect": {"text": "Never there", "timeout": 0.5}}]
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": steps})
        assert "Logs (since" not in str(e.value)
        await d.run_tool("a", "look", {"screenshot": False})
        die(d, FakeReport())
        during_tap(d, lambda: d.session.device.emit(42, BASE, "fatal: lost the database", raw=0x11))
        steps = [{"tool": "wait", "seconds": 0.5},  # (the log line and the death come with the second step)
                 {"tool": "tap", "target": {"ref": "e1"}, "expect": {"text": "Never there", "timeout": 0.5}}]
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": steps})
        text = str(e.value)
        assert text.splitlines()[-1] == NOTE and text.count("the app crashed") == 1
        assert "stopped at step 2" in text and "Logs (since step 2):" in text and "lost the database" in text
        await finish(stop, task)
    run(go)


# ---- review findings (1.3 review, W7) ----
def test_a_report_is_the_apps_only_by_exact_name_type_and_bundle_id():
    mine = FakeReport(name="Example", bundle_id=APP)
    other_app = FakeReport(name="Example", bundle_id="com.other.app")  # same executable name, another app
    no_bundle = FakeReport(name="Example")  # names no bundle: nothing proves it is this app's (Codex, PR #4)
    unnamed = FakeReport(name=None)

    class Unreadable:
        bug_type_str, _metadata, _data = "309", {}, {}

        @property
        def name(self):
            raise KeyError("name")
    unreadable = Unreadable()
    helper = FakeReport(name="Example-Dev", bundle_id=APP)
    resource = FakeReport(name="Example", bundle_id=APP, bug_type="385")  # a resource report is not a crash
    kept = diagnostics.only_mine([mine, other_app, no_bundle, unnamed, helper, resource], "Example", APP)
    assert kept == [mine]
    assert diagnostics.only_mine([unreadable], "Example", APP) == []


def test_the_crash_time_is_compared_in_the_reports_own_zone():
    now_local = datetime.datetime.now().astimezone()
    zone = datetime.timezone(datetime.timedelta(hours=-5))
    old = (now_local - datetime.timedelta(hours=3)).astimezone(zone)  # three hours ago, in a zone far from here's
    stamp = old.strftime("%Y-%m-%d %H:%M:%S.00 %z")
    # pycrashreport drops the offset, leaving the phone's wall clock, which looks like now to this computer
    wall = old.replace(tzinfo=None)
    report = FakeReport(when=wall, stamp=stamp)
    info = diagnostics.describe_crash(report, "Example")
    assert info.when.tzinfo is not None and not diagnostics.newer(info, now_local.replace(tzinfo=None))
    fresh = (now_local - datetime.timedelta(seconds=5)).astimezone(zone)
    info = diagnostics.describe_crash(FakeReport(when=fresh.replace(tzinfo=None),
                                                 stamp=fresh.strftime("%Y-%m-%d %H:%M:%S.00 %z")), "Example")
    assert diagnostics.newer(info, datetime.datetime.now())
    naive = diagnostics.describe_crash(FakeReport(when=datetime.datetime.now()), "Example")  # no offset: as before
    assert naive.when.tzinfo is None and diagnostics.newer(naive, datetime.datetime.now())


def test_a_new_process_is_read_at_once_when_open_app_starts_it_and_the_old_stream_stops():
    async def go():
        d = tuned(make())
        d.log_check = 60.0  # the pid check alone would not notice for a minute: open_app has to
        stop, task = await started(d)
        await opened(d)
        device = d.session.device
        device.pids[APP] = [43]  # the app crashed and was opened again
        await d.run_tool("a", "open_app", {"app": "Example", "look": False})
        await settle(lambda: 43 in device.streamed and 42 in device.stream_closed)
        device.emit(43, BASE + 5, "second life")
        device.emit(42, BASE + 6, "ghost of the first")  # nobody reads the old stream any more
        await settle(lambda: d._rings[APP].latest() == BASE + 5)
        assert [e.text for e in d._rings[APP].entries] == ["second life"]
        await finish(stop, task)
    run(go)


def test_a_process_that_changes_without_open_app_is_found_by_the_watcher():
    async def go():
        d = tuned(make())
        stop, task = await started(d)
        await opened(d)
        d.session.device.pids[APP] = [77]  # started from the phone's home screen, say
        await settle(lambda: 77 in d.session.device.streamed and 42 in d.session.device.stream_closed)
        await finish(stop, task)
    run(go)


def test_a_stream_that_starts_again_for_the_same_process_does_not_repeat_what_the_ring_has():
    async def go():
        d = tuned(make())
        device = d.session.device
        device.replay[42] = [(BASE + 1, 0, "a", 42), (BASE + 2, 0, "b", 42), (BASE + 2, 0, "b2", 42)]  # the phone's replay
        stop, task = await started(d)
        await opened(d)
        await settle(lambda: len(d._rings[APP].entries) == 3)
        device.emit(42, BASE + 3, "c")
        await settle(lambda: len(d._rings[APP].entries) == 4)
        device.replay[42] = [(BASE + 1, 0, "a", 42), (BASE + 2, 0, "b", 42), (BASE + 2, 0, "b2", 42), (BASE + 3, 0, "c", 42)]
        device.streams[42].put_nowait(None)  # the relay closes the stream; the watcher opens another, replay and all
        await settle(lambda: device.streamed.count(42) == 2)
        device.emit(42, BASE + 3, "c2")  # the same millisecond as the newest entry, but a new line: kept
        device.emit(42, BASE + 4, "d")
        await settle(lambda: d._rings[APP].latest() == BASE + 4)
        assert [e.text for e in d._rings[APP].entries] == ["a", "b", "b2", "c", "c2", "d"]
        await finish(stop, task)
    run(go)


def test_personal_mode_keeps_the_apps_logs_and_crash_note_when_the_crash_leaves_the_home_screen_in_front():
    """CodeRabbit, PR #4: _withheld cut a stopped run to its first line once the app's screen was gone, taking the
    app-scoped logs and crash note (which the CLI replay needs for its junit and crash pull) with it."""
    async def go():
        d = tuned(make())
        stop, task = await started(d)
        d.session.app = ("Home screen", SPRINGBOARD)  # the crash left the home screen in front
        message = ("Ran 0 of 1 steps; stopped at step 1 (tap e1): its action ran, but text \"x\" didn't appear.\n"
                   "Screen 400x800 pt @3x · 1 elements\ne2 button \"OK\" id=ok-button @10,100 100x40\n"
                   "Logs (since step 1):\n12:00:01.123 error fatal: lost the database\n"
                   f"{NOTE}")
        kept = await d._withheld("a", "run", {"text": message, "image": None}, {APP: "Example (com.example.app)"}, "Example")
        lines = kept["text"].splitlines()
        assert "Logs (since step 1):" in lines and "12:00:01.123 error fatal: lost the database" in lines
        assert lines[-1] == NOTE or NOTE in lines
        assert "outside_apps" in kept["text"] and "ok-button" not in kept["text"]  # the home screen's list is still not shown
        await finish(stop, task)
    run(go)


def test_a_batch_that_starts_before_log_collection_does_not_report_the_replay_as_since_the_step():
    """CodeRabbit, PR #4: with no ring at the step's start the boundary was epoch zero, so the phone's replay of what
    the app logged before the batch was labelled `since step 1`."""
    async def go():
        d = tuned(make())
        d.log_warmup = 0.05
        device = d.session.device
        device.replay[42] = [(BASE - 5_000, 0x10, "an old error from before the batch", 42)]
        d.session.app = ("Example", APP)  # in front, and logs never asked for
        stop, task = await started(d)
        assert APP not in d._rings
        steps = [{"tool": "tap", "target": {"id": "ok-button"}, "expect": {"text": "Never there", "timeout": 0.5}}]
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": steps})
        assert e.value.code == "expect_failed" and "an old error" not in str(e.value) and "Logs (since" not in str(e.value)
        assert [x.text for x in d._rings[APP].entries] == ["an old error from before the batch"]  # (it is in the ring)
        await finish(stop, task)
    run(go)


def test_the_executable_lookup_of_the_crash_check_has_the_same_deadline_as_the_report_search():
    """CodeRabbit, PR #4: only crash_reports was bounded, so an installation proxy that never answered held the answer."""
    async def go():
        d = tuned(make())
        d.crash_lookup = 0.05

        async def never():
            await asyncio.sleep(3600)
        stop, task = await started(d)
        d.session.apps = never  # (after start-up, which lists the apps itself)
        d._executables.clear()
        began = asyncio.get_running_loop().time()
        assert await d._find_crash(APP, 1) is None
        assert asyncio.get_running_loop().time() - began < 1.0
        await finish(stop, task)
    run(go)


def test_a_crash_found_by_the_tap_inside_performance_is_in_its_answer():
    """CodeRabbit, PR #4: the nested tap consumed the report and its note was thrown away, so the outer check had none."""
    from tapwright.performance import Measurement

    async def go():
        d = tuned(make())
        stop, task = await started(d)
        await opened(d)
        await d.run_tool("a", "look", {"screenshot": False})
        d.session.device.measurement = Measurement(memory_mb=[100.0])
        die(d, FakeReport())
        r = await d.run_tool("a", "performance", {"seconds": 1, "tap": {"ref": "e1"}})
        assert r["text"].splitlines()[-1] == NOTE and r["text"].count("the app crashed") == 1
        await finish(stop, task)
    run(go)


START = BASE / 1000 - 100  # when the app's process started (epoch s): 100 s before BASE


def at(ms: int) -> int:
    """An entry time `ms` after the process started."""
    return int(START * 1000) + ms


def test_a_reused_pid_is_not_read_as_the_app_and_what_it_logged_is_dropped():
    """Codex, PR #4 (P1): the app's pid goes to another app before the next ownership poll."""
    async def go():
        d = tuned(make())
        d.log_check = 0.02
        device = d.session.device
        device.starts[42] = START
        stop, task = await started(d)
        await opened(d)
        device.emit(42, at(5_000), "own line")
        await settle(lambda: len(d._rings[APP].entries) == 1)
        device.pids[APP] = []  # the app is gone ...
        device.holders[42] = ("com.private.app", START + 200)  # ... and another app has its pid, from 200 s on
        device.emit(42, at(250_000), "another app's private line")
        await settle(lambda: 42 in device.stream_closed)
        device.emit(42, at(260_000), "after the stream was closed")
        await asyncio.sleep(0.15)
        assert [e.text for e in d._rings[APP].entries] == ["own line"]
        await finish(stop, task)
    run(go)


def test_what_the_phone_replays_from_before_the_process_started_is_dropped():
    async def go():
        d = tuned(make())
        device = d.session.device
        device.starts[42] = START
        device.replay[42] = [(at(-60_000), 0, "the previous owner of pid 42", 42), (at(-1_500), 0, "also before", 42),
                             (at(5), 0, "mine", 42)]
        stop, task = await started(d)
        await opened(d)
        await settle(lambda: len(d._rings[APP].entries) >= 1)
        await asyncio.sleep(0.05)
        assert [e.text for e in d._rings[APP].entries] == ["mine"]
        await finish(stop, task)
    run(go)


def test_a_stream_whose_owner_cannot_be_checked_is_closed_and_the_unchecked_lines_are_dropped():
    async def go():
        d = tuned(make())
        d.log_check = 0.02
        device = d.session.device
        device.starts[42] = START
        stop, task = await started(d)
        await opened(d)
        device.emit(42, at(1_000), "checked line")
        await settle(lambda: len(d._rings[APP].entries) == 1)
        checks = device.app_pids_calls
        await settle(lambda: device.app_pids_calls > checks + 1)  # a check has passed since that line
        device.proc_error = RuntimeError("the process list can't be read")
        device.emit(42, at(2_000), "line nobody can vouch for")
        await settle(lambda: 42 in device.stream_closed)  # it does not stay open on a phone that can't say
        await settle(lambda: [e.text for e in d._rings[APP].entries] == ["checked line"])  # the unchecked line is taken back
        await finish(stop, task)
    run(go)


def test_a_phone_without_a_log_service_is_tried_a_few_times_and_logs_says_so():
    async def go():
        d = tuned(make())
        d.session.device.stream_error = RuntimeError("no such service: com.apple.os_trace_relay")
        stop, task = await started(d)
        await opened(d)
        await settle(lambda: APP in d._log_dead)
        assert d.session.device.streamed == [42] * diagnostics.LOG_STREAM_FAILS  # then it stops trying
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "logs", {})
        assert e.value.code == "unsupported" and diagnostics.LOGS_NO_SOURCE in str(e.value)
        await asyncio.sleep(0.1)
        assert d.session.device.streamed == [42] * diagnostics.LOG_STREAM_FAILS
        await finish(stop, task)
    run(go)


def test_the_meter_stops_its_sampling_when_start_is_cancelled(monkeypatch):
    from tapwright import device as device_module
    stopped = []

    class Quiet:
        rsd = None

        def log(self, text):
            pass

    async def never_ready(self):
        try:
            await asyncio.sleep(60)
        finally:
            stopped.append("sampling closed")  # where the instrument sessions would be closed
    monkeypatch.setattr(device_module.AppMeter, "_run", never_ready)

    async def go():
        meter = device_module.AppMeter(Quiet(), APP)
        starting = asyncio.create_task(meter.start())
        await asyncio.sleep(0.05)
        starting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await starting
        assert stopped == ["sampling closed"] and meter._task is None
    run(go)


def test_a_cancel_in_the_turn_the_meter_becomes_ready_is_not_lost(monkeypatch):
    """Codex, PR #4: on Python 3.10 asyncio.wait_for returned normally when the inner wait and the cancel came in one
    loop turn, so a cancelled `performance(launch=True)` went on to close and open the app."""
    from tapwright import device as device_module
    stopped = []

    class Quiet:
        rsd = None

        def log(self, text):
            pass

    async def sampling(self):
        try:
            await asyncio.sleep(60)
        finally:
            stopped.append("sampling closed")
    monkeypatch.setattr(device_module.AppMeter, "_run", sampling)

    async def go():
        meter = device_module.AppMeter(Quiet(), APP)
        starting = asyncio.create_task(meter.start())
        await asyncio.sleep(0.05)
        meter._ready.set()  # ready ...
        starting.cancel()  # ... and cancelled in the same turn
        with pytest.raises(asyncio.CancelledError):
            await starting
        assert stopped == ["sampling closed"] and meter._task is None
    run(go)


def test_errors_of_the_phones_services_are_refused_clearly_and_an_unreadable_report_is_told():
    async def go():
        d = tuned(make())
        stop, task = await started(d)
        await opened(d)
        device = d.session.device
        device.crash_error = ConnectionLost("tunnel dropped")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "crash", {})
        assert e.value.code == "reconnecting"
        device.crash_error = RuntimeError("AFC error: permission denied")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "crash", {})
        assert e.value.code == "unsupported" and "permission denied" in str(e.value)
        device.crash_error = None
        device.crash_unreadable = 1  # a half-written report: skipped, said so, and not taken for "no crash"
        r = await d.run_tool("a", "crash", {})
        assert r["text"].startswith("No crash report for") and "1 crash report(s) of this app couldn't be read" in r["text"]
        device.crashes["Example"] = [FakeReport()]
        r = await d.run_tool("a", "crash", {})
        assert "Crash of " in r["text"] and "couldn't be read" in r["text"]
        device.crash_too_big = 2  # a report over the byte cap: never read, and the answer says so
        r = await d.run_tool("a", "crash", {})
        assert diagnostics.CRASH_TOO_BIG.format(n=2, mb=diagnostics.CRASH_MAX_MB) in r["text"]
        await finish(stop, task)
    run(go)


def test_performance_refuses_an_app_that_came_to_the_front_after_the_check():
    from tapwright.performance import Measurement
    async def go():
        d = tuned(make())
        stop, task = await started(d)
        session = d.session
        reads = []

        async def front():
            reads.append(1)
            return APP if len(reads) == 1 else "com.bank.app"  # another app comes forward between the two reads
        session.foreground_bundle_id = front
        session.device.measurement = Measurement(memory_mb=[1.0])
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "performance", {"seconds": 1}, scope=Scope((APP,)))
        assert e.value.code == "not_allowed" and session.device.metered == []
        await finish(stop, task)
    run(go)


def test_a_late_crash_note_goes_to_its_own_client_while_it_drives_that_app():
    async def go():
        d = tuned(make())
        d.crash_polls = 1
        d.session.installed.append({"bundle_id": "com.other.app", "name": "Other", "version": "1", "build": "1",
                                    "executable": "Other"})
        stop, task = await started(d)
        await opened(d)
        await d.run_tool("a", "look", {"screenshot": False})
        die(d)  # no report yet
        await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert "a" in d._crash_pending
        d.session.device.crashes["Example"] = [FakeReport(when=datetime.datetime.now().replace(microsecond=0))]
        # another agent, given only another app: no note, and the note is still waiting for its own client
        r = await d.run_tool("b", "apps", {}, scope=Scope(("com.other.app",)))
        assert "crashed" not in r["text"] and "a" in d._crash_pending
        # the same client, while another app is in front: it waits
        d.session.app, d.session.foreground = ("Other", "com.other.app"), "com.other.app"
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert "crashed" not in r["text"] and "a" in d._crash_pending
        # and when it is back with the stopped app (or the home screen), it gets the note, once
        d.session.app, d.session.foreground = ("Home screen", SPRINGBOARD), SPRINGBOARD
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert "Note: the app crashed at " in r["text"] and "a" not in d._crash_pending
        await finish(stop, task)
    run(go)


def test_nan_and_infinity_are_bad_params_and_a_stored_line_is_capped():
    async def go():
        d = tuned(make())
        stop, task = await started(d)
        await opened(d)
        for bad in ({"since": float("inf")}, {"since": float("nan")}, {"limit": float("inf")}, {"limit": float("nan")}):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "logs", bad)
            assert e.value.code == "bad_params"
        await finish(stop, task)
    run(go)
    ring = LogRing()
    ring.add(LogEntry(BASE, "info", "x" * 5000))
    assert len(ring.entries[0].text) == diagnostics.LOG_TEXT_KEPT == 2000
    assert diagnostics.line_of(ring.entries[0]).endswith("…(+1700)")  # the answer still cuts at 300


def test_the_cli_pulls_a_crash_only_for_the_crash_note_not_for_words_in_the_message(tmp_path, monkeypatch):
    calls = []

    def call(tool, args, client_id, timeout=120.0):
        calls.append(tool)
        raise client.DaemonError("expect_failed", "Ran 0 of 1 steps; stopped at step 1 (tap id=save): text \"x\" didn't "
                                 "appear.\nLogs (since step 1):\n12:00:01.123 error the app crashed while saving, retrying")

    monkeypatch.setattr(client, "call", call)
    monkeypatch.setattr(client, "set_scope", lambda apps, dedicated=False: None)
    a = argparse.Namespace(files=[write_script(tmp_path, "words")], junit=None, apps=APP, dedicated=False)
    assert cli.replay(a) == 1 and calls == ["replay"]


# ---- the CLI ----
FAIL = ('Ran 0 of 1 steps; stopped at step 1 (tap id=save): text "x" didn\'t appear within 2 s.\n'
        "Logs (since step 1):\n12:00:01.123 error db failed\n12:00:01.456 warning slow query\n"
        "Note: the app crashed: EXC_BAD_ACCESS in Example main +12. logs and crash show more.")


def write_script(tmp_path, name):
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps({"format": "agent-tools-script", "version": 1, "name": name, "app": APP,
                                "steps": [{"tool": "tap", "target": {"id": "save"}}]}), "utf-8")
    return str(path)


def test_the_junit_report_carries_the_logs_and_the_crash_report_is_pulled_beside_the_script(tmp_path, monkeypatch, capsys):
    pulled = tmp_path / "phone-side.ips"
    pulled.write_bytes(b"the report")
    calls = []

    def call(tool, args, client_id, timeout=120.0):
        calls.append((tool, args))
        if tool == "crash":
            return {"text": f"Crash of Example at 2026-11-12 10:00:00.\nFile: x.ips\nSaved: {pulled}"}
        raise client.DaemonError("expect_failed", FAIL)

    monkeypatch.setattr(client, "call", call)
    monkeypatch.setattr(client, "set_scope", lambda apps, dedicated=False: None)
    out = tmp_path / "junit.xml"
    a = argparse.Namespace(files=[write_script(tmp_path, "login")], junit=str(out), apps=APP, dedicated=False)
    assert cli.replay(a) == 1
    beside = tmp_path / "login.diff" / "login.crash.ips"
    assert beside.read_bytes() == b"the report" and ("crash", {"pull": True}) in calls
    system_out = ET.parse(out).getroot().find("testcase[@name='login']/system-out").text.splitlines()
    assert system_out == ["Logs (since step 1):", "12:00:01.123 error db failed", "12:00:01.456 warning slow query",
                          "Note: the app crashed: EXC_BAD_ACCESS in Example main +12. logs and crash show more.",
                          f"Crash report: {beside}"]
    assert f"Crash report: {beside}" in capsys.readouterr().out


def test_a_failure_without_diagnostics_has_no_system_out_and_pulls_nothing(tmp_path, monkeypatch):
    calls = []

    def call(tool, args, client_id, timeout=120.0):
        calls.append(tool)
        raise client.DaemonError("expect_failed", "Ran 0 of 1 steps; stopped at step 1 (tap id=save): text \"x\" didn't appear.")

    monkeypatch.setattr(client, "call", call)
    monkeypatch.setattr(client, "set_scope", lambda apps, dedicated=False: None)
    out = tmp_path / "junit.xml"
    a = argparse.Namespace(files=[write_script(tmp_path, "plain")], junit=str(out), apps=APP, dedicated=False)
    assert cli.replay(a) == 1 and calls == ["replay"]
    assert ET.parse(out).getroot().find("testcase[@name='plain']/system-out") is None
    assert not (tmp_path / "plain.diff").exists()


def test_a_line_of_a_process_that_is_not_proven_the_apps_is_in_no_logs_answer():
    """Codex, PR #4 cycle 2: the app's pid went to another app, whose line was in the ring before the watcher's next
    check, and `logs` answered with it. Lines count only once the phone's own list has shown the process is the app's."""
    async def go():
        d = tuned(make())
        d.log_check = 60.0  # the watcher does not look again: only logs' own check stands between the line and the answer
        stop, task = await started(d)
        await opened(d)
        device, ring = d.session.device, d._rings[APP]
        device.emit(42, BASE + 1, "the app's own line")
        await settle(lambda: len(ring.entries) == 1)
        assert "the app's own line" in (await d.run_tool("a", "logs", {}))["text"]  # proven: it is shown
        device.pids[APP] = []  # pid 42 goes to another app, which logs at once
        device.holders[42] = ("com.private.app", START + 60)
        device.emit(42, BASE + 2, "another app's secret")
        await settle(lambda: len(ring.entries) == 2)
        shown = (await d.run_tool("a", "logs", {}))["text"]
        assert "secret" not in shown and "the app's own line" in shown and f"Latest: {BASE + 1}" in shown
        device.holders.clear()  # the app has its pid again, but the phone can't be asked whose the process is
        device.pids[APP] = [42]
        device.emit(42, BASE + 3, "not yet proven")
        await settle(lambda: len(ring.entries) == 2)  # (the other app's secret was taken back when it was seen holding the pid)
        device.proc_error = RuntimeError("no process list")
        assert "not yet proven" not in (await d.run_tool("a", "logs", {}))["text"]
        device.proc_error = None
        assert "not yet proven" in (await d.run_tool("a", "logs", {}))["text"]  # asked and answered: shown
        await finish(stop, task)
    run(go)


def slow_down(d: Daemon, seconds: float) -> None:
    """The next tap takes `seconds` of real time (the batch's own clock is a fake one, so a step would be over at once)."""
    session = d.session
    real = session.tap

    async def tap(target, hold_ms=60):
        await asyncio.sleep(seconds)
        return await real(target, hold_ms)
    session.tap = tap


def test_a_replay_that_arrives_during_the_step_is_not_shown_as_since_the_step():
    """Codex and CodeRabbit, PR #4: the replay of the phone's older lines came after the step began, the ring had no
    boundary, and they were shown from epoch zero as `since step 1`. The boundary is the phone's clock then."""
    async def go():
        d = tuned(make())
        d.log_warmup = 0.05
        device = d.session.device
        device.replay_delay = 0.2  # the stream gives its replay after the warm-up ended, while the step runs
        device.phone_ms = BASE
        device.replay[42] = [(BASE - 5_000, 0x10, "an old error from before the batch", 42)]
        d.session.app = ("Example", APP)  # in front, and logs never asked for
        stop, task = await started(d)
        slow_down(d, 0.45)
        asyncio.get_running_loop().call_later(0.35, device.emit, 42, BASE + 10, "a failure during the step", 0x10)
        steps = [{"tool": "tap", "target": {"id": "ok-button"}, "expect": {"text": "Never there", "timeout": 0.5}}]
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": steps})
        text = str(e.value)
        assert e.value.code == "expect_failed" and "an old error" not in text
        assert "Logs (since step 1):" in text and "a failure during the step" in text  # what happened in the step is shown
        await finish(stop, task)
    run(go)


def test_a_replay_that_arrives_during_a_step_is_not_the_steps_even_when_the_ring_already_has_an_old_entry():
    """Codex and CodeRabbit, PR #4 cycle 3: the ring held an error from T-10 s, so the step's boundary stayed there and a
    second historical error (T-5 s), replayed during the step, was shown as logged since the step."""
    async def go():
        d = tuned(make())
        d.log_warmup = 0.05
        device = d.session.device
        device.phone_ms = BASE
        device.replay[42] = [(BASE - 10_000, 0x10, "the first old error, in the warm-up", 42)]
        d.session.app = ("Example", APP)
        stop, task = await started(d)
        slow_down(d, 0.45)
        loop = asyncio.get_running_loop()
        loop.call_later(0.2, device.emit, 42, BASE - 5_000, "a delayed historical error", 0x10)
        loop.call_later(0.35, device.emit, 42, BASE + 10, "a failure during the step", 0x10)
        steps = [{"tool": "tap", "target": {"id": "ok-button"}, "expect": {"text": "Never there", "timeout": 0.5}}]
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": steps})
        text = str(e.value)
        assert "the first old error" not in text and "a delayed historical error" not in text
        assert "Logs (since step 1):" in text and "a failure during the step" in text
        await finish(stop, task)
    run(go)


def test_a_step_boundary_is_never_older_than_the_phones_clock():
    """Nor newer than the ring's newest entry, when that is the later of the two."""
    async def go():
        d = tuned(make())
        device = d.session.device
        d.session.app = ("Example", APP)
        stop, task = await started(d)
        ring = d._rings.setdefault(APP, diagnostics.LogRing())
        ring.add(LogEntry(BASE - 10_000, "error", "old"))
        device.phone_ms = BASE
        marks = {APP: ring.latest()}
        await d._clock_mark(None, marks)
        assert marks[APP] == BASE
        ring.add(LogEntry(BASE + 500, "error", "newer than the clock reading"))
        marks = {APP: ring.latest()}
        await d._clock_mark(None, marks)
        assert marks[APP] == BASE + 500
        device.phone_ms = None  # no clock: an old entry's time is not a boundary
        marks = {APP: BASE - 10_000}
        await d._clock_mark(None, marks)
        assert marks[APP] is None
        await finish(stop, task)
    run(go)


def test_lines_of_a_replacement_process_stay_out_when_it_exits_and_logs_is_asked_again():
    """Codex, PR #4 cycle 3: another app took the app's pid and logged a secret; it was withheld while that app held the
    pid, but stayed in the ring, and was published once that app was gone and nobody held the pid."""
    async def go():
        d = tuned(make())
        d.log_check = 60.0
        stop, task = await started(d)
        await opened(d)
        device, ring = d.session.device, d._rings[APP]
        device.emit(42, BASE + 1, "the app's own line")
        await settle(lambda: len(ring.entries) == 1)
        assert "the app's own line" in (await d.run_tool("a", "logs", {}))["text"]
        device.pids[APP] = []
        device.holders[42] = ("com.private.app", START + 60)
        device.emit(42, BASE + 2, "another app's secret")
        await settle(lambda: len(ring.entries) == 2)
        assert "secret" not in (await d.run_tool("a", "logs", {}))["text"]
        device.emit(42, BASE + 3, "its second secret")  # more from the pid's new holder, after it was seen holding it
        await settle(lambda: len(ring.entries) >= 2)
        device.holders.clear()  # the other app exits: the app is still absent and nobody holds the pid
        for _ in range(2):
            shown = (await d.run_tool("a", "logs", {}))["text"]
            assert "secret" not in shown and f"Latest: {BASE + 1}" in shown
        assert not any("secret" in e.text for e in ring.entries)
        await finish(stop, task)
    run(go)


def test_a_replacement_process_the_watcher_itself_saw_stays_out_when_it_has_left_before_the_next_look():
    """Codex, PR #4 cycle 4 (P1): the log watcher's own look found another app holding the app's pid, and left the lines it
    had read (a secret) unchecked; that app exited during the reader's cleanup, so no later look found a holder, and a
    logs call found nobody on the pid, vouched for the lines and published them."""
    async def go():
        d = tuned(make())
        d.log_check = 0.02
        device = d.session.device
        stop, task = await started(d)
        await opened(d)
        ring = d._rings[APP]
        device.emit(42, BASE + 1, "the app's own line")
        await settle(lambda: len(ring.entries) == 1)
        assert "the app's own line" in (await d.run_tool("a", "logs", {}))["text"]
        seen: list[int] = []
        real = device.process_holder

        async def holder(pid):
            seen.append(pid)
            if len(seen) > 1:
                return await real(pid)
            await settle(lambda: len(ring.entries) == 2)  # the watcher's look finds the other app after its line was read
            found = await real(pid)
            device.holders.clear()  # and that app exits at once: every later look finds nobody on the pid
            return found
        device.process_holder = holder
        device.pids[APP] = []
        device.holders[42] = ("com.private.app", START + 60)
        device.emit(42, BASE + 2, "another app's secret")
        await settle(lambda: len(seen) >= 1 and 42 in device.stream_closed)
        await asyncio.sleep(0.15)  # the reader's cleanup and the watcher's last look are done
        for _ in range(2):
            shown = (await d.run_tool("a", "logs", {}))["text"]
            assert "secret" not in shown and f"Latest: {BASE + 1}" in shown
        assert not any("secret" in e.text for e in ring.entries)
        await finish(stop, task)
    run(go)


def test_a_step_with_no_boundary_and_no_phone_clock_shows_no_logs():
    async def go():
        d = tuned(make())
        d.log_warmup = 0.05
        device = d.session.device
        device.phone_ms = None  # the clock can't be read
        device.replay_delay = 0.2
        slow_down(d, 0.45)
        device.replay[42] = [(BASE - 5_000, 0x10, "an old error from before the batch", 42)]
        d.session.app = ("Example", APP)
        stop, task = await started(d)
        steps = [{"tool": "tap", "target": {"id": "ok-button"}, "expect": {"text": "Never there", "timeout": 0.5}}]
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": steps})
        assert "Logs (since" not in str(e.value) and "an old error" not in str(e.value)
        await finish(stop, task)
    run(go)


def test_an_unvouched_entry_is_kept_but_in_no_answer_until_it_is_vouched_for():
    ring = LogRing()
    ring.add(LogEntry(BASE, "info", "proven"))
    mark = ring.added
    ring.add(LogEntry(BASE + 1, "error", "unproven"), vouched=False)
    ring.add(LogEntry(BASE + 2, "error", "unproven too"))  # (after an unproven one, it is no more proven than that)
    assert [e.text for e in ring.select()] == ["proven"] and ring.latest(shown=True) == BASE and ring.latest() == BASE + 2
    assert diagnostics.logs_answer(ring, None, "info", None, 10)[0].endswith(f"Latest: {BASE}")
    ring.vouch(mark)  # vouching for what was added before `mark` changes nothing about what came after
    assert [e.text for e in ring.select()] == ["proven"]
    ring.vouch(ring.added)
    assert [e.text for e in ring.select()] == ["proven", "unproven", "unproven too"]
    ring.add(LogEntry(BASE + 3, "info", "x"), vouched=False)
    ring.discard_since(ring.added - 1)
    assert ring.unvouched == 0 and len(ring.entries) == 3

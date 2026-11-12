"""The action log (AGENT-TOOLS §12, v1.8)."""
import asyncio
import datetime
import json

import pytest

from tapwright.actionlog import ActionLog, target_of
from tapwright.control import Refused
from tapwright.ui import Target
from fakes import el
from test_daemon import finish, make, started

DAY = datetime.datetime(2026, 11, 12, 1, 30, 5, 123000, tzinfo=datetime.timezone.utc)


def lines(folder):
    return [json.loads(x) for f in sorted(folder.glob("actions-*.jsonl")) for x in f.read_text("utf-8").splitlines()]


def test_entries_go_to_the_day_file_with_their_time_first_and_no_empty_fields(tmp_path):
    log = ActionLog(tmp_path, now=lambda: DAY)
    log.write({"source": "tapwright", "by": "agent", "tool": "tap", "code": None, "ok": True})
    f = tmp_path / "actions-2026-11-12.jsonl"
    first = f.read_text("utf-8").splitlines()[0]
    assert first.startswith('{"t": "2026-11-12T01:30:05.123Z"')
    assert json.loads(first) == {"t": "2026-11-12T01:30:05.123Z", "source": "tapwright", "by": "agent", "tool": "tap",
                                 "ok": True}


def test_day_files_older_than_two_weeks_are_deleted_when_the_log_opens(tmp_path):
    for day in ("2026-10-28", "2026-10-29", "2026-11-11"):
        (tmp_path / f"actions-{day}.jsonl").write_text("{}\n", "utf-8")
    (tmp_path / "notes.txt").write_text("mine", "utf-8")
    ActionLog(tmp_path, now=lambda: DAY)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["actions-2026-10-29.jsonl", "actions-2026-11-11.jsonl",
                                                           "notes.txt"]


def test_off_writes_nothing_and_a_broken_log_is_reported_once(tmp_path):
    ActionLog(None).write({"tool": "tap"})
    blocker = tmp_path / "file"
    blocker.write_text("x", "utf-8")
    said = []
    log = ActionLog(blocker / "log", now=lambda: DAY, report=said.append)  # a folder inside a file can't be made
    log.write({"tool": "tap"})
    log.write({"tool": "tap"})
    assert len(said) == 1 and "can't be written" in said[0]


def test_a_target_is_logged_as_given():
    assert target_of(Target(id="save")) == {"id": "save"}
    assert target_of(Target(text="Save", index=1)) == {"text": "Save", "index": 1}
    assert target_of(Target(x=10.0, y=20.0)) == {"x": 10.0, "y": 20.0}
    assert target_of(None) is None


def run(go):
    asyncio.run(go())


def test_the_daemon_logs_agent_actions_and_never_the_typed_text(tmp_path):
    async def go():
        d = make(actions=ActionLog(tmp_path))
        stop, task = await started(d)
        d.session.tree = [el("e1", "button", "Save", "save"),
                          el("e2", "input", "Password", "pw", rect=(10, 200, 300, 44))]
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "tap", {"target": {"id": "save"}, "look": False})
        await d.run_tool("a", "type_text", {"target": {"id": "pw"}, "text": "hunter2", "look": False})
        with pytest.raises(Refused):
            await d.run_tool("a", "tap", {"target": {"id": "nothing-here"}, "look": False})
        await finish(stop, task)
        got = lines(tmp_path)
        assert [g["tool"] for g in got] == ["look", "tap", "type_text", "tap"]  # the look is a read: logged as one (§16.1)
        assert got[0]["kind"] == "read" and "kind" not in got[1]
        assert got[1]["by"] == "agent" and got[1]["ok"] is True and got[1]["target"] == {"id": "save"}
        assert got[2]["chars"] == 7 and "hunter2" not in json.dumps(got)
        assert got[3]["ok"] is False and got[3]["code"] == "not_found" and isinstance(got[3]["ms"], int)
        assert {g["client"] for g in got} == {"a"}
        assert all(isinstance(g["answer_chars"], int) and g["answer_chars"] > 0 for g in got)
        assert got[2]["chars"] != got[2]["answer_chars"]  # typed-text length and answer length are two fields
    run(go)


def test_the_daemon_logs_a_persons_input_from_the_live_view(tmp_path):
    async def go():
        d = make(actions=ActionLog(tmp_path))
        stop, task = await started(d)
        await d.viewer_message({"type": "gesture", "points": [[0, 0.5, 0.5], [60, 0.5, 0.5]]})
        await finish(stop, task)
        got = lines(tmp_path)
        assert got and got[-1]["by"] == "person" and got[-1]["source"] == "tapwright"
    run(go)


def test_every_read_is_logged_with_its_kind_and_size_but_no_text(tmp_path):
    async def go():
        d = make(actions=ActionLog(tmp_path))
        stop, task = await started(d)
        d.session.tree = [el("e1", "button", "Save", "save")]
        await d.run_tool("a", "look", {"screenshot": False, "labels": True})
        await d.run_tool("a", "apps", {})
        await d.run_tool("a", "wait_for", {"text": "Save", "look": False})
        await d.run_tool("a", "session_report", {})  # not logged itself
        await finish(stop, task)
        got = lines(tmp_path)
        assert [g["tool"] for g in got] == ["look", "apps", "wait_for"]
        assert all(g["kind"] == "read" and g["client"] == "a" and g["answer_chars"] > 0 for g in got)
        assert got[0]["labels"] is True
        assert "Save" not in json.dumps([{k: v for k, v in g.items() if k != "t"} for g in got])
    run(go)


def test_a_run_is_one_call_and_its_steps_say_which_run_they_belong_to(tmp_path):
    async def go():
        d = make(actions=ActionLog(tmp_path))
        stop, task = await started(d)
        d.session.tree = [el("e1", "button", "Save", "save")]
        await d.run_tool("a", "run", {"steps": [{"tool": "tap", "target": {"id": "save"}}]})
        await finish(stop, task)
        got = lines(tmp_path)
        assert [g["tool"] for g in got if not g.get("batch")] == ["run"]
        assert any(g["tool"] == "tap" and g.get("batch") for g in got)
    run(go)

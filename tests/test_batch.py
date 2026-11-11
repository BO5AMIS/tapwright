"""Batches and scripts (AGENT-TOOLS §9, v1.8)."""
import asyncio
import json

import pytest

from tapwright import batch
from tapwright.control import Refused
from fakes import el
from test_daemon import finish, make, started


def run(go):
    asyncio.run(go())


FORM = [el("e1", "input", "Email", "email", rect=(10, 100, 300, 44)),
        el("e2", "button", "Save", "save", rect=(10, 200, 100, 44))]
SAVED = el("e3", "text", "Saved", rect=(10, 300, 100, 20))


def test_steps_are_checked_before_anything_runs():
    for bad, words in (([], "non-empty list"), ([{"tool": "look"}], "can't be a step"),
                       ([{"tap": 1}], 'must be an object with "tool"'),
                       ([{"tool": "tap", "expect": {"text": "a", "id": "b"}}], "exactly one of text, id or label"),
                       ([{"tool": "tap", "expect": {"text": "a", "value": "x"}}], "value goes with id or label"),
                       ([{"tool": "tap", "expect": {"id": "a", "timeout": 99}}], "timeout must be from 0.5 to 30"),
                       ([{"tool": "tap"}] * 51, "at most 50 steps")):
        with pytest.raises(Refused) as e:
            batch.check_steps(bad)
        assert e.value.code == "bad_params" and words in str(e.value), (bad, str(e.value))
    with pytest.raises(Refused) as e:
        batch.check_steps([{"tool": "tap", "target": {"ref": "e3"}}], script=True)
    assert "can't use refs" in str(e.value)


def test_a_step_is_named_by_its_tool_and_target_as_written():
    assert batch.summary({"tool": "tap", "target": {"text": "Save"}}) == 'tap "Save"'
    assert batch.summary({"tool": "tap", "target": {"id": "save"}}) == "tap id=save"
    assert batch.summary({"tool": "type_text", "target": {"label": "Email"}, "text": "x"}) == 'type_text "Email"'
    assert batch.summary({"tool": "tap", "target": {"x": 10, "y": 20.5}}) == "tap (10, 20.5)"
    assert batch.summary({"tool": "scroll"}) == "scroll down"
    assert batch.summary({"tool": "wait", "seconds": 1.0}) == "wait 1.0"


def test_run_does_every_step_and_answers_once_with_the_whole_change():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.tree = list(FORM)
        await d.run_tool("a", "look", {"screenshot": False})
        d.session.tree = [*FORM, SAVED]  # what the steps lead to
        r = await d.run_tool("a", "run", {"steps": [
            {"tool": "type_text", "target": {"id": "email"}, "text": "a@b.co"},
            {"tool": "tap", "target": {"id": "save"}, "expect": {"text": "Saved"}}]})
        lines = r["text"].splitlines()
        assert lines[0].startswith("Ran 2 of 2 steps in ") and lines[0].endswith(" s.")
        assert any(line.startswith('+ e3 text "Saved"') for line in lines), r["text"]
        assert "actions ago" not in r["text"]
        await finish(stop, task)
    run(go)


def test_run_stops_at_a_missed_expectation_and_names_the_step():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.tree = list(FORM)
        await d.run_tool("a", "look", {"screenshot": False})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": [
                {"tool": "tap", "target": {"id": "save"}, "expect": {"text": "Saved", "timeout": 0.5}},
                {"tool": "tap", "target": {"id": "save"}}]})
        assert e.value.code == "expect_failed"
        assert str(e.value).splitlines()[0] == ('Ran 0 of 2 steps; stopped at step 1 (tap id=save): text "Saved" '
                                                "didn't appear within 0.5 s.")
        assert d.session.calls.count("tap") == 1  # step 2 never ran
        await finish(stop, task)
    run(go)


def test_run_keeps_a_failed_steps_own_code():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.tree = list(FORM)
        await d.run_tool("a", "look", {"screenshot": False})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": [{"tool": "tap", "target": {"id": "save"}},
                                                    {"tool": "tap", "target": {"id": "nope"}}]})
        assert e.value.code == "not_found"
        assert str(e.value).startswith("Ran 1 of 2 steps; stopped at step 2 (tap id=nope): nothing on screen has id")
        await finish(stop, task)
    run(go)


def test_an_expected_value_is_compared_and_a_switch_reads_on_or_off():
    async def go():
        d = make()
        stop, task = await started(d)
        switch = el("e4", "switch", "Alerts", "alerts", rect=(10, 400, 300, 44), checked=True)
        d.session.tree = [*FORM, switch]
        await d.run_tool("a", "look", {"screenshot": False})
        r = await d.run_tool("a", "run", {"steps": [{"tool": "wait", "seconds": 0.1,
                                                     "expect": {"id": "alerts", "value": "on"}}]})
        assert r["text"].startswith("Ran 1 of 1 steps")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": [{"tool": "wait", "seconds": 0.1,
                                                     "expect": {"id": "alerts", "value": "off", "timeout": 0.5}}]})
        assert e.value.code == "expect_failed" and 'didn\'t have the value "off" within 0.5 s.' in str(e.value)
        await finish(stop, task)
    run(go)


def test_trace_adds_each_steps_own_first_line():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.tree = list(FORM)
        await d.run_tool("a", "look", {"screenshot": False})
        r = await d.run_tool("a", "run", {"steps": [{"tool": "tap", "target": {"id": "save"}}], "trace": True})
        second = r["text"].splitlines()[1]
        assert second.startswith('  1. Tapped e2 button "Save" id=save') and second.endswith(" s)")
        await finish(stop, task)
    run(go)


def test_record_saves_successful_actions_by_id_then_replay_runs_them(tmp_path):
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.tree = list(FORM)
        await d.run_tool("a", "look", {"screenshot": False})
        r = await d.run_tool("a", "record", {"start": True, "name": "save it"})
        assert r["text"].startswith("Recording:")
        await d.run_tool("a", "tap", {"target": {"ref": "e2"}, "look": False})  # a ref is saved as the element's id
        with pytest.raises(Refused):
            await d.run_tool("a", "tap", {"target": {"id": "nope"}, "look": False})  # failed: not saved
        path = tmp_path / "s.json"
        r = await d.run_tool("a", "record", {"stop": True, "path": str(path)})
        assert r["text"] == f"Saved 1 steps to {path}."
        saved = json.loads(path.read_text("utf-8"))
        assert saved["format"] == "agent-tools-script" and saved["version"] == 1 and saved["name"] == "save it"
        assert saved["steps"] == [{"tool": "tap", "target": {"id": "save"}}]
        saved["fresh"] = False
        path.write_text(json.dumps(saved), "utf-8")
        taps = d.session.calls.count("tap")
        r = await d.run_tool("a", "replay", {"path": str(path)})
        assert r["text"].startswith("Ran 1 of 1 steps") and d.session.calls.count("tap") == taps + 1
        await finish(stop, task)
    run(go)


def test_a_bad_script_is_refused(tmp_path):
    path = tmp_path / "x.json"
    path.write_text('{"format": "something else"}', "utf-8")
    with pytest.raises(Refused) as e:
        batch.load_script(str(path))
    assert e.value.code == "bad_params" and "isn't an agent-tools script" in str(e.value)


def test_a_dry_run_batch_resolves_step_one_and_touches_nothing():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.tree = list(FORM)
        await d.run_tool("a", "look", {"screenshot": False})
        r = await d.run_tool("a", "run", {"dry_run": True, "steps": [{"tool": "tap", "target": {"id": "save"}},
                                                                     {"tool": "tap", "target": {"id": "nope"}}]})
        assert r["text"].startswith('Would run 2 steps; step 1 would tap e2 button "Save"'), r["text"]
        assert "tap" not in d.session.calls
        await finish(stop, task)
    run(go)


def test_a_fresh_replay_reopens_the_app_and_waits_for_what_step_one_aims_at(tmp_path):
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.tree = list(FORM)
        await d.run_tool("a", "look", {"screenshot": False})
        path = tmp_path / "s.json"
        path.write_text(json.dumps(batch.script("fresh", "com.example.app", [{"tool": "tap", "target": {"id": "save"}}])),
                        "utf-8")
        r = await d.run_tool("a", "replay", {"path": str(path)})
        assert r["text"].startswith("Ran 1 of 1 steps")
        calls = [c for c in d.session.calls if isinstance(c, str)]
        assert calls.index("terminate") < calls.index("launch") < len(calls) - 1 - calls[::-1].index("tap")
        await finish(stop, task)
    run(go)

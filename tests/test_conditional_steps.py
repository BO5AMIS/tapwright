"""`if` and `optional` on a step (AGENT-TOOLS 1.13 §9.1): one check, made once after the step before settled."""
import asyncio
import json

import pytest

from tapwright import batch
from tapwright.control import Refused
from fakes import button, el
from test_daemon import LongList, finish, make, started


def run(go):
    asyncio.run(go())


SAVE = button("e1", "Save", "save", rect=(10, 100, 100, 40))
NOT_NOW = button("e2", "Not now", "notnow", rect=(10, 200, 100, 40))
LOADING = el("e3", "text", "Loading", rect=(10, 300, 100, 20))


async def world(d, tree):
    stop, task = await started(d)
    d.session.tree = list(tree)
    await d.run_tool("a", "look", {"screenshot": False})
    return stop, task


def tap(target: dict, **more) -> dict:
    return {"tool": "tap", "target": target, **more}


# ---- checking the shapes ----

def test_good_conditions_pass_in_a_run_and_in_a_script():
    steps = [tap({"id": "save"}, **{"if": {"text": "Not now"}}),
             tap({"id": "save"}, **{"if": {"id": "notnow", "gone": True}}),
             tap({"label": "Save"}, **{"if": {"label": "Save", "gone": False}}),
             tap({"text": "Not now"}, optional=True),
             tap({"id": "x"}, optional=False),
             {"tool": "type_text", "text": "a", "target": {"label": "Email"}, "optional": True},
             {"tool": "wait", "seconds": 1, "if": {"text": "Loading"}}]
    assert batch.check_steps(steps) is steps
    assert batch.check_steps(steps, script=True) is steps


@pytest.mark.parametrize("step, words", [
    (tap({"id": "a"}, **{"if": "Not now"}), "if must be an object"),
    (tap({"id": "a"}, **{"if": {}}), "exactly one of text, id or label"),
    (tap({"id": "a"}, **{"if": {"text": "a", "id": "b"}}), "exactly one of text, id or label"),
    (tap({"id": "a"}, **{"if": {"text": ""}}), "non-empty string"),
    (tap({"id": "a"}, **{"if": {"text": 3}}), "non-empty string"),
    (tap({"id": "a"}, **{"if": {"text": "a", "gone": "yes"}}), "gone must be true or false"),
    (tap({"id": "a"}, **{"if": {"text": "a", "timeout": 5}}), "if takes only"),
    (tap({"id": "a"}, **{"if": {"text": "a", "value": "on"}}), "if takes only"),
    (tap({"id": "a"}, optional="yes"), "optional must be true or false"),
    ({"tool": "wait", "seconds": 1, "optional": True}, "optional goes with a step that has a target"),
    ({"tool": "back", "optional": True}, "optional goes with a step that has a target"),
    ({"tool": "type_text", "text": "a", "optional": True}, "optional goes with a step that has a target"),
    (tap({"x": 10, "y": 20}, optional=True), "optional needs a target by"),
])
def test_bad_shapes_are_bad_params_with_an_example(step, words):
    with pytest.raises(Refused) as e:
        batch.check_steps([{"tool": "wait", "seconds": 1}, step])
    assert e.value.code == "bad_params" and str(e.value).startswith("step 2: ") and words in str(e.value), str(e.value)
    assert '"if": {"text": "Not now"}' in str(e.value) or '"if": {"text": "Loading", "gone": true}' in str(e.value) \
        or '"optional": true' in str(e.value), "an example comes with it"


def test_a_script_file_may_carry_them_and_a_recording_never_writes_them(tmp_path):
    path = tmp_path / "s.json"
    steps = [tap({"id": "save"}, **{"if": {"text": "Not now"}}), tap({"text": "Not now"}, optional=True)]
    path.write_text(json.dumps(batch.script("x", "com.example.app", steps)), "utf-8")
    assert batch.load_script(str(path))["steps"] == steps
    path.write_text(json.dumps(batch.script("x", None, [tap({"id": "a"}, optional=True, **{"if": {"text": ""}})])), "utf-8")
    with pytest.raises(Refused):
        batch.load_script(str(path))


def test_a_conditional_step_still_gets_its_tools_arguments_checked():
    with pytest.raises(Refused) as e:
        batch.check_steps([tap({"id": "a"}, optional=True, labels=True)])
    assert "tap doesn't take labels" in str(e.value)
    assert "if" not in batch.args_of(tap({"id": "a"}, **{"if": {"text": "b"}}, optional=True))
    assert "optional" not in batch.args_of(tap({"id": "a"}, optional=True))


# ---- running them ----

def test_an_if_that_holds_runs_the_step_and_one_that_does_not_skips_it():
    async def go():
        d = make()
        stop, task = await world(d, [SAVE, NOT_NOW])
        r = await d.run_tool("a", "run", {"steps": [tap({"id": "save"}, **{"if": {"text": "Not now"}})]})
        assert r["text"].startswith("Ran 1 of 1 steps in ") and d.session.calls.count("tap") == 1
        assert "skipped" not in r["text"]
        d.session.tree = [SAVE]
        r = await d.run_tool("a", "run", {"steps": [tap({"id": "save"}, **{"if": {"text": "Not now"}})]})
        lines = r["text"].splitlines()
        assert lines[0].startswith("Ran 1 of 1 steps in ")  # a skipped step is done: the batch went through it
        assert lines[1] == '  1. skipped (if: "Not now" isn\'t on screen).'
        assert d.session.calls.count("tap") == 1  # not tapped again
        await finish(stop, task)
    run(go)


def test_if_matches_the_way_wait_for_does_by_text_id_and_label_with_the_whole_text_first():
    async def go():
        d = make()
        stop, task = await world(d, [SAVE, NOT_NOW, el("e4", "text", "Not now, maybe later", rect=(10, 400, 100, 20))])
        for cond in ({"text": "Not now"}, {"id": "notnow"}, {"label": "not now"}, {"text": "maybe"}):
            r = await d.run_tool("a", "run", {"steps": [{"tool": "wait", "seconds": 0.1, "if": cond}]})
            assert "skipped" not in r["text"], cond  # (two matches is a match, as for wait_for)
        for cond, shown in (({"id": "nope"}, "id=nope"), ({"label": "Not"}, '"Not"'), ({"text": "zzz"}, '"zzz"')):
            r = await d.run_tool("a", "run", {"steps": [{"tool": "wait", "seconds": 0.1, "if": cond}]})
            assert f"  1. skipped (if: {shown} isn't on screen)." in r["text"], cond
        await finish(stop, task)
    run(go)


def test_if_gone_runs_the_step_only_when_the_thing_is_not_on_screen():
    async def go():
        d = make()
        stop, task = await world(d, [SAVE, LOADING])
        step = tap({"id": "save"}, **{"if": {"text": "Loading", "gone": True}})
        r = await d.run_tool("a", "run", {"steps": [step]})
        assert '  1. skipped (if: "Loading" is on screen).' in r["text"] and d.session.calls.count("tap") == 0  # (1.13.1, item 11)
        d.session.tree = [SAVE]
        r = await d.run_tool("a", "run", {"steps": [step]})
        assert "skipped" not in r["text"] and d.session.calls.count("tap") == 1
        await finish(stop, task)
    run(go)


def test_optional_skips_a_step_whose_target_is_absent_and_runs_it_when_present():
    async def go():
        d = make()
        stop, task = await world(d, [SAVE])
        r = await d.run_tool("a", "run", {"steps": [tap({"text": "Not now"}, optional=True), tap({"id": "save"})]})
        lines = r["text"].splitlines()
        assert lines[0].startswith("Ran 2 of 2 steps in ") and lines[1] == '  1. skipped (optional: "Not now" isn\'t on screen).'
        assert d.session.calls.count("tap") == 1
        d.session.tree = [SAVE, NOT_NOW]
        r = await d.run_tool("a", "run", {"steps": [tap({"text": "Not now"}, optional=True)]})
        assert "skipped" not in r["text"] and d.session.calls.count("tap") == 2
        r = await d.run_tool("a", "run", {"steps": [tap({"id": "gone"}, optional=True), tap({"ref": "e9"}, optional=True)]})
        assert "  1. skipped (optional: id=gone isn't on screen)." in r["text"]
        assert "  2. skipped (optional: e9 isn't on screen)." in r["text"]
        await finish(stop, task)
    run(go)


def test_an_optional_element_below_the_fold_counts_as_there_and_the_step_scrolls_to_it():
    async def go():
        d = make()
        stop, task = await started(d)
        LongList(d)
        await d.run_tool("a", "look", {"screenshot": False})
        r = await d.run_tool("a", "run", {"steps": [tap({"id": "row-30"}, optional=True)], "trace": True})
        assert "skipped" not in r["text"] and "Scrolled to it, then tapped" in r["text"] and d.session.scrolls
        r = await d.run_tool("a", "run", {"steps": [{"tool": "wait", "seconds": 0.1, "if": {"id": "row-3"}},
                                                    {"tool": "wait", "seconds": 0.1, "if": {"id": "row-99"}}]})
        assert "  2. skipped (if: id=row-99 isn't on screen)." in r["text"] and "  1. skipped" not in r["text"]
        await finish(stop, task)
    run(go)


def test_a_skipped_steps_expect_is_not_checked_and_trace_lists_the_steps_in_order():
    async def go():
        d = make()
        stop, task = await world(d, [SAVE])
        steps = [tap({"id": "save"}), tap({"text": "Not now"}, optional=True, expect={"text": "Never", "timeout": 0.5}),
                 {"tool": "wait", "seconds": 0.1, "if": {"text": "Nope"}, "expect": {"text": "Never", "timeout": 0.5}}]
        r = await d.run_tool("a", "run", {"steps": steps, "trace": True})
        lines = r["text"].splitlines()
        assert lines[0].startswith("Ran 3 of 3 steps")
        assert lines[1].startswith('  1. Tapped e1 button "Save"') and lines[1].endswith(" s)")
        assert lines[2] == '  2. skipped (optional: "Not now" isn\'t on screen).'
        assert lines[3] == '  3. skipped (if: "Nope" isn\'t on screen).'
        await finish(stop, task)
    run(go)


def test_a_failure_after_a_skip_counts_the_skipped_step_as_run():
    async def go():
        d = make()
        stop, task = await world(d, [SAVE])
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": [tap({"text": "Not now"}, optional=True), tap({"id": "nope"})]})
        lines = str(e.value).splitlines()
        assert lines[0].startswith("Ran 1 of 2 steps; stopped at step 2 (tap id=nope)")
        assert '  1. skipped (optional: "Not now" isn\'t on screen).' in lines
        await finish(stop, task)
    run(go)


def test_the_check_reads_the_screen_once_and_waits_for_nothing():
    async def go():
        d = make()
        stop, task = await world(d, [SAVE])
        before = d.session.calls.count("refresh") + d.session.calls.count("snapshot")
        began = d.control.clock()
        await d.run_tool("a", "run", {"steps": [{"tool": "wait", "seconds": 0.1, "if": {"text": "Never"}}]})
        reads = d.session.calls.count("refresh") + d.session.calls.count("snapshot") - before
        assert d.control.clock() - began < 2.0 and reads <= 3  # the check, and the answer's own read
        await finish(stop, task)
    run(go)


def test_replay_runs_conditional_steps_from_a_script(tmp_path):
    async def go():
        d = make()
        stop, task = await world(d, [SAVE])
        path = tmp_path / "s.json"
        script = batch.script("x", None, [tap({"text": "Not now"}, optional=True), tap({"id": "save"}, **{"if": {"id": "save"}})])
        script["fresh"] = False
        path.write_text(json.dumps(script), "utf-8")
        r = await d.run_tool("a", "replay", {"path": str(path)})
        lines = r["text"].splitlines()
        assert lines[0].startswith("Ran 2 of 2 steps") and lines[1] == '  1. skipped (optional: "Not now" isn\'t on screen).'
        assert d.session.calls.count("tap") == 1
        await finish(stop, task)
    run(go)


# ---- the batch's 120 s, and the time spent waiting for a person (§9.3) ----

def test_a_batch_that_takes_over_120_s_times_out_and_held_time_does_not_count():
    async def go():
        d = make()
        stop, task = await world(d, [SAVE])
        steps = [{"tool": "wait", "seconds": 30, "look": False}] * 6
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": steps})
        assert e.value.code == "timeout" and "Ran 5 of 6 steps; stopped at step 6 (wait 30)" in str(e.value)
        real = d._wait

        async def person_waits(view, args, notes, shot):  # what a held wait for a person's OK does (§11.3)
            answer = await real(view, args, notes, shot)
            d.held(args["seconds"])
            return answer
        d._wait = person_waits
        r = await d.run_tool("a", "run", {"steps": steps})
        assert r["text"].startswith("Ran 6 of 6 steps in ")
        d.held(50)  # outside a batch: nothing to count
        await finish(stop, task)
    run(go)

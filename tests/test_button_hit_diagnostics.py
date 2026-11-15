"""Refused button probes retain bounded gate diagnostics without persisting UI content."""
import dataclasses
import json

import pytest

from tapwright import ui
from tapwright.actionlog import ActionLog
from tapwright.control import Refused
from test_171_button_content import setup
from test_daemon import finish, run, started
from test_review_17_c4_touches import real_dispatch


@pytest.mark.parametrize("condition,expected", [
    ("missing_app", "source_app_unknown"),
    ("different_app", "source_app_mismatch"),
    ("invalid", "invalid_source"),
    ("changed", "source_target_count"),
    ("disabled", "source_disabled"),
])
def test_source_diagnostics_distinguish_refusal_gates(condition, expected):
    d, target, child, direct = setup()
    original = d.session.last_source
    source = {
        "missing_app": original.replace(f' bundleId="{direct.bundle}"', ""),
        "different_app": original.replace(direct.bundle, "com.example.different"),
        "invalid": "<broken",
        "changed": original.replace('label="Account"', 'label="Changed"'),
        "disabled": original.replace('enabled="true" x="28"', 'enabled="false" x="28"'),
    }[condition]
    diagnostic = {}
    assert ui.button_background_points(source, target, child, direct.bundle, d.session.screen,
                                       diagnostic=diagnostic) == []
    assert diagnostic == {"recovery": expected}


def test_refused_probes_are_logged_without_a_sent_phase_or_private_hit_words(tmp_path):
    async def go():
        d, target, child, _ = setup()
        d.actions = ActionLog(tmp_path)
        # The private hit's words are deliberately incompatible. Diagnostics must contain only a gate code.
        private = "UNRELATED-SAVED-IDENTITY@example.invalid"
        d.session.center_answer = dataclasses.replace(child, name=private, label=private)
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            with pytest.raises(Refused) as error:
                await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False})
            assert not s.dispatched and s.hit_asks == [target.center]
            diagnostic = error.value.hit_check
            assert diagnostic == {"initial_relation": "unplaced", "relation": "unplaced",
                                  "recovery": "hit_label", "probes": 0}
            logs = "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("actions-*.jsonl"))
            entries = [json.loads(line) for line in logs.splitlines()]
            assert entries[-1]["hit_check"] == diagnostic
            assert private not in logs and "source" not in diagnostic
            assert "hit_check" not in entries[0]
        finally:
            await finish(stop, task)
    run(go)


def test_failed_candidates_record_counts_and_relations_not_payloads(tmp_path):
    async def go():
        d, _, child, _ = setup()
        d.actions = ActionLog(tmp_path)
        d.session.background_answer = child
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            with pytest.raises(Refused) as error:
                await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False})
            assert not s.dispatched
            detail = error.value.hit_check
            assert detail["recovery"] == "probing"
            assert 1 <= detail["probes"] == detail["candidates"] <= 4
            assert detail["probe_relation"] == "unplaced" and detail["probe_owner"] == "same"
            assert "Account" not in json.dumps(detail) and child.bundle not in json.dumps(detail)
        finally:
            await finish(stop, task)
    run(go)

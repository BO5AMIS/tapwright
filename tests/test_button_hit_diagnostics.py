"""Refused button probes retain bounded gate diagnostics without persisting UI content."""
import dataclasses
import json

import pytest

from tapwright import daemon as daemon_module, ui
from tapwright.actionlog import ActionLog
from tapwright.control import Refused
from test_171_button_content import SHAPES, setup
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


@pytest.mark.parametrize("shape", [SHAPES[1], SHAPES[-1]], ids=["Account", "Continue"])
def test_failed_candidates_record_counts_and_relations_not_payloads(tmp_path, shape):
    async def go():
        d, _, child, _ = setup(shape)
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


@pytest.mark.parametrize("grant_note,crash_note", [(True, False), (False, True), (True, True)])
@pytest.mark.parametrize("draft", [False, True])
@pytest.mark.parametrize("code", ["not_actionable", "dry_run"])
def test_refusal_notes_preserve_diagnostics_and_draft_in_dispatch_and_log(
        tmp_path, monkeypatch, grant_note, crash_note, draft, code):
    async def go():
        d, _, _, _ = setup()
        d.actions = ActionLog(tmp_path)
        refusal = Refused(code, "synthetic refusal", draft=draft)
        refusal.hit_check = {"gate": "foreign_recipient", "owner": "foreign", "probes": 0}

        async def refuse(*args):
            if grant_note:
                daemon_module._GRANT_CALL.get().notes.append("Grant did not cover this action.")
            raise refusal

        async def crash(*args):
            return "Synthetic crash note." if crash_note else None

        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            monkeypatch.setattr(d, "_run_tool", refuse)
            monkeypatch.setattr(d, "_crash_note", crash)
            with pytest.raises(Refused) as error:
                await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False,
                                              "dry_run": code == "dry_run"})
            assert error.value.code == code and error.value.draft is draft
            assert error.value.hit_check == refusal.hit_check
            expected = ["synthetic refusal"]
            if code != "dry_run":
                if grant_note:
                    expected.append("Grant did not cover this action.")
                if crash_note:
                    expected.append("Note: Synthetic crash note.")
            assert str(error.value) == "\n".join(expected)
            logs = "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("actions-*.jsonl"))
            assert json.loads(logs.splitlines()[-1])["hit_check"] == refusal.hit_check
            assert "tap" not in d.session.calls
        finally:
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("contains_point", [False, True])
def test_foreign_refusal_has_sanitized_gate_and_point_diagnostics(tmp_path, contains_point):
    async def go():
        d, target, passive, _ = setup()
        d.actions = ActionLog(tmp_path)
        private = "UNRELATED-SAVED-IDENTITY@example.invalid"
        rect = passive.rect if contains_point else (87, 93, 272, 23)
        d.session.center_answer = dataclasses.replace(passive, name=private, label=private,
                                                      bundle="com.private.other", rect=rect)
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            with pytest.raises(Refused) as error:
                await d.run_tool("a", "tap", {"target": {"ref": target.ref}, "dry_run": True,
                                                "screenshot": False})
            assert not s.dispatched
            detail = error.value.hit_check
            assert detail["gate"] == "foreign_recipient" and detail["owner"] == "foreign"
            assert detail["point"] == "center" and detail["hit_contains_point"] is contains_point
            assert detail["probes"] == 0
            logs = "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("actions-*.jsonl"))
            assert json.loads(logs.splitlines()[-1])["hit_check"] == detail
            assert private not in logs and "com.private.other" not in logs
        finally:
            await finish(stop, task)
    run(go)

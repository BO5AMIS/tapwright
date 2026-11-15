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

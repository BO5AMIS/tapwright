"""`tapwright replay --repair suggest|apply [--adopt]` (AGENT-TOOLS §26.5): adopting is the command line's and a person's, and
acts only after the run that wrote the repaired script passed."""
import argparse
import json
from xml.etree import ElementTree as ET

from tapwright import auditlog, cli, client, schedules, store


def files(tmp_path, original='{"format": "agent-tools-script", "version": 1, "name": "sign_in", "app": "com.example.app", "steps": [{"tool": "tap", "target": {"id": "old"}}]}',
          repaired='{"format": "agent-tools-script", "version": 1, "name": "sign_in", "app": "com.example.app", "steps": [{"tool": "tap", "target": {"id": "new"}}]}'):
    script = tmp_path / "sign_in.json"
    script.write_text(original, "utf-8")
    (tmp_path / "sign_in.repaired.json").write_text(repaired, "utf-8")
    (tmp_path / "sign_in.repair.md").write_text("# repairs", "utf-8")
    return script


def ns(script, **kw):
    return argparse.Namespace(files=[str(script)], junit=kw.pop("junit", None), apps="com.example.app", dedicated=False,
                              repair=kw.pop("repair", None), adopt=kw.pop("adopt", False), **kw)


PASSED = {"text": "Ran 1 of 1 steps in 1.0 s, with 1 repair (not adopted).\nRepaired: step 1 tap id=old to id=new (score 1.00).\n"
                  "Wrote sign_in.repaired.json and sign_in.repair.md. The original is unchanged."}


def patched(monkeypatch, tmp_path, answer=PASSED):
    calls: list[dict] = []

    def call(tool, args, client_id, timeout=120.0):
        calls.append({"tool": tool, **args})
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(client, "call", call)
    monkeypatch.setattr(client, "set_scope", lambda apps, dedicated=False: None)
    monkeypatch.setattr(store, "HOME", tmp_path / "home")
    return calls


def audit(tmp_path):
    return [json.loads(line) for line in auditlog.read_lines(tmp_path / "home" / "audit")]


def test_repair_is_passed_to_the_replay_and_nothing_is_adopted_without_adopt(tmp_path, monkeypatch, capsys):
    calls = patched(monkeypatch, tmp_path)
    script = files(tmp_path)
    assert cli.replay(ns(script, repair="apply")) == 0
    assert calls == [{"tool": "replay", "path": str(script), "repair": "apply"}]
    assert json.loads(script.read_text("utf-8"))["steps"][0]["target"] == {"id": "old"}  # the original is unchanged
    assert (tmp_path / "sign_in.repaired.json").exists() and not (tmp_path / "sign_in.orig.json").exists()
    assert "Wrote sign_in.repaired.json" in capsys.readouterr().out and audit(tmp_path) == []


def test_adopt_replaces_the_script_keeps_the_original_once_and_logs_it(tmp_path, monkeypatch, capsys):
    patched(monkeypatch, tmp_path)
    script = files(tmp_path)
    assert cli.replay(ns(script, repair="apply", adopt=True)) == 0
    assert json.loads(script.read_text("utf-8"))["steps"][0]["target"] == {"id": "new"}
    assert json.loads((tmp_path / "sign_in.orig.json").read_text("utf-8"))["steps"][0]["target"] == {"id": "old"}
    assert not (tmp_path / "sign_in.repaired.json").exists() and (tmp_path / "sign_in.repair.md").exists()
    [line] = audit(tmp_path)
    assert (line["kind"], line["name"], line["app"], line["detail"], line["tool"]) == ("repair", "sign_in", "com.example.app", "adopted", "tapwright")
    assert "Adopted sign_in.repaired.json" in capsys.readouterr().out
    # a second repair adopted later: the first original is never lost
    (tmp_path / "sign_in.repaired.json").write_text(script.read_text("utf-8").replace('"new"', '"newer"'), "utf-8")
    assert cli.replay(ns(script, repair="apply", adopt=True)) == 0
    assert json.loads((tmp_path / "sign_in.orig.json").read_text("utf-8"))["steps"][0]["target"] == {"id": "old"}
    assert json.loads(script.read_text("utf-8"))["steps"][0]["target"] == {"id": "newer"}


def test_adopt_acts_only_after_the_run_that_passed(tmp_path, monkeypatch, capsys):
    patched(monkeypatch, tmp_path, client.DaemonError("not_found", "Ran 0 of 1 steps; stopped at step 1 (tap id=old): nothing"))
    script = files(tmp_path)
    assert cli.replay(ns(script, repair="apply", adopt=True)) == 1  # a failed apply
    assert json.loads(script.read_text("utf-8"))["steps"][0]["target"] == {"id": "old"} and not (tmp_path / "sign_in.orig.json").exists()
    assert audit(tmp_path) == []
    # a run that passed with no repair wrote no file: an older .repaired.json lying there is not adopted
    patched(monkeypatch, tmp_path, {"text": "Ran 1 of 1 steps in 1.0 s."})
    assert cli.replay(ns(script, repair="apply", adopt=True)) == 0
    assert "nothing to adopt" in capsys.readouterr().out and json.loads(script.read_text("utf-8"))["steps"][0]["target"] == {"id": "old"}


def test_adopt_needs_the_audit_line(tmp_path, monkeypatch, capsys):
    patched(monkeypatch, tmp_path)
    script = files(tmp_path)
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "audit").write_text("a file where the log's folder should be", "utf-8")
    assert cli.replay(ns(script, repair="apply", adopt=True)) == 0
    assert "audit log couldn't be written" in capsys.readouterr().out
    assert json.loads(script.read_text("utf-8"))["steps"][0]["target"] == {"id": "old"} and (tmp_path / "sign_in.repaired.json").exists()


def test_the_flags_are_checked_before_the_phone_is_asked(tmp_path, monkeypatch, capsys):
    calls = patched(monkeypatch, tmp_path)
    script = files(tmp_path)
    for kw in ({"adopt": True}, {"adopt": True, "repair": "suggest"}):
        assert cli.replay(ns(script, **kw)) == 2
    assert "--adopt goes with --repair apply" in capsys.readouterr().out
    assert cli.replay(ns(script, repair="apply", update_references=True)) == 2
    assert calls == []


def test_the_parser_takes_repair_and_adopt_and_only_those_values():
    import pytest
    import sys
    from unittest import mock
    with mock.patch.object(sys, "argv", ["tapwright", "replay", "x", "--apps", "a", "--repair", "apply", "--adopt"]), \
            mock.patch.object(cli, "replay", lambda a: 0 if (a.repair, a.adopt) == ("apply", True) else 9):
        assert cli.main() == 0
    with mock.patch.object(sys, "argv", ["tapwright", "replay", "x", "--apps", "a", "--repair", "fix"]), pytest.raises(SystemExit):
        cli.main()


def test_suggest_with_junit_puts_the_suggestion_into_the_failure_message(tmp_path, monkeypatch):
    text = ("Ran 0 of 1 steps; stopped at step 1 (tap id=login-btn): nothing on screen has id \"login-btn\".\n"
            "Repair (suggest, not checked):\n1. tap id=login-button, button \"Log in\": score 1.00 (id_stem 1.00). Why: close.\n"
            'To check it: replay with repair: "apply".')
    patched(monkeypatch, tmp_path, client.DaemonError("not_found", text))
    script = files(tmp_path)
    out = tmp_path / "junit.xml"
    assert cli.replay(ns(script, repair="suggest", junit=str(out))) == 1
    failure = ET.parse(out).getroot().find("testcase/failure")
    assert failure is not None and "Repair (suggest, not checked):" in failure.get("message") and "login-button" in failure.get("message")


def test_a_schedule_file_has_no_repair_key(tmp_path):
    text = json.dumps({"version": 1, "tool": "tapwright", "route": "sign_in", "at": "09:00 daily", "repair": "apply"})
    parsed = schedules.parse("nightly", text)
    assert not hasattr(parsed, "repair") and "repair" not in json.dumps(parsed.to_json())  # ignored, never passed on

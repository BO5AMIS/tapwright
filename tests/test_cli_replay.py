"""`tapwright replay FILE…` (AGENT-TOOLS §9.4): exit codes, JUnit and the scope."""
import argparse
import json
from xml.etree import ElementTree as ET

from tapwright import cli, client


def script(tmp_path, name, app="com.example.app"):
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps({"format": "agent-tools-script", "version": 1, "name": name, "app": app,
                                "steps": [{"tool": "tap", "target": {"id": "save"}}]}), "utf-8")
    return str(path)


def test_replay_runs_each_script_and_writes_junit(tmp_path, monkeypatch, capsys):
    scopes, answers = [], {
        "ok": {"text": "Ran 1 of 1 steps in 0.7 s."},
        "stops": client.DaemonError("expect_failed", 'Ran 0 of 1 steps; stopped at step 1 (tap id=save): text "x" '
                                                     "didn't appear within 2 s."),
    }

    def call(tool, args, client_id, timeout=120.0):
        assert tool == "replay"
        answer = answers["stops" if "stops" in args["path"] else "ok"]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(client, "call", call)
    monkeypatch.setattr(client, "set_scope", lambda apps, dedicated=False: scopes.append((apps, dedicated)))
    out = tmp_path / "junit.xml"
    a = argparse.Namespace(files=[script(tmp_path, "ok"), script(tmp_path, "stops")], junit=str(out),
                           apps="com.example.app", dedicated=False)
    assert cli.replay(a) == 1
    assert scopes == [(["com.example.app"], False)] * 2  # the scope the command line gave
    printed = capsys.readouterr().out
    assert "ok.json: Ran 1 of 1 steps" in printed and "stops.json: Error expect_failed: Ran 0 of 1" in printed
    suite = ET.parse(out).getroot()
    assert suite.get("tests") == "2" and suite.get("failures") == "1" and suite.get("errors") == "0"
    failure = suite.find("testcase[@name='stops']/failure")
    assert failure is not None and failure.get("message").startswith("expect_failed: Ran 0 of 1 steps")


def test_a_scripts_own_app_never_widens_the_scope(tmp_path, monkeypatch):
    scopes: list[tuple[list[str], bool]] = []
    monkeypatch.setattr(client, "call", lambda tool, args, client_id, timeout=120.0: {"text": "Ran 1 of 1 steps."})
    monkeypatch.setattr(client, "set_scope", lambda apps, dedicated=False: scopes.append((apps, dedicated)))
    a = argparse.Namespace(files=[script(tmp_path, "evil", app="com.bank.app")], junit=None,
                           apps="com.example.a, com.example.b", dedicated=False)
    assert cli.replay(a) == 0
    assert scopes == [(["com.example.a", "com.example.b"], False)]


def test_dedicated_is_the_other_way_to_give_a_scope(tmp_path, monkeypatch):
    scopes: list[tuple[list[str], bool]] = []
    monkeypatch.setattr(client, "call", lambda tool, args, client_id, timeout=120.0: {"text": "ok"})
    monkeypatch.setattr(client, "set_scope", lambda apps, dedicated=False: scopes.append((apps, dedicated)))
    a = argparse.Namespace(files=[script(tmp_path, "ok")], junit=None, apps=None, dedicated=True)
    assert cli.replay(a) == 0 and scopes == [([], True)]


def test_without_apps_or_dedicated_it_refuses_with_2(tmp_path, monkeypatch, capsys):
    called: list[str] = []
    monkeypatch.setattr(client, "call", lambda *args, **kw: called.append("call"))
    monkeypatch.setattr(client, "set_scope", lambda apps, dedicated=False: called.append("scope"))
    a = argparse.Namespace(files=[script(tmp_path, "ok")], junit=None, apps=None, dedicated=False)
    assert cli.replay(a) == 2
    printed = capsys.readouterr().out
    assert "--apps <bundle id>" in printed and "or --dedicated" in printed
    assert called == []  # nothing reached the daemon
    both = argparse.Namespace(files=[script(tmp_path, "ok")], junit=None, apps="a", dedicated=True)
    assert cli.replay(both) == 2 and called == []


def test_a_script_that_cant_start_exits_2(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", "utf-8")
    monkeypatch.setattr(client, "set_scope", lambda apps, dedicated=False: None)
    a = argparse.Namespace(files=[str(bad)], junit=None, apps="com.example.app", dedicated=False)
    assert cli.replay(a) == 2

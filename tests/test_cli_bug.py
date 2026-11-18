"""`tapwright bug` (AGENT-TOOLS 1.16.2 §29.3): a bug from an explore report's finding with no phone, `bug list`, and
`verify`/`export` as part 2's (unsupported, exit 2). The tool's home is a temporary folder: never the real ~/.tapwright."""
import argparse
import json

import pytest
from test_bugstore import APP, SECRET, write_run

from tapwright import cli, store


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "HOME", tmp_path / "home")
    return tmp_path / "home"


def args(*what, **over):
    base = dict(what=list(what), with_picture=False, verify=None, app=None, accounts=None, maps=None)
    base.update(over)
    return argparse.Namespace(**base)


def test_bug_from_a_report_folder_then_the_list(home, tmp_path):
    folder = write_run(home / "explore", got=f"the app crashed: {SECRET}")
    vault = tmp_path / "vault.json"
    vault.write_text(json.dumps({"version": 1, "accounts": {"tester": {"username": "tester@example.com", "password": SECRET}}}),
                     encoding="utf-8")
    said: list[str] = []
    code = cli.bug_cmd(args(str(folder), "f3", accounts=str(vault)), out=said.append)
    assert code == 0, said
    lines = said[0].splitlines()
    assert lines[0].startswith("New bug: Crash on place_4: the app crashed: <tester's password>")
    assert lines[1] == "Reproduced: not checked (the reproduction check is part of 1.8.5, not built yet)."
    bugs = home / "bugs" / APP
    assert len(list(bugs.iterdir())) == 1
    assert SECRET not in "".join(p.read_text("utf-8", "replace") for p in bugs.rglob("*") if p.is_file())
    said.clear()
    assert cli.bug_cmd(args("list"), out=said.append) == 0
    assert said[0].splitlines()[0] == APP and "seen 1, last 2026-11-14, not checked" in said[0]
    said.clear()
    assert cli.bug_cmd(args("list", app="com.none"), out=said.append) == 0 and said == ["No bugs are kept."]


def test_bug_says_what_it_cant_do_yet_and_what_is_wrong(home):
    folder = write_run(home / "explore")
    said: list[str] = []
    assert cli.bug_cmd(args("verify", "bug_0123456789abcdef"), out=said.append) == 2
    assert cli.bug_cmd(args("export", "bug_0123456789abcdef"), out=said.append) == 2
    assert cli.bug_cmd(args(str(folder), "f3", verify=3), out=said.append) == 2
    assert all("part of 1.8.5, not built yet" in s for s in said)
    said.clear()
    assert cli.bug_cmd(args(str(folder), "f9"), out=said.append) == 2 and said[0].startswith("Error not_found")
    assert cli.bug_cmd(args(str(folder)), out=said.append) == 2
    assert not (home / "bugs").exists()


def test_the_parser_knows_bug(home, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "bug_cmd", lambda a: seen.setdefault("a", a) and 0)
    monkeypatch.setattr("sys.argv", ["tapwright", "bug", "list", "--app", APP])
    cli.main()
    assert seen["a"].what == ["list"] and seen["a"].app == APP

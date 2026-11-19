"""`tapwright bug` (AGENT-TOOLS 1.16.2 §29.3): a bug from an explore report's finding with no phone, `bug list`, and
`bug verify` through the daemon (a fake call) with the exit codes, and `bug export`. The tool's home is a temporary folder:
never the real ~/.tapwright; no network."""
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
    base = dict(what=list(what), with_picture=False, verify=None, app=None, accounts=None, maps=None, to=None, repo=None,
                public=False, yes=False)
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
    assert lines[1] == "Reproduced: not checked (not asked: tapwright bug verify <bug id> checks it on the phone)."
    bugs = home / "bugs" / APP
    assert len(list(bugs.iterdir())) == 1
    assert SECRET not in "".join(p.read_text("utf-8", "replace") for p in bugs.rglob("*") if p.is_file())
    said.clear()
    assert cli.bug_cmd(args("list"), out=said.append) == 0
    assert said[0].splitlines()[0] == APP and "seen 1, last 2026-11-14, not checked" in said[0]
    said.clear()
    assert cli.bug_cmd(args("list", app="com.none"), out=said.append) == 0 and said == ["No bugs are kept."]


class Call:
    def __init__(self, text="", error=None):
        self.calls: list = []
        self.text, self.error = text, error

    def __call__(self, tool, a, client_id, timeout=120.0):
        self.calls.append((tool, a, timeout))
        if self.error is not None:
            raise self.error
        return {"text": self.text}


def test_bug_verify_goes_to_the_daemon_and_its_exit_code_says_what_happened(home):
    folder = write_run(home / "explore")
    for line, code in (("Reproduced: 3 of 3 runs.", 0), ("Reproduced: 1 of 3 runs (flaky).", 0),
                       ("Reproduced: 0 of 3 runs: not reproduced.", 1),
                       ('Reproduced: not checked (step 2 needs a person (a typed value)).', 2),
                       ("Reproduced: not checked (stopped (busy)).", 2),
                       ("Reproduced: not checked (an unreachable place has no steps).", 0)):
        call = Call(f"Known bug (seen 1 time): x\n{line}\nSeen: 1 time\nFolder: ~/x")
        said: list[str] = []
        assert cli.bug_cmd(args("verify", "bug_0123456789abcdef"), out=said.append, call=call) == code, line
        assert call.calls[0][:2] == ("bug_report", {"bug": "bug_0123456789abcdef", "verify": 3})
    call = Call("New bug: x\nReproduced: 2 of 2 runs.")
    said = []
    assert cli.bug_cmd(args(str(folder), "f3", verify=2), out=said.append, call=call) == 0
    assert call.calls[0][1]["verify"] == 2 and call.calls[0][1]["bug"].startswith("bug_")
    from tapwright import client
    assert cli.bug_cmd(args("verify", "bug_x"), out=said.append, call=Call(error=client.DaemonError("busy", "x"))) == 2
    assert cli.bug_cmd(args("verify", "bug_x", verify=11), out=said.append, call=Call()) == 2


def test_bug_says_what_is_wrong(home):
    folder = write_run(home / "explore")
    said: list[str] = []
    assert cli.bug_cmd(args(str(folder), "f9"), out=said.append) == 2 and said[0].startswith("Error not_found")
    assert cli.bug_cmd(args(str(folder)), out=said.append) == 2
    assert cli.bug_cmd(args("export", "bug_0123456789abcdef", to="zip"), out=said.append) == 2
    assert "Error not_found" in said[-1]
    assert not (home / "bugs").exists()


def test_bug_export_writes_a_zip_and_asks_before_github(home, monkeypatch, tmp_path):
    from test_journey import FakeGh

    from tapwright import auditlog, journey_export
    folder = write_run(home / "explore")
    vault = tmp_path / "vault.json"
    vault.write_text(json.dumps({"version": 1, "accounts": {"tester": {"username": "t@example.com", "password": SECRET}}}),
                     encoding="utf-8")
    said: list[str] = []
    assert cli.bug_cmd(args(str(folder), "f3"), out=said.append, alive=lambda: False) == 0
    assert said[-1].splitlines()[-1] == f"Checked for: {cli.BUG_VALUES_NONE}."
    bug_id = next(ln for ln in said[-1].splitlines() if ln.startswith("Folder: ")).replace("\\", "/").rsplit("/", 1)[-1]
    gh = FakeGh({})
    monkeypatch.setattr(journey_export, "run_gh", gh)
    # (made with no daemon: no app is one of its test apps, so a zip needs a yes too: review R185 H2)
    assert cli.bug_cmd(args("export", bug_id, to="zip"), out=said.append, ask=lambda q: "n", alive=lambda: False) == 2
    assert "needs_confirmation" in said[-1] and "test apps" in said[-1]
    assert not (home / "bugs" / APP / f"{bug_id}.zip").exists()
    assert cli.bug_cmd(args("export", bug_id, to="zip", yes=True), out=said.append, alive=lambda: False) == 0
    assert (home / "bugs" / APP / f"{bug_id}.zip").exists() and not gh.calls
    assert f"Checked for: {cli.BUG_VALUES_NONE}." in said[-1]
    # (no daemon and no --accounts: nothing could check the values, so GitHub is refused: review R185 M3)
    assert cli.bug_cmd(args("export", bug_id, to="issue", repo="o/r", yes=True), out=said.append, alive=lambda: False) == 2
    assert said[-1] == cli.BUG_ISSUE_NEEDS_VALUES and not gh.calls
    assert cli.bug_cmd(args("export", bug_id, to="issue", repo="o/r", accounts=str(vault)), out=said.append,
                       ask=lambda q: "n", alive=lambda: False) == 2
    assert "needs_confirmation" in said[-1] and "--yes" in said[-1] and not gh.calls
    assert cli.bug_cmd(args("export", bug_id), out=said.append, alive=lambda: False) == 2  # --to is needed
    kinds = [json.loads(x) for x in auditlog.read_lines(home / "audit")]
    assert [x["kind"] for x in kinds] == ["export"] * 4
    assert kinds[0]["detail"].endswith("refused (needs confirmation)")
    assert kinds[1]["detail"].endswith(f"picture not sent; {cli.BUG_VALUES_NONE}")
    assert kinds[2]["detail"].endswith("refused (no values to check)")
    assert kinds[3]["detail"].endswith("refused (needs confirmation)")


def test_with_a_daemon_running_the_make_and_the_export_go_through_it(home):
    """Review R185 M3: the daemon's live values (the session's typed values, the codes) hold for the command line too."""
    folder = write_run(home / "explore")
    call = Call(text="New bug: x\nReproduced: not checked (not asked).\nSeen: 1 time.\nFolder: ~/b/bug_0123456789abcdef")
    said: list[str] = []
    assert cli.bug_cmd(args(str(folder), "f3"), out=said.append, call=call, alive=lambda: True) == 0
    assert call.calls[0][0] == "bug_report" and call.calls[0][1]["finding"] == "f3"
    assert call.calls[0][1]["run"] == str(folder) and call.calls[0][1]["verify"] is False
    assert not (home / "bugs").exists()  # (nothing made here: the daemon makes it)
    made = cli.bug_cmd(args(str(folder), "f3"), out=said.append, alive=lambda: False)
    assert made == 0
    bug_id = next(ln for ln in said[-1].splitlines() if ln.startswith("Folder: ")).replace("\\", "/").rsplit("/", 1)[-1]
    assert cli.bug_cmd(args("export", bug_id, to="issue", repo="o/r", yes=True), out=said.append, call=call,
                       alive=lambda: True) == 0
    assert call.calls[-1][1] == {"bug": bug_id, "export": {"to": "issue", "repo": "o/r", "public": False, "confirm": True}}


def test_the_parser_knows_bug(home, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "bug_cmd", lambda a: seen.setdefault("a", a) and 0)
    monkeypatch.setattr("sys.argv", ["tapwright", "bug", "list", "--app", APP])
    cli.main()
    assert seen["a"].what == ["list"] and seen["a"].app == APP

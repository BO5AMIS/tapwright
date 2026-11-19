"""W177 round 2 (the 1.8.5 recheck): a bug from a failed check names the place the run's answer named and keeps only the
app's own log lines; a `from: "now"` bug made after an explore names the screen the newest read shows."""
import json

import pytest
from test_bug_w177 import FAILING, call, home, serving  # noqa: F401 - (home is a fixture)
from test_daemon import finish, run, started
from test_daemon_bug import bugged, folder_of
from test_daemon_explore import BUNDLE

from tapwright import diagnostics
from tapwright.actionlog import ActionLog


def place_line(page: str) -> str:
    return next(line for line in page.splitlines() if line.startswith("- Place: "))


def test_from_now_after_an_explore_names_the_screen_the_newest_read_shows(tmp_path):
    async def go():
        d = bugged(tmp_path, actions=ActionLog(tmp_path / "log"))
        stop, task = await started(d)
        await d.run_tool("a", "look", {})
        await d.run_tool("a", "tap", {"target": {"id": "go-settings"}})
        r = await d.run_tool("a", "explore", {"minutes": 1})
        assert "Explored" in r["text"]
        from tapwright import places
        d.session.at = "form"  # (where the phone is now, whatever the explore left)
        want = places.read_place(d.session.screens["form"]).key["signature"]
        made = await d.run_tool("a", "bug_report", {"from": "now", "note": "the form looks wrong"})
        meta = json.loads((folder_of(made, d.bug_home) / "finding.json").read_text("utf-8"))
        assert meta["bug"]["place_key"] == {"signature": want}, (meta["place"], meta["bug"]["place_key"])
        await finish(stop, task)
    run(go)


def test_a_thin_failure_names_the_place_the_runs_answer_named_and_keeps_only_the_apps_lines(tmp_path, home, capsys):
    """The recheck: `tapwright call run` answered `Place: place_31`, then the bug said "Check failed on an unnamed screen"
    and its Logs were iOS's lines ("Did post notification"), not the app's."""
    async def go():
        d = bugged(tmp_path)
        stop, task = await started(d)
        ring = diagnostics.LogRing()
        ring.add(diagnostics.LogEntry(1_700_000_000_000, "warning", "Did post notification. notification: (4002)", False))
        ring.add(diagnostics.LogEntry(1_700_000_000_001, "error", "BUGLAB save failed", True))
        d._rings[BUNDLE] = ring
        server, serve_task, sock = await serving(d, home)
        try:
            # (CodeRabbit, PR #21) the run's destination is a known place first: seen once, then back
            assert (await call(capsys, "look", "{}", "--client", "6101"))[0] == 0
            assert (await call(capsys, "tap", json.dumps({"target": FAILING[0]["target"]}), "--client", "6101"))[0] == 0
            assert (await call(capsys, "back", "{}", "--client", "6101"))[0] == 0
            assert (await call(capsys, "look", "{}", "--client", "6101"))[0] == 0
            code, out, err = await call(capsys, "run", json.dumps({"steps": FAILING}), "--client", "6102")
            assert code == 2 and "expect_failed" in err
            named = next((x for x in err.splitlines() if x.startswith("Place: ")), None)
            code, out, err = await call(capsys, "bug_report", '{"from": "failure", "verify": false}', "--client", "6103")
            assert code == 0, err
            assert "unnamed screen" not in out.splitlines()[0], out
            # (CodeRabbit, PR #21) the failed run names its place, and the bug's title names that very place
            assert named is not None and named.startswith("Place: place_"), err
            assert f"on {named.removeprefix('Place: ').rstrip('.')}:" in out.splitlines()[0], (named, out)
            folder = folder_of({"text": out}, d.bug_home)
            page = (folder / "report.md").read_text("utf-8")
            assert "its key isn't known" not in page, page
            logs = page.split("## Logs", 1)[1].split("## Picture", 1)[0]
            assert "BUGLAB save failed" in logs and "Did post notification" not in logs and "Logs (since" not in logs, logs
        finally:
            server.should_exit = True
            await serve_task
            sock.close()
            await finish(stop, task)
    run(go)

"""The `bug_report` tool end to end on the fake daemon (AGENT-TOOLS 1.16.2 §29.3): from an explore report's finding, from the
last touches (`from: "now"`), from the last failed check, a kept bug again; the arguments; and that a typed value and a
vault value reach no file of the bug and no answer (§29.6). No phone, no real ~/.tapwright."""
import json
from pathlib import Path

import pytest
from test_bugstore import APP as RUN_APP, write_run
from test_daemon import finish, run, started
from test_daemon_explore import BUNDLE, explored

from tapwright.actionlog import ActionLog
from tapwright.control import Refused

TYPED = "Margaret-Hamilton-1969"


def bugged(tmp_path, **options):
    d = explored(tmp_path, **options)
    d.bug_home = tmp_path / "bugs"
    return d


def files_text(root: Path) -> str:
    return "\n".join(p.read_bytes().decode("utf-8", "replace") for p in root.rglob("*") if p.is_file())


def test_from_now_writes_the_last_touches_and_never_the_typed_value(tmp_path):
    async def go():
        d = bugged(tmp_path, actions=ActionLog(tmp_path / "log"))
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "bug_report", {"from": "now", "note": "looks wrong"})
        assert e.value.code == "not_found" and "no touch" in str(e.value)
        await d.run_tool("a", "look", {})
        await d.run_tool("a", "tap", {"target": {"id": "go-form"}})
        await d.run_tool("a", "type_text", {"target": {"id": "name-test-safe"}, "text": TYPED})
        await d.run_tool("a", "tap", {"target": {"id": "b-more"}, "dry_run": True})
        r = await d.run_tool("a", "bug_report", {"from": "now", "note": f"the name field kept {TYPED} after saving",
                                                 "expected": "the field is cleared"})
        lines = r["text"].splitlines()
        assert lines[0].startswith("New bug: Observed on place_") and TYPED not in r["text"]
        assert lines[1] == "Reproduced: not checked." and lines[2].startswith("Seen: 1 time, ")
        folder = Path(lines[3].removeprefix("Folder: ").replace("~", str(Path.home()), 1))
        assert folder.parent.parent == tmp_path / "bugs" and folder.parent.name == BUNDLE
        page = (folder / "report.md").read_text("utf-8")
        assert "1. Tap id go-form" in page and "2. Type something in id name-test-safe" in page
        assert "b-more" not in page  # a dry run is no step
        assert "Expected: the field is cleared. Got: the name field kept <typed value> after saving" in page
        assert "A picture exists and is private." in page and not (folder / "picture.jpg").exists()
        text = files_text(tmp_path / "bugs")
        assert TYPED not in text and not any(TYPED[i:i + 12] in text for i in range(len(TYPED) - 11))
        finding = json.loads((folder / "finding.json").read_text("utf-8"))
        assert finding["kind"] == "observed" and finding["bug"]["source"] == {"how": "now"}
        assert finding["path_from"] is not None and finding["bug"]["place_key"]["signature"]
        # an ordinary answer still shows what the screen shows: only the bug report hides typed values
        await finish(stop, task)
    run(go)


def test_from_now_needs_the_action_log_and_a_note(tmp_path):
    async def go():
        d = bugged(tmp_path)  # no action log: serve --no-log
        stop, task = await started(d)
        await d.run_tool("a", "tap", {"target": {"id": "go-form"}})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "bug_report", {"from": "now", "note": "x"})
        assert e.value.code == "unsupported" and "--no-log" in str(e.value)
        for bad in ({"from": "now"}, {"from": "now", "note": "a\nb"}, {"from": "now", "note": "x" * 301},
                    {"from": "later"}, {"finding": "f1", "from": "now", "note": "x"}, {},
                    {"finding": "f1", "note": "x"}, {"bug": "bug_x", "run": "last"}, {"finding": "f1", "verify": 11},
                    {"from": "now", "note": "x", "bogus": 1}):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "bug_report", bad)
            assert e.value.code == "bad_params", bad
        for bad in ({"bug": "bug_0123456789abcdef", "export": {"to": "gist"}},
                    {"bug": "bug_0123456789abcdef", "export": {"to": "issue"}}):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "bug_report", bad)
            assert e.value.code == "bad_params", bad
        assert not (tmp_path / "bugs").exists()
        await finish(stop, task)
    run(go)


def test_from_a_finding_of_the_newest_explore_report_and_the_kept_bug_again(tmp_path):
    async def go():
        d = bugged(tmp_path)
        d.explore_home = tmp_path / "explore"
        stop, task = await started(d)
        await d.run_tool("a", "look", {})
        write_run(d.explore_home, "20261112T095900Z")  # (another app's report: not the app in front)
        assert RUN_APP != BUNDLE
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "bug_report", {"finding": "f3"})
        assert e.value.code == "not_found" and "explore first" in str(e.value)
        r = await d.run_tool("a", "bug_report", {"finding": "f3", "run": str(d.explore_home / RUN_APP / "20261112T095900Z")})
        lines = r["text"].splitlines()
        assert lines[0].startswith("New bug: Crash on place_4: the app crashed: EXC_BAD_ACCESS")
        # (the default check runs: the report's app isn't on this phone, so no reset reaches a known place of it)
        assert lines[1] == "Reproduced: not checked (no reset reached a known place)."
        bug_id = lines[3].rsplit("/", 1)[-1]
        again = await d.run_tool("a", "bug_report", {"bug": bug_id})
        assert again["text"].startswith("Known bug (seen 1 time): Crash on place_4")
        added = await d.run_tool("a", "bug_report", {"bug": bug_id, "with_picture": True})
        assert added["text"].startswith("Known bug")
        assert (tmp_path / "bugs" / RUN_APP / bug_id / "picture.jpg").exists()
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "bug_report", {"bug": "bug_0123456789abcdef"})
        assert e.value.code == "not_found"
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "bug_report", {"finding": "f3", "run": str(tmp_path / "elsewhere")})
        assert e.value.code in ("not_found", "bad_params")
        await finish(stop, task)
    run(go)


def test_from_the_last_failed_check_and_a_later_run_clears_it(tmp_path):
    async def go():
        d = bugged(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "look", {})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": [{"tool": "tap", "target": {"id": "go-settings"},
                                                     "expect": {"text": "Never on this screen", "timeout": 0.5}}]})
        assert e.value.code == "expect_failed"
        r = await d.run_tool("a", "bug_report", {"from": "failure"})
        assert r["text"].startswith("New bug: Check failed on ")
        folder = Path(r["text"].splitlines()[3].removeprefix("Folder: ").replace("~", str(Path.home()), 1))
        page = (folder / "report.md").read_text("utf-8")
        assert "1. Tap id go-settings" in page and "step 1 holds its check (text 'Never on this screen')" in page
        assert json.loads((folder / "repro.json").read_text("utf-8"))["steps"][0]["expect"]["text"] == "Never on this screen"
        await d.run_tool("a", "run", {"steps": [{"tool": "back"}]})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "bug_report", {"from": "failure"})
        assert e.value.code == "not_found"
        await finish(stop, task)
    run(go)


def test_a_typed_value_is_registered_before_it_is_sent_but_not_the_explorers_fake_values(tmp_path):
    async def go():
        d = bugged(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "tap", {"target": {"id": "go-form"}})
        await d.run_tool("a", "type_text", {"target": {"id": "name-test-safe"}, "text": TYPED})
        assert TYPED in d.accounts.bug_values()
        assert d.accounts.bug_scrub(f"x {TYPED} y") == "x <typed value> y"
        assert d.scrub_everywhere(f"x {TYPED} y") == f"x {TYPED} y"  # ordinary answers are left as they are
        from tapwright import daemon as daemonlib
        token = daemonlib._EXPLORING.set(True)
        try:
            d._bug_typed("type_text", {"text": "test@example.com"})
        finally:
            daemonlib._EXPLORING.reset(token)
        assert "test@example.com" not in d.accounts.bug_values()
        await finish(stop, task)
    run(go)


def folder_of(answer: dict) -> Path:
    line = next(ln for ln in answer["text"].splitlines() if ln.startswith("Folder: "))
    return Path(line.removeprefix("Folder: ").replace("~", str(Path.home()), 1))


def gated(d) -> list:
    """Every move's dispatch gate, with the floor it ran under (None: no floor, an agent's own call)."""
    from tapwright import daemon as daemonlib
    seen: list = []
    real = d._floor_gate

    def spy(tool, a, element, bundle=None):
        seen.append((tool, daemonlib._TOUCH_FLOOR.get()))
        return real(tool, a, element, bundle)
    d._floor_gate = spy
    return seen


async def failed_check(d) -> None:
    await d.run_tool("a", "look", {})
    with pytest.raises(Refused):
        await d.run_tool("a", "run", {"steps": [{"tool": "tap", "target": {"id": "go-settings"},
                                                 "expect": {"text": "Never on this screen", "timeout": 0.5}}]})


def test_the_check_replays_the_failed_check_under_the_one_dispatch_gate(tmp_path):
    async def go():
        d = bugged(tmp_path)
        stop, task = await started(d)
        await failed_check(d)
        seen = gated(d)
        before = len(d.session.tapped)
        r = await d.run_tool("a", "bug_report", {"from": "failure", "verify": 2})
        lines = r["text"].splitlines()
        assert lines[1] == "Reproduced: 2 of 2 runs.", r["text"]
        assert [k for _, k in d.session.tapped[before:]] == ["go-settings", "go-settings"]
        taps = [floor for tool, floor in seen if tool == "tap"]
        assert taps and all(floor is not None for floor in taps)  # every touch under the floor
        page = (folder_of(r) / "report.md").read_text("utf-8")
        assert "2 of 2 runs. Script: repro.json" in page and "- Reset before each run: relaunch only, data kept" in page
        meta = json.loads((folder_of(r) / "finding.json").read_text("utf-8"))["bug"]["reproduced"]
        assert (meta["n"], meta["of"], meta["reset"]) == (2, 2, "relaunch only, data kept")
        again = await d.run_tool("a", "bug_report", {"bug": folder_of(r).name, "verify": 1})
        assert again["text"].splitlines()[1] == "Reproduced: 1 of 1 runs."
        await finish(stop, task)
    run(go)


def test_the_check_stops_before_a_step_that_needs_a_person_and_touches_nothing(tmp_path):
    async def go():
        d = bugged(tmp_path, actions=ActionLog(tmp_path / "log"))
        stop, task = await started(d)
        await d.run_tool("a", "look", {})
        await d.run_tool("a", "tap", {"target": {"id": "go-delete"}})
        tapped = list(d.session.tapped)
        r = await d.run_tool("a", "bug_report", {"from": "now", "note": "nothing happened", "verify": 3})
        assert r["text"].splitlines()[1].startswith("Reproduced: not checked (step 1 needs a person (the policy word")
        assert d.session.tapped == tapped and not d._confirms  # no move, nobody asked
        await finish(stop, task)
        d = bugged(tmp_path / "typed", actions=ActionLog(tmp_path / "typed-log"))
        stop, task = await started(d)
        await d.run_tool("a", "look", {})
        await d.run_tool("a", "tap", {"target": {"id": "go-form"}})
        await d.run_tool("a", "type_text", {"target": {"id": "name-test-safe"}, "text": TYPED})
        tapped = list(d.session.tapped)
        r = await d.run_tool("a", "bug_report", {"from": "now", "note": "the name is lost", "verify": True})
        assert "needs a person (a typed value)" in r["text"].splitlines()[1]
        assert d.session.tapped == tapped
        await finish(stop, task)
    run(go)


def test_a_check_with_no_phone_or_an_app_reset_still_makes_the_bug(tmp_path):
    async def go():
        d = bugged(tmp_path)
        stop, task = await started(d)
        await failed_check(d)
        r = await d.run_tool("a", "bug_report", {"from": "failure", "reset": "app", "account": "tester"})
        assert "app_command reset" in r["text"].splitlines()[1] and "Note: account isn't used" in r["text"]
        assert (folder_of(r) / "report.md").exists()
        held = d._bugs().read(BUNDLE, folder_of(r).name)
        await finish(stop, task)
        d.state = "no-device"  # (the supervisor has stopped: nothing sets it back to live)
        assert await d._bug_check(held, 2, "own", None, client_id="a") == {"checked": False, "why": "no phone connected"}
    run(go)


def test_export_to_a_zip_with_its_audit_line_and_none_without_an_audit_log(tmp_path):
    async def go():
        from tapwright import auditlog
        d = bugged(tmp_path, audit=auditlog.AuditLog(tmp_path / "audit"))
        stop, task = await started(d)
        await failed_check(d)
        r = await d.run_tool("a", "bug_report", {"from": "failure", "verify": False, "export": {"to": "zip"}})
        folder = folder_of(r)
        assert r["text"].splitlines()[-1].startswith("Exported: ") and (folder.parent / f"{folder.name}.zip").exists()
        lines = [json.loads(x) for x in auditlog.read_lines(tmp_path / "audit")]
        assert [(x["kind"], x["name"], x["app"]) for x in lines] == [("export", "bug_report", BUNDLE)]
        assert lines[0]["detail"].endswith(f": {folder.name}, picture not sent")
        (folder.parent / f"{folder.name}.zip").unlink()
        d.audit = None
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "bug_report", {"bug": folder.name, "export": {"to": "zip"}})
        assert e.value.code == "not_allowed" and "audit line" in str(e.value) and "Known bug" in str(e.value)
        assert not (folder.parent / f"{folder.name}.zip").exists()
        await finish(stop, task)
    run(go)


def test_a_word_only_the_actual_recipient_holds_stops_the_check_at_dispatch(tmp_path):
    async def go():
        from tapwright import policy as policylib
        d = bugged(tmp_path)
        stop, task = await started(d)
        await failed_check(d)
        rules = policylib.parse({"confirm": ["area"]})  # the step's id (go-settings) doesn't hold it; its element's label does
        before = len(d.session.tapped)
        r = await d.run_tool("a", "bug_report", {"from": "failure", "verify": 3}, None, rules)
        assert r["text"].splitlines()[1] == 'Reproduced: not checked (step 1 needs a person (the policy word "area")).'
        assert d.session.tapped[before:] == [] and not d._confirms  # no move of that step, nobody asked
        await finish(stop, task)
    run(go)

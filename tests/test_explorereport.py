"""The explore report and answer (AGENT-TOOLS §25.2, §25.10, §25.11), the progress line to a host, the vault's `explore` marker
and the call's arguments."""
import asyncio
import datetime
import json
from pathlib import Path

import pytest
from explorefakes import identity

from tapwright import accounts, explore, explorereport, exploretool, findings, hostdialog
from tapwright.control import Refused

NOW = datetime.datetime(2026, 11, 14, 9, 15, tzinfo=datetime.timezone.utc)


def finding(kind="js_error", n=1, place="place_2", got="TypeError: boom", **more) -> findings.Finding:
    return findings.make(finding_id=f"f{n}", kind=kind, app="com.example.app", app_version="1.0", build="7", tool="tapwright",
                         tool_version="1.8.0", device=findings.Device("iPhone14,2", "iOS 26.5"), expected="No error is logged.",
                         got=got, scrub=lambda s: s, place=place, path_from="place_1",
                         path=[{"tool": "tap", "target": {"id": "go-form"}}], **more)


def result(found=(), stopped="frontier_empty", **more) -> explore.Result:
    base = dict(stopped=stopped, start_place="place_1", left_at="place_2", places=["place_1", "place_2", "place_3"], expanded=[],
                moves=[], steps=212, seconds=372.0, findings=tuple(found), skipped=[explore.Skipped("place_1", "text:Sign out", "floor", "Sign out")],
                notes=["rows: first 2 of the list tried (place_2)"], resets=[{"how": "relaunch", "data_kept": True, "at_step": 88}],
                deferred=[], frontier_left=0, prompts=0)
    base.update(more)
    return explore.Result(**base)


def test_the_answer_is_a_summary_with_the_findings_and_the_report_and_no_coverage_line(tmp_path):
    found = [finding("crash", 1), finding("js_error", 2), finding("js_error", 3, got="other"), finding("a11y", 4, got="one issue")]
    text = explorereport.answer(result(found), tmp_path / "20261114T091500Z", lambda s: s)
    assert text.splitlines() == [
        "Explored 3 places in 6.2 min (212 steps), 4 findings. Stopped: the frontier is empty.",
        "Findings: 1 crash, 2 js_error, 1 a11y.",
        f"Report: {tmp_path / '20261114T091500Z' / 'report.md'}"]
    assert "Coverage:" not in text  # a tool with no places known up front (§25.11)


def test_every_stop_ends_with_its_reason():
    for stopped, said in (("budget_steps", "the budget (steps) is used up."), ("budget_minutes", "the budget (minutes) is used up."),
                          ("crash", "the app crashed."), ("cant_get_back", "it can't get back to a known place."),
                          ("app_closed", "the app is closed.")):
        assert explorereport.answer(result(stopped=stopped), None, str).splitlines()[0].endswith(f"Stopped: {said}")
    assert explorereport.stop_line(result(stopped="toggle_not_restored", detail="id=notify-switch")) == \
        "a toggle could not be set back (id=notify-switch)."
    assert explorereport.answer(result(findings=()), None, str).count("\n") == 0  # no findings, no folder: one line
    assert "1 finding." in explorereport.answer(result([finding()]), None, str)


def test_the_error_of_a_pause_a_busy_phone_or_a_lock_names_the_report_and_where_it_was_left(tmp_path):
    for stopped, said in (("paused", "paused by the user in the live view; ask them to resume."),
                          ("busy", "a person is using the phone; try again in a moment."),
                          ("locked", "the phone is locked; ask the user to unlock it.")):
        text = explorereport.error_text(result(stopped=stopped, steps=41, places=["a"] * 9, left_at="settings"), tmp_path, str)
        assert text == f"{said} Done: 41 steps, 9 places. Left at: settings. Report: {tmp_path / 'report.md'}"


def test_the_folder_holds_report_json_report_md_a_picture_and_a_script_per_finding(tmp_path):
    shot = finding(n=1, picture_file="findings/f1.jpg")
    out = explorereport.write(tmp_path, result([shot], pictures={"f1": b"\xff\xd8jpeg"}), identity(), explore.Budget(), NOW, NOW,
                              lambda s: s)
    assert out == tmp_path / "com.example.app" / "20261114T091500Z"
    assert (out / "findings" / "f1.jpg").read_bytes() == b"\xff\xd8jpeg"
    script = json.loads((out / "repro" / "f1.json").read_text("utf-8"))
    assert script["start"] == "place_1" and script["steps"] == [{"tool": "tap", "target": {"id": "go-form"}}]
    body = json.loads((out / "report.json").read_text("utf-8"))
    assert body["format"] == "agent-tools-explore-report" and body["version"] == 1 and body["tool"] == "tapwright"
    assert body["started"] == "2026-11-14T09:15:00Z" and body["stopped"] == "frontier_empty" and body["start_place"] == "place_1"
    assert body["budget"] == {"minutes": 10.0, "steps": 500, "depth": 8} and body["used"] == {"minutes": 6.2, "steps": 212}
    assert body["coverage"] == {"places": 3, "frontier_left": 0}  # no known_up_front, not_reached or areas
    assert body["skipped"] == [{"place": "place_1", "sig": "text:Sign out", "why": "floor", "word": "Sign out"}]
    assert body["notes"] == ["rows: first 2 of the list tried (place_2)"]
    assert body["resets"] == [{"how": "relaunch", "data_kept": True, "at_step": 88}]
    assert body["findings"][0]["repro"] == "repro/f1.json" and body["findings"][0]["picture"] == {"file": "findings/f1.jpg", "private": True}
    md = (out / "report.md").read_text("utf-8")
    assert md.index("## Findings") < md.index("## Skipped") and "private: shown only when a person asks" in md and "![" not in md
    assert "reset: relaunch only, data kept" in md


def test_a_test_apps_picture_is_shown_in_the_markdown(tmp_path):
    shot = findings.make(finding_id="f1", kind="js_error", app="com.example.app", app_version="1.0", build="7", tool="tapwright",
                         tool_version="1.8.0", device=findings.Device("iPhone", "iOS"), expected="x", got="y", scrub=str,
                         place="place_1", path_from="place_1", path=[{"tool": "back"}], picture_file="findings/f1.jpg", test_app=True)
    out = explorereport.write(tmp_path, result([shot], pictures={"f1": b"jpeg"}), identity(), explore.Budget(), NOW, NOW, str)
    assert "![f1](findings/f1.jpg)" in (out / "report.md").read_text("utf-8")


def test_all_text_goes_through_the_redaction_and_no_value_reaches_a_file_or_the_answer(tmp_path):
    secret = "Lisbon-Alfama-77"

    def scrub(text: str) -> str:
        return text.replace(secret, "<trip's destination>")
    raw = findings.make(finding_id="f1", kind="js_error", app="com.example.app", app_version="1.0", build="7", tool="tapwright",
                        tool_version="1.8.0", device=findings.Device("iPhone", "iOS"), expected="x", got=f"error at {secret}", scrub=str,
                        place="place_1", path_from="place_1", path=[{"tool": "tap", "target": {"label": f"Go {secret}"}}])
    out = explorereport.write(tmp_path, result([raw], notes=[f"a note about {secret}"]), identity(), explore.Budget(), NOW, NOW, scrub)
    every = "\n".join(p.read_text("utf-8") for p in out.rglob("*") if p.is_file())
    assert secret not in every and "<trip's destination>" in every
    assert secret not in explorereport.answer(result([raw]), out, scrub) + explorereport.error_text(result(stopped="busy"), out, scrub)


def test_the_newest_twenty_folders_of_an_app_are_kept(tmp_path):
    base = tmp_path / "com.example.app"
    for n in range(25):
        (base / f"202611{n + 1:02d}T000000Z").mkdir(parents=True)
    (base / "notes").mkdir()
    out = explorereport.write(tmp_path, result(), identity(), explore.Budget(), NOW, NOW, str)
    kept = sorted(p.name for p in base.iterdir() if p.name != "notes")
    assert len(kept) == 20 and out.name in kept and "20261101T000000Z" not in kept and (base / "notes").exists()
    assert kept[-1] == "20261114T091500Z" or out.name == "20261114T091500Z"


def test_two_runs_in_one_second_keep_both_folders(tmp_path):
    a = explorereport.write(tmp_path, result(), identity(), explore.Budget(), NOW, NOW, str)
    b = explorereport.write(tmp_path, result(), identity(), explore.Budget(), NOW, NOW, str)
    assert a != b and a.exists() and b.exists()


# ---- the call's arguments (§25.2) ----

def test_the_defaults_and_the_ranges():
    parsed = exploretool.parse({})
    assert (parsed.budget.minutes, parsed.budget.steps, parsed.budget.depth, parsed.fill, parsed.keep_going) == (10, 500, 8, "none", False)
    assert exploretool.parse({"minutes": 1, "steps": 5000, "depth": 20, "fill": "test-data", "continue": True}).budget.depth == 20
    assert exploretool.parse({"fill": "fixture:trip_data.destination"}).fill == "fixture:trip_data.destination"
    for bad in ({"minutes": 0.5}, {"minutes": 60.1}, {"steps": 0}, {"steps": True}, {"depth": 0}, {"depth": "8"},
                {"fill": "fixture:trip"}, {"fill": "Test-Data"}, {"from": ""}, {"focus": 3}, {"account": ""}, {"continue": 1}):
        with pytest.raises(Refused) as e:
            exploretool.parse(bad)
        assert e.value.code == "bad_params", bad
    assert exploretool.timeout_for({}) == 660 and exploretool.timeout_for({"minutes": 60}) == 3660


def test_the_mcp_timeout_holds_the_whole_budget():
    from tapwright import mcp_server
    assert mcp_server._timeout("explore", {"minutes": 30}) == 1860 and mcp_server._timeout("explore", {}) == 660


# ---- the issue lines of audit and ux_check ----

def test_the_issue_lines_of_an_audit_and_a_ux_check():
    assert exploretool.issues_of('Audit of "Example": no issues found.\nChecked: contrast.') == ()
    assert exploretool.issues_of('UX check of "Example": 0 findings.\nMeasured: 3 actionable elements; main action: none found.') == ()
    audit = 'Audit of "Example": 2 issues (1 contrast, 1 clipped_text).\n1. e3 button "OK": contrast\n2. e4 text "x": clipped\n(+3 more issues; pass types to narrow it.)'
    assert exploretool.issues_of(audit) == ('1. e3 button "OK": contrast', '2. e4 text "x": clipped')


# ---- progress to a host (§25.2) ----

class Ctx:
    def __init__(self, token=None) -> None:
        self.token, self.sent = token, []
        meta = type("Meta", (), {"progress_token": token})()
        self.request_context = type("Req", (), {"meta": meta})()

    async def report_progress(self, progress, total=None, message=None):
        self.sent.append((progress, message))


def test_a_progress_line_reaches_a_client_that_gave_a_token_and_never_the_answer():
    async def go():
        events = [{"type": "progress", "id": "p1", "message": "explored 3 places, 1 findings"}]
        polls = {"n": 0}

        def poll(since, wait):
            polls["n"] += 1
            return {"seq": polls["n"], "events": events if polls["n"] == 1 else []}
        dialogs = hostdialog.HostDialogs(poll=poll, answer=lambda *a: True)
        ctx = Ctx(token="tok")
        assert hostdialog.has_progress_token(ctx) and not hostdialog.has_progress_token(Ctx()) and not hostdialog.has_progress_token(object())
        async with dialogs.during(ctx, progress=True) as dialog:
            assert dialog is False  # no dialog: this client shows no forms
            for _ in range(50):
                if ctx.sent:
                    break
                await asyncio.sleep(0.01)
        assert ctx.sent == [(1.0, "explored 3 places, 1 findings")]
        quiet = Ctx()
        async with dialogs.during(quiet, progress=True) as dialog:  # no token: nothing is polled for it
            assert dialog is False and dialogs._pump is None
    asyncio.run(go())


# ---- the vault's marker (§25.6) ----

def test_the_vault_reads_the_explore_marker_and_refuses_a_non_boolean(tmp_path):
    def load(entry: dict):
        path = tmp_path / "v.json"
        path.write_text(json.dumps({"version": 2, "vault": {"trip": {"kind": "fixture", "destination": "Lisbon", **entry}}}), "utf-8")
        return accounts.load(str(path))
    assert load({"explore": True}).vault["trip"].explore is True
    assert load({}).vault["trip"].explore is False
    refused = load({"explore": "yes"})
    assert refused.problem is not None and "explore must be true or false" in refused.problem and "Lisbon" not in refused.problem

"""The first Opus review of 1.8.5 (R185): a link kept only as its place's pattern (H1), a bug of an app that isn't one of the
daemon's test apps (H2), a content row (M1), paths in `got` (M2), the command line's values (M3, in test_cli_bug.py), a failed
run's steps as replayed (M4), a check that writes back what changed meanwhile (M5), the system sheet of an explore finding
(M6), and the lows. No phone, no network, no real ~/.tapwright: everything is in tmp_path."""
import base64
import json
import zipfile
from pathlib import Path

import pytest
from explorefakes import grid
from test_bugexport import ISSUE, Audit, issue_replies
from test_bugstore import APP, NOT_ASKED, SECRET, ctx_for, make, store_with, write_run
from test_daemon import finish, run, started
from test_daemon_bug import bugged, files_text, folder_of
from test_daemon_explore import BACK_TO, BUNDLE, GOES, ExploreSession, app_screens
from test_journey import FakeGh

from tapwright import bug, bugcheck, bugexport, bugmake, bugstore, findings, journey_export, mapkeeper
from tapwright.actionlog import ActionLog
from tapwright.auditlog import AuditLog, read_lines
from tapwright.control import Refused

TOKEN = "SECRETTOKEN123456"
ROW = "Dinner with Sarah, card ends 4242"


def everything_sent(gh: FakeGh) -> str:
    """Every argument and input given to gh, with each blob's base64 content decoded."""
    out = []
    for args, stdin in gh.calls:
        out.append(" ".join(args))
        if stdin:
            out.append(stdin)
            try:
                content = json.loads(stdin).get("content")
            except (ValueError, AttributeError):
                content = None
            if isinstance(content, str):
                out.append(base64.b64decode(content).decode("utf-8", "replace"))
    return "\n".join(out)


def zip_text(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        return "\n".join(z.read(n).decode("utf-8", "replace") for n in z.namelist())


# ---- H1: a link is kept only as its place's pattern ----

def test_a_link_is_kept_as_its_places_pattern_never_its_token_in_any_file_the_zip_or_the_issue(tmp_path, monkeypatch):
    async def go():
        d = bugged(tmp_path, actions=ActionLog(tmp_path / "log"), audit=AuditLog(tmp_path / "audit"))
        stop, task = await started(d)
        await d.run_tool("a", "look", {})
        here = d.control.client("a").place[1]
        await d.run_tool("a", "map", {"link": "example://login", "place": here})
        await d.run_tool("a", "open_url", {"url": f"example://reset/{TOKEN}"})  # (matches no pattern: never kept)
        await d.run_tool("a", "open_url", {"url": f"example://login?magic={TOKEN}#t={TOKEN}"})
        await d.run_tool("a", "tap", {"target": {"id": "go-settings"}})
        r = await d.run_tool("a", "bug_report", {"from": "now", "note": "the sign-in link lands on settings",
                                                 "export": {"to": "zip"}})
        folder = folder_of(r)
        page = (folder / "report.md").read_text("utf-8")
        assert "Earlier steps couldn't be written down." in page
        assert "1. Open the link example://login (its query and fragment aren't kept" in page and "2. Tap id go-settings" in page
        script = json.loads((folder / "repro.json").read_text("utf-8"))
        assert script["steps"][0] == {"tool": "open_url", "url": "example://login"}
        assert json.loads((folder / "finding.json").read_text("utf-8"))["path"][0]["url"] == "example://login"
        assert TOKEN not in files_text(tmp_path / "bugs") and TOKEN not in r["text"]
        assert TOKEN not in zip_text(folder.parent / f"{folder.name}.zip")
        gh = FakeGh(issue_replies())
        monkeypatch.setattr(journey_export, "run_gh", gh)
        await d.run_tool("a", "bug_report", {"bug": folder.name, "export": ISSUE})
        assert any(a[:2] == ["issue", "create"] for a, _ in gh.calls) and TOKEN not in everything_sent(gh)
        await finish(stop, task)
    run(go)


def test_a_failed_runs_link_is_cut_too_and_a_pattern_with_a_value_isnt_checked(tmp_path):
    record = bugmake.failure_record(
        app=APP, identity={"app_version": "1.0", "build": "3", "tool_version": "1.8.5",
                           "device": {"model": "iPhone16,1", "os": "iOS 19.1"}},
        when="2026-11-19T10:00:00.000Z", failed_at=2, expected="step 2 holds its check", got="no", path_from="place_1",
        place="place_2", logs=[], link_of=lambda url: "example://trips/[id]" if url.startswith("example://trips/") else None,
        steps=[{"tool": "open_url", "url": f"example://trips/42?token={TOKEN}"},
               {"tool": "tap", "target": {"id": "save"}, "expect": {"text": "Saved"}}])
    assert record["finding"]["path"][0] == {"tool": "open_url", "url": "example://trips/[id]"}
    assert record["links_cut"] == [0] and TOKEN not in json.dumps(record)
    assert bugcheck.unaimed(record["finding"]["path"]).startswith("step 1 opens a link whose value isn't kept")
    gone = bugmake.failure_record(**{**dict(app=APP, identity={"app_version": "1.0", "build": "3", "tool_version": "1.8.5",
                                                                "device": {"model": "m", "os": "o"}},
                                            when="2026-11-19T10:00:00.000Z", failed_at=2, expected="e", got="g",
                                            path_from="place_1", place=None, logs=[]),
                                     "steps": [{"tool": "open_url", "url": f"other://x?{TOKEN}"},
                                               {"tool": "tap", "target": {"id": "save"}, "expect": {"text": "Saved"}}]})
    assert gone["cut"] and TOKEN not in json.dumps(gone) and len(gone["finding"]["path"]) == 1


# ---- H2: a bug of an app that isn't one of the daemon's test apps ----

def test_a_bug_of_an_app_that_isnt_a_test_app_keeps_no_screen_text_or_logs_and_any_export_needs_confirm(tmp_path):
    async def go():
        d = bugged(tmp_path / "not", named=None, actions=ActionLog(tmp_path / "log"), audit=AuditLog(tmp_path / "audit"))
        asked: list[str] = []

        async def logs(bundle):
            asked.append(bundle)
            return ["10:00:00.000 error the inbox of jane@example.com failed"]
        d._bug_logs = logs
        stop, task = await started(d)
        await d.run_tool("a", "look", {})
        await d.run_tool("a", "tap", {"target": {"label": "Profile form"}})  # (aimed by words: not kept for such an app)
        await d.run_tool("a", "tap", {"target": {"id": "b-more"}})
        r = await d.run_tool("a", "bug_report", {"from": "now", "note": "order 42 of 'Jane Doe' is lost at 10:21",
                                                 "with_picture": True})
        folder = folder_of(r)
        page = (folder / "report.md").read_text("utf-8")
        assert asked == []  # (its log isn't even read)
        assert "No log lines." in page and "jane@example.com" not in files_text(folder)
        assert "Got: order # of 'Jane Doe' is lost at #" in page  # (its own got, normalized)
        assert "Profile form" not in page and "Earlier steps couldn't be written down." in page and "1. Tap id b-more" in page
        assert f"- The app is {bug.NOT_A_TEST_APP}." in page
        meta = json.loads((folder / "finding.json").read_text("utf-8"))["bug"]
        assert meta["test_app"] is False
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "bug_report", {"bug": folder.name, "export": {"to": "zip"}})
        assert "needs_confirmation" in str(e.value) and "test apps" in str(e.value)
        assert not (folder.parent / f"{folder.name}.zip").exists()
        done = await d.run_tool("a", "bug_report", {"bug": folder.name, "export": {"to": "zip", "confirm": True}})
        assert done["text"].splitlines()[-1].startswith("Exported: ")
        details = [json.loads(x)["detail"] for x in read_lines(tmp_path / "audit")]
        assert details[0].endswith("refused (needs confirmation)") and "picture" in details[1]
        await finish(stop, task)
        # the same with the app named (serve --apps): its log lines and its words are kept
        d = bugged(tmp_path / "named", actions=ActionLog(tmp_path / "log2"))
        d._bug_logs = logs
        stop, task = await started(d)
        await d.run_tool("a", "look", {})
        await d.run_tool("a", "tap", {"target": {"label": "Profile form"}})
        r = await d.run_tool("a", "bug_report", {"from": "now", "note": "order 42 is lost"})
        page = (folder_of(r) / "report.md").read_text("utf-8")
        assert asked == [BUNDLE] and "the inbox of" in page and 'Tap "Profile form"' in page
        assert bug.NOT_A_TEST_APP not in page
        await finish(stop, task)
    run(go)


def test_the_test_apps_are_the_daemons_named_ones_dedicated_names_every_app(tmp_path):
    from tapwright.ceiling import Ceiling

    async def go():
        d = bugged(tmp_path, named=None)
        assert not await d._bug_named(BUNDLE)
        d.ceiling = Ceiling(apps=("com.other.app",))
        assert not await d._bug_named(BUNDLE)
        d.ceiling = Ceiling(apps=(BUNDLE,))
        assert await d._bug_named(BUNDLE)
        d.ceiling = Ceiling(dedicated=True)
        assert await d._bug_named(BUNDLE)
    run(go)


def test_a_finding_of_an_app_that_isnt_a_test_app_has_a_private_picture_and_no_logs(tmp_path):
    home = tmp_path / "explore"
    write_run(home, private=False, logs=("09:21:07.440 error the card 4242 of Jane",),
              path=[{"tool": "tap", "target": {"id": "trips-tab"}}, {"tool": "tap", "target": {"text": ROW}}])
    made = make(ctx_for(tmp_path), home, "20261114T091500Z", named=False)
    text = "\n".join(p.read_text("utf-8") for p in made.folder.iterdir())
    assert "4242" not in text and ROW not in text and "Jane" not in text
    assert made.finding["picture"]["private"] is True and not (made.folder / "picture.jpg").exists()
    assert made.meta["test_app"] is False and made.meta["steps_cut"] is True


# ---- M1: a content row is the n-th row ----

def inbox_daemon(tmp_path, **options):
    from test_daemon import make as make_daemon
    screens = {**app_screens(), "inbox": grid("Inbox", cells=((ROW, ()), ("Lunch with Tom", ())), back="Home")}
    session = ExploreSession(screens, "inbox", {**GOES, ("inbox", ROW): "more"}, {**BACK_TO, "inbox": "home"})
    d = make_daemon(session, **options)
    d.maps = mapkeeper.Maps(str(tmp_path / "maps"))
    d.explore_home = tmp_path / "explore"
    d.bug_home = tmp_path / "bugs"
    from tapwright.ceiling import Ceiling
    d.ceiling = Ceiling(apps=(BUNDLE,))
    return d


def test_a_tapped_row_with_no_id_is_the_nth_row_never_its_text(tmp_path):
    async def go():
        d = inbox_daemon(tmp_path, actions=ActionLog(tmp_path / "log"))
        stop, task = await started(d)
        await d.run_tool("a", "look", {})
        await d.run_tool("a", "tap", {"target": {"label": ROW}})
        r = await d.run_tool("a", "bug_report", {"from": "now", "note": "the row opens the wrong screen"})
        folder = folder_of(r)
        page = (folder / "report.md").read_text("utf-8")
        assert "1. Tap the 1st row of the list." in page
        assert "Sarah" not in files_text(folder) and "4242" not in files_text(folder)
        await finish(stop, task)
    run(go)


# ---- M2: paths in got ----

def test_paths_in_got_and_the_title_are_cut_and_the_new_path_forms():
    assert bug.strip_paths("at file:///Users/jane/app/x.js:1 y") == "at x.js:1 y"
    assert bug.strip_paths("\\\\MACHINE\\share\\x.txt err") == "x.txt err"
    assert bug.strip_paths("open /home/jane bad") == "open ~ bad"
    assert bug.strip_paths("C:/Users/jane") == "~"
    assert bug.strip_paths("C:\\Users\\Jane Doe\\proj\\src\\a.ts:48 x") == "a.ts:48 x"  # (a folder with a space: cut whole)


def test_a_crash_reason_with_a_user_folder_never_reaches_the_title(tmp_path):
    home = tmp_path / "explore"
    reason = "the app crashed: Fatal error: file /Users/jane/Wayfare/TripView.swift, line 48"
    write_run(home, got=reason)
    made = make(ctx_for(tmp_path), home, "20261114T091500Z")
    assert "jane" not in made.title and "file TripView.swift, line 48" in made.title
    assert "jane" not in (made.folder / "report.md").read_text("utf-8")


# ---- M4: a failed run's steps as replayed ----

def test_a_failed_run_keeps_its_other_steps_and_their_expect_so_its_check_reproduces(tmp_path):
    async def go():
        d = bugged(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "look", {})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": [
                {"tool": "wait_for", "text": "Profile form", "timeout": 1},
                {"tool": "tap", "target": {"id": "go-settings"}, "expect": {"text": "Never on this screen", "timeout": 0.5}}]})
        assert e.value.code == "expect_failed"
        r = await d.run_tool("a", "bug_report", {"from": "failure", "verify": 2})
        assert r["text"].splitlines()[1] == "Reproduced: 2 of 2 runs.", r["text"]
        script = json.loads((folder_of(r) / "repro.json").read_text("utf-8"))
        assert script["steps"][0] == {"tool": "wait_for", "text": "Profile form", "timeout": 1}
        assert script["steps"][1]["expect"]["text"] == "Never on this screen"
        await finish(stop, task)
    run(go)


def test_a_failed_steps_typed_text_is_never_kept_and_a_drive_with_values_cuts_the_path():
    step, _ = bugmake.failed_step({"tool": "type_text", "target": {"id": "name"}, "text": "hunter2", "expect": {"text": "x"}},
                                  None, lambda url: None)
    assert step == {"tool": "type_text", "target": {"id": "name"}, "text": "${typed}", "expect": {"text": "x"}}
    assert bugmake.failed_step({"tool": "drive", "to": "trip", "with": {"id": "42"}}, None, lambda url: None)[0] is None
    assert bugmake.failed_step({"tool": "open_app", "app": BUNDLE, "expect": {"text": "Home"}, "place": "p"}, None,
                               lambda url: None)[0] == {"tool": "open_app", "app": BUNDLE, "expect": {"text": "Home"}}


# ---- M5: a check writes back only its own result, under the lock ----

def test_a_check_keeps_a_sighting_made_while_it_ran_and_never_makes_a_removed_bug_again(tmp_path):
    home = tmp_path / "explore"
    write_run(home)
    ctx = ctx_for(tmp_path)
    held_before = ctx.store.read(APP, make(ctx, home, "20261114T091500Z").meta["id"])
    write_run(home, "20261114T101500Z", build="221")
    make(ctx, home, "20261114T101500Z")  # (another sighting while the check ran)
    result = {"n": 1, "of": 1, "build": "1.5.0 (221)", "when": "2026-11-14T10:30:00.000Z", "reset": bugcheck.RESET_OWN}
    done = ctx.store.checked(held_before, result, ctx.gate, scrub=ctx.safe())
    seen = json.loads((done.folder / "finding.json").read_text("utf-8"))["bug"]
    assert seen["seen"]["count"] == 2 and seen["seen"]["builds"] == ["1.5.0 (220)", "1.5.0 (221)"]
    assert seen["reproduced"]["n"] == 1 and (done.folder / "repro.json").exists()
    import shutil
    shutil.rmtree(done.folder)
    assert ctx.store.checked(held_before, result, ctx.gate) is None and not done.folder.exists()


def test_retention_never_removes_a_bug_a_check_holds(tmp_path):
    home = tmp_path / "explore"
    ctx = ctx_for(tmp_path, kept=1)
    write_run(home, got="first thing")
    first = make(ctx, home, "20261114T091500Z")
    with ctx.store.hold(APP, first.meta["id"]):
        write_run(home, "20261114T101500Z", got="second thing", place_keys={"place_4": {"signature": "ffff000011112222"}})
        second = make(ctx, home, "20261114T101500Z")
        assert first.folder.exists()  # (held: kept although it is over the limit)
    ctx.store.prune(APP, keep=second.meta["id"])
    assert not first.folder.exists()


# ---- M6: the system sheet of an explore finding ----

def test_a_finding_records_the_system_sheet_in_front_and_its_picture_is_private_even_for_a_test_app():
    made = findings.make(finding_id="f1", kind="crash", app=APP, app_version="1.0", build="3", tool="tapwright",
                         tool_version="1.8.5", device=findings.Device("iPhone", "iOS 19"), expected="e",
                         got="the app crashed: x", scrub=lambda t: t, picture_file="findings/f1.jpg", test_app=True,
                         crash_reason="x", system="share sheet")
    out = findings.to_dict(made)
    assert out["system"] == "share sheet" and out["picture"] == {"file": "findings/f1.jpg", "private": True}
    assert findings.parse(json.dumps(out), lambda t: t).system == "share sheet"


def test_a_bug_from_an_explore_report_whose_finding_had_the_share_sheet_says_so(tmp_path):
    home = tmp_path / "explore"
    folder = write_run(home, private=False)
    report = json.loads((folder / "report.json").read_text("utf-8"))
    report["findings"][0]["system"] = "share sheet"
    (folder / "report.json").write_text(json.dumps(report), encoding="utf-8")
    made = make(ctx_for(tmp_path), home, "20261114T091500Z")
    page = (made.folder / "report.md").read_text("utf-8")
    assert "- A system sheet was in front: share sheet." in page and not (made.folder / "picture.jpg").exists()


def test_the_explorer_writes_the_system_sheet_on_a_finding_made_with_it_in_front(tmp_path):
    async def go():
        from test_daemon_explore import explored
        d = explored(tmp_path)
        d.bug_home = tmp_path / "bugs"
        session = d.session
        sheet = grid("Share", popover=True)  # (how iOS 26 draws the share sheet)
        session.screens["share"] = sheet
        session.goes[("form", "b-more")] = "share"
        stop, task = await started(d)
        r = await d.run_tool("a", "explore", {})
        folder = Path(next(ln for ln in r["text"].splitlines() if ln.startswith("Report: ")).removeprefix("Report: ")).parent
        found = json.loads((folder / "report.json").read_text("utf-8"))["findings"]
        assert any(f.get("system") == "share sheet" for f in found), found
        await finish(stop, task)
    run(go)


# ---- lows ----

def test_l1_a_check_whose_port_cant_be_made_leaves_no_busy_mark(tmp_path, monkeypatch):
    async def go():
        d = bugged(tmp_path)
        stop, task = await started(d)
        from test_daemon_bug import failed_check
        await failed_check(d)
        r = await d.run_tool("a", "bug_report", {"from": "failure", "verify": False})
        held = d._bugs().read(BUNDLE, folder_of(r).name)

        def broken(*a, **k):
            raise RuntimeError("no screen")
        monkeypatch.setattr(bugcheck, "DaemonCheck", broken)
        result = await d._bug_check(held, 1, "own", None, client_id="a")
        assert result["checked"] is False and d._explore_active is None
        await finish(stop, task)
    run(go)


def test_l2_a_sighting_after_a_value_was_added_to_the_vault_hides_it_instead_of_failing(tmp_path):
    home = tmp_path / "explore"
    write_run(home, logs=("09:21:07.440 error token NEWSECRETVALUE99 lost",))
    ctx = ctx_for(tmp_path, store_with(SECRET))
    first = make(ctx, home, "20261114T091500Z")
    assert "NEWSECRETVALUE99" in (first.folder / "report.md").read_text("utf-8")
    later = ctx_for(tmp_path, store_with(SECRET, "NEWSECRETVALUE99"))
    write_run(home, "20261114T101500Z", logs=("09:21:07.440 error token NEWSECRETVALUE99 lost",))
    again = make(later, home, "20261114T101500Z")
    assert not again.new and "NEWSECRETVALUE99" not in files_text(again.folder)


def test_l2_an_update_the_gate_refuses_says_the_bug_was_kept(tmp_path):
    home = tmp_path / "explore"
    write_run(home)
    ctx = ctx_for(tmp_path)
    held = ctx.store.read(APP, make(ctx, home, "20261114T091500Z").meta["id"])
    with pytest.raises(bugstore.BugGateError) as e:
        ctx.store.checked(held, NOT_ASKED, lambda files: "report.md")
    assert e.value.update is True


def test_l3_a_sighting_with_with_picture_adds_its_picture(tmp_path):
    home = tmp_path / "explore"
    write_run(home)
    ctx = ctx_for(tmp_path)
    first = make(ctx, home, "20261114T091500Z")
    assert not (first.folder / "picture.jpg").exists()
    write_run(home, "20261114T101500Z")
    again = make(ctx, home, "20261114T101500Z", with_picture=True)
    assert (again.folder / "picture.jpg").exists() and again.meta["picture_file"]["consent"] is True


def test_l4_ids_after_an_underscore_are_normalized():
    assert bug.normalize_got("order_7b9e3c1a-0d52-4f10-9a77-1c2b3d4e5f60 failed") == "order_# failed"
    assert bug.normalize_got("user_deadbeef12 at x") == "user_# at x"


def test_l6_an_issue_body_mentions_nobody_and_links_no_issue(tmp_path, monkeypatch):
    home = tmp_path / "explore"
    write_run(home, got="the app crashed: ping @octocat about #12")
    ctx = ctx_for(tmp_path)
    held = ctx.store.read(APP, make(ctx, home, "20261114T091500Z").meta["id"])
    gh = FakeGh(issue_replies())
    monkeypatch.setattr(journey_export, "run_gh", gh)
    bugexport.export(held, bugexport.check(ISSUE), ctx.gate, Audit(), tmp_path)
    body = next(s for a, s in gh.calls if a[:2] == ["issue", "create"])
    assert "\\@octocat" in body and "\\#12" in body and "## What happened" in body and "@octocat" not in body.replace("\\@", "")


def test_l7_a_tools_own_moves_are_not_the_agents_touches(tmp_path):
    from tapwright import daemon as daemonlib
    d = bugged(tmp_path)
    for var in (daemonlib._NESTED, daemonlib._EXPLORING):
        token = var.set(True)
        try:
            d._bug_touch("tap", {"target": {"id": "x"}}, {"id": "x"}, None)
        finally:
            var.reset(token)
    assert list(d._bug_trail) == []
    assert "another bug check" in __import__("tapwright.bugtool", fromlist=["CHECK_BUSY"]).CHECK_BUSY


def test_l9_an_issue_refused_for_a_missing_script_has_its_audit_line(tmp_path, monkeypatch):
    home = tmp_path / "explore"
    write_run(home)
    ctx = ctx_for(tmp_path)
    held = ctx.store.read(APP, make(ctx, home, "20261114T091500Z").meta["id"])
    (held.folder / "repro.json").unlink()
    monkeypatch.setattr(journey_export, "run_gh", FakeGh(issue_replies()))
    audit = Audit()
    with pytest.raises(Refused) as e:
        bugexport.export(held, bugexport.check(ISSUE), ctx.gate, audit, tmp_path)
    assert e.value.code == "not_found" and audit.lines == [f"issue o/r: {held.id}, refused (missing files)"]


def test_l10_a_folder_that_cant_be_read_is_refused_not_an_internal_error(tmp_path):
    home = tmp_path / "explore"
    write_run(home)
    ctx = ctx_for(tmp_path)
    made = make(ctx, home, "20261114T091500Z")
    (made.folder / "finding.json").write_text("{broken", encoding="utf-8")
    write_run(home, "20261114T101500Z")
    with pytest.raises(bugstore.BugFolderError) as e:
        make(ctx, home, "20261114T101500Z")
    assert "can't be read" in str(e.value)


def test_l11_an_observed_bug_with_no_place_key_isnt_checked():
    target = bugcheck.Target("observed", "abc", None, "note", "place_1", [{"tool": "tap", "target": {"id": "x"}}])
    assert bugcheck.why_not(target, "own") == bugcheck.NO_KEY

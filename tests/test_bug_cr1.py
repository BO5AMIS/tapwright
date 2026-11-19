"""PR #21, CodeRabbit cycle 1 on 1.8.5's bug report (AGENT-TOOLS 1.16.2 §29): the check's 60 s, a fresh process per run, one
screen read for a dead end, a malformed script; the export's own staging file, one locked read of the bug, an unreadable
file, a GitHub issue that failed after its commit; the store's containment, its pid lock, independent holds, a report made
again during a check, a rewrite as one version, a script that can't be reread, sources that aged out and an id retention
freed; `--phone` names no test app; the command line's --accounts, --app and exit codes; a field's value in a finding's line;
the bug trail at turn; and the smaller ones. No phone, no network, no real ~/.tapwright."""
import asyncio
import json
import os
import subprocess
import sys
import types
import zipfile
from pathlib import Path, PurePosixPath

import pytest
from test_bugcheck import FRAMES, KEY, Port, check, crash_target
from test_bugexport import ISSUE, Audit, issue_replies, kept
from test_bugstore import APP, NOT_ASKED, SIG, ctx_for, make, write_run
from test_cli_bug import Call, args, home  # noqa: F401 - the fixture
from test_journey import FakeGh

from tapwright import bug, bugcheck, bugexport, bugmake, bugstore, bugtool, cli, client, journey_export, reflex, ui
from tapwright.ceiling import Ceiling
from tapwright.control import Refused

RUN = "20261114T091500Z"


# ---- the check (bugcheck.py) ----

class SlowLastStep(Port):
    """The last step starts inside the run's 60 s and ends after them, with the crash."""

    async def step(self, step):
        done = await super().step(step)
        if self.at == 2:
            self.t += bugcheck.RUN_SECONDS
        return done


def test_what_a_run_shows_after_its_60_s_never_counts():
    assert check(Port(), crash_target())["n"] == 3
    assert check(SlowLastStep(), crash_target())["n"] == 0


class Daemon:
    """Just what DaemonCheck touches: run_tool answers by tool, the session's screen, the clock."""

    def __init__(self, refused=()):
        self.refused, self.calls = set(refused), []
        self.session = types.SimpleNamespace(screen=(393, 852), last_tree=None)
        self.control = types.SimpleNamespace(clock=lambda: 0.0, client=lambda cid: types.SimpleNamespace(app=None))
        self._language = {}

    def _wall(self):
        return 0.0

    def _bundle(self):
        return APP

    async def run_tool(self, cid, name, a, scope=None):
        self.calls.append(name)
        if name in self.refused:
            raise Refused("not_allowed", f"{name} refused")
        return {"text": "ok"}


def test_a_close_that_was_refused_makes_no_run():
    d = Daemon(refused=("close_app",))
    port = bugcheck.DaemonCheck(d, "a", None, APP, lambda lang, tree: KEY)

    async def launched(opened=True):
        return True
    port.phone.launched = launched
    assert asyncio.run(port.reset()) is False
    assert d.calls == ["close_app"]  # the old process is never brought back to the front as a fresh run
    ok = Daemon()
    port = bugcheck.DaemonCheck(ok, "a", None, APP, lambda lang, tree: KEY)
    port.phone.launched = launched
    assert asyncio.run(port.reset()) is True and ok.calls == ["close_app", "open_app"]


def test_the_back_of_a_dead_end_is_read_from_the_screen_that_gave_its_key():
    back = types.SimpleNamespace(point=(20, 60), dismiss=None)
    trees = [types.SimpleNamespace(back=None), types.SimpleNamespace(back=back)]
    port = bugcheck.DaemonCheck(Daemon(), "a", None, APP, lambda lang, tree: KEY)

    async def tree():
        return trees.pop(0)
    port._tree = tree
    assert asyncio.run(port.place_key()) == KEY
    assert asyncio.run(port.has_back()) is False  # the keyed screen has no back; a later read (with one) isn't asked
    fresh = bugcheck.DaemonCheck(Daemon(), "a", None, APP, lambda lang, tree: KEY)
    assert asyncio.run(fresh.has_back()) is True  # no read: can't tell, so not the same dead end


def test_a_script_with_a_step_that_isnt_one_is_not_run_shorter():
    script = {"start": "home", "steps": [{"tool": "tap", "target": {"id": "trips-tab"}}, None]}
    target = bugcheck.target_of({"kind": "crash", "got": "x", "path": []}, {"signature": SIG, "place_key": KEY}, script)
    assert bugcheck.why_not(target, "own") == "its script's step 2 isn't a step"


def test_a_crash_at_the_same_place_with_another_top_symbol_is_not_the_same_bug():
    other = bugcheck.Target("crash", bug.signature("crash", KEY, "the app crashed: EXC_BAD_ACCESS (SIGSEGV)",
                                                   "EXC_BAD_ACCESS (SIGSEGV)", (" 3 * Wayfare renderWeeks +12",)),
                            KEY, "the app crashed: EXC_BAD_ACCESS (SIGSEGV)", "home",
                            crash_target().steps)
    assert FRAMES != (" 3 * Wayfare renderWeeks +12",)
    assert check(Port(), other)["n"] == 0


# ---- the export (bugexport.py) ----

def test_each_zip_export_has_its_own_staging_file_and_a_failed_write_is_a_refusal(tmp_path, monkeypatch):
    ctx, held = kept(tmp_path)
    other = held.folder.parent / f".{held.id}.zip.tmp"  # (another export's staging file, as the old fixed name was)
    other.write_bytes(b"another export's bytes")
    bugexport.export(held, bugexport.check({"to": "zip"}), ctx.gate, Audit(), tmp_path)
    assert other.read_bytes() == b"another export's bytes"
    assert zipfile.is_zipfile(held.folder.parent / f"{held.id}.zip")

    def broken(a, b):
        raise OSError("disk full")
    monkeypatch.setattr(bugexport.os, "replace", broken)
    audit = Audit()
    with pytest.raises(Refused) as e:
        bugexport.export(held, bugexport.check({"to": "zip"}), ctx.gate, audit, tmp_path)
    assert e.value.code == "unsupported" and "couldn't be written" in str(e.value)
    assert sorted(p.name for p in held.folder.parent.iterdir() if p.name.endswith(".tmp")) == [other.name]
    assert audit.lines[-1].endswith("failed (the zip couldn't be written)")


def test_an_unreadable_report_file_is_a_refusal_that_names_no_content(tmp_path):
    ctx, held = kept(tmp_path)
    (held.folder / "report.md").write_bytes(b"\xff\xfe not utf-8 \x80")
    audit = Audit()
    with pytest.raises(Refused) as e:
        bugexport.export(held, bugexport.check({"to": "zip"}), ctx.gate, audit, tmp_path)
    assert "can't be read (UnicodeDecodeError)" in str(e.value) and "not utf-8" not in str(e.value)
    assert audit.lines == [f"zip ~/bugs/{APP}/{held.id}.zip: {held.id}, refused (unreadable)"]


def test_the_picture_and_its_privacy_come_from_one_read_of_the_bug(tmp_path):
    """The caller's copy of the bug says its picture is public; the folder now says private (a rewrite since)."""
    ctx, held = kept(tmp_path, private=False)
    assert (held.folder / "picture.jpg").exists() and held.meta["picture_file"]["private"] is False
    data = json.loads((held.folder / "finding.json").read_text("utf-8"))
    data["bug"]["picture_file"]["private"] = True
    (held.folder / "finding.json").write_text(json.dumps(data), "utf-8")
    bugexport.export(held, bugexport.check({"to": "zip"}), ctx.gate, Audit(), tmp_path)
    with zipfile.ZipFile(held.folder.parent / f"{held.id}.zip") as z:
        assert f"{held.id}/picture.jpg" not in z.namelist()


def test_an_issue_that_failed_after_its_commit_says_so_and_the_retry_uploads_nothing_again(tmp_path, monkeypatch):
    ctx, held = kept(tmp_path)
    gh = FakeGh({**issue_replies(), "issue create": Refused("unsupported", "gh failed: HTTP 502")})
    monkeypatch.setattr(journey_export, "run_gh", gh)
    audit = Audit()
    with pytest.raises(Refused) as e:
        bugexport.export(held, bugexport.check(ISSUE), ctx.gate, audit, tmp_path)
    base = bugexport.issue_base(held)
    assert "repro.json was committed to the qa-reports branch of o/r" in str(e.value) and f"under {base}/" in str(e.value)
    assert "couldn't be made" in str(e.value) and audit.lines[-1] == f"issue o/r: {held.id}, files committed, issue failed " \
                                                                    "(unsupported)"
    script = (held.folder / "repro.json").read_bytes().replace(b"\r\n", b"\n")
    there = {f"api repos/o/r/contents/{base}/repro.json": json.dumps({"type": "file", "sha": bugexport._blob_sha(script)})}
    gh = FakeGh({**there, **issue_replies()})
    monkeypatch.setattr(journey_export, "run_gh", gh)
    said = bugexport.export(held, bugexport.check(ISSUE), ctx.gate, Audit(), tmp_path)
    assert "issue in o/r" in said
    assert not [a for a, _ in gh.calls if "POST" in a or "PATCH" in a]  # nothing uploaded again
    assert any(a[:2] == ["issue", "create"] for a, _ in gh.calls)


# ---- the store (bugstore.py) ----

def test_a_bug_is_read_only_inside_the_stores_own_root(tmp_path):
    home = tmp_path / "explore"
    write_run(home)
    made = make(ctx_for(tmp_path), home, RUN)
    elsewhere = bugstore.BugStore(tmp_path / "other")
    (tmp_path / "other").mkdir()
    try:
        os.symlink(made.folder.parent, tmp_path / "other" / APP, target_is_directory=True)
    except OSError:
        pytest.skip("this system can't make a symbolic link")
    assert elsewhere.read(APP, made.meta["id"]) is None
    assert bugstore.BugStore(tmp_path / "bugs").read(APP, made.meta["id"]) is not None


def _dead_pid() -> int:
    done = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True,
                          check=True)
    return int(done.stdout.strip())


def test_a_live_holders_lock_is_never_taken_for_its_age_and_only_its_holder_releases_it(tmp_path, monkeypatch):
    store = bugstore.BugStore(tmp_path / "bugs")
    folder = store.app_dir(APP)
    folder.mkdir(parents=True)
    lock = folder / bugstore.LOCK_FILE
    lock.write_text(json.dumps({"owner": "someone-else", "pid": os.getpid(), "started": None}) + "\n", "utf-8")
    os.utime(lock, (1_000_000, 1_000_000))  # (very old)
    monkeypatch.setattr(bugstore, "LOCK_WAIT", 0.2)
    with pytest.raises(bugstore.BugFolderError):
        with store.locked(APP):
            pass
    assert json.loads(lock.read_text("utf-8"))["owner"] == "someone-else"
    lock.write_text(json.dumps({"owner": "gone", "pid": _dead_pid(), "started": 1}) + "\n", "utf-8")
    with store.locked(APP):  # a holder whose process is gone loses it
        lock.write_text(json.dumps({"owner": "someone-else", "pid": os.getpid(), "started": None}) + "\n", "utf-8")
    assert lock.exists()  # (not ours any more: not removed)


def test_a_second_hold_that_ends_never_ends_the_first(tmp_path):
    store = bugstore.BugStore(tmp_path / "bugs")
    with store.hold(APP, "bug_" + SIG):
        with store.hold(APP, "bug_" + SIG):
            pass
        assert store._held(APP, "bug_" + SIG)
    assert not store._held(APP, "bug_" + SIG)


def test_a_check_of_a_bug_made_again_meanwhile_is_not_written_into_the_new_one(tmp_path):
    home = tmp_path / "explore"
    write_run(home)
    ctx = ctx_for(tmp_path)
    made = make(ctx, home, RUN)
    held = ctx.store.read(APP, made.meta["id"])
    import shutil
    shutil.rmtree(made.folder)
    again = make(ctx, home, RUN)
    assert again.new and again.meta["id"] == held.id
    assert ctx.store.checked(held, {"n": 3, "of": 3}, ctx.gate) is None
    assert ctx.store.read(APP, held.id).meta["reproduced"] == NOT_ASKED


def test_a_rewrite_that_fails_part_way_leaves_the_whole_old_version(tmp_path, monkeypatch):
    home = tmp_path / "explore"
    write_run(home)
    ctx = ctx_for(tmp_path)
    made = make(ctx, home, RUN)
    held = ctx.store.read(APP, made.meta["id"])
    before = {n: (made.folder / n).read_bytes() for n in bugstore.TEXT_FILES}
    real = bugstore._write

    def failing(path, data):
        if Path(path).name == "finding.json":
            raise OSError("disk full")
        real(path, data)
    monkeypatch.setattr(bugstore, "_write", failing)
    with pytest.raises(OSError):
        ctx.store.checked(held, {"n": 3, "of": 3, "build": "1.5.0 (220)", "when": "x", "reset": "r"}, ctx.gate)
    assert {n: (made.folder / n).read_bytes() for n in bugstore.TEXT_FILES} == before
    assert [p.name for p in made.folder.parent.iterdir() if p.name.startswith(".bug_")] == []
    monkeypatch.setattr(bugstore, "_write", real)
    done = ctx.store.checked(held, {"n": 3, "of": 3, "build": "1.5.0 (220)", "when": "x", "reset": "r"}, ctx.gate)
    assert done is not None and "3 of 3" in (made.folder / "report.md").read_text("utf-8")


def test_a_previous_version_left_by_a_writer_that_died_goes_back(tmp_path):
    home = tmp_path / "explore"
    write_run(home)
    ctx = ctx_for(tmp_path)
    made = make(ctx, home, RUN)
    os.replace(made.folder, made.folder.parent / f".{made.folder.name}.old.dead")
    ctx.store.prune(APP)
    assert ctx.store.read(APP, made.meta["id"]) is not None


def test_a_rewrite_whose_script_cant_be_read_again_writes_nothing(tmp_path):
    home = tmp_path / "explore"
    write_run(home)
    ctx = ctx_for(tmp_path)
    made = make(ctx, home, RUN)
    held = ctx.store.read(APP, made.meta["id"])
    (made.folder / "repro.json").write_text("{ broken", "utf-8")
    page = (made.folder / "report.md").read_bytes()
    with pytest.raises(bugstore.BugFolderError):
        ctx.store.checked(held, {"n": 3, "of": 3}, ctx.gate, scrub=ctx.safe())
    assert (made.folder / "report.md").read_bytes() == page


def test_a_source_that_aged_out_of_the_list_is_not_counted_again(tmp_path, monkeypatch):
    monkeypatch.setattr(bugstore, "SOURCES_KEPT", 2)
    home = tmp_path / "explore"
    ctx = ctx_for(tmp_path)
    for run in ("20261111T091500Z", "20261112T091500Z", "20261113T091500Z"):
        write_run(home, run)
        made = make(ctx, home, run)
    assert made.meta["seen"]["count"] == 3 and len(made.meta["seen"]["sources"]) == 2
    again = make(ctx, home, "20261111T091500Z")  # (no longer in `sources`)
    assert again.meta["seen"]["count"] == 3 and again.meta["seen"]["occurrences"] == 3


def test_a_source_kept_under_a_later_id_is_found_when_retention_freed_the_first(tmp_path):
    home = tmp_path / "explore"
    ctx = ctx_for(tmp_path)
    write_run(home, "20261114T091500Z", place="place_7", place_keys={})
    write_run(home, "20261115T091500Z", place="place_7", place_keys={})
    first = make(ctx, home, "20261114T091500Z")
    second = make(ctx, home, "20261115T091500Z")
    assert second.meta["id"] == first.meta["id"] + "_2"
    import shutil
    shutil.rmtree(first.folder)  # (retention removed `bug_<sig>`)
    again = make(ctx, home, "20261115T091500Z")
    assert not again.new and again.meta["id"] == second.meta["id"]
    third = make(ctx, home, "20261114T091500Z")  # the first source, its bug gone: a free id again
    assert third.new and third.meta["id"] == first.meta["id"]


# ---- the tool (bugtool.py) ----

class Named(bugtool.BugTools):
    def __init__(self, ceiling):
        self.ceiling = ceiling

    async def _app(self, name, partial=False):
        raise Refused("not_found", name)


def test_phone_mode_names_no_test_app_by_itself():
    assert asyncio.run(Named(Ceiling(phone=True))._bug_named(APP)) is False
    assert asyncio.run(Named(Ceiling(phone=True, apps=(APP,)))._bug_named(APP)) is True
    assert asyncio.run(Named(Ceiling(dedicated=True))._bug_named(APP)) is True
    assert asyncio.run(Named(Ceiling())._bug_named(APP)) is False


def test_a_bug_id_two_apps_hold_is_the_one_of_the_calls_app(tmp_path):
    from tapwright import daemon as daemonlib
    home = tmp_path / "explore"
    write_run(home)
    ctx = ctx_for(tmp_path)
    made = make(ctx, home, RUN)
    other = "com.example.other"
    copy = ctx.store.app_dir(other) / made.meta["id"]
    copy.parent.mkdir(parents=True)
    import shutil
    shutil.copytree(made.folder, copy)
    data = json.loads((copy / "finding.json").read_text("utf-8"))
    data["app"] = other
    (copy / "finding.json").write_text(json.dumps(data), "utf-8")

    class Tools(bugtool.BugTools):
        _opened = None

        def _bundle(self):
            return "com.example.front"
    token = daemonlib._APP_HINT.set(other)
    try:
        got = asyncio.run(Tools()._bug_again(ctx, made.meta["id"], False))
    finally:
        daemonlib._APP_HINT.reset(token)
    assert got.finding["app"] == other
    with pytest.raises(Refused) as e:
        asyncio.run(Tools()._bug_again(ctx, made.meta["id"], False))
    assert e.value.code == "ambiguous"


# ---- the command line (cli.py) ----

def test_accounts_with_a_running_daemon_is_refused_not_ignored(home, tmp_path):
    folder = write_run(home / "explore")
    vault = tmp_path / "vault.json"
    vault.write_text(json.dumps({"version": 1, "accounts": {}}), encoding="utf-8")
    said: list[str] = []
    call = Call("New bug: x")
    assert cli.bug_cmd(args(str(folder), "f3", accounts=str(vault)), out=said.append, call=call, alive=lambda: True) == 2
    assert not call.calls and "--accounts is for when no daemon is running" in said[0]
    said.clear()
    assert cli.bug_cmd(args("export", "bug_" + SIG, to="zip", accounts=str(vault)), out=said.append, call=call,
                       alive=lambda: True) == 2
    assert not call.calls


def test_an_export_through_the_daemon_names_its_app_and_its_exit_is_the_exports(home):
    write_run(home / "explore")
    made = make(ctx_for(home), home / "explore", RUN)
    seen: list = []

    def call(tool, a, client_id, timeout=120.0):
        seen.append(client.SCOPE.get("X-Tapwright-Apps"))
        return {"text": f"Known bug (seen 1 time): x\nReproduced: 0 of 3 runs: not reproduced.\nSeen: 1 time.\n"
                        f"Folder: ~/x\nExported: ~/bugs/{APP}/{made.meta['id']}.zip."}
    before = dict(client.SCOPE)
    assert cli.bug_cmd(args("export", made.meta["id"], to="zip", yes=True), out=lambda s: None, call=call,
                       alive=lambda: True) == 0
    assert seen == [APP] and client.SCOPE == before


def test_a_check_that_stopped_is_a_stop_even_when_the_page_keeps_0_of_3():
    text = ("Known bug (seen 2 times): x\nReproduced: 0 of 3 runs: not reproduced.\nSeen: 2 times.\nFolder: ~/x\n"
            "Note: not checked this time (stopped (paused)); the page keeps the last check.")
    assert cli.bug_exit(text) == 2
    assert cli.bug_exit("Known bug: x\nReproduced: 0 of 3 runs: not reproduced.") == 1


# ---- a field's value in a finding (ui.py, exploretool.py) ----

def test_an_unlabeled_fields_value_never_reaches_a_findings_line():
    field = ui.Element("e4", "input", value="PRIVATE-NOTE-7731", rect=(10, 10, 300, 40))
    assert "PRIVATE-NOTE-7731" in field.line()  # an ordinary answer shows what is in it
    token = ui.FIELD_VALUES_HIDDEN.set(True)
    try:
        hidden = field.line()
        labeled = ui.Element("e5", "input", label="Note", value="PRIVATE-NOTE-7731").line()
        empty = ui.Element("e6", "input", value="Search", placeholder="Search").line()
    finally:
        ui.FIELD_VALUES_HIDDEN.reset(token)
    assert "PRIVATE" not in hidden and "value=<field value>" in hidden
    assert labeled.startswith('e5 input "Note"') and "PRIVATE" not in labeled
    assert 'placeholder="Search"' in empty  # an empty field's placeholder stays


def test_explore_and_the_check_read_issues_with_field_values_hidden():
    from tapwright.exploretool import DaemonPhone
    seen: list = []

    class D(Daemon):
        async def run_tool(self, cid, name, a, scope=None):
            seen.append(ui.FIELD_VALUES_HIDDEN.get())
            return {"text": "No issues found."}
    phone = DaemonPhone(D(), "a", None, APP)
    asyncio.run(phone.audit())
    asyncio.run(phone.ux())
    assert seen == [True, True] and ui.FIELD_VALUES_HIDDEN.get() is False


def test_a_frameworks_error_line_is_no_js_error_of_the_app():
    from tapwright import diagnostics
    from tapwright.exploretool import DaemonPhone
    ring = diagnostics.LogRing()
    ring.add(diagnostics.LogEntry(1, "error", "UIKit: constraint broke", False))
    ring.add(diagnostics.LogEntry(2, "error", "TypeError: undefined is not a function", True))

    class D(Daemon):
        _rings = {APP: ring}

        async def _refresh_logs(self, bundle):
            return None

        def _safe_scrub(self, text):
            return text
    d = D()
    d.control = types.SimpleNamespace(clock=lambda: 0.0, client=lambda cid: types.SimpleNamespace(app=("W", APP)))
    watch = asyncio.run(DaemonPhone(d, "a", None, APP).watch(0))
    assert watch.errors == ("TypeError: undefined is not a function",)


# ---- the smaller ones ----

def test_after_a_prompt_is_answered_the_screen_read_next_is_no_permission_prompt(tmp_path):
    from explorefakes import build
    from test_explore_safety import red_team
    from test_explore_safety import run as explore_run
    phone = red_team()
    phone.goes[("prompt", "ask")] = "asking"
    phone.alert_to["asking"] = "prompt"
    explorer = build(tmp_path, phone)[0]
    seen: list = []
    real = explorer._look

    async def look(*a, **k):
        got = await real(*a, **k)
        if got.tree.alert is None:
            seen.append(explorer._system_now)
        return got
    explorer._look = look
    explore_run(explorer)
    assert phone.answered == ["Don’t Allow"] and seen and "permission prompt" not in seen

def test_a_sliders_percent_is_no_loading_screen():
    tree = ui.Tree([ui.Element("e1", "slider", label="Volume", value="50%", rect=(10, 100, 300, 30)),
                    ui.Element("e2", "button", label="Done", rect=(10, 200, 100, 44))], app=("Wayfare", APP))
    assert reflex.starting(tree, APP) is None
    loading = ui.Tree([ui.Element("e1", "text", label="Downloading 45%", rect=(10, 100, 300, 30))], app=("Wayfare", APP))
    assert reflex.starting(loading, APP) == "loading"


def test_an_explore_report_that_isnt_an_object_is_bad_params(tmp_path):
    folder = write_run(tmp_path / "explore")
    (folder / "report.json").write_text("[]", "utf-8")
    with pytest.raises(bugmake.BugError) as e:
        bugmake.read_run(tmp_path / "explore", folder, "f3")
    assert e.value.code == "bad_params"


def test_two_opens_of_a_link_that_lost_part_of_it_both_stay():
    trail = [{"tool": "open_url", "ok": True, "app": APP, "place": "place_1", "link": "wayfare://trip/[id]",
              "link_cut": False},
             {"tool": "open_url", "ok": True, "app": APP, "place": "place_2", "link": "wayfare://trip/[id]",
              "link_cut": True}]
    got = bugmake.observed_path(trail, APP, "place_2")
    assert len(got.path) == 2 and got.links_cut == (1,)
    same = [dict(trail[0]), dict(trail[0], place="place_2")]
    assert len(bugmake.observed_path(same, APP, "place_2").path) == 1  # (nothing lost: one step, as before)


def test_a_start_is_never_a_later_steps_place():
    trail = [{"tool": "tap", "ok": True, "app": APP, "place": None, "after": None, "target": {"id": "a"}},
             {"tool": "tap", "ok": True, "app": APP, "place": "place_5", "after": "place_6", "target": {"id": "b"}}]
    got = bugmake.observed_path(trail, APP, "place_6")
    assert got.path_from is None and len(got.path) == 2


def test_this_computers_home_with_a_space_in_its_name_is_cut_whole(monkeypatch):
    class Home:
        @staticmethod
        def home():
            return PurePosixPath("/Users/Jane Doe")
    monkeypatch.setattr(bug, "Path", Home)
    assert bug.strip_paths("open /Users/Jane Doe") == "open ~"
    assert bug.strip_paths("open /Users/Jane Doe: done") == "open ~: done"
    assert "Doe" not in bug.strip_paths(r"C:\Users\Jane Doe\proj\Trip.swift:48 renderDays")

    class WinHome:
        @staticmethod
        def home():
            return Path(r"C:\Users\Jane Doe") if os.name == "nt" else PurePosixPath("/home/Jane Doe")
    monkeypatch.setattr(bug, "Path", WinHome)
    shown = bug.strip_paths(f"opened {WinHome.home()} then quit")
    assert shown == "opened ~ then quit"


# ---- the daemon ----

def test_a_dropped_line_forgets_the_thin_calls_place(tmp_path):
    from test_daemon_bug import bugged
    d = bugged(tmp_path)
    d._thin_place = (("com.example", "place_3"), d.control.generation)
    d.lost("gone")
    assert d._thin_place is None


def test_the_bug_trail_takes_the_place_the_touch_got_its_turn_on(tmp_path):
    from test_daemon import finish, run, started
    from test_daemon_bug import bugged
    from test_daemon_explore import BUNDLE

    async def go():
        d = bugged(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "look", {})
        d._bug_aim = lambda cid, name, a: ((BUNDLE, "place_before_the_turn"), {"id": "go-form"})
        await d.run_tool("a", "tap", {"target": {"id": "go-form"}})
        entry = d._bug_trail[-1]
        assert entry["place"] != "place_before_the_turn" and entry["target"] == {"point": True}, entry
        await finish(stop, task)
    run(go)


def test_a_bug_checks_floor_reads_the_callers_policy():
    from tapwright import server, thin
    assert "bug_report" in server.FLOOR_TOOLS and thin.floor_call("bug_report", {})

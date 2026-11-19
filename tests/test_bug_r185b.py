"""Review R185b H1 (1.8.5): a short value typed in the session ("2026", "place", the build number, a run of a bug id's hex)
that also occurs in what the tool writes itself (dates, place names, builds, ids, the page's own words) never blocks a bug,
a check or an export, and never turns a kept bug's dates or id into `<typed value>`. A typed value in the app's own text is
still hidden everywhere. No phone and no real ~/.tapwright: everything is in tmp_path."""
import json
import zipfile
from pathlib import Path

import pytest

from tapwright import bug, bugexport, bugmake, bugstore, findings
from tapwright.control import Refused
from test_bugstore import APP, NOT_ASKED, SECRET, TYPED, ctx_for, every_text, make, store_with, write_run

RUN = "20261114T091500Z"
CHECK = {"n": 1, "of": 1, "build": "1.5.0 (220)", "when": "2026-11-19T00:00:00.000Z", "reset": "relaunch only, data kept"}


class Audit:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, detail: str) -> bool:
        self.lines.append(detail)
        return True


def texts(folder) -> dict[str, str]:
    return {n: (folder / n).read_text("utf-8") for n in bugstore.TEXT_FILES}


@pytest.mark.parametrize("typed", ["2026", "place", "220", "Crash on"])
def test_a_typed_value_the_tool_also_writes_never_blocks_the_bug_and_its_text_stays(tmp_path, typed):
    home = tmp_path / "explore"
    write_run(home, got=f"the app crashed after {typed} taps: EXC_BAD_ACCESS")
    keep = store_with(SECRET)
    keep.remember_typed(typed)
    ctx = ctx_for(tmp_path, keep)
    made = make(ctx, home, RUN)
    page = (made.folder / "report.md").read_text("utf-8")
    data = json.loads((made.folder / "finding.json").read_text("utf-8"))
    # the tool's own: the dates, the place, the build, the id, as they were
    assert data["when"] == "2026-11-14T09:21:07.512Z" and data["bug"]["seen"]["last"] == "2026-11-14T09:21:07.512Z"
    assert data["place"] == "place_4" and data["build"] == "220" and data["bug"]["seen"]["builds"] == ["1.5.0 (220)"]
    assert "last 2026-11-14" in page and "- App: com.example.wayfare 1.5.0 (220)" in page
    # the app's own text: the typed value is hidden there
    assert f"after {typed} taps" not in every_text(made.folder)
    assert "after <typed value> taps" in data["got"] and "after <typed value> taps" in page
    answer = bugmake.answer(made, ctx.scrub, home=tmp_path)
    assert "Seen: 1 time, 2026-11-14, build 1.5.0 (220)." in answer and f"after {typed} taps" not in answer
    # a check rewrites it with its real dates and build; the bug stays readable
    held = ctx.store.read(APP, made.meta["id"])
    done = ctx.store.checked(held, CHECK, ctx.gate, scrub=ctx.safe())
    again = ctx.store.read(APP, made.meta["id"])
    assert done is not None and again is not None
    assert again.finding["when"] == "2026-11-14T09:21:07.512Z" and again.meta["seen"]["first"] == "2026-11-14T09:21:07.512Z"
    assert again.meta["reproduced"] == CHECK and again.finding["build"] == "220"
    # and every export goes
    audit = Audit()
    bugexport.export(again, bugexport.check({"to": "zip"}), ctx.gate, audit, tmp_path)
    with zipfile.ZipFile(again.folder.parent / f"{again.id}.zip") as z:
        sent = b"".join(z.read(n) for n in z.namelist()).decode("utf-8")
    assert f"after {typed} taps" not in sent and "2026-11-14T09:21:07.512Z" in sent
    assert audit.lines[-1].endswith("picture not sent")


def test_a_typed_value_inside_the_bug_id_never_makes_the_bug_unreadable(tmp_path):
    home = tmp_path / "explore"
    write_run(home)
    bug_id = make(ctx_for(tmp_path / "first"), home, RUN).meta["id"]  # (the id is the signature's: the same here)
    keep = store_with(SECRET)
    keep.remember_typed(bug_id[4:10])  # six hex characters of the id
    keep.remember_typed(bug_id[-5:])
    ctx = ctx_for(tmp_path, keep)
    made = make(ctx, home, RUN)
    assert made.meta["id"] == bug_id
    held = ctx.store.read(APP, bug_id)
    assert held is not None and held.meta["signature"] == bug_id[4:]
    assert ctx.store.checked(held, CHECK, ctx.gate, scrub=ctx.safe()) is not None
    again = ctx.store.read(APP, bug_id)
    assert again is not None and again.meta["id"] == bug_id and again.meta["reproduced"] == CHECK
    # a sighting from another run counts, and the bug is still its own
    write_run(home, "20261115T091500Z", when="2026-11-15T09:21:07.512Z")
    later = make(ctx, home, "20261115T091500Z")
    assert not later.new and later.meta["id"] == bug_id and later.meta["seen"]["count"] == 2
    assert ctx.store.read(APP, bug_id).meta["seen"]["last"] == "2026-11-15T09:21:07.512Z"


def test_a_check_after_a_year_was_typed_keeps_the_dates_so_retention_removes_the_oldest(tmp_path):
    home = tmp_path / "explore"
    keep = store_with(SECRET)
    ctx = ctx_for(tmp_path, keep, kept=2)
    write_run(home, "20261114T091500Z", place="place_1", place_keys={"place_1": {"signature": "a" * 16}},
              when="2026-11-14T09:00:00.000Z")
    newer = make(ctx, home, "20261114T091500Z")
    write_run(home, "20261112T091500Z", place="place_2", place_keys={"place_2": {"signature": "b" * 16}},
              when="2026-11-12T09:00:00.000Z")
    older = make(ctx, home, "20261112T091500Z")
    keep.remember_typed("2026")  # the agent types a year into a date field afterwards
    held = ctx.store.read(APP, older.meta["id"])
    ctx.store.checked(held, CHECK, ctx.gate, scrub=ctx.safe())
    assert ctx.store.read(APP, older.meta["id"]).meta["seen"]["last"] == "2026-11-12T09:00:00.000Z"
    write_run(home, "20261116T091500Z", place="place_3", place_keys={"place_3": {"signature": "c" * 16}},
              when="2026-11-16T09:00:00.000Z")
    third = make(ctx, home, "20261116T091500Z")
    kept = [k.id for k in ctx.store.bugs(APP)]
    assert kept == [third.meta["id"], newer.meta["id"]]  # the oldest by its real date went


def test_a_typed_value_written_into_the_bugs_own_text_still_refuses_the_export(tmp_path):
    """The export gates the app's text of a bug as the tool built it: a typed value put into `got` (and the page built
    from it, so the files agree) is refused; a page that doesn't agree with its finding is gated whole."""
    home = tmp_path / "explore"
    write_run(home)
    keep = store_with(SECRET)
    keep.remember_typed(TYPED)
    ctx = ctx_for(tmp_path, keep)
    made = make(ctx, home, RUN)
    folder = made.folder
    data = json.loads((folder / "finding.json").read_text("utf-8"))
    data["got"] = f"the app crashed after {TYPED}"
    meta = data["bug"]
    finding = {k: v for k, v in data.items() if k != "bug"}
    page = bug.markdown(finding, meta, None)
    (folder / "finding.json").write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", "utf-8", newline="\n")
    (folder / "report.md").write_text(page, "utf-8", newline="\n")
    (folder / "report.html").write_text(bug.page_html(page), "utf-8", newline="\n")
    assert bugstore.composed(texts(folder)) is not None  # (the files agree: the app text is what is gated)
    held = ctx.store.read(APP, made.meta["id"])
    with pytest.raises(Refused) as e:
        bugexport.export(held, bugexport.check({"to": "zip"}), ctx.gate, Audit(), tmp_path)
    assert "found in" in str(e.value) and TYPED not in str(e.value)
    # a page edited by hand (it no longer agrees with its finding) is gated whole
    data["got"] = "the app crashed"
    (folder / "finding.json").write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", "utf-8", newline="\n")
    assert bugstore.composed(texts(folder)) is None
    with pytest.raises(Refused):
        bugexport.export(held, bugexport.check({"to": "zip"}), ctx.gate, Audit(), tmp_path)


def test_a_bug_with_a_private_picture_left_out_exports_after_a_year_was_typed(tmp_path):
    home = tmp_path / "explore"
    write_run(home)
    keep = store_with(SECRET)
    ctx = ctx_for(tmp_path, keep)
    made = make(ctx, home, RUN, with_picture=True)
    assert made.meta["picture_included"] is True
    keep.remember_typed("2026")
    held = ctx.store.read(APP, made.meta["id"])
    said = bugexport.export(held, bugexport.check({"to": "zip"}), ctx.gate, Audit(), tmp_path)
    assert said.startswith("Exported:") and "without its private picture" in said


def test_the_finding_labels_still_refuse_a_vault_value_but_not_a_typed_one():
    keep = store_with(SECRET, "Hunter2Pass99")
    keep.remember_typed("220")
    ctx = bugmake.Context(bugstore.BugStore(Path("unused")), keep.bug_scrub, keep.bug_gate,
                          lambda app: None)
    raw = {"id": "f1", "kind": "crash", "app": APP, "app_version": "1.5.0", "build": "220", "tool": "tapwright",
           "tool_version": "1.8.5", "when": "2026-11-14T09:00:00.000Z", "device": {"model": "iPhone16,1", "os": "iOS 26"},
           "expected": "runs", "got": "crashed", "crash": {"reason": "boom", "frames": []}}
    assert bugmake.clean(raw, ctx.safe(), True, ctx.label_check())["build"] == "220"
    with pytest.raises(findings.FindingError):
        bugmake.clean({**raw, "build": "Hunter2Pass99"}, ctx.safe(), True, ctx.label_check())


# ---- M1: the clients wait as long as the check can take ----

def test_the_bug_report_timeouts_cover_each_runs_two_launch_waits_and_agree():
    from tapwright import bugcheck, exploretool, mcp_server, thin
    assert bugcheck.LAUNCH_WAITS == 2 * exploretool.LAUNCH_WAIT
    per_run = bugcheck.RUN_SECONDS + 60.0 + 2 * exploretool.LAUNCH_WAIT  # a run, its reset and way, two launch waits
    for args, runs in (({"verify": 3}, 3), ({"finding": "f3"}, bugcheck.RUNS_DEFAULT), ({"verify": 5}, 5),
                       ({"from": "failure", "export": {"to": "zip"}}, bugcheck.RUNS_DEFAULT)):
        want = 120.0 + runs * per_run + (600.0 if "export" in args else 0.0)
        assert bugcheck.timeout_for(args) == want
        assert mcp_server._timeout("bug_report", args) == want
        assert thin.default_timeout("bug_report", args) == want
    assert thin.BUG_RUN == per_run and thin.BUG_RUNS == bugcheck.RUNS_DEFAULT


# ---- L2: a backup a failed removal left part-deleted never comes back as the bug ----

def test_a_part_deleted_backup_is_never_put_back_and_the_bug_can_be_made_again(tmp_path, monkeypatch):
    import os
    import shutil
    home = tmp_path / "explore"
    write_run(home)
    ctx = ctx_for(tmp_path)
    made = make(ctx, home, RUN)
    bug_id = made.meta["id"]
    real_rmtree = shutil.rmtree

    def stopped(path, ignore_errors=False):  # a reader held a file: the removal stops after finding.json
        try:
            os.unlink(os.path.join(path, "finding.json"))
        except OSError:
            if not ignore_errors:
                raise
    monkeypatch.setattr(bugstore.shutil, "rmtree", stopped)
    ctx.store.checked(ctx.store.read(APP, bug_id), CHECK, ctx.gate, scrub=ctx.safe())
    monkeypatch.setattr(bugstore.shutil, "rmtree", real_rmtree)
    left = [p.name for p in made.folder.parent.iterdir() if p.name.startswith(f".{bug_id}.")]
    assert left and all(".dead." in n for n in left)  # (renamed before its removal)
    # a part-deleted backup with the old name too (from before this fix, or a rename that went through partly)
    part = made.folder.parent / f".{bug_id}.old.abcd1234"
    part.mkdir()
    (part / "report.md").write_text("# half", "utf-8")
    real_rmtree(made.folder)  # the bug is removed (retention, or a person)
    ctx.store.prune(APP)
    assert not (made.folder).exists() and not part.exists()
    assert not any(p.name.startswith(f".{bug_id}.") for p in made.folder.parent.iterdir())
    again = make(ctx, home, RUN)  # the same finding makes its bug again: never "exists but can't be read"
    assert again.new and ctx.store.read(APP, bug_id) is not None


def test_a_whole_backup_still_goes_back(tmp_path):
    import os
    home = tmp_path / "explore"
    write_run(home)
    ctx = ctx_for(tmp_path)
    made = make(ctx, home, RUN, with_picture=True)
    old = made.folder.parent / f".{made.folder.name}.old.0000"
    os.replace(made.folder, old)
    (old / "picture.jpg").unlink()  # its picture is gone: not whole
    ctx.store.prune(APP)
    assert ctx.store.read(APP, made.meta["id"]) is None and not old.exists()
    made = make(ctx, home, RUN, with_picture=True)
    os.replace(made.folder, old)
    ctx.store.prune(APP)
    assert ctx.store.read(APP, made.meta["id"]) is not None and not old.exists()


# ---- L3: the daemon's first use of the bugs never waits on another process's lock ----

def test_the_first_retention_pass_never_waits_for_a_held_lock(tmp_path):
    import time
    from tapwright import pidlock
    from tapwright.bugtool import BugTools
    home = tmp_path / "explore"
    ctx = ctx_for(tmp_path)
    for i in range(2):
        write_run(home, f"20261114T09150{i}Z", got=f"crash {chr(97 + i)}",
                  place_keys={"place_4": {"signature": f"{'d' * 15}{i}"}})
        make(ctx, home, f"20261114T09150{i}Z")
    tools = BugTools()
    tools.bug_home = tmp_path / "bugs"
    lock = ctx.store.app_dir(APP) / bugstore.LOCK_FILE
    token = pidlock.take(lock)  # the command line holds the app's bugs (an export writing a zip)
    assert token is not None
    try:
        began = time.monotonic()
        bugs = tools._bugs()
        assert time.monotonic() - began < 1.0
        assert tools._bug_pruned is False  # (left for the next call)
    finally:
        pidlock.release(lock, token)
    assert len(bugs.bugs(APP)) == 2
    tools._bugs()
    assert tools._bug_pruned is True

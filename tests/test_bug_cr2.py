"""PR #21, CodeRabbit cycle 2 on 1.8.5's bug report: a half-done swap mended under the lock before any lookup, a backup that
can't be put back kept; a check that couldn't run keeps one finished meanwhile; a picture of an earlier report refused; a
picture's privacy from what was in front as it was taken. No phone, no network, no real ~/.tapwright."""
import os
import shutil

import pytest
from test_bugstore import APP, NOT_ASKED, ctx_for, make, write_run

from tapwright import bugmake, bugstore

RUN = "20261114T091500Z"
DONE = {"n": 3, "of": 3, "build": "1.5.0 (220)", "when": "2026-11-19T07:00:00.000Z", "reset": "relaunch only, data kept"}


def made_bug(tmp_path):
    home = tmp_path / "explore"
    write_run(home)
    ctx = ctx_for(tmp_path)
    return home, ctx, make(ctx, home, RUN)


def test_a_backup_that_cant_be_put_back_is_kept(tmp_path, monkeypatch):
    _, ctx, made = made_bug(tmp_path)
    old = made.folder.parent / f".{made.folder.name}.old.dead"
    os.replace(made.folder, old)

    def refused(a, b):
        raise PermissionError("in use")
    monkeypatch.setattr(bugstore.os, "replace", refused)
    ctx.store.prune(APP)
    assert old.is_dir() and (old / "finding.json").is_file()
    monkeypatch.undo()
    ctx.store.prune(APP)  # the next lock puts it back
    assert ctx.store.read(APP, made.meta["id"]) is not None and not old.exists()


def test_a_half_done_swap_is_mended_before_the_same_finding_is_reported_again(tmp_path):
    home, ctx, made = made_bug(tmp_path)
    ctx.store.checked(ctx.store.read(APP, made.meta["id"]), DONE, ctx.gate)
    os.replace(made.folder, made.folder.parent / f".{made.folder.name}.old.dead")  # (a writer died between its renames)
    again = make(ctx, home, RUN)
    assert not again.new and again.meta["reproduced"] == DONE  # the first report, its check kept: not a new bug over it


def test_a_check_that_couldnt_run_keeps_a_check_finished_meanwhile(tmp_path):
    _, ctx, made = made_bug(tmp_path)
    stale = ctx.store.read(APP, made.meta["id"])
    assert stale.meta["reproduced"] == NOT_ASKED
    ctx.store.checked(stale, DONE, ctx.gate)  # another check finished
    busy = {"checked": False, "why": "stopped (busy): an explore or another bug check is running on this phone"}
    got = ctx.store.checked(stale, busy, ctx.gate)
    assert got is not None and got.meta["reproduced"] == DONE
    assert ctx.store.read(APP, made.meta["id"]).meta["reproduced"] == DONE
    assert "3 of 3" in (made.folder / "report.md").read_text("utf-8")


def test_a_picture_of_an_earlier_report_is_never_added_to_one_made_again(tmp_path):
    home, ctx, made = made_bug(tmp_path)
    held = ctx.store.read(APP, made.meta["id"])
    picture = bugmake.picture_again(home, held.meta["picture_source"])
    shutil.rmtree(made.folder)
    make(ctx, home, RUN)
    with pytest.raises(bugstore.BugFolderError):
        ctx.store.add_picture(held, picture, ctx.gate)
    assert not (made.folder / "picture.jpg").exists()


def test_a_share_sheet_in_front_as_the_picture_is_taken_makes_it_private(tmp_path):
    from explorefakes import build, identity
    from test_explore_fill import forms, run
    phone = forms()
    phone.errors[("home", "go-form")] = ("TypeError: boom",)
    real = phone.picture

    async def picture():
        phone.picture_system = "share sheet"  # (came up after the last read)
        return await real()
    phone.picture = picture
    explorer, maps = build(tmp_path, phone, who=identity(test_app=True))
    found = next(f for f in run(explorer, maps).findings if f.kind == "js_error")
    assert found.picture.private is True and found.system == "share sheet"


def test_no_picture_when_the_screen_as_it_was_taken_cant_be_read():
    from test_daemon import finish, make as make_daemon, run, started

    from tapwright import exploretool

    async def go():
        d = make_daemon()
        d.session.foreground = "com.example.app"
        stop, task = await started(d)
        phone = exploretool.DaemonPhone(d, "a", None, "com.example.app")
        assert await phone.picture() is not None and phone.picture_system is None

        async def broken(*a, **k):
            raise OSError("no tree")
        d.session.snapshot = broken
        assert await phone.picture() is None
        await finish(stop, task)
    run(go)

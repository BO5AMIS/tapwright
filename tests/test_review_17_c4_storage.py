"""Final PR #12 review, H3/H4: unknown grant restrictions and redaction at every scheduled-run output boundary.
Synthetic files and phone sessions only.
"""
import base64
import datetime
import json
import os
import plistlib
from pathlib import Path

import pytest

from test_daemon import finish, run, started
from test_grants import APP, Clock, grant as file_grant, write_policy
from test_review_17_c1 import AdmissionPhone, admission_deps, calls_of, chat, daemon_with, grant

from tapwright import accounts, auditlog, grants, policy, schedulerun, schedules, store
from tapwright.ceiling import Ceiling
from tapwright.control import Refused




def test_h3_restriction_queries_distinguish_unreadable_from_a_valid_empty_file(tmp_path):
    path = tmp_path / "policy.json"
    write_policy(path, file_grant(draftsOnly=True, route="post_update"))
    book = grants.Book(path, None, now=Clock())
    queries = (lambda: book.drafts_only_for(APP, ("post_update",)),
               lambda: book.drafts_only_touch(APP, "Post", ("post_update",)),
               lambda: book.naming_route("post_update"))
    assert all(query() == "wayfare_posting" for query in queries)
    path.write_text("{", encoding="utf-8")
    for query in queries:
        with pytest.raises(OSError, match="can't be read"):
            query()
    write_policy(path)  # A person's valid edit that removes the grants takes effect immediately.
    assert all(query() is None for query in queries)


SECRET = "synthetic_username_170"


def scheduled(tmp_path, monkeypatch, *, missing_caption=False):
    monkeypatch.setattr(store, "HOME", tmp_path / "home")
    vault = tmp_path / "vault.json"
    vault.write_text(json.dumps({"version": 2, "vault": {"tester": {"kind": "account", "username": SECRET,
                                                                   "password": "synthetic_password_170"}}}), "utf-8")
    phone = AdmissionPhone(0)
    deps, _ = admission_deps(tmp_path, phone)
    deps.runs = tmp_path / SECRET / "runs"
    s = schedules.Schedule(SECRET, SECRET, {"caption": "@missing.txt"} if missing_caption else {},
                           "09:00 daily", None, accounts=str(vault))
    schedules.write(s, deps.where)
    routes = store.HOME / "maps" / "com.example.wayfare" / "routes"
    routes.mkdir(parents=True)
    (routes / f"{SECRET}.json").write_text("{}", "utf-8")
    return phone, deps, vault, s


def assert_outputs_redacted(out, deps):
    assert SECRET not in out.text and SECRET not in out.result
    assert SECRET not in "\n".join(auditlog.read_lines(deps.audit_folder))
    # The path retained for opening the report is operational; the printed representation is safe.
    path = Path(out.report)
    assert path.is_file() and SECRET in str(path)
    raw_json = path.read_text("utf-8")
    markdown = path.with_suffix(".md").read_text("utf-8")
    assert SECRET not in raw_json and SECRET not in markdown
    return json.loads(raw_json)


@pytest.mark.parametrize("missing_caption", [False, True])
def test_h4_all_report_metadata_and_displayed_paths_are_redacted(tmp_path, monkeypatch, missing_caption):
    phone, deps, vault, s = scheduled(tmp_path, monkeypatch, missing_caption=missing_caption)

    def call(*args, **kwargs):
        # Names in the shared audit may have been written by another process with a different vault.
        audit = auditlog.AuditLog(deps.audit_folder, now=deps.now)
        audit.write("grant", name=f"grant_{SECRET}")
        audit.write("vault", name=f"identity_{SECRET}")
        return {"text": f"Arrived for {SECRET}", "image": base64.b64encode(b"synthetic image").decode("ascii")}
    phone.call = call
    out = schedulerun.run(s.name, deps=deps)
    # The scheduler cannot rewrite another process's already-written audit line. Its own lines must be redacted.
    for line in auditlog.read_lines(deps.audit_folder):
        item = json.loads(line)
        if item["kind"] == "schedule":
            assert SECRET not in line
    assert SECRET not in out.text and SECRET not in out.result
    path = Path(out.report)
    assert path.is_file() and SECRET in str(path)
    raw_json, markdown = path.read_text("utf-8"), path.with_suffix(".md").read_text("utf-8")
    assert SECRET not in raw_json and SECRET not in markdown
    data = json.loads(raw_json)
    assert data["schedule"] == "<tester's username>" and data["route"] == "<tester's username>"
    if missing_caption:
        assert out.status == "failed" and data["picture"] is None
    else:
        assert out.status == "done" and path.with_suffix(".png").read_bytes() == b"synthetic image"
        assert data["picture"].startswith("<tester's username>-")
        assert data["grants_used"] == {"grant_<tester's username>": 1}
        assert data["vault_names_used"] == ["identity_<tester's username>"]


@pytest.mark.parametrize("report_fails", [False, True])
def test_h4_outer_error_paths_keep_the_loaded_redactor_even_after_the_vault_disappears(
        tmp_path, monkeypatch, report_fails):
    phone, deps, vault, s = scheduled(tmp_path, monkeypatch)

    def broken(schedule):
        vault.unlink()
        raise KeyError(SECRET)
    monkeypatch.setattr(schedules, "read_values", broken)
    if report_fails:
        def unavailable(*args, **kwargs):
            raise OSError("synthetic disk failure")
        monkeypatch.setattr(schedulerun, "finish", unavailable)
    out = schedulerun.run(s.name, deps=deps)
    assert out.status == "failed" and out.result.startswith(schedulerun.INTERNAL)
    if report_fails:
        assert out.report is None and SECRET not in out.text
        assert SECRET not in "\n".join(auditlog.read_lines(deps.audit_folder))
    else:
        assert_outputs_redacted(out, deps)


def test_h4_no_trigger_error_redacts_the_schedule_name(tmp_path, monkeypatch):
    _, deps, _, s = scheduled(tmp_path, monkeypatch)
    out = schedulerun.run(s.name, if_triggered=True, deps=deps)
    assert out.status == "failed" and "no trigger" in out.text and SECRET not in out.text


def test_h4_an_unexpected_redactor_error_withholds_the_schedule_name(tmp_path, monkeypatch):
    _, deps, _, s = scheduled(tmp_path, monkeypatch)

    def broken(*args):
        raise OSError(SECRET)
    monkeypatch.setattr(schedulerun, "early_scrubber", broken)
    out = schedulerun.run(s.name, deps=deps)
    assert out.status == "failed" and out.result.startswith(schedulerun.INTERNAL)
    assert_outputs_redacted(out, deps)


def test_h4_report_writer_redacts_result_and_dynamic_grant_keys_as_a_backstop(tmp_path, monkeypatch):
    _, deps, vault, s = scheduled(tmp_path, monkeypatch)
    loaded = accounts.load(vault)
    assert loaded.problem is None
    monkeypatch.setattr(schedulerun, "used_since", lambda *args: ({SECRET: 2}, [SECRET]))
    out = schedulerun.finish(s.name, deps, "failed", f"failed: {SECRET}", s, deps.now(),
                              text=f"Details for {SECRET}", scrub=loaded.scrub)
    data = assert_outputs_redacted(out, deps)
    assert data["grants_used"] == {"<tester's username>": 2}
    assert data["vault_names_used"] == ["<tester's username>"]


def test_report_counts_include_uses_in_the_runs_first_millisecond(tmp_path):
    began = datetime.datetime(2026, 11, 14, 10, 0, 0, 123456, tzinfo=datetime.timezone.utc)
    deps = schedulerun.Deps(audit_folder=tmp_path / "audit")
    earlier = auditlog.AuditLog(deps.audit_folder, now=lambda: began.replace(microsecond=122999))
    earlier.write("grant", name="previous_run")
    current = auditlog.AuditLog(deps.audit_folder, now=lambda: began.replace(microsecond=123789))
    current.write("grant", name="posting")
    current.write("vault", name="tester")
    assert schedulerun.used_since(deps, began) == ({"posting": 1}, ["tester"])


def test_an_unreadable_trigger_folder_keeps_its_snapshot_and_never_retriggers_old_files(tmp_path, monkeypatch):
    inbox, where = tmp_path / "inbox", tmp_path / "schedules"
    inbox.mkdir()
    old = inbox / "old.txt"
    old.write_text("synthetic", "utf-8")
    s = schedules.Schedule("files", "route", {}, None, {"file": str(inbox)})
    now_ns = old.stat().st_mtime_ns + 10_000_000_000
    assert schedules.new_files(s, where, now_ns=now_ns) == []
    before = schedules.seen_of(s.name, where).read_bytes()
    scan = os.scandir

    def unreadable(path):
        if Path(path) == inbox:
            raise PermissionError("synthetic unavailable folder")
        return scan(path)
    with monkeypatch.context() as patch:
        patch.setattr(schedules.os, "scandir", unreadable)
        assert schedules.new_files(s, where, now_ns=now_ns) == []
        assert schedules.seen_of(s.name, where).read_bytes() == before
    assert schedules.new_files(s, where, now_ns=now_ns) == []
    fresh = inbox / "new.txt"
    fresh.write_text("synthetic new", "utf-8")
    assert schedules.new_files(s, where, now_ns=fresh.stat().st_mtime_ns + 10_000_000_000) == ["new.txt"]
    old.unlink()
    fresh.unlink()
    assert schedules.new_files(s, where, now_ns=now_ns) == []
    assert json.loads(schedules.seen_of(s.name, where).read_text("utf-8")) == {}  # A successful empty scan counts.


@pytest.mark.parametrize("trigger", [False, True])
def test_macos_python_fallback_is_an_executable_vector(tmp_path, monkeypatch, trigger):
    monkeypatch.setattr(schedules.shutil, "which", lambda _: None)
    command = ["/Applications/Synthetic Python/bin/python3", "-m", "tapwright"]
    scheduler = schedules.Scheduler(platform="darwin", command=command)
    s = schedules.Schedule("posting", "post", {}, None if trigger else "09:00 daily",
                           {"file": str(tmp_path)} if trigger else None)
    instructions = scheduler.instructions(s)
    doc = plistlib.loads(instructions[instructions.index("<?xml"):].encode("utf-8"))
    expected = [*command, "run-schedule", s.name, *(["--if-triggered"] if trigger else [])]
    assert doc["ProgramArguments"] == expected

"""Seeing grants (docs/AGENT-TOOLS.md §19.7, §19.5, §16.1): `tapwright grants`, `--revoke-all`, `grants unlock`, the
`Grants:` line of session_report, and the lines doctor adds."""
import datetime
import json
import os
import time

import pytest

from tapwright import cli, grants, grantscmd, pidlock, report, store
from tapwright.auditlog import AuditLog

TZ = datetime.timezone(datetime.timedelta(hours=3))
NOW = datetime.datetime(2026, 11, 14, 10, 0, tzinfo=TZ)
APP = "com.example.app"


def make(tmp_path, items, version=2):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"version": version, "confirm": ["Post"], "block": [], "grants": items}), encoding="utf-8")
    audit = AuditLog(tmp_path / "audit", now=lambda: NOW.astimezone(datetime.timezone.utc))
    return path, audit, grants.Book(path, audit, now=lambda: NOW)


POST = {"name": "wayfare_posting", "app": APP, "words": ["Post", "Send"], "route": "post_update", "perDay": 3,
        "perHour": 2, "until": "2026-11-21", "quiet": {"from": "22:00", "to": "07:00"}}
OLD = {"name": "ended_one", "app": APP, "words": ["Delete"], "until": "2026-11-10", "draftsOnly": False}


def use(book, name="wayfare_posting", times=1):
    [grant] = [g for g in book.load().grants if g.name == name]
    for _ in range(times):
        got = book._claim(grant, "post_update")
        book.audit.write("grant", app=APP, name=name, detail=f'tap "Post" ({got.today} of {grant.per_day} today)')


def run(tmp_path, **kw):
    return grantscmd.run_grants(str(tmp_path / "policy.json"), audit_folder=tmp_path / "audit", now=lambda: NOW, **kw)


def test_the_listing_has_every_grant_todays_counts_and_the_last_uses(tmp_path):
    path, audit, book = make(tmp_path, [POST, OLD, {"name": "bad", "app": APP, "words": ["Buy"], "until": "2026-11-15"}])
    use(book, times=2)
    text, code = run(tmp_path)
    assert code == 0
    lines = text.splitlines()
    assert lines[0] == "wayfare_posting: active"
    assert lines[1] == ('  app com.example.app; words "Post", "Send"; route post_update; until 2026-11-21; '
                        'quiet 22:00 to 07:00; drafts shown first')
    assert lines[2] == "  today 2 of 3, this hour 2 of 2"
    assert lines[3] == ('  last uses: 2026-11-14T07:00:00.000Z tap "Post" (2 of 3 today); '
                        '2026-11-14T07:00:00.000Z tap "Post" (1 of 3 today)')
    assert lines[4] == "ended_one: ended 2026-11-10"
    assert lines[6] == "  today 0 of 1" and lines[7] == "  last uses: none"
    assert lines[8] == 'Refused: grant "bad" is refused: payments and purchases are never grantable ("Buy").'


def test_no_policy_no_grants_and_an_unreadable_file(tmp_path):
    assert grantscmd.run_grants(None)[1] == 2
    path, audit, book = make(tmp_path, [])
    assert run(tmp_path) == ("No grants.", 0)
    path.write_text("not json", encoding="utf-8")
    assert run(tmp_path)[1] == 1


def test_revoke_all_marks_the_file_audits_each_and_lists_them_revoked(tmp_path):
    path, audit, book = make(tmp_path, [POST, {**OLD, "until": "2026-11-20"}, {**OLD, "name": "gone", "revoked": "2026-11-01"}])
    use(book)
    text, code = run(tmp_path, revoke_all=True)
    assert code == 0
    assert text.splitlines()[0] == ("Revoked 2 grants: wayfare_posting, ended_one. A running route stops at its next "
                                    "move.")
    on_disk = json.loads(path.read_text("utf-8"))["grants"]
    assert [g.get("revoked") for g in on_disk] == ["2026-11-14", "2026-11-14", "2026-11-01"]
    assert "wayfare_posting: revoked 2026-11-14" in text and "gone: revoked 2026-11-01" in text
    revokes = [json.loads(x) for x in (tmp_path / "audit" / "2026-11.jsonl").read_text("utf-8").splitlines()
               if '"revoke"' in x]
    assert [(r["name"], r["detail"]) for r in revokes] == [("wayfare_posting", "grants --revoke-all"),
                                                           ("ended_one", "grants --revoke-all")]
    assert not (tmp_path / "policy.json.uses" / "wayfare_posting").exists()
    again, code = run(tmp_path, revoke_all=True)
    assert again.splitlines()[0] == "Every grant was already revoked (3)."


def test_revoke_all_with_a_held_lock_leaves_the_file_and_revokes_by_its_mark(tmp_path):
    """Review 1.7 A-M5: a held lock never blocks the kill switch; the file is left as it is and `.revoked` revokes."""
    path, audit, book = make(tmp_path, [POST])
    held = pidlock.take(f"{path}.edit.lock")
    before = path.read_text("utf-8")
    book.lock_wait = 0.05
    done = book.revoke_all()
    assert not isinstance(done, str) and done[0] and path.read_text("utf-8") == before
    assert book.active() == frozenset()
    pidlock.release(f"{path}.edit.lock", held)


# ---- unlock ----
def test_unlock_is_refused_while_the_holder_lives_and_removes_nothing(tmp_path):
    path, audit, book = make(tmp_path, [POST])
    lock = f"{path}.edit.lock"
    token = pidlock.take(lock)  # this live process holds it
    before = open(lock, encoding="utf-8").read()
    text, code = grantscmd.run_unlock(str(path), audit_folder=tmp_path / "audit")
    assert code == 1 and text == (f"The policy file's lock is held by a live process (pid {os.getpid()}); nothing was "
                                  "removed. Wait for it to finish, or stop that process first.")
    assert open(lock, encoding="utf-8").read() == before
    assert not (tmp_path / "audit").exists()
    pidlock.release(lock, token)
    assert grantscmd.run_unlock(str(path), audit_folder=tmp_path / "audit") == (
        "The policy file has no lock; nothing to remove.", 0)


def test_unlock_removes_a_dead_holders_lock_and_an_empty_one_and_audits_it(tmp_path):
    path, audit, book = make(tmp_path, [POST])
    lock = f"{path}.edit.lock"
    open(lock, "w", encoding="utf-8").write(json.dumps({"owner": "x", "pid": 4242, "started": "t"}) + "\n")
    dead = pidlock.Deps(alive=lambda pid: False)
    text, code = grantscmd.run_unlock(str(path), audit_folder=tmp_path / "audit", deps=dead)
    assert code == 0 and text == f"Removed {lock} (held by pid 4242, which is not running)."
    assert not os.path.exists(lock)
    open(lock, "w", encoding="utf-8").write("")
    os.utime(lock, (time.time() - 60, time.time() - 60))  # (left empty a minute ago: pidlock.EMPTY_AGE)
    text, code = grantscmd.run_unlock(str(path), audit_folder=tmp_path / "audit")
    assert code == 0 and text == f"Removed {lock} (empty (its holder stopped while writing it))."
    lines = [json.loads(x) for f in sorted((tmp_path / "audit").iterdir()) for x in f.read_text("utf-8").splitlines()]
    assert [(x["kind"], x["name"]) for x in lines] == [("approval", "policy edit lock")] * 2
    assert lines[0]["detail"] == "unlocked (held by pid 4242, which is not running)"


def test_unlock_needs_the_policy_file():
    assert grantscmd.run_unlock(None)[1] == 2


def test_the_command_line_reaches_all_three(tmp_path, monkeypatch, capsys):
    path, audit, book = make(tmp_path, [POST])
    monkeypatch.setattr(store, "HOME", tmp_path / "home")
    monkeypatch.setattr("sys.argv", ["tapwright", "grants", "--policy", str(path), "--revoke-all"])
    assert cli.main() == 0
    out = capsys.readouterr().out
    assert out.startswith("Revoked 1 grant: wayfare_posting.") and "wayfare_posting: revoked " in out
    assert (tmp_path / "home" / "audit").exists()
    monkeypatch.setattr("sys.argv", ["tapwright", "grants", "unlock", "--policy", str(path)])
    assert cli.main() == 0 and capsys.readouterr().out.strip() == "The policy file has no lock; nothing to remove."
    monkeypatch.setattr("sys.argv", ["tapwright", "grants"])
    assert cli.main() == 2 and "grants needs the policy file" in capsys.readouterr().out


def test_the_cli_cannot_be_reached_as_an_mcp_tool():
    from tapwright import mcp_server
    names = {t.name for t in mcp_server.server._tool_manager.list_tools()}
    assert not {n for n in names if "grant" in n or "unlock" in n or "revoke" in n}


# ---- the Grants: line ----
def entry(tool="tap", **more):
    return {"t": "2026-11-14T10:00:00.000Z", "tool": tool, "ok": True, "client": "c", **more}


def test_the_grants_line_has_uses_drafts_and_why_a_person_was_asked():
    cs = [entry(grant="instagram_posting", grant_use="1 of 3 today"),
          entry(grant="instagram_posting", grant_use="2 of 3 today"),
          entry(grant_draft="instagram_posting"),
          entry(grant_asked="limit reached")]
    assert report.grants_line(cs) == ("Grants: instagram_posting 2 uses (2 of 3 today), 1 draft shown; "
                                      "asked a person 1 time (limit reached).")


def test_the_grants_line_with_one_use_several_reasons_and_nothing_else():
    cs = [entry(grant="a", grant_use="1 of 1 today"), entry(grant_asked="quiet hours"),
          entry(grant_asked="limit reached"), entry(grant_asked="limit reached"), entry(grant="b", dry_run=True)]
    assert report.grants_line(cs) == ("Grants: a 1 use (1 of 1 today); "
                                      "asked a person 3 times (limit reached 2, quiet hours 1).")
    assert report.grants_line([entry(), entry(grant="x", ok=False)]) is None
    assert report.grants_line([entry(), entry("look")]) is None


def test_the_line_comes_right_after_time_and_is_left_out_with_no_grant():
    es = [{"t": f"2026-11-14T10:0{i}:00.000Z", "tool": "tap", "ok": True, "client": "c", "ms": 400, **more}
          for i, more in enumerate([{"grant": "g", "grant_use": "1 of 3 today"}, {}])]
    lines = report.build(es, "c", None).splitlines()
    assert lines[0].startswith("Session: 2 calls") and lines[1].startswith("Time: ")
    assert lines[2] == "Grants: g 1 use (1 of 3 today)." and lines[3] == "Waste: none."
    plain = report.build([{**e, "grant": None} for e in es], "c", None).splitlines()
    assert not any(line.startswith("Grants:") for line in plain)


# ---- doctor ----
def test_doctor_names_a_refused_grant_and_a_stuck_lock_with_its_path(tmp_path, capsys):
    path, audit, book = make(tmp_path, [POST, {"name": "shop_pay", "app": APP, "words": ["Pay"], "until": "2026-11-15"}])
    lock = f"{path}.edit.lock"
    open(lock, "w", encoding="utf-8").write("")
    assert cli.report_grants(str(path)) == 1
    out = capsys.readouterr().out
    assert 'note  grant "shop_pay" is refused: payments and purchases are never grantable ("Pay").' in out
    assert f"FAIL  the policy file's edit lock {lock} is stuck: empty (its holder stopped while writing it)." in out
    assert f"tapwright grants unlock --policy {path}" in out


def test_doctor_is_quiet_about_a_live_lock_and_says_ok_for_a_clean_file(tmp_path, capsys):
    path, audit, book = make(tmp_path, [POST])
    token = pidlock.take(f"{path}.edit.lock")
    assert cli.report_grants(str(path)) == 0
    pidlock.release(f"{path}.edit.lock", token)
    out = capsys.readouterr().out
    assert "ok    policy file" in out and "is stuck" not in out
    assert cli.report_grants(None) == 0


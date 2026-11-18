"""Export of a kept bug (AGENT-TOOLS 1.16.2 §29.7): the zip (no network, fixed entries, a private picture only on request),
the GitHub issue through a fake `gh` (confirm, public, the app in the path, the picture), the grep gate again on every
text sent (a value planted after the bug was made refuses it, nothing sent), and one `export` audit line each, a refused one
too, holding no content; with no audit line nothing is sent. No phone, no network, no real ~/.tapwright."""
import json
import zipfile

import pytest
from test_bugstore import APP, SECRET, ctx_for, make, store_with, write_run
from test_journey import FakeGh

from tapwright import auditlog, bug, bugexport, bugstore, journey_export
from tapwright.control import Refused

OTHER_APP = "com.example.other"


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("no network call may be made")
    monkeypatch.setattr(journey_export, "run_gh", refuse)
    import socket
    monkeypatch.setattr(socket, "create_connection", refuse)


class Audit:
    def __init__(self, ok=True):
        self.lines: list[str] = []
        self.ok = ok

    def __call__(self, detail):
        self.lines.append(detail)
        return self.ok


def kept(tmp_path, private=True, with_picture=False, keep=None):
    home = tmp_path / "explore"
    write_run(home, private=private)
    ctx = ctx_for(tmp_path, keep)
    made = make(ctx, home, "20261114T091500Z", with_picture=with_picture)
    return ctx, ctx.store.read(APP, made.meta["id"])


def zip_of(path):
    with zipfile.ZipFile(path) as z:
        return {i.filename: (i.date_time, z.read(i.filename)) for i in z.infolist()}


def test_zip_is_written_next_to_the_folder_with_no_network_and_a_fixed_time(tmp_path, no_network):
    ctx, held = kept(tmp_path, with_picture=True)
    audit = Audit()
    said = bugexport.export(held, bugexport.check({"to": "zip"}), ctx.gate, audit, tmp_path)
    path = held.folder.parent / f"{held.id}.zip"
    assert said.startswith(f"Exported: ~/bugs/{APP}/{held.id}.zip") and "without its private picture" in said
    entries = zip_of(path)
    assert sorted(entries) == sorted(f"{held.id}/{n}" for n in ("report.md", "report.html", "repro.json", "finding.json"))
    assert all(t == bugexport.ZIP_TIME for t, _ in entries.values())
    page = entries[f"{held.id}/report.md"][1].decode()
    assert "A picture exists and is private." in page and "picture.jpg" not in page
    assert json.loads(entries[f"{held.id}/finding.json"][1])["bug"]["picture_included"] is False
    assert str(tmp_path) not in b"".join(d for _, d in entries.values()).decode()
    assert audit.lines == [f"zip ~/bugs/{APP}/{held.id}.zip: {held.id}, picture not sent"]
    # with the person's request the private picture goes, and the same export gives the same bytes
    bugexport.export(held, bugexport.check({"to": "zip", "with_picture": True}), ctx.gate, audit, tmp_path)
    first = path.read_bytes()
    assert f"{held.id}/picture.jpg" in zip_of(path)
    bugexport.export(held, bugexport.check({"to": "zip", "with_picture": True}), ctx.gate, audit, tmp_path)
    assert path.read_bytes() == first
    assert audit.lines[-1].endswith("picture sent")


def test_a_picture_that_isnt_private_always_goes(tmp_path, no_network):
    ctx, held = kept(tmp_path, private=False)
    bugexport.export(held, bugexport.check({"to": "zip"}), ctx.gate, Audit(), tmp_path)
    assert f"{held.id}/picture.jpg" in zip_of(held.folder.parent / f"{held.id}.zip")


def test_a_value_planted_after_the_bug_was_made_refuses_the_export_and_nothing_is_written(tmp_path, no_network):
    ctx, held = kept(tmp_path)
    page = held.folder / "report.md"
    page.write_text(page.read_text("utf-8") + f"\nplanted {SECRET}\n", "utf-8")
    audit = Audit()
    with pytest.raises(Refused) as e:
        bugexport.export(held, bugexport.check({"to": "zip"}), ctx.gate, audit, tmp_path)
    assert e.value.code == "not_allowed"
    assert str(e.value).startswith("export refused: a vault value was found in report.md.") and SECRET not in str(e.value)
    assert not (held.folder.parent / f"{held.id}.zip").exists()
    assert audit.lines == [f"zip ~/bugs/{APP}/{held.id}.zip: {held.id}, refused (redaction check)"]
    # a 12-character piece of a value is enough
    page.write_text(page.read_text("utf-8").replace(SECRET, SECRET[2:14]), "utf-8")
    with pytest.raises(Refused):
        bugexport.export(held, bugexport.check({"to": "zip"}), ctx.gate, audit, tmp_path)


def test_with_no_audit_line_nothing_is_sent(tmp_path, no_network):
    ctx, held = kept(tmp_path)
    with pytest.raises(Refused) as e:
        bugexport.export(held, bugexport.check({"to": "zip"}), ctx.gate, Audit(ok=False), tmp_path)
    assert e.value.code == "not_allowed" and "audit line" in str(e.value)
    assert not (held.folder.parent / f"{held.id}.zip").exists()


def test_the_export_argument_is_checked():
    assert bugexport.check({"to": "issue", "repo": "o/r", "confirm": True}) == {
        "to": "issue", "repo": "o/r", "confirm": True, "public": False, "with_picture": False}
    for bad in ("zip", {"to": "gist"}, {"to": "issue"}, {"to": "issue", "repo": "o/r; rm"}, {"to": "zip", "repo": "o/r"},
                {"to": "zip", "x": 1}, {"to": "issue", "repo": "o/r", "confirm": "yes"}, {"to": "zip", "public": True},
                {"to": "issue", "repo": "../.."}, {"to": "issue", "repo": "-x/y"}, {"to": "issue", "repo": "o/.."}):
        with pytest.raises(Refused) as e:
            bugexport.check(bad)
        assert e.value.code == "bad_params", bad
    # (a zip of a bug of an app that isn't a test app needs confirm too: review R185 H2)
    assert bugexport.check({"to": "zip", "confirm": True})["confirm"] is True


def issue_replies(visibility="PRIVATE"):
    return {"repo view": json.dumps({"visibility": visibility}) if visibility else "{}",
            "api repos/o/r/branches/qa-reports": "{}",
            "api repos/o/r/git/ref/heads/qa-reports": json.dumps({"object": {"sha": "h1"}}),
            "api repos/o/r/git/commits/h1": json.dumps({"tree": {"sha": "t1"}}),
            "api --method POST repos/o/r/git/blobs": json.dumps({"sha": "b1"}),
            "api --method POST repos/o/r/git/trees": json.dumps({"sha": "t2"}),
            "api --method POST repos/o/r/git/commits": json.dumps({"sha": "c2"}),
            "api repos/o/r": json.dumps({"default_branch": "main"}),
            "issue create": "https://github.com/o/r/issues/7\n"}


ISSUE = {"to": "issue", "repo": "o/r", "confirm": True}


def sent(gh):
    return [a for a, _ in gh.calls if "POST" in a or "PATCH" in a or a[:2] == ["issue", "create"]]


def test_an_issue_needs_confirm_and_a_public_repository_needs_public(tmp_path, monkeypatch):
    ctx, held = kept(tmp_path)
    gh = FakeGh(issue_replies("PUBLIC"))
    monkeypatch.setattr(journey_export, "run_gh", gh)
    audit = Audit()
    with pytest.raises(Refused) as e:
        bugexport.export(held, bugexport.check({"to": "issue", "repo": "o/r"}), ctx.gate, audit, tmp_path)
    assert e.value.code == "not_allowed" and str(e.value).startswith("needs_confirmation") and not gh.calls
    with pytest.raises(Refused) as e:
        bugexport.export(held, bugexport.check(ISSUE), ctx.gate, audit, tmp_path)
    assert "public repository" in str(e.value) and not sent(gh)
    assert audit.lines == [f"issue o/r: {held.id}, refused (needs confirmation)",
                           f"issue o/r: {held.id}, refused (public repository)"]
    bugexport.export(held, bugexport.check({**ISSUE, "public": True}), ctx.gate, audit, tmp_path)
    assert audit.lines[-1] == f"issue o/r: {held.id}, picture not sent (public)"
    # a repository whose visibility can't be read counts as public
    monkeypatch.setattr(journey_export, "run_gh", FakeGh(issue_replies(None)))
    with pytest.raises(Refused):
        bugexport.export(held, bugexport.check(ISSUE), ctx.gate, audit, tmp_path)


def test_an_issue_has_the_pages_title_and_body_and_commits_the_script_under_the_apps_folder(tmp_path, monkeypatch):
    ctx, held = kept(tmp_path, with_picture=True)
    gh = FakeGh(issue_replies())
    monkeypatch.setattr(journey_export, "run_gh", gh)
    said = bugexport.export(held, bugexport.check(ISSUE), ctx.gate, Audit(), tmp_path)
    base = f".tapwright/bugs/{bug.app_folder(APP)}/{held.id}"
    trees = [json.loads(s) for a, s in gh.calls if a[:4] == ["api", "--method", "POST", "repos/o/r/git/trees"]]
    assert [e["path"] for e in trees[0]["tree"]] == [f"{base}/repro.json"]  # the private picture stays
    create = next((a, s) for a, s in gh.calls if a[:2] == ["issue", "create"])
    assert create[0][create[0].index("--title") + 1] == held.title
    assert "## How to see it" in create[1] and f"{base}/repro.json" in create[1] and "A picture exists and is private" in create[1]
    assert "issue in o/r" in said and "private picture wasn't sent" in said
    gh.calls.clear()
    bugexport.export(held, bugexport.check({**ISSUE, "with_picture": True}), ctx.gate, Audit(), tmp_path)
    trees = [json.loads(s) for a, s in gh.calls if a[:4] == ["api", "--method", "POST", "repos/o/r/git/trees"]]
    assert sorted(e["path"] for e in trees[0]["tree"]) == [f"{base}/picture.jpg", f"{base}/repro.json"]
    body = next(s for a, s in gh.calls if a[:2] == ["issue", "create"])
    assert f"raw/qa-reports/{base}/picture.jpg" in body


def test_two_apps_bugs_with_one_id_never_share_files(tmp_path):
    assert bugexport.issue_base(bugstore.Kept(APP, "bug_0123456789abcdef", tmp_path, {}, {})) != \
        bugexport.issue_base(bugstore.Kept(OTHER_APP, "bug_0123456789abcdef", tmp_path, {}, {}))


def test_an_issue_with_a_planted_value_sends_nothing(tmp_path, monkeypatch):
    ctx, held = kept(tmp_path)
    script = held.folder / "repro.json"
    script.write_text(script.read_text("utf-8").replace("trips-tab", SECRET), "utf-8")
    gh = FakeGh(issue_replies())
    monkeypatch.setattr(journey_export, "run_gh", gh)
    audit = Audit()
    with pytest.raises(Refused) as e:
        bugexport.export(held, bugexport.check(ISSUE), ctx.gate, audit, tmp_path)
    assert "found in repro.json" in str(e.value) and not gh.calls
    assert audit.lines == [f"issue o/r: {held.id}, refused (redaction check)"]


def test_the_audit_log_has_the_export_kind(tmp_path):
    log = auditlog.AuditLog(tmp_path / "audit", scrub=store_with(SECRET).scrub)
    assert log.write("export", name="bug_report", app=APP, detail=f"zip x.zip: bug_1, picture not sent {SECRET}")
    line = json.loads(auditlog.read_lines(tmp_path / "audit")[0])
    assert line["kind"] == "export" and line["name"] == "bug_report" and SECRET not in line["detail"]

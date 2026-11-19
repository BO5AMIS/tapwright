"""AGENT-TOOLS 1.16.2 §29.2, §29.3, §29.5, §29.6: a bug made from an explore report's finding, its folder, later sightings,
retention, and redaction at every file with the grep gate. No phone and no real ~/.tapwright: everything is in tmp_path."""
import json
from pathlib import Path

import pytest

from tapwright import accounts as accountlib, bug, bugmake, bugstore, findings

APP = "com.example.wayfare"
SECRET = "Sup3rSecretPassw0rd!"  # a vault value (an account's password)
TYPED = "my-typed-value-1234"  # a value typed in this session
SIG = "c41f9a07d2aa0011"


def store_with(*values: str) -> accountlib.Store:
    keep = accountlib.Store()
    keep._keep(tuple(("tester", "password", v) for v in values))
    return keep


def write_run(home: Path, run: str = "20261114T091500Z", *, place: str = "place_4", got: str = "the app crashed: EXC_BAD_ACCESS",
              build: str = "220", kind: str = "crash", logs=("09:21:07.440 error boom",), frames=(" 3 * Wayfare renderDays +148",),
              picture: bool = True, private: bool = True, place_keys=None, finding_id: str = "f3", path=None) -> Path:
    folder = home / APP / run
    (folder / "findings").mkdir(parents=True, exist_ok=True)
    (folder / "repro").mkdir(parents=True, exist_ok=True)
    finding = {"id": finding_id, "kind": kind, "app": APP, "app_version": "1.5.0", "build": build, "tool": "tapwright",
               "tool_version": "1.8.0", "when": "2026-11-14T09:21:07.512Z", "device": {"model": "iPhone16,1", "os": "iOS 19.1"},
               "place": place, "path_from": "home",
               "path": path if path is not None else [{"tool": "tap", "target": {"id": "trips-tab"}},
                                                      {"tool": "tap", "target": {"id": "trip-row-1"}}],
               "expected": "The app keeps running.", "got": got,
               "picture": {"file": f"findings/{finding_id}.jpg", "private": private} if picture else None,
               "logs": list(logs), "repro": f"repro/{finding_id}.json", "count": 1}
    if kind == "crash":
        finding["crash"] = {"reason": "EXC_BAD_ACCESS (SIGSEGV)", "frames": list(frames)}
    report = {"format": "agent-tools-explore-report", "version": 1, "app": APP, "findings": [finding],
              "place_keys": place_keys if place_keys is not None else {place: {"signature": SIG}}}
    (folder / "report.json").write_text(json.dumps(report), encoding="utf-8")
    script = {"format": "agent-tools-route", "version": 2, "name": finding_id, "app": APP, "start": "home", "end": None,
              "variables": [], "steps": finding["path"]}
    (folder / "repro" / f"{finding_id}.json").write_text(json.dumps(script), encoding="utf-8")
    if picture:
        (folder / "findings" / f"{finding_id}.jpg").write_bytes(b"\xff\xd8JPEG-BYTES")
    return folder


def ctx_for(tmp_path: Path, keep: accountlib.Store | None = None, kept: int = bugstore.KEPT, map_data=None) -> bugmake.Context:
    keep = keep or store_with(SECRET)
    return bugmake.Context(bugstore.BugStore(tmp_path / "bugs", kept), keep.bug_scrub, keep.bug_gate,
                           lambda app: map_data)


NOT_ASKED = {"checked": False, "why": "not asked"}


def make(ctx, home, run, with_picture=False, finding_id="f3", named=True):
    """(`named`: the app is one of the daemon's test apps, `serve --apps`)"""
    got = bugmake.read_run(home, home / APP / run, finding_id)
    return bugmake.from_run(ctx, got, with_picture=with_picture, reproduced=NOT_ASKED, named=named)


def every_text(folder: Path) -> str:
    return "\n".join(p.read_bytes().decode("utf-8", "replace") for p in folder.rglob("*") if p.is_file())


def test_a_bug_from_a_finding_has_its_folder_and_files(tmp_path):
    home = tmp_path / "explore"
    write_run(home)
    ctx = ctx_for(tmp_path)
    made = make(ctx, home, "20261114T091500Z")
    assert made.new and made.folder == tmp_path / "bugs" / APP / made.meta["id"]
    assert sorted(p.name for p in made.folder.iterdir()) == ["finding.json", "report.html", "report.md", "repro.json"]
    data = json.loads((made.folder / "finding.json").read_text("utf-8"))
    meta = data["bug"]
    assert meta["id"] == f"bug_{meta['signature']}" and meta["place_key"] == {"signature": SIG}
    assert meta["source"] == {"how": "explore", "run": "20261114T091500Z", "finding": "f3"}
    assert meta["seen"]["count"] == 1 and meta["seen"]["builds"] == ["1.5.0 (220)"]
    assert meta["seen"]["sources"] == ["20261114T091500Z/f3"] and meta["picture_included"] is False
    assert meta["reproduced"] == NOT_ASKED
    page = (made.folder / "report.md").read_text("utf-8")
    assert page.startswith("# Crash on place_4: the app crashed: EXC_BAD_ACCESS")
    assert "A picture exists and is private." in page and len(page.splitlines()) <= 80
    assert json.loads((made.folder / "repro.json").read_text("utf-8"))["steps"][0] == {"tool": "tap", "target": {"id": "trips-tab"}}
    answer = bugmake.answer(made, ctx.scrub, home=tmp_path)
    assert answer.splitlines()[0] == f"New bug: {made.title}"
    assert answer.splitlines()[1] == "Reproduced: not checked."  # (no check asked: no reason, W177)
    assert answer.splitlines()[2] == "Seen: 1 time, 2026-11-14, build 1.5.0 (220)."
    assert answer.splitlines()[3] == f"Folder: ~/bugs/{APP}/{made.meta['id']}"


def test_the_same_bug_in_two_runs_is_one_bug_with_count_2_and_a_new_build_adds_to_builds(tmp_path):
    home = tmp_path / "explore"
    write_run(home, "20261112T095900Z", build="219", got="the app crashed: EXC_BAD_ACCESS at 0x7ffee3a1c8d0",
              frames=(" 3 * Wayfare 0x1111 renderDays +148",))
    write_run(home, "20261114T091500Z", build="220", got="the app crashed: EXC_BAD_ACCESS at 0x7ffee3a1aaaa",
              frames=(" 3 * Wayfare 0x2222 renderDays +12",), place="place_9",  # renamed since: the key is the same
              place_keys={"place_9": {"signature": SIG}})
    ctx = ctx_for(tmp_path)
    first = make(ctx, home, "20261112T095900Z")
    second = make(ctx, home, "20261114T091500Z")
    assert not second.new and second.meta["id"] == first.meta["id"]
    assert second.meta["seen"]["count"] == 2 and second.meta["seen"]["builds"] == ["1.5.0 (219)", "1.5.0 (220)"]
    again = make(ctx, home, "20261114T091500Z")  # the same source again counts nothing
    assert again.meta["seen"]["count"] == 2
    assert len(list((tmp_path / "bugs" / APP).iterdir())) == 1
    page = (first.folder / "report.md").read_text("utf-8")
    assert "2 times; first 2026-11-14, last 2026-11-14; builds 1.5.0 (219), 1.5.0 (220)." in page
    assert page.startswith("# Crash on place_4")  # the first finding stays
    assert bugmake.answer(again, ctx.scrub).startswith("Known bug (seen 2 times): Crash on place_4")


def test_a_changed_exception_symbol_makes_another_bug_and_no_key_is_never_merged(tmp_path):
    home = tmp_path / "explore"
    write_run(home, "20261112T095900Z")
    write_run(home, "20261113T095900Z", frames=(" 3 * Wayfare renderOther +1",))
    ctx = ctx_for(tmp_path)
    assert make(ctx, home, "20261112T095900Z").meta["id"] != make(ctx, home, "20261113T095900Z").meta["id"]
    write_run(home, "20261114T091500Z", place="place_7", place_keys={})  # a place the map doesn't know: no key
    write_run(home, "20261115T091500Z", place="place_7", place_keys={})
    a = make(ctx, home, "20261114T091500Z")
    b = make(ctx, home, "20261115T091500Z")
    assert a.meta["place_key"] is None and b.meta["id"] == a.meta["id"] + "_2" and b.new
    assert make(ctx, home, "20261115T091500Z").meta["id"] == b.meta["id"]  # the same source finds its own


def test_the_place_key_falls_back_to_the_map(tmp_path):
    home = tmp_path / "explore"
    write_run(home, place_keys={})
    ctx = ctx_for(tmp_path, map_data={"places": {"place_4": {"key": {"signature": "feed", "parts": {"ids": ["x"]}}}},
                                      "roads": [{"from": "home", "to": "trips", "move": {"tool": "tap", "target": {"id": "trips-tab"}},
                                                 "names": {"id": "trips-tab", "label": "Trips"}}]})
    made = make(ctx, home, "20261114T091500Z")
    assert made.meta["place_key"] == {"signature": "feed"}
    assert '1. Tap "Trips" (id trips-tab)' in (made.folder / "report.md").read_text("utf-8")


def test_retention_keeps_the_newest_and_never_the_one_just_made(tmp_path):
    home = tmp_path / "explore"
    ctx = ctx_for(tmp_path, kept=3)
    ids = []
    for n in range(5):
        write_run(home, f"2026111{n}T091500Z", got=f"error kind {'abcde'[n]}", kind="js_error", picture=False)
        ids.append(make(ctx, home, f"2026111{n}T091500Z").meta["id"])
    left = sorted(p.name for p in (tmp_path / "bugs" / APP).iterdir())
    assert len(left) == 3 and ids[-1] in left


def test_a_private_picture_goes_in_only_when_asked_and_a_public_one_always(tmp_path):
    home = tmp_path / "explore"
    write_run(home, "20261114T091500Z")
    ctx = ctx_for(tmp_path)
    made = make(ctx, home, "20261114T091500Z")
    assert not (made.folder / "picture.jpg").exists()
    added = ctx.store.add_picture(ctx.store.read(APP, made.meta["id"]),
                                  bugmake.picture_again(home, made.meta["picture_source"]), ctx.gate)
    assert (made.folder / "picture.jpg").read_bytes() == b"\xff\xd8JPEG-BYTES"
    assert added.meta["picture_file"]["consent"] is True and len(added.meta["picture_file"]["sha256"]) == 64
    assert "![The screen at the moment](picture.jpg)" in (made.folder / "report.md").read_text("utf-8")
    assert '<img src="picture.jpg"' in (made.folder / "report.html").read_text("utf-8")
    write_run(home, "20261115T091500Z", got="other", kind="js_error", private=False)
    public = make(ctx, home, "20261115T091500Z")
    assert (public.folder / "picture.jpg").exists() and public.meta["picture_file"]["private"] is False
    write_run(home, "20261116T091500Z", got="third", kind="js_error")
    asked = make(ctx, home, "20261116T091500Z", with_picture=True)
    assert (asked.folder / "picture.jpg").exists() and asked.meta["picture_file"] == {
        "private": True, "consent": True, "sha256": asked.meta["picture_file"]["sha256"]}


def test_a_vault_value_and_a_typed_value_reach_no_file_and_no_answer(tmp_path):
    """§29.9's redaction test: a value planted in a note-like got, a log line, a typed step and a frame path."""
    home = tmp_path / "explore"
    write_run(home, got=f"the app crashed: password {SECRET} rejected",
              logs=(f"09:21:07.440 error login {SECRET} failed", f"typed {TYPED} in field", "x" * 290 + SECRET),
              frames=(rf" 3 * Wayfare C:\Users\Jane Doe\{SECRET}\Trip.swift:48 renderDays +148",),
              path=[{"tool": "tap", "target": {"label": f"Hi {SECRET}"}}, {"tool": "type_text", "target": {"id": "email"},
                                                                          "text": TYPED}])
    keep = store_with(SECRET)
    keep.remember_typed(TYPED)
    ctx = ctx_for(tmp_path, keep)
    made = make(ctx, home, "20261114T091500Z")
    text = every_text(made.folder) + bugmake.answer(made, ctx.scrub)
    for value in (SECRET, TYPED):
        assert value not in text
        assert not any(value[i:i + 12] in text for i in range(len(value) - 11))
    assert "Jane Doe" not in text and "Users" not in text
    assert "Type something in id email" in (made.folder / "report.md").read_text("utf-8")
    assert keep.bug_gate({n: (made.folder / n).read_text("utf-8") for n in bugstore.TEXT_FILES}) is None


def test_the_gate_refuses_and_nothing_is_written(tmp_path):
    home = tmp_path / "explore"
    write_run(home)
    ctx = ctx_for(tmp_path)
    ctx.scrub = lambda text: text  # a redactor that missed it: the gate still finds it
    write_run(home, got=f"the app crashed: {SECRET}")
    with pytest.raises(bugstore.BugGateError) as e:
        make(ctx, home, "20261114T091500Z")
    assert SECRET not in str(e.value) and "report.md" in str(e.value) or "finding.json" in str(e.value)
    assert not (tmp_path / "bugs" / APP).exists() or not any((tmp_path / "bugs" / APP).iterdir())


def test_the_gate_finds_an_encoded_value_and_a_12_character_piece_but_not_a_placeholder():
    keep = store_with(SECRET)
    assert keep.bug_gate({"a.md": "ok"}) is None
    assert keep.bug_gate({"a.md": "x Sup3rSecret%50assw0rd%21 y"}) == "a.md"  # percent-encoded in part
    assert keep.bug_gate({"b.md": "fine", "a.md": "only a piece: 3rSecretPass here"}) == "a.md"
    assert keep.bug_gate({"a.md": "<tester's password> and <typed value>"}) is None


def test_a_redactor_that_fails_hides_the_text(tmp_path):
    home = tmp_path / "explore"
    write_run(home, kind="js_error", got="boom secret text", picture=False)
    ctx = ctx_for(tmp_path)

    def broken(text):
        if "boom" in text:
            raise RuntimeError("no")
        return text
    ctx.scrub = broken
    made = make(ctx, home, "20261114T091500Z")
    page = (made.folder / "report.md").read_text("utf-8")
    assert "boom secret text" not in page and findings.HIDDEN in page


def test_the_run_folder_is_a_trust_boundary(tmp_path):
    home = tmp_path / "explore"
    outside = tmp_path / "elsewhere" / APP / "20261114T091500Z"
    write_run(tmp_path / "elsewhere")
    with pytest.raises(bugmake.BugError) as e:
        bugmake.read_run(home, outside, "f3")
    assert e.value.code == "bad_params"
    write_run(home)
    report = home / APP / "20261114T091500Z" / "report.json"
    data = json.loads(report.read_text("utf-8"))
    data["findings"][0]["repro"] = "repro/../../../../x.json"
    data["findings"][0]["picture"]["file"] = "findings/../../secret.jpg"
    report.write_text(json.dumps(data), encoding="utf-8")
    got = bugmake.read_run(home, home / APP / "20261114T091500Z", "f3")
    assert got.script is None and got.picture is None
    with pytest.raises(bugmake.BugError) as e:
        bugmake.read_run(home, home / APP / "20261114T091500Z", "f9")
    assert e.value.code == "not_found" and "f3" in str(e.value)


def test_a_bug_is_read_only_from_its_own_apps_folder_and_the_list(tmp_path):
    home = tmp_path / "explore"
    write_run(home)
    ctx = ctx_for(tmp_path)
    made = make(ctx, home, "20261114T091500Z")
    planted = tmp_path / "bugs" / "com.other.app" / made.meta["id"]
    planted.mkdir(parents=True)
    (planted / "finding.json").write_text((made.folder / "finding.json").read_text("utf-8"), encoding="utf-8")
    assert ctx.store.read("com.other.app", made.meta["id"]) is None
    assert ctx.store.holders(made.meta["id"]) == [APP]
    lines = ctx.store.listing()
    assert lines[0] == APP and lines[1].startswith(f"  {made.meta['id']}  Crash on place_4") and lines[1].endswith(
        "seen 1, last 2026-11-14, not checked")


def test_a_step_with_a_policy_word_needs_a_person(tmp_path):
    home = tmp_path / "explore"
    write_run(home, kind="dead_end", got="no way on", picture=False,
              path=[{"tool": "tap", "target": {"label": "Delete trip"}}, {"tool": "tap", "target": {"id": "go-on"}}])
    made = make(ctx_for(tmp_path), home, "20261114T091500Z")
    page = (made.folder / "report.md").read_text("utf-8")
    assert '1. Tap "Delete trip" (needs a person)' in page and "2. Tap id go-on\n" in page


def test_the_observed_finding_from_the_last_touches(tmp_path):
    trail = [{"tool": "tap", "ok": True, "app": APP, "target": {"point": True}},
             *({"tool": "tap", "ok": True, "app": APP, "target": {"id": f"b{n}"}, "place": f"place_{n}"} for n in range(12)),
             {"tool": "tap", "ok": True, "app": APP, "dry_run": True, "target": {"id": "dry"}},
             {"tool": "type_text", "ok": True, "app": APP, "target": {"label": "Email"}},
             {"tool": "tap", "ok": True, "app": "com.other", "target": {"id": "x"}}]
    seen = bugmake.observed_path(trail, APP, "place_20")
    assert len(seen.path) == 10 and seen.path[-1] == {"tool": "type_text", "target": {"label": "Email"}, "text": "${typed}"}
    assert seen.path_from == "place_3" and not seen.cut
    cut = bugmake.observed_path([*trail[:3], {"tool": "tap", "ok": True, "app": APP, "target": {"point": True}}, trail[5]],
                                APP, None)
    assert cut.cut and len(cut.path) == 1
    assert bugmake.observed_path([], APP, None) is None
    ctx = ctx_for(tmp_path)
    made = bugmake.from_now(ctx, app=APP, note=f"Total shows $12.50 after 2 coupons {SECRET}", expected=None,
                            identity={"app_version": "1.5.0", "build": "220", "tool_version": "1.8.5",
                                      "device": {"model": "iPhone16,1", "os": "iOS 19.1"}},
                            when="2026-11-14T09:21:07.512Z", observed=seen, place="place_20",
                            place_key={"signature": "c41f9a07d2"}, logs=["a", "b"], picture=b"jpg", system="share sheet",
                            with_picture=False, named=True)
    page = (made.folder / "report.md").read_text("utf-8")
    assert page.startswith("# Observed on place_20: Total shows $12.50 after 2 coupons <tester's password>")
    assert "Expected: (not given). Got:" in page and "## Reproduced\nnot checked\n" in page  # (no check asked: W177)
    assert "- A system sheet was in front: share sheet." in page and not (made.folder / "picture.jpg").exists()
    assert json.loads((made.folder / "finding.json").read_text("utf-8"))["system"] == "share sheet"
    assert bugmake.answer(made, ctx.scrub, reason_shown=False).splitlines()[1] == "Reproduced: not checked."
    assert bug.BUG_ID.match(made.meta["id"])


def test_a_picture_with_a_system_sheet_in_front_is_always_private():
    raw = {"id": "f1", "kind": "crash", "app": APP, "app_version": "1", "build": "2", "tool": "tapwright", "tool_version": "1.8.5",
           "when": "2026-11-14T09:21:07.512Z", "device": {"model": "iPhone16,1", "os": "iOS 19.1"}, "place": "place_4",
           "path_from": "home", "path": [], "expected": "e", "got": "the app crashed: x",
           "picture": {"file": "findings/f1.jpg", "private": False}, "logs": [], "count": 1, "system": "share sheet",
           "crash": {"reason": "x", "frames": []}}
    out = bugmake.clean(raw, lambda s: s)
    assert out["system"] == "share sheet" and out["picture"]["private"] is True


def test_retention_runs_at_the_daemons_first_use_of_the_bugs(tmp_path):
    from tapwright.bugtool import BugTools
    home = tmp_path / "explore"
    ctx = ctx_for(tmp_path)
    for i in range(3):
        write_run(home, f"20261114T09150{i}Z", got=f"crash {chr(97 + i)}", place_keys={"place_4": {"signature": f"{SIG[:-1]}{i}"}})
        make(ctx, home, f"20261114T09150{i}Z")
    tools = BugTools()
    tools.bug_home = tmp_path / "bugs"
    import tapwright.bugstore as bs
    real = bs.KEPT
    bs.BugStore.__init__.__defaults__ = (1,)
    try:
        assert len(tools._bugs().bugs(APP)) == 1
    finally:
        bs.BugStore.__init__.__defaults__ = (real,)

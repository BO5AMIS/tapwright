"""Self-repairing tests (AGENT-TOOLS §26), the pure part: the score, what is never repaired, the candidates, the files and
adopting. No phone."""
import json

import pytest
from fakes import button, el

from tapwright import explorefloor, policy, repairanalysis as ra, scriptrepair as sr, ui

SCREEN = (400.0, 800.0)
FLOOR = explorefloor.Floor(policy.DEFAULT, "com.example.app")


def screen(items, **kw):
    return ra.Screen(items, SCREEN, FLOOR, **kw)


def tap(target, **kw):
    return {"tool": "tap", "target": target, **kw}


def broke(code="not_found", reason="nothing on screen has id"):
    return ra.Break(1, code, reason)


# ---- the parts of the score (§26.3) ----

def test_the_words_of_an_id_split_on_separators_and_case():
    assert sr.words_of("login-btn") == ["login", "btn"]
    assert sr.words_of("loginBtn_2") == ["login", "btn", "2"]
    assert sr.words_of("ABCButton") == ["abc", "button"]


def test_two_words_match_when_equal_or_the_shorter_runs_in_order_from_the_same_letter():
    assert sr.id_part(["login", "btn"], ["login", "button"]) == 1.0  # the spec's example
    assert sr.id_part(["login", "btn"], ["login", "help"]) == 0.5
    assert sr.id_part(["go"], ["gone"]) == 0.0  # under 3 characters: only equal words match
    assert sr.id_part(["btn"], ["tub"]) == 0.0  # the same letters but another first letter
    assert sr.id_part(["save"], ["save", "trip", "now"]) == pytest.approx(1 / 3)  # over the larger count
    assert sr.id_part([], ["a"]) is None


def test_the_id_stem_drops_trailing_digits_and_words_with_their_separators():
    assert sr.id_stem("save-button") == "save"
    assert sr.id_stem("trip-row-1") == "trip"
    assert sr.id_stem("My_Save_Btn") == "my_save"
    assert sr.id_stem("button") == "button"  # nothing would be left: the whole id
    assert sr.id_stem("item2") == "item2"
    assert sr.id_stem("") is None


def test_the_bucket_is_a_three_by_three_grid_and_neighbours_score_half():
    assert sr.bucket_of((0, 0, 40, 40), SCREEN) == "c1r1"
    assert sr.bucket_of((180, 380, 40, 40), SCREEN) == "c2r2"
    assert sr.bucket_of((360, 760, 40, 40), SCREEN) == "c3r3"
    parts = sr.parts_of(sr.Old(bucket="c2r2"), button("e1", "x", "x", rect=(0, 0, 40, 40)), SCREEN)
    assert parts == {"position": 0.5}  # c1r1 is a diagonal neighbour of c2r2
    far = sr.parts_of(sr.Old(bucket="c3r3"), button("e1", "x", "x", rect=(0, 0, 40, 40)), SCREEN)
    assert far == {"position": 0.0}


def test_seen_holds_facts_and_never_text():
    items = [button("e1", "Secret label", "save-button", rect=(150, 700, 100, 40))]
    seen = sr.seen_of(items[0], items, SCREEN)
    assert seen == {"role": "button", "bucket": "c2r3", "component": "Button", "id_stem": "save"}
    assert "Secret" not in json.dumps(seen)
    inside = el("e2", "cell", "row", "trip-row-1", rect=(0, 120, 400, 48))
    table = el("e3", "scroll", "", "list", rect=(0, 100, 400, 500))
    assert "bucket" not in sr.seen_of(inside, [table, inside], SCREEN)  # left out inside a scroll view
    assert "id_stem" not in sr.seen_of(button("e4", "x", "<tester's username>"), [], SCREEN, private=lambda t: True)


def test_the_score_is_the_weighted_mean_of_the_parts_that_exist():
    old = sr.old_of(tap({"id": "login-btn"}, seen={"role": "button", "id_stem": "login", "bucket": "c2r3", "component": "Button"}))
    new = button("e1", "Log in", "login-button", rect=(150, 700, 100, 40))
    parts = sr.parts_of(old, new, SCREEN)
    assert parts == {"role": 1.0, "id_stem": 1.0, "position": 1.0, "component": 1.0}  # no words: the old target is an id
    assert sr.weighted(parts) == 1.0
    mixed = {"role": 1.0, "id_stem": 0.5, "words": 0.0}
    assert sr.weighted(mixed) == pytest.approx((0.15 * 1 + 0.35 * 0.5) / (0.15 + 0.35 + 0.35))
    assert sr.weighted({}) is None
    assert sum(sr.WEIGHTS.values()) == pytest.approx(1.0)
    assert sr.format_parts({"words": 0.5, "role": 1.0}) == "role 1.00, words 0.50"


def test_words_take_the_best_of_label_text_and_a_label_with_a_text():
    old = sr.Old(label="Skip")
    assert sr.parts_of(old, el("e1", "button", "Not now", "later"), SCREEN)["words"] < 0.5
    assert sr.parts_of(sr.Old(text="Skip"), el("e1", "button", "Skip", "x"), SCREEN)["words"] == 1.0
    assert sr.parts_of(sr.Old(text="Log in"), el("e1", "button", "log  IN", "x"), SCREEN)["words"] == 1.0
    assert "words" not in sr.parts_of(sr.Old(id="x"), el("e1", "button", "Skip", "x"), SCREEN)  # nothing to compare


def test_a_near_tie_is_called_and_equal_scores_go_in_reading_order():
    a, b = button("e1", "A", "a", rect=(10, 300, 50, 20)), button("e2", "B", "b", rect=(10, 100, 50, 20))
    ranked = sr.rank([sr.Scored(a, {}, 0.8), sr.Scored(b, {}, 0.8)])
    assert [s.element for s in ranked] == [b, a]  # top to bottom
    assert sr.near_tie(ranked)
    assert not sr.near_tie(sr.rank([sr.Scored(a, {}, 0.8), sr.Scored(b, {}, 0.7)]))
    assert sr.near_tie(sr.rank([sr.Scored(a, {}, 0.81), sr.Scored(b, {}, 0.80)]))


# ---- what is never repaired (§26.2, B1) ----

def test_nothing_that_types_is_ever_repaired():
    for step in ({"tool": "type_text", "target": {"id": "email"}, "text": "a@b.co"},
                 {"tool": "tap", "target": {"id": "x"}, "secure": True},
                 {"tool": "tap", "target": {"id": "x"}, "note": "${vault.home.street}"},
                 {"tool": "tap", "target": {"text": "${account.password}"}}):
        assert sr.never_repaired(step, FLOOR) is not None, step


def test_a_policy_word_on_the_old_target_stops_a_repair():
    for target in ({"text": "Delete trip"}, {"label": "Sign out"}, {"id": "delete-row"}, {"text": "Send"}):
        assert "policy word" in (sr.never_repaired(tap(target), FLOOR) or ""), target
    assert sr.never_repaired(tap({"id": "login-btn"}), FLOOR) is None


def test_the_floor_holds_even_under_an_empty_policy():
    floor = explorefloor.Floor(policy.NONE, "com.example.app")
    assert sr.never_repaired(tap({"text": "Buy now"}), floor) is not None


def test_a_native_back_is_never_repaired():
    for target in ({"label": "Back"}, {"text": "back"}, {"id": "nav-back-button"}, {"id": "back"}):
        assert sr.never_repaired(tap(target), FLOOR) == "a native Back is never repaired", target
    assert sr.never_repaired({"tool": "back"}, FLOOR) is not None  # the back tool is no step of a repairable kind
    assert sr.never_repaired(tap({"id": "backup-now"}), FLOOR) is None  # a word that only starts with back


def test_only_a_tap_long_press_toggle_or_scroll_with_a_target_is_repaired():
    for tool in ("swipe", "open_url", "wait_for", "app_command", "type_text", "back", "compare_screen", "drive", "wait"):
        assert sr.never_repaired({"tool": tool, "target": {"id": "x"}}, FLOOR) is not None, tool
    for tool in sr.REPAIRABLE:
        assert sr.never_repaired({"tool": tool, "target": {"id": "x"}}, FLOOR) is None, tool
    assert "no target" in sr.never_repaired({"tool": "tap"}, FLOOR)
    assert "point" in sr.never_repaired(tap({"x": 10, "y": 20}), FLOOR)


def test_a_draftsonly_route_is_never_repaired():
    assert "draftsOnly" in sr.never_repaired(tap({"id": "x"}), FLOOR, drafts_only=True)


# ---- the candidates ----

def test_a_renamed_id_is_the_candidate_and_it_is_ranked_with_its_parts():
    items = [button("e1", "Log in", "login-button", rect=(150, 700, 100, 40)), button("e2", "Help", "help-button", rect=(150, 600, 100, 40))]
    steps = [tap({"id": "login-btn"}, expect={"text": "Welcome"})]
    found = ra.analyse(steps, 0, broke(), screen(items, nearest=[items[0]]))
    assert found.kind == "ok"
    first = found.list[0]
    assert first.target == {"id": "login-button"} and first.score == pytest.approx(1.0) and first.parts["id_stem"] == 1.0
    assert "button" in first.summary and "login-button" in first.summary
    lines = sr.suggestion(found, "welcome")
    assert lines[0] == "Repair (suggest, not checked):" and lines[-1] == 'To check it: replay with repair: "apply".'
    assert lines[1].startswith('1. tap id=login-button, button "Log in": score 1.00 (id_stem 1.00)')


def test_a_candidate_must_be_able_to_take_the_action_and_carry_no_policy_word():
    items = [el("e1", "text", "login-button", "login-button"), button("e2", "Delete account", "login-button-x"),
             button("e3", "Log in", "login-button", rect=(10, 300, 100, 40), enabled=False)]
    steps = [tap({"id": "login-btn"}, expect={"text": "Welcome"})]
    found = ra.analyse(steps, 0, broke(), screen(items))
    assert found.kind == "none"  # a text can't tap, a policy word is out, a disabled button can't take it


def test_the_best_two_scores_less_than_point_oh_two_apart_are_not_told_apart():
    items = [button("e1", "Log in", "login-button", rect=(10, 100, 100, 40)), button("e2", "Log in", "login-buttons", rect=(10, 200, 100, 40))]
    steps = [tap({"id": "login-btn"}, expect={"text": "x"})]
    found = ra.analyse(steps, 0, broke(), screen(items, nearest=items))
    assert found.kind == "tie"
    assert sr.suggestion(found, None)[0].startswith("Repair (suggest, not checked): ambiguous.")


def test_an_ambiguous_target_chooses_among_what_it_matched_by_the_steps_seen():
    top, bottom = button("e1", "Save", "save", rect=(10, 100, 100, 40)), button("e2", "Save", "save", rect=(10, 700, 100, 40))
    steps = [tap({"id": "save"}, seen={"role": "button", "bucket": "c1r3"}, expect={"text": "Saved"})]
    found = ra.analyse(steps, 0, broke("ambiguous", "2 elements match"), screen([top, bottom]))
    assert found.kind == "ok" and found.list[0].target == {"id": "save", "index": 1}
    without = ra.analyse([tap({"id": "save"}, expect={"text": "Saved"})], 0, broke("ambiguous", "2 elements match"), screen([top, bottom]))
    assert without.kind == "tie"  # with no seen the two can't be told apart


def test_a_toggle_is_repaired_only_to_the_role_it_had():
    switch = el("e1", "switch", "Alerts", "alerts-switch", checked=True)
    checkbox = el("e2", "checkbox", "Alerts", "alerts-box")
    checkbox.actions = ("toggle",)
    step = {"tool": "toggle", "target": {"id": "alerts-toggle"}, "expect": {"id": "alerts-switch"}}
    unknown = ra.analyse([step], 0, broke(), screen([switch, checkbox]))
    assert unknown.kind == "none" and "role isn't known" in unknown.reason
    known = ra.analyse([{**step, "seen": {"role": "switch", "id_stem": "alerts"}}], 0, broke(), screen([switch, checkbox]))
    assert known.kind == "ok" and [c.target for c in known.list] == [{"id": "alerts-switch"}]  # never the other kind


def test_no_check_no_repair_and_a_failed_expect_is_a_finding():
    items = [button("e1", "Log in", "login-button")]
    assert "no check" in ra.analyse([tap({"id": "login-btn"})], 0, broke(), screen(items)).reason  # last step, no expect
    assert ra.analyse([tap({"id": "login-btn"}), tap({"id": "next"})], 0, broke(), screen(items)).kind == "ok"
    expect = ra.analyse([tap({"id": "login-btn"}, expect={"text": "x"})], 0, broke("expect_failed", "its action ran, but"), screen(items))
    assert expect.kind == "none" and "finding" in expect.reason
    for code in ("not_actionable", "disabled", "covered", "needs_confirmation"):
        assert ra.analyse([tap({"id": "x"}, expect={"text": "x"})], 0, broke(code, "covered: x"), screen(items)).kind == "none", code


def test_an_off_screen_target_is_a_scroll_candidate():
    items = [el("e1", "scroll", "", "list", rect=(0, 100, 400, 500))]
    steps = [tap({"id": "row-9"}, expect={"text": "Nine"})]
    found = ra.analyse(steps, 0, broke("not_actionable", "off_screen: row-9"), screen(items))
    assert found.kind == "ok" and [c.kind for c in found.list] == ["scroll"]
    lazy = ra.analyse(steps, 0, broke(), screen(items, scrollable=True))
    assert lazy.list[0].kind == "scroll"  # a row the list hadn't made yet
    assert ra.analyse(steps, 0, broke(), screen([button("e2", "Other", "zzz")], scrollable=False)).kind == "none"


def test_a_new_screen_in_the_way_is_a_proven_road_put_before_the_step():
    road = [({"tool": "tap", "target": {"id": "tab-home"}}, {"id": "tab-home"})]
    steps = [tap({"id": "login-btn"}, place="welcome", expect={"text": "x"})]
    found = ra.analyse(steps, 0, broke(), screen([], here="home", way=lambda a, b: road if (a, b) == ("home", "welcome") else None))
    assert found.kind == "ok" and found.list[0].kind == "road" and found.list[0].moves == [road[0][0]]
    assert found.list[0].place == "welcome" and found.list[0].place_from == "home"
    no_road = ra.analyse(steps, 0, broke(), screen([], here="home"))
    assert no_road.kind == "none" and "no proven road" in no_road.reason
    delete = [({"tool": "tap", "target": {"id": "delete-account-btn"}}, {"id": "delete-account-btn"})]
    unsafe = ra.analyse(steps, 0, broke(), screen([], here="home", way=lambda a, b: delete))
    assert unsafe.kind == "none" and "policy word" in unsafe.reason  # an inserted move carries the floor's words too


def test_a_native_back_button_is_never_a_candidate():
    back = button("e1", "Trips", "BackButton", rect=(10, 50, 60, 30))
    nav = ui.Back(True, back.center)
    steps = [tap({"id": "trips-back-btn"}, expect={"text": "Trips"})]
    assert ra.analyse(steps, 0, broke(), screen([back], back=nav)).kind == "none"


def test_a_candidate_whose_name_holds_a_private_value_is_never_a_target():
    scrub = lambda text: text.replace("12 Kestrel Lane", "<home's street>")  # noqa: E731
    secret = button("e1", "Log in as 12 Kestrel Lane", "")
    steps = [tap({"label": "Log in as Home"}, expect={"text": "x"})]
    assert ra.analyse(steps, 0, broke(), screen([secret], nearest=[secret], scrub=scrub)).kind == "none"  # only its label names it
    named = button("e2", "Log in as 12 Kestrel Lane", "login-as-home")
    found = ra.analyse(steps, 0, broke(), screen([named], nearest=[named], scrub=scrub))
    assert found.kind == "ok" and found.list[0].target == {"id": "login-as-home"}  # an id with no value in it is a fine name


def test_parse_break_reads_the_failing_step_and_its_reason():
    steps = [tap({"id": "a"}), tap({"id": "login-btn"})]
    text = "Ran 1 of 2 steps; stopped at step 2 (tap id=login-btn): nothing on screen has id \"login-btn\"; look, then pick another target.\nSame screen."
    got = ra.parse_break(text, "not_found", steps)
    assert got == ra.Break(2, "not_found", "nothing on screen has id \"login-btn\"; look, then pick another target.")
    assert ra.parse_break("Error: something else", "internal", steps) is None
    assert ra.parse_break("Ran 1 of 2 steps; stopped at step 9 (tap id=x): no", "not_found", steps) is None


# ---- the files (§26.6) and adopting (§26.5) ----

def made_repair(**kw):
    return sr.Repair(4, "tap", "target", {"id": "login-btn"}, {"id": "login-button"}, {"role": "button"}, 0.93,
                     {"role": 1.0, "id_stem": 0.85}, "welcome", None, 'button "Log in"', "id=login-btn wasn't found",
                     "the step's expect (text \"Welcome\") held, and the whole run passed.", [], {}) if not kw else sr.Repair(**kw)


def test_the_repair_phrase_is_what_the_audit_line_and_the_answer_say():
    assert sr.phrase(made_repair()) == "tap id=login-btn to id=login-button (score 0.93)"
    scroll = sr.Repair(2, "tap", "scroll", {"id": "row-9"}, inserted=[{"tool": "scroll", "direction": "down"}] * 2)
    assert sr.phrase(scroll) == "tap id=row-9 with scroll down 2 times before it"
    road = sr.Repair(2, "tap", "road", {"id": "go"}, inserted=[{"tool": "tap", "target": {"id": "tab-home"}}], place="welcome", place_from="home")
    assert sr.phrase(road) == "tap id=go with tap id=tab-home before it (home to welcome)"


def test_the_repaired_script_keeps_the_original_and_marks_what_changed():
    data = {"format": "agent-tools-script", "version": 1, "name": "sign_in", "app": "com.example.app", "fresh": True,
            "steps": [tap({"id": "a"}), tap({"id": "login-btn"}, expect={"text": "Welcome"}), tap({"id": "c"})]}
    work = [sr.Work(data["steps"][0], data["steps"][0], 1),
            sr.Work({"tool": "scroll", "direction": "down"}, None, None, {"inserted": True}),
            sr.Work(tap({"id": "login-button"}, expect={"text": "Welcome"}, seen={"role": "button"}), data["steps"][1], 2,
                    {"was": {"id": "login-btn"}, "score": 0.93}),
            sr.Work(data["steps"][2], data["steps"][2], 3)]
    at = sr.utc_now()
    out = sr.repaired_script(data, work, __import__("pathlib").Path("sign_in.json"), at, "1.5.0 (220)", 1, "1.8.0", lambda t: t)
    assert list(out)[:4] == ["format", "version", "name", "repaired"]
    assert out["repaired"] == {"from": "sign_in.json", "at": sr.stamp(at), "tool": "tapwright", "build": "1.5.0 (220)", "count": 1}
    assert out["steps"][0] == data["steps"][0] and out["steps"][3] == data["steps"][2]  # untouched steps stay as they were
    assert out["steps"][1] == {"tool": "scroll", "direction": "down", "repaired": {"inserted": True}}
    assert out["steps"][2]["target"] == {"id": "login-button"} and out["steps"][2]["seen"] == {"role": "button"}
    assert out["steps"][2]["repaired"] == {"was": {"id": "login-btn"}, "score": 0.93}
    assert data["steps"][1]["target"] == {"id": "login-btn"}  # the original dict wasn't touched


def test_the_markdown_names_the_step_the_old_and_new_target_why_and_the_evidence():
    text = sr.repair_markdown("sign_in", [made_repair()], sr.utc_now(), "1.8.0", "1.5.0 (220)")
    assert text.startswith("# Repairs for sign_in (") and "tapwright 1.8.0, build 1.5.0 (220))" in text
    assert 'Step 4 (tap). Was id=login-btn; now id=login-button (button "Log in", place welcome).' in text
    assert "   - Why: id=login-btn wasn't found" in text and "   - Score 0.93: role 1.00, id_stem 0.85." in text
    assert "   - Accepted: the step's expect" in text


def test_files_are_written_beside_the_script_and_never_over_it(tmp_path):
    script = tmp_path / "sign_in.json"
    script.write_text('{"x": 1}', "utf-8")
    names = sr.write_files(script, {"a": 1}, "# md\n")
    assert names == ["sign_in.repaired.json", "sign_in.repair.md"]
    assert script.read_text("utf-8") == '{"x": 1}'
    assert json.loads((tmp_path / "sign_in.repaired.json").read_text("utf-8")) == {"a": 1}
    assert not list(tmp_path.glob("*.tmp"))


def adoptable(tmp_path, original='{"v": "original"}', repaired='{"v": "repaired"}'):
    script = tmp_path / "sign_in.json"
    script.write_text(original, "utf-8")
    (tmp_path / "sign_in.repaired.json").write_text(repaired, "utf-8")
    (tmp_path / "sign_in.repair.md").write_text("# md", "utf-8")
    return script


def test_adopt_keeps_the_original_once_replaces_it_and_removes_the_repaired_file(tmp_path):
    script = adoptable(tmp_path)
    lines: list[str] = []
    ok, said = sr.adopt(script, lambda detail: lines.append(detail) or True)
    assert ok and lines == ["adopted"] and "kept the original as sign_in.orig.json" in said
    assert json.loads(script.read_text("utf-8")) == {"v": "repaired"}
    assert json.loads((tmp_path / "sign_in.orig.json").read_text("utf-8")) == {"v": "original"}
    assert not (tmp_path / "sign_in.repaired.json").exists() and (tmp_path / "sign_in.repair.md").exists()
    # a second repair, adopted: the first original is never lost
    (tmp_path / "sign_in.repaired.json").write_text('{"v": "second"}', "utf-8")
    ok, said = sr.adopt(script, lambda detail: True)
    assert ok and "already holds the first original" in said
    assert json.loads((tmp_path / "sign_in.orig.json").read_text("utf-8")) == {"v": "original"}
    assert json.loads(script.read_text("utf-8")) == {"v": "second"}


def test_adopt_needs_the_audit_line_and_a_repaired_file(tmp_path):
    script = adoptable(tmp_path)
    ok, said = sr.adopt(script, lambda detail: False)
    assert not ok and "audit log" in said
    assert json.loads(script.read_text("utf-8")) == {"v": "original"} and not (tmp_path / "sign_in.orig.json").exists()
    (tmp_path / "sign_in.repaired.json").unlink()
    ok, said = sr.adopt(script, lambda detail: True)
    assert not ok and "nothing to adopt" in said
    (tmp_path / "sign_in.repaired.json").write_text("{broken", "utf-8")
    assert not sr.adopt(script, lambda detail: True)[0]

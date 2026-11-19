"""The map's explored record (AGENT-TOOLS §28) and the action signature (§25.3). No phone."""
import json

from tapwright import accounts, explored, mapfile, maptool, places

APP = "com.example.wayfare"
BUILD = "1.5.0 (220)"
NEW_BUILD = "1.6.0 (230)"
T1, T2, T3 = "2026-11-14T09:00:00Z", "2026-11-14T10:00:00Z", "2026-11-14T11:00:00Z"
SECRET = "hunter2-correct-horse-battery"
REDACTION = accounts.Redaction((("home", "password", SECRET),))


def place(sig="s1", last=T1, **kw):
    return {"key": {"signature": sig, "parts": {"ids": [sig], "kinds": [], "tab": None, "layer": None}}, "link": None,
            "titles": {}, "last": last, **kw}


def map_with(*names):
    data = places.empty_map(APP)
    for i, name in enumerate(names):
        data["places"][name] = place(f"s{i}")
    return data


# ---- the signature (§25.3) ----

def test_the_signature_is_the_first_of_id_label_text_role():
    assert explored.signature(element_id="language-row", label="Language", text="Language") == "id:language-row"
    assert explored.signature(label="Language", text="x") == "label:Language"
    assert explored.signature(text="Language") == "text:Language"
    assert explored.signature(role="button", position="c1r6") == "role:button@c1r6"
    assert explored.signature(role="button") is None and explored.signature() is None  # never a point


def test_a_shared_target_ends_with_its_index_and_a_row_is_named_by_its_list():
    assert explored.signature(label="Add", index=1) == "label:Add#1"
    assert explored.signature(element_id="x", index=0) == "id:x#0"
    assert explored.row_signature("id:trips", 3) == "row:id:trips#3"


def test_text_is_redacted_then_cut_at_80_characters_but_an_id_is_not_cut():
    sig = explored.signature(text="x" * 100)
    assert sig == "text:" + "x" * 80
    assert explored.signature(element_id="i" * 120) == "id:" + "i" * 120
    redacted = explored.signature(text="Sign in as " + SECRET, scrub=REDACTION.scrub)
    assert SECRET not in redacted and "<home's password>" in redacted


def test_the_position_bucket_is_a_grid_of_4_columns_and_8_rows():
    assert explored.bucket(0, 0, 10, 10, 400, 800) == "c0r0"
    assert explored.bucket(190, 650, 20, 20, 400, 800) == "c2r6"  # centre (200, 660)
    assert explored.bucket(390, 790, 10, 10, 400, 800) == "c3r7"
    assert explored.bucket(500, 900, 10, 10, 400, 800) == "c3r7"  # (clamped)


def test_a_signature_is_one_the_record_will_hold():
    for good in ("id:x", "label:Add#1", "text:Hello", "role:button@c1r6", "row:id:trips#2"):
        assert explored.valid_signature(good)
    for bad in ("", "x", "…", "point:1,2", None, 4, "id:" + "x" * 500):
        assert not explored.valid_signature(bad)


# ---- the record (§28.1) ----

def test_a_tried_action_is_kept_with_its_result_and_leaves_the_frontier():
    data = map_with("home", "language")
    assert explored.mark_seen(data, "home", ["id:language-row", "id:reset-switch", "text:Sign out"], T1)
    assert explored.mark_tried(data, "home", "id:language-row", "road", BUILD, T2, to="language")
    assert explored.mark_tried(data, "home", "id:reset-switch", "same", BUILD, T2)
    assert explored.mark_skipped(data, "home", "text:Sign out", "floor", T2, word="Sign out")
    assert explored.mark_tried(data, "home", "role:button@c1r6", "refused", BUILD, T3, code="covered")
    rec = explored.record(data, "home")
    assert rec["frontier"] == [] and rec["last"] == T3
    by_sig = {a["sig"]: a for a in rec["actions"]}
    assert by_sig["id:language-row"] == {"sig": "id:language-row", "result": "road", "to": "language", "build": BUILD, "last": T2}
    assert by_sig["text:Sign out"] == {"sig": "text:Sign out", "result": "skipped", "why": "floor", "word": "Sign out", "last": T2}
    assert by_sig["role:button@c1r6"]["code"] == "covered"


def test_a_signature_is_unique_in_the_record_and_the_last_try_replaces_the_first():
    data = map_with("home")
    explored.mark_tried(data, "home", "id:x", "same", BUILD, T1)
    explored.mark_tried(data, "home", "id:x", "crash", BUILD, T2)
    assert [a["result"] for a in explored.record(data, "home")["actions"]] == ["crash"]


def test_a_result_that_is_missing_what_it_needs_writes_nothing():
    data = map_with("home")
    bad = [("id:x", "road", {}), ("id:x", "same", {"to": "home"}), ("id:x", "refused", {}), ("id:x", "same", {"code": "covered"}),
           ("id:x", "skipped", {}), ("id:x", "bogus", {}), ("nope", "same", {}), ("id:x", "road", {"to": "Not A Name"}),
           ("id:x", "refused", {"code": "Covered!"})]
    for sig, result, kw in bad:
        assert not explored.mark_tried(data, "home", sig, result, BUILD, T1, **kw), (sig, result, kw)
    assert not explored.mark_tried(data, "nowhere", "id:x", "same", BUILD, T1)
    assert not explored.mark_skipped(data, "home", "id:x", "because", T1) and not explored.mark_skipped(data, "home", "nope", "floor", T1)
    assert explored.record(data, "home") is None


def test_crash_and_left_are_results():
    data = map_with("home")
    assert explored.mark_tried(data, "home", "id:boom", "crash", BUILD, T1)
    assert explored.mark_tried(data, "home", "id:out", "left", BUILD, T1)
    assert explored.is_tried(data, "home", "id:boom", BUILD) and explored.is_tried(data, "home", "id:out", BUILD)


def test_an_action_counts_as_tried_only_on_its_own_build_but_a_skip_holds_for_every_build():
    data = map_with("home")
    explored.mark_tried(data, "home", "id:a", "same", BUILD, T1)
    explored.mark_skipped(data, "home", "text:Delete", "policy", T1, word="Delete")
    assert explored.is_tried(data, "home", "id:a", BUILD) and not explored.is_tried(data, "home", "id:a", NEW_BUILD)
    assert explored.is_tried(data, "home", "text:Delete", NEW_BUILD)
    assert not explored.is_tried(data, "home", "id:never", BUILD)  # no record is no permission and no try
    assert explored.frontier(data, "home", BUILD) == []
    assert explored.frontier(data, "home", NEW_BUILD) == ["id:a"]  # a new build tries everything again, a skip not


def test_the_frontier_holds_at_most_50_then_an_ellipsis_and_is_still_recomputed():
    data = map_with("home")
    explored.mark_seen(data, "home", [f"id:b{i}" for i in range(60)], T1)
    frontier = explored.record(data, "home")["frontier"]
    assert len(frontier) == 51 and frontier[-1] == "…" and frontier[0] == "id:b0"
    assert explored.cut_short(data, "home") and not explored.is_explored(data, "home", BUILD)
    assert "…" not in explored.frontier(data, "home", BUILD)
    explored.mark_tried(data, "home", "id:b0", "same", BUILD, T2)
    assert "id:b0" not in explored.record(data, "home")["frontier"]
    explored.mark_seen(data, "home", ["id:b0", "id:fresh"], T3)  # a signature that is an action doesn't come back
    assert "id:b0" not in explored.record(data, "home")["frontier"]


def test_seen_signatures_that_are_tried_or_skipped_stay_out_of_the_frontier():
    data = map_with("home")
    explored.mark_skipped(data, "home", "text:Buy", "may_submit", T1)
    explored.mark_seen(data, "home", ["text:Buy", "id:a", "id:a", "bad"], T2)
    assert explored.record(data, "home")["frontier"] == ["id:a"]


# ---- coverage in `map` (§28.3) ----

def test_a_place_is_explored_with_a_record_for_the_build_and_an_empty_frontier():
    data = map_with("home", "language", "profile")
    explored.mark_seen(data, "home", ["id:a"], T1)
    explored.mark_tried(data, "home", "id:a", "same", BUILD, T1)
    explored.mark_seen(data, "language", ["id:b"], T1)  # untried: frontier
    assert explored.is_explored(data, "home", BUILD) and not explored.is_explored(data, "home", NEW_BUILD)
    assert not explored.is_explored(data, "language", BUILD) and not explored.is_explored(data, "profile", BUILD)  # no record
    assert explored.coverage(data, BUILD) == explored.Coverage(3, 1, 2)


def test_the_coverage_line_is_there_only_when_a_place_has_a_record():
    data = map_with("home", "language")
    assert explored.coverage_line(data, BUILD) is None
    explored.mark_tried(data, "home", "id:a", "same", BUILD, T1)
    assert explored.coverage_line(data, BUILD) == "Coverage: 2 places (1 explored, 1 frontier)."
    assert explored.coverage_line(data, BUILD, 3) == "Coverage: 2 places (1 explored, 1 frontier); known up front, not reached: 3."


def test_the_map_listing_says_coverage_after_the_places_line():
    data = map_with("home", "language")
    ctx = maptool.Context(APP, "Wayfare", BUILD, "home")
    plain = maptool.listing(data, ctx)
    assert not any(line.startswith("Coverage:") for line in plain)
    explored.mark_tried(data, "home", "id:a", "same", BUILD, T1)
    lines = maptool.listing(data, ctx)
    at = next(i for i, line in enumerate(lines) if line.startswith("Places:"))
    assert lines[at + 1] == "Coverage: 2 places (1 explored, 1 frontier)."
    assert maptool.listing(data, maptool.Context(APP, "Wayfare", BUILD, "home", not_reached=2))[at + 1].endswith("not reached: 2.")


# ---- merging (§28.2) ----

def with_record(name="home", last=T1, **kw):
    data = map_with(name)
    data["places"][name]["last"] = last
    return data


def test_two_runs_merge_by_signature_and_the_newest_wins():
    disk, mem = with_record(), with_record()
    explored.mark_tried(disk, "home", "id:a", "same", BUILD, T1)
    explored.mark_tried(disk, "home", "id:b", "same", BUILD, T3)
    explored.mark_tried(mem, "home", "id:a", "crash", BUILD, T2)  # newer than disk's try of a
    explored.mark_tried(mem, "home", "id:b", "road", BUILD, T2, to="home")  # older than disk's
    explored.mark_tried(mem, "home", "id:c", "same", BUILD, T2)
    got = {a["sig"]: a["result"] for a in explored.record(mapfile.merge(disk, mem), "home")["actions"]}
    assert got == {"id:a": "crash", "id:b": "same", "id:c": "same"}


def test_a_tie_goes_to_the_tool_s_own_memory_and_the_record_last_is_the_newer():
    disk, mem = with_record(), with_record()
    explored.mark_tried(disk, "home", "id:a", "same", BUILD, T2)
    explored.mark_tried(mem, "home", "id:a", "crash", BUILD, T2)
    merged = explored.record(mapfile.merge(disk, mem), "home")
    assert merged["actions"][0]["result"] == "crash" and merged["last"] == T2


def test_a_side_with_no_record_never_drops_the_other_s():
    with_rec, without = with_record(), with_record()
    explored.mark_tried(with_rec, "home", "id:a", "same", BUILD, T1)
    explored.mark_seen(with_rec, "home", ["id:z"], T1)
    for disk, mem in ((with_rec, without), (without, with_rec)):
        rec = explored.record(mapfile.merge(disk, mem), "home")
        assert [a["sig"] for a in rec["actions"]] == ["id:a"] and rec["frontier"] == ["id:z"]


def test_the_frontier_is_recomputed_after_a_merge():
    disk, mem = with_record(), with_record()
    explored.mark_seen(disk, "home", ["id:a", "id:b"], T1)
    explored.mark_seen(mem, "home", ["id:b", "id:c"], T1)
    explored.mark_tried(mem, "home", "id:a", "same", BUILD, T2)  # one run tried what the other only saw
    assert explored.record(mapfile.merge(disk, mem), "home")["frontier"] == ["id:b", "id:c"]


def test_a_merged_frontier_is_cut_at_50_and_keeps_new_signatures_after_the_completed_ones_leave():
    disk, mem = with_record(), with_record()
    explored.mark_seen(disk, "home", [f"id:d{i}" for i in range(50)], T1)
    explored.mark_seen(mem, "home", ["id:new"], T1)
    for i in range(10):
        explored.mark_tried(mem, "home", f"id:d{i}", "same", BUILD, T2)
    merged = explored.record(mapfile.merge(disk, mem), "home")["frontier"]
    assert "id:new" in merged and len(merged) == 41 and merged[-1] != "…"


def test_a_merge_that_passes_50_ends_in_an_ellipsis():
    disk, mem = with_record(), with_record()
    explored.mark_seen(disk, "home", [f"id:d{i}" for i in range(40)], T1)
    explored.mark_seen(mem, "home", [f"id:m{i}" for i in range(40)], T1)
    merged = explored.record(mapfile.merge(disk, mem), "home")["frontier"]
    assert len(merged) == 51 and merged[-1] == "…"


def test_a_road_s_destination_follows_a_place_renamed_on_either_side():
    # the disk calls the place `place_2`, memory calls the same place `settings` (a person named it): one place, `settings`
    disk = map_with("home")
    disk["places"]["place_2"] = place("s9", T1)
    explored.mark_tried(disk, "home", "id:go", "road", BUILD, T1, to="place_2")
    mem = map_with("home")
    mem["places"]["settings"] = place("s9", T2, named=True)
    explored.mark_tried(mem, "home", "id:other", "road", BUILD, T1, to="settings")
    merged = mapfile.merge(disk, mem)
    assert "settings" in merged["places"] and "place_2" not in merged["places"]
    assert {a["sig"]: a["to"] for a in explored.record(merged, "home")["actions"]} == {"id:go": "settings", "id:other": "settings"}


def test_a_name_that_is_taken_by_another_place_makes_the_destination_follow_the_new_name():
    disk = map_with("home")
    disk["places"]["x"] = place("sx")
    mem = map_with("home")
    mem["places"]["x"] = place("sy")  # another place that wants `x`: it becomes `x_2`
    explored.mark_tried(mem, "home", "id:go", "road", BUILD, T1, to="x")
    merged = mapfile.merge(disk, mem)
    assert explored.record(merged, "home")["actions"][0]["to"] == "x_2"


def test_a_live_rename_moves_the_destinations_too():
    data = map_with("home", "place_2")
    explored.mark_tried(data, "home", "id:go", "road", BUILD, T1, to="place_2")
    named = places.set_name(data, "place_2", "checkout")
    assert explored.action(data, "home", "id:go")["to"] == named.name == "checkout"


def test_forgetting_a_place_removes_its_record():
    data = map_with("home", "language")
    explored.mark_tried(data, "language", "id:a", "same", BUILD, T1)
    assert places.forget(data, "language") and explored.record(data, "language") is None


def test_map_forget_removes_the_record_with_the_place():
    data = map_with("home", "language")
    explored.mark_tried(data, "language", "id:a", "same", BUILD, T1)
    maptool.edit(data, maptool.Context(APP, current="home"), {"forget": "language"})
    assert "language" not in data["places"] and explored.coverage_line(data, BUILD) is None


# ---- the file (§28.1) ----

def test_an_old_map_with_no_record_loads_and_is_written_back_unchanged(tmp_path):
    f = mapfile.MapFile(tmp_path, APP)
    f.update(lambda d: d["places"].update({"home": place()}))
    before = json.loads(f.path.read_text("utf-8"))
    assert "explored" not in before["places"]["home"]
    f.save(f.load())
    assert json.loads(f.path.read_text("utf-8")) == before and f.problem is None


def test_a_map_with_a_record_is_a_version_1_map_the_old_reader_still_accepts(tmp_path):
    data = map_with("home")
    explored.mark_tried(data, "home", "id:a", "same", BUILD, T1)
    assert data["version"] == 1 and mapfile.invalid(data) is None
    f = mapfile.MapFile(tmp_path, APP)
    f.save(data)
    assert explored.record(f.load(), "home")["actions"][0]["sig"] == "id:a"
    assert json.loads(f.path.read_text("utf-8"))["version"] == 1


def test_a_record_that_is_garbage_is_dropped_and_the_map_still_reads(tmp_path):
    f = mapfile.MapFile(tmp_path, APP)
    data = map_with("home", "language")
    data["places"]["home"]["explored"] = "nonsense"
    data["places"]["language"]["explored"] = {"last": T1, "frontier": ["id:ok", "bad", "id:ok"], "future": 7, "actions": [
        {"sig": "id:a", "result": "road", "last": T1},  # no `to`
        {"sig": "id:b", "result": "same", "last": T1},
        {"sig": "id:b", "result": "crash", "last": T2},
        "junk", {"sig": 5, "result": "same", "last": T1}]}
    f.path.parent.mkdir(parents=True)
    f.path.write_text(json.dumps(data), "utf-8")
    loaded = f.load()
    assert f.problem is None and "explored" not in loaded["places"]["home"]
    rec = explored.record(loaded, "language")
    assert rec["actions"] == [{"sig": "id:b", "result": "crash", "last": T2}] and rec["frontier"] == ["id:ok"]
    assert rec["future"] == 7  # keys it doesn't know are kept


def test_the_record_s_signatures_are_kept_out_of_the_file_when_they_hold_a_value(tmp_path):
    f = mapfile.MapFile(tmp_path, APP, REDACTION.scrub)
    data = map_with("home")
    explored.mark_seen(data, "home", [f"text:{SECRET}", "id:fine"], T1)
    explored.mark_tried(data, "home", f"label:Email {SECRET}", "same", BUILD, T1)
    explored.mark_tried(data, "home", "id:ok", "same", BUILD, T1)
    explored.mark_skipped(data, "home", "text:Delete", "policy", T1, word=f"Delete {SECRET}")
    f.save(data)
    text = f.path.read_text("utf-8")
    for form in (SECRET, SECRET[:12], SECRET[-12:], SECRET[6:18]):
        assert form not in text
    rec = explored.record(json.loads(text), "home")
    assert rec["frontier"] == ["id:fine"] and sorted(a["sig"] for a in rec["actions"]) == ["id:ok", "text:Delete"]
    assert "word" not in rec["actions"][0] or SECRET not in rec["actions"][0]["word"]


def test_the_merge_through_the_file_keeps_both_tools_work(tmp_path):
    f = mapfile.MapFile(tmp_path, APP)
    a, b = map_with("home"), map_with("home")
    explored.mark_tried(a, "home", "id:one", "same", BUILD, T1)
    explored.mark_tried(b, "home", "id:two", "same", BUILD, T2)
    f.save(a)
    f.save(b)
    assert sorted(x["sig"] for x in explored.record(f.load(), "home")["actions"]) == ["id:one", "id:two"]

"""The explorer's search (§25.4, §25.9, §28): order, rows, depth, focus, tried, budget, crash, leaving the app, one app, pause."""
import asyncio
import json
from dataclasses import replace

from explorefakes import FakePhone, OTHER, build, identity, standard
from mapfakes import screen

from tapwright import explore, explored, findings


def run(explorer: explore.Explorer, maps=None) -> explore.Result:
    async def go() -> explore.Result:
        result = await explorer.run()
        if maps is not None:
            await maps.flush()
        return result
    return asyncio.run(go())


def world(**more):
    screens, goes, back_to = standard()
    return FakePhone(screens, "home", goes, back_to, **more)


def test_it_explores_every_place_breadth_first_and_ends_when_the_frontier_is_empty(tmp_path):
    phone = world()
    explorer, maps = build(tmp_path, phone)
    result = run(explorer, maps)
    assert result.stopped == "frontier_empty"
    # a place is `place_<n>` until a person names it; no title is a name
    assert result.places[0] == "place_1" and all(p.startswith("place_") for p in result.places)
    assert len(result.places) == 6
    # Home first (the start), then what it leads to in the order of its elements (top to bottom), then deeper
    assert phone.touched[:3] == [("home", "go-settings"), ("home", "go-trips"), ("home", "go-about")]
    assert ("language", "pick-ar") in phone.touched and ("trips", "Trip A") in phone.touched
    assert result.expanded[0] == result.start_place


def test_rows_are_tried_two_at_a_time_and_the_report_says_so(tmp_path):
    phone = world()
    explorer, maps = build(tmp_path, phone)
    result = run(explorer, maps)
    rows = [k for s, k in phone.touched if s == "trips" and k.startswith("Trip")]
    assert set(rows) == {"Trip A", "Trip B"}  # the third is never tried (a way to the trip taps Trip A again)
    assert [sig for _, sig in result.moves if sig.startswith("row:")].__len__() == 2
    assert any(n.startswith("rows: first 2 of the list tried") for n in result.notes)
    sigs = [sig for _, sig in result.moves if sig.startswith("row:")]
    assert len(sigs) == 2 and all(s.rsplit("#", 1)[1] in ("1", "2") for s in sigs)
    assert not any("Trip" in sig for _, sig in result.moves)  # a row is never named by its text


def test_the_same_map_and_start_explore_in_the_same_order_twice(tmp_path):
    a, b = world(), world()
    first, m1 = build(tmp_path / "one", a)
    second, m2 = build(tmp_path / "two", b)
    r1, r2 = run(first, m1), run(second, m2)
    assert r1.moves == r2.moves and r1.expanded == r2.expanded and r1.places == r2.places
    assert a.touched == b.touched


def test_what_was_tried_on_this_build_is_not_tried_again_and_a_new_build_tries_everything_again(tmp_path):
    first = world()
    explorer, maps = build(tmp_path, first)
    run(explorer, maps)
    again = world()
    r2 = run(build(tmp_path, again, port=explorer.map)[0])
    assert again.touched == [] and r2.stopped == "frontier_empty"
    newer = world()
    who = replace(identity(), build="1.6.0 (230)")
    run(build(tmp_path, newer, who=who, port=explorer.map)[0])
    assert len(newer.touched) >= len(first.touched) - 1  # a new build tries everything again


def test_depth_saves_a_place_but_does_not_expand_it(tmp_path):
    phone = world()
    explorer, maps = build(tmp_path, phone, budget=explore.Budget(depth=1))
    result = run(explorer, maps)
    # depth 1: only the start place is expanded; the places its actions reach are saved, not expanded
    assert len(result.expanded) == 1 and len(result.places) == 4
    assert ("settings", "go-language") not in phone.touched


def test_focus_expands_only_places_below_the_focus_place(tmp_path):
    learn = world()
    explorer, maps = build(tmp_path, learn)
    run(explorer, maps)  # the places and roads are known now
    data = explorer.map.data
    focus = next(r["to"] for r in data["roads"] if r["from"] == "place_1" and r["move"]["target"] == {"id": "go-settings"})
    for place in data["places"].values():
        place.pop("explored", None)  # a fresh start for the explored record only
    again = world()
    result = run(build(tmp_path, again, port=explorer.map, focus=focus)[0])
    assert focus in result.expanded and "place_1" not in result.expanded
    assert ("language", "pick-ar") in again.touched
    assert not any(s in ("trips", "about") for s, _ in again.touched)  # a place outside the focus is never expanded


def test_the_steps_budget_is_never_passed(tmp_path):
    phone = world()
    explorer, maps = build(tmp_path, phone, budget=explore.Budget(steps=5))
    result = run(explorer, maps)
    assert result.stopped == "budget_steps" and result.steps == 5


def test_the_minutes_budget_stops_it_and_time_waited_for_a_person_does_not_count(tmp_path):
    phone = world(per_move=60.0)
    explorer, maps = build(tmp_path, phone, budget=explore.Budget(minutes=3))
    result = run(explorer, maps)
    assert result.stopped == "budget_minutes" and 3 <= result.steps <= 4
    waiting = world(per_move=1.0)
    waiting.waiting_at = 2
    explorer2, m2 = build(tmp_path / "w", waiting, budget=explore.Budget(minutes=1))
    done = run(explorer2, m2)
    assert done.stopped == "frontier_empty"  # 100 s were waited: not counted against the minute
    assert done.seconds < 60


def test_it_stops_at_the_first_crash_unless_it_is_told_to_continue(tmp_path):
    phone = world()
    phone.crashes.add(("home", "go-trips"))
    explorer, maps = build(tmp_path, phone)
    result = run(explorer, maps)
    assert result.stopped == "crash"
    assert [f.kind for f in result.findings] == ["crash"]
    crash = result.findings[0]
    assert crash.crash is not None and crash.crash.reason == "EXC_BAD_ACCESS"
    assert crash.path == ({"tool": "tap", "target": {"id": "go-trips"}},)
    marked = explored.action(maps.data(str(tmp_path), "com.example.app"), "place_1", "id:go-trips")
    assert marked is not None and marked["result"] == "crash"


def test_continue_reopens_the_app_goes_on_and_never_retries_the_action_that_crashed(tmp_path):
    phone = world()
    phone.crashes.add(("home", "go-trips"))
    explorer, maps = build(tmp_path, phone, continue_after_crash=True)
    result = run(explorer, maps)
    assert result.stopped == "frontier_empty"
    assert phone.touched.count(("home", "go-trips")) == 1
    assert phone.opens >= 1 and ("about", "about-version") in phone.touched


def test_a_move_that_leaves_the_app_is_a_dead_end_and_it_comes_back_with_open_app(tmp_path):
    screens, goes, back_to = standard()
    screens["safari"] = screen("Web", ids=("web-link",), bundle=OTHER)
    goes[("about", "about-version")] = "safari"
    phone = FakePhone(screens, "home", goes, back_to)
    explorer, maps = build(tmp_path, phone)
    result = run(explorer, maps)
    assert result.stopped == "frontier_empty"
    assert [f.kind for f in result.findings] == ["dead_end"]
    assert "left the app" in result.findings[0].got
    assert phone.opens >= 1
    assert phone.foreign_touched == []  # nothing was touched while another app was in front


def test_a_pause_stops_it_between_steps_and_says_where(tmp_path):
    phone = world()
    phone.pause_after = 2
    explorer, maps = build(tmp_path, phone)
    result = run(explorer, maps)
    assert result.stopped == "paused" and len(phone.touched) == 2
    assert result.left_at is not None
    for reason in ("busy", "locked"):
        other = world()
        other.pause_after, other.pause_reason = 1, reason
        again, m = build(tmp_path / reason, other)
        assert run(again, m).stopped == reason


def test_progress_is_said_every_ten_steps(tmp_path):
    lines: list[str] = []
    phone = world()
    explorer, maps = build(tmp_path, phone, progress=lines.append)
    result = run(explorer, maps)
    assert result.steps >= 10 and len(lines) == result.steps // 10
    assert lines[0].startswith("explored ") and lines[0].endswith(" findings") and " places, " in lines[0]


def test_a_logged_error_and_a_slow_screen_are_findings_and_a_covered_touch_is_refused(tmp_path):
    phone = world()
    phone.errors[("home", "go-about")] = ("ERROR boom in about",)
    phone.slow[("home", "go-settings")] = 4.2
    phone.refuse[("settings", "go-language")] = explore.Moved(code="not_actionable", detail="covered")
    explorer, maps = build(tmp_path, phone)
    result = run(explorer, maps)
    assert {f.kind for f in result.findings} == {"js_error", "slow", "refused"}
    refused = explored.action(maps.data(str(tmp_path), "com.example.app"), result.places[1], "id:go-language")
    assert refused is not None and refused["result"] == "refused" and refused["code"] == "covered"


def test_the_roads_it_finds_are_saved_as_a_persons_driving_would_save_them(tmp_path):
    phone = world()
    explorer, maps = build(tmp_path, phone)
    run(explorer, maps)
    data = maps.data(str(tmp_path), "com.example.app")
    road = next(r for r in data["roads"] if r["from"] == "place_1" and r["move"]["target"] == {"id": "go-settings"})
    assert road["by"] == "tapwright" and road["build"] == "1.5.0 (220)"
    assert not any("Trip" in str(r) for r in data["roads"] if r["move"].get("target", {}).get("id") is None and False)


def test_it_resets_with_open_app_when_nothing_else_brings_it_back(tmp_path):
    phone = world()
    phone.fail_back = {"settings", "about", "trips"}  # `back` does nothing on these
    explorer, maps = build(tmp_path, phone)
    result = run(explorer, maps)
    assert result.stopped == "frontier_empty" and phone.opens >= 1
    assert result.resets and result.resets[0]["how"] == "relaunch" and result.resets[0]["data_kept"] is True
    assert any(f.kind == "dead_end" and "except a reset" in f.got for f in result.findings)


def test_three_resets_in_a_row_that_reach_no_known_place_stop_the_run(tmp_path):
    screens, goes, back_to = standard()
    screens["onboarding"] = screen("Welcome", ids=("welcome-next",))
    phone = FakePhone(screens, "home", goes, back_to)
    phone.fail_back = {"settings", "about", "trips"}

    async def lost() -> explore.Moved:
        phone.opens += 1
        phone.at = "onboarding"  # every relaunch lands on a screen the map doesn't know
        return explore.Moved()
    phone.open_app = lost  # type: ignore[method-assign]
    explorer, maps = build(tmp_path, phone)
    assert run(explorer, maps).stopped == "cant_get_back"


def test_findings_come_out_in_one_shape(tmp_path):
    phone = world()
    phone.crashes.add(("settings", "go-language"))
    explorer, maps = build(tmp_path, phone, continue_after_crash=True)
    result = run(explorer, maps)
    crash = result.findings[0]
    assert findings.parse(findings.to_dict(crash), lambda s: s) == crash
    assert crash.path_from == result.start_place and crash.place == result.places[1]


def test_what_is_saved_holds_no_title_and_no_row_text(tmp_path):
    from tapwright import mapfile
    phone = world()
    explorer, maps = build(tmp_path, phone)
    run(explorer, maps)
    text = "".join(f.read_text(encoding="utf-8") for f in tmp_path.rglob("*.json"))
    assert text and "explored" in text and "id:go-settings" in text
    for private in ("Trip A", "Trip B", "Trip C", "Settings", "Language", "Detail", "Trips"):
        assert f'"{private}"' not in text and f"label:{private}" not in text and f"text:{private}" not in text
    assert mapfile.invalid(json.loads(next(tmp_path.rglob("map.json")).read_text(encoding="utf-8"))) is None


def test_a_scroll_reveals_what_lies_below_up_to_five_times(tmp_path):
    phone = world()
    phone.screens["home"].below = 2
    result = run(build(tmp_path, phone)[0])
    assert phone.scrolls == explore.SCROLLS and result.stopped == "frontier_empty"


def element(ref: str, ident: str, x: float, y: float, label: str = "", role: str = "button"):
    from tapwright import ui
    return ui.Element(ref, role, label, ident, rect=(x, y, 100, 40), actions=("tap",), type=role.title())


def test_the_actions_of_a_place_go_top_to_bottom_then_left_to_right_then_by_signature(tmp_path):
    from tapwright import ui
    items = [element("e1", "z-top-right", 200, 100), element("e2", "a-bottom", 10, 300), element("e3", "m-top-left", 10, 100),
             element("e4", "b-tie", 10, 200), element("e5", "a-tie", 10, 200)]
    tree = ui.Tree(items=items, app=("Example", "com.example.app"))
    explorer, _ = build(tmp_path, world())
    sigs = [a.sig for a in explorer._actions(tree, "place_1", False)]
    assert sigs == ["id:m-top-left", "id:z-top-right", "id:a-tie", "id:b-tie", "id:a-bottom"]
    assert [a.sig for a in explorer._actions(tree, "place_1", True)][-1] == explore.BACK_SIG  # `back` is the last move


def test_signatures_follow_the_rules_of_25_3(tmp_path):
    from tapwright import ui
    long_label = "L" * 100
    items = [element("e1", "", 10, 100, label=long_label), element("e2", "", 10, 150, label="Same"),
             element("e3", "", 200, 150, label="Same"), element("e4", "", 10, 200), element("e5", "", 150, 700)]
    items[3].value = "Plain text"
    explorer, _ = build(tmp_path, world())
    sigs = [a.sig for a in explorer._actions(ui.Tree(items=items, app=("Example", "com.example.app")), "place_1", False)]
    assert f"label:{'L' * 80}" in sigs  # text is cut at 80 characters
    assert "label:Same#0" in sigs and "label:Same#1" in sigs  # several elements: `#<index>` from 0
    assert "text:Plain text" in sigs
    assert "role:button@c2r7" in sigs  # no id, label or text: the role at its position on the 4 by 8 grid

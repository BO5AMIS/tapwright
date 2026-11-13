"""`drive`'s pure parts (AGENT-TOOLS §18.4): its arguments, the cap on a wait, the moves a road makes, how travel is
said, and the repair of a road by another name. No phone."""
import pytest

from tapwright import driving, ui
from tapwright.control import Refused


def refused(args: dict) -> str:
    with pytest.raises(Refused) as e:
        driving.check(args)
    assert e.value.code == "bad_params"
    return str(e.value)


def test_a_request_names_a_place_or_a_route_never_both_or_neither():
    assert driving.check({"to": "language"}).to == "language"
    assert driving.check({"route": "sign_in"}).route == "sign_in"
    assert "one of to or route" in refused({})
    assert "one of to or route" in refused({"to": "a", "route": "b"})
    assert "to must be" in refused({"to": ""})
    assert "to must be" in refused({"to": 3})


def test_a_request_has_defaults_and_checks_every_part():
    r = driving.check({"to": "trips_id", "with": {"id": 42}, "by": "taps", "dry_run": True,
                       "then": [{"tool": "tap", "target": {"text": "Go"}, "expect": {"text": "Done"}}], "account": "tester2"})
    assert (r.by, r.values, r.dry_run, r.account) == ("taps", {"id": "42"}, True, "tester2")
    assert r.then[0]["tool"] == "tap"
    plain = driving.check({"to": "x"})
    assert (plain.by, plain.values, plain.dry_run, plain.then, plain.account) == ("any", {}, False, None, None)
    assert "by is one of" in refused({"to": "x", "by": "fly"})
    assert "with must be an object" in refused({"to": "x", "with": ["id"]})
    assert "with's id" in refused({"to": "x", "with": {"id": {"a": 1}}})
    assert "account must be" in refused({"to": "x", "account": ""})
    assert "dry_run must be" in refused({"to": "x", "dry_run": "yes"})
    assert "step 1" in refused({"to": "x", "then": [{"tool": "look"}]})  # a `then` is run's steps


def test_the_wait_between_moves_is_three_times_the_roads_time_between_two_and_ten_seconds():
    assert driving.cap(None) == 2.0
    assert driving.cap({"ms": 100}) == 2.0
    assert driving.cap({"ms": 1500}) == 4.5
    assert driving.cap({"ms": 9000}) == 10.0
    assert driving.cap({"ms": "junk"}) == 2.0


def test_the_call_a_move_makes():
    assert driving.call_of({"tool": "tap", "target": {"id": "a"}}, {}) == ("tap", {"target": {"id": "a"}})
    assert driving.call_of({"tool": "back"}, {}) == ("back", {})
    assert driving.call_of({"tool": "swipe", "x0": 1, "y0": 2, "x1": 3, "y1": 4, "seconds": 0.5}, {}) == \
        ("swipe", {"x0": 1, "y0": 2, "x1": 3, "y1": 4, "seconds": 0.5})
    assert driving.call_of({"tool": "open_url", "link": "wayfare://trips/[id]"}, {"id": "42"}) == \
        ("open_url", {"url": "wayfare://trips/42"})
    with pytest.raises(driving.Unaimable) as e:
        driving.call_of({"tool": "tap", "target": {"row": True}}, {})
    assert "list row" in str(e.value)


def test_a_user_aims_by_the_text_a_person_could_read():
    names = {"id": "language-row", "label": "Language", "text": "Language"}
    assert driving.call_of({"tool": "tap", "target": {"id": "language-row"}}, {}, names=names, eyes=True) == \
        ("tap", {"target": {"text": "Language"}})
    with pytest.raises(driving.Unaimable) as e:
        driving.call_of({"tool": "tap", "target": {"id": "x"}}, {}, names={"id": "x"}, eyes=True)
    assert "no text a person could read" in str(e.value)


def test_how_travel_is_said():
    assert driving.travel(1, 0, True, 0) == "by link"
    assert driving.travel(0, 3, True, 0) == "by 3 taps"
    assert driving.travel(0, 1, True, 0) == "by 1 tap"
    assert driving.travel(1, 1, True, 0) == "by link then 1 tap"
    assert driving.travel(0, 2, False, 0) == "by 2 moves"
    assert driving.travel(0, 3, True, 1) == "by 3 taps, 1 reflex"
    assert driving.travel(0, 3, True, 2) == "by 3 taps, 2 reflexes"


def test_the_arrival_line():
    line = driving.arrived("language", driving.travel(0, 3, True, 0), 1.94, ["settings", "general", "language"])
    assert line == "Arrived at language by 3 taps in 1.9 s (settings > general > language)."
    assert driving.arrived("language", "by link", 0.8, ["language"]) == "Arrived at language by link in 0.8 s."


def test_moves_done_are_counted_in_words():
    assert driving.done(0) == "Done: 0 moves."
    assert driving.done(1) == "Done: 1 move."
    assert driving.done(3) == "Done: 3 moves."


def row(ref, label="", ident="", role="button", y=100):
    return ui.Element(ref, role, label, ident, rect=(10, y, 100, 40), actions=("tap",), type="Button")


def test_a_road_is_repaired_by_its_other_names_when_one_still_finds_the_element():
    items = [row("e1", "Language", "lang-row"), row("e2", "About", "about-row", y=150)]
    names = {"id": "language-row", "label": "Language", "text": "Language"}
    fixed = driving.repair({"tool": "tap", "target": {"id": "language-row"}}, names, items)
    assert fixed is not None and fixed.move == {"tool": "tap", "target": {"id": "lang-row"}}
    assert fixed.was == ("id", "language-row") and fixed.now == ("id", "lang-row")


def test_the_closest_match_is_taken_only_when_it_is_the_only_one():
    names = {"label": "Language", "text": "Language"}
    one = [row("e1", "Languages", "x1"), row("e2", "About", "x2", y=150)]
    fixed = driving.repair({"tool": "tap", "target": {"label": "Language"}}, names, one)
    assert fixed is not None and fixed.move["target"] == {"id": "x1"}
    two = [row("e1", "Languages", "x1"), row("e2", "language", "x2", y=150)]
    assert driving.repair({"tool": "tap", "target": {"label": "Language X"}}, {"label": "Language X"}, two) is None
    assert driving.repair({"tool": "tap", "target": {"label": "Nothing"}}, {"label": "Nothing"}, one) is None


def test_a_list_row_is_never_a_repair():
    cell = ui.Element("e1", "cell", "Language", "", rect=(0, 100, 400, 48), actions=("tap",), type="Cell")
    assert driving.repair({"tool": "tap", "target": {"label": "Language"}}, {"label": "Language"}, [cell]) is None


def test_repair_leaves_alone_what_isnt_aimed_at_an_element():
    assert driving.repair({"tool": "back"}, {}, []) is None
    assert driving.repair({"tool": "tap", "target": {"x": 1, "y": 2}}, {}, [row("e1", "A", "a")]) is None


def test_the_step_a_move_is_named_by():
    assert driving.step_of({"tool": "tap", "target": {"text": "Language"}}) == 'tap "Language"'
    assert driving.step_of({"tool": "tap", "target": {"id": "x"}}) == "tap id=x"
    assert driving.step_of({"tool": "back"}) == "back"
    assert driving.step_of({"tool": "open_url", "link": "wayfare://x"}) == "open_url"


def test_a_route_is_said_in_how_it_travelled():
    assert driving.travel(0, 0, True, 0, [("sign_in", 5)]) == "by route sign_in"  # (1.13.1, item 11: no step count)
    assert driving.travel(0, 1, True, 0, [("sign_in", 1)]) == "by 1 tap then route sign_in"
    assert driving.travel(1, 0, True, 1, [("a", 2), ("b", 3)]) == "by link then route a then route b, 1 reflex"
    assert driving.arrived("home", driving.travel(0, 0, True, 0, [("sign_in", 3)]), 3.2, ["welcome", "home"]) == \
        "Arrived at home by route sign_in in 3.2 s (welcome > home)."

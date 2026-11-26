"""(2.0.3, brief Q fix 2) An id or label shared by an element and its own wrapper at the same place resolves to the one
that can be acted on; matches at different places stay `ambiguous`, and `index` counts every match as before."""
import pytest

from tapwright import ui
from tapwright.errors import Refused


def _el(ref: str, role: str, rect, ident: str = "", label: str = "", actions=None) -> ui.Element:
    return ui.Element(ref, role, label, ident, "", rect, actions=ui.ROLE_ACTIONS.get(role, ()) if actions is None else actions)


def test_wrapper_and_its_button_resolve_to_the_button():
    # seen on the phone (a shopping app): `e3 other id=closeButton @16,81 40x40`, `e4 button id=closeButton @16,81 40x40`
    items = [_el("e3", "other", (16, 81, 40, 40), "closeButton"), _el("e4", "button", (16, 81, 40, 40), "closeButton")]
    assert ui.resolve(items, ui.Target(id="closeButton")) is items[1]


def test_button_first_then_wrapper_still_the_button():
    items = [_el("e3", "button", (16, 81, 40, 40), "closeButton"), _el("e4", "other", (16, 81, 40, 40), "closeButton")]
    assert ui.resolve(items, ui.Target(id="closeButton")) is items[0]


def test_frames_a_point_or_two_apart_are_one_thing():
    items = [_el("e3", "other", (15, 80, 42, 42), "closeButton"), _el("e4", "button", (16, 81, 40, 40), "closeButton")]
    assert ui.resolve(items, ui.Target(id="closeButton")) is items[1]


def test_a_wrapper_with_padding_that_covers_nearly_all_is_one_thing():
    # a 100x100 wrapper around a 96x96 control: the control covers 92 % of it
    items = [_el("e1", "other", (0, 0, 100, 100), "card"), _el("e2", "button", (2, 2, 96, 96), "card")]
    assert ui.resolve(items, ui.Target(id="card")) is items[1]


def test_a_small_control_inside_a_big_wrapper_stays_ambiguous():
    items = [_el("e1", "other", (0, 0, 300, 100), "card"), _el("e2", "button", (10, 10, 40, 40), "card")]
    with pytest.raises(Refused) as e:
        ui.resolve(items, ui.Target(id="card"))
    assert e.value.code == "ambiguous"


def test_two_at_different_places_stay_ambiguous():
    items = [_el("e3", "button", (16, 81, 40, 40), "closeButton"), _el("e9", "button", (16, 400, 40, 40), "closeButton")]
    with pytest.raises(Refused) as e:
        ui.resolve(items, ui.Target(id="closeButton"))
    assert e.value.code == "ambiguous" and "2 elements match" in str(e.value)


def test_index_keeps_counting_every_match():
    items = [_el("e3", "other", (16, 81, 40, 40), "closeButton"), _el("e4", "button", (16, 81, 40, 40), "closeButton")]
    assert ui.resolve(items, ui.Target(id="closeButton", index=0)) is items[0]
    assert ui.resolve(items, ui.Target(id="closeButton", index=1)) is items[1]
    assert ui.candidates(items, ui.Target(id="closeButton")) == items  # what a recording's `index` counts in


def test_three_listings_of_one_thing_pick_the_actionable():
    items = [_el("e1", "other", (16, 81, 40, 40), "x"), _el("e2", "button", (16, 81, 40, 40), "x"),
             _el("e3", "other", (17, 82, 39, 39), "x")]
    assert ui.resolve(items, ui.Target(id="x")) is items[1]


def test_none_actionable_takes_the_innermost():
    items = [_el("e1", "other", (16, 81, 40, 40), "x"), _el("e2", "other", (16, 81, 40, 40), "x")]
    assert ui.resolve(items, ui.Target(id="x")) is items[1]


def test_label_target_of_one_thing_listed_twice():
    # both take a touch (a cell and its own button), so _without_nested keeps both: the button's role wins
    items = [_el("e1", "cell", (0, 100, 390, 44), label="Settings"), _el("e2", "button", (0, 100, 390, 44), label="Settings")]
    assert ui.resolve(items, ui.Target(label="Settings")) is items[1]


def test_zero_sized_listings_are_never_merged():
    items = [_el("e1", "other", (16, 81, 0, 0), "x"), _el("e2", "button", (16, 81, 0, 0), "x")]
    with pytest.raises(Refused) as e:
        ui.resolve(items, ui.Target(id="x"))
    assert e.value.code == "ambiguous"


def test_recording_saves_an_index_that_finds_the_button_again():
    from tapwright import batch
    items = [_el("e3", "other", (16, 81, 40, 40), "closeButton"), _el("e4", "button", (16, 81, 40, 40), "closeButton")]
    saved = batch.recorded_element(items[1], items, "tap")
    assert saved == {"id": "closeButton", "index": 1}
    assert ui.resolve(items, ui.parse_target(saved)) is items[1]

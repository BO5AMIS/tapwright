"""Targets: parsing the one-kind object, and resolving it on a list (docs/AGENT-TOOLS.md §2)."""
from pathlib import Path

import pytest

from tapwright import ui
from tapwright.control import Refused
from tapwright.errors import ToolError

DATA = Path(__file__).parent / "data"
SCREEN = (430.0, 932.0)


def items(name: str) -> list[ui.Element]:
    return ui.elements((DATA / f"{name}.xml").read_text(encoding="utf-8"), SCREEN)


def code(fn, *args) -> Refused:
    with pytest.raises(Refused) as e:
        fn(*args)
    return e.value


def test_toolerror_is_refused():
    assert ToolError is Refused


@pytest.mark.parametrize("raw,kind", [({"ref": "e3"}, "ref"), ({"id": "login-button"}, "id"),
                                      ({"label": "Log in"}, "label"), ({"text": "Log in"}, "text"),
                                      ({"x": 120, "y": 300}, "point"), ({"label": "7", "index": 1}, "label"),
                                      ({"id": "a", "index": 0}, "id"), ({"text": "a", "index": 2}, "text")])
def test_one_kind_parses(raw, kind):
    t = ui.parse_target(raw)
    assert t.kind == kind
    assert t.index == raw.get("index")


def test_a_point_and_nulls():
    t = ui.parse_target({"x": 120, "y": 300.5})
    assert t.point == (120.0, 300.5) and (t.x, t.y) == (120.0, 300.5)
    assert ui.parse_target({"ref": "e1", "id": None, "label": None, "index": None}).ref == "e1"  # null = absent
    assert ui.parse_target({"ref": "e1"}).point is None


@pytest.mark.parametrize("raw", [
    None, "e3", ["e3"], {}, {"index": 0},                              # not an object, or nothing to act on
    {"ref": "e3", "id": "x"}, {"label": "a", "text": "a"}, {"id": "a", "x": 1, "y": 2},  # two kinds
    {"x": 1}, {"y": 1},                                                # half a point
    {"ref": "e3", "index": 0}, {"x": 1, "y": 2, "index": 0},           # index only with id, label, text
    {"id": "a", "index": -1}, {"id": "a", "index": 1.5}, {"id": "a", "index": True}, {"id": "a", "index": "1"},
    {"ref": ""}, {"label": "   "}, {"id": 7}, {"text": ["a"]},         # wrong values
    {"x": "1", "y": 2}, {"x": True, "y": 2}, {"x": float("nan"), "y": 2},
    {"testID": "a"}, {"id": "a", "extra": 1},                          # unknown keys
])
def test_everything_else_is_bad_params(raw):
    e = code(ui.parse_target, raw)
    assert e.code == "bad_params" and "target" in str(e)


def test_bad_params_says_what_a_target_looks_like():
    assert '{"ref": "e3"}' in str(code(ui.parse_target, {"ref": "e3", "id": "x"}))


def test_ref_and_id_resolve_on_the_list():
    list_ = items("settings_main")
    assert ui.resolve(list_, ui.Target(ref="e7")) is list_[6]
    assert ui.resolve(list_, ui.Target(id="airplane-mode-cell")) is list_[4]
    e = code(ui.resolve, list_, ui.Target(ref="e99"))
    assert e.code == "not_found" and "e99" in str(e) and "look" in str(e)
    e = code(ui.resolve, list_, ui.Target(id="Airplane-Mode-Cell"))  # ids are exact
    assert e.code == "not_found" and "look for its id" in str(e)
    assert "look first" in str(code(ui.resolve, [], ui.Target(ref="e1")))


def test_a_real_identifier_wins_over_a_name_that_mirrors_a_label():
    list_ = items("settings_main")
    # "Settings" is the navigation bar's identifier and the header's mirrored label: the identifier first
    assert ui.resolve(list_, ui.Target(id="Settings")) is list_[0]
    # with no real identifier, the raw name (WebDriverAgent's label mirror) still matches
    assert ui.resolve(list_, ui.Target(id="Bluetooth")) is list_[8]
    # "Airplane Mode" is only a mirror, on the text and on the switch: both match, so it is ambiguous
    assert code(ui.resolve, list_, ui.Target(id="Airplane Mode")).code == "ambiguous"


def test_label_is_exact_ignoring_case_and_never_partial():
    list_ = items("settings_main")
    assert ui.resolve(list_, ui.Target(label="bluetooth")) is list_[8]
    assert ui.resolve(list_, ui.Target(label="  Wi-Fi, Home ")) is list_[7]
    e = code(ui.resolve, list_, ui.Target(label="Wi-Fi"))  # that's `text`
    assert e.code == "not_found" and '{"text"' in str(e)


def test_text_is_exact_first_then_partial_on_label_or_value():
    list_ = items("settings_main")
    assert ui.resolve(list_, ui.Target(text="BLUETOOTH")) is list_[8]
    assert ui.resolve(list_, ui.Target(text="Wi-Fi")) is list_[7]      # partial: the label "Wi-Fi, Home"
    assert ui.resolve(list_, ui.Target(text="home")) is list_[7]       # partial in the value too
    assert ui.resolve(list_, ui.Target(text="On")) is list_[8]         # exact value beats the partial "Notifications"
    e = code(ui.resolve, list_, ui.Target(text="Display"))
    assert e.code == "not_found" and "scroll" in str(e)
    assert ui.resolve(list_, ui.Target(text="  bluetooth  ")) is list_[8]


def test_a_point_never_becomes_an_element():
    list_ = items("settings_main")
    assert ui.resolve(list_, ui.Target(x=20.0, y=230.0)) == (20.0, 230.0)  # over e5, still just the point
    assert ui.resolve([], ui.Target(x=1.0, y=2.0)) == (1.0, 2.0)


def test_several_matches_are_ambiguous_with_candidates():
    list_ = items("calculator")  # two buttons labelled "7", one with id=7-memory
    e = code(ui.resolve, list_, ui.Target(label="7"))
    assert e.code == "ambiguous"
    head, _, candidates = str(e).partition("Candidates:\n")
    assert head == '2 elements match label "7"; use one of their refs, or add index (0 is the first). '
    assert candidates.splitlines() == ['e3 button "7" @14,520 90x90 [tap]',
                                       'e7 button "7" id=7-memory @14,620 90x90 [tap]']
    assert str(e).endswith("[tap]")  # the candidates are the end of the message


def test_ambiguity_shows_at_most_five():
    many = [ui.Element(f"e{i}", "button", "Row", rect=(0, i * 50, 100, 40), actions=("tap",)) for i in range(1, 8)]
    e = code(ui.resolve, many, ui.Target(text="row"))
    head, _, candidates = str(e).partition("Candidates:\n")
    assert head.startswith('7 elements match text "row", showing 5;') and len(candidates.splitlines()) == 5
    assert candidates.splitlines()[-1].startswith("e5 ")


def test_index_picks_in_reading_order_and_is_checked():
    list_ = items("calculator")
    assert ui.resolve(list_, ui.Target(label="7", index=0)) is list_[2]
    assert ui.resolve(list_, ui.Target(label="7", index=1)) is list_[6]   # not re-sorted by enabled or role
    assert ui.resolve(list_, ui.Target(label="clear", index=0)) is list_[1]
    e = code(ui.resolve, list_, ui.Target(label="7", index=2))
    assert e.code == "not_found" and "index 2 is out of range: only 2 elements match" in str(e)
    assert "only 1 element match" in str(code(ui.resolve, list_, ui.Target(label="clear", index=1)))


def test_the_old_default_pick_is_gone():
    disabled_first = [ui.Element("e1", "button", "Save", enabled=False, actions=("tap",)),
                      ui.Element("e2", "button", "Save", actions=("tap",))]
    assert code(ui.resolve, disabled_first, ui.Target(label="save")).code == "ambiguous"  # no silent pick
    assert ui.resolve(disabled_first, ui.Target(label="save", index=0)) is disabled_first[0]


def test_at_point_is_the_front_most_element_of_a_fitting_role():
    list_ = items("settings_main")
    assert ui.at_point(list_, (370.0, 240.0), "switch") is list_[6]
    assert ui.at_point(list_, (30.0, 180.0), "input") is list_[3]
    assert ui.at_point(list_, (370.0, 240.0), "input") is None  # a switch is no input
    assert ui.at_point(list_, (5.0, 5.0), "switch") is None

"""Refs that survive an action, and the change lists (docs/AGENT-TOOLS.md §3.1 and §3.4)."""
from dataclasses import replace

from tapwright import changes, ui


def el(ref, role="button", label="", identifier="", value="", x=0.0, y=0.0, w=100.0, h=40.0, **fields):
    return ui.Element(ref, role, label, identifier, value, (x, y, w, h), **fields)


def refs(items):
    return [e.ref for e in items]


def test_elements_still_there_keep_their_refs_and_new_ones_count_on():
    old = [el("e1", label="Wi-Fi", y=0), el("e2", label="Bluetooth", y=50), el("e3", label="Sounds", y=100)]
    new = [el("e1", label="Bluetooth", y=0), el("e2", label="Done", y=50), el("e3", label="Sounds", y=100),
           el("e4", label="Wi-Fi", y=150)]  # the read numbers from e1; carry_refs renumbers
    carried = changes.carry_refs(old, new)
    assert [(e.ref, e.label) for e in carried] == [("e2", "Bluetooth"), ("e4", "Done"), ("e3", "Sounds"),
                                                   ("e1", "Wi-Fi")]
    assert new[0].ref == "e1"  # the read itself isn't touched


def test_a_gone_ref_is_not_handed_to_another_element():
    old = [el("e1", label="A"), el("e5", label="B", y=50)]
    assert refs(changes.carry_refs(old, [el("e1", label="C"), el("e2", label="B", y=50)])) == ["e6", "e5"]
    assert refs(changes.carry_refs(old, [el("e1", label="C")], used=9)) == ["e10"]  # e7..e9 were given out before
    assert changes.highest(old) == 5 and changes.highest([]) == 0


def test_the_key_is_role_and_identifier_else_role_and_label():
    old = [el("e1", label="Save", identifier="save-button"), el("e2", role="text", label="Save", y=50)]
    new = [el("e1", role="text", label="Save", y=50), el("e2", label="Saved!", identifier="save-button")]
    assert refs(changes.carry_refs(old, new)) == ["e2", "e1"]  # the id is the key: the label may change
    renamed = [el("e1", label="Save", identifier="other-id")]
    assert refs(changes.carry_refs(old[:1], renamed)) == ["e2"]  # another id is another element: a new ref
    different_role = [el("e1", role="link", label="Save", identifier="save-button")]
    assert refs(changes.carry_refs(old[:1], different_role)) == ["e2"]


def test_a_shared_key_keeps_its_refs_only_when_every_member_stayed_put():
    old = [el("e1", label="7", x=0, y=0), el("e2", label="7", x=0, y=100), el("e3", label="7", x=0, y=200)]
    same = [el("e1", label="7", x=0, y=0.5), el("e2", label="7", x=0, y=100), el("e3", label="7", x=0, y=200)]
    assert refs(changes.carry_refs(old, same)) == ["e1", "e2", "e3"]
    one_went = [el("e1", label="7", x=0, y=95), el("e2", label="7", x=0, y=205)]
    assert refs(changes.carry_refs(old, one_went)) == ["e4", "e5"]  # the group changed size: all fresh
    shifted = [el("e1", label="7", x=0, y=5), el("e2", label="7", x=0, y=105), el("e3", label="7", x=0, y=205)]
    assert refs(changes.carry_refs(old, shifted)) == ["e4", "e5", "e6"]
    assert refs(changes.carry_refs([], [el("e1", label="7"), el("e2", label="7", y=60)])) == ["e1", "e2"]


def test_scrolling_a_list_of_identical_buttons_never_hands_a_ref_to_another_row():
    def edits(ys):
        return [el(f"e{i + 1}", label="Edit", y=y) for i, y in enumerate(ys)]
    old = edits([100, 200, 300])  # the buttons of rows A, B, C
    new = changes.carry_refs(old, edits([100, 200, 300]))  # after a scroll: B, C, D sit where A, B, C were
    assert refs(new) == ["e1", "e2", "e3"]  # nothing moved, so the answer can't tell: only the rows' labels do
    rows_old = [el("e1", role="text", label="Row A", y=90), el("e2", label="Edit", y=100), el("e3", label="Edit", y=200),
                el("e4", label="Edit", y=300)]
    rows_new = [el("e1", role="text", label="Row B", y=90), el("e2", label="Edit", y=100),
                el("e3", label="Edit", y=200), el("e4", label="Edit", y=300), el("e5", label="Edit", y=400)]
    carried = changes.carry_refs(rows_old, rows_new)
    assert refs(carried) == ["e5", "e6", "e7", "e8", "e9"]  # four became five Edits: every old Edit ref is gone
    assert not {"e2", "e3", "e4"} & set(refs(carried))


def test_a_unique_key_keeps_its_ref_however_far_it_moved_and_a_move_shows():
    old = [el("e7", label="General", y=500, actions=("tap",)), el("e8", label="Other", y=600)]
    new = [el("e1", label="General", y=100, actions=("tap",))]
    carried = changes.carry_refs(old, new)
    assert refs(carried) == ["e7"]
    assert changes.diff(old, carried) == ['- button "Other"', '↕ e7 button "General" @0,100 100x40 [tap]']


def test_a_value_only_element_is_keyed_by_its_value():
    old = [el("e1", role="text", value="42", y=0)]
    assert refs(changes.carry_refs(old, [el("e1", role="text", value="42", y=300)])) == ["e1"]
    assert refs(changes.carry_refs(old, [el("e1", role="text", value="43")])) == ["e2"]


def test_an_unlabeled_element_keeps_its_ref_only_if_it_stayed_put():
    old = [el("e1", role="image", y=0)]
    assert refs(changes.carry_refs(old, [el("e1", role="image", y=0.5)])) == ["e1"]
    assert refs(changes.carry_refs(old, [el("e1", role="image", y=80)])) == ["e2"]
    with_id = [el("e1", role="image", identifier="logo", y=0)]
    assert refs(changes.carry_refs(with_id, [el("e1", role="image", identifier="logo", y=80)])) == ["e1"]


def test_no_change_is_one_line():
    old = [el("e1", label="Wi-Fi"), el("e2", label="Bluetooth", y=50)]
    assert changes.diff(old, changes.carry_refs(old, old)) == ["No visible change."]
    assert changes.diff([], []) == ["No visible change."]


def test_added_gone_and_changed_lines():
    old = [el("e1", label="Loading", role="text", y=0),
           el("e2", role="switch", label="Airplane Mode", y=50, checked=False, actions=("toggle",)),
           el("e3", role="cell", label="Wi-Fi", value="Off", y=100, actions=("tap",)),
           el("e4", label="Save", enabled=False, y=150, actions=("tap",)),
           el("e5", role="tab", label="General", y=200, selected=False)]
    new = [el("e1", role="switch", label="Airplane Mode", y=50, checked=True, actions=("toggle",)),
           el("e2", role="cell", label="Wi-Fi", value="Home", y=100, actions=("tap",)),
           el("e3", label="Save", y=150, actions=("tap",)),
           el("e4", role="tab", label="General", y=200, selected=True),
           el("e5", label="Done", y=250, actions=("tap",))]
    carried = changes.carry_refs(old, new)
    assert changes.diff(old, carried) == [
        '+ e6 button "Done" @0,250 100x40 [tap]',
        '- text "Loading"',
        '~ e2 switch "Airplane Mode" checked=true @0,50 100x40 [toggle] (was "false")',
        '~ e3 cell "Wi-Fi" value="Home" @0,100 100x40 [tap] (was "Off")',
        '~ e4 button "Save" @0,150 100x40 [tap] (was "true")',
        '~ e5 tab "General" selected @0,200 100x40 (was "false")',
    ]


def test_a_gone_element_shows_its_text_else_its_id_else_just_the_role():
    old = [el("e1", label="Loading", role="text", identifier="spinner-text"), el("e2", role="image", identifier="logo", y=50),
           el("e3", role="image", y=100), el("e4", role="cell", value="Off", y=150)]
    assert changes.diff(old, []) == ['- text "Loading"', "- image id=logo", "- image", '- cell "Off"']


def test_a_changed_text_is_one_field_even_though_label_and_value_both_moved():
    old = [el("e1", role="text", label="2 items", value="2 items", identifier="count")]
    new = [el("e1", role="text", label="3 items", value="3 items", identifier="count")]
    [line] = changes.diff(old, changes.carry_refs(old, new))
    assert line == '~ e1 text "3 items" id=count @0,0 100x40 (was "2 items")'


def test_several_changed_fields_are_named():
    old = [el("e1", role="cell", label="Wi-Fi", identifier="wifi", value="Off", enabled=False, actions=("tap",))]
    new = [el("e1", role="cell", label="Wi-Fi, Home", identifier="wifi", value="On", actions=("tap",))]
    [line] = changes.diff(old, changes.carry_refs(old, new))
    assert line.startswith('~ e1 cell "Wi-Fi, Home"')
    assert line.endswith('(was text="Wi-Fi", value="Off", disabled="true")')


def test_actions_are_a_field_too():
    old = [el("e1", role="cell", label="Row", actions=())]
    new = [el("e1", role="cell", label="Row", actions=("tap",))]
    [line] = changes.diff(old, changes.carry_refs(old, new))
    assert line.endswith('[tap] (was "")')


def test_a_switch_value_is_not_a_field_of_its_own():
    old = [el("e1", role="switch", label="Wi-Fi", identifier="wifi", value="0", checked=False)]
    new = [el("e1", role="switch", label="Wi-Fi", identifier="wifi", value="1", checked=True)]
    [line] = changes.diff(old, changes.carry_refs(old, new))
    assert line.endswith('(was "false")')


def test_moved_elements_are_listed_up_to_five_then_counted():
    def column(shift, n):
        return [el(f"e{i + 1}", label=f"Row {i}", y=i * 50 + shift) for i in range(n)]
    old = column(0, 8)
    five = [replace(e, rect=(0, e.y + 20, 100, 40)) if i < 5 else e for i, e in enumerate(old)]
    lines = changes.diff(old, changes.carry_refs(old, five))
    assert len(lines) == 5 and all(line.startswith("↕ e") for line in lines)
    assert lines[0] == '↕ e1 button "Row 0" @0,20 100x40'
    six = [replace(e, rect=(0, e.y + 20, 100, 40)) if i < 6 else e for i, e in enumerate(old)]
    assert changes.diff(old, changes.carry_refs(old, six)) == ["↕ 6 elements moved"]


def test_a_frame_change_of_more_than_one_point_is_a_move_and_one_point_is_not():
    old = [el("e1", label="Row", y=0)]
    assert changes.diff(old, changes.carry_refs(old, [replace(old[0], rect=(1.0, 1.0, 101.0, 41.0))])) == [
        "No visible change."]  # whole points: rounding can move a value by 1
    assert changes.diff(old, changes.carry_refs(old, [replace(old[0], rect=(0, 1.5, 100, 40))])) == [
        '↕ e1 button "Row" @0,2 100x40']
    assert changes.diff(old, changes.carry_refs(old, [replace(old[0], rect=(0, 0, 100, 43))])) == [
        '↕ e1 button "Row" @0,0 100x43']  # a size-only change is a move too
    assert changes.diff(old, changes.carry_refs(old, [replace(old[0], rect=(0, 0, 130, 40))])) == [
        '↕ e1 button "Row" @0,0 130x40']


def test_the_frame_never_appears_in_a_changed_line():
    old = [el("e1", role="cell", label="Wi-Fi", value="Off", y=0, actions=("tap",))]
    new = [el("e1", role="cell", label="Wi-Fi", value="On", y=80, w=120, actions=("tap",))]
    assert changes.diff(old, changes.carry_refs(old, new)) == ['~ e1 cell "Wi-Fi" value="On" @0,80 120x40 [tap] (was "Off")']


def test_update_keeps_refs_and_headlines_the_changes():
    old = [el("e1", label="Wi-Fi", y=0), el("e2", label="Bluetooth", y=50)]
    new = [el("e1", label="Wi-Fi", y=0), el("e2", label="Bluetooth", y=50), el("e3", label="Done", y=100)]
    up = changes.update(old, new)
    assert up.full is False and refs(up.items) == ["e1", "e2", "e3"] and up.top == 3
    assert up.lines == ["Same screen. Changes:", '+ e3 button "Done" @0,100 100x40']
    same = changes.update(old, old)
    assert same.lines == ["Same screen. Changes:", "No visible change."] and same.top == 2
    again = changes.update(up.items, [el("e1", label="Wi-Fi"), el("e2", label="Other", y=50)], used=up.top)
    assert refs(again.items) == ["e1", "e4"] and again.top == 4  # e2 and e3 are gone, never reused


def test_changes_are_measured_against_what_the_agent_was_shown():
    shown = [el("e1", label="A"), el("e2", label="B", y=50)]
    held = [el("e1", label="A"), el("e2", label="B", y=50), el("e3", label="C", y=100)]  # a silent re-read added C
    new = [el("e1", label="A"), el("e2", label="B", y=50), el("e3", label="C", y=100), el("e4", label="D", y=150)]
    up = changes.update(held, new, used=3, shown=shown)
    assert up.lines == ["Same screen. Changes:", '+ e3 button "C" @0,100 100x40', '+ e4 button "D" @0,150 100x40']
    assert refs(up.items) == ["e1", "e2", "e3", "e4"]


def test_the_full_list_replaces_the_changes_when_the_app_changed_or_there_was_no_list():
    old = [el("e1", label="Wi-Fi"), el("e2", label="Bluetooth", y=50)]
    new = [el("e1", label="Wi-Fi"), el("e2", label="Bluetooth", y=50)]
    up = changes.update(old, new, app_changed=True)
    assert up.full and up.lines == [] and refs(up.items) == ["e1", "e2"] and up.top == 2
    first = changes.update([], new)
    assert first.full and refs(first.items) == ["e1", "e2"]


def test_the_full_list_replaces_the_changes_past_half_the_list_or_eight():
    def rows(n, tag="A", replaced=0):
        """n rows; the first `replaced` of them are different elements (a new label, the same place)."""
        return [el(f"e{i + 1}", label=f"{'B' if i < replaced else tag} {i}", y=i * 50) for i in range(n)]
    old = rows(30)
    assert changes.update(old, rows(30, replaced=7)).full is False  # 7 new + 7 gone = 14: not over half of 30
    assert changes.update(old, rows(30, replaced=8)).full is True   # 16 > 15
    assert changes.update(rows(10), rows(10, replaced=4)).full is False  # 8: not over max(8, 5)
    assert changes.update(rows(10), rows(10, replaced=5)).full is True   # 10 > 8
    assert changes.update(rows(4), [el("e1", label="Z")] + rows(4)[1:]).full is False  # 2 changed of 4


def test_a_full_list_renumbers_from_e1():
    old = [el("e1", label="A"), el("e9", label="B", y=50)]
    new = [el("e1", label="X"), el("e2", label="Y", y=50)]
    up = changes.update(old, new, app_changed=True, used=9)
    assert refs(up.items) == ["e1", "e2"] and up.top == 2


def test_nothing_left_of_a_list_is_the_full_list_not_a_column_of_gone_lines():
    old = [el("e1", label="A"), el("e2", label="B", y=50)]
    up = changes.update(old, [])
    assert up.full and up.items == [] and up.lines == [] and up.top == 0
    assert changes.update([], []).lines == [changes.SAME_SCREEN, changes.NO_CHANGE]


def test_full_forces_the_full_list_even_for_an_empty_screen():
    up = changes.update([], [], full=True)
    assert up.full and up.lines == [] and up.items == []
    up = changes.update([el("e1", label="A")], [el("e1", label="A")], full=True)
    assert up.full and refs(up.items) == ["e1"]


def test_a_forced_full_list_does_not_diff():
    old = [el("e1", label="A")]
    up = changes.update(old, [el("e1", label="A"), el("e2", label="B", y=50)], full=True)
    assert up.full and refs(up.items) == ["e1", "e2"] and up.lines == []

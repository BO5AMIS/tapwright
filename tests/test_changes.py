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
    assert changes.diff(old, carried) == ['- button "Other"', '↕ e7 button "General" @0,100 100x40']


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
        '+ e6 button "Done" @0,250 100x40',
        '- text "Loading"',
        '~ e2 switch "Airplane Mode" checked=true (was "false")',
        '~ e3 cell "Wi-Fi" value="Home" (was "Off")',
        '~ e4 button "Save" (was "true")',
        '~ e5 tab "General" selected (was "false")',
    ]


def test_a_gone_element_shows_its_text_else_its_id_else_just_the_role():
    old = [el("e1", label="Loading", role="text", identifier="spinner-text"), el("e2", role="image", identifier="logo", y=50),
           el("e3", role="image", y=100), el("e4", role="cell", value="Off", y=150)]
    assert changes.diff(old, []) == ['- text "Loading"', "- image id=logo", "- image", '- cell "Off"']


def test_a_changed_text_is_one_field_even_though_label_and_value_both_moved():
    old = [el("e1", role="text", label="2 items", value="2 items", identifier="count")]
    new = [el("e1", role="text", label="3 items", value="3 items", identifier="count")]
    [line] = changes.diff(old, changes.carry_refs(old, new))
    assert line == '~ e1 text "3 items" id=count (was "2 items")'


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
    assert changes.diff(old, changes.carry_refs(old, new)) == ['~ e1 cell "Wi-Fi" value="On" @0,80 120x40 (was "Off")']


def test_a_changed_line_gives_the_position_and_the_actions_only_when_they_changed():
    """AGENT-TOOLS 1.8 §13 rule 2."""
    old = [el("e6", role="text", identifier="n", label="17", y=0)]
    [line] = changes.diff(old, changes.carry_refs(old, [el("e6", role="text", identifier="n", label="20", y=0)]))
    assert line == '~ e6 text "20" id=n (was "17")'
    [line] = changes.diff(old, changes.carry_refs(old, [el("e6", role="text", identifier="n", label="20", y=90)]))
    assert line == '~ e6 text "20" id=n @0,90 100x40 (was "17")'
    [line] = changes.diff(old, changes.carry_refs(old, [el("e6", role="text", identifier="n", label="20", y=0, actions=("tap",))]))
    assert line == '~ e6 text "20" id=n [tap] (was text="17", actions="")'
    [moved] = changes.diff(old, changes.carry_refs(old, [replace(old[0], rect=(0, 90, 100, 40))]))
    assert moved == '↕ e6 text "17" id=n @0,90 100x40'  # a move keeps its own line, with the position


STATE = "pressed=- typed= typed2= switch=off taps=2 long=0 slow=0 anim=off sheet=closed alert=none"


def was(old, new):
    [line] = changes.diff([el("e1", role="text", label=old)], [el("e1", role="text", label=new)])
    return line.split(" (was ", 1)[1][:-1]


def test_a_long_old_value_is_cut_to_what_changed():
    """AGENT-TOOLS 1.8 §13 rule 3, with the spec's and Tapline's examples."""
    assert was(STATE, STATE.replace("typed=", "typed=hello")) == '"…sed=- typed= typed2= swi…"'
    assert was("Total: 12 items in your basket, delivery on Thursday 14 May",
               "Total: 12 items in your basket, delivery on Friday 15 May") == '"…delivery on Thursday 14 May"'
    assert was("3 trips planned for the spring, 2 of them shared with friends",
               "4 trips planned for the spring, 2 of them shared with friends") == '"3 trips plann…"'
    assert was("Tap me 17", "Tap me 18") == '"Tap me 17"'


def test_cut_old_counts_code_points_and_keeps_a_value_that_would_lose_nothing():
    assert changes.cut_old("x" * 40, "y") == "x" * 40  # 40 characters: whole
    assert changes.cut_old("x" * 41, "y") == "x" * 41  # nothing in common: nothing to cut
    old = "é" * 50 + "X" + "é" * 50
    assert changes.cut_old(old, "é" * 50 + "Y" + "é" * 50) == "…" + "é" * 12 + "X" + "é" * 12 + "…"
    emoji = "😀" * 50 + "X" + "😀" * 50  # one code point each, though two UTF-16 units
    assert changes.cut_old(emoji, "😀" * 50 + "Y" + "😀" * 50) == "…" + "😀" * 12 + "X" + "😀" * 12 + "…"
    assert changes.cut_old("abc" * 20, "") == "abc" * 20  # nothing in common with the new value
    assert changes.cut_old("ab" * 30, "ab" * 30 + "c") == "…" + "ab" * 6  # the old is the start of the new


def test_the_cut_applies_off_screen_and_to_an_input_that_showed_its_placeholder():
    old = [el("", role="text", label=STATE, identifier="state", y=1400)]
    new = [el("", role="text", label=STATE.replace("taps=2", "taps=3"), identifier="state", y=1400)]
    [line] = changes.off_screen(old, new)
    assert line.endswith('(off screen; was "…ch=off taps=2 long=0 slow…")')
    hint = "Type the name of the person you want to invite to this trip"
    field = el("e1", role="input", label="Name", value=hint, placeholder=hint, actions=("tap", "type"))
    typed = replace(field, value=hint[:20] + "!")  # the first 20 characters were kept: 12 + 8 of context
    [line] = changes.diff([field], [typed])
    assert line.endswith('(was placeholder "… name of the person you want to invite to this trip")')


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
    assert up.full and up.lines == [] and refs(up.items) == ["e3", "e4"] and up.top == 4  # nothing carried
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


def test_a_full_list_keeps_numbering_so_no_old_ref_lands_on_another_element():
    def rows(n, tag="A", replaced=0):
        return [el(f"e{i + 1}", label=f"{'B' if i < replaced else tag} {i}", y=i * 50) for i in range(n)]
    old = rows(10)
    # 5 of 10 replaced: 10 changes > max(8, 5), so the full list answers
    up = changes.update(old, rows(10, replaced=5), used=12)
    assert up.full and up.lines == []
    # (a) what stayed on screen keeps its ref; (b) the new elements count on from the highest ref given (12)
    assert refs(up.items) == ["e13", "e14", "e15", "e16", "e17", "e6", "e7", "e8", "e9", "e10"]
    assert up.top == 17
    # (c) another app in front, or a forced full list: nothing of the old list is carried
    same = [el("e1", label="A"), el("e2", label="B", y=50)]
    for kwargs in ({"app_changed": True}, {"full": True}):
        fresh = changes.update(same, [el("e1", label="A"), el("e2", label="B", y=50)], **kwargs)
        assert fresh.full and refs(fresh.items) == ["e3", "e4"] and not set(refs(fresh.items)) & set(refs(same))
    gap = changes.update([el("e1", label="A"), el("e9", label="B", y=50)],
                         [el("e1", label="X"), el("e2", label="Y", y=50)], app_changed=True, used=9)
    assert refs(gap.items) == ["e10", "e11"] and gap.top == 11
    # (d) the top never goes down, whatever the answer
    assert changes.update(same, []).top == 2
    assert changes.update(same, [], used=7).top == 7
    assert changes.update([], [], used=5).top == 5
    assert changes.update(old, rows(10, replaced=5), used=12).top >= 12


def test_nothing_left_of_a_list_is_the_full_list_not_a_column_of_gone_lines():
    old = [el("e1", label="A"), el("e2", label="B", y=50)]
    up = changes.update(old, [])
    assert up.full and up.items == [] and up.lines == [] and up.top == 2  # no elements, but e1 and e2 were given
    assert changes.update([], []).lines == [changes.SAME_SCREEN, changes.NO_CHANGE]


def test_full_forces_the_full_list_even_for_an_empty_screen():
    up = changes.update([], [], full=True)
    assert up.full and up.lines == [] and up.items == []
    up = changes.update([el("e1", label="A")], [el("e1", label="A")], full=True)
    assert up.full and refs(up.items) == ["e2"] and up.top == 2  # forced: nothing carried, so a new number


def test_a_forced_full_list_does_not_diff():
    old = [el("e1", label="A")]
    up = changes.update(old, [el("e1", label="A"), el("e2", label="B", y=50)], full=True)
    assert up.full and refs(up.items) == ["e2", "e3"] and up.lines == []


def _rows(labels):
    """A list with a unique text row above and a "Edit" button beside each of three rows, as the screen lists it."""
    out = [el(f"e{i + 1}", role="text", label=f"Row {c}", x=0, y=90 + 100 * i) for i, c in enumerate(labels)]
    out += [el(f"e{i + 4}", label="Edit", x=200, y=100 + 100 * i) for i in range(3)]
    return out


def test_a_one_row_scroll_refreshes_the_duplicates_because_the_unique_rows_moved():
    old = _rows("ABC")
    new = [el(f"e{i + 1}", role="text", label=f"Row {c}", x=0, y=90 + 100 * i) for i, c in enumerate("BCD")]
    new += [el(f"e{i + 4}", label="Edit", x=200, y=100 + 100 * i) for i in range(3)]  # the buttons sit where they sat
    carried = changes.carry_refs(old, new)  # even a plain action: rows B, C and D moved, so the buttons did
    assert refs(carried)[:3] == ["e2", "e3", "e7"] and not {"e4", "e5", "e6"} & set(refs(carried))
    assert refs(carried)[3:] == ["e8", "e9", "e10"]


def test_the_same_layout_after_an_action_that_moved_nothing_keeps_the_duplicate_refs():
    old = _rows("ABC")
    assert refs(changes.carry_refs(old, _rows("ABC"), moves_content=False)) == refs(old)


def test_a_scroll_refreshes_duplicates_even_when_nothing_seems_to_have_moved():
    old = _rows("ABC")
    carried = changes.carry_refs(old, _rows("ABC"), moves_content=True)
    assert refs(carried) == ["e7", "e8", "e9", "e10", "e11", "e12"]  # nothing says the content moved: all new
    assert changes.update(old, _rows("ABC"), moves_content=True).full  # so much is new that the full list is the answer


def test_an_idless_button_that_stayed_put_while_the_content_scrolled_gets_a_fresh_ref():
    """Only row A's "Edit" was visible; after the scroll only row C's is, at the same y."""
    old = [el(f"e{i + 1}", role="text", label=f"Row {c}", y=100 * i) for i, c in enumerate("ABCDE")]
    old.append(el("e6", label="Edit", x=200, y=10))
    new = [el(f"e{i + 1}", role="text", label=f"Row {c}", y=100 * i) for i, c in enumerate("CDEFG")]
    new.append(el("e6", label="Edit", x=200, y=10))
    carried = changes.carry_refs(old, new, moves_content=True)
    assert refs(carried)[:3] == ["e3", "e4", "e5"] and "e6" not in refs(carried)  # A's Edit ref is gone
    assert changes.carry_refs(old, new)[-1].ref == "e6"  # after a plain action the same layout is the same button


def test_elements_that_scrolled_together_keep_their_refs_and_show_as_moved():
    old = [el("e1", label="General", y=300, actions=("tap",)), el("e2", label="Wi-Fi", y=400, actions=("tap",)),
           el("e3", label="Bluetooth", y=500, actions=("tap",))]
    new = [replace(e, rect=(0, e.y - 150, 100, 40)) for e in old]
    carried = changes.carry_refs(old, new, moves_content=True)
    assert refs(carried) == ["e1", "e2", "e3"]
    assert changes.diff(old, carried)[0] == '↕ e1 button "General" @0,150 100x40'


def test_too_few_agreeing_pairs_means_no_idless_element_keeps_its_ref():
    old = [el("e1", label="General", y=300), el("e2", label="Wi-Fi", y=400), el("e3", label="Stays", y=0)]
    new = [replace(old[0], rect=(0, 150, 100, 40)), replace(old[1], rect=(0, 250, 100, 40)), old[2]]
    carried = changes.carry_refs(old, new, moves_content=True)
    assert refs(carried) == ["e4", "e5", "e6"]  # only 2 pairs agree: no estimate, so no id-less element keeps one
    with_id = [replace(old[0], identifier="gen")]
    assert refs(changes.carry_refs(with_id, [replace(with_id[0], rect=(0, 150, 100, 40))], moves_content=True)) == ["e1"]


def test_back_to_a_screen_with_a_done_button_elsewhere_gets_a_fresh_ref():
    old = [el("e1", label="Done", y=100)]
    new = [el("e1", label="Done", y=500)]
    assert refs(changes.carry_refs(old, new, moves_content=True)) == ["e2"]
    assert refs(changes.carry_refs(old, new, moves_content=False)) == ["e1"]  # a tap: a move is news, not a swap


def test_a_read_with_no_action_before_it_keeps_the_refs_of_what_stayed():
    """`moves_content=None`: the re-read a target is resolved on. A first-time agent saw `Lab sheet` and an icon
    go and come back under new refs on almost every answer, because each such read counted as moved content."""
    old = [el("e1", role="text", label="Lab sheet", y=20), el("e2", role="image", label="gearshape.fill", y=60),
           el("e3", label="Edit", x=200, y=100), el("e4", label="Edit", x=200, y=200)]
    same = [replace(e) for e in old]
    assert refs(changes.carry_refs(old, same, moves_content=None)) == ["e1", "e2", "e3", "e4"]
    assert refs(changes.carry_refs(old, same, moves_content=True)) == ["e5", "e6", "e7", "e8"]  # after a scroll: all new


def test_a_read_with_no_action_before_it_still_sees_content_that_moved():
    """The list scrolled by itself (or a person's flick is still running): the unique rows agree on a move, so the
    rule for moved content holds, and the button that stayed put is another row's."""
    old = [el(f"e{i + 1}", role="text", label=f"Row {c}", y=100 * i) for i, c in enumerate("ABCDE")]
    old.append(el("e6", label="Edit", x=200, y=10))
    new = [el(f"e{i + 1}", role="text", label=f"Row {c}", y=100 * i) for i, c in enumerate("CDEFG")]
    new.append(el("e6", label="Edit", x=200, y=10))
    carried = changes.carry_refs(old, new, moves_content=None)
    assert refs(carried)[:3] == ["e3", "e4", "e5"] and "e6" not in refs(carried)


def test_fixed_elements_do_not_make_a_stay_look_like_a_match():
    """A nav title and tab items outnumber the scrolled elements: the estimate is zero, so there is none."""
    fixed = [el(f"e{i + 1}", role="text", label=f"Fixed {i}", y=i * 5) for i in range(3)]
    old = [*fixed, el("e4", label="Edit", x=200, y=300)]  # row A's button
    new = [*fixed, el("e4", label="Edit", x=200, y=300)]  # row C's, one row of scrolling later
    carried = changes.carry_refs(old, new, moves_content=True)
    assert "e4" not in refs(carried)
    assert changes.carry_refs(old, new)[3].ref == "e4"  # after a tap it is the same button


def test_a_read_with_no_action_before_it_gives_a_text_that_moved_alone_a_new_ref():
    """Row 3's "Delete" was the only one on screen; the list moved by itself and now another row's is, somewhere
    else. Too few elements to agree on a move, so nothing says it is the same button: a new ref. (After a plain
    action, a unique text keeps its ref wherever it went.)"""
    old = [el("e1", role="text", label="Title", y=0), el("e2", label="Delete", x=200, y=300)]
    new = [el("e1", role="text", label="Title", y=0), el("e2", label="Delete", x=200, y=500)]
    assert refs(changes.carry_refs(old, new, moves_content=None)) == ["e1", "e3"]
    assert refs(changes.carry_refs(old, new, moves_content=False)) == ["e1", "e2"]
    with_id = [replace(old[1], identifier="row-delete")]
    moved = [replace(with_id[0], rect=(200, 500, 100, 40))]
    assert refs(changes.carry_refs(with_id, moved, moves_content=None)) == ["e2"]  # an identifier is the same element


# ---- changes off screen (AGENT-TOOLS v1.6) ----
def test_a_changed_element_off_screen_gets_a_line_without_a_ref():
    old = [el("", role="text", label="taps=1", identifier="lab-state", y=1400), el("", label="Row 9", y=1500)]
    new = [el("", role="text", label="taps=2", identifier="lab-state", y=1400), el("", label="Row 9", y=1500)]
    assert changes.off_screen(old, new) == ['~ text "taps=2" id=lab-state (off screen; was "taps=1")']
    assert changes.off_screen(old, old) == []


def test_off_screen_an_identifier_follows_its_element_and_a_place_only_counts_when_nothing_moved():
    old = [el("", role="text", label="taps=1", identifier="lab-state", y=1400), el("", label="Edit", y=1500)]
    scrolled = [el("", role="text", label="taps=2", identifier="lab-state", y=1100), el("", label="Delete", y=1500)]
    after_scroll = changes.off_screen(old, scrolled, by_place=False)  # the row at 1500 is another row now
    assert after_scroll == ['~ text "taps=2" id=lab-state (off screen; was "taps=1")']
    in_place = changes.off_screen(old, [old[0], el("", label="Delete", y=1500)])
    assert in_place == ['~ button "Delete" (off screen; was "Edit")']
    twice = [el("", label="A", identifier="row", y=1400), el("", label="B", identifier="row", y=1500)]
    moved = [el("", label="C", identifier="row", y=1450), el("", label="D", identifier="row", y=1550)]
    assert changes.off_screen(twice, moved) == []  # an identifier two elements share names neither


def test_off_screen_lists_five_and_counts_the_rest_and_new_or_gone_elements_are_not_news():
    old = [el("", label=f"v{i}", identifier=f"id-{i}", y=1000 + 50 * i) for i in range(7)]
    new = [el("", label=f"w{i}", identifier=f"id-{i}", y=1000 + 50 * i) for i in range(7)]
    lines = changes.off_screen(old, new)
    assert len(lines) == 6 and lines[-1] == "~ 2 more changed off screen"
    assert changes.off_screen(old[:2], old) == [] and changes.off_screen(old, old[:2]) == []
    switch = [el("", role="switch", label="Wi-Fi", identifier="wifi", y=1400, checked=False)]
    flipped = [el("", role="switch", label="Wi-Fi", identifier="wifi", y=1400, checked=True)]
    assert changes.off_screen(switch, flipped)[0].endswith('(off screen; was "false")')


def test_a_row_removed_off_screen_does_not_make_every_row_below_it_look_changed():
    """A review's case: row 5 deleted, the rows below move up by one: by place each would read as its neighbour
    changed. A place counts only while every element off screen is where it was."""
    old = [el("", role="text", label=f"Row {i}", y=1000 + 60 * i) for i in range(5, 12)]
    new = [el("", role="text", label=f"Row {i}", y=1000 + 60 * (i - 1)) for i in range(6, 12)]
    assert changes.off_screen(old, new) == []
    grown = [*old, el("", role="text", label="Row 12", y=1000 + 60 * 12)]
    assert changes.off_screen(old, grown) == []  # one more row at the end: the places are no longer the same set


def test_another_screen_that_reuses_an_identifier_is_not_a_change_off_screen():
    old = [el("", role="text", label=f"Old {i}", identifier=name, y=1000 + 60 * i)
           for i, name in enumerate(("title", "footer", "total", "note"))]
    other = [el("", role="text", label="Other footer", identifier="footer", y=1300),
             el("", role="text", label="Help", identifier="help", y=1400)]
    assert changes.off_screen(old, other, by_place=False) == []  # one identifier of four is left: another screen
    same = [replace(old[0], label="New 0"), *old[1:]]
    assert len(changes.off_screen(old, same, by_place=False)) == 1


def test_an_element_that_was_listed_and_is_off_screen_now_is_compared_with_what_it_was():
    """Tapline's phone check: the state line was on the agent's list, a scroll answered without a list took it off
    screen, the next tap changed it, and nothing said so."""
    listed = [el("e7", role="text", label="taps=1", identifier="lab-state", y=700)]
    now_off = [el("", role="text", label="taps=2", identifier="lab-state", y=-300)]
    assert changes.off_screen([], now_off, by_place=False, listed=listed) == [
        '~ text "taps=2" id=lab-state (off screen; was "taps=1")']
    assert changes.off_screen([], now_off, by_place=False) == []  # without the list there is nothing to compare with
    unchanged = [el("", role="text", label="taps=1", identifier="lab-state", y=-300)]
    assert changes.off_screen([], unchanged, by_place=False, listed=listed) == []


def test_an_empty_input_shows_its_placeholder_and_typing_into_it_says_so():
    """AGENT-TOOLS v1.7: WebDriverAgent reports an empty field's placeholder as its value; a first-time agent took
    Wayfare's empty Name field for filled in (2026-11-11)."""
    empty = ui.Element("e1", "input", "Name", "name", value="Alex Morgan", placeholder="Alex Morgan",
                       rect=(20, 100, 380, 50), actions=("tap", "type"))
    assert empty.line() == 'e1 input "Name" id=name placeholder="Alex Morgan" @20,100 380x50'
    typed = replace(empty, value="abc")
    assert typed.line() == 'e1 input "Name" id=name value="abc" @20,100 380x50'
    assert changes._was(empty, typed) == ['placeholder "Alex Morgan"']
    assert changes._was(typed, empty) == ['"abc"']  # cleared again: the placeholder is back on the line
    assert "value=" not in empty.line() and "placeholder=" not in typed.line()

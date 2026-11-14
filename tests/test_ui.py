"""The element model: roles, lines, the header, alerts and the keyboard. Recorded WebDriverAgent trees
(`/source?format=xml`) live in tests/data (hand-written, Settings and Calculator content only)."""
import json
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from tapwright import ui
from tapwright.device import DeviceError

DATA = Path(__file__).parent / "data"
SCREEN = (430.0, 932.0)


def load(name: str) -> str:
    return (DATA / f"{name}.xml").read_text(encoding="utf-8")


def node(kind: str, label: str = "", name: str = "", value: str = "", rect=(10, 10, 100, 40), children=(),
         **extra: str) -> ET.Element:
    attrs = {"type": f"XCUIElementType{kind}", "enabled": "true", **dict(zip(("x", "y", "width", "height"),
                                                                           map(str, rect))), **extra}
    for key, v in (("label", label), ("name", name), ("value", value)):
        if v:
            attrs[key] = v
    n = ET.Element(attrs["type"], attrs)
    n.extend(children)
    return n


def xml(n: ET.Element) -> str:
    return ET.tostring(n, encoding="unicode")


def one(kind: str, label: str = "", **kw) -> ui.Element:
    [e] = ui.elements(xml(node(kind, label, **kw)), SCREEN)
    return e


def test_the_settings_screen_as_lines():
    tree = ui.read(load("settings_main"), SCREEN)
    lines = [e.line() for e in tree.items]
    assert lines == [
        'e1 navigationbar id=Settings @0,59 430x96',
        'e2 header "Settings" @16,103 200x40',
        'e3 scroll id=com.apple.settings.root @0,155 430x777',
        'e4 input "Search" @16,163 398x36',
        'e5 cell "Airplane Mode" id=airplane-mode-cell @16,215 398x52 [tap]',
        'e6 text "Airplane Mode" @60,229 200x24',
        'e7 switch "Airplane Mode" checked=false @349,224 51x31',
        'e8 cell "Wi-Fi, Home" id=Wi-Fi value="Home" @16,268 398x52 [tap]',
        'e9 cell "Bluetooth" value="On" @16,321 398x52 [tap]',
        'e10 cell "Notifications" disabled @16,400 398x52 [tap]',
    ]  # not listed: "Accessibility", below the screen
    assert tree.more == 0 and tree.alert is None and tree.keyboard is False
    assert not any(e.in_alert or e.focused for e in tree.items)


def test_header_with_every_line():
    tree = ui.read(load("settings_alert_keyboard"), SCREEN)
    text = ui.describe(tree.items, SCREEN, 3, ("Settings", "com.apple.Preferences"), tree.alert, tree.keyboard)
    assert text.splitlines() == [
        "Screen 430x932 pt @3x · 4 elements",
        "App: Settings (com.apple.Preferences)",
        'Alert: "Reset All Settings?" [Cancel] [Reset]',
        "Keyboard: shown",
        'e1 input "Search" value="wifi" @16,163 398x36',
        'e2 text "Reset All Settings?" @94,376 242x22',
        'e3 button "Cancel" @78,490 137x50',
        'e4 button "Reset" @215,490 137x50',
    ]


def test_an_os_alert_is_a_header_line_and_its_buttons_stay_tappable():
    tree = ui.read(load("settings_alert_keyboard"), SCREEN)
    assert tree.alert == ui.Alert("Reset All Settings?", ("Cancel", "Reset"), (78.0, 360.0, 274.0, 180.0))
    assert [e.role for e in tree.items if e.label in ("Cancel", "Reset")] == ["button", "button"]
    assert "alert" not in [e.role for e in tree.items]  # the container itself isn't listed
    assert [e.in_alert for e in tree.items] == [False, True, True, True]  # the search field is outside it
    assert tree.keyboard is True and "q" not in [e.label for e in tree.items]  # keys only on request
    assert "q" in [e.label for e in ui.read(load("settings_alert_keyboard"), SCREEN, keyboard=True).items]


def test_the_header_without_the_optional_lines():
    text = ui.describe([], SCREEN)
    assert text.splitlines() == ["Screen 430x932 pt · 0 elements",
                                 "(nothing visible; the app may still be loading)"]
    items = ui.read(load("calculator"), SCREEN).items[:1]
    assert ui.describe(items, SCREEN, scale=2.5).splitlines()[0] == "Screen 430x932 pt @2.5x · 1 elements"


def test_calculator_text_buttons_and_the_same_label_twice():
    items = ui.read(load("calculator"), SCREEN).items
    assert [e.line() for e in items][:3] == ['e1 text "42" @326,294 88x87',
                                             'e2 button "clear" id=AC @14,420 90x90',
                                             'e3 button "7" @14,520 90x90']
    assert [e.line() for e in items][-1] == 'e7 button "7" id=7-memory @14,620 90x90'


def test_roles_actions_and_unknown_types():
    cases = {"Button": ("button", ("tap",)), "Link": ("link", ("tap",)), "StaticText": ("text", ()),
             "TextField": ("input", ("tap", "type")), "SecureTextField": ("input", ("tap", "type")),
             "SearchField": ("input", ("tap", "type")), "TextView": ("input", ("tap", "type")),
             "Switch": ("switch", ("toggle",)), "Toggle": ("switch", ("toggle",)), "Slider": ("slider", ()),
             "Image": ("image", ()), "Icon": ("image", ()), "Tab": ("tab", ("tap",)), "Cell": ("cell", ("tap",)),
             "ScrollView": ("scroll", ("scroll",)), "Table": ("scroll", ("scroll",)),
             "CollectionView": ("scroll", ("scroll",)), "Stepper": ("stepper", ()),
             "SegmentedControl": ("segmentedcontrol", ())}
    for kind, (role, actions) in cases.items():
        e = one(kind, "x")
        assert (e.role, e.actions, e.type) == (role, actions, kind), kind


def test_a_button_inside_a_tab_bar_is_a_tab():
    bar = node("TabBar", rect=(0, 840, 430, 92), children=[node("Button", "Wi-Fi"), node("Tab", "Bluetooth")])
    assert [e.role for e in ui.elements(xml(bar), SCREEN)] == ["tabbar", "tab", "tab"]
    plain = node("Other", children=[node("Button", "Back")])
    assert [e.role for e in ui.elements(xml(plain), SCREEN)] == ["button"]


def test_only_a_static_text_with_the_header_trait_is_a_header():
    assert one("StaticText", "General", traits="StaticText,Header").role == "header"
    assert one("StaticText", "General", traits="StaticText").role == "text"
    assert one("Button", "General", traits="Button,Header").role == "button"


def test_switch_state_selected_and_disabled():
    on = one("Switch", "Bluetooth", value="1")
    off = one("Switch", "Bluetooth", value="0")
    assert (on.checked, off.checked) == (True, False)
    assert on.line() == 'e1 switch "Bluetooth" checked=true @10,10 100x40'  # no value="1" as well
    assert one("Button", "Wi-Fi").checked is None
    picked = one("Tab", "Bluetooth", traits="Button,Selected", enabled="false")
    assert picked.selected and not picked.enabled
    assert picked.line() == 'e1 tab "Bluetooth" disabled selected @10,10 100x40'
    assert not one("Tab", "Bluetooth", traits="Button").selected


def test_enabled_is_false_for_enabled_false_or_the_not_enabled_trait():
    assert not one("Button", "A", enabled="false").enabled
    assert not one("Button", "B", traits="Button,NotEnabled").enabled
    assert one("Button", "C", traits="Button").enabled


def test_the_line_clips_flattens_and_quotes_text():
    long = "Line one\n   line   two " + "x" * 200
    e = one("StaticText", long)
    quoted = json.loads(e.line().split(" ", 2)[2].rsplit(" @", 1)[0])
    # AGENT-TOOLS v1.5: cut at 200 characters, and the cut says how many are missing
    assert quoted == ("Line one line two " + "x" * 200)[:200] + "…(+18)"
    exact = one("StaticText", "y" * 200)
    assert json.loads(exact.line().split(" ", 2)[2].rsplit(" @", 1)[0]) == "y" * 200  # nothing cut: no mark
    odd = one("StaticText", 'Say "hi"', value='Say "hi" now')
    assert odd.line() == 'e1 text "Say \\"hi\\"" value="Say \\"hi\\" now" @10,10 100x40'
    assert one("TextField", "Email", value="a@b.example").line() == \
        'e1 input "Email" value="a@b.example" @10,10 100x40'
    only_value = one("StaticText", value="Home")
    assert only_value.text == "Home" and only_value.line() == 'e1 text "Home" @10,10 100x40'


def test_line_without_a_ref_or_without_actions():
    e = ui.read(load("calculator"), SCREEN).items[1]
    assert e.line(ref=False) == 'button "clear" id=AC @14,420 90x90'
    assert e.line(actions=False) == 'e2 button "clear" id=AC @14,420 90x90'
    assert e.line(ref=False, actions=False) == 'button "clear" id=AC @14,420 90x90'
    assert e.rect == (14.0, 420.0, 90.0, 90.0) and e.center == (59.0, 465.0)


def test_a_line_leaves_out_the_position_on_request():
    e = ui.Element("e3", "button", "Log in", "login-button", rect=(24, 610, 354, 48), actions=("tap",))
    assert e.line(place=False) == 'e3 button "Log in" id=login-button'
    assert e.line(place=False, ref=False) == 'button "Log in" id=login-button'


def test_the_actions_a_role_implies_are_not_listed_and_any_other_set_is():
    """AGENT-TOOLS 1.8 §13 rule 4."""
    def line(role, *actions):
        return ui.Element("e1", role, "X", rect=(0, 0, 10, 10), actions=actions).line()
    for role, actions in (("button", ("tap",)), ("tab", ("tap",)), ("link", ("tap",)),
                          ("input", ("tap", "type")), ("switch", ("toggle",)), ("scroll", ("scroll",))):
        assert line(role, *actions) == f'e1 {role} "X" @0,0 10x10'
    assert line("button", "tap", "longPress") == 'e1 button "X" @0,0 10x10 [tap, longPress]'
    assert line("cell", "tap") == 'e1 cell "X" @0,0 10x10 [tap]'
    assert line("text", "tap") == 'e1 text "X" @0,0 10x10 [tap]'
    assert line("input", "tap") == 'e1 input "X" @0,0 10x10 [tap]'
    assert line("button") == 'e1 button "X" @0,0 10x10'


def test_without_place_takes_the_position_off_the_end_of_a_first_line_only():
    assert ui.without_place('Tapped e3 button "Log in" id=login-button @24,610 354x48.') == \
        'Tapped e3 button "Log in" id=login-button.'
    assert ui.without_place("Tapped (215, 640).") == "Tapped (215, 640)."
    assert ui.without_place('Tapped e3 button "At @1,2 3x4 now".') == 'Tapped e3 button "At @1,2 3x4 now".'


def test_visibility_comes_from_the_rect():
    tree = ui.read(load("settings_main"), SCREEN)
    assert "Accessibility" not in [e.label for e in tree.items]  # y=1000 is below the 932 pt screen
    flat = node("Other", rect=(0, 0, 430, 932), children=[
        node("Button", "Zero", rect=(10, 10, 0, 40)), node("Button", "Left", rect=(-120, 10, 100, 40)),
        node("Button", "Right", rect=(430, 10, 100, 40)), node("Button", "Edge", rect=(-10, 10, 60, 40)),
        node("Button", "Sliver", rect=(-50, 10, 60, 40))])
    assert [e.label for e in ui.elements(xml(flat), SCREEN)] == ["Edge"]  # the centre must be on screen


def test_the_list_holds_200_and_counts_the_rest():
    rows = [node("Button", f"Row {i}", rect=(0, i, 100, 1)) for i in range(250)]
    tree = ui.read(xml(node("Other", rect=(0, 0, 430, 932), children=rows)), SCREEN)
    assert len(tree.items) == 200 and tree.more == 50 and tree.items[-1].ref == "e200"
    text = ui.describe(tree.items, SCREEN, more=tree.more)
    assert text.splitlines()[0].endswith("· 200 elements") and text.splitlines()[-1] == "(+50 more below; scroll)"


def test_direction_marks_are_dropped():
    # iOS Calculator, measured: the result is a StaticText labelled "‎42".
    e = ui.read(load("calculator"), SCREEN).items[0]
    assert e.label == "42" and e.line() == 'e1 text "42" @326,294 88x87'


def test_a_scroll_container_clips_what_is_inside_it():
    table = node("Table", name="list", rect=(0, 300, 430, 400), children=[
        node("Cell", "Scrolled above", rect=(16, 200, 398, 52)),  # inside the screen, outside the table
        node("Cell", "Half hidden", rect=(16, 280, 398, 52)),  # centre y=306: inside the table
        node("Cell", "Centre clipped", rect=(16, 260, 398, 52)),  # centre y=286: above the table
        node("Cell", "Visible", rect=(16, 400, 398, 52)),
        node("Cell", "Past the bottom", rect=(16, 750, 398, 52))])
    labels = [e.label for e in ui.elements(xml(node("Other", rect=(0, 0, 430, 932), children=[table])), SCREEN)]
    assert labels == ["", "Half hidden", "Visible"]  # "" is the table itself


def test_the_clip_applies_to_web_views_and_collection_views():
    for kind in ("WebView", "CollectionView", "ScrollView"):
        view = node(kind, name="v", rect=(0, 300, 430, 400), children=[node("Button", "Above", rect=(0, 100, 50, 50))])
        assert "Above" not in [e.label for e in ui.elements(xml(view), SCREEN)], kind


def test_find_by_id_uses_the_raw_name_even_when_it_equals_the_label():
    e = one("Button", "Login", name="Login")
    assert e.identifier == "" and e.name == "Login"  # not shown: it only mirrors the label
    assert e.line() == 'e1 button "Login" @10,10 100x40'
    assert ui.resolve([e], ui.Target(id="Login")) is e
    assert ui.resolve([one("Button", "OK", name="ok-button")], ui.Target(id="ok-button")).label == "OK"


def test_control_characters_are_removed_before_parsing():
    raw = '<A><XCUIElementTypeButton type="XCUIElementTypeButton" label="Hi&#x1;&#11;there' + chr(2) + '" x="0" y="0" width="50" height="50"/></A>'
    assert [e.label for e in ui.elements(raw, SCREEN)] == ["Hithere"]
    tab = '<A><XCUIElementTypeButton type="XCUIElementTypeButton" label="a&#9;b" x="0" y="0" width="50" height="50"/></A>'
    assert ui.elements(tab, SCREEN)[0].label == "a	b"  # tab, newline and return stay legal


def test_an_unreadable_tree_is_a_device_error():
    with pytest.raises(DeviceError, match="unreadable element tree"):
        ui.read('<AppiumAUT><XCUIElementTypeButton type="XCUIElementTypeButton" label="x', SCREEN)


def test_a_bad_rect_counts_as_zero_size():
    for bad in ({"width": "inf"}, {"x": "nan"}, {"height": "1e999"}, {"width": "wide"}):
        attrs = {"x": "0", "y": "0", "width": "50", "height": "50", **bad}
        n = '<XCUIElementTypeButton type="XCUIElementTypeButton" label="b" ' + " ".join(
            f'{k}="{v}"' for k, v in attrs.items()) + "/>"
        assert ui.elements(n, SCREEN) == [], bad
    assert ui._rect(ET.fromstring('<A x="inf" y="0" width="5" height="5"/>')) == (0.0, 0.0, 0.0, 0.0)


def test_traits_are_split_on_commas_and_stripped():
    assert one("StaticText", "General", traits="StaticText, Header").role == "header"
    assert one("Tab", "A", traits="Button, Selected").selected
    assert not one("Button", "B", traits="Button, NotEnabled").enabled


def test_a_real_identifier_wins_over_a_name_that_mirrors_a_label():
    mirror = one("Button", "Settings", name="Settings")  # id only because the name copies the label
    real = one("StaticText", "Other", name="Settings")
    assert ui.resolve([mirror, real], ui.Target(id="Settings")) is real
    assert ui.resolve([mirror], ui.Target(id="Settings")) is mirror  # with no real id, the mirror still matches


def _wrapped(wrapper: ET.Element) -> list[ui.Element]:
    return ui.elements(xml(node("Other", children=[wrapper])), SCREEN)


def test_every_invisible_direction_mark_is_removed_from_text_label_and_value():
    marks = "‎‏‪‫‬‭‮⁦⁧⁨⁩"
    e = one("Cell", f"{marks}Tot{marks}al", value=f"4{marks}2", name=f"{marks}id")
    assert (e.label, e.value, e.identifier) == ("Total", "42", "id")
    assert ui.resolve([e], ui.Target(label="total")) is e and ui.resolve([e], ui.Target(text="42")) is e
    assert e.line() == 'e1 cell "Total" id=id value="42" @10,10 100x40 [tap]'


def test_a_wrapper_and_its_text_with_the_same_label_and_rect_are_one_element():
    """React Native exposes a wrapper and its text as two elements: list one."""
    text = node("Button", "Sign in", rect=(10, 10, 200, 40))
    [e] = _wrapped(node("Button", "Sign in", name="sign-in", rect=(10, 10, 200, 40), children=[text]))
    assert e.line() == 'e1 button "Sign in" id=sign-in @10,10 200x40'  # the ancestor stays
    [e] = _wrapped(node("Button", "Sign in", rect=(10.4, 10, 200.9, 40), children=[text]))
    assert e.ref == "e1"  # rects equal within 1 pt


def test_roles_may_differ_and_the_one_with_actions_is_listed():
    """v1.3: same quoted text and rect make one element, whatever the roles; the one that takes an action stays."""
    [e] = _wrapped(node("StaticText", "Sign in", rect=(10, 10, 200, 40), children=[node("StaticText", "Sign in", rect=(10, 10, 200, 40))]))
    assert (e.role, e.ref) == ("text", "e1")
    inner = node("Button", "Sign in", rect=(10, 10, 200, 40))
    [e] = _wrapped(node("StaticText", "Sign in", rect=(10, 10, 200, 40), children=[inner]))
    assert (e.role, e.ref, e.actions) == ("button", "e1", ("tap",))  # the child has the action
    text = node("StaticText", "Sign in", rect=(10, 10, 200, 40))
    [e] = _wrapped(node("Button", "Sign in", rect=(10, 10, 200, 40), children=[text]))
    assert e.role == "button"  # the ancestor has it
    [e] = _wrapped(node("Image", "Sign in", name="logo", rect=(10, 10, 200, 40), children=[node("StaticText", "Sign in", rect=(10, 10, 200, 40))]))
    assert (e.role, e.identifier) == ("image", "logo")  # neither has one: the ancestor


def test_an_other_wrapper_around_an_input_lists_only_the_input():
    field = node("TextField", "Email", value="me@example.com", rect=(10, 10, 200, 40))
    [e] = _wrapped(node("Other", "Email", rect=(10, 10, 200, 40), children=[field]))
    assert e.line() == 'e1 input "Email" value="me@example.com" @10,10 200x40'
    [e] = _wrapped(node("Other", "Email", name="email-wrapper", rect=(10, 10, 200, 40), children=[field]))
    assert e.role == "input"  # an identifier only one of them carries doesn't keep both


def test_identifiers_and_values_keep_both_only_when_both_carry_them():
    assert len(_pair({"kind": "Other", "name": "a"}, {"kind": "Button", "name": "b"})) == 2
    assert len(_pair({"kind": "Other", "name": "a"}, {"kind": "Button", "value": "v"})) == 1
    assert len(_pair({"kind": "StaticText", "value": "x"}, {"kind": "Button", "value": "y"})) == 2


def test_different_labels_or_rects_are_kept():
    items = _wrapped(node("Button", "Sign in", rect=(10, 10, 200, 40),
                          children=[node("StaticText", "Sign in", rect=(10, 10, 200, 40)),
                                    node("Button", "Sign in", rect=(10, 10, 200, 44)),
                                    node("Button", "Sign up", rect=(10, 10, 200, 40))]))
    assert [(e.role, e.label, e.rect[3]) for e in items] == [("button", "Sign in", 40), ("button", "Sign in", 44),
                                                             ("button", "Sign up", 40)]  # the text child folds into the button


def test_dropped_duplicates_do_not_count_against_the_cap_or_the_numbers():
    kids = [node("StaticText", f"Row {i}", rect=(10, 10 + i, 100, 20),
                 children=[node("StaticText", f"Row {i}", rect=(10, 10 + i, 100, 20))]) for i in range(3)]
    items = ui.elements(xml(node("Other", rect=(0, 0, 430, 932), children=kids)), SCREEN)
    assert [e.ref for e in items] == ["e1", "e2", "e3"]


def _pair(parent: dict, child: dict) -> list[ui.Element]:
    p = {"kind": "Button", "label": "Go", "rect": (10, 10, 100, 40)}
    c = {"kind": "Button", "label": "Go", "rect": (10, 10, 100, 40)}
    p.update(parent)
    c.update(child)
    inner = node(c.pop("kind"), c.pop("label"), **c)
    return _wrapped(node(p.pop("kind"), p.pop("label"), children=[inner], **p))


def test_a_child_is_collapsed_only_when_nothing_is_lost():
    assert len(_pair({}, {})) == 1
    assert len(_pair({"name": "go-id"}, {"name": "go-id"})) == 1
    assert len(_pair({}, {"name": "child-id"})) == 1  # only the child carries an id: no clash
    assert len(_pair({"name": "a"}, {"name": "b"})) == 2
    assert len(_pair({}, {"value": "x"})) == 1  # only the child carries a value
    assert len(_pair({"value": "y"}, {"value": "x"})) == 2  # different values
    assert len(_pair({}, {"traits": "Selected"})) == 2
    assert len(_pair({}, {"enabled": "false"})) == 2
    assert len(_pair({"kind": "Switch", "value": "1"}, {"kind": "Switch", "value": "0"})) == 2  # another state
    assert len(_pair({"kind": "StaticText"}, {"kind": "Button"})) == 1  # another role folds (v1.3)
    assert len(_pair({}, {"label": "Stop"})) == 2
    assert len(_pair({}, {"rect": (10, 10, 100, 44)})) == 2


def test_the_child_with_an_action_the_parent_lacks_is_kept():
    assert len(_pair({"kind": "Other", "name": "g"}, {"kind": "Other", "name": "g"})) == 1  # no actions either side
    inner = node("Cell", "Row", rect=(10, 10, 100, 40))
    parent = node("Cell", "Row", rect=(10, 10, 100, 40), children=[inner])
    [e] = _wrapped(parent)
    assert e.actions == ("tap",)


def test_unlabeled_pairs_are_never_collapsed():
    assert len(_pair({"kind": "Image", "label": ""}, {"kind": "Image", "label": ""})) == 2


def test_a_calculator_key_is_a_button_but_the_keyboards_keys_stay_hidden():
    pad = node("Other", rect=(0, 400, 430, 400), children=[node("Key", "7", "Seven", rect=(14, 520, 90, 90))])
    [e] = [e for e in ui.elements(xml(pad), SCREEN) if e.type == "Key"]
    assert (e.role, e.actions, e.line()) == ("button", ("tap",), 'e1 button "7" id=Seven @14,520 90x90')
    board = node("Keyboard", rect=(0, 600, 430, 300), children=[node("Key", "q", rect=(10, 620, 40, 50))])
    assert [e.type for e in ui.elements(xml(board), SCREEN)] == []


def test_a_blank_touch_prefers_a_point_inside_a_scroll_view():
    from fakes import el
    """A tap in a scroll view closes React Native's keyboard; one on a header above it often doesn't (2026-11-12)."""
    items = [el("e1", "header", "Where to next?", rect=(20, 90, 390, 40)),
             el("e2", "scroll", "", rect=(0, 200, 430, 600)),
             el("e3", "text", "Kyoto", rect=(35, 370, 66, 28))]
    assert ui.blank_point(items, (430.0, 932.0), 50.0, 849.0) == (68.0, 384.0)


# ---- what lies wholly to the left or right of the screen is not off-screen content (V1.6 finding 6) ----
PHONE = (393.0, 852.0)


def stack(*, behind_x: float = -129.0, behind_children=(), pushed_children=()) -> ui.Tree:
    """A stack navigator mid-push: the previous screen kept mounted, slid to `behind_x`, and the new one in front."""
    behind = node("Other", rect=(behind_x, 0, 393, 852), children=behind_children)
    pushed = node("Other", rect=(0, 0, 393, 852), children=pushed_children)
    return ui.read(xml(node("Application", "App", rect=(0, 0, 393, 852), children=[behind, pushed])), PHONE)


def test_a_previous_screen_at_a_negative_x_leaves_no_element_in_the_tree_or_the_off_screen_list():
    tree = stack(behind_children=[node("StaticText", "Trips", rect=(-113, 100, 100, 20)),
                                  node("Button", "Add", "add-trip", rect=(-100, 200, 90, 40))],
                 pushed_children=[node("StaticText", "Plan a trip", rect=(20, 100, 200, 30))])
    assert [e.text for e in tree.items] == ["Plan a trip"]
    assert tree.offscreen == [] and (tree.above, tree.below) == (0, 0)


def test_what_lies_wholly_to_the_right_is_dropped_too_and_a_partly_visible_element_keeps_its_old_handling():
    tree = stack(behind_x=393.0, behind_children=[node("Button", "Next", "next", rect=(420, 200, 90, 40))],
                 pushed_children=[node("StaticText", "Plan a trip", rect=(20, 100, 200, 30)),
                                  node("StaticText", "Half", rect=(380, 300, 60, 20))])
    assert [e.text for e in tree.items] == ["Plan a trip"]
    assert [e.text for e in tree.offscreen] == ["Half"]  # its centre is past the edge but it touches the screen


def test_an_element_below_the_screen_is_still_counted_and_listed_as_off_screen():
    tree = stack(pushed_children=[node("Button", "Far", "far", rect=(20, 900, 90, 40))])
    assert [e.text for e in tree.offscreen] == ["Far"] and tree.below == 1


def test_a_horizontal_scroll_view_keeps_its_cards_beyond_the_edge():
    cards = node("ScrollView", "", "carousel", rect=(0, 100, 393, 120), children=[
        node("Button", "Card 1", "c1", rect=(16, 110, 160, 100)), node("Button", "Card 2", "c2", rect=(420, 110, 160, 100)),
        node("Button", "Card 3", "c3", rect=(600, 110, 160, 100))])
    tree = stack(pushed_children=[cards])
    assert [e.text for e in tree.items if e.role == "button"] == ["Card 1"]
    assert [e.text for e in tree.offscreen] == ["Card 2", "Card 3"]


def test_the_pages_of_a_pager_keep_their_content_even_inside_a_scroll_view_of_their_own():
    page = node("Other", "", "page-2", rect=(393, 0, 393, 852), children=[
        node("ScrollView", "", "page-2-list", rect=(393, 0, 393, 852), children=[
            node("StaticText", "Second page", rect=(413, 100, 200, 30))])])
    pager = node("ScrollView", "", "pager", rect=(0, 0, 393, 852), children=[page])
    tree = stack(pushed_children=[pager])
    assert "Second page" in [e.text for e in tree.offscreen]


def test_a_scroll_view_of_the_previous_screen_is_no_pager():
    old = node("ScrollView", "", "old-list", rect=(-129, 0, 393, 852), children=[
        node("StaticText", "Old row", rect=(-109, 100, 100, 20))])
    tree = stack(behind_children=[old], pushed_children=[node("StaticText", "Plan a trip", rect=(20, 100, 200, 30))])
    assert tree.offscreen == [] and [e.text for e in tree.items if e.text] == ["Plan a trip"]
# React Native gives accessibilityRole radio and checkbox no accessibility trait, so XCUITest types such a view
# `Other`, and VoiceOver speaks the role's words after the label. These are the lines an agent should get for it.
def _option(label: str, word: str, y: int, same_rect: bool = False) -> ET.Element:
    text_rect = (16, y, 398, 44) if same_rect else (56, y + 10, 80, 24)
    return node("Other", label, rect=(16, y, 398, 44), children=[node("StaticText", word, rect=text_rect)])


def _radio_group(same_rect: bool = False) -> list[ui.Element]:
    group = node("Application", rect=(0, 0, 430, 932), children=[
        _option("Daily, radio button, checked", "Daily", 200, same_rect),
        _option("Weekly, radio button", "Weekly", 250, same_rect),
        _option("Monthly, radio button", "Monthly", 300, same_rect)])
    return ui.elements(xml(group), SCREEN)


def test_a_react_native_radio_option_is_listed_as_a_radio_with_its_state():
    lines = [e.line() for e in _radio_group()]
    assert lines == ['e1 radio "Daily" checked=true @16,200 398x44 [tap]', 'e2 text "Daily" @56,210 80x24',
                     'e3 radio "Weekly" @16,250 398x44 [tap]', 'e4 text "Weekly" @56,260 80x24',
                     'e5 radio "Monthly" @16,300 398x44 [tap]', 'e6 text "Monthly" @56,310 80x24']
    daily = _radio_group()[0]
    assert (daily.type, daily.actions, daily.checked, daily.no_trait) == ("Other", ("tap",), True, True)


def test_a_react_native_checkbox_and_the_state_words():
    def checkbox(label: str, value: str = "") -> ui.Element:
        n = node("Other", label, value=value, rect=(16, 400, 398, 44),
                 children=[node("StaticText", "Remember me", rect=(56, 410, 120, 24))])
        return ui.elements(xml(node("Application", rect=(0, 0, 430, 932), children=[n])), SCREEN)[0]
    assert checkbox("Remember me, checkbox, unchecked").line() == 'e1 checkbox "Remember me" checked=false @16,400 398x44 [tap]'
    assert checkbox("Remember me, Checkbox, not checked").checked is False  # case doesn't matter
    assert checkbox("Remember me, checkbox, checked").checked is True
    assert checkbox("Remember me, checkbox, selected").checked is True
    assert checkbox("Remember me, checkbox").checked is None  # no state said, none shown
    assert checkbox("Remember me, checkbox").line() == 'e1 checkbox "Remember me" @16,400 398x44 [tap]'
    e = checkbox("Remember me", value="CheckBox, checked")  # the words may come in the value
    assert (e.role, e.label, e.value, e.checked) == ("checkbox", "Remember me", "", True)


def test_a_state_word_before_the_role_is_part_of_the_label_not_the_state():
    """CodeRabbit, PR #8: an option named "Selected" (an RN checkbox, unchecked) was checked=true with no label."""
    def option(label: str, value: str = "") -> ui.Element:
        n = node("Other", label, value=value, rect=(16, 400, 398, 44),
                 children=[node("StaticText", "Selected", rect=(56, 410, 120, 24))])
        return ui.elements(xml(node("Application", rect=(0, 0, 430, 932), children=[n])), SCREEN)[0]
    e = option("Selected, checkbox, unchecked")
    assert (e.role, e.label, e.checked) == ("checkbox", "Selected", False)
    assert e.line() == 'e1 checkbox "Selected" checked=false @16,400 398x44 [tap]'
    e = option("Checked, radio button")
    assert (e.role, e.label, e.checked) == ("radio", "Checked", None)
    e = option("Selected, radio button, selected")
    assert (e.label, e.checked) == ("Selected", True)
    e = option("Selected", value="checkbox, checked")  # the label has no role token: it stays whole
    assert (e.role, e.label, e.checked) == ("checkbox", "Selected", True)
    e = option("Selected, checkbox", value="unchecked")
    assert (e.label, e.checked) == ("Selected", False)
    assert ui.rn_label("Selected, checkbox, unchecked") == "Selected" and ui.rn_label("Total, 3 items") == "Total, 3 items"


def _screen_of(*children: ET.Element) -> list[ui.Element]:
    return ui.elements(xml(node("Application", rect=(0, 0, 430, 932), children=children)), SCREEN)


def test_an_other_without_role_words_is_listed_as_before():
    """Only React Native's role words make an `Other` a control. Any other `Other` is as it was before 1.5.1: a plain
    labelled view is not listed (reading `accessible` would say which are controls, and that is not read yet)."""
    # the scroll indicators UIKit adds to an alert's scroll area, seen on the phone 2026-11-12
    bars = [node("Other", "Vertical scroll bar, 1 page", value="0%", rect=(420, 100, 4, 200)),
            node("Other", "Horizontal scroll bar, 1 page", value="0%", rect=(16, 300, 200, 4))]
    assert _screen_of(*bars) == []
    # a card with a custom label, with or without children, is not a control either
    card = node("Other", "Order summary", rect=(16, 100, 300, 80),
                children=[node("StaticText", "Total", rect=(20, 110, 60, 24)), node("StaticText", "$5", rect=(100, 110, 30, 24))])
    assert [e.line() for e in _screen_of(card)] == ['e1 text "Total" @20,110 60x24', 'e2 text "$5" @100,110 30x24']
    assert _screen_of(node("Other", "Help", rect=(16, 100, 100, 44))) == []
    # the label "..." over a text "…" (symbols only) is no longer a guess about children's text
    assert [e.role for e in _screen_of(node("Other", "...", rect=(16, 100, 300, 44),
                                            children=[node("StaticText", "…", rect=(20, 110, 20, 24))]))] == ["text"]
    assert not any(e.no_trait for e in _screen_of(*bars, card))


def test_a_label_target_stays_unique_beside_an_other_with_the_same_label():
    items = _screen_of(node("Other", "Help", rect=(16, 100, 40, 40)), node("StaticText", "Help", rect=(100, 100, 60, 24)))
    assert [e.role for e in items] == ["text"]
    assert ui.resolve(items, ui.parse_target({"label": "Help"})).role == "text"
    assert ui.resolve(items, ui.parse_target({"text": "Help"})).role == "text"


def test_the_off_screen_list_has_no_plain_other_either():
    group = node("Application", rect=(0, 0, 430, 932), children=[
        node("Other", "Vertical scroll bar, 1 page", value="0%", rect=(420, 1000, 4, 200)),
        node("Other", "Help", rect=(16, 1100, 100, 44))])
    tree = ui.read(xml(group), SCREEN)
    assert tree.items == [] and tree.offscreen == []


def test_an_other_with_an_identifier_keeps_its_line():
    [e, _] = ui.elements(xml(node("Application", rect=(0, 0, 430, 932), children=[
        node("Other", "Daily, radio button", name="plan-daily", rect=(16, 200, 398, 44),
             children=[node("StaticText", "Daily", rect=(56, 210, 80, 24))])])), SCREEN)
    assert e.line() == 'e1 other "Daily, radio button" id=plan-daily @16,200 398x44' and not e.no_trait
    [e] = ui.elements(xml(node("Application", rect=(0, 0, 430, 932), children=[
        node("Other", name="screen-root", rect=(16, 200, 398, 44))])), SCREEN)
    assert e.line() == 'e1 other id=screen-root @16,200 398x44'


@pytest.mark.parametrize("same_rect", [False, True])
def test_a_label_target_picks_the_option_not_its_text(same_rect):
    items = _radio_group(same_rect)
    weekly = ui.resolve(items, ui.parse_target({"label": "Weekly"}))
    assert isinstance(weekly, ui.Element) and (weekly.role, weekly.actions) == ("radio", ("tap",))
    assert ui.resolve(items, ui.parse_target({"text": "Daily"})).role == "radio"
    assert [e.role for e in items if e.label == "Weekly"] == (["radio"] if same_rect else ["radio", "text"])


def test_an_option_scrolled_off_screen_is_still_a_target_that_is_off_screen():
    group = node("Application", rect=(0, 0, 430, 932), children=[_option("Yearly, radio button", "Yearly", 1000)])
    tree = ui.read(xml(group), SCREEN)
    assert tree.items == [] and [(e.role, e.label, e.no_trait) for e in tree.offscreen] == [
        ("radio", "Yearly", True), ("text", "Yearly", False)]


ALERT_XML = """<AppiumAUT><XCUIElementTypeApplication type="XCUIElementTypeApplication" name="Wayfare" label="Wayfare" x="0" y="0" width="430" height="932">
<XCUIElementTypeWindow type="XCUIElementTypeWindow" x="0" y="0" width="430" height="932">
<XCUIElementTypeAlert type="XCUIElementTypeAlert" name="Allow notifications?" label="Allow notifications?" x="78" y="360" width="274" height="180">
<XCUIElementTypeOther type="XCUIElementTypeOther" x="78" y="360" width="274" height="180">
<XCUIElementTypeStaticText type="XCUIElementTypeStaticText" name="Allow notifications?" label="Allow notifications?" value="Allow notifications?" x="94" y="376" width="242" height="22"/>
{message}
<XCUIElementTypeButton type="XCUIElementTypeButton" name="Don't Allow" label="Don't Allow" x="78" y="490" width="137" height="50"/>
<XCUIElementTypeButton type="XCUIElementTypeButton" name="Allow" label="Allow" x="215" y="490" width="137" height="50"/>
</XCUIElementTypeOther></XCUIElementTypeAlert></XCUIElementTypeWindow></XCUIElementTypeApplication></AppiumAUT>"""


def alert_tree(message: str = "Wayfare would like to send you notifications.") -> ui.Tree:
    body = (f'<XCUIElementTypeStaticText type="XCUIElementTypeStaticText" name="{message}" label="{message}" '
            f'value="{message}" x="94" y="404" width="242" height="40"/>') if message else ""
    return ui.read(ALERT_XML.replace("{message}", body), SCREEN)


def test_the_alert_line_carries_the_alerts_message():
    tree = alert_tree()
    assert tree.alert is not None and tree.alert.message == "Wayfare would like to send you notifications."
    assert ui.alert_line(tree.alert) == ('Alert: "Allow notifications?" "Wayfare would like to send you notifications." '
                                         "[Don't Allow] [Allow]")
    text = ui.describe(tree.items, SCREEN, 3, ("Wayfare", "com.example.wayfare"), tree.alert, False)
    assert text.splitlines()[2].startswith('Alert: "Allow notifications?" "Wayfare would')


def test_an_alert_without_a_message_keeps_todays_line():
    tree = alert_tree("")
    assert tree.alert is not None and tree.alert.message == ""
    assert ui.alert_line(tree.alert) == 'Alert: "Allow notifications?" [Don\'t Allow] [Allow]'


def test_the_alert_message_is_cut_like_element_text_and_redacted_before_the_cut():
    long = "word " * 80
    line = ui.alert_line(ui.Alert("Title", ("OK",), (0, 0, 1, 1), long))
    assert '…(+' in line and line.endswith("[OK]")
    secret = "hunter2-secret"
    token = ui.REDACT.set(lambda t: t.replace(secret, "<tester's password>"))
    try:
        # the value sits across the cut: redacted first, so no half of it is left behind
        padded = "x" * 195 + secret
        line = ui.alert_line(ui.Alert("T", ("OK",), (0, 0, 1, 1), padded))
    finally:
        ui.REDACT.reset(token)
    assert "hunter" not in line and "secret" not in line


def test_the_message_is_data_for_the_line_not_a_policy_word_and_control_marks_go():
    alert = ui.Alert("Delete trip?", ("Cancel", "Delete"), (0, 0, 1, 1), "‎Send\n  Post  it")
    assert 'Alert: "Delete trip?" "Send Post it"' in ui.alert_line(alert) or "Send" in ui.alert_line(alert)


def test_the_keyboard_accessory_window_hides_nothing_but_its_bar():
    """Seen on the phone 2026-11-14 (Reminders' New Reminder sheet): UIKit hosts the bar over the keyboard in a
    full-screen window that starts with an empty full-screen view, like a sheet's backdrop; it is no layer, so the
    sheet's fields stay listed. Shape from a capture with every text replaced; synthetic words."""
    def window(inner):
        return ('<XCUIElementTypeWindow type="XCUIElementTypeWindow" x="0" y="0" width="430" height="932">'
                f'{inner}</XCUIElementTypeWindow>')
    app_window = window(
        '<XCUIElementTypeOther type="XCUIElementTypeOther" x="0" y="59" width="430" height="873">'
        '<XCUIElementTypeOther type="XCUIElementTypeOther" x="-430" y="-932" width="1290" height="2796"/>'
        '<XCUIElementTypeTable type="XCUIElementTypeTable" x="0" y="59" width="430" height="873">'
        '<XCUIElementTypeCell type="XCUIElementTypeCell" x="20" y="141" width="390" height="76">'
        '<XCUIElementTypeTextField type="XCUIElementTypeTextField" name="title-field" label="Title" x="40" y="158" '
        'width="350" height="42"/></XCUIElementTypeCell></XCUIElementTypeTable></XCUIElementTypeOther>')
    accessory = window(
        '<XCUIElementTypeOther type="XCUIElementTypeOther" x="0" y="0" width="430" height="932">'
        '<XCUIElementTypeOther type="XCUIElementTypeOther" x="0" y="0" width="430" height="932"/>'
        '<XCUIElementTypeOther type="XCUIElementTypeOther" x="0" y="558" width="430" height="374">'
        '<XCUIElementTypeToolbar type="XCUIElementTypeToolbar" name="Toolbar" x="0" y="558" width="430" height="48">'
        '<XCUIElementTypeButton type="XCUIElementTypeButton" name="Flag" label="Flag" x="352" y="563" width="53" '
        'height="38"/></XCUIElementTypeToolbar>'
        '<XCUIElementTypeOther type="XCUIElementTypeOther" name="inputView" x="0" y="614" width="430" height="318"/>'
        '</XCUIElementTypeOther></XCUIElementTypeOther>')
    keys = window(
        '<XCUIElementTypeOther type="XCUIElementTypeOther" x="0" y="0" width="430" height="932">'
        '<XCUIElementTypeOther type="XCUIElementTypeOther" x="0" y="631" width="430" height="243">'
        '<XCUIElementTypeKeyboard type="XCUIElementTypeKeyboard" x="0" y="631" width="430" height="243">'
        '<XCUIElementTypeKey type="XCUIElementTypeKey" name="q" label="q" x="4" y="640" width="40" height="40"/>'
        '</XCUIElementTypeKeyboard></XCUIElementTypeOther></XCUIElementTypeOther>')
    source = ('<AppiumAUT><XCUIElementTypeApplication type="XCUIElementTypeApplication" name="Reminders" '
              'label="Reminders" bundleId="com.apple.reminders" x="0" y="0" width="430" height="932">'
              f'{app_window}{accessory}{keys}</XCUIElementTypeApplication></AppiumAUT>')
    tree = ui.read(source, (430.0, 932.0))
    assert [e.identifier for e in tree.items if e.role == "input"] == ["title-field"]
    assert any(e.label == "Flag" for e in tree.items) and tree.keyboard

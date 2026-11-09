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
        'e3 scroll id=com.apple.settings.root @0,155 430x777 [scroll]',
        'e4 input "Search" @16,163 398x36 [tap, type]',
        'e5 cell "Airplane Mode" id=airplane-mode-cell @16,215 398x52 [tap]',
        'e6 text "Airplane Mode" @60,229 200x24',
        'e7 switch "Airplane Mode" checked=false @349,224 51x31 [toggle]',
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
        'e1 input "Search" value="wifi" @16,163 398x36 [tap, type]',
        'e2 text "Reset All Settings?" @94,376 242x22',
        'e3 button "Cancel" @78,490 137x50 [tap]',
        'e4 button "Reset" @215,490 137x50 [tap]',
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
                                             'e2 button "clear" id=AC @14,420 90x90 [tap]',
                                             'e3 button "7" @14,520 90x90 [tap]']
    assert [e.line() for e in items][-1] == 'e7 button "7" id=7-memory @14,620 90x90 [tap]'


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
    assert on.line() == 'e1 switch "Bluetooth" checked=true @10,10 100x40 [toggle]'  # no value="1" as well
    assert one("Button", "Wi-Fi").checked is None
    picked = one("Tab", "Bluetooth", traits="Button,Selected", enabled="false")
    assert picked.selected and not picked.enabled
    assert picked.line() == 'e1 tab "Bluetooth" disabled selected @10,10 100x40 [tap]'
    assert not one("Tab", "Bluetooth", traits="Button").selected


def test_enabled_is_false_for_enabled_false_or_the_not_enabled_trait():
    assert not one("Button", "A", enabled="false").enabled
    assert not one("Button", "B", traits="Button,NotEnabled").enabled
    assert one("Button", "C", traits="Button").enabled


def test_the_line_clips_flattens_and_quotes_text():
    long = "Line one\n   line   two " + "x" * 200
    e = one("StaticText", long)
    quoted = json.loads(e.line().split(" ", 2)[2].rsplit(" @", 1)[0])
    assert len(quoted) == 80 and quoted.startswith("Line one line two xxxx")
    odd = one("StaticText", 'Say "hi"', value='Say "hi" now')
    assert odd.line() == 'e1 text "Say \\"hi\\"" value="Say \\"hi\\" now" @10,10 100x40'
    assert one("TextField", "Email", value="a@b.example").line() == \
        'e1 input "Email" value="a@b.example" @10,10 100x40 [tap, type]'
    only_value = one("StaticText", value="Home")
    assert only_value.text == "Home" and only_value.line() == 'e1 text "Home" @10,10 100x40'


def test_line_without_a_ref_or_without_actions():
    e = ui.read(load("calculator"), SCREEN).items[1]
    assert e.line(ref=False) == 'button "clear" id=AC @14,420 90x90 [tap]'
    assert e.line(actions=False) == 'e2 button "clear" id=AC @14,420 90x90'
    assert e.line(ref=False, actions=False) == 'button "clear" id=AC @14,420 90x90'
    assert e.rect == (14.0, 420.0, 90.0, 90.0) and e.center == (59.0, 465.0)


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
    assert e.line() == 'e1 button "Login" @10,10 100x40 [tap]'
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
    assert e.line() == 'e1 button "Sign in" id=sign-in @10,10 200x40 [tap]'  # the ancestor stays
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
    assert e.line() == 'e1 input "Email" value="me@example.com" @10,10 200x40 [tap, type]'
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
    assert (e.role, e.actions, e.line()) == ("button", ("tap",), 'e1 button "7" id=Seven @14,520 90x90 [tap]')
    board = node("Keyboard", rect=(0, 600, 430, 300), children=[node("Key", "q", rect=(10, 620, 40, 50))])
    assert [e.type for e in ui.elements(xml(board), SCREEN)] == []

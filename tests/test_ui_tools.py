"""The tree-reading helpers the tools use: the navigation bar's back button."""
from tapwright import ui

SCREEN = (400.0, 800.0)


def node(kind, x, y, w, h, label="", children=(), name=None):
    return (f'<XCUIElementType{kind} type="XCUIElementType{kind}" label="{label}" name="{label if name is None else name}" '
            f'enabled="true" x="{x}" y="{y}" width="{w}" height="{h}">{"".join(children)}</XCUIElementType{kind}>')


def screen(*children):
    return f"<AppiumAUT>{node('Application', 0, 0, 400, 800, children=children)}</AppiumAUT>"


def bar(*buttons):
    return node("NavigationBar", 0, 59, 400, 44, children=buttons)


def test_the_back_button_is_the_one_named_back_even_when_others_sit_left_of_it():
    top = bar(node("Button", 8, 59, 60, 44, "Settings", name="BackButton"), node("StaticText", 150, 59, 100, 44, "General"),
              node("Button", 340, 59, 50, 44, "Edit"))
    assert ui.back_button(screen(top), SCREEN) == ui.Back(True, (38.0, 81.0))
    assert ui.back_button(screen(bar(node("Button", 8, 59, 60, 44, "Cancel"), node("Button", 150, 59, 60, 44, "Back"))),
                          SCREEN).point == (180.0, 81.0)  # by its label, wherever it is


def test_the_leftmost_button_in_the_left_quarter_is_the_back_button():
    top = bar(node("Button", 8, 59, 80, 44, "Settings"), node("Button", 340, 59, 50, 44, "Edit"))
    assert ui.back_button(screen(top), SCREEN) == ui.Back(True, (48.0, 81.0))
    nested = bar(node("Other", 0, 59, 200, 44, children=[node("Button", 8, 59, 80, 44, "Wi-Fi")]))
    assert ui.back_button(screen(nested), SCREEN).point == (48.0, 81.0)


def test_a_cancel_or_menu_on_the_left_is_not_a_back_button():
    for label in ("Cancel", "Menu", "Done", "Save", "Edit", "Delete", "Clear", "Discard", "Close", "Sign Out", "cancel"):
        assert ui.back_button(screen(bar(node("Button", 8, 59, 80, 44, label))), SCREEN) == ui.Back(True, None), label


def test_no_button_in_the_left_quarter_means_no_tap_but_a_bar():
    assert ui.back_button(screen(bar(node("Button", 340, 59, 50, 44, "Edit"))), SCREEN) == ui.Back(True, None)


def test_no_navigation_bar_at_all():
    assert ui.back_button(screen(node("Button", 8, 59, 80, 44, "Settings")), SCREEN) == ui.Back(False, None)


def test_the_top_bar_wins_and_bars_off_screen_are_ignored():
    sheet = node("NavigationBar", 0, 300, 400, 44, children=[node("Button", 8, 300, 80, 44, "Sheet")])
    top = bar(node("Button", 8, 59, 80, 44, "Back"))
    hidden = node("NavigationBar", 0, -200, 400, 44, children=[node("Button", 8, -200, 80, 44, "Ghost")])
    assert ui.back_button(screen(sheet, top, hidden), SCREEN).point == (48.0, 81.0)
    assert ui.back_button(screen(hidden), SCREEN) == ui.Back(False, None)

"""The tree-reading helpers the tools use: the navigation bar's back button."""
from tapwright import ui

SCREEN = (400.0, 800.0)


def node(kind, x, y, w, h, label="", children=()):
    return (f'<XCUIElementType{kind} type="XCUIElementType{kind}" label="{label}" name="{label}" enabled="true" '
            f'x="{x}" y="{y}" width="{w}" height="{h}">{"".join(children)}</XCUIElementType{kind}>')


def screen(*children):
    return f"<AppiumAUT>{node('Application', 0, 0, 400, 800, children=children)}</AppiumAUT>"


def test_the_back_button_is_the_first_button_left_of_the_bars_centre():
    bar = node("NavigationBar", 0, 59, 400, 44, children=[
        node("Button", 8, 59, 80, 44, "Settings"), node("StaticText", 150, 59, 100, 44, "General"),
        node("Button", 340, 59, 50, 44, "Edit")])
    assert ui.back_button(screen(bar), SCREEN) == (48.0, 81.0)


def test_no_back_button():
    right_only = node("NavigationBar", 0, 59, 400, 44, children=[node("Button", 340, 59, 50, 44, "Edit")])
    assert ui.back_button(screen(right_only), SCREEN) is None
    assert ui.back_button(screen(node("Button", 8, 59, 80, 44, "Settings")), SCREEN) is None  # not in a bar


def test_the_top_bar_wins_and_bars_off_screen_are_ignored():
    sheet = node("NavigationBar", 0, 300, 400, 44, children=[node("Button", 8, 300, 80, 44, "Cancel")])
    top = node("NavigationBar", 0, 59, 400, 44, children=[node("Button", 8, 59, 80, 44, "Back")])
    hidden = node("NavigationBar", 0, -200, 400, 44, children=[node("Button", 8, -200, 80, 44, "Ghost")])
    assert ui.back_button(screen(sheet, top, hidden), SCREEN) == (48.0, 81.0)


def test_a_button_nested_in_the_bar_counts():
    bar = node("NavigationBar", 0, 59, 400, 44, children=[
        node("Other", 0, 59, 200, 44, children=[node("Button", 8, 59, 80, 44, "Back")])])
    assert ui.back_button(screen(bar), SCREEN) == (48.0, 81.0)

"""The tree-reading helpers the tools use: the navigation bar's back button, and scroll areas."""
import pytest
from fakes import el

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


def test_scroll_area_points_are_a_share_of_the_area():
    assert ui.scroll_area_points("down", (0, 100, 400, 600), 0.5) == (200.0, 550.0, 200.0, 250.0)
    assert ui.scroll_area_points("up", (0, 100, 400, 600), 0.5) == (200.0, 250.0, 200.0, 550.0)
    assert ui.scroll_area_points("left", (0, 100, 400, 600), 0.5) == (100.0, 400.0, 300.0, 400.0)
    assert ui.scroll_area_points("right", (0, 100, 400, 600), 0.5) == (300.0, 400.0, 100.0, 400.0)


def test_scroll_never_touches_the_edge_of_its_area():
    x0, y0, x1, y1 = ui.scroll_area_points("down", (0, 100, 400, 600), 1.0)
    assert (y0, y1) == (652.0, 148.0) and 100 < y1 < y0 < 700  # amount 1.0 is held 8% inside each edge
    with pytest.raises(ValueError):
        ui.scroll_area_points("sideways", (0, 0, 10, 10))


def test_the_scroll_area_is_the_first_scroll_element_over_the_screen_centre():
    corner = el("e1", "scroll", "Corner", rect=(0, 0, 100, 100))
    middle = el("e2", "scroll", "Middle", rect=(0, 100, 400, 600))
    button = el("e3", "button", "OK", rect=(100, 300, 200, 200))
    assert ui.scroll_container([corner, button, middle], SCREEN) is middle
    assert ui.scroll_container([corner, button], SCREEN) is None


def test_the_tree_says_how_far_the_bars_reach_into_the_screen():
    bars = screen(node("NavigationBar", 0, 59, 400, 44), node("TabBar", 0, 749, 400, 51))
    tree = ui.read(bars, SCREEN)
    assert (tree.top_inset, tree.bottom_inset) == (103.0, 51.0)
    tree = ui.read(screen(node("StatusBar", 0, 0, 400, 50), node("Toolbar", 0, 740, 400, 60)), SCREEN)
    assert (tree.top_inset, tree.bottom_inset) == (50.0, 60.0)
    assert ui.read(screen(node("Button", 8, 300, 80, 44, "OK")), SCREEN).top_inset is None  # no bars: unknown


def test_bars_behind_an_alert_set_no_insets():
    alert = node("Alert", 60, 300, 280, 180, "Allow?", children=[node("Button", 60, 420, 140, 60, "Don't Allow")])
    tree = ui.read(screen(node("NavigationBar", 0, 59, 400, 44), node("TabBar", 0, 749, 400, 51), alert), SCREEN)
    assert tree.alert is not None and (tree.top_inset, tree.bottom_inset) == (None, None)


def test_swipes_stay_between_the_bars():
    for direction in ("up", "down"):
        _, y0, _, y1 = ui.scroll_area_points(direction, (0, 0, 400, 800), 1.0, band=(100.0, 720.0))
        assert 100 < y0 < 720 and 100 < y1 < 720, direction
    _, y, _, _ = ui.scroll_area_points("left", (0, 0, 400, 800), 1.0, band=(100.0, 720.0))
    assert y == 410.0  # a sideways swipe runs through the middle of what is between the bars


def test_the_scroll_area_is_the_innermost_one_around_the_centre_or_a_point():
    outer = el("e1", "scroll", "Page", rect=(0, 0, 400, 800))
    inner = el("e2", "scroll", "Feed", rect=(0, 300, 400, 200))
    assert ui.scroll_container([outer, inner], SCREEN) is inner
    assert ui.scroll_container([inner, outer], SCREEN) is inner
    assert ui.scroll_at([inner, outer], (50.0, 350.0)) is inner
    assert ui.scroll_at([inner, outer], (50.0, 50.0)) is outer
    assert ui.scroll_at([inner, outer], (50.0, 900.0)) is None


def test_a_secure_text_field_is_marked_secure():
    items = ui.elements(screen(node("SecureTextField", 10, 100, 300, 44, "Password"),
                               node("TextField", 10, 160, 300, 44, "Email")), SCREEN)
    assert [(e.role, e.secure) for e in items] == [("input", True), ("input", False)]


def test_the_scroll_view_of_an_element_is_itself_or_the_smallest_one_around_it():
    page = el("e1", "scroll", "Page", rect=(0, 100, 400, 600))
    row = el("e2", "scroll", "Row", rect=(0, 200, 400, 100))
    inside = el("e3", "button", "OK", rect=(10, 210, 100, 40))
    outside = el("e4", "button", "Back", rect=(10, 20, 100, 40))
    items = [page, row, inside, outside]
    assert ui.scroll_of(items, page) is page
    assert ui.scroll_of(items, inside) is row
    assert ui.scroll_of(items, outside) is None

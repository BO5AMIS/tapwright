"""The tree-reading helpers the tools use: the navigation bar's back button, and scroll areas."""
import pytest
from fakes import el

from tapwright import ui
from tapwright.control import Refused

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


# ---- bars drawn over content, and layers that hide what is behind them (Task 8a) ----
PHONE = (430.0, 932.0)


def phone(*children):
    return f"<AppiumAUT>{node('Application', 0, 0, 430, 932, children=children)}</AppiumAUT>"


def base_screen():
    """A screen with a card whose centre is under the tab bar, and a tab bar with two tabs."""
    tabs = node("TabBar", 0, 849, 430, 83, children=[node("Button", 0, 849, 215, 83, "Home"),
                                                     node("Button", 215, 849, 215, 83, "Stats")])
    return node("Other", 0, 0, 430, 932, children=[node("Button", 20, 100, 100, 44, "Add"),
                                                   node("Cell", 20, 808, 390, 90, "Card"), tabs])


def labels(items):
    return [e.label for e in items if e.label]  # the tab bar itself is listed, without a label


def test_an_element_under_a_bar_that_is_not_its_ancestor_knows_the_bar():
    items = ui.elements(phone(base_screen()), PHONE)
    by = {e.label: e for e in items}
    assert [b.kind for b in by["Card"].covered_by] == ["TabBar"]
    assert by["Card"].covered_by[0].rect == (0.0, 849.0, 430.0, 83.0)
    assert by["Card"].covered_by[0].line == "tabbar @0,849 430x83"
    assert by["Add"].covered_by == () and by["Home"].covered_by == () and by["Stats"].covered_by == ()  # a bar's own tabs


def test_a_toolbar_and_a_navigation_bar_cover_too_and_a_status_bar_does_not():
    top = node("NavigationBar", 0, 0, 430, 100, "Title")
    tool = node("Toolbar", 0, 800, 430, 132)
    status = node("StatusBar", 0, 400, 430, 40)
    items = ui.elements(phone(node("Cell", 20, 50, 390, 80, "Row"), node("Cell", 20, 820, 390, 80, "Low"),
                              node("Cell", 20, 380, 390, 80, "Mid"), top, tool, status), PHONE)
    by = {e.label: e for e in items}
    assert [b.kind for b in by["Row"].covered_by] == ["NavigationBar"]
    assert [b.kind for b in by["Low"].covered_by] == ["Toolbar"]
    assert by["Mid"].covered_by == ()


def sheet_over_base(layer_children):
    return phone(base_screen(), node("Other", 0, 0, 430, 932, children=layer_children))


SHEET = [node("Button", 0, 0, 430, 510, "Dismiss"), node("Button", 20, 560, 390, 50, "Sheet action"),
         node("Button", 20, 850, 390, 50, "Sheet close")]  # the last one sits where the tab bar is


def test_a_full_screen_layer_hides_what_is_behind_it():
    items = ui.elements(sheet_over_base(SHEET), PHONE)
    assert labels(items) == ["Dismiss", "Sheet action", "Sheet close"]  # a sheet dims the tab bar behind it
    assert [e.ref for e in items] == ["e1", "e2", "e3"]  # numbered after the hiding
    assert all(e.covered_by == () for e in items)  # the tab bar behind the sheet covers nothing in front of it


def test_a_small_later_layer_hides_nothing():
    toast = node("Other", 0, 700, 430, 80, children=[node("Button", 300, 710, 100, 60, "Undo")])
    assert labels(ui.elements(phone(base_screen(), toast), PHONE)) == ["Add", "Card", "Home", "Stats", "Undo"]
    big_but_sparse = node("Other", 0, 0, 430, 932, children=[node("Button", 20, 500, 100, 44, "Floating")])
    assert "Add" in labels(ui.elements(phone(base_screen(), big_but_sparse), PHONE))  # 95% of the screen, under 50% filled


def test_a_layer_before_the_content_hides_nothing():
    items = ui.elements(phone(node("Other", 0, 0, 430, 932, children=SHEET), base_screen()), PHONE)
    assert {"Add", "Card", "Dismiss"} <= set(labels(items))  # the base screen is in front here


def test_a_full_screen_scroll_drawn_after_a_bar_leaves_the_bar_listed():
    rows = [node("Cell", 0, 100 + 60 * i, 430, 60, f"Row {i}") for i in range(12)]
    items = ui.elements(phone(node("NavigationBar", 0, 59, 430, 44, children=[node("Button", 8, 59, 60, 44, "Back")]),
                              node("ScrollView", 0, 0, 430, 932, children=rows)), PHONE)
    assert "Back" in labels(items)  # bars are never hidden by a layer; the bar logic handles touches near them


def test_a_tab_bar_before_a_full_screen_scroll_stays_listed():
    tabs = node("TabBar", 0, 849, 430, 83, children=[node("Button", 0, 849, 215, 83, "Home")])
    rows = [node("Cell", 0, 60 * i, 430, 60, f"Row {i}") for i in range(15)]
    items = ui.elements(phone(tabs, node("ScrollView", 0, 0, 430, 932, children=rows)), PHONE)
    assert "Home" in labels(items) and "tabbar" in [e.type.lower() for e in items]


def test_the_visible_part_of_an_element_is_its_rect_minus_the_bars():
    tab = ui.Bar("TabBar", (0.0, 849.0, 430.0, 83.0), "tabbar @0,849 430x83")
    assert ui.visible_part((20, 808, 390, 90), (tab,), PHONE) == (20, 808, 390, 41)
    assert ui.visible_part((20, 845, 390, 60), (tab,), PHONE)[3] == 4  # a sliver: the caller refuses it
    top = ui.Bar("NavigationBar", (0.0, 0.0, 430.0, 100.0), "navigationbar @0,0 430x100")
    assert ui.visible_part((20, 60, 390, 200), (top, tab), PHONE) == (20, 100, 390, 160)
    assert ui.visible_part((20, 900, 390, 100), (), PHONE) == (20, 900, 390, 32)  # the screen's edge counts too


# ---- review fixes (Task 8a, follow-up) ----
def nested(depth, *children):
    """`children` inside `depth` plain Other wrappers (each a level below the one before)."""
    for _ in range(depth):
        children = (node("Other", 0, 0, 430, 932, children=children),)
    return "".join(children)


def cards(top=0, count=4, height=233):
    return [node("Cell", 0, top + i * height, 430, height, f"Card {i}") for i in range(count)]


def test_deep_app_content_hides_nothing_even_when_a_later_body_fills_the_screen():
    header = node("Other", 0, 0, 430, 120, children=[node("Button", 20, 50, 100, 44, "Menu")])
    body = node("Other", 0, 0, 430, 932, children=cards())
    items = ui.elements(phone(nested(10, header, body)), PHONE)  # React Native: the body sits 11 levels below the application
    assert labels(items) == ["Menu", "Card 0", "Card 1", "Card 2", "Card 3"]


def test_a_full_screen_page_with_a_scroll_view_hides_the_screen_under_it():
    rows = [node("Cell", 0, 60 * i, 430, 60, f"Row {i}") for i in range(15)]
    page = node("Other", 0, 0, 430, 932, children=[node("ScrollView", 0, 0, 430, 900, children=rows)])
    items = ui.elements(phone(base_screen(), page), PHONE)
    assert labels(items) == ["Home", "Stats"] + [f"Row {i}" for i in range(15)]  # opaque content hides the screen, not its bars


def test_a_sheet_three_levels_down_hides_the_base_screen():
    items = ui.elements(phone(nested(2, base_screen(), node("Other", 0, 0, 430, 932, children=SHEET))), PHONE)
    assert labels(items) == ["Dismiss", "Sheet action", "Sheet close"]


def test_a_layer_hides_only_what_is_inside_its_rect():
    footer = node("Button", 0, 900, 430, 32, "Footer")  # centre y 916: below a 900 pt tall layer
    layer = node("Other", 0, 0, 430, 900, children=[node("Button", 0, 0, 430, 450, "Dismiss"),
                                                    node("Button", 0, 450, 430, 450, "Sheet body")])
    items = ui.elements(phone(nested(1, node("Button", 20, 100, 100, 44, "Add"), footer, layer)), PHONE)
    assert labels(items) == ["Footer", "Dismiss", "Sheet body"]


def test_the_layer_depth_and_thresholds_are_named_constants():
    assert (ui.LAYER_MAX_DEPTH, ui.LAYER_COVER, ui.LAYER_FILL) == (6, 0.95, 0.5)


def test_every_bar_over_an_element_is_listed_and_a_bar_beside_it_is_not():
    nav = node("NavigationBar", 0, 0, 430, 100)
    tool = node("Toolbar", 0, 300, 430, 40)
    tab = node("TabBar", 0, 849, 430, 83, children=[node("Button", 0, 849, 215, 83, "Home")])
    tall = node("Cell", 20, 80, 390, 830, "Tall")
    short = node("Cell", 20, 150, 390, 100, "Short")
    items = ui.elements(phone(nested(1, tall, short, nav, tool, tab)), PHONE)
    by = {e.label: e for e in items}
    # its centre is in none of the bars; the later cell over it counts as a cover too (Task 8c)
    assert [b.kind for b in by["Tall"].covered_by] == ["NavigationBar", "Toolbar", "TabBar", "Cell"]
    assert by["Short"].covered_by == () and by["Home"].covered_by == ()


def test_a_shown_keyboard_covers_like_a_bottom_bar():
    keys = node("Keyboard", 0, 600, 430, 332, children=[node("Key", 10, 610, 40, 50, "q")])
    items = ui.elements(phone(node("Cell", 20, 560, 390, 100, "Row"), keys), PHONE)
    assert [b.kind for b in items[0].covered_by] == ["Keyboard"] and items[0].covered_by[0].line.startswith("keyboard @0,600")
    assert ui.elements(phone(node("Cell", 20, 560, 390, 100, "Row")), PHONE)[0].covered_by == ()


def test_the_visible_part_is_cut_by_every_bar_and_keeps_the_strip_nearest_the_centre():
    nav = ui.Bar("NavigationBar", (0.0, 0.0, 430.0, 100.0), "navigationbar @0,0 430x100")
    tool = ui.Bar("Toolbar", (0.0, 430.0, 430.0, 100.0), "toolbar @0,430 430x100")
    tab = ui.Bar("TabBar", (0.0, 849.0, 430.0, 83.0), "tabbar @0,849 430x83")
    # y 80-910, centre 495 (inside the toolbar): the strip 530-849 is nearer than the larger 100-430
    assert ui.visible_part((20, 80, 390, 830), (nav, tool, tab), PHONE) == (20, 530, 390, 319)


# ---- a layer needs a backdrop (Task 8a, follow-up 2) ----
def test_a_header_and_a_full_height_body_twelve_levels_down_hide_nothing():
    header = node("Other", 0, 0, 430, 120, children=[node("Button", 20, 50, 100, 44, "Menu")])
    body = node("Other", 0, 0, 430, 932, children=cards())
    assert labels(ui.elements(phone(nested(12, header, body)), PHONE)) == ["Menu", "Card 0", "Card 1", "Card 2", "Card 3"]


def test_a_dimming_view_then_a_dialog_hides_the_base_screen():
    dim = node("Other", 0, 0, 430, 932)  # childless, unlisted
    dialog = node("Other", 30, 150, 370, 650, children=[node("Cell", 30, 150, 370, 650, "Dialog body")])
    items = ui.elements(phone(nested(1, base_screen(), node("Other", 0, 0, 430, 932, children=[dim, dialog]))), PHONE)
    assert labels(items) == ["Dialog body"]


def test_a_first_child_with_listed_children_is_content_not_a_backdrop():
    top = node("Other", 0, 0, 430, 500, children=[node("Cell", 0, 0, 430, 500, "Top")])
    layer = node("Other", 0, 0, 430, 932, children=[top, node("Cell", 0, 500, 430, 432, "Bottom")])
    assert {"Add", "Card", "Top", "Bottom"} <= set(labels(ui.elements(phone(nested(10, base_screen(), layer)), PHONE)))


def test_a_backdrop_after_the_layers_content_does_not_count():
    layer = node("Other", 0, 0, 430, 932, children=[node("Cell", 0, 500, 430, 432, "Content"),
                                                    node("Button", 0, 0, 430, 510, "Late backdrop")])
    assert "Add" in labels(ui.elements(phone(nested(10, base_screen(), layer)), PHONE))


def test_the_backdrop_thresholds_are_named_constants():
    assert (ui.BACKDROP_WIDTH, ui.BACKDROP_HEIGHT) == (0.95, 0.4)


def test_a_translucent_large_title_bar_over_a_scrolling_list_leaves_the_part_below_it_to_touch():
    rows = [node("Cell", 0, 40 + 100 * i, 430, 100, f"Row {i}") for i in range(8)]
    items = ui.elements(phone(node("ScrollView", 0, 0, 430, 932, children=rows),
                              node("NavigationBar", 0, 0, 430, 100, "Title")), PHONE)
    row = next(e for e in items if e.label == "Row 0")  # y 40-140: its centre, 90, is under the bar
    assert [b.kind for b in row.covered_by] == ["NavigationBar"]
    assert ui.visible_part(row.rect, row.covered_by, PHONE) == (0, 100, 430, 40)


# ---- layers: the rule with a presentation path and an opaque-content path (Task 8c) ----
def under_window(*screens, depth=1):
    """`screens` as children of a node `depth` levels below the Window, inside Application > Window."""
    inner = "".join(screens)
    for _ in range(depth - 1):
        inner = node("Other", 0, 0, 430, 932, children=[inner])
    return phone(node("Window", 0, 0, 430, 932, children=[inner]))


def test_a_stale_screen_under_the_roots_later_child_is_hidden():
    stale = node("Other", 0, 0, 430, 932, children=[
        node("StaticText", 20, 79, 348, 63, "Something went wrong"), node("StaticText", 20, 154, 360, 52, "Try again later"),
        node("ScrollView", 0, 205, 430, 533, "Old list")])
    body = node("Other", 0, 0, 430, 932, children=[
        node("NavigationBar", 0, 59, 430, 54, "Orders", name="orders-bar"),
        node("Button", 330, 66, 90, 40, "Filter"), node("StaticText", 20, 130, 300, 30, "Recent orders"),
        node("ScrollView", 0, 0, 430, 932, "Orders list", children=[node("Cell", 0, 170, 430, 100, "Order 1")])])
    real = node("Other", 0, 0, 430, 932, children=[body])  # the bar and the scroll sit deeper than a layer can
    # Application 0, Window 1, then 2 and 3: the container sits at depth 4 and its children at depth 5
    items = ui.elements(under_window(node("Other", 0, 0, 430, 932, children=[stale, real]), depth=3), PHONE)
    shown = labels(items)
    assert "Order 1" in shown and "Filter" in shown
    assert not {"Something went wrong", "Try again later", "Old list"} & set(shown)
    assert [e.label for e in items if e.role == "scroll"] == ["Orders list"]


def bottom_sheet(base):
    backdrop = node("Other", 0, 0, 430, 932)  # childless, unnamed: the dimming view
    shell = node("Other", 0, 0, 430, 932, children=[node("Other", 0, 727, 430, 205)])
    sheet = node("Other", 0, 727, 430, 205, "", name="lab-sheet")
    inner = node("Other", 0, 0, 430, 932, children=[
        backdrop, shell, sheet, node("StaticText", 20, 747, 390, 26, "Sheet title"),
        node("Button", 20, 784, 390, 49, "Confirm"), node("Button", 20, 844, 390, 49, "Dismiss sheet")])
    layer = node("Other", 0, 0, 430, 932, children=[node("Other", 0, 0, 430, 932, children=[inner])])
    return under_window(base, layer)


def lab_base():
    return node("Other", 0, 0, 430, 932, children=[node("Button", 20, 100, 390, 49, "Base action", name="lab-base"),
                                                    node("Cell", 20, 400, 390, 80, "Base row"),
                                                    node("Button", 20, 800, 390, 49, "Base footer")])


def test_a_bottom_sheet_with_a_backdrop_hides_the_base_screen_it_covers():
    items = ui.elements(bottom_sheet(lab_base()), PHONE)
    assert [e.label for e in items] == ["", "Sheet title", "Confirm", "Dismiss sheet"]
    assert items[0].identifier == "lab-sheet"
    for target in (ui.Target(label="Base action"), ui.Target(id="lab-base"), ui.Target(text="Base row")):
        with pytest.raises(Refused) as e:
            ui.resolve(items, target)
        assert e.value.code == "not_found"


def test_the_keyboard_window_is_no_layer_and_hides_nothing():
    """On the phone the keyboard's own full-screen window starts with an empty full-screen view and has a key
    to act on ("Next keyboard"): it looked like a sheet with a backdrop and hid the whole app (2026-11-09)."""
    # as the phone lists it: an empty full-screen view, the strip below the keys with "Next keyboard", the keys
    strip = node("Other", 0, 832, 430, 100, children=[node("Button", 2, 864, 81, 69, "Next keyboard")])
    keys = node("Other", 0, 589, 430, 243, children=[
        node("Keyboard", 0, 589, 430, 243, children=[node("Key", 4, 596, 43, 56, "q")])])
    window = node("Other", 0, 0, 430, 932, children=[node("Other", 0, 0, 430, 932), strip, keys])
    shown = labels(ui.elements(under_window(lab_base(), window), PHONE))
    assert {"Base action", "Base row", "Next keyboard"} <= set(shown)


def test_the_keyboard_covers_its_whole_view_not_just_its_keys():
    """On the phone the keys start at y 631, but the keyboard's view (its suggestions strip, the globe and dictation
    keys below) runs from 614 to the bottom: a switch at 612-640 was touched at 626, on the keyboard (2026-11-09)."""
    base = node("Other", 0, 0, 430, 932, children=[node("Switch", 351, 612, 63, 28, "Switch", name="lab-switch")])
    keys = node("Other", 0, 631, 430, 243, children=[
        node("Keyboard", 0, 631, 430, 243, children=[node("Key", 4, 640, 43, 56, "q")])])
    strip = node("Other", 0, 631, 430, 301, children=[node("Button", 2, 864, 81, 69, "Next keyboard")])
    view = node("Other", 0, 614, 430, 318, children=[node("Other", 0, 614, 430, 318), keys, strip])
    window = node("Other", 0, 0, 430, 932, children=[node("Other", 0, 0, 430, 932), view])
    switch = next(e for e in ui.elements(under_window(base, window), PHONE) if e.identifier == "lab-switch")
    assert [(b.kind, b.rect) for b in switch.covered_by] == [("Keyboard", (0.0, 614.0, 430.0, 318.0))]
    assert ui.visible_part(switch.rect, switch.covered_by, PHONE) == (351.0, 612.0, 63.0, 2.0)


def test_a_layer_with_a_backdrop_but_nothing_to_act_on_hides_nothing():
    inner = node("Other", 0, 0, 430, 932, children=[node("Other", 0, 0, 430, 932), node("StaticText", 20, 747, 390, 26, "Loading")])
    items = ui.elements(under_window(lab_base(), node("Other", 0, 0, 430, 932, children=[inner])), PHONE)
    assert {"Base action", "Base row", "Loading"} <= set(labels(items))


def test_react_native_header_and_full_height_body_deep_in_the_tree_hide_nothing():
    header = node("Other", 0, 0, 430, 120, children=[node("Button", 20, 50, 100, 44, "Menu")])
    body = node("Other", 0, 0, 430, 932, children=cards())
    assert labels(ui.elements(under_window(header, body, depth=10), PHONE)) == ["Menu", "Card 0", "Card 1", "Card 2", "Card 3"]


def test_a_uikit_navigation_controller_hides_nothing():
    transition = node("Other", 0, 0, 430, 932, children=[node("StaticText", 20, 130, 300, 30, "Title"),
                                                         node("Cell", 0, 170, 430, 100, "Row")])
    nav = node("NavigationBar", 0, 59, 430, 54, "Settings", children=[node("Button", 8, 62, 80, 44, "Back")])
    assert {"Title", "Row", "Back"} <= set(labels(ui.elements(under_window(transition, nav, depth=3), PHONE)))


def test_a_toast_layer_hides_nothing():
    toast = node("Other", 0, 0, 430, 932, children=[node("Other", 20, 800, 390, 60, children=[node("StaticText", 30, 810, 300, 30, "Saved")])])
    assert {"Add", "Card", "Saved"} <= set(labels(ui.elements(under_window(base_screen(), toast), PHONE)))


# ---- later elements cover like bars (Task 8c) ----
def test_a_later_element_over_a_button_is_listed_as_its_cover():
    button = node("Button", 20, 100, 390, 60, "Covered", name="lab-covered")
    overlay = node("Other", 10, 90, 410, 80, "", name="lab-overlay")
    by = {e.identifier or e.label: e for e in ui.elements(phone(button, overlay), PHONE)}
    cover = by["lab-covered"].covered_by
    assert [(b.kind, b.rect, b.line) for b in cover] == [("Other", (10.0, 90.0, 410.0, 80.0), "other id=lab-overlay @10,90 410x80")]
    assert by["lab-overlay"].covered_by == ()  # nothing is later than it


def test_an_ancestor_a_descendant_an_earlier_element_and_a_big_container_do_not_cover():
    row = node("Cell", 0, 200, 430, 100, "Row", name="row-id", children=[node("Button", 300, 210, 100, 40, "Inner")])
    earlier = node("Other", 20, 400, 100, 40, "", name="earlier")
    target = node("Button", 20, 400, 100, 40, "Target")
    big = node("Other", 0, 0, 430, 932, "", name="big-container")  # over 90% of the screen: a layer, not a cover
    by = {e.identifier or e.label: e for e in ui.elements(phone(row, earlier, target, big), PHONE)}
    assert by["row-id"].covered_by == () and by["Inner"].covered_by == ()
    assert by["Target"].covered_by == ()  # `earlier` is before it; `big` is a layer


def test_a_badge_in_a_corner_is_a_cover_that_misses_the_centre():
    button = node("Button", 20, 100, 390, 60, "Covered")
    badge = node("Other", 390, 100, 20, 20, "", name="badge")
    (cover,) = {e.label: e for e in ui.elements(phone(button, badge), PHONE)}["Covered"].covered_by
    assert cover.rect == (390.0, 100.0, 20.0, 20.0)


# ---- the keyboard, and the raw swipe (Task 8c) ----
def test_the_header_shows_the_keyboard_only_when_it_meets_the_screen():
    parked = node("Keyboard", 0, 932, 430, 243, children=[node("Key", 10, 940, 40, 50, "q")])  # a hardware keyboard
    assert ui.read(phone(parked), PHONE).keyboard is False
    up = node("Keyboard", 0, 700, 430, 232)
    assert ui.read(phone(up), PHONE).keyboard is True
    rising = node("Keyboard", 0, 900, 430, 243)  # its centre is still below the screen
    assert ui.read(phone(rising), PHONE).keyboard is True


def test_a_swipe_can_hold_before_the_finger_lifts():
    held = ui.swipe(5, 400, 5, 100, 600, hold_ms=150)["actions"][0]["actions"]
    assert [s["type"] for s in held[-3:]] == ["pointerMove", "pause", "pointerUp"] and held[-2]["duration"] == 150
    plain = ui.swipe(5, 400, 5, 100, 600)["actions"][0]["actions"]
    assert [s["type"] for s in plain[-2:]] == ["pointerMove", "pointerUp"]


def test_a_later_transparent_wrapper_with_a_small_button_hides_nothing():
    wrapper = node("Other", 0, 0, 430, 932, "Screen", name="wrapper", children=[node("Button", 20, 500, 100, 44, "Floating")])
    layer = node("Other", 0, 0, 430, 932, children=[wrapper])
    assert {"Add", "Card", "Floating"} <= set(labels(ui.elements(phone(base_screen(), layer), PHONE)))


def test_the_keyboard_is_never_hidden_by_a_layer():
    keys = node("Keyboard", 0, 700, 430, 232, children=[node("Key", 10, 710, 40, 50, "q")])
    layer = node("Other", 0, 0, 430, 932, children=[node("Cell", 0, 0, 430, 932, "Page")])
    tree = ui.read(phone(keys, layer), PHONE, keyboard=True)
    assert tree.keyboard and "q" in labels(tree.items)


def test_a_later_element_up_to_95_percent_of_the_screen_covers():
    button = node("Button", 20, 100, 390, 60, "Covered")
    overlay = node("Other", 0, 0, 430, 858, "", name="lab-overlay")  # 92% of the screen
    (cover,) = {e.label: e for e in ui.elements(phone(button, overlay), PHONE)}["Covered"].covered_by
    assert cover.rect == (0.0, 0.0, 430.0, 858.0)


def test_a_presented_sheet_hides_the_tab_bar_behind_it_and_a_tap_on_a_tab_is_not_found():
    items = ui.elements(sheet_over_base(SHEET), PHONE)
    assert "Home" not in labels(items) and "tabbar" not in [e.type.lower() for e in items]
    with pytest.raises(Refused) as e:
        ui.resolve(items, ui.Target(label="Home"))
    assert e.value.code == "not_found"


def test_the_keyboard_stays_in_front_of_a_presented_sheet():
    keys = node("Keyboard", 0, 700, 430, 232, children=[node("Key", 10, 710, 40, 50, "q")])
    layer = node("Other", 0, 0, 430, 932, children=SHEET)
    tree = ui.read(phone(keys, layer), PHONE, keyboard=True)
    assert "q" in labels(tree.items)

"""A list scrolled differently is the same place (1.8.0, W168). On the phone (W166, Wayfare): after a destination's detail,
Back came to the Explore list scrolled a few rows, the explorer read it as another place (`place_28`, not `place_12`),
called it a dead end and relaunched, and runs ended `cant_get_back`. The trees here are built the way the phone showed
the demo app's screens (a React Native FlatList is a ScrollView with the list's testID; each card is a button with its
own testID and its texts inside; the tab bar is three buttons). No phone."""
from xml.etree import ElementTree as ET

from tapwright import mapfile, places, ui

SCREEN = (393.0, 852.0)
CITIES = ("banff", "kyoto", "lisbon", "marrakech", "santorini", "reykjavik", "cusco", "hanoi", "cape-town", "queenstown",
          "petra", "havana", "bergen", "tbilisi")
CARD_H = 260.0
LIST_TOP = 230.0


def node(kind: str, label: str = "", name: str = "", rect=(0, 0, 10, 10), children=(), **extra: str) -> ET.Element:
    attrs = {"type": f"XCUIElementType{kind}", "enabled": "true",
             **dict(zip(("x", "y", "width", "height"), map(str, rect))), **extra}
    if label:
        attrs["label"] = label
    if name:
        attrs["name"] = name
    n = ET.Element(attrs["type"], attrs)
    n.extend(children)
    return n


def tab_bar(selected: str) -> ET.Element:
    return node("Other", rect=(0, 769, 393, 83), children=[
        node("Button", t.title(), name=f"tab-{t}", rect=(10 + 128 * i, 775, 120, 60),
             **({"traits": "Button,Selected"} if t == selected else {"traits": "Button"}))
        for i, t in enumerate(("explore", "trips", "settings"))])


def app(*children: ET.Element) -> ui.Tree:
    root = node("Application", "Wayfare", rect=(0, 0, 393, 852), bundleId="com.example.wayfare", children=[
        node("Window", rect=(0, 0, 393, 852), children=[node("Other", rect=(0, 0, 393, 852), children=list(children))])])
    return ui.read(ET.tostring(root, encoding="unicode"), SCREEN)


def card(city: str, y: float) -> ET.Element:
    return node("Button", f"{city.title()}, 4.8, $1,200", name=f"dest-card-{city}", rect=(20, y, 353, CARD_H - 20), children=[
        node("StaticText", city.title(), name=f"dest-name-{city}", rect=(36, y + 170, 200, 24)),
        node("Other", "4.8", name=f"dest-rating-{city}", rect=(300, y + 170, 50, 20)),
        node("StaticText", "$1,200", name=f"dest-price-{city}", rect=(36, y + 200, 120, 20))])


def explore(rows_scrolled: int = 0, list_id: str = "explore-list") -> ui.Tree:
    """The Explore tab with its list scrolled by `rows_scrolled` cards (each card a row): the header above the list and
    the tab bar don't move; only the cards on screen are in the tree (and those just off its edge)."""
    offset = rows_scrolled * CARD_H
    cards = [card(c, LIST_TOP + i * CARD_H - offset) for i, c in enumerate(CITIES)]
    return app(
        node("StaticText", "Hello, Sam", name="explore-greeting", rect=(20, 60, 200, 20)),
        node("StaticText", "Where to next?", name="explore-title", rect=(20, 84, 300, 36)),
        node("SearchField", "Search destinations", name="explore-search", rect=(20, 134, 353, 50)),
        node("ScrollView", name=list_id, rect=(0, LIST_TOP - 20, 393, 769 - LIST_TOP + 20), children=cards),
        tab_bar("explore"))


def detail(city: str = "kyoto") -> ui.Tree:
    return app(node("ScrollView", name="detail-scroll", rect=(0, 0, 393, 852), children=[
        node("Button", "Back", name="detail-back", rect=(16, 60, 44, 44)),
        node("StaticText", city.title(), name="detail-name", rect=(20, 320, 300, 36)),
        node("StaticText", "Japan", name="detail-region", rect=(20, 360, 300, 20)),
        node("StaticText", "$1,200", name="detail-price", rect=(20, 400, 100, 20)),
        node("StaticText", "4.8", name="detail-rating", rect=(200, 400, 100, 20)),
        node("StaticText", "Temples and gardens.", name="detail-description", rect=(20, 440, 353, 120)),
        node("Button", "Plan a trip", name="detail-plan-trip", rect=(20, 700, 353, 54))]))


def trips() -> ui.Tree:
    return app(node("StaticText", "My trips", name="trips-title", rect=(20, 70, 300, 36)),
               node("ScrollView", name="trips-list", rect=(0, 120, 393, 649), children=[
                   node("StaticText", "No trips yet", name="trips-empty", rect=(20, 300, 353, 20)),
                   node("Button", "Explore destinations", name="trips-explore", rect=(20, 340, 353, 50))]),
               tab_bar("trips"))


def settings() -> ui.Tree:
    return app(node("ScrollView", name="settings-scroll", rect=(0, 0, 393, 769), children=[
        node("StaticText", "Settings", name="settings-title", rect=(20, 70, 300, 36)),
        node("Switch", "Prices in local currency", name="settings-local-currency", rect=(20, 300, 353, 44)),
        node("Switch", "Show ratings", name="settings-show-ratings", rect=(20, 350, 353, 44)),
        node("Button", "Sign out", name="settings-signout", rect=(20, 600, 353, 54))]), tab_bar("settings"))


def lab_rows(rows_scrolled: int) -> ui.Tree:
    """A list without an identifier of its own: only its rows' families can say which list it is."""
    offset = rows_scrolled * 60.0
    rows = [node("Button", f"Row {n}", name=f"lab-row-{n}", rect=(0, 200 + 60 * n - offset, 393, 56)) for n in range(40)]
    return app(node("Other", "Lab", name="lab-bar", rect=(0, 0, 393, 120)),
               node("ScrollView", rect=(0, 120, 393, 732), children=rows))


def new_map() -> dict:
    return places.empty_map("com.example.wayfare")


def at(data: dict, tree: ui.Tree) -> places.Visit:
    return places.visit(data, places.read_place(tree), "2026-11-19T10:00:00Z")


# ---- the list scrolled by 0, 3 and 10 rows is one place ----

def test_the_list_scrolled_by_0_3_and_10_rows_has_one_signature():
    keys = {places.read_place(explore(n)).key["signature"] for n in (0, 3, 10)}
    assert len(keys) == 1
    # the rows on screen really are different ones
    assert set(explore(0).structure.ids) != set(explore(10).structure.ids)


def test_the_list_scrolled_by_0_3_and_10_rows_is_one_place_in_the_map():
    data = new_map()
    first = at(data, explore(0))
    assert at(data, detail()).name != first.name
    for n in (3, 10, 1):
        back = at(data, explore(n))
        assert back.name == first.name and not back.new, n
    assert len(data["places"]) == 2


def test_a_list_without_an_identifier_goes_by_its_rows_families():
    data = new_map()
    first = at(data, lab_rows(0))
    assert at(data, lab_rows(3)).name == first.name
    assert at(data, lab_rows(10)).name == first.name
    assert places.read_place(lab_rows(0)).key["signature"] == places.read_place(lab_rows(10)).key["signature"]


def test_no_row_content_reaches_the_key():
    key = places.read_place(explore(3)).key
    dumped = str(key)
    for city in CITIES:
        assert city not in dumped
    assert key["parts"]["lists"] == ["explore-list"]


# ---- genuinely different screens still differ ----

def test_the_list_and_its_detail_are_two_places():
    data = new_map()
    lst = at(data, explore(0)).name
    det = at(data, detail("kyoto")).name
    assert lst != det
    assert not places.same_place(places.read_place(explore(3)).key["parts"], places.read_place(detail()).key["parts"])
    assert at(data, detail("banff")).name == det  # another destination's detail is the same screen
    assert at(data, explore(5)).name == lst


def test_the_tabs_are_three_places():
    data = new_map()
    names = [at(data, t).name for t in (explore(0), trips(), settings())]
    assert len(set(names)) == 3
    assert [at(data, t).name for t in (settings(), trips(), explore(4))] == names[::-1]
    sig = {places.read_place(t).key["signature"] for t in (explore(0), trips(), settings())}
    assert len(sig) == 3


def test_two_screens_whose_lists_differ_are_two_places_though_their_tab_bar_ids_agree():
    a = places.read_place(trips()).key["parts"]
    b = places.read_place(settings()).key["parts"]
    assert a["lists"] != b["lists"]
    assert not places.same_place(a, b)
    other_list = places.read_place(explore(0, list_id="explore-results")).key["parts"]
    assert not places.same_place(places.read_place(explore(0)).key["parts"], other_list)


# ---- a map saved before 1.8.0 ----

def _old_parts(tree: ui.Tree) -> dict:
    """The parts as 4ddf06f made them: every identifier on screen, rows included, and no `lists`."""
    return {"ids": list(tree.structure.ids), "kinds": list(tree.structure.kinds), "tab": None, "layer": None}


def _old_map() -> dict:
    data = new_map()
    for name, tree in (("place_12", explore(0)), ("place_3", detail()), ("place_1", trips())):
        parts = _old_parts(tree)
        data["places"][name] = {"key": {"signature": places.signature(parts), "parts": parts}, "link": None,
                                "titles": {}, "last": "2026-11-18T21:00:00Z"}
    return data


def test_an_old_map_still_finds_its_places_from_a_scrolled_list():
    data = _old_map()
    assert places.locate(data, places.read_place(explore(0)))[0] == "place_12"
    assert places.locate(data, places.read_place(explore(3)))[0] == "place_12"
    assert places.locate(data, places.read_place(detail()))[0] == "place_3"
    assert places.locate(data, places.read_place(trips()))[0] == "place_1"
    assert places.locate(data, places.read_place(settings()))[0] is None
    v = at(data, explore(10))
    assert v.name == "place_12" and not v.new
    # the old place keeps its key (no rewrite: a new signature would merge with the disk as another place)
    assert "lists" not in data["places"]["place_12"]["key"]["parts"]


def test_an_old_map_with_new_places_saves_and_loads_without_loss(tmp_path):
    file = mapfile.MapFile(tmp_path, "com.example.wayfare")
    data = file.load()
    data.update({k: v for k, v in _old_map().items() if k in ("places", "titleKey")})
    file.save(data)
    loaded = file.load()
    at(loaded, explore(3))
    at(loaded, settings())
    saved = file.save(loaded)
    assert sorted(saved["places"]) == ["place_1", "place_12", "place_13", "place_3"]
    assert sorted(file.load()["places"]) == ["place_1", "place_12", "place_13", "place_3"]


def _wrapped(family: str, fields: int, scrolled: int = 0) -> ui.Tree:
    """A screen an app wraps in one shared scroll view (`screen-scroll`), its own controls inside it."""
    offset = scrolled * 60.0
    return app(node("ScrollView", name="screen-scroll", rect=(0, 0, 393, 769), children=[
        node("Button", f"Item {n}", name=f"{family}-item-{n}", rect=(0, 80 + 60 * n - offset, 393, 56)) for n in range(fields)]),
        tab_bar("settings"))


def test_two_screens_in_one_shared_scroll_view_stay_two_places_through_a_save(tmp_path):
    data = new_map()
    a = at(data, _wrapped("profile", 30)).name
    b = at(data, _wrapped("billing", 30)).name
    assert a != b
    assert at(data, _wrapped("profile", 30, scrolled=12)).name == a
    # the two keys share a signature (what is inside a list with an id is left out of it), and still stay two on disk
    assert data["places"][a]["key"]["signature"] == data["places"][b]["key"]["signature"]
    file = mapfile.MapFile(tmp_path, "com.example.wayfare")
    first = file.load()
    first["titleKey"] = data["titleKey"]
    at(first, _wrapped("profile", 30))
    file.save(first)  # the disk holds the profile screen
    second = places.empty_map("com.example.wayfare")
    second["titleKey"] = data["titleKey"]
    at(second, _wrapped("billing", 30))  # another session saw only the billing screen
    assert len(file.save(second)["places"]) == 2

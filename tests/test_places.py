"""Places: the signature of a screen, the names, the title hashes (AGENT-TOOLS §18.1 and §18.9). No phone."""
import json
import re
from xml.etree import ElementTree as ET

from tapwright import places, ui

SCREEN = (430.0, 932.0)


def node(kind: str, label: str = "", name: str = "", value: str = "", rect=(10, 10, 100, 40), children=(),
         **extra: str) -> ET.Element:
    attrs = {"type": f"XCUIElementType{kind}", "enabled": "true",
             **dict(zip(("x", "y", "width", "height"), map(str, rect))), **extra}
    for key, v in (("label", label), ("name", name), ("value", value)):
        if v:
            attrs[key] = v
    n = ET.Element(attrs["type"], attrs)
    n.extend(children)
    return n


def screen(title: str = "Settings", rows: tuple[str, ...] = ("Wi-Fi", "Bluetooth"), ids: tuple[str, ...] = (),
           tab: str | None = None, tabs: tuple[str, ...] = ("home", "settings"), alert: str | None = None,
           row_ids: bool = True, keyboard: bool = False, kind: str = "Table", body: str = "body") -> ui.Tree:
    """A screen: a navigation bar with a title, a list of rows (each a Cell with a name and a label), buttons with
    identifiers, a tab bar with one tab selected, and perhaps an alert or the keyboard."""
    bar = node("NavigationBar", name=title, rect=(0, 59, 430, 96),
               children=[node("StaticText", title, title, title, rect=(16, 103, 200, 40), traits="StaticText,Header")])
    cells = [node("Cell", row, name=f"row-{row}" if row_ids else row, rect=(16, 215 + 52 * i, 398, 52))
             for i, row in enumerate(rows)]
    buttons = [node("Button", f"Label of {i}", name=i, rect=(16, 700 + 50 * n, 100, 40)) for n, i in enumerate(ids)]
    body = node(kind, name=body, rect=(0, 155, 430, 600), children=cells)
    children = [bar, body, *buttons]
    if tab is not None:
        children.append(node("TabBar", rect=(0, 840, 430, 92), children=[
            node("Button", t, name=f"tab-{t}", rect=(20 + 100 * n, 845, 90, 80), **({"traits": "Selected"} if t == tab else {}))
            for n, t in enumerate(tabs)]))
    if alert:
        children.append(node("Alert", alert, rect=(60, 300, 310, 200), children=[
            node("Button", "OK", rect=(60, 450, 310, 50))]))
    if keyboard:
        children.append(node("Keyboard", rect=(0, 600, 430, 332)))
    app = node("Application", "App", rect=(0, 0, 430, 932), bundleId="com.example.app", children=[
        node("Window", rect=(0, 0, 430, 932), children=[node("Other", rect=(0, 0, 430, 932), children=children)])])
    return ui.read(ET.tostring(app, encoding="unicode"), SCREEN)


def test_a_screen_says_what_it_is_made_of():
    tree = screen(ids=("save", "cancel"), tab="settings")
    assert tree.structure.kinds == ("nav", "list", "tabs")
    assert tree.structure.ids == ("body", "cancel", "save", "tab-home", "tab-settings")
    assert tree.structure.title == "Settings"


def test_list_rows_take_no_part_in_the_signature():
    a = places.read_place(screen(rows=("Wi-Fi", "Bluetooth")))
    b = places.read_place(screen(rows=("Chat with Sara", "Order #10432", "Another")))
    assert a.key == b.key  # same ids, same kinds: the rows' names and texts are content
    assert a.key["signature"] == b.key["signature"]
    assert not any("row-" in i for i in a.key["parts"]["ids"])


def test_the_signature_is_the_same_in_another_language():
    en = places.read_place(screen(title="Settings", rows=("Wi-Fi",), ids=("save",)))
    ar = places.read_place(screen(title="الإعدادات", rows=("واي فاي",), ids=("save",)))
    assert en.key == ar.key
    assert en.title == "Settings" and ar.title == "الإعدادات"


def test_signature_changes_with_ids_kinds_tab_and_what_lies_on_top():
    base = places.read_place(screen(ids=("save",), tab="home"))
    assert places.read_place(screen(ids=("save", "delete"), tab="home")).key != base.key
    assert places.read_place(screen(ids=("save",), tab="settings")).key != base.key
    assert places.read_place(screen(ids=("save",), tab="home", kind="CollectionView")).key == base.key  # a list is a list
    assert places.read_place(screen(ids=("save",), tab="home", alert="Delete?")).key != base.key
    assert places.read_place(screen(ids=("save",))).key != base.key  # no tab bar at all


def test_the_keyboard_is_not_counted():
    assert places.read_place(screen(ids=("save",), keyboard=True)).key == places.read_place(screen(ids=("save",))).key


def test_the_selected_tab_is_its_identifier_else_its_position():
    assert places.read_place(screen(tab="settings")).key["parts"]["tab"] == "tab-settings"
    # labels only (WebDriverAgent mirrors the label into the name): a label is text a language changes
    plain = ui.Tree([ui.Element("e1", "tab", "Een"), ui.Element("e2", "tab", "Twee", selected=True)])
    assert places.read_place(plain).key["parts"]["tab"] == "#2"


def test_a_sheet_on_top_is_a_layer():
    covered = ui.Tree([], behind=[(ui.Element("e1", "button", "Hidden"), None)])
    assert places.read_place(covered).key["parts"]["layer"] == "sheet"
    assert places.read_place(screen(alert="Delete?")).key["parts"]["layer"] == "alert"
    assert places.read_place(screen()).key["parts"]["layer"] is None


def test_two_reads_are_compared_part_by_part():
    a = places.read_place(screen(ids=("a", "b", "c", "d", "e"))).key["parts"]
    near = places.read_place(screen(ids=("a", "b", "c", "d", "e", "banner"))).key["parts"]  # one id came
    other = places.read_place(screen(ids=("x", "y", "z"))).key["parts"]
    assert places.similarity(a, a) == 1.0
    assert places.same_place(a, near)
    assert not places.same_place(a, other)
    assert places.similarity(a, near) > places.similarity(a, other)
    assert 0 < places.THRESHOLD <= 1


def test_a_sheet_over_a_screen_is_never_the_same_place_as_the_screen():
    plain = places.read_place(screen(ids=("a", "b"))).key["parts"]
    over = {**plain, "layer": "sheet"}
    assert places.similarity(plain, over) == 0.0 and not places.same_place(plain, over)


def test_title_hash_is_an_hmac_per_language_and_never_the_title():
    key = "ab" * 16
    h = places.title_hash(key, "en", "Chat with Sara")
    assert re.fullmatch(r"[0-9a-f]{16}", h) and "sara" not in h.lower()
    assert h == places.title_hash(key, "en", "  Chat   with Sara ")  # whitespace is not a difference
    assert h != places.title_hash(key, "ar", "Chat with Sara")
    assert h != places.title_hash("cd" * 16, "en", "Chat with Sara")  # a map's own key


def test_the_language_of_a_title():
    assert places.language_of("Settings") == "latin"
    assert places.language_of("الإعدادات") == "ar"
    assert places.language_of("Settings", asked="fr-CA") == "fr"
    assert places.language_of("") == "und"


def test_slug_is_ascii_lower_case_with_underscores():
    assert places.slugify("Language Settings") == "language_settings"
    assert places.slugify("Café & Bar!") == "cafe_bar"
    assert places.slugify("الإعدادات") is None  # no ASCII letters
    assert places.slugify("12345") is None
    assert places.slugify("") is None


def new_map():
    return places.empty_map("com.example.app")


def test_a_new_map_has_its_own_random_title_key():
    a, b = new_map(), new_map()
    assert a["format"] == "agent-tools-map" and a["version"] == 1 and a["app"] == "com.example.app"
    assert re.fullmatch(r"[0-9a-f]{32,}", a["titleKey"]) and a["titleKey"] != b["titleKey"]
    assert a["places"] == {} and a["roads"] == [] and a["routes"] == {} and a["reflexes"] == []


def seen(data, tree, now="2026-11-12T10:00:00Z", arrived=True, lang=None):
    return places.visit(data, places.read_place(tree, lang), now, arrived)


def test_a_new_place_is_numbered_until_its_title_shows_twice():
    data = new_map()
    first = seen(data, screen(title="Language", ids=("a",)))
    assert first.new and first.name == "place_1" and first.renamed_from is None
    again = seen(data, screen(title="Language", ids=("a",)))
    assert not again.new and again.name == "language" and again.renamed_from == "place_1"
    assert list(data["places"]) == ["language"]
    assert seen(data, screen(title="Language", ids=("a",))).name == "language"  # and it stays


def test_a_look_at_the_same_screen_is_not_a_second_visit():
    data = new_map()
    seen(data, screen(title="Language", ids=("a",)))
    again = seen(data, screen(title="Language", ids=("a",)), arrived=False)  # a scroll, a second look
    assert again.name == "place_1" and again.renamed_from is None


def test_a_title_that_changes_is_content_not_a_name():
    data = new_map()
    seen(data, screen(title="Chat with Sara", ids=("send",)))
    second = seen(data, screen(title="Chat with Omar", ids=("send",)))
    assert second.name == "place_1" and not second.new
    third = seen(data, screen(title="Chat with Omar", ids=("send",)))
    assert third.name == "place_1"  # it changed once: never a name
    assert "omar" not in json.dumps(data).lower() and "sara" not in json.dumps(data).lower()


def test_a_title_with_no_ascii_letters_never_names_a_place():
    data = new_map()
    for _ in range(3):
        v = seen(data, screen(title="الإعدادات", ids=("a",)))
    assert v.name == "place_1"


def test_two_places_with_one_title_get_a_suffix():
    data = new_map()
    for _ in range(2):
        seen(data, screen(title="Details", ids=("a",)))
    for _ in range(2):
        seen(data, screen(title="Details", ids=("b", "c")))
    assert sorted(data["places"]) == ["details", "details_2"]


def test_numbering_continues_after_a_place_is_forgotten():
    data = new_map()
    seen(data, screen(ids=("a",)))
    seen(data, screen(ids=("b",)))
    assert sorted(data["places"]) == ["place_1", "place_2"]
    assert places.forget(data, "place_1")
    assert seen(data, screen(ids=("c",))).name == "place_3"
    assert not places.forget(data, "nothing")


def test_the_same_place_in_another_language_is_found_and_keeps_its_name():
    data = new_map()
    places.rename(data, seen(data, screen(title="Settings", ids=("a",))).name, "settings")
    v = seen(data, screen(title="الإعدادات", ids=("a",)))
    assert v.name == "settings" and not v.new
    assert set(data["places"]["settings"]["titles"]) == {"latin", "ar"}


def test_a_given_name_is_kept_and_roads_follow_it():
    data = new_map()
    a = seen(data, screen(ids=("a",))).name
    b = seen(data, screen(ids=("b",))).name
    data["roads"].append({"from": a, "to": b, "move": {"tool": "tap", "target": {"id": "go"}}})
    data["reflexes"].append({"name": "r", "at": a})
    result = places.set_name(data, a, "Home Screen")
    assert result.name == "home_screen" and result.taken is False
    assert data["roads"][0]["from"] == "home_screen" and data["reflexes"][0]["at"] == "home_screen"
    for _ in range(2):  # a person's name is never replaced by a title
        seen(data, screen(title="Other Title", ids=("a",)))
    assert "home_screen" in data["places"]
    assert places.set_name(data, b, "home_screen").name == "home_screen_2"


def test_a_name_that_is_not_ascii_is_refused():
    data = new_map()
    name = seen(data, screen(ids=("a",))).name
    for bad in ("", "   ", "اسم", "a/b", "x" * 61):
        try:
            places.set_name(data, name, bad)
        except ValueError as e:
            assert "name" in str(e)
        else:
            raise AssertionError(f"{bad!r} was taken")


def test_titles_are_kept_only_as_hashes_and_no_private_text_gets_into_a_map():
    data = new_map()
    private = ("Chat with Sara", "Order #10432", "sara@example.com", "4111 1111 1111 1111", "hunter2")
    for _ in range(2):
        seen(data, screen(title="Chat with Sara", rows=private, ids=("send",)))
    dumped = json.dumps(data, ensure_ascii=False)
    for text in (*private, "Sara", "10432"):
        assert text not in dumped
    [place] = data["places"].values()
    assert re.fullmatch(r"[0-9a-f]{16}", place["titles"]["latin"])


def test_a_secure_field_and_typed_text_are_not_in_a_key():
    typed = node("TextField", "Email", name="email", value="sara@example.com", rect=(16, 300, 398, 44))
    secret = node("SecureTextField", "Password", name="password", value="hunter2", rect=(16, 360, 398, 44))
    app = node("Application", "App", rect=(0, 0, 430, 932), bundleId="com.example.app", children=[
        node("Window", rect=(0, 0, 430, 932), children=[node("Other", rect=(0, 0, 430, 932), children=[
            node("ScrollView", name="signin", rect=(0, 100, 430, 800), children=[typed, secret])])])])
    read = places.read_place(ui.read(ET.tostring(app, encoding="unicode"), SCREEN))
    dumped = json.dumps(read.key)
    assert "sara@example.com" not in dumped and "hunter2" not in dumped
    assert read.key["parts"]["kinds"] == ["form"]
    assert read.key["parts"]["ids"] == ["email", "password", "signin"]


def test_a_screen_with_no_ids_needs_its_title_to_be_the_same_place():
    data = new_map()
    seen(data, screen(title="One", body=""))
    other = seen(data, screen(title="Two", body=""))  # the same kinds, no ids: only the title tells them apart
    assert other.new and len(data["places"]) == 2
    back = seen(data, screen(title="One", body=""))
    assert not back.new


def test_places_told_apart_by_title_stay_two_through_save_merge_and_load(tmp_path):
    from tapwright import mapfile
    file = mapfile.MapFile(tmp_path, "com.example.app")
    data = file.load()
    one = seen(data, screen(title="One", body="")).name
    file.save(data)  # observe and flush
    two = seen(data, screen(title="Two", body="")).name
    data["roads"].append({"from": one, "to": two, "move": {"tap": "go"}, "last": "2026-11-12T10:00:01Z"})
    file.save(data)  # observe and flush again: merges with what the first save wrote
    loaded = file.load()
    assert sorted(loaded["places"]) == sorted([one, two]) and one != two
    [road] = loaded["roads"]
    assert (road["from"], road["to"]) == (one, two)
    # and a fresh memory of the same two screens merges onto them without folding
    again = places.empty_map("com.example.app")
    again["titleKey"] = loaded["titleKey"]
    seen(again, screen(title="Two", body=""))
    seen(again, screen(title="One", body=""))
    assert len(file.save(again)["places"]) == 2


def test_a_tie_between_two_places_is_broken_by_the_title():
    data = new_map()
    a = seen(data, screen(title="Alpha", ids=("a", "b", "c", "d", "e", "f"))).name
    b = seen(data, screen(title="Beta", ids=("a", "b", "c", "x", "y", "z"))).name
    assert a != b
    # as near to one as to the other (5 of 8 ids each): the title says which
    near = places.read_place(screen(title="Beta", ids=("a", "b", "c", "d", "x")))
    assert places.locate(data, near)[0] == b
    near = places.read_place(screen(title="Alpha", ids=("a", "b", "c", "d", "x")))
    assert places.locate(data, near)[0] == a


def test_places_without_a_signature_are_left_alone():
    data = new_map()
    data["places"]["language"] = {"key": {"route": ["settings", "language"]}, "link": None, "titles": {}, "last": "x"}
    v = seen(data, screen(ids=("a",)))
    assert v.new and v.name == "place_1" and "language" in data["places"]

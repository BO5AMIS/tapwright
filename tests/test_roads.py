"""Roads: one move that left a place and arrived at another (AGENT-TOOLS §18.2), and the way `drive` would take. No phone."""
import json

from tapwright import places, roads, ui

NOW = "2026-11-12T18:20:00Z"
LATER = "2026-11-12T18:30:00Z"
BUILD = "1.4.0 (212)"


def el(ref="e1", role="button", label="", identifier="", value="", **kw) -> ui.Element:
    return ui.Element(ref, role, label, identifier, value, (0, 0, 100, 40), **kw)


def move_of(tool, element, items=None, **args):
    args = {**args}
    target = args.pop("target", None)
    return roads.move_of(tool, ui.Target(**target) if target else None, element, items or ([element] if element else []),
                         args)


def test_a_tap_is_saved_by_id_else_label_else_text_never_by_ref():
    button = el(label="Language", identifier="language-row")
    assert move_of("tap", button, target={"ref": "e1"}) == {"tool": "tap", "target": {"id": "language-row"}}
    plain = el(label="Language")
    assert move_of("tap", plain, target={"ref": "e1"}) == {"tool": "tap", "target": {"label": "Language"}}
    valued = el(value="Language")
    assert move_of("tap", valued, target={"ref": "e1"}) == {"tool": "tap", "target": {"text": "Language"}}
    assert "ref" not in json.dumps(move_of("long_press", button, target={"ref": "e1"}))
    assert move_of("long_press", button, target={"ref": "e1"})["tool"] == "long_press"
    assert move_of("toggle", button, target={"id": "x"})["tool"] == "toggle"


def test_an_element_that_several_share_is_saved_with_its_index():
    first, second = el("e1", label="Edit"), el("e2", label="Edit")
    items = [first, second]
    assert move_of("tap", second, items, target={"ref": "e2"}) == {"tool": "tap", "target": {"label": "Edit", "index": 1}}
    assert move_of("tap", first, items, target={"ref": "e1"}) == {"tool": "tap", "target": {"label": "Edit", "index": 0}}


def test_a_point_is_saved_only_where_there_is_no_element():
    assert move_of("tap", None, target={"x": 120.0, "y": 300.0}) == {"tool": "tap", "target": {"x": 120.0, "y": 300.0}}
    button = el(label="Map pin")
    assert move_of("tap", button, target={"x": 20.0, "y": 20.0}) == {"tool": "tap", "target": {"label": "Map pin"}}


def test_a_list_row_is_saved_by_its_id_and_a_row_without_one_as_a_row():
    with_id = el(role="cell", label="Wi-Fi, Home", identifier="Wi-Fi")
    assert move_of("tap", with_id, target={"ref": "e1"}) == {"tool": "tap", "target": {"id": "Wi-Fi"}}
    bare = el(role="cell", label="Chat with Sara", value="3 new")
    assert move_of("tap", bare, target={"label": "Chat with Sara"}) == {"tool": "tap", "target": {"row": True}}
    assert roads.names_of(bare) == {} and roads.names_of(with_id) == {"id": "Wi-Fi"}  # a row's text is never kept


def test_names_keep_every_name_an_element_had():
    button = el(label="Language", identifier="language-row", value="English")
    assert roads.names_of(button) == {"id": "language-row", "label": "Language", "text": "Language"}
    assert roads.names_of(el(value="Hello")) == {"text": "Hello"}


def test_back_and_swipe_and_open_url_and_what_is_never_saved():
    assert roads.move_of("back", None, None, [], {}) == {"tool": "back"}
    assert roads.move_of("swipe", None, None, [], {"x0": 10.0, "y0": 500.0, "x1": 10.0, "y1": 100.0, "seconds": 0.35}) == \
        {"tool": "swipe", "x0": 10.0, "y0": 500.0, "x1": 10.0, "y1": 100.0, "seconds": 0.35}
    assert roads.move_of("open_url", None, None, [], {"link": "wayfare://settings"}) == {"tool": "open_url", "link": "wayfare://settings"}
    assert roads.move_of("open_url", None, None, [], {"url": "wayfare://settings?token=SECRET"}) is None  # no safe link given
    for tool in ("type_text", "scroll", "wait", "look", "alert", "open_app", "run", "replay", "press_button"):
        assert roads.move_of(tool, None, None, [], {"text": "hello"}) is None


def road(src="settings", dst="language", target="language-row", **kw):
    return {"from": src, "to": dst, "move": {"tool": "tap", "target": {"id": target}}, **kw}


def test_a_road_as_it_was_seen():
    data = places.empty_map("com.example.app")
    made = roads.record(data, "settings", "language", {"tool": "tap", "target": {"id": "language-row"}},
                        {"id": "language-row", "label": "Language", "text": "Language"}, 840, NOW, BUILD)
    assert made.new and not made.dropped
    assert data["roads"] == [{"from": "settings", "to": "language", "move": {"tool": "tap", "target": {"id": "language-row"}},
                              "names": {"id": "language-row", "label": "Language", "text": "Language"},
                              "by": "tapwright", "seen": 1, "failed": 0, "ms": 840, "last": NOW, "build": BUILD}]


def test_the_same_road_again_is_counted_not_added():
    data = places.empty_map("com.example.app")
    move = {"tool": "tap", "target": {"id": "language-row"}}
    roads.record(data, "settings", "language", move, {"id": "language-row"}, 840, NOW, BUILD)
    data["roads"][0]["failed"] = 1
    made = roads.record(data, "settings", "language", move, {"id": "language-row", "label": "Language"}, 700, LATER, BUILD)
    assert not made.new and len(data["roads"]) == 1
    r = data["roads"][0]
    assert (r["seen"], r["failed"], r["ms"], r["last"]) == (2, 0, 700, LATER)
    assert r["names"] == {"id": "language-row", "label": "Language"}


def test_a_move_that_stays_on_its_place_is_no_road():
    data = places.empty_map("com.example.app")
    made = roads.record(data, "settings", "settings", {"tool": "tap", "target": {"id": "x"}}, {}, 100, NOW, BUILD)
    assert made is None and data["roads"] == []


def test_a_road_that_arrives_somewhere_else_counts_against_the_old_one_and_twice_drops_it():
    data = places.empty_map("com.example.app")
    move = {"tool": "tap", "target": {"id": "go"}}
    roads.record(data, "a", "b", move, {"id": "go"}, 100, NOW, BUILD)
    first = roads.record(data, "a", "c", move, {"id": "go"}, 100, NOW, BUILD)  # the same move led elsewhere
    assert first.new and not first.dropped
    assert [(r["to"], r["failed"]) for r in data["roads"]] == [("b", 1), ("c", 0)]
    second = roads.record(data, "a", "c", move, {"id": "go"}, 100, LATER, BUILD)
    assert second.dropped == [("a", "b")]
    assert [r["to"] for r in data["roads"]] == ["c"]


def test_a_failure_twice_in_a_row_drops_a_road_and_a_success_in_between_does_not():
    data = places.empty_map("com.example.app")
    move = {"tool": "tap", "target": {"id": "go"}}
    roads.record(data, "a", "b", move, {"id": "go"}, 100, NOW, BUILD)
    assert roads.fail(data, "a", move) == []
    roads.record(data, "a", "b", move, {"id": "go"}, 100, LATER, BUILD)  # worked again
    assert roads.fail(data, "a", move) == []
    assert roads.fail(data, "a", move) == [("a", "b")] and data["roads"] == []


def test_a_road_that_another_tool_drove_becomes_both_when_this_one_drives_it():
    data = places.empty_map("com.example.app")
    move = {"tool": "tap", "target": {"id": "go"}}
    data["roads"].append({"from": "a", "to": "b", "move": move, "by": "tapline", "seen": 3, "failed": 0, "ms": 90,
                          "last": NOW, "build": BUILD})
    roads.record(data, "a", "b", move, {"id": "go"}, 100, LATER, BUILD)
    assert data["roads"][0]["by"] == "both" and data["roads"][0]["seen"] == 4


def test_a_road_of_another_build_is_unproven_until_driven_cleanly_once():
    data = places.empty_map("com.example.app")
    move = {"tool": "tap", "target": {"id": "go"}}
    roads.record(data, "a", "b", move, {"id": "go"}, 100, NOW, BUILD)
    r = data["roads"][0]
    assert roads.is_proven(r, BUILD) and not roads.is_proven(r, "1.5.0 (220)")
    roads.record(data, "a", "b", move, {"id": "go"}, 100, LATER, "1.5.0 (220)")
    assert roads.is_proven(data["roads"][0], "1.5.0 (220)")


def test_with_no_build_known_every_road_counts_as_proven():
    r = {"from": "a", "to": "b", "move": {"tool": "back"}, "build": None}
    assert roads.is_proven(r, None)
    assert roads.is_proven({**r, "build": "1.0 (1)"}, None)  # the app's build can't be read now: nothing to be unproven against


def graph(*edges):
    data = places.empty_map("com.example.app")
    for src, dst, target, *rest in edges:
        data["roads"].append({"from": src, "to": dst, "move": {"tool": "tap", "target": {"id": target}}, "by": "tapwright",
                              "seen": 1, "failed": 0, "ms": 100, "last": rest[0] if rest else NOW, "build": BUILD})
    return data


def test_the_way_is_the_fewest_moves():
    data = graph(("home", "settings", "s"), ("settings", "general", "g"), ("general", "language", "l"),
                 ("home", "profile", "p"), ("profile", "language", "pl"))
    way = roads.plan(data, "home", "language", BUILD)
    assert way.kind == "roads" and [r["to"] for r in way.roads] == ["profile", "language"] and way.proven
    assert way.cost == 2


def test_an_unproven_road_counts_double():
    data = graph(("home", "settings", "s"), ("settings", "language", "l"), ("home", "profile", "p"),
                 ("profile", "x", "px"), ("x", "y", "xy"), ("y", "language", "yl"))
    data["roads"][0]["build"] = "1.3.0 (100)"  # home -> settings is unproven: 2 + 1 = 3, against 4 proven
    way = roads.plan(data, "home", "language", BUILD)
    assert way.cost == 3 and not way.proven and way.unproven == 1
    data["roads"][1]["build"] = "1.3.0 (100)"  # now 2 + 2 = 4 against 4: the one used most recently...
    data["roads"][2]["last"] = LATER
    assert [r["to"] for r in roads.plan(data, "home", "language", BUILD).roads] == ["profile", "x", "y", "language"]
    data["roads"][5]["build"] = "1.3.0 (100)"  # ...and with the long way unproven too: 2 + 1 + 1 + 2 = 6 against 4
    assert [r["to"] for r in roads.plan(data, "home", "language", BUILD).roads] == ["settings", "language"]


def test_two_ways_of_equal_cost_take_the_one_used_most_recently():
    data = graph(("home", "a", "ha", NOW), ("a", "end", "ae", NOW), ("home", "b", "hb", LATER), ("b", "end", "be", LATER))
    assert [r["to"] for r in roads.plan(data, "home", "end", BUILD).roads] == ["b", "end"]


def test_a_tool_replays_only_its_own_roads_unless_marked_both():
    data = graph(("home", "settings", "s"))
    data["roads"][0]["by"] = "tapline"
    assert roads.plan(data, "home", "settings", BUILD).kind == "none"
    data["roads"][0]["by"] = "both"
    assert roads.plan(data, "home", "settings", BUILD).kind == "roads"


def test_a_place_with_a_link_is_reached_by_link_unless_taps_are_asked_for():
    data = graph(("home", "settings", "s"))
    data["places"]["settings"] = {"key": {}, "link": "wayfare://settings", "titles": {}, "last": NOW}
    way = roads.plan(data, "home", "settings", BUILD)
    assert way.kind == "link" and way.link == "wayfare://settings" and way.cost == 1
    assert roads.plan(data, "home", "settings", BUILD, by="taps").kind == "roads"
    assert roads.plan(data, None, "settings", BUILD).kind == "link"  # a jump needs no start
    assert roads.plan(data, None, "settings", BUILD, by="taps").kind == "none"  # roads from nowhere don't exist
    data["places"]["home"] = {"key": {}, "link": None, "titles": {}, "last": NOW}
    assert roads.plan(data, "home", "home", BUILD).kind == "here"
    assert roads.plan(data, "home", "settings", BUILD, by="link").kind == "link"
    assert roads.plan(data, "home", "profile", BUILD, by="link").kind == "none"


def test_a_saved_route_is_one_move_per_step_between_its_start_and_end():
    data = places.empty_map("com.example.app")
    route = roads.RouteEdge("sign_in", "welcome", "home", 5)
    way = roads.plan(data, "welcome", "home", BUILD, routes=[route])
    assert way.kind == "roads" and way.cost == 5 and way.routes == [route]
    one = graph(("welcome", "login", "w"), ("login", "home", "l"))
    assert [r["to"] for r in roads.plan(one, "welcome", "home", BUILD, routes=[route]).roads] == ["login", "home"]


def test_no_way():
    data = graph(("home", "settings", "s"))
    assert roads.plan(data, "settings", "home", BUILD).kind == "none"
    assert roads.plan(data, None, "home", BUILD).kind == "none"


def test_the_way_is_told_in_names_and_how_sure_it_is():
    data = graph(("home", "settings", "s"), ("settings", "language", "l"))
    way = roads.plan(data, "home", "language", BUILD)
    assert roads.say_way(way, "language") == "Would drive to language by 2 taps: home > settings > language (all proven)."
    data["roads"][1]["build"] = None
    assert roads.say_way(roads.plan(data, "home", "language", BUILD), "language") == \
        "Would drive to language by 2 taps: home > settings > language (1 of 2 roads unproven for this build)."
    data["places"]["language"] = {"key": {}, "link": "wayfare://settings/language", "titles": {}, "last": NOW}
    assert roads.say_way(roads.plan(data, "home", "language", BUILD), "language") == \
        "Would drive to language by link: wayfare://settings/language."


def test_a_repaired_road_replaces_the_old_move_and_keeps_its_count():
    data = graph(("home", "settings", "s"))
    old = data["roads"][0]["move"]
    old_seen = data["roads"][0]["seen"]
    made = roads.repair(data, "home", old, "settings", {"tool": "tap", "target": {"id": "s2"}}, {"id": "s2"}, 120, LATER, BUILD)
    assert made.new and [r["move"]["target"] for r in data["roads"]] == [{"id": "s2"}]
    assert data["roads"][0]["seen"] == old_seen + 1 and data["roads"][0]["build"] == BUILD
    assert [roads.identity(r) for r in made.gone] == [("home", json.dumps(old, sort_keys=True, separators=(",", ":")), "settings")]


def test_a_road_that_is_not_there_is_repaired_as_a_new_one():
    data = graph(("home", "settings", "s"))
    made = roads.repair(data, "home", {"tool": "tap", "target": {"id": "gone"}}, "settings", {"tool": "tap", "target": {"id": "s"}},
                        {"id": "s"}, 90, LATER, BUILD)
    assert not made.new and made.gone == [] and len(data["roads"]) == 1

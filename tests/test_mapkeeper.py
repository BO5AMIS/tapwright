"""The daemon's maps in memory: one per (folder, app), saved in the background under the lock (AGENT-TOOLS §18.9)."""
import asyncio
import json
from xml.etree import ElementTree as ET

from tapwright import mapkeeper, places, roads, ui

APP = "com.example.wayfare"


def tree(title="Settings", ids=("a",), kinds=True):
    bar = ET.Element("XCUIElementTypeNavigationBar", {"type": "XCUIElementTypeNavigationBar", "name": title, "enabled": "true",
                                                     "x": "0", "y": "50", "width": "430", "height": "90"})
    body = ET.Element("XCUIElementTypeTable", {"type": "XCUIElementTypeTable", "enabled": "true", "x": "0", "y": "150",
                                               "width": "430", "height": "600"})
    buttons = [ET.Element("XCUIElementTypeButton", {"type": "XCUIElementTypeButton", "name": i, "label": f"Label {i}",
                                                    "enabled": "true", "x": "10", "y": str(760 + 10 * n), "width": "100",
                                                    "height": "30"}) for n, i in enumerate(ids)]
    app = ET.Element("XCUIElementTypeApplication", {"type": "XCUIElementTypeApplication", "bundleId": APP, "label": "Wayfare",
                                                    "enabled": "true", "x": "0", "y": "0", "width": "430", "height": "932"})
    app.extend([*([bar, body] if kinds else []), *buttons])
    return ui.read(ET.tostring(app, encoding="unicode"), (430.0, 932.0))


def test_a_screen_with_nothing_to_recognise_it_by_is_no_place():
    empty = ui.read(ET.tostring(ET.Element("XCUIElementTypeApplication", {"type": "XCUIElementTypeApplication", "x": "0", "y": "0",
                                                                          "width": "430", "height": "932"}), encoding="unicode"),
                    (430.0, 932.0))
    assert not places.has_structure(empty) and places.has_structure(tree())


def test_the_first_screen_makes_a_map_and_says_there_was_none(tmp_path):
    maps = mapkeeper.Maps(str(tmp_path))
    assert not maps.has_map(None, APP)
    first = maps.observe(None, APP, tree(ids=("a",)), arrived=True)
    assert first.new and first.name == "place_1" and first.had_map is False
    second = maps.observe(None, APP, tree(ids=("x", "y", "z"), title="Other"), arrived=True)
    assert second.new and second.had_map is True and maps.has_map(None, APP)
    assert maps.observe(None, APP, tree(), arrived=True) is not None


def test_a_screen_with_no_structure_is_not_observed(tmp_path):
    maps = mapkeeper.Maps(str(tmp_path))
    nothing = ui.Tree([])
    assert maps.observe(None, APP, nothing, arrived=True) is None and not maps.has_map(None, APP)


def test_what_is_learned_is_saved_in_the_background_and_flush_waits(tmp_path):
    async def go():
        maps = mapkeeper.Maps(str(tmp_path))
        seen = maps.observe(None, APP, tree(ids=("a",)), arrived=True)
        other = maps.observe(None, APP, tree(ids=("x", "y", "z"), title="Other"), arrived=True)
        made = maps.record(None, APP, seen.name, other.name, {"tool": "tap", "target": {"id": "go"}}, {"id": "go"}, 90, "1.0 (1)")
        assert made.new
        await maps.flush()
        on_disk = json.loads((tmp_path / APP / "map.json").read_text("utf-8"))
        assert sorted(on_disk["places"]) == ["place_1", "place_2"] and len(on_disk["roads"]) == 1
        assert on_disk["roads"][0]["build"] == "1.0 (1)" and on_disk["roads"][0]["by"] == "tapwright"
    asyncio.run(go())


def test_two_daemons_on_one_folder_keep_each_others_roads(tmp_path):
    async def go():
        ours, theirs = mapkeeper.Maps(str(tmp_path)), mapkeeper.Maps(str(tmp_path))
        a = ours.observe(None, APP, tree(ids=("a",)), arrived=True)
        await ours.flush()
        b = theirs.observe(None, APP, tree(ids=("x", "y", "z"), title="Other"), arrived=True)  # loaded the file: knows `a`
        theirs.record(None, APP, a.name, b.name, {"tool": "tap", "target": {"id": "t"}}, {}, 1, None)
        ours.record(None, APP, a.name, a.name + "x", {"tool": "tap", "target": {"id": "o"}}, {}, 1, None)
        await asyncio.gather(ours.flush(), theirs.flush())
        got = json.loads((tmp_path / APP / "map.json").read_text("utf-8"))
        assert len(got["roads"]) == 2
    asyncio.run(go())


def test_a_map_file_that_does_not_parse_is_left_alone_and_the_problem_is_kept(tmp_path):
    async def go():
        (tmp_path / APP).mkdir()
        (tmp_path / APP / "map.json").write_text("{nope", "utf-8")
        maps = mapkeeper.Maps(str(tmp_path))
        seen = maps.observe(None, APP, tree(), arrived=True)
        assert seen.new  # driving goes on from memory
        await maps.flush()
        assert (tmp_path / APP / "map.json").read_text("utf-8") == "{nope"
        assert "doesn't parse" in maps.problem(None, APP)
        assert maps.problem_once(None, APP).startswith("The map file")  # said once...
        assert maps.problem_once(None, APP) is None  # ...not on every answer
    asyncio.run(go())


def test_each_call_may_name_its_own_folder(tmp_path):
    async def go():
        maps = mapkeeper.Maps(str(tmp_path / "default"))
        maps.observe(None, APP, tree(), arrived=True)
        maps.observe(str(tmp_path / "project"), APP, tree(), arrived=True)
        await maps.flush()
        assert (tmp_path / "default" / APP / "map.json").is_file() and (tmp_path / "project" / APP / "map.json").is_file()
    asyncio.run(go())


def test_forgetting_and_naming_write_through_at_once(tmp_path):
    async def go():
        maps = mapkeeper.Maps(str(tmp_path))
        seen = maps.observe(None, APP, tree(), arrived=True)
        named = maps.edit(None, APP, lambda data: places.set_name(data, seen.name, "Home"))
        assert named.name == "home"
        on_disk = json.loads((tmp_path / APP / "map.json").read_text("utf-8"))
        assert list(on_disk["places"]) == ["home"]
        maps.edit(None, APP, lambda data: places.forget(data, "home"))
        assert json.loads((tmp_path / APP / "map.json").read_text("utf-8"))["places"] == {}
        assert not maps.has_map(None, APP)
        # an edit through the file is not undone by the memory merged back in
        other = mapkeeper.Maps(str(tmp_path))
        other.observe(None, APP, tree(), arrived=True)
        await other.flush()
        assert json.loads((tmp_path / APP / "map.json").read_text("utf-8"))["places"]
    asyncio.run(go())


def test_a_repaired_road_replaces_the_old_one_in_memory_and_in_the_file(tmp_path):
    async def go():
        maps = mapkeeper.Maps(str(tmp_path))
        a = maps.observe(None, APP, tree(title="A", ids=("a",)), arrived=True)
        b = maps.observe(None, APP, tree(title="B", ids=("x", "y", "z")), arrived=True)
        old = {"tool": "tap", "target": {"id": "old"}}
        maps.record(None, APP, a.name, b.name, old, {"id": "old"}, 100, "1.0 (1)")
        await maps.flush()
        made = maps.repair(None, APP, a.name, old, b.name, {"tool": "tap", "target": {"id": "new"}}, {"id": "new"}, 90, "1.0 (1)")
        assert made.new and [r["move"]["target"] for r in maps.data(None, APP)["roads"]] == [{"id": "new"}]
        await maps.flush()
        on_disk = json.loads((tmp_path / APP / "map.json").read_text("utf-8"))
        assert [r["move"]["target"] for r in on_disk["roads"]] == [{"id": "new"}]  # the merge didn't bring the old one back
    asyncio.run(go())


def test_a_failed_road_is_dropped_on_the_second_failure_and_from_the_file(tmp_path):
    async def go():
        maps = mapkeeper.Maps(str(tmp_path))
        a = maps.observe(None, APP, tree(title="A", ids=("a",)), arrived=True)
        b = maps.observe(None, APP, tree(title="B", ids=("x", "y", "z")), arrived=True)
        move = {"tool": "tap", "target": {"id": "go"}}
        maps.record(None, APP, a.name, b.name, move, {"id": "go"}, 100, "1.0 (1)")
        await maps.flush()
        assert maps.fail(None, APP, a.name, move) == []
        assert maps.fail(None, APP, a.name, move) == [(a.name, b.name)]
        await maps.flush()
        assert json.loads((tmp_path / APP / "map.json").read_text("utf-8"))["roads"] == []
    asyncio.run(go())

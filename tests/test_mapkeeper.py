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


# ---- CodeRabbit PR #9 ----
def test_a_save_that_fails_on_the_disk_is_a_problem_the_flush_can_see_not_a_dead_task(tmp_path, monkeypatch):
    async def go():
        maps = mapkeeper.Maps(str(tmp_path))
        maps.observe(None, APP, tree(), arrived=True)

        def full(self, memory):
            raise OSError(28, "No space left on device")
        monkeypatch.setattr(mapkeeper.mapfile.MapFile, "save", full)
        await maps.flush()
        assert maps.problem(None, APP) is not None and "couldn't be saved" in maps.problem(None, APP)
        assert maps.has_map(None, APP)  # what was learned is kept in memory
    asyncio.run(go())


def test_an_edit_after_the_file_turned_unusable_keeps_the_places_in_memory(tmp_path):
    maps = mapkeeper.Maps(str(tmp_path))
    seen = maps.observe(None, APP, tree(), arrived=True)
    maps.edit(None, APP, lambda data: data["reflexes"].append({"name": "r", "do": {"tool": "back"}, "when": {"text": "x"}}))
    (tmp_path / APP / "map.json").write_text("{ not json", "utf-8")  # broken after the map was cached
    maps.edit(None, APP, lambda data: data["reflexes"].append({"name": "r2", "do": {"tool": "back"}, "when": {"text": "y"}}))
    data = maps.data(None, APP)
    assert seen.name in data["places"] and [r["name"] for r in data["reflexes"]] == ["r", "r2"]
    assert maps.problem(None, APP) is not None


def test_a_saved_route_follows_its_places_when_they_are_renamed(tmp_path):
    maps = mapkeeper.Maps(str(tmp_path))
    seen = maps.observe(None, APP, tree(), arrived=True)
    file = maps.file(None, APP)
    route = {"format": "agent-tools-script", "version": 2, "name": "go", "app": APP, "start": seen.name, "end": seen.name,
            "variables": [], "steps": []}
    file.write_route("go", route)
    maps.data(None, APP)["routes"]["go"] = "routes/go.json"
    maps.edit(None, APP, lambda data: places.set_name(data, seen.name, "Home"))
    saved = json.loads((tmp_path / APP / "routes" / "go.json").read_text("utf-8"))
    assert (saved["start"], saved["end"]) == ("home", "home")


def test_a_screen_with_only_container_kinds_is_not_saved_as_a_place_it_could_never_be_found_again(tmp_path):
    maps = mapkeeper.Maps(str(tmp_path))
    bare = tree(title="", ids=())
    assert places.has_structure(bare) and not places.recognisable(bare)
    assert maps.observe(None, APP, bare, arrived=True) is None and not maps.has_map(None, APP)


def _two_places_one_signature(tmp_path):
    """A map with places `one` and `two`: the same structural signature, different titles (so two places, one key id)."""
    maps = mapkeeper.Maps(str(tmp_path))
    seen = maps.observe(None, APP, tree(title="Alpha", ids=("a",)), arrived=True)
    data = maps.data(None, APP)
    first = data["places"].pop(seen.name)
    second = json.loads(json.dumps(first))
    second["titles"] = {k: "f" * 16 for k in first.get("titles", {"en": 0})}
    data["places"] = {"one": first, "two": second}
    assert places.key_id(first["key"]) == places.key_id(second["key"])
    return maps


def test_forgetting_a_place_never_rewrites_a_route_to_another_place_with_its_signature(tmp_path):
    """Codex PR #9 c2: the signature-only lookup took `forget one` for a rename `one -> two` and rewrote the routes."""
    async def go():
        maps = _two_places_one_signature(tmp_path)
        file = maps.file(None, APP)
        file.save(maps.data(None, APP))
        file.write_route("there", {"start": "one", "end": "two", "steps": [{"tool": "tap"}]})
        maps.data(None, APP)["routes"] = file.load()["routes"]
        maps.edit(None, APP, lambda data: places.forget(data, "one"))
        route = json.loads((tmp_path / APP / "routes" / "there.json").read_text("utf-8"))
        assert route["start"] == "one" and route["end"] == "two"  # a forget rewrites no route
        assert sorted(maps.data(None, APP)["places"]) == ["two"]
    asyncio.run(go())


def test_a_real_rename_still_rewrites_the_routes(tmp_path):
    async def go():
        maps = mapkeeper.Maps(str(tmp_path))
        seen = maps.observe(None, APP, tree(title="Alpha", ids=("a",)), arrived=True)
        other = maps.observe(None, APP, tree(title="Beta", ids=("x", "y", "z")), arrived=True)
        file = maps.file(None, APP)
        file.save(maps.data(None, APP))
        file.write_route("there", {"start": seen.name, "end": other.name, "steps": [{"tool": "tap"}]})
        maps.data(None, APP)["routes"] = file.load()["routes"]
        maps.edit(None, APP, lambda data: places.set_name(data, seen.name, "home"))
        route = json.loads((tmp_path / APP / "routes" / "there.json").read_text("utf-8"))
        assert route["start"] == "home" and route["end"] == other.name
    asyncio.run(go())


def test_a_place_forgotten_while_the_cleanup_runs_is_not_put_back(tmp_path, monkeypatch):
    """Codex PR #9 c2: a forget starts the background cleanup (`file.update(undo)`); a second forget made while it ran added
    its tombstone after the batch was taken, the cleanup's map still held the place, and memory took it back."""
    async def go():
        maps = mapkeeper.Maps(str(tmp_path))
        a = maps.observe(None, APP, tree(title="Alpha", ids=("a",)), arrived=True)
        b = maps.observe(None, APP, tree(title="Beta", ids=("x", "y", "z")), arrived=True)
        await maps.flush()
        assert sorted(json.loads((tmp_path / APP / "map.json").read_text("utf-8"))["places"]) == sorted([a.name, b.name])
        maps.observe(None, APP, tree(title="Gamma", ids=("p", "q", "r", "s")), arrived=True)  # a save is now under way
        real, fired = asyncio.to_thread, []

        async def interleaved(fn, *args):
            done = await real(fn, *args)
            name = getattr(fn, "__name__", "")
            if name == "save" and not fired:  # the save has returned: an edit forgets `a` (its tombstone: the cleanup is due)
                fired.append("a")
                maps.edit(None, APP, lambda data: places.forget(data, a.name))
            elif name == "update" and fired == ["a"]:  # the cleanup's file update has just returned: a second edit
                fired.append("b")
                maps.edit(None, APP, lambda data: places.forget(data, b.name))
            return done
        monkeypatch.setattr(mapkeeper.asyncio, "to_thread", interleaved)
        await maps.flush()
        monkeypatch.undo()
        assert fired == ["a", "b"] and b.name not in maps.data(None, APP)["places"], "the cleanup's map put the forgotten place back"
        maps.observe(None, APP, tree(title="Delta", ids=("m", "n", "o", "p", "q")), arrived=True)  # a later save
        await maps.flush()
        for held in (maps.data(None, APP), json.loads((tmp_path / APP / "map.json").read_text("utf-8"))):
            assert a.name not in held["places"] and b.name not in held["places"]
    asyncio.run(go())

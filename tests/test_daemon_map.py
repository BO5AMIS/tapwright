"""The `map` tool through the daemon (AGENT-TOOLS §18.8): the app's map read from the screen in front, edited, saved."""
import asyncio
import json
import threading

import pytest
from mapfakes import APP, MapSession, screen
from test_daemon import finish, make, run, started

from tapwright import mapkeeper
from tapwright.control import Refused
from tapwright.daemon import Scope

BUNDLE = APP[1]


def home():
    return screen("Home", ids=("settings-btn",), tab="home")


def settings():
    return screen("Settings", ids=("language-row", "about-row"))


def language():
    return screen("Language", ids=("english", "arabic"))


def mapped(tmp_path, start="home", **goes):
    d = make(MapSession({"home": home(), "settings": settings(), "language": language()}, start, goes))
    d.maps = mapkeeper.Maps(str(tmp_path))
    return d


def file_of(tmp_path) -> dict:
    return json.loads((tmp_path / BUNDLE / "map.json").read_text("utf-8"))


def test_the_map_lists_the_app_and_where_it_is(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        r = await d.run_tool("a", "map", {})
        assert r["text"].splitlines() == [
            "Map of Example (com.example.app), build 1.0 (7): 1 place, 0 roads (0 proven), 0 routes, 0 reflexes. Now at: place_1.",
            "Places: place_1"]
        assert r["image"] is None
        await d.maps.flush()
        assert list(file_of(tmp_path)["places"]) == ["place_1"]
        await finish(stop, task)
    run(go)


def test_a_map_call_touches_nothing_on_the_phone(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        before = list(d.session.calls)
        await d.run_tool("a", "map", {})
        new = d.session.calls[len(before):]
        assert [c for c in new if c in ("tap", "swipe", "launch", "key:enter", "alert")] == []
        await finish(stop, task)
    run(go)


def test_naming_the_place_and_the_way_to_another(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        assert (await d.run_tool("a", "map", {"name": "Home Screen"}))["text"] == "Named: home_screen."
        assert d.session.at == "home"
        d.session.at = "settings"
        await d.run_tool("a", "map", {"name": "settings"})
        data = d.maps.data(None, BUNDLE)
        data["roads"].append({"from": "home_screen", "to": "settings", "move": {"tool": "tap", "target": {"id": "settings-btn"}},
                              "names": {}, "by": "tapwright", "seen": 1, "failed": 0, "ms": 90, "last": "2026-11-12T10:00:00Z",
                              "build": "1.0 (7)"})
        d.session.at = "home"
        r = await d.run_tool("a", "map", {"to": "settings"})
        assert r["text"] == "Would drive to settings by 1 tap: home_screen > settings (all proven)."
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "map", {"to": "nowhere"})
        assert e.value.code == "not_found"
        await d.maps.flush()
        assert sorted(file_of(tmp_path)["places"]) == ["home_screen", "settings"]
        await finish(stop, task)
    run(go)


def test_a_link_a_reflex_and_forgetting(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "map", {"name": "home"})
        assert (await d.run_tool("a", "map", {"link": "wayfare://home", "place": "home"}))["text"] == "Link for home: wayfare://home."
        rate = {"name": "rate_prompt", "when": {"text": "Rate Wayfare"}, "do": {"tool": "tap", "target": {"text": "Not now"}}}
        assert (await d.run_tool("a", "map", {"reflex": rate}))["text"] == "Reflex added: rate_prompt."
        listed = (await d.run_tool("a", "map", {}))["text"].splitlines()
        assert listed[0].endswith("1 place, 0 roads (0 proven), 0 routes, 1 reflex. Now at: home.")
        assert listed[1:] == ["Places: home (link wayfare://home)", "Reflexes: rate_prompt."]
        await d.maps.flush()  # no background save is under way while the edits below are made
        assert (await d.run_tool("a", "map", {"forget_reflex": "rate_prompt"}))["text"] == "Reflex removed: rate_prompt."
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "map", {"reflex": {**rate, "do": {"tool": "tap", "target": {"text": "Delete account"}}}})
        assert e.value.code == "not_allowed"
        assert (await d.run_tool("a", "map", {"forget": "home"}))["text"] == "Forgot home."
        assert file_of(tmp_path)["places"] == {} and file_of(tmp_path)["reflexes"] == []
        await finish(stop, task)
    run(go)


def test_a_forgotten_reflex_is_not_brought_back_by_a_save_under_way(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "map", {"name": "home"})
        rate = {"name": "rate_prompt", "when": {"text": "Rate Wayfare"}, "do": {"tool": "tap", "target": {"text": "Not now"}}}
        await d.run_tool("a", "map", {"reflex": rate})
        await d.maps.flush()
        file = d.maps.file(None, BUNDLE)
        real_save, entered, release = file.save, threading.Event(), threading.Event()

        def held_save(memory):  # the background save: its snapshot is taken, it waits before it reaches the file
            entered.set()
            assert release.wait(10)
            return real_save(memory)
        file.save = held_save
        d.maps._changed(None, BUNDLE)
        assert await asyncio.get_running_loop().run_in_executor(None, entered.wait, 10)
        file.save = real_save
        assert (await d.run_tool("a", "map", {"forget_reflex": "rate_prompt"}))["text"] == "Reflex removed: rate_prompt."
        release.set()
        await d.maps.flush()
        assert file_of(tmp_path)["reflexes"] == []
        assert d.maps.data(None, BUNDLE)["reflexes"] == []
        await finish(stop, task)
    run(go)


def test_a_forgotten_place_and_its_roads_are_not_brought_back_by_a_save_under_way(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "map", {"name": "home"})
        d.session.at = "settings"
        await d.run_tool("a", "map", {"name": "settings"})
        data = d.maps.data(None, BUNDLE)
        data["roads"].append({"from": "home", "to": "settings", "move": {"tool": "tap", "target": {"id": "settings-btn"}},
                              "names": {}, "by": "tapwright", "seen": 1, "failed": 0, "ms": 90, "last": "2026-11-12T10:00:00Z",
                              "build": "1.0 (7)"})
        await d.maps.flush()
        file = d.maps.file(None, BUNDLE)
        real_save, entered, release = file.save, threading.Event(), threading.Event()

        def held_save(memory):  # the snapshot holds home and its road; the save waits before it reaches the file
            entered.set()
            assert release.wait(10)
            return real_save(memory)
        file.save = held_save
        d.maps._changed(None, BUNDLE)
        assert await asyncio.get_running_loop().run_in_executor(None, entered.wait, 10)
        file.save = real_save
        assert (await d.run_tool("a", "map", {"forget": "home"}))["text"] == "Forgot home and its 1 road."  # the phone is at settings
        release.set()
        await d.maps.flush()
        assert list(file_of(tmp_path)["places"]) == ["settings"] and file_of(tmp_path)["roads"] == []
        assert list(d.maps.data(None, BUNDLE)["places"]) == ["settings"] and d.maps.data(None, BUNDLE)["roads"] == []
        await finish(stop, task)
    run(go)


def test_bad_arguments(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        for args in ({"to": "x", "name": "y"}, {"place": "x"}, {"colour": "red"}, {"name": ""}, {"reflex": "x"}):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "map", args)
            assert e.value.code == "bad_params", args
        await finish(stop, task)
    run(go)


def test_map_is_not_a_step_of_run(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": [{"tool": "map"}]})
        assert e.value.code == "bad_params" and "'map' can't be a step" in str(e.value)
        await finish(stop, task)
    run(go)


def test_map_needs_the_app_in_scope_like_every_call(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        assert (await d.run_tool("a", "map", {}, scope=Scope(apps=(BUNDLE,))))["text"].startswith("Map of Example")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "map", {}, scope=Scope(apps=("com.other.app",)))
        assert e.value.code == "not_allowed"
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "map", {}, scope=Scope(apps=()))
        assert e.value.code == "not_allowed" and "no_apps" in str(e.value)
        await finish(stop, task)
    run(go)


def test_no_app_in_front(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        d.session.app = None
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "map", {})
        assert e.value.code == "not_found" and "open_app" in str(e.value)
        await finish(stop, task)
    run(go)


def test_a_calls_own_folder_and_the_build_it_cannot_read(tmp_path):
    async def go():
        d = mapped(tmp_path / "default")
        stop, task = await started(d)
        d.session.installed = []  # a built-in app: no version to read
        r = await d.run_tool("a", "map", {}, maps=str(tmp_path / "project"))
        assert r["text"].startswith("Map of Example (com.example.app): 1 place")  # no "build ..."
        await d.maps.flush()
        assert (tmp_path / "project" / BUNDLE / "map.json").is_file() and not (tmp_path / "default").exists()
        await finish(stop, task)
    run(go)


def test_a_map_file_that_does_not_parse_is_said_once(tmp_path):
    async def go():
        (tmp_path / BUNDLE).mkdir()
        (tmp_path / BUNDLE / "map.json").write_text("{nope", "utf-8")
        d = mapped(tmp_path)
        stop, task = await started(d)
        first = (await d.run_tool("a", "map", {}))["text"]
        assert "Note: The map file" in first and "doesn't parse" in first and "left alone" in first
        assert "Note:" not in (await d.run_tool("a", "map", {}))["text"]
        await d.maps.flush()
        assert (tmp_path / BUNDLE / "map.json").read_text("utf-8") == "{nope"
        await finish(stop, task)
    run(go)


def test_the_build_is_the_apps_version_and_number(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        assert await d._build_of(BUNDLE) == "1.0 (7)"
        assert await d._build_of("com.apple.Preferences") is None
        d.session.installed = [{"bundle_id": BUNDLE, "name": "Example", "version": "2.0", "build": "9", "executable": "E"}]
        assert await d._build_of(BUNDLE) == "1.0 (7)"  # remembered for a while
        d._builds.clear()
        assert await d._build_of(BUNDLE) == "2.0 (9)"
        await finish(stop, task)
    run(go)


def test_a_journey_in_a_users_eyes_has_no_map(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "journey", {"start": True, "goal": "see the settings"})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "map", {})
        assert e.value.code == "not_allowed" and "a user has no map" in str(e.value)
        await finish(stop, task)
    run(go)

"""`drive` through the daemon (AGENT-TOOLS §18.4): over the map's proven roads, by a link, with its checks and its repairs."""
import json
import re

import pytest
from mapfakes import APP, MapSession, screen
from test_daemon import finish, make, run, started

from tapwright import mapkeeper
from tapwright.control import Refused
from tapwright.daemon import Scope

BUNDLE = APP[1]
GOES = {("home", "settings-btn"): "settings", ("settings", "general-row"): "general", ("general", "language-row"): "language",
        ("settings", "about-row"): "about", ("about", "language-row"): "language", ("home", "wayfare://settings"): "settings",
        ("home", "wayfare://trips/42"): "trip", ("language", "swipe"): "home", ("unknown", "wayfare://trips/42"): "trip"}


def screens():
    return {"home": screen("Home", ids=("settings-btn",), tab="home"),
            "settings": screen("Settings", ids=("general-row", "about-row", "s1", "s2", "s3")),
            "general": screen("General", ids=("language-row", "x1", "x2")),
            "about": screen("About", ids=("language-row", "version", "licenses")),
            "language": screen("Language", ids=("english", "arabic")),
            "trip": screen("Trip", ids=("share", "map-pin"), rows=("a",)),
            "unknown": screen("Mystery", ids=("q1", "q2", "q3", "q4"), inputs=("z",))}


def mapped(tmp_path, goes=None, extra=None):
    session = MapSession({**screens(), **(extra or {})}, "home", {**GOES, **(goes or {})})
    session.has_nav_bar, session.back_at = True, (20.0, 70.0)
    d = make(session)
    d.maps = mapkeeper.Maps(str(tmp_path))
    return d


def file_of(tmp_path) -> dict:
    return json.loads((tmp_path / BUNDLE / "map.json").read_text("utf-8"))


async def learn(d, path=(("home", "settings-btn", "settings"), ("settings", "general-row", "general"),
                         ("general", "language-row", "language"))):
    """Drive the way by hand once, naming each place, so that the map has places and roads."""
    await d.run_tool("a", "look", {"screenshot": False})
    await d.run_tool("a", "map", {"name": path[0][0]})
    for _, ident, name in path:
        await d.run_tool("a", "tap", {"target": {"id": ident}})
        await d.run_tool("a", "map", {"name": name})
    d.session.at = path[0][0]
    await d.run_tool("a", "look", {"screenshot": False})


async def drive(d, **args):
    return await d.run_tool("a", "drive", args)


def taps(d) -> int:
    return d.session.calls.count("tap")


def test_it_drives_a_chain_of_proven_roads_and_answers_once(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await learn(d)
        before = taps(d)
        r = await drive(d, to="language", screenshot=False)
        lines = r["text"].splitlines()
        assert re.fullmatch(r"Arrived at language by 3 taps in \d+\.\d s \(home > settings > general > language\)\.", lines[0]), lines[0]
        assert lines[1] == "Place: language." and lines[2].startswith("Screen ")
        assert taps(d) - before == 3 and d.session.at == "language"
        assert r["image"] is None
        await finish(stop, task)
    run(go)


def test_the_roads_are_driven_again_and_counted(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await learn(d)
        await drive(d, to="language")
        await d.maps.flush()
        assert [r["seen"] for r in file_of(tmp_path)["roads"]] == [2, 2, 2]
        await finish(stop, task)
    run(go)


def test_roads_of_another_build_are_proven_again_and_said(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await learn(d)
        for road in d.maps.data(None, BUNDLE)["roads"]:
            road["build"] = "0.9 (1)"
        r = await drive(d, to="language")
        assert "Note: 3 roads proven again for build 1.0 (7)." in r["text"]
        assert {road["build"] for road in d.maps.data(None, BUNDLE)["roads"]} == {"1.0 (7)"}
        d.session.at = "home"
        assert "proven again" not in (await drive(d, to="language"))["text"]
        await finish(stop, task)
    run(go)


def test_a_dry_run_says_the_way_and_touches_nothing(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await learn(d)
        before = taps(d)
        r = await drive(d, to="language", dry_run=True)
        assert r["text"] == "Would drive to language by 3 taps: home > settings > general > language (all proven)."
        assert taps(d) == before and d.session.at == "home"
        await finish(stop, task)
    run(go)


def test_already_there(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await learn(d)
        before = taps(d)
        r = await drive(d, to="home")
        assert r["text"].splitlines()[:2] == ["Already at home.", "Place: home."] and taps(d) == before
        await finish(stop, task)
    run(go)


def test_a_place_with_a_link_is_a_jump_and_by_taps_never_jumps(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await learn(d)
        await d.run_tool("a", "map", {"link": "wayfare://settings", "place": "settings"})
        before = taps(d)
        r = await drive(d, to="settings")
        assert re.fullmatch(r"Arrived at settings by link in \d+\.\d s( \(home > settings\))?\.", r["text"].splitlines()[0])
        assert "open_url" in d.session.calls and taps(d) == before
        d.session.at = "home"
        calls = d.session.calls.count("open_url")
        r = await drive(d, to="settings", by="taps")
        assert "by 1 tap " in r["text"] and d.session.calls.count("open_url") == calls
        d.session.at = "home"
        with pytest.raises(Refused) as e:
            await drive(d, to="general", by="link")
        assert e.value.code == "not_found"
        await finish(stop, task)
    run(go)


def test_a_jump_works_from_a_screen_the_map_doesnt_know_and_fills_its_params(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await learn(d)
        await d.run_tool("a", "tap", {"target": {"id": "settings-btn"}})
        d.session.at = "trip"
        await d.run_tool("a", "map", {"name": "trip_id"})
        await d.run_tool("a", "map", {"link": "wayfare://trips/[id]", "place": "trip_id"})
        d.session.at = "unknown"
        r = await drive(d, to="trip_id", **{"with": {"id": "42"}})
        assert r["text"].startswith("Arrived at trip_id by link") and d.session.at == "trip"
        d.session.at = "unknown"
        with pytest.raises(Refused) as e:
            await drive(d, to="trip_id")
        assert e.value.code == "bad_params" and "needs id" in str(e.value)
        await finish(stop, task)
    run(go)


def test_from_a_place_the_map_doesnt_know_roads_are_off_route_before_anything_is_touched(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await learn(d)
        d.session.at = "unknown"
        before = taps(d)
        with pytest.raises(Refused) as e:
            await drive(d, to="language")
        assert e.value.code == "off_route" and "Nothing else was touched." in str(e.value) and "Done: 0 moves." in str(e.value)
        assert taps(d) == before
        await finish(stop, task)
    run(go)


def test_no_such_place_and_no_known_way(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await learn(d)
        with pytest.raises(Refused) as e:
            await drive(d, to="nowhere")
        assert e.value.code == "not_found" and "no place called nowhere" in str(e.value)
        d.session.at = "language"
        await d.run_tool("a", "look", {"screenshot": False})
        with pytest.raises(Refused) as e:
            await drive(d, to="home")
        assert str(e.value) == "no known way from language to home; drive it once (the driver), and it is remembered."
        await finish(stop, task)
    run(go)


def test_a_missing_target_is_repaired_by_the_closest_match_and_saved_after_arrival(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await learn(d)
        d.session.screens["settings"] = screen("Settings", ids=("general-row2", "about-row", "s1", "s2", "s3"))
        d.session.goes[("settings", "general-row2")] = "general"
        r = await drive(d, to="language")
        assert 'Road repaired: id=general-row2 (was id=general-row).' in r["text"] and r["text"].startswith("Arrived at language by 3 taps")
        await d.maps.flush()
        moves = {(x["from"], x["to"]): x["move"]["target"] for x in file_of(tmp_path)["roads"]}
        assert moves[("settings", "general")] == {"id": "general-row2"} and len(moves) == 3
        await finish(stop, task)
    run(go)


def test_a_repair_that_doesnt_arrive_is_not_saved(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await learn(d)
        d.session.screens["settings"] = screen("Settings", ids=("general-row2", "about-row", "s1", "s2", "s3"))  # no `goes`: the tap leads nowhere
        with pytest.raises(Refused) as e:
            await drive(d, to="language")
        assert e.value.code == "off_route" and "Done: 2 moves." in str(e.value)
        await d.maps.flush()
        assert {x["move"]["target"]["id"] for x in file_of(tmp_path)["roads"]} == {"settings-btn", "general-row", "language-row"}
        await finish(stop, task)
    run(go)


def test_a_missing_target_with_no_repair_is_off_route_and_counts_a_failure(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await learn(d)
        d.session.screens["settings"] = screen("Settings", ids=("about-row", "s1", "s2", "s3"))
        with pytest.raises(Refused) as e:
            await drive(d, to="language")
        text = str(e.value)
        assert e.value.code == "off_route"
        assert text.splitlines()[0] == ('stopped at step 2 of 3 (tap id=general-row): it isn\'t on this screen. '
                                        'Now at: settings (known). Done: 1 move. Nothing else was touched.')
        assert any(line.startswith("Screen ") for line in text.splitlines())  # followed by the screen
        failed = [r for r in d.maps.data(None, BUNDLE)["roads"] if r["move"]["target"].get("id") == "general-row"]
        assert failed[0]["failed"] == 1
        with pytest.raises(Refused) as e:  # the second time the road is dropped, and said
            await drive(d, to="language")
        assert "road dropped: settings > general (it failed twice)." in str(e.value)
        await finish(stop, task)
    run(go)


def test_arriving_somewhere_else_that_is_known_plans_again_from_there(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await learn(d)
        await d.run_tool("a", "tap", {"target": {"id": "settings-btn"}})
        await d.run_tool("a", "tap", {"target": {"id": "about-row"}})
        await d.run_tool("a", "map", {"name": "about"})
        await d.run_tool("a", "tap", {"target": {"id": "language-row"}})
        d.session.at = "home"
        for road in d.maps.data(None, BUNDLE)["roads"]:  # the way through about costs more, so general is the way chosen first
            if road["from"] == "about":
                road["build"] = "0.9 (1)"
        d.session.goes[("settings", "general-row")] = "about"  # the road to general now leads to about
        r = await drive(d, to="language")
        assert r["text"].startswith("Arrived at language by ") and d.session.at == "language"
        assert "about" in r["text"].splitlines()[0]
        await d.maps.flush()
        road = next(x for x in file_of(tmp_path)["roads"] if (x["from"], x["to"]) == ("settings", "general"))
        assert road["failed"] == 1
        await finish(stop, task)
    run(go)


def test_a_screen_that_is_nowhere_known_after_a_move_is_off_route(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await learn(d)
        d.session.goes[("settings", "general-row")] = "unknown"
        with pytest.raises(Refused) as e:
            await drive(d, to="language")
        text = str(e.value).splitlines()[0]
        assert e.value.code == "off_route" and "it led to a screen the map doesn't know" in text and "Done: 2 moves." in text
        await finish(stop, task)
    run(go)


def test_it_waits_for_a_screen_that_is_still_loading(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await learn(d)
        shown = {"n": 0}
        real = d.session._show

        def slow():
            if d.session.at == "settings" and shown["n"] < 4:
                shown["n"] += 1
                d.session.screens["splash"] = screen("Loading", ids=("spinner-a", "spinner-b", "spinner-c"))
                tree = d.session.screens["splash"]
                d.session.items, d.session.last_tree = list(tree.items), tree
                return tree
            return real()
        d.session._show = slow
        r = await drive(d, to="language")
        assert r["text"].startswith("Arrived at language by 3 taps") and shown["n"] == 4
        await finish(stop, task)
    run(go)


def test_a_flagged_move_stops_the_drive_with_the_usual_error_and_what_was_done(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await learn(d)
        before = taps(d)
        d.session.screens["settings"] = screen("Settings", ids=("Delete", "about-row", "s1", "s2", "s3"))
        data = d.maps.data(None, BUNDLE)
        for road in data["roads"]:
            if road["move"]["target"].get("id") == "general-row":
                road["names"] = {"id": "general-row", "label": "Label of Delete"}
        with pytest.raises(Refused) as e:
            await drive(d, to="language")
        assert e.value.code == "not_allowed" and "needs_confirmation" in str(e.value).splitlines()[0]
        assert "Done: 1 move." in str(e.value).splitlines()[0]
        assert taps(d) - before == 1  # only the first move was made
        await finish(stop, task)
    run(go)


def test_a_pause_between_moves_stops_the_drive(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await learn(d)
        real = d.session.tap

        async def tap_then_pause(target, hold_ms=60):
            out = await real(target, hold_ms)
            d.control.set_paused(True)
            return out
        d.session.tap = tap_then_pause
        with pytest.raises(Refused) as e:
            await drive(d, to="language")
        assert e.value.code == "paused" and "Done: 1 move." in str(e.value)
        await finish(stop, task)
    run(go)


def test_a_row_with_no_id_cant_be_replayed(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await learn(d)
        for road in d.maps.data(None, BUNDLE)["roads"]:
            if road["move"]["target"].get("id") == "general-row":
                road["move"] = {"tool": "tap", "target": {"row": True}}
        with pytest.raises(Refused) as e:
            await drive(d, to="language")
        assert e.value.code == "off_route" and "list row" in str(e.value)
        await finish(stop, task)
    run(go)


def test_then_steps_run_after_arriving_in_the_same_call(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await learn(d)
        r = await drive(d, to="language", then=[{"tool": "tap", "target": {"id": "english"}}])
        lines = r["text"].splitlines()
        assert lines[0].startswith("Arrived at language by 3 taps") and re.fullmatch(r"Ran 1 of 1 steps in \d+\.\d s\.", lines[1])
        with pytest.raises(Refused) as e:
            d.session.at = "home"
            await drive(d, to="language", then=[{"tool": "tap", "target": {"id": "nope"}}])
        assert e.value.code == "not_found" and str(e.value).startswith("Arrived at language")
        await finish(stop, task)
    run(go)


def test_look_false_and_a_screenshot_and_brief(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await learn(d)
        r = await drive(d, to="language", look=False)
        lines = r["text"].splitlines()
        assert len(lines) == 1 and lines[0].startswith("Arrived at language")
        d.session.at = "home"
        r = await drive(d, to="language", brief=True)
        assert r["text"].startswith("Arrived at language")
        await finish(stop, task)
    run(go)


def test_drive_is_a_step_of_run_and_counts_as_one(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await learn(d)
        r = await d.run_tool("a", "run", {"steps": [{"tool": "drive", "to": "language"}, {"tool": "tap", "target": {"id": "english"}}]})
        assert r["text"].startswith("Ran 2 of 2 steps")
        await finish(stop, task)
    run(go)


def test_bad_arguments(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        for args in ({}, {"to": "a", "route": "b"}, {"to": "a", "by": "fly"}, {"to": "a", "colour": 1}):
            with pytest.raises(Refused) as e:
                await drive(d, **args)
            assert e.value.code == "bad_params", args
        await finish(stop, task)
    run(go)


def test_personal_mode_only_in_a_given_app(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await learn(d)
        r = await d.run_tool("a", "drive", {"to": "language"}, scope=Scope(apps=(BUNDLE,)))
        assert r["text"].startswith("Arrived at language")
        d.session.at = "home"
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "drive", {"to": "language"}, scope=Scope(apps=("com.other.app",)))
        assert e.value.code == "not_allowed"
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "drive", {"to": "language"}, scope=Scope(apps=()))
        assert e.value.code == "not_allowed" and "no_apps" in str(e.value)
        await finish(stop, task)
    run(go)


def test_the_drive_is_logged_as_one_call_and_its_moves_as_steps_of_it(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await learn(d)
        r = await drive(d, to="language")
        assert "Arrived" in r["text"]
        await finish(stop, task)
    run(go)


def test_in_a_journey_in_a_users_eyes_drive_uses_roads_aimed_by_their_visible_text(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await learn(d)
        await d.run_tool("a", "map", {"link": "wayfare://settings", "place": "settings"})
        await d.run_tool("a", "journey", {"start": True, "goal": "change the language"})
        before = d.session.calls.count("open_url")
        r = await drive(d, to="settings")  # a link would be a jump: a user has none
        assert "by 1 tap " in r["text"].splitlines()[0] and d.session.calls.count("open_url") == before
        assert d.session.tap_points  # the tap was aimed by "Label of settings-btn", the text a person reads
        with pytest.raises(Refused) as e:
            await drive(d, to="settings", by="link")
        assert e.value.code == "not_allowed" and "allow" in str(e.value)
        await finish(stop, task)
    run(go)


def test_a_journey_that_allows_drive_may_jump(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await learn(d)
        await d.run_tool("a", "map", {"link": "wayfare://settings", "place": "settings"})
        await d.run_tool("a", "journey", {"start": True, "goal": "g", "allow": ["drive"]})
        r = await drive(d, to="settings")
        assert r["text"].startswith("Arrived at settings by link") and "open_url" in d.session.calls
        await finish(stop, task)
    run(go)


def test_a_road_with_no_visible_text_cant_be_driven_in_a_users_eyes(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await learn(d)
        await d.maps.flush()  # (a later save merges the file's names back in: edit once nothing is waiting to be saved)
        for road in d.maps.data(None, BUNDLE)["roads"]:
            road["names"] = {"id": road["names"]["id"]}
        await d.run_tool("a", "journey", {"start": True, "goal": "g"})
        with pytest.raises(Refused) as e:
            await drive(d, to="language")
        assert e.value.code == "off_route" and "it has no text a person could read" in str(e.value)
        await finish(stop, task)
    run(go)

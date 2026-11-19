"""1.9.0, the final Opus review (REVIEW-1.9.0-OPUS2), its repros turned round: each test fails on c6cf7cc."""
import asyncio
import dataclasses
import json

import pytest
from mapfakes import APP, MapSession, screen
from test_daemon import finish, keyboard_up, make, run, started
from test_tokens_190 import multiline

from tapwright import autoroute, mapfile, mapkeeper, routes as routelib
from tapwright.control import Refused

BUNDLE = APP[1]


def search_screens():
    return {"home": screen("Home", ids=("open-list", "h1"), tab="home", inputs=("search",)),
            "list": screen("Results", ids=("open-detail", "l1", "go-search")),
            "detail": screen("Detail", ids=("d1", "d2")),
            "lost": screen("Elsewhere", ids=("z1",), texts=("never seen",))}


GOES = {("home", "open-list"): "list", ("list", "open-detail"): "detail", ("list", "go-search"): "detail"}


def mapped(tmp_path, screens=None, goes=None):
    d = make(MapSession(screens or search_screens(), "home", goes or GOES))
    d.maps = mapkeeper.Maps(str(tmp_path))
    return d


def saved(tmp_path):
    folder = tmp_path / BUNDLE / "routes"
    return {p.stem: json.loads(p.read_text("utf-8")) for p in folder.glob("*.json")} if folder.is_dir() else {}


def known(text):
    return [line for line in text.splitlines() if line.startswith("Known:")]


# ---- H1: the map's link values are `[id]` ----

def test_h1_one_helper_knows_a_links_value_in_the_maps_own_form():
    assert autoroute.needs_value("app://home/[id]") and autoroute.needs_value("app://trip/${id}")
    assert not autoroute.needs_value("app://home")
    assert autoroute.step_of({"tool": "open_url", "link": "app://item/[id]"}) is None
    assert not autoroute.given_nothing({"variables": [], "steps": [{"tool": "open_url", "url": "app://item/[id]"}]})
    assert not autoroute.only_navigation([{"tool": "open_url", "url": "app://item/[id]"}], strict=True)


def test_h1_known_leaves_out_a_route_whose_start_needs_a_links_value(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": BUNDLE})
        await d.run_tool("a", "tap", {"target": {"id": "open-list"}})
        start = saved(tmp_path)["open_list"]["start"]
        d.maps.edit(str(tmp_path), BUNDLE, lambda m: m["places"][start].__setitem__("link", "app://home/[id]"))
        d.session.at = "lost"
        r = await d.run_tool("b", "look", {"screenshot": False})
        assert not known(r["text"])
        await finish(stop, task)
    run(go)


def test_h1_an_open_url_to_a_pattern_place_saves_no_route_with_the_pattern(tmp_path):
    async def go():
        d = mapped(tmp_path, goes={**GOES, ("home", "app://item/42"): "detail"})
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": BUNDLE})
        await d.run_tool("a", "tap", {"target": {"id": "open-list"}})
        await d.run_tool("a", "tap", {"target": {"id": "open-detail"}})
        detail = saved(tmp_path)["open_detail"]["end"]
        d.maps.edit(str(tmp_path), BUNDLE, lambda m: m["places"][detail].__setitem__("link", "app://item/[id]"))
        for f in (tmp_path / BUNDLE / "routes").glob("open_detail.json"):
            f.unlink()
        d.maps.edit(str(tmp_path), BUNDLE, lambda m: m["routes"].pop("open_detail", None))
        d.session.at = "home"
        await d.run_tool("a", "open_app", {"app": BUNDLE})
        await d.run_tool("a", "open_url", {"url": "app://item/42"})
        urls = [s.get("url") for r in saved(tmp_path).values() for s in r["steps"] if s.get("tool") == "open_url"]
        assert "app://item/[id]" not in urls
        await finish(stop, task)
    run(go)


# ---- H2: Known offers only a route that is strictly navigation ----

PERSONS = {
    "floor": [{"tool": "tap", "target": {"id": "open-list"}}, {"tool": "tap", "target": {"label": "Delete trip"}}],
    "nested": [{"tool": "drive", "route": "needs_city"}, {"tool": "tap", "target": {"id": "open-detail"}}],
    "vault": [{"tool": "type_text", "target": {"id": "search"}, "text": "${vault.card.number}"},
              {"tool": "tap", "target": {"id": "open-list"}}],
    "typed": [{"tool": "type_text", "target": {"id": "search"}, "text": "private words", "submit": True},
              {"tool": "tap", "target": {"id": "open-list"}}],
    "submit": [{"tool": "tap", "target": {"id": "open-list"}}, {"tool": "tap", "target": {"label": "Send"}}],
}


@pytest.mark.parametrize("kind", sorted(PERSONS))
def test_h2_known_never_offers_a_persons_route_that_does_more_than_navigate(tmp_path, kind):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": BUNDLE})
        await d.run_tool("a", "tap", {"target": {"id": "open-list"}})
        home, lst = saved(tmp_path)["open_list"]["start"], saved(tmp_path)["open_list"]["end"]
        need = routelib.route("needs_city", BUNDLE, home, lst, ["city"], [
            {"tool": "type_text", "target": {"id": "search"}, "text": "${city}"}, {"tool": "tap", "target": {"id": "open-list"}}])
        d.maps.file(str(tmp_path), BUNDLE).write_route("needs_city", need)
        person = routelib.route("persons_way", BUNDLE, home, lst, [], PERSONS[kind])
        routelib.check(person)
        d.maps.file(str(tmp_path), BUNDLE).write_route("persons_way", person)
        d.maps.edit(str(tmp_path), BUNDLE, lambda m: m["routes"].update(
            persons_way="routes/persons_way.json", needs_city="routes/needs_city.json"))
        d.session.at = "home"
        r = await d.run_tool("c", "open_app", {"app": BUNDLE})
        lines = known(r["text"])
        assert lines and "open_list" in lines[0] and "persons_way" not in lines[0] and "needs_city" not in lines[0]
        assert "persons_way" in d.maps.data(str(tmp_path), BUNDLE)["routes"]  # still drivable by name
        await finish(stop, task)
    run(go)


def test_l3_the_known_way_hint_never_names_a_route_known_would_leave_out(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": BUNDLE})
        await d.run_tool("a", "tap", {"target": {"id": "open-list"}})
        home, lst = saved(tmp_path)["open_list"]["start"], saved(tmp_path)["open_list"]["end"]
        for f in (tmp_path / BUNDLE / "routes").glob("open_list.json"):
            f.unlink()
        d.maps.edit(str(tmp_path), BUNDLE, lambda m: m["routes"].pop("open_list", None))
        person = routelib.route("persons_way", BUNDLE, home, lst, [], PERSONS["typed"])
        d.maps.file(str(tmp_path), BUNDLE).write_route("persons_way", person)
        d.maps.edit(str(tmp_path), BUNDLE, lambda m: m["routes"].__setitem__("persons_way", "routes/persons_way.json"))
        view = d.control.client("a")
        view.place = (BUNDLE, lst)
        assert d._known_name(view) is None
        await finish(stop, task)
    run(go)


# ---- M1: a dropped route never comes back through a save under way ----

def test_m1_a_dropped_route_stays_out_of_the_index(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": BUNDLE})
        await d.run_tool("a", "tap", {"target": {"id": "open-list"}})
        await d.maps.flush()
        root = str(tmp_path)
        file = d.maps.file(root, BUNDLE)
        gate, entered = asyncio.Event(), asyncio.Event()
        real_save = file.save
        loop = asyncio.get_running_loop()

        def slow_save(snapshot):
            loop.call_soon_threadsafe(entered.set)
            asyncio.run_coroutine_threadsafe(gate.wait(), loop).result()
            return real_save(snapshot)
        file.save = slow_save
        d.maps.changed(root, BUNDLE)  # a background save starts with "open_list" in its snapshot
        await entered.wait()
        await d._auto_counted(root, BUNDLE, "open_list", False)
        said = await d._auto_counted(root, BUNDLE, "open_list", False)
        assert said is not None
        gate.set()
        await d.maps.flush()
        index = json.loads((tmp_path / BUNDLE / "map.json").read_text("utf-8"))["routes"]
        assert "open_list" not in index and "open_list" not in d.maps.data(root, BUNDLE)["routes"]
        await finish(stop, task)
    run(go)


# ---- M2: a multiline Done that changed the screen stops the tap ----

def test_m2_a_done_that_changed_the_screen_refuses_stale_and_never_taps_through():
    async def go():
        d, stop, task = await keyboard_up(blank_tap_closes=False, dismiss_ok=False, done_key=True)
        s = d.session
        multiline(s)
        await d.run_tool("a", "look", {"screenshot": False})
        plain = s.dismiss_keyboard

        async def done_submits(keys=()):
            closed = await plain(keys)
            if closed:  # the form's submit ran: Save trip is gone, a new screen comes
                s.tree = [e for e in s.tree if e.identifier not in ("low", "save")]
            return closed
        s.dismiss_keyboard = done_submits
        taps = len(s.tap_points)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "low"}})
        assert e.value.code == "stale" and "Done may have submitted the form" in str(e.value)
        assert (60.0, 620.0) not in s.tap_points[taps:]  # the tap on Save trip never went through
        await finish(stop, task)
    run(go)


# ---- L1: a count under the map's lock, for the version that ran ----

def test_l1_a_count_never_touches_a_persons_route_saved_under_the_same_name(tmp_path):
    file = mapfile.MapFile(str(tmp_path), BUNDLE)
    mine = {**routelib.route("open_list", BUNDLE, "place_1", "place_2", [], [{"tool": "tap", "target": {"id": "a"}}]),
            "auto": True, "used": 0, "failed": 1}
    assert file.write_auto_route("open_list", mine, None)
    stamp = file.route_stamp(file.load(), "open_list")
    person = routelib.route("open_list", BUNDLE, "place_1", "place_2", [], [{"tool": "tap", "target": {"id": "mine"}}])
    file.write_route("open_list", person)  # a person's record under the same name, after the drive read the route
    assert file.auto_result("open_list", stamp, False, autoroute.FAILS, mapfile.now()) is False
    kept = json.loads((tmp_path / BUNDLE / "routes" / "open_list.json").read_text("utf-8"))
    assert kept["steps"][0]["target"]["id"] == "mine" and "auto" not in kept

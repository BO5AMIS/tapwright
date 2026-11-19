"""1.9.0, what the phone check found (W186, 2026-11-19): a route that saved itself kept the form as it came in (the detail
screen still drawn beside it), so its replay arrived on the form and was judged elsewhere, and dropped; and the
`Known:` line was spent on the "Loading from Metro…" screen. Each test fails on 85d7746."""
import json
import shutil
from pathlib import Path

import pytest
from mapfakes import APP, MapSession, screen
from test_daemon import finish, make, run, started
from test_tokens_190 import saved

from tapwright import driving, mapkeeper, places, trip, ui
from tapwright.control import Refused

BUNDLE = APP[1]
REAL = Path(__file__).parent / "data" / "wayfare-map-w186.json"  # the phone check's map, as it was left (ids only)
FORM_IDS = ("BackButton", "trip-departure-minus", "trip-departure-plus", "trip-departure-value", "trip-dest-name",
            "trip-form", "trip-nights", "trip-notes", "trip-reminder", "trip-return-minus", "trip-return-plus",
            "trip-return-value", "trip-save", "trip-travellers-minus", "trip-travellers-plus", "trip-travellers-value")


def form_tree(ids, bundle="com.bo5amis.tapwright.wayfare"):
    """The form as the phone shows it settled: a navigation bar and its controls, all on screen."""
    from xml.etree import ElementTree as ET

    def node(kind, **attrs):
        return ET.Element(f"XCUIElementType{kind}", {"type": f"XCUIElementType{kind}", "enabled": "true",
                                                      **{k: str(v) for k, v in attrs.items()}})
    app = node("Application", bundleId=bundle, label="Wayfare", x=0, y=0, width=400, height=800)
    app.append(node("NavigationBar", name="Plan a trip", x=0, y=40, width=400, height=60))
    for n, i in enumerate(ids):
        app.append(node("Button", label=f"Label of {i}", name=i, x=10, y=110 + 40 * n, width=200, height=36))
    return ui.read(ET.tostring(app, encoding="unicode"), (400.0, 800.0))


def test_the_real_maps_form_as_it_came_in_and_as_it_settled_are_one_screen(tmp_path):
    """place_3 (the form with the detail screen still drawn: detail-scroll and the detail's rows) and place_4 (the form
    settled, keyboard up): the route's end place_3, read as place_4, is the same screen."""
    folder = tmp_path / "maps" / "com.bo5amis.tapwright.wayfare"
    folder.mkdir(parents=True)
    shutil.copy(REAL, folder / "map.json")

    async def go():
        d = make()
        d.maps = mapkeeper.Maps(str(tmp_path / "maps"))
        made = trip.Trip(d, "a", None, driving.Request(None, "detail_plan_trip", {}, None, None, "any", False), {})
        made.root, made.bundle = str(tmp_path / "maps"), "com.bo5amis.tapwright.wayfare"
        settled = form_tree(FORM_IDS)
        assert made.same_screen("place_3", settled, "place_1")
        explore = form_tree(("explore-greeting", "explore-list", "explore-search", "explore-title"))
        assert not made.same_screen("place_3", explore, "place_1")
    run(go)


def test_the_keyboards_own_ids_are_never_part_of_a_place():
    plain = form_tree(("trip-save", "trip-notes", "trip-form"))
    typing = form_tree(("trip-save", "trip-notes", "trip-form", "dictation", "inputView", "com.apple.text.grabber.leading",
                        "_UITextSelectionRangeAdjustmentContainerView"))
    assert places.parts_of(plain)["ids"] == places.parts_of(typing)["ids"]


# a fake app where the form is first read while the detail screen is still drawn beside it

DETAIL = ("plan-trip", "to-list", "detail-scroll", "d1", "d2")
FORM = ("trip-save", "trip-notes", "trip-form", "trip-nights", "trip-dest-name", "BackButton")


def app_screens():
    return {"home": screen("Home", ids=("open-list", "h1", "h2"), tab="home"),
            "list": screen("Explore", ids=("dest-card-kyoto", "to-home", "l1"), tab="home"),
            "detail": form_tree(DETAIL, BUNDLE),
            "coming": form_tree((*FORM, *DETAIL), BUNDLE),  # the form read while the detail is still drawn beside it
            "form": form_tree(FORM, BUNDLE)}


def test_a_route_whose_end_was_read_as_it_came_in_arrives_and_is_kept(tmp_path):
    async def go():
        goes = {("home", "open-list"): "list", ("list", "dest-card-kyoto"): "detail", ("detail", "plan-trip"): "coming"}
        d = make(MapSession(app_screens(), "home", goes))
        d.maps = mapkeeper.Maps(str(tmp_path))
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": BUNDLE})
        for target in ("open-list", "dest-card-kyoto", "plan-trip"):
            await d.run_tool("a", "tap", {"target": {"id": target}})
        recorded = saved(tmp_path)["plan_trip"]["end"]
        d.session.goes[("detail", "plan-trip")] = "form"  # from now on the form is read settled
        for _ in range(3):
            d.session.at = "home"
            await d.run_tool("b", "look", {"screenshot": False})
            r = await d.run_tool("b", "drive", {"route": "plan_trip"})
            assert r["text"].startswith("Arrived at ") and d.session.at == "form"
        kept = saved(tmp_path)["plan_trip"]
        assert kept["failed"] == 0 and kept["used"] == 3 and kept["end"] != recorded  # its end is the settled form now
        assert r["text"].splitlines()[0].startswith(f"Arrived at {kept['end']} ")  # and the replay is judged there
        await finish(stop, task)
    run(go)


# ---- (2) Known isn't spent on the loading screen ----

def test_known_isnt_spent_on_the_loading_screen(tmp_path):
    async def go():
        d = make(MapSession(app_screens(), "home", {("home", "open-list"): "list"}))
        d.maps = mapkeeper.Maps(str(tmp_path))
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": BUNDLE})
        await d.run_tool("a", "tap", {"target": {"id": "open-list"}})  # saves open_list
        view = d.control.client("z")
        loading = screen("", ids=("reload",), texts=("Loading from Metro…",))
        view.app, view.tree, view.place = ("Example", BUNDLE), loading, (BUNDLE, "place_1")
        text = "Screen 400x800 pt\nApp: Example (com.example.app)"
        assert d._with_known(view, text) == text and BUNDLE not in view.known_told
        await finish(stop, task)
    run(go)

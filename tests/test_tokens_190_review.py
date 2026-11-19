"""1.9.0, the first Opus review's fixes (REVIEW-1.9.0-OPUS1: H1-H3, M1-M7, L1-L6), as the leads ruled on them. Each test
fails on a2317b5."""
import argparse
import dataclasses
import json
from xml.etree import ElementTree as ET

import pytest
from fakes import button
from mapfakes import APP, MapSession, screen
from test_daemon import KEYBOARD_NOTE, finish, keyboard_up, make, run, started
from test_tokens_190 import app_logs, multiline, saved, walk_to_form
from test_tokens_190 import mapped as mapped_trip

from tapwright import autoroute, client, mapkeeper, routes as routelib, thin, tokentext, ui
from tapwright.control import Refused

BUNDLE = APP[1]


def form_screens():
    return {"home": screen("Home", ids=("open-form", "h1"), tab="home"),
            "form": screen("Account", ids=("sign-in", "plan-weekly", "f1"), inputs=("email", "password"),
                           labels={"sign-in": "Sign in"}),
            "inside": screen("Inbox", ids=("i1", "i2"))}


def mapped(tmp_path, screens=None, goes=None):
    d = make(MapSession(screens or form_screens(), "home", goes or {("home", "open-form"): "form", ("form", "sign-in"): "inside"}))
    d.maps = mapkeeper.Maps(str(tmp_path))
    return d


# ---- H1: typing in a run, and a form's submit, never make a route ----

def test_h1_a_form_filled_in_a_run_then_saved_makes_no_route(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": BUNDLE})
        await d.run_tool("a", "tap", {"target": {"id": "open-form"}})
        await d.run_tool("a", "run", {"steps": [
            {"tool": "type_text", "target": {"id": "email"}, "text": "me@example.com"},
            {"tool": "type_text", "target": {"id": "password"}, "text": "hunter2"}]})
        await d.run_tool("a", "tap", {"target": {"id": "sign-in"}})
        assert set(saved(tmp_path)) == {"open_form"}
        await finish(stop, task)
    run(go)


def test_h1_a_submit_tap_that_leaves_a_form_is_never_saved_even_with_nothing_typed(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": BUNDLE})
        await d.run_tool("a", "tap", {"target": {"id": "open-form"}})
        await d.run_tool("a", "tap", {"target": {"id": "sign-in"}})  # a form with defaults: Sign in submits it
        assert "sign_in" not in saved(tmp_path)
        await finish(stop, task)
    run(go)


def test_h1_typing_with_look_false_breaks_the_way_too(tmp_path):
    async def go():
        # (CodeRabbit, PR #22) a screen with one input and a plain way on: the same walk with and without the typing
        screens = {"home": screen("Home", ids=("open-form", "h1"), tab="home"),
                   "form": screen("Search", ids=("go-on", "f1"), inputs=("query",)),
                   "inside": screen("Results", ids=("i1", "i2"))}
        for typed in (False, True):
            folder = tmp_path / ("typed" if typed else "plain")
            d = mapped(folder, screens, {("home", "open-form"): "form", ("form", "go-on"): "inside"})
            stop, task = await started(d)
            await d.run_tool("a", "open_app", {"app": BUNDLE})
            await d.run_tool("a", "tap", {"target": {"id": "open-form"}})
            if typed:
                await d.run_tool("a", "type_text", {"target": {"id": "query"}, "text": "x", "look": False})
                await d.run_tool("a", "look", {"screenshot": False})
            await d.run_tool("a", "tap", {"target": {"id": "go-on"}})
            assert d.session.at == "inside"
            assert ("go_on" in saved(folder)) is (not typed)
            await finish(stop, task)
    run(go)


def test_h1_a_run_of_navigation_only_still_counts_as_nothing_typed():
    assert autoroute.only_navigation([{"tool": "tap", "target": {"id": "a"}}, {"tool": "scroll", "to": "end"}])
    assert not autoroute.only_navigation([{"tool": "type_text", "text": "x"}])
    assert not autoroute.only_navigation([{"tool": "toggle", "target": {"id": "s"}}])
    assert "run" not in autoroute.NEUTRAL


# ---- H2: a person's route is carried in only when it is plain navigation ----

def search_screens():
    return {"home": screen("Home", ids=("open-list", "h1"), tab="home", inputs=("search",)),
            "list": screen("Results", ids=("open-detail", "l1")),
            "detail": screen("Detail", ids=("d1", "d2")),
            "lost": screen("Elsewhere", ids=("z1",), texts=("never seen",))}


SEARCH_GOES = {("home", "open-list"): "list", ("list", "open-detail"): "detail"}


def test_h2_a_persons_typed_text_never_goes_into_a_route_of_the_tools(tmp_path):
    async def go():
        d = mapped(tmp_path, search_screens(), SEARCH_GOES)
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": BUNDLE})
        await d.run_tool("a", "tap", {"target": {"id": "open-list"}})
        data = d.maps.data(str(tmp_path), BUNDLE)
        places = list(data["places"])
        person = routelib.route("my_search", BUNDLE, places[0], places[1], [], [
            {"tool": "type_text", "target": {"id": "search"}, "text": "private words"},
            {"tool": "tap", "target": {"id": "open-list"}}])
        d.maps.file(str(tmp_path), BUNDLE).write_route("my_search", person)
        d.maps.edit(str(tmp_path), BUNDLE, lambda m: m["routes"].__setitem__("my_search", "routes/my_search.json"))
        for f in (tmp_path / BUNDLE / "routes").glob("open_list.json"):
            f.unlink()
        d.maps.edit(str(tmp_path), BUNDLE, lambda m: m["routes"].pop("open_list", None))
        d.session.at = "home"
        await d.run_tool("a", "open_app", {"app": BUNDLE})
        await d.run_tool("a", "drive", {"route": "my_search"})
        await d.run_tool("a", "tap", {"target": {"id": "open-detail"}})
        autos = {k: v for k, v in saved(tmp_path).items() if v.get("auto")}
        assert "private words" not in json.dumps(autos) and "open_detail" not in autos
        await finish(stop, task)
    run(go)


# ---- H3: not reaching a route's start never counts against it ----

def test_h3_drives_from_a_screen_with_no_way_to_the_start_keep_the_route(tmp_path):
    async def go():
        d = mapped(tmp_path, search_screens(), SEARCH_GOES)
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": BUNDLE})
        await d.run_tool("a", "tap", {"target": {"id": "open-list"}})
        await d.run_tool("a", "tap", {"target": {"id": "open-detail"}})
        assert "open_detail" in saved(tmp_path)
        for n in (1, 2, 3):
            d.session.at = "lost"
            await d.run_tool(f"c{n}", "look", {"screenshot": False})
            with pytest.raises(Refused) as e:
                await d.run_tool(f"c{n}", "drive", {"route": "open_detail"})
            assert "dropped" not in str(e.value)
        assert "open_detail" in saved(tmp_path) and saved(tmp_path)["open_detail"]["failed"] == 0
        await finish(stop, task)
    run(go)


# ---- M1: a name the redaction would change is left out ----

def test_m1_a_known_name_the_redaction_changes_is_left_out(tmp_path):
    async def go():
        d = mapped_trip(tmp_path)
        stop, task = await started(d)
        await walk_to_form(d)
        d.scrub_everywhere = lambda text: text.replace("plan_trip", "<tester2's username>")
        d.session.at = "home"
        r = await d.run_tool("b", "open_app", {"app": BUNDLE})
        known = [line for line in r["text"].splitlines() if line.startswith("Known:")]
        assert known and "plan_trip" not in known[0] and "tester2" not in known[0] and "open_list" in known[0]
        await finish(stop, task)
    run(go)


# ---- M2: nothing to fill ----

def test_m2_a_route_whose_way_needs_a_links_value_is_left_out(tmp_path):
    async def go():
        d = mapped(tmp_path, search_screens(), SEARCH_GOES)
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": BUNDLE})
        await d.run_tool("a", "tap", {"target": {"id": "open-list"}})
        start = saved(tmp_path)["open_list"]["start"]
        d.session.at = "lost"
        d.session.goes[("lost", "app://home")] = "home"  # the link does lead home
        r = await d.run_tool("b", "look", {"screenshot": False})
        assert "Known:" not in r["text"]  # no way from here at all

        def link(value):  # (through the map's own edit: the map in memory is replaced by its background saves)
            d.maps.edit(str(tmp_path), BUNDLE, lambda m: m["places"][start].__setitem__("link", value))
        link("app://home/[id]")  # a way only by a link with a value to fill (the map's own form)
        r = await d.run_tool("c", "look", {"screenshot": False})
        assert "Known:" not in r["text"]
        link("app://home")  # a link with nothing to fill reaches it
        r = await d.run_tool("e", "look", {"screenshot": False})
        assert any(line.startswith("Known: open_list") for line in r["text"].splitlines())
        r = await d.run_tool("e", "drive", {"route": "open_list"})  # and the drive it names gets there, given nothing
        assert r["text"].startswith("Arrived at ") and d.session.at == "list"
        await finish(stop, task)
    run(go)


def test_m2_a_route_with_a_link_to_fill_or_a_variable_is_never_known():
    assert not autoroute.given_nothing({"variables": ["account"], "steps": []})
    assert not autoroute.given_nothing({"variables": [], "steps": [{"tool": "open_url", "url": "app://trip/[id]"}]})
    assert autoroute.given_nothing({"variables": [], "steps": [{"tool": "open_url", "url": "app://trips"}]})


# ---- M3: no hint on a pause or any safety refusal ----

@pytest.mark.parametrize("code", ["paused", "not_allowed"])
def test_m3_no_hint_on_a_safety_refusal(tmp_path, code):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        view = d.control.client("a")
        view.calls = 25
        app_logs(d, "error")
        said = "paused: paused by the user in the live view; ask them to resume"
        assert d._with_hints(view, "tap", {}, said, code) == said
        await finish(stop, task)
    run(go)


# ---- M4: --no-hints is 1.8.5's behaviour ----

def test_m4_no_hints_saves_no_route_shows_none_and_counts_none(tmp_path):
    async def go():
        d = mapped_trip(tmp_path)
        stop, task = await started(d)
        await walk_to_form(d, hints=False)
        assert saved(tmp_path) == {}
        d.session.at = "home"
        await walk_to_form(d)  # hints on: routes save themselves
        assert "plan_trip" in saved(tmp_path)
        r = await d.run_tool("x", "map", {}, hints=False)
        assert "plan_trip" not in r["text"] and "0 routes" in r["text"]
        d.session.at = "home"
        with pytest.raises(Refused) as e:  # as 1.8.5: no such route
            await d.run_tool("x", "drive", {"route": "plan_trip"}, hints=False)
        assert e.value.code == "not_found" and "plan_trip" not in str(e.value).split("the routes are:")[-1]
        await finish(stop, task)
    run(go)


def test_m4_no_hints_presses_no_done():
    async def go():
        d, stop, task = await keyboard_up(blank_tap_closes=False, dismiss_ok=False, done_key=True)
        s = d.session
        multiline(s)
        await d.run_tool("a", "look", {"screenshot": False}, hints=False)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "low"}}, hints=False)
        assert str(e.value).endswith("type_text into the field with submit: true closes it (it presses return).")  # 1.8.5's
        assert all(not keys for keys in getattr(s, "dismiss_keys", []))
        await finish(stop, task)
    run(go)


# ---- M5: every path ----

def test_m5_the_thin_call_takes_no_hints_and_the_environment(monkeypatch):
    a = thin._parser().parse_args(["look", "--no-hints"])
    assert thin._headers(a, "t")["X-Tapwright-Hints"] == "off"
    monkeypatch.setenv("TAPWRIGHT_NO_HINTS", "1")
    assert thin._headers(thin._parser().parse_args(["look"]), "t")["X-Tapwright-Hints"] == "off"
    monkeypatch.delenv("TAPWRIGHT_NO_HINTS")
    assert "X-Tapwright-Hints" not in thin._headers(thin._parser().parse_args(["look"]), "t")


def test_m5_the_mcp_server_honours_the_environment_and_the_scope_keeps_the_header(monkeypatch):
    from tapwright import descriptions, mcp_server
    monkeypatch.setenv("TAPWRIGHT_NO_HINTS", "1")
    try:
        mcp_server.set_hints(True)
        client.set_scope(["com.example.app"])
        assert client.SCOPE.get("X-Tapwright-Hints") == "off"
        assert mcp_server.server.instructions == descriptions.INSTRUCTIONS
    finally:
        monkeypatch.delenv("TAPWRIGHT_NO_HINTS")
        mcp_server.set_hints(True)
        client.SCOPE.clear()


def test_m5_the_command_lines_own_calls_get_no_hints(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        for _ in range(tokentext.LONG_TASK_CALLS + 1):
            r = await d.run_tool("cli-replay", "look", {"screenshot": False})
            assert "Hint:" not in r["text"]
        await finish(stop, task)
    run(go)


# ---- M6: an accessory Done is the keyboard's ----

def node(kind, **attrs):
    return ET.Element(f"XCUIElementType{kind}", {"type": f"XCUIElementType{kind}", "enabled": "true",
                                                  **{k: str(v) for k, v in attrs.items()}})


def test_m6_only_a_done_in_the_keyboards_window_is_by_the_keyboard():
    app = node("Application", bundleId=BUNDLE, label="Example", x=0, y=0, width=400, height=800)
    win = node("Window", x=0, y=0, width=400, height=800)
    sheet_done = node("Button", label="Done", name="sheet-done", x=20, y=458, width=360, height=40)
    win.append(sheet_done)
    kwin = node("Window", x=0, y=0, width=400, height=800)
    host = node("Other", x=0, y=0, width=400, height=800)
    bar = node("Toolbar", x=0, y=456, width=400, height=44)
    bar.append(node("Button", label="Done", name="Done", x=330, y=458, width=60, height=42))
    holder = node("Other", x=0, y=500, width=400, height=300)
    holder.append(node("Keyboard", x=0, y=500, width=400, height=300))
    host.extend([bar, holder])
    kwin.append(host)
    app.extend([win, kwin])
    tree = ui.read(ET.tostring(app, encoding="unicode"), (400.0, 800.0))
    dones = {e.identifier or e.name: e.by_keyboard for e in tree.items if e.label == "Done"}
    assert dones.get("sheet-done") is False and True in dones.values()


def test_m6_a_sheets_own_done_is_never_pressed():
    async def go():
        d, stop, task = await keyboard_up(blank_tap_closes=False, dismiss_ok=False)
        s = d.session
        s.tree = [*s.tree, button("e9", "Done", "sheet-done", rect=(20, 460, 360, 40))]  # a sheet's footer, on the keys' edge
        await d.run_tool("a", "look", {"screenshot": False})
        taps = len(s.tap_points)
        with pytest.raises(Refused):
            await d.run_tool("a", "tap", {"target": {"id": "low"}})
        assert (200.0, 480.0) not in s.tap_points[taps:]
        await finish(stop, task)
    run(go)


def test_m6_an_accessory_done_off_the_keys_edge_is_not_the_keyboards():
    # (the edge is measured where the tree is read, against the keys' top: CodeRabbit, PR #22 cycle 2)
    app = node("Application", bundleId=BUNDLE, label="Example", x=0, y=0, width=400, height=800)
    app.append(node("Window", x=0, y=0, width=400, height=800))
    kwin = node("Window", x=0, y=0, width=400, height=800)
    host = node("Other", x=0, y=0, width=400, height=800)
    bar = node("Toolbar", x=0, y=436, width=400, height=44)  # ends 20 pt above the keys
    bar.append(node("Button", label="Done", name="Done", x=330, y=438, width=60, height=40))
    holder = node("Other", x=0, y=500, width=400, height=300)
    holder.append(node("Keyboard", x=0, y=500, width=400, height=300))
    host.extend([bar, holder])
    kwin.append(host)
    app.append(kwin)
    tree = ui.read(ET.tostring(app, encoding="unicode"), (400.0, 800.0))
    assert [e.by_keyboard for e in tree.items if e.label == "Done"] == [False]


# ---- M7: an empty field gets no Done ----

def test_m7_a_field_whose_value_cant_be_read_gets_no_done():
    async def go():
        d, stop, task = await keyboard_up(blank_tap_closes=False, dismiss_ok=False, done_key=True)
        s = d.session
        multiline(s, value="")
        await d.run_tool("a", "look", {"screenshot": False})
        with pytest.raises(Refused):
            await d.run_tool("a", "tap", {"target": {"id": "low"}})
        assert ("Done",) not in s.dismiss_keys
        await finish(stop, task)
    run(go)


# ---- L1: a tap with no visible change breaks the way ----

def test_l1_a_selection_with_no_visible_change_breaks_the_way(tmp_path):
    async def go():
        d = mapped(tmp_path, {**form_screens(), "form": screen("Choose", ids=("next-step", "plan-weekly", "f1"))},
                   {("home", "open-form"): "form", ("form", "next-step"): "inside"})
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": BUNDLE})
        await d.run_tool("a", "tap", {"target": {"id": "open-form"}})
        r = await d.run_tool("a", "tap", {"target": {"id": "plan-weekly"}})  # a selection: stays
        await d.run_tool("a", "tap", {"target": {"id": "next-step"}})
        assert "next_step" not in saved(tmp_path), r["text"]
        await finish(stop, task)
    run(go)


# ---- L2: a Done that failed still lets the plain dismissal try ----

def test_l2_a_failed_done_falls_back_to_the_plain_dismissal():
    async def go():
        d, stop, task = await keyboard_up(blank_tap_closes=False, dismiss_ok=False)
        s = d.session
        multiline(s)
        await d.run_tool("a", "look", {"screenshot": False})
        asked = []

        async def dismissal(keys=()):
            asked.append(tuple(keys))
            if keys:
                return False
            s.last_keyboard = False
            s.tree = [dataclasses.replace(e, covered_by=()) if e.identifier == "low" else e for e in s.tree]
            return True
        s.dismiss_keyboard = dismissal
        r = await d.run_tool("a", "tap", {"target": {"id": "low"}})
        assert asked == [("Done",), ()] and r["text"].endswith(KEYBOARD_NOTE)
        await finish(stop, task)
    run(go)


# ---- L3: one route per place, a cap per app ----

def test_l3_one_route_per_place_and_a_cap():
    other_start = autoroute.Held("plan_trip", "elsewhere", "form", 3, True, 0, False)
    assert autoroute.decide([other_start], "home", "form", [{}] * 3, "x", {"plan_trip"}) is None  # the place has one
    autos = [{"name": f"r{i}", "used": i % 3, "reached": f"2026-11-19T10:00:{i:02d}Z"} for i in range(60)]
    gone = autoroute.over_cap(autos, 50)
    assert len(gone) == 10 and all(r["used"] == 0 for r in autos if r["name"] in gone)
    assert autoroute.MAX_AUTO == 50


# ---- L6: Known isn't spent where the place isn't known ----

def test_l6_known_is_not_spent_on_an_answer_whose_place_is_unknown(tmp_path):
    async def go():
        d = mapped_trip(tmp_path)
        stop, task = await started(d)
        await walk_to_form(d)
        view = d.control.client("z")
        view.app, view.place = ("Example", BUNDLE), None
        text = "Screen 400x800 pt\nApp: Example (com.example.app)"
        assert d._with_known(view, text) == text and BUNDLE not in view.known_told
        await finish(stop, task)
    run(go)


# ---- a journey in a user's eyes has no Known: (frozen §30.2) ----

def test_a_users_eyes_journey_gets_no_known_line(tmp_path, monkeypatch):
    from tapwright import journey
    monkeypatch.setattr(journey, "reports_dir", lambda: tmp_path / "reports")

    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": BUNDLE})
        await d.run_tool("a", "tap", {"target": {"id": "open-form"}})
        d.session.at = "home"
        r = await d.run_tool("b", "journey", {"start": True, "goal": "sign in"})
        assert not any(line.startswith(("Known:", "Hint:")) for line in r["text"].splitlines())
        await finish(stop, task)
    run(go)

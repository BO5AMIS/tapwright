"""Opus's last review of PR #20 (1.8.0, "the Explorer"), R180b, fixed by W175: a toggle's flip with no clean answer stops the run
(M1), an old map's place matched only by shared chrome doesn't absorb a new screen (M2), the thin call waits as long as the MCP
server and the CLI (M3), and the Lows. Each test fails on c37e11d."""
import asyncio

from explorefakes import FakePhone, build, grid
from tapwright import explore, explorereport
from tapwright.control import Refused
from tapwright.explore import Moved


def explore_run(explorer, maps=None) -> explore.Result:
    async def go() -> explore.Result:
        result = await explorer.run()
        if maps is not None:
            await maps.flush()
        return result
    return asyncio.run(go())


def _prefs() -> tuple[dict, str, dict, dict]:
    screens = {"home": grid("Home", buttons=(("go-prefs", "Prefs"),)),
               "prefs": grid("Prefs", switches=(("notify-switch", "Notifications"),), back="Home")}
    return screens, "home", {("home", "go-prefs"): "prefs"}, {"prefs": "home"}


def _value(phone: FakePhone) -> bool | None:
    return next(e.checked for e in phone.screens["prefs"].items if e.identifier == "notify-switch")


# ---- M1: a toggle's flip that gets no clean answer stops the run naming the switch ----

def test_a_flip_that_times_out_after_flipping_stops_the_run_and_names_the_switch(tmp_path):
    class Late(FakePhone):
        async def toggle(self, target, value):
            await super().toggle(target, value)  # the touch went out and the switch moved...
            return Moved("timeout", settled=10.0)  # ...but the answer came too late
    phone = Late(*_prefs())
    explorer, maps = build(tmp_path, phone)
    result = explore_run(explorer, maps)
    assert result.stopped == "toggle_not_restored"
    assert result.detail == "id=notify-switch, may be flipped, was off"
    assert explorereport.stop_line(result) == "a toggle could not be set back (id=notify-switch, may be flipped, was off)."
    assert any("may be flipped" in n and "timeout" in n for n in result.notes)
    assert [v for _, _, v in phone.toggles] == [True]  # nothing else is touched to hide it


def test_a_flip_whose_line_drops_or_errors_stops_the_run(tmp_path):
    for code in ("not_answering", "internal", "reconnecting"):
        class Dropped(FakePhone):
            async def toggle(self, target, value, code=code):
                return Moved(code)
        phone = Dropped(*_prefs())
        explorer, maps = build(tmp_path / code, phone)
        result = explore_run(explorer, maps)
        assert result.stopped == "toggle_not_restored", code
        assert result.detail == "id=notify-switch, may be flipped, was off", code

    class Covered(FakePhone):  # (a refusal before the touch went out still goes on: nothing was flipped)
        async def toggle(self, target, value):
            return Moved("not_actionable", "covered")
    explorer, maps = build(tmp_path / "covered", Covered(*_prefs()))
    assert explore_run(explorer, maps).stopped == "frontier_empty"


def test_a_screen_that_cant_be_read_after_the_flip_names_the_switch(tmp_path):
    class Unread(FakePhone):
        async def read(self):
            if self.toggles:
                raise Refused("reconnecting", "the phone didn't answer; try again")
            return await super().read()
    phone = Unread(*_prefs())
    explorer, maps = build(tmp_path, phone)
    result = explore_run(explorer, maps)
    assert result.stopped == "toggle_not_restored" and result.detail == "id=notify-switch, was off"
    assert any("couldn't be set back (reconnecting)" in n for n in result.notes)
    assert _value(phone) is True


def test_a_restoring_touch_that_raises_a_refusal_names_the_switch(tmp_path):
    class Breaks(FakePhone):
        async def toggle(self, target, value):
            if self.toggles:
                raise Refused("internal", "boom")
            return await super().toggle(target, value)
    phone = Breaks(*_prefs())
    explorer, maps = build(tmp_path, phone)
    result = explore_run(explorer, maps)
    assert result.stopped == "toggle_not_restored" and result.detail == "id=notify-switch, was off"


# ---- M2: a place of a map saved before 1.8.0 matched only by the tab bar doesn't absorb a new screen ----

def _tab_bar(selected: str = "explore"):
    """Wayfare's tab bar as the phone shows it (kinds `tabs`, the selected tab `tab-explore`): three buttons in a TabBar."""
    from test_places_scroll import node
    return node("TabBar", rect=(0, 769, 393, 83), children=[
        node("Button", t.title(), name=f"tab-{t}", rect=(10 + 128 * i, 775, 120, 60), selected="true" if t == selected else "false",
             **({"traits": "Button,Selected"} if t == selected else {"traits": "Button"}))
        for i, t in enumerate(("explore", "trips", "settings"))])


def _wayfare_explore(rows_scrolled: int = 0, search_inside: bool = False):
    """Wayfare's Explore tab with its cards in one ScrollView without an identifier (as an RN screen whose content is all
    in one unnamed ScrollView); the search field above the list, or (`search_inside`) scrolling with it."""
    from test_places_scroll import CARD_H, CITIES, LIST_TOP, app, card, node
    cards = [card(c, LIST_TOP + i * CARD_H - rows_scrolled * CARD_H) for i, c in enumerate(CITIES)]
    search = node("SearchField", "Search destinations", name="explore-search", rect=(20, 134, 353, 50))
    scroll = node("ScrollView", rect=(0, 0, 393, 769), children=[search, *cards] if search_inside else cards)
    return app(scroll, _tab_bar()) if search_inside else app(search, scroll, _tab_bar())


def _wayfare_detail():
    """A destination's detail pushed in the Explore tab: the tab bar stays, everything else is in one unnamed ScrollView."""
    from test_places_scroll import app, node
    return app(node("ScrollView", rect=(0, 0, 393, 769), children=[
        node("StaticText", "Kyoto", name="detail-title", rect=(20, 320, 300, 36)),
        node("StaticText", "Temples and gardens.", name="detail-description", rect=(20, 440, 353, 120)),
        node("Button", "Plan a trip", name="detail-plan", rect=(20, 700, 353, 54))]), _tab_bar())


def _old_map(*trees):
    """A map as a release before 1.8.0 saved it: every id on screen (rows too), no `lists` or `inner`."""
    from tapwright import places
    data = places.empty_map("com.example.wayfare")
    for name, tree in trees:
        parts = {"ids": list(tree.structure.ids), "kinds": list(tree.structure.kinds), "tab": places._tab(tree), "layer": None}
        data["places"][name] = {"key": {"signature": places.signature(parts), "parts": parts}, "link": None, "titles": {},
                                "last": "2026-11-10T09:00:00Z"}
    return data


def test_a_detail_never_mapped_is_not_absorbed_by_an_old_list_place_through_the_tab_bar():
    from tapwright import places
    data = _old_map(("place_12", _wayfare_explore()))
    assert {"tab-explore", "tab-trips", "tab-settings", "dest-card-kyoto"} <= set(data["places"]["place_12"]["key"]["parts"]["ids"])
    read = places.read_place(_wayfare_detail())
    assert set(read.key["parts"]["ids"]) == {"tab-explore", "tab-trips", "tab-settings"}  # only the tab bar is fixed
    assert places.locate(data, read) == (None, 0.0)
    v = places.visit(data, read, "2026-11-19T10:00:00Z")
    assert v.new and v.name != "place_12"
    # the old place is still found from its own screen, scrolled or not, and with its search scrolling with the cards
    for tree in (_wayfare_explore(0), _wayfare_explore(3), _wayfare_explore(5, search_inside=True)):
        assert places.locate(data, places.read_place(tree))[0] == "place_12"


def test_an_old_map_that_holds_both_tells_the_detail_from_the_list():
    from tapwright import places
    data = _old_map(("place_12", _wayfare_explore()), ("place_13", _wayfare_detail()))
    data["places"]["place_12"]["last"] = "2026-11-11T09:00:00Z"  # (the newer: a tie would pick it)
    assert places.locate(data, places.read_place(_wayfare_detail())) == ("place_13", 1.0)
    assert places.locate(data, places.read_place(_wayfare_explore(4)))[0] == "place_12"


# ---- L2: two reads with no list don't merge on the tab bar's ids when their kinds and tabs differ ----

def test_two_screens_with_no_list_and_other_kinds_and_tabs_are_two_places():
    from tapwright import places
    bar = ["tab-a", "tab-b", "tab-c"]
    a = {"ids": bar, "lists": [], "inner": [], "kinds": ["nav", "tabs"], "tab": "tab-a", "layer": None}
    b = {"ids": bar, "lists": [], "inner": [], "kinds": ["tabs"], "tab": "tab-b", "layer": None}
    assert not places.same_place(a, b)
    assert places.similarity(a, a) == 1.0
    # as on a map saved before 1.8.0: ids and layer alone are no place, one of kinds or tab more is
    old = {"ids": bar, "kinds": ["nav", "tabs"], "tab": "tab-a", "layer": None}
    assert not places.same_place(old, {**old, "kinds": ["tabs"], "tab": "tab-b"})
    assert places.same_place(old, {**old, "tab": "tab-b"}) and places.same_place(old, {**old, "kinds": ["tabs"]})


def test_a_tab_bar_that_isnt_a_tabbar_is_chrome_by_being_on_most_old_places():
    """The same with the tab bar as three plain buttons in a view (no TabBar, so no `tab` role): its ids are on most of the
    old map's places, so they alone don't match the detail to the Explore list."""
    from test_places_scroll import app, node, settings, tab_bar, trips
    from tapwright import places
    from test_places_scroll import CARD_H, CITIES, LIST_TOP, card
    cards = [card(c, LIST_TOP + i * CARD_H) for i, c in enumerate(CITIES)]
    explore_list = app(node("ScrollView", rect=(0, 0, 393, 769), children=cards), tab_bar("explore"))
    detail = app(node("ScrollView", rect=(0, 0, 393, 769), children=[
        node("StaticText", "Kyoto", name="detail-title", rect=(20, 320, 300, 36)),
        node("Button", "Plan a trip", name="detail-plan", rect=(20, 700, 353, 54))]), tab_bar("explore"))
    data = _old_map(("place_12", explore_list), ("place_1", trips()), ("place_2", settings()))
    assert {"tab-explore", "tab-trips", "tab-settings"} <= places.shared_ids(data)
    assert places.read_place(detail).chrome == frozenset()  # (nothing marks the bar as one)
    assert places.locate(data, places.read_place(detail)) == (None, 0.0)
    assert places.locate(data, places.read_place(explore_list))[0] == "place_12"
    assert places.locate(data, places.read_place(trips()))[0] == "place_1"
    assert places.locate(data, places.read_place(settings()))[0] == "place_2"


# ---- M3: the thin call waits for explore and a repair's apply as long as the MCP server does; --policy reaches the floor ----

from test_thin import fake, home, run  # noqa: E402,F401 - the fake daemon and its fixtures


def test_the_thin_call_waits_per_tool_as_the_mcp_server_does():
    import json as _json
    from tapwright import mcp_server, server, thin
    cases = [("explore", {}), ("explore", {"minutes": 2}), ("explore", {"minutes": float("nan")}), ("explore", {"minutes": True}),
             ("replay", {"repair": "apply"}), ("replay", {"repair": "suggest"}), ("replay", {}), ("run", {}), ("drive", {}),
             ("media", {}), ("tap", {}), ("look", {}), ("journey", {"stop": True, "export": {}}), ("journey", {"stop": True})]
    for tool, args in cases:
        assert thin.default_timeout(tool, args) == mcp_server._timeout(tool, args), (tool, _json.dumps(args))
    assert thin.default_timeout("explore", {}) == 10 * 60 + 60 + 5 * 60
    assert thin.default_timeout("replay", {"repair": "apply"}) > 120
    assert thin.FLOOR_TOOLS == server.FLOOR_TOOLS
    assert [thin.floor_call(*c) for c in (("explore", {}), ("replay", {"repair": "apply"}), ("replay", {}), ("tap", {}))] == \
        [True, True, False, False]


def test_a_thin_calls_policy_reaches_explores_floor_and_nothing_else(fake, capsys, tmp_path):
    import json as _json
    pol = tmp_path / "policy.json"
    pol.write_text('{\n  "explore": {"skip": ["Archive"]}\n}', encoding="utf-8")
    for tool, body in (("explore", "{}"), ("replay", '{"path": "a.json", "repair": "apply"}'), ("look", "{}"),
                       ("replay", '{"path": "a.json"}')):
        code, _, err = run(capsys, tool, body, "--apps", "a.b", "--policy", str(pol))
        assert code == 0, (tool, err)
    sent = [h["headers"].get("x-tapwright-policy") for h in fake.seen]
    assert [_json.loads(s) if s else None for s in sent] == [{"explore": {"skip": ["Archive"]}}] * 2 + [None, None]
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    code, _, err = run(capsys, "explore", "{}", "--apps", "a.b", "--policy", str(bad))
    assert code == 2 and "isn't JSON" in err and len(fake.seen) == 4  # a skip list that can't be read is never lost silently


# ---- L1: a repair's proven road is taps only ----

def test_a_repairs_proven_road_never_holds_a_toggle_or_a_long_press(tmp_path, monkeypatch):
    import types
    from test_daemon_repair import mapped
    from tapwright import roads
    d = mapped(tmp_path)
    d._may_map = lambda bundle: True
    monkeypatch.setattr(d.maps, "data", lambda root, bundle: {})
    for tool, kept in (("toggle", False), ("long_press", False), ("tap", True)):
        road = {"move": {"tool": tool, "target": {"id": "notify-switch"}}, "names": {"id": "notify-switch"}}
        plan = types.SimpleNamespace(kind="roads", proven=True, routes=(), roads=[road])
        monkeypatch.setattr(roads, "plan", lambda *a, plan=plan, **k: plan)
        got = d._proven_taps("root", "com.example.app", "a", "b", "1")
        assert (got is not None) == kept, tool


# ---- L3: --adopt installs exactly the bytes the run checked ----

def test_adopt_installs_the_checked_bytes_and_the_files_have_lf_line_ends(tmp_path):
    from tapwright import scriptrepair
    script = tmp_path / "sign_in.json"
    script.write_bytes(b'{"app": "old"}\n')
    raw = b'{\n  "format": "agent-tools-script",\n  "app": "com.example.app"\n}\n'
    (tmp_path / "sign_in.repaired.json").write_bytes(raw)
    ok, line = scriptrepair.adopt(script, lambda detail: True, scriptrepair.digest(raw))
    assert ok, line
    assert script.read_bytes() == raw
    names = scriptrepair.write_files(tmp_path / "other.json", {"app": "x", "steps": []}, "# Repair\n\nline\n")
    for name in names:
        assert b"\r\n" not in (tmp_path / name).read_bytes(), name


# ---- L4: a dry run that holds a touch without naming a word is refused for this build, not skipped for every build ----

def test_a_dry_run_hold_without_a_word_is_not_a_permanent_skip(tmp_path):
    from explorefakes import standard
    from tapwright.explore import Check

    class Held(FakePhone):
        async def check(self, target, tool="tap"):
            el = self._element(target)
            key = el.identifier or el.label
            self.checked.append(key)
            if key == "go-about":
                return Check("needs_person", None)  # a sheet seen for a moment, another app in front: no word
            if key == "go-settings":
                return Check("needs_person", "Delete")  # the floor's own word holds it
            return Check()
    screens, goes, back_to = standard()
    phone = Held(screens, "home", goes, back_to)
    explorer, maps = build(tmp_path, phone)
    result = explore_run(explorer, maps)
    skipped = {s.sig for s in result.skipped}
    assert "id:go-settings" in skipped and "id:go-about" not in skipped
    assert ("home", "go-about") not in phone.touched and ("home", "go-settings") not in phone.touched


# ---- L5: the check replay drives to a route's start without the floor (the script's own start) ----

def test_the_check_replay_drives_to_a_routes_start_outside_the_floor(tmp_path, monkeypatch):
    from test_daemon_repair import mapped
    from tapwright import daemon, explorefloor, policy
    floor = explorefloor.Floor(policy.DEFAULT, "com.example.app")
    seen: list = []

    class Trip:
        def __init__(self, *a, **k):
            pass

        async def reach(self):
            seen.append(daemon._TOUCH_FLOOR.get())
    monkeypatch.setattr(daemon.trip, "Trip", Trip)
    d = mapped(tmp_path)
    data = {"format": "agent-tools-script", "version": 2, "name": "r", "start": "welcome", "fresh": False,
            "steps": [{"tool": "tap", "target": {"id": "continue-btn"}}]}

    async def go():
        gate = daemon._TOUCH_FLOOR.set(floor)
        only = daemon._FLOOR_REPAIRED_ONLY.set(True)
        try:
            await d._replay_prepare("a", dict(data), None)
        finally:
            daemon._FLOOR_REPAIRED_ONLY.reset(only)
        try:
            await d._replay_prepare("a", dict(data), None)  # (a trial's own floor, not the check's: held as before)
            assert daemon._TOUCH_FLOOR.get() is floor
        finally:
            daemon._TOUCH_FLOOR.reset(gate)
    asyncio.run(go())
    assert seen == [None, floor]


# ---- L7: a repair's floor app is the script's own, not whatever was in front ----

def test_a_repairs_floor_app_is_the_scripts_app(tmp_path):
    from test_daemon_repair import mapped
    d = mapped(tmp_path)
    got: dict = {}

    async def prepare(client_id, data, scope):
        return [], []

    async def apply(client_id, data, steps, notes, scope, args, path, bundle, drafts_only):
        got["bundle"] = bundle
        return {"text": "ok"}
    d._replay_prepare = prepare
    d._apply = apply
    d._bundle = lambda: "com.apple.springboard"  # a permission prompt in front after the fresh start
    data = {"format": "agent-tools-script", "version": 1, "name": "s", "app": "com.example.app", "fresh": True, "steps": []}
    asyncio.run(d._repair_replay("a", data, None, {}, tmp_path / "s.json", "apply", False))
    assert got["bundle"] == "com.example.app"

"""1.9.0, round 2 (OPTIMIZE-1.9-ROUND2): the core tools loaded with the session and trimmed schemas, the keyboard told
rather than fought, route memory that gets used, the failed-call fixes, the form hint that fires, and the trimmed
instructions with an app-only `logs`."""
import asyncio
import json

import pytest
from mcp.types import ListToolsResult

from tapwright import mcp_server


def wire_tools() -> dict:
    """tools/list as a host receives it (by alias: `_meta`, `inputSchema`)."""
    listed = asyncio.run(mcp_server.server.list_tools())
    wire = ListToolsResult(tools=listed).model_dump(by_alias=True, exclude_none=True, mode="json")
    return {t["name"]: t for t in wire["tools"]}


@pytest.fixture
def eager():
    mcp_server.set_lazy_tools(False)
    mcp_server.set_hints(True)
    yield
    mcp_server.set_lazy_tools(False)
    mcp_server.set_hints(True)


# ---- item 1: the core tools load with the session; schemas without titles ----

def test_the_core_tools_carry_always_load_and_the_rest_stay_deferred(eager):
    tools = wire_tools()
    marked = {name for name, t in tools.items() if (t.get("_meta") or {}).get("anthropic/alwaysLoad") is True}
    assert marked == {"open_app", "open_url", "look", "tap", "type_text", "run", "scroll", "back", "toggle", "wait_for",
                      "drive"}
    assert set(mcp_server.CORE_TOOLS) == marked


def test_the_listed_schemas_have_no_title_keys_but_keep_everything_else(eager):
    tools = wire_tools()
    text = json.dumps({n: t["inputSchema"] for n, t in tools.items()})
    assert '"title"' not in text
    tap = tools["tap"]["inputSchema"]
    assert tap["required"] == ["target"] and tap["properties"]["force"] == {"default": False, "type": "boolean"}
    target = tap["$defs"]["Target"]
    assert set(target["properties"]) == {"ref", "id", "label", "text", "x", "y", "index"}
    assert len(target["description"]) < 90
    # the server's own copy is untouched (so --lazy-tools lists 1.8.5's schemas)
    assert "title" in mcp_server.server._tool_manager.get_tool("tap").parameters["properties"]["force"]


def test_a_property_named_title_is_kept():
    schema = {"title": "X", "properties": {"title": {"title": "Title", "type": "string"}, "a": {"title": "A"}}}
    mcp_server._drop_titles(schema)
    assert schema == {"properties": {"title": {"type": "string"}, "a": {}}}


@pytest.mark.parametrize("how", ["flag", "env", "no_hints"])
def test_lazy_tools_or_no_hints_list_every_tool_deferred_as_before(eager, monkeypatch, how):
    if how == "flag":
        mcp_server.set_lazy_tools(True)
    elif how == "env":
        monkeypatch.setenv("TAPWRIGHT_LAZY_TOOLS", "1")
        mcp_server.set_lazy_tools(False)
    else:
        mcp_server.set_hints(False)
    tools = wire_tools()
    assert not any("_meta" in t for t in tools.values())
    assert '"title"' in json.dumps(tools["tap"]["inputSchema"])
    monkeypatch.delenv("TAPWRIGHT_LAZY_TOOLS", raising=False)
    mcp_server.set_lazy_tools(False)
    mcp_server.set_hints(True)
    assert "_meta" in wire_tools()["tap"]


def test_the_command_line_takes_lazy_tools(monkeypatch):
    import sys
    from tapwright import cli
    seen = {}
    monkeypatch.setattr(mcp_server, "run", lambda **kw: seen.update(kw))
    monkeypatch.setattr(sys, "argv", ["tapwright", "mcp", "--lazy-tools"])
    cli.main()
    assert seen["lazy_tools"] is True
    monkeypatch.setattr(sys, "argv", ["tapwright", "mcp"])
    cli.main()
    assert seen["lazy_tools"] is False


# ---- item 2: the keyboard told, not fought ----

from test_daemon import finish, keyboard_up, run  # noqa: E402

from tapwright import daemon as daemon_module  # noqa: E402


def test_the_keyboard_line_names_what_it_covers_and_that_a_tap_there_closes_it_first():
    async def go():
        d, stop, task = await keyboard_up()
        shown = "Screen 400x800 pt · 6 elements\nKeyboard: shown\ne1 text \"Title\""
        d.session.SCREEN_TEXT = shown
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert "Keyboard: shown (covers low; a tap on it closes the keyboard first)" in r["text"].splitlines()
        plain = await d.run_tool("a", "look", {"screenshot": False}, hints=False)  # --no-hints: 1.8.5's line
        assert "Keyboard: shown" in plain["text"].splitlines() and "covers" not in plain["text"]
        view = d.control.client("a")
        many = d._with_keyboard(view, "Keyboard: shown")
        assert many == "Keyboard: shown (covers low; a tap on it closes the keyboard first)"
        await finish(stop, task)
    run(go)


def test_a_keyboard_that_stays_up_over_a_tap_it_doesnt_cover_is_said_plainly_and_plain_text_is_no_finding():
    async def go():
        d, stop, task = await keyboard_up(blank_tap_closes=False, dismiss_ok=False)
        r = await d.run_tool("a", "tap", {"target": {"text": "Title"}})
        text = r["text"]
        assert "Note: " + daemon_module.NOTE_KEYBOARD_STAYS in text
        assert daemon_module.NOTE_KEYBOARD_STAYED not in text
        assert "report it as a finding" not in text
        assert daemon_module.HINT_PLAIN_TEXT in text
        await finish(stop, task)
    run(go)


def test_no_hints_keeps_1_8_5s_keyboard_and_finding_notes():
    async def go():
        d, stop, task = await keyboard_up(blank_tap_closes=False, dismiss_ok=False)
        r = await d.run_tool("a", "tap", {"target": {"text": "Title"}}, hints=False)
        text = r["text"]
        assert "Note: " + daemon_module.NOTE_KEYBOARD_STAYED in text
        assert daemon_module.NOTE_KEYBOARD_STAYS not in text and "report it as a finding" in text
        await finish(stop, task)
    run(go)


# ---- item 3: route memory that gets used ----

from mapfakes import APP, screen  # noqa: E402
from test_daemon import started  # noqa: E402
from test_tokens_190 import mapped, saved  # noqa: E402

from tapwright import autoroute, tokentext  # noqa: E402
from tapwright.control import Refused  # noqa: E402

BUNDLE = APP[1]


def with_search(d, extra_ids=()):
    """The list screen gets a search field (as Wayfare's Explore)."""
    d.session.screens["list"] = screen("Explore", ids=(*extra_ids, "dest-card-kyoto", "dest-card-lisbon", "to-home", "l1"),
                                       tab="home", inputs=("explore-search",))
    d.session.goes[("list", "dest-card-lisbon")] = "detail"


def test_the_way_begins_at_a_first_look_of_an_app_already_in_front(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})  # no open_app: the app is already in front
        await d.run_tool("a", "tap", {"target": {"id": "open-list"}})
        await d.run_tool("a", "tap", {"target": {"id": "dest-card-kyoto"}})
        assert {"open_list", "dest_card_kyoto"} <= set(saved(tmp_path))
        await finish(stop, task)
    run(go)


def test_the_way_begins_at_the_first_screen_an_open_url_shows(tmp_path):
    async def go():
        d = mapped(tmp_path)
        d.session.goes[("home", "example://explore")] = "list"
        stop, task = await started(d)
        await d.run_tool("a", "open_url", {"url": "example://explore"})
        await d.run_tool("a", "tap", {"target": {"id": "dest-card-kyoto"}})
        await d.run_tool("a", "tap", {"target": {"id": "plan-trip"}})
        kept = saved(tmp_path)
        assert "plan_trip" in kept
        assert [s["target"]["id"] for s in kept["plan_trip"]["steps"]] == ["dest-card-kyoto", "plan-trip"]
        await finish(stop, task)
    run(go)


def test_a_result_tapped_after_a_search_is_kept_as_scroll_to_it_and_tap_never_the_typed_text(tmp_path):
    async def go():
        d = mapped(tmp_path)
        with_search(d)
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": BUNDLE})
        await d.run_tool("a", "tap", {"target": {"id": "open-list"}})
        await d.run_tool("a", "type_text", {"text": "Lisbon secret", "target": {"id": "explore-search"}})
        await d.run_tool("a", "tap", {"target": {"id": "dest-card-lisbon"}})
        await d.run_tool("a", "tap", {"target": {"id": "plan-trip"}})
        kept = saved(tmp_path)
        route = kept["dest_card_lisbon"]
        assert route["steps"] == [{"tool": "tap", "target": {"id": "open-list"}},
                                  {"tool": "scroll", "to": "target", "target": {"id": "dest-card-lisbon"}},
                                  {"tool": "tap", "target": {"id": "dest-card-lisbon"}}]
        assert route["derived"] is True and route["auto"] is True
        assert kept["plan_trip"]["derived"] is True  # the way on from it was made from the search too
        assert "secret" not in json.dumps(kept) and "Lisbon" not in json.dumps(kept)
        await finish(stop, task)
    run(go)


def test_a_result_that_wasnt_in_the_list_before_the_search_breaks_the_way(tmp_path):
    async def go():
        d = mapped(tmp_path)
        with_search(d)
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": BUNDLE})
        await d.run_tool("a", "tap", {"target": {"id": "open-list"}})
        await d.run_tool("a", "type_text", {"text": "Paris", "target": {"id": "explore-search"}})
        with_search(d, extra_ids=("dest-card-paris",))  # a server-side search brought a card the list didn't hold
        d.session.goes[("list", "dest-card-paris")] = "detail"
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "tap", {"target": {"id": "dest-card-paris"}})
        assert set(saved(tmp_path)) == {"open_list"}
        await finish(stop, task)
    run(go)


def test_typing_into_a_field_that_isnt_a_search_still_breaks_the_way(tmp_path):
    async def go():
        d = mapped(tmp_path)
        d.session.screens["list"] = screen("Explore", ids=("dest-card-kyoto", "to-home", "l1"), tab="home",
                                           inputs=("name",))
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": BUNDLE})
        await d.run_tool("a", "tap", {"target": {"id": "open-list"}})
        await d.run_tool("a", "type_text", {"text": "Kyoto", "target": {"id": "name"}})
        await d.run_tool("a", "tap", {"target": {"id": "dest-card-kyoto"}})
        assert set(saved(tmp_path)) == {"open_list"}
        await finish(stop, task)
    run(go)


def test_a_route_made_from_a_search_is_dropped_after_one_failed_replay(tmp_path):
    async def go():
        d = mapped(tmp_path)
        with_search(d)
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": BUNDLE})
        await d.run_tool("a", "tap", {"target": {"id": "open-list"}})
        await d.run_tool("a", "type_text", {"text": "Lis", "target": {"id": "explore-search"}})
        await d.run_tool("a", "tap", {"target": {"id": "dest-card-lisbon"}})
        assert "dest_card_lisbon" in saved(tmp_path)
        d.session.screens["list"] = screen("Explore", ids=("dest-card-kyoto", "to-home", "l1"), tab="home",
                                           inputs=("explore-search",))  # Lisbon is gone from the list
        d.session.at = "home"
        await d.run_tool("a", "look", {"screenshot": False})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "drive", {"route": "dest_card_lisbon"})
        assert "dropped (its replay failed; it was made from a search)" in str(e.value)
        assert "dest_card_lisbon" not in saved(tmp_path)
        await finish(stop, task)
    run(go)


def test_routes_are_kept_per_target_not_per_place(tmp_path):
    async def go():
        d = mapped(tmp_path)
        with_search(d)
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": BUNDLE})
        await d.run_tool("a", "tap", {"target": {"id": "open-list"}})
        await d.run_tool("a", "tap", {"target": {"id": "dest-card-kyoto"}})
        await d.run_tool("a", "tap", {"target": {"id": "to-list"}})
        await d.run_tool("a", "tap", {"target": {"id": "dest-card-lisbon"}})
        kept = saved(tmp_path)
        assert {"dest_card_kyoto", "dest_card_lisbon"} <= set(kept)  # both reach the detail place, by different cards
        assert kept["dest_card_kyoto"]["end"] == kept["dest_card_lisbon"]["end"]
        await finish(stop, task)
    run(go)


def test_known_shows_each_routes_way_in_ids_and_map_lists_it_too(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": BUNDLE})
        await d.run_tool("a", "tap", {"target": {"id": "open-list"}})
        await d.run_tool("a", "scroll", {"to": "target", "target": {"id": "dest-card-kyoto"}})
        await d.run_tool("a", "tap", {"target": {"id": "dest-card-kyoto"}})
        d.session.at = "home"
        r = await d.run_tool("b", "open_app", {"app": BUNDLE})
        known = next(line for line in r["text"].splitlines() if line.startswith("Known: "))
        assert "dest_card_kyoto (tap open-list, scroll to dest-card-kyoto, tap)" in known
        assert "open_list (tap open-list)" in known and "; " in known
        assert known.endswith('(one call each: drive {"route": "<name>"})')
        m = await d.run_tool("c", "map", {})
        assert any(line.startswith("Known: ") and "dest_card_kyoto (" in line for line in m["text"].splitlines())
        plain = await d.run_tool("e", "map", {}, hints=False)
        assert "Known:" not in plain["text"]
        await finish(stop, task)
    run(go)


def test_the_way_text_is_ids_only_and_short():
    assert autoroute.way_text([{"tool": "scroll", "to": "target", "target": {"id": "dest-card-lisbon"}},
                               {"tool": "tap", "target": {"id": "dest-card-lisbon"}}]) == "scroll to dest-card-lisbon, tap"
    assert autoroute.way_text([{"tool": "tap", "target": {"label": "Chat with Sara"}}, {"tool": "back"},
                               {"tool": "scroll", "to": "end"}, {"tool": "open_url", "url": "app://x"}]) == \
        "tap, back, scroll to end, open link"
    long = [{"tool": "tap", "target": {"id": f"s{n}"}} for n in range(9)]
    assert autoroute.way_text(long) == "tap s0, tap s1, +4 more, tap s6, tap s7, tap s8"
    assert autoroute.way_text([]) is None
    assert tokentext.known_line([("a", "tap x"), ("b", None)]) == 'Known: a (tap x); b (one call each: drive {"route": "<name>"})'


def test_the_target_key_tells_cards_apart():
    assert autoroute.target_key([{"tool": "tap", "target": {"id": "a"}}]) == "id:a"
    assert autoroute.target_key([{"tool": "back"}]) == '{"tool": "back"}'
    kyoto = autoroute.Held("dest_card_kyoto", "home", "detail", 2, True, 0, False, "id:dest-card-kyoto")
    assert autoroute.decide([kyoto], "home", "detail", [{"tool": "tap", "target": {"id": "dest-card-lisbon"}}],
                            "dest_card_lisbon", {"dest_card_kyoto"}) == "dest_card_lisbon"
    assert autoroute.decide([kyoto], "home", "detail", [{"tool": "tap", "target": {"id": "dest-card-kyoto"}}] * 2,
                            "dest_card_kyoto", {"dest_card_kyoto"}) is None  # the same card: one route, not shorter


def test_a_search_that_filters_the_list_still_keeps_the_result_by_its_id(tmp_path):
    async def go():
        d = mapped(tmp_path)
        with_search(d)
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": BUNDLE})
        await d.run_tool("a", "tap", {"target": {"id": "open-list"}})
        # the typing filters the list: only Lisbon's card is left
        d.session.screens["list"] = screen("Explore", ids=("dest-card-lisbon", "to-home"), tab="home",
                                           inputs=("explore-search",))
        await d.run_tool("a", "type_text", {"text": "Lis", "target": {"id": "explore-search"}})
        await d.run_tool("a", "tap", {"target": {"id": "dest-card-lisbon"}})
        kept = saved(tmp_path)
        assert kept["dest_card_lisbon"]["steps"][-2:] == [
            {"tool": "scroll", "to": "target", "target": {"id": "dest-card-lisbon"}},
            {"tool": "tap", "target": {"id": "dest-card-lisbon"}}]
        await finish(stop, task)
    run(go)

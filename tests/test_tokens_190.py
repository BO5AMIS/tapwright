"""1.9.0, "the Token Update" (AGENT-TOOLS 1.17 §30): routes that save themselves, the `Known:` line, the hints, the
token rules in the instructions, and `--no-hints`, which gives 1.8.5's answers and instructions back."""
import dataclasses
import hashlib
import json

import pytest
from mapfakes import APP, MapSession, screen
from test_daemon import finish, make, run, started

from tapwright import autoroute, descriptions, mapkeeper, tokentext, ui
from tapwright.ceiling import Ceiling
from tapwright.control import Refused

BUNDLE = APP[1]
GOES = {("home", "open-list"): "list", ("list", "dest-card-kyoto"): "detail", ("detail", "plan-trip"): "form",
        ("form", "to-detail"): "detail", ("list", "to-home"): "home", ("detail", "to-list"): "list"}


def screens():
    return {"home": screen("Home", ids=("open-list", "h1", "h2"), tab="home"),
            "list": screen("Explore", ids=("dest-card-kyoto", "to-home", "l1"), tab="home"),
            "detail": screen("Kyoto", ids=("plan-trip", "to-list", "d1", "d2")),
            "form": screen("Plan a trip", ids=("trip-save", "to-detail", "f1"), inputs=("trip-notes", "trip-name"))}


def mapped(tmp_path, **options):
    session = MapSession(screens(), "home", GOES)
    d = make(session, **options)
    d.maps = mapkeeper.Maps(str(tmp_path))
    return d


def routes_dir(tmp_path):
    return tmp_path / BUNDLE / "routes"


def saved(tmp_path) -> dict:
    folder = routes_dir(tmp_path)
    return {p.stem: json.loads(p.read_text("utf-8")) for p in folder.glob("*.json")} if folder.is_dir() else {}


async def walk_to_form(d, hints=None):
    extra = {} if hints is None else {"hints": hints}
    answers = [await d.run_tool("a", "open_app", {"app": BUNDLE}, **extra)]
    for target in ("open-list", "dest-card-kyoto", "plan-trip"):
        answers.append(await d.run_tool("a", "tap", {"target": {"id": target}}, **extra))
    return answers


# ---- the shared words ----

def test_the_instructions_begin_with_the_token_rules_and_no_hints_gives_1_8_5s_word_for_word():
    with_rules = descriptions.instructions(True)
    assert with_rules.startswith("Working well with this tool (fewer calls, fewer tokens):\n")
    assert with_rules == tokentext.RULES + "\n" + descriptions.INSTRUCTIONS
    assert 'reachable in one call (drive {"route": "<name>"})' in with_rules
    assert "A bug report is `bug_report`, not a file you write." in with_rules
    assert ("- Ask for a screenshot only to judge how something looks (`screenshot: false` on a `look` that only reads); "
            "the text answer is enough to act.") in with_rules
    # 1.8.5's instructions, unchanged (sha256 of 1.8.5's text)
    assert descriptions.instructions(False) == descriptions.INSTRUCTIONS
    assert hashlib.sha256(descriptions.INSTRUCTIONS.encode()).hexdigest() == INSTRUCTIONS_185


INSTRUCTIONS_185 = "cec34a040cb9cb22ab1611d6a0e039e97a489c7689f5b783ef9056207bd3bc7a"  # tapwright 1.8.5's, at ba3780b


def test_the_known_line_has_at_most_eight_names_then_more():
    assert tokentext.known_line([]) is None
    assert tokentext.known_line(["a", "b"]) == 'Known: a, b (one call each: drive {"route": "<name>"})'
    line = tokentext.known_line([f"p{i}" for i in range(11)])
    assert line.startswith("Known: p0, p1, p2, p3, p4, p5, p6, p7, +3 more (")


def test_the_mcp_server_takes_no_hints(monkeypatch):
    from tapwright import client, mcp_server
    try:
        mcp_server.set_hints(False)
        assert mcp_server.server.instructions == descriptions.INSTRUCTIONS
        assert client.SCOPE.get("X-Tapwright-Hints") == "off"
        mcp_server.set_hints(True)
        assert mcp_server.server.instructions.startswith("Working well with this tool")
        assert "X-Tapwright-Hints" not in client.SCOPE
    finally:
        mcp_server.set_hints(True)


def test_the_command_line_has_no_hints_on_mcp_and_serve(monkeypatch):
    import sys
    from tapwright import cli, mcp_server
    seen = {}
    monkeypatch.setattr(mcp_server, "run", lambda **kw: seen.update(kw))
    monkeypatch.setattr(sys, "argv", ["tapwright", "mcp", "--no-hints"])
    cli.main()
    assert seen["hints"] is False
    monkeypatch.setattr(sys, "argv", ["tapwright", "mcp"])
    cli.main()
    assert seen["hints"] is True
    assert Ceiling(no_hints=True).is_set


# ---- the chain, as pure functions ----

def test_a_chain_keeps_navigation_and_cuts_loops():
    c = autoroute.Chain("app")
    c.begin("home")
    assert c.moved("list", {"tool": "tap", "target": {"id": "open-list"}}) == [{"tool": "tap", "target": {"id": "open-list"}}]
    c.scrolled({"tool": "scroll", "to": "end"})
    way = c.moved("detail", {"tool": "tap", "target": {"id": "dest-card-kyoto"}})
    assert [s["tool"] for s in way] == ["tap", "scroll", "tap"]
    assert c.moved("list", {"tool": "back"}) is None and c.places == ["home", "list"]  # back on the chain: the loop is cut
    assert c.moved("detail", {"tool": "tap", "target": {"id": "dest-card-kyoto"}}) == [
        {"tool": "tap", "target": {"id": "open-list"}}, {"tool": "tap", "target": {"id": "dest-card-kyoto"}}]


def test_typing_or_a_tap_that_stays_breaks_the_way_from_there():
    c = autoroute.Chain("app")
    c.begin("list")
    c.taint()  # typed into the search field here
    assert c.moved("detail", {"tool": "tap", "target": {"id": "dest-card-lisbon"}}) is None
    assert c.moved("list", {"tool": "back"}) is None and c.broken_at == 0  # what was typed at the list is still there
    d = autoroute.Chain("app")
    d.begin("form")
    assert d.moved("form", {"tool": "tap", "target": {"id": "plus"}}) is None and d.broken_at == 0  # a stepper's value
    e = autoroute.Chain("app")
    e.begin("a")
    e.moved("b", {"tool": "tap", "target": {"id": "x"}})
    e.taint()  # broken at b ...
    e.moved("a", {"tool": "back"})  # ... and back at a, before it: clean again
    assert e.broken_at is None and e.moved("c", {"tool": "tap", "target": {"id": "y"}}) == [{"tool": "tap", "target": {"id": "y"}}]


def test_only_steps_that_replay_and_keep_nothing_private_are_steps():
    assert autoroute.step_of({"tool": "tap", "target": {"id": "go", "index": 1}}) == {"tool": "tap", "target": {"id": "go", "index": 1}}
    assert autoroute.step_of({"tool": "tap", "target": {"row": True}}) is None
    assert autoroute.step_of({"tool": "tap", "target": {"x": 1, "y": 2}}) is None
    assert autoroute.step_of({"tool": "toggle", "target": {"id": "s"}}) is None
    assert autoroute.step_of({"tool": "open_url", "link": "app://item/{id}"}) is None
    assert autoroute.step_of({"tool": "open_url", "link": "app://settings"}) == {"tool": "open_url", "url": "app://settings"}
    assert autoroute.scroll_step({"to": "target", "target": ui.Target(id="dest-card-kyoto")}) == {
        "tool": "scroll", "to": "target", "target": {"id": "dest-card-kyoto"}}
    assert autoroute.scroll_step({"to": "target", "target": ui.Target(text="Reykjavik")}) is None  # words are content
    assert autoroute.scroll_step({"direction": "down", "amount": 0.5}) == {"tool": "scroll", "direction": "down", "amount": 0.5}


def test_the_shortest_way_is_kept_and_a_persons_route_is_never_replaced():
    mine = autoroute.Held("plan_trip", "home", "form", 3, True, 0, False)
    assert autoroute.decide([mine], "home", "form", [{}] * 3, "x", {"plan_trip"}) is None  # not shorter, still works
    assert autoroute.decide([mine], "home", "form", [{}] * 2, "x", {"plan_trip"}) == "plan_trip"  # shorter: replaces it
    failing = autoroute.Held("plan_trip", "home", "form", 3, True, 1, False)
    assert autoroute.decide([failing], "home", "form", [{}] * 3, "x", set()) == "plan_trip"
    person = autoroute.Held("my_way", "home", "form", 5, False, 0, False)
    assert autoroute.decide([person], "home", "form", [{}], "x", {"my_way"}) is None
    assert autoroute.decide([], "home", "form", [{}], "Sign_In".lower(), {"SIGN_IN"}) == "sign_in_2"  # never a case twin


# ---- through the daemon ----

def test_reaching_a_place_by_navigation_saves_its_route_by_itself(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        answers = await walk_to_form(d)
        kept = saved(tmp_path)
        assert set(kept) == {"open_list", "dest_card_kyoto", "plan_trip"}  # each by the id that reached it
        route = kept["plan_trip"]
        assert route["auto"] is True and route["variables"] == [] and route["format"] == "agent-tools-script"
        assert [s["target"]["id"] for s in route["steps"]] == ["open-list", "dest-card-kyoto", "plan-trip"]
        assert route["start"] != route["end"]
        assert "Kyoto" not in json.dumps(route) and "Plan a trip" not in json.dumps(route)  # never a title
        assert not any("Saved" in a["text"] or "route" in a["text"] for a in answers)  # nothing said: no tokens spent
        await finish(stop, task)
    run(go)


def test_typed_text_and_what_comes_after_it_is_never_saved(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": BUNDLE})
        await d.run_tool("a", "tap", {"target": {"id": "open-list"}})
        d.session.screens["list"] = screen("Explore", ids=("dest-card-kyoto", "to-home", "l1"), tab="home",
                                           inputs=("search",))
        await d.run_tool("a", "type_text", {"text": "secret words", "target": {"id": "search"}})
        await d.run_tool("a", "tap", {"target": {"id": "dest-card-kyoto"}})
        assert set(saved(tmp_path)) == {"open_list"}
        assert "secret words" not in json.dumps(saved(tmp_path))
        await finish(stop, task)
    run(go)


def test_one_call_replays_it_and_the_next_session_is_told(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await walk_to_form(d)
        d.session.at = "home"
        r = await d.run_tool("b", "open_app", {"app": BUNDLE})  # a new session (another client)
        known = [line for line in r["text"].splitlines() if line.startswith("Known: ")]
        assert len(known) == 1 and "plan_trip" in known[0] and known[0].endswith('(one call each: drive {"route": "<name>"})')
        lines = r["text"].splitlines()
        assert lines.index(known[0]) > next(i for i, line in enumerate(lines) if line.startswith("Screen "))
        r = await d.run_tool("b", "drive", {"route": "plan_trip"})
        assert r["text"].startswith("Arrived at ") and d.session.at == "form"
        assert saved(tmp_path)["plan_trip"]["used"] == 1
        again = await d.run_tool("b", "look", {"screenshot": False})
        assert "Known:" not in again["text"]  # the first answer only
        await finish(stop, task)
    run(go)


def test_a_route_whose_replay_fails_twice_is_dropped(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await walk_to_form(d)
        del d.session.goes[("detail", "plan-trip")]  # the app changed: the button no longer opens the form
        for n in (1, 2):
            d.session.at = "home"
            await d.run_tool("a", "look", {"screenshot": False})
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "drive", {"route": "plan_trip"})
            if n == 1:
                assert saved(tmp_path)["plan_trip"]["failed"] == 1
        assert "route plan_trip dropped (its replay failed twice)" in str(e.value)
        assert "plan_trip" not in saved(tmp_path)
        assert "plan_trip" not in d.maps.data(str(tmp_path), BUNDLE)["routes"]
        await finish(stop, task)
    run(go)


def test_no_hints_gives_the_same_answers_without_known_and_hint_lines(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await walk_to_form(d)
        d.session.at = "home"
        d.control.clients.clear()
        plain = await walk_to_form(d, hints=False)  # (the client's --no-hints)
        for a in plain:
            assert not any(line.startswith(("Known:", "Hint:")) for line in a["text"].splitlines())
        d.session.at = "home"
        d.control.clients.clear()
        hinted = await walk_to_form(d)
        def kept(text, also=()):  # (how long a fake screen took to settle is the machine's, not the answer's)
            return [line for line in text.splitlines() if not line.startswith(("Settled in ", *also))]
        assert any("Known:" in a["text"] for a in hinted) and any("Hint:" in a["text"] for a in hinted)
        assert [kept(a["text"], ("Known:", "Hint:")) for a in hinted] == [kept(a["text"]) for a in plain]
        await finish(stop, task)
    run(go)


def test_serve_no_hints_turns_them_off_for_every_call(tmp_path):
    async def go():
        d = mapped(tmp_path)
        d.ceiling = Ceiling(no_hints=True)
        stop, task = await started(d)
        await walk_to_form(d)
        d.session.at = "home"
        d.control.clients.clear()
        for a in await walk_to_form(d):
            assert "Known:" not in a["text"] and "Hint:" not in a["text"]
        await finish(stop, task)
    run(go)


def test_a_form_gets_its_hint_once(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        answers = await walk_to_form(d)
        assert answers[-1]["text"].splitlines()[-1] == tokentext.HINTS["form"]
        await d.run_tool("a", "tap", {"target": {"id": "to-detail"}})
        r = await d.run_tool("a", "tap", {"target": {"id": "plan-trip"}})
        assert "Hint:" not in r["text"]  # once per session
        await finish(stop, task)
    run(go)


def test_three_taps_along_known_roads_get_the_known_way_hint(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await walk_to_form(d)
        d.session.at = "home"
        d.control.clients.clear()
        answers = await walk_to_form(d)
        hints = [line for a in answers for line in a["text"].splitlines() if line.startswith("Hint:")]
        assert 'Hint: this way is known: `drive {"route": "plan_trip"}` reaches plan_trip in one call.' in hints
        assert len([h for h in hints if "this way is known" in h]) == 1
        await finish(stop, task)
    run(go)


def test_a_screenshot_on_an_action_that_had_its_changes_gets_the_hint(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": BUNDLE})
        r = await d.run_tool("a", "tap", {"target": {"id": "open-list"}, "screenshot": True})
        assert r["text"].splitlines()[-1] == tokentext.HINTS["screenshot"]
        await finish(stop, task)
    run(go)


def test_the_long_task_hint_comes_at_the_20th_call_once_and_is_harmless_to_a_helper(tmp_path):
    assert tokentext.LONG_TASK_CALLS == 20
    assert tokentext.HINTS["long_task"] == ("Hint: on a long task, the main session can hand the driving to one small, fast "
                                            "agent; if you are that agent, carry on.")

    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        texts = [(await d.run_tool("a", "look", {"screenshot": False}))["text"] for _ in range(23)]
        said = [n for n, t in enumerate(texts, 1) if tokentext.HINTS["long_task"] in t]
        assert said == [20]
        await finish(stop, task)
    run(go)


# ---- the logs hint: the app's own errors or a crash, never a tool error ----

def app_logs(d, *levels):
    from tapwright import diagnostics
    ring = d._rings.setdefault(BUNDLE, diagnostics.LogRing())
    for n, level in enumerate(levels):
        ring.add(diagnostics.LogEntry(1_800_000_000_000 + n, level, f"line {n}"))


def test_a_tool_error_or_a_failed_check_without_an_app_error_gets_no_logs_hint(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "nowhere"}})
        assert e.value.code == "not_found" and "Hint:" not in str(e.value)
        app_logs(d, "info", "warning")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": [{"tool": "tap", "target": {"id": "h1"}, "expect": {"text": "Nope"}}]})
        assert e.value.code == "expect_failed" and tokentext.HINTS["logs"] not in str(e.value)
        await finish(stop, task)
    run(go)


def test_an_error_the_app_logged_asks_for_logs_until_they_are_read(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        app_logs(d, "error")
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert r["text"].splitlines()[-1] == tokentext.HINTS["logs"]
        assert tokentext.HINTS["logs"] == "Hint: the app logged an error: `logs` shows it."
        await finish(stop, task)
    run(go)


def test_an_error_from_before_the_session_or_before_logs_were_read_asks_nothing(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        app_logs(d, "error")  # before this session's first answer of the app
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert "Hint:" not in r["text"]
        await d.run_tool("a", "logs", {})
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert "Hint:" not in r["text"]
        await finish(stop, task)
    run(go)


# ---- §30.2 names: never a label or a title ----

def test_a_route_is_named_by_the_given_name_or_the_moves_id_never_its_words():
    data = {"places": {"place_4": {}, "settings": {"named": True}}}
    assert autoroute.base_name(data, "place_4", {"tool": "tap", "target": {"id": "new-trip"}}) == "new_trip"
    assert autoroute.base_name(data, "place_4", {"tool": "tap", "target": {"label": "Chat with Sara"}}) == "place_4"
    assert autoroute.base_name(data, "place_4", {"tool": "tap", "target": {"text": "Kyoto"}}) == "place_4"
    assert autoroute.base_name(data, "place_4", {"tool": "back"}) == "place_4"
    assert autoroute.base_name(data, "settings", {"tool": "tap", "target": {"id": "gear"}}) == "settings"


def test_known_lists_the_most_used_first_else_the_most_recently_reached():
    held = [{"name": "old", "start": "home", "end": "a", "steps": [], "reached": "2026-11-18T10:00:00Z"},
            {"name": "new", "start": "home", "end": "b", "steps": [], "reached": "2026-11-19T10:00:00Z"},
            {"name": "used", "start": "home", "end": "c", "steps": [], "used": 2, "reached": "2026-11-01T10:00:00Z"},
            {"name": "never", "start": "home", "end": "d", "steps": []}]
    assert autoroute.known(held, "home", lambda start: True) == ["used", "new", "old", "never"]


def test_a_place_reached_by_a_label_saves_a_route_named_by_its_place(tmp_path):
    async def go():
        d = mapped(tmp_path)
        d.session.screens["home"] = screen("Home", ids=("h1", "h2"), tab="home", texts=("Open the list",))
        d.session.goes[("home", "Open the list")] = "list"
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": BUNDLE})
        await d.run_tool("a", "tap", {"target": {"text": "Open the list"}})
        kept = saved(tmp_path)
        assert len(kept) == 1 and next(iter(kept)).startswith("place_")
        assert "open_the_list" not in kept
        await finish(stop, task)
    run(go)


# ---- the keyboard over a tap's target (Wayfare's trip form: Save under the keyboard of a multiline Notes) ----

from test_daemon import KEYBOARD_NOTE, keyboard_up  # noqa: E402
from fakes import button  # noqa: E402


def multiline(s, value="Bench trip"):
    """Field A, which has the focus, is a text view (React Native's multiline TextInput) holding `value`."""
    s.tree = [dataclasses.replace(e, type="TextView", value=value) if e.identifier == "a" else e for e in s.tree]


def uncover_low_when_closed(s):
    plain = s.dismiss_keyboard

    async def done_closes(keys=()):
        closed = await plain(keys)
        if closed:  # the keyboard went down: Low is uncovered
            s.tree = [dataclasses.replace(e, covered_by=()) if e.identifier == "low" else e for e in s.tree]
        return closed
    s.dismiss_keyboard = done_closes


def test_in_a_multiline_field_the_keyboards_done_key_closes_it_before_a_covered_target():
    async def go():
        d, stop, task = await keyboard_up(blank_tap_closes=False, dismiss_ok=False, done_key=True)
        s = d.session
        multiline(s)
        await d.run_tool("a", "look", {"screenshot": False})
        uncover_low_when_closed(s)
        r = await d.run_tool("a", "tap", {"target": {"id": "low"}})
        assert s.dismiss_keys == [("Done",)]  # exactly Done: never return, Go, Send, Search, Next or Join
        assert s.tap_points[-1] == (60.0, 620.0)  # then the tap went to Low itself
        assert r["text"].endswith(KEYBOARD_NOTE)
        assert not any(isinstance(c, str) and c.startswith("key:") for c in s.calls)  # nothing typed, nothing submitted
        await finish(stop, task)
    run(go)


def test_in_a_one_line_field_no_key_is_pressed_and_the_tap_is_refused_as_in_1_8_5():
    async def go():
        d, stop, task = await keyboard_up(blank_tap_closes=False, dismiss_ok=False, done_key=True)
        s = d.session
        with pytest.raises(Refused) as e:  # field A is a text field: its Done (return) could submit the form
            await d.run_tool("a", "tap", {"target": {"id": "low"}})
        assert e.value.code == "not_actionable" and str(e.value).startswith("covered:")
        assert all(not keys for keys in s.dismiss_keys)  # no key named: only 1.8.5's plain dismissal
        await finish(stop, task)
    run(go)


def test_an_accessory_done_button_above_the_keyboard_is_tapped_first():
    async def go():
        d, stop, task = await keyboard_up(blank_tap_closes=False, dismiss_ok=False)
        s = d.session
        done = button("e9", "Done", "", rect=(330, 460, 60, 40))  # the keyboard's accessory bar, on its top edge (500)
        done.by_keyboard = True
        s.tree = [*s.tree, done]
        await d.run_tool("a", "look", {"screenshot": False})
        taps = len(s.tap_points)

        async def accessory_closes(target, hold_ms=60):
            s._record("tap")
            s.tap_points.append((target.x, target.y))
            if (target.x, target.y) == (360.0, 480.0):
                s.last_keyboard = False
                s.tree = [dataclasses.replace(e, covered_by=()) if e.identifier == "low" else e for e in s.tree]
            return "tapped"
        s.tap = accessory_closes
        r = await d.run_tool("a", "tap", {"target": {"id": "low"}})
        assert (360.0, 480.0) in s.tap_points[taps:] and r["text"].endswith(KEYBOARD_NOTE)
        assert ("Done",) not in getattr(s, "dismiss_keys", [])  # no key of the keyboard was pressed
        await finish(stop, task)
    run(go)


def test_a_done_key_that_only_added_a_new_line_has_it_taken_back_and_said():
    async def go():
        d, stop, task = await keyboard_up(blank_tap_closes=False, dismiss_ok=False)
        s = d.session
        multiline(s)
        field = next(e for e in s.tree if e.identifier == "a")
        await d.run_tool("a", "look", {"screenshot": False})

        async def done_types_a_line(keys=()):
            s._record("dismiss_keyboard")
            if keys:
                field.value += "\n"  # a multiline field whose Done key is its return: the keyboard stays
            return False
        s.dismiss_keyboard = done_types_a_line

        async def backspace(name):
            s._record(f"key:{name}")
            field.value = field.value[:-1]
            return f"pressed {name}"
        s.key = backspace
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "low"}})
        assert e.value.code == "not_actionable" and str(e.value).startswith("covered:")
        assert "Note: the keyboard's Done key added a line break to" in str(e.value) and "taken back" in str(e.value)
        assert s.calls.count("key:backspace") == 1 and field.value == "Bench trip"
        with pytest.raises(Refused):  # the Done key isn't tried again on this screen
            await d.run_tool("a", "tap", {"target": {"id": "low"}})
        assert s.calls.count("key:backspace") == 1
        await finish(stop, task)
    run(go)


def test_no_backspace_when_the_value_changed_otherwise():
    async def go():
        d, stop, task = await keyboard_up(blank_tap_closes=False, dismiss_ok=False)
        s = d.session
        multiline(s)
        field = next(e for e in s.tree if e.identifier == "a")
        await d.run_tool("a", "look", {"screenshot": False})

        async def done_changes_text(keys=()):
            s._record("dismiss_keyboard")
            field.value = "Something else"
            return False
        s.dismiss_keyboard = done_changes_text
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "low"}})
        assert "key:backspace" not in s.calls and "taken back" not in str(e.value)
        assert "a line break may have been added; check its value." in str(e.value)  # (review 1.9.0 M7) said plainly
        await finish(stop, task)
    run(go)


# ---- round 3: §30.3 as both leads agreed it ----

def test_the_five_hint_sentences_are_the_drafts_word_for_word():
    assert tokentext.HINTS["form"] == "Hint: fill the fields and save in one `run`, with an `expect` on the save step."
    assert tokentext.hint("known_way", name="plan_trip") == (
        'Hint: this way is known: `drive {"route": "plan_trip"}` reaches plan_trip in one call.')
    assert tokentext.HINTS["screenshot"] == ("Hint: the text answer had the changes; ask for a screenshot only to judge how "
                                             "something looks.")
    assert tokentext.HINTS["long_task"] == ("Hint: on a long task, the main session can hand the driving to one small, fast "
                                            "agent; if you are that agent, carry on.")
    assert tokentext.HINTS["logs"] == "Hint: the app logged an error: `logs` shows it."
    assert tokentext.ORDER == ("logs", "known_way", "form", "screenshot", "long_task")


def test_a_form_is_two_inputs_and_a_button():
    from tapwright.daemon import Daemon
    from fakes import el
    two = [el("e1", "input", "A", "a"), el("e2", "input", "B", "b")]
    assert not Daemon._is_form(two)  # no button: nothing to save with
    assert Daemon._is_form([*two, button("e3", "Save", "save")])
    assert not Daemon._is_form([el("e1", "input", "A", "a"), el("e2", "switch", "S", "s"), button("e3", "Save", "save")])


def test_a_screen_of_two_fields_and_no_button_gets_no_form_hint(tmp_path):
    async def go():
        d = mapped(tmp_path)
        d.session.screens["form"] = screen("Plan a trip", inputs=("trip-notes", "trip-name"))
        stop, task = await started(d)
        answers = await walk_to_form(d)
        assert tokentext.HINTS["form"] not in answers[-1]["text"]
        await finish(stop, task)
    run(go)


def test_when_several_apply_logs_comes_first_then_the_rest_in_order(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": BUNDLE})
        for target in ("open-list", "dest-card-kyoto"):
            await d.run_tool("a", "tap", {"target": {"id": target}})
        app_logs(d, "error")
        r = await d.run_tool("a", "tap", {"target": {"id": "plan-trip"}, "screenshot": True})  # a form, a screenshot, an error
        assert r["text"].splitlines()[-1] == tokentext.HINTS["logs"]
        assert sum(line.startswith("Hint:") for line in r["text"].splitlines()) == 1
        await d.run_tool("a", "tap", {"target": {"id": "to-detail"}})
        r = await d.run_tool("a", "tap", {"target": {"id": "plan-trip"}, "screenshot": True})  # the form, then: form first
        assert r["text"].splitlines()[-1] == tokentext.HINTS["form"]
        await finish(stop, task)
    run(go)


def test_no_hint_on_a_safety_refusal(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        view = d.control.client("a")
        app_logs(d, "error")
        held = "needs_confirmation: Delete is on the skip floor; explore and repair touch nothing it holds, and ask nobody."
        assert d._with_hints(view, "tap", {}, held, "not_allowed") == held
        assert "logs" not in view.hints_given  # not spent: the next answer may still carry it
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert r["text"].splitlines()[-1] == tokentext.HINTS["logs"]
        await finish(stop, task)
    run(go)

"""AGENT-TOOLS §17.1, §17.4, §17.5: `journey` (a user's eyes, measuring, stress moments, the report, its export)."""
import base64
import io
import json
import re

import pytest
from fakes import DescribingSession, button, el
from PIL import Image
from test_daemon import finish, make, run, started

from tapwright import journey, journey_export, journey_report, ui
from tapwright.control import Refused
from tapwright.daemon import Daemon, Scope

HIDDEN = "in a journey, ids and labels are hidden; use a ref or the visible text"
HOME = [button("e1", "Log meal", "log-meal"), button("e2", "Settings", "settings", rect=(340, 20, 44, 44)),
        el("e3", "text", "Today")]
FOOD = [el("e1", "header", "Add food"), el("e2", "input", "Search", "search", rect=(10, 100, 300, 44)),
        button("e3", "Save", "save", rect=(10, 600, 100, 44)), el("e4", "text", "Nothing yet")]
OTHER = [el("e1", "header", "Settings"), el("e2", "switch", "Notifications", "notif", checked=False),
         button("e3", "Back", "back")]


@pytest.fixture(autouse=True)
def reports(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "reports_dir", lambda: tmp_path / "reports")
    return tmp_path / "reports"


def daemon(tree=None) -> Daemon:
    session = DescribingSession()
    session.tree = list(tree if tree is not None else HOME)
    return make(session)


def icon_button(ref: str, label: str) -> ui.Element:
    return ui.Element(ref, "button", label, rect=(340, 20, 44, 44), actions=("tap",), type="Button", icon_only=True)


def only_folder(root):
    (folder,) = [p for p in root.iterdir() if p.is_dir()]
    return folder


# ---- the eyes: one filter in ui ----
def test_in_a_users_eyes_a_line_has_no_id_and_an_icon_button_shows_its_label_apart():
    plain, icon = button("e1", "Log in", "login-button"), icon_button("e2", "Settings")
    assert plain.line() == 'e1 button "Log in" id=login-button @10,100 100x40'
    token = ui.USERS_EYES.set(True)
    try:
        assert plain.line() == 'e1 button "Log in" @10,100 100x40'
        assert icon.line() == 'e2 button (icon only) (VoiceOver: "Settings") @340,20 44x44'
        field = el("e3", "input", "Name", "name-field", value="Hint", placeholder="Hint")
        assert field.line() == 'e3 input "Name" placeholder="Hint" @10,100 100x40'  # the placeholder is on the glass
    finally:
        ui.USERS_EYES.reset(token)
    assert icon.line().startswith('e2 button "Settings"')  # not in a journey: as before


def test_the_header_says_more_below_only_where_there_is_more_and_no_counts():
    items = [button("e1", "OK", "ok")]
    assert ui.describe(items, (400, 800), None, None, None, False, 0, (2, 5)).splitlines()[0] == \
        "Screen 400x800 pt · 1 elements (+5 below, +2 above)"
    token = ui.USERS_EYES.set(True)
    try:
        assert ui.describe(items, (400, 800), None, None, None, False, 0, (2, 5)).splitlines()[0] == \
            "Screen 400x800 pt · 1 elements (more below, more above)"
        assert ui.describe(items, (400, 800), None, None, None, False, 0, (0, 0)).splitlines()[0] == \
            "Screen 400x800 pt · 1 elements"
        assert ui.describe(items, (400, 800), None, None, None, False, 7, (0, 0)).splitlines()[-1] == "(more below; scroll)"
    finally:
        ui.USERS_EYES.reset(token)


def test_what_the_keyboard_covers_is_not_on_the_glass():
    keys = ui.Bar("Keyboard", (0, 500, 400, 300), "keyboard")
    hidden, shown = button("e1", "Save", "save", rect=(10, 600, 100, 40)), button("e2", "Cancel", "c", rect=(10, 100, 100, 40))
    hidden.covered_by = (keys,)
    assert ui.on_glass([hidden, shown]) == [shown]
    shown.covered_by = (ui.Bar("TabBar", (0, 700, 400, 100), "tabs"),)  # another bar doesn't hide it
    assert ui.on_glass([shown]) == [shown]


def _source(inner: str) -> str:
    return ('<XCUIElementTypeApplication type="XCUIElementTypeApplication" x="0" y="0" width="400" height="800">'
            f"{inner}</XCUIElementTypeApplication>")


def test_which_buttons_are_icon_only():
    tree = ui.read(_source(
        # an icon with an image inside, a small square one, and two that show words
        '<XCUIElementTypeButton type="XCUIElementTypeButton" label="Settings" x="340" y="20" width="44" height="44">'
        '<XCUIElementTypeImage type="XCUIElementTypeImage" x="350" y="30" width="24" height="24"/></XCUIElementTypeButton>'
        '<XCUIElementTypeButton type="XCUIElementTypeButton" label="Search" x="10" y="20" width="40" height="40"/>'
        '<XCUIElementTypeButton type="XCUIElementTypeButton" label="Save" x="10" y="100" width="120" height="44">'
        '<XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="Save" x="30" y="110" width="60" height="24"/>'
        "</XCUIElementTypeButton>"
        '<XCUIElementTypeButton type="XCUIElementTypeButton" label="Done" x="300" y="100" width="80" height="40"/>'),
        (400, 800))
    by_label = {e.label: e.icon_only for e in tree.items if e.role == "button"}
    assert by_label == {"Settings": True, "Search": True, "Save": False, "Done": False}


def test_a_tab_is_never_an_icon_only_button():
    tree = ui.read(_source(
        '<XCUIElementTypeTabBar type="XCUIElementTypeTabBar" x="0" y="740" width="400" height="60">'
        '<XCUIElementTypeButton type="XCUIElementTypeButton" label="Home" x="0" y="740" width="60" height="50">'
        '<XCUIElementTypeImage type="XCUIElementTypeImage" x="10" y="745" width="24" height="24"/>'
        "</XCUIElementTypeButton></XCUIElementTypeTabBar>"), (400, 800))
    assert [e.icon_only for e in tree.items if e.role == "tab"] == [False]


# ---- the numbers, from the calls ----
def new_journey(tmp_path, **kw) -> journey.Journey:
    import datetime
    return journey.Journey("goal", None, {}, (), "user", 0.0, datetime.datetime(2026, 1, 1), tmp_path, **kw)


def full(*items: tuple[str, str]) -> str:
    return "Screen 400x800 pt · n elements\n" + "\n".join(f'e{i} {role} "{text}"' for i, (role, text) in enumerate(items, 1))


def test_screens_are_told_apart_by_their_elements(tmp_path):
    j = new_journey(tmp_path)
    home, food = full(("text", "Today"), ("button", "Log meal"), ("button", "Settings")), \
        full(("header", "Add food"), ("input", "Search"), ("button", "Save"))
    assert j.see(home) and j.current.index == 1 and j.current.title == "Today"
    assert not j.see(home)  # the same screen again
    assert not j.see("Same screen. Changes:\n~ e3 text")  # a change block is no new screen
    assert j.see(food) and j.current.index == 2 and j.current.title == "Add food"  # a header names it
    assert j.see(home) and j.current.index == 1  # a return, not a third screen
    assert len(j.screens) == 2


def test_taps_typing_scrolls_waits_errors_and_hesitations(tmp_path):
    j = new_journey(tmp_path)
    j.note_call("open_app", {}, full(("button", "Log meal")), 1.0, None)
    j.note_call("look", {}, full(("button", "Log meal")), 0.1, None)
    j.note_call("look", {}, full(("button", "Log meal")), 0.1, None)  # two reads in a row: one hesitation
    j.note_call("look", {}, full(("button", "Log meal")), 0.1, None)  # a third is the same hesitation
    j.note_call("tap", {"target": {"text": "Log meal"}}, "Same screen. Changes:\n+ e5 text", 0.3, None)
    j.note_call("type_text", {"text": "2 eggs"}, "Same screen.", 0.3, None)
    j.note_call("scroll", {}, "Same screen.", 0.3, None)
    j.note_call("wait_for", {"text": "x"}, "Done after 4.0 s.", 4.0, None)
    j.note_call("wait", {"seconds": 2}, "Waited 2.0 s.", 2.0, None)
    j.note_call("tap", {}, "", 0.1, "not_found")
    j.note_call("look", {}, "", 0.1, None)
    j.note_call("tap", {}, "", 0.1, None)
    assert (j.taps, j.typing, j.typed_chars, j.scrolls, j.errors, j.hesitations) == (2, 1, 6, 1, 1, 1)
    assert j.waiting == 6.0


def test_a_screen_left_by_back_without_acting_is_a_wrong_turn_and_a_dead_end_if_it_was_searched(tmp_path):
    j = new_journey(tmp_path)
    home, food, other = (full(("button", "Log meal"), ("text", "Today")), full(("header", "Add food"), ("input", "Search")),
                         full(("header", "Settings"), ("switch", "Notifications")))
    j.note_call("look", {}, home, 0, None)
    j.note_call("tap", {}, food, 0, None)
    j.note_call("back", {}, home, 0, None)  # entered, nothing done there, back: a wrong turn
    assert (j.wrong_turns, j.dead_ends) == (1, 0)
    j.note_call("tap", {}, other, 0, None)
    j.note_call("scroll", {}, "Same screen.", 0, None)
    j.note_call("back", {}, home, 0, None)  # scrolled looking for a way on: a dead end too
    assert (j.wrong_turns, j.dead_ends) == (2, 1)
    j.note_call("tap", {}, food, 0, None)
    j.note_call("type_text", {"text": "a"}, "Same screen.", 0, None)
    j.note_call("back", {}, home, 0, None)  # it did something there: not a wrong turn
    assert j.wrong_turns == 2


def test_the_summary_words(tmp_path):
    j = new_journey(tmp_path)
    j.goal, j.persona = "log breakfast by scanning its barcode", "a first-timer"
    j.taps, j.wrong_turns, j.hesitations, j.waiting = 9, 2, 3, 34
    assert journey.summary_line(j, True, 112) == ('Journey "log breakfast by scanning its barcode" as a first-timer: '
                                                  "reached in 9 taps, 1 min 52 s (34 s waiting), 2 wrong turns, 3 hesitations.")
    j.taps, j.wrong_turns, j.hesitations, j.persona = 1, 1, 1, None
    assert journey.summary_line(j, False, 59) == ('Journey "log breakfast by scanning its barcode": not reached after '
                                                  "1 tap, 59 s (34 s waiting), 1 wrong turn, 1 hesitation.")
    assert journey.duration(120) == "2 min"


def test_findings_are_checked():
    for bad in ({}, {"severity": "urgent", "what": "x", "basis": "y"}, {"severity": "high", "basis": "y"},
                {"severity": "high", "what": "x"}, {"severity": "high", "what": "x", "basis": "y", "color": 1},
                {"severity": "high", "what": "x", "basis": "y", "rect": [1, 2]}, "text"):
        with pytest.raises(Refused) as e:
            journey.check_finding(bad)
        assert e.value.code == "bad_params"
    assert journey.check_finding({"severity": "low", "what": " x ", "basis": "measured", "screen": 2}) == {
        "severity": "low", "what": "x", "basis": "measured", "screen": "2"}


def test_a_call_names_a_hidden_thing_by_id_or_label_in_any_place():
    assert journey.hidden("tap", {"target": {"id": "x"}}) and journey.hidden("tap", {"target": {"label": "x"}})
    assert not journey.hidden("tap", {"target": {"ref": "e1"}}) and not journey.hidden("tap", {"target": {"text": "x"}})
    assert journey.hidden("wait_for", {"id": "x"}) and journey.hidden("wait_for", {"label": "x"})
    assert not journey.hidden("wait_for", {"text": "x"})
    assert journey.hidden("performance", {"tap": {"id": "x"}}) and journey.hidden("filmstrip", {"tap": {"label": "x"}})
    assert journey.hidden("run", {"steps": [{"tool": "wait", "seconds": 1}, {"tool": "tap", "target": {"id": "x"}}]})
    assert journey.hidden("run", {"steps": [{"tool": "tap", "target": {"text": "x"}, "expect": {"id": "done"}}]})
    assert not journey.hidden("run", {"steps": [{"tool": "tap", "target": {"text": "x"}, "expect": {"text": "Saved"}}]})


# ---- the tool in the daemon ----
def test_start_answers_the_goal_the_persona_and_the_screen_as_a_user_sees_it(reports):
    async def go():
        d = daemon([*HOME[:1], icon_button("e2", "Settings"), HOME[2]])
        stop, task = await started(d)
        r = await d.run_tool("a", "journey", {"start": True, "goal": "log breakfast", "persona": "a first-timer"})
        lines = r["text"].splitlines()
        assert lines[0] == 'Journey started: "log breakfast" as a first-timer, in a user\'s eyes.'
        assert lines[1].startswith("Screen 400x800 pt @3x · 3 elements")
        assert "id=" not in r["text"] and 'e2 button (icon only) (VoiceOver: "Settings")' in r["text"]
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert "id=" not in r["text"]
        only_folder(reports)
        await finish(stop, task)
    run(go)


def test_a_journey_with_no_persona_and_in_the_agents_eyes():
    async def go():
        d = daemon()
        stop, task = await started(d)
        r = await d.run_tool("a", "journey", {"start": True, "goal": "g", "eyes": "agent"})
        assert r["text"].splitlines()[0] == 'Journey started: "g" as no persona, in the agent\'s eyes.'
        assert "id=log-meal" in r["text"]
        await finish(stop, task)
    run(go)


def test_ids_and_labels_are_refused_with_the_exact_message_and_refs_and_text_work():
    async def go():
        d = daemon()
        stop, task = await started(d)
        await d.run_tool("a", "journey", {"start": True, "goal": "g"})
        for target in ({"id": "log-meal"}, {"label": "Log meal"}):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "tap", {"target": target})
            assert e.value.code == "bad_params" and str(e.value) == HIDDEN
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "wait_for", {"id": "x", "timeout": 1})
        assert str(e.value) == HIDDEN
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": [{"tool": "tap", "target": {"id": "log-meal"}}]})
        assert str(e.value) == HIDDEN
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert r["text"].startswith('Tapped e1 button "Log meal"') and "id=" not in r["text"]
        r = await d.run_tool("a", "tap", {"target": {"text": "Settings"}})
        assert r["text"].startswith('Tapped e2 button "Settings"')
        assert d.session.actions().count("tap") == 2
        # another agent is not in the journey: it still uses ids
        r = await d.run_tool("b", "tap", {"target": {"id": "log-meal"}})
        assert "id=log-meal" in r["text"]
        await finish(stop, task)
    run(go)


def test_no_jump_scroll_no_off_screen_names_and_nothing_extra_in_the_answer():
    async def go():
        d = daemon()
        d.session.offscreen = [button("", "Hidden thing", "hidden")]
        stop, task = await started(d)
        await d.run_tool("a", "journey", {"start": True, "goal": "g"})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "scroll", {"to": "target", "target": {"text": "Hidden thing"}})
        assert e.value.code == "bad_params" and 'to: "target"' in str(e.value)
        # an element past the edge is not there for a user: not_found, with no closest-match naming it
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"text": "Hidden thing"}})
        assert e.value.code == "not_found" and "Hidden" not in str(e.value).replace("Hidden thing\"", "") and "Closest" not in str(e.value)
        assert d.session.scrolls == []  # nothing was scrolled to reach it
        # a agent that isn't in a journey would have been told it is off screen
        with pytest.raises(Refused) as e:
            await d.run_tool("b", "tap", {"target": {"text": "Hidden thing"}})
        assert e.value.code == "not_actionable"
        await finish(stop, task)
    run(go)


def test_the_header_says_more_below_where_a_view_can_scroll_and_no_count():
    async def go():
        d = daemon()
        d.session.below = 4
        stop, task = await started(d)
        r = await d.run_tool("a", "journey", {"start": True, "goal": "g"})
        assert "(more below)" in r["text"] and "+4" not in r["text"]
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert "(more below)" in r["text"] and "+4" not in r["text"]
        d.session.below = 0
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert "more below" not in r["text"]
        await finish(stop, task)
    run(go)


def test_open_url_is_refused_unless_allowed_and_the_report_lists_it(reports):
    async def go():
        d = daemon()
        stop, task = await started(d)
        await d.run_tool("a", "journey", {"start": True, "goal": "g"})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "open_url", {"url": "myapp://x"})
        assert e.value.code == "not_allowed" and e.value.args[0].startswith("blocked: in a journey, open_url is refused")
        await d.run_tool("a", "journey", {"stop": True, "reached": False})
        await d.run_tool("a", "journey", {"start": True, "goal": "g2", "allow": ["open_url"]})
        r = await d.run_tool("a", "open_url", {"url": "myapp://x"})
        assert r["text"].startswith("opened myapp://x") or "myapp://x" in r["text"]
        await d.run_tool("a", "journey", {"stop": True, "reached": True})
        folder = sorted(p for p in reports.iterdir())[-1]
        assert "open_url myapp://x" in (folder / "report.md").read_text("utf-8")
        await finish(stop, task)
    run(go)


def test_the_agent_can_turn_the_eyes_off_and_the_report_says_so(reports):
    async def go():
        d = daemon()
        stop, task = await started(d)
        await d.run_tool("a", "journey", {"start": True, "goal": "g"})
        await d.run_tool("a", "look", {"screenshot": False})
        r = await d.run_tool("a", "journey", {"eyes": "agent"})
        assert r["text"].startswith("Now in the agent's eyes")
        r = await d.run_tool("a", "tap", {"target": {"id": "log-meal"}})  # allowed now
        assert "id=log-meal" in r["text"]
        await d.run_tool("a", "journey", {"stop": True, "reached": True})
        assert "until call 1, then the agent's eyes" in (only_folder(reports) / "report.md").read_text("utf-8")
        await finish(stop, task)
    run(go)


def test_the_arguments_of_journey_are_checked():
    async def go():
        d = daemon()
        stop, task = await started(d)
        for args, word in (({}, "needs start"), ({"start": True}, "needs a goal"),
                           ({"start": True, "goal": "g", "eyes": "x"}, "eyes must be"),
                           ({"start": True, "goal": "g", "allow": ["open_app"]}, "allow is"),
                           ({"start": True, "goal": "g", "conditions": {"mood": "x"}}, "conditions doesn't take mood"),
                           ({"start": True, "goal": "g", "conditions": {"language": "arabic!"}}, "language"),
                           ({"start": True, "goal": "g", "conditions": {"network": "slow"}}, "network"),
                           ({"start": True, "stop": True}, "one of"),
                           ({"start": True, "goal": "g", "reached": True}, "doesn't take reached"),
                           ({"stop": True, "reached": True}, "no journey is running"),
                           ({"stress": "background"}, "no journey is running"),
                           ({"finding": {"severity": "high", "what": "x", "basis": "y"}}, "no journey is running"),
                           ({"eyes": "agent"}, "needs start")):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "journey", args)
            assert e.value.code == "bad_params" and word in str(e.value), (args, str(e.value))
        assert d._journeys == {}  # nothing was left behind by the refused starts
        await d.run_tool("a", "journey", {"start": True, "goal": "g"})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "journey", {"start": True, "goal": "second"})
        assert e.value.code == "bad_params" and "already running" in str(e.value)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "journey", {"stop": True})
        assert "reached" in str(e.value) and "a" in d._journeys  # still running
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "journey", {"stop": True, "reached": True, "export": {"to": "gist", "repo": "a/b"}})
        assert e.value.code == "bad_params" and "a" in d._journeys
        with pytest.raises(Refused) as e:  # unknown names are refused by the table, like every tool
            await d.run_tool("a", "journey", {"stop": True, "reached": True, "color": 1})
        assert "doesn't take color" in str(e.value)
        # one journey per agent: another agent starts its own
        r = await d.run_tool("b", "journey", {"start": True, "goal": "other"})
        assert "other" in r["text"]
        await finish(stop, task)
    run(go)


def test_a_failed_start_leaves_no_journey_and_no_folder(reports):
    async def go():
        d = daemon()
        stop, task = await started(d)
        d.session.fail["look"] = Refused("reconnecting", "lost")  # the start's own look fails
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "journey", {"start": True, "goal": "g"})
        assert e.value.code == "reconnecting"
        assert d._journeys == {} and not [p for p in reports.iterdir()]
        await finish(stop, task)
    run(go)


def test_journey_is_not_a_run_step():
    from tapwright.batch import check_steps
    with pytest.raises(Refused) as e:
        check_steps([{"tool": "journey", "start": True}])
    assert e.value.code == "bad_params" and "'journey' can't be a step" in str(e.value)


def test_a_whole_journey_is_measured_and_reported(reports):
    async def go():
        d = daemon()
        stop, task = await started(d)
        await d.run_tool("a", "journey", {"start": True, "goal": "log breakfast by scanning its barcode",
                                          "persona": "a first-timer"})
        d.session.tree = list(FOOD)
        r = await d.run_tool("a", "tap", {"target": {"text": "Log meal"}})  # a new screen
        assert r["text"].startswith("Tapped e")
        await d.run_tool("a", "type_text", {"text": "2 eggs", "target": {"text": "Search"}})
        await d.run_tool("a", "scroll", {"direction": "down"})
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "look", {"screenshot": False})
        filed = await d.run_tool("a", "journey", {"finding": {"severity": "high", "screen": "2", "what": "Save is too low",
                                                              "basis": "measured"}})
        assert filed["text"] == 'Finding 1 filed: high, screen "2": Save is too low'
        await d.run_tool("a", "journey", {"finding": {"severity": "low", "what": "x", "basis": "best practice: y"}})
        r = await d.run_tool("a", "journey", {"stop": True, "reached": True, "why": "it logged"})
        first, why, second = r["text"].splitlines()[:3]
        assert re.fullmatch(r'Journey "log breakfast by scanning its barcode" as a first-timer: reached in 1 tap, '
                            r"\d+ s \(0 s waiting\), 0 wrong turns, 1 hesitation\.", first)
        assert why == "Why: it logged"
        folder = only_folder(reports)
        assert second == f"Findings: 1 high, 1 low. Report: {folder}"
        assert re.fullmatch(r"\d{8}-\d{6}-log-breakfast-by-scanning-its-barcode", folder.name)
        for name in ("report.md", "report.html", "journey.gif", "filmstrip.jpg", "journey.json"):
            assert (folder / name).is_file(), name
        shots = sorted((folder / "screens").glob("*.jpg"))
        assert len(shots) >= 4  # the start, and after the tap, the typing and the scroll
        assert sorted(p.name for p in (folder / "findings").glob("*.jpg")) == ["finding-1.jpg", "finding-2.jpg"]
        md = (folder / "report.md").read_text("utf-8")
        assert "Save is too low" in md and "Basis: measured" in md and "![The journey](journey.gif)" in md
        assert "2 entries" not in md and "1 entries, 6 characters" in md
        page = (folder / "report.html").read_text("utf-8")
        assert page.startswith("<!doctype html>") and 'src="journey.gif"' in page and "http" not in page.replace("http-equiv", "")
        assert "Save is too low" in page
        # at most two small pictures come back with the summary
        assert r["image"] and len(r.get("images", [])) <= 1
        assert Image.open(io.BytesIO(base64.b64decode(r["image"]))).width <= 360
        assert d._journeys == {}  # over: the eyes are back to the agent's
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert "id=log-meal" in r["text"] or "id=save" in r["text"]
        await finish(stop, task)
    run(go)


def test_a_missed_goal_says_so(reports):
    async def go():
        d = daemon()
        stop, task = await started(d)
        await d.run_tool("a", "journey", {"start": True, "goal": "g"})
        r = await d.run_tool("a", "journey", {"stop": True, "reached": False, "why": "no scan button"})
        lines = r["text"].splitlines()
        assert lines[0].startswith('Journey "g": not reached after 0 taps, ') and lines[1] == "Why: no scan button"
        assert lines[2].startswith("Findings: none. Report: ")
        await finish(stop, task)
    run(go)


# ---- conditions ----
def test_conditions_are_applied_at_the_start_and_undone_at_the_stop():
    async def go():
        d = daemon()
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": "com.example.app", "look": False})
        d.session.launch_args.clear()
        await d.run_tool("a", "journey", {"start": True, "goal": "g", "conditions": {
            "language": "ar", "text_size": "xl", "network": "3g"}})
        assert d.session.launch_args == [["-AppleLanguages", "(ar)", "-UIPreferredContentSizeCategoryName",
                                          "UICTContentSizeCategoryXL"]]
        assert d._network == "3g"
        # a later open_app in the journey carries them without being asked
        await d.run_tool("a", "open_app", {"app": "com.example.app", "look": False})
        assert d.session.launch_args[-1][:2] == ["-AppleLanguages", "(ar)"]
        d.session.launch_args.clear()
        r = await d.run_tool("a", "journey", {"stop": True, "reached": True})
        assert d._network is None and ("clear",) in d.session.device.network_calls
        assert d.session.launch_args == [[]]  # opened again, plain
        assert "Note: the network condition was set back to normal." in r["text"]
        assert "Note: the app was opened again without" in r["text"]
        await finish(stop, task)
    run(go)


def test_conditions_with_no_app_open_wait_for_open_app():
    async def go():
        d = daemon()
        stop, task = await started(d)
        d.session.app = None
        r = await d.run_tool("a", "journey", {"start": True, "goal": "g", "conditions": {"region": "QA"}})
        assert "will apply when you open_app" in r["text"] and d.session.launch_args == []
        await d.run_tool("a", "open_app", {"app": "com.example.app", "look": False})
        assert d.session.launch_args == [["-AppleLocale", "en_QA"]]
        await finish(stop, task)
    run(go)


# ---- stress moments ----
def test_stress_background_leaves_the_app_and_comes_back_and_checks_what_survives():
    async def go():
        d = daemon(FOOD)
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": "com.example.app", "look": False})
        await d.run_tool("a", "journey", {"start": True, "goal": "g"})
        d.session.calls.clear()
        d.session.tree = [FOOD[0], el("e2", "input", "Search", "search", value="2 eggs", rect=(10, 100, 300, 44)), FOOD[2]]
        r = await d.run_tool("a", "journey", {"stress": "background", "survives": {"text": "2 eggs"}})
        assert r["text"].splitlines()[0] == 'Stress: background; it survived: text "2 eggs" is still in the field.'
        assert "button:home" in d.session.calls and d.session.launched[-1] == "com.example.app"
        assert d.session.calls.index("button:home") < len(d.session.calls) - d.session.calls[::-1].index("launch") - 1
        d.session.tree = [FOOD[0], FOOD[1], FOOD[2]]  # the field was emptied by the app
        r = await d.run_tool("a", "journey", {"stress": "background", "survives": {"text": "2 eggs"}})
        assert r["text"].splitlines()[0] == 'Stress: background; it didn\'t survive: text "2 eggs" is no longer on screen.'
        r = await d.run_tool("a", "journey", {"stress": "background"})
        assert r["text"].splitlines()[0] == "Stress: background."
        await finish(stop, task)
    run(go)


def test_stress_relaunch_closes_and_opens_and_goal_step_checks_the_screen():
    async def go():
        d = daemon(HOME)
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": "com.example.app", "look": False})
        await d.run_tool("a", "journey", {"start": True, "goal": "g"})
        d.session.closed.clear()
        d.session.launched.clear()
        r = await d.run_tool("a", "journey", {"stress": "relaunch", "survives": {"goal_step": 1}})
        assert d.session.closed == ["com.example.app"] and d.session.launched == ["com.example.app"]
        assert r["text"].splitlines()[0] == 'Stress: relaunch; it survived: screen 1 ("Today") is showing again.'
        d.session.tree = list(OTHER)
        r = await d.run_tool("a", "journey", {"stress": "relaunch", "survives": {"goal_step": 1}})
        assert r["text"].splitlines()[0] == ('Stress: relaunch; it didn\'t survive: the screen isn\'t screen 1 ("Today") '
                                             "any more.")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "journey", {"stress": "relaunch", "survives": {"goal_step": 9}})
        assert e.value.code == "bad_params" and "2 screens so far" in str(e.value)
        await finish(stop, task)
    run(go)


def test_stress_rotate_goes_there_and_back_and_network_sets_the_condition_until_the_stop():
    async def go():
        d = daemon()
        stop, task = await started(d)
        await d.run_tool("a", "journey", {"start": True, "goal": "g"})
        d.session.calls.clear()
        r = await d.run_tool("a", "journey", {"stress": "rotate"})
        assert r["text"].splitlines()[0] == "Stress: rotate."
        assert [c for c in d.session.actions() if c.startswith("rotate")] == ["rotate:landscape_left", "rotate:portrait"]
        r = await d.run_tool("a", "journey", {"stress": "network", "mode": "3g", "survives": {"text": "Today"}})
        assert r["text"].splitlines()[0] == 'Stress: network 3g; it survived: text "Today" is still on screen.'
        assert d._network == "3g"
        await d.run_tool("a", "journey", {"stop": True, "reached": True})
        assert d._network is None  # undone at the stop
        await finish(stop, task)
    run(go)


def test_stress_double_tap_sends_the_same_touch_twice_and_is_not_counted_as_a_tap(reports):
    async def go():
        d = daemon()
        stop, task = await started(d)
        await d.run_tool("a", "journey", {"start": True, "goal": "g"})
        d.session.tap_points.clear()
        r = await d.run_tool("a", "journey", {"stress": "double_tap", "target": {"text": "Log meal"}})
        assert len(d.session.tap_points) == 2 and d.session.tap_points[0] == d.session.tap_points[1]
        assert r["text"].splitlines()[0] == "Stress: double_tap."
        for args in ({"stress": "double_tap"}, {"stress": "double_tap", "target": {"id": "log-meal"}},
                     {"stress": "background", "target": {"text": "x"}}, {"stress": "network"},
                     {"stress": "rotate", "mode": "3g"}, {"stress": "shake"}, {"stress": "rotate", "survives": {"x": 1}},
                     {"stress": "rotate", "survives": {"text": ""}}):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "journey", args)
            assert e.value.code == "bad_params", args
        r = await d.run_tool("a", "journey", {"stop": True, "reached": True})
        assert "reached in 0 taps" in r["text"]
        assert "Stress: double_tap." in (only_folder(reports) / "report.md").read_text("utf-8")
        await finish(stop, task)
    run(go)


def test_stress_with_done_only_checks_what_survives():
    async def go():
        d = daemon()
        stop, task = await started(d)
        await d.run_tool("a", "journey", {"start": True, "goal": "g"})
        d.session.calls.clear()
        r = await d.run_tool("a", "journey", {"stress": "background", "done": True, "survives": {"text": "Today"}})
        assert r["text"].startswith('Stress: background; it survived: text "Today"')
        assert "button:home" not in d.session.calls
        await finish(stop, task)
    run(go)


def test_a_stress_moment_in_personal_mode_goes_through_the_app_check():
    async def go():
        d = daemon()
        stop, task = await started(d)
        scope = Scope(("com.example.app",))
        await d.run_tool("a", "journey", {"start": True, "goal": "g"}, scope=scope)
        d.session.foreground = "com.other.app"
        d.session.app = ("Other", "com.other.app")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "journey", {"stress": "double_tap", "target": {"text": "Log meal"}}, scope=scope)
        assert e.value.code == "not_allowed"
        await finish(stop, task)
    run(go)


# ---- findings API for ux_check ----
def test_add_finding_is_a_small_api(tmp_path):
    j = new_journey(tmp_path)
    j.see(full(("header", "Add food"), ("input", "Search")))
    got = j.add_finding({"severity": "medium", "what": "tiny", "basis": "measured", "rect": (1, 2, 3, 4)})
    assert got["screen"] == "1. Add food" and got["screen_index"] == 1 and got["rect"] == (1.0, 2.0, 3.0, 4.0)
    assert j.screen_number("Add food") == 1 and j.screen_number("step 1") == 1 and j.screen_number("9") is None
    assert j.counts() == {"high": 0, "medium": 1, "low": 0}


def test_extra_sections_land_in_the_report(reports):
    async def go():
        d = daemon()
        stop, task = await started(d)
        await d.run_tool("a", "journey", {"start": True, "goal": "g"})
        d._journeys["a"].extra_sections.append(("House rules", ["- way_back: kept", "- <b>max_taps_to_goal</b>: kept"]))
        await d.run_tool("a", "journey", {"stop": True, "reached": True})
        folder = only_folder(reports)
        assert "## House rules\n\n- way_back: kept" in (folder / "report.md").read_text("utf-8")
        assert "&lt;b&gt;max_taps_to_goal" in (folder / "report.html").read_text("utf-8")  # escaped
        await finish(stop, task)
    run(go)


# ---- the GIF and the pictures ----
def noisy(path, seed: int, size=(540, 1080)):
    import random
    rnd = random.Random(seed)
    img = Image.frombytes("RGB", (60, 120), bytes(rnd.randrange(256) for _ in range(60 * 120 * 3))).resize(size)
    img.save(path, "JPEG", quality=90)


def test_the_gif_is_360_wide_with_a_ring_and_trims_to_fit(tmp_path):
    frames = []
    for i in range(10):
        p = tmp_path / f"{i}.jpg"
        noisy(p, i)
        frames.append(journey.Frame(p, "x", (200.0, 400.0) if i % 2 else None, (400.0, 800.0), 1))
    out = tmp_path / "j.gif"
    size = journey_report.make_gif(frames, out)
    gif = Image.open(out)
    assert gif.width == 360 and gif.n_frames == 10 and size == out.stat().st_size
    small = journey_report.make_gif(frames, tmp_path / "small.gif", max_bytes=size // 3)
    assert small <= size // 3 or Image.open(tmp_path / "small.gif").n_frames == 2
    assert Image.open(tmp_path / "small.gif").n_frames < 10
    assert journey_report.make_gif([], tmp_path / "none.gif") == 0


def test_stills_are_stretched_before_anything_else_is_dropped(tmp_path):
    p = tmp_path / "a.jpg"
    Image.new("RGB", (540, 1080), (10, 120, 200)).save(p, "JPEG")
    frames = [journey.Frame(p, "x", None, (400.0, 800.0), 1) for _ in range(6)]
    out = tmp_path / "s.gif"
    journey_report.make_gif(frames, out)
    gif = Image.open(out)
    assert gif.n_frames == 1  # six equal pictures are one that stays longer
    assert gif.info["duration"] >= 1600


def test_a_marked_screenshot_and_a_key_picture(tmp_path):
    src = tmp_path / "s.jpg"
    Image.new("RGB", (540, 1080), (255, 255, 255)).save(src, "JPEG")
    out = tmp_path / "m.jpg"
    journey_report.mark_finding(src, out, 3, "high", (100, 200, 100, 50), (400.0, 800.0))
    marked = Image.open(out).convert("RGB")
    assert marked.getpixel((int(100 * 540 / 400) + 1, int(200 * 1080 / 800) + 1)) != (255, 255, 255) or \
        marked.getpixel((int(100 * 540 / 400), int(200 * 1080 / 800))) != (255, 255, 255)
    data, size = journey_report.key_picture(src, 200)
    assert size[0] == 200 and Image.open(io.BytesIO(data)).width == 200


# ---- export ----
class FakeGh:
    def __init__(self, replies=None):
        self.calls = []
        self.replies = replies or {}

    def __call__(self, args, stdin=None):
        self.calls.append((list(args), stdin))
        for prefix, reply in self.replies.items():
            if " ".join(args).startswith(prefix):
                if isinstance(reply, Exception):
                    raise reply
                return reply
        return ""


@pytest.fixture
def report_folder(tmp_path):
    folder = tmp_path / "20260101-120000-g"
    (folder / "screens").mkdir(parents=True)
    (folder / "findings").mkdir()
    for name in ("report.md", "report.html"):
        (folder / name).write_text("x", "utf-8")
    for name in ("journey.gif", "filmstrip.jpg", "screens/001.jpg", "findings/finding-1.jpg"):
        (folder / name).write_bytes(b"\x89data")
    return folder


OK = {"to": "pr", "repo": "o/r", "pr": 310, "confirm": True}


def test_export_checks_its_argument():
    assert journey_export.check(OK) == {"to": "pr", "repo": "o/r", "pr": 310, "public": False, "fork": False}
    assert journey_export.check({"to": "issue", "repo": "o/r", "confirm": True, "public": True}) == {
        "to": "issue", "repo": "o/r", "pr": None, "public": True, "fork": False}
    assert journey_export.check({**OK, "fork": True})["fork"] is True
    for bad in ("pr", {"to": "gist", "repo": "o/r", "confirm": True}, {"to": "pr", "repo": "o/r", "confirm": True},
                {**OK, "repo": "nope"}, {**OK, "pr": "1"}, {"to": "issue", "repo": "o/r", "pr": 3, "confirm": True},
                {**OK, "x": 1}, {**OK, "repo": "o/r; rm"}, {**OK, "public": "yes"},
                {"to": "issue", "repo": "o/r", "confirm": True, "fork": True}):
        with pytest.raises(Refused) as e:
            journey_export.check(bad)
        assert e.value.code == "bad_params", bad


def test_m2_export_needs_the_agent_to_say_its_user_asked():
    for args in ({"to": "pr", "repo": "o/r", "pr": 1}, {"to": "pr", "repo": "o/r", "pr": 1, "confirm": False}):
        with pytest.raises(Refused) as e:
            journey_export.check(args)
        assert e.value.code == "bad_params" and str(e.value) == journey_export.NEED_CONFIRM and "Ask your user" in str(e.value)


def pr_replies(owner="o", visibility="PRIVATE"):
    return {"pr view": json.dumps({"headRefName": "feature/x", "headRepository": {"name": "r"},
                                   "headRepositoryOwner": {"login": owner}}),
            "repo view": json.dumps({"visibility": visibility}),
            f"api repos/{owner}/r/git/ref/heads/feature/x": json.dumps({"object": {"sha": "h1"}}),
            f"api repos/{owner}/r/git/commits/h1": json.dumps({"tree": {"sha": "t1"}}),
            f"api --method POST repos/{owner}/r/git/blobs": json.dumps({"sha": "b1"}),
            f"api --method POST repos/{owner}/r/git/trees": json.dumps({"sha": "t2"}),
            f"api --method POST repos/{owner}/r/git/commits": json.dumps({"sha": "c2"})}


def test_export_to_a_pr_makes_one_commit_of_the_report_files_and_comments(monkeypatch, report_folder):
    gh = FakeGh(pr_replies())
    monkeypatch.setattr(journey_export, "run_gh", gh)
    spec = journey_export.check(OK)
    line = journey_export.export(spec, report_folder, "Journey \"g\": reached.\nFindings: none.")
    blobs = [a for a, _ in gh.calls if a[:4] == ["api", "--method", "POST", "repos/o/r/git/blobs"]]
    assert len(blobs) == 5  # report.md, report.html, journey.gif, filmstrip.jpg, finding-1.jpg: no screenshot
    trees = [json.loads(s) for a, s in gh.calls if a[:4] == ["api", "--method", "POST", "repos/o/r/git/trees"]]
    base = f".tapwright/reports/{report_folder.name}"
    assert len(trees) == 1 and trees[0]["base_tree"] == "t1"
    assert sorted(e["path"] for e in trees[0]["tree"]) == sorted(f"{base}/{n}" for n in (
        "report.md", "report.html", "journey.gif", "filmstrip.jpg", "findings/finding-1.jpg"))
    commits = [json.loads(s) for a, s in gh.calls if a[:4] == ["api", "--method", "POST", "repos/o/r/git/commits"]]
    assert len(commits) == 1 and commits[0]["parents"] == ["h1"] and commits[0]["tree"] == "t2"
    patch = [(a, json.loads(s)) for a, s in gh.calls if a[:3] == ["api", "--method", "PATCH"]]
    assert patch == [(["api", "--method", "PATCH", "repos/o/r/git/refs/heads/feature/x", "--input", "-"],
                      {"sha": "c2", "force": False})]
    assert not [a for a, _ in gh.calls if a[:3] == ["api", "--method", "PUT"]]  # never a commit per file
    comment = [(a, s) for a, s in gh.calls if a[:2] == ["pr", "comment"]]
    assert len(comment) == 1 and comment[0][0][2:] == ["310", "--repo", "o/r", "--body-file", "-"]
    assert f"https://github.com/o/r/raw/feature/x/{base}/journey.gif" in comment[0][1]
    assert "PR #310" in line and "o/r" in line


def test_m2_a_public_repository_is_refused_unless_public_is_given(monkeypatch, report_folder):
    gh = FakeGh(pr_replies(visibility="PUBLIC"))
    monkeypatch.setattr(journey_export, "run_gh", gh)
    with pytest.raises(Refused) as e:
        journey_export.export(journey_export.check(OK), report_folder, "s")
    assert e.value.code == "not_allowed" and "public repository" in str(e.value) and "public: true" in str(e.value)
    assert not [a for a, _ in gh.calls if "POST" in a or "PATCH" in a]
    journey_export.export(journey_export.check({**OK, "public": True}), report_folder, "s")
    assert [a for a, _ in gh.calls if a[:3] == ["api", "--method", "PATCH"]]
    # a visibility gh can't tell is not taken as private
    monkeypatch.setattr(journey_export, "run_gh", FakeGh({**pr_replies(), "repo view": "{}"}))
    with pytest.raises(Refused) as e:
        journey_export.export(journey_export.check(OK), report_folder, "s")
    assert "public or private" in str(e.value)


def test_m2_a_pr_from_a_fork_is_refused_unless_fork_is_given_and_says_where_it_would_write(monkeypatch, report_folder):
    gh = FakeGh(pr_replies(owner="fork"))
    monkeypatch.setattr(journey_export, "run_gh", gh)
    with pytest.raises(Refused) as e:
        journey_export.export(journey_export.check(OK), report_folder, "s")
    assert e.value.code == "not_allowed" and "written to fork/r" in str(e.value) and "fork: true" in str(e.value)
    assert not [a for a, _ in gh.calls if "POST" in a or "PATCH" in a]
    line = journey_export.export(journey_export.check({**OK, "fork": True}), report_folder, "s")
    assert "fork/r branch feature/x" in line
    views = [a[2] for a, _ in gh.calls if a[:2] == ["repo", "view"]]
    assert "fork/r" in views and "o/r" in views  # both are checked for visibility


def test_export_to_an_issue_pushes_pictures_to_qa_reports_in_one_commit_and_links_them(monkeypatch, report_folder):
    gh = FakeGh({"api repos/o/r/branches/qa-reports": Refused("unsupported", "gh api failed: Not Found"),
                 "repo view": json.dumps({"visibility": "PRIVATE"}),
                 "api repos/o/r/git/ref/heads/main": json.dumps({"object": {"sha": "abc123"}}),
                 "api repos/o/r/git/ref/heads/qa-reports": json.dumps({"object": {"sha": "abc123"}}),
                 "api repos/o/r/git/commits/abc123": json.dumps({"tree": {"sha": "t1"}}),
                 "api --method POST repos/o/r/git/blobs": json.dumps({"sha": "b1"}),
                 "api --method POST repos/o/r/git/trees": json.dumps({"sha": "t2"}),
                 "api --method POST repos/o/r/git/commits": json.dumps({"sha": "c2"}),
                 "api repos/o/r": json.dumps({"default_branch": "main"}),
                 "issue create": "https://github.com/o/r/issues/7\n"})
    monkeypatch.setattr(journey_export, "run_gh", gh)
    line = journey_export.export(journey_export.check({"to": "issue", "repo": "o/r", "confirm": True}), report_folder,
                                 "Journey \"g\": reached.\nFindings: none.")
    created = [a for a, _ in gh.calls if a[:4] == ["api", "--method", "POST", "repos/o/r/git/refs"]]
    assert created == [["api", "--method", "POST", "repos/o/r/git/refs", "-f", "ref=refs/heads/qa-reports", "-f", "sha=abc123"]]
    tree = [json.loads(s) for a, s in gh.calls if a[:4] == ["api", "--method", "POST", "repos/o/r/git/trees"]][0]["tree"]
    assert sorted(e["path"].split(report_folder.name + "/")[1] for e in tree) == [
        "filmstrip.jpg", "findings/finding-1.jpg", "journey.gif", "report.html", "report.md"]  # no screenshots
    assert [a[3] for a, _ in gh.calls if a[:3] == ["api", "--method", "PATCH"]] == ["repos/o/r/git/refs/heads/qa-reports"]
    issue = [(a, s) for a, s in gh.calls if a[:2] == ["issue", "create"]]
    assert len(issue) == 1 and f"/raw/qa-reports/reports/{report_folder.name}/journey.gif" in issue[0][1]
    assert line.endswith("https://github.com/o/r/issues/7") and "qa-reports" in line


def test_export_does_not_make_the_branch_when_it_is_there(monkeypatch, report_folder):
    gh = FakeGh({"api repos/o/r/branches/qa-reports": "{}", "repo view": json.dumps({"visibility": "PRIVATE"}),
                 "api repos/o/r/git/ref/heads/qa-reports": json.dumps({"object": {"sha": "a"}}),
                 "api repos/o/r/git/commits/a": json.dumps({"tree": {"sha": "t"}}),
                 "api --method POST repos/o/r/git/blobs": json.dumps({"sha": "b"}),
                 "api --method POST repos/o/r/git/trees": json.dumps({"sha": "t2"}),
                 "api --method POST repos/o/r/git/commits": json.dumps({"sha": "c"}),
                 "api repos/o/r": json.dumps({"default_branch": "main"})})
    monkeypatch.setattr(journey_export, "run_gh", gh)
    journey_export.export(journey_export.check({"to": "issue", "repo": "o/r", "confirm": True}), report_folder, "s")
    assert not [a for a, _ in gh.calls if a[:4] == ["api", "--method", "POST", "repos/o/r/git/refs"]]


def test_gh_missing_or_failing_is_unsupported_and_never_called_for_real(monkeypatch):
    monkeypatch.setattr(journey_export.shutil, "which", lambda name: None)
    called = []
    monkeypatch.setattr(journey_export.subprocess, "run", lambda *a, **k: called.append(a))
    with pytest.raises(Refused) as e:
        journey_export.run_gh(["pr", "view"])
    assert e.value.code == "unsupported" and "gh auth login" in str(e.value) and not called

    class Done:
        returncode, stdout, stderr = 1, "", "HTTP 404: Not Found\nsecond line"
    asked = []
    monkeypatch.setattr(journey_export.shutil, "which", lambda name: asked.append(name) or "/usr/bin/gh")
    monkeypatch.setattr(journey_export.subprocess, "run", lambda *a, **k: Done())
    with pytest.raises(Refused) as e:
        journey_export.run_gh(["pr", "view", "1"])
    assert "second line" in str(e.value)
    Done.returncode, Done.stdout = 0, "ok"
    assert journey_export.run_gh(["x"]) == "ok"
    # on Windows only gh.exe is looked for: a .cmd shim would pass a goal's text through cmd.exe
    assert asked[-1] == ("gh.exe" if journey_export.os.name == "nt" else "gh")


def test_stop_without_export_never_touches_gh_and_a_failed_export_is_a_note(monkeypatch, reports):
    async def go():
        d = daemon()
        stop, task = await started(d)
        calls = []
        monkeypatch.setattr(journey_export, "run_gh", lambda *a, **k: calls.append(a) or "")
        await d.run_tool("a", "journey", {"start": True, "goal": "g"})
        await d.run_tool("a", "journey", {"stop": True, "reached": True})
        assert calls == []
        await d.run_tool("a", "journey", {"start": True, "goal": "g2"})

        def failing(args, stdin=None):
            raise Refused("unsupported", "gh pr view failed: no such PR")
        monkeypatch.setattr(journey_export, "run_gh", failing)
        r = await d.run_tool("a", "journey", {"stop": True, "reached": True,
                                              "export": {"to": "pr", "repo": "o/r", "pr": 3, "confirm": True}})
        assert "Note: export failed, the report is still on this computer: gh pr view failed: no such PR" in r["text"]
        assert "Report: " in r["text"]
        # no confirm: refused before the journey is stopped
        await d.run_tool("a", "journey", {"start": True, "goal": "g3"})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "journey", {"stop": True, "reached": True, "export": {"to": "pr", "repo": "o/r", "pr": 3}})
        assert e.value.code == "bad_params" and "a" in d._journeys
        await finish(stop, task)
    run(go)


def test_stop_exports_through_gh_when_asked(monkeypatch, reports):
    async def go():
        d = daemon()
        stop, task = await started(d)
        gh = FakeGh(pr_replies())
        monkeypatch.setattr(journey_export, "run_gh", gh)
        await d.run_tool("a", "journey", {"start": True, "goal": "g"})
        r = await d.run_tool("a", "journey", {"stop": True, "reached": True,
                                              "export": {"to": "pr", "repo": "o/r", "pr": 3, "confirm": True}})
        assert any("Exported: the report is committed to o/r branch feature/x" in line for line in r["text"].splitlines())
        assert [a for a, _ in gh.calls if a[:2] == ["pr", "comment"]]
        await finish(stop, task)
    run(go)


def test_m9_stop_never_loses_its_answer(monkeypatch, reports):
    from tapwright import uxcheck

    async def go():
        d = daemon()
        stop, task = await started(d)
        rules = uxcheck.Rules((), 0, False)
        await d.run_tool("a", "journey", {"start": True, "goal": "g"}, ux_rules=rules)
        await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        jr = d._journeys["a"]
        jr.findings.extend({"severity": "low", "what": f"f{i}", "basis": "b"} for i in range(journey.MAX_FINDINGS))
        r = await d.run_tool("a", "journey", {"stop": True, "reached": True}, ux_rules=rules)  # the rule can't be filed
        assert "reached in 1 tap" in r["text"] and d._journeys == {}
        # a report that can't be written, and an export that fails in a way gh never reports, are notes
        await d.run_tool("a", "journey", {"start": True, "goal": "g2"})

        def full_disk(*a):
            raise OSError(28, "No space left")
        monkeypatch.setattr(journey_report, "write", full_disk)
        r = await d.run_tool("a", "journey", {"stop": True, "reached": False})
        assert r["text"].startswith('Journey "g2"') and "Note: couldn't write the report to" in r["text"]
        assert d._journeys == {}
        monkeypatch.undo()
        await d.run_tool("a", "journey", {"start": True, "goal": "g3"})

        def odd(*a, **k):
            raise ValueError("odd")
        monkeypatch.setattr(journey_export, "run_gh", odd)
        r = await d.run_tool("a", "journey", {"stop": True, "reached": True,
                                              "export": {"to": "pr", "repo": "o/r", "pr": 3, "confirm": True}})
        assert "reached" in r["text"] and "Note: export failed" in r["text"] and d._journeys == {}
        await finish(stop, task)
    run(go)


def test_the_mcp_result_carries_a_second_picture():
    from tapwright import mcp_server
    result = mcp_server._result("t", "AAAA", more=["BBBB"])
    assert [c.type for c in result.content] == ["text", "image", "image"]


def test_the_findings_line_names_the_errors_the_report_counts(tmp_path):
    j = new_journey(tmp_path)
    assert journey.findings_line(j, "F") == "Findings: none. Report: F"  # no errors: the line is as it was
    j.note_call("tap", {}, "", 0.1, "not_found")
    j.note_call("tap", {}, "", 0.1, "covered")
    j.note_call("tap", {}, "", 0.1, "not_found")
    assert j.errors == 3  # (the report's `Errors` row)
    assert journey.findings_line(j, "F") == "Findings: none. Errors: 3 (2 not_found, 1 covered). Report: F"
    j.findings.append({"severity": "high", "what": "x"})
    assert journey.findings_line(j, "F") == "Findings: 1 high. Errors: 3 (2 not_found, 1 covered). Report: F"


# ---- ux_check and the house rules (the merge with §17.3) ----
UX = {"kind": "icon_only", "element": 'e2 button @340,20 44x44', "what": "an icon-only button with no label",
      "basis": "best practice: label every control", "screen": '"Example" (Today)'}


def test_a_ux_check_finding_goes_to_the_journey_the_agent_has_running(reports):
    async def go():
        d = daemon()
        stop, task = await started(d)
        assert journey.add_finding("a", UX) is False  # no journey: nothing happens
        await d.run_tool("a", "journey", {"start": True, "goal": "g"})
        assert journey.add_finding("b", UX) is False  # another agent's
        assert journey.add_finding("a", UX) is True
        assert journey.add_finding("a", UX) is False  # the same finding again is filed once
        assert journey.add_finding("a", {**UX, "kind": "naming", "what": "Save here, Done there"}) is True
        assert journey.add_finding("a", {**UX, "kind": "way_back", "basis": "house rule: way_back", "what": "none"}) is True
        assert journey.add_finding("a", {"kind": "x"}) is False
        got = d._journeys["a"].findings
        assert [(f["kind"], f["severity"]) for f in got] == [("icon_only", "high"), ("naming", "low"), ("way_back", "high")]
        assert got[0]["rect"] == (340.0, 20.0, 44.0, 44.0) and got[0]["screen"] == "1. Today"
        r = await d.run_tool("a", "journey", {"stop": True, "reached": True})
        assert r["text"].splitlines()[1].startswith("Findings: 2 high, 1 low. Report: ")
        md = (only_folder(reports) / "report.md").read_text("utf-8")
        assert "icon_only: e2 button @340,20 44x44: an icon-only button with no label" in md
        assert "Basis: best practice: label every control" in md
        assert (only_folder(reports) / "findings" / "finding-1.jpg").is_file()
        assert journey.add_finding("a", UX) is False  # over: nothing is running
        await finish(stop, task)
    run(go)


def test_ux_check_inside_a_journey_lands_in_the_report(reports):
    async def go():
        d = daemon([button("e1", "Log meal", "log-meal"), ui.Element("e2", "button", "", rect=(340, 20, 44, 44),
                                                                       actions=("tap",), type="Button"), el("e3", "text", "Today")])
        stop, task = await started(d)
        await d.run_tool("a", "journey", {"start": True, "goal": "g"})
        r = await d.run_tool("a", "ux_check", {})
        assert "icon_only" in r["text"]
        assert any(f.get("kind") == "icon_only" for f in d._journeys["a"].findings)
        await d.run_tool("a", "journey", {"stop": True, "reached": False})
        assert "icon_only" in (only_folder(reports) / "report.md").read_text("utf-8")
        await finish(stop, task)
    run(go)


def test_max_taps_to_goal_is_checked_at_the_stop_and_the_report_names_the_rule(reports):
    from tapwright import uxcheck

    async def go():
        d = daemon()
        stop, task = await started(d)
        rules = uxcheck.Rules(("core features within 2 taps of home",), 1, True)
        await d.run_tool("a", "journey", {"start": True, "goal": "g"}, ux_rules=rules)
        await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        await d.run_tool("a", "tap", {"target": {"ref": "e2"}})
        r = await d.run_tool("a", "journey", {"stop": True, "reached": True}, ux_rules=rules)
        assert r["text"].splitlines()[1].startswith("Findings: 1 high. Report: ")
        md = (only_folder(reports) / "report.md").read_text("utf-8")
        assert "## House rules" in md and "core features within 2 taps of home (judged by the agent" in md
        assert "max_taps_to_goal: 1: **broken**, reached the goal in 2 taps, more than the 1 the rule allows." in md
        assert "way_back: no screen ux_check read broke it." in md
        assert "house rule: max_taps_to_goal 1" in md
        await finish(stop, task)
    run(go)


def test_a_journey_within_the_rule_keeps_it_and_one_that_missed_its_goal_is_not_judged(reports):
    from tapwright import uxcheck

    async def go():
        d = daemon()
        stop, task = await started(d)
        rules = uxcheck.Rules((), 3, False)
        await d.run_tool("a", "journey", {"start": True, "goal": "g"}, ux_rules=rules)
        await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        r = await d.run_tool("a", "journey", {"stop": True, "reached": True}, ux_rules=rules)
        assert "Findings: none." in r["text"]
        assert "max_taps_to_goal: 3: kept, reached in 1 tap." in (only_folder(reports) / "report.md").read_text("utf-8")
        await d.run_tool("a", "journey", {"start": True, "goal": "g2"}, ux_rules=rules)
        for _ in range(4):
            await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        r = await d.run_tool("a", "journey", {"stop": True, "reached": False}, ux_rules=rules)
        assert "Findings: none." in r["text"]
        newest = sorted(reports.iterdir())[-1]
        assert "not applicable, the goal wasn't reached" in (newest / "report.md").read_text("utf-8")
        # no rules at all: no section
        await d.run_tool("a", "journey", {"start": True, "goal": "g3"})
        await d.run_tool("a", "journey", {"stop": True, "reached": True})
        assert "House rules" not in (sorted(reports.iterdir())[-1] / "report.md").read_text("utf-8")
        await finish(stop, task)
    run(go)


# ---- review fixes (W14) ----
SPRINGBOARD = "com.apple.springboard"


def test_h1_a_frame_is_kept_in_personal_mode_only_while_a_given_app_is_in_front(reports):
    async def go():
        d = daemon()
        stop, task = await started(d)
        scope = Scope(("com.example.app",))
        d.session.foreground = "com.example.app"
        await d.run_tool("a", "journey", {"start": True, "goal": "g"}, scope=scope)
        jr = d._journeys["a"]
        assert len(jr.frames) == 1
        d.session.tree = list(FOOD)
        await d.run_tool("a", "tap", {"target": {"text": "Log meal"}}, scope=scope)
        assert len(jr.frames) == 2 and jr.left_out == 0
        # the tap opened Safari: the answer is withheld (outside_apps) and no picture is kept
        d.session.app = ("Safari", "com.apple.mobilesafari")
        d.session.tree = list(HOME)
        r = await d.run_tool("a", "tap", {"target": {"text": "Save"}}, scope=scope)
        assert "outside_apps" in r["text"]
        assert len(jr.frames) == 2 and jr.left_out == 1
        await d.run_tool("a", "journey", {"stop": True, "reached": False}, scope=scope)
        assert "1 frame left out: another app was in front." in (only_folder(reports) / "report.md").read_text("utf-8")
        await finish(stop, task)
    run(go)


def test_h1_a_frame_is_kept_in_dedicated_mode_only_for_the_journeys_app(reports):
    async def go():
        d = daemon()
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": "com.example.app", "look": False})
        await d.run_tool("a", "journey", {"start": True, "goal": "g"})
        jr = d._journeys["a"]
        assert jr.app == "com.example.app" and len(jr.frames) == 1
        d.session.app, d.session.foreground = ("SpringBoard", SPRINGBOARD), SPRINGBOARD
        await d.run_tool("a", "press_button", {"name": "home"})
        assert len(jr.frames) == 1 and jr.left_out == 1
        d.session.app, d.session.foreground = ("Example", "com.example.app"), "com.example.app"
        await d.run_tool("a", "tap", {"target": {"text": "Log meal"}})
        assert len(jr.frames) == 2 and jr.left_out == 1
        await finish(stop, task)
    run(go)


def test_m5_the_naming_memory_skips_a_withheld_look_and_is_scoped_to_the_journey(reports):
    from tapwright import uxcheck

    async def go():
        d = daemon()
        stop, task = await started(d)
        scope = Scope(("com.example.app",))
        uxcheck.note_screen("a", HOME)  # left over from before the journey
        await d.run_tool("a", "journey", {"start": True, "goal": "g"}, scope=scope)
        assert len(uxcheck.screens_seen("a")) == 1  # (the start's own look), the old one is gone
        before = list(uxcheck.screens_seen("a"))
        d.session.app, d.session.foreground = ("Other", "com.other.app"), "com.other.app"
        d.session.tree = list(OTHER)
        with pytest.raises(Refused):
            await d.run_tool("a", "look", {"screenshot": False}, scope=scope)
        assert uxcheck.screens_seen("a") == before
        await d.run_tool("a", "journey", {"stop": True, "reached": False}, scope=scope)
        assert uxcheck.screens_seen("a") == []
        await finish(stop, task)
    run(go)


def test_m3_double_tap_is_refused_on_what_the_policy_holds_and_re_police_before_the_second_touch(reports):
    class Swapping(DescribingSession):
        swap: list | None = None

        async def tap(self, target, hold_ms=60):
            done = await super().tap(target, hold_ms)
            if self.swap is not None:
                self.tree, self.swap = self.swap, None
            return done

    async def go():
        session = Swapping()
        session.tree = [button("e1", "Buy now", "buy"), el("e2", "text", "Today")]
        d = make(session)
        stop, task = await started(d)
        await d.run_tool("a", "journey", {"start": True, "goal": "g"})
        session.tap_points.clear()
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "journey", {"stress": "double_tap", "target": {"text": "Buy now"}})
        assert e.value.code == "not_allowed" and "double_tap on an element the safety policy holds" in str(e.value)
        assert session.tap_points == []
        # a harmless button; the first touch brings a "Delete" under the finger: the second touch is not sent
        session.tree = [button("e1", "Log meal", "log"), el("e2", "text", "Today", rect=(10, 400, 100, 40))]
        session.swap = [button("e1", "Delete all", "del"), el("e2", "text", "Today", rect=(10, 400, 100, 40))]
        session.tap_points.clear()
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "journey", {"stress": "double_tap", "target": {"x": 60, "y": 120}})
        assert "safety policy holds" in str(e.value) and "Delete all" in str(e.value)
        assert len(session.tap_points) == 1
        await finish(stop, task)
    run(go)


def test_m4_in_a_users_eyes_text_does_not_reach_an_icon_only_button_by_its_voiceover_label(reports):
    async def go():
        d = daemon([button("e1", "Log meal", "log-meal"), icon_button("e2", "Settings gear")])
        stop, task = await started(d)
        await d.run_tool("a", "journey", {"start": True, "goal": "g"})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"text": "Settings gear"}})
        assert e.value.code == "not_found"
        r = await d.run_tool("a", "tap", {"target": {"ref": "e2"}})  # the ref, from the list, still works
        assert r["text"].startswith("Tapped e2")
        r = await d.run_tool("a", "tap", {"target": {"text": "Log meal"}})
        assert r["text"].startswith("Tapped e1")
        await d.run_tool("a", "journey", {"stop": True, "reached": True})
        # outside a journey (the agent's eyes) the label is the way in
        r = await d.run_tool("a", "tap", {"target": {"text": "Settings gear"}})
        assert r["text"].startswith("Tapped e2")
        await finish(stop, task)
    run(go)


def test_lows_the_gif_samples_a_long_path_and_the_report_says_when_the_frame_cap_stopped_pictures(tmp_path, monkeypatch):
    frames = []
    for i in range(12):
        p = tmp_path / f"{i}.jpg"
        noisy(p, i)
        frames.append(journey.Frame(p, "x", None, (400.0, 800.0), 1))
    monkeypatch.setattr(journey_report, "GIF_MAX_FRAMES", 5)
    journey_report.make_gif(frames, tmp_path / "s.gif")
    assert Image.open(tmp_path / "s.gif").n_frames <= 5
    j = new_journey(tmp_path)
    j.over_cap = 3
    assert "3 pictures not kept: a report holds at most 200." in journey_report.model(j, True, None, 1.0, {}, tmp_path)["notes"]


def test_lows_a_cancelled_start_leaves_no_journey_and_no_folder(reports):
    import asyncio

    async def go():
        d = daemon()
        stop, task = await started(d)

        async def cancelled(*a, **k):
            raise asyncio.CancelledError
        d._calls_of_its_own = cancelled
        with pytest.raises(asyncio.CancelledError):
            await d.run_tool("a", "journey", {"start": True, "goal": "g", "conditions": {"network": "3g"}})
        assert d._journeys == {} and "a" not in journey.running
        assert not reports.exists() or not list(reports.iterdir())
        await finish(stop, task)
    run(go)


def test_codex_pr7_c1_double_tap_does_not_touch_again_in_an_app_the_agent_was_not_given(reports):
    """The first touch opens a link in Safari: the second touch is not made, in personal mode scoped to another app."""
    class Linking(DescribingSession):
        async def tap(self, target, hold_ms=60):
            done = await super().tap(target, hold_ms)
            self.foreground, self.app = "com.apple.mobilesafari", ("Safari", "com.apple.mobilesafari")
            return done

    async def go():
        session = Linking()
        session.tree = list(HOME)
        session.foreground = "com.example.app"
        d = make(session)
        stop, task = await started(d)
        scope = Scope(("com.example.app",))
        await d.run_tool("a", "journey", {"start": True, "goal": "g"}, scope=scope)
        session.tap_points.clear()
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "journey", {"stress": "double_tap", "target": {"x": 200, "y": 500}}, scope=scope)
        assert e.value.code == "not_allowed" and "outside_apps" in str(e.value) and "com.apple.mobilesafari" in str(e.value)
        assert len(session.tap_points) == 1
        await finish(stop, task)
    run(go)


def test_codex_pr7_c1_a_picture_taken_while_the_app_in_front_changes_is_not_kept(reports):
    """Personal mode: the picture is a screenshot between two reads of the app in front, kept only when both name the same
    given app. A switch while it is taken (to an unlisted app, or back and forth) drops it; a frame of the stream, which
    could come from before the check or from another app, is never used."""
    async def go():
        session = DescribingSession()
        session.tree = list(HOME)
        session.foreground = "com.example.app"
        d = make(session)
        stop, task = await started(d)
        scope = Scope(("com.example.app",))
        await d.run_tool("a", "journey", {"start": True, "goal": "g"}, scope=scope)
        jr = d._journeys["a"]
        kept, left = len(jr.frames), jr.left_out
        shoot = session.screenshot_png

        async def switching():
            session.foreground, session.app = "com.other.app", ("Other", "com.other.app")  # the person switches meanwhile
            return await shoot()
        session.screenshot_png = switching

        async def no_stream():
            raise AssertionError("personal mode never takes a stream frame for the report")
        d._newest_frame = no_stream
        await d._journey_frame(jr, "tap", "Tapped Log meal\nApp: Example (com.example.app)", scope)
        assert len(jr.frames) == kept and jr.left_out == left + 1
        # in front of the call already: dropped before any picture is taken
        await d._journey_frame(jr, "tap", "Tapped Log meal\nApp: Example (com.example.app)", scope)
        assert len(jr.frames) == kept and jr.left_out == left + 2
        session.foreground, session.app = "com.example.app", ("Example", "com.example.app")
        session.screenshot_png = shoot
        await d._journey_frame(jr, "tap", "Tapped Log meal\nApp: Example (com.example.app)", scope)
        assert len(jr.frames) == kept + 1  # the given app in front throughout: its picture is kept
        await finish(stop, task)
    run(go)


def test_codex_pr7_c1_a_call_made_while_a_stress_moment_waits_for_the_phone_still_goes_through_the_gate(reports):
    """`open_url` of the same client, sent while the journey's own network call awaits the phone, is refused as ever."""
    import asyncio

    async def go():
        session = DescribingSession()
        session.tree = list(HOME)
        session.asked, session.go_on = asyncio.Event(), asyncio.Event()
        phone_set_network = session.device.set_network

        async def slow_set_network(mode):  # the phone takes its time
            session.asked.set()
            await session.go_on.wait()
            return await phone_set_network(mode)
        session.device.set_network = slow_set_network
        d = make(session)
        stop, task = await started(d)
        await d.run_tool("a", "journey", {"start": True, "goal": "g", "allow": []})
        jr = d._journeys["a"]
        stress = asyncio.ensure_future(d.run_tool("a", "journey", {"stress": "network", "mode": "3g"}))
        await asyncio.wait_for(session.asked.wait(), 5)
        calls = jr.calls
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "open_url", {"url": "myapp://x"})
        assert e.value.code == "not_allowed" and e.value.args[0].startswith("blocked: in a journey, open_url is refused")
        assert jr.calls == calls and jr.shortcuts == []  # refused at the gate: no shortcut slipped through
        session.go_on.set()
        r = await stress
        assert r["text"].splitlines()[0] == "Stress: network 3g."
        await finish(stop, task)
    run(go)


def test_codex_pr7_c2_a_stop_sent_while_a_network_stress_waits_for_the_phone_ends_the_condition_too(reports):
    """Codex, PR #7 cycle 2: the stop waits for the stress moment to finish, so it sees the condition the stress set and
    ends it; the phone is not left on 3g after the journey has stopped."""
    import asyncio

    async def go():
        session = DescribingSession()
        session.tree = list(HOME)
        session.asked, session.go_on = asyncio.Event(), asyncio.Event()
        phone_set_network = session.device.set_network

        async def slow_set_network(mode):  # the phone takes its time with the first one
            if mode != "normal" and not session.go_on.is_set():
                session.asked.set()
                await session.go_on.wait()
            return await phone_set_network(mode)
        session.device.set_network = slow_set_network
        d = make(session)
        stop, task = await started(d)
        await d.run_tool("a", "journey", {"start": True, "goal": "g"})
        stress = asyncio.ensure_future(d.run_tool("a", "journey", {"stress": "network", "mode": "3g"}))
        await asyncio.wait_for(session.asked.wait(), 5)
        stopping = asyncio.ensure_future(d.run_tool("a", "journey", {"stop": True, "reached": False}))
        await asyncio.sleep(0.05)
        assert not stopping.done()  # it waits for the stress moment
        session.go_on.set()
        await stress
        await stopping
        assert "a" not in d._journeys
        assert d._network is None  # the stop ended the condition the stress had set
        await finish(stop, task)
    run(go)


def test_codex_pr7_c2_a_report_picture_is_taken_in_one_turn_of_the_phones_queue(reports):
    """Codex, PR #7 cycle 2: the two reads of the app in front and the screenshot are one turn of the queue, so an action
    queued meanwhile (to another app and back) runs after them, and the picture is of the given app."""
    import asyncio

    async def go():
        session = DescribingSession()
        session.tree = list(HOME)
        session.foreground = "com.example.app"
        d = make(session)
        stop, task = await started(d)
        scope = Scope(("com.example.app",))
        await d.run_tool("a", "journey", {"start": True, "goal": "g"}, scope=scope)
        jr = d._journeys["a"]
        kept = len(jr.frames)
        shoot, seen, front = session.screenshot_png, [], session.foreground_bundle_id

        async def slow_front():  # the read takes a moment: an action is queued meanwhile
            await asyncio.sleep(0.02)
            return await front()
        session.foreground_bundle_id = slow_front

        async def recording_shot():
            seen.append(session.foreground)
            return await shoot()
        session.screenshot_png = recording_shot
        later = []

        async def to_back():
            session.foreground = "com.example.app"

        async def to_other():  # an action to another app; the one that comes back is queued only when this has run
            session.foreground = "com.other.app"
            later.append(asyncio.ensure_future(d.control.agent(to_back)))
        said = "Tapped Log meal" + chr(10) + "App: Example (com.example.app)"
        picture = asyncio.ensure_future(d._journey_frame(jr, "tap", said, scope))
        await asyncio.sleep(0.005)  # the first read of the app in front is under way
        switching = asyncio.ensure_future(d.control.agent(to_other))
        await picture
        await switching
        await asyncio.gather(*later)
        assert seen == ["com.example.app"] and len(jr.frames) == kept + 1
        await finish(stop, task)
    run(go)


def test_codex_pr7_c1_background_resumes_the_running_app_and_relaunch_keeps_the_journeys_conditions(reports):
    async def go():
        d = daemon(FOOD)
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": "com.example.app", "look": False})
        await d.run_tool("a", "journey", {"start": True, "goal": "g", "conditions": {"language": "ar"}})
        assert d.session.launch_args[-1] == ["-AppleLanguages", "(ar)"] and "com.example.app" in d._overridden
        d.session.closed.clear()
        d.session.launch_args.clear()
        await d.run_tool("a", "journey", {"stress": "background"})
        assert d.session.closed == []  # the app was not closed to be started again with the defaults
        assert d.session.launch_args == [[]] and "com.example.app" in d._overridden  # brought to the front as it is
        d.session.launch_args.clear()
        await d.run_tool("a", "journey", {"stress": "relaunch"})
        assert d.session.closed and set(d.session.closed) == {"com.example.app"}  # (a launch with conditions closes it first, too)
        assert d.session.launch_args == [["-AppleLanguages", "(ar)"]]  # a new start with the journey's conditions
        await d.run_tool("a", "journey", {"stop": True, "reached": True})
        await finish(stop, task)
    run(go)


def test_cr_export_uploads_the_screen_pictures_the_report_links_and_only_those(report_folder):
    (report_folder / "screens/002.jpg").write_bytes(b"\x89data")
    (report_folder / "report.md").write_text("1. Home\n   ![screen 1](screens/001.jpg)\n", "utf-8")
    names = [p.relative_to(report_folder).as_posix() for p in journey_export.files_of(report_folder)]
    assert "screens/001.jpg" in names and "screens/002.jpg" not in names
    assert "report.md" in names and "findings/finding-1.jpg" in names


def test_cr_every_switch_of_the_eyes_is_in_the_report(reports):
    async def go():
        d = daemon()
        stop, task = await started(d)
        await d.run_tool("a", "journey", {"start": True, "goal": "g", "eyes": "agent"})
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "journey", {"eyes": "user"})
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "journey", {"eyes": "agent"})
        await d.run_tool("a", "journey", {"stop": True, "reached": True})
        text = (only_folder(reports) / "report.md").read_text("utf-8")
        assert "the agent's eyes until call 1, then a user's eyes until call 2, then the agent's eyes" in text
        await finish(stop, task)
    run(go)


def test_cr_the_eyes_report_keeps_its_wording_for_the_simple_cases(tmp_path):
    j = journey.Journey("g", None, {}, (), "user", 0.0, __import__("datetime").datetime(2026, 1, 1), tmp_path)
    j.eyes, j.eyes_switches = "agent", [(4, "agent")]
    facts = dict(journey_report.model(j, True, None, 1.0, {}, tmp_path)["facts"])
    assert facts["Seen through"] == "a user's eyes until call 4, then the agent's eyes"
    j.eyes, j.eyes_switches = "user", [(2, "user")]  # started in the agent's eyes, then a user's
    facts = dict(journey_report.model(j, True, None, 1.0, {}, tmp_path)["facts"])
    assert facts["Seen through"] == "the agent's eyes until call 2, then a user's eyes"


def test_cr_a_start_whose_undo_raises_still_leaves_no_journey_registered(reports):
    async def go():
        d = daemon()
        stop, task = await started(d)

        async def broken(*a, **k):
            raise RuntimeError("undo broke")

        async def fails(*a, **k):
            raise RuntimeError("start broke")
        d._calls_of_its_own = fails
        d._journey_undo = broken
        with pytest.raises(RuntimeError, match="undo broke"):
            await d.run_tool("a", "journey", {"start": True, "goal": "g"})
        assert d._journeys == {} and "a" not in journey.running
        assert not reports.exists() or not list(reports.iterdir())
        await finish(stop, task)
    run(go)


def test_cr_a_later_visit_to_a_known_screen_counts_wrong_turns_and_dead_ends_on_its_own(tmp_path):
    j = new_journey(tmp_path)
    home, food = full(("button", "Log meal"), ("text", "Today")), full(("header", "Add food"), ("input", "Search"))
    j.note_call("look", {}, home, 0, None)
    j.note_call("tap", {}, food, 0, None)
    j.note_call("scroll", {}, "Same screen.", 0, None)
    j.note_call("type_text", {"text": "a"}, "Same screen.", 0, None)  # worked here at the first visit
    j.note_call("back", {}, home, 0, None)
    assert (j.wrong_turns, j.dead_ends) == (0, 0)
    j.note_call("tap", {}, food, 0, None)  # a second visit: nothing done, and it is not a dead end
    j.note_call("back", {}, home, 0, None)
    assert (j.wrong_turns, j.dead_ends) == (1, 0)
    assert j.screens[1].acted  # the report still says something was done there

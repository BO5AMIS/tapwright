"""1.9.0, CodeRabbit's first cycle on PR #22: each test fails on 2335837."""
import asyncio
import dataclasses
import json
import threading

import pytest
from fakes import button, el
from mapfakes import APP, MapSession, screen
from test_daemon import KEYBOARD_BAR, KEYBOARD_NOTE, finish, keyboard_up, make, run, started
from test_tokens_190 import multiline, saved, walk_to_form
from test_tokens_190 import mapped as mapped_trip

from tapwright import autoroute, diagnostics, mapfile, mapkeeper, routes as routelib
from tapwright.control import Refused

BUNDLE = APP[1]


# ---- 4628457978, 4628457987: scrolls ----

def test_a_scroll_a_route_cant_replay_breaks_the_way():
    c = autoroute.Chain("app")
    c.begin("list")
    c.scrolled(autoroute.scroll_step({"to": "target", "target": {"text": "Reykjavik"}}))
    assert c.broken_at == 0
    assert c.moved("detail", {"tool": "tap", "target": {"id": "dest-card-reykjavik"}}) is None


def test_a_scroll_keeps_its_direction_and_amount_and_a_bare_one_scrolls_down():
    assert autoroute.scroll_step({"to": "end", "direction": "right", "amount": 0.3}) == {
        "tool": "scroll", "to": "end", "direction": "right", "amount": 0.3}
    assert autoroute.scroll_step({"to": "end"}) == {"tool": "scroll", "to": "end"}
    assert autoroute.scroll_step({}) == {"tool": "scroll", "direction": "down"}


# ---- 4628457999: a `then` that fails isn't the route's failure ----

def test_a_failing_then_after_the_route_arrived_counts_as_the_route_working(tmp_path):
    async def go():
        d = mapped_trip(tmp_path)
        stop, task = await started(d)
        await walk_to_form(d)
        for _ in range(2):
            d.session.at = "home"
            await d.run_tool("a", "look", {"screenshot": False})
            with pytest.raises(Refused):
                await d.run_tool("a", "drive", {"route": "plan_trip", "then": [{"tool": "tap", "target": {"id": "nowhere"}}]})
        kept = saved(tmp_path)
        assert "plan_trip" in kept and kept["plan_trip"]["failed"] == 0 and kept["plan_trip"]["used"] == 2
        await finish(stop, task)
    run(go)


# ---- 4628458008: covered only when the match lies over the keyboard ----

def test_a_match_clipped_above_the_keyboard_is_not_covered_by_it():
    async def go():
        d, stop, task = await keyboard_up()
        view = d.control.client("a")
        d.session.offscreen = [button("e8", "Above", "above", rect=(10, 440, 100, 40)),  # past a short list, above the keys
                               button("e9", "Below", "below", rect=(10, 620, 100, 40))]
        from tapwright import ui
        assert not d._offscreen_under_keyboard(view, ui.Target(id="above"))
        assert d._offscreen_under_keyboard(view, ui.Target(id="below"))
        await finish(stop, task)
    run(go)


# ---- 4628458016: route writes off the event loop ----

def test_a_route_is_written_on_a_worker_thread(tmp_path, monkeypatch):
    threads = []
    plain = mapfile.MapFile.write_route

    def watched(self, name, route):
        threads.append(threading.current_thread() is threading.main_thread())
        return plain(self, name, route)
    monkeypatch.setattr(mapfile.MapFile, "write_route", watched)

    async def go():
        d = mapped_trip(tmp_path)
        stop, task = await started(d)
        await walk_to_form(d)
        assert threads and not any(threads)  # never on the event loop's thread
        assert "plan_trip" in d.maps.data(str(tmp_path), BUNDLE)["routes"]  # and the map in memory knows it
        await finish(stop, task)
    run(go)


# ---- 4628458030: a held route is never evicted ----

def test_eviction_skips_a_route_a_schedule_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(autoroute, "MAX_AUTO", 2)

    async def go():
        d = mapped_trip(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": BUNDLE})
        for target in ("open-list", "dest-card-kyoto"):
            await d.run_tool("a", "tap", {"target": {"id": target}})
        d._route_held = lambda name: "schedule \"daily\" runs it" if name == "open_list" else None  # (scheduled since)
        await d.run_tool("a", "tap", {"target": {"id": "plan-trip"}})  # a third route: one must go
        kept = saved(tmp_path)
        assert "open_list" in kept and "plan_trip" in kept and "dest_card_kyoto" not in kept
        await finish(stop, task)
    run(go)


def test_a_new_route_waits_when_only_held_routes_could_go(tmp_path, monkeypatch):
    monkeypatch.setattr(autoroute, "MAX_AUTO", 2)

    async def go():
        d = mapped_trip(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": BUNDLE})
        for target in ("open-list", "dest-card-kyoto"):
            await d.run_tool("a", "tap", {"target": {"id": target}})
        d._route_held = lambda name: "held" if name in ("open_list", "dest_card_kyoto") else None
        await d.run_tool("a", "tap", {"target": {"id": "plan-trip"}})
        assert set(saved(tmp_path)) == {"open_list", "dest_card_kyoto"}
        await finish(stop, task)
    run(go)


# ---- 4628458043: results only for the version that ran ----

def test_a_result_isnt_applied_to_a_route_replaced_since(tmp_path):
    async def go():
        d = mapped_trip(tmp_path)
        stop, task = await started(d)
        await walk_to_form(d)
        root = str(tmp_path)
        file, data = d.maps.file(root, BUNDLE), d.maps.data(root, BUNDLE)
        ran = file.route_stamp(data, "plan_trip")
        assert ran is not None
        assert await d._auto_counted(root, BUNDLE, "plan_trip", False, ran + 1) is None  # another version
        assert saved(tmp_path)["plan_trip"]["failed"] == 0
        await d._auto_counted(root, BUNDLE, "plan_trip", False, ran)  # the version that ran
        assert saved(tmp_path)["plan_trip"]["failed"] == 1
        await finish(stop, task)
    run(go)


# ---- 4628458052: a run that typed with look: false ----

def test_a_run_that_types_without_look_breaks_the_way(tmp_path):
    async def go():
        screens = {"home": screen("Home", ids=("open-form", "h1"), tab="home"),
                   "form": screen("Search", ids=("go-on", "f1"), inputs=("query",)),
                   "inside": screen("Results", ids=("i1", "i2"))}
        d = make(MapSession(screens, "home", {("home", "open-form"): "form", ("form", "go-on"): "inside"}))
        d.maps = mapkeeper.Maps(str(tmp_path))
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": BUNDLE})
        await d.run_tool("a", "tap", {"target": {"id": "open-form"}})
        await d.run_tool("a", "run", {"steps": [{"tool": "type_text", "target": {"id": "query"}, "text": "x"}], "look": False})
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "tap", {"target": {"id": "go-on"}})
        assert "go_on" not in saved(tmp_path)
        await finish(stop, task)
    run(go)


# ---- 4628458062: a person's route is never carried into the tool's ----

def test_a_persons_route_of_plain_taps_isnt_carried_into_a_route_of_the_tools(tmp_path):
    async def go():
        d = mapped_trip(tmp_path)
        stop, task = await started(d)
        await walk_to_form(d)
        root = str(tmp_path)
        data = d.maps.data(root, BUNDLE)
        theirs = json.loads((tmp_path / BUNDLE / "routes" / "dest_card_kyoto.json").read_text("utf-8"))
        person = routelib.route("to_kyoto", BUNDLE, theirs["start"], theirs["end"], [], theirs["steps"])
        d.maps.file(root, BUNDLE).write_route("to_kyoto", person)
        d.maps.edit(root, BUNDLE, lambda m: m["routes"].__setitem__("to_kyoto", "routes/to_kyoto.json"))
        for name in ("plan_trip",):
            (tmp_path / BUNDLE / "routes" / f"{name}.json").unlink()
            d.maps.edit(root, BUNDLE, lambda m, n=name: m["routes"].pop(n, None))
        d.session.at = "home"
        d.control.clients.clear()
        await d.run_tool("b", "open_app", {"app": BUNDLE})
        await d.run_tool("b", "drive", {"route": "to_kyoto"})
        await d.run_tool("b", "tap", {"target": {"id": "plan-trip"}})
        assert "plan_trip" not in saved(tmp_path)  # the chain wasn't carried through the person's route
        await finish(stop, task)
    run(go)


# ---- 4628458068: the known-way hint's name goes through the redaction ----

def test_the_known_way_hint_never_names_a_route_the_redaction_changes(tmp_path):
    async def go():
        d = mapped_trip(tmp_path)
        stop, task = await started(d)
        await walk_to_form(d)
        d.scrub_everywhere = lambda text: text.replace("plan_trip", "<tester2's username>")
        d.session.at = "home"
        d.control.clients.clear()
        answers = await walk_to_form(d)
        assert not any("this way is known" in a["text"] and "plan_trip" in a["text"] for a in answers)
        await finish(stop, task)
    run(go)


# ---- 4628458094: a Done in the keyboard's window but no accessory ----

def test_a_sheet_footer_in_the_keyboards_window_is_no_accessory():
    from xml.etree import ElementTree as ET
    from tapwright import ui

    def node(kind, **attrs):
        return ET.Element(f"XCUIElementType{kind}", {"type": f"XCUIElementType{kind}", "enabled": "true",
                                                      **{k: str(v) for k, v in attrs.items()}})
    app = node("Application", bundleId=BUNDLE, label="Example", x=0, y=0, width=400, height=800)
    kwin = node("Window", x=0, y=0, width=400, height=800)
    host = node("Other", x=0, y=0, width=400, height=800)
    sheet = node("Other", x=0, y=300, width=400, height=200)  # a sheet's body, its footer on the keys' edge
    sheet.append(node("Button", label="Done", name="sheet-done", x=20, y=458, width=360, height=42))
    holder = node("Other", x=0, y=500, width=400, height=300)
    holder.append(node("Keyboard", x=0, y=500, width=400, height=300))
    host.extend([sheet, holder])
    kwin.append(host)
    app.append(kwin)
    tree = ui.read(ET.tostring(app, encoding="unicode"), (400.0, 800.0))
    done = next(e for e in tree.items if e.label == "Done")
    assert done.by_keyboard is False


# ---- the review body's minors ----

def test_a_stale_done_note_is_never_told_by_a_later_action():
    async def go():
        d, stop, task = await keyboard_up(shown=False)
        view = d.control.client("a")
        view.done_taken_back = "the keyboard's Done key changed something earlier"
        r = await d.run_tool("a", "tap", {"target": {"id": "save"}})
        assert "changed something earlier" not in r["text"]
        await finish(stop, task)
    run(go)


def test_an_unvouched_entry_never_makes_an_old_error_new(tmp_path):
    async def go():
        d = mapped_trip(tmp_path)
        stop, task = await started(d)
        ring = d._rings.setdefault(BUNDLE, diagnostics.LogRing())
        ring.add(diagnostics.LogEntry(1_800_000_000_000, "error", "old"))
        await d.run_tool("a", "look", {"screenshot": False})  # the session's mark: after the old error
        ring.add(diagnostics.LogEntry(1_800_000_000_001, "info", "not yet the app's"), vouched=False)
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert "Hint:" not in r["text"]
        await finish(stop, task)
    run(go)


def test_a_route_file_that_cant_be_deleted_is_never_said_dropped(tmp_path, monkeypatch):
    async def go():
        d = mapped_trip(tmp_path)
        stop, task = await started(d)
        await walk_to_form(d)
        file = d.maps.file(str(tmp_path), BUNDLE)
        from pathlib import Path
        plain = Path.unlink

        def stuck(self, *a, **k):
            if self.name == "plan_trip.json":
                raise PermissionError("in use")
            return plain(self, *a, **k)
        monkeypatch.setattr(Path, "unlink", stuck)
        assert d._drop_route_io(file, "plan_trip") is False
        assert "plan_trip" in file.load()["routes"]
        await finish(stop, task)
    run(go)


def test_a_field_that_had_no_done_doesnt_stop_another_field_on_the_screen():
    async def go():
        d, stop, task = await keyboard_up(blank_tap_closes=False, dismiss_ok=False, done_key=True)
        s = d.session
        with pytest.raises(Refused):  # field A is one line: no Done
            await d.run_tool("a", "tap", {"target": {"id": "low"}})
        multiline(s)  # the same screen, now a multiline field has the focus
        await d.run_tool("a", "look", {"screenshot": False})
        with pytest.raises(Refused):
            await d.run_tool("a", "tap", {"target": {"id": "low"}})
        assert ("Done",) in s.dismiss_keys
        await finish(stop, task)
    run(go)


def test_visible_reads_each_route_once(tmp_path, monkeypatch):
    file = mapfile.MapFile(str(tmp_path), BUNDLE)
    reads = []

    def read(rel, hide=True):
        reads.append(rel)
        return {"auto": True} if len(reads) == 1 else None
    monkeypatch.setattr(file, "_route_file", read)
    token = mapfile.HIDE_AUTO.set(True)
    try:
        assert file.visible({"routes": {"a": "routes/a.json"}}) == []
    finally:
        mapfile.HIDE_AUTO.reset(token)
    assert len(reads) == 1


def test_an_older_maps_title_named_place_never_names_a_route():
    data = {"places": {"kyoto_detail": {}}}
    assert autoroute.base_name(data, "kyoto_detail", {"tool": "tap", "target": {"label": "Kyoto"}}) == "place"
    assert autoroute.base_name({"places": {"place_4": {}}}, "place_4", {"tool": "back"}) == "place_4"


def test_the_line_break_is_taken_back_by_what_the_keyboard_shows_not_the_runners_word():
    async def go():
        d, stop, task = await keyboard_up(blank_tap_closes=False, dismiss_ok=False)
        s = d.session
        multiline(s)
        field = next(e for e in s.tree if e.identifier == "a")
        await d.run_tool("a", "look", {"screenshot": False})

        async def says_closed_but_isnt(keys=()):
            s._record("dismiss_keyboard")
            if keys:
                field.value += "\n"
                return True  # the runner said so; the keyboard is still up
            return False
        s.dismiss_keyboard = says_closed_but_isnt

        async def backspace(name):
            s._record(f"key:{name}")
            field.value = field.value[:-1]
            return f"pressed {name}"
        s.key = backspace
        with pytest.raises(Refused):
            await d.run_tool("a", "tap", {"target": {"id": "low"}})
        assert s.calls.count("key:backspace") == 1 and field.value == "Bench trip"
        await finish(stop, task)
    run(go)


# ---- the lead's ruling on 4628458079: a multiline Done only for a tap that submits anyway ----

@pytest.mark.parametrize("label", ["Low", "Send", "Delete draft", "Buy now"])
def test_a_multiline_done_is_never_pressed_for_a_target_that_isnt_a_submit_the_floor_allows(label):
    async def go():
        d, stop, task = await keyboard_up(blank_tap_closes=False, dismiss_ok=False, done_key=True)
        s = d.session
        multiline(s, low=label)  # a chat composer's Send, a plain button, a held word
        await d.run_tool("a", "look", {"screenshot": False})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "low"}})
        assert e.value.code == "not_actionable" and str(e.value).startswith("covered:")
        assert ("Done",) not in s.dismiss_keys
        await finish(stop, task)
    run(go)


@pytest.mark.parametrize("label", ["Save trip", "Submit", "Continue", "Confirm", "Sign in"])
def test_a_multiline_done_closes_the_keyboard_for_a_submit_button(label):
    async def go():
        d, stop, task = await keyboard_up(blank_tap_closes=False, dismiss_ok=False, done_key=True)
        s = d.session
        multiline(s, low=label)
        await d.run_tool("a", "look", {"screenshot": False})
        plain = s.dismiss_keyboard

        async def done_closes(keys=()):
            closed = await plain(keys)
            if closed:
                s.tree = [dataclasses.replace(e, covered_by=()) if e.identifier == "low" else e for e in s.tree]
            return closed
        s.dismiss_keyboard = done_closes
        r = await d.run_tool("a", "tap", {"target": {"id": "low"}})
        assert s.dismiss_keys == [("Done",)] and r["text"].endswith(KEYBOARD_NOTE)
        await finish(stop, task)
    run(go)

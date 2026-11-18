"""CodeRabbit's first review of PR #20 (1.8.0, "the Explorer"), fixed by W170: the floor's policy from `serve --policy`, the
host's dialog and progress, what a repair adopts and publishes, the trial and the check replay of a repair, the explorer's typed
screens, toggles, scans and first-visit findings, and the report's files. Each test fails on b70d47a."""
import asyncio
import dataclasses
import json
import os
import types
from pathlib import Path

import pytest
from explorefakes import FakePhone, build, grid, identity, standard
from fakes import button, el
from mapfakes import screen
from test_daemon import finish, make, run, started
from test_daemon_repair import Flow, phone, repair_lines, replay, script, tap, title

from tapwright import (cli, client, explore, explored, explorefloor, explorereport, exploretool, findings, hostdialog, mapfile,
                       places, policy as policylib, repairanalysis as ra, scriptrepair, ui)
from tapwright.control import Refused
from tapwright.explore import Fill, PhoneStop

APPID = "com.example.app"
SCREEN = (400.0, 800.0)
FLOOR = explorefloor.Floor(policylib.DEFAULT, APPID)


def explore_run(explorer, maps=None) -> explore.Result:
    async def go() -> explore.Result:
        result = await explorer.run()
        if maps is not None:
            await maps.flush()
        return result
    return asyncio.run(go())


# ---- cli.py: `serve --policy` and `mcp --policy` reach the skip floor ----

def test_serve_and_mcp_policy_files_reach_the_floor_and_nothing_else(tmp_path, monkeypatch):
    from test_ceiling import run_cli
    from tapwright import mcp_server
    rules = tmp_path / "policy.json"
    rules.write_text(json.dumps({"explore": {"skip": ["Archive"]}, "block": ["Wipe"]}), "utf-8")
    _, got = run_cli(monkeypatch, "--policy", str(rules))
    assert got["ceiling"].policy is not None and got["ceiling"].policy.explore.skip == ("Archive",)
    seen: dict = {}
    monkeypatch.setattr(mcp_server, "run", lambda **kw: seen.update(kw))
    monkeypatch.setattr("sys.argv", ["tapwright", "mcp", "--policy", str(rules)])
    assert cli.main() == 0
    assert seen["rules"] is not None and seen["rules"].block == ("Wipe",)


# ---- cli.py: adoption installs only the checked bytes, and a failed one fails the command ----

def _files(tmp_path):
    script_path = tmp_path / "sign_in.json"
    script_path.write_text(json.dumps({"format": "agent-tools-script", "version": 1, "name": "sign_in", "app": APPID,
                                       "steps": [{"tool": "tap", "target": {"id": "old"}}]}), "utf-8")
    (tmp_path / "sign_in.repaired.json").write_text(json.dumps({"format": "agent-tools-script", "version": 1, "name": "sign_in",
                                                                "app": APPID, "steps": [{"tool": "tap", "target": {"id": "new"}}]}), "utf-8")
    return script_path


def test_adopt_installs_only_the_bytes_the_run_checked(tmp_path, monkeypatch, capsys):
    from tapwright import store
    script_path = _files(tmp_path)
    monkeypatch.setattr(store, "HOME", tmp_path / "home")
    monkeypatch.setattr(client, "set_scope", lambda *a, **k: None)

    def call(tool, args, client_id, timeout=120.0):
        sha = scriptrepair.digest((tmp_path / "sign_in.repaired.json").read_bytes())
        # another apply run writes its own repaired file after this one checked its own
        (tmp_path / "sign_in.repaired.json").write_text('{"format": "agent-tools-script", "version": 1, "name": "sign_in", '
                                                        '"app": "com.example.app", "steps": [{"tool": "tap", "target": '
                                                        '{"id": "other"}}]}', "utf-8")
        return {"text": "Ran 1 of 1 steps in 1.0 s, with 1 repair (not adopted).\nRepaired: step 1 tap id=old to id=new.\n"
                        f"Checked: replayed sign_in.repaired.json from the script's fresh start: all 1 steps passed (sha256 {sha}).\n"
                        "Wrote sign_in.repaired.json and sign_in.repair.md. The original is unchanged."}
    monkeypatch.setattr(client, "call", call)
    import argparse
    ns = argparse.Namespace(files=[str(script_path)], junit=None, apps=APPID, dedicated=False, repair="apply", adopt=True)
    assert cli.replay(ns) == 2
    assert "isn't the file this run checked" in capsys.readouterr().out
    assert json.loads(script_path.read_text("utf-8"))["steps"][0]["target"] == {"id": "old"}


# ---- hostdialog.py: an ask goes to a call that can show a form; progress to the explore that asked, even with --no-dialog ----

class Ctx:
    def __init__(self, form: bool, token: object) -> None:
        self.request_context = types.SimpleNamespace(session=types.SimpleNamespace(check_client_capability=lambda c: form),
                                                     meta=types.SimpleNamespace(progress_token=token))
        self.elicited: list[str] = []
        self.progress: list[str] = []

    async def elicit(self, message, model):
        self.elicited.append(message)
        return None

    async def report_progress(self, n, message=None):
        self.progress.append(message)


def test_an_ask_goes_to_a_form_call_and_progress_to_the_explore_call():
    async def go():
        dialogs = hostdialog.HostDialogs(poll=lambda since, wait: {"events": [], "seq": since}, answer=lambda i, v: None)
        explore_ctx, form_ctx = Ctx(False, "tok"), Ctx(True, None)
        async with dialogs.during(explore_ctx, progress=True), dialogs.during(form_ctx):
            dialogs._handle({"type": "ask", "id": "a1", "message": "Allow Delete?", "kind": "confirm"})
            dialogs._handle({"type": "progress", "id": "p1", "message": "explored 3 places, 0 findings"})
            for _ in range(20):
                await asyncio.sleep(0.01)
        assert form_ctx.elicited == ["Allow Delete?"] and explore_ctx.elicited == []
        assert explore_ctx.progress == ["explored 3 places, 0 findings"] and form_ctx.progress == []
        off = hostdialog.HostDialogs(poll=lambda since, wait: {"events": [], "seq": since}, answer=lambda i, v: None, enabled=False)
        quiet = Ctx(False, "tok")
        async with off.during(quiet, progress=True) as dialog:
            assert dialog is False
            off._handle({"type": "progress", "id": "p2", "message": "explored 1 places, 0 findings"})
            for _ in range(20):
                await asyncio.sleep(0.01)
        assert quiet.progress == ["explored 1 places, 0 findings"]  # --no-dialog turns off the dialog, not progress
    asyncio.run(go())


# ---- mapfile.py: every record of one place is merged ----

def test_three_records_of_one_place_are_all_merged():
    t1, t2, t3 = "2026-11-14T09:00:00Z", "2026-11-14T10:00:00Z", "2026-11-14T11:00:00Z"

    def place():
        return {"key": {"signature": "s0", "parts": {"ids": ["s0"], "kinds": [], "tab": None, "layer": None}}, "link": None,
                "titles": {}, "last": t1}
    disk, mem = places.empty_map(APPID), places.empty_map(APPID)
    mem["titleKey"] = disk["titleKey"]
    disk["places"]["home"] = place()
    mem["places"]["place_2"], mem["places"]["place_3"] = place(), place()
    explored.mark_tried(disk, "home", "id:a", "same", "1", t1)
    explored.mark_tried(mem, "place_2", "id:b", "same", "1", t2)
    explored.mark_tried(mem, "place_3", "id:c", "same", "1", t3)
    merged = mapfile.merge(disk, mem)
    sigs = {a["sig"] for p in merged["places"].values() if "explored" in p for a in p["explored"]["actions"]}
    assert sigs == {"id:a", "id:b", "id:c"}


# ---- policy.py: an app's own list on one side never hides the other side's global words ----

def test_an_apps_own_list_never_hides_the_other_sides_global_words():
    daemon = policylib.parse({"block": ["Archive"]})
    call = policylib.parse({"apps": {APPID: {"block": []}}})
    assert policylib.combined(daemon, call).check(APPID, "Archive trip") == ("blocked", "Archive")
    daemon2 = policylib.parse({"apps": {APPID: {"block": ["Shred"]}}})
    call2 = policylib.parse({"block": ["Wipe"]})
    both = policylib.combined(daemon2, call2)
    assert both.check(APPID, "Wipe all") == ("blocked", "Wipe") and both.check(APPID, "Shred") == ("blocked", "Shred")
    assert explorefloor.Floor(both, APPID).named("Wipe all") is not None


# ---- places.py: a named list whose rows' names have more words is still the same list ----

def test_rows_whose_names_have_more_words_are_still_one_named_list():
    base = {"ids": ["explore-search"], "lists": ["explore-list"], "kinds": [], "tab": None, "layer": None}
    a = {**base, "inner": ["dest-card", "dest-name"]}  # dest-card-kyoto, dest-name-kyoto
    b = {**base, "inner": ["dest-card-cape", "dest-name-cape"]}  # dest-card-cape-town, dest-name-cape-town
    assert places.similarity(a, b) == 1.0
    shared = {**base, "lists": ["main-scroll"]}
    assert not places.same_place({**shared, "inner": ["settings-row"]}, {**shared, "inner": ["profile-row"]})


# ---- explore.py: a switch whose restoring touch stops is named ----

def test_a_restoring_touch_that_stops_names_the_switch_left_changed(tmp_path):
    screens = {"home": grid("Home", buttons=(("go-prefs", "Prefs"),)),
               "prefs": grid("Prefs", switches=(("notify-switch", "Notifications"),), back="Home")}

    class Stops(FakePhone):
        async def toggle(self, target, value):
            if self.toggles:
                raise PhoneStop("paused")  # the owner paused just as the flip back went out
            return await super().toggle(target, value)
    phone_ = Stops(screens, "home", {("home", "go-prefs"): "prefs"}, {"prefs": "home"})
    explorer, maps = build(tmp_path, phone_)
    result = explore_run(explorer, maps)
    assert result.stopped == "toggle_not_restored" and result.detail == "id=notify-switch, was off"
    assert any("couldn't be set back (paused)" in n for n in result.notes)


# ---- explore.py: a place stays a frontier place until its scroll scan ends; an overflow marker clears ----

def test_a_run_that_stops_before_a_places_scroll_leaves_it_to_explore(tmp_path):
    screens = {"home": screen("Home", ids=("stay",))}
    screens["home"].below = 3
    first = FakePhone(screens, "home")
    explorer, maps = build(tmp_path, first, budget=explore.Budget(steps=1))
    assert explore_run(explorer, maps).stopped == "budget_steps" and first.scrolls == 0
    data = maps.data(str(tmp_path), APPID)
    home = next(iter(data["places"]))
    assert not explored.is_explored(data, home, identity().build)
    second = FakePhone(screens, "home")
    explorer2, maps2 = build(tmp_path, second)
    explore_run(explorer2, maps2)
    assert second.scrolls > 0  # the next run scans what lay below


def test_an_overflowing_frontier_clears_once_every_action_was_tried(tmp_path):
    data = places.empty_map(APPID)
    data["places"]["home"] = {"key": {"signature": "s0", "parts": {"ids": ["s0"], "kinds": [], "tab": None, "layer": None}},
                              "link": None, "titles": {}, "last": "2026-11-14T09:00:00Z"}
    sigs = [f"id:b{i}" for i in range(51)]
    explored.mark_seen(data, "home", sigs, "2026-11-14T09:00:00Z")
    for sig in sigs:
        explored.mark_tried(data, "home", sig, "same", "1", "2026-11-14T09:00:01Z")
    assert explored.cut_short(data, "home")  # (before: the marker stayed for ever)
    assert explored.scan_done(data, "home", []) and not explored.cut_short(data, "home") and explored.is_explored(data, "home", "1")
    # through a run: a place whose frontier kept the marker is expanded once more, and the marker goes
    screens = {"home": grid("Home", buttons=(("b1", "One"), ("b2", "Two")))}
    explorer, maps = build(tmp_path, FakePhone(screens, "home"))
    explore_run(explorer, maps)
    port = explore.MapsPort(maps, str(tmp_path), APPID)
    home = next(iter(port.data["places"]))
    port.data["places"][home]["explored"]["frontier"].append(explored.MORE)  # (as an older run left it)
    assert explored.cut_short(port.data, home)
    explorer2, _ = build(tmp_path, FakePhone(screens, "home"), port=port)
    explore_run(explorer2, maps)
    assert not explored.cut_short(port.data, home) and explored.is_explored(port.data, home, identity().build)


# ---- explore.py: where it typed, a saved road's tap that isn't a listed submit is never made ----

def test_a_road_from_a_screen_it_typed_on_taps_no_control_but_a_listed_submit(tmp_path):
    screens = {"home": grid("Home", buttons=(("go-form", "Form"),)),
               "form": grid("Form", buttons=(("b-preview", "Preview"),), inputs=("name-test-safe",), back="Home"),
               "preview": grid("Preview", buttons=(("p-1", "Nothing"),), back="Form")}
    goes = {("home", "go-form"): "form", ("form", "b-preview"): "preview"}
    back_to = {"form": "home", "preview": "form"}
    first = FakePhone(screens, "home", goes, back_to)
    explorer, maps = build(tmp_path, first)
    explore_run(explorer, maps)
    assert ("form", "b-preview") in first.touched  # the first run (no fill) saved the road form > preview
    second = FakePhone(screens, "home", goes, back_to)
    newer = dataclasses.replace(identity(), build="1.6.0 (230)")
    explorer2, maps2 = build(tmp_path, second, who=newer, fill=Fill("test-data"))
    explore_run(explorer2, maps2)
    assert second.typed and ("form", "b-preview") not in second.touched


# ---- explore.py: a first-visit finding has the way to it ----

def test_a_first_visit_finding_on_a_place_reached_by_a_tap_has_its_path(tmp_path):
    screens, goes, back_to = standard()
    phone_ = FakePhone(screens, "home", goes, back_to)
    phone_.audits["settings"] = ("button \"x\" has no label",)
    explorer, maps = build(tmp_path, phone_)
    result = explore_run(explorer, maps)
    a11y = next(f for f in result.findings if f.kind == "a11y")
    assert a11y.path_from == result.start_place and a11y.path and a11y.path[-1]["target"] == {"id": "go-settings"}


# ---- explorefloor.py: card words inside an id, and a sole Allow ----

def test_a_card_word_run_into_an_id_is_never_typed_into():
    field = ui.Element("e1", "input", "Number", "cardnumber-test-safe", rect=(0, 0, 100, 40))
    assert explorefloor.never_typed(field)
    assert explorefloor.never_typed(ui.Element("e2", "input", "New", "newpassword-test-safe", rect=(0, 0, 100, 40)))
    assert not explorefloor.never_typed(ui.Element("e3", "input", "Shipping", "shipping-test-safe", rect=(0, 0, 100, 40)))


def test_a_sole_allow_button_is_never_pressed_and_a_sole_ok_is(tmp_path):
    allow = ui.Alert("Notifications", ("Allow",), (0, 0, 10, 10))
    assert explorefloor.safe_button(allow, FLOOR) is None
    assert explorefloor.safe_button(ui.Alert("Saved", ("OK",), (0, 0, 10, 10)), FLOOR) == "OK"
    screens = {"home": grid("Home", buttons=(("b1", "One"),), alert=("Notifications", ("Allow",)))}
    phone_ = FakePhone(screens, "home")
    explorer, maps = build(tmp_path, phone_)
    explore_run(explorer, maps)
    assert phone_.answered == []


# ---- repairanalysis.py: role for a similar name; no private value in an inserted road ----

def test_a_similar_id_of_another_role_is_no_candidate_when_the_old_role_is_known():
    cell = el("c1", "cell", "Login row", "login-row", rect=(10, 700, 300, 40))
    assert "tap" in cell.actions
    seen = {"role": "button", "id_stem": "login"}
    steps = [{"tool": "tap", "target": {"id": "login-btn"}, "seen": seen, "expect": {"text": "Home"}}]
    found = ra.analyse(steps, 0, ra.Break(1, "not_found", "nothing"), ra.Screen([cell], SCREEN, FLOOR))
    assert found.kind == "none"


def test_a_road_move_that_names_a_private_value_is_never_inserted():
    scrub = lambda text: text.replace("12 Kestrel Lane", "<home's street>")  # noqa: E731
    road = [({"tool": "tap", "target": {"label": "12 Kestrel Lane"}}, {"label": "12 Kestrel Lane"})]
    steps = [{"tool": "tap", "target": {"id": "login-btn"}, "place": "welcome", "expect": {"text": "x"}}]
    found = ra.analyse(steps, 0, ra.Break(1, "not_found", "nothing"),
                       ra.Screen([], SCREEN, FLOOR, here="home", way=lambda a, b: road, scrub=scrub))
    assert found.kind == "none" and "test account's value" in found.reason


# ---- scriptrepairrun.py: the trial and the check ----

HOME = [title("Home"), button("h1", "Go", "go", rect=(10, 600, 100, 40))]
WELCOME = [title("Welcome"), button("e1", "Log in", "login-button", rect=(10, 600, 100, 40)),
           button("e2", "Help", "help-button", rect=(10, 300, 100, 40))]


def test_a_trial_whose_step_was_skipped_is_not_accepted(tmp_path, monkeypatch):
    helped = [title("Help"), button("x1", "Log in", "login-btn", rect=(10, 600, 100, 40))]
    road = scriptrepair.Candidate("road", "insert tap id=help-button", "before it: tap id=help-button", "a test's pick",
                                  moves=[{"tool": "tap", "target": {"id": "help-button"}}], place="welcome", place_from="x")
    monkeypatch.setattr(ra, "analyse", lambda *a, **k: scriptrepair.Analysis("ok", [road]))

    async def go():
        d = phone(tmp_path, Flow(WELCOME, {"help-button": helped, "login-btn": HOME}))
        stop, task = await started(d)
        step = {**tap({"id": "login-btn"}, expect={"text": "Home"}), "if": {"text": "Welcome"}}
        with pytest.raises(Refused):
            await replay(d, script(tmp_path, [step]), "apply")
        assert not (tmp_path / "sign_in.repaired.json").exists()
        assert any("skipped" in ln and "rejected" in ln for ln in repair_lines(tmp_path))
        await finish(stop, task)
    run(go)


class Scrolls(Flow):
    """A list that moves on at each scroll: `pages[n]` after n scrolls; opening the app shows the top again."""

    def __init__(self, tree, pages, goes=None):
        super().__init__(tree, goes)
        self.pages, self.at_page = pages, 0

    async def launch(self, bundle_id, arguments=None):
        self.at_page = 0
        return await super().launch(bundle_id, arguments)

    async def swipe(self, x0, y0, x1, y1, duration_ms=350, hold_ms=0):
        text = await super().swipe(x0, y0, x1, y1, duration_ms, hold_ms)
        if y1 < y0:  # a scroll down (the finger moves up)
            self.at_page = min(self.at_page + 1, len(self.pages) - 1)
        else:
            self.at_page = 0
        self.tree = list(self.pages[self.at_page])
        return text


def test_a_scroll_trial_makes_its_scrolls_once(tmp_path):
    lst = el("e1", "scroll", "", "trip-list", rect=(0, 100, 400, 500))
    top = [title("Trips"), lst, button("r1", "Trip 1", "trip-row-1", rect=(10, 150, 300, 40))]
    one = [title("Trips"), lst, button("r9", "Trip 9", "trip-row-9", rect=(10, 400, 300, 40))]
    two = [title("Trips"), lst, button("r20", "Trip 20", "trip-row-20", rect=(10, 400, 300, 40))]

    async def go():
        d = phone(tmp_path, Scrolls(top, [top, one, two], {"trip-row-9": HOME}))
        stop, task = await started(d)
        r = await replay(d, script(tmp_path, [tap({"id": "trip-row-9"}, expect={"text": "Home"})]), "apply")
        assert "with scroll down 1 time before it" in r["text"]
        await finish(stop, task)
    run(go)


def test_a_rejected_scroll_stops_the_repair_when_the_list_cant_be_shown_as_it_was(tmp_path, monkeypatch):
    lst = el("e1", "scroll", "", "trip-list", rect=(0, 100, 400, 500))
    top = [title("Trips"), lst, button("r1", "Trip 1", "trip-row-1", rect=(10, 150, 300, 40))]
    more = [title("Trips"), lst, button("r2", "Trip 2", "trip-row-1", rect=(10, 150, 300, 40))]  # another row, same id
    scroll = scriptrepair.Candidate("scroll", "scroll down", "before it: scroll down", "off screen")
    row = scriptrepair.Candidate("target", "tap id=trip-row-1", 'button "Trip 1"', "close", 0.9, target={"id": "trip-row-1"})
    monkeypatch.setattr(ra, "analyse", lambda *a, **k: scriptrepair.Analysis("ok", [scroll, row]))

    async def go():
        d = phone(tmp_path, Flow(top, {"scroll": more, "trip-row-1": HOME}))
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await replay(d, script(tmp_path, [tap({"id": "trip-row-7"}, expect={"text": "Home"})]), "apply")
        assert "the list couldn't be shown as it was before it" in str(e.value)
        assert d.session.calls.count("tap") == 0  # the row candidate read on the top of the list was never tapped below it
        await finish(stop, task)
    run(go)


def BUNDLE_OF():
    from test_daemon_repair import BUNDLE
    return BUNDLE


def test_the_check_gates_the_step_an_inserted_move_leads_to(tmp_path):
    welcome = [title("Welcome"), button("e1", "Skip", "skip-btn", rect=(10, 600, 100, 40))]
    home = [title("Home"), button("h1", "Delete trip", "trip-x", rect=(10, 600, 100, 40))]
    assert scriptrepair.under_repair([{"tool": "scroll", "repaired": {"inserted": True}}, {"tool": "tap"}], 1)
    assert not scriptrepair.under_repair([{"tool": "tap"}, {"tool": "tap"}], 1)

    async def check(d, steps):  # (as _check_repaired runs it, with the script's app as the floor's)
        from tapwright import batch, daemon
        from test_daemon_repair import BUNDLE
        data = batch.load_script(str(script(tmp_path, steps, name="checked")))
        gate = daemon._TOUCH_FLOOR.set(explorefloor.Floor(daemon.safety.NONE, BUNDLE))
        only = daemon._FLOOR_REPAIRED_ONLY.set(True)
        try:
            return await d._replay("a", data, None, {"look": False})
        finally:
            daemon._FLOOR_REPAIRED_ONLY.reset(only)
            daemon._TOUCH_FLOOR.reset(gate)

    async def go():
        d = phone(tmp_path, Flow(welcome, {"skip-btn": home, "trip-x": home}))
        stop, task = await started(d)
        steps = [{**tap({"id": "skip-btn"}), "repaired": {"inserted": True}}, tap({"id": "trip-x"})]
        with pytest.raises(Refused) as e:
            await check(d, steps)
        assert "needs_confirmation" in str(e.value) and "stopped at step 2" in str(e.value)
        await finish(stop, task)
    run(go)


def test_the_check_replay_runs_without_the_original_routes_identity(tmp_path):
    from tapwright import daemon as daemonlib
    seen: list[tuple] = []

    async def go():
        d = phone(tmp_path, Flow(WELCOME, {"login-button": HOME}))
        d._saved_route_name = lambda path, data: "sign_in" if Path(str(path)).name == "sign_in.json" else None
        real = d._replay

        async def spy(*a, **k):
            seen.append(daemonlib._ROUTES.get())
            return await real(*a, **k)
        d._replay = spy
        stop, task = await started(d)
        await replay(d, script(tmp_path, [tap({"id": "login-btn"}, expect={"text": "Home"}), tap({"id": "go"})]), "apply")
        await finish(stop, task)
    run(go)
    assert seen and "sign_in" not in seen[-1]


def test_a_repaired_file_that_cant_be_published_fails_the_apply(tmp_path, monkeypatch):
    def broken(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(scriptrepair, "keep_checked", broken)

    async def go():
        d = phone(tmp_path, Flow(WELCOME, {"login-button": HOME}))
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await replay(d, script(tmp_path, [tap({"id": "login-btn"}, expect={"text": "Home"}), tap({"id": "go"})]), "apply")
        assert "couldn't write the repaired files" in str(e.value) and str(e.value).startswith("Ran 2 of 2 steps")
        assert any(ln.endswith("rejected (couldn't write the repaired files (No space left on device))") for ln in repair_lines(tmp_path))
        await finish(stop, task)
    run(go)


# ---- scriptrepair.py: the first original, the earlier notes, and a non-fresh "fresh" ----

def test_a_backup_that_cant_be_copied_whole_is_never_the_first_original(tmp_path, monkeypatch):
    script_path = tmp_path / "s.json"
    script_path.write_text('{"v": "original"}', "utf-8")
    (tmp_path / "s.repaired.json").write_text('{"v": "repaired"}', "utf-8")
    real = Path.read_bytes

    def failing(self):
        if self == script_path:
            raise OSError(5, "I/O error")
        return real(self)
    monkeypatch.setattr(Path, "read_bytes", failing)
    ok, said = scriptrepair.adopt(script_path, lambda detail: True)
    assert not ok and not (tmp_path / "s.orig.json").exists()
    monkeypatch.setattr(Path, "read_bytes", real)
    ok, said = scriptrepair.adopt(script_path, lambda detail: True)
    assert ok and (tmp_path / "s.orig.json").read_text("utf-8") == '{"v": "original"}'


def test_the_earlier_repair_notes_come_back_when_the_new_file_cant_be_kept(tmp_path):
    script_path = tmp_path / "sign_in.json"
    script_path.write_text("{}", "utf-8")
    (tmp_path / "sign_in.repair.md").write_text("# the earlier notes", "utf-8")
    checked = scriptrepair.write_check(script_path, {"steps": []})
    blocker = tmp_path / "sign_in.repaired.json"
    blocker.mkdir()
    (blocker / "x").write_text("x", "utf-8")  # the rename can't go over a folder that holds a file
    with pytest.raises(OSError):
        scriptrepair.keep_checked(script_path, checked, "# new notes")
    assert (tmp_path / "sign_in.repair.md").read_text("utf-8") == "# the earlier notes"
    assert not list(tmp_path.glob("*.previous"))


def test_a_fresh_that_is_falsy_but_not_false_is_not_a_fresh_start():
    for value in (0, "", None):
        assert scriptrepair.why_not_fresh({"app": APPID, "fresh": value}) is not None, value
    assert scriptrepair.why_not_fresh({"app": APPID}) is None and scriptrepair.why_not_fresh({"app": APPID, "fresh": True}) is None


# ---- the minors ----

def test_the_busy_wait_counts_the_calls_own_time(monkeypatch, capsys):
    now = [0.0]
    calls = []

    def call(tool, args, client_id, timeout=120.0):
        calls.append(1)
        now[0] += 60.0  # each refused call takes a minute
        raise client.DaemonError("busy", "a person is using the phone; try again in a moment")
    monkeypatch.setattr(client, "call", call)
    for name in ("set_scope", "set_policy", "set_maps", "set_accounts"):
        monkeypatch.setattr(client, name, lambda *a, **k: None)
    import argparse
    a = argparse.Namespace(minutes=None, steps=None, depth=None, start=None, focus=None, fill=None, keep_going=False,
                           apps=APPID, dedicated=False, phone=False, policy=None, accounts=None, maps=None)

    def sleep(s):
        now[0] += s
    assert cli.explore(a, sleep=sleep, clock=lambda: now[0]) == 2
    assert now[0] <= cli.EXPLORE_WAIT + 60.0 and len(calls) <= 9


def test_a_screen_no_app_is_named_for_goes_no_further_than_the_adapter():
    d = make()
    tree = ui.Tree([button("e1", "Secret", "s1")], app=None)
    assert exploretool.DaemonPhone(d, "a", None, APPID)._own(tree).items == []


def test_a_non_finite_minutes_still_gives_a_finite_wait():
    import math
    for bad in (float("inf"), float("nan")):
        assert math.isfinite(exploretool.timeout_for({"minutes": bad}))


def test_a_finding_path_that_holds_a_private_value_is_refused():
    scrub = lambda text: text.replace("tester@example.com", "<tester's email>")  # noqa: E731
    base = dict(finding_id="f1", kind="js_error", app=APPID, app_version="1.0", build="7", tool="tapwright", tool_version="1.8.0",
                device=findings.Device("iPhone14,2", "iOS 26.5"), expected="x", got="y", scrub=scrub)
    with pytest.raises(findings.FindingError):
        findings.make(**base, picture_file="findings/tester@example.com.jpg")
    with pytest.raises(findings.FindingError):
        findings.make(**base, repro="repro/tester@example.com.json")
    assert findings.make(**base, picture_file="findings/f1.jpg").picture is not None


def _result(found=()):
    return explore.Result(stopped="frontier_empty", start_place="place_1", left_at="place_1", places=["place_1"], expanded=[],
                          moves=[], steps=3, seconds=10.0, findings=tuple(found), skipped=[], notes=[], resets=[], deferred=[],
                          frontier_left=0, prompts=0)


def test_reports_are_pruned_only_after_the_new_one_is_written_and_suffixed_folders_count(tmp_path, monkeypatch):
    import datetime
    app = tmp_path / APPID
    for n in range(20):
        (app / f"202611{10 + n:02d}T091500Z").mkdir(parents=True)
    real = explorereport._write

    def failing(path, data):
        if path.name == "report.json":
            raise OSError(28, "No space left on device")
        real(path, data)
    monkeypatch.setattr(explorereport, "_write", failing)
    when = datetime.datetime(2026, 12, 1, 9, 0, tzinfo=datetime.timezone.utc)
    with pytest.raises(OSError):
        explorereport.write(tmp_path, _result(), identity(), explore.Budget(), when, when, lambda s: s)
    assert all((app / f"202611{10 + n:02d}T091500Z").exists() for n in range(20))  # a failed write cost no finished report
    monkeypatch.setattr(explorereport, "_write", real)
    (app / "20261129T091500Z-2").mkdir()
    explorereport.prune(app, 20)
    kept = sorted(p.name for p in app.iterdir() if explorereport.FOLDER.match(p.name))
    assert len(kept) == 20 and "20261129T091500Z-2" in kept and "20261110T091500Z" not in kept


def test_a_repro_keeps_its_tool_names_when_a_value_is_tap(tmp_path):
    import datetime
    scrub = lambda text: text.replace("tap", "<a value>")  # noqa: E731
    made = findings.make(finding_id="f1", kind="js_error", app=APPID, app_version="1.0", build="7", tool="tapwright",
                         tool_version="1.8.0", device=findings.Device("iPhone14,2", "iOS 26.5"), expected="x", got="y",
                         scrub=lambda s: s, place="place_2", path_from="place_1",
                         path=[{"tool": "tap", "target": {"id": "go-form"}}])
    when = datetime.datetime(2026, 12, 1, 9, 0, tzinfo=datetime.timezone.utc)
    folder = explorereport.write(tmp_path, _result([made]), identity(), explore.Budget(), when, when, scrub)
    [repro] = list((folder / "repro").glob("*.json"))
    step = json.loads(repro.read_text("utf-8"))["steps"][0]
    assert step["tool"] == "tap" and step["target"] == {"id": "go-form"}


def test_a_recorded_step_keeps_the_place_it_ran_on_when_another_agent_moved_the_app_meanwhile(tmp_path):
    from test_daemon_repair import mapped
    from test_daemon_routes import explore as explore_map

    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await explore_map(d)
        d.session.at = "welcome"
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "record", {"start": True, "name": "r"})
        inner = d._run_tool_inner

        async def moved_meanwhile(client_id, name, args, scope):
            if client_id == "a" and name == "tap" and not getattr(moved_meanwhile, "done", False):
                moved_meanwhile.done = True
                await d.run_tool("b", "tap", {"target": {"id": "continue-btn"}})  # another agent, while "a" waited
            return await inner(client_id, name, args, scope)
        d._run_tool_inner = moved_meanwhile
        await d.run_tool("a", "tap", {"target": {"id": "sign-in-btn"}})  # it ran on login, where "b" left the app
        d._run_tool_inner = inner
        path = tmp_path / "r.json"
        await d.run_tool("a", "record", {"stop": True, "path": str(path)})
        steps = json.loads(path.read_text("utf-8"))["steps"]
        assert steps and all(s.get("place") != "welcome" for s in steps), steps
        await finish(stop, task)
    run(go)

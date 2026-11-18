"""The first Opus review of 1.8.0 (R180), fixed by W164: every move of a repair (its way back and its check replay too) passes
the ONE dispatch gate; the floor reads an icon button's id and symbol name; a switch's flip back is protected, and a switch with
no name is never flipped; `account` isn't offered; `replay --repair apply` gets the time both replays take and keeps nothing
for a client that left; explore's signatures keep no content-carrying id; and the lows."""
import argparse
import asyncio
import inspect
import json

import pytest
from explorefakes import FakePhone, build, grid
from fakes import button, el
from starlette.testclient import TestClient
from test_daemon import finish, make, run, started
from test_daemon_repair import Flow, names, phone, repair_lines, replay, script, tap, title
from test_explore_180_merge import Floored, refused

from tapwright import (cli, client, explore, explored, explorefloor, exploretool, mcp_server, policy as policylib,
                       repairanalysis, scriptrepair, ui)
from tapwright.control import STILL_WANTED, Refused
from tapwright.explore import Moved
from tapwright.server import create_app

APPID = "com.example.app"
TOKEN = "t" * 43
GLYPH = ""  # an icon font's private-use glyph: what XCUITest gives as a React Native icon button's label


def explore_run(explorer, maps=None) -> explore.Result:
    async def go() -> explore.Result:
        result = await explorer.run()
        if maps is not None:
            await maps.flush()
        return result
    return asyncio.run(go())


# ---- H1: a repair's way back passes the gate and the floor-word filter ----

def test_a_rejected_trial_whose_only_way_back_is_a_road_through_sign_out_taps_nothing_more(tmp_path, monkeypatch):
    welcome = [title("Welcome"), button("e1", "Guest", "guest-btn", rect=(10, 600, 100, 40))]
    home = [title("Home"), button("h1", "Sign out", "signout-btn", rect=(10, 600, 100, 40))]
    guest = scriptrepair.Candidate("target", "tap id=guest-btn", 'button "Guest"', "a test's own pick", 0.9,
                                   target={"id": "guest-btn"})
    monkeypatch.setattr(repairanalysis, "analyse", lambda *a, **k: scriptrepair.Analysis("ok", [guest]))

    async def go():
        d = phone(tmp_path, Flow(welcome, {"guest-btn": home, "signout-btn": welcome}))

        async def where():  # the place is the screen's title (the map is the road below)
            return next(e.label for e in d.session.tree if e.role == "text"), None
        d._repair_where = where
        # the proven road Home > Welcome a person's driving saved: a tap of "Sign out"
        d._proven_taps = lambda *a: [({"tool": "tap", "target": {"label": "Sign out"}}, {"id": "signout-btn", "label": "Sign out"})]
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await replay(d, script(tmp_path, [tap({"id": "login-btn"}, expect={"text": "Profile", "timeout": 0.5})]), "apply")
        assert "couldn't be taken back to where the step started" in str(e.value)
        assert d.session.calls.count("tap") == 1  # the trial's tap of Guest; never Sign out
        assert [ln for ln in repair_lines(tmp_path) if "rejected" in ln]
        await finish(stop, task)
    run(go)


def test_back_under_the_floor_is_refused_in_another_app():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        with Floored():
            d.session.app = ("Other", "com.other.app")
            e = await refused(d, "back", {"look": False})
            assert e.code == "not_allowed" and "another app" in str(e)
        await finish(stop, task)
    run(go)


# ---- H2: an icon button's id and symbol name ----

def test_an_icon_button_with_a_glyph_label_is_held_by_its_id_and_a_symbol_name_by_itself():
    floor = explorefloor.Floor(policylib.NONE, APPID)
    rn = ui.Element("e1", "button", GLYPH, "delete-row", rect=(0, 0, 40, 40), actions=("tap",))
    native = ui.Element("e2", "button", "trash", "", rect=(0, 50, 40, 40), actions=("tap",))
    share = ui.Element("e3", "button", "square.and.arrow.up", "", rect=(0, 100, 40, 40), actions=("tap",))
    plain = ui.Element("e4", "button", "Open", "open-trip", rect=(0, 150, 40, 40), actions=("tap",))
    assert floor.element(rn).word == "Delete" and floor.element(native).word == "trash"
    assert floor.element(share) is not None and floor.element(plain) is None

    async def go():  # the dispatch gate, on the daemon
        d = make()
        d.session.tree = [button("e1", GLYPH, "delete-row", rect=(10, 100, 40, 40)), button("e2", "trash", "", rect=(10, 200, 40, 40)),
                          button("e3", "Open", "open-trip", rect=(10, 300, 100, 40))]
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        with Floored():
            for target in ({"id": "delete-row"}, {"label": "trash"}):
                e = await refused(d, "tap", {"target": target, "look": False})
                assert e.code == "not_allowed" and "needs_confirmation" in str(e), target
            await d.run_tool("a", "tap", {"target": {"id": "open-trip"}, "look": False})
        assert d.session.calls.count("tap") == 1
        await finish(stop, task)
    run(go)


# ---- M1: Pause between a toggle's flip and its flip back ----

def test_a_pause_between_the_flip_and_the_flip_back_names_the_switch_left_changed(tmp_path):
    from test_daemon_explore import explored as explored_daemon

    async def go():
        d = explored_daemon(tmp_path)
        tap_first = d.session.tap
        paused = []

        async def tap_then_pause(target, hold_ms=60):
            out = await tap_first(target, hold_ms)
            if not paused and any(k == "notify-switch" for _, k in d.session.tapped):
                paused.append(1)
                d.control.set_paused(True)  # the owner presses Pause right after the flip
            return out
        d.session.tap = tap_then_pause
        stop, task = await started(d)
        try:
            text = (await d.run_tool("a", "explore", {}))["text"]
        except Refused as e:
            text = str(e)
        assert paused and "id=notify-switch, was off" in text, text
        assert [k for _, k in d.session.tapped].count("notify-switch") == 1  # it doesn't fight the person
        await finish(stop, task)
    run(go)


# ---- M2: a switch with no id and no label ----

def test_a_switch_with_no_id_and_no_label_is_never_flipped_and_listed_as_skipped(tmp_path):
    screens = {"home": grid("Home", buttons=(("go-prefs", "Prefs"),)),
               "prefs": grid("Prefs", switches=(("", ""), ("notify-switch", "Notifications")), back="Home")}
    phone_ = FakePhone(screens, "home", {("home", "go-prefs"): "prefs"}, {"prefs": "home"})
    explorer, maps = build(tmp_path, phone_)
    result = explore_run(explorer, maps)
    assert result.stopped != "toggle_not_restored", result.detail
    assert [k for _, k, _ in phone_.toggles if k == ""] == []  # never flipped
    assert [k for _, k, _ in phone_.toggles].count("notify-switch") == 2  # a named one is flipped and set back
    nameless = [s for s in result.skipped if s.why == explore.NO_NAME]
    assert nameless and nameless[0].sig.startswith("role:switch@") and "text:" not in nameless[0].sig


# ---- M3: no account in 1.8.0 ----

def test_account_is_refused_in_plain_words_and_offered_nowhere():
    with pytest.raises(Refused) as e:
        exploretool.parse({"account": "tester2"})
    assert e.value.code == "unsupported" and "account isn't offered" in str(e.value)
    assert "account" not in inspect.signature(mcp_server.explore).parameters
    assert 'add_argument("--account",' not in inspect.getsource(cli)  # `tapwright explore` has no --account


# ---- M4: the client waits for both replays; nothing is kept for a client that left ----

def test_the_client_waits_for_apply_and_its_check(tmp_path, monkeypatch):
    assert mcp_server._timeout("replay", {"repair": "apply"}) == scriptrepair.APPLY_TIMEOUT >= 2 * mcp_server.LONG_TIMEOUT
    assert mcp_server._timeout("replay", {"repair": "suggest"}) == mcp_server.LONG_TIMEOUT
    waited = []

    def call(tool, args, client_id, timeout=120.0):
        waited.append(timeout)
        return {"text": "Ran 1 of 1 steps in 1.0 s."}
    monkeypatch.setattr(client, "call", call)
    monkeypatch.setattr(client, "set_scope", lambda apps, dedicated=False: None)
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"format": "agent-tools-script", "version": 1, "name": "s", "app": APPID, "fresh": True,
                                "steps": [{"tool": "tap", "target": {"id": "x"}}]}), "utf-8")
    for repair in ("apply", None):
        cli.replay(argparse.Namespace(files=[str(path)], junit=None, apps=APPID, dedicated=False, repair=repair, adopt=False))
    assert waited == [scriptrepair.APPLY_TIMEOUT, 200.0]


def test_a_repaired_file_is_never_kept_after_the_client_left(tmp_path):
    from test_daemon_repair import HOME, RENAMED, WELCOME

    async def go():
        d = phone(tmp_path, Flow(WELCOME, {"login-button": HOME}))
        stop, task = await started(d)
        checked_first = d._check_repaired
        state = {"left": False}

        async def check_then_leave(*a):
            out = await checked_first(*a)
            state["left"] = True  # the client's own timeout ran out during the check
            return out
        d._check_repaired = check_then_leave

        async def wanted():
            return not state["left"]
        token = STILL_WANTED.set(wanted)
        try:
            with pytest.raises(Refused) as e:
                await replay(d, script(tmp_path, RENAMED), "apply")
        finally:
            STILL_WANTED.reset(token)
        assert "nothing was kept" in str(e.value)
        assert "sign_in.repaired.json" not in names(tmp_path) and "sign_in.repair.md" not in names(tmp_path)
        assert not list(tmp_path.glob("*.checking"))
        await finish(stop, task)
    run(go)


# ---- M5: no content-carrying id in a signature ----

def test_an_id_that_carries_content_is_kept_in_no_signature(tmp_path):
    screens = {"home": grid("Home", buttons=(("event-shown:Dr appointment 10:30", "Dr appointment 10:30"), ("go-about", "About")))}
    phone_ = FakePhone(screens, "home")
    explorer, maps = build(tmp_path, phone_)
    result = explore_run(explorer, maps)
    saved = next(tmp_path.rglob("map.json")).read_text("utf-8")
    sigs = [s for _, s in result.moves] + [s.sig for s in result.skipped]
    assert "appointment" not in saved and not any("appointment" in s for s in sigs), sigs
    assert any(s.startswith("role:button@") for s in sigs)  # it is still tried, by its place on the screen


# ---- M6: the check replay passes the gate ----

def test_the_check_replay_of_the_repaired_file_passes_the_gate(tmp_path):
    welcome = [title("Welcome"), button("e1", "Log in", "login-button", rect=(10, 600, 100, 40))]
    home = [title("Home"), button("h1", "Delete trip", "trip-x", rect=(10, 600, 100, 40))]

    async def go():
        d = phone(tmp_path, Flow(welcome, {"login-button": home, "trip-x": home}))
        stop, task = await started(d)
        steps = [tap({"id": "login-btn"}, expect={"text": "Home"}), tap({"id": "trip-x"})]
        with pytest.raises(Refused) as e:
            await replay(d, script(tmp_path, steps), "apply")
        assert "didn't pass when it was replayed" in str(e.value) and "needs_confirmation" in str(e.value)
        assert "sign_in.repaired.json" not in names(tmp_path)
        assert d.session.calls.count("tap") == 3  # the trial, the script's own Delete step in the run, the check's Log in
        await finish(stop, task)
    run(go)


# ---- lows ----

def test_l1_an_alert_over_another_app_is_never_answered():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.app_names[APPID] = "Example"
        d.session.app = ("Safari", "com.apple.mobilesafari")
        d.session.last_alert = ui.Alert("Open in “Other”?", ("OK",), (50, 300, 300, 200))
        with Floored():
            e = await refused(d, "alert", {"action": "accept", "name": "OK", "look": False})
            assert "another app" in str(e)
            # the app's own permission prompt, drawn by SpringBoard, is still answered with its denial
            d.session.app = ("SpringBoard", "com.apple.springboard")
            d.session.last_alert = ui.Alert("“Example” Would Like to Send You Notifications", ("Don't Allow", "Allow"), (50, 300, 300, 200))
            await d.run_tool("a", "alert", {"action": "dismiss", "name": "Don't Allow", "look": False})
        side = exploretool.DaemonPhone(d, "a", None, APPID)
        other = ui.Tree([], app=("Safari", "com.apple.mobilesafari"), alert=ui.Alert("Open?", ("OK",), (0, 0, 1, 1)))
        prompt = ui.Tree([], app=("SpringBoard", "com.apple.springboard"),
                         alert=ui.Alert("“Example” Would Like to Use Your Location", ("Don't Allow", "Allow"), (0, 0, 1, 1)))
        assert side._own(other).alert is None and side._own(prompt).alert is not None
        assert d.session.calls.count("alert") == 1
        await finish(stop, task)
    run(go)


def test_l3_a_place_link_that_takes_params_is_never_opened(tmp_path):
    screens = {"home": grid("Home", buttons=(("go-trip", "Trip"),)), "trip": grid("Trip", buttons=(("t-1", "Notes"),))}
    phone_ = FakePhone(screens, "home", {("home", "go-trip"): "trip"})
    explorer, maps = build(tmp_path, phone_)

    async def go():
        explorer.t0, explorer.waited0 = phone_.clock(), phone_.waited()
        await explorer._begin()
        await phone_.tap(ui.Target(id="go-trip"))
        look = await explorer._look(True)
        explorer.data["places"][look.place]["link"] = "wayfare://trips/[id]"
        phone_.at = "home"
        await explorer._look(False)
        await explorer._return_to(look.place)
    asyncio.run(go())
    assert phone_.urls == []


def test_l4_l5_force_and_a_dry_run_meet_the_gate():
    async def go():
        d = make()
        d.session.tree = [el("e4", "input", "Password", "pw", rect=(10, 400, 200, 40))]
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        d.session.last_alert = ui.Alert("Allow “Example” to use your location?", ("Allow", "Don't Allow"), (50, 300, 300, 200))
        with Floored():
            e = await refused(d, "type_text", {"target": {"id": "pw"}, "text": "x", "force": True, "look": False})
            assert "needs_confirmation" in str(e)
            e = await refused(d, "alert", {"action": "accept", "name": "Allow", "dry_run": True, "look": False})
            assert "needs_confirmation" in str(e)
        assert d.session.typed == []
        await finish(stop, task)
    run(go)


def test_l7_only_a_refusal_with_a_policy_word_is_kept_as_a_skip(tmp_path):
    screens = {"home": grid("Home", buttons=(("b-one", "One"), ("b-two", "Two")))}
    phone_ = FakePhone(screens, "home")
    phone_.refuse[("home", "b-one")] = Moved("not_allowed", "needs_confirmation")  # another app in front, for a moment
    phone_.refuse[("home", "b-two")] = Moved("not_allowed", "needs_confirmation", word="Archive")
    explorer, maps = build(tmp_path, phone_)
    explore_run(explorer, maps)
    data = json.loads(next(tmp_path.rglob("map.json")).read_text("utf-8"))
    rec = {a["sig"]: a for p in data["places"].values() for a in (p.get("explored") or {}).get("actions", [])}
    assert rec["id:b-one"]["result"] == "refused" and rec["id:b-two"]["result"] == "skipped"
    assert not explored.is_tried(data, next(iter(data["places"])), "id:b-one", "2.0 (1)")  # tried again on another build


def test_l8_a_repair_md_that_cant_be_written_keeps_nothing(tmp_path):
    script_path = tmp_path / "sign_in.json"
    script_path.write_text("{}", "utf-8")
    checked = scriptrepair.write_check(script_path, {"steps": []})
    (tmp_path / "sign_in.repair.md").mkdir()  # the .md can't be written there
    with pytest.raises(OSError):
        scriptrepair.keep_checked(script_path, checked, "# repairs")
    assert not (tmp_path / "sign_in.repaired.json").exists()


def test_l11_a_plain_replay_ignores_a_bad_policy_header_and_submit_is_limited_to_serves_list():
    d = make()
    seen = []

    async def run_tool(client_id, name, args, scope=None, policy=None, **via):
        seen.append((name, policy))
        return {"text": "ok", "image": None, "image_size": None}
    d.run_tool = run_tool
    head = {"Authorization": f"Bearer {TOKEN}", "X-Tapwright-Client": "agent-1", "X-Tapwright-Policy": "{not json"}
    with TestClient(create_app(d, TOKEN, hosts={"testserver"})) as c:
        assert c.post("/api/replay", headers=head, json={"path": "x.json"}).status_code == 200
        assert c.post("/api/replay", headers=head, json={"path": "x.json", "repair": "apply"}).status_code == 400
    serve = policylib.parse({"explore": {"submit": ["send-form", "save-btn"]}})
    call = policylib.parse({"explore": {"submit": ["save-btn", "pay-btn"]}})
    assert policylib.combined(serve, call).explore.submit == ("save-btn",)  # (the lead's ruling: an intersection)
    assert policylib.combined(serve, policylib.NONE).explore.submit == ("send-form", "save-btn")


def test_l6_no_reach_scroll_goes_out_toward_what_the_floor_holds():
    from test_reach import world

    async def go():
        d = make()
        stop, task, _ = await world(d)
        with Floored(rules=policylib.parse({"explore": {"skip": ["Row 30"]}})):
            e = await refused(d, "tap", {"target": {"id": "row-30"}, "look": False})
        assert e.code == "not_allowed" and "needs_confirmation" in str(e)
        assert d.session.scrolls == [] and "tap" not in d.session.calls
        await finish(stop, task)
    run(go)


def test_l2_an_explore_that_fails_before_its_run_leaves_no_busy_mark(tmp_path):
    from test_daemon_explore import explored as explored_daemon

    async def go():
        d = explored_daemon(tmp_path)
        stop, task = await started(d)
        build_of = d._build_of

        async def broken(bundle):
            raise RuntimeError("the build can't be read")
        d._build_of = broken
        with pytest.raises(Exception):
            await d.run_tool("a", "explore", {})
        d._build_of = build_of
        r = await d.run_tool("a", "explore", {"steps": 3})  # not `busy: another explore is running`
        assert r["text"].startswith("Explored ")
        await finish(stop, task)
    run(go)

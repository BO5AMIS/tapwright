"""Tapwright 2.0, worker E: the review of the perception code (REVIEW-200-VISION, R200v). Each test failed before its
fix: the short budget against the runner's own arithmetic and a still screen (H1), nothing proven on a cut-short tree
(H2), the tree readers that took a cut tree for the whole screen (M1), o refs after `look: false` (M2), the aimed app
of a cut-short look (M3), the fast OCR level's languages (M4) and the Low ones (L1-L6)."""
import asyncio

import pytest

from fakes import DescribingSession, button
from test_daemon import finish, make, started
from test_vision_200 import BUTTON, FEED, TIKTOK, V9Runner, Clock, _app, feed_session, no_pc_ocr, state  # noqa: F401

from tapwright import daemon as daemon_module
from tapwright import ui
from tapwright.control import Refused
from tapwright.session import ReadBudget, Session
from tapwright import session as session_module


def run(go):
    asyncio.run(go())


# ---- H1: the short budget ----

def _runner_tiers(budget_ms: int, read_ms: float, idle_ms: float = 150.0, app_ms: float = 5.0) -> list[str]:
    """The tiers the runner (TWTreeCommands.m) can answer within `budget_ms` for a screen whose whole tree reads in
    `read_ms`: `full` gets 60 % of the budget less finding the app and the idle wait (at most a quarter of the
    budget); `shallow` gets what is left less root's reserve (10 %, 0.2-1 s), and isn't tried under 0.25 s."""
    idle = min(idle_ms, budget_ms * 0.25)
    full_window = budget_ms * 0.6 - app_ms - idle
    tiers = ["full"] if read_ms <= full_window else []
    reserve = min(1000.0, max(200.0, budget_ms * 0.1))
    if budget_ms - budget_ms * 0.6 - reserve >= 250:
        tiers.append("shallow")
    return [*tiers, "root"]


def test_the_short_budget_reads_a_screen_like_the_home_screen_whole_and_leaves_room_for_shallow():
    # Phase 0: the home screen's tree read in 0.4 s. At 600 ms a read like it came back root (no shallow was possible)
    assert _runner_tiers(600, 400) == ["root"]
    assert _runner_tiers(session_module.SHORT_BUDGET_MS, 400) == ["full", "shallow", "root"]
    assert session_module.SHORT_BUDGET_MS == 1500 and ReadBudget().short_ms == 1500


def test_a_still_screen_gets_the_normal_budget_while_its_app_is_on_the_short_one():
    clock = Clock()
    b = ReadBudget(normal_ms=3000, short_ms=1500, memory=20.0, clock=clock)
    asked = []

    def still():
        asked.append(1)
        return True
    assert b.budget("a", still=still) == 3000 and asked == []  # (no memory of a cut: not asked)
    b.answered("a", True, 3000)
    assert b.budget("a", still=lambda: False) == 1500  # a playing feed: never still
    assert b.budget("a", still=still) == 3000 and asked == [1]  # a still profile of the same app: the normal budget
    assert b.budget("a") == 1500  # (no one to ask: as before)
    b.answered("a", True, 3000)  # the still screen's normal read was cut too: the memory is renewed
    assert b.budget("a", still=lambda: False) == 1500


def test_the_short_budget_is_never_more_than_a_normal_budget_set_lower():
    # (CodeRabbit, PR #26) TAPWRIGHT_TREE_BUDGET=500: a read cut short on it was then given 1500 ms
    b = ReadBudget(normal_ms=500, short_ms=1500, memory=20.0, clock=Clock())
    b.answered("a", True, 500)
    assert b.budget("a", still=lambda: False) == 500 and b.plan("a", still=lambda: False) == (500, False)


def test_a_budget_setting_that_isnt_a_finite_positive_number_of_ms_is_the_default(monkeypatch):
    # (CodeRabbit, PR #26) "inf" failed the import (int of infinity), 0.5 gave a budget of 0 ms
    for bad in ("inf", "nan", "-1", "0", "0.5", "x"):
        monkeypatch.setenv("TAPWRIGHT_TREE_BUDGET", bad)
        assert session_module._env_ms("TAPWRIGHT_TREE_BUDGET", 3000) == 3000, bad
    monkeypatch.setenv("TAPWRIGHT_TREE_BUDGET", "500")
    assert session_module._env_ms("TAPWRIGHT_TREE_BUDGET", 3000) == 500
    monkeypatch.setenv("TAPWRIGHT_AX_TIMEOUT", "inf")
    assert session_module._env_float("TAPWRIGHT_AX_TIMEOUT", 3.0) == 3.0


def test_the_session_asks_the_daemons_stillness_before_a_short_budget(state):
    runner = V9Runner()
    runner.trees = [{"token": "r1", "tree": _app(), "tier": "root", "truncated": True}]
    s = Session(runner)
    s.read_app = ("TikTok", TIKTOK)
    asyncio.run(s.refresh())
    s.screen_still = lambda: True
    asyncio.run(s.refresh())
    s.screen_still = lambda: False
    asyncio.run(s.refresh())
    assert runner.paths("/wda/tw/tree") == ["/wda/tw/tree?budget=3000&path=xctest", "/wda/tw/tree?budget=3000&path=xctest",
                                            "/wda/tw/tree?budget=1500&path=direct"]


def test_the_daemon_gives_the_session_its_stillness():
    s = DescribingSession()
    d = make(s)
    assert s.screen_still == d._still_for_budget
    assert d._still_for_budget() is False  # no frames: nothing is proven still

    def broken(noted=True):
        raise ValueError("a picture that can't be decoded")
    d._proven_still = broken
    assert d._still_for_budget() is False


def test_a_root_look_says_only_what_is_known(no_pc_ocr):
    assert "doesn't answer" not in ui.ELEMENTS_NONE and "weren't read within the time given" in ui.ELEMENTS_NONE
    assert "doesn't answer" not in daemon_module.HINT_NO_ELEMENTS

    async def go():
        s = feed_session()
        d = make(s)
        stop, task = await started(d)
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert "doesn't answer" not in r["text"]
        assert ui.ELEMENTS_NONE + "; text read from the picture:" in r["text"].splitlines()
        await finish(stop, task)
    run(go)


# ---- L2: a root the runner couldn't name an app for ----

def test_a_root_without_an_app_is_kept_under_the_app_the_read_was_for(state):
    runner = V9Runner()
    nameless = {"t": "Application", "r": [0, 0, 430, 932]}
    runner.trees = [{"token": "r1", "tree": nameless, "tier": "root", "truncated": True, "why": "app: none"}]
    s = Session(runner)
    asyncio.run(s.ensure())
    s.read_app = ("TikTok", TIKTOK)
    asyncio.run(s.refresh())  # normal budget, cut: TikTok goes on the short budget
    assert s.budgets.short(TIKTOK)
    asyncio.run(s.refresh())  # short budget, and its nameless root is not read again with the normal one
    asyncio.run(s.refresh())  # (the refresh above left read_app None: the budget's app is remembered)
    assert runner.paths("/wda/tw/tree") == ["/wda/tw/tree?budget=3000&path=xctest", "/wda/tw/tree?budget=1500&path=direct",
                                            "/wda/tw/tree?budget=1500&path=direct"]


# ---- H2: nothing proven on a cut-short tree ----

SWIPE = {"tool": "swipe", "x0": 215, "y0": 700, "x1": 215, "y1": 200}


def test_wait_for_gone_on_a_root_tree_never_says_done(no_pc_ocr):
    async def go():
        s = feed_session()
        d = make(s)
        stop, task = await started(d)
        for target in ({"id": "spinner"}, {"label": "Loading"}):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "wait_for", {**target, "gone": True, "timeout": 0.6, "screenshot": False})
            assert e.value.code == "timeout" and daemon_module.unread(e.value), target
            assert "it didn't happen" not in str(e.value)
        with pytest.raises(Refused) as e:  # appear: the same (not "it didn't happen": nothing was read)
            await d.run_tool("a", "wait_for", {"id": "like-button", "timeout": 0.6})
        assert daemon_module.unread(e.value)
        await finish(stop, task)
    run(go)


def test_an_element_a_partly_read_tree_gave_is_there(no_pc_ocr):
    async def go():
        s = feed_session(tier="shallow")
        s.tree = [button("e1", "Like", "like-button", rect=(380, 400, 40, 40))]
        d = make(s)
        stop, task = await started(d)
        r = await d.run_tool("a", "wait_for", {"id": "like-button", "timeout": 1, "screenshot": False})
        assert r["text"].startswith("Done after")
        with pytest.raises(Refused) as e:  # gone: it is there, so the wait times out as usual
            await d.run_tool("a", "wait_for", {"id": "like-button", "gone": True, "timeout": 0.5})
        assert e.value.code == "timeout" and not daemon_module.unread(e.value)
        await finish(stop, task)
    run(go)


def test_a_text_gone_needs_a_picture_read_that_worked(no_pc_ocr):
    from tapwright.device import DeviceError

    async def go():
        s = feed_session()
        s.runner_text = DeviceError("WebDriverAgent: unknown error: tapwright ocr: Vision failed")
        d = make(s)
        stop, task = await started(d)
        with pytest.raises(Refused) as e:  # the phone's OCR failed and there is none here: nothing proven
            await d.run_tool("a", "wait_for", {"text": "For You", "gone": True, "timeout": 0.6})
        assert daemon_module.unread(e.value) and "picture's text wasn't read either" in str(e.value)

        async def slow(level="accurate", implicit=False):
            await asyncio.sleep(5)
            return [], ""
        d._screen_text = slow
        with pytest.raises(Refused) as e:  # the picture read ran out of time: nothing proven either
            await d.run_tool("a", "wait_for", {"text": "For You", "gone": True, "timeout": 0.5})
        assert daemon_module.unread(e.value)
        s.runner_text = list(FEED)  # a picture read that worked decides it
        del d._screen_text
        r = await d.run_tool("a", "wait_for", {"text": "Comments", "gone": True, "timeout": 1, "screenshot": False})
        assert r["text"].startswith("Done after")
        await finish(stop, task)
    run(go)


def test_a_runs_expect_on_a_root_tree_fails_the_step_as_not_checked_never_as_a_failed_check(no_pc_ocr):
    async def go():
        s = feed_session()
        d = make(s)
        stop, task = await started(d)
        for expect in ({"id": "loading", "gone": True}, {"id": "like-button"}, {"label": "Like", "value": "on"},
                       {"id": "caption", "text": "dogs"}):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "run", {"steps": [{**SWIPE, "expect": {**expect, "timeout": 0.5}}],
                                              "screenshot": False})
            said = str(e.value)
            assert e.value.code == "timeout" and "its expect couldn't be checked" in said, expect
            assert said.startswith("Ran 0 of 1 steps; stopped at step 1") and "didn't appear" not in said
            assert d._bug_failures.get("a") is None  # (not kept for bug_report from: "failure")
        await finish(stop, task)
    run(go)


def test_an_optional_wait_and_an_optional_tap_on_a_root_tree_are_skipped_as_not_checked(no_pc_ocr):
    async def go():
        s = feed_session()
        d = make(s)
        stop, task = await started(d)
        r = await d.run_tool("a", "run", {"steps": [
            {"tool": "wait_for", "id": "spinner", "gone": True, "timeout": 0.5, "optional": True},
            {"tool": "tap", "target": {"id": "skip"}, "optional": True}], "trace": True, "screenshot": False})
        text = r["text"]
        assert "optional: id=spinner couldn't be checked within 0.5 s" in text
        assert f"optional: id=skip couldn't be found ({daemon_module.UNREAD})" in text
        assert "tap" not in s.calls  # (nothing was run on a guess)
        await finish(stop, task)
    run(go)


def test_a_steps_if_no_read_can_decide_stops_the_run_timeout(no_pc_ocr):
    # (CodeRabbit, PR #26) AGENT-TOOLS: a step's `if` no read can decide answers `timeout`, never `Done`: the steps
    # after it were written for a screen whose state is known, so the run stops there instead of going on
    async def go():
        s = feed_session()
        d = make(s)
        stop, task = await started(d)
        for first in ({**SWIPE, "if": {"id": "next"}},
                      {"tool": "wait_for", "id": "x", "timeout": 0.5, "optional": True, "if": {"label": "Next"}}):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "run", {"steps": [first, {"tool": "tap", "target": {"x": 100, "y": 100}}],
                                              "screenshot": False})
            said = str(e.value)
            assert e.value.code == "timeout" and daemon_module.unread(e.value), first
            assert said.startswith("Ran 0 of 2 steps; stopped at step 1") and "couldn't be checked" in said, said
        assert "swipe" not in s.calls and "tap" not in s.calls  # (nothing was run on a guess)
        await finish(stop, task)
    run(go)


def test_a_links_expect_on_a_root_tree_is_not_checked(no_pc_ocr):
    async def go():
        s = feed_session()
        d = make(s)
        stop, task = await started(d)
        view = d.control.client("a")
        with pytest.raises(Refused) as e:
            await d._expect_after_link(view, None, {"id": "profile", "timeout": 0.5}, [])
        assert daemon_module.unread(e.value)
        s.runner_text = list(FEED)  # a text is looked for in the picture there, as wait_for does
        assert await d._expect_after_link(view, None, {"text": "For You", "timeout": 1}, []) is None
        await finish(stop, task)
    run(go)


# ---- M2, M3 ----

def test_o_refs_end_with_any_action_look_false_included(no_pc_ocr):
    async def go():
        s = feed_session()
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("b", "look", {"screenshot": False})
        await d.run_tool("a", "swipe", {"x0": 215, "y0": 700, "x1": 215, "y1": 200, "look": False})
        with pytest.raises(Refused) as e:  # the video changed: o2 was a point of the screen before the swipe
            await d.run_tool("a", "tap", {"target": {"ref": "o2"}, "look": False})
        assert e.value.code == "not_found"
        assert d.control.client("b").ocr  # (another client's refs are its own: kept)
        await finish(stop, task)
    run(go)


def test_o_refs_end_with_an_action_that_may_have_sent_input_and_failed(no_pc_ocr):
    # (CodeRabbit, PR #26) a swipe the phone didn't answer, or one refused after its touch went out, may have moved
    # the screen: o2 must not become the old point on the new screen
    from tapwright.device import Unanswered

    async def go():
        s = feed_session()
        s.calls_touch_hook = True
        d = make(s)
        stop, task = await started(d)
        for failure in (Unanswered("the phone didn't answer within 10 s"),
                        Refused("timeout", "the touch went out; its end wasn't seen")):
            await d.run_tool("a", "look", {"screenshot": False})
            assert d.control.client("a").ocr
            s.fail["swipe"] = failure
            with pytest.raises(Refused):
                await d.run_tool("a", "swipe", {"x0": 215, "y0": 700, "x1": 215, "y1": 200, "look": False})
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "tap", {"target": {"ref": "o2"}, "look": False})
            assert e.value.code == "not_found" and "isn't in your last OCR reading" in str(e.value), failure
        await finish(stop, task)
    run(go)


def test_an_o_ref_after_another_clients_action_is_refused_stale(no_pc_ocr):
    # (CodeRabbit, PR #26) b's swipe moved the feed after a's look: a's o2 is a point of the screen before it, and on a
    # cut-short screen no picture check stands in the way (_moved_under), so the refs carry the generation they were
    # read at, as a {text} aimed by the picture does (review K-H2)
    async def go():
        s = feed_session()
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("b", "swipe", {"x0": 215, "y0": 700, "x1": 215, "y1": 200, "look": False})
        taps = s.calls.count("tap")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"ref": "o2"}, "look": False})
        assert e.value.code == "stale" and "Nothing was touched" in str(e.value)
        assert s.calls.count("tap") == taps
        await d.run_tool("a", "look", {"screenshot": False})  # a new look: its refs are of the screen now
        r = await d.run_tool("a", "tap", {"target": {"ref": "o2"}, "look": False})
        assert r["text"].startswith("Tapped")
        await finish(stop, task)
    run(go)


def test_o_refs_stay_after_a_refusal_before_any_input(no_pc_ocr):
    async def go():
        s = feed_session()
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        with pytest.raises(Refused) as e:  # refused before anything was sent: the screen is the one the refs are of
            await d.run_tool("a", "tap", {"target": {"ref": "o9"}})
        assert e.value.code == "not_found"
        assert d.control.client("a").ocr
        await finish(stop, task)
    run(go)


def test_a_cut_short_look_aims_typing_at_the_app_it_shows(no_pc_ocr):
    async def go():
        s = feed_session()
        d = make(s)
        stop, task = await started(d)
        d.control.client("a").aimed_app = ("Lab", "com.example.lab")  # an earlier touch; TikTok came by a link
        await d.run_tool("a", "look", {"screenshot": False})
        assert d.control.client("a").aimed_app == ("TikTok", TIKTOK)
        r = await d.run_tool("a", "type_text", {"text": "dogs", "screenshot": False})
        assert r["text"].startswith("Typed 4 characters into whatever has the keyboard's focus")
        await finish(stop, task)
    run(go)


def test_a_recording_keeps_no_text_typed_into_a_field_that_wasnt_read(no_pc_ocr, tmp_path):
    # (CodeRabbit, PR #26) on a cut-short screen the keys go to whatever has the focus: nothing tells whether it is a
    # password's field, so the script keeps no text, as for a secure field
    import json

    async def go():
        s = feed_session()
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "record", {"start": True, "name": "sign in"})
        await d.run_tool("a", "tap", {"target": {"x": 200, "y": 300}, "look": False})
        await d.run_tool("a", "type_text", {"text": "hunter2", "look": False})
        path = tmp_path / "s.json"
        r = await d.run_tool("a", "record", {"stop": True, "path": str(path)})
        saved = path.read_text("utf-8")
        assert "hunter2" not in saved and "Step 2 typed into a secure field" in r["text"]
        assert json.loads(saved)["steps"][1] == {"tool": "type_text", "text": "", "secure": True}
        await finish(stop, task)
    run(go)


# ---- M1: nothing presented or saved as a whole screen from a cut-short tree ----

def test_audit_and_ux_check_refuse_a_screen_read_in_part(no_pc_ocr, monkeypatch):
    from tapwright import uxcheck
    filed = []
    monkeypatch.setattr(uxcheck, "tell_journey", lambda *a, **k: filed.append(a))

    async def go():
        for tier in ("root", "shallow"):
            s = feed_session(tier=tier)
            s.tree = [button("e1", "Like", "like-button", rect=(380, 400, 40, 40))]
            d = make(s)
            stop, task = await started(d)
            for tool in ("audit", "ux_check"):
                with pytest.raises(Refused) as e:
                    await d.run_tool("a", tool, {})
                assert e.value.code == "unsupported" and daemon_module.UNREAD.capitalize() in str(e.value), (tier, tool)
                assert "no issues" not in str(e.value) and "0 findings" not in str(e.value)
            await finish(stop, task)
        assert filed == []  # (nothing went on a journey)
    run(go)


def test_ux_check_refuses_by_its_own_read_when_another_read_came_back_whole_since(no_pc_ocr, monkeypatch):
    # (CodeRabbit, PR #26) between ux_check's turn and its refusal another client's read can make the shared tier
    # "full": the refusal goes by what this check read, never by the newest read of someone else
    from tapwright import uxcheck
    checked = []
    monkeypatch.setattr(uxcheck, "check", lambda *a, **k: checked.append(a))

    async def go():
        s = feed_session(tier="shallow")
        s.tree = [button("e1", "Like", "like-button", rect=(380, 400, 40, 40))]
        d = make(s)
        stop, task = await started(d)
        read = d._read

        async def then_whole(fn):
            out = await read(fn)
            s.last_tier = "full"  # another client's read, in a turn of its own, came back whole
            return out
        d._read = then_whole
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "ux_check", {})
        assert e.value.code == "unsupported" and checked == []
        await finish(stop, task)
    run(go)


def test_a_cut_short_look_is_no_screen_for_naming_drift(no_pc_ocr, monkeypatch):
    from tapwright import uxcheck
    noted = []
    monkeypatch.setattr(uxcheck, "note_screen", lambda client, items: noted.append(items))

    async def go():
        s = feed_session(tier="shallow")
        s.tree = [button("e1", "Like", "like-button")]
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        assert noted == []
        s.tier = "full"
        await d.run_tool("a", "look", {"screenshot": False})
        assert len(noted) == 1
        await finish(stop, task)
    run(go)


def test_a_cut_short_tree_is_no_place():
    import dataclasses
    from mapfakes import screen
    from tapwright import places
    whole = screen("Settings", ids=("go-language",), back="Home")
    data = {"places": {}, "titleKey": "k", "roads": []}
    assert places.recognisable(whole) and places.read_place(whole).whole
    for tier in ("shallow", "root"):
        cut = dataclasses.replace(whole, tier=tier)
        assert not places.recognisable(cut)
        read = places.read_place(cut)
        assert not read.whole and places.locate(data, read) == (None, 0.0)


def test_explore_files_no_way_back_from_a_screen_it_couldnt_read(tmp_path):
    import dataclasses
    from explorefakes import FakePhone, build, standard
    from mapfakes import screen
    from test_explore import run as explore_run
    screens, goes, back_to = standard()
    # About's version row opens a feed that never answers whole; back there lands on the feed again
    screens["feed"] = dataclasses.replace(screen("Feed", ids=("feed-video",), back="About"), tier="root")
    goes[("about", "about-version")] = "feed"
    back_to["feed"] = "feed"
    phone = FakePhone(screens, "home", goes, back_to)
    explorer, maps = build(tmp_path, phone)
    result = explore_run(explorer, maps)
    assert [f for f in result.findings if f.kind == "dead_end"] == []
    assert any("couldn't be read" in n for n in explorer.notes)


def test_a_move_to_a_screen_that_couldnt_be_read_breaks_the_way(no_pc_ocr, tmp_path):
    from tapwright import autoroute, mapkeeper

    async def go():
        s = feed_session()
        d = make(s)
        d.maps = mapkeeper.Maps(str(tmp_path / "maps"))
        d._may_map = lambda bundle: True
        view = d.control.client("a")
        chain = view.auto = autoroute.Chain(TIKTOK)
        chain.begin("Home")
        view.app, view.tree = ("TikTok", TIKTOK), ui.Tree([], tier="root")
        await d._learn(view, "tap", {"target": {"x": 10, "y": 300}}, {"text": "Tapped."}, {"mapping": True}, 0.0)
        assert view.place is None and chain.broken_at == 0
    run(go)


def test_back_never_takes_a_cut_short_tree_for_an_unchanged_screen(no_pc_ocr):
    async def go():
        s = feed_session()
        d = make(s)
        stop, task = await started(d)
        view = d.control.client("a")
        view.back_before = (None, daemon_module._screen_sig([]))  # (a root before; a root after compares equal)
        assert await d._back_unchanged(view) is False
        await finish(stop, task)
    run(go)


def test_compare_screen_wont_ignore_elements_it_couldnt_read(no_pc_ocr):
    async def go():
        s = feed_session()
        d = make(s)
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "compare_screen", {"name": "feed", "ignore": [{"id": "clock"}]})
        assert e.value.code == "unsupported" and daemon_module.UNREAD in str(e.value)
        await finish(stop, task)
    run(go)


def test_a_replay_that_couldnt_read_its_screen_is_no_break_to_repair_and_no_failed_route():
    import types
    from tapwright import explorefloor, policy, repairanalysis
    steps = [{"tool": "tap", "target": {"id": "like-button"}, "expect": {"id": "liked"}}]
    screen = types.SimpleNamespace(floor=explorefloor.Floor(policy.DEFAULT, TIKTOK), here=None)
    text = ('Ran 0 of 1 steps; stopped at step 1 (tap id=like-button): no element with id "like-button"\n'
            + ui.HINT_NO_ELEMENTS)
    brk = repairanalysis.parse_break(text, "not_found", steps)
    assert brk is not None and brk.unread
    analysed = repairanalysis.analyse(steps, 0, brk, screen)
    assert analysed.kind == "none" and "weren't read" in analysed.reason
    plain = repairanalysis.parse_break("Ran 0 of 1 steps; stopped at step 1 (tap id=like-button): no element",
                                       "not_found", steps)
    assert plain is not None and not plain.unread
    assert daemon_module.cut_refusal(Refused("not_found", "x\n" + ui.HINT_PART_ELEMENTS))
    assert not daemon_module.cut_refusal(Refused("not_found", "no element with id x"))


def test_a_journey_stress_on_a_cut_short_screen_doesnt_say_what_survived(no_pc_ocr):
    import types

    async def go():
        s = feed_session()
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        jr = types.SimpleNamespace(users_eyes=False, screens=[types.SimpleNamespace(index=1, title="Feed",
                                                                                  signature="x")])
        ok, said = d._survives("a", jr, {"text": "dogs"}, "")
        assert ok is None and "couldn't be looked for" in said
        ok, said = d._survives("a", jr, {"goal_step": 1}, "")
        assert ok is None
        await finish(stop, task)
    run(go)


# ---- L4: Notification Center ----

def test_notification_center_is_read_with_the_normal_budget_and_kept_out_of_the_memory(state):
    runner = V9Runner()
    center = {"t": "Application", "r": [0, 0, 430, 932], "b": "com.apple.springboard", "l": "SpringBoard"}
    s = Session(runner)
    asyncio.run(s.ensure())
    s.read_app = ("TikTok", TIKTOK)
    s.budgets.answered(TIKTOK, True, 3000)  # TikTok's feed is on the short budget
    s.last_tier = "root"
    runner.trees = [{"token": "c1", "tree": center, "tier": "shallow", "truncated": True}]
    asyncio.run(s.read_center())
    assert runner.paths("/wda/tw/tree") == ["/wda/tw/tree?budget=3000&path=xctest"]  # (not TikTok's short budget)
    assert not s.budgets.short("com.apple.springboard") and s.budgets.short(TIKTOK)
    assert s.center_tier == "shallow" and s.last_tier == "root" and s.last_compact is None


def test_the_notifications_answer_says_when_the_center_was_read_in_part(tmp_path):
    from test_notifications import BUNDLE, center_daemon
    from tapwright.daemon import Scope
    from tapwright import center_tool

    async def go():
        d, s = center_daemon(tmp_path, [])
        s.center_tier = "root"
        stop, task = await started(d)
        r = await d.run_tool("a", "notifications", {}, scope=Scope((BUNDLE,)))
        assert f"Note: {center_tool.CENTER_CUT}" in r["text"]
        assert "Notifications: none from" not in r["text"]
        s.center_tier = "full"
        r = await d.run_tool("a", "notifications", {}, scope=Scope((BUNDLE,)))
        assert center_tool.CENTER_CUT not in r["text"]
        await finish(stop, task)
    run(go)


def test_opening_a_notification_past_a_center_read_in_part_isnt_told_absent(tmp_path):
    # (CodeRabbit, PR #26) n20 may be among what the cut-short read left out: "isn't there" would be a guess
    from test_notifications import BUNDLE, center_daemon
    from tapwright.daemon import Scope
    from tapwright import center_tool

    async def go():
        d, s = center_daemon(tmp_path, [])
        s.center_tier = "shallow"
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "notifications", {"open": 20}, scope=Scope((BUNDLE,)))
        said = str(e.value)
        assert e.value.code == "not_found" and "isn't there" not in said and "couldn't be checked" in said
        assert center_tool.CENTER_CUT in said and "nothing was opened" in said
        s.center_tier = "full"
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "notifications", {"open": 20}, scope=Scope((BUNDLE,)))
        assert "n20 isn't there" in str(e.value) and center_tool.CENTER_CUT not in str(e.value)
        await finish(stop, task)
    run(go)


# ---- lead's addition 1: the runner's way of reading ----

def test_the_read_path_follows_the_budget_unless_forced(monkeypatch):
    assert session_module.tree_path(3000, 3000, forced="") == "xctest"
    assert session_module.tree_path(1500, 3000, forced="") == "direct"
    assert session_module.tree_path(1500, 3000, forced="xctest") == "xctest"
    assert session_module.tree_path(3000, 3000, forced="direct") == "direct"
    assert session_module.tree_path(3000, 3000, forced="nonsense") == "xctest"
    monkeypatch.setattr(session_module, "TREE_PATH_FORCED", "direct")
    assert session_module.tree_path(3000, 3000) == "direct"


# ---- lead's addition 2: the moved-under guard over a playing feed ----

def test_a_touch_by_point_on_a_cut_short_screen_isnt_refused_for_a_picture_that_moves():
    import types
    from PIL import Image
    s = DescribingSession()
    d = make(s)
    view = d.control.client("a")
    before = types.SimpleNamespace(thumb=Image.new("L", (43, 93), 0), at=d.hub.now_ms())
    after = types.SimpleNamespace(thumb=Image.new("L", (43, 93), 255), at=d.hub.now_ms())
    view.read_mark, view.moved = (1, before), False
    d.hub.latest = after
    view.tree = ui.Tree([], tier="full")
    assert d._moved_under(view, 200, 400)  # a whole screen: the guard holds, as before
    for tier in ("root", "shallow"):
        view.tree = ui.Tree([], tier=tier)
        assert not d._moved_under(view, 200, 400), tier  # a video plays under every point there


# ---- lead's addition 3: the settle on a screen that never stands still ----

def test_the_settle_cap_memory():
    from tapwright.session import SettleCap
    caps = SettleCap(short=0.5)
    assert caps.cap("a", 2.0) == 2.0 and caps.cap(None, 2.0) == 2.0
    assert not caps.settled("a", capped=True, cut=False)  # a whole tree: a long animation, not a feed
    assert caps.cap("a", 2.0) == 2.0
    assert caps.settled("a", capped=True, cut=True)  # capped, still moving, its tree cut short: switched
    assert caps.cap("a", 2.0) == 0.5 and caps.cap("a", 4.0) == 0.5 and caps.cap("a", 0.3) == 0.3
    # (lead, phone 2026-11-26) while its newest tree is whole the settle is given a second (the elements are read after
    # it); after its launch two (0.5 s after one the app wasn't up yet)
    assert caps.cap("a", 2.0, cut=False) == 1.0 and caps.cap("a", 4.0, launch=True) == 2.0 and caps.cap("a", 0.3, cut=False) == 0.3
    assert caps.cap("b", 2.0, cut=False) == 2.0 and caps.cap("b", 4.0, launch=True) == 4.0
    assert caps.cap("b", 2.0) == 2.0
    assert not caps.settled("a", capped=True, cut=True)  # (already: no second log line)
    # (worker H) it stood still while its screen still doesn't read (a feed between two videos): the short cap stays
    assert not caps.settled("a", capped=False, cut=True, seconds=0.3) and caps.cap("a", 2.0) == 0.5
    assert caps.settled("a", capped=False, cut=False)  # it stood still on a screen that reads: the normal cap again
    assert caps.cap("a", 2.0) == 2.0
    caps.settled("a", capped=True, cut=True)
    # (lead, phone 2026-11-26) a whole read alone switches nothing: TikTok's feed reads whole for a moment after a launch
    # and then not again, and each flip back cost a 2 s settle
    assert not caps.read_whole("a") and caps.cap("a", 2.0) == 0.5
    caps.forget()
    assert caps.cap("a", 2.0) == 2.0


def test_the_daemons_settle_shortens_on_a_feed_and_comes_back_when_it_stands_still(no_pc_ocr):
    from tapwright.daemon import Settled

    async def go():
        s = feed_session()
        s.settles = session_module.SettleCap()
        s.read_app = ("TikTok", TIKTOK)
        s.last_tier = "root"
        logged = []
        d = make(s, log=logged.append)
        caps_asked, ends = [], [Settled(2.0, True, True), Settled(0.5, True, True), Settled(0.3, True, False, True),
                                Settled(0.3, True, False, True), Settled(2.0, True, True)]

        async def frames(after_seq, still=None, cap=2.0, since=None, early=None):
            caps_asked.append(cap)
            return ends.pop(0)
        d._settle_frames = frames
        for _ in range(3):  # (the third stood still, but the screen still doesn't read: the short cap stays, worker H)
            await d._settle(0, cap=2.0)
        s.last_tier = "full"  # a whole tree: a still end gives the normal cap back, and capped is only a long animation
        await d._settle(0, cap=2.0)  # (the tree reads: a second, not half of one)
        await d._settle(0, cap=4.0)
        assert caps_asked == [2.0, 0.5, 0.5, 1.0, 4.0]
        assert sum("never stands still" in str(x) for x in logged) == 1
        assert sum("stood still" in str(x) for x in logged) == 1
    run(go)


def test_a_whole_read_gives_the_app_the_normal_settle_cap_again(state):
    runner = V9Runner()
    runner.trees = [{"token": "f1", "tree": _app(children=[BUTTON]), "tier": "full", "truncated": False}]
    s = Session(runner)
    s.settles.settled(TIKTOK, capped=True, cut=True)
    asyncio.run(s.refresh())
    assert not s.settles.restless(TIKTOK)


# ---- M4, L1, L3, L5, L6 ----

def test_a_fast_read_in_a_language_the_fast_level_cant_read_goes_accurate(state, monkeypatch):
    runner = V9Runner()
    s = Session(runner)
    asyncio.run(s.ensure())
    monkeypatch.setattr(session_module, "OCR_LANGUAGES", "ar-SA,en-US")
    asyncio.run(s.read_text("fast"))
    assert runner.paths("/wda/tw/ocr")[-1] == "/wda/tw/ocr?level=accurate&languages=ar-SA,en-US"
    monkeypatch.setattr(session_module, "OCR_LANGUAGES", "en-US,fr-FR")
    asyncio.run(s.read_text("fast"))
    assert runner.paths("/wda/tw/ocr")[-1] == "/wda/tw/ocr?level=fast&languages=en-US,fr-FR"
    assert session_module.fast_reads("") and not session_module.fast_reads("zh-Hans")


def test_an_ocr_answer_from_a_turned_screen_is_refused_not_stretched():
    from tapwright import ocr
    with pytest.raises(ValueError):
        ocr.of_runner({"screen": [932, 430], "lines": [{"text": "A", "rect": [10, 10, 20, 10]}]}, (430.0, 932.0))
    same = ocr.of_runner({"screen": [430, 932], "lines": [{"text": "A", "rect": [10, 10, 20, 10]}]}, (430.0, 932.0))
    assert same[0].rect == (10.0, 10.0, 20.0, 10.0)


def test_a_near_miss_on_a_partly_read_screen_keeps_its_closest_line(no_pc_ocr):
    async def go():
        s = feed_session(tier="shallow")
        s.tree = [button("e1", "Profile", "profile-tab", rect=(330, 870, 70, 40))]
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "profile-tabs"}})
        said = str(e.value)
        assert e.value.code == "not_found" and "Closest:" in said and daemon_module.HINT_PART_ELEMENTS in said
        assert said.index("Closest:") < said.index(daemon_module.HINT_PART_ELEMENTS)
        await finish(stop, task)
    run(go)


def test_a_broken_ocr_install_never_fails_a_look_the_agent_didnt_ask_ocr_for(monkeypatch):
    from tapwright import ocr

    def broken(image, screen):
        raise ImportError("onnxruntime DLL load failed")
    monkeypatch.setattr(ocr, "reader", broken)
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(daemon_module, "RUNNER_OCR", "0")

    async def go():
        s = feed_session()
        d = make(s)
        stop, task = await started(d)
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert ui.ELEMENTS_NONE + "." in r["text"].splitlines()
        assert "reading it here failed (ImportError)" in r["text"].splitlines()[-1]
        with pytest.raises(Refused) as e:  # wait_for's picture read: nothing proven, never `internal`
            await d.run_tool("a", "wait_for", {"text": "For You", "timeout": 0.5})
        assert daemon_module.unread(e.value)
        await finish(stop, task)
    run(go)


def test_a_session_that_learns_version_9_late_sets_the_runners_ax_timeout(state):
    from tapwright.device import DeviceError
    runner = V9Runner()
    runner.fail["/wda/tw/version"] = DeviceError("HTTP 500: boom")  # ensure's probe can't tell
    s = Session(runner)
    asyncio.run(s.ensure())
    assert not s.bounded
    asyncio.run(s.refresh())  # the session's own route says 9
    posted = [body for m, p, body in runner.requests if p.endswith("/appium/settings")]
    assert s.bounded and posted[-1] == {"settings": {"twAXTimeout": session_module.AX_TIMEOUT}}


# ---- M1: a drive (trip) on a screen it couldn't read ----

def _bare_trip(**fields):
    import types
    from tapwright import trip

    async def no_after(*a, **k):
        raise RuntimeError("no screen in this test")
    t = object.__new__(trip.Trip)
    t.d = types.SimpleNamespace(_after=no_after)
    t.view, t.notes, t.moves, t.jumps, t.unread, t.foreign = None, [], 0, 0, False, False
    for k, v in fields.items():
        setattr(t, k, v)
    return t


def test_a_drive_that_stops_on_a_screen_it_couldnt_read_says_so_as_timeout_not_off_route():
    from tapwright import trip
    t = _bare_trip()
    assert t.place_of(ui.Tree([], tier="root")) is None and t.unread
    with pytest.raises(Refused) as e:
        asyncio.run(t.off_route(2, 3, None, trip.NOT_KNOWN, None))
    assert e.value.code == "timeout" and "couldn't be read" in str(e.value) and "map doesn't know" not in str(e.value)
    t.unread = False
    with pytest.raises(Refused) as e:
        asyncio.run(t.off_route(2, 3, None, trip.NOT_KNOWN, None))
    assert e.value.code == "off_route"  # (a whole screen the map doesn't know: as before)


def test_an_arrival_that_couldnt_be_read_is_no_failed_road():
    import types
    from tapwright import trip
    failed = []
    t = _bare_trip(writes=True, chain=[], root=None, bundle=TIKTOK, clock=lambda: 0.0)
    t.d = types.SimpleNamespace(maps=types.SimpleNamespace(fail=lambda *a: failed.append(a) or []))
    t.dropped = lambda pairs: None
    hop = types.SimpleNamespace(dst="Profile", jump=False, kind="road", road={"move": {"tool": "tap"}}, src="Feed",
                                fixed=None, move={"tool": "tap"}, names={})
    asyncio.run(t.judge(hop, trip.Seen(None, False, ui.Tree([], tier="root")), 0.0))
    assert failed == []
    asyncio.run(t.judge(hop, trip.Seen(None, False, ui.Tree([], tier="full")), 0.0))
    assert len(failed) == 1  # (a whole screen that isn't the road's end: counted, as before)


# ---- runner version 10: the lean direct read (lead, phone 2026-11-26) ----

def test_a_version_10_runner_is_always_read_the_lean_direct_way(state, monkeypatch):
    runner = V9Runner(version=10)
    runner.trees = [{"token": "r1", "tree": _app(), "tier": "root", "truncated": True}]
    s = Session(runner)
    s.read_app = ("TikTok", TIKTOK)
    asyncio.run(s.refresh())  # the normal budget
    s.screen_still = lambda: False
    asyncio.run(s.refresh())  # the short one
    assert runner.paths("/wda/tw/tree") == ["/wda/tw/tree?budget=3000&path=direct&attrs=lean",
                                            "/wda/tw/tree?budget=1500&path=direct&attrs=lean"]


def test_the_lean_read_can_be_turned_off(state, monkeypatch):
    monkeypatch.setattr(session_module, "TREE_ATTRS", "std")
    runner = V9Runner(version=10)
    s = Session(runner)
    asyncio.run(s.refresh())
    assert runner.paths("/wda/tw/tree") == ["/wda/tw/tree?budget=3000&path=xctest"]

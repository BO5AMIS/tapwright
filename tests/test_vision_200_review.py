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
    assert runner.paths("/wda/tw/tree") == ["/wda/tw/tree?budget=3000", "/wda/tw/tree?budget=3000",
                                            "/wda/tw/tree?budget=1500"]


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
    assert runner.paths("/wda/tw/tree") == ["/wda/tw/tree?budget=3000", "/wda/tw/tree?budget=1500",
                                            "/wda/tw/tree?budget=1500"]


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

        async def slow(level="accurate"):
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


def test_a_steps_if_and_an_optional_wait_on_a_root_tree_are_skipped_as_not_checked(no_pc_ocr):
    async def go():
        s = feed_session()
        d = make(s)
        stop, task = await started(d)
        r = await d.run_tool("a", "run", {"steps": [
            {**SWIPE, "if": {"id": "next"}},
            {"tool": "wait_for", "id": "spinner", "gone": True, "timeout": 0.5, "optional": True},
            {"tool": "tap", "target": {"id": "skip"}, "optional": True}], "trace": True, "screenshot": False})
        text = r["text"]
        assert f"if: id=next couldn't be checked ({daemon_module.UNREAD})" in text
        assert "optional: id=spinner couldn't be checked within 0.5 s" in text
        assert f"optional: id=skip couldn't be found ({daemon_module.UNREAD})" in text
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

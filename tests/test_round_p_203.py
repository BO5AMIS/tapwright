"""Tapwright 2.0.3, round P: review RO's findings on round O. RO-M1 (the late o-ref check judges the text against
where the first check found it, once an action: a scroll aimed by an o ref goes when its text stayed), RO-M2 (after a
late read the front and banner check runs once more, so nothing is read between it and the finger), RO-L1 (the runner
path's third question clears only when prompt), RO-L2 (a fast "nothing" there is a banner in motion), RO-L4 (the
check's age counts from before its read), and the no-quiet rule on the Unanswered branch. Each fails on e38bba5."""
import asyncio
import time

from test_banner_hit_203 import _looked
from test_daemon import finish
from test_round_i_203 import hit_of
from test_round_k_203 import APP, PLATE, answers, status_bar
from test_round_m_203 import _session
from test_touch_other_app_203 import SHOP, PointSession, quick_banner, refused, run, unread  # noqa: F401 (fixture)

from tapwright import ocr
from tapwright import session as session_module
from tapwright.device import ConnectionLost, DeviceError
from tapwright.session import SPRINGBOARD

SEARCH = ocr.Seen("Search", (170.0, 75.0, 60.0, 20.0), 1.0)  # (centre 200, 85: in the top zone)


def _words(tail: str) -> DeviceError:
    return DeviceError(f"unsupported operation: {session_module.HIT_NOTHING} ({tail})")


def _sixteen(script):
    s = _session(script)
    s.tw_version = 16
    return s


class ScrollSession(PointSession):
    """A scroll's drag goes through the real _touching and gate, from its start point (the reviewer's probe). `moves`:
    each drag moves the text this far up; `between`: seconds after the first drag (a drag and its read on the phone)."""

    def __init__(self) -> None:
        super().__init__()
        self.tier, self.tree, self.runner_text = "root", [], [SEARCH]
        self.moves, self.between = 0.0, 0.0

    async def scroll(self, x0, y0, x1, y1):
        await self._goes("scroll", 300, (x0, y0))
        self.runner_text = [ocr.Seen(t.text, (t.rect[0], t.rect[1] - self.moves, t.rect[2], t.rect[3]), t.score)
                            for t in self.runner_text]
        await asyncio.sleep(self.between)
        self.between = 0.0  # (the first drag's wait puts the next drag's gate past O_REF_FRESH; the rest quickly)
        return f"scrolled ({round(x0)}, {round(y0)}) -> ({round(x1)}, {round(y1)})"


async def _o_looked(s):
    d, stop, task = await _looked(s)
    r = await d.run_tool("a", "look", {"screenshot": False})
    assert 'o1 "Search" @170,75 60x20' in r["text"].splitlines()
    answers(s, status_bar(), APP)
    return d, stop, task


def _after_a_wait(d, monkeypatch, seconds=1.1, then=None):
    turn = d.control.agent

    async def waited(fn):
        if then is not None:
            then()
        await asyncio.sleep(seconds)  # (another caller's read held the queue)
        return await turn(fn)
    monkeypatch.setattr(d.control, "agent", waited)


# ---- RO-M1 ----

def test_ro_m1_a_scroll_at_an_o_ref_after_a_wait_goes_when_its_text_stayed(monkeypatch):
    async def go():
        s = ScrollSession()
        d, stop, task = await _o_looked(s)
        _after_a_wait(d, monkeypatch)
        levels = len(s.text_levels)
        await d.run_tool("a", "scroll", {"target": {"ref": "o1"}, "direction": "down", "look": False})
        assert [k for k, _ in s.sent] == ["scroll"] and len(s.text_levels) == levels + 2  # (the check, the late read)
        await finish(stop, task)
    run(go)


def test_ro_m1_the_late_check_judges_where_the_first_check_found_the_text_and_still_refuses_a_moved_one(monkeypatch):
    async def go():
        s = ScrollSession()
        d, stop, task = await _o_looked(s)
        _after_a_wait(d, monkeypatch, then=lambda: setattr(s, "runner_text", [
            ocr.Seen("Search", (170.0, 275.0, 60.0, 20.0), 1.0)]))
        e = await refused(d, "scroll", {"target": {"ref": "o1"}, "direction": "down", "look": False})
        assert e.code == "stale" and "(it is at (200, 285) now)" in str(e) and s.sent == []
        await finish(stop, task)
    run(go)


def test_ro_m1_the_late_check_runs_once_an_action_a_scrolls_later_drags_dont_run_it():
    async def go():
        s = ScrollSession()
        s.late_check_calls = []

        async def late(at):
            s.late_check_calls.append(at)
            return True
        s.touch_rules = session_module.TouchRules(session_module.Front(session_module.FRONT_APP, SHOP[1]))
        s.late_check = late
        await s._goes("scroll", 300, (215.0, 400.0))
        assert len(s.late_check_calls) == 2  # (a read each time: two at most in one gate, the front checked after each)
        await s._goes("scroll", 300, (215.0, 400.0))  # (the second drag of `to: "end"`)
        assert len(s.late_check_calls) == 2 and s.late_check is None
    run(go)


def test_ro_m1_a_scroll_to_the_end_aimed_by_an_o_ref_goes_when_its_text_stayed(monkeypatch):
    async def go():
        s = ScrollSession()
        d, stop, task = await _o_looked(s)
        _after_a_wait(d, monkeypatch)
        s.moves, s.between = 150.0, 1.1  # (each drag moves the text; a drag and its read take about 2 s on the phone)
        await d.run_tool("a", "scroll", {"target": {"ref": "o1"}, "to": "end", "look": False})
        assert len(s.sent) >= 2 and {k for k, _ in s.sent} == {"scroll"}  # (every drag went: the text moved by them)
        await finish(stop, task)
    run(go)


# ---- RO-M2 ----

def _banner_during_the_late_read(s, stays: bool):
    state = {"banner": False}
    def at_point(x, y):
        if state["banner"]:
            state["seen"] = True  # (a question while the banner was down: the check after the late read)
            return hit_of(SPRINGBOARD, PLATE)
        return APP
    answers(s, status_bar(), at_point)
    real = s.read_text

    async def read_text(level="accurate"):
        seen = await real(level)
        if s._gate_at is not None and not state.get("done"):
            state["banner"] = True  # (a banner comes down while the late read is made)
            if not stays:
                async def leave():
                    await asyncio.sleep(0.02)
                    state["banner"], state["done"] = False, True
                asyncio.ensure_future(leave())
        return seen
    s.read_text = read_text
    return state


def test_ro_m2_a_banner_that_comes_during_the_late_read_is_seen_by_the_runners_path(monkeypatch):
    async def go():
        s = unread()
        s.runner_text = [SEARCH]
        d, stop, task = await _o_looked(s)
        state = _banner_during_the_late_read(s, stays=True)
        _after_a_wait(d, monkeypatch)
        e = await refused(d, "tap", {"target": {"ref": "o1"}, "look": False})
        assert state["banner"] and e.code == "not_actionable" and "covered" in str(e) and s.sent == []
        await finish(stop, task)
    run(go)


def test_ro_m2_and_by_the_hands(monkeypatch):
    async def go():
        s = unread()
        s.by_hands = True
        s.runner_text = [SEARCH]
        d, stop, task = await _o_looked(s)
        state = _banner_during_the_late_read(s, stays=True)
        _after_a_wait(d, monkeypatch)
        e = await refused(d, "tap", {"target": {"ref": "o1"}, "look": False})
        assert state["banner"] and e.code == "not_actionable" and "covered" in str(e) and s.sent == []
        await finish(stop, task)
    run(go)


def test_ro_m2_a_banner_that_leaves_is_waited_out_and_the_late_read_is_not_made_again(monkeypatch):
    monkeypatch.setattr(session_module, "BANNER_WAIT", 1.0)
    monkeypatch.setattr(session_module, "BANNER_POLL", 0.05)

    async def go():
        s = unread()
        s.by_hands = True
        s.runner_text = [SEARCH]
        d, stop, task = await _o_looked(s)
        state = _banner_during_the_late_read(s, stays=False)
        _after_a_wait(d, monkeypatch)
        levels = len(s.text_levels)
        r = await d.run_tool("a", "tap", {"target": {"ref": "o1"}, "look": False})
        assert r["text"].startswith("Tapped (200, 85).") and s.sent == [("tap", (200.0, 85.0))]
        assert len(s.text_levels) == levels + 2  # (the check and one late read: not again after a short wait)
        assert state.get("done") and state.get("seen")  # (the banner was asked about after the late read, and left)
        await finish(stop, task)
    run(go)


# ---- RO-L1, RO-L2: the runner path's third question ----

def test_ro_l1_a_clock_answering_late_at_the_third_question_is_slow_and_the_hold_ended():
    def script(n, path, dev):
        if "x=66.00" in path:
            return (0.02, status_bar()) if n == 1 else (0.95, status_bar())
        return 0.31, _words("hit 304 ms, all 310 ms, client")

    async def go():
        s = _sixteen(script)
        assert await s.banner_at((383.0, 168.0), SHOP[1], hands=False) == session_module.BANNER_SLOW
        assert not s._held() and len(s.device.asked) == 3
    run(go)


def test_ro_l2_a_fast_nothing_at_the_third_question_is_a_banner_in_motion_with_no_hold():
    def script(n, path, dev):
        if "x=66.00" in path:
            return (0.02, status_bar()) if n == 1 else (0.0, _words("hit 14 ms, all 20 ms, client"))
        return 0.31, _words("hit 304 ms, all 310 ms, client")

    async def go():
        s = _sixteen(script)
        assert await s.banner_at((383.0, 168.0), SHOP[1], hands=False) == session_module.BANNER_OVER
        assert s._banner_moving and not s._held()
    run(go)


# ---- RO-L4: the check's age from before its read ----

def test_ro_l4_the_checks_age_counts_its_own_read(monkeypatch):
    async def go():
        s = unread()
        s.runner_text = [SEARCH]
        d, stop, task = await _o_looked(s)
        real = s.read_text

        async def slow_read(level="accurate"):
            await asyncio.sleep(0.6)  # (this computer's OCR, or a slow runner read)
            return await real(level)
        s.read_text = slow_read
        _after_a_wait(d, monkeypatch, seconds=0.5)  # (0.5 s after the read's answer, 1.1 s after its picture)
        levels = len(s.text_levels)
        r = await d.run_tool("a", "tap", {"target": {"ref": "o1"}, "look": False})
        assert r["text"].startswith("Tapped (200, 85).") and len(s.text_levels) == levels + 2  # (read again late)
        await finish(stop, task)
    run(go)


# ---- RN-L3's no-quiet rule, on the Unanswered branch too ----

def test_a_held_clock_question_that_gets_no_answer_on_the_line_sets_no_quiet():
    async def go():
        # (the line's own "didn't answer", which Session._call turns into Unanswered for a question asked once)
        s = _sixteen(lambda n, path, dev: (0.0, ConnectionLost("WebDriverAgent didn't answer GET /wda/tw/hit within 0 s")))
        s.runner_held = session_module.RunnerHeld(SHOP[1], SHOP[0])
        assert await s.held_answers() is False
        assert getattr(s, "_hit_quiet_until", 0.0) <= time.monotonic()
        assert len(s.device.asked) == 1
    run(go)

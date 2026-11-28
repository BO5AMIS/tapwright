"""Tapwright 2.0.3, round J: review RI's findings (its failing probes, moved here). The hands path's banner wait has one
deadline per touch and hears a pause at every poll (RI-M1); hit_at's silence is no dropped line and sets the quiet
(RI-L1, RI-L4); a qualifying flick goes on a slow app too, and the slow words name it (RI-L2, the lead's decision)."""
import asyncio
import time

import pytest
from fakes import button
from test_banner_hit_203 import BANNER, _looked
from test_daemon import finish
from test_round_i_203 import _real_session, hit_of
from test_touch_other_app_203 import SHOP, PointSession, _Hands, quick_banner, refused, run, unread  # noqa: F401

from tapwright import session as session_module
from tapwright.control import Refused
from tapwright.device import ConnectionLost, Unanswered
from tapwright.session import SPRINGBOARD, Session


def _hands_session(s: PointSession) -> _Hands:
    s.touch_rules = session_module.TouchRules(session_module.Front(session_module.FRONT_APP, SHOP[1]), True)
    hands = _Hands()
    s.hands, s.hands_on = hands, (lambda: True)
    s.before_input, s.front_apps, s.hands_connection = None, None, None

    async def ensure():
        return None
    s.ensure = ensure
    return hands


# ---- RI-M1: the hands path's wait, bounded and paused ----

def test_ri_hands_wait_is_bounded_when_clear_and_silence_alternate(monkeypatch):
    monkeypatch.setattr(session_module, "HIT_QUIET", 0.0)

    async def go():
        s = PointSession()
        hands = _hands_session(s)
        state = {"in_wait": False, "waits": 0}
        real_wait = Session._wait_banner

        async def wait_banner(self=s):
            state["waits"] += 1
            state["in_wait"] = True
            try:
                return await real_wait(s)
            finally:
                state["in_wait"] = False
        s._wait_banner = wait_banner

        def hit(x, y):
            if len(s.hit_questions) == 1:
                return hit_of(SPRINGBOARD, BANNER)  # the banner, seen once
            return hit_of(SHOP[1], (0, 0, 430, 932)) if state["in_wait"] else Unanswered("no answer")
        s.hit = hit
        await s._touching(60, (50.0, 85.0))
        began = time.monotonic()
        with pytest.raises(Refused) as e:
            await asyncio.wait_for(Session._by_hands(s, "tap", lambda before: hands.tap(50, 85, 60, before=before)),
                                   2.0)
        took = time.monotonic() - began
        assert not hands.sent and str(e.value) == session_module.COVERED_BANNER and took < 0.5, \
            f"{state['waits']} waits, {len(s.hit_questions)} hit questions in {took:.2f} s"
    run(go)


def test_ri_runner_wait_is_bounded_in_the_same_sequence(monkeypatch):
    monkeypatch.setattr(session_module, "HIT_QUIET", 0.0)

    async def go():
        s = unread()
        s.tree, s.tier = [button("e1", "Menu", rect=(20, 70, 60, 30))], "full"
        d, stop, task = await _looked(s)
        n = {"q": 0}

        def hit(x, y):
            n["q"] += 1
            return hit_of(SPRINGBOARD, BANNER) if n["q"] == 1 else Unanswered("no answer")
        s.hit = hit
        e = await refused(d, "tap", {"target": {"ref": "e1"}, "look": False})
        assert str(e) == session_module.COVERED_BANNER and s.sent == []
        await finish(stop, task)
    run(go)


@pytest.mark.parametrize("who", ["paused", "busy"])  # (a pause, or a person's live-view input)
def test_ri_a_pause_or_a_person_during_the_hands_wait_stops_it_within_a_poll(monkeypatch, who):
    monkeypatch.setattr(session_module, "BANNER_WAIT", 0.5)
    monkeypatch.setattr(session_module, "BANNER_POLL", 0.01)

    async def go():
        s = PointSession()
        hands = _hands_session(s)
        stopped = {"on": False}
        s.hit = lambda x, y: hit_of(SPRINGBOARD, BANNER)

        async def guard():
            if stopped["on"]:
                raise Refused(who, who)
        s.execution_guard = guard
        await s._touching(60, (50.0, 85.0))

        async def press():
            await asyncio.sleep(0.05)
            stopped["on"] = True
        asyncio.create_task(press())
        began = time.monotonic()
        with pytest.raises(Refused) as e:
            await Session._by_hands(s, "tap", lambda before: hands.tap(50, 85, 60, before=before))
        took = time.monotonic() - began
        assert not hands.sent and e.value.code == who and took < 0.2, f"{e.value.code} after {took:.2f} s"
        assert s._gate_at is None and not s._banner_seen  # (the gate ends with it)
    run(go)


def test_a_pause_during_the_runner_paths_wait_is_heard_within_a_poll(monkeypatch):
    monkeypatch.setattr(session_module, "BANNER_WAIT", 0.5)
    monkeypatch.setattr(session_module, "BANNER_POLL", 0.01)

    async def go():
        s = unread()
        s.touch_rules = session_module.TouchRules(session_module.Front(session_module.FRONT_APP, SHOP[1]), True)
        s.hit = lambda x, y: hit_of(SPRINGBOARD, BANNER)
        stopped = {"on": False}

        async def guard():
            if stopped["on"]:
                raise Refused("paused", "paused")
        s.execution_guard = guard

        async def press():
            await asyncio.sleep(0.05)
            stopped["on"] = True
        asyncio.create_task(press())
        began = time.monotonic()
        with pytest.raises(Refused) as e:
            await Session._front_rules(s, (50.0, 85.0), wait=True)
        assert e.value.code == "paused" and time.monotonic() - began < 0.2
    run(go)


def test_the_touchs_deadline_resets_with_the_next_touch():
    async def go():
        s = PointSession()
        s._banner_until = time.monotonic() - 1.0  # (a past touch's, left by a cancel)
        await s._touching(60, (50.0, 85.0))
        assert s._banner_until is None and s._banner_seen is False
    run(go)


# ---- RI-L1, RI-L4: hit_at ----

@pytest.mark.parametrize("status_answers", [False, True])
def test_ri_hit_at_silence_sets_the_quiet_and_is_no_dropped_line(status_answers):
    async def go():
        s = _real_session(status_answers=status_answers)

        async def routes(strict=False):
            return None
        s._fast_routes = routes
        with pytest.raises(Refused) as e:  # (route version 11: a proof that couldn't be made refuses the touch)
            await Session.hit_at(s, 100.0, 300.0)
        assert getattr(e.value, "hit_check", {}).get("gate") == "hit_timeout"
        assert s.sid == "s1" and getattr(s, "_hit_quiet_until", 0) > time.monotonic()
        assert "/status" not in s.device.asked and len(s.device.asked) == 1
    run(go)


def test_hit_at_waits_for_the_lock_the_banner_check_holds():
    async def go():
        s = _real_session(status_answers=True)
        order: list[str] = []

        async def routes(strict=False):
            return None
        s._fast_routes = routes
        lock = s._hit_lock = asyncio.Lock()
        await lock.acquire()  # (a banner check's question is out)

        async def http(method, path, payload=None, timeout=30.0):
            order.append("hit_at asked")
            return {"value": {"t": "XCUIElementTypeButton", "r": [0, 280, 200, 40], "b": SHOP[1],
                              "identity": "verified"}}
        s.device.http = http

        async def release():
            await asyncio.sleep(0.1)
            order.append("released")
            lock.release()
        asyncio.create_task(release())
        hit = await Session.hit_at(s, 100.0, 300.0)
        assert hit is not None and order == ["released", "hit_at asked"]
    run(go)


# ---- RI-L2 (the lead): the flick on a slow app ----

def test_a_flick_away_on_a_slow_app_goes_and_the_slow_words_name_it():
    async def go():
        s = unread()
        d, stop, task = await _looked(s)
        s.hit = lambda x, y: Unanswered("no answer")
        await d.run_tool("a", "swipe", {"x0": 215, "y0": 70, "x1": 215, "y1": 2, "look": False})
        assert s.sent == [("swipe", (215, 70))]
        assert "swipe from it up off the top edge" in session_module.COVERED_SLOW
        e = await refused(d, "swipe", {"x0": 215, "y0": 70, "x1": 215, "y1": 30, "look": False})  # (no flick)
        assert str(e) == session_module.COVERED_SLOW
        await finish(stop, task)
    run(go)


def test_a_flick_on_the_home_screen_goes_on_slow_too():
    from test_banner_hit_203 import _home

    async def go():
        s = _home()
        d, stop, task = await _looked(s)
        s.hit = lambda x, y: TimeoutError()
        await d.run_tool("a", "swipe", {"x0": 215, "y0": 70, "x1": 215, "y1": 2, "look": False})
        assert s.sent == [("swipe", (215, 70))]
        await finish(stop, task)
    run(go)


def test_connection_lost_is_still_raised_for_a_real_drop():
    """(a dropped line that isn't a silence stays one: `once` changes only "didn't answer")"""
    async def go():
        s = _real_session(status_answers=True)

        async def http(method, path, payload=None, timeout=30.0):
            raise ConnectionLost("the tunnel closed")
        s.device.http = http
        with pytest.raises(ConnectionLost):
            await s.banner_at((200.0, 85.0), SHOP[1])
    run(go)

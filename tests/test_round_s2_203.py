"""Tapwright 2.0.3, round S, S-F2 (the showcase's practice on f5eb807): a top-zone touch was refused slow after 8.9 s
(its banner wait's limit, under HIT_HELD), every clock question meanwhile "no element answered"; the hold wasn't
carried, the next look's front route went into the held runner, was taken for a lost line, and a reconnect and a new
session followed (47.9 s, 0 elements). Now a hold still standing at the end of a call (no real answer of the clock
since it was set) is carried to the next call, and a front route that gets no answer while the runner answers the clock
a slow "no element answered" is the given-up state, never a lost line. (Round T, review RS-H2) The carried hold is still
a hold: the next call waits out the rest of it (the clock only), and answers not_answering only once it is HIT_HELD
old."""
import asyncio
import time

import pytest
from test_banner_hit_203 import _looked
from test_daemon import finish
from test_round_i_203 import hit_of
from test_round_k_203 import PLATE, WITNESS, _phone_session, nothing, scaled, status_bar  # noqa: F401 (fixture)
from test_touch_other_app_203 import SHOP, PointSession, _real, quick_banner, refused, run, unread  # noqa: F401

from tapwright import session as session_module
from tapwright.device import ConnectionLost
from tapwright.session import SPRINGBOARD

TAB = (215.0, 117.0)  # (the practice's tab, in the top zone below the status bar)
HELD_WORDS = ("Shop (com.example.shop) is in front and isn't answering, so nothing on the phone can be read. close_app "
              "can still close it; then open_app it again.")


class PracticeSession(PointSession):
    """The practice's phone on the fakes: a banner's plate at the tab first; then the app holds accessibility for
    `hold_for` seconds (every hit question "no element answered" after the runner's wait, the clock's too); the front
    route waits for the runner as Session's does and is noted when it goes."""

    _given_up = _real("_given_up")
    held_answers = _real("held_answers")
    hold_standing = _real("hold_standing")
    carry_hold = _real("carry_hold")
    hold_carried = _real("hold_carried")
    hold_left = _real("hold_left")
    give_up_carried = _real("give_up_carried")

    def __init__(self, hold_for: float) -> None:
        super().__init__(SHOP)
        self.tier, self.tree, self.runner_text = "root", [], []
        self.nothing_takes = 0.03
        self.hold_for, self.held_until, self.plates = hold_for, 0.0, 1
        self.fronts: list[float] = []
        self.hit = self._hit

    def _hit(self, x, y):
        if time.monotonic() < self.held_until:
            return nothing()
        if (x, y) == WITNESS:
            return status_bar()
        if (x, y) == TAB and self.plates > 0:
            self.plates -= 1
            return hit_of(SPRINGBOARD, PLATE)  # (a banner over the tab: the touch waits)
        if (x, y) == TAB and self.hold_for:
            self.held_until, self.hold_for = time.monotonic() + self.nothing_takes + self.hold_for, 0.0
            return nothing()
        return {"t": "XCUIElementTypeOther", "r": [0, 0, 430, 932], "b": SHOP[1], "identity": "verified"}

    async def front_app(self):
        await self.channel_free()
        self.fronts.append(time.monotonic())
        return await super().front_app()


def test_s_f2_a_touch_refused_slow_with_the_hold_standing_carries_it_and_the_next_look_waits_out_its_rest(
        scaled, monkeypatch):
    monkeypatch.setattr(session_module, "BANNER_WAIT", 0.89)  # (8.9 s on the phone, at a tenth: under HIT_HELD)
    monkeypatch.setattr(session_module, "BANNER_POLL", 0.05)

    async def go():
        s = PracticeSession(hold_for=6.0)  # (60 s on the phone)
        d, stop, task = await _looked(s)
        d.heartbeat = 3600.0  # (the heartbeat's own clock question has its own test, round O)
        attempts, sid = d._attempts, s.sid
        e = await refused(d, "tap", {"target": {"x": TAB[0], "y": TAB[1]}, "look": False})
        assert e.code == "not_actionable" and str(e).startswith("covered:") and s.sent == []
        # (carried as a hold, round T: the state stays live, no agent's list dropped)
        assert s.hold_carried() and d.stuck is None and d.state == "live" and getattr(s, "runner_held", None) is None
        assert not any(v.dropped for v in d.control.clients.values())
        # the next look: the clock only, until the hold is HIT_HELD old (from its first slow answer, not from the
        # look); then not_answering, with no front question into the hold and no reconnect
        s.hit_questions.clear()
        fronts = len(s.fronts)
        left = s.hold_left()
        assert 0.1 < left < session_module.HIT_HELD
        began = time.monotonic()
        e = await refused(d, "look", {"screenshot": False})
        took = time.monotonic() - began
        assert e.code == "not_answering" and str(e) == HELD_WORDS
        assert left - 0.02 <= took < left + 0.25, (took, left)  # (the limit, plus one step and one question at most)
        assert set(s.hit_questions) == {WITNESS} and 1 <= len(s.hit_questions) <= left / 0.05 + 2
        assert len(s.fronts) == fronts
        assert isinstance(d.stuck, session_module.RunnerHeld) and d.state == "not-answering"
        assert d._attempts == attempts and s.sid == sid and not d._lost.is_set()
        # given up: every later call asks the clock once, not_answering at once (round N)
        s.nothing_takes = 0.3
        s.hit_questions.clear()
        began = time.monotonic()
        e = await refused(d, "look", {"screenshot": False})
        assert e.code == "not_answering" and time.monotonic() - began < 0.5 and s.hit_questions == [WITNESS]
        # once the app answers, the next call works and reads
        s.held_until, s.nothing_takes = 0.0, 0.03
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert r["text"].startswith("Screen ") and d.state == "live" and d.stuck is None and len(s.fronts) > fronts
        await finish(stop, task)
    run(go)


def test_a_refused_touch_whose_hold_ended_before_the_refusal_leaves_no_mark():
    async def go():
        s = unread()
        s.by_hands = True
        s.nothing_takes = 0.03
        # (the point: a slow "no element answered" at every question, the clock answering the status bar every time:
        # the clock's real answer comes after each hold is set, so none stands at the refusal)
        s.hit = lambda x, y: status_bar() if (x, y) == WITNESS else nothing()
        d, stop, task = await _looked(s)
        e = await refused(d, "tap", {"target": {"x": TAB[0], "y": TAB[1]}, "look": False})
        assert e.code == "not_actionable" and s.sent == []
        assert not isinstance(d.stuck, session_module.RunnerHeld) and d.state == "live"
        assert getattr(s, "runner_held", None) is None
        await finish(stop, task)
    run(go)


def test_hold_standing_is_a_hold_with_no_real_clock_answer_after_it():
    s = unread()
    s._hold("point", TAB)
    assert Session_standing(s)
    s._clock_real_at = time.monotonic()
    assert not Session_standing(s)
    s._hold("point", TAB)
    assert Session_standing(s)
    s.runner_held = session_module.RunnerHeld(SHOP[1], SHOP[0])
    assert not Session_standing(s)  # (given up)
    s.runner_held = None
    session_module.Session.carry_hold(s)
    assert not Session_standing(s)  # (already carried)


def Session_standing(s) -> bool:
    return session_module.Session.hold_standing(s)


# ---- the front route that got no answer: no lost line while the runner answers the clock ----

class _StalledFront(type(_phone_session(0.0).device)):
    """The front route gets no answer (taken for a dropped line); the hit test answers as `_PhoneDev` does."""

    async def http(self, method, path, payload=None, timeout=30.0):
        if path.split("?")[0] == session_module.FRONT_PATH:
            self.asked.append((time.monotonic(), session_module.FRONT_PATH))
            raise ConnectionLost("WebDriverAgent didn't answer GET /wda/tw/front within 5 s")
        return await super().http(method, path, payload, timeout)


@pytest.mark.parametrize("held", [True, False])
def test_a_front_route_with_no_answer_while_the_runner_answers_the_clock_is_the_held_state(held):
    async def go():
        s = _phone_session(free_at=time.monotonic() + (60.0 if held else 0.0))
        s.device.__class__ = _StalledFront
        s.app, s.app_names = ("Shop", SHOP[1]), {SHOP[1]: "Shop"}

        async def in_front():
            return [SHOP[1]]
        s.device.foreground_apps = in_front
        if held:
            with pytest.raises(session_module.RunnerHeld) as e:
                await s.front_app()
            assert e.value.bundle_id == SHOP[1] and s.runner_held is not None and s.sid == "s1"  # (session kept)
        else:  # (the clock answers for real: the front route's silence is something else, a dropped line as before)
            with pytest.raises(ConnectionLost) as e:
                await s.front_app()
            assert not isinstance(e.value, session_module.RunnerHeld) and getattr(s, "runner_held", None) is None
        assert [p for _, p in s.device.asked].count(session_module.HIT_PATH) == 1
    asyncio.run(go())

"""Tapwright 2.0.3, round K (phone 2026-11-27, BRIEF-K version 4). Over an app held upright the banner check asks the
status bar's clock first, then the touch's point once: an ordinary banner sits below the status bar, which keeps
answering as if nothing were there (F1), so only the point can see it; a question the app doesn't answer holds the
runner for seconds (F4), so nothing is asked twice, and the hold is waited for before the front route and a tree read;
a "no element answered" that comes fast is SpringBoard's own (a banner coming or going, F2); the read's banner note
asks nothing after a read cut short."""
import asyncio
import base64
import time

import pytest
from fakes import button
from test_banner_hit_203 import BANNER, _home, _looked
from test_daemon import finish, make
from test_round_i_203 import hit_of
from test_touch_other_app_203 import SHOP, quick_banner, refused, run, unread  # noqa: F401 (fixture)

from tapwright import session as session_module
from tapwright import ui
from tapwright.control import Refused
from tapwright.device import DeviceError, Unanswered
from tapwright.session import SPRINGBOARD, Session

WITNESS = session_module.HIT_WITNESS
CLOCK = [55, 18, 52, 23]  # the status bar's clock (F7)
PLATE = [8, 56, 414, 73]  # an ordinary banner's plate, below the status bar (F1)
PLATE_TEXT = [72, 67, 294, 23]
WHOLE = [0, 0, 430, 932]  # SpringBoard's whole screen (F2: a banner coming or going)
APP = {"t": "XCUIElementTypeOther", "r": [0, 0, 430, 932], "b": SHOP[1], "identity": "verified"}


def nothing(fast: bool = False) -> DeviceError:
    """ "no element answered": after the runner's 0.3 s (the app didn't answer), or `fast` (SpringBoard's own)."""
    e = DeviceError(f"unsupported operation: {session_module.HIT_NOTHING}")
    e.fast = fast
    return e


def status_bar(rect=CLOCK):
    return hit_of(SPRINGBOARD, rect, "XCUIElementTypeStaticText")


def answers(s, witness, point):
    """The hit test: `witness` at the status bar's clock, `point` anywhere else (each a dict, an exception, or a
    callable of (x, y))."""
    def hit(x, y):
        said = witness if (x, y) == WITNESS else point
        return said(x, y) if callable(said) else said
    s.hit = hit


def busy_app(s, point_first=None):
    """(round L) A busy app, as on the phone (BRIEF-K F4): the clock answers the status bar until the app's point is
    asked; that question gets "no element answered" after the runner's 0.3 s and leaves the runner held, so every
    question after it, the clock's too, says "no element answered" as slowly. `point_first`: the answers at the point
    before the app is asked (a callable of the count of questions so far, None when the app is asked)."""
    state = {"held": False}

    def hit(x, y):
        if state["held"]:
            return nothing()
        if (x, y) == WITNESS:
            return status_bar()
        early = point_first(len(s.hit_questions)) if point_first is not None else None
        if early is not None:
            return early
        state["held"] = True
        return nothing()
    s.hit = hit
    return state


def asked_twice_at_one_point(s) -> bool:
    return any(s.hit_questions[i] == s.hit_questions[i + 1] for i in range(len(s.hit_questions) - 1))


# ---- 1. the status bar holds, the point says nothing slowly: clear, the hold set ----

def test_the_witness_holds_and_a_slow_nothing_at_the_point_is_clear_with_the_hold_set():
    async def go():
        s = unread()
        busy_app(s)  # (round L: the hold confirmed by the clock asked once more; the hands' touch)
        assert await s.banner_at((383.0, 168.0), SHOP[1], hands=True) == session_module.BANNER_CLEAR
        assert s.hit_questions == [WITNESS, (383.0, 168.0), WITNESS] and s._held()
    run(go)


def test_on_a_busy_app_a_point_tap_and_an_element_in_the_zone_go():
    async def go():
        s = unread()
        s.tree, s.tier = [button("e1", "Close", rect=(363, 148, 40, 40))], "full"
        d, stop, task = await _looked(s)
        s.by_hands = True  # (round L, review RK M2: the witness's clear is for the hands' touch)
        busy_app(s)
        await d.run_tool("a", "tap", {"target": {"x": 383, "y": 168}, "look": False})
        s._held_until = 0.0  # (the app answered meanwhile)
        busy_app(s)
        await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False})
        assert s.sent == [("tap", (383, 168)), ("tap", (383.0, 168.0))]
        await finish(stop, task)
    run(go)


# ---- 2. the status bar holds, the point answers ----

def test_the_point_answering_the_app_is_clear_with_no_hold():
    async def go():
        s = unread()
        answers(s, status_bar(), APP)
        assert await s.banner_at((383.0, 168.0), SHOP[1]) == session_module.BANNER_CLEAR
        assert not s._held() and s.hit_questions == [WITNESS, (383.0, 168.0)]
    run(go)


@pytest.mark.parametrize("plate", [hit_of(SPRINGBOARD, PLATE), hit_of(SPRINGBOARD, PLATE_TEXT, "XCUIElementTypeStaticText")])
def test_an_ordinary_banners_plate_at_the_point_is_waited_out_and_covered_then_goes(plate):
    async def go():
        s = unread()
        d, stop, task = await _looked(s)
        answers(s, status_bar(), plate)
        e = await refused(d, "tap", {"target": {"x": 215, "y": 80}, "look": False})
        assert str(e) == session_module.COVERED_BANNER and s.sent == []
        answers(s, status_bar(), lambda x, y: plate if len(s.hit_questions) <= 2 else APP)
        s.hit_questions.clear()
        await d.run_tool("a", "tap", {"target": {"x": 215, "y": 80}, "look": False})
        assert s.sent == [("tap", (215, 80))]
        await finish(stop, task)
    run(go)


def test_another_process_at_the_point_is_unsure_as_before():
    async def go():
        s = unread()
        answers(s, status_bar(), hit_of("com.apple.GameOverlayUI", [81, 79, 268, 56], "XCUIElementTypeButton"))
        assert await s.banner_at((200.0, 85.0), SHOP[1]) == session_module.BANNER_UNSURE
    run(go)


# ---- 3. F1 itself: version 3 of the brief failed here ----

def test_f1_the_status_bar_answering_never_lets_a_touch_onto_the_plate_below_it():
    async def go():
        s = unread()
        d, stop, task = await _looked(s)
        answers(s, status_bar(), lambda x, y: hit_of(SPRINGBOARD, PLATE) if 56 <= y <= 129 else APP)
        e = await refused(d, "tap", {"target": {"x": 215, "y": 80}, "look": False})
        assert e.code == "not_actionable" and s.sent == []
        await finish(stop, task)
    run(go)


# ---- 4. SpringBoard's whole screen ----

def test_springboards_whole_screen_at_the_point_is_over_an_app_and_clear_on_the_home_screen():
    async def go():
        s = unread()
        answers(s, status_bar(), hit_of(SPRINGBOARD, WHOLE))
        assert await s.banner_at((215.0, 80.0), SHOP[1]) == session_module.BANNER_OVER
        h = _home()
        h.hit = lambda x, y: hit_of(SPRINGBOARD, WHOLE)
        assert await h.banner_at((215.0, 80.0), None) == session_module.BANNER_CLEAR
    run(go)


# ---- 5. a fast "no element answered" ----

def test_a_fast_nothing_at_the_point_is_over_with_no_hold():
    async def go():
        s = unread()
        answers(s, status_bar(), nothing(fast=True))
        assert await s.banner_at((215.0, 80.0), SHOP[1]) == session_module.BANNER_OVER
        assert not s._held()
    run(go)


def test_a_fast_nothing_at_the_witness_is_over_and_the_point_not_asked():
    async def go():
        s = unread()
        answers(s, nothing(fast=True), APP)
        assert await s.banner_at((215.0, 80.0), SHOP[1]) == session_module.BANNER_OVER
        assert s.hit_questions == [WITNESS] and not s._held()
    run(go)


# ---- 6. a banner at the witness ----

@pytest.mark.parametrize("witness", [hit_of(SPRINGBOARD, BANNER), hit_of(session_module.IN_CALL, [8, 11, 414, 76]),
                                     hit_of(SPRINGBOARD, WHOLE)])
def test_a_banner_or_a_call_or_the_whole_screen_at_the_witness_is_over_and_the_point_not_asked(witness):
    async def go():
        s = unread()
        answers(s, witness, APP)
        assert await s.banner_at((383.0, 168.0), SHOP[1]) == session_module.BANNER_OVER
        assert s.hit_questions == [WITNESS]
    run(go)


# ---- 7. the witness says nothing slowly: the runner is held ----

def test_a_slow_nothing_at_the_witness_is_slow_with_the_hold_and_the_point_not_asked():
    async def go():
        s = unread()
        s.tree, s.tier = [button("e1", "Close", rect=(363, 148, 40, 40))], "full"
        d, stop, task = await _looked(s)
        answers(s, nothing(), APP)
        assert await s.banner_at((383.0, 168.0), SHOP[1]) == session_module.BANNER_SLOW
        assert s.hit_questions == [WITNESS] and s._held()
        e = await refused(d, "tap", {"target": {"x": 383, "y": 168}, "look": False})
        assert str(e) == session_module.COVERED_SLOW
        e = await refused(d, "tap", {"target": {"ref": "e1"}, "look": False})  # (round I: an element too)
        assert str(e) == session_module.COVERED_SLOW
        await d.run_tool("a", "swipe", {"x0": 215, "y0": 70, "x1": 215, "y1": 2, "look": False})  # (round J)
        assert s.sent == [("swipe", (215, 70))] and s.hit_questions == [WITNESS]  # (held: nothing asked since)
        await finish(stop, task)
    run(go)


# ---- 8. the witness silent ----

def test_a_silent_witness_is_slow_the_quiet_and_the_point_not_asked():
    async def go():
        s = unread()
        answers(s, Unanswered("no answer"), APP)
        assert await s.banner_at((383.0, 168.0), SHOP[1]) == session_module.BANNER_SLOW
        assert s.hit_questions == [WITNESS] and s._hit_quiet_until > time.monotonic()
    run(go)


# ---- 9. no status bar at the clock ----

@pytest.mark.parametrize("point,want,held", [(APP, session_module.BANNER_CLEAR, False),
                                             (nothing(), session_module.BANNER_SLOW, True)])
def test_without_a_status_bar_at_the_clock_the_point_is_asked_once(point, want, held):
    async def go():
        s = unread()
        answers(s, APP, point)
        assert await s.banner_at((383.0, 168.0), SHOP[1]) == want
        assert s.hit_questions == [WITNESS, (383.0, 168.0)] and s._held() is held
    run(go)


# ---- 10. a hold known ----

def test_a_hold_asks_nothing_of_the_banner_check_nor_of_hit_at():
    async def go():
        s = unread()
        s._held_until = time.monotonic() + 5.0
        answers(s, status_bar(), APP)
        assert await s.banner_at((383.0, 168.0), SHOP[1]) == session_module.BANNER_SLOW
        assert s.hit_questions == []
    run(go)


class _PhoneDev:
    """A device whose runner is held by a pending question until `free_at` (time.monotonic): every hit question until
    then says "no element answered" after the runner's 0.3 s (scaled); the front route and the tree read answer."""
    rsd = object()

    def __init__(self, free_at: float, nothing_takes: float = 0.03):
        self.free_at, self.nothing_takes = free_at, nothing_takes
        self.asked: list[tuple[float, str]] = []

    def log(self, *a):
        pass

    async def http(self, method, path, payload=None, timeout=30.0):
        bare = path.split("?")[0]
        self.asked.append((time.monotonic(), bare))
        if bare == session_module.HIT_PATH:
            if time.monotonic() < self.free_at:
                await asyncio.sleep(self.nothing_takes)
                raise DeviceError(f"unsupported operation: {session_module.HIT_NOTHING}")
            return {"value": status_bar()}
        if bare == session_module.FRONT_PATH:
            return {"value": {"b": SHOP[1], "pid": 9}}
        if bare == ui.SOURCE_PATH.split("?")[0]:
            return {"value": "<XCUIElementTypeApplication/>"}
        if bare.endswith("/wda/locked"):
            return {"value": False}
        if bare == "/screenshot":
            return {"value": base64.b64encode(b"png").decode()}
        raise AssertionError(f"unexpected {method} {path}")


def _phone_session(free_at: float):
    s = object.__new__(Session)
    s.device = _PhoneDev(free_at)
    s.sid, s.front_apps, s.tw_version, s.turn, s.screen = "s1", None, 11, 0, (430.0, 932.0)
    s.execution_guard, s.before_input, s._gate_at, s.input_point = None, None, None, None
    s.fast_read = False

    async def ensure():
        return None
    s.ensure = ensure

    async def follow(source):
        return None
    s._follow_orientation = follow
    return s


@pytest.fixture
def scaled(monkeypatch):
    """The hold's times at a tenth: HIT_HELD 1.2 s, a poll every 0.07 s, 0.1 s at most a question."""
    monkeypatch.setattr(session_module, "HIT_HELD", 1.2)
    monkeypatch.setattr(session_module, "HIT_FREE_EVERY", 0.07)
    monkeypatch.setattr(session_module, "HIT_FREE_WAIT", 0.1)
    monkeypatch.setattr(session_module, "HIT_NOTHING_SLOW", 0.025)
    # (round M: the clear's lines, at a tenth too)
    monkeypatch.setattr(session_module, "HIT_NOTHING_MARGIN", 0.02)
    monkeypatch.setattr(session_module, "HIT_WITNESS_PROMPT", 0.015)


def test_channel_free_asks_the_witness_every_poll_and_ends_the_hold_at_the_first_answer(scaled):
    async def go():
        s = _phone_session(free_at=time.monotonic() + 0.5)
        s._held_until = time.monotonic() + session_module.HIT_HELD
        began = time.monotonic()
        await s.channel_free()
        took = time.monotonic() - began
        hits = [t for t, p in s.device.asked if p == session_module.HIT_PATH]
        assert not s._held() and 0.45 <= took < 0.7 and len(hits) >= 5
        gaps = [b - a for a, b in zip(hits, hits[1:])]
        assert all(g >= 0.06 for g in gaps)  # (about every HIT_FREE_EVERY, never piled up)
    run(go)


def test_channel_free_gives_up_at_the_holds_end(scaled):
    async def go():
        s = _phone_session(free_at=time.monotonic() + 60.0)
        s._held_until = time.monotonic() + session_module.HIT_HELD
        began = time.monotonic()
        await s.channel_free()
        assert not s._held() and session_module.HIT_HELD - 0.1 <= time.monotonic() - began < session_module.HIT_HELD + 0.3
    run(go)


def test_the_front_route_and_a_tree_read_wait_for_the_hold_and_the_lock_check_and_screenshot_dont(scaled):
    async def go():
        s = _phone_session(free_at=time.monotonic() + 0.4)
        s._held_until = time.monotonic() + session_module.HIT_HELD
        assert await s.locked() is False  # (not an accessibility request: at once)
        await s.screenshot_png()
        assert [p for _, p in s.device.asked] == ["/session/s1/wda/locked", "/screenshot"]
        began = time.monotonic()
        assert await s.front_app() == (SHOP[1], 9)
        front_at = next(t for t, p in s.device.asked if p == session_module.FRONT_PATH)
        assert front_at - began >= 0.35  # (after the hold ended)
        s._held_until = time.monotonic() + session_module.HIT_HELD
        s.device.free_at = time.monotonic() + 0.3
        began = time.monotonic()
        await s._read_tree(5.0)
        read_at = next(t for t, p in reversed(s.device.asked) if p == ui.SOURCE_PATH.split("?")[0])
        assert read_at - began >= 0.25
    run(go)


def test_a_pause_during_the_holds_wait_is_heard(scaled):
    async def go():
        s = _phone_session(free_at=time.monotonic() + 60.0)
        s._held_until = time.monotonic() + session_module.HIT_HELD
        stopped = {"on": False}

        async def guard():
            if stopped["on"]:
                raise Refused("paused", "paused")
        s.execution_guard = guard

        async def press():
            await asyncio.sleep(0.15)
            stopped["on"] = True
        asyncio.create_task(press())
        began = time.monotonic()
        with pytest.raises(Refused) as e:
            await s.channel_free()
        assert e.value.code == "paused" and time.monotonic() - began < 0.4
    run(go)


def test_hit_at_respects_the_hold(scaled):
    async def go():
        s = _phone_session(free_at=time.monotonic() + 60.0)
        s._held_until = time.monotonic() + session_module.HIT_HELD

        async def routes(strict=False):
            return None
        s._fast_routes = routes
        with pytest.raises(Refused) as e:
            await Session.hit_at(s, 100.0, 300.0)
        assert e.value.hit_check["gate"] == "hit_held" and s.device.asked == []
    run(go)


# ---- 11. the whole sequence on a hung app ----

def test_a_hung_app_the_tap_goes_at_once_and_the_read_after_waits_with_no_lost_connection(scaled):
    async def go():
        s = _phone_session(free_at=0.0)
        hung_from = time.monotonic()
        real_http = s.device.http

        async def http(method, path, payload=None, timeout=30.0):
            bare = path.split("?")[0]
            if bare == session_module.HIT_PATH and "x=320.00" in path:
                s.device.free_at = time.monotonic() + 0.9  # (the question to the app holds the runner: 9 s scaled)
                s.device.asked.append((time.monotonic(), bare))
                await asyncio.sleep(0.03)
                raise DeviceError(f"unsupported operation: {session_module.HIT_NOTHING}")
            return await real_http(method, path, payload, timeout)
        s.device.http = http
        # (the tap goes at once, by the hands; round L: the hold confirmed by the clock once more)
        assert await s.banner_at((320.0, 83.0), SHOP[1], hands=True) == session_module.BANNER_CLEAR
        assert time.monotonic() - hung_from < 0.2 and s._held()
        began = time.monotonic()
        assert await s.front_app() == (SHOP[1], 9)  # (the read after it: waits for the runner, no lost connection)
        assert 0.8 <= time.monotonic() - began < 1.2 and s.sid == "s1"
    run(go)


# ---- 12. no witness ----

@pytest.mark.parametrize("case", ["landscape", "home", "status_bar_point"])
def test_no_witness_one_try_and_a_slow_nothing_sets_the_hold(case):
    async def go():
        s = _home() if case == "home" else unread()
        app = None if case == "home" else SHOP[1]
        at = (215.0, 40.0) if case == "status_bar_point" else (200.0, 85.0)
        if case == "landscape":
            s.screen, s.turn = (932.0, 430.0), 90
        answers(s, status_bar(), nothing())
        assert await s.banner_at(at, app) == session_module.BANNER_SLOW
        assert len(s.hit_questions) == 1 and WITNESS not in s.hit_questions and s._held()
    run(go)


def test_the_turn_unknown_asks_nothing():
    async def go():
        s = unread()
        s.screen, s.turn = (932.0, 430.0), -1
        answers(s, status_bar(), APP)
        assert await s.banner_at((200.0, 85.0), SHOP[1]) == session_module.BANNER_UNSURE
        assert s.hit_questions == []
    run(go)


# ---- 13. a banner seen: only a clear ends the wait ----

def test_after_a_banner_was_seen_only_a_clear_ends_the_wait():
    async def go():
        s = unread()
        d, stop, task = await _looked(s)
        s.by_hands = True  # (round L: the hands' path, where the witness's clear is taken)
        # (the plate first; then the status bar and a slow "nothing" at the point, confirmed: the witness's clear)
        busy_app(s, lambda n: hit_of(SPRINGBOARD, PLATE) if n <= 2 else None)
        await d.run_tool("a", "tap", {"target": {"x": 215, "y": 80}, "look": False})
        assert s.sent == [("tap", (215, 80))]
        s._held_until = 0.0
        s.hit_questions.clear()
        # (the plate, then a silence: not a clear)
        answers(s, status_bar(), lambda x, y: hit_of(SPRINGBOARD, PLATE) if len(s.hit_questions) <= 2
                else Unanswered("no answer"))
        e = await refused(d, "tap", {"target": {"x": 215, "y": 80}, "look": False})
        assert str(e) == session_module.COVERED_BANNER and s.sent == [("tap", (215, 80))]
        await finish(stop, task)
    run(go)


# ---- 14. the read's note ----

@pytest.mark.parametrize("tier,source,asked", [
    ("root", None, False),  # (a read cut short, or "tree not asked")
    ("shallow", '<XCUIElementTypeApplication name="Shop"><XCUIElementTypeWindow/></XCUIElementTypeApplication>', False),
    ("full", None, True),
])
def test_the_reads_note_asks_nothing_after_a_read_cut_short(tier, source, asked):
    async def go():
        s = unread()
        s.read_app, s.screen, s.last_tier, s.last_source = SHOP, (430.0, 932.0), tier, source
        d = make(s)
        answers(s, status_bar(), lambda x, y: hit_of(SPRINGBOARD, PLATE) if (x, y) == (215.0, 80.0) else APP)
        shown = await d._banner_shown()
        if asked:
            assert shown and s.hit_questions == [WITNESS, (215.0, 80.0)]
        else:
            assert not shown and s.hit_questions == []
    run(go)


# ---- 15. never twice at one point ----

@pytest.mark.parametrize("witness,point", [(status_bar(), nothing()), (nothing(), APP), (APP, nothing()),
                                           (status_bar(), nothing(fast=True)), (Unanswered("x"), APP)])
def test_never_a_second_question_at_one_point_within_a_call(witness, point):
    async def go():
        s = unread()
        answers(s, witness, point)
        await s.banner_at((215.0, 80.0), SHOP[1])
        assert not asked_twice_at_one_point(s) and len(s.hit_questions) <= 2
    run(go)


# ---- the touch's one banner deadline holds with the runner held (round J's bound, kept) ----

def test_a_hold_during_a_banner_wait_never_stretches_the_touch_past_its_deadline(monkeypatch):
    monkeypatch.setattr(session_module, "BANNER_WAIT", 0.3)
    monkeypatch.setattr(session_module, "BANNER_POLL", 0.05)

    async def go():
        s = unread()
        real_front = s.front_app

        async def front_app():  # (as Session.front_app: the front route waits for a held runner)
            await s.channel_free()
            return await real_front()
        s.front_app = front_app
        d, stop, task = await _looked(s)
        # (the plate at the point first; then the clock says nothing slowly: the runner held for 12 s)
        answers(s, lambda x, y: status_bar() if len(s.hit_questions) <= 1 else nothing(),
                lambda x, y: hit_of(SPRINGBOARD, PLATE))
        began = time.monotonic()
        e = await refused(d, "tap", {"target": {"x": 215, "y": 80}, "look": False})
        assert str(e) == session_module.COVERED_BANNER and s.sent == [] and s._held()
        assert time.monotonic() - began < 1.5
        await finish(stop, task)
    run(go)

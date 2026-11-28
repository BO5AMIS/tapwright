"""Tapwright 2.0.3, round O: review RN's findings on round N. RN-H1 (on runner 16 the runner's own touch on a busy app
goes only once the clock answers again), RN-M1 (the phone's "hit" is the lookup alone; "all" never decides), RN-M3
(the held state ends at a heartbeat with no call), RN-M4 (an o ref's check made again right before the touch when it
is old), RN-L1 (a sheet's words never outlive their check), RN-L2 (one device question when a hold is given up),
RN-L3 (leaving the held state is given what entering it was), RN-L5 (an unrestored timeout is a refusal, never clear),
RN-L6 (open_app of the app that held the runner says it starts fresh). Each test fails on 8d3fa3d."""
import asyncio
import time

import pytest
from test_banner_hit_203 import _looked
from test_daemon import finish
from test_round_i_203 import hit_of
from test_round_k_203 import APP, PLATE, WITNESS, answers, scaled, status_bar  # noqa: F401 (fixture)
from test_round_m_203 import _session
from test_round_n_203 import POINT, HeldSession, _held
from test_touch_other_app_203 import SHOP, quick_banner, refused, run, unread  # noqa: F401 (fixture)

from tapwright import daemon as daemon_module
from tapwright import ocr
from tapwright import session as session_module
from tapwright.control import Refused
from tapwright.device import DeviceError
from tapwright.session import SPRINGBOARD

STOREKIT = "com.apple.ios.StoreKitUIService"


def _words(tail: str) -> DeviceError:
    return DeviceError(f"unsupported operation: {session_module.HIT_NOTHING} ({tail})")


def _sixteen(script):
    s = _session(script)
    s.tw_version = 16
    return s


# ---- RN-M1: the lookup's own time decides; the whole never does ----

@pytest.mark.parametrize("hands", [True, False])
def test_rn_m1_a_fast_lookup_padded_by_the_timeouts_install_is_a_banner_in_motion(hands):
    # (the reviewer's probe: a lookup of 12 ms whose AX timeout install and restore took 260 ms under load)
    async def go():
        # (the refusal also late on the link: only the lookup's own 12 ms may decide, and nothing more is asked)
        s = _sixteen(lambda n, path, dev: (0.02, status_bar()) if "x=66.00" in path
                     else (0.31, _words("hit 12 ms, all 272 ms, client")))
        assert await s.banner_at((215.0, 80.0), SHOP[1], hands=hands) == session_module.BANNER_OVER
        assert not s._held() and len(s.device.asked) == 2
    run(go)


def test_rn_m1_the_words_are_read_with_and_without_the_whole():
    said = "tapwright hit: no element answered at the point (hit 304 ms, all 318 ms, client, kAXErrorIPCTimeout)"
    assert session_module.phone_time(said.lower(), 16) == 0.304
    assert session_module.phone_words(said) == {"hit": 304.0, "all": 318.0, "how": "client", "err": "kAXErrorIPCTimeout"}
    old = "tapwright hit: no element answered at the point (hit 272 ms, client)"  # (4392284f's words)
    assert session_module.phone_time(old.lower(), 16) == 0.272
    # (review RN-M2: a client lookup that raised was asked the daemon way, the exception named after it: runner 15's rules)
    raised = "no element answered at the point (hit 300 ms, all 305 ms, daemon, NSInternalInconsistencyException)"
    assert session_module.phone_time(raised.lower(), 16) is None
    assert session_module.phone_words(raised)["err"] == "NSInternalInconsistencyException"


# ---- RN-L5: an AX timeout that couldn't be restored is a refusal, never clear ----

def test_rn_l5_an_unrestored_timeout_is_never_clear_and_never_a_proof():
    async def go():
        unrestored = DeviceError("unsupported operation: tapwright hit: the accessibility timeout could not be restored")
        s = _sixteen(lambda n, path, dev: (0.0, status_bar()) if "x=66.00" in path else (0.0, unrestored))
        assert await s.banner_at((215.0, 80.0), SHOP[1], hands=True) == session_module.BANNER_UNSURE
        s2 = _sixteen(lambda n, path, dev: (0.0, unrestored))

        async def routes(strict=False):
            return None
        s2._fast_routes = routes
        with pytest.raises(Refused) as e:
            await s2.hit_at(215.0, 600.0)
        assert e.value.hit_check["gate"] == "hit_request_failed"
    run(go)


# ---- RN-L1: the sheet's words belong to the check that found it ----

def test_rn_l1_a_sheet_found_before_is_forgotten_on_a_turned_screen():
    async def go():
        s = _sixteen(lambda n, path, dev: (0.0, status_bar()))
        s.sheet_over = STOREKIT  # (left by an earlier check)
        s.turn = None  # (which side is up not known yet)
        assert await s.banner_at((200.0, 85.0), SHOP[1]) == session_module.BANNER_UNSURE
        assert s.sheet_over is None
    run(go)


# ---- RN-L2: giving up asks the device once, within its budget ----

def test_rn_l2_giving_up_waits_for_ioss_list_once_and_asks_no_names(scaled, monkeypatch):
    monkeypatch.setattr(session_module, "HELD_FRONT_WAIT", 0.3)

    async def go():
        s = HeldSession(hold_for=9.0)
        names_asked, lists_asked = [], []

        async def slow_list():
            lists_asked.append(1)  # (CodeRabbit, PR #30: counted on entry, before the sleep can be cancelled)
            await asyncio.sleep(5.0)
            return [SHOP[1]]

        async def names(refresh=False):
            names_asked.append(1)
        s.device.foreground_apps = slow_list
        s.read_app_names = names
        s.app_names = {}
        s._held_until = time.monotonic() + session_module.HIT_HELD
        s.held_until = time.monotonic() + 60.0
        began = time.monotonic()
        with pytest.raises(session_module.RunnerHeld) as e:
            await s.front_app()
        assert time.monotonic() - began < session_module.HIT_HELD + 0.3 + 0.2
        assert names_asked == [] and e.value.bundle_id == SHOP[1]  # (the newest read names it)
        assert lists_asked == [1]  # (iOS's list was asked, once)
    run(go)


# ---- RN-L3: leaving the held state ----

def test_rn_l3_a_clock_slower_than_the_calls_question_leaves_no_quiet_and_a_way_out_hears_it():
    async def go():
        s = _sixteen(lambda n, path, dev: (0.5, status_bar()))
        s.runner_held = session_module.RunnerHeld(SHOP[1], SHOP[0])
        began = time.monotonic()
        assert await s.held_answers() is False and time.monotonic() - began < 0.6  # (a call: answered at once)
        assert getattr(s, "_hit_quiet_until", 0.0) <= time.monotonic()  # (no quiet: the next question is asked)
        assert await s.held_answers(session_module.HIT_FREE_WAIT) is True and s.runner_held is None
    run(go)


# ---- RN-M3: the held state ends with no call ----

def test_rn_m3_a_hold_that_ends_by_itself_is_live_within_a_heartbeat_and_a_persons_input_works(scaled):
    async def go():
        s = HeldSession(hold_for=9.0)
        d, stop, task = await _looked(s)
        d.heartbeat = 3600.0
        await d.run_tool("a", "tap", {"target": {"x": POINT[0], "y": POINT[1]}, "screenshot": False})
        assert isinstance(d.stuck, session_module.RunnerHeld)
        said = await d.viewer_message({"type": "gesture", "points": [[0, 0.5, 0.5], [60, 0.5, 0.5]]})
        assert said is not None and said["code"] == "not_answering"
        s.held_until = time.monotonic() + 0.3  # (the app answers its request at 30 s on the phone, at a tenth)
        d.heartbeat = 0.05
        d._lost.set()  # (wakes the supervisor's wait now: the hourly beat it was in; no line was lost)
        for _ in range(60):
            if d.state == "live":
                break
            await asyncio.sleep(0.05)
        assert d.state == "live" and d.stuck is None and s.runner_held is None
        assert await d.viewer_message({"type": "gesture", "points": [[0, 0.5, 0.5], [60, 0.5, 0.5]]}) is None
        await finish(stop, task)
    run(go)


def test_rn_m3_while_held_the_heartbeat_asks_the_clock_and_nothing_else(scaled):
    async def go():
        s = HeldSession(hold_for=9.0)
        d, stop, task = await _held(s)
        asks = list(s.runner_asks)
        attempts = d._attempts  # (CodeRabbit, PR #30: before the heartbeat)
        d.heartbeat = 0.05
        d._lost.set()
        await asyncio.sleep(0.4)
        assert isinstance(d.stuck, session_module.RunnerHeld) and d.state == "not-answering"
        assert s.hit_questions and set(s.hit_questions) == {WITNESS} and s.runner_asks == asks
        assert d._attempts == attempts  # (no reconnect: the supervisor stays out of the held state)
        await finish(stop, task)
    run(go)


# ---- RN-L6: open_app of the app that held the runner ----

def test_rn_l6_open_app_of_the_held_app_says_it_starts_fresh(scaled):
    async def go():
        s = HeldSession(hold_for=9.0)
        d, stop, task = await _held(s)
        r = await d.run_tool("a", "open_app", {"app": "com.example.shop"})
        assert r["text"].startswith("Opened com.example.shop (it was ended first: it held the phone's runner, so it "
                                    "starts fresh).")
        await finish(stop, task)
    run(go)


# ---- RN-M4: an o ref's check made again right before the touch when it is old ----

SEARCH = ocr.Seen("Search", (170.0, 75.0, 60.0, 20.0), 1.0)  # (centre 200, 85: in the top zone)


def _o_session():
    s = unread()
    s.runner_text = [SEARCH]
    return s


async def _o_looked(s):
    d, stop, task = await _looked(s)
    r = await d.run_tool("a", "look", {"screenshot": False})
    assert 'o1 "Search" @170,75 60x20' in r["text"].splitlines()
    return d, stop, task


def test_rn_m4_an_o_ref_in_the_zone_a_banner_waited_out_and_the_text_moved_meanwhile_is_stale(monkeypatch):
    monkeypatch.setattr(session_module, "BANNER_WAIT", 3.0)
    monkeypatch.setattr(session_module, "BANNER_POLL", 0.05)

    async def go():
        s = _o_session()
        d, stop, task = await _o_looked(s)
        gone = time.monotonic() + 1.2

        def at_point(x, y):
            if time.monotonic() < gone:
                return hit_of(SPRINGBOARD, PLATE)  # (a banner's plate over the text)
            s.runner_text = [ocr.Seen("Search", (170.0, 275.0, 60.0, 20.0), 1.0)]  # (the screen moved meanwhile)
            return APP
        answers(s, status_bar(), at_point)
        e = await refused(d, "tap", {"target": {"ref": "o1"}, "look": False})
        assert e.code == "stale"
        assert 'o1 "Search" is no longer where your last look read it (it is at (200, 285) now)' in str(e)
        assert s.sent == []
        await finish(stop, task)
    run(go)


def test_rn_m4_the_queue_between_the_check_and_the_touch(monkeypatch):
    async def go():
        s = _o_session()
        d, stop, task = await _o_looked(s)
        answers(s, status_bar(), APP)
        turn = d.control.agent

        async def after_a_long_read(fn):
            # (after the check: another caller's long read held the queue, and the screen moved meanwhile)
            s.runner_text = [ocr.Seen("Search", (170.0, 375.0, 60.0, 20.0), 1.0)]
            await asyncio.sleep(1.1)
            return await turn(fn)
        monkeypatch.setattr(d.control, "agent", after_a_long_read)
        e = await refused(d, "tap", {"target": {"ref": "o1"}, "look": False})
        assert e.code == "stale" and "(it is at (200, 385) now)" in str(e) and s.sent == []
        await finish(stop, task)
    run(go)


def test_rn_m4_nothing_waited_one_read_and_the_touch_goes():
    async def go():
        s = _o_session()
        d, stop, task = await _o_looked(s)
        answers(s, status_bar(), APP)
        levels = len(s.text_levels)
        r = await d.run_tool("a", "tap", {"target": {"ref": "o1"}, "look": False})
        assert r["text"].startswith("Tapped (200, 85).") and s.sent == [("tap", (200.0, 85.0))]
        assert len(s.text_levels) == levels + 1  # (the check's own read; none again right before the touch)
        await finish(stop, task)
    run(go)


def test_rn_m4_an_old_check_whose_text_stayed_goes(monkeypatch):
    async def go():
        s = _o_session()
        d, stop, task = await _o_looked(s)
        answers(s, status_bar(), APP)
        turn = d.control.agent

        async def after_a_wait(fn):
            await asyncio.sleep(1.1)
            return await turn(fn)
        monkeypatch.setattr(d.control, "agent", after_a_wait)
        levels = len(s.text_levels)
        r = await d.run_tool("a", "tap", {"target": {"ref": "o1"}, "look": False})
        assert r["text"].startswith("Tapped (200, 85).") and len(s.text_levels) == levels + 2
        await finish(stop, task)
    run(go)


def test_o_ref_fresh_is_one_second():
    assert daemon_module.O_REF_FRESH == 1.0

"""Tapwright 2.0.3, round N (phone 2026-11-28). N-F2: a video app's feed held XCTest's accessibility for more than 80 s
after a touch; the runner answered every hit question on time the whole while, but the front route asked into it at
the hold's end stalled, was taken for a lost line, and a reconnect and a new session followed (82 s). Now a hold never
costs the line: at HIT_HELD nothing more goes into the runner (session.RunnerHeld), the call answers `not_answering`
(a delivered touch says so first), every later call asks the clock once (under 0.5 s), and close_app, open_app and the
home button get out of it. Runner 16 times its own point lookup (N-F3, N-F4): "slow" and "fast" are judged by the
phone's own time there, rounds K-M's rules stay for runner 15."""
import asyncio
import time

import pytest
from test_banner_hit_203 import _looked
from test_daemon import finish
from test_round_k_203 import WITNESS, nothing, scaled, status_bar  # noqa: F401 (fixture)
from test_round_m_203 import APP_ELEMENT, _session
from test_touch_other_app_203 import SHOP, CHAT, PointSession, _real, quick_banner, refused, run, unread  # noqa: F401

from tapwright import session as session_module
from tapwright.device import ConnectionLost, DeviceError
from tapwright.session import Session

POINT = (320.0, 83.0)  # (N-F2's touch: in the top zone, below the status bar)
HELD_WORDS = ("Shop (com.example.shop) is in front and isn't answering, so nothing on the phone can be read. close_app "
              "can still close it; then open_app it again.")


class HeldSession(PointSession):
    """N-F2's phone on the fakes: the app's point, once asked, holds the runner `hold_for` seconds (every hit question in
    it "no element answered" after the runner's wait, the clock's too); the front route and the tree read wait for the
    runner as Session's do (channel_free) and are noted when they go (`runner_asks`); iOS's process control ends the
    app (which frees the runner) and launches one."""

    _given_up = _real("_given_up")
    held_answers = _real("held_answers")

    def __init__(self, hold_for: float) -> None:
        super().__init__(SHOP)
        self.tier, self.tree, self.runner_text = "root", [], []
        self.by_hands, self.nothing_takes = True, 0.03
        self.hold_for, self.held_until, self.app_asked, self.hold_set_at = hold_for, 0.0, 0, None
        self.runner_asks: list[tuple[str, float]] = []
        self.launched: list[tuple[str, object]] = []
        self.frees_on_launch: set[str] = set()
        self.hit = self._hit

        async def kill_app(bundle_id):
            self.device.kill_asks.append(bundle_id)
            if bundle_id == SHOP[1]:
                self.held_until = 0.0  # (the app that held it is gone)
            return 1
        self.device.kill_app = kill_app

        async def launch_app(bundle_id, arguments=None):
            self.launched.append((bundle_id, arguments))
            if bundle_id in self.frees_on_launch:
                self.held_until = 0.0
            self.app, self.front, self.in_front = ((CHAT if bundle_id == CHAT[1] else SHOP), (bundle_id, 9),
                                                   [bundle_id])
            return 9
        self.device.launch_app = launch_app

    def _hit(self, x, y):
        if time.monotonic() < self.held_until:
            return nothing()
        if (x, y) == WITNESS:
            return status_bar()
        if (x, y) == POINT and self.hold_for:
            self.app_asked += 1
            self.hold_set_at = time.monotonic()
            self.held_until = self.hold_set_at + self.nothing_takes + self.hold_for
            return nothing()
        return APP_ELEMENT

    async def front_app(self):
        await self.channel_free()
        self.runner_asks.append(("front", time.monotonic()))
        return await super().front_app()

    async def snapshot(self, keyboard=False, timeout=None):
        await self.channel_free()
        self.runner_asks.append(("tree", time.monotonic()))
        return await super().snapshot(keyboard, timeout)

    async def describe(self, *a, **k):
        await self.channel_free()
        self.runner_asks.append(("tree", time.monotonic()))
        return await super().describe(*a, **k)


# ---- Part 2: N-F2's sequence ----

def test_n_f2_a_hold_past_hit_held_answers_not_answering_and_never_costs_the_line(scaled):
    async def go():
        s = HeldSession(hold_for=9.0)  # (90 s on the phone, at a tenth)
        d, stop, task = await _looked(s)
        d.heartbeat = 3600.0  # (round O: the heartbeat's own clock question has its own test; the call's are counted)
        attempts, sid = d._attempts, s.sid
        began = time.monotonic()
        r = await d.run_tool("a", "tap", {"target": {"x": POINT[0], "y": POINT[1]}, "screenshot": False})
        took = time.monotonic() - began
        given_up = time.monotonic()
        # the tap went (by the hands, clear: the clock answered, the app's point didn't), once
        assert s.sent == [("tap", POINT)] and s.app_asked == 1
        # the call answers within HIT_HELD and a question, with the delivered touch first and the not-answering words
        assert took < session_module.HIT_HELD + 0.3, f"{took:.2f} s"
        assert r["text"].startswith("Tapped (320, 83).")
        assert f"couldn't look afterwards: {HELD_WORDS}" in r["text"]
        # no front or tree request went into the runner after the app's point held it, and the line was never taken
        # for lost
        assert [k for k, t in s.runner_asks if t > s.hold_set_at] == []
        assert d.state == "not-answering" and isinstance(d.stuck, session_module.RunnerHeld)
        assert d._attempts == attempts and s.sid == sid and not d._lost.is_set()
        assert d.status()["state"] == "not-answering"
        # the next look: one question at the clock, then not_answering at once (the runner's 0.3 s, as on the phone)
        s.nothing_takes = 0.3
        s.hit_questions.clear()
        began = time.monotonic()
        e = await refused(d, "look", {"screenshot": False})
        assert e.code == "not_answering" and str(e) == HELD_WORDS
        assert time.monotonic() - began < 0.5 and s.hit_questions == [WITNESS]
        assert [k for k, t in s.runner_asks if t > given_up] == []
        # close_app: through iOS's process control; the clock answers, the state ends, the look after it is normal
        s.nothing_takes = 0.03
        r = await d.run_tool("a", "close_app", {"app": "com.example.shop"})
        assert r["text"].startswith("Closed com.example.shop.") and "hasn't answered" not in r["text"]
        assert s.device.kill_asks == [SHOP[1]] and d.state == "live" and d.stuck is None and s.runner_held is None
        closed = time.monotonic()
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert r["text"].startswith("Screen ") and "App: Shop (com.example.shop)" in r["text"]
        assert "front" in [k for k, t in s.runner_asks if t > closed]  # (the runner asked as usual again)
        assert d._attempts == attempts and d.state == "live"
        await finish(stop, task)
    run(go)


def test_n_f1_a_hold_that_ends_before_hit_held_still_ends_as_before(scaled):
    async def go():
        s = HeldSession(hold_for=1.1)  # (11 s on the phone, at a tenth: within HIT_HELD)
        d, stop, task = await _looked(s)
        r = await d.run_tool("a", "tap", {"target": {"x": POINT[0], "y": POINT[1]}, "screenshot": False})
        assert s.sent == [("tap", POINT)] and r["text"].startswith("Tapped (320, 83).")
        assert "isn't answering" not in r["text"] and "couldn't look afterwards" not in r["text"]
        assert d.state == "live" and d.stuck is None and getattr(s, "runner_held", None) is None
        front_at = next(t for k, t in s.runner_asks if k == "front" and t > s.hold_set_at)
        assert front_at >= s.held_until  # (the read after the touch waited for the runner)
        await finish(stop, task)
    run(go)


async def _held(s: HeldSession):
    """A daemon in the state N-F2 now ends in: looked, tapped, the hold given up. (Round O) The heartbeat asks the clock
    once a beat in that state (RN-M3, its own tests below): here it beats once an hour, so a call's questions are its
    own."""
    d, stop, task = await _looked(s)
    d.heartbeat = 3600.0
    await d.run_tool("a", "tap", {"target": {"x": POINT[0], "y": POINT[1]}, "screenshot": False})
    assert isinstance(d.stuck, session_module.RunnerHeld)
    s.hit_questions.clear()
    return d, stop, task


def test_open_app_of_another_app_goes_by_ioss_process_control_and_the_clock_ends_the_state(scaled):
    async def go():
        s = HeldSession(hold_for=9.0)
        s.frees_on_launch = {CHAT[1]}
        d, stop, task = await _held(s)
        r = await d.run_tool("a", "open_app", {"app": "com.example.chat"})
        assert s.launched == [(CHAT[1], None)] and s.device.kill_asks == []  # (brought forward, nothing ended)
        assert r["text"].startswith("Opened") and "look to see it." in r["text"]
        assert d.state == "live" and d.stuck is None
        await finish(stop, task)
    run(go)


def test_open_app_of_the_held_app_ends_it_and_starts_it_again(scaled):
    async def go():
        s = HeldSession(hold_for=9.0)
        d, stop, task = await _held(s)
        r = await d.run_tool("a", "open_app", {"app": "com.example.shop"})
        assert s.device.kill_asks == [SHOP[1]] and s.launched == [(SHOP[1], None)]
        assert r["text"].startswith("Opened") and d.state == "live"
        await finish(stop, task)
    run(go)


def test_open_app_while_the_runner_stays_held_says_so_and_the_next_call_is_answered_at_once(scaled):
    async def go():
        s = HeldSession(hold_for=9.0)
        d, stop, task = await _held(s)
        r = await d.run_tool("a", "open_app", {"app": "com.example.chat"})  # (the runner stays held: N-F2's app runs)
        assert s.launched == [(CHAT[1], None)] and "hasn't answered since Shop held it" in r["text"]
        assert isinstance(d.stuck, session_module.RunnerHeld) and d.state == "not-answering"
        s.hit_questions.clear()
        began = time.monotonic()
        e = await refused(d, "tap", {"target": {"x": 200, "y": 500}, "screenshot": False})
        assert e.code == "not_answering" and time.monotonic() - began < 0.5 and s.hit_questions == [WITNESS]
        await finish(stop, task)
    run(go)


def test_the_home_button_works_while_held(scaled):
    async def go():
        s = HeldSession(hold_for=9.0)
        asked = []

        async def http(method, path, payload=None, timeout=30.0):
            asked.append((method, path))
            if path == "/wda/homescreen":
                s.held_until = 0.0  # (the app goes to the background and answers its request)
            return {"value": False}
        s.device.http = http
        d, stop, task = await _held(s)
        r = await d.run_tool("a", "press_button", {"name": "home"})
        assert ("POST", "/wda/homescreen") in asked and r["text"].startswith("Pressed home while Shop")
        assert d.state == "live" and d.stuck is None
        await finish(stop, task)
    run(go)


def test_a_real_answer_at_the_next_calls_question_ends_the_state_and_the_call_goes_on(scaled):
    async def go():
        s = HeldSession(hold_for=9.0)
        d, stop, task = await _held(s)
        s.held_until = 0.0  # (the app answered its request by itself)
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert "isn't answering" not in r["text"] and d.state == "live" and d.stuck is None
        assert s.hit_questions == [WITNESS]
        await finish(stop, task)
    run(go)


def test_a_real_drop_while_held_reconnects_as_any_drop(scaled):
    async def go():
        s = HeldSession(hold_for=9.0)
        d, stop, task = await _held(s)
        d.lost(ConnectionLost("WebDriverAgent didn't answer GET /status within 5 s"))
        assert d.stuck is None and s.runner_held is None and d.state == "reconnecting" and d._lost.is_set()
        await finish(stop, task)
    run(go)


def test_the_given_up_state_is_named_by_ioss_process_list_and_asks_the_runner_nothing(scaled):
    async def go():
        s = HeldSession(hold_for=9.0)
        s._held_until = time.monotonic() + session_module.HIT_HELD
        s.held_until = time.monotonic() + 60.0
        s.in_front = [CHAT[1]]
        with pytest.raises(session_module.RunnerHeld) as e:
            await s.front_app()
        assert (e.value.bundle_id, e.value.name) == (CHAT[1], CHAT[0]) and s.runner_asks == []
        asked = len(s.hit_questions)
        with pytest.raises(session_module.RunnerHeld):  # (again at once, nothing asked)
            await s.snapshot()
        assert len(s.hit_questions) == asked and s.runner_asks == []
        s.in_front = OSError("no process list")
        s.runner_held = None
        s._held_until = time.monotonic() + 0.05
        with pytest.raises(session_module.RunnerHeld) as e:
            await s.front_app()
        assert e.value.bundle_id == SHOP[1]  # (iOS's list couldn't say: the newest read names it)
    run(go)


# ---- Part 3: runner 16, the phone's own time ----

def _timed(ms: int, how: str = "client") -> DeviceError:
    return DeviceError(f"unsupported operation: {session_module.HIT_NOTHING} (hit {ms} ms, {how})")


def _sixteen(script, version=16):
    s = _session(script)
    s.tw_version = version
    return s


@pytest.mark.parametrize("link", [0.0, 0.4])
def test_16_a_busy_app_is_clear_by_the_hands_with_no_confirm_and_the_hold_set(link):
    async def go():
        s = _sixteen(lambda n, path, dev: (0.02 + link, status_bar()) if "x=66.00" in path
                     else (0.31 + link, _timed(304)))
        assert await s.banner_at((383.0, 168.0), SHOP[1], hands=True) == session_module.BANNER_CLEAR
        assert len(s.device.asked) == 2 and s._held()  # (the clock and the point, no confirm; a late clock is fine)
    run(go)


@pytest.mark.parametrize("link,want", [(0.0, session_module.BANNER_CLEAR), (0.4, session_module.BANNER_SLOW)])
def test_16_rn_h1_by_the_runner_a_free_runner_is_proven_by_the_clock_once_more(link, want):
    # (round O, review RN-H1) the runner's own touch goes only once the clock answers again: clear, and no hold.
    # (Round P, review RO-L1) only when that answer is prompt: one 0.42 s late on the link ends the hold but is slow
    async def go():
        s = _sixteen(lambda n, path, dev: (0.02 + link, status_bar()) if "x=66.00" in path
                     else (0.31 + link, _timed(304)))
        assert await s.banner_at((383.0, 168.0), SHOP[1], hands=False) == want
        assert [p for _, p in s.device.asked] == ["x=66.00&y=22.00", "x=383.00&y=168.00", "x=66.00&y=22.00"]
        assert not s._held()
    run(go)


@pytest.mark.parametrize("again", ["held", "silent"])
def test_16_rn_h1_by_the_runner_a_hold_that_persists_is_slow_and_by_the_hands_goes(again, monkeypatch):
    # (round O, the reviewer's probe: on 16 the point's slow "nothing" no longer clears the runner's path by itself)
    monkeypatch.setattr(session_module, "HIT_FREE_WAIT", 0.2)

    def script(n, path, dev):
        if time.monotonic() < dev.held_until:
            return (0.03, _timed(301)) if again == "held" else (0.4, status_bar())  # (silent: past HIT_FREE_WAIT)
        if "x=66.00" in path:
            return 0.0, status_bar()
        dev.held_until = time.monotonic() + 0.03 + 5.0  # (the app's request stays: N-F4 not holding)
        return 0.03, _timed(304)

    async def go():
        s = _sixteen(script)
        assert await s.banner_at((383.0, 168.0), SHOP[1], hands=False) == session_module.BANNER_SLOW
        assert len(s.device.asked) == 3 and s._held()
        s2 = _sixteen(script)
        assert await s2.banner_at((383.0, 168.0), SHOP[1], hands=True) == session_module.BANNER_CLEAR
        assert len(s2.device.asked) == 2 and s2._held()
    run(go)


def test_16_rn_h1_a_banner_at_the_clock_when_asked_again_is_over():
    async def go():
        from test_round_i_203 import hit_of
        s = _sixteen(lambda n, path, dev: (0.0, status_bar() if n == 1 else hit_of("com.apple.springboard",
                                                                                     [8, 11, 414, 90]))
                     if "x=66.00" in path else (0.31, _timed(304)))
        assert await s.banner_at((383.0, 168.0), SHOP[1], hands=False) == session_module.BANNER_OVER
    run(go)


@pytest.mark.parametrize("link", [0.0, 0.26, 0.6])
def test_16_a_fast_nothing_by_the_phones_time_is_a_banner_in_motion_however_late_on_the_link(link):
    # (RL-H2 on runner 16: the same late answers that runner 15 reads as clear are judged right)
    async def go():
        def script(n, path, dev):
            if n == 1:
                return 0.0, status_bar()
            return link, _timed(12)
        s = _sixteen(script)
        assert await s.banner_at((215.0, 80.0), SHOP[1], hands=True) == session_module.BANNER_OVER
        assert len(s.device.asked) == 2 and not s._held()
    run(go)


def test_rl_h2_on_runner_15_is_still_the_pinned_limit():
    async def go():
        def script(n, path, dev):
            if n == 1:
                return 0.0, status_bar()
            return 0.26, _timed(12)  # (runner 15 sends no time: the words are only read on 16)
        s = _sixteen(script, version=15)
        assert await s.banner_at((215.0, 80.0), SHOP[1], hands=True) == session_module.BANNER_CLEAR
    run(go)


@pytest.mark.parametrize("ms,want,held", [(304, session_module.BANNER_SLOW, True),
                                          (15, session_module.BANNER_OVER, False)])
@pytest.mark.parametrize("link", [0.0, 0.4])
def test_16_the_clocks_nothing_is_judged_by_the_phones_time(ms, want, held, link):
    async def go():
        s = _sixteen(lambda n, path, dev: (link, _timed(ms)))
        assert await s.banner_at((383.0, 168.0), SHOP[1], hands=True) == want
        assert len(s.device.asked) == 1 and s._held() is held
    run(go)


def test_16_a_slow_nothing_with_no_witness_is_slow_with_the_hold():
    async def go():
        s = _sixteen(lambda n, path, dev: (0.0, APP_ELEMENT) if "x=66.00" in path else (0.0, _timed(305)))
        assert await s.banner_at((383.0, 168.0), SHOP[1], hands=True) == session_module.BANNER_SLOW
        assert len(s.device.asked) == 2 and s._held()
    run(go)


@pytest.mark.parametrize("words", ["daemon", "none"])
def test_16_asked_the_daemons_way_or_with_no_time_keeps_runner_15s_rules(words):
    async def go():
        said = _timed(304, "daemon") if words == "daemon" else DeviceError(
            f"unsupported operation: {session_module.HIT_NOTHING}")
        s = _sixteen(lambda n, path, dev: (0.0, status_bar()) if "x=66.00" in path and n == 1 else (0.31, said))
        assert await s.banner_at((383.0, 168.0), SHOP[1], hands=False) == session_module.BANNER_SLOW  # (runner path)
        assert len(s.device.asked) == 2 and s._held()
        s2 = _sixteen(lambda n, path, dev: (0.0, status_bar()) if "x=66.00" in path and n == 1 else (0.31, said))
        assert await s2.banner_at((383.0, 168.0), SHOP[1], hands=True) == session_module.BANNER_CLEAR
        assert len(s2.device.asked) == 3  # (the confirm, as on runner 15)
    run(go)


@pytest.mark.parametrize("ms,link,held", [(12, 0.4, False), (304, 0.0, True)])
def test_16_hit_at_sets_the_hold_by_the_phones_time(ms, link, held):
    async def go():
        s = _sixteen(lambda n, path, dev: (link, _timed(ms)))

        async def routes(strict=False):
            return None
        s._fast_routes = routes
        with pytest.raises(Exception):
            await s.hit_at(215.0, 600.0)
        assert s._held() is held
    run(go)


def test_16_the_hold_is_not_assumed_away_the_front_route_waits_for_the_clock(scaled):
    async def go():
        def script(n, path, dev):
            if time.monotonic() < dev.held_until:
                return 0.03, _timed(301)
            if "x=66.00" in path:
                return 0.0, status_bar()
            dev.held_until = time.monotonic() + 0.3
            return 0.03, _timed(304)
        s = _sixteen(script)
        # (round O, RN-H1: the runner's path asks the clock once more, which the held runner doesn't answer: slow)
        assert await s.banner_at((383.0, 168.0), SHOP[1], hands=False) == session_module.BANNER_SLOW
        assert s._held()
        await s.front_app()
        assert not s.device.front_into_held  # (the clock was asked until it answered)
    run(go)


# ---- (round Q) runner 15 again: neither doctor nor the status says anything of an "older" runner ----

@pytest.mark.parametrize("version", [15, 0])
def test_the_status_names_the_runners_version_and_no_note(version):
    from test_daemon import make
    d = make()
    d.session.tw_version = version
    status = d.status()
    assert status.get("runner") == (version or None) and "runner_note" not in status
    assert not hasattr(session_module, "runner_note") and not hasattr(session_module, "RUNNER_OLD")


def test_the_default_runner_is_15_again():
    from tapwright import store
    assert store.WDA_SOURCES["fork"] == ("bo5amis/WebDriverAgent", "tw-16.15.3.15",
                                         "bcbc21c4b87b931bcc1de1641527f2a57b0b377e1fcdac4a82d0e969eb1e1665",
                                         "fork-tw-16.15.3.15")
    assert store.WDA_DEFAULT == "fork" and not hasattr(store, "WDA_16_SHA256")


def test_doctor_says_nothing_of_an_older_runner_on_runner_15(monkeypatch, capsys, tmp_path):
    from types import SimpleNamespace

    from pymobiledevice3 import usbmux

    from tapwright import cli, repair, signing, store

    async def no_devices():
        return []

    async def nobody(usb):
        return []
    for name in ("report_checkout", "report_schedules"):
        monkeypatch.setattr(cli, name, lambda *a: 0)
    monkeypatch.setattr(cli, "report_accounts", lambda *a: 0)
    monkeypatch.setattr(cli, "report_grants", lambda *a: 0)
    monkeypatch.setattr(cli, "needs_person", nobody)
    monkeypatch.setattr(usbmux, "list_devices", no_devices)
    monkeypatch.setattr(signing, "PROFILE_FILE", tmp_path / "none.mobileprovision")
    monkeypatch.setattr(repair, "profile_expiry", lambda: None)
    monkeypatch.setattr(repair, "certificate_expiry", lambda: None)
    monkeypatch.setattr(store, "load_state", lambda: {"runner_id": "r", "udid": "u", "wda_build": "fork-tw-16.15.3.15",
                                                      "pairing_port": 1})
    asyncio.run(cli.doctor(SimpleNamespace(fix=False, unattended=False)))
    out = capsys.readouterr().out
    assert "WebDriverAgent set up for u" in out
    assert "older than 16" not in out and "route version" not in out


# ---- Part 4: another app's sheet over the screen (N-F5) ----

SHEET = "com.apple.ios.StoreKitUIService"


def test_n_f5_a_tap_under_another_apps_sheet_is_refused_with_what_it_is():
    from test_round_i_203 import hit_of
    from test_round_k_203 import answers

    async def go():
        s = unread()
        d, stop, task = await _looked(s)
        answers(s, hit_of(SHEET, [0, 0, 430, 932]), hit_of(SHEET, [0, 0, 430, 932]))
        e = await refused(d, "tap", {"target": {"x": 200, "y": 85}, "look": False})
        assert e.code == "not_actionable"
        assert str(e) == "covered: another app's sheet (com.apple.ios.StoreKitUIService) is over the screen; look again."
        assert s.sent == []
        await finish(stop, task)
    run(go)


def test_another_process_not_over_the_whole_screen_keeps_the_words_it_had():
    from test_round_i_203 import hit_of
    from test_round_k_203 import answers

    async def go():
        s = unread()
        d, stop, task = await _looked(s)
        answers(s, status_bar(), hit_of("com.apple.GameOverlayUI", [81, 79, 268, 56], "XCUIElementTypeButton"))
        e = await refused(d, "tap", {"target": {"x": 200, "y": 85}, "look": False})
        assert str(e) == session_module.COVERED_UNSURE and s.sent == []
        await finish(stop, task)
    run(go)


def test_the_sheet_is_found_by_the_check_and_forgotten_by_the_next():
    from test_round_i_203 import hit_of
    from test_round_k_203 import answers

    async def go():
        s = unread()
        answers(s, hit_of(SHEET, [0, 0, 430, 932]), hit_of(SHEET, [0, 0, 430, 932]))
        assert await s.banner_at((200.0, 85.0), SHOP[1]) == session_module.BANNER_UNSURE and s.sheet_over == SHEET
        answers(s, status_bar(), APP_ELEMENT)
        assert await s.banner_at((200.0, 85.0), SHOP[1]) == session_module.BANNER_CLEAR and s.sheet_over is None
    run(go)


def test_n_f5_a_touch_by_the_pictures_text_says_the_sheet_its_read_found(monkeypatch):
    from test_daemon import make, started
    from test_text_by_picture_200 import whole_session

    from tapwright import ocr

    async def go():
        s = whole_session([ocr.Seen("Not Now", (150.0, 600.0, 80.0, 20.0), 1.0)])
        d = make(s)
        stop, task = await started(d)

        async def banner_shown():
            s.sheet_over = SHEET  # (the read's hit question found the sheet; no banner)
            return False
        monkeypatch.setattr(d, "_banner_shown", banner_shown)
        r = await d.run_tool("a", "tap", {"target": {"text": "Not Now"}, "screenshot": False})
        assert r["text"].startswith("Tapped (")
        assert session_module.SHEET_NOTE.format(bundle=SHEET) in r["text"].split("\n")
        await finish(stop, task)
    run(go)

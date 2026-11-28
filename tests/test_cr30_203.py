"""Tapwright 2.0.3: CodeRabbit's findings on PR #30 (cycle 2), one test (or more) per fix."""
import asyncio

from fakes import button
from test_banner_hit_203 import _looked
from test_daemon import finish
from test_round_h_203 import other
from test_touch_other_app_203 import refused, run, unread

from test_slow_readable_203 import state  # noqa: F401 (fixture)

from tapwright import session as session_module

SHEET = "com.apple.ios.StoreKitUIService"


# ---- 4727955298: an element touch under another app's whole-screen sheet ----

def test_an_element_touch_under_another_apps_whole_screen_sheet_is_refused():
    async def go():
        s = unread()
        s.tree, s.tier = [button("e1", "Menu", rect=(20, 70, 60, 30))], "full"
        d, stop, task = await _looked(s)
        s.hit = lambda x, y: other(SHEET, (0, 0, 430, 932), "XCUIElementTypeOther")
        e = await refused(d, "tap", {"target": {"ref": "e1"}, "look": False})
        assert e.code == "not_actionable"
        assert str(e) == session_module.COVERED_SHEET.format(bundle=SHEET)
        assert s.sent == []
        await finish(stop, task)
    run(go)


# ---- 4727955261 (AGENTS.md), and the review body's SKILL.md:26 ----

def test_the_guides_send_a_sheet_to_a_new_look_and_say_which_runner_checks():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    texts = [" ".join((root / p).read_text("utf-8").split())
             for p in ("AGENTS.md", "skills/tapwright/SKILL.md", "tapwright/skill/SKILL.md")]
    for text in texts:
        assert "(aim at an element of your last look, or look again)" not in text
        assert "another app's sheet over the screen needs a new look" in text
        assert "route version 11 or later" in text
        assert "look again before aiming at a system sheet or alert that came up after your last look" in text


# ---- 4727955295: an element of the system over the app, once the system has gone ----

def test_an_element_of_the_system_over_the_app_is_stale_once_the_system_has_gone():
    from test_daemon import make, started
    from test_touch_other_app_203 import SHOP, banner_down

    from tapwright import ui

    async def go():
        s = unread()
        s.tree, s.tier = [button("e1", "Allow", rect=(100, 400, 200, 44))], "full"
        banner_down(s)  # a permission prompt over the app: SpringBoard's, the app under it
        s.last_alert = ui.Alert("Allow Shop to use your location?", ("Allow", "Don't Allow"), (40, 300, 320, 200))
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        s.front, s.in_front, s.last_alert = (SHOP[1], 7), [SHOP[1]], None  # the prompt went; Shop alone
        e = await refused(d, "tap", {"target": {"label": "Allow"}, "look": False, "force": True})
        assert e.code == "stale" and str(e).endswith("Nothing was touched; look again.")
        assert s.sent == []
        await d.run_tool("a", "tap", {"target": {"x": 200, "y": 600}, "look": False})  # a point on Shop alone: goes
        assert s.sent == [("tap", (200, 600))]
        await finish(stop, task)
    run(go)


# ---- 4727955265: a frame stamped ahead of the hub's clock (the wall clock stepped back) ----

def test_a_frame_stamped_ahead_of_the_clock_is_of_unknown_age_and_private_while_an_unlock_is_kept():
    from fakes import DescribingSession
    from test_daemon import make

    async def go():
        d = make(DescribingSession())
        mono, wall = [1000.0], [5_000_000.0]
        d.hub._mono, d.hub._clock = (lambda: mono[0]), (lambda: wall[0])
        span = d._unlock_begin()
        during = d.hub.publish(b"\xff\xd8keys")  # (a picture of the passcode keys)
        for _ in range(80):  # (it drops out of the hub's kept history)
            d.hub.publish(b"\xff\xd8more")
        d._unlock_end(span)
        mono[0] += 30.0
        wall[0] -= 3600.0  # (the wall clock stepped back an hour: the frame's stamp is ahead of it)
        assert d.hub.arrival_of(during.seq) is None
        assert d.frame_private(during)
    asyncio.run(go())


# ---- 4727955291: a fast touch the runner refused unplayed is checked again before its W3C fallback ----

import pytest  # noqa: E402

from tapwright import ui as ui_module  # noqa: E402
from tapwright.device import DeviceError  # noqa: E402
from tapwright.session import Refused, Session  # noqa: E402


def _gated(runner, monkeypatch):
    from test_session import FastRunner  # noqa: F401
    monkeypatch.setattr(session_module.store, "load_state", lambda: {"runner_id": "runner"})
    monkeypatch.setattr(session_module, "FAST_TAP", "1")
    monkeypatch.setattr(session_module, "FAST_DRAG", "1")
    s = Session(runner)
    checks: list[tuple[float, float]] = []

    async def front_rules(at, wait=True):
        checks.append(at)
        if len(checks) > 1:  # another app came to the front after the first check
            raise Refused("stale", "another app is in front now; look again.")
    s._front_rules = front_rules
    s.touch_rules = session_module.TouchRules(session_module.Front(session_module.FRONT_APP, "com.example.shop"))
    return s, checks


@pytest.mark.parametrize("gesture", ["tap", "scroll"])
def test_a_fast_touch_refused_unplayed_is_checked_again_before_the_w3c_fallback(monkeypatch, gesture):
    from test_session import FastRunner, _paths
    runner = FastRunner(version=2)
    runner.tap_error = DeviceError("WebDriverAgent: invalid argument: x and y must be finite numbers (screen points)")
    runner.drag_error = DeviceError("WebDriverAgent: invalid argument: point times must be in order, 0-60000 ms")
    s, checks = _gated(runner, monkeypatch)
    with pytest.raises(Refused) as e:
        if gesture == "tap":
            asyncio.run(s.tap(ui_module.Target(x=30, y=400)))
        else:
            asyncio.run(s.scroll(215, 700, 215, 300))
    assert e.value.code == "stale" and len(checks) == 2
    assert "/actions" not in _paths(runner)  # (nothing went out by the fallback)


def test_a_fast_touch_refused_unplayed_goes_by_w3c_once_checked_again(monkeypatch):
    from test_session import FastRunner, _paths
    runner = FastRunner()
    runner.tap_error = DeviceError("WebDriverAgent: invalid argument: x and y must be finite numbers (screen points)")
    s, checks = _gated(runner, monkeypatch)

    async def fine(at, wait=True):
        checks.append(at)
    s._front_rules = fine
    asyncio.run(s.tap(ui_module.Target(x=30, y=400)))
    assert checks == [(30.0, 400.0), (30.0, 400.0)] and _paths(runner)[-1] == "/actions"
    assert s._gate_at is None


# ---- review body, session.py:1249: reset retires the old runner's hold ----

def test_reset_forgets_the_old_runners_hold_and_a_new_slow_hit_sets_one_again(monkeypatch):
    from test_session import Recorder
    monkeypatch.setattr(session_module.store, "load_state", lambda: {"runner_id": "runner"})
    s = Session(Recorder())
    s._hold("point", (200.0, 85.0))
    assert s._held()
    asyncio.run(s.reset())
    assert not s._held()
    s._hold("point", (200.0, 85.0))
    assert s._held()


# ---- review body, session.py:2646: a touch's deadline is checked again once the hit lock is held ----

def test_a_touchs_deadline_that_passed_while_the_hit_lock_was_busy_asks_nothing():
    import time

    from test_round_m_203 import _session

    def script(n, path, dev):
        return 0.0, {"t": "XCUIElementTypeStatusBar", "r": [0, 0, 430, 54], "b": "com.apple.springboard",
                     "identity": "verified"}

    async def go():
        s = _session(script)
        s._hold("point", (200.0, 85.0))
        s._hit_lock = asyncio.Lock()
        await s._hit_lock.acquire()  # (another touch's hit question holds the lock past this touch's deadline)
        until = time.monotonic() + 0.1
        later = asyncio.get_running_loop().call_later(0.3, s._hit_lock.release)
        free = await s.channel_free(until)
        later.cancel()
        assert free is False
        assert not [p for _, p in s.device.asked if p.startswith("x=")]  # (no clock question after the deadline)
        assert s._held()
    asyncio.run(go())


# ---- 4727955286: type_text's own read for its field is a real read with the normal budget ----

def test_type_texts_field_read_is_a_real_read_with_the_normal_budget_on_a_moving_screen(state):
    from test_long_block_screen_203 import _on, _shop
    from test_slow_readable_203 import ShopRunner, Stream, look

    async def go():
        stream = Stream()
        s, runner = _shop(stream, ShopRunner(stream, answers={}))
        _on(stream, b"a page")
        await look(s, stream)
        stream.now[0] += 20.0
        stream.after = b"next video"
        await look(s, stream, moving=True)
        assert runner.budgets() == [3000, 8000, 1500]  # (an ordinary look there: the short budget)
        token = session_module.NO_LONG.set(True)
        try:
            await look(s, stream, moving=True)  # (at once: an ordinary look could be left unasked here)
        finally:
            session_module.NO_LONG.reset(token)
        assert runner.budgets() == [3000, 8000, 1500, 3000]  # (asked, with the normal budget)
        assert runner.paths("/wda/tw/front") == []
    run(go)


# ---- 4727955289: a cut read that names another app gets that app's long try ----

def test_a_short_read_that_names_another_app_gives_that_app_its_long_try(state):
    from test_slow_readable_203 import CUT, ITEM, OTHER, SHOP, WHOLE, ShopRunner, Stream, look, shop
    from test_vision_200 import _app

    async def go():
        stream = Stream()
        s, runner = shop(stream, ShopRunner(stream, answers={}))
        await look(s, stream)  # Shop: cut, its long try cut: Shop blocked
        assert runner.budgets() == [3000, 8000] and s.budgets.blocked(SHOP)
        runner.answers = {1500: {**CUT, "tree": _app(OTHER, "Other")}, 3000: {**CUT, "tree": _app(OTHER, "Other")},
                          8000: {**WHOLE, "tree": _app(OTHER, "Other", [ITEM])}}
        s.screen_still = lambda: False  # (Shop's plan: the short budget)

        async def mark(check):
            return b"still", False

        async def across(mark):
            return True
        s.still_mark, s.still_across = mark, across
        stream.now[0] += 20.0
        tree = await look(s, stream)
        assert runner.budgets()[2:] == [1500, 3000, 8000]  # (Other's own long try, though Shop is blocked)
        assert tree.tier == "full"
    run(go)


# ---- 4727955275: an open_url step's expect that held records the app it found ----

def test_an_open_url_steps_expect_records_the_app_its_link_brought_forward():
    from test_daemon import make, started
    from test_touch_other_app_203 import CHAT

    async def go():
        s = unread()
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})

        async def open_url(url):  # (the link's app comes to the front a moment later, during the expect's reads)
            return "opened " + url
        s.open_url = open_url
        real = s.refresh

        async def refresh(timeout=None):
            s.app, s.front, s.in_front = CHAT, (CHAT[1], 9), [CHAT[1]]
            s.tree, s.tier = [button("e9", "Message", "message-field", rect=(20, 700, 360, 40))], "full"
            return await real(timeout)
        s.refresh = refresh
        await d.run_tool("a", "run", {"steps": [
            {"tool": "open_url", "url": "chat://thread/1", "expect": {"id": "message-field"}},
            {"tool": "tap", "target": {"x": 200, "y": 400}}]})
        assert d.control.client("a").seen_front == session_module.Front(session_module.FRONT_APP, CHAT[1])
        assert s.sent == [("tap", (200, 400))]
        await finish(stop, task)
    run(go)


# ---- 4727955277: the app credited by an expect is the one whose read found the element ----

def test_an_expects_picture_check_of_another_app_credits_no_app():
    from types import SimpleNamespace

    from test_daemon import make, started
    from test_touch_other_app_203 import CHAT, SHOP

    async def go():
        s = unread()
        s.tree, s.tier, s.tree_app = [button("e9", "Message", "message-field", rect=(20, 700, 360, 40))], "full", SHOP
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})

        async def compare(view, name, **kw):  # (its read: another app came forward meanwhile, and the picture matches)
            s.app, s.front, s.in_front, s.read_app = CHAT, (CHAT[1], 9), [CHAT[1]], CHAT
            return SimpleNamespace(reason=None, notes=[], named=False, lines=[""], folder=None, picture=None)
        d._compare = compare
        e = await refused(d, "run", {"steps": [
            {"tool": "tap", "target": {"x": 200, "y": 300}, "expect": {"id": "message-field", "matches": "thread"}},
            {"tool": "tap", "target": {"x": 200, "y": 400}}]})
        assert "stopped at step 2" in str(e) and str(e).count(session_module.UNSEEN_FRONT.split(";")[0]) == 1
        assert s.sent == [("tap", (200, 300))]
        await finish(stop, task)
    run(go)


# ---- 4727955280: open_app's record of the front is the app it opened, or unknown ----

def test_open_app_records_unknown_when_another_app_came_forward_before_its_record():
    from test_daemon import make, started
    from test_touch_other_app_203 import CHAT, SHOP

    async def go():
        s = unread()
        s.app_names.update({SHOP[1]: SHOP[0], CHAT[1]: CHAT[0]})
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        real = d.control.agent

        async def agent(act):
            result = await real(act)
            s.app, s.front, s.in_front = SHOP, (SHOP[1], 7), [SHOP[1]]  # (another agent's queued call, in between)
            return result
        d.control.agent = agent
        await d.run_tool("a", "open_app", {"app": CHAT[1], "look": False})
        assert d.control.client("a").seen_front == session_module.Front(session_module.FRONT_UNKNOWN)
        d.control.agent = real
        e = await refused(d, "tap", {"target": {"x": 200, "y": 400}, "look": False})
        assert e.code == "not_actionable" and str(e) == session_module.UNSEEN_FRONT
        await finish(stop, task)
    run(go)


def test_open_app_records_the_app_it_opened():
    from test_daemon import make, started
    from test_touch_other_app_203 import CHAT, SHOP

    async def go():
        s = unread()
        s.app_names.update({SHOP[1]: SHOP[0], CHAT[1]: CHAT[0]})
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "open_app", {"app": CHAT[1], "look": False})
        assert d.control.client("a").seen_front == session_module.Front(session_module.FRONT_APP, CHAT[1])
        await finish(stop, task)
    run(go)


# ---- 4727955282: back's stillness check looks at a changed frame that came before it started ----

def test_a_changed_frame_that_came_during_backs_read_is_moved_not_late(monkeypatch):
    from fakes import DescribingSession
    from test_daemon import make

    from tapwright import daemon as daemon_module

    async def go():
        monkeypatch.setattr(daemon_module, "BACK_STILL_PAUSE", 0.05)
        d = make(DescribingSession())
        before = d.hub.publish(b"\xff\xd8sheet low")
        d.hub.publish(b"\xff\xd8sheet higher")  # (the sheet slid during the read; no frame after it in time)
        assert await d._still_across_read(before) == "moved"
        again = d.hub.publish(b"\xff\xd8still")
        d.hub.publish(b"\xff\xd8still")  # (an unchanged frame and nothing after it: late, as before)
        assert await d._still_across_read(again) == "late"
    asyncio.run(go())


# ---- review body, daemon.py:10514: frames that can't be compared don't release a failed screen's block ----

def _jpeg(size, shade):
    import io

    from PIL import Image
    out = io.BytesIO()
    Image.new("L", size, shade).save(out, "JPEG")
    return out.getvalue()


def test_pictures_of_two_sizes_cant_tell_another_screen_and_the_block_holds():
    from test_slow_readable_203 import Stream

    from tapwright.daemon import StillMark

    stream = Stream()
    d = stream.d
    d.hub.publish(_jpeg((430, 932), 200))
    kept = StillMark(d.hub.latest)
    d.hub.publish(_jpeg((932, 430), 200))  # (the same screen, turned: another size, no comparison)
    assert d._other_screen(kept) is None
    d.hub.publish(_jpeg((430, 932), 20))  # (a real comparison that differs all over: another screen)
    assert d._other_screen(kept) is True


# ---- review body, daemon.py:4603: the late o-ref check judges the text the first check judged ----

def test_the_late_o_ref_check_gets_the_text_of_the_first_check(monkeypatch):
    from test_round_p_203 import ScrollSession, _o_looked

    async def go():
        s = ScrollSession()
        d, stop, task = await _o_looked(s)
        real_still, real_late = d._o_ref_still, d._o_ref_late
        judged: list[str] = []

        async def still(client_id, name, ref, args):
            out = await real_still(client_id, name, ref, args)
            d.control.client(client_id).ocr_text[ref] = "Cancel"  # (another look of this client, meanwhile)
            return out

        def late(ref, text, checked, found):
            judged.append(text)
            return real_late(ref, text, checked, found)
        d._o_ref_still, d._o_ref_late = still, late
        await d.run_tool("a", "scroll", {"target": {"ref": "o1"}, "direction": "down", "look": False})
        assert judged == ["Search"]
        await finish(stop, task)
    run(go)

"""Tapwright 2.0.3: CodeRabbit's findings on PR #30 (cycle 2), one test (or more) per fix."""
import asyncio

from fakes import button
from test_banner_hit_203 import _looked
from test_daemon import finish
from test_round_h_203 import other
from test_touch_other_app_203 import refused, run, unread

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

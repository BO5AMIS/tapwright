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

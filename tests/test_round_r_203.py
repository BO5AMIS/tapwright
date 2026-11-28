"""Tapwright 2.0.3, round R (phone 2026-11-28, R-F1): after `open_app` with `look: false`, a point tap near the top went
rightly to the opened app, then the service took it for a theft and opened the app before again ("Went back to ..."),
because the app the touch was meant for came from the last READ, which that open_app doesn't make. Now it is the app the
agent's last answer showed, as rule B knows it (`view.seen_front`)."""
import pytest
from test_banner_hit_203 import _looked
from test_daemon import finish
from test_touch_other_app_203 import CHAT, SHOP, PointSession, quick_banner, run  # noqa: F401 (fixture)

NOTES = ("Notes", "com.example.notes")
TOP = (200.0, 85.0)


class OpeningSession(PointSession):
    """A touch at a point in `opens` brings that app to the front, the activeAppInfo answer included."""

    def __init__(self) -> None:
        super().__init__(SHOP)
        self.tier, self.tree, self.runner_text = "root", [], []
        self.installed = [{"bundle_id": b, "name": n, "version": "1.0", "build": "1", "executable": n}
                          for n, b in (SHOP, CHAT, NOTES)]  # (the installed apps' names, as the phone lists them)
        self.app_names = {b: n for n, b in (SHOP, CHAT, NOTES)}

    async def _goes(self, kind, ms, at):
        await super()._goes(kind, ms, at)
        self.foreground = self.app[1]


@pytest.mark.parametrize("force", [False, True])
def test_r_f1_a_tap_near_the_top_after_open_app_look_false_stays_in_the_opened_app(force):
    async def go():
        s = OpeningSession()
        d, stop, task = await _looked(s)  # (a read in app A)
        r = await d.run_tool("a", "open_app", {"app": CHAT[1], "look": False})
        assert r["text"].startswith("Opened")
        args = {"target": {"x": TOP[0], "y": TOP[1]}, "look": False, **({"force": True} if force else {})}
        r = await d.run_tool("a", "tap", args)
        assert r["text"].startswith("Tapped (200, 85).") and "came to the front" not in r["text"]
        assert s.launched == [CHAT[1]] and s.front[0] == CHAT[1]  # (no launch of A: B is still in front)
        r = await d.run_tool("a", "look", {"screenshot": False})  # (the next call's check doesn't reopen A either)
        assert "came to the front" not in r["text"] and s.launched == [CHAT[1]]
        await finish(stop, task)
    run(go)


def test_r_f1_a_real_theft_after_open_app_look_false_still_goes_back_to_the_opened_app():
    async def go():
        s = OpeningSession()
        d, stop, task = await _looked(s)
        await d.run_tool("a", "open_app", {"app": CHAT[1], "look": False})
        s.opens[TOP] = NOTES  # (a banner over the top took the touch: another app came to the front)
        r = await d.run_tool("a", "tap", {"target": {"x": TOP[0], "y": TOP[1]}, "look": False})
        assert r["text"].startswith("Tapped (200, 85).")
        assert "Notes (com.example.notes) came to the front after the touch" in r["text"]
        assert "Went back to Chat (com.example.chat)." in r["text"]
        assert s.launched == [CHAT[1], CHAT[1]]  # (B opened again, never A)
        await finish(stop, task)
    run(go)


def test_with_nothing_recorded_and_an_open_since_the_read_nothing_is_compared():
    # (a runner older than route version 11 records no front: the read's app is used only when the agent's last answer
    # was that read)
    async def go():
        s = OpeningSession()
        d, stop, task = await _looked(s)
        view = d.control.client("a")
        view.seen_front = None
        assert d._meant_app(view) == SHOP
        view.opened = True
        assert d._meant_app(view) is None
        view.opened, view.unlooked = False, 1
        assert d._meant_app(view) is None
        await finish(stop, task)
    run(go)

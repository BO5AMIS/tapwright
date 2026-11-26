"""Tapwright 2.0.3, safety: a touch must not land in another app. On the owner's phone (2026-11-26) an agent aimed two
point taps at a screen whose elements weren't read. A notification banner was over the top of the screen: the first tap
landed on it and opened another app on a chat with a person, the second landed on that chat's call button and started
a call. Nothing stopped either: the banner wait held only touches aimed at an element, and a touch was refused for
another app in front only when it was typing.

Two rules now, both before every touch of an agent, on one question to the runner's front route (a few ms):
A. A touch that starts in the top BANNER_ZONE waits a banner out and is refused `covered` if it stays, whatever it is
   aimed by (an element, a point, an `o` ref, a text in the picture) and whatever the gesture.
B. A touch goes only to an app the agent's last look or answer showed it: another app in front is refused `stale`.

The fake session's touches go through the real Session._touching with their point: the banner wait and the check."""
import asyncio

import pytest

from fakes import DescribingSession, button
from test_daemon import finish, make, started

from tapwright import session as session_module
from tapwright import ui
from tapwright.control import Refused
from tapwright.session import SPRINGBOARD, Session

SHOP = ("Shop", "com.example.shop")
CHAT = ("Chat", "com.example.chat")


@pytest.fixture(autouse=True)
def quick_banner(monkeypatch):
    monkeypatch.setattr(session_module, "BANNER_WAIT", 0.04)
    monkeypatch.setattr(session_module, "BANNER_POLL", 0.01)
    from tapwright import ocr
    monkeypatch.setattr(ocr, "reader", None)
    monkeypatch.setattr(ocr, "available", lambda: False)


class PointSession(DescribingSession):
    """A fake session whose touches go through the real Session._touching with their point. `front`: what the runner's
    front route answers ((bundle id, pid) or None: it can't say); `in_front`: iOS's process list; `opens`: a tap at
    that point brings that app to the front (its screen: no elements)."""

    _touching = Session._touching
    _clear_of_banner = Session._clear_of_banner
    _seen_check = getattr(Session, "_seen_check", None)  # (getattr: so this file runs against 91c9e74, before it)

    def __init__(self, app=SHOP) -> None:
        super().__init__()
        self.tw_version = 11
        self.app = app
        self.front: tuple[str, int | None] | None = (app[1], 7)
        self.in_front = [app[1]]
        self.front_questions = 0
        self.sent: list[tuple[str, tuple[float, float]]] = []
        self.opens: dict[tuple[float, float], tuple[str, str]] = {}
        self.input_point, self.top_touch, self.seen_app = None, False, None
        self.app_names = {SHOP[1]: SHOP[0], CHAT[1]: CHAT[0]}

        async def foreground_apps():
            return list(self.in_front)
        self.device.foreground_apps = foreground_apps

    async def front_app(self):
        self.front_questions += 1
        return self.front

    def _went(self, kind: str, at: tuple[float, float]) -> None:
        self.sent.append((kind, at))
        if at in self.opens:
            self.app = self.opens[at]
            self.front, self.in_front, self.tree = (self.app[1], 9), [self.app[1]], []

    async def tap(self, target, hold_ms=60):
        hit = ui.resolve(self.items, target)
        at = hit if isinstance(hit, tuple) else hit.center
        await self._touching(hold_ms, at)
        self._record("tap")
        self._went("tap", at)
        return f"Tapped ({round(at[0])}, {round(at[1])})."

    async def launch(self, bundle_id, arguments=None):
        out = await super().launch(bundle_id, arguments)
        self.front, self.in_front = (bundle_id, 9), [bundle_id]
        return out

    async def swipe(self, x0, y0, x1, y1, duration_ms=350, hold_ms=0):
        await self._touching(duration_ms + hold_ms, (x0, y0))
        self._record("swipe")
        self._went("swipe", (x0, y0))
        return f"swiped ({round(x0)}, {round(y0)}) -> ({round(x1)}, {round(y1)})"


def unread(app=SHOP) -> PointSession:
    """A screen whose elements weren't read (the incident's): the agent aims by point."""
    s = PointSession(app)
    s.tier, s.tree = "root", []
    s.runner_text = []
    return s


def run(go):
    return asyncio.run(go())


async def refused(d, name, args) -> Refused:
    with pytest.raises(Refused) as e:
        await d.run_tool("a", name, args)
    return e.value


# ---- the incident ----

def test_the_incident_two_point_taps_on_an_unread_screen_with_a_banner_then_another_app():
    async def go():
        s = unread()
        d = make(s)
        stop, task = await started(d)
        s.app_names.update({SHOP[1]: SHOP[0], CHAT[1]: CHAT[0]})  # (the installed apps' names, as the session reads them)
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert ui.ELEMENTS_NONE in r["text"]
        # a banner of another app slides over the top of the screen: the route names SpringBoard, the app stays
        s.front, s.in_front = (SPRINGBOARD, None), [SHOP[1]]
        e = await refused(d, "tap", {"target": {"x": 200, "y": 85}, "screenshot": False})
        assert e.code == "not_actionable" and "covered: a notification is over the top of the screen" in str(e)
        assert s.sent == []
        # (as if the first tap had gone to the banner) another app is in front, on a chat
        s.front, s.in_front = (CHAT[1], 9), [CHAT[1]]
        e = await refused(d, "tap", {"target": {"x": 394, "y": 85}, "screenshot": False})
        assert e.code == "stale"
        assert str(e) == ("Chat (com.example.chat) is in front now, not Shop (com.example.shop), where your last look "
                          "was. Nothing was touched; look again.")
        assert s.sent == []
        await finish(stop, task)
    run(go)


def test_a_look_at_the_new_app_lets_the_agent_touch_it():
    # (the rule is about not touching what was never shown, not about staying in one app)
    async def go():
        s = unread()
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        s.app, s.front, s.in_front = CHAT, (CHAT[1], 9), [CHAT[1]]
        await refused(d, "tap", {"target": {"x": 200, "y": 400}, "screenshot": False})
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "tap", {"target": {"x": 200, "y": 400}, "screenshot": False})
        assert s.sent == [("tap", (200, 400))]
        await finish(stop, task)
    run(go)


def test_an_answer_that_shows_the_new_app_lets_the_next_touch_go_and_look_false_doesnt():
    async def go():
        s = unread()
        s.opens[(100, 600)] = CHAT
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "tap", {"target": {"x": 100, "y": 600}, "screenshot": False})  # its answer shows Chat
        await d.run_tool("a", "tap", {"target": {"x": 200, "y": 400}, "screenshot": False})
        assert s.sent == [("tap", (100, 600)), ("tap", (200, 400))]
        s.opens[(200, 500)] = SHOP
        await d.run_tool("a", "tap", {"target": {"x": 200, "y": 500}, "look": False})  # it brought Shop back, unseen
        e = await refused(d, "tap", {"target": {"x": 200, "y": 400}, "screenshot": False})
        assert e.code == "stale" and s.sent[-1] == ("tap", (200, 500))
        await finish(stop, task)
    run(go)


def test_a_run_batch_stops_where_a_step_would_touch_an_app_no_answer_showed():
    # (the brief expected each step's answer to show the next step's app; a run's steps have none: batch.args_of runs
    # them with look: false and the batch answers once. So a step that would touch an app an earlier step brought
    # forward stops the run, as any refused step does, and nothing is touched. STATUS-P: a decision for the lead)
    async def go():
        s = unread()
        s.opens[(100, 600)] = CHAT
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        e = await refused(d, "run", {"steps": [{"tool": "tap", "target": {"x": 100, "y": 600}},
                                               {"tool": "tap", "target": {"x": 200, "y": 400}}]})
        assert e.code == "stale" and "stopped at step 2" in str(e) and "Nothing was touched" in str(e)
        assert s.sent == [("tap", (100, 600))]
        # a run that stays in the app it was shown runs whole
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "run", {"steps": [{"tool": "tap", "target": {"x": 150, "y": 400}},
                                               {"tool": "tap", "target": {"x": 250, "y": 400}}]})
        assert s.sent[-2:] == [("tap", (150, 400)), ("tap", (250, 400))]
        await finish(stop, task)
    run(go)


# ---- the banner wait (A) ----

@pytest.mark.parametrize("args", [
    {"target": {"x": 200, "y": 85}},  # a point
    {"target": {"ref": "e1"}},  # an element
])
def test_every_touch_in_the_banner_zone_waits_a_banner_out(args):
    async def go():
        s = PointSession()
        s.tree = [button("e1", "Search", rect=(150, 70, 100, 30))]
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        s.front, s.in_front = (SPRINGBOARD, None), [SHOP[1]]
        e = await refused(d, "tap", {**args, "screenshot": False})
        assert "covered" in str(e) and s.sent == []
        await finish(stop, task)
    run(go)


def test_a_swipe_that_starts_in_the_banner_zone_waits_a_banner_out():
    async def go():
        s = unread()
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        s.front, s.in_front = (SPRINGBOARD, None), [SHOP[1]]
        e = await refused(d, "swipe", {"x0": 200, "y0": 100, "x1": 200, "y1": 600, "screenshot": False})
        assert "covered" in str(e) and s.sent == []
        await finish(stop, task)
    run(go)


def test_a_banner_that_leaves_lets_the_touch_go():
    async def go():
        s = unread()
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        s.front = (SPRINGBOARD, None)
        answers = iter([(SPRINGBOARD, None), (SHOP[1], 7)])

        async def front_app():
            s.front_questions += 1
            return next(answers, (SHOP[1], 7))
        s.front_app = front_app
        await d.run_tool("a", "tap", {"target": {"x": 200, "y": 85}, "screenshot": False})
        assert s.sent == [("tap", (200, 85))] and s.front_questions == 2  # (the banner, then the app: no third)
        await finish(stop, task)
    run(go)


def test_springboard_in_front_over_the_app_below_the_banner_zone_is_no_other_app():
    # (a banner, an alert or a permission prompt drawn by SpringBoard over the app: iOS's process list keeps the app)
    async def go():
        s = unread()
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        s.front, s.in_front = (SPRINGBOARD, None), [SHOP[1]]
        await d.run_tool("a", "tap", {"target": {"x": 200, "y": 500}, "screenshot": False})
        assert s.sent == [("tap", (200, 500))]
        s.in_front = []  # the home screen itself: the app is gone, and the agent didn't see that
        e = await refused(d, "tap", {"target": {"x": 200, "y": 500}, "screenshot": False})
        assert e.code == "stale" and "Home screen (com.apple.springboard) is in front now" in str(e)
        await finish(stop, task)
    run(go)


# ---- the exceptions (B) ----

def test_the_tools_that_change_the_app_on_purpose_are_not_checked():
    from tapwright import daemon as daemon_module
    d = make(PointSession())
    view = d.control.client("a")
    view.app = SHOP
    for name in ("open_app", "open_url", "press_button", "alert", "close_app", "rotate"):
        assert d._seen_apps(view, name) is None, name
    for name in ("tap", "long_press", "swipe", "scroll", "pinch", "toggle", "type_text", "back"):
        assert d._seen_apps(view, name) == (SHOP,), name
    view.aimed_app = CHAT  # (open_app with look: false: where the agent went)
    assert d._seen_apps(view, "tap") == (SHOP, CHAT)
    view.app = view.aimed_app = None
    assert d._seen_apps(view, "tap") is None  # never shown an app: as before
    assert daemon_module.SEEN_EXEMPT == {"open_app", "open_url", "press_button", "alert", "close_app", "rotate"}


def test_open_app_with_look_false_then_a_tap_in_the_app_it_opened():
    async def go():
        s = unread()
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        s.app, s.front, s.in_front = CHAT, (CHAT[1], 9), [CHAT[1]]
        view = d.control.client("a")
        view.aimed_app = CHAT  # (what open_app sets: the app it opened, until the next look)
        await d.run_tool("a", "tap", {"target": {"x": 200, "y": 400}, "look": False})
        assert s.sent == [("tap", (200, 400))]
        await finish(stop, task)
    run(go)


def test_a_keyboard_or_share_sheet_of_another_process_keeps_the_app_in_front():
    async def go():
        s = unread()
        s.last_keyboard = True
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "tap", {"target": {"x": 200, "y": 400}, "screenshot": False})
        assert s.sent[-1] == ("tap", (200, 400))  # (and the keyboard reflex's touch before it went out too)
        await finish(stop, task)
    run(go)


def test_a_persons_touch_from_the_live_view_is_not_checked():
    async def go():
        s = unread()
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        s.front, s.in_front = (CHAT[1], 9), [CHAT[1]]
        assert s.seen_app is None  # (set only for an agent's action, and cleared after it)
        await d.viewer_message({"type": "gesture", "kind": "tap", "path": [[0, 0.5, 0.5]]})
        assert s.front_questions == 0 and s.seen_app is None
        await finish(stop, task)
    run(go)


# ---- the cost, and an older runner ----

@pytest.mark.parametrize("y", [85, 400])
def test_one_question_to_the_runner_per_touch(y):
    async def go():
        s = unread()
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        s.front_questions = 0
        await d.run_tool("a", "tap", {"target": {"x": 200, "y": y}, "screenshot": False})
        assert s.sent == [("tap", (200, y))] and s.front_questions == 1  # (the banner wait's answer serves the check)
        await finish(stop, task)
    run(go)


def test_an_older_runner_without_the_front_route_touches_as_before():
    async def go():
        s = unread()
        s.tw_version = 10
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        s.front, s.in_front = (CHAT[1], 9), [CHAT[1]]
        await d.run_tool("a", "tap", {"target": {"x": 200, "y": 85}, "screenshot": False})
        assert s.sent == [("tap", (200, 85))] and s.front_questions == 0
        await finish(stop, task)
    run(go)


def test_a_front_route_that_cant_say_lets_the_touch_go():
    async def go():
        s = unread()
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        s.front = None
        await d.run_tool("a", "tap", {"target": {"x": 200, "y": 85}, "screenshot": False})
        assert s.sent == [("tap", (200, 85))]
        await finish(stop, task)
    run(go)


def test_a_run_that_opens_an_app_then_touches_it():
    # (open_app records the app it opened, `aimed_app`, with or without a look: the next step's touch may go there)
    async def go():
        s = unread()
        d = make(s)
        stop, task = await started(d)
        s.app_names.update({SHOP[1]: SHOP[0], CHAT[1]: CHAT[0]})
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "run", {"steps": [{"tool": "open_app", "app": CHAT[1]},
                                               {"tool": "tap", "target": {"x": 200, "y": 400}}]})
        assert s.front == (CHAT[1], 9) and s.sent == [("tap", (200, 400))]
        await finish(stop, task)
    run(go)

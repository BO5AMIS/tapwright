"""Tapwright 2.0.3, safety: a touch must not land in another app. On a person's phone (2026-11-26) an agent aimed two point
taps at a screen whose elements weren't read. A notification banner was over the top of the screen: the first tap landed
on it and opened another app on a chat with a person, the second landed on that chat's call button and started a call.

The rules (review RP): what the agent saw is recorded from the runner's front route (and iOS's process list when that
names SpringBoard) as each answer shows it the screen, in three states: an app in front, the home screen, SpringBoard
over an app. Right before each touch the same question is asked again and compared (Session._front_rules); a banner in
the top zone is waited out (`covered` if it stays); anything Tapwright can't tell is refused. The principle: when
Tapwright can't tell a touch is safe, it doesn't touch.

The fake session's touches go through the real Session._touching and its gate, with their point."""
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
HOME = ("Home screen", SPRINGBOARD)


@pytest.fixture(autouse=True)
def quick_banner(monkeypatch):
    monkeypatch.setattr(session_module, "BANNER_WAIT", 0.04)
    monkeypatch.setattr(session_module, "BANNER_POLL", 0.01)
    from tapwright import ocr
    monkeypatch.setattr(ocr, "reader", None)
    monkeypatch.setattr(ocr, "available", lambda: False)


def _real(name: str):
    """Session's own method (None on a build without it: this file also runs against 137b583, before the review)."""
    return getattr(Session, name, None)


class PointSession(DescribingSession):
    """A fake session whose touches go through the real Session._touching and its gate (as the hands' `before` or the
    runner's request runs it), with their point. `front`: what the runner's front route answers ((bundle id, pid) or
    None: it can't say); `in_front`: iOS's process list; `opens`: a touch at that point brings that app to the front."""

    _touching = _real("_touching")
    _clear_of_banner = _real("_clear_of_banner")
    _seen_check = _real("_seen_check")
    _gate = _real("_gate")
    front_state = _real("front_state")
    _front_rules = _real("_front_rules")
    _words = _real("_words")
    see_front = _real("see_front")
    _wait_banner = _real("_wait_banner")

    def __init__(self, app=SHOP) -> None:
        super().__init__()
        self.tw_version = 11
        self.app = app
        self.front: tuple[str, int | None] | None = (app[1], 7)
        self.in_front = [] if app[1] == SPRINGBOARD else [app[1]]
        self.front_questions = 0
        self.sent: list[tuple[str, tuple[float, float]]] = []
        self.opens: dict[tuple[float, float], tuple[str, str]] = {}
        self.input_point, self.top_touch, self.seen_app = None, False, None
        self.touch_rules, self._gate_at, self.banner_guard = None, None, False
        self.app_names = {SHOP[1]: SHOP[0], CHAT[1]: CHAT[0]}

        async def foreground_apps():
            if isinstance(self.in_front, Exception):
                raise self.in_front
            return list(self.in_front)
        self.device.foreground_apps = foreground_apps

    async def front_app(self):
        self.front_questions += 1
        return self.front

    async def _goes(self, kind: str, ms: float, at: tuple[float, float]) -> None:
        await self._touching(ms, at)
        if callable(getattr(self, "_gate", None)):
            await self._gate()  # (as the hands' `before` or the runner's touch request run it)
        self._record(kind)
        self.sent.append((kind, at))
        if at in self.opens:
            self.app = self.opens[at]
            self.front, self.in_front, self.tree = (self.app[1], 9), [self.app[1]], []

    async def tap(self, target, hold_ms=60):
        hit = ui.resolve(self.items, target)
        at = hit if isinstance(hit, tuple) else hit.center
        await self._goes("tap" if hold_ms < 300 else "long_press", hold_ms, at)
        return f"Tapped ({round(at[0])}, {round(at[1])})."

    async def launch(self, bundle_id, arguments=None):
        out = await super().launch(bundle_id, arguments)
        self.front, self.in_front = (bundle_id, 9), [bundle_id]
        return out

    async def swipe(self, x0, y0, x1, y1, duration_ms=350, hold_ms=0):
        await self._goes("swipe", duration_ms + hold_ms, (x0, y0))
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


def banner_down(s: PointSession, over=SHOP) -> None:
    """SpringBoard over `over`: the route names SpringBoard, iOS's process list keeps the app."""
    s.front, s.in_front = (SPRINGBOARD, None), [over[1]]


# ---- the incident ----

def test_the_incident_two_point_taps_on_an_unread_screen_with_a_banner_then_another_app():
    async def go():
        s = unread()
        d = make(s)
        stop, task = await started(d)
        s.app_names.update({SHOP[1]: SHOP[0], CHAT[1]: CHAT[0]})
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert ui.ELEMENTS_NONE in r["text"]
        banner_down(s)  # a banner of another app slides over the top of the screen
        e = await refused(d, "tap", {"target": {"x": 200, "y": 85}, "screenshot": False})
        assert e.code == "not_actionable" and "covered: a notification is over the top of the screen" in str(e)
        assert s.sent == []
        s.front, s.in_front = (CHAT[1], 9), [CHAT[1]]  # (as if the first tap had gone to the banner)
        e = await refused(d, "tap", {"target": {"x": 394, "y": 85}, "screenshot": False})
        assert e.code == "stale"
        assert str(e) == ("Chat (com.example.chat) is in front now, not Shop (com.example.shop), where your last look "
                          "was. Nothing was touched; look again.")
        assert s.sent == []
        await finish(stop, task)
    run(go)


def test_a_look_at_the_new_app_lets_the_agent_touch_it():
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
    # (a run's steps have no answers of their own: batch.args_of runs them with look: false. A step that merely taps
    # and brings another app forward makes the next touch `stale`: that is the incident, and it stays)
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
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "run", {"steps": [{"tool": "tap", "target": {"x": 150, "y": 400}},
                                               {"tool": "tap", "target": {"x": 250, "y": 400}}]})
        assert s.sent[-2:] == [("tap", (150, 400)), ("tap", (250, 400))]
        await finish(stop, task)
    run(go)


def test_a_run_that_opens_an_app_then_touches_it():
    async def go():
        s = unread()
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "run", {"steps": [{"tool": "open_app", "app": CHAT[1]},
                                               {"tool": "tap", "target": {"x": 200, "y": 400}}]})
        assert s.front == (CHAT[1], 9) and s.sent == [("tap", (200, 400))]
        await finish(stop, task)
    run(go)


def test_open_app_with_look_false_then_a_tap_in_the_app_it_opened():
    async def go():
        s = unread()
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "open_app", {"app": CHAT[1], "look": False})
        await d.run_tool("a", "tap", {"target": {"x": 200, "y": 400}, "look": False})
        assert s.sent == [("tap", (200, 400))]
        await finish(stop, task)
    run(go)


# ---- the banner wait ----

@pytest.mark.parametrize("args", [
    {"target": {"x": 200, "y": 85}},  # a point
    {"target": {"ref": "e1"}},  # an element of the app, seen alone
])
def test_every_touch_in_the_banner_zone_waits_a_banner_out(args):
    async def go():
        s = PointSession()
        s.tree = [button("e1", "Search", rect=(150, 70, 100, 30))]
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        banner_down(s)
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
        banner_down(s)
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
        banner_down(s)
        answers = iter([(SPRINGBOARD, None), (SHOP[1], 7)])

        async def front_app():
            s.front_questions += 1
            return next(answers, (SHOP[1], 7))
        s.front_app = front_app
        s.front_questions = 0
        await d.run_tool("a", "tap", {"target": {"x": 200, "y": 85}, "look": False})
        assert s.sent == [("tap", (200, 85))] and s.front_questions == 2  # (the banner, then the app: no third)
        await finish(stop, task)
    run(go)


def test_springboard_over_the_app_the_agent_saw_alone_is_refused_below_the_zone_too():
    # (review RP M1a, the reviewer's P3: the process list keeping the app is no proof of an alert; Notification Center or
    # an expanded notification over the app took any unseen touch)
    async def go():
        s = unread()
        d = make(s)
        stop, task = await started(d)
        s.app_names.update({SHOP[1]: SHOP[0]})
        await d.run_tool("a", "look", {"screenshot": False})
        banner_down(s)
        e = await refused(d, "tap", {"target": {"x": 200, "y": 420}, "look": False})
        # (review RP2 M2: a point is never touched on the system over the app)
        assert e.code == "not_actionable" and str(e).startswith("covered: something of the system over Shop")
        s.in_front = []  # the home screen itself: the app is gone, and the agent didn't see that
        e = await refused(d, "tap", {"target": {"x": 200, "y": 500}, "look": False})
        assert e.code == "stale" and "the home screen is in front now, not Shop" in str(e)
        assert s.sent == []
        await finish(stop, task)
    run(go)


def test_an_alert_the_agent_looked_at_may_be_touched():
    # (the agent's last answer showed SpringBoard over the app: it saw what it is touching)
    async def go():
        s = unread()
        s.tree = [button("e1", "Allow", rect=(100, 400, 200, 44))]
        s.tier = "full"
        banner_down(s)  # a permission prompt over the app: SpringBoard's, the app under it
        s.last_alert = ui.Alert("Allow Shop to use your location?", ("Allow", "Don't Allow"), (40, 300, 320, 200))
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        # (force: past the daemon's own behind_alert check of the fake's elements; what is tested is the touch rule)
        await d.run_tool("a", "tap", {"target": {"label": "Allow"}, "look": False, "force": True})
        assert s.sent == [("tap", (200.0, 422.0))]
        await finish(stop, task)
    run(go)


# ---- the review's attacks, each now refused (P1-P8) ----

def test_p1_a_look_taken_while_a_banner_is_down_doesnt_disarm_the_rules():
    async def go():
        s = unread()
        d = make(s)
        stop, task = await started(d)
        s.app, s.front, s.in_front = HOME, (SPRINGBOARD, None), [SHOP[1]]  # the banner is down as the agent looks
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert r["text"].startswith("Note: something of the system")  # (never "the agent saw the home screen")
        assert d.control.client("a").seen_front == session_module.Front(session_module.FRONT_OVER, SHOP[1])
        e = await refused(d, "tap", {"target": {"x": 200, "y": 85}, "look": False})
        assert "covered" in str(e) and s.sent == []
        await finish(stop, task)
    run(go)


def test_p2_no_long_press_on_a_banner_the_agent_didnt_aim_at():
    async def go():
        s = unread()
        d = make(s)
        stop, task = await started(d)
        s.app, s.front, s.in_front = HOME, (SPRINGBOARD, None), [SHOP[1]]
        await d.run_tool("a", "look", {"screenshot": False})
        e = await refused(d, "long_press", {"target": {"x": 200, "y": 85}, "look": False})
        assert "covered" in str(e)
        # (review RP2 M2) below the zone too: a point is never touched on the system over the app
        e = await refused(d, "tap", {"target": {"x": 200, "y": 420}, "look": False})
        assert str(e).startswith("covered: something of the system") and s.sent == []
        await finish(stop, task)
    run(go)


def test_p3_springboard_over_the_app_seen_alone_takes_no_touch():
    async def go():
        s = unread()
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        banner_down(s)
        for y in (420, 520):
            await refused(d, "tap", {"target": {"x": 200, "y": y}, "look": False})
        assert s.sent == []
        await finish(stop, task)
    run(go)


def test_p4_the_front_route_that_cant_say_right_after_a_switch_refuses():
    async def go():
        s = unread()
        s.opens[(100, 600)] = CHAT
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "tap", {"target": {"x": 100, "y": 600}, "look": False})
        s.front = None  # (mid-launch: the route can't tell)
        e = await refused(d, "tap", {"target": {"x": 394, "y": 85}, "look": False})
        assert e.code == "not_actionable" and str(e) == session_module.UNKNOWN_FRONT
        assert s.sent == [("tap", (100, 600))]
        await finish(stop, task)
    run(go)


def test_p5_a_process_list_that_fails_with_springboard_in_front_refuses():
    async def go():
        s = unread()
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        s.front, s.in_front = (SPRINGBOARD, None), RuntimeError("usbmux")
        for y in (85, 500):
            e = await refused(d, "tap", {"target": {"x": 200, "y": y}, "look": False})
            assert e.code == "not_actionable"
        assert s.sent == []
        await finish(stop, task)
    run(go)


def test_p6_a_runs_refusal_says_which_step_was_not_touched():
    async def go():
        s = unread()
        s.opens[(100, 600)] = CHAT
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        e = await refused(d, "run", {"steps": [{"tool": "tap", "target": {"x": 100, "y": 600}},
                                               {"tool": "tap", "target": {"x": 200, "y": 400}}]})
        assert str(e).startswith("Ran 1 of 2 steps; stopped at step 2") and "Nothing was touched" in str(e)
        await finish(stop, task)
    run(go)


def test_p7_open_url_with_look_false_records_where_the_link_went():
    async def go():
        s = unread()
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})

        async def open_url(url):
            s.app, s.front, s.in_front = CHAT, (CHAT[1], 9), [CHAT[1]]
            return "opened " + url
        s.open_url = open_url
        await d.run_tool("a", "run", {"steps": [{"tool": "open_url", "url": "chat://thread/1"},
                                               {"tool": "tap", "target": {"x": 200, "y": 400}}]})
        assert s.sent == [("tap", (200, 400))]
        await finish(stop, task)
    run(go)


def test_p8_the_home_screen_doesnt_stick_after_an_icons_tap():
    async def go():
        s = unread(HOME)
        s.opens[(100, 600)] = SHOP
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "tap", {"target": {"x": 100, "y": 600}, "screenshot": False})  # its answer shows Shop
        s.app, s.front, s.in_front = HOME, (SPRINGBOARD, None), []  # Shop crashes to the home screen
        e = await refused(d, "tap", {"target": {"x": 300, "y": 500}, "look": False})
        assert e.code == "stale" and s.sent == [("tap", (100, 600))]
        await finish(stop, task)
    run(go)


def test_a_passed_expect_counts_as_seeing_the_screen():
    async def go():
        s = unread()
        s.opens[(100, 600)] = CHAT
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        s.tree_after = [button("e9", "Message", "message-field", rect=(20, 700, 360, 40))]
        real = s._goes

        async def goes(kind, ms, at):
            await real(kind, ms, at)
            if at == (100, 600):
                s.tree, s.tier = list(s.tree_after), "full"
        s._goes = goes
        await d.run_tool("a", "run", {"steps": [
            {"tool": "tap", "target": {"x": 100, "y": 600}, "expect": {"id": "message-field"}},
            {"tool": "tap", "target": {"x": 200, "y": 400}}]})
        assert s.sent == [("tap", (100, 600)), ("tap", (200, 400))]
        await finish(stop, task)
    run(go)


# ---- the window between the check and the touch (M3) ----

def test_the_check_comes_after_the_touch_hook():
    async def go():
        s = unread()
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        real = d._agent_touches.notify

        async def notify(ms):
            s.front, s.in_front = (CHAT[1], 9), [CHAT[1]]  # another app comes forward during Tapline's notice
            await real(ms)
        d._agent_touches.notify = notify
        e = await refused(d, "tap", {"target": {"x": 200, "y": 400}, "look": False})
        assert e.code == "stale" and s.sent == []
        await finish(stop, task)
    run(go)


# ---- the tool's own touches (decision 6) ----

def test_a_tool_that_opened_notification_center_may_close_it():
    async def go():
        s = PointSession()
        s.touch_rules = session_module.TouchRules(session_module.Front(session_module.FRONT_APP, SHOP[1]))
        await s._goes("swipe", 300, (120.0, 1.0))  # the tool opens Notification Center: Shop was in front
        banner_down(s)  # Notification Center over Shop
        with pytest.raises(Refused):  # checked against the agent's answer (Shop alone): refused
            await s._goes("swipe", 250, (200.0, 798.0))
        await s.see_front(element=True)  # the tool read what it opened
        await s._goes("swipe", 250, (200.0, 798.0))  # its closing swipe
        await s._goes("tap", 60, (150.0, 120.0))  # or a notification's row, an element of its read, near the top
        assert [k for k, _ in s.sent] == ["swipe", "swipe", "tap"]
    run(go)


# ---- the exceptions ----

def test_the_tools_that_change_the_app_on_purpose_are_not_checked():
    from tapwright import daemon as daemon_module
    d = make(PointSession())
    view = d.control.client("a")
    view.seen_front = session_module.Front(session_module.FRONT_APP, SHOP[1])
    for name in ("open_app", "open_url", "press_button", "alert", "close_app", "rotate"):
        assert d._touch_rules(view, name, None) is None, name
    for name in ("tap", "long_press", "swipe", "scroll", "pinch", "toggle", "type_text", "back"):
        assert d._touch_rules(view, name, None) == session_module.TouchRules(view.seen_front, False), name
    assert d._touch_rules(view, "tap", ui.Target(ref="e1")).element is True
    assert d._touch_rules(view, "tap", ui.Target(x=1.0, y=2.0)).element is False
    assert daemon_module.SEEN_EXEMPT == {"open_app", "open_url", "press_button", "alert", "close_app", "rotate"}


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


def test_the_keyboard_reflex_is_checked_too():
    async def go():
        s = unread()
        s.last_keyboard = True
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        s.front, s.in_front = (CHAT[1], 9), [CHAT[1]]
        await refused(d, "tap", {"target": {"x": 200, "y": 400}, "look": False})
        assert s.sent == []
        await finish(stop, task)
    run(go)


def test_a_persons_touch_from_the_live_view_is_not_checked():
    async def go():
        s = unread()
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        s.front, s.in_front = (CHAT[1], 9), [CHAT[1]]
        assert s.touch_rules is None  # (set only for an agent's action, and cleared after it)
        s.front_questions = 0
        await d.viewer_message({"type": "gesture", "kind": "tap", "path": [[0, 0.5, 0.5]]})
        assert s.front_questions == 0 and s.touch_rules is None
        await finish(stop, task)
    run(go)


# ---- the cost, and an older runner ----

@pytest.mark.parametrize("y", [85, 400])
def test_one_question_to_the_runner_per_touch_and_one_per_answer(y):
    async def go():
        s = unread()
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        s.front_questions = 0
        await d.run_tool("a", "tap", {"target": {"x": 200, "y": y}, "look": False})
        assert s.sent == [("tap", (200, y))] and s.front_questions == 1  # (the touch's own)
        await d.run_tool("a", "tap", {"target": {"x": 200, "y": y}, "screenshot": False})
        assert s.front_questions == 4  # (the touch's, and the answer's two: before and after its read, review RP2 H2)
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


# ---- review RP2: its attacks (A1-A7), each now refused, and its honest refusals (B*) as decided ----

def test_a1_an_expect_of_something_gone_doesnt_count_as_seeing():
    async def go():
        s = unread()
        s.opens[(100, 600)] = CHAT
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        e = await refused(d, "run", {"steps": [
            {"tool": "tap", "target": {"x": 100, "y": 600}, "expect": {"text": "Loading", "gone": True}},
            {"tool": "tap", "target": {"x": 394, "y": 85}}]})
        assert e.code == "stale" and s.sent == [("tap", (100, 600))]
        await finish(stop, task)
    run(go)


def test_a2_an_app_that_comes_forward_after_the_read_isnt_recorded_as_seen():
    async def go():
        s = PointSession()
        s.tree = [button("e1", "Contact", rect=(50, 580, 100, 40))]
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        real = s.snapshot

        async def snapshot(*a, **k):
            snap = await real(*a, **k)
            s.front, s.in_front = (CHAT[1], 9), [CHAT[1]]  # the read is of Shop; Chat comes forward right after it
            return snap
        s.snapshot = snapshot
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "screenshot": False})
        assert "Note: another app came to the front while this was read; look again." in r["text"]
        assert d.control.client("a").seen_front.kind == session_module.FRONT_UNKNOWN
        e = await refused(d, "tap", {"target": {"x": 394, "y": 85}, "look": False})
        assert e.code == "not_actionable" and str(e).startswith("unknown_front:") and s.sent == [("tap", (100.0, 600.0))]
        await finish(stop, task)
    run(go)


def test_a3_the_system_the_agent_saw_over_the_app_licenses_nothing_once_it_has_gone():
    async def go():
        s = unread()
        d = make(s)
        stop, task = await started(d)
        banner_down(s)  # a banner that stays: the look records the system over Shop
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert r["text"].startswith("Note: something of the system")
        s.front, s.in_front = (SHOP[1], 7), [SHOP[1]]
        await d.run_tool("a", "tap", {"target": {"x": 200, "y": 600}, "look": False})  # Shop alone: the record follows
        assert d.control.client("a").seen_front == session_module.Front(session_module.FRONT_APP, SHOP[1])
        banner_down(s)  # later a system alert over Shop
        e = await refused(d, "tap", {"target": {"x": 200, "y": 460}, "look": False})
        assert str(e).startswith("covered: something of the system") and s.sent == [("tap", (200, 600))]
        await finish(stop, task)
    run(go)


def test_a4_on_the_home_screen_a_point_in_the_banner_zone_is_refused_and_an_element_goes():
    async def go():
        s = unread(HOME)
        s.tree, s.tier = [button("e1", "Weather", rect=(170, 80, 60, 60))], "full"
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        e = await refused(d, "long_press", {"target": {"x": 200, "y": 100}, "look": False})
        assert e.code == "not_actionable" and str(e) == session_module.COVERED_HOME
        await d.run_tool("a", "tap", {"target": {"x": 200, "y": 330}, "look": False})  # below the zone: goes
        await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False})  # an element of the look: goes
        assert s.sent == [("tap", (200, 330)), ("tap", (200.0, 110.0))]
        await finish(stop, task)
    run(go)


def test_a5_a_runner_version_not_known_is_never_taken_for_an_old_runner():
    async def go():
        s = unread()
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        s.tw_version = 0  # (the version question failed after a reconnect: not answered)
        s.front, s.in_front = (CHAT[1], 9), [CHAT[1]]
        e = await refused(d, "tap", {"target": {"x": 394, "y": 85}, "look": False})
        assert str(e).startswith("unknown_front:") and s.sent == []
        s.tw_answered = 10  # (a version that was answered, below 11: no check, as before 2.0.3)
        await d.run_tool("a", "tap", {"target": {"x": 394, "y": 85}, "look": False})
        assert s.sent == [("tap", (394, 85))]
        await finish(stop, task)
    run(go)


def test_a6_the_notification_center_tools_looser_rule_ends_with_the_center():
    from fakes import FakeSession
    from test_notifications import BUNDLE, SCREEN

    async def go():
        s = FakeSession()
        s.foreground, s.center_source = BUNDLE, SCREEN
        d = make(s)
        agents = session_module.TouchRules(session_module.Front(session_module.FRONT_APP, BUNDLE))
        s.touch_rules = agents
        seen = []

        async def see_front(element=False):
            s.touch_rules = session_module.TouchRules(session_module.Front(session_module.FRONT_OVER, BUNDLE), True)
            seen.append(s.touch_rules)
        s.see_front = see_front
        await d._read_center(lambda b: True)
        assert seen and s.touch_rules is agents  # (the agent's own rules again once the center is closed)
    run(go)


class _Hands:
    """Hands whose touch awaits `before` with its lock held, as Hands._play does."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.sent = False

    async def tap(self, x, y, hold, screen=None, turn=0, before=None):
        async with self.lock:
            await before()
            self.sent = True


def _hands_session(on_poll) -> "tuple[PointSession, _Hands]":
    s = PointSession()
    s.touch_rules = session_module.TouchRules(session_module.Front(session_module.FRONT_APP, SHOP[1]))
    banner_down(s)
    hands = _Hands()
    polls = {"n": 0}

    async def front_app():
        polls["n"] += 1
        on_poll(polls["n"], hands)
        return (SHOP[1], 7) if polls["n"] >= 3 else (SPRINGBOARD, None)
    s.front_app = front_app
    s.hands, s.hands_on = hands, (lambda: True)
    s.before_input, s.front_apps, s.hands_connection = None, None, None

    async def ensure():
        return None
    s.ensure = ensure
    return s, hands


@pytest.mark.parametrize("who", ["pause", "person"])
def test_a7_a_pause_or_a_persons_touch_during_the_banner_wait_stops_the_touch(who):
    # (review RP2 H1: the guard ran before the gate's banner wait, never after it, and the wait held the hands' lock)
    state = {"stop": False, "lock_held_in_wait": None}

    def on_poll(n, hands):
        if n == 2:
            state["stop"] = True  # the owner presses pause, or a person touches the phone, as the banner is waited out
            state["lock_held_in_wait"] = hands.lock.locked()

    async def go():
        s, hands = _hands_session(on_poll)

        async def guard():
            if state["stop"]:
                raise Refused("paused" if who == "pause" else "busy", "stopped")
        s.execution_guard = guard
        await s._touching(60, (200, 85))
        with pytest.raises(Refused):
            await Session._by_hands(s, "tap", lambda before: hands.tap(200, 85, 60, before=before))
        assert not hands.sent and state["lock_held_in_wait"] is False  # (a person's own touch could go first)
    run(go)


def test_b1_a_screen_whose_app_the_route_cant_name_says_how_to_go_on_and_open_app_names_it():
    async def go():
        s = unread()
        s.front = None  # the route never names the app on this screen
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        e = await refused(d, "tap", {"target": {"x": 200, "y": 500}, "look": False})
        assert str(e).startswith("unknown_front:") and "open_app" in str(e)
        await d.run_tool("a", "open_app", {"app": SHOP[1], "look": False})
        s.front = None  # (still can't name it; iOS's process list has Shop in the foreground)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "tap", {"target": {"x": 200, "y": 500}, "look": False})
        assert s.sent == [("tap", (200, 500))]
        await finish(stop, task)
    run(go)


def test_b2_and_b5_a_touch_with_no_look_yet_says_so():
    async def go():
        s = unread()
        d = make(s)
        stop, task = await started(d)
        e = await refused(d, "tap", {"target": {"x": 200, "y": 500}, "look": False})
        assert e.code == "not_actionable" and str(e) == session_module.NO_LOOK
        await d.run_tool("a", "look", {"screenshot": False})
        e = await refused(d, "tap", {"target": {"x": 200, "y": 500}, "look": False}) if False else None
        with pytest.raises(Refused) as other:  # another client that never looked
            await d.run_tool("b", "tap", {"target": {"x": 200, "y": 500}, "look": False})
        assert str(other.value) == session_module.NO_LOOK
        await finish(stop, task)
    run(go)


def test_b4_a_whole_read_of_the_system_over_the_app_is_recorded_at_once():
    # (review RP2 L4: Control Center or Notification Center the agent opened, read whole: no 8 s wait for a "banner")
    async def go():
        s = PointSession()
        s.tree = [button("e1", "Wi-Fi", rect=(40, 300, 100, 100))]
        d = make(s)
        stop, task = await started(d)
        banner_down(s)
        s.front_questions = 0
        await d.run_tool("a", "look", {"screenshot": False})
        assert s.front_questions == 2  # (before and after the read: no polls)
        assert d.control.client("a").seen_front == session_module.Front(session_module.FRONT_OVER, SHOP[1])
        await finish(stop, task)
    run(go)

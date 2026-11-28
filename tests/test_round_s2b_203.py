"""Tapwright 2.0.3, round S2: review RQ on rounds Q and R. RQ-H1: with the system over the app in the agent's last
answer (Control Center, Notification Center, a banner that stayed), round R made the app underneath the one a touch was
meant for, so a touch the agent aimed at the system UI it saw was taken for a theft and the app reopened. That state is
SpringBoard's again. RQ-L1: on a runner that records no front, only open_app or open_url since the read skips the
comparison, not any action without a look."""
from fakes import button, el
from test_banner_hit_203 import _looked
from test_daemon import finish
from test_round_r_203 import OpeningSession
from test_touch_other_app_203 import CHAT, SHOP, quick_banner, run  # noqa: F401 (fixture)

from tapwright import session as session_module
from tapwright.session import SPRINGBOARD

HOME = ("Home screen", SPRINGBOARD)


def system_over_the_shop(rows):
    """The system's UI over the shopping app, as the phone shows it: the front route names SpringBoard, iOS's process
    list keeps the app; the tree is SpringBoard's."""
    s = OpeningSession()
    s.tier, s.tree = "full", rows
    s.app, s.front, s.in_front, s.foreground = HOME, (SPRINGBOARD, None), [SHOP[1]], SPRINGBOARD
    s.hit = lambda x, y: {"t": "XCUIElementTypeButton", "r": [0, 0, 430, 300], "b": SPRINGBOARD, "identity": "verified"}
    return s


def test_rq_h1_a_tap_on_a_control_center_toggle_by_ref_reopens_nothing():
    async def go():
        s = system_over_the_shop([button("e1", "Screen Mirroring", "mirroring", rect=(20, 110, 180, 40))])
        d, stop, task = await _looked(s)
        assert d.control.client("a").seen_front == session_module.Front(session_module.FRONT_OVER, SHOP[1])
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False})
        assert r["text"].startswith("Tapped") and "came to the front" not in r["text"]
        assert s.launched == [] and s.sent == [("tap", (110.0, 130.0))]
        await finish(stop, task)
    run(go)


def test_rq_h1_a_tap_on_a_banner_row_the_agent_saw_that_opens_its_app_is_not_sent_back():
    async def go():
        s = system_over_the_shop([button("e1", "Chat: see you at 6", "banner", rect=(8, 70, 414, 50))])
        s.opens[(215.0, 95.0)] = CHAT  # (the banner's app opens, on purpose)
        d, stop, task = await _looked(s)
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False})
        assert "came to the front" not in r["text"] and s.launched == [] and s.front[0] == CHAT[1]
        await finish(stop, task)
    run(go)


def test_rq_h1_typing_into_notification_centers_search_then_without_a_target_is_not_stale():
    async def go():
        s = system_over_the_shop([el("e1", "input", "Search", "search", rect=(20, 40, 390, 36))])
        d, stop, task = await _looked(s)
        await d.run_tool("a", "type_text", {"target": {"ref": "e1"}, "text": "wea", "look": False})
        assert d.control.client("a").aimed_app == HOME
        await d.run_tool("a", "type_text", {"text": "ther"})  # (no target: into the field the agent typed into)
        assert len(s.typed) == 2
        await finish(stop, task)
    run(go)


def test_rq_l1_an_action_without_a_look_still_compares_on_a_runner_that_records_no_front():
    async def go():
        s = OpeningSession()
        d, stop, task = await _looked(s)
        view = d.control.client("a")
        await d.run_tool("a", "tap", {"target": {"x": 200, "y": 600}, "look": False})  # (lower down, in the app)
        view.seen_front = None  # (as on a runner below route version 11, which records no front)
        assert view.unlooked == 1 and d._meant_app(view) == SHOP
        await d.run_tool("a", "open_app", {"app": CHAT[1], "look": False})
        view.seen_front = None
        assert d._meant_app(view) is None  # (open_app since the read: no comparison)
        await d.run_tool("a", "look", {"screenshot": False})
        view.seen_front = None
        assert view.left_by_call is False and d._meant_app(view) is not None  # (a read since: compared again)
        await finish(stop, task)
    run(go)

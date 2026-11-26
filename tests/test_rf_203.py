"""Tapwright 2.0.3, the final review (RF) of the merged branch: the open_app fallback is a guess that licenses no point
near the top (H1), `back` taps a screen's own back control only when "back" is its whole meaning (M1) and only from its
own whole read (M2), the tap beside a sheet only for a real sheet and a clear point (M3), a passing banner is named as
one (L2), and the loop watchdog's lines aren't scrubbed on its thread (L1). The review's four probes are here inverted."""
import asyncio
import logging
import threading

import pytest
from fakes import FrameFeeder, button, el
from test_203q_own_back import SEARCH, UPRIGHT
from test_daemon import finish, looked, make, on_loop_clock, run, run_virtual, started
from test_touch_other_app_203 import SHOP, unread

from tapwright import loopdog, server, ui
from tapwright import session as session_module
from tapwright.control import Refused

Front = session_module.Front


async def refused(d, name: str, args: dict) -> Refused:
    with pytest.raises(Refused) as e:
        await d.run_tool("a", name, args)
    return e.value


# ---- H1: the app named only by iOS's process list ----

def _guessed_shop():
    """open_app Shop and a look on a screen whose app the route can't name; then (the probe) a banner comes down: the
    route still says nothing, and the process list, which never lists SpringBoard, has Shop alone."""
    s = unread()
    s.tree = [button("e1", "Menu", rect=(20, 70, 60, 30)), button("e2", "Buy now", rect=(50, 500, 200, 44))]
    return s


def test_rf1_under_a_guess_a_point_in_the_banner_zone_is_refused_covered():
    async def go():
        s = _guessed_shop()
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": SHOP[1], "look": False})
        await d.run_tool("a", "look", {"screenshot": False})
        s.front, s.in_front = None, [SHOP[1]]
        e = await refused(d, "tap", {"target": {"x": 200, "y": 85}, "look": False})
        assert e.code == "not_actionable" and str(e).startswith("covered: can't tell whether a notification is over")
        assert s.sent == []  # (the probe: the tap went out onto the banner)
        await finish(stop, task)
    run(go)


def test_rf1_under_a_guess_an_element_and_a_point_below_the_zone_go():
    async def go():
        s = _guessed_shop()
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": SHOP[1], "look": False})
        s.front, s.in_front = None, [SHOP[1]]
        await d.run_tool("a", "look", {"screenshot": False})
        assert d.control.client("a").seen_front == Front(session_module.FRONT_APP, SHOP[1], guessed=True)
        await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False})  # an element of the look, in the zone
        await d.run_tool("a", "tap", {"target": {"x": 200, "y": 400}, "look": False})  # below the zone
        assert s.sent == [("tap", (50.0, 85.0)), ("tap", (200, 400))]
        await finish(stop, task)
    run(go)


def test_a_named_and_a_guessed_state_of_one_app_agree_and_the_record_is_the_guess():
    from tapwright.daemon import Daemon
    named, guess = Front(session_module.FRONT_APP, SHOP[1]), Front(session_module.FRONT_APP, SHOP[1], guessed=True)
    assert Daemon._front_agreed(named, guess, SHOP[1]) == guess
    assert Daemon._front_agreed(guess, named, SHOP[1]) == guess
    assert Daemon._front_agreed(named, named, SHOP[1]) == named


# ---- M1: "back" as the control's whole meaning ----

@pytest.mark.parametrize("label", ["Back", "back", " BACK ", "Go back", "go Back", "Back button", "Navigate back",
                                   "رجوع", "عودة"])
def test_a_label_that_is_back_and_nothing_else(label):
    assert ui.names_back(label, label=True)


@pytest.mark.parametrize("label", ["Call back", "Send back", "Back up", "Back camera", "Text me back", "Get back in",
                                   "Jack Back", "Back to top", "Bring it back", "Give back", "Pay back", "رجوع للخلف"])
def test_a_label_where_back_is_part_of_something_else(label):
    assert not ui.names_back(label, label=True)


@pytest.mark.parametrize("name", ["searchResultBackButton", "icBrutalBack", "nav_back", "backButton", "back", "BackBtn",
                                  "ic_back", "back-icon", "nav.back.image", "go_back_arrow", "headerBackView",
                                  "BACK_BUTTON", "toolbar2Back"])
def test_an_id_that_ends_in_back(name):
    assert ui.names_back(name)


@pytest.mark.parametrize("name", ["callBackButton", "backCamera", "backupButton", "playbackButton", "feedback",
                                  "backgroundPicker", "switchToBackCamera", "FeedBack", "PlayBack", "CashBack",
                                  "BackSpace", "backToTop", "rollback", "sendBack_btn", "flashBackIcon"])
def test_an_id_where_back_is_something_else(name):
    assert not ui.names_back(name)


@pytest.mark.parametrize("word", sorted(ui.NOT_BEFORE_BACK))
def test_every_word_that_makes_back_something_else(word):
    assert not ui.names_back(f"{word}BackButton") and not ui.names_back(f"{word}_back")


def _corner(label="", ident="", **fields) -> ui.Element:
    return button("e1", label, ident, rect=(16, 59, 90, 40), **fields)


def test_rf2_rf3_own_back_takes_no_call_back_and_no_back_camera():
    call = _corner("Call back", "callBackButton")
    assert ui.own_back([call], UPRIGHT) is None
    assert ui.own_back([_corner("Back camera")], UPRIGHT) is None
    assert ui.own_back([_corner("Back", "callBackButton")], UPRIGHT) is None  # (its id says it is something else)
    assert ui.own_back([_corner("Call back", "backButton")], UPRIGHT) is None
    mine = _corner("Back", "searchResultBackButton")
    assert ui.own_back([mine], UPRIGHT) is mine


def test_own_back_skips_a_control_something_is_drawn_over():
    bar = ui.Bar("Other", (0, 50, 430, 60), "other @0,50 430x60")
    assert ui.own_back([_corner("Back", covered_by=(bar,))], UPRIGHT) is None


def test_back_never_taps_a_corner_control_that_needs_a_persons_ok():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.screen = UPRIGHT
        await looked(d, [button("e1", "", "deleteBackButton", rect=(16, 59, 40, 40))])
        e = await refused(d, "back", {})
        assert e.code == "not_actionable" and str(e).startswith("no_action: the control in the corner")
        assert 'says "Delete"' in str(e) and d.session.tap_points == [] and "swipe" not in d.session.calls
        await finish(stop, task)
    run(go)


# ---- M2: `back` from its own whole read only ----

def test_rf4_back_doesnt_tap_an_older_lists_corner_button_when_its_own_read_lists_nothing():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.screen = UPRIGHT
        await looked(d, SEARCH)
        await d.run_tool("a", "tap", {"target": {"ref": "e2"}, "look": False})  # into a product page
        d.session.tree = []  # the product page reads as nothing; the agent's list is still the results'
        d.session.front_items = []
        d.session.tap_points.clear()
        try:  # (the fake screen doesn't change after the swipe: back may then say no_action)
            await d.run_tool("a", "back", {"look": False})
        except Refused as e:
            assert str(e).startswith("no_action: nothing changed after back")
        assert d.session.tap_points == [] and "swipe" in d.session.calls  # (the probe: (36, 79) was tapped)
        await finish(stop, task)
    run(go)


def test_back_on_a_cut_read_doesnt_look_for_its_own_button():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.screen = UPRIGHT
        await looked(d, SEARCH)
        d.session.tier = "shallow"  # back's own read is cut: "exactly one" can't be proven
        d.session.front_items = list(SEARCH)
        r = await d.run_tool("a", "back", {"look": False})
        assert d.session.tap_points == [] and r["text"].startswith("Swiped from the left edge. This screen's elements")
        await finish(stop, task)
    run(go)


# ---- M3: the tap beside a sheet ----

def _layer_back(front_items, layer, behind_items, change_on_tap=False):
    out = {}

    async def go():
        d = make()
        on_loop_clock(d)  # (frames on the virtual clock: the same on every machine)
        stop, task = await started(d)
        d.session.screen = UPRIGHT
        d.hub.publish(b"A")
        feeder = FrameFeeder(d.hub).start(then=b"A")
        await looked(d, front_items)
        d.session.behind = [(item, layer) for item in behind_items]
        d.session.front_items = list(front_items)
        real = d.session.tap

        async def tap(target, hold_ms=60):
            if change_on_tap:
                feeder.then = b"B"
                d.session.behind = []
            return await real(target, hold_ms)
        d.session.tap = tap
        try:
            r = await d.run_tool("a", "back", {})
            out["text"] = r["text"]
        except Refused as e:
            out["refused"] = str(e)
        out["taps"], out["swiped"] = list(d.session.tap_points), "swipe" in d.session.calls
        await feeder.stop()
        await finish(stop, task)
    run_virtual(go)
    return out


def test_a_header_strip_read_as_a_layer_gets_no_tap_and_no_pull():
    strip = el("e1", "other", "", "DKView_id", rect=(0, 64, 430, 54))
    rows = [button("e2", "Result one", "r1", rect=(0, 130, 430, 80)), button("e3", "Result two", "r2", rect=(0, 420, 430, 80))]
    out = _layer_back([strip, *rows], strip, [button("", "Sort", "sort", rect=(16, 130, 100, 40))])
    assert out["refused"].startswith("no_action: no back button, and what is in front is no sheet to close")
    assert out["taps"] == [] and not out["swiped"]


def test_a_real_bottom_sheet_is_closed_by_a_tap_on_what_it_dims():
    sheet = el("e1", "other", "", "lab-sheet", rect=(0, 647, 430, 285))
    inside = button("e2", "Option", "opt", rect=(20, 700, 390, 44))
    out = _layer_back([sheet, inside], sheet, [button("", "Row", "row", rect=(0, 300, 430, 80))], change_on_tap=True)
    assert out.get("text", "").startswith("Closed the sheet.") and len(out["taps"]) == 1
    x, y = out["taps"][0]
    assert y < 647  # above the sheet, on what it dims


def test_a_sheet_whose_only_free_area_holds_a_button_gets_no_tap_beside_it():
    sheet = el("e1", "other", "", "lab-sheet", rect=(0, 500, 430, 432))
    banner = button("e2", "Shop the sale", "promo", rect=(0, 100, 430, 380))  # where the tap beside would land
    out = _layer_back([banner, sheet, button("e3", "Option", "opt", rect=(20, 560, 390, 44))], sheet, [])
    assert out["taps"] == [] and out["swiped"]  # the pull from its top edge is still tried
    assert out["refused"].startswith("no_action: nothing changed after back")


def test_sheet_like_and_clear_of_touches():
    screen = (430.0, 932.0)
    assert not ui.sheet_like((0, 64, 430, 54), screen)  # a strip along the top
    assert ui.sheet_like((0, 647, 430, 285), screen)  # reaches the bottom
    assert ui.sheet_like((40, 250, 350, 400), screen)  # a centred dialog, 43 % tall
    box = (0, 647, 430, 285)
    backdrop = el("e9", "button", "Dismiss", rect=(0, 0, 430, 932))  # holds the whole sheet: a touch on it closes it
    near = button("e2", "Row", rect=(0, 300, 430, 50))
    assert ui.clear_of_touches((215, 400), box, [backdrop])
    assert not ui.clear_of_touches((215, 325), box, [near])  # inside it
    assert not ui.clear_of_touches((215, 355), box, [near])  # 5 pt from it
    assert ui.clear_of_touches((215, 360), box, [near])  # 10 pt from it


# ---- L2: a notification passing is no app coming forward ----

def test_a_banner_that_passed_during_a_read_is_named_as_one():
    from tapwright.daemon import Daemon
    app, over = Front(session_module.FRONT_APP, SHOP[1]), Front(session_module.FRONT_OVER, SHOP[1])
    other, home = Front(session_module.FRONT_APP, "com.example.chat"), Front(session_module.FRONT_HOME)
    assert Daemon._front_moved(app, over) == "banner" and Daemon._front_moved(over, app) == "banner"
    assert Daemon._front_moved(app, other) == "app" and Daemon._front_moved(app, home) == "app"
    assert Daemon._front_moved(app, app) == "" and Daemon._front_moved(None, app) == ""
    d = make()
    view = d.control.client("a")
    view.front_moved = "banner"
    assert d._over_line(view) == "Note: a notification passed over the app while this was read; look again."
    view.front_moved = "app"
    assert d._over_line(view) == "Note: another app came to the front while this was read; look again."


# ---- L1: the watchdog's lines pass the accounts' scrub untouched ----

def test_the_watchdogs_lines_are_not_scrubbed_on_its_thread():
    scrubbed = []

    def scrub(text):
        scrubbed.append(threading.current_thread().name)
        return text
    guard = server.RedactAccounts(scrub)

    def record(thread: str) -> logging.LogRecord:
        r = logging.LogRecord("tapwright", logging.INFO, __file__, 1, "held: daemon.py:10 in look", None, None)
        r.threadName = thread
        return r
    dog = record(loopdog.THREAD)
    assert guard.filter(dog) and dog.getMessage() == "held: daemon.py:10 in look" and scrubbed == []
    assert guard.filter(record("MainThread")) and scrubbed == [threading.current_thread().name]


def test_the_watchdog_thread_has_the_name_the_guard_knows():
    seen = []
    dog = loopdog.LoopDog(seen.append, every=0.01)
    try:
        dog.start()
        assert loopdog.THREAD in {t.name for t in threading.enumerate()}
    finally:
        dog.stop()
    asyncio.run(asyncio.sleep(0))

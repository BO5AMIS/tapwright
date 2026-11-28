"""(2.0.3, brief Q fix 1) `back` on a screen with no navigation bar's back button taps the screen's own back control in
the top-left corner when exactly one qualifies, and the layer path gives up after fewer reads."""
import pytest
from fakes import FrameFeeder, button, el
from test_daemon import finish, looked, make, on_loop_clock, run, run_virtual, started

from tapwright import ui
from tapwright.errors import Refused


@pytest.mark.parametrize("text", ["searchResultBackButton", "icBrutalBack", "back_button", "Back", "Go back", "BACK",
                                  "nav-back", "btnBack2", "BackButton", "go_back_arrow", "BACKButton"])
def test_names_back_as_a_word(text):
    assert ui.names_back(text)


@pytest.mark.parametrize("text", ["background", "BackgroundView", "feedback", "FeedbackButton", "playback", "Playback",
                                  "backup", "BackupNow", "backspace", "Backspace", "kickback", "BACKGROUND", "Backward",
                                  "Back to top", "back_to_top", "BlurIconFillButton_id", "", "Close"])
def test_names_back_never_inside_another_word(text):
    assert not ui.names_back(text)


UPRIGHT = (430.0, 932.0)


def _b(ref, label="", ident="", rect=(16, 59, 40, 40), **fields):
    return ui.Element(ref, "button", label, ident, "", rect, actions=("tap",), type="Button", **fields)


def test_own_back_the_one_in_the_corner():
    mine = _b("e3", "icBrutalBack", "searchResultBackButton")
    items = [el("e1", "text", "Results"), mine, _b("e4", "Filter", "filter", rect=(370, 59, 40, 40))]
    assert ui.own_back(items, UPRIGHT) is mine


def test_own_back_not_outside_the_corner():
    assert ui.own_back([_b("e3", "Back", rect=(16, 120, 40, 40))], UPRIGHT) is None  # ends at 160 down
    assert ui.own_back([_b("e3", "Back", rect=(100, 59, 40, 40))], UPRIGHT) is None  # ends at 140 across
    assert ui.own_back([_b("e3", "Back", rect=(16, 59, 40, 40))], UPRIGHT) is not None


def test_own_back_in_landscape_the_corner_is_wider_and_shorter():
    landscape = (932.0, 430.0)
    assert ui.own_back([_b("e3", "Back", rect=(78, 10, 44, 44))], landscape) is not None  # past the camera's side
    assert ui.own_back([_b("e3", "Back", rect=(60, 70, 44, 44))], landscape) is None  # ends at 114 down
    assert ui.own_back([_b("e3", "Back", rect=(60, 70, 44, 44))], UPRIGHT) is not None


def test_own_back_none_when_its_words_say_nothing_of_back():
    assert ui.own_back([_b("e3", "", "BlurIconFillButton_id")], UPRIGHT) is None


def test_own_back_none_when_two_qualify():
    items = [_b("e3", "Back", "first_back_button", rect=(10, 59, 40, 40)),
             _b("e4", "Back", "second_back_button", rect=(60, 59, 40, 40))]
    assert ui.own_back(items, UPRIGHT) is None


def test_own_back_one_thing_listed_twice_counts_once():
    wrapper = ui.Element("e3", "other", "", "searchResultBackButton", "", (16, 59, 40, 40), actions=("tap",))
    mine = _b("e4", "icBrutalBack", "searchResultBackButton")
    assert ui.own_back([wrapper, mine], UPRIGHT) is mine


def test_own_back_not_disabled_not_text_not_in_an_alert_not_hosted():
    assert ui.own_back([_b("e3", "Back", enabled=False)], UPRIGHT) is None
    assert ui.own_back([el("e3", "text", "Back", rect=(16, 59, 40, 40))], UPRIGHT) is None
    assert ui.own_back([_b("e3", "Back", in_alert=True)], UPRIGHT) is None
    assert ui.own_back([_b("e3", "Back", placed="moved")], UPRIGHT) is None


def test_own_back_an_element_with_the_tap_action_counts():
    other = ui.Element("e3", "other", "", "back_button", "", (16, 59, 40, 40), actions=("tap",))
    assert ui.own_back([other], UPRIGHT) is other


SEARCH = [button("e1", "icBrutalBack", "searchResultBackButton", rect=(16, 59, 40, 40)),
          button("e2", "Shoes", "result-1", rect=(16, 200, 398, 120))]


def test_back_taps_the_screens_own_back_button():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.screen = UPRIGHT
        await looked(d, SEARCH)
        r = await d.run_tool("a", "back", {})
        assert r["text"].startswith('Tapped the screen\'s back button: e1 button "icBrutalBack" id=searchResultBackButton.')
        assert d.session.tap_points == [(36.0, 79.0)] and "swipe" not in d.session.calls
        await finish(stop, task)
    run(go)


def test_back_dry_run_says_what_it_would_tap():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.screen = UPRIGHT
        await looked(d, SEARCH)
        r = await d.run_tool("a", "back", {"dry_run": True})
        assert "Would tap the screen's back button: e1 button \"icBrutalBack\"" in r["text"]
        assert d.session.tap_points == [] and "swipe" not in d.session.calls
        await finish(stop, task)
    run(go)


def test_back_prefers_the_navigation_bars_button():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.screen = UPRIGHT
        await looked(d, SEARCH)
        d.session.back_at = (48.0, 81.0)
        r = await d.run_tool("a", "back", {})
        assert r["text"].startswith("Tapped the back button.") and d.session.tap_points == [(48.0, 81.0)]
        await finish(stop, task)
    run(go)


def test_back_with_two_candidates_swipes_as_before():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.screen = UPRIGHT
        await looked(d, [button("e1", "Back", "first_back_button", rect=(10, 59, 40, 40)),
                         button("e2", "Back", "second_back_button", rect=(60, 59, 40, 40))])
        r = await d.run_tool("a", "back", {})
        assert r["text"].startswith("Swiped from the left edge.") and d.session.tap_points == []
        await finish(stop, task)
    run(go)


def test_back_taps_its_own_button_under_a_header_strip_read_as_a_layer():
    """The phone, 2026-11-26 (a shopping app): a header strip was read as a layer in front (`other id=DKView_id
    @0,64 430x54`), and `back` went the sheet's way: 15.7 s, four reads, a tap beside the strip, and no_action."""
    async def go():
        d = make()
        on_loop_clock(d)  # (the virtual clock: frames every 4 ms as written, on any machine's timer)
        stop, task = await started(d)
        d.session.screen = UPRIGHT
        d.hub.publish(b"A")
        feeder = FrameFeeder(d.hub).start(then=b"A")
        strip = el("e3", "other", "", "DKView_id", rect=(0, 64, 430, 54))
        await looked(d, [*SEARCH, strip])
        hidden = button("", "Sort", "sort", rect=(16, 130, 100, 40))
        d.session.behind = [(hidden, strip)]
        d.session.front_items = [*SEARCH, strip]
        real = d.session.tap

        async def tap(target, hold_ms=60):
            feeder.then = b"B"  # the search page goes, with the touch
            return await real(target, hold_ms)
        d.session.tap = tap
        r = await d.run_tool("a", "back", {})
        assert r["text"].startswith("Tapped the screen's back button: e1 button \"icBrutalBack\"")
        assert d.session.tap_points == [(36.0, 79.0)] and "swipe" not in d.session.calls
        await feeder.stop()
        await finish(stop, task)
    run_virtual(go)


def test_back_on_a_layer_it_cant_close_gives_up_after_one_read():
    """Nothing to tap, a layer in front, a still screen: one read for where to touch (the picture didn't move across
    it), and no read after each way out that changed nothing on the stream."""
    async def go():
        d = make()
        on_loop_clock(d)  # (the virtual clock: frames every 4 ms as written, on any machine's timer)
        stop, task = await started(d)
        d.session.screen = UPRIGHT
        d.hub.publish(b"A")
        feeder = FrameFeeder(d.hub).start(then=b"A")  # nothing ever changes on the screen
        sheet = el("e1", "other", "", "lab-sheet", rect=(0, 647, 430, 285))
        await looked(d, [sheet])
        d.session.behind = [(button("", "Tap me", "lab-taps", rect=(16, 197, 398, 120)), sheet)]
        d.session.front_items = [sheet]
        reads = []
        point = d.session.back_point

        async def counted():
            reads.append(1)
            return await point()
        d.session.back_point = counted
        refreshes = d.session.calls.count("refresh")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "back", {})
        assert str(e.value).startswith("no_action: nothing changed after back; e1 other id=lab-sheet")
        assert len(reads) == 1 and d.session.calls.count("refresh") == refreshes
        assert len(d.session.tap_points) == 1 and "swipe" in d.session.calls  # both ways out were still tried
        await feeder.stop()
        await finish(stop, task)
    run_virtual(go)


def test_back_on_a_moving_layer_reads_again_as_before():
    async def go():
        d = make()
        on_loop_clock(d)  # (the virtual clock: frames every 4 ms as written, on any machine's timer)
        stop, task = await started(d)
        d.session.screen = UPRIGHT
        d.hub.publish(b"A")
        feeder = FrameFeeder(d.hub).start(churn=True)  # the picture keeps changing: the read can't be trusted alone
        sheet = el("e1", "other", "", "lab-sheet", rect=(0, 647, 430, 285))
        await looked(d, [sheet])
        d.session.behind = [(button("", "Tap me", "lab-taps", rect=(16, 197, 398, 120)), sheet)]
        d.session.front_items = [sheet]
        reads = []
        point = d.session.back_point

        async def counted():
            reads.append(1)
            return await point()
        d.session.back_point = counted
        with pytest.raises(Refused):
            await d.run_tool("a", "back", {})
        assert len(reads) >= 2
        await feeder.stop()
        await finish(stop, task)
    run_virtual(go)

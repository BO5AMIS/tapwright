"""Fixes from Codex's third and fourth reviews of PR #6 (1.4.0): an indexed target that keeps its element through the
scroll (a row before it can leave the tree, a match can stay put outside the list, rows can be replaced), and an ended
agent that stays ended however many end after it."""
import pytest
from fakes import el
from test_daemon import finish, make, run, started

from tapwright.control import Refused
from tapwright.daemon import _followed


def _form(d, before, after):
    """A form in a scroll view. `before` and `after` are its elements, (label, y, identifier), at the start and once it
    has scrolled: those whose middle is on the 800 pt screen are in the tree, the rest off screen."""
    s = d.session
    view = el("e1", "scroll", "", "form", rect=(0, 0, 400, 800))

    def lay(rows):
        tree, off = [view], []
        for label, y, ident in rows:
            e = el("", "button", label, ident, rect=(10, y, 100, 40))
            (tree if 0 <= y + 20 < 800 else off).append(e)
        for n, e in enumerate(tree[1:], 2):
            e.ref = f"e{n}"
        s.tree, s.offscreen = tree, off
    lay(before)
    real = s.scroll

    async def scroll(x0, y0, x1, y1):
        lay(after)
        return await real(x0, y0, x1, y1)
    s.scroll = scroll


async def _tap(d, index):
    await d.run_tool("a", "look", {"screenshot": False})
    await d.run_tool("a", "tap", {"target": {"label": "Account", "index": index}, "look": False})
    return d.session.tap_points[-1]


# ---- cycle 3: a match before the target leaves the tree as the list scrolls ----
def test_an_indexed_tap_keeps_its_element_when_the_rows_before_it_leave_the_tree():
    async def go():
        d = make()
        stop, task = await started(d)
        # A at 100 (on screen), the Billing heading, B and C below; the form moves up by 700 and A leaves the tree
        _form(d, [("Account", 100, ""), ("Billing", 950, ""), ("Account", 1000, ""), ("Account", 1050, "")],
              [("Billing", 250, ""), ("Account", 300, ""), ("Account", 350, "")])
        assert (await _tap(d, 1))[1] == 320  # B (300 to 340), not C
        await finish(stop, task)
    run(go)


def test_an_indexed_tap_follows_its_element_by_its_own_identifier():
    async def go():
        d = make()
        stop, task = await started(d)
        # evenly spaced rows and nothing else that moves: B's identifier tells
        _form(d, [("Account", 100, ""), ("Account", 1000, "acct-b"), ("Account", 1050, "acct-c")],
              [("Account", 300, "acct-b"), ("Account", 350, "acct-c")])
        assert (await _tap(d, 1))[1] == 320
        await finish(stop, task)
    run(go)


# ---- cycle 4: a match that stays put outside the list ----
def test_an_indexed_tap_is_not_drawn_to_a_match_that_did_not_move():
    async def go():
        d = make()
        stop, task = await started(d)
        # A at 100 is outside the list and stays; the list moves up by 248: B at 752, C at 802 (off screen)
        _form(d, [("Account", 100, ""), ("Billing", 950, ""), ("Account", 1000, ""), ("Account", 1050, "")],
              [("Account", 100, ""), ("Billing", 702, ""), ("Account", 752, ""), ("Account", 802, "")])
        assert (await _tap(d, 1))[1] == 772  # B, not A at 120
        await finish(stop, task)
    run(go)


# ---- nothing tells: refused, nothing touched ----
@pytest.mark.parametrize("after", [
    # rows only, no other element: the shift can't be measured (rows can be replaced as a list scrolls)
    [("Account", 300, ""), ("Account", 350, ""), ("Account", 400, "")],
    # the elements that moved disagree (the list was laid out again)
    [("Billing", 250, ""), ("Totals", 420, ""), ("Account", 300, ""), ("Account", 350, "")],
])
def test_an_indexed_tap_that_cant_tell_its_element_after_the_scroll_touches_nothing(after):
    async def go():
        d = make()
        stop, task = await started(d)
        _form(d, [("Account", 100, ""), ("Billing", 950, ""), ("Account", 1000, ""), ("Account", 1050, ""),
                  ("Totals", 1100, "")], after)
        await d.run_tool("a", "look", {"screenshot": False})
        taps = len(d.session.tap_points)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"label": "Account", "index": 1}, "look": False})
        assert e.value.code == "stale" and "wasn't touched" in str(e.value)
        assert len(d.session.tap_points) == taps
        await finish(stop, task)
    run(go)


def test_replaced_rows_of_the_same_count_are_not_taken_for_the_target():
    """Codex, cycle 4: before A B C D at 100..250, after B C D E at 100..250 (shift -50). Rows only: refused, where
    matching the rows one for one picked C."""
    rows = lambda ys: [el(f"e{i}", "button", "Account", "", rect=(10, y, 100, 40)) for i, y in enumerate(ys)]
    before, after = rows((100, 150, 200, 250)), rows((100, 150, 200, 250))
    assert _followed(before[1], before, after, after, after) is None
    # with a heading that moved by -50 too, B is the row at 100
    head0, head1 = el("", "text", "Plans", rect=(10, 60, 100, 30)), el("", "text", "Plans", rect=(10, 10, 100, 30))
    assert _followed(before[1], [head0, *before], [head1, *after], after, after) is after[0]


def test_a_horizontal_list_is_followed_by_its_shift_across():
    cards = lambda xs: [el(f"e{i}", "button", "Open", "", rect=(x, 300, 120, 80)) for i, x in enumerate(xs)]
    before, after = cards((10, 500, 640)), cards((10, 160, 300))  # the first card is fixed; the rest moved by -340
    tag0, tag1 = el("", "text", "Deals", rect=(470, 260, 80, 20)), el("", "text", "Deals", rect=(130, 260, 80, 20))
    assert _followed(before[1], [tag0, *before], [tag1, *after], after, after) is after[1]


# ---- cycle 5: an element in another pane that scrolls by another amount says nothing ----
@pytest.mark.parametrize("heading", [False, True])
def test_an_indexed_tap_measures_the_shift_only_inside_its_own_scroll_view(heading):
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        left = el("e1", "scroll", "", "list", rect=(0, 200, 190, 600))
        right = el("e2", "scroll", "", "pane", rect=(200, 200, 200, 600))
        acct = lambda y: el("", "button", "Account", "", rect=(10, y, 100, 40))
        side = lambda y: el("", "text", "Sidebar", "", rect=(210, y, 100, 30))
        head = lambda y: el("", "text", "Billing", "", rect=(10, y, 100, 30))
        s.tree = [left, right, acct(100), side(600)]
        s.offscreen = [acct(950), acct(1000), *([head(900)] if heading else [])]
        real = s.scroll

        async def scroll(x0, y0, x1, y1):  # the list moves by -248, the linked pane by -298
            s.tree = [left, right, acct(100), side(302), acct(702), acct(752), *([head(652)] if heading else [])]
            s.offscreen = []
            for n, e in enumerate(s.tree, 1):
                e.ref = f"e{n}"
            return await real(x0, y0, x1, y1)
        s.scroll = scroll
        for n, e in enumerate(s.tree, 1):
            e.ref = f"e{n}"
        await d.run_tool("a", "look", {"screenshot": False})
        taps = len(s.tap_points)
        if heading:  # the heading moved with the list: C, at 752
            await d.run_tool("a", "tap", {"target": {"label": "Account", "index": 2}, "look": False})
            assert s.tap_points[-1][1] == 772
        else:  # only the pane's Sidebar moved: it doesn't tell, so nothing is touched (it said B, at 702)
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "tap", {"target": {"label": "Account", "index": 2}, "look": False})
            assert e.value.code == "stale" and len(s.tap_points) == taps
        await finish(stop, task)
    run(go)


# ---- cycle 6: an element past the screen in a column two panes share proves nothing ----
def test_an_off_screen_element_of_a_stacked_pane_does_not_measure_the_shift():
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        top = el("e1", "scroll", "", "pane", rect=(0, 0, 400, 150))
        lst = el("e2", "scroll", "", "list", rect=(0, 150, 400, 650))
        acct = lambda y: el("", "button", "Account", "", rect=(10, y, 100, 40))
        side = lambda y: el("", "text", "Sidebar", "", rect=(10, y, 100, 30))
        s.tree, s.offscreen = [top, lst], [acct(950), acct(1000), side(1500)]
        real = s.scroll

        async def scroll(x0, y0, x1, y1):  # the list moves by -248, the pane above by -298: Sidebar stays below
            s.tree = [top, lst, acct(702), acct(752)]
            s.tree[2].ref, s.tree[3].ref = "e3", "e4"
            s.offscreen = [side(1202)]
            return await real(x0, y0, x1, y1)
        s.scroll = scroll
        await d.run_tool("a", "look", {"screenshot": False})
        taps = len(s.tap_points)
        with pytest.raises(Refused) as e:  # it tapped B at 722 when Sidebar counted
            await d.run_tool("a", "tap", {"target": {"label": "Account", "index": 1}, "look": False})
        assert e.value.code == "stale" and len(s.tap_points) == taps
        await finish(stop, task)
    run(go)


# ---- cycle 3: an ended agent is not forgotten after others end ----
def test_an_ended_agent_stays_ended_after_hundreds_of_others_end():
    async def go():
        d = make()
        stop, task = await started(d)
        d.agent_alive("a")
        await d.agent_ended("a")
        for n in range(300):
            await d.agent_ended(f"other-{n}")
        with pytest.raises(Refused):
            await d.run_tool("a", "network", {"mode": "3g"})  # its call arrives late
        assert d._network is None and d.session.device.network_calls == []
        d.agent_alive("a")
        assert "a" not in d._agent_alive
        await finish(stop, task)
    run(go)

"""Fixes from Codex's third review of PR #6 (1.4.0): an indexed target that keeps its element when a row before it
leaves the tree during the scroll, and an ended agent that stays ended however many end after it."""
import pytest
from fakes import el
from test_daemon import finish, make, run, started

from tapwright.control import Refused


def _accounts(d, before, after, after_off=()):
    """A form holding Account rows: `before` and `after` are (y, identifier) of the rows off screen at the start and of
    those on screen once it has scrolled (`after_off`: still off screen then)."""
    s = d.session
    s.tree = [el("e1", "scroll", "", "form", rect=(0, 0, 400, 800)),
              el("e2", "button", "Account", "", rect=(10, 100, 100, 40))]
    s.offscreen = [el("", "button", "Account", ident, rect=(10, y, 100, 40)) for y, ident in before]
    real = s.scroll

    async def scroll(x0, y0, x1, y1):
        s.tree = [s.tree[0], *(el(f"e{3 + i}", "button", "Account", ident, rect=(10, y, 100, 40))
                               for i, (y, ident) in enumerate(after))]
        s.offscreen = [el("", "button", "Account", ident, rect=(10, y, 100, 40)) for y, ident in after_off]
        return await real(x0, y0, x1, y1)
    s.scroll = scroll


# ---- P2: a match before the target leaves the tree as the list scrolls ----
def test_an_indexed_tap_keeps_its_element_when_the_rows_before_it_leave_the_tree():
    async def go():
        d = make()
        stop, task = await started(d)
        # A at 100 (on screen), B at 1000, C at 1050; after the scroll B and C are at 300 and 350, and A is gone
        _accounts(d, before=[(1000, ""), (1050, "")], after=[(300, ""), (350, "")])
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "tap", {"target": {"label": "Account", "index": 1}, "look": False})
        assert d.session.tap_points[-1][1] == 320  # B (300 to 340), not C (350 to 390)
        await finish(stop, task)
    run(go)


def test_an_indexed_tap_follows_its_element_by_its_own_identifier():
    async def go():
        d = make()
        stop, task = await started(d)
        # evenly spaced rows: where they lie can't tell them apart, but B's identifier can
        _accounts(d, before=[(1000, "acct-b"), (1050, "acct-c"), (1100, "acct-d")],
                  after=[(300, "acct-b"), (350, "acct-c"), (400, "acct-d")])
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "tap", {"target": {"label": "Account", "index": 1}, "look": False})
        assert d.session.tap_points[-1][1] == 320  # acct-b
        await finish(stop, task)
    run(go)


def test_an_indexed_tap_that_cant_tell_its_element_after_the_scroll_touches_nothing():
    async def go():
        d = make()
        stop, task = await started(d)
        # A at 100, B at 1000, C at 1050; after the scroll A is gone and the rows were laid out again (300 and 330):
        # no shift puts them where the first look saw them, so the element can't be told
        _accounts(d, before=[(1000, ""), (1050, "")], after=[(300, ""), (330, "")])
        await d.run_tool("a", "look", {"screenshot": False})
        taps = len(d.session.tap_points)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"label": "Account", "index": 1}, "look": False})
        assert e.value.code == "stale" and "nothing was touched" in str(e.value)
        assert len(d.session.tap_points) == taps
        await finish(stop, task)
    run(go)


def test_evenly_spaced_rows_with_one_dropped_can_not_be_told_apart():
    from tapwright.daemon import _lined_up
    rows = [el("", "button", "Account", "", rect=(10, y, 100, 40)) for y in (100, 150, 200, 250)]
    now = [el(f"e{i}", "button", "Account", "", rect=(10, y, 100, 40)) for i, y in enumerate((100, 150, 200))]
    # the first row has gone and every row moved up by 50, or none did and the last has gone: both fit
    assert _lined_up(rows[1], rows, now, now) is None
    # rows spaced unevenly: only one shift fits
    rows = [el("", "button", "Account", "", rect=(10, y, 100, 40)) for y in (100, 150, 230, 250)]
    now = [el(f"e{i}", "button", "Account", "", rect=(10, y, 100, 40)) for i, y in enumerate((100, 180, 200))]
    assert _lined_up(rows[1], rows, now, now) is now[0]


# ---- P2: an ended agent is not forgotten after others end ----
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

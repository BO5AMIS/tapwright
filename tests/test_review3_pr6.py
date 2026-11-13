"""Fixes from Codex's reviews of PR #6 (1.4.0), cycles 3 to 8: an indexed off-screen target is followed through the
scroll by its own identifier, and without one it is scrolled into view and not touched (where the matches lie after a
scroll can't prove which one it is: rows drop, get replaced, repeat, and other elements move on their own); and an
ended agent stays ended however many end after it."""
import pytest
from fakes import el
from test_daemon import finish, make, run, started

from tapwright.control import Refused


def _screen(d, before, after):
    """Elements as (role, label, identifier, rect), at the start and once the drag has happened: those whose middle
    is on the 800 pt screen are in the tree (with refs), the rest off screen."""
    s = d.session

    def lay(items):
        tree, off = [], []
        for role, label, ident, rect in items:
            e = el("", role, label, ident, rect=rect)
            cx, cy = e.center
            (tree if 0 <= cx < 400 and 0 <= cy < 800 else off).append(e)
        for n, e in enumerate(tree, 1):
            e.ref = f"e{n}"
        s.tree, s.offscreen = tree, off
    lay(before)
    real = s.scroll

    async def scroll(x0, y0, x1, y1):
        lay(after)
        return await real(x0, y0, x1, y1)
    s.scroll = scroll


def acct(y, ident=""):
    return ("button", "Account", ident, (10, y, 100, 40))


def text(label, x, y):
    return ("text", label, "", (x, y, 100, 30))


FORM = ("scroll", "", "", (0, 0, 400, 800))
LIST = ("scroll", "", "", (0, 400, 400, 400))

# The inputs of Codex's cycles 3 to 8, each with the matches it named: without identifiers, none can be followed
NO_IDS = {
    "a row before it leaves the tree (3)": (
        [FORM, acct(100), text("Billing", 10, 950), acct(1000), acct(1050)],
        [FORM, text("Billing", 10, 250), acct(300), acct(350)], 1),
    "a fixed match outside the list (4)": (
        [FORM, acct(100), text("Billing", 10, 950), acct(1000), acct(1050)],
        [FORM, acct(100), text("Billing", 10, 702), acct(752), acct(802)], 1),
    "a pane beside it moves by another amount (5)": (
        [("scroll", "", "", (0, 200, 190, 600)), ("scroll", "", "", (200, 200, 200, 600)), acct(100),
         text("Sidebar", 210, 600), acct(950), acct(1000)],
        [("scroll", "", "", (0, 200, 190, 600)), ("scroll", "", "", (200, 200, 200, 600)), acct(100),
         text("Sidebar", 210, 302), acct(702), acct(752)], 2),
    "a pane stacked above it (6)": (
        [("scroll", "", "", (0, 0, 400, 150)), ("scroll", "", "", (0, 150, 400, 650)), acct(950), acct(1000),
         text("Sidebar", 10, 1500)],
        [("scroll", "", "", (0, 0, 400, 150)), ("scroll", "", "", (0, 150, 400, 650)), acct(702), acct(752),
         text("Sidebar", 10, 1202)], 1),
    "a header outside it animates (7)": (
        [LIST, text("Header", 10, 350), acct(950), acct(1000)],
        [LIST, text("Header", 10, 52), acct(702), acct(752)], 1),
    "repeated rows and a header inside it that animates (8)": (
        [LIST, text("Header", 250, 725), *(acct(y) for y in (650, 700, 750, 800, 850, 900, 950, 1000))],
        [LIST, text("Header", 250, 427), *(acct(y) for y in (402, 452, 502, 552, 602, 652, 702, 752))], 7),
}


@pytest.mark.parametrize("case", list(NO_IDS))
def test_an_indexed_off_screen_target_without_an_identifier_is_scrolled_into_view_and_not_touched(case):
    before, after, index = NO_IDS[case]

    async def go():
        d = make()
        stop, task = await started(d)
        _screen(d, before, after)
        await d.run_tool("a", "look", {"screenshot": False})
        taps = len(d.session.tap_points)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"label": "Account", "index": index}, "look": False})
        assert e.value.code == "stale" and "wasn't touched" in str(e.value) and "by its ref" in str(e.value)
        assert len(d.session.tap_points) == taps
        await finish(stop, task)
    run(go)


# The same inputs with an identifier on each row: the target follows its own and is touched where it is now
WITH_IDS = {
    "a row before it leaves the tree": (
        [FORM, acct(100, "a"), acct(1000, "b"), acct(1050, "c")],
        [FORM, acct(300, "b"), acct(350, "c")], 1, 320),
    "a fixed match outside the list": (
        [FORM, acct(100, "a"), acct(1000, "b"), acct(1050, "c")],
        [FORM, acct(100, "a"), acct(752, "b"), acct(802, "c")], 1, 772),
    "repeated rows and a header that animates": (
        [LIST, text("Header", 250, 725), *(acct(y, f"r{i}") for i, y in enumerate(range(650, 1001, 50)))],
        [LIST, text("Header", 250, 427), *(acct(y, f"r{i}") for i, y in enumerate(range(402, 753, 50)))], 7, 772),
}


@pytest.mark.parametrize("case", list(WITH_IDS))
def test_an_indexed_off_screen_target_follows_its_own_identifier_through_the_scroll(case):
    before, after, index, y = WITH_IDS[case]

    async def go():
        d = make()
        stop, task = await started(d)
        _screen(d, before, after)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "tap", {"target": {"label": "Account", "index": index}, "look": False})
        assert d.session.tap_points[-1][1] == y
        await finish(stop, task)
    run(go)


def test_an_identifier_another_element_shares_is_not_followed():
    async def go():
        d = make()
        stop, task = await started(d)
        _screen(d, [FORM, acct(100, "row"), acct(1000, "row"), acct(1050, "c")],
                [FORM, acct(300, "row"), acct(350, "c")])
        await d.run_tool("a", "look", {"screenshot": False})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"label": "Account", "index": 1}, "look": False})
        assert e.value.code == "stale"
        await finish(stop, task)
    run(go)


# ---- cycle 9: a followed identifier that is gone after the scroll never falls back to a raw name ----
def _gone(role):
    """A at 100 (identifier a), B at 1000 (identifier profile), C at 1050 (its label, so its raw name, is profile, and
    it has no identifier). The form moves up by 700, and an app refresh drops B: C is on screen at 350."""
    def item(label, ident, rect):
        return (role, label, ident, rect)
    before = [FORM, item("Account", "a", (10, 100, 100, 40)), item("Account", "profile", (10, 1000, 100, 40)),
              item("profile", "", (10, 1050, 100, 40))]
    after = [FORM, item("Account", "a", (10, -600, 100, 40)), item("profile", "", (10, 350, 100, 40))]
    return before, after


@pytest.mark.parametrize("call", [
    ("tap", {"target": {"label": "Account", "index": 1}}),
    ("tap", {"target": {"id": "profile"}}),
    ("type_text", {"target": {"label": "Account", "index": 1}, "text": "x"}),
])
def test_an_element_followed_by_its_identifier_that_is_gone_after_the_scroll_is_not_replaced(call):
    tool, args = call

    async def go():
        d = make()
        stop, task = await started(d)
        _screen(d, *_gone("input" if tool == "type_text" else "button"))
        for e in [*d.session.tree, *d.session.offscreen]:
            e.name = e.identifier or e.label  # WebDriverAgent's raw name: the identifier, else the label
        real_scroll = d.session.scroll

        async def scroll(*a):
            r = await real_scroll(*a)
            for e in [*d.session.tree, *d.session.offscreen]:
                e.name = e.identifier or e.label
            return r
        d.session.scroll = scroll
        await d.run_tool("a", "look", {"screenshot": False})
        taps, typed = len(d.session.tap_points), len(d.session.typed)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", tool, {**args, "look": False})
        assert e.value.code == "stale" and "nothing was touched" in str(e.value)
        assert len(d.session.tap_points) == taps and len(d.session.typed) == typed
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

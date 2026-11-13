"""AGENT-TOOLS §16.2: a touch on a target that is in the tree but off screen scrolls it into view first; `not_found`
names the closest matches."""
import re

import pytest

from tapwright import closest, daemon, policy, ui
from tapwright.control import Refused
from fakes import button, el
from test_daemon import LongList, finish, make, run, started


class RoleList(LongList):
    """LongList whose rows are of one role, so an input or a switch can be off screen."""

    def __init__(self, d, role: str, **more) -> None:
        self.role = role
        super().__init__(d, **more)

    def lay(self) -> None:
        super().lay()
        for e in [*self.s.tree[1:], *self.s.offscreen]:
            e.role, e.actions = self.role, ui.ROLE_ACTIONS.get(self.role, ())
            if self.role == "switch":
                e.checked = False


async def world(d, **more):
    stop, task = await started(d)
    rows = LongList(d, **more)
    await d.run_tool("a", "look", {"screenshot": False})
    return stop, task, rows


def test_a_tap_on_a_row_off_screen_scrolls_to_it_then_taps_in_one_call():
    async def go():
        d = make()
        stop, task, _ = await world(d)
        r = await d.run_tool("a", "tap", {"target": {"id": "row-30"}})
        first = r["text"].splitlines()[0]
        assert re.fullmatch(r'Scrolled to it, then tapped e\d+ button "Row 30" id=row-30\.', first), first
        assert len(d.session.scrolls) == 4 and "tap" in d.session.calls
        row = next(e for e in d.session.tree if e.identifier == "row-30")
        assert 60 <= row.y and row.y + row.height <= 760  # clear of the bars
        await finish(stop, task)
    run(go)


def test_an_element_already_on_screen_is_tapped_without_a_scroll():
    async def go():
        d = make()
        stop, task, _ = await world(d)
        r = await d.run_tool("a", "tap", {"target": {"id": "row-2"}, "look": False})
        assert r["text"].startswith("Tapped e") and d.session.scrolls == []
        await finish(stop, task)
    run(go)


def test_long_press_scrolls_and_says_so_in_its_own_verb():
    async def go():
        d = make()
        stop, task, _ = await world(d)
        r = await d.run_tool("a", "long_press", {"target": {"label": "Row 25"}, "look": False})
        assert r["text"].startswith("Scrolled to it, then ") and 'id=row-25' in r["text"].splitlines()[0]
        await finish(stop, task)
    run(go)


def test_type_text_scrolls_to_an_input_and_toggle_to_a_switch():
    async def go():
        d = make()
        stop, task = await started(d)
        RoleList(d, "input")
        await d.run_tool("a", "look", {"screenshot": False})
        r = await d.run_tool("a", "type_text", {"target": {"id": "row-30"}, "text": "hello", "look": False})
        assert r["text"].startswith("Scrolled to it, then typed into e") and d.session.scrolls
        await finish(stop, task)
        d = make()
        stop, task = await started(d)
        RoleList(d, "switch")
        await d.run_tool("a", "look", {"screenshot": False})
        r = await d.run_tool("a", "toggle", {"target": {"id": "row-30"}, "look": False})
        assert r["text"].startswith("Scrolled to it, then tapped e") and 'switch "Row 30"' in r["text"].splitlines()[0]
        await finish(stop, task)
    run(go)


def test_a_dry_run_says_what_it_would_do_and_moves_nothing():
    async def go():
        d = make()
        stop, task, _ = await world(d)
        r = await d.run_tool("a", "tap", {"target": {"id": "row-30"}, "dry_run": True})
        said = r["text"]
        assert re.fullmatch(r'Would scroll to it, then tap button "Row 30" id=row-30 at 200,\d+\.', said), said
        y = int(said.rsplit(",", 1)[1].rstrip("."))
        assert 60 <= y <= 760  # where it expects the element after the scroll
        t = await d.run_tool("a", "type_text", {"target": {"id": "row-31"}, "text": "abc", "dry_run": True})
        assert t["text"] == 'Would scroll to it, then type 3 characters into button "Row 31" id=row-31.'
        assert d.session.scrolls == [] and "tap" not in d.session.calls
        await finish(stop, task)
    run(go)


def test_the_policy_has_its_say_before_the_scroll_and_an_allow_is_used_once(monkeypatch):
    monkeypatch.setattr(daemon.secrets, "token_hex", lambda n=4: "x")

    async def go():
        d = make()
        d._queues_seen = d.subscribe()
        stop, task = await started(d)
        LongList(d, labels=lambda i: "Delete trip" if i == 30 else f"Row {i}")
        await d.run_tool("a", "look", {"screenshot": False})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "row-30"}})
        assert e.value.code == "not_allowed" and "needs_confirmation" in str(e.value)
        assert d.session.scrolls == [] and "tap" not in d.session.calls  # nothing moved
        dry = await d.run_tool("a", "tap", {"target": {"id": "row-30"}, "dry_run": True})
        assert dry["text"].endswith('It needs a person\'s OK (policy word "Delete").') and d.session.scrolls == []
        await d.viewer_message({"type": "confirm_answer", "id": "c1-x", "allow": True})
        r = await d.run_tool("a", "tap", {"target": {"id": "row-30"}, "look": False})
        assert r["text"].startswith("Scrolled to it, then tapped e") and r["text"].endswith("Note: a person allowed this.")
        assert d.session.scrolls and "tap" in d.session.calls
        await finish(stop, task)
    run(go)


def test_a_block_word_refuses_before_any_scroll():
    async def go():
        d = make()
        stop, task = await started(d)
        LongList(d, labels=lambda i: "Delete account" if i == 30 else f"Row {i}")
        await d.run_tool("a", "look", {"screenshot": False})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "row-30"}}, policy=policy.parse({"block": ["Delete account"]}))
        assert str(e.value).startswith("blocked:") and d.session.scrolls == []
        await finish(stop, task)
    run(go)


def test_when_no_scroll_can_bring_it_on_screen_the_refusal_says_why():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.screen = (430.0, 932.0)
        d.session.tree = [el("e1", "scroll", "", "list", rect=(0, 0, 430, 932))]
        d.session.offscreen = [button("", "Stuck", "stuck", rect=(16, 1500, 398, 48))]  # the content never moves
        await d.run_tool("a", "look", {"screenshot": False})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "stuck"}})
        assert e.value.code == "not_actionable" and str(e.value).startswith('off_screen: button "Stuck" id=stuck')
        assert "scrolling didn't bring it on screen" in str(e.value)
        await finish(stop, task)
    run(go)


def test_a_lazy_row_that_is_not_in_the_tree_stays_not_found():
    async def go():
        d = make()
        stop, task, _ = await world(d, rows=40)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "row-99"}})
        assert e.value.code == "not_found" and d.session.scrolls == []
        await finish(stop, task)
    run(go)


# ---- the closest matches ----
def items(*specs):
    return [el(ref, role, label, ident, rect=(10, 100 + 50 * n, 100, 40)) for n, (ref, role, label, ident) in enumerate(specs)]


@pytest.mark.parametrize("query, candidate", [
    ("save", "Save"), ("Draft save", "save draft"), ("Save", "Save draft"), ("Save draft", "Save"),
    ("Setings", "Settings"), ("Sign in", "Sign-in"), ("Create an account please", "Create an acount please")])
def test_close_means_case_order_containment_or_a_small_edit_distance(query, candidate):
    assert closest.rank(query, candidate) is not None


@pytest.mark.parametrize("query, candidate", [
    ("Save", "Cancel"), ("the", "The end"), ("Back to top", "to"), ("a", "A"), ("Log", "Sign out"),
    ("Settings", "Setup"), ("ok", "go"), ("A very long label here", "Another long thing"), ("", "Save")])
def test_what_is_not_close_or_only_shares_a_common_word_is_not_a_match(query, candidate):
    assert closest.rank(query, candidate) is None


def test_the_edit_distance_is_two_up_to_ten_characters_and_three_above():
    assert closest.rank("Notif", "Notifications") is not None  # contained
    assert closest.rank("abcdefgh", "abcdefxy") is not None  # 2 of 8
    assert closest.rank("abcdefgh", "abcdxyzw") is None  # 4
    assert closest.rank("abcdefghijkl", "abcdefghixyz") is not None  # 3 of 12
    assert closest.rank("abcdefghijkl", "abcdefgwxyz!") is None  # 5


def test_find_lists_up_to_three_with_refs_and_where_the_off_screen_ones_are():
    on = items(("e12", "button", "Save draft", "save-draft"), ("e13", "button", "Cancel", "cancel"))
    off = [el("", "text", "Saved", "", rect=(10, 1500, 100, 20)), el("", "text", "Savings", "", rect=(10, -300, 100, 20)),
           el("", "text", "Save all", "", rect=(10, 1600, 100, 20)), el("", "text", "Save everything", "", rect=(10, 1700, 100, 20))]
    line = closest.find(ui.Target(label="Save"), on, off, (400.0, 800.0))
    # nearest first (here all contain "Save"): on screen before off, then top to bottom; three at most
    assert line == ('Closest: e12 button "Save draft" id=save-draft, text "Saved" (off screen, below), '
                    'text "Save all" (off screen, below).')
    above = closest.find(ui.Target(label="Savings"), [], [el("", "text", "Saving", "", rect=(10, -300, 100, 20))], (400.0, 800.0))
    assert above == 'Closest: text "Saving" (off screen, above).'
    assert closest.find(ui.Target(text="Zebra"), on, off, (400.0, 800.0)) == ""
    assert closest.find(ui.Target(ref="e1"), on, off, (400.0, 800.0)) == ""
    assert closest.find(ui.Target(x=1.0, y=2.0), on, off, (400.0, 800.0)) == ""
    # an id is compared with identifiers
    assert closest.find(ui.Target(id="save_draft"), on, off, (400.0, 800.0)).startswith('Closest: e12 button "Save draft"')


def test_a_not_found_carries_the_closest_matches_and_none_when_nothing_is_close():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.tree = [button("e1", "Save draft", "save-draft"), button("e2", "Cancel", "cancel", rect=(10, 200, 100, 40))]
        d.session.offscreen = [el("", "text", "Saved", "", rect=(10, 1500, 100, 20))]
        await d.run_tool("a", "look", {"screenshot": False})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"label": "Save dra"}})  # contained in "Save draft"
        assert e.value.code == "not_found" and "\nClosest: e1 button \"Save draft\" id=save-draft" in str(e.value)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"label": "Savd"}})
        assert str(e.value).endswith('Closest: e1 button "Save draft" id=save-draft, text "Saved" (off screen, below).') \
            or 'text "Saved" (off screen, below)' in str(e.value)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"text": "Zebra"}})
        assert e.value.code == "not_found" and "Closest" not in str(e.value)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"text": "Cancel", "index": 5}})
        assert "out of range" in str(e.value) and "Closest" not in str(e.value)
        await finish(stop, task)
    run(go)

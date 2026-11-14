"""The fork's route version 5 (tw-16.15.3.5, Tapwright 1.7 part 7): the compact tree's keyboard focus (`f`, read into
Element.focused), the hit test before a touch on an element (GET /wda/tw/hit; QUALITY-BAR 2.5 R3, `lab-behind-bar`),
and typing into a field the runner says has focus without tapping it again (part 10: no caret menu)."""
import asyncio
import dataclasses

import pytest
from fakes import button
from test_compact_read import Runner as V4Runner
from test_daemon import finish, looked, make, run, started

from tapwright import session as session_module
from tapwright import store, ui
from tapwright.control import Refused
from tapwright.device import ConnectionLost, DeviceError
from tapwright.session import Session


def node(t, r, c=(), **keys) -> dict:
    out = {"t": t, "r": list(r), **keys}
    if c:
        out["c"] = list(c)
    return out


# A login screen with a custom bar drawn over the bottom of a list: the bar is a sibling of the list, as `lab-behind-bar`
EMAIL = node("TextField", (20, 120, 390, 44), n="email", l="Email", v="", f=1)
PASSWORD = node("SecureTextField", (20, 180, 390, 44), n="password", l="Password", v="")
ROW_TEXT = node("StaticText", (36, 780, 200, 20), n="Terms", l="Terms")
ROW = node("Cell", (20, 770, 390, 44), [ROW_TEXT], n="terms-row", l="Terms")
LIST = node("Table", (0, 100, 430, 760), [EMAIL, PASSWORD, ROW], n="form")
BAR_BUTTON = node("Button", (20, 772, 390, 48), n="checkout", l="Checkout")
BAR = node("Other", (0, 760, 430, 120), [BAR_BUTTON], n="sticky-bar")
WINDOW = node("Window", (0, 0, 430, 932), [node("Other", (0, 0, 430, 932), [LIST, BAR])])
TREE = node("Application", (0, 0, 430, 932), [WINDOW], n="Lab", l="Lab", pid=42, b="com.example.lab")
SCREEN = (430.0, 932.0)


def source(tree=TREE) -> str:
    return ui.xml_of_tree(tree)


def listed(tree=TREE) -> list[ui.Element]:
    return ui.read(source(tree), SCREEN).items


def find(items, name) -> ui.Element:
    return next(e for e in items if e.name == name)


# ---- the compact tree's `f` ----

def test_f_is_written_as_the_focused_attribute_and_read_into_focused():
    xml = source()
    assert xml.count(f'{ui.FOCUSED_ATTR}="true"') == 1 and 'name="email"' in xml.split('focused="true"')[0].splitlines()[-1]
    items = listed()
    assert find(items, "email").focused is True
    assert [e.name for e in items if e.focused] == ["email"]


def test_a_focus_inside_an_input_is_the_inputs_and_none_is_none():
    inner = node("StaticText", (20, 300, 390, 80), l="Hello", f=1)
    view = node("TextView", (20, 300, 390, 80), [inner], n="notes", v="Hello")
    tree = node("Application", (0, 0, 430, 932), [node("Window", (0, 0, 430, 932), [view])], pid=1)
    assert find(listed(tree), "notes").focused is True
    plain = node("Application", (0, 0, 430, 932), [node("Window", (0, 0, 430, 932), [dict(EMAIL, f=0), PASSWORD])], pid=1)
    assert not any(e.focused for e in listed(plain))
    truthy = node("Application", (0, 0, 430, 932), [node("Window", (0, 0, 430, 932), [dict(EMAIL, f=True)])], pid=1)
    assert 'focused=' not in source(truthy)  # only the number 1 is the runner's "f"


def test_sources_own_xml_has_no_focus():
    xml = source().replace(' focused="true"', "")
    assert not any(e.focused for e in ui.read(xml, SCREEN).items)


# ---- the hit test's relation ----

def hit(n: dict, bundle: str = "com.example.lab") -> ui.Hit:
    value = {"t": n["t"], "r": n["r"], "pid": 42, "b": bundle, "via": "element", "ms": {"hit": 9, "read": 14}}
    for k in ("n", "l"):
        if k in n:
            value[k] = n[k]
    got = ui.hit_of(value)
    assert got is not None
    return got


def test_the_hit_on_the_target_inside_it_or_holding_it_passes_and_a_bar_over_it_doesnt():
    items, xml = listed(), source()
    row = find(items, "terms-row")
    assert ui.hit_relation(xml, row, hit(ROW)) == "same"
    assert ui.hit_relation(xml, row, hit(ROW_TEXT)) == "inside"
    assert ui.hit_relation(xml, find(items, "email"), hit(LIST)) == "ancestor"  # nothing deeper there: nothing over it
    assert ui.hit_relation(xml, row, hit(BAR_BUTTON)) == "other"
    assert ui.hit_relation(xml, row, hit(BAR)) == "other"


def test_without_the_tree_the_frames_prove_nothing_inside():
    row = find(listed(), "terms-row")
    assert ui.hit_relation(None, row, hit(ROW_TEXT)) == "unplaced"  # (proves nothing: Codex, PR #12 c1, M1)
    assert ui.hit_relation(None, row, hit(BAR_BUTTON)) == "other"
    assert ui.hit_relation("<not xml", row, hit(ROW)) == "same"


@pytest.mark.parametrize("bad", [None, [], {}, {"t": "Button"}, {"t": "Button", "r": [1, 2, 3]},
                                 {"t": "Button", "r": [1, 2, 3, "4"]}, {"t": "Button", "r": [1, 2, 3, float("nan")]},
                                 {"t": "1x", "r": [1, 2, 3, 4]}, {"t": "Button", "r": [True, 2, 3, 4]}])
def test_an_answer_that_isnt_a_hit_is_none(bad):
    assert ui.hit_of(bad) is None


def test_the_hit_line_names_a_listed_element_by_its_ref():
    items = listed()
    assert ui.hit_line(hit(BAR_BUTTON), items) == find(items, "checkout").line(actions=False)
    assert ui.hit_line(hit(BAR), items) == find(items, "sticky-bar").line(actions=False)
    assert ui.hit_line(ui.Hit("Other", "veil", "Veil", (0, 0, 430, 932)), items) == 'other "Veil" id=veil @0,0 430x932'


# ---- the session ----

class Runner(V4Runner):
    """Route version 5 (or what `version` says): the compact tree of TREE, the hit test answering `hits` (a node, or
    an exception), and enough of WebDriverAgent's element routes for typing."""

    def __init__(self, version=5, hits=None, tree=TREE):
        super().__init__(version=version)
        self.hits, self.tree = hits, tree
        self.active = "E1"  # the element /element/active answers...
        self.active_name = "email"  # ...and its name

    async def wda_alive(self):
        return True

    async def http(self, method, path, payload=None, timeout=30.0):
        route = path.split("?", 1)[0]
        if route.endswith("/wda/tw/tree"):
            self.requests.append((method, path, payload))
            self.read_no += 1
            tree = self.tree if self.version >= 5 else _without_f(self.tree)
            return {"value": {"token": f"v{self.read_no}", "tree": tree, "ms": {"snapshot": 90}}}
        if route.endswith("/wda/tw/hit"):
            self.requests.append((method, path, payload))
            if self.version < 5:
                raise DeviceError(f"WebDriverAgent: unknown command: Unhandled endpoint: {path}")
            if isinstance(self.hits, BaseException):
                raise self.hits
            return {"value": {"t": self.hits["t"], "r": self.hits["r"], "n": self.hits.get("n"),
                              "l": self.hits.get("l"), "pid": 42, "b": "com.example.lab", "via": "element",
                              "ms": {"hit": 9, "read": 14}}}
        if route.endswith("/element/active"):
            self.requests.append((method, path, payload))
            return {"value": {"ELEMENT": self.active}}
        if "/attribute/name" in route:
            self.requests.append((method, path, payload))
            return {"value": self.active_name}
        return await super().http(method, path, payload, timeout)


def _without_f(n: dict) -> dict:
    out = {k: v for k, v in n.items() if k != "f"}
    if "c" in out:
        out["c"] = [_without_f(c) for c in out["c"]]
    return out


@pytest.fixture(autouse=True)
def checks_on(monkeypatch):
    monkeypatch.setattr(session_module, "HIT_CHECK", "1")


def ready(runner) -> Session:
    s = Session(runner)
    s.screen = SCREEN
    asyncio.run(s.refresh())
    return s


def hits_asked(runner) -> list[str]:
    return [p for _, p, _ in runner.requests if p.split("?")[0].endswith("/wda/tw/hit")]


def test_a_v5_runner_is_asked_once_at_the_point_and_its_answer_read():
    runner = Runner(hits=BAR_BUTTON)
    s = ready(runner)
    got = asyncio.run(s.hit_at(215.0, 792.0))
    assert got == ui.Hit("Button", "checkout", "Checkout", (20.0, 772.0, 390.0, 48.0), "com.example.lab")
    assert hits_asked(runner) == ["/wda/tw/hit?x=215.00&y=792.00"]
    assert s.last_source is not None and 'name="sticky-bar"' in s.last_source


def test_an_older_runner_landscape_the_off_switch_or_an_error_skip_the_check(monkeypatch):
    old = Runner(version=4, hits=BAR_BUTTON)
    s = ready(old)
    assert asyncio.run(s.hit_at(215.0, 792.0)) is None and hits_asked(old) == []
    runner = Runner(hits=DeviceError("WebDriverAgent: unsupported operation: tapwright hit: no element answered"))
    s = ready(runner)
    assert asyncio.run(s.hit_at(215.0, 792.0)) is None and len(hits_asked(runner)) == 1
    runner.hits = {"t": "Button"}  # not a hit
    assert asyncio.run(s.hit_at(215.0, 792.0)) is None
    s.screen = (932.0, 430.0)
    assert asyncio.run(s.hit_at(215.0, 792.0)) is None and len(hits_asked(runner)) == 2
    s.screen = SCREEN
    monkeypatch.setattr(session_module, "HIT_CHECK", "0")
    assert asyncio.run(s.hit_at(215.0, 792.0)) is None and len(hits_asked(runner)) == 2


def test_a_dropped_line_during_the_hit_test_is_raised():
    runner = Runner(hits=ConnectionLost("the line dropped"))
    s = ready(runner)
    with pytest.raises(ConnectionLost):
        asyncio.run(s.hit_at(215.0, 792.0))


def test_a_hit_test_that_gets_no_answer_is_skipped_not_asked_again():
    runner = Runner(hits=ConnectionLost("the phone didn't answer within 3 s"))
    s = ready(runner)
    assert asyncio.run(s.hit_at(215.0, 792.0)) is None
    assert len(hits_asked(runner)) == 1 and s.sid is not None  # WebDriverAgent answers: the session stays


def touches(runner) -> list[str]:
    return [p for _, p, _ in runner.requests if p.endswith(("/wda/tw/tap", "/actions"))]


def test_typing_into_the_field_the_runner_says_has_focus_taps_nothing():
    runner = Runner()
    s = ready(runner)
    field = find(s.items, "email")
    assert field.focused
    asyncio.run(s.type_text("hi@example.com", target=ui.Target(ref=field.ref)))
    assert touches(runner) == []
    keys = [b for _, p, b in runner.requests if p.endswith("/wda/keys")]
    assert keys == [{"value": list("hi@example.com")}]
    assert s.typed_into == ("input:email", "E1")


def test_a_field_without_focus_or_an_older_runner_is_tapped_as_before():
    runner = Runner()
    s = ready(runner)
    other = find(s.items, "password")
    assert not other.focused
    runner.active_name = "password"
    asyncio.run(s.type_text("secret", target=ui.Target(ref=other.ref)))
    assert len(touches(runner)) == 1
    old = Runner(version=4)
    s = ready(old)
    field = find(s.items, "email")
    assert not field.focused  # a v4 read can't tell
    asyncio.run(s.type_text("hi", target=ui.Target(ref=field.ref)))
    assert len(touches(old)) == 1
    # a v4 runner whose read somehow said focused still taps: the decision needs version 5
    old2 = Runner(version=4)
    s = ready(old2)
    s.items = [dataclasses.replace(find(s.items, "email"), focused=True)]
    asyncio.run(s.type_text("hi", target=ui.Target(ref=s.items[0].ref)))
    assert len(touches(old2)) == 1


def test_a_focused_field_with_old_text_is_cleared_the_runners_way_not_with_a_tap():
    filled = dict(EMAIL, v="old@example.com")
    tree = node("Application", (0, 0, 430, 932), [node("Window", (0, 0, 430, 932), [filled])], pid=42, b="com.example.lab")
    runner = Runner(tree=tree)
    s = ready(runner)
    assert s.items[0].focused and s.items[0].value == "old@example.com"
    asyncio.run(s.type_text("new@example.com", replace=True, target=ui.Target(ref=s.items[0].ref)))
    assert touches(runner) == []
    assert any(p.endswith("/element/E1/clear") for _, p, _ in runner.requests)


# ---- the daemon: refuse a touch on something covered ----

def lab_items() -> list[ui.Element]:
    row = ui.Element("e1", "button", "Terms", "terms-row", rect=(20, 770, 390, 44), actions=("tap",), type="Cell",
                     name="terms-row")
    return [row, button("e2", "OK", "ok-button", rect=(20, 300, 100, 40), name="ok-button")]


def armed_daemon(answer):
    d = make()
    d.session.screen, d.session.app = SCREEN, ("Lab", "com.example.lab")
    d.session.hit_answer = answer
    d.session.last_source = source()
    return d


def test_a_tap_on_a_target_a_bar_covers_is_refused_and_touches_nothing():
    async def go():
        d = armed_daemon(hit(BAR_BUTTON))
        stop, task = await started(d)
        await looked(d, lab_items())
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "terms-row"}})
        assert e.value.code == "not_actionable"
        assert str(e.value).startswith('covered: e1 button "Terms" id=terms-row @20,770 390x44 is behind ')
        assert 'button "Checkout" id=checkout @20,772 390x48' in str(e.value)
        assert "tap" not in d.session.calls and d.session.hit_asks == [(215.0, 792.0)]
        await finish(stop, task)
    run(go)


def test_a_hit_on_the_target_or_inside_it_taps():
    async def go():
        for answer in (hit(ROW), hit(ROW_TEXT)):
            d = armed_daemon(answer)
            stop, task = await started(d)
            await looked(d, lab_items())
            r = await d.run_tool("a", "tap", {"target": {"id": "terms-row"}})
            assert r["text"].startswith("Tapped") and "tap" in d.session.calls
            await finish(stop, task)
    run(go)


def test_no_answer_force_or_a_point_tap_as_before():
    async def go():
        for answer in (None,):  # (another app's element over it is covered since W144: test_review_17_w144)
            d = armed_daemon(answer)
            stop, task = await started(d)
            await looked(d, lab_items())
            r = await d.run_tool("a", "tap", {"target": {"id": "terms-row"}})
            assert r["text"].startswith("Tapped")
            await finish(stop, task)
        d = armed_daemon(hit(BAR_BUTTON))
        stop, task = await started(d)
        await looked(d, lab_items())
        await d.run_tool("a", "tap", {"target": {"x": 215, "y": 792}})
        await d.run_tool("a", "tap", {"target": {"id": "terms-row"}, "force": True})
        assert d.session.hit_asks == [] and d.session.calls.count("tap") == 2
        await finish(stop, task)
    run(go)


# ---- the pin ----

def test_the_runner_pinned_is_tw_16_15_3_5():
    repo, tag, sha, folder = store.WDA_SOURCES["fork"]
    assert (repo, tag, folder) == ("bo5amis/WebDriverAgent", "tw-16.15.3.5", "fork-tw-16.15.3.5")
    assert len(sha) == 64 and store.WDA_VERSION == "fork-tw-16.15.3.5"


# ---- a cover the tree only guesses: the runner's hit test decides ----

CANCEL = node("Button", (24, 83, 36, 36), n="Cancel", l="Cancel")
SHEET_TABLE = node("Table", (0, 59, 430, 873), n="sheet-table")
SHEET = node("Application", (0, 0, 430, 932), [node("Window", (0, 0, 430, 932), [node("Other", (0, 0, 430, 932), [
    node("NavigationBar", (0, 79, 430, 54), [CANCEL], n="New Item"), SHEET_TABLE])])],
    n="Lab", l="Lab", pid=42, b="com.example.lab")


def sheet_items() -> list[ui.Element]:
    """Seen on the phone 2026-11-14 (Reminders' New Reminder sheet): the sheet's table is listed after its nav bar and
    overlaps it, so the tree guesses the table covers Cancel; iOS draws the bar on top."""
    cancel = button("e1", "Cancel", "", rect=(24, 83, 36, 36), name="Cancel")
    cancel.covered_by = (ui.Bar("Table", (0, 59, 430, 873), 'scroll id=sheet-table @0,59 430x873'),)
    return [cancel]


def test_a_cover_the_tree_only_guesses_is_dropped_when_the_hit_lands_on_the_target():
    async def go():
        d = armed_daemon(hit(CANCEL))
        d.session.last_source = source(SHEET)
        stop, task = await started(d)
        await looked(d, sheet_items())
        r = await d.run_tool("a", "tap", {"target": {"text": "Cancel"}})
        assert r["text"].startswith("Tapped") and "tap" in d.session.calls
        await finish(stop, task)
        # the hit lands on the table: the guess holds, and nothing is touched
        d = armed_daemon(hit(SHEET_TABLE))
        d.session.last_source = source(SHEET)
        stop, task = await started(d)
        await looked(d, sheet_items())
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"text": "Cancel"}})
        assert e.value.code == "not_actionable" and "tap" not in d.session.calls
        await finish(stop, task)
        # no answer (an older runner): as the tree says
        d = armed_daemon(None)
        stop, task = await started(d)
        await looked(d, sheet_items())
        with pytest.raises(Refused):
            await d.run_tool("a", "tap", {"target": {"text": "Cancel"}})
        await finish(stop, task)
    run(go)


def test_an_element_behind_a_guessed_layer_is_lifted_when_the_hit_lands_on_it():
    """Seen on the phone 2026-11-14 (Calendar's search): the results container read as a sheet over the search bar, so
    Close was 'behind' it; the runner's hit test says a touch there lands on Close."""
    async def go():
        close = button("", "Close", "", rect=(24, 83, 36, 36), name="Cancel")
        close.type, close.name = "Button", "Cancel"
        front = button("e1", "Today", "", rect=(176, 861, 78, 38), name="Today")
        for answer, tapped in ((hit(CANCEL), True), (hit(SHEET_TABLE), False), (None, False)):
            d = armed_daemon(answer)
            d.session.last_source = source(SHEET)
            stop, task = await started(d)
            await looked(d, [front])
            d.session.behind = [(close, front)]
            if tapped:
                r = await d.run_tool("a", "tap", {"target": {"text": "Close"}})
                assert r["text"].startswith("Tapped") and "tap" in d.session.calls
            else:
                with pytest.raises(Refused) as e:
                    await d.run_tool("a", "tap", {"target": {"text": "Close"}})
                assert e.value.code == "not_actionable" and "behind" in str(e.value) and "tap" not in d.session.calls
            await finish(stop, task)
    run(go)

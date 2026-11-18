"""1.7.8, the first Opus review of PR #18: each test fails on 930e33f and passes after."""
import asyncio
import dataclasses

import pytest

from test_daemon import finish, make, run, started

from tapwright import session as session_module, ui
from tapwright.control import Refused
from tapwright.session import Session

APP_HEAD = ('<AppiumAUT><XCUIElementTypeApplication type="XCUIElementTypeApplication" label="X" enabled="true" x="0" '
            'y="0" width="430" height="932">')
APP_TAIL = "</XCUIElementTypeApplication></AppiumAUT>"


def _text_view(y: float, value: str, label: str = "", name: str = "") -> str:
    attrs = (f' label="{label}"' if label else "") + (f' name="{name}"' if name else "")
    return (f'<XCUIElementTypeTextView type="XCUIElementTypeTextView"{attrs} value="{value}" enabled="true" x="56" '
            f'y="{y}" width="334" height="100"/>')


def _phone(tree: str, active: str, attrs: dict, rects: dict, stale: list | None = None):
    """A runner whose tree is `tree`, focus on `active`, element attributes and rects as given; `stale` lists rect
    requests (by element) that answer stale once each."""
    from test_session import Recorder
    from tapwright.device import DeviceError
    phone = Recorder()
    original = phone.http
    stale = list(stale or [])

    async def http(method, path, payload=None, timeout=30.0):
        if path == ui.SOURCE_PATH:
            phone.requests.append((method, path, payload))
            return {"value": tree}
        if path.endswith("/element/active"):
            return {"value": {"ELEMENT": active}}
        for (element, name), value in attrs.items():
            if path.endswith(f"/element/{element}/attribute/{name}"):
                return {"value": value}
        for element, (x, y, w, h) in rects.items():
            if path.endswith(f"/element/{element}/rect"):
                if element in stale:
                    stale.remove(element)
                    raise DeviceError("WebDriverAgent: stale element reference: the element is not present")
                return {"value": {"x": x, "y": y, "width": w, "height": h}}
        return await original(method, path, payload, timeout)
    phone.http = http
    return phone


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(session_module.store, "load_state", lambda: {"runner_id": "runner"})
    monkeypatch.setattr(session_module, "FOCUS_POLL", 0.01)
    monkeypatch.setattr(session_module, "FOCUS_WAIT", 0.05)


def _sent(phone) -> list[tuple[str, object]]:
    return [(p, b) for _, p, b in phone.requests if p.endswith(("/clear", "/wda/keys"))]


def _post(ref: str, y: float, value: str, label: str = "Post text", name: str = "", focused: bool = False):
    return ui.Element(ref, "input", label, name, value=value, rect=(56, y, 334, 100), actions=("tap", "type"),
                      focused=focused, name=name)


# H1: `again` (no tap, backspaces) went to the field typed into last, though the target was another field with its label

def test_h1_typing_again_never_goes_to_the_last_field_when_the_target_is_another_with_its_label(quick):
    tree = APP_HEAD + _text_view(125, "first post", "Post text") + _text_view(485, "second", "Post text") + APP_TAIL
    phone = _phone(tree, "p1", {("p1", "label"): "Post text"}, {"p1": (56, 125, 334, 100)})
    s = Session(phone)
    p1, p2 = _post("e1", 125, "first post"), _post("e2", 485, "second")

    async def go():
        await s.ensure()
        s.tw_version = 5
        s.items = [dataclasses.replace(p1, focused=True), p2]
        await s.type_text("hello", target=ui.Target(ref="e1"))  # post 1 has the focus: typed without a tap
        assert s.typed_into is not None
        phone.requests.clear()
        s.items = [p1, p2]
        try:
            await s.type_text("WORLD", replace=True, target=ui.Target(ref="e2"), at=p2.center)
        except Refused:
            pass  # post 1 keeps the focus: post 2 never took it
    asyncio.run(go())
    assert _sent(phone) == []  # nothing typed into post 1, nothing cleared


# H2: fields sharing an identifier (a testID repeated on every row): the name proved nothing

def test_h2_a_shared_identifier_never_lets_replace_clear_the_other_field(quick):
    long = "first post text that is long enough to need clear clear clear clear"
    tree = APP_HEAD + _text_view(125, long, name="postText") + _text_view(485, "second", name="postText") + APP_TAIL
    phone = _phone(tree, "p1", {("p1", "name"): "postText"}, {"p1": (56, 125, 334, 100)})
    s = Session(phone)
    p1, p2 = _post("e1", 125, long, "", "postText"), _post("e2", 485, "second " * 12, "", "postText")
    s.items = [p1, p2]

    async def go():
        await s.ensure()
        s.tw_version = 5
        with pytest.raises(Refused):  # the focusing tap at post 2 landed on post 1 (a re-layout)
            await s.type_text("WORLD", replace=True, target=ui.Target(ref="e2"), at=p2.center)
    asyncio.run(go())
    assert _sent(phone) == []


def test_h2_a_shared_identifier_clears_the_field_under_the_tap_when_it_has_the_focus(quick):
    long = "second " * 12
    tree = APP_HEAD + _text_view(125, "first", name="postText") + _text_view(485, long, name="postText") + APP_TAIL
    phone = _phone(tree, "p2", {("p2", "name"): "postText"}, {"p2": (56, 485, 334, 100)})
    s = Session(phone)
    p1, p2 = _post("e1", 125, "first", "", "postText"), _post("e2", 485, long, "", "postText")
    s.items = [p1, p2]

    async def go():
        await s.ensure()
        s.tw_version = 5
        await s.type_text("WORLD", replace=True, target=ui.Target(ref="e2"), at=p2.center)
    asyncio.run(go())
    assert [p for p, _ in _sent(phone)] == ["/session/S/element/p2/clear", "/session/S/wda/keys"]


def test_h2_the_target_row_gone_and_another_alike_field_left_is_not_taken_for_it(quick):
    """The listed read had two posts labelled alike; post 2 went away, and post 1 (the only one now) has the focus."""
    long = "first " * 12
    tree = APP_HEAD + _text_view(125, long, "Post text") + APP_TAIL
    phone = _phone(tree, "p1", {("p1", "label"): "Post text"}, {"p1": (56, 125, 334, 100)})
    s = Session(phone)
    s.items = [_post("e1", 125, long), _post("e2", 485, "second " * 12)]

    async def go():
        await s.ensure()
        s.tw_version = 5
        with pytest.raises(Refused):
            await s.type_text("WORLD", replace=True, target=ui.Target(ref="e2"))
    asyncio.run(go())
    assert _sent(phone) == []


# 6: a hosted field (share sheet): XCTest's rect is in the host's own space; the listed one was moved

def _node(kind, rect, children="", name="", label="", value=""):
    x, y, w, h = rect
    attrs = (f' name="{name}"' if name else "") + (f' label="{label}"' if label else "") + (f' value="{value}"' if value else "")
    head = f'<XCUIElementType{kind} type="XCUIElementType{kind}"{attrs} enabled="true" x="{x}" y="{y}" width="{w}" height="{h}"'
    return f"{head}>{children}</XCUIElementType{kind}>" if children else f"{head}/>"


def _hosted_comment_tree() -> str:
    content = _node("Other", (0, 0, 412, 393), _node("TextField", (20, 300, 372, 40), label="Add a comment",
                                                     value="old note"))
    bar = _node("NavigationBar", (0, 0, 412, 107), _node("Button", (360, 20, 30, 30), label="Close"))
    remote = _node("Other", (9, 531, 412, 393), bar + content, name="ShareSheet.RemoteContainerView")
    return ("<AppiumAUT>" + _node("Application", (0, 0, 430, 932), _node("Window", (0, 0, 430, 932), remote),
                                  label="Safari") + "</AppiumAUT>")


def test_6_replace_into_a_hosted_field_compares_xctests_frame_with_xctests_frame(quick):
    tree = _hosted_comment_tree()
    field = next(e for e in ui.read(tree, (430.0, 932.0)).items if e.role == "input")
    assert field.placed == "moved" and field.rect == (29, 831, 372, 40)
    phone = _phone(tree, "c1", {("c1", "label"): "Add a comment"}, {"c1": (20, 300, 372, 40)})
    s = Session(phone)
    s.items = [dataclasses.replace(field, focused=True)]

    async def go():
        await s.ensure()
        s.tw_version = 5
        await s.type_text("new", replace=True, target=ui.Target(ref=field.ref))
    asyncio.run(go())
    assert [p for p, _ in _sent(phone)] == ["/session/S/element/c1/clear", "/session/S/wda/keys"]


# 8: a stale element while focus is awaited is "not yet"; is_focused doesn't claim "Nothing was typed"

def test_8_a_field_that_goes_stale_once_as_it_takes_focus_is_still_typed_into(quick, monkeypatch):
    monkeypatch.setattr(session_module, "FOCUS_WAIT", 1.0)
    tree = APP_HEAD + _text_view(485, "", "Reply") + APP_TAIL
    phone = _phone(tree, "r1", {("r1", "label"): "Something else"}, {"r1": (56, 485, 334, 100)}, stale=["r1"])
    s = Session(phone)
    s.items = [_post("e1", 485, "", "Reply")]

    async def go():
        await s.ensure()
        await s.type_text("hi", target=ui.Target(ref="e1"))
    asyncio.run(go())
    assert [p for p, _ in _sent(phone)] == ["/session/S/wda/keys"]


def test_8_is_focused_on_a_stale_element_is_false_not_a_nothing_was_typed_refusal(quick):
    tree = APP_HEAD + _text_view(485, "x", "Reply") + APP_TAIL
    phone = _phone(tree, "r1", {("r1", "label"): "Something else"}, {"r1": (56, 485, 334, 100)}, stale=["r1"])
    s = Session(phone)
    s.items = [_post("e1", 485, "x", "Reply")]

    async def go():
        await s.ensure()
        return await s.is_focused(s.items[0])
    assert asyncio.run(go()) is False


# 3: ordinary screens near the origin were marked "unknown" (untouchable, even with force)

def _app(children: str) -> str:
    return ("<AppiumAUT>" + _node("Application", (0, 0, 430, 932), _node("Window", (0, 0, 430, 932), children),
                                  label="App") + "</AppiumAUT>")


def _read(xml: str):
    return ui.read(xml, (430.0, 932.0))


@pytest.mark.parametrize("y", [-1.5, 1.5])
def test_3_a_list_scrolled_a_point_or_two_is_an_ordinary_screen(y):
    cell = _node("Cell", (0, y, 430, 44), _node("Other", (0, y, 430, 44), _node("Button", (330, 4, 90, 34), label="Follow")
                                                + _node("StaticText", (16, 10, 200, 20), label="Row A")))
    items = _read(_app(_node("Table", (0, 0, 430, 932), cell))).items
    assert items and all(not e.placed and e.raw_rect is None for e in items)


def test_3_a_header_two_points_down_is_an_ordinary_screen():
    header = _node("Other", (0, 2, 430, 100), _node("Other", (0, 2, 430, 100), _node("Button", (16, 50, 60, 30),
                                                                                    label="Edit")))
    items = _read(_app(header)).items
    assert all(not e.placed for e in items) and next(e for e in items if e.label == "Edit").rect == (16, 50, 60, 30)


# 4: a parallax header (its cover pinned at (0, 0), the size of its container) was "moved"

def test_4_a_parallax_header_scrolled_up_is_left_where_it_is():
    header = _node("Other", (0, -60, 430, 300), _node("Image", (0, 0, 430, 300), label="cover")
                   + _node("Button", (16, 150, 80, 30), label="Follow"))
    items = _read(_app(header)).items
    follow = next(e for e in items if e.label == "Follow")
    assert not follow.placed and follow.rect == (16, 150, 80, 30)


def test_4_a_header_in_a_scroll_views_content_is_never_a_host():
    header = _node("Other", (0, 60, 430, 300), _node("Image", (0, 0, 430, 300), label="cover")
                   + _node("Button", (16, 210, 80, 30), label="Follow"))
    items = _read(_app(_node("ScrollView", (0, 0, 430, 932), header))).items
    follow = next(e for e in items if e.label == "Follow")
    assert not follow.placed and follow.rect == (16, 210, 80, 30)


def test_4_and_5_a_scroll_in_a_moved_host_is_dragged_only_where_the_phone_confirms_it():
    from test_hotfix_178 import _hosted_session, _share_sheet

    async def go():
        s = _hosted_session(_share_sheet())  # the hit test answers nothing
        d = make(s)
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "scroll", {"target": {"label": "Copy"}, "direction": "right", "look": False})
            assert e.value.code == "not_actionable" and str(e.value).startswith("covered: ")
            assert s.scrolls == []
        finally:
            await finish(stop, task)
    run(go)


# 5: back and scroll used raw frames of a host whose place can't be told

def _unknown_sheet_with(app_part: str) -> str:
    bar = _node("NavigationBar", (0, 0, 412, 60), _node("Button", (8, 10, 60, 40), name="BackButton", label="Back"))
    content = _node("Other", (0, 0, 412, 393), _node("Switch", (300, 120, 51, 31), label="All Photos Data"))
    stray = _node("Button", (300, 600, 100, 44), label="Stray")
    remote = _node("Other", (9, 531, 412, 393), bar + content + stray, name="ShareSheet.RemoteContainerView")
    return _app(app_part + _node("Other", (9, 531, 412, 393), remote, name="ActivityListView"))


def test_5_back_never_presses_a_button_of_a_host_whose_place_cant_be_told():
    appbar = _node("NavigationBar", (0, 47, 430, 50), _node("Button", (8, 50, 44, 44), name="BackButton", label="Back"))
    back = _read(_unknown_sheet_with(appbar)).back
    assert back is not None and back.point is None and back.hosted


def _async(value):
    async def answer(*args, **kwargs):
        return value
    return answer


def test_5_back_refuses_on_a_hosted_bar():
    from fakes import FakeSession

    async def go():
        s = FakeSession()
        d = make(s)
        d._still_back = _async(ui.Back(True, None, hosted=True))
        view = type("V", (), {})()
        with pytest.raises(Refused) as e:
            await d._back(view, {})
        assert e.value.code == "not_actionable" and str(e.value).startswith("covered: ")
        assert "tap" not in s.calls and "swipe" not in s.calls
    run(go)


def test_5_scroll_by_an_element_whose_place_cant_be_told_is_refused():
    from test_hotfix_178 import _hosted_session
    page = _node("ScrollView", (0, 0, 430, 932), _node("StaticText", (16, 100, 200, 20), label="Article"))

    async def go():
        s = _hosted_session(_unknown_sheet_with(page))
        assert next(e for e in s.tree if e.label == "All Photos Data").placed == "unknown"
        d = make(s)
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "scroll", {"target": {"label": "All Photos Data"}, "look": False})
            assert e.value.code == "not_actionable" and "can't be told" in str(e.value)
            assert s.scrolls == []
        finally:
            await finish(stop, task)
    run(go)


# 7: the placing pass walked every subtree (O(nodes x depth)): 4x slower reads on deep React Native trees

def _deep_tree(rows: int = 60, depth: int = 40) -> str:
    def chain(y: float) -> str:
        s = (f'<XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="row" enabled="true" x="20" '
             f'y="{y + 5}" width="100" height="20"/>')
        for _ in range(depth):
            s = (f'<XCUIElementTypeOther type="XCUIElementTypeOther" enabled="true" x="10" y="{y}" width="400" '
                 f'height="40">{s}</XCUIElementTypeOther>')
        return s
    return _app("".join(chain(100 + 44 * i) for i in range(rows)))


def test_7_placing_reads_each_frame_about_once(monkeypatch):
    root = ui.ET.fromstring(_deep_tree())
    nodes = sum(1 for _ in root.iter())
    calls = [0]
    real = ui._rect

    def counted(node):
        calls[0] += 1
        return real(node)
    monkeypatch.setattr(ui, "_rect", counted)
    ui._place_hosted(root)
    assert calls[0] <= nodes


def test_7_placing_costs_a_small_part_of_a_deep_read():
    import time
    xml = _deep_tree()
    root = ui.ET.fromstring(xml)

    def best(f) -> float:
        times = []
        for _ in range(3):
            t = time.perf_counter()
            f()
            times.append(time.perf_counter() - t)
        return min(times)
    placing = best(lambda: ui._place_hosted(root))
    parsing = best(lambda: ui.ET.fromstring(xml))  # (a read parses, then walks the tree several times)
    assert placing < 2 * parsing  # 930e33f: many times the parse


# 9: home while stuck pressed through a locked phone; a timeout said the home screen didn't come up

def _home(answers: dict):
    from tapwright.device import AppNotAnswering

    async def go():
        d = make()
        d.stuck = AppNotAnswering("com.atebits.Tweetie2", "X")
        sent = []

        async def http(method, path, payload=None, timeout=30.0):
            sent.append((method, path))
            got = answers.get(path, {})
            if isinstance(got, BaseException):
                raise got
            return got
        d.session.device.http = http
        try:
            return await d.run_tool("a", "press_button", {"name": "home"}), sent
        except Refused as e:
            return e, sent
    return asyncio.run(go())


def test_9_home_while_stuck_is_refused_locked_on_a_locked_phone():
    got, sent = _home({"/wda/locked": {"value": True}})
    assert isinstance(got, Refused) and got.code == "locked"
    assert ("POST", "/wda/homescreen") not in sent


def test_9_home_with_no_answer_in_time_says_it_may_still_come_up():
    from tapwright.device import ConnectionLost
    got, _ = _home({"/wda/locked": {"value": False},
                    "/wda/homescreen": ConnectionLost("WebDriverAgent didn't answer within 10 s")})
    assert isinstance(got, Refused) and got.code == "not_answering" and "may still come up" in str(got)


# 10: open_url's note after a redirect, and on a same-host page whose difference is text

def test_10_a_redirect_to_another_host_has_no_note():
    from test_hotfix_178 import _open_in_safari, _safari_tab

    async def go():
        text = await _open_in_safari(_safari_tab("9A3F", "example.com"), _safari_tab("F1E5", "m.youtube.com"),
                                     "https://youtu.be/abc")
        assert "may not have loaded" not in text
    run(go)


def test_10_a_same_host_page_whose_text_differs_has_no_note():
    from fakes import el
    from test_hotfix_178 import _open_in_safari, _safari_tab

    async def go():
        before = _safari_tab("9A3F", "example.com", [el("e30", "text", "Article one", rect=(8, 200, 300, 20))])
        after = _safari_tab("9A3F", "example.com", [el("e30", "text", "Article two", rect=(8, 200, 300, 20))])
        text = await _open_in_safari(before, after, "https://example.com/?x=2")
        assert "may not have loaded" not in text
    run(go)


# 11: lows

def test_11_a_hit_on_a_hosted_cells_own_text_confirms_the_cell():
    from tapwright import daemon as daemon_module
    cell = _node("Cell", (306, 239, 83, 154), _node("StaticText", (310, 330, 70, 20), label="View More"),
                 name="actionGroupCell", label="View More")
    other = _node("StaticText", (310, 100, 70, 20), label="Elsewhere")
    content = _node("Other", (0, 0, 412, 393), cell + other)
    xml = _app(_node("Other", (9, 531, 412, 393), content, name="host"))
    more = next(e for e in _read(xml).items if e.type == "Cell")
    assert more.placed == "moved"
    inner = ui.Hit("StaticText", "", "View More", (310, 330, 70, 20), "com.apple.mobilesafari")
    assert daemon_module._hit_is(inner, more, xml)
    outside = ui.Hit("StaticText", "", "Elsewhere", (310, 100, 70, 20), "com.apple.mobilesafari")
    assert not daemon_module._hit_is(outside, more, xml)


def test_11_type_text_at_a_point_on_a_control_drawn_over_the_field_is_refused():
    from fakes import el
    from test_daemon import looked
    toolbar = ui.Bar("Other", (0, 566, 430, 48), "other id=ComposeButtonBarView @0,566 430x48")
    post = dataclasses.replace(el("e26", "input", "What always bugged me", rect=(56, 485, 334, 240),
                                  value="What always bugged me"), covered_by=(toolbar,))

    async def go():
        d = make()
        stop, task = await started(d)
        try:
            await looked(d, [post])
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "type_text", {"target": {"x": 200, "y": 590}, "text": "x", "look": False})
            assert e.value.code == "not_actionable" and str(e.value).startswith("covered: (200, 590) is under other")
            assert d.session.typed == []
        finally:
            await finish(stop, task)
    run(go)


def test_11_type_text_at_a_point_under_the_keyboard_is_refused_even_with_force():
    from test_daemon import looked
    from test_hotfix_178 import _composer

    async def go():
        d = make()
        stop, task = await started(d)
        try:
            post, bar, gif, keyboard = _composer()
            post = dataclasses.replace(post, covered_by=(keyboard,))
            await looked(d, [post, bar, gif])
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "type_text", {"target": {"x": 200, "y": 700}, "text": "x", "force": True,
                                                    "look": False})
            assert e.value.code == "not_actionable" and "even with force" in str(e.value)
            assert d.session.typed == []
        finally:
            await finish(stop, task)
    run(go)

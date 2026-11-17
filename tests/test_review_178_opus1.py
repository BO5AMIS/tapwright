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

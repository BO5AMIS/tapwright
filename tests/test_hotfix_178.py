"""1.7.8, from the second demo-video report (2026-11-17, Tapwright 1.7.6 on a USB iPhone): bugs 3 to 7."""
import pytest

from test_daemon import finish, make, run, started

from tapwright.control import Refused
from tapwright.device import AppNotAnswering


# Bug 4: close_app and press_button home while the app in front isn't answering.

def test_close_app_survives_the_reconnect_storing_a_new_not_answering_for_the_same_app():
    """Each reconnect attempt stores a fresh AppNotAnswering; the guard compared objects and answered `reconnecting`
    on nearly every close_app for 2 min 40 s."""
    async def go():
        d = make()
        first = AppNotAnswering("com.atebits.Tweetie2", "X")
        d.stuck = AppNotAnswering("com.atebits.Tweetie2", "X")  # the supervisor's next attempt, the same app
        answer = await d._close_stuck(first, "X", [])
        assert answer["text"].startswith("Closed X (com.atebits.Tweetie2)")
        assert d.session.device.killed == ["com.atebits.Tweetie2"]
    run(go)


def test_close_app_still_refuses_when_another_app_became_the_stuck_one():
    async def go():
        d = make()
        first = AppNotAnswering("com.atebits.Tweetie2", "X")
        d.stuck = AppNotAnswering("com.example.other", "Other")
        with pytest.raises(Refused) as e:
            await d._close_stuck(first, "X", [])
        assert e.value.code == "reconnecting" and d.session.device.killed == []
    run(go)


def test_home_gets_through_while_the_front_app_is_not_answering():
    async def go():
        d = make()
        d.stuck = AppNotAnswering("com.atebits.Tweetie2", "X")
        sent = []

        async def http(method, path, payload=None, timeout=30.0):
            sent.append((method, path, timeout))
            return {}
        d.session.device.http = http
        answer = await d.run_tool("a", "press_button", {"name": "home"})
        assert answer["text"].startswith("Pressed home")
        assert sent == [("POST", "/wda/homescreen", 10.0)]
        assert "session" not in sent[0][1]  # never through a session, which would wait on the frozen app
    run(go)


def test_home_that_gets_no_answer_says_not_answering_and_other_buttons_stay_refused():
    from tapwright.device import ConnectionLost

    async def go():
        d = make()
        d.stuck = AppNotAnswering("com.atebits.Tweetie2", "X")

        async def http(method, path, payload=None, timeout=30.0):
            raise ConnectionLost("WebDriverAgent didn't answer POST /wda/homescreen within 10 s")
        d.session.device.http = http
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "press_button", {"name": "home"})
        assert e.value.code == "not_answering" and "close_app" in str(e.value)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "press_button", {"name": "volumeUp"})
        assert e.value.code == "not_answering"
    run(go)


# Bug 6: X's thread composer.

def _composer():
    """X's composer as post2/61 lists it: the second post's field, its centre under the app's floating toolbar."""
    from fakes import el
    from tapwright import ui
    post = el("e26", "input", "What always bugged me", rect=(56, 485, 334, 240), value="What always bugged me " * 4)
    bar = el("e30", "other", "", "ComposeButtonBarView", rect=(0, 566, 430, 48))
    gif = el("e31", "button", "GIF library", rect=(195, 574, 24, 32))
    keyboard = ui.Bar("Keyboard", (0, 650, 430, 282), 'keyboard @0,650 430x282')
    return post, bar, gif, keyboard


@pytest.mark.parametrize("force", [False, True])
def test_type_text_at_a_point_focuses_the_field_at_that_point_not_its_centre(force):
    """post2/62: (200, 497) named the right field, then the focusing tap went to its centre (223, 605), on X's toolbar
    beside GIF library, and the GIF picker opened."""
    from test_daemon import looked

    async def go():
        d = make()
        stop, task = await started(d)
        try:
            post, bar, gif, _ = _composer()
            await looked(d, [post, bar, gif])
            await d.run_tool("a", "type_text", {"target": {"x": 200, "y": 497}, "text": "Second post", "replace": True,
                                                "force": force, "look": False})
            assert d.session.last_point == (200, 497)
        finally:
            await finish(stop, task)
    run(go)


def test_type_text_at_a_point_under_the_keyboard_is_refused_not_typed_as_a_key():
    import dataclasses
    from test_daemon import looked

    async def go():
        d = make()
        stop, task = await started(d)
        try:
            post, bar, gif, keyboard = _composer()
            post = dataclasses.replace(post, covered_by=(keyboard,))
            await looked(d, [post, bar, gif])
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "type_text", {"target": {"x": 200, "y": 700}, "text": "x", "look": False})
            assert e.value.code == "not_actionable" and str(e.value).startswith("covered: (200, 700) is under keyboard")
            assert d.session.typed == []
        finally:
            await finish(stop, task)
    run(go)


def _thread_phone(phone, fields: list[tuple[float, float]], rect_error: Exception | None = None):
    """WebDriverAgent with X's composer: id-less TextViews, all labelled alike, at these (x, y); element/active is
    f0, whose label matches and whose rect is the first field's."""
    import tapwright.ui as ui_module
    from test_session import SETTINGS_TREE
    nodes = "".join(f'<XCUIElementTypeTextView type="XCUIElementTypeTextView" label="What always bugged me" '
                    f'enabled="true" x="{x}" y="{y}" width="334" height="240"/>' for x, y in fields)
    original = phone.http

    async def http(method, path, payload=None, timeout=30.0):
        if path == ui_module.SOURCE_PATH:
            return {"value": SETTINGS_TREE.replace("</XCUIElementTypeApplication>", nodes + "</XCUIElementTypeApplication>")}
        if path.endswith("/element/active"):
            return {"value": {"ELEMENT": "f0"}}
        if path.endswith("/element/f0/attribute/label"):
            return {"value": "What always bugged me"}
        if path.endswith("/element/f0/rect"):
            if rect_error is not None:
                raise rect_error
            x, y = fields[0]
            return {"value": {"x": x, "y": y, "width": 334, "height": 240}}
        return await original(method, path, payload, timeout)
    phone.http = http


def _post_field(y: float = 485):
    from tapwright import ui
    return ui.Element("e26", "input", "What always bugged me", value="What always bugged me " * 4, rect=(56, y, 334, 240),
                      actions=("tap", "type"))


def test_replace_refuses_when_the_label_alone_cant_tell_which_post_has_focus(monkeypatch):
    """Two id-less posts with the same label: the focus check accepted the label, and the clear could have wiped the
    other post. Right before clearing, the field's place must prove it; here it can't, so nothing is cleared."""
    import asyncio
    from test_session import Recorder
    from tapwright import session as session_module, ui
    from tapwright.session import Session
    monkeypatch.setattr(session_module.store, "load_state", lambda: {"runner_id": "runner"})
    monkeypatch.setattr(session_module, "FOCUS_POLL", 0.01)
    phone = Recorder()
    _thread_phone(phone, [(56, 485), (56, 125)])
    s = Session(phone)
    s.items = [_post_field()]
    with pytest.raises(Refused) as e:
        asyncio.run(s.type_text("Second post", replace=True, target=ui.Target(ref="e26")))
    assert e.value.code == "stale" and "Nothing was cleared" in str(e.value)
    assert not any(p.endswith(("/clear", "/wda/keys")) for _, p, _ in phone.requests)


def test_replace_clears_the_one_post_whose_place_proves_it(monkeypatch):
    import asyncio
    from test_session import Recorder
    from tapwright import session as session_module, ui
    from tapwright.session import Session
    monkeypatch.setattr(session_module.store, "load_state", lambda: {"runner_id": "runner"})
    monkeypatch.setattr(session_module, "FOCUS_POLL", 0.01)
    phone = Recorder()
    _thread_phone(phone, [(56, 485)])
    s = Session(phone)
    s.items = [_post_field()]
    asyncio.run(s.type_text("Second post", replace=True, target=ui.Target(ref="e26")))
    assert [p for _, p, _ in phone.requests if p.endswith(("/clear", "/wda/keys"))] == [
        "/session/S/element/f0/clear", "/session/S/wda/keys"]


def test_a_stale_element_while_checking_focus_is_stale_not_internal(monkeypatch):
    """post2/62: the old focus owner went stale as the GIF picker opened, and its rect request raised `internal`."""
    import asyncio
    from test_session import Recorder
    from tapwright import session as session_module, ui
    from tapwright.device import DeviceError
    from tapwright.session import Session
    monkeypatch.setattr(session_module.store, "load_state", lambda: {"runner_id": "runner"})
    monkeypatch.setattr(session_module, "FOCUS_POLL", 0.01)
    phone = Recorder()
    _thread_phone(phone, [(56, 485)], rect_error=DeviceError(
        "WebDriverAgent: stale element reference: The previously found element \"TextView\" is not present"))
    s = Session(phone)
    field = ui.Element("e26", "input", "Second post", rect=(56, 485, 334, 240), actions=("tap", "type"))
    s.items = [field]
    original = phone.http

    async def http(method, path, payload=None, timeout=30.0):
        if path.endswith("/element/f0/attribute/label"):
            return {"value": "I gave an AI agent my iPhone"}  # the old focus owner: another post
        if path == ui.SOURCE_PATH:  # the field is found again, so its rect is compared: that request meets the stale one
            return {"value": '<AppiumAUT><XCUIElementTypeApplication type="XCUIElementTypeApplication" label="X" '
                             'enabled="true" x="0" y="0" width="430" height="932"><XCUIElementTypeTextView '
                             'type="XCUIElementTypeTextView" label="Second post" enabled="true" x="56" y="485" '
                             'width="334" height="240"/></XCUIElementTypeApplication></AppiumAUT>'}
        return await original(method, path, payload, timeout)
    phone.http = http
    with pytest.raises(Refused) as e:
        asyncio.run(s.type_text("Second post", replace=True, target=ui.Target(ref="e26")))
    assert e.value.code == "stale"
    assert not any(p.endswith(("/clear", "/wda/keys")) for _, p, _ in phone.requests)


# Bug 5: Safari's focused address field, drawn in the keyboard's accessory area.

def _safari(focused: bool = True):
    import dataclasses
    from fakes import el
    from tapwright import ui
    keyboard = ui.Bar("Keyboard", (0, 546, 430, 386), "keyboard @0,546 430x386")
    page = el("e2", "scroll", "", rect=(0, 0, 430, 932))
    address = dataclasses.replace(el("e40", "input", "Address", "URL", rect=(20, 558, 302, 48), value="example.com"),
                                  focused=focused, covered_by=(keyboard,))
    find = el("e41", "input", "Find on page", rect=(20, 200, 302, 40))
    return [page, find, address]


def test_the_focused_field_the_keyboard_serves_isnt_covered_by_it():
    """post1/29: type_text (no target, submit) was refused `covered … by keyboard`; force typed fine (31)."""
    async def go():
        d = make()
        stop, task = await started(d)
        try:
            from test_daemon import looked
            d.session.tw_version = 5
            await looked(d, _safari())
            answer = await d.run_tool("a", "type_text", {"text": "example.com/page", "submit": True, "look": False})
            assert answer["text"].startswith('Typed into e40 input "Address"')
            assert d.session.no_tap is True  # Session must not tap it (its centre is on the keys' side of the cover)
        finally:
            await finish(stop, task)
    run(go)


def test_an_unfocused_field_under_the_keyboard_is_still_covered():
    async def go():
        d = make()
        stop, task = await started(d)
        try:
            from test_daemon import looked
            d.session.tw_version = 5
            await looked(d, _safari(focused=False))
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "type_text", {"target": {"ref": "e40"}, "text": "x", "look": False})
            assert e.value.code == "not_actionable" and str(e.value).startswith("covered:")
            assert d.session.typed == []
        finally:
            await finish(stop, task)
    run(go)


def test_no_tap_refuses_stale_when_the_focus_moved_off_the_field(monkeypatch):
    import asyncio
    from test_session import Recorder
    from tapwright import session as session_module, ui
    from tapwright.session import Session
    monkeypatch.setattr(session_module.store, "load_state", lambda: {"runner_id": "runner"})
    phone = Recorder()
    s = Session(phone)
    s.tw_version = 5
    s.items = [ui.Element("e40", "input", "Address", "URL", rect=(20, 558, 302, 48), focused=True, actions=("tap", "type"))]
    with pytest.raises(Refused) as e:  # element/active answers nothing: the focus is no longer the field's
        asyncio.run(s.type_text("x", target=ui.Target(ref="e40"), no_tap=True))
    assert e.value.code == "stale"
    assert not any(p.endswith(("/tap", "/wda/keys", "/clear")) or "actions" in p for _, p, _ in phone.requests)

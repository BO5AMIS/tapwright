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
            return {"value": False} if path == "/wda/locked" else {}
        d.session.device.http = http
        answer = await d.run_tool("a", "press_button", {"name": "home"})
        assert answer["text"].startswith("Pressed home")
        assert sent == [("GET", "/wda/locked", 2.0), ("POST", "/wda/homescreen", 10.0)]  # (Opus review 9: not locked)
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


# Bug 7: open_url while Safari shows a video kept the old tab and answered "opened".

TAB = "TabDocument?IsLoadedUsingDesktopUserAgent=false&InReaderMode=false&UUID={uuid}&IsPrivate=false&IsPageLoaded=true"


def _safari_tab(uuid: str, host: str, page: list | None = None) -> list:
    import dataclasses
    from fakes import button, el
    address = dataclasses.replace(el("e35", "input", "Address", "TabBarItemTitle", rect=(145, 861, 140, 26)), value=host)
    return [el("e20", "other", "", TAB.format(uuid=uuid), rect=(0, 0, 430, 932)),
            *(page if page is not None else [button("e25", "Play", rect=(158, 411, 119, 60)),
                                             el("e26", "text", "Elapsed: 52 seconds", rect=(8, 538, 36, 16))]),
            address]


async def _open_in_safari(before: list, after: list, url: str) -> str:
    from fakes import FakeSession
    s = FakeSession()
    s.app = ("Safari", "com.apple.mobilesafari")
    s.tree = before
    d = make(s)
    stop, task = await started(d)
    try:
        await d.run_tool("a", "look", {"screenshot": False})
        s.tree = after
        return (await d.run_tool("a", "open_url", {"url": url}))["text"]
    finally:
        await finish(stop, task)


def test_open_url_says_when_safari_kept_the_open_page():
    async def go():
        page = _safari_tab("9A3F", "192.168.100.67")
        text = await _open_in_safari(page, list(page), "http://192.168.100.67:19005/index.html?x=2")
        assert text.startswith("opened ") and "Note: Safari kept the open page; the link may not have loaded." in text
    run(go)


def test_open_url_says_when_safari_shows_another_host():
    async def go():
        from fakes import el
        before = _safari_tab("9A3F", "192.168.100.148")
        after = _safari_tab("9A3F", "192.168.100.148", [el("e30", "text", "Elapsed: 53 seconds", rect=(8, 538, 36, 16))])
        text = await _open_in_safari(before, after, "http://192.168.100.67:19005/index.html")
        assert "Note: Safari shows 192.168.100.148, not 192.168.100.67; the link may not have loaded." in text
    run(go)


@pytest.mark.parametrize("change", ["new tab", "new page", "www"])
def test_open_url_that_loaded_has_no_note(change):
    async def go():
        from fakes import el
        before = _safari_tab("9A3F", "192.168.100.67")
        if change == "new tab":
            after, url = _safari_tab("F1E5", "192.168.100.67"), "http://192.168.100.67:19005/index.html"
        elif change == "new page":
            after = _safari_tab("9A3F", "192.168.100.67", [el("e40", "link", "Download video", rect=(20, 200, 200, 40))])
            url = "http://192.168.100.67:19005/index.html"
        else:
            after, url = _safari_tab("F1E5", "example.com"), "https://www.example.com/"
        text = await _open_in_safari(before, after, url)
        assert "may not have loaded" not in text
    run(go)


# Bug 3: content another process draws (a share sheet, a photo picker) is listed with its host view's own frames.

def _node(kind: str, rect, children: str = "", name: str = "", label: str = "") -> str:
    x, y, w, h = rect
    attrs = (f' name="{name}"' if name else "") + (f' label="{label}"' if label else "")
    head = f'<XCUIElementType{kind} type="XCUIElementType{kind}"{attrs} enabled="true" x="{x}" y="{y}" width="{w}" height="{h}"'
    return f"{head}>{children}</XCUIElementType{kind}>" if children else f"{head}/>"


def _app(children: str, label: str = "Safari") -> str:
    return ("<AppiumAUT>" + _node("Application", (0, 0, 430, 932), _node("Window", (0, 0, 430, 932), children),
                                  label=label) + "</AppiumAUT>")


def _share_sheet(host_child_extra: str = "") -> str:
    """Safari's share sheet as post1/19 lists it: the host at (9, 531), its content at (0, 0) with the host's size."""
    cells = _node("ScrollView", (22, 239, 368, 154),
                  _node("Cell", (22, 239, 83, 154), name="actionGroupCell", label="Copy")
                  + _node("Cell", (306, 239, 83, 154), name="actionGroupCell", label="View More"))
    content = _node("Other", (0, 0, 412, 393), _node("CollectionView", (0, 0, 412, 393), cells, name="activityCollectionView"),
                    name="shareSheet.activity.contentView")
    bar = _node("NavigationBar", (0, 0, 412, 107), _node("Image", (26, 26, 65, 65), label="safari"),
                name="UIActivityContentView")
    remote = _node("Other", (9, 531, 412, 393), bar + content + host_child_extra, name="ShareSheet.RemoteContainerView")
    return _app(_node("Other", (0, 0, 430, 932), name="PopoverDismissRegion", label="dismiss popup")
                + _node("Image", (-141, 381, 712, 693))
                + _node("Other", (9, 531, 412, 393), remote, name="ActivityListView"))


def _photo_picker() -> str:
    """X's photo picker as post2/25 lists it: TUINativePhotoPicker at (0, 69), its scroll view at (0, 0) 430x863."""
    bar = _node("NavigationBar", (0, 20, 430, 96), _node("Button", (24, 58, 36, 36), label="Cancel")
                + _node("Button", (370, 58, 36, 36), name="Add", label="Done"), name="Photos")
    grid = _node("ScrollView", (0, 0, 430, 863), _node("Image", (-1, 106, 144, 143), name="PXGGridLayout-Info",
                                                          label="Video, fifty-two seconds"),
                 name="photosView_content_scroll_view")
    return _app(_node("Image", (-150, -81, 730, 1163)) + _node("Other", (0, 69, 430, 863), bar + grid,
                                                                 name="TUINativePhotoPicker"), label="X")


def _items(xml: str):
    from tapwright import ui
    return ui.read(xml, (430.0, 932.0)).items


def _one(items, label):
    [e] = [e for e in items if e.label == label]
    return e


def test_a_share_sheets_content_is_put_on_the_screen_by_its_hosts_place():
    items = _items(_share_sheet())
    more = _one(items, "View More")
    assert more.rect == (315, 770, 83, 154) and more.center == (356.5, 847)  # the report's real centre
    assert more.placed == "moved" and more.raw_rect == (306, 239, 83, 154)
    host = next(e for e in items if e.identifier == "ShareSheet.RemoteContainerView")
    outer = next(e for e in items if e.identifier == "ActivityListView")
    assert host.rect == outer.rect == (9, 531, 412, 393) and not host.placed and not outer.placed  # the hosts stay
    assert _one(items, "safari").rect == (35, 557, 65, 65)


def test_the_photo_pickers_done_is_put_where_it_is():
    items = _items(_photo_picker())
    done = _one(items, "Done")
    assert done.center == (388, 145) and done.placed == "moved" and done.raw_rect == (370, 58, 36, 36)
    assert not next(e for e in items if e.identifier == "TUINativePhotoPicker").placed


def test_ordinary_screens_are_left_as_they_are():
    from tapwright import ui
    # a sheet at (0, 100) whose children are in the screen's space; a modal over a full-screen dimming view
    sheet = _node("Other", (0, 100, 430, 700), _node("Button", (20, 120, 100, 44), label="Close")
                  + _node("Other", (0, 100, 430, 700), _node("StaticText", (20, 200, 300, 20), label="Title")),
                  name="sheet")
    modal = _node("Other", (20, 200, 390, 400), _node("Other", (0, 0, 430, 932), name="dim")
                  + _node("Button", (40, 500, 100, 44), label="OK"), name="modal")
    for xml in (_app(sheet), _app(modal)):
        items = ui.read(xml, (430.0, 932.0)).items
        assert items and all(not e.placed and e.raw_rect is None for e in items)
    assert _one(ui.read(_app(sheet), (430.0, 932.0)).items, "Close").rect == (20, 120, 100, 44)


def test_a_host_whose_frames_dont_all_fit_is_unknown_not_moved():
    """The tell (a child at (0, 0) with the host's size) with a frame that can't be in the host's own space: no
    translation, and every element in it is marked unknown, listed where XCTest put it."""
    stray = _node("Button", (300, 600, 100, 44), label="Stray")
    items = _items(_share_sheet(host_child_extra=stray))
    for label in ("Copy", "Stray"):
        assert _one(items, label).placed == "unknown"
    assert _one(items, "Copy").rect == (22, 239, 83, 154)


def _hosted_session(xml: str, version: int = 6):
    from fakes import FakeSession
    s = FakeSession()
    s.app = ("Safari", "com.apple.mobilesafari")
    s.tree = _items(xml)
    s.tw_version = version
    return s


def _hit_of(e, raw=None):
    from tapwright import ui
    return ui.Hit(e.type, e.name, e.label, raw or e.raw_rect, "com.apple.mobilesafari")


@pytest.mark.parametrize("force", [False, True])
def test_a_hosted_element_is_touched_where_the_phone_confirms_it(force):
    async def go():
        s = _hosted_session(_share_sheet())
        s.hit_answer = _hit_of(_one(s.tree, "View More"))
        d = make(s)
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            answer = await d.run_tool("a", "tap", {"target": {"label": "View More"}, "force": force, "look": False})
            assert answer["text"].startswith("Tapped") and s.hit_asks == [(356.5, 847)]
            assert s.last_point == (356.5, 847)
        finally:
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("case", ["another element", "no answer", "older runner"])
def test_a_hosted_element_the_phone_doesnt_confirm_is_not_touched(case):
    """post1/20: the tap aimed at "View More"'s listed frame landed on the video behind and answered "Tapped"."""
    import dataclasses

    async def go():
        s = _hosted_session(_share_sheet(), version=5 if case == "older runner" else 6)
        more = _one(s.tree, "View More")
        if case == "another element":
            s.hit_answer = dataclasses.replace(_hit_of(more), label="Copy", rect=(22, 239, 83, 154))
        elif case == "older runner":
            s.hit_answer = _hit_of(more)
        d = make(s)
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "tap", {"target": {"label": "View More"}, "look": False})
            assert e.value.code == "not_actionable" and str(e.value).startswith("covered: ")
            assert "nothing was touched" in str(e.value) and "tap it by point" in str(e.value)
            assert "tap" not in s.calls
        finally:
            await finish(stop, task)
    run(go)


def test_an_element_whose_place_is_unknown_is_never_touched_by_its_frame_even_with_force():
    async def go():
        s = _hosted_session(_share_sheet(host_child_extra=_node("Button", (300, 600, 100, 44), label="Stray")))
        copy = _one(s.tree, "Copy")
        s.hit_answer = _hit_of(copy, raw=copy.rect)  # even a hit that seems to agree proves nothing here
        d = make(s)
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            for force in (False, True):
                with pytest.raises(Refused) as e:
                    await d.run_tool("a", "tap", {"target": {"label": "Copy"}, "force": force, "look": False})
                assert e.value.code == "not_actionable" and "can't be told" in str(e.value)
            assert "tap" not in s.calls
            await d.run_tool("a", "tap", {"target": {"x": 356, "y": 847}, "look": False})  # a point stays a raw touch
            assert s.last_point == (356, 847)
        finally:
            await finish(stop, task)
    run(go)

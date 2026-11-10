"""Session without a phone: a recording device answers WebDriverAgent's requests."""
import asyncio
import base64

import pytest
from fakes import image_bytes

from tapwright import session as session_module
from tapwright import ui as ui_module
from tapwright.control import Refused
from tapwright.device import AppNotAnswering, ConnectionLost, DeviceError, NotSetUp, PhoneLocked
from tapwright.session import Session


class Recorder:
    def __init__(self):
        self.rsd, self.tunnel, self.runner, self.via = "tunnel", None, None, "USB"
        self.requests, self.started, self.closed = [], [], False
        self.fail = {}  # path suffix -> exception to raise once
        self.close_error = None
        self.locked = True  # what /wda/locked answers
        self.in_front: list[str] = []  # the bundle ids iOS has in the foreground (its process list)

    async def foreground_apps(self):
        return list(self.in_front)

    async def user_apps(self):
        return [{"bundle_id": "app.jld.mobile", "name": "JLD", "version": "1.0", "build": "1"}]

    async def connect(self):
        self.rsd = "tunnel"

    async def mount(self):
        pass

    async def start_wda(self, runner):
        self.started.append(runner)

    async def close(self):
        self.closed = True
        if self.close_error:
            raise self.close_error

    async def http(self, method, path, payload=None, timeout=30.0):
        self.requests.append((method, path, payload))
        for suffix in list(self.fail):
            if path.endswith(suffix):
                raise self.fail.pop(suffix)
        if path == "/session":
            return {"sessionId": "S"}
        if path.endswith("/window/size"):
            return {"value": {"width": 430, "height": 932}}
        if path.endswith("/wda/locked"):
            return {"value": self.locked}
        return {"value": None}


@pytest.fixture
def phone(monkeypatch):
    monkeypatch.setattr(session_module.store, "load_state", lambda: {"runner_id": "runner"})
    return Recorder()


def test_session_starts_once_then_costs_nothing(phone):
    s = Session(phone)
    asyncio.run(s.ensure())
    assert s.sid == "S" and s.screen == (430.0, 932.0) and phone.started == ["runner"]
    count = len(phone.requests)
    asyncio.run(s.ensure())
    assert len(phone.requests) == count  # no /status round trip before every call


def test_a_frozen_app_in_front_is_named_instead_of_a_dropped_line(phone):
    """2026-11-10: JLD froze in front; every read hung behind it and the daemon said 'reconnecting' for hours."""
    phone.locked, phone.in_front = False, ["app.jld.mobile"]
    phone.fail["/window/size"] = ConnectionLost("WebDriverAgent didn't answer GET /session/S/window/size within 10 s")
    s = Session(phone)
    with pytest.raises(AppNotAnswering) as e:
        asyncio.run(s.ensure())
    assert (e.value.bundle_id, e.value.name) == ("app.jld.mobile", "JLD") and s.sid is None
    assert str(e.value) == "JLD (app.jld.mobile) is in front and isn't answering, so nothing on the phone can be read"
    paths = [path for _, path, _ in phone.requests]
    # asked before the first read of the app: once that read hangs, nothing else gets an answer
    assert paths.index("/session/S/wda/locked") < paths.index("/session/S/window/size")


def test_a_tree_read_that_hangs_mid_session_is_cut_and_the_frozen_app_named(phone):
    """An app that freezes while in use: the read used to wait up to 60 s in silence. It is cut at READ_CUT, and
    the app iOS has in front is named (R6: a drop ends clearly)."""
    s = Session(phone)
    asyncio.run(s.ensure())
    phone.locked, phone.in_front = False, ["app.jld.mobile"]
    waits = []
    real = phone.http

    async def http(method, path, payload=None, timeout=30.0):
        if path.startswith("/session/S/source") or "/source" in path:
            waits.append(timeout)
            raise ConnectionLost(f"WebDriverAgent didn't answer {method} {path} within {timeout:.0f} s")
        return await real(method, path, payload, timeout)
    phone.http = http
    with pytest.raises(AppNotAnswering) as e:
        asyncio.run(s.refresh())
    assert e.value.bundle_id == "app.jld.mobile" and waits == [session_module.READ_CUT]
    phone.in_front = []  # nothing in front can be blamed: it stays a dropped line
    asyncio.run(s.ensure())
    with pytest.raises(ConnectionLost) as e:
        asyncio.run(s.refresh())
    assert type(e.value) is ConnectionLost


def test_a_locked_phone_at_connect_is_named(phone):
    phone.locked, phone.in_front = True, ["app.jld.mobile"]
    phone.fail["/window/size"] = ConnectionLost("didn't answer")
    with pytest.raises(PhoneLocked):
        asyncio.run(Session(phone).ensure())


def test_a_hang_with_no_app_in_front_stays_a_dropped_line(phone):
    phone.locked = False
    phone.fail["/window/size"] = ConnectionLost("didn't answer")
    with pytest.raises(ConnectionLost) as e:
        asyncio.run(Session(phone).ensure())
    assert type(e.value) is ConnectionLost


def test_not_set_up(monkeypatch):
    monkeypatch.setattr(session_module.store, "load_state", lambda: {})
    with pytest.raises(NotSetUp):
        asyncio.run(Session(Recorder()).ensure())


def test_keys_paths_settings_and_lock(phone):
    s = Session(phone)

    async def go():
        await s.key("backspace")
        await s.key("enter")
        await s.path([(0, 10.0, 20.0), (120, 10.0, 220.0)])
        await s.settings({"mjpegServerFramerate": 30})
        return await s.locked()
    assert asyncio.run(go()) is True
    keys = [p for _, path, p in phone.requests if path == "/session/S/wda/keys"]
    assert keys == [{"value": ["\b"]}, {"value": ["\n"]}]
    actions = next(p for _, path, p in phone.requests if path == "/session/S/actions")
    steps = actions["actions"][0]["actions"]
    assert [st["type"] for st in steps] == ["pointerMove", "pointerDown", "pointerMove", "pointerUp"]
    assert steps[2]["duration"] == 120 and (steps[2]["x"], steps[2]["y"]) == (10, 220)
    assert ("POST", "/session/S/appium/settings", {"settings": {"mjpegServerFramerate": 30}}) in phone.requests
    assert s.last_point == (10.0, 20.0)
    with pytest.raises(Refused) as e:
        asyncio.run(s.key("escape"))
    assert e.value.code == "bad_params"


def test_reset_forgets_the_connection(phone):
    s = Session(phone)
    asyncio.run(s.ensure())
    asyncio.run(s.reset())
    assert phone.closed and phone.rsd is None and s.sid is None


def test_dead_line_starts_a_new_session_on_the_next_call(phone):
    s = Session(phone)
    asyncio.run(s.ensure())
    phone.fail["/wda/locked"] = ConnectionLost("line is dead")
    with pytest.raises(ConnectionLost):
        asyncio.run(s.locked())
    assert s.sid is None
    assert asyncio.run(s.locked()) is True
    assert phone.started == ["runner", "runner"] and s.sid == "S"


def test_invalid_session_counts_as_a_dead_line(phone):
    s = Session(phone)
    asyncio.run(s.ensure())
    phone.fail["/wda/locked"] = DeviceError("HTTP 404: invalid session id")
    with pytest.raises(ConnectionLost):
        asyncio.run(s.locked())
    assert s.sid is None
    phone.fail["/wda/locked"] = DeviceError("HTTP 500: something else")
    with pytest.raises(DeviceError) as e:
        asyncio.run(s.locked())
    assert not isinstance(e.value, ConnectionLost)


def test_failed_window_size_leaves_no_session(phone):
    s = Session(phone)
    phone.fail["/window/size"] = ConnectionLost("gone")
    with pytest.raises(ConnectionLost):
        asyncio.run(s.ensure())
    assert s.sid is None
    asyncio.run(s.ensure())
    assert s.sid == "S"


def test_reset_clears_everything_even_when_close_fails(phone):
    s = Session(phone)
    asyncio.run(s.ensure())
    phone.tunnel, phone.runner = object(), object()
    phone.close_error = RuntimeError("boom")
    s.items, s.last_point = [object()], (1.0, 2.0)
    asyncio.run(s.reset())
    assert phone.rsd is None and phone.tunnel is None and phone.runner is None
    assert s.sid is None and s.items == [] and s.last_point is None


def test_an_alert_answer_with_no_alert_is_not_found(phone):
    s = Session(phone)
    for action in ("accept", "dismiss"):
        phone.fail[f"/alert/{action}"] = DeviceError("WebDriverAgent: no such alert: An attempt was made to operate "
                                                     "on a modal dialog when one was not open.")
        with pytest.raises(Refused) as e:
            asyncio.run(s.alert(action))
        assert e.value.code == "not_found" and str(e.value) == "no alert on screen."
    phone.fail["/alert/text"] = DeviceError("WebDriverAgent: no such alert: none")
    with pytest.raises(Refused) as e:
        asyncio.run(s.alert("text"))
    assert e.value.code == "not_found"
    phone.fail["/alert/accept"] = DeviceError("WebDriverAgent: unknown error: boom")
    with pytest.raises(DeviceError):  # any other failure stays the phone side's
        asyncio.run(s.alert("accept"))


def test_a_scroll_swipe_holds_still_before_the_finger_lifts(phone):
    s = Session(phone)
    asyncio.run(s.swipe(5.0, 400.0, 5.0, 100.0, 600, hold_ms=150))
    steps = [r for r in phone.requests if r[1].endswith("/actions")][-1][2]["actions"][0]["actions"]
    assert [t["type"] for t in steps[-3:]] == ["pointerMove", "pause", "pointerUp"] and steps[-2]["duration"] == 150


def test_tap_and_swipe_record_last_point(phone):
    s = Session(phone)
    asyncio.run(s.tap(ui_module.Target(x=30, y=40)))
    assert s.last_point == (30.0, 40.0)
    asyncio.run(s.swipe(5.0, 6.0, 5.0, 200.0))
    assert s.last_point == (5.0, 6.0)


SETTINGS_TREE = ('<AppiumAUT><XCUIElementTypeApplication type="XCUIElementTypeApplication" label="Settings" '
                 'enabled="true" x="0" y="0" width="430" height="932">'
                 '<XCUIElementTypeButton type="XCUIElementTypeButton" name="wifi" label="Wi-Fi" enabled="true" '
                 'x="16" y="215" width="398" height="52"/></XCUIElementTypeApplication></AppiumAUT>')


def test_look_reads_the_tree_the_app_and_the_scale(phone):
    answers = {ui_module.SOURCE_PATH: {"value": SETTINGS_TREE},
               "/wda/activeAppInfo": {"value": {"bundleId": "com.apple.Preferences", "name": "Settings", "pid": 12}},
               "/session/S/wda/screen": {"value": {"scale": 3, "statusBarSize": {"width": 430, "height": 59}}}}
    original = phone.http

    async def http(method, path, payload=None, timeout=30.0):
        if path in answers:
            phone.requests.append((method, path, payload))
            return answers[path]
        return await original(method, path, payload, timeout)
    phone.http = http
    s = Session(phone)
    asyncio.run(s.read_scale())  # at connect
    text, image = asyncio.run(s.look(screenshot=False))
    assert image is None and s.scale == 3.0
    assert text.splitlines() == ["Screen 430x932 pt @3x · 1 elements", "App: Settings (com.apple.Preferences)",
                                 'e1 button "Wi-Fi" id=wifi @16,215 398x52 [tap]']
    assert [e.ref for e in s.items] == ["e1"]
    asyncio.run(s.look(screenshot=False))
    assert [p for _, p, _ in phone.requests].count("/session/S/wda/screen") == 1  # the scale is read once


def test_look_without_app_or_scale_still_works(phone):
    original = phone.http

    async def http(method, path, payload=None, timeout=30.0):
        if path == ui_module.SOURCE_PATH:
            return {"value": SETTINGS_TREE}
        if path.endswith("/wda/activeAppInfo") or path.endswith("/wda/screen"):
            raise DeviceError("HTTP 404: unknown command")
        return await original(method, path, payload, timeout)
    phone.http = http
    text, _ = asyncio.run(Session(phone).look(screenshot=False))
    assert text.splitlines()[0] == "Screen 430x932 pt · 1 elements" and "App:" not in text


def test_a_dead_line_during_look_is_not_swallowed(phone):
    original = phone.http

    async def http(method, path, payload=None, timeout=30.0):
        if path == "/wda/activeAppInfo":
            raise ConnectionLost("gone")
        if path == ui_module.SOURCE_PATH:
            return {"value": SETTINGS_TREE}
        return await original(method, path, payload, timeout)
    phone.http = http
    with pytest.raises(ConnectionLost):
        asyncio.run(Session(phone).look(screenshot=False))


def test_type_text_at_a_point_needs_an_input_there(phone):
    s = Session(phone)
    s.items = [ui_module.Element("e1", "input", "Email", rect=(10, 10, 200, 40), actions=("tap", "type")),
               ui_module.Element("e2", "button", "Go", rect=(10, 100, 80, 40), actions=("tap",))]
    with pytest.raises(Refused) as e:
        asyncio.run(s.type_text("hi", target=ui_module.Target(x=20.0, y=120.0)))  # over a button
    assert e.value.code == "not_actionable" and str(e.value).startswith("no_action: Nothing at (20, 120)")
    _keyboard_after(phone, 1)
    _focus(phone, [FIELD_RECT])
    asyncio.run(s.type_text("hi", target=ui_module.Target(x=20.0, y=20.0)))
    taps = [r for r in phone.requests if r[1].endswith("/actions")]
    assert taps and taps[0][2]["actions"][0]["actions"][0]["x"] == 110  # tapped the input's centre


KEYBOARD = ('<XCUIElementTypeKeyboard type="XCUIElementTypeKeyboard" enabled="true" x="0" y="600" '
            'width="430" height="300"/>')


FIELD_NODE = ('<XCUIElementTypeTextField type="XCUIElementTypeTextField" label="Email" name="Email" enabled="true" '
              'x="10" y="10" width="200" height="40"/>')  # the field of FIELD, where the tree shows it


def _keyboard_after(phone, polls: int, monkeypatch=None):
    """The source shows the keyboard from the `polls`-th read on (1: at once); returns the read counter."""
    reads = []
    original = phone.http

    async def http(method, path, payload=None, timeout=30.0):
        if path == ui_module.SOURCE_PATH:
            reads.append(1)
            up = len(reads) >= polls
            return {"value": SETTINGS_TREE.replace("</XCUIElementTypeApplication>",
                                                   FIELD_NODE + (KEYBOARD if up else "") + "</XCUIElementTypeApplication>")}
        return await original(method, path, payload, timeout)
    phone.http = http
    return reads


FIELD_RECT = (10, 10, 200, 40)


def _focus(phone, rects):
    """element/active answers each rect in turn (the last repeats): who has focus, as WebDriverAgent says it."""
    asked = []
    original = phone.http

    async def http(method, path, payload=None, timeout=30.0):
        if path.endswith("/element/active"):
            asked.append(1)
            return {"value": {"ELEMENT": f"f{min(len(asked), len(rects)) - 1}"}}
        if "/element/f" in path and path.endswith("/rect"):
            x, y, w, h = rects[int(path.split("/element/f")[1].split("/")[0])]
            return {"value": {"x": x, "y": y, "width": w, "height": h}}
        return await original(method, path, payload, timeout)
    phone.http = http
    return asked


FIELD = [ui_module.Element("e1", "input", "Email", rect=(10, 10, 200, 40), actions=("tap", "type"))]


def test_typing_needs_focus_not_a_keyboard_on_screen(phone, monkeypatch):
    """With a hardware keyboard iOS parks the software one below the screen: the field has focus all the same."""
    monkeypatch.setattr(session_module, "FOCUS_POLL", 0.01)
    monkeypatch.setattr(session_module, "FOCUS_WAIT", 0.1)
    reads = _keyboard_after(phone, 10 ** 9)  # no keyboard in the tree
    _focus(phone, [FIELD_RECT])
    s = Session(phone)
    s.items = list(FIELD)
    assert asyncio.run(s.type_text("hi", target=ui_module.Target(ref="e1"))) == "typed 2 characters"
    assert reads == [] or phone.requests[-1][1].endswith("/wda/keys")
    assert phone.requests[-1][1].endswith("/wda/keys")


def test_typing_focuses_the_field_at_the_point_it_is_given(phone, monkeypatch):
    """The daemon passes where the field shows when a bar or the keyboard covers its centre."""
    monkeypatch.setattr(session_module, "FOCUS_POLL", 0.01)
    monkeypatch.setattr(session_module, "FOCUS_WAIT", 0.1)
    _keyboard_after(phone, 10 ** 9)
    _focus(phone, [FIELD_RECT])
    s = Session(phone)
    s.items = list(FIELD)
    assert asyncio.run(s.type_text("hi", target=ui_module.Target(ref="e1"), at=(60.0, 15.0))) == "typed 2 characters"
    assert s.last_point == (60.0, 15.0)


def test_typing_into_a_field_that_never_gets_focus_is_refused(phone, monkeypatch):
    monkeypatch.setattr(session_module, "FOCUS_POLL", 0.01)
    monkeypatch.setattr(session_module, "FOCUS_WAIT", 0.1)
    s = Session(phone)  # element/active answers nothing
    s.items = list(FIELD)
    with pytest.raises(Refused) as e:
        asyncio.run(s.type_text("hi", target=ui_module.Target(ref="e1")))
    assert e.value.code == "not_actionable"
    assert str(e.value) == "no_action: e1 input \"Email\" @10,10 200x40 didn't take focus."
    assert not any(r[1].endswith("/wda/keys") for r in phone.requests)


def test_replace_says_when_the_focused_field_could_not_be_found(phone, monkeypatch):
    monkeypatch.setattr(session_module, "FOCUS_POLL", 0.01)
    _keyboard_after(phone, 1)
    s = Session(phone)
    s.items = list(FIELD)
    text = asyncio.run(s.type_text("hi", replace=True))  # into whatever has focus: none has
    assert text.startswith("typed 2 characters") and "couldn't find the focused field to clear" in text


def _typing(phone, monkeypatch, **kwargs):
    """Type "hi"-like text into the field at the fake phone and return the requests the typing made."""
    monkeypatch.setattr(session_module, "FOCUS_POLL", 0.01)
    _keyboard_after(phone, 1)
    _focus(phone, [FIELD_RECT])
    s = Session(phone)
    s.items = _field(kwargs.pop("value", "old"))  # some text in it, so replace has something to clear
    text = kwargs.pop("text", "hi")
    asyncio.run(s.type_text(text, target=ui_module.Target(ref="e1"), **kwargs))
    return [(m, p, v) for m, p, v in phone.requests if p.endswith(("/clear", "/wda/keys"))]


def test_replace_clears_the_field_its_focus_check_found_without_asking_again(phone, monkeypatch):
    monkeypatch.setattr(session_module, "FOCUS_POLL", 0.01)
    _keyboard_after(phone, 1)
    asked = _focus(phone, [FIELD_RECT])
    s = Session(phone)
    s.items = [ui_module.Element("e1", "input", "Email", value="old", rect=(10, 10, 200, 40), actions=("tap", "type"))]
    asyncio.run(s.type_text("hi", replace=True, target=ui_module.Target(ref="e1")))
    assert len(asked) == 1  # element/active cost about 0.16 s a time on the phone
    assert [p for _, p, _ in phone.requests if p.endswith("/clear")] == ["/session/S/element/f0/clear"]


def _field(value: str = "", placeholder: str = "") -> list:
    return [ui_module.Element("e1", "input", "Email", value=value, rect=(10, 10, 200, 40), actions=("tap", "type"),
                              placeholder=placeholder)]


def test_typing_again_into_the_field_it_just_typed_into_skips_the_tap_and_the_clear(phone, monkeypatch):
    """The runner types into one field 20 times. With the field still focused by Tapwright's own typing and
    nothing done since, its tap (0.45 s) and clear (0.4 s with text) are skipped: one element/active confirms
    the focus, and backspaces go out with the keys, the cursor being at the end of its own text."""
    monkeypatch.setattr(session_module, "FOCUS_POLL", 0.01)
    _keyboard_after(phone, 1)
    asked = _focus(phone, [FIELD_RECT])
    s = Session(phone)
    s.items = _field("old")
    asyncio.run(s.type_text("w1", replace=True, target=ui_module.Target(ref="e1")))
    before = len(phone.requests)
    s.items = _field("w1")  # what the answer's read shows
    asyncio.run(s.type_text("w2", replace=True, target=ui_module.Target(ref="e1")))
    after = [(p, v) for _, p, v in phone.requests[before:]]  # (_focus answers element/active unlogged)
    assert [p.rsplit("/", 1)[1] for p, _ in after] == ["keys"] and len(asked) == 2
    assert after[-1][1] == {"value": list("\b\bw2")}


def test_after_any_other_action_typing_taps_the_field_again(phone, monkeypatch):
    monkeypatch.setattr(session_module, "FOCUS_POLL", 0.01)
    _keyboard_after(phone, 1)
    _focus(phone, [FIELD_RECT])
    s = Session(phone)
    s.items = _field("old")
    asyncio.run(s.type_text("w1", replace=True, target=ui_module.Target(ref="e1")))
    s.items = _field("w1")
    asyncio.run(s.swipe(5.0, 400.0, 5.0, 100.0))  # the content moved: focus and cursor may have too
    before = len(phone.requests)
    asyncio.run(s.type_text("w2", replace=True, target=ui_module.Target(ref="e1")))
    assert [p.rsplit("/", 1)[1] for _, p, _ in phone.requests[before:]][:1] == ["actions"]  # tapped first


def test_when_focus_moved_by_itself_typing_taps_the_field_again(phone, monkeypatch):
    monkeypatch.setattr(session_module, "FOCUS_POLL", 0.01)
    _keyboard_after(phone, 1)
    _focus(phone, [FIELD_RECT])
    s = Session(phone)
    s.items = _field("old")
    asyncio.run(s.type_text("w1", replace=True, target=ui_module.Target(ref="e1")))
    s.items = _field("w1")
    original = phone.http
    moved = []

    async def http(method, path, payload=None, timeout=30.0):
        if path.endswith("/element/active") and not moved:
            moved.append(1)
            return {"value": {"ELEMENT": "next-field"}}  # the app moved focus on (a code field does)
        return await original(method, path, payload, timeout)
    phone.http = http
    before = len(phone.requests)
    asyncio.run(s.type_text("w2", replace=True, target=ui_module.Target(ref="e1")))
    kinds = [p.rsplit("/", 1)[1] for _, p, _ in phone.requests[before:]]
    assert "actions" in kinds and "clear" in kinds  # the full path: tap, check, clear, keys


def test_submit_ends_the_shortcut_and_an_empty_field_is_not_cleared(phone, monkeypatch):
    monkeypatch.setattr(session_module, "FOCUS_POLL", 0.01)
    _keyboard_after(phone, 1)
    _focus(phone, [FIELD_RECT])
    s = Session(phone)
    s.items = _field("Email", placeholder="Email")  # empty: the tree shows the placeholder as its value
    asyncio.run(s.type_text("w1", replace=True, submit=True, target=ui_module.Target(ref="e1")))
    assert not [p for _, p, _ in phone.requests if p.endswith("/clear")]
    s.items = _field("w1", placeholder="Email")
    before = len(phone.requests)
    asyncio.run(s.type_text("w2", replace=True, target=ui_module.Target(ref="e1")))
    assert [p.rsplit("/", 1)[1] for _, p, _ in phone.requests[before:]][:1] == ["actions"]  # return may move focus


def test_replace_clears_the_field_before_the_keys_go_out(phone, monkeypatch):
    sent = _typing(phone, monkeypatch, replace=True)
    assert [p.rsplit("/", 1)[1] for _, p, _ in sent] == ["clear", "keys"]


def test_without_replace_nothing_is_cleared(phone, monkeypatch):
    sent = _typing(phone, monkeypatch, replace=False)
    assert [p.rsplit("/", 1)[1] for _, p, _ in sent] == ["keys"]


def test_submit_sends_return_after_the_text(phone, monkeypatch):
    sent = _typing(phone, monkeypatch, submit=True)
    assert sent[-1][2] == {"value": ["h", "i", "\n"]}


def test_multi_line_text_is_sent_intact(phone, monkeypatch):
    sent = _typing(phone, monkeypatch, text="a\nb  c")
    assert sent[-1][2] == {"value": ["a", "\n", "b", " ", " ", "c"]}


def _png(width: int) -> str:
    import base64
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, 10)).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def test_snapshot_is_the_tree_the_app_and_the_scale_and_sets_items(phone):
    answers = {ui_module.SOURCE_PATH: {"value": SETTINGS_TREE},
               "/wda/activeAppInfo": {"value": {"bundleId": "com.apple.Preferences", "name": "Settings"}},
               "/session/S/wda/screen": {"value": {"scale": 2}}}
    original = phone.http

    async def http(method, path, payload=None, timeout=30.0):
        if path in answers:
            return answers[path]
        return await original(method, path, payload, timeout)
    phone.http = http
    s = Session(phone)
    asyncio.run(s.read_scale())
    snap = asyncio.run(s.snapshot())
    assert [e.line() for e in snap.tree.items] == ['e1 button "Wi-Fi" id=wifi @16,215 398x52 [tap]']
    assert snap.app == ("Settings", "com.apple.Preferences") == s.app and snap.scale == 2.0
    assert s.items is snap.tree.items
    asyncio.run(s.reset())
    assert s.app is None and s.scale is None


def test_the_scale_comes_from_a_screenshot_when_the_screen_endpoint_has_none(phone):
    original = phone.http

    async def http(method, path, payload=None, timeout=30.0):
        if path == "/session/S/wda/screen":
            raise DeviceError("HTTP 404: unknown command")  # inside an app other than SpringBoard
        if path == "/screenshot":
            return {"value": _png(1290)}
        return await original(method, path, payload, timeout)
    phone.http = http
    s = Session(phone)
    assert asyncio.run(s.read_scale()) == 3.0 and s.scale == 3.0  # 1290 px / 430 pt
    count = len(phone.requests)
    asyncio.run(s.read_scale())
    assert len(phone.requests) == count  # read once


def test_an_unknown_scale_is_remembered_and_snapshots_never_read_it(phone):
    original = phone.http

    async def http(method, path, payload=None, timeout=30.0):
        phone.requests.append((method, path, payload))
        if path == "/session/S/wda/screen":
            raise DeviceError("HTTP 404: unknown command")
        if path == "/screenshot":
            raise DeviceError("HTTP 500: no picture")
        if path == ui_module.SOURCE_PATH:
            return {"value": SETTINGS_TREE}
        phone.requests.pop()  # the recorder notes it itself
        return await original(method, path, payload, timeout)
    phone.http = http
    s = Session(phone)
    assert asyncio.run(s.read_scale()) is None
    paths = lambda: [p for _, p, _ in phone.requests]  # noqa: E731
    assert paths().count("/screenshot") == 1
    for _ in range(2):
        text, _ = asyncio.run(s.look(screenshot=False))
    assert paths().count("/screenshot") == 1 and paths().count("/session/S/wda/screen") == 1
    assert text.splitlines()[0] == "Screen 430x932 pt · 1 elements"
    asyncio.run(s.read_scale(refresh=True))  # a new connection reads it again
    assert paths().count("/screenshot") == 2


def test_screenshot_png_is_the_phones_screenshot(phone):
    png = image_bytes((4, 8), (1, 2, 3), "PNG")

    class WithScreenshot(Recorder):
        async def http(self, method, path, payload=None, timeout=30.0):
            if path.endswith("/screenshot"):
                self.requests.append((method, path, payload))
                return {"value": base64.b64encode(png).decode()}
            return await super().http(method, path, payload, timeout)
    s = Session(WithScreenshot())
    assert asyncio.run(s.screenshot_png()) == png


NAV_TREE = ('<AppiumAUT><XCUIElementTypeApplication type="XCUIElementTypeApplication" x="0" y="0" width="430" '
            'height="932"><XCUIElementTypeNavigationBar type="XCUIElementTypeNavigationBar" x="0" y="59" width="430" '
            'height="44"><XCUIElementTypeButton type="XCUIElementTypeButton" label="Back" x="8" y="59" width="80" '
            'height="44"/></XCUIElementTypeNavigationBar></XCUIElementTypeApplication></AppiumAUT>')


def with_answers(phone, answers):
    original = phone.http

    async def http(method, path, payload=None, timeout=30.0):
        if path in answers:
            phone.requests.append((method, path, payload))
            return answers[path]
        return await original(method, path, payload, timeout)
    phone.http = http


def test_back_point_and_foreground_come_from_the_phones_tree(phone):
    with_answers(phone, {ui_module.SOURCE_PATH: {"value": NAV_TREE},
                         "/wda/activeAppInfo": {"value": {"bundleId": "com.apple.Preferences", "name": "Settings"}}})
    s = Session(phone)
    assert asyncio.run(s.back_point()) == ui_module.Back(True, (48.0, 81.0))
    assert asyncio.run(s.foreground_bundle_id()) == "com.apple.Preferences"


def test_the_header_names_the_app_from_the_installed_list(phone):
    class Apps(Recorder):
        async def user_apps(self):
            return [{"bundle_id": "app.example.mobile", "name": "Example", "version": "1", "build": "1"}]
    phone = Apps()
    phone.via = "USB"
    with_answers(phone, {ui_module.SOURCE_PATH: {"value": SETTINGS_TREE},
                         "/wda/activeAppInfo": {"value": {"bundleId": "app.example.mobile", "name": "app.example.mobile"}}})
    s = Session(phone)
    asyncio.run(s.read_app_names())
    text, _ = asyncio.run(s.look(screenshot=False))
    assert "App: Example (app.example.mobile)" in text.splitlines()


ROOTED_TREE = ('<XCUIElementTypeApplication type="XCUIElementTypeApplication" name="Example" label="Example" '
               'x="0" y="0" width="430" height="932" bundleId="app.example.mobile">'
               '<XCUIElementTypeWindow type="XCUIElementTypeWindow" x="0" y="0" width="430" height="932">'
               '<XCUIElementTypeButton type="XCUIElementTypeButton" name="OK" label="OK" x="10" y="100" width="100" '
               'height="44" enabled="true"/></XCUIElementTypeWindow></XCUIElementTypeApplication>')


def test_the_foreground_app_comes_from_the_trees_root_with_no_extra_request(phone):
    """The tree's root names the app (bundleId, label): asking activeAppInfo as well cost 70 ms an answer."""
    class Apps(Recorder):
        async def user_apps(self):
            return [{"bundle_id": "app.example.mobile", "name": "Example App", "version": "1", "build": "1"}]
    phone = Apps()
    with_answers(phone, {ui_module.SOURCE_PATH: {"value": ROOTED_TREE}})
    s = Session(phone)
    asyncio.run(s.read_app_names())
    assert asyncio.run(s.snapshot()).app == ("Example App", "app.example.mobile")
    assert not [p for _, p, _ in phone.requests if p.endswith("/wda/activeAppInfo")]
    unlisted = Recorder()  # not in the installed list (a system app): the root's own label
    with_answers(unlisted, {ui_module.SOURCE_PATH: {"value": ROOTED_TREE}})
    assert asyncio.run(Session(unlisted).snapshot()).app == ("Example", "app.example.mobile")


def test_springboard_is_the_home_screen_and_an_unknown_app_rereads_the_list(phone):
    reads = []

    class Apps(Recorder):
        async def user_apps(self):
            reads.append(1)
            return [{"bundle_id": "app.example.new", "name": "New One", "version": "1", "build": "1"}] if len(reads) > 1 else []
    phone = Apps()
    answers = {ui_module.SOURCE_PATH: {"value": SETTINGS_TREE},
               "/wda/activeAppInfo": {"value": {"bundleId": "com.apple.springboard", "name": "SpringBoard"}}}
    with_answers(phone, answers)
    s = Session(phone)
    asyncio.run(s.read_app_names())
    assert asyncio.run(s.snapshot()).app == ("Home screen", "com.apple.springboard")
    answers["/wda/activeAppInfo"] = {"value": {"bundleId": "app.example.new", "name": "app.example.new"}}
    assert asyncio.run(s.snapshot()).app == ("New One", "app.example.new") and len(reads) == 2
    assert asyncio.run(s.snapshot()).app == ("New One", "app.example.new") and len(reads) == 2
    answers["/wda/activeAppInfo"] = {"value": {"bundleId": "com.apple.Preferences", "name": "Settings"}}
    assert asyncio.run(s.snapshot()).app == ("Settings", "com.apple.Preferences")  # a system app: WDA's own name
    assert len(reads) == 3  # asked once for it, then remembered as not installed by you
    asyncio.run(s.snapshot())
    assert len(reads) == 3


def test_typing_waits_until_the_target_not_the_old_field_has_focus(phone, monkeypatch):
    monkeypatch.setattr(session_module, "FOCUS_POLL", 0.01)
    _keyboard_after(phone, 1)  # the keyboard is already up, for another field
    asked = _focus(phone, [(10, 300, 200, 40), (10, 300, 200, 40), (11, 11, 199, 41)])  # old, old, then the target
    s = Session(phone)
    s.items = list(FIELD)
    asyncio.run(s.type_text("hi", target=ui_module.Target(ref="e1")))
    assert len(asked) == 3 and phone.requests[-1][1].endswith("/wda/keys")


def test_typing_when_focus_never_moves_to_the_target_types_nothing(phone, monkeypatch):
    monkeypatch.setattr(session_module, "FOCUS_POLL", 0.01)
    monkeypatch.setattr(session_module, "FOCUS_WAIT", 0.1)
    _keyboard_after(phone, 1)
    _focus(phone, [(10, 300, 200, 40)])
    s = Session(phone)
    s.items = list(FIELD)
    with pytest.raises(Refused) as e:
        asyncio.run(s.type_text("hi", target=ui_module.Target(ref="e1")))
    assert e.value.code == "not_actionable"
    assert str(e.value) == "no_action: e1 input \"Email\" @10,10 200x40 didn't take focus."
    assert not any(r[1].endswith("/wda/keys") for r in phone.requests)


PARKED_KEYBOARD = ('<XCUIElementTypeKeyboard type="XCUIElementTypeKeyboard" enabled="true" x="0" y="932" '
                   'width="430" height="243"/>')


def _moved_field(phone, attributes: dict | None):
    """A hardware keyboard (the keyboard node is off screen) and a field the tap scrolled up by 120 pt. `attributes`
    is what element/<id>/attribute/<name> answers (None: the phone has no answer)."""
    moved = FIELD_NODE.replace('y="10"', 'y="130"').replace('name="Email"', 'name="email-field"')
    original = phone.http

    async def http(method, path, payload=None, timeout=30.0):
        if path == ui_module.SOURCE_PATH:
            return {"value": SETTINGS_TREE.replace("</XCUIElementTypeApplication>",
                                                   moved + PARKED_KEYBOARD + "</XCUIElementTypeApplication>")}
        if path.endswith("/element/active"):
            return {"value": {"ELEMENT": "f0"}}
        if path.endswith("/element/f0/rect"):
            return {"value": {"x": 10, "y": 130, "width": 200, "height": 40}}
        if "/element/f0/attribute/" in path:
            return {"value": (attributes or {}).get(path.rsplit("/", 1)[1])}
        return await original(method, path, payload, timeout)
    phone.http = http


def test_typing_into_a_field_the_keyboard_moved_goes_by_identity(phone, monkeypatch):
    monkeypatch.setattr(session_module, "FOCUS_POLL", 0.01)
    monkeypatch.setattr(session_module, "FOCUS_WAIT", 0.1)
    _moved_field(phone, {"name": "email-field", "label": "Email"})
    s = Session(phone)
    s.items = [ui_module.Element("e1", "input", "Email", "email-field", rect=(10, 10, 200, 40), actions=("tap", "type"))]
    assert asyncio.run(s.type_text("hi", target=ui_module.Target(ref="e1"))) == "typed 2 characters"
    assert phone.requests[-1][1].endswith("/wda/keys")


def test_typing_into_a_moved_field_falls_back_to_its_rect_in_a_fresh_read(phone, monkeypatch):
    monkeypatch.setattr(session_module, "FOCUS_POLL", 0.01)
    monkeypatch.setattr(session_module, "FOCUS_WAIT", 0.1)
    _moved_field(phone, None)  # no attributes: only the rect says it is the field (found again by its identifier)
    s = Session(phone)
    s.items = [ui_module.Element("e1", "input", "Email", "email-field", rect=(10, 10, 200, 40), actions=("tap", "type"))]
    assert asyncio.run(s.type_text("hi", target=ui_module.Target(ref="e1"))) == "typed 2 characters"


def test_typing_is_refused_when_another_field_has_focus_after_the_move(phone, monkeypatch):
    monkeypatch.setattr(session_module, "FOCUS_POLL", 0.01)
    monkeypatch.setattr(session_module, "FOCUS_WAIT", 0.1)
    _moved_field(phone, {"name": "other-field", "label": "Other"})
    original = phone.http

    async def http(method, path, payload=None, timeout=30.0):
        if path.endswith("/element/f0/rect"):
            return {"value": {"x": 10, "y": 500, "width": 200, "height": 40}}
        return await original(method, path, payload, timeout)
    phone.http = http
    s = Session(phone)
    s.items = [ui_module.Element("e1", "input", "Email", "email-field", rect=(10, 10, 200, 40), actions=("tap", "type"))]
    with pytest.raises(Refused):
        asyncio.run(s.type_text("hi", target=ui_module.Target(ref="e1")))


def test_dismiss_keyboard_is_true_on_success_false_on_a_refusal_and_raises_on_a_dead_line(phone):
    s = Session(phone)
    assert asyncio.run(s.dismiss_keyboard()) is True
    assert phone.requests[-1] == ("POST", "/session/S/wda/keyboard/dismiss", {})
    phone.fail["/wda/keyboard/dismiss"] = DeviceError("WebDriverAgent: unknown error: no dismiss key")
    assert asyncio.run(s.dismiss_keyboard()) is False
    phone.fail["/wda/keyboard/dismiss"] = ConnectionLost("line is dead")
    with pytest.raises(ConnectionLost):
        asyncio.run(s.dismiss_keyboard())

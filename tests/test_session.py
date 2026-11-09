"""Session without a phone: a recording device answers WebDriverAgent's requests."""
import asyncio
import base64

import pytest
from fakes import image_bytes

from tapwright import session as session_module
from tapwright import ui as ui_module
from tapwright.control import Refused
from tapwright.device import ConnectionLost, DeviceError, NotSetUp
from tapwright.session import Session


class Recorder:
    def __init__(self):
        self.rsd, self.tunnel, self.runner, self.via = "tunnel", None, None, "USB"
        self.requests, self.started, self.closed = [], [], False
        self.fail = {}  # path suffix -> exception to raise once
        self.close_error = None

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
            return {"value": True}
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
    asyncio.run(s.type_text("hi", target=ui_module.Target(x=20.0, y=20.0)))
    taps = [r for r in phone.requests if r[1].endswith("/actions")]
    assert taps and taps[0][2]["actions"][0]["actions"][0]["x"] == 110  # tapped the input's centre


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

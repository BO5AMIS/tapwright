"""The fork's compact tree read (route version 4, GET /wda/tw/tree; 1.6 E2) and the tap that checks the app in front
itself (E3). The compact answers here are made from recorded /source trees the way the fork builds them
(WebDriverAgentLib/Commands/TWTreeCommands.m: the same attribute values, only the ones the list reads), so the list
read from one must be the list read from the other. tests/data/real: the lab app's screens, recorded on the phone."""
import asyncio
import math
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from tapwright import session as session_module
from tapwright import ui
from tapwright.device import ConnectionLost, DeviceError
from tapwright.session import FrontUnchecked, Session

DATA = Path(__file__).parent / "data"
SCREENS = ["calculator", "settings_main", "settings_alert_keyboard", "real/lab-keyboard-up", "real/lab-sheet"]
PORTRAIT, LANDSCAPE = (430.0, 932.0), (932.0, 430.0)


def load(name: str) -> str:
    """A recorded tree as /source sends it for the app in front (the hand-written ones were wrapped in an AppiumAUT
    root, which only a scoped /source writes)."""
    return (DATA / f"{name}.xml").read_text(encoding="utf-8").replace("<AppiumAUT>", "").replace("</AppiumAUT>", "")


def compact(source: str) -> dict:
    """The node the fork answers for this /source tree: type without the prefix, value, name, label and placeholder
    where /source has them, traits when not empty, the frame (zeros when not a number), `d` when not enabled, the
    application's pid and bundle id, and the children."""
    root = ET.fromstring(source)

    def node(n: ET.Element) -> dict:
        out: dict = {"t": (n.get("type") or n.tag).removeprefix("XCUIElementType")}
        for key, attr in (("v", "value"), ("n", "name"), ("l", "label"), ("p", "placeholderValue")):
            if n.get(attr) is not None:
                out[key] = n.get(attr)
        if n.get("traits"):
            out["tr"] = n.get("traits")
        rect = [float(n.get(k, "0")) for k in ("x", "y", "width", "height")]
        rect = rect if all(math.isfinite(v) for v in rect) else [0.0] * 4
        out["r"] = [int(v) if v.is_integer() else v for v in rect]
        if n.get("enabled") == "false":
            out["d"] = 1
        if n.get("processId"):
            out["pid"] = int(n.get("processId"))
        if n.get("bundleId"):
            out["b"] = n.get("bundleId")
        if len(n):
            out["c"] = [node(child) for child in n]
        return out
    return node(root)


def same_reads(a: str, b: str, screen: tuple[float, float]) -> None:
    for keyboard in (False, True):
        one, two = ui.read(a, screen, keyboard=keyboard), ui.read(b, screen, keyboard=keyboard)
        assert one == two
        assert ui.describe(one.items, screen, 3.0, one.app, one.alert, one.keyboard, one.more, (one.above, one.below)) \
            == ui.describe(two.items, screen, 3.0, two.app, two.alert, two.keyboard, two.more, (two.above, two.below))
    assert ui.back_button(a, screen) == ui.back_button(b, screen)


@pytest.mark.parametrize("name", SCREENS)
@pytest.mark.parametrize("screen", [PORTRAIT, LANDSCAPE])
def test_the_compact_read_lists_what_source_lists(name, screen):
    source = load(name)
    again = ui.xml_of_tree(compact(source))
    same_reads(source, again, screen)
    root = session_module.ROOT_SIZE.search(again[:2000])
    first = session_module.ROOT_SIZE.search(source[:2000])
    assert (root and root.groups()) == (first and first.groups())


def test_what_xml_needs_escaped_and_what_an_attribute_would_lose_come_back_whole():
    label = 'Say "hi" & <wave>\ttwo\nlines\r'
    tree = {"t": "Application", "l": "Lab", "n": "Lab", "r": [0, 0, 430, 932], "pid": 7, "b": "com.example.lab",
            "c": [{"t": "Button", "l": label, "n": "go", "v": "1 > 0", "r": [10.5, 20, 100, 44], "tr": "Button, Selected"},
                  {"t": "TextField", "l": "", "p": "Your name", "v": "Your name", "r": [10, 80, 300, 40], "d": 1},
                  {"t": "StaticText", "l": "€ ‎42 ✓", "r": [10, 140, 300, 20]}]}
    root = ET.fromstring(ui.xml_of_tree(tree))
    button, field, text = list(root.iter())[1:]
    assert root.get("bundleId") == "com.example.lab" and root.get("processId") == "7" and root.get("traits") == ""
    assert button.get("label") == label and button.get("value") == "1 > 0" and button.get("x") == "10.5"
    assert button.get("traits") == "Button, Selected" and button.get("enabled") == "true"
    assert field.get("enabled") == "false" and field.get("placeholderValue") == "Your name" and field.get("label") == ""
    assert text.get("label") == "€ ‎42 ✓" and text.get("value") is None and text.get("name") is None
    assert ui.read(ui.xml_of_tree(tree), PORTRAIT).items[0].label == label.strip()


def test_a_springboard_tree_with_a_banner_reads_as_one_from_source():
    """What the session checks on the text itself: SpringBoard at the root, a banner, an alert."""
    source = ('<?xml version="1.0" encoding="UTF-8"?>\n<XCUIElementTypeApplication type="XCUIElementTypeApplication" '
              'name=" " label=" " enabled="true" x="0" y="0" width="430" height="932" index="0" traits="" '
              'processId="34" bundleId="com.apple.springboard">\n  <XCUIElementTypeOther type="XCUIElementTypeOther" '
              'name="NotificationShortLookView" label="Mail, now" enabled="true" x="8" y="60" width="414" height="80" '
              'index="0" traits=""/>\n</XCUIElementTypeApplication>')
    again = ui.xml_of_tree(compact(source))
    assert ui.SPRINGBOARD_ROOT in again[:600] and session_module.BANNER_ID in again
    same_reads(source, again, PORTRAIT)


@pytest.mark.parametrize("bad", [None, [], {"t": "Button"}, {"t": "Button", "r": [0, 0, 1]},
                                 {"t": "Bu tton", "r": [0, 0, 1, 1]}, {"t": "Button", "r": [0, 0, "a", 1]},
                                 {"t": "Button", "r": [0, 0, 1, 1], "c": {"t": "Other"}},
                                 {"t": "Button", "r": [0, 0, 1, 1], "c": [7]}])
def test_an_answer_that_isnt_a_tree_is_refused(bad):
    with pytest.raises(ValueError):
        ui.xml_of_tree(bad)


def test_a_deep_tree_is_written_without_recursion():
    node: dict = {"t": "Other", "r": [0, 0, 10, 10]}
    root = node
    for _ in range(3000):
        child = {"t": "Other", "r": [0, 0, 10, 10]}
        node["c"] = [child]
        node = child
    assert ui.xml_of_tree(root).count("<XCUIElementTypeOther") == 3001


# ---- the session ----

class Runner:
    """A recording phone with Tapwright's runner of route version `version`: GET /wda/tw/tree answers `screens`
    (one /source tree each, the last one repeats) the way the fork does, "unchanged" when `since` is the token."""

    def __init__(self, version=4, screens=("settings_main",)):
        self.rsd, self.tunnel, self.runner, self.via = "tunnel", None, None, "USB"
        self.version, self.screens, self.requests = version, [load(n) for n in screens], []
        self.read_no = 0
        self.tree_error: Exception | None = None
        self.tree_value: object = None  # answered instead of a tree when set
        self.tap_error: Exception | None = None
        self.in_front = "com.example.lab"  # what the runner's tap check finds in front

    async def foreground_apps(self):
        return []

    async def connect(self):
        self.rsd = "tunnel"

    async def mount(self):
        pass

    async def start_wda(self, runner):
        pass

    async def close(self):
        pass

    def screen(self) -> str:
        source = self.screens[min(self.read_no, len(self.screens) - 1)]
        self.read_no += 1
        return source

    async def http(self, method, path, payload=None, timeout=30.0):
        self.requests.append((method, path, payload))
        route = path.split("?", 1)[0]
        if path == "/session":
            return {"sessionId": "S"}
        if route.endswith("/window/size"):
            return {"value": {"width": 430, "height": 932}}
        if route.endswith("/wda/locked"):
            return {"value": False}
        if route.endswith("/source"):
            return {"value": self.screen()}
        if route.endswith("/wda/tw/version"):
            if not self.version:
                raise DeviceError(f"WebDriverAgent: unknown command: Unhandled endpoint: {path}")
            return {"value": {"tapwright": self.version}}
        if route.endswith("/wda/tw/tree"):
            if self.version < 4:
                raise DeviceError(f"WebDriverAgent: unknown command: Unhandled endpoint: {path}")
            if self.tree_error is not None:
                raise self.tree_error
            if self.tree_value is not None:
                return {"value": self.tree_value}
            source = self.screen()
            token = f"t{hash(source) & 0xffff:x}"
            if path.endswith(f"?since={token}"):
                return {"value": {"token": token, "unchanged": True, "ms": {"snapshot": 90}}}
            return {"value": {"token": token, "tree": compact(source), "ms": {"snapshot": 90}}}
        if route.endswith("/wda/tw/tap"):
            if self.version < 1:
                raise DeviceError(f"WebDriverAgent: unknown command: Unhandled endpoint: {path}")
            if self.tap_error is not None:
                raise self.tap_error
            apps = (payload or {}).get("apps")
            if apps is not None and self.in_front not in apps:
                raise DeviceError(f"WebDriverAgent: invalid element state: tapwright front: {self.in_front} is in "
                                  "front, not one of the given apps; nothing was touched")
            return {"value": {"ms": 240}}
        return {"value": None}


@pytest.fixture(autouse=True)
def runner_state(monkeypatch):
    monkeypatch.setattr(session_module.store, "load_state", lambda: {"runner_id": "runner"})
    for name in ("FAST_TAP", "FAST_READ", "FRONT_IN_TAP"):
        monkeypatch.setattr(session_module, name, "1")


def routes(runner: Runner) -> list[str]:
    return [p.replace("/session/S", "") for _, p, _ in runner.requests
            if "/wda/tw/" in p or p.startswith("/source") or p.endswith("/actions")]


def test_a_version_4_runner_reads_through_the_compact_route_and_lists_the_same():
    runner = Runner()
    s = Session(runner)
    tree = asyncio.run(s.refresh())
    assert routes(runner) == ["/wda/tw/version", "/wda/tw/tree"] and s.fast_read is True
    assert tree == ui.read(load("settings_main"), s.screen)


def test_an_unchanged_screen_comes_back_without_its_tree_and_reads_the_same():
    runner = Runner()
    s = Session(runner)
    first = asyncio.run(s.refresh())
    again = asyncio.run(s.refresh())
    sent = [p for _, p, _ in runner.requests if "/wda/tw/tree" in p]
    assert sent[1].endswith(f"?since={s.last_compact[0]}") and again == first
    runner.screens.append(load("calculator"))  # the screen changes: the whole tree comes back
    runner.read_no = len(runner.screens) - 1
    assert asyncio.run(s.refresh()) == ui.read(load("calculator"), s.screen)


def test_an_unchanged_answer_to_a_token_never_sent_reads_the_whole_tree():
    runner = Runner()
    s = Session(runner)
    asyncio.run(s.refresh())
    s.last_compact = ("other", "<broken")  # (as if the token were another's)
    tree = asyncio.run(s.refresh())
    assert tree == ui.read(load("settings_main"), s.screen)


def test_a_v3_runner_and_appiums_read_source_and_ask_the_version_once():
    for version in (3, 0):
        runner = Runner(version=version)
        s = Session(runner)
        asyncio.run(s.refresh())
        asyncio.run(s.refresh())
        assert routes(runner) == ["/wda/tw/version", "/source?format=xml&excluded_attributes=visible,accessible",
                                  "/source?format=xml&excluded_attributes=visible,accessible"], version


def test_a_route_the_runner_lacks_after_all_falls_back_to_source_for_good():
    runner = Runner()
    runner.tree_error = DeviceError("WebDriverAgent: unknown command: Unhandled endpoint")
    s = Session(runner)
    asyncio.run(s.refresh())
    asyncio.run(s.refresh())
    assert routes(runner)[1:] == ["/wda/tw/tree", "/source?format=xml&excluded_attributes=visible,accessible",
                                  "/source?format=xml&excluded_attributes=visible,accessible"] and s.fast_read is False


def test_an_unreadable_answer_reads_source_this_time_and_tries_again_next_time():
    runner = Runner()
    runner.tree_value = {"token": "x", "tree": {"t": "Application"}}
    s = Session(runner)
    assert asyncio.run(s.refresh()) == ui.read(load("settings_main"), s.screen)
    runner.tree_value = None
    asyncio.run(s.refresh())
    assert routes(runner)[1:] == ["/wda/tw/tree", "/source?format=xml&excluded_attributes=visible,accessible",
                                  "/wda/tw/tree"]


def test_a_dropped_line_during_a_compact_read_is_raised_as_from_source():
    runner = Runner()
    s = Session(runner)
    asyncio.run(s.ensure())
    runner.tree_error = ConnectionLost("the phone didn't answer within 10 s")
    with pytest.raises(ConnectionLost):
        asyncio.run(s.refresh())


def test_the_off_switch_and_a_reset_keep_reads_honest(monkeypatch):
    runner = Runner()
    s = Session(runner)
    asyncio.run(s.refresh())
    asyncio.run(s.reset())
    assert s.last_compact is None  # a new runner's token means nothing to this one
    monkeypatch.setattr(session_module, "FAST_READ", "0")
    runner.requests.clear()
    asyncio.run(s.refresh())
    assert [r for r in routes(runner) if "tree" in r or "source" in r] == [
        "/source?format=xml&excluded_attributes=visible,accessible"]


# ---- the tap that checks the app in front (E3) ----

def armed(runner: Runner) -> Session:
    s = Session(runner)
    asyncio.run(s.refresh())  # (the session knows its runner's version)
    assert s.front_by_touch() is (runner.version >= 4)
    s.front_apps = ("com.example.lab",)
    return s


def test_a_personal_tap_sends_the_given_apps_and_clears_the_check_once_touched():
    runner = Runner()
    s = armed(runner)
    asyncio.run(s.tap(ui.Target(x=30, y=40)))
    body = [b for _, p, b in runner.requests if p.endswith("/wda/tw/tap")][-1]
    assert body["apps"] == ["com.example.lab"] and s.front_apps is None


def test_another_app_in_front_touches_nothing_and_says_so():
    runner = Runner()
    runner.in_front = "com.example.mail"
    s = armed(runner)
    with pytest.raises(FrontUnchecked):
        asyncio.run(s.tap(ui.Target(x=30, y=40)))
    assert "/actions" not in routes(runner)  # never the other way round the check
    assert s.last_point is None


def test_no_tap_goes_round_the_check():
    """A v3 runner, landscape, the fast tap off or refused before playing: nothing is touched; the question comes."""
    for setup in ("v3", "landscape", "off", "refused", "unknown"):
        runner = Runner(version=3 if setup == "v3" else 4)
        s = armed(runner)
        if setup == "landscape":
            s.screen, s.turn = LANDSCAPE, 90
        if setup == "off":
            session_module.FAST_TAP = "0"
        if setup == "refused":
            runner.tap_error = DeviceError("WebDriverAgent: unknown error: too many touches are still being played")
        if setup == "unknown":
            runner.tap_error = DeviceError("WebDriverAgent: unknown command: Unhandled endpoint")
        try:
            with pytest.raises(FrontUnchecked):
                asyncio.run(s.tap(ui.Target(x=30, y=40)))
        finally:
            session_module.FAST_TAP = "1"
        assert "/actions" not in routes(runner), setup


def test_nothing_else_acts_before_the_checking_tap():
    runner = Runner()
    s = armed(runner)
    with pytest.raises(FrontUnchecked):
        asyncio.run(s.press_button("volumeUp"))
    with pytest.raises(FrontUnchecked):
        asyncio.run(s.swipe(10, 400, 10, 200))
    assert asyncio.run(s.foreground_bundle_id()) is None  # a question may still be asked


def test_a_runner_that_can_check_is_known_before_the_tap_relies_on_it(monkeypatch):
    runner = Runner()
    s = Session(runner)
    assert not s.front_by_touch()  # not asked yet: the separate question
    asyncio.run(s.refresh())
    assert s.front_by_touch()
    monkeypatch.setattr(session_module, "FRONT_IN_TAP", "0")
    assert not s.front_by_touch()
    assert not Session(Runner(version=3)).front_by_touch()


def test_the_timing_trace_notes_a_compact_read_as_a_read(tmp_path):
    import json

    from tapwright import trace
    path = tmp_path / "trace.jsonl"
    trace.disable()
    trace.enable(path)
    try:
        runner = Runner()
        s = Session(runner)
        asyncio.run(s.refresh())
        asyncio.run(s.refresh())
    finally:
        trace.disable()
    found = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    reads = [x for x in found if x["tool"] == "read"]
    assert [x.get("compact") for x in reads] == ["tree", "unchanged"]
    assert reads[0]["wda_ms"] == {"snapshot": 90}

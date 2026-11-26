"""Tapwright 2.0, worker D: perception that never stalls. The client side of runner route version 9 (bounded tree reads
with a per-app budget, the screen's size without reading the app, the AX timeout setting, OCR on the phone), and looks
and actions on a screen whose tree came back cut short (`tier` shallow or root)."""
import asyncio

import pytest

from fakes import DescribingSession, FakeSession, button
from test_daemon import finish, make, started, until_stuck
from test_session import Recorder

from tapwright import daemon as daemon_module
from tapwright import ocr
from tapwright import session as session_module
from tapwright import ui
from tapwright.control import Refused
from tapwright.device import AppNotAnswering, DeviceError
from tapwright.session import ReadBudget, Session

TIKTOK = "com.zhiliaoapp.musically"


def run(go):
    asyncio.run(go())


# ---- the runner (route version 9) ----

def _app(bundle=TIKTOK, label="TikTok", children=None):
    node = {"t": "Application", "r": [0, 0, 430, 932], "b": bundle, "l": label, "pid": 7}
    if children:
        node["c"] = children
    return node


BUTTON = {"t": "Button", "n": "lab-taps", "l": "Tap me", "r": [16, 197, 398, 120]}


class V9Runner(Recorder):
    """A runner that answers route version `version` (9: bounded reads). `trees` are the tree route's answers in turn
    (the last one stays); `ocr_value` is what its OCR route answers (an Exception is raised)."""

    def __init__(self, version=9):
        super().__init__()
        self.version = version
        self.trees = [{"token": "t1", "tree": _app(), "tier": "full", "truncated": False}]
        self.ocr_value: object = {"screen": [430, 932], "lines": []}

    async def http(self, method, path, payload=None, timeout=30.0):
        bare = path.replace("/session/S", "")
        for suffix in list(self.fail):
            if path.endswith(suffix):
                self.requests.append((method, path, payload))
                raise self.fail.pop(suffix)
        if bare == "/wda/tw/version":
            self.requests.append((method, path, payload))
            return {"value": {"tapwright": self.version}}
        if bare == "/wda/tw/screen":
            self.requests.append((method, path, payload))
            if self.version < 9:
                raise DeviceError("WebDriverAgent: unknown command: Unhandled endpoint")
            return {"value": {"width": 430, "height": 932, "scale": 3, "orientation": 1}}
        if bare.startswith("/wda/tw/tree"):
            self.requests.append((method, path, payload))
            answer = self.trees.pop(0) if len(self.trees) > 1 else self.trees[0]
            return {"value": dict(answer)}
        if bare.startswith("/wda/tw/ocr"):
            self.requests.append((method, path, payload))
            if isinstance(self.ocr_value, Exception):
                raise self.ocr_value
            return {"value": self.ocr_value}
        return await super().http(method, path, payload, timeout)

    def paths(self, part):
        return [p.replace("/session/S", "") for _, p, _ in self.requests if part in p]


@pytest.fixture
def state(monkeypatch):
    monkeypatch.setattr(session_module.store, "load_state", lambda: {"runner_id": "runner"})
    monkeypatch.setattr(session_module, "FAST_READ", "1")


def test_a_v9_session_starts_without_reading_the_app_and_sets_the_ax_timeout(state):
    runner = V9Runner()
    s = Session(runner)
    asyncio.run(s.ensure())
    paths = [p for _, p, _ in runner.requests]
    assert "/wda/tw/version" in paths and "/wda/tw/screen" in paths  # (session-less: the session isn't up yet)
    assert not any(p.endswith("/window/size") for p in paths)  # the screen's size reads nothing of the app
    assert s.screen == (430.0, 932.0) and s.scale == 3.0 and s.scale_read and s.bounded
    settings = next(body for m, p, body in runner.requests if p.endswith("/appium/settings"))["settings"]
    assert settings["twAXTimeout"] == session_module.AX_TIMEOUT == 3.0
    # each POST /session resets it, and only the capability bounds the session's own start (RUNNER-V9-PROTOCOL §2)
    assert paths.index("/wda/tw/version") < paths.index("/session")
    created = next(body for m, p, body in runner.requests if p == "/session")
    assert created == {"capabilities": {"alwaysMatch": {"twAXTimeout": 3.0}}}
    assert s.tw_version == 9 and s.fast_tap and s.fast_drag  # (the probe's answer: not asked again on a session route)
    asyncio.run(s.refresh())
    assert runner.paths("/wda/tw/version") == ["/wda/tw/version"]


def test_an_older_runner_starts_as_in_1_10(state):
    for runner in (V9Runner(version=8), Recorder()):
        s = Session(runner)
        asyncio.run(s.ensure())
        paths = [p for _, p, _ in runner.requests]
        assert "/session/S/window/size" in paths and "/wda/tw/screen" not in paths
        settings = next(body for m, p, body in runner.requests if p.endswith("/appium/settings"))["settings"]
        assert "twAXTimeout" not in settings and not s.bounded
        assert next(body for m, p, body in runner.requests if p == "/session") == {"capabilities": {"alwaysMatch": {}}}
    assert s.tw_version == 0 and s.fast_tap is False  # Appium's build said "unknown command": known, not asked again


def test_a_failed_version_probe_leaves_the_session_as_before(state):
    runner = V9Runner()
    runner.fail["/wda/tw/version"] = DeviceError("HTTP 500: boom")
    s = Session(runner)
    asyncio.run(s.ensure())
    assert "/session/S/window/size" in [p for _, p, _ in runner.requests] and s.fast_tap is None
    asyncio.run(s.refresh())  # the session's own route is asked then, as in 1.10
    assert runner.paths("/wda/tw/version") == ["/wda/tw/version", "/wda/tw/version"]


def test_the_ax_timeout_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("TAPWRIGHT_AX_TIMEOUT", "1.5")
    assert session_module._env_float("TAPWRIGHT_AX_TIMEOUT", 3.0) == 1.5
    monkeypatch.setenv("TAPWRIGHT_AX_TIMEOUT", "nonsense")
    assert session_module._env_float("TAPWRIGHT_AX_TIMEOUT", 3.0) == 3.0
    monkeypatch.setenv("TAPWRIGHT_AX_TIMEOUT", "-1")
    assert session_module._env_float("TAPWRIGHT_AX_TIMEOUT", 3.0) == 3.0


def test_a_v9_read_carries_the_budget_and_says_how_much_it_read(state):
    runner = V9Runner()
    runner.trees = [{"token": "t1", "tree": _app("com.example.lab", "Lab", [BUTTON]), "tier": "full",
                     "truncated": False}]
    s = Session(runner)
    tree = asyncio.run(s.refresh())
    assert runner.paths("/wda/tw/tree") == ["/wda/tw/tree?budget=3000&path=xctest"]
    assert tree.tier == "full" and not tree.truncated and s.last_tier == "full"
    assert [e.identifier for e in tree.items] == ["lab-taps"]
    asyncio.run(s.refresh())  # a whole tree is the next read's baseline
    assert runner.paths("/wda/tw/tree")[-1] == "/wda/tw/tree?budget=3000&path=xctest&since=t1"


def test_a_screen_that_doesnt_answer_comes_back_as_root_and_its_app_gets_the_short_budget(state):
    runner = V9Runner()
    runner.trees = [{"token": "r1", "tree": _app(), "tier": "root", "truncated": True, "why": "full: timeout"}]
    s = Session(runner)
    s.read_app = ("TikTok", TIKTOK)  # (the newest read named it)
    tree = asyncio.run(s.refresh())
    assert tree.tier == "root" and tree.truncated and tree.items == [] and s.last_why == "full: timeout"
    assert tree.app == ("TikTok", TIKTOK)  # the root still names the app (personal mode's scope needs it)
    assert s.last_compact is None  # a cut-short tree is no baseline for "unchanged"
    asyncio.run(s.refresh())
    assert runner.paths("/wda/tw/tree") == ["/wda/tw/tree?budget=3000&path=xctest", "/wda/tw/tree?budget=1500&path=direct"]
    # the app answers whole again (its profile reads in 0.2 s): the short budget was enough for that, and it stays (a
    # feed that reads whole for a moment and then not again paid the normal budget at every flip, phone 2026-11-26)
    runner.trees = [{"token": "f1", "tree": _app(children=[BUTTON]), "tier": "full", "truncated": False}]
    asyncio.run(s.refresh())
    asyncio.run(s.refresh())
    assert runner.paths("/wda/tw/tree")[-2:] == ["/wda/tw/tree?budget=1500&path=direct", "/wda/tw/tree?budget=1500&path=direct&since=f1"]


def test_a_short_budget_meant_for_another_app_is_read_again_with_the_normal_one(state):
    runner = V9Runner()
    s = Session(runner)
    asyncio.run(s.ensure())
    s.read_app = ("TikTok", TIKTOK)
    s.budgets.answered(TIKTOK, True, 3000)
    # Settings came forward: its first read had TikTok's short budget, and its long list didn't fit in it
    runner.trees = [{"token": "s1", "tree": _app("com.apple.Preferences", "Settings", [BUTTON]), "tier": "shallow",
                     "truncated": True},
                    {"token": "s2", "tree": _app("com.apple.Preferences", "Settings", [BUTTON]), "tier": "full",
                     "truncated": False}]
    tree = asyncio.run(s.refresh())
    assert runner.paths("/wda/tw/tree") == ["/wda/tw/tree?budget=1500&path=direct", "/wda/tw/tree?budget=3000&path=xctest"]
    assert tree.tier == "full" and not s.budgets.short("com.apple.Preferences")


def test_a_shallow_answer_and_a_truncated_flag_are_never_taken_for_a_whole_tree(state):
    runner = V9Runner()
    runner.trees = [{"token": "x", "tree": _app(children=[BUTTON]), "tier": "full", "truncated": True},
                    {"token": "y", "tree": _app(children=[BUTTON]), "tier": "odd", "truncated": True}]
    s = Session(runner)
    assert asyncio.run(s.refresh()).tier == "shallow"
    assert asyncio.run(s.refresh()).tier == "shallow"


def test_an_older_runner_reads_without_a_budget(state):
    runner = V9Runner(version=8)
    runner.trees = [{"token": "t1", "tree": _app(children=[BUTTON]), "tier": "root", "truncated": True}]
    s = Session(runner)
    tree = asyncio.run(s.refresh())
    assert runner.paths("/wda/tw/tree") == ["/wda/tw/tree"] and tree.tier == "full"  # (no budget sent: no tier read)


def test_look_says_a_screen_gave_no_elements(state):
    runner = V9Runner()
    runner.trees = [{"token": "r1", "tree": _app(), "tier": "root", "truncated": True}]
    s = Session(runner)
    text, _ = asyncio.run(s.look(screenshot=False))
    assert ui.ELEMENTS_NONE + "." in text.splitlines()
    assert "nothing visible" not in text  # (the app isn't loading: it doesn't answer)


# ---- the budget memory ----

class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_the_budget_memory():
    clock = Clock()
    b = ReadBudget(normal_ms=3000, short_ms=600, memory=20.0, clock=clock)
    assert b.budget(None) == b.budget("a") == 3000
    b.answered("a", True, 3000)
    assert b.budget("a") == 600 and b.short("a") and b.budget("b") == 3000
    clock.t += 15
    b.answered("a", True, 600)  # a short read cut short again doesn't renew the time
    clock.t += 5
    assert b.budget("a") == 3000  # 20 s since the normal read: it is asked whole once more
    b.answered("a", True, 3000)
    assert b.budget("a") == 600
    b.answered("a", False, 600)  # it answered whole: the normal budget at once
    assert b.budget("a") == 3000
    b.answered("a", True, 3000)
    b.answered("a", False, 600, took_ms=200)  # whole within the short budget: that says nothing of the screens that
    assert b.budget("a") == 600               # didn't answer, and the short budget was enough for this one
    b.answered("a", False, 3000, took_ms=2100)  # whole, but only after more than the short budget: it is slow to read,
    assert b.budget("a") == 3000                # and gets the time it needs
    b.answered(None, True, 3000)  # (no app named: nothing kept)
    b.answered("c", True, 3000)
    b.forget()
    assert b.budget("c") == 3000


# ---- OCR on the phone ----

def test_the_runners_ocr_answer_is_placed_in_points_in_reading_order():
    value = {"screen": [430, 932], "lines": [
        {"text": " Following ", "rect": [120.4, 60, 80, 20], "confidence": 0.9},
        {"text": "For You", "rect": [220, 61, 70, 20], "confidence": 1.0},
        {"text": "faint", "rect": [10, 300, 40, 10], "confidence": 0.1},
        {"text": "  ", "rect": [10, 400, 40, 10], "confidence": 1.0},
        {"text": "@someone", "rect": [16, 800, 100, 18]}]}
    seen = ocr.of_runner(value, (430.0, 932.0))
    assert [s.text for s in seen] == ["Following", "For You", "@someone"]
    assert seen[0].line("o1") == 'o1 "Following" @120,60 80x20' and seen[2].score == 1.0
    # the runner's screen the other way round from the session's (it turned since): scaled to the session's points
    turned = ocr.of_runner({"screen": [215, 466], "lines": [{"text": "A", "rect": [10, 10, 20, 10]}]}, (430.0, 932.0))
    assert turned[0].rect == (20.0, 20.0, 40.0, 20.0)
    for bad in (None, {"lines": "x"}, {"lines": [{"text": "A"}]}, {"lines": [{"text": "A", "rect": [1, 2, "x", 4]}]}):
        with pytest.raises(ValueError):
            ocr.of_runner(bad, (430.0, 932.0))


def test_the_session_reads_text_on_the_phone(state, monkeypatch):
    runner = V9Runner()
    runner.ocr_value = {"screen": [430, 932], "ms": {"shot": 40, "ocr": 210},
                        "lines": [{"text": "For You", "rect": [220, 61, 70, 20], "confidence": 1.0}]}
    s = Session(runner)
    asyncio.run(s.ensure())
    assert s.reads_text()
    seen = asyncio.run(s.read_text("fast"))
    assert [x.text for x in seen] == ["For You"] and runner.paths("/wda/tw/ocr") == ["/wda/tw/ocr?level=fast"]
    monkeypatch.setattr(session_module, "OCR_LANGUAGES", " en-US, ar-SA ,")
    asyncio.run(s.read_text())
    assert runner.paths("/wda/tw/ocr")[-1] == "/wda/tw/ocr?level=accurate&languages=en-US,ar-SA"
    runner.ocr_value = {"no": "lines"}
    with pytest.raises(DeviceError):
        asyncio.run(s.read_text())
    with pytest.raises(ValueError):
        asyncio.run(s.read_text("slow"))
    assert not Session(Recorder()).reads_text()


# ---- the header ----

def test_describe_says_how_much_was_read():
    shallow = ui.describe([button("e1", "OK", "ok")], (430.0, 932.0), 3.0, ("TikTok", TIKTOK), tier="shallow")
    assert shallow.splitlines()[2] == ui.ELEMENTS_PART + "." and 'e1 button "OK"' in shallow
    root = ui.describe([], (430.0, 932.0), 3.0, ("TikTok", TIKTOK), tier="root")
    assert root.splitlines() == ["Screen 430x932 pt @3x · 0 elements", f"App: TikTok ({TIKTOK})", ui.ELEMENTS_NONE + "."]
    assert "nothing visible" in ui.describe([], (430.0, 932.0))  # (a whole read with nothing in it: as before)


# ---- the daemon: looks and actions on a cut-short screen ----

FEED = [ocr.Seen("Following", (120.0, 60.0, 80.0, 20.0), 0.9), ocr.Seen("For You", (220.0, 60.0, 70.0, 20.0), 1.0),
        ocr.Seen("12:31", (42.0, 19.0, 75.0, 19.0), 1.0)]


def feed_session(tier="root", text=FEED):
    s = DescribingSession()
    s.tier, s.tree, s.app = tier, [], ("TikTok", TIKTOK)
    s.runner_text = list(text) if text is not None else None
    return s


@pytest.fixture
def no_pc_ocr(monkeypatch):
    monkeypatch.setattr(ocr, "reader", None)
    monkeypatch.setattr(ocr, "available", lambda: False)


def test_a_look_at_a_screen_that_doesnt_answer_reads_its_text_without_being_asked(no_pc_ocr):
    async def go():
        s = feed_session()
        d = make(s)
        stop, task = await started(d)
        r = await d.run_tool("a", "look", {})
        lines = r["text"].splitlines()
        none = ui.ELEMENTS_NONE + "; text read from the picture:"
        assert none in lines and lines[lines.index(none) + 1:] == ['o1 "Following" @120,60 80x20',
                                                                   'o2 "For You" @220,60 70x20']  # (not the clock)
        assert daemon_module.OCR_HEAD not in lines and r["image"] is not None and s.text_levels == ["accurate"]
        r = await d.run_tool("a", "tap", {"target": {"ref": "o2"}, "screenshot": False})
        assert r["text"].startswith("Tapped (255, 70).") and r["image"] is None
        assert ui.ELEMENTS_NONE + "." in r["text"] and daemon_module.AFTER_CUT in r["text"]
        assert "-" not in [line[:1] for line in r["text"].splitlines()]  # no change list
        with pytest.raises(Refused) as e:  # the action's answer let the o refs go: they were of the screen before
            await d.run_tool("a", "tap", {"target": {"ref": "o2"}})
        assert e.value.code == "not_found"
        await finish(stop, task)
    run(go)


def test_ocr_false_and_a_screen_with_no_text(no_pc_ocr):
    async def go():
        s = feed_session(text=[])
        d = make(s)
        stop, task = await started(d)
        r = await d.run_tool("a", "look", {"screenshot": False, "ocr": False})
        assert ui.ELEMENTS_NONE + "." in r["text"].splitlines() and s.text_levels == []
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert ui.ELEMENTS_NONE + "; no text read from the picture either." in r["text"].splitlines()
        await finish(stop, task)
    run(go)


def test_without_any_ocr_the_cut_short_look_says_how_to_get_it(no_pc_ocr):
    async def go():
        s = feed_session(text=None)  # an older runner (no OCR route), and no extra on this computer
        d = make(s)
        stop, task = await started(d)
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert ui.ELEMENTS_NONE + "." in r["text"].splitlines()
        assert r["text"].splitlines()[-1] == "Note: no text read from the picture: " + ocr.INSTALL
        await finish(stop, task)
    run(go)


def test_a_failed_ocr_on_the_phone_falls_back_to_this_computer(monkeypatch):
    monkeypatch.setattr(ocr, "reader", lambda image, screen: [ocr.Seen("PLAY", (150.0, 400.0, 100.0, 40.0), 0.98)])

    async def go():
        s = feed_session()
        s.runner_text = DeviceError("WebDriverAgent: unknown error: tapwright ocr: Vision failed")
        d = make(s)
        stop, task = await started(d)
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert 'o1 "PLAY" @150,400 100x40' in r["text"].splitlines() and "Note:" not in r["text"]
        monkeypatch.setattr(ocr, "reader", None)
        monkeypatch.setattr(ocr, "available", lambda: False)
        r = await d.run_tool("a", "look", {"screenshot": False})  # and with nothing here either: a note, no failure
        assert r["text"].splitlines()[-1] == "Note: the phone's OCR failed (DeviceError), and " + ocr.INSTALL
        await finish(stop, task)
    run(go)


def test_ocr_true_on_a_whole_screen_prefers_the_phone(monkeypatch):
    def never(image, screen):
        raise AssertionError("read on this computer although the phone can")
    monkeypatch.setattr(ocr, "reader", never)

    async def go():
        s = FakeSession()
        s.runner_text = [ocr.Seen("PLAY", (150.0, 400.0, 100.0, 40.0), 0.98)]
        d = make(s)
        stop, task = await started(d)
        r = await d.run_tool("a", "look", {"screenshot": False, "ocr": True})
        lines = r["text"].splitlines()
        assert lines[lines.index(daemon_module.OCR_HEAD) + 1] == 'o1 "PLAY" @150,400 100x40'
        r = await d.run_tool("a", "look", {"screenshot": False})  # a whole screen: no OCR unless asked
        assert daemon_module.OCR_HEAD not in r["text"] and s.text_levels == ["accurate"]
        await finish(stop, task)
    run(go)


def test_a_partly_read_screen_lists_what_it_gave_and_the_text_after_it(no_pc_ocr):
    async def go():
        s = feed_session(tier="shallow", text=FEED + [ocr.Seen("Profile", (340.0, 880.0, 50.0, 14.0), 1.0)])
        s.tree = [button("e1", "Profile", "profile", rect=(330, 870, 70, 40))]
        d = make(s)
        stop, task = await started(d)
        r = await d.run_tool("a", "look", {"screenshot": False})
        lines = r["text"].splitlines()
        assert ui.ELEMENTS_PART + "." in lines and any(line.startswith('e1 button "Profile"') for line in lines)
        refs = lines[lines.index(daemon_module.OCR_HEAD) + 1:]
        assert refs == ['o1 "Following" @120,60 80x20', 'o2 "For You" @220,60 70x20']  # (Profile: an element says it)
        await finish(stop, task)
    run(go)


def test_targets_by_name_on_a_screen_with_no_elements_say_how_to_reach_it(no_pc_ocr):
    async def go():
        s = feed_session()
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        # (a text the picture shows is found there: test_text_by_picture_200; one it doesn't show is not_found as before)
        for target in ({"text": "Nowhere"}, {"label": "Like"}, {"id": "like-button"}, {"ref": "e3"}):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "tap", {"target": target})
            assert e.value.code == "not_found" and daemon_module.HINT_NO_ELEMENTS in str(e.value), target
        assert "tap" not in s.calls
        await finish(stop, task)
    run(go)


def test_swipe_scroll_and_buttons_work_on_a_screen_with_no_elements(no_pc_ocr):
    async def go():
        s = feed_session()
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        r = await d.run_tool("a", "swipe", {"x0": 215, "y0": 700, "x1": 215, "y1": 200})
        assert r["text"].startswith("swiped (215, 700) -> (215, 200)") and r["image"] is not None
        assert ui.ELEMENTS_NONE + "." in r["text"]
        r = await d.run_tool("a", "scroll", {"direction": "down", "screenshot": False})
        assert r["text"].startswith("Scrolled the screen down.") and len(s.scrolls) == 1
        r = await d.run_tool("a", "scroll", {"direction": "down", "target": {"x": 200, "y": 500}, "screenshot": False})
        assert r["text"].startswith("Scrolled the screen down.") and len(s.scrolls) == 2  # (no scroll view to find)
        r = await d.run_tool("a", "press_button", {"name": "volumeUp", "screenshot": False})
        assert r["text"].startswith("pressed volumeUp") and "button:volumeUp" in s.calls
        await finish(stop, task)
    run(go)


def test_typing_without_a_target_goes_to_the_focused_field(no_pc_ocr):
    async def go():
        s = feed_session()
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        r = await d.run_tool("a", "type_text", {"text": "dogs", "submit": True, "screenshot": False})
        assert r["text"].startswith("Typed 4 characters and pressed return into whatever has the keyboard's focus (this "
                                    "screen's elements weren't read, so no field was checked).")
        assert s.typed == [("dogs", False, True, None)]  # never cleared: nothing can say what the field held
        r = await d.run_tool("a", "type_text", {"text": "cats", "screenshot": False})
        assert "the field wasn't cleared first" in r["text"]
        for text, code in (("${account.email}", "not_found"), ("", "not_actionable")):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "type_text", {"text": text})
            assert e.value.code == code
        assert len(s.typed) == 2
        with pytest.raises(Refused) as e:  # a target by name: not_found, with the hint
            await d.run_tool("a", "type_text", {"text": "x", "target": {"label": "Search"}})
        assert e.value.code == "not_found" and daemon_module.HINT_NO_ELEMENTS in str(e.value)
        await finish(stop, task)
    run(go)


def test_wait_for_text_reads_the_picture_on_a_screen_with_no_elements(no_pc_ocr):
    async def go():
        s = feed_session()
        d = make(s)
        stop, task = await started(d)
        r = await d.run_tool("a", "wait_for", {"text": "for you", "timeout": 2, "screenshot": False})
        assert r["text"].startswith("Done after") and s.text_levels == ["fast"] and r["image"] is None
        r = await d.run_tool("a", "wait_for", {"text": "Following", "timeout": 2})  # (unset: the picture comes)
        assert ui.ELEMENTS_NONE + "." in r["text"] and r["image"] is not None
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "wait_for", {"text": "Comments", "timeout": 0.5})
        assert e.value.code == "timeout"
        s.tier = "full"  # a whole screen: its elements alone decide, as before
        before = len(s.text_levels)
        with pytest.raises(Refused):
            await d.run_tool("a", "wait_for", {"text": "For You", "timeout": 0.5})
        assert len(s.text_levels) == before
        await finish(stop, task)
    run(go)


def test_a_whole_screen_answers_as_before():
    async def go():
        s = DescribingSession()
        s.runner_text = [ocr.Seen("PLAY", (150.0, 400.0, 100.0, 40.0), 0.98)]
        d = make(s)
        stop, task = await started(d)
        r = await d.run_tool("a", "look", {"screenshot": False})
        assert "Elements:" not in r["text"] and "o1" not in r["text"] and s.text_levels == []
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert r["image"] is None and daemon_module.AFTER_CUT not in r["text"]
        await finish(stop, task)
    run(go)


def test_the_frozen_app_seam_answers_nothing_yet_and_is_asked_first():
    async def go():
        s = FakeSession()
        s.stuck = AppNotAnswering(TIKTOK, "TikTok")
        d = make(s, ready_wait=2.0)
        stop, task = await started(d, until=())
        await until_stuck(d)
        with pytest.raises(Refused) as e:  # today: refused as in 1.10.0
            await d.run_tool("a", "tap", {"target": {"x": 10, "y": 300}})
        assert e.value.code == "not_answering"
        asked = []

        async def seam(client_id, name, args, notes, shot):
            asked.append(name)
            return {"text": f"{name} by hands", "image": None, "image_size": None}
        d._frozen_tool = seam
        r = await d.run_tool("a", "swipe", {"x0": 1, "y0": 700, "x1": 1, "y1": 200})
        assert r["text"].startswith("swipe by hands") and asked == ["swipe"]
        with pytest.raises(Refused) as e:  # what isn't in FROZEN_TOOLS never reaches it
            await d.run_tool("a", "open_url", {"url": "https://example.com"})
        assert e.value.code == "not_answering" and asked == ["swipe"]
        await finish(stop, task)
    run(go)

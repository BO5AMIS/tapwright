"""1.10.1 (blind mode): TikTok's For You feed never answers accessibility reads (2026-11-24), so no session could be made
and every call was refused `not_answering`, a point tap included. Each test fails on e9e511b (1.10.0) and passes after."""
import asyncio
import base64
import types

import pytest

from fakes import image_bytes
from test_daemon import make
from test_session import Recorder

from tapwright import ocr, session as session_module
from tapwright.control import Refused
from tapwright.device import AppNotAnswering, ConnectionLost
from tapwright.session import Session

TIKTOK = "com.zhiliaoapp.musically"
BLIND = ("_blind_path", "blind_version", "_blind_touching", "blind_tap", "blind_drag", "blind_keys", "blind_picture")
PNG = image_bytes((1290, 2796), (20, 20, 20))  # an iPhone 15 Pro Max's screenshot: 430x932 pt at 3x


# ---- the session: the session a frozen app's first read hung on is kept ----

@pytest.fixture
def phone(monkeypatch):
    monkeypatch.setattr(session_module.store, "load_state", lambda: {"runner_id": "runner"})
    return Recorder()


def test_the_session_whose_first_read_hung_is_kept_for_blind_mode(phone):
    phone.locked, phone.in_front = False, [TIKTOK]
    phone.fail["/window/size"] = ConnectionLost("WebDriverAgent didn't answer GET /session/S/window/size within 15 s")
    s = Session(phone)
    with pytest.raises(AppNotAnswering):
        asyncio.run(s.ensure())
    assert s.sid is None and s.blind_sid == "S"
    asyncio.run(s.reset())
    assert s.blind_sid is None


def test_a_working_session_has_no_blind_session(phone):
    phone.locked = False
    s = Session(phone)
    asyncio.run(s.ensure())
    assert s.sid == "S" and s.blind_sid is None


def _kept(phone, alive=True) -> Session:
    async def wda_alive():
        return alive
    phone.wda_alive = wda_alive
    s = Session(phone)
    s.blind_sid = "B1"
    return s


def test_a_reconnect_asks_the_blind_session_and_takes_it_when_the_app_reads_again(phone):
    phone.locked = False
    s = _kept(phone)
    asyncio.run(s.ensure())
    assert s.sid == "B1" and s.blind_sid is None and s.screen == (430.0, 932.0)
    assert ("POST", "/session", {"capabilities": {"alwaysMatch": {}}}) not in phone.requests and phone.started == []


def test_a_reconnect_while_the_app_still_hangs_keeps_the_blind_session(phone):
    phone.locked, phone.in_front = False, [TIKTOK]
    phone.fail["/session/B1/window/size"] = ConnectionLost("WebDriverAgent didn't answer within 3 s")
    s = _kept(phone)
    with pytest.raises(AppNotAnswering):
        asyncio.run(s.ensure())
    assert s.sid is None and s.blind_sid == "B1"
    assert not any(p == "/session" for _, p, _ in phone.requests)  # no new session to end the blind one


def test_a_reconnect_after_the_runner_died_starts_as_usual(phone):
    phone.locked = False
    phone.fail["/session/B1/window/size"] = ConnectionLost("WebDriverAgent didn't answer within 3 s")
    s = _kept(phone, alive=False)
    asyncio.run(s.ensure())
    assert s.sid == "S" and s.blind_sid is None and phone.started == ["runner"]


def test_settings_go_before_the_first_read_with_the_depth_asked_for(phone, monkeypatch):
    monkeypatch.setattr(session_module, "SNAPSHOT_DEPTH", "12")
    phone.locked = False
    asyncio.run(Session(phone).ensure())
    paths = [p for _, p, _ in phone.requests]
    settings = paths.index("/session/S/appium/settings")
    assert settings < paths.index("/session/S/window/size")
    assert phone.requests[settings][2]["settings"]["snapshotMaxDepth"] == 12


@pytest.mark.parametrize("depth", ["", "0", "abc", "999"])
def test_no_depth_setting_unless_a_sane_one_is_asked_for(phone, monkeypatch, depth):
    monkeypatch.setattr(session_module, "SNAPSHOT_DEPTH", depth)
    phone.locked = False
    asyncio.run(Session(phone).ensure())
    sent = next(b for _, p, b in phone.requests if p == "/session/S/appium/settings")
    assert "snapshotMaxDepth" not in sent["settings"]


# ---- the daemon: what works while the app in front doesn't answer ----

class Phone:
    """WebDriverAgent's answers while TikTok hangs every read: the session-less routes and the fork's touch routes."""

    def __init__(self, locked=False, front=TIKTOK, version=8):
        self.sent: list = []
        self.locked, self.front, self.version = locked, front, version
        self.fail: dict = {}

    async def http(self, method, path, payload=None, timeout=30.0):
        self.sent.append((method, path, payload))
        for suffix in list(self.fail):
            if path.endswith(suffix):
                raise self.fail.pop(suffix)
        if path == "/wda/locked":
            return {"value": self.locked}
        if path == "/wda/tw/version":
            return {"value": {"tapwright": self.version}}
        if path == "/screenshot":
            return {"value": base64.b64encode(PNG).decode()}
        if "/window/size" in path or "/source" in path or "/wda/tw/tree" in path:
            raise AssertionError(f"blind mode read the app: {path}")
        return {"value": None}

    async def foreground_apps(self):
        return [self.front] if self.front else []

    def touches(self):
        return [(p, b) for m, p, b in self.sent if m == "POST"]


def _blind(phone: Phone | None = None, blind_sid: "str | None" = "B1"):
    phone = phone or Phone()
    d = make()
    d.stuck = AppNotAnswering(TIKTOK, "TikTok")
    s = d.session
    for name in BLIND:
        setattr(s, name, types.MethodType(getattr(Session, name), s))
    s.blind_sid, s.touch_hook, s.scale, s.typed_into, s.input_point = blind_sid, None, 3.0, None, None
    s.device.http, s.device.foreground_apps = phone.http, phone.foreground_apps
    return d, phone


def _call(d, name, args):
    async def go():
        try:
            return await d.run_tool("a", name, args)
        except Refused as e:
            return e
    return asyncio.run(go())


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    from tapwright import daemon as daemon_module
    monkeypatch.setattr(daemon_module, "BLIND_SETTLE", 0.0)
    monkeypatch.setattr(ocr, "reader", None)
    monkeypatch.setattr(ocr, "installed", lambda: False)


def test_a_point_tap_goes_out_on_the_blind_session_without_reading_the_app():
    d, phone = _blind()
    got = _call(d, "tap", {"target": {"x": 215, "y": 893}})
    assert not isinstance(got, Refused), got
    from tapwright.control import TAP_MS
    assert ("/session/B1/wda/tw/tap", {"x": 215.0, "y": 893.0, "duration": TAP_MS / 1000, "wait": True}) in phone.touches()
    assert got["text"].startswith("Tapped (215, 893) in TikTok (com.zhiliaoapp.musically) without reading it")
    assert got["image"] and got["image_size"][0] == 600  # the screen after it


def test_long_press_holds_for_its_seconds():
    d, phone = _blind()
    _call(d, "long_press", {"target": {"x": 100, "y": 200}, "seconds": 2})
    assert [b["duration"] for p, b in phone.touches() if p.endswith("/wda/tw/tap")] == [2.0]


def test_a_swipe_goes_through_the_fast_drag():
    d, phone = _blind()
    got = _call(d, "swipe", {"x0": 215, "y0": 700, "x1": 215, "y1": 200})
    assert not isinstance(got, Refused), got
    drags = [b for p, b in phone.touches() if p == "/session/B1/wda/tw/drag"]
    assert len(drags) == 1 and drags[0]["points"][0][1:] == [215, 700] and drags[0]["points"][-1][1:] == [215, 200]


def test_typing_goes_to_the_focused_field_and_says_it_wasnt_cleared():
    d, phone = _blind()
    got = _call(d, "type_text", {"text": "Day 12", "submit": True})
    assert ("/session/B1/wda/keys", {"value": list("Day 12\n")}) in phone.touches()
    assert "Typed 6 characters and pressed return into the focused field" in got["text"]
    assert "wasn't cleared" in got["text"]


@pytest.mark.parametrize("name,args", [
    ("tap", {"target": {"label": "Post"}}),
    ("tap", {"target": {"ref": "e3"}}),
    ("type_text", {"text": "hi", "target": {"label": "Caption"}}),
])
def test_only_points_while_blind(name, args):
    d, phone = _blind()
    got = _call(d, name, args)
    assert isinstance(got, Refused) and got.code == "not_answering" and "Nothing was" in str(got)
    assert phone.touches() == []


def test_a_saved_accounts_value_is_never_typed_blind():
    d, phone = _blind()
    got = _call(d, "type_text", {"text": "${account.password}"})
    assert isinstance(got, Refused) and got.code == "not_answering" and "saved account" in str(got)
    assert phone.touches() == []


def test_nothing_is_touched_when_another_app_is_in_front():
    d, phone = _blind(Phone(front="com.apple.mobileslideshow"))
    got = _call(d, "tap", {"target": {"x": 215, "y": 893}})
    assert isinstance(got, Refused) and got.code == "reconnecting" and "isn't in front" in str(got)
    assert phone.touches() == []


def test_nothing_is_touched_on_a_locked_phone():
    d, phone = _blind(Phone(locked=True))
    got = _call(d, "tap", {"target": {"x": 215, "y": 893}})
    assert isinstance(got, Refused) and got.code == "locked"
    assert phone.touches() == []


def test_a_point_off_the_screen_is_refused():
    d, phone = _blind()
    got = _call(d, "tap", {"target": {"x": 215, "y": 2000}})
    assert isinstance(got, Refused) and got.code == "bad_params" and "430x932" in str(got)
    assert phone.touches() == []


def test_an_older_runner_without_the_touch_route_touches_nothing():
    d, phone = _blind(Phone(version=1))
    got = _call(d, "swipe", {"x0": 215, "y0": 700, "x1": 215, "y1": 200})  # the drag came with version 2
    assert isinstance(got, Refused) and got.code == "not_answering" and "tapwright setup" in str(got)
    assert phone.touches() == []


def test_a_dry_run_is_refused_blind():
    d, phone = _blind()
    got = _call(d, "tap", {"target": {"x": 215, "y": 893}, "dry_run": True})
    assert isinstance(got, Refused) and got.code == "not_answering" and phone.touches() == []


def test_a_touch_with_no_answer_is_a_timeout_that_may_have_happened():
    phone = Phone()
    phone.fail["/wda/tw/tap"] = ConnectionLost("WebDriverAgent didn't answer within 10 s")
    d, _ = _blind(phone)
    got = _call(d, "tap", {"target": {"x": 215, "y": 893}})
    assert isinstance(got, Refused) and got.code == "timeout" and "may still have happened" in str(got)


def test_look_is_the_picture_with_the_ways_that_work():
    d, phone = _blind()
    got = _call(d, "look", {})
    assert not isinstance(got, Refused), got
    lines = got["text"].split("\n")
    assert lines[0] == "App: TikTok (com.zhiliaoapp.musically)"
    assert "doesn't answer accessibility reads" in lines[1] and "Screen 430x932 pt." in lines
    assert got["image"] and "no OCR reading" in got["text"]


def test_look_reads_the_text_and_its_refs_are_touched_at_their_centre(monkeypatch):
    seen = [ocr.Seen("Upload", (180.0, 880.0, 70.0, 26.0), 0.97), ocr.Seen("9:41", (20.0, 10.0, 40.0, 16.0), 0.99)]
    monkeypatch.setattr(ocr, "reader", lambda image, screen: seen)
    d, phone = _blind()
    got = _call(d, "look", {})
    assert 'o1 "Upload" @180,880 70x26' in got["text"] and "9:41" not in got["text"]  # the clock is the phone's
    got = _call(d, "tap", {"target": {"ref": "o1"}})
    assert not isinstance(got, Refused), got
    assert [b["x"] for p, b in phone.touches() if p.endswith("/wda/tw/tap")] == [215.0]


def test_without_a_blind_session_everything_is_refused_as_before():
    d, phone = _blind(blind_sid=None)
    got = _call(d, "tap", {"target": {"x": 215, "y": 893}})
    assert isinstance(got, Refused) and got.code == "not_answering" and "OCR ref" not in str(got)
    assert phone.touches() == []


def test_the_refusal_of_other_calls_names_what_still_works():
    d, _ = _blind()
    got = _call(d, "scroll", {"direction": "down"})
    assert isinstance(got, Refused) and got.code == "not_answering"
    assert "tap and long_press at {x, y} or an OCR ref" in str(got) and "close_app it, then open_app it again" in str(got)


# ---- review R1101 (Opus 1) ----

def test_m1_the_runners_own_timeout_after_the_touch_is_a_timeout_that_may_have_happened():
    from tapwright.device import RunnerTimedOut
    phone = Phone()
    phone.fail["/wda/tw/tap"] = RunnerTimedOut("WebDriverAgent: timeout: the touch was not played in time")
    d, _ = _blind(phone)
    got = _call(d, "tap", {"target": {"x": 215, "y": 893}})
    assert isinstance(got, Refused) and got.code == "timeout" and "may still have happened" in str(got)


def test_m1_a_runner_without_the_sessionless_version_route_gets_the_setup_refusal():
    from tapwright.device import DeviceError
    phone = Phone()
    phone.fail["/wda/tw/version"] = DeviceError("WebDriverAgent: unknown command: Unhandled endpoint")
    d, _ = _blind(phone)
    got = _call(d, "tap", {"target": {"x": 215, "y": 893}})
    assert isinstance(got, Refused) and got.code == "not_answering" and "tapwright setup" in str(got)
    assert phone.touches() == []


def test_m1_a_session_the_runner_no_longer_knows_is_dropped_and_said_to_be_reconnecting():
    from tapwright.device import DeviceError
    phone = Phone()
    phone.fail["/wda/tw/tap"] = DeviceError("WebDriverAgent: invalid session id: Session does not exist")
    d, _ = _blind(phone)
    got = _call(d, "tap", {"target": {"x": 215, "y": 893}})
    assert isinstance(got, Refused) and got.code == "reconnecting" and "nothing was touched" in str(got)
    assert d.session.blind_sid is None


def test_m1_a_refused_keys_request_is_coded():
    from tapwright.device import DeviceError
    phone = Phone()
    phone.fail["/wda/keys"] = DeviceError("WebDriverAgent: unknown error: Keyboard is not present")
    d, _ = _blind(phone)
    got = _call(d, "type_text", {"text": "hi"})
    assert isinstance(got, Refused) and got.code == "not_answering" and "refused the type_text" in str(got)


def test_m1_a_look_whose_picture_fails_is_coded():
    phone = Phone()
    phone.fail["/screenshot"] = ConnectionLost("WebDriverAgent didn't answer GET /screenshot within 15 s")
    d, _ = _blind(phone)
    got = _call(d, "look", {})
    assert isinstance(got, Refused) and got.code == "not_answering" and "picture couldn't be taken" in str(got)


def test_m2_two_apps_in_front_touch_nothing():
    phone = Phone()

    async def two():
        return [TIKTOK, "com.apple.mobilesafari"]
    d, _ = _blind(phone)
    d.session.device.foreground_apps = two
    got = _call(d, "tap", {"target": {"x": 215, "y": 893}})
    assert isinstance(got, Refused) and got.code == "reconnecting" and phone.touches() == []


def test_m2_the_safety_checks_come_right_before_the_touch():
    d, phone = _blind()
    _call(d, "tap", {"target": {"x": 215, "y": 893}})
    paths = [p for _, p, _ in phone.sent]
    tap = next(i for i, p in enumerate(paths) if p.endswith("/wda/tw/tap"))
    assert paths[tap - 1] == "/wda/locked"  # (the process list is asked after it, outside http)
    assert paths.index("/screenshot") < paths.index("/wda/tw/version") < tap - 1


def test_m3_the_reconnect_question_waits_as_long_as_a_first_read(phone):
    seen = []
    original = phone.http

    async def http(method, path, payload=None, timeout=30.0):
        if path == "/session/B1/window/size":
            seen.append(timeout)
        return await original(method, path, payload, timeout)
    phone.http = http
    phone.locked = False
    asyncio.run(_kept(phone).ensure())
    assert seen == [session_module.FIRST_READ_WAIT]


def test_l1_tapline_hears_a_blind_touch_is_the_agents():
    d, _ = _blind()
    heard = []

    async def notify(ms):
        heard.append(ms)
    d._agent_touches.notify = notify
    _call(d, "tap", {"target": {"x": 215, "y": 893}})
    assert len(heard) == 1 and d.session.touch_hook is None


def test_l2_no_answer_before_the_touch_says_nothing_was_touched():
    phone = Phone()
    phone.fail["/wda/tw/version"] = ConnectionLost("WebDriverAgent didn't answer GET /wda/tw/version within 5 s")
    d, _ = _blind(phone)
    got = _call(d, "tap", {"target": {"x": 215, "y": 893}})
    assert isinstance(got, Refused) and got.code == "not_answering" and "nothing was touched" in str(got)
    assert "may still have happened" not in str(got)


def test_l3_a_blind_look_works_while_paused():
    d, _ = _blind()
    d.control.paused = True
    got = _call(d, "look", {})
    assert not isinstance(got, Refused), got
    assert isinstance(_call(d, "tap", {"target": {"x": 215, "y": 893}}), Refused)


def test_l4_a_setting_that_hangs_is_named_as_the_read_is(phone):
    phone.locked, phone.in_front = False, [TIKTOK]
    phone.fail["/appium/settings"] = ConnectionLost("WebDriverAgent didn't answer within 10 s")
    s = Session(phone)
    with pytest.raises(AppNotAnswering):
        asyncio.run(s.ensure())
    assert s.blind_sid == "S"


def test_l6_an_empty_text_clears_nothing_blind():
    d, phone = _blind()
    got = _call(d, "type_text", {"text": ""})
    assert isinstance(got, Refused) and got.code == "not_answering" and "cleared" in str(got)
    assert phone.touches() == []


# ---- phone test 2026-11-25: POST /session hangs too while TikTok's feed is in front ----

def test_a_read_cut_by_a_frozen_app_keeps_the_session_it_was_made_on(phone):
    """The session from before TikTok came forward still plays touches; a new one can't be had (POST /session hangs)."""
    phone.locked = False
    s = Session(phone)
    asyncio.run(s.ensure())
    assert s.sid == "S"
    phone.in_front = [TIKTOK]
    original = phone.http

    async def frozen(method, path, payload=None, timeout=30.0):
        if "/source" in path:
            phone.requests.append((method, path, payload))
            raise ConnectionLost(f"WebDriverAgent didn't answer GET {path} within {timeout:.0f} s")
        return await original(method, path, payload, timeout)
    phone.http = frozen
    monkey_fast = session_module.FAST_READ
    session_module.FAST_READ = "0"
    try:
        with pytest.raises(AppNotAnswering):
            asyncio.run(s.refresh())
    finally:
        session_module.FAST_READ = monkey_fast
    assert s.sid is None and s.blind_sid == "S"
    # the reconnect asks that session, and never makes a new one while the app still hangs
    phone.fail["/session/S/window/size"] = ConnectionLost("WebDriverAgent didn't answer within 10 s")

    async def alive():
        return True
    phone.wda_alive = alive
    posts = len([p for _, p, _ in phone.requests if p == "/session"])
    with pytest.raises(AppNotAnswering):
        asyncio.run(s.ensure())
    assert s.blind_sid == "S" and len([p for _, p, _ in phone.requests if p == "/session"]) == posts


# ---- phone test 2026-11-25 (2): a read of TikTok's feed holds the runner for minutes, so blind mode begins before it ----

def test_an_app_found_stuck_is_remembered_across_restarts():
    from tapwright import daemon as daemon_module
    d = make()
    d.state = "live"
    d.lost(AppNotAnswering(TIKTOK, "TikTok"))
    assert TIKTOK in d._blind_apps and TIKTOK in daemon_module._load_blind_apps()
    assert TIKTOK in make()._blind_apps


def test_the_refusal_says_to_close_and_open_it_again():
    d, _ = _blind(blind_sid=None)
    got = _call(d, "scroll", {"direction": "down"})
    assert isinstance(got, Refused) and "close_app it, then open_app it again" in str(got)


def test_open_app_of_a_remembered_app_goes_blind_without_reading_it(monkeypatch):
    from tapwright import daemon as daemon_module
    from test_daemon import finish, started
    monkeypatch.setattr(daemon_module, "BLIND_OPEN_SETTLE", 0.0)

    async def go():
        phone = Phone()
        d = make()
        d._blind_apps = {TIKTOK}
        stop, task = await started(d)
        s = d.session
        for name in BLIND:
            setattr(s, name, types.MethodType(getattr(Session, name), s))
        s.touch_hook, s.scale, s.typed_into, s.input_point = None, 3.0, None, None
        s.device.http, s.device.foreground_apps = phone.http, phone.foreground_apps
        s.sid = "S1"
        launched = []

        async def launch(bundle_id, arguments=None):
            launched.append(bundle_id)
            return "launched"
        s.launch = launch
        reads = len([c for c in s.calls if c in ("look", "refresh", "snapshot")])
        got = await d.run_tool("a", "open_app", {"app": TIKTOK})
        assert launched == [TIKTOK]
        assert got["text"].startswith(f"Opened {TIKTOK} without reading it") and got["image"]
        assert d.state == "not-answering" and s.blind_sid == "S1" and s.sid is None
        assert len([c for c in s.calls if c in ("look", "refresh", "snapshot")]) == reads  # nothing read the app
        got = await d.run_tool("a", "tap", {"target": {"x": 215, "y": 893}})
        assert got["text"].startswith("Tapped (215, 893)")
        await finish(stop, task)
    asyncio.run(go())


def test_open_app_of_another_app_is_as_before():
    from test_daemon import finish, started

    async def go():
        d = make()
        d._blind_apps = {TIKTOK}
        stop, task = await started(d)
        got = await d.run_tool("a", "open_app", {"app": "com.example.app"})
        assert "without reading it" not in got["text"] and d.state == "live"
        await finish(stop, task)
    asyncio.run(go())


def test_the_hold_waits_from_when_blind_mode_began():
    import time as time_module
    d, _ = _blind()
    d._blind_since = time_module.monotonic()
    assert asyncio.run(d._blind_hold()) is True  # just began: no try to connect (each is a read of the app)
    d._blind_since = d._blind_at = d._blind_probe_at = time_module.monotonic() - 120
    assert asyncio.run(d._blind_hold()) is False  # idle and due: one try
    assert asyncio.run(d._blind_hold()) is True  # (and not again at once)


def test_another_app_in_front_ends_the_hold():
    d, _ = _blind(Phone(front="com.apple.mobileslideshow"))
    import time as time_module
    d._blind_since = time_module.monotonic()
    assert asyncio.run(d._blind_hold()) is False


def test_an_app_whose_kept_session_reads_again_stays_remembered():
    from tapwright import daemon as daemon_module
    from test_daemon import finish, started

    async def go():
        d = make()
        d._blind_apps = {TIKTOK}
        d.stuck = AppNotAnswering(TIKTOK, "TikTok")
        d.session.blind_read_again = True
        d.state = "not-answering"
        stop, task = await started(d)
        assert d.state == "live" and TIKTOK in d._blind_apps  # (its feed still won't read: its reads are guarded)
        await finish(stop, task)
    asyncio.run(go())


def test_a_remembered_apps_read_is_guarded_and_goes_blind_before_the_tree_read(phone):
    """On TikTok's feed one window-size read takes 14 s, and the whole-tree read never ends: the cheap read goes first."""
    phone.locked, phone.in_front = False, [TIKTOK]
    s = Session(phone)
    asyncio.run(s.ensure())
    s.guard_apps, s.read_app = {TIKTOK}, ("TikTok", TIKTOK)
    phone.fail["/session/S/window/size"] = ConnectionLost("WebDriverAgent didn't answer within 3 s")
    before = len(phone.requests)
    with pytest.raises(AppNotAnswering):
        asyncio.run(s.refresh())
    sent = [p for _, p, _ in phone.requests[before:]]
    assert sent == ["/session/S/window/size"]  # no tree read went out
    assert s.sid is None and s.blind_sid == "S"


def test_a_remembered_apps_screen_that_reads_is_read_as_usual(phone):
    phone.locked, phone.in_front = False, [TIKTOK]
    s = Session(phone)
    asyncio.run(s.ensure())
    s.guard_apps, s.read_app = {TIKTOK}, ("TikTok", TIKTOK)
    before = len(phone.requests)
    try:
        asyncio.run(s.refresh())
    except Exception:  # noqa: BLE001 - the fake's tree is empty; only what was asked matters here
        pass
    sent = [p for _, p, _ in phone.requests[before:]]
    assert sent[0] == "/session/S/window/size" and len(sent) > 1 and s.blind_sid is None


def test_other_apps_reads_are_not_guarded(phone):
    phone.locked = False
    s = Session(phone)
    asyncio.run(s.ensure())
    s.guard_apps, s.read_app = {TIKTOK}, ("Lab", "com.example.lab")
    before = len(phone.requests)
    try:
        asyncio.run(s.refresh())
    except Exception:  # noqa: BLE001
        pass
    assert "/session/S/window/size" not in [p for _, p, _ in phone.requests[before:]]

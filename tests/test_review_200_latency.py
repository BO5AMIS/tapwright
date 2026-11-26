"""Review K of the 2.0 latency work (2026-11-26): one test for each finding, written by the reviewer to fail on
45380f5; each asserts the behaviour the review says is right. K-M1 as the fix reads it: the runner's screen route,
which asks no app, says the phone was turned."""
import asyncio

import pytest

from tapwright import daemon as daemon_module, session as session_module
from tapwright.session import ReadBudget, Session

TT = "com.zhiliaoapp.musically"


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def state(monkeypatch):
    monkeypatch.setattr(session_module.store, "load_state", lambda: {"runner_id": "runner"})
    monkeypatch.setattr(session_module, "FAST_READ", "1")


def _v11(trees):
    """A route version 11 runner (as the deleted test_latency_200 helper made it): `front` says TikTok, the tree route
    answers `trees` in turn (the last one stays)."""
    from test_vision_200 import V9Runner

    class V11Runner(V9Runner):
        def __init__(self):
            super().__init__(version=11)
            self.front = {"b": TT, "pid": 8496, "ms": 6}
            self.trees = list(trees)

        async def http(self, method, path, payload=None, timeout=30.0):
            bare = path.replace("/session/S", "")
            if bare.startswith("/wda/tw/front"):
                self.requests.append((method, path, payload))
                return {"value": dict(self.front)}
            if bare.startswith("/wda/activeAppInfo"):
                self.requests.append((method, path, payload))
                return {"value": {"bundleId": TT, "name": "TikTok"}}
            return await super().http(method, path, payload, timeout)
    return V11Runner()


def _root(w=430, h=932):
    return {"token": "r1", "tree": {"t": "Application", "r": [0, 0, w, h], "b": TT, "l": "TikTok", "pid": 7},
            "tier": "root", "truncated": True, "why": "full: timeout"}


def _whole():
    from test_vision_200 import BUTTON
    return {"token": "f1", "tree": {"t": "Application", "r": [0, 0, 430, 932], "b": TT, "l": "TikTok", "pid": 7,
                                    "c": [BUTTON]}, "tier": "full", "truncated": False}


# ---- K-H1: a screen that just read whole is answered "Elements: none" for the next 8 s ----

def test_k_h1_a_read_right_after_a_whole_read_is_not_made_up_as_an_empty_root(state):
    """TikTok's feed was cut short (normal budget); 8 s later the agent is on a screen of the app that reads (a
    profile with a video playing in it, as UNASKED_FOR's own comment says) and the real short read comes back WHOLE,
    fast. The next read 1 s later is left unasked and answered as a made-up `root` with no elements, and its why says
    the app's read "gave no elements in time", which is false: the last real read gave all of them."""
    clock = _Clock()
    runner = _v11([_root(), _whole()])
    s = Session(runner)
    s.budgets = ReadBudget(clock=clock)
    s.read_app = ("TikTok", TT)
    first = asyncio.run(s.refresh())
    assert first.tier == "root"  # the feed: cut short on the normal budget
    clock.t += 8.5
    second = asyncio.run(s.refresh())
    assert second.tier == "full" and len(second.items) == 1  # a real short read: the screen reads whole
    clock.t += 1.0
    third = asyncio.run(s.refresh())
    # what a real read answers (the runner still has the whole tree), not a root made here
    assert len(runner.paths("/wda/tw/tree")) == 3, s.last_why
    assert third.tier == "full" and len(third.items) == 1


# ---- K-M1: while reads are left unasked, the phone's orientation isn't followed ----

def test_k_m1_a_turn_of_the_phone_is_seen_by_the_next_read(state):
    """The feed in portrait, cut short; the phone turns to landscape (a person turns it, or a landscape video). The next
    read within 8 s is made up from the session's old `screen`, so `screen` and `turn` stay portrait: a hands touch by
    point maps with the portrait size and turn 0, and the header says 430x932 over a landscape picture."""
    runner = _v11([_root(), _root(932, 430)])
    s = Session(runner)
    s.read_app = ("TikTok", TT)
    asyncio.run(s.refresh())
    assert s.screen == (430.0, 932.0)
    real = runner.http

    async def http(method, path, payload=None, timeout=30.0):  # the phone was turned: the screen route says so
        if path.replace("/session/S", "") == "/wda/tw/screen":
            runner.requests.append((method, path, payload))
            return {"value": {"width": 932, "height": 430, "scale": 3, "orientation": 4}}
        if path.endswith("/rotation"):
            runner.requests.append((method, path, payload))
            return {"value": {"x": 0, "y": 0, "z": 270}}
        return await real(method, path, payload, timeout)
    runner.http = http
    asyncio.run(s.refresh())  # the runner's root now says 932x430
    assert s.screen == (932.0, 430.0)


# ---- K-H2: a person's input between the picture's text read and the touch is not caught ----

def test_k_h2_a_person_acting_after_the_picture_was_read_stops_the_touch_aimed_from_it(monkeypatch):
    """`tap {"text": "Friends"}` on a screen with no elements: the picture's text is read (outside the touch's turn),
    then the touch goes in a NEW turn whose person check counts from that turn's start. A person who swipes the feed
    between the two is not caught: the touch goes to where "Friends" WAS. On the element path the touch's own turn
    reads the screen again after a person acted (the list isn't current), so the old point is never used."""
    from test_daemon import finish, make, started
    from test_text_by_picture_200 import TABS
    from test_vision_200 import feed_session, run

    from tapwright import ocr

    monkeypatch.setattr(ocr, "reader", None)
    monkeypatch.setattr(ocr, "available", lambda: False)

    async def go():
        s = feed_session(text=TABS)
        d = make(s)
        stop, task = await started(d)
        real = s.read_text
        acted = []

        async def read_text(level="accurate"):
            out = await real(level)
            if not acted:
                async def swipe():  # a person swipes the feed in the live view as the text is being read
                    acted.append(True)
                asyncio.ensure_future(d.control.person(swipe))
                await asyncio.sleep(0)
            return out
        s.read_text = read_text
        sent = None
        try:
            r = await d.run_tool("a", "tap", {"target": {"text": "Friends"}, "screenshot": False})
            sent = r["text"]
        except daemon_module.Refused as e:
            assert e.code in ("busy", "stale"), e
        assert acted
        assert "tap" not in s.calls, f"the touch went to a point read before the person acted: {sent!r}"
        await finish(stop, task)
    run(go)


# ---- K-M2: one page push on a partly read (shallow) screen caps that app's settles at 0.5 s for good ----

def test_k_m2_a_shallow_screen_that_stood_still_after_a_push_keeps_the_normal_cap():
    """Safari on a heavy page reads `shallow` (the whole tree takes longer than the budget, but most of it is read). A
    tap that pushes a page settles still after 0.8 s. SettleCap counts that as "moving" (seconds > 0.5) on a "cut" tree
    (tier != full), so Safari is restless: every later settle waits at most 0.5 s while its pages read shallow, and a
    still end never gives the normal cap back (only a still end on a WHOLE tree does). Before 9a3c50c only a settle
    that ran into its cap counted."""
    from test_daemon import make, run_virtual
    from test_vision_200 import feed_session

    from tapwright.daemon import Settled

    async def go():
        s = feed_session(tier="shallow")
        s.settles = session_module.SettleCap()
        s.read_app, s.last_tier = ("Safari", "com.apple.mobilesafari"), "shallow"
        d = make(s)
        caps_asked = []
        ends = [Settled(0.8, True, False, True), Settled(0.3, True, False, True), Settled(0.3, True, False, True)]

        async def frames(after_seq, still=None, cap=2.0, since=None, early=None):
            caps_asked.append(cap)
            return ends.pop(0)
        d._settle_frames = frames
        for _ in range(3):
            await d._settle(0, cap=2.0)
        assert caps_asked == [2.0, 2.0, 2.0], caps_asked  # a screen that stands still is not one that never does
    run_virtual(go)


# ---- K-M3: the `apps` tool answers from a kept list of any age ----

def test_k_m3_the_apps_tool_lists_an_app_installed_since_the_list_was_kept(monkeypatch):
    """The list is kept at connect. The user installs a TestFlight build ten minutes later and the agent calls `apps`:
    it gets the kept list (stale lists are given at once and read again only behind the call), so the new app is
    missing from an answer the agent asked for explicitly. `_build_of` (a road's build, kept BUILD_TTL = 300 s) reads
    the same kept list, so a reinstalled build is taken for the old one."""
    from test_daemon import finish, make, started
    from test_latency_200 import a_session
    from test_vision_200 import run

    monkeypatch.setattr(session_module.store, "load_state", lambda: {"runner_id": "runner"})

    async def go():
        real, phone, _ = a_session()
        await real.apps()  # kept at connect
        monkeypatch.setattr(session_module, "APPS_STALE", 0.0)  # (ten minutes later)
        phone.listed.append({"bundle_id": "app.example.new", "name": "Newly", "version": "1", "build": "1"})
        d = make()
        stop, task = await started(d)
        d.session.apps = real.apps
        r = await d.run_tool("a", "apps", {})
        assert "app.example.new" in r["text"], r["text"]
        await finish(stop, task)
    run(go)

"""Reviewer L (Tapwright 2.0, 149a482..2f31f06): failing tests for the findings in REVIEW-200-FINAL.md.
Each test asserts the behaviour the review argues for; on 2f31f06 they fail."""
import asyncio

import pytest
from test_daemon import finish, make, run, started
from test_session import Recorder, with_answers

from tapwright import daemon as daemon_module, session as session_module
from tapwright.control import Refused
from tapwright.device import DeviceError
from tapwright.session import SPRINGBOARD, Session


@pytest.fixture(autouse=True)
def set_up(monkeypatch):
    monkeypatch.setattr(session_module.store, "load_state", lambda: {"runner_id": "runner"})


# ---- M1: the banner wait on a v11 runner falls back to /wda/activeAppInfo ----

def test_m1_banner_wait_never_asks_activeappinfo_when_the_front_route_fails():
    """_guards_banners turns the wait on because route version 11 says the app in front 'in a few ms'. When that route
    fails or answers "unknown", foreground_bundle_id falls back to /wda/activeAppInfo (1.7-30 s on a heavy app, the very
    cost that turned the wait off in 1.7.5), before every element-aimed touch in the top 200 pt."""
    async def go():
        phone = Recorder()
        with_answers(phone, {"/wda/activeAppInfo": {"value": {"bundleId": "com.example.app", "name": "App"}}})
        s = Session(phone)
        await s.ensure()
        s.tw_version = session_module.FRONT_VERSION
        phone.fail[session_module.FRONT_PATH] = DeviceError("no such route right now")
        s.banner_guard = True
        await s._touching(60, (40.0, 80.0))  # `back`'s button, or a row by label near the top
        asked = [p for _, p, _ in phone.requests]
        assert "/wda/activeAppInfo" not in asked, asked
    asyncio.run(go())


# ---- M2: SpringBoard over an app that is not a banner: refused as "a notification" after 8 s ----

def test_m2_an_element_aimed_touch_under_springboard_ui_over_an_app_is_not_refused_as_a_banner(monkeypatch):
    """Control Center or Notification Center pulled down over an app (dedicated mode): the runner names SpringBoard
    in front, iOS's process list still has the app, so _clear_of_banner takes it for a banner, waits BANNER_WAIT and
    refuses `covered: a notification is over the top of the screen` -- for a tap by label on a Control Center tile or a
    notification, which 1.10.0 (guard off since 1.7.5) made. The premise that the front route names SpringBoard there is
    the same one the banner logic itself relies on; it wants one look on the phone."""
    monkeypatch.setattr(session_module, "BANNER_WAIT", 0.02)
    monkeypatch.setattr(session_module, "BANNER_POLL", 0.01)

    async def go():
        phone = Recorder()
        phone.in_front = ["com.example.app"]  # the app stays in the foreground under Control Center
        s = Session(phone)
        await s.ensure()

        async def springboard():
            return SPRINGBOARD
        s.foreground_bundle_id = springboard
        s.banner_guard = True
        # (the fix: the agent aimed at the tile from a read of Control Center, whose root is SpringBoard; a touch
        # aimed from a read of the app is still held while a banner lies over it: test_session's banner tests)
        s.read_app = ("Home screen", SPRINGBOARD)
        await s._touching(60, (300.0, 120.0))  # Control Center's Wi-Fi tile, aimed by its label
    try:
        asyncio.run(go())
    except Refused as e:
        pytest.fail(f"refused as a banner: {e}")


# ---- L1: press_button lock's early answer skips the live view's announcement ----

def test_l1_press_button_lock_is_still_announced_to_the_live_view():
    async def go():
        d = make()
        stop, task = await started(d)
        queue = d.subscribe()
        real = d.session.press_button

        async def press_button(name):
            out = await real(name)
            if name == "lock":
                d.session.is_locked = True
            return out
        d.session.press_button = press_button
        r = await d.run_tool("a", "press_button", {"name": "lock"})
        assert r["text"].splitlines()[0] == daemon_module.LOCKED_BY_PRESS
        kinds = []
        while not queue.empty():
            m = queue.get_nowait()
            if m.get("type") == "action":
                kinds.append((m["by"], m["kind"]))
        await finish(stop, task)
        assert ("agent", "press_button") in kinds, kinds
    run(go)


# ---- L2: alike_on_glass cuts a portrait strip when the screen is landscape with turn 0 ----

def test_l2_alike_on_glass_ignores_nothing_wider_than_its_strips_on_a_landscape_screen_with_turn_0():
    """With turn 0 but a landscape screen (the state Hands._portrait calls 'which side is up isn't known yet'), the
    bottom strip of the captured (portrait) frame is computed from the landscape height: 24 pt x 349/430 = 20 thumb px,
    about 53 pt of what is a side of the landscape screen, so a change there is called no change."""
    import io

    from PIL import Image, ImageDraw

    from tapwright.stream import Frame

    def jpeg(mark: bool) -> bytes:
        im = Image.new("RGB", (1290, 2796), "white")  # captured portrait, as the stream sends it
        if mark:
            ImageDraw.Draw(im).rectangle([300, 2796 - 140, 900, 2796 - 90], fill="black")  # ~30-47 pt from that edge
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
        return buf.getvalue()
    a, b = Frame(1, 0, jpeg(False)), Frame(2, 0, jpeg(True))
    assert not daemon_module.alike_on_glass(a, b, (932.0, 430.0), turn=0)

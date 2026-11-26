"""2.0 latency, measured on the phone 2026-11-26: the phone's list of installed apps takes about a second to ask for, and
open_app, close_app, the first look at a built-in app and the call after a close_app each asked for it; close_app waited a
second for the runner's own terminate of an app iOS had already ended; and the call after the agent's own close_app looked
for a crash of the app it had closed."""
import asyncio

import pytest
from test_daemon import finish, make, run, started
from test_diagnostics import die, opened, tuned
from test_session import SETTINGS_TREE, Recorder, with_answers

from tapwright import daemon as daemon_module, session as session_module, ui as ui_module
from tapwright.session import Session


class Apps(Recorder):
    """A phone whose list of installed apps counts how often it is asked, and can take a while."""

    def __init__(self):
        super().__init__()
        self.reads, self.takes = 0, 0.0
        self.listed = [{"bundle_id": "app.example.one", "name": "One", "version": "1", "build": "1"}]

    async def user_apps(self):
        self.reads += 1
        if self.takes:
            await asyncio.sleep(self.takes)
        return list(self.listed)


@pytest.fixture(autouse=True)
def set_up(monkeypatch):
    monkeypatch.setattr(session_module.store, "load_state", lambda: {"runner_id": "runner"})


def a_session(front: str = "com.apple.springboard") -> tuple[Session, Apps, dict]:
    phone = Apps()
    answers = {ui_module.SOURCE_PATH: {"value": SETTINGS_TREE},
               "/wda/activeAppInfo": {"value": {"bundleId": front, "name": front}}}
    with_answers(phone, answers)
    return Session(phone), phone, answers


def test_the_installed_apps_are_asked_for_once_and_kept():
    async def go():
        s, phone, _ = a_session()
        first = await s.apps()
        assert await s.apps() == first and phone.reads == 1  # the second answer costs nothing
        phone.listed.append({"bundle_id": "app.example.two", "name": "Two", "version": "1", "build": "1"})
        assert len(await s.apps()) == 1  # (the kept list, until it is stale or a fresh one is asked for)
        assert len(await s.apps(fresh=True)) == 2 and phone.reads == 2 and s.app_names["app.example.two"] == "Two"
    asyncio.run(go())


def test_a_stale_list_is_given_at_once_and_read_again_behind_the_call(monkeypatch):
    async def go():
        s, phone, _ = a_session()
        await s.apps()
        monkeypatch.setattr(session_module, "APPS_STALE", 0.0)
        phone.listed.append({"bundle_id": "app.example.two", "name": "Two", "version": "1", "build": "1"})
        phone.takes = 0.05
        began = asyncio.get_running_loop().time()
        assert len(await s.apps()) == 1 and asyncio.get_running_loop().time() - began < 0.04  # nothing was waited for
        assert len(await s.apps()) == 1
        await s._apps_task
        assert phone.reads == 2  # one read behind, not one for each call
        assert len(await s.apps()) == 2 and "app.example.two" in s.app_names
    asyncio.run(go())


def test_a_failed_read_behind_a_call_fails_nothing(monkeypatch):
    async def go():
        s, phone, _ = a_session()
        kept = await s.apps()
        monkeypatch.setattr(session_module, "APPS_STALE", 0.0)

        async def broken():
            raise RuntimeError("the phone's app list can't be had")
        phone.user_apps = broken
        assert await s.apps() == kept
        await asyncio.wait({s._apps_task})
        assert await s.apps() == kept  # the kept list stays
    asyncio.run(go())


def test_the_first_look_at_a_built_in_app_does_not_wait_for_the_list():
    """Calculator, Settings, the Clock: no list of installed apps holds them, and each first look paid a second to learn it."""
    async def go():
        s, phone, answers = a_session()
        await s.read_app_names()
        answers["/wda/activeAppInfo"] = {"value": {"bundleId": "com.apple.calculator", "name": "Calculator"}}
        phone.takes = 0.2
        began = asyncio.get_running_loop().time()
        assert (await s.snapshot()).app == ("Calculator", "com.apple.calculator")  # WebDriverAgent's own name
        assert asyncio.get_running_loop().time() - began < 0.15
        await s._apps_task
        assert phone.reads == 2  # read once more, behind the answer
        await s.snapshot()
        assert phone.reads == 2  # and not asked for again
    asyncio.run(go())


def test_an_apple_app_installed_since_has_its_name_from_the_next_answer():
    async def go():
        s, phone, answers = a_session()
        await s.read_app_names()
        phone.listed.append({"bundle_id": "com.apple.Pages", "name": "Pages", "version": "1", "build": "1"})
        answers["/wda/activeAppInfo"] = {"value": {"bundleId": "com.apple.Pages", "name": "com.apple.Pages"}}
        await s.snapshot()
        await s._apps_task
        assert (await s.snapshot()).app == ("Pages", "com.apple.Pages")
    asyncio.run(go())


def test_open_app_by_a_built_in_bundle_id_asks_for_no_new_list():
    async def go():
        d = make()
        stop, task = await started(d)
        asked = []
        real = d.session.apps

        async def apps(fresh=False):
            asked.append(fresh)
            return await real(fresh)
        d.session.apps = apps
        await d.run_tool("a", "open_app", {"app": "com.apple.calculator", "look": False})
        assert d.session.launched[-1] == "com.apple.calculator" and True not in asked
        await finish(stop, task)
    run(go)


def test_a_name_the_kept_list_lacks_is_looked_for_in_a_fresh_one():
    """An app installed since the list was read is found by its name; one that isn't there is still refused."""
    async def go():
        d = make()
        stop, task = await started(d)
        real = d.session.apps
        asked = []

        async def apps(fresh=False):
            asked.append(fresh)
            if fresh:
                d.session.installed.append({"bundle_id": "app.example.fresh", "name": "Freshly", "version": "1", "build": "1"})
            return await real(fresh)
        d.session.apps = apps
        r = await d.run_tool("a", "open_app", {"app": "Freshly", "look": False})
        assert r["text"].startswith("Opened Freshly (app.example.fresh).") and asked == [False, True]
        await finish(stop, task)
    run(go)


def test_a_bundle_id_looked_for_once_and_not_found_is_not_looked_for_again():
    async def go():
        d = make()
        stop, task = await started(d)
        real = d.session.apps
        asked = []

        async def apps(fresh=False):
            asked.append(fresh)
            return await real(fresh)
        d.session.apps = apps
        await d.run_tool("a", "open_app", {"app": "org.example.elsewhere", "look": False})
        assert asked == [False, True]
        await d.run_tool("a", "close_app", {"app": "org.example.elsewhere"})
        assert asked == [False, True, False]
        await finish(stop, task)
    run(go)


def test_close_app_answers_without_waiting_for_the_runners_terminate(monkeypatch):
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.terminate_hangs = True  # the runner's terminate never answers
        r = await asyncio.wait_for(d.run_tool("a", "close_app", {"app": "Example"}), 2)
        assert r["text"] == "Closed Example (com.example.app)." and d.session.device.killed == ["com.example.app"]
        assert d._closing  # still going, behind the answer
        monkeypatch.setattr(daemon_module, "CLOSE_TERMINATE_CAP", 0.05)
        await asyncio.wait_for(d.run_tool("a", "open_app", {"app": "Example", "look": False}), 2)
        assert d.session.launched[-1] == "com.example.app"  # the launch waited its cap for it, then went on
        await finish(stop, task)
    run(go)


def test_an_open_app_after_a_close_app_comes_after_the_runners_terminate():
    async def go():
        d = make()
        stop, task = await started(d)
        order = []
        real_terminate, real_launch = d.session.terminate, d.session.launch

        async def terminate(bundle_id):
            await asyncio.sleep(0.05)
            order.append("terminate")
            return await real_terminate(bundle_id)

        async def launch(bundle_id, arguments=None):
            order.append("launch")
            return await real_launch(bundle_id, arguments)
        d.session.terminate, d.session.launch = terminate, launch
        await d.run_tool("a", "close_app", {"app": "Example"})
        assert order == []
        await d.run_tool("a", "open_app", {"app": "Example", "look": False})
        assert order == ["terminate", "launch"]
        await finish(stop, task)
    run(go)


def test_the_call_after_the_agents_own_close_app_looks_for_no_crash():
    async def go():
        d = tuned(make())
        d.crash_polls = 1
        stop, task = await started(d)
        await opened(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "close_app", {"app": "Example"})
        die(d)  # the next call finds what closing it leaves: the home screen in front, no process of the app
        asks = d.session.device.crash_asks
        r = await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert "crashed" not in r["text"] and not d._crash_pending
        assert d.session.device.crash_asks == asks  # no report was looked for
        assert d._closed_on_purpose is None  # it held for that one call
        await finish(stop, task)
    run(go)


def test_an_app_opened_again_after_a_close_app_is_watched_for_crashes_again():
    async def go():
        d = tuned(make())
        d.crash_polls = 1
        stop, task = await started(d)
        await opened(d)
        await d.run_tool("a", "close_app", {"app": "Example"})
        await d.run_tool("a", "open_app", {"app": "Example", "look": False})
        assert d._closed_on_purpose is None
        await d.run_tool("a", "look", {"screenshot": False})
        die(d)  # it goes away by itself
        before = d.session.device.crash_asks
        await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert d.session.device.crash_asks > before and d._crash_pending  # looked for, and looked for again later
        await finish(stop, task)
    run(go)


# ---- a read 0.5 s after a launch: the app isn't up yet ----

def test_a_read_the_runner_gave_up_at_once_is_asked_again_within_its_budget(monkeypatch):
    """Seen on the phone: TikTok read 0.5 s after its launch answered `root` in 8 ms (kAXErrorServerNotFound: no app to
    ask yet). It was taken for a screen that doesn't answer: the short budget, and its next reads left unasked."""
    from test_vision_200 import BUTTON, TIKTOK, V9Runner, _app

    monkeypatch.setattr(session_module, "RETRY_GAP", 0.01)
    runner = V9Runner()
    quick = {"token": "r1", "tree": _app(), "tier": "root", "truncated": True,
             "why": "full: Error getting main window kAXErrorServerNotFound (after 8 ms); shallow: Error getting main "
                    "window kAXErrorServerNotFound (after 3 ms)"}
    whole = {"token": "f1", "tree": _app(children=[BUTTON]), "tier": "full", "truncated": False}
    runner.trees = [quick, quick, whole]
    s = Session(runner)
    s.read_app = ("TikTok", TIKTOK)
    tree = asyncio.run(s.refresh())
    assert tree.tier == "full" and len(tree.items) == 1
    asked = [int(p.split("budget=")[1].split("&")[0]) for p in runner.paths("/wda/tw/tree")]
    assert len(asked) == 3 and asked[0] == 3000 and 2000 < asked[2] < asked[1] < 3000  # what is left of the budget
    assert not s.budgets.short(TIKTOK)  # the app is not one that doesn't answer


def test_a_read_that_used_its_budget_is_not_asked_again(monkeypatch):
    from test_vision_200 import TIKTOK, V9Runner, _app

    runner = V9Runner()
    runner.trees = [{"token": "r1", "tree": _app(), "tier": "root", "truncated": True,
                     "why": "full: Error kAXErrorIPCTimeout getting snapshot (after 1807 ms); shallow: Error getting "
                            "main window kAXErrorIPCTimeout (after 901 ms)"}]
    s = Session(runner)
    s.read_app = ("TikTok", TIKTOK)
    assert asyncio.run(s.refresh()).tier == "root" and len(runner.paths("/wda/tw/tree")) == 1
    assert s.budgets.short(TIKTOK)


def test_quick_give_ups_that_use_the_whole_budget_end_as_a_cut_read(monkeypatch):
    """An app that never has anything to answer with costs one budget, not a wait without end; then the short one."""
    from test_vision_200 import TIKTOK, V9Runner, _app

    monkeypatch.setattr(session_module, "RETRY_GAP", 0.0)
    monkeypatch.setattr(session_module, "RETRY_MIN_MS", 2998)  # (the fake answers at once: two more asks, then too little is left)
    runner = V9Runner()
    runner.trees = [{"token": "r1", "tree": _app(), "tier": "root", "truncated": True,
                     "why": "full: Error getting main window kAXErrorServerNotFound (after 8 ms)"}]
    s = Session(runner)
    s.read_app = ("TikTok", TIKTOK)
    assert asyncio.run(s.refresh()).tier == "root"
    assert len(runner.paths("/wda/tw/tree")) == 3 and s.budgets.short(TIKTOK)  # kept under the budget first asked

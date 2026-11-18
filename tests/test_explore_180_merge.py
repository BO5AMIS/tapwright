"""1.8.0 on top of 1.7.x (W158): explore and a repair's trials keep their narrowing on a whole-phone daemon. Since 1.7.5 no policy
word stops an agent's touch, so the skip floor is ONE dispatch gate on the element a move really goes to (daemon._floor_gate);
explore searches ONE app (the one named, else the one in front; never Home, Settings, Wallet or Passwords); it never unlocks the
phone; its pictures leave out an unlock and Notification Center; the caller's policy reaches the floor and nothing else."""
import json

import pytest
from fakes import button, el
from starlette.testclient import TestClient
from test_daemon import finish, make, run, started, until
from test_daemon_repair import Flow, phone, repair_lines, replay, script, tap, title

from tapwright import daemon as dm, explorefloor, exploretool, policy as policylib, repairanalysis, scriptrepair, ui
from tapwright.control import Refused
from tapwright.daemon import Scope
from tapwright.server import create_app

APPID = "com.example.app"


class Floored:
    """The floor of explore's own moves set for the block, as `_explore_run` sets it."""

    def __init__(self, app=APPID, rules=policylib.NONE):
        self.floor = explorefloor.Floor(rules, app)

    def __enter__(self):
        self.tokens = (dm._EXPLORING.set(True), dm._TOUCH_FLOOR.set(self.floor))
        return self.floor

    def __exit__(self, *exc):
        dm._TOUCH_FLOOR.reset(self.tokens[1])
        dm._EXPLORING.reset(self.tokens[0])


async def refused(d, name, args):
    with pytest.raises(Refused) as e:
        await d.run_tool("a", name, args)
    return e.value


def test_the_gate_judges_the_element_the_touch_really_goes_to():
    """A benign target whose centre has a Delete button drawn over it, a point, a swipe, a password field and another app's
    screen are all refused under the floor, the dry run too; what the floor doesn't hold goes through; an agent's own touch of
    Delete has no gate at all (1.7.5)."""
    async def go():
        d = make()
        d.session.tree = [button("e1", "Open", "open", rect=(10, 100, 200, 40)),
                          button("e2", "Delete", "", rect=(90, 110, 40, 20)),  # over Open's centre, drawn in front
                          button("e3", "OK", "ok-button", rect=(10, 300, 100, 40)),
                          el("e4", "input", "Password", "pw", rect=(10, 400, 200, 40))]
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        with Floored():
            for name, args in (("tap", {"target": {"id": "open"}}), ("tap", {"target": {"id": "open"}, "dry_run": True}),
                               ("tap", {"target": {"x": 50, "y": 320}}),
                               ("swipe", {"x0": 50, "y0": 500, "x1": 50, "y1": 200}),
                               ("type_text", {"target": {"id": "pw"}, "text": "x"})):
                e = await refused(d, name, {**args, "look": False})
                assert e.code == "not_allowed" and "needs_confirmation" in str(e), (name, args, str(e))
            await d.run_tool("a", "tap", {"target": {"id": "ok-button"}, "look": False})  # the floor holds nothing here
            d.session.app = ("Other", "com.other.app")  # another app in front: nothing of it is touched
            e = await refused(d, "tap", {"target": {"id": "ok-button"}, "look": False})
            assert "another app" in str(e)
        assert d.session.calls.count("tap") == 1 and d.session.typed == []
        d.session.app = ("Example", APPID)
        await d.run_tool("a", "tap", {"target": {"label": "Delete"}, "look": False})  # the agent's own call: no gate
        assert d.session.calls.count("tap") == 2
        await finish(stop, task)
    run(go)


def test_an_alert_is_answered_only_with_the_button_the_floor_picks():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.last_alert = ui.Alert("Allow “Example” to use your location?",
                                        ("Allow Once", "Allow While Using App", "Don't Allow"), (50, 300, 300, 200))
        with Floored():
            for args in ({"action": "accept", "name": "Allow Once"}, {"action": "accept"}, {"action": "dismiss"}):
                e = await refused(d, "alert", {**args, "look": False})
                assert "needs_confirmation" in str(e), args
            await d.run_tool("a", "alert", {"action": "dismiss", "name": "Don't Allow", "look": False})
        assert d.session.calls.count("alert") == 1
        await finish(stop, task)
    run(go)


def test_explore_presses_the_alert_button_it_chose_by_name():
    async def go():
        d = make()
        stop, task = await started(d)
        sent = []

        async def alert(action, *, name=None):
            sent.append((action, name))
            return "sent"
        d.session.alert = alert
        d.session.last_alert = ui.Alert("“Example” Would Like to Send You Notifications", ("Don't Allow", "Allow"), (50, 300, 300, 200))
        phone_side = exploretool.DaemonPhone(d, "a", None, APPID)
        with Floored():
            await phone_side.alert("Don't Allow")
        assert sent == [("dismiss", "Don't Allow")]
        await finish(stop, task)
    run(go)


def test_no_picture_of_an_unlock_or_of_notification_center():
    async def go():
        d = make()
        d.session.foreground = APPID
        stop, task = await started(d)
        phone_side = exploretool.DaemonPhone(d, "a", None, APPID)
        assert await phone_side.picture() is not None  # the app in front, nothing private: a picture
        span = d._unlock_begin()
        assert await phone_side.picture() is None
        d._unlock_end(span)
        d._center_begin()
        assert await phone_side.picture() is None
        await finish(stop, task)
    run(go)


def test_explore_searches_the_one_app_named_and_never_home_or_settings():
    async def go():
        d = make()
        d.session.app = ("Other", "com.other.app")
        stop, task = await started(d)
        token = dm._APP_HINT.set(APPID)
        try:
            assert await d._explore_app(None) == APPID  # the app named, though another is in front (explore opens it)
        finally:
            dm._APP_HINT.reset(token)
        for bundle in ("com.apple.Preferences", "com.apple.springboard"):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "explore", {}, Scope((bundle,)))
            assert e.value.code == "not_allowed" and "never explored" in str(e.value), bundle
        await finish(stop, task)
    run(go)


def test_a_locked_phone_ends_explore_and_explore_never_unlocks_it(tmp_path):
    from test_daemon_explore import explored

    async def go():
        d = explored(tmp_path)
        tried = []

        async def unlock_phone():
            tried.append(1)
            raise AssertionError("explore must never unlock the phone")
        d.unlock_phone = unlock_phone
        d._unlocks_itself = lambda *a: True  # serve --phone with a passcode: a top-level call would unlock
        stop, task = await started(d)
        d.session.is_locked = True
        await until(lambda: d.state == "locked")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "explore", {}, Scope((APPID,)))
        assert e.value.code == "locked" and "explore never unlocks it" in str(e.value) and tried == []
        await finish(stop, task)
    run(go)


def test_a_repair_trial_passes_the_same_gate(tmp_path, monkeypatch):
    """Whatever the analysis chose, a candidate whose touch would go to what the floor holds is never touched."""
    tree = [title("Welcome"), button("e1", "Delete trip now", "delete-trip-btn", rect=(10, 600, 100, 40))]
    bad = scriptrepair.Candidate("target", "tap id=delete-trip-btn", 'button "Delete trip now"', "a test's own pick", 0.99,
                                 target={"id": "delete-trip-btn"})
    monkeypatch.setattr(repairanalysis, "analyse", lambda *a, **k: scriptrepair.Analysis("ok", [bad]))

    async def go():
        d = phone(tmp_path, Flow(tree))
        stop, task = await started(d)
        with pytest.raises(Refused):
            await replay(d, script(tmp_path, [tap({"id": "login-btn"}, expect={"text": "Home"})]), "apply")
        assert "tap" not in d.session.calls
        lines = repair_lines(tmp_path)
        assert lines and all("rejected" in ln for ln in lines) and "needs_confirmation" in lines[0]
        await finish(stop, task)
    run(go)


def test_the_server_gives_explore_its_app_and_the_policy_for_its_floor_only():
    TOKEN = "t" * 43
    d = make()
    seen = []

    async def run_tool(client_id, name, args, scope=None, policy=None, **via):
        seen.append((name, policy, via.get("app_hint")))
        return {"text": "ok", "image": None, "image_size": None}
    d.run_tool = run_tool
    rules = policylib.parse({"explore": {"skip": ["Archive"]}})
    head = {"Authorization": f"Bearer {TOKEN}", "X-Tapwright-Client": "agent-1", "X-Tapwright-Apps": APPID,
            "X-Tapwright-Policy": rules.dumps()}
    with TestClient(create_app(d, TOKEN, hosts={"testserver"})) as c:
        assert c.post("/api/explore", headers=head, json={}).status_code == 200
        assert c.post("/api/tap", headers=head, json={"target": {"id": "x"}}).status_code == 200
        r = c.post("/api/explore", headers={**head, "X-Tapwright-Policy": "{not json"}, json={})
        assert r.status_code == 400 and r.json()["error"]["code"] == "bad_params"
        assert c.post("/api/tap", headers={**head, "X-Tapwright-Policy": "{not json"}, json={}).status_code == 200
    (n1, p1, h1), (n2, p2, h2) = seen[0], seen[1]
    assert n1 == "explore" and p1.explore.skip == ("Archive",) and h1 == APPID
    assert n2 == "tap" and p2 is None and h2 is None  # (any other call: legacy headers are no limit, 1.7.5)
    assert json.dumps(len(seen)) == "3"


def test_a_redactor_that_fails_hides_the_text():
    """Tapline's lesson: an exception in the redactor hides the whole text; it is never passed on as it was."""
    from tapwright import findings

    def broken(text):
        raise RuntimeError("redactor down")
    assert findings.safe_scrub(broken)("SECRET-VALUE 123") == findings.HIDDEN
    assert findings.safe_scrub(lambda t: t.replace("SECRET", "***"))("SECRET x") == "*** x"
    d = make()
    d.scrub_everywhere = broken
    assert d._safe_scrub("SECRET-VALUE") == findings.HIDDEN

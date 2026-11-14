"""Review 1.7, the safety findings W143 fixed: B-H1 (the phone in use for a schedule, the phone lock, a scheduled run's
hold), B-H2 (a draft stop decided by the words that reach people, not the policy file; `submit` that would send), B-M7
(a full swipe on a list row), B-L9 (a pending request reused only in the same draft mode), A-M1 (a grant's route met
only by the map's own route file; record never replaces a route a grant or a schedule names), A-M3 (account deletions and
payments recognised by pattern). B-M6 is in test_starters.py. No phone."""
import asyncio
import datetime
import json
import time

import pytest
from fakes import button, el
from test_daemon import finish, make, run, started

from tapwright import daemon as daemon_module
from tapwright import grants, mapkeeper, pidlock, policy, schedulerun, schedules
from tapwright.actionlog import ActionLog
from tapwright.auditlog import AuditLog
from tapwright.control import Refused

APP = "com.example.app"  # the fake session's app
TZ = datetime.timezone(datetime.timedelta(hours=3))
NOW = datetime.datetime(2026, 11, 14, 10, 0, tzinfo=TZ)
T1 = 1_763_013_612_481
BUNDLE = "com.example.wayfare"


def screen(d, *extra):
    d.session.tree = [button("e1", "Send", "send"), button("e2", "Reply", "reply", rect=(10, 300, 100, 40)),
                      button("e3", "Comment", "comment", rect=(10, 400, 100, 40)),
                      el("e4", "input", "Message", "message", value="Hello there", rect=(10, 200, 300, 44)), *extra]


def daemon_with(tmp_path, obj: dict | None):
    """A daemon and the policy to pass: `obj` written as a policy file (version 2 when it holds grants), or the
    built-in NONE (`--no-policy`) for None."""
    audit = AuditLog(tmp_path / "audit", now=lambda: NOW.astimezone(datetime.timezone.utc))
    d = make(audit=audit, grant_clock=lambda: NOW, actions=ActionLog(tmp_path / "log"))
    screen(d)
    if obj is None:
        return d, policy.NONE
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(obj), encoding="utf-8")
    return d, policy.load(path)


def draft_grant(**over):
    g = {"name": "chat_send", "app": APP, "words": ["Send"], "until": "2026-11-21", "perDay": 3, "draftsOnly": True}
    g.update(over)
    return g


def taps(d, tool="tap"):
    return [c for c in d.session.calls if (c if isinstance(c, str) else c[0]) == tool]


async def ask_of(d):
    out = await d.dialog_poll("a", 0, 2.0)
    assert out["events"], "the daemon asked the host nothing"
    return out["events"][0]


# ---- B-H2: the draft stop is the reach words', whatever the policy file says ---------------------------------------
@pytest.mark.parametrize("rules", [None, {"confirm": ["Buy"], "block": []},
                                   {"confirm": ["Send"], "block": [], "apps": {APP: {"confirm": []}}}])
@pytest.mark.parametrize("target,word", [("send", "Send"), ("reply", "Reply"), ("comment", "Comment")])
def test_a_draftsonly_route_stops_at_the_draft_whatever_the_policy_says(tmp_path, rules, target, word):
    async def go():
        d, given = daemon_with(tmp_path, rules)
        stop, task = await started(d)
        token = daemon_module._DRAFTS_ROUTE.set(True)
        try:
            await d.run_tool("a", "look", {"screenshot": False}, policy=given)
            call = asyncio.create_task(d.run_tool("a", "tap", {"target": {"id": target}, "look": False}, policy=given,
                                                  dialog=True))
            ask = await ask_of(d)
            assert ask["kind"] == "draft" and ask["message"] == f'{word} this in Example? "Hello there"'
            assert taps(d) == []  # nothing was touched
            d.dialog_answer("a", ask["id"], "decline")
            with pytest.raises(Refused) as e:
                await asyncio.wait_for(call, 2)
            assert e.value.code == "not_allowed" and e.value.draft is True and taps(d) == []
        finally:
            daemon_module._DRAFTS_ROUTE.reset(token)
        await finish(stop, task)
    run(go)


def test_a_draftsonly_grant_stops_at_the_draft_when_the_policy_has_no_such_word(tmp_path):
    async def go():
        d, given = daemon_with(tmp_path, {"version": 2, "confirm": ["Buy"], "block": [], "grants": [draft_grant()]})
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False}, policy=given)
        call = asyncio.create_task(d.run_tool("a", "tap", {"target": {"id": "send"}, "look": False}, policy=given,
                                              dialog=True))
        ask = await ask_of(d)
        assert ask["kind"] == "draft" and ask["message"] == 'Send this in Example? "Hello there"' and taps(d) == []
        d.dialog_answer("a", ask["id"], "allow")
        r = await asyncio.wait_for(call, 2)
        assert r["text"].splitlines()[-1] == "Note: sent by a person (in chat)." and len(taps(d)) == 1
        # a word that reaches nobody, with no policy word, still goes through untouched by any of this
        r = await d.run_tool("a", "tap", {"target": {"id": "message"}, "look": False}, policy=given)
        assert "needs_confirmation" not in r["text"]
        await finish(stop, task)
    run(go)


def test_outside_a_draftsonly_route_or_grant_a_send_with_no_policy_word_goes_through(tmp_path):
    async def go():
        d, given = daemon_with(tmp_path, None)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False}, policy=given)
        await d.run_tool("a", "tap", {"target": {"id": "send"}, "look": False}, policy=given)
        assert len(taps(d)) == 1
        await finish(stop, task)
    run(go)


@pytest.mark.parametrize("where", ["route", "grant"])
def test_submit_is_refused_where_a_send_stops_at_a_draft(tmp_path, where):
    async def go():
        rules = {"version": 2, "confirm": [], "block": [], "grants": [draft_grant()] if where == "grant" else []}
        d, given = daemon_with(tmp_path, rules)
        stop, task = await started(d)
        token = daemon_module._DRAFTS_ROUTE.set(True) if where == "route" else None
        try:
            await d.run_tool("a", "look", {"screenshot": False}, policy=given)
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "type_text", {"text": "hi", "target": {"id": "message"}, "submit": True},
                                 policy=given)
            assert e.value.code == "not_allowed" and "submit would send" in str(e.value)
            assert "tap the send button" in str(e.value)
            assert not taps(d, "type")
            # without submit it types, as always
            r = await d.run_tool("a", "type_text", {"text": "hi", "target": {"id": "message"}, "look": False},
                                 policy=given)
            assert r["text"].startswith("Typed into")
        finally:
            if token is not None:
                daemon_module._DRAFTS_ROUTE.reset(token)
        await finish(stop, task)
    run(go)


def test_submit_goes_through_with_no_draftsonly_route_or_grant(tmp_path):
    async def go():
        d, given = daemon_with(tmp_path, {"version": 2, "confirm": [], "block": [], "grants": [draft_grant(app="x.y")]})
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False}, policy=given)
        r = await d.run_tool("a", "type_text", {"text": "hi", "target": {"id": "message"}, "submit": True, "look": False},
                             policy=given)
        assert r["text"].startswith("Typed into")
        await finish(stop, task)
    run(go)


# ---- B-L9: a pending request is reused only in the same draft mode ----------------------------------------------------
def test_a_pending_plain_request_is_never_reused_for_a_draft(tmp_path):
    async def go():
        d, given = daemon_with(tmp_path, {"confirm": ["Send"], "block": []})
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False}, policy=given)
        with pytest.raises(Refused):  # a plain request, pending in the live view ("Allow tap Send?", no draft)
            await d.run_tool("a", "tap", {"target": {"id": "send"}, "look": False}, policy=given)
        [plain] = list(d._confirms.values())
        assert plain.draft is None
        token = daemon_module._DRAFTS_ROUTE.set(True)
        try:
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "tap", {"target": {"id": "send"}, "look": False}, policy=given)
        finally:
            daemon_module._DRAFTS_ROUTE.reset(token)
        assert e.value.draft is True
        drafts = [c for c in d._confirms.values() if c is not plain]
        assert len(drafts) == 1 and drafts[0].draft is not None
        assert drafts[0].message["caption"] == 'Send this in Example? "Hello there"'
        # the person allows the plain one: the route's Send still doesn't go through on it
        await d.viewer_message({"type": "confirm_answer", "id": plain.id, "allow": True})
        token = daemon_module._DRAFTS_ROUTE.set(True)
        try:
            with pytest.raises(Refused):
                await d.run_tool("a", "tap", {"target": {"id": "send"}, "look": False}, policy=given)
        finally:
            daemon_module._DRAFTS_ROUTE.reset(token)
        assert taps(d) == []
        await finish(stop, task)
    run(go)


# ---- B-M7: a full swipe on a list row --------------------------------------------------------------------------------
ROW = (0, 500, 400, 60)


def row_screen(d):
    screen(d, el("e9", "cell", "Lunch with Sam", "row-1", rect=ROW))


def test_a_full_swipe_across_a_list_row_needs_a_persons_ok_and_a_short_one_doesnt(tmp_path):
    async def go():
        d, given = daemon_with(tmp_path, {"version": 2, "confirm": ["Delete"], "block": [],
                                          "grants": [draft_grant(name="del", words=["Delete"], draftsOnly=False)]})
        row_screen(d)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False}, policy=given)
        full = {"x0": 380, "y0": 530, "x1": 20, "y1": 530}
        dry = await d.run_tool("a", "swipe", {**full, "dry_run": True}, policy=given)
        assert "It needs a person's OK (a full swipe on a list row" in dry["text"] and not taps(d, "swipe")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "swipe", full, policy=given)
        assert e.value.code == "not_allowed" and str(e.value).startswith("needs_confirmation: a full swipe on cell")
        assert "short swipe" in str(e.value) and not taps(d, "swipe")
        [request] = list(d._confirms.values())
        assert request.offer is None  # no grant is ever offered for it
        # a short swipe reveals the row's buttons: it goes as before
        await d.run_tool("a", "swipe", {"x0": 380, "y0": 530, "x1": 260, "y1": 530, "look": False}, policy=given)
        assert len(taps(d, "swipe")) == 1
        # a mostly-vertical swipe that starts on a row is a scroll
        await d.run_tool("a", "swipe", {"x0": 200, "y0": 530, "x1": 120, "y1": 100, "look": False}, policy=given)
        assert len(taps(d, "swipe")) == 2
        # the person allows it: the same call makes the swipe, once
        await d.viewer_message({"type": "confirm_answer", "id": request.id, "allow": True})
        r = await d.run_tool("a", "swipe", {**full, "look": False}, policy=given)
        assert r["text"].splitlines()[-1] == "Note: allowed by a person (in the live view)." and len(taps(d, "swipe")) == 3
        with pytest.raises(Refused):
            await d.run_tool("a", "swipe", full, policy=given)
        await finish(stop, task)
    run(go)


@pytest.mark.parametrize("who", ["route", "schedule"])
def test_a_full_row_swipe_is_never_made_in_a_draftsonly_route_or_a_scheduled_run(tmp_path, who):
    async def go():
        d, given = daemon_with(tmp_path, None)
        row_screen(d)
        stop, task = await started(d)
        client = "schedule-tidy" if who == "schedule" else "a"
        token = daemon_module._DRAFTS_ROUTE.set(True) if who == "route" else None
        try:
            await d.run_tool(client, "look", {"screenshot": False}, policy=given)
            with pytest.raises(Refused) as e:
                await d.run_tool(client, "swipe", {"x0": 380, "y0": 530, "x1": 20, "y1": 530}, policy=given, dialog=True)
        finally:
            if token is not None:
                daemon_module._DRAFTS_ROUTE.reset(token)
        assert e.value.code == "not_allowed" and "needs_confirmation" in str(e.value) and "short swipe" in str(e.value)
        assert not d._confirms and not taps(d, "swipe")  # nobody is asked: it is never made there
        await finish(stop, task)
    run(go)


# ---- B-H1: the phone in use, for the schedules --------------------------------------------------------------------------
def test_a_recent_call_or_a_persons_input_holds_the_phone_for_90_seconds(tmp_path):
    async def go():
        d = make()
        stop, task = await started(d)
        now = time.monotonic()
        d.control._agent_at = None
        assert d.status()["in_use"] is False and d.phone_in_use() is False
        await d.run_tool("a", "look", {"screenshot": False})
        d.control._agent_at = None  # the queue's 3 s are over: an agent thinking between its calls
        assert d.control.driver is None and d.status()["in_use"] is True
        d._call_at["a"] = now - 60
        assert d.phone_in_use() is True
        d._call_at["a"] = now - 100
        assert d.phone_in_use() is False
        d._call_at["schedule-x"] = now - 5  # a scheduled run's own finished call holds nothing
        assert d.phone_in_use() is False
        d._person_at = now - 60  # a person in the live view a minute ago
        assert d.phone_in_use() is True
        await finish(stop, task)
    run(go)


def test_other_agents_actions_and_the_trigger_check_are_busy_while_a_scheduled_run_drives(tmp_path):
    async def go():
        d = make()
        d.session.tree = [button("e1", "OK", "ok-button")]
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        gate, inside = asyncio.Event(), asyncio.Event()
        real = d.session.swipe

        async def held_swipe(*args, **kwargs):
            inside.set()
            await gate.wait()
            return await real(*args, **kwargs)
        d.session.swipe = held_swipe
        run_ = asyncio.create_task(d.run_tool("schedule-post", "swipe", {"x0": 200, "y0": 600, "x1": 200, "y1": 200,
                                                                         "look": False}))
        await asyncio.wait_for(inside.wait(), 2)
        with pytest.raises(Refused) as e:  # (at once: before 1.7's fix it queued behind the run's own touch)
            await asyncio.wait_for(d.run_tool("a", "tap", {"target": {"id": "ok-button"}}), 2)
        assert e.value.code == "busy" and "scheduled run" in str(e.value)
        with pytest.raises(Refused) as e:
            await d.notification_keys(APP)
        assert e.value.code == "busy"
        gate.set()
        await asyncio.wait_for(run_, 2)
        d.session.swipe = real
        d._call_at.clear()
        d._person_at = -float("inf")
        d.control._agent_at = None
        r = await d.run_tool("a", "tap", {"target": {"id": "ok-button"}, "look": False})  # the run is over
        assert r["text"].startswith("Tapped")
        await finish(stop, task)
    run(go)


class Phone:
    class DaemonError(RuntimeError):
        def __init__(self, code, message="", draft=False):
            super().__init__(message)
            self.code, self.draft = code, draft

    def __init__(self, status=None):
        self.state = status or {"state": "live", "driver": None, "paused": False}
        self.calls, self.asked = [], []

    def status(self):
        return self.state

    def notification_keys(self, app):
        self.asked.append(app)
        return ["k"]

    def set_scope(self, apps, dedicated=False):
        pass

    set_policy = set_accounts = set_maps = lambda self, x=None: None

    def call(self, tool, args, client_id, timeout=120.0, dialog=False):
        self.calls.append((tool, client_id))
        return {"text": "Arrived."}


class Clock:
    def __init__(self):
        self.t = 0.0

    def sleep(self, seconds):
        self.t += seconds

    def monotonic(self):
        return self.t


@pytest.fixture
def home(tmp_path, monkeypatch):
    from tapwright import store
    monkeypatch.setattr(store, "HOME", tmp_path / "home")
    routes = tmp_path / "home" / "maps" / BUNDLE / "routes"
    routes.mkdir(parents=True)
    (routes / "post_update.json").write_text("{}", encoding="utf-8")
    return tmp_path / "home"


def deps_for(tmp_path, phone, trigger=None):
    where = tmp_path / "sched"
    schedules.write(schedules.Schedule("post", "post_update", {}, None if trigger else "09:00 daily", trigger), where)
    clock = Clock()
    return schedulerun.Deps(client=phone, sleep=clock.sleep, monotonic=clock.monotonic, where=where, runs=tmp_path / "runs",
                            audit_folder=tmp_path / "audit", unlock_phone=None,
                            lock=pidlock.Deps(pid=4242, alive=lambda p: True, start_of=lambda p: T1)), clock


def other_run_holds_the_phone(deps):
    """Another schedule's run (a live process) holds the phone lock."""
    holder = pidlock.Deps(pid=7777, alive=lambda p: True, start_of=lambda p: T1)
    return pidlock.take(schedules.phone_lock_of(deps.where), holder)


def test_two_scheduled_runs_never_overlap_on_the_phone(tmp_path, home):
    phone = Phone()
    deps, clock = deps_for(tmp_path, phone)
    token = other_run_holds_the_phone(deps)
    out = schedulerun.run("post", deps=deps)
    assert (out.status, out.result) == ("skipped", schedulerun.IN_USE) and phone.calls == [] and clock.t >= deps.wait
    pidlock.release(schedules.phone_lock_of(deps.where), token)
    out = schedulerun.run("post", deps=deps)
    assert out.status == "done" and phone.calls == [("drive", "schedule-post")]
    assert not schedules.phone_lock_of(deps.where).exists()  # released after the drive


def test_a_trigger_check_never_runs_inside_a_run(tmp_path, home):
    phone = Phone()
    deps, _ = deps_for(tmp_path, phone, {"notification": BUNDLE})
    s = schedules.load("post", deps.where)
    token = other_run_holds_the_phone(deps)
    assert schedulerun.notification_arrived(s, deps) is False and phone.asked == []
    pidlock.release(schedules.phone_lock_of(deps.where), token)
    schedulerun.notification_arrived(s, deps)
    assert phone.asked == [BUNDLE] and not schedules.phone_lock_of(deps.where).exists()


def test_the_run_waits_while_the_daemon_says_the_phone_is_in_use(tmp_path, home):
    phone = Phone({"state": "live", "driver": None, "paused": False, "in_use": True})
    deps, _ = deps_for(tmp_path, phone)
    assert schedulerun.run("post", deps=deps).result == schedulerun.IN_USE and phone.calls == []
    trigger = Phone({"state": "live", "driver": None, "paused": False, "in_use": True})
    deps, _ = deps_for(tmp_path, trigger, {"notification": BUNDLE})
    assert schedulerun.notification_arrived(schedules.load("post", deps.where), deps) is False and trigger.asked == []


def test_a_stuck_phone_lock_is_named_by_doctor_and_removed_by_unlock(tmp_path):
    where = tmp_path / "sched"
    where.mkdir()
    schedules.phone_lock_of(where).write_text("", encoding="utf-8")
    [(path, why)] = schedules.stuck_phone_lock(where)
    assert path.endswith(".phone.lock") and why
    text, code = schedules.unlock_phone(where, tmp_path / "audit")
    assert code == 0 and not schedules.phone_lock_of(where).exists() and "Removed" in text


# ---- A-M1: a grant's route is the map's own route file -------------------------------------------------------------
def test_only_the_maps_own_route_file_counts_as_the_route(tmp_path):
    from test_daemon_routes import explore, mapped
    from mapfakes import APP as MAP_APP

    async def go():
        d = mapped(tmp_path / "maps")
        stop, task = await started(d)
        await explore(d)
        await d.run_tool("a", "record", {"start": True, "route": "sign_in"})
        await d.run_tool("a", "tap", {"target": {"id": "continue-btn"}})
        await d.run_tool("a", "tap", {"target": {"id": "sign-in-btn"}})
        await d.run_tool("a", "record", {"stop": True})
        await d.maps.flush()
        own = tmp_path / "maps" / MAP_APP[1] / "routes" / "sign_in.json"
        script = json.loads(own.read_text("utf-8"))
        script["fresh"] = False
        own.write_text(json.dumps(script), "utf-8")
        elsewhere = tmp_path / "mine.json"
        elsewhere.write_text(json.dumps(script), "utf-8")  # the model's own script, naming itself sign_in
        seen = []
        real = d.session.tap

        async def tap(*args, **kwargs):
            seen.append(daemon_module._ROUTES.get())
            return await real(*args, **kwargs)
        d.session.tap = tap
        d.session.at = "welcome"
        await d.run_tool("a", "replay", {"path": str(elsewhere)})
        assert seen and all(r == () for r in seen)
        seen.clear()
        d.session.at = "welcome"
        await d.run_tool("a", "replay", {"path": str(own)})
        assert seen and all(r == ("sign_in",) for r in seen)
        await finish(stop, task)
    run(go)


def test_record_never_replaces_a_route_a_grant_or_a_schedule_names(tmp_path):
    from test_daemon_routes import explore, mapped
    from mapfakes import APP as MAP_APP

    async def go():
        today = datetime.date.today()
        path = tmp_path / "policy.json"
        path.write_text(json.dumps({"version": 2, "confirm": ["Post"], "block": [], "grants": [
            {"name": "g", "app": MAP_APP[1], "words": ["Post"], "route": "sign_in", "perDay": 1,
             "until": (today + datetime.timedelta(days=5)).isoformat()}]}), encoding="utf-8")
        rules = policy.load(path)
        d = mapped(tmp_path / "maps")
        d.schedules_folder = tmp_path / "sched"
        schedules.write(schedules.Schedule("daily", "home_run", {}, "09:00 daily", None), d.schedules_folder)
        stop, task = await started(d)
        await explore(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "record", {"start": True, "route": "sign_in"}, policy=rules)
        assert e.value.code == "not_allowed" and 'grant "g" names it' in str(e.value)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "record", {"start": True, "route": "home_run"}, policy=rules)
        assert e.value.code == "not_allowed" and 'schedule "daily" runs it' in str(e.value)
        r = await d.run_tool("a", "record", {"start": True, "route": "other"}, policy=rules)
        assert r["text"].startswith("Recording route other")
        await finish(stop, task)
    run(go)


# ---- A-M3: never grantable, by pattern ------------------------------------------------------------------------------
@pytest.mark.parametrize("label", ["Delete your account", "Remove account", "Delete this account", "Close my profile",
                                   "Terminate account", "Send $25", "Send Money Now", "Confirm order · $49.99",
                                   "Pay 25 QAR", "Transfer", "Checkout", "Add a tip", "Confirm 30 EUR", "Send £5"])
def test_account_deletions_and_payments_are_never_covered(label):
    assert grants.touch_never(APP, label) is True


@pytest.mark.parametrize("label", ["Delete trip", "Send", "Post", "Remove photo", "Account settings", "Close",
                                   "Profile", "Reply"])
def test_ordinary_words_stay_grantable(label):
    assert grants.touch_never(APP, label) is False


def test_a_grant_naming_a_payment_or_an_account_deletion_is_refused_when_it_loads():
    for word, why in (("Send money", "payments"), ("Transfer", "payments"), ("Remove profile", "deleting an account")):
        loaded, refused = grants.parse_grants([{"name": "g", "app": APP, "words": [word], "until": "2026-11-20"}],
                                              datetime.date(2026, 11, 14), lambda app: ())
        assert not loaded and why in refused[0].reason, word

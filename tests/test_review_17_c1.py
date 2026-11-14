"""Codex's review of PR #12, cycle 1: H1 (an account-deletion alert's Delete is never covered by a grant), H2 (a route's
draftsOnly stop comes before any grant), H3 (a draft approval is for the draft the person saw). No phone."""
import asyncio
import datetime
import json

import pytest
from fakes import button, el
from test_daemon import finish, make, run, started

from tapwright import daemon as daemon_module
from tapwright import policy, ui
from tapwright.actionlog import ActionLog
from tapwright.auditlog import AuditLog
from tapwright.control import Refused

APP = "com.example.app"  # the fake session's app
TZ = datetime.timezone(datetime.timedelta(hours=3))
NOW = datetime.datetime(2026, 11, 14, 10, 0, tzinfo=TZ)


def daemon_with(tmp_path, obj: dict):
    audit = AuditLog(tmp_path / "audit", now=lambda: NOW.astimezone(datetime.timezone.utc))
    d = make(audit=audit, grant_clock=lambda: NOW, actions=ActionLog(tmp_path / "log"))
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(obj), encoding="utf-8")
    return d, policy.load(path)


def grant(**over):
    g = {"name": "wayfare_posts", "app": APP, "words": ["Delete"], "until": "2026-11-21", "perDay": 3,
         "draftsOnly": False}
    g.update(over)
    return g


def calls_of(d, tool):
    return [c for c in d.session.calls if (c if isinstance(c, str) else c[0]) == tool]


def uses(tmp_path):
    return sorted(p.name for p in (tmp_path / "policy.json.uses").rglob("*") if p.is_file()) \
        if (tmp_path / "policy.json.uses").exists() else []


async def ask_of(d, since=0):
    out = await d.dialog_poll("a", since, 2.0)
    assert out["events"], "the daemon asked the host nothing"
    return out["events"][0], out["seq"]


# ---- H1: an account-deletion alert's Delete is never covered ----------------------------------------------------------
ALERT = ui.Alert("Delete your account?", ("Cancel", "Delete"), (60.0, 300.0, 300.0, 200.0),
                 message="All your trips go with it.")


def test_h1_alert_accept_in_an_account_deletion_alert_asks_a_person_whatever_the_delete_grant(tmp_path):
    async def go():
        d, rules = daemon_with(tmp_path, {"version": 2, "confirm": ["Delete"], "block": [], "grants": [grant()]})
        d.session.tree = [button("e1", "OK", "ok")]
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
        d.session.last_alert = ALERT
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "alert", {"action": "accept"}, policy=rules)
        assert e.value.code == "not_allowed" and "needs_confirmation" in str(e.value)
        assert calls_of(d, "alert") == [] and uses(tmp_path) == []
        await finish(stop, task)
    run(go)


def test_h1_a_tap_on_the_delete_button_of_an_account_deletion_alert_asks_a_person(tmp_path):
    async def go():
        d, rules = daemon_with(tmp_path, {"version": 2, "confirm": ["Delete"], "block": [], "grants": [grant()]})
        d.session.tree = [el("e1", "text", "Delete your account?", rect=(70, 320, 280, 20), in_alert=True),
                          button("e2", "Cancel", "cancel", rect=(70, 440, 130, 44), in_alert=True),
                          button("e3", "Delete", "delete", rect=(220, 440, 130, 44), in_alert=True)]
        d.session.last_alert = ALERT
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "delete"}}, policy=rules)
        assert e.value.code == "not_allowed" and "needs_confirmation" in str(e.value)
        assert calls_of(d, "tap") == [] and uses(tmp_path) == []
        await finish(stop, task)
    run(go)


def test_h1_an_account_deletion_screen_keeps_its_confirm_from_any_grant_and_from_the_offer(tmp_path):
    async def go():
        d, rules = daemon_with(tmp_path, {"version": 2, "confirm": ["Delete"], "block": [], "grants": [grant()]})
        d.session.tree = [el("e1", "header", "Delete account", rect=(10, 60, 300, 30)),
                          el("e2", "text", "This deletes your account and every trip.", rect=(10, 100, 300, 40)),
                          button("e3", "Delete", "delete", rect=(10, 300, 300, 44))]
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
        call = asyncio.create_task(d.run_tool("a", "tap", {"target": {"id": "delete"}, "look": False}, policy=rules,
                                              dialog=True))
        ask, seq = await ask_of(d)
        assert ask["kind"] == "confirm" and calls_of(d, "tap") == [] and uses(tmp_path) == []
        d.dialog_answer("a", ask["id"], "allow")
        await asyncio.wait_for(call, 2)
        assert len(calls_of(d, "tap")) == 1
        out = await d.dialog_poll("a", seq, 0.2)  # no grant is offered for it (§19.6: never for §19.2)
        assert [x for x in out["events"] if x.get("type") == "ask"] == []
        await finish(stop, task)
    run(go)


def test_h1_an_ordinary_delete_alert_is_still_covered(tmp_path):
    async def go():
        d, rules = daemon_with(tmp_path, {"version": 2, "confirm": ["Delete"], "block": [], "grants": [grant()]})
        d.session.tree = [button("e1", "OK", "ok")]
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
        d.session.last_alert = ui.Alert("Delete trip?", ("Cancel", "Delete"), (60.0, 300.0, 300.0, 200.0))
        r = await d.run_tool("a", "alert", {"action": "accept"}, policy=rules)
        assert 'allowed by grant "wayfare_posts"' in r["text"] and len(calls_of(d, "alert")) == 1
        await finish(stop, task)
    run(go)


# ---- H2: a draftsOnly route stops at the draft, whatever the grants ---------------------------------------------------
def chat(d, text="Hello there"):
    d.session.tree = [button("e1", "Post", "post"),
                      el("e2", "input", "Message", "message", value=text, rect=(10, 200, 300, 44))]


def test_h2_an_unrestricted_grant_never_lifts_a_routes_draftsonly(tmp_path):
    async def go():
        d, rules = daemon_with(tmp_path, {"version": 2, "confirm": ["Post"], "block": [],
                                          "grants": [grant(words=["Post"])]})
        chat(d)
        stop, task = await started(d)
        token = daemon_module._DRAFTS_ROUTE.set(True)
        try:
            await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
            dry = await d.run_tool("a", "tap", {"target": {"id": "post"}, "dry_run": True}, policy=rules)
            assert "It would stop at a draft" in dry["text"] and "allowed by grant" not in dry["text"]
            call = asyncio.create_task(d.run_tool("a", "tap", {"target": {"id": "post"}, "look": False}, policy=rules,
                                                  dialog=True))
            ask, _ = await ask_of(d)
            assert ask["kind"] == "draft" and ask["message"] == 'Post this in Example? "Hello there"'
            assert calls_of(d, "tap") == [] and uses(tmp_path) == []
            d.dialog_answer("a", ask["id"], "allow")
            r = await asyncio.wait_for(call, 2)
            assert r["text"].splitlines()[-1] == "Note: sent by a person (in chat)." and len(calls_of(d, "tap")) == 1
            assert uses(tmp_path) == []  # a draft a person sent claims no use of the grant
        finally:
            daemon_module._DRAFTS_ROUTE.reset(token)
        await finish(stop, task)
    run(go)


# ---- H3: an approval is for the draft the person saw ------------------------------------------------------------------
def test_h3_a_draft_changed_after_the_live_view_allow_is_asked_again(tmp_path):
    async def go():
        d, rules = daemon_with(tmp_path, {"version": 2, "confirm": ["Post"], "block": [], "grants": []})
        chat(d)
        stop, task = await started(d)
        token = daemon_module._DRAFTS_ROUTE.set(True)
        try:
            await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "tap", {"target": {"id": "post"}, "look": False}, policy=rules)
            assert e.value.draft is True
            [first] = list(d._confirms.values())
            assert first.message["caption"] == 'Post this in Example? "Hello there"'
            await d.viewer_message({"type": "confirm_answer", "id": first.id, "allow": True})
            chat(d, "Something else entirely")  # the composer's text changes after the person's OK
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "tap", {"target": {"id": "post"}, "look": False}, policy=rules)
            assert e.value.code == "not_allowed" and e.value.draft is True and calls_of(d, "tap") == []
            [again] = list(d._confirms.values())
            assert again is not first and again.message["caption"] == 'Post this in Example? "Something else entirely"'
            # the person allows this one, and the same text goes
            await d.viewer_message({"type": "confirm_answer", "id": again.id, "allow": True})
            r = await d.run_tool("a", "tap", {"target": {"id": "post"}, "look": False}, policy=rules)
            assert r["text"].splitlines()[-1] == "Note: sent by a person (in the live view)."
            assert len(calls_of(d, "tap")) == 1
        finally:
            daemon_module._DRAFTS_ROUTE.reset(token)
        await finish(stop, task)
    run(go)


def test_h3_a_draft_changed_while_the_dialog_waits_is_not_sent(tmp_path):
    async def go():
        d, rules = daemon_with(tmp_path, {"version": 2, "confirm": ["Post"], "block": [], "grants": []})
        chat(d)
        stop, task = await started(d)
        token = daemon_module._DRAFTS_ROUTE.set(True)
        try:
            await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
            call = asyncio.create_task(d.run_tool("a", "tap", {"target": {"id": "post"}, "look": False}, policy=rules,
                                                  dialog=True))
            ask, _ = await ask_of(d)
            assert ask["message"] == 'Post this in Example? "Hello there"'
            chat(d, "Hello thera")  # another client changes one letter while the person reads
            d.dialog_answer("a", ask["id"], "allow")
            with pytest.raises(Refused) as e:
                await asyncio.wait_for(call, 2)
            assert e.value.code == "not_allowed" and e.value.draft is True and "draft changed" in str(e.value)
            assert calls_of(d, "tap") == []
        finally:
            daemon_module._DRAFTS_ROUTE.reset(token)
        await finish(stop, task)
    run(go)


# ---- H4, H5: what the notifications answer shows -----------------------------------------------------------------------
from test_notifications import cell, center, otp_daemon, SMS  # noqa: E402
from test_daemon_accounts import BUNDLE as CODE_APP  # noqa: E402

SECRET_NOTE = "syntheticSECRET_value_1234567890"


@pytest.mark.parametrize("shown", ["123 456", "123-456"])
def test_h4_a_split_code_is_hidden_in_the_notifications_answer(tmp_path, shown):
    async def go():
        source = center(cell("Messages", "now", "+1 555 0100", f"Your Wayfare code is {shown}."))
        d, s, said = otp_daemon(tmp_path, source=source)
        stop, task = await started(d)
        r = await d.run_tool("a", "notifications", {})
        await finish(stop, task)
        assert "Your Wayfare code is <one-time code>." in r["text"], r["text"]
        for form in ("123 456", "123-456", "123456"):
            assert form not in r["text"] and form not in d.scrub_everywhere(f"again {form}")
    run(go)


@pytest.mark.parametrize("where", ["title", "body"])
def test_h5_a_vault_value_across_the_cut_is_redacted_before_it(tmp_path, where):
    async def go():
        data = {"version": 2, "vault": {"n": {"kind": "note", "text": SECRET_NOTE}},
                "otp": {CODE_APP: {"from": [SMS], "match": "Wayfare"}}}
        pad = "pad " * (15 if where == "title" else 45)  # 60 or 180 characters: the value crosses the 80 or 200 cut
        title, body = (pad + SECRET_NOTE, "short body") if where == "title" else ("Trip", pad + SECRET_NOTE)
        source = center(cell("Messages", "now", title, body))
        d, s, said = otp_daemon(tmp_path, data=data, source=source)
        stop, task = await started(d)
        r = await d.run_tool("a", "notifications", {})
        await finish(stop, task)
        text = r["text"]
        assert "n1 Messages" in text
        for i in range(len(SECRET_NOTE) - 11):  # no 12-character piece of it (§20.3's test)
            assert SECRET_NOTE[i:i + 12] not in text, (SECRET_NOTE[i:i + 12], text)
    run(go)


# ---- M1: a hit the tree can't place is never "inside" by its frame alone -----------------------------------------------
from test_runner_v5 import ROW, ROW_TEXT, armed_daemon, hit, lab_items, looked, node  # noqa: E402

# drawn over the Terms row after the read, within its frame: SpringBoard's or the app's own, in no tree the daemon read
OVERLAY_DELETE = node("Button", (50, 772, 200, 40), n="delete", l="Delete")


@pytest.mark.parametrize("bundle", ["com.apple.springboard", "com.example.lab"])
def test_m1_an_unplaced_hit_within_the_targets_frame_is_covered(bundle):
    async def go():
        d = armed_daemon(hit(OVERLAY_DELETE, bundle))
        stop, task = await started(d)
        await looked(d, lab_items())
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "terms-row"}})
        assert e.value.code == "not_actionable" and str(e.value).startswith("covered:")
        assert "tap" not in d.session.calls and "read_source" in d.session.calls  # (the tree was read again first)
        await finish(stop, task)
    run(go)


def test_m1_a_hit_the_tree_places_inside_the_target_still_taps():
    async def go():
        for answer in (hit(ROW), hit(ROW_TEXT)):
            d = armed_daemon(answer)
            stop, task = await started(d)
            await looked(d, lab_items())
            r = await d.run_tool("a", "tap", {"target": {"id": "terms-row"}})
            assert r["text"].startswith("Tapped") and "tap" in d.session.calls
            await finish(stop, task)
    run(go)


# ---- M2: the daemon admits a scheduled run's first call itself -----------------------------------------------------------
import time  # noqa: E402

from tapwright import schedulerun, schedules  # noqa: E402
from tapwright.daemon import Scope  # noqa: E402


def test_m2_a_scheduled_runs_first_call_is_busy_while_another_clients_call_is_in_flight():
    async def go():
        d = make()
        d.session.tree = [button("e1", "OK", "ok-button")]
        stop, task = await started(d)
        gate, inside = asyncio.Event(), asyncio.Event()
        real = d.session.look

        async def held_look(*args, **kwargs):
            inside.set()
            await gate.wait()
            return await real(*args, **kwargs)
        d.session.look = held_look
        waiting = asyncio.create_task(d.run_tool("a", "look", {"screenshot": False}))
        await asyncio.wait_for(inside.wait(), 2)  # agent "a" holds a call; the scheduler's status said idle before it
        before = list(d.session.actions())
        with pytest.raises(Refused) as e:
            await asyncio.wait_for(d.run_tool("schedule-post", "tap", {"target": {"id": "ok-button"}}), 2)
        assert e.value.code == "busy" and str(e.value).startswith(schedules.ADMISSION_BUSY)
        assert d.session.actions() == before and not d.scheduled_running()
        gate.set()
        await asyncio.wait_for(waiting, 2)
        d.session.look = real
        with pytest.raises(Refused) as e:  # the agent's call ended a moment ago: still within the 90 s
            await d.run_tool("schedule-post", "look", {"screenshot": False})
        assert e.value.code == "busy"
        d._person_at = -float("inf")
        d._call_at.clear()
        r = await d.run_tool("schedule-post", "look", {"screenshot": False})  # quiet now: admitted
        assert r["text"]
        d._person_at = time.monotonic()  # a person in the live view a moment ago
        with pytest.raises(Refused) as e:
            await d.run_tool("schedule-post", "look", {"screenshot": False})
        assert e.value.code == "busy"
        await finish(stop, task)
    run(go)


class AdmissionPhone:
    """The scheduler's client: idle status, and a drive the daemon refuses `busy` (not admitted) `refusals` times."""

    class DaemonError(RuntimeError):
        def __init__(self, code, message, draft=False):
            super().__init__(message)
            self.code, self.draft = code, draft

    def __init__(self, refusals, message=None):
        self.refusals, self.calls = refusals, []
        self.message = message or schedules.ADMISSION_BUSY

    def status(self):
        return {"state": "live", "driver": None, "paused": False, "in_use": False}

    def set_scope(self, apps, dedicated=False):
        pass

    set_policy = set_accounts = set_maps = lambda self, x=None: None

    def call(self, tool, args, client_id, timeout=120.0, dialog=False):
        self.calls.append(tool)
        if self.refusals:
            self.refusals -= 1
            raise self.DaemonError("busy", self.message)
        return {"text": "Arrived."}


@pytest.fixture
def sched_home(tmp_path, monkeypatch):
    from tapwright import store
    monkeypatch.setattr(store, "HOME", tmp_path / "home")
    routes = tmp_path / "home" / "maps" / "com.example.wayfare" / "routes"
    routes.mkdir(parents=True)
    (routes / "post_update.json").write_text("{}", encoding="utf-8")
    return tmp_path / "home"


def admission_deps(tmp_path, phone):
    from test_review_17_safety import Clock, T1
    from tapwright import pidlock
    where = tmp_path / "sched"
    schedules.write(schedules.Schedule("post", "post_update", {}, "09:00 daily", None), where)
    clock = Clock()
    return schedulerun.Deps(client=phone, sleep=clock.sleep, monotonic=clock.monotonic, where=where, runs=tmp_path / "runs",
                            audit_folder=tmp_path / "audit", unlock_phone=None,
                            lock=pidlock.Deps(pid=4242, alive=lambda p: True, start_of=lambda p: T1)), clock


def test_m2_the_scheduler_waits_and_asks_again_when_the_daemon_doesnt_admit_it(tmp_path, sched_home):
    from tapwright import auditlog
    phone = AdmissionPhone(2)
    deps, clock = admission_deps(tmp_path, phone)
    out = schedulerun.run("post", deps=deps)
    assert out.status == "done" and phone.calls == ["drive"] * 3 and 0 < clock.t < deps.wait
    lines = [json.loads(x) for x in auditlog.read_lines(tmp_path / "audit")]
    assert [x["detail"] for x in lines] == ["started", "done"]  # one start, once it was admitted
    # never admitted within the 10 minutes: skipped as in use
    phone = AdmissionPhone(10 ** 6)
    (tmp_path / "b").mkdir()
    deps, clock = admission_deps(tmp_path / "b", phone)
    out = schedulerun.run("post", deps=deps)
    assert (out.status, out.result) == ("skipped", schedulerun.IN_USE) and clock.t >= deps.wait
    # any other busy (a scheduled run's own, mid-route) still ends the run
    phone = AdmissionPhone(1, "a scheduled run is using the phone; try again when it ends")
    (tmp_path / "c").mkdir()
    deps, _ = admission_deps(tmp_path / "c", phone)
    assert schedulerun.run("post", deps=deps).status == "failed" and phone.calls == ["drive"]


# ---- M5: a scheduled run opens its route's app first ---------------------------------------------------------------------
from mapfakes import MapSession, screen  # noqa: E402
from test_daemon_routes import GOES, explore, record_sign_in, screens  # noqa: E402

from tapwright import mapkeeper  # noqa: E402

OTHER = "com.example.notes"
SAFARI = "com.apple.mobilesafari"


class TwoApps(MapSession):
    """The routes' app (com.example.app), a second one and Safari; opening one shows its first screen."""
    FIRST = {APP: "welcome", OTHER: "b_start", SAFARI: "web"}

    async def launch(self, bundle_id, arguments=None):
        text = await super().launch(bundle_id, arguments)
        self.at = self.FIRST.get(bundle_id, self.at)
        return text


def two_apps(tmp_path):
    shown = {**screens(), "b_start": screen("Start B", ids=("go-b", "b1", "b2"), bundle=OTHER),
             "b_end": screen("End B", ids=("b3", "b4", "b5"), bundle=OTHER),
             "web": screen("Web", ids=("w9", "w8", "w7"), bundle=SAFARI)}
    s = TwoApps(shown, "welcome", {**GOES, ("b_start", "go-b"): "b_end"})
    s.installed = [*s.installed, {"bundle_id": OTHER, "name": "Notes B", "version": "1", "build": "1",
                                  "executable": "NotesB"}]
    s.app_names = {APP: "Example", OTHER: "Notes B", SAFARI: "Safari"}
    d = make(s)
    d.maps = mapkeeper.Maps(str(tmp_path / "maps"))
    return d, s


def in_front(s, bundle):
    s.foreground, s.app, s.at = bundle, (s.app_names.get(bundle, bundle), bundle), TwoApps.FIRST[bundle]


def test_m5_scheduled_runs_open_their_routes_app_from_another_app_and_two_apps_in_a_row(tmp_path):
    async def go():
        d, s = two_apps(tmp_path)
        stop, task = await started(d)
        await explore(d)  # app A's places and its sign_in route
        await record_sign_in(d)
        in_front(s, OTHER)  # app B's places and its route
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "map", {"name": "b_start"})
        await d.run_tool("a", "record", {"start": True, "route": "b_route"})
        await d.run_tool("a", "tap", {"target": {"id": "go-b"}})
        await d.run_tool("a", "map", {"name": "b_end"})
        await d.run_tool("a", "record", {"stop": True})
        await d.maps.flush()
        d._call_at.clear()  # agent "a" has been quiet for a while
        in_front(s, SAFARI)  # Safari is in front when A's schedule falls due
        r = await d.run_tool("schedule-a", "drive", {"route": "sign_in", "with": {"email": "a@b.c"}},
                             scope=Scope((APP,)))
        assert r["text"].startswith("Arrived at home by route sign_in") and s.launched[-1] == APP
        # B's schedule right after, with A's home screen in front
        r = await d.run_tool("schedule-b", "drive", {"route": "b_route"}, scope=Scope((OTHER,)))
        assert r["text"].startswith("Arrived at b_end by route b_route") and s.launched[-1] == OTHER
        # an agent's own drive still drives the app in front, and opens nothing
        launched = len(s.launched)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "drive", {"route": "sign_in", "with": {"email": "a@b.c"}}, scope=Scope((APP,)))
        assert e.value.code == "not_allowed" and len(s.launched) == launched
        await finish(stop, task)
    run(go)


# ---- M3, M4 and one turn for /otp ----------------------------------------------------------------------------------------
from starlette.testclient import TestClient  # noqa: E402
from test_notifications import CHAT, CODE, INSTALLED, handover_daemon  # noqa: E402
from test_otp import AUTH, A, prove  # noqa: E402

from tapwright.ui import Element  # noqa: E402


@pytest.mark.parametrize("field", [
    Element("e1", "input", "Message", "message", value="please send me the verification code", type="TextView"),
    Element("e1", "input", "Search", "search", value="code", type="SearchField"),
    Element("e1", "input", "Search", "search", value="code", type="TextField"),
])
def test_m3_otp_needs_a_real_code_field_not_a_composers_typed_words(tmp_path, field):
    app, d, s = handover_daemon(tmp_path)
    s.tree = [field]
    c = TestClient(app, client=A)
    prove(c)
    assert c.post("/otp", json={"app": CODE_APP}, headers=AUTH).status_code == 404 and s.center_reads == 0
    assert d.code_fields.shown_at(CODE_APP) is None  # nor does a read note one


def test_m3_a_code_field_noted_earlier_counts_only_while_the_screen_shows_one_now(tmp_path):
    app, d, s = handover_daemon(tmp_path)
    d.code_fields.seen(CODE_APP, True)  # a read a moment ago saw one
    s.tree = [Element("e1", "input", "Message", "message", value="the code is?", type="TextView")]
    s.on_tree = None  # (the read now doesn't clear it: the handover itself must look)
    c = TestClient(app, client=A)
    prove(c)
    assert c.post("/otp", json={"app": CODE_APP}, headers=AUTH).status_code == 404 and s.center_reads == 0


def test_m4_a_code_handed_over_isnt_handed_over_again_after_a_restart(tmp_path):
    app, d, s = handover_daemon(tmp_path)
    c = TestClient(app, client=A)
    prove(c)
    got = c.post("/otp", json={"app": CODE_APP}, headers=AUTH)
    assert got.status_code == 200 and got.json()["code"] == CODE
    # the daemon restarts; the same notification is still in Notification Center, the same field shows
    app, d2, s2 = handover_daemon(tmp_path)
    assert d2 is not d
    c = TestClient(app, client=A)
    prove(c)
    assert c.post("/otp", json={"app": CODE_APP}, headers=AUTH).status_code == 404
    from tapwright import otp
    marks = [p for p in otp.used_folder().iterdir() if p.name != "key"]
    assert len(marks) == 1 and CODE not in marks[0].name  # a keyed digest names it, never the code
    assert all(CODE not in p.read_text("utf-8", "replace") for p in otp.used_folder().iterdir())


def test_cr_otp_checks_the_app_and_reads_the_code_in_one_turn(tmp_path):
    """CodeRabbit, PR #12 (center_tool.py:267): a person who switches apps between the check and the read of the code."""
    app, d, s = handover_daemon(tmp_path)
    real_agent, real_read = d.control.agent, d._read_center
    turns, front_at_read = [], []

    async def agent(fn):
        try:
            return await real_agent(fn)
        finally:
            turns.append(1)
            if len(turns) == 1:  # a person's input, queued during the first turn, runs right after it: another app
                s.foreground, s.tree_app = CHAT, (INSTALLED[CHAT], CHAT)

    async def read_center(*args, **kwargs):
        front_at_read.append(s.foreground)
        return await real_read(*args, **kwargs)
    d.control.agent, d._read_center = agent, read_center
    c = TestClient(app, client=A)
    prove(c)
    got = c.post("/otp", json={"app": CODE_APP}, headers=AUTH)
    assert front_at_read in ([], [CODE_APP]), front_at_read  # never read while another app is in front
    assert got.status_code in (200, 404) and (got.status_code == 404) == (front_at_read == [])

"""Codex's review of PR #12, cycle 2 (H1-H7, M1-M6): each test failed at 97c0541 and passes after its fix. No phone,
synthetic values only."""
import asyncio
import datetime
import json
import os
import threading

import pytest
from fakes import button, el
from test_daemon import finish, make, run, started
from test_review_17_c1 import APP, ask_of, calls_of, chat, daemon_with, grant, uses

from tapwright import daemon as daemon_module
from tapwright import pidlock, policy, ui
from tapwright.ceiling import Ceiling
from tapwright.control import Refused


# ---- H1: the draft shown and its fingerprint come from one read, and where it goes is part of it ----------------------
def test_h1_the_draft_shown_is_the_draft_the_ok_is_for_even_when_the_app_changes_it_right_after_the_read(tmp_path):
    async def go():
        d, rules = daemon_with(tmp_path, {"version": 2, "confirm": ["Post"], "block": [], "grants": []})
        chat(d, "Approved draft A")
        stop, task = await started(d)
        token = daemon_module._DRAFTS_ROUTE.set(True)
        try:
            await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
            real = d.session.snapshot

            async def snapshot_then_changed(*args, **kwargs):
                got = await real(*args, **kwargs)
                chat(d, "Unreviewed draft B")  # the app changes the composer right after the read the person is shown
                d.session.snapshot = real
                return got
            d.session.snapshot = snapshot_then_changed
            with pytest.raises(Refused):
                await d.run_tool("a", "tap", {"target": {"id": "post"}, "look": False}, policy=rules)
            [first] = list(d._confirms.values())
            assert first.message["caption"] == 'Post this in Example? "Approved draft A"'
            await d.viewer_message({"type": "confirm_answer", "id": first.id, "allow": True})
            with pytest.raises(Refused) as e:  # the OK was for A; B is on screen: asked again, nothing sent
                await d.run_tool("a", "tap", {"target": {"id": "post"}, "look": False}, policy=rules)
            assert e.value.draft is True and calls_of(d, "tap") == []
            [again] = list(d._confirms.values())
            assert again.message["caption"] == 'Post this in Example? "Unreviewed draft B"'
        finally:
            daemon_module._DRAFTS_ROUTE.reset(token)
        await finish(stop, task)
    run(go)


def test_h1_a_conversation_that_changed_hands_is_asked_again_though_the_composer_is_the_same(tmp_path):
    async def go():
        d, rules = daemon_with(tmp_path, {"version": 2, "confirm": ["Post"], "block": [], "grants": []})

        def conversation(who):
            chat(d, "See you at six")
            d.session.tree = [el("e0", "header", who, rect=(10, 60, 300, 30)), *d.session.tree]
        conversation("Alice")
        stop, task = await started(d)
        token = daemon_module._DRAFTS_ROUTE.set(True)
        try:
            await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
            with pytest.raises(Refused):
                await d.run_tool("a", "tap", {"target": {"id": "post"}, "look": False}, policy=rules)
            [first] = list(d._confirms.values())
            await d.viewer_message({"type": "confirm_answer", "id": first.id, "allow": True})
            conversation("Bob")  # Bob's chat now: the same words, the same Post
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "tap", {"target": {"id": "post"}, "look": False}, policy=rules)
            assert e.value.draft is True and calls_of(d, "tap") == []
            [again] = list(d._confirms.values())
            assert again is not first
        finally:
            daemon_module._DRAFTS_ROUTE.reset(token)
        await finish(stop, task)
    run(go)


# ---- H2: a route the daemon's grants name is protected whatever policy the call sends --------------------------------
def test_h2_a_stricter_call_policy_never_frees_a_route_the_daemons_grant_names(tmp_path):
    from mapfakes import APP as MAP_APP
    from test_daemon_routes import explore, mapped

    async def go():
        today = datetime.date.today()
        path = tmp_path / "policy.json"
        path.write_text(json.dumps({"version": 2, "confirm": ["Post"], "block": [], "grants": [
            {"name": "g", "app": MAP_APP[1], "words": ["Post"], "route": "sign_in", "perDay": 1,
             "until": (today + datetime.timedelta(days=5)).isoformat()}]}), encoding="utf-8")
        rules = policy.load(path)
        ceiling = Ceiling(policy=rules)
        stricter, why = ceiling.check_policy(policy.Policy(confirm=("Post", "Extra confirmation")))
        assert why is None and stricter is not None and stricter.file is None  # (accepted: it only adds a word)
        d = mapped(tmp_path / "maps")
        d.ceiling = ceiling
        stop, task = await started(d)
        await explore(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "record", {"start": True, "route": "sign_in"}, policy=stricter)
        assert e.value.code == "not_allowed" and 'grant "g" names it' in str(e.value)
        r = await d.run_tool("a", "record", {"start": True, "route": "sign_in_2"}, policy=stricter)  # another name: fine
        assert "sign_in_2" in r["text"]
        await finish(stop, task)
    run(go)


# ---- H3: a line break in drafts-only typing is Return, refused as submit is ------------------------------------------
@pytest.mark.parametrize("text", ["Unapproved post\n", "Unapproved\rpost", "Unapproved post ", "Unapproved "])
def test_h3_a_line_break_in_the_text_is_refused_where_submit_is(tmp_path, text):
    async def go():
        d, rules = daemon_with(tmp_path, {"version": 2, "confirm": ["Post"], "block": [], "grants": []})
        chat(d, "")
        stop, task = await started(d)
        token = daemon_module._DRAFTS_ROUTE.set(True)
        try:
            await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "type_text", {"text": text, "target": {"id": "message"}, "submit": False},
                                 policy=rules)
            assert e.value.code == "not_allowed" and "line break" in str(e.value)
            assert d.session.typed == [] and calls_of(d, "tap") == []
        finally:
            daemon_module._DRAFTS_ROUTE.reset(token)
        # outside drafts-only, a line break types as before
        await d.run_tool("a", "type_text", {"text": text, "target": {"id": "message"}}, policy=rules)
        assert [t[0] for t in d.session.typed] == [text]
        await finish(stop, task)
    run(go)


def test_h3_a_vault_value_with_a_line_break_is_refused_without_showing_it(tmp_path):
    from test_daemon_vault import vaulted
    secret = "Synthetic first line\nSynthetic second line"

    async def go():
        d, _ = vaulted(tmp_path, {"version": 2, "vault": {"n": {"kind": "note", "text": secret}}})
        stop, task = await started(d)
        token = daemon_module._DRAFTS_ROUTE.set(True)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "type_text", {"target": {"id": "email"}, "text": "${vault.n.text}"})
            assert e.value.code == "not_allowed" and "line break" in str(e.value) and "n" in str(e.value)
            assert "Synthetic" not in str(e.value) and d.session.typed == []
        finally:
            daemon_module._DRAFTS_ROUTE.reset(token)
        await finish(stop, task)
    run(go)


# ---- H4: a generic Confirm on an account-deletion screen or alert asks a person ----------------------------------------
def test_h4_a_bare_confirm_on_an_account_deletion_screen_asks_a_person(tmp_path):
    async def go():
        d, rules = daemon_with(tmp_path, {"version": 2, "confirm": ["Delete", "Post", "Send"], "block": [],
                                          "grants": []})
        d.session.tree = [el("e1", "header", "Delete your account?", rect=(10, 60, 300, 30)),
                          button("e2", "Confirm", "confirm", rect=(10, 300, 300, 44)),
                          button("e3", "Cancel", "cancel", rect=(10, 360, 300, 44))]
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "confirm"}}, policy=rules)
        assert e.value.code == "not_allowed" and "needs_confirmation" in str(e.value) and calls_of(d, "tap") == []
        # with no policy file at all (--no-policy) too
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "confirm"}}, policy=policy.Policy())
        assert "needs_confirmation" in str(e.value) and calls_of(d, "tap") == []
        r = await d.run_tool("a", "tap", {"target": {"id": "cancel"}}, policy=rules)  # leaving asks nobody
        assert r["text"].startswith("Tapped") and len(calls_of(d, "tap")) == 1
        await finish(stop, task)
    run(go)


def test_h4_accepting_an_account_deletion_alert_whose_button_says_confirm_asks_a_person(tmp_path):
    async def go():
        d, rules = daemon_with(tmp_path, {"version": 2, "confirm": ["Delete", "Post", "Send"], "block": [],
                                          "grants": [grant(words=["Confirm"])]})
        d.session.tree = [button("e1", "OK", "ok")]
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
        d.session.last_alert = ui.Alert("Delete your account?", ("Cancel", "Confirm"), (60.0, 300.0, 300.0, 200.0))
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "alert", {"action": "accept"}, policy=rules)
        assert e.value.code == "not_allowed" and "needs_confirmation" in str(e.value)
        assert calls_of(d, "alert") == [] and uses(tmp_path) == []
        await finish(stop, task)
    run(go)


# ---- H5: a payment screen's Send is never covered by a Send grant ----------------------------------------------------
def test_h5_a_send_grant_never_covers_send_money(tmp_path):
    async def go():
        d, rules = daemon_with(tmp_path, {"version": 2, "confirm": ["Send"], "block": [],
                                          "grants": [grant(words=["Send"])]})
        d.session.tree = [el("e1", "header", "Send money", rect=(10, 60, 300, 30)),
                          el("e2", "text", "Transfer $25 to Alice", rect=(10, 100, 300, 30)),
                          button("e3", "Send", "send", rect=(10, 300, 300, 44))]
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "send"}}, policy=rules)
        assert "needs_confirmation" in str(e.value) and calls_of(d, "tap") == [] and uses(tmp_path) == []
        # an amount alone on the screen ("$25.00") with a Send: money sent, too
        d.session.tree = [el("e1", "header", "Alice", rect=(10, 60, 300, 30)),
                          el("e2", "text", "$25.00", rect=(10, 100, 300, 30)),
                          button("e3", "Send", "send", rect=(10, 300, 300, 44))]
        await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "send"}}, policy=rules)
        assert "needs_confirmation" in str(e.value) and calls_of(d, "tap") == [] and uses(tmp_path) == []
        # a price among other words is no payment: the grant covers a chat's Send
        d.session.tree = [el("e1", "header", "Alice", rect=(10, 60, 300, 30)),
                          el("e2", "text", "The latte was $4, see you there", rect=(10, 100, 300, 30)),
                          button("e3", "Send", "send", rect=(10, 300, 300, 44))]
        await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
        r = await d.run_tool("a", "tap", {"target": {"id": "send"}}, policy=rules)
        assert 'allowed by grant "wayfare_posts"' in r["text"] and len(calls_of(d, "tap")) == 1
        await finish(stop, task)
    run(go)


# ---- H6: split codes in any decimal digits and separators are redacted as shown -----------------------------------------
@pytest.mark.parametrize("shown", ["١٢٣ ٤٥٦", "١٢٣-٤٥٦",
                                   "123 456", "123 456", "123‑456", "１２３ ４５６"])
def test_h6_a_split_code_in_any_digits_is_hidden(tmp_path, shown):
    from test_notifications import SMS, cell, center, otp_daemon

    async def go():
        source = center(cell("Messages", "now", "+1 555 0100", f"Your Wayfare code is {shown}."))
        d, s, said = otp_daemon(tmp_path, source=source)
        stop, task = await started(d)
        r = await d.run_tool("a", "notifications", {})
        await finish(stop, task)
        assert "Your Wayfare code is <one-time code>." in r["text"], r["text"]
        assert shown not in r["text"] and "123456" not in d.scrub_everywhere("again 123456")
        assert SMS
    run(go)


# ---- H7: a scheduled run's audit line goes through the run's vault redactor ----------------------------------------------
def test_h7_a_failed_runs_audit_line_never_holds_a_vault_value(tmp_path, monkeypatch):
    from test_review_17_c1 import AdmissionPhone, admission_deps
    from tapwright import auditlog, schedulerun, schedules, store
    monkeypatch.setattr(store, "HOME", tmp_path / "home")
    routes = tmp_path / "home" / "maps" / "com.example.wayfare" / "routes"
    routes.mkdir(parents=True)
    (routes / "post_update.json").write_text("{}", encoding="utf-8")
    email = "synthetic.secret@example.test"
    vault = tmp_path / "vault.json"
    vault.write_text(json.dumps({"version": 2, "vault": {"me": {"kind": "identity", "name": "Tess Tester",
                                                               "email": email}}}), encoding="utf-8")
    phone = AdmissionPhone(0)
    deps, _ = admission_deps(tmp_path, phone)
    schedules.write(schedules.Schedule("post", "post_update", {"caption": f"@{tmp_path / email}.txt"}, "09:00 daily",
                                       None, accounts=str(vault)), deps.where)
    out = schedulerun.run("post", deps=deps)
    assert out.status == "failed" and phone.calls == []
    written = "\n".join(auditlog.read_lines(tmp_path / "audit"))
    assert "can't read" in written and email not in written and "synthetic.secret" not in written, written
    assert email not in out.text and email not in (tmp_path / "runs").joinpath(os.path.basename(out.report)).read_text(
        "utf-8")


# ---- M1: a control that kept its id and frame but changed its words is not "the same" ------------------------------------
def test_m1_a_hit_with_the_targets_id_and_frame_but_other_words_is_stale():
    from test_runner_v5 import ROW, armed_daemon, hit, lab_items, looked

    async def go():
        d = armed_daemon(hit({**ROW, "l": "Delete account"}))
        stop, task = await started(d)
        await looked(d, lab_items())
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "terms-row"}})
        assert e.value.code == "stale" and "tap" not in d.session.calls
        await finish(stop, task)
    run(go)


# ---- M2: another app's words never go into a refusal --------------------------------------------------------------------
def test_m2_a_banner_of_another_app_over_the_target_is_named_without_its_words():
    from test_runner_v5 import armed_daemon, hit, lab_items, looked, node

    async def go():
        banner = node("Button", (8, 740, 414, 90), n="NotificationCell-private-thread-77",
                      l="Private message from another app: Synthetic diagnosis")
        d = armed_daemon(hit(banner, "com.apple.springboard"))
        stop, task = await started(d)
        await looked(d, lab_items())
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "terms-row"}})
        said = str(e.value)
        assert e.value.code == "not_actionable" and said.startswith("covered:") and "another app" in said
        assert "Synthetic diagnosis" not in said and "Private message" not in said and "private-thread" not in said
        assert "tap" not in d.session.calls
        await finish(stop, task)
    run(go)


# ---- M3: a stale takeover gate is never moved away from a live taker ----------------------------------------------------
def test_m3_unlock_and_two_takers_never_leave_two_holders(tmp_path, monkeypatch):
    lock = tmp_path / "x.lock"
    gate = tmp_path / "x.lock.takeover"
    t1 = 1_763_013_612_481
    lock.write_text(json.dumps({"owner": "dead-main", "pid": 1, "started": t1}) + "\n", encoding="utf-8")
    gate.write_text(json.dumps({"owner": "dead-gate", "pid": 2, "started": t1}) + "\n", encoding="utf-8")

    def deps(pid):
        return pidlock.Deps(pid=pid, alive=lambda p: p not in (1, 2), start_of=lambda p: t1)
    a_snapped, a_go, b_validated, b_go = (threading.Event() for _ in range(4))
    tokens: dict[str, "str | None"] = {}
    real_snapshot, real_remove = pidlock._snapshot, pidlock._remove
    real_rename, real_unlink = os.rename, os.unlink
    seen_by_a = [0]

    def snapshot(path, d):
        got = real_snapshot(path, d)
        if threading.current_thread().name == "A" and str(path) == str(gate) and not a_snapped.is_set():
            seen_by_a[0] += 1
            a_snapped.set()  # A has judged both files (dead, dead)
            a_go.wait(5)
        return got

    def remove(path):
        if threading.current_thread().name == "B" and str(path) == str(lock) and not b_validated.is_set():
            b_validated.set()  # B holds the gate and found the main lock still dead
            b_go.wait(5)
        return real_remove(path)

    def c_in_the_gap():
        if "C" not in tokens:
            tokens["C"] = pidlock.take(lock, deps(4))

    def rename(src, dst):
        out = real_rename(src, dst)
        if threading.current_thread().name == "A" and str(src) == str(gate):
            c_in_the_gap()  # while A has B's gate out of its place, taker C comes
        return out

    def unlink(path):
        out = real_unlink(path)
        if threading.current_thread().name == "A" and str(path) == str(gate):
            c_in_the_gap()
        return out
    monkeypatch.setattr(pidlock, "_snapshot", snapshot)
    monkeypatch.setattr(pidlock, "_remove", remove)
    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(os, "unlink", unlink)
    a = threading.Thread(target=lambda: tokens.__setitem__("A", pidlock.unlock(lock, deps(5)).status), name="A")
    b = threading.Thread(target=lambda: tokens.__setitem__("B", pidlock.take(lock, deps(3))), name="B")
    a.start()
    assert a_snapped.wait(5)
    b.start()
    assert b_validated.wait(5)
    a_go.set()
    a.join(5)
    c_in_the_gap()  # (if A never touched B's gate, C comes now)
    b_go.set()
    b.join(5)
    holders = [k for k in ("B", "C") if tokens.get(k)]
    assert len(holders) == 1, tokens  # one holder, never two
    owner = json.loads(lock.read_text(encoding="utf-8"))["owner"]
    assert owner == tokens[holders[0]]


# ---- M4: a trigger's check decides again inside its turn ------------------------------------------------------------------
def test_m4_a_trigger_check_never_reads_after_an_agents_call_slipped_in(tmp_path):
    from test_notifications import BUNDLE, center_daemon

    async def go():
        d, s = center_daemon(tmp_path, [])
        s.tree = [button("e1", "OK", "ok")]
        stop, task = await started(d)
        gate, inside = asyncio.Event(), asyncio.Event()
        real = d.installed_names

        async def held_names():
            inside.set()
            await gate.wait()
            return await real()
        d.installed_names = held_names
        d._call_at.clear()
        check = asyncio.create_task(d.notification_keys(BUNDLE))
        await asyncio.wait_for(inside.wait(), 2)  # the check passed its first test and waits on the phone's app list
        await d.run_tool("a", "look", {"screenshot": False})  # an agent's call now
        assert d.phone_in_use()
        gate.set()
        with pytest.raises(Refused) as e:
            await asyncio.wait_for(check, 2)
        assert e.value.code == "busy" and s.center_reads == 0 and not s.center_open
        await finish(stop, task)
    run(go)


# ---- M5: /otp is within serve's --apps ----------------------------------------------------------------------------------
def test_m5_otp_refuses_an_app_outside_serves_apps(tmp_path):
    from starlette.testclient import TestClient
    from test_notifications import handover_daemon
    from test_otp import AUTH, TOKEN, A, prove
    from test_daemon_accounts import BUNDLE as CODE_APP
    from tapwright.server import create_app
    _, d, s = handover_daemon(tmp_path)
    d.ceiling = Ceiling(apps=("com.example.unrelated",), accounts=d.ceiling.accounts)
    c = TestClient(create_app(d, TOKEN, hosts={"testserver"}, ceiling=d.ceiling), client=A)
    prove(c)
    got = c.post("/otp", json={"app": CODE_APP}, headers=AUTH)
    assert got.status_code == 403 and "code" not in got.json() and s.center_reads == 0
    # no scope at all (no --apps, --dedicated or --phone): none either
    d.ceiling = Ceiling(accounts=d.ceiling.accounts)
    c = TestClient(create_app(d, TOKEN, hosts={"testserver"}, ceiling=d.ceiling), client=A)
    prove(c)
    assert c.post("/otp", json={"app": CODE_APP}, headers=AUTH).status_code == 403 and s.center_reads == 0
    # within --apps: handed over
    d.ceiling = Ceiling(apps=(CODE_APP,), accounts=d.ceiling.accounts)
    c = TestClient(create_app(d, TOKEN, hosts={"testserver"}, ceiling=d.ceiling), client=A)
    prove(c)
    assert c.post("/otp", json={"app": CODE_APP}, headers=AUTH).status_code == 200


# ---- M6: a close that failed is never answered as a closed read ------------------------------------------------------------
def test_m6_a_failed_close_restores_the_app_or_says_notification_center_may_be_open(tmp_path):
    from test_notifications import BUNDLE, center_daemon
    from tapwright.device import DeviceError

    async def go():
        d, s = center_daemon(tmp_path, [])
        stop, task = await started(d)
        real = s.swipe

        async def swipe(x0, y0, x1, y1, *args, **kwargs):
            if y1 < y0:  # the swipe up that closes it fails, once the gesture is sent
                raise DeviceError("HTTP 500: synthetic")
            return await real(x0, y0, x1, y1, *args, **kwargs)
        s.swipe = swipe
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "notifications", {})
        assert e.value.code == "internal" and "may still be on the phone's screen" in str(e.value)
        assert s.launched[-1:] == [BUNDLE]  # the app that was in front was brought back, tried anyway
        await finish(stop, task)
    run(go)

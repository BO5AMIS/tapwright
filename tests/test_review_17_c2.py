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




# ---- H2: a route the daemon's grants name is protected whatever policy the call sends --------------------------------


# ---- H3: a line break in drafts-only typing is Return, refused as submit is ------------------------------------------




# ---- H4: a generic Confirm on an account-deletion screen or alert asks a person ----------------------------------------




# ---- H5: a payment screen's Send is never covered by a Send grant ----------------------------------------------------


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


# ---- M2: another app's words never go into a refusal --------------------------------------------------------------------


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

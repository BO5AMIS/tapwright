"""The passcode unlock in the daemon (AGENT-TOOLS §18.6, §11.5, §20.3, §23; Tapwright 1.7): a locked phone and a vault
with `phone.passcode` is unlocked by the tool itself before an action; never on the XCTest prompt, never while paused,
and the passcode is in no output: answers, errors, the log, the audit log, the action log, the live view's messages,
the trace. All values are synthetic."""
import asyncio
import json

import pytest
from fakes import FakeSession
from test_daemon import finish, make, run, started
from test_unlock import PASSCODE, Screen

from tapwright import trace, unlock
from tapwright.actionlog import ActionLog
from tapwright.auditlog import AuditLog
from tapwright.control import Refused
from tapwright.device import PASSCODE_WORDS

FORMS = (PASSCODE, " ".join(PASSCODE), json.dumps(PASSCODE)[1:-1], PASSCODE[:4], PASSCODE[2:])


class SessionScreen(Screen):
    """The fake lock screen tied to the fake session: unlocking it unlocks the session."""

    def __init__(self, session, **kw):
        super().__init__(**kw)
        self.session = session

    async def locked(self):
        return self.session.is_locked

    async def press_keys(self, points):
        self.is_locked = self.session.is_locked
        await super().press_keys(points)
        self.session.is_locked = self.is_locked


def vault(tmp_path, passcode=PASSCODE) -> str:
    path = tmp_path / "vault.json"
    entries = {"tester2": {"kind": "account", "username": "tester2@example.com", "password": "hunter2-PW-9x"}}
    if passcode is not None:
        entries["phone"] = {"kind": "phone", "passcode": passcode}
    path.write_text(json.dumps({"version": 2, "vault": entries}), encoding="utf-8")
    return str(path)


def locked_daemon(tmp_path, right=PASSCODE, said=None):
    s = FakeSession()
    s.is_locked = True
    audit = AuditLog(tmp_path / "audit")
    d = make(s, audit=audit, actions=ActionLog(tmp_path / "log"), log=(said.append if said is not None else lambda *a: None))
    d.lock_screen = SessionScreen(s, right=right)
    return d, s


def everything(tmp_path, said, d, answers) -> str:
    """Every output this test can reach, joined: answers and errors, the daemon's log, the audit and action logs, the
    live view's messages and the trace file."""
    files = [p.read_text("utf-8") for p in tmp_path.rglob("*") if p.is_file() and p.name != "vault.json"]
    return "\n".join([*answers, *map(str, said), *files])


def test_a_locked_phone_is_unlocked_with_the_vaults_passcode_and_the_tap_goes_on(tmp_path):
    async def go():
        said: list = []
        d, s = locked_daemon(tmp_path, said=said)
        accounts = vault(tmp_path)
        trace.enable(tmp_path / "trace.jsonl")
        queue = d.subscribe()
        try:
            stop, task = await started(d, until=("locked",))
            r = await d.run_tool("a", "tap", {"target": {"id": "ok-button"}}, accounts=accounts)
            assert r["text"].startswith("Tapped e1") and d.state == "live" and not s.is_locked
            assert d.lock_screen.pressed == [PASSCODE] and "tap" in s.actions()
            await finish(stop, task)
        finally:
            trace._path = None
        lines = [json.loads(x) for p in (tmp_path / "audit").glob("*.jsonl") for x in p.read_text("utf-8").splitlines()]
        assert [(a["kind"], a["name"], a["detail"]) for a in lines] == [
            ("vault", "phone.passcode", "typed on the lock screen; unlocked")]
        views = []
        while not queue.empty():
            views.append(json.dumps(queue.get_nowait()))
        out = everything(tmp_path, said, d, [r["text"], *views])
        assert "passcode unlock: unlocked the phone" in out
        for form in FORMS:
            assert form not in out, form
    run(go)


def test_a_refused_passcode_answers_locked_and_is_not_typed_again(tmp_path):
    async def go():
        said: list = []
        d, s = locked_daemon(tmp_path, right="000000", said=said)
        accounts = vault(tmp_path)
        stop, task = await started(d, until=("locked",))
        errors = []
        for _ in range(3):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "tap", {"target": {"id": "ok-button"}}, accounts=accounts)
            assert e.value.code == "locked" and str(e.value).startswith(unlock.REFUSED)
            errors.append(str(e.value))
        assert d.lock_screen.pressed == [PASSCODE] and "tap" not in s.actions()  # one attempt, no touch of the app
        await finish(stop, task)
        out = everything(tmp_path, said, d, errors)
        for form in FORMS:
            assert form not in out, form
    run(go)


def test_the_xctest_prompt_is_never_answered_with_the_passcode(tmp_path):
    async def go():
        d, s = locked_daemon(tmp_path)
        stop, task = await started(d, until=("locked",))
        d._set_state("locked", f"the phone is {PASSCODE_WORDS} to allow UI automation")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "ok-button"}}, accounts=vault(tmp_path))
        assert e.value.code == "locked"
        assert d.lock_screen.pressed == [] and d.lock_screen.calls == []
        await finish(stop, task)
    run(go)


def test_without_a_passcode_a_locked_phone_answers_as_before(tmp_path):
    async def go():
        d, s = locked_daemon(tmp_path)
        stop, task = await started(d, until=("locked",))
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "ok-button"}}, accounts=vault(tmp_path, passcode=None))
        assert e.value.code == "locked" and str(e.value) == "the phone is locked; ask the user to unlock it"
        assert d.lock_screen.pressed == [] and d.lock_screen.calls == []
        await finish(stop, task)
    run(go)


def test_a_look_reads_the_lock_screen_without_unlocking(tmp_path):
    async def go():
        d, s = locked_daemon(tmp_path)
        stop, task = await started(d, until=("locked",))
        await d.run_tool("a", "look", {"screenshot": False}, accounts=vault(tmp_path))
        assert d.lock_screen.pressed == [] and s.is_locked
        await finish(stop, task)
    run(go)


def test_never_while_paused_or_while_a_person_drives(tmp_path):
    async def go():
        d, s = locked_daemon(tmp_path)
        accounts = vault(tmp_path)
        stop, task = await started(d, until=("locked",))
        d.control.set_paused(True)
        r = await d.unlock_phone(accounts)
        assert r.code == "paused" and d.lock_screen.pressed == []
        d.control.set_paused(False)
        d.control._person_at = d.control.clock()
        r = await d.unlock_phone(accounts)
        assert r.code == "busy" and d.lock_screen.pressed == []
        await finish(stop, task)
    run(go)


def test_the_schedules_unlock_endpoint_answers_fixed_words(tmp_path):
    from starlette.testclient import TestClient

    from tapwright.server import create_app

    class Fake:
        def __init__(self):
            self.asked = []

        async def unlock_phone(self, accounts=None):
            self.asked.append(accounts)
            return unlock.Result(False, "refused", unlock.REFUSED)

        def status(self):
            return {"protocol": 1}
    fake = Fake()
    token = "t" * 43
    app = create_app(fake, token, hosts={"testserver"})
    path = vault(tmp_path)
    with TestClient(app) as c:
        r = c.post("/api/phone/unlock", headers={"Authorization": f"Bearer {token}", "X-Tapwright-Accounts": path}, json={})
        assert r.status_code == 200 and r.json() == {"ok": False, "code": "refused", "message": unlock.REFUSED}
        assert c.post("/api/phone/unlock", json={}).status_code == 401  # the token, as every /api route
        assert c.post("/api/phone/unlock", headers={"Authorization": f"Bearer {token}",
                                                    "X-Tapwright-Accounts": "relative.json"}, json={}).status_code == 400
    assert fake.asked == [path]


def test_the_schedule_hook_says_whether_the_phone_is_unlocked(monkeypatch):
    from tapwright import client

    class S:
        accounts = "C:/vault.json"
    seen = []
    monkeypatch.setattr(client, "unlock_phone", lambda accounts=None: seen.append(accounts) or {"ok": True})
    assert unlock.schedule_hook(S()) is True and seen == ["C:/vault.json"]
    monkeypatch.setattr(client, "unlock_phone", lambda accounts=None: {"ok": False, "code": "refused"})
    assert unlock.schedule_hook(S()) is False

    def boom(accounts=None):
        raise OSError("no daemon")
    monkeypatch.setattr(client, "unlock_phone", boom)
    assert unlock.schedule_hook(S()) is False


def test_the_passcode_is_redacted_as_a_value(tmp_path):
    from tapwright import accounts
    loaded = accounts.load(vault(tmp_path))
    assert ("phone", "passcode", PASSCODE) in loaded.entries()
    d = make()
    d.accounts.remember(loaded)
    said = d.scrub_everywhere(f"code {PASSCODE} and \"{PASSCODE}\"")
    assert PASSCODE not in said and said.startswith("code <phone's passcode> and ")
    assert PASSCODE not in d.scrub_everywhere(" ".join(PASSCODE))  # whitespace-spread too (§20.3)

"""Review 1.7, half A (W142): H1 (locking the phone stops the agent), H2 (the passcode attempts are kept across processes,
and the passcode comes only from serve's own vault), H3 (a one-time code goes only into a code field), M2 (the passcode
is no redaction entry), L3 (only the entry named `phone` is the passcode), L5 (the notification trigger's digests are
keyed). Each test failed on 1dae706, before the fixes. No phone; all values are synthetic."""
import asyncio
import hashlib
import json

import pytest
from fakes import FakeSession
from starlette.testclient import TestClient
from test_daemon import finish, make, run, started
from test_unlock import PASSCODE, Clock, Screen
from test_unlock_daemon import SessionScreen, locked_daemon, vault

from tapwright import accounts, dialog, notifications as nl, schedules, ui, unlock, vault as vaultlib
from tapwright.auditlog import AuditLog
from tapwright.ceiling import Ceiling
from tapwright.control import Refused
from tapwright.server import create_app

TAP = {"target": {"id": "ok-button"}}
NO_GIT = lambda args, cwd: None  # noqa: E731 (not inside a repo)


def lock(d, s) -> None:
    """The owner presses the side button."""
    s.is_locked = True
    d._set_state("locked", "unlock the phone")


# ---- H1: a lock is the owner's stop --------------------------------------------------------------------------------
def test_a_lock_in_the_middle_of_a_run_ends_it_with_locked_and_types_no_passcode(tmp_path):
    """serve --phone with a passcode, and still: the owner locks the phone between two steps of a `run`, and the run
    ends with `locked` at the next step. Before, that step typed the passcode and went on."""
    async def go():
        d, s = locked_daemon(tmp_path, phone=True)
        s.is_locked = False
        stop, task = await started(d, until=("live",))
        real_tap = s.tap

        async def tap_then_lock(target, hold_ms=60):
            out = await real_tap(target, hold_ms)
            lock(d, s)
            return out
        s.tap = tap_then_lock
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": [{"tool": "tap", **TAP}, {"tool": "tap", **TAP}]})
        assert e.value.code == "locked"
        assert d.lock_screen.pressed == [] and s.is_locked and s.actions().count("tap") == 1
        await finish(stop, task)
    run(go)


@pytest.mark.parametrize("mode", ["dedicated", "no flag"])
def test_without_serve_phone_a_locked_phone_answers_locked_as_in_1_6(tmp_path, mode):
    """--dedicated (and a serve with only --accounts): the passcode is never typed by an agent's call; a person
    unlocks the phone."""
    async def go():
        d, s = locked_daemon(tmp_path, phone=False)
        if mode == "no flag":
            d.ceiling = Ceiling(accounts=d.ceiling.accounts)
        stop, task = await started(d, until=("locked",))
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", TAP)
        assert e.value.code == "locked" and d.lock_screen.pressed == [] and d.lock_screen.calls == []
        await finish(stop, task)
    run(go)


def test_serve_phone_unlocks_at_the_start_of_a_new_top_level_run(tmp_path):
    async def go():
        d, s = locked_daemon(tmp_path, phone=True)
        stop, task = await started(d, until=("locked",))
        r = await d.run_tool("a", "run", {"steps": [{"tool": "tap", **TAP}]})
        assert d.lock_screen.pressed == [PASSCODE] and not s.is_locked and "tap" in s.actions() and r["text"]
        await finish(stop, task)
    run(go)


# ---- H2: attempts across processes, only serve's own vault ---------------------------------------------------------
def test_a_refused_passcode_is_not_typed_again_by_the_next_daemon(tmp_path):
    """The daemon stops when idle, and a new one starts: before, each new one typed the refused passcode once more."""
    path = vault(tmp_path)
    pressed_by_each: list = []

    async def go():
        for _ in range(3):  # three daemons, one after another, the same vault file
            s = FakeSession()
            s.is_locked = True
            d = make(s, audit=AuditLog(tmp_path / "audit"))
            d.lock_screen = SessionScreen(s, right="000000")
            d.ceiling = Ceiling(phone=True, accounts=path)
            stop, task = await started(d, until=("locked",))
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "tap", TAP)
            assert e.value.code == "locked"
            pressed_by_each.append(list(d.lock_screen.pressed))
            await finish(stop, task)
    run(go)
    assert pressed_by_each == [[PASSCODE], [], []]


def test_at_most_three_attempts_a_day_whichever_process_types_them(tmp_path):
    clock = Clock()
    for n in range(3):
        u = unlock.Unlocker(clock=clock, sleep=clock.sleep, confirm_wait=1.0, poll=0.2,
                            ledger=unlock.Ledger(wall=clock))  # a new process each time
        screen = Screen()
        assert asyncio.run(u.unlock(screen, PASSCODE)).code == "unlocked"
        clock.t += 2 * 3600
    u = unlock.Unlocker(clock=clock, sleep=clock.sleep, confirm_wait=1.0, poll=0.2, ledger=unlock.Ledger(wall=clock))
    screen = Screen()
    r = asyncio.run(u.unlock(screen, PASSCODE))
    assert (r.code, r.message) == ("limit", unlock.LIMITED_DAY) and screen.pressed == []
    clock.t += 24 * 3600
    assert asyncio.run(u.unlock(Screen(), PASSCODE)).code == "unlocked"


def test_a_changed_file_gets_one_try_then_nothing_until_a_person_resets(tmp_path):
    clock = Clock()
    ledger = unlock.Ledger(wall=clock)
    u = unlock.Unlocker(clock=clock, sleep=clock.sleep, confirm_wait=1.0, poll=0.2, ledger=ledger)
    screen = Screen(right="000000")
    first, second, third = (unlock.Source("v.json", n, 40) for n in (1, 2, 3))
    assert asyncio.run(u.unlock(screen, PASSCODE, source=first)).code == "refused"
    clock.t += 7200
    assert asyncio.run(u.unlock(screen, "135790", source=second)).code == "refused"  # the file changed: one try
    clock.t += 7200
    held = asyncio.run(u.unlock(screen, "246802", source=third))
    assert (held.code, held.message) == ("refused", unlock.REFUSED_HELD) and screen.pressed == [PASSCODE, "135790"]
    done, said = asyncio.run(ledger.reset())
    assert done and said.startswith("Cleared")
    screen.right = "246802"
    assert asyncio.run(u.unlock(screen, "246802", source=third)).code == "unlocked"


def test_the_record_keeps_nothing_of_the_passcode_and_an_unreadable_one_types_nothing(tmp_path):
    clock = Clock()
    u = unlock.Unlocker(clock=clock, sleep=clock.sleep, confirm_wait=1.0, poll=0.2, ledger=unlock.Ledger(wall=clock))
    assert asyncio.run(u.unlock(Screen(right="000000"), PASSCODE, source=unlock.Source("v.json", 5, 40))).code == "refused"
    text = unlock.state_file().read_text("utf-8")
    record = json.loads(text)
    assert set(record) == {"version", "tries", "refused", "retried", "pending"}
    assert record["refused"] == {"path": "v.json", "mtime_ns": 5, "size": 40}
    assert PASSCODE not in text and hashlib.sha256(PASSCODE.encode()).hexdigest() not in text
    unlock.state_file().write_text("{not json", "utf-8")
    screen = Screen()
    r = asyncio.run(u.unlock(screen, PASSCODE))
    assert (r.code, r.message) == ("no_record", unlock.NO_RECORD) and screen.pressed == []
    done, said = asyncio.run(unlock.Ledger().reset())
    assert done and "started again" in said


def test_the_passcode_is_typed_only_from_serves_own_vault(tmp_path):
    """A call or a request that names its own vault file never gets its passcode typed (each guess would be "a changed
    passcode")."""
    async def go():
        d, s = locked_daemon(tmp_path, phone=True)
        own = d.ceiling.accounts
        d.ceiling = Ceiling(phone=True)  # serve --phone with no --accounts
        stop, task = await started(d, until=("locked",))
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", TAP, accounts=own)
        assert e.value.code == "locked" and d.lock_screen.pressed == []
        assert (await d.unlock_phone()).code == "no_passcode" and d.lock_screen.pressed == []
        await finish(stop, task)
    run(go)


def test_the_unlock_endpoint_refuses_a_file_serve_wasnt_given(tmp_path):
    class Fake:
        asked: list = []

        async def unlock_phone(self, *a):
            self.asked.append(a)
            return unlock.Result(True, "unlocked")

        def status(self):
            return {"protocol": 1}
    fake, token = Fake(), "t" * 43
    auth = {"Authorization": f"Bearer {token}", "X-Tapwright-Accounts": vault(tmp_path)}
    with TestClient(create_app(fake, token, hosts={"testserver"})) as c:
        r = c.post("/api/phone/unlock", headers=auth, json={})
        assert r.status_code == 403 and r.json()["error"]["code"] == "not_allowed"
    assert fake.asked == []


def test_unlock_reset_is_a_command(tmp_path, monkeypatch, capsys):
    from tapwright import cli
    monkeypatch.setattr("sys.argv", ["tapwright", "unlock-reset"])
    assert cli.main() == 0
    assert "nothing to clear" in capsys.readouterr().out


# ---- H3: a one-time code only into a code field --------------------------------------------------------------------
def test_a_code_is_never_read_or_typed_into_a_field_that_isnt_a_code_field(tmp_path):
    from test_notifications import BUNDLE, otp_daemon

    async def go():
        d, s, _ = otp_daemon(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        for extra in ({}, {"dry_run": True}):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "type_text", {"target": {"id": "email"}, "text": "${otp." + BUNDLE + "}", **extra})
            assert e.value.code == "not_allowed" and "code field" in str(e.value)
        assert s.typed == [] and s.center_reads == 0  # no code read, nothing typed
        await finish(stop, task)
    run(go)


def test_a_code_field_is_one_line_and_says_code_by_its_own_words():
    def field(label="", ident="", value="", placeholder="", kind="TextField"):
        return ui.Element("e1", "input", label, ident, value, type=kind, placeholder=placeholder)
    assert nl.otp_target(field("Verification code", "otp-input"))
    assert nl.otp_target(field(placeholder="Enter code", value="Enter code"))  # the placeholder shows as its value
    assert not nl.otp_target(field("Verification code", kind="TextView"))  # many lines: a composer
    assert not nl.otp_target(field("Message", "composer", value="code"))  # typed text never makes it a code field
    assert not nl.otp_target(field("Email", "email"))
    assert not nl.otp_target(ui.Element("e2", "button", "Enter code", "code-btn", type="Button"))


def test_a_draft_that_holds_a_one_time_code_says_so(tmp_path):
    code = "834201"
    caption = dialog.draft_caption("send", "Chat", "<one-time code>", True)
    assert caption == 'Send this in Chat? "<one-time code>" A one-time code is in this draft.'

    async def go():
        s = FakeSession()
        s.tree = [ui.Element("e1", "input", "Message", "composer", f"here: {code}", type="TextView", focused=True)]
        d = make(s, audit=AuditLog(tmp_path / "audit"))
        d.accounts.remember_code(code)
        text, holds = await d._draft_text()
        assert holds and code not in text and "<one-time code>" in text
        s.tree = [ui.Element("e1", "input", "Message", "composer", "see you at 6", type="TextView", focused=True)]
        assert (await d._draft_text())[1] is False
    run(go)


# ---- M2: the passcode is no oracle ---------------------------------------------------------------------------------
def test_the_passcode_is_not_redacted_so_a_date_doesnt_give_it_away(tmp_path):
    path = tmp_path / "v.json"
    path.write_text(json.dumps({"version": 2, "vault": {"phone": {"kind": "phone", "passcode": "2026"}}}), "utf-8")
    loaded = accounts.load(str(path), NO_GIT)
    assert loaded.passcode == "2026" and all(f != "passcode" for _, f, _ in loaded.entries())
    d = make()
    d.accounts.remember(loaded)
    assert d.scrub_everywhere("Saturday, 14 Nov 2026") == "Saturday, 14 Nov 2026"


# ---- L3: only the entry named phone ---------------------------------------------------------------------------------
def test_only_the_entry_named_phone_is_the_passcode(tmp_path):
    path = tmp_path / "v.json"
    path.write_text(json.dumps({"version": 2, "vault": {
        "tester2": {"kind": "account", "username": "tester2@example.com", "password": "hunter2-PW-9x"},
        "tester2_phone": {"kind": "phone", "passcode": "4321"}}}), "utf-8")
    assert accounts.load(str(path), NO_GIT).passcode is None
    path.write_text(json.dumps({"version": 2, "vault": {"phone": {"kind": "phone", "passcode": "482913"}}}), "utf-8")
    assert accounts.load(str(path), NO_GIT).passcode == "482913"


def test_protect_drops_an_accounts_passcode_and_never_makes_it_a_phone_entry():
    dropped: list = []
    out = vaultlib._as_version2({"version": 1, "accounts": {
        "tester2": {"username": "tester2@example.com", "password": "hunter2-PW-9x", "passcode": "4321"},
        "pinonly": {"passcode": "9999"},
        "phone": {"passcode": "482913"}}}, dropped)
    assert out == {"version": 2, "vault": {
        "tester2": {"kind": "account", "username": "tester2@example.com", "password": "hunter2-PW-9x"},
        "pinonly": {"kind": "account"},
        "phone": {"kind": "phone", "passcode": "482913"}}}
    assert dropped == ["tester2", "pinonly"]


# ---- L5: the trigger's digests are keyed ---------------------------------------------------------------------------
def test_a_notification_digest_cant_be_tried_against_every_code():
    n = nl.Notification("com.apple.MobileSMS", "Messages", "now", (0.0, 60.0), "Instagram",
                        "Your Instagram code is 482913. Don't share it.", (10.0, 10.0))
    secret = schedules.trigger_secret()
    assert secret is not None and secret == schedules.trigger_secret()  # made once, kept
    assert len(schedules.trigger_key_path().read_text().strip()) == 64
    digest = n.key(secret)
    plain = hashlib.sha256("\0".join((n.bundle, n.title, n.body)).encode()).hexdigest()[:24]
    assert len(digest) == 24 and digest != plain and digest != n.key(b"another key of 32 bytes........")
    with pytest.raises(ValueError):
        n.key(b"")


def test_an_older_record_of_unkeyed_digests_is_noted_again_not_run(tmp_path):
    s = schedules.Schedule("n", "post_update", {}, None, {"notification": "com.example.wayfare"})
    (tmp_path / "n.seen").write_text(json.dumps({"notification": True, "seen": {"a" * 24: 1}}), "utf-8")
    assert schedules.new_notifications(s, ["b" * 24], tmp_path) is False  # noted, no run
    assert schedules.new_notifications(s, ["c" * 24, "b" * 24], tmp_path) is True  # then one new: one run

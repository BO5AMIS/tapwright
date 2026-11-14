"""Regressions for the existing second CodeRabbit review on PR 12; synthetic data and phone responses only."""
import asyncio
import datetime
import json
import threading
from pathlib import Path

import pytest

from tapwright import accounts, auditlog, notifications, pidlock, unlock, vault
from test_notifications import center, cell, ios26_cell
from test_unlock import Clock, PASSCODE, Screen, unlocker
from test_vault import FakeDpapi, VAULT, write


@pytest.mark.parametrize("keep_current", [False, True])
def test_audit_pruning_does_not_erase_an_acknowledged_concurrent_append(tmp_path, monkeypatch, keep_current):
    now = datetime.datetime(2026, 11, 14, tzinfo=datetime.timezone.utc)
    path = tmp_path / "2026-11.jsonl"
    entries = [{"t": "2026-07-01T00:00:00.000Z", "kind": "grant", "name": "old"}]
    if keep_current:
        entries.append({"t": "2026-11-01T00:00:00.000Z", "kind": "grant", "name": "kept"})
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    writer = auditlog.AuditLog(tmp_path, now=lambda: now)
    writer._pruned_on = now.date()
    reached_lock = threading.Event()
    outcomes = []

    def append():
        try:
            outcomes.append(writer.write("grant", name="concurrent"))
        finally:
            reached_lock.set()  # the old implementation has no lock, so it signals after appending

    thread = threading.Thread(target=append)
    original_take = pidlock.take

    def take(*args, **kwargs):
        token = original_take(*args, **kwargs)
        if threading.current_thread() is thread:
            reached_lock.set()
        return token

    original_read = Path.read_text

    def read(self, *args, **kwargs):
        text = original_read(self, *args, **kwargs)
        if self == path and threading.current_thread() is threading.main_thread() and not thread.ident:
            thread.start()
            assert reached_lock.wait(5)
        return text

    monkeypatch.setattr(pidlock, "take", take)
    monkeypatch.setattr(Path, "read_text", read)
    auditlog.AuditLog(tmp_path, now=lambda: now).prune()
    thread.join(5)
    assert not thread.is_alive() and outcomes == [True]
    records = [json.loads(line) for line in auditlog.read_lines(tmp_path)]
    assert [r["name"] for r in records] == (["kept"] if keep_current else []) + ["concurrent"]


@pytest.mark.parametrize("shape", ["ios26", "legacy", "static"])
def test_a_comma_in_a_notification_app_name_never_grants_another_apps_scope(shape):
    private = "com.example.private"
    name = "Mail, Work"
    if shape == "ios26":
        xml = ios26_cell(name, "now", "Private title", "Private body", more="2 more")
    else:
        xml = cell(name, "now", "Private title", "Private body", texts=shape == "static")
    apps = {private: name}
    assert notifications.parse(center(xml), apps, lambda b: b == "com.apple.mobilemail") == []
    allowed = notifications.parse(center(xml), apps, lambda b: b == private)
    assert len(allowed) == 1 and allowed[0].bundle == private and allowed[0].title == "Private title"
    assert notifications.parse(center(xml), {}, lambda b: b == "com.apple.mobilemail") == []


@pytest.mark.parametrize("line_break", ["\n", "\r"])
def test_a_keychain_path_with_a_line_break_is_rejected_before_any_write(line_break):
    calls = []
    keychain = vault.Keychain(lambda args, stdin: calls.append((args, stdin)) or (44, "", ""))
    with pytest.raises(vault.VaultOpenError, match="line break"):
        keychain.write("/tmp/vault" + line_break + "delete-generic-password", json.dumps(VAULT))
    assert all(args != ["-i"] for args, _ in calls)


def test_vault_protection_preserves_a_source_changed_during_crypto(tmp_path):
    path = write(tmp_path, VAULT)
    newer = json.dumps({"version": 2, "vault": {"updated": {"kind": "account", "username": "new-user"}}})

    class Changed(FakeDpapi):
        def protect(self, data, entropy):
            Path(path).write_text(newer, encoding="utf-8")
            return super().protect(data, entropy)

    ok, message = vault.protect(path, vault.Deps("win32", Changed()), accounts.check_text)
    assert not ok and "changed" in message and Path(path).read_text(encoding="utf-8") == newer


def test_two_protection_calls_for_the_same_vault_do_not_interleave(tmp_path):
    path = write(tmp_path, VAULT)
    nested = []

    class Concurrent(FakeDpapi):
        def protect(self, data, entropy):
            nested.append(vault.protect(path, vault.Deps("win32", FakeDpapi()), accounts.check_text))
            return super().protect(data, entropy)

    assert vault.protect(path, vault.Deps("win32", Concurrent()), accounts.check_text)[0]
    assert len(nested) == 1 and not nested[0][0] and "lock" in nested[0][1]


@pytest.mark.parametrize("unknown_on", [1, 2])
def test_unknown_xctest_prompt_status_never_types_a_passcode(unknown_on):
    class Unknown(Screen):
        checks = 0

        async def xctest_prompt(self):
            self.checks += 1
            return None if self.checks == unknown_on else False

    u, _ = unlocker()
    phone = Unknown()
    result = asyncio.run(u.unlock(phone, PASSCODE))
    assert not result.ok and result.code == "not_proven"
    assert phone.pressed == [] and phone.secrets == [] and u.tries_left() == 2


def test_an_old_unlock_completion_cannot_clear_a_newer_attempt(tmp_path):
    async def finish(ledger, token, success):
        # Exercise the old signature too, so the pre-fix failure proves the stale-completion bug.
        if isinstance(token, str):
            await ledger.end(token, success)
        else:
            await ledger.end(success)

    async def scenario():
        clock = Clock()
        a = unlock.Ledger(tmp_path / "ledger.json", wall=clock)
        b = unlock.Ledger(tmp_path / "ledger.json", wall=clock)
        old_source, new_source = unlock.Source("vault", 1, 10), unlock.Source("vault", 2, 10)
        old = await a.begin(old_source)
        clock.t += unlock.PENDING_FOR + 1
        newer = await b.begin(new_source)
        assert not isinstance(newer, unlock.Result)
        await finish(b, newer, False)
        before = b.read()
        await finish(a, old, True)
        assert b.read() == before
        assert b.peek(new_source).code == "refused"

    asyncio.run(scenario())

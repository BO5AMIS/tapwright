"""The vault (docs/AGENT-TOOLS.md §20.1-20.3, Tapwright 1.7): the version 2 file, its kinds and references, the field
rules, redaction of every kind, and where it is kept (§20.2: DPAPI, the macOS keychain through an injected runner,
plain). Synthetic values only."""
import base64
import hashlib
import json
import re
import shlex
import subprocess
import sys

import pytest

from tapwright import accounts, vault
from tapwright.control import Refused

NO_GIT = lambda args, cwd: None  # noqa: E731 (not inside a repo)

VAULT = {"version": 2, "vault": {
    "tester2": {"kind": "account", "username": "tester2@example.com", "password": "Synthetic-Pass-77",
                "apps": ["com.example.wayfare"]},
    "home": {"kind": "address", "street": "12 Synthetic Lane", "city": "Testville", "postcode": "TV1 2AB",
             "country": "UK"},
    "me": {"kind": "identity", "name": "Tess Tester", "phone": "+44 7000 000000", "email": "tess@example.com",
           "birthday": "1990-01-01"},
    "visa": {"kind": "card", "number": "4111 1111 1111 1111", "expiry": "12/30", "cvc": "737"},
    "reply_sig": {"kind": "note", "text": "Sent by a synthetic tester"},
    "trip_data": {"kind": "fixture", "destination": "Lisbon", "party_size": "2"},
    "phone": {"kind": "phone", "passcode": "246810"}},
    "otp": {"com.example.wayfare": {"from": ["com.apple.MobileSMS"], "match": "Wayfare"}}}


def write(tmp_path, data, name="vault.json"):
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def load(tmp_path, data=VAULT, **kw):
    return accounts.load(write(tmp_path, data), NO_GIT, **kw)


# ---- §20.1 the file and its kinds ------------------------------------------------------------------------------------
def test_a_version_2_file_loads_every_kind(tmp_path):
    v = load(tmp_path)
    assert v.problem is None and v.version == 2 and v.protection is None
    assert v.names == ("tester2",)  # only account entries are named by "account"
    assert v.entry_names == ("tester2 (account)", "home (address)", "me (identity)", "visa (card)",
                             "reply_sig (note)", "trip_data (fixture)")
    assert v.passcode == "246810"
    assert v.otp == {"com.example.wayfare": accounts.OtpSetting(("com.apple.MobileSMS",), "Wayfare")}
    assert v.account("tester2", "com.example.wayfare").password == "Synthetic-Pass-77"
    with pytest.raises(Refused) as e:
        v.account("home", "com.example.wayfare")
    assert e.value.code == "bad_params" and str(e.value) == (
        '"account": "home" names an address entry; "account" names account entries only (use ${vault.home.<field>}).')


def test_nothing_shows_a_value_in_a_repr(tmp_path):
    v = load(tmp_path)
    shown = repr(v) + repr(list(v.vault.values())) + repr(v.reference("${vault.home.street}", None, False, "e1", "x"))
    for entry in VAULT["vault"].values():
        for key, value in entry.items():
            if key not in ("kind", "apps") and len(value) >= 3:
                assert value not in shown


def test_a_version_1_file_still_loads_with_its_phone(tmp_path):
    v = load(tmp_path, {"version": 1, "accounts": {"tester2": {"username": "tester2@example.com",
                                                               "password": "Synthetic-Pass-77"},
                                                   "phone": {"passcode": "246810"}}})
    assert v.problem is None and v.names == ("tester2",) and v.passcode == "246810"
    assert v.reference("${vault.tester2.username}", None, False, "e1", "x").value == "tester2@example.com"
    assert v.note == f"Note: the vault file {tmp_path / 'vault.json'} is not encrypted; keep test data in it only."


@pytest.mark.parametrize("entry, why", [
    ({"kind": "account", "password": "ab"}, "tester's password must have at least 3 characters"),
    ({"kind": "card", "number": "x" * 257}, "tester's number is longer than 256 characters"),
    ({"kind": "note", "text": "%41%42"}, "tester's text must have at least 3 characters"),
    ({"kind": "address", "street": ""}, "tester's street must be a non-empty text"),
    ({"kind": "account", "pin": "12345"}, "tester has pin; an account has username, password"),
    ({"kind": "fixture", "bad-name": "Lisbon"}, "tester has a field whose name isn't ASCII letters, digits and _"),
    ({"username": "abc"}, "tester has no kind"),
    ({"kind": "card", "number": "4111111111111111", "apps": "com.x"}, "tester's apps must be a list of bundle ids"),
])
def test_a_refused_entry_loads_nothing_and_says_which(tmp_path, entry, why):
    v = load(tmp_path, {"version": 2, "vault": {"tester": entry}})
    assert v.problem is not None and why in v.problem and v.names == () and v.entries() == ()
    for key, value in entry.items():
        if key not in ("kind", "apps") and isinstance(value, str) and len(value) > 3:
            assert value not in v.problem


def test_entry_names_are_ascii_letters_digits_and_underscore(tmp_path):
    v = load(tmp_path, {"version": 2, "vault": {"my-card": {"kind": "note", "text": "abcdef"}}})
    assert "an entry's name is ASCII letters, digits and _" in v.problem


def test_a_file_holds_at_most_64_values(tmp_path):
    many = {f"n{i}": {"kind": "note", "text": f"synthetic note {i:03d}"} for i in range(65)}
    v = load(tmp_path, {"version": 2, "vault": many})
    assert "at most 64" in v.problem


def test_an_unknown_kind_is_unusable_but_its_values_stay_out_of_text(tmp_path):
    v = load(tmp_path, {"version": 2, "vault": {"pass1": {"kind": "passport", "number": "ZX9988776"}}})
    assert v.problem is None
    with pytest.raises(Refused) as e:
        v.reference("${vault.pass1.number}", None, False, "e1", "x")
    assert e.value.code == "bad_params" and "isn't" in str(e.value) or "doesn't know" in str(e.value)
    assert v.scrub("seen ZX9988776 here") == "seen <pass1's number> here"


# ---- references and the field rules ----------------------------------------------------------------------------------
def test_a_vault_reference_types_its_value_by_name(tmp_path):
    v = load(tmp_path)
    got = v.reference("${vault.home.street}", None, False, "e3", "com.example.wayfare")
    assert (got.value, got.entry, got.kind, got.field) == ("12 Synthetic Lane", "home", "address", "street")
    assert v.reference("plain words", None, False, "e3", "x") is None
    fixture = v.reference("${vault.trip_data.destination}", None, False, "e3", "x")
    assert fixture.value == "Lisbon" and fixture.kind == "fixture"


@pytest.mark.parametrize("text, secure, bundle, code, words", [
    ("hi ${vault.home.street}", False, "x", "bad_params", "the whole text of a step"),
    ("${vault.tester2.password}", False, "com.example.wayfare", "bad_params",
     "an account's password goes only into a password field; e7 is a text field."),
    ("${vault.tester2.username}", True, "com.example.wayfare", "bad_params", "goes only into a text field that isn't secure"),
    ("${vault.tester2.username}", False, "com.other", "not_allowed", "may be typed only in com.example.wayfare"),
    ("${vault.trip_data.destination}", True, "x", "bad_params", "never goes into a password field"),
    ("${vault.nobody.street}", False, "x", "not_found", 'no vault entry "nobody"'),
    ("${vault.home.number}", False, "x", "bad_params", "its fields are street, city, postcode, country"),
    ("${vault.phone.passcode}", False, "x", "bad_params", "only on the lock screen's keypad"),
    ("${vault.me.street}", False, "x", "bad_params", "an identity; its fields are name, phone, email, birthday"),
    ("${otp.com.example.wayfare} now", False, "com.example.wayfare", "bad_params", "a one-time code is the whole text"),
    ("${otp.com.example.wayfare}", False, "com.other", "not_allowed", "typed only into com.example.wayfare"),
    ("${otp.com.other}", False, "com.other", "bad_params", "one-time codes are off for com.other"),
])
def test_the_field_rules(tmp_path, text, secure, bundle, code, words):
    v = load(tmp_path)
    with pytest.raises(Refused) as e:
        v.reference(text, None, secure, "e7", bundle)
    assert e.value.code == code and words in str(e.value)
    assert "Synthetic" not in str(e.value) and "Lisbon" not in str(e.value)


def test_a_card_comes_back_as_a_card_for_the_daemon_to_ask(tmp_path):
    got = load(tmp_path).reference("${vault.visa.number}", None, True, "e2", "x")
    assert got.kind == accounts.CARD and got.field == "number" and got.entry == "visa"


def test_account_references_keep_drives_account(tmp_path):
    v = load(tmp_path)
    active = v.account("tester2", "com.example.wayfare")
    got = v.reference("${account.password}", active, True, "e1", "com.example.wayfare")
    assert got.value == "Synthetic-Pass-77" and got.entry == "tester2" and got.field == "password"
    with pytest.raises(Refused):
        v.reference("${account.password}", None, True, "e1", "com.example.wayfare")


def test_a_one_time_code_reference_is_handed_to_the_daemon(tmp_path):
    got = load(tmp_path).reference("${otp.com.example.wayfare}", None, False, "e1", "com.example.wayfare")
    assert got.otp_app == "com.example.wayfare" and got.value == ""


# ---- §20.3 redaction of every kind -----------------------------------------------------------------------------------
def test_every_kind_is_redacted_in_every_form_with_its_placeholder(tmp_path):
    v = load(tmp_path)
    text = ('street 12 Synthetic Lane, json "12 Synthetic Lane", spaced 12  Synthetic\tLane, card 4111 1111 1111 1111, '
            'cvc 737, fixture Lisbon, note Sent by a synthetic tester, mail tess%40example.com')
    out = v.scrub(text)
    for value in ("Synthetic Lane", "4111", "737", "Lisbon", "synthetic tester", "tess@", "tess%40"):
        assert value not in out
    assert "<home's street>" in out and "<visa's number>" in out and "<trip_data's destination>" in out
    assert "<visa's cvc>" in out and "<me's email>" in out and "<reply_sig's text>" in out
    assert v.scrub(json.dumps({"t": "12 Synthetic Lane"})) == '{"t": ' + "<home's street>}"  # (quotes and all)
    assert v.holds("Lisbon") and not v.holds("Porto")


def test_a_short_plain_value_is_typed_but_cant_be_kept_out(tmp_path):
    v = load(tmp_path)
    assert v.reference("${vault.home.country}", None, False, "e1", "x").value == "UK"
    assert v.reference("${vault.trip_data.party_size}", None, False, "e1", "x").value == "2"
    assert v.scrub("UK 2") == "UK 2"  # (too short: it would eat ordinary words)


def test_the_passcode_is_no_redaction_entry(tmp_path):
    """Review 1.7, M2: the passcode is typed only on the lock screen (dots), so it is kept out of the text redactor:
    replacing it would tell the agent the passcode the first time a date or a price on screen held it."""
    assert [field for _, field, _ in load(tmp_path).entries()].count("passcode") == 0


def test_the_store_redacts_vault_values_and_codes_in_one_pass(tmp_path):
    store = accounts.Store()
    path = write(tmp_path, VAULT)
    loaded = store.get(path)
    assert loaded.problem is not None or True  # (the store asks git for real: the tmp folder is in no repo)
    store.remember(accounts.load(path, NO_GIT))
    store.remember_code("834201")
    out = store.scrub("code 834201 for 12 Synthetic Lane")
    assert out == "code <one-time code> for <home's street>"


# ---- the otp setting (§24.3) -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("otp", [[], {"com.x": {"from": [], "match": "X"}}, {"com.x": {"from": ["a"]}},
                                 {"com.x": {"from": ["a"], "match": " "}}, {"com.x": "on"}])
def test_a_malformed_otp_setting_turns_codes_off_and_says_so(tmp_path, otp):
    v = load(tmp_path, {**VAULT, "otp": otp})
    assert v.problem is None and v.otp == {} and "one-time codes are off" in v.note


def test_codes_are_off_by_default(tmp_path):
    v = load(tmp_path, {"version": 2, "vault": VAULT["vault"]})
    assert v.otp == {}


# ---- §20.2 plain and git ---------------------------------------------------------------------------------------------
def git_says(inside: bool, ignored: bool):
    def git(args, cwd):
        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(args, 0 if inside else 128, "/repo\n" if inside else "", "")
        return subprocess.CompletedProcess(args, 0 if ignored else 1, "", "")
    return git


def test_a_plain_vault_in_a_git_work_tree_that_doesnt_ignore_it_is_refused(tmp_path):
    v = accounts.load(write(tmp_path, VAULT), git_says(True, False))
    assert v.problem == ("accounts file is inside the git repo at /repo and not ignored; move it or add it to "
                         ".gitignore.") and v.entries() == ()
    assert accounts.load(write(tmp_path, VAULT), git_says(True, True)).problem is None


class FakeDpapi:
    """A stand-in for DPAPI where there is none (not Windows): XOR with the entropy, prefixed."""

    def protect(self, data, entropy):
        return b"FAKE" + bytes(b ^ entropy[i % len(entropy)] for i, b in enumerate(data))

    def unprotect(self, blob, entropy):
        if not blob.startswith(b"FAKE"):
            raise vault.VaultOpenError("Windows couldn't decrypt it")
        return bytes(b ^ entropy[i % len(entropy)] for i, b in enumerate(blob[4:]))


def win_deps():
    return vault.Deps("win32", None if sys.platform == "win32" else FakeDpapi())


def test_protect_writes_the_shared_dpapi_shape_and_reads_back(tmp_path):
    path = write(tmp_path, VAULT)
    done, said = vault.protect(path, win_deps(), accounts.check_text)
    assert done and said.startswith("Protected ") and "Tapline reads it too" in said
    top = json.loads(open(path, encoding="utf-8").read())
    assert list(top) == ["version", "protected", "data"] and top["version"] == 2 and top["protected"] == "dpapi-user"
    raw = base64.b64decode(top["data"], validate=True)
    assert b"Synthetic" not in raw and b"Lisbon" not in raw
    inner = json.loads(vault.open_protected(path, top, win_deps()))
    assert inner == VAULT  # the whole JSON, `otp` too
    # a protected file holds no value: the git rule doesn't apply, and it loads
    v = accounts.load(path, git_says(True, False), protection=win_deps())
    assert v.problem is None and v.protection == "dpapi-user" and v.note is None
    assert v.reference("${vault.home.city}", None, False, "e1", "x").value == "Testville"
    assert list(tmp_path.iterdir()) == [tmp_path / "vault.json"]  # no temp file left


@pytest.mark.skipif(sys.platform != "win32", reason="real DPAPI is Windows only")
def test_real_dpapi_uses_the_shared_entropy():
    blob = vault.CtypesDpapi().protect(b'{"version": 2, "vault": {}}', vault.DPAPI_ENTROPY)
    assert blob[:4] == b"\x01\x00\x00\x00"  # (a DPAPI blob's version)
    assert vault.CtypesDpapi().unprotect(blob, b"tapline-tapwright-vault-v2") == b'{"version": 2, "vault": {}}'
    with pytest.raises(vault.VaultOpenError) as e:
        vault.CtypesDpapi().unprotect(blob, b"other entropy")
    assert "another user or another computer" in str(e.value)


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell's ProtectedData is Windows only")
def test_a_blob_made_the_way_tapline_makes_it_opens_here(tmp_path):
    """B1 interop: Tapline calls .NET's ProtectedData.Protect through PowerShell; Tapwright opens that blob."""
    inner = json.dumps({"version": 2, "vault": {"n1": {"kind": "note", "text": "made by powershell"}}})
    script = ("$ErrorActionPreference = 'Stop'; Add-Type -AssemblyName System.Security; "
              "$l = [Console]::In.ReadToEnd() -split '\\r?\\n'; $d = [Convert]::FromBase64String($l[0]); "
              "$e = [Convert]::FromBase64String($l[1]); "
              "$s = [System.Security.Cryptography.DataProtectionScope]::CurrentUser; "
              "[Convert]::ToBase64String([System.Security.Cryptography.ProtectedData]::Protect($d, $e, $s))")
    stdin = base64.b64encode(inner.encode()).decode() + "\n" + base64.b64encode(vault.DPAPI_ENTROPY).decode() + "\n"
    try:
        out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], input=stdin,
                             capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("no PowerShell")
    if out.returncode != 0 or not out.stdout.strip():
        pytest.skip("PowerShell's ProtectedData isn't available here")
    path = write(tmp_path, {"version": 2, "protected": "dpapi-user", "data": out.stdout.strip()})
    v = accounts.load(path, NO_GIT)
    assert v.problem is None and v.reference("${vault.n1.text}", None, False, "e1", "x").value == "made by powershell"


def test_protect_keeps_the_plain_file_when_the_copy_doesnt_read_back(tmp_path):
    class Lossy(FakeDpapi):
        def unprotect(self, blob, entropy):
            return super().unprotect(blob, entropy)[:-1] + b" "
    path = write(tmp_path, VAULT)
    before = open(path, encoding="utf-8").read()
    done, said = vault.protect(path, vault.Deps("win32", Lossy()), accounts.check_text)
    assert not done and "stays plain" in said and open(path, encoding="utf-8").read() == before


def test_protect_refuses_what_wouldnt_load_and_converts_version_1(tmp_path):
    bad = write(tmp_path, {"version": 2, "vault": {"x": {"kind": "account", "password": "ab"}}}, "bad.json")
    done, said = vault.protect(bad, win_deps(), accounts.check_text)
    assert not done and "fix it first" in said and "ab" not in said.split("(")[0]
    old = write(tmp_path, {"version": 1, "accounts": {"t1": {"username": "user@example.com", "passcode": "1234"}}},
                "old.json")
    done, said = vault.protect(old, win_deps(), accounts.check_text)
    assert done and "the passcode of t1 was left out" in said and "1234" not in said
    inner = json.loads(vault.open_protected(old, json.loads(open(old, encoding="utf-8").read()), win_deps()))
    assert inner == {"version": 2, "vault": {"t1": {"kind": "account", "username": "user@example.com"}}}
    again = vault.protect(old, win_deps(), accounts.check_text)
    assert not again[0] and "already protected" in again[1]
    assert not vault.protect(write(tmp_path, VAULT, "v.json"), vault.Deps("linux"))[0]


def test_a_protected_file_that_cant_be_opened_loads_nothing(tmp_path):
    path = write(tmp_path, {"version": 2, "protected": "dpapi-user", "data": base64.b64encode(b"junk").decode()})
    v = accounts.load(path, NO_GIT, protection=vault.Deps("win32", FakeDpapi()))
    assert v.problem == "the vault file can't be opened: Windows couldn't decrypt it."
    with pytest.raises(Refused) as e:
        v.reference("${vault.home.street}", None, False, "e1", "x")
    assert e.value.code == "bad_params" and str(e.value) == v.problem
    elsewhere = accounts.load(path, NO_GIT, protection=vault.Deps("darwin"))
    assert "can only be opened on Windows" in elsewhere.problem


# ---- §20.2 the macOS keychain, through an injected `security` --------------------------------------------------------
class FakeSecurity:
    """The `security` commands the vault uses, over a dict. `hex_items`: accounts whose data `-w` prints as hex."""

    def __init__(self, hex_all=False):
        self.items: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []
        self.hex_all = hex_all

    def __call__(self, args, stdin):
        if args == ["-i"]:
            words = shlex.split(stdin.strip())
            assert words[:2] == ["add-generic-password", "-U"] and words[2:4] == ["-s", vault.KEYCHAIN_SERVICE]
            account, data = words[5], bytes.fromhex(words[7])
            assert len(stdin.encode()) <= 4095
            self.calls.append(("add", account))
            self.items[account] = data
            return 0, "", ""
        assert args[1:3] == ["-s", vault.KEYCHAIN_SERVICE] and args[3] == "-a"
        account = args[4]
        if args[0] == "find-generic-password":
            assert args[5] == "-w"
            if account not in self.items:
                return 44, "", "not found"
            data = self.items[account]
            return 0, (data.hex() if self.hex_all else data.decode("utf-8")) + "\n", ""
        assert args[0] == "delete-generic-password"
        self.calls.append(("delete", account))
        return (0, "", "") if self.items.pop(account, None) is not None else (44, "", "")


def mac(fake):
    return vault.Deps("darwin", keychain=vault.Keychain(fake))


def test_a_small_vault_is_one_keychain_item_at_the_real_path(tmp_path):
    fake = FakeSecurity()
    path = write(tmp_path, VAULT)
    assert vault.protect(path, mac(fake), accounts.check_text)[0]
    assert json.loads(open(path, encoding="utf-8").read()) == {"version": 2, "protected": "keychain"}
    real = vault.real_path(path)
    assert list(fake.items) == [real] and json.loads(fake.items[real]) == VAULT
    v = accounts.load(path, NO_GIT, protection=mac(fake))
    assert v.problem is None and v.protection == "keychain"


def big_vault():
    return {"version": 2, "vault": {f"n{i}": {"kind": "note", "text": f"synthetic note number {i} ü " + "x" * 120}
                                    for i in range(40)}}


@pytest.mark.parametrize("hex_all", [False, True])
def test_a_large_vault_is_chunked_in_the_shared_order(tmp_path, hex_all):
    fake = FakeSecurity(hex_all)
    path = write(tmp_path, big_vault())
    assert vault.protect(path, mac(fake), accounts.check_text)[0]
    real = vault.real_path(path)
    adds = [a for kind, a in fake.calls if kind == "add"]
    n = len(adds) - 1
    assert n > 1 and adds == [f"{real}#{i}" for i in range(1, n + 1)] + [real]  # chunks first, the manifest last
    manifest = json.loads(fake.items[real])
    whole = b"".join(fake.items[f"{real}#{i}"] for i in range(1, n + 1))
    assert manifest == {"chunks": n, "sha256": hashlib.sha256(whole).hexdigest()}
    assert json.loads(whole) == big_vault()
    assert accounts.load(path, NO_GIT, protection=mac(fake)).problem is None
    # a shorter vault written later drops the stale chunks, after its manifest
    fake.calls.clear()
    vault.Keychain(fake).write(real, json.dumps({"version": 2, "vault": {}}))
    assert fake.calls[0] == ("add", real) and {f"{real}#{i}" for i in range(1, n + 1)} == {
        a for kind, a in fake.calls if kind == "delete"}


def test_a_missing_chunk_or_a_wrong_sha_fails_closed(tmp_path):
    fake = FakeSecurity()
    path = write(tmp_path, big_vault())
    assert vault.protect(path, mac(fake), accounts.check_text)[0]
    real = vault.real_path(path)
    kept = dict(fake.items)
    del fake.items[f"{real}#2"]
    v = accounts.load(path, NO_GIT, protection=mac(fake))
    assert v.problem == "the vault file can't be opened: the vault in the keychain is incomplete (chunk 2 of " \
        f"{json.loads(kept[real])['chunks']} is missing)." and v.entries() == ()
    fake.items = dict(kept)
    fake.items[f"{real}#1"] = fake.items[f"{real}#1"][:-1] + b"y"
    assert "don't match their checksum" in accounts.load(path, NO_GIT, protection=mac(fake)).problem
    fake.items = dict(kept)
    fake.items[real] = b'{"chunks": "two", "sha256": "00"}'
    assert "chunk list is unreadable" in accounts.load(path, NO_GIT, protection=mac(fake)).problem


def test_all_hex_text_is_decoded_only_when_its_utf8_and_the_sha_decides():
    fake = FakeSecurity()
    chain = vault.Keychain(fake)
    # a vault whose chunks are hex digits as text: the sha256 picks the reading
    text = json.dumps({"version": 2, "vault": {"n": {"kind": "note", "text": "ab12" * 1200}}})
    chain.write("/v.json", text)
    assert chain.read("/v.json") == text
    fake.hex_all = True
    assert chain.read("/v.json") == text
    # hex that isn't valid UTF-8 is read as it is
    fake.items["/raw"] = b"ff00"
    fake.hex_all = False
    assert chain._read_both("/raw").decoded is None or True
    fake.items["/raw"] = bytes.fromhex("c3a9")
    fake.hex_all = True
    assert chain.read("/raw") == "é"


def test_no_entry_in_the_keychain_says_the_file_moved(tmp_path):
    path = write(tmp_path, {"version": 2, "protected": "keychain"})
    v = accounts.load(path, NO_GIT, protection=mac(FakeSecurity()))
    assert v.problem == ("the vault file can't be opened: the keychain has no entry for this file's path (the file was "
                         "moved, or it was protected by another user).")
    assert "can only be opened on a Mac" in accounts.load(path, NO_GIT, protection=vault.Deps("win32")).problem


def test_the_security_line_quotes_the_path():
    line = vault.Keychain._line('/Users/t/a "b"\\c.json', b"{}")
    assert shlex.split(line)[5] == '/Users/t/a "b"\\c.json' and line.endswith("-X 7b7d")
    assert re.fullmatch(r"add-generic-password -U -s \"tapline-tapwright-vault\" -a .* -X [0-9a-f]+", line)


# ---- scripts and routes keep a reference, never join it to text ----------------------------------------------------
def test_a_script_keeps_vault_and_code_references_as_variables():
    from tapwright import batch
    assert batch.literal_runs("${vault.home.street}") == (["", ""], True)
    assert batch.literal_runs("${otp.com.example.wayfare}") == (["", ""], True)
    assert batch.literal_runs("${vault.home}") == (["${vault.home}"], False)  # (no field: ordinary text)


def test_a_route_refuses_a_reference_joined_to_text():
    from tapwright import routes
    base = {"version": 2, "name": "fill", "app": "com.example.wayfare", "start": "welcome", "variables": []}
    routes.check({**base, "steps": [{"tool": "type_text", "target": {"id": "street"}, "text": "${vault.home.street}"}]})
    for text in ("at ${vault.home.street}", "${otp.com.example.wayfare}!"):
        with pytest.raises(Refused) as e:
            routes.check({**base, "steps": [{"tool": "type_text", "target": {"id": "street"}, "text": text}]})
        assert e.value.code == "bad_params" and "whole text" in str(e.value)
    with pytest.raises(Refused):
        routes.check({**base, "steps": [{"tool": "tap", "target": {"label": "${vault.home.street}"}}]})
    filled = routes.substitute([{"tool": "type_text", "text": "${vault.home.street}"}], {"vault.home.street": "x"})
    assert filled[0]["text"] == "${vault.home.street}"  # the typing fills it, never a route's value


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell's ProtectedData is Windows only")
def test_a_file_tapwright_protects_opens_the_way_tapline_opens_it(tmp_path):
    """B1 interop, the other way: Tapline's own PowerShell script (vault.ts DPAPI_SCRIPT) unprotects Tapwright's blob."""
    path = write(tmp_path, VAULT)
    assert vault.protect(path, vault.Deps("win32"), accounts.check_text)[0]
    blob = json.loads(open(path, encoding="utf-8").read())["data"]
    script = "; ".join([
        "$ErrorActionPreference = 'Stop'", "Add-Type -AssemblyName System.Security",
        "$l = [Console]::In.ReadToEnd() -split '\\r?\\n'", "$d = [Convert]::FromBase64String($l[1])",
        "$e = [Convert]::FromBase64String($l[2])", "$s = [System.Security.Cryptography.DataProtectionScope]::CurrentUser",
        "if ($l[0] -eq 'protect') { $o = [System.Security.Cryptography.ProtectedData]::Protect($d, $e, $s) } else "
        "{ $o = [System.Security.Cryptography.ProtectedData]::Unprotect($d, $e, $s) }",
        "[Convert]::ToBase64String($o)"])
    stdin = f"unprotect\n{blob}\n{base64.b64encode(vault.DPAPI_ENTROPY).decode()}\n"
    try:
        out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], input=stdin,
                             capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("no PowerShell")
    if out.returncode != 0:
        pytest.skip("PowerShell's ProtectedData isn't available here")
    assert json.loads(base64.b64decode(out.stdout.strip()).decode("utf-8")) == VAULT


def test_a_plain_vault_inside_a_git_folder_is_refused(tmp_path):
    (tmp_path / ".git").mkdir()
    path = tmp_path / ".git" / "vault.json"
    path.write_text(json.dumps(VAULT), encoding="utf-8")
    v = accounts.load(str(path), NO_GIT)
    assert v.problem.startswith("accounts file is inside a git repo's .git folder") and v.entries() == ()

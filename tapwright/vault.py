"""Where the vault is kept (docs/AGENT-TOOLS.md §20.2), byte for byte the format Tapline reads and writes:

- **Windows:** `{"version": 2, "protected": "dpapi-user", "data": "<base64 of CryptProtectData(UTF-8 JSON)>"}`, for the
  current user, with the optional entropy fixed to the bytes `tapline-tapwright-vault-v2`. Tapline calls DPAPI through
  PowerShell (.NET's ProtectedData), Tapwright through ctypes: the blob is the same, and needs no path.
- **macOS:** a keychain generic password, service `tapline-tapwright-vault`, account = the file's real path, holding the
  UTF-8 JSON; the file holds `{"version": 2, "protected": "keychain"}`. A vault too long for one `security -i` line is
  chunked (1.14.1): items `<realpath>#1…#n` hold consecutive pieces, and the main item holds the manifest
  `{"chunks": n, "sha256": "<hex of the whole JSON>"}`. Written chunks first, then the manifest, then the stale chunks of
  an earlier, longer vault are deleted. A missing chunk or a sha mismatch fails closed. `security -w` may print an item
  as hex: all-hex text is decoded only when that gives valid UTF-8, and the sha256 decides between the two readings.
  Every call goes through an injected runner, so this is tested without a Mac.
- **Plain:** `{"version": 2, "vault": {...}}`, for test data only (accounts.py says so at every start).

Nothing here ever quotes a value: a failure says why in words. No new dependency (ctypes, `/usr/bin/security`)."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

DPAPI_ENTROPY = b"tapline-tapwright-vault-v2"
KEYCHAIN_SERVICE = "tapline-tapwright-vault"
DPAPI = "dpapi-user"
KEYCHAIN = "keychain"
SECURITY = "/usr/bin/security"
LINE_BUDGET = 3900  # `security -i` reads one line of about 4 KB: the whole command with its hex data
MIN_CHUNK = 64
MAX_AMBIGUOUS = 12  # chunks that read two ways (hex or text) searched against the sha256 at most: 2^12 tries

# (args after the program, stdin text) -> (exit code, stdout, stderr). The stdin may hold a secret: never on a command line.
Runner = Callable[[list[str], str], "tuple[int | None, str, str]"]


class VaultOpenError(Exception):
    """A protected vault that can't be opened, with the reason in words (never a value)."""


class VaultIncomplete(VaultOpenError):
    """A chunked keychain vault with a missing chunk or a wrong checksum: never a partial vault."""


# ---- Windows DPAPI, through ctypes --------------------------------------------------------------------------------
def _blob_type():
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    return DataBlob


def _dpapi(protect: bool, data: bytes, entropy: bytes) -> bytes:
    """CryptProtectData / CryptUnprotectData for the current user, no UI, with `entropy`."""
    if sys.platform != "win32":
        raise VaultOpenError("Windows DPAPI is only on Windows")
    import ctypes
    from ctypes import wintypes
    DataBlob = _blob_type()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    func = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    func.argtypes = (ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.POINTER(DataBlob), ctypes.c_void_p,
                     ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob))
    func.restype = wintypes.BOOL
    in_buf = ctypes.create_string_buffer(data, len(data))
    ent_buf = ctypes.create_string_buffer(entropy, len(entropy))
    blob_in = DataBlob(len(data), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_char)))
    blob_ent = DataBlob(len(entropy), ctypes.cast(ent_buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DataBlob()
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    if not func(ctypes.byref(blob_in), None, ctypes.byref(blob_ent), None, None, CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(blob_out)):
        error = ctypes.get_last_error()
        if protect:
            raise VaultOpenError(f"Windows couldn't protect the data (error {error})")
        raise VaultOpenError("Windows couldn't decrypt it (it was protected for another user or another computer)")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


class Dpapi(Protocol):
    def protect(self, data: bytes, entropy: bytes) -> bytes: ...
    def unprotect(self, blob: bytes, entropy: bytes) -> bytes: ...


class CtypesDpapi:
    """The real DPAPI of this Windows user."""

    def protect(self, data: bytes, entropy: bytes) -> bytes:
        return _dpapi(True, data, entropy)

    def unprotect(self, blob: bytes, entropy: bytes) -> bytes:
        return _dpapi(False, blob, entropy)


# ---- the macOS keychain, through `security` -----------------------------------------------------------------------
def run_security(args: list[str], stdin: str) -> "tuple[int | None, str, str]":
    try:
        done = subprocess.run([SECURITY, *args], input=stdin, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None, "", "couldn't start"
    return done.returncode, done.stdout, done.stderr


def _quote(text: str) -> str:
    """An argument of one `security -i` line."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _all_hex(text: str) -> bool:
    return bool(text) and len(text) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in text)


def _manifest(text: str | None) -> "tuple[int, str | None] | None":
    """(chunk count, sha256) of a manifest; None when the text is a plain vault (no `chunks` key) or not JSON. A count
    that isn't a whole number from 1 to 100000 is 0 (unreadable)."""
    if text is None or not text.startswith("{"):
        return None
    try:
        top = json.loads(text)
    except ValueError:
        return None
    if not isinstance(top, dict) or "chunks" not in top:
        return None
    chunks, sha = top.get("chunks"), top.get("sha256")
    ok = isinstance(chunks, int) and not isinstance(chunks, bool) and 1 <= chunks <= 100_000
    return (chunks if ok else 0), (sha if isinstance(sha, str) else None)


@dataclass
class _Reading:
    raw: str
    decoded: str | None  # the other reading, when the item printed as all hex that is valid UTF-8


class Keychain:
    """The vault's generic passwords in the macOS keychain, through `/usr/bin/security` (an injected runner). The data
    goes in on stdin (`security -i`), never on a command line."""

    def __init__(self, run: Runner = run_security) -> None:
        self.run = run

    def _read_both(self, account: str) -> _Reading | None:
        code, out, _ = self.run(["find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"], "")
        if code != 0:
            return None
        text = out[:-1] if out.endswith("\n") else out
        text = text[:-1] if text.endswith("\r") else text
        if not text:
            return None
        if text.startswith("{") or not _all_hex(text):
            return _Reading(text, None)
        try:
            decoded = bytes.fromhex(text).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return _Reading(text, None)
        return _Reading(text, None if decoded == text else decoded)

    def _read_text(self, account: str) -> str | None:
        both = self._read_both(account)
        return None if both is None else (both.decoded if both.decoded is not None else both.raw)

    def read(self, account: str) -> str | None:
        """The stored vault JSON, or None when there is no entry. A chunked vault that can't be put back together
        raises VaultIncomplete."""
        text = self._read_text(account)
        manifest = _manifest(text)
        if manifest is None:
            return text
        count, sha = manifest
        if count == 0 or sha is None:
            raise VaultIncomplete("the vault in the keychain is incomplete (its chunk list is unreadable)")
        readings: list[_Reading] = []
        for i in range(1, count + 1):
            chunk = self._read_both(f"{account}#{i}")
            if chunk is None:
                raise VaultIncomplete(f"the vault in the keychain is incomplete (chunk {i} of {count} is missing)")
            readings.append(chunk)
        wanted = sha.lower()

        def matches(pieces: list[str]) -> str | None:
            whole = "".join(pieces)
            return whole if hashlib.sha256(whole.encode("utf-8")).hexdigest() == wanted else None
        found = matches([r.raw for r in readings]) or matches([r.decoded or r.raw for r in readings])
        ambiguous = [i for i, r in enumerate(readings) if r.decoded is not None]
        if found is None and len(ambiguous) <= MAX_AMBIGUOUS:
            for mask in range(1 << len(ambiguous)):
                pick = {i for bit, i in enumerate(ambiguous) if mask >> bit & 1}
                found = matches([r.decoded if i in pick and r.decoded is not None else r.raw
                                 for i, r in enumerate(readings)])
                if found is not None:
                    break
        if found is None:
            raise VaultIncomplete("the vault in the keychain is incomplete (its chunks don't match their checksum)")
        return found

    @staticmethod
    def _line(account: str, data: bytes) -> str:
        return f"add-generic-password -U -s {_quote(KEYCHAIN_SERVICE)} -a {_quote(account)} -X {data.hex()}"

    def _store(self, account: str, data: bytes) -> None:
        self.run(["-i"], self._line(account, data) + "\n")  # (-i doesn't always set the exit code: read back instead)

    def _drop(self, account: str, first: int, last: int) -> None:
        for i in range(first, last + 1):
            self.run(["delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", f"{account}#{i}"], "")

    def write(self, account: str, text: str) -> None:
        """Store the vault JSON: one item when it fits a line, else chunks #1…#n, then the manifest, then the stale
        chunks of an earlier, longer vault dropped. Each item is read back; raises VaultOpenError when one didn't keep."""
        try:
            previous = (_manifest(self._read_text(account)) or (0, None))[0]
        except VaultOpenError:
            previous = 0
        data = text.encode("utf-8")
        if len(self._line(account, data).encode("utf-8")) <= LINE_BUDGET:
            self._store(account, data)
            if self.read(account) != text:
                raise VaultOpenError("the keychain didn't keep the entry")
            self._drop(account, 1, previous)
            return
        size = (LINE_BUDGET - len(self._line(f"{account}#999999", b"").encode("utf-8"))) // 2 - 3
        if size < MIN_CHUNK:
            raise VaultOpenError("the vault file's path is too long to store in the macOS keychain")
        pieces: list[bytes] = []
        at = 0
        while at < len(data):
            end = min(at + size, len(data))
            while at < end < len(data) and data[end] & 0xC0 == 0x80:  # (cut at a character's start: each is UTF-8)
                end -= 1
            # a piece of only hex digits, in an even number, would read two ways: cut it one byte short
            if end < len(data) and end - at > 1 and (end - at) % 2 == 0 and _all_hex(data[at:end].decode("latin-1")):
                end -= 1
            pieces.append(data[at:end])
            at = end
        for i, piece in enumerate(pieces, 1):
            self._store(f"{account}#{i}", piece)
            back = self._read_both(f"{account}#{i}")
            want = piece.decode("utf-8")
            if back is None or (back.raw != want and back.decoded != want):
                raise VaultOpenError("the keychain didn't keep the entry")
        manifest = json.dumps({"chunks": len(pieces), "sha256": hashlib.sha256(data).hexdigest()},
                              separators=(",", ":"))
        self._store(account, manifest.encode("utf-8"))
        if self.read(account) != text:
            raise VaultOpenError("the keychain didn't keep the entry")
        self._drop(account, len(pieces) + 1, previous)


# ---- the file ------------------------------------------------------------------------------------------------------
@dataclass
class Deps:
    """The system's protection, injectable for tests."""
    platform: str = sys.platform
    dpapi: Dpapi | None = None
    keychain: Keychain | None = None

    def the_dpapi(self) -> Dpapi:
        return self.dpapi if self.dpapi is not None else CtypesDpapi()

    def the_keychain(self) -> Keychain:
        return self.keychain if self.keychain is not None else Keychain()


def protection_of(platform: str) -> str | None:
    """The protection this system has: DPAPI on Windows, the keychain on macOS, none elsewhere."""
    return DPAPI if platform == "win32" else KEYCHAIN if platform == "darwin" else None


def real_path(path: str) -> str:
    """The file's real path (symlinks resolved): the keychain item's account."""
    try:
        return os.path.realpath(os.path.abspath(path))
    except OSError:
        return os.path.abspath(path)


def protection(data: object) -> str | None:
    """How a parsed file says it is kept: its `protected` text, or None for a plain file."""
    if isinstance(data, dict) and isinstance(data.get("protected"), str):
        return data["protected"]
    return None


def open_protected(path: str, data: dict, deps: Deps | None = None) -> str:
    """The vault JSON a protected file stands for. Raises VaultOpenError with the reason, never a value."""
    deps = deps or Deps()
    how = data.get("protected")
    if how == DPAPI:
        if deps.platform != "win32":
            raise VaultOpenError("the file is protected with Windows DPAPI and can only be opened on Windows")
        blob = data.get("data")
        if not isinstance(blob, str) or not blob:
            raise VaultOpenError("the protected file holds no data")
        try:
            raw = base64.b64decode(blob, validate=True)
        except ValueError:
            raise VaultOpenError("the protected file's data isn't base64") from None
        try:
            return deps.the_dpapi().unprotect(raw, DPAPI_ENTROPY).decode("utf-8")
        except UnicodeDecodeError:
            raise VaultOpenError("what Windows decrypted isn't UTF-8 text") from None
    if how == KEYCHAIN:
        if deps.platform != "darwin":
            raise VaultOpenError("the file is protected with the macOS keychain and can only be opened on a Mac")
        found = deps.the_keychain().read(real_path(path))  # (VaultIncomplete goes up as it is: fail closed)
        if found is None:
            raise VaultOpenError("the keychain has no entry for this file's path (the file was moved, or it was "
                                 "protected by another user)")
        return found
    raise VaultOpenError(f'the file says it is protected as "{how}", which this Tapwright doesn\'t know')


def protected_file(how: str, text: str, deps: Deps, path: str) -> str:
    """The file's text for a vault JSON `text`, protected the way `how` says (the keychain item is written here)."""
    if how == DPAPI:
        blob = deps.the_dpapi().protect(text.encode("utf-8"), DPAPI_ENTROPY)
        return json.dumps({"version": 2, "protected": DPAPI, "data": base64.b64encode(blob).decode("ascii")})
    deps.the_keychain().write(real_path(path), text)
    return json.dumps({"version": 2, "protected": KEYCHAIN})


def _as_version2(top: dict) -> dict | None:
    """A plain file's content as a version 2 vault (a version 1 file's accounts become account entries, its `phone`
    a phone entry; other top-level keys, such as `otp`, travel along). None when it is neither."""
    if top.get("version") == 2 and isinstance(top.get("vault"), dict):
        return top
    if top.get("version") != 1 or not isinstance(top.get("accounts"), dict):
        return None
    rest = {k: v for k, v in top.items() if k not in ("version", "accounts")}
    vault: dict[str, object] = {}
    accounts = top["accounts"]
    for name, entry in accounts.items():
        fields = entry if isinstance(entry, dict) else {}
        if "passcode" in fields and "username" not in fields and "password" not in fields:
            vault[name] = {"kind": "phone", **fields}
            continue
        vault[name] = {"kind": "account", **{k: v for k, v in fields.items() if k != "passcode"}}
        if "passcode" in fields:  # (a version 2 account has no passcode: it moves into a phone entry of its own)
            phone, n = f"{name[:52]}_phone", 2
            while phone in accounts or phone in vault:
                phone, n = f"{name[:50]}_phone{n}", n + 1
            vault[phone] = {"kind": "phone", "passcode": fields["passcode"]}
    return {"version": 2, "vault": vault, **rest}


def protect(path: str, deps: Deps | None = None, check: Callable[[str], str | None] | None = None) -> tuple[bool, str]:
    """`tapwright vault protect FILE`: a plain vault file written in this system's protected form (a version 1 file
    becomes version 2). The plain file stays until the protected copy reads back and its sha256 matches; then it is
    replaced through a temporary file and a rename. `check(text)` says why a vault JSON wouldn't load (accounts.py), so a
    file that wouldn't load is never protected. (ok, the words to print)."""
    deps = deps or Deps()
    file = os.path.abspath(path)
    if not os.path.isfile(file):
        return False, f"the vault file {file} doesn't exist."
    how = protection_of(deps.platform)
    if how is None:
        return False, ("this system has no protection for the vault (Windows DPAPI or the macOS keychain); the file "
                       "stays plain, for test data only.")
    real = real_path(file)
    try:
        text = Path(real).read_text("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return False, f"couldn't read {file}."
    try:
        top = json.loads(text)
    except (ValueError, RecursionError):
        return False, f"the vault file {file} isn't valid JSON."  # (never json's own message: it quotes the text)
    if not isinstance(top, dict):
        return False, f"the vault file {file} isn't a vault."
    if protection(top) is not None:
        return False, f"the vault file {file} is already protected."
    payload = _as_version2(top)
    if payload is None:
        return False, f"the vault file {file} isn't a version 1 or 2 vault."
    plain = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if check is not None and (why := check(plain)) is not None:
        return False, f"the vault file {file} can't be protected as it is ({why}); fix it first. The file stays plain."
    try:
        out = protected_file(how, plain, deps, real)
        back = open_protected(real, json.loads(out), deps)
    except VaultOpenError as e:
        return False, f"couldn't protect the file: {e}. The file stays plain."
    except Exception as e:  # (any other failure leaves the plain file as it was)
        return False, f"couldn't protect the file: {type(e).__name__}. The file stays plain."
    if hashlib.sha256(back.encode("utf-8")).digest() != hashlib.sha256(plain.encode("utf-8")).digest():
        return False, "the protected copy didn't read back the same; the file stays plain."
    temp = os.path.join(os.path.dirname(real), f".{secrets.token_hex(6)}.vault.tmp")
    try:
        fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(out + "\n")
        os.replace(temp, real)
    except OSError as e:
        try:
            os.unlink(temp)
        except OSError:
            pass
        return False, f"couldn't write {file} ({e.strerror or 'an error'}); the file stays plain."
    if how == DPAPI:
        return True, f"Protected {file} with Windows DPAPI (current user). Tapline reads it too."
    return True, (f"Protected {file} in the macOS keychain (service {KEYCHAIN_SERVICE}; the entry belongs to this path, "
                  "so a moved file needs protecting again).")

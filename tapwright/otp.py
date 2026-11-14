"""One-time codes (docs/AGENT-TOOLS.md §20.4, §24.3, §18.11): `${otp.<app>}` is typed by the tool itself and never shown.

Tapwright reads a code from a notification of a source app the vault's `otp` setting names, and only for an app whose
setting is on (off by default). A code is valid once, and only within 2 minutes after the app showed a code field. It
is kept out of every sink of §20.3 (accounts.Store.remember_code) from the moment it is known.

The code comes from Notification Center (notifications.py): when a step types `${otp.<app>}`, or Tapline's host asks
`POST /otp` for that app, the daemon reads the notifications of the setting's SOURCE apps only, and takes the one code of
the newest that matches, arrived within 2 minutes and not before the app showed its code field (FieldWatch). The rules of
the link are kept exactly (§18.11), and checked in `serve`:
  - Tapline's host proves the daemon first, `POST /api/prove`, and only then, on that SAME keep-alive connection, sends
    `POST /otp {"app": "<app id>"}` with Tapwright's token. The daemon remembers which connections proved (the client's
    address and port: a new connection has another port), and answers `/otp` only on one of them.
  - The answer is `{"code", "expires", "source"}`: `source` is the bundle id the code was read from (§23's `otp` line
    uses it), or `404` when there is none."""
from __future__ import annotations

import dataclasses
import hashlib
import hmac
import os
import re
import secrets
import time
from pathlib import Path
from typing import Callable

WINDOW = 120.0  # seconds after the app showed a code field that a code may be used (§20.4)
# a handed-over code's mark is kept this long (Codex, PR #12 cycle 1, M4): longer than any notification that holds it can
# still count (2 minutes old at most, told to the minute), so a restarted daemon never hands it over again
USED_KEEP = 600.0
USED_DIR: Path | None = None  # where the marks are (None: ~/.tapwright/otp-used); tests point it elsewhere
PROVED_FOR = 30.0  # seconds a proved connection may ask (a keep-alive connection sends /otp right after the proof)
MAX_PROVED = 64


@dataclasses.dataclass
class Code:
    code: str = dataclasses.field(repr=False)
    source: str
    expires: float  # wall clock (time.time()) when it can no longer be used


def used_folder() -> Path:
    if USED_DIR is not None:
        return USED_DIR
    from . import store
    return store.HOME / "otp-used"


class Codes:
    """The codes read and not yet used, one per app: handed over once, then dropped. `remember` keeps each code out of
    text (the daemon's redactor) from the moment it is put here. A code handed over is never handed over again: a
    notification still in Notification Center would offer it again, after a restart too (Codex, PR #12 cycle 1, M4). So
    before a code is handed over or typed, its mark is made in `folder`: a file named by an HMAC of the code under a random
    key kept there (never the code, nor a plain hash of it, which the million codes would find again), created only if
    absent, so two processes can't both take it. A mark is kept USED_KEEP seconds. When the mark can't be made, the code
    isn't handed over (the safe side)."""

    def __init__(self, remember: Callable[[str], None] | None = None, clock: Callable[[], float] = time.time,
                 folder: Path | None = None) -> None:
        self._held: dict[str, Code] = {}
        self._used: set[str] = set()
        self._remember = remember
        self._clock = clock
        self._folder = folder
        self._key: bytes | None = None

    def put(self, app: str, code: str, source: str, field_shown_at: float) -> None:
        """A code read for `app` from `source`, for a code field the app showed at `field_shown_at` (wall clock)."""
        if self._remember is not None:
            self._remember(code)
        self._held[app] = Code(code, source, field_shown_at + WINDOW)

    def take(self, app: str) -> Code | None:
        """The code for `app`, once: dropped as it is handed over, and None once its 2 minutes are over, or when it was
        handed over before (by this process or an earlier one), or its mark can't be made."""
        found = self._held.pop(app, None)
        if found is None or self._clock() > found.expires or self.used(found.code):
            return None
        digest = self._digest(found.code)
        if digest is None or not self._mark(digest):
            return None
        self._used.add(digest)
        return found

    def used(self, code: str) -> bool:
        """Was this code handed over before? True when that can't be told (no key): the safe side."""
        digest = self._digest(code)
        if digest is None:
            return True
        if digest in self._used:
            return True
        path = self._where() / digest
        try:
            at = path.stat().st_mtime
        except FileNotFoundError:
            return False
        except OSError:
            return True
        return time.time() - at < USED_KEEP or not self._drop(path)

    # ---- the marks on disk (M4) ----
    def _where(self) -> Path:
        return self._folder if self._folder is not None else used_folder()

    def _digest(self, code: str) -> str | None:
        key = self._secret()
        return None if key is None else hmac.new(key, code.encode("utf-8"), hashlib.sha256).hexdigest()[:40]

    def _secret(self) -> bytes | None:
        """The marks' random key, made once (readable by this user only), shared by every daemon on this PC."""
        if self._key is not None:
            return self._key
        path = self._where() / "key"
        for _ in range(2):
            try:
                text = path.read_text(encoding="ascii").strip()
            except FileNotFoundError:
                text = None
            except (OSError, UnicodeDecodeError):
                return None
            if text is not None:
                if not re.fullmatch(r"[0-9a-f]{64}", text):
                    return None
                self._key = bytes.fromhex(text)
                return self._key
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue  # another process made it a moment ago: read that one
            except OSError:
                return None
            key = secrets.token_hex(32)
            with os.fdopen(fd, "w", encoding="ascii") as f:
                f.write(key + "\n")
            self._key = bytes.fromhex(key)
            return self._key
        return None

    def _mark(self, digest: str) -> bool:
        """Make the code's mark, only if it isn't there (O_EXCL: of two processes, one makes it). Old marks go first."""
        folder = self._where()
        self._prune(folder)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            fd = os.open(folder / digest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError:  # there already (handed over by another process), or it can't be written
            return False
        os.close(fd)
        return True

    def _prune(self, folder: Path) -> None:
        try:
            marks = [p for p in folder.iterdir() if p.name != "key"]
        except OSError:
            return
        now = time.time()
        for p in marks:
            try:
                if now - p.stat().st_mtime >= USED_KEEP:
                    p.unlink()
            except OSError:
                pass

    @staticmethod
    def _drop(path: Path) -> bool:
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False


class FieldWatch:
    """When each app (with codes on) showed its code field (§20.4: a code counts only within 2 minutes after): the first
    read that shows one starts the time, and a read of that app without one clears it, so a field shown again starts
    again. Wall clock."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._at: dict[str, float] = {}
        self._clock = clock

    def seen(self, app: str, showing: bool) -> None:
        if showing:
            self._at.setdefault(app, self._clock())
        else:
            self._at.pop(app, None)

    def shown_at(self, app: str) -> float | None:
        return self._at.get(app)


class Proved:
    """The connections that proved the daemon (`/api/prove`) a moment ago, by the client's (address, port): `/otp` is
    answered only on one of them (§18.11: the token goes only over the connection the proof came on)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._at: dict[tuple[str, int], float] = {}
        self._clock = clock

    def mark(self, client: object) -> None:
        key = _key(client)
        if key is None:
            return
        now = self._clock()
        for old in [k for k, t in self._at.items() if now - t > PROVED_FOR]:
            del self._at[old]
        if len(self._at) >= MAX_PROVED:
            del self._at[min(self._at, key=self._at.__getitem__)]
        self._at[key] = now

    def holds(self, client: object) -> bool:
        key = _key(client)
        at = self._at.get(key) if key is not None else None
        return at is not None and self._clock() - at <= PROVED_FOR


def _key(client: object) -> tuple[str, int] | None:
    if isinstance(client, (tuple, list)) and len(client) == 2 and isinstance(client[1], int):
        return str(client[0]), client[1]
    return None


def answer(found: Code) -> dict:
    """The `/otp` answer for a code (§18.11): `expires` in epoch ms."""
    return {"code": found.code, "expires": int(found.expires * 1000), "source": found.source}

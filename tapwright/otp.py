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
import time
from typing import Callable

WINDOW = 120.0  # seconds after the app showed a code field that a code may be used (§20.4)
PROVED_FOR = 30.0  # seconds a proved connection may ask (a keep-alive connection sends /otp right after the proof)
MAX_PROVED = 64


@dataclasses.dataclass
class Code:
    code: str = dataclasses.field(repr=False)
    source: str
    expires: float  # wall clock (time.time()) when it can no longer be used


class Codes:
    """The codes read and not yet used, one per app: handed over once, then dropped. `remember` keeps each code out of
    text (the daemon's redactor) from the moment it is put here. A code handed over is never handed over again (its
    digest is kept for the daemon's life: a notification still in Notification Center would offer it again)."""

    def __init__(self, remember: Callable[[str], None] | None = None, clock: Callable[[], float] = time.time) -> None:
        self._held: dict[str, Code] = {}
        self._used: set[str] = set()
        self._remember = remember
        self._clock = clock

    def put(self, app: str, code: str, source: str, field_shown_at: float) -> None:
        """A code read for `app` from `source`, for a code field the app showed at `field_shown_at` (wall clock)."""
        if self._remember is not None:
            self._remember(code)
        self._held[app] = Code(code, source, field_shown_at + WINDOW)

    def take(self, app: str) -> Code | None:
        """The code for `app`, once: dropped as it is handed over, and None once its 2 minutes are over."""
        found = self._held.pop(app, None)
        if found is None or self._clock() > found.expires or self.used(found.code):
            return None
        self._used.add(_digest(found.code))
        return found

    def used(self, code: str) -> bool:
        return _digest(code) in self._used


def _digest(code: str) -> str:
    return hashlib.sha256(("tapwright-otp\0" + code).encode()).hexdigest()


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

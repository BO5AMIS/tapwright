"""The passcode unlock (AGENT-TOOLS §18.6, §11.5, §24.5; Tapwright 1.7).

When a call needs the phone and it is locked, and the vault gives `phone.passcode`, the daemon unlocks it by itself:
it wakes the screen, proves that iOS's own lock screen is in front with its passcode entry showing, types the passcode
there, and checks that the phone is unlocked. The rules, each tested (tests/test_unlock.py):

  - **Only on the lock screen.** Before a key goes out, all of these hold: WebDriverAgent says the phone is locked; the
    app in front is SpringBoard; its tree shows the passcode entry (all ten digit keys of the keypad, each found once,
    or a secure passcode field); nothing on it mentions XCTest; and iOS's XCTest prompt (LocalAuthenticationUIService,
    "Enter iPhone Passcode for XCTest") is not up. Anything else: nothing is typed.
  - **Never the XCTest prompt.** That prompt is iOS's consent to UI automation, not the lock screen. §18.6 says the
    passcode is typed "only on the lock screen's keypad", and §11.5 lists the XCTest prompt as a moment for a person, so
    the tool never types the passcode there: it answers `locked` for the owner (Settings > Developer > Enable UI
    Automation stops the prompt).
  - **Never shown.** The passcode is never in an answer, an error, a log line, the trace, a caption or a report: the
    results below carry fixed words only, a failure's own message is dropped (a device error could echo what was sent),
    and the keypad's touches are sent straight to WebDriverAgent (not through the agent's touch path, its log or the
    `last_point` a later answer could name). The audit log gets `vault` with the name `phone.passcode`, never a value.
  - **At most 2 attempts an hour, and none after a refusal.** An attempt is a passcode typed. After one that leaves the
    phone locked, no further attempt is made with that passcode (`locked: the passcode in the file was refused`): iOS
    delays the lock screen after a few wrong ones, and a loop must never get there. A changed passcode in the file may
    be tried (only a SHA-256 of the refused one is kept, in memory).

The phone side (`PhoneLockScreen`) is one adapter over Session; the logic (`Unlocker`) takes any object with the same
methods, so the tests drive it with a fake.
"""
from __future__ import annotations

import asyncio
import collections
import hashlib
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from . import ui

SPRINGBOARD = "com.apple.springboard"
LIMIT = 2  # passcode attempts ...
WINDOW = 3600.0  # ... per this many seconds
CONFIRM_WAIT = 6.0  # seconds the phone gets to say it unlocked after the last key
CONFIRM_POLL = 0.3
KEY_GAP = 0.08  # seconds between two keypad touches
DIGITS = "0123456789"

# What a result says. Fixed words: never anything a device or the phone sent back, never the passcode.
REFUSED = "the passcode in the file was refused"
NO_PASSCODE = "the phone is locked; ask the user to unlock it"
LIMITED = ("the phone is locked, and the passcode unlock was already tried twice this hour (at most 2 an hour, so iOS's "
           "lockout delays are never reached); ask the user to unlock it")
NOT_PROVEN = ("the phone is locked, and its lock screen's passcode entry couldn't be found, so the passcode was not "
              "typed; ask the user to unlock it")
XCTEST = ("the phone is asking for its passcode to allow UI automation (the XCTest prompt); the tool never types the "
          "passcode there: ask the user to enter it on the phone (Settings > Developer > Enable UI Automation stops it "
          "asking)")
FAILED = "typing the passcode on the lock screen didn't finish; ask the user to unlock the phone"
LETTERS_ON_KEYPAD = ("the phone is locked, and its lock screen shows a number keypad, but the passcode in the file has "
                     "other characters; nothing was typed. Ask the user to unlock it")
AUDIT_NAME = "phone.passcode"


@dataclass(frozen=True)
class Result:
    ok: bool  # the phone is unlocked now (or wasn't locked)
    code: str  # unlocked, not_locked, no_passcode, refused, limit, not_proven, xctest_prompt, failed, busy
    message: str = ""  # fixed words for an answer: never a value

    @property
    def typed(self) -> bool:
        return self.code in ("unlocked", "refused", "failed")


@dataclass(frozen=True)
class Entry:
    """The lock screen's passcode entry, as found in SpringBoard's tree."""
    keys: dict[str, tuple[float, float]]  # digit -> where its key's centre is (all ten, or none)
    field: ui.Element | None  # the secure passcode field, when there is one


class LockScreen(Protocol):
    async def locked(self) -> bool: ...
    async def wake(self) -> None: ...  # wake the screen (and bring the passcode entry up where a press does)
    async def reveal(self) -> None: ...  # a swipe up from the bottom edge: the passcode entry of a Face ID phone
    async def lock_tree(self) -> tuple[ui.Tree, str | None]: ...  # SpringBoard's tree now, and the app in front
    async def xctest_prompt(self) -> bool | None: ...  # is iOS's XCTest passcode prompt up (None: can't say)
    async def press_keys(self, points: list[tuple[float, float]]) -> None: ...
    async def type_secret(self, text: str) -> None: ...


def _key_of(e: ui.Element) -> str | None:
    """The digit a keypad key types: a button named or labelled with the digit (iOS labels them "2" or "2, ABC")."""
    if e.role != "button" and e.type != "Button":
        return None
    for said in (e.identifier, e.name, e.label):
        text = (said or "").strip()
        if len(text) == 1 and text in DIGITS:
            return text
        if len(text) > 1 and text[0] in DIGITS and text[1] in ", " and not any(c in DIGITS for c in text[1:]):
            return text[0]
    return None


def _mentions_xctest(e: ui.Element) -> bool:
    return any("xctest" in (s or "").casefold() for s in (e.label, e.identifier, e.name, e.value))


def _passcode_field(e: ui.Element) -> bool:
    secure = e.secure or e.type == "SecureTextField"
    return secure and any("passcode" in (s or "").casefold() for s in (e.label, e.identifier, e.name, e.placeholder))


def find_entry(tree: ui.Tree, front: str | None) -> Entry | None:
    """The passcode entry iOS's lock screen shows, or None when this screen isn't proven to be it: SpringBoard in front,
    no alert, nothing about XCTest, and the keypad's ten keys (each once) or a secure passcode field."""
    if front != SPRINGBOARD:
        return None
    if tree.alert is not None:
        return None
    items = [*tree.items, *tree.offscreen]
    if any(_mentions_xctest(e) for e in items):
        return None
    found: dict[str, list[ui.Element]] = collections.defaultdict(list)
    for e in tree.items:
        digit = _key_of(e)
        if digit is not None and e.enabled and not e.in_alert:
            found[digit].append(e)
    keypad = all(len(found.get(d, [])) == 1 for d in DIGITS)
    keys = {d: found[d][0].center for d in DIGITS} if keypad else {}
    fields = [e for e in tree.items if _passcode_field(e) and not e.in_alert]
    field = fields[0] if len(fields) == 1 else None
    if not keys and field is None:
        return None
    return Entry(keys, field)


def _digest(passcode: str) -> str:
    return hashlib.sha256(passcode.encode("utf-8")).hexdigest()


class Unlocker:
    """The passcode unlock's rules: what may be typed, when, and how often. One per daemon."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, limit: int = LIMIT, window: float = WINDOW,
                 confirm_wait: float = CONFIRM_WAIT, poll: float = CONFIRM_POLL,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, key_gap: float = KEY_GAP) -> None:
        self.clock, self.limit, self.window = clock, limit, window
        self.confirm_wait, self.poll, self.sleep, self.key_gap = confirm_wait, poll, sleep, key_gap
        self._tries: collections.deque[float] = collections.deque()
        self._refused: str | None = None  # SHA-256 of a passcode the phone refused (in memory only)
        self._lock = asyncio.Lock()

    def tries_left(self) -> int:
        now = self.clock()
        while self._tries and now - self._tries[0] >= self.window:
            self._tries.popleft()
        return max(0, self.limit - len(self._tries))

    def refused(self, passcode: str | None) -> bool:
        return bool(passcode) and self._refused == _digest(passcode or "")

    async def unlock(self, phone: LockScreen, passcode: str | None,
                     audit: Callable[[str], object] | None = None) -> Result:
        """Unlock the phone with `passcode` when every rule allows it. `audit(detail)` writes the `vault` line."""
        async with self._lock:  # (two calls at once: the second finds the phone unlocked, or the first's refusal)
            return await self._unlock(phone, passcode, audit)

    async def _unlock(self, phone: LockScreen, passcode: str | None, audit: Callable[[str], object] | None) -> Result:
        if not await phone.locked():
            return Result(True, "not_locked")
        if not passcode:
            return Result(False, "no_passcode", NO_PASSCODE)
        if self.refused(passcode):
            return Result(False, "refused", REFUSED)
        if self.tries_left() <= 0:
            return Result(False, "limit", LIMITED)
        if await phone.xctest_prompt():
            return Result(False, "xctest_prompt", XCTEST)
        try:
            await phone.wake()
        except Exception:
            pass  # (WebDriverAgent's unlock answers an error on a phone with a passcode, after it woke the screen)
        if not await phone.locked():
            return Result(True, "not_locked")  # (woken into an unlocked phone)
        entry = await self._entry(phone)
        if entry is None:
            try:
                await phone.reveal()
            except Exception:
                return Result(False, "not_proven", NOT_PROVEN)
            entry = await self._entry(phone)
        if entry is None:
            return Result(False, "not_proven", NOT_PROVEN)
        digits = passcode.isascii() and passcode.isdigit()
        if not entry.keys and digits and entry.field is None:
            return Result(False, "not_proven", NOT_PROVEN)
        if not digits and entry.field is None:
            return Result(False, "not_proven", LETTERS_ON_KEYPAD)
        # the last checks, right before the keys: still locked, and still no XCTest prompt
        if not await phone.locked():
            return Result(True, "not_locked")
        if await phone.xctest_prompt():
            return Result(False, "xctest_prompt", XCTEST)
        self._tries.append(self.clock())
        try:
            if digits and entry.keys:
                points = [entry.keys[c] for c in passcode]
                await phone.press_keys(points)
            else:
                await phone.type_secret(passcode)
        except Exception:  # its message may hold what was sent: never passed on
            self._refused = _digest(passcode)  # it may have counted as a wrong one on the phone: no second try
            self._audit(audit, "typed on the lock screen; it didn't finish")
            return Result(False, "failed", FAILED)
        if await self._unlocked(phone):
            self._audit(audit, "typed on the lock screen; unlocked")
            return Result(True, "unlocked")
        self._refused = _digest(passcode)
        self._audit(audit, "typed on the lock screen; refused")
        return Result(False, "refused", REFUSED)

    async def _entry(self, phone: LockScreen) -> Entry | None:
        try:
            tree, front = await phone.lock_tree()
        except Exception:
            return None
        return find_entry(tree, front)

    async def _unlocked(self, phone: LockScreen) -> bool:
        deadline = self.clock() + self.confirm_wait
        while True:
            try:
                if not await phone.locked():
                    return True
            except Exception:
                pass
            if self.clock() >= deadline:
                return False
            await self.sleep(self.poll)

    @staticmethod
    def _audit(audit: Callable[[str], object] | None, detail: str) -> None:
        if audit is None:
            return
        try:
            audit(detail)
        except Exception:
            pass


class PhoneLockScreen:
    """The lock screen through a Session (WebDriverAgent). The keys go straight to WebDriverAgent's W3C actions: not
    through Session.tap (whose answer names the key, and which sets `last_point`), not through the agent's touch hook."""

    def __init__(self, session, key_gap: float = KEY_GAP, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.session, self.key_gap, self.sleep = session, key_gap, sleep

    async def locked(self) -> bool:
        return bool(await self.session.locked())

    async def wake(self) -> None:
        # WebDriverAgent's unlock: wakes the screen and presses home (the passcode keypad on a Touch ID phone); on a
        # phone with a passcode it answers an error after it has woken the screen
        await self.session._call("POST", "/wda/unlock", {}, timeout=15)

    async def reveal(self) -> None:
        w, h = self.session.screen or (390.0, 844.0)
        await self.session._call("POST", "/session/{sid}/actions", ui.swipe(w / 2, h - 4, w / 2, h * 0.35, 300),
                                 timeout=10)
        await self.sleep(0.8)

    async def lock_tree(self) -> tuple[ui.Tree, str | None]:
        tree = await self.session.refresh(timeout=10)
        front = tree.app[1] if tree.app else None
        if front is None:
            front = await self.session.foreground_bundle_id()
        return tree, front

    async def xctest_prompt(self) -> bool | None:
        ask = getattr(self.session.device, "passcode_prompt_up", None)
        return await ask() if ask is not None else None

    async def press_keys(self, points: list[tuple[float, float]]) -> None:
        for i, (x, y) in enumerate(points):
            if i:
                await self.sleep(self.key_gap)
            await self.session._call("POST", "/session/{sid}/actions", ui.tap(x, y, 60), timeout=10)

    async def type_secret(self, text: str) -> None:
        await self.session._call("POST", "/session/{sid}/wda/keys", {"value": list(text) + ["\n"]}, timeout=15)


def schedule_hook(schedule: object) -> bool:
    """The scheduled run's `Deps.unlock_phone` (schedulerun.py, §22.3): ask the daemon to unlock with the vault file the
    schedule names. True when the phone is unlocked now; the run then looks at the phone again. The run never sees the
    passcode: only the file's path travels, and the daemon types it."""
    from . import client
    accounts = getattr(schedule, "accounts", None)
    try:
        answer = client.unlock_phone(accounts if isinstance(accounts, str) and accounts else None)
    except Exception:
        return False
    return isinstance(answer, dict) and answer.get("ok") is True

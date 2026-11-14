"""The passcode unlock (AGENT-TOOLS §18.6, §11.5, §19.5, §24.5; Tapwright 1.7).

When the phone is locked and the vault that `tapwright serve` was started with (`--accounts FILE`) gives
`phone.passcode`, the daemon can unlock it by itself: it wakes the screen, proves that iOS's own lock screen is in front
with its passcode entry showing, types the passcode there, and checks that the phone is unlocked. The rules, each tested
(tests/test_unlock.py, tests/test_unlock_daemon.py):

  - **Only where unattended running is meant to happen.** At the start of a scheduled run (`/api/phone/unlock`, from
    `tapwright run-schedule`), and with `tapwright serve --phone` at the start of a new top-level call. Never in the
    middle of a `run`, `drive`, `replay`, route or `journey`: locking the phone is how the owner stops the agent
    (AGENTS.md, §19.5), so a lock there ends it with `locked`. With `--apps` (personal mode) or `--dedicated`, a locked
    phone answers `locked` as in 1.6: a person unlocks it.
  - **Only serve's own vault.** The passcode is read only from the file `tapwright serve --accounts` names, never from a
    file that a request or a call names.
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
    It is not in the text redactor either: a 4 to 6 digit passcode replaced in answers would turn every date and price
    into a test of it, and it is never typed anywhere text can show it (the lock screen shows dots).
  - **At most 2 attempts an hour and 3 a day, across processes, and none after a refusal.** An attempt is a passcode
    typed. The attempts and the refusal are kept on disk (`~/.tapwright/unlock-state.json`, written whole and changed
    under a lock), so a new daemon (the last one stopped when idle, or was shut down) knows them. After an attempt that
    leaves the phone locked, nothing more is typed (`locked: the passcode in the file was refused`): iOS delays the lock
    screen after a few wrong ones, and erases the phone after ten when "Erase Data" is on. The record keeps nothing of
    the passcode, not even a hash: the refusal is tied to the vault file (its path, modification time and size). A
    changed file may be tried once; after that nothing is typed until a person runs `tapwright unlock-reset`, which
    clears the refusal and leaves the counts.

The phone side (`PhoneLockScreen`) is one adapter over Session; the logic (`Unlocker`) takes any object with the same
methods, so the tests drive it with a fake.
"""
from __future__ import annotations

import asyncio
import collections
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from . import pidlock, ui

SPRINGBOARD = "com.apple.springboard"
LIMIT = 2  # passcode attempts ...
WINDOW = 3600.0  # ... per this many seconds
DAY_LIMIT = 3  # and at most this many ...
DAY = 86400.0  # ... per this many seconds, whichever process typed them
PENDING_FOR = 120.0  # seconds an attempt under way (in any process) holds off another
LOCK_WAIT = 5.0  # seconds a change of the record waits for the record's lock; then nothing is typed
CONFIRM_WAIT = 6.0  # seconds the phone gets to say it unlocked after the last key
CONFIRM_POLL = 0.3
KEY_GAP = 0.08  # seconds between two keypad touches
DIGITS = "0123456789"
STATE_FILE: Path | None = None  # the record of attempts; None: ~/.tapwright/unlock-state.json (the tests set their own)
RESET = "tapwright unlock-reset"

# What a result says. Fixed words: never anything a device or the phone sent back, never the passcode.
REFUSED = "the passcode in the file was refused"
REFUSED_HELD = (f"the phone is locked, and the passcode unlock stopped after a refused passcode: nothing is typed until "
                f"a person checks phone.passcode in the vault and runs `{RESET}`. Ask the user to unlock it")
NO_PASSCODE = "the phone is locked; ask the user to unlock it"
LIMITED = ("the phone is locked, and the passcode unlock was already tried twice this hour (at most 2 an hour, so iOS's "
           "lockout delays are never reached); ask the user to unlock it")
LIMITED_DAY = ("the phone is locked, and the passcode unlock was already tried 3 times today (at most 3 a day); ask the "
               "user to unlock it")
NO_RECORD = (f"the phone is locked, and the passcode unlock's record of attempts can't be read or written, so nothing "
             f"was typed; ask the user to unlock it (`{RESET}` starts a new record)")
UNDER_WAY = "the passcode unlock is under way in another process; try again in a moment"
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
    code: str  # unlocked, not_locked, no_passcode, refused, limit, not_proven, xctest_prompt, failed, busy, no_record
    message: str = ""  # fixed words for an answer: never a value

    @property
    def typed(self) -> bool:
        return self.code in ("unlocked", "refused", "failed")


@dataclass(frozen=True)
class Entry:
    """The lock screen's passcode entry, as found in SpringBoard's tree."""
    keys: dict[str, tuple[float, float]]  # digit -> where its key's centre is (all ten, or none)
    field: ui.Element | None  # the secure passcode field, when there is one


@dataclass(frozen=True)
class Source:
    """The vault file a passcode came from, as the record ties a refusal to it: its real path, modification time and
    size. Never the passcode, nor a hash of it (the hash of a 4 to 6 digit passcode is found by trying them all)."""
    path: str
    mtime_ns: int
    size: int

    @classmethod
    def of(cls, path: str | None) -> "Source":
        """The file as it is now; a file that can't be looked at is "no file", which a refusal latches as well."""
        if not path:
            return NO_SOURCE
        try:
            real = os.path.realpath(path)
            st = os.stat(real)
        except (OSError, ValueError):
            return NO_SOURCE
        return cls(os.path.normcase(real), int(st.st_mtime_ns), int(st.st_size))

    def as_json(self) -> dict:
        return {"path": self.path, "mtime_ns": self.mtime_ns, "size": self.size}


NO_SOURCE = Source("", 0, 0)


def state_file() -> Path:
    if STATE_FILE is not None:
        return Path(STATE_FILE)
    from . import store
    return store.HOME / "unlock-state.json"


def _fresh() -> dict:
    return {"version": 1, "tries": [], "refused": None, "retried": False, "pending": None}


def _number(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


class Ledger:
    """The record of passcode attempts, on disk and shared by every process of this user (review 1.7, H2):
    `{"version": 1, "tries": [unix times], "refused": {"path", "mtime_ns", "size"} or null, "retried": bool,
    "pending": unix time or null}`. Read whole (a reader never sees half a write: it is written to a temporary file and
    renamed), and changed only under `<file>.lock` (pidlock, as the policy edit lock). A record that can't be read stops
    the unlock (`no_record`) until a person runs `tapwright unlock-reset`."""

    def __init__(self, path: Path | str | None = None, wall: Callable[[], float] = time.time,
                 lock_wait: float = LOCK_WAIT, lock_deps: "pidlock.Deps | None" = None) -> None:
        self._given = Path(path) if path is not None else None
        self.wall, self.lock_wait, self.lock_deps = wall, lock_wait, lock_deps

    @property
    def path(self) -> Path:
        return self._given if self._given is not None else state_file()

    # ---- reading and writing ----
    def read(self) -> dict | None:
        """The record; a fresh one when there is no file; None when it can't be read or isn't one."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _fresh()
        except (OSError, UnicodeDecodeError):
            return None
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            return None
        if not isinstance(data, dict) or data.get("version") != 1:
            return None
        tries, refused, pending = data.get("tries"), data.get("refused"), data.get("pending")
        if not isinstance(tries, list) or not all(_number(t) for t in tries):
            return None
        if refused is not None and not (isinstance(refused, dict) and isinstance(refused.get("path"), str)
                                        and isinstance(refused.get("mtime_ns"), int)
                                        and isinstance(refused.get("size"), int)):
            return None
        if pending is not None and not _number(pending):
            return None
        return {"version": 1, "tries": [float(t) for t in tries],
                "refused": None if refused is None else
                {"path": refused["path"], "mtime_ns": refused["mtime_ns"], "size": refused["size"]},
                "retried": data.get("retried") is True, "pending": None if pending is None else float(pending)}

    def _write(self, data: dict) -> bool:
        path = self.path
        temp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
        try:
            fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(data) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            _unlink(temp)
            return False
        for attempt in range(40):  # (on Windows a reader holding the file open this moment makes the rename fail)
            try:
                os.replace(temp, path)
                return True
            except PermissionError:
                if attempt == 39:
                    break
                time.sleep(0.005)
            except OSError:
                break
        _unlink(temp)
        return False

    async def _change(self, change: Callable[[dict | None], "tuple[dict | None, Result | None]"]) -> Result | None:
        """Read, change and write the record under its lock: `change(record)` gives (what to write, or None for
        nothing; the result). No lock within `lock_wait`: `busy`, nothing changed. A write that fails: `no_record`."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return Result(False, "no_record", NO_RECORD)
        lock = f"{self.path}.lock"
        waited = 0.0
        while (token := pidlock.take(lock, self.lock_deps)) is None:
            if waited >= self.lock_wait:
                return Result(False, "busy", UNDER_WAY)
            await asyncio.sleep(0.05)
            waited += 0.05
        try:
            data, result = change(self.read())
            if data is not None and not self._write(data):
                return Result(False, "no_record", NO_RECORD)
            return result
        finally:
            pidlock.release(lock, token)

    # ---- the rules ----
    def _counted(self, tries: list[float], now: float, span: float) -> int:
        # a time ahead of now (the clock went back) counts as within every span: it fails closed
        return sum(1 for t in tries if now - t < span)

    def _verdict(self, data: dict | None, source: Source) -> Result | None:
        """Why nothing may be typed now with the passcode from `source`, or None."""
        if data is None:
            return Result(False, "no_record", NO_RECORD)
        now = self.wall()
        pending = data.get("pending")
        if pending is not None and now - pending < PENDING_FOR:
            return Result(False, "busy", UNDER_WAY)
        refused = data.get("refused")
        if refused is not None:
            if data.get("retried"):
                return Result(False, "refused", REFUSED_HELD)  # a changed file had its one try: a person clears it
            if refused == source.as_json():
                return Result(False, "refused", REFUSED)
        if self._counted(data["tries"], now, WINDOW) >= LIMIT:
            return Result(False, "limit", LIMITED)
        if self._counted(data["tries"], now, DAY) >= DAY_LIMIT:
            return Result(False, "limit", LIMITED_DAY)
        return None

    def peek(self, source: Source) -> Result | None:
        """The verdict without the lock, before the phone is woken; `begin` decides again under the lock."""
        return self._verdict(self.read(), source)

    def left(self) -> int:
        """Attempts left now (the fewer of this hour's and today's); 0 when the record can't be read."""
        data = self.read()
        if data is None:
            return 0
        now = self.wall()
        return max(0, min(LIMIT - self._counted(data["tries"], now, WINDOW),
                          DAY_LIMIT - self._counted(data["tries"], now, DAY)))

    async def begin(self, source: Source) -> Result | None:
        """Right before the first key: decide again under the lock and, when it may go on, write down the attempt AND
        the refusal it may turn out to be, so a process that dies while typing leaves the latch set (the keys may have
        counted on the phone). None: type now; else why not (nothing is typed)."""
        def change(data: dict | None) -> "tuple[dict | None, Result | None]":
            why = self._verdict(data, source)
            if why is not None or data is None:
                return None, why
            now = self.wall()
            tries = [t for t in data["tries"] if now - t < DAY] + [now]
            return {"version": 1, "tries": tries, "refused": source.as_json(),
                    "retried": data.get("refused") is not None, "pending": now}, None
        return await self._change(change)

    async def end(self, unlocked: bool) -> None:
        """After the attempt: unlocked clears the refusal (the file's passcode is the phone's); else the latch `begin`
        set stays."""
        def change(data: dict | None) -> "tuple[dict | None, Result | None]":
            if data is None:
                return None, None
            if unlocked:
                return {**data, "refused": None, "retried": False, "pending": None}, None
            return {**data, "pending": None}, None
        await self._change(change)

    async def reset(self) -> tuple[bool, str]:
        """`tapwright unlock-reset`, a person's command: the refusal is cleared, so the vault's passcode may be tried
        again; the counts (2 an hour, 3 a day) still hold. A record that can't be read is started again."""
        def change(data: dict | None) -> "tuple[dict | None, Result | None]":
            if data is None:
                return _fresh(), Result(True, "new")
            was = data.get("refused") is not None or data.get("pending") is not None
            return {**data, "refused": None, "retried": False, "pending": None}, Result(True, "cleared" if was else "none")
        result = await self._change(change)
        if result is None or not result.ok:
            why = f" ({result.message})" if result is not None and result.message else ""
            return False, f"couldn't change the passcode unlock's record at {self.path}{why}."
        if result.code == "new":
            return True, f"The passcode unlock's record at {self.path} couldn't be read; it was started again."
        if result.code == "none":
            return True, "The passcode unlock wasn't stopped by a refused passcode; nothing to clear."
        return True, ("Cleared: the passcode unlock may type the vault's phone.passcode again (at most 2 an hour and 3 a "
                      "day). Check first that it is the phone's passcode: each wrong one counts on the phone.")

    def stopped(self) -> bool:
        """Is the unlock stopped by a refusal, or by a record that can't be read (for `doctor --unattended`)?"""
        data = self.read()
        return data is None or data.get("refused") is not None


def _unlink(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


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


class Unlocker:
    """The passcode unlock's rules: what may be typed, when, and how often. One per daemon; the counts and the refusal
    are the Ledger's, on disk, shared with every other process."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, confirm_wait: float = CONFIRM_WAIT,
                 poll: float = CONFIRM_POLL, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 key_gap: float = KEY_GAP, ledger: Ledger | None = None) -> None:
        self.clock = clock
        self.confirm_wait, self.poll, self.sleep, self.key_gap = confirm_wait, poll, sleep, key_gap
        self.ledger = ledger if ledger is not None else Ledger()
        self._lock = asyncio.Lock()

    def tries_left(self) -> int:
        return self.ledger.left()

    async def unlock(self, phone: LockScreen, passcode: str | None, audit: Callable[[str], object] | None = None,
                     source: Source = NO_SOURCE) -> Result:
        """Unlock the phone with `passcode` (from the vault file `source`) when every rule allows it. `audit(detail)`
        writes the `vault` line."""
        async with self._lock:  # (two calls at once: the second finds the phone unlocked, or the first's refusal)
            return await self._unlock(phone, passcode, audit, source)

    async def _unlock(self, phone: LockScreen, passcode: str | None, audit: Callable[[str], object] | None,
                      source: Source) -> Result:
        if not await phone.locked():
            return Result(True, "not_locked")
        if not passcode:
            return Result(False, "no_passcode", NO_PASSCODE)
        if (why := self.ledger.peek(source)) is not None:
            return why
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
        if (why := await self.ledger.begin(source)) is not None:  # counted, and latched, before the first key
            return why
        try:
            if digits and entry.keys:
                points = [entry.keys[c] for c in passcode]
                await phone.press_keys(points)
            else:
                await phone.type_secret(passcode)
        except Exception:  # its message may hold what was sent: never passed on
            await self.ledger.end(False)  # it may have counted as a wrong one on the phone: no second try
            self._audit(audit, "typed on the lock screen; it didn't finish")
            return Result(False, "failed", FAILED)
        unlocked = await self._unlocked(phone)
        await self.ledger.end(unlocked)
        if unlocked:
            self._audit(audit, "typed on the lock screen; unlocked")
            return Result(True, "unlocked")
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
    """The scheduled run's `Deps.unlock_phone` (schedulerun.py, §22.3): ask the daemon to unlock the phone. True when the
    phone is unlocked now; the run then looks at the phone again. The run never sees the passcode: only the file's path
    travels, and the daemon types the passcode only from its own `--accounts` file (a schedule naming another file is
    refused)."""
    from . import client
    accounts = getattr(schedule, "accounts", None)
    try:
        answer = client.unlock_phone(accounts if isinstance(accounts, str) and accounts else None)
    except Exception:
        return False
    return isinstance(answer, dict) and answer.get("ok") is True

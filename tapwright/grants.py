"""Grants (docs/AGENT-TOOLS.md §19): a person's standing OK for a kind of touch, with limits, given ahead of time.

A grant lives in the policy file (version 2). Its uses are slot files next to the policy file (§19.4):
    <policy file>.uses/<grant name, ASCII lower-cased>/<YYYY-MM-DD>/<k>       the day, k = 1..perDay
    <policy file>.uses/<grant name, ASCII lower-cased>/<YYYY-MM-DDTHH>/h<k>   the clock hour, k = 1..perHour
in local time. A use is a slot file created exclusively (O_CREAT|O_EXCL), so the file system decides who got it, and
nothing is ever overwritten: a stalled process can't undo another's use. A count is the number of files present; an
unreadable or garbage file counts (fails closed). Every use needs its audit line (§23): no line, no grant.

Only a person makes, changes or lifts a grant; nothing here does that on the model's word. Every change to the policy
file goes through `edit`, under `<policy file>.edit.lock` (§19.5, pidlock.py). The grant dialog (§19.6) and the
`tapwright grants` command (§19.7) build on `edit`, `listing` and `revoke_all`."""
from __future__ import annotations

import dataclasses
import datetime
import json
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Callable

from . import pidlock
from .auditlog import AuditLog

MAX_DAYS = 30
OFFER_PER_DAY = 3  # §19.6: the grant a person is offered after an Allow once: 3 a day ...
OFFER_DAYS = 7  # ... for 7 days
TOOL = "tapwright"
# §19.2: never grantable, whatever the file says. "Any word the tool treats as a payment" adds a few to the four.
PAYMENT_WORDS = ("Buy", "Purchase", "Pay", "Subscribe", "Checkout", "Check out", "Place order", "Donate")
ACCOUNT_WORDS = ("Delete account", "Delete my account", "Close account", "Deactivate account")
# Words that reach other people: a grant naming one is draftsOnly unless a person lifted it (§19.1, §21.3)
REACH_WORDS = ("Send", "Post", "Publish", "Reply", "Comment")
# §19.2's last line, Tapwright's to enforce: Settings › Passwords, Wallet, and the account, privacy and security panes.
# The panes of Settings can't be told apart reliably from the tree (a sub-pane such as Location Services has its own
# title, and titles change with the iOS version and language), so no grant covers ANY touch in Settings. The
# Passwords app (iOS 18) and Wallet are covered by nothing either.
NEVER_APPS = ("com.apple.Preferences", "com.apple.Passbook", "com.apple.Passwords")
CLAIM_TRIES = 4
_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
# names Windows keeps for devices: a folder called "con" can't be made there
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_CLOCK = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def matches(word: str, text: str) -> bool:
    from .policy import _matches
    return _matches(word, text)


def _any(words: tuple[str, ...] | list[str], text: str) -> str | None:
    return next((w for w in words if matches(w, text)), None)


def ascii_lower(name: str) -> str:
    return "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in name)


def safe_name(name: str) -> bool:
    """Letters, digits, `_`, `.` and `-` (it names a folder); not `.`, `..`, a name ending in a dot, or a device name."""
    return (bool(_NAME.match(name)) and name not in (".", "..") and not name.endswith(".")
            and ascii_lower(name).split(".")[0] not in _RESERVED)


def never_reason(word: str, block: tuple[str, ...]) -> str | None:
    """Why a grant's word can never be granted (§19.2), as the refusal says it; None when it can be."""
    if (pay := _any(PAYMENT_WORDS, word)) is not None:
        return f"payments and purchases are never grantable ({json.dumps(pay)})."
    if (acct := _any(ACCOUNT_WORDS, word)) is not None:
        return f"deleting an account is never grantable ({json.dumps(acct)})."
    if (blocked := _any(block, word)) is not None:
        return f"a blocked word is never grantable ({json.dumps(blocked)})."
    return None


def touch_never(app: str | None, name: str | None) -> bool:
    """Is this touch in §19.2, whatever the grants say? A payment or an account deletion on the element, or an app
    where the account, privacy and security settings, the passwords and the cards live."""
    return (app in NEVER_APPS) or (bool(name) and _any(PAYMENT_WORDS + ACCOUNT_WORDS, name or "") is not None)


def reaches_people(word: str) -> bool:
    return _any(REACH_WORDS, word) is not None


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def short_date(day: datetime.date) -> str:
    """`2026-11-21` as `21 Nov` (the same in every locale)."""
    return f"{day.day} {_MONTHS[day.month - 1]}"


def caption(grant: "Grant", app: str) -> str:
    """The grant dialog's question, saying everything the grant allows (§19.6): the word, the app, the counts, the end
    date, the route, the quiet hours, and drafts shown first. `app` is the app's name; text from the app and the file is
    cleaned and cut to 80 characters, as §11.3 says."""
    from .dialog import clean
    words = [f'"{clean(w)}"' for w in grant.words]
    spoken = words[0] if len(words) == 1 else f"{', '.join(words[:-1])} or {words[-1]}"
    counts = f"up to {grant.per_day} {'time' if grant.per_day == 1 else 'times'} a day"
    if grant.per_hour is not None:
        counts += f" and {grant.per_hour} an hour"
    quiet = f", not between {grant.quiet[0]} and {grant.quiet[1]}" if grant.quiet else ""
    route = f", only in route {clean(grant.route)}" if grant.route else ""
    drafts = " (drafts shown to you first)" if grant.drafts_only else ""
    return (f"Allow tapping {spoken} in {clean(app) or 'the app'} {counts}{quiet}{route}, "
            f"until {short_date(grant.until)}{drafts}?")


def name_base(app: str, word: str) -> str:
    """A grant's name from the app's name and the word, as `instagram_post`: ASCII letters, digits and `_`."""
    def slug(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", ascii_lower(text)).strip("_")
    return "_".join(part for part in (slug(app), slug(word)) if part) or "grant"


def _date(text: object) -> datetime.date | None:
    if not isinstance(text, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def _minutes(clock: str) -> int:
    m = _CLOCK.match(clock)
    return int(m.group(1)) * 60 + int(m.group(2)) if m else 0


def _whole(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 100_000


# ---- the grant (§19.1) -------------------------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class Grant:
    name: str
    app: str
    words: tuple[str, ...]
    until: datetime.date
    per_day: int = 1
    per_hour: int | None = None
    route: str | None = None
    quiet: tuple[str, str] | None = None
    drafts_only: bool = False
    revoked: str | None = None

    @property
    def folder(self) -> str:
        return ascii_lower(self.name)

    def in_quiet(self, at: datetime.datetime) -> bool:
        if self.quiet is None:
            return False
        start, end, now = _minutes(self.quiet[0]), _minutes(self.quiet[1]), at.hour * 60 + at.minute
        return start <= now < end if start < end else (now >= start or now < end)


@dataclasses.dataclass(frozen=True)
class Refusal:
    name: str  # the grant's name, or `#3` (its place in the list) when it has none
    reason: str  # a sentence ending in a full stop

    def line(self) -> str:
        shown = self.name if self.name.startswith("#") else json.dumps(self.name)
        return f"Note: grant {shown} is refused: {self.reason}"


def parse_grants(raw: object, today: datetime.date,
                 block_for: Callable[[str], tuple[str, ...]]) -> tuple[list[Grant], list[Refusal]]:
    """A version 2 file's `grants`. A grant that can't load is refused alone, with why (§19.1); the others load.
    `block_for(app)` is the block list in force for an app."""
    grants: list[Grant] = []
    refused: list[Refusal] = []
    if raw is None:
        return grants, refused
    if not isinstance(raw, list):
        return grants, [Refusal("#1", "grants must be a list.")]
    keys: set[str] = set()
    for index, item in enumerate(raw, 1):
        name = item.get("name") if isinstance(item, dict) else None
        label = name if isinstance(name, str) and name.strip() else f"#{index}"

        def refuse(reason: str) -> None:
            refused.append(Refusal(label, reason))

        if not isinstance(item, dict):
            refuse("it isn't an object.")
            continue
        if label.startswith("#"):
            refuse("name is missing.")
            continue
        if not safe_name(label):
            refuse("name may only use letters, digits, _, . and - (it names a folder), and not be . or .., end in a "
                   "dot, or be a device name such as con.")
            continue
        key = ascii_lower(label)
        if key in keys:  # (1.14.2) equal ignoring ASCII case is one name: the later is refused, active or revoked
            refuse("name is already used by an earlier grant (names that differ only in case are one name).")
            continue
        keys.add(key)
        app = item.get("app")
        if not isinstance(app, str) or not app.strip():
            refuse("app is missing.")
            continue
        words = item.get("words")
        if not isinstance(words, list) or not words or not all(isinstance(w, str) and w.strip() for w in words):
            refuse("words must be a list of words, at least one.")
            continue
        words_t = tuple(" ".join(w.split()) for w in words)
        until = _date(item.get("until"))
        if until is None:
            refuse("until must be a date, like 2026-11-21.")
            continue
        away = (until - today).days
        if away > MAX_DAYS:
            refuse(f"until is {away} days away; the most is {MAX_DAYS}.")
            continue
        route = item.get("route")
        if route is not None and (not isinstance(route, str) or not route.strip()):
            refuse("route must be the name of a saved route.")
            continue
        per_day, per_hour = item.get("perDay", 1), item.get("perHour")
        if not _whole(per_day):
            refuse("perDay must be a whole number, 1 or more.")
            continue
        if per_hour is not None and not _whole(per_hour):
            refuse("perHour must be a whole number, 1 or more.")
            continue
        drafts = item.get("draftsOnly")
        if drafts is not None and not isinstance(drafts, bool):
            refuse("draftsOnly must be true or false.")
            continue
        quiet = item.get("quiet")
        quiet_t: tuple[str, str] | None = None
        if quiet is not None:
            start = quiet.get("from") if isinstance(quiet, dict) else None
            end = quiet.get("to") if isinstance(quiet, dict) else None
            if not (isinstance(start, str) and isinstance(end, str) and _CLOCK.match(start) and _CLOCK.match(end)):
                refuse("quiet needs from and to as HH:MM.")
                continue
            if start == end:
                refuse("quiet's from and to must differ.")
                continue
            quiet_t = (start, end)
        revoked = item.get("revoked")
        if revoked is not None and _date(revoked) is None:
            refuse("revoked must be a date, like 2026-11-14.")
            continue
        never = next((r for w in words_t if (r := never_reason(w, block_for(app))) is not None), None)
        if never is not None:
            refuse(never)
            continue
        grants.append(Grant(label, app, words_t, until, per_day, per_hour, route.strip() if route else None, quiet_t,
                            drafts if isinstance(drafts, bool) else any(reaches_people(w) for w in words_t),
                            revoked))
    return grants, refused


@dataclasses.dataclass(frozen=True)
class Spec:
    """What a person was offered (§19.6), before it is a grant."""
    name_base: str
    app: str
    words: tuple[str, ...]
    per_day: int = OFFER_PER_DAY
    per_hour: int | None = None
    days: int = OFFER_DAYS
    route: str | None = None
    quiet: tuple[str, str] | None = None
    drafts_only: bool | None = None  # only False is written: the person said Yes to sending without drafts


# ---- what a touch is, and what the grants say -------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class Touch:
    app: str | None  # the bundle id of the app the element is in
    name: str | None  # the element's own label or text, as the policy matches it (§11.1)
    routes: tuple[str, ...] = ()  # the saved routes running now (a drive of one, a replay, a scheduled run)
    tool: str = "tap"


@dataclasses.dataclass
class Use:
    """A claimed use: who made it and its slot files (relative to the uses folder, `/`-separated)."""
    name: str
    owner: str
    slots: list[str]
    today: int
    per_day: int


@dataclasses.dataclass
class Decision:
    kind: str  # "covered", "draft" or "ask"
    name: str | None = None
    counts: str = ""  # "2 of 3", with this use
    use: Use | None = None  # a covered touch's use (none for a dry run's peek)
    notes: list[str] = dataclasses.field(default_factory=list)  # why a matching grant didn't cover it (§19.3)
    asked: list[str] = dataclasses.field(default_factory=list)  # §12's grant_asked words

    @property
    def line(self) -> str:
        return f"Note: allowed by grant {json.dumps(self.name)} ({self.counts} today)."

    @property
    def dry(self) -> str:
        return f"It is allowed by grant {json.dumps(self.name)} ({self.counts} today)."


@dataclasses.dataclass
class Loaded:
    ok: bool  # False: the file can't be read or isn't a policy now; no grant covers anything
    grants: list[Grant]
    refused: list[Refusal]
    block_for: Callable[[str], tuple[str, ...]]


def _local_now() -> datetime.datetime:
    return datetime.datetime.now().astimezone()


def day_folder(at: datetime.datetime) -> str:
    return at.strftime("%Y-%m-%d")


def hour_folder(at: datetime.datetime) -> str:
    return at.strftime("%Y-%m-%dT%H")


class Book:
    """The grants of one policy file, its use slots, and its edits."""

    def __init__(self, policy_path: str | os.PathLike, audit: AuditLog | None, *,
                 now: Callable[[], datetime.datetime] | None = None, lock_deps: pidlock.Deps | None = None,
                 lock_wait: float = 5.0, hooks: dict[str, Callable[..., None]] | None = None) -> None:
        self.path = Path(policy_path)
        self.uses = Path(f"{self.path}.uses")
        self.lock_path = Path(f"{self.path}.edit.lock")
        self.audit = audit
        self.now = now or _local_now
        self.lock_deps = lock_deps
        self.lock_wait = lock_wait
        self.hooks = hooks or {}  # for tests: "before_create", "after_claim", "before_write"
        self._cache: tuple[tuple[int, int], datetime.date, Loaded] | None = None

    def _hook(self, name: str, *args) -> None:
        if (hook := self.hooks.get(name)) is not None:
            hook(*args)

    # ---- reading the file (§19.5: every touch reads it again, by its modification time) ----
    def load(self) -> Loaded:
        from . import policy
        today = self.now().date()
        try:
            st = self.path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            return Loaded(False, [], [], lambda app: ())
        if self._cache is not None and self._cache[0] == stamp and self._cache[1] == today:
            return self._cache[2]
        try:
            obj = json.loads(self.path.read_text(encoding="utf-8-sig"))
            rules = policy.parse(obj)
        except (OSError, UnicodeDecodeError, ValueError, RecursionError):
            return Loaded(False, [], [], lambda app: ())

        def block_for(app: str) -> tuple[str, ...]:
            return rules.words(app)[0]
        grants, refused = parse_grants(obj.get("grants"), today, block_for) if obj.get("version") == 2 else ([], [])
        loaded = Loaded(True, grants, refused, block_for)
        self._cache = (stamp, today, loaded)
        return loaded

    def notes(self) -> list[str]:
        """The `Note:` lines for `doctor` and the session's first answer: each refused grant, a file that can't be read."""
        loaded = self.load()
        lines = [r.line() for r in loaded.refused]
        if not loaded.ok:
            lines.append("Note: the policy file can't be read now, so no grant covers anything; every touch asks a "
                         "person.")
        return lines

    # ---- counting ----
    def _list(self, folder: Path) -> list[str] | None:
        """A folder's entries; [] when it doesn't exist; None when it can't be read (fails closed)."""
        try:
            return os.listdir(folder)
        except FileNotFoundError:
            return []
        except OSError:
            return None

    def counts(self, grant: Grant, at: datetime.datetime) -> tuple[int, int] | None:
        """(today's uses, this hour's uses) at `at`; None when they can't be counted."""
        base = self.uses / grant.folder
        day, hour = self._list(base / day_folder(at)), self._list(base / hour_folder(at))
        return None if day is None or hour is None else (len(day), len(hour))

    @staticmethod
    def _limit(grant: Grant, today: int, hour: int) -> str | None:
        if today >= grant.per_day:
            return f"{today} of {grant.per_day} today"
        if grant.per_hour is not None and hour >= grant.per_hour:
            return f"{hour} of {grant.per_hour} this hour"
        return None

    # ---- claiming (§19.4) ----
    def _claim_slot(self, folder: Path, prefix: str, limit: int, content: bytes) -> tuple[str, int] | int | None:
        """The first free slot k = 1..limit in `folder`, created exclusively: (its name, the folder's count with it);
        an int when every slot is taken (the count); None when the folder can't be read or written."""
        try:
            folder.mkdir(parents=True, exist_ok=True)  # two claimers may make it at once: exist ok
        except OSError:
            return None
        names = self._list(folder)
        if names is None:
            return None
        if len(names) >= limit:
            return len(names)
        for k in range(1, limit + 1):
            slot = folder / f"{prefix}{k}"
            if slot.name in names:
                continue
            self._hook("before_create", slot)
            try:
                fd = os.open(slot, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
            except FileExistsError:
                continue
            except PermissionError:
                continue  # Windows: a slot being deleted right now is taken for a moment
            except OSError:
                return None
            try:
                os.write(fd, content)
            except OSError:
                pass  # an empty slot still counts as a use: fails closed
            finally:
                os.close(fd)
            after = self._list(folder)
            if after is None or len(after) > limit:  # a garbage file took a place: give this one back
                _unlink(slot)
                return None if after is None else max(limit, len(after) - 1)
            return slot.name, len(after)
        return limit

    def _claim(self, grant: Grant, route: str | None) -> Use | str | None:
        """One use in the buckets of a fresh clock: the hour slot first (with a perHour), then the day slot; a day claim
        that fails gives the hour slot back. Then the clock is read again: a claim that midnight (or the next hour)
        overtook is deleted and made again in the current one. A string: the limit that stopped it; None: it can't
        be counted."""
        for _ in range(CLAIM_TRIES):
            at = self.now()
            counted = self.counts(grant, at)
            if counted is None:
                return None
            if (stopped := self._limit(grant, *counted)) is not None:
                return stopped
            owner = secrets.token_hex(8)
            body = {"t": at.isoformat(timespec="seconds"), "tool": TOOL, "owner": owner}
            if route:
                body["route"] = route
            content = (json.dumps(body) + "\n").encode()
            use = Use(grant.name, owner, [], 0, grant.per_day)
            if grant.per_hour is not None:
                hdir = hour_folder(at)
                got = self._claim_slot(self.uses / grant.folder / hdir, "h", grant.per_hour, content)
                if got is None:
                    return None
                if isinstance(got, int):
                    return f"{got} of {grant.per_hour} this hour"
                use.slots.append(f"{grant.folder}/{hdir}/{got[0]}")
            ddir = day_folder(at)
            got = self._claim_slot(self.uses / grant.folder / ddir, "", grant.per_day, content)
            if got is None or isinstance(got, int):
                self.release(use)  # the day failed: the hour slot goes back
                return None if got is None else f"{got} of {grant.per_day} today"
            use.slots.append(f"{grant.folder}/{ddir}/{got[0]}")
            use.today = got[1]
            if self._current(use, self.now()):
                return use
            self.release(use)  # (1.14.2) it crossed midnight or the hour: claimed again in the current one
        return None

    @staticmethod
    def _current(use: Use, at: datetime.datetime) -> bool:
        """Is every slot of the use in the day (or hour) of `at`?"""
        return all(slot.split("/")[1] in (day_folder(at), hour_folder(at)) for slot in use.slots)

    def _slot_path(self, use: Use, slot: str) -> Path | None:
        parts = slot.split("/")
        if len(parts) != 3 or parts[0] != ascii_lower(use.name) or not safe_name(use.name):
            return None
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(T\d{2})?", parts[1]) or not re.fullmatch(r"h?\d+", parts[2]):
            return None
        return self.uses.joinpath(*parts)

    def release(self, use: Use) -> None:
        """Give a use back: its own slot files are deleted, each only when its content names this owner (§19.4). A file
        that can't be read or deleted stays, and the use stays counted: the safe side."""
        if not use.owner:
            return
        for slot in list(use.slots):
            path = self._slot_path(use, slot)
            if path is not None and _owner(path) == use.owner:
                _unlink(path)
        use.slots.clear()

    def _fits(self, grant: Grant, use: Use) -> bool:
        """Does a claim still fit the grant as the file has it now: one day slot within perDay, and an hour slot within
        perHour exactly when there is a perHour; and do the buckets hold no more than the limits?"""
        days = hours = 0
        for slot in use.slots:
            folder, file = slot.split("/")[1:]
            if file.startswith("h"):
                hours += 1
                if grant.per_hour is None or int(file[1:]) > grant.per_hour:
                    return False
                limit = grant.per_hour
            else:
                days += 1
                if int(file) > grant.per_day:
                    return False
                limit = grant.per_day
            names = self._list(self.uses / grant.folder / folder)
            if names is None or len(names) > limit:
                return False
        return days == 1 and hours == (0 if grant.per_hour is None else 1)

    def holds(self, grant: Grant, touch: Touch, at: datetime.datetime, draft: bool = False) -> bool:
        """Does this grant, as it is now, cover the touch at `at`, the counts aside (§19.3)? `draft`: for the use a
        person's Send of a draft counts (§21.3), which a draftsOnly grant holds too."""
        return (grant.app == touch.app and not touch_never(touch.app, touch.name)
                and _any(grant.words, touch.name or "") is not None
                and (grant.route is None or grant.route in touch.routes)
                and grant.revoked is None and (draft or not grant.drafts_only) and at.date() <= grant.until
                and not grant.in_quiet(at))

    def use_draft(self, touch: Touch, name: str, detail: str) -> bool:
        """A draft a person sent (§21.3) counts as a use of the draftsOnly grant that stopped it, so that a loop can't post
        50 times (§19.1's limits): claimed, checked again and audited as any use is. False when it can't be counted; the
        person's Send stands all the same (the touch was theirs)."""
        grant = next((g for g in self.load().grants if g.name == name), None)
        if grant is None:
            return False
        return isinstance(self._use(grant, touch, detail, draft=True), Use)

    def _use(self, grant: Grant, touch: Touch, detail: str, draft: bool = False) -> Use | str | None:
        """Claim a use, then read the file and the clock again (a revocation, an end, quiet hours or a cut quota that
        came meanwhile win), then write the audit line. No line: the use is given back and the grant doesn't cover
        the touch (§23)."""
        if self.audit is None:
            return None
        current = grant
        for _ in range(CLAIM_TRIES):
            got = self._claim(current, current.route or (touch.routes[0] if touch.routes else None))
            if not isinstance(got, Use):
                return got
            try:
                self._hook("after_claim", got)
                fresh = next((g for g in self.load().grants if g.name == grant.name), None)
                at = self.now()
                if fresh is None or not self.holds(fresh, touch, at, draft):
                    self.release(got)
                    return None
                if not self._current(got, at) or not self._fits(fresh, got):
                    self.release(got)
                    current = fresh
                    continue
                if not self.audit.write("grant", app=touch.app, name=grant.name,
                                        detail=f"{detail} ({got.today} of {fresh.per_day} today)"):
                    self.release(got)
                    return None
                got.per_day = fresh.per_day
                return got
            except Exception:
                self.release(got)
                raise
        return None

    def decide(self, touch: Touch, mode: str, detail: str = "") -> Decision:
        """Whether a grant covers this touch (§19.3), for a touch the policy says needs a person. `mode` "use" claims a
        use and writes its audit line; "peek" (a dry run) claims and writes nothing. `detail` is the audit line's
        words, such as `tap "Post"`."""
        loaded = self.load()
        if not loaded.ok or touch_never(touch.app, touch.name):
            return Decision("ask")
        at = self.now()
        notes: list[str] = []
        asked: list[str] = []
        for grant in loaded.grants:
            if grant.app != touch.app or _any(grant.words, touch.name or "") is None:
                continue
            if grant.route is not None and grant.route not in touch.routes:
                continue  # a mismatch of app, word or route isn't noted: that grant simply isn't the one

            def doesnt(why: str, word: str) -> None:
                notes.append(f"Note: grant {json.dumps(grant.name)} doesn't cover this ({why}); asking a person.")
                asked.append(word)
            if grant.revoked is not None:
                doesnt(f"it was revoked on {grant.revoked}", "revoked")
                continue
            if at.date() > grant.until:
                doesnt(f"it ended on {grant.until.isoformat()}", "ended")
                continue
            if grant.in_quiet(at):
                doesnt(f"quiet hours {grant.quiet[0]} to {grant.quiet[1]}", "quiet hours")
                continue
            if mode == "use" and not grant.drafts_only:
                got = self._use(grant, touch, detail)
            else:
                counted = self.counts(grant, at)
                got = None if counted is None else self._limit(grant, *counted)
                if counted is not None and got is None:
                    got = Use(grant.name, "", [], counted[0] + 1, grant.per_day)
            if got is None:
                continue  # it can't be counted, or its line can't be written: it covers nothing, said nowhere
            if isinstance(got, str):
                doesnt(got, "limit reached")
                continue
            if grant.drafts_only:  # stops before the touch: a person sees the draft (§21.3); no use is counted
                return Decision("draft", grant.name, notes=notes, asked=asked)
            return Decision("covered", grant.name, f"{got.today} of {got.per_day}", got if got.owner else None)
        return Decision("ask", notes=notes, asked=asked)

    def still_covers(self, touch: Touch, use: Use) -> bool:
        """The check right before the touch (§19.4): the file and the clock read again, the use's slots still its own,
        in the current day and hour (else given back and claimed again there). False: the touch asks a person; the use
        is given back."""
        for _ in range(CLAIM_TRIES):
            grant = next((g for g in self.load().grants if g.name == use.name), None)
            at = self.now()
            if grant is None or not self.holds(grant, touch, at) or not use.slots:
                self.release(use)
                return False
            for slot in use.slots:
                path = self._slot_path(use, slot)
                if path is None or _owner(path) != use.owner:
                    return False
            at = self.now()
            if self._current(use, at) and self._fits(grant, use):
                return self.holds(grant, touch, self.now())
            self.release(use)
            got = self._claim(grant, grant.route or (touch.routes[0] if touch.routes else None))
            if not isinstance(got, Use):
                return False
            use.owner, use.slots, use.today, use.per_day = got.owner, got.slots, got.today, got.per_day
        self.release(use)
        return False

    # ---- pruning (§19.4) ----
    def prune(self) -> list[str]:
        """At start: the uses of a grant that has ended or was revoked are deleted, and the day and hour folders
        before today of every grant. Returns the grants whose uses were cleared."""
        loaded = self.load()
        if not loaded.ok:
            return []
        today = self.now().date()
        cleared: list[str] = []
        active = {g.folder for g in loaded.grants if g.revoked is None and today <= g.until}
        for grant in loaded.grants:
            if grant.folder in active:
                for folder in self._list(self.uses / grant.folder) or []:
                    day = _date(folder[:10])
                    if day is not None and day < today:
                        shutil.rmtree(self.uses / grant.folder / folder, ignore_errors=True)
                continue
            if self._remove_uses(grant.folder):
                cleared.append(grant.name)
        return cleared

    def _remove_uses(self, folder: str) -> bool:
        if not safe_name(folder):
            return False
        shutil.rmtree(self.uses / folder, ignore_errors=True)
        return not (self.uses / folder).exists()

    # ---- changing the file (§19.5) ----
    def edit(self, change: Callable[[dict], bool]) -> str | None:
        """Change the policy file under its edit lock: the file is read again under the lock, `change` edits that
        content (True: it changed something), and it is written with a temporary file and a rename. None when done
        (or nothing changed); else why nothing was written. A change that can't get the lock in a few seconds writes
        nothing. The hook for the grant dialog (§19.6) and lifting draftsOnly."""
        deadline = time.monotonic() + self.lock_wait
        while (token := pidlock.take(self.lock_path, self.lock_deps)) is None:
            if time.monotonic() >= deadline:
                return ("couldn't take the policy file's edit lock (another change is under way); nothing was "
                        "changed, try again.")
            time.sleep(0.025)
        try:
            try:
                obj = json.loads(self.path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeDecodeError, ValueError) as e:
                return f"couldn't read the policy file: {e}"
            if not isinstance(obj, dict):
                return "the policy file isn't a policy."
            if not change(obj):
                return None
            self._hook("before_write")
            temp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
            try:
                temp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                for attempt in range(6):
                    try:
                        os.replace(temp, self.path)
                        break
                    except PermissionError:  # Windows: a reader has it open this moment
                        if attempt == 5:
                            raise
                        time.sleep(0.02)
            except OSError as e:
                _unlink(temp)
                return f"couldn't write the policy file: {e}"
            self._cache = None
            return None
        finally:
            pidlock.release(self.lock_path, token)

    def revoke_all(self, detail: str = "grants --revoke-all") -> tuple[list[str], list[str]] | str:
        """`grants --revoke-all` (§19.5): every grant marked `"revoked": "<today>"` (one already revoked keeps its
        date), one `revoke` audit line each, and the revoked grants' uses deleted. A running route stops between
        moves (`revoked_since`). (revoked, already) or why it couldn't."""
        today = self.now().date().isoformat()
        revoked: list[tuple[str, str | None]] = []
        already: list[str] = []

        def change(obj: dict) -> bool:
            revoked.clear()
            already.clear()
            items = obj.get("grants")
            if not isinstance(items, list):
                return False
            for index, item in enumerate(items, 1):
                if not isinstance(item, dict):
                    continue
                name = item.get("name") if isinstance(item.get("name"), str) and item["name"].strip() else f"#{index}"
                if _date(item.get("revoked")) is not None:
                    already.append(name)
                    continue
                item["revoked"] = today
                revoked.append((name, item.get("app") if isinstance(item.get("app"), str) else None))
            return bool(revoked)
        why = self.edit(change)
        if why is not None:
            return why
        for name, app in revoked:  # the kill switch never waits on its record: a line that fails doesn't undo it
            if self.audit is not None:
                self.audit.write("revoke", app=app, name=name, detail=detail)
            if safe_name(name):
                self._remove_uses(ascii_lower(name))
        return [n for n, _ in revoked], already

    def create(self, spec: "Spec") -> Grant | str:
        """A grant a person made through the dialog (§19.6), written into the policy file under the edit lock: a name
        not used yet (ignoring ASCII case, revoked ones too), `until` at most 30 days away, checked by the loader's own
        rules, a version 1 file becoming version 2. The creation is audited (§23, `approval`, `grant created`). Only
        the daemon's dialog calls this, with what the person answered. The grant, or why nothing was written."""
        today = self.now().date()
        until = today + datetime.timedelta(days=min(MAX_DAYS, max(0, spec.days)))
        made: list[Grant] = []
        refusal: list[str] = []

        def change(obj: dict) -> bool:
            from . import policy
            made.clear()
            refusal.clear()
            if obj.get("version") not in (1, 2):
                refusal.append("the policy file has no version 1 or 2.")
                return False
            items = obj.get("grants")
            items = items if isinstance(items, list) else []
            taken = {ascii_lower(i["name"]) for i in items if isinstance(i, dict) and isinstance(i.get("name"), str)}
            name = spec.name_base
            n = 2
            while ascii_lower(name) in taken:
                name = f"{spec.name_base}_{n}"
                n += 1
            raw: dict = {"name": name, "app": spec.app, "words": list(spec.words)}
            if spec.route:
                raw["route"] = spec.route
            raw["perDay"] = spec.per_day
            if spec.per_hour is not None:
                raw["perHour"] = spec.per_hour
            raw["until"] = until.isoformat()
            if spec.quiet:
                raw["quiet"] = {"from": spec.quiet[0], "to": spec.quiet[1]}
            if spec.drafts_only is False:  # only a person's Yes writes it; otherwise the file says nothing (§19.1)
                raw["draftsOnly"] = False
            rules = policy.parse({**obj, "version": 2, "grants": []})
            grants, refused = parse_grants([raw], today, lambda app: rules.words(app)[0])
            if not grants:
                refusal.append(refused[0].reason if refused else "it can't be a grant.")
                return False
            obj["version"] = 2
            obj["grants"] = [*items, raw]
            made.append(grants[0])
            return True
        why = self.edit(change)
        if why is not None:
            return why
        if not made:
            return refusal[0] if refusal else "nothing was written."
        if self.audit is not None:
            first = made[0].words[0]
            self.audit.write("approval", app=made[0].app, name=first, detail="grant created")
        return made[0]

    def active(self) -> frozenset[str]:
        """The grants that cover anything now (not revoked, not ended): what a running route started with."""
        loaded = self.load()
        today = self.now().date()
        return frozenset(g.name for g in loaded.grants if g.revoked is None and today <= g.until) if loaded.ok \
            else frozenset()

    def revoked_since(self, started_with: frozenset[str]) -> list[str]:
        """Which of the grants a run started with are revoked now (by `--revoke-all`, by the other tool, by hand): the
        run stops between moves (§19.5). A file that can't be read stops it too."""
        loaded = self.load()
        if not loaded.ok:
            return sorted(started_with)
        by_name = {g.name: g for g in loaded.grants}
        return sorted(n for n in started_with if n not in by_name or by_name[n].revoked is not None)


def _owner(path: Path) -> str | None:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return obj.get("owner") if isinstance(obj, dict) and isinstance(obj.get("owner"), str) else None


def _unlink(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass

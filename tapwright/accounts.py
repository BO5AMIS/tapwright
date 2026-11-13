"""Test accounts (AGENT-TOOLS §18.6): named accounts kept in a file on the user's computer (`--accounts FILE`). An agent
says `account: "tester2"`; the tool types the values. They are never in an answer, the action log, the map, a script, a
report or a caption: every answer is scrubbed of them, a name that holds one is never saved, and a step carries only the
placeholder `${account.username}` or `${account.password}`, filled in at the moment of typing.

  - A value is the whole text of a step, never part of one (joining text would get around the secure-field rule).
  - A password goes only into a secure field, a username only into a text field that isn't secure.
  - `apps` limits where an account may be used.
  - A file inside a git work tree that doesn't ignore it loads no accounts: any use says so.
  - `phone.passcode` is stored and exposed nowhere. (Typing it on the lock screen is a later task.)

Pure but for reading the file and asking git: no phone.
"""
from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .control import Refused

VERSION = 1
NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
FIELDS = ("username", "password", "passcode", "apps")
PLACEHOLDER = re.compile(r"\$\{account\.(username|password)\}")
MENTION = re.compile(r"\$\{account\.")
MIN_SCRUBBED = 3  # a value shorter than this isn't scrubbed out of text (it would eat ordinary words)
BIDI_MARKS = dict.fromkeys(map(ord, "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"))  # the invisible direction marks ui._text drops from screen text
PHONE = "phone"  # the entry that holds the phone's passcode, not an account to sign in with
NO_FILE = ("no test accounts: start tapwright serve or mcp with --accounts FILE (a JSON file kept outside any git repo, "
           "or ignored by it)")
GitRunner = Callable[[list[str], str], "subprocess.CompletedProcess[str] | None"]


@dataclass(frozen=True)
class Account:
    name: str
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)
    apps: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Loaded:
    """What a file holds: the accounts (by name, in the file's order), the passcode, and why none can be used (None when
    all is well). Nothing here shows a value in its repr."""
    path: str = ""
    accounts: dict[str, Account] = field(default_factory=dict, repr=False)
    passcode: str | None = field(default=None, repr=False)
    problem: str | None = None

    @property
    def names(self) -> tuple[str, ...]:
        """The accounts an agent may name (a problem leaves none)."""
        if self.problem is not None:
            return ()
        return tuple(n for n, a in self.accounts.items() if a.username or a.password)

    def account(self, name: str, bundle: str | None) -> Account:
        """The account called `name`, for use in app `bundle`. `bad_params` when no file is given or the file can't be used,
        `not_found` for a name it hasn't, `not_allowed` for an app the account isn't for."""
        if not self.path:
            raise Refused("bad_params", NO_FILE)
        if self.problem is not None:
            raise Refused("bad_params", self.problem)
        if name not in self.names:
            raise Refused("not_found", f"no account called {name}; the accounts are: {', '.join(self.names) or 'none'}")
        found = self.accounts[name]
        if found.apps is not None and bundle not in found.apps:
            raise Refused("not_allowed", f"account {name} may be used only in {', '.join(found.apps)}; this is {bundle or 'no app'}")
        return found

    def entries(self) -> tuple[tuple[str, str, str], ...]:
        """(account name, "username" or "password", value) of every value this file holds."""
        return _entries(self.accounts)

    @functools.cached_property
    def _redaction(self) -> "Redaction":
        return Redaction(self.entries())

    @property
    def _values(self) -> list[tuple[str, str]]:
        return self._redaction.values

    @property
    def _placeholders(self) -> "re.Pattern[str] | None":
        return self._redaction.placeholders

    @property
    def redacts(self) -> bool:
        """Is there any value to redact?"""
        return self._redaction.redacts

    def scrub(self, text: str) -> str:
        """The text with every account's username and password replaced by `<name's username>` / `<name's password>`
        (Redaction.scrub)."""
        return self._redaction.scrub(text)

    def holds(self, text: str) -> bool:
        """Does the text hold an account's value, or the placeholder it is shown as? What does is never saved to a map,
        a recording or a route: the value is private, and the placeholder finds nothing on a phone."""
        return self._redaction.holds(text)

    def holds_in(self, value: object) -> bool:
        """Does any string inside `value` (a move, a target: dicts and lists, to any depth) hold an account's value?"""
        if isinstance(value, str):
            return self.holds(value)
        if isinstance(value, dict):
            return any(self.holds_in(v) for v in value.values())
        if isinstance(value, (list, tuple)):
            return any(self.holds_in(v) for v in value)
        return False

    def clean(self, names: dict) -> dict:
        """A road's names without the ones that hold an account's value (a field showing the username is no name)."""
        return {k: v for k, v in names.items() if not (isinstance(v, str) and self.holds(v))}


NONE = Loaded()  # no file was given


def _entries(found: dict[str, Account]) -> tuple[tuple[str, str, str], ...]:
    return tuple((a.name, kind, value) for a in found.values()
                 for kind, value in (("username", a.username), ("password", a.password))
                 if value and len(value) >= MIN_SCRUBBED)


def _repr_inner(text: str, quote: str) -> str:
    """What Python's repr puts between its quotes, as it is inside a longer string's repr too: each character escaped as
    repr escapes it alone (a backslash, a control, an unprintable one), and `quote` (the quote that string's repr chose:
    `'` when it holds both kinds) escaped as well."""
    out = "".join(repr(c)[1:-1] if c not in "'\"" else c for c in text)
    return out.replace(quote, "\\" + quote) if quote else out


def _ascii_inner(text: str, quote: str) -> str:
    """The same for ascii(): a non-ASCII character escaped too (backslash x, u or U)."""
    out = "".join(ascii(c)[1:-1] if c not in "'\"" else c for c in text)
    return out.replace(quote, "\\" + quote) if quote else out


# The ways a value is changed on its way out, each as it shows INSIDE a longer string (a log line, a JSON document, a
# URL): a form found anywhere in a text is replaced, so a quoted shape needs no form of its own. Each works character by
# character (or, collapsing, on whitespace runs), so what one makes of a value is part of what it makes of a longer string
# that holds it, and the shapes compose: a repr inside a JSON document, a JSON string URL-encoded (Codex, PR #10 cycle 6).
_STEPS: tuple[Callable[[str], str], ...] = (
    lambda t: " ".join(t.split()),  # a text line's whitespace collapsed
    lambda t: t.translate(BIDI_MARKS),  # the direction marks ui._text drops
    lambda t: json.dumps(t, ensure_ascii=False)[1:-1],
    lambda t: json.dumps(t, ensure_ascii=True)[1:-1],
    lambda t: _repr_inner(t, ""),
    lambda t: _repr_inner(t, "'"),
    lambda t: _ascii_inner(t, ""),
    lambda t: _ascii_inner(t, "'"),
    lambda t: urllib.parse.quote(t),
    lambda t: urllib.parse.quote(t, safe=""),
    lambda t: urllib.parse.quote_plus(t),
)
FORM_DEPTH = 3  # how many of the steps are composed at most


@functools.lru_cache(maxsize=512)
def forms(value: str) -> frozenset[str]:
    """Every shape a value can take on its way out (Codex, PR #10 cycles 5 and 6): the value with up to FORM_DEPTH of the
    `_STEPS` applied, in every order, each one that changes the string (collapsed, without direction marks, JSON-escaped
    with and without escapes of non-ASCII, as Python's `repr` and `ascii` write it, URL-encoded with `quote` and
    `quote_plus`): a `%r` in a log line written into a JSON crash report is one of them. A value written whole with its
    quotes (`repr`, `ascii`, JSON) is a form too, so a quoted one is replaced quotes and all. Deduplicated and cached."""
    out = {value}
    level = {value}
    for _ in range(FORM_DEPTH):
        level = {made for text in level for step in _STEPS if (made := step(text)) != text} - out
        out |= level
    bases = {value, " ".join(value.split()), value.translate(BIDI_MARKS), " ".join(value.translate(BIDI_MARKS).split())}
    out.update(shown for b in bases for shown in (json.dumps(b, ensure_ascii=False), json.dumps(b, ensure_ascii=True),
                                                  repr(b), ascii(b)))
    return frozenset(f for f in out if f)


_forms = forms  # (the old name)

_ESCAPE = re.compile(r"%[0-9a-fA-F]{2}")


def upper_escapes(text: str) -> str:
    """The text with the hex digits of every percent escape uppercased (`%2f` is `%2F`: hex digits are case-insensitive in a
    URL, `quote` writes them in capitals). The same length, so a span found in it is the same span of the text; the rest
    of the text keeps its case (Codex, PR #10 cycle 8)."""
    return _ESCAPE.sub(lambda m: m.group(0).upper(), text) if "%" in text else text


def _swap(text: str, norm: str, value: str, label: str) -> tuple[str, str]:
    """`text` and its `norm` (upper_escapes of it) with every `value` found in `norm` replaced by `label` in both."""
    out, out_norm, at = [], [], 0
    while (i := norm.find(value, at)) >= 0:
        out += [text[at:i], label]
        out_norm += [norm[at:i], label]
        at = i + len(value)
    return "".join(out) + text[at:], "".join(out_norm) + norm[at:]


class Redaction:
    """A set of account values, as (name, kind, value), and what redacts them: every form of each (`forms`) becomes
    `<name's username>` / `<name's password>`, the longest form first, so a value that holds another is replaced whole."""

    def __init__(self, entries: "tuple[tuple[str, str, str], ...]" = ()) -> None:
        # (forms and the text they are looked for in both have the hex digits of each %XX uppercased: Codex, PR #10 cycle 8)
        pairs = [(upper_escapes(form), f"<{name}'s {kind}>") for name, kind, value in entries for form in forms(value)]
        pairs.sort(key=lambda pair: -len(pair[0]))
        self.values: list[tuple[str, str]] = pairs
        shown = sorted({label for _, label in pairs}, key=len, reverse=True)
        self.placeholders = re.compile("(" + "|".join(map(re.escape, shown)) + ")") if shown else None

    @property
    def redacts(self) -> bool:
        return bool(self.values)

    @functools.cached_property
    def _any(self) -> "re.Pattern[str] | None":
        """One search for every form at once (a value has up to a few hundred: a loop over them was 3 times slower)."""
        return re.compile("|".join(re.escape(value) for value, _ in self.values)) if self.values else None

    def scrub(self, text: str) -> str:
        """Idempotent: a placeholder already there is left alone, so text redacted where it came in can be redacted again
        where it goes out (an account called `tester` with the password `test` doesn't become `<<tester's ...`)."""
        values = self.values
        if not text or self._any is None or self._any.search(upper_escapes(text)) is None:
            return text
        parts = self.placeholders.split(text)  # (even: text between placeholders; odd: a placeholder, kept)
        for n in range(0, len(parts), 2):
            part = parts[n]
            norm = upper_escapes(part)  # (same length, so a match in it is the same span of the part)
            for value, label in values:
                if value in norm:
                    part, norm = _swap(part, norm, value, label)
            parts[n] = part
        return "".join(parts)

    def holds(self, text: str) -> bool:
        if not text:
            return False
        return bool(self._any is not None and self._any.search(upper_escapes(text))) or bool(
            self.placeholders is not None and self.placeholders.search(text))


def mentions(text: str) -> bool:
    return bool(MENTION.search(text))


def typed(text: str, active: Account | None, secure: bool, ref: str) -> str:
    """The text a `type_text` really types into the field `ref`: an account's placeholder becomes its value, where the
    field allows it (§18.6). `bad_params` for a placeholder with no account active, one that is part of a text, a value
    the account lacks, a password into a field that isn't secure or a username into one that is."""
    if not mentions(text):
        return text
    if active is None:
        raise Refused("bad_params", "an account's value is typed only inside drive with an account: drive {\"route\": "
                                    "\"<name>\", \"account\": \"<account>\"}, or its `then` steps")
    whole = PLACEHOLDER.fullmatch(text)
    if whole is None:
        raise Refused("bad_params", "an account's value is the whole text of a step, never part of one: "
                                    "\"text\": \"${account.username}\" works, \"hi ${account.password}\" doesn't")
    which = whole.group(1)
    value = active.password if which == "password" else active.username
    if not value:
        raise Refused("bad_params", f"account {active.name} has no {which}")
    if which == "password" and not secure:
        raise Refused("bad_params", f"an account's password goes only into a password field; {ref} is a text field.")
    if which == "username" and secure:
        raise Refused("bad_params", f"an account's username goes only into a text field that isn't secure; {ref} is a password field.")
    return value


# ---- the file ----

def _git(args: list[str], cwd: str) -> "subprocess.CompletedProcess[str] | None":
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None  # no git here, or it didn't answer: the file isn't in a repo as far as anyone can tell


def git_problem(path: str, git: GitRunner = _git) -> str | None:
    """Why the file may not be used: it is inside a git work tree that doesn't ignore it (a tracked file isn't ignored)."""
    folder = str(Path(path).resolve().parent)
    top = git(["rev-parse", "--show-toplevel"], folder)
    if top is None or top.returncode != 0 or not top.stdout.strip():
        return None
    ignored = git(["check-ignore", "-q", "--", str(Path(path).resolve())], folder)
    if ignored is None or ignored.returncode == 0:
        return None
    if ignored.returncode != 1:
        return None  # git failed rather than said no: not a verdict
    return (f"accounts file is inside the git repo at {top.stdout.strip()} and not ignored; move it or add it to .gitignore.")


def _read(data: object) -> tuple[dict[str, Account], str | None, str | None]:
    """(accounts, passcode, problem) of a parsed file."""
    if not isinstance(data, dict):
        return {}, None, "it isn't a JSON object"
    if data.get("version") != VERSION:
        return {}, None, f"its version is {data.get('version')!r}; this tool reads version {VERSION}"
    raw = data.get("accounts")
    if not isinstance(raw, dict):
        return {}, None, 'it has no "accounts" object'
    found: dict[str, Account] = {}
    passcode = None
    for name, entry in raw.items():
        if not isinstance(name, str) or not NAME.match(name):
            return {}, None, f"an account's name is letters, digits, - and _, not {name!r}"
        if not isinstance(entry, dict):
            return {}, None, f"{name} isn't an object"
        other = [k for k in entry if k not in FIELDS]
        if other:
            return {}, None, f"{name} has {other[0]}; an account has {', '.join(FIELDS)}"
        for key in ("username", "password", "passcode"):
            if key in entry and (not isinstance(entry[key], str) or not entry[key]):
                return {}, None, f"{name}'s {key} must be a non-empty text"
        for key in ("username", "password"):
            if key in entry and len(entry[key]) < MIN_SCRUBBED:  # too short to be scrubbed out of text, so not accepted
                return {}, None, f"{name}'s {key} must be at least {MIN_SCRUBBED} characters: a shorter one can't be kept out of answers and maps"
        apps = entry.get("apps")
        if apps is not None and (not isinstance(apps, list) or not all(isinstance(a, str) and a for a in apps)):
            return {}, None, f"{name}'s apps must be a list of bundle ids"
        if name == PHONE:
            passcode = entry.get("passcode")
        found[name] = Account(name, entry.get("username"), entry.get("password"), tuple(apps) if apps is not None else None)
    return found, passcode, None


def load(path: str, git: GitRunner = _git) -> Loaded:
    """The accounts file at `path`. It never raises: a file that can't be used is a `problem`, said at the first use."""
    try:
        text = Path(path).read_text("utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        return Loaded(path, problem=f"the accounts file {path} can't be read ({getattr(e, 'strerror', None) or e})")
    if (why := git_problem(path, git)) is not None:
        return Loaded(path, problem=why)  # the file is not even parsed: nothing of it is loaded
    try:
        data = json.loads(text)
    except (ValueError, RecursionError) as e:
        return Loaded(path, problem=f"the accounts file {path} isn't JSON ({e})")
    found, passcode, why = _read(data)
    if why is not None:
        return Loaded(path, problem=f"the accounts file {path} can't be used: {why}")
    return Loaded(path, found, passcode)


class Store:
    """The loaded files, each read again when it changes (its size or time), so an edit needs no restart. Every value ever
    loaded from any file stays in `scrub` for the daemon's life, even after the file changes: what the app still has in
    flight can hold the old value (Codex, PR #10 cycle 5). Typing uses the current values only."""

    def __init__(self) -> None:
        self._held: dict[str, tuple[tuple[int, int], Loaded]] = {}
        self._ever: dict[tuple[str, str, str], None] = {}
        self._redaction = Redaction()

    def get(self, path: str | None) -> Loaded:
        if not path:
            return NONE
        try:
            stat = os.stat(path)
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            stamp = (-1, -1)
        held = self._held.get(path)
        if held is None or held[0] != stamp:
            held = (stamp, load(path))
            self._held[path] = held
            if held[1].problem is None:
                self.remember(held[1])
        return held[1]

    def remember(self, loaded: Loaded) -> None:
        """Keep the values of a file that loaded (a deduplicated set, for the daemon's life)."""
        fresh = [e for e in loaded.entries() if e not in self._ever]
        if fresh:
            self._ever.update(dict.fromkeys(fresh))
            self._redaction = Redaction(tuple(self._ever))

    def scrub(self, text: str) -> str:
        """The text without any value ever loaded (a log line has no call whose accounts to use)."""
        return self._redaction.scrub(text)

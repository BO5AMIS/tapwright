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


# ---- the matcher (Codex, PR #10 cycle 10) ----
# A value is found in a text by one regular expression that follows it character by character, each character in any of
# the shapes the ways out give it: as itself (case kept), percent-encoded (each UTF-8 byte as %HH, hex in either case, up
# to twice encoded again: %25HH, %2525HH), JSON- or Python-escaped (\uXXXX, \xHH, \UXXXXXXXX, \n, \", \', \/, \\, behind
# the backslashes of up to ESCAPE_DEPTH escapings, themselves possibly percent-encoded), a run of whitespace as any run of whitespace, `+` or
# their encodings, and an invisible direction mark (as itself, encoded or escaped) allowed between any two characters.
# Each encoder works character by character, so a composition of them (a repr in a JSON document, a JSON string in a
# URL, an encodeURIComponent that leaves an apostrophe as it is) is still the value one character at a time: no list of
# whole forms, no token to cut out and decode. Every alternative for a value's first character starts with a literal
# character, so the regex engine skips ahead to where one could start (its first-character set) instead of trying the
# whole expression at every position of a log line.

_PCT = "%(?:25){0,2}"  # a percent sign, as itself or encoded once or twice again
ESCAPE_DEPTH = 4  # how many times an escape may have been escaped again (each doubles its backslashes)
# more backslashes, as themselves or percent-encoded: bounded, so a long run of backslashes in a text is never taken whole
# and given back one at a time at each of its positions (that was quadratic: 5,000 backslashes took half a second)
_BACKSLASHES = r"(?:\\|" + _PCT + "5[cC])" + f"{{0,{2 ** ESCAPE_DEPTH - 1}}}"
_LEADS = (r"\\", _PCT + "5[cC]")  # an escape's first backslash, as itself or percent-encoded
_SIMPLE = {"\n": "n", "\r": "r", "\t": "t", "\b": "b", "\f": "f", "\v": "v", "\a": "a", "\0": "0"}
_MARK_ESCAPES = "(?:u|U0000)20(?:0[eEfF]|2[a-eA-E]|6[6-9])"
_MARKS = "|".join(["[‎‏‪-‮⁦-⁩]",  # the direction marks ui._text drops, as themselves,
                   f"{_PCT}[eE]2{_PCT}80{_PCT}(?:8[eEfF]|[aA][a-eA-E])|{_PCT}[eE]2{_PCT}81{_PCT}[aA][6-9]",  # encoded
                   *(lead + _BACKSLASHES + _MARK_ESCAPES for lead in _LEADS)])  # or escaped
_BETWEEN = f"(?:{_MARKS})*"


def _hex(number: int, width: int) -> str:
    return "".join(f"[{d}{d.upper()}]" if d.isalpha() else d for d in f"{number:0{width}x}")


def _escaped(escapes: list[str]) -> list[str]:
    """An escape (one of `escapes`, after its backslashes), each way its first backslash can be written."""
    tail = _BACKSLASHES + "(?:" + "|".join(escapes) + ")"
    return [lead + tail for lead in _LEADS]


def _encoded(c: str) -> list[str]:
    """Every shape of one character but itself (percent-encoded, escaped), each starting with a literal character."""
    o = ord(c)
    shapes = ["".join(_PCT + _hex(b, 2) for b in c.encode("utf-8", "surrogatepass"))]
    escapes = [f"U{_hex(o, 8)}"]
    if o < 0x10000:
        escapes.append(f"u{_hex(o, 4)}")
    if o < 0x100:
        escapes.append(f"x{_hex(o, 2)}")
    if c in _SIMPLE:
        escapes.append(_SIMPLE[c])
    if c in "\"'/":
        escapes.append(f"{re.escape(c)}|{_PCT}{_hex(o, 2)}")  # \" \' \/ (the character itself perhaps encoded)
    if o > 0xFFFF:  # JSON's surrogate pair
        high, low = 0xD800 + ((o - 0x10000) >> 10), 0xDC00 + ((o - 0x10000) & 0x3FF)
        escapes.append(f"u{_hex(high, 4)}" + "(?:" + "|".join(_escaped([f"u{_hex(low, 4)}"])) + ")")
    return shapes + _escaped(escapes)


def _char(c: str) -> str:
    return "(?:" + "|".join([re.escape(c), *_encoded(c)]) + ")"


_ONE_BACKSLASH = "(?:u005[cC]|x5[cC]|U0000005[cC])?"  # (after a backslash, u005c or x5c makes it one escaped)


def _backslashes(count: int, first: bool = False) -> str:
    """A run of `count` backslashes in a value: as many in the text, or up to 2 ** ESCAPE_DEPTH times as many (each
    escaping doubles them), each as itself, percent-encoded or escaped, a direction mark allowed between two. One bounded
    repeat for the whole run, never one per backslash of the value (a long run in a text split among them every way
    there is). A run the value starts with is found only from the start of a run in the text (`_RUN_START`), with no
    upper bound: tried once per run, not at each of its backslashes (that was quadratic: a second for 20,000)."""
    unit = f"(?:\\\\{_ONE_BACKSLASH}|{_PCT}5[cC]{_ONE_BACKSLASH})"
    if first:
        return _RUN_START + unit + f"(?:{_BETWEEN}{unit}){{{count - 1},}}"
    return unit + f"(?:{_BETWEEN}{unit}){{{count - 1},{count * 2 ** ESCAPE_DEPTH - 1}}}"


# not just after a backslash, as itself, percent-encoded or escaped (the start of a run of them)
_RUN_START = "".join(f"(?<!{before})" for before in (
    r"\\", "%5[cC]", "%255[cC]", "%25255[cC]", r"\\u005[cC]", r"\\x5[cC]", r"\\U0000005[cC]"))


_WHITESPACE = " \t\n\r\f\v+"  # (a run collapsed or reflowed can hold any of them, each escaped or encoded later)


def _space(run: str) -> str:
    """A run of whitespace in a value: any run of whitespace, a `+`, or their encodings (a line collapsed, a form encoded)."""
    shapes = {s for c in {*run, *_WHITESPACE} for s in _encoded(c)}
    unit = r"(?:\s|\+|" + "|".join(sorted(shapes)) + ")"
    return unit + f"(?:{_BETWEEN}{unit})*"  # (a direction mark may sit anywhere inside the run too, Codex, PR #10 cycle 11)


@functools.lru_cache(maxsize=512)
def pattern(value: str) -> str | None:
    """The regular expression that finds `value` in any of its shapes (see above), or None when nothing of it is left to
    find. Its direction marks are dropped first (the value is found with or without them), and whitespace at its ends
    when what is between is long enough to be scrubbed alone."""
    core = value.translate(BIDI_MARKS)
    if len(core.strip()) >= MIN_SCRUBBED:
        core = core.strip()
    if not core:
        return None
    parts = [_space(run) if run[0].isspace() else _backslashes(len(run), n == 0) if run[0] == "\\" else _char(run)
             for n, run in enumerate(re.findall(r"\s+|\\+|\S", core))]
    return _BETWEEN.join(parts)


_QUOTE = re.compile(r"(?:\\|" + _PCT + "5[cC])*(?:['\"]|" + _PCT + "2[27])")  # a quote, escaped or encoded or not
_QUOTE_LONGEST = 16


def _quoted(text: str, start: int, end: int) -> tuple[int, int]:
    """The span of a value found at text[start:end], with the quotes around it when it is quoted whole (`'v'`, `\\"v\\"`,
    `%22v%22`): what repr or JSON writes of the value alone is replaced quotes and all, as before cycle 10."""
    for k in range(min(start, _QUOTE_LONGEST), 0, -1):  # (the longest quote first: `\\"` rather than `"`)
        quote = text[start - k:start]
        if text.startswith(quote, end) and _QUOTE.fullmatch(quote):
            return start - k, end + k
    return start, end


@functools.lru_cache(maxsize=512)
def _compiled(made: str) -> "re.Pattern[str]":
    return re.compile(made)


class Redaction:
    """A set of account values, as (name, kind, value), and what redacts them: each value in any of its shapes (`pattern`)
    becomes `<name's username>` / `<name's password>`, the longest value first, so a value that holds another is replaced
    whole. One compiled expression per value: each keeps the engine's skip to its first-character set, which one
    alternation of them all loses."""

    def __init__(self, entries: "tuple[tuple[str, str, str], ...]" = ()) -> None:
        found: dict[str, str] = {}
        for name, kind, value in sorted(entries, key=lambda e: -len(e[2])):
            if (made := pattern(value)) is not None:
                found.setdefault(made, f"<{name}'s {kind}>")
        self._each = [(_compiled(made), label) for made, label in found.items()]
        shown = sorted(set(found.values()), key=len, reverse=True)
        self.placeholders = re.compile("(" + "|".join(map(re.escape, shown)) + ")") if shown else None

    @property
    def redacts(self) -> bool:
        return bool(self._each)

    def _spans(self, text: str) -> list[tuple[int, int, str]]:
        """Where the values are in `text`, as (start, end, placeholder), in the text's order. Overlapping matches are
        merged into one span (the first one's placeholder), so no piece of either value is left out of it: a username
        `abcdef` and a password `cdefgh` in `abcdefgh` leave no `gh` behind."""
        found: list[tuple[int, int, int, int, str]] = []  # (start, -length, precedence, end, label)
        for rank, (each, label) in enumerate(self._each):
            for m in each.finditer(text):
                start, end = _quoted(text, m.start(), m.end())
                found.append((start, start - end, rank, end, label))
        found.sort()
        taken: list[tuple[int, int, str]] = []
        reached = 0  # one ordered sweep with a running end: linear in the matches, not one scan of the taken for each
        for start, _, _, end, label in found:
            if start >= reached:
                taken.append((start, end, label))
                reached = end
            elif end > reached:  # overlaps the span before and runs past it: that span grows to cover it
                taken[-1] = (taken[-1][0], end, taken[-1][2])
                reached = end
        return taken

    def _scrubbed(self, part: str) -> str:
        out, at = [], 0
        for start, end, label in self._spans(part):
            out += [part[at:start], label]
            at = end
        return "".join(out) + part[at:]

    def scrub(self, text: str) -> str:
        """Idempotent: a placeholder already there is left alone, so text redacted where it came in can be redacted again
        where it goes out (an account called `tester` with the password `test` doesn't become `<<tester's ...`)."""
        if not text or not any(each.search(text) for each, _ in self._each):
            return text
        parts = self.placeholders.split(text)  # (even: text between placeholders; odd: a placeholder, kept)
        for n in range(0, len(parts), 2):
            parts[n] = self._scrubbed(parts[n])
        return "".join(parts)

    def holds(self, text: str) -> bool:
        if not text:
            return False
        return any(each.search(text) for each, _ in self._each) or bool(
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

    def holds(self, text: str) -> bool:
        """Does the text hold any value ever loaded, or a placeholder?"""
        return self._redaction.holds(text)

    def scrub(self, text: str) -> str:
        """The text without any value ever loaded (a log line has no call whose accounts to use)."""
        return self._redaction.scrub(text)

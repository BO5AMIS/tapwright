"""Test accounts (AGENT-TOOLS §18.6): named accounts kept in a file on the user's computer (`--accounts FILE`). An agent
says `account: "tester2"`; the tool types the values. They are never in an answer, the action log, the map, a script, a
report or a caption: every answer is scrubbed of them, a name that holds one is never saved, and a step carries only the
placeholder `${account.username}` or `${account.password}`, filled in at the moment of typing.

  - A value is the whole text of a step, never part of one (joining text would get around the secure-field rule).
  - A password goes only into a secure field, a username only into a text field that isn't secure.
  - `apps` limits where an account may be used.
  - A file inside a git work tree that doesn't ignore it loads no accounts: any use says so.
  - `phone.passcode` is exposed nowhere, and typed by the tool itself only on the lock screen's passcode entry (unlock.py,
    Tapwright 1.7), where it shows as dots. It is not in the text redactor: a 4 to 6 digit passcode replaced in answers
    would turn every date, year and price on screen into a test of it (review 1.7, M2). Only the entry named `phone`
    holds it.

What the redaction guarantees (the threat model; Codex, PR #10 cycles 14, 15, 20 and 21):
  - Values: a username or password is 3 to 256 characters: at most 256 as written in the file, at least 3 once decoded,
    without direction marks and trimmed. A value still escaped after the work limit's passes is refused too. A file
    holds at most 32 accounts (64 values; the `phone` entry isn't counted). A file that breaks a limit loads no
    accounts, and says which account and field (or how many accounts), never a value.
  - Encodings: a value is found as it is; with whitespace normalized (any run of whitespace or `+` is one space, the
    ends may be trimmed) and direction marks anywhere; percent-encoded (%HH, either case, UTF-8); with JSON or Python
    string escapes (\\uXXXX and surrogate pairs, \\UXXXXXXXX, \\xHH, \\\\, \\", \\', \\/, \\b, \\f, \\n, \\t, \\r);
    and any composition of those, to any depth within the work limit: each layer is a whole-text encoder (quote,
    quote_plus, json.dumps, repr, ascii, ...) or a different escape for each character. Only a recognized escape
    is decoded: any other backslash is a literal character on both sides, so `pass\\word` is not `password`.
  - Work limit: a text is decoded at most 16 times (MAX_PASSES). If a 17th pass would still change it, every
    whitespace-separated token that holds what it would change is redacted whole (as UNCHECKED) and `holds` is true:
    nothing is released that wasn't checked to the end. Each level is searched in time linear in its length and the
    values' (Knuth-Morris-Pratt for overlapping occurrences), so a text costs at most 17 such searches.
  - Not covered:
    - other encodings (base64, HTML entities, hashing, case changes, a value split by other text);
    - a value's backslash left bare, or percent-encoded under a JSON or Python layer, right before an escaped
      character (`\\` + `\\x73`): JSON and Python read `\\\\x73` as an escaped backslash and `x73`, and so does the
      matcher. No encoder writes that: one that escapes a character escapes the backslash too;
    - text around a value that ends or starts with an incomplete escape (`%2`, `\\`, `\\u00`) and so is decoded
      together with the value's first or last characters (boundary fusion); the matcher catches the common cases of
      this as a best effort, without a guarantee. No encoder writes an escape cut short next to a value (Codex, PR #10
      cycle 21).

The vault (§20, Tapwright 1.7): a version 2 file is `{"version": 2, "vault": {...}}`, kept plain or protected (vault.py:
Windows DPAPI, the macOS keychain). Its kinds are account, card, address, identity, note, fixture and phone. Every value
of every kind (but the phone's passcode, typed nowhere but the lock screen) joins the same redactor as an account's, under
the same limits, shown as `<entry's field>`. A step types one by reference, `${vault.<name>.<field>}`, as the whole text
(`${account.<field>}` stays, for drive's account): a password only into a secure field, a username only into one that
isn't, a fixture never into a secure field, a card only with a person's OK each time (the daemon asks), `apps` limiting
any field. A plain field (an address's, an identity's name, a fixture's) may be shorter than MIN_SCRUBBED; such a value
can't be kept out of text, and is typed all the same (as Tapline does). The top-level `otp` key is Tapwright's one-time
code setting (§24.3), read here and used by otp.py.

Pure but for reading the file, asking git and (for a protected file) the system's protection: no phone.
"""
from __future__ import annotations

import bisect
import functools
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from .control import Refused

VERSION = 1
NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
FIELDS = ("username", "password", "passcode", "apps")
PLACEHOLDER = re.compile(r"\$\{account\.(username|password)\}")
MENTION = re.compile(r"\$\{(?:account|vault|otp)\.")
MIN_SCRUBBED = 3  # a value shorter than this isn't scrubbed out of text (it would eat ordinary words)
MAX_VALUE = 256  # a username or password longer than this is refused when the file loads (no real credential is longer)
MAX_ACCOUNTS = 32  # a file with more accounts than this is refused when it loads (a test-account file holds a handful)
BIDI_MARKS = dict.fromkeys(map(ord, "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"))  # the invisible direction marks ui._text drops from screen text
PHONE = "phone"  # the entry that holds the phone's passcode, not an account to sign in with
NO_FILE = ("no test accounts: start tapwright serve or mcp with --accounts FILE (a JSON file kept outside any git repo, "
           "or ignored by it)")
GitRunner = Callable[[list[str], str], "subprocess.CompletedProcess[str] | None"]

# ---- the vault's kinds (§20.1) ----
VERSION2 = 2
KIND_FIELDS: dict[str, tuple[str, ...]] = {
    "account": ("username", "password"),
    "card": ("number", "expiry", "cvc"),
    "address": ("street", "city", "postcode", "country"),
    "identity": ("name", "phone", "email", "birthday"),
    "note": ("text",),
    "phone": ("passcode",),
}
FIXTURE = "fixture"  # (v1.16) plain test data: any fields, string values
# fields that may be shorter than MIN_SCRUBBED (Tapline's `plain`): typed, but too short to be kept out of text
PLAIN_FIELDS = frozenset({("address", f) for f in KIND_FIELDS["address"]} | {("identity", "name"), ("phone", "passcode")})
ENTRY_NAME = re.compile(r"^[A-Za-z0-9_]{1,64}$")  # an entry's (and a fixture field's) name: ASCII letters, digits, _
ENTRY_KEYS = frozenset({"kind", "apps", "explore"})  # (`explore`, 1.15 §25.6, is the explorer's: read, not used here)
MAX_VALUES = 2 * MAX_ACCOUNTS  # the values a version 2 file holds at most (the matcher's cost grows with them)
CARD = "card"
# a reference, as the whole text of a step: `${account.<field>}`, `${vault.<name>.<field>}`, `${otp.<app>}`
REFERENCE = re.compile(r"\$\{(?:account\.(username|password)|vault\.([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)"
                       r"|otp\.([^{}\s]+))\}")
CODE_LABEL = "one-time code"  # what a one-time code is shown as: `<one-time code>`


@dataclass(frozen=True)
class Account:
    name: str
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)
    apps: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Entry:
    """One vault entry (§20.1): its kind (a kind this tool doesn't know makes it unusable: `known` False), its values by
    field (never in the repr), and the apps any of them may be typed into (None: any given app)."""
    name: str
    kind: str
    values: dict[str, str] = field(default_factory=dict, repr=False)
    apps: tuple[str, ...] | None = None
    known: bool = True


@dataclass(frozen=True)
class OtpSetting:
    """Tapwright's one-time code setting for one app (§24.3): the bundle ids a code may come from, and a text the
    notification must hold (in its title, often the sender, or its body). An app without one has codes off. `pattern`
    (optional, Tapwright's own key): a regular expression for the code itself (its first group, if it has one), for a
    code that isn't 4 to 8 digits."""
    sources: tuple[str, ...]
    match: str
    pattern: "re.Pattern[str] | None" = None


@dataclass(frozen=True)
class Typing:
    """What a reference types: the value (never in the repr), and what it is: the entry, its kind and field (for the
    audit's `vault` line and the card's OK), or the app of a one-time code."""
    value: str = field(repr=False)
    entry: str | None = None
    kind: str | None = None
    field: str | None = None
    otp_app: str | None = None


@dataclass(frozen=True)
class Loaded:
    """What a file holds: the accounts (by name, in the file's order), the passcode, and why none can be used (None when
    all is well). Nothing here shows a value in its repr. A version 2 file (the vault) also has every entry by name
    (its accounts are in `accounts` too), the `otp` settings, how it is kept (`protection`: "dpapi-user", "keychain" or
    None for plain) and the note a plain file gets at every start."""
    path: str = ""
    accounts: dict[str, Account] = field(default_factory=dict, repr=False)
    passcode: str | None = field(default=None, repr=False)
    problem: str | None = None
    vault: dict[str, Entry] = field(default_factory=dict, repr=False)
    otp: dict[str, OtpSetting] = field(default_factory=dict)
    protection: str | None = None
    note: str | None = None
    version: int = VERSION

    @property
    def names(self) -> tuple[str, ...]:
        """The accounts an agent may name (a problem leaves none)."""
        if self.problem is not None:
            return ()
        return tuple(n for n, a in self.accounts.items() if a.username or a.password)

    @property
    def entry_names(self) -> tuple[str, ...]:
        """Every vault entry's name, with its kind (`tester2 (account)`), for `map` and `session_report`: names only."""
        if self.problem is not None:
            return ()
        return tuple(f"{n} ({e.kind})" for n, e in self.vault.items() if e.kind != "phone")

    def account(self, name: str, bundle: str | None) -> Account:
        """The account called `name`, for use in app `bundle`. `bad_params` when no file is given or the file can't be used,
        `not_found` for a name it hasn't, `not_allowed` for an app the account isn't for."""
        if not self.path:
            raise Refused("bad_params", NO_FILE)
        if self.problem is not None:
            raise Refused("bad_params", self.problem)
        other = self.vault.get(name)
        if other is not None and other.kind not in ("account", "phone"):  # (the phone entry is no account: not_found)
            article = "an" if other.kind[:1] in "aeiou" else "a"
            raise Refused("bad_params", f'"account": "{name}" names {article} {other.kind} entry; "account" names account '
                                        f"entries only (use ${{vault.{name}.<field>}}).")
        if name not in self.names:
            raise Refused("not_found", f"no account called {name}; the accounts are: {', '.join(self.names) or 'none'}")
        found = self.accounts[name]
        if found.apps is not None and bundle not in found.apps:
            raise Refused("not_allowed", f"account {name} may be used only in {', '.join(found.apps)}; this is {bundle or 'no app'}")
        return found

    def entries(self) -> tuple[tuple[str, str, str], ...]:
        """(entry name, field, value) of every value this file holds that is kept out of text: an account's username and
        password, and in a vault every other kind's fields too. Never the phone's passcode (review 1.7, M2): it is
        typed only on the lock screen's keypad, where it shows as dots, so it never reaches text, and replacing a 4 to 6
        digit number in every answer would tell the agent the passcode the first time a date or a price showed it."""
        found = _entries(self.accounts)
        found += tuple((e.name, f, v) for e in self.vault.values() if e.kind not in ("account", "phone")
                       for f, v in e.values.items() if v and not weak(v))
        return found

    def reference(self, text: str, active: Account | None, secure: bool, ref: str, bundle: str | None) -> Typing | None:
        """What a step's text types when it is a reference (`${account.…}`, `${vault.…}`, `${otp.…}`), checked against
        the field it goes into (`secure`, its `ref`) and the app in front (`bundle`); None for ordinary text. A card's
        value comes back like any other: the daemon asks a person before it types it (§20.1). `${otp.…}` comes back with
        `otp_app` set and no value: the daemon gets the code (otp.py)."""
        if not mentions(text):
            return None
        whole = REFERENCE.fullmatch(text)
        if whole is None:
            if text.count("${") == 1 and "${otp." in text:
                raise Refused("bad_params", "a one-time code is the whole text of a step, never part of one: "
                                            "\"text\": \"${otp.<app id>}\"")
            raise Refused("bad_params", "an account's value is the whole text of a step, never part of one: "
                                        "\"text\": \"${account.username}\" works, \"hi ${account.password}\" doesn't")
        account_field, name, wanted, otp_app = whole.groups()
        if account_field is not None:
            return Typing(typed(text, active, secure, ref), active.name if active else None, "account", account_field)
        if otp_app is not None:
            if otp_app != bundle:
                raise Refused("not_allowed", f"${{otp.{otp_app}}} is typed only into {otp_app}; this is {bundle or 'no app'}. "
                                             "Nothing was typed.")
            if otp_app not in self.otp:
                raise Refused("bad_params", f"one-time codes are off for {otp_app}: the vault file's \"otp\" setting turns "
                                            "them on for an app (Tapwright §24).")
            return Typing("", otp_app=otp_app)
        return self._vault_value(name, wanted, secure, ref, bundle)

    def _vault_value(self, name: str, wanted: str, secure: bool, ref: str, bundle: str | None) -> Typing:
        if not self.path:
            raise Refused("bad_params", NO_FILE)
        if self.problem is not None:
            raise Refused("bad_params", self.problem)
        entry = self.vault.get(name)
        if entry is None:
            names = ", ".join(n for n, e in self.vault.items() if e.kind != "phone") or "none"
            raise Refused("not_found", f'no vault entry "{name}"; the entries are: {names}')
        if entry.kind == "phone":
            raise Refused("bad_params", "the phone's passcode is typed by the tool itself, only on the lock screen's keypad; "
                                        "no step types it.")
        if not entry.known:
            raise Refused("bad_params", f'vault entry "{name}" is of a kind this Tapwright doesn\'t know ({entry.kind}); '
                                        "it can't be used.")
        fields = KIND_FIELDS.get(entry.kind)
        if fields is not None and wanted not in fields:
            article = "an" if entry.kind[:1] in "aeiou" else "a"
            raise Refused("bad_params", f'vault entry "{name}" is {article} {entry.kind}; its fields are {", ".join(fields)}.')
        value = entry.values.get(wanted)
        if not value:
            raise Refused("bad_params", f'vault entry "{name}" has no {wanted}.')
        if entry.apps is not None and bundle not in entry.apps:
            raise Refused("not_allowed", f'vault entry "{name}" may be typed only in {", ".join(entry.apps)}; this is '
                                         f"{bundle or 'no app'}. Nothing was typed.")
        if entry.kind == "account" and wanted == "password" and not secure:
            raise Refused("bad_params", f"an account's password goes only into a password field; {ref} is a text field.")
        if entry.kind == "account" and wanted == "username" and secure:
            raise Refused("bad_params", f"an account's username goes only into a text field that isn't secure; {ref} is a "
                                        "password field.")
        if entry.kind == FIXTURE and secure:
            raise Refused("bad_params", f"a fixture is plain test data and never goes into a password field; {ref} is one.")
        return Typing(value, name, entry.kind, wanted)

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


# ---- the matcher (Codex, PR #10 cycles 14 and 15) ----
# A value is found by decoding, not by a regular expression of its shapes. A text is read at several levels: as it is
# (level 0), then decoded once, twice, ... until a pass changes nothing. A pass reads the text left to right, as JSON and
# Python read a string, and decodes percent escapes (%HH in either case, consecutive bytes read as UTF-8 where they are
# valid, else kept as they are) and JSON or Python escapes (\uXXXX, a surrogate pair, \UXXXXXXXX, \xHH, \\ \" \' \/ \b \f
# \n \t \r). Any other backslash is a literal character, kept as it is on both sides: `pass\word` is not `password`. So
# an escape escaped again (`\\u00e9`, `%2525`) is undone one level per pass. Each level is then normalized for matching:
# direction marks dropped, and every run of whitespace or `+` made one space (`+` is a space on both sides, so a
# form-encoded value is found and a value with a `+` matches its spaces). A value's forms are its own levels, normalized
# the same way. Every form is looked for in every level (Knuth-Morris-Pratt past the first occurrence, so overlapping
# occurrences are found with each character read once), and each occurrence maps back to the original text through
# each level's position map: canonical character i came from text[starts[i]:ends[i]]. So the value as it is, decoded or
# encoded any number of times, by any composition of those encoders, is found, and the redaction covers every character
# of its shape in the original text.
# The work limit: a text is decoded at most MAX_PASSES times (each pass is linear and makes the text shorter). When a
# further pass would still change it, nothing that still decodes is released unchecked: each whitespace-separated token
# of the original that holds such an escape is taken for a value, redacted whole (as UNCHECKED), and `holds` is true.
# Decoding reads a text without knowing where a value starts, so an escape can take in characters of the value with
# characters of the text around it: `%2` + `abc` reads `%2a` + `bc`, `\` + `tab` a tab + `ab`, `ab%2` + `F` reads `ab/`,
# and both at once: `%2` + `a%62c%2` + `F` (the value `abc%2`) reads `*bc/`. That is boundary fusion: outside
# the guarantee (no encoder writes an escape cut short next to a value) and caught as a best effort. In a decoded level
# each character an escape made is checked too: when the escape's text ends with a value's first characters and the level goes on with
# the rest, or with the rest but its last characters, which a later escape's text starts with (or the mirror, at the
# value's end alone), the value is there, and the escapes are redacted with it. Each such check is a few dictionary
# lookups, by the exact characters the escape's text ends (starts) with and the value's character that must come next
# (before), then exact comparisons on the forms found: linear in the escapes, never a scan of every value. Escapes at
# one place with one text are checked once. Matching is case-sensitive but for the hex of an escape.

MAX_PASSES = 16  # the work limit: decoding passes over one text (17 levels, the text as it is included)
UNCHECKED = "<a value encoded too deep to check>"  # what a token that still decodes at the work limit is redacted as
_MARK_CLASS = "".join(map(chr, BIDI_MARKS))  # (the direction marks, as ui._text drops them)
_ESCAPE = re.compile(  # (left to right: `\\u0041` is an escaped backslash, then `u0041`)
    r"(?:%[0-9A-Fa-f]{2})+"
    r"|\\(?:u([dD][89abAB][0-9A-Fa-f]{2})\\u([dD][c-fC-F][0-9A-Fa-f]{2})"
    r"|u([0-9A-Fa-f]{4})|U([0-9A-Fa-f]{8})|x([0-9A-Fa-f]{2})|([\\\"'/bfnrt]))")
_SIMPLE = {"\\": "\\", '"': '"', "'": "'", "/": "/", "b": "\b", "f": "\f", "n": "\n", "t": "\t", "r": "\r"}
_LOOSE = re.compile(r"[\s+" + _MARK_CLASS + "]+")  # what normalizing changes: whitespace, `+`, direction marks
_SPACE = re.compile(r"[\s+]")


class _Layer:
    """One step from a text `source` to `out`: pieces of (source start, source end, kind), kind 0 for a run copied as it
    is, 1 for one character made from the whole piece, 2 for a piece dropped. The position map is built only when an
    occurrence has to be mapped."""

    __slots__ = ("source", "out", "_pieces", "_maps", "_kept", "_slots", "_index")

    def __init__(self, source: str, out: str, pieces: list[tuple[int, int, int]]) -> None:
        self.source = source
        self.out = out
        self._pieces = pieces
        self._maps: tuple[list[int], list[int], list[int], list[int]] | None = None
        self._kept: list[int] | None = None
        self._slots: list[int] | None = None
        self._index: tuple[list[int], list[int], list[int], list[int], list[int], list[int]] | None = None

    def changed(self) -> "Iterator[tuple[int, int]]":
        """The source ranges this layer doesn't copy as they are."""
        return ((a, b) for a, b, kind in self._pieces if kind)

    def slots(self) -> list[int]:
        """For each source character, where its piece is in the output: 2 * position, plus 1 when the piece is an output
        character (copied, or a whitespace run made one space), 0 when it is dropped (a direction mark)."""
        if self._slots is None:
            slots = [0] * len(self.source)
            at = 0
            for a, b, kind in self._pieces:
                if kind == 0:
                    slots[a:b] = range(2 * at + 1, 2 * (at + b - a) + 1, 2)
                    at += b - a
                elif kind == 1:
                    slots[a:b] = [2 * at + 1] * (b - a)
                    at += 1
                else:
                    slots[a:b] = [2 * at] * (b - a)
            self._slots = slots
        return self._slots

    def kept(self) -> list[int]:
        """For each source character, the output character it was copied to as it is, or -1 (made into another, or
        dropped)."""
        if self._kept is None:
            where = [-1] * len(self.source)
            at = 0
            for a, b, kind in self._pieces:
                if kind == 0:
                    where[a:b] = range(at, at + b - a)
                    at += b - a
                elif kind == 1:
                    at += 1
            self._kept = where
        return self._kept

    def carried(self, made: "list[tuple[int, str, bool]]") -> "list[tuple[int, str, bool]]":
        """The characters an escape made, as (output index, the escape's text, dropped): those of the source (`made`, in
        order) this layer copies as they are, then the ones it makes itself, in order of output. One that a layer
        dropped (a direction mark an escape made, dropped before the next pass) is kept as dropped at the place it was,
        so an escape that took in a value's edge and made a mark is still known (Codex, PR #10 cycle 15)."""
        out: list[tuple[int, str, bool]] = []
        where, slots, n = self.kept(), self.slots(), len(self.source)
        for x, raw, gone in made:
            if x >= n:
                out.append((len(self.out), raw, True))
            elif not gone and where[x] >= 0:
                out.append((where[x], raw, False))
            elif gone or slots[x] % 2 == 0:
                out.append((slots[x] // 2, raw, True))
        at = 0
        for a, b, kind in self._pieces:
            if kind == 1:
                out.append((at, self.source[a:b], False))
                at += 1
            elif kind == 0:
                at += b - a
        out.sort()
        return out

    def span(self, i: int, j: int, lead: bool = False, trail: bool = False) -> tuple[int, int]:
        """The source range that output characters i:j came from; with `lead` (`trail`) it reaches over a dropped piece
        just before (after) them. A lookup by bisection: no map of every character is built."""
        if self._index is None:
            outs, starts, ends, kinds, wide_starts, wide_ends = [], [], [], [], [], []  # (per piece that isn't dropped)
            at, pending = 0, -1
            for a, b, kind in self._pieces:
                if kind == 2:
                    if wide_ends:
                        wide_ends[-1] = b
                    if pending < 0:
                        pending = a
                    continue
                outs.append(at)
                starts.append(a)
                ends.append(b)
                kinds.append(kind)
                wide_starts.append(pending if pending >= 0 else a)
                wide_ends.append(b)
                pending = -1
                at += b - a if kind == 0 else 1
            self._index = (outs, starts, ends, kinds, wide_starts, wide_ends)
        outs, starts, ends, kinds, wide_starts, wide_ends = self._index
        p = bisect.bisect_right(outs, i) - 1
        a = wide_starts[p] if lead and i == outs[p] else starts[p] + (i - outs[p] if kinds[p] == 0 else 0)
        q = bisect.bisect_right(outs, j - 1) - 1
        b = starts[q] + j - outs[q] if kinds[q] == 0 else ends[q]
        return a, wide_ends[q] if trail and b == ends[q] else b

    def maps(self) -> tuple[list[int], list[int], list[int], list[int]]:
        """(starts, ends, wide starts, wide ends): output character i came from source[starts[i]:ends[i]]; the wide ones
        reach over a dropped piece just before or after it (a value that starts or ends with a direction mark)."""
        if self._maps is None:
            starts: list[int] = []
            ends: list[int] = []
            wide_starts: list[int] = []
            wide_ends: list[int] = []
            pending = -1
            for a, b, kind in self._pieces:
                if kind == 2:
                    if wide_ends:
                        wide_ends[-1] = b
                    if pending < 0:
                        pending = a
                    continue
                first = len(starts)
                if kind == 0:
                    starts.extend(range(a, b))
                    ends.extend(range(a + 1, b + 1))
                    wide_starts.extend(range(a, b))
                    wide_ends.extend(range(a + 1, b + 1))
                else:
                    starts.append(a)
                    ends.append(b)
                    wide_starts.append(a)
                    wide_ends.append(b)
                if pending >= 0 and len(starts) > first:
                    wide_starts[first] = pending
                pending = -1
            self._maps = (starts, ends, wide_starts, wide_ends)
        return self._maps


def _build(source: str, changes: "list[tuple[int, int, str]]") -> _Layer:
    """The layer that makes each source[a:b] of `changes` (in order, apart) into its text: one character, or none."""
    out: list[str] = []
    pieces: list[tuple[int, int, int]] = []
    at = 0
    for a, b, made in changes:
        if a > at:
            out.append(source[at:a])
            pieces.append((at, a, 0))
        out.append(made)
        pieces.append((a, b, 1 if made else 2))
        at = b
    if at < len(source):
        out.append(source[at:])
        pieces.append((at, len(source), 0))
    return _Layer(source, "".join(out), pieces)


def _percent(run: str, at: int, changes: "list[tuple[int, int, str]]") -> None:
    """A run of %HH starting at `at`: each valid UTF-8 character it encodes, from the bytes that encode it."""
    raw = bytes.fromhex(run.replace("%", ""))
    for c in raw.decode("utf-8", "surrogateescape"):
        if "\udc80" <= c <= "\udcff":
            at += 3  # (a byte that isn't UTF-8 here is kept as it is)
            continue
        width = 3 * len(c.encode("utf-8"))
        changes.append((at, at + width, c))
        at += width


def _decoded(text: str) -> _Layer | None:
    """One decoding pass, or None when it changes nothing."""
    changes: list[tuple[int, int, str]] = []
    for m in _ESCAPE.finditer(text):
        if m.group(0)[0] == "%":
            _percent(m.group(0), m.start(), changes)
            continue
        high, low, u, big, x, simple = m.groups()
        if high is not None:
            o = 0x10000 + ((int(high, 16) - 0xD800) << 10) + (int(low, 16) - 0xDC00)
        elif simple is not None:
            changes.append((m.start(), m.end(), _SIMPLE[simple]))
            continue
        else:
            o = int(u or big or x, 16)
            if o > 0x10FFFF:
                continue
        changes.append((m.start(), m.end(), chr(o)))
    # a surrogate pair whose halves were decoded in different passes (`\ud83d%5Cude00`): two characters now, one again
    taken = [(a, b) for a, b, _ in changes]  # (in text order, apart: finditer's)
    k = 0  # one sweep over both, in text order: linear, not a scan of every escape for each pair (Codex, cycle 17)
    for m in _HALVES.finditer(text):
        while k < len(taken) and taken[k][1] <= m.start():
            k += 1
        if not (k < len(taken) and taken[k][0] < m.end()):
            high, low = map(ord, m.group(0))
            changes.append((m.start(), m.end(), chr(0x10000 + ((high - 0xD800) << 10) + (low - 0xDC00))))
    changes.sort()
    return _build(text, changes) if changes else None


_HALVES = re.compile("[\ud800-\udbff][\udc00-\udfff]")


_MARKS = re.compile("[" + _MARK_CLASS + "]+")


def _unmarked(text: str) -> _Layer | None:
    """The text without its direction marks, or None when it has none."""
    changes = [(m.start(), m.end(), "") for m in _MARKS.finditer(text)]
    return _build(text, changes) if changes else None


def _normalized(text: str) -> _Layer:
    """Direction marks dropped, each run of whitespace or `+` one space."""
    changes = []
    for m in _LOOSE.finditer(text):
        run = m.group(0)
        if run != " ":
            changes.append((m.start(), m.end(), " " if _SPACE.search(run) else ""))
    return _build(text, changes)




def _levels(text: str) -> "Iterator[tuple[_Layer | None, list[_Layer]]]":
    """Each level of `text` as (its normalized layer, the decoding layers that led to it, outermost last), level 0 first,
    until a pass changes nothing. At the work limit, when one more pass would still change the text, last comes
    (None, the chain with that pass): what it changes was never checked."""
    chain: list[_Layer] = []
    current = text
    for passes in range(MAX_PASSES + 1):
        yield _normalized(current), chain
        unmarked = _unmarked(current)  # (a mark is invisible: backslash, mark, backslash is an escaped backslash)
        step = _decoded(unmarked.out if unmarked is not None else current)
        if step is None:
            return
        chain = [*chain, *([unmarked] if unmarked is not None else []), step]
        if passes == MAX_PASSES:
            yield None, chain
            return
        current = step.out


def _back(chain: "list[_Layer]", a: int, b: int, lead: bool = False, trail: bool = False) -> tuple[int, int]:
    """The original text's range that the range a:b of the chain's last text came from (with `lead` and `trail`,
    reaching over a direction mark dropped just before or after it, at any level)."""
    for step in reversed(chain):
        a, b = step.span(a, b, lead, trail)
    return a, b


def _unchecked(text: str, chain: "list[_Layer]") -> "list[tuple[int, int]]":
    """The whitespace-separated tokens of `text` that hold an escape still decoding at the work limit (`chain`'s last
    pass changes them), in order, apart."""
    *done, last = chain
    spans = sorted(_back(done, a, b) for a, b in last.changed())
    out: list[tuple[int, int]] = []
    reached = 0  # (one sweep: each character is looked at a bounded number of times)
    for a, b in spans:
        if a < reached:
            if b <= reached:
                continue
            a = reached
        while a > reached and not text[a - 1].isspace():
            a -= 1
        while b < len(text) and not text[b].isspace():
            b += 1
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
        reached = b
    return out


def unsettled(text: str) -> bool:
    """Does decoding the text still change it at the work limit?"""
    return any(layer is None for layer, _ in _levels(text))


def _made(chain: "list[_Layer]", known: "list[list[tuple[int, str, bool]]]") -> "list[tuple[int, str, bool]]":
    """The characters escapes made in the last text of `chain` (`known` caches each level's)."""
    while len(known) <= len(chain):
        n = len(known)
        known.append(chain[n - 1].carried(known[n - 1]) if n else [])
    return known[len(chain)]


def canon(text: str) -> tuple[str, list[int], list[int]]:
    """The text decoded (to the work limit) and normalized, and where each of its characters came from: canonical
    character i is text[starts[i]:ends[i]]."""
    last: _Layer | None = None
    chain: list[_Layer] = []
    for layer, steps in _levels(text):
        if layer is not None:
            last, chain = layer, steps
    assert last is not None  # (level 0 is always there)
    starts, ends, _, _ = last.maps()
    for step in reversed(chain):
        s, e, _, _ = step.maps()
        starts, ends = [s[a] for a in starts], [e[b - 1] for b in ends]
    return last.out, starts, ends


def weak(value: str) -> bool:
    """Is the value too weak to redact safely: under MIN_SCRUBBED characters once decoded, normalized and stripped?"""
    return len(canon(value)[0].strip()) < MIN_SCRUBBED


def _forms(value: str) -> "list[tuple[str, bool, bool]]":
    """The value's forms to look for, as (form, starts with a dropped piece, ends with one): each level of it normalized,
    and that stripped when it is still long enough (a screen trims a value's ends)."""
    found: list[tuple[str, bool, bool]] = []
    for layer, _ in _levels(value):
        if layer is None or not layer.out:
            continue
        form = layer.out
        starts, ends, wide_starts, wide_ends = layer.maps()
        lead, trail = wide_starts[0] != starts[0], wide_ends[-1] != ends[-1]
        stripped = form.strip()
        if len(stripped) >= MIN_SCRUBBED:
            found.append((form, lead, trail))
            if stripped != form:
                found.append((stripped, lead and form[0] != " ", trail and form[-1] != " "))
    return found


# how many of a value's characters an escape around it can take in: the longest escape that makes one character is a
# percent-encoded 4-byte UTF-8 character, `%F0%9F%98%80` (12 characters), so up to 11 of them; `\\U` and 8 hex digits is
# 10 (Codex, PR #10 cycle 18: 8 missed `a%62c%F0%9F%98` + `%80`)
SEAM = 11


def _failure(needle: str) -> list[int]:
    """Knuth-Morris-Pratt's failure function: fail[q] is the length of the longest proper border of needle[:q]."""
    fail = [0] * (len(needle) + 1)
    k = 0
    for q in range(1, len(needle)):
        while k and needle[q] != needle[k]:
            k = fail[k]
        if needle[q] == needle[k]:
            k += 1
        fail[q + 1] = k
    return fail


def _occurrences(hay: str, needle: str, fail: list[int], merge: bool) -> "Iterator[tuple[int, int]]":
    """Every occurrence of `needle` in `hay`, overlapping ones too, as (start, end); with `merge` a chain of overlapping
    ones is one. O(len(hay) + len(needle)): `str.find` reaches the first occurrence, and from there Knuth-Morris-Pratt
    reads on (each character once) while a match that overlaps it can still be going on; then `find` again."""
    size, n = len(needle), len(hay)
    at = hay.find(needle)
    while at != -1:
        start, end = at, at + size
        q, pos = fail[size], end
        while q and pos < n:
            c = hay[pos]
            while q and needle[q] != c:
                q = fail[q]
            if needle[q] == c:
                q += 1
            pos += 1
            if q == size:
                if merge and pos - size < end:
                    end = pos
                else:
                    yield start, end
                    start, end = pos - size, pos
                q = fail[size]
        yield start, end
        at = hay.find(needle, pos) if pos < n else -1


def _starter(c: str) -> bool:
    """Can an escape's text start with `c`? Every escape (`_ESCAPE`) starts with `%` or a backslash, and a surrogate
    pair joined across passes (`_HALVES`) with its high half."""
    return c in "%\\" or "\ud800" <= c <= "\udbff"


_QUOTE = re.compile(r"(?:\\|%(?:25){0,2}5[cC])*(?:['\"]|%(?:25){0,2}2[27])")  # a quote, escaped or encoded or not
_QUOTE_LONGEST = 16


def _quoted(text: str, start: int, end: int) -> tuple[int, int]:
    """The span of a value found at text[start:end], with the quotes around it when it is quoted whole (`'v'`, `\\"v\\"`,
    `%22v%22`): what repr or JSON writes of the value alone is replaced quotes and all."""
    if start == 0 or text[start - 1] not in "'\"27":
        return start, end
    for k in range(min(start, _QUOTE_LONGEST), 0, -1):  # (the longest quote first: `\\"` rather than `"`)
        quote = text[start - k:start]
        if text.startswith(quote, end) and _QUOTE.fullmatch(quote):
            return start - k, end + k
    return start, end


def _inside(shown: "list[tuple[int, int]]", shown_starts: list[int], a: int, b: int) -> bool:
    """Does text[a:b] lie wholly inside one of the placeholders already in the text (`shown`, apart, in order)?"""
    k = bisect.bisect_right(shown_starts, a) - 1
    return k >= 0 and b <= shown[k][1]


def _firsts(level: _Layer, chain: "list[_Layer]", lead: bool) -> list[int]:
    """For each character of a level, where in the original text the range of an occurrence that starts with it starts
    (what `_back` gives as the start, with `lead` as there): never decreasing."""
    pick = 2 if lead else 0
    starts = level.maps()[pick]
    for step in reversed(chain):
        where = step.maps()[pick]
        starts = [where[a] for a in starts]
    return starts


def _kept(hay: str, form: str, fail: list[int], i: int, j: int, where: "Callable[[int, int], tuple[int, int]]",
          firsts: "Callable[[], list[int]]", shown: "list[tuple[int, int]]", shown_starts: list[int]
          ) -> "list[tuple[int, int]]":
    """The pieces of hay[i:j], a chain of overlapping occurrences of `form` that `_occurrences` merged, that cover every
    occurrence in it not wholly inside a placeholder already in the text, and nothing else of it: what the occurrences
    one by one would give, without going through each (Codex, PR #10 cycle 21: a placeholder anywhere in the text made
    every occurrence everywhere be listed, millions in a long run of one character). `where(s, t)` is the text range of
    hay[s:t], `firsts()` the starts of those ranges for every level character.
    Only an occurrence whose text starts inside a placeholder can lie inside one. The starts are in order, so those that
    do are, for each placeholder the chain meets, one range of the level (a window). Only the occurrences near a window
    are listed: each window widened by the value's length, which always holds an occurrence (two in a row overlap) that
    starts in no window. The occurrences between such stretches all count and overlap in a row: one piece."""
    size = len(form)
    last = j - size  # (where the chain's last occurrence starts)
    a, b = where(i, j)
    k = max(bisect.bisect_right(shown_starts, a) - 1, 0)
    windows: list[tuple[int, int]] = []
    starts: list[int] | None = None
    while k < len(shown) and shown[k][0] < b:
        p, q = shown[k]
        k += 1
        if q <= a or q - p < size:  # (before the chain, or too short to hold an occurrence: each level character has
            continue                # one text character or more)
        if starts is None:
            starts = firsts()
        lo, hi = bisect.bisect_left(starts, p, i, last + 1), bisect.bisect_left(starts, q, i, last + 1)
        if lo < hi:
            windows.append((max(i, lo - size), min(last + 1, hi + size)))
    if not windows:
        return [(i, j)]
    regions: list[tuple[int, int]] = []
    for lo, hi in windows:
        if regions and lo <= regions[-1][1]:
            regions[-1] = (regions[-1][0], max(regions[-1][1], hi))
        else:
            regions.append((lo, hi))
    pieces: list[tuple[int, int]] = []
    run_start, run_end = i, -1  # the piece being built; run_end -1: none yet, 0: it goes on to the next occurrence
    if regions[0][0] > i:
        run_end = 0  # (every occurrence before the first region counts)
    for lo, hi in regions:
        for s, t in _occurrences(hay[lo:min(j, hi - 1 + size)], form, fail, False):
            s, t = s + lo, t + lo
            if _inside(shown, shown_starts, *where(s, t)):
                if run_end > 0:
                    pieces.append((run_start, run_end))
                run_end = -1
            elif run_end < 0:
                run_start, run_end = s, t
            elif run_end == 0 or s < run_end:
                run_end = t
            else:
                pieces.append((run_start, run_end))
                run_start, run_end = s, t
        if hi <= last:
            run_end = 0  # (the region's last occurrence counted; so do all of them up to the next region)
    if run_end >= 0:
        pieces.append((run_start, j if run_end == 0 else run_end))
    return pieces


class Redaction:
    """A set of account values, as (name, kind, value), and what redacts them: each value, in any of its shapes, becomes
    `<name's username>` / `<name's password>`. Overlapping occurrences are merged into one span, the earliest and then the
    longest first (a value that holds another is replaced whole). A token still decoding at the work limit becomes
    UNCHECKED."""

    def __init__(self, entries: "tuple[tuple[str, str, str], ...]" = ()) -> None:
        forms: dict[str, tuple[int, str, bool, bool]] = {}
        labels: list[str] = []
        for name, kind, value in sorted(entries, key=lambda e: -len(e[2])):
            label = f"<{name}'s {kind}>" if name else f"<{kind}>"
            for form, lead, trail in _forms(value):
                if form not in forms:
                    forms[form] = (len(forms), label, lead, trail)
                    labels.append(label)
        self._forms = [(form, rank, label, lead, trail, _failure(form))
                       for form, (rank, label, lead, trail) in forms.items()]
        # (a value's first characters taken into an escape of the text before it, `%2` + `abc` read as `%2a` + `bc`, or
        # its last ones into an escape with the text after it, `ab%2` + `F`: by the exact characters the escape holds,
        # form[:k] (form[-k:]), then by the value's character just after them (before them). So an escape finds only
        # the forms that can go on where it is: a few lookups, never a scan of the values (Codex, PR #10 cycle 20).
        # A head entry is (form, k, rank, label, where in the form a later escape's text can start), a tail entry
        # (form, k, rank, label))
        self._heads: dict[str, dict[str, list[tuple[str, int, int, str, tuple[int, ...]]]]] = {}
        self._tails: dict[str, dict[str, list[tuple[str, int, int, str]]]] = {}
        for form, rank, label, _, _, _ in self._forms:
            # an escape's text starts with `%`, `\` or a high surrogate (one half of a pair joined), so a later escape
            # can take in a form's last characters only from one of those on (latest first)
            starts = tuple(p for p in range(len(form) - 1, 0, -1) if _starter(form[p]))
            for k in range(1, min(SEAM, len(form) - 1) + 1):
                self._heads.setdefault(form[:k], {}).setdefault(form[k], []).append((form, k, rank, label, starts))
                self._tails.setdefault(form[len(form) - k:], {}).setdefault(form[len(form) - k - 1], []).append(
                    (form, k, rank, label))
        # (which k to look up for an escape: those for which some form's k-th character is the escape's last (its first,
        # for a tail), so an escape that can't hold any value's edge costs one lookup)
        head_ks: dict[str, set[int]] = {}
        tail_ks: dict[str, set[int]] = {}
        for key in self._heads:
            head_ks.setdefault(key[-1], set()).add(len(key))
        for key in self._tails:
            tail_ks.setdefault(key[0], set()).add(len(key))
        self._head_ks = {c: sorted(ks) for c, ks in head_ks.items()}
        self._tail_ks = {c: sorted(ks) for c, ks in tail_ks.items()}
        # (by head key, the next characters that can start an escape: a form that goes on with one where the level
        # has another character is still found when a later escape there takes in all the rest)
        self._head_starters = {key: tuple(c for c in by_next if _starter(c)) for key, by_next in self._heads.items()}
        shown = sorted(set(labels) | ({UNCHECKED} if labels else set()), key=len, reverse=True)
        self.placeholders = re.compile("(" + "|".join(map(re.escape, shown)) + ")") if shown else None

    @property
    def redacts(self) -> bool:
        return bool(self._forms)

    def _spans(self, text: str, first: bool = False) -> list[tuple[int, int, str]]:
        """Where the values are in `text`, as (start, end, placeholder), in the text's order (with `first`, as soon as one
        is found, that one alone). Overlapping occurrences are merged into one span (the first one's placeholder), so no
        piece of either value is left out of it. Only an occurrence that lies wholly inside a placeholder is passed over,
        so a placeholder already there stays as it is; one that holds or crosses a placeholder is redacted whole (cycle
        12). Overlapping occurrences of one form are merged as they are found, and only where such a run meets a
        placeholder are the occurrences near it looked at one by one (`_kept`, cycle 21). A token still decoding at the
        work limit is a span (UNCHECKED)."""
        shown = [(m.start(), m.end()) for m in self.placeholders.finditer(text)] if self.placeholders is not None else []
        shown_starts = [a for a, _ in shown]
        found: list[tuple[int, int, int, int, str]] = []  # (start, -length, rank, end, label)
        known: list[list[tuple[int, str, bool]]] = []
        for level, chain in _levels(text):
            if level is None:  # the work limit: what still decodes was never checked, so it is taken for a value
                for a, b in _unchecked(text, chain):
                    if first:
                        return [(a, b, UNCHECKED)]
                    found.append((a, a - b, -1, b, UNCHECKED))
                continue
            for i, j, rank, label, lead, trail in self._pieces(level, chain, known, shown, shown_starts):
                a, b = _back(chain, *level.span(i, j, lead, trail), lead, trail)
                if shown and _inside(shown, shown_starts, a, b):
                    continue  # a piece of a placeholder (`test` in `<tester's password>`), not a value
                a, b = _quoted(text, a, b)
                if first:
                    return [(a, b, label)]
                found.append((a, a - b, rank, b, label))
        found.sort()
        taken: list[tuple[int, int, str]] = []
        reached = 0  # one ordered sweep with a running end: linear in the occurrences
        for start, _, _, end, label in found:
            if start >= reached:
                taken.append((start, end, label))
                reached = end
            elif end > reached:  # overlaps the span before and runs past it: that span grows to cover it
                taken[-1] = (taken[-1][0], end, taken[-1][2])
                reached = end
        return taken

    def _pieces(self, level: _Layer, chain: "list[_Layer]", known: "list[list[tuple[int, str, bool]]]",
                shown: "list[tuple[int, int]]", shown_starts: list[int]
                ) -> "Iterator[tuple[int, int, int, str, bool, bool]]":
        """What `_found` gives with overlapping occurrences merged, but a merged run that meets a placeholder already in
        the text split around the occurrences wholly inside one (`_kept`)."""
        hay = level.out
        firsts: dict[bool, list[int]] = {}

        def starts_for(lead: bool) -> "Callable[[], list[int]]":
            def get() -> list[int]:
                if lead not in firsts:
                    firsts[lead] = _firsts(level, chain, lead)
                return firsts[lead]
            return get

        for form, rank, label, lead, trail, fail in self._forms:
            for i, j in _occurrences(hay, form, fail, True):
                if not shown or j - i == len(form):  # (one occurrence alone: `_spans` checks it)
                    yield i, j, rank, label, lead, trail
                    continue

                def where(s: int, t: int, lead: bool = lead, trail: bool = trail) -> tuple[int, int]:
                    return _back(chain, *level.span(s, t, lead, trail), lead, trail)

                for x, y in _kept(hay, form, fail, i, j, where, starts_for(lead), shown, shown_starts):
                    yield x, y, rank, label, lead, trail
        yield from self._edges(level, chain, known)

    def _found(self, level: _Layer, chain: "list[_Layer]", known: "list[list[tuple[int, str, bool]]]",
               merge: bool) -> "Iterator[tuple[int, int, int, str, bool, bool]]":
        """The occurrences of the values in one level, as (start, end, rank, label, lead, trail) in its canonical text:
        each form where it is whole (with `merge`, a run of overlapping ones as one), then the occurrences at escapes
        (`_edges`)."""
        hay = level.out
        for form, rank, label, lead, trail, fail in self._forms:
            for i, j in _occurrences(hay, form, fail, merge):
                yield i, j, rank, label, lead, trail
        yield from self._edges(level, chain, known)

    def _edges(self, level: _Layer, chain: "list[_Layer]", known: "list[list[tuple[int, str, bool]]]"
               ) -> "Iterator[tuple[int, int, int, str, bool, bool]]":
        """In a decoded level, where an escape took in a value's first characters with characters of the text before it
        (and maybe a later escape its last ones with the text after it), or its last characters alone, as `_found`
        gives them. Such an occurrence takes the whole escape: a couple of characters too many rather than one of the
        value too few. A best effort (the threat model's boundary fusion): no encoder writes an escape cut short next
        to a value."""
        hay = level.out
        if not chain:
            return
        slots, n, end, size = level.slots(), len(level.source), 2 * len(hay), len(hay)
        made = _made(chain, known)
        at_slot: dict[int, set[str]] | None = None  # each slot's escapes as the set of their beginnings
        seen: set[tuple[int, str]] = set()
        # an escape takes in at most its own length less one of a value's characters: the longest escape in this level
        # bounds the tail lengths tried below (SEAM is the bound over every escape there is)
        seam = min(SEAM, max((len(raw) for _, raw, _ in made), default=1) - 1)
        head_ks, tail_ks = self._head_ks, self._tail_ks
        for x, raw, gone in made:
            hks, tks = head_ks.get(raw[-1]), tail_ks.get(raw[0])
            if hks is None and tks is None:
                continue
            # (gone: what the escape made was dropped, a direction mark: it has no place, own 0)
            place = (slots[x] & ~1 if x < n else end) if gone else slots[x]
            if not place & 1:  # (what it made is dropped here: many escapes can share that place; one that made a
                # character the level keeps is alone at its own)
                if (place, raw) in seen:
                    continue  # the same escape at the same place finds the same occurrences (dropped marks share one)
                seen.add((place, raw))
            i, own = divmod(place, 2)
            at = i + own  # where the rest of a value goes on after an escape that took in its first characters
            here = hay[at] if at < size else ""
            for k in hks or ():
                if k > len(raw):
                    break
                key = raw[-k:]
                by_next = self._heads.get(key)  # the forms whose first k characters the escape ends with
                if by_next is None:
                    continue
                # a form goes on with the level's character here, or with one a later escape here starts with (that
                # escape then takes in all the rest)
                for c in (here, *(c for c in self._head_starters[key] if c != here)):
                    for form, _, rank, label, starts in by_next.get(c, ()):
                        rest = len(form) - k
                        if c == here and hay.startswith(form[k:], at):
                            yield i, at + rest, rank, label, not own, False
                        # its last characters taken into a later escape too (Codex, PR #10 cycle 15: `%2` + `a%62c%2` +
                        # `F`): the form goes on in the level up to p, and an escape there starts with form[p:]. That
                        # is at most `seam` characters, and starts with what an escape can start with
                        earliest = len(form) - min(seam, rest)
                        for p in starts:
                            if p < earliest:
                                break
                            if not hay.startswith(form[k:p], at):
                                continue
                            if at_slot is None:
                                # each slot's escapes as the set of their beginnings (up to `seam` characters): many
                                # escapes can share a slot (dropped direction marks all land on one), and a lookup must
                                # not scan them (Codex, cycle 19)
                                at_slot = {}
                                for y, other, dropped in made:
                                    begins = at_slot.setdefault(
                                        (slots[y] & ~1 if y < n else end) if dropped else slots[y], set())
                                    begins.update(other[:m] for m in range(1, min(seam, len(other)) + 1))
                            q, tail = at + p - k, form[p:]
                            for own2 in (1, 0):
                                if q + own2 > i and tail in at_slot.get(2 * q + own2, ()):
                                    yield i, q + own2, rank, label, not own, not own2
            if i == 0 or tks is None:
                continue
            before = hay[i - 1]  # the form's character just before what an escape that took in its last ones holds
            for k in tks:
                if k > len(raw):
                    break
                by_before = self._tails.get(raw[:k])  # the forms whose last k characters the escape starts with
                for form, _, rank, label in by_before.get(before, ()) if by_before is not None else ():
                    rest = len(form) - k
                    if i >= rest and hay.startswith(form[:rest], i - rest):
                        yield (i - rest, i + 1, rank, label, False, False) if own else \
                            (i - rest, i, rank, label, False, True)

    def scrub(self, text: str) -> str:
        """Idempotent: a placeholder already there is left alone, so text redacted where it came in can be redacted again
        where it goes out (an account called `tester` with the password `test` doesn't become `<<tester's ...`)."""
        if not text or not self._forms:
            return text
        out, at = [], 0
        for start, end, label in self._spans(text):
            out += [text[at:start], label]
            at = end
        return "".join(out) + text[at:] if out else text

    def holds(self, text: str) -> bool:
        if not text or not self._forms:
            return False
        if self.placeholders is not None and self.placeholders.search(text):
            return True
        return bool(self._spans(text, first=True))


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
    if ".git" in Path(path).resolve().parts:  # a repo's own folder: no .gitignore covers it, and git won't say (Tapline's rule)
        return f"accounts file is inside a git repo's .git folder at {folder}; move it."
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
    # (Codex, PR #10 cycle 20: the matcher's cost grows with the values; the phone's passcode alone isn't an account)
    count = sum(1 for name, entry in raw.items()
                if name != PHONE or (isinstance(entry, dict) and ("username" in entry or "password" in entry)))
    if count > MAX_ACCOUNTS:
        return {}, None, (f"it holds {count} accounts; a file holds at most {MAX_ACCOUNTS} (split them into several "
                          "files, one per run)")
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
            if key in entry and len(entry[key]) > MAX_VALUE:  # (Codex, PR #10 cycle 15: the matcher's cost is bounded by it)
                return {}, None, f"{name}'s {key} is longer than {MAX_VALUE} characters; a test account's value is at most {MAX_VALUE}"
            if key in entry and unsettled(entry[key]):
                return {}, None, (f"{name}'s {key} is still escaped after {MAX_PASSES} rounds of decoding; write it decoded, "
                                  "as it is typed")
            if key in entry and len(entry[key]) < MIN_SCRUBBED:  # too short to be scrubbed out of text, so not accepted
                return {}, None, f"{name}'s {key} must be at least {MIN_SCRUBBED} characters: a shorter one can't be kept out of answers and maps"
            if key in entry and weak(entry[key]):  # (Codex, PR #10 cycle 14: `"   "` or `%41%42` is as weak as `ab`)
                return {}, None, (f"{name}'s {key} must have at least {MIN_SCRUBBED} characters once decoded, without "
                                  "direction marks, backslashes or whitespace at its ends: a weaker one can't be kept out "
                                  "of answers and maps")
        apps = entry.get("apps")
        if apps is not None and (not isinstance(apps, list) or not all(isinstance(a, str) and a for a in apps)):
            return {}, None, f"{name}'s apps must be a list of bundle ids"
        if name == PHONE:
            passcode = entry.get("passcode")
        found[name] = Account(name, entry.get("username"), entry.get("password"), tuple(apps) if apps is not None else None)
    return found, passcode, None


def _value_problem(name: str, key: str, value: object, plain: bool) -> str | None:
    """Why a vault value is refused at load (the 1.6.1 limits; a plain field may be short), naming the entry and
    field, never the value."""
    if not isinstance(value, str) or not value:
        return f"{name}'s {key} must be a non-empty text"
    if len(value) > MAX_VALUE:
        return f"{name}'s {key} is longer than {MAX_VALUE} characters; a vault value is at most {MAX_VALUE}"
    if unsettled(value):
        return f"{name}'s {key} is still escaped after {MAX_PASSES} rounds of decoding; write it decoded, as it is typed"
    if not plain and (len(value) < MIN_SCRUBBED or weak(value)):
        return (f"{name}'s {key} must have at least {MIN_SCRUBBED} characters once decoded, without direction marks, "
                "backslashes or whitespace at its ends: a weaker one can't be kept out of answers and maps")
    return None


def _read_otp(raw: object) -> tuple[dict[str, OtpSetting], str | None]:
    """Tapwright's `otp` setting (§24.3): `{"<app id>": {"from": ["<bundle id>", ...], "match": "<text>"}}`. Off by
    default: an app not named has codes off. A setting that isn't this shape turns codes off for every app, and says so."""
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return {}, 'its "otp" setting isn\'t an object; one-time codes are off'
    found: dict[str, OtpSetting] = {}
    for app, setting in raw.items():
        sources = setting.get("from") if isinstance(setting, dict) else None
        match = setting.get("match") if isinstance(setting, dict) else None
        if not app or not isinstance(sources, list) or not sources or \
                not all(isinstance(s, str) and s.strip() for s in sources) or not isinstance(match, str) or not match.strip():
            return {}, (f'its "otp" setting for {app} needs "from" (a list of bundle ids) and "match" (a text); one-time '
                        "codes are off")
        pattern, compiled = setting.get("pattern"), None
        if pattern is not None:
            try:
                if not isinstance(pattern, str) or not pattern or len(pattern) > OTP_PATTERN_MAX:
                    raise re.error("not a pattern")
                compiled = re.compile(pattern)
            except (re.error, RecursionError, OverflowError):
                return {}, (f'its "otp" setting for {app} has a "pattern" that isn\'t a regular expression of 1 to '
                            f"{OTP_PATTERN_MAX} characters; one-time codes are off")
        found[app] = OtpSetting(tuple(s.strip() for s in sources), match.strip(), compiled)
    return found, None


OTP_PATTERN_MAX = 200  # characters of an `otp` setting's own code pattern


def _read_v2(data: dict) -> "tuple[dict[str, Entry], str | None]":
    """(entries, problem) of a parsed version 2 file (§20.1). An entry of a kind this tool doesn't know is unusable, and
    its text values are still kept out of text."""
    raw = data.get("vault")
    if not isinstance(raw, dict):
        return {}, 'it has no "vault" object'
    found: dict[str, Entry] = {}
    count = 0
    for name, entry in raw.items():
        if not isinstance(name, str) or not ENTRY_NAME.match(name):
            return {}, "an entry's name is ASCII letters, digits and _ (one of them isn't)"
        if not isinstance(entry, dict):
            return {}, f"{name} isn't an object"
        kind = entry.get("kind")
        if not isinstance(kind, str) or not kind:
            return {}, f"{name} has no kind; the kinds are {', '.join([*KIND_FIELDS, FIXTURE])}"
        apps = entry.get("apps")
        if apps is not None and (not isinstance(apps, list) or not all(isinstance(a, str) and a for a in apps)):
            return {}, f"{name}'s apps must be a list of bundle ids"
        known = kind in KIND_FIELDS or kind == FIXTURE
        values: dict[str, str] = {}
        for key, value in entry.items():
            if key in ENTRY_KEYS:
                continue
            if kind in KIND_FIELDS and key not in KIND_FIELDS[kind]:
                return {}, f"{name} has {key}; {'an' if kind[0] in 'aeiou' else 'a'} {kind} has {', '.join(KIND_FIELDS[kind])}"
            if not ENTRY_NAME.match(key):
                return {}, f"{name} has a field whose name isn't ASCII letters, digits and _"
            if not known and not isinstance(value, str):
                continue  # (an unknown kind's other values: nothing to type or keep out)
            plain = (kind, key) in PLAIN_FIELDS or kind == FIXTURE or not known
            if (why := _value_problem(name, key, value, plain)) is not None:
                return {}, why
            values[key] = value
            if kind != "phone" and not weak(value):
                count += 1
        if count > MAX_VALUES:
            return {}, f"it holds more than {MAX_VALUES} values; a vault file holds at most {MAX_VALUES} (split it)"
        found[name] = Entry(name, kind, values, tuple(apps) if apps is not None else None, known)
    return found, None


def plain_note(path: str) -> str:
    """What every start says of a plain vault file (§20.2)."""
    return f"Note: the vault file {path} is not encrypted; keep test data in it only."


def load(path: str, git: GitRunner = _git, protection: "vault.Deps | None" = None) -> Loaded:
    """The accounts file (the vault) at `path`. It never raises: a file that can't be used is a `problem`, said at the
    first use. A protected file is opened with the system's protection (vault.py) and the git rule doesn't apply to it
    (it holds no value); a plain one inside a git work tree that doesn't ignore it loads nothing, and is never parsed
    past what tells it from a protected one."""
    from . import vault
    try:
        text = Path(vault.real_path(path)).read_text("utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        return Loaded(path, problem=f"the accounts file {path} can't be read ({getattr(e, 'strerror', None) or e})")
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        data = None
    how = vault.protection(data)
    if how is not None:
        try:
            text = vault.open_protected(path, data, protection)
        except vault.VaultOpenError as e:
            return Loaded(path, problem=f"the vault file can't be opened: {str(e).rstrip('. ')}.", protection=how)
        except Exception as e:  # (a stalled or broken protection call: nothing loads, and it says why in words)
            return Loaded(path, problem=f"the vault file can't be opened: {type(e).__name__}.", protection=how)
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            return Loaded(path, problem=f"the vault file {path} was opened, but what it holds isn't valid JSON",
                          protection=how)
    else:
        if (why := git_problem(path, git)) is not None:
            return Loaded(path, problem=why)  # nothing of it is loaded
        if data is None:
            return Loaded(path, problem=f"the accounts file {path} isn't JSON")  # (json's message would quote the text)
    return _loaded(path, data, how)


def _loaded(path: str, data: object, how: str | None) -> Loaded:
    """A parsed file (version 1 or 2) as what it holds."""
    note = None if how is not None else plain_note(path)
    if isinstance(data, dict) and data.get("version") == VERSION2:
        entries, why = _read_v2(data)
        if why is not None:
            return Loaded(path, problem=f"the vault file {path} can't be used: {why}", protection=how, note=note,
                          version=VERSION2)
        otp, otp_why = _read_otp(data.get("otp"))
        accounts = {n: Account(n, e.values.get("username"), e.values.get("password"), e.apps)
                    for n, e in entries.items() if e.kind == "account"}
        # only the entry named `phone` is the phone's passcode (review 1.7, L3): another entry of kind phone (an
        # account's app PIN that an older `vault protect` moved into `<name>_phone`) is never typed on the lock screen
        phone = entries.get(PHONE) if PHONE in entries and entries[PHONE].kind == "phone" else None
        notes = " ".join(n for n in (note, f"Note: the vault file {path}: {otp_why}." if otp_why else None) if n)
        return Loaded(path, accounts, phone.values.get("passcode") if phone else None, None, entries, otp, how,
                      notes or None, VERSION2)
    found, passcode, why = _read(data)
    if why is not None:
        if isinstance(data, dict) and data.get("version") not in (VERSION, VERSION2):
            why = f"its version is {data.get('version')!r}; this tool reads version {VERSION} or {VERSION2}"
        return Loaded(path, problem=f"the accounts file {path} can't be used: {why}", protection=how, note=note)
    vault_entries = {n: Entry(n, "phone" if n == PHONE and not (a.username or a.password) else "account",
                              {k: v for k, v in (("username", a.username), ("password", a.password)) if v}, a.apps)
                     for n, a in found.items()}
    return Loaded(path, found, passcode, None, vault_entries, {}, how, note)


def check_text(text: str) -> str | None:
    """Why a vault JSON wouldn't load (for `vault protect`: never protect what wouldn't open), or None."""
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return "it isn't JSON"
    loaded = _loaded("", data, "check")
    if loaded.problem is None:
        return None
    return loaded.problem.split(" can't be used: ", 1)[-1]


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
        self._keep(loaded.entries())

    def remember_code(self, code: str) -> None:
        """A one-time code handed over or typed (§20.4): kept out of text from now on, as `<one-time code>`, in the same
        single pass as every vault value."""
        if code and not weak(code):
            # every form a notification writes it in: `123 456` and `123-456` as well as `123456` (Codex, PR #12 c1, H4)
            from .notifications import code_forms
            self._keep(tuple(("", CODE_LABEL, form) for form in code_forms(code)))

    def _keep(self, entries: "tuple[tuple[str, str, str], ...]") -> None:
        fresh = [e for e in entries if e not in self._ever]
        if fresh:
            self._ever.update(dict.fromkeys(fresh))
            self._redaction = Redaction(tuple(self._ever))

    def holds(self, text: str) -> bool:
        """Does the text hold any value ever loaded, or a placeholder?"""
        return self._redaction.holds(text)

    def scrub(self, text: str) -> str:
        """The text without any value ever loaded (a log line has no call whose accounts to use)."""
        return self._redaction.scrub(text)

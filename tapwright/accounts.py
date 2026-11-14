"""Test accounts (AGENT-TOOLS §18.6): named accounts kept in a file on the user's computer (`--accounts FILE`). An agent
says `account: "tester2"`; the tool types the values. They are never in an answer, the action log, the map, a script, a
report or a caption: every answer is scrubbed of them, a name that holds one is never saved, and a step carries only the
placeholder `${account.username}` or `${account.password}`, filled in at the moment of typing.

  - A value is the whole text of a step, never part of one (joining text would get around the secure-field rule).
  - A password goes only into a secure field, a username only into a text field that isn't secure.
  - `apps` limits where an account may be used.
  - A file inside a git work tree that doesn't ignore it loads no accounts: any use says so.
  - `phone.passcode` is stored and exposed nowhere. (Typing it on the lock screen is a later task.)

What the redaction guarantees (the threat model; Codex, PR #10 cycles 14 and 15):
  - Values: a username or password is 3 to 256 characters: at most 256 as written in the file, at least 3 once decoded,
    without direction marks and trimmed. A value still escaped after the work limit's passes is refused too. A file
    that breaks a limit loads no accounts, and says which account and field, never the value.
  - Encodings: a value is found as it is; with whitespace normalized (any run of whitespace or `+` is one space, the
    ends may be trimmed) and direction marks anywhere; percent-encoded (%HH, either case, UTF-8); with JSON or Python
    string escapes (\\uXXXX and surrogate pairs, \\UXXXXXXXX, \\xHH, \\\\, \\", \\', \\/, \\b, \\f, \\n, \\t, \\r);
    and any composition of those, to any depth within the work limit: each layer is a whole-text encoder (quote,
    quote_plus, json.dumps, repr, ascii, ...) or a different escape for each character. Only a recognized escape
    is decoded: any other backslash is a literal character on both sides, so `pass\\word` is not `password`. An escape
    that takes in a value's first or last characters, or both, with the text around it is redacted with the value.
  - Work limit: a text is decoded at most 16 times (MAX_PASSES). If a 17th pass would still change it, every
    whitespace-separated token that holds what it would change is redacted whole (as UNCHECKED) and `holds` is true:
    nothing is released that wasn't checked to the end. Each level is searched in time linear in its length and the
    values' (Knuth-Morris-Pratt for overlapping occurrences), so a text costs at most 17 such searches.
  - Not covered: other encodings (base64, HTML entities, hashing, case changes, a value split by other text), and a
    value's backslash left bare, or percent-encoded under a JSON or Python layer, right before an escaped character
    (`\\` + `\\x73`): JSON and Python read `\\\\x73` as an escaped backslash and `x73`, and so does the matcher. No
    encoder writes that: one that escapes a character escapes the backslash too.

Pure but for reading the file and asking git: no phone.
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
MENTION = re.compile(r"\$\{account\.")
MIN_SCRUBBED = 3  # a value shorter than this isn't scrubbed out of text (it would eat ordinary words)
MAX_VALUE = 256  # a username or password longer than this is refused when the file loads (no real credential is longer)
BIDI_MARKS = dict.fromkeys(map(ord, "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"))  # the invisible direction marks ui._text drops from screen text
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
# and both at once: `%2` + `a%62c%2` + `F` (the value `abc%2`) reads `*bc/`. So in a decoded level each character an
# escape made is checked too: when the escape's text ends with a value's first characters and the level goes on with
# the rest, or with the rest but its last characters, which a later escape's text starts with (or the mirror, at the
# value's end alone), the value is there, and the escapes are redacted with it. Each such check is a dictionary lookup by
# the escape's last or first character, then exact comparisons: linear in the escapes, never a search. Matching is
# case-sensitive but for the hex of an escape.

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


class Redaction:
    """A set of account values, as (name, kind, value), and what redacts them: each value, in any of its shapes, becomes
    `<name's username>` / `<name's password>`. Overlapping occurrences are merged into one span, the earliest and then the
    longest first (a value that holds another is replaced whole). A token still decoding at the work limit becomes
    UNCHECKED."""

    def __init__(self, entries: "tuple[tuple[str, str, str], ...]" = ()) -> None:
        forms: dict[str, tuple[int, str, bool, bool]] = {}
        labels: list[str] = []
        for name, kind, value in sorted(entries, key=lambda e: -len(e[2])):
            label = f"<{name}'s {kind}>"
            for form, lead, trail in _forms(value):
                if form not in forms:
                    forms[form] = (len(forms), label, lead, trail)
                    labels.append(label)
        self._forms = [(form, rank, label, lead, trail, _failure(form))
                       for form, (rank, label, lead, trail) in forms.items()]
        # (a value's first characters taken into an escape of the text before it, `%2` + `abc` read as `%2a` + `bc`, or
        # its last ones into an escape with the text after it, `ab%2` + `F`: by the character the escape ends or starts
        # with, each (form, how many characters of it the escape holds, rank, label))
        self._heads: dict[str, list[tuple[str, int, int, str]]] = {}
        self._tails: dict[str, list[tuple[str, int, int, str]]] = {}
        for form, rank, label, _, _, _ in self._forms:
            for k in range(1, min(SEAM, len(form) - 1) + 1):
                self._heads.setdefault(form[k - 1], []).append((form, k, rank, label))
                self._tails.setdefault(form[len(form) - k], []).append((form, k, rank, label))
        shown = sorted(set(labels) | ({UNCHECKED} if labels else set()), key=len, reverse=True)
        self.placeholders = re.compile("(" + "|".join(map(re.escape, shown)) + ")") if shown else None

    @property
    def redacts(self) -> bool:
        return bool(self._forms)

    def _spans(self, text: str, first: bool = False) -> list[tuple[int, int, str]]:
        """Where the values are in `text`, as (start, end, placeholder), in the text's order (with `first`, as soon as one
        is found, that one alone). Overlapping occurrences are merged into one span (the first one's placeholder), so no
        piece of either value is left out of it. Only an occurrence that lies wholly inside a placeholder is passed over,
        so a placeholder already there stays as it is. A token still decoding at the work limit is a span (UNCHECKED)."""
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
            for i, j, rank, label, lead, trail in self._found(level, chain, known, merge=not shown):
                a, b = _back(chain, *level.span(i, j, lead, trail), lead, trail)
                if shown:
                    k = bisect.bisect_right(shown_starts, a) - 1
                    if k >= 0 and b <= shown[k][1]:
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

    def _found(self, level: _Layer, chain: "list[_Layer]", known: "list[list[tuple[int, str, bool]]]",
               merge: bool) -> "Iterator[tuple[int, int, int, str, bool, bool]]":
        """The occurrences of the values in one level, as (start, end, rank, label, lead, trail) in its canonical text:
        each form where it is whole, then (in a decoded level) where an escape took in its first characters with
        characters of the text before it (and maybe a later escape its last ones with the text after it), or its last
        characters alone. Such an occurrence takes the whole escape: a couple of characters too many rather than one of
        the value too few."""
        hay = level.out
        for form, rank, label, lead, trail, fail in self._forms:
            for i, j in _occurrences(hay, form, fail, merge):
                yield i, j, rank, label, lead, trail
        if not chain:
            return
        slots, n, end = level.slots(), len(level.source), 2 * len(hay)
        made = _made(chain, known)
        at_slot: dict[int, list[str]] | None = None  # the escapes' texts by where their characters are in this level
        # an escape takes in at most its own length less one of a value's characters: the longest escape in this level
        # bounds the tail lengths tried below (SEAM is the bound over every escape there is)
        seam = min(SEAM, max((len(raw) for _, raw, _ in made), default=1) - 1)
        for x, raw, gone in made:
            heads, tails = self._heads.get(raw[-1]), self._tails.get(raw[0])
            if heads is None and tails is None:
                continue
            # (gone: what the escape made was dropped, a direction mark: it has no place, own 0)
            i, own = divmod((slots[x] & ~1 if x < n else end) if gone else slots[x], 2)
            for form, k, rank, label in heads or ():
                if not raw.endswith(form[:k]):
                    continue
                at, rest = i + own, len(form) - k  # where the rest of the form goes on, and how long it is
                if hay.startswith(form[k:], at):
                    yield i, at + rest, rank, label, not own, False
                # its last characters taken into a later escape too (Codex, PR #10 cycle 15: `%2` + `a%62c%2` + `F`).
                # Every middle the loop below tries starts with the shortest one: when that isn't there, none is
                shortest = rest - min(seam, rest)
                if not hay.startswith(form[k:k + shortest], at):
                    continue
                if at_slot is None:
                    at_slot = {}
                    for y, other, dropped in made:
                        at_slot.setdefault((slots[y] & ~1 if y < n else end) if dropped else slots[y], []).append(other)
                for k2 in range(1, min(seam, rest) + 1):
                    q, tail = at + rest - k2, form[len(form) - k2:]
                    for own2 in (1, 0):
                        if q + own2 > i and any(r.startswith(tail) for r in at_slot.get(2 * q + own2, ())) and \
                                hay.startswith(form[k:len(form) - k2], at):
                            yield i, q + own2, rank, label, not own, not own2
            for form, k, rank, label in tails or ():
                rest = len(form) - k
                if i >= rest and raw.startswith(form[rest:]) and hay.startswith(form[:rest], i - rest):
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

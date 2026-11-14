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


# ---- the matcher (Codex, PR #10 cycle 14) ----
# A value is found by decoding, not by a regular expression of its shapes. A text is read at several levels: as it is
# (level 0), then decoded once, twice, ... until a pass changes nothing (at most MAX_DECODES passes). A pass decodes
# percent escapes (%HH in either case, consecutive bytes read as UTF-8 where they are valid, else kept as they are) and
# JSON or Python escapes (\uXXXX, a surrogate pair, \UXXXXXXXX, \xHH, \" \' \/ \n \t \r) behind a run of backslashes of
# any length, so an escape escaped again (`\\\\u00e9`) is undone in one pass and `%2525...` over passes. Each level is
# then normalized for matching: direction marks and backslashes dropped (a backslash that escapes nothing is noise
# whichever way a text was escaped), and every run of whitespace or `+` made one space (`+` is a space on both sides,
# so a form-encoded value is found and a value with a `+` matches its spaces). A value's forms are its own levels,
# normalized the same way. Every form is looked for in every level with `str.find`, overlapping occurrences included,
# and each occurrence maps back to the original text through each level's position map: canonical character i came from
# text[starts[i]:ends[i]]. So the value as it is, decoded or encoded any number of times, by any composition of those
# encoders, is found, and the redaction covers every character of its shape in the original text.
# Decoding reads a text without knowing where a value starts, so an escape can take in characters of the value with
# characters of the text around it: `%2` + `abc` reads `%2a` + `bc`, `\` + `tab` a tab + `ab`, `ab%2` + `F` reads `ab/`.
# So in a decoded level each character an escape made is checked too: when the escape's text ends with a value's first
# characters and the level goes on with the rest (or the mirror, at the value's end), the value is there, and the
# escape is redacted with it. Each such check is a dictionary lookup by the escape's last or first character, then
# exact comparisons: linear in the escapes, never a search. Matching is case-sensitive but for the hex of an escape.

MAX_DECODES = 6
_MARK_CLASS = "".join(map(chr, BIDI_MARKS))  # (the direction marks, as ui._text drops them)
_ESCAPE = re.compile(
    r"(?:%[0-9A-Fa-f]{2})+"
    r"|(?<!\\)\\+(?:u([dD][89abAB][0-9A-Fa-f]{2})\\+u([dD][c-fC-F][0-9A-Fa-f]{2})"  # (a run of backslashes is tried
    r"|u([0-9A-Fa-f]{4})|U([0-9A-Fa-f]{8})|x([0-9A-Fa-f]{2})|([\"'/ntr]))")         # from its start only: linear)
_SIMPLE = {'"': '"', "'": "'", "/": "/", "n": "\n", "t": "\t", "r": "\r"}
_LOOSE = re.compile(r"[\s+\\" + _MARK_CLASS + "]+")  # what normalizing changes: whitespace, `+`, backslashes, marks
_SPACE = re.compile(r"[\s+]")


class _Layer:
    """One step from a text `source` to `out`: pieces of (source start, source end, kind), kind 0 for a run copied as it
    is, 1 for one character made from the whole piece, 2 for a piece dropped. The position map is built only when an
    occurrence has to be mapped."""

    __slots__ = ("source", "out", "_pieces", "_maps", "_kept", "_slots")

    def __init__(self, source: str, out: str, pieces: list[tuple[int, int, int]]) -> None:
        self.source = source
        self.out = out
        self._pieces = pieces
        self._maps: tuple[list[int], list[int], list[int], list[int]] | None = None
        self._kept: list[int] | None = None
        self._slots: list[int] | None = None

    def slots(self) -> list[int]:
        """For each source character, where its piece is in the output: 2 * position, plus 1 when the piece is an output
        character (copied, or a whitespace run made one space), 0 when it is dropped (a mark, a backslash)."""
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

    def carried(self, made: "list[tuple[int, str]]") -> "list[tuple[int, str]]":
        """The characters an escape made, as (output index, the escape's text without backslashes): those of the source
        (`made`, in order) this layer copies as they are, then the ones it makes itself, in order of output."""
        out: list[tuple[int, str]] = []
        where = self.kept()
        for x, raw in made:
            if where[x] >= 0:
                out.append((where[x], raw))
        at = 0
        for a, b, kind in self._pieces:
            if kind == 1:
                out.append((at, self.source[a:b].replace("\\", "")))
                at += 1
            elif kind == 0:
                at += b - a
        out.sort()
        return out

    def maps(self) -> tuple[list[int], list[int], list[int], list[int]]:
        """(starts, ends, wide starts, wide ends): output character i came from source[starts[i]:ends[i]]; the wide ones
        reach over a dropped piece just before or after it (a value that starts or ends with a backslash)."""
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
    return _build(text, changes) if changes else None


def _normalized(text: str) -> _Layer:
    """Direction marks and backslashes dropped, each run of whitespace or `+` one space."""
    changes = []
    for m in _LOOSE.finditer(text):
        run = m.group(0)
        if run != " ":
            changes.append((m.start(), m.end(), " " if _SPACE.search(run) else ""))
    return _build(text, changes)


def _levels(text: str) -> "Iterator[tuple[_Layer, list[_Layer]]]":
    """Each level of `text` as (its normalized layer, the decoding layers that led to it, outermost last), level 0 first."""
    chain: list[_Layer] = []
    current = text
    for _ in range(MAX_DECODES + 1):
        yield _normalized(current), chain
        step = _decoded(current)
        if step is None:
            return
        chain = [*chain, step]
        current = step.out


def _made(chain: "list[_Layer]", known: "list[list[tuple[int, str]]]") -> "list[tuple[int, str]]":
    """The characters escapes made in the last text of `chain` (`known` caches each level's)."""
    while len(known) <= len(chain):
        n = len(known)
        known.append(chain[n - 1].carried(known[n - 1]) if n else [])
    return known[len(chain)]


def canon(text: str) -> tuple[str, list[int], list[int]]:
    """The text fully decoded and normalized, and where each of its characters came from: canonical character i is
    text[starts[i]:ends[i]]."""
    *_, (last, chain) = _levels(text)
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
        form = layer.out
        if not form:
            continue
        starts, ends, wide_starts, wide_ends = layer.maps()
        lead, trail = wide_starts[0] != starts[0], wide_ends[-1] != ends[-1]
        stripped = form.strip()
        if len(stripped) >= MIN_SCRUBBED:
            found.append((form, lead, trail))
            if stripped != form:
                found.append((stripped, lead and form[0] != " ", trail and form[-1] != " "))
    return found


SEAM = 8  # how many of a value's characters an escape around it can take in (\\U and 8 hex digits)


def _occurrences(hay: str, needle: str, merge: bool) -> "Iterator[tuple[int, int]]":
    """Every occurrence of `needle` in `hay`, overlapping ones too, as (start, end); with `merge` a chain of overlapping
    ones is one (linear in the occurrences: `aaaa` in 20,000 `a`s is one span, found in one pass)."""
    size = len(needle)
    at = hay.find(needle)
    while at != -1:
        end = at + size
        after = hay.find(needle, at + 1)
        while merge and after != -1 and after < end:
            end = after + size
            after = hay.find(needle, after + 1)
        yield at, end
        at = after


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
    longest first (a value that holds another is replaced whole)."""

    def __init__(self, entries: "tuple[tuple[str, str, str], ...]" = ()) -> None:
        forms: dict[str, tuple[int, str, bool, bool]] = {}
        labels: list[str] = []
        for name, kind, value in sorted(entries, key=lambda e: -len(e[2])):
            label = f"<{name}'s {kind}>"
            for form, lead, trail in _forms(value):
                if form not in forms:
                    forms[form] = (len(forms), label, lead, trail)
                    labels.append(label)
        self._forms = [(form, rank, label, lead, trail) for form, (rank, label, lead, trail) in forms.items()]
        # (a value's first characters taken into an escape of the text before it, `%2` + `abc` read as `%2a` + `bc`, or
        # its last ones into an escape with the text after it, `ab%2` + `F`: by the character the escape ends or starts
        # with, each (form, how many characters of it the escape holds, rank, label))
        self._heads: dict[str, list[tuple[str, int, int, str]]] = {}
        self._tails: dict[str, list[tuple[str, int, int, str]]] = {}
        for form, rank, label, _, _ in self._forms:
            for k in range(1, min(SEAM, len(form) - 1) + 1):
                self._heads.setdefault(form[k - 1], []).append((form, k, rank, label))
                self._tails.setdefault(form[len(form) - k], []).append((form, k, rank, label))
        shown = sorted(set(labels), key=len, reverse=True)
        self.placeholders = re.compile("(" + "|".join(map(re.escape, shown)) + ")") if shown else None

    @property
    def redacts(self) -> bool:
        return bool(self._forms)

    def _spans(self, text: str, first: bool = False) -> list[tuple[int, int, str]]:
        """Where the values are in `text`, as (start, end, placeholder), in the text's order (with `first`, as soon as one
        is found, that one alone). Overlapping occurrences are merged into one span (the first one's placeholder), so no
        piece of either value is left out of it. Only an occurrence that lies wholly inside a placeholder is passed over,
        so a placeholder already there stays as it is."""
        shown = [(m.start(), m.end()) for m in self.placeholders.finditer(text)] if self.placeholders is not None else []
        shown_starts = [a for a, _ in shown]
        found: list[tuple[int, int, int, int, str]] = []  # (start, -length, rank, end, label)
        known: list[list[tuple[int, str]]] = []
        for level, chain in _levels(text):
            for i, j, rank, label, lead, trail in self._found(level, chain, known, merge=not shown):
                starts, ends, wide_starts, wide_ends = level.maps()
                a, b = (wide_starts if lead else starts)[i], (wide_ends if trail else ends)[j - 1]
                for step in reversed(chain):
                    s, e, _, _ = step.maps()
                    a, b = s[a], e[b - 1]
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

    def _found(self, level: _Layer, chain: "list[_Layer]", known: "list[list[tuple[int, str]]]",
               merge: bool) -> "Iterator[tuple[int, int, int, str, bool, bool]]":
        """The occurrences of the values in one level, as (start, end, rank, label, lead, trail) in its canonical text:
        each form where it is whole, then (in a decoded level) where an escape took in its first or last characters
        with characters of the text around it. Such an occurrence takes the whole escape: a couple of characters too
        many rather than one of the value too few."""
        hay = level.out
        for form, rank, label, lead, trail in self._forms:
            for i, j in _occurrences(hay, form, merge):
                yield i, j, rank, label, lead, trail
        if not chain:
            return
        slots = level.slots()
        for x, raw in _made(chain, known):
            if not raw:
                continue
            i, own = divmod(slots[x], 2)  # (own 0: what the escape made was dropped, a direction mark: it has no place)
            for form, k, rank, label in self._heads.get(raw[-1], ()):
                if raw.endswith(form[:k]) and hay.startswith(form[k:], i + own):
                    yield (i, i + own + len(form) - k, rank, label, False, False) if own else \
                        (i, i + len(form) - k, rank, label, True, False)
            for form, k, rank, label in self._tails.get(raw[0], ()):
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

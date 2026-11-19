"""The closest matches for a `not_found` (docs/AGENT-TOOLS.md §16.2): up to 3 elements, on screen or off, whose id or
text is close to what the agent asked for, so a retry names the right one. Close means: the same words in another
case or order; one contained in the other; or a small edit distance (at most 2 for up to 10 characters, 3 above). A
match on only a common word ("the", "a", "of", "to", "and", "in") doesn't count. Pure: no phone."""
from __future__ import annotations

import re

from . import ui

MAX_CLOSEST = 3
COMMON = frozenset({"the", "a", "of", "to", "and", "in"})
MIN_FUZZY = 3  # characters: shorter words are not compared by edit distance ("ok" is not near "go")


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", text.casefold())


def _distance(a: str, b: str, limit: int) -> int:
    """Edit distance of two strings, or limit + 1 when it is more than `limit`."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        row = [i]
        for j, cb in enumerate(b, 1):
            row.append(min(prev[j] + 1, row[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(row) > limit:
            return limit + 1
        prev = row
    return prev[-1]


def rank(query: str, candidate: str) -> int | None:
    """How close a candidate's text is to the query (lower is closer), or None when it isn't close."""
    qw, cw = _words(query), _words(candidate)
    if not qw or not cw or not [w for w in qw if w not in COMMON] or not [w for w in cw if w not in COMMON]:
        return None  # nothing but common words on a side: no match of substance
    q, c = " ".join(qw), " ".join(cw)
    if sorted(qw) == sorted(cw):
        return 0
    for inner, outer, words in ((q, c, qw), (c, q, cw)):
        if inner in outer and [w for w in words if w not in COMMON]:
            return 1
    if min(len(q), len(c)) >= MIN_FUZZY:
        limit = 2 if len(q) <= 10 else 3
        d = _distance(q, c, limit)
        if d <= limit:
            return 1 + d
    return None


def wanted(target: ui.Target) -> tuple[str, bool] | None:
    """What a target asks for: (the text, whether it is an id), or None for a ref or a point."""
    for text, is_id in ((target.id, True), (target.label, False), (target.text, False)):
        if text is not None:
            return text, is_id
    return None


def where(e: ui.Element, screen: tuple[float, float]) -> str:
    """` (off screen, below)` for an element off screen, by the side its centre lies on."""
    cx, cy = e.center
    if cy >= screen[1]:
        side = "below"
    elif cy < 0:
        side = "above"
    elif cx >= screen[0]:
        side = "right"
    elif cx < 0:
        side = "left"
    else:  # past its scroll view's edge, or under a bar, with its centre on the screen
        side = "below" if cy >= screen[1] / 2 else "above"
    return f" (off screen, {side})"


def _nearest(target: ui.Target, items: list[ui.Element], off: list[ui.Element]) -> list[tuple[int, int, float, float, ui.Element]]:
    """The close elements, closest first: (rank, off screen, y, x, element), at most MAX_CLOSEST."""
    ask = wanted(target)
    if ask is None:
        return []
    query, is_id = ask
    found: list[tuple[int, int, float, float, ui.Element]] = []
    seen: set[tuple[str, str, str]] = set()
    for off_screen, group in ((0, items), (1, off)):
        for e in group:
            texts = [e.identifier, e.name] if is_id else [e.label, e.value]
            ranks = [r for t in texts if t and (r := rank(query, t)) is not None]
            key = (e.role, e.text, e.identifier)
            if not ranks or key in seen:
                continue
            seen.add(key)
            found.append((min(ranks), off_screen, e.y, e.x, e))
    found.sort(key=lambda f: f[:4])
    return found[:MAX_CLOSEST]


def nearest(target: ui.Target, items: list[ui.Element], off: list[ui.Element] | None = None) -> list[ui.Element]:
    """The elements a `Closest:` line would name for this target (§16.2), for a repair's candidates (§26.1)."""
    return [f[4] for f in _nearest(target, items, off or [])]


def find(target: ui.Target, items: list[ui.Element], off: list[ui.Element], screen: tuple[float, float]) -> str:
    """`Closest: <element>, <element> (off screen, below).`, or "" when nothing is close."""
    found = _nearest(target, items, off)
    lines = [e.line(ref=bool(e.ref), actions=False, place=False) + (where(e, screen) if off_screen else "")
             for _, off_screen, _, _, e in found]
    return f"Closest: {', '.join(lines)}." if lines else ""

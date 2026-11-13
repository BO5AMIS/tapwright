"""`brief` (docs/AGENT-TOOLS 1.13 §13.4): the shorter answer. Pure functions over an answer's change block and over a
new screen's elements; the daemon decides when to call them (Daemon._after).

The first line, the notes, the errors and the lines about the alert, the keyboard and the network are never touched
here: this module sees only the change block and the element list."""
from __future__ import annotations

import re

from .ui import Element

LIMIT = 12  # lines of a change block, or of a new screen, that a brief answer shows
# A list row (`cell`) is one of them: the lead's reading of §13.4, agreed with Tapline as a clarification, since a list
# screen in brief would otherwise show none of the rows an agent came to tap. React Native's radio and checkbox too.
ACTIONABLE = ("button", "input", "switch", "link", "tab", "cell", "radio", "checkbox")
_CHANGE = re.compile(r"^[+\-~↕] ")
_NEW_TEXT = re.compile(r"^\+ e\d+ (text|header)\b")


def more(n: int) -> str:
    """The last line of a cut block."""
    return f"…{n} more: look shows them."


def cut(block: list[str]) -> list[str]:
    """A change block (`Same screen. Changes:`, the alert and keyboard lines, then the `+` `-` `~` `↕` lines) with at
    most LIMIT change lines. What is cut is chosen so that new text and headers stay ahead of everything else, then
    the rest in order; what stays keeps its order. The `Same screen. Changes:`, `Alert:`, `Keyboard:` and `Network:`
    lines are not change lines: they are never cut and don't count. A cut block ends with `…<n> more: look shows
    them.`"""
    where = [i for i, line in enumerate(block) if _CHANGE.match(line)]
    if len(where) <= LIMIT:
        return list(block)
    urgent = [i for i in where if _NEW_TEXT.match(block[i])]
    ranked = urgent + [i for i in where if i not in set(urgent)]
    keep = set(ranked[:LIMIT])
    gone = set(where) - keep
    return [line for i, line in enumerate(block) if i not in gone] + [more(len(gone))]


def screen(items: list[Element], before: list[Element]) -> list[str]:
    """The element lines a brief answer gives for a new screen: every text element that is new since `before` (the list
    the agent last had), the headers, then the actionable elements (buttons, inputs, switches, links, tabs, list rows), each
    group in reading order, at most LIMIT lines, then `…<n> more: look shows them.` when some were left out."""
    known = {e.text for e in before if e.role in ("text", "header")}
    new_text = [e for e in items if e.role == "text" and e.text not in known]
    headers = [e for e in items if e.role == "header"]
    actions = [e for e in items if e.role in ACTIONABLE]
    wanted = [*new_text, *headers, *actions]
    lines = [e.line() for e in wanted[:LIMIT]]
    return [*lines, more(len(wanted) - LIMIT)] if len(wanted) > LIMIT else lines

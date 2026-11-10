"""Refs that survive an action, and the change list an action answers with (docs/AGENT-TOOLS.md
§3.1 and §3.4). Pure functions over element lists, so they're testable without a phone."""
import math
from dataclasses import dataclass, replace

from .ui import Element, clip

NO_CHANGE = "No visible change."
SAME_SCREEN = "Same screen. Changes:"
MOVE_PT = 1.0  # a frame moved when x, y, width or height differ by more than this many points (WDA's
#                XML has whole points, so rounding alone can move a value by 1)
MOVED_LISTED = 5  # up to this many moved elements get a line each; more become a count
FULL_MIN = 8  # the full list replaces the changes above max(FULL_MIN, half the new list)


def highest(items: list[Element]) -> int:
    """The largest number among the refs e1, e2 ... of a list; 0 for none."""
    return max((int(e.ref[1:]) for e in items if e.ref[1:].isdigit()), default=0)


def _key(e: Element) -> tuple[str, str, str]:
    """What makes two elements "the same one" on different reads: the identifier, else the text (label,
    else value)."""
    return (e.role, "id", e.identifier) if e.identifier else (e.role, "text", e.text)


def _stayed(a: Element, b: Element) -> bool:
    return math.hypot(a.center[0] - b.center[0], a.center[1] - b.center[1]) <= MOVE_PT


def _shift(a: Element, b: Element) -> tuple[float, float]:
    return b.x - a.x, b.y - a.y


def _moved_by(a: Element, b: Element, shift: tuple[float, float]) -> bool:
    dx, dy = _shift(a, b)
    return abs(dx - shift[0]) <= MOVE_PT and abs(dy - shift[1]) <= MOVE_PT


def _content_shift(pairs: list[tuple[Element, Element]]) -> tuple[float, float] | None:
    """How far the content moved: the displacement most unique elements with an identifier or text agree on
    (within a point), if at least three do and it is a real move; else None."""
    shifts = [_shift(a, b) for a, b in pairs if a.identifier or a.text]
    best = max(shifts, key=lambda s: sum(abs(s[0] - t[0]) <= MOVE_PT and abs(s[1] - t[1]) <= MOVE_PT for t in shifts),
               default=None)
    if best is None:
        return None
    agreeing = sum(abs(best[0] - t[0]) <= MOVE_PT and abs(best[1] - t[1]) <= MOVE_PT for t in shifts)
    if agreeing < 3 or (abs(best[0]) <= MOVE_PT and abs(best[1]) <= MOVE_PT):
        return None  # too few agree, or they all stayed (fixed bars): nothing says the content moved
    return round(best[0]), round(best[1])


def carry_refs(old: list[Element], new: list[Element], used: int = 0,
               moves_content: bool | None = False) -> list[Element]:
    """`new` (a fresh read, in reading order) with the refs of the elements still on screen, and new
    numbers for the others, counted on from the highest ref in use. `used` is the highest number this
    agent was ever given, so a ref that went away is never handed to another element.

    `moves_content` None is a read with no action of the agent's before it (the re-read a target is
    resolved on): the content counts as moved only if the unique elements agree that it did. Calling
    every such read a move gave each element without an identifier a new ref on almost every answer.

    A key (see _key) that is unique in both lists keeps its ref however far the element moved, except
    an element with neither identifier nor text, which keeps it only if it stayed put. A key shared by
    several elements keeps its refs only if the group is the same size in both lists, every member
    stayed put, the action wasn't one that moves content (`moves_content`: scroll, swipe, back, open_app,
    open_url) and no unique element moved: positions alone can't tell "Edit" of row A from "Edit" of row B
    after a scroll by one row, and a wrong guess presses the wrong row. Else every member is new."""
    before: dict[tuple[str, str, str], list[Element]] = {}
    after: dict[tuple[str, str, str], list[Element]] = {}
    for e in old:
        before.setdefault(_key(e), []).append(e)
    for e in new:
        after.setdefault(_key(e), []).append(e)
    kept: dict[int, str] = {}  # id(new element) -> the ref it keeps
    pairs = [(before[key][0], group[0]) for key, group in after.items()
             if len(group) == 1 and len(before.get(key, [])) == 1]
    if moves_content is None:
        moves_content = _content_shift(pairs) is not None
    content_moved = moves_content
    shift = _content_shift(pairs) if moves_content else None
    for was, now in pairs:
        if moves_content and not was.identifier:
            # no identifier: a same-named element elsewhere may be another row's. It keeps its ref only
            # if it moved the way the content did; with no estimate of that, never.
            ok = shift is not None and _moved_by(was, now, shift)
        else:
            ok = bool(_key(was)[1] == "id" or was.text or _stayed(was, now))
        if ok:
            kept[id(now)] = was.ref
            content_moved = content_moved or not _stayed(was, now)
    for key, group in after.items():
        was = before.get(key, [])
        if (not content_moved and not (len(was) == 1 and len(group) == 1) and len(was) == len(group)
                and all(_stayed(o, n) for o, n in zip(was, group))):
            kept.update((id(n), o.ref) for o, n in zip(was, group))
    last = max(highest(old), used)
    out: list[Element] = []
    for e in new:
        ref = kept.get(id(e))
        if ref is None:
            last += 1
            ref = f"e{last}"
        out.append(replace(e, ref=ref))
    return out


def _flag(v: bool) -> str:
    return f'"{str(v).lower()}"'


def _was(old: Element, new: Element) -> list[str]:
    """`field=<old value>` for each of text, value, checked, selected, disabled and actions that changed.
    A value that only mirrors the text, and a switch's value (it is `checked`), aren't fields of their own.
    The frame is never one: a moved or resized element is a `↕`."""
    out = []
    if old.text != new.text:
        out.append(("text", clip(old.text)))
    if (old.value != new.value and old.checked is None and new.checked is None
            and (old.value != old.text or new.value != new.text)):
        out.append(("value", clip(old.value)))
    if old.checked != new.checked:
        out.append(("checked", "null" if old.checked is None else _flag(old.checked)))
    if old.selected != new.selected:
        out.append(("selected", _flag(old.selected)))
    if old.enabled != new.enabled:
        out.append(("disabled", _flag(not old.enabled)))
    if old.actions != new.actions:
        out.append(("actions", clip(", ".join(old.actions))))
    return [f"{name}={v}" for name, v in out] if len(out) > 1 else [v for _, v in out]


def _gone(e: Element) -> str:
    """`- <role> "<text>"`, else `<role> id=<id>`, else the role alone: no ref, no position."""
    if e.text:
        return f"- {e.role} {clip(e.text)}"
    return f"- {e.role} id={e.identifier}" if e.identifier else f"- {e.role}"


def _frame_moved(old: Element, new: Element) -> bool:
    return any(abs(a - b) > MOVE_PT for a, b in zip(old.rect, new.rect))


def _changes(old: list[Element], new: list[Element]) -> tuple[list[str], int]:
    """(the change lines, how many elements changed) between two lists whose refs agree (see carry_refs)."""
    before, after = {e.ref: e for e in old}, {e.ref: e for e in new}
    added = [f"+ {e.line()}" for e in new if e.ref not in before]
    gone = [_gone(e) for e in old if e.ref not in after]
    modified, moved = [], []
    for e in new:
        o = before.get(e.ref)
        if o is None:
            continue
        was = _was(o, e)
        if was:
            modified.append(f"~ {e.line()} (was {', '.join(was)})")
        elif _frame_moved(o, e):
            moved.append(e)
    lines = added + gone + modified
    if len(moved) > MOVED_LISTED:
        lines.append(f"↕ {len(moved)} elements moved")
    else:
        lines += [f"↕ {e.line()}" for e in moved]
    return lines or [NO_CHANGE], len(added) + len(gone) + len(modified) + len(moved)


def diff(old: list[Element], new: list[Element]) -> list[str]:
    """The change lines from `old` to `new`, whose refs came from carry_refs: `+` new, `-` gone (no ref),
    `~` changed (with the old value), `↕` moved; or just "No visible change."."""
    return _changes(old, new)[0]


@dataclass
class Update:
    """What an action answers with, and the list the agent holds afterwards."""
    items: list[Element]
    full: bool  # the full list replaces the changes
    lines: list[str]  # the change block, headed by "Same screen. Changes:"; empty when `full`
    top: int  # the highest ref number issued so far


def update(old: list[Element], new: list[Element], app_changed: bool = False, used: int = 0,
           shown: list[Element] | None = None, full: bool = False, header: list[str] | None = None,
           moves_content: bool = False) -> Update:
    """After an action: carry the refs over, and say what changed. The full list instead when the app changed,
    when there was no list before, or when more than max(8, half the new list) of the elements changed, or
    when nothing is left of a list that had elements (a column of `-` lines would only hide that the screen is
    covered). A full list keeps numbering too (AGENT-TOOLS v1.4): what stayed keeps its ref and everything else
    counts on from the highest ref given, so an older ref never lands on another element. Only `look` starts
    again at e1. After another app came to the front, or when `full` is forced, nothing is carried.
    `old` is the list whose refs carry; `shown` is what the agent was last told, when a silent re-read
    moved `old` past it: the changes are measured against that, so nothing it hasn't heard of is lost.
    `full` forces the full list (a person acted, the line dropped, the agent never looked).
    `header` lines (an alert or the keyboard changed) come first in the change block."""
    base = old if shown is None else shown
    if full or app_changed:
        fresh = carry_refs([], new, max(used, highest(old)))
        return Update(fresh, True, [], max(used, highest(old), highest(fresh)))
    carried = carry_refs(old, new, used, moves_content)
    top = max(used, highest(old), highest(carried))
    if (not base and bool(new)) or (bool(base) and not new):
        return Update(carried, True, [], top)
    lines, count = _changes(base, carried)
    if count > max(FULL_MIN, len(new) / 2):
        return Update(carried, True, [], top)
    if header:
        lines = [*header, *([] if lines == [NO_CHANGE] else lines)]
    return Update(carried, False, [SAME_SCREEN, *lines], top)

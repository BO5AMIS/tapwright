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
    """What makes two elements "the same one" on different reads."""
    return (e.role, "id", e.identifier) if e.identifier else (e.role, "label", e.label)


def carry_refs(old: list[Element], new: list[Element], used: int = 0) -> list[Element]:
    """`new` (a fresh read, in reading order) with the refs of the elements still on screen, and new
    numbers for the others, counted on from the highest ref in use. `used` is the highest number this
    agent was ever given, so a ref that went away is never handed to another element.
    Elements with the same key are paired nearest first, by the centre of their rect, in reading order."""
    unclaimed: dict[tuple[str, str, str], list[Element]] = {}
    for e in old:
        unclaimed.setdefault(_key(e), []).append(e)
    last = max(highest(old), used)
    out: list[Element] = []
    for e in new:
        pool = unclaimed.get(_key(e))
        if pool:
            cx, cy = e.center
            nearest = min(pool, key=lambda o: math.hypot(o.center[0] - cx, o.center[1] - cy))
            pool.remove(nearest)
            out.append(replace(e, ref=nearest.ref))
        else:
            last += 1
            out.append(replace(e, ref=f"e{last}"))
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
           shown: list[Element] | None = None) -> Update:
    """After an action: carry the refs over, and say what changed. The full list instead (renumbered from
    e1, so every older ref is invalid) when the app changed, when there was no list before, or when more
    than max(8, half the new list) of the elements changed, or when nothing is left of a list that had
    elements (a column of `-` lines would only hide that the screen is covered).
    `old` is the list whose refs carry; `shown` is what the agent was last told, when a silent re-read
    moved `old` past it: the changes are measured against that, so nothing it hasn't heard of is lost."""
    carried = carry_refs(old, new, used)
    base = old if shown is None else shown
    lines, count = _changes(base, carried)
    if app_changed or (not base and bool(new)) or (bool(base) and not new) or count > max(FULL_MIN, len(new) / 2):
        return Update(list(new), True, [], highest(new))
    return Update(carried, False, [SAME_SCREEN, *lines], max(used, highest(carried)))

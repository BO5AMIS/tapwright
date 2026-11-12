"""The accessibility audit's report (docs/AGENT-TOOLS.md §10, v1.8): XCTest's own issues, named by the agent's element
lines, in the format both tools share, plus `duplicate_id` from the tree."""
from __future__ import annotations

import json
import re
from collections import Counter

from . import ui

# The shared type words and XCTest's names for them (XCUIAccessibilityAuditType…)
XCTEST = {
    "contrast": "XCUIAccessibilityAuditTypeContrast",
    "element_detection": "XCUIAccessibilityAuditTypeElementDetection",
    "hit_region": "XCUIAccessibilityAuditTypeHitRegion",
    "description": "XCUIAccessibilityAuditTypeSufficientElementDescription",
    "dynamic_type": "XCUIAccessibilityAuditTypeDynamicType",
    "clipped_text": "XCUIAccessibilityAuditTypeTextClipped",
    "trait": "XCUIAccessibilityAuditTypeTrait",
}
FROM_XCTEST = {v: k for k, v in XCTEST.items()}
OURS = ("duplicate_id",)  # checked from the tree, not by XCTest
TYPES = (*XCTEST, *OURS, "overlap")  # every word AGENT-TOOLS §10 knows; overlap is Tapline's
CHECKED_HERE = (*XCTEST, *OURS)
MAX_ISSUES = 30

# What is wrong (when XCTest's own words are missing) and how to fix it, per type
WHAT = {
    "contrast": "the text is hard to read against its background",
    "element_detection": "VoiceOver can't find it as a separate element",
    "hit_region": "too small to tap reliably",
    "description": "its description doesn't say what it is or does",
    "dynamic_type": "it doesn't grow with the user's text size",
    "clipped_text": "its text is cut off",
    "trait": "its traits don't say what kind of control it is",
    "duplicate_id": "another element has the same id",
}
FIX = {
    "contrast": "raise the contrast to at least 4.5:1 (3:1 for large text).",
    "element_detection": "make it its own accessibility element, or group it with its label.",
    "hit_region": "make it at least 44x44 pt.",
    "description": "give it an accessibility label that names it (an icon button needs one).",
    "dynamic_type": "use a text style that scales (Dynamic Type), and let the layout grow.",
    "clipped_text": "let the text wrap or the view grow; don't fix its height.",
    "trait": "give it the right role (button, header, switch with its state).",
    "duplicate_id": "give each element its own id (testID).",
}


def element_of(attrs: dict, items: list[ui.Element]) -> str:
    """The issue's element as a line: the agent's own line with its ref when the tree has it (same id, else the same
    place and label), else a line made from XCTest's attributes, without a ref. Empty when there is none."""
    if not attrs:
        return ""
    ident = str(attrs.get("name") or attrs.get("identifier") or "")
    label = str(attrs.get("label") or "")
    box = _box(attrs.get("rect") or attrs.get("frame"))
    for e in items:
        if ident and (e.identifier == ident or e.name == ident):
            return e.line()
    for e in items:
        if any(box[2:]) and tuple(round(v) for v in e.rect) == box and (not label or label in (e.label, e.value, e.text)):
            return e.line()
    # the tree has no such element: say what XCTest itself says of it (its label or id, and its frame in points)
    kind = str(attrs.get("type") or "").removeprefix("XCUIElementType")
    # what the app's own text says is shown as element lines show text: one line, quoted and capped (review 1.3.1:
    # a raw label could add a line of its own to the answer)
    named = f" {ui.clip(label)}" if label else ""
    plain_id = " ".join(str(ident).split())[:ui.TEXT_LIMIT] if ident else ""
    shown = f" id={plain_id}" if plain_id else ""
    if not kind:
        if not (label or ident or any(box[2:])):
            return ""
        return f"an element{named}{shown} at @{box[0]},{box[1]} {box[2]}x{box[3]}" if any(box[2:]) else f"an element{named}{shown}"
    role = ui.ROLES.get(kind, kind.lower())
    return f"{role}{named}{shown} @{box[0]},{box[1]} {box[2]}x{box[3]}" if any(box[2:]) else f"{role}{named}{shown}"


def _box(rect: object) -> tuple[int, int, int, int]:
    """XCTest's frame as whole points (x, y, w, h): a dict with x, y, width, height, or a "{{x, y}, {w, h}}" string.
    Zeros when it is neither."""
    try:
        if isinstance(rect, dict):
            return tuple(round(float(rect.get(k, 0) or 0)) for k in ("x", "y", "width", "height"))  # type: ignore[return-value]
        if isinstance(rect, str):
            x, y, w, h = (round(float(n)) for n in re.findall(r"-?\d+(?:\.\d+)?", rect)[:4])
            return x, y, w, h
    except (TypeError, ValueError, OverflowError):  # (an infinite or absurd number is no frame)
        pass
    return 0, 0, 0, 0


def duplicates(items: list[ui.Element]) -> list[tuple[str, str]]:
    """(`duplicate_id`, element line) for every element after the first that shares an id with another."""
    counts = Counter(e.identifier for e in items if e.identifier)
    seen: set[str] = set()
    found = []
    for e in items:
        if e.identifier and counts[e.identifier] > 1:
            if e.identifier in seen:
                found.append(("duplicate_id", e.line()))
            seen.add(e.identifier)
    return found


NO_TRAIT_FIX = 'give it accessibilityRole="button" (or a native control) with accessibilityState.'


def no_trait(items: list[ui.Element]) -> list[tuple[str, str, str]]:
    """(`trait`, element line, what) for every element listed for its label alone, as React Native's radio and checkbox
    options are (an `Other` with no trait: VoiceOver can focus it but doesn't announce it as a control)."""
    return [("trait", e.line(), f'no trait: VoiceOver doesn\'t announce {json.dumps(e.text or e.role, ensure_ascii=False)}'
             " as a control") for e in items if e.no_trait]


def rows_of(issues: list[dict], items: list[ui.Element], types: list[str]) -> list[tuple[str, str, str]]:
    """(type, element line, what) for each issue of the asked types, XCTest's first, then `duplicate_id`. Shared by
    `audit`'s report and `ux_check`."""
    rows: list[tuple[str, str, str]] = []
    for issue in issues:
        kind = FROM_XCTEST.get(str(issue.get("auditType") or ""), "")
        if not kind or kind not in types:
            continue
        what = str(issue.get("compactDescription") or "").strip().rstrip(".") or WHAT[kind]
        rows.append((kind, element_of(issue.get("elementAttributes") or {}, items), what))
    if "trait" in types:
        rows += no_trait(items)
    if "duplicate_id" in types:
        rows +=[(kind, line, WHAT[kind]) for kind, line in duplicates(items)]
    return rows


def report(screen: str, issues: list[dict], items: list[ui.Element], types: list[str]) -> str:
    """The answer (AGENT-TOOLS §10): a count line, one numbered line per issue, the `Checked:` line."""
    rows = rows_of(issues, items, types)
    checked = [t for t in TYPES if t in types and t in CHECKED_HERE]
    not_here = [t for t in TYPES if t in types and t not in CHECKED_HERE]
    tail = f"Checked: {', '.join(checked)}." + (f" Not checked here: {', '.join(not_here)}." if not_here else "")
    if not rows:
        return f"Audit of {screen}: no issues found.\n{tail}"
    by_kind = Counter(r[0] for r in rows)
    counts = ", ".join(f"{k} {n}" for k, n in sorted(by_kind.items(), key=lambda kv: TYPES.index(kv[0])))
    lines = [f"Audit of {screen}: {len(rows)} issue{'s' if len(rows) != 1 else ''} ({counts})."]
    for n, (kind, line, what) in enumerate(rows[:MAX_ISSUES], 1):
        lines.append(f"{n}. {kind}: {line or 'the screen'}: {what}. Fix: {NO_TRAIT_FIX if what.startswith('no trait:') else FIX[kind]}")
    if len(rows) > MAX_ISSUES:
        lines.append(f"(+{len(rows) - MAX_ISSUES} more issues; pass types to narrow it.)")
    lines.append(tail)
    return "\n".join(lines)

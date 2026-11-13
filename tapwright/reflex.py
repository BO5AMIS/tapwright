"""Reflexes (AGENT-TOOLS §18.7): rules the tool applies by itself when their moment comes, between the steps of a call.

Declared, per app, in the map: `{"name": "rate_prompt", "when": {"text": "Rate Wayfare"}, "do": {"tool": "tap", "target":
{"text": "Not now"}}, "max": 1, "at": "home"}`. `when` is something on screen; `do` is one step; `max` the firings a call may
have (1 by default, 3 at most); `at` a place the reflex is limited to. The daemon checks the policy again at the moment
one fires (the policy file can change, and the target can carry another word on another screen), never fires one at a
permission prompt, and says every firing as a `Reflex:` line.

Built in: wait while a loading indicator is on screen instead of answering `not_found`, and close a keyboard that is in
the way. Pure functions: no phone.
"""
from __future__ import annotations

from . import batch, ui
from .control import Refused

LOADING_TYPES = ("ActivityIndicator", "ProgressIndicator")
LOADING_ROLES = ("activityindicator", "progressindicator")
DEFAULT_MAX = 1
PAST = {"tap": "tapped", "long_press": "long-pressed", "toggle": "toggled", "back": "went back"}


def _when(reflex: object) -> tuple[str, str] | None:
    """What a reflex waits for as (kind, value), or None for a reflex that isn't well formed."""
    if not isinstance(reflex, dict) or not isinstance(reflex.get("when"), dict) or not isinstance(reflex.get("do"), dict):
        return None
    when = reflex["when"]
    for kind in ("text", "label", "id"):
        if isinstance(when.get(kind), str) and when[kind]:
            return kind, when[kind]
    return None


def due(reflexes: list, items: list[ui.Element], place: str | None, fired: dict[str, int]) -> list[dict]:
    """The reflexes whose moment has come: what they wait for is on screen, they are at their place (when they have
    one), and they have fired fewer times than their `max` in this call."""
    out = []
    for r in reflexes:
        waits = _when(r)
        if waits is None or not isinstance(r.get("name"), str):
            continue
        if r.get("at") is not None and r["at"] != place:
            continue
        limit = r.get("max", DEFAULT_MAX)
        if fired.get(r["name"], 0) >= (limit if isinstance(limit, int) and not isinstance(limit, bool) else DEFAULT_MAX):
            continue
        try:
            ui.resolve(items, ui.Target(**{waits[0]: waits[1]}, index=0))
        except Refused:
            continue
        out.append(r)
    return out


def seen(reflex: dict) -> str:
    """What the reflex waits for, as a person reads it: `Rate Wayfare`."""
    when = reflex["when"]
    return next(str(when[k]) for k in ("text", "label", "id") if isinstance(when.get(k), str) and when[k])


def touched(reflex: dict, items: list[ui.Element]) -> ui.Element | None:
    """The element the reflex's `do` would touch on this screen, or None (a `back`, or a target that isn't here)."""
    target = (reflex.get("do") or {}).get("target")
    if not isinstance(target, dict):
        return None
    try:
        found = ui.resolve(items, ui.parse_target(target))
    except Refused:
        return None
    return found if isinstance(found, ui.Element) else None


def word(element: ui.Element) -> str:
    """The word the safety policy looks at: an input by its label (what is typed in it is data), the rest by their text."""
    return element.label if element.role == "input" else element.text


def aim(reflex: dict) -> str | None:
    """How the `do` names what it touches: `"Not now"`, or `id=not-now`; None for a `back`."""
    target = (reflex.get("do") or {}).get("target")
    if not isinstance(target, dict):
        return None
    kind = next((k for k in ("id", "label", "text") if isinstance(target.get(k), str)), None)
    return batch.aim_name(kind, target[kind]) if kind else None


def said(reflex: dict, seen_text: str, aimed: str | None) -> str:
    """`Reflex: closed "Rate Wayfare" (tapped "Not now").`"""
    do = reflex["do"]["tool"]
    did = PAST.get(do, do) + (f" {aimed}" if aimed else "")
    if do == "tap":
        return f'Reflex: closed "{seen_text}" ({did}).'
    return f'Reflex: {reflex["name"]}: "{seen_text}" was on screen ({did}).'


def loading(items: list[ui.Element]) -> bool:
    """Is a loading indicator on screen? Then a target that isn't there yet may be coming (§18.7)."""
    return any(e.type in LOADING_TYPES or e.role in LOADING_ROLES for e in items)


def waited(seconds: float) -> str:
    return f"Reflex: waited {seconds:.1f} s for loading to finish."


def keyboard(how: str) -> str:
    return f"Reflex: closed the keyboard ({how})."

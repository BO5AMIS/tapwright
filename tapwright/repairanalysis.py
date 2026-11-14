"""Analysing a break for self-repair (docs/AGENT-TOOLS.md §26.1, §26.3): from the failed step and the screen it failed on, the
candidates, best first, without touching anything. `replay` with `repair: "suggest"` stops here; `apply` goes on to try them
(scriptrepairrun.py). Pure: the screen is plain data (`Screen`), so it is tried without a phone. The score and the rules of what
is never repaired are scriptrepair.py's."""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from . import batch, explorefloor, roads, ui
from .scriptrepair import (MAX_SCROLLS, NEAR_TIE, Analysis, Candidate, Old, Scored, aim_names, aim_of, bucket_of, can_take,
                           clean_seen, element_label, format_parts, near_tie, never_repaired, old_of, parts_of, rank, seen_of,
                           step_name, weighted)


@dataclass
class Break:
    """The first step that failed: its number (1-based, in the steps run), the code and the reason that follows the step's
    name in the answer."""
    index: int
    code: str
    reason: str

    @property
    def off_screen(self) -> bool:
        return self.code == "not_actionable" and self.reason.startswith("off_screen")


def parse_break(text: str, code: str, steps: list[dict]) -> Break | None:
    """The break of a stopped run's answer (`Ran 3 of 7 steps; stopped at step 4 (tap id=x): <reason>`), or None when the run
    didn't stop at a step."""
    found = re.match(r"Ran \d+ of \d+ steps; stopped at step (\d+) \(", text)
    if found is None:
        return None
    index = int(found.group(1))
    if not 1 <= index <= len(steps):
        return None
    prefix = f"{found.group(0)[:-2]} ({batch.summary(steps[index - 1])}): "
    if not text.startswith(prefix):
        return None
    return Break(index, code, text[len(prefix):].split("\n")[0])


def _no_way(a: str, b: str) -> list[tuple[dict, dict]] | None:
    return None


@dataclass
class Screen:
    """What the analysis reads from the phone. `here` is the known place the app is on (None: none known), `nearest` the
    elements a `Closest:` line would name, `way(a, b)` the proven road of taps from place a to place b as (step, names of its
    road) pairs, or None when there is none."""
    items: list[ui.Element]
    screen: tuple[float, float]
    floor: explorefloor.Floor
    back: ui.Back | None = None
    here: str | None = None
    nearest: list[ui.Element] = field(default_factory=list)
    scrollable: bool = False
    way: Callable[[str, str], list[tuple[dict, dict]] | None] = _no_way
    scrub: Callable[[str], str] = lambda text: text


def next_target(step: dict | None) -> dict | None:
    """The target of the step after, by id, label or text: what must resolve to exactly one element after a repair whose step
    has no `expect` of its own (§26.4)."""
    target = step.get("target") if isinstance(step, dict) else None
    if isinstance(target, dict) and any(isinstance(target.get(k), str) for k in ("id", "label", "text")):
        return target
    return None


def has_check(steps: list[dict], at: int) -> bool:
    """§26.4 rule 1 can be checked for this step: it has an `expect`, or the step after it has a target to resolve. A last
    step with no `expect` has no check, so it isn't repaired."""
    return "expect" in steps[at] or next_target(steps[at + 1] if at + 1 < len(steps) else None) is not None


def _usable(name: str | None, scrub: Callable[[str], str]) -> bool:
    return name is None or (not ui.private(name) and scrub(name) == name)


def target_for(element: ui.Element, items: list[ui.Element], tool: str, scrub: Callable[[str], str]) -> dict | None:
    """The target an element is named by: its id, else its label, else its text, with `index` where several answer to it (a
    recording's rule). None for a list row with no id (its text is content), for a point, and for a name that holds a private
    value (a vault value never goes into a file)."""
    if element.role == "cell" and not element.identifier:
        return None
    found = batch.recorded_element(element, items, tool)
    if "x" in found or not all(_usable(found.get(k), scrub) for k in ("id", "label", "text")):
        return None
    return found


def analyse(steps: list[dict], at: int, brk: Break, screen: Screen, drafts_only: bool = False) -> Analysis:
    """The candidates for the break at `steps[at]` (§26.1), best first. `none` says why the step isn't repaired; `tie` that the
    best two scores can't be told apart (§26.3)."""
    step = steps[at]
    never = never_repaired(step, screen.floor, drafts_only)
    if never is not None:
        return Analysis("none", reason=never)
    if not (brk.code in ("not_found", "ambiguous") or brk.off_screen):
        return Analysis("none", reason="an expect that fails is a finding, never a break to repair" if brk.code == "expect_failed"
                        else f"a {brk.code} failure is a finding, not a break to repair")
    if not has_check(steps, at):
        return Analysis("none", reason="the step has no check (an expect, or a target in the step after it), so a repair "
                                       "couldn't be checked")
    expected, here = step.get("place"), screen.here
    if isinstance(expected, str) and here is not None and expected != here:  # a new screen in the way (§26.1)
        return _road(step, expected, here, screen)
    target = step["target"]
    try:
        tgt = ui.parse_target({k: v for k, v in target.items() if k != "index"})
    except Exception:  # noqa: BLE001 - a target written badly by hand: there is nothing to repair it from
        return Analysis("none", reason="the step's target isn't one a repair can read")
    matching = ui.candidates(screen.items, tgt)
    old = old_of(step, {e.role for e in matching} if brk.code == "ambiguous" else None)
    if step["tool"] == "toggle" and old.role is None:
        return Analysis("none", reason="the toggle's old role isn't known (the step has no seen), so it can't be repaired "
                                       "only to an element of the same role")
    found: list[Candidate] = []
    if brk.off_screen or (brk.code == "not_found" and not matching and screen.scrollable):
        found.append(Candidate(
            "scroll", f"scroll down (up to {MAX_SCROLLS} times) until {aim_of(target)} shows, then {step_name(step)}",
            f"before it: scroll down toward {aim_of(target)}",
            f"{aim_of(target)} {'was off the screen' if brk.off_screen else 'was not on the screen'}; the list is scrolled "
            "down until it shows.", place=here))
    if not brk.off_screen:
        ranked = _ranked(step, brk, screen, old, matching)
        if near_tie(ranked):
            if not found:
                two = f"{ranked[0].score:.2f} and {ranked[1].score:.2f}"
                return Analysis("tie", reason=f"the best two scores ({two}) are less than {NEAR_TIE:g} apart, so it can't "
                                              "tell them apart")
        else:
            found.extend(_candidate(step, brk, screen, s, here) for s in ranked)
    if not found:
        return Analysis("none", reason=f"nothing on the screen can take the step's {step['tool']} and looks like {aim_of(target)}")
    return Analysis("ok", found)


def _ranked(step: dict, brk: Break, screen: Screen, old: Old, matching: list[ui.Element]) -> list[Scored]:
    tool, items = step["tool"], screen.items
    near = {id(e) for e in screen.nearest}
    seen = clean_seen(step.get("seen"))
    scored: list[Scored] = []
    for element in items:
        if not can_take(tool, element, step):
            continue
        if brk.code == "ambiguous" and element not in matching:
            continue
        if tool == "toggle" and element.role != old.role:
            continue  # a toggle is repaired only to an element of the same role (§26.2)
        if ui.is_back(element, screen.back) or screen.floor.touch(element, items) is not None:
            continue  # never a native Back, and never an element that carries a policy word (§26.2)
        if target_for(element, items, tool, screen.scrub) is None:
            continue
        parts = parts_of(old, element, screen.screen)
        score = weighted(parts)
        if score is None:
            continue
        in_bucket = bool(seen.get("role") and seen.get("bucket") and element.role == seen["role"]
                         and bucket_of(element.rect, screen.screen) == seen["bucket"])
        if not (brk.code == "ambiguous" or id(element) in near or in_bucket or parts.get("id_stem", 0) > 0
                or parts.get("words", 0) >= 0.5):
            continue  # the pool of §26.1: a `Closest:` match, a similar id, label or text, or the same role in the same bucket
        scored.append(Scored(element, parts, score))
    return rank(scored)


def _candidate(step: dict, brk: Break, screen: Screen, scored: Scored, here: str | None) -> Candidate:
    element, tool = scored.element, step["tool"]
    target = target_for(element, screen.items, tool, screen.scrub) or {}
    was = aim_of(step["target"])
    cause = f"{was} matched more than one element" if brk.code == "ambiguous" else f"{was} wasn't found"
    shown = element_label(element)
    summary = f"{tool} {aim_of(target)}, {shown}" + (f" on {here}" if here else "")
    why = f"{cause}; {aim_of(target)} can take a {tool.replace('_', ' ')} and is the closest ({format_parts(scored.parts)})."
    return Candidate("target", summary, shown, why, scored.score, scored.parts, target,
                     seen_of(element, screen.items, screen.screen), place=here, names=roads.names_of(element))


def _road(step: dict, expected: str, here: str, screen: Screen) -> Analysis:
    """A new screen in the way: the proven road from where the app is to the place the step expected, put before it."""
    road = screen.way(here, expected)
    if not road:
        return Analysis("none", reason=f"the step ran on {expected}, the app is on {here}, and no proven road of taps leads there")
    for move, names in road:
        words = [*aim_names(move.get("target")), *(explorefloor.id_words(v) if k == "id" else v
                                                    for k, v in names.items() if isinstance(v, str))]
        if any(screen.floor.named(w) is not None for w in words):
            return Analysis("none", reason=f"a road move to {expected} can't be inserted: it carries a policy word")
    moves = [move for move, _ in road]
    shown = ", ".join(step_name(m) for m in moves)
    return Analysis("ok", [Candidate("road", f"insert {shown} to reach {expected}, then {step_name(step)}", f"before it: {shown}",
                                     f"the step ran on {here}, not on {expected}; the proven road {here} to {expected} is put "
                                     "in front of it.", moves=moves, place=expected, place_from=here)])

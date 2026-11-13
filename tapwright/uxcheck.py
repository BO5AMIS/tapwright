"""`ux_check` (docs/AGENT-TOOLS.md §17.3): one screen against the usability basics, as measured facts first, each with
the element it concerns and its basis; and the user's house rules (`--ux-rules FILE`). Pure logic over the element
list and the screen size: no phone, no network. The accessibility part (cut-off text, contrast, target size) comes
from the audit's rows (audit.rows_of), which the daemon reads from XCTest.

The heuristics, all from the tree (it holds frames and roles, not colours):
- Competing choices: enabled elements a finger can act on (buttons, links, tabs, inputs, switches, sliders), counted
  apart from list rows (cells). More than CHOICES_MAX compete.
- The main action: among the buttons at least a quarter of the screen wide and 36 pt tall (a nav-bar icon or a text
  link is not a main action), the one with the largest area. It is clear when it is the only such button or at least
  1.5 times the area of the next; otherwise two or more look equally important.
- Thumb reach: portrait phones only. The bottom 40% of the screen height is the easy zone; above the top third is hard
  one-handed. A main action (any candidate with at least 60% of the largest area) whose centre is in the top third
  is a finding.
- An icon-only button: a button with no label, no value.
- Naming drift: buttons, links and tabs whose label is a known synonym of an action word (Save/Done, Log in/Sign in,
  Delete/Remove ...) that another screen seen earlier by this agent names differently.
- An error or empty state: a text saying error, failed, no results, empty, nothing... with no button or link within
  NEXT_STEP_REACH points below it (or above, up to a third of that).
"""
from __future__ import annotations

import contextvars
import json
import re
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path

from . import ui

VERSION = 1
CHOICES_MAX = 10  # more actionable elements than this on one screen compete
MAIN_MIN_WIDTH, MAIN_MIN_HEIGHT = 0.25, 36.0  # a main action is at least this share of the width, and this tall
MAIN_CLEAR = 1.5  # the main action is clear when it has this many times the area of the next button
REACH_PHONE_MAX = 600.0  # points: a screen narrower than this is a phone
REACH_HARD = 1 / 3  # above this share of the height is hard to reach one-handed
REACH_EASY = 0.6  # from this share down (the bottom 40%) is easy
NEXT_STEP_REACH = 200.0  # points below an error or empty text that a button or link counts as its next step
MAX_FINDINGS = 30
SEEN_SCREENS, SEEN_CLIENTS = 40, 64  # what the naming memory keeps

ACTIONABLE = ("tap", "toggle", "type")
ACTION_ROLES = {"button", "link", "tab", "input", "switch", "slider"}
STATE_WORDS = re.compile(r"\b(error|errors|failed|failure|no results|empty|nothing|couldn.t|could not|can.t|cannot|"
                         r"something went wrong|no [a-z ]{0,20} (yet|found))\b", re.IGNORECASE)
BACK_WORDS = {"back", "close", "cancel", "done", "dismiss", "x", "✕", "×", "‹", "<"}

# Synonym groups for naming drift: a whole label (lowercase, punctuation out) in one group names the same action.
SYNONYMS = {
    "save": ("save", "done", "submit", "apply", "confirm", "ok", "finish"),
    "sign in": ("sign in", "log in", "login", "signin"),
    "sign out": ("sign out", "log out", "logout", "signout"),
    "sign up": ("sign up", "register", "create account", "join", "get started"),
    "delete": ("delete", "remove", "discard", "trash"),
    "cancel": ("cancel", "close", "dismiss", "not now"),
    "next": ("next", "continue"),
    "add": ("add", "create", "new"),
    "edit": ("edit", "change", "modify"),
    "search": ("search", "find"),
    "share": ("share", "send"),
}
_GROUP_OF = {phrase: group for group, phrases in SYNONYMS.items() for phrase in phrases}


# ---- house rules ----
_KEYS = ("version", "rules")
_MEASURED = ("max_taps_to_goal", "way_back")


@dataclass(frozen=True)
class Rules:
    """The user's house rules. `texts` are handed to the agent to judge; the measured keys the tool checks."""
    texts: tuple[str, ...] = ()
    max_taps_to_goal: int | None = None
    way_back: bool = False

    def __bool__(self) -> bool:
        return bool(self.texts) or self.max_taps_to_goal is not None or self.way_back

    def to_obj(self) -> dict:
        rules: list = list(self.texts)
        if self.max_taps_to_goal is not None:
            rules.append({"max_taps_to_goal": self.max_taps_to_goal})
        if self.way_back:
            rules.append({"way_back": True})
        return {"version": VERSION, "rules": rules}

    def dumps(self) -> str:
        """One line of ASCII JSON: fit for an HTTP header."""
        return json.dumps(self.to_obj(), separators=(",", ":"), ensure_ascii=True)

    def listing(self) -> list[str]:
        """Every rule as the report names it (§17.3: each rule appears in the report)."""
        out = list(self.texts)
        if self.max_taps_to_goal is not None:
            out.append(f"max_taps_to_goal: {self.max_taps_to_goal}")
        if self.way_back:
            out.append("way_back: every screen has a visible way back (a back button, a close control or a tab bar)")
        return out


def parse(obj: object) -> Rules:
    """House rules from their JSON object. Raises ValueError, saying what is wrong (an unknown key included)."""
    if not isinstance(obj, dict):
        raise ValueError("the ux rules must be a JSON object")
    extra = sorted(set(obj) - set(_KEYS))
    if extra:
        raise ValueError(f"unknown key {extra[0]!r} in the ux rules; the keys are {', '.join(_KEYS)}")
    if obj.get("version", VERSION) != VERSION:
        raise ValueError(f"version must be {VERSION}, not {obj['version']!r}")
    items = obj.get("rules", [])
    if not isinstance(items, list):
        raise ValueError("rules must be a list of rules")
    texts: list[str] = []
    taps: int | None = None
    way_back = False
    for rule in items:
        if isinstance(rule, str):
            if not rule.strip():
                raise ValueError("a rule written as text must not be empty")
            texts.append(" ".join(rule.split()))
        elif isinstance(rule, dict):
            if len(rule) != 1:
                raise ValueError(f"a measured rule has exactly one key ({', '.join(_MEASURED)}), not {sorted(rule)!r}")
            (key, value), = rule.items()
            if key not in _MEASURED:
                raise ValueError(f"unknown key {key!r} in a rule; the measured keys are {', '.join(_MEASURED)}")
            if key == "max_taps_to_goal":
                if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                    raise ValueError(f"max_taps_to_goal must be a whole number of 1 or more, not {value!r}")
                taps = value if taps is None else min(taps, value)
            else:
                if not isinstance(value, bool):
                    raise ValueError(f"way_back must be true or false, not {value!r}")
                way_back = way_back or value
        else:
            raise ValueError(f"a rule is text or an object like {{\"way_back\": true}}, not {rule!r}")
    return Rules(tuple(texts), taps, way_back)


def loads(text: str) -> Rules:
    """House rules from JSON text (the header). ValueError when it isn't JSON or isn't rules."""
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError) as e:
        raise ValueError(f"the ux rules aren't JSON: {e}") from None
    return parse(obj)


def load(path: str | Path) -> Rules:
    """`--ux-rules FILE`. ValueError, naming the file, when it can't be read or isn't rules."""
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        raise ValueError(f"can't read the ux rules file {path}: {getattr(e, 'strerror', None) or e}") from None
    try:
        return loads(text)
    except ValueError as e:
        raise ValueError(f"the ux rules file {path}: {e}") from None


NONE = Rules()
# The house rules of the call being run: set by the daemon for that call only (like the safety policy), so the journey
# can read them with `rules_in_force()`.
_RULES: contextvars.ContextVar[Rules] = contextvars.ContextVar("tapwright_ux_rules", default=NONE)


def rules_in_force() -> Rules:
    return _RULES.get()


# ---- the naming memory (the screens this agent has seen) ----
@dataclass(frozen=True)
class Seen:
    title: str
    labels: tuple[str, ...]  # the labels of its buttons, links and tabs


_SEEN: "OrderedDict[str, deque[Seen]]" = OrderedDict()


def title_of(items: list[ui.Element]) -> str:
    """What names a screen: its first header, else its first text in the top fifth of the list's reach."""
    for e in items:
        if e.role == "header" and e.text:
            return e.text
    texts = [e for e in items if e.role == "text" and e.text]
    return min(texts, key=lambda e: e.y).text if texts else ""


def _norm(label: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", label.casefold()).split())


def _is_back_label(label: str) -> bool:
    """A way-back label: a symbol (x, close mark, chevron, <) is compared as it is, since the punctuation strip would
    leave nothing of it; words are compared normalized."""
    return label.strip().casefold() in BACK_WORDS or _norm(label) in BACK_WORDS


def _named(items: list[ui.Element]) -> tuple[str, ...]:
    return tuple(e.text for e in items if e.role in ("button", "link", "tab") and e.text)


def note_screen(client: str, items: list[ui.Element]) -> None:
    """Remember the screen this agent has just seen (its title and button labels), for naming drift on later ones.
    The journey calls this too for every screen it reads."""
    seen = Seen(title_of(items), _named(items))
    if not seen.labels:
        return
    memory = _SEEN.setdefault(client, deque(maxlen=SEEN_SCREENS))
    _SEEN.move_to_end(client)
    while len(_SEEN) > SEEN_CLIENTS:
        _SEEN.popitem(last=False)
    if not memory or memory[-1] != seen:
        memory.append(seen)


def screens_seen(client: str) -> list[Seen]:
    """The screens this agent has seen, oldest first (for the journey)."""
    return list(_SEEN.get(client, ()))


def forget(client: str) -> None:
    _SEEN.pop(client, None)


# ---- the findings ----
@dataclass(frozen=True)
class Finding:
    kind: str
    element: str  # the element's line, or "the screen"
    what: str
    basis: str  # `measured`, `best practice: <rule>` or `house rule: <rule>`

    def line(self, n: int) -> str:
        return f"{n}. {self.kind}: {self.element}: {self.what} ({self.basis})"

    def to_dict(self, screen: str = "") -> dict:
        return {"kind": self.kind, "element": self.element, "what": self.what, "basis": self.basis, "screen": screen}


@dataclass
class Result:
    findings: list[Finding] = field(default_factory=list)
    choices: int = 0
    rows: int = 0  # list rows (cells), not counted as choices
    main: ui.Element | None = None


def actionable(items: list[ui.Element]) -> list[ui.Element]:
    # (a slider has no tap, toggle or type action in the list: it is counted by its role)
    return [e for e in items if e.enabled and e.role in ACTION_ROLES
            and (e.role == "slider" or any(a in ACTIONABLE for a in e.actions))]


def main_candidates(items: list[ui.Element], screen: tuple[float, float]) -> list[ui.Element]:
    """The buttons that can be a screen's main action, the largest first."""
    big = [e for e in items if e.role == "button" and e.enabled and e.type != "Key"
           and e.width >= MAIN_MIN_WIDTH * screen[0] and e.height >= MAIN_MIN_HEIGHT]
    return sorted(big, key=lambda e: e.width * e.height, reverse=True)


def _is_phone(screen: tuple[float, float]) -> bool:
    return screen[0] < screen[1] and screen[0] < REACH_PHONE_MAX


def has_way_back(items: list[ui.Element], screen: tuple[float, float]) -> bool:
    """A visible way back: a back button, a close control, or a tab bar. From the list: a tab; a button named
    BackButton or labelled Back, Close, Cancel, Done...; or a labelled button at the top left (where a navigation bar
    keeps its back button)."""
    for e in items:
        if e.role == "tab":
            return True
        if e.role == "button" and e.enabled:
            if e.identifier == "BackButton" or e.name == "BackButton" or _is_back_label(e.text):
                return True
            if e.center[1] < screen[1] * 0.15 and e.center[0] < screen[0] * 0.25 and e.text:
                return True
    return False


def check(items: list[ui.Element], screen: tuple[float, float], client: str = "", rules: Rules = NONE,
          audit_rows: list[tuple[str, str, str]] | None = None) -> Result:
    """The findings for the screen listed in `items` (screen size in points). `audit_rows` are the audit's
    (type, element line, what) rows; of them cut-off text, contrast and target size are findings. The earlier screens
    of `client` are compared for naming; this screen is remembered after."""
    out = Result()
    found = out.findings
    act = actionable(items)
    out.rows = sum(1 for e in items if e.role == "cell" and e.enabled)
    out.choices = len(act)

    # 1. competing choices
    if out.choices > CHOICES_MAX:
        found.append(Finding("choices", "the screen", f"{out.choices} actionable elements compete for attention",
                             f"best practice: offer few choices at once (more than {CHOICES_MAX} is a lot)"))

    # 2. one clear main action
    candidates = main_candidates(items, screen)
    if candidates:
        out.main = candidates[0]
    if len(candidates) >= 2:
        top, second = candidates[0], candidates[1]
        if top.width * top.height < MAIN_CLEAR * second.width * second.height:
            found.append(Finding("main_action", top.line(),
                                 f"no one main action: {ui.clip(top.text) if top.text else 'it'} and "
                                 f"{ui.clip(second.text) if second.text else second.ref} are about the same size",
                                 "measured"))

    # 3. main actions out of thumb reach
    if candidates and _is_phone(screen):
        biggest = candidates[0].width * candidates[0].height
        for e in candidates:
            if e.width * e.height < 0.6 * biggest:
                break
            if e.center[1] < screen[1] * REACH_HARD:
                found.append(Finding("reach", e.line(), f"the main action is in the top third of the screen "
                                     f"(y {round(e.center[1])} of {round(screen[1])}), hard to reach one-handed",
                                     "best practice: keep the main action in the lower part of the screen, "
                                     "where a thumb reaches"))

    # 4. icon-only buttons with no label
    for e in items:
        if e.role == "button" and e.enabled and e.type != "Key" and not e.text:
            found.append(Finding("icon_only", e.line(), "an icon-only button with no label, so VoiceOver can't "
                                 "name it and a tester can't tell what it does", "measured"))

    # 5. cut-off text, contrast and target size (from the audit)
    names = {"clipped_text": "cut_off_text", "contrast": "contrast", "hit_region": "target_size"}
    for kind, line, what in audit_rows or ():
        if kind in names:
            found.append(Finding(names[kind], line or "the screen", what, "measured"))

    # 6. the same action named differently on screens seen before
    reported: set[tuple[str, str]] = set()
    here = Seen(title_of(items), _named(items))  # (a look may have remembered this very screen already)
    before = [s for s in screens_seen(client) if s != here]
    for e in items:
        if e.role not in ("button", "link", "tab") or not e.text:
            continue
        group = _GROUP_OF.get(_norm(e.text))
        if group is None:
            continue
        for earlier in reversed(before):
            for label in earlier.labels:
                other = _GROUP_OF.get(_norm(label))
                if other == group and _norm(label) != _norm(e.text) and (e.text, label) not in reported:
                    reported.add((e.text, label))
                    where = f' on "{ui.clip(earlier.title)[1:-1]}"' if earlier.title else " on an earlier screen"
                    found.append(Finding("naming", e.line(),
                                         f"this action is {ui.clip(e.text)} here but {ui.clip(label)}{where}",
                                         "measured"))

    # 7. an error or empty state that doesn't say what to do next
    next_steps = [e for e in act if e.role in ("button", "link")]
    for e in items:
        if e.role not in ("text", "header") or not e.text or not STATE_WORDS.search(e.text):
            continue
        near = [b for b in next_steps
                if -NEXT_STEP_REACH / 3 <= b.center[1] - (e.y + e.height) <= NEXT_STEP_REACH]
        if not near:
            found.append(Finding("dead_end", e.line(), "an error or empty state with no button or link near it "
                                 "that says what to do next",
                                 "best practice: an error or empty state offers a next step"))

    # house rule: way_back
    if rules.way_back and not has_way_back(items, screen):
        found.append(Finding("way_back", "the screen", "no visible back button, close control or tab bar",
                             "house rule: way_back"))

    note_screen(client, items)
    return out


def report(screen: str, result: Result, rules: Rules = NONE) -> str:
    """The answer (AGENT-TOOLS §17.3): `UX check of <screen>: <n> findings.`, one line per finding, then the numbers
    that were measured and the house rules written as text, for the agent to judge."""
    found = result.findings
    head = f"UX check of {screen}: {len(found)} finding{'s' if len(found) != 1 else ''}."
    lines = [head, *(f.line(n) for n, f in enumerate(found[:MAX_FINDINGS], 1))]
    if len(found) > MAX_FINDINGS:
        lines.append(f"(+{len(found) - MAX_FINDINGS} more findings.)")
    main = f"main action: {result.main.line(place=False)}" if result.main is not None else "main action: none found"
    rows = f" (and {result.rows} list rows)" if result.rows else ""
    lines.append(f"Measured: {result.choices} actionable elements{rows}; {main}.")
    if rules.texts:
        lines.append("House rules to judge from the screen (they are yours to decide, say so): "
                     + "; ".join(f'"{t}"' for t in rules.texts) + ".")
    return "\n".join(lines)


def tell_journey(client: str, result: Result, screen: str) -> None:
    """Add the findings to the journey that is running, if there is one: `journey.add_finding(client, finding)` with
    the finding as a dict (kind, element, what, basis, screen). Without the journey module, or without a journey
    running, nothing happens."""
    try:
        from . import journey  # type: ignore[attr-defined]
        add = journey.add_finding
    except (ImportError, AttributeError):
        return
    for f in result.findings:
        try:
            add(client, f.to_dict(screen))
        except Exception:  # a journey that can't take a finding never fails the check
            return

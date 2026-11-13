"""Journeys (docs/AGENT-TOOLS.md §17.1, §17.4): the state of one agent's journey, the numbers the tool measures from the
calls it handles for that agent, and the findings the agent files. The daemon (Daemon._journey_tool and run_tool)
feeds it; the report is written by journey_report.py. No phone and no network here.

What is measured, and how (all from the calls this agent makes while the journey runs):
- taps: successful `tap`, `long_press`, `toggle` and `alert` calls. typing: `type_text` calls and their characters.
  scrolls: `scroll` and `swipe`. errors: calls that were refused.
- screens: the distinct screens entered. A screen is the set of (role, text) pairs of an answer's element list; an
  answer that lists the screen in full is a new screen when it shares fewer than half its elements with the current
  one, and a return to an earlier screen (80 % alike) is not a new one.
- wrong turns: a screen entered and left by `back` without acting on it (a tap, long press, typing, toggle or alert).
- dead ends: a wrong turn where the agent had scrolled, or read the screen twice, looking for a way on first.
- hesitations: two or more reads (`look`, `audit`, `compare_screen`) in a row without an action between them; each
  such run counts once.
- time: from start to stop, with the seconds spent in `wait` and `wait_for` (waits for loading) shown apart.
- taps to goal: the taps so far when the journey is stopped as reached.
"""
from __future__ import annotations

import datetime
import re
import weakref
from dataclasses import dataclass, field
from pathlib import Path

from . import store
from .control import Refused

CONDITION_KEYS = ("language", "region", "text_size", "network")
ALLOWABLE = ("open_url", "app_command")  # shortcuts a journey may allow (app_command is Tapline's; Tapwright has none)
SEVERITIES = ("high", "medium", "low")
STRESSES = ("background", "network", "double_tap", "rotate", "relaunch")
TAPS = frozenset({"tap", "long_press", "toggle", "alert"})
ACTING = TAPS | {"type_text"}
SCROLLS = frozenset({"scroll", "swipe"})
READS = frozenset({"look", "audit", "compare_screen"})
WAITS = frozenset({"wait", "wait_for"})
MOVES = frozenset({"back", "open_app", "close_app", "open_url", "press_button", "rotate"})  # actions that aren't a tap
SAME_SCREEN = 0.8  # share of elements alike for a return to a screen already seen
NEW_SCREEN = 0.5  # fewer alike than this: another screen
MAX_FRAMES = 200
MAX_FINDINGS = 100

# The journeys that are running, by agent (the daemon puts them here and takes them out): what `add_finding` finds them by.
running: "weakref.WeakValueDictionary[str, Journey]" = weakref.WeakValueDictionary()

# How serious a ux_check finding is, by its kind (a house rule is always high): an agent can't use it or is lost (high),
# something that makes the screen harder to use (medium), a matter of taste or polish (low).
SEVERITY_OF_KIND = {"icon_only": "high", "dead_end": "high", "way_back": "high", "main_action": "medium",
                    "reach": "medium", "clipped_text": "medium", "contrast": "medium", "hit_region": "medium",
                    "choices": "low", "naming": "low"}
_PLACE = re.compile(r" @(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?) (\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)")

_ELEMENT = re.compile(r'^[eo]\d+ (\w+)(?: ("(?:[^"\\]|\\.)*"|\(icon only\)(?: \(VoiceOver: "(?:[^"\\]|\\.)*"\))?))?')


def reports_dir() -> Path:
    """Where reports are written: ~/.tapwright/reports. (A function, so tests can move it.)"""
    return store.HOME / "reports"


def slug(text: str, limit: int = 40) -> str:
    """The words of `text` as a folder name part: lower case letters and digits, joined by dashes."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    out = "-".join(words)[:limit].strip("-")
    return out or "journey"


def signature(text: str) -> frozenset[tuple[str, str]]:
    """The (role, text) pairs of the element lines in an answer."""
    out = set()
    for line in text.splitlines():
        m = _ELEMENT.match(line)
        if m:
            out.add((m.group(1), m.group(2) or ""))
    return frozenset(out)


def title_of(text: str) -> str:
    """A name for a screen from its answer: the first header, else the first text, else the app's name."""
    plain = first_text = ""
    for line in text.splitlines():
        m = _ELEMENT.match(line)
        if m and m.group(2) and m.group(2).startswith('"'):
            if m.group(1) == "header":
                return m.group(2).strip('"')
            if m.group(1) == "text":
                plain = plain or m.group(2).strip('"')
            first_text = first_text or m.group(2).strip('"')
    if plain or first_text:
        return plain or first_text
    app = re.search(r"^App: (.+?) \(", text, re.M)
    return app.group(1) if app else "screen"


def alike(a: frozenset, b: frozenset) -> float:
    """The share of the smaller set that is in both."""
    if not a or not b:
        return 1.0 if a == b else 0.0
    return len(a & b) / min(len(a), len(b))


@dataclass
class Screen:
    index: int
    signature: frozenset
    title: str
    acted: bool = False  # something was done here at some visit (the report's "nothing done here")
    acted_here: bool = False  # ... in the current visit; with scrolls and reads, counted per visit
    scrolls: int = 0
    reads: int = 0
    frame: int | None = None  # the index in Journey.frames of the first picture of it


@dataclass
class Frame:
    """A picture taken after a settled action: the file, the touch point (points) and the screen's size (points)."""
    path: Path
    caption: str
    point: tuple[float, float] | None
    screen_size: tuple[float, float] | None
    screen: int  # the Screen.index it shows
    at: float = 0.0  # seconds from the start


@dataclass
class Journey:
    goal: str
    persona: str | None
    conditions: dict
    allow: tuple[str, ...]
    eyes: str  # "user" or "agent"
    started: float  # the daemon's clock at the start
    started_wall: datetime.datetime
    folder: Path
    app: str | None = None  # the app under test (bundle id) when it began, for stress moments and undoing conditions
    eyes_switches: list[tuple[int, str]] = field(default_factory=list)  # (call number, eyes switched to), in order
    calls: int = 0
    taps: int = 0
    typing: int = 0
    typed_chars: int = 0
    scrolls: int = 0
    errors: int = 0
    wrong_turns: int = 0
    dead_ends: int = 0
    hesitations: int = 0
    waiting: float = 0.0
    reads_in_a_row: int = 0
    screens: list[Screen] = field(default_factory=list)
    current: Screen | None = None
    frames: list[Frame] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    stress: list[str] = field(default_factory=list)  # the answer lines of the stress moments, in order
    shortcuts: list[str] = field(default_factory=list)  # allowed shortcuts used (`open_url https://…`)
    notes: list[str] = field(default_factory=list)  # things the report says (undone conditions, a network ended)
    extra_sections: list[tuple[str, list[str]]] = field(default_factory=list)  # (heading, markdown lines) added by ux_check
    network_set: bool = False  # the journey put the phone's network in a condition (to undo at stop)
    launched_with: dict = field(default_factory=dict)  # the language/region/text size the app was launched with
    left_out: int = 0  # pictures not kept because another app was in front (H1)
    over_cap: int = 0  # pictures not kept because the report holds at most MAX_FRAMES

    @property
    def users_eyes(self) -> bool:
        return self.eyes == "user"

    # ---- the numbers ----
    def note_call(self, name: str, args: dict, text: str, seconds: float, error: str | None) -> bool:
        """Count one call this agent made. Returns whether the screen changed (a new screen was entered)."""
        if name == "journey":
            return False
        self.calls += 1
        if error is not None:
            self.errors += 1
            return False
        if name in WAITS:
            self.waiting += max(0.0, seconds)
        if name in READS:
            self.reads_in_a_row += 1
            if self.reads_in_a_row == 2:
                self.hesitations += 1
            if self.current is not None:
                self.current.reads += 1
        elif name not in WAITS:
            self.reads_in_a_row = 0
        left = self.current
        if name in TAPS:
            self.taps += 1
        elif name == "type_text":
            self.typing += 1
            self.typed_chars += len(args.get("text") or "")
        elif name in SCROLLS:
            self.scrolls += 1
            if left is not None:
                left.scrolls += 1
        if name in ACTING and left is not None:
            left.acted = left.acted_here = True
        entered = self.see(text)
        if name == "back" and entered and left is not None and not left.acted_here:
            self.wrong_turns += 1
            if left.scrolls or left.reads >= 2:
                self.dead_ends += 1
        return entered

    def see(self, text: str) -> bool:
        """Take the screen an answer lists, if it lists one in full. True when it is another screen than the current."""
        if not any(line.startswith("Screen ") for line in text.splitlines()):
            return False
        sig = signature(text)
        if self.current is not None and alike(self.current.signature, sig) >= NEW_SCREEN:
            self.current.signature = sig
            return False
        for s in self.screens:
            if alike(s.signature, sig) >= SAME_SCREEN:
                self.current = s
                s.acted_here, s.scrolls, s.reads = False, 0, 0  # a new visit: wrong turns and dead ends count per visit
                return True
        self.current = Screen(len(self.screens) + 1, sig, title_of(text))
        self.screens.append(self.current)
        return True

    def elapsed(self, now: float) -> float:
        return max(0.0, now - self.started)

    # ---- findings (also the API for ux_check: journey.add_finding) ----
    def add_finding(self, finding: dict) -> dict:
        """File a finding: `{severity, what, basis, screen?, rect?}`. `screen` is a screen's number or its name (the
        current one by default); `rect` is `(x, y, width, height)` in points to mark on its picture."""
        clean = check_finding(finding)
        if len(self.findings) >= MAX_FINDINGS:
            raise Refused("bad_params", f"a journey keeps at most {MAX_FINDINGS} findings")
        if "screen" not in clean and self.current is not None:
            clean["screen_index"] = self.current.index
            clean["screen"] = f"{self.current.index}. {self.current.title}"
        else:
            clean["screen_index"] = self.screen_number(clean.get("screen"))
        clean["frame"] = self.last_frame_of(clean.get("screen_index"))
        self.findings.append(clean)
        return clean

    def screen_number(self, named: object) -> int | None:
        if named is None:
            return None
        text = str(named).strip()
        m = re.match(r"^(?:screen |step )?(\d+)\b", text, re.I)
        if m and 1 <= int(m.group(1)) <= len(self.screens):
            return int(m.group(1))
        for s in self.screens:
            if text.lower() and (text.lower() in s.title.lower() or (s.title and s.title.lower() in text.lower())):
                return s.index
        return None

    def last_frame_of(self, screen: int | None) -> int | None:
        """The index of the newest picture of a screen (the last frame when `screen` is None or has none)."""
        if screen is not None:
            for i in range(len(self.frames) - 1, -1, -1):
                if self.frames[i].screen == screen:
                    return i
        return len(self.frames) - 1 if self.frames else None

    def counts(self) -> dict[str, int]:
        return {sev: sum(1 for f in self.findings if f["severity"] == sev) for sev in SEVERITIES}


def add_finding(client: str, finding: dict) -> bool:
    """File a `ux_check` finding in the journey `client` has running (docs/AGENT-TOOLS.md §17.3, §17.5). `finding` is
    `{kind, element, what, basis, screen}`; its severity comes from its kind (a house rule is high) and the box round
    the element from the `@x,y WxH` of its line. It goes on the journey's current screen (ux_check reads that one).
    The same finding twice (the agent checked the screen again) is filed once. True when it was filed."""
    jr = running.get(client)
    if jr is None or not isinstance(finding, dict):
        return False
    kind, element = str(finding.get("kind") or ""), str(finding.get("element") or "")
    what, basis = str(finding.get("what") or "").strip(), str(finding.get("basis") or "").strip()
    if not what or not basis:
        return False
    here = jr.current.index if jr.current is not None else None
    if any((f.get("kind"), f.get("element"), f["what"], f.get("screen_index")) == (kind or None, element or None, what, here)
           for f in jr.findings):
        return False
    severity = "high" if basis.startswith("house rule") else SEVERITY_OF_KIND.get(kind, "medium")
    raw: dict = {"severity": severity, "what": what, "basis": basis}
    if kind:
        raw["kind"] = kind
    if element:
        raw["element"] = element
        if (m := _PLACE.search(element)) is not None:
            raw["rect"] = tuple(float(v) for v in m.groups())
    jr.add_finding(raw)
    return True


def finding_text(f: dict) -> str:
    """What a finding says in the report: `kind: element: what` for a ux_check one, else `what`."""
    return ": ".join(p for p in (f.get("kind"), f.get("element"), f["what"]) if p)


def check_finding(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise Refused("bad_params", 'finding must be an object, like {"severity": "high", "what": "...", "basis": "..."}')
    unknown = sorted(set(raw) - {"severity", "screen", "what", "basis", "rect", "kind", "element"})
    if unknown:
        raise Refused("bad_params", f"finding doesn't take {', '.join(unknown)}; it takes severity, screen, what, basis")
    if raw.get("severity") not in SEVERITIES:
        raise Refused("bad_params", f"finding severity must be high, medium or low, not {raw.get('severity')!r}")
    for key in ("what", "basis"):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            raise Refused("bad_params", f"finding {key} must be a non-empty string")
    screen = raw.get("screen")
    if screen is not None and (isinstance(screen, bool) or not isinstance(screen, (str, int))):
        raise Refused("bad_params", "finding screen must be a screen's number or name")
    out = {"severity": raw["severity"], "what": raw["what"].strip(), "basis": raw["basis"].strip()}
    for key in ("kind", "element"):  # from ux_check: what sort of finding, and the element's line
        if isinstance(raw.get(key), str) and raw[key].strip():
            out[key] = raw[key].strip()
    if raw.get("screen") is not None:
        out["screen"] = str(raw["screen"]).strip()
    rect = raw.get("rect")
    if rect is not None:
        if (not isinstance(rect, (list, tuple)) or len(rect) != 4
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in rect)):
            raise Refused("bad_params", "finding rect must be [x, y, width, height] in points")
        out["rect"] = tuple(float(v) for v in rect)
    return out


HIDDEN_KEYS = ("id", "label")


def _names_hidden(target: object) -> bool:
    return isinstance(target, dict) and any(target.get(k) is not None for k in HIDDEN_KEYS)


def hidden(name: str, args: dict) -> bool:
    """Does this call name an element by id or label, which a user can't see (§17.1)? A `run` is looked through step by
    step, `expect` included."""
    if _names_hidden(args.get("target")) or (name in ("performance", "filmstrip") and _names_hidden(args.get("tap"))):
        return True
    if name == "wait_for" and (args.get("id") is not None or args.get("label") is not None):
        return True
    if name == "run" and isinstance(args.get("steps"), list):
        return any(isinstance(s, dict) and (hidden(str(s.get("tool")), s) or _names_hidden(s.get("expect")))
                   for s in args["steps"])
    return False


# ---- the arguments ----
def check_start(args: dict) -> dict:
    """The start arguments, cleaned: goal, persona, conditions, allow, eyes. `bad_params` for anything else."""
    goal = args.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise Refused("bad_params", 'journey start needs a goal, like "log breakfast by scanning its barcode"')
    persona = args.get("persona")
    if persona is not None and (not isinstance(persona, str) or not persona.strip()):
        raise Refused("bad_params", "persona must be text, like \"a first-timer\" or the name of an example persona")
    allow = args.get("allow") or []
    if not isinstance(allow, list) or any(a not in ALLOWABLE for a in allow):
        raise Refused("bad_params", f"allow is a list of shortcuts the journey may use: {', '.join(ALLOWABLE)}")
    eyes = args.get("eyes", "user")
    if eyes not in ("user", "agent"):
        raise Refused("bad_params", f'eyes must be "user" (the default) or "agent", not {eyes!r}')
    conditions = args.get("conditions") or {}
    if not isinstance(conditions, dict):
        raise Refused("bad_params", "conditions is an object with any of language, region, text_size, network")
    unknown = sorted(set(conditions) - set(CONDITION_KEYS))
    if unknown:
        raise Refused("bad_params", f"conditions doesn't take {', '.join(unknown)}; it takes {', '.join(CONDITION_KEYS)}")
    return {"goal": goal.strip(), "persona": persona.strip() if persona else None, "allow": tuple(dict.fromkeys(allow)),
            "eyes": eyes, "conditions": {k: v for k, v in conditions.items() if v is not None}}


def check_survives(raw: object) -> dict | None:
    if raw is None:
        return None
    if (not isinstance(raw, dict) or len(raw) != 1 or next(iter(raw)) not in ("text", "goal_step")):
        raise Refused("bad_params", 'survives is what must still be there after: {"text": "2 eggs"} or {"goal_step": 3}')
    key, value = next(iter(raw.items()))
    if key == "text" and (not isinstance(value, str) or not value.strip()):
        raise Refused("bad_params", "survives text must be a non-empty string")
    if key == "goal_step" and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
        raise Refused("bad_params", "survives goal_step is a screen's number on the journey's path, 1 or more")
    return {key: value}


# ---- words ----
def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def duration(seconds: float) -> str:
    """`34 s`, `1 min 52 s`, `2 min`."""
    total = max(0, round(seconds))
    if total < 60:
        return f"{total} s"
    minutes, rest = divmod(total, 60)
    return f"{minutes} min" + (f" {rest} s" if rest else "")


def summary_line(j: Journey, reached: bool, elapsed: float) -> str:
    who = f" as {j.persona}" if j.persona else ""
    numbers = (f"{duration(elapsed)} ({duration(j.waiting)} waiting), {plural(j.wrong_turns, 'wrong turn')}, "
               f"{plural(j.hesitations, 'hesitation')}.")
    head = f'Journey "{j.goal}"{who}: '
    if reached:
        return f"{head}reached in {plural(j.taps, 'tap')}, {numbers}"
    return f"{head}not reached after {plural(j.taps, 'tap')}, {numbers}"


def findings_line(j: Journey, folder: Path | str) -> str:
    counts = j.counts()
    said = ", ".join(f"{n} {sev}" for sev, n in counts.items() if n)
    return f"Findings: {said or 'none'}. Report: {folder}"

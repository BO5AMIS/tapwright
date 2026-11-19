"""Routes saved by themselves (AGENT-TOOLS 1.17 §30, Tapwright's own): the way a session went from the app's start to a
place, kept as a route of the app's map folder (`routes/<name>.json`, `"auto": true`) with no `record` call, and
replayed in one call with `drive {"route": "<name>"}`.

Navigation only. A chain starts at the place the app shows after `open_app`; each tap, back or open_url that leaves the
place for another adds a step, made from the move the map keeps for its road (roads.move_of: an id, else a label or
text, never a title, a list row's text or an id that carries content). Scrolls made at a place before leaving it go
with the next step (by direction, to an end, or to a target by its id). Typing, a toggle, any other action, a touch the
policy held, a tap that stayed on its place, a move the map can't keep (a list row with no id, a point) break the chain
from where it happened: no place reached through that point is saved. Going back to a place already on the chain cuts
the loop, so the way kept is the shortest the session found.

A place keeps one route. A new one replaces it when it is shorter, or when the old one has failed since it last worked;
a route whose replay fails twice in a row is dropped (counted by the daemon's drive). A route a person recorded is never
touched: a place one of them already reaches from the start gets none of its own.

Pure functions over plain data: no phone, no files.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import links, ui

NAV_TOOLS = ("tap", "back", "open_url")
NEUTRAL = ("look", "wait", "wait_for", "apps", "logs", "map", "audit", "ux_check", "session_report",
           "compare_screen", "viewer", "crash", "performance", "filmstrip")  # change nothing a route would need
MAX_PENDING = 5  # scrolls kept at one place before its next step; more break the chain
MAX_STEPS = 20  # a longer way isn't saved
FAILS = 2  # a route whose replay fails this many times in a row is dropped
NAME_MAX = 50
FAIL_CODES = ("off_route", "not_found", "not_actionable", "expect_failed", "stale")  # a replay that failed, not one held up
MAX_AUTO = 50  # (review 1.9.0 L3) routes that saved themselves, per app: past it the least used and oldest go
RUN_NAVIGATION = ("tap", "back", "open_url", "scroll", "look", "wait", "wait_for")  # a run of only these types nothing


@dataclass
class Chain:
    """One session's way from the app's start: `places[0]` is the start, `steps[i]` the route steps from `places[i]` to
    `places[i + 1]`. `broken_at`: the index of the place where a step that isn't navigation was made."""
    app: str
    places: list[str] = field(default_factory=list)
    steps: list[list[dict]] = field(default_factory=list)
    pending: list[dict] = field(default_factory=list)
    broken_at: int | None = None
    # (1.9.0 round 2) the ids each place's tree held (on and off screen) while the way there was clean and nothing had
    # been typed there; a search typed at a place: (its index, the ids it held before the typing); the index of the
    # first step made from a search (`derived`: a route through it is dropped on its first failed replay)
    ids_at: dict = field(default_factory=dict)
    search: tuple | None = None
    derived_at: int | None = None

    @property
    def derived(self) -> bool:
        return self.derived_at is not None

    def saw(self, ids: set) -> None:
        """What the place the chain is at holds now, read before any typing there."""
        at = len(self.places) - 1
        if at < 0 or self.broken_at is not None or (self.search is not None and self.search[0] == at):
            return
        self.ids_at.setdefault(at, set()).update(ids)

    def searched(self) -> None:
        """Text was typed into a search field at the place the chain is at: the way breaks here as for any typing, but a
        tap on a result that was in this place's list before the typing may still be kept (`after_search`)."""
        at = len(self.places) - 1
        if at >= 0 and self.broken_at is None and self.ids_at.get(at):
            # (review Opus 3 M4) only with a read of the place from before the typing: none, no route from the search
            self.search = (at, frozenset(self.ids_at[at]))
        self.taint()

    def after_search(self, dst: str, ident: object) -> list[dict] | None:
        """A tap on `ident` right after a search at the place the chain is at, which arrived at `dst`: kept as "scroll to
        the result, tap it" when the result was in the place's list before the typing (ids only: never the typed text),
        else None (a server-side search: the way stays broken). Returns the whole way to save, as `moved` does."""
        at = len(self.places) - 1
        if (self.search is None or self.search[0] != at or self.broken_at != at or dst == self.here or dst in self.places
                or not isinstance(ident, str) or ident not in self.search[1]):
            return None
        self.broken_at, self.pending, self.search = None, [], None
        self.steps.append([{"tool": "scroll", "to": "target", "target": {"id": ident}},
                           {"tool": "tap", "target": {"id": ident}}])
        self.places.append(dst)
        if self.derived_at is None:
            self.derived_at = len(self.steps) - 1
        way = [s for part in self.steps for s in part]
        return way if 0 < len(way) <= MAX_STEPS else None

    @property
    def start(self) -> str | None:
        return self.places[0] if self.places else None

    @property
    def here(self) -> str | None:
        return self.places[-1] if self.places else None

    def begin(self, place: str) -> None:
        if not self.places:
            self.places = [place]

    def taint(self) -> None:
        """A step that isn't navigation was made at the place the chain is at."""
        at = len(self.places) - 1
        self.broken_at = at if self.broken_at is None else min(self.broken_at, at)
        self.pending = []

    def scrolled(self, step: dict | None) -> None:
        """A scroll at the place the chain is at: kept for the next step (None: one that can't be kept, left out)."""
        if step is None:
            self.taint()  # (CodeRabbit, PR #22) a scroll a route can't replay may have brought what the next tap needs
            return
        if len(self.pending) >= MAX_PENDING:
            self.taint()
            return
        self.pending.append(step)

    def moved(self, dst: str, step: dict | None) -> list[dict] | None:
        """A move from `here` that arrived at `dst`. `step`: the route step it is, None for one that can't be saved (it
        breaks the chain). Returns the whole way from the start to `dst` when it is a clean one to save, else None."""
        if dst == self.here:
            if step is None or step.get("tool") != "back":
                self.taint()  # a tap that stayed: a form's value, a filter, a selection (review 1.9.0 L1)
            return None
        if step is None:
            self.taint()
        if dst in self.places:  # back on the chain: the loop is cut, and what broke it there may be left behind
            k = self.places.index(dst)
            self.places, self.steps, self.pending = self.places[:k + 1], self.steps[:k], []
            if self.broken_at is not None and self.broken_at > k:
                self.broken_at = None
            if self.derived_at is not None and self.derived_at >= k:
                self.derived_at = None  # (the step made from a search is cut off with the loop)
            if self.search is not None and self.search[0] > k:
                self.search = None
            self.ids_at = {i: v for i, v in self.ids_at.items() if i <= k}
            return None
        self.steps.append([*self.pending, step] if step is not None else [])
        self.places.append(dst)
        self.pending = []
        if self.broken_at is not None:
            return None
        way = [s for part in self.steps for s in part]
        return way if 0 < len(way) <= MAX_STEPS else None


def step_of(move: dict | None) -> dict | None:
    """The route step a road's move makes, as `run` takes it; None when it can't be replayed or isn't navigation."""
    if not isinstance(move, dict):
        return None
    tool = move.get("tool")
    if tool == "back":
        return {"tool": "back"}
    if tool == "open_url":
        link = move.get("link")
        return {"tool": "open_url", "url": link} if isinstance(link, str) and link and not needs_value(link) else None
    if tool != "tap":
        return None
    target = move.get("target")
    if not isinstance(target, dict) or target.get("row") or "x" in target:
        return None  # a row with no id (its words are content) or a point (it breaks with the layout)
    kept = {k: v for k, v in target.items() if k in ("id", "label", "text", "index")}
    if not any(k in kept for k in ("id", "label", "text")):
        return None
    return {"tool": "tap", "target": kept}


def only_navigation(steps: object, strict: bool = False) -> bool:
    """Are these `run` steps (or a saved route's) plain navigation (review 1.9.0 H1, H2)? Never typing, a toggle or any
    other tool. `strict` (a route carried into one of the tool's): each step must also be one a route of its own would
    keep (`step_of`, `scroll_step`), with nothing conditional."""
    if not isinstance(steps, list):
        return False
    for step in steps:
        if not isinstance(step, dict) or step.get("tool") not in RUN_NAVIGATION:
            return False
        if not strict:
            continue
        if "if" in step or step.get("optional"):
            return False
        tool = step["tool"]
        if tool == "tap" and step_of({"tool": "tap", "target": step.get("target")}) is None:
            return False
        if tool == "open_url" and (not isinstance(step.get("url"), str) or needs_value(step["url"])):
            return False
        if tool == "scroll" and scroll_step(step) is None:
            return False
    return True


def needs_value(link: object) -> bool:
    """(review 1.9.0, Opus 2 H1) Does this link need a value the agent must give: a map pattern's `[param]` segment (the
    form links.py writes and fills), or a route's `${variable}`? The one test every "needs a link's value" check uses."""
    if not isinstance(link, str):
        return False
    return bool(links.variables(link)) or "${" in link or ("{" in link and "}" in link)


def given_nothing(route: dict) -> bool:
    """Does this route replay with nothing the agent must give (§30.2): no variable, no account, no link value?"""
    if route.get("variables"):
        return False
    steps = route.get("steps")
    return isinstance(steps, list) and not any(
        isinstance(s, dict) and s.get("tool") == "open_url" and needs_value(s.get("url")) for s in steps)


def known_ok(route: dict, held_word=None) -> bool:
    """(review 1.9.0, Opus 2 H2) May `Known:` (or the known-way hint) offer this route as a screen one call away? Only when
    it replays with nothing given and its steps are strictly navigation (`only_navigation(strict=True)`: no nested drive,
    no typing, no vault, account or one-time value, no toggle), it isn't draftsOnly, and no step aims at a word the skip
    floor holds (`held_word(text)`: Delete, Send, Buy...). A person's route that does more stays drivable by name."""
    if not given_nothing(route) or route.get("draftsOnly") is True:
        return False
    steps = route.get("steps")
    if not only_navigation(steps, strict=True):
        return False
    text = json_text(steps)
    if any(f"${{{kind}." in text for kind in ("vault", "otp", "account")):
        return False
    if held_word is not None:
        for step in steps:
            target = step.get("target") if isinstance(step, dict) else None
            if isinstance(target, dict) and any(held_word(v) for k, v in target.items()
                                                if k in ("id", "label", "text") and isinstance(v, str)):
                return False
    return True


def json_text(value: object) -> str:
    import json
    return json.dumps(value, ensure_ascii=False)


def over_cap(autos: list[dict], keep: int) -> list[str]:
    """The names to drop so that at most `keep` of these routes (that saved themselves) stay: the least used, then the
    least recently reached."""
    if len(autos) <= keep:
        return []

    def rank(r: dict) -> tuple:
        used = r.get("used")
        reached = r.get("reached")
        return (used if isinstance(used, int) and not isinstance(used, bool) else 0,
                reached if isinstance(reached, str) else "")
    ordered = sorted(autos, key=rank)
    return [r["name"] for r in ordered[:len(autos) - keep]]


def scroll_step(args: dict) -> dict | None:
    """A scroll as a route keeps it: by direction and amount, to an end, or to a target by its id (structural and no
    test account's value). None for one aimed by words or a ref (left out: a tap scrolls to a target in the tree)."""
    target = args.get("target")
    to = args.get("to")
    step: dict = {"tool": "scroll"}
    if target is not None:
        ident = getattr(target, "id", None) if not isinstance(target, dict) else target.get("id")
        if (not isinstance(ident, str) or not ui.structural_id(ident) or ui.private(ident)
                or (getattr(target, "index", None) if not isinstance(target, dict) else target.get("index")) is not None):
            return None
        if to == "target":
            return {"tool": "scroll", "to": "target", "target": {"id": ident}}
        step["target"] = {"id": ident}  # (the list that holds it is the one scrolled)
    if to in ("start", "end"):
        step["to"] = to
    elif to is not None:
        return None
    # (CodeRabbit, PR #22) the direction and amount given are kept (a horizontal list scrolled to its end); a scroll with
    # neither direction nor end scrolls down, as the tool does
    direction = args.get("direction")
    if direction in ("up", "down", "left", "right"):
        step["direction"] = direction
    elif direction is not None:
        return None
    elif "to" not in step:
        step["direction"] = "down"
    amount = args.get("amount")
    if isinstance(amount, (int, float)) and not isinstance(amount, bool):
        step["amount"] = amount
    return step


def clean(text: str) -> str:
    """Words as a route's name: lower-case ASCII letters, digits and `_` (empty when nothing is left)."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:NAME_MAX].strip("_")


def base_name(data: dict, end: str, last: dict) -> str:
    """What a route to `end` is called (§30.2): the name given with `map {"name"}`, else the id of the move that reaches
    the place (the developer's, not content), else the place's own `place_<n>`. Never a label, a text or a title: they are
    the screen's words, and can be private."""
    place = data.get("places", {}).get(end, {})
    if place.get("named"):
        return end
    target = last.get("target") if isinstance(last, dict) else None
    ident = target.get("id") if isinstance(target, dict) else None
    if isinstance(ident, str) and ui.structural_id(ident) and not ui.private(ident) and (name := clean(ident)):
        return name
    # (CodeRabbit, PR #22) the place's own name only when it is a numbered one: an older map may hold a name a title gave it
    return end if re.fullmatch(r"place_\d+", end) else "place"


@dataclass(frozen=True)
class Held:
    """A saved route as the decision needs it."""
    name: str
    start: str | None
    end: str | None
    steps: int
    auto: bool
    failed: int
    variables: bool
    key: str | None = None  # (1.9.0 round 2) target_key of its last step; None: any (a route read without its steps)


def target_key(way: list) -> str:
    """(1.9.0 round 2) What a route's last step aims at: the tool's own routes are kept one per target, not one per place
    (every destination's detail screen is one place, and a route via Lisbon is not one via Kyoto)."""
    last = way[-1] if way else None
    if not isinstance(last, dict):
        return ""
    target = last.get("target")
    if last.get("tool") == "tap" and isinstance(target, dict) and isinstance(target.get("id"), str):
        return "id:" + target["id"] + (f"#{target['index']}" if target.get("index") is not None else "")
    return json_text(last)


def decide(held: list[Held], start: str, end: str, way: list[dict], wanted: str,
           taken: set[str]) -> str | None:
    """The name to save `way` under, or None when it isn't saved: a person's route already reaches `end` from `start`,
    or this place's own route is no longer and still works. `wanted`: the name it would take; `taken`: every route name
    the folder has (a twin in another case counts)."""
    key = target_key(way)
    if any(not h.auto and h.start == start and h.end == end and not h.variables and h.key in (None, key) for h in held):
        return None
    # (review 1.9.0 L3) one route of the tool's own per place and target (1.9.0 round 2), whatever start it was reached from
    mine = next((h for h in held if h.auto and h.end == end and h.key in (None, key)), None)
    if mine is not None:
        return mine.name if (len(way) < mine.steps or mine.failed > 0) else None
    low = {t.lower() for t in taken}
    name, n = wanted, 2
    while name.lower() in low:
        suffix = f"_{n}"
        name, n = wanted[:NAME_MAX - len(suffix)] + suffix, n + 1
    return name


def known(held: list[dict], here: str | None, reachable, held_word=None) -> list[str]:
    """The names for §30.2's `Known:` line: saved routes with an end, that replay in one call with no value to give
    (`variables` empty), whose start `reachable(start)` says the app can get to from `here`, and that don't end where the
    app is; the most used first, else the most recently reached, then by name. `held`: route files as dicts with `name`
    added."""
    out = []
    for route in held:
        end, start = route.get("end"), route.get("start")
        if not isinstance(end, str) or not isinstance(start, str) or end == here or not known_ok(route, held_word):
            continue
        if not reachable(start):
            continue
        used = route.get("used")
        reached = route.get("reached")
        out.append((-(used if isinstance(used, int) and not isinstance(used, bool) else 0),
                    "\U0010ffff" if not isinstance(reached, str) else _negated(reached), route["name"]))
    return [name for *_, name in sorted(out)]


WAY_PARTS = 6  # (1.9.0 round 2) the most parts of a way the Known: line shows; a longer one keeps its first and last


def way_text(steps: object) -> str | None:
    """(1.9.0 round 2) A route's way in ids only, for the Known: line: `scroll to dest-card-lisbon, tap`, `tap open-list,
    back`. A target that isn't an id a developer wrote is shown as its tool alone (a label or a text is the screen's
    words); a link as `open link`. None when there are no steps."""
    if not isinstance(steps, list) or not steps:
        return None

    def ident(step: dict) -> str | None:
        target = step.get("target")
        value = target.get("id") if isinstance(target, dict) else None
        return value if isinstance(value, str) and ui.structural_id(value) and not ui.private(value) else None
    parts: list[str] = []
    for n, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        tool = step.get("tool")
        if tool == "tap":
            before = steps[n - 1] if n > 0 and isinstance(steps[n - 1], dict) else {}
            if (ident(step) is not None and before.get("tool") == "scroll" and before.get("to") == "target"
                    and ident(before) == ident(step) and parts):
                parts[-1] += ", tap"  # (scroll to X, then tap X: said once)
                continue
            parts.append(f"tap {ident(step)}" if ident(step) else "tap")
        elif tool == "scroll":
            if step.get("to") == "target":
                parts.append(f"scroll to {ident(step)}" if ident(step) else "scroll")
            elif step.get("to") in ("start", "end"):
                parts.append(f"scroll to {step['to']}")
            else:
                parts.append(f"scroll {step.get('direction') or 'down'}")
        elif tool == "open_url":
            parts.append("open link")
        elif isinstance(tool, str):
            parts.append(tool)
    if len(parts) > WAY_PARTS:
        parts = [*parts[:2], f"+{len(parts) - WAY_PARTS + 1} more", *parts[-(WAY_PARTS - 3):]]
    return ", ".join(parts) if parts else None


def _negated(stamp: str) -> str:
    """A UTC stamp (`2026-11-19T12:00:00Z`) that sorts newest first."""
    return "".join(chr(0x10FFFF - ord(c)) if c.isdigit() else c for c in stamp)

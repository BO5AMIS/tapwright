"""`explore`'s search (AGENT-TOOLS §25): a deterministic, breadth first search over the frontier, the (place, action) pairs
nobody has tried on this build (§28). It needs no model. This module is the engine only: it reads the screen, touches, goes
back and relaunches through a narrow `Phone` (the daemon's adapter is part 2b's; tests drive a fake), saves what it finds
to the map through a `MapPort`, and makes findings with findings.py.

What it does, in the order of §25.4: the actions of a place, top to bottom then left to right then by signature; `tap`, a
`back`, and a `scroll` for what lies below (up to 5 times); in a list the first two rows only; a place at `depth` is saved
and not expanded; with `focus` only places below it are expanded; an action tried on this build is not tried again.
After each move it reads the screen, the app's error log since the move, and whether the app is still there. Then it
goes back to the place it left: by `back`, by a proven road, by a link, then by a relaunch (`open_app`).

What it never does (§25.5): it touches nothing the skip floor holds (explorefloor.py, checked afresh at every touch, on the
element the touch would really land on); it asks no person (a touch the dry run says needs an OK or is blocked is skipped); it
looks at no grant; it never makes a long press or a swipe; it never allows a permission prompt (it answers "Don't Allow",
"Not Now" or Cancel); it stays inside ONE app: a move after which another app is in front is a `dead_end` finding and it
comes back with `open_app`, and nothing is touched while another app is in front.

Not here (part 2b): the MCP tool and the command line, typing and `fill` (§25.6), submitting and flipping toggles (§25.7:
until then a control that may submit is skipped as `may_submit` and a toggle is left untried), the report (§25.10) and
coverage (§25.11). The hooks it leaves for them are named in `Explorer`: `on_new_place`, `on_toggle`.
"""
import heapq
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Protocol

from . import explored, explorefloor, findings, mapfile, places, policy, roads, ui
from .control import Refused

MINUTES, STEPS, DEPTH = 10.0, 500, 8  # §25.2's defaults
RANGES = {"minutes": (1, 60), "steps": (1, 5000), "depth": (1, 20)}
ROWS_TRIED = 2  # §25.4 item 3
SCROLLS = 5  # §25.4 item 2
SLOW_SECONDS = 3.0  # §25.10: `slow` is more than this to settle
RESETS_IN_A_ROW = 3  # §25.8
PROGRESS_EVERY = 10  # §25.2
ALERT_TRIES = 3
BACK_SIG = "role:back@c0r0"  # the signature of the `back` move: the native Back button's own label is a title, never a name
STOPS = ("budget_minutes", "budget_steps", "frontier_empty", "crash", "paused", "busy", "locked", "cant_get_back", "app_closed")
TOGGLE_ROLES = ("switch", "radio", "checkbox")
WAITING_CODES = ("paused", "busy", "locked")  # a refusal that ends the run (§25.9)
RETRY_CODES = ("stale", "timeout")  # a refusal that says nothing about the element: not recorded
MOVE_TOOLS = ("tap", "back")  # the moves a way is driven by: never a long_press, a swipe or a toggle (§25.4)


_CODE = re.compile(r"^[a-z_]{1,40}$")


def _code(moved: "Moved") -> str:
    """How a refused touch is recorded (§28.1 `code`): the word after the refusal's code (`covered`), else the code itself."""
    for word in (moved.detail.strip().strip(":").lower(), (moved.code or "").lower()):
        if _CODE.match(word):
            return word
    return "refused"


class PhoneStop(Exception):
    """A person's pause, a busy phone or a locked one ends the run between steps (§25.9): `reason` is paused, busy or locked."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Halt(Exception):
    """The run is over: `reason` is one of STOPS."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Moved:
    """What a move answered. `code` is the refusal's code (not_found, ambiguous, not_actionable, not_allowed, stale, timeout,
    not_answering, paused, busy, locked...), `detail` the word after it (`covered`, `off_screen`, `needs_confirmation`...)."""
    code: str | None = None
    detail: str = ""
    settled: float = 0.0


@dataclass(frozen=True)
class Check:
    """The dry run's say (§11.4) on a touch: `ok`, `needs_person` (a person's OK is needed) or `blocked`."""
    kind: str = "ok"
    word: str | None = None


@dataclass(frozen=True)
class CrashFacts:
    reason: str
    frames: tuple[str, ...] = ()


@dataclass(frozen=True)
class Watch:
    """What the app did since a mark: its error-level log lines, and a crash."""
    errors: tuple[str, ...] = ()
    crash: CrashFacts | None = None


class Phone(Protocol):
    """The explorer's whole view of the daemon. Every move is an ordinary action: the daemon's checks, refusals and action
    log hold. A pause, a busy phone or a locked one is `PhoneStop` (from `gate` or a move) or a `Moved` with that code."""
    screen: tuple[float, float]

    async def read(self) -> ui.Tree: ...
    async def tap(self, target: ui.Target) -> Moved: ...
    async def back(self) -> Moved: ...
    async def scroll(self) -> Moved: ...
    async def scroll_to_start(self) -> Moved: ...
    async def open_app(self) -> Moved: ...
    async def open_url(self, link: str) -> Moved: ...
    async def alert(self, button: str) -> Moved: ...
    async def check(self, target: ui.Target) -> Check: ...
    def mark(self) -> int: ...
    async def watch(self, mark: int) -> Watch: ...
    def waited(self) -> float: ...
    async def gate(self) -> None: ...


class MapPort(Protocol):
    """The app's map in memory (mapkeeper.Maps, or a test's own). `changed` says the explored record was edited in place."""
    @property
    def data(self) -> dict: ...
    def observe(self, tree: ui.Tree, arrived: bool) -> "Seen | None": ...
    def record(self, src: str, dst: str, move: dict, names: dict, ms: int, build: str | None,
               held: frozenset[str]) -> object: ...
    def changed(self) -> None: ...


class Seen(Protocol):
    name: str
    new: bool


class MapsPort:
    """`mapkeeper.Maps` as a MapPort, for one app and one folder."""

    def __init__(self, maps, root: str | None, app: str) -> None:
        self.maps, self.root, self.app = maps, root, app

    @property
    def data(self) -> dict:
        return self.maps.data(self.root, self.app)

    def observe(self, tree: ui.Tree, arrived: bool) -> "Seen | None":
        return self.maps.observe(self.root, self.app, tree, arrived)

    def record(self, src: str, dst: str, move: dict, names: dict, ms: int, build: str | None, held: frozenset[str]) -> object:
        return self.maps.record(self.root, self.app, src, dst, move, names, ms, build, held)

    def changed(self) -> None:
        self.maps.changed(self.root, self.app)


@dataclass(frozen=True)
class Budget:
    minutes: float = MINUTES
    steps: int = STEPS
    depth: int = DEPTH

    def check(self) -> "Budget":
        """ValueError (the caller's `bad_params`) for a value out of §25.2's range."""
        for key, (low, high) in RANGES.items():
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                raise ValueError(f"{key} must be a number from {low} to {high}, not {value!r}")
        if not isinstance(self.steps, int) or not isinstance(self.depth, int):
            raise ValueError("steps and depth are whole numbers")
        return self


@dataclass(frozen=True)
class Identity:
    """What a finding says about the app, the tool and the phone (§27.1)."""
    app: str
    app_version: str
    build: str
    device: findings.Device
    tool_version: str
    tool: str = "tapwright"
    scrub: Callable[[str], str] = lambda s: s
    test_app: bool = False


@dataclass(frozen=True)
class Skipped:
    place: str
    sig: str
    why: str
    word: str | None = None


@dataclass
class Result:
    """What a run did, for part 2b's answer and report (§25.2, §25.10)."""
    stopped: str
    start_place: str | None
    left_at: str | None
    places: list[str]  # every place seen, in the order first seen
    expanded: list[str]  # the places it expanded, in order
    moves: list[tuple[str, str]]  # (place, signature) of every touch, in order
    steps: int
    seconds: float  # the run's time, without time spent waiting for a person
    findings: tuple[findings.Finding, ...]
    skipped: list[Skipped]
    notes: list[str]
    resets: list[dict]
    deferred: list[tuple[str, str, str]]  # (place, signature, kind): toggles left for §25.7 (part 2b)
    frontier_left: int
    prompts: int  # permission prompts and alerts it answered


@dataclass(frozen=True)
class Action:
    """One thing to try on a place. kind: `tap`, `row`, `toggle` or `back`."""
    sig: str
    kind: str
    element: ui.Element | None
    step: dict | None  # the move as a run step names it (id, else label, else text); None when it has no name
    list_sig: str | None = None


@dataclass(frozen=True)
class Look:
    tree: ui.Tree
    place: str | None  # None: nothing to recognise it by, another app is in front, or an alert can't be answered
    new: bool = False
    foreign: bool = False
    sheet: bool = False  # a system sheet that spends or shares is open (the share sheet): it is not a place


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    sig: str | None  # a hop made in this run: found by its signature on the screen
    move: dict | None  # a saved road's move
    proven: bool


class Explorer:
    """One run. `await Explorer(...).run()` gives a Result; it never raises for what the app or the phone does (a stop is a
    Result with its reason)."""

    def __init__(self, phone: Phone, maps: MapPort, rules: policy.Policy, identity: Identity, *, budget: Budget = Budget(),
                 start_place: str | None = None, focus: str | None = None, continue_after_crash: bool = False,
                 clock: Callable[[], float] = time.monotonic, progress: Callable[[str], None] | None = None,
                 skip: tuple[str, ...] | None = None) -> None:
        self.phone, self.map, self.who = phone, maps, identity
        self.budget = budget.check()
        self.want_start, self.focus, self.keep_going = start_place, focus, continue_after_crash
        self.clock, self.progress = clock, progress
        self.floor = explorefloor.Floor(rules, identity.app, skip)
        self.build = identity.build
        self.steps = 0
        self.t0 = 0.0
        self.waited0 = 0.0
        self.start: str | None = None
        self.seen: dict[str, None] = {}
        self.depth: dict[str, int] = {}
        self.parent: dict[str, str | None] = {}
        self.path: dict[str, list[dict]] = {}
        self.hops: dict[tuple[str, str], str] = {}  # (src, dst) -> the signature that made the road, in this run
        self.heap: list[tuple[int, int, str]] = []
        self.tie = 0
        self.expanded: list[str] = []
        self.done_places: set[str] = set()
        self.moves: list[tuple[str, str]] = []
        self.found: tuple[findings.Finding, ...] = ()
        self.skipped: dict[tuple[str, str], Skipped] = {}
        self.notes: list[str] = []
        self.resets: list[dict] = []
        self.deferred: list[tuple[str, str, str]] = []
        self.prompts = 0
        self.resets_in_row = 0
        self.at: str | None = None  # the place the app was last seen at
        self._announced: set[str] = set()
        self._listed: dict[str, set[str]] = {}  # the signatures already put on each place's frontier in this run

    # ---- hooks for part 2b (all do nothing here) ----

    async def on_new_place(self, place: str, tree: ui.Tree) -> None:
        """A place seen for the first time in this run (§25.10: the audit and ux checks that make `a11y` and `ux` findings)."""

    async def on_toggle(self, place: str, action: Action, tree: ui.Tree) -> bool:
        """A toggle on `place` (§25.7: flip, read, flip back). True when it was handled; here none is, and it stays untried."""
        return False

    # ---- the run ----

    @property
    def data(self) -> dict:
        return self.map.data

    def elapsed(self) -> float:
        """The run's seconds, without the time spent waiting for a person (§25.9)."""
        return self.clock() - self.t0 - (self.phone.waited() - self.waited0)

    async def run(self) -> Result:
        self.t0, self.waited0 = self.clock(), self.phone.waited()
        reason = "frontier_empty"
        try:
            await self._begin()
            await self._search()
        except Halt as stop:
            reason = stop.reason
        except PhoneStop as stop:
            reason = stop.reason
        return self._result(reason)

    def _result(self, reason: str) -> Result:
        left = [s for p in self.data.get("places", {}) for s in explored.frontier(self.data, p, self.build)]
        return Result(reason, self.start, self.at, list(self.seen), list(self.expanded), list(self.moves), self.steps,
                      self.elapsed(), self.found, list(self.skipped.values()), self.notes, self.resets, self.deferred,
                      len(left), self.prompts)

    async def _begin(self) -> None:
        look = await self._look(False)
        if look.foreign:
            await self._open()
            look = await self._look(False)
            if look.foreign:
                raise Halt("app_closed")
        if look.place is None:
            self.notes.append("the screen has nothing to recognise it by; the explorer can't start from it")
            raise Halt("frontier_empty")
        self.start = look.place
        self._discover(look.place, None, 0, [])
        if self.want_start is not None and self.want_start != look.place:
            if self.want_start not in self.data.get("places", {}) or not await self._reach(self.want_start):
                self.notes.append("it could not drive to the place to start at")
                raise Halt("cant_get_back")
            self.start = self.want_start
            self.depth = {self.want_start: 0}
            self.parent = {self.want_start: None}
            self.path = {self.want_start: []}
            self.heap = []
            self._push(self.want_start, 0)
        self._seed()

    def _seed(self) -> None:
        """Places an earlier run or a person's driving found, in the order the map's roads reach them (a breadth first search
        from the start place): the explorer expands those that still have untried actions."""
        if self.start is None:
            return
        queue = deque([self.start])
        while queue:
            here = queue.popleft()
            for edge in self._edges(here):
                if edge.dst not in self.depth:
                    self._discover(edge.dst, here, self.depth[here] + 1, self.path.get(here, []))
                    queue.append(edge.dst)

    def _push(self, place: str, depth: int) -> None:
        self.tie += 1
        heapq.heappush(self.heap, (depth, self.tie, place))

    def _discover(self, place: str, parent: str | None, depth: int, path: list[dict], step: dict | None = None) -> None:
        """A place the search can reach: its depth (the moves from the start place by the shortest road found), where it was
        first reached from, and the steps that reach it from the start (kept only when every hop has a name)."""
        if place not in self.depth or depth < self.depth[place]:
            self.depth[place], self.parent[place] = depth, parent
            if parent is None:
                self.path[place] = []
            elif step is not None and parent in self.path:
                self.path[place] = [*path, step]
            else:
                self.path.pop(place, None)  # findings there start at the place itself
            self._push(place, depth)

    async def _search(self) -> None:
        while self.heap:
            depth, _, place = heapq.heappop(self.heap)
            if place in self.done_places or self.depth.get(place, depth) < depth:
                continue
            self.done_places.add(place)
            if depth >= self.budget.depth or not self._below(place) or place not in self.data.get("places", {}):
                continue
            if explored.is_explored(self.data, place, self.build):
                continue
            if not await self._reach(place) and not await self._recover(place):
                self.notes.append("a place could not be reached again; it is left for a later run")
                continue
            self.expanded.append(place)
            await self._expand(place)

    def _below(self, place: str | None) -> bool:
        """With `focus`: is the place the focus or first reached from it or from a place below it (§25.4 item 5)?"""
        if self.focus is None:
            return True
        seen: set[str] = set()
        while place is not None and place not in seen:
            if place == self.focus:
                return True
            seen.add(place)
            place = self.parent.get(place)
        return False

    # ---- budget, steps, progress ----

    def _spend(self) -> None:
        """One move is about to be made: stops the run, before it, when the budget is used up (§25.9)."""
        if self.elapsed() >= self.budget.minutes * 60:
            raise Halt("budget_minutes")
        if self.steps >= self.budget.steps:
            raise Halt("budget_steps")
        self.steps += 1
        if self.progress is not None and self.steps % PROGRESS_EVERY == 0:
            self.progress(f"explored {len(self.seen)} places, {len(self.found)} findings")

    async def _moved(self, moved: Moved) -> Moved:
        """The answer to a move: a pause, a busy phone or a locked one ends the run here (§25.9)."""
        if moved.code in WAITING_CODES:
            raise Halt(moved.code)
        return moved

    # ---- reading the screen ----

    def _foreign(self, tree: ui.Tree) -> bool:
        return tree.app is None or tree.app[1] != self.who.app

    async def _look(self, arrived: bool) -> Look:
        """Read the screen, answer an alert (a permission prompt is never allowed), and say what place it is."""
        tree = await self.phone.read()
        tries = 0
        while tree.alert is not None and tries < ALERT_TRIES:
            button = explorefloor.safe_button(tree.alert, self.floor)
            if button is None:
                self.notes.append("an alert it may not answer was on the screen")
                return Look(tree, None)
            self._spend()
            await self._moved(await self.phone.alert(button))
            self.prompts += 1
            tries += 1
            tree = await self.phone.read()
        if tree.alert is not None:
            return Look(tree, None)
        if self._foreign(tree):
            return Look(tree, None, foreign=True)
        if explorefloor.system_sheet(tree):
            return Look(tree, None, sheet=True)
        seen = self.map.observe(tree, arrived)
        if seen is None:
            return Look(tree, None)
        self.at = seen.name
        self.seen.setdefault(seen.name)
        if seen.name not in self._announced:  # first seen in this run: the hook of part 2b's audit and ux checks
            self._announced.add(seen.name)
            await self.on_new_place(seen.name, tree)
        return Look(tree, seen.name, seen.new)

    # ---- the actions of a place (§25.3, §25.4) ----

    def _actions(self, tree: ui.Tree, place: str, back: bool) -> list[Action]:
        items = [e for e in tree.items if not e.in_alert]
        screen_w, screen_h = self.phone.screen
        cells = [e for e in items if e.role == "cell"]

        def inside_row(e: ui.Element) -> bool:
            return any(c is not e and explorefloor.inside(e, c) for c in cells)

        def base(e: ui.Element, kind: str) -> tuple[str | None, dict | None]:
            at = explored.bucket(e.x, e.y, e.width, e.height, screen_w, screen_h)
            sig = explored.signature(element_id=e.identifier or None, label=e.label or None, text=e.value or None,
                                     role=e.role, position=at, scrub=self.who.scrub)
            move = roads.move_of("tap", None, e, items, {}) if kind != "row" else None
            return sig, move["target"] if move and "target" in move and "x" not in move["target"] else None

        picked: list[tuple[ui.Element, str, str | None, dict | None]] = []
        for e in items:
            toggles = e.role in TOGGLE_ROLES or "toggle" in e.actions
            if not toggles and ("tap" not in e.actions or e.role in ("input", "scroll", "cell")):
                continue
            if inside_row(e) or ui.is_back(e, tree.back) or not e.enabled:
                continue
            sig, target = base(e, "toggle" if toggles else "tap")
            if sig is not None:
                picked.append((e, "toggle" if toggles else "tap", sig, target))
        out: list[Action] = []
        ordered = sorted(picked, key=lambda p: (p[0].y, p[0].x, p[2]))
        counted: dict[str, int] = {}
        for _, _, sig, _ in ordered:
            counted[sig] = counted.get(sig, 0) + 1
        seen_n: dict[str, int] = {}
        for e, kind, sig, target in ordered:
            if counted[sig] > 1:  # the same signature on several elements ends `#<index>`, from 0, in reading order (§25.3)
                n = seen_n.get(sig, 0)
                seen_n[sig] = n + 1
                sig = f"{sig}#{n}"
            out.append(Action(sig, kind, e, {"tool": "tap", "target": target} if target else None))
        out += self._rows(tree, items, cells, place)
        out.sort(key=lambda a: (a.element.y, a.element.x, a.sig) if a.element is not None else (float("inf"), 0.0, a.sig))
        if back:
            out.append(Action(BACK_SIG, "back", None, {"tool": "back"}))
        return out

    def _rows(self, tree: ui.Tree, items: list[ui.Element], cells: list[ui.Element], place: str) -> list[Action]:
        """The first two rows of each list (§25.4 item 3): named by the list and the row's number, never by its text."""
        screen_w, screen_h = self.phone.screen
        lists = [e for e in items if e.role == "scroll"]
        groups: dict[int, list[ui.Element]] = {}
        for cell in cells:
            holders = [(i, s) for i, s in enumerate(lists) if explorefloor.inside(cell, s)]
            key = min(holders, key=lambda h: lists[h[0]].width * lists[h[0]].height)[0] if holders else -1
            groups.setdefault(key, []).append(cell)
        out: list[Action] = []
        for key in sorted(groups):
            rows = sorted(groups[key], key=lambda c: (c.y, c.x))
            if key >= 0:
                s = lists[key]
                list_sig = explored.signature(element_id=s.identifier or None, label=s.label or None, text=None, role="scroll",
                                              position=explored.bucket(s.x, s.y, s.width, s.height, screen_w, screen_h),
                                              scrub=self.who.scrub)
            else:
                list_sig = "role:list@c0r0"
            if list_sig is None:
                continue
            if len(rows) > ROWS_TRIED and not any("rows: first" in n and f"({place})" in n for n in self.notes):
                self.notes.append(f"rows: first {ROWS_TRIED} of the list tried ({place})")
            for n, cell in enumerate(rows[:ROWS_TRIED], 1):
                out.append(Action(explored.row_signature(list_sig, n), "row", cell, {"row": True, "index": n}, list_sig))
        return out

    # ---- expanding a place ----

    async def _expand(self, place: str) -> None:
        done: set[str] = set()
        scrolls = 0
        lost = 0
        while True:
            await self.phone.gate()
            self._time()
            look = await self._look(False)
            if look.foreign or look.sheet or look.place != place:
                lost += 1
                if lost > 3 or not (await self._come_back(place) if look.foreign else await self._return_to(place)):
                    self.notes.append("the explorer lost a place it was expanding")
                    return
                continue
            tree = look.tree
            back = place != self.start and (tree.back is not None and (tree.back.point is not None or tree.back.dismiss is not None))
            actions = self._actions(tree, place, back)
            if (screen := self.floor.screen(tree)) is not None:
                for a in actions:
                    if a.kind != "back":
                        self._skip(place, a.sig, screen)
                self.notes.append("a screen of the system or of an account, privacy or security setting was saved, not expanded")
                return
            fresh = [a.sig for a in actions if a.kind != "back" and a.sig not in self._listed.setdefault(place, set())]
            if fresh and explored.mark_seen(self.data, place, fresh, mapfile.now()):
                self._listed[place].update(fresh)
                self.map.changed()
            action = next((a for a in actions if a.sig not in done), None)
            if action is None:
                if scrolls < SCROLLS and tree.below > 0 and not any(
                        self.floor.element(e) for e in tree.items if e.role == "scroll"):  # a scroll view is a recipient too
                    scrolls += 1
                    self._spend()
                    await self._moved(await self.phone.scroll())
                    continue
                break
            done.add(action.sig)
            if explored.is_tried(self.data, place, action.sig, self.build):
                continue
            if action.kind == "toggle":
                if not await self.on_toggle(place, action, tree):
                    self.deferred.append((place, action.sig, "toggle"))
                continue
            if not await self._do(place, action, tree):
                return
        if scrolls:
            self._spend()
            await self._moved(await self.phone.scroll_to_start())


    def _time(self) -> None:
        if self.elapsed() >= self.budget.minutes * 60:
            raise Halt("budget_minutes")

    def _skip(self, place: str, sig: str, why: explorefloor.Skip) -> None:
        if explored.mark_skipped(self.data, place, sig, why.why, mapfile.now(), why.word):
            self.map.changed()
        self.skipped[(place, sig)] = Skipped(place, sig, why.why, why.word)

    # ---- one action ----

    async def _do(self, place: str, action: Action, tree: ui.Tree) -> bool:
        """Try one action at `place`. False when the explorer is no longer on the place for good (a `back`)."""
        if action.kind == "back":
            return await self._back_action(place)
        element = action.element
        assert element is not None
        items = [e for e in tree.items if not e.in_alert]
        held = self.floor.touch(element, items) or self.floor.may_submit(element, items)
        if held is not None:
            self._skip(place, action.sig, held)
            return True
        target = ui.Target(ref=element.ref)
        check = await self.phone.check(target)
        if check.kind != "ok":
            self._skip(place, action.sig, explorefloor.Skip("policy", check.word))
            return True
        mark = self.phone.mark()
        titles = places.titled_places(self.data, element.label, element.value)
        self._spend()
        self.moves.append((place, action.sig))
        moved = await self._moved(await self.phone.tap(target))
        if moved.code is not None:
            await self._refused(place, action, moved)
            return True
        look = await self._look(True)
        watch = await self.phone.watch(mark)
        step = action.step
        if watch.crash is not None:
            self._mark(place, action.sig, "crash")
            self._find("crash", place, "The app keeps running.", f"the app crashed: {watch.crash.reason}", step,
                       crash=watch.crash)
            if not self.keep_going:
                raise Halt("crash")
            await self._recover_from_crash(place)
            return True
        self._errors(place, step, watch)
        if moved.settled > SLOW_SECONDS:
            self._find("slow", place, f"The screen settles within {SLOW_SECONDS:g} seconds.", f"{moved.settled:.1f} s to settle", step)
        if look.foreign or look.sheet:
            bundle = look.tree.app[1] if look.tree.app and look.foreign else "the system's sheet"
            self._mark(place, action.sig, "left")
            self._find("dead_end", place, "The move stays in the app.", f"it left the app (now {bundle})", step)
            await (self._come_back(place) if look.foreign else self._return_to(place))
            return True
        if look.place is None:
            self._mark(place, action.sig, "same")
            self.notes.append("a move reached a screen with nothing to recognise it by; it went back")
            await self._return_to(place)
            return True
        if look.place == place:
            self._mark(place, action.sig, "same")
            return True
        self._arrived(place, look, action, element, items, titles, moved)
        await self._return_to(place)
        return True

    def _arrived(self, place: str, look: Look, action: Action, element: ui.Element, items: list[ui.Element],
                 titles: frozenset[str], moved: Moved) -> None:
        """A move that left `place` for another: a road (§18.2), a record, and a place to expand."""
        assert look.place is not None
        self._mark(place, action.sig, "road", to=look.place)
        move = roads.move_of("tap", None, element, items, {})
        if move is not None:
            names = roads.names_of(element)
            self.map.record(place, look.place, move, names, int(moved.settled * 1000), self.build, titles)
            self.map.changed()
        self.hops.setdefault((place, look.place), action.sig)
        self._discover(look.place, place, self.depth.get(place, 0) + 1, self.path.get(place, []), action.step)

    def _mark(self, place: str, sig: str, result: str, to: str | None = None, code: str | None = None) -> None:
        if explored.mark_tried(self.data, place, sig, result, self.build, mapfile.now(), to=to, code=code):
            self.map.changed()

    async def _refused(self, place: str, action: Action, moved: Moved) -> None:
        """A touch the phone refused (§25.4 item 7, §28.1): `not_actionable` covered or off screen is a finding."""
        if moved.code == "not_allowed":
            self._skip(place, action.sig, explorefloor.Skip("policy"))
        elif moved.code == "not_answering":
            await self._recover_from_crash(place)
        elif moved.code not in RETRY_CODES and moved.code is not None:
            self._mark(place, action.sig, "refused", code=_code(moved))
            if moved.code == "not_actionable" and moved.detail in ("covered", "off_screen"):
                self._find("refused", place, "The touch works.", f"not_actionable: {moved.detail}", action.step)

    def _errors(self, place: str, step: dict | None, watch: Watch) -> None:
        if watch.errors:
            self._find("js_error", place, "No error is logged.", watch.errors[0], step, logs=watch.errors)

    # ---- findings ----

    def _find(self, kind: str, place: str | None, expected: str, got: str, step: dict | None, *, logs: tuple[str, ...] = (),
              crash: CrashFacts | None = None) -> None:
        path_from, path = self._path_for(place, step)
        try:
            made = findings.make(
                finding_id=findings.next_id(self.found), kind=kind, app=self.who.app, app_version=self.who.app_version,
                build=self.who.build, tool=self.who.tool, tool_version=self.who.tool_version, device=self.who.device,
                expected=expected, got=got, scrub=self.who.scrub, place=place, path_from=path_from, path=path,
                test_app=self.who.test_app, logs=logs, crash_reason=crash.reason if crash else None,
                crash_frames=crash.frames if crash else ())
        except findings.FindingError as e:
            self.notes.append(f"a {kind} finding could not be made ({e})")
            return
        self.found = findings.add(self.found, made)

    def _path_for(self, place: str | None, step: dict | None) -> tuple[str | None, list[dict]]:
        """Where a script that reaches the finding starts and the steps it takes: from the start place by the roads this run
        found; a place reached another way starts there, with the move alone."""
        if place is not None and place in self.path and self.start is not None:
            return self.start, [*self.path[place], *([step] if step else [])]
        return place, [step] if step else []

    # ---- going back, crashes, resets ----

    async def _back_action(self, place: str) -> bool:
        """`back` as an action of a place that isn't the start (§25.4 item 2): the last thing tried there."""
        sig = BACK_SIG
        if explored.is_tried(self.data, place, sig, self.build):
            return False
        mark = self.phone.mark()
        self._spend()
        self.moves.append((place, sig))
        moved = await self._moved(await self.phone.back())
        if moved.code is not None:
            self._mark(place, sig, "refused", code=_code(moved))
            return True
        look = await self._look(True)
        watch = await self.phone.watch(mark)
        if watch.crash is not None:
            self._mark(place, sig, "crash")
            self._find("crash", place, "The app keeps running.", f"the app crashed: {watch.crash.reason}", {"tool": "back"},
                       crash=watch.crash)
            if not self.keep_going:
                raise Halt("crash")
            await self._recover_from_crash(place)
            return False
        self._errors(place, {"tool": "back"}, watch)
        if look.foreign:
            self._mark(place, sig, "left")
            self._find("dead_end", place, "The move stays in the app.", "it left the app", {"tool": "back"})
            await self._open()
            return False
        if look.place is None or look.place == place:
            self._mark(place, sig, "same")
            return look.place == place
        self._mark(place, sig, "road", to=look.place)
        self.map.record(place, look.place, {"tool": "back"}, {}, int(moved.settled * 1000), self.build, frozenset())
        self.map.changed()
        self.hops.setdefault((place, look.place), sig)
        self._discover(look.place, place, self.depth.get(place, 0) + 1, self.path.get(place, []), {"tool": "back"})
        return False

    async def _open(self) -> Moved:
        self._spend()
        return await self._moved(await self.phone.open_app())

    async def _come_back(self, place: str) -> bool:
        """A move left the app: `open_app`, and drive to `place` if the app isn't there (§25.7)."""
        await self._open()
        return await self._return_to(place, relaunched=True)

    async def _recover_from_crash(self, place: str) -> None:
        """After a crash with `continue` (§25.9): reopen the app; when it can't be reopened the run ends."""
        await self._open()
        look = await self._look(False)
        if look.foreign:
            raise Halt("app_closed")
        self.resets.append({"how": "relaunch", "data_kept": True, "at_step": self.steps})
        if look.place != place:
            await self._reach(place) or await self._recover(place)

    async def _return_to(self, place: str, relaunched: bool = False) -> bool:
        """Back to the place a move left (§25.4 item 8): by `back`, by a proven road, by a link, in that order, checking
        that it is there; when it can't, a reset."""
        look = await self._look(False)
        for _ in range(2):  # a system sheet is closed by `back` (it is no place, and no app is left)
            if not look.sheet:
                break
            self._spend()
            await self._moved(await self.phone.back())
            look = await self._look(False)
        if look.place == place:
            self.resets_in_row = 0
            return True
        if look.foreign:
            await self._open()
            look = await self._look(False)
        if look.place == place:
            return True
        if not relaunched and look.place is not None and look.tree.back is not None \
                and (look.tree.back.point is not None or look.tree.back.dismiss is not None):
            was = look.place
            self._spend()
            await self._moved(await self.phone.back())
            look = await self._look(True)
            if look.place == place:
                if was != look.place:
                    self.map.record(was, place, {"tool": "back"}, {}, 0, self.build, frozenset())
                    self.map.changed()
                return True
        if look.place is not None and await self._reach(place, proven_only=True):
            return True
        link = self.data.get("places", {}).get(place, {}).get("link")
        if link and look.place is not None:
            self._spend()
            await self._moved(await self.phone.open_url(link))
            if (await self._look(True)).place == place:
                return True
        return await self._recover(place)

    async def _recover(self, place: str) -> bool:
        """The reset of §25.8: Tapwright's own is a relaunch with `open_app`, which keeps the app's data. At most 3 in a row
        that don't bring the app to a known place."""
        self._find("dead_end", place, "There is a way back.", f"no way back to {place} except a reset", None)
        await self._open()
        self.resets.append({"how": "relaunch", "data_kept": True, "at_step": self.steps})
        look = await self._look(False)
        # a reset worked when the app is at the place, or at one from which the map has a way to it
        if look.place is not None and (look.place == place or await self._reach(place)):
            self.resets_in_row = 0
            return True
        self.resets_in_row += 1
        if self.resets_in_row >= RESETS_IN_A_ROW:
            raise Halt("cant_get_back")
        return False

    # ---- ways ----

    def _edges(self, here: str, proven_only: bool = False) -> list[Edge]:
        """The ways out of a place: the hops made in this run (found by signature), then the map's roads that a tap or a back
        makes (not a long press, a swipe or a toggle, §25.4), in a fixed order."""
        out: list[Edge] = []
        for (src, dst), sig in self.hops.items():
            if src == here:
                out.append(Edge(src, dst, sig, None, True))
        for road in sorted((r for r in self.data.get("roads", []) if r.get("from") == here),
                           key=lambda r: (str(r.get("to")), roads._key(r["move"]))):
            move = road.get("move", {})
            if move.get("tool") not in MOVE_TOOLS or road.get("by", roads.TOOL) not in (roads.TOOL, "both"):
                continue
            if isinstance(move.get("target"), dict) and move["target"].get("row"):
                continue
            proven = roads.is_proven(road, self.build)
            if proven or not proven_only:
                out.append(Edge(here, road["to"], None, move, proven))
        return out

    def _way(self, src: str, dst: str, proven_only: bool) -> list[Edge] | None:
        """The way with the fewest moves from `src` to `dst`, or None. Ties go to the first found, in a fixed order."""
        queue: deque[tuple[str, list[Edge]]] = deque([(src, [])])
        seen = {src}
        while queue:
            here, way = queue.popleft()
            if here == dst:
                return way
            for edge in self._edges(here, proven_only):
                if edge.dst not in seen:
                    seen.add(edge.dst)
                    queue.append((edge.dst, [*way, edge]))
        return None

    async def _reach(self, dst: str, proven_only: bool = False) -> bool:
        """Drive to `dst` from where the app is, move by move, checking each arrival (§18.4)."""
        look = await self._look(False)
        if look.place == dst:
            return True
        if look.place is None:
            return False
        way = self._way(look.place, dst, proven_only)
        if way is None:
            return False
        for edge in way:
            if not await self._leg(edge):
                return False
        return True

    async def _leg(self, edge: Edge) -> bool:
        await self.phone.gate()
        look = await self._look(False)
        if look.place != edge.src:
            return False
        tree = look.tree
        items = [e for e in tree.items if not e.in_alert]
        if edge.sig == BACK_SIG or (edge.move is not None and edge.move.get("tool") == "back"):
            self._spend()
            await self._moved(await self.phone.back())
        else:
            element = self._aim(edge, tree, items)
            if element is None:
                return False
            if self.floor.touch(element, items) is not None or self.floor.may_submit(element, items) is not None:
                return False
            target = ui.Target(ref=element.ref)
            if (await self.phone.check(target)).kind != "ok":
                return False
            self._spend()
            moved = await self._moved(await self.phone.tap(target))
            if moved.code is not None:
                return False
        after = await self._look(True)
        return after.place == edge.dst

    def _aim(self, edge: Edge, tree: ui.Tree, items: list[ui.Element]) -> ui.Element | None:
        """The element a leg touches: the one with the hop's signature, or the one a road's target names (the first, in a
        fixed order, when it names several and has no index)."""
        if edge.sig is not None:
            found = next((a.element for a in self._actions(tree, edge.src, False) if a.sig == edge.sig), None)
            return found if found is not None and found.role != "scroll" else None
        target = (edge.move or {}).get("target")
        if not isinstance(target, dict) or target.get("row"):
            return None
        try:
            found = ui.resolve(items, ui.Target(**{k: v for k, v in target.items() if k in ("id", "label", "text", "index")}))
        except (Refused, TypeError):
            return None
        return found if isinstance(found, ui.Element) else None


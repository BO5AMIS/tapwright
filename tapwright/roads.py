"""Roads: one move that left a place and arrived at another, as it was seen (AGENT-TOOLS §18.2), and the way `drive`
would take between two places (§18.4). Pure functions over the map as plain JSON (places.empty_map), so they are
testable without a phone. The daemon decides what is a road (not a move that needed a person's OK, not typing, not a
replay, not a dry run, not a person's input: §18.2) and calls `record`.

Privacy: a road keeps the target's id, else its label, else its text, never a ref; a list row only by its id (a row
without one is `{"row": true}`, and keeps no name); no typed text, no params. A link a road follows has already been
cut to scheme and path by links.py.
"""
import copy
import heapq
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import ui

TOOL = "tapwright"  # what `by` says for a road this tool drove
SAVED_TOOLS = ("tap", "long_press", "toggle", "back", "open_url", "swipe")  # the moves that can be a road (§18.2)
TARGETED = ("tap", "long_press", "toggle")
FAILS = 2  # a road that fails this many times in a row is dropped
UNPROVEN_COST = 2  # an unproven road counts double when the way is chosen (§18.4)
MAX_INDEX = 20  # how far down a list of look-alikes an index is searched for


@dataclass(frozen=True)
class Recorded:
    """What `record` did: the road is new, and the roads (from, to) it dropped because they failed twice."""
    new: bool
    dropped: list[tuple[str, str]] = field(default_factory=list)
    gone: list[dict] = field(default_factory=list)  # those roads themselves, so the file can be told they are dropped


@dataclass(frozen=True)
class RouteEdge:
    """A saved route as a way between two places: one move per step (§18.5)."""
    name: str
    start: str
    end: str
    steps: int


@dataclass(frozen=True)
class Way:
    """The way to a place. kind: `here` (already there), `link` (a jump), `roads` (a chain of roads and routes) or
    `none`. `steps` holds road dicts and RouteEdges, in order."""
    kind: str
    start: str | None
    steps: list = field(default_factory=list)
    link: str | None = None
    cost: int = 0
    unproven: int = 0  # how many of its roads were last driven on another build of the app

    @property
    def proven(self) -> bool:
        return self.unproven == 0

    @property
    def roads(self) -> list[dict]:
        return [s for s in self.steps if isinstance(s, dict)]

    @property
    def routes(self) -> list[RouteEdge]:
        return [s for s in self.steps if isinstance(s, RouteEdge)]


# ---- what is saved ----

def _same(element: ui.Element, other: ui.Element | None) -> bool:
    return other is element or (other is not None and other.ref == element.ref and other.line() == element.line())


def _target_for(element: ui.Element, items: list[ui.Element]) -> dict | None:
    """The element as a target a later read can resolve to it again: id, else label, else text, with an `index` only
    where several elements answer to it. Checked by resolving it against `items`, the way a call would."""
    for kind, value in (("id", element.identifier), ("label", element.label), ("text", element.value or element.text)):
        if not value or (kind != "id" and element.role == "cell"):  # a row's label and text are content: its id or nothing
            continue
        target = ui.Target(**{kind: value})
        try:
            if _same(element, ui.resolve(items, target)):
                return {kind: value}
        except Exception as e:  # ambiguous: look for its index; not found: this name doesn't reach it
            if getattr(e, "code", "") != "ambiguous":
                continue
        for index in range(MAX_INDEX):
            try:
                if _same(element, ui.resolve(items, ui.Target(**{kind: value, "index": index}))):
                    return {kind: value, "index": index}
            except Exception:
                break
    return None


def names_of(element: ui.Element) -> dict:
    """Every name an element had, for repair (§18.4): `id`, `label`, `text`. A list row keeps only its id."""
    names = {}
    if element.identifier:
        names["id"] = element.identifier
    if element.role == "cell":
        return names
    if element.label:
        names["label"] = element.label
    if element.text:
        names["text"] = element.text
    return names


def move_of(tool: str, target: ui.Target | None, element: ui.Element | None, items: list[ui.Element],
            args: dict) -> dict | None:
    """The move a call makes, as a road keeps it; None when that tool is never a road (§18.2). `element`: what the
    target resolved to (None for a point with nothing under it); `items`: the list it resolved in; `args`: the call's
    own arguments (a swipe's points; an open_url's `link`, already cut to what may be kept)."""
    if tool not in SAVED_TOOLS:
        return None
    if tool == "back":
        return {"tool": "back"}
    if tool == "swipe":
        keys = ("x0", "y0", "x1", "y1")
        if any(args.get(k) is None for k in keys):
            return None
        return {"tool": "swipe", **{k: args[k] for k in keys}, **({"seconds": args["seconds"]} if "seconds" in args else {})}
    if tool == "open_url":
        link = args.get("link")
        return {"tool": "open_url", "link": link} if isinstance(link, str) and link else None
    if element is not None:
        if element.role == "cell" and not element.identifier:
            return {"tool": tool, "target": {"row": True}}  # a row's text is content: the road exists, drive can't replay it
        found = _target_for(element, items)
        if found is not None:
            return {"tool": tool, "target": found}
        if element.role == "cell":
            return {"tool": tool, "target": {"row": True}}  # its id can't tell it from the others: same placeholder
    if target is not None and target.point is not None and element is None:
        return {"tool": tool, "target": {"x": target.x, "y": target.y}}
    return None


# ---- recording ----

def _key(move: dict) -> str:
    return json.dumps(move, sort_keys=True, separators=(",", ":"))


def _epoch(stamp: object) -> float:
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except ValueError:
        return 0.0


def is_proven(road: dict, build: str | None) -> bool:
    """Was the road last driven cleanly on this build of the app? With the app's build unknown there is nothing to be
    unproven against."""
    return build is None or road.get("build") == build


def _fail(data: dict, road: dict, dropped: list[tuple[str, str]], gone: list[dict] | None = None) -> None:
    road["failed"] = int(road.get("failed", 0)) + 1
    if road["failed"] >= FAILS:
        data["roads"].remove(road)
        dropped.append((road["from"], road["to"]))
        if gone is not None:
            gone.append(road)


def record(data: dict, src: str, dst: str, move: dict, names: dict, ms: int, now: str, build: str | None,
           by: str = TOOL) -> Recorded | None:
    """Save a move that left `src` and arrived at `dst`. A road seen again is counted (`seen`, `ms`, `last`, `build`:
    driven cleanly now, so proven for this build) and its failures start over. The same move from `src` that led
    somewhere else before counts a failure against that road, and a road failing twice in a row is dropped. None when
    the move stayed on its place (it is no road)."""
    key = _key(move)
    dropped: list[tuple[str, str]] = []
    gone: list[dict] = []
    failing = [r for r in data["roads"] if r["from"] == src and _key(r["move"]) == key and r["to"] != dst]
    for other in failing:
        _fail(data, other, dropped, gone)
    if src == dst:  # no road of its own, but a saved road of this move that no longer leaves the place has failed
        return Recorded(False, dropped, gone) if failing else None
    found = next((r for r in data["roads"] if r["from"] == src and r["to"] == dst and _key(r["move"]) == key), None)
    if found is None:
        data["roads"].append({"from": src, "to": dst, "move": copy.deepcopy(move), "names": dict(names), "by": by,
                              "seen": 1, "failed": 0, "ms": ms, "last": now, "build": build})
        return Recorded(True, dropped, gone)
    found["names"] = {**found.get("names", {}), **names}
    found["seen"], found["failed"], found["ms"], found["last"], found["build"] = int(found.get("seen", 0)) + 1, 0, ms, now, build
    if found.get("by", by) != by:
        found["by"] = "both"  # each tool has driven it once (§18.2)
    return Recorded(False, dropped, gone)


def identity(road: dict) -> tuple[str, str, str]:
    """What names a road in a map: where it starts, its move, where it ends (§18.9)."""
    return road["from"], _key(road["move"]), road["to"]


def fail(data: dict, src: str, move: dict, gone: list[dict] | None = None) -> list[tuple[str, str]]:
    """A road from `src` by this move failed to be driven (its target is missing, or it led somewhere else). It is
    dropped on the second failure in a row; returns the roads dropped as (from, to)."""
    dropped: list[tuple[str, str]] = []
    key = _key(move)
    for road in [r for r in data["roads"] if r["from"] == src and _key(r["move"]) == key]:
        _fail(data, road, dropped, gone)
    return dropped


def repair(data: dict, src: str, old_move: dict, dst: str, new_move: dict, names: dict, ms: int, now: str,
           build: str | None, by: str = TOOL) -> Recorded:
    """A road whose target was missing, driven by another name that reached the same place (§18.4): the road is
    updated, after the arrival check passed. The old move's road goes, the new one takes its count, and both are
    told in `gone` so the file drops the old one too. `new` is whether the new move is a road the map didn't have."""
    key = _key(old_move)
    old = [r for r in data["roads"] if r["from"] == src and r["to"] == dst and _key(r["move"]) == key]
    for road in old:
        data["roads"].remove(road)
    made = record(data, src, dst, new_move, names, ms, now, build, by)
    if made is None:
        return Recorded(False, [], old)
    carried = sum(int(r.get("seen", 0)) for r in old)
    if carried:
        found = next(r for r in data["roads"] if r["from"] == src and r["to"] == dst and _key(r["move"]) == _key(new_move))
        found["seen"] = int(found.get("seen", 0)) + carried
    return Recorded(made.new, made.dropped, [*old, *made.gone])


# ---- the way ----

def _is_row(road: dict) -> bool:
    """A road whose move aims at a list row with no id: it is kept, but drive can't replay it, so no way goes by it."""
    target = road.get("move", {}).get("target")
    return isinstance(target, dict) and bool(target.get("row"))


def _allowed(road: dict, tool: str) -> bool:
    return road.get("by", TOOL) in (tool, "both")


def plan(data: dict, src: str | None, dst: str, build: str | None, by: str = "any", routes: list[RouteEdge] | tuple = (),
         tool: str = TOOL) -> Way:
    """`_plan` over the roads drive can replay; only when that finds no way, over the row roads too, so that the drive
    stops at the row and says it can't be replayed (a row road is kept, but no way is chosen by it while another exists)."""
    way = _plan(data, src, dst, build, by, routes, tool, rows=False)
    if way.kind == "none" and any(_is_row(r) for r in data["roads"]):
        return _plan(data, src, dst, build, by, routes, tool, rows=True)
    return way


def _plan(data: dict, src: str | None, dst: str, build: str | None, by: str, routes: list[RouteEdge] | tuple,
          tool: str, rows: bool) -> Way:
    """The way `drive` would take from `src` (None: unknown) to `dst` (§18.4). A jump when the place has a link and
    `by` allows one (`any`, `link`); else the chain of this tool's roads (and saved routes, one move per step) with the
    fewest moves, an unproven road counting double; two ways of equal cost: the one used most recently. `by="taps"`
    never jumps; `by="link"` only jumps."""
    if src == dst and dst in data["places"]:
        return Way("here", src)
    link = data["places"].get(dst, {}).get("link")
    if link and by in ("any", "link"):
        return Way("link", src, link=link, cost=1)
    if by == "link" or src is None:
        return Way("none", src)
    edges: dict[str, list[tuple[str, int, float, dict | RouteEdge, bool]]] = {}
    for road in data["roads"]:
        if _allowed(road, tool) and road.get("move", {}).get("tool") in SAVED_TOOLS and (rows or not _is_row(road)):
            proven = is_proven(road, build)
            edges.setdefault(road["from"], []).append(
                (road["to"], 1 if proven else UNPROVEN_COST, _epoch(road.get("last")), road, proven))
    for route in routes:
        edges.setdefault(route.start, []).append((route.end, route.steps, 0.0, route, True))
    best: dict[str, tuple[int, float]] = {}
    queue: list[tuple[int, float, int, str, list]] = [(0, 0.0, 0, src, [])]
    tie = 0
    while queue:
        cost, recency, _, here, steps = heapq.heappop(queue)
        if here in best:
            continue
        best[here] = (cost, recency)
        if here == dst:
            roads = [s for s in steps if isinstance(s, dict)]
            return Way("roads", src, steps, None, cost, sum(1 for r in roads if not is_proven(r, build)))
        for to, price, last, step, _ in edges.get(here, []):
            if to not in best:
                tie += 1
                heapq.heappush(queue, (cost + price, recency - last, tie, to, [*steps, step]))
    return Way("none", src)


def _word(way: Way) -> str:
    n = len(way.steps)
    taps = all(not isinstance(s, dict) or s["move"].get("tool") in TARGETED for s in way.steps)
    return f"{n} {'tap' if taps else 'move'}{'' if n == 1 else 's'}"


def say_way(way: Way, dst: str) -> str:
    """`map {"to": ...}` and a dry run's answer (§18.4): the way, and whether each road is proven."""
    if way.kind == "here":
        return f"Already at {dst}."
    if way.kind == "link":
        return f"Would drive to {dst} by link: {way.link}."
    if way.kind == "none":
        where = f" from {way.start}" if way.start else ""
        return f"No known way{where} to {dst}; drive it once (the driver), and it is remembered."
    names = [way.start or "?"]
    for step in way.steps:
        names.append(step["to"] if isinstance(step, dict) else step.end)
    chain = " > ".join(names)
    routes = way.routes
    if not routes:
        how = f"by {_word(way)}"
    elif len(way.steps) == 1:
        how = f"by route {routes[0].name} ({routes[0].steps} steps)"
    else:
        how = f"by {_word(way)} and {len(routes)} route{'s' if len(routes) != 1 else ''}"
    state = "all proven" if way.proven else f"{way.unproven} of {len(way.roads)} roads unproven for this build"
    return f"Would drive to {dst} {how}: {chain} ({state})."

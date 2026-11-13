"""One `drive` call (AGENT-TOOLS §18.4): where the app is, the way to the place, then the moves, each through the same
call a single action goes through (scope, policy, a person's OK, `force` never implied), with no answer built for the
steps in between. Between moves it waits only until the next target is on screen, steady and touchable; after each it
checks where the app is. When a road is wrong it repairs it by another name, plans again from a known place (twice at
most), or stops with `off_route` and touches nothing else.

The daemon owns the phone: a Trip reaches it through the daemon's own reads and calls, and never touches the session.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import driving, links, places, roads, routes, ui
from .control import Refused

if TYPE_CHECKING:
    from .daemon import Daemon, Scope

def _account_var():
    from .daemon import _ACCOUNT
    return _ACCOUNT


POLL = 0.1  # seconds between reads while waiting for the next target
PERSON_WAIT = 30.0  # seconds a drive waits for a person to finish before it says `busy`
SETTLE_READS = 2  # reads that show the same other known place before it counts as arrived there
NOT_HERE = "it isn't on this screen"
LED_ELSEWHERE = "it led to a screen that isn't where the road goes"
NOT_KNOWN = "it led to a screen the map doesn't know"


@dataclass
class Hop:
    """One move of the way: a road, or a jump (an `open_url` of the place's link). `road` is the saved road (None for a
    jump); `move` is what is made now, which differs from the road's once it is repaired."""
    kind: str
    src: str | None
    dst: str
    move: dict
    road: dict | None = None
    names: dict = field(default_factory=dict)
    fixed: driving.Repaired | None = None

    @property
    def jump(self) -> bool:
        return self.kind == "jump"


@dataclass(frozen=True)
class Seen:
    """What a wait found: the place the app is at (None: not a known place), whether the next target is ready, and the
    tree it read."""
    place: str | None
    ready: bool
    tree: ui.Tree | None


class Trip:
    def __init__(self, daemon: "Daemon", client_id: str, scope: "Scope | None", req: driving.Request, args: dict) -> None:
        self.d, self.client_id, self.scope, self.req, self.args = daemon, client_id, scope, req, args
        self.view = daemon.control.client(client_id)
        self.clock = daemon.control.clock
        self.root: str | None = None
        self.bundle = ""
        self.build: str | None = None
        self.moves = self.jumps = 0  # moves made by road (taps and the like), and jumps
        self.taps_only = True
        self.replans = 0
        self.chain: list[str] = []
        self.repairs: list[driving.Repaired] = []
        self.proven_again = 0
        self.notes: list[str] = []  # what the answer ends with, a line each (as `Note: `)
        self.lines: list[str] = []  # what the answer says after its first line (`Road repaired:`, `Reflex:`)
        self.reflexes = 0
        self.by = req.by
        self.started = 0.0
        self.person_at = daemon.control.person_generation
        self.eyes = False
        self.account = None  # the test account this call was given (§18.6)
        self.token = None
        self.end = req.to or ""  # the place the call ends at
        self.goal = req.to or ""  # the place the moves go to: the call's `to`, or a route's start
        self.route: dict | None = None  # the route a call drives (`route`)
        self.ran: list[tuple[str, int]] = []  # the routes run on the way: (name, steps)
        self.writes = True  # a replay reads the map and never writes it (§18.9)
        self.edges: list[roads.RouteEdge] = []
        self.bid = ""
        self.may_jump = True
        self.foreign = False  # the app in front at the last read isn't the trip's app
        self.live: dict[str, set[str]] = {}  # the titles each place's screen showed during this call (kept out of a repaired road)

    # ---- the map ----
    @property
    def data(self) -> dict:
        return self.d.maps.data(self.root, self.bundle)

    def place_of(self, tree: ui.Tree | None) -> str | None:
        """The saved place a read is; None when it is none (nothing is saved here: a drive only recognises)."""
        if tree is None or self.foreign or not places.has_structure(tree):
            return None
        read = places.read_place(tree, self.d._language.get(self.bundle))
        place = places.locate(self.data, read)[0]
        if place is not None and read.title:
            self.live.setdefault(place, set()).add(read.title)
        return place

    # ---- reading the screen ----
    async def _stop_if_needed(self) -> None:
        """A pause or a lock stops a drive between moves, with the usual errors and what was done; a person at the phone
        is waited out (as every call waits), then `busy`."""
        d = self.d
        if d.control.paused:
            raise self._stopped(Refused("paused", "paused by the user in the live view; ask them to resume"))
        if d.state == "locked":
            raise self._stopped(Refused("locked", "the phone is locked; ask the user to unlock it"))
        waited = self.clock()
        while d.control.person_active():
            if self.clock() - waited >= PERSON_WAIT:
                raise self._stopped(Refused("busy", "a person is using the phone; try again in a moment"))
            await d.control.sleep(0.25)
            if d.control.paused:
                raise self._stopped(Refused("paused", "paused by the user in the live view; ask them to resume"))

    def _stopped(self, e: Refused) -> Refused:
        """The error a move ended in, with what the drive had done (`Done: 1 move.`) at the end of its first line."""
        first, sep, rest = str(e).partition("\n")
        return Refused(e.code, f"{first} {driving.done(self.moves + self.jumps)}{sep}{rest}")

    async def _read(self) -> ui.Tree:
        d = self.d
        await self._stop_if_needed()
        front: list[str | None] = []

        async def turn() -> bool:
            more = await d._silent_read(self.view)
            tree = d.session.last_tree
            # the app in front as THIS read has it: the tree's own root, else asked now, in the same turn. `session.app`
            # is as the last look left it (Session.refresh never updates it), so a tap that opened another app doesn't show
            front.append(tree.app[1] if tree is not None and tree.app else await d.session.foreground_bundle_id())
            return more
        await d._read(turn)
        now = front[-1] if front else None
        given = await d._given(self.scope) if self.scope is not None else None
        if given is not None and now not in given:
            from .daemon import _outside
            raise self._stopped(Refused("not_allowed", _outside(now, d.session.app_names.get(now or ""), ", ".join(given.values()))))
        tree = d.session.last_tree
        if tree is None:
            raise Refused("internal", "the phone gave no screen to read; look, then try again")
        self.foreign = bool(self.bundle) and now != self.bundle  # another allowed app: the map here isn't its map
        return tree

    def ready(self, hop: Hop | None) -> bool:
        """Is the next move's target on screen, enabled, and with nothing in front? A jump and a swipe and a back need
        none; a point is always there."""
        if hop is None or hop.jump or hop.kind == "route" or hop.move["tool"] in ("back", "swipe", "open_url"):
            return True
        if self.view.alert_now is not None:
            return False
        target = (hop.move.get("target") or {})
        if "x" in target:
            return True
        try:
            _, args = driving.call_of(hop.move, {}, hop.names, self.eyes)
            found = ui.resolve(self.view.items, ui.parse_target(args["target"]))
        except driving.Unaimable:
            return True  # said when the move is made
        except Refused as e:
            return e.code == "ambiguous"  # the call says which; a miss is not ready
        return not isinstance(found, ui.Element) or found.enabled

    async def wait(self, expected: str | None, nxt: Hop | None, cap: float) -> Seen:
        """Wait for the app to be at `expected` and the next move's target to be ready (§18.4). Ends early on another
        known place the app stays at; at the cap with whatever it found."""
        deadline = self.clock() + cap
        other: tuple[str, int] | None = None
        while True:
            tree = await self._read()
            place = self.place_of(tree)
            if place is not None and place == expected:
                if self.ready(nxt):
                    return Seen(place, True, tree)
                await self.between(tree)
            elif place is not None:
                other = (place, other[1] + 1) if other and other[0] == place else (place, 1)
                if other[1] >= SETTLE_READS:
                    return Seen(place, False, tree)
                await self.between(tree)
            else:
                other = None
                await self.between(tree)  # a sheet over the page isn't the page: its dismissing reflex still has its moment
            if self.clock() >= deadline:
                return Seen(place, False, tree)
            await self.d.control.sleep(POLL)

    async def between(self, tree: ui.Tree) -> None:
        """While the next target isn't ready: a declared reflex whose moment has come (a rating prompt over the page) fires,
        and is said (§18.7). The loading wait is the wait itself: it goes on until the cap."""
        said = await self.d._fire_reflexes(self.client_id, self.scope)
        if said:
            self.lines.extend(said)
            self.reflexes += len(said)

    # ---- the way ----
    def way(self, here: str | None, by: str | None = None):
        return roads.plan(self.data, here, self.goal, self.build, by or self.by, routes=self.edges)

    def hops(self, way: roads.Way, here: str | None) -> list[Hop]:
        if way.kind == "link":
            try:
                links.fill(way.link or "", self.req.values)  # a param with no value is said before anything is touched
            except ValueError as e:
                raise Refused("bad_params", str(e)) from None
            return [Hop("jump", here, self.goal, {"tool": "open_url", "link": way.link})]
        return [Hop("road", s["from"], s["to"], s["move"], s, dict(s.get("names", {}))) if isinstance(s, dict)
                else Hop("route", s.start, s.end, {"route": s.name}) for s in way.steps]

    # ---- running ----
    async def setup(self) -> tuple[ui.Tree, str | None]:
        """Everything before the first move: the app in front and its map, what the journey allows, the route (its variables
        and account must be given), the place the moves go to, and where the app is. Nothing is touched."""
        from .daemon import SCRIPT
        d, req = self.d, self.req
        self.writes = SCRIPT.get() is None
        await d.wait_connected()
        if d.state == "locked":
            raise Refused("locked", "the phone is locked; ask the user to unlock it")
        snap = await d._read(lambda: d.session.snapshot())
        if snap.app is None:
            raise Refused("not_found", "no app is in front to drive; open_app one first")
        shown, self.bundle = snap.app
        given = await d._given(self.scope) if self.scope is not None else None
        if given is not None and self.bundle not in given:
            from .daemon import _outside
            raise Refused("not_allowed", _outside(self.bundle, shown, ", ".join(given.values())))
        if not d._may_map(self.bundle):
            raise Refused("not_allowed", "the map of this app isn't for this call")
        self.root, self.build = d._maps_root(), await d._build_of(self.bundle)
        file = d.maps.file(self.root, self.bundle)
        self.edges = file.route_edges(self.data)
        jr = d._journeys.get(self.client_id)
        if ui.USERS_EYES.get():  # a user's-eyes journey (§18.12): only roads a person could make, unless the start allows more
            self.eyes = True
            allowed = jr.allow if jr is not None else ()
            self.may_jump = "drive" in allowed
            if self.by == "link" and not self.may_jump:
                raise Refused("not_allowed", "in a journey in a user's eyes drive uses roads only (no jumps by link); "
                                             "start the journey with allow: [\"drive\"] to allow them")
            if not self.may_jump:
                self.by = "taps"
        if req.account is not None:  # (the file's problem, an unknown name or an app it isn't for come before anything is touched)
            self.account = d._loaded().account(req.account, self.bundle)
            self.token = _account_var().set(self.account)
        if req.route is not None:
            self.route = self.saved(req.route)
            routes.need(self.route, req.values, req.account)
            self.goal = self.route["start"]
        if self.goal not in self.data["places"]:
            known = ", ".join(list(self.data["places"])[:20]) or "none yet"
            what = f"the route's start, {self.goal}, isn't a place the map has" if self.route else f"no place called {self.goal}"
            raise Refused("not_found", f"{what}; the places are: {known}")
        here = self.place_of(snap.tree)
        self.chain = [here] if here else []
        self.started = self.clock()
        self.bid = f"b{next(d._batch_ids)}"
        return snap.tree, here

    def saved(self, name: str) -> dict:
        """A saved route of this app, read and checked (`not_found` when there is none)."""
        file = self.d.maps.file(self.root, self.bundle)
        found = file.read_route(self.data, name)
        if found is None:
            held = ", ".join(self.data.get("routes", {})) or "none yet"
            raise Refused("not_found", f"no saved route called {name}; the routes are: {held}")
        routes.check({**found, "name": found.get("name", name)})
        return {**found, "name": name}

    async def run(self) -> dict:
        tree, here = await self.setup()
        way = self.way(here)
        if self.req.dry_run:
            return self.dry(way, here)
        if way.kind == "none":
            await self.no_way(here)
        await self.travel([] if way.kind == "here" else self.hops(way, here), here)
        if self.route is not None:
            await self.run_route(self.route["name"], self.route)
            await self.check_end(self.route)
        return await self.arrive()

    async def reach(self) -> None:
        """Only the way to the place (a version 2 replay drives to its start first); a way that can't be driven is `off_route`
        or `not_found`, which fails the replay."""
        _, here = await self.setup()
        way = self.way(here)
        if way.kind == "none":
            await self.no_way(here)
        if way.kind != "here":
            await self.travel(self.hops(way, here), here)

    def release(self) -> None:
        """The account stops being the active one when the call ends."""
        if self.token is not None:
            _account_var().reset(self.token)
            self.token = None

    def dry(self, way: roads.Way, here: str | None) -> dict:
        from .daemon import _answer
        if self.route is not None:
            n = len(self.route["steps"])
            said = f"Would run route {self.route['name']} ({n} step{'' if n == 1 else 's'}), from {self.goal} to {self.route.get('end') or '?'}."
            if way.kind == "none":
                where = f" from {here}" if here else ""
                raise Refused("not_found", f"no known way{where} to {self.goal}; drive it once (the driver), and it is remembered.")
            return _answer([said] if way.kind == "here" else [roads.say_way(way, self.goal), said])
        if way.kind == "none":
            if here is None and self.by != "link":
                raise Refused("off_route", f"the app is at a screen the map doesn't know, so there is no way to {self.goal} "
                                           "from here. Nothing was touched.")
            where = f" from {here}" if here else ""
            raise Refused("not_found", f"no known way{where} to {self.goal}; drive it once (the driver), and it is remembered.")
        return _answer([roads.say_way(way, self.goal)])

    async def no_way(self, here: str | None) -> None:
        if here is None and self.by != "link":
            await self.off_route(0, 0, None, "the app is at a screen the map doesn't know, so there is no way to "
                                                f"{self.goal} from here", None)
        where = f" from {here}" if here else ""
        raise Refused("not_found", f"no known way{where} to {self.goal}; drive it once (the driver), and it is remembered.")

    async def travel(self, hops: list[Hop], here: str | None) -> None:
        """Make the moves of `hops`, judging each arrival, until the app is at the place."""
        pending: Hop | None = None  # the move just made, whose arrival isn't judged yet
        expected, i, moved_at = here, 0, self.clock()
        while True:
            total = len(hops)
            nxt = hops[i] if i < total else None
            if pending is None and nxt is not None and nxt.jump:  # a jump needs no start: it works from anywhere
                moved_at = self.clock()
                await self.make(nxt, i + 1, total)
                pending, expected, i = nxt, nxt.dst, i + 1
                continue
            ref = nxt or pending
            seen = await self.wait(expected, nxt, driving.cap(ref.road if ref is not None else None))
            moved = pending
            if pending is not None:
                await self.judge(pending, seen, moved_at)
                pending = None
            if seen.place is not None and seen.place == expected:
                if nxt is None:
                    return
                if not seen.ready:
                    nxt = await self.repaired(nxt, i + 1, total, seen)
                moved_at = self.clock()
                await self.make(nxt, i + 1, total)
                pending, expected, i = nxt, nxt.dst, i + 1
            elif seen.place is not None:  # arrived at another known place: plan again from there
                self.replans += 1
                again = self.way(seen.place, "taps" if self.by == "any" else self.by)
                stayed = moved is not None and moved.src == seen.place  # the move changed nothing: the same one would again
                if stayed or self.replans > driving.MAX_REPLANS or again.kind == "none":
                    last = hops[i - 1] if i else None
                    await self.off_route(i, total, last.move if last is not None else None, LED_ELSEWHERE, seen.place)
                if not self.chain or self.chain[-1] != seen.place:
                    self.chain.append(seen.place)
                if again.kind == "here":
                    return
                hops, i, expected = self.hops(again, seen.place), 0, seen.place
            else:
                await self.off_route(i, total, ref.move if ref is not None else None, NOT_KNOWN, None)

    async def repaired(self, hop: Hop, step: int, total: int, seen: Seen) -> Hop:
        """The next move's target isn't on the screen the app is at: the same element by the road's other names, else the
        closest match when it is the only one (§18.4); the repair is saved only once the arrival check passes. Else the
        road counts a failure and the drive stops with `off_route`."""
        fixed = driving.repair(hop.move, hop.names, self.view.items) if hop.road is not None else None
        if fixed is not None:
            hop.fixed, hop.move = fixed, fixed.move
            return hop
        if self.writes and hop.road is not None and hop.src is not None:  # (a replay never writes the map)
            self.dropped(self.d.maps.fail(self.root, self.bundle, hop.src, hop.road["move"]))
        await self.off_route(step, total, hop.move, NOT_HERE, seen.place)
        return hop

    async def make(self, hop: Hop, step: int, total: int) -> None:
        """Make one move: the same call a single action is (scope, policy, confirmation). A move that can't be made as it
        is saved (a row with no id) stops the drive."""
        from .daemon import _JOURNEY_OWN, BATCH, _NESTED
        d = self.d
        await self._stop_if_needed()
        if hop.kind == "route":
            await self.run_route(hop.move["route"], self.saved(hop.move["route"]))
            return
        try:
            tool, call = driving.call_of(hop.move, self.req.values, hop.names, self.eyes)
        except driving.Unaimable as e:
            await self.off_route(step, total, hop.move, str(e), hop.src)
        token, nested = BATCH.set({"id": self.bid, "step": step}), _NESTED.set(True)
        # a jump a journey allows is the tool's own move: not gated or counted as the user's. Of this task only, as a
        # journey's stress moment is (1.5.1): another call of the agent made meanwhile is still gated and counted
        own = _JOURNEY_OWN.set(True) if hop.jump and self.client_id in d._journeys else None
        try:
            answer = await d.run_tool(self.client_id, tool, {**call, "look": False}, self.scope)
            for line in answer["text"].splitlines():  # what the tool did by itself on the way: the keyboard closed another way
                if line.startswith("Reflex: "):
                    self.lines.append(line)
                    self.reflexes += 1
        except Refused as e:
            raise self._stopped(e) from None
        finally:
            if own is not None:
                _JOURNEY_OWN.reset(own)
            BATCH.reset(token)
            _NESTED.reset(nested)
        if hop.jump:
            self.jumps += 1
        else:
            self.moves += 1
            self.taps_only = self.taps_only and hop.move["tool"] in roads.TARGETED

    # ---- judging a move ----
    async def judge(self, hop: Hop, seen: Seen, since: float) -> None:
        """The arrival check of a move: the road is proven again for this build, or counted as failed (§18.4)."""
        d, ms = self.d, round((self.clock() - since) * 1000)
        if seen.place == hop.dst and (not self.chain or self.chain[-1] != hop.dst):
            self.chain.append(hop.dst)
        if hop.jump:
            if seen.place != hop.dst:
                self.by = "taps" if self.by == "any" else self.by  # a jump that missed isn't tried again
            return
        if hop.kind == "route" or not self.writes:  # a route is no road; a replay never writes the map
            return
        if seen.place == hop.dst:
            if hop.fixed is not None:
                if not d._loaded().holds_in(hop.move):  # a target that holds an account's value is no road: it stays as it was
                    made = d.maps.repair(self.root, self.bundle, hop.src or "", hop.road["move"], hop.dst, hop.move, d._loaded().clean(hop.fixed.names), ms, self.build,
                                       frozenset(self.live.get(hop.src or "", set()) | self.live.get(hop.dst, set())))
                    self.dropped([(a, b) for a, b in made.dropped])
                self.repairs.append(hop.fixed)
                self.lines.append(driving.repaired_line(hop.fixed))
            else:
                if hop.road is not None and not roads.is_proven(hop.road, self.build):
                    self.proven_again += 1
                d.maps.record(self.root, self.bundle, hop.src or "", hop.dst, hop.move, d._loaded().clean(hop.names), ms, self.build)
        elif seen.place is not None and hop.src is not None:
            made = d.maps.record(self.root, self.bundle, hop.src, seen.place, hop.move, hop.names, ms, self.build)
            self.dropped(made.dropped if made else [])
        elif hop.road is not None and hop.src is not None:
            self.dropped(d.maps.fail(self.root, self.bundle, hop.src, hop.road["move"]))

    async def run_route(self, name: str, route: dict) -> None:
        """Run a saved route's steps, with its variables filled in, through `run`'s own executor: every step passes the
        same checks as a single call, `if` and `optional` work, and a step that fails stops the drive (§18.5)."""
        steps = routes.substitute(route["steps"], self.req.values)
        try:
            answer = await self.d._batch(self.client_id, steps, self.scope, {"look": False})
        except Refused as e:
            first, sep, rest = str(e).partition("\n")
            raise Refused(e.code, f"route {name}: {first} {driving.done(self.moves + self.jumps)}{sep}{rest}") from None
        for line in answer["text"].splitlines():
            found = re.match(r"Note(?: \(step (\d+)\))?: (.*)", line)
            if found:
                self.notes.append(f"route {name}, step {found.group(1)}: {found.group(2)}" if found.group(1) else found.group(2))
        self.ran.append((name, len(steps)))

    async def check_end(self, route: dict) -> None:
        """A route says where it ends; drive never claims an arrival it didn't check (§18.5)."""
        end = route.get("end")
        if not end:
            return
        place = self.place_of(await self._read())
        if place != end:
            n = len(route["steps"])
            there = place or "a screen the map doesn't know"
            await self.off_route(n, n, None, f"the route ended at {there}, not {end}", place)
        if not self.chain or self.chain[-1] != end:
            self.chain.append(end)

    def dropped(self, gone: list[tuple[str, str]]) -> None:
        self.notes.extend(f"road dropped: {a} > {b} (it failed twice)." for a, b in gone)

    # ---- the end ----
    async def off_route(self, step: int, total: int, move: dict | None, why: str, here: str | None) -> None:
        """Stop where the screen isn't where the road leads, with nothing else touched (§4, §18.4)."""
        d = self.d
        where = f"Now at: {here} (known)." if here else "Now at: a screen the map doesn't know."
        named = f" ({driving.step_of(move)})" if move else ""
        place = f"stopped at step {step} of {total}{named}: " if step else "stopped before the first move: "
        head = f"{place}{why}. {where} {driving.done(self.moves + self.jumps)} Nothing else was touched."
        notes = [n for n in self.notes]
        try:
            answer = await d._after(self.view, head, notes, True, moves_content=None)
            text = answer["text"]
        except Exception:  # noqa: BLE001 - the reason is the answer; the screen is a courtesy
            text = "\n".join([head, *(f"Note: {n}" for n in notes)])
        raise Refused("off_route", text)

    async def arrive(self) -> dict:
        from .daemon import _answer
        d, req = self.d, self.req
        seconds = self.clock() - self.started
        tail = self.chain[:]
        how = driving.travel(self.jumps, self.moves, self.taps_only, self.reflexes, self.ran)
        end = self.route.get("end") if self.route is not None else self.goal
        if self.route is not None and not end:
            name, steps = self.ran[-1]
            first = f"Ran route {name} ({steps} step{'' if steps == 1 else 's'}) in {seconds:.1f} s."
        elif not self.moves and not self.jumps and not self.repairs and not self.ran:
            first = f"Already at {self.goal}."
        else:
            first = driving.arrived(end, how, seconds, tail)
        self.end = end or self.goal
        if self.proven_again:
            self.notes.append(f"{self.proven_again} road{'s' if self.proven_again != 1 else ''} proven again for build {self.build}.")
        view = self.view
        if req.then:
            return await self.then(first)
        if not self.args.get("look", True):
            view.unlooked += 1
            view.place = None
            answer = _answer([first, *self.lines], self.notes)
        else:
            view.unlooked = 0
            full = view.seen != d.control.generation or view.dropped or view.read_at == float("-inf")
            answer = await d._after(view, first, self.notes, full, moves_content=None)
            answer = self.placed(answer)
        return await d._add_shot(answer, view, d._shot_args(self.args, default=False, custom=False))

    def placed(self, answer: dict) -> dict:
        """The `Place:` line after the first line (and what was repaired or fired), and the app's place for the next call."""
        d, view = self.d, self.view
        name = self.end
        if view.tree is not None and self.writes:
            seen = d.maps.observe(self.root, self.bundle, view.tree, arrived=True, lang=d._language.get(self.bundle))
            if seen is not None:
                name = seen.name
                view.place, view.place_seen = (self.bundle, name), d.control.generation
        lines = answer["text"].split("\n")
        lines[1:1] = [*self.lines, f"Place: {name}."]
        return {**answer, "text": "\n".join(lines)}

    async def then(self, first: str) -> dict:
        """`then`: ordinary `run` steps after arriving, in the same call; their answer follows the arrival line (§9.3)."""
        d = self.d
        batch_args = {k: v for k, v in self.args.items() if k in ("look", "screenshot", "brief")}
        try:
            answer = await d._batch(self.client_id, self.req.then, self.scope, batch_args, [f"Note: {n}" for n in self.notes])
        except Refused as e:
            raise Refused(e.code, "\n".join([first, *self.lines, str(e)])) from None
        return {**answer, "text": "\n".join([first, *self.lines, answer["text"]])}

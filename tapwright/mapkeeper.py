"""The daemon's maps (AGENT-TOOLS §18): each app's map held in memory, learned from what the agent sees and does, and
saved to its file (mapfile.py) in the background. One map per (folder, app id); the folder is `--maps DIR`, else the
tool's home.

What is learned goes into memory at once (a place seen, a road driven) and is written by a task that merges memory into
the file under the lock, so two tools or two daemons on one folder keep each other's work. `flush` waits for those
writes (the `map` tool and the daemon's stop call it). Explicit edits (`map name`, `forget`...) write through at once.
A file that is left alone (it doesn't parse) keeps the tool driving from memory; `problem_once` says so once.
"""
import asyncio
import copy
from dataclasses import dataclass
from typing import Callable, TypeVar

from . import mapfile, places, roads, ui

T = TypeVar("T")
Key = tuple[str, str]


@dataclass(frozen=True)
class Seen:
    """A screen observed: its place's name, whether the place is new, its earlier name when a place was just renamed (never by a title since 1.13.2), and
    whether the app had a map (a place saved) before this screen."""
    name: str
    new: bool
    renamed_from: str | None
    had_map: bool


class Maps:
    def __init__(self, root: str | None = None, scrub: Callable[[str], str] | None = None) -> None:
        self.root = root
        self.scrub = scrub  # takes a test account's value out of a string (the daemon sets it): the writer's backstop  # the folder given to `tapwright serve --maps`; None: the tool's home
        self._files: dict[Key, mapfile.MapFile] = {}
        self._memory: dict[Key, dict] = {}
        self._dirty: set[Key] = set()
        self._dropped: dict[Key, list[tuple[str, str, str]]] = {}  # roads dropped in memory, to be dropped from the file too
        self._forgotten: dict[Key, set[str | None]] = {}  # reflexes forgotten by an edit, to be dropped from a save under way
        self._forgotten_places: dict[Key, dict[str, str]] = {}  # places forgotten by an edit (name: key id), likewise
        self._tasks: dict[Key, asyncio.Task] = {}
        self._told: set[Key] = set()

    # ---- what is held ----
    def _key(self, root: str | None, app: str) -> Key:
        return str(mapfile.root(root or self.root)), app

    def file(self, root: str | None, app: str) -> mapfile.MapFile:
        key = self._key(root, app)
        if key not in self._files:
            self._files[key] = mapfile.MapFile(key[0], app, lambda text: self.scrub(text) if self.scrub is not None else text)
        return self._files[key]

    def data(self, root: str | None, app: str) -> dict:
        """The app's map in memory, read from its file the first time."""
        key = self._key(root, app)
        if key not in self._memory:
            self._memory[key] = self.file(root, app).load()
        return self._memory[key]

    def has_map(self, root: str | None, app: str) -> bool:
        return bool(self.data(root, app)["places"])

    def problem(self, root: str | None, app: str) -> str | None:
        self.data(root, app)
        return self.file(root, app).problem

    def problem_once(self, root: str | None, app: str) -> str | None:
        """Why the file is left alone, the first time it is asked for (an answer says so once, not on every call)."""
        key = self._key(root, app)
        why = self.problem(root, app)
        if why is None or key in self._told:
            return None
        self._told.add(key)
        return why

    # ---- learning ----
    def observe(self, root: str | None, app: str, tree: ui.Tree, arrived: bool | None = True, lang: str | None = None,
                previous: str | None = None) -> Seen | None:
        """The screen the agent is on: the place it is (saved if new). None for a screen with nothing to recognise it by.
        `arrived`: the agent came here from somewhere else (a title counts only on an arrival); None: it did, unless this
        is the place it was at before (`previous`), as a look at the same screen again."""
        if not places.recognisable(tree):
            return None
        data = self.data(root, app)
        had_map = bool(data["places"])
        read = places.read_place(tree, lang)
        if arrived is None:
            arrived = places.locate(data, read)[0] != previous or previous is None
        visit = places.visit(data, read, mapfile.now(), arrived)
        self._changed(root, app)
        if visit.renamed_from is not None and self.file(root, app).problem is None:
            self.file(root, app).rename_in_routes(data, {visit.renamed_from: visit.name})
        return Seen(visit.name, visit.new, visit.renamed_from, had_map)

    def record(self, root: str | None, app: str, src: str, dst: str, move: dict, names: dict, ms: int,
               build: str | None, held: frozenset[str] = frozenset()) -> roads.Recorded | None:
        data = self.data(root, app)
        if self.titled(data, src, dst, move, held):
            return None  # a name that is a title is never saved: no road rather than a title in plain text
        made = roads.record(data, src, dst, move, self.untitled(data, src, dst, names, held), ms, mapfile.now(), build)
        if made is not None:
            self._dropped.setdefault(self._key(root, app), []).extend(roads.identity(r) for r in made.gone)
            self._changed(root, app)
        return made

    @staticmethod
    def titled(data: dict, src: str, dst: str, move: dict, held: frozenset[str] = frozenset()) -> bool:
        """Does the move aim by a label or text that is a title of the road's two places? (`held`: the places a title
        matched before the screen was read, places.titled_places.)"""
        target = move.get("target")
        return isinstance(target, dict) and places.is_title(data, (src, dst), *(target.get(k) for k in ("label", "text")), held=held)

    @staticmethod
    def untitled(data: dict, src: str, dst: str, names: dict, held: frozenset[str] = frozenset()) -> dict:
        """The names a road keeps, without the label or text that is a title of its two places (the id stays)."""
        return {k: v for k, v in names.items() if k == "id" or not places.is_title(data, (src, dst), v, held=held)}

    def fail(self, root: str | None, app: str, src: str, move: dict) -> list[tuple[str, str]]:
        gone: list[dict] = []
        dropped = roads.fail(self.data(root, app), src, move, gone)
        self._dropped.setdefault(self._key(root, app), []).extend(roads.identity(r) for r in gone)
        self._changed(root, app)
        return dropped

    def repair(self, root: str | None, app: str, src: str, old_move: dict, dst: str, new_move: dict, names: dict, ms: int,
               build: str | None, live: frozenset[str] = frozenset()) -> roads.Recorded:
        """A road driven by another name than the one it was saved with, and the arrival check passed (§18.4): the new
        move replaces the old one, here and (a merge would bring it back) in the file. `live`: the titles the two screens
        showed during the drive (their stored hashes may not have caught up): never kept in the road's target or names."""
        data = self.data(root, app)
        if self.titled(data, src, dst, new_move):
            return roads.Recorded(False)  # the road stays as it was; a title is not put in its place
        shown = {places._flat(t) for t in live if t}
        move = new_move
        target = new_move.get("target")
        if shown and isinstance(target, dict):
            kept = {k: v for k, v in target.items() if not (k in ("label", "text") and isinstance(v, str) and places._flat(v) in shown)}
            if kept != target:
                if "id" not in kept and "x" not in kept:
                    return roads.Recorded(False)  # nothing safe is left to aim by
                move = {**new_move, "target": kept}
        names = {k: v for k, v in names.items() if k == "id" or not (isinstance(v, str) and places._flat(v) in shown)}
        made = roads.repair(data, src, old_move, dst, move, self.untitled(data, src, dst, names), ms, mapfile.now(), build)
        self._dropped.setdefault(self._key(root, app), []).extend(roads.identity(r) for r in made.gone)
        self._changed(root, app)
        return made

    def link(self, root: str | None, app: str, name: str, url: str) -> str | None:
        """An open_url landed on place `name`: it remembers the link when the URL matches a pattern. Returns the note."""
        from . import links
        done = links.landed(self.data(root, app), url, name)
        if done.link is not None:
            self._changed(root, app)
        return done.note

    # ---- writing ----
    def _changed(self, root: str | None, app: str) -> None:
        key = self._key(root, app)
        self._dirty.add(key)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (a plain test): flush by hand
        task = self._tasks.get(key)
        if task is None or task.done():
            self._tasks[key] = asyncio.create_task(self._persist(key))

    async def _persist(self, key: Key) -> None:
        file = self._files[key]
        while key in self._dirty:
            self._dirty.discard(key)
            if file.problem is not None:
                continue  # left alone: memory is all there is
            snapshot = copy.deepcopy(self._memory[key])
            try:
                merged = await asyncio.to_thread(file.save, snapshot)
            except (mapfile.MapError, OSError) as e:
                file.problem = f"The map for {key[1]} couldn't be saved ({e}); what is learned is kept until the daemon stops."
                self._told.discard(key)
                continue
            if file.problem is not None:  # the save found the file unusable: it was left alone, memory is all there is
                continue
            failed = False
            # What an edit forgot while the save ran (or while the cleanup below ran: its tombstones are made after the
            # batch was taken) is taken out of the file; the batch is taken again until an edit has added none, and only
            # then does memory take the result, so a place an edit forgot is never put back (Codex, PR #9 cycle 2).
            while (batch := (self._dropped.pop(key, []), self._forgotten.pop(key, set()),
                             self._forgotten_places.pop(key, {})))[0] or batch[1] or batch[2]:
                try:
                    merged = await asyncio.to_thread(file.update, self._undo(*batch))
                except (mapfile.MapError, OSError) as e:
                    file.problem = f"The map for {key[1]} couldn't be saved ({e}); what is learned is kept until the daemon stops."
                    self._told.discard(key)
                    failed = True
                    break
            if failed:
                continue
            if file.problem is None:
                self._memory[key] = mapfile.merge(merged, self._memory[key])  # what changed meanwhile stays

    @staticmethod
    def _undo(gone: list[tuple[str, str, str]], forgotten: set[str | None], lost: dict[str, str]) -> Callable[[dict], None]:
        """The change that takes dropped roads, forgotten reflexes and forgotten places (with the roads and reflexes that
        touched them) out of a map the merge brought them back into."""
        def undo(d: dict) -> None:
            out = {n for n, kid in lost.items() if n in d["places"] and places.key_id(d["places"][n]["key"]) == kid}
            d.update(places={n: p for n, p in d["places"].items() if n not in out},
                     roads=[r for r in d["roads"] if roads.identity(r) not in gone
                            and not ({r.get("from"), r.get("to")} & out)],
                     reflexes=[r for r in d["reflexes"] if r.get("name") not in forgotten and r.get("at") not in out])
        return undo

    async def flush(self) -> None:
        """Wait until everything learned so far is in the files."""
        while True:
            tasks = [t for t in self._tasks.values() if not t.done()]
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    def edit(self, root: str | None, app: str, change: Callable[[dict], T]) -> T:
        """An explicit edit (`map name`, `link`, `forget`...): what is unsaved is merged into the file first, then the
        change is made on the file's map under the lock, and memory becomes the result. With a file that is left alone
        the change is made on memory."""
        key = self._key(root, app)
        data, file = self.data(root, app), self.file(root, app)
        result: list[T] = []
        renames: dict[str, str] = {}
        if file.problem is not None:
            result.append(change(data))
            return result[0]
        def edited(d: dict) -> None:
            before = {r.get("name") for r in d["reflexes"]}
            places_before = {n: places.key_id(p["key"]) for n, p in d["places"].items()}
            result.append(change(d))
            # a place under another name: one name went and one came, for the same key. Only a name that is NEW in this
            # edit can be a rename's target, so a forget (nothing new) never rewrites a route, even when another place
            # shares the forgotten one's structural signature (Codex, PR #9 cycle 2)
            vanished: dict[str, list[str]] = {}
            for n, kid in places_before.items():
                if n not in d["places"]:
                    vanished.setdefault(kid, []).append(n)
            appeared: dict[str, list[str]] = {}
            for n, p in d["places"].items():
                if n not in places_before:
                    appeared.setdefault(places.key_id(p["key"]), []).append(n)
            renames.update({gone[0]: appeared[kid][0] for kid, gone in vanished.items()
                            if len(gone) == 1 and len(appeared.get(kid, [])) == 1})
            after = {r.get("name") for r in d["reflexes"]}
            task = self._tasks.get(key)
            forgotten = self._forgotten.setdefault(key, set())
            gone_places = self._forgotten_places.setdefault(key, {})
            if task is not None and not task.done():
                forgotten |= before - after  # a save already under way would bring these back from its snapshot
                # ...and so would a place (with the roads and reflexes that touched it) from its snapshot or the merge
                gone_places.update({n: kid for n, kid in places_before.items() if n not in d["places"]})
            forgotten -= after  # (a reflex added again is wanted)
            for n in [n for n in gone_places if n in d["places"] and places.key_id(d["places"][n]["key"]) == gone_places[n]]:
                del gone_places[n]  # (a place put back is wanted)
        try:
            file.save(copy.deepcopy(data))
            if file.problem is not None:  # the file turned out unusable: the edit goes on the map in memory, not on a new one
                result.append(change(data))
                self._dirty.discard(key)
                return result[0]
            self._memory[key] = file.update(edited)
            if renames:
                file.rename_in_routes(self._memory[key], renames)
        except (mapfile.MapError, OSError) as e:
            result.clear()
            file.problem = f"The map for {app} couldn't be saved ({e}); what is learned is kept until the daemon stops."
            result.append(change(data))
        self._dirty.discard(key)
        return result[0]

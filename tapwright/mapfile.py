"""The map file (AGENT-TOOLS §18.9): one folder per app, `<root>/<bundle id>/map.json` (format `agent-tools-map`,
version 1) with `routes/<name>.json` beside it. The root is `~/.tapwright/maps` unless `--maps DIR` moves it, so a team,
CI and both tools can share one folder.

Writing is read, merge and write under a lock file (`map.json.lock`, created exclusively, stale after LOCK_STALE s):
the file on disk is merged with what the tool holds in memory by place key and by road (`from`, `move`, `to`), the
newest `last` winning, so one tool's save never drops the other's work. The result goes to a temporary file, then a
rename over the map; on Windows the rename can fail while the other tool has the file open (EPERM, EBUSY), so it is
retried. A file that doesn't parse, or isn't a map this tool reads, is left alone: `problem` says so and the tool keeps
driving from memory without it.
"""
import copy
import errno
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable

from . import explored, names, places, roads, routes, store

log = logging.getLogger(__name__)
LOCK_STALE = 10.0  # seconds after which a lock file is taken as left behind
LOCK_WAIT = 15.0  # seconds to wait for a fresh lock before giving up
LOCK_POLL = 0.01
RENAME_RETRIES = 5  # after the first try: up to 5 more, RENAME_WAIT apart
RENAME_WAIT = 0.05
_BUNDLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_BUSY = (errno.EPERM, errno.EACCES, errno.EBUSY)
_PROCESS_LOCK = threading.Lock()  # one thread of this process at a time; the lock file keeps the other processes out


class MapError(RuntimeError):
    """The map couldn't be written (a lock that never freed, a rename that never worked). Nothing on disk was lost."""


def root(override: str | os.PathLike | None = None) -> Path:
    """Where the per-app folders are: `--maps DIR`, else the tool's home (`~/.tapwright/maps`)."""
    return Path(override).expanduser() if override else store.HOME / "maps"


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def invalid(data: object) -> str | None:
    """Why `data` isn't a map this tool can read and write back whole, or None. Strict on purpose: a hand-edited file
    that is half wrong is left alone rather than rewritten with parts dropped."""
    if not isinstance(data, dict):
        return "it isn't a JSON object"
    if data.get("format") != places.FORMAT:
        return f"its format isn't {places.FORMAT}"
    version = data.get("version")
    if version != places.VERSION:
        return f"it is version {version!r}; this tool reads version {places.VERSION}"
    if not isinstance(data.get("places"), dict) or not all(
            isinstance(p, dict) and isinstance(p.get("key"), dict) for p in data["places"].values()):
        return "its places aren't an object of places that each have a key"
    if not all(isinstance(p.get("titles", {}), dict) for p in data["places"].values()):
        return "a place's titles aren't an object"
    for section, kind in (("roads", list), ("reflexes", list), ("routes", dict)):
        if section in data and not isinstance(data[section], kind):
            return f"its {section} are the wrong kind of thing"
    if not all(isinstance(r, dict) and isinstance(r.get("from"), str) and isinstance(r.get("to"), str)
               and isinstance(r.get("move"), dict) for r in data.get("roads", [])):
        return "a road lacks its from, to or move"
    if not all(isinstance(r, dict) for r in data.get("reflexes", [])):
        return "a reflex isn't an object"
    return None


# the keys whose string values are the screen's words in a map: a target's or a road's names (id, label, text), a link
# (a place's, a move's), a place's identifiers and selected tab. Nothing else is content: not the format, the version,
# the app, the title key, a hash, a place's signature, a place's or a route's name, a reference or a time (those are made by the
# code and a value in them would break the map: Codex, PR #10 cycle 5)
CONTENT_KEYS = frozenset({"id", "label", "text", "value", "url", "link", "ids", "tab", "sig", "word"})


def without_values(data: dict, scrub: Callable[[str], str]) -> dict:
    """The map as it may be written: no CONTENT string (CONTENT_KEYS, at any depth) holds a test account's value (`scrub`
    takes it out; AGENT-TOOLS §18.6). A road that held one is dropped whole (a road with half a target is no road);
    anywhere else the value is replaced by its placeholder. Keys and every other value are never touched. A last
    backstop: what learns a map keeps values out already."""
    def clean(v: object, content: bool) -> object:
        if isinstance(v, str):
            return scrub(v) if content else v
        if isinstance(v, dict):
            return {k: clean(x, k in CONTENT_KEYS) for k, x in v.items()}
        if isinstance(v, list):
            return [clean(x, content) for x in v]
        return v
    data = _without_held_sigs(data, scrub)
    out = {k: clean(v, k in CONTENT_KEYS) for k, v in data.items() if k != "roads"}
    if "roads" in data:
        out["roads"] = [r for r in data["roads"] if clean(r, False) == r]
    return out


def _without_held_sigs(data: dict, scrub: Callable[[str], str]) -> dict:
    """An action signature holds the screen's words (§25.3): an action (or a frontier entry) whose signature holds a value
    is dropped whole, as a road is, since a signature with half a word is none. The map given is not changed."""
    def held(sig: object) -> bool:
        return isinstance(sig, str) and scrub(sig) != sig
    if not isinstance(data.get("places"), dict):
        return data
    out_places = {}
    for name, place in data["places"].items():
        rec = place.get("explored") if isinstance(place, dict) else None
        if isinstance(rec, dict):
            rec = {**rec, "actions": [a for a in rec.get("actions", []) if not (isinstance(a, dict) and held(a.get("sig")))],
                   "frontier": [s for s in rec.get("frontier", []) if not held(s)]}
            place = {**place, "explored": rec}
        out_places[name] = place
    return {**data, "places": out_places}


def _normal(data: dict) -> dict:
    for section, empty in (("roads", list), ("reflexes", list), ("routes", dict)):
        data.setdefault(section, empty())
    if not isinstance(data.get("titleKey"), str) or not re.fullmatch(r"[0-9a-f]+", data["titleKey"]):
        data["titleKey"] = places.empty_map("")["titleKey"]  # a hand-made file with no key gets one (titles are hashes)
    for name, place in data["places"].items():
        if "explored" in place:  # a record that isn't one is dropped, never a reason to leave the map alone (§28.1)
            kept = explored.clean_record(place["explored"])
            if kept is None:
                del place["explored"]
            else:
                place["explored"] = kept
    return data


def _last(record: dict) -> str:
    return str(record.get("last", ""))


def _unique(taken: dict, base: str) -> str:
    name, n = base, 2
    while name in taken:
        name, n = f"{base}_{n}", n + 1
    return name


def _rename(mapping: dict[str, str], name: str) -> str:
    return mapping.get(name, name)


def _same_place(a: dict, b: dict) -> bool:
    """Two records with one signature: one place? A screen with no identifiers is told from the others of its kinds by
    its title alone (places.locate), so two such records are one place only when a title hash is shared (or neither
    has a title); with identifiers the signature is the identity."""
    parts = a["key"].get("parts")
    if isinstance(parts, dict) and parts.get("ids"):
        return True
    ta, tb = set(a.get("titles", {}).values()), set(b.get("titles", {}).values())
    return bool(ta & tb) or not (ta or tb)


def merge(disk: dict, memory: dict) -> dict:
    """The map on disk and the one in memory as one (§18.9). Places go by key (a signature, or a route key), roads by
    (`from`, `move`, `to`); for either the newest `last` wins. A place's name: a person's beats a title's, which beats
    a number; two different places that want one name get `_2`, and the roads follow. The disk's title key stands (hashes
    under another key can't be compared). Reflexes go by name, routes by name; fields this tool doesn't know are kept."""
    out, mem = _normal(copy.deepcopy(disk)), _normal(copy.deepcopy(memory))
    if mem.get("titleKey") != out.get("titleKey"):
        for place in mem["places"].values():
            place["titles"] = {}
            place.pop("titleVaries", None)
    disk_records = {n: p["explored"] for n, p in out["places"].items() if "explored" in p}
    mem_records = {n: p["explored"] for n, p in mem["places"].items() if "explored" in p}
    by_key: dict[str, list[str]] = {}
    for name, p in out["places"].items():
        by_key.setdefault(places.key_id(p["key"]), []).append(name)
    disk_names: dict[str, str] = {}
    mem_names: dict[str, str] = {}
    for name, rec in mem["places"].items():
        kid = places.key_id(rec["key"])
        found = next((n for n in by_key.get(kid, []) if _same_place(out["places"][n], rec)), None)
        if found is None:
            final = name if name not in out["places"] else _unique(out["places"], name)
            out["places"][final], mem_names[name] = rec, final
            by_key.setdefault(kid, []).append(final)
            continue
        old = out["places"][found]
        newer, other = (rec, old) if _last(rec) >= _last(old) else (old, rec)
        joined = {**other, **newer, "titles": {**other.get("titles", {}), **newer.get("titles", {})},
                  "link": newer.get("link") or other.get("link")}
        if rec.get("named") or old.get("named"):
            joined["named"] = True
        if (rec.get("titleVaries") or old.get("titleVaries")):
            joined["titleVaries"] = sorted(set(rec.get("titleVaries", [])) | set(old.get("titleVaries", [])))
        final = found
        if name != found:
            if rec.get("named") and not old.get("named"):
                final = name
            elif not old.get("named") and not rec.get("named") and places._NUMBERED.match(found) and not places._NUMBERED.match(name):
                final = name
        if final != found:
            final = final if final not in out["places"] else _unique(out["places"], final)
            out["places"] = {(final if n == found else n): p for n, p in out["places"].items()}
            disk_names[found] = final
            by_key[kid] = [final if n == found else n for n in by_key[kid]]
        out["places"][final] = joined
        mem_names[name] = final
    # the explored records (§28.2), once every rename is known: an action's `to` follows its own side's renames
    held_records: dict[str, list[dict]] = {}
    for source, renames in ((disk_records, disk_names), (mem_records, mem_names)):
        for name, rec in source.items():
            held_records.setdefault(_rename(renames, name), []).append(explored.rename_targets(rec, renames))
    for final, place in out["places"].items():
        sides = held_records.get(final, [])
        place.pop("explored", None)
        if sides:
            place["explored"] = explored.merge_record(*sides) if len(sides) == 2 else sides[0]
    all_roads: dict[tuple[str, str, str], dict] = {}
    for source, names in ((out["roads"], disk_names), (mem["roads"], mem_names)):
        for road in source:
            road = {**road, "from": _rename(names, road["from"]), "to": _rename(names, road["to"])}
            key = (road["from"], json.dumps(road["move"], sort_keys=True, separators=(",", ":")), road["to"])
            held = all_roads.get(key)
            if held is None:
                all_roads[key] = road
                continue
            newer, other = (road, held) if _last(road) >= _last(held) else (held, road)
            joined = {**other, **newer, "names": {**other.get("names", {}), **newer.get("names", {})}}
            if newer.get("by") != other.get("by"):
                joined["by"] = "both"
            all_roads[key] = joined
    out["roads"] = list(all_roads.values())
    def _at(reflex: dict, names: dict[str, str]) -> dict:
        return {**reflex, "at": _rename(names, reflex["at"])} if "at" in reflex else reflex
    reflexes = {r.get("name"): _at(r, disk_names) for r in out["reflexes"]}  # each follows its own map's renames
    for reflex in mem["reflexes"]:
        reflexes[reflex.get("name")] = _at(reflex, mem_names)
    out["reflexes"] = list(reflexes.values())
    out["routes"] = {**out["routes"], **mem["routes"]}
    for field, value in mem.items():
        out.setdefault(field, value)
    return out


class MapFile:
    """One app's map folder. `problem` is why the file is being left alone (None when all is well)."""

    def __init__(self, base: str | os.PathLike, app: str, scrub: Callable[[str], str] | None = None) -> None:
        if not _BUNDLE.match(app) or set(app) == {"."}:
            raise ValueError(f"{app!r} isn't an app id a map folder can be named after")
        self.app = app
        self.folder = Path(base) / app
        self.path = self.folder / "map.json"
        self.lock_path = self.folder / "map.json.lock"
        self.routes_dir = self.folder / "routes"
        self.problem: str | None = None
        self.scrub = scrub  # takes a test account's value out of a string; None: no accounts to keep out

    # ---- reading ----
    def read(self) -> tuple[dict | None, str | None]:
        """(the map on disk, why it can't be used). (None, None) when there is no file yet."""
        for attempt in range(RENAME_RETRIES + 1):
            try:
                text = self.path.read_text("utf-8-sig")
                break
            except FileNotFoundError:
                return None, None
            except PermissionError:  # the other tool is renaming over it just now
                if attempt == RENAME_RETRIES:
                    return None, f"The map file {self.path} can't be read just now, so it is left alone."
                time.sleep(RENAME_WAIT)
            except (OSError, UnicodeDecodeError) as e:
                return None, f"The map file {self.path} can't be read ({getattr(e, 'strerror', None) or e}), so it is left alone."
        try:
            data = json.loads(text)
        except (ValueError, RecursionError) as e:
            return None, f"The map file {self.path} doesn't parse ({e}), so it is left alone and nothing is saved to it; fix or delete it."
        if (why := invalid(data)) is not None:
            return None, f"The map file {self.path} isn't a map this tool can read ({why}), so it is left alone; fix or delete it."
        return _normal(data), None

    def load(self) -> dict:
        """The map to work from: the file's, or a new one (nothing is written). `problem` says when the file is left alone."""
        data, self.problem = self.read()
        return data if data is not None else places.empty_map(self.app)

    # ---- writing ----
    def _acquire(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    if time.time() - self.lock_path.stat().st_mtime > LOCK_STALE:
                        self.lock_path.unlink()  # left behind by a process that died
                        continue
                except OSError:
                    if time.monotonic() > deadline:
                        raise MapError(f"the map's lock {self.lock_path} can't be cleared ({LOCK_WAIT:g} s)") from None
                    continue  # it went away between the two calls
                if time.monotonic() > deadline:
                    raise MapError(f"the map's lock {self.lock_path} stayed taken for {LOCK_WAIT:g} s") from None
                time.sleep(LOCK_POLL)
            except PermissionError as e:  # Windows: the lock file is being deleted just now (or the folder can't be written)
                if time.monotonic() > deadline:
                    raise MapError(f"the map's lock {self.lock_path} can't be taken ({e.strerror or e})") from None
                time.sleep(LOCK_POLL)
            else:
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return

    def _release(self) -> None:
        try:
            self.lock_path.unlink()
        except OSError:
            pass

    def _write(self, data: dict) -> None:
        self.routes_dir.mkdir(parents=True, exist_ok=True)
        if self.scrub is not None:
            data = without_values(data, self.scrub)
            if (why := invalid(data)) is not None:  # never replace a map that reads with one that doesn't: the old file stays
                log.error("the map %s was not saved: the redacted map is invalid (%s)", self.path, self.scrub(why))
                return
        tmp = self.folder / f"map.json.{os.getpid()}.{threading.get_ident()}.tmp"
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", "utf-8")
        try:
            for attempt in range(RENAME_RETRIES + 1):
                try:
                    os.replace(tmp, self.path)
                    return
                except OSError as e:
                    if e.errno not in _BUSY and not isinstance(e, PermissionError):
                        raise MapError(f"couldn't write {self.path}: {e.strerror or e}") from e
                    if attempt == RENAME_RETRIES:
                        raise MapError(f"couldn't write {self.path}: the file stayed in use") from e
                    time.sleep(RENAME_WAIT)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    def _locked(self, change: Callable[[dict | None], dict]) -> dict:
        with _PROCESS_LOCK:
            self._acquire()
            try:
                disk, self.problem = self.read()
                if self.problem is not None:
                    return change(None)  # the file is left alone: the tool's own copy is all there is
                data = change(disk)
                self._write(data)
                return data
            finally:
                self._release()

    def update(self, change: Callable[[dict], object]) -> dict:
        """Read the map under the lock, let `change` edit it in place, write it back; returns the map. With a file that
        is left alone, `change` edits a new map held in memory instead, and nothing is written."""
        def apply(disk: dict | None) -> dict:
            data = disk if disk is not None else places.empty_map(self.app)
            change(data)
            return data
        return self._locked(apply)

    def save(self, memory: dict) -> dict:
        """Merge the tool's map in memory into the file's (§18.9) and write the result; returns it. A file that is left
        alone returns `memory` unchanged."""
        def apply(disk: dict | None) -> dict:
            return memory if disk is None and self.problem is not None else (
                _normal(copy.deepcopy(memory)) if disk is None else merge(disk, memory))
        return self._locked(apply)

    # ---- routes ----
    def write_route(self, name: str, route: dict) -> Path:
        """Save a route as `routes/<name>.json` beside the map, and list it in the map (a route is a file of its own, so a
        person can review each one). ValueError for a name that isn't lower-case ASCII, digits and `_`."""
        held = [*self.load()["routes"]]  # (the map's list, and the files: either may be all there is)
        if self.routes_dir.is_dir():
            held += [p.stem for p in self.routes_dir.glob("*.json")]
        if (other := names.twin(name, held)) is not None:  # before anything is written (1.13.1, item 5)
            raise ValueError(names.twin_message("route", name, other))
        if not routes.NAME.match(name):
            raise ValueError(f"a route's name is lower-case ASCII letters, digits and _ (at most 60), not {name!r}")
        rel = f"routes/{name}.json"
        path = self.routes_dir / f"{name}.json"
        self.routes_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.routes_dir / f"{name}.json.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(route, indent=2, ensure_ascii=False) + "\n", "utf-8")
        os.replace(tmp, path)
        self.update(lambda d: d["routes"].__setitem__(name, rel))
        return path

    def read_route(self, data: dict, name: str) -> dict | None:
        """A saved route's file as a dict, or None when the map has no such route or its file can't be read."""
        return self._route_file(data.get("routes", {}).get(name))

    def _route_file(self, rel: object) -> dict | None:
        if not isinstance(rel, str):
            return None
        path = (self.folder / rel).resolve()
        if self.folder.resolve() not in path.parents:
            return None  # a route is a file inside this app's folder
        try:
            data = json.loads(path.read_text("utf-8-sig"))
        except (OSError, ValueError, RecursionError):
            return None
        return data if isinstance(data, dict) else None

    def rename_in_routes(self, data: dict, renames: dict[str, str]) -> None:
        """A place was renamed: the saved routes that start or end there say the new name (a route's start and end are
        place names, and a route that names a place the map no longer has can't be driven). Never raises."""
        for rel in data.get("routes", {}).values():
            route = self._route_file(rel)
            if not route or not any(route.get(end) in renames for end in ("start", "end")):
                continue
            for end in ("start", "end"):
                if route.get(end) in renames:
                    route[end] = renames[route[end]]
            path = (self.folder / rel).resolve()
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            try:
                tmp.write_text(json.dumps(route, indent=2, ensure_ascii=False) + "\n", "utf-8")
                os.replace(tmp, path)
            except OSError:
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def route_edges(self, data: dict) -> list[roads.RouteEdge]:
        """The saved routes as ways between places (`drive` counts one move per step). A route that can't be read, or
        has no start and end, isn't one."""
        edges = []
        for name, rel in data.get("routes", {}).items():
            route = self._route_file(rel)
            if route and isinstance(route.get("start"), str) and isinstance(route.get("end"), str):
                steps = route.get("steps")
                edges.append(roads.RouteEdge(name, route["start"], route["end"], len(steps) if isinstance(steps, list) else 1))
        return edges

    def route_info(self, data: dict) -> list[tuple[str, str, str, list[str]]]:
        """(name, start, end, variables) of each saved route, for the listing."""
        info = []
        for name, rel in data.get("routes", {}).items():
            route = self._route_file(rel)
            if route:
                variables = route.get("variables")
                info.append((name, str(route.get("start", "?")), str(route.get("end", "?")),
                             [v for v in variables if isinstance(v, str)] if isinstance(variables, list) else []))
        return info

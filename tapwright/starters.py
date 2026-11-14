"""Starter maps and routes shipped with Tapwright (AGENT-TOOLS §24.1) for the phone's own apps, and
`tapwright starters install`, which puts them into the user's maps (§18.9) without overwriting their work.

The package keeps one folder per app, `tapwright/starters/<bundle id>/`, in the map file's own layout: `map.json`
(the places the starter routes use, and the roads between them; never a title, so `titles` is empty) and
`routes/<name>.json`. Installing is the map merge of mapfile.py, with the shipped copy dated long ago so anything the
user's own map learned wins, and a route the user already has by that name is kept: the answer says so.
"""
import copy
import json
from pathlib import Path

from . import mapfile, places

SHIPPED = Path(__file__).resolve().parent / "starters"
OLD = "2026-01-01T00:00:00Z"  # the shipped places' and roads' `last`: a user's own learning is always newer


class StarterError(ValueError):
    """A shipped starter that can't be read (a broken package, never the user's files)."""


def apps(folder: Path | None = None) -> list[str]:
    """The bundle ids that ship a starter, sorted."""
    base = folder or SHIPPED
    return sorted(p.name for p in base.iterdir() if (p / "map.json").is_file()) if base.is_dir() else []


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as e:
        raise StarterError(f"the starter file {path} can't be read ({e})") from e
    if not isinstance(data, dict):
        raise StarterError(f"the starter file {path} isn't a JSON object")
    return data


def load(app: str, folder: Path | None = None) -> tuple[dict, dict[str, dict]]:
    """(the shipped map, its routes by name) of one app. StarterError when either doesn't read as one."""
    base = (folder or SHIPPED) / app
    data = _read(base / "map.json")
    if (why := mapfile.invalid(data)) is not None:
        raise StarterError(f"the starter map of {app} isn't a map ({why})")
    found: dict[str, dict] = {}
    for name, rel in data.get("routes", {}).items():
        if not isinstance(rel, str):
            raise StarterError(f"the starter route {name} of {app} has no file")
        found[name] = _read(base / rel)
    return data, found


def _plain(data: dict) -> dict:
    """The shipped map as it is merged: no titles, no routes (those are written by name), every date old."""
    out = copy.deepcopy(data)
    out["routes"] = {}
    for place in out["places"].values():
        place["titles"], place["last"] = {}, OLD
        place.pop("titleVaries", None)
    for road in out.get("roads", []):
        road["last"] = OLD
    return out


def _final_names(merged: dict, shipped: dict) -> dict[str, str]:
    """Where each shipped place went in the merged map, when the user's map already had another place by its name."""
    held = {places.key_id(p["key"]): n for n, p in merged["places"].items()}
    out = {}
    for name, rec in shipped["places"].items():
        final = held.get(places.key_id(rec["key"]), name)
        if final != name:
            out[name] = final
    return out


def install_one(base: Path, app: str, folder: Path | None = None) -> tuple[list[str], list[str], str | None]:
    """Put one app's starters into `<base>/<app>/`. (routes added, routes kept as the user's, why nothing was done).
    A route already there that is the shipped one, word for word, is neither: it is left, and not mentioned."""
    shipped, route_files = load(app, folder)
    target = mapfile.MapFile(base, app)
    _, problem = target.read()
    if problem is not None:
        return [], [], problem
    existing = set(target.load()["routes"])
    if target.routes_dir.is_dir():
        existing |= {p.stem for p in target.routes_dir.glob("*.json")}
    merged = target.save(_plain(shipped))
    if target.problem is not None:
        return [], [], target.problem
    renames = _final_names(merged, shipped)
    added, kept = [], []
    for name, route in sorted(route_files.items()):
        route = {**route, **{end: renames.get(route.get(end), route.get(end)) for end in ("start", "end")}}
        if name in existing:
            if target.read_route(merged, name) != route:
                kept.append(name)
            continue
        target.write_route(name, route)
        added.append(name)
    return added, kept, None


def install(maps: str | None = None, only: list[str] | None = None, folder: Path | None = None) -> tuple[str, int]:
    """`tapwright starters install`: (the answer, the exit code)."""
    base = mapfile.root(maps)
    wanted = apps(folder)
    if only:
        unknown = [a for a in only if a not in wanted]
        if unknown:
            return f"No starter for {', '.join(unknown)}. Starters ship for: {', '.join(wanted) or 'no app'}.", 2
        wanted = [a for a in wanted if a in only]
    lines, code = [f"Starter routes, into {base}:"], 0
    for app in wanted:
        try:
            added, kept, problem = install_one(base, app, folder)
        except (StarterError, mapfile.MapError, ValueError) as e:
            lines.append(f"  {app}: not installed ({e})")
            code = 1
            continue
        if problem is not None:
            lines.append(f"  {app}: not installed, {problem}")
            code = 1
            continue
        part = [f"added {', '.join(added)}" if added else "nothing new to add"]
        if kept:
            part.append(f"kept yours: {', '.join(kept)}")
        lines.append(f"  {app}: {'; '.join(part)}")
    return "\n".join(lines), code

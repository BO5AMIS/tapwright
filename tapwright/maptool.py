"""The `map` tool's answers and edits (AGENT-TOOLS §18.8): the listing of an app's map, the way `drive` would take, and
the edits (`name`, `link`, `reflex`, `forget_reflex`, `forget`). Pure functions over the map as plain JSON, so they are
testable without a phone; the daemon reads the screen, calls these, and saves what changed.
"""
import re
from dataclasses import dataclass, field

from . import links, places, roads, ui
from . import policy as safety
from .control import Refused

MAX_LISTED = 60  # places in the listing, then `…`
REFLEX_TOOLS = ("tap", "long_press", "toggle", "back")  # a reflex's `do` is one of these
REFLEX_KEYS = ("name", "when", "do", "max", "at")
REFLEX_MAX = 3  # firings per call: 1 (the default) to this
_REFLEX_NAME = re.compile(r"^[a-z0-9_]{1,60}$")
OPS = ("to", "name", "link", "reflex", "forget_reflex", "forget")


@dataclass(frozen=True)
class Context:
    """What the daemon knows around one call: the app (its id, its display name, its build), the place the app is at
    now (None: not a known place), the account names, the saved routes (as listed, and as ways), and the policy."""
    app: str
    name: str | None = None
    build: str | None = None
    current: str | None = None
    accounts: tuple[str, ...] = ()
    routes: list[tuple[str, str, str, list[str]]] = field(default_factory=list)
    edges: list[roads.RouteEdge] = field(default_factory=list)
    policy: safety.Policy = safety.DEFAULT


def _count(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else plural or word + 's'}"


def kind(args: dict) -> str:
    """Which thing a call asks for: `list` (nothing given), or one of OPS. `bad_params` for two at once, a wrong type, or
    `place` without `link`."""
    given = {k: v for k, v in args.items() if v is not None}
    ops = [k for k in OPS if k in given]
    if len(ops) > 1:
        raise Refused("bad_params", f"map does one thing at a time: {', '.join(ops)} were all given")
    if "place" in given and ops != ["link"]:
        raise Refused("bad_params", "place goes with link: map {\"link\": \"wayfare://x\", \"place\": \"x\"}")
    if not ops:
        return "list"
    op, value = ops[0], given[ops[0]]
    if op == "reflex":
        if not isinstance(value, dict):
            raise Refused("bad_params", 'reflex must be an object, like {"name": "rate_prompt", "when": {"text": "Rate Wayfare"}, '
                                        '"do": {"tool": "tap", "target": {"text": "Not now"}}}')
    elif not isinstance(value, str) or not value.strip():
        raise Refused("bad_params", f"{op} must be a non-empty text, not {value!r}")
    if "place" in given and (not isinstance(given["place"], str) or not given["place"].strip()):
        raise Refused("bad_params", f"place must be a non-empty text, not {given['place']!r}")
    return op


# ---- reading ----

def listing(data: dict, ctx: Context) -> list[str]:
    """The listing, one line each (§18.8)."""
    shown = f"{ctx.name} ({ctx.app})" if ctx.name else ctx.app
    head = f"Map of {shown}" + (f", build {ctx.build}" if ctx.build else "")
    if not data["places"]:
        return [f"{head}: no places yet. Look or act, and what is seen is saved."]
    proven = sum(1 for r in data["roads"] if roads.is_proven(r, ctx.build))
    lines = [f"{head}: {_count(len(data['places']), 'place')}, {_count(len(data['roads']), 'road')} ({proven} proven), "
             f"{_count(len(data['routes']), 'route')}, {_count(len(data['reflexes']), 'reflex', 'reflexes')}. "
             f"Now at: {ctx.current or 'unknown'}."]
    names = [f"{n} (link {p['link']})" if p.get("link") else n for n, p in list(data["places"].items())[:MAX_LISTED]]
    lines.append("Places: " + ", ".join(names) + (", …" if len(data["places"]) > MAX_LISTED else ""))
    if ctx.routes:
        lines.append("Routes: " + ", ".join(f"{n} ({start} → {end}{'; ' + ', '.join(v) if v else ''})"
                                            for n, start, end, v in ctx.routes[:MAX_LISTED]))
    if data["reflexes"]:
        lines.append("Reflexes: " + ", ".join(str(r.get("name")) for r in data["reflexes"]) + ".")
    if ctx.accounts:
        lines.append("Accounts: " + ", ".join(ctx.accounts) + ".")
    return lines


def way(data: dict, ctx: Context, to: str) -> str:
    """`map {"to": ...}`: the way `drive` would take, and whether each road is proven. `not_found` for a place the map
    doesn't have, and for no way."""
    if to not in data["places"]:
        known = ", ".join(list(data["places"])[:20]) or "none yet"
        raise Refused("not_found", f"no place called {to}; the places are: {known}")
    found = roads.plan(data, ctx.current, to, ctx.build, routes=ctx.edges)
    if found.kind == "none":
        if ctx.current is None:
            raise Refused("not_found", "where the app is now isn't a place the map knows yet; look first, then ask "
                                       "again. (A place with a link can be reached from anywhere.)")
        raise Refused("not_found", f"no known way from {ctx.current} to {to}; drive it once (the driver), and it is remembered.")
    return roads.say_way(found, to)


# ---- editing ----

def _here(ctx: Context) -> str:
    if ctx.current is None:
        raise Refused("not_found", "the app isn't at a place the map knows yet; look first, or name the place")
    return ctx.current


def _target_text(do: dict) -> str | None:
    target = do.get("target")
    if not isinstance(target, dict):
        return None
    return next((v for k, v in target.items() if k in ("id", "label", "text") and isinstance(v, str)), None)


def _reflex(data: dict, ctx: Context, raw: dict) -> dict:
    """A reflex as it is kept, or `bad_params` / `not_allowed` (§18.7)."""
    unknown = [k for k in raw if k not in REFLEX_KEYS]
    if unknown:
        raise Refused("bad_params", f"a reflex has {', '.join(REFLEX_KEYS)}, not {unknown[0]!r}")
    name = raw.get("name")
    if not isinstance(name, str) or not _REFLEX_NAME.match(name):
        raise Refused("bad_params", f"a reflex's name is lower-case ASCII letters, digits and _, like rate_prompt, not {name!r}")
    when = raw.get("when")
    if not isinstance(when, dict) or len(when) != 1 or next(iter(when)) not in ("text", "label", "id") or \
            not isinstance(next(iter(when.values())), str) or not next(iter(when.values())).strip():
        raise Refused("bad_params", 'a reflex\'s when is one of {"text": ...}, {"label": ...} or {"id": ...} (what is on screen)')
    do = raw.get("do")
    if not isinstance(do, dict) or do.get("tool") not in REFLEX_TOOLS:
        raise Refused("bad_params", f"a reflex's do is one step: a tool of {', '.join(REFLEX_TOOLS)} with its target, like "
                                    '{"tool": "tap", "target": {"text": "Not now"}}')
    if do["tool"] != "back":
        if set(do) - {"tool", "target"}:
            raise Refused("bad_params", "a reflex's do has a tool and a target, nothing else")
        target = ui.parse_target(do.get("target"))  # bad_params, with an example of a target
        if target.kind not in ("id", "label", "text"):
            raise Refused("bad_params", "a reflex's target is an id, label or text: a ref doesn't outlive the screen it came from")
        word = _target_text(do)
        flagged = ctx.policy.check(ctx.app, word)
        if flagged is not None:
            raise Refused("not_allowed", f'{do["tool"]} "{word}" is flagged by the safety policy ({flagged[0]} word '
                                         f'"{flagged[1]}"), and a reflex never does that.')
    elif set(do) != {"tool"}:
        raise Refused("bad_params", 'a back reflex is {"tool": "back"}')
    limit = raw.get("max", 1)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= REFLEX_MAX:
        raise Refused("bad_params", f"a reflex's max is a whole number from 1 to {REFLEX_MAX}, not {limit!r}")
    out = {"name": name, "when": dict(when), "do": dict(do), "max": limit}
    if raw.get("at") is not None:
        if raw["at"] not in data["places"]:
            raise Refused("bad_params", f"a reflex's at is a place the map has; there is no {raw['at']}")
        out["at"] = raw["at"]
    return out


def edit(data: dict, ctx: Context, args: dict) -> str:
    """Make the edit a call asks for on the map, and say what was done. Raises `bad_params`, `not_found` or `not_allowed`."""
    op = kind(args)
    if op == "name":
        here = _here(ctx)
        try:
            base = places.clean_name(args["name"])
            named = places.set_name(data, here, args["name"])
        except ValueError as e:
            raise Refused("bad_params", str(e)) from None
        return f"Named: {named.name}." if not named.taken else f"Named: {named.name} ({base} was taken)."
    if op == "link":
        place = args.get("place") or _here(ctx)
        try:
            links.set_link(data, place, args["link"])
        except KeyError:
            raise Refused("not_found", f"no place called {place}; the places are: {', '.join(list(data['places'])[:20])}") from None
        except ValueError as e:
            raise Refused("bad_params", str(e)) from None
        return f"Link for {place}: {args['link']}."
    if op == "forget":
        name = args["forget"]
        if name not in data["places"]:
            raise Refused("not_found", f"no place called {name}; the places are: {', '.join(list(data['places'])[:20])}")
        gone_roads = sum(1 for r in data["roads"] if name in (r.get("from"), r.get("to")))
        gone_reflexes = sum(1 for r in data["reflexes"] if r.get("at") == name)
        places.forget(data, name)
        parts = [p for p in (f"its {_count(gone_roads, 'road')}" if gone_roads else "",
                             _count(gone_reflexes, "reflex", "reflexes") if gone_reflexes else "") if p]
        tail = "" if not parts else " and " + parts[0] if len(parts) == 1 else f", {parts[0]} and {parts[1]}"
        return f"Forgot {name}{tail}."
    if op == "reflex":
        reflex = _reflex(data, ctx, args["reflex"])
        held = next((i for i, r in enumerate(data["reflexes"]) if r.get("name") == reflex["name"]), None)
        if held is None:
            data["reflexes"].append(reflex)
            return f"Reflex added: {reflex['name']}."
        data["reflexes"][held] = reflex
        return f"Reflex updated: {reflex['name']}."
    if op == "forget_reflex":
        before = len(data["reflexes"])
        data["reflexes"] = [r for r in data["reflexes"] if r.get("name") != args["forget_reflex"]]
        if len(data["reflexes"]) == before:
            raise Refused("not_found", f"no reflex called {args['forget_reflex']}")
        return f"Reflex removed: {args['forget_reflex']}."
    raise Refused("bad_params", f"{op} isn't an edit")

"""`drive` (AGENT-TOOLS §18.4): what a call may say, the wait between moves, the call a road's move makes, how travel is
reported, and the repair of a road by another name. Pure functions, testable without a phone; trip.py makes the moves.

A move is a road's `move` (`tap`, `long_press`, `toggle`, `back`, `swipe`, `open_url`). It is aimed by id, label or
text, never a ref; a list row without an id (`{"row": true}`) can't be replayed. A repaired road is taken only when its
other names find the same element, or one element is the closest match and the only one.
"""
import json
from dataclasses import dataclass

from . import batch, closest, roads, ui
from .control import Refused

BY = ("any", "taps", "link")
KEYS = ("to", "route", "with", "account", "then", "by", "dry_run", "look", "screenshot", "brief")
CAP_FLOOR, CAP_CEILING, CAP_TIMES = 2.0, 10.0, 3.0  # a wait between moves: the road's own time x 3, 2 s to 10 s
MAX_REPLANS = 2  # a call plans again from a known place at most this often
NO_TEXT = "it has no text a person could read"
ROW = "it is a list row with no id, so drive can't replay it; the driver chooses the row"


class Unaimable(Exception):
    """A move that can't be made as it is saved: the reason, for `off_route`."""


@dataclass(frozen=True)
class Request:
    to: str | None
    route: str | None
    values: dict[str, str]
    account: str | None
    then: list[dict] | None
    by: str
    dry_run: bool


def _flag(args: dict, key: str) -> bool:
    value = args.get(key, False)
    if not isinstance(value, bool):
        raise Refused("bad_params", f"{key} must be true or false, not {value!r}")
    return value


def check(args: dict) -> Request:
    """The call's arguments, checked (`bad_params` otherwise)."""
    for key in ("to", "route"):
        if key in args and args[key] is not None and (not isinstance(args[key], str) or not args[key].strip()):
            raise Refused("bad_params", f"{key} must be a non-empty text, not {args[key]!r}")
    to, route = args.get("to"), args.get("route")
    if (to is None) == (route is None):
        raise Refused("bad_params", 'drive takes one of to or route, like {"to": "language"} or {"route": "sign_in"}')
    values: dict[str, str] = {}
    given = args.get("with")
    if given is not None:
        if not isinstance(given, dict):
            raise Refused("bad_params", 'with must be an object of values, like {"id": "42"}')
        for key, value in given.items():
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise Refused("bad_params", f"with's {key} must be a text or a number, not {value!r}")
            values[str(key)] = str(value)
    account = args.get("account")
    if account is not None and (not isinstance(account, str) or not account.strip()):
        raise Refused("bad_params", f"account must be a test account's name, not {account!r}")
    by = args.get("by", "any")
    if by not in BY:
        raise Refused("bad_params", f"by is one of {', '.join(BY)}, not {by!r}")
    then = args.get("then")
    if then is not None:
        then = batch.check_steps(then)
    return Request(to, route, values, account, then, by, _flag(args, "dry_run"))


def cap(road: dict | None) -> float:
    """How long to wait for the next move's target (§18.4): the road's own time x 3, at least 2 s, at most 10 s."""
    ms = road.get("ms") if road else None
    if isinstance(ms, bool) or not isinstance(ms, (int, float)):
        return CAP_FLOOR
    return min(max(ms / 1000 * CAP_TIMES, CAP_FLOOR), CAP_CEILING)


def call_of(move: dict, values: dict[str, str], names: dict | None = None, eyes: bool = False) -> tuple[str, dict]:
    """The tool call a road's move makes: (tool, arguments). `values` fill the params of a link. `eyes`: a user's-eyes
    journey (§18.12): aimed by the visible text the road had, since a person can't see ids."""
    from . import links
    tool = move["tool"]
    if tool == "back":
        return "back", {}
    if tool == "swipe":
        return "swipe", {k: move[k] for k in ("x0", "y0", "x1", "y1", "seconds") if k in move}
    if tool == "open_url":
        try:
            return "open_url", {"url": links.fill(str(move["link"]), values)}
        except ValueError as e:
            raise Refused("bad_params", str(e)) from None
    target = move.get("target")
    if not isinstance(target, dict) or target.get("row"):
        raise Unaimable(ROW)
    if eyes:
        text = (names or {}).get("text") or (names or {}).get("label")
        if not text:
            raise Unaimable(NO_TEXT)
        return tool, {"target": {"text": text}}
    return tool, {"target": dict(target)}


def travel(jumps: int, moves: int, taps_only: bool, reflexes: int, ran: list[tuple[str, int]] | tuple = ()) -> str:
    """`by link`, `by 3 taps`, `by link then 1 tap`, `by 1 tap then route sign_in`, and `, 1 reflex` when one fired
    (§18.4). A route is named without its step count (1.13.1, item 11): `Arrived at home by route sign_in in 3.2 s.`"""
    parts = []
    if jumps:
        parts.append("link")
    if moves:
        parts.append(f"{moves} {'tap' if taps_only else 'move'}{'' if moves == 1 else 's'}")
    parts.extend(f"route {name}" for name, _steps in ran)
    text = "by " + " then ".join(parts) if parts else "by 0 moves"
    return text + (f", {reflexes} reflex{'' if reflexes == 1 else 'es'}" if reflexes else "")


def arrived(place: str, how: str, seconds: float, chain: list[str]) -> str:
    """`Arrived at language by 3 taps in 1.9 s (settings > general > language).`"""
    way = f" ({' > '.join(chain)})" if len(chain) > 1 else ""
    return f"Arrived at {place} {how} in {seconds:.1f} s{way}."


def done(moves: int) -> str:
    return f"Done: {moves} move{'' if moves == 1 else 's'}."


def step_of(move: dict) -> str:
    """How an answer names a move: `tap "Language"`, `tap id=x`, `back`."""
    target = move.get("target")
    return batch.summary({"tool": move["tool"], **({"target": target} if isinstance(target, dict) else {})}) \
        if move["tool"] in roads.TARGETED else move["tool"]


# ---- repair (§18.4, "When the road is wrong") ----

@dataclass(frozen=True)
class Repaired:
    """A road whose target is missing, and the element that stands in for it: the move to make now, the name it was
    aimed by (kind, value) and the name it is aimed by now, and every name the element has."""
    move: dict
    was: tuple[str, str]
    now: tuple[str, str]
    names: dict


def _aim(move: dict) -> tuple[str, str] | None:
    target = move.get("target")
    if not isinstance(target, dict):
        return None
    return next(((k, str(target[k])) for k in ("id", "label", "text") if isinstance(target.get(k), str)), None)


def _unique(items: list[ui.Element], kind: str, value: str) -> ui.Element | None:
    try:
        found = ui.resolve(items, ui.Target(**{kind: value}))
    except Refused:
        return None
    return found if isinstance(found, ui.Element) else None


def _closest_only(names: dict, items: list[ui.Element]) -> ui.Element | None:
    near = []
    for e in items:
        if not e.actions or e.role == "scroll":
            continue
        for kind in ("id", "label", "text"):
            query = names.get(kind)
            texts = [e.identifier, e.name] if kind == "id" else [e.label, e.value]
            if query and any(t and closest.rank(query, t) is not None for t in texts):
                near.append(e)
                break
    return near[0] if len(near) == 1 else None


def repair(move: dict, names: dict, items: list[ui.Element]) -> Repaired | None:
    """The same element by the road's other names, else the closest match when it is the only one (§16.2). None when
    there is none, when the move isn't aimed at an element, or when the element is a list row (a row's text is content)."""
    was = _aim(move)
    if was is None:
        return None
    found = None
    for kind in ("id", "label", "text"):
        value = names.get(kind)
        if kind == was[0] or not isinstance(value, str) or not value:
            continue
        found = _unique(items, kind, value)
        if found is not None:
            break
    if found is None:
        found = _closest_only(names, items)
    if found is None or (found.role == "cell" and not found.identifier):
        return None
    made = roads.move_of(move["tool"], None, found, items, {})
    now = _aim(made) if made else None
    if made is None or now is None or now == was:
        return None
    return Repaired(made, was, now, roads.names_of(found))


def repaired_line(fixed: Repaired) -> str:
    """`Road repaired: "Language" (was id=language-row).`"""
    return f"Road repaired: {batch.aim_name(*fixed.now)} (was {batch.aim_name(*fixed.was)})."


def aim_text(move: dict) -> str:
    return json.dumps(move.get("target"), ensure_ascii=False)

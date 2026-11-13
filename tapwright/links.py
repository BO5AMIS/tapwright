"""Links: the URL that reaches a place (AGENT-TOOLS §18.3). A link can carry a secret (a magic sign-in, a password reset
or an invite holds a token in its query or its path), so what is kept of one the agent opened is cut down:
  - only scheme and path: the query and the fragment are dropped;
  - a path segment is kept only where a known place's pattern has fixed text; where the pattern has a param, `[id]`, the
    segment becomes that variable (`wayfare://trips/42` is kept as `wayfare://trips/[id]`);
  - a link that matches no pattern isn't kept at all.
The patterns are the links the map's places already have. For Tapwright those are links added by hand (`map {"link": ...,
"place": ...}`, kept as given); an Expo Router app's route tree, which Tapline reads, would add more.
A jump is an `open_url` of a place's link: it goes through the app's linking, as a person's tap on the link would.
"""
import re
from dataclasses import dataclass
from urllib.parse import quote, unquote

NOT_SAVED = "Link not saved: it may hold a value; add it with map link."
_PARAM = re.compile(r"^\[([A-Za-z_][A-Za-z0-9_]*)\]$")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*$")


@dataclass(frozen=True)
class Landed:
    """What `landed` did: the link the place now remembers (None when nothing was added) and the note to give."""
    link: str | None
    note: str | None


def _split(url: str) -> tuple[str, list[str]] | None:
    """(scheme lower-cased, path segments) of `scheme://path`, with the query and the fragment dropped and trailing
    empty segments ignored; None when it isn't of that form."""
    if not isinstance(url, str):
        return None
    bare = url.split("#", 1)[0].split("?", 1)[0]
    scheme, sep, tail = bare.partition("://")
    if not sep or not _SCHEME.match(scheme):
        return None
    segments = tail.split("/")
    while segments and segments[-1] == "":
        segments.pop()
    return scheme.lower(), segments


_ENCODED = re.compile(r"%[0-9A-Fa-f]{2}")


def _decoded(segment: str, rounds: int = 8) -> str | None:
    """A path segment with its percent-encoding undone, again until nothing changes (bounded): `%34%38%32` is `482`,
    `%2534` is `4`. A segment is compared decoded, so an encoding can't make fixed text, or a value, look like something
    else (Tapline's finding, 1.6 security). What is kept is never the URL's own text: it is a place's pattern.
    None (fail closed: the link matches nothing) when it still holds an escape after the last round, or an escape
    that isn't UTF-8 (`%FF%64…`): such a segment can't be read for sure (Tapline's Codex finding)."""
    for _ in range(rounds):
        try:
            plain = unquote(segment, errors="strict")
        except UnicodeDecodeError:
            return None
        if plain == segment:
            return segment
        segment = plain
    return None if _ENCODED.search(segment) else segment


def _matches(pattern: tuple[str, list[str]], url: tuple[str, list[str]]) -> bool:
    if pattern[0] != url[0] or len(pattern[1]) != len(url[1]):
        return False
    return all((_PARAM.match(p) is not None and u != "") or p == u for p, u in zip(pattern[1], url[1]))


def canonical(data: dict, url: str) -> str | None:
    """The link as the map may keep it: the pattern of a place's link that `url` matches, else None (not kept)."""
    opened = _split(url)
    if opened is None:
        return None
    plain = [_decoded(seg) for seg in opened[1]]  # (the pattern is kept as written: it is not decoded)
    if any(seg is None for seg in plain):
        return None  # a segment that can't be read for sure: not kept
    opened = (opened[0], [seg for seg in plain if seg is not None])
    for place in data["places"].values():
        link = place.get("link")
        pattern = _split(link) if isinstance(link, str) else None
        if pattern is not None and _matches(pattern, opened):
            return link
    return None


def landed(data: dict, url: str, place: str) -> Landed:
    """An `open_url` of `url` landed on `place`: the place remembers the link when the URL matches a pattern and it has
    no link yet. A URL that matches none isn't kept, and the note says how to add one."""
    found = canonical(data, url)
    if found is None:
        return Landed(None, NOT_SAVED)
    held = data["places"][place]
    if held.get("link"):
        return Landed(None, None)
    held["link"] = found
    return Landed(found, None)


def set_link(data: dict, place: str, link: str | None) -> str | None:
    """`map {"link": ..., "place": ...}`: the link as given (a pattern may have `[param]` segments); None removes it.
    KeyError for no such place; ValueError for something that isn't `scheme://path`."""
    if place not in data["places"]:
        raise KeyError(place)
    if link is not None:
        if _split(link) is None or re.search(r"\s", link):
            raise ValueError(f"a link is like wayfare://settings or wayfare://trips/[id], not {link!r}")
    data["places"][place]["link"] = link
    return link


def variables(link: str) -> list[str]:
    """The params a link pattern takes, in order: `wayfare://trips/[id]` takes `id`."""
    split = _split(link)
    return [m.group(1) for seg in (split[1] if split else []) if (m := _PARAM.match(seg))]


def fill(link: str, values: dict) -> str:
    """The link with its params filled in (`drive`'s `with`), each value encoded as one path segment. ValueError when a
    param has no value."""
    def one(match: re.Match) -> str:
        name = match.group(1)
        if name not in values or values[name] in (None, ""):
            raise ValueError(f"this link needs {name}: pass it in with")
        return quote(str(values[name]), safe="")
    return re.sub(r"(?<=/)\[([A-Za-z_][A-Za-z0-9_]*)\](?=/|$|\?|#)", one, link)

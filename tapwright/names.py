"""Names are ASCII, and a name that differs from an existing one only in case is refused (AGENT-TOOLS 1.13.1, item 5).

`home` and `HOME` are one file on Windows and macOS, so a script, a reference picture, a route or a place may not take
a name that another one already has in another case. The check comes before anything is written."""
from collections.abc import Iterable

from .control import Refused


def twin(name: str, existing: Iterable[str]) -> str | None:
    """The existing name that is `name` in another case, or None. (The same name is no twin: it is the same file.)"""
    low = name.lower()
    return next((other for other in existing if other != name and other.lower() == low), None)


def twin_message(kind: str, name: str, other: str) -> str:
    return (f'the {kind} "{name}" differs only in case from the existing {kind} "{other}" (one file on Windows and '
            f'macOS); use "{other}", or pick another name')


def refuse_twin(kind: str, name: str, existing: Iterable[str]) -> None:
    """`bad_params` naming the existing one when `name` is its twin."""
    if (other := twin(name, existing)) is not None:
        raise Refused("bad_params", twin_message(kind, name, other))

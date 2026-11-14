"""Routes (AGENT-TOOLS §18.5): a script in version 2 that starts at a place instead of a fresh start, saved by name in the
app's map folder (`routes/<name>.json`). Its steps may hold `${variable}` values, filled when the route is driven, and an
account's `${account.username}` and `${account.password}`, which are left for the typing itself to fill (accounts.py): an
account's value is the whole text of a step, never part of one, so joining text can't get around the secure-field rule.
Only the text a `type_text` types is a template: there a `${name}` is filled in and a literal `${` is written `$${`. Every
other field (a target's label, a link, an expectation) is literal, at the save and at the replay alike, so a selector
replays the element it was recorded on and is checked for account values as the text it is (Codex, PR #10 cycle 14).
Pure functions: no phone, no files.
"""
from __future__ import annotations

import re
from typing import Iterator

from .control import Refused

FORMAT, VERSION = "agent-tools-script", 2
NAME = re.compile(r"^[a-z0-9_]{1,60}$")
VARIABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ACCOUNT_FIELDS = ("username", "password")
ACCOUNT = "account"
_TOKEN = re.compile(r"\$\$\{|\$\{([^}]*)\}")
_PLACED = re.compile(r"\$\{(" + ACCOUNT + r"\.[^}]*)\}")  # an account's placeholder in a literal field


def route(name: str, app: str | None, start: str, end: str | None, variables: list[str], steps: list[dict]) -> dict:
    return {"format": FORMAT, "version": VERSION, "name": name, "app": app, "start": start, "end": end,
            "variables": list(variables), "steps": steps}


def literal(text: str) -> str:
    """Typed text as a step keeps it: a `${` in it is written `$${`, so it is never taken for a variable."""
    return text.replace("${", "$${")


def recorded_text(text: str, name: str | None) -> str:
    """The text a recorded `type_text` is saved with: `${name}` when the agent named it with `as`, else the text itself,
    escaped. `bad_params` for a name that can't be a variable."""
    if name is None:
        return literal(text)
    if not isinstance(name, str) or not VARIABLE.match(name):
        raise Refused("bad_params", f"as names a variable: letters, digits and _, not {name!r}")
    if name == ACCOUNT:
        raise Refused("bad_params", "account is the test account's name; give the variable another name")
    return "${" + name + "}"


def _strings(value: object, typed: bool | None = None) -> Iterator[str]:
    """Every string a step holds, nested ones too: with `typed` True only the texts a `type_text` types (the templates),
    with False only the others (literal), with None all of them."""
    if isinstance(value, str):
        if typed is not True:
            yield value
    elif isinstance(value, dict):
        typing = value.get("tool") == "type_text"
        for key, item in value.items():
            if typing and key == "text" and isinstance(item, str):
                if typed is not False:
                    yield item
            else:
                yield from _strings(item, typed)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item, typed)


def references(steps: list[dict]) -> list[tuple[str, str, str]]:
    """Every `${name}` in the steps' typed texts as (name, the whole string it is in, "type_text"): the only place a
    variable stands for anything (Codex, PR #10 cycle 14)."""
    found = []
    for step in steps:
        for text in _strings(step, typed=True):
            for match in _TOKEN.finditer(text):
                if match.group(1) is not None:
                    found.append((match.group(1), text, "type_text"))
    return found


def check(data: dict) -> None:
    """A version 2 script's own fields (`bad_params` otherwise): a name, a `start`, an optional `end`, the variables its
    steps use, and nothing in them that would join an account's value to other text."""
    name = data.get("name")
    start = data.get("start")
    if not isinstance(start, str) or not start.strip():
        raise Refused("bad_params", "a route needs a start: the name of the place it starts at, like \"welcome\"")
    if data.get("end") is not None and (not isinstance(data["end"], str) or not data["end"].strip()):
        raise Refused("bad_params", "a route's end is a place's name, or left out")
    variables = data.get("variables", [])
    if not isinstance(variables, list) or any(not isinstance(v, str) or not VARIABLE.match(v) for v in variables):
        raise Refused("bad_params", "a route's variables are a list of names made of letters, digits and _, like [\"language\"]"
                                    " (a variable's name can't have spaces)")
    if isinstance(name, str) and not NAME.match(name):
        raise Refused("bad_params", f"a route's name is lower-case ASCII letters, digits and _, not {name!r}")
    for text in _strings(data.get("steps", []), typed=True):
        if "$${" + ACCOUNT + "." in text:  # filled to a literal ${account.…}, which typing would then take for a placeholder
            raise Refused("bad_params", "a literal account placeholder can't be written in a route: it would be typed as "
                                        f"the account's value, not as text: {text!r}")
    for text in _strings(data.get("steps", []), typed=False):  # (literal there, so it would find nothing on the phone)
        if (placed := _PLACED.search(text)) is not None:
            raise Refused("bad_params", f"an account's value is the whole text of a type_text step, never part of one or "
                                        f"anywhere else: {text!r} holds ${{{placed.group(1)}}}")
    for ref, text, tool in references(data.get("steps", [])):
        if ref.startswith(ACCOUNT + "."):
            field = ref[len(ACCOUNT) + 1:]
            if field not in ACCOUNT_FIELDS:
                raise Refused("bad_params", f"${{{ref}}} isn't a value an account has: use ${{account.username}} or "
                                            "${account.password}")
            if text != "${" + ref + "}" or tool != "type_text":
                raise Refused("bad_params", f"an account's value is the whole text of a type_text step, never part of "
                                            f"one or anywhere else: {text!r} holds ${{{ref}}}")
            if ACCOUNT not in variables:
                raise Refused("bad_params", f"a route that types ${{{ref}}} lists account in its variables, so that "
                                            "drive is given an account")
        elif ref not in variables:
            raise Refused("bad_params", f"a step uses ${{{ref}}}, which the route doesn't list in its variables "
                                        f"({', '.join(variables) or 'none'}); a literal ${{ is written $${{")


def need(data: dict, values: dict[str, str], account: str | None) -> None:
    """`bad_params` naming a variable the route declares that the call gave no value for."""
    for variable in data.get("variables", []):
        if variable == ACCOUNT:
            if account is None:
                raise Refused("bad_params", f'route {data.get("name")} needs an account: pass account: "<name>" '
                                            "(the test accounts are named in map)")
        elif variable not in values:
            raise Refused("bad_params", f'route {data.get("name")} needs {variable}: pass it in with, like '
                                        f'with: {{"{variable}": "..."}}')


def _fill(text: str, values: dict[str, str]) -> str:
    def one(match: re.Match) -> str:
        if match.group(1) is None:
            return "${"  # `$${` is a literal `${`
        name = match.group(1)
        if name.startswith(ACCOUNT + "."):
            return match.group(0)  # the typing fills it, from the account, where the field is known
        return values.get(name, match.group(0))
    return _TOKEN.sub(one, text)


def _walk(value: object, values: dict[str, str]) -> object:
    if isinstance(value, dict):
        typing = value.get("tool") == "type_text"
        return {k: _fill(v, values) if typing and k == "text" and isinstance(v, str) else _walk(v, values)
                for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(v, values) for v in value]
    return value  # (a string anywhere else is literal: kept as it is, `$${` and `${name}` included)


def substitute(steps: list[dict], values: dict[str, str]) -> list[dict]:
    """The steps with each `${variable}` filled in and each `$${` made `${` in the text a `type_text` types, the only
    template; every other field is left literal, as the save checked it (Codex, PR #10 cycle 14). The route itself is
    left as it was. An account's placeholder stays: the value is typed from the account and goes no further."""
    return [_walk(step, values) for step in steps]

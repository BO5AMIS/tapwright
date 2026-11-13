"""Test accounts (AGENT-TOOLS §18.6): named accounts kept in a file on the user's computer (`--accounts FILE`). An agent
says `account: "tester2"`; the tool types the values. They are never in an answer, the action log, the map, a script, a
report or a caption: every answer is scrubbed of them, a name that holds one is never saved, and a step carries only the
placeholder `${account.username}` or `${account.password}`, filled in at the moment of typing.

  - A value is the whole text of a step, never part of one (joining text would get around the secure-field rule).
  - A password goes only into a secure field, a username only into a text field that isn't secure.
  - `apps` limits where an account may be used.
  - A file inside a git work tree that doesn't ignore it loads no accounts: any use says so.
  - `phone.passcode` is stored and exposed nowhere. (Typing it on the lock screen is a later task.)

Pure but for reading the file and asking git: no phone.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .control import Refused

VERSION = 1
NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
FIELDS = ("username", "password", "passcode", "apps")
PLACEHOLDER = re.compile(r"\$\{account\.(username|password)\}")
MENTION = re.compile(r"\$\{account\.")
MIN_SCRUBBED = 3  # a value shorter than this isn't scrubbed out of text (it would eat ordinary words)
PHONE = "phone"  # the entry that holds the phone's passcode, not an account to sign in with
NO_FILE = ("no test accounts: start tapwright serve or mcp with --accounts FILE (a JSON file kept outside any git repo, "
           "or ignored by it)")
GitRunner = Callable[[list[str], str], "subprocess.CompletedProcess[str] | None"]


@dataclass(frozen=True)
class Account:
    name: str
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)
    apps: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Loaded:
    """What a file holds: the accounts (by name, in the file's order), the passcode, and why none can be used (None when
    all is well). Nothing here shows a value in its repr."""
    path: str = ""
    accounts: dict[str, Account] = field(default_factory=dict, repr=False)
    passcode: str | None = field(default=None, repr=False)
    problem: str | None = None

    @property
    def names(self) -> tuple[str, ...]:
        """The accounts an agent may name (a problem leaves none)."""
        if self.problem is not None:
            return ()
        return tuple(n for n, a in self.accounts.items() if a.username or a.password)

    def account(self, name: str, bundle: str | None) -> Account:
        """The account called `name`, for use in app `bundle`. `bad_params` when no file is given or the file can't be used,
        `not_found` for a name it hasn't, `not_allowed` for an app the account isn't for."""
        if not self.path:
            raise Refused("bad_params", NO_FILE)
        if self.problem is not None:
            raise Refused("bad_params", self.problem)
        if name not in self.names:
            raise Refused("not_found", f"no account called {name}; the accounts are: {', '.join(self.names) or 'none'}")
        found = self.accounts[name]
        if found.apps is not None and bundle not in found.apps:
            raise Refused("not_allowed", f"account {name} may be used only in {', '.join(found.apps)}; this is {bundle or 'no app'}")
        return found

    def _values(self) -> list[tuple[str, str]]:
        out = []
        for account in self.accounts.values():
            if account.username and len(account.username) >= MIN_SCRUBBED:
                out.append((account.username, f"<{account.name}'s username>"))
            if account.password and len(account.password) >= MIN_SCRUBBED:
                out.append((account.password, f"<{account.name}'s password>"))
        out.sort(key=lambda pair: -len(pair[0]))  # the longest first: a value that holds another is replaced whole
        return out

    def scrub(self, text: str) -> str:
        """The text with every account's username and password replaced by `<name's username>` / `<name's password>`."""
        if self.problem is not None and not self.accounts:
            return text
        for value, label in self._values():
            if value in text:
                text = text.replace(value, label)
        return text

    def holds(self, text: str) -> bool:
        """Does the text hold an account's value? What does is never saved to a map."""
        return any(value in text for value, _ in self._values())

    def clean(self, names: dict) -> dict:
        """A road's names without the ones that hold an account's value (a field showing the username is no name)."""
        return {k: v for k, v in names.items() if not (isinstance(v, str) and self.holds(v))}


NONE = Loaded()  # no file was given


def mentions(text: str) -> bool:
    return bool(MENTION.search(text))


def typed(text: str, active: Account | None, secure: bool, ref: str) -> str:
    """The text a `type_text` really types into the field `ref`: an account's placeholder becomes its value, where the
    field allows it (§18.6). `bad_params` for a placeholder with no account active, one that is part of a text, a value
    the account lacks, a password into a field that isn't secure or a username into one that is."""
    if not mentions(text):
        return text
    if active is None:
        raise Refused("bad_params", "an account's value is typed only inside drive with an account: drive {\"route\": "
                                    "\"<name>\", \"account\": \"<account>\"}, or its `then` steps")
    whole = PLACEHOLDER.fullmatch(text)
    if whole is None:
        raise Refused("bad_params", "an account's value is the whole text of a step, never part of one: "
                                    "\"text\": \"${account.username}\" works, \"hi ${account.password}\" doesn't")
    which = whole.group(1)
    value = active.password if which == "password" else active.username
    if not value:
        raise Refused("bad_params", f"account {active.name} has no {which}")
    if which == "password" and not secure:
        raise Refused("bad_params", f"an account's password goes only into a password field; {ref} is a text field.")
    if which == "username" and secure:
        raise Refused("bad_params", f"an account's username goes only into a text field that isn't secure; {ref} is a password field.")
    return value


# ---- the file ----

def _git(args: list[str], cwd: str) -> "subprocess.CompletedProcess[str] | None":
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None  # no git here, or it didn't answer: the file isn't in a repo as far as anyone can tell


def git_problem(path: str, git: GitRunner = _git) -> str | None:
    """Why the file may not be used: it is inside a git work tree that doesn't ignore it (a tracked file isn't ignored)."""
    folder = str(Path(path).resolve().parent)
    top = git(["rev-parse", "--show-toplevel"], folder)
    if top is None or top.returncode != 0 or not top.stdout.strip():
        return None
    ignored = git(["check-ignore", "-q", "--", str(Path(path).resolve())], folder)
    if ignored is None or ignored.returncode == 0:
        return None
    if ignored.returncode != 1:
        return None  # git failed rather than said no: not a verdict
    return (f"accounts file is inside the git repo at {top.stdout.strip()} and not ignored; move it or add it to .gitignore.")


def _read(data: object) -> tuple[dict[str, Account], str | None, str | None]:
    """(accounts, passcode, problem) of a parsed file."""
    if not isinstance(data, dict):
        return {}, None, "it isn't a JSON object"
    if data.get("version") != VERSION:
        return {}, None, f"its version is {data.get('version')!r}; this tool reads version {VERSION}"
    raw = data.get("accounts")
    if not isinstance(raw, dict):
        return {}, None, 'it has no "accounts" object'
    found: dict[str, Account] = {}
    passcode = None
    for name, entry in raw.items():
        if not isinstance(name, str) or not NAME.match(name):
            return {}, None, f"an account's name is letters, digits, - and _, not {name!r}"
        if not isinstance(entry, dict):
            return {}, None, f"{name} isn't an object"
        other = [k for k in entry if k not in FIELDS]
        if other:
            return {}, None, f"{name} has {other[0]}; an account has {', '.join(FIELDS)}"
        for key in ("username", "password", "passcode"):
            if key in entry and (not isinstance(entry[key], str) or not entry[key]):
                return {}, None, f"{name}'s {key} must be a non-empty text"
        for key in ("username", "password"):
            if key in entry and len(entry[key]) < MIN_SCRUBBED:  # too short to be scrubbed out of text, so not accepted
                return {}, None, f"{name}'s {key} must be at least {MIN_SCRUBBED} characters: a shorter one can't be kept out of answers and maps"
        apps = entry.get("apps")
        if apps is not None and (not isinstance(apps, list) or not all(isinstance(a, str) and a for a in apps)):
            return {}, None, f"{name}'s apps must be a list of bundle ids"
        if name == PHONE:
            passcode = entry.get("passcode")
        found[name] = Account(name, entry.get("username"), entry.get("password"), tuple(apps) if apps is not None else None)
    return found, passcode, None


def load(path: str, git: GitRunner = _git) -> Loaded:
    """The accounts file at `path`. It never raises: a file that can't be used is a `problem`, said at the first use."""
    try:
        text = Path(path).read_text("utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        return Loaded(path, problem=f"the accounts file {path} can't be read ({getattr(e, 'strerror', None) or e})")
    if (why := git_problem(path, git)) is not None:
        return Loaded(path, problem=why)  # the file is not even parsed: nothing of it is loaded
    try:
        data = json.loads(text)
    except (ValueError, RecursionError) as e:
        return Loaded(path, problem=f"the accounts file {path} isn't JSON ({e})")
    found, passcode, why = _read(data)
    if why is not None:
        return Loaded(path, problem=f"the accounts file {path} can't be used: {why}")
    return Loaded(path, found, passcode)


class Store:
    """The loaded files, each read again when it changes (its size or time), so an edit needs no restart."""

    def __init__(self) -> None:
        self._held: dict[str, tuple[tuple[int, int], Loaded]] = {}

    def get(self, path: str | None) -> Loaded:
        if not path:
            return NONE
        try:
            stat = os.stat(path)
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            stamp = (-1, -1)
        held = self._held.get(path)
        if held is None or held[0] != stamp:
            held = (stamp, load(path))
            self._held[path] = held
        return held[1]

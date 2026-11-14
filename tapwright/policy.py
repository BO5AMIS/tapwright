"""The safety policy (docs/AGENT-TOOLS.md section 11): words on a button or switch that make a touch need a person's
OK (`confirm`) or never happen (`block`). A word matches the touched element's own label or text, whole words,
ignoring case; testIDs and accessibility identifiers are never matched. Pure logic: no phone, no network.

Version 2 (§19.1) adds `grants`: a person's standing OK for a kind of touch, with limits. Their rules, counts and edits
are grants.py's; a policy loaded from a version 2 file keeps the file's absolute path (`file`), which the daemon reads
again at every touch a grant might cover (§19.5). The built-in policy has no grants."""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

VERSION = 1
VERSIONS = (1, 2)
BLOCKED, CONFIRM = "blocked", "confirm"
DEFAULT_CONFIRM = ("Buy", "Purchase", "Subscribe", "Pay", "Delete", "Remove", "Send", "Post", "Publish",
                   "Sign out", "Log out", "Erase")
_KEYS = ("version", "confirm", "block", "apps", "grants", "explore")
_EXPLORE_KEYS = ("skip", "submit")
_APP_KEYS = ("confirm", "block")


@dataclass(frozen=True)
class AppWords:
    """One app's own lists. A list left out (None) stays the global one; an empty list turns that class off."""
    confirm: tuple[str, ...] | None = None
    block: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ExploreWords:
    """`"explore": {"skip": [...], "submit": [...]}` (§25.5, §25.7): words the explorer never touches, and ids of controls it
    may treat as a form's submit. Only `explore` reads them; a tool that doesn't know the key ignores it."""
    skip: tuple[str, ...] = ()
    submit: tuple[str, ...] = ()


@dataclass(frozen=True)
class Policy:
    confirm: tuple[str, ...] = ()
    block: tuple[str, ...] = ()
    apps: dict[str, AppWords] = field(default_factory=dict)  # by bundle id; an app's lists replace the global ones
    file: str | None = None  # a version 2 file's absolute path: its grants are read from it at every touch (§19.5)
    explore: ExploreWords = field(default_factory=ExploreWords)  # only `explore` reads it (§25.5)

    def words(self, app_bundle: str | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """(block, confirm) words in force for an app."""
        own = self.apps.get(app_bundle or "", AppWords())
        return (self.block if own.block is None else own.block,
                self.confirm if own.confirm is None else own.confirm)

    def check(self, app_bundle: str | None, element_name: str | None) -> tuple[str, str] | None:
        """("blocked" | "confirm", the policy word) for a touch on an element named `element_name` (its label or
        text) in the app `app_bundle`, or None. A block comes before a confirm."""
        if not element_name:
            return None
        block, confirm = self.words(app_bundle)
        for kind, words in ((BLOCKED, block), (CONFIRM, confirm)):
            for word in words:
                if _matches(word, element_name):
                    return kind, word
        return None

    def blocks_tool(self, tool: str, app_bundle: str | None) -> str | None:
        """Does the block list in force for an app hold the name of a whole tool (`network`)? "app" when it is the
        app's own list that does, "global" when it is the global one, else None. (The whole word, ignoring case.)"""
        block, _ = self.words(app_bundle)
        if not any(w.strip().casefold() == tool.casefold() for w in block):
            return None
        own = self.apps.get(app_bundle or "")
        return "app" if own is not None and own.block is not None else "global"

    def to_obj(self) -> dict:
        """The policy as the JSON object `parse` reads."""
        apps = {bundle: {k: list(v) for k, v in (("confirm", w.confirm), ("block", w.block)) if v is not None}
                for bundle, w in self.apps.items()}
        obj = {"version": VERSION, "confirm": list(self.confirm), "block": list(self.block), "apps": apps}
        if self.explore.skip or self.explore.submit:
            obj["explore"] = {k: list(v) for k, v in (("skip", self.explore.skip), ("submit", self.explore.submit)) if v}
        if self.file is not None:  # (to the daemon: the grants are read from the file itself, never from a header)
            obj.update(version=2, file=self.file)
        return obj

    def dumps(self) -> str:
        """One line of ASCII JSON: fit for an HTTP header."""
        return json.dumps(self.to_obj(), separators=(",", ":"), ensure_ascii=True)


DEFAULT = Policy(confirm=DEFAULT_CONFIRM)
NONE = Policy()  # `--no-policy`: the same as {"confirm": [], "block": []}


def _matches(word: str, text: str) -> bool:
    """Is `word` (one word or a phrase) in `text` as whole words, ignoring case? `Delete` is in `Delete trip` and
    not in `Deleted`; the words of a phrase may be parted by any run of spaces."""
    parts = word.split()
    if not parts:
        return False
    pattern = r"(?<!\w)" + r"\s+".join(re.escape(p) for p in parts) + r"(?!\w)"
    return re.search(pattern, text, re.IGNORECASE) is not None


def _words(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{where} must be a list of words")
    for word in value:
        if not isinstance(word, str) or not word.strip():
            raise ValueError(f"{where} must hold only non-empty strings, not {word!r}")
    return tuple(" ".join(w.split()) for w in value)


def _unknown(obj: dict, allowed: tuple[str, ...], where: str) -> None:
    extra = sorted(set(obj) - set(allowed))
    if extra:
        raise ValueError(f"unknown key {extra[0]!r} in {where}; the keys are {', '.join(allowed)}")


def parse(obj: object, *, header: bool = False) -> Policy:
    """A policy from its JSON object (docs/AGENT-TOOLS.md 11.1, 19.1). Raises ValueError, saying what is wrong. A
    grant that can't load doesn't make the policy fail: it is refused alone (grants.parse_grants). `header`: the
    object came from the MCP server in a header, which may name the policy file (`file`) the grants are read from."""
    if not isinstance(obj, dict):
        raise ValueError("the policy must be a JSON object")
    _unknown(obj, _KEYS + (("file",) if header else ()), "the policy")
    version = obj.get("version", VERSION)
    if isinstance(version, bool) or version not in VERSIONS:
        raise ValueError(f"version must be 1 or 2, not {obj['version']!r}")
    if "grants" in obj and version != 2:
        raise ValueError("grants need version 2 (a version 1 file has no grants)")
    file = obj.get("file") if header else None
    if file is not None and (not isinstance(file, str) or not file or version != 2):
        raise ValueError("file must be the policy file's path, in a version 2 policy")
    apps_obj = obj.get("apps", {})
    if not isinstance(apps_obj, dict):
        raise ValueError("apps must be an object of bundle id: {confirm, block}")
    apps: dict[str, AppWords] = {}
    for bundle, own in apps_obj.items():
        if not isinstance(own, dict):
            raise ValueError(f'apps."{bundle}" must be an object with confirm and block lists')
        _unknown(own, _APP_KEYS, f'apps."{bundle}"')
        apps[bundle] = AppWords(*(_words(own[k], f'apps."{bundle}".{k}') if k in own else None for k in _APP_KEYS))
    explore = obj.get("explore", {})
    if not isinstance(explore, dict):
        raise ValueError('explore must be an object like {"skip": ["Archive"]}')
    # (a key inside `explore` this tool doesn't know is ignored: a newer file may hold one, §25.5)
    words = ExploreWords(*(_words(explore[k], f"explore.{k}") if k in explore else () for k in _EXPLORE_KEYS))
    return Policy(_words(obj.get("confirm", []), "confirm"), _words(obj.get("block", []), "block"), apps, file, words)


def loads(text: str, *, header: bool = False) -> Policy:
    """A policy from JSON text (`header`: the one the MCP server sends the daemon). ValueError when it isn't JSON or
    isn't a policy."""
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError) as e:
        raise ValueError(f"the policy isn't JSON: {e}") from None
    return parse(obj, header=header)


def load(path: str | Path) -> Policy:
    """`--policy FILE`. ValueError, naming the file, when it can't be read or isn't a policy."""
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        raise ValueError(f"can't read the policy file {path}: {getattr(e, 'strerror', None) or e}") from None
    try:
        rules = loads(text)
    except ValueError as e:
        raise ValueError(f"the policy file {path}: {e}") from None
    if json.loads(text).get("version") == 2:  # its grants are read from the file, again at every touch
        rules = Policy(rules.confirm, rules.block, rules.apps, str(Path(path).expanduser().resolve()), rules.explore)
    return rules

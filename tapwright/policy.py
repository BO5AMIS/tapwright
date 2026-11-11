"""The safety policy (docs/AGENT-TOOLS.md section 11): words on a button or switch that make a touch need a person's
OK (`confirm`) or never happen (`block`). A word matches the touched element's own label or text, whole words,
ignoring case; testIDs and accessibility identifiers are never matched. Pure logic: no phone, no network."""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

VERSION = 1
BLOCKED, CONFIRM = "blocked", "confirm"
DEFAULT_CONFIRM = ("Buy", "Purchase", "Subscribe", "Pay", "Delete", "Remove", "Send", "Post", "Publish",
                   "Sign out", "Log out", "Erase")
_KEYS = ("version", "confirm", "block", "apps")
_APP_KEYS = ("confirm", "block")


@dataclass(frozen=True)
class AppWords:
    """One app's own lists. A list left out (None) stays the global one; an empty list turns that class off."""
    confirm: tuple[str, ...] | None = None
    block: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Policy:
    confirm: tuple[str, ...] = ()
    block: tuple[str, ...] = ()
    apps: dict[str, AppWords] = field(default_factory=dict)  # by bundle id; an app's lists replace the global ones

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

    def to_obj(self) -> dict:
        """The policy as the JSON object `parse` reads."""
        apps = {bundle: {k: list(v) for k, v in (("confirm", w.confirm), ("block", w.block)) if v is not None}
                for bundle, w in self.apps.items()}
        return {"version": VERSION, "confirm": list(self.confirm), "block": list(self.block), "apps": apps}

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


def parse(obj: object) -> Policy:
    """A policy from its JSON object (docs/AGENT-TOOLS.md 11.1). Raises ValueError, saying what is wrong."""
    if not isinstance(obj, dict):
        raise ValueError("the policy must be a JSON object")
    _unknown(obj, _KEYS, "the policy")
    if obj.get("version", VERSION) != VERSION:
        raise ValueError(f"version must be {VERSION}, not {obj['version']!r}")
    apps_obj = obj.get("apps", {})
    if not isinstance(apps_obj, dict):
        raise ValueError("apps must be an object of bundle id: {confirm, block}")
    apps: dict[str, AppWords] = {}
    for bundle, own in apps_obj.items():
        if not isinstance(own, dict):
            raise ValueError(f'apps."{bundle}" must be an object with confirm and block lists')
        _unknown(own, _APP_KEYS, f'apps."{bundle}"')
        apps[bundle] = AppWords(*(_words(own[k], f'apps."{bundle}".{k}') if k in own else None for k in _APP_KEYS))
    return Policy(_words(obj.get("confirm", []), "confirm"), _words(obj.get("block", []), "block"), apps)


def loads(text: str) -> Policy:
    """A policy from JSON text (the header). ValueError when it isn't JSON or isn't a policy."""
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError) as e:
        raise ValueError(f"the policy isn't JSON: {e}") from None
    return parse(obj)


def load(path: str | Path) -> Policy:
    """`--policy FILE`. ValueError, naming the file, when it can't be read or isn't a policy."""
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        raise ValueError(f"can't read the policy file {path}: {getattr(e, 'strerror', None) or e}") from None
    try:
        return loads(text)
    except ValueError as e:
        raise ValueError(f"the policy file {path}: {e}") from None

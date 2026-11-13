"""Places: a screen the tool can recognise again (AGENT-TOOLS §18.1), and the names they go by.

A place's key is a signature, built only from what doesn't change with content or language: the identifiers of the
elements that aren't list rows, the kinds of the top-level containers, the selected tab, and what lies on top (a
sheet or an alert). The keyboard isn't counted. `parts` keeps those pieces, so two reads can be compared part by
part.

Never in a key, a name or the map file: a list row's text, a param's value, typed text, anything from a secure field.
A navigation title is the one piece of content that is looked at: it breaks a tie between two places. It never names
one (1.13.2): a place is `place_<n>` until a person names it. In the map it is kept only as an HMAC (`title_hash`), per
language.

The functions here work on the map as plain JSON (the `agent-tools-map` of §18.9, built by `empty_map`), so they are
testable without a file or a phone. mapfile.py reads and writes it.
"""
import hashlib
import hmac
import json
import re
import secrets
import unicodedata
from dataclasses import dataclass

from . import names, ui

# Two reads are the same place when the parts that agree weigh at least this much of the whole (a starting value, to
# be tuned on the phone: AGENT-TOOLS §18.1 lets each tool tune its own). The weights: the ids are what tells screens
# apart, the kinds and the tab say which family a screen belongs to, and a layer on top is another place outright.
THRESHOLD = 0.8
WEIGHTS = {"ids": 0.5, "kinds": 0.2, "tab": 0.15, "layer": 0.15}
IDS_AGREE = 0.6  # the ids "agree" when this share of both reads' ids (Jaccard) are the same ones
TIE = 0.01  # similarities this close are a tie, and the title decides
HASH_CHARS = 16  # a title's HMAC, cut to this many hex characters (§18.9)
KEY_BYTES = 16  # the map's own random key for those HMACs
NAME_MAX = 60
FORMAT, VERSION = "agent-tools-map", 1
_NAME = re.compile(r"^[a-z0-9_]+$")
_NUMBERED = re.compile(r"^place_(\d+)$")


@dataclass(frozen=True)
class PlaceRead:
    """What one screen read says about where the app is. `title` is plain text, in memory only (it is never written);
    `lang` says which language bucket its hash goes under."""
    key: dict
    title: str
    lang: str


@dataclass(frozen=True)
class Visit:
    """What `visit` did: the place's name, whether it is new, and its earlier name when a title just named it."""
    name: str
    new: bool
    renamed_from: str | None
    score: float


@dataclass(frozen=True)
class Named:
    """What `set_name` did: the name the place has now, and whether the one asked for was taken (so it got `_2`)."""
    name: str
    taken: bool


# ---- the signature ----

def _tab(tree: ui.Tree) -> str | None:
    """The selected tab's identifier, else its position (1-based, `#2`): a tab's label is text a language changes."""
    tabs = [e for e in tree.items if e.role == "tab"]
    for position, tab in enumerate(tabs, 1):
        if tab.selected:
            return tab.identifier or f"#{position}"
    return None


def parts_of(tree: ui.Tree) -> dict:
    layer = "alert" if tree.alert is not None else "sheet" if tree.behind else None
    return {"ids": list(tree.structure.ids), "kinds": list(tree.structure.kinds), "tab": _tab(tree), "layer": layer}


def signature(parts: dict) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:HASH_CHARS]


_SCRIPTS = (("ar", "؀", "ۿ"), ("ar", "ݐ", "ݿ"), ("he", "֐", "׿"),
            ("ru", "Ѐ", "ӿ"), ("el", "Ͱ", "Ͽ"), ("hi", "ऀ", "ॿ"),
            ("th", "฀", "๿"), ("ko", "가", "힯"), ("ja", "぀", "ヿ"),
            ("zh", "一", "鿿"))


def language_of(title: str, asked: str | None = None) -> str:
    """The language bucket a title's hash is kept under. The language the app was started with (open_app's
    `language`, `fr-CA` is `fr`) when the daemon knows it; else the title's script (`ar`, `he`, `ru`...); Latin text is
    `latin`, since its language can't be told from the letters; nothing to go by is `und`."""
    if asked and asked.strip():
        return re.split(r"[-_]", asked.strip().lower())[0] or "und"
    for ch in title:
        for lang, low, high in _SCRIPTS:
            if low <= ch <= high:
                return lang
        if ch.isalpha():
            return "latin"
    return "und"


def has_structure(tree: ui.Tree) -> bool:
    """Does the screen have anything to recognise it by (a container or an identifier)? One that has none (a game's
    canvas, an app still loading) is no place: every such screen would be the same one."""
    return bool(tree.structure.kinds or tree.structure.ids)


def recognisable(tree: ui.Tree) -> bool:
    """Could a later read tell this screen from the others of its kinds? Only by an identifier or a title (`locate`): a
    screen with only container kinds would be saved as a new place at every visit and never found again."""
    return has_structure(tree) and bool(tree.structure.ids or " ".join(tree.structure.title.split()))


def read_place(tree: ui.Tree, lang: str | None = None) -> PlaceRead:
    """The place a screen read shows: its signature key, and its title with the language bucket. `lang`: the
    language the app was started with, when it is known."""
    parts = parts_of(tree)
    title = " ".join(tree.structure.title.split())
    return PlaceRead({"signature": signature(parts), "parts": parts}, title, language_of(title, lang))


def similarity(a: dict, b: dict) -> float:
    """How much of two reads' parts agree, 0 to 1 (WEIGHTS). A sheet or alert over one and not the other is another
    place outright: 0."""
    if a.get("layer") != b.get("layer"):
        return 0.0
    ids_a, ids_b = set(a.get("ids", ())), set(b.get("ids", ()))
    union = ids_a | ids_b
    agree = {"ids": not union or len(ids_a & ids_b) / len(union) >= IDS_AGREE,
             "kinds": a.get("kinds") == b.get("kinds"), "tab": a.get("tab") == b.get("tab"), "layer": True}
    return sum(WEIGHTS[part] for part, ok in agree.items() if ok)


def same_place(a: dict, b: dict, threshold: float = THRESHOLD) -> bool:
    return similarity(a, b) >= threshold


# ---- titles and names ----

def _flat(title: str) -> str:
    return " ".join(unicodedata.normalize("NFC", title).split())


def title_hash(title_key: str, lang: str, title: str) -> str:
    """A title as it is kept in the map: HMAC-SHA-256 with the map's random key, over the language and the title,
    cut to 16 hex characters. Equality is all a tie-break needs; no title can be read back."""
    mac = hmac.new(bytes.fromhex(title_key), f"{lang}\0{_flat(title)}".encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()[:HASH_CHARS]


def clean_name(text: str) -> str:
    """A name a person gave, as `[a-z0-9_]`: lower case, spaces and dashes as `_`. ValueError when it can't be one."""
    name = re.sub(r"[\s-]+", "_", text.strip().lower())
    if not name or len(name) > NAME_MAX or not _NAME.match(name):
        raise ValueError(f"a place's name is ASCII letters, digits and _ (at most {NAME_MAX}), not {text!r}")
    return name


def empty_map(app: str) -> dict:
    return {"format": FORMAT, "version": VERSION, "app": app, "titleKey": secrets.token_hex(KEY_BYTES),
            "places": {}, "roads": [], "routes": {}, "reflexes": []}


def key_id(key: dict) -> str:
    """What says two places' keys are the same place, for merging maps (§18.9): a signature, else the key itself."""
    if isinstance(key.get("signature"), str):
        return "sig:" + key["signature"]
    return "key:" + json.dumps(key, sort_keys=True, separators=(",", ":"))


def _unique(data: dict, base: str, besides: str | None = None) -> str:
    taken = set(data["places"]) - {besides}
    name, n = base, 2
    while name in taken:
        name, n = f"{base}_{n}", n + 1
    return name


def _next_number(data: dict) -> int:
    numbers = [int(m.group(1)) for name in data["places"] if (m := _NUMBERED.match(name))]
    return max(numbers, default=0) + 1


# ---- finding and saving places ----

def locate(data: dict, read: PlaceRead) -> tuple[str | None, float]:
    """The saved place this read is, and how well it fits; (None, 0.0) when it is none. The best fit at or over the
    threshold wins; fits within TIE of each other are told apart by the navigation title, then by the newest.
    A screen with no identifiers at all is only the same place when its title is the same too: nothing else could
    tell it from any other screen of the same kinds."""
    wanted = title_hash(data["titleKey"], read.lang, read.title) if read.title else None
    fits: list[tuple[float, bool, str, str]] = []
    for name, place in data["places"].items():
        parts = place["key"].get("parts")
        if not isinstance(place["key"].get("signature"), str) or not isinstance(parts, dict):
            continue  # another tool's kind of key
        score = similarity(read.key["parts"], parts)
        if score < THRESHOLD:
            continue
        has_title = wanted is not None and place.get("titles", {}).get(read.lang) == wanted
        if not read.key["parts"]["ids"] and not parts.get("ids") and not has_title:
            continue
        fits.append((score, has_title, str(place.get("last", "")), name))
    if not fits:
        return None, 0.0
    best = max(score for score, *_ in fits)
    close = [f for f in fits if best - f[0] < TIE]
    close.sort(key=lambda f: (f[1], f[2]), reverse=True)
    return close[0][3], close[0][0]


def visit(data: dict, read: PlaceRead, now: str, arrived: bool = True) -> Visit:
    """The screen the agent is on: the saved place it is, or a new one (`place_<n>`). `arrived`: the agent came here
    from somewhere else (a look at the same screen again is not a second visit). A place stays `place_<n>` until a
    person names it: a title never names it (AGENT-TOOLS 1.13.2, §18.1)."""
    name, score = locate(data, read)
    if name is None:
        name = _unique(data, f"place_{_next_number(data)}")
        titles = {read.lang: title_hash(data["titleKey"], read.lang, read.title)} if read.title else {}
        data["places"][name] = {"key": read.key, "link": None, "titles": titles, "last": now}
        return Visit(name, True, None, 1.0)
    place = data["places"][name]
    place["last"] = now
    if arrived and read.title:  # the latest title's hash, for the tie-break; it never names the place (1.13.2)
        place.setdefault("titles", {})[read.lang] = title_hash(data["titleKey"], read.lang, read.title)
    return Visit(name, False, None, score)


def _move(data: dict, old: str, new: str) -> None:
    """Rename a place everywhere the map names it: the place, its roads, the reflexes that fire at it."""
    if old == new:
        return
    data["places"] = {(new if n == old else n): p for n, p in data["places"].items()}
    for road in data["roads"]:
        for end in ("from", "to"):
            if road.get(end) == old:
                road[end] = new
    for reflex in data["reflexes"]:
        if reflex.get("at") == old:
            reflex["at"] = new


def rename(data: dict, old: str, new: str) -> None:
    """Rename without any check (the caller has chosen a free name)."""
    _move(data, old, new)


def set_name(data: dict, current: str, wanted: str) -> Named:
    """A person's name for a place (`map {"name": ...}`): cleaned to `[a-z0-9_]`, `_2` if another place has it. It is
    kept: a title never replaces it. ValueError when the text can't be a name."""
    base = clean_name(wanted)
    if current not in data["places"]:
        raise ValueError(f"no place called {current}")
    # (1.13.1, item 5) capitals that would only make it another place's name are refused, not folded into `_2`
    as_typed = re.sub(r"[\s-]+", "_", wanted.strip())
    if as_typed != base and (other := names.twin(as_typed, set(data["places"]) - {current})) is not None:
        raise ValueError(names.twin_message("place", as_typed, other))
    name = _unique(data, base, besides=current)
    _move(data, current, name)
    data["places"][name]["named"] = True
    return Named(name, name != base)


def forget(data: dict, name: str) -> bool:
    """Drop a place, the roads that leave or reach it and the reflexes that fire at it. False when there is none.
    (A saved route that starts or ends there is a file of its own: it is left, and says where it was meant to go.)"""
    if name not in data["places"]:
        return False
    del data["places"][name]
    data["roads"] = [r for r in data["roads"] if name not in (r.get("from"), r.get("to"))]
    data["reflexes"] = [r for r in data["reflexes"] if r.get("at") != name]
    return True

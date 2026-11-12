"""`media` (docs/AGENT-TOOLS.md §17.6), the computer's side: test pictures for an app's photo picker. It reads the
pictures from disk, keeps a ledger of what this daemon put in the phone's photo library (so `remove` deletes only
that, even after a restart: ~/.tapwright/media.json), and words the answers. The phone's side is the runner's
`/wda/tw/photos/*` routes (Session.photos_*)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import store

EXTENSIONS = (".jpg", ".jpeg", ".png", ".heic")
MAX_BYTES = 20 * 1024 * 1024  # per picture
MAX_FILES = 20  # per call
MAX_TOTAL = 40 * 1024 * 1024  # per call, all pictures together (the runner's JSON body is a third bigger)
MIN_TW_VERSION = 3  # the runner route version that has the photo routes
OLD_RUNNER = ("this phone's runner is older than the photo routes (tw version < 3); update the runner")
NEED_CONFIRM = ("media changes the photo library on the user's phone. Ask your user first whether pictures may be "
                "added (and whether to remove them afterwards), then call again with confirm: true.")


def ledger_path() -> Path:
    return store.HOME / "media.json"


def read_pictures(paths: list[str]) -> list[tuple[str, bytes]]:
    """(file name, bytes) of each picture. ValueError, naming the file, when one isn't there, isn't a jpeg, png or
    heic, or is over 20 MB."""
    if not paths:
        raise ValueError("add needs at least one path")
    if len(paths) > MAX_FILES:
        raise ValueError(f"add takes at most {MAX_FILES} pictures at a time, not {len(paths)}")
    out: list[tuple[str, bytes]] = []
    total = 0
    for raw in paths:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"add holds paths of pictures, not {raw!r}")
        path = Path(raw).expanduser()
        if path.suffix.lower() not in EXTENSIONS:
            raise ValueError(f"{path.name}: only jpeg, png and heic pictures can be added")
        try:
            size = path.stat().st_size
        except OSError:
            raise ValueError(f"{raw}: no such file on this computer") from None
        if not path.is_file():
            raise ValueError(f"{raw}: not a file")
        if size > MAX_BYTES:
            raise ValueError(f"{path.name}: {size / 1048576:.1f} MB is over the {MAX_BYTES // 1048576} MB limit")
        total += size
        if total > MAX_TOTAL:
            raise ValueError(f"{path.name}: the pictures together are over the {MAX_TOTAL // 1048576} MB limit for one call")
        try:
            out.append((path.name, path.read_bytes()))
        except OSError as e:
            raise ValueError(f"{raw}: can't be read ({e.strerror or e})") from None
    return out


# ---- the ledger: what this daemon added, per client ----
def load() -> dict[str, list[dict]]:
    """{client id: [{"id": local identifier, "name": file name}]}. Empty when there is no file or it is broken."""
    try:
        data = json.loads(ledger_path().read_text(encoding="utf-8")).get("clients", {})
    except (OSError, ValueError, AttributeError):
        return {}
    if not isinstance(data, dict):
        return {}
    clean: dict[str, list[dict]] = {}
    for client, entries in data.items():
        good = [{"id": e["id"], "name": str(e.get("name", ""))} for e in entries
                if isinstance(e, dict) and isinstance(e.get("id"), str)] if isinstance(entries, list) else []
        if good:
            clean[str(client)] = good
    return clean


def save(clients: dict[str, list[dict]]) -> None:
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps({"clients": clients}, indent=1), encoding="utf-8")
    os.replace(temporary, path)


def record(client: str, identifiers: list[str], names: list[str]) -> None:
    clients = load()
    clients.setdefault(client, []).extend({"id": i, "name": n} for i, n in zip(identifiers, names))
    save(clients)


def ledger_ids() -> set[str]:
    """Every identifier in the ledger now (the daemon takes them at its start: they are from an earlier run)."""
    return {e["id"] for entries in load().values() for e in entries}


def held_by(client: str, earlier: set[str] | frozenset[str] = frozenset(), everything: bool = False) -> tuple[list[dict], bool]:
    """What `remove` takes for this client: its own pictures, plus the ones recorded before this daemon started
    (`earlier`: their identifiers; True in the answer says some were). `everything` (remove: "all") takes every picture
    in the ledger, another live agent's too."""
    clients = load()
    if everything:
        return [e for entries in clients.values() for e in entries], False
    own = list(clients.get(client, ()))
    mine = {e["id"] for e in own}
    old = [e for c, entries in clients.items() if c != client for e in entries if e["id"] in earlier and e["id"] not in mine]
    return own + old, bool(old)


def drop(identifiers: list[str]) -> None:
    gone = set(identifiers)
    clients = {c: [e for e in entries if e["id"] not in gone] for c, entries in load().items()}
    save({c: entries for c, entries in clients.items() if entries})


# ---- the words ----
def pictures(n: int) -> str:
    return f"{n} picture{'s' if n != 1 else ''}"


def added_text(names: list[str]) -> str:
    return (f"Added {pictures(len(names))} to the photo library: {', '.join(names)}. "
            "They stay until removed (media with remove: true).")


def removed_text(names: list[str], asked: int) -> str:
    if len(names) == asked:
        return f"Removed {pictures(len(names))} from the photo library: {', '.join(names)}."
    return (f"Removed {len(names)} of {pictures(asked)} from the photo library: {', '.join(names)}. "
            "The rest were already gone, or the person declined the delete on the phone.")


def by_hand_text(names: list[str], why: str) -> str:
    return (f"Couldn't remove them: {why} Delete {'it' if len(names) == 1 else 'them'} by hand in Photos > "
            f"Recently Added: {', '.join(names)}.")

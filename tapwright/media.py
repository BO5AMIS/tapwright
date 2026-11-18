"""`media` (docs/AGENT-TOOLS.md §17.6), the computer's side: test pictures and videos for an app's photo picker. It
reads them from disk (a video only its first bytes and its hash: the session copies it to the phone from the file), keeps a ledger of what this daemon put in the phone's photo library (so `remove` deletes only
that, even after a restart: ~/.tapwright/media.json), and words the answers. The phone's side is the runner's
`/wda/tw/photos/*` routes (Session.photos_*)."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import NamedTuple

from . import store

EXTENSIONS = (".jpg", ".jpeg", ".png", ".heic")
MAX_BYTES = 20 * 1024 * 1024  # per picture
MAX_FILES = 20  # per call
MAX_TOTAL = 40 * 1024 * 1024  # per call, all pictures together (the runner's JSON body is a third bigger)
MIN_TW_VERSION = 3  # the runner route version that has the photo routes
OLD_RUNNER = ("this phone's runner is older than the photo routes (tw version < 3); update the runner")
# Videos (1.7.9, runner route version 7). A 50 s 1080p clip is 20-100 MB, so one clip may be 200 MB (about 1.5 minutes
# of 1080p at 30 fps, or 30 s of 4K) and a call 400 MB: over Wi-Fi at 5-10 MB/s that is a minute or so of copying, and
# the runner's import wait (a second per 4 MB on top of 2 minutes) stays under 10 minutes. 5 clips at a time: a picker
# test rarely needs more, and each one is a copy to the phone.
VIDEO_EXTENSIONS = (".mp4", ".mov", ".m4v")
MAX_VIDEO_BYTES = 200 * 1024 * 1024  # per video
MAX_VIDEO_TOTAL = 400 * 1024 * 1024  # per call, all videos together
MAX_VIDEOS = 5  # per call (pictures and videos together are still at most MAX_FILES)
VIDEO_TW_VERSION = 7  # the runner route version that adds videos
# the ftyp brands of an mp4, a QuickTime movie or an m4v (an 'ftyp' alone is also HEIC's: the brand tells them apart)
VIDEO_BRANDS = frozenset({b"isom", b"iso2", b"iso3", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"qt  ",
                          b"M4V ", b"M4VH", b"M4VP", b"MSNV", b"dash", b"f4v ", b"XAVC",
                          b"3gp4", b"3gp5", b"3gp6", b"3g2a", b"mmp4"})  # (3GPP and phone cameras: review R179 L6)
# a vendor's own major brand is still a video when its compatible brands (after the minor version) name an mp4 one or
# QuickTime's (Apple lets a QuickTime-compatible file prefer another major brand: CodeRabbit, PR #19)
COMPATIBLE_VIDEO = frozenset({b"isom", b"mp41", b"mp42", b"qt  "})
# an old QuickTime file may start with one of these atoms and no ftyp at all
QUICKTIME_ATOMS = frozenset({b"moov", b"mdat", b"wide", b"free", b"skip", b"pnot"})
# a video's first bytes kept to tell what it is: the whole ftyp box, every compatible brand in it (CodeRabbit, PR #19:
# 64 bytes cut the list at 12 brands), up to this cap (about 1000 brands; real files list a handful)
FTYP_MAX = 4096
STAGE_ROOT = "tmp/tapwright-media"  # where a video is copied in the runner's container (the runner takes files only there)
CHUNK = 1024 * 1024


def old_runner_for_video(version: int) -> str:
    return (f"this phone's runner can't add videos (its route version is {version}; videos need {VIDEO_TW_VERSION}): "
            "run `tapwright setup --force` on this computer to install the current runner, then call media again. "
            "Pictures can still be added.")


class Stamp(NamedTuple):
    """What a file was when it was checked and hashed: the copy to the phone refuses a file that changed since (it
    grew, or another file was put in its place: review R179 L5)."""
    size: int
    ino: int
    mtime_ns: int

    @classmethod
    def of(cls, st: os.stat_result) -> "Stamp":
        return cls(st.st_size, st.st_ino, st.st_mtime_ns)


class Video(NamedTuple):
    """A video to add: read from its file when it is copied to the phone, never held in memory whole."""
    name: str
    path: Path
    size: int
    digest: str  # sha256 of its bytes: the same clip again is the same add (Daemon._media_adding)
    stamp: "Stamp | None" = None  # the file as it was hashed; the copy refuses it if it changed since


NEVER_ASKS_FULL = ("the fixture-removal API needs full photo-library access; fixture import requests only "
                   "add-only access. Use the Photos UI to manage the library.")
NEED_CONFIRM = ("media changes the photo library on the user's phone. Ask your user first whether pictures may be "
                "added (and whether to remove them afterwards), then call again with confirm: true.")


def ledger_path() -> Path:
    return store.HOME / "media.json"


def read_pictures(paths: list[str]) -> list[tuple[str, bytes]]:
    """(file name, bytes) of each picture: read_media, pictures only."""
    out = read_media(paths)
    for item in out:
        if isinstance(item, Video):
            raise ValueError(f"{item.name}: only jpeg, png and heic pictures can be added here")
    return [item for item in out if not isinstance(item, Video)]


def read_media(paths: list[str]) -> "list[tuple[str, bytes] | Video]":
    """Each item in order: a picture as (file name, bytes), a video as a Video. ValueError, naming the file, when one
    isn't there, isn't a jpeg, png, heic, mp4, mov or m4v (by its first bytes too), or is over its limit."""
    if not paths:
        raise ValueError("add needs at least one path")
    if len(paths) > MAX_FILES:
        raise ValueError(f"add takes at most {MAX_FILES} pictures and videos at a time, not {len(paths)}")
    out: list[tuple[str, bytes] | Video] = []
    total = 0
    videos = video_total = 0
    for raw in paths:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"add holds paths of pictures, not {raw!r}")
        if raw.strip().startswith(("\\\\", "//")):  # a network path would make this computer connect to that host
            raise ValueError(f"{raw}: pictures must be on this computer, not on a network path")
        path = Path(raw).expanduser()
        suffix = path.suffix.lower()
        if suffix not in EXTENSIONS and suffix not in VIDEO_EXTENSIONS:
            raise ValueError(f"{path.name}: only jpeg, png and heic pictures and mp4, mov and m4v videos can be added")
        if path.is_symlink():
            raise ValueError(f"{path.name}: a link isn't followed; give the picture's own path")
        try:
            size = path.stat().st_size
        except OSError:
            raise ValueError(f"{raw}: no such file on this computer") from None
        if not path.is_file():
            raise ValueError(f"{raw}: not a file")
        if suffix in VIDEO_EXTENSIONS:
            videos += 1
            if videos > MAX_VIDEOS:
                raise ValueError(f"add takes at most {MAX_VIDEOS} videos at a time")
            if size > MAX_VIDEO_BYTES:
                raise ValueError(f"{path.name}: {size / 1048576:.1f} MB is over the {MAX_VIDEO_BYTES // 1048576} MB "
                                 "limit for a video")
            video_total += size
            if video_total > MAX_VIDEO_TOTAL:
                raise ValueError(f"{path.name}: the videos together are over the {MAX_VIDEO_TOTAL // 1048576} MB limit "
                                 "for one call")
            out.append(_video(raw, path, size))
            continue
        if size > MAX_BYTES:
            raise ValueError(f"{path.name}: {size / 1048576:.1f} MB is over the {MAX_BYTES // 1048576} MB limit")
        total += size
        if total > MAX_TOTAL:
            raise ValueError(f"{path.name}: the pictures together are over the {MAX_TOTAL // 1048576} MB limit for one call")
        try:
            data = path.read_bytes()
        except OSError as e:
            raise ValueError(f"{raw}: can't be read ({e.strerror or e})") from None
        if not looks_like(path.suffix.lower(), data):
            raise ValueError(f"{path.name}: the file isn't a {path.suffix.lstrip('.').lower()} picture (its first bytes say otherwise)")
        out.append((path.name, data))
    return out


def _video(raw: str, path: Path, size: int) -> Video:
    """The video at `path`, its first bytes checked and its bytes hashed in chunks (never all in memory)."""
    digest = hashlib.sha256()
    head = b""
    try:
        with open(path, "rb") as f:
            before = Stamp.of(os.fstat(f.fileno()))
            while chunk := f.read(CHUNK):
                if len(head) < FTYP_MAX:
                    head += chunk[:FTYP_MAX - len(head)]
                digest.update(chunk)
            after = Stamp.of(os.fstat(f.fileno()))
    except OSError as e:
        raise ValueError(f"{raw}: can't be read ({e.strerror or e})") from None
    if before != after or after.size != size:
        raise ValueError(f"{path.name}: the file changed while it was read (is it still being written?); call media "
                         "again once it is complete")
    suffix = path.suffix.lower()
    if not looks_like(suffix, head):
        raise ValueError(f"{path.name}: the file isn't a {suffix.lstrip('.')} video (its first bytes say otherwise)")
    return Video(path.name, path, size, digest.hexdigest(), after)


def looks_like(suffix: str, data: bytes) -> bool:
    """Do the file's first bytes fit its extension: jpeg FF D8 FF, png 89 'PNG', heic an 'ftyp' box, a video an
    'ftyp' box with a video brand (mp4, QuickTime, m4v), or an old .mov's first atom."""
    if suffix in (".jpg", ".jpeg"):
        return data[:3] == b"\xff\xd8\xff"
    if suffix == ".png":
        return data[:4] == b"\x89PNG"
    if suffix in VIDEO_EXTENSIONS:  # an 'ftyp' box with a video brand; an old .mov may start with another atom
        if data[4:8] == b"ftyp":
            if data[8:12] in VIDEO_BRANDS:
                return True
            # compatible brands: byte 16 to the box's end (never a later atom's bytes), as far as `data` holds
            end = min(len(data), int.from_bytes(data[:4], "big"))
            return any(data[i:i + 4] in COMPATIBLE_VIDEO for i in range(16, end - 3, 4))
        return suffix == ".mov" and len(data) >= 8 and data[4:8] in QUICKTIME_ATOMS
    return data[4:8] == b"ftyp"


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


def items(n_pictures: int, n_videos: int = 0) -> str:
    """'3 pictures', '1 video', '1 picture and 2 videos'."""
    parts = ([pictures(n_pictures)] if n_pictures or not n_videos else []) + \
        ([f"{n_videos} video{'s' if n_videos != 1 else ''}"] if n_videos else [])
    return " and ".join(parts)


def added_text(names: list[str], videos: int = 0) -> str:
    return (f"Added {items(len(names) - videos, videos)} to the photo library: {', '.join(names)}. "
            "They stay until removed (media with remove: true).")


def is_video_name(name: str) -> bool:
    return Path(name).suffix.lower() in VIDEO_EXTENSIONS


def named(names: list[str]) -> str:
    """'2 pictures', '1 picture and 1 video', by the names' extensions (the ledger keeps names, not kinds)."""
    videos = sum(is_video_name(n) for n in names)
    return items(len(names) - videos, videos)


def removed_text(names: list[str], asked: list[str]) -> str:
    """`names`: what was removed; `asked`: what was to be removed."""
    if len(names) == len(asked):
        return f"Removed {named(names)} from the photo library: {', '.join(names)}."
    return (f"Removed {len(names)} of {named(asked)} from the photo library: {', '.join(names)}. "
            "The rest were already gone, or the person declined the delete on the phone.")


def by_hand_text(names: list[str], why: str) -> str:
    return (f"Couldn't remove them: {why} Remove {'it' if len(names) == 1 else 'them'} through the Photos UI > "
            f"Recently Added: {', '.join(names)}.")

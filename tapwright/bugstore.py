"""The bug report's folder (AGENT-TOOLS 1.16.2 §29.2, §29.5, §29.6): `~/.tapwright/bugs/<app folder>/<bug id>/` with
`report.md`, `report.html`, `repro.json`, `finding.json` and, only when allowed, `picture.jpg`. One bug is one folder: a
later sighting of the same signature updates it (its count, last time, builds, and the two page sections `Reproduced` and
`Seen before`), and never adds another.

Nothing is written before the grep gate (§29.6) has passed every text file it would write: a file that still holds a value
raises `BugGateError` naming the file, never the value, and the folder stays as it was. The newest 100 bugs of an app are
kept (by `seen.last`); a bug just made or updated, or one a check holds, is never the one removed. A bug is read only from
its own app's folder, and only when its folder name is its own id (Tapline 1.8.6's lesson): a folder planted elsewhere is
never listed or read.

Every change of an app's bugs is made under one lock of that app's folder (a lock file, so the command line and the daemon
take turns too), and a bug is read again inside it right before it is written: what changed meanwhile (a sighting, another
check) is kept, and a bug removed meanwhile is not made again (review R185 M5). Every rewrite reads the kept finding and the
script through the redactor again and builds the page anew, so a value added to the vault since is hidden too (L2).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import contextlib
import tempfile
import time
from pathlib import Path
from typing import Callable, Iterator

from . import bug, findings

KEPT = 100  # the newest bugs of an app (§29.2)
SOURCES_KEPT = 50  # the newest sources `seen.sources` keeps (§29.5)
TEXT_FILES = ("report.md", "report.html", "repro.json", "finding.json")
LOCK_FILE = ".lock"  # in an app's bugs folder: one change of its bugs at a time, across processes
LOCK_WAIT, LOCK_POLL, LOCK_STALE = 15.0, 0.02, 60.0  # seconds; a lock older than LOCK_STALE was left by a process that died
HOLD_STALE = 3600.0  # a check's hold on a bug (`.<bug id>.held`) older than this was left behind: retention may remove it


class BugGateError(Exception):
    """A file of the folder would still hold a vault value, a code or a typed value: nothing was written (§29.6). `update`:
    the bug exists already and was left as it was."""

    def __init__(self, file: str, update: bool = False) -> None:
        super().__init__(f"a vault value was found in {file}.")
        self.file = file
        self.update = update


class BugFolderError(Exception):
    """The bug's folder can't be used: it exists but can't be read (a damaged `finding.json`), or the lock of the app's
    bugs stayed taken (review R185 L10). Nothing was written."""


@dataclasses.dataclass(frozen=True)
class Made:
    meta: dict
    folder: Path
    new: bool
    title: str
    finding: dict


@dataclasses.dataclass(frozen=True)
class Kept:
    """A bug as its folder holds it."""
    app: str
    id: str
    folder: Path
    finding: dict
    meta: dict

    @property
    def title(self) -> str:
        return bug.title(self.finding["kind"], self.finding.get("place"), self.finding["got"])


def build_of(finding: dict) -> str:
    return f"{finding['app_version']} ({finding['build']})"


def _write(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    if isinstance(data, bytes):
        temporary.write_bytes(data)
    else:
        temporary.write_text(data, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def _dumps(value: object) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


class BugStore:
    def __init__(self, base: Path, kept: int = KEPT) -> None:
        self.base = Path(base)
        self.kept = kept

    def app_dir(self, app: str) -> Path:
        return self.base / bug.app_folder(app)

    # ---- reading ----

    def read(self, app: str, bug_id: str) -> Kept | None:
        """A kept bug of this app, or None: its folder is its id, inside the app's own folder, and its `finding.json`
        names this app and this id (an identity check on every read)."""
        if not bug.BUG_ID.match(bug_id):
            return None
        app_dir = self.app_dir(app)
        folder = app_dir / bug_id
        try:
            if folder.is_symlink() or not folder.is_dir():
                return None
            if os.path.realpath(folder) != os.path.join(os.path.realpath(app_dir), bug_id):
                return None
            data = json.loads((folder / "finding.json").read_text("utf-8"))
        except (OSError, ValueError, RecursionError):
            return None
        meta = data.get("bug") if isinstance(data, dict) else None
        if not isinstance(meta, dict) or meta.get("id") != bug_id or data.get("app") != app:
            return None
        if not isinstance(meta.get("seen"), dict) or not isinstance(data.get("kind"), str) or not isinstance(data.get("got"), str):
            return None
        finding = {k: v for k, v in data.items() if k != "bug"}
        return Kept(app, bug_id, folder, finding, meta)

    def apps(self) -> list[str]:
        """The apps with a bugs folder, by the app their bugs name (a folder whose bugs name another app holds none)."""
        found: dict[str, None] = {}
        try:
            folders = sorted(p for p in self.base.iterdir() if p.is_dir() and not p.is_symlink())
        except OSError:
            return []
        for folder in folders:
            for one in sorted(folder.iterdir()) if folder.is_dir() else []:
                try:
                    app = json.loads((one / "finding.json").read_text("utf-8")).get("app")
                except (OSError, ValueError, AttributeError, RecursionError):
                    continue
                if isinstance(app, str) and bug.app_folder(app) == folder.name:
                    found[app] = None
                    break
        return list(found)

    def bugs(self, app: str) -> list[Kept]:
        """The bugs of an app, newest first (by `seen.last`)."""
        try:
            names = [p.name for p in self.app_dir(app).iterdir() if bug.BUG_ID.match(p.name)]
        except OSError:
            return []
        kept = [k for k in (self.read(app, n) for n in names) if k is not None]
        return sorted(kept, key=lambda k: str(k.meta["seen"].get("last", "")), reverse=True)

    def holders(self, bug_id: str) -> list[str]:
        """The apps that hold a bug with this id (two apps can: the id holds no app, §29.7)."""
        return [app for app in self.apps() if self.read(app, bug_id) is not None]

    # ---- making ----

    # ---- the lock (review R185 M5) ----

    @contextlib.contextmanager
    def locked(self, app: str) -> Iterator[None]:
        """One change of this app's bugs at a time, in this process and in any other (a lock file beside them)."""
        folder = self.app_dir(app)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / LOCK_FILE
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                break
            except FileExistsError:
                try:
                    if time.time() - path.stat().st_mtime > LOCK_STALE:
                        path.unlink()  # (left behind by a process that died)
                        continue
                except OSError:
                    pass
            except PermissionError:  # (Windows: the lock file is being removed just now)
                pass
            if time.monotonic() > deadline:
                raise BugFolderError(f"the bugs of {app} stayed locked for {LOCK_WAIT:g} s; try again.")
            time.sleep(LOCK_POLL)
        try:
            yield
        finally:
            try:
                path.unlink()
            except OSError:
                pass

    @contextlib.contextmanager
    def hold(self, app: str, bug_id: str) -> Iterator[None]:
        """A check holds the bug while it runs: retention never removes it meanwhile (review R185 M5)."""
        marker = self.app_dir(app) / f".{bug_id}.held"
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(str(os.getpid()), encoding="utf-8")
        except OSError:
            pass
        try:
            yield
        finally:
            try:
                marker.unlink()
            except OSError:
                pass

    def _held(self, app: str, bug_id: str) -> bool:
        try:
            return time.time() - (self.app_dir(app) / f".{bug_id}.held").stat().st_mtime < HOLD_STALE
        except OSError:
            return False

    # ---- making, under the lock ----

    def make(self, *, finding: dict, place_key: dict | None, source: dict, script: dict | None, picture: bytes | None,
             with_picture: bool, extras: dict, reproduced: dict, gate: Callable[[dict[str, str]], str | None],
             picture_source: str | None = None, test_app: bool = False,
             scrub: Callable[[str], str] | None = None) -> Made:
        """Make the bug of `finding` (already redacted, §29.6), or add this sighting to the bug with its signature.
        `source`: `how`, `key` (`<run folder>/<finding id>`, kept in `seen.sources`), and `run`/`finding` when there are.
        `picture`: the picture's bytes while its file exists; a private one goes in only `with_picture`. `test_app`: the app
        is one of the daemon's test apps (review R185 H2: any other app's picture is private, and any export needs
        confirm). `scrub`: the redactor a sighting reads the kept bug through again (L2)."""
        with self.locked(finding["app"]):
            return self._make(finding=finding, place_key=place_key, source=source, script=script, picture=picture,
                              with_picture=with_picture, extras=extras, reproduced=reproduced, gate=gate,
                              picture_source=picture_source, test_app=test_app, scrub=scrub)

    def _make(self, *, finding: dict, place_key: dict | None, source: dict, script: dict | None, picture: bytes | None,
              with_picture: bool, extras: dict, reproduced: dict, gate: Callable[[dict[str, str]], str | None],
              picture_source: str | None, test_app: bool, scrub: Callable[[str], str] | None) -> Made:
        crash = finding.get("crash") or {}
        sig = bug.signature(finding["kind"], place_key, finding["got"], crash.get("reason"), crash.get("frames") or ())
        app = finding["app"]
        bug_id, held = self._place_for(app, sig, place_key, source["key"])
        if held is not None:
            return self._sighting(held, finding, source, reproduced, gate, picture=picture, with_picture=with_picture,
                                  test_app=test_app, scrub=scrub)
        if (self.app_dir(app) / bug_id).exists():
            raise BugFolderError(f"the folder of {bug_id} exists but can't be read (a damaged finding.json?). Move it away "
                                 "or remove it, then report the bug again.")
        meta: dict = {"id": bug_id, "signature": sig, "place_key": place_key,
                      "source": {k: source[k] for k in ("how", "run", "finding") if source.get(k) is not None},
                      "seen": {"first": finding["when"], "last": finding["when"], "count": finding.get("count", 1),
                               "builds": [build_of(finding)], "sources": [source["key"]]},
                      "reproduced": reproduced, "picture_included": False, "test_app": bool(test_app)}
        if picture_source:
            meta["picture_source"] = picture_source
        meta.update({k: v for k, v in extras.items() if v})
        pic = finding.get("picture")
        private = not (isinstance(pic, dict) and pic.get("private") is False) or not test_app
        include = picture is not None and isinstance(pic, dict) and (not private or with_picture)
        if include:
            meta["picture_included"] = True
            meta["picture_file"] = {"private": private, "consent": bool(with_picture and private),
                                    "sha256": hashlib.sha256(picture).hexdigest()}
        files = self._files(finding, meta, script, "picture.jpg" if include else None)
        if (bad := gate(files)) is not None:
            raise BugGateError(bad)
        folder = self.app_dir(app) / bug_id
        self._write_new(folder, files, picture if include else None)
        self._prune(app, keep=bug_id)
        return Made(meta, folder, True, bug.title(finding["kind"], finding.get("place"), finding["got"]), finding)

    def _place_for(self, app: str, sig: str, place_key: dict | None, key: str) -> tuple[str, Kept | None]:
        """The id this finding's bug has, and the bug kept with it already (None: a new one). A finding with no place key is
        never merged (§29.5): it gets `bug_<sig>`, else `_2` and on, unless the same source made one already."""
        if place_key is not None:
            return bug.bug_id(sig), self.read(app, bug.bug_id(sig))
        n = 1
        while True:
            held = self.read(app, bug.bug_id(sig, n))
            if held is None and not (self.app_dir(app) / bug.bug_id(sig, n)).exists():
                return bug.bug_id(sig, n), None
            if held is not None and key in held.meta["seen"].get("sources", []):
                return held.id, held
            n += 1

    def _sighting(self, held: Kept, finding: dict, source: dict, reproduced: dict,
                  gate: Callable[[dict[str, str]], str | None], *, picture: bytes | None = None, with_picture: bool = False,
                  test_app: bool = True, scrub: Callable[[str], str] | None = None) -> Made:
        """The same signature again (§29.5): the count grows once per source, `last` and `builds` follow. The first finding
        and its script stay; the page's `Reproduced` (when it was checked) and `Seen before` follow. The call that updates
        the bug may add its picture (`with_picture`, §29.2; review R185 L3) when the bug has none in it yet."""
        meta = json.loads(json.dumps(held.meta))
        seen = meta["seen"]
        sources = list(seen.get("sources", []))
        if source["key"] not in sources:
            seen["count"] = int(seen.get("count", 1)) + int(finding.get("count", 1))
            sources.append(source["key"])
            seen["sources"] = sources[-SOURCES_KEPT:]
            seen["last"] = max(str(seen.get("last", "")), finding["when"])
            build = build_of(finding)
            if build not in seen.setdefault("builds", []):
                seen["builds"].append(build)
        if reproduced.get("checked") is not False:
            meta["reproduced"] = reproduced
        meta["test_app"] = meta.get("test_app") is True and bool(test_app)  # (a sighting never makes it less private)
        new_picture = None
        if picture is not None and not meta.get("picture_included") and isinstance(finding.get("picture"), dict):
            private = finding["picture"].get("private") is not False or not meta["test_app"]
            if not private or with_picture:
                new_picture = picture
                meta["picture_included"] = True
                meta["picture_file"] = {"private": private, "consent": bool(with_picture and private),
                                        "sha256": hashlib.sha256(picture).hexdigest()}
        return self._rewrite(held, meta, gate, new=False, picture=new_picture, scrub=scrub)

    def checked(self, held: Kept, reproduced: dict, gate: Callable[[dict[str, str]], str | None], *, new: bool = False,
                scrub: Callable[[str], str] | None = None) -> Made | None:
        """The result of a reproduction check (§29.4): `reproduced` replaced, and the page's `Reproduced` and `Environment`
        sections (the reset the check used) rewritten. The bug is read again under the lock first, so a sighting or a
        picture added during the check is kept (review R185 M5); None when the bug was removed meanwhile (nothing is
        written: a half bug is never made again)."""
        with self.locked(held.app):
            now = self.read(held.app, held.id)
            if now is None:
                return None
            meta = json.loads(json.dumps(now.meta))
            meta["reproduced"] = reproduced
            return self._rewrite(now, meta, gate, new=new, scrub=scrub)

    def add_picture(self, held: Kept, picture: bytes, gate: Callable[[dict[str, str]], str | None],
                    scrub: Callable[[str], str] | None = None) -> Made:
        """`with_picture: true` on a kept bug (§29.2): its own private picture goes in, while its source file still exists."""
        with self.locked(held.app):
            now = self.read(held.app, held.id)
            if now is None:
                raise BugFolderError(f"{held.id} was removed meanwhile; nothing was written.")
            meta = json.loads(json.dumps(now.meta))
            pic = now.finding.get("picture")
            private = not (isinstance(pic, dict) and pic.get("private") is False) or meta.get("test_app") is not True
            meta["picture_included"] = True
            meta["picture_file"] = {"private": private, "consent": private, "sha256": hashlib.sha256(picture).hexdigest()}
            return self._rewrite(now, meta, gate, new=False, picture=picture, scrub=scrub)

    def _rewrite(self, held: Kept, meta: dict, gate: Callable[[dict[str, str]], str | None], *, new: bool,
                 picture: bytes | None = None, scrub: Callable[[str], str] | None = None) -> Made:
        """Write the bug again (under the lock): its finding, meta and script read through the redactor again when there is
        one (a value added to the vault since is hidden: review R185 L2), and its page built anew from them."""
        finding = held.finding
        script_text = None
        if scrub is not None:
            safe = findings.safe_scrub(scrub)
            finding = findings.redact(finding, safe)  # type: ignore[assignment]
            meta = findings.redact(meta, safe)  # type: ignore[assignment]
            try:
                script = json.loads((held.folder / "repro.json").read_text("utf-8"))
                script_text = _dumps(findings.redact(script, safe))
            except (OSError, ValueError, RecursionError):
                script_text = None
        page = bug.markdown(finding, meta, "picture.jpg" if meta.get("picture_included") else None)
        files = {"report.md": page, "report.html": bug.page_html(page), "finding.json": _dumps({**finding, "bug": meta})}
        if script_text is not None:
            files["repro.json"] = script_text
        if (bad := gate(files)) is not None:
            raise BugGateError(bad, update=True)
        if picture is not None:
            _write(held.folder / "picture.jpg", picture)
        for name, text in files.items():
            _write(held.folder / name, text)
        self._prune(held.app, keep=held.id)
        return Made(meta, held.folder, new, bug.title(finding["kind"], finding.get("place"), finding["got"]), finding)

    def _files(self, finding: dict, meta: dict, script: dict | None, picture_file: str | None) -> dict[str, str]:
        page = bug.markdown(finding, meta, picture_file)
        return {"report.md": page, "report.html": bug.page_html(page), "repro.json": _dumps(script or {}),
                "finding.json": _dumps({**finding, "bug": meta})}

    def _write_new(self, folder: Path, files: dict[str, str], picture: bytes | None) -> None:
        """A new bug's folder, written whole beside it and then moved into place: a half-written bug is never seen."""
        folder.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{folder.name}.", dir=folder.parent))
        try:
            for name, text in files.items():
                (temporary / name).write_text(text, encoding="utf-8", newline="\n")
            if picture is not None:
                (temporary / "picture.jpg").write_bytes(picture)
            os.replace(temporary, folder)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    # ---- retention (§29.2) ----

    def prune(self, app: str, keep: str | None = None) -> None:
        """The newest `kept` bugs of the app stay; the others are removed with their `.zip`. `keep`, and a bug a check
        holds, is never removed."""
        with self.locked(app):
            self._prune(app, keep)

    def _prune(self, app: str, keep: str | None = None) -> None:
        bugs = self.bugs(app)
        extra = [k for k in bugs if k.id != keep][max(0, self.kept - (1 if keep else 0)):]
        for old in extra:
            if self._held(app, old.id):
                continue
            shutil.rmtree(old.folder, ignore_errors=True)
            try:
                (old.folder.parent / f"{old.id}.zip").unlink()
            except OSError:
                pass

    # ---- the list (§29.3) ----

    def listing(self, app: str | None = None) -> list[str]:
        lines: list[str] = []
        for each in ([app] if app else self.apps()):
            kept = self.bugs(each)
            if not kept:
                continue
            lines.append(each)
            for k in kept:
                r = k.meta.get("reproduced") or {}
                checked = "not checked" if r.get("checked") is False else f"{r.get('n')} of {r.get('of')}"
                lines.append(f"  {k.id}  {k.title}   seen {k.meta['seen']['count']}, last {str(k.meta['seen']['last'])[:10]}, "
                             f"{checked}")
        return lines

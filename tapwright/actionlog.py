"""The action log (docs/AGENT-TOOLS.md §12, v1.8): one JSON object per line for every action an agent or a person
makes, in ~/.tapwright/log/actions-YYYY-MM-DD.jsonl. Typed text is never written, only its length. Files older
than KEEP_DAYS are deleted when the log opens. Writing never fails an action: a log that can't be written is
reported once and then left alone."""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
from typing import Callable

KEEP_DAYS = 14
PREFIX, SUFFIX = "actions-", ".jsonl"


class ActionLog:
    def __init__(self, folder: Path | None, *, now: Callable[[], datetime.datetime] | None = None,
                 report: Callable[[str], None] = lambda message: None) -> None:
        """`folder` None turns the log off (`tapwright serve --no-log`)."""
        self.folder = folder
        self.now = now or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self.report = report
        self.broken = False
        if folder is not None:
            self.prune()

    def path(self) -> Path | None:
        if self.folder is None:
            return None
        return self.folder / f"{PREFIX}{self.now().strftime('%Y-%m-%d')}{SUFFIX}"

    def prune(self) -> None:
        """Delete the day files older than KEEP_DAYS (by the date in their name, not the file's clock)."""
        if self.folder is None or not self.folder.is_dir():
            return
        oldest = (self.now() - datetime.timedelta(days=KEEP_DAYS)).date()
        for f in self.folder.glob(f"{PREFIX}*{SUFFIX}"):
            try:
                day = datetime.date.fromisoformat(f.name[len(PREFIX):-len(SUFFIX)])
            except ValueError:
                continue  # not one of ours
            if day < oldest:
                try:
                    f.unlink()
                except OSError:
                    pass

    def write(self, entry: dict) -> None:
        """Append one entry, with its time first. Fields with no value (None) are left out."""
        path = self.path()
        if path is None or self.broken:
            return
        stamp = self.now().isoformat(timespec="milliseconds").replace("+00:00", "Z")
        line = json.dumps({"t": stamp, **{k: v for k, v in entry.items() if v is not None}}, ensure_ascii=False)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            self.broken = True
            self.report(f"the action log can't be written ({e}); actions go on without it")


def target_of(target: object) -> dict | None:
    """A target as the agent gave it, for the log: the fields it set (id, label, text, ref, x, y, index)."""
    if target is None:
        return None
    fields = ("id", "label", "text", "ref", "x", "y", "index")
    if isinstance(target, dict):  # as the call carried it, before it is checked
        found = {f: target[f] for f in fields if target.get(f) is not None}
    else:
        found = {f: getattr(target, f) for f in fields if getattr(target, f, None) is not None}
    return found or None


def default_folder() -> Path:
    from .store import HOME
    return HOME / "log"


def enabled() -> bool:
    """`TAPWRIGHT_NO_LOG=1` turns the log off as `serve --no-log` does."""
    return os.environ.get("TAPWRIGHT_NO_LOG", "") not in ("1", "true", "yes")

"""The audit log (docs/AGENT-TOOLS.md §23): the record of what was done under a person's standing permission. JSON
lines in ~/.tapwright/audit/YYYY-MM.jsonl, one per grant use, revocation, vault value typed, one-time code, scheduled
run and person's approval. It is not the action log (§12) and not the accessibility `audit` (§10).

- `t` is UTC to the millisecond; `tool` is "tapwright".
- `detail` never holds a value: it is redacted (§20.3) with the `scrub` the log is given, and names a vault entry, never
  its text.
- Entries older than 90 days are removed at start.
- A grant needs its line: `write` answers False when the line isn't on disk, and the grant then doesn't cover the
  touch (grants.py). There is no off switch; `--no-log` turns off the action log only.
- `tapwright audit-log` (a command, never an MCP tool) reads and deletes it: `read_lines` and `delete` below."""
from __future__ import annotations

import datetime
import json
import os
import re
from pathlib import Path
from typing import Callable

KINDS = ("grant", "revoke", "vault", "otp", "schedule", "approval")
KEEP_DAYS = 90
TOOL = "tapwright"
_FILE = re.compile(r"^\d{4}-\d{2}\.jsonl$")


def default_folder() -> Path:
    from .store import HOME
    return HOME / "audit"


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _stamp(at: datetime.datetime) -> str:
    return at.astimezone(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _line_time(line: str) -> datetime.datetime | None:
    try:
        obj = json.loads(line)
        t = obj.get("t") if isinstance(obj, dict) else None
        if not isinstance(t, str):
            return None
        at = datetime.datetime.fromisoformat(t.replace("Z", "+00:00"))
        return at if at.tzinfo is not None else at.replace(tzinfo=datetime.timezone.utc)
    except (ValueError, TypeError):
        return None


class AuditLog:
    def __init__(self, folder: Path | None = None, *, now: Callable[[], datetime.datetime] | None = None,
                 scrub: Callable[[str], str] | None = None) -> None:
        self.folder = Path(folder) if folder is not None else default_folder()
        self.now = now or _utc_now
        self.scrub = scrub or (lambda text: text)
        self._pruned_on: datetime.date | None = None

    def prune(self) -> None:
        """Remove entries older than KEEP_DAYS, by each line's own `t`. A line that can't be read is kept. Never raises."""
        self._pruned_on = self.now().date()
        cutoff = self.now() - datetime.timedelta(days=KEEP_DAYS)
        try:
            names = [p for p in self.folder.iterdir() if _FILE.match(p.name)]
        except OSError:
            return
        for path in names:
            try:
                lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
                keep = [line for line in lines if (when := _line_time(line)) is None or when >= cutoff]
                if len(keep) == len(lines):
                    continue
                if not keep:
                    path.unlink()
                else:
                    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
                    temp.write_text("".join(f"{line}\n" for line in keep), encoding="utf-8")
                    os.replace(temp, path)
            except OSError:
                continue  # an unreadable file stays as it is

    def write(self, kind: str, *, name: str | None = None, app: str | None = None, detail: str | None = None) -> bool:
        """Append one line; True only when it is on disk. `name`, `app` and `detail` are redacted first (§20.3)."""
        if kind not in KINDS:
            raise ValueError(f"unknown audit kind {kind!r}")
        at = self.now()
        if self._pruned_on != at.date():
            self.prune()  # (a daemon that runs for months prunes once a day, not only when it starts)
        entry: dict[str, str] = {"t": _stamp(at), "tool": TOOL, "kind": kind}
        try:
            for key, value in (("app", app), ("name", name), ("detail", detail)):
                if value is not None:
                    entry[key] = self.scrub(value)
            line = json.dumps(entry, ensure_ascii=False) + "\n"
            self.folder.mkdir(parents=True, exist_ok=True)
            path = self.folder / f"{entry['t'][:7]}.jsonl"
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            return True
        except Exception:  # a scrub that fails, a full disk, a folder in the way: no line, so no grant
            return False


def read_lines(folder: Path | None = None, since: datetime.datetime | None = None) -> list[str]:
    """Every line from `since` on (None: all), oldest file first, as written (for `tapwright audit-log` and `grants`)."""
    folder = Path(folder) if folder is not None else default_folder()
    out: list[str] = []
    try:
        names = sorted(p for p in folder.iterdir() if _FILE.match(p.name))
    except OSError:
        return out
    for path in names:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            if since is not None and ((when := _line_time(line)) is None or when < since):
                continue
            out.append(line)
    return out


def delete(folder: Path | None = None) -> int:
    """`tapwright audit-log --delete`: remove the log's files; how many lines went."""
    folder = Path(folder) if folder is not None else default_folder()
    gone = 0
    try:
        names = [p for p in folder.iterdir() if _FILE.match(p.name)]
    except OSError:
        return 0
    for path in names:
        try:
            gone += sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        except OSError:
            pass
        try:
            path.unlink()
        except OSError:
            pass
    return gone

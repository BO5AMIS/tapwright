"""The pid-liveness lock (docs/AGENT-TOOLS.md §22.3, §19.5): the policy file's edit lock (`<policy file>.edit.lock`) now,
and a schedule's run lock later. A lock file holds `{"owner", "pid", "started"}`: a random owner token, the holder's
process id, and the process's start time when the OS gives it, so a reused pid isn't mistaken for the holder.

- **Published whole:** the full JSON is written to a temporary file and hard-linked to the lock's name (a link is an
  exclusive create on NTFS, APFS and ext4). Where links aren't available it is created exclusively and written in place.
- **Stale only on proof:** its pid no longer runs on this computer, or the process with that pid started at another
  time. An empty or unreadable lock is never taken over; there is no age rule.
- **Takeover** goes through `<lock>.takeover`, published the same way: under it the lock is read again, and only if it
  is unchanged and its holder still dead is it unlinked and replaced.
- **A live holder is never displaced.** **Release** only while the content is still yours.

No new dependencies: the start time comes from ctypes (OpenProcess + GetProcessTimes) on Windows and `ps -o lstart=`
elsewhere, written in the same text Tapline writes, since both tools take the same edit lock."""
from __future__ import annotations

import dataclasses
import datetime
import errno
import json
import os
import secrets
import subprocess
import sys
import time
from typing import Callable

# Errors that mean this file system can't hard-link (so the exclusive-create fallback is used), not "the name is taken"
_NO_LINK = {errno.EPERM, errno.ENOSYS, errno.EXDEV, errno.EMLINK, errno.EACCES,
            getattr(errno, "ENOTSUP", errno.EPERM), getattr(errno, "EOPNOTSUPP", errno.EPERM)}


# ---- the process facts -------------------------------------------------------------------------------------------
def _win_handle(pid: int):
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    return kernel32, handle, ctypes.get_last_error()


def process_alive(pid: int) -> bool:
    """Does a process with this id run on this computer? A process we may not look at still runs."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel32, handle, error = _win_handle(pid)
        if not handle:
            return error == 5  # ERROR_ACCESS_DENIED: it exists; ERROR_INVALID_PARAMETER (87): no such process
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True  # can't tell: alive is the safe answer (a live holder is never displaced)
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def filetime_text(ticks: int) -> str:
    """A Windows FILETIME (100 ns ticks since 1601-01-01 UTC) as .NET's round-trip UTC text, `2026-11-14T06:00:12.4810000Z`:
    the text Tapline writes (`(Get-Process -Id N).StartTime.ToUniversalTime().ToString('o')`), so either tool reads
    the other's lock with the same start time (§22.3: the policy edit lock is shared)."""
    seconds, frac = divmod(ticks, 10_000_000)
    at = datetime.datetime(1601, 1, 1) + datetime.timedelta(seconds=seconds)
    return f"{at:%Y-%m-%dT%H:%M:%S}.{frac:07d}Z"


def process_start(pid: int) -> str | None:
    """When a process started, as text that is the same each time for one process, and the same text Tapline writes
    for it (Windows: .NET's round-trip UTC time; elsewhere `ps -o lstart=`); None when it can't be told."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel32, handle, _ = _win_handle(pid)
        if not handle:
            return None
        try:
            created, ended, kernel, user = (wintypes.FILETIME() for _ in range(4))
            if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(ended), ctypes.byref(kernel),
                                            ctypes.byref(user)):
                return None
            return filetime_text((created.dwHighDateTime << 32) | created.dwLowDateTime)
        finally:
            kernel32.CloseHandle(handle)
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


_OWN_START: list[str | None] = []


@dataclasses.dataclass
class Deps:
    """What a lock needs from the process and the system; tests replace any of it."""
    pid: int = dataclasses.field(default_factory=os.getpid)
    alive: Callable[[int], bool] = process_alive
    start_of: Callable[[int], str | None] = process_start
    link: Callable[[str, str], None] = os.link
    before_publish: Callable[[str], None] | None = None  # for tests: after the temp file, before the link

    def own_start(self) -> str | None:
        if self.pid != os.getpid():
            return self.start_of(self.pid)
        if not _OWN_START:
            _OWN_START.append(self.start_of(self.pid))
        return _OWN_START[0]


# ---- publishing and reading --------------------------------------------------------------------------------------
def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def _holder(text: str | None) -> dict:
    try:
        obj = json.loads(text) if text else None
    except ValueError:
        return {}
    return obj if isinstance(obj, dict) else {}


def _remove(path: str) -> bool:
    """Unlink `path`; True when it is gone. On Windows a file another reader has open this moment can't be deleted:
    tried again for a short while."""
    for attempt in range(40):
        try:
            os.unlink(path)
            return True
        except FileNotFoundError:
            return True
        except PermissionError:
            if sys.platform != "win32" or attempt == 39:
                return False
            time.sleep(0.005)
        except OSError:
            return False
    return False


def publish_new(deps: Deps, path: str, text: str) -> str:
    """Create `path` holding `text`, whole, only if it doesn't exist: "published", "exists" or "error"."""
    temp = f"{path}.{secrets.token_hex(6)}.pub"
    try:
        fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        _remove(temp)
        return "error"
    try:
        if deps.before_publish is not None:
            deps.before_publish(path)
        try:
            deps.link(temp, path)
            return "published"
        except FileExistsError:
            return "exists"
        except OSError as e:
            if e.errno not in _NO_LINK:
                return "error"
        # no hard links here: an exclusive create, then the content (a reader that can't read a holder waits)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
        except FileExistsError:
            return "exists"
        except OSError:
            return "error"
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            return "published"
        except OSError:
            return "error"
    finally:
        _remove(temp)


def _dead(deps: Deps, holder: dict) -> bool:
    """Is the holder a lock names provably gone? Its pid doesn't run, or that pid's process started at another time."""
    pid = holder.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or not isinstance(holder.get("owner"), str):
        return False  # unreadable: never taken over
    if not deps.alive(pid):
        return True
    started = holder.get("started")
    if isinstance(started, str):
        now = deps.start_of(pid)
        return now is not None and now != started
    return False


def _remove_if_unchanged(deps: Deps, path: str, seen: str) -> bool:
    """Remove `path` only while it still holds `seen`: moved aside under a name of our own first, then checked; a file
    that changed hands meanwhile is put back, never deleted."""
    tomb = f"{path}.{secrets.token_hex(6)}.tomb"
    for attempt in range(40):
        try:
            os.rename(path, tomb)
            break
        except PermissionError:
            if sys.platform != "win32" or attempt == 39:
                return False
            time.sleep(0.005)
        except OSError:
            return False
    try:
        moved = _read(tomb)
        if moved == seen:
            return True
        if moved is not None:
            publish_new(deps, path, moved)
    finally:
        _remove(tomb)
    return False


# ---- the lock ----------------------------------------------------------------------------------------------------
def take(path: str | os.PathLike, deps: Deps | None = None) -> str | None:
    """Take the lock: its owner token, or None while a live (or not provably dead) holder has it."""
    deps = deps or Deps()
    path = str(path)
    gate_path = f"{path}.takeover"
    token = f"{secrets.token_hex(8)}-{deps.pid}"
    mine = json.dumps({"owner": token, "pid": deps.pid, "started": deps.own_start()}) + "\n"
    for _ in range(6):
        gate = _read(gate_path)
        if gate is not None and not _dead(deps, _holder(gate)):
            return None  # a takeover (or a person's unlock) is under way
        first = publish_new(deps, path, mine)
        if first == "published":
            return token
        if first == "error":
            return None
        seen = _read(path)
        if seen is None:
            continue  # released between our create and our read
        if not _dead(deps, _holder(seen)):
            return None
        claimed = publish_new(deps, gate_path, mine)
        if claimed == "error":
            return None
        if claimed == "exists":
            other = _read(gate_path)
            if other is not None and _dead(deps, _holder(other)):
                _remove_if_unchanged(deps, gate_path, other)
            continue
        try:
            now = _read(path)
            if now == seen and _dead(deps, _holder(now)):
                if not _remove(path):
                    continue
                if publish_new(deps, path, mine) == "published":
                    return token
        finally:
            if _read(gate_path) == mine:
                _remove_if_unchanged(deps, gate_path, mine)
    return None


def release(path: str | os.PathLike, token: str) -> None:
    """Release the lock only while it is still `token`'s."""
    path = str(path)
    if _holder(_read(path)).get("owner") == token:
        _remove(path)


def state(path: str | os.PathLike, deps: Deps | None = None) -> tuple[str, int | None]:
    """What the lock is now, for `doctor` and `grants unlock` (later): ("absent" | "live" | "empty" | "unreadable" |
    "dead", the holder's pid)."""
    deps = deps or Deps()
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return "absent", None
    except (OSError, UnicodeDecodeError):
        return "unreadable", None
    if not text.strip():
        return "empty", None
    holder = _holder(text)
    pid = holder.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or not isinstance(holder.get("owner"), str):
        return "unreadable", None
    return ("dead" if _dead(deps, holder) else "live"), pid

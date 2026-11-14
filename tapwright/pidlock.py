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

**`started` (v1.16):** the holder's process start time as an integer, milliseconds since 1970-01-01 UTC, rounded down: the
creation time from ctypes (OpenProcess + GetProcessTimes) on Windows, `ps -o lstart=` read with `LC_ALL=C` and `TZ=UTC`
on macOS and Linux. A reader compares whole seconds (`started // 1000` against the live process's start, floored the
same way). A `started` that isn't an integer (an older tool's text) can't prove a reused pid: its holder counts as alive
while that pid runs, and a person clears it with `unlock`. Both tools take the same edit lock, so this is one format.
No new dependencies."""
from __future__ import annotations

import dataclasses
import datetime
import errno
import json
import os
import re
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


_EPOCH_TICKS = 116_444_736_000_000_000  # 1970-01-01 as a Windows FILETIME (100 ns ticks since 1601-01-01 UTC)
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_LSTART = re.compile(r"[A-Za-z]{3}\s+([A-Za-z]{3})\s+(\d{1,2})\s+(\d{1,2}):(\d{2}):(\d{2})\s+(\d{4})")


def filetime_ms(ticks: int) -> int:
    """A Windows FILETIME as whole milliseconds since 1970-01-01 UTC, rounded down (what .NET's
    `ToUnixTimeMilliseconds` gives Tapline for the same process)."""
    return (ticks - _EPOCH_TICKS) // 10_000


def parse_lstart(text: str) -> int | None:
    """`ps -o lstart=` read under `LC_ALL=C` and `TZ=UTC` (`Fri Nov 13 05:00:00 2026`, the day may be padded) as UTC
    milliseconds; None when it isn't that shape."""
    m = _LSTART.fullmatch(text.strip())
    if m is None or m.group(1) not in _MONTHS:
        return None
    try:
        at = datetime.datetime(int(m.group(6)), _MONTHS.index(m.group(1)) + 1, int(m.group(2)), int(m.group(3)),
                               int(m.group(4)), int(m.group(5)), tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    return int(at.timestamp()) * 1000


def _run_ps(pid: int) -> str:
    env = {**os.environ, "LC_ALL": "C", "TZ": "UTC"}
    return subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5,
                          env=env).stdout


def is_ms(started: object) -> bool:
    """Is a lock's `started` the v1.16 form, an integer (never a bool, a float or text)?"""
    return isinstance(started, int) and not isinstance(started, bool)


def process_start(pid: int, platform: str | None = None, run_ps: Callable[[int], str] | None = None) -> int | None:
    """When a process started (§22.3, v1.16): whole milliseconds since 1970-01-01 UTC, rounded down; None when it can't
    be told. Windows: GetProcessTimes' creation time; elsewhere `ps -o lstart=` under `LC_ALL=C TZ=UTC` (whole
    seconds), so both tools write the same number for one process."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if (platform or sys.platform) == "win32":
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
            return filetime_ms((created.dwHighDateTime << 32) | created.dwLowDateTime)
        finally:
            kernel32.CloseHandle(handle)
    try:
        return parse_lstart((run_ps or _run_ps)(pid))
    except (OSError, subprocess.SubprocessError):
        return None


_OWN_START: list[int | None] = []


@dataclasses.dataclass
class Deps:
    """What a lock needs from the process and the system; tests replace any of it."""
    pid: int = dataclasses.field(default_factory=os.getpid)
    alive: Callable[[int], bool] = process_alive
    start_of: Callable[[int], int | None] = process_start
    link: Callable[[str, str], None] = os.link
    before_publish: Callable[[str], None] | None = None  # for tests: after the temp file, before the link

    def own_start(self) -> int | None:
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
    """Is the holder a lock names provably gone? Its pid doesn't run, or that pid's process started in another second
    (v1.16: `started` is an integer of UTC ms, compared as whole seconds). Any other `started` can't prove a reuse."""
    pid = holder.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or not isinstance(holder.get("owner"), str):
        return False  # unreadable: never taken over
    if not deps.alive(pid):
        return True
    started = holder.get("started")
    if is_ms(started):
        now = deps.start_of(pid)
        return is_ms(now) and now // 1000 != started // 1000
    return False


def _legacy(holder: dict) -> bool:
    """A holder whose `started` isn't the v1.16 integer (an older tool's text, or null: a holder that couldn't read its
    own start, review 1.7 A-M5): alive while its pid runs, since a reuse can't be proven, but `unlock` may clear it."""
    return not is_ms(holder.get("started"))


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
    """What the lock is now, for `doctor` and `grants unlock`: ("absent" | "live" | "old" | "empty" | "unreadable" |
    "dead", the holder's pid). "old": the holder's pid runs but its `started` is an older tool's text, so a reuse
    can't be proven: it counts as alive (never taken over), and only a person's `unlock` clears it (v1.16)."""
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
    if _dead(deps, holder):
        return "dead", pid
    return ("old" if _legacy(holder) else "live"), pid


# ---- the way out of a stuck lock (`tapwright grants unlock`, §19.5, 1.14.2) ---------------------------------------
def stuck_words(kind: str, pid: int | None) -> str:
    """Why a lock is stuck, for `doctor` and `unlock`."""
    if kind == "dead":
        return f"held by pid {pid}, which is not running"
    if kind == "empty":
        return "empty (its holder stopped while writing it)"
    if kind == "old":
        return f"started in an older format, cleared by unlock (pid {pid})"
    return "unreadable"


@dataclasses.dataclass
class Unlocked:
    """What `unlock` did: status `none` (no lock), `live` (a holder is proven alive: nothing was removed), `removed`, or
    `failed` (it changed or can't be deleted); the files removed, each with why; the pid of a live holder."""
    status: str
    pid: int | None = None
    removed: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    failed: str | None = None


def unlock(path: str | os.PathLike, deps: Deps | None = None) -> Unlocked:
    """A person's removal of a stuck lock and its `.takeover`: only when no holder is proven alive. A live holder (or a
    lock this process can't tell is stale) is never removed; one whose `started` is an older tool's text can't be
    proven alive, so `unlock` clears it (v1.16). A file is removed only while it still holds what was seen."""
    deps = deps or Deps()
    paths = [str(path), f"{path}.takeover"]
    seen: list[tuple[str, str, str, int | None]] = []
    for p in paths:
        kind, pid = state(p, deps)
        if kind == "absent":
            continue
        if kind == "live":
            return Unlocked("live", pid)
        text = _read(p)
        if text is None and kind != "unreadable":
            continue
        seen.append((p, text or "", kind, pid))
    if not seen:
        return Unlocked("none")
    done = Unlocked("removed")
    for p, text, kind, pid in seen:
        if not text.strip() or kind == "unreadable":
            gone = _remove_if_unchanged(deps, p, text) if text else _remove(p)
        else:
            gone = _remove_if_unchanged(deps, p, text)
        if not gone:
            done.status, done.failed = "failed", p
            break
        done.removed.append((p, stuck_words(kind, pid)))
    return done

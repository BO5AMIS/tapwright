"""Find the running `tapwright serve`, or start it, and call its API (standard library only)."""
import http.client
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from . import store


class DaemonError(RuntimeError):
    """A refusal or error from the daemon, with its code (docs/VIEWER-PROTOCOL.md)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def base_url(info: dict) -> str:
    return f"http://127.0.0.1:{info['port']}"


def viewer_url(info: dict) -> str:
    return f"{base_url(info)}/?token={info['token']}"


def _request(info: dict, method: str, path: str, body: dict | None = None, timeout: float = 10.0,
             client_id: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {info['token']}", "Content-Type": "application/json"}
    if client_id:
        headers["X-Tapwright-Client"] = client_id
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base_url(info) + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read() or b"{}").get("error") or {}
        except ValueError:
            err = {}
        raise DaemonError(err.get("code", f"http-{e.code}"), err.get("message", f"HTTP {e.code}")) from None


def healthy(info: dict, timeout: float = 2.0) -> dict | None:
    """The daemon's status if it answers with this token, else None."""
    if not info.get("port") or not info.get("token"):
        return None
    try:
        status = _request(info, "GET", "/api/health", timeout=timeout)
    except (OSError, DaemonError, ValueError, http.client.HTTPException):
        return None
    return status if isinstance(status, dict) and "protocol" in status else None


def start_daemon() -> subprocess.Popen:
    """`tapwright serve` in the background: no console window, and it outlives the agent that
    started it (on Windows it leaves the agent host's job object when Windows allows that)."""
    cmd = [sys.executable, "-m", "tapwright", "serve"]
    quiet = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
             "close_fds": True}
    if os.name != "nt":
        return subprocess.Popen(cmd, start_new_session=True, **quiet)
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        return subprocess.Popen(cmd, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **quiet)
    except OSError:  # the job doesn't allow breaking away
        return subprocess.Popen(cmd, creationflags=flags, **quiet)


def find_or_start(wait: float = 60.0) -> dict:
    info = store.read_serve_info()
    if healthy(info):
        return info
    proc = start_daemon()
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        time.sleep(0.5)
        info = store.read_serve_info()
        if healthy(info):
            return info
        if proc is not None and proc.poll() is not None:
            raise DaemonError("reconnecting", "tapwright serve exited at start; see ~/.tapwright/serve.log")
    raise DaemonError("reconnecting", "tapwright serve didn't start; see ~/.tapwright/serve.log")


def _refused(e: BaseException) -> bool:
    return isinstance(e, ConnectionRefusedError) or (
        isinstance(e, urllib.error.URLError) and isinstance(e.reason, ConnectionRefusedError))


def _timed_out(e: BaseException) -> bool:
    return isinstance(e, (TimeoutError, socket.timeout)) or (
        isinstance(e, urllib.error.URLError) and isinstance(e.reason, (TimeoutError, socket.timeout)))


def _attempt(info: dict, tool: str, args: dict, timeout: float, client_id: str, retry_ok: bool) -> dict:
    """One request. Only a refused connection (nothing was sent) is passed up for a retry."""
    try:
        return _request(info, "POST", f"/api/{tool}", args, timeout, client_id)
    except DaemonError:
        raise
    except (OSError, http.client.HTTPException) as e:
        if retry_ok and _refused(e):
            raise
        if _timed_out(e):
            raise DaemonError("timeout", f"no answer from tapwright serve within {timeout:.0f} s; "
                                         "look at the screen before retrying") from None
        raise DaemonError("reconnecting", str(e)) from None


def call(tool: str, args: dict, client_id: str, timeout: float = 120.0) -> dict:
    """One agent tool in the daemon: {"texts", "image", "notes"}. Raises DaemonError for refusals.
    It never sends an action twice: it retries only when the connection was refused."""
    info = find_or_start()
    try:
        return _attempt(info, tool, args, timeout, client_id, retry_ok=True)
    except OSError:  # refused: the daemon went away between the check and the call
        info = find_or_start()
        return _attempt(info, tool, args, timeout, client_id, retry_ok=False)


def shutdown(info: dict) -> None:
    _request(info, "POST", "/api/shutdown", {})

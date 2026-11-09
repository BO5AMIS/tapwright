"""Find the running `tapwright serve`, or start it, and call its API (standard library only)."""
import json
import os
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
        return _request(info, "GET", "/api/health", timeout=timeout)
    except (OSError, DaemonError, ValueError):
        return None


def start_daemon() -> None:
    """`tapwright serve` in the background: no console window, and it outlives the agent that
    started it (on Windows it leaves the agent host's job object when Windows allows that)."""
    cmd = [sys.executable, "-m", "tapwright", "serve"]
    quiet = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
             "close_fds": True}
    if os.name != "nt":
        subprocess.Popen(cmd, start_new_session=True, **quiet)
        return
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(cmd, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **quiet)
    except OSError:  # the job doesn't allow breaking away
        subprocess.Popen(cmd, creationflags=flags, **quiet)


def find_or_start(wait: float = 60.0) -> dict:
    info = store.read_serve_info()
    if healthy(info):
        return info
    start_daemon()
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        time.sleep(0.5)
        info = store.read_serve_info()
        if healthy(info):
            return info
    raise DaemonError("reconnecting", "tapwright serve didn't start; see ~/.tapwright/serve.log")


def call(tool: str, args: dict, client_id: str, timeout: float = 120.0) -> dict:
    """One agent tool in the daemon: {"texts", "image", "notes"}. Raises DaemonError for refusals."""
    info = find_or_start()
    try:
        return _request(info, "POST", f"/api/{tool}", args, timeout, client_id)
    except DaemonError:
        raise
    except OSError:  # the daemon went away between the check and the call
        info = find_or_start()
        return _request(info, "POST", f"/api/{tool}", args, timeout, client_id)


def shutdown(info: dict) -> None:
    _request(info, "POST", "/api/shutdown", {})

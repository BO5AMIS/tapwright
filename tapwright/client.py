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

from . import policy, store, uxcheck


class DaemonError(RuntimeError):
    """A refusal or error from the daemon, with its code (docs/VIEWER-PROTOCOL.md)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def base_url(info: dict) -> str:
    return f"http://127.0.0.1:{info['port']}"


def viewer_url(info: dict) -> str:
    return f"{base_url(info)}/?token={info['token']}"


SCOPE: dict[str, str] = {}  # the headers that say what this process's agent may use; none: no apps at all


def set_scope(apps: list[str] | None = None, dedicated: bool = False) -> None:
    """Personal mode (the default): the agent may read and touch only `apps` (bundle ids or exact names).
    `dedicated`: the phone is kept for testing, so there is no limit. The daemon enforces it on every call."""
    SCOPE.clear()
    if dedicated:
        SCOPE["X-Tapwright-Mode"] = "dedicated"
    elif apps:
        SCOPE["X-Tapwright-Apps"] = ",".join(apps)


def set_policy(rules: policy.Policy | None = None) -> None:
    """The safety policy this process's agent works under (docs/AGENT-TOOLS.md 11.1). None, the built-in one, is
    sent as no header at all. The daemon refuses a call whose policy it can't read."""
    if rules is None or rules == policy.DEFAULT:
        SCOPE.pop("X-Tapwright-Policy", None)
    else:
        SCOPE["X-Tapwright-Policy"] = rules.dumps()


def set_ux_rules(rules: uxcheck.Rules | None = None) -> None:
    """The user's house rules for `ux_check` and journeys (docs/AGENT-TOOLS.md 17.3). None or empty is sent as no
    header at all. The daemon refuses a call whose rules it can't read."""
    if not rules:
        SCOPE.pop("X-Tapwright-Ux-Rules", None)
    else:
        SCOPE["X-Tapwright-Ux-Rules"] = rules.dumps()


def _request(info: dict, method: str, path: str, body: dict | None = None, timeout: float = 10.0,
             client_id: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {info['token']}", "Content-Type": "application/json"}
    if client_id:
        headers["X-Tapwright-Client"] = client_id
        headers.update(SCOPE)  # an agent's calls carry the apps it was given, or that the device is dedicated
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
            info = store.read_serve_info()  # exit code 0 means "already running"
            if healthy(info):
                return info
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
    """One agent tool in the daemon: {"text", "image", "image_size"}. Raises DaemonError for refusals.
    It never sends an action twice: it retries only when the connection was refused."""
    info = find_or_start()
    try:
        return _attempt(info, tool, args, timeout, client_id, retry_ok=True)
    except OSError:  # refused: the daemon went away between the check and the call
        info = find_or_start()
        return _attempt(info, tool, args, timeout, client_id, retry_ok=False)


def tell_daemon(path: str, client_id: str, timeout: float = 3.0) -> bool:
    """A word from this agent's process to a daemon that is already running (never starting one): `/api/session/ping`
    (still here) or `/api/session/end` (gone). True when the daemon took it."""
    info = store.read_serve_info()
    if not info.get("port") or not info.get("token"):
        return False
    try:
        _request(info, "POST", path, {}, timeout, client_id)
    except (OSError, ValueError, DaemonError, http.client.HTTPException):
        return False
    return True


def ping(client_id: str) -> bool:
    """This agent's process is alive (sent every few seconds): a condition it set is kept while it is heard from."""
    return tell_daemon("/api/session/ping", client_id)


def end_session(client_id: str) -> bool:
    """This agent's process is ending: whatever it set on the phone (a network condition) ends with it."""
    return tell_daemon("/api/session/end", client_id)


def shutdown(info: dict) -> None:
    """Ask the daemon to stop. It may drop the connection before its answer is out (seen now and then with a
    stream open): that is the stop happening, not a failure, so `tapwright serve --stop` doesn't end in a
    traceback. A refusal (a wrong token) is still raised."""
    try:
        _request(info, "POST", "/api/shutdown", {})
    except (OSError, ValueError, http.client.HTTPException):
        pass

"""Find the running `tapwright serve`, or start it, and call its API (standard library only)."""
import hashlib
import hmac
import http.client
import json
import os
import secrets
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


def set_scope(apps: list[str] | None = None, dedicated: bool = False, phone: bool = False) -> None:
    """Personal mode (the default): the agent may read and touch only `apps` (bundle ids or exact names).
    `dedicated`: the phone is kept for testing, so there is no limit. `phone` (1.7, §24.4): the phone is dedicated to
    automation; every app, as `dedicated`. The daemon enforces it on every call."""
    SCOPE.clear()
    if phone:
        SCOPE["X-Tapwright-Mode"] = "phone"
    elif dedicated:
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


def set_maps(folder: str | None = None) -> None:
    """Where this process's agent keeps its per-app maps (`tapwright mcp --maps DIR`, docs/AGENT-TOOLS.md 18.9), as an
    absolute folder. None is sent as no header: the daemon's own folder, else ~/.tapwright/maps."""
    if not folder:
        SCOPE.pop("X-Tapwright-Maps", None)
    else:
        SCOPE["X-Tapwright-Maps"] = str(os.path.abspath(os.path.expanduser(folder)))


def set_accounts(file: str | None = None) -> None:
    """The test accounts file this process's agent uses (`tapwright mcp --accounts FILE`, AGENT-TOOLS §18.6), as an absolute
    path: only the path is sent, never what is in it. None is sent as no header."""
    if not file:
        SCOPE.pop("X-Tapwright-Accounts", None)
    else:
        SCOPE["X-Tapwright-Accounts"] = str(os.path.abspath(os.path.expanduser(file)))


PROOF_PREFIX = b"tapwright-proof-v2\x00"  # as server.PROOF_PREFIX (this module must not import server: starlette)


def _prove(info: dict, timeout: float) -> http.client.HTTPConnection:
    """Before the token goes anywhere: ask whoever listens on the port to prove it holds it. A stale serve.json (the
    daemon died, another local process took its port) must not hand the token to that process. The client sends a random
    challenge and the daemon answers HMAC-SHA256(token, prefix + challenge); the token itself is not sent until the
    answer is right (constant-time compare). Raises DaemonError("unauthorized") for a wrong or missing proof; OSError
    (a refused connection) passes up as it did, since nothing was sent.
    Returns the connection that was proved: the token goes over THAT connection (keep-alive) and never over a new one,
    or a process that bound the port between the two connections would get it. The connection never reconnects by
    itself (auto_open off): if the daemon closed it after the proof, the request fails and no token is sent."""
    challenge = secrets.token_bytes(32)
    conn = http.client.HTTPConnection("127.0.0.1", int(info["port"]), timeout=timeout)  # the literal address, never a name
    try:
        conn.request("POST", "/api/prove", body=json.dumps({"challenge": challenge.hex()}).encode(),
                     headers={"Content-Type": "application/json"})
        sock = conn.sock
        reply = conn.getresponse()
        raw = reply.read()
        given = None
        if reply.status == 200:  # (an older daemon has no such route: it answers 401)
            try:
                given = json.loads(raw or b"{}").get("proof")
            except (ValueError, AttributeError):
                given = None
        # signed with the port this client dialled: a proof relayed from a daemon on another port never matches
        signed = PROOF_PREFIX + challenge + b"\x00127.0.0.1\x00" + str(int(info["port"])).encode()
        want = hmac.new(str(info["token"]).encode(), signed, hashlib.sha256).hexdigest()
        if not isinstance(given, str) or not hmac.compare_digest(given.encode(), want.encode()):
            raise DaemonError("unauthorized", "whatever listens on tapwright serve's port did not prove it holds the "
                                              "token (a stale serve.json, or an older tapwright serve: `tapwright serve "
                                              "--stop`, then start it again); the token was not sent")
        if sock is None or conn.sock is not sock:  # the line was closed after the proof: not the proved peer any more
            raise DaemonError("unauthorized", "the connection that proved tapwright serve's token was closed; the token "
                                              "was not sent")
        conn.auto_open = 0  # a closed line raises NotConnected on the next request instead of dialling again
        return conn
    except BaseException:
        conn.close()
        raise


def _request(info: dict, method: str, path: str, body: dict | None = None, timeout: float = 10.0,
             client_id: str | None = None, dialog: bool = False) -> dict:
    conn = _prove(info, timeout)
    headers = {"Authorization": f"Bearer {info['token']}", "Content-Type": "application/json"}
    if client_id:
        headers["X-Tapwright-Client"] = client_id
        headers.update(SCOPE)  # an agent's calls carry the apps it was given, or that the device is dedicated
    if dialog:
        headers["X-Tapwright-Dialog"] = "1"  # this agent's host can show a dialog, and is polling for it (dialog.py)
    data = json.dumps(body).encode() if body is not None else None
    try:
        conn.request(method, path, body=data, headers=headers)  # the proved connection: never a new one
        reply = conn.getresponse()
        raw = reply.read()
    finally:
        conn.close()
    if reply.status >= 400:
        try:
            err = json.loads(raw or b"{}").get("error") or {}
        except (ValueError, AttributeError):
            err = {}
        if not isinstance(err, dict):
            err = {}
        raise DaemonError(err.get("code", f"http-{reply.status}"), err.get("message", f"HTTP {reply.status}"))
    return json.loads(raw or b"{}")


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


def _attempt(info: dict, tool: str, args: dict, timeout: float, client_id: str, retry_ok: bool,
             dialog: bool = False) -> dict:
    """One request. Only a refused connection (nothing was sent) is passed up for a retry."""
    try:
        return _request(info, "POST", f"/api/{tool}", args, timeout, client_id, dialog)
    except DaemonError:
        raise
    except (OSError, http.client.HTTPException) as e:
        if retry_ok and _refused(e):
            raise
        if _timed_out(e):
            raise DaemonError("timeout", f"no answer from tapwright serve within {timeout:.0f} s; "
                                         "look at the screen before retrying") from None
        raise DaemonError("reconnecting", str(e)) from None


def call(tool: str, args: dict, client_id: str, timeout: float = 120.0, dialog: bool = False) -> dict:
    """One agent tool in the daemon: {"text", "image", "image_size"}. Raises DaemonError for refusals.
    It never sends an action twice: it retries only when the connection was refused. `dialog`: the caller's host can
    show a dialog, so the daemon asks a person there and waits for the answer (see dialog.py)."""
    info = find_or_start()
    try:
        return _attempt(info, tool, args, timeout, client_id, retry_ok=True, dialog=dialog)
    except OSError:  # refused: the daemon went away between the check and the call
        info = find_or_start()
        return _attempt(info, tool, args, timeout, client_id, retry_ok=False, dialog=dialog)


def dialog_poll(client_id: str, since: int, wait: float) -> dict:
    """The events the daemon has for this agent's host after `since` ({"seq", "events"}); waits up to `wait` seconds for
    one. Never starts the daemon: a call in flight has one (see dialog.py)."""
    return _request(store.read_serve_info(), "POST", "/api/dialog_poll", {"since": since, "wait": wait},
                    timeout=wait + 10, client_id=client_id)


def dialog_answer(client_id: str, ask_id: str, answer: str) -> bool:
    """The host's answer to an ask; whether the daemon took it (the first answer wins, an old one is ignored)."""
    done = _request(store.read_serve_info(), "POST", "/api/dialog_answer", {"id": ask_id, "answer": answer},
                    client_id=client_id)
    return bool(done.get("ok"))


def unlock_phone(accounts: str | None = None, timeout: float = 60.0) -> dict:
    """Ask the running daemon to unlock a locked phone with the vault's `phone.passcode` (§22.3: a scheduled run on a
    locked phone). `accounts`: the vault file, as a path (only the path is sent; the daemon reads the value and types
    it). `{"ok", "code", "message"}`; never starts the daemon."""
    headers_before = dict(SCOPE)
    try:
        set_accounts(accounts)
        return _request(store.read_serve_info(), "POST", "/api/phone/unlock", {}, timeout, "schedule")
    finally:
        SCOPE.clear()
        SCOPE.update(headers_before)


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

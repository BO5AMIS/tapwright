"""`tapwright serve`: the HTTP side of the daemon. The live view page, the stream, the viewer
WebSocket and the agent API, all behind one token; and starting, finding and stopping it.
Wire format: docs/VIEWER-PROTOCOL.md."""
import asyncio
import dataclasses
import hashlib
import hmac
import json
import logging
import os
import secrets
import socket
import time
import urllib.parse
from importlib.resources import files
from logging.handlers import RotatingFileHandler

from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

from . import policy, store, trace, uxcheck
from .ceiling import Ceiling
from .control import STILL_WANTED, Refused
from .daemon import Scope
from .errors import HTTP_STATUS
from .stream import PictureGone, jpeg_of

COOKIE = "tapwright_token"
PAGE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
# What a person sees when the link carries no valid key (2026-11-12: an agent opened `?token=SECRET`, and the old one
# line, "Open the link that tapwright view prints", didn't tell the owner what to do)
NO_KEY_PAGE = (
    "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
    "<title>Tapwright live view</title>"
    "<body style='font:16px/1.5 system-ui,sans-serif;max-width:36rem;margin:3rem auto;padding:0 1rem'>"
    "<h1 style='font-size:1.3rem'>This link has no valid key</h1>"
    "<p>The live view opens only with the key this computer holds. A link that carries the key opens it for whoever "
    "has the link, so keep the key private and never paste such a link into a chat.</p>"
    "<p>To open it:</p><ul>"
    "<li>on this computer, run <code>tapwright view</code>: it opens the live view with a fresh key;</li>"
    "<li>or ask your agent to use its <code>viewer</code> tool, which opens it for you.</li></ul>"
    "<p>An agent driving Tapwright through a script opens it with <code>tapwright.client</code>'s "
    "<code>viewer_url</code> and the browser, never by writing the link out.</p>"
)
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})
DEFAULT_PORT = 8417
IDLE_LIMIT = 30 * 60  # seconds without viewers or requests before the daemon stops itself


def _host_ok(conn, hosts) -> bool:
    """The Host header names this server (against DNS rebinding)."""
    return conn.headers.get("host", "").split(":")[0] in hosts


def _same(given: str, token: str) -> bool:
    return bool(given) and secrets.compare_digest(given.encode(), token.encode())


PROOF_PREFIX = b"tapwright-proof-v2\x00"  # domain separation: the same HMAC key is never asked to sign anything else
CHALLENGE_BYTES = (8, 64)


def proof_of(token: str, challenge: bytes, port: int, host: str = "127.0.0.1") -> str:
    """What a daemon that holds `token` answers to `challenge` (hex): HMAC-SHA256 over a fixed prefix and the challenge.
    A client sends its token only to a process that has answered this, so a stale serve.json whose port another process
    has taken never gets the token (Tapline's finding, 1.6 security). The daemon's own listening port is signed too, and
    the client checks it against the port it dialled: a process on a stale port that relays the challenge to a live
    daemon elsewhere gets a proof for that other port, which never matches (Tapline's Codex finding)."""
    signed = PROOF_PREFIX + challenge + b"\x00" + _normal_host(host).encode() + b"\x00" + str(int(port)).encode()
    return hmac.new(token.encode(), signed, hashlib.sha256).hexdigest()


def _normal_host(host: str) -> str:
    """The local address as both ends write it: an IPv4-mapped IPv6 address (`::ffff:127.0.0.1`) is its IPv4 form."""
    host = str(host).strip("[]").lower()
    return host[len("::ffff:"):] if host.startswith("::ffff:") and "." in host else host


def _bearer_ok(conn, token: str) -> bool:
    bearer = conn.headers.get("authorization", "")
    return _same(bearer[7:] if bearer.startswith("Bearer ") else "", token)


def _cookie_ok(conn, token: str) -> bool:
    """The cookie is shared across ports and sent by any page on this host, so a request that
    only has the cookie must also come from this page itself (Fetch Metadata)."""
    return _same(conn.cookies.get(COOKIE, ""), token) and conn.headers.get("sec-fetch-site") in (
        None, "same-origin", "none")


def _cookie_present(conn, token: str) -> bool:
    return _same(conn.cookies.get(COOKIE, ""), token)


def _error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _hv(request, name: str, default: str | None = None) -> str | None:
    """A custom request header's value. `tapwright call` percent-encodes them all (and says so) when one has characters
    outside Latin-1, which HTTP can't carry as they are (an app named 微信)."""
    value = request.headers.get(name)
    if value is None:
        return default
    return urllib.parse.unquote(value) if request.headers.get("x-tapwright-encoding") == "percent" else value


def create_app(daemon, token: str, hosts=LOCAL_HOSTS, on_stop=None, ceiling: Ceiling | None = None) -> Starlette:
    page = files("tapwright").joinpath("viewer.html").read_text(encoding="utf-8")

    def denied(conn, cookie: bool = False) -> Response | None:
        """/api takes only the Authorization header; the page and its stream may use the cookie."""
        if not _host_ok(conn, hosts):
            return _error("unauthorized", "unknown host", 403)
        if _bearer_ok(conn, token):
            return None
        if cookie and _cookie_present(conn, token):
            if _cookie_ok(conn, token):
                return None
            return _error("unauthorized", "cross-site request refused", 403)
        return _error("unauthorized", "missing or wrong token: open the link `tapwright view` prints", 401)

    async def index(request):
        if not _host_ok(request, hosts):
            return Response("unknown host", status_code=403)
        if _same(request.query_params.get("token", ""), token):
            # Answer with the page itself: a link clicked on another site arrives cross-site, and a
            # redirected request would too, so Chrome would withhold the Strict cookie. The page
            # drops the token from the address bar with history.replaceState.
            response = HTMLResponse(page, headers=PAGE_HEADERS)
            response.set_cookie(COOKIE, token, httponly=True, samesite="strict")
            return response
        if not (_bearer_ok(request, token) or _cookie_present(request, token)):
            return HTMLResponse(NO_KEY_PAGE, status_code=401)
        if not _bearer_ok(request, token) and not _cookie_ok(request, token):
            return HTMLResponse("cross-site request refused", status_code=403)
        return HTMLResponse(page, headers=PAGE_HEADERS)

    async def prove(request):
        """The proof that this process holds the token, asked before a client sends it. It needs no token (the client has
        not sent it yet) and gives none away: an HMAC of a caller's challenge reveals nothing of its key."""
        if not _host_ok(request, hosts):
            return _error("unauthorized", "unknown host", 403)
        try:
            given = json.loads(await request.body())
            challenge = bytes.fromhex(given["challenge"])
        except (ValueError, KeyError, TypeError, RecursionError):
            return _error("bad_params", 'send {"challenge": "<8 to 64 random bytes as hex>"}', 400)
        if not CHALLENGE_BYTES[0] <= len(challenge) <= CHALLENGE_BYTES[1]:
            return _error("bad_params", 'send {"challenge": "<8 to 64 random bytes as hex>"}', 400)
        # the port this connection really arrived on (the listening socket's, not the Host header, which a relay sets)
        server = request.scope.get("server") or (None, None)
        if not isinstance(server[1], int):
            return _error("internal", "can't tell which port this daemon listens on", 500)
        # the address and port this connection really arrived on (the socket's: a relay elsewhere signs its own)
        return JSONResponse({"proof": proof_of(token, challenge, server[1], str(server[0]))})

    async def health(request):
        # `pages`: live-view pages connected now, so an agent's `viewer` can leave an open one be instead of opening
        # another tab (2026-11-12: agents opened two dozen). Only here, not in the status the viewer protocol sends.
        return denied(request) or JSONResponse({**daemon.status(), "pages": daemon.ws_pages})

    async def tool(request):
        problem = denied(request)
        if problem is not None:
            return problem
        try:
            body = await request.body()
            args = json.loads(body) if body else {}
        except (ValueError, RecursionError):
            return _error("bad_params", "the body must be JSON", 400)
        if not isinstance(args, dict):
            return _error("bad_params", "the body must be a JSON object", 400)
        # X-Agent-Client is the name both tools share; X-Tapwright-Client is the older one, still accepted
        client = _hv(request, "x-agent-client") or _hv(request, "x-tapwright-client") or "anonymous"
        # Personal mode unless the caller says the device is dedicated: a caller that names no apps gets none.
        apps = tuple(a.strip() for a in _hv(request, "x-tapwright-apps", "").split(",") if a.strip())
        dedicated = _hv(request, "x-tapwright-mode") == "dedicated"
        scope = None if dedicated else Scope(apps)
        # What the daemon was started with is a ceiling: a call may only narrow it (AGENT-TOOLS §18.10)
        if ceiling is not None and (why := ceiling.refuse_scope(apps, dedicated)) is not None:
            return _error("not_allowed", why, HTTP_STATUS["not_allowed"])
        # The safety policy (docs/AGENT-TOOLS.md 11.1): none sent is the built-in one; a bad one is refused
        sent = _hv(request, "x-tapwright-policy")
        try:
            rules = policy.loads(sent, header=True) if sent else None
        except ValueError as e:
            return _error("bad_params", f"the policy this agent was started with is refused: {e}", 400)
        if ceiling is not None:
            rules, why = ceiling.check_policy(rules)  # the daemon's own policy when the call sent none
            if why is not None:
                return _error("not_allowed", why, HTTP_STATUS["not_allowed"])
        # The user's house rules (--ux-rules, AGENT-TOOLS §17.3): none sent is none; a bad set is refused
        sent_ux = _hv(request, "x-tapwright-ux-rules")
        try:
            ux_rules = uxcheck.loads(sent_ux) if sent_ux else None
        except ValueError as e:
            return _error("bad_params", f"the ux rules this agent was started with are refused: {e}", 400)
        # `tapwright call` says each call is a process of its own, for session_report; no other value is kept
        via: dict[str, str | bool] = {"via": "call"} if _hv(request, "x-tapwright-via") == "call" else {}
        if _hv(request, "x-tapwright-dialog") == "1":
            via["dialog"] = True  # the caller's host can show a dialog and is polling for it (dialog.py)
        if rules is not None and rules.file is not None and via.get("via") == "call" \
                and (ceiling is None or ceiling.policy is None or rules.file != ceiling.policy.file):
            # The model writes a thin call's command line: a grants file it names is never in force (§19.6: only a
            # person makes a grant). Only the policy file `tapwright serve` was started with brings grants.
            rules = dataclasses.replace(rules, file=None)
        # Where the per-app maps are kept (--maps, AGENT-TOOLS §18.9): an absolute folder; a daemon's own can't be moved
        sent_maps = _hv(request, "x-tapwright-maps")
        if sent_maps is not None and (not os.path.isabs(sent_maps) or "\x00" in sent_maps):
            return _error("bad_params", "--maps must be an absolute folder path", 400)
        if ceiling is not None:
            sent_maps, why = ceiling.check_maps(sent_maps)
            if why is not None:
                return _error("not_allowed", why, HTTP_STATUS["not_allowed"])
        if sent_maps is not None:
            via["maps"] = sent_maps
        # The test accounts file (--accounts, §18.6): only its path travels; a thin call may not name one serve wasn't given
        sent_accounts = _hv(request, "x-tapwright-accounts")
        if sent_accounts is not None and (not os.path.isabs(sent_accounts) or "\x00" in sent_accounts):
            return _error("bad_params", "--accounts must be an absolute file path", 400)
        thin_call = via.get("via") == "call"
        if ceiling is not None:
            accounts_path, refused = ceiling.check_accounts(sent_accounts, thin_call)
        elif sent_accounts is not None and thin_call:
            accounts_path, refused = None, ("bad_params", "--accounts is set when tapwright serve starts, not per call")
        else:
            accounts_path, refused = sent_accounts, None
        if refused is not None:
            return _error(refused[0], refused[1], HTTP_STATUS[refused[0]])
        if accounts_path is not None:
            via["accounts"] = accounts_path

        async def still_wanted() -> bool:
            # a client that gave up (its own timeout) while the call waited for the phone gets nothing done for it
            return not await request.is_disconnected()
        wanted = STILL_WANTED.set(still_wanted)
        try:
            return JSONResponse(await daemon.run_tool(client, request.path_params["name"], args, scope=scope,
                                                      policy=rules, ux_rules=ux_rules, **via))
        except Refused as e:
            return _error(e.code, str(e), HTTP_STATUS.get(e.code, 409))
        except Exception:  # the daemon turns tool failures into `internal` itself; this is the last net
            logging.getLogger("tapwright").exception("tool %s crashed", request.path_params["name"])
            return _error("internal", "tapwright serve failed unexpectedly; see ~/.tapwright/serve.log", 500)
        finally:
            STILL_WANTED.reset(wanted)

    async def dialog_body(request) -> tuple[str, dict] | Response:
        """The agent's client id and the JSON body of one of the host-dialog calls, or the refusal (dialog.py)."""
        problem = denied(request)
        if problem is not None:
            return problem
        client = _hv(request, "x-agent-client") or _hv(request, "x-tapwright-client")
        if not client:
            return _error("bad_params", "the host dialog's calls name their agent (X-Agent-Client)", 400)
        try:
            body = json.loads(await request.body() or b"{}")
        except (ValueError, RecursionError):
            return _error("bad_params", "the body must be JSON", 400)
        if not isinstance(body, dict):
            return _error("bad_params", "the body must be a JSON object", 400)
        return client, body

    async def dialog_poll(request):
        """The events for an agent's host after `since`: `ask` (show this) and `close` (take it down)."""
        got = await dialog_body(request)
        if isinstance(got, Response):
            return got
        client, body = got
        since, wait = body.get("since", 0), body.get("wait", 0)
        if not isinstance(since, int) or isinstance(since, bool) or not isinstance(wait, (int, float)) or isinstance(wait, bool):
            return _error("bad_params", "since is a whole number and wait is seconds", 400)
        return JSONResponse(await daemon.dialog_poll(client, since, float(wait)))

    async def dialog_answer(request):
        """The host's answer to an `ask`: only this agent's own open request counts."""
        got = await dialog_body(request)
        if isinstance(got, Response):
            return got
        client, body = got
        if not isinstance(body.get("id"), str):
            return _error("bad_params", "id is the ask's id", 400)
        return JSONResponse({"ok": daemon.dialog_answer(client, body["id"], body.get("answer"))})

    async def session(request):
        """`/api/session/ping` and `/api/session/end`: an agent's MCP process says it lives, or that it is going. A
        condition it set on the phone is kept while it is heard from, and cleared when it is gone."""
        problem = denied(request)
        if problem is not None:
            return problem
        client = _hv(request, "x-agent-client") or _hv(request, "x-tapwright-client")
        if request.path_params["what"] not in ("ping", "end"):
            return _error("not_found", "the session calls are ping and end", 404)
        if not client:
            return _error("bad_params", "no client named (X-Tapwright-Client)", 400)
        if request.path_params["what"] == "ping":
            daemon.agent_alive(client)
        else:
            await daemon.agent_ended(client)
        return JSONResponse({"ok": True})

    async def shutdown(request):
        problem = denied(request)
        if problem is not None:
            return problem
        if on_stop is not None:
            on_stop()
        return JSONResponse({"ok": True})

    async def stream(request):
        problem = denied(request, cookie=True)
        if problem is not None:
            return problem

        async def parts():
            async with daemon.pump.watching() as hub:
                seq, sent = 0, None
                yield b"--frame\r\n"  # each part then ends with the next boundary, so a browser shows it at once
                while True:
                    frame = await hub.next_after(seq, timeout=15)
                    if frame is None:
                        if hub.closed:
                            return
                        continue
                    seq = frame.seq
                    picture = getattr(frame, "picture", None)
                    if picture is not None and picture is sent:
                        continue  # a still screen's picture, published again for the settle: the viewer has it
                    sent = picture
                    w, h = daemon.session.screen
                    try:
                        jpeg = await jpeg_of(frame)  # a video picture is encoded off the loop, once
                    except PictureGone:
                        continue  # a newer frame is already there
                    turn = getattr(daemon.session, "turn", 0)
                    if turn and picture is None:  # landscape: the WebDriverAgent frame comes as captured, portrait
                        jpeg = await daemon.turned(frame.seq, jpeg, turn)
                    head = (f"Content-Type: image/jpeg\r\nContent-Length: {len(jpeg)}\r\n"
                            f"X-Seq: {frame.seq}\r\nX-Captured-At: {frame.at}\r\nX-Coverage: screen\r\n"
                            f"X-Source: wda\r\nX-Size: {round(w)}x{round(h)}\r\n\r\n")
                    yield head.encode() + jpeg + b"\r\n--frame\r\n"

        return StreamingResponse(parts(), media_type="multipart/x-mixed-replace; boundary=frame",
                                 headers={"Cache-Control": "no-store"})

    async def ws_allowed(websocket: WebSocket) -> bool:
        """The checks both WebSockets share: token (header or same-origin cookie), Host and Origin."""
        host = websocket.headers.get("host", "")
        origin = websocket.headers.get("origin")
        if denied(websocket, cookie=True) is not None or origin not in (None, f"http://{host}", f"https://{host}"):
            await websocket.close(code=4403)
            return False
        return True

    async def video(websocket: WebSocket):
        """H.264 pictures (VIEWER-PROTOCOL `WS /video`): the config, then binary pictures."""
        if not await ws_allowed(websocket):
            return
        await websocket.accept()
        hub = getattr(daemon, "video", None)
        if hub is None or not hub.live:  # accepted first, so a browser can read the code and use /stream
            await websocket.close(code=1013)
            return
        sub = hub.subscribe()

        async def send_all():  # the only writer on this socket
            while True:
                item = await sub.get()
                if item is None:
                    return
                if isinstance(item, dict):
                    await websocket.send_json(item)
                else:
                    await websocket.send_bytes(item)

        async def receive_all():
            while True:
                event = await websocket.receive()
                if event["type"] == "websocket.disconnect":
                    return
                try:
                    message = json.loads(event.get("text") or "")
                except (ValueError, RecursionError):
                    continue
                if isinstance(message, dict) and message.get("type") == "key":
                    hub.request_key()

        tasks = [asyncio.create_task(receive_all()), asyncio.create_task(send_all())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            sub.close()
            for task in tasks:
                task.cancel()
            await asyncio.wait(tasks)
            for task in tasks:
                if not task.cancelled():
                    task.exception()  # mark a failure as seen

    async def ws(websocket: WebSocket):
        if not await ws_allowed(websocket):
            return
        await websocket.accept()
        queue = daemon.subscribe()
        daemon.ws_clients += 1
        page = websocket.headers.get("origin") is not None  # ws_allowed checked it is this server's own
        if page:
            daemon.ws_pages += 1
        queue.put_nowait(daemon.status())
        for ask in getattr(daemon, "open_confirms", lambda: [])():  # a request made before this viewer came
            queue.put_nowait(ask)

        async def send_all():  # the only writer on this socket
            while True:
                await websocket.send_json(await queue.get())

        async def tick():
            while True:
                await asyncio.sleep(1.0)
                if not queue.full():
                    queue.put_nowait(daemon.status())

        async def receive_all():
            while True:
                event = await websocket.receive()
                if event["type"] == "websocket.disconnect":
                    return
                text = event.get("text")
                if text is None:
                    continue
                try:
                    message = json.loads(text)
                except (ValueError, RecursionError):
                    continue
                # a person's OK comes from the live view page only, which a browser sends with its Origin; a socket a
                # script opened has none (an agent holds the link). This guards against slips, not a determined agent.
                if isinstance(message, dict) and message.get("type") == "confirm_answer" and not page:
                    continue
                if isinstance(message, dict):
                    error = await daemon.viewer_message(message)
                    if error is not None and not queue.full():
                        queue.put_nowait(error)

        tasks = [asyncio.create_task(receive_all()), asyncio.create_task(send_all()),
                 asyncio.create_task(tick())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:  # whichever ends first, the rest stop
            daemon.ws_clients -= 1  # first: nothing below may skip it, even if this handler is cancelled
            if page:
                daemon.ws_pages -= 1
            daemon.unsubscribe(queue)
            for task in tasks:
                task.cancel()
            await asyncio.wait(tasks)  # not gather: under TestClient's cancel scope it re-raises CancelledError
            for task in tasks:
                if not task.cancelled():
                    task.exception()  # mark a failure as seen

    return Starlette(routes=[
        Route("/", index),
        Route("/viewer", index),  # the same page: both tools serve it at / and /viewer (AGENT-TOOLS v1.4)
        Route("/stream", stream),
        WebSocketRoute("/ws", ws),
        WebSocketRoute("/video", video),
        Route("/api/health", health),
        Route("/api/prove", prove, methods=["POST"]),  # (before /api/{name}: which is one word)
        Route("/api/shutdown", shutdown, methods=["POST"]),
        Route("/api/dialog_poll", dialog_poll, methods=["POST"]),
        Route("/api/dialog_answer", dialog_answer, methods=["POST"]),
        Route("/api/session/{what:str}", session, methods=["POST"]),  # (before /api/{name}: which is one word)
        Route("/api/{name}", tool, methods=["POST"]),
    ])


class _DropNoise(logging.Filter):
    """pymobiledevice3 logs its cancelled background readers as errors when a connection closes."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not (record.exc_info and record.exc_info[0] is asyncio.CancelledError)


class RedactAccounts(logging.Filter):
    """The log's last guard: every record's message, traceback and stack, whoever logged it, loses the values of the
    test accounts the daemon has loaded (`scrub`) before the handler writes it. (AGENT-TOOLS §18.6)"""

    def __init__(self, scrub) -> None:
        super().__init__()
        self.scrub = scrub

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg, record.args = self.scrub(record.getMessage()), None
            if record.exc_info and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = self.scrub(record.exc_text)
            if record.stack_info:
                record.stack_info = self.scrub(record.stack_info)
        except Exception:  # a log line that can't be checked is not written
            return False
        return True


def redact_log(scrub) -> RedactAccounts:
    """Put the guard on every handler of the root logger (what `configure_logging` set up); returns it."""
    guard = RedactAccounts(scrub)
    for handler in logging.getLogger().handlers:
        handler.addFilter(guard)
    return guard


def configure_logging(path) -> logging.Logger:
    """Everything to a rotating file (never the token); pymobiledevice3 only from warnings up."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(_DropNoise())
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("pymobiledevice3").setLevel(logging.WARNING)
    return logging.getLogger("tapwright")


def _bind(port: int) -> socket.socket | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name != "nt":  # on Windows this flag would let two servers share the port
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    else:
        # Windows lets a later socket with SO_REUSEADDR bind the same address and port alongside this one unless this
        # one claims it exclusively: a local process could then take some of the clients' connections, and a relay on
        # the very same address and port would pass the token proof (Tapline's Codex review, 1.6 security)
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        sock.close()
        return None
    return sock


async def _idle_watch(daemon, stop: asyncio.Event, limit: float, every: float = 30.0) -> None:
    while not stop.is_set():
        await asyncio.sleep(every)
        if daemon.pump.viewers == 0 and daemon.ws_clients == 0 and time.monotonic() - daemon.last_request > limit:
            daemon.log(f"no viewers or requests for {limit / 60:.0f} min: stopping")
            stop.set()


def _default_daemon(log, phone: str | None):
    from .daemon import Daemon
    from .device import Device
    from .session import Session
    from . import actionlog
    actions = actionlog.ActionLog(actionlog.default_folder() if actionlog.enabled() else None, report=log)
    from .auditlog import AuditLog
    # the audit log (§23) has no off switch: --no-log turns off the action log only
    return Daemon(Session(Device(host=phone, log=log)), log=log, actions=actions, audit=AuditLog())


def _lock_instance():
    """The lock that keeps one daemon running, held for its lifetime (the OS frees it if it dies).
    The open file, or None if another daemon holds it."""
    path = store.HOME / "serve.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


async def serve(port: int = DEFAULT_PORT, keep_alive: bool = False, new_token: bool = False,
                phone: str | None = None, make_daemon=_default_daemon, idle_limit: float = IDLE_LIMIT,
                wait_for_other: float = 60.0, video: bool = False, settle_on_video: bool = False,
                ceiling: Ceiling | None = None) -> int:
    """Run the daemon until it is stopped. A healthy daemon already running counts as success. `ceiling`: the most
    any call may have (scope, policy, ...); None, or one with nothing set, is no limit, as before."""
    import uvicorn

    from .client import healthy

    trace.enable_from_env(store.HOME / "trace.jsonl")  # TAPWRIGHT_TRACE=1: the timing trace, read once here

    # Nothing here prints the live view's link with its key: an app around the terminal that hides keys in what it
    # shows (T3 Code shows ?token=SECRET) opened that hidden copy as a browser tab each time a daemon started, and the
    # tab could only fail (2026-11-12). `tapwright view` opens the real link.
    def running(found: dict) -> int:
        print(f"already running on port {found.get('port')}; open the live view with `tapwright view`", flush=True)
        if new_token:
            print("the running daemon keeps its token; stop it first (tapwright serve --stop) to get a new one",
                  flush=True)
        if ceiling is not None and ceiling.is_set:  # the flags were not applied: the running daemon keeps its own
            print("the running daemon keeps the scope and policy it started with; stop it first (tapwright serve "
                  "--stop) to start one with these", flush=True)
        return 0

    info = store.read_serve_info()
    if await asyncio.to_thread(healthy, info):
        return running(info)
    lock = _lock_instance()
    if lock is None:  # another daemon is starting, or stuck
        deadline = time.monotonic() + wait_for_other
        while lock is None and time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            lock = _lock_instance()  # a stopping daemon frees it last; take over as soon as it does
            if lock is not None:
                info = store.read_serve_info()
                break
            info = store.read_serve_info()
            if await asyncio.to_thread(healthy, info):
                return running(info)
        if lock is None:
            print("another tapwright serve is starting or stuck; see ~/.tapwright/serve.log", flush=True)
            return 1
    log = logging.getLogger("tapwright")
    daemon = server = sock = serving = stopping = None
    helpers: list[asyncio.Task] = []
    stop = asyncio.Event()
    code = 0
    try:
        log = configure_logging(store.HOME / "serve.log")
        try:
            sock = _bind(port) or _bind(0)
            if sock is None:
                raise OSError("no free port on 127.0.0.1")
            token = info["token"] if info.get("token") and not new_token else secrets.token_urlsafe(32)
            actual = sock.getsockname()[1]
            daemon = make_daemon(log.info, phone)
            redact_log(daemon.scrub_everywhere)  # an account's value reaches serve.log in no message or traceback
            daemon.want_video = video  # the H.264 live view, set up once the phone is connected
            daemon.settle_on_video = video and settle_on_video
            daemon.ceiling = ceiling  # the rest of the flags (--no-dialog, --accounts) are read from it later
            app = create_app(daemon, token, LOCAL_HOSTS, on_stop=stop.set, ceiling=ceiling)
            server = uvicorn.Server(uvicorn.Config(app, ws="wsproto", lifespan="off", log_config=None,
                                                   access_log=False, timeout_graceful_shutdown=3))
            store.write_serve_info({"pid": os.getpid(), "port": actual, "token": token,
                                    "started": int(time.time())})
            log.info("listening on http://127.0.0.1:%d", actual)
            print(f"Tapwright is serving on port {actual}; open the live view with `tapwright view`", flush=True)
            serving = asyncio.create_task(server.serve(sockets=[sock]))
            helpers = [asyncio.create_task(daemon.supervise(stop))]
            if not keep_alive:
                helpers.append(asyncio.create_task(_idle_watch(daemon, stop, idle_limit)))
            stopping = asyncio.create_task(stop.wait())
            await asyncio.wait({serving, stopping}, return_when=asyncio.FIRST_COMPLETED)
            if serving.done() and not serving.cancelled() and serving.exception() is not None:
                log.error("the server stopped with an error", exc_info=serving.exception())
                code = 1
        except Exception:
            log.exception("tapwright serve failed")
            code = 1
    finally:  # also on Ctrl+C, which arrives as cancellation
        stop.set()
        if daemon is not None:
            daemon.hub.close()  # ends open stream responses, which would hold the server up
        if server is not None:
            server.should_exit = True
        tasks = [t for t in [*helpers, stopping] if t is not None]
        for task in tasks:
            task.cancel()
        waiting = [t for t in [serving, *tasks] if t is not None]
        if waiting:
            await asyncio.gather(*waiting, return_exceptions=True)
        if daemon is not None:
            try:
                await asyncio.shield(daemon.close())
            except Exception:
                log.exception("closing the phone session failed")
        if sock is not None:
            sock.close()
        lock.close()
        log.info("stopped")
    return code

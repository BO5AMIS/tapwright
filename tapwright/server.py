"""`tapwright serve`: the HTTP side of the daemon. The live view page, the stream, the viewer
WebSocket and the agent API, all behind one token; and starting, finding and stopping it.
Wire format: docs/VIEWER-PROTOCOL.md."""
import asyncio
import json
import logging
import os
import secrets
import socket
import time
from importlib.resources import files
from logging.handlers import RotatingFileHandler

from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

from . import policy, store
from .control import Refused
from .daemon import Scope
from .errors import HTTP_STATUS
from .stream import PictureGone, jpeg_of

COOKIE = "tapwright_token"
PAGE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})
DEFAULT_PORT = 8417
IDLE_LIMIT = 30 * 60  # seconds without viewers or requests before the daemon stops itself


def _host_ok(conn, hosts) -> bool:
    """The Host header names this server (against DNS rebinding)."""
    return conn.headers.get("host", "").split(":")[0] in hosts


def _same(given: str, token: str) -> bool:
    return bool(given) and secrets.compare_digest(given.encode(), token.encode())


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


def create_app(daemon, token: str, hosts=LOCAL_HOSTS, on_stop=None) -> Starlette:
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
            return HTMLResponse("<p>Open the link that <code>tapwright view</code> prints.</p>", status_code=401)
        if not _bearer_ok(request, token) and not _cookie_ok(request, token):
            return HTMLResponse("cross-site request refused", status_code=403)
        return HTMLResponse(page, headers=PAGE_HEADERS)

    async def health(request):
        return denied(request) or JSONResponse(daemon.status())

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
        client = request.headers.get("x-tapwright-client", "anonymous")
        # Personal mode unless the caller says the device is dedicated: a caller that names no apps gets none.
        apps = tuple(a.strip() for a in request.headers.get("x-tapwright-apps", "").split(",") if a.strip())
        scope = None if request.headers.get("x-tapwright-mode") == "dedicated" else Scope(apps)
        # The safety policy (docs/AGENT-TOOLS.md 11.1): none sent is the built-in one; a bad one is refused
        sent = request.headers.get("x-tapwright-policy")
        try:
            rules = policy.loads(sent) if sent else None
        except ValueError as e:
            return _error("bad_params", f"the policy this agent was started with is refused: {e}", 400)
        try:
            return JSONResponse(await daemon.run_tool(client, request.path_params["name"], args, scope=scope,
                                                      policy=rules))
        except Refused as e:
            return _error(e.code, str(e), HTTP_STATUS.get(e.code, 409))
        except Exception:  # the daemon turns tool failures into `internal` itself; this is the last net
            logging.getLogger("tapwright").exception("tool %s crashed", request.path_params["name"])
            return _error("internal", "tapwright serve failed unexpectedly; see ~/.tapwright/serve.log", 500)

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
        Route("/api/shutdown", shutdown, methods=["POST"]),
        Route("/api/{name}", tool, methods=["POST"]),
    ])


class _DropNoise(logging.Filter):
    """pymobiledevice3 logs its cancelled background readers as errors when a connection closes."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not (record.exc_info and record.exc_info[0] is asyncio.CancelledError)


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
    return Daemon(Session(Device(host=phone, log=log)), log=log, actions=actions)


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
                wait_for_other: float = 60.0, video: bool = False, settle_on_video: bool = False) -> int:
    """Run the daemon until it is stopped. A healthy daemon already running counts as success."""
    import uvicorn

    from .client import healthy, viewer_url

    def running(found: dict) -> int:
        print(f"already running: {viewer_url(found)}", flush=True)
        if new_token:
            print("the running daemon keeps its token; stop it first (tapwright serve --stop) to get a new one",
                  flush=True)
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
            daemon.want_video = video  # the H.264 live view, set up once the phone is connected
            daemon.settle_on_video = video and settle_on_video
            app = create_app(daemon, token, LOCAL_HOSTS, on_stop=stop.set)
            server = uvicorn.Server(uvicorn.Config(app, ws="wsproto", lifespan="off", log_config=None,
                                                   access_log=False, timeout_graceful_shutdown=3))
            store.write_serve_info({"pid": os.getpid(), "port": actual, "token": token,
                                    "started": int(time.time())})
            log.info("listening on http://127.0.0.1:%d", actual)
            print(f"Tapwright live view: http://127.0.0.1:{actual}/?token={token}", flush=True)
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

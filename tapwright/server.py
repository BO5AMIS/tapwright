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
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

from . import store
from .control import Refused

COOKIE = "tapwright_token"
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})
HTTP_STATUS = {"busy": 409, "paused": 409, "stale": 409, "reconnecting": 503, "locked": 503, "unsupported": 400}
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
            response = RedirectResponse("/", status_code=303)  # the token leaves the address bar and history
            response.set_cookie(COOKIE, token, httponly=True, samesite="strict")
            return response
        if not (_bearer_ok(request, token) or _cookie_present(request, token)):
            return HTMLResponse("<p>Open the link that <code>tapwright view</code> prints.</p>", status_code=401)
        if not _bearer_ok(request, token) and not _cookie_ok(request, token):
            return HTMLResponse("cross-site request refused", status_code=403)
        return HTMLResponse(page, headers={"Cache-Control": "no-store"})

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
            return _error("unsupported", "the body must be JSON", 400)
        if not isinstance(args, dict):
            return _error("unsupported", "the body must be a JSON object", 400)
        client = request.headers.get("x-tapwright-client", "anonymous")
        try:
            return JSONResponse(await daemon.run_tool(client, request.path_params["name"], args))
        except Refused as e:
            return _error(e.code, str(e), HTTP_STATUS.get(e.code, 409))
        except (ValueError, TypeError) as e:
            return _error("unsupported", f"bad arguments: {e}", 400)

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
                seq = 0
                while True:
                    frame = await hub.next_after(seq, timeout=15)
                    if frame is None:
                        if hub.closed:
                            return
                        continue
                    seq = frame.seq
                    w, h = daemon.session.screen
                    head = (f"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(frame.jpeg)}\r\n"
                            f"X-Seq: {frame.seq}\r\nX-Captured-At: {frame.at}\r\nX-Coverage: screen\r\n"
                            f"X-Source: wda\r\nX-Size: {round(w)}x{round(h)}\r\n\r\n")
                    yield head.encode() + frame.jpeg + b"\r\n"

        return StreamingResponse(parts(), media_type="multipart/x-mixed-replace; boundary=frame",
                                 headers={"Cache-Control": "no-store"})

    async def ws(websocket: WebSocket):
        host = websocket.headers.get("host", "")
        origin = websocket.headers.get("origin")
        if denied(websocket, cookie=True) is not None or origin not in (None, f"http://{host}", f"https://{host}"):
            await websocket.close(code=4403)
            return
        await websocket.accept()
        queue = daemon.subscribe()
        daemon.ws_clients += 1
        queue.put_nowait(daemon.status())

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
        Route("/stream", stream),
        WebSocketRoute("/ws", ws),
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
    return Daemon(Session(Device(host=phone, log=log)), log=log)


async def serve(port: int = DEFAULT_PORT, keep_alive: bool = False, new_token: bool = False,
                phone: str | None = None, make_daemon=_default_daemon, idle_limit: float = IDLE_LIMIT) -> int:
    """Run the daemon until it is stopped. A healthy daemon already running counts as success."""
    import uvicorn

    from .client import healthy, viewer_url
    info = store.read_serve_info()
    if await asyncio.to_thread(healthy, info):
        print(f"already running: {viewer_url(info)}", flush=True)
        return 0
    sock = _bind(port)
    if sock is None:
        for _ in range(20):  # another daemon may be starting on that port right now
            await asyncio.sleep(0.5)
            info = store.read_serve_info()
            if await asyncio.to_thread(healthy, info):
                print(f"already running: {viewer_url(info)}", flush=True)
                return 0
        sock = _bind(0)
    token = info["token"] if info.get("token") and not new_token else secrets.token_urlsafe(32)
    actual = sock.getsockname()[1]
    log = configure_logging(store.HOME / "serve.log")
    daemon = make_daemon(log.info, phone)
    stop = asyncio.Event()
    app = create_app(daemon, token, LOCAL_HOSTS, on_stop=stop.set)
    server = uvicorn.Server(uvicorn.Config(app, ws="wsproto", lifespan="off", log_config=None, access_log=False))
    store.write_serve_info({"pid": os.getpid(), "port": actual, "token": token, "started": int(time.time())})
    log.info("listening on http://127.0.0.1:%d", actual)
    print(f"Tapwright live view: http://127.0.0.1:{actual}/?token={token}", flush=True)
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    helpers = [asyncio.create_task(daemon.supervise(stop))]
    if not keep_alive:
        helpers.append(asyncio.create_task(_idle_watch(daemon, stop, idle_limit)))
    stopping = asyncio.create_task(stop.wait())
    await asyncio.wait({serving, stopping}, return_when=asyncio.FIRST_COMPLETED)
    stop.set()
    server.should_exit = True
    for task in helpers + [stopping]:
        task.cancel()
    await asyncio.gather(serving, *helpers, stopping, return_exceptions=True)
    await daemon.close()
    log.info("stopped")
    return 0

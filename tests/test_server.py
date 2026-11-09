"""The HTTP side with a fake session: page, token, API, stream, WebSocket."""
from importlib.resources import files

import pytest
from fakes import Clock, FakeSession
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tapwright.control import Controller
from tapwright.daemon import Daemon
from tapwright.server import COOKIE, create_app
from tapwright.stream import MjpegParser


def test_viewer_page_is_packaged():
    page = files("tapwright").joinpath("viewer.html")
    assert page.is_file()
    text = page.read_text(encoding="utf-8")
    assert "<title>Tapwright live view</title>" in text and 'id="screen"' in text and "new WebSocket" in text
    assert "history.replaceState" in text  # the token leaves the address bar
    assert "innerHTML" not in text  # server text only ever goes in as textContent


TOKEN = "t" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}", "X-Tapwright-Client": "agent-1"}


def app_and_daemon():
    clock = Clock()
    d = Daemon(FakeSession(), control=Controller(clock=clock, sleep=clock.sleep), log=lambda *a: None,
               stream_linger=0, stream_retry=0.01)
    d._set_state("live")
    return create_app(d, TOKEN, hosts={"testserver"}), d


def test_token_and_host_are_required():
    app, _ = app_and_daemon()
    with TestClient(app) as c:
        assert c.get("/api/health").status_code == 401
        assert c.get("/api/health", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert c.get("/api/health", headers=AUTH).json()["protocol"] == 1
        assert c.get("/api/health", headers={**AUTH, "host": "evil.example"}).status_code == 403
        assert c.get(f"/api/health?token={TOKEN}").status_code == 401  # never from the address


def test_api_ignores_the_cookie():
    app, _ = app_and_daemon()
    with TestClient(app) as c:
        c.get(f"/?token={TOKEN}", follow_redirects=False)  # the client now holds the cookie
        assert c.cookies.get(COOKIE) == TOKEN
        assert c.post("/api/tap", json={"target": {"x": 1, "y": 1}}).status_code == 401
        assert c.post("/api/tap", content=b'{"x": 1}', headers={"content-type": "text/plain"}).status_code == 401
        assert c.get("/api/health").status_code == 401


def test_cookie_requests_must_not_be_cross_site():
    app, d = app_and_daemon()
    d.hub.publish(b"\xff\xd8one")
    d.hub.close()
    with TestClient(app) as c:
        c.get(f"/?token={TOKEN}", follow_redirects=False)
        assert c.get("/stream", headers={"sec-fetch-site": "same-site"}).status_code == 403
        assert c.get("/", headers={"sec-fetch-site": "cross-site"}).status_code == 403
        assert c.get("/stream", headers={"sec-fetch-site": "same-origin"}).status_code == 200
        assert d.pump.viewers == 0
        assert c.get("/", headers={"sec-fetch-site": "none"}).status_code == 200


def test_shutdown_calls_on_stop():
    app, d = app_and_daemon()
    calls = []
    app = create_app(d, TOKEN, hosts={"testserver"}, on_stop=lambda: calls.append(1))
    with TestClient(app) as c:
        assert c.post("/api/shutdown").status_code == 401 and not calls
        assert c.post("/api/shutdown", headers=AUTH).json() == {"ok": True}
    assert calls == [1]


def test_json_body_must_be_an_object():
    app, _ = app_and_daemon()
    with TestClient(app) as c:
        assert c.post("/api/tap", headers=AUTH, content=b"[]").status_code == 400
        assert c.post("/api/tap", headers=AUTH, content=b"[" * 100000).status_code == 400


def test_page_moves_the_token_into_a_cookie():
    app, _ = app_and_daemon()
    with TestClient(app) as c:
        r = c.get(f"/?token={TOKEN}", follow_redirects=False)
        assert r.status_code == 200 and "Tapwright live view" in r.text  # the page itself, no redirect
        assert r.headers["referrer-policy"] == "no-referrer" and r.headers["cache-control"] == "no-store"
        cookie = r.headers["set-cookie"].lower()
        assert cookie.startswith(f"{COOKIE}=") and "httponly" in cookie and "samesite=strict" in cookie
        page = c.get("/")
        assert page.status_code == 200 and "Tapwright live view" in page.text
        assert page.headers["referrer-policy"] == "no-referrer"
    with TestClient(app) as stranger:
        assert stranger.get("/").status_code == 401


def test_token_link_clicked_on_another_site_still_works():
    app, _ = app_and_daemon()
    with TestClient(app) as c:
        r = c.get(f"/?token={TOKEN}", headers={"sec-fetch-site": "cross-site"})
        assert r.status_code == 200 and "httponly" in r.headers["set-cookie"].lower()
        assert c.get("/", headers={"sec-fetch-site": "same-origin"}).status_code == 200
        assert c.get("/", headers={"sec-fetch-site": "cross-site"}).status_code == 403


def test_agent_api():
    app, d = app_and_daemon()
    with TestClient(app) as c:
        look = c.post("/api/look", headers=AUTH, json={"screenshot": False})
        assert look.status_code == 200 and look.json()["text"].startswith("Screen")
        tap = c.post("/api/tap", headers=AUTH, json={"target": {"ref": "e1"}, "look": False})
        assert tap.json()["text"].startswith("Tapped e1")
        d.control.set_paused(True)
        refused = c.post("/api/tap", headers=AUTH, json={"target": {"x": 1, "y": 1}})
        assert refused.status_code == 409 and refused.json()["error"]["code"] == "paused"
        d.control.set_paused(False)
        assert c.post("/api/format_phone", headers=AUTH, json={}).json()["error"]["code"] == "unsupported"
        assert c.post("/api/tap", headers=AUTH, content=b"not json").status_code == 400


def test_stream_sends_the_newest_frame_with_its_headers():
    app, d = app_and_daemon()
    d.hub.publish(b"\xff\xd8old")
    d.hub.publish(b"\xff\xd8new")
    d.hub.close()
    with TestClient(app) as c:
        r = c.get("/stream", headers=AUTH)
    assert r.status_code == 200 and r.headers["content-type"].startswith("multipart/x-mixed-replace")
    body = r.content
    assert body.count(b"Content-Type: image/jpeg") == 1 and b"\xff\xd8new" in body and b"X-Seq: 2\r\n" in body
    assert b"X-Source: wda\r\n" in body and b"X-Coverage: screen\r\n" in body and b"X-Size: 400x800\r\n" in body


def test_stream_closes_each_part_with_the_next_boundary():
    """A browser <img> shows a frame when the next boundary arrives, so it must follow the JPEG at once."""
    app, d = app_and_daemon()
    d.hub.publish(b"\xff\xd8one")
    d.hub.close()
    with TestClient(app) as c:
        with c.stream("GET", "/stream", headers=AUTH) as r:
            chunks = list(r.iter_raw())
    body = b"".join(chunks)
    assert body.startswith(b"--frame\r\n") and body.endswith(b"\xff\xd8one\r\n--frame\r\n")
    assert body.count(b"--frame") == 2
    parser = MjpegParser()
    assert parser.feed(body) == [b"\xff\xd8one"]
    for chunk in chunks[1:]:
        assert chunk.endswith(b"\r\n--frame\r\n")


def test_websocket_status_pause_and_input():
    app, _ = app_and_daemon()
    with TestClient(app) as c, c.websocket_connect("/ws", headers=AUTH) as ws:
        first = ws.receive_json()
        assert first["type"] == "status" and first["protocol"] == 1 and first["state"] == "live"
        ws.send_bytes(b"x")  # odd frames are ignored
        ws.send_text("[" * 100000)
        ws.send_json({"type": "pause", "on": True})
        for _ in range(5):
            m = ws.receive_json()
            if m["type"] == "status" and m["paused"]:
                break
        else:
            pytest.fail("no status with paused")
        ws.send_json({"type": "gesture", "points": [[0, 0.5, 0.5], [50, 0.5, 0.5]]})
        for _ in range(5):
            m = ws.receive_json()
            if m["type"] == "action":
                break
        else:
            pytest.fail("no action message")
        assert (m["by"], m["kind"], m["point"]) == ("person", "tap", {"x": 0.5, "y": 0.5})
        ws.send_json({"type": "button", "name": "power"})
        for _ in range(5):
            m = ws.receive_json()
            if m["type"] == "error":
                break
        else:
            pytest.fail("no error message")
        assert m["code"] == "unsupported"


def test_websocket_cookie_origin_and_cleanup():
    app, d = app_and_daemon()
    with TestClient(app) as c:
        c.get(f"/?token={TOKEN}", follow_redirects=False)
        with c.websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:
            assert ws.receive_json()["type"] == "status"
            assert d.ws_clients == 1
    assert d.ws_clients == 0 and not d._queues


def test_websocket_refuses_strangers():
    app, d = app_and_daemon()
    with TestClient(app) as c:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws") as ws:
                ws.receive_json()
        with pytest.raises(WebSocketDisconnect):  # the token is never taken from the address
            with c.websocket_connect(f"/ws?token={TOKEN}") as ws:
                ws.receive_json()
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws", headers={**AUTH, "origin": "http://evil.example"}) as ws:
                ws.receive_json()
        c.get(f"/?token={TOKEN}", follow_redirects=False)
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws", headers={"sec-fetch-site": "same-site"}) as ws:
                ws.receive_json()
    assert d.ws_clients == 0


def test_every_code_has_an_http_status():
    from tapwright.errors import CODES
    from tapwright.server import HTTP_STATUS
    assert set(HTTP_STATUS) == set(CODES)
    assert (HTTP_STATUS["not_found"], HTTP_STATUS["ambiguous"], HTTP_STATUS["not_actionable"]) == (404, 409, 409)
    assert (HTTP_STATUS["bad_params"], HTTP_STATUS["not_connected"], HTTP_STATUS["internal"]) == (400, 503, 500)


def test_failures_come_back_with_a_code_and_a_status():
    app, d = app_and_daemon()
    with TestClient(app) as c:
        c.post("/api/look", headers=AUTH, json={"screenshot": False})
        gone = c.post("/api/tap", headers=AUTH, json={"target": {"ref": "e9"}, "look": False})
        assert gone.status_code == 404 and gone.json()["error"]["code"] == "not_found"
        assert "e9" in gone.json()["error"]["message"]
        bad = c.post("/api/type_text", headers=AUTH, json={})
        assert bad.status_code == 400 and bad.json()["error"]["code"] == "bad_params"
        assert c.post("/api/tap", headers=AUTH, content=b"[]").json()["error"]["code"] == "bad_params"
        d.session.fail["tap"] = RuntimeError("boom")
        crash = c.post("/api/tap", headers=AUTH, json={"target": {"x": 1, "y": 1}, "look": False})
        assert crash.status_code == 500 and crash.json()["error"]["code"] == "internal"
        d._set_state("no-device", "needs setup: run `tapwright setup`")
        none = c.post("/api/look", headers=AUTH, json={})
        assert none.status_code == 503 and none.json()["error"]["code"] == "not_connected"

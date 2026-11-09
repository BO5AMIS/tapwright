"""The HTTP side with a fake session: page, token, API, stream, WebSocket."""
from importlib.resources import files

import pytest
from fakes import Clock, FakeSession
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tapwright.control import Controller
from tapwright.daemon import Daemon
from tapwright.server import COOKIE, create_app


def test_viewer_page_is_packaged():
    page = files("tapwright").joinpath("viewer.html")
    assert page.is_file()
    text = page.read_text(encoding="utf-8")
    assert "<title>Tapwright live view</title>" in text and 'id="screen"' in text and "new WebSocket" in text
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
        assert c.get(f"/api/health?token={TOKEN}").status_code == 200


def test_page_moves_the_token_into_a_cookie():
    app, _ = app_and_daemon()
    with TestClient(app) as c:
        r = c.get(f"/?token={TOKEN}", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"
        cookie = r.headers["set-cookie"].lower()
        assert cookie.startswith(f"{COOKIE}=") and "httponly" in cookie and "samesite=strict" in cookie
        page = c.get("/")
        assert page.status_code == 200 and "Tapwright live view" in page.text
    with TestClient(app) as stranger:
        assert stranger.get("/").status_code == 401


def test_agent_api():
    app, d = app_and_daemon()
    with TestClient(app) as c:
        look = c.post("/api/look", headers=AUTH, json={"screenshot": False})
        assert look.status_code == 200 and look.json()["texts"][0].startswith("screen")
        tap = c.post("/api/tap", headers=AUTH, json={"ref": "e1", "look": False})
        assert tap.json()["texts"][0].startswith("tapped e1")
        d.control.set_paused(True)
        refused = c.post("/api/tap", headers=AUTH, json={"x": 1, "y": 1})
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
    assert body.count(b"--frame") == 1 and b"\xff\xd8new" in body and b"X-Seq: 2\r\n" in body
    assert b"X-Source: wda\r\n" in body and b"X-Coverage: screen\r\n" in body and b"X-Size: 400x800\r\n" in body


def test_websocket_status_pause_and_input():
    app, _ = app_and_daemon()
    with TestClient(app) as c, c.websocket_connect(f"/ws?token={TOKEN}") as ws:
        first = ws.receive_json()
        assert first["type"] == "status" and first["protocol"] == 1 and first["state"] == "live"
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
        assert (m["by"], m["kind"], m["point"]) == ("person", "tap", {"x": 0.5, "y": 0.5})
        ws.send_json({"type": "button", "name": "power"})
        for _ in range(5):
            m = ws.receive_json()
            if m["type"] == "error":
                break
        assert m["code"] == "unsupported"


def test_websocket_refuses_strangers():
    app, _ = app_and_daemon()
    with TestClient(app) as c:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws") as ws:
                ws.receive_json()
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect(f"/ws?token={TOKEN}", headers={"origin": "http://evil.example"}) as ws:
                ws.receive_json()

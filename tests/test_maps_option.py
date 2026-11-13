"""`--maps DIR` on `tapwright serve`, `tapwright mcp` and `tapwright call` (AGENT-TOOLS §18.9): where the per-app map
folders are kept. serve's is the daemon's root; a call's may be sent in a header, and may never move a root serve was
given."""
import subprocess
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from tapwright import client, thin
from tapwright.ceiling import Ceiling
from tapwright.server import create_app

TOKEN = "t" * 43
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Tapwright-Client": "agent-1"}
ANSWER = {"text": "ok", "image": None, "image_size": None}
ROOT = str(Path("maps-root").resolve())
OTHER = str(Path("other-root").resolve())


class Seen:
    def __init__(self):
        self.calls: list[dict] = []

    async def run_tool(self, client_id, name, args, scope=None, policy=None, ux_rules=None, **more):
        self.calls.append(more)
        return ANSWER

    def status(self):
        return {"protocol": 1}


def post(ceiling, headers):
    seen = Seen()
    app = create_app(seen, TOKEN, hosts={"testserver"}, ceiling=ceiling)
    with TestClient(app) as c:
        r = c.post("/api/look", headers={**HEADERS, **headers}, json={})
    return r, seen


def test_a_call_without_the_header_hands_the_daemon_no_root():
    r, seen = post(None, {})
    assert r.status_code == 200 and "maps" not in seen.calls[0]


def test_a_calls_root_is_handed_to_the_daemon():
    r, seen = post(None, {"X-Tapwright-Maps": ROOT})
    assert r.status_code == 200 and seen.calls[0]["maps"] == ROOT


def test_a_root_that_is_not_an_absolute_path_is_refused():
    r, _ = post(None, {"X-Tapwright-Maps": "relative/dir"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_params" and "--maps" in r.json()["error"]["message"]


def test_the_root_serve_was_given_is_the_calls_default_and_cannot_be_moved():
    ceiling = Ceiling(maps=ROOT)
    assert ceiling.is_set
    r, seen = post(ceiling, {})
    assert r.status_code == 200 and seen.calls[0]["maps"] == ROOT
    r, seen = post(ceiling, {"X-Tapwright-Maps": ROOT})
    assert r.status_code == 200 and seen.calls[0]["maps"] == ROOT
    r, _ = post(ceiling, {"X-Tapwright-Maps": OTHER})
    assert r.status_code == 403 and r.json()["error"]["code"] == "not_allowed"
    assert "over_ceiling" in r.json()["error"]["message"] and ROOT in r.json()["error"]["message"]


def test_the_mcp_servers_root_goes_with_each_call():
    try:
        client.set_maps(ROOT)
        assert client.SCOPE["X-Tapwright-Maps"] == ROOT
        client.set_maps(None)
        assert "X-Tapwright-Maps" not in client.SCOPE
        client.set_maps("relative/dir")  # made absolute, from where the server was started
        assert Path(client.SCOPE["X-Tapwright-Maps"]).is_absolute()
    finally:
        client.SCOPE.clear()


def test_the_thin_calls_root_is_an_absolute_header():
    a = thin._parser().parse_args(["look", "--maps", "some/dir"])
    h = thin._headers(a, "tok")
    assert Path(h["X-Tapwright-Maps"]).is_absolute() and h["X-Tapwright-Maps"].endswith("dir")
    assert "X-Tapwright-Maps" not in thin._headers(thin._parser().parse_args(["look"]), "tok")


@pytest.mark.parametrize("command", ["serve", "mcp", "call"])
def test_every_command_documents_maps(command):
    out = subprocess.run([sys.executable, "-m", "tapwright", command, "--help"], capture_output=True, text=True)
    assert out.returncode == 0 and "--maps" in out.stdout

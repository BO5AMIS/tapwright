"""`--accounts FILE` on `tapwright serve`, `tapwright mcp` and `tapwright call` (AGENT-TOOLS §18.6, §18.10): the path of the
test accounts file. serve's is the daemon's; the MCP server may send its own where serve has none; a thin call may never
widen what serve was started with."""
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
FILE = str(Path("accounts.json").resolve())
OTHER = str(Path("other-accounts.json").resolve())


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


def test_a_call_without_the_header_hands_the_daemon_no_path():
    r, seen = post(None, {})
    assert r.status_code == 200 and "accounts" not in seen.calls[0]


def test_the_mcp_servers_own_file_is_handed_on_where_serve_has_none():
    r, seen = post(None, {"X-Tapwright-Accounts": FILE})
    assert r.status_code == 200 and seen.calls[0]["accounts"] == FILE


def test_a_path_that_is_not_absolute_is_refused():
    r, _ = post(None, {"X-Tapwright-Accounts": "relative.json"})
    assert r.status_code == 400 and "--accounts" in r.json()["error"]["message"]


def test_the_file_serve_was_given_is_the_default_and_cannot_be_changed():
    ceiling = Ceiling(accounts=FILE)
    assert ceiling.is_set
    r, seen = post(ceiling, {})
    assert r.status_code == 200 and seen.calls[0]["accounts"] == FILE
    r, seen = post(ceiling, {"X-Tapwright-Accounts": FILE})
    assert r.status_code == 200 and seen.calls[0]["accounts"] == FILE
    r, _ = post(ceiling, {"X-Tapwright-Accounts": OTHER})
    assert r.status_code == 403 and "over_ceiling" in r.json()["error"]["message"]


def test_a_thin_call_may_not_name_a_file_serve_wasnt_started_with():
    call = {"X-Tapwright-Via": "call"}
    r, _ = post(None, {**call, "X-Tapwright-Accounts": FILE})
    assert r.status_code == 400 and r.json()["error"] == {
        "code": "bad_params", "message": "--accounts is set when tapwright serve starts, not per call"}
    r, _ = post(Ceiling(accounts=FILE), {**call, "X-Tapwright-Accounts": OTHER})
    assert r.status_code == 400 and "set when tapwright serve starts" in r.json()["error"]["message"]
    r, seen = post(Ceiling(accounts=FILE), {**call, "X-Tapwright-Accounts": FILE})
    assert r.status_code == 200 and seen.calls[0]["accounts"] == FILE


def test_the_mcp_servers_file_goes_with_each_call():
    try:
        client.set_accounts(FILE)
        assert client.SCOPE["X-Tapwright-Accounts"] == FILE
        client.set_accounts(None)
        assert "X-Tapwright-Accounts" not in client.SCOPE
        client.set_accounts("relative.json")
        assert Path(client.SCOPE["X-Tapwright-Accounts"]).is_absolute()
    finally:
        client.SCOPE.clear()


def test_the_thin_calls_file_is_an_absolute_header():
    h = thin._headers(thin._parser().parse_args(["look", "--accounts", "some/accounts.json"]), "tok")
    assert Path(h["X-Tapwright-Accounts"]).is_absolute() and h["X-Tapwright-Accounts"].endswith("accounts.json")
    assert "X-Tapwright-Accounts" not in thin._headers(thin._parser().parse_args(["look"]), "tok")


@pytest.mark.parametrize("command", ["serve", "mcp", "call"])
def test_every_command_documents_accounts(command):
    out = subprocess.run([sys.executable, "-m", "tapwright", command, "--help"], capture_output=True, text=True)
    assert out.returncode == 0 and "--accounts" in out.stdout


def test_doctor_reports_whether_the_accounts_file_can_be_used(tmp_path, capsys):
    import json

    from tapwright import cli
    good = tmp_path / "accounts.json"
    good.write_text(json.dumps({"version": 1, "accounts": {"tester2": {"username": "t@example.com", "password": "pw-pw-pw"}}}), "utf-8")
    assert cli.report_accounts(str(good)) == 0
    out = capsys.readouterr().out
    assert "tester2" in out and "pw-pw-pw" not in out and "t@example.com" not in out
    assert cli.report_accounts(str(tmp_path / "missing.json")) == 1
    assert "FAIL" in capsys.readouterr().out
    assert cli.report_accounts(None) == 0

"""The daemon's ceiling (AGENT-TOOLS §18.10, D1): what `tapwright serve` was started with (--apps, --dedicated, --policy,
--no-dialog, --accounts) is the most a call may have. With the thin call the model writes the flags, so a call may only
narrow them. A daemon started with none of them behaves as before, which keeps older clients working."""
import subprocess
import sys

import pytest
from starlette.testclient import TestClient

from tapwright import policy
from tapwright.ceiling import Ceiling
from tapwright.server import create_app

TOKEN = "t" * 43
BARE = {"Authorization": f"Bearer {TOKEN}", "X-Tapwright-Client": "agent-1"}
ANSWER = {"text": "ok", "image": None, "image_size": None}


class Seen:
    """A stand-in for the daemon that records what the route hands it."""

    def __init__(self):
        self.calls: list[dict] = []

    async def run_tool(self, client_id, name, args, scope=None, policy=None, ux_rules=None, **more):
        self.calls.append({"client": client_id, "name": name, "scope": scope, "policy": policy, **more})
        return ANSWER

    def status(self):
        return {"protocol": 1}


def app_for(ceiling=None):
    seen = Seen()
    return create_app(seen, TOKEN, hosts={"testserver"}, ceiling=ceiling), seen


def post(c, headers, tool="look"):
    return c.post(f"/api/{tool}", headers={**BARE, **headers}, json={})


def refusal(r):
    assert r.status_code == 403 and r.json()["error"]["code"] == "not_allowed", r.text
    return r.json()["error"]["message"]


# ---- scope ----









def test_a_dedicated_daemon_lets_a_call_name_any_apps_or_be_dedicated():
    app, seen = app_for(Ceiling(dedicated=True))
    with TestClient(app) as c:
        assert post(c, {"X-Tapwright-Mode": "dedicated"}).status_code == 200
        assert post(c, {"X-Tapwright-Apps": "com.any"}).status_code == 200




# ---- the client's name ----

def test_either_client_header_is_accepted_and_the_new_one_wins():
    app, seen = app_for()
    with TestClient(app) as c:
        post(c, {"X-Tapwright-Mode": "dedicated"})  # BARE carries only X-Tapwright-Client
        post(c, {"X-Tapwright-Mode": "dedicated", "X-Agent-Client": "new", "X-Tapwright-Client": "old"})
        c.post("/api/look", headers={"Authorization": f"Bearer {TOKEN}", "X-Agent-Client": "only-new",
                                     "X-Tapwright-Mode": "dedicated"}, json={})
        c.post("/api/look", headers={"Authorization": f"Bearer {TOKEN}", "X-Tapwright-Mode": "dedicated"}, json={})
    assert [s["client"] for s in seen.calls] == ["agent-1", "new", "only-new", "anonymous"]


def test_a_thin_call_says_so_and_the_route_hands_it_on_for_the_log():
    app, seen = app_for()
    with TestClient(app) as c:
        post(c, {"X-Tapwright-Mode": "dedicated", "X-Tapwright-Via": "call"})
        post(c, {"X-Tapwright-Mode": "dedicated"})
        post(c, {"X-Tapwright-Mode": "dedicated", "X-Tapwright-Via": "something else"})  # no other value is kept
    assert [s.get("via") for s in seen.calls] == ["call", None, None]


# ---- policy ----

DAEMON_POLICY = policy.Policy(confirm=("Buy", "Pay"), block=("Wipe",))


def sent(rules: policy.Policy) -> dict:
    return {"X-Tapwright-Mode": "dedicated", "X-Tapwright-Policy": rules.dumps()}










def test_words_are_compared_ignoring_case_and_spacing():
    app, _ = app_for(Ceiling(policy=policy.Policy(confirm=("Sign  out",))))
    with TestClient(app) as c:
        assert post(c, sent(policy.Policy(confirm=("sign out",)))).status_code == 200








# ---- the ceiling's own fields ----

def test_the_ceiling_keeps_no_dialog_and_the_accounts_path_for_later():
    c = Ceiling(apps=("com.a",), no_dialog=True, accounts="C:/x/accounts.json")
    assert c.no_dialog is True and c.accounts == "C:/x/accounts.json"
    assert Ceiling().no_dialog is False and Ceiling().accounts is None
    assert not Ceiling().is_set and Ceiling(no_dialog=True).is_set and Ceiling(accounts="a").is_set


# ---- `tapwright serve` takes the flags ----

def test_serve_lists_the_ceiling_flags():
    out = subprocess.run([sys.executable, "-m", "tapwright", "serve", "--help"], capture_output=True, text=True)
    assert out.returncode == 0 and all(f in out.stdout for f in ("--apps", "--dedicated", "--policy", "--no-dialog",
                                                                 "--accounts"))


def run_cli(monkeypatch, *argv):
    from tapwright import cli, device, server
    got: dict = {}

    def fake_serve(**kwargs):
        got.update(kwargs)
        return 0
    monkeypatch.setattr("sys.argv", ["tapwright", "serve", *argv])
    monkeypatch.setattr(server, "serve", fake_serve)
    monkeypatch.setattr(device, "run", lambda fn, *a: fn(*a))
    return cli.main(), got




def test_serve_dedicated_and_serve_with_no_flags(monkeypatch):
    _, got = run_cli(monkeypatch, "--dedicated")
    assert got["ceiling"].dedicated and got["ceiling"].apps == ()
    _, got = run_cli(monkeypatch)
    assert not got["ceiling"].is_set  # a daemon started as before




# ---- grants (AGENT-TOOLS §19.6: only a person makes a grant) ----

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

def test_a_call_may_name_the_apps_of_the_ceiling_or_fewer():
    app, seen = app_for(Ceiling(apps=("com.a", "com.b")))
    with TestClient(app) as c:
        assert post(c, {"X-Tapwright-Apps": "com.a,com.b"}).status_code == 200
        assert post(c, {"X-Tapwright-Apps": "com.b"}).status_code == 200
        assert post(c, {"X-Tapwright-Apps": " com.a , com.b "}).status_code == 200  # spaces around names don't matter
    assert [s["scope"].apps for s in seen.calls] == [("com.a", "com.b"), ("com.b",), ("com.a", "com.b")]


def test_a_call_naming_an_app_outside_the_ceiling_is_refused_and_the_message_names_the_ceiling():
    app, seen = app_for(Ceiling(apps=("com.a", "com.b")))
    with TestClient(app) as c:
        message = refusal(post(c, {"X-Tapwright-Apps": "com.c"}))
        assert "com.c" in message and "--apps com.a,com.b" in message and "narrow" in message
        refusal(post(c, {"X-Tapwright-Apps": "com.a,com.c"}))  # one outside is enough
    assert seen.calls == []  # nothing ran


def test_a_call_asking_for_dedicated_under_an_apps_ceiling_is_refused():
    app, seen = app_for(Ceiling(apps=("com.a",)))
    with TestClient(app) as c:
        message = refusal(post(c, {"X-Tapwright-Mode": "dedicated"}))
        assert "dedicated" in message and "--apps com.a" in message
        refusal(post(c, {"X-Tapwright-Mode": "dedicated", "X-Tapwright-Apps": "com.a"}))
    assert seen.calls == []


def test_a_call_naming_no_apps_under_a_ceiling_gets_none_as_before():
    app, seen = app_for(Ceiling(apps=("com.a",)))
    with TestClient(app) as c:
        assert post(c, {}).status_code == 200  # the daemon's own check says no_apps; the ceiling adds nothing
    assert seen.calls[0]["scope"].apps == ()


def test_a_dedicated_daemon_lets_a_call_name_any_apps_or_be_dedicated():
    app, seen = app_for(Ceiling(dedicated=True))
    with TestClient(app) as c:
        assert post(c, {"X-Tapwright-Mode": "dedicated"}).status_code == 200
        assert post(c, {"X-Tapwright-Apps": "com.any"}).status_code == 200


def test_a_daemon_started_without_a_ceiling_behaves_as_it_always_did():
    """A tester's 1.1.1 client: X-Tapwright-Client and X-Tapwright-Apps, no ceiling anywhere."""
    for ceiling in (None, Ceiling()):
        app, seen = app_for(ceiling)
        with TestClient(app) as c:
            assert post(c, {"X-Tapwright-Apps": "com.whatever"}).status_code == 200
            assert post(c, {"X-Tapwright-Mode": "dedicated"}).status_code == 200
            assert post(c, {}).status_code == 200
        assert [s["scope"] for s in seen.calls][1] is None and seen.calls[0]["scope"].apps == ("com.whatever",)
        assert seen.calls[0]["policy"] is None  # no daemon policy: the built-in one, as before


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


def test_a_call_with_no_policy_works_under_the_daemons_policy():
    app, seen = app_for(Ceiling(policy=DAEMON_POLICY))
    with TestClient(app) as c:
        assert post(c, {"X-Tapwright-Mode": "dedicated"}).status_code == 200
    assert seen.calls[0]["policy"] == DAEMON_POLICY  # not the built-in one


def test_a_call_may_add_words_to_the_daemons_policy():
    stricter = policy.Policy(confirm=("Buy", "Pay", "Share"), block=("Wipe", "Reset"))
    app, seen = app_for(Ceiling(policy=DAEMON_POLICY))
    with TestClient(app) as c:
        assert post(c, sent(stricter)).status_code == 200
        assert post(c, sent(DAEMON_POLICY)).status_code == 200  # the same is fine
    assert seen.calls[0]["policy"] == stricter and seen.calls[1]["policy"] == DAEMON_POLICY


def test_a_call_may_not_remove_a_word_of_the_daemons_policy():
    app, seen = app_for(Ceiling(policy=DAEMON_POLICY))
    with TestClient(app) as c:
        message = refusal(post(c, sent(policy.Policy(confirm=("Buy",), block=("Wipe",)))))
        assert "Pay" in message and "add words" in message and "confirm" in message
        message = refusal(post(c, sent(policy.Policy(confirm=("Buy", "Pay"), block=()))))
        assert "Wipe" in message and "block" in message
        refusal(post(c, sent(policy.NONE)))  # --no-policy is every word removed
        refusal(post(c, sent(policy.DEFAULT)))  # the built-in list isn't a superset of this one
    assert seen.calls == []


def test_a_word_may_move_from_confirm_to_block_but_not_the_other_way():
    app, _ = app_for(Ceiling(policy=DAEMON_POLICY))
    with TestClient(app) as c:
        assert post(c, sent(policy.Policy(confirm=("Pay",), block=("Wipe", "Buy")))).status_code == 200  # stricter
        refusal(post(c, sent(policy.Policy(confirm=("Buy", "Pay", "Wipe"), block=()))))  # a block turned into an OK


def test_words_are_compared_ignoring_case_and_spacing():
    app, _ = app_for(Ceiling(policy=policy.Policy(confirm=("Sign  out",))))
    with TestClient(app) as c:
        assert post(c, sent(policy.Policy(confirm=("sign out",)))).status_code == 200


def test_an_apps_own_words_are_held_to_the_daemons_for_that_app():
    floor = policy.Policy(confirm=("Buy",), apps={"com.shop": policy.AppWords(confirm=("Buy", "Checkout"))})
    app, _ = app_for(Ceiling(policy=floor))
    with TestClient(app) as c:
        # global words are enough for other apps, but for com.shop Checkout must still need an OK
        refusal(post(c, sent(policy.Policy(confirm=("Buy",)))))
        assert post(c, sent(policy.Policy(confirm=("Buy", "Checkout")))).status_code == 200
        assert post(c, sent(policy.Policy(confirm=("Buy",), apps={"com.shop": policy.AppWords(
            confirm=("Buy", "Checkout", "Tip"))}))).status_code == 200
        # an app list that turns a word off, for the app the daemon holds a word on
        refusal(post(c, sent(policy.Policy(confirm=("Buy", "Checkout"),
                                           apps={"com.shop": policy.AppWords(confirm=("Buy",))}))))


def test_a_daemon_without_a_policy_leaves_a_calls_policy_alone():
    app, seen = app_for(Ceiling(apps=("com.a",)))
    with TestClient(app) as c:
        assert post(c, {"X-Tapwright-Apps": "com.a", "X-Tapwright-Policy": policy.NONE.dumps()}).status_code == 200
    assert seen.calls[0]["policy"] == policy.NONE


def test_a_policy_the_daemon_cannot_read_is_still_bad_params():
    app, _ = app_for(Ceiling(policy=DAEMON_POLICY))
    with TestClient(app) as c:
        r = post(c, {"X-Tapwright-Mode": "dedicated", "X-Tapwright-Policy": "{nope"})
        assert r.status_code == 400 and r.json()["error"]["code"] == "bad_params"


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


def test_serve_hands_the_flags_to_the_server_as_a_ceiling(monkeypatch, tmp_path):
    pol = tmp_path / "policy.json"
    pol.write_text('{"confirm": ["Buy"], "block": ["Wipe"]}', encoding="utf-8")
    code, got = run_cli(monkeypatch, "--apps", "com.a, com.b", "--policy", str(pol), "--no-dialog",
                        "--accounts", str(tmp_path / "accounts.json"))
    assert code == 0
    ceiling = got["ceiling"]
    assert ceiling.apps == ("com.a", "com.b") and not ceiling.dedicated and ceiling.no_dialog
    assert ceiling.policy == policy.Policy(confirm=("Buy",), block=("Wipe",))
    assert ceiling.accounts == str(tmp_path / "accounts.json")


def test_serve_dedicated_and_serve_with_no_flags(monkeypatch):
    _, got = run_cli(monkeypatch, "--dedicated")
    assert got["ceiling"].dedicated and got["ceiling"].apps == ()
    _, got = run_cli(monkeypatch)
    assert not got["ceiling"].is_set  # a daemon started as before


def test_serve_refuses_apps_with_dedicated_and_a_policy_it_cannot_read(monkeypatch, tmp_path):
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, "--apps", "com.a", "--dedicated")
    with pytest.raises(SystemExit) as e:
        run_cli(monkeypatch, "--policy", str(tmp_path / "missing.json"))
    assert "policy" in str(e.value)


# ---- grants (AGENT-TOOLS §19.6: only a person makes a grant) ----

def test_a_thin_call_never_brings_a_grants_file_but_the_mcp_server_does():
    """The model writes a thin call's command line, so a policy file it names (even one that says `file`) brings no
    grants; the MCP server's own --policy, started by the person's host config, does."""
    rules = policy.Policy(confirm=("Post",), file="C:/someone/policy.json")
    for ceiling in (None, Ceiling(policy=policy.Policy(confirm=("Post",), file="C:/owner/policy.json"))):
        app, seen = app_for(ceiling)
        with TestClient(app) as c:
            post(c, {"X-Tapwright-Mode": "dedicated", "X-Tapwright-Via": "call", **sent(rules)})
            post(c, {"X-Tapwright-Mode": "dedicated", **sent(rules)})
        files = [s["policy"].file for s in seen.calls]
        assert files == [None, None if ceiling is not None else rules.file]
    owner = policy.Policy(confirm=("Post",), file="C:/owner/policy.json")
    app, seen = app_for(Ceiling(policy=owner))
    with TestClient(app) as c:
        post(c, {"X-Tapwright-Mode": "dedicated", "X-Tapwright-Via": "call"})  # none sent: serve's own, grants and all
    assert seen.calls[0]["policy"].file == owner.file

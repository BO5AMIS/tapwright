"""The safety policy (docs/AGENT-TOOLS.md section 11): words, the per-app override, the person's OK through the live
view, dry runs, and the policy's way from `tapwright mcp --policy` to the daemon."""
import asyncio
import json
import subprocess
import sys

import pytest
from fakes import Clock, FakeSession, button, el
from starlette.testclient import TestClient

from tapwright import client, policy
from tapwright.control import Controller, Refused
from tapwright.daemon import Daemon
from tapwright.server import create_app
from test_daemon import finish, make, run, started

TRIP = button("e1", "Delete trip", "trip-delete")  # a button whose label has a policy word
TRIP_LINE = 'button "Delete trip"'
NEEDS = ('needs_confirmation: ' + TRIP_LINE + ' needs a person\'s OK (policy word "Delete"). The live '
         "view asks them now; when they allow it, make the same call again. If nobody is watching, ask your user.")
TAP = {"target": {"label": "Delete trip"}, "look": False}


# ---- the words ----
@pytest.mark.parametrize("label, word", [
    ("Delete trip", "Delete"), ("delete", "Delete"), ("Buy now", "Buy"), ("Purchase", "Purchase"),
    ("Subscribe", "Subscribe"), ("Pay $5", "Pay"), ("Remove item", "Remove"), ("Send", "Send"),
    ("Post", "Post"), ("Publish", "Publish"), ("Sign out", "Sign out"), ("SIGN   OUT", "Sign out"),
    ("Please log out now", "Log out"), ("Erase all", "Erase"), ("(Delete)", "Delete")])
def test_each_default_word_matches_whole_words_ignoring_case(label, word):
    assert policy.DEFAULT.check("com.example.app", label) == ("confirm", word)


@pytest.mark.parametrize("label", ["Deleted", "Undelete", "Payment", "Sender", "Posting", "Sign", "Sign outs", "OK", "",
                                   "Removes", "Buyer"])
def test_a_near_miss_is_not_a_match(label):
    assert policy.DEFAULT.check("com.example.app", label) is None


@pytest.fixture(autouse=True)
def _known_request_ids(monkeypatch):
    """Request ids carry a random part so that only someone shown the request can answer it; here it is fixed."""
    from tapwright import daemon
    monkeypatch.setattr(daemon.secrets, "token_hex", lambda n=4: "x")


def test_a_block_word_wins_over_a_confirm_word_and_phrases_need_all_their_words():
    rules = policy.parse({"confirm": ["Delete"], "block": ["Delete account"]})
    assert rules.check(None, "Delete account") == ("blocked", "Delete account")
    assert rules.check(None, "Delete trip") == ("confirm", "Delete")
    assert rules.check(None, "Account") is None


def test_the_per_app_lists_replace_the_global_ones_for_that_app():
    rules = policy.parse({"confirm": ["Delete", "Buy"], "block": [],
                          "apps": {"com.example.test": {"confirm": [], "block": ["Delete account"]},
                                   "com.example.half": {"confirm": ["Send"]}}})
    assert rules.check("com.example.app", "Delete trip") == ("confirm", "Delete")
    assert rules.check("com.example.test", "Delete trip") is None  # its own confirm list is empty
    assert rules.check("com.example.test", "Buy") is None
    assert rules.check("com.example.test", "Delete account") == ("blocked", "Delete account")
    assert rules.check("com.example.app", "Delete account") == ("confirm", "Delete")  # the block is its own
    assert rules.check("com.example.half", "Delete") is None and rules.check("com.example.half", "Send") == ("confirm", "Send")
    assert rules.check("com.example.half", "Buy") is None


def test_no_policy_is_empty_lists():
    assert policy.NONE == policy.parse({"confirm": [], "block": []})
    assert policy.NONE.check("com.example.app", "Delete trip") is None


def test_the_policy_round_trips_through_json():
    rules = policy.parse({"version": 1, "confirm": ["Buy", "Sign out"], "block": ["Erase"],
                          "apps": {"com.example.app": {"confirm": [], "block": ["Delete account"]}}})
    assert policy.loads(rules.dumps()) == rules
    assert policy.loads(policy.DEFAULT.dumps()) == policy.DEFAULT
    assert policy.loads(policy.NONE.dumps()) == policy.NONE
    assert rules.dumps().isascii() and "\n" not in rules.dumps()


@pytest.mark.parametrize("bad, message", [
    ([], "must be a JSON object"), ({"confirm": "Delete"}, "confirm must be a list"),
    ({"confirm": ["Delete", 3]}, "only non-empty strings"), ({"block": [" "]}, "only non-empty strings"),
    ({"version": 3}, "version must be 1 or 2"), ({"grants": []}, "grants need version 2"), ({"colour": []}, "unknown key 'colour'"),
    ({"apps": []}, "apps must be an object"), ({"apps": {"a.b": []}}, "must be an object"),
    ({"apps": {"a.b": {"confirmm": []}}}, "unknown key 'confirmm'")])
def test_a_bad_policy_is_refused_with_a_clear_message(bad, message):
    with pytest.raises(ValueError, match=message):
        policy.parse(bad)


def test_a_bad_policy_file_is_refused_naming_the_file(tmp_path):
    for text, expected in (("not json", "isn't JSON"), ('{"confirm": 5}', "confirm must be a list")):
        path = tmp_path / "policy.json"
        path.write_text(text, encoding="utf-8")
        with pytest.raises(ValueError, match="policy.json") as e:
            policy.load(path)
        assert expected in str(e.value)
    with pytest.raises(ValueError, match="can't read the policy file"):
        policy.load(tmp_path / "missing.json")
    good = tmp_path / "good.json"
    good.write_text('{"confirm": ["Delete"]}', encoding="utf-8")
    assert policy.load(good).confirm == ("Delete",)




# ---- the daemon ----
async def ready(d: Daemon, tree=None) -> tuple:
    stop, task = await started(d)
    d.session.tree = tree or [TRIP]
    await d.run_tool("a", "look", {"screenshot": False})
    return stop, task


def sent(d: Daemon) -> list[dict]:
    """The messages broadcast to live views since the last call."""
    out = []
    while True:
        try:
            out.append(d._queues_seen.get_nowait())
        except asyncio.QueueEmpty:
            return out


async def refused(d: Daemon, tool: str, args: dict, **options) -> Refused:
    with pytest.raises(Refused) as e:
        await d.run_tool("a", tool, args, **options)
    return e.value
















def test_no_policy_and_the_per_app_override_let_the_touch_through():
    async def go():
        d = make()
        stop, task = await ready(d)
        r = await d.run_tool("a", "tap", TAP, policy=policy.NONE)
        assert r["text"].startswith("Tapped e1") and "Note:" not in r["text"]
        mine = policy.parse({"apps": {"com.example.app": {"confirm": [], "block": []}}, "confirm": ["Delete"]})
        await d.run_tool("a", "tap", TAP, policy=mine)
        assert d.session.calls.count("tap") == 2
        await finish(stop, task)
    run(go)


def test_the_testid_and_the_alert_are_never_matched():
    async def go():
        d = make()
        stop, task = await ready(d, [button("e1", "Trip", "delete-button")])
        r = await d.run_tool("a", "tap", {"target": {"id": "delete-button"}, "look": False})
        assert r["text"].startswith("Tapped e1")
        r = await d.run_tool("a", "alert", {"action": "accept", "look": False})
        assert "alert" in d.session.calls
        await finish(stop, task)
    run(go)




# ---- dry runs ----




def test_a_dry_run_of_the_other_tools_sends_nothing_to_the_phone():
    async def go():
        d = make()
        field = el("e2", "input", "Email", "email", rect=(10, 200, 200, 40))
        switch = el("e3", "switch", "Wi-Fi", "wifi", rect=(300, 100, 60, 30), checked=False)
        stop, task = await ready(d, [TRIP, field, switch])
        d.session.back_at = (30.0, 60.0)
        for tool, args, line in (
                ("swipe", {"x0": 10, "y0": 20, "x1": 10, "y1": 400}, "Would swipe from (10, 20) to (10, 400)."),
                ("type_text", {"text": "hello", "target": {"id": "email"}}, 'Would type 5 characters into e2 input "Email".'),
                ("toggle", {"target": {"id": "wifi"}}, 'Would toggle e3 switch "Wi-Fi" at 330,115.'),
                ("back", {}, "Would tap the back button."),
                ("scroll", {}, None), ("scroll", {"to": "end"}, None)):
            r = await d.run_tool("a", tool, {**args, "dry_run": True})
            assert r["text"].startswith("Would "), r["text"]
            if line:
                assert r["text"] == line
        assert r["text"].startswith("Would scroll down to the end: swipe from (")
        assert d.session.actions().count("tap") == 0 and not d.session.typed and not d.session.scrolls
        assert not [c for c in d.session.calls if c in ("tap", "swipe", "type_text", "dismiss_keyboard")]
        await finish(stop, task)
    run(go)


def test_dry_run_is_validated_and_belongs_to_the_touch_tools():
    async def go():
        d = make()
        stop, task = await ready(d)
        e = await refused(d, "tap", {**TAP, "dry_run": "yes"})
        assert e.code == "bad_params" and "dry_run must be true or false" in str(e)
        e = await refused(d, "open_app", {"app": "Example", "dry_run": True})
        assert e.code == "bad_params" and str(e) == "open_app doesn't take dry_run; it takes app, language, region, text_size, look, screenshot, brief."
        await finish(stop, task)
    run(go)


# ---- the header ----
def test_the_policy_goes_in_a_header_and_the_default_leaves_it_out():
    client.SCOPE.clear()
    client.set_policy(None)
    assert "X-Tapwright-Policy" not in client.SCOPE
    client.set_policy(policy.DEFAULT)
    assert "X-Tapwright-Policy" not in client.SCOPE
    custom = policy.parse({"confirm": ["Delete"], "block": ["Erase"]})
    client.set_policy(custom)
    assert policy.loads(client.SCOPE["X-Tapwright-Policy"]) == custom
    client.set_policy(policy.NONE)
    assert policy.loads(client.SCOPE["X-Tapwright-Policy"]) == policy.NONE
    client.set_policy(None)
    assert "X-Tapwright-Policy" not in client.SCOPE
    client.SCOPE.clear()


TOKEN = "t" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}", "X-Tapwright-Client": "agent-1", "X-Tapwright-Mode": "dedicated"}

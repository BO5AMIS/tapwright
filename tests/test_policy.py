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


def test_mcp_refuses_to_start_with_a_bad_policy_file(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"block": "x"}', encoding="utf-8")
    done = subprocess.run([sys.executable, "-m", "tapwright", "mcp", "--policy", str(path)], capture_output=True,
                          text=True, timeout=60)
    assert done.returncode != 0 and "block must be a list" in done.stderr
    both = subprocess.run([sys.executable, "-m", "tapwright", "mcp", "--policy", str(path), "--no-policy"],
                          capture_output=True, text=True, timeout=60)
    assert both.returncode != 0 and "not allowed with argument" in both.stderr


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


def test_a_block_word_refuses_with_the_words_of_the_spec_and_never_asks_a_person():
    async def go():
        d = make()
        d._queues_seen = d.subscribe()
        stop, task = await ready(d, [button("e1", "Delete account", "x")])
        rules = policy.parse({"block": ["Delete account"]})
        e = await refused(d, "tap", {"target": {"ref": "e1"}, "look": False}, policy=rules)
        assert e.code == "not_allowed"
        assert str(e) == ('blocked: button "Delete account" is blocked by the policy ("Delete account"); '
                          "don't try another way.")
        assert not [m for m in sent(d) if m["type"] == "confirm"] and "tap" not in d.session.calls
        await finish(stop, task)
    run(go)


def test_a_confirm_word_asks_the_live_view_and_the_retry_after_an_allow_passes_once():
    async def go():
        d = make()
        d._queues_seen = d.subscribe()
        stop, task = await ready(d)
        sent(d)
        e = await refused(d, "tap", TAP)
        assert e.code == "not_allowed" and str(e) == NEEDS and "tap" not in d.session.calls
        (ask,) = [m for m in sent(d) if m["type"] == "confirm"]
        assert (ask["id"], ask["by"], ask["kind"], ask["caption"], ask["word"]) == ("c1-x", "agent", "tap",
                                                                                      'Tap "Delete trip"', "Delete")
        assert isinstance(ask["expires"], int)
        await refused(d, "tap", TAP)  # asked again before an answer: no second request
        assert not [m for m in sent(d) if m["type"] == "confirm"]
        assert await d.viewer_message({"type": "confirm_answer", "id": "c1-x", "allow": True}) is None
        assert sent(d) == [{"type": "confirm_closed", "id": "c1-x", "allow": True}]
        await d.viewer_message({"type": "confirm_answer", "id": "c1-x", "allow": False})  # the first answer won
        assert sent(d) == []
        answer = await d.run_tool("a", "tap", TAP)
        assert answer["text"].splitlines()[-1] == "Note: allowed by a person (in the live view)." and d.session.calls.count("tap") == 1
        e = await refused(d, "tap", TAP)  # the OK was for one call: a second one needs a new one
        assert str(e) == NEEDS and "They declined" not in str(e)
        (again,) = [m for m in sent(d) if m["type"] == "confirm"]
        assert again["id"] == "c2-x"
        await finish(stop, task)
    run(go)


def test_an_allow_is_for_the_same_tool_and_the_same_element_only():
    async def go():
        d = make()
        d._queues_seen = d.subscribe()
        other = button("e2", "Remove photo", "photo-remove", rect=(10, 200, 100, 40))
        stop, task = await ready(d, [TRIP, other])
        await refused(d, "tap", TAP)
        await d.viewer_message({"type": "confirm_answer", "id": "c1-x", "allow": True})
        e = await refused(d, "tap", {"target": {"id": "photo-remove"}, "look": False})  # another element
        assert "needs_confirmation" in str(e) and "Remove photo" in str(e)
        e = await refused(d, "long_press", TAP)  # another tool
        assert "needs_confirmation" in str(e)
        assert (await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False}))["text"].endswith(
            "Note: allowed by a person (in the live view).")  # the same element, named another way
        await finish(stop, task)
    run(go)


def test_a_decline_and_an_expiry_are_said_on_the_retry_and_it_asks_again():
    async def go():
        d = make()
        d._queues_seen = d.subscribe()
        stop, task = await ready(d)
        await refused(d, "tap", TAP)
        await d.viewer_message({"type": "confirm_answer", "id": "c1-x", "allow": False})
        e = await refused(d, "tap", TAP)  # right after a no: no new request, ask the user (safety review L2)
        assert "they declined it just now" in str(e) and "ask your user" in str(e)
        assert [m["id"] for m in sent(d) if m["type"] == "confirm"] == ["c1-x"]
        d.control.clock.t += 61  # the decline held a minute: now it may be asked again
        e = await refused(d, "tap", TAP)
        assert str(e) == NEEDS + " They declined."
        assert [m["id"] for m in sent(d) if m["type"] == "confirm"] == ["c2-x"]
        d.control.clock.t += 121  # nobody answered c2
        await d.viewer_message({"type": "confirm_answer", "id": "c2-x", "allow": True})  # too late: ignored
        e = await refused(d, "tap", TAP)
        assert str(e) == NEEDS + " Nobody answered."
        assert [m["id"] for m in sent(d) if m["type"] == "confirm"] == ["c3-x"]
        await d.viewer_message({"type": "confirm_answer", "id": "c3-x", "allow": True})
        d.control.clock.t += 121  # an allow nobody used lapses
        e = await refused(d, "tap", TAP)
        assert str(e) == NEEDS and "tap" not in d.session.calls
        await d.viewer_message({"type": "confirm_answer", "id": "nope", "allow": True})  # an unknown id is ignored
        await d.viewer_message({"type": "confirm_answer", "allow": True})
        await finish(stop, task)
    run(go)


def test_force_does_not_skip_the_policy():
    async def go():
        d = make()
        d._queues_seen = d.subscribe()
        stop, task = await ready(d)
        e = await refused(d, "tap", {**TAP, "force": True})
        assert str(e) == NEEDS and "tap" not in d.session.calls
        blocked = policy.parse({"block": ["Delete"]})
        e = await refused(d, "tap", {**TAP, "force": True}, policy=blocked)
        assert str(e).startswith('blocked: button "Delete trip" is blocked') and "tap" not in d.session.calls
        await finish(stop, task)
    run(go)


def test_a_point_over_a_delete_button_is_checked_as_the_button():
    async def go():
        d = make()
        d._queues_seen = d.subscribe()
        stop, task = await ready(d)
        e = await refused(d, "tap", {"target": {"x": 60, "y": 120}, "look": False})
        assert str(e) == NEEDS and "tap" not in d.session.calls
        await d.viewer_message({"type": "confirm_answer", "id": "c1-x", "allow": True})
        answer = await d.run_tool("a", "tap", {"target": {"x": 60, "y": 120}, "look": False})
        assert d.session.calls.count("tap") == 1 and answer["text"].endswith("Note: allowed by a person (in the live view).")
        d.session.tap_points.clear()
        await d.run_tool("a", "tap", {"target": {"x": 300, "y": 600}, "look": False})  # nothing there: no word
        assert d.session.tap_points == [(300, 600)]
        await finish(stop, task)
    run(go)


def test_a_switch_is_checked_too_and_a_toggle_to_where_it_is_asks_nothing():
    async def go():
        d = make()
        d._queues_seen = d.subscribe()
        notify = el("e1", "switch", "Send notifications", "notify", rect=(300, 100, 60, 30), checked=False)
        stop, task = await ready(d, [notify])
        e = await refused(d, "toggle", {"target": {"id": "notify"}, "look": False})
        assert str(e).startswith('needs_confirmation: switch "Send notifications" needs a person\'s OK (policy word "Send")')
        assert "tap" not in d.session.calls
        r = await d.run_tool("a", "toggle", {"target": {"id": "notify"}, "value": False, "look": False})
        assert "already false" in r["text"]
        await d.viewer_message({"type": "confirm_answer", "id": "c1-x", "allow": True})
        r = await d.run_tool("a", "toggle", {"target": {"id": "notify"}, "look": False})
        assert d.session.calls.count("tap") == 1 and r["text"].endswith("Note: allowed by a person (in the live view).")
        await finish(stop, task)
    run(go)


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


def test_each_call_has_the_policy_it_came_with():
    async def go():
        d = make()
        stop, task = await ready(d)
        results = await asyncio.gather(d.run_tool("a", "tap", TAP, policy=policy.NONE),
                                       d.run_tool("a", "tap", TAP), return_exceptions=True)
        assert isinstance(results[1], Refused) and not isinstance(results[0], BaseException)
        await finish(stop, task)
    run(go)


# ---- dry runs ----
def test_a_dry_run_names_the_touch_and_the_policy_and_touches_nothing():
    async def go():
        d = make()
        d._queues_seen = d.subscribe()
        stop, task = await ready(d)
        sent(d)
        before = list(d.session.calls)
        r = await d.run_tool("a", "tap", {**TAP, "dry_run": True})
        assert r["text"] == 'Would tap e1 button "Delete trip" at 60,120. It needs a person\'s OK (policy word "Delete").'
        r = await d.run_tool("a", "long_press", {**TAP, "dry_run": True})
        assert r["text"].startswith('Would long-press e1 button "Delete trip" at 60,120.')
        r = await d.run_tool("a", "tap", {"target": {"x": 60, "y": 120}, "dry_run": True})
        assert r["text"] == 'Would tap at 60,120. It needs a person\'s OK (policy word "Delete").'
        r = await d.run_tool("a", "tap", {**TAP, "dry_run": True}, policy=policy.NONE)
        assert r["text"] == 'Would tap e1 button "Delete trip" at 60,120.'
        assert sent(d) == []  # no request, no action message
        assert [c for c in d.session.calls if c not in before and c in ("tap", "swipe", "type_text", "button:home")] == []
        e = await refused(d, "tap", {**TAP, "dry_run": True}, policy=policy.parse({"block": ["Delete"]}))
        assert str(e).startswith("blocked:")
        await refused(d, "tap", {"target": {"ref": "e1"}, "look": False})  # the ref is still good: nothing changed
        assert d.session.calls.count("tap") == 0 and sent(d)[0]["type"] == "confirm"
        await finish(stop, task)
    run(go)


def test_a_dry_run_does_not_use_up_a_persons_ok():
    async def go():
        d = make()
        stop, task = await ready(d)
        await refused(d, "tap", TAP)
        await d.viewer_message({"type": "confirm_answer", "id": "c1-x", "allow": True})
        r = await d.run_tool("a", "tap", {**TAP, "dry_run": True})
        assert r["text"] == 'Would tap e1 button "Delete trip" at 60,120.'  # it is allowed: nothing to add
        assert (await d.run_tool("a", "tap", TAP))["text"].endswith("Note: allowed by a person (in the live view).")
        await finish(stop, task)
    run(go)


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


def test_the_server_reads_the_header_and_refuses_a_bad_one():
    clock = Clock()
    d = Daemon(FakeSession(), control=Controller(clock=clock, sleep=clock.sleep), log=lambda *a: None,
               stream_linger=0, stream_retry=0.01)
    d._set_state("live")
    d.session.tree = [TRIP]
    app = create_app(d, TOKEN, hosts={"testserver"})
    with TestClient(app) as c:
        assert c.post("/api/look", headers=AUTH, json={"screenshot": False}).status_code == 200
        r = c.post("/api/tap", headers=AUTH, json=TAP)  # no header: the built-in policy
        assert r.status_code == 403 and r.json()["error"]["message"] == NEEDS
        off = {**AUTH, "X-Tapwright-Policy": policy.NONE.dumps()}
        r = c.post("/api/tap", headers=off, json=TAP)
        assert r.status_code == 200 and r.json()["text"].startswith("Tapped e1")
        for bad in ("{not json", '{"confirm": 5}', "[]"):
            r = c.post("/api/tap", headers={**AUTH, "X-Tapwright-Policy": bad}, json=TAP)
            assert r.status_code == 400 and r.json()["error"]["code"] == "bad_params"
            assert "policy" in r.json()["error"]["message"]
        assert json.dumps(d.session.calls.count("tap")) == "1"


def test_a_viewer_that_connects_while_a_request_is_open_is_sent_it():
    async def go():
        d = make()
        d._queues_seen = d.subscribe()
        stop, task = await ready(d)
        assert d.open_confirms() == []
        await refused(d, "tap", TAP)
        (ask,) = d.open_confirms()
        assert ask["type"] == "confirm" and ask["id"] == "c1-x" and ask["word"]
        await d.viewer_message({"type": "confirm_answer", "id": "c1-x", "allow": False})
        assert d.open_confirms() == []
        await finish(stop, task)
    run(go)


def test_an_alerts_policy_word_button_and_a_swipe_that_is_a_touch_go_through_the_policy():
    """Safety review H3 (2026-11-12): `alert accept` on an app's own "Delete trip?" alert, and a swipe that doesn't
    move, were touches the policy never saw. A permission prompt's Allow still goes straight through."""
    from tapwright import ui

    async def go():
        d = make()
        d._queues_seen = d.subscribe()
        stop, task = await ready(d)
        d.session.last_alert = ui.Alert("Delete trip?", ("Cancel", "Delete"), (50.0, 300.0, 300.0, 200.0))
        e = await refused(d, "alert", {"action": "accept", "look": False})
        assert "needs_confirmation" in str(e) and '"Delete"' in str(e)
        d.session.last_alert = ui.Alert("Allow notifications?", ("Allow", "Don't Allow"), (50.0, 300.0, 300.0, 200.0))
        await d.run_tool("a", "alert", {"action": "accept", "look": False})
        d.session.last_alert = None
        e = await refused(d, "swipe", {"x0": 50, "y0": 120, "x1": 52, "y1": 121, "look": False})
        assert "needs_confirmation" in str(e)
        await d.run_tool("a", "swipe", {"x0": 50, "y0": 120, "x1": 50, "y1": 400, "look": False})  # a real drag
        await finish(stop, task)
    run(go)


def test_the_explore_key_holds_a_skip_list_and_submit_ids_and_is_kept_when_the_policy_travels():
    rules = policy.parse({"explore": {"skip": ["Archive"], "submit": ["save-profile"], "future": 1}})
    assert rules.explore.skip == ("Archive",) and rules.explore.submit == ("save-profile",)
    assert policy.parse(json.loads(rules.dumps()), header=True).explore == rules.explore
    assert policy.parse({}).explore == policy.ExploreWords() and "explore" not in policy.DEFAULT.to_obj()


@pytest.mark.parametrize("bad", [[], {"skip": "Archive"}, {"skip": [""]}, {"submit": [3]}])
def test_a_bad_explore_key_is_refused(bad):
    with pytest.raises(ValueError, match="explore"):
        policy.parse({"explore": bad})

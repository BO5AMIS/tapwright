"""1.8.0 open items (W161). A caller's `explore`/`replay` policy is combined with `serve --policy` for the skip floor (every word of
both; a caller can only add), never put in its place. Explore's stop on a phone that reads as locked says that Notification Center
may be the cause, in 1.7.8's words."""
import pytest
from starlette.testclient import TestClient
from test_daemon import finish, make, run, started, until

from tapwright import explorefloor, policy as policylib
from tapwright.ceiling import Ceiling
from tapwright.control import Refused
from tapwright.daemon import Scope
from tapwright.server import create_app

TOKEN = "t" * 43
APPID = "com.example.app"
SERVE = policylib.parse({"block": ["Wipe"], "confirm": ["Publish"], "apps": {APPID: {"block": ["Shred"]}},
                         "explore": {"skip": ["Archive"], "submit": ["send-form"]}})
CALL = policylib.parse({"confirm": ["publish", "Transfer"], "apps": {APPID: {"confirm": ["Rewind"]}},
                        "explore": {"skip": ["Rename", "archive"]}})


# ---- the policy: combined, never replaced ----

def test_combined_keeps_every_word_of_both_once():
    both = policylib.combined(SERVE, CALL)
    assert both.block == ("Wipe",) and both.confirm == ("Publish", "Transfer")  # (once, ignoring case: the daemon's spelling)
    # (each side's words in force for the app: the call's own confirm list doesn't hide serve's global Publish there, PR #20)
    assert both.apps[APPID] == policylib.AppWords(confirm=("Publish", "Rewind"), block=("Shred",))
    assert both.explore.skip == ("Archive", "Rename") and both.explore.submit == ("send-form",)
    # a caller's policy that names nothing still leaves every word of the daemon's
    assert policylib.combined(SERVE, policylib.NONE).to_obj() == SERVE.to_obj()


def test_the_floor_of_the_combined_policy_skips_the_words_of_both():
    floor = explorefloor.Floor(policylib.combined(SERVE, CALL), APPID)
    assert floor.named("Archive trip").why == "skip_list" and floor.named("Rename trip").why == "skip_list"
    assert floor.named("Wipe all").why == "policy" and floor.named("Shred").why == "policy" and floor.named("Rewind").why == "policy"
    assert floor.named("Transfer").why == "policy" and floor.named("Open trip") is None


def _seen_by_the_daemon(ceiling, sent):
    d = make()
    seen = []

    async def run_tool(client_id, name, args, scope=None, policy=None, **via):
        seen.append((name, policy))
        return {"text": "ok", "image": None, "image_size": None}
    d.run_tool = run_tool
    head = {"Authorization": f"Bearer {TOKEN}", "X-Tapwright-Client": "agent-1", "X-Tapwright-Apps": APPID}
    if sent is not None:
        head["X-Tapwright-Policy"] = sent.dumps()
    with TestClient(create_app(d, TOKEN, hosts={"testserver"}, ceiling=ceiling)) as c:
        for tool, body in (("explore", {}), ("replay", {"path": "x.json", "repair": "apply"}), ("tap", {"target": {"id": "x"}})):
            assert c.post(f"/api/{tool}", headers=head, json=body).status_code == 200
    return dict(seen)


def test_a_callers_policy_is_combined_with_serve_policy_for_explore_and_replay():
    seen = _seen_by_the_daemon(Ceiling(policy=SERVE), CALL)
    for tool in ("explore", "replay"):
        rules = seen[tool]
        assert rules.explore.skip == ("Archive", "Rename"), tool  # serve's skip word is never lost
        assert rules.block == ("Wipe",) and "Transfer" in rules.confirm and rules.apps[APPID].block == ("Shred",), tool
    assert seen["tap"] is None  # (any other call: no floor, 1.7.5)
    # with no call policy serve's holds; with no serve policy the caller's does
    seen = _seen_by_the_daemon(Ceiling(policy=SERVE), None)
    assert seen["explore"].to_obj() == SERVE.to_obj()
    seen = _seen_by_the_daemon(Ceiling(), CALL)
    assert seen["explore"].to_obj() == CALL.to_obj()
    seen = _seen_by_the_daemon(None, CALL)
    assert seen["replay"].to_obj() == CALL.to_obj()


# ---- the words of a stop on a phone that reads as locked ----

LOCKED_WORDS = ("the phone reads as locked (or Notification Center is open over it): call unlock, or ask the user to unlock it")


def test_explore_stopped_by_a_phone_that_reads_as_locked_says_notification_center_may_be_open(tmp_path):
    from test_daemon_explore import explored

    async def go():
        d = explored(tmp_path)
        stop, task = await started(d)
        d.session.is_locked = True
        await until(lambda: d.state == "locked")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "explore", {}, Scope((APPID,)))
        assert e.value.code == "locked" and str(e.value).startswith(LOCKED_WORDS), str(e.value)
        await finish(stop, task)
    run(go)

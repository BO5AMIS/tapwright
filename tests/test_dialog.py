"""The host's dialog for a person's OK (AGENT-TOOLS 1.13 section 11.3), on the daemon's side: a call that came with a
dialog waits for the answer, asks the host through the client's channel, and goes on (or not) as the answer says."""
import asyncio
import json

import pytest
from tapwright.actionlog import ActionLog
from fakes import button, el
from starlette.testclient import TestClient

from tapwright import dialog, policy
from tapwright.control import Refused
from tapwright.server import create_app
from test_daemon import finish, make, run
from test_policy import NEEDS, TAP, TRIP, TRIP_LINE, ready, refused, sent  # noqa: F401  (the same screen and words)

NOTE_CHAT = "Note: allowed by a person (in chat)."
NOTE_VIEW = "Note: allowed by a person (in the live view)."
CAPTION = 'Allow: tap "Delete trip" in Example? (policy word "Delete")'


@pytest.fixture(autouse=True)
def _known_request_ids(monkeypatch):
    from tapwright import daemon
    monkeypatch.setattr(daemon.secrets, "token_hex", lambda n=4: "x")


async def asked(d, since: int = 0, client: str = "a") -> tuple[dict, int]:
    """The next event the host would be sent: (the event, the sequence number to poll from next)."""
    out = await d.dialog_poll(client, since, 2.0)
    assert out["events"], "the daemon asked the host nothing"
    return out["events"][0], out["seq"]


def test_a_call_with_a_dialog_waits_and_an_allow_in_chat_lets_it_through():
    async def go():
        d = make()
        d._queues_seen = d.subscribe()
        stop, task = await ready(d)
        sent(d)
        call = asyncio.create_task(d.run_tool("a", "tap", TAP, dialog=True))
        ask, seq = await asked(d)
        assert ask == {"type": "ask", "id": "c1-x", "kind": "confirm", "message": CAPTION,
                       "choices": ["Allow once", "Don't allow"]}
        assert [m["id"] for m in sent(d) if m["type"] == "confirm"] == ["c1-x"]  # the live view is asked too
        await asyncio.sleep(0.05)
        assert not call.done() and "tap" not in d.session.calls  # it waits; nothing is touched yet
        assert d.dialog_answer("a", "c1-x", "allow") is True
        answer = await asyncio.wait_for(call, 2)
        assert answer["text"].splitlines()[-1] == NOTE_CHAT and d.session.calls.count("tap") == 1
        assert [m for m in sent(d) if m["type"].startswith("confirm")] == [
            {"type": "confirm_closed", "id": "c1-x", "allow": True, "by": "chat"}]
        await finish(stop, task)
    run(go)


def test_the_live_view_answering_first_closes_the_dialog_and_the_note_says_so():
    async def go():
        d = make()
        d._queues_seen = d.subscribe()
        stop, task = await ready(d)
        call = asyncio.create_task(d.run_tool("a", "tap", TAP, dialog=True))
        ask, seq = await asked(d)
        await d.viewer_message({"type": "confirm_answer", "id": "c1-x", "allow": True})
        assert (await asked(d, seq))[0] == {"type": "close", "id": "c1-x"}  # the host's dialog is taken down
        answer = await asyncio.wait_for(call, 2)
        assert answer["text"].splitlines()[-1] == NOTE_VIEW
        assert d.dialog_answer("a", "c1-x", "allow") is False  # too late: the first answer won
        await finish(stop, task)
    run(go)


def test_the_dialog_answering_first_wins_over_a_later_live_view_answer():
    async def go():
        d = make()
        d._queues_seen = d.subscribe()
        stop, task = await ready(d)
        call = asyncio.create_task(d.run_tool("a", "tap", TAP, dialog=True))
        await asked(d)
        d.dialog_answer("a", "c1-x", "decline")
        await d.viewer_message({"type": "confirm_answer", "id": "c1-x", "allow": True})
        with pytest.raises(Refused) as e:
            await asyncio.wait_for(call, 2)
        assert str(e.value) == NEEDS + " They declined." and "tap" not in d.session.calls
        await finish(stop, task)
    run(go)


@pytest.mark.parametrize("value", ["decline", "cancel", "", "Allow once", "yes", None])
def test_only_allow_allows(value):
    async def go():
        d = make()
        stop, task = await ready(d)
        call = asyncio.create_task(d.run_tool("a", "tap", TAP, dialog=True))
        await asked(d)
        d.dialog_answer("a", "c1-x", value)
        with pytest.raises(Refused) as e:
            await asyncio.wait_for(call, 2)
        assert str(e.value) == NEEDS + " They declined." and "tap" not in d.session.calls
        await finish(stop, task)
    run(go)


def test_after_a_refusal_the_same_touch_asks_no_one_for_a_minute():
    async def go():
        d = make()
        d._queues_seen = d.subscribe()
        stop, task = await ready(d)
        call = asyncio.create_task(d.run_tool("a", "tap", TAP, dialog=True))
        await asked(d)
        d.dialog_answer("a", "c1-x", "decline")
        with pytest.raises(Refused):
            await call
        sent(d)
        e = await refused_with_dialog(d)
        assert "they declined it just now" in str(e) and not [m for m in sent(d) if m["type"] == "confirm"]
        assert (await d.dialog_poll("a", 1, 0))["events"] == []  # no new dialog either
        d.control.clock.t += 61
        call = asyncio.create_task(d.run_tool("a", "tap", TAP, dialog=True))
        ask, _ = await asked(d, 1)
        assert ask["id"] == "c2-x"  # asked again after a minute
        d.dialog_answer("a", "c2-x", "decline")
        with pytest.raises(Refused) as e:
            await call
        assert str(e.value) == NEEDS + " They declined."
        await finish(stop, task)
    run(go)


async def refused_with_dialog(d) -> Refused:
    with pytest.raises(Refused) as e:
        await asyncio.wait_for(d.run_tool("a", "tap", TAP, dialog=True), 2)
    return e.value


def test_nobody_answering_ends_it_after_the_limit_and_closes_the_dialog():
    async def go():
        d = make()
        held = []
        d.held = held.append  # (what a batch is told: the time spent waiting)
        stop, task = await ready(d)
        call = asyncio.create_task(d.run_tool("a", "tap", TAP, dialog=True))
        ask, seq = await asked(d)
        d.control.clock.t += 121
        with pytest.raises(Refused) as e:
            await asyncio.wait_for(call, 3)
        assert str(e.value) == NEEDS + " Nobody answered." and "tap" not in d.session.calls
        assert (await asked(d, seq))[0] == {"type": "close", "id": "c1-x"}
        assert held and 120 <= sum(held) <= 122
        assert d.dialog_answer("a", "c1-x", "allow") is False  # it ran out
        await finish(stop, task)
    run(go)


def test_an_allow_is_checked_again_and_a_changed_screen_asks_again():
    async def go():
        d = make()
        stop, task = await ready(d)
        call = asyncio.create_task(d.run_tool("a", "tap", TAP, dialog=True))
        await asked(d)
        d.session.tree = [button("e1", "Delete trip", "trip-delete", rect=(10, 300, 100, 40))]  # it moved while waiting
        d.dialog_answer("a", "c1-x", "allow")
        with pytest.raises(Refused) as e:
            await asyncio.wait_for(call, 2)
        assert e.value.code == "not_allowed"
        assert str(e.value) == "needs_confirmation: the screen changed while waiting for the OK; ask again."
        assert "tap" not in d.session.calls
        await finish(stop, task)
    run(go)


@pytest.mark.parametrize("after", [
    [],  # the element is gone
    [button("e1", "Remove trip", "other-id")],  # another element in its place
    [el("e1", "button", "Delete trip", "trip-delete", rect=(10, 100, 100, 40), enabled=False)]])  # not touchable now
def test_the_element_must_be_the_same_and_still_touchable_after_the_wait(after):
    async def go():
        d = make()
        stop, task = await ready(d)
        call = asyncio.create_task(d.run_tool("a", "tap", TAP, dialog=True))
        await asked(d)
        d.session.tree = after
        d.dialog_answer("a", "c1-x", "allow")
        with pytest.raises(Refused) as e:
            await asyncio.wait_for(call, 2)
        assert "the screen changed while waiting for the OK" in str(e.value) and "tap" not in d.session.calls
        await finish(stop, task)
    run(go)


def test_another_app_in_front_after_the_wait_asks_again():
    async def go():
        d = make()
        stop, task = await ready(d)
        call = asyncio.create_task(d.run_tool("a", "tap", TAP, dialog=True))
        await asked(d)
        d.session.foreground = "com.example.other"
        d.dialog_answer("a", "c1-x", "allow")
        with pytest.raises(Refused) as e:
            await asyncio.wait_for(call, 2)
        assert "the screen changed while waiting for the OK" in str(e.value) and "tap" not in d.session.calls
        await finish(stop, task)
    run(go)


def test_a_pause_while_the_ok_is_awaited_is_honoured_when_it_comes():
    """(Codex, PR #9 cycle 1) The live view pauses the phone while the dialog waits, then the person allows once: the
    touch does not go out; the call answers `paused`."""
    async def go():
        d = make()
        stop, task = await ready(d)
        call = asyncio.create_task(d.run_tool("a", "tap", TAP, dialog=True))
        await asked(d)
        assert await d.viewer_message({"type": "pause", "on": True}) is None
        d.dialog_answer("a", "c1-x", "allow")
        with pytest.raises(Refused) as e:
            await asyncio.wait_for(call, 2)
        assert e.value.code == "paused" and "tap" not in d.session.calls
        assert "c1-x" not in d._confirms  # the OK went with the call: a resume doesn't make it count for another
        await d.viewer_message({"type": "pause", "on": False})
        await finish(stop, task)
    run(go)


def test_the_live_view_can_use_the_phone_while_the_ok_is_awaited():
    """The wait doesn't hold the phone: a person's touch from the live view goes out meanwhile. After the OK the screen
    is read again, and the touch goes ahead on the same element."""
    async def go():
        d = make()
        stop, task = await ready(d)
        call = asyncio.create_task(d.run_tool("a", "tap", TAP, dialog=True))
        await asked(d)
        taps = d.session.calls.count("tap")
        error = await asyncio.wait_for(d.viewer_message({"type": "gesture", "points": [[0, 0.5, 0.9], [60, 0.5, 0.9]]}), 1)
        assert error is None and d.session.calls.count("tap") == taps + 1 and not call.done()  # the person's touch
        reads = d.session.calls.count("refresh")
        d.dialog_answer("a", "c1-x", "allow")
        answer = await asyncio.wait_for(call, 2)
        assert answer["text"].splitlines()[-1] == NOTE_CHAT and d.session.calls.count("tap") == taps + 2
        assert d.session.calls.count("refresh") > reads  # read again before the touch
        await finish(stop, task)
    run(go)


def test_after_the_wait_the_touch_goes_to_the_element_as_the_new_read_has_it():
    """The agent's ref names the element on its own list; after the wait the screen is read again, where another
    element came first (refs of a fresh read would number it e1). The touch still goes to the allowed element."""
    async def go():
        d = make()
        stop, task = await ready(d, [TRIP, button("e2", "Edit", "edit", rect=(10, 200, 100, 40))])
        call = asyncio.create_task(d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False}, dialog=True))
        await asked(d)
        d.session.tree = [button("e1", "Banner", "banner", rect=(10, 20, 100, 40)), *d.session.tree]
        d.dialog_answer("a", "c1-x", "allow")
        await asyncio.wait_for(call, 2)
        assert d.session.tap_points == [TRIP.center]
        await finish(stop, task)
    run(go)


DRAFT = button("e1", "Delete draft", "delete", rect=(10, 100, 100, 40))


@pytest.mark.parametrize("now", ["Delete account", "Delete all trips"])
def test_an_ok_is_for_the_controls_words_too_not_only_its_id(now):
    """(Codex, PR #9 cycle 1) A control keeps its id and place while its words change during the wait ("Delete draft"
    to "Delete account", which the policy blocks, or to other words it holds back): the OK for the first is none for
    it, and nothing is touched."""
    async def go():
        d = make()
        stop, task = await ready(d, [DRAFT])
        rules = policy.parse({"confirm": ["Delete"], "block": ["Delete account"]})
        call = asyncio.create_task(d.run_tool("a", "tap", {"target": {"id": "delete"}, "look": False}, policy=rules,
                                              dialog=True))
        await asked(d)
        d.session.tree = [button("e1", now, "delete", rect=(10, 100, 100, 40))]
        d.dialog_answer("a", "c1-x", "allow")
        with pytest.raises(Refused) as e:
            await asyncio.wait_for(call, 2)
        assert e.value.code == "not_allowed" and "tap" not in d.session.calls
        await finish(stop, task)
    run(go)


def test_a_live_view_ok_is_not_used_by_the_same_id_with_other_words():
    async def go():
        d = make()
        d._queues_seen = d.subscribe()
        stop, task = await ready(d, [DRAFT])
        tap = {"target": {"id": "delete"}, "look": False}
        with pytest.raises(Refused):
            await d.run_tool("a", "tap", tap)
        await d.viewer_message({"type": "confirm_answer", "id": "c1-x", "allow": True})
        d.session.tree = [button("e1", "Delete all trips", "delete", rect=(10, 100, 100, 40))]
        await d.run_tool("a", "look", {"screenshot": False})
        sent(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", tap)
        assert str(e.value).startswith('needs_confirmation: button "Delete all trips"') and "tap" not in d.session.calls
        assert [m["id"] for m in sent(d) if m["type"] == "confirm"] == ["c2-x"]  # asked anew, for the new words
        await finish(stop, task)
    run(go)


def test_a_blocked_touch_never_reaches_a_dialog():
    async def go():
        d = make()
        stop, task = await ready(d, [button("e1", "Delete account", "x")])
        rules = policy.parse({"block": ["Delete account"]})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False}, policy=rules, dialog=True)
        assert str(e.value).startswith('blocked: ') and (await d.dialog_poll("a", 0, 0))["events"] == []
        await finish(stop, task)
    run(go)


def test_a_call_without_a_dialog_answers_at_once_and_asks_the_host_nothing():
    async def go():
        d = make()
        stop, task = await ready(d)
        e = await refused(d, "tap", TAP)
        assert str(e) == NEEDS
        assert (await d.dialog_poll("a", 0, 0))["events"] == []
        await finish(stop, task)
    run(go)


def test_an_answer_needs_the_client_the_request_was_for():
    async def go():
        d = make()
        stop, task = await ready(d)
        call = asyncio.create_task(d.run_tool("a", "tap", TAP, dialog=True))
        await asked(d)
        assert d.dialog_answer("someone-else", "c1-x", "allow") is False
        assert d.dialog_answer("a", "nope", "allow") is False
        await asyncio.sleep(0.05)
        assert not call.done()
        assert d.dialog_answer("a", "c1-x", "allow") is True
        await asyncio.wait_for(call, 2)
        await finish(stop, task)
    run(go)


def test_a_dialog_that_vanishes_leaves_the_live_view_able_to_answer():
    async def go():
        d = make()
        d._queues_seen = d.subscribe()
        stop, task = await ready(d)
        call = asyncio.create_task(d.run_tool("a", "tap", TAP, dialog=True))
        await asked(d)
        call.cancel()  # the client gave up waiting
        with pytest.raises(asyncio.CancelledError):
            await call
        await d.viewer_message({"type": "confirm_answer", "id": "c1-x", "allow": True})
        answer = await d.run_tool("a", "tap", TAP)  # the retry goes through on the live view's OK
        assert answer["text"].splitlines()[-1] == NOTE_VIEW
        await finish(stop, task)
    run(go)


def test_the_caption_is_the_tools_own_words_with_the_apps_text_quoted_clipped_and_cleaned():
    async def go():
        d = make()
        stop, task = await ready(d, [button("e1", 'Delete\n"all"\x07‮ ' + "x" * 200, "x")])
        call = asyncio.create_task(d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False}, dialog=True))
        ask, _ = await asked(d)
        text = ask["message"]
        assert text.startswith("Allow: tap \"Delete 'all'") and text.endswith('" in Example? (policy word "Delete")')
        quoted = text[len("Allow: tap \""):text.index('" in Example')]
        assert len(quoted) <= 80 and quoted.endswith("…")
        assert all(ch.isprintable() for ch in text)
        d.dialog_answer("a", ask["id"], "decline")
        with pytest.raises(Refused):
            await call
        await finish(stop, task)
    run(go)


def test_the_other_touches_have_their_own_verbs():
    assert dialog.caption("long_press", "Delete trip", "Wayfare", "Delete") == 'Allow: long-press "Delete trip" in Wayfare? (policy word "Delete")'
    assert dialog.caption("toggle", "Send notifications", "Wayfare", "Send") == 'Allow: toggle "Send notifications" in Wayfare? (policy word "Send")'
    assert dialog.caption("alert", "Delete", None, "Delete") == 'Allow: press "Delete" in the app in front? (policy word "Delete")'
    assert dialog.caption("swipe", "Remove", "Wayfare", "Remove") == 'Allow: tap "Remove" in Wayfare? (policy word "Remove")'


def test_each_ok_names_where_it_came_from_in_the_action_log(tmp_path):
    async def go():
        d = make(actions=ActionLog(tmp_path))
        stop, task = await ready(d)
        call = asyncio.create_task(d.run_tool("a", "tap", TAP, dialog=True))
        await asked(d)
        d.dialog_answer("a", "c1-x", "allow")
        await asyncio.wait_for(call, 2)
        await d.viewer_message({"type": "confirm_answer", "id": "c9-x", "allow": True})
        await finish(stop, task)
        got = [json.loads(line) for f in tmp_path.glob("actions-*.jsonl") for line in f.read_text("utf-8").splitlines()]
        (done,) = [g for g in got if g["tool"] == "tap" and g["ok"]]
        assert done["confirmed"] is True and done["confirmed_by"] == "chat"
        refused_first = [g for g in got if g["tool"] == "tap" and not g["ok"]]
        assert refused_first == [] and "confirmed_by" not in next(g for g in got if g["tool"] == "look")
    run(go)


# ---- the HTTP routes ----
def client_for(d, token="secret"):
    return TestClient(create_app(d, token, hosts={"testserver"}), headers={"Authorization": f"Bearer {token}"})


def test_the_poll_and_the_answer_routes_need_the_token_and_the_client():
    d = make()
    d.dialog_channel("c").push({"type": "ask", "id": "q1", "kind": "confirm", "message": "m", "choices": []})
    with client_for(d) as http:
        assert http.post("/api/dialog_poll", json={}, headers={"Authorization": "Bearer wrong"}).status_code == 401
        got = http.post("/api/dialog_poll", json={"since": 0, "wait": 0}, headers={"X-Agent-Client": "c"})
        assert got.status_code == 200 and got.json()["events"][0]["id"] == "q1" and got.json()["seq"] == 1
        again = http.post("/api/dialog_poll", json={"since": 1, "wait": 0}, headers={"X-Agent-Client": "c"})
        assert again.json() == {"seq": 1, "events": []}
        none = http.post("/api/dialog_poll", json={"since": 0, "wait": 0})  # no client id
        assert none.status_code == 400
        bad = http.post("/api/dialog_answer", json={"id": "q1", "answer": "allow"}, headers={"Authorization": "Bearer wrong"})
        assert bad.status_code == 401
        miss = http.post("/api/dialog_answer", json={"id": "q1", "answer": "allow"}, headers={"X-Agent-Client": "c"})
        assert miss.status_code == 200 and miss.json() == {"ok": False}  # nothing is asking by that id
        junk = http.post("/api/dialog_answer", json={"id": 3}, headers={"X-Agent-Client": "c"})
        assert junk.status_code == 400


# ---- where each OK came from, in the reports ----
def test_session_report_says_where_each_ok_came_from():
    from test_report import entry
    from tapwright import report
    entries = [entry(0, "tap", confirmed=True, confirmed_by="chat"), entry(1, "tap", confirmed=True, confirmed_by="the live view"),
               entry(2, "tap", confirmed=True, confirmed_by="chat"), entry(3, "look")]
    text = report.build(entries, "c", None)
    assert "Approvals: 3 by a person (2 in chat, 1 in the live view)." in text.splitlines()
    assert "Approvals" not in report.build([entry(0, "tap")], "c", None)


def test_a_journey_counts_the_oks_and_its_report_names_where_they_came_from(tmp_path):
    import datetime
    from tapwright import journey, journey_report
    j = journey.Journey("goal", None, {}, (), "user", 0.0, datetime.datetime(2026, 1, 1), tmp_path)
    note = "\nNote: allowed by a person (in {}).".format
    for text in ("Tapped e1." + note("chat"), "Tapped e2.", "Tapped e3." + note("the live view"),
                 "Tapped e4." + note("chat")):
        j.note_call("tap", {}, text, 0.1, None)
    assert j.approvals == ["chat", "the live view", "chat"]
    notes = journey_report.model(j, True, None, 1.0, {}, tmp_path)["notes"]
    assert "A person allowed 3 touches: 2 in chat, 1 in the live view." in notes
    quiet = journey.Journey("goal", None, {}, (), "user", 0.0, datetime.datetime(2026, 1, 1), tmp_path)
    assert not [n for n in journey_report.model(quiet, True, None, 1.0, {}, tmp_path)["notes"] if "allowed" in n]

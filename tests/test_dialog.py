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












async def refused_with_dialog(d) -> Refused:
    with pytest.raises(Refused) as e:
        await asyncio.wait_for(d.run_tool("a", "tap", TAP, dialog=True), 2)
    return e.value




















DRAFT = button("e1", "Delete draft", "delete", rect=(10, 100, 100, 40))


















def test_the_other_touches_have_their_own_verbs():
    assert dialog.caption("long_press", "Delete trip", "Wayfare", "Delete") == 'Allow: long-press "Delete trip" in Wayfare? (policy word "Delete")'
    assert dialog.caption("toggle", "Send notifications", "Wayfare", "Send") == 'Allow: toggle "Send notifications" in Wayfare? (policy word "Send")'
    assert dialog.caption("alert", "Delete", None, "Delete") == 'Allow: press "Delete" in the app in front? (policy word "Delete")'
    assert dialog.caption("swipe", "Remove", "Wayfare", "Remove") == 'Allow: tap "Remove" in Wayfare? (policy word "Remove")'




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

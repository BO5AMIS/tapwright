"""A stop at a draft (AGENT-TOOLS 21.3) answers agents with the spec's own words and code (`Error not_allowed:
needs_confirmation: the draft wasn't sent ...`). The schedule run still has to tell it from any other
needs_confirmation, and does so by an INTERNAL field of the daemon's HTTP error JSON, never by the words, and no agent
(thin call, MCP server) ever sees that field."""
import asyncio
import json

import pytest
from fakes import Clock, FakeSession
from starlette.testclient import TestClient
from test_thin import TOKEN as THIN_TOKEN
from test_thin import fake, home, run as thin_run  # noqa: F401  (the thin call's fake daemon and its fixtures)

from tapwright import client, mcp_server, schedulerun
from tapwright.control import Controller, Refused
from tapwright.daemon import Daemon
from tapwright.server import create_app

TOKEN = "t" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}", "X-Tapwright-Client": "agent-1", "X-Tapwright-Mode": "dedicated"}
SPEC = "needs_confirmation: the draft wasn't sent (They declined.). Ask your user what to do."


def app_with(monkeypatch, refusal):
    clock = Clock()
    d = Daemon(FakeSession(), control=Controller(clock=clock, sleep=clock.sleep), log=lambda *a: None,
               stream_linger=0, stream_retry=0.01)
    d._set_state("live")

    async def refuse(*a, **k):
        raise refusal
    monkeypatch.setattr(d, "run_tool", refuse)
    return create_app(d, TOKEN, hosts={"testserver"})


def test_the_daemon_marks_a_draft_stop_in_its_error_json_and_keeps_the_specs_code_and_words(monkeypatch):
    with TestClient(app_with(monkeypatch, Refused("not_allowed", SPEC, draft=True))) as c:
        r = c.post("/api/tap", headers=AUTH, json={"target": {"x": 1, "y": 1}})
    assert r.status_code == 403
    assert r.json() == {"error": {"code": "not_allowed", "message": SPEC, "draft": True}}


def test_another_refusal_carries_no_draft_field(monkeypatch):
    with TestClient(app_with(monkeypatch, Refused("not_allowed", 'needs_confirmation: Allow tapping "Post"?'))) as c:
        r = c.post("/api/tap", headers=AUTH, json={"target": {"x": 1, "y": 1}})
    assert r.status_code == 403 and "draft" not in r.json()["error"]


def test_the_client_reads_the_flag_into_its_error(fake):
    fake.status = 403
    fake.reply = {"error": {"code": "not_allowed", "message": SPEC, "draft": True}}
    with pytest.raises(client.DaemonError) as e:
        client._request({"port": fake.port, "token": THIN_TOKEN}, "POST", "/api/tap", {}, 5.0, "t1")
    assert (e.value.code, str(e.value), e.value.draft) == ("not_allowed", SPEC, True)
    fake.reply = {"error": {"code": "not_allowed", "message": SPEC, "draft": "yes"}}  # only a real true counts
    with pytest.raises(client.DaemonError) as e:
        client._request({"port": fake.port, "token": THIN_TOKEN}, "POST", "/api/tap", {}, 5.0, "t1")
    assert e.value.draft is False
    fake.reply = {"error": {"code": "not_allowed", "message": 'needs_confirmation: Allow tapping "Post"?'}}
    with pytest.raises(client.DaemonError) as e:
        client._request({"port": fake.port, "token": THIN_TOKEN}, "POST", "/api/tap", {}, 5.0, "t1")
    assert e.value.draft is False


def test_the_thin_call_shows_the_spec_words_and_never_the_flag(fake, capsys):
    fake.status = 403
    fake.reply = {"error": {"code": "not_allowed", "message": SPEC, "draft": True}}
    code, out, err = thin_run(capsys, "tap", '{"target": {"text": "Post"}}', "--apps", "a.b")
    assert (code, out) == (2, "")
    assert err == f"Error not_allowed: {SPEC}\n"


def test_the_mcp_server_shows_the_spec_words_and_never_the_flag(monkeypatch):
    def refuse(tool, args, client_id, timeout=120.0):
        raise client.DaemonError("not_allowed", SPEC, draft=True)
    monkeypatch.setattr(client, "call", refuse)
    out = asyncio.run(mcp_server.server.call_tool("tap", {"target": {"text": "Post"}}))
    assert out.is_error
    assert out.content[0].text == f"Error not_allowed: {SPEC}"
    assert "draft_stopped" not in json.dumps(out.model_dump(), default=str)
    assert "True" not in json.dumps(out.model_dump(), default=str).replace('"isError": true', "")


@pytest.mark.parametrize("message,draft,result", [
    (SPEC, True, schedulerun.DRAFT),
    ("anything at all", True, schedulerun.DRAFT),  # the flag decides, not the words
    ('needs_confirmation: Allow deleting the draft "Trip notes"?', False, schedulerun.NEEDS_PERSON),
    (SPEC, False, schedulerun.NEEDS_PERSON),  # the words alone are not a draft stop
])
def test_the_schedule_run_tells_a_draft_stop_by_the_internal_signal(message, draft, result):
    assert schedulerun.classify("not_allowed", message, draft) == ("stopped", result)


def test_the_codes_are_the_specs():
    from tapwright import descriptions, errors
    assert "draft_stopped" not in errors.CODES and "draft_stopped" not in errors.HTTP_STATUS
    assert not hasattr(errors, "DRAFT_STOPPED")
    assert "draft_stopped" not in "".join(str(getattr(descriptions, n)) for n in dir(descriptions) if n.isupper() or n == "_CORE")

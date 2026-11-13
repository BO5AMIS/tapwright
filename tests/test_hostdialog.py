"""The MCP server's side of the host's dialog (AGENT-TOOLS 1.13 section 11.3, 11.5): a real MCP client and server in one
process, a client that shows elicitation forms and one that doesn't, and a stand-in for the daemon's channel."""
import asyncio
import threading
import time

import mcp_types as types
import pytest
from mcp.client import Client

from tapwright import client, hostdialog, mcp_server


class FakeDaemon:
    """What the MCP server talks to: `call` is a tool call that waits for the person's answer to what it pushed."""

    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.events: list[dict] = []
        self.answers: dict[str, str] = {}
        self.calls: list[tuple] = []
        self.to_ask: list[dict] = []  # asks the next call pushes, one after another

    def push(self, event: dict) -> None:
        with self.cond:
            self.events.append(event)
            self.cond.notify_all()

    def poll(self, since: int, wait: float) -> dict:
        deadline = time.monotonic() + min(wait, 0.3)
        with self.cond:
            while len(self.events) <= since and time.monotonic() < deadline:
                self.cond.wait(0.05)
            return {"seq": len(self.events), "events": self.events[since:]}

    def answer(self, ask_id: str, value: str) -> bool:
        with self.cond:
            self.answers[ask_id] = value
            self.cond.notify_all()
        return True

    def call(self, tool, args, client_id, timeout=120.0, **options):
        self.calls.append((tool, options, timeout))
        for ask in self.to_ask:
            self.push(ask)
        end = time.monotonic() + 5
        with self.cond:
            while not all(a["id"] in self.answers or self.closed(a["id"]) for a in self.to_ask) and time.monotonic() < end:
                self.cond.wait(0.05)
        verdicts = {a["id"]: self.answers.get(a["id"]) for a in self.to_ask}
        if any(v not in (None, "allow", "done") for v in verdicts.values()):
            raise client.DaemonError("not_allowed", "needs_confirmation: declined")
        return {"text": f"Done. {sorted(v for v in verdicts.values() if v)}"}

    def closed(self, ask_id: str) -> bool:
        return any(e.get("type") == "close" and e.get("id") == ask_id for e in self.events)


@pytest.fixture
def daemon(monkeypatch):
    fake = FakeDaemon()
    monkeypatch.setattr(client, "call", fake.call)
    monkeypatch.setattr(mcp_server, "DIALOGS", hostdialog.HostDialogs(poll=fake.poll, answer=fake.answer))
    monkeypatch.setattr(mcp_server, "_first_answer_notice", lambda: None)
    return fake


ASK = {"type": "ask", "id": "c1", "kind": "confirm", "message": 'Allow: tap "Delete trip" in Wayfare? (policy word "Delete")',
       "choices": ["Allow once", "Don't allow"]}


def run_tap(callback=None, **options):
    """One tap through a real MCP session; `callback` is the client's elicitation (None: a client that has none)."""
    async def go():
        async with Client(mcp_server.server, mode="legacy", elicitation_callback=callback, **options) as c:
            return await c.call_tool("tap", {"target": {"label": "Delete trip"}})
    return asyncio.run(go())


def form_answer(value):
    """An elicitation callback that records what it was shown and answers `value` (a dict) with `action`."""
    seen = []

    async def callback(context, params):
        seen.append(params)
        if isinstance(value, Exception):
            raise value
        action, content = value
        return types.ElicitResult(action=action, content=content)
    callback.seen = seen
    return callback


def text_of(result) -> str:
    return result.content[0].text


def test_the_host_is_asked_with_one_required_choice_and_an_allow_goes_back_as_allow(daemon):
    daemon.to_ask = [ASK]
    callback = form_answer(("accept", {"answer": "Allow once"}))
    result = run_tap(callback)
    assert text_of(result) == "Done. ['allow']" and not result.is_error
    (shown,) = callback.seen
    assert shown.message == ASK["message"]
    schema = shown.requested_schema
    assert schema["required"] == ["answer"] and list(schema["properties"]) == ["answer"]
    assert schema["properties"]["answer"]["enum"] == ["Allow once", "Don't allow"]
    assert "default" not in schema["properties"]["answer"]
    (_, options, timeout) = daemon.calls[0]
    assert options == {"dialog": True} and timeout > 300  # a person may take their time


@pytest.mark.parametrize("answer", [
    ("accept", {"answer": "Don't allow"}),
    ("decline", None),
    ("cancel", None),
    ("accept", {}),  # accepted, but nothing chosen
    ("accept", None),
    ("accept", {"answer": "Allow please"}),  # not one of the choices
    RuntimeError("the host failed to show it")])
def test_only_accept_with_allow_once_allows(daemon, answer):
    daemon.to_ask = [ASK]
    result = run_tap(form_answer(answer))
    assert result.is_error and daemon.answers == {"c1": "decline"}
    assert text_of(result).startswith("Error not_allowed: needs_confirmation")


def test_a_needs_a_person_ask_has_done_and_cant_now(daemon):
    daemon.to_ask = [{"type": "ask", "id": "n1", "kind": "needs", "message": "Needs a person: unlock the phone.",
                      "choices": ["Done", "Can't now"]}]
    callback = form_answer(("accept", {"answer": "Done"}))
    result = run_tap(callback)
    assert daemon.answers == {"n1": "done"} and not result.is_error
    assert callback.seen[0].requested_schema["properties"]["answer"]["enum"] == ["Done", "Can't now"]
    daemon.answers.clear()
    daemon.events.clear()
    mcp_server.DIALOGS._since = 0  # (the fake numbers from the start again)
    run_tap(form_answer(("accept", {"answer": "Can't now"})))
    assert daemon.answers == {"n1": "cant"}
    daemon.answers.clear()
    daemon.events.clear()
    mcp_server.DIALOGS._since = 0  # (the fake numbers from the start again)
    run_tap(form_answer(("decline", None)))
    assert daemon.answers == {"n1": "cant"}


def test_a_client_with_no_dialog_is_not_asked_and_the_call_says_so(daemon):
    daemon.to_ask = []
    result = run_tap(None)
    assert not result.is_error
    (_, options, timeout) = daemon.calls[0]
    assert options == {} and timeout <= 200  # today's call: no header, no wait for a person


def test_no_dialog_turns_it_off_even_for_a_client_that_has_one(daemon):
    mcp_server.DIALOGS.enabled = False
    callback = form_answer(("accept", {"answer": "Allow once"}))
    run_tap(callback)
    assert callback.seen == [] and daemon.calls[0][1] == {}


def test_a_close_takes_the_dialog_down_and_answers_nothing(daemon):
    daemon.to_ask = [ASK]
    released = threading.Event()
    started = threading.Event()
    state = {"cancelled": False}

    async def callback(context, params):
        started.set()
        try:
            while not released.is_set():
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise
        return types.ElicitResult(action="decline")

    def later():  # the live view answered first: the daemon sends a close
        started.wait(5)
        daemon.push({"type": "close", "id": "c1"})
        time.sleep(0.3)
        released.set()
    threading.Thread(target=later, daemon=True).start()
    result = run_tap(callback)
    assert not result.is_error and daemon.answers == {}  # the call ended without any answer from the host
    assert state["cancelled"] or released.is_set()


def test_one_dialog_is_up_at_a_time_and_the_second_waits_behind_the_first(daemon):
    second = {**ASK, "id": "c2", "message": "Allow: tap \"Remove photo\" in Wayfare? (policy word \"Remove\")"}
    daemon.to_ask = [ASK, second]
    up, most = [], [0]

    async def callback(context, params):
        up.append(params.message)
        most[0] = max(most[0], len(up))
        await asyncio.sleep(0.15)
        up.remove(params.message)
        return types.ElicitResult(action="accept", content={"answer": "Allow once"})
    result = run_tap(callback)
    assert not result.is_error and most[0] == 1
    assert daemon.answers == {"c1": "allow", "c2": "allow"}


def test_the_poll_starts_only_with_a_call_and_ends_with_it(daemon):
    polled = []
    real = daemon.poll
    daemon.poll = lambda since, wait: (polled.append(since), real(since, wait))[1]
    mcp_server.DIALOGS._poll = daemon.poll
    daemon.to_ask = [ASK]  # the call stays up until the dialog is answered, so the poll has certainly started (a call
    # with nothing to ask can end before the pump's first poll is sent: that raced on a slow or coarse clock)
    run_tap(form_answer(("accept", {"answer": "Allow once"})))
    seen = len(polled)  # (asyncio.run waited for any poll still out in its thread, so this count is final)
    assert seen >= 1
    time.sleep(0.8)  # more than two poll periods (0.3 s each)
    assert len(polled) == seen  # the call ended: no poll starts after it


def test_a_daemon_that_started_again_numbers_events_from_the_start(daemon):
    hd = mcp_server.DIALOGS
    hd._since = 7  # the old daemon had sent seven
    daemon.to_ask = [ASK]
    result = run_tap(form_answer(("accept", {"answer": "Allow once"})))
    assert not result.is_error and daemon.answers == {"c1": "allow"}


# What the SDK does with the two limits the Tapline team (TypeScript SDK) found. Both are pinned here so a later
# `mcp` upgrade can't silently break the dialog: (a) a client that declares `elicitation: {}` (older clients mean form
# mode) must still get the dialog; (b) the server's elicitation request must not time out before the 120 s wait.

def run_tap_with_capability(monkeypatch, capability, callback):
    """One tap through a client that declares `capability` as its elicitation capability, whatever its callback."""
    from mcp.client.session import ClientSession
    real = ClientSession._build_capabilities

    def build(self, version):
        return real(self, version).model_copy(update={"elicitation": capability})
    monkeypatch.setattr(ClientSession, "_build_capabilities", build)
    return run_tap(callback)


@pytest.mark.parametrize("capability", [
    types.ElicitationCapability(),  # `elicitation: {}`
    types.ElicitationCapability(form=types.FormElicitationCapability()),
    types.ElicitationCapability(form=types.FormElicitationCapability(), url=types.UrlElicitationCapability())])
def test_a_client_declaring_empty_elicitation_gets_the_dialog_as_form_mode(daemon, monkeypatch, capability):
    daemon.to_ask = [ASK]
    callback = form_answer(("accept", {"answer": "Allow once"}))
    result = run_tap_with_capability(monkeypatch, capability, callback)
    assert not result.is_error and daemon.answers == {"c1": "allow"}
    assert daemon.calls[0][1] == {"dialog": True} and len(callback.seen) == 1


def test_a_client_declaring_no_elicitation_at_all_is_still_not_asked(daemon, monkeypatch):
    daemon.to_ask = []
    result = run_tap_with_capability(monkeypatch, None, form_answer(("accept", {"answer": "Allow once"})))
    assert not result.is_error and daemon.calls[0][1] == {}


def test_a_person_taking_a_long_time_still_answers_and_no_sdk_timeout_cuts_the_request_short(daemon, monkeypatch):
    from mcp.server.session import ServerSession
    timeouts = []
    real = ServerSession.send_request

    async def spy(self, request, result_type, request_read_timeout_seconds=None, *args, **kwargs):
        if request.method == "elicitation/create":
            timeouts.append(request_read_timeout_seconds)
        return await real(self, request, result_type, request_read_timeout_seconds, *args, **kwargs)
    monkeypatch.setattr(ServerSession, "send_request", spy)
    daemon.to_ask = [ASK]

    async def slow(context, params):
        await asyncio.sleep(1.5)  # stands for a person taking 90 s: nothing in the path times out in between
        return types.ElicitResult(action="accept", content={"answer": "Allow once"})
    result = run_tap(slow)
    assert not result.is_error and daemon.answers == {"c1": "allow"}
    # the SDK sets no timeout of its own (None = wait), or ours is past the 120 s wait
    assert timeouts and all(t is None or t >= 120 + 30 for t in timeouts)

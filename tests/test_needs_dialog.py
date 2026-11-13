"""Needs a person (AGENT-TOOLS 1.13 section 11.5): a call that can't go on without a person asks through the host's
dialog, waits up to 120 s, and goes on by itself when the moment clears; `Can't now` or the limit is today's error."""
import asyncio

import pytest
from fakes import FakeSession, button
from test_daemon import finish, make, run, started, until_stuck

from tapwright import needs
from tapwright.control import Refused
from tapwright.daemon import Scope
from tapwright.device import PasscodeNeeded
from test_dialog import _known_request_ids  # noqa: F401  (fixed request ids)
from test_dialog import asked

UNLOCK = "Needs a person: unlock the phone. I'll go on by myself when it's unlocked."
TAP = {"target": {"x": 5, "y": 5}, "look": False}


def test_the_messages_are_the_tools_own_words():
    assert needs.message(needs.UNLOCK) == UNLOCK
    assert needs.message(needs.PASSCODE_PROMPT).startswith("Needs a person: enter the passcode on the phone")
    assert needs.message(needs.PASSCODE_PROMPT).endswith("I'll go on by myself when it's in.")
    assert needs.message(needs.HEALTH_SHEET) == ("Needs a person: choose in iOS's Health sheet and tap Allow (iOS doesn't "
                                                 "let automation change it). I'll go on by myself when the sheet is gone.")
    assert needs.message(needs.IOS_ALERT) == ("Needs a person: answer iOS's alert on the phone. I'll go on by myself when "
                                              "it's gone.")


async def locked(d_s: FakeSession | None = None):
    s = d_s or FakeSession()
    s.is_locked = True
    d = make(s)
    stop, task = await started(d, until=("locked",))
    return s, d, stop, task


def test_a_locked_phone_asks_and_goes_on_by_itself_when_it_is_unlocked():
    async def go():
        s, d, stop, task = await locked()
        call = asyncio.create_task(d.run_tool("a", "tap", TAP, dialog=True))
        ask, seq = await asked(d)
        assert ask == {"type": "ask", "id": "n1-x", "kind": "needs", "message": UNLOCK, "choices": ["Done", "Can't now"]}
        await asyncio.sleep(0.1)
        assert not call.done()
        s.is_locked = False  # the person unlocked it
        answer = await asyncio.wait_for(call, 5)
        assert answer["text"].startswith("Tapped at") or "Tapped" in answer["text"]
        assert (await asked(d, seq))[0] == {"type": "close", "id": "n1-x"}  # the tool closed the dialog itself
        await finish(stop, task)
    run(go)


def test_cant_now_answers_the_usual_error_at_once():
    async def go():
        s, d, stop, task = await locked()
        call = asyncio.create_task(d.run_tool("a", "tap", TAP, dialog=True))
        await asked(d)
        assert d.dialog_answer("a", "n1-x", "cant") is True
        with pytest.raises(Refused) as e:
            await asyncio.wait_for(call, 2)
        assert e.value.code == "locked" and str(e.value) == "the phone is locked; ask the user to unlock it"
        await finish(stop, task)
    run(go)


def test_done_makes_it_look_again_at_once_and_a_still_locked_phone_keeps_waiting():
    async def go():
        s, d, stop, task = await locked()
        call = asyncio.create_task(d.run_tool("a", "tap", TAP, dialog=True))
        await asked(d)
        assert d.dialog_answer("a", "n1-x", "done") is True  # they say it is done; it is not yet
        await asyncio.sleep(0.1)
        assert not call.done()
        s.is_locked = False
        await asyncio.wait_for(call, 5)
        await finish(stop, task)
    run(go)


def test_the_limit_ends_the_wait_with_the_usual_error_and_closes_the_dialog():
    async def go():
        s, d, stop, task = await locked()
        held = []
        d.held = held.append
        call = asyncio.create_task(d.run_tool("a", "tap", TAP, dialog=True))
        ask, seq = await asked(d)
        d.control.clock.t += 121
        with pytest.raises(Refused) as e:
            await asyncio.wait_for(call, 5)
        assert e.value.code == "locked"
        assert (await asked(d, seq))[0] == {"type": "close", "id": "n1-x"}
        assert held and 120 <= sum(held) <= 122  # a batch doesn't count the wait against its 120 s
        await finish(stop, task)
    run(go)


def test_a_call_with_no_dialog_answers_locked_at_once_as_before():
    async def go():
        s, d, stop, task = await locked()
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", TAP)
        assert e.value.code == "locked" and (await d.dialog_poll("a", 0, 0))["events"] == []
        await finish(stop, task)
    run(go)


def test_a_look_on_a_locked_phone_is_not_a_moment():
    async def go():
        s, d, stop, task = await locked()
        r = await asyncio.wait_for(d.run_tool("a", "look", {"screenshot": False}, dialog=True), 2)
        assert r["text"].startswith("Screen") and (await d.dialog_poll("a", 0, 0))["events"] == []
        await finish(stop, task)
    run(go)


def test_the_passcode_prompt_asks_and_the_call_goes_on_when_it_is_typed():
    async def go():
        s = FakeSession()
        s.stuck = PasscodeNeeded()
        d = make(s)
        stop, task = await started(d, until=())
        await until_stuck(d)
        call = asyncio.create_task(d.run_tool("a", "look", {"screenshot": False}, dialog=True))
        ask, seq = await asked(d)
        assert ask["kind"] == "needs" and ask["message"] == needs.message(needs.PASSCODE_PROMPT)
        s.stuck = None  # the person typed it
        r = await asyncio.wait_for(call, 10)
        assert r["text"].startswith("Screen ")
        assert (await asked(d, seq))[0]["type"] == "close"
        await finish(stop, task)
    run(go)


def test_the_health_sheet_asks_and_the_answer_says_a_person_dealt_with_it():
    async def go():
        d = make()
        stop, task = await started(d)
        sheet = [button("e1", "Turn On All", rect=(20, 200, 200, 40)), button("e2", "Don’t Allow", rect=(20, 60, 100, 40)),
                 button("e3", "Allow", rect=(300, 60, 80, 40), enabled=False)]
        d.session.tree = sheet
        await d.run_tool("a", "look", {"screenshot": False})
        call = asyncio.create_task(d.run_tool("a", "tap", {"target": {"ref": "e1"}}, dialog=True))
        ask, seq = await asked(d)
        assert ask["kind"] == "needs" and ask["message"] == needs.message(needs.HEALTH_SHEET)
        d.session.tree = [button("e1", "Connected", rect=(20, 200, 200, 40))]  # the person chose Allow
        r = await asyncio.wait_for(call, 10)
        assert "Note: a person dealt with iOS's Health sheet; the screen has changed since: look." in r["text"]
        assert "doesn't let automation change it. Ask a person" not in r["text"]
        assert (await asked(d, seq))[0] == {"type": "close", "id": ask["id"]}
        with pytest.raises(Refused) as e:  # the agent's list is from before: its refs are stale until it looks
            await d.run_tool("a", "tap", {"target": {"ref": "e1"}})
        assert e.value.code == "stale"
        await finish(stop, task)
    run(go)


def test_the_health_sheet_that_stays_keeps_todays_note_after_cant_now():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.tree = [button("e1", "Turn On All", rect=(20, 200, 200, 40)), button("e2", "Don’t Allow", rect=(20, 60, 100, 40)),
                          button("e3", "Allow", rect=(300, 60, 80, 40), enabled=False)]
        await d.run_tool("a", "look", {"screenshot": False})
        call = asyncio.create_task(d.run_tool("a", "tap", {"target": {"ref": "e1"}}, dialog=True))
        ask, _ = await asked(d)
        d.dialog_answer("a", ask["id"], "cant")
        r = await asyncio.wait_for(call, 5)
        assert "Note: this is iOS's Health access sheet; iOS doesn't let automation change it." in r["text"]
        await finish(stop, task)
    run(go)


def test_an_ios_alert_over_the_phone_asks_and_the_touch_goes_on_when_it_is_gone():
    from test_daemon import ALERT as an_alert

    async def go():
        s = FakeSession()
        s.app_names = {"com.example.app": "Example"}
        d = make(s)
        stop, task = await started(d)
        mine = Scope(("com.example.app",))
        s.foreground, s.app, s.last_alert = "com.apple.springboard", ("Home screen", "com.apple.springboard"), an_alert
        call = asyncio.create_task(d.run_tool("a", "tap", {"target": {"x": 5, "y": 5}}, scope=mine, dialog=True))
        ask, seq = await asked(d)
        assert ask["kind"] == "needs" and ask["message"] == needs.message(needs.IOS_ALERT)
        s.foreground, s.app, s.last_alert = "com.example.app", ("Example", "com.example.app"), None  # the person answered
        r = await asyncio.wait_for(call, 10)
        assert "Tapped" in r["text"]
        assert (await asked(d, seq))[0] == {"type": "close", "id": ask["id"]}
        await finish(stop, task)
    run(go)


def test_an_ios_alert_with_cant_now_is_the_usual_refusal():
    from test_daemon import ALERT as an_alert

    async def go():
        s = FakeSession()
        s.app_names = {"com.example.app": "Example"}
        d = make(s)
        stop, task = await started(d)
        s.foreground, s.app, s.last_alert = "com.apple.springboard", ("Home screen", "com.apple.springboard"), an_alert
        call = asyncio.create_task(d.run_tool("a", "tap", {"target": {"x": 5, "y": 5}}, scope=Scope(("com.example.app",)), dialog=True))
        ask, _ = await asked(d)
        d.dialog_answer("a", ask["id"], "cant")
        with pytest.raises(Refused) as e:
            await asyncio.wait_for(call, 5)
        assert str(e.value).startswith("outside_apps: an iOS system alert is in front")
        await finish(stop, task)
    run(go)


def test_another_agents_answer_does_not_count():
    async def go():
        s, d, stop, task = await locked()
        call = asyncio.create_task(d.run_tool("a", "tap", TAP, dialog=True))
        await asked(d)
        assert d.dialog_answer("b", "n1-x", "cant") is False
        await asyncio.sleep(0.1)
        assert not call.done()
        s.is_locked = False
        await asyncio.wait_for(call, 5)
        await finish(stop, task)
    run(go)

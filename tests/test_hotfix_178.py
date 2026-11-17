"""1.7.8, from the second demo-video report (2026-11-17, Tapwright 1.7.6 on a USB iPhone): bugs 3 to 7."""
import pytest

from test_daemon import finish, make, run, started

from tapwright.control import Refused
from tapwright.device import AppNotAnswering


# Bug 4: close_app and press_button home while the app in front isn't answering.

def test_close_app_survives_the_reconnect_storing_a_new_not_answering_for_the_same_app():
    """Each reconnect attempt stores a fresh AppNotAnswering; the guard compared objects and answered `reconnecting`
    on nearly every close_app for 2 min 40 s."""
    async def go():
        d = make()
        first = AppNotAnswering("com.atebits.Tweetie2", "X")
        d.stuck = AppNotAnswering("com.atebits.Tweetie2", "X")  # the supervisor's next attempt, the same app
        answer = await d._close_stuck(first, "X", [])
        assert answer["text"].startswith("Closed X (com.atebits.Tweetie2)")
        assert d.session.device.killed == ["com.atebits.Tweetie2"]
    run(go)


def test_close_app_still_refuses_when_another_app_became_the_stuck_one():
    async def go():
        d = make()
        first = AppNotAnswering("com.atebits.Tweetie2", "X")
        d.stuck = AppNotAnswering("com.example.other", "Other")
        with pytest.raises(Refused) as e:
            await d._close_stuck(first, "X", [])
        assert e.value.code == "reconnecting" and d.session.device.killed == []
    run(go)


def test_home_gets_through_while_the_front_app_is_not_answering():
    async def go():
        d = make()
        d.stuck = AppNotAnswering("com.atebits.Tweetie2", "X")
        sent = []

        async def http(method, path, payload=None, timeout=30.0):
            sent.append((method, path, timeout))
            return {}
        d.session.device.http = http
        answer = await d.run_tool("a", "press_button", {"name": "home"})
        assert answer["text"].startswith("Pressed home")
        assert sent == [("POST", "/wda/homescreen", 10.0)]
        assert "session" not in sent[0][1]  # never through a session, which would wait on the frozen app
    run(go)


def test_home_that_gets_no_answer_says_not_answering_and_other_buttons_stay_refused():
    from tapwright.device import ConnectionLost

    async def go():
        d = make()
        d.stuck = AppNotAnswering("com.atebits.Tweetie2", "X")

        async def http(method, path, payload=None, timeout=30.0):
            raise ConnectionLost("WebDriverAgent didn't answer POST /wda/homescreen within 10 s")
        d.session.device.http = http
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "press_button", {"name": "home"})
        assert e.value.code == "not_answering" and "close_app" in str(e.value)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "press_button", {"name": "volumeUp"})
        assert e.value.code == "not_answering"
    run(go)

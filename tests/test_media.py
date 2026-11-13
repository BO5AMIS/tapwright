"""`media`: test pictures for photo pickers (AGENT-TOOLS §17.6), the computer's side, with a fake runner."""
import asyncio
import base64
import pathlib

import pytest

from tapwright import client, daemon as daemon_module, media, mcp_server, session as session_module, store, ui
from tapwright.control import Refused
from tapwright.daemon import Scope
from tapwright.device import ConnectionLost, DeviceError
from tapwright.session import Session
from test_daemon import finish, make, run, started


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "HOME", tmp_path / "home")


HEADERS = {".jpg": bytes([0xFF, 0xD8, 0xFF, 0xE0]), ".png": bytes([0x89]) + b"PNG", ".heic": bytes(4) + b"ftypheic"}


@pytest.fixture
def pictures(tmp_path):
    out = []
    for name in ("food.jpg", "label.PNG", "scan.heic"):
        path = tmp_path / name
        path.write_bytes(HEADERS[path.suffix.lower()] + b"picture " + name.encode())
        out.append(str(path))
    return out


async def refused(d, args, client_id="a", **kw) -> Refused:
    with pytest.raises(Refused) as e:
        await d.run_tool(client_id, "media", args, **kw)
    return e.value


# ---- what is asked ----
def test_it_needs_add_or_remove_and_a_confirm_that_says_to_ask_the_user_first(pictures):
    async def go():
        d = make()
        stop, task = await started(d)
        e = await refused(d, {})
        assert e.code == "bad_params" and "needs add" in str(e)
        e = await refused(d, {"add": pictures, "remove": True, "confirm": True})
        assert e.code == "bad_params" and "not both" in str(e)
        for args in ({"add": pictures}, {"add": pictures, "confirm": False}, {"remove": True}):
            e = await refused(d, args)
            assert e.code == "bad_params" and str(e) == media.NEED_CONFIRM and "Ask your user first" in str(e)
        assert d.session.calls.count("photos_access") == 0  # nothing reached the phone
        e = await refused(d, {"add": pictures, "confirm": True, "sure": 1})
        assert e.code == "bad_params" and "media doesn't take sure" in str(e)
        await finish(stop, task)
    run(go)


def test_a_missing_file_a_wrong_type_a_big_one_and_too_many_are_bad_params(tmp_path, pictures, monkeypatch):
    async def go():
        d = make()
        stop, task = await started(d)
        notes = tmp_path / "notes.txt"
        notes.write_text("x", encoding="utf-8")
        cases = [([str(tmp_path / "gone.jpg")], "no such file"), ([str(notes)], "only jpeg, png and heic"),
                 ([str(tmp_path)], "only jpeg, png and heic"), (["x.jpg"] * 21, "at most 20")]
        for add, message in cases:
            e = await refused(d, {"add": add, "confirm": True})
            assert e.code == "bad_params" and message in str(e), (add, str(e))
        monkeypatch.setattr(media, "MAX_BYTES", 5)
        e = await refused(d, {"add": pictures[:1], "confirm": True})
        assert e.code == "bad_params" and "food.jpg" in str(e) and "over the" in str(e)
        e = await refused(d, {"add": "food.jpg", "confirm": True})
        assert e.code == "bad_params" and "list of paths" in str(e)
        assert not any(isinstance(c, tuple) and c[0] == "photos_add" for c in d.session.calls)
        await finish(stop, task)
    run(go)


def test_a_runner_older_than_the_photo_routes_is_unsupported_and_says_so(pictures):
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.tw_version = 2
        for args in ({"add": pictures, "confirm": True}, {"remove": True, "confirm": True}):
            e = await refused(d, args)
            assert e.code == "unsupported"
            assert str(e) == "this phone's runner is older than the photo routes (tw version < 3); update the runner"
        await finish(stop, task)
    run(go)


# ---- adding and removing ----
def test_add_puts_the_pictures_in_the_library_and_keeps_what_it_added_for_remove(pictures):
    async def go():
        d = make()
        stop, task = await started(d)
        r = await d.run_tool("a", "media", {"add": pictures, "confirm": True})
        assert r["text"] == ("Added 3 pictures to the photo library: food.jpg, label.PNG, scan.heic. "
                             "They stay until removed (media with remove: true).")
        assert ("photos_add", ("food.jpg", "label.PNG", "scan.heic")) in d.session.calls
        kept = media.load()["a"]
        assert [e["name"] for e in kept] == ["food.jpg", "label.PNG", "scan.heic"]
        one = await d.run_tool("a", "media", {"add": pictures[:1], "confirm": True})
        assert one["text"].startswith("Added 1 picture to the photo library: food.jpg.")

        gone = await d.run_tool("a", "media", {"remove": True, "confirm": True})
        assert gone["text"] == "Removed 4 pictures from the photo library: food.jpg, label.PNG, scan.heic, food.jpg."
        assert media.load() == {}
        again = await d.run_tool("a", "media", {"remove": True, "confirm": True})
        assert again["text"].startswith("Nothing to remove")
        await finish(stop, task)
    run(go)


def test_remove_deletes_only_what_was_added_and_a_restart_can_still_remove_it(pictures):
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("old-agent", "media", {"add": pictures[:2], "confirm": True})
        await d.run_tool("other", "media", {"add": pictures[2:], "confirm": True})
        await finish(stop, task)

        d2 = make()  # the daemon was restarted: the ledger is on disk
        stop, task = await started(d2)
        ids = {e["id"] for entries in media.load().values() for e in entries}
        r = await d2.run_tool("new-agent", "media", {"remove": True, "confirm": True})
        assert r["text"].startswith("Pictures from an earlier session: Removed 3 pictures")
        deletes = [c for c in d2.session.calls if isinstance(c, tuple) and c[0] == "photos_delete"]
        assert len(deletes) == 1 and set(deletes[0][1]) == ids
        assert media.load() == {} and len(ids) == 3
        await finish(stop, task)
    run(go)


def test_an_agent_with_its_own_pictures_removes_those_first(pictures):
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "media", {"add": pictures[:1], "confirm": True})
        await d.run_tool("b", "media", {"add": pictures[1:], "confirm": True})
        r = await d.run_tool("a", "media", {"remove": True, "confirm": True})
        assert r["text"] == "Removed 1 picture from the photo library: food.jpg."
        assert list(media.load()) == ["b"]
        await finish(stop, task)
    run(go)


def test_without_full_access_remove_says_what_to_delete_by_hand(pictures):
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "media", {"add": pictures, "confirm": True})
        d.session.photo_access = {"add": "authorized", "full": "denied"}
        r = await d.run_tool("a", "media", {"remove": True, "confirm": True})
        assert r["text"] == ("Couldn't remove them: the runner has no full access to the photo library (iOS has it "
                             "denied). Delete them by hand in Photos > Recently Added: food.jpg, label.PNG, scan.heic.")
        assert not any(isinstance(c, tuple) and c[0] == "photos_delete" for c in d.session.calls)
        assert len(media.load()["a"]) == 3  # still recorded
        # iOS has not been asked yet: the delete route would ask for full access, so it isn't called (Codex, PR #7)
        for undecided in ({"add": "authorized", "full": "notDetermined"}, {"add": "authorized"}):
            d.session.photo_access = undecided
            r = await d.run_tool("a", "media", {"remove": True, "confirm": True})
            assert r["text"].startswith("Couldn't remove them: deleting needs full access to the photo library, and "
                                        "Tapwright never asks for it (it adds with add-only access). Delete them by "
                                        "hand in Photos > Recently Added: food.jpg")
            assert not any(isinstance(c, tuple) and c[0] == "photos_delete" for c in d.session.calls)
            assert len(media.load()["a"]) == 3
        # a full access the runner had but lost on the way still words the by-hand answer
        d.session.photo_access = {"add": "authorized", "full": "authorized"}
        d.session.photo_deletes = DeviceError("HTTP 500: full access to the photo library is needed to delete")
        r = await d.run_tool("a", "media", {"remove": True, "confirm": True})
        assert r["text"].startswith("Couldn't remove them: the runner has no full access to the photo library. "
                                    "Delete them by hand in Photos > Recently Added: food.jpg")
        await finish(stop, task)
    run(go)


def test_a_person_declining_the_delete_keeps_the_record(pictures):
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "media", {"add": pictures[:2], "confirm": True})
        d.session.photo_deletes = 0
        r = await d.run_tool("a", "media", {"remove": True, "confirm": True})
        assert r["text"].startswith("Removed none of 2 pictures: the person declined the delete on the phone")
        assert len(media.load()["a"]) == 2
        await finish(stop, task)
    run(go)


def test_adding_is_refused_when_ios_has_the_runner_denied_and_a_failed_add_is_unsupported(pictures):
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.photo_access = {"add": "denied", "full": "denied"}
        e = await refused(d, {"add": pictures, "confirm": True})
        assert e.code == "unsupported" and "Settings > Privacy & Security > Photos" in str(e)
        d.session.photo_access = {"add": "notDetermined", "full": "notDetermined"}
        d.session.fail[("photos_add", ("food.jpg", "label.PNG", "scan.heic"))] = DeviceError("HTTP 500: not granted")
        e = await refused(d, {"add": pictures, "confirm": True})
        assert e.code == "unsupported" and "not granted" in str(e)
        assert media.load() == {}
        d.session.fail[("photos_add", ("food.jpg", "label.PNG", "scan.heic"))] = ConnectionLost("dropped")
        e = await refused(d, {"add": pictures, "confirm": True})
        assert e.code == "reconnecting" and "may or may not have happened" in str(e)
        await finish(stop, task)
    run(go)


def test_media_works_in_personal_mode_and_shows_nothing_of_another_app(pictures):
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.app = ("Safari", "com.apple.mobilesafari")
        r = await d.run_tool("a", "media", {"add": pictures, "confirm": True}, scope=Scope(("com.example.app",)))
        assert r["text"].startswith("Added 3 pictures") and r["image"] is None
        e = await refused(d, {"add": pictures, "confirm": True}, scope=Scope(()))
        assert e.code == "not_allowed"
        await finish(stop, task)
    run(go)


# ---- the runner's photo prompt (phone checks, 2026-11-12: media waited 120 s on an alert nobody could read) ----
RUNNER_PROMPT = ui.Alert("“WebDriverAgentRunner-Runner” Would Like Full Access to Your Photo Library",
                         ("Limit Access…", "Allow Full Access", "Don’t Allow"), (60.0, 300.0, 300.0, 200.0))
NEEDS_A_PERSON = ("needs_a_person: iOS is asking on the phone whether Tapwright's runner may use Photos. A person answers "
                  "it on the phone (to add pictures: \"Allow\" or \"Limit Access…\"; never needed: full access), then "
                  "call media again. Until they answer (2 minutes at most) the runner answers no other call: ask "
                  "your user now, and wait for them.")


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(daemon_module, "MEDIA_WATCH", 0.02)
    monkeypatch.setattr(daemon_module, "FRONT_RETRY", 0.001)  # (a look waits out a banner before it refuses)


def prompted(d):
    """The phone with iOS's photo prompt up and the runner's add waiting for the person to answer it."""
    d.session.photos_gate = asyncio.Event()
    d.session.last_alert = RUNNER_PROMPT
    d.session.foreground, d.session.app = daemon_module.SPRINGBOARD, ("Home screen", daemon_module.SPRINGBOARD)


def test_media_stops_waiting_at_once_when_a_system_alert_is_in_front(pictures, quick):
    async def go():
        d = make()
        stop, task = await started(d)
        prompted(d)
        began = asyncio.get_running_loop().time()
        e = await refused(d, {"add": pictures, "confirm": True})
        assert e.code == "not_allowed" and str(e) == NEEDS_A_PERSON
        assert asyncio.get_running_loop().time() - began < 2.0  # not the runner's 150 s
        assert media.load() == {}  # nothing is recorded until the runner says it added them
        await finish(stop, task)
    run(go)


def test_the_alert_tool_is_not_queued_behind_a_waiting_media(pictures, quick, monkeypatch):
    async def go():
        d = make()
        stop, task = await started(d)
        monkeypatch.setattr(daemon_module, "MEDIA_WATCH", 30.0)  # media looks for no alert meanwhile: it stays pending
        d.session.photos_gate = asyncio.Event()
        waiting = asyncio.ensure_future(d.run_tool("a", "media", {"add": pictures, "confirm": True}))
        await asyncio.sleep(0.1)
        assert not waiting.done()  # no alert yet: media waits for the runner
        d.session.last_alert = ui.Alert("Allow?", ("Allow",), (0.0, 0.0, 10.0, 10.0))
        r = await asyncio.wait_for(d.run_tool("b", "alert", {"action": "text"}), 2.0)  # (before the 150 s wait ends)
        assert r["text"]
        assert not waiting.done()  # the alert was answered while media is still pending, not after it gave up
        d.session.last_alert = None
        d.session.photos_gate.set()  # the add finishes
        answer = await asyncio.wait_for(waiting, 5.0)
        assert answer["text"].startswith("Added 3 pictures")
        await finish(stop, task)
    run(go)


def test_the_pictures_are_added_once_when_media_is_called_again_after_the_person_answers(pictures, quick):
    async def go():
        d = make()
        stop, task = await started(d)
        prompted(d)
        e = await refused(d, {"add": pictures, "confirm": True})
        assert str(e) == NEEDS_A_PERSON
        d.session.last_alert = None  # the person allowed it: the runner's add goes on and finishes
        d.session.photos_gate.set()
        r = await d.run_tool("a", "media", {"add": pictures, "confirm": True})
        assert r["text"].startswith("Added 3 pictures to the photo library: food.jpg, label.PNG, scan.heic.")
        assert [c for c in d.session.calls if isinstance(c, tuple) and c[0] == "photos_add"] == [
            ("photos_add", ("food.jpg", "label.PNG", "scan.heic"))]  # sent once, never again
        assert [e["name"] for e in media.load()["a"]] == ["food.jpg", "label.PNG", "scan.heic"]  # and kept for remove
        r = await d.run_tool("a", "media", {"remove": True, "confirm": True})
        assert r["text"].startswith("Removed 3 pictures")
        await finish(stop, task)
    run(go)


def test_two_calls_of_one_client_with_the_same_pictures_send_them_once(pictures, quick):
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.photos_gate = asyncio.Event()  # the add is still going when the second call looks for it
        agent = d.control.agent

        async def slow_agent(fn):  # the gate takes a moment (a person-first queue, a busy loop): the calls overlap there
            await asyncio.sleep(0.05)
            return await agent(fn)
        d.control.agent = slow_agent
        args = {"add": pictures, "confirm": True}
        first = asyncio.ensure_future(d.run_tool("a", "media", args))
        second = asyncio.ensure_future(d.run_tool("a", "media", args))
        await asyncio.sleep(0.15)
        d.session.photos_gate.set()
        answers = await asyncio.wait_for(asyncio.gather(first, second), 5.0)
        assert all(r["text"].startswith("Added 3 pictures") for r in answers)
        assert [c for c in d.session.calls if isinstance(c, tuple) and c[0] == "photos_add"] == [
            ("photos_add", ("food.jpg", "label.PNG", "scan.heic"))]  # one add, not two
        assert len(media.load()["a"]) == 3
        await finish(stop, task)
    run(go)


def test_other_pictures_wait_while_an_add_is_still_pending_on_the_person(pictures, quick):
    async def go():
        d = make()
        stop, task = await started(d)
        prompted(d)
        e = await refused(d, {"add": pictures, "confirm": True})
        assert str(e) == NEEDS_A_PERSON
        e = await refused(d, {"add": pictures[:1], "confirm": True})  # other pictures: not queued behind it
        assert e.code == "busy" and "still waiting" in str(e) and "once the person has answered" in str(e)
        d.session.last_alert = None
        d.session.photos_gate.set()
        r = await d.run_tool("a", "media", {"add": pictures, "confirm": True})  # the first set picks up its own add
        assert r["text"].startswith("Added 3 pictures")
        assert [c for c in d.session.calls if isinstance(c, tuple) and c[0] == "photos_add"] == [
            ("photos_add", ("food.jpg", "label.PNG", "scan.heic"))]  # the first add once, the other never sent
        assert d._media_adds == {}
        r = await d.run_tool("a", "media", {"add": pictures[:1], "confirm": True})  # now it is free to go
        assert r["text"].startswith("Added 1 picture")
        await finish(stop, task)
    run(go)


def test_a_person_driving_still_holds_media_back(pictures, quick):
    async def go():
        d = make()
        stop, task = await started(d)
        d.control.person_active = lambda: True
        e = await refused(d, {"add": pictures, "confirm": True})
        assert e.code == "busy"
        await finish(stop, task)
    run(go)


def test_in_personal_mode_the_runners_prompt_can_be_read_but_never_answered_by_the_tool(pictures, quick):
    async def go():
        d = make()
        stop, task = await started(d)
        s, mine = d.session, Scope(("com.example.app",))
        s.alert_text = "“WebDriverAgentRunner-Runner” Would Like Full Access to Your Photo Library"
        prompted(d)

        async def text(action):
            s.calls.append(("alert", action))
            return s.alert_text
        s.alert = text

        async def running():
            return ["com.example.app"]
        s.device.foreground_apps = running
        r = await d.run_tool("a", "alert", {"action": "text"}, scope=mine)
        assert r["text"] == s.alert_text and "outside_apps" not in r["text"]
        for action in ("accept", "dismiss"):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "alert", {"action": action}, scope=mine)
            assert e.value.code == "not_allowed" and str(e.value) == NEEDS_A_PERSON
        assert [c for c in s.calls if isinstance(c, tuple) and c[0] == "alert"] == [("alert", "text")]  # only the text
        with pytest.raises(Refused) as e:  # `look` still shows nothing of it: only the alert's text is read
            await d.run_tool("a", "look", {"screenshot": False}, scope=mine)
        assert str(e.value).startswith("outside_apps")
        s.last_alert = ui.Alert("“Mail” Would Like to Send You Notifications", ("Allow",), (0.0, 0.0, 10.0, 10.0))
        with pytest.raises(Refused) as e:  # another app's prompt is no one of the given apps'
            await d.run_tool("a", "alert", {"action": "text"}, scope=mine)
        assert str(e.value).startswith("outside_apps")
        await finish(stop, task)
    run(go)


# ---- the MCP tool ----
def test_the_mcp_tool_sends_absolute_paths_and_only_what_was_given(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(client, "call", lambda tool, args, cid, timeout=120.0: seen.append((tool, args)) or
                        {"text": "ok", "image": None, "image_size": None})
    monkeypatch.chdir(tmp_path)
    asyncio.run(mcp_server.media(add=["pic.jpg"], confirm=True))
    asyncio.run(mcp_server.media(remove=True, confirm=True))
    asyncio.run(mcp_server.media())
    assert seen == [("media", {"add": [str((tmp_path / "pic.jpg").resolve())], "confirm": True}),
                    ("media", {"remove": True, "confirm": True}), ("media", {"confirm": False})]


# ---- the runner routes ----
class Calls:
    def __init__(self, answers):
        self.answers, self.sent = answers, []

    async def __call__(self, method, path, payload=None, timeout=30, again=None):
        self.sent.append((method, path.rsplit("/wda/tw/", 1)[-1], payload, timeout))
        answer = self.answers[path.rsplit("/wda/tw/", 1)[-1]]
        if isinstance(answer, Exception):
            raise answer
        return {"value": answer}


def runner(answers) -> tuple[Session, Calls]:
    s = Session(None)
    calls = Calls(answers)

    async def ensure():
        return None
    s.ensure, s._call = ensure, calls
    return s, calls


def test_the_session_asks_the_forks_photo_routes():
    s, calls = runner({"version": {"tapwright": 3}, "photos/access": {"add": "authorized", "full": "notDetermined"},
                       "photos/add": {"identifiers": ["A/L0/001", "B/L0/001"]}, "photos/delete": {"deleted": 2}})

    async def go():
        assert await s.runner_version() == 3
        assert await s.photos_access() == {"add": "authorized", "full": "notDetermined"}
        assert await s.photos_add([("a.jpg", b"AAA"), ("b.png", b"BBB")]) == ["A/L0/001", "B/L0/001"]
        assert await s.photos_delete(["A/L0/001", "B/L0/001"]) == 2
    asyncio.run(go())
    sent = {path: (method, payload, timeout) for method, path, payload, timeout in calls.sent}
    assert sent["photos/add"] == ("POST", {"images": [base64.b64encode(b"AAA").decode(), base64.b64encode(b"BBB").decode()],
                                           "names": ["a.jpg", "b.png"]}, session_module.PHOTOS_WAIT)
    assert sent["photos/delete"][:2] == ("POST", {"identifiers": ["A/L0/001", "B/L0/001"]})
    assert sent["photos/access"][0] == "GET"


def test_an_older_or_appiums_runner_has_version_zero():
    s, _ = runner({"version": DeviceError("unknown command")})
    assert asyncio.run(s.runner_version()) == 0
    s, _ = runner({"version": {"tapwright": 2}})
    assert asyncio.run(s.runner_version()) == 2 and s.fast_drag and s.fast_tap


def test_the_session_reads_the_system_alert_apart_from_the_agents_list():
    s, _ = runner({})
    source = ('<AppiumAUT><XCUIElementTypeApplication type="XCUIElementTypeApplication" name="SpringBoard" label="SpringBoard" '
              'x="0" y="0" width="430" height="932"><XCUIElementTypeAlert type="XCUIElementTypeAlert" '
              'label="“WebDriverAgentRunner-Runner” Would Like Full Access to Your Photo Library" x="60" y="300" '
              'width="310" height="200"><XCUIElementTypeButton type="XCUIElementTypeButton" label="Allow Full Access" '
              'x="60" y="440" width="310" height="50"/></XCUIElementTypeAlert></XCUIElementTypeApplication></AppiumAUT>')

    async def read(timeout=60):
        return source
    s._source, s.screen, s.items = read, (430.0, 932.0), ["the agent's list"]
    alert = asyncio.run(s.peek_alert())
    assert alert is not None and "WebDriverAgentRunner" in alert.title and alert.buttons == ("Allow Full Access",)
    assert s.items == ["the agent's list"]  # (what the agent works with is untouched)


# ---- review fixes (W14) ----
def test_m7_remove_takes_this_agents_pictures_and_the_ones_from_before_the_daemon_started_not_a_live_agents(pictures):
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("old", "media", {"add": pictures[:1], "confirm": True})
        await finish(stop, task)

        d2 = make()  # restarted: "old"'s picture is from an earlier run
        stop, task = await started(d2)
        d2.session.photos = ["a-library-that-has-others"]  # (the fake numbers pictures by the library's size)
        await d2.run_tool("live", "media", {"add": pictures[1:2], "confirm": True})
        r = await d2.run_tool("new", "media", {"remove": True, "confirm": True})
        assert r["text"].startswith("Pictures from an earlier session: Removed 1 picture")
        assert [e["name"] for e in media.load()["live"]] == ["label.PNG"]  # the live agent's is untouched
        r = await d2.run_tool("new", "media", {"remove": True, "confirm": True})
        assert r["text"].startswith("Nothing to remove")
        assert list(media.load()) == ["live"]
        r = await d2.run_tool("new", "media", {"remove": "all", "confirm": True})
        assert r["text"] == "Removed 1 picture from the photo library: label.PNG." and media.load() == {}
        e = await refused(d2, {"remove": "some", "confirm": True})
        assert e.code == "bad_params"
        await finish(stop, task)
    run(go)


def test_m8_media_writes_go_through_the_agent_path_a_person_first(pictures):
    async def go():
        d = make()
        stop, task = await started(d)
        d.control.person_active = lambda: True  # a person is driving the phone
        e = await refused(d, {"add": pictures, "confirm": True})
        assert e.code == "busy"
        assert not any(isinstance(c, tuple) and c[0] == "photos_add" for c in d.session.calls)
        await finish(stop, task)
    run(go)


def test_m6_the_pictures_together_have_a_size_cap(tmp_path, pictures, monkeypatch):
    async def go():
        d = make()
        stop, task = await started(d)
        monkeypatch.setattr(media, "MAX_TOTAL", 40)  # each picture is ~20 bytes
        e = await refused(d, {"add": pictures, "confirm": True})
        assert e.code == "bad_params" and "together are over the" in str(e)
        assert not any(isinstance(c, tuple) and c[0] == "photos_add" for c in d.session.calls)
        await finish(stop, task)
    run(go)


def test_the_mcp_client_waits_longer_for_media_and_for_a_stop_with_export():
    assert mcp_server._timeout("look", {}) == 120.0
    assert mcp_server._timeout("media", {"add": ["x.jpg"]}) > 150
    assert mcp_server._timeout("journey", {"stop": True}) == 120.0
    assert mcp_server._timeout("journey", {"stop": True, "export": {"to": "issue"}}) >= 600


def test_the_pictures_are_checked_by_their_first_bytes_and_a_link_or_network_path_is_refused(tmp_path, pictures):
    fake = tmp_path / "fake.png"
    fake.write_bytes(b"MZ not a picture")
    link = tmp_path / "link.jpg"
    try:
        link.symlink_to(pictures[0])
    except (OSError, NotImplementedError):
        link = None  # (no permission to make a link here)
    cases = [([str(fake)], "isn't a png picture"), (["\\\\host\\share\\a.jpg"], "network path"),
             (["//host/share/a.jpg"], "network path")]
    if link is not None:
        cases.append(([str(link)], "a link isn't followed"))
    for add, message in cases:
        with pytest.raises(ValueError) as e:
            media.read_pictures(add)
        assert message in str(e.value), (add, str(e.value))
    assert [name for name, _ in media.read_pictures(pictures)] == ["food.jpg", "label.PNG", "scan.heic"]


def test_pictures_a_short_answer_did_make_stay_recorded(pictures):
    async def go():
        d = make()
        stop, task = await started(d)

        async def short(pics):
            d.session.calls.append("short")
            return ["ONLY/L0/001"]
        d.session.photos_add = short
        e = await refused(d, {"add": pictures, "confirm": True})
        assert e.code == "internal"
        assert [x["id"] for x in media.load()["a"]] == ["ONLY/L0/001"]
        await finish(stop, task)
    run(go)


# ---- Codex, PR #8 ----
def test_the_first_add_says_a_person_is_needed_from_the_access_alone_while_the_runner_is_held(pictures, quick):
    """The runner's add waits for iOS's prompt with its whole queue, so no alert can be read meanwhile: the access says
    the prompt comes (add not decided yet), and media answers needs_a_person at once. The add goes on, and is picked up
    by the next call once the person answers."""
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        s.photo_access = {"add": "notDetermined", "full": "notDetermined"}
        s.photos_gate = asyncio.Event()

        async def held(timeout=5.0):  # the runner's queue is busy with the add: an alert read gets no answer
            await asyncio.sleep(3600)
        s.peek_alert = held
        began = asyncio.get_running_loop().time()
        e = await refused(d, {"add": pictures, "confirm": True})
        assert e.code == "not_allowed" and str(e) == NEEDS_A_PERSON
        assert asyncio.get_running_loop().time() - began < 2.0
        s.photo_access = {"add": "authorized", "full": "notDetermined"}  # the person allowed it
        s.photos_gate.set()
        r = await d.run_tool("a", "media", {"add": pictures, "confirm": True})
        assert r["text"].startswith("Added 3 pictures")
        assert len([c for c in s.calls if isinstance(c, tuple) and c[0] == "photos_add"]) == 1  # sent once
        await finish(stop, task)
    run(go)


def test_a_cut_short_add_is_picked_up_only_by_the_same_pictures_not_by_a_file_of_the_same_name(tmp_path, pictures, quick):
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        s.photo_access = {"add": "notDetermined", "full": "notDetermined"}
        s.photos_gate = asyncio.Event()
        first = pictures[0]
        other = tmp_path / "second" / pathlib.Path(first).name
        other.parent.mkdir()
        data = pathlib.Path(first).read_bytes()
        other.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))  # same name, other bytes
        e = await refused(d, {"add": [first], "confirm": True})
        assert e.code == "not_allowed"
        s.photo_access = {"add": "authorized", "full": "notDetermined"}
        s.photos_gate.set()
        await d.run_tool("a", "media", {"add": [str(other)], "confirm": True})
        adds = [c for c in s.calls if isinstance(c, tuple) and c[0] == "photos_add"]
        assert len(adds) == 2  # the second picture was sent, not taken for the first
        await finish(stop, task)
    run(go)


def test_while_ios_photo_prompt_holds_the_runner_nothing_takes_the_silence_for_a_drop(pictures, quick):
    """Codex, PR #8 cycle 2: the runner answers nothing while its add waits on iOS's prompt. The heartbeat's lock
    check used to time out, call it a drop, reconnect and stop the runner (and the add) long before the person
    answered. Now the heartbeat skips its check, a request that gets no answer isn't a drop, other calls are refused
    at once, and the add is picked up when the person has answered."""
    from tapwright.device import ConnectionLost

    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        s.photo_access = {"add": "notDetermined", "full": "notDetermined"}
        s.photos_gate = asyncio.Event()
        e = await refused(d, {"add": pictures, "confirm": True})
        assert str(e) == NEEDS_A_PERSON

        async def held():  # every other request waits behind the add, as the runner's queue does, and times out
            await asyncio.sleep(0.05)
            raise ConnectionLost("no answer from WebDriverAgent")
        s.locked = held
        await asyncio.sleep(0.3)  # many heartbeats
        assert d.state == "live" and "reset" not in s.calls  # no reconnect, the runner and its add stay
        d.lost(ConnectionLost("a request timed out behind the add"))
        assert d.state == "live" and not d._lost.is_set()
        with pytest.raises(Refused) as other:
            await asyncio.wait_for(d.run_tool("b", "look", {"screenshot": False}), 1.0)
        assert other.value.code == "busy" and "photo prompt" in str(other.value)
        # the person answers: the add finishes, and the same call picks it up without asking the runner anything
        s.photos_gate.set()
        r = await d.run_tool("a", "media", {"add": pictures, "confirm": True})
        assert r["text"].startswith("Added 3 pictures")
        assert len([c for c in s.calls if isinstance(c, tuple) and c[0] == "photos_add"]) == 1
        assert d._photo_wait_left() is None
        await finish(stop, task)
    run(go)


def test_a_second_agents_add_during_the_photo_wait_is_refused_and_doesnt_take_the_wait_over(pictures, quick):
    """Codex, PR #8 cycle 3: B's add would queue behind A's on the runner, past the wait's deadline, and replaced the
    wait's owner. It is refused busy; the wait stays A's, and A picking its add up doesn't move the deadline."""
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        s.photo_access = {"add": "notDetermined", "full": "notDetermined"}
        s.photos_gate = asyncio.Event()
        e = await refused(d, {"add": pictures, "confirm": True})
        assert str(e) == NEEDS_A_PERSON
        owner, until = d._photo_wait
        with pytest.raises(Refused) as other:
            await d.run_tool("b", "media", {"add": pictures[:1], "confirm": True})
        assert other.value.code == "busy" and "photo prompt" in str(other.value)
        assert d._photo_wait == (owner, until)
        e = await refused(d, {"add": pictures, "confirm": True})  # A asks again: still waiting
        assert str(e) == NEEDS_A_PERSON and d._photo_wait == (owner, until)  # same owner, same deadline
        assert len([c for c in s.calls if isinstance(c, tuple) and c[0] == "photos_add"]) == 1
        s.photos_gate.set()
        r = await d.run_tool("a", "media", {"add": pictures, "confirm": True})
        assert r["text"].startswith("Added 3 pictures")
        await finish(stop, task)
    run(go)


def test_a_tools_own_call_during_the_photo_wait_waits_for_the_answer_and_then_runs(pictures, quick):
    """Codex, PR #8 cycle 3: a journey's stress putting the phone back upright runs its own calls through run_tool;
    refused busy, the cleanup was lost. A tool's own call waits for the person's answer instead, then runs."""
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        s.photo_access = {"add": "notDetermined", "full": "notDetermined"}
        s.photos_gate = asyncio.Event()
        await refused(d, {"add": pictures, "confirm": True})
        token = daemon_module._JOURNEY_OWN.set(True)
        try:
            own = asyncio.ensure_future(d.run_tool("j", "look", {"screenshot": False}))
        finally:
            daemon_module._JOURNEY_OWN.reset(token)
        await asyncio.sleep(0.1)
        assert not own.done()  # it waits; it isn't refused
        s.photos_gate.set()  # the person answers
        r = await asyncio.wait_for(own, 5.0)
        assert r["text"]
        assert d._photo_wait_left() is None  # the wait is over: an agent's own calls aren't refused any more
        await finish(stop, task)
    run(go)


def test_two_agents_first_adds_at_once_send_one_and_the_other_is_told_busy(pictures, quick):
    """Codex, PR #8 cycle 4: both checked for a waiting add before either had claimed the wait (their reads and the
    touch gate let each other in), so both were sent and the second took the wait over."""
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        s.photo_access = {"add": "notDetermined", "full": "notDetermined"}
        s.photos_gate = asyncio.Event()
        real_access = s.photos_access

        async def slow_access():  # the reads of the two calls interleave
            await asyncio.sleep(0.01)
            return await real_access()
        s.photos_access = slow_access
        real_agent = d.control.agent

        async def agent(fn):  # the touch gate waits its turn behind other calls, as on a busy phone
            await asyncio.sleep(0.02)
            return await real_agent(fn)
        d.control.agent = agent
        results = await asyncio.gather(
            d.run_tool("a", "media", {"add": pictures, "confirm": True}),
            d.run_tool("b", "media", {"add": pictures[:1], "confirm": True}), return_exceptions=True)
        codes = sorted(r.code for r in results if isinstance(r, Refused))
        assert codes == ["busy", "not_allowed"], results  # one waits on the person, the other is told busy
        assert len([c for c in s.calls if isinstance(c, tuple) and c[0] == "photos_add"]) == 1
        s.photos_gate.set()
        await asyncio.sleep(0.05)
        await finish(stop, task)
    run(go)

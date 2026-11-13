"""`media`: test pictures for photo pickers (AGENT-TOOLS §17.6), the computer's side, with a fake runner."""
import asyncio
import base64

import pytest

from tapwright import client, media, mcp_server, session as session_module, store
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

"""1.7.9, Opus review R179: M1 (a runner timeout isn't a refusal), M2 (a video's copy and import hold the phone), M3
(empty text with replace clears the field), L1-L3 (the hold and the pick-up window count from when the add goes out),
L5 (the copy refuses a file that changed), L6 (more mp4 brands), L7 (videos in the words). All values are synthetic."""
import asyncio

import pytest
from test_daemon import finish, make, run, started
from test_media import pictures, prompted, quick, refused  # noqa: F401 (fixtures)
from test_media_179 import MP4, RUNNER_ID, FakeContainer, box, clips, fake_house_arrest, staged_session  # noqa: F401

from tapwright import batch, daemon as daemon_module, device as device_module, media, session as session_module, store, ui
from tapwright.control import Refused
from tapwright.device import ConnectionLost, DeviceError

TIMED_OUT = "WebDriverAgent: timeout: adding the pictures took too long; check Photos > Recently Added"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "HOME", tmp_path / "home")


def adds(d) -> int:
    return len([c for c in d.session.calls if isinstance(c, tuple) and c[0] == "photos_add"])


# ---- M1: the runner's own timeout leaves the clip, and the add is reported, not done twice ----
def test_the_runners_timeout_answer_is_a_runner_timed_out_not_a_refusal():
    """device.http words every WebDriverAgent error reply as a DeviceError; its "timeout" is one of its own."""
    assert issubclass(device_module.RunnerTimedOut, DeviceError)

    async def go():
        d = device_module.Device.__new__(device_module.Device)
        d._keep_forced = False

        class Conn:
            pass

        class Rsd:
            async def create_service_connection(self, port):
                return Conn()
        d.rsd = Rsd()

        async def exchange(conn, request):
            return 408, b'{"value": {"error": "timeout", "message": "adding the pictures took too long"}}'

        async def close(conn):
            return None
        d._exchange, d._close = exchange, close
        with pytest.raises(device_module.RunnerTimedOut) as e:
            await d._http("POST", "/session/S/wda/tw/photos/add", {})
        assert str(e.value).startswith("WebDriverAgent: timeout: adding the pictures took too long")

        async def refusal(conn, request):
            return 400, b'{"value": {"error": "invalid argument", "message": "no such file"}}'
        d._exchange = refusal
        with pytest.raises(DeviceError) as e:
            await d._http("POST", "/session/S/wda/tw/photos/add", {})
        assert not isinstance(e.value, device_module.RunnerTimedOut)
    asyncio.run(go())


def test_a_runner_timeout_leaves_the_staged_clip_to_the_runner(clips):
    """Review R179 M1: the runner gave up waiting while the library still moves the clip; deleting the folder then
    could break the import. Only a real refusal removes it."""
    video = media.read_media(clips[:1])[0]
    s, calls = staged_session(device_module.RunnerTimedOut(TIMED_OUT))
    with pytest.raises(device_module.RunnerTimedOut):
        asyncio.run(s.photos_add([video]))
    assert [c[0] for c in s.device.calls] == ["sweep", "stage"]  # no unstage


def test_a_video_add_the_runner_timed_out_is_timeout_and_reported_once_before_it_is_added_again(clips):
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.tw_version = 7
        sent_adds = []

        async def timed_out(items, sent=None):
            sent_adds.append([p[0] for p in items])
            if sent is not None:
                sent()
            raise device_module.RunnerTimedOut(TIMED_OUT)
        d.session.photos_add = timed_out
        e = await refused(d, {"add": clips[:1]})
        assert e.code == "timeout" and "may still finish" in str(e) and "Recently Added" in str(e)
        assert "couldn't do it" not in str(e)  # (1.7.9 before the review: `unsupported`)
        e = await refused(d, {"add": clips[:1]})  # the same clip again: said, not added twice
        assert e.code == "timeout" and "earlier add" in str(e) and len(sent_adds) == 1
        e = await refused(d, {"add": clips[:1]})  # the agent checked Recently Added: now it is added again
        assert len(sent_adds) == 2
        await finish(stop, task)
    run(go)


# ---- M2 and L2: a video's copy and import hold the phone, in a video's words ----
def test_while_a_video_is_imported_the_heartbeat_waits_and_other_calls_are_told_busy(clips, quick):
    """Review R179 M2: access already granted, so no photo prompt is claimed; a long import held the runner, three
    heartbeats failed, and the reconnect stopped the runner and the add."""
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        s.tw_version = 7
        s.photos_gate = asyncio.Event()  # the library's import takes this long
        call = asyncio.ensure_future(d.run_tool("a", "media", {"add": clips[:1]}))
        for _ in range(100):
            if d._photo_wait_left() is not None and d._photo_wait_kind == "video":
                break
            await asyncio.sleep(0.01)
        assert d._photo_wait_kind == "video"
        left = d._photo_wait_left()
        assert left == pytest.approx(session_module.photos_timeout(len(MP4)) + daemon_module.MEDIA_HOLD_SLACK, abs=1)
        s.locked_errors = 30  # every heartbeat would fail
        await asyncio.sleep(0.3)
        assert d.state == "live" and "reset" not in s.calls and s.locked_errors == 30  # not even asked
        d.lost(ConnectionLost("a request timed out behind the import"))
        assert d.state == "live" and not d._lost.is_set()
        with pytest.raises(Refused) as other:
            await asyncio.wait_for(d.run_tool("b", "look", {"screenshot": False}), 1.0)
        assert other.value.code == "busy" and "a video is being added to Photos" in str(other.value)
        assert "photo prompt" not in str(other.value)
        with pytest.raises(Refused) as other:  # another agent's media call too
            await d.run_tool("b", "media", {"add": clips[1:2]})
        assert other.value.code == "busy" and "a video is being added" in str(other.value)
        assert "peek_alert" not in s.calls  # the import is waited for, not watched (each look would queue behind it)
        s.locked_errors = 0
        s.photos_gate.set()
        r = await asyncio.wait_for(call, 5.0)
        assert r["text"].startswith("Added 1 video")
        assert d._photo_wait_left() is None
        await finish(stop, task)
    run(go)


def test_during_a_first_video_adds_copy_others_hear_of_the_video_not_of_a_prompt(clips, quick):
    """Review R179 L2: iOS asks only once the add goes out; during the copy, the words don't send the user to look
    for a dialog that isn't there."""
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        s.tw_version = 7
        s.photo_access = {"add": "notDetermined", "full": "notDetermined"}
        prompted(d)
        s.stage_gate = asyncio.Event()
        call = asyncio.ensure_future(d.run_tool("a", "media", {"add": clips[:1]}))
        await asyncio.sleep(0.1)
        with pytest.raises(Refused) as other:
            await d.run_tool("b", "look", {"screenshot": False})
        assert other.value.code == "busy" and "a video is being added to Photos" in str(other.value)
        assert "answer iOS's photo prompt" not in str(other.value)
        s.stage_gate.set()  # sent: now iOS asks
        with pytest.raises(Refused) as e:
            await call
        assert e.value.code == "not_allowed" and "iOS is asking" in str(e.value)
        with pytest.raises(Refused) as other:
            await d.run_tool("b", "look", {"screenshot": False})
        assert other.value.code == "busy" and "photo prompt" in str(other.value)
        s.photos_gate.set()
        await finish(stop, task)
    run(go)


# ---- L1 and L3: the hold and the pick-up window count from when the add goes out ----
def test_a_copy_slower_than_its_budget_still_holds_the_phone_once_the_add_goes_out(clips, quick):
    """Review R179 L1: the copy's hold ran out before the add was sent, and sent() didn't arm it again, so the add
    went out with iOS's prompt up and the heartbeat checking."""
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        s.tw_version = 7
        s.photo_access = {"add": "notDetermined", "full": "notDetermined"}
        prompted(d)
        s.stage_gate = asyncio.Event()
        call = asyncio.ensure_future(d.run_tool("a", "media", {"add": clips[:1]}))
        await asyncio.sleep(0.1)
        d.control.clock.t += 10_000  # a stalled copy: far past its budget
        assert d._photo_wait_left() is None
        s.stage_gate.set()
        with pytest.raises(Refused):
            await call
        left = d._photo_wait_left()
        assert left is not None and d._photo_wait_kind == "video-prompt"
        s.photos_gate.set()
        await finish(stop, task)
    run(go)


def test_the_pick_up_window_counts_from_when_the_add_went_out_not_from_the_copys_start(clips, quick):
    """Review R179 L3: a long copy, a late answer, then the agent calls again as needs_a_person said: the same clip is
    picked up, not copied and imported a second time."""
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        s.tw_version = 7
        s.photo_access = {"add": "notDetermined", "full": "notDetermined"}
        prompted(d)
        s.stage_gate = asyncio.Event()
        call = asyncio.ensure_future(d.run_tool("a", "media", {"add": clips[:1]}))
        await asyncio.sleep(0.1)
        d.control.clock.t += daemon_module.MEDIA_KEEP - 10  # the copy took nearly the whole window
        s.stage_gate.set()
        with pytest.raises(Refused):
            await call
        s.photos_gate.set()  # the person answers; the add finishes
        await asyncio.sleep(0.05)
        d.control.clock.t += 60  # past MEDIA_KEEP from the add's start, well inside it from when it went out
        r = await d.run_tool("a", "media", {"add": clips[:1]})
        assert r["text"].startswith("Added 1 video") and adds(d) == 1
        await finish(stop, task)
    run(go)


# ---- M3: an empty text with replace clears the field ----
def test_an_empty_text_with_replace_clears_the_field_and_sends_no_keys(monkeypatch):
    """Review R179 M3: the WebDriverAgent clear path sent an empty key list, which the runner refuses (internal)."""
    from test_hotfix_178 import _post_field, _thread_phone
    from test_session import Recorder
    from tapwright.session import Session
    monkeypatch.setattr(session_module.store, "load_state", lambda: {"runner_id": "runner"})
    monkeypatch.setattr(session_module, "FOCUS_POLL", 0.01)
    phone = Recorder()
    _thread_phone(phone, [(56, 485)])
    s = Session(phone)
    s.items = [_post_field()]  # 88 characters: cleared WebDriverAgent's way
    asyncio.run(s.type_text("", replace=True, target=ui.Target(ref="e26")))
    paths = [p for _, p, _ in phone.requests if p.endswith(("/clear", "/wda/keys"))]
    assert paths == ["/session/S/element/f0/clear"]
    # with submit, an empty text presses Return alone
    phone.requests.clear()
    s.items = [_post_field()]
    asyncio.run(s.type_text("", replace=False, submit=True, target=ui.Target(ref="e26")))
    keys = [payload for _, p, payload in phone.requests if p.endswith("/wda/keys")]
    assert keys == [{"value": ["\n"]}]


def test_type_text_with_empty_text_clears_through_the_tool_and_is_refused_only_with_nothing_to_do():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.tree = [ui.Element("e1", "input", "Email", "email", value="old@example.com", rect=(10, 100, 300, 40),
                                     actions=("tap", "type"))]
        await d.run_tool("a", "look", {"screenshot": False})
        r = await d.run_tool("a", "type_text", {"target": {"id": "email"}, "text": "", "look": False})
        assert r["text"].startswith("Typed into")
        assert d.session.typed[-1][:3] == ("", True, False)  # replace (the default): cleared
        r = await d.run_tool("a", "type_text", {"target": {"id": "email"}, "text": "", "replace": False,
                                                "submit": True, "look": False})
        assert d.session.typed[-1][:3] == ("", False, True)  # return alone
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "type_text", {"target": {"id": "email"}, "text": "", "replace": False, "look": False})
        assert e.value.code == "bad_params" and "nothing to type" in str(e.value)
        await finish(stop, task)
    run(go)


def test_a_recorded_secure_step_whose_text_is_still_empty_is_refused_on_replay():
    """An empty text now clears the field: a replay of a password step nobody filled in would clear it and pass."""
    with pytest.raises(Refused) as e:
        batch.args_of({"tool": "type_text", "target": {"id": "password"}, "text": "", "secure": True})
    assert e.value.code == "bad_params" and "test password" in str(e.value)
    assert batch.args_of({"tool": "type_text", "target": {"id": "password"}, "text": "pw", "secure": True})["text"] == "pw"
    assert batch.args_of({"tool": "type_text", "target": {"id": "name"}, "text": ""})["text"] == ""


# ---- L5: the copy refuses a file that changed since it was checked ----
def test_the_copy_refuses_a_clip_that_changed_since_it_was_checked_and_copies_only_its_checked_size(tmp_path, monkeypatch):
    monkeypatch.setattr(device_module, "STAGE_CHUNK", 4)
    clip = tmp_path / "walk.mp4"
    clip.write_bytes(MP4)
    video = media.read_media([str(clip)])[0]
    assert video.stamp is not None and video.stamp.size == len(MP4)
    container, opened = FakeContainer(), []
    fake_house_arrest(monkeypatch, container, opened)
    d = device_module.Device.__new__(device_module.Device)
    d.rsd = object()
    remote = "tmp/tapwright-media/ab/1.mp4"
    asyncio.run(d.stage_media(RUNNER_ID, "tmp/tapwright-media/ab", [(clip, remote)], stamps=[video.stamp]))
    assert container.files[remote] == MP4
    with open(clip, "ab") as f:  # it grows (still being exported)
        f.write(b"more")
    container2 = FakeContainer()
    fake_house_arrest(monkeypatch, container2, [])
    with pytest.raises(DeviceError) as e:
        asyncio.run(d.stage_media(RUNNER_ID, "tmp/tapwright-media/cd", [(clip, remote)], stamps=[video.stamp]))
    assert "walk.mp4 changed on this computer" in str(e.value) and "nothing was added" in str(e.value)
    assert remote not in container2.files  # refused before anything was copied


def test_the_session_hands_the_checked_stamps_to_the_copy(clips):
    video = media.read_media(clips[:1])[0]
    s, _ = staged_session({"identifiers": ["V/L0/001"]})
    asyncio.run(s.photos_add([video]))
    assert s.device.stamps == [video.stamp]


# ---- L6: more mp4 brands ----
@pytest.mark.parametrize("brand, compatible", [
    (b"3gp4", b"3gp4isom"), (b"3gp5", b""), (b"3gp6", b""), (b"3g2a", b""), (b"mmp4", b"mmp4mp41"),
])
def test_common_mp4_and_3gpp_brands_are_videos(tmp_path, brand, compatible):
    path = tmp_path / "camera.mp4"
    path.write_bytes(box(brand, compatible) + b"data")
    assert isinstance(media.read_media([str(path)])[0], media.Video)


def test_a_vendor_brand_counts_as_a_video_only_by_an_mp4_compatible_brand_inside_its_box(tmp_path):
    path = tmp_path / "camera.mp4"
    path.write_bytes(box(b"XYZ1", b"XYZ1isom") + b"data")  # a vendor's own brand, compatible with isom
    assert isinstance(media.read_media([str(path)])[0], media.Video)
    path = tmp_path / "photo.mp4"
    path.write_bytes(box(b"heic", b"mif1heic") + b"isom")  # 'isom' after the box doesn't count
    with pytest.raises(ValueError):
        media.read_media([str(path)])


# ---- L7: videos in the words ----
def test_the_remove_and_count_words_name_videos():
    assert media.removed_text(["a.jpg", "b.mp4"], ["a.jpg", "b.mp4"]) == (
        "Removed 1 picture and 1 video from the photo library: a.jpg, b.mp4.")
    assert media.removed_text(["b.mov"], ["b.mov", "c.MP4"]).startswith("Removed 1 of 2 videos from the photo library")
    assert media.removed_text(["a.png"], ["a.png"]) == "Removed 1 picture from the photo library: a.png."


def test_the_media_errors_name_videos_too():
    async def go():
        d = make()
        stop, task = await started(d)
        e = await refused(d, {})
        assert "pictures or videos" in str(e)
        e = await refused(d, {"add": "x.mp4"})
        assert "pictures or videos" in str(e)
        await finish(stop, task)
    run(go)


def test_tapwright_call_waits_long_enough_for_media_by_default(tmp_path, monkeypatch, capsys):
    """Review R179 L7: `tapwright call media` kept the 120 s default, far too short for a video's copy and import."""
    import json
    from tapwright import thin
    monkeypatch.setenv("TAPWRIGHT_HOME", str(tmp_path))
    (tmp_path / "serve.json").write_text(json.dumps({"pid": 1, "port": 1, "token": "k" * 43}))

    class Silent:
        timeout, sock = None, None

        def request(self, *a, **k):
            raise TimeoutError

        def close(self):
            return None

    monkeypatch.setattr(thin, "_proved", lambda port, token, timeout: Silent())
    waits = {}
    for tool, args in (("media", '{"add": ["clip.mp4"]}'), ("look", "{}")):
        assert thin.main([tool, args]) == thin.EXIT_UNREACHABLE
        waits[tool] = capsys.readouterr().err
    assert "within 900 s" in waits["media"] and "within 120 s" in waits["look"]
    assert thin.main(["media", "{}", "--timeout", "30"]) == thin.EXIT_UNREACHABLE
    assert "within 30 s" in capsys.readouterr().err

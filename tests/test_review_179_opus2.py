"""1.7.9, Opus review R179b: L1 (a remove's timeout in a remove's words), L4 (a media call that saw a hold which has
ended starts nothing), L5 (a first video add waits for the prompt and then the import), L6 (a dropped line during the
copy is `reconnecting`), L7 (a tool's own call during a video hold is told busy at once). All values are synthetic."""
import asyncio

import pytest
from test_daemon import finish, make, run, started
from test_media import pictures, prompted, quick, refused  # noqa: F401 (fixtures)
from test_media_179 import MP4, RUNNER_ID, FakeContainer, clips, fake_house_arrest, staged_session  # noqa: F401

from tapwright import daemon as daemon_module, device as device_module, media, session as session_module, store
from tapwright.control import Refused
from tapwright.device import ConnectionLost, DeviceError

TIMED_OUT = "WebDriverAgent: timeout: deleting took too long"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "HOME", tmp_path / "home")


def adds(d) -> int:
    return len([c for c in d.session.calls if isinstance(c, tuple) and c[0] == "photos_add"])


async def video_hold(d, clips):
    """Agent a's video add, held on the library's import: the call and its future."""
    s = d.session
    s.tw_version = 7
    s.photos_gate = asyncio.Event()
    call = asyncio.ensure_future(d.run_tool("a", "media", {"add": clips[:1]}))
    for _ in range(200):
        if d._photo_wait_left() is not None and d._photo_wait_kind == "video":
            break
        await asyncio.sleep(0.01)
    assert d._photo_wait_kind == "video"
    return call


# ---- L1: a remove that the runner timed out is worded as a remove ----
def test_a_remove_the_runner_timed_out_says_to_check_photos_not_recently_added_before_adding(clips):
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.tw_version = 7
        await d.run_tool("a", "media", {"add": clips[:1]})
        d.session.photo_deletes = device_module.RunnerTimedOut(TIMED_OUT)
        e = await refused(d, {"remove": True})
        assert e.code == "timeout"
        assert "while removing the 1 video" in str(e) and "check Photos before calling media again" in str(e)
        assert "before adding again" not in str(e) and "Recently Added" not in str(e)
        await finish(stop, task)
    run(go)


def test_an_add_the_runner_timed_out_keeps_its_recently_added_advice(clips):
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.tw_version = 7

        async def timed_out(items, sent=None, asks=False):
            if sent is not None:
                await sent()
            raise device_module.RunnerTimedOut("WebDriverAgent: timeout: adding took too long")
        d.session.photos_add = timed_out
        e = await refused(d, {"add": clips[:1]})
        assert e.code == "timeout" and "check Photos > Recently Added before adding again" in str(e)
        await finish(stop, task)
    run(go)


# ---- L4: the hold ended while this call waited for its turn: nothing starts without the version and access reads ----
def test_a_media_call_that_saw_a_hold_which_ended_meanwhile_starts_nothing_and_is_told_to_call_again(clips, quick):
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        s.tw_version = 6  # no videos on this runner: an add that skipped the version read would copy the clip anyway
        holder = asyncio.get_running_loop().create_future()  # agent a's picture add, on iOS's prompt
        d._hold(holder, 100.0, "prompt")
        await d._media_lock.acquire()  # a's add is still in its step
        call = asyncio.ensure_future(d.run_tool("b", "media", {"add": clips[:1]}))
        await asyncio.sleep(0.05)
        assert not call.done()  # b waits for the step
        holder.set_result(None)  # a's add ends: the hold is gone
        d._media_lock.release()
        with pytest.raises(Refused) as e:
            await asyncio.wait_for(call, 2.0)
        assert e.value.code == "busy" and "call media again" in str(e.value)
        assert adds(d) == 0 and d._photo_wait_left() is None
        # the retry takes the normal way: the version is read, and the old runner is named
        e = await refused(d, {"add": clips[:1]}, client_id="b")
        assert e.code == "unsupported" and "setup --force" in str(e)
        assert adds(d) == 0
        await finish(stop, task)
    run(go)


# ---- L5: a first video add's deadline and hold cover iOS's prompt and then the import ----
def test_a_first_video_adds_wait_counts_the_runners_prompt_wait_and_then_its_import_wait():
    size = 200 * 1048576
    plain = session_module.photos_timeout(size)
    first = session_module.photos_timeout(size, asks=True)
    assert first == pytest.approx(plain + session_module.RUNNER_PROMPT_WAIT)
    # the runner's worst case: 120 s for the answer, then min(600, 120 + x) for the change
    assert first >= 120 + 120 + size / session_module.VIDEO_RATE
    assert session_module.photos_timeout(0, asks=True) == session_module.PHOTOS_WAIT  # pictures: as in 1.7.8


def test_the_session_sends_a_first_video_add_with_the_longer_deadline(clips):
    video = media.read_media(clips[:1])[0]
    s, calls = staged_session({"identifiers": ["V/L0/001"]})
    asyncio.run(s.photos_add([video], asks=True))
    (_, _, _, timeout), = calls.sent
    assert timeout == pytest.approx(session_module.photos_timeout(video.size, asks=True))
    assert timeout > session_module.photos_timeout(video.size)


def test_a_first_video_add_holds_the_phone_for_the_prompt_and_then_the_import(clips, quick):
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        s.tw_version = 7
        s.photo_access = {"add": "notDetermined", "full": "notDetermined"}
        prompted(d)
        with pytest.raises(Refused):
            await d.run_tool("a", "media", {"add": clips[:1]})
        assert d._photo_wait_kind == "video-prompt" and s.photos_asks is True
        left = d._photo_wait_left()
        assert left == pytest.approx(session_module.photos_timeout(len(MP4), asks=True)
                                     + daemon_module.MEDIA_HOLD_SLACK, abs=1)
        s.photos_gate.set()
        await finish(stop, task)
    run(go)


# ---- L6: a line dropped during the copy is a drop, not a phone that can't take videos ----
class DroppingContainer(FakeContainer):
    def __init__(self, error):
        super().__init__()
        self.error = error

    async def fwrite(self, handle, data):
        raise self.error


@pytest.mark.parametrize("error", ["terminated", ConnectionResetError(10054, "reset"), BrokenPipeError()])
def test_a_line_dropped_during_the_copy_is_connection_lost(tmp_path, monkeypatch, error):
    from pymobiledevice3.exceptions import ConnectionTerminatedError
    if error == "terminated":
        error = ConnectionTerminatedError()
    clip = tmp_path / "walk.mp4"
    clip.write_bytes(MP4)
    fake_house_arrest(monkeypatch, DroppingContainer(error), [])
    d = device_module.Device.__new__(device_module.Device)
    d.rsd = object()
    with pytest.raises(ConnectionLost) as e:
        asyncio.run(d.stage_media(RUNNER_ID, "tmp/tapwright-media/ab", [(clip, "tmp/tapwright-media/ab/1.mp4")]))
    assert "dropped while copying the video" in str(e.value)


def test_an_afc_refusal_during_the_copy_is_still_a_device_error(tmp_path, monkeypatch):
    from pymobiledevice3.exceptions import AfcException
    clip = tmp_path / "walk.mp4"
    clip.write_bytes(MP4)
    fake_house_arrest(monkeypatch, DroppingContainer(AfcException("no space", 7)), [])
    d = device_module.Device.__new__(device_module.Device)
    d.rsd = object()
    with pytest.raises(DeviceError) as e:
        asyncio.run(d.stage_media(RUNNER_ID, "tmp/tapwright-media/ab", [(clip, "tmp/tapwright-media/ab/1.mp4")]))
    assert not isinstance(e.value, ConnectionLost) and "copying the video to the phone failed" in str(e.value)


def test_a_copy_cut_by_a_dropped_line_answers_reconnecting(clips):
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.tw_version = 7

        async def dropped(items, sent=None, asks=False):
            raise ConnectionLost("the line to the phone dropped while copying the video (terminated)")
        d.session.photos_add = dropped
        e = await refused(d, {"add": clips[:1]})
        assert e.code == "reconnecting" and "couldn't do it" not in str(e)
        await finish(stop, task)
    run(go)


# ---- L7: a tool's own call during a video's hold is told busy at once; a journey's restore still waits ----
def test_a_tools_own_call_during_a_video_hold_is_told_busy_at_once(clips, quick):
    async def go():
        d = make()
        stop, task = await started(d)
        call = await video_hold(d, clips)
        token = daemon_module._NESTED.set(True)  # a drive's start, a reflex, performance's own tap
        try:
            own = asyncio.ensure_future(d.run_tool("b", "look", {"screenshot": False}))
        finally:
            daemon_module._NESTED.reset(token)
        with pytest.raises(Refused) as e:
            await asyncio.wait_for(own, 1.0)
        assert e.value.code == "busy" and "a video is being added to Photos" in str(e.value)
        # a journey's own restore waits it out, as before
        token = daemon_module._JOURNEY_OWN.set(True)
        try:
            restore = asyncio.ensure_future(d.run_tool("j", "look", {"screenshot": False}))
        finally:
            daemon_module._JOURNEY_OWN.reset(token)
        await asyncio.sleep(0.1)
        assert not restore.done()
        d.session.photos_gate.set()
        assert (await asyncio.wait_for(call, 5.0))["text"].startswith("Added 1 video")
        assert (await asyncio.wait_for(restore, 5.0))["text"]
        await finish(stop, task)
    run(go)


def test_a_tools_own_call_during_ios_photo_prompt_still_waits_for_the_answer(pictures, quick):
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        s.photo_access = {"add": "notDetermined", "full": "notDetermined"}
        s.photos_gate = asyncio.Event()
        await refused(d, {"add": pictures, "confirm": True})
        assert d._photo_wait_kind == "prompt"
        token = daemon_module._NESTED.set(True)
        try:
            own = asyncio.ensure_future(d.run_tool("b", "look", {"screenshot": False}))
        finally:
            daemon_module._NESTED.reset(token)
        await asyncio.sleep(0.1)
        assert not own.done()
        s.photos_gate.set()
        assert (await asyncio.wait_for(own, 5.0))["text"]
        await finish(stop, task)
    run(go)


def test_the_media_description_says_other_calls_are_busy_and_the_cli_wait():
    from tapwright.descriptions import EXTRA
    assert "other calls answer `busy`" in EXTRA["media"] and "900 s" in EXTRA["media"]


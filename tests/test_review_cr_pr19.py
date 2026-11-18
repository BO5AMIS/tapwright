"""1.7.9, CodeRabbit on PR #19: one add at a time when a video is in it (a second add never starts while a video add's
copy or import is in flight, whatever its hold budget says), a pause is checked again right before a staged video goes
to the runner, and the ftyp brand scan reads every compatible brand in the box and takes QuickTime's. Synthetic data."""
import asyncio

import pytest
from test_daemon import finish, make, run, started
from test_media import pictures, prompted, quick, refused  # noqa: F401 (fixtures)
from test_media_179 import box, clips, staged_session  # noqa: F401

from tapwright import daemon as daemon_module, media, store
from tapwright.control import Refused


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "HOME", tmp_path / "home")


def adds(d) -> int:
    return len([c for c in d.session.calls if isinstance(c, tuple) and c[0] == "photos_add"])


# ---- one add at a time when a video is in it ----
def test_no_second_add_starts_while_a_video_add_whose_hold_ran_out_is_still_copying(clips, pictures, quick):
    """A's copy outlasted its hold budget, so nothing held the phone: B's add started, A's sent() only stretched B's
    hold, and B's end cleared it while A still imported (heartbeat and other calls then reached the busy runner)."""
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        s.tw_version = 7
        s.stage_gate = asyncio.Event()  # A's copy takes this long
        call = asyncio.ensure_future(d.run_tool("a", "media", {"add": clips[:1]}))
        await asyncio.sleep(0.1)
        d.control.clock.t += 10_000  # far past the copy's budget: the hold ran out
        assert d._photo_wait_left() is None
        for other in (clips[1:2], pictures[:1]):  # another agent's video, or its pictures
            with pytest.raises(Refused) as e:  # (before the fix B's video add queued behind A's copy: never answered)
                await asyncio.wait_for(d.run_tool("b", "media", {"add": other}), 2.0)
            assert e.value.code == "busy" and "with a video is still going on" in str(e.value)
        assert adds(d) == 1
        s.stage_gate.set()
        r = await asyncio.wait_for(call, 5.0)
        assert r["text"].startswith("Added 1 video")
        r = await d.run_tool("b", "media", {"add": clips[1:2]})  # A is done: B adds now
        assert r["text"].startswith("Added 1 video") and adds(d) == 2
        await finish(stop, task)
    run(go)


def test_a_video_add_doesnt_start_behind_a_picture_add_still_waiting_on_the_person(clips, pictures, quick):
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        s.tw_version = 7
        s.photo_access = {"add": "notDetermined", "full": "notDetermined"}
        prompted(d)
        e = await refused(d, {"add": pictures[:1]})
        assert e.code == "not_allowed"  # iOS asks; the add waits on the person
        d.control.clock.t += daemon_module.PHOTO_WAIT_MAX + 1  # its hold ran out, the person still hasn't answered
        assert d._photo_wait_left() is None
        e = await refused(d, {"add": clips[:1]}, client_id="b")
        assert e.code == "busy" and adds(d) == 1
        s.photos_gate.set()
        await finish(stop, task)
    run(go)


# ---- a pause is checked again right before the staged video goes out ----
def test_a_pause_during_a_videos_copy_sends_nothing_removes_the_staged_folder_and_answers_paused(clips):
    video = media.read_media(clips[:1])[0]
    s, calls = staged_session({"identifiers": ["V/L0/001"]})

    async def paused() -> None:
        raise Refused("paused", "paused by the user in the live view; ask them to resume")
    with pytest.raises(Refused) as e:
        asyncio.run(s.photos_add([video], sent=paused))
    assert e.value.code == "paused"
    assert calls.sent == []  # no POST to the runner
    assert [c[0] for c in s.device.calls] == ["sweep", "stage", "unstage"]


def test_the_daemon_checks_the_pause_again_once_the_copy_is_done(clips, quick):
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        s.tw_version = 7
        s.stage_gate = asyncio.Event()
        call = asyncio.ensure_future(d.run_tool("a", "media", {"add": clips[:1]}))
        await asyncio.sleep(0.1)
        d.control.set_paused(True)  # the person pauses the phone while the clip is copied
        s.stage_gate.set()
        with pytest.raises(Refused) as e:
            await asyncio.wait_for(call, 5.0)
        assert e.value.code == "paused"
        assert not getattr(s, "photos", [])  # nothing was added
        assert d._photo_wait_left() is None  # the hold went with the add
        d.control.set_paused(False)
        r = await d.run_tool("a", "media", {"add": clips[:1]})  # resumed: the same clip is added, not "said once"
        assert r["text"].startswith("Added 1 video") and adds(d) == 2
        await finish(stop, task)
    run(go)


# ---- the brand scan: every compatible brand in the box, QuickTime's too ----
def test_a_compatible_brand_past_the_first_64_bytes_still_counts(tmp_path):
    path = tmp_path / "camera.mp4"
    path.write_bytes(box(b"XYZ1", b"XYZ1" * 12 + b"mp42") + b"data")  # mp42 at byte 64
    assert isinstance(media.read_media([str(path)])[0], media.Video)
    path.write_bytes(box(b"XYZ1", b"XYZ1" * 12) + b"mp42" + bytes(64))  # just past the box: not its brand
    with pytest.raises(ValueError):
        media.read_media([str(path)])


def test_a_vendor_brand_compatible_with_quicktime_is_a_video_and_heic_still_isnt(tmp_path):
    path = tmp_path / "camera.mov"
    path.write_bytes(box(b"vndr", b"vndrqt  ") + b"data")
    assert isinstance(media.read_media([str(path)])[0], media.Video)
    path = tmp_path / "photo.mov"
    path.write_bytes(box(b"heic", b"mif1heic") + b"data")
    with pytest.raises(ValueError):
        media.read_media([str(path)])


def test_the_scan_is_bounded_by_a_cap_on_huge_boxes(tmp_path):
    path = tmp_path / "odd.mp4"
    path.write_bytes(box(b"XYZ1", b"XYZ1" * (media.FTYP_MAX // 4) + b"mp42") + b"data")  # its one brand past the cap
    with pytest.raises(ValueError):
        media.read_media([str(path)])

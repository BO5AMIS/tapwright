"""1.7.9: video in `media add` (BUGS-REPORT2, "Feature: video in media add"), and three leftovers of 1.7.8 (type_text with
empty text, unlock's no-passcode words, the wake limit on disk). All values are synthetic."""
import asyncio
import base64
import hashlib
import json

import pytest
from test_daemon import finish, make, run, started
from test_media import pictures, prompted, quick, refused, runner  # noqa: F401 (fixtures)
from test_unlock import PASSCODE, Screen, unlocker

from tapwright import daemon as daemon_module, mcp_server, media, session as session_module, store, ui, unlock
from tapwright.control import Refused
from tapwright.device import DeviceError, Unanswered


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "HOME", tmp_path / "home")


def box(brand: bytes, compatible: bytes = b"") -> bytes:
    """An ISO base media file's first box: size, 'ftyp', the major brand, a minor version, compatible brands."""
    body = b"ftyp" + brand + bytes(4) + compatible
    return (len(body) + 4).to_bytes(4, "big") + body


MP4 = box(b"isom", b"isomiso2avc1mp41") + b"\x00\x00\x00\x08free" + b"clip data"
MOV = box(b"qt  ", b"qt  ") + b"\x00\x00\x00\x08wide" + b"quicktime data"
M4V = box(b"M4V ", b"M4V M4A mp42isom") + b"m4v data"
OLD_MOV = b"\x00\x00\x00\x10moov" + b"an old QuickTime file without ftyp"


@pytest.fixture
def clips(tmp_path):
    out = []
    for name, data in (("walk.mp4", MP4), ("pan.MOV", MOV), ("tv.m4v", M4V)):
        path = tmp_path / name
        path.write_bytes(data)
        out.append(str(path))
    return out


# ---- the files ----
def test_mp4_mov_and_m4v_are_videos_by_their_ftyp_brand_and_stay_on_disk(clips, pictures, tmp_path):
    items = media.read_media([pictures[0], *clips])
    assert items[0] == ("food.jpg", (tmp_path / "food.jpg").read_bytes())  # a picture: as before
    videos = items[1:]
    assert all(isinstance(v, media.Video) for v in videos)
    assert [v.name for v in videos] == ["walk.mp4", "pan.MOV", "tv.m4v"]
    assert [v.size for v in videos] == [len(MP4), len(MOV), len(M4V)]
    assert videos[0].digest == hashlib.sha256(MP4).hexdigest()
    assert not hasattr(videos[0], "data")  # a clip is never read into memory whole
    old = tmp_path / "old.mov"
    old.write_bytes(OLD_MOV)
    assert isinstance(media.read_media([str(old)])[0], media.Video)


@pytest.mark.parametrize("name, data, said", [
    ("photo.mp4", box(b"heic", b"mif1heic") + b"x", "isn't a mp4 video"),
    ("still.mov", bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b"jpeg", "isn't a mov video"),
    ("odd.m4v", box(b"avif") + b"x", "isn't a m4v video"),
    ("short.mp4", b"\x00\x00", "isn't a mp4 video"),
])
def test_a_video_name_on_something_else_is_refused_by_its_first_bytes(tmp_path, name, data, said):
    path = tmp_path / name
    path.write_bytes(data)
    with pytest.raises(ValueError) as e:
        media.read_media([str(path)])
    assert said in str(e.value) and name in str(e.value)


def test_videos_have_limits_of_their_own_and_the_picture_limits_are_unchanged(tmp_path, monkeypatch, pictures):
    assert (media.MAX_BYTES, media.MAX_TOTAL, media.MAX_FILES) == (20 * 1048576, 40 * 1048576, 20)
    assert (media.MAX_VIDEO_BYTES, media.MAX_VIDEO_TOTAL, media.MAX_VIDEOS) == (200 * 1048576, 400 * 1048576, 5)
    clip = tmp_path / "a.mp4"
    clip.write_bytes(MP4)
    with pytest.raises(ValueError) as e:
        media.read_media([str(clip)] * 6)
    assert "at most 5 videos" in str(e.value)
    monkeypatch.setattr(media, "MAX_VIDEO_BYTES", len(MP4) - 1)
    with pytest.raises(ValueError) as e:
        media.read_media([str(clip)])
    assert "a.mp4" in str(e.value) and "limit for a video" in str(e.value)
    monkeypatch.setattr(media, "MAX_VIDEO_BYTES", len(MP4))
    monkeypatch.setattr(media, "MAX_VIDEO_TOTAL", 2 * len(MP4))
    with pytest.raises(ValueError) as e:
        media.read_media([str(clip)] * 3)
    assert "videos together" in str(e.value)
    # a clip doesn't count against the pictures' 40 MB: the two caps are apart
    monkeypatch.setattr(media, "MAX_TOTAL", 40)
    assert len(media.read_media([pictures[0], str(clip)])) == 2
    with pytest.raises(ValueError) as e:  # the words name what can be added now
        media.read_media([str(tmp_path / "notes.txt")])
    assert "jpeg, png and heic pictures and mp4, mov and m4v videos" in str(e.value)


# ---- the tool ----
def test_a_video_on_a_runner_older_than_7_is_unsupported_and_names_setup_force(clips, pictures):
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.tw_version = 6
        e = await refused(d, {"add": [pictures[0], clips[0]]})
        assert e.code == "unsupported"
        assert "tapwright setup --force" in str(e) and "route version is 6" in str(e) and "videos need 7" in str(e)
        assert not any(isinstance(c, tuple) and c[0] == "photos_add" for c in d.session.calls)
        r = await d.run_tool("a", "media", {"add": pictures[:1]})  # pictures alone still go to a v6 runner
        assert r["text"].startswith("Added 1 picture to the photo library: food.jpg.")
        await finish(stop, task)
    run(go)


def test_a_video_add_on_runner_7_is_recorded_and_said_as_a_video(clips, pictures):
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.tw_version = 7
        r = await d.run_tool("a", "media", {"add": [pictures[0], clips[0]]})
        assert r["text"] == ("Added 1 picture and 1 video to the photo library: food.jpg, walk.mp4. "
                             "They stay until removed (media with remove: true).")
        assert ("photos_add", ("food.jpg", "walk.mp4")) in d.session.calls
        assert [e["name"] for e in media.load()["a"]] == ["food.jpg", "walk.mp4"]
        r = await d.run_tool("a", "media", {"add": clips[1:]})
        assert r["text"].startswith("Added 2 videos to the photo library: pan.MOV, tv.m4v.")
        await finish(stop, task)
    run(go)


def test_the_first_video_add_waits_for_the_upload_before_it_says_ios_is_asking(clips, quick):
    """While the clip is copied to the phone the runner isn't held, and iOS has asked nothing yet: the answer that a
    person is needed comes once the add is sent."""
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.tw_version = 7
        d.session.photo_access = {"add": "notDetermined", "full": "notDetermined"}
        prompted(d)
        d.session.stage_gate = asyncio.Event()
        call = asyncio.ensure_future(d.run_tool("a", "media", {"add": clips[:1]}))
        await asyncio.sleep(0.2)
        assert not call.done()  # (on 1.7.8 there is no upload: it answered after one MEDIA_WATCH)
        d.session.stage_gate.set()
        with pytest.raises(Refused) as e:
            await call
        assert e.value.code == "not_allowed" and "iOS is asking" in str(e.value)
        d.session.photos_gate.set()
        await finish(stop, task)
    run(go)


def test_the_mcp_client_waits_long_enough_for_a_big_clip():
    assert mcp_server._timeout("media", {"add": ["x.mp4"]}) >= 900


# ---- the session: staged in the runner's container, then sent by path ----
class Stager:
    """The device's side of staging: house_arrest into the runner's container (fake)."""

    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    async def stage_media(self, bundle_id, folder, files, stamps=None):
        self.calls.append(("stage", bundle_id, folder, [(str(p), n) for p, n in files]))
        self.stamps = stamps
        if self.fail is not None:
            raise self.fail

    async def unstage_media(self, bundle_id, folder):
        self.calls.append(("unstage", bundle_id, folder))

    async def sweep_media(self, bundle_id, older_than):
        self.calls.append(("sweep", bundle_id, older_than))


RUNNER_ID = "com.example.tapwright.WebDriverAgentRunner.xctrunner"


def staged_session(answer):
    s, calls = runner({"photos/add": answer})
    s.device = Stager()
    store.save_state(runner_id=RUNNER_ID)
    return s, calls


def test_a_video_is_staged_in_the_runners_container_sent_by_path_and_its_folder_removed(clips, pictures):
    s, calls = staged_session({"identifiers": ["P/L0/001", "V/L0/001"]})
    photo = media.read_media(pictures[:1])[0]
    video = media.read_media(clips[:1])[0]
    assert asyncio.run(s.photos_add([photo, video])) == ["P/L0/001", "V/L0/001"]
    sweep, stage, unstage = s.device.calls
    assert sweep == ("sweep", RUNNER_ID, session_module.STAGE_STALE)
    _, bundle, folder, files = stage
    assert bundle == RUNNER_ID and folder.startswith("tmp/tapwright-media/") and folder.count("/") == 2
    assert files == [(clips[0], f"{folder}/1.mp4")]
    assert unstage == ("unstage", RUNNER_ID, folder)
    (method, path, payload, timeout), = calls.sent
    assert (method, path) == ("POST", "photos/add")
    assert payload == {"items": [{"kind": "photo", "data": base64.b64encode(photo[1]).decode(), "name": "food.jpg"},
                                 {"kind": "video", "file": f"{folder}/1.mp4", "name": "walk.mp4"}]}
    assert timeout > session_module.PHOTOS_WAIT


def test_the_wait_for_a_video_add_grows_with_its_size_to_a_cap():
    small = session_module.photos_timeout(0)
    assert small == session_module.PHOTOS_WAIT
    assert session_module.photos_timeout(200 * 1048576) == pytest.approx(session_module.PHOTOS_WAIT + 50)
    assert session_module.photos_timeout(10 ** 12) == session_module.PHOTOS_WAIT + 480


def test_a_refused_add_or_a_failed_upload_removes_the_staged_folder(clips):
    """A real refusal (bad input, no access, unsupported) or a failed copy: nothing is moving, so the folder goes. The
    runner's own timeout is no refusal (review R179 M1: test_review_179_opus1)."""
    video = media.read_media(clips[:1])[0]
    s, calls = staged_session(DeviceError("the photo library refused the pictures: no"))
    with pytest.raises(DeviceError):
        asyncio.run(s.photos_add([video]))
    assert [c[0] for c in s.device.calls] == ["sweep", "stage", "unstage"]

    s, calls = staged_session({"identifiers": ["V/L0/001"]})
    s.device.fail = DeviceError("the runner's container couldn't be opened")
    with pytest.raises(DeviceError):
        asyncio.run(s.photos_add([video]))
    assert [c[0] for c in s.device.calls] == ["sweep", "stage", "unstage"] and calls.sent == []


def test_after_a_timeout_the_staged_clip_is_left_to_the_runner_and_swept_by_the_next_add(clips):
    """The library may still be moving the clip when the answer times out: removing it then could break the import.
    The runner removes it when the change ends; the next video add sweeps folders older than STAGE_STALE."""
    video = media.read_media(clips[:1])[0]
    s, calls = staged_session(Unanswered("no answer"))
    with pytest.raises(Unanswered):
        asyncio.run(s.photos_add([video]))
    assert [c[0] for c in s.device.calls] == ["sweep", "stage"]
    assert session_module.STAGE_STALE >= 3600


# ---- the device: house_arrest into the runner's container ----
class FakeContainer:
    """pymobiledevice3's HouseArrestService, as far as staging uses it: AFC rooted at the app's container."""

    def __init__(self, files=None, refuse=None):
        self.files = files if files is not None else {}  # path -> bytes, or {"mtime": datetime} for a folder
        self.ops, self.refuse, self.handles = [], refuse, {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.ops.append("close")

    async def makedirs(self, path):
        self.ops.append(("mkdir", path))

    async def fopen(self, path, mode="r"):
        self.handles[7] = path
        self.files[path] = b""
        return 7

    async def fwrite(self, handle, data):
        self.ops.append(("write", self.handles[handle], len(data)))
        self.files[self.handles[handle]] += data

    async def fclose(self, handle):
        self.ops.append(("fclose", self.handles.pop(handle)))

    async def rm(self, path, force=False):
        self.ops.append(("rm", path))
        return []

    async def exists(self, path):
        return any(p.startswith(path) for p in self.files)

    async def listdir(self, path):
        return sorted({p[len(path) + 1:].split("/")[0] for p in self.files if p.startswith(path + "/")})

    async def stat(self, path):
        return {"st_mtime": self.files[path]["mtime"]}


def fake_house_arrest(monkeypatch, container, opened):
    from pymobiledevice3.exceptions import PyMobileDevice3Exception
    from pymobiledevice3.services import house_arrest

    async def create(lockdown, bundle_id, documents_only=False):
        opened.append((bundle_id, documents_only))
        if container.refuse:
            raise PyMobileDevice3Exception(container.refuse)
        return container
    monkeypatch.setattr(house_arrest.HouseArrestService, "create", staticmethod(create))


def test_the_device_copies_a_clip_in_chunks_into_the_runners_whole_container(tmp_path, monkeypatch):
    from tapwright import device as device_module
    monkeypatch.setattr(device_module, "STAGE_CHUNK", 4)
    clip = tmp_path / "walk.mp4"
    clip.write_bytes(MP4)
    container, opened = FakeContainer(), []
    fake_house_arrest(monkeypatch, container, opened)
    d = device_module.Device.__new__(device_module.Device)
    d.rsd = object()
    asyncio.run(d.stage_media(RUNNER_ID, "tmp/tapwright-media/ab", [(clip, "tmp/tapwright-media/ab/1.mp4")]))
    assert opened == [(RUNNER_ID, False)]  # VendContainer: the whole container, where the runner's tmp is
    assert container.files["tmp/tapwright-media/ab/1.mp4"] == MP4
    writes = [op for op in container.ops if op[0] == "write"]
    assert len(writes) == -(-len(MP4) // 4) and all(n <= 4 for _, _, n in writes)
    assert container.ops[0] == ("mkdir", "tmp/tapwright-media/ab") and container.ops[-1] == "close"
    assert ("fclose", "tmp/tapwright-media/ab/1.mp4") in container.ops
    asyncio.run(d.unstage_media(RUNNER_ID, "tmp/tapwright-media/ab"))
    assert ("rm", "tmp/tapwright-media/ab") in container.ops


def test_the_device_sweeps_only_old_staged_folders_and_words_a_refused_container(tmp_path, monkeypatch):
    import datetime
    from tapwright import device as device_module
    now = datetime.datetime.now()
    container, opened = FakeContainer({"tmp/tapwright-media/old": {"mtime": now - datetime.timedelta(hours=2)},
                                       "tmp/tapwright-media/new": {"mtime": now}}), []
    fake_house_arrest(monkeypatch, container, opened)
    d = device_module.Device.__new__(device_module.Device)
    d.rsd = object()
    asyncio.run(d.sweep_media(RUNNER_ID, 3600))
    assert [op for op in container.ops if op[0] == "rm"] == [("rm", "tmp/tapwright-media/old")]
    container.refuse = "InstallationLookupFailed"
    with pytest.raises(DeviceError) as e:
        asyncio.run(d.stage_media(RUNNER_ID, "tmp/tapwright-media/x", []))
    assert "runner's container" in str(e.value) and "tapwright setup --force" in str(e.value)


def test_pictures_alone_keep_the_v3_shape_and_stage_nothing(pictures):
    s, calls = staged_session({"identifiers": ["P/L0/001"]})
    photo = media.read_media(pictures[:1])[0]
    asyncio.run(s.photos_add([photo]))
    assert s.device.calls == []
    assert calls.sent[0][2] == {"images": [base64.b64encode(photo[1]).decode()], "names": ["food.jpg"]}
    assert calls.sent[0][3] == session_module.PHOTOS_WAIT


# ---- 1.7.8 leftovers ----
def test_type_text_with_empty_text_and_nothing_to_do_is_bad_params_in_plain_words():
    """Only an empty text with replace false and no submit is refused (review R179 M3: an empty text with replace, the
    default, clears the field as 1.7.8 did; with submit it presses Return)."""
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.tree = [ui.Element("e1", "input", "Name", "name-field", rect=(10, 100, 100, 40), actions=("tap", "type"))]
        await d.run_tool("a", "look", {"screenshot": False})
        for target in ({"text": "Name"}, {"id": "name-field"}, None):
            args = {"text": "", "replace": False, "look": False, **({"target": target} if target else {})}
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "type_text", args)
            assert e.value.code == "bad_params" and str(e.value).startswith("text is empty and replace is false"), target
        r = await d.run_tool("a", "type_text", {"text": "", "submit": True, "look": False})  # return alone: allowed
        assert r["text"].startswith("Typed into")
        await finish(stop, task)
    run(go)


def test_unlocks_no_passcode_words_dont_claim_a_lock_notification_center_may_be_open():
    for words in (unlock.NO_PASSCODE, daemon_module.UNLOCK_NO_PASSCODE):
        assert words.startswith("the phone reads as locked (or Notification Center is open"), words
        assert "ask the user to unlock it" in words
    assert "phone.passcode" in daemon_module.UNLOCK_NO_PASSCODE and "--accounts FILE" in daemon_module.UNLOCK_NO_PASSCODE


def test_the_wake_limit_is_kept_on_disk_with_the_attempts(tmp_path):
    """Fails on def30f4: the 3 wakes in 10 minutes were counted in memory, so a restarted daemon (or another process)
    woke the lock screen again."""
    path = tmp_path / "record.json"
    first, clock = unlocker(path=path)
    phone = Screen(items=[])

    async def go():
        for _ in range(unlock.WAKE_LIMIT):
            assert (await first.unlock(phone, PASSCODE)).code == "not_proven"
        again, _ = unlocker(clock=clock, path=path)  # a new daemon, the same record
        r = await again.unlock(phone, PASSCODE)
        assert r.code == "wake_limit" and phone.calls.count("wake") == unlock.WAKE_LIMIT
        assert len(json.loads(path.read_text(encoding="utf-8"))["wakes"]) == unlock.WAKE_LIMIT
        clock.t += unlock.WAKE_WINDOW
        assert (await again.unlock(phone, PASSCODE)).code == "not_proven"
    asyncio.run(go())


@pytest.mark.parametrize("wakes", ["three", [1, "x"], {"t": 1}])
def test_a_record_whose_wakes_cant_be_read_wakes_nothing(tmp_path, wakes):
    path = tmp_path / "record.json"
    path.write_text(json.dumps({"version": 1, "tries": [], "refused": None, "retried": False, "pending": None,
                                "wakes": wakes}), encoding="utf-8")
    u, _ = unlocker(path=path)
    phone = Screen(items=[])
    r = asyncio.run(u.unlock(phone, PASSCODE))
    assert r.code == "no_record" and phone.calls == []


def test_a_wake_that_typed_leaves_the_wake_count_and_a_record_without_wakes_still_reads(tmp_path):
    path = tmp_path / "record.json"
    path.write_text(json.dumps({"version": 1, "tries": [], "refused": None, "retried": False, "pending": None}),
                    encoding="utf-8")
    u, _ = unlocker(path=path)
    assert asyncio.run(u.unlock(Screen(), PASSCODE)).code == "unlocked"
    assert json.loads(path.read_text(encoding="utf-8"))["wakes"] == []

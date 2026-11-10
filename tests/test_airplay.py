"""The AirPlay receiver's command line, and what the daemon does when video can't be set up."""
import asyncio

from test_daemon import make, started, finish

from tapwright import airplay, store, video


def test_the_command_sends_the_phones_h264_as_rtp_to_the_hub_with_a_fixed_id(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "HOME", tmp_path)
    args = airplay.command("uxplay", 5791, (1290, 2796))
    assert args[0] == "uxplay" and args[args.index("-n") + 1] == "Tapwright"
    assert "port=5791" in args[args.index("-vrtp") + 1] and "host=127.0.0.1" in args[args.index("-vrtp") + 1]
    assert args[args.index("-s") + 1] == "1290x2796@60" and "-a" in args  # the phone's own size; no audio
    first = args[args.index("-m") + 1]
    assert len(first) == 17 and int(first[:2], 16) & 0x03 == 0x02  # locally administered, unicast
    assert airplay.command("uxplay", 1, (2, 2))[-2] == first  # made once, then kept


def test_find_takes_the_named_receiver_only_if_it_exists(tmp_path, monkeypatch):
    exe = tmp_path / "uxplay.exe"
    monkeypatch.setenv("TAPWRIGHT_UXPLAY", str(exe))
    assert airplay.find() is None
    exe.write_bytes(b"")
    assert airplay.find() == str(exe)


def test_without_what_video_needs_the_daemon_says_why_and_keeps_the_jpeg_stream(monkeypatch):
    async def go():
        said = []
        d = make(log=said.append)
        stop, task = await started(d)
        monkeypatch.setattr(video, "available", lambda: False)
        assert "PyAV isn't installed" in await d.start_video()
        monkeypatch.setattr(video, "available", lambda: True)
        monkeypatch.setattr(airplay, "find", lambda: None)
        assert "no AirPlay receiver found" in await d.start_video()
        assert d.video is None and "video" not in d.status()["accepts"]
        assert all("stays on the JPEG stream" in line for line in said if line.startswith("video:"))
        await finish(stop, task)
    asyncio.run(go())

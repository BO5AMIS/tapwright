"""The pid-liveness lock (docs/AGENT-TOOLS.md §22.3, 1.14.2), used by the policy file's edit lock (§19.5)."""
import errno
import json
import os
import subprocess
import sys

from tapwright import pidlock


def deps(**over):
    d = pidlock.Deps(pid=4242, alive=lambda pid: True, start_of=lambda pid: "T1")
    for k, v in over.items():
        setattr(d, k, v)
    return d


def test_a_lock_is_published_whole_with_pid_and_start(tmp_path):
    path = tmp_path / "x.lock"
    seen = []
    token = pidlock.take(path, deps(before_publish=lambda p: seen.append(os.path.exists(p))))
    assert token is not None and seen == [False]  # the name doesn't exist until the whole file is linked to it
    held = json.loads(path.read_text(encoding="utf-8"))
    assert held == {"owner": token, "pid": 4242, "started": "T1"}
    assert [p.name for p in tmp_path.iterdir()] == ["x.lock"]  # no temp file left


def test_a_live_holder_is_never_displaced(tmp_path):
    path = tmp_path / "x.lock"
    assert pidlock.take(path, deps(pid=1)) is not None
    assert pidlock.take(path, deps(pid=2)) is None
    assert pidlock.state(path, deps()) == ("live", 1)


def test_a_dead_holder_is_taken_over_and_a_reused_pid_is_seen(tmp_path):
    path = tmp_path / "x.lock"
    first = pidlock.take(path, deps(pid=1))
    second = pidlock.take(path, deps(pid=2, alive=lambda pid: pid != 1))
    assert second is not None and second != first and not (tmp_path / "x.lock.takeover").exists()
    # pid 2 runs again, but it is another process (another start time): the lock's holder is gone
    third = pidlock.take(path, deps(pid=3, start_of=lambda pid: "T2" if pid == 2 else "T1"))
    assert third is not None and json.loads(path.read_text("utf-8"))["owner"] == third


def test_an_empty_or_unreadable_lock_is_never_taken_over(tmp_path):
    path = tmp_path / "x.lock"
    for text in ("", "{not json", '{"pid": 1}'):
        path.write_text(text, encoding="utf-8")
        assert pidlock.take(path, deps(alive=lambda pid: False)) is None
        assert path.read_text(encoding="utf-8") == text
    assert pidlock.state(path, deps())[0] == "unreadable"
    path.write_text("", encoding="utf-8")
    assert pidlock.state(path, deps())[0] == "empty"


def test_a_live_takeover_blocks_and_a_dead_ones_is_cleared(tmp_path):
    path = tmp_path / "x.lock"
    gate = tmp_path / "x.lock.takeover"
    gate.write_text(json.dumps({"owner": "o", "pid": 9, "started": "T1"}), encoding="utf-8")
    assert pidlock.take(path, deps()) is None  # a takeover is under way
    assert pidlock.take(path, deps(alive=lambda pid: pid != 9)) is not None


def test_release_only_while_it_is_still_yours(tmp_path):
    path = tmp_path / "x.lock"
    token = pidlock.take(path, deps())
    pidlock.release(path, "someone-else")
    assert path.exists()
    pidlock.release(path, token)
    assert not path.exists()


def test_without_hard_links_it_is_created_exclusively(tmp_path):
    def no_links(src, dst):
        raise OSError(errno.EPERM, "no hard links here")
    path = tmp_path / "x.lock"
    token = pidlock.take(path, deps(link=no_links))
    assert token is not None and json.loads(path.read_text("utf-8"))["owner"] == token
    assert pidlock.take(path, deps(link=no_links, pid=5)) is None


def test_the_real_process_facts():
    me = os.getpid()
    assert pidlock.process_alive(me)
    start = pidlock.process_start(me)
    assert start is not None and pidlock.process_start(me) == start
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert not pidlock.process_alive(child.pid) or pidlock.process_start(child.pid) != start
    assert not pidlock.process_alive(-1) and pidlock.process_start(0) is None


def test_the_start_time_is_the_text_tapline_writes():
    """Both tools take the same `<policy file>.edit.lock`, so `started` must be one text: .NET's round-trip UTC time on
    Windows ((Get-Process).StartTime.ToUniversalTime().ToString('o'), Tapline's), `ps -o lstart=` elsewhere."""
    assert pidlock.filetime_text(0) == "1601-01-01T00:00:00.0000000Z"
    assert pidlock.filetime_text(134_074_872_124_810_000) == "2025-11-13T06:00:12.4810000Z"
    if sys.platform == "win32":
        import re
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{7}Z", pidlock.process_start(os.getpid()))

"""The pid-liveness lock (docs/AGENT-TOOLS.md §22.3, 1.14.2), used by the policy file's edit lock (§19.5)."""
import errno
import json
import os
import subprocess
import sys

from tapwright import pidlock

T1 = 1_763_013_612_481  # a start time, UTC ms


def deps(**over):
    d = pidlock.Deps(pid=4242, alive=lambda pid: True, start_of=lambda pid: T1)
    for k, v in over.items():
        setattr(d, k, v)
    return d


def test_a_lock_is_published_whole_with_pid_and_start(tmp_path):
    path = tmp_path / "x.lock"
    seen = []
    token = pidlock.take(path, deps(before_publish=lambda p: seen.append(os.path.exists(p))))
    assert token is not None and seen == [False]  # the name doesn't exist until the whole file is linked to it
    held = json.loads(path.read_text(encoding="utf-8"))
    assert held == {"owner": token, "pid": 4242, "started": T1}
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
    third = pidlock.take(path, deps(pid=3, start_of=lambda pid: T1 + 5_000 if pid == 2 else T1))
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
    gate.write_text(json.dumps({"owner": "o", "pid": 9, "started": T1}), encoding="utf-8")
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


def test_started_is_integer_utc_ms_the_number_tapline_writes():
    """v1.16 (§22.3): `started` is the start time as an integer of ms since 1970-01-01 UTC, rounded down; Tapline writes
    `[DateTimeOffset]StartTime).ToUnixTimeMilliseconds()` on Windows and parses `ps -o lstart=` under LC_ALL=C TZ=UTC."""
    assert pidlock.filetime_ms(116_444_736_000_000_000) == 0
    assert pidlock.filetime_ms(134_074_872_124_819_999) == 1_763_013_612_481  # 2025-11-13T06:00:12.4819999Z, floored
    assert pidlock.parse_lstart("Fri Nov 13 05:00:00 2026\n") == 1_794_546_000_000
    assert pidlock.parse_lstart("Mon Nov  2 05:00:07 2026") == 1_793_595_607_000  # a padded day
    for bad in ("", "13 Nov 2026 05:00", "Fri Foo 13 05:00:00 2026", "Fri Feb 30 05:00:00 2026", "ven. nov. 13 05:00:00 2026"):
        assert pidlock.parse_lstart(bad) is None
    seen = []

    def ps(pid):
        seen.append(pid)
        return "Fri Nov 13 05:00:00 2026"
    assert pidlock.process_start(77, platform="darwin", run_ps=ps) == 1_794_546_000_000 and seen == [77]

    def broken(pid):
        raise OSError("no ps")
    assert pidlock.process_start(77, platform="linux", run_ps=broken) is None
    if sys.platform == "win32":
        import time
        mine = pidlock.process_start(os.getpid())
        assert pidlock.is_ms(mine) and 0 < time.time() * 1000 - mine < 86_400_000 * 30


def test_the_ps_call_fixes_the_locale_and_zone(monkeypatch):
    calls = []

    class Done:
        stdout = "Fri Nov 13 05:00:00 2026\n"

    def fake_run(args, **kw):
        calls.append((args, kw["env"]))
        return Done()
    monkeypatch.setattr(pidlock.subprocess, "run", fake_run)
    monkeypatch.setenv("LC_ALL", "fr_FR.UTF-8")
    assert pidlock.process_start(77, platform="linux") == 1_794_546_000_000
    assert calls[0][0] == ["ps", "-o", "lstart=", "-p", "77"]
    assert calls[0][1]["LC_ALL"] == "C" and calls[0][1]["TZ"] == "UTC"


def test_a_reused_pid_is_seen_only_in_another_second(tmp_path):
    """Readers compare floor(started / 1000) with the live start floored the same way: the same second is the same
    process (a tool that knows only whole seconds agrees), another second is a reuse."""
    path = tmp_path / "x.lock"
    path.write_text(json.dumps({"owner": "o", "pid": 7, "started": 1_700_000_000_999}), encoding="utf-8")
    same_second = deps(pid=8, start_of=lambda pid: 1_700_000_000_000)
    assert pidlock.state(path, same_second) == ("live", 7) and pidlock.take(path, same_second) is None
    next_second = deps(pid=8, start_of=lambda pid: 1_700_000_001_000)
    assert pidlock.state(path, next_second) == ("dead", 7)
    token = pidlock.take(path, next_second)
    assert token is not None and json.loads(path.read_text("utf-8"))["owner"] == token
    # the live start time unknown: no proof, the holder stands
    path.write_text(json.dumps({"owner": "o", "pid": 7, "started": 1_700_000_000_000}), encoding="utf-8")
    assert pidlock.take(path, deps(pid=8, start_of=lambda pid: None)) is None


def test_a_dead_pid_is_stale_whatever_its_started(tmp_path):
    path = tmp_path / "x.lock"
    for started in (1_700_000_000_000, "2026-11-14T08:37:24.7357940Z", None):
        path.write_text(json.dumps({"owner": "o", "pid": 7, "started": started}), encoding="utf-8")
        gone = deps(pid=8, alive=lambda pid: pid != 7)
        assert pidlock.state(path, gone) == ("dead", 7)
        assert pidlock.take(path, gone) is not None
        path.unlink()


def test_an_older_tools_text_started_counts_as_alive_and_unlock_clears_it(tmp_path):
    """A non-integer `started` (W123's .NET text, `ps` text) can't prove a reused pid: never taken over while the pid
    runs, even if the live start time is another; a person's `unlock` clears it, and so does the holder's death."""
    path = tmp_path / "x.lock"
    for old in ("2026-11-14T08:37:24.7357940Z", "Fri Nov 13 05:00:00 2026", 1.7e12, True):
        path.write_text(json.dumps({"owner": "o", "pid": 7, "started": old}), encoding="utf-8")
        other_start = deps(pid=8, start_of=lambda pid: 1_800_000_000_000)
        assert pidlock.take(path, other_start) is None
        assert pidlock.state(path, other_start)[0] in ("old", "live")
    path.write_text(json.dumps({"owner": "o", "pid": 7, "started": "2026-11-14T08:37:24.7357940Z"}), encoding="utf-8")
    assert pidlock.state(path, deps()) == ("old", 7)
    done = pidlock.unlock(path, deps())
    assert done.status == "removed" and not path.exists()
    assert done.removed == [(str(path), "started in an older format, cleared by unlock (pid 7)")]
    # an integer holder that is alive is still refused
    path.write_text(json.dumps({"owner": "o", "pid": 7, "started": T1}), encoding="utf-8")
    assert pidlock.unlock(path, deps()).status == "live" and path.exists()


def test_this_process_writes_its_own_start_as_an_integer(tmp_path):
    path = tmp_path / "x.lock"
    token = pidlock.take(path)
    try:
        held = json.loads(path.read_text("utf-8"))
        assert held["pid"] == os.getpid() and (held["started"] is None or pidlock.is_ms(held["started"]))
        if sys.platform == "win32":
            assert pidlock.is_ms(held["started"])
        assert pidlock.state(path) == ("live", os.getpid())
    finally:
        pidlock.release(path, token)

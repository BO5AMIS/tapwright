"""CodeRabbit's second review of PR #20 (1.8.0, "the Explorer"), fixed by W174: explore's --maps/--accounts sent resolved, a
finished scroll scan that a stale disk copy can't reopen, report retention that counts only finished reports, an app-less read
with an alert, row families in an unnamed list's identity, a row's value in the rejected-scroll check, a checking file of its
own for each apply run, and the floor judged against the app the live read names. Each test fails on b4174e6."""
import argparse
import datetime
import json
import os
import time

import pytest
from explorefakes import identity
from fakes import button, el
from test_daemon import finish, make, run, started
from test_daemon_repair import Flow, phone, replay, script, tap, title

from tapwright import client, cli, explore, explored, explorefloor, explorereport, exploretool, places, policy as policylib
from tapwright import repairanalysis as ra, scriptrepair, ui
from tapwright import daemon as dm
from tapwright.control import Refused

APPID = "com.example.app"


# ---- cli.py / client.py: --maps and --accounts as serve resolves them ----

def test_explore_sends_maps_and_accounts_with_symlinks_resolved(tmp_path, monkeypatch):
    real_maps, real_file = tmp_path / "real-maps", tmp_path / "real" / "accounts.json"
    real_maps.mkdir()
    real_file.parent.mkdir()
    real_file.write_text("{}", "utf-8")
    try:
        os.symlink(real_maps, tmp_path / "maps-link", target_is_directory=True)
        os.symlink(real_file.parent, tmp_path / "acc-link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks here")
    seen: dict = {}

    def call(tool, args, client_id, timeout=120.0):
        seen.update(client.SCOPE)
        return {"text": "Explored 1 places in 0.1 min (1 steps), 0 findings. Stopped: done"}
    monkeypatch.setattr(client, "call", call)
    a = argparse.Namespace(minutes=None, steps=None, depth=None, start=None, focus=None, fill=None, keep_going=False,
                           apps=APPID, dedicated=False, phone=False, policy=None,
                           maps=str(tmp_path / "maps-link"), accounts=str(tmp_path / "acc-link" / "accounts.json"))
    try:
        cli.explore(a)
    finally:
        client.SCOPE.clear()
    # what `serve --maps/--accounts` holds (Path.resolve()), so the ceiling's string compare agrees
    assert seen["X-Tapwright-Maps"] == str(real_maps.resolve())
    assert seen["X-Tapwright-Accounts"] == str(real_file.resolve())


# ---- explored.py: a finished scan isn't reopened by the disk's copy from before it ended ----

def test_a_scan_that_ended_stays_ended_through_a_merge_with_the_disk_copy_from_during_it():
    def place():
        return {"places": {"home": {"key": {}}}}
    mem = place()
    explored.mark_seen(mem, "home", ["id:a"], "2026-11-19T10:00:00Z")
    assert explored.scan_open(mem, "home", "2026-11-19T10:00:01Z")
    disk_during = json.loads(json.dumps(mem["places"]["home"]["explored"]))  # the flush that ran mid-scan
    explored.mark_tried(mem, "home", "id:a", "same", "1", "2026-11-19T10:00:02Z")
    assert explored.scan_done(mem, "home", [])
    merged = explored.merge_record(disk_during, mem["places"]["home"]["explored"])
    data = {"places": {"home": {"key": {}, "explored": merged}}}
    assert explored.is_explored(data, "home", "1"), merged
    again = explored.merge_record(merged, disk_during)  # and the next flush, the other way round
    assert explored.is_explored({"places": {"home": {"key": {}, "explored": again}}}, "home", "1"), again
    # a scan still open on one side keeps its marker; an overflow cut (no scan) keeps its marker too
    open_mem = place()
    explored.mark_seen(open_mem, "home", ["id:b"], "2026-11-19T11:00:00Z")
    explored.scan_open(open_mem, "home", "2026-11-19T11:00:01Z")
    assert explored.MORE in explored.merge_record(merged, open_mem["places"]["home"]["explored"])["frontier"]
    cut = {"last": "2026-11-19T12:00:00Z", "actions": [], "frontier": ["id:c", explored.MORE]}
    assert explored.MORE in explored.merge_record(cut, merged)["frontier"]


def test_a_scan_that_begins_in_the_second_another_ended_keeps_its_marker():
    """CodeRabbit, PR #20 cycle 2 follow-up: times have one-second precision; a scan that ended and a new one that began in
    the same second must stay apart, or the merge drops the new scan's `…` and the place reads explored mid-scan."""
    t = "2026-11-19T10:00:05Z"
    done = {"places": {"home": {"key": {}}}}
    explored.mark_seen(done, "home", ["id:a"], "2026-11-19T10:00:00Z")
    explored.scan_open(done, "home", t)
    explored.mark_tried(done, "home", "id:a", "same", "1", t)
    assert explored.scan_done(done, "home", [])
    fresh = {"places": {"home": {"key": {}}}}
    explored.mark_seen(fresh, "home", ["id:b"], "2026-11-19T10:00:04Z")
    assert explored.scan_open(fresh, "home", t)  # the next scan, in that same second
    merged = explored.merge_record(done["places"]["home"]["explored"], fresh["places"]["home"]["explored"])
    assert explored.MORE in merged["frontier"], merged


# ---- explorereport.py: retention counts finished reports only ----

def _finished(folder):
    folder.mkdir(parents=True)
    (folder / "report.md").write_text("# report\n", "utf-8")


def _result():
    return explore.Result(stopped="frontier_empty", start_place="place_1", left_at="place_1", places=["place_1"], expanded=[],
                          moves=[], steps=3, seconds=10.0, findings=(), skipped=[], notes=[], resets=[], deferred=[],
                          frontier_left=0, prompts=0)


def test_an_unfinished_report_folder_never_displaces_a_finished_one(tmp_path):
    app = tmp_path / APPID
    for n in range(20):
        _finished(app / f"202611{10 + n:02d}T091500Z")
    (app / "20261130T091500Z").mkdir()  # a write that failed after the folder was made
    stale = app / "20261101T091500Z"
    stale.mkdir()
    old = time.time() - 2 * 86400  # two days: no run is still writing it
    os.utime(stale, (old, old))
    when = datetime.datetime(2026, 12, 1, 9, 0, tzinfo=datetime.timezone.utc)
    explorereport.write(tmp_path, _result(), identity(), explore.Budget(), when, when, lambda s: s)
    done = sorted(p.name for p in app.iterdir() if (p / "report.md").is_file())
    assert len(done) == 20 and "20261201T090000Z" in done and "20261111T091500Z" in done and "20261110T091500Z" not in done
    assert (app / "20261130T091500Z").exists()  # a recent unfinished one may still be another run's write
    assert not stale.exists()  # an old unfinished one is cleaned up on its own path


# ---- exploretool.py: an app-less read with an alert ----

def test_an_app_less_read_with_an_alert_drops_the_alert():
    d = make()
    tree = ui.Tree([button("e1", "Secret", "s1")], app=None, alert=ui.Alert("Open?", ("OK",), (0, 0, 1, 1)))
    own = exploretool.DaemonPhone(d, "a", None, APPID)._own(tree)
    assert own.items == [] and own.alert is None


# ---- places.py: an unnamed list's identity by compatible row families ----

def test_an_unnamed_list_is_one_place_when_rows_names_have_more_words():
    base = {"ids": ["explore-search"], "lists": [""], "kinds": [], "tab": None, "layer": None}
    a = {**base, "inner": ["dest-card", "dest-name"]}  # dest-card-kyoto, dest-name-kyoto
    b = {**base, "inner": ["dest-card-cape", "dest-name-cape"]}  # dest-card-cape-town, dest-name-cape-town
    assert places.similarity(a, b) == 1.0
    shared = {**base, "ids": ["tab-a", "tab-b"]}
    assert not places.same_place({**shared, "inner": ["settings-row"]}, {**shared, "inner": ["profile-row"]})


# ---- scriptrepairrun.py: a row's value counts in the rejected-scroll check ----

def test_a_rejected_scroll_stops_when_rows_differ_only_by_value(tmp_path, monkeypatch):
    lst = el("e1", "scroll", "", "trip-list", rect=(0, 100, 400, 500))
    top = [title("Trips"), lst, button("r1", "", "trip-row-1", rect=(10, 150, 300, 40), value="Trip 1")]
    more = [title("Trips"), lst, button("r2", "", "trip-row-1", rect=(10, 150, 300, 40), value="Trip 2")]
    scroll = scriptrepair.Candidate("scroll", "scroll down", "before it: scroll down", "off screen")
    row = scriptrepair.Candidate("target", "tap id=trip-row-1", "button", "close", 0.9, target={"id": "trip-row-1"})
    monkeypatch.setattr(ra, "analyse", lambda *a, **k: scriptrepair.Analysis("ok", [scroll, row]))

    async def go():
        d = phone(tmp_path, Flow(top, {"scroll": more, "trip-row-1": [title("Home")]}))
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await replay(d, script(tmp_path, [tap({"id": "trip-row-7"}, expect={"text": "Home"})]), "apply")
        assert "the list couldn't be shown as it was before it" in str(e.value)
        assert d.session.calls.count("tap") == 0
        await finish(stop, task)
    run(go)


# ---- scriptrepair.py: each apply run checks a file of its own ----

def test_two_checks_of_one_script_in_one_process_never_share_a_file(tmp_path):
    path = tmp_path / "sign_in.json"
    path.write_text("{}", "utf-8")
    first = scriptrepair.write_check(path, {"steps": ["a"]})
    second = scriptrepair.write_check(path, {"steps": ["b"]})
    try:
        assert first != second
        assert json.loads(first.read_text("utf-8")) == {"steps": ["a"]}  # run A's bytes, untouched by run B
        assert first.name.endswith(".checking") and second.name.endswith(".checking")
    finally:
        first.unlink(missing_ok=True)
        second.unlink(missing_ok=True)


# ---- daemon.py: the floor judges the app the live read names ----

def test_the_floor_refuses_a_touch_when_the_live_read_names_another_app():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        floor = explorefloor.Floor(policylib.NONE, APPID)
        tokens = (dm._EXPLORING.set(True), dm._TOUCH_FLOOR.set(floor))
        try:
            d.session.app = ("Example", APPID)  # the last snapshot's
            d.session.read_app = ("Other", "com.other.app")  # what the newest read named
            with pytest.raises(Refused) as e:
                d._floor_gate("tap", {}, d.session.items[0])
            assert "another app" in str(e.value)
            with pytest.raises(Refused):
                d._floor_gate("back", {}, None)
            d.session.read_app = ("Example", APPID)
            d._floor_gate("tap", {}, d.session.items[0])  # the app itself: nothing held
        finally:
            dm._TOUCH_FLOOR.reset(tokens[1])
            dm._EXPLORING.reset(tokens[0])
        await finish(stop, task)
    run(go)

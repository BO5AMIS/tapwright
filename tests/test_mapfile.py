"""The map file: one folder per app, read-merge-write under a lock (AGENT-TOOLS §18.9). No phone."""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from tapwright import mapfile, places, roads, store, ui

APP = "com.example.wayfare"
T1, T2, T3 = "2026-11-12T10:00:00Z", "2026-11-12T11:00:00Z", "2026-11-12T12:00:00Z"
REPO = Path(__file__).resolve().parent.parent


def road(src="a", dst="b", target="go", last=T1, by="tapwright", **kw):
    return {"from": src, "to": dst, "move": {"tool": "tap", "target": {"id": target}}, "names": {"id": target}, "by": by,
            "seen": 1, "failed": 0, "ms": 100, "last": last, "build": "1.0 (1)", **kw}


def place(sig="s1", last=T1, **kw):
    return {"key": {"signature": sig, "parts": {"ids": [sig], "kinds": [], "tab": None, "layer": None}}, "link": None,
            "titles": {}, "last": last, **kw}


def test_the_folder_holds_the_map_and_its_routes(tmp_path):
    f = mapfile.MapFile(tmp_path, APP)
    assert f.path == tmp_path / APP / "map.json" and f.routes_dir == tmp_path / APP / "routes"


def test_the_default_root_is_the_tool_home(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "HOME", tmp_path)
    assert mapfile.root(None) == tmp_path / "maps"
    assert mapfile.root(str(tmp_path / "project")) == tmp_path / "project"


def test_an_app_id_that_is_not_a_bundle_id_is_refused(tmp_path):
    for bad in ("", "../x", "a/b", "a\\b", ".", "..", "com.example app", "x" * 300):
        with pytest.raises(ValueError):
            mapfile.MapFile(tmp_path, bad)


def test_no_file_is_an_empty_map_and_reading_writes_nothing(tmp_path):
    f = mapfile.MapFile(tmp_path, APP)
    data = f.load()
    assert data["app"] == APP and data["places"] == {} and f.problem is None
    assert not (tmp_path / APP).exists()


def test_update_writes_a_valid_map_with_the_format_of_the_spec(tmp_path):
    f = mapfile.MapFile(tmp_path, APP)
    f.update(lambda data: data["places"].update({"home": place()}))
    on_disk = json.loads(f.path.read_text("utf-8"))
    assert on_disk["format"] == "agent-tools-map" and on_disk["version"] == 1 and on_disk["app"] == APP
    assert set(on_disk) >= {"titleKey", "places", "roads", "routes", "reflexes"}
    assert list(on_disk["places"]) == ["home"]
    assert not f.lock_path.exists() and not list((tmp_path / APP).glob("*.tmp"))
    again = mapfile.MapFile(tmp_path, APP).load()
    assert again["titleKey"] == on_disk["titleKey"]  # the one key of this map, kept


def test_a_file_that_does_not_parse_is_left_alone_and_said(tmp_path):
    f = mapfile.MapFile(tmp_path, APP)
    f.path.parent.mkdir(parents=True)
    f.path.write_text("{not json", "utf-8")
    seen = f.update(lambda data: data["places"].update({"home": place()}))
    assert f.path.read_text("utf-8") == "{not json"  # never overwritten
    assert "home" in seen["places"]  # the tool keeps driving, from memory
    assert f.problem and str(f.path) in f.problem and "doesn't parse" in f.problem
    assert f.load()["places"] == {}


@pytest.mark.parametrize("text", ['[]', '{"format": "other"}', '{"format": "agent-tools-map", "version": 2, "places": {}}',
                                  '{"format": "agent-tools-map", "version": 1, "places": []}',
                                  '{"format": "agent-tools-map", "version": 1, "places": {"a": 3}}',
                                  '{"format": "agent-tools-map", "version": 1, "places": {}, "roads": [{"from": 1}]}'])
def test_a_file_that_is_not_a_map_is_left_alone_too(tmp_path, text):
    f = mapfile.MapFile(tmp_path, APP)
    f.path.parent.mkdir(parents=True)
    f.path.write_text(text, "utf-8")
    f.update(lambda data: data["roads"].append(road()))
    assert f.path.read_text("utf-8") == text and f.problem


def test_the_merge_keeps_each_places_newest_record_and_every_road():
    disk = {**places.empty_map(APP), "places": {"home": place("h", T1)}, "roads": [road("a", "b", "x", T1)]}
    memory = {**disk, "places": {"home": place("h", T2, link="wayfare://"), "other": place("o", T2)},
              "roads": [road("a", "c", "y", T2)]}
    merged = mapfile.merge(disk, memory)
    assert sorted(merged["places"]) == ["home", "other"] and merged["places"]["home"]["last"] == T2
    assert merged["places"]["home"]["link"] == "wayfare://"
    assert sorted((r["from"], r["to"]) for r in merged["roads"]) == [("a", "b"), ("a", "c")]


def test_the_newest_last_wins_for_a_road_and_for_a_place():
    disk = {**places.empty_map(APP), "places": {"home": place("h", T3, link="wayfare://new")}, "roads": [road(last=T3, ms=50)]}
    memory = {**disk, "places": {"home": place("h", T1, link="wayfare://old")}, "roads": [road(last=T1, ms=999)]}
    merged = mapfile.merge(disk, memory)
    assert merged["roads"][0]["ms"] == 50 and merged["places"]["home"]["link"] == "wayfare://new"
    flipped = mapfile.merge(memory, disk)
    assert flipped["roads"][0]["ms"] == 50  # whichever side it came from


def test_a_place_with_the_same_key_under_two_names_is_one_place_and_its_roads_follow():
    disk = {**places.empty_map(APP), "places": {"place_1": place("h", T1)}, "roads": [road("place_1", "z", "x")]}
    memory = {**disk, "places": {"home": place("h", T2, named=True)}, "roads": [road("home", "q", "y", T2)]}
    merged = mapfile.merge(disk, memory)
    assert list(merged["places"]) == ["home"]  # a person's name beats a number
    assert sorted((r["from"], r["to"]) for r in merged["roads"]) == [("home", "q"), ("home", "z")]


def test_two_places_with_one_name_and_different_keys_both_stay():
    disk = {**places.empty_map(APP), "places": {"details": place("d1", T1)}}
    memory = {**disk, "places": {"details": place("d2", T2)}, "roads": [road("details", "x", "go", T2)]}
    merged = mapfile.merge(disk, memory)
    assert sorted(merged["places"]) == ["details", "details_2"]
    assert merged["roads"][0]["from"] == "details_2"


def test_the_disks_title_key_stands_and_titles_under_another_key_are_not_mixed_in():
    disk = {**places.empty_map(APP), "places": {"home": place("h", T1)}}
    memory = {**places.empty_map(APP), "places": {"home": place("h", T2, titles={"latin": "0123456789abcdef"})}}
    merged = mapfile.merge(disk, memory)
    assert merged["titleKey"] == disk["titleKey"] and merged["places"]["home"]["titles"] == {}


def test_a_road_driven_by_both_tools_says_so_and_routes_reflexes_and_unknown_fields_survive():
    disk = {**places.empty_map(APP), "roads": [road(by="tapline", last=T1)], "routes": {"sign_in": "routes/sign_in.json"},
            "reflexes": [{"name": "r1", "when": {"text": "x"}, "do": {"tool": "tap"}}], "note": "from a person"}
    memory = {**places.empty_map(APP), "roads": [road(by="tapwright", last=T2)],
              "reflexes": [{"name": "r2", "when": {"text": "y"}, "do": {"tool": "tap"}}]}
    merged = mapfile.merge(disk, memory)
    assert merged["roads"][0]["by"] == "both" and merged["roads"][0]["last"] == T2
    assert merged["routes"] == {"sign_in": "routes/sign_in.json"} and merged["note"] == "from a person"
    assert sorted(r["name"] for r in merged["reflexes"]) == ["r1", "r2"]


def test_save_merges_memory_with_the_file_so_neither_tool_drops_the_others_roads(tmp_path):
    ours, theirs = mapfile.MapFile(tmp_path, APP), mapfile.MapFile(tmp_path, APP)
    mine = ours.load()
    mine["places"]["a"], mine["roads"] = place("a"), [road("a", "b", "mine")]
    theirs.update(lambda data: (data["places"].update({"b": place("b", T2)}), data["roads"].append(road("b", "a", "theirs", T2))))
    after = ours.save(mine)  # `mine` never saw their work
    assert sorted(after["places"]) == ["a", "b"] and len(after["roads"]) == 2
    assert len(json.loads(ours.path.read_text("utf-8"))["roads"]) == 2


def test_a_lock_that_is_old_is_broken_and_a_fresh_one_is_waited_for(tmp_path):
    f = mapfile.MapFile(tmp_path, APP)
    f.path.parent.mkdir(parents=True)
    f.lock_path.write_text("x")
    old = time.time() - mapfile.LOCK_STALE - 5
    os.utime(f.lock_path, (old, old))
    f.update(lambda data: None)  # the stale lock was no obstacle
    assert not f.lock_path.exists()
    f.lock_path.write_text("x")  # fresh: someone is writing
    threading.Timer(0.3, lambda: f.lock_path.unlink()).start()
    began = time.monotonic()
    f.update(lambda data: data["places"].update({"home": place()}))
    assert 0.25 <= time.monotonic() - began < mapfile.LOCK_STALE
    assert "home" in json.loads(f.path.read_text("utf-8"))["places"]


def test_the_rename_is_retried_on_windows_style_refusals(tmp_path, monkeypatch):
    f = mapfile.MapFile(tmp_path, APP)
    real, calls = os.replace, []

    def flaky(src, dst):
        calls.append(1)
        if len(calls) <= 2:
            raise PermissionError(13, "in use")
        real(src, dst)
    monkeypatch.setattr(os, "replace", flaky)
    monkeypatch.setattr(mapfile, "RENAME_WAIT", 0.001)
    f.update(lambda data: data["places"].update({"home": place()}))
    assert len(calls) == 3 and "home" in json.loads(f.path.read_text("utf-8"))["places"]


def test_a_rename_that_never_works_is_an_error_not_a_lost_file(tmp_path, monkeypatch):
    f = mapfile.MapFile(tmp_path, APP)
    f.update(lambda data: data["places"].update({"home": place()}))
    before = f.path.read_text("utf-8")

    def never(src, dst):
        raise PermissionError(13, "in use")
    monkeypatch.setattr(os, "replace", never)
    monkeypatch.setattr(mapfile, "RENAME_WAIT", 0.001)
    with pytest.raises(mapfile.MapError):
        f.update(lambda data: data["places"].update({"other": place("o")}))
    assert f.path.read_text("utf-8") == before and not f.lock_path.exists()
    assert not list(f.path.parent.glob("*.tmp"))


def test_threads_writing_at_once_lose_nothing(tmp_path):
    def worker(n):
        f = mapfile.MapFile(tmp_path, APP)
        for i in range(12):
            f.update(lambda data: data["roads"].append(road(f"p{n}", f"q{i}", f"t{n}-{i}")))
    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    got = json.loads(mapfile.MapFile(tmp_path, APP).path.read_text("utf-8"))["roads"]
    assert len(got) == 72 and len({(r["from"], r["to"]) for r in got}) == 72


def test_two_processes_writing_at_once_lose_nothing(tmp_path):
    code = (
        "import sys\n"
        "from tapwright import mapfile\n"
        "root, n = sys.argv[1], sys.argv[2]\n"
        "f = mapfile.MapFile(root, 'com.example.wayfare')\n"
        "mem = f.load()\n"
        "for i in range(15):\n"
        "    mem['roads'].append({'from': 'p'+n, 'to': 'q%d' % i, 'move': {'tool': 'tap', 'target': {'id': 't'+n+str(i)}},\n"
        "                         'names': {}, 'by': 'tapwright', 'seen': 1, 'failed': 0, 'ms': 1,\n"
        "                         'last': '2026-11-12T10:00:00Z', 'build': None})\n"
        "    mem = f.save(mem)\n")
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    procs = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path), str(n)], env=env, stderr=subprocess.PIPE)
             for n in range(3)]
    for p in procs:
        _, err = p.communicate(timeout=120)
        assert p.returncode == 0, err.decode()
    got = json.loads(mapfile.MapFile(tmp_path, APP).path.read_text("utf-8"))["roads"]
    assert len(got) == 45 and len({(r["from"], r["to"]) for r in got}) == 45


def test_saved_routes_are_ways_between_places(tmp_path):
    f = mapfile.MapFile(tmp_path, APP)
    f.update(lambda data: data["routes"].update({"sign_in": "routes/sign_in.json", "broken": "routes/broken.json",
                                                 "gone": "routes/gone.json"}))
    f.routes_dir.mkdir(parents=True, exist_ok=True)
    (f.routes_dir / "sign_in.json").write_text(json.dumps({"format": "agent-tools-script", "version": 2, "name": "sign_in",
                                                           "start": "welcome", "end": "home", "variables": ["account"],
                                                           "steps": [{"tool": "tap"}, {"tool": "tap"}, {"tool": "wait_for"}]}))
    (f.routes_dir / "broken.json").write_text("{nope")
    data = f.load()
    assert f.route_edges(data) == [roads.RouteEdge("sign_in", "welcome", "home", 3)]
    assert f.route_info(data) == [("sign_in", "welcome", "home", ["account"])]


def test_no_private_text_reaches_the_file(tmp_path):
    """Rows, a title that changes, typed text and a secure field never get into the file, whatever the screens say."""
    private = ("Chat with Sara", "Order #10432", "sara@example.com", "hunter2", "4111 1111 1111 1111")

    def tree(title, rows):
        cells = [ET.Element("XCUIElementTypeCell", {"type": "XCUIElementTypeCell", "label": r, "value": "3 new", "name": r,
                                                    "enabled": "true", "x": "0", "y": str(200 + 50 * i), "width": "400",
                                                    "height": "50"}) for i, r in enumerate(rows)]
        field = ET.Element("XCUIElementTypeSecureTextField", {"type": "XCUIElementTypeSecureTextField", "label": "Password",
                                                              "name": "password", "value": "hunter2", "enabled": "true",
                                                              "x": "0", "y": "700", "width": "400", "height": "40"})
        bar = ET.Element("XCUIElementTypeNavigationBar", {"type": "XCUIElementTypeNavigationBar", "name": title, "enabled": "true",
                                                         "x": "0", "y": "50", "width": "430", "height": "90"})
        table = ET.Element("XCUIElementTypeTable", {"type": "XCUIElementTypeTable", "name": "inbox", "enabled": "true",
                                                    "x": "0", "y": "150", "width": "430", "height": "600"})
        table.extend(cells)
        app = ET.Element("XCUIElementTypeApplication", {"type": "XCUIElementTypeApplication", "bundleId": "com.x",
                                                        "label": "App", "enabled": "true", "x": "0", "y": "0",
                                                        "width": "430", "height": "932"})
        app.extend([bar, table, field])
        return ui.read(ET.tostring(app, encoding="unicode"), (430.0, 932.0))

    f = mapfile.MapFile(tmp_path, APP)
    for title, rows in (("Chat with Sara", private), ("Chat with Omar", private[::-1]), ("Order #10432", private[:2])):
        def learn(data, title=title, rows=rows):
            t = tree(title, rows)
            v = places.visit(data, places.read_place(t), T1)
            row = t.items[[e.role for e in t.items].index("cell")]
            move = roads.move_of("tap", ui.Target(ref=row.ref), row, t.items, {})
            roads.record(data, v.name, "elsewhere", move, roads.names_of(row), 10, T1, None)
        f.update(learn)
    text = f.path.read_text("utf-8")
    for secret in (*private, "Sara", "Omar", "10432", "3 new"):
        assert secret not in text
    assert any(r["move"]["target"] == {"row": True} for r in json.loads(text)["roads"])

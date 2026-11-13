"""Fixes from the 1.4.0 review (REVIEW-1.4-TAPWRIGHT.md): network limits and policy, holds, reach, log, report."""
import asyncio
import datetime
import json

import pytest
from fakes import button, el
from test_daemon import finish, make, run, started

from tapwright import daemon as daemon_module, device as device_module, policy, report
from tapwright.actionlog import ActionLog
from tapwright.control import Refused
from tapwright.daemon import Scope
from tapwright.device import ConnectionLost, Device


# ---- M1: the hard cap, and a policy that blocks `network` ----
def test_the_cap_is_30_minutes_and_clears_whatever_the_agent_does(monkeypatch):
    assert daemon_module.NETWORK_MAX == 1800.0

    async def go():
        monkeypatch.setattr(daemon_module, "NETWORK_MAX", 0.25)
        monkeypatch.setattr(daemon_module, "NETWORK_CHECK", 0.05)
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "network", {"mode": "3g"})
        text = ""
        for _ in range(12):  # the setter keeps calling, so the idle clear never comes
            await asyncio.sleep(0.05)
            text = (await d.run_tool("a", "look", {"screenshot": False}))["text"]
            if d._network is None:
                break
        assert d._network is None and d.session.device.network_calls[-1] == ("clear",)
        assert "\nNote: the network condition ended after " in text and "set it again if the test needs it." in text
        assert "Network:" not in text
        again = (await d.run_tool("a", "look", {"screenshot": False}))["text"]
        assert "the network condition ended" not in again  # told once
        await finish(stop, task)
    run(go)


def test_a_global_block_word_network_refuses_every_mode_but_normal():
    async def go():
        d = make()
        stop, task = await started(d)
        p = policy.parse({"block": ["network"]})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "network", {"mode": "3g"}, policy=p)
        assert e.value.code == "not_allowed" and str(e.value) == "blocked: the safety policy blocks network here."
        assert d.session.device.network_calls == []
        await d.run_tool("a", "network", {"mode": "3g"})
        r = await d.run_tool("a", "network", {"mode": "normal"}, policy=p)  # always allowed
        assert r["text"].startswith("Network: normal")
        await finish(stop, task)
    run(go)


def test_a_block_word_in_the_front_apps_own_list_names_the_app():
    async def go():
        d = make()
        stop, task = await started(d)
        p = policy.parse({"apps": {"com.example.app": {"block": ["network"]}}})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "network", {"mode": "lossy"}, policy=p)
        assert str(e.value) == "blocked: the safety policy blocks network for Example."
        other = policy.parse({"apps": {"com.other": {"block": ["network"]}}})
        d.session.foreground = "com.example.app"  # the phone names its front app (an unnamed one is refused)
        assert (await d.run_tool("a", "network", {"mode": "3g"}, policy=other))["text"].startswith("Network: 3g")
        await finish(stop, task)
    run(go)


# ---- M2: set and clear never leak a hold ----
def test_concurrent_set_and_clear_leave_no_hold_running(monkeypatch):
    live: set = set()

    class FakeHold:
        def __init__(self, dev, mode):
            self.mode = mode

        async def start(self):
            await asyncio.sleep(0.05)
            live.add(self)
            return "p"

        async def stop(self):
            live.discard(self)

        def abandon(self):
            live.discard(self)

    monkeypatch.setattr(device_module, "NetworkHold", FakeHold)

    async def go():
        dev = Device(log=lambda *a: None)
        await asyncio.gather(dev.set_network("3g"), dev.set_network("offline"), dev.clear_network())
        await dev.clear_network()
        assert live == set() and dev._network_holds == set() and dev._network_hold is None
        await dev.set_network("3g")
        assert len(live) == 1
        dev.drop_network()
        assert live == set()
    run(go)


def test_a_hold_that_dies_tells_the_device_owner_but_a_clear_does_not():
    async def go():
        dev = Device(log=lambda *a: None)
        told: list[str] = []
        dev.on_network_end = told.append

        class H:
            async def stop(self):
                pass

            def abandon(self):
                pass
        h = H()
        dev._network_hold = h
        dev._network_holds.add(h)
        dev._hold_died(h, "the phone's condition connection closed (boom)")
        assert told == ["the phone's condition connection closed (boom)"] and dev._network_hold is None
        dev._network_hold = h
        dev._network_holds.add(h)
        await dev.clear_network()
        assert len(told) == 1
        dev._network_hold = h
        dev._network_holds.add(h)
        dev.drop_network("the phone's connection was rebuilt")
        assert told[-1] == "the phone's connection was rebuilt"
    run(go)


# ---- L6: the hold reports its death; a WebDriverAgent hiccup keeps the condition ----
def test_a_wda_hiccup_keeps_the_condition_and_a_dead_hold_ends_it_with_a_note():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "network", {"mode": "3g"})
        d.lost(ConnectionLost("WebDriverAgent hiccup"))
        assert d._network == "3g" and ("drop",) not in d.session.device.network_calls
        d.session.device.on_network_end("the phone's condition connection closed (boom)")
        assert d._network is None
        text = (await d.run_tool("a", "look", {"screenshot": False}))["text"]
        assert "Network:" not in text
        assert text.endswith("Note: the network condition ended: the phone's condition connection closed (boom).")
        assert "the network condition ended" not in (await d.run_tool("a", "look", {"screenshot": False}))["text"]
        other = await d.run_tool("b", "look", {"screenshot": False})
        assert "the network condition ended" not in other["text"]
        await finish(stop, task)
    run(go)


# ---- L11 ----
def test_a_lost_connection_while_setting_the_network_is_reconnecting():
    async def go():
        d = make()
        d.session.device.network_error = ConnectionLost("tunnel gone")
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "network", {"mode": "3g"})
        assert e.value.code == "reconnecting"
        assert d._network is None
        await finish(stop, task)
    run(go)


# ---- L7 ----
def test_run_clears_a_condition_it_set_when_it_stops_but_not_when_it_finishes():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.tree = [el("e1", "button", "Save", "save")]
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": [{"tool": "network", "mode": "3g"},
                                                    {"tool": "tap", "target": {"id": "missing"}}]})
        assert "stopped at step 2" in str(e.value)
        assert "Note: the network condition this script set was cleared" in str(e.value)
        assert d._network is None and d.session.device.network_calls == [("set", "3g"), ("clear",)]
        r = await d.run_tool("a", "run", {"steps": [{"tool": "network", "mode": "3g"},
                                                    {"tool": "tap", "target": {"id": "save"}}]})
        assert r["text"].startswith("Ran 2 of 2 steps") and d._network == "3g"  # the script decides
        await finish(stop, task)
    run(go)


# ---- M3 ----
def test_reach_polices_and_names_the_element_scroll_to_will_reach_when_index_is_set():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.screen = (400.0, 800.0)
        d.session.tree = [el("e1", "scroll", "", "list", rect=(0, 0, 400, 800)),
                          button("e2", "Account", "acc-on", rect=(16, 100, 300, 40))]
        d.session.offscreen = [button("", "Account", "acc-a", rect=(16, 1000, 300, 40)),
                               button("", "Account", "acc-b", rect=(16, 1200, 300, 40))]
        await d.run_tool("a", "look", {"screenshot": False})
        r = await d.run_tool("a", "tap", {"target": {"text": "Account", "index": 2}, "dry_run": True})
        assert "id=acc-b" in r["text"] and "acc-a" not in r["text"], r["text"]
        await finish(stop, task)
    run(go)


# ---- M4 ----
def test_a_read_in_the_log_carries_nothing_of_the_action_before_it(tmp_path):
    async def go():
        d = make(actions=ActionLog(tmp_path))
        stop, task = await started(d)
        d._announced, d._granted_now = {"label": "Save"}, True
        d._log_action("look", {}, 0.0, None)
        d._log_action("tap", {}, 0.0, None)
        await finish(stop, task)
        got = [json.loads(x) for f in tmp_path.glob("actions-*.jsonl") for x in f.read_text("utf-8").splitlines()]
        assert "element" not in got[0] and "confirmed" not in got[0]
        assert got[1]["element"] == "Save" and got[1]["confirmed"] is True
    run(go)


# ---- L1 ----
def test_closest_and_candidates_stay_quiet_when_another_app_is_in_front():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.tree = [button("e1", "Save draft", "save-draft")]
        view = d.control.client("a")
        view.items = d.session.tree
        from tapwright import ui
        target = ui.Target(label="Save draft ")
        assert d._closest(view, ui.Target(id="save-draf"), "nothing on screen has id") != ""
        token = daemon_module._GIVEN.set({"com.other": "Other"})  # personal mode, and the app in front isn't given
        try:
            assert d._closest(view, ui.Target(id="save-draf"), "nothing on screen has id") == ""
            assert d._other_app_shown() is True
        finally:
            daemon_module._GIVEN.reset(token)
        assert target and d._other_app_shown() is False
        await finish(stop, task)
    run(go)


# ---- L2 ----
def test_session_report_with_no_app_list_is_refused_in_personal_mode(tmp_path):
    async def go():
        d = make(actions=ActionLog(tmp_path))
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "session_report", {}, scope=Scope(apps=[]))
        assert e.value.code == "not_allowed" and "no_apps" in str(e.value)
        await finish(stop, task)
    run(go)


# ---- L3 ----
def test_read_entries_reads_only_the_day_files_the_window_can_reach(tmp_path):
    def day(name, tool):
        (tmp_path / f"actions-{name}.jsonl").write_text(json.dumps({"t": f"{name}T10:00:00.000Z", "tool": tool}) + "\n", "utf-8")
    day("2026-11-01", "old")
    day("2026-11-11", "recent")
    day("2026-11-12", "today")
    since = round(datetime.datetime(2026, 11, 12, 9, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000)
    assert [e["tool"] for e in report.read_entries(tmp_path, since)] == ["recent", "today"]
    assert [e["tool"] for e in report.read_entries(tmp_path)] == ["old", "recent", "today"]


def test_the_log_prunes_once_a_day_while_it_runs(tmp_path):
    now = [datetime.datetime(2026, 11, 12, 10, 0, tzinfo=datetime.timezone.utc)]
    log = ActionLog(tmp_path, now=lambda: now[0])
    old = tmp_path / "actions-2026-11-20.jsonl"
    old.write_text("{}\n", "utf-8")
    now[0] = datetime.datetime(2026, 12, 20, 10, 0, tzinfo=datetime.timezone.utc)  # the daemon is still running
    log.write({"tool": "tap"})
    assert not old.exists()
    assert (tmp_path / "actions-2026-12-20.jsonl").exists()


def test_the_tool_reads_the_log_off_the_event_loop(tmp_path, monkeypatch):
    seen = []
    real = report.read_entries
    import threading
    main = threading.get_ident()

    def spy(folder, since=None):
        seen.append(threading.get_ident())
        return real(folder, since)
    monkeypatch.setattr(report, "read_entries", spy)

    async def go():
        d = make(actions=ActionLog(tmp_path))
        stop, task = await started(d)
        await d.run_tool("a", "session_report", {})
        assert seen and seen[0] != main
        await finish(stop, task)
    run(go)


# ---- L4 ----
def _entry(i, tool, ok=True, code=None, **more):
    at = datetime.datetime(2026, 11, 12, 10, 0, 0, tzinfo=datetime.timezone.utc) + datetime.timedelta(seconds=10 * i)
    return {"t": at.isoformat(timespec="milliseconds").replace("+00:00", "Z"), "by": "agent", "client": "c",
            "tool": tool, "ok": ok, **({"code": code} if code else {}), **more}


def test_a_look_that_asked_for_the_picture_is_not_a_wasted_look():
    es = [_entry(0, "tap", listed=True), _entry(1, "look", kind="read", screenshot=True)]
    assert "right after an action" not in report.build(es, "c", None)


def test_retry_flag_allows_one_look_between_needs_another_kind_and_says_likely():
    es = [_entry(0, "tap", ok=False, code="not_found", target={"text": "Save"}),
          _entry(1, "look", kind="read"),
          _entry(2, "tap", target={"id": "save"})]
    text = report.build(es, "c", None)
    assert "likely the same element (calls 1)" in text
    same_kind = [_entry(0, "tap", ok=False, code="not_found", target={"text": "Save"}),
                 _entry(1, "tap", target={"text": "Other"})]  # another element, same kind of target
    assert "retr" not in report.build(same_kind, "c", None)
    far = [_entry(0, "tap", ok=False, code="not_found", target={"text": "Save"}),
           _entry(1, "look", kind="read"), _entry(2, "look", kind="read"), _entry(3, "tap", target={"id": "save"})]
    assert "retr" not in report.build(far, "c", None)


# ---- L5 ----
def test_since_in_epoch_seconds_is_converted_and_nan_is_bad_params():
    now = 1_762_950_000_000
    assert report.parse_since("1762940000", now) == 1_762_940_000_000
    assert report.parse_since("1762940000000", now) == 1_762_940_000_000
    assert report.to_epoch_ms(1_762_940_000) == 1_762_940_000_000

    async def go():
        d = make(actions=ActionLog(None))
        stop, task = await started(d)
        for bad in (float("nan"), float("inf")):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "session_report", {"since": bad})
            assert e.value.code == "bad_params"
        await d.run_tool("a", "session_report", {"since": 1_762_940_000})
        await finish(stop, task)
    run(go)

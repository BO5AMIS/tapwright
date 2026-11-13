"""Codex, PR #10 cycle 7: a number in an exported crash report that is an account value is replaced (or the export refused),
a repaired road keeps no title the screens showed during the drive, and a recording saves the element a handler touched."""
import json

import pytest
from mapfakes import screen
from test_account_values_at_ingest import assert_absent
from test_account_values_raw_inside import USERNAME, daemon
from test_daemon import finish, make, run, started
from test_daemon_drive import BUNDLE, drive, file_of, learn, mapped
from test_diagnostics import FakeReport, opened, tuned
from test_pr10_cycle4 import with_accounts

from tapwright import accounts as accountlib
from tapwright import diagnostics
from tapwright.control import Refused

HEADER = '{"app_name":"Example","bug_type":"309"}\n'


def pull_with(tmp_path, report: bytes, username: str, password: str) -> str:
    out = {}

    async def go():
        d = with_accounts(tuned(make()), tmp_path / "acc", username=username, password=password)
        stop, task = await started(d)
        await opened(d)
        await d.run_tool("a", "look", {"screenshot": False})
        d.session.device.crashes["Example"] = [FakeReport(filename="/Example-new.ips")]
        d.session.device.crash_files["/Example-new.ips"] = report
        try:
            out["answer"] = (await d.run_tool("a", "crash", {"pull": True}))["text"]
        except Refused as e:
            out["answer"] = str(e)
        await finish(stop, task)
    (tmp_path / "acc").mkdir()
    run(go)
    return out["answer"]


# ---- 1: numbers ----
@pytest.mark.parametrize("body, password, username, number", [
    ('{"pin":836291}', "836291", "tester2@example.com", "836291"),
    ('{"userId":10432}', "pw-secret-0042", "10432", "10432"),
    ('{"deep":[{"n":[836291.0]}]}', "836291", "tester2@example.com", "836291"),
])
def test_a_number_that_is_an_account_value_is_replaced_in_the_pulled_report(tmp_path, monkeypatch, body, password, username, number):
    monkeypatch.setattr(diagnostics, "crash_folder", lambda bundle: tmp_path / "crashes" / bundle)
    answer = pull_with(tmp_path, f"{HEADER}{body}\n".encode(), username, password)
    path = next((tmp_path / "crashes").rglob("*.ips"))
    raw = path.read_text("utf-8")
    assert f"Saved: {path}" in answer
    assert number not in raw
    json.loads(raw.splitlines()[1])  # (still valid JSON)
    assert "<tester2's" in raw


def test_a_report_that_still_holds_a_value_after_sanitizing_is_not_saved():
    redact = accountlib.Loaded(path="x", accounts={"t": accountlib.Account("t", None, "836291")}).scrub
    leaky = lambda text: text.replace("836291", "[836291]")  # (a redaction that leaves the value in: the final check finds it)
    with pytest.raises(diagnostics.Unsanitizable):
        diagnostics.sanitized_report(b'{"pin":1}\n{"a":"836291"}\n', leaky)
    with pytest.raises(diagnostics.Unsanitizable):
        diagnostics.sanitized_report(b'plain 836291 text', leaky)
    assert diagnostics.sanitized_report(b'{"pin":1}', redact) == b'{"pin":1}'


def test_the_daemon_refuses_the_export_and_saves_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "crash_folder", lambda bundle: tmp_path / "crashes" / bundle)

    def refuse(data, redact):
        raise diagnostics.Unsanitizable(diagnostics.CRASH_UNSAFE)
    monkeypatch.setattr(diagnostics, "sanitized_report", refuse)
    answer = pull_with(tmp_path, f"{HEADER}{{}}\n".encode(), "tester2@example.com", "836291")
    assert "couldn't be saved safely" in answer and "836291" not in answer
    assert not list((tmp_path / "crashes").rglob("*.ips"))


# ---- 2: a live title never goes into a repaired road ----
def test_a_road_repaired_by_a_title_keeps_no_title_the_screens_showed(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await learn(d)
        d.session.screens["settings"] = screen("Settings", ids=("general-row2", "about-row", "s1", "s2", "s3"),
                                               labels={"general-row2": "Chat with Sara"})
        d.session.screens["general"] = screen("Chat with Sara", ids=("language-row", "x1", "x2"))
        d.session.goes[("settings", "general-row2")] = "general"
        r = await drive(d, to="language")
        assert r["text"].startswith("Arrived at language by 3 taps"), r["text"]
        await d.maps.flush()
        raw = (tmp_path / BUNDLE / "map.json").read_text("utf-8")
        assert "Chat with Sara" not in raw
        roads = {(x["from"], x["to"]): x for x in file_of(tmp_path)["roads"]}
        assert roads[("settings", "general")]["move"]["target"] == {"id": "general-row2"}
        await finish(stop, task)
    run(go)


def test_a_repair_with_nothing_safe_to_aim_by_is_not_persisted(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await learn(d)
        await d.maps.flush()
        before = file_of(tmp_path)["roads"]
        made = d.maps.repair(None, BUNDLE, "settings", {"tool": "tap", "target": {"id": "general-row"}}, "general",
                             {"tool": "tap", "target": {"label": "Chat with Sara"}}, {"label": "Chat with Sara"}, 5, None,
                             frozenset({"Chat with Sara"}))
        await d.maps.flush()
        assert made.gone == [] and file_of(tmp_path)["roads"] == before
        await finish(stop, task)
    run(go)


# ---- 3: the element the handler touched ----
def test_a_toggle_by_a_value_is_recorded_as_the_switch_it_toggled_and_replays(tmp_path):
    async def go():
        d = daemon(tmp_path)
        d.session.headings = [("account-heading", USERNAME)]
        d.session.switches = [("account-switch", USERNAME)]
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "record", {"start": True, "name": "tg"})
        await d.run_tool("a", "toggle", {"target": {"label": "<tester2's username>", "index": 0}, "look": False})
        script = tmp_path / "tg.json"
        said = (await d.run_tool("a", "record", {"stop": True, "path": str(script)}))["text"]
        assert said.startswith("Saved 1 steps"), said
        steps = json.loads(script.read_text("utf-8"))["steps"]
        assert steps == [{"tool": "toggle", "target": {"id": "account-switch"}}], steps
        assert_absent(USERNAME, script.read_text("utf-8") + said)
        before = d.session.calls.count("tap")
        await d.run_tool("a", "replay", {"path": str(script)})
        assert d.session.calls.count("tap") == before + 1
        await finish(stop, task)
    run(go)


def test_a_tap_by_a_value_is_still_recorded_as_the_element_tapped(tmp_path):
    async def go():
        d = daemon(tmp_path)
        d.session.buttons = [("profile-btn", USERNAME), ("l1", "Other")]
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "record", {"start": True, "name": "tp"})
        await d.run_tool("a", "tap", {"target": {"label": "<tester2's username>"}, "look": False})
        script = tmp_path / "tp.json"
        await d.run_tool("a", "record", {"stop": True, "path": str(script)})
        assert json.loads(script.read_text("utf-8"))["steps"] == [{"tool": "tap", "target": {"id": "profile-btn"}}]
        await finish(stop, task)
    run(go)

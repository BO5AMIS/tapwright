"""`explore` through the daemon: what the app's log and a crash become (§25.10), a refused touch, the dry run's say, and the reset."""
import json
import time

import pytest
from explorefakes import grid
from fakes import FakeReport
from mapfakes import APP
from test_daemon import finish, run, started
from test_daemon_explore import BUNDLE, explored, report_of

from tapwright import policy as policylib
from tapwright.control import Refused


def body_of(r: dict) -> dict:
    return json.loads((report_of(r) / "report.json").read_text("utf-8"))


def test_an_error_level_log_line_since_a_move_is_a_js_error_finding_with_its_script(tmp_path):
    async def go():
        d = explored(tmp_path)
        d.session.after_tap = lambda s: s.device.emit(1, int(time.time() * 1000), "TypeError: boom in LanguageScreen", 0x10) \
            if s.tapped[-1][1] == "go-language" else None
        stop, task = await started(d)
        r = await d.run_tool("a", "explore", {})
        body = body_of(r)
        found = [f for f in body["findings"] if f["kind"] == "js_error"]
        assert found and "TypeError: boom in LanguageScreen" in found[0]["got"] and found[0]["place"].startswith("place_")
        assert found[0]["path_from"] and found[0]["path"][-1] == {"tool": "tap", "target": {"id": "go-language"}}
        assert (report_of(r) / found[0]["repro"]).exists() and "1 js_error" in r["text"]
        await finish(stop, task)
    run(go)


def test_a_crash_is_a_finding_with_its_stack_and_stops_the_run(tmp_path):
    async def go():
        d = explored(tmp_path)

        def crash(s) -> None:
            if s.tapped[-1][1] == "go-language":
                s.screens["dead"] = grid("Home screen", buttons=(("icon", "Wayfare"),), bundle="com.apple.springboard")
                s.at = "dead"
                s.device.pids[BUNDLE] = []
                s.device.crashes["Example"] = [FakeReport()]
        d.session.after_tap = crash
        stop, task = await started(d)
        r = await d.run_tool("a", "explore", {})
        assert r["text"].splitlines()[0].endswith("Stopped: the app crashed.")
        body = body_of(r)
        crashed = [f for f in body["findings"] if f["kind"] == "crash"]
        assert crashed and crashed[0]["crash"]["frames"] and crashed[0]["picture"] is None  # the app took its screen with it
        assert body["stopped"] == "crash" and "1 crash" in r["text"]
        assert not [k for s, k in d.session.tapped if s == "dead"]  # nothing was touched in the home screen
        await finish(stop, task)
    run(go)


def test_a_policy_word_the_dry_run_holds_is_skipped_and_never_asked(tmp_path):
    async def go():
        d = explored(tmp_path)
        rules = policylib.parse({"confirm": ["Language"], "block": ["Arabic"]})
        stop, task = await started(d)
        r = await d.run_tool("a", "explore", {}, None, rules)
        assert "go-language" not in {k for _, k in d.session.tapped}
        assert not d._confirms and d.dialog_channel("a").after(0) == [] or all(e.get("type") == "progress" for e in d.dialog_channel("a").after(0))
        body = body_of(r)
        assert any(s["sig"] == "id:go-language" and s["why"] == "policy" and s["word"] == "Language" for s in body["skipped"])
        await finish(stop, task)
    run(go)


def test_a_touch_the_policy_holds_is_refused_as_needs_confirmation_with_no_dialog_and_no_grant(tmp_path):
    async def go():
        d = explored(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        from tapwright import daemon as dm
        token = dm._EXPLORING.set(True)
        try:
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "tap", {"target": {"label": "Delete everything"}, "look": False})
            assert e.value.code == "not_allowed" and "needs_confirmation" in str(e.value) and "explore" in str(e.value)
            dry = await d.run_tool("a", "tap", {"target": {"label": "Delete everything"}, "dry_run": True})  # what it would meet
            assert dry["text"].startswith("Would tap") and "needs a person's OK" in dry["text"]
        finally:
            dm._EXPLORING.reset(token)
        assert not d._confirms  # no request was made: nobody was asked
        await finish(stop, task)
    run(go)


def test_the_progress_line_is_pushed_every_ten_steps_and_never_in_the_answer(tmp_path):
    async def go():
        d = explored(tmp_path)
        stop, task = await started(d)
        r = await d.run_tool("a", "explore", {})
        lines = [e["message"] for e in d.dialog_channel("a").after(0) if e.get("type") == "progress"]
        assert lines and all(ln.startswith("explored ") and " places, " in ln and ln.endswith(" findings") for ln in lines)
        assert not any(ln in r["text"] for ln in lines)
        await finish(stop, task)
    run(go)

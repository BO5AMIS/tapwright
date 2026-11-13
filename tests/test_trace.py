"""The opt-in timing trace (TAPWRIGHT_TRACE=1): off writes nothing; on writes one well-formed line per touch and per
tree read; the report computes medians and p90s."""
import asyncio
import json

import pytest
from fakes import FakeSession, FrameFeeder
from test_daemon import finish, make, run, started

from tapwright import server, trace, ui
from tapwright import session as session_module
from tapwright.session import Session

XML = ('<AppiumAUT><XCUIElementTypeApplication type="XCUIElementTypeApplication" name="Lab" label="Lab" '
       'bundleId="com.example.lab" enabled="true" x="0" y="0" width="430" height="932">'
       '<XCUIElementTypeButton type="XCUIElementTypeButton" name="lab-taps" label="Tap me" enabled="true" '
       'x="16" y="197" width="398" height="120"/></XCUIElementTypeApplication></AppiumAUT>')


@pytest.fixture(autouse=True)
def _trace_off():
    trace.disable()
    yield
    trace.disable()


def lines(path):
    return [json.loads(raw) for raw in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


class TracedSession(FakeSession):
    """A fake session whose touch and read go through the trace hooks as the real Session's _call does."""

    feeder: FrameFeeder | None = None

    async def tap(self, target, hold_ms=60):
        sent = trace.request("POST", "/session/S/wda/tw/tap")
        await asyncio.sleep(0.01)
        if self.feeder is not None:
            self.feeder.then = b"after the touch"  # the screen changes with the touch
        trace.response(sent, {"value": {"queued": 1, "played_ms": 20.5, "name": "x"}})
        return await super().tap(target, hold_ms)

    async def _traced_read(self):
        asked = trace.request("GET", ui.SOURCE_PATH)
        await asyncio.sleep(0.005)
        trace.note_bytes(len(XML))
        trace.response(asked, {"value": XML})
        ui.read(XML, self.screen)

    async def refresh(self, timeout=None):
        await self._traced_read()
        return await super().refresh(timeout)

    async def look(self, screenshot=True):
        await self._traced_read()
        return await super().look(screenshot)

    async def snapshot(self, keyboard=False, timeout=None):
        await self._traced_read()
        return await super().snapshot(keyboard, timeout)


def ordered(phases: dict, names) -> None:
    values = [phases[n] for n in names]
    assert values == sorted(values) and all(v >= 0 for v in values), phases


def test_off_by_default_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("TAPWRIGHT_TRACE", raising=False)
    assert trace.enable_from_env(tmp_path / "trace.jsonl") is False

    async def go():
        d = make(TracedSession())
        stop, task = await started(d)
        feeder = d.session.feeder = FrameFeeder(d.hub).start(then=b"before the touch")
        await d.run_tool("a", "look", {})
        await d.run_tool("a", "tap", {"target": {"x": 5, "y": 5}})
        await feeder.stop()
        await finish(stop, task)
    run(go)
    assert not (tmp_path / "trace.jsonl").exists() and not trace.on()


def test_the_environment_variable_turns_it_on_and_only_the_value_1(tmp_path):
    assert trace.enable_from_env(tmp_path / "t.jsonl", {"TAPWRIGHT_TRACE": "0"}) is False
    assert trace.enable_from_env(tmp_path / "t.jsonl", {"TAPWRIGHT_TRACE": "1"}) is True


def test_serve_reads_the_variable_once_at_start(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setenv("TAPWRIGHT_TRACE", "1")
    monkeypatch.setattr(server.store, "HOME", tmp_path)
    monkeypatch.setattr(trace, "enable_from_env", lambda path, env=None: seen.append(path))

    async def go():
        await server.serve(make_daemon=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop here")))
    try:
        asyncio.run(go())
    except Exception:
        pass
    assert seen == [tmp_path / "trace.jsonl"]


def test_a_traced_tap_and_a_read_write_ordered_lines(tmp_path):
    path = tmp_path / "trace.jsonl"
    trace.enable(path)

    async def go():
        d = make(TracedSession())
        stop, task = await started(d)
        feeder = d.session.feeder = FrameFeeder(d.hub).start(then=b"before the touch")
        await d.run_tool("a", "look", {})
        reads_before = len(lines(path))
        answer = await d.run_tool("a", "tap", {"target": {"x": 5, "y": 5}})
        assert answer["text"].startswith("Tapped (5, 5).")
        await feeder.stop()
        await finish(stop, task)
        return reads_before
    reads_before = run_value(go)
    found = lines(path)
    assert reads_before >= 1 and found[0]["tool"] == "read"
    reads = [x for x in found if x["tool"] == "read"]
    for line in reads:
        assert line["kind"] == "read" and line["bytes"] == len(XML) and line["t0"] > 0
        ordered(line["phases"], ["sent", "received", "parsed", "built"])
    taps = [x for x in found if x["tool"] == "tap"]
    assert len(taps) == 1
    tap = taps[0]
    assert tap["kind"] == "touch" and tap["touches"] == 1 and tap["wda_value"] == {"queued": 1, "played_ms": 20.5}
    phases = tap["phases"]
    # a point target needs no read: the front-end phases are the ones every touch passes
    assert set(phases) == {"scope", "act", "resolved", "policy", "sent", "wda", "frame", "settled", "returned"}
    ordered(phases, ["scope", "act", "resolved", "policy", "sent", "wda", "settled", "returned"])
    assert phases["sent"] <= phases["frame"] <= phases["settled"]  # the frame that differs came after the touch went out
    assert found.index(tap) > found.index(reads[0]) and tap["t0"] >= reads[0]["t0"]  # monotonic clock


def test_a_tap_by_id_after_an_unlooked_action_splits_its_time_before_the_touch(tmp_path):
    """E1a: the read an id target needs (the list is stale after `look: false`) shows as still/read phases, in order,
    between the queue's turn and the target being resolved; the touch goes out after the policy."""
    path = tmp_path / "trace.jsonl"
    trace.enable(path)

    async def go():
        d = make(TracedSession())
        stop, task = await started(d)
        feeder = d.session.feeder = FrameFeeder(d.hub).start(then=b"before the touch")
        await d.run_tool("a", "look", {})
        await d.run_tool("a", "tap", {"target": {"x": 5, "y": 5}, "look": False})
        await d.run_tool("a", "tap", {"target": {"id": "ok-button"}, "look": False})
        await feeder.stop()
        await finish(stop, task)
    run(go)
    taps = [x for x in lines(path) if x["tool"] == "tap"]
    assert len(taps) == 2
    phases = taps[1]["phases"]
    ordered(phases, ["scope", "act", "still", "read", "resolved", "policy", "sent", "wda", "returned"])
    assert "still" not in taps[0]["phases"]  # a point needs no read


def test_the_trace_hooks_do_nothing_when_off():
    assert trace.arrive() is None and trace.begin("tap") is None
    trace.mark("scope")  # no active touch, and off: nothing happens
    trace.leave(None)


def run_value(scenario):
    return asyncio.run(scenario())


def test_a_refused_or_untouched_call_writes_no_touch_line(tmp_path):
    trace.enable(tmp_path / "trace.jsonl")

    async def go():
        d = make(TracedSession())
        stop, task = await started(d)
        await d.run_tool("a", "look", {})
        with pytest.raises(Exception):
            await d.run_tool("a", "tap", {"target": {"id": "nothing-like-it"}})
        await finish(stop, task)
    run(go)
    assert [x["tool"] for x in lines(tmp_path / "trace.jsonl") if x["kind"] == "touch"] == []


def test_the_real_session_marks_a_fast_tap_and_a_read(tmp_path, monkeypatch):
    """Through Session._call, with a device that answers like the fork's fast tap route."""
    from test_session import FastRunner
    monkeypatch.setattr(session_module.store, "load_state", lambda: {"runner_id": "runner"})
    monkeypatch.setattr(session_module, "FAST_TAP", "1")
    path = tmp_path / "trace.jsonl"
    trace.enable(path)
    runner = FastRunner()
    real = runner.http

    async def http(method, route, payload=None, timeout=30.0):
        if route.startswith("/source"):
            trace.note_bytes(len(XML))  # (the device notes the answer's size)
            return {"value": XML}
        if route.endswith("/wda/tw/tap"):
            return {"value": {"played": 0.031}}
        return await real(method, route, payload, timeout)
    runner.http = http
    s = Session(runner)

    async def go():
        await s.refresh()
        started = trace.begin("tap")
        await s.tap(ui.Target(x=30.4, y=800))
        trace.end(started)
    asyncio.run(go())
    found = lines(path)
    assert [x["tool"] for x in found] == ["read", "tap"]
    assert found[0]["bytes"] == len(XML)
    ordered(found[0]["phases"], ["sent", "received", "parsed", "built"])
    assert found[1]["wda_value"] == {"played": 0.031}
    ordered(found[1]["phases"], ["sent", "wda", "returned"])


def test_the_report_computes_medians_and_p90s_from_a_small_sample(tmp_path, capsys):
    path = tmp_path / "trace.jsonl"
    rows = []
    for n, (wda, settled) in enumerate([(100, 400), (110, 420), (120, 440), (130, 460), (500, 900)]):
        rows.append({"tool": "tap", "kind": "touch", "t0": 1.0 + n,
                     "phases": {"sent": 5.0, "wda": wda, "frame": wda - 20, "settled": settled, "returned": settled + 5}})
    for n, size in enumerate([1000, 2000, 3000]):
        rows.append({"tool": "read", "kind": "read", "t0": 9.0 + n, "bytes": size,
                     "phases": {"sent": 0.0, "received": 50.0 + n, "parsed": 60.0 + n, "built": 70.0 + n}})
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n", encoding="utf-8")
    summary = trace.summarize(trace.load(path))
    tap, read = summary["tap"], summary["read"]
    assert tap["n"] == 5 and tap["phases"]["wda"] == (120.0, 500.0) and tap["phases"]["settled"] == (440.0, 900.0)
    assert list(tap["phases"]) == ["sent", "wda", "frame", "settled", "returned"]
    assert read["n"] == 3 and read["phases"]["received"] == (51.0, 52.0) and read["bytes"] == (2000.0, 3000.0)
    assert trace.main([str(path)]) == 0
    out = capsys.readouterr().out
    assert "tap" in out and "120.0" in out and "500.0" in out and "bytes" in out and "2000" in out
    assert trace.main([str(tmp_path / "missing.jsonl")]) == 1

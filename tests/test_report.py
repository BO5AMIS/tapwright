"""`session_report` and `tapwright report` (AGENT-TOOLS §16.1)."""
import asyncio
import datetime
import json

import pytest

from tapwright import report
from tapwright.actionlog import ActionLog
from tapwright.control import Refused
from fakes import el
from test_daemon import finish, make, started

START = datetime.datetime(2026, 11, 12, 10, 0, 0, tzinfo=datetime.timezone.utc)


def entry(i, tool, client="c", ok=True, code=None, **more):
    at = START + datetime.timedelta(seconds=10 * i)
    return {"t": at.isoformat(timespec="milliseconds").replace("+00:00", "Z"), "source": "tapwright", "by": "agent",
            "client": client, "tool": tool, "ok": ok, **({"code": code} if code else {}), **more}


def build(entries, client="c", since=None):
    return report.build(entries, client, since)


def test_a_quiet_session_has_counts_failures_and_no_waste():
    es = [entry(0, "look", kind="read"), entry(1, "tap", listed=True), entry(2, "tap", ok=False, code="not_found"),
          entry(3, "look", kind="read", labels=True)]
    text = build(es)
    assert text.splitlines()[0] == "Session: 4 calls in 1 min (look 2, tap 2). Failed: 1 (not_found 1)."
    assert text.splitlines()[1] == "Waste: none."


def test_looks_right_after_an_answer_that_had_the_changes_are_flagged_unless_they_ask_for_more():
    es = [entry(0, "tap", listed=True), entry(1, "look", kind="read"),      # 2: flagged
          entry(2, "tap", listed=True), entry(3, "look", kind="read", ocr=True),  # ocr adds something
          entry(4, "tap"), entry(5, "look", kind="read"),                   # the tap's answer had no changes
          entry(6, "tap", listed=True, ok=False, code="not_found"), entry(7, "look", kind="read")]
    text = build(es)
    assert "- 1 look right after an action whose answer already had the changes (calls 2)" in text


def test_retries_after_not_found_with_another_target_name_the_failed_call():
    es = [entry(0, "tap", ok=False, code="not_found", target={"text": "Save"}),
          entry(1, "tap", target={"id": "save"}),
          entry(2, "tap", ok=False, code="not_found", target={"text": "X"}),
          entry(3, "tap", ok=False, code="not_found", target={"text": "X"})]
    assert ("- 1 retry after not_found then worked with another kind of target, likely the same element (calls 1): "
            "use its id first" in build(es))


def test_a_retry_whose_failed_target_was_an_id_says_to_target_what_the_look_shows():
    """AGENT-TOOLS 1.13.1 (4): `use its id first` is no advice when the id was what failed."""
    es = [entry(0, "tap", ok=False, code="not_found", target={"id": "save"}),
          entry(1, "tap", target={"text": "Save"})]
    text = build(es)
    assert ("- 1 retry after not_found then worked with another kind of target, likely the same element (calls 1): "
            "target what the latest look shows (its id there, when it has one)" in text)
    assert "use its id first" not in text
    mixed = es + [entry(2, "tap", ok=False, code="not_found", target={"text": "Save"}),
                  entry(3, "tap", target={"id": "save"})]
    lines = [x for x in build(mixed).splitlines() if "retry after not_found" in x]
    assert lines == [
        "- 1 retry after not_found then worked with another kind of target, likely the same element (calls 3): "
        "use its id first",
        "- 1 retry after not_found then worked with another kind of target, likely the same element (calls 1): "
        "target what the latest look shows (its id there, when it has one)"]


def test_a_scroll_hunt_is_sure_only_when_the_action_after_it_aimed_at_what_was_off_screen():
    scrolls = [entry(i, "scroll", listed=True) for i in range(3)]
    sure = scrolls + [entry(3, "tap", target={"id": "save"}, known_off=True)]
    assert "- 1 scroll hunt of 3 or more scrolls for an element already in the list (calls 1–3): let the action scroll" \
        in build(sure)
    maybe = scrolls + [entry(3, "tap", target={"id": "save"})]
    assert "(calls 1–3 (maybe))" in build(maybe)
    assert "scroll hunt" not in build(scrolls[:2] + [entry(2, "tap")])
    between = [scrolls[0], entry(1, "look", kind="read"), scrolls[1], scrolls[2], entry(4, "tap", known_off=True)]
    assert "calls 1–4" in build(between)


def test_runs_of_three_actions_with_nothing_read_between_them():
    es = [entry(0, "look", kind="read"), entry(1, "tap"), entry(2, "type_text"), entry(3, "toggle"),
          entry(4, "look", kind="read"), entry(5, "tap"), entry(6, "tap"), entry(7, "tap", ok=False, code="stale")]
    text = build(es)
    assert "- 1 run of 3 or more actions with no answer read in between (calls 2–4): one run each" in text
    assert "calls 6" not in text


def test_at_most_five_numbers_then_an_ellipsis_and_the_steps_of_a_run_are_not_calls():
    es = []
    for i in range(7):
        es += [entry(2 * i, "tap", listed=True), entry(2 * i + 1, "look", kind="read")]
    es.append(entry(99, "tap", batch={"id": "r", "step": 0}))
    text = build(es)
    assert "(calls 2, 4, 6, 8, 10, …)" in text
    assert "Session: 14 calls" in text


def test_the_window_is_one_client_since_a_time():
    es = [entry(0, "tap", client="x"), entry(1, "look", client="c", kind="read"), entry(2, "tap", client="c")]
    assert build(es, "c").startswith("Session: 2 calls")
    at = report.epoch_ms(es[2]["t"])
    assert build(es, "c", at).startswith("Session: 1 call in")
    assert report.client_start(es, "c") == report.epoch_ms(es[1]["t"])
    assert report.last_client(es) == "c"
    assert build([], "c") == "Session: 0 calls.\nWaste: none."


def test_since_takes_epoch_iso_or_how_long_ago():
    now = 1_762_950_000_000
    assert report.parse_since("1762940000000", now) == 1_762_940_000_000
    assert report.parse_since("30m", now) == now - 30 * 60_000
    assert report.parse_since("2h", now) == now - 2 * 3_600_000
    assert report.parse_since("2026-11-12T10:00:00Z", now) == report.epoch_ms("2026-11-12T10:00:00Z")
    with pytest.raises(ValueError):
        report.parse_since("yesterday-ish", now)


def test_the_reader_skips_what_is_not_an_entry(tmp_path):
    (tmp_path / "actions-2026-11-12.jsonl").write_text('not json\n[1]\n{"t": "2026-11-12T10:00:00.000Z", "tool": "tap"}\n', "utf-8")
    assert [e["tool"] for e in report.read_entries(tmp_path)] == ["tap"]
    assert report.read_entries(None) == [] and report.read_entries(tmp_path / "nope") == []


def run(go):
    asyncio.run(go())


def test_the_tool_reports_this_agents_calls_from_the_log_in_personal_mode_too(tmp_path):
    async def go():
        d = make(actions=ActionLog(tmp_path))
        stop, task = await started(d)
        d.session.tree = [el("e1", "button", "Save", "save")]
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "tap", {"target": {"id": "save"}})
        await d.run_tool("b", "tap", {"target": {"id": "save"}, "look": False})
        answer = await d.run_tool("a", "session_report", {})
        assert answer["text"].startswith("Session: 2 calls in 1 min (look 1, tap 1). Failed: 0.")
        assert answer["text"].splitlines()[1].startswith("Time:")  # the daemon logs ms, so the line is there
        assert answer["text"].splitlines()[2].startswith("Waste:")
        from tapwright.daemon import Scope
        scoped = await d.run_tool("a", "session_report", {"since": 0}, scope=Scope(apps=["com.example.app"]))
        assert scoped["text"].startswith("Session: 2 calls")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "session_report", {"since": "yesterday"})
        assert e.value.code == "bad_params"
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "session_report", {"client": "b"})
        assert e.value.code == "bad_params"
        await finish(stop, task)
    run(go)


def test_with_the_log_off_the_tool_says_so():
    async def go():
        d = make(actions=ActionLog(None))
        stop, task = await started(d)
        answer = await d.run_tool("a", "session_report", {})
        assert "call log is off" in answer["text"]
        await finish(stop, task)
    run(go)


def test_a_hunt_is_logged_sure_when_the_target_was_off_screen_before_the_first_scroll(tmp_path):
    async def go():
        d = make(actions=ActionLog(tmp_path))
        stop, task = await started(d)
        d.session.tree = [el("e1", "button", "Top", "top")]
        d.session.offscreen = [el("", "button", "Save", "save", rect=(10, 900, 100, 40))]
        for _ in range(3):
            await d.run_tool("a", "scroll", {"look": False})
        d.session.tree = [el("e1", "button", "Top", "top"), el("e2", "button", "Save", "save")]
        await d.run_tool("a", "tap", {"target": {"id": "save"}, "look": False})
        await d.run_tool("a", "tap", {"target": {"id": "save"}, "look": False})  # the run is over
        await finish(stop, task)
        got = [json.loads(x) for f in sorted(tmp_path.glob("actions-*.jsonl")) for x in f.read_text("utf-8").splitlines()]
        assert [g.get("known_off") for g in got] == [None, None, None, True, None]
        assert "(maybe)" not in report.build(got, "a", None)
        assert "scroll hunt" in report.build(got, "a", None)
    run(go)


# ---- seconds (AGENT-TOOLS §16.1, v1.13): `"ms"` in every entry, the Time: line, calls each from a new process ----

def stamped(end_s, tool, ms=None, client="c", **more):
    """An entry as the log writes it: its time is when the call ENDED, `ms` how long it took in the tool."""
    at = START + datetime.timedelta(seconds=end_s)
    return {"t": at.isoformat(timespec="milliseconds").replace("+00:00", "Z"), "source": "tapwright", "by": "agent",
            "client": client, "tool": tool, "ok": True, **({"ms": ms} if ms is not None else {}), **more}


def time_line(es, client="c"):
    lines = build(es, client).splitlines()
    assert lines[0].startswith("Session:")
    return lines[1] if lines[1].startswith("Time:") else None


def test_the_time_line_has_thinking_between_calls_time_in_the_tool_and_medians_by_tool():
    es = [stamped(10, "look", 500),
          stamped(20, "tap", 700),    # began at 19.3: thought for 9.3 s after the look's answer
          stamped(30, "look", 300),   # 9.7 s
          stamped(400, "tap", 900)]   # began at 399.1: 369 s is idle and left out
    text = build(es)
    assert text.splitlines()[1] == ("Time: 21 s: thinking between calls 19 s (median 9.5 s), "
                                    "in the tool 2.4 s (median 0.6 s; tap 0.8 s, look 0.4 s).")
    assert text.splitlines()[2] == "Waste: none."  # the line comes after Session: and before Waste:


def test_minutes_for_long_spans_and_a_gap_of_exactly_two_minutes_still_counts():
    es = [stamped(60, "look", 1000), stamped(180, "tap", 2000), stamped(300, "tap", 2000)]
    # each call began 178 s and 118 s after the one before ended: both count (the limit is over 2 minutes)
    assert time_line(es) == ("Time: 4 min: thinking between calls 4 min (median 118.0 s), "
                             "in the tool 5.0 s (median 2.0 s; tap 2.0 s, look 1.0 s).")
    on_the_limit = [stamped(10, "look", 0), stamped(130, "look", 0)]
    assert "thinking between calls 2 min (median 120.0 s)" in time_line(on_the_limit)
    over = [stamped(10, "look", 0), stamped(131, "look", 0)]
    assert "thinking" not in time_line(over)  # 121 s: idle


def test_one_call_has_no_thinking_and_no_gap_is_made_up():
    assert time_line([stamped(5, "look", 1200)]) == "Time: 1.2 s: in the tool 1.2 s (median 1.2 s; look 1.2 s)."


def test_the_time_line_counts_this_agents_own_calls_and_not_the_steps_of_a_run():
    es = [stamped(10, "look", 500),
          stamped(12, "tap", 100, client="other"),            # another agent's call
          stamped(14, "tap", 9000, batch="r1"),                # a step inside a run: the run's own entry holds its time
          stamped(20, "run", 700)]
    assert time_line(es) == ("Time: 11 s: thinking between calls 9.3 s (median 9.3 s), "
                             "in the tool 1.2 s (median 0.6 s; run 0.7 s, look 0.5 s).")


def test_old_log_entries_without_ms_get_no_time_line_and_the_report_is_as_before():
    es = [entry(0, "look", kind="read"), entry(1, "tap", listed=True), entry(2, "tap")]
    text = build(es)
    assert not any(line.startswith("Time:") for line in text.splitlines())
    assert text.splitlines()[1].startswith("Waste")
    mixed = [stamped(10, "look", 500), stamped(20, "tap")]  # one entry has ms: the line is about what has it
    assert time_line(mixed) == "Time: 0.5 s: in the tool 0.5 s (median 0.5 s; look 0.5 s)."


def test_only_the_three_slowest_tools_are_named_then_an_ellipsis():
    es = [stamped(10, "look", 100), stamped(20, "tap", 4000), stamped(30, "scroll", 3000), stamped(40, "wait", 2000),
          stamped(50, "back", 500)]
    assert time_line(es).endswith("(median 2.0 s; tap 4.0 s, scroll 3.0 s, wait 2.0 s, …).")


def test_calls_each_from_a_new_process_are_flagged_with_their_numbers():
    es = [stamped(10 * i, "look" if i % 2 else "tap", 700, via="call") for i in range(1, 15)]
    flags = build(es).splitlines()
    assert ("- 14 calls each from a new process (calls 1–14): load the MCP server once; each call paid about 2 s"
            in flags)
    assert "Waste:" in flags


def test_a_few_thin_calls_or_one_among_calls_from_a_kept_connection_are_not_flagged():
    two = [stamped(10, "look", 5, via="call"), stamped(20, "tap", 5, via="call"), stamped(30, "look", 5)]
    assert "new process" not in build(two)
    # (1.13.1, item 11: five thin calls in a row make the flag, four don't)
    four = [stamped(10 * i, "look", 5, via="call") for i in range(1, 5)] + [stamped(50, "tap", 5)]
    assert "new process" not in build(four)
    es = ([stamped(10 * i, "look", 5, via="call") for i in range(1, 6)] + [stamped(60, "tap", 5)]
          + [stamped(70 + 10 * i, "tap", 5, via="call") for i in range(0, 5)])
    assert "- 10 calls each from a new process (calls 1–5, 7–11)" in build(es)
    assert "new process" not in build([stamped(10 * i, "look", 5) for i in range(1, 8)])  # an MCP server: none


def test_the_daemon_logs_ms_for_actions_and_reads_and_the_thin_calls_mark(tmp_path, monkeypatch):
    """`ms` is the call's time in the tool, from a fake monotonic clock that moves a quarter second per reading."""
    from tapwright import daemon as daemon_module

    monkeypatch.setattr(daemon_module, "time", _FakeTime(iter(x * 0.25 for x in range(1, 100_000))))

    async def go():
        d = make(actions=ActionLog(tmp_path))
        stop, task = await started(d)
        d.session.tree = [el("e1", "button", "Save", "save")]
        await d.run_tool("a", "look", {"screenshot": False}, via="call")
        await d.run_tool("a", "tap", {"target": {"id": "save"}, "look": False})
        await finish(stop, task)
        got = [json.loads(x) for f in sorted(tmp_path.glob("actions-*.jsonl")) for x in f.read_text("utf-8").splitlines()]
        assert [g["tool"] for g in got] == ["look", "tap"]
        for g in got:
            assert isinstance(g["ms"], int) and g["ms"] >= 250 and g["ms"] % 250 == 0, g
        assert got[0]["via"] == "call" and "via" not in got[1]
    run(go)


class _FakeTime:
    """`time` as the daemon module sees it, with monotonic() from a list of readings; everything else is real."""

    def __init__(self, ticks):
        self._ticks = ticks

    def monotonic(self):
        return next(self._ticks)

    def __getattr__(self, name):
        import time as real_time
        return getattr(real_time, name)


def test_a_picture_check_that_matched_with_named_changes_is_a_line_to_look_at():
    es = [entry(0, "compare_screen", kind="read", percent=0.3, named=1), entry(1, "compare_screen", kind="read", percent=0.0),
          entry(2, "compare_screen", kind="read", percent=0.4, named=2)]
    text = build(es)
    assert "- 2 picture checks matched with named changes under the threshold (calls 1, 3): look at what changed" in text
    assert "Waste: none." not in text
    assert "matched with named" not in build(es[1:2])

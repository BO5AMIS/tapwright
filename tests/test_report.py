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
    assert ("- 1 retry after not_found that found the same element by another target (calls 1): use its id first"
            in build(es))


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
        assert answer["text"].splitlines()[1].startswith("Waste:")
        from tapwright.daemon import Scope
        scoped = await d.run_tool("a", "session_report", {"since": 0}, scope=Scope(apps=[]))
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

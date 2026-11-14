"""Grants in the daemon (docs/AGENT-TOOLS.md §19.3, §19.5, §12, §23): a covered touch goes through with its note, a
matching grant that can't cover it says why before the person's OK, a dry run counts nothing, the action log carries
grant, grant_use and grant_asked, and --revoke-all stops a running batch between moves while a pause revokes nothing."""
import asyncio
import datetime
import json

import pytest
from fakes import button
from test_daemon import finish, make, run, started

from tapwright import daemon as daemon_module
from tapwright import grants, policy
from tapwright.actionlog import ActionLog
from tapwright.auditlog import AuditLog
from tapwright.control import Refused

TZ = datetime.timezone(datetime.timedelta(hours=3))
APP = "com.example.app"  # the fake session's app


def clock_at(text="2026-11-14T10:00:00"):
    box = [datetime.datetime.fromisoformat(text).replace(tzinfo=TZ)]
    return box


def setup(tmp_path, *items, at="2026-11-14T10:00:00"):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"version": 2, "confirm": ["Post", "Delete"], "block": [], "grants": list(items)}),
                    encoding="utf-8")
    box = clock_at(at)
    audit = AuditLog(tmp_path / "audit", now=lambda: box[0].astimezone(datetime.timezone.utc))
    d = make(audit=audit, grant_clock=lambda: box[0], actions=ActionLog(tmp_path / "log"))
    d.session.tree = [button("e1", "Post", "post"), button("e2", "OK", "ok-button", rect=(10, 200, 100, 40))]
    return d, policy.load(path), path, box


def grant(**over):
    g = {"name": "wayfare_posting", "app": APP, "words": ["Post"], "until": "2026-11-21", "perDay": 2,
         "draftsOnly": False}
    g.update(over)
    return {k: v for k, v in g.items() if v is not None}


def log_lines(tmp_path):
    return [json.loads(line) for f in sorted((tmp_path / "log").glob("*.jsonl"))
            for line in f.read_text("utf-8").splitlines()]


def audit_lines(tmp_path):
    return [json.loads(line) for line in (tmp_path / "audit" / "2026-11.jsonl").read_text("utf-8").splitlines()]


def test_a_covered_tap_goes_through_with_its_note_audit_and_log(tmp_path):
    async def go():
        d, rules, path, box = setup(tmp_path, grant())
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
        r = await d.run_tool("a", "tap", {"target": {"id": "post"}, "look": False}, policy=rules)
        assert r["text"].startswith("Tapped e1") and r["text"].endswith(
            'Note: allowed by grant "wayfare_posting" (1 of 2 today).')
        assert "tap" in [c if isinstance(c, str) else c[0] for c in d.session.calls]
        r = await d.run_tool("a", "tap", {"target": {"id": "post"}, "look": False}, policy=rules)
        assert r["text"].endswith('Note: allowed by grant "wayfare_posting" (2 of 2 today).')
        with pytest.raises(Refused) as e:  # the limit: a person is asked, and the answer says why
            await d.run_tool("a", "tap", {"target": {"id": "post"}, "look": False}, policy=rules)
        assert e.value.code == "not_allowed" and "needs_confirmation" in str(e.value)
        assert str(e.value).endswith('Note: grant "wayfare_posting" doesn\'t cover this (2 of 2 today); asking a person.')
        await finish(stop, task)
        taps = [g for g in log_lines(tmp_path) if g["tool"] == "tap"]
        assert [(g.get("grant"), g.get("grant_use"), g.get("grant_asked")) for g in taps] == [
            ("wayfare_posting", "1 of 2 today", None), ("wayfare_posting", "2 of 2 today", None),
            (None, None, "limit reached")]
        lines = audit_lines(tmp_path)
        assert [(a["kind"], a["name"], a["detail"]) for a in lines] == [
            ("grant", "wayfare_posting", 'tap "Post" (1 of 2 today)'),
            ("grant", "wayfare_posting", 'tap "Post" (2 of 2 today)')]
    run(go)


def test_the_note_comes_before_the_persons_ok_and_an_approval_is_audited(tmp_path):
    async def go():
        d, rules, path, box = setup(tmp_path, grant(quiet={"from": "09:00", "to": "11:00"}))
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
        # with the host's dialog the call waits for the person, and its one answer carries both notes (§19.3)
        call = asyncio.create_task(d.run_tool("a", "tap", {"target": {"id": "post"}, "look": False}, policy=rules,
                                              dialog=True))
        for _ in range(100):
            if d._confirms:
                break
            await asyncio.sleep(0.01)
        [ask] = list(d._confirms)
        await d.viewer_message({"type": "confirm_answer", "id": ask, "allow": True})
        r = await asyncio.wait_for(call, 2)
        assert r["text"].splitlines()[-2:] == [
            'Note: grant "wayfare_posting" doesn\'t cover this (quiet hours 09:00 to 11:00); asking a person.',
            "Note: allowed by a person (in the live view)."]
        await finish(stop, task)
        assert [(a["kind"], a["name"], a["detail"]) for a in audit_lines(tmp_path)] == [
            ("approval", "Post", "allowed by a person (in the live view)")]
        taps = [g for g in log_lines(tmp_path) if g["tool"] == "tap"]
        assert taps[-1].get("confirmed") is True and taps[-1].get("grant_asked") == "quiet hours"
        assert taps[-1].get("grant") is None
    run(go)


def test_a_dry_run_says_the_grant_and_counts_nothing(tmp_path):
    async def go():
        d, rules, path, box = setup(tmp_path, grant())
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
        for _ in range(3):
            r = await d.run_tool("a", "tap", {"target": {"id": "post"}, "dry_run": True}, policy=rules)
            assert r["text"].endswith('. It is allowed by grant "wayfare_posting" (1 of 2 today).')
            assert r["text"].startswith("Would tap e1")
        await finish(stop, task)
        assert not (tmp_path / "audit" / "2026-11.jsonl").exists()
    run(go)


def test_a_failed_audit_write_asks_a_person(tmp_path):
    async def go():
        d, rules, path, box = setup(tmp_path, grant())
        (tmp_path / "audit").write_text("in the way", encoding="utf-8")
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "post"}, "look": False}, policy=rules)
        assert "needs_confirmation" in str(e.value)
        await finish(stop, task)
    run(go)


def test_no_grant_covers_a_touch_without_an_audit_log(tmp_path):
    async def go():
        d, rules, path, box = setup(tmp_path, grant())
        d.audit = None
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
        with pytest.raises(Refused):
            await d.run_tool("a", "tap", {"target": {"id": "post"}, "look": False}, policy=rules)
        await finish(stop, task)
    run(go)


def test_a_route_grant_covers_only_inside_that_route(tmp_path):
    async def go():
        d, rules, path, box = setup(tmp_path, grant(route="post_update"))
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "post"}, "look": False}, policy=rules)
        assert "doesn't cover" not in str(e.value)  # a route mismatch isn't noted
        token = daemon_module._ROUTES.set(("post_update",))
        try:
            r = await d.run_tool("a", "tap", {"target": {"id": "post"}, "look": False}, policy=rules)
        finally:
            daemon_module._ROUTES.reset(token)
        assert r["text"].endswith('(1 of 2 today).')
        await finish(stop, task)
    run(go)


def test_a_refused_grant_is_said_in_the_first_answer(tmp_path):
    async def go():
        d, rules, path, box = setup(tmp_path, grant(name="shop_pay", words=["Pay"]))
        stop, task = await started(d)
        r = await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
        assert 'Note: grant "shop_pay" is refused: payments and purchases are never grantable ("Pay").' in r["text"]
        await finish(stop, task)
    run(go)


def test_revoke_all_stops_a_running_batch_between_moves(tmp_path):
    async def go():
        d, rules, path, box = setup(tmp_path, grant(perDay=5))
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
        book = grants.Book(path, None, now=lambda: box[0])  # `tapwright grants --revoke-all`, in another process
        tapped = d.session.tap

        async def tap_then_revoke(*args, **kwargs):
            result = await tapped(*args, **kwargs)
            if not path.read_text("utf-8").count("revoked"):
                assert isinstance(book.revoke_all(), tuple)
            return result
        d.session.tap = tap_then_revoke
        steps = [{"tool": "tap", "target": {"id": "post"}}, {"tool": "tap", "target": {"id": "ok-button"}},
                 {"tool": "tap", "target": {"id": "post"}}]
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": steps, "look": False}, policy=rules)
        first = str(e.value).splitlines()[0]
        assert e.value.code == "not_allowed" and first.startswith("Ran 1 of 3 steps; stopped at step 2")
        assert 'revoked: the grants were revoked while this ran ("wayfare_posting")' in first
        await finish(stop, task)
        assert len([a for a in audit_lines(tmp_path) if a["kind"] == "grant"]) == 1
    run(go)


def test_a_pause_stops_but_doesnt_revoke(tmp_path):
    async def go():
        d, rules, path, box = setup(tmp_path, grant())
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False}, policy=rules)
        await d.viewer_message({"type": "pause", "on": True})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "post"}, "look": False}, policy=rules)
        assert e.value.code == "paused"
        await d.viewer_message({"type": "pause", "on": False})
        assert "revoked" not in path.read_text("utf-8")
        r = await d.run_tool("a", "tap", {"target": {"id": "post"}, "look": False}, policy=rules)
        assert r["text"].endswith('Note: allowed by grant "wayfare_posting" (1 of 2 today).')
        await finish(stop, task)
    run(go)


def test_a_thin_calls_grants_file_is_never_in_force():
    """The model writes a thin call's command line: a grants file it names covers nothing (§19.6)."""
    import asyncio as _asyncio  # noqa: F401  (the server's check is plain logic; see test_server for the HTTP side)
    from tapwright.ceiling import Ceiling
    floor = policy.Policy(confirm=("Post",), file="C:/owner/policy.json")
    sent = policy.Policy(confirm=("Post",), file="C:/model/own.json")
    rules, why = Ceiling(policy=floor).check_policy(sent)
    assert why is None and rules.file is None
    rules, why = Ceiling(policy=floor).check_policy(policy.Policy(confirm=("Post",), file=floor.file))
    assert rules.file == floor.file


def test_revoke_all_stops_a_drive_between_moves(tmp_path):
    """§19.5: the kill switch revokes AND stops a drive between moves (a pause only stops it: see above)."""
    from mapfakes import MapSession
    from test_daemon_drive import GOES, learn, screens

    async def go():
        path = tmp_path / "policy.json"
        path.write_text(json.dumps({"version": 2, "confirm": ["Delete"], "grants": [
            grant(app="com.example.wayfare", words=["Delete"])]}), encoding="utf-8")
        rules = policy.load(path)
        box = clock_at()
        session = MapSession(screens(), "home", GOES)
        session.has_nav_bar, session.back_at = True, (20.0, 70.0)
        d = make(session, audit=AuditLog(tmp_path / "audit"), grant_clock=lambda: box[0])
        from tapwright import mapkeeper
        d.maps = mapkeeper.Maps(str(tmp_path / "maps"))
        stop, task = await started(d)
        await learn(d)
        real = d.session.tap
        book = grants.Book(path, None, now=lambda: box[0])

        async def tap_then_revoke(target, hold_ms=60):
            out = await real(target, hold_ms)
            if "revoked" not in path.read_text("utf-8"):
                assert book.revoke_all() == (["wayfare_posting"], [])
            return out
        d.session.tap = tap_then_revoke
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "drive", {"to": "language"}, policy=rules)
        assert e.value.code == "not_allowed" and "revoked: the grants were revoked while this ran" in str(e.value)
        assert "Done: 1 move." in str(e.value)
        await finish(stop, task)
    run(go)

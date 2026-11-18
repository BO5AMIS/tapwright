"""Later CodeRabbit findings: dry runs, stable refs, and localized starter sends."""
import pytest
from fakes import button, el
from test_daemon import finish, looked, run, started
from test_runner_v5 import CANCEL, SHEET, armed_daemon, hit, source
from test_review_17_safety import daemon_with, draft_grant, taps
from test_unlock_daemon import locked_daemon
from tapwright import daemon as dm
from tapwright.control import Refused


@pytest.mark.parametrize("tool,args", [
    ("tap", {"target": {"id": "ok-button"}}),
    ("run", {"steps": [{"tool": "tap", "target": {"id": "ok-button"}}]}),
    ("drive", {"route": "synthetic_route"}),
    ("replay", {"file": "missing.json"}),
])
def test_dry_run_never_unlocks_or_types_the_vault_passcode(tmp_path, tool, args):
    async def go():
        d, session = locked_daemon(tmp_path)
        stop, task = await started(d, until=("locked",))
        try:
            try:
                await d.run_tool("a", tool, {**args, "dry_run": True})
            except Refused:
                pass  # a locked screen or missing route is expected; no phone effect is allowed
            assert d.lock_screen.pressed == [] and d.lock_screen.calls == [] and session.is_locked
        finally:
            await finish(stop, task)
    run(go)


def test_lifted_target_gets_a_ref_above_every_ref_ever_issued_in_the_view():
    async def go():
        d = armed_daemon(hit({**CANCEL, "l": "Close"}))
        d.session.last_source = source(SHEET)
        stop, task = await started(d)
        try:
            await looked(d, [button("e1", "Today", "today")])
            view = d.control.client("a")
            d.session.items = view.items = [button("e2", "Today", "today"), button("e3", "Next", "next")]
            view.top_ref = 9  # e4..e9 were issued earlier, then disappeared
            close = button("", "Close", "", rect=(24, 83, 36, 36), name="Cancel")
            close.type = "Button"
            d.session.behind = [(close, view.items[0])]
            answer = await d.run_tool("a", "tap", {"target": {"text": "Close"}, "look": False})
            allocated = int(answer["text"].split()[1][1:])
            assert allocated > 9 and view.top_ref >= allocated
            assert len({e.ref for e in d.session.items}) == len(d.session.items)
        finally:
            await finish(stop, task)
    run(go)




def test_schedule_creation_cannot_race_the_final_route_replacement_check(tmp_path):
    from test_daemon_routes import explore, mapped
    from tapwright import schedules

    async def go():
        d = mapped(tmp_path / "maps")
        d.schedules_folder = tmp_path / "schedules"
        stop, task = await started(d)
        try:
            await explore(d)
            await d.run_tool("a", "record", {"start": True, "route": "sign_in"})
            await d.run_tool("a", "tap", {"target": {"id": "continue-btn"}})
            original = d._refuse_held_route
            checks, refused = [], []

            def checked(name):
                original(name)
                checks.append(name)
                if len(checks) == 2:  # after the final check, immediately before the route is written
                    try:
                        schedules.write(schedules.Schedule("daily", name, {}, "09:00 daily", None), d.schedules_folder)
                    except schedules.ScheduleError:
                        refused.append(True)
            d._refuse_held_route = checked
            await d.run_tool("a", "record", {"stop": True})
            assert refused == [True] and not schedules.file_of("daily", d.schedules_folder).exists()
            schedules.write(schedules.Schedule("daily", "sign_in", {}, "09:00 daily", None), d.schedules_folder)
            with pytest.raises(Refused):
                original("sign_in")
        finally:
            await finish(stop, task)
    run(go)

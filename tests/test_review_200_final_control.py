"""Controls for test_review_l.py: what passes on 2f31f06 (the mechanism each failing test isolates)."""
from test_daemon import finish, make, run, started


def test_control_an_unconfirmed_lock_press_is_announced():
    async def go():
        d = make()
        stop, task = await started(d)
        queue = d.subscribe()
        d.session.is_locked = False
        await d.run_tool("a", "press_button", {"name": "lock", "look": False})
        kinds = []
        while not queue.empty():
            m = queue.get_nowait()
            if m.get("type") == "action":
                kinds.append((m["by"], m["kind"]))
        await finish(stop, task)
        assert ("agent", "press_button") in kinds, kinds
    run(go)

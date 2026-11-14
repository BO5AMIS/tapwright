"""Recording while a fresh replay runs (Codex P2, PR #5): the replay's app restart is part of what the recording saves."""
from fakes import bare
import asyncio
import json

from tapwright import batch
from fakes import el
from test_daemon import finish, make, started


def run(go):
    asyncio.run(go())


def test_a_recording_that_replays_a_fresh_script_keeps_its_app_restart(tmp_path):
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.tree = [el("b_only", "button", "Only in B", "b_only", rect=(10, 200, 100, 44))]
        await d.run_tool("a", "look", {"screenshot": False})
        script = tmp_path / "b.json"
        script.write_text(json.dumps(batch.script("b", "com.example.b", [{"tool": "tap", "target": {"id": "b_only"}}])),
                          "utf-8")
        await d.run_tool("a", "record", {"start": True})
        await d.run_tool("a", "open_app", {"app": "com.example.a", "look": False})
        await d.run_tool("a", "replay", {"path": str(script)})
        out = tmp_path / "out.json"
        await d.run_tool("a", "record", {"stop": True, "path": str(out)})
        steps = json.loads(out.read_text("utf-8"))["steps"]
        assert bare(steps) == [{"tool": "open_app", "app": "com.example.a"},
                         {"tool": "close_app", "app": "com.example.b"},
                         {"tool": "open_app", "app": "com.example.b", "expect": {"id": "b_only",
                                                                                 "timeout": 30.0}},
                         {"tool": "tap", "target": {"id": "b_only"}}], steps
        await finish(stop, task)
    run(go)

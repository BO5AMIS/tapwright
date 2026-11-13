"""Fixes from Codex's review of PR #6 (1.4.0): secrets in recordings, network owners, policy on the live front app,
launch overrides, indexed reach, the hold's own death."""
import asyncio
import json

import pytest
from fakes import button, el
from test_daemon import LongList, finish, make, run, started

from tapwright import daemon as daemon_module, policy
from tapwright.control import Refused


# ---- P1: a secure field below the screen is never recorded in plaintext ----
def test_recording_never_saves_text_typed_into_a_secure_field_that_was_off_screen(tmp_path):
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        s.screen = (400.0, 800.0)
        s.tree = [el("e1", "scroll", "", "form", rect=(0, 0, 400, 800)), el("e2", "input", "Email", "email",
                                                                                 rect=(10, 100, 300, 44))]
        s.offscreen = [el("", "input", "Password", "pw", rect=(10, 1000, 300, 44), secure=True)]
        real = s.scroll

        async def scroll(x0, y0, x1, y1):  # the form moves: the password field comes up
            s.tree = [s.tree[0], el("e2", "input", "Password", "pw", rect=(10, 300, 300, 44), secure=True)]
            s.offscreen = []
            return await real(x0, y0, x1, y1)
        s.scroll = scroll
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "record", {"start": True, "name": "sign in"})
        await d.run_tool("a", "type_text", {"target": {"id": "pw"}, "text": "hunter2", "look": False})
        assert s.typed and s.typed[-1][0] == "hunter2"  # it was typed, after the scroll
        path = tmp_path / "s.json"
        r = await d.run_tool("a", "record", {"stop": True, "path": str(path)})
        saved = path.read_text("utf-8")
        assert "hunter2" not in saved and "hunter2" not in r["text"]
        assert json.loads(saved)["steps"] == [{"tool": "type_text", "text": "", "secure": True,
                                               "target": {"id": "pw"}}]
        assert "Step 1 typed into a secure field" in r["text"]
        await finish(stop, task)
    run(go)

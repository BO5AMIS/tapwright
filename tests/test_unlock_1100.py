"""1.10.0: the daemon's unlock paths never type on SpringBoard's other passcode keypad ("Enter Passcode to Update Tonight",
the lead's note of 2026-11-19 ~19:45). The tool call and the start of a `serve --phone` call both go through the
Unlocker; here the locked phone shows that screen instead of the lock screen."""
import pytest
from test_daemon import finish, run, started
from test_unlock import PASSCODE, update_tonight_tree
from test_unlock_daemon import locked_daemon

from tapwright import unlock
from tapwright.control import Refused


def _update_screen(d):
    tree = update_tonight_tree(cover=True)

    async def lock_tree():
        d.lock_screen.calls.append("tree")
        return tree, unlock.SPRINGBOARD
    d.lock_screen.lock_tree = lock_tree


def test_the_unlock_tool_refuses_the_update_keypad_and_types_nothing(tmp_path):
    async def go():
        d, s = locked_daemon(tmp_path, phone=False)
        _update_screen(d)
        stop, task = await started(d, until=("locked",))
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "unlock", {})
        assert e.value.code == "locked" and str(e.value) == unlock.UPDATE_PROMPT
        assert d.lock_screen.pressed == [] and d.lock_screen.secrets == [] and s.is_locked
        assert PASSCODE not in str(e.value)
        await finish(stop, task)
    run(go)


def test_the_phone_mode_unlock_at_a_call_start_refuses_it_too(tmp_path):
    async def go():
        d, s = locked_daemon(tmp_path, phone=True)
        _update_screen(d)
        stop, task = await started(d, until=("locked",))
        result = await d.unlock_phone()
        assert (result.ok, result.code) == (False, "update_prompt")
        assert d.lock_screen.pressed == [] and d.lock_screen.secrets == []
        await finish(stop, task)
    run(go)

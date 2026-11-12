"""Unknown arguments are refused, for every tool (1.3.1): over the HTTP API an argument a tool doesn't take used to be
dropped without a word."""
import asyncio

import pytest

from tapwright import params
from tapwright.batch import check_steps
from tapwright.control import Refused
from test_daemon import finish, make, started


def run(go):
    asyncio.run(go())


def test_the_table_matches_the_mcp_tools_signatures():
    from tapwright import mcp_server
    tools = {t.name: set(t.parameters["properties"]) for t in mcp_server.server._tool_manager.list_tools()}
    tools.pop("viewer")  # (it opens the live view here; it isn't a daemon tool)
    assert set(tools) == set(params.ALLOWED)
    for tool, names in tools.items():
        extra = params.NOT_IN_MCP.get(tool, set())
        assert set(params.ALLOWED[tool]) == names | extra, tool
        assert not names & extra, tool


def test_the_daemon_names_the_arguments_a_tool_does_not_take():
    async def go():
        d = make()
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "audit", {"labels": True, "ocr": True})
        assert e.value.code == "bad_params"
        assert str(e.value) == "audit doesn't take labels, ocr; it takes types."
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "apps", {"x": 1})
        assert str(e.value) == "apps doesn't take x; it takes no arguments."
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "x"}, "forse": True})
        assert "tap doesn't take forse; it takes target, index, force, look, screenshot, dry_run." == str(e.value)
        await finish(stop, task)
    run(go)


def test_names_every_tool_takes_still_pass_and_a_batch_step_is_held_to_the_same_table():
    assert params.unknown("look", {"screenshot": False, "width": 300, "labels": True, "ocr": False}) == []
    assert params.unknown("tap", {"target": {"id": "x"}, "index": 1, "dry_run": True}) == []
    check_steps([{"tool": "tap", "target": {"id": "x"}, "expect": {"text": "Saved"}, "route": "a", "secure": True},
                 {"tool": "performance", "tap": {"id": "x"}, "launch": True}])
    with pytest.raises(Refused) as e:
        check_steps([{"tool": "wait", "seconds": 1}, {"tool": "tap", "target": {"id": "x"}, "labels": True}])
    assert e.value.code == "bad_params"
    assert str(e.value).startswith("step 2: tap doesn't take labels; it takes target, ")


def test_a_steps_own_look_and_screenshot_are_ignored_on_every_step_tool():
    """Review 1.3.1: §9.1 says a step's look and screenshot are ignored, but close_app, performance and compare_screen
    steps carrying them were refused."""
    check_steps([{"tool": "close_app", "app": "x", "look": False},
                 {"tool": "performance", "launch": True, "look": False},
                 {"tool": "compare_screen", "name": "home", "screenshot": False}])


def test_network_idle_is_unsupported_not_an_unknown_argument():
    async def go():
        d = make()
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "wait_for", {"network_idle": True})
        assert e.value.code == "unsupported" and "network_idle" in str(e.value)
        await finish(stop, task)
    run(go)

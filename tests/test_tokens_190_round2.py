"""1.9.0, round 2 (OPTIMIZE-1.9-ROUND2): the core tools loaded with the session and trimmed schemas, the keyboard told
rather than fought, route memory that gets used, the failed-call fixes, the form hint that fires, and the trimmed
instructions with an app-only `logs`."""
import asyncio
import json

import pytest
from mcp.types import ListToolsResult

from tapwright import mcp_server


def wire_tools() -> dict:
    """tools/list as a host receives it (by alias: `_meta`, `inputSchema`)."""
    listed = asyncio.run(mcp_server.server.list_tools())
    wire = ListToolsResult(tools=listed).model_dump(by_alias=True, exclude_none=True, mode="json")
    return {t["name"]: t for t in wire["tools"]}


@pytest.fixture
def eager():
    mcp_server.set_lazy_tools(False)
    mcp_server.set_hints(True)
    yield
    mcp_server.set_lazy_tools(False)
    mcp_server.set_hints(True)


# ---- item 1: the core tools load with the session; schemas without titles ----

def test_the_core_tools_carry_always_load_and_the_rest_stay_deferred(eager):
    tools = wire_tools()
    marked = {name for name, t in tools.items() if (t.get("_meta") or {}).get("anthropic/alwaysLoad") is True}
    assert marked == {"open_app", "open_url", "look", "tap", "type_text", "run", "scroll", "back", "toggle", "wait_for",
                      "drive"}
    assert set(mcp_server.CORE_TOOLS) == marked


def test_the_listed_schemas_have_no_title_keys_but_keep_everything_else(eager):
    tools = wire_tools()
    text = json.dumps({n: t["inputSchema"] for n, t in tools.items()})
    assert '"title"' not in text
    tap = tools["tap"]["inputSchema"]
    assert tap["required"] == ["target"] and tap["properties"]["force"] == {"default": False, "type": "boolean"}
    target = tap["$defs"]["Target"]
    assert set(target["properties"]) == {"ref", "id", "label", "text", "x", "y", "index"}
    assert len(target["description"]) < 90
    # the server's own copy is untouched (so --lazy-tools lists 1.8.5's schemas)
    assert "title" in mcp_server.server._tool_manager.get_tool("tap").parameters["properties"]["force"]


def test_a_property_named_title_is_kept():
    schema = {"title": "X", "properties": {"title": {"title": "Title", "type": "string"}, "a": {"title": "A"}}}
    mcp_server._drop_titles(schema)
    assert schema == {"properties": {"title": {"type": "string"}, "a": {}}}


@pytest.mark.parametrize("how", ["flag", "env", "no_hints"])
def test_lazy_tools_or_no_hints_list_every_tool_deferred_as_before(eager, monkeypatch, how):
    if how == "flag":
        mcp_server.set_lazy_tools(True)
    elif how == "env":
        monkeypatch.setenv("TAPWRIGHT_LAZY_TOOLS", "1")
        mcp_server.set_lazy_tools(False)
    else:
        mcp_server.set_hints(False)
    tools = wire_tools()
    assert not any("_meta" in t for t in tools.values())
    assert '"title"' in json.dumps(tools["tap"]["inputSchema"])
    monkeypatch.delenv("TAPWRIGHT_LAZY_TOOLS", raising=False)
    mcp_server.set_lazy_tools(False)
    mcp_server.set_hints(True)
    assert "_meta" in wire_tools()["tap"]


def test_the_command_line_takes_lazy_tools(monkeypatch):
    import sys
    from tapwright import cli
    seen = {}
    monkeypatch.setattr(mcp_server, "run", lambda **kw: seen.update(kw))
    monkeypatch.setattr(sys, "argv", ["tapwright", "mcp", "--lazy-tools"])
    cli.main()
    assert seen["lazy_tools"] is True
    monkeypatch.setattr(sys, "argv", ["tapwright", "mcp"])
    cli.main()
    assert seen["lazy_tools"] is False

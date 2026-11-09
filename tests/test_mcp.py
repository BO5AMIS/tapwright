"""MCP tools pass through to the daemon (client.call faked)."""
import asyncio
import base64

import pytest
from mcp.types import ImageContent, TextContent

from tapwright import client, mcp_server
from tapwright.errors import CODES


def test_tools_call_the_daemon(monkeypatch):
    seen = {}
    text = "tapped e1\nscreen 1x1 pt, 0 elements\nNote: a person used the phone since your last look."

    def fake_call(tool, args, client_id, timeout=120.0):
        seen.update(tool=tool, args=args, client=client_id)
        return {"text": text, "image": base64.b64encode(b"JPEG").decode(), "image_size": [1, 1]}
    monkeypatch.setattr(client, "call", fake_call)
    out = asyncio.run(mcp_server.tap(target=mcp_server.Target(ref="e1")))
    assert seen["tool"] == "tap" and seen["args"]["target"] == {"ref": "e1"}  # only what was given
    assert seen["client"] == mcp_server.CLIENT_ID
    assert out.is_error is False and len(out.content) == 2
    assert isinstance(out.content[0], TextContent) and out.content[0].text == text
    assert isinstance(out.content[1], ImageContent) and out.content[1].mime_type == "image/jpeg"
    assert base64.b64decode(out.content[1].data) == b"JPEG"


def test_an_answer_without_a_picture_is_one_text_item(monkeypatch):
    monkeypatch.setattr(client, "call", lambda *a, **k: {"text": "closed x", "image": None, "image_size": None})
    out = asyncio.run(mcp_server.terminate_app(bundle_id="x"))
    assert out.is_error is False and [c.text for c in out.content] == ["closed x"]


@pytest.mark.parametrize("code", CODES)
def test_every_code_comes_back_as_an_error(monkeypatch, code):
    def refuse(*a, **k):
        raise client.DaemonError(code, "do the next thing")
    monkeypatch.setattr(client, "call", refuse)
    out = asyncio.run(mcp_server.look())
    assert out.is_error is True and [c.text for c in out.content] == [f"Error {code}: do the next thing"]


def test_a_crash_outside_the_daemon_is_internal_logged_and_not_echoed(monkeypatch, caplog):
    def broken(*a, **k):
        raise OSError("disk on fire")
    monkeypatch.setattr(client, "call", broken)
    out = asyncio.run(mcp_server.look())
    assert out.is_error is True
    assert out.content[0].text == "Error internal: look failed unexpectedly. This is a bug; report it."
    assert any("look" in r.getMessage() and r.exc_info and "disk on fire" in str(r.exc_info[1])
               for r in caplog.records)


def test_a_tool_call_passes_text_and_image_end_to_end(monkeypatch):
    monkeypatch.setattr(client, "call", lambda *a, **k: {
        "text": "tapped e1", "image": base64.b64encode(b"JPEG").decode(), "image_size": [1, 1]})
    out = asyncio.run(mcp_server.server.call_tool("tap", {"target": {"ref": "e1"}}))
    assert out.is_error is False and out.content[0].text == "tapped e1"
    assert isinstance(out.content[1], ImageContent) and base64.b64decode(out.content[1].data) == b"JPEG"


def test_an_unknown_tool_is_unsupported_and_names_the_tools(monkeypatch):
    monkeypatch.setattr(client, "call", lambda *a, **k: pytest.fail("the daemon must not be called"))
    out = asyncio.run(mcp_server.server.call_tool("format_phone", {}))
    text = out.content[0].text
    assert out.is_error is True and text.startswith("Error unsupported: ")
    assert "format_phone" in text and "look" in text


def test_arguments_the_schema_rejects_are_bad_params(monkeypatch):
    monkeypatch.setattr(client, "call", lambda *a, **k: pytest.fail("the daemon must not be called"))
    out = asyncio.run(mcp_server.server.call_tool("type_text", {}))
    assert out.is_error is True
    assert out.content[0].text.startswith("Error bad_params: ") and "text" in out.content[0].text
    assert "Error executing tool" not in out.content[0].text


def test_validation_text_names_fields_without_echoing_input(monkeypatch):
    monkeypatch.setattr(client, "call", lambda *a, **k: pytest.fail("the daemon must not be called"))
    out = asyncio.run(mcp_server.server.call_tool("tap", {"target": {"index": "secret-x"}, "look": "maybe"}))
    text = out.content[0].text
    assert out.is_error is True and text.startswith("Error bad_params: ")
    assert "index" in text and "look" in text
    assert "input_value" not in text and "secret-x" not in text and "pydantic" not in text

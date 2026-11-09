"""MCP tools pass through to the daemon (client.call faked)."""
import asyncio
import base64

from mcp.server.mcpserver import Image

from tapwright import client, mcp_server


def test_tools_call_the_daemon(monkeypatch):
    seen = {}

    def fake_call(tool, args, client_id, timeout=120.0):
        seen.update(tool=tool, args=args, client=client_id)
        return {"texts": ["tapped e1", "screen 1x1 pt, 0 elements"], "image": base64.b64encode(b"JPEG").decode(),
                "notes": ["a person used the phone since your last look"]}
    monkeypatch.setattr(client, "call", fake_call)
    out = asyncio.run(mcp_server.tap(ref="e1"))
    assert seen["tool"] == "tap" and seen["args"]["ref"] == "e1" and seen["client"] == mcp_server.CLIENT_ID
    assert out[:3] == ["tapped e1", "screen 1x1 pt, 0 elements", "note: a person used the phone since your last look"]
    assert isinstance(out[3], Image) and out[3].data == b"JPEG"


def test_refusals_come_back_as_text(monkeypatch):
    def refuse(*a, **k):
        raise client.DaemonError("paused", "paused by the user in the live view; ask them to resume")
    monkeypatch.setattr(client, "call", refuse)
    assert asyncio.run(mcp_server.look()) == ["paused: paused by the user in the live view; ask them to resume"]

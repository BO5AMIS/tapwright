"""The MCP tool set is exactly the shared interface: names, arguments, defaults, hints, round trips."""
import asyncio

import pytest

from tapwright import client, mcp_server

EXPECTED = {
    "look": {"screenshot", "width", "labels", "ocr"},
    "tap": {"target", "force", "look", "screenshot", "dry_run"},
    "long_press": {"target", "seconds", "force", "look", "screenshot", "dry_run"},
    "type_text": {"text", "target", "replace", "submit", "force", "look", "screenshot", "dry_run"},
    "scroll": {"direction", "amount", "to", "target", "look", "screenshot", "dry_run"},
    "toggle": {"target", "value", "force", "look", "screenshot", "dry_run"},
    "back": {"look", "screenshot", "dry_run"},
    "open_url": {"url", "look", "screenshot"},
    "wait": {"seconds", "look", "screenshot"},
    "wait_for": {"text", "id", "label", "gone", "timeout", "look", "screenshot"},
    "apps": set(),
    "open_app": {"app", "look", "screenshot"},
    "viewer": set(),
    "close_app": {"app"},
    "alert": {"action", "look", "screenshot"},
    "press_button": {"name", "look", "screenshot"},
    "swipe": {"x0", "y0", "x1", "y1", "seconds", "look", "screenshot", "dry_run"},
    "rotate": {"to", "look", "screenshot"},
    "audit": {"types"},
    "performance": {"launch", "seconds", "tap"},
    "filmstrip": {"frames", "interval_ms", "frame_width", "tap"},
    "compare_screen": {"name", "update", "width", "threshold", "ignore"},
    "run": {"steps", "trace", "dry_run", "look", "screenshot"},
    "record": {"start", "stop", "name", "path"},
    "replay": {"name", "path", "look", "screenshot"},
}
REQUIRED = {"compare_screen": ["name"], "run": ["steps"], "tap": ["target"], "long_press": ["target"], "toggle": ["target"], "type_text": ["text"],
            "open_url": ["url"], "wait": ["seconds"], "open_app": ["app"], "close_app": ["app"],
            "alert": ["action"], "press_button": ["name"], "swipe": ["x0", "y0", "x1", "y1"], "rotate": ["to"]}
DEFAULTS = {
    "look": {"screenshot": True, "width": 600, "labels": False, "ocr": False},
    "tap": {"force": False, "look": True, "screenshot": False, "dry_run": False},
    "long_press": {"seconds": 1.0, "force": False, "look": True, "screenshot": False, "dry_run": False},
    "type_text": {"target": None, "replace": True, "submit": False, "force": False, "look": True, "screenshot": False,
                  "dry_run": False},
    # no default direction in the schema: it is down, or the way `to` goes (as Tapline declares it)
    "scroll": {"direction": None, "amount": 0.8, "to": None, "target": None, "look": True, "screenshot": False,
               "dry_run": False},
    "toggle": {"value": None, "force": False, "look": True, "screenshot": False, "dry_run": False},
    "wait_for": {"gone": False, "timeout": 10, "look": True, "screenshot": False},
    "swipe": {"seconds": 0.35, "look": True, "screenshot": False, "dry_run": False},
    "filmstrip": {"frames": 6, "interval_ms": 250, "frame_width": 200, "tap": None},
    "compare_screen": {"update": False, "width": 600, "threshold": 0.5, "ignore": None},
}
READ_ONLY = {"audit", "look", "apps", "viewer", "wait", "wait_for"}


def listed() -> dict:
    return {t.name: t for t in asyncio.run(mcp_server.server.list_tools())}


def test_the_tool_set_is_exactly_the_shared_interface():
    tools = listed()
    assert set(tools) == set(EXPECTED)
    for name, tool in tools.items():
        assert set(tool.input_schema.get("properties", {})) == EXPECTED[name], name
        assert sorted(tool.input_schema.get("required", [])) == sorted(REQUIRED.get(name, [])), name


def test_defaults_match_the_daemons():
    tools = listed()
    for name, defaults in DEFAULTS.items():
        props = tools[name].input_schema["properties"]
        for arg, value in defaults.items():
            if value is None:
                assert props[arg].get("default") is None and arg not in tools[name].input_schema.get("required", []), (name, arg)
            else:
                assert props[arg]["default"] == value, (name, arg)


def test_enums_are_literals():
    tools = listed()
    def enum(tool, arg):
        prop = tools[tool].input_schema["properties"][arg]
        return set(prop.get("enum") or [v for part in prop.get("anyOf", []) for v in part.get("enum", [])])
    assert enum("alert", "action") == {"accept", "dismiss", "text"}
    assert enum("press_button", "name") == {"home", "volumeUp", "volumeDown"}
    assert enum("scroll", "direction") == {"up", "down", "left", "right"}
    assert enum("scroll", "to") == {"start", "end", "target"}


def test_read_only_tools_say_so():
    for name, tool in listed().items():
        flag = tool.annotations.read_only_hint if tool.annotations else None
        assert (flag is True) == (name in READ_ONLY), name


def test_the_target_is_one_typed_object_with_exactly_one_kind():
    target = listed()["tap"].input_schema["$defs"]["Target"]
    assert set(target["properties"]) == {"ref", "id", "label", "text", "x", "y", "index"}
    assert target["additionalProperties"] is False
    assert target["oneOf"] == [{"required": ["ref"]}, {"required": ["id"]}, {"required": ["label"]},
                               {"required": ["text"]}, {"required": ["x", "y"]}]
    for name in ("long_press", "toggle", "type_text", "scroll"):
        assert "Target" in listed()[name].input_schema["$defs"], name


def test_an_unknown_key_in_a_target_is_bad_params(monkeypatch):
    monkeypatch.setattr(client, "call", lambda *a, **k: pytest.fail("the daemon must not be called"))
    out = asyncio.run(mcp_server.server.call_tool("tap", {"target": {"zzz": 1}}))
    assert out.is_error and out.content[0].text.startswith("Error bad_params: ")


def test_tools_forward_their_arguments_to_the_daemon(monkeypatch):
    seen = []

    def fake_call(tool, args, client_id, timeout=120.0):
        seen.append((tool, args))
        return {"text": "ok", "image": None, "image_size": None}
    monkeypatch.setattr(client, "call", fake_call)
    asyncio.run(mcp_server.tap(target=mcp_server.Target(label="Log in", index=1)))
    asyncio.run(mcp_server.look())
    asyncio.run(mcp_server.scroll(to="start"))
    asyncio.run(mcp_server.wait(seconds=1.5))
    asyncio.run(mcp_server.wait_for(text="Done", gone=True))
    asyncio.run(mcp_server.open_app(app="Settings"))
    assert seen == [
        ("tap", {"target": {"label": "Log in", "index": 1}, "force": False, "look": True, "screenshot": False,
                 "dry_run": False}),
        ("look", {"screenshot": True, "width": 600, "labels": False}),
        ("scroll", {"direction": None, "amount": 0.8, "to": "start", "target": None, "look": True,
                    "screenshot": False, "dry_run": False}),
        ("wait", {"seconds": 1.5, "look": True, "screenshot": False}),
        ("wait_for", {"text": "Done", "id": None, "label": None, "gone": True, "timeout": 10.0, "look": True,
                      "screenshot": False}),
        ("open_app", {"app": "Settings", "look": True, "screenshot": False}),
    ]


def test_round_trip_through_the_server(monkeypatch):
    seen = []

    def fake_call(tool, args, client_id, timeout=120.0):
        seen.append((tool, args))
        return {"text": f"did {tool}", "image": None, "image_size": None}
    monkeypatch.setattr(client, "call", fake_call)
    out = asyncio.run(mcp_server.server.call_tool("toggle", {"target": {"id": "wifi"}, "value": True}))
    assert not out.is_error and out.content[0].text == "did toggle"
    out = asyncio.run(mcp_server.server.call_tool("press_button", {"name": "home", "look": False}))
    assert not out.is_error and out.content[0].text == "did press_button"
    assert seen == [
        ("toggle", {"target": {"id": "wifi"}, "value": True, "force": False, "look": True, "screenshot": False,
                   "dry_run": False}),
        ("press_button", {"name": "home", "look": False, "screenshot": False}),
    ]
    bad = asyncio.run(mcp_server.server.call_tool("press_button", {"name": "power"}))
    assert bad.is_error and bad.content[0].text.startswith("Error bad_params: ") and len(seen) == 2


def test_the_old_tool_names_are_gone(monkeypatch):
    monkeypatch.setattr(client, "call", lambda *a, **k: pytest.fail("the daemon must not be called"))
    for name in ("list_apps", "launch_app", "terminate_app"):
        out = asyncio.run(mcp_server.server.call_tool(name, {}))
        assert out.is_error and out.content[0].text.startswith("Error unsupported: ")


def test_viewer_opens_the_live_view_for_the_user_and_never_gives_the_agent_its_key(monkeypatch):
    """The link's key can drive the phone and approve a held-back touch (safety review M2, 2026-11-12)."""
    import webbrowser
    opened: list[str] = []
    monkeypatch.setattr(client, "find_or_start", lambda wait=60.0: {"port": 8417, "token": "T0KEN"})
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
    out = asyncio.run(mcp_server.viewer())
    text = out.content[0].text
    assert not out.is_error and opened == ["http://127.0.0.1:8417/?token=T0KEN"]
    assert "T0KEN" not in text and text.startswith("Opened the live view in a browser on this computer")
    monkeypatch.setattr(webbrowser, "open", lambda url: False)
    text = asyncio.run(mcp_server.viewer()).content[0].text
    assert "T0KEN" not in text and "tapwright view" in text


def test_viewer_never_logs_the_token(monkeypatch, caplog):
    monkeypatch.setattr(client, "find_or_start", lambda wait=60.0: {"port": 8417, "token": "SECRET"})
    with caplog.at_level("DEBUG"):
        asyncio.run(mcp_server.viewer())
    assert "SECRET" not in caplog.text


def test_viewer_when_the_daemon_cannot_start(monkeypatch):
    def broken(wait=60.0):
        raise client.DaemonError("not_connected", "tapwright serve didn't start; see ~/.tapwright/serve.log")
    monkeypatch.setattr(client, "find_or_start", broken)
    out = asyncio.run(mcp_server.viewer())
    assert out.is_error and out.content[0].text.startswith("Error not_connected: ")


def test_the_target_schema_has_no_nullable_members_and_validates_strictly():
    import jsonschema
    target = listed()["tap"].input_schema["$defs"]["Target"]
    for name, prop in target["properties"].items():
        assert "anyOf" not in prop and prop.get("type") != "null" and "default" not in prop, name
    validator = jsonschema.Draft202012Validator(target)
    assert validator.is_valid({"id": "a"}) and validator.is_valid({"x": 1, "y": 2})
    assert validator.is_valid({"label": "Row", "index": 1})
    assert not validator.is_valid({"id": "a", "ref": "e1"})
    assert not validator.is_valid({"id": "a", "ref": None})  # an unset key must be absent


def test_a_target_filled_with_nulls_is_still_accepted(monkeypatch):
    seen = []
    monkeypatch.setattr(client, "call", lambda tool, args, cid, timeout=120.0: seen.append(args) or
                        {"text": "ok", "image": None, "image_size": None})
    nulls = {"id": "a", "ref": None, "label": None, "text": None, "x": None, "y": None, "index": None}
    out = asyncio.run(mcp_server.server.call_tool("tap", {"target": nulls}))
    assert not out.is_error and seen[0]["target"] == {"id": "a"}

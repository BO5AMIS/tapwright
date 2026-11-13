"""MCP tools pass through to the daemon (client.call faked)."""
import asyncio
import base64
import re
from datetime import datetime, timedelta

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
    out = asyncio.run(mcp_server.close_app(app="x"))
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


def test_save_screenshot_names_prunes_and_answers_with_the_path(tmp_path):
    (tmp_path / "keep-me.jpg").write_bytes(b"mine")
    start = datetime(2026, 11, 9, 12, 0, 0)
    paths = []
    for i in range(55):
        line = mcp_server.save_screenshot(tmp_path, "look", b"jpeg%d" % i, (600, 1200),
                                          now=start + timedelta(milliseconds=i))
        paths.append(line)
    ours = sorted(p.name for p in tmp_path.iterdir() if re.fullmatch(r"\d{8}-\d{6}-\d{3}-[0-9a-f]{4}-look\.jpg", p.name))
    assert len(ours) == 50 and (tmp_path / "keep-me.jpg").exists()       # newest 50; others left alone
    assert ours[-1].startswith("20261109-120000-054-") and ours[0].startswith("20261109-120000-005-")
    assert paths[-1] == f"Screenshot: {tmp_path / ours[-1]} (600x1200)"
    assert (tmp_path / ours[-1]).read_bytes() == b"jpeg54"


def test_file_mode_replaces_the_image_with_a_path_line(monkeypatch, tmp_path):
    def fake_call(tool, args, client_id, timeout=120.0):
        return {"text": "Screen 1x1\nSettled in 0.10 s.", "image": base64.b64encode(b"JPEG").decode(),
                "image_size": [600, 1200]}
    monkeypatch.setattr(client, "call", fake_call)
    monkeypatch.setattr(mcp_server, "SHOTS_DIR", tmp_path)
    out = asyncio.run(mcp_server._tool("look", screenshot=True))
    assert not out.is_error and len(out.content) == 1 and out.content[0].type == "text"
    last = out.content[0].text.splitlines()[-1]
    assert re.fullmatch(r"Screenshot: .+-look\.jpg \(600x1200\)", last)
    saved = list(tmp_path.iterdir())
    assert len(saved) == 1 and saved[0].read_bytes() == b"JPEG"


def test_without_file_mode_the_image_is_sent(monkeypatch):
    def fake_call(tool, args, client_id, timeout=120.0):
        return {"text": "Screen", "image": base64.b64encode(b"JPEG").decode(), "image_size": [600, 1200]}
    monkeypatch.setattr(client, "call", fake_call)
    monkeypatch.setattr(mcp_server, "SHOTS_DIR", None)
    out = asyncio.run(mcp_server._tool("look", screenshot=True))
    assert [c.type for c in out.content] == ["text", "image"] and out.content[1].mime_type == "image/jpeg"


def test_look_takes_screenshot_width_and_labels_and_actions_take_screenshot(monkeypatch):
    seen = {}

    def fake_call(tool, args, client_id, timeout=120.0):
        seen.clear()
        seen.update(args)
        return {"text": "Screen", "image": None, "image_size": None}
    monkeypatch.setattr(client, "call", fake_call)
    asyncio.run(mcp_server.look(screenshot=False, width=300, labels=True))
    assert seen == {"screenshot": False, "width": 300, "labels": True}
    asyncio.run(mcp_server.tap(target=mcp_server.Target(ref="e1"), screenshot=True))
    assert seen["screenshot"] is True


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


def test_a_target_error_is_one_line_with_an_example(monkeypatch):
    monkeypatch.setattr(client, "call", lambda *a, **k: pytest.fail("the daemon must not be called"))
    for arguments in ({"target": {"index": "x"}}, {}, {"target": {"bogus": 1}}):
        text = asyncio.run(mcp_server.server.call_tool("tap", arguments)).content[0].text
        assert text.startswith("Error bad_params: ") and '{"ref": "e3"}' in text, text
        assert "\n" not in text.removeprefix("Error bad_params: ").split("; a target is")[0], text


def test_two_saves_in_the_same_millisecond_keep_both(tmp_path):
    when = datetime(2026, 11, 9, 12, 0, 0)
    for i in range(2):
        mcp_server.save_screenshot(tmp_path, "look", b"%d" % i, (1, 1), now=when)
    assert len(list(tmp_path.iterdir())) == 2


def test_a_save_leaves_no_temporary_file(tmp_path):
    mcp_server.save_screenshot(tmp_path, "look", b"x", (1, 1))
    assert [p.suffix for p in tmp_path.iterdir()] == [".jpg"]


def test_a_directory_that_cannot_be_written_keeps_the_answer_and_sends_no_image(monkeypatch, tmp_path):
    (tmp_path / "file").write_text("not a directory")
    bad = tmp_path / "file" / "shots"
    monkeypatch.setattr(client, "call", lambda *a, **k: {
        "text": "Screen", "image": base64.b64encode(b"JPEG").decode(), "image_size": [6, 12]})
    monkeypatch.setattr(mcp_server, "SHOTS_DIR", bad)
    out = asyncio.run(mcp_server._tool("look", screenshot=True))
    assert not out.is_error and [c.type for c in out.content] == ["text"]
    last = out.content[0].text.splitlines()[-1]
    assert last.startswith(f"Note: no screenshot: couldn't save to {bad}: ") and out.content[0].text.startswith("Screen")


def test_the_directory_is_checked_at_startup(tmp_path):
    (tmp_path / "file").write_text("x")
    mcp_server.check_screenshots_dir(tmp_path / "ok")  # made on the way
    assert (tmp_path / "ok").is_dir()
    with pytest.raises(SystemExit) as e:
        mcp_server.check_screenshots_dir(tmp_path / "file" / "shots")
    assert "--screenshots" in str(e.value) and "shots" in str(e.value)


def test_file_mode_saves_a_journeys_second_picture_too_and_sends_nothing_inline(monkeypatch, tmp_path):
    from PIL import Image
    import io as _io
    buf = _io.BytesIO()
    Image.new("RGB", (30, 60)).save(buf, "JPEG")
    jpeg = base64.b64encode(buf.getvalue()).decode()

    def fake_call(tool, args, client_id, timeout=120.0):
        return {"text": "Journey done.", "image": jpeg, "image_size": [30, 60], "images": [jpeg]}
    monkeypatch.setattr(client, "call", fake_call)
    monkeypatch.setattr(mcp_server, "SHOTS_DIR", tmp_path)
    out = asyncio.run(mcp_server._tool("journey", stop=True, reached=True))
    assert [c.type for c in out.content] == ["text"]
    lines = out.content[0].text.splitlines()
    assert lines[-1].endswith("-journey_more.jpg (30x60)") and lines[-2].endswith("-journey.jpg (30x60)")
    assert len(list(tmp_path.iterdir())) == 2

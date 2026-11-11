"""tapwright as an MCP server (stdio): tools an agent uses to test apps on your iPhone.

The tools, targets and error codes are described in descriptions.py (and docs/AGENT-TOOLS.md).

The phone belongs to `tapwright serve`, which this server starts when needed; a person
may be watching or driving in its live view at the same time.
"""
import asyncio
import base64
import logging
import os
import re
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, ValidationError

from . import client, device, policy, ui
from .descriptions import INSTRUCTIONS, describe

logger = logging.getLogger("tapwright")

SHOTS_DIR: Path | None = None  # set by `tapwright mcp --screenshots DIR`
KEEP_SHOTS = 50
_SHOT_NAME = re.compile(r"^\d{8}-\d{6}-\d{3}-[0-9a-f]{4}-[a-z_]+\.jpg$")


def check_screenshots_dir(directory: Path) -> None:
    """Fail at startup, with a clear message, when `--screenshots DIR` can't be written to."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".tapwright-{secrets.token_hex(4)}.tmp"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as e:
        raise SystemExit(f"tapwright mcp: can't write screenshots to --screenshots {directory}: {e.strerror or e}") from e


def save_screenshot(directory: Path, tool: str, jpeg: bytes, size, now: datetime | None = None) -> str:
    """Write the picture to DIR/<UTC time>-<tool>.jpg (a short random part keeps two in one millisecond apart),
    keep the newest 50 of ours, and return the answer line. Written under a temporary name first, so
    nothing ever reads half a picture."""
    directory.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now(timezone.utc)
    stem = f"{now:%Y%m%d-%H%M%S}-{now.microsecond // 1000:03d}-{secrets.token_hex(2)}-{tool}"
    path, temporary = directory / f"{stem}.jpg", directory / f".{stem}.tmp"
    temporary.write_bytes(jpeg)
    os.replace(temporary, path)
    ours = sorted(p for p in directory.iterdir() if _SHOT_NAME.match(p.name))  # UTC names sort by time
    for old in ours[:-KEEP_SHOTS]:
        try:
            old.unlink()
        except OSError:
            pass
    return f"Screenshot: {path} ({size[0]}x{size[1]})"


def _result(text: str, image: str | None = None, is_error: bool = False) -> CallToolResult:
    """One text item, plus the screenshot (base64 JPEG) when there is one."""
    content: list[TextContent | ImageContent] = [TextContent(type="text", text=text)]
    if image:
        content.append(ImageContent(type="image", data=image, mime_type="image/jpeg"))
    return CallToolResult(content=content, is_error=is_error)


def _error(code: str, message: str) -> CallToolResult:
    """A failed call: `Error <code>: <message>` as the only content, with is_error set."""
    return _result(f"Error {code}: {message}", is_error=True)


class TapwrightServer(MCPServer):
    """MCPServer whose own failures (arguments the schema rejects, a tool that raised) use the same
    `Error <code>: <message>` form as the daemon's refusals."""

    async def call_tool(self, name, arguments, context=None):
        try:
            return await super().call_tool(name, arguments, context)
        except UnexpectedToolError:
            logger.exception("tool %s crashed", name)
            return _error("internal", f"{name} failed unexpectedly. This is a bug; report it.")
        except ToolError as e:
            if isinstance(e.__cause__, ValidationError):  # the field names and what's wrong, not the input
                errors = e.__cause__.errors()
                lines = [f"{'.'.join(str(part) for part in err['loc'])}: {err['msg'].splitlines()[0]}"
                         for err in errors]
                if any(err["loc"][:1] == ("target",) for err in errors):
                    lines.append(ui.TARGET_HELP)
                return _error("bad_params", "; ".join(lines))
            if str(e).startswith("Unknown tool"):
                tools = ", ".join(t.name for t in self._tool_manager.list_tools())
                return _error("unsupported", f"no tool named {name!r}; the tools are {tools}")
            return _error("bad_params", str(e).removeprefix(f"Error executing tool {name}: "))


def _unset_means_absent(schema: dict) -> None:
    """Drop the null branch from every field, so a client must leave an unset key out. Otherwise a `null`
    counts as present in the `oneOf` below. The server still accepts nulls and strips them."""
    schema["oneOf"] = [{"required": ["ref"]}, {"required": ["id"]}, {"required": ["label"]},
                       {"required": ["text"]}, {"required": ["x", "y"]}]
    for prop in schema["properties"].values():
        prop.pop("default", None)
        kinds = [k for k in prop.pop("anyOf", []) if k.get("type") != "null"]
        if kinds:
            prop.update(kinds[0])


class Target(BaseModel):
    """What to act on: exactly one of ref, id, label, text, or x with y. The daemon checks the rule and
    answers `bad_params` with an example when it isn't met."""
    model_config = ConfigDict(extra="forbid", json_schema_extra=_unset_means_absent)
    ref: str | None = None
    id: str | None = None
    label: str | None = None
    text: str | None = None
    x: float | None = None
    y: float | None = None
    index: int | None = None


READ_ONLY = ToolAnnotations(read_only_hint=True)
server = TapwrightServer("tapwright", instructions=INSTRUCTIONS)
CLIENT_ID = uuid.uuid4().hex  # this agent's own last look, kept by the daemon


async def _tool(tool: str, /, **args) -> CallToolResult:
    """Run one tool in the daemon; its answer is one text item, plus the screenshot if there is one."""
    args = {k: (v.model_dump(exclude_none=True) if isinstance(v, BaseModel) else v) for k, v in args.items()}
    try:
        answer = await asyncio.to_thread(client.call, tool, args, CLIENT_ID)
    except client.DaemonError as e:
        return _error(e.code, str(e))
    except Exception:  # the daemon unreachable: never crash the server
        logger.exception("tool %s crashed", tool)
        return _error("internal", f"{tool} failed unexpectedly. This is a bug; report it.")
    text, image = answer.get("text") or "(no answer)", answer.get("image")
    if image and SHOTS_DIR is not None:  # file mode: one text item, the picture on disk
        try:
            text += "\n" + save_screenshot(SHOTS_DIR, tool, base64.b64decode(image), answer.get("image_size") or [0, 0])
        except OSError as e:  # the user chose file mode: no inline picture instead, but the answer stays
            text += f"\nNote: no screenshot: couldn't save to {SHOTS_DIR}: {e.strerror or e}"
        image = None
    return _result(text, image)


@server.tool(annotations=READ_ONLY, description=describe("look"), structured_output=False)
async def look(screenshot: bool = True, width: int = 600, labels: bool = False) -> CallToolResult:
    return await _tool("look", screenshot=screenshot, width=width, labels=labels)


@server.tool(description=describe("tap"), structured_output=False)
async def tap(target: Target, force: bool = False, look: bool = True, screenshot: bool = False,
              dry_run: bool = False) -> CallToolResult:
    return await _tool("tap", target=target, force=force, look=look, screenshot=screenshot, dry_run=dry_run)


@server.tool(description=describe("long_press"), structured_output=False)
async def long_press(target: Target, seconds: float = 1.0, force: bool = False, look: bool = True,
                     screenshot: bool = False, dry_run: bool = False) -> CallToolResult:
    return await _tool("long_press", target=target, seconds=seconds, force=force, look=look, screenshot=screenshot,
                       dry_run=dry_run)


@server.tool(description=describe("type_text"), structured_output=False)
async def type_text(text: str, target: Target | None = None, replace: bool = True, submit: bool = False,
                    force: bool = False, look: bool = True, screenshot: bool = False,
                    dry_run: bool = False) -> CallToolResult:
    return await _tool("type_text", text=text, target=target, replace=replace, submit=submit, force=force,
                       look=look, screenshot=screenshot, dry_run=dry_run)


@server.tool(description=describe("scroll"), structured_output=False)
async def scroll(direction: Literal["up", "down", "left", "right"] | None = None, amount: float = 0.8,
                 to: Literal["start", "end", "target"] | None = None, target: Target | None = None, look: bool = True,
                 screenshot: bool = False, dry_run: bool = False) -> CallToolResult:
    return await _tool("scroll", direction=direction, amount=amount, to=to, target=target, look=look,
                       screenshot=screenshot, dry_run=dry_run)


@server.tool(description=describe("toggle"), structured_output=False)
async def toggle(target: Target, value: bool | None = None, force: bool = False, look: bool = True,
                 screenshot: bool = False, dry_run: bool = False) -> CallToolResult:
    return await _tool("toggle", target=target, value=value, force=force, look=look, screenshot=screenshot,
                       dry_run=dry_run)


@server.tool(description=describe("back"), structured_output=False)
async def back(look: bool = True, screenshot: bool = False, dry_run: bool = False) -> CallToolResult:
    return await _tool("back", look=look, screenshot=screenshot, dry_run=dry_run)


@server.tool(description=describe("open_url"), structured_output=False)
async def open_url(url: str, look: bool = True, screenshot: bool = False) -> CallToolResult:
    return await _tool("open_url", url=url, look=look, screenshot=screenshot)


@server.tool(annotations=READ_ONLY, description=describe("wait"), structured_output=False)
async def wait(seconds: float, look: bool = True, screenshot: bool = False) -> CallToolResult:
    return await _tool("wait", seconds=seconds, look=look, screenshot=screenshot)


@server.tool(annotations=READ_ONLY, description=describe("wait_for"), structured_output=False)
async def wait_for(text: str | None = None, id: str | None = None, label: str | None = None, gone: bool = False,
                   timeout: float = 10.0, look: bool = True, screenshot: bool = False) -> CallToolResult:
    return await _tool("wait_for", text=text, id=id, label=label, gone=gone, timeout=timeout, look=look,
                       screenshot=screenshot)


@server.tool(annotations=READ_ONLY, description=describe("apps"), structured_output=False)
async def apps() -> CallToolResult:
    return await _tool("apps")


@server.tool(description=describe("open_app"), structured_output=False)
async def open_app(app: str, look: bool = True, screenshot: bool = False) -> CallToolResult:
    return await _tool("open_app", app=app, look=look, screenshot=screenshot)


@server.tool(annotations=READ_ONLY, description=describe("viewer"), structured_output=False)
async def viewer() -> CallToolResult:
    # Unlike the other tools this one may start the daemon (find_or_start), since only this side knows its port and key.
    try:
        info = await asyncio.to_thread(client.find_or_start)
    except client.DaemonError as e:
        return _error(e.code, str(e))
    return _result(f"Live view: {client.viewer_url(info)}\n"
                   "The link carries a key and is for the user only: give it to no one else, and don't repeat it.")


@server.tool(description=describe("close_app"), structured_output=False)
async def close_app(app: str) -> CallToolResult:
    return await _tool("close_app", app=app)


@server.tool(description=describe("alert"), structured_output=False)
async def alert(action: Literal["accept", "dismiss", "text"], look: bool = True,
                screenshot: bool = False) -> CallToolResult:
    return await _tool("alert", action=action, look=look, screenshot=screenshot)


@server.tool(description=describe("press_button"), structured_output=False)
async def press_button(name: Literal["home", "volumeUp", "volumeDown"], look: bool = True,
                       screenshot: bool = False) -> CallToolResult:
    return await _tool("press_button", name=name, look=look, screenshot=screenshot)


@server.tool(description=describe("swipe"), structured_output=False)
async def swipe(x0: float, y0: float, x1: float, y1: float, seconds: float = 0.35, look: bool = True,
                screenshot: bool = False, dry_run: bool = False) -> CallToolResult:
    return await _tool("swipe", x0=x0, y0=y0, x1=x1, y1=y1, seconds=seconds, look=look, screenshot=screenshot,
                       dry_run=dry_run)


@server.tool(description=describe("rotate"), structured_output=False)
async def rotate(to: Literal["portrait", "landscape_left", "landscape_right"], look: bool = True,
                 screenshot: bool = False) -> CallToolResult:
    return await _tool("rotate", to=to, look=look, screenshot=screenshot)


def run(screenshots: str | None = None, apps: list[str] | None = None, dedicated: bool = False,
        rules: policy.Policy | None = None) -> None:
    client.set_scope(apps, dedicated)
    client.set_policy(rules)
    global SHOTS_DIR
    SHOTS_DIR = Path(screenshots).expanduser().resolve() if screenshots else None
    if SHOTS_DIR is not None:
        check_screenshots_dir(SHOTS_DIR)
    device.run(server.run_stdio_async)

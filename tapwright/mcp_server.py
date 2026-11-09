"""tapwright as an MCP server (stdio): tools an agent uses to test apps on your iPhone.

Every action returns the screen right after it (elements as text, plus a screenshot),
so one call is one step. A `target` is one object: `{"ref": "e12"}` from the last look,
`{"id": ...}` (the accessibility identifier, React Native's testID), `{"label": ...}`,
`{"text": ...}` (visible text), or `{"x": ..., "y": ...}` in points.

The phone belongs to `tapwright serve`, which this server starts when needed; a person
may be watching or driving in its live view at the same time.
"""
import asyncio
import logging
import uuid
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import CallToolResult, ImageContent, TextContent
from pydantic import BaseModel, ConfigDict, ValidationError

from . import client, device, ui

logger = logging.getLogger("tapwright")

INSTRUCTIONS = (
    "Test apps on the user's real iPhone. Start with list_apps and launch_app, then look. Every action "
    "returns the new screen, so check it before the next step. Prefer `id` targets (accessibility ids, "
    "React Native testIDs), then `label`, then `ref`; use x and y only for things with no element. This is "
    "the user's own phone: don't buy, send, post, delete or change account settings unless the user "
    "asked for exactly that. System alerts (permissions) show up in look; answer them with `alert`. "
    "A person may be watching or driving in the live view. Refusals: `busy` (a person is driving: wait "
    "and retry), `paused` (ask the user to resume), `stale` (look again), `locked` (ask the user to "
    "unlock the phone), `reconnecting` (wait, then look)."
)


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


class Target(BaseModel):
    """What to act on: exactly one of ref, id, label, text, or x with y. The daemon checks the rule and
    answers `bad_params` with an example when it isn't met."""
    model_config = ConfigDict(extra="forbid")
    ref: str | None = None
    id: str | None = None
    label: str | None = None
    text: str | None = None
    x: float | None = None
    y: float | None = None
    index: int | None = None


server = TapwrightServer("tapwright", instructions=INSTRUCTIONS)
CLIENT_ID = uuid.uuid4().hex  # this agent's own last look, kept by the daemon


async def _tool(name: str, **args) -> CallToolResult:
    """Run one tool in the daemon; its answer is one text item, plus the screenshot if there is one."""
    args = {k: (v.model_dump(exclude_none=True) if isinstance(v, BaseModel) else v) for k, v in args.items()}
    try:
        answer = await asyncio.to_thread(client.call, name, args, CLIENT_ID)
    except client.DaemonError as e:
        return _error(e.code, str(e))
    except Exception:  # the daemon unreachable: never crash the server
        logger.exception("tool %s crashed", name)
        return _error("internal", f"{name} failed unexpectedly. This is a bug; report it.")
    return _result(answer.get("text") or "(no answer)", answer.get("image"))


@server.tool(structured_output=False)
async def look(screenshot: bool = True) -> CallToolResult:
    """The current screen: every visible element (type, label, id, position) and a screenshot."""
    return await _tool("look", screenshot=screenshot)


@server.tool(structured_output=False)
async def list_apps() -> CallToolResult:
    """Apps installed on the phone by you (App Store, TestFlight, development builds), with bundle ids."""
    return await _tool("list_apps")


@server.tool(structured_output=False)
async def launch_app(bundle_id: str, look: bool = True) -> CallToolResult:
    """Open an app by bundle id (from list_apps), bringing it to the front."""
    return await _tool("launch_app", bundle_id=bundle_id, look=look)


@server.tool(structured_output=False)
async def terminate_app(bundle_id: str) -> CallToolResult:
    """Close an app (to start it fresh next time)."""
    return await _tool("terminate_app", bundle_id=bundle_id)


@server.tool(structured_output=False)
async def tap(target: Target, look: bool = True) -> CallToolResult:
    """Tap an element. `target` is exactly one of {"ref": "e3"} (from the last look), {"id": "login-button"},
    {"label": "Log in"} (exact, ignoring case), {"text": "Log in"} (exact, then partial) or {"x": 120, "y": 300}
    (points), plus "index" (0 is the first) with id, label or text when several match."""
    return await _tool("tap", target=target, look=look)


@server.tool(structured_output=False)
async def long_press(target: Target, seconds: float = 1.0, look: bool = True) -> CallToolResult:
    """Press and hold an element (context menus, drag handles). `target` is as for tap."""
    return await _tool("long_press", target=target, seconds=seconds, look=look)


@server.tool(structured_output=False)
async def type_text(text: str, target: Target | None = None, replace: bool = False, submit: bool = False,
                    look: bool = True) -> CallToolResult:
    """Type into a field: the target (as for tap) is tapped first to focus it; without a target, into the
    focused field. replace clears the field first; submit presses return afterwards."""
    return await _tool("type_text", text=text, target=target, replace=replace, submit=submit, look=look)


@server.tool(structured_output=False)
async def scroll(direction: Literal["up", "down", "left", "right"] = "down", amount: float = 0.5,
                 look: bool = True) -> CallToolResult:
    """Scroll the content: "down" shows what is below. amount is a share of the screen (0.1-0.8)."""
    return await _tool("scroll", direction=direction, amount=amount, look=look)


@server.tool(structured_output=False)
async def swipe(x0: float, y0: float, x1: float, y1: float, seconds: float = 0.35, look: bool = True) -> CallToolResult:
    """Drag from one point to another, in points (sliders, carousels, swipe actions, edge gestures)."""
    return await _tool("swipe", x0=x0, y0=y0, x1=x1, y1=y1, seconds=seconds, look=look)


@server.tool(structured_output=False)
async def press_button(name: Literal["home", "volumeUp", "volumeDown"], look: bool = True) -> CallToolResult:
    """Press a hardware button. home leaves the app (it keeps running)."""
    return await _tool("press_button", name=name, look=look)


@server.tool(structured_output=False)
async def alert(action: Literal["accept", "dismiss", "text"], look: bool = True) -> CallToolResult:
    """Answer the system alert on screen (accept = the default/allow button, dismiss = cancel/don't
    allow), or read its text."""
    return await _tool("alert", action=action, look=look)


@server.tool(structured_output=False)
async def open_url(url: str, look: bool = True) -> CallToolResult:
    """Open a URL or deep link (myapp://screen) on the phone."""
    return await _tool("open_url", url=url, look=look)


@server.tool(structured_output=False)
async def wait(seconds: float = 1.0) -> CallToolResult:
    """Wait (0.2-30 s) for something to load or animate, then look."""
    return await _tool("wait", seconds=seconds)


def run() -> None:
    device.run(server.run_stdio_async)

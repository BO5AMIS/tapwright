"""tapwright as an MCP server (stdio): tools an agent uses to test apps on your iPhone.

Every action returns the screen right after it (elements as text, plus a screenshot),
so one call is one step. Targets: `ref` from the last look (e.g. "e12"), `id` (the
accessibility identifier, React Native's testID), `label` (visible text), or `x`,`y`
in points.

The phone belongs to `tapwright serve`, which this server starts when needed; a person
may be watching or driving in its live view at the same time.
"""
import asyncio
import base64
import uuid
from typing import Literal

from mcp.server.mcpserver import Image, MCPServer

from . import client, device

INSTRUCTIONS = (
    "Test apps on the user's real iPhone. Start with list_apps and launch_app, then look. Every action "
    "returns the new screen, so check it before the next step. Prefer `id` targets (accessibility ids, "
    "React Native testIDs), then `label`, then `ref`; use x/y only for things with no element. This is "
    "the user's own phone: don't buy, send, post, delete or change account settings unless the user "
    "asked for exactly that. System alerts (permissions) show up in look; answer them with `alert`. "
    "A person may be watching or driving in the live view. Refusals: `busy` (a person is driving: wait "
    "and retry), `paused` (ask the user to resume), `stale` (look again), `locked` (ask the user to "
    "unlock the phone), `reconnecting` (wait, then look)."
)

server = MCPServer("tapwright", instructions=INSTRUCTIONS)
CLIENT_ID = uuid.uuid4().hex  # this agent's own last look, kept by the daemon


async def _tool(name: str, **args) -> list:
    """Run one tool in the daemon; its answer as MCP content: texts, notes, then the screenshot."""
    try:
        result = await asyncio.to_thread(client.call, name, args, CLIENT_ID)
    except client.DaemonError as e:
        return [f"{e.code}: {e}"]
    except Exception as e:  # the daemon unreachable, a timeout: never crash the server
        return [f"error: {e}"]
    content: list = list(result.get("texts") or []) + [f"note: {n}" for n in result.get("notes") or []]
    if result.get("image"):
        content.append(Image(data=base64.b64decode(result["image"]), format="jpeg"))
    return content or ["(no answer)"]


@server.tool(structured_output=False)
async def look(screenshot: bool = True) -> list:
    """The current screen: every visible element (type, label, id, position) and a screenshot."""
    return await _tool("look", screenshot=screenshot)


@server.tool(structured_output=False)
async def list_apps() -> list:
    """Apps installed on the phone by you (App Store, TestFlight, development builds), with bundle ids."""
    return await _tool("list_apps")


@server.tool(structured_output=False)
async def launch_app(bundle_id: str, look: bool = True) -> list:
    """Open an app by bundle id (from list_apps), bringing it to the front."""
    return await _tool("launch_app", bundle_id=bundle_id, look=look)


@server.tool(structured_output=False)
async def terminate_app(bundle_id: str) -> list:
    """Close an app (to start it fresh next time)."""
    return await _tool("terminate_app", bundle_id=bundle_id)


@server.tool(structured_output=False)
async def tap(ref: str | None = None, id: str | None = None, label: str | None = None,
              x: float | None = None, y: float | None = None, index: int = 0, look: bool = True) -> list:
    """Tap an element: by ref from the last look, accessibility id, visible label, or x/y in points.
    `index` picks among several matches (0 = first)."""
    return await _tool("tap", ref=ref, id=id, label=label, x=x, y=y, index=index, look=look)


@server.tool(structured_output=False)
async def long_press(ref: str | None = None, id: str | None = None, label: str | None = None,
                     x: float | None = None, y: float | None = None, index: int = 0, seconds: float = 1.0,
                     look: bool = True) -> list:
    """Press and hold an element (context menus, drag handles)."""
    return await _tool("long_press", ref=ref, id=id, label=label, x=x, y=y, index=index, seconds=seconds, look=look)


@server.tool(structured_output=False)
async def type_text(text: str, ref: str | None = None, id: str | None = None, label: str | None = None,
                    replace: bool = False, submit: bool = False, look: bool = True) -> list:
    """Type into a field: the target is tapped first to focus it; without a target, into the focused
    field. replace clears the field first; submit presses return afterwards."""
    return await _tool("type_text", text=text, ref=ref, id=id, label=label, replace=replace, submit=submit,
                       look=look)


@server.tool(structured_output=False)
async def scroll(direction: Literal["up", "down", "left", "right"] = "down", amount: float = 0.5,
                 look: bool = True) -> list:
    """Scroll the content: "down" shows what is below. amount is a share of the screen (0.1-0.8)."""
    return await _tool("scroll", direction=direction, amount=amount, look=look)


@server.tool(structured_output=False)
async def swipe(x0: float, y0: float, x1: float, y1: float, seconds: float = 0.35, look: bool = True) -> list:
    """Drag from one point to another, in points (sliders, carousels, swipe actions, edge gestures)."""
    return await _tool("swipe", x0=x0, y0=y0, x1=x1, y1=y1, seconds=seconds, look=look)


@server.tool(structured_output=False)
async def press_button(name: Literal["home", "volumeUp", "volumeDown"], look: bool = True) -> list:
    """Press a hardware button. home leaves the app (it keeps running)."""
    return await _tool("press_button", name=name, look=look)


@server.tool(structured_output=False)
async def alert(action: Literal["accept", "dismiss", "text"], look: bool = True) -> list:
    """Answer the system alert on screen (accept = the default/allow button, dismiss = cancel/don't
    allow), or read its text."""
    return await _tool("alert", action=action, look=look)


@server.tool(structured_output=False)
async def open_url(url: str, look: bool = True) -> list:
    """Open a URL or deep link (myapp://screen) on the phone."""
    return await _tool("open_url", url=url, look=look)


@server.tool(structured_output=False)
async def wait(seconds: float = 1.0) -> list:
    """Wait (0.2-30 s) for something to load or animate, then look."""
    return await _tool("wait", seconds=seconds)


def run() -> None:
    device.run(server.run_stdio_async)

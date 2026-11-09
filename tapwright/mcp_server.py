"""tapwright as an MCP server (stdio): tools an agent uses to test apps on your iPhone.

Every action returns the screen right after it (elements as text, plus a screenshot),
so one call is one step. Targets: `ref` from the last look (e.g. "e12"), `id` (the
accessibility identifier, React Native's testID), `label` (visible text), or `x`,`y`
in points.
"""
import asyncio
from typing import Literal

from mcp.server.mcpserver import Image, MCPServer

from . import device
from .device import Device
from .session import Session
from .ui import NotFound

INSTRUCTIONS = (
    "Test apps on the user's real iPhone. Start with list_apps and launch_app, then look. Every action "
    "returns the new screen, so check it before the next step. Prefer `id` targets (accessibility ids, "
    "React Native testIDs), then `label`, then `ref`; use x/y only for things with no element. This is "
    "the user's own phone: don't buy, send, post, delete or change account settings unless the user "
    "asked for exactly that. System alerts (permissions) show up in look; answer them with `alert`."
)

server = MCPServer("tapwright", instructions=INSTRUCTIONS)
_session: Session | None = None
_options: dict = {}
_lock = asyncio.Lock()


def configure(**options) -> None:
    _options.update(options)


async def session() -> Session:
    global _session
    if _session is None:
        _session = Session(Device(**_options, log=lambda *a: None))
    return _session


async def _look(screenshot: bool = True) -> list:
    text, image = await (await session()).look(screenshot=screenshot)
    return [text] + ([Image(data=image, format="jpeg")] if image else [])


async def _act(coro_factory, look: bool = True, settle: float = 0.6) -> list:
    """Run one action, then return the screen after it settled."""
    async with _lock:
        try:
            result = await coro_factory(await session())
        except NotFound as e:
            return [f"not found: {e}"]
        except Exception as e:  # never crash the server over one failed step
            return [f"error: {e}"]
        if not look:
            return [result]
        await asyncio.sleep(settle)
        try:
            return [result] + await _look()
        except Exception as e:
            return [result, f"(couldn't look afterwards: {e})"]


def _target(ref: str | None, id: str | None, label: str | None, x: float | None, y: float | None,
            index: int) -> dict:
    return {"ref": ref, "id": id, "label": label, "x": x, "y": y, "index": index}


@server.tool(structured_output=False)
async def look(screenshot: bool = True) -> list:
    """The current screen: every visible element (type, label, id, position) and a screenshot."""
    async with _lock:
        try:
            return await _look(screenshot)
        except Exception as e:
            return [f"error: {e}"]


@server.tool(structured_output=False)
async def list_apps() -> list:
    """Apps installed on the phone by you (App Store, TestFlight, development builds), with bundle ids."""
    async with _lock:
        try:
            apps = await (await session()).apps()
        except Exception as e:
            return [f"error: {e}"]
    return ["\n".join(f"{a['bundle_id']}  {a['name']} {a['version']} ({a['build']})" for a in apps) or "no apps"]


@server.tool(structured_output=False)
async def launch_app(bundle_id: str, look: bool = True) -> list:
    """Open an app by bundle id (from list_apps), bringing it to the front."""
    return await _act(lambda s: s.launch(bundle_id), look, settle=2.0)


@server.tool(structured_output=False)
async def terminate_app(bundle_id: str) -> list:
    """Close an app (to start it fresh next time)."""
    return await _act(lambda s: s.terminate(bundle_id), look=False)


@server.tool(structured_output=False)
async def tap(ref: str | None = None, id: str | None = None, label: str | None = None,
              x: float | None = None, y: float | None = None, index: int = 0, look: bool = True) -> list:
    """Tap an element: by ref from the last look, accessibility id, visible label, or x/y in points.
    `index` picks among several matches (0 = first)."""
    return await _act(lambda s: s.tap(**_target(ref, id, label, x, y, index)), look)


@server.tool(structured_output=False)
async def long_press(ref: str | None = None, id: str | None = None, label: str | None = None,
                     x: float | None = None, y: float | None = None, index: int = 0, seconds: float = 1.0,
                     look: bool = True) -> list:
    """Press and hold an element (context menus, drag handles)."""
    return await _act(lambda s: s.tap(hold_ms=int(seconds * 1000), **_target(ref, id, label, x, y, index)), look)


@server.tool(structured_output=False)
async def type_text(text: str, ref: str | None = None, id: str | None = None, label: str | None = None,
                    replace: bool = False, submit: bool = False, look: bool = True) -> list:
    """Type into a field: the target is tapped first to focus it; without a target, into the focused
    field. replace clears the field first; submit presses return afterwards."""
    target = {"ref": ref, "id": id, "label": label, "x": None, "y": None, "index": 0}
    if not any((ref, id, label)):
        target = {}
    return await _act(lambda s: s.type_text(text, replace=replace, submit=submit, **target), look)


@server.tool(structured_output=False)
async def scroll(direction: Literal["up", "down", "left", "right"] = "down", amount: float = 0.5,
                 look: bool = True) -> list:
    """Scroll the content: "down" shows what is below. amount is a share of the screen (0.1-0.8)."""
    return await _act(lambda s: s.scroll(direction, amount), look)


@server.tool(structured_output=False)
async def swipe(x0: float, y0: float, x1: float, y1: float, seconds: float = 0.35, look: bool = True) -> list:
    """Drag from one point to another, in points (sliders, carousels, swipe actions, edge gestures)."""
    return await _act(lambda s: s.swipe(x0, y0, x1, y1, int(seconds * 1000)), look)


@server.tool(structured_output=False)
async def press_button(name: Literal["home", "volumeUp", "volumeDown"], look: bool = True) -> list:
    """Press a hardware button. home leaves the app (it keeps running)."""
    return await _act(lambda s: s.press_button(name), look)


@server.tool(structured_output=False)
async def alert(action: Literal["accept", "dismiss", "text"], look: bool = True) -> list:
    """Answer the system alert on screen (accept = the default/allow button, dismiss = cancel/don't
    allow), or read its text."""
    return await _act(lambda s: s.alert(action), look and action != "text")


@server.tool(structured_output=False)
async def open_url(url: str, look: bool = True) -> list:
    """Open a URL or deep link (myapp://screen) on the phone."""
    return await _act(lambda s: s.open_url(url), look, settle=1.5)


@server.tool(structured_output=False)
async def wait(seconds: float = 1.0) -> list:
    """Wait (0.2-30 s) for something to load or animate, then look."""
    await asyncio.sleep(min(max(seconds, 0.2), 30.0))
    async with _lock:
        try:
            return await _look()
        except Exception as e:
            return [f"error: {e}"]


def run(**options) -> None:
    configure(**options)
    device.run(server.run_stdio_async)

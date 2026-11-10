"""Starting and stopping the phone's Screen Mirroring to this computer, the way the owner would: Control Center,
Screen Mirroring, the receiver's name. The phone's own AirPlay mirror is the best picture Tapwright can get (the
phone's H.264 at full size, 60 frames a second, a frame only when the screen changes), and iOS offers no other way
to start it than these touches.

Each step reads the screen first and stops when it doesn't show what the step expects, so a touch never goes to
something unknown. Measured on an iPhone 14 Pro Max, iOS 26.5 (2026-11-10)."""
import asyncio

from . import ui
from .control import Refused

OPEN_FROM = (30.0, 4.0)  # where the swipe that opens Control Center starts: this far from the right edge, from the top
OPEN_BY = 420.0  # points the finger travels down
STEP = 1.2  # seconds Control Center and its sheets take to settle
CONNECT = 3.0  # seconds the phone takes to start mirroring after the receiver is picked


class MirrorError(Refused):
    """A step didn't find what it needed; nothing was touched from that step on."""

    def __init__(self, message: str):
        super().__init__("unsupported", message)


def control(items: list[ui.Element]) -> ui.Element | None:
    """Control Center's Screen Mirroring control: the button if listed, else the image that carries the name."""
    named = [e for e in items if e.label.strip().lower() == "screen mirroring"]
    return next((e for e in named if "tap" in e.actions), named[0] if named else None)


def receiver(items: list[ui.Element], name: str) -> ui.Element | None:
    """The row of the receiver called `name` in the Screen Mirroring list (exact, ignoring case)."""
    return next((e for e in items if e.label.strip().lower() == name.lower() and "tap" in e.actions), None)


def stop_button(items: list[ui.Element]) -> ui.Element | None:
    return next((e for e in items if e.label.strip().lower() == "stop mirroring" and "tap" in e.actions), None)


def mirroring_to(items: list[ui.Element], name: str) -> bool:
    """Does the Screen Mirroring list show `name` as the one in use?"""
    row = receiver(items, name)
    return row is not None and (row.selected or row.value == "1")


async def _open_list(session) -> list[ui.Element]:
    """Open Control Center and its Screen Mirroring list; the list's elements."""
    width = session.screen[0]
    x = width - OPEN_FROM[0]
    await session.swipe(x, OPEN_FROM[1], x, OPEN_FROM[1] + OPEN_BY, 300)
    await asyncio.sleep(STEP)
    centre = (await session.refresh()).items
    button = control(centre)
    if button is None:
        await session.press_button("home")
        raise MirrorError("Control Center didn't open, or it has no Screen Mirroring control; nothing was changed.")
    await session.tap(ui.Target(ref=button.ref))
    await asyncio.sleep(STEP)
    return (await session.refresh()).items


async def start(session, name: str) -> str:
    """Start mirroring to the receiver called `name`. Leaves the phone on the Home screen: the caller brings its
    app back. Raises MirrorError when the receiver isn't offered (not running, or another network)."""
    rows = await _open_list(session)
    if mirroring_to(rows, name):
        await session.press_button("home")
        return f"Already mirroring to {name}."
    row = receiver(rows, name)
    if row is None:
        await session.press_button("home")
        raise MirrorError(f'the phone doesn\'t offer "{name}" under Screen Mirroring. The phone and this computer '
                          "must be on the same Wi-Fi, and the receiver must be running.")
    await session.tap(ui.Target(ref=row.ref))
    await asyncio.sleep(CONNECT)
    await session.press_button("home")
    return f"Mirroring to {name}."


async def stop(session) -> str:
    """Stop mirroring, if the phone is mirroring. Leaves the phone on the Home screen."""
    rows = await _open_list(session)
    button = stop_button(rows)
    if button is None:
        await session.press_button("home")
        return "The phone wasn't mirroring."
    await session.tap(ui.Target(ref=button.ref))
    await asyncio.sleep(STEP)
    await session.press_button("home")
    return "Stopped mirroring."

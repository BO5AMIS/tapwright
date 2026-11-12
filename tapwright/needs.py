"""What only the phone's owner can clear, said before the work starts instead of at the first failing call (1.3.1):
the XCTest passcode prompt, a locked phone, Developer Mode off, a computer the phone doesn't trust. `doctor` lists
them under "Needs a person:" and the daemon's first answer to each agent carries them as notes. Only what can
be read is listed."""
from __future__ import annotations

from .device import PASSCODE_WORDS

PASSCODE = ("Enter the passcode on the phone (the XCTest prompt is up). Settings > Developer > Enable UI Automation "
            "stops it asking.")
LOCKED = "Unlock the phone."
DEVELOPER_MODE = ("Turn on Developer Mode: Settings > Privacy & Security > Developer Mode, restart the phone and "
                  "confirm.")
NOT_TRUSTED = "Unlock the phone and tap Trust on \"Trust This Computer?\" (enter the passcode if it asks)."


def from_state(state: str | None, detail: str | None) -> list[str]:
    """From the daemon's own state and its detail line."""
    found = []
    if PASSCODE_WORDS in (detail or ""):
        found.append(PASSCODE)
    if state == "locked":
        found.append(LOCKED)
    return found


def from_lockdown(trusted: bool, locked: bool, developer_mode: bool | None) -> list[str]:
    """From what the phone says over USB: trusted, locked (it asked for the passcode), Developer Mode (None: unknown)."""
    if not trusted:
        return [NOT_TRUSTED]
    if locked:
        return [LOCKED]
    return [DEVELOPER_MODE] if developer_mode is False else []

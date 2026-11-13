"""What only the phone's owner can clear, said before the work starts instead of at the first failing call (1.3.1):
the XCTest passcode prompt, a locked phone, Developer Mode off, a computer the phone doesn't trust. `doctor` lists
them under "Needs a person:" and the daemon's first answer to each agent carries them as notes. Only what can
be read is listed."""
from __future__ import annotations

from typing import NamedTuple

from .device import PASSCODE_WORDS

PASSCODE = ("Enter the passcode on the phone (the XCTest prompt is up). Settings > Developer > Enable UI Automation "
            "stops it asking.")
LOCKED = "Unlock the phone."
DEVELOPER_MODE = ("Turn on Developer Mode: Settings > Privacy & Security > Developer Mode, restart the phone and "
                  "confirm.")
NOT_TRUSTED = "Unlock the phone and tap Trust on \"Trust This Computer?\" (enter the passcode if it asks)."


def from_state(state: str | None, detail: str | None, stuck_locked: bool = False) -> list[str]:
    """From the daemon's own state and its detail line. `stuck_locked`: the last connect found the phone locked (the
    state then reads connecting or reconnecting, not locked)."""
    found = []
    if PASSCODE_WORDS in (detail or ""):
        found.append(PASSCODE)
    if state == "locked" or stuck_locked:
        found.append(LOCKED)
    return found


def from_lockdown(trusted: bool, locked: bool, developer_mode: bool | None) -> list[str]:
    """From what the phone says over USB: trusted, locked (it asked for the passcode), Developer Mode (None: unknown)."""
    if not trusted:
        return [NOT_TRUSTED]
    if locked:
        return [LOCKED]
    return [DEVELOPER_MODE] if developer_mode is False else []


class Moment(NamedTuple):
    """A moment a call can't go on without a person (AGENT-TOOLS 1.13 §11.5): what to do, and what it is waiting for."""
    do: str
    until: str


UNLOCK = Moment("unlock the phone", "it's unlocked")
PASSCODE_PROMPT = Moment("enter the passcode on the phone (the XCTest prompt is up; Settings > Developer > Enable UI "
                         "Automation stops it asking)", "it's in")
HEALTH_SHEET = Moment("choose in iOS's Health sheet and tap Allow (iOS doesn't let automation change it)",
                      "the sheet is gone")
IOS_ALERT = Moment("answer iOS's alert on the phone", "it's gone")


def message(moment: Moment) -> str:
    """What the host's dialog says: `Needs a person: unlock the phone. I'll go on by myself when it's unlocked.`"""
    return f"Needs a person: {moment.do}. I'll go on by myself when {moment.until}."


def phone_moment(state: str | None, detail: str | None) -> Moment | None:
    """The moment the daemon's own state says the phone is in: the XCTest passcode prompt, else a locked phone."""
    if PASSCODE_WORDS in (detail or ""):
        return PASSCODE_PROMPT
    return UNLOCK if state == "locked" else None


# ---- `doctor --unattended`: what would stop a run with nobody at the phone ----
LOW_BATTERY = 20  # percent under which a long run may lose the phone
PASSCODE_FIX = ("On a phone kept for testing, set Settings > Display & Brightness > Auto-Lock to Never and unlock it.")
UI_AUTOMATION_FIX = "Settings > Developer > Enable UI Automation: turn it on."


class Facts(NamedTuple):
    """What doctor could find out about a run with nobody there. None: couldn't be read."""
    trusted: bool = True
    locked: bool = False
    developer_mode: bool | None = None
    ui_automation: bool | None = None  # (iOS doesn't say: known only from a passcode prompt that came)
    passcode_given: bool = False  # an accounts file with `phone.passcode` is given (the value is never read out)
    battery: int | None = None


class Stop(NamedTuple):
    what: str
    fix: str


def unattended(f: Facts) -> list[Stop]:
    """What would stop a run without a person, in the order a person would fix it, each with how to fix it."""
    stops: list[Stop] = []
    if not f.trusted:
        stops.append(Stop("This computer isn't trusted by the phone.", NOT_TRUSTED))
    if f.developer_mode is False:
        stops.append(Stop("Developer Mode is off.", DEVELOPER_MODE))
    if f.ui_automation is False:
        stops.append(Stop("Enable UI Automation is off: iOS asks for the passcode before each start, and nothing can "
                          "answer it without a person.", UI_AUTOMATION_FIX))
    elif f.ui_automation is None:
        stops.append(Stop("Enable UI Automation can't be read from the computer: if it's off, iOS asks for the passcode "
                          "before each start, and nothing can answer it without a person.",
                          "Check that it's on. " + UI_AUTOMATION_FIX))
    if f.locked:  # (a given passcode is never typed on the lock screen: it doesn't lift this stop)
        stops.append(Stop("The phone is locked: a call that needs the phone stops at the lock screen.", LOCKED))
    elif not f.passcode_given:
        stops.append(Stop("No passcode is given: if the phone locks during a run (Auto-Lock), nothing can unlock it.",
                          PASSCODE_FIX))
    if f.battery is not None and f.battery < LOW_BATTERY:
        stops.append(Stop(f"The battery is at {f.battery}%: the phone may switch itself off in a long run.",
                          "Charge it, and keep it on a cable during the run."))
    return stops

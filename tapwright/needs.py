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
PASSCODE_FIX = ("Put the phone's passcode in the vault file you give with --accounts (a \"phone\" entry with "
                "\"passcode\"; `tapwright vault protect FILE` encrypts it), so the tool unlocks the phone on its lock "
                "screen; or, on a phone kept for testing, set Settings > Display & Brightness > Auto-Lock to Never.")
UI_AUTOMATION_FIX = "Settings > Developer > Enable UI Automation: turn it on."
RUNNER_FIX = {"reinstall": "Run tapwright doctor --fix with the phone on USB.",
              "setup": "Run tapwright setup with the phone on USB (App Store Connect key, or your .p12 and profile).",
              "missing": "Run tapwright setup with the phone on USB."}
UNREACHABLE_FIX = ("Plug the phone in over USB, or put it on this computer's Wi-Fi and unlock it once, then run "
                   "tapwright probe; for a long run keep it on a cable.")
WAKE_FIX = ("Allow wake timers: Control Panel > Power Options > Change plan settings > Change advanced power settings > "
            "Sleep > Allow wake timers > Enable (on mains power; and on battery for a laptop), or run "
            "`powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1` then `powercfg /setactive SCHEME_CURRENT`.")


class Facts(NamedTuple):
    """What doctor could find out about a run with nobody there. None: couldn't be read (or not checked)."""
    trusted: bool = True
    locked: bool = False
    developer_mode: bool | None = None
    ui_automation: bool | None = None  # (iOS doesn't say: known only from a passcode prompt that came)
    passcode_given: bool = False  # an accounts file with `phone.passcode` is given (the value is never read out)
    battery: int | None = None
    # (1.7, §24.5) what else a long run needs
    runner: str | None = None  # "ok", "reinstall", "setup" or "missing" (repair.decide_wda without the phone); None: not checked
    runner_why: str = ""
    reachable: bool | None = None  # the phone on USB, or the running daemon connected to it
    wake_timers: bool | None = None  # Windows: the power plan lets a scheduled task wake the PC (None: not Windows, unread)
    tasks_missing: tuple[str, ...] = ()  # schedules whose Task Scheduler task is gone
    tasks: int | None = None  # schedules found (None: not checked)
    vault_problem: str | None = None  # the vault file can't be used (its own words; never a value)
    unlock_stopped: bool = False  # the passcode unlock stopped after a refused passcode (unlock.Ledger; review 1.7, H2)


class Stop(NamedTuple):
    what: str
    fix: str


def unattended(f: Facts) -> list[Stop]:
    """What would stop a run without a person, in the order a person would fix it, each with how to fix it."""
    stops: list[Stop] = []
    if f.runner in RUNNER_FIX:
        why = f" ({f.runner_why})" if f.runner_why else ""
        stops.append(Stop(f"The runner (WebDriverAgent) isn't installed and current{why}.", RUNNER_FIX[f.runner]))
    if not f.trusted:
        stops.append(Stop("This computer isn't trusted by the phone.", NOT_TRUSTED))
    if f.reachable is False and f.trusted:
        stops.append(Stop("The phone can't be reached: it isn't on USB, and no running tapwright serve is connected "
                          "to it.", UNREACHABLE_FIX))
    if f.developer_mode is False:
        stops.append(Stop("Developer Mode is off.", DEVELOPER_MODE))
    if f.ui_automation is False:
        stops.append(Stop("Enable UI Automation is off: iOS asks for the passcode before each start, and nothing can "
                          "answer it without a person (the tool never types the passcode into that prompt).",
                          UI_AUTOMATION_FIX))
    elif f.ui_automation is None:
        stops.append(Stop("Enable UI Automation can't be read from the computer: if it's off, iOS asks for the passcode "
                          "before each start, and nothing can answer it without a person (the tool never types the "
                          "passcode into that prompt).", "Check that it's on. " + UI_AUTOMATION_FIX))
    if f.vault_problem is not None:
        stops.append(Stop(f"The vault file can't be used: {f.vault_problem}", "Fix the file (doctor --accounts FILE says "
                                                                               "more), or give another with --accounts."))
    if f.locked and not f.passcode_given:  # (1.7: with phone.passcode the tool unlocks it on the lock screen)
        stops.append(Stop("The phone is locked, and no passcode is given: a call that needs the phone stops at the lock "
                          "screen, and a scheduled run is skipped.", LOCKED + " " + PASSCODE_FIX))
    elif not f.passcode_given:
        stops.append(Stop("No passcode is given: if the phone locks during a run (Auto-Lock), nothing can unlock it, "
                          "and a scheduled run that finds it locked is skipped.", PASSCODE_FIX))
    if f.passcode_given and f.unlock_stopped:
        stops.append(Stop("The passcode unlock stopped after the phone refused the vault's passcode (or its record of "
                          "attempts can't be read): nothing is typed on the lock screen until a person clears it.",
                          "Check that the vault's phone entry holds the phone's passcode, then run tapwright "
                          "unlock-reset."))
    if f.battery is not None and f.battery < LOW_BATTERY:
        stops.append(Stop(f"The battery is at {f.battery}%: the phone may switch itself off in a long run.",
                          "Charge it, and keep it on a cable during the run."))
    if f.wake_timers is False:
        stops.append(Stop("Wake timers are off in this PC's power plan: a scheduled run can't wake a sleeping PC, so it "
                          "runs only while the PC is awake.", WAKE_FIX))
    for name in f.tasks_missing:
        stops.append(Stop(f"The schedule \"{name}\" has no Task Scheduler task: it never runs.",
                          f"Add it again (tapwright schedule add), or remove it: tapwright schedule remove {name}."))
    return stops


def unattended_ready(f: Facts) -> list[str]:
    """What doctor checked and found ready for a long run, one line each (the passcode by presence only)."""
    ready: list[str] = []
    if f.runner == "ok":
        ready.append(f"The runner (WebDriverAgent) is installed and current{f' ({f.runner_why})' if f.runner_why else ''}.")
    if f.trusted and f.reachable:
        ready.append("The phone is paired and reachable.")
    if f.developer_mode is True:
        ready.append("Developer Mode is on.")
    if f.passcode_given and f.vault_problem is None:
        ready.append("The vault gives phone.passcode (never shown): the tool unlocks a locked phone on its lock screen "
                     "at a scheduled run's start, and with tapwright serve --phone --accounts FILE at a new call (never "
                     "in the middle of a run: a lock stops it); at most twice an hour and 3 times a day, and it stops "
                     "after a refusal.")
    if f.battery is not None and f.battery >= LOW_BATTERY:
        ready.append(f"The battery is at {f.battery}%.")
    if f.wake_timers is True:
        ready.append("Wake timers are allowed: the PC wakes for a scheduled run.")
    if f.tasks is not None and not f.tasks_missing:
        ready.append(f"Scheduled tasks: {f.tasks} schedule(s), each with its task." if f.tasks else
                     "Scheduled tasks: no schedules yet (tapwright schedule add).")
    return ready

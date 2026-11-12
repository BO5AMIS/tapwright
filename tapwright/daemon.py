"""The phone side of `tapwright serve`: one Session (tunnel and WebDriverAgent), the control
rules, the stream, the agent tools, a person's input from the live view, and a supervisor
that keeps it all connected. server.py only turns HTTP into calls here."""
import asyncio
import base64
import contextvars
import itertools
import io
import json
import dataclasses
import hashlib
import math
import os
import pathlib
import re
import secrets
import time
import traceback
from contextlib import nullcontext
from typing import NamedTuple

from PIL import Image, ImageChops, UnidentifiedImageError

from . import actionlog, audit, batch, changes, compare, filmstrip as filmlib, ocr, ui
from . import policy as safety
from .actionlog import ActionLog
from . import session as session_module
from . import shot as shotlib
from .control import TAP_MS, Controller, Refused, classify
from .device import (AppNotAnswering, ConnectionLost, DeviceError, NotSetUp, PasscodeNeeded, PhoneLocked, Unanswered,
                     PhoneNotFound)
from .session import Snapshot
from .tapline import AgentTouches
from .stream import (ALIKE_TOLERANCE, NO_IDLE_WAIT, PRESETS, Frame, FrameHub, PictureFeeder, PictureGone, StreamPump,
                     alike, jpeg_of, turn_jpeg)

PROTOCOL = 1
ACCEPTS = ["tap", "longPress", "drag", "scroll", "text", "enter", "backspace", "home", "volume", "quality"]
BUTTONS = ("home", "volumeUp", "volumeDown")
STILL = 0.1  # seconds without a changed frame before a screen counts as still (docs/AGENT-TOOLS.md §6)
RESOLVE_SETTLE = 1.0  # the longest a silent re-read waits for the screen to be still
CAP = 2.0  # the longest an action's answer waits for the screen
LONG_CAP = 4.0  # the same after launching an app or opening a URL
LIVE_AGE = 2.0  # seconds a list stays current for id, label and text targets (docs/AGENT-TOOLS.md §2)
FRESH_FRAME = 2.0  # seconds a stream frame counts as "the screen now"
# the agent tools that act, each written to the action log; reads (look, apps, wait_for, viewer) aren't
LOGGED = frozenset({"tap", "long_press", "type_text", "scroll", "toggle", "back", "swipe", "open_url", "open_app",
                    "close_app", "alert", "press_button", "rotate"})
BATCH_TOOLS = ("run", "record", "replay")  # AGENT-TOOLS §9: they call the other tools
FEW_ELEMENTS = 3  # a look with fewer elements than this says that ocr: true can read the picture
NOTE_FEW = ("this screen has few elements (a game, a canvas or a web view?); look with ocr: true reads the text "
            "in its picture.")
OCR_REF = re.compile(r"o[1-9][0-9]*")
OCR_HEAD = "Text in the picture (OCR; not elements; touch one by its ref, at its centre):"
OCR_NONE = "Text in the picture (OCR): nothing beyond what the elements say."
SWIPE_IS_TAP = 10.0  # points: a swipe moving less than this is a touch where it starts
DISMISS_WORDS = {"cancel", "don't allow", "not now", "close", "no", "later", "dismiss", "ask app not to track"}
FRESH_WAIT = 30.0  # seconds a replay waits after a fresh start for what its first step aims at
BATCH = contextvars.ContextVar("batch", default=None)  # {id, step} of the batch step running now, for the log
# The script a replay is running (§14.3): {"path": Path, "update": bool}. Checks look for references in the script's own
# folder first, a failed check writes its picture beside the script, and with `update` they save there instead.
SCRIPT = contextvars.ContextVar("script", default=None)
REASONED = frozenset({"not_actionable", "not_allowed"})  # codes whose message starts with a reason word
STALE_AREA = 40.0  # points around a point target that must look as they did in the agent's last list (from PhoneUse)
MIN_QUIET = 0.3  # seconds a screen must stay as it was before "No visible change." is believed
NOTE_CAPPED = ("the screen was still changing after {cap:g} s (an animation or video); "
               "positions may be mid-change.")


# The safety policy of the call being run (docs/AGENT-TOOLS.md 11.1): set by run_tool for that call only, so two
# agents with different policies never see each other's.
_POLICY: contextvars.ContextVar[safety.Policy] = contextvars.ContextVar("tapwright_policy", default=safety.DEFAULT)
# Set to a list by run_tool; a touch a person's OK let through puts True in it, and the answer then ends with NOTE_ALLOWED
_GRANTED: contextvars.ContextVar[list[bool]] = contextvars.ContextVar("tapwright_granted", default=[])
# Set to a list by the recording branch of run_tool; a compare_screen call puts what it found there (the folder, the
# bundle id, and the reference file it used), for `_record_compare`. Per call, so another client's comparison can't
# change it.
_COMPARED: contextvars.ContextVar[list["Compared"] | None] = contextvars.ContextVar("tapwright_compared", default=None)
# While a client records: the pictures its recording already compared (the recording's `references`: folder, name,
# source, digest), so `_compare` can refuse a save that would change one before it writes anything.
_BASELINES: contextvars.ContextVar[list[tuple[str, str, pathlib.Path | None, str | None]] | None] = \
    contextvars.ContextVar("tapwright_baselines", default=None)
CONFIRM_TTL = 120.0  # seconds a person has to answer a request, and an allow lasts for one matching call (11.3)
DECLINE_HOLD = 60.0  # seconds after a decline during which the same touch isn't asked again
CONFIRM_KEEP = 600.0  # seconds after that a request is still remembered, to say `They declined.` on a retry
NOTE_ALLOWED = "a person allowed this."
DRY_TOOLS = ("tap", "long_press", "toggle", "type_text", "swipe", "scroll", "back")  # the tools `dry_run` is for


class Scope(NamedTuple):
    """Personal mode: the apps an agent may read and touch, as bundle ids or exact names. Empty: none, so
    nothing runs (it fails closed). A dedicated device has no Scope at all."""
    apps: tuple[str, ...] = ()


NO_APPS = ("no_apps: this agent was started without an app list, so it may not use the phone. Start it with "
           "`tapwright mcp --apps <bundle id>` for the apps under test, or with `--dedicated` on a device "
           "kept for testing.")
SCOPE_FREE = ("open_app", "close_app", "open_url", "press_button")  # how an agent gets back to its app
APP_LINE = re.compile(r"^App: (.+) \(([^()\s]+)\)$", re.M)  # the header line of a full list: name, bundle id


def _outside(bundle: str | None, name: str | None, given: str, alert: bool = False) -> str:
    if alert and bundle == SPRINGBOARD:  # SpringBoard is in front because iOS put up an alert of its own
        return ("outside_apps: an iOS system alert is in front (not a prompt of one of your apps), so nothing of it "
                "is shown or touched. Ask the user to answer it on the phone, then go back with open_app.")
    front = "the phone can't say which app" if not bundle else f"{name} ({bundle})" if name else bundle
    return (f"outside_apps: {front} is in front and isn't one of the apps you were given ({given}), so nothing of it is shown or "
            "touched. Go back with open_app.")


class Settled(NamedTuple):
    seconds: float
    changed: bool  # False only when the stream showed the screen unchanged for certain
    capped: bool  # the cap ended the wait with frames still changing
WAIT_MIN, WAIT_MAX = 0.1, 30.0  # seconds, for `wait`
WAIT_FOR_MIN, WAIT_FOR_DEFAULT = 0.5, 10.0  # `wait_for`'s timeout in seconds (its upper end is WAIT_MAX)
WAIT_FOR_KINDS = ("text", "id", "label")
WAIT_FOR_POLL = 0.3  # seconds between tree reads while waiting
SCROLL_MIN, SCROLL_MAX = 0.1, 1.0  # `scroll`'s amount: a share of the scroll area
MAX_SCROLL_SWIPES = 15  # `scroll to start|end` gives up after this many swipes
SCROLL_CAP = 12.0  # ... or after this many seconds (a spinner or a bounce can keep the screen moving)
SCROLL_TO = {"end": ("down", "right"), "start": ("up", "left")}  # the directions each `to` can mean; the first is the default
SCROLL_TOP_INSET, SCROLL_BOTTOM_INSET = 60.0, 40.0  # points a swipe keeps off the screen's edges when no bar says more
WAIT_FOR_READ_MAX = 5.0  # the longest one tree read in `wait_for` may take
WAIT_FOR_MIN_LEFT = 0.1  # with less time than this left, `wait_for` gives up instead of reading again
WAIT_FOR_FIRST_READ = 3.0  # `wait_for`'s first read of the screen gets at least this long (twice the slowest look allowed)
# The stream's pace. Every frame WebDriverAgent captures costs the phone, mostly in iOS's testmanagerd: about 88% of
# a core at balanced's ~27 fps and 38% at light's ~10 fps (DVT sysmontap, the lab animating, 2026-11-10). So the
# stream runs at its preset's rate while something happens, and at STILL_FPS once nothing has: no agent call for
# the first quiet span (the stream's own linger, so an agent between steps never waits for the rate to rise), no
# person's input for the second, no change on the screen for the third. STILL_FPS keeps the longest gap
# between frames at 0.2 s (V3: at most 0.25 s).
STILL_FPS = 5
PACE_QUIET = (30.0, 2.0, 1.0)  # seconds without an agent call, a person's input, a change on the screen
PACE_TICK = 0.25  # how often the pace is checked
LOOP_TICK, LOOP_HELD, LOOP_SAY_EVERY = 0.05, 0.3, 10.0  # seconds: see Daemon._watch_loop
BACK_STILL_PAUSE, BACK_STILL_TRIES = 0.3, 6  # see Daemon._still_back
TOGGLE_LAND_WAIT, TOGGLE_POLL = 0.5, 0.05  # seconds: see Daemon._toggle_landed
# A switch element this many times wider than tall is a whole row (label and control): toggle touches the control,
# SWITCH_FROM_RIGHT points in from its right end (half an iOS switch, 63 pt wide), not the label in the middle
WIDE_SWITCH, SWITCH_FROM_RIGHT = 3.0, 32.0
NOTE_PERSON = "a person used the phone since your last look."
# a notification banner took a touch near the top (Daemon._taken_away)
NOTE_TAKEN = ("{other} came to the front after the touch: the app opened it, or a notification banner over the top of "
              "the screen took the touch. Went back to {app}. If the app was meant to open it, that worked; if not, "
              "look, then try again.")
NOTE_TAKEN_SYSTEM = ("{other} came to the front after the touch: a notification banner over the top of the screen, or "
                     "a drag from its top edge, can take a touch there. Went back to {app}; the touch may not have "
                     "reached it. Look, then try again.")
SYSTEM_IN_FRONT = "the home screen, Notification Center or Control Center"
RECHECK_WITHIN = 5.0  # seconds: a banner's theft shows within about one; later, the agent or a person moved on
NOTE_KEYBOARD = "closed the keyboard first."
NOTE_RETYPED = "the field didn't hold what was typed (keys went missing), so it was cleared and typed once more."
KEYBOARD_HINT = "type_text into the field with submit: true closes it (it presses return)."
SCROLL_HINT = 'scroll to it first (scroll with this target and to: "target").'  # AGENT-TOOLS v1.6: the one-call way
NOTE_SPAN = "changes are since your last list, {n} actions ago."  # after answers with look: false (v1.6)
# v1.6: `force` let through what the checks would have refused. A real touch doesn't make the hidden element act
NOTE_FORCED = "forced ({reason}): the touch may have landed on what is in front."
KEYBOARD_TOOLS = ("tap", "long_press", "toggle", "type_text")  # the touches that close the keyboard before them
NOTE_COVERED = "something outside the app (a system sheet or browser) may cover it."


def _size(jpeg: bytes | None) -> list[int] | None:
    """[width, height] in pixels of a JPEG, or None when there is no readable picture."""
    if not jpeg:
        return None
    try:
        with Image.open(io.BytesIO(jpeg)) as img:
            return [img.width, img.height]
    except (UnidentifiedImageError, OSError):
        return None


MARK_NAME_MAX = 32  # characters of an element's name in the live view (VIEWER-PROTOCOL 1.4)
CAPTION_MAX = 60  # characters of a step's caption (VIEWER-PROTOCOL 1.4)
BUTTON_CAPTIONS = {"home": "Home", "volumeUp": "Volume up", "volumeDown": "Volume down"}


def element_name(result: str) -> str:
    """The element's name in an action's first answer line ("Tapped e25 button "Equals" @320,780 95x90." →
    `Equals`), cut to MARK_NAME_MAX; "" when it names none. The live view's `label` (VIEWER-PROTOCOL 1.4): the
    whole answer line covered the keys around a touch and ran off the picture (recording the demo, 2026-11-11)."""
    name = re.search(r'"([^"]+)"', result)
    if name is None:
        return ""
    text = name.group(1)
    return text if len(text) <= MARK_NAME_MAX else text[:MARK_NAME_MAX - 1] + "…"


def step_caption(tool: str, result: str, args: dict) -> str | None:
    """The agent's step in plain words for people watching the live view (VIEWER-PROTOCOL 1.4, "Captions"):
    `Tap "Sign up"`, `Type into "Email"`, `Scroll down`, `Open Wayfare`. Never the typed text, a link's address,
    refs or coordinates. None for a step a viewer needn't see."""
    name = element_name(result)
    quoted = f'"{name}"' if name else ""
    if tool in ("tap", "double_tap"):
        text = f"Tap {quoted}"
    elif tool == "long_press":
        text = f"Press and hold {quoted}"
    elif tool == "type_text":
        text = f"Type into {quoted}" if name else "Type"
    elif tool == "scroll":
        to = args.get("to")
        if to == "target" and name:
            text = f"Scroll to {quoted}"
        else:
            text = f"Scroll {args.get('direction') or {'end': 'down', 'start': 'up'}.get(to or '', '')}"
    elif tool == "swipe":
        text = "Swipe"
    elif tool == "toggle":
        text = f"Switch {quoted}"
    elif tool == "back":
        text = "Go back"
    elif tool == "alert":
        text = "Answer the alert"
    elif tool == "open_url":
        text = "Open link"  # never the address: a link can carry codes or tokens
    elif tool in ("open_app", "close_app"):
        app = re.match(r"(?:Opened|Closed) (.+?)(?: \(|\.|,|$)", result)
        text = f"{'Open' if tool == 'open_app' else 'Close'} {app.group(1) if app else 'the app'}"
    elif tool == "rotate":
        text = "Turn the phone"
    elif tool == "press_button":
        text = BUTTON_CAPTIONS.get(str(args.get("name") or ""), "Press a button")
    elif tool == "wait_for":
        return None
    else:
        text = tool.replace("_", " ")
    text = " ".join(text.split())
    return text if len(text) <= CAPTION_MAX else text[:CAPTION_MAX - 1] + "…"


def _answer(lines: list[str], notes: list[str] | None = None, image: bytes | None = None) -> dict:
    """One tool answer (docs/AGENT-TOOLS.md §3): the lines as one text, a `Note: ` line per note,
    and the picture, if any, with its size."""
    text = "\n".join([*lines, *(f"Note: {n}" for n in notes or [])])
    return {"text": text, "image": base64.b64encode(image).decode() if image else None, "image_size": _size(image)}


def _said(text: str, known: list[str]) -> bool:
    """Does an element already say this OCR reading (the same words, or one inside the other: OCR adds an icon
    as a letter: "Q Search destinations")?"""
    t = text.casefold().strip()
    return any(t == k or (len(t) >= 3 and t in k) or (len(k) >= 3 and k in t and len(k) >= 0.6 * len(t)) for k in known)


def settled_line(seconds: float) -> str:
    return f"Settled in {seconds:.2f} s."


def _alert_key(alert: ui.Alert | None) -> tuple[str, tuple[str, ...]] | None:
    return (alert.title, alert.buttons) if alert else None


def _furniture(view, alert: ui.Alert | None, keyboard: bool) -> list[str]:
    """What an alert or the keyboard did since the agent's last answer, as the first lines of a change block."""
    lines = []
    if _alert_key(alert) != view.alert:
        lines.append(ui.alert_line(alert) if alert else "Alert: gone")
    if keyboard != view.keyboard:
        lines.append(f"Keyboard: {'shown' if keyboard else 'hidden'}")
    return lines


def _number(args: dict, key: str) -> float | None:
    """args[key] as a finite float; None when it isn't given. Anything else is bad_params."""
    value = args.get(key)
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = math.nan
    if not math.isfinite(number):
        raise Refused("bad_params", f"{key} must be a number, not {value!r}")
    return number


def _checked(name: str, args: dict) -> dict:
    """The agent's arguments with numbers coerced, or Refused(bad_params) naming the one that's wrong.
    This runs before anything reaches the phone, so a ValueError later is never the agent's fault."""
    checked = dict(args)
    for key in ("x0", "y0", "x1", "y1", "seconds", "amount"):
        if (number := _number(args, key)) is not None:
            checked[key] = number
    if args.get("target") is not None:
        checked["target"] = ui.parse_target(args["target"])  # bad_params, with an example of a target
    if name == "scroll":
        if checked.get("direction") is None:  # null (the MCP server sends every argument) is left out
            checked.pop("direction", None)
        if checked.get("direction", "down") not in ("up", "down", "left", "right"):
            raise Refused("bad_params", f"direction must be up, down, left or right, not {args['direction']!r}")
        if args.get("to") not in (None, "start", "end", "target"):
            raise Refused("bad_params", f"to must be start, end or target, not {args['to']!r}")
        if args.get("to") == "target":
            aim = checked.get("target")
            if aim is None or aim.point is not None:
                raise Refused("bad_params", 'to: "target" scrolls to an element: pass it as target (ref, id, label '
                                            "or text)")
            if "direction" in checked:
                raise Refused("bad_params", 'to: "target" finds the direction itself; leave direction out')
        elif args.get("to") and "direction" in checked and checked["direction"] not in SCROLL_TO[args["to"]]:
            raise Refused("bad_params", f"to {args['to']} scrolls {' or '.join(SCROLL_TO[args['to']])}, not "
                                        f"{args['direction']}; leave direction out, or make them agree")
        if "amount" in checked and not SCROLL_MIN <= checked["amount"] <= SCROLL_MAX:
            raise Refused("bad_params", f"amount must be from {SCROLL_MIN:g} to {SCROLL_MAX:g}, not {args['amount']!r}")
    if name == "type_text":
        if not isinstance(args.get("text"), str):
            raise Refused("bad_params", f"text must be a string, not {args.get('text')!r}")
        for key in ("replace", "submit", "force"):
            if key in args and not isinstance(args[key], bool):
                raise Refused("bad_params", f"{key} must be true or false, not {args[key]!r}")
    if name == "rotate" and args.get("to") not in ("portrait", "landscape_left", "landscape_right"):
        raise Refused("bad_params", f"to must be portrait, landscape_left or landscape_right, not {args.get('to')!r}")
    if name == "wait_for":
        given = [k for k in WAIT_FOR_KINDS if args.get(k) is not None]
        if len(given) != 1:
            raise Refused("bad_params", "wait_for needs exactly one of text, id or label")
        if not isinstance(args[given[0]], str) or not args[given[0]]:
            raise Refused("bad_params", f"{given[0]} must be a non-empty string, not {args[given[0]]!r}")
        if "gone" in args and not isinstance(args["gone"], bool):
            raise Refused("bad_params", f"gone must be true or false, not {args['gone']!r}")
        timeout = args.get("timeout", WAIT_FOR_DEFAULT)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not WAIT_FOR_MIN <= timeout <= WAIT_MAX:
            raise Refused("bad_params", f"timeout must be a number from {WAIT_FOR_MIN:g} to {WAIT_MAX:g}, not {timeout!r}")
    for key in ("look", "screenshot", "labels", "dry_run", "ocr"):
        if key in args and not isinstance(args[key], bool):
            raise Refused("bad_params", f"{key} must be true or false, not {args[key]!r}")
    if name == "filmstrip":  # AGENT-TOOLS §14.4
        for key, (low, high, _) in FILM_LIMITS.items():
            value = args.get(key)
            if value is None:
                checked.pop(key, None)
                continue
            if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                    or value != int(value) or not low <= value <= high):
                raise Refused("bad_params", f"{key} must be a whole number from {low} to {high}, not {value!r}")
            checked[key] = int(value)
        if args.get("tap") is not None:
            ui.parse_target(args["tap"])  # bad_params, with an example of a target
        else:
            checked.pop("tap", None)
    if name == "audit" and args.get("types") is not None:
        types = args["types"]
        if not isinstance(types, list) or not types or any(t not in audit.TYPES for t in types):
            raise Refused("bad_params", f"types must be a list of: {', '.join(audit.TYPES)}; not {types!r}")
    if name == "compare_screen":
        compare.check_params(args)
    if args.get("dry_run") and name not in DRY_TOOLS:
        raise Refused("bad_params", f"dry_run is for {', '.join(DRY_TOOLS)}, not {name}")
    if name == "wait":
        seconds = args.get("seconds")  # required (REQUIRED); a bool is a number to Python, a string isn't one to us
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not WAIT_MIN <= seconds <= WAIT_MAX:
            raise Refused("bad_params", f"seconds must be a number from {WAIT_MIN:g} to {WAIT_MAX:g}, not {seconds!r}")
    if name in ("open_app", "close_app") and (not isinstance(args["app"], str) or not args["app"].strip()):
        raise Refused("bad_params", f"app must be a bundle id or an app's name, not {args['app']!r}")
    if name == "toggle" and args.get("value") is not None and not isinstance(args["value"], bool):
        raise Refused("bad_params", f"value must be true or false, not {args['value']!r}")
    return checked


class DryRun(Refused):
    """A dry run's whole answer (`Would tap ...`), raised where the touch would go out. It is a Refused so the
    controller counts nothing as done: no new screen generation, so the agent's refs stay good."""

    def __init__(self, line: str) -> None:
        super().__init__("dry_run", line)


@dataclasses.dataclass
class Confirm:
    """One request for a person's OK (docs/AGENT-TOOLS.md 11.3) and where it stands: `pending`, then `allowed` or
    `declined`. `until` is the clock time (the controller's) a pending request expires, or an allow lapses."""
    id: str
    tool: str
    key: tuple[str, ...]  # the element: its id, else its role, label and text
    until: float
    state: str = "pending"
    message: dict | None = None  # what viewers were sent, for one that connects while it is open


@dataclasses.dataclass
class Compared:
    """What `Daemon._compare` found: `kind` is saved, matches, differs or missing; `lines` are the answer's;
    `reason` is why a check didn't hold (None when it did, or when a reference was saved)."""
    kind: str
    lines: list[str]
    notes: list[str]
    reason: str | None = None
    image: bytes | None = None
    percent: float | None = None
    folder: str | None = None  # the references folder (bundle id) it was compared in
    source: pathlib.Path | None = None  # the reference file it was compared with, or saved: a script's own, or the store's


def _picture_digest(path: pathlib.Path | None) -> str | None:
    """The hash of a reference file's content, as it is now (None when there is no file, or it can't be read): two
    checks of one name in a recording are the same baseline only when this is the same."""
    if path is None:
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _reference_names(steps: list[dict]) -> set[str]:
    """The reference pictures a script's steps use: a compare_screen step's name, an expect's matches."""
    names = {s["name"] for s in steps if s.get("tool") == "compare_screen" and isinstance(s.get("name"), str)}
    return names | {s["expect"]["matches"] for s in steps if isinstance(s.get("expect"), dict)
                    and isinstance(s["expect"].get("matches"), str)}


def _brief(e: ui.Element) -> str:
    """An element as a dry run names it: `e12 button "Delete trip"`."""
    return " ".join(p for p in (e.ref, e.role, ui.clip(e.text) if e.text else "") if p)


class Noted(str):
    """A handler's result line with a note for the answer's end."""
    note: str = ""


class Unchanged(str):
    """A handler's result that did nothing on the phone (`toggle` to the state it's already in): it is
    the whole answer, with no change block."""


class Toggled(Noted):
    """`toggle`'s result, `Tapped <line>.` until the tree is read: then the first line says what the switch did."""
    ref: str = ""
    before: ui.Element = ui.Element("", "switch")  # the switch as it was tapped
    want: bool | None = None  # the `value` asked for
    flipped: bool | None = None  # where a tap should have taken it

    def verdict(self, now: ui.Element, alert: bool) -> str:
        """The first line from the switch as it reads after the tap. Raises when a wanted value wasn't reached
        (unless an alert is up: it may be asking to confirm)."""
        line = now.line(actions=False, place=False)
        if now.checked == self.flipped:
            return f"Toggled {line}."
        if self.want is not None and not alert:
            raise Refused("not_actionable", f"no_action: {now.line(actions=False)} didn't change")
        return f"Tapped {line}, but it is still checked={'true' if now.checked else 'false'}."


class Typed(Noted):
    """`type_text`'s result, `Typed into <line>.` until the tree is read: then the first line also gives the value."""
    field: ui.Element = ui.Element("", "input")  # the input as it was when typed into

    def same_field(self, now: ui.Element) -> bool:
        """Is `now` the field typed into (the ref may have gone to another input since)? The same ref and
        role, and the same identifier, or without one the same label."""
        old = self.field
        return (now.ref, now.role, now.identifier) == (old.ref, old.role, old.identifier) \
            and (bool(old.identifier) or now.label == old.label)

    def verdict(self, now: ui.Element) -> str:
        line = now.line(actions=False, place=False)
        if now.secure:
            return f"Typed into {line}. The value is hidden (secure field)."
        return f"Typed into {line}. Value is now {json.dumps(now.value, ensure_ascii=False)}."


class Scrolled(Noted):
    """`scroll`'s result, with a note for the answer when it stopped early."""


# What `open_app "Settings"` means: the built-in apps aren't in the installed list.
SYSTEM_APPS = {"Settings": "com.apple.Preferences", "Safari": "com.apple.mobilesafari",
               "Calculator": "com.apple.calculator", "Camera": "com.apple.camera",
               "Photos": "com.apple.mobileslideshow", "Messages": "com.apple.MobileSMS",
               "Mail": "com.apple.mobilemail", "Maps": "com.apple.Maps", "App Store": "com.apple.AppStore",
               "Clock": "com.apple.mobiletimer", "Notes": "com.apple.mobilenotes"}


def app_line(a: dict) -> str:
    return f"{a['bundle_id']}  {a['name']} {a['version']} ({a['build']})"


def _target(a: dict) -> ui.Target:
    return a["target"]  # parsed by _checked; tap and long_press require it


# Agent actions: tool -> (what it does on the session, seconds to settle before the look; None: no look)
ACTIONS = {
    "open_app": (None, LONG_CAP),  # None: the handler is a Daemon method (Daemon._handlers), which needs the agent's list
    "close_app": (None, None),
    "toggle": (None, CAP),
    "back": (None, CAP),
    "tap": (None, CAP),
    "long_press": (None, CAP),
    "type_text": (None, CAP),
    "scroll": (None, CAP),
    "swipe": (lambda s, a: s.swipe(float(a["x0"]), float(a["y0"]), float(a["x1"]), float(a["y1"]),
                                   int(float(a.get("seconds", 0.35)) * 1000)), CAP),
    "press_button": (lambda s, a: s.press_button(a["name"]), CAP),
    "alert": (lambda s, a: s.alert(a["action"]), CAP),
    "open_url": (lambda s, a: s.open_url(a["url"]), LONG_CAP),
    "rotate": (lambda s, a: s.rotate(a["to"]), CAP),
}
SPRINGBOARD = "com.apple.springboard"
PROMPT_FRONT = "(a given app's prompt)"  # Daemon._front: a given app's own system prompt is in front
PROMPT_RETURN_WAIT = 2.0  # seconds the app may take to come back once its prompt is answered
FRONT_RETRIES, FRONT_RETRY = 16, 0.5  # see Daemon._front: as long as a notification banner stays
FRONT_WATCH = 0.15  # s between front-app questions while a filmstrip films in personal mode
MIN_VISIBLE = 10.0  # points: less of an element showing than this (high or wide) is no place to touch
# The status bar across the top of a phone held upright: a touch there scrolls the app's list to its top (iOS),
# whatever row lies under it. It is in no app's tree, so a row half under it looked reachable: a tap on Settings'
# "Notifications" row at y 10 scrolled the list instead (seen on the phone 2026-11-11). 50 pt covers the status
# bar of the current iPhones (47 to 54); in landscape an iPhone shows none.
STATUS_BAR = 50.0
MOVES_CONTENT = ("scroll", "swipe")  # a drag: identical elements can't be told apart after these
# These may show the same screen again, somewhere else in it, or another: the elements say which. Calling each a
# move gave every element without an identifier a new ref, as a `+` and a `-` line for the same thing (seen on
# the phone 2026-11-10: open_url to the screen already open).
MAY_MOVE_CONTENT = ("back", "open_app", "open_url", "press_button", "rotate")
READS = ("look", "apps", "audit", "filmstrip", "compare_screen")
# The scope of the filmstrip call being run: its tap goes through run_tool with it, like any other touch
_FILM_SCOPE: contextvars.ContextVar["Scope | None"] = contextvars.ContextVar("tapwright_film_scope", default=None)
# Personal mode's given apps for the call being run (set by _within): a picture tool checks the front app again after
# its picture is taken, since the app may have changed after the question before the call
_GIVEN: contextvars.ContextVar["dict[str, str] | None"] = contextvars.ContextVar("tapwright_given", default=None)
FILM_LIMITS = {"frames": filmlib.FRAMES, "interval_ms": filmlib.INTERVAL_MS, "frame_width": filmlib.FRAME_WIDTH}
BUNDLE_ID = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
SPECIAL = ("wait", "wait_for")  # sleep before they answer, so they don't hold the phone's queue meanwhile
REQUIRED = {"rotate": ("to",), "open_app": ("app",), "close_app": ("app",), "toggle": ("target",), "wait": ("seconds",),
            "type_text": ("text",), "tap": ("target",), "long_press": ("target",),
            "swipe": ("x0", "y0", "x1", "y1"), "press_button": ("name",), "alert": ("action",),
            "open_url": ("url",), "compare_screen": ("name",)}


async def _first(timeout: float, *events: asyncio.Event) -> None:
    """Wait until any of the events is set, or `timeout` seconds."""
    waits = [asyncio.create_task(e.wait()) for e in events]
    try:
        await asyncio.wait(waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waits:
            w.cancel()


class Daemon:
    def __init__(self, session, control: Controller | None = None, log=print, heartbeat: float = 5.0,
                 backoff: tuple = (1.0, 2.0, 5.0, 10.0), ready_wait: float = 60.0,
                 stream_linger: float = 30.0, stream_retry: float = 1.0, settle_still: float = STILL,
                 settle_first_frame: float = 1.0, settle_fallback: float = 0.6,
                 settle_quiet: float = MIN_QUIET, shot_wait: float = 1.0, shot_fresh: float = 1.0,
                 pace_tick: float | None = PACE_TICK, pace_quiet: tuple[float, float, float] = PACE_QUIET,
                 actions: ActionLog | None = None) -> None:
        self.session = session
        self.actions = actions or ActionLog(None)  # the action log (AGENT-TOOLS §12); off unless the server gives one
        self._announced: dict | None = None  # the action message this agent call announced, for its log line
        self._granted_now = False  # this agent call went through on a person's OK (for its log line)
        self._no_dismiss: set[str] = set()  # apps whose keyboard WebDriverAgent couldn't dismiss (see _dismiss_keyboard_now)
        self._recording: dict[str, dict] = {}  # client id -> the recording in progress (AGENT-TOOLS §9.4)
        # while a client records, its calls run one at a time: a check's guard, its save and its registration in the
        # recording are one step, so two overlapping checks can't both pass the guard (Codex, PR #3 cycle 6)
        self._record_locks: dict[str, asyncio.Lock] = {}
        self._batch_ids = itertools.count(1)
        # the stream's pace (see STILL_FPS): how often it's checked (None: never), and the frame rate the phone
        # streams at now (None until connected)
        self.pace_tick, self.pace_quiet = pace_tick, pace_quiet
        self._fps: int | None = None
        self._person_at = self._changed_at = -math.inf  # monotonic: a person's last input, the screen's last change
        self._pace_seen: Frame | None = None  # the newest frame at the last check
        # screenshots: how long to wait for a stream frame before asking the phone, and how old one may be
        self.shot_wait, self.shot_fresh = shot_wait, shot_fresh
        self.unsettled = False  # the last action returned without a settle: the screen may still be moving, for every client
        # settle: how long a still screen is quiet, how long to wait for a first frame, and the
        # blind sleep used when the stream gives no frames at all
        self.settle_still, self.settle_first_frame, self.settle_fallback = settle_still, settle_first_frame, settle_fallback
        self.settle_quiet = settle_quiet
        self.control = control or Controller()
        self.hub = FrameHub()
        self.pump = StreamPump(self._open_stream, self.hub, linger=stream_linger, retry=stream_retry, log=log,
                               suspended=self._fed_by_video)
        self.log = log
        # A trial switch (TAPWRIGHT_AGENT_STREAM=0): tool calls don't open the JPEG stream, so an action settles
        # on a short wait and a tree read. To learn whether the stream is what stalls requests over Wi-Fi.
        self.agent_stream = os.environ.get("TAPWRIGHT_AGENT_STREAM", "1") != "0"
        self._agent_touches = AgentTouches()
        self._turned: tuple | None = None  # ((frame seq, degrees), the turned JPEG): see turned()
        self.heartbeat, self.backoff, self.ready_wait = heartbeat, backoff, ready_wait
        self.state, self.detail = "connecting", None
        self.preset = os.environ.get("TAPWRIGHT_PRESET", "balanced")  # (a trial switch: the stream's preset at start)
        if self.preset not in PRESETS:
            self.preset = "balanced"
        self.ws_clients = 0
        self.video = None  # a tapwright.video.VideoHub while the H.264 live view is set up (see start_video)
        self.want_video = False  # `tapwright serve --video`: set the video up once the phone is connected
        # `--settle-on-video`: actions watch the video's frames and the JPEG stream stays closed. Off unless asked:
        # a mirror that stalls sends nothing, exactly as a still screen does, so an action could answer "No
        # visible change" for a screen that changed. Not for use until a check against the phone guards that.
        self.settle_on_video = False
        self._receiver = None  # the AirPlay receiver process that feeds `video`
        self._video_task: asyncio.Task | None = None
        self._feeder: PictureFeeder | None = None  # feeds `hub` from `video` while the hub is live (_start_feeder)
        self._feeder_task: asyncio.Task | None = None
        self.last_request = time.monotonic()
        self._ever_connected = False
        # why the last connect couldn't finish, when the phone said: a PhoneLocked, or an AppNotAnswering
        self.stuck: PhoneLocked | AppNotAnswering | None = None
        self._connected = asyncio.Event()
        self._lost = asyncio.Event()
        self._wake = asyncio.Event()  # connect again now, without waiting out the backoff
        self._connecting = False  # a connect is under way
        self._attempts = 0  # connects that ended, either way
        self._scopes: dict[tuple[str, ...], dict[str, str]] = {}  # each app list seen, as {bundle id: shown}
        # {agent: the app its last touch near the top was meant for}, for a look: false answer that asked once
        # {agent: (the app, when, the person generation then)}: see _recheck_later and _check_again
        self._recheck: dict[str, tuple[tuple[str, str], float, int]] = {}
        self._taken_notes: dict[str, str] = {}  # {agent: a note _check_again found for its look}
        self._queues: set = set()
        self._confirms: dict[str, Confirm] = {}  # requests for a person's OK, by id (see _police)
        self._confirm_seq = 0
        self._handlers = {"open_app": self._open_app, "close_app": self._close_app, "toggle": self._toggle,
                          "tap": self._tap, "long_press": self._long_press, "back": self._back, "type_text": self._type_text, "scroll": self._scroll,
                          "alert": self._alert, "swipe": self._swipe}

    # ---- status and events ----
    def status(self) -> dict:
        w, h = self.session.screen
        via = (getattr(self.session.device, "via", None) or "").lower()
        via = ("usb" if via == "usb" else "wifi" if via == "local network"
               else "tailscale" if via.startswith("network") else None)
        return {"type": "status", "protocol": PROTOCOL, "tool": "Tapwright", "state": self.state, "detail": self.detail, "via": via,
                "fps": self.hub.fps(), "driver": self.control.driver, "paused": self.control.paused,
                "size": {"w": w, "h": h}, "quality": self.preset,
                "accepts": [*ACCEPTS, "video"] if self.video is not None and self.video.live else ACCEPTS}

    def subscribe(self) -> asyncio.Queue:
        self._asked_for()
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._queues.discard(queue)

    def broadcast(self, message: dict) -> None:
        """To every live view; one that can't keep up loses its oldest messages."""
        for queue in list(self._queues):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(message)

    def _set_state(self, state: str, detail: str | None = None) -> None:
        changed = (state, detail) != (self.state, self.detail)
        self.state, self.detail = state, detail
        if state in ("live", "locked"):
            self._connected.set()
        else:
            self._connected.clear()
        if changed:
            self.broadcast(self.status())

    def _point(self, point) -> dict | None:
        if not point:
            return None
        w, h = self.session.screen
        return {"x": round(point[0] / w, 4), "y": round(point[1] / h, 4)}

    def _announce(self, by: str, kind: str, label: str | None = None, point=None, ok: bool = True,
                  code: str | None = None, caption: str | None = None, reason: str | None = None) -> None:
        message = {"type": "action", "by": by, "kind": kind, "at": int(time.time() * 1000), "ok": ok}
        if label:
            message["label"] = label
        if caption:
            message["caption"] = caption
        if reason:
            message["reason"] = reason
        if point:
            message["point"] = point
        if code:
            message["code"] = code
        self.broadcast(message)
        if by == "person":
            self.actions.write({"source": "tapwright", "by": "person", "tool": kind, "element": label, "ok": ok,
                                "code": code, "app": self._bundle()})
        else:
            self._announced = message

    # ---- the connection ----
    def lost(self, reason: "str | Exception") -> None:
        """A request found the line dead: the supervisor reconnects. When the request was cut because the app in
        front doesn't answer (AppNotAnswering), that is what agents and the live view are told meanwhile."""
        stuck = reason if isinstance(reason, AppNotAnswering) else None
        if stuck is not None:
            self.stuck = stuck
        if self.state in ("live", "locked"):
            self.log(f"connection lost: {reason}")
            self._set_state("not-answering" if stuck is not None else "reconnecting", str(reason)[:200])
        for view in self.control.clients.values():
            view.dropped = True  # every agent's list is from before the drop
        self._lost.set()

    def _not_connected(self) -> Refused:
        """No phone found or not set up: the agent can't fix that by waiting. Otherwise the link
        is coming back."""
        if self.state == "no-device":
            return Refused("not_connected", f"no phone to drive ({self.detail}); fix that, then try again")
        if isinstance(self.stuck, PasscodeNeeded):
            return Refused("locked", "the phone is asking for its passcode to allow UI automation; ask the user "
                                     "to enter it on the phone")
        if isinstance(self.stuck, PhoneLocked):
            return Refused("locked", "the phone is locked; ask the user to unlock it")
        if isinstance(self.stuck, AppNotAnswering):
            return Refused("not_answering", f"{self.stuck}. close_app can still close it; then open_app it again.")
        return Refused("reconnecting", f"not connected to the phone ({self.detail or self.state}); "
                                       "wait a few seconds, then look")

    def _require_connected(self) -> None:
        if not self._connected.is_set():
            raise self._not_connected()

    async def _connected_only(self, fn):
        """Run `fn` only if the line is up when its turn comes: a reconnect holds the queue,
        and whatever waited for it must not reconnect on its own."""
        self._require_connected()
        return await fn()

    def _asked_for(self) -> bool:
        """A tool call or a viewer wants the phone while it waits behind the passcode screen: put that screen up
        now if it has gone (the starts thin out when nobody asks, see device.PROMPT_PAUSES). True when a start
        was asked for."""
        if isinstance(self.stuck, PasscodeNeeded):
            hurry = getattr(self.session.device, "hurry", None)
            if hurry is not None:
                hurry()
                self._wake.set()
                return True
        return False

    async def _next_start(self) -> None:
        """Wait for a connect that began after now to end (or for the line to come up). What the last start
        found may be minutes old: the person may have typed the passcode since, with nothing started to notice
        (seen on the phone 2026-11-10: a tap answered `locked` 20 s before the phone was back). A connect
        already under way may have looked before it was asked to hurry, so the one after it counts."""
        need = self._attempts + (2 if self._connecting else 1)
        deadline = time.monotonic() + self.ready_wait
        while self._attempts < need and not self._connected.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

    async def wait_connected(self) -> None:
        if self._asked_for():
            await self._next_start()
        if self.state == "no-device" or self.stuck is not None:  # waiting won't fix either
            raise self._not_connected()
        try:
            await asyncio.wait_for(self._connected.wait(), self.ready_wait)
        except asyncio.TimeoutError:
            raise self._not_connected() from None

    async def _connect(self) -> None:
        """A working WebDriverAgent session: a new WebDriverAgent if the tunnel still works,
        otherwise everything from scratch."""
        async with self.control.exclusive():
            s = self.session
            if s.device.rsd is not None:
                s.sid = None
                try:
                    await s.ensure()
                except (NotSetUp, PhoneLocked, AppNotAnswering):  # the tunnel is fine: rebuilding it won't help
                    raise
                except Exception as e:
                    self.log(f"restarting WebDriverAgent didn't work ({e}); reconnecting")
                    await s.reset()
                    await s.ensure()
            else:
                await s.ensure()
            await s.settings({**PRESETS[self.preset], **NO_IDLE_WAIT})
            self._fps = self._preset_fps()
            await s.read_scale(refresh=True)  # once per connection, so every header says @3x, not only SpringBoard's
            await s.read_app_names(refresh=True)  # so the header's App: line says "Settings", not a bundle id

    async def supervise(self, stop: asyncio.Event) -> None:
        """Connect, then keep checking; reconnect whenever the line drops. Runs until `stop`."""
        self._agent_touches.start()  # find Tapline's hosts, if any, before the first touch needs them
        pacer = asyncio.create_task(self._pace(stop), name="pace") if self.pace_tick else None
        watch = asyncio.create_task(self._watch_loop(stop), name="loop-watch") if self.pace_tick else None
        try:
            await self._supervise(stop)
        finally:
            for helper in (pacer, watch):
                if helper is not None:
                    helper.cancel()
                    await asyncio.gather(helper, return_exceptions=True)

    async def _watch_loop(self, stop: asyncio.Event) -> None:
        """Say in the log when this process's event loop was held up. The tunnel to the phone is a TCP/IP stack
        in pure Python on this same loop: while something holds the loop, no packet moves, and a held loop looks
        from outside like a phone that doesn't answer."""
        loop = asyncio.get_running_loop()
        worst, told = 0.0, loop.time()
        while not stop.is_set():
            before = loop.time()
            await asyncio.sleep(LOOP_TICK)
            worst = max(worst, loop.time() - before - LOOP_TICK)
            if worst >= LOOP_HELD and loop.time() - told >= LOOP_SAY_EVERY:
                self.log(f"the event loop was held for {worst:.2f} s; the tunnel's packets waited meanwhile")
                worst, told = 0.0, loop.time()

    # ---- the stream's pace ----
    def _preset_fps(self) -> int:
        return int(PRESETS[self.preset]["mjpegServerFramerate"])

    async def _set_fps(self, fps: int) -> None:
        await self.session.settings({"mjpegServerFramerate": fps})
        self._fps = fps

    async def _quicken(self) -> None:
        """Back to the preset's rate before an action, so its settle (and the person's picture) has every frame.
        Only after a quiet spell; a failure to change the rate never stops the action."""
        if self._fps is not None and self._fps < self._preset_fps() and not self._fed_by_video():
            try:
                await self._set_fps(self._preset_fps())
            except ConnectionLost:
                raise
            except DeviceError as e:
                self.log(f"pace: couldn't raise the frame rate: {e}")

    def _fed_by_video(self) -> bool:
        """Is the AirPlay video the hub's source now (so the MJPEG stream stays closed)?"""
        return self._feeder is not None and self._feeder.feeding

    async def _pace(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await _first(self.pace_tick, stop)
            if stop.is_set():
                return
            try:
                await self._pace_once()
            except ConnectionLost as e:
                self.lost(e)
            except Refused:
                pass  # not connected, or paused: the next check tries again
            except Exception as e:
                self.log(f"pace: {e}")

    async def _pace_once(self) -> None:
        """Slow the stream when nothing has happened for a while, and quicken it when the screen changes."""
        if self._fed_by_video():
            return  # the MJPEG stream is closed: there is no frame rate to set
        now = time.monotonic()
        frame = self.hub.latest
        if frame is not None and frame is not self._pace_seen:
            if not alike(frame, self._pace_seen):  # a first frame counts as a change: nothing says it's still yet
                self._changed_at = now
            self._pace_seen = frame
        if self.state != "live" or self._fps is None or frame is None or self.hub.fps() == 0:
            return  # nothing streams, so the pace costs nothing
        agent, person, still = self.pace_quiet
        busy = (now - self.last_request < agent or now - self._person_at < person or now - self._changed_at < still)
        want = self._preset_fps() if busy else min(STILL_FPS, self._preset_fps())
        if want != self._fps:
            await self.control.read(lambda: self._connected_only(lambda: self._set_fps(want)))

    async def _supervise(self, stop: asyncio.Event) -> None:
        attempt = failures = 0
        while not stop.is_set():
            if self.state not in ("live", "locked"):
                self._connecting = True
                try:
                    try:
                        await self._connect()
                    finally:
                        self._connecting = False
                        self._attempts += 1
                except NotSetUp as e:
                    self._set_state("no-device", f"needs setup: {e}")
                except PhoneNotFound:
                    self._set_state("no-device", "phone not found: plug it in and unlock it")
                except (PhoneLocked, AppNotAnswering) as e:
                    if self.stuck is None:
                        self.log(f"connected, but {e}")
                    self.stuck = e
                    state = "reconnecting" if self._ever_connected else "connecting"
                    self._set_state("not-answering" if isinstance(e, AppNotAnswering) else state, str(e)[:200])
                except Exception as e:
                    self.stuck = None
                    self._set_state("reconnecting" if self._ever_connected else "connecting", str(e)[:200])
                else:
                    attempt = failures = 0
                    self.stuck = None
                    self._ever_connected = True
                    self._lost.clear()  # a drop noticed while reconnecting is already dealt with
                    self._set_state("live")
                    self.log(f"connected over {self.session.device.via}")
                    if self.want_video and self._video_task is None:
                        self._video_task = asyncio.create_task(self.start_video(), name="video")
                    continue
                await _first(self.backoff[min(attempt, len(self.backoff) - 1)], stop, self._wake)
                self._wake.clear()
                attempt += 1
                continue
            await _first(self.heartbeat, stop, self._lost)
            if stop.is_set():
                break
            if self._lost.is_set():
                self._lost.clear()
                if self.state in ("live", "locked"):
                    self._set_state("reconnecting", "connection lost")
                continue
            try:
                locked = await self.control.read(lambda: self._connected_only(self.session.locked))
            except ConnectionLost as e:
                self.lost(e)
            except Refused:
                pass
            except Exception as e:
                failures += 1
                self.log(f"heartbeat: {e}")
                if failures >= 3:
                    failures = 0
                    self.lost(f"WebDriverAgent keeps failing: {e}")
            else:
                failures = 0
                if not self._lost.is_set() and self.state in ("live", "locked"):
                    if locked:
                        self._set_state("locked", "unlock the phone")
                    else:
                        self._set_state("live")

    # ---- agent tools ----
    async def run_tool(self, client_id: str, name: str, args: dict, scope: "Scope | None" = None,
                       policy: "safety.Policy | None" = None) -> dict:
        """One agent tool call: {"text": str, "image": base64 JPEG or None, "image_size": [w, h] or None}.
        Raises Refused with a code from errors.CODES; any other failure is `internal` (logged).
        `scope` is the apps this agent was given (personal mode); None is a dedicated device: no limit.
        `policy` is its safety policy (docs/AGENT-TOOLS.md 11.1); None is the built-in one."""
        self.last_request = time.monotonic()
        # the policy holds for the whole call, a batch's steps included (they call run_tool without one)
        ptoken = _POLICY.set(policy) if policy is not None else None
        # a recording's earlier pictures hold for the whole call too: a save that would change one is refused first
        btoken = _BASELINES.set(self._recording[client_id].setdefault("references", [])) \
            if client_id in self._recording and _BASELINES.get() is None else None
        try:
            if btoken is not None:  # the outer call of a recording client (a batch's steps are inside it)
                async with self._record_locks.setdefault(client_id, asyncio.Lock()):
                    return await self._run_tool_policed(client_id, name, args, scope)
            return await self._run_tool_policed(client_id, name, args, scope)
        finally:
            if ptoken is not None:
                _POLICY.reset(ptoken)
            if btoken is not None:
                _BASELINES.reset(btoken)

    async def _run_tool_policed(self, client_id: str, name: str, args: dict, scope: "Scope | None") -> dict:
        if name in BATCH_TOOLS:
            if scope is None:
                return await self._batch_tool(client_id, name, args, scope)
            return await self._batch_within(scope, client_id, name, args)
        if name == "filmstrip":
            return await self._filmstrip_logged(client_id, args, scope)
        if name == "wait_for" and client_id in self._recording:
            answer = await self._run_tool_inner(client_id, name, args, scope)
            self._record_expect(client_id, args)
            return answer
        if name == "compare_screen" and client_id in self._recording:
            ignore = self._recorded_ignore(client_id, args)  # before the call: a ref that can't be saved is refused first
            compared: list[Compared] = []
            token = _COMPARED.set(compared)
            try:
                answer = await self._run_tool_inner(client_id, name, args, scope)
            finally:
                _COMPARED.reset(token)
            # a step of a run stays the step the agent wrote; a call of its own is folded into the action before it
            self._record_compare(client_id, args, ignore, compared[0].folder if compared else None,
                                 compared[0].source if compared else None, fold=BATCH.get() is None)
            return answer
        if name in LOGGED:
            started, self._announced, self._granted_now = time.monotonic(), None, False
            aimed = self._aim_for_recording(client_id, name, args)
            try:
                result = await self._run_tool_inner(client_id, name, args, scope)
            except Refused as e:
                self._log_action(name, args, started, e)
                raise
            self._log_action(name, args, started, None)
            self._record_step(client_id, name, args, aimed)
            return result
        return await self._run_tool_inner(client_id, name, args, scope)

    def _log_action(self, name: str, args: dict, started: float, error: Refused | None,
                    frames: int | None = None) -> None:
        """The agent's action, one line in the action log. Typed text only as its length (AGENT-TOOLS §12)."""
        told = self._announced or {}
        message = str(error) if error is not None else ""
        reason = message.split(":", 1)[0] if error is not None and error.code in REASONED else None
        text = args.get("text")
        self.actions.write({
            "source": "tapwright", "by": "agent", "tool": name, "target": actionlog.target_of(args.get("target")),
            "element": told.get("label"), "ok": error is None, "code": error.code if error is not None else None,
            "reason": reason, "ms": round((time.monotonic() - started) * 1000),
            "app": self._bundle(), "chars": len(text) if isinstance(text, str) else None, "frames": frames,
            "batch": BATCH.get(), "confirmed": True if self._granted_now else None})

    def _bundle(self) -> str | None:
        """The bundle id of the app in front at the last read, for the log and a recording."""
        app = getattr(self.session, "app", None)
        return app[1] if isinstance(app, tuple) and len(app) == 2 else None

    # ---- batches and scripts (AGENT-TOOLS §9) ----
    def _aim_for_recording(self, client_id: str, name: str, args: dict) -> dict | None:
        """While this agent records: the target its step will be saved with, worked out before the call (a ref names
        an element of the list the agent has now)."""
        if client_id not in self._recording or name not in batch.STEP_TOOLS:
            return None
        items = self.control.client(client_id).items
        if name == "type_text":  # is the field secure? Its text then never goes into the file (safety review M4)
            field = None
            try:
                found = ui.resolve(items, ui.parse_target(args["target"])) if args.get("target") else None
                field = found if isinstance(found, ui.Element) else None
            except Refused:
                pass
            if field is None and not args.get("target"):
                inputs = [e for e in items if e.role == "input"]
                field = inputs[0] if len(inputs) == 1 else None
            self._recording[client_id]["next_secure"] = bool(field is not None and field.secure)
        return batch.recorded_target(args.get("target"), items)

    def _record_expect(self, client_id: str, args: dict) -> None:
        """While recording: a `wait_for` that held right after an action becomes that step's `expect`."""
        rec = self._recording[client_id]
        kind = next((k for k in batch.EXPECT_KINDS if args.get(k) is not None), None)
        if not rec["steps"] or kind is None or "expect" in rec["steps"][-1] or rec["steps"][-1]["tool"] == "compare_screen":
            return
        expect: dict = {kind: args[kind]}
        if args.get("gone"):
            expect["gone"] = True
        if args.get("timeout") is not None:
            expect["timeout"] = min(max(float(args["timeout"]), batch.EXPECT_MIN), batch.EXPECT_MAX)
        rec["steps"][-1]["expect"] = expect

    def _recorded_ignore(self, client_id: str, args: dict) -> list[dict] | None:
        """While this agent records: the `ignore` list its step will be saved with, worked out before the call."""
        if not isinstance(args.get("ignore"), list):
            return None
        return batch.recorded_ignore(args["ignore"], self.control.client(client_id).items)

    def _record_compare(self, client_id: str, args: dict, ignore: list[dict] | None, folder: str | None,
                        source: pathlib.Path | None,
                        fold: bool = True) -> None:
        """While recording: a `compare_screen` right after an action becomes that step's `expect.matches` (beside the
        text, id or label already expected, if any), when `fold`; otherwise it is a step of its own. Its `name` and
        the `folder` (bundle id) it was compared in and the `source` file it was compared with (a replayed script's
        own reference, say, not the store's of the same name) are remembered, so `stop` can copy that reference next
        to the script (§14.3). A script keeps one picture per name: the same name in another app is refused, and so is
        the same name against another picture (the content of the file, as it is now) in the same app."""
        rec = self._recording[client_id]
        folder = folder or self._bundle() or "unknown"
        digest = _picture_digest(source)
        for bundle, seen, _, before in rec.get("references", ()):
            if seen != args["name"]:
                continue
            if bundle != folder:
                raise Refused("bad_params", f'this recording already compares "{args["name"]}" in {bundle}, and a script '
                                            f"keeps one picture for each name; give the one in {folder} another name")
            if digest is not None and before is not None and digest != before:
                raise Refused("bad_params", f'record: "{args["name"]}" was already compared against another picture in '
                                            "this recording; give this check another name.")
        extra: dict = {}
        if args.get("threshold") not in (None, compare.DEFAULT_THRESHOLD):  # (the default isn't written down)
            extra["threshold"] = args["threshold"]
        if ignore:
            extra["ignore"] = ignore
        rec.setdefault("references", []).append((folder, args["name"], source, digest))
        last = rec["steps"][-1] if fold and rec["steps"] else None
        if last is not None and last["tool"] != "compare_screen" and "matches" not in last.get("expect", {}):
            last["expect"] = {**last.get("expect", {}), "matches": args["name"], **extra}
            return
        step = {"tool": "compare_screen", "name": args["name"], **extra}
        if args.get("width") not in (None, shotlib.DEFAULT_WIDTH):
            step["width"] = int(args["width"])  # (a whole-number float passed the check; a script wants an int)
        rec["steps"].append(step)

    def _record_step(self, client_id: str, name: str, args: dict, aimed: dict | None) -> None:
        """An action that succeeded while this agent records becomes a step of its script."""
        rec = self._recording.get(client_id)
        if rec is None or name not in batch.STEP_TOOLS:
            return
        step = {"tool": name, **{k: v for k, v in args.items() if k not in ("target", "look", "screenshot")}}
        if name == "type_text" and rec.pop("next_secure", False):
            # a password is never written to a file (safety review M4): the user puts a test password in the script
            step["text"] = ""
            step["secure"] = True
            rec.setdefault("secure_steps", []).append(len(rec["steps"]) + 1)
        if aimed is not None:
            step["target"] = aimed
        elif "target" in args:
            return  # a target that can't be saved (a ref gone stale): leave the step out rather than guess
        rec["steps"].append(step)
        rec["app"] = rec["app"] or self._bundle()

    async def _batch_tool(self, client_id: str, name: str, args: dict, scope: "Scope | None") -> dict:
        """`run`, `record` and `replay`."""
        for key in ("trace", "look", "screenshot", "start", "stop", "dry_run", "update_references"):
            if key in args and not isinstance(args[key], bool):
                raise Refused("bad_params", f"{key} must be true or false, not {args[key]!r}")
        if args.get("update_references") and name != "replay":
            raise Refused("bad_params", "update_references is for replay")
        if name == "record":
            return self._record(client_id, args)
        if name == "run":
            steps = batch.check_steps(args.get("steps"))
            if args.get("dry_run"):
                return await self._dry_batch(client_id, steps, scope)
            return await self._batch(client_id, steps, scope, args)
        for key in ("name", "path"):
            if args.get(key) is not None and (not isinstance(args[key], str) or not args[key]):
                raise Refused("bad_params", f"{key} must be a non-empty string, not {args[key]!r}")
        if not args.get("name") and not args.get("path"):
            names = batch.saved()
            return _answer([f"Saved scripts: {', '.join(names)}." if names else
                            "No saved scripts yet: record one with record start: true, then stop: true."])
        path = batch.resolve(args.get("name"), args.get("path"))
        data = batch.load_script(path)
        token = SCRIPT.set({"path": pathlib.Path(path), "update": bool(args.get("update_references"))})
        try:
            return await self._replay(client_id, data, scope, args)
        finally:
            SCRIPT.reset(token)

    async def _replay(self, client_id: str, data: dict, scope: "Scope | None", args: dict) -> dict:
        """A loaded script's steps, after its fresh start (the SCRIPT context is set by the caller)."""
        if data.get("fresh", True) and data.get("app"):
            # a known start: the app closed and opened again (Tapline reloads its JS instead)
            await self.run_tool(client_id, "close_app", {"app": data["app"]}, scope)
            await self.run_tool(client_id, "open_app", {"app": data["app"], "look": False}, scope)
            # a fresh start takes the app a while (a development build loads its bundle): wait for what step 1 aims
            # at, up to FRESH_WAIT. If it never comes, step 1 says not_found, which is the honest answer.
            aim = data["steps"][0].get("target")
            kind = next((k for k in ("id", "label", "text") if isinstance(aim, dict) and aim.get(k)), None)
            if kind is not None:
                try:
                    await self.run_tool(client_id, "wait_for", {kind: aim[kind], "timeout": FRESH_WAIT, "look": False},
                                        scope)
                except Refused as e:
                    if e.code != "timeout":
                        raise
        points = [n for n, step in enumerate(data["steps"], 1)
                  if isinstance(step.get("target"), dict) and step["target"].get("x") is not None]
        notes = [f"Note (step {n}): a point target; it breaks when the layout changes." for n in points]
        if data.get("fresh", True) and data.get("app"):
            notes.append("Note: the app was closed and opened again first (the script's \"fresh\").")
        return await self._batch(client_id, data["steps"], scope, args, notes)

    async def _dry_batch(self, client_id: str, steps: list[dict], scope: "Scope | None") -> dict:
        """`run` with dry_run (§11.4): every step's parameters are checked already; step 1 is resolved and checked like a
        dry run of its own, and nothing touches the phone. Later steps depend on what step 1 does, so they aren't."""
        first, n = steps[0], len(steps)
        if first["tool"] in DRY_TOOLS:
            answer = await self.run_tool(client_id, first["tool"], {**batch.args_of(first), "dry_run": True}, scope)
            would = answer["text"].splitlines()[0]
            what = "w" + would[1:] if would.startswith("Would ") else would
        else:
            what = f"would {batch.summary(first)}."
        return _answer([f"Would run {n} step{'s' if n != 1 else ''}; step 1 {what}"])

    def _record(self, client_id: str, args: dict) -> dict:
        if args.get("start") and args.get("stop"):
            raise Refused("bad_params", "record takes start: true or stop: true, not both")
        if args.get("start"):
            name = args.get("name") or "recording"
            if not isinstance(name, str):
                raise Refused("bad_params", f"name must be a string, not {name!r}")
            self._recording[client_id] = {"name": name, "app": None, "steps": []}
            return _answer(["Recording: every action that succeeds from now on is saved as a step. Call record with "
                            "stop: true to save the script (under its name, or to a path)."])
        if args.get("stop"):
            rec = self._recording.get(client_id)
            if rec is None:
                raise Refused("bad_params", "nothing is being recorded; start with record start: true")
            path = args.get("path")
            if path is not None and (not isinstance(path, str) or not path):
                raise Refused("bad_params", f"path must be a file path, not {path!r}")
            path = str(batch.writable(path or str(batch.named(rec["name"]))))
            try:
                pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
                pathlib.Path(path).write_text(json.dumps(batch.script(rec["name"], rec["app"], rec["steps"]),
                                                         ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            except OSError as e:
                raise Refused("bad_params", f"can't save the script to {path}: {e.strerror or e}") from None
            del self._recording[client_id]
            # the references the steps use travel with the script (§14.3), as the app they were saved for last had them
            wanted = _reference_names(rec["steps"])
            bundles = {name: bundle for bundle, name, _, _ in rec.get("references", ()) if name in wanted}
            sources = {name: source for _, name, source, _ in rec.get("references", ()) if name in wanted and source}
            folder = compare.script_references(pathlib.Path(path))
            secure = rec.get("secure_steps") or []
            note = ([f"Step{'s' if len(secure) > 1 else ''} {', '.join(map(str, secure))} typed into a secure field: "
                     "the text isn't saved. Put a test password in the file's \"text\" before replaying."]
                    if secure else [])
            try:  # the script is saved already: a copy that fails is said, not an error that loses the answer
                copied = sum(compare.copy_to_script([name], bundle, pathlib.Path(path), sources)[1]  # (small files)
                             for name, bundle in bundles.items())
            except OSError as e:
                copied = 0
                note.append(f"the reference pictures couldn't be copied to {folder}: {e.strerror or e}; copy them from "
                            f"{compare.references_dir()} by hand, or replay with --update-references.")
            pictures = f", and {copied} reference picture{'s' if copied != 1 else ''} to {folder}" if copied else ""
            return _answer([f"Saved {len(rec['steps'])} steps to {path}{pictures}."], note)
        raise Refused("bad_params", "record takes start: true (with an optional name), or stop: true (with an optional "
                                    "path)")

    async def _batch(self, client_id: str, steps: list[dict], scope: "Scope | None", args: dict,
                     notes: list[str] | None = None) -> dict:
        """Run the steps in order through the ordinary tools; stop at the first that fails or whose expectation
        doesn't hold. One answer for the whole batch: its first line, then what changed since the list the agent
        had before the call, then each step's notes (AGENT-TOOLS §9.3)."""
        view, clock = self.control.client(client_id), self.control.clock
        began, n, ident = clock(), len(steps), f"b{next(self._batch_ids)}"
        traced: list[str] = []
        step_notes = list(notes or [])
        failed: tuple[str, str] | None = None
        done = 0
        for i, step in enumerate(steps, 1):
            if clock() - began > batch.BATCH_LIMIT:
                failed = ("timeout", f"Ran {done} of {n} steps; stopped at step {i} ({batch.summary(step)}): the "
                                     f"batch took longer than {batch.BATCH_LIMIT:g} s.")
                break
            t0, token = clock(), BATCH.set({"id": ident, "step": i})
            before = len(self._recording[client_id]["steps"]) if client_id in self._recording else None
            try:
                answer = await self.run_tool(client_id, step["tool"], batch.args_of(step), scope)
                recorded = before is not None and len(self._recording[client_id]["steps"]) > before
                lines = answer["text"].splitlines()
                step_notes += [f"Note (step {i}): {line[6:]}" for line in lines if line.startswith("Note: ")]
                if args.get("trace"):
                    traced.append(f"  {i}. {lines[0] if lines else step['tool']} ({clock() - t0:.1f} s)")
                seen_notes: list[str] = []
                missed = (await self._expect(client_id, view, scope, step["expect"], seen_notes, fold=recorded)
                          if "expect" in step else None)
                step_notes += [f"Note (step {i}): {note}" for note in seen_notes]
            except Refused as e:
                failed = (e.code, f"Ran {done} of {n} steps; stopped at step {i} ({batch.summary(step)}): {e}")
                break
            finally:
                BATCH.reset(token)
            if missed is not None:
                failed = ("expect_failed", f"Ran {done} of {n} steps; stopped at step {i} ({batch.summary(step)}): "
                                           f"its action ran, but {missed}")
                break
            done += 1
        first = failed[1] if failed else f"Ran {n} of {n} steps in {clock() - began:.1f} s."
        if args.get("look", True):
            view.unlooked = 0  # the block covers the batch: one call, not "N actions ago"
            full = view.seen != self.control.generation or view.dropped or view.read_at == float("-inf")
            answer = await self._after(view, "\n".join([first, *traced]), [], full, moves_content=None)
        else:
            view.unlooked += 1
            answer = _answer([first, *traced])
        answer["text"] = "\n".join([answer["text"], *step_notes])
        if failed:
            raise Refused(failed[0], answer["text"])
        return await self._add_shot(answer, view, self._shot_args(args, default=False, custom=False))

    async def _expect(self, client_id: str, view, scope: "Scope | None", expect: dict,
                      notes: list[str] | None = None, fold: bool = False) -> str | None:
        """Does the step's expectation hold (§9.2)? None when it did, else why not. A text, id or label is waited
        for up to the timeout; `matches` is then checked once, on the screen as it is by then (§14.2). `notes`
        collects what a picture check has to say (an ignored element that isn't there). While this client records,
        a check that held is saved with the step (`fold`: the step itself was saved, so it goes in its `expect`)."""
        if any(expect.get(k) is not None for k in batch.EXPECT_KINDS):
            missed = await self._expect_element(client_id, view, scope, expect)
            if missed is not None:
                return missed
        if expect.get("matches") is None:
            return None
        given = await self._given(scope) if scope is not None else None
        recording = client_id in self._recording
        kept = batch.recorded_ignore(expect["ignore"], view.items) if recording and expect.get("ignore") else None
        done = await self._compare(view, expect["matches"], threshold=expect.get("threshold"),
                                   ignore=expect.get("ignore"), check=True, given=given)
        if notes is not None:
            notes.extend(done.notes)
        if recording and done.reason is None and client_id in self._recording:
            self._record_compare(client_id, {"name": expect["matches"], "threshold": expect.get("threshold")}, kept,
                                 done.folder, done.source, fold=fold)
        return done.reason

    async def _expect_element(self, client_id: str, view, scope: "Scope | None", expect: dict) -> str | None:
        """Wait up to the expectation's timeout for its text, id or label to hold. None when it did, else why not."""
        kind = next(k for k in batch.EXPECT_KINDS if expect.get(k) is not None)
        timeout = float(expect.get("timeout", batch.EXPECT_DEFAULT))
        if "value" not in expect:
            try:
                await self.run_tool(client_id, "wait_for", {kind: expect[kind], "gone": bool(expect.get("gone")),
                                                            "timeout": timeout, "look": False}, scope)
                return None
            except Refused as e:
                if e.code == "timeout":
                    return batch.expect_failed(expect)
                raise
        target, deadline = ui.parse_target({kind: expect[kind]}), self.control.clock() + timeout
        given = await self._given(scope) if scope is not None else None
        while True:
            await self._read(lambda: self._silent_read(view))
            if given is not None and self._bundle() not in given:
                # in personal mode a value is read only in a given app, as wait_for is (safety review M3)
                app = getattr(self.session, "app", None)
                raise Refused("not_allowed", _outside(self._bundle(), app[0] if app else None, ", ".join(given.values())))
            try:
                found = ui.resolve(view.items, target)
            except Refused:
                found = None
            if isinstance(found, ui.Element) and batch.has_value(found, expect["value"]):
                return None
            if self.control.clock() >= deadline:
                return batch.expect_failed(expect)
            await self.control.sleep(0.25)

    async def _run_tool_inner(self, client_id: str, name: str, args: dict, scope: "Scope | None") -> dict:
        granted: list[bool] = []
        grant_token = _GRANTED.set(granted)
        try:
            # The stream is held open for the length of every tool call; the pump's own linger keeps
            # it open for 30 s after the last one, so the settle always has frames to read.
            # Not when there is no phone: the pump would only retry opening it.
            async with self.pump.watching() if self._connected.is_set() and self.agent_stream else nullcontext():
                await self._check_again(client_id, name)  # before personal mode's front check: see there
                if scope is None:
                    answer = await self._run_tool(client_id, name, args)
                else:
                    answer = await self._within(scope, client_id, name, args)
                if granted:  # a person's OK let a touch through: the answer ends with it
                    self._granted_now = True
                    answer = {**answer, "text": f"{answer['text']}\nNote: {NOTE_ALLOWED}"}
                return answer
        except Refused:
            raise
        except DeviceError as e:  # WebDriverAgent or the link answered with an error; not our bug
            self.log(f"{name}: the phone side failed: {e}")
            raise Refused("internal", f"the phone side failed: {e}") from e
        except Exception as e:  # one failed step never takes the daemon down
            self.log(f"{name} failed unexpectedly:\n{traceback.format_exc()}")
            raise Refused("internal", f"{name} failed unexpectedly: {e}. This is a bug; report it.") from e
        finally:
            _GRANTED.reset(grant_token)

    async def _batch_within(self, scope: "Scope", client_id: str, name: str, args: dict) -> dict:
        """`run`, `record` and `replay` in personal mode: like every tool, nothing without an app list, and the one
        answer (or a stopped batch's message, which carries the same change block) shows no other app's screen.
        Each step went through the app check on its own already."""
        if not scope.apps:
            raise Refused("not_allowed", NO_APPS)
        given = await self._given(scope)
        names = ", ".join(given.values())
        try:
            answer = await self._batch_tool(client_id, name, args, scope)
        except Refused as e:
            message = str(e)
            if "\n" not in message:
                raise
            kept = await self._withheld(client_id, name, {"text": message, "image": None}, given, names)
            raise Refused(e.code, kept["text"]) from None
        return await self._withheld(client_id, name, answer, given, names)

    # ---- personal mode: the agent stays in the apps it was given ----
    async def _given(self, scope: "Scope") -> dict[str, str]:
        """{bundle id: how to show it} of the apps in a scope. An entry that looks like a bundle id is taken as
        it is; a name is looked up among the installed apps, once."""
        if scope.apps not in self._scopes:
            given: dict[str, str] = {}
            for entry in scope.apps:
                if BUNDLE_ID.match(entry):
                    given[entry] = entry
                    continue
                await self.wait_connected()
                try:
                    bundle, shown = await self._app(entry, partial=False)
                except Refused as e:
                    raise Refused("unsupported", f'the app list this agent was started with names "{entry}", which '
                                                 f"can't be used: {e}") from None
                given[bundle] = shown
            self._scopes[scope.apps] = given
        return self._scopes[scope.apps]

    async def _front(self, given: dict[str, str], prompt=None) -> str | None:
        """The app in front, for personal mode's question before a touch. WebDriverAgent answers SpringBoard
        while a notification banner slides over the app in front (seen on the phone 2026-11-10: twice in forty
        calls, each a false `outside_apps`; a banner stays for some seconds). When it says SpringBoard and the phone's
        own process list has one of the given apps in the foreground, it is asked again, FRONT_RETRIES times
        FRONT_RETRY s apart at most. Only WebDriverAgent's answer lets a call through; the process list never
        does, and on the real home screen it has no given app, so that refusal isn't delayed. `prompt`: asked first
        whether the screen is a given app's own permission prompt (see _prompt_of_given): then PROMPT_FRONT at once,
        with no wait, since a prompt doesn't leave by itself as a banner does."""
        front = await self._read(self.session.foreground_bundle_id)
        if front != SPRINGBOARD:
            return front
        try:
            running = set(await asyncio.wait_for(self.session.device.foreground_apps(), 5))
        except Exception:  # no process list in time: WebDriverAgent's word stands
            return front
        if running & set(given) and prompt is not None and await prompt():
            return PROMPT_FRONT
        for _ in range(FRONT_RETRIES if running & set(given) else 0):
            await asyncio.sleep(FRONT_RETRY)
            front = await self._read(self.session.foreground_bundle_id)
            if front != SPRINGBOARD:
                break
        return front

    async def _prompt_of_given(self, given: dict[str, str], view, read: bool) -> bool:
        """Is the system alert on screen a given app's own: a permission prompt the app raised ("“Expo Go” Would
        Like to Send You Notifications")? iOS draws it in SpringBoard, so personal mode saw the home screen in front
        and refused the very `alert` the skill says to answer (a first-time agent stuck on it, 2026-11-11). Only
        when the alert's title names a given app and the phone's process list still has that app in the
        foreground under it. `read`: read the screen first (a touch's check); else the answer's own read counts."""
        if read:
            await self._read(lambda: self._silent_read(view))
        if view.alert_now is None:
            return False
        title = view.alert_now[0]
        named = [b for b in given if (self.session.app_names.get(b) or given[b].split(" (")[0]) in title]
        if not named:
            return False
        try:
            running = set(await asyncio.wait_for(self.session.device.foreground_apps(), 5))
        except Exception:
            return False  # the phone can't say: refuse, the safe side
        return bool(running & set(named))

    async def _back_from_prompt(self, client_id: str, given: dict[str, str], answer: dict) -> dict:
        """After a given app's own permission prompt is answered, the app comes back as the prompt fades. The
        answer's read can come mid-fade and show SpringBoard, which personal mode withholds; a first-time agent then
        reopened the app for nothing (2026-11-11). So wait for the app, up to PROMPT_RETURN_WAIT s, and answer
        with its screen."""
        shown = APP_LINE.search(answer["text"])  # as the check below decides: the App: line, else the last read's app
        app = shown.group(2) if shown else (self.session.app or (None, None))[1]
        if app in given:
            return answer
        loop = asyncio.get_running_loop()
        until = loop.time() + PROMPT_RETURN_WAIT
        while await self._read(self.session.foreground_bundle_id) not in given:
            if loop.time() >= until:
                return answer
            await asyncio.sleep(0.25)
        fresh = await self._run_tool(client_id, "look", {"screenshot": False})
        return {**fresh, "text": answer["text"].splitlines()[0] + "\n" + fresh["text"]}

    async def _within(self, scope: "Scope", client_id: str, name: str, args: dict) -> dict:
        """One tool call in personal mode, failing closed. Nothing runs without an app list. Opening or closing an
        app outside the list is refused. Reading or touching is refused while another app is in front (asked of
        the phone before the call), and an answer never shows another app's screen."""
        if not scope.apps:
            raise Refused("not_allowed", NO_APPS)
        given = await self._given(scope)
        names = ", ".join(given.values())
        front = None
        if name in ("open_app", "close_app") and isinstance(args.get("app"), str) and args["app"].strip():
            if name == "close_app" and isinstance(self.stuck, AppNotAnswering):
                bundle, shown = self.stuck.bundle_id, f"{self.stuck.name} ({self.stuck.bundle_id})"
            else:
                await self.wait_connected()
                bundle, shown = await self._app(args["app"], partial=name == "open_app")
            if bundle not in given:
                raise Refused("not_allowed", f"outside_apps: {shown} isn't one of the apps you were given ({names}).")
        elif name in ACTIONS and name not in SCOPE_FREE or name in ("wait_for", "audit", "filmstrip", "compare_screen"):
            # A look needs no question first: its own read says which app is in front, and an answer from
            # another app is withheld below. Touches, and wait_for (it reveals whether a text is there), ask.
            await self.wait_connected()
            view = self.control.client(client_id)
            front = await self._front(given, prompt=lambda: self._prompt_of_given(given, view, read=True))
            if front not in given and front != PROMPT_FRONT:
                raise Refused("not_allowed", _outside(front, self.session.app_names.get(front or ""), names,
                                                      alert=getattr(self.session, "last_alert", None) is not None))
        given_token = _GIVEN.set(given)
        try:
            answer = await self._run_tool(client_id, name, args)
        finally:
            _GIVEN.reset(given_token)
        if name == "alert" and front == PROMPT_FRONT:
            answer = await self._back_from_prompt(client_id, given, answer)
        if name == "filmstrip":  # asked of the phone before and after filming (_filmstrip): the last read is no guide
            return answer
        if name == "apps":  # only its own apps: the rest of the phone is the owner's business
            lines = answer["text"].splitlines()
            kept = [ln for ln in lines if ln.split(" ", 1)[0] in given or not BUNDLE_ID.match(ln.split(" ", 1)[0])]
            return {**answer, "text": "\n".join(kept)}
        return await self._withheld(client_id, name, answer, given, names)

    async def _withheld(self, client_id: str, name: str, answer: dict, given: dict[str, str], names: str) -> dict:
        """An answer as personal mode may show it: whole when it lists one of the given apps (or iOS's prompt for
        one), else only its first line and why. The answer's own App: line says whose screen it lists; without one,
        the app at the last read does; when not even that is known, nothing beyond the first line is shown."""
        shown = APP_LINE.search(answer["text"])
        app = (shown.group(1), shown.group(2)) if shown else self.session.app
        bundle = app[1] if app is not None else None
        if ((bundle is None or bundle not in given) and ("\n" in answer["text"] or answer.get("image"))
                and not (bundle == SPRINGBOARD
                         and await self._prompt_of_given(given, self.control.client(client_id), read=True))):
            alert = "\nAlert: " in answer["text"]
            named = app[0] if app is not None else None
            if name == "look":  # nothing was done, so there is nothing to report but the refusal
                raise Refused("not_allowed", _outside(bundle, named, names, alert))
            first = answer["text"].splitlines()[0]
            first = "" if first.startswith("Screen ") else first + "\n"  # a look's first line is the screen itself
            return {"text": f"{first}Note: {_outside(bundle, named, names, alert)}", "image": None, "image_size": None}
        return answer

    async def _run_tool(self, client_id: str, name: str, args: dict) -> dict:
        if name not in ACTIONS and name not in READS and name not in SPECIAL:
            raise Refused("unsupported", f"no tool named {name!r}; the tools are "
                                         f"{', '.join([*READS, *SPECIAL, *ACTIONS])}")
        missing = [k for k in REQUIRED.get(name, ()) if args.get(k) is None]
        if missing:
            raise Refused("bad_params", f"{name} needs {', '.join(missing)}")
        args = self._ocr_target(client_id, args)
        if "index" in args and isinstance(args.get("target"), dict) and "index" not in args["target"]:
            # `index` belongs inside `target`; a first-time agent put it beside it and got an ambiguous error back
            # with no hint (2026-11-12). Taken where it was meant to go.
            args = {**{k: v for k, v in args.items() if k != "index"}, "target": {**args["target"], "index": args["index"]}}
        args = _checked(name, args)
        shot = self._shot_args(args, default=name == "look", custom=name == "look")  # bad width: refused here
        # A bad target was refused by _checked, before anything is queued or sent. Any tool whose
        # arguments carry `target` goes through here, so the live-read rule in act() covers it too.
        target = args.get("target")
        view = self.control.client(client_id)
        # an agent that hasn't looked yet has no "last look": its first list is whole anyway
        notes = [NOTE_PERSON] if 0 <= view.seen < self.control.person_generation else []
        if name == "close_app" and isinstance(self.stuck, AppNotAnswering):
            return await self._close_stuck(self.stuck, args["app"], notes)
        await self.wait_connected()
        if (taken := self._taken_notes.pop(client_id, None)) is not None:
            notes = [*notes, taken]
        if name == "apps":
            return await self._apps(notes)
        if name == "wait":
            return await self._wait(view, args, notes, shot)
        if name == "wait_for":
            return await self._wait_for(view, args, notes, shot)
        if name == "audit":
            return await self._audit(view, args, notes)
        if name == "filmstrip":
            return await self._filmstrip(client_id, args, notes)
        if name == "compare_screen":
            return await self._compare_screen(view, args, notes)
        if name == "look":
            await self._settle_pending()
            if args.get("ocr") and not ocr.available():
                raise Refused("unsupported", ocr.INSTALL)
            view.ocr = {}  # refs o1… belong to one look with ocr: true, and go with the next look
            answer = await self._look(view, shot, [], notes)
            if args.get("ocr"):
                return await self._with_ocr(view, answer)
            if len(view.items) < FEW_ELEMENTS and ocr.available():  # (without the extra, the description says it)
                answer = {**answer, "text": answer["text"] + "\nNote: " + NOTE_FEW}
            return answer
        if self.state == "locked":
            raise Refused("locked", "the phone is locked; ask the user to unlock it")
        call, settle = ACTIONS[name]
        if session_module.TRACE:
            self.log(f"trace {name} begins")
        by_ref = target is not None and target.kind == "ref"
        # these tools at a point act on the element under it, so they need a current list too
        by_lookup = target is not None and (target.kind in ("id", "label", "text")
                                            or (name in ("type_text", "toggle", "scroll") and target.kind == "point"))

        fresh, full, box = [True], [False], {}
        dry = bool(args.get("dry_run"))  # resolve and check, but send nothing to the phone (_dry)

        async def act():
            self._require_connected()
            await self._quicken()
            s = self.session
            # a list read mid-redraw can miss elements while the screen looks unchanged since: a target missing
            # from it is read for again before it is not_found
            live = by_lookup and (not self._current(view) or (target.kind != "point" and self._missing(view.items, target)))
            if not live:  # a silent re-read settles for itself
                await self._settle_pending()
            s.items = view.items
            fresh[0] = view.seen == self.control.generation
            # never looked, a person acted, or the line dropped: the agent's list may be anything
            full[0] = not fresh[0] or view.dropped or view.read_at == float("-inf")
            if by_ref and view.dropped:
                raise Refused("stale", "the connection to the phone dropped since your last look; look again")
            if by_ref and not fresh[0]:
                raise Refused("stale", "the screen changed since your last look (someone else used the "
                                       "phone); look again")
            if live:
                # Resolve on the live tree. The agent's refs follow their elements onto it, so `e3` still
                # means the same element when it moved. The view's freshness doesn't change: a person's
                # action still makes refs stale, and `view.shown` (what the agent was told) stays put.
                if await self._silent_read(view) and self._missing(s.items, target):
                    # the app may have been mid-redraw: one more look, once it is still, before saying so
                    await self._silent_read(view)
            if by_lookup and target.kind != "point" and self._missing(s.items, target):
                if self._missing(s.items, dataclasses.replace(target, index=None)):
                    # an alert hides the app's elements: say so, not "not found". (When only the `index` is
                    # out of range the alert's own elements matched, and that miss is the answer.)
                    self._refuse_behind_alert(view, force=False)
                self._refuse_behind_sheet(target)  # before off_screen: scrolling can't help until the sheet is closed
                if not (name == "scroll" and args.get("to") == "target"):  # that scroll is how it comes on screen
                    if name in KEYBOARD_TOOLS and view.read_keyboard and not dry and self._under_keyboard(target):
                        # the keyboard hides it, and a short form can't scroll it up: a person closes the keyboard
                        # first (a first-time agent was told off_screen for Wayfare's "Create account", 2026-11-11)
                        closed = await self._dismiss_keyboard_now(view)
                        if not self._missing(s.items, target):
                            box["closed"] = closed
                    if "closed" not in box:
                        self._refuse_off_screen(target)
            # a person would close the keyboard before the touch
            box["note"] = box.pop("closed", "") or ("" if dry else await self._close_keyboard(view, name, target))
            s.last_point = None
            box["app"], s.top_touch = s.app, False  # the app the touch is meant for (see _taken_away)
            box["mark"] = self._mark()  # where the stream stands as the gesture goes out
            handler = self._handlers.get(name)
            if dry and name == "swipe":
                self._dry(args, f"Would swipe from ({args['x0']:g}, {args['y0']:g}) to ({args['x1']:g}, {args['y1']:g}).")
            return await (handler(view, args) if handler else call(s, args))

        async def act_as_agent():
            # the touches inside are the agent's: Tapline is told before each one (tapline.py). Set only here, so a
            # person's input from the live view, queued separately, is never announced as the agent's
            self.session.touch_hook, self.session.banner_guard = self._agent_touches.notify, True
            try:
                return await act()
            finally:
                self.session.touch_hook, self.session.banner_guard = None, False

        try:
            result = await self.control.agent(act_as_agent)
        except Unanswered as e:
            # the line is alive and the session stays; the screen may or may not have changed under the agent's list
            view.moved = True
            self.unsettled = True
            self._announce("agent", name, ok=False, code="timeout")
            raise Refused("timeout", f"{e}; the action may have happened: look before you retry.") from None
        except ConnectionLost as e:
            self.lost(e)
            if isinstance(e, AppNotAnswering):  # the action went out, then the app stopped answering
                self._announce("agent", name, ok=False, code="not_answering")
                raise self._not_connected() from None
            self._announce("agent", name, ok=False, code="reconnecting")
            raise Refused("reconnecting", "the connection dropped during the action; look to check what "
                                          "happened") from None
        except DryRun as e:  # nothing was sent: the answer is what would have been
            return _answer([str(e)], notes)
        except Refused as e:
            # a touch a finger couldn't make: people watching see the step and why (VIEWER-PROTOCOL 1.4 `reason`)
            if e.code == "not_actionable":
                said = str(e)
                word = said.split(":", 1)[0] if ":" in said else ""
                self._announce("agent", name, label=element_name(said) or None, ok=False, code=e.code,
                               reason=word if word.isidentifier() else None, caption=step_caption(name, said, args))
            raise
        if isinstance(result, Noted) and result.note:
            notes = [*notes, result.note]
        # The keyboard note is given only when the keyboard is still closed as the call answers (AGENT-TOOLS
        # v1.4): type_text focuses its input, so the keyboard is up again and the note would contradict the header
        closed_first = box.get("note") if name != "type_text" else None
        if closed_first:
            notes = [*notes, closed_first]
        if fresh[0]:  # its own action keeps what it knew current; after someone else's, it must look
            view.seen = self.control.generation
        if isinstance(result, Unchanged):  # nothing was done: nothing to settle or diff
            return _answer([str(result)], notes)
        reads_only = name == "alert" and args.get("action") == "text"  # it changes nothing on the phone
        looks = settle is not None and args.get("look", True) and not reads_only
        view.moved = view.moved or not looks
        if not reads_only:
            self.unsettled = not looks
        self._announce("agent", name, label=element_name(str(result)), caption=step_caption(name, str(result), args),
                       point=self._point(self.session.last_point))
        if not looks:
            view.unlooked += 0 if reads_only else 1
            taken = await self._taken_away(view, box.get("app"))
            self._recheck_later(client_id, box.get("app"), taken)
            return _answer([str(result)], [*notes, taken] if taken else notes)
        mark = box["mark"]  # the stream as the gesture went out; the settle only trusts frames after the return
        # frames during the call count for actions with the normal cap; a launch may show a still splash first
        if isinstance(result, Toggled):
            landed = await self._toggle_landed(view, result)
        elif isinstance(result, Typed) and args.get("replace", True) and not args.get("submit"):
            landed = await self._typed_landed(view, result, args["text"])
            if landed is None and not result.field.secure and await self._typed_wrong(result, args["text"]) and \
                    await self.control.agent(lambda: self.session.retype(args["text"])):
                # the field doesn't hold what was typed: keys went missing (iOS swapping in another keyboard as
                # the field took focus, seen on the phone 2026-11-11: "abc" landed as "a", and the retry with the
                # caret at the start made it "abca"). Cleared and typed once more, and the answer says so.
                notes = [*notes, NOTE_RETYPED]
                landed = await self._typed_landed(view, result, args["text"])
        else:
            landed = None
        if landed is not None:
            done = Settled(landed[1], True, False)
        else:
            done = await self._settle(self._mark()[0], cap=settle, since=mark[1],
                                      early=mark[0] if settle == CAP else None)
        if session_module.TRACE:
            self.log(f"trace {name} settle {done.seconds:.2f} s, changed={done.changed}")
        if done.capped:
            notes = [*notes, NOTE_CAPPED.format(cap=settle)]
        if name == "back" and not done.changed:
            raise Refused("not_actionable", "no_action: nothing changed after back" + self._still_open())
        # a toggle that changed nothing on the stream is not trusted: the tree says whether the switch flipped
        # Never on the AirPlay video's word alone: a mirror that stalls repeats its last picture, which looks exactly
        # like a still screen, so "No visible change" could be false. Then the tree read decides (0.16 s).
        if (not done.changed and name != "toggle" and not (full[0] or view.moved) and not view.shown_off
                and not self._fed_by_video()
                and (mark[1] is view.read_mark[1] or alike(mark[1], view.read_mark[1]))):
            # no frame changed after the call returned, and the picture at its start is the one the agent's
            # list was read against (nothing changed silently before it): no tree read, the list stays.
            # Not when something lies off screen: the action's effect may be there, and only a read shows it
            view.read_at, view.read_mark = self.control.clock(), self._mark()
            view.unlooked = 0  # this block covers them: nothing changed since the list
            return await self._add_shot(_answer([str(result), changes.SAME_SCREEN, changes.NO_CHANGE,
                                                 settled_line(done.seconds)], notes), view, shot, at_read=False)
        to_target = name == "scroll" and args.get("to") == "target"
        # (a scroll to a target read the list after each of its drags, with the refs carried: its last read is no
        # move for the refs, though the content did move since the agent's last answer)
        moves = None if name in MAY_MOVE_CONTENT else name in MOVES_CONTENT and not to_target
        answer = await self._after(view, str(result), notes, full[0], moves, done.seconds, drifted=to_target,
                                   snap=landed[0] if landed is not None else None)
        if closed_first and view.read_keyboard:  # the tap focused another input: the keyboard is up again
            answer["text"] = answer["text"].replace(f"\nNote: {closed_first}", "", 1)
        if isinstance(result, Toggled):
            # say only what the new list shows; if the switch can't be found again, claim nothing
            old = result.before
            same = [e for e in view.items if (e.role, e.label, e.identifier) == (old.role, old.label, old.identifier)]
            # by its ref; when the answer is a full list that numbered it anew (an agent's first call), by being the
            # only switch with that identity
            now = next((e for e in same if e.ref == result.ref), same[0] if len(same) == 1 else None)
            if now is not None:
                answer["text"] = answer["text"].replace(str(result), result.verdict(now, bool(view.alert)), 1)
        if isinstance(result, Typed):  # the value as the new list shows it, not as it was typed
            old = result.field
            alike_fields = [e for e in view.items if e.role == old.role and (
                e.identifier == old.identifier if old.identifier else e.label == old.label)]
            now = next((e for e in view.items if result.same_field(e)),
                       alike_fields[0] if len(alike_fields) == 1 else None)  # renumbered in a first full list
            if now is not None:
                answer["text"] = answer["text"].replace(str(result), result.verdict(now), 1)
        taken = await self._taken_away(view, box.get("app"), read=self.session.app)
        self._recheck_later(client_id, box.get("app"), taken)
        if taken:  # the screen the agent is shown is the app's, back in front, not the other app's
            return await self._look(view, shot, [str(result)], [*notes, taken])
        return await self._add_shot(answer, view, shot)

    def _recheck_later(self, client_id: str, before: tuple[str, str] | None, taken: str | None) -> None:
        """After a touch near the top that nothing was seen taking: the other app may come to the front a moment
        after the answer's check (seen on the phone 2026-11-11 with look: false), so the agent's next call asks
        again, if it comes within RECHECK_WITHIN and no person acted in between (Tapline's review)."""
        if taken is None and before is not None and self.session.top_touch:
            self._recheck[client_id] = (before, time.monotonic(), self.control.person_generation)
        else:
            self._recheck.pop(client_id, None)

    async def _check_again(self, client_id: str, name: str) -> None:
        """At an agent's call: the check _recheck_later left for it. Done before personal mode's own check of the
        app in front, so the agent hears that a banner took its touch rather than `not_allowed`. An action is
        refused `stale` (its refs are for the app, back in front now); a look carries the note. `apps` reads
        nothing of the screen and leaves the check for the next call; open_app makes it moot."""
        if name == "apps" or (pending := self._recheck.pop(client_id, None)) is None or name == "open_app":
            return
        before, when, persons = pending
        if time.monotonic() - when > RECHECK_WITHIN or self.control.person_generation != persons:
            return
        await self.wait_connected()
        view = self.control.client(client_id)
        taken = await self._taken_away(view, before, top=True)
        if taken is None:
            return
        if name in ACTIONS:
            view.moved = True
            raise Refused("stale", f"after your last action: {taken} This call did nothing.")
        self._taken_notes[client_id] = taken

    async def _taken_away(self, view, before: tuple[str, str] | None,
                          read: tuple[str, str] | None = None, top: bool | None = None) -> str | None:
        """After an agent's touch that began where a notification banner comes down: did another app come to the
        front? A banner landing between the check before the touch and the touch itself takes it (seen on the phone
        2026-11-11: twice Mail opened, once on a toggle at y 159). Then the app the touch was meant for is opened
        again and the note says so; None when nothing took it. `read`: the app the answer's own read found in
        front, when it made one (then the phone is asked only if that differs); without one the phone is asked
        once (about 70 ms, and only after a touch near the top). The home screen or a notification pulled open
        count too, unless an alert is up (a permission prompt the touch raised is the app's own) or the phone's
        process list still has the app in front (a banner still sliding over it). `top`: whether the touch began
        near the top, when it isn't the latest action's (the check at the next call, see _run_tool)."""
        s = self.session
        if before is None or not (s.top_touch if top is None else top):
            return None
        name, bundle = before
        if bundle == SPRINGBOARD:
            # never the app a touch is meant for: a read taken while a banner slides over an app names SpringBoard
            # (seen on the phone 2026-11-11: "opened" the home screen over Settings), and a touch on the home
            # screen that opens an app is what it is for
            return None
        if read is not None and read[1] == bundle:
            return None
        front = await self._read(s.foreground_bundle_id)
        if not front or front == bundle:
            return None
        if await self._still_under(bundle):
            # the app still runs in front: something lies over it, not instead of it. Sign in with Apple, Apple Pay,
            # a Safari view, the share sheet are other processes over the app (Tapline's review), and a banner
            # slides over it. Only SpringBoard staying in front once a banner would have gone is a theft: a shade
            # (Notification Center, Control Center) pulled down from the top edge
            if front != SPRINGBOARD:
                return None
            if read is None:  # the answer had no read of its own: one now, which waits out a banner
                await self._read(lambda: self._silent_read(view))
            if view.alert_now is not None or await self._read(s.foreground_bundle_id) != SPRINGBOARD:
                return None  # a permission prompt the touch raised is the app's own; or the banner has gone
        elif front == SPRINGBOARD:
            if read is None:
                await self._read(lambda: self._silent_read(view))
            if view.alert_now is not None:
                return None
        await self.control.agent(lambda: s.launch(bundle))
        self.log(f"the agent's touch near the top left {bundle} for {front}: opened {bundle} again")
        if front == SPRINGBOARD:
            return NOTE_TAKEN_SYSTEM.format(other=SYSTEM_IN_FRONT, app=f"{name} ({bundle})")
        known = read[0] if read is not None and read[1] == front else s.app_names.get(front)
        return NOTE_TAKEN.format(other=f"{known or front} ({front})", app=f"{name} ({bundle})")

    async def _still_under(self, bundle: str) -> bool:
        """Whether iOS's process list still has the app running in front (under whatever WebDriverAgent names)."""
        try:
            return bundle in await asyncio.wait_for(self.session.device.foreground_apps(), 5)
        except Exception:
            return False  # the phone can't say: count the app as gone, the safer side for an agent's next touch

    async def turned(self, seq: int, jpeg: bytes, degrees: int) -> bytes:
        """A stream frame turned upright for landscape, made once per frame for every viewer."""
        kept = self._turned
        if kept is not None and kept[0] == (seq, degrees):
            return kept[1]
        out = await asyncio.to_thread(turn_jpeg, jpeg, degrees)
        self._turned = ((seq, degrees), out)
        return out

    async def _toggle_landed(self, view, result: "Toggled") -> tuple[Snapshot, float] | None:
        """A switch's value changes the moment the touch lands; the knob's slide after it is only drawing. Waiting
        for the screen to stand still cost a toggle 0.33 to 0.65 s (measured on the phone 2026-11-11). So the tree
        is read until the switch shows its new value, and that read is the answer. Not when anything else
        appeared or went away in that read (a toggle that opens a section may still be animating it in): then
        None, and the usual wait for a still screen follows. None too when the switch doesn't flip in
        TOGGLE_LAND_WAIT."""
        loop = asyncio.get_running_loop()
        began = loop.time()
        old = result.before
        while loop.time() - began < TOGGLE_LAND_WAIT:
            snap = await self._read(lambda: self.session.snapshot())
            if snap.tree.alert is not None:
                return None  # a confirm alert came up: the usual wait, and the verdict knows about alerts
            now = next((e for e in snap.tree.items if (e.role, e.identifier or "", e.label if not e.identifier else "")
                        == (old.role, old.identifier or "", old.label if not old.identifier else "")), None)
            if now is not None and now.checked == result.flipped:
                if len(snap.tree.items) != len(view.items):
                    return None
                return snap, loop.time() - began
            await asyncio.sleep(TOGGLE_POLL)
        return None

    async def _typed_wrong(self, result: "Typed", text: str) -> bool:
        """Does the field typed into hold something other than `text` on a fresh read? False when it can't be found
        again (claim nothing) or holds exactly `text`."""
        snap = await self._read(lambda: self.session.snapshot())
        old = result.field
        same = [e for e in snap.tree.items if e.role == old.role and (
            e.identifier == old.identifier if old.identifier else e.label == old.label)]
        return len(same) == 1 and not same[0].secure and same[0].value != text

    async def _typed_landed(self, view, result: "Typed", text: str) -> tuple[Snapshot, float] | None:
        """As _toggle_landed, for typing: the field holds the typed text as soon as the keys call returns, and the
        screen's wait after it (0.2 to 0.4 s on the phone) only saw the caret blink. The first read that shows the
        field with exactly that text is the answer, unless anything else appeared or went away in it."""
        loop = asyncio.get_running_loop()
        began = loop.time()
        old = result.field
        while loop.time() - began < TOGGLE_LAND_WAIT:
            snap = await self._read(lambda: self.session.snapshot())
            if snap.tree.alert is not None:
                return None
            same = [e for e in snap.tree.items if e.role == old.role and (
                e.identifier == old.identifier if old.identifier else e.label == old.label)]
            if len(same) == 1 and not same[0].secure and same[0].value == text:
                if len(snap.tree.items) != len(view.items):
                    return None
                return snap, loop.time() - began
            await asyncio.sleep(TOGGLE_POLL)
        return None

    async def _apps(self, notes: list) -> dict:
        apps = await self._read(self.session.apps)
        if not apps:
            return _answer(["No apps installed by you."], notes)
        try:
            front = await self._read(self.session.foreground_bundle_id)
        except Refused:
            raise
        except Exception:
            front = None  # the list is still right without the marker
        lines = [app_line(a) + (" *" if a["bundle_id"] == front else "") for a in apps]
        return _answer([*lines, "* = in front."], notes)

    async def _app(self, query: str, partial: bool = True) -> tuple[str, str]:
        """(bundle id, how to show it) for what the agent named, in this order: an installed bundle id; a
        name, ignoring case, among the installed apps (exact, then, if `partial`, the start of a name); a
        built-in app's name; a bundle-id-looking string as given (built-in apps aren't in `apps`). Parts
        of bundle ids and of later words never match, so "Notes" doesn't hit "Sticky Notes"."""
        q = query.strip()
        apps = await self.session.apps()
        low = q.lower()
        for a in apps:
            if a["bundle_id"] == q:
                return q, f"{a['name']} ({q})"
        hits = [a for a in apps if a["name"].lower() == low]
        builtin = next((n for n in SYSTEM_APPS if n.lower() == low), None)
        if not hits and builtin:
            return SYSTEM_APPS[builtin], f"{builtin} ({SYSTEM_APPS[builtin]})"
        if not hits and partial:
            hits = [a for a in apps if a["name"].lower().startswith(low)]
        if not hits:
            if BUNDLE_ID.match(q):
                return q, q  # launching it says whether it exists
            raise Refused("not_found", f'no installed app is named "{q}"; call apps to see them')
        if len(hits) > 1:
            raise Refused("ambiguous", f'{len(hits)} installed apps match "{q}". Pick one by its bundle id:\n'
                                       + "\n".join(app_line(a) for a in hits[:10]))
        return hits[0]["bundle_id"], f"{hits[0]['name']} ({hits[0]['bundle_id']})"

    async def _open_app(self, view, a: dict) -> str:
        bundle_id, shown = await self._app(a["app"])
        try:
            await self.session.launch(bundle_id)
        except ConnectionLost:
            raise
        except DeviceError as e:
            raise Refused("not_found", f"couldn't open {bundle_id}: {e}; call apps for the installed apps") from None
        return f"Opened {shown}."

    async def _close_app(self, view, a: dict) -> str:
        bundle_id, shown = await self._app(a["app"], partial=False)
        await self.session.terminate(bundle_id)
        return f"Closed {shown}."

    async def _close_stuck(self, stuck: AppNotAnswering, query: str, notes: list) -> dict:
        """close_app while the app in front is frozen: end its processes through iOS (WebDriverAgent's own
        terminate would wait on the app), then connect again at once. Only that app: the rest can wait."""
        if self.control.paused:
            raise Refused("paused", "paused by the user in the live view; ask them to resume")
        if query.strip().lower() not in (stuck.bundle_id.lower(), stuck.name.lower()):
            raise self._not_connected()
        device = self.session.device
        if device.rsd is None:
            raise self._not_connected()
        await device.kill_app(stuck.bundle_id)
        self.log(f"closed {stuck.bundle_id}, which wasn't answering")
        self.stuck = None
        self._wake.set()
        self._announce("agent", "close_app", label=stuck.name)
        return _answer([f"Closed {stuck.name} ({stuck.bundle_id}), which wasn't answering."], notes)

    async def _wait(self, view, args: dict, notes: list, shot) -> dict:
        """Sleep outside the phone's queue (a person may act meanwhile), then answer with what changed."""
        seconds = args["seconds"]
        await self.control.sleep(seconds)
        result = f"Waited {seconds:.1f} s."
        if not args.get("look", True):
            view.unlooked += 1
            return _answer([result], notes)
        full = view.seen != self.control.generation or view.dropped or view.read_at == float("-inf")
        answer = await self._after(view, result, notes, full)  # no `Settled in`: the app did nothing
        return await self._add_shot(answer, view, shot)

    def _ocr_target(self, client_id: str, args: dict) -> dict:
        """A ref from the last OCR reading (`o3`) is a point: the centre of that text."""
        target = args.get("target")
        ref = target.get("ref") if isinstance(target, dict) else None
        if not isinstance(ref, str) or not OCR_REF.fullmatch(ref):
            return args
        point = self.control.client(client_id).ocr.get(ref)
        if point is None:
            raise Refused("not_found", f"{ref} isn't in your last OCR reading; look with ocr: true again.")
        return {**args, "target": {"x": round(point[0], 1), "y": round(point[1], 1)}}

    async def _with_ocr(self, view, answer: dict) -> dict:
        """`look` with `ocr: true`: the text the picture shows that no element carries, as lines with refs o1, o2…
        after the list (before its notes). Read from the stream's newest frame when it is fresh, else a screenshot."""
        frame = self.hub.latest
        if frame is not None and self.hub.now_ms() - frame.at <= FRESH_FRAME * 1000:
            picture = await jpeg_of(frame)
        else:
            picture = await self._read(self.session.screenshot_png)
        seen = await asyncio.to_thread(ocr.read, picture, self.session.screen)
        known = [k.casefold() for e in view.items for k in (e.text, e.label, e.value, e.placeholder) if k]
        width, height = self.session.screen
        top = STATUS_BAR if height > width else 0.0  # the clock and the battery are the phone's, not the app's
        new = [s for s in seen if s.rect[1] >= top and not _said(s.text, known)]
        view.ocr = {f"o{n}": s.center for n, s in enumerate(new, 1)}
        lines = answer["text"].split("\n")
        cut = next((i for i, line in enumerate(lines) if line.startswith("Note: ")), len(lines))
        head = OCR_HEAD if new else OCR_NONE
        lines[cut:cut] = [head, *(s.line(f"o{n}") for n, s in enumerate(new, 1))]
        return {**answer, "text": "\n".join(lines)}

    async def _audit(self, view, args: dict, notes: list) -> dict:
        """AGENT-TOOLS §10: XCTest's own accessibility audit of the screen in front, plus `duplicate_id` from the
        tree, as one report. The tree is read first so each issue is named by the agent's current element line."""
        types = list(args.get("types") or audit.TYPES)
        asked = [audit.XCTEST[t] for t in types if t in audit.XCTEST]
        await self._settle_pending()
        await self._read(lambda: self._silent_read(view))
        issues = await self._read(lambda: self.session.audit(asked)) if asked else []
        app = getattr(self.session, "app", None)
        name = app[0] if app else "the screen"
        text = audit.report(f'"{name}"', issues, view.items, types)
        return {"text": "\n".join([text, *(f"Note: {n}" for n in notes)]), "image": None, "image_size": None}

    async def _filmstrip_logged(self, client_id: str, args: dict, scope: "Scope | None") -> dict:
        """`filmstrip` as one tool call, with its line in the action log (its tap is logged as a tap, as usual)."""
        started, self._announced, self._granted_now = time.monotonic(), None, False
        token = _FILM_SCOPE.set(scope)
        logged = {**args, "target": args.get("tap")}
        try:
            result = await self._run_tool_inner(client_id, "filmstrip", args, scope)
        except Refused as e:
            self._log_action("filmstrip", logged, started, e)
            raise
        finally:
            _FILM_SCOPE.reset(token)
        made = re.match(r"Filmstrip: (\d+) frames", result["text"])
        self._log_action("filmstrip", logged, started, None, frames=int(made.group(1)) if made else None)
        return result

    async def _watch_front(self, given: dict[str, str], client_id: str, away: list) -> None:
        """While a filmstrip films in personal mode: ask the phone which app is in front, again and again, and note
        the first that isn't a given app (or iOS's prompt for one) in `away`. SpringBoard is asked about as _front does,
        so a notification banner over the app isn't taken for leaving it."""
        view = self.control.client(client_id)
        while True:
            front = await self._front(given, prompt=lambda: self._prompt_of_given(given, view, read=True))
            if front not in given and front != PROMPT_FRONT:
                away.append(front)
                return
            await asyncio.sleep(FRONT_WATCH)

    async def _still_given(self, given: dict[str, str]) -> None:
        """Refuse unless a given app is in front now (after a picture was taken for it): personal mode's question
        before the call can't see an app that changes during it."""
        front = await self._front(given)
        if front not in given:
            raise Refused("not_allowed", _outside(front, self.session.app_names.get(front or ""),
                                                  ", ".join(given.values())))

    async def _film_frame(self, after_seq: int, due: float) -> tuple[int, int, bytes]:
        """(seq, epoch ms, picture) of the stream's newest frame once loop time `due` has come. It must be newer than
        `after_seq` and fresh; if none comes in time the phone is asked for a screenshot instead (seq unchanged)."""
        loop = asyncio.get_running_loop()
        if (wait := due - loop.time()) > 0:
            await asyncio.sleep(wait)

        def fresh(f: Frame | None) -> bool:
            return f is not None and f.seq > after_seq and self.hub.now_ms() - f.at <= FRESH_FRAME * 1000
        frame = self.hub.latest
        if not fresh(frame):
            frame = await self.hub.next_after(after_seq, timeout=self.shot_wait)
        if fresh(frame):
            try:
                source = await jpeg_of(frame)
            except PictureGone:  # one of the older pictures, whose pixels are let go
                source = None
            turn = getattr(self.session, "turn", 0)
            if source is not None and turn and getattr(frame, "picture", None) is None:
                source = await self.turned(frame.seq, source, turn)  # landscape: the stream's frame comes as captured
            if source is not None:
                return frame.seq, frame.at, source
        return after_seq, self.hub.now_ms(), await self._read(self.session.screenshot_png)

    async def _filmstrip(self, client_id: str, args: dict, notes: list) -> dict:
        """AGENT-TOOLS §14.4: `frames` pictures of the screen about `interval_ms` apart, from the live stream (a
        screenshot where it has none), in one numbered image. With `tap`, frame 1 is taken first, then the tap goes
        through the ordinary tool (policy, personal mode, log), then the rest. How far apart they were is measured
        from when the frames arrived."""
        count, tap = args.get("frames", filmlib.FRAMES[2]), args.get("tap")
        interval, width = args.get("interval_ms", filmlib.INTERVAL_MS[2]) / 1000, args.get("frame_width", filmlib.FRAME_WIDTH[2])
        scope, loop = _FILM_SCOPE.get(), asyncio.get_running_loop()
        notes, shots, seq = list(notes), [], 0
        given = await self._given(scope) if scope is not None else None
        # Personal mode: the front app is watched while filming, not only at the end. A tap that opens another app
        # and comes back inside the strip would otherwise put that app's frames in the picture (review 2026-11-12).
        away: list[str | None] = []
        watcher = asyncio.create_task(self._watch_front(given, client_id, away)) if given is not None else None
        try:
            if tap is not None:
                shots.append(await self._film_frame(0, loop.time()))
                seq = shots[0][0]
                tapped = await self.run_tool(client_id, "tap", {"target": tap, "look": False}, scope)
                notes += [line[len("Note: "):] for line in tapped["text"].splitlines()[1:] if line.startswith("Note: ")]
                seq = max(seq, self._mark()[0])  # the rest follow the tap: a frame that was there as it returned is before
            start = loop.time()
            while len(shots) < count and not away:
                shots.append(await self._film_frame(seq, start + (len(shots) - (1 if tap is not None else 0)) * interval))
                seq = shots[-1][0]
        finally:
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
        if given is not None and not away:  # and once more at the end, for the last frame
            front = await self._front(given, prompt=lambda: self._prompt_of_given(
                given, self.control.client(client_id), read=True))
            if front not in given and front != PROMPT_FRONT:
                away.append(front)
        if away:
            raise Refused("not_allowed", _outside(away[0], self.session.app_names.get(away[0] or ""),
                                                  ", ".join(given.values())))
        arrivals = [at for _, at, _ in shots]
        later = arrivals[1:] if tap is not None and count >= 3 else arrivals  # the tap's own time isn't a spacing
        spacing = round((later[-1] - later[0]) / (len(later) - 1))
        try:
            strip, _ = await asyncio.to_thread(filmlib.compose, [picture for _, _, picture in shots], width)
        except ValueError as e:  # a frame far larger than any screen
            raise Refused("internal", str(e)) from None
        head = (f"Filmstrip: {count} frames about {spacing} ms apart, numbered in order, left to right and top to "
                f"bottom{'; frame 1 is just before the tap, the rest follow it' if tap is not None else ''}.")
        return _answer([head], notes, strip)

    async def _compare_screen(self, view, args: dict, notes: list) -> dict:
        """AGENT-TOOLS §14.1: the screen against the reference picture called `name`, or saved as it. Inside `run`
        or a script it is a check (§14.2): it never saves, and a difference stops the batch as `expect_failed`."""
        started, check = time.monotonic(), BATCH.get() is not None
        done: Compared | None = None
        try:
            if check and args.get("update"):
                raise Refused("bad_params", "a compare_screen step only checks and never saves; leave out update")
            width = self._shot_args({"width": args["width"]}, default=True, custom=True)[0] \
                if args.get("width") is not None else None
            if self.state == "locked":
                raise Refused("locked", "the phone is locked; ask the user to unlock it")
            done = await self._compare(view, args["name"], update=bool(args.get("update")), width=width,
                                       threshold=args.get("threshold"), ignore=args.get("ignore"), check=check)
            if (held := _COMPARED.get()) is not None:
                held.append(done)
            if check and done.reason is not None:
                raise Refused("expect_failed", done.reason)
        except Refused as e:
            self._log_compare(args["name"], started, e, done)
            raise
        self._log_compare(args["name"], started, None, done)
        return _answer(done.lines, [*done.notes, *notes], done.image)

    def _log_compare(self, name: str, started: float, error: Refused | None, done: "Compared | None") -> None:
        """The action log's line for a picture check: the name and the share that changed, never the picture."""
        self.actions.write({
            "source": "tapwright", "by": "agent", "tool": "compare_screen", "name": name,
            "percent": round(done.percent, 2) if done is not None and done.percent is not None else None,
            "ok": error is None, "code": error.code if error is not None else None,
            "ms": round((time.monotonic() - started) * 1000), "app": self._bundle(), "batch": BATCH.get()})

    async def _compare(self, view, name: str, *, update: bool = False, width: int | None = None,
                       threshold: float | None = None, ignore: list[dict] | None = None, check: bool = False,
                       given: dict[str, str] | None = None) -> "Compared":
        """The screen against a reference (§14): saves it when there is none (or `update`), else compares. `check`
        is a batch's way (§14.2): no saving, a missing reference or a difference comes back as `reason`. In a
        replay the script's own references folder comes first, and `--update-references` saves into it. `given`:
        in personal mode only a given app's screen is compared or saved."""
        script = SCRIPT.get()
        given = given if given is not None else _GIVEN.get()
        keep = bool(script and script["update"])  # a replay that makes the script's references
        threshold = compare.DEFAULT_THRESHOLD if threshold is None else float(threshold)
        await self._settle_pending()
        await self._read(lambda: self._silent_read(view))
        bundle = self._bundle()
        if given is not None and bundle not in given:
            # The read may have caught SpringBoard under a notification banner: asked as a touch asks (with its
            # retries), and the reference is kept under the given app that is in front, not SpringBoard's name.
            front = await self._front(given)
            if front not in given:
                raise Refused("not_allowed", _outside(front, self.session.app_names.get(front or ""),
                                                      ", ".join(given.values())))
            bundle = front
        folder = bundle or "unknown"  # (a recording copies the reference from this folder: it comes back in Compared)
        rects, notes = compare.ignored(view.items, ignore or [])
        found = None if (update and not check) or keep else compare.find(name, folder, script["path"] if check and script else None)
        if found is None and check and not keep:
            return Compared("missing", [], notes, compare.missing(name), folder=folder)
        png = await self._read(self.session.screenshot_png)
        if given is not None:  # the picture is saved or compared only if a given app was still in front for it
            await self._still_given(given)
        before = await asyncio.to_thread(compare.load, found) if found is not None else None
        size = width or (before.width if before is not None and check else shotlib.DEFAULT_WIDTH)
        if not shotlib.MIN_WIDTH <= size <= shotlib.MAX_WIDTH:  # a reference's own width is a script's, not ours
            raise Refused("bad_params", f'the reference "{name}" is {size} px wide, and a screen picture is '
                                        f"{shotlib.MIN_WIDTH}-{shotlib.MAX_WIDTH} px wide; save the reference again with "
                                        "update: true.")
        after = await asyncio.to_thread(compare.fit, png, size)
        if before is None:
            dest = compare.script_references(script["path"]) / f"{name}.png" if keep else compare.reference_path(folder, name)
            trusted = script["path"].parent if keep else compare.references_dir()  # nothing below it may be a link
            await asyncio.to_thread(self._refuse_changed_baseline, name, folder, after)  # before anything is written
            try:
                await asyncio.to_thread(compare.save, dest, after, trusted)
            except OSError as e:
                raise Refused("internal", f"couldn't save the reference to {dest}: {e.strerror or e}") from None
            return Compared("saved", [f'Saved the reference "{name}" ({after.width}x{after.height} px).'], notes,
                            folder=folder, source=dest)
        if before.size != after.size:
            raise Refused("bad_params", f'the reference "{name}" is {before.width}x{before.height} px and this screen is '
                                        f"{after.width}x{after.height}; take it at the same width, or update: true.")
        screen = self.session.screen
        scale = after.width / screen[0]
        painted = [(x * scale, y * scale, w * scale, h * scale) for x, y, w, h in rects]
        if screen[1] > screen[0]:  # the clock and the battery are the phone's, not the app's
            painted.append((0, 0, after.width, STATUS_BAR * scale))
        result = await asyncio.to_thread(compare.compare, before, after, painted)
        share = compare.shown(result.percent, result.changed, threshold)
        if result.percent <= threshold:
            return Compared("matches", [f'Matches the reference "{name}": {share} changed.'], notes, None, None,
                            result.percent, folder, found)
        reason = compare.failure(name, result, threshold, scale)
        if check:
            if script is not None and (where := await asyncio.to_thread(self._write_diff, script["path"], name, result)):
                reason += f" (diff: {where})"
            return Compared("differs", [], notes, reason, None, result.percent, folder, found)
        n = len(result.regions)
        lines = [f'Differs from the reference "{name}": {share} changed, in {n} region{"s" if n != 1 else ""} (points):',
                 *compare.region_lines(result.regions, scale, view.items)]
        image = await asyncio.to_thread(result.picture)
        return Compared("differs", lines, notes, reason, image, result.percent, folder, found)

    @staticmethod
    def _refuse_changed_baseline(name: str, folder: str, picture: "Image.Image") -> None:
        """While a client records: a reference about to be saved under a name the recording already compared (in this
        app), as a picture that isn't the one it compared, is refused before anything is written, so the stored file
        and what `record stop` exports stay the baseline. The same bytes are no change (the same screen again)."""
        seen = _BASELINES.get()
        if not seen:
            return
        # the same name in another app is refused here too, before the save (it was refused only after, so a replay
        # with update_references had already replaced the first app's picture: Codex, PR #3 cycle 5)
        other = next((bundle for bundle, was, _, _ in seen if was == name and bundle != folder), None)
        if other is not None:
            raise Refused("bad_params", f'this recording already compares "{name}" in {other}, and a script keeps one '
                                        f"picture for each name; give the one in {folder} another name")
        wanted = [digest for bundle, was, _, digest in seen if was == name and bundle == folder and digest is not None]
        if not wanted:
            return
        out = io.BytesIO()
        picture.save(out, "PNG")  # the bytes compare.save would write
        if hashlib.sha256(out.getvalue()).hexdigest() not in wanted:
            raise Refused("bad_params", f'record: "{name}" was already compared against another picture in this '
                                        "recording; give this check another name.")

    @staticmethod
    def _write_diff(script: pathlib.Path, name: str, result: "compare.Result") -> str | None:
        """A failed check's picture, next to the script (§14.3): its path, or None when it can't be written."""
        path = compare.script_diffs(script) / f"{name}.png"
        try:
            data = result.picture("PNG")
            compare.write_file(path, lambda out: out.write(data), script.parent)  # not through a link; see write_file
        except OSError:
            return None
        return str(path)

    async def _wait_for(self, view, args: dict, notes: list, shot) -> dict:
        """Poll the fast tree (outside the phone's queue between reads) until the text, id or label is
        on the screen (or, with `gone`, no longer is), then answer with what changed. No `Settled in`."""
        kind = next(k for k in WAIT_FOR_KINDS if args.get(k) is not None)
        target = ui.parse_target({kind: args[kind]})
        gone, timeout = bool(args.get("gone", False)), float(args.get("timeout", WAIT_FOR_DEFAULT))
        clock = self.control.clock
        began = clock()
        timed_out = Refused("timeout", f'Waited {timeout:g} s for {kind} "{args[kind]}" to '
                                       f"{'disappear' if gone else 'appear'}; it didn't happen.")
        seen = False  # has the screen been read at all during this wait?
        while True:
            left = timeout - (clock() - began)
            if left < WAIT_FOR_MIN_LEFT:
                raise timed_out
            # The read gets its own full deadline, so a slow phone isn't taken for a dead one; wait_for's
            # deadline is kept by cancelling the read, which answers `timeout` and leaves the link alone.
            # "It didn't happen" needs one look at the screen, so the first read gets at least
            # WAIT_FOR_FIRST_READ seconds, whatever the wait's own time: over Wi-Fi one read can take most of a
            # second, and a 0.5 s wait then never saw the screen at all (seen 2026-11-10: twelve of twelve). If
            # even that read doesn't come back (a stall), the answer says that the screen wasn't read, not what
            # was on it (the same day: a 5 s wait said a text didn't appear which had been there all along).
            grace = left if seen else max(left, WAIT_FOR_FIRST_READ)
            try:
                tree = await asyncio.wait_for(
                    self._read(lambda: self.session.refresh(timeout=WAIT_FOR_READ_MAX)), grace)
            except asyncio.TimeoutError:
                if not seen:
                    raise Refused("timeout", f"The phone didn't answer within {max(timeout, WAIT_FOR_FIRST_READ):g} s, so it "
                                             f'isn\'t known whether {kind} "{args[kind]}" is on screen; look, then '
                                             "wait again.") from None
                raise timed_out from None
            seen = True
            if self._missing(tree.items, target) == gone:
                break
            left = timeout - (clock() - began)
            if left < WAIT_FOR_MIN_LEFT:
                raise timed_out
            await self.control.sleep(min(WAIT_FOR_POLL, left))
        result = f"Done after {clock() - began:.1f} s."
        if not args.get("look", True):
            view.unlooked += 1
            return _answer([result], notes)
        full = view.seen != self.control.generation or view.dropped or view.read_at == float("-inf")
        # the last poll is the answer's tree: no read of its own, only the app lookup the polls left out
        snap = await self._read(lambda: self.session.snapshot_from(tree))
        return await self._add_shot(await self._after(view, result, notes, full, snap=snap), view, shot)

    async def _type_text(self, view, a: dict) -> Typed:
        """Type into the target, or into the only input on screen (the fast tree has no focus attribute).
        The target is tapped first by the session, which waits for the keyboard and for focus to arrive."""
        target = a.get("target")
        if target is None:
            await self._silent_read(view)  # which inputs there are must be read now
            inputs = [e for e in view.items if e.role == "input"]
            if not inputs:
                raise Refused("not_found", "No input on screen")
            if len(inputs) > 1:
                shown = "\n".join([*(f"  {e.line()}" for e in inputs[:ui.MAX_CANDIDATES]),
                                   *ui.more_candidates(len(inputs), ui.MAX_CANDIDATES),
                                   "Retry with one of the refs as the target."])  # no target, so no `index` to add
                raise Refused("ambiguous", f"{len(inputs)} inputs on screen; pass a target to pick one:\n"
                                           f"Candidates:\n{shown}")
            field = inputs[0]
        elif target.point is not None:
            x, y = target.point
            field = ui.at_point(view.items, target.point, "input")
            if field is None:
                raise Refused("not_actionable", f"no_action: Nothing at ({round(x)}, {round(y)}) takes typing.")
        else:
            field = ui.resolve(view.items, target)
            if field.role != "input":
                raise Refused("not_actionable", f"no_action: {field.line(actions=False)} is not an input")
        # the focusing tap is a touch: refused where tap would be, and made where it shows; `force` as for tap
        self._refuse_disabled(field)
        forced, at = "", None
        if a.get("force"):
            forced = self._forced(view, field)
        else:
            self._guard(view, field)
            at = self._touch_point(field)
        self._dry(a, f"Would type {len(a['text'])} character{'s' if len(a['text']) != 1 else ''} into {_brief(field)}.")

        async def typed() -> None:
            await self.session.type_text(a["text"], replace=bool(a.get("replace", True)),
                                         submit=bool(a.get("submit", False)), target=ui.Target(ref=field.ref), at=at)
        try:
            await typed()
        except Refused as e:
            # The keyboard was left up for the tap on another input, and this app gave that tap to the keyboard
            # (it closed it, or nothing happened): close it the old way and tap again. Nothing was typed.
            if not (view.read_keyboard and "didn't take focus" in str(e)):
                raise
            await self._close_keyboard(view, "type_text", ui.Target(ref=field.ref), shown_inputs=True)
            await typed()
        done = Typed(f"Typed into {field.line(actions=False, place=False)}.")
        retyped = NOTE_RETYPED if getattr(self.session, "retyped", False) else ""
        done.field, done.note = field, " ".join(n for n in (forced, retyped) if n)
        return done

    async def _scroll(self, view, a: dict) -> Scrolled:
        """Swipe through the centre of the scroll area (the innermost one around the target, the point or the
        screen's centre), `amount` of its height (or width), between the bars. `to` repeats that until a swipe
        leaves the settled screen as the one before left it (a rubber band or a spinner moves the screen
        meanwhile, so "no change at all" would never come), at most MAX_SCROLL_SWIPES times or SCROLL_CAP seconds."""
        s = self.session
        target, to = a.get("target"), a.get("to")
        if to == "target":
            if a.get("dry_run"):
                self._dry(a, f"Would scroll until {_brief(self._anywhere(view, target))} is on screen.")
            return await self._scroll_to(view, target)
        if target is None:
            area = ui.scroll_container(view.items, s.screen)
        elif target.point is not None:
            x, y = target.point
            area = ui.scroll_at(view.items, target.point)
            if area is None:
                raise Refused("not_actionable", f"no_action: Nothing at ({round(x)}, {round(y)}) takes a scroll.")
        else:
            hit = ui.resolve(view.items, target)
            area = ui.scroll_of(view.items, hit)
            if area is None:
                raise Refused("not_actionable", f"no_action: {hit.line(actions=False)} is not inside a scroll view")
        direction = a.get("direction") or (SCROLL_TO[to][0] if to else "down")  # _checked made `to` and it agree
        top, bottom = s.last_insets
        band = (SCROLL_TOP_INSET if top is None else top,
                s.screen[1] - (SCROLL_BOTTOM_INSET if bottom is None else bottom))
        x0, y0, x1, y1 = ui.scroll_area_points(direction, area.rect if area else (0.0, 0.0, *s.screen),
                                               a.get("amount", 0.8), band)
        bounds = area.rect if area else (0.0, 0.0, *s.screen)
        # Whether the tree shows what lies past the list's edge. Where it does (React Native renders a whole list),
        # the end is reached when a read after a drag shows nothing past it any more: no waiting for the pictures
        # to repeat, which over Wi-Fi took 4 to 14 s. A list that keeps only its visible rows shows nothing past
        # its edge either way: it is scrolled as before, until the pictures stop changing.
        beyond = bool(to) and direction in ("up", "down") and self._more_beyond(direction, bounds)
        other = {"up": "down", "down": "up"}.get(direction)
        if to and other and not beyond and self._more_beyond(other, bounds):
            # The tree shows this list's rows past its other edge, so it would show them past this one too: there
            # are none, so the list is already at its start (or end). No drag, and no wait for pictures to repeat
            # (13 s over Wi-Fi, 2026-11-11). (A list that keeps only its visible rows shows none either way and
            # is scrolled as before.)
            return Scrolled(f"{area.line(actions=False, place=False) if area else 'The screen'} is already at its {to}.")
        self._dry(a, f"Would scroll {direction}{f' to the {to}' if to else ''}: swipe from ({x0:g}, {y0:g}) to ({x1:g}, {y1:g}).")
        loop = asyncio.get_running_loop()
        began, previous, note = loop.time(), self._mark()[1], ""
        for swipes in range(1, MAX_SCROLL_SWIPES + 1):
            seq, since = self._mark()
            await s.scroll(x0, y0, x1, y1)
            if not to or swipes == MAX_SCROLL_SWIPES:
                break
            await self._settle(seq, cap=RESOLVE_SETTLE, since=since)
            settled = self._mark()[1]
            if beyond:
                await self._silent_read(view)
                if not self._more_beyond(direction, bounds):
                    break  # the tree said there was more past that edge, and now there is none: reached
            if alike(settled, previous):
                break  # the swipe left the screen as the one before did: the end of the list
            previous = settled  # (with no stream there is nothing to compare: every swipe is made)
            if loop.time() - began >= SCROLL_CAP:
                # the screen still changed after every swipe: more may be loading, or the list is long
                note = f"the {to} wasn't reached in {SCROLL_CAP:g} s; scroll again to go on."
                break
        done = Scrolled(f"Scrolled {area.line(actions=False, place=False) if area else 'the screen'} {direction}.")
        done.note = note
        return done

    def _more_beyond(self, direction: str, bounds: tuple[float, float, float, float]) -> bool:
        """Does the last read have elements past the scroll area's top (`up`) or bottom (`down`) edge, within its
        width? What a list holds beyond its edge is in the tree (React Native renders it, UIKit keeps nearby rows);
        an empty answer means its start (or end) is on screen."""
        x, y, w, h = bounds
        for e in getattr(self.session, "offscreen", []):
            if e.x + e.width <= x or e.x >= x + w:
                continue  # beside the area (a carousel's cards), not past its edge
            # by its middle: a row that straddles the edge with its middle past it is still past it (seen over
            # Wi-Fi 2026-11-11: Bottom at y 915 to 963 on a 932 pt screen, and the scroll stopped one drag short)
            if direction == "up" and e.center[1] < y:
                return True
            if direction == "down" and e.center[1] >= y + h:
                return True
        return False

    def _anywhere(self, view, target: ui.Target) -> ui.Element:
        """The element a target names, on the agent's list or, failing that, among what the last read found off
        screen. Raises as `resolve` does when it is in neither."""
        off = getattr(self.session, "offscreen", [])
        if target.text is not None:
            # {text} is an exact match first, then a part of one: an element off screen that is exactly the text
            # comes before one on screen that only contains it (seen on the phone 2026-11-10: "Alert" found the
            # state line, which reads "… alert=none …", and the Alert button below the screen was never reached)
            want = ui._norm(target.text)
            if (not any(want in (ui._norm(e.label), ui._norm(e.value)) for e in view.items)
                    and any(want in (ui._norm(e.label), ui._norm(e.value)) for e in off)):
                return ui.resolve(sorted([*view.items, *off], key=lambda e: (e.y, e.x)), target)
        try:
            return ui.resolve(view.items, target)
        except Refused as e:
            if e.code != "not_found" or target.ref is not None:
                raise
            # on screen and off it together, in reading order: an `index` counts through both, and several
            # matches without one are `ambiguous` (the first off-screen one may lie the other way)
            everything = sorted([*view.items, *off], key=lambda e: (e.y, e.x))

            return ui.resolve(everything, target)

    async def _scroll_to(self, view, target: ui.Target) -> Scrolled:
        """`scroll` with `to: "target"` (AGENT-TOOLS v1.6): drag the scroll view that holds the element until the
        element is on screen and clear of the bars. The tree gives the place of an element that is off screen, and
        an eased drag doesn't slide on, so each drag covers a known distance; the screen is read after each one,
        and the next drag covers what is left. It stops when the element is clear, when a drag no longer moves it
        (the end of the list), or at the limits `to: "end"` has. A row a lazy list hasn't made yet is `not_found`."""
        s = self.session
        loop = asyncio.get_running_loop()
        began, note, where, clear = loop.time(), "", None, False
        seen: list[tuple[float, ...]] = []  # where the element was before each drag
        for drags in range(MAX_SCROLL_SWIPES + 1):
            try:
                element = self._anywhere(view, target)
            except Refused:
                if seen:  # the screen has moved under the agent's list, and this answer carries none
                    view.moved = True
                raise
            area = ui.scroll_holding(view.items, element, s.screen)
            where = where or (area.line(actions=False, place=False) if area else "the screen")
            top, bottom = s.last_insets
            band = (SCROLL_TOP_INSET if top is None else top,
                    s.screen[1] - (SCROLL_BOTTOM_INSET if bottom is None else bottom))
            x, y, w, h = area.rect if area else (0.0, 0.0, *s.screen)
            y, h = max(y, band[0]), min(y + h, band[1]) - max(y, band[0])
            if h <= 0:  # the scroll view itself lies under the bars: nothing to drag in
                note = "it doesn't scroll further: the target is as far into view as it gets."
                break
            move = ui.scroll_move(element.rect, (x, y, w, h))
            # after a drag, inside the view is enough: chasing the last points of margin with a drag that can't
            # be shorter than SCROLL_MIN_DRAG would go back and forth
            if move == (0.0, 0.0) or (seen and ui.scroll_move(element.rect, (x, y, w, h), margin=0.0) == (0.0, 0.0)):
                clear = True
                break
            if any(ui._same_rect(was, element.rect) for was in seen):
                note = "it doesn't scroll further: the target is as far into view as it gets."
                break
            if loop.time() - began >= SCROLL_CAP:
                note = f"the target wasn't reached in {SCROLL_CAP:g} s; scroll again to go on."
                break
            if drags == MAX_SCROLL_SWIPES:
                note = f"the target wasn't reached in {MAX_SCROLL_SWIPES} drags; scroll again to go on."
                break
            seen.append(element.rect)
            await s.scroll(*ui.scroll_step(move, (x, y, w, h)))
            await self._silent_read(view)  # once the screen is still: where the element is now
        line = element.line(ref=bool(element.ref), actions=False, place=False)
        if clear and not seen:  # nothing was dragged: "Scrolled" would read as if the layout had moved
            return Scrolled(f"{line} is already on screen.")
        done = Scrolled(f"Scrolled {where} to {line}." if clear else f"Scrolled {where} toward {line}.")
        done.note = note
        return done

    async def _outside_focus(self, view, name: str, target: ui.Target, keyboard_top: float,
                             shown_inputs: bool = False) -> bool:
        """Is the touch's target something other than the focused input, so the keyboard is in the way of it
        (AGENT-TOOLS v1.4)? False for the focused input itself, an element in an OS alert, a point on the
        keyboard (a key), and whatever the tool is about to refuse (nothing is touched then). False too for
        `type_text` into another input that shows above the keyboard: a tap there moves the focus, as a person's
        does when filling a form, and closing the keyboard between two fields cost two seconds a field (measured
        on the phone 2026-11-10: 3.4 s against 1.7 s). `shown_inputs` asks about those as well."""
        items = view.items
        if target.point is not None:
            x, y = target.point
            if name != "type_text" and y >= keyboard_top:
                return False
            if any(e.in_alert and e.x <= x < e.x + e.width and e.y <= y < e.y + e.height for e in items):
                return False
            field = ui.at_point(items, target.point, "input")
        else:
            try:
                field = ui.resolve(items, target)
            except Refused:
                return False
            if isinstance(field, tuple) or field.in_alert:
                return False
        if name == "type_text" and (field is None or field.role != "input"):
            return False  # it refuses typing into this itself
        if name == "type_text" and not shown_inputs and keyboard_top - field.y >= MIN_VISIBLE:
            # at least 10 pt of it show above the keyboard: a person taps that part and focus moves (AGENT-TOOLS
            # §4, v1.6; the lab's second field shows 17 pt, and closing first cost 2 s there)
            return False
        return field is None or not await self.session.is_focused(field)  # a non-input is outside by definition

    async def _close_keyboard(self, view, name: str, target: ui.Target | None, shown_inputs: bool = False) -> str:
        """Before a touch (or a focusing tap) on something other than the focused input while the keyboard is up:
        close it the way a person does, since the first tap outside the field only closes it and the keyboard
        covers what is near the bottom. One touch where nothing responds, then WebDriverAgent's own dismissal
        if it is still up; neither presses return. Gives the note when it ended up closed, else leaves the
        screen to the usual `covered` rules."""
        s = self.session
        if name not in KEYBOARD_TOOLS or target is None or not view.read_keyboard or view.alert_now is not None:
            return ""  # (an OS alert in front is modal: a touch outside it closes nothing, and behind_alert decides)
        top, bottom = s.last_insets
        top = SCROLL_TOP_INSET if top is None else top
        bottom = s.screen[1] - (SCROLL_BOTTOM_INSET if bottom is None else bottom)
        if not await self._outside_focus(view, name, target, bottom, shown_inputs):
            return ""
        return await self._dismiss_keyboard_now(view)

    async def _dismiss_keyboard_now(self, view) -> str:
        """Close the keyboard the way a person does: one touch where nothing responds, then WebDriverAgent's own
        dismissal if it is still up; neither presses return. NOTE_KEYBOARD when it ended up closed, else ""."""
        s = self.session
        top, bottom = s.last_insets
        top = SCROLL_TOP_INSET if top is None else top
        bottom = s.screen[1] - (SCROLL_BOTTOM_INSET if bottom is None else bottom)
        # what an open layer hides takes touches too, once it closes (an AutoFill menu hid a card that a "blank" touch
        # then opened, 2026-11-12): keep away from those as well
        hidden = [e for e, _ in getattr(s, "behind", []) if e.actions]  # (element, layer) pairs
        if (blank := ui.blank_point([*view.items, *hidden], s.screen, top, bottom)) is not None:
            await s.tap(ui.Target(x=blank[0], y=blank[1]))
            await self._silent_read(view)
        if view.read_keyboard and (drag := self._dismiss_drag(view.items, top, bottom)) is not None:
            # many lists close the keyboard when dragged (keyboardDismissMode on-drag, as a person flicks a list
            # down to hide it); a short pull down at its top moves the content at most a little, and back
            await s.swipe(*drag, duration_ms=250)
            await self._silent_read(view)
        app = self._bundle()
        # WebDriverAgent's dismissal looks for a key that hides the keyboard; an iPhone keyboard often has none, and the
        # search takes about 4 s: once it failed in an app, it isn't tried there again this session
        if view.read_keyboard and app not in self._no_dismiss:
            if await s.dismiss_keyboard():
                await self._silent_read(view)
            elif app is not None:
                self._no_dismiss.add(app)
        return "" if view.read_keyboard else NOTE_KEYBOARD

    @staticmethod
    def _dismiss_drag(items: list[ui.Element], top: float, bottom: float) -> tuple[float, float, float, float] | None:
        """A short pull down near the top of the biggest scroll view above the keyboard (x0, y0, x1, y1), or None."""
        views = [e for e in items if e.role == "scroll" and not e.in_alert and e.y + 60 < bottom and e.y + e.height > top]
        if not views:
            return None
        v = max(views, key=lambda e: e.width * e.height)
        x, y = v.x + v.width / 2, max(v.y, top) + 20
        return x, y, x, min(y + 40, bottom - 10)

    def _under_keyboard(self, target: ui.Target) -> bool:
        """Is the target an element the last read found past its scroll view's edge but still on the phone's
        screen: hidden by the keyboard rather than scrolled away?"""
        try:
            hit = ui.resolve(getattr(self.session, "offscreen", []), dataclasses.replace(target, index=0))
        except Refused:
            return False
        return isinstance(hit, ui.Element) and 0 <= hit.y < self.session.screen[1] and hit.y + hit.height > 0

    @staticmethod
    def _refuse_behind_alert(view, force: bool = True) -> None:
        """When the newest read shows an OS alert: `behind_alert`. `force` says whether passing it could help
        (it can't touch an element that isn't found)."""
        if view.alert_now is not None:
            title, buttons = view.alert_now
            alert = " ".join([json.dumps(title, ensure_ascii=False), *(f"[{b}]" for b in buttons)])
            raise Refused("not_actionable", f"behind_alert: an alert covers it: {alert}; answer it with alert, "
                                            + ("or pass force: true." if force else "or ask the user."))

    def _refuse_behind_sheet(self, target: ui.Target) -> None:
        """A target on no listed element but on one an open sheet (or another layer) hides: `covered`, naming what
        is in front, not `not_found`. It isn't on the list, so `force` has nothing to touch."""
        behind = getattr(self.session, "behind", [])
        try:
            hit = ui.resolve([e for e, _ in behind], dataclasses.replace(target, index=0))
        except Refused:
            return
        for e, front in behind:
            if e is hit:
                raise Refused("not_actionable", f"covered: {hit.line(ref=False, actions=False)} is behind "
                                                f"{self._front_line(front)}; close that first.")

    def _front_line(self, front: ui.Element | None) -> str:
        """The line of what is open in front, with the ref the agent's list has for it (the read that found it
        numbered it for itself); with no ref when the agent's list doesn't have it."""
        if front is None:
            return "what is open in front of it"
        plain = front.line(ref=False, actions=False)
        known = next((e for e in self.session.items if e.line(ref=False, actions=False) == plain), None)
        return known.line(actions=False) if known is not None else plain

    def _still_open(self) -> str:
        """For a `back` that changed nothing: what is open on top, if the last read found a layer in front."""
        behind = getattr(self.session, "behind", [])
        if not behind:
            return "."
        front = behind[0][1]
        named = self._front_line(front) if front is not None else "a sheet or menu"
        return f"; {named} is still open: tap its close control."

    def _refuse_off_screen(self, target: ui.Target) -> None:
        """A target on no listed element but on one the last read found past the screen (or its scroll view's
        edge): `off_screen`, not `not_found`. A touch can't reach it, so `force` doesn't either."""
        try:
            hit = ui.resolve(getattr(self.session, "offscreen", []), dataclasses.replace(target, index=0))
        except Refused:
            return
        if isinstance(hit, ui.Element):
            raise Refused("not_actionable", f"off_screen: {hit.line(ref=False, actions=False)} is off screen; "
                                            f"{SCROLL_HINT}")

    @staticmethod
    def _refuse_disabled(element: ui.Element) -> None:
        if not element.enabled:
            raise Refused("not_actionable", f"disabled: {element.line(actions=False)} is disabled.")

    def _guard(self, view, element: ui.Element) -> None:
        """A9: refuse what a real touch couldn't do. The reason word comes first in the message."""
        self._refuse_disabled(element)
        if not element.in_alert:
            self._refuse_behind_alert(view)

    @staticmethod
    def _dry(a: dict, line: str) -> None:
        """For a dry run: stop here with what would be done (docs/AGENT-TOOLS.md 11.4). Nothing was sent."""
        if a.get("dry_run"):
            raise DryRun(line)

    def _element_at(self, point: tuple[float, float], items: list[ui.Element] | None = None) -> ui.Element | None:
        """The front-most element of the agent's list (or of `items`) whose rect contains the point (a later one is
        drawn over an earlier one), or None."""
        x, y = point
        return next((e for e in reversed(self.session.items if items is None else items)
                     if e.x <= x < e.x + e.width and e.y <= y < e.y + e.height), None)

    def _police(self, tool: str, a: dict, element: ui.Element | None) -> str:
        """The safety policy's say on a touch of `element` (docs/AGENT-TOOLS.md 11): nothing when it has no word,
        `not_allowed: blocked` for a block word, and for a confirm word `not_allowed: needs_confirmation` (a
        request goes to the live view) unless a person's OK for this very call is waiting. That OK is used up
        here. For a dry run, nothing is asked or used up: the sentence for the answer's end, or "". `force` never
        skips it, and nothing here touches the phone."""
        if element is None:
            return ""
        # an input is matched by its label only: what someone typed into it is data, not what a touch does
        hit = _POLICY.get().check(self._app_bundle(), element.label if element.role == "input" else element.text)
        if hit is None:
            return ""
        kind, word = hit
        what = f"{element.role} {ui.clip(element.text)}"
        if kind == safety.BLOCKED:
            raise Refused("not_allowed", f'blocked: {what} is blocked by the policy ("{word}"); '
                                         "don't try another way.")
        # the element in this app: an OK for "Delete" in one app is no OK for another app's (safety review M1)
        key = (self._app_bundle() or "", *((element.identifier and ("id", element.identifier))
                                           or ("role", element.role, element.label, element.text)))
        now = self.control.clock()
        grant = next((c for c in self._confirms.values()
                      if c.tool == tool and c.key == key and c.state == "allowed" and c.until >= now), None)
        if a.get("dry_run"):
            return "" if grant else f' It needs a person\'s OK (policy word "{word}").'
        if grant is not None:
            del self._confirms[grant.id]
            _GRANTED.get().append(True)
            return ""
        raise Refused("not_allowed", self._ask(tool, a, key, element, word, what, now))

    def _ask(self, tool: str, a: dict, key: tuple[str, ...], element: ui.Element, word: str, what: str,
             now: float) -> str:
        """The `needs_confirmation` message, after asking the live view (once for a request still open). An
        earlier request for this call that was declined, or ran out unanswered, is said at the message's end."""
        for stale in [c for c in self._confirms.values() if c.until < now - CONFIRM_KEEP]:
            del self._confirms[stale.id]
        said, open_ = "", False
        for c in [c for c in self._confirms.values() if c.tool == tool and c.key == key]:
            if c.state == "pending" and c.until >= now:
                open_ = True
                continue
            if c.state == "declined" and c.until >= now:
                # a person said no a moment ago: asking again at once would only wear them down (safety review L2)
                return (f"needs_confirmation: {what} needs a person's OK (policy word \"{word}\"), and they declined "
                        "it just now. Don't ask again by repeating the call: ask your user what they want.")
            if c.state != "allowed":  # an allow nobody used lapses without a word
                said = " They declined." if c.state == "declined" else " Nobody answered."
            del self._confirms[c.id]
        if not open_:
            self._confirm_seq += 1
            # not guessable: an answer must come from someone who was shown the request
            request = Confirm(f"c{self._confirm_seq}-{secrets.token_hex(4)}", tool, key, now + CONFIRM_TTL)
            self._confirms[request.id] = request
            request.message = {"type": "confirm", "id": request.id, "by": "agent", "kind": tool,
                               "caption": step_caption(tool, what, a), "word": word,
                               "expires": int((time.time() + CONFIRM_TTL) * 1000)}
            self.broadcast(request.message)
        return (f"needs_confirmation: {what} needs a person's OK (policy word \"{word}\"). The live view asks them "
                "now; when they allow it, make the same call again. If nobody is watching, ask your user." + said)

    def _answer_confirm(self, message: dict) -> None:
        """A live view's answer to a request. The first one wins; an unknown, answered or expired id is ignored."""
        request = self._confirms.get(message.get("id")) if isinstance(message.get("id"), str) else None
        now = self.control.clock()
        if request is None or request.state != "pending" or request.until < now:
            return
        allow = message.get("allow") is True
        request.state = "allowed" if allow else "declined"
        # an allow is good for one call within CONFIRM_TTL; a decline holds for DECLINE_HOLD, then may be asked again
        request.until = now + (CONFIRM_TTL if allow else DECLINE_HOLD)
        self.broadcast({"type": "confirm_closed", "id": request.id, "allow": allow})

    def _app_bundle(self) -> str | None:
        """The bundle id of the app the last read found in front, for the per-app policy."""
        return (self.session.app or (None, None))[1]

    def _touch_point(self, element: ui.Element) -> tuple[float, float] | None:
        """Where a finger would touch an element a bar (or the keyboard) is drawn over its centre: the middle of
        the part that shows after every overlapping bar is cut away, nearest the centre. None when no bar
        covers the centre. Refuses when too little shows."""
        centre = element.center
        bars = element.covered_by
        width, height = self.session.screen or (0.0, 0.0)
        if height > width and element.rect[1] < STATUS_BAR:
            bars = (*bars, ui.Bar("StatusBar", (0.0, 0.0, width, STATUS_BAR), "the status bar"))
        over = [b for b in bars if b.rect[0] <= centre[0] < b.rect[0] + b.rect[2]
                and b.rect[1] <= centre[1] < b.rect[1] + b.rect[3]]
        if not over:
            return None
        x, y, w, h = ui.visible_part(element.rect, bars, self.session.screen)
        if w >= MIN_VISIBLE and h >= MIN_VISIBLE:
            return x + w / 2, y + h / 2
        line = element.line(actions=False)
        if over[0].kind == "StatusBar":
            raise Refused("not_actionable", f"covered: {line} is under the status bar, where a touch scrolls the "
                                            f"list to its top; {SCROLL_HINT}")
        if over[0].kind == "TabBar":
            raise Refused("not_actionable", f"under_tab_bar: {line} is under the tab bar iOS draws; {SCROLL_HINT}")
        scrolls = any(e.role == "scroll" and ui._holds(e.rect, element.center) for e in self.session.items)
        if over[0].kind == "Keyboard" and not scrolls:  # it stayed up; no scrolling moves a tab bar from under it
            raise Refused("not_actionable", f"covered: {line} is under the keyboard, and this app doesn't close it on a "
                                            f"touch outside the field; {KEYBOARD_HINT}")
        if over[0].kind in ui.COVERING_BARS:
            raise Refused("not_actionable", f"covered: {line} is covered by {over[0].line}; {SCROLL_HINT}")
        raise Refused("not_actionable", f"covered: {line} is covered by {over[0].line}; move or close it first.")

    async def _touch(self, view, a: dict, hold_ms: int) -> str:
        """`tap` and `long_press`: refuse a disabled element or one behind an alert, and touch the part of an
        element under a bar that shows. `force` skips all of it. A point is a raw touch."""
        s, target = self.session, _target(a)
        verb = "Long-pressed" if hold_ms > 300 else "Tapped"
        found = ui.resolve(s.items, target)
        tool = "long_press" if hold_ms > 300 else "tap"
        word = "long-press" if hold_ms > 300 else "tap"
        if isinstance(found, tuple):  # the policy looks at the element under the point, if the list has one
            if not a.get("force") and self._moved_under(view, *found):
                x, y = (round(c) for c in found)
                raise Refused("stale", f"the screen around ({x}, {y}) changed since your last look; "
                                       "look again, or pass force: true to touch there anyway.")
            under = self._element_at(found)
            if any(_POLICY.get().words(self._app_bundle())) and (view.moved or not self._screen_unchanged(view)):
                # the agent's list can't vouch for what is under the point (it acted without a list since, or the
                # picture moved): read what is there now, so a policy word that came in is seen (safety review H2)
                under = self._element_at(found, (await s.refresh()).items)
            said = self._police(tool, a, under)
            self._dry(a, f"Would {word} at {found[0]:g},{found[1]:g}.{said}")
            return ui.without_place(await s.tap(target, hold_ms=hold_ms))
        self._refuse_disabled(found)  # even `force` doesn't touch what the app has switched off
        if a.get("force"):
            said = self._police(tool, a, found)  # `force` skips the checks above, never the policy
            self._dry(a, f"Would {word} {_brief(found)} at {round(found.center[0])},{round(found.center[1])}.{said}")
            done = Noted(ui.without_place(await s.tap(target, hold_ms=hold_ms)))  # at its centre, whatever is drawn there
            done.note = self._forced(view, found)
            return done
        self._guard(view, found)
        point = self._touch_point(found)
        said = self._police(tool, a, found)
        at = point or found.center
        self._dry(a, f"Would {word} {_brief(found)} at {round(at[0])},{round(at[1])}.{said}")
        if point is None:
            return ui.without_place(await s.tap(target, hold_ms=hold_ms))
        await s.tap(ui.Target(x=point[0], y=point[1]), hold_ms=hold_ms)
        return f"{verb} {found.line(actions=False, place=False)} at its visible part ({round(point[0])}, {round(point[1])})."

    def _moved_under(self, view, x: float, y: float) -> bool:
        """Has the picture around a point changed since the list the agent last had? A touch by position is aimed
        at what that list's moment showed there; if something moved in (a sheet, a banner, a scrolled row) the
        touch would land on something else. Only a fresh stream frame against the frame of the agent's last read
        can tell; without both, or after the agent chose an answer without a list (`look: false`), it can't, and
        the touch goes ahead as before."""
        (_, was), now = view.read_mark, self.hub.latest
        if view.moved or was is None or now is None or now is was or self.hub.now_ms() - now.at > FRESH_FRAME * 1000:
            return False
        a, b = was.thumb, now.thumb
        w, h = self.session.screen
        if a is None or b is None or a.size != b.size or (a.width > a.height) != (w > h):
            return False
        sx, sy = a.width / w, a.height / h
        box = (max(0, int((x - STALE_AREA) * sx)), max(0, int((y - STALE_AREA) * sy)),
               min(a.width, int((x + STALE_AREA) * sx) + 1), min(a.height, int((y + STALE_AREA) * sy) + 1))
        if box[2] <= box[0] or box[3] <= box[1]:
            return False
        return ImageChops.difference(a.crop(box), b.crop(box)).getextrema()[1] > ALIKE_TOLERANCE

    def _forced(self, view, element: ui.Element) -> str:
        """The note for a touch made with `force` at the centre of an element that the checks would have refused,
        or whose centre a bar is drawn over (there the unforced touch goes to the part that shows): with a real
        touch, `force` doesn't make the hidden element act. Empty when nothing was in the way."""
        reason = ""
        try:
            if not element.in_alert:
                self._refuse_behind_alert(view)
        except Refused as e:
            reason = str(e).split(":", 1)[0]
        try:
            if not reason and self._touch_point(element) is not None:
                centre = element.center
                over = [b for b in element.covered_by if b.rect[0] <= centre[0] < b.rect[0] + b.rect[2]
                        and b.rect[1] <= centre[1] < b.rect[1] + b.rect[3]]
                reason = "under_tab_bar" if over and over[0].kind == "TabBar" else "covered"
        except Refused as e:
            reason = str(e).split(":", 1)[0]
        return NOTE_FORCED.format(reason=reason) if reason else ""

    async def _tap(self, view, a: dict) -> str:
        return await self._touch(view, a, TAP_MS)

    async def _alert(self, view, a: dict) -> str:
        """`alert`: `accept` presses an alert's main button. An app's own alert ("Delete trip?" [Cancel] [Delete]) is
        an alert too, so a button with a policy word goes through the policy, as a tap on it would (safety review
        H3). Permission prompts' Allow carries none. `dismiss` and `text` are never held back."""
        if a.get("action") == "accept":
            alert = getattr(self.session, "last_alert", None)
            for button in (alert.buttons if alert is not None else ()):
                if button.strip().lower() not in DISMISS_WORDS:
                    self._police("alert", a, ui.Element(ref="", role="button", label=button))
        return await ACTIONS["alert"][0](self.session, a)

    async def _swipe(self, view, a: dict) -> str:
        """`swipe`: a drag that barely moves is a touch where it starts, so it goes through the policy as a tap there
        would (safety review H3)."""
        x0, y0, x1, y1 = (float(a[k]) for k in ("x0", "y0", "x1", "y1"))
        if math.hypot(x1 - x0, y1 - y0) < SWIPE_IS_TAP and any(_POLICY.get().words(self._app_bundle())):
            under = self._element_at((x0, y0), (await self.session.refresh()).items)
            self._police("swipe", a, under)
        return await ACTIONS["swipe"][0](self.session, a)

    async def _long_press(self, view, a: dict) -> str:
        return await self._touch(view, a, int(float(a.get("seconds", 1.0)) * 1000))

    async def _toggle(self, view, a: dict) -> str:
        target = _target(a)
        if target.point is not None:
            x, y = target.point
            found = ui.at_point(view.items, target.point, "switch")
            if found is None:
                raise Refused("not_actionable", f"no_action: Nothing at ({round(x)}, {round(y)}) takes a toggle.")
        else:
            found = self._switch_first(view.items, target)
        line = found.line(actions=False)
        if found.role != "switch" or found.checked is None:
            raise Refused("not_actionable", f"no_action: {line} is not a switch")
        self._refuse_disabled(found)
        forced, point, shown = "", None, None
        if a.get("force"):
            forced = self._forced(view, found)
        else:
            self._guard(view, found)
            point = shown = self._touch_point(found)
            if found.width > WIDE_SWITCH * found.height:
                point = self._switch_control(view.items, found, shown)
        want = a.get("value")
        if want is not None and found.checked == want:
            return Unchanged(f"{found.line(actions=False, place=False)} is already {'true' if want else 'false'}.")
        said = self._police("toggle", a, found)
        at = point or found.center
        self._dry(a, f"Would toggle {_brief(found)} at {round(at[0])},{round(at[1])}.{said}")
        await self.session.tap(ui.Target(ref=found.ref) if point is None else ui.Target(x=point[0], y=point[1]))
        # Until the tree is read, claim no new state: the first line is the verdict's to write
        where = "" if shown is None else f" at its visible part ({round(point[0])}, {round(point[1])})"
        done = Toggled(f"Tapped {dataclasses.replace(found, checked=None, value='').line(actions=False, place=False)}{where}.")
        done.ref, done.before, done.want, done.flipped = found.ref, found, want, not found.checked
        done.note = forced
        return done

    def _switch_control(self, items: list[ui.Element], switch: ui.Element,
                        shown: tuple[float, float] | None) -> tuple[float, float] | None:
        """Where the control of a switch that is a whole row sits (iOS Settings: "Allow Notifications" is one
        element 350x29 at x 40; a tap on its middle lands on the label and toggles nothing, seen on the phone
        2026-11-11). The control is at the end away from the label: the right in a left-to-right layout, the
        left in Arabic or Hebrew. The row's label shows as its own element inside it; without one, the right.
        Partly covered (`shown`, the visible part's middle when the centre is covered): the control's end at that
        height. A bar over the control itself is refused `covered`, as tap refuses: a finger couldn't reach it."""
        x, y, w, h = switch.rect
        label = next((e for e in items if e is not switch and e.label and e.label == switch.label
                      and x <= e.center[0] <= x + w and y <= e.center[1] <= y + h), None)
        leading_right = label is not None and label.center[0] > x + w / 2
        point = (x + SWITCH_FROM_RIGHT if leading_right else x + w - SWITCH_FROM_RIGHT,
                 y + h / 2 if shown is None else shown[1])
        over = next((b for b in switch.covered_by if ui._holds(b.rect, point)), None)
        if over is not None:
            hint = (KEYBOARD_HINT if over.kind == "Keyboard" else SCROLL_HINT if over.kind in ui.COVERING_BARS
                    else "move or close it first.")
            raise Refused("not_actionable", f"covered: {switch.line(actions=False)} is covered by {over.line}; {hint}")
        return point

    @staticmethod
    def _switch_first(items: list[ui.Element], target: ui.Target) -> ui.Element:
        """The element a target names, preferring a switch: a row and its switch often share a label."""
        if target.ref is None:
            try:
                return ui.resolve([e for e in items if e.role == "switch"], target)
            except Refused as e:
                if e.code != "not_found":
                    raise  # several switches match: the agent picks one
        return ui.resolve(items, target)

    async def _back(self, view, a: dict) -> str:
        """Close an open popover by a touch outside it (AGENT-TOOLS v1.4); else tap the navigation bar's back
        button. A bar without one is a root screen (or has only Cancel,
        Done and the like): nothing is tapped. The edge swipe is only for screens with no bar at all."""
        s = self.session
        back = await self._still_back()
        if back.dismiss is not None:  # AGENT-TOOLS v1.4: what is open on top closes first, as "up one screen"
            self._dry(a, "Would close the sheet.")
            await s.tap(ui.Target(x=back.dismiss[0], y=back.dismiss[1]))
            return "Closed the sheet."
        behind = getattr(s, "behind", [])
        # A sheet in front with no region of its own to tap. A back button it hides belongs to the screen behind
        # (it can't be reached, and where the backdrop lets a touch through it would leave that screen with the
        # sheet open); one that shows is the sheet's own and is tapped as usual.
        if behind and not (back.point is not None and not any(ui._holds(e.rect, back.point) for e, _ in behind)):
            self._dry(a, "Would close the sheet in front.")
            return await self._close_sheet(view)
        if back.point is not None:
            self._dry(a, "Would tap the back button.")
            await s.tap(ui.Target(x=back.point[0], y=back.point[1]))
            return "Tapped the back button."
        if back.bar:
            raise Refused("not_actionable", "no_action: the navigation bar has no back button")
        width, height = s.screen
        self._dry(a, "Would swipe from the left edge.")
        await s.swipe(2, height / 2, 0.6 * width, height / 2, 300)
        return "Swiped from the left edge."

    async def _still_back(self) -> ui.Back:
        """Where `back` would touch, read until two reads in a row agree. A sheet that is still sliding in has
        more room above it than it will have: the middle of that room, tapped half a second later, is inside the
        sheet. On the phone that touch landed on an app in the share sheet and opened it (2026-11-10, over Wi-Fi,
        where a read and the touch after it are further apart; Mail came to the front). So nothing is touched
        until the sheet stands still; a sheet that never does is `no_action`."""
        s = self.session

        def where(back: ui.Back) -> tuple:
            behind = getattr(s, "behind", [])
            front = behind[0][1] if behind else None
            box = ui.sheet_box(front, getattr(s, "front_items", None) or [], s.screen) if behind else None
            return back.dismiss, back.point, box
        back = await s.back_point()
        seen = where(back)
        if back.dismiss is None and not getattr(s, "behind", []):
            return back  # nothing is open on top: a bar's back button doesn't slide
        for _ in range(BACK_STILL_TRIES):
            await asyncio.sleep(BACK_STILL_PAUSE)
            back = await s.back_point()
            now = where(back)
            if now == seen:
                return back
            seen = now
        raise Refused("not_actionable", "no_action: what is open on top is still moving; call back again in a moment")

    async def _close_sheet(self, view) -> str:
        """Close the sheet in front the two ways a person tries from outside it: a touch beside it, on what it
        dims, then pulling it down from its top edge. After each, the screen is read: the sheet is closed when the
        layer in front is gone or is another one. A sheet that takes neither is `no_action`, naming it."""
        s = self.session

        def front() -> str | None:
            behind = getattr(s, "behind", [])
            if not behind:
                return None
            return behind[0][1].line(ref=False, actions=False) if behind[0][1] is not None else ""
        was = front()
        box = ui.sheet_box(getattr(s, "behind")[0][1], getattr(s, "front_items", None) or view.items, s.screen)
        if box is not None:
            top, bottom = s.last_insets
            beside = ui.beside_sheet(box, s.screen, SCROLL_TOP_INSET if top is None else top,
                                     s.screen[1] - (SCROLL_BOTTOM_INSET if bottom is None else bottom))
            if beside is not None:
                await s.tap(ui.Target(x=beside[0], y=beside[1]))
                await self._silent_read(view)
                if front() != was:
                    return "Closed the sheet."
            if (pull := ui.sheet_pull(box, s.screen)) is not None:
                await s.swipe(*pull, 300)
                await self._silent_read(view)
                if front() != was:
                    return "Closed the sheet."
        raise Refused("not_actionable", "no_action: nothing changed after back" + self._still_open())

    async def _settle_pending(self) -> None:
        """XCTest no longer waits for the app to go idle, so after an action that wasn't settled (`look: false`)
        nothing may act or read until the screen is still (cap RESOLVE_SETTLE)."""
        if self.unsettled:
            await self._settle(self._mark()[0], cap=RESOLVE_SETTLE)
            self.unsettled = False

    async def _silent_read(self, view) -> bool:
        """Read the live tree for target resolution, once the screen is still (cap RESOLVE_SETTLE; with no
        frames that is a `settle_fallback` wait, 0.6 s). Read mid-redraw, an element can be briefly missing.
        True when a re-read could show something else: the stream moved, ended capped, or has no frames."""
        s = self.session
        done = await self._settle(self._mark()[0], cap=RESOLVE_SETTLE)
        self.unsettled = False
        mark = self._mark()  # taken before the read: a frame that changes meanwhile counts against the list
        tree = await s.refresh()
        view.alert_now = _alert_key(tree.alert)  # the alert as this read shows it, not as the last answer did
        view.read_keyboard = tree.keyboard
        known = {e.ref for e in view.items}  # what came back may get its old ref: look in both lists
        held = sorted([*view.items, *(e for e in view.shown if e.ref not in known)], key=lambda e: (e.y, e.x))
        # no action of the agent's came before this read: the content moved only if the elements say so
        view.items = changes.carry_refs(held, s.items, view.top_ref, moves_content=None)
        view.top_ref = max(view.top_ref, changes.highest(view.items))
        view.read_at, view.read_mark = self.control.clock(), mark  # the next target in the same window needn't read again
        s.items = view.items
        return done.capped or mark[1] is None or not alike(self._mark()[1], mark[1])

    @staticmethod
    def _missing(items: list[ui.Element], target: ui.Target) -> bool:
        """Does the target resolve to `not_found` (not `ambiguous`) on this list?"""
        try:
            ui.resolve(items, target)
        except Refused as e:
            return e.code == "not_found"
        return False

    async def settle(self, after_seq: int, still: float | None = None, cap: float = CAP,
                     since: Frame | None = None, early: int | None = None) -> tuple[float, bool]:
        """(seconds waited, changed): see _settle."""
        done = await self._settle(after_seq, still, cap, since, early)
        return done.seconds, done.changed

    async def _settle(self, after_seq: int, still: float | None = None, cap: float = CAP,
                      since: Frame | None = None, early: int | None = None) -> Settled:
        """Wait for the screen to be still, as the stream shows it. `after_seq` is the newest frame when the
        phone call returned: the settle ends only on a later frame, since earlier ones may predate the effect.
        WebDriverAgent streams continuously even when nothing changes, so a silence is a stall, not stillness.

        `early` is the newest frame when the gesture went out. A tap's call returns about 200 ms after its effect
        shows, so the frames in between count too, but only once one of them is unlike `since`: they can then
        already hold the still run, and the settle ends at the first frame after the return that continues it.

        Still: at least 2 frames alike (stream.alike) spanning `still` seconds (at least 2.5 median frame gaps,
        so a slow stream isn't called still too soon). Each frame is compared with the first of its run, so a
        slow fade, close to the last frame every time, is not still. `changed`: a frame is unlike the one at the
        start of the action (`since`, from _mark; None means there was no frame, so any frame counts).
        `changed` is False only when the answer is certain: 3 frames, all like `since`, and `quiet` seconds.
        A stall, no frames at all (it sleeps `settle_fallback` in total, first-frame wait included), too
        few frames, or the cap all report changed=True, so the tree gets read."""
        gap = self.hub.gap()
        still = max(self.settle_still if still is None else still, 2.5 * gap if gap else 0.0)
        stall = max(3 * gap if gap else 0.0, still)
        loop = asyncio.get_running_loop()
        began = loop.time()
        deadline = began + cap
        seq, differs, run, run_start, first_of_run = after_seq, False, 0, began, None
        if early is not None:
            last = None
            for arrived, frame in self.hub.since(early, upto=after_seq):
                differs = differs or not alike(frame, since)
                if alike(frame, first_of_run):
                    run += 1
                else:
                    run, run_start, first_of_run = 1, arrived, frame
                last = arrived
            if not differs:  # nothing showed during the call: only frames after the return count
                run, run_start, first_of_run = 0, began, None
            elif (gap and last is not None and run >= 2 and began - run_start >= still
                  and began - last <= 1.5 * gap):
                return Settled(0.0, True, False)  # changed, then still up to a fresh frame at the return
        while (left := deadline - loop.time()) > 0:
            first = seq == after_seq
            wait = min(left, self.settle_first_frame if first else stall)
            frame = await self.hub.next_after(seq, timeout=wait)
            now = loop.time()
            if frame is None:
                if not first and wait == left:
                    break  # the cap came before the stall did
                if first:  # no frames at all: the stream is down or still opening
                    await asyncio.sleep(max(0.0, self.settle_fallback - (now - began)))
                return Settled(loop.time() - began, True, False)  # or a stall
            seq = frame.seq
            differs = differs or not alike(frame, since)
            if alike(frame, first_of_run):
                run += 1
            else:
                run, run_start, first_of_run = 1, now, frame
            if run >= 2 and now - run_start >= still:
                if differs or (seq - after_seq >= 3 and now - began >= self.settle_quiet):
                    return Settled(now - began, differs, False)
        return Settled(loop.time() - began, True, differs)

    def _mark_read(self, view) -> None:
        """A tree read starts: note the stream's picture (its SHA-1 and, if it's recent, its bytes), so a
        screenshot shows the moment the list is of, not a later one."""
        view.read_mark = self._mark()
        frame = self.hub.latest
        fresh = frame is not None and self.hub.now_ms() - frame.at <= self.shot_fresh * 1000
        view.read_frame = frame if fresh else None

    def _mark(self) -> tuple[int, Frame | None]:
        """(seq, the newest frame): where the stream stood at this moment."""
        frame = self.hub.latest
        return (frame.seq, frame) if frame else (0, None)

    def _shot_args(self, args: dict, *, default: bool, custom: bool) -> tuple[int, bool] | None:
        """(width, labels) when a screenshot is wanted, else None. Only `look` takes width and labels;
        an action's screenshot uses look's defaults (AGENT-TOOLS §3.5)."""
        if not custom:
            return (shotlib.DEFAULT_WIDTH, False) if args.get("screenshot", default) else None
        width = args.get("width", shotlib.DEFAULT_WIDTH)
        if isinstance(width, bool) or not isinstance(width, (int, float)) or width != int(width):
            raise Refused("bad_params", "width must be a whole number of pixels")
        if not shotlib.MIN_WIDTH <= width <= shotlib.MAX_WIDTH:
            raise Refused("bad_params", f"width must be {shotlib.MIN_WIDTH}-{shotlib.MAX_WIDTH} pixels, not {width:g}")
        if not args.get("screenshot", default):
            return None
        return int(width), bool(args.get("labels", False))

    async def _newest_frame(self) -> Frame | None:
        """The newest stream frame if it's recent, else the next one if it comes soon, else None."""
        latest = self.hub.latest
        if latest is not None and self.hub.now_ms() - latest.at <= self.shot_fresh * 1000:
            return latest
        return await self.hub.next_after(latest.seq if latest else 0, timeout=self.shot_wait)

    async def _add_shot(self, answer: dict, view, shot: tuple[int, bool] | None, at_read: bool = True) -> dict:
        """Add the picture to an answer: from the stream (full resolution), else the phone's /screenshot.
        A picture that can't be had is a Note line, never an error. Decoding and resizing happen only here,
        so only when a picture was asked for. The picture is the frame from when the tree was read (`at_read`),
        else the newest one (the no-change shortcut read no tree); /screenshot only when there is no frame."""
        if shot is None:
            return answer
        width, labels = shot
        try:
            frame = view.read_frame if at_read else await self._newest_frame()
            try:
                source = await jpeg_of(frame) if frame is not None else None  # a video picture is encoded here
            except PictureGone:  # it was one of the older pictures, whose pixels are let go
                source = None
            turn = getattr(self.session, "turn", 0)
            if source is not None and turn and getattr(frame, "picture", None) is None:
                source = await self.turned(frame.seq, source, turn)  # landscape: the stream's frame comes as captured
            if source is None:
                source = await self._read(self.session.screenshot_png)
            boxes = [(e.ref, *e.rect) for e in view.items if e.actions] if labels else None
            jpeg, size, skipped = await asyncio.to_thread(shotlib.render, source, self.session.screen, width, boxes)
        except Exception as e:
            answer["text"] += f"\nNote: no screenshot: {str(e) or type(e).__name__}"
            return answer
        if skipped:
            answer["text"] += "\nNote: labels skipped: the screen orientation changed."
        answer["image"], answer["image_size"] = base64.b64encode(jpeg).decode(), [size[0], size[1]]
        return answer

    async def _look(self, view, shot: tuple[int, bool] | None, lines: list, notes: list) -> dict:
        async def read():
            self._mark_read(view)
            text, image = await self.session.look(screenshot=False)
            view.items = view.shown = self.session.items
            view.shown_off = self._off_screen_now()
            view.top_ref, view.app, view.seen = changes.highest(view.items), self.session.app, self.control.generation
            view.alert, view.keyboard = _alert_key(self.session.last_alert), self.session.last_keyboard
            view.read_keyboard = view.keyboard
            view.alert_now = view.alert
            view.read_at, view.moved, view.dropped = self.control.clock(), False, False
            view.unlooked = 0
            return text, image
        try:
            text, image = await self._read(read)
        except Exception as e:
            if not lines:
                raise
            view.moved = True  # no list came back: the screen has probably changed
            return _answer(lines, [*notes, f"couldn't look afterwards: {e}."])
        return await self._add_shot(_answer([*lines, text], notes, image), view, shot)

    async def _after(self, view, result: str, notes: list, full: bool, moves_content: bool | None = False,
                     seconds: float | None = None, snap: Snapshot | None = None, drifted: bool = False) -> dict:
        """An action's answer: its result line, then what changed on screen since the agent's last answer
        (`view.shown`), or the full list when the app changed or `full`, then `Settled in` (left out when
        `seconds` is None: `wait` did nothing to settle), then the notes. Refs follow their elements."""
        s = self.session
        tail = [] if seconds is None else [settled_line(seconds)]
        app_before, listed_before = view.app, view.shown

        async def read():
            self._mark_read(view)
            read_now = snap or await s.snapshot()  # `snap`: a read the caller already made (wait_for's last poll)
            s.items = read_now.tree.items
            app_changed = read_now.app is not None and view.app is not None and read_now.app != view.app
            head = _furniture(view, read_now.tree.alert, read_now.tree.keyboard)
            up = changes.update(view.items, read_now.tree.items, app_changed, view.top_ref, view.shown, full, head,
                                moves_content)
            return read_now, up
        try:
            snap, up = await self._read(read)
        except Exception as e:
            view.moved = True  # no list came back: the screen has probably changed
            return _answer([result, *tail], [*notes, f"couldn't look afterwards: {e}."])
        covered = not snap.tree.items and bool(view.shown)  # the agent saw elements; now there are none
        s.items = view.items = view.shown = up.items
        view.top_ref, view.app = up.top, snap.app or view.app
        view.alert, view.keyboard = _alert_key(snap.tree.alert), snap.tree.keyboard
        view.read_keyboard = view.keyboard
        view.alert_now = view.alert
        view.seen, view.read_at, view.moved, view.dropped = (self.control.generation, self.control.clock(),
                                                             False, False)
        # what changed off screen (v1.6), against what lay there at the agent's last answer
        off_was, view.shown_off = view.shown_off, self._off_screen_of(snap.tree)
        # ...unless the old list is no baseline: another app is in front now, or the full list was forced (a
        # person acted, the line dropped, the agent had never looked). A place only says "the same element" when
        # nothing can have moved the content since the agent's last answer.
        same_app = not (snap.app is not None and app_before is not None and snap.app != app_before)
        still = not (moves_content is not False or drifted or view.unlooked)
        off = (changes.off_screen(off_was, view.shown_off, by_place=still, listed=listed_before)
               if same_app and not full else [])
        if off and not up.full:
            up.lines = [line for line in up.lines if line != changes.NO_CHANGE] + off
        earlier, view.unlooked = view.unlooked, 0
        if earlier and not up.full:
            # the block is measured from the agent's last list, so it holds those actions' changes too: a
            # first-time agent closed a sheet it had opened with look: false and read "No visible change."
            notes = [*notes, NOTE_SPAN.format(n=earlier + 1)]
        if up.full:
            tree = snap.tree
            head = ui.describe(up.items, s.screen, snap.scale, snap.app, tree.alert, tree.keyboard, tree.more,
                               (tree.above, tree.below))
            # the full list because most of the screen changed: what changed off screen still follows it, under
            # a heading of its own (bare `~` lines after a list read as a second, broken block)
            beyond = [changes.OFF_SCREEN, *off] if off else []
            return _answer([result, head, *beyond, *tail], [*notes, *([NOTE_COVERED] if covered else [])])
        return _answer([result, *up.lines, *tail], notes)

    def _off_screen_now(self) -> list[ui.Element]:
        """What the session's last read found off screen, without what an open sheet hides (as in the header's
        counts). Only right inside the read itself: another client's read may come next."""
        s = self.session
        hidden = {id(e) for e, _ in getattr(s, "behind", [])}
        return [e for e in getattr(s, "offscreen", []) if id(e) not in hidden]

    def _off_screen_of(self, tree: ui.Tree) -> list[ui.Element]:
        """The same from one read's own tree. (A tree a test made by hand has no off-screen list of its own: the
        session's stands in.)"""
        if not tree.offscreen and not tree.behind:
            return self._off_screen_now()
        hidden = {id(e) for e, _ in tree.behind}
        return [e for e in tree.offscreen if id(e) not in hidden]

    def _current(self, view) -> bool:
        """Is the agent's last list still the screen? Not if a person or another agent acted since
        (a different generation), if an action returned no list since, or if it is older than
        LIVE_AGE seconds and the screen may have changed meanwhile."""
        if view.seen != self.control.generation or view.moved or view.dropped:
            return False
        return self.control.clock() - view.read_at < LIVE_AGE or self._screen_unchanged(view)

    def _screen_unchanged(self, view) -> bool:
        """Does the stream show the picture the list was read against? Only a fresh newest frame that
        matches the one at read time says so; with no stream the answer is no, and the list is read again."""
        frame, (_, was) = self.hub.latest, view.read_mark
        if frame is None or was is None or self.hub.now_ms() - frame.at > FRESH_FRAME * 1000:
            return False
        return alike(frame, was)

    async def _read(self, fn):
        """A read through the queue; after a dropped line, once more when reconnected."""
        for attempt in (1, 2):
            try:
                return await self.control.read(lambda: self._connected_only(fn))
            except ConnectionLost as e:
                self.lost(e)
                if attempt == 2:
                    raise Refused("reconnecting", "lost the connection to the phone; try again") from None
                await self.wait_connected()

    def open_confirms(self) -> list[dict]:
        """The `confirm` messages still waiting for an answer, for a viewer that connects now (VIEWER-PROTOCOL 1.5)."""
        now = self.control.clock()
        return [c.message for c in self._confirms.values() if c.state == "pending" and c.until > now and c.message]

    # ---- a person, from the live view ----
    async def viewer_message(self, message: dict) -> dict | None:
        """One message from a live view (docs/VIEWER-PROTOCOL.md). Returns an error message for
        that viewer, or None. Unknown types are ignored."""
        kind = message.get("type")
        try:
            if kind == "pause":
                self.control.set_paused(bool(message.get("on")))
                self.broadcast(self.status())
                return None
            if kind == "quality":
                await self.set_quality(str(message.get("preset")))
                return None
            if kind == "confirm_answer":  # a person's answer to the policy's request (VIEWER-PROTOCOL 1.5)
                self._answer_confirm(message)
                return None
            if kind not in ("gesture", "scroll", "text", "key", "button"):
                return None  # "overlay" comes in part 2; anything else is unknown
            if self.state != "live":
                return {"type": "error", "code": {"locked": "locked", "not-answering": "not_answering"}.get(self.state, "reconnecting"),
                        "message": f"not connected to the phone ({self.detail or self.state})"}
            run, action, point = self._person_action(kind, message)
            self._person_at = time.monotonic()

            async def quickened():
                await self._quicken()  # so the picture after the input comes at full rate (V1)
                return await run()
            await self.control.person(lambda: self._connected_only(quickened))
            self._announce("person", action, point=self._point(point))
            return None
        except Refused as e:
            return {"type": "error", "code": e.code, "message": str(e)}
        except ConnectionLost as e:
            self.lost(e)
            return {"type": "error", "code": "reconnecting",
                    "message": "the connection dropped; your input may not have reached the phone"}
        except (ValueError, KeyError, TypeError, IndexError) as e:
            return {"type": "error", "code": "unsupported", "message": f"bad {kind} message: {e}"}
        except Exception as e:
            return {"type": "error", "message": str(e)[:300]}

    def _person_action(self, kind: str, m: dict):
        """(coroutine function to run, kind for the action message, start point in points)."""
        s = self.session
        if kind == "gesture":
            g = classify(m["points"], s.screen)
            if g.kind == "drag":
                return (lambda: s.path(list(g.points))), "drag", g.points[0][1:]
            _, x, y = g.points[0]
            return (lambda: s.tap(ui.Target(x=x, y=y), hold_ms=g.hold_ms)), g.kind, (x, y)
        if kind == "scroll":
            x0, y0, x1, y1 = ui.scroll_swipe(float(m["x"]), float(m["y"]), float(m.get("dx", 0)),
                                             float(m.get("dy", 0)), s.screen)
            return (lambda: s.scroll(x0, y0, x1, y1)), "scroll", (x0, y0)  # a wheel stops where it should
        if kind == "text":
            text = str(m["text"])
            if not text:
                raise ValueError("no text")
            return (lambda: s.type_text(text)), "text", None
        if kind == "key":
            name = str(m["key"])
            if name not in ("backspace", "enter"):
                raise ValueError(f"unknown key {name!r}")
            return (lambda: s.key(name)), name, None
        name = str(m["name"])
        if name not in BUTTONS:
            raise ValueError(f"unknown button {name!r}")
        return (lambda: s.press_button(name)), name, None

    async def set_quality(self, preset: str) -> None:
        if preset not in PRESETS:
            raise ValueError(f"preset must be one of {', '.join(PRESETS)}")
        if self.state in ("live", "locked"):
            await self._read(lambda: self.session.settings({**PRESETS[preset], **NO_IDLE_WAIT}))
            self._fps = int(PRESETS[preset]["mjpegServerFramerate"])  # full rate; the pace slows it again when still
        self.preset = preset
        if self.video is not None:
            self.video.set_quality(preset)
        self.broadcast(self.status())

    # ---- the stream and shutting down ----
    async def _open_stream(self):
        try:
            await asyncio.wait_for(self._connected.wait(), 30)
        except asyncio.TimeoutError:
            raise ConnectionLost("not connected to the phone yet") from None
        return await self.session.device.open_stream()

    async def start_video(self, tries: int = 3, gap: float = 3.0) -> str:
        """Set up the H.264 live view: the hub, the AirPlay receiver, and Screen Mirroring on the phone, started
        the way the owner would (mirror.py). The app that was in front comes back afterwards. Returns what
        happened in words; it never raises, since the JPEG stream stays as the picture."""
        from . import airplay, mirror, video
        s = self.session
        if not video.available():
            return self._video_note("PyAV isn't installed (pip install tapwright[video])")
        uxplay = airplay.find()
        if uxplay is None:
            return self._video_note("no AirPlay receiver found (install UxPlay, or set TAPWRIGHT_UXPLAY)")
        try:
            scale = s.scale or 3.0
            pixels = (round(s.screen[0] * scale), round(s.screen[1] * scale))
            receiver = airplay.Receiver(uxplay, 0, pixels, self.log)
            hub = video.VideoHub(port=0, screen_width=lambda: s.screen[0], mirroring=lambda: receiver.mirroring,
                                 log=self.log)
            receiver.args = airplay.command(uxplay, hub.port, pixels)
            receiver.start()
            self.video, self._receiver = hub, receiver
            if self.settle_on_video:
                self._start_feeder()
            hub.set_quality(self.preset)
            said = ""
            for attempt in range(tries):
                await asyncio.sleep(gap)  # the receiver takes a moment to show up in the phone's list
                async with self.control.exclusive():
                    self._require_connected()
                    front = await s.foreground_bundle_id()
                    try:
                        said = await mirror.start(s, airplay.NAME)
                    except mirror.MirrorError as e:
                        said = str(e)
                        continue
                    finally:
                        if front and front != "com.apple.springboard":
                            await s.launch(front)  # back to the app that was in front
                break
            for view in self.control.clients.values():
                view.dropped = True  # the phone left the app for a moment: every agent's list is old
            self.broadcast(self.status())
            return self._video_note(said, ok=receiver.mirroring or said.startswith(("Mirroring", "Already")))
        except Exception as e:  # the JPEG stream stays: video is an extra
            return self._video_note(f"couldn't start: {e}")

    def _start_feeder(self) -> None:
        """While `video` is live its pictures feed `hub` (and the MJPEG stream stays closed); when it isn't, the
        MJPEG stream does, as before. The feeder checks which, so mirroring may stop and start again."""
        if self._feeder_task is None and self.video is not None:
            self._feeder = PictureFeeder(self.video, self.hub, log=self.log)
            self._feeder_task = asyncio.create_task(self._feeder.run(), name="feeder")

    def _video_note(self, text: str, ok: bool = False) -> str:
        self.log(f"video: {text}" if ok else f"video: {text}; the live view stays on the JPEG stream")
        return text

    async def close(self) -> None:
        if self._feeder_task is not None:
            self._feeder_task.cancel()
            await asyncio.gather(self._feeder_task, return_exceptions=True)
        await self.pump.stop()
        self.hub.close()
        if self._video_task is not None:
            self._video_task.cancel()
        if self._receiver is not None:
            await asyncio.to_thread(self._receiver.stop)
        if self.video is not None:
            await asyncio.to_thread(self.video.close)  # joins its threads
        try:
            await asyncio.wait_for(self.session.close(), 10)
        except Exception as e:
            self.log(f"closing: {e}")

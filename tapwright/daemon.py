"""The phone side of `tapwright serve`: one Session (tunnel and WebDriverAgent), the control
rules, the stream, the agent tools, a person's input from the live view, and a supervisor
that keeps it all connected. server.py only turns HTTP into calls here."""
import asyncio
import base64
import collections
import contextvars
import copy
import itertools
import io
import json
import dataclasses
import datetime
import hashlib
import math
import os
import pathlib
import re
import secrets
import time
import unicodedata
import urllib.parse
import traceback
from collections.abc import Awaitable, Callable
from contextlib import contextmanager, nullcontext
from typing import NamedTuple

from PIL import Image, ImageChops, UnidentifiedImageError

from . import (accounts as accountlib, actionlog, aio, audit, batch, brief as briefing, bugtool, changes, closest, compare,
               diagnostics, dialog, driving, explorefloor, exploretool, filmstrip as filmlib, journey, journey_tool, launch as launchlib, links,
               mapfile, mapkeeper, maptool, media as medialib, names as namelib, needs, network, ocr, params, performance,
               places, reflex, report, roads, routes, scriptrepair, scriptrepairrun, store, trace, trip, ui, uxcheck)
from . import autoroute, tokentext
from . import unlock as unlocklib, watchdog as watchlib
from . import policy as safety
from . import auditlog, center_tool, grants as grantlib, otp as otplib, schedules as schedulelib
from .actionlog import ActionLog
from . import device as device_module
from . import session as session_module
from . import shot as shotlib
from .control import STILL_WANTED, TAP_MS, Controller, Refused, classify
from .errors import PHONE_LOCKED
from .device import (AppNotAnswering, ConnectionLost, CrashTooBig, DeviceError, LockedBeforeStart, NotSetUp,
                     NotUnlockedSinceRestart, PasscodeNeeded, PhoneLocked, Proc, Unanswered, PhoneNotFound, PASSCODE_WORDS)
from .session import Snapshot
from .tapline import AgentTouches
from .stream import (ALIKE_TOLERANCE, NO_IDLE_WAIT, PRESETS, Frame, FrameHub, PictureFeeder, PictureGone, StreamPump,
                     alike, jpeg_of, turn_jpeg)

PROTOCOL = 1
ACCEPTS = ["tap", "longPress", "drag", "scroll", "text", "enter", "backspace", "home", "volume", "quality"]
BUTTONS = ("home", "volumeUp", "volumeDown")  # the live view's buttons
# press_button's (2.0: `lock` through the hands, CoreDevice HID; XCTest can't press it)
AGENT_BUTTONS = ("home", "lock", "volumeUp", "volumeDown")
PINCH_SCALE = (0.2, 5.0)  # the least and most a pinch may zoom by
PINCH_SECONDS = (0.1, 3.0)
STILL = 0.1  # seconds without a changed frame before a screen counts as still (docs/AGENT-TOOLS.md §6)
RESOLVE_SETTLE = 1.0  # the longest a silent re-read waits for the screen to be still
CAP = 2.0  # the longest an action's answer waits for the screen
LONG_CAP = 4.0  # the same after launching an app or opening a URL
CLOSE_KILL_CAP = 5.0  # the longest close_app waits for iOS's process control to end the app
HOME_STUCK_CAP = 10.0  # press_button home while the app in front is frozen: WebDriverAgent's home screen route
HOME_LOCK_CAP = 2.0  # ... and the lock question asked before it
CLOSE_TERMINATE_CAP = 3.0  # ... and for WebDriverAgent's terminate afterwards, which has nothing left to wait for
LIVE_AGE = 2.0  # seconds a list stays current for id, label and text targets (docs/AGENT-TOOLS.md §2)
FRESH_FRAME = 2.0  # seconds a stream frame counts as "the screen now"
# the agent tools that act, each written to the action log; reads (look, apps, wait_for, viewer) aren't
LOGGED = frozenset({"tap", "long_press", "type_text", "scroll", "toggle", "back", "swipe", "pinch", "open_url", "open_app",
                    "close_app", "alert", "press_button", "rotate", "performance", "logs", "crash", "wait", "network", "media",
                    "unlock"})
# AGENT-TOOLS §16.1: reads are logged too, with "kind": "read" (the ones in LOGGED above, and these)
READ_LOGGED = frozenset({"look", "wait_for", "audit", "ux_check", "apps", "map", "notifications"})  # (compare_screen writes its own line: _log_compare)
READ_KINDS = READ_LOGGED | {"compare_screen", "performance", "logs", "crash"}
BATCH_TOOLS = ("run", "record", "replay", "drive")  # AGENT-TOOLS §9, §18.4: they call the other tools
UNLOCKS_NOTED = 10  # unlocks in an hour above which each one is noted in the log (they are never limited)
# (W185) a connect that launched no runner because of the phone's lock state: a person is needed, and `unlock` can't help
HELD_BEFORE_START = (NotUnlockedSinceRestart, LockedBeforeStart)
UNLOCK_AT_START = ("run", "replay", "drive")  # with serve --phone, these unlock a locked phone before their first step
# `unlock` (1.7.8): the agent's own call; its refusals in fixed words (never a value)
UNLOCK_TOP_ONLY = ("unlock is a call of its own: it can't be a step of run, replay, a route, drive or a journey (a lock "
                   "there ends them; call unlock after, then start again)")
UNLOCK_NO_PASSCODE = ("the phone reads as locked (or Notification Center is open over it), and no passcode is saved for "
                      "this phone: ask the user to unlock it or close Notification Center, or to save the passcode as "
                      "phone.passcode in the accounts file Tapwright is started with (--accounts FILE; docs/setup.md)")
HOME_NOT_PROVEN = ("the phone reads as locked (or Notification Center is open where Tapwright can't prove it): call "
                   "unlock (it types the passcode saved for this phone), or ask the user to unlock the phone or close "
                   "Notification Center. Nothing was pressed.")
UNLOCK_BUSY = ("the phone is being unlocked: no picture of the screen is taken until that ends (its keypad shows each key "
               "as it is touched); try again in a few seconds")
UNLOCK_NOT_YET = ("the phone is locked, and the connection to it isn't up yet, so the passcode can't be typed now; try "
                  "unlock again in a few seconds, or ask the user to unlock it")
FEW_ELEMENTS = 3  # a look with fewer elements than this says that ocr: true can read the picture
NOTE_NO_OCR = "no OCR reading, so this look is the elements alone. " + ocr.INSTALL  # (1.9.0 round 2)
NOTE_FEW = ("this screen has few elements (a game, a canvas or a web view?); look with ocr: true reads the text "
            "in its picture.")
OCR_REF = re.compile(r"o[1-9][0-9]*")
OCR_HEAD = "Text in the picture (OCR; not elements; touch one by its ref, at its centre):"
OCR_NONE = "Text in the picture (OCR): nothing beyond what the elements say."
# (2.0) the calls that need no read of the app: while one blocks even the runner's bounded routes (not-answering),
# Daemon._frozen_tool is where they are answered once hands exist (it answers none yet: refused as in 1.10.0)
FROZEN_TOOLS = frozenset({"look", "tap", "long_press", "swipe", "press_button"})
# (2.0) TAPWRIGHT_RUNNER_OCR=0: text is read from the picture on this computer only (RapidOCR), never by the runner
RUNNER_OCR = os.environ.get("TAPWRIGHT_RUNNER_OCR", "1")
SWIPE_IS_TAP = 10.0  # points: a swipe moving less than this is a touch where it starts
FULL_SWIPE_SHARE = 0.4  # a mostly-across swipe on a list row longer than this share of its width runs its first action
IN_USE_S = 90.0  # for a schedule's busy test only: a client's call, or a person's input, this recent means the phone is in use
SCHEDULE_FREE = frozenset({"look", "wait", "wait_for", "audit", "ux_check", "logs", "crash", "apps", "session_report",
                           "viewer", "filmstrip", "compare_screen"})  # what other agents may still do during a scheduled run
SCHEDULED_BUSY = "a scheduled run is using the phone; try again when it ends"
ALERT_ACTIONS = ("accept", "dismiss", "text", "cancel")  # what `alert` takes (AGENT-TOOLS §4)
DISMISS_WORDS = {"cancel", "don't allow", "not now", "close", "no", "later", "dismiss", "ask app not to track"}
# what str.splitlines() breaks a line at: each is Return to a text field (Codex, PR #12 cycle 2, H3)
LINE_BREAKS = frozenset("\n\r\x0b\x0c\x1c\x1d\x1e\x85  ")
NO_APP_TO_MEASURE = "no app is in front to measure; open_app the app first."
FRESH_WAIT = 30.0  # seconds a replay waits after a fresh start for what its first step aims at
BATCH = contextvars.ContextVar("batch", default=None)  # {id, step} of the batch step running now, for the log
# The script a replay is running (§14.3): {"path": Path, "update": bool}. Checks look for references in the script's own
# folder first, a failed check writes its picture beside the script, and with `update` they save there instead.
SCRIPT = contextvars.ContextVar("script", default=None)
REASONED = frozenset({"not_actionable", "not_allowed"})  # codes whose message starts with a reason word
STALE_AREA = 40.0  # points around a point target that must look as they did in the agent's last list (from PhoneUse)
MIN_QUIET = 0.3  # seconds a screen must stay as it was before "No visible change." is believed
# (2.0, phone 2026-11-26) the same after a touch sent by the hands. XCTest's tap answered about 200 ms after its effect
# showed, so 0.3 s more was 0.5 s from the touch; a HID touch is acked at once, and the first tap into a page after a
# cold start showed nothing for over 0.4 s: it was answered "No visible change." while the page was opening (11 of 11).
HANDS_QUIET = 0.8
HANDS_FRESH = 1.5  # seconds after a hands touch ended within which a settle is that touch's
# (2.0, phone 2026-11-26) the earliest a settle may end "changed, then still" after a touch the hands sent, counted from
# the touch's end. A HID tap is acked at once (60-110 ms after it starts): in Settings a row shows its highlight first,
# then stands still for a moment, then the push animation runs (0.7-0.9 s to stillness). A settle that ended on the
# highlight (0.11 s) read the tree mid-transition: right after a back, `tap {"label": "General"}` found the outgoing
# page's title too ("2 elements match"). The stillness after a highlight is about 0.1-0.2 s; 0.35 s lets the push start.
HANDS_SETTLE_MIN = 0.35
NOTE_CAPPED = ("the screen was still changing after {cap:g} s (an animation or video); "
               "positions may be mid-change.")


# The safety policy of the call being run (docs/AGENT-TOOLS.md 11.1): set by run_tool for that call only, so two
# agents with different policies never see each other's.
_APP_HINT: contextvars.ContextVar[str | None] = contextvars.ContextVar("tapwright_app_hint", default=None)
_POLICY: contextvars.ContextVar[safety.Policy] = contextvars.ContextVar("tapwright_policy", default=safety.NONE)
# Set to a list by run_tool; a touch a person's OK let through puts where the OK came from in it ("chat", "the live
# view"), and the answer then ends with NOTE_ALLOWED
_GRANTED: contextvars.ContextVar[list[str]] = contextvars.ContextVar("tapwright_granted", default=[])
# The agent (client id) whose call is being run: where the draft of a touch is read from (§21.3: the field it last typed into)
_CALLER: contextvars.ContextVar[str | None] = contextvars.ContextVar("tapwright_caller", default=None)
# True while a saved route with `"draftsOnly": true` runs: a touch on a word that reaches other people stops at a draft (§21.3)
_DRAFTS_ROUTE: contextvars.ContextVar[bool] = contextvars.ContextVar("tapwright_drafts_route", default=False)


class Sent(str):
    """The origin of a person's OK of a DRAFT (§21.3) ("chat", "the live view"): the answer then ends `Note: sent by a person
    (in chat).` where any other OK ends `Note: allowed by a person (in chat).`"""
# The scope (personal mode's app list) of the call being run, for a tool that makes calls of its own (`performance`)
_SCOPE: contextvars.ContextVar["Scope | None"] = contextvars.ContextVar("tapwright_scope", default=None)
# True while `explore` makes its own moves (AGENT-TOOLS §25.5): a touch the policy holds is never asked of a person (no dialog, no
# live-view request) and no grant is looked at: the touch is refused as `needs_confirmation`, and a dry run says so.
_EXPLORING: contextvars.ContextVar[bool] = contextvars.ContextVar("tapwright_exploring", default=False)
# The skip floor of the move being made by `explore`, or by a repair's trial (§25.5, §26.4): set by them for their own moves
# only. Since 1.7.5 no policy word stops an agent's touch, so this is the ONE dispatch gate those moves pass: `_last_checks`
# reads it on the element the touch really goes to (after resolution and any scroll), dry runs included (_floor_gate).
_TOUCH_FLOOR: contextvars.ContextVar["explorefloor.Floor | None"] = contextvars.ContextVar("tapwright_touch_floor", default=None)
# A repaired script's check replay: the floor gates only the steps the repair made (they carry `repaired`); the script's own
# unchanged steps run as a plain replay would, as the person wrote them (lead's ruling on review R180 M6).
_FLOOR_REPAIRED_ONLY: contextvars.ContextVar[bool] = contextvars.ContextVar("tapwright_floor_repaired_only", default=False)
# The policy the caller sent (`tapwright mcp --policy`, `tapwright explore --policy`), for the skip floor alone: since 1.7.5 it
# authorizes and restricts nothing else (the call's own policy is safety.NONE); its words only add to what explore and a
# repair never touch, and its `explore` key is the user's skip list (§25.5).
_FLOOR_RULES: contextvars.ContextVar["safety.Policy | None"] = contextvars.ContextVar("tapwright_floor_rules", default=None)
# The crash reports `explore` has seen found while it runs (any call's crash check finds them first, and tells each once):
# `_find_crash` adds each it returns, so the search can make a `crash` finding of it with its frames (§25.10).
_CRASHES: contextvars.ContextVar[list | None] = contextvars.ContextVar("tapwright_explore_crashes", default=None)
# True while a tool makes calls of its own: they are not recorded as steps (the tool's own step is)
_NESTED: contextvars.ContextVar[bool] = contextvars.ContextVar("tapwright_nested", default=False)
# True while the call being run answers in brief (§13.4): set by run_tool for that call only, so a batch's own answer is
# brief and two agents never see each other's setting
_BRIEF: contextvars.ContextVar[bool] = contextvars.ContextVar("tapwright_brief", default=False)
# The seconds a running batch has spent waiting for a person (their OK, a moment that needs them), as a one-item list;
# None outside a batch. Daemon.held() adds to it, and the batch's 120 s limit leaves them out (§9.3).
_HELD: contextvars.ContextVar[list[float] | None] = contextvars.ContextVar("tapwright_held", default=None)
BRIEF_TOOLS = frozenset(name for name, takes in params.ALLOWED.items() if "brief" in takes)  # every action, and run
# True inside a journey tool's own call (a stress moment, the conditions): only that task's calls skip the journey's gate and
# count. A shared counter on the journey let an independent call of the same client, made meanwhile, skip them too.
_JOURNEY_OWN: contextvars.ContextVar[bool] = contextvars.ContextVar("tapwright_journey_own", default=False)
# True while a journey's `background` stress brings its app back: the running process is brought to the front as it is
# (WebDriverAgent does that for a launch with no arguments), never closed and started again with the phone's defaults, and
# the launch arguments it runs with stay on record (Codex, PR #7 cycle 1).
_RESUME: contextvars.ContextVar[bool] = contextvars.ContextVar("tapwright_resume_app", default=False)
# True while a journey's `double_tap` stress makes its tap: the touch goes out twice, DOUBLE_TAP_GAP apart (§17.4)
_DOUBLE: contextvars.ContextVar[bool] = contextvars.ContextVar("tapwright_double_tap", default=False)
# what the double tap found changed under its second touch (1.13.1, item 6), for the journey's stress line
_DOUBLE_CHANGED: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar("tapwright_double_changed", default=None)


SAFARI = "com.apple.mobilesafari"
SAFARI_ADDRESS = ("TabBarItemTitle", "URL")  # the identifiers of Safari's address field (iOS 26's capsule bar; older bars)
URL_NOT_LOADED = ("Safari {shows}; the link may not have loaded. Type it into the address bar (type_text with "
                  "submit: true).")


def _safari_page(items: list) -> tuple[str | None, str | None, frozenset]:
    """Safari's address field's text, its open tab (TabDocument's UUID), and what the page shows (each element's role,
    identifier and label; no values, which a playing video changes by itself)."""
    address = next((e.value for e in items if e.role == "input" and e.identifier in SAFARI_ADDRESS
                    and e.value and e.value != e.placeholder), None)
    tab = next((m.group(1) for e in items if e.identifier.startswith("TabDocument?")
                and (m := re.search(r"[?&]UUID=([^&]+)", e.identifier))), None)
    # (text labels count: two articles can share every link; Opus review 10)
    page = frozenset((e.role, e.identifier, e.label) for e in items
                     if not e.identifier.startswith(("TabDocument?", "SafariWindow?")) and e.role != "slider")
    return address, tab, page


def _host_names(host: str) -> tuple[str, ...]:
    """The link's host as Safari may show it: as written, and in Unicode for an international (xn--) name."""
    host = host.lower().removeprefix("www.")  # (Safari leaves www. out)
    try:
        shown = host.encode("ascii").decode("idna") if "xn--" in host else host
    except (UnicodeError, ValueError):
        shown = host
    return (host, shown.lower())


def _url_not_shown(args: dict, box: dict, items: list, app: "tuple[str, str] | None", unchanged: bool) -> str | None:
    """open_url into Safari: a note when Safari doesn't show the link (1.7.8, bug 7: with a video open, Safari kept its
    tab and the call said "opened"). Its address field must show the link's host; with the host already open, the tab
    must have changed or the page with it. None when it shows, or when it can't be told (another app, no address)."""
    url = str(args.get("url") or "")
    host = (urllib.parse.urlsplit(url).hostname or "") if url.lower().startswith(("http://", "https://")) else ""
    if not host or (app or (None, None))[1] != SAFARI:
        return None
    address, tab, page = _safari_page(items)
    if address is None:
        return None
    before, before_app = box.get("url_before") or ([], None)
    address_before, tab_before, page_before = _safari_page(before) if before_app == SAFARI else (None, None, None)
    if not any(name in address.lower() for name in _host_names(host)):
        # another host: a note only when Safari still shows the address it showed before the call. One that changed
        # is most likely a redirect (youtu.be to m.youtube.com, a short link, x.com): no note (Opus review 10)
        return URL_NOT_LOADED.format(shows=f"shows {address}, not {host}") if address == address_before else None
    if before_app != SAFARI:
        return None
    if unchanged or (tab is not None and tab == tab_before and page == page_before):
        return URL_NOT_LOADED.format(shows="kept the open page")
    return None


HOSTED_UNKNOWN = ("covered: {line} is in a view another app draws (a share sheet, a picker), and where it is on the "
                  "screen can't be told; nothing was touched. Look with screenshot: true, then tap it by point.")
HOSTED_BACK = ("covered: a navigation bar on screen is in a view another app draws (a share sheet, a picker), so where "
               "back would touch can't be told; nothing was touched. Tap the back button you mean by label (the phone "
               "confirms where that lands) or by point.")
HOSTED_SHEET = ("covered: the sheet in front is in a view another app draws (a share sheet, a picker), and where it is on "
                "the screen can't be told; nothing was touched. Close it by its own button (by label), or by point.")
HOSTED_UNPROVEN = ("covered: {line} is in a view another app draws (a share sheet, a picker), and the phone couldn't "
                   "confirm that a touch at ({x}, {y}) lands on it; nothing was touched. Look with screenshot: true, then "
                   "tap it by point.")


def _hit_is(hit: ui.Hit, element: ui.Element, source: str | None = None) -> bool:
    """The hit test names this element: its type, name and label, and the frame XCTest gives it (a hosted view's own
    space, as in the tree before it was placed); or an element inside it: within its frame, and its descendant in the
    tree as XCTest gave it (`source`: a share sheet's cell answers with its image or its text, Opus review 11)."""
    raw = element.raw_rect
    if raw is None:
        return False
    if (hit.type == element.type and hit.name == element.name and hit.label == element.label
            and all(abs(a - b) <= 1 for a, b in zip(hit.rect, raw))):
        return True
    x, y, w, h = hit.rect
    within = x >= raw[0] - 1 and y >= raw[1] - 1 and x + w <= raw[0] + raw[2] + 1 and y + h <= raw[1] + raw[3] + 1
    return (within and source is not None
            and ui.hit_relation(source, dataclasses.replace(element, rect=raw), hit) == "inside")


def _near_rect(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    """The same frame, to 2 points (the slack a re-read gives)."""
    return all(abs(p - q) <= 2.0 for p, q in zip(a, b))


def _same_control(a: ui.Element, b: ui.Element) -> bool:
    """The control a double tap's second touch lands on is the one its first touch landed on: the same id, label and text."""
    return (a.identifier, a.label, a.text) == (b.identifier, b.label, b.text)
# "call" while the call being run came from `tapwright call` (a process of its own); None otherwise. For the action log.
_VIA: contextvars.ContextVar[str | None] = contextvars.ContextVar("tapwright_via", default=None)
# The folder this call's per-app maps are kept in (`--maps DIR`, AGENT-TOOLS §18.9); None: the daemon's own, else ~/.tapwright/maps
_MAPS: contextvars.ContextVar[str | None] = contextvars.ContextVar("tapwright_maps", default=None)
# False when the call's MCP server was started with --no-hints (AGENT-TOOLS §30.4); None: the daemon's own setting
_HINTS: contextvars.ContextVar[bool | None] = contextvars.ContextVar("tapwright_hints", default=None)
# Whether a step of a `run` reads the screen after it, so that its move can be a road (§18.2). `run` takes its steps
# without a read to be fast; a read adds about 0.2 s to each step, so this is off until the lead decides.
READ_RUN_STEPS = False
# The test accounts file this call uses (`--accounts FILE`, §18.6): the call's own, else the daemon's; None: no file
_ACCOUNTS: contextvars.ContextVar[str | None] = contextvars.ContextVar("tapwright_accounts", default=None)
# The test account `drive` was given: its values are typed where a step's text is `${account.username}` or `${account.password}`
_ACCOUNT: contextvars.ContextVar["accountlib.Account | None"] = contextvars.ContextVar("tapwright_account", default=None)
# The `Reflex:` lines the call being run has earned so far (a built-in reflex: the keyboard closed another way, a wait for
# loading), a list per `_run_tool`; the answer carries them after its first line (§18.7)
_REFLEXED: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar("tapwright_reflexed", default=None)
# How often each declared reflex has fired in the outermost call being run: its `max` is per call (§18.7)
_FIRED: contextvars.ContextVar[dict[str, int] | None] = contextvars.ContextVar("tapwright_fired", default=None)
REFLEX_TRIES = 6  # reflexes made for one step, whatever their `max`es (each is at most 3 a call)
REFLEX_CODES = ("not_found", "not_actionable")  # a step that fails like this may have a prompt over its target
LOADING_CAP, LOADING_POLL = 10.0, 0.25  # seconds a target is waited for while a loading indicator is on screen
PLACE_NEW = "Place: new (call it with map name)."  # AGENT-TOOLS §18.1: a place seen for the first time
NOTE_NOT_ROAD = "not saved as a road (it needed a person's OK)."  # §18.2
BUILD_TTL = 300.0  # seconds an app's build (version and number) is remembered before the installed apps are asked again
DOUBLE_TAP_GAP = 0.08  # seconds between the two touches of a double tap
STRESS_BACKGROUND_S = 1.0  # seconds the app stays in the background in a `background` stress moment
STRESS_ROTATE_S = 0.6  # seconds in landscape in a `rotate` stress moment
# the calls after which a journey keeps a picture of the screen (§17.5): the actions that can change it
JOURNEY_FRAMES = frozenset({"tap", "long_press", "type_text", "scroll", "toggle", "back", "swipe", "open_url", "open_app",
                            "alert", "press_button", "rotate", "wait_for", "wait", "run", "replay", "drive"})
# whether the field type_text really typed into is secure, appended by _type_text once it has resolved the field (after
# any scroll or re-read), so a recording never trusts a guess made before the call (Codex, PR #6)
_TYPED_SECURE: contextvars.ContextVar[list[bool] | None] = contextvars.ContextVar("tapwright_typed_secure", default=None)
# while a recorded action runs: the element its touch was really aimed at, with the list it was found in, appended as the
# touch goes out (a nested call's too: performance's tap). A recording saves a selector that holds an account's value by
# this element, never by the list the agent had before the call (Codex, PR #10 cycle 6)
_RESOLVED: contextvars.ContextVar[list[tuple["ui.Element", list["ui.Element"]]] | None] = contextvars.ContextVar(
    "tapwright_resolved", default=None)
# True while a replay's fresh start runs: its calls are real steps of a recording in progress (the app restart), but the
# first-answer note goes on the replay's own answer, not on theirs
_GREETING_HELD: contextvars.ContextVar[bool] = contextvars.ContextVar("tapwright_greeting_held", default=False)
# Set to a list by the recording branch of run_tool; a compare_screen call puts what it found there (the folder, the
# bundle id, and the reference file it used), for `_record_compare`. Per call, so another client's comparison can't
# change it.
_COMPARED: contextvars.ContextVar[list["Compared"] | None] = contextvars.ContextVar("tapwright_compared", default=None)
# While a client records: the pictures its recording already compared (the recording's `references`: folder, name,
# digest of the bytes the check used), so `_compare` can refuse a save that would change one before it writes anything.
def _title_of(tree: "ui.Tree | None") -> str:
    """The navigation title a screen read shows, whitespace collapsed ("" when it has none)."""
    structure = getattr(tree, "structure", None)
    return " ".join((getattr(structure, "title", "") or "").split())


def _name_key(name: str) -> str:
    """What a file system may make of a reference name: Windows and macOS ignore case (`home.png`, `HOME.png`), and
    macOS also treats a name's Unicode forms as one (NFC and NFD Hangul or accents). Names with one key are one file."""
    return unicodedata.normalize("NFC", name).casefold()


def _refuse_case_only(seen: str, name: str) -> None:
    """A recording that compared one name refuses another with the same key (_name_key), so a later picture can't
    replace an earlier check's (Codex, PR #3 cycles 9 and 10)."""
    if seen != name:
        raise Refused("bad_params", f'record: "{name}" differs from "{seen}", already compared in this recording, only in '
                                    "case or in Unicode form, and on Windows or macOS they are one file; give this check "
                                    "another name.")


_BASELINES: contextvars.ContextVar[list[tuple[str, str, str]] | None] = \
    contextvars.ContextVar("tapwright_baselines", default=None)
CONFIRM_TTL = 120.0  # seconds a person has to answer a request, and an allow lasts for one matching call (11.3)
DECLINE_HOLD = 60.0  # seconds after a decline during which the same touch isn't asked again
CONFIRM_KEEP = 600.0  # seconds after that a request is still remembered, to say `They declined.` on a retry
# the kill switch (§19.5): a run stops between moves when a grant it started with is revoked
REVOKED_STOP = ("revoked: the grants were revoked while this ran ({names}), so it stopped between moves; nothing more "
                "was touched. Ask your user what to do.")
NOTE_SENT = "sent by a person (in {origin})."  # §21.3: a draft a person sent
NOTE_ALLOWED = "allowed by a person (in {origin})."  # where the OK came from: "chat" (the host's dialog) or "the live view"
CONFIRM_TICK = 0.25  # seconds between looks at a waited-for request's clock (an answer wakes it at once)
NUDGE_EVERY = 3.0  # seconds between the daemon's looks at the phone while a call waits for a person (§11.5)
# The agent (client id) whose call is being run came with a host dialog: a person's OK is waited for and asked through
# its channel (dialog.py) instead of answering needs_confirmation at once. None: today's way, the live view alone.
_DIALOG: contextvars.ContextVar[str | None] = contextvars.ContextVar("tapwright_dialog", default=None)
# What the grants (§19) said about this call's touch, for its answer and its log line (GrantCall); set per call.
_GRANT_CALL: contextvars.ContextVar["GrantCall | None"] = contextvars.ContextVar("tapwright_grant_call", default=None)
# The saved routes running now (a drive of one, a replay of one): a grant that names a route covers only these (§19.1)
_ROUTES: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar("tapwright_routes", default=())
# the place a recorded touch went out from, filled inside its turn (CodeRabbit, PR #20); None outside a recorded call
_PLACE_AT_TURN: contextvars.ContextVar[list | None] = contextvars.ContextVar("tapwright_place_at_turn", default=None)
# The grants in force when the outermost run (run, replay, drive) began: one revoked since stops it between moves (§19.5)
BEFORE_TOUCH = frozenset({"not_actionable", "stale", "not_found", "ambiguous"})  # refusals made before a touch (A-L1)
_RUN_GRANTS: contextvars.ContextVar["tuple[grantlib.Book, frozenset[str], float] | None"] =     contextvars.ContextVar("tapwright_run_grants", default=None)
DRY_TOOLS = ("tap", "long_press", "toggle", "type_text", "swipe", "scroll", "back", "alert")  # the tools `dry_run` is for


_wait_for = aio.wait_for  # cancellation-safe, and it joins its child (aio.py)


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
                "is shown or touched. Ask the user to answer it on the phone or in the human live view. "
                "If the owner explicitly asks to cancel it, use alert with action: cancel (exact Cancel only), "
                "then go back with open_app.")
    front = "the phone can't say which app" if not bundle else f"{name} ({bundle})" if name else bundle
    return (f"outside_apps: {front} is in front and isn't one of the apps you were given ({given}), so nothing of it is shown or "
            "touched. Go back with open_app.")


class UxRead(NamedTuple):
    """What one ux_check turn read (Daemon._ux_check): checked, filed and shown from this, never from the shared view."""
    items: list
    alert_now: "tuple[str, tuple[str, ...], str] | None"
    screen: tuple
    app: "tuple[str, str] | None"
    whole: bool = True  # (2.0) the tree was read whole: a cut-short one is no screen to check (R200v M1)


class Settled(NamedTuple):
    seconds: float
    changed: bool  # False only when the stream showed the screen unchanged for certain
    capped: bool  # the cap ended the wait with frames still changing
    still: bool = False  # (2.0) it ended on frames that stood still (not a stall, no frames, or the cap)
WAIT_MIN, WAIT_MAX = 0.1, 30.0  # seconds, for `wait`
WAIT_FOR_MIN, WAIT_FOR_DEFAULT = 0.5, 10.0  # `wait_for`'s timeout in seconds (its upper end is WAIT_MAX)
WAIT_FOR_KINDS = ("text", "id", "label")
WAIT_FOR_POLL = 0.3  # seconds between tree reads while waiting
SCROLL_MIN, SCROLL_MAX = 0.1, 1.0  # `scroll`'s amount: a share of the scroll area
def _bare_name(shown: str, bundle: str) -> str:
    """An app's name as `Daemon._app` shows it ("Wayfare (com.example.wayfare)") without the bundle id it ends with."""
    tail = f" ({bundle})"
    return shown[:-len(tail)] if shown.endswith(tail) and len(shown) > len(tail) else shown


def _app_words(app: tuple[str, str]) -> str:
    """(name, bundle id) as an answer says it: `Name (bundle id)`, or the bundle id once when that is all the name is."""
    name, bundle = app
    name = _bare_name(name, bundle)
    return bundle if not name or name == bundle else f"{name} ({bundle})"


def _screen_sig(items: list[ui.Element]) -> tuple:
    """What a light read says the screen holds: for comparing two reads when there is no stream."""
    return tuple((e.role, e.label, e.value, e.rect) for e in items)


MAX_SCROLL_SWIPES = 15  # `scroll to start|end` gives up after this many swipes
SCROLL_CAP = 12.0  # ... or after this many seconds (a spinner or a bounce can keep the screen moving)
SCROLL_TO = {"end": ("down", "right"), "start": ("up", "left")}  # the directions each `to` can mean; the first is the default
SCROLL_TOP_INSET, SCROLL_BOTTOM_INSET = 60.0, 40.0  # points a swipe keeps off the screen's edges when no bar says more
WAIT_FOR_READ_MAX = 5.0  # the longest one tree read in `wait_for` may take
WAIT_FOR_MIN_LEFT = 0.1  # with less time than this left, `wait_for` gives up instead of reading again
SKIP_TEXT_WAIT = 3.0  # (2.0) seconds a step's `if` may spend reading the picture's text on a cut-short screen
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
WATCH_TICK, BATTERY_EVERY = 15.0, 300.0  # seconds: the watchdog's look at the state, and at the battery (§24.5)
WATCH_NOTES = 5  # the watchdog's messages an agent's next answer carries at most
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
REFLEX_SCREEN_MIN = 4  # with fewer listed elements, the keyboard reflex can't tell another screen from this one
# (1.7.8, X item 1) every way to close it was tried, or none could be tried without touching a control
NOTE_KEYBOARD_STAYED = "couldn't close the keyboard first: it is still up."
# (1.9.0 round 2, hints on) the same, for a target the keyboard doesn't cover: nothing is wrong, nothing to fight
NOTE_KEYBOARD_STAYS = ("the keyboard stayed up (a touch outside the field didn't close it); it doesn't cover this target, "
                       "so the {what} went ahead.")  # (review Opus 3 L1: no promise about what closes it later)
KEYBOARD_WHAT = {"long_press": "long press", "type_text": "typing"}
NOTE_RETYPED = "the field didn't hold what was typed (keys went missing), so it was cleared and typed once more."
DONE_SETTLE = 0.5  # (Opus 2 M2) seconds a multiline Done is given to show what it did before the tap goes on
DONE_CHANGED = ("the keyboard's Done key was pressed to close the keyboard, and the screen changed after it: Done may have "
                "submitted the form. Nothing else was touched; look before you tap again.")
DONE_KEYS = ("Done",)  # (1.9.0) exactly Done: never return, Go, Send, Search, Next or Join (they can submit)
# (1.9.0) the refusal's hint when hints are on: Return's effect is the app's (KEYBOARD_HINT stays 1.8.5's, for --no-hints)
KEYBOARD_HINT_190 = ("type_text into the field with submit: true presses Return, which closes the keyboard in many forms "
                     "but can also submit, send or add a line: use it only where that is what you want.")
KEYBOARD_HINT = "type_text into the field with submit: true closes it (it presses return)."
SCROLL_HINT = 'scroll to it first (scroll with this target and to: "target").'  # AGENT-TOOLS v1.6: the one-call way
# AGENT-TOOLS 1.13.1 (3): off_screen means no scroll can bring the element on screen, and says what to do then
NOTHING_SCROLLS = ("It lies in a part of the screen that nothing scrolls (behind a fixed bar, or outside every list): "
                   "look at the screen, then target something else or report it.")
NOTE_SPAN = "changes are since your last list, {n} actions ago."  # after answers with look: false (v1.6)
# iOS draws the Health access sheet (HealthKit) in a separate service inside the app's window, and doesn't let UI
# automation change it: its switches and "Turn On All" take touches and nothing changes, "Allow" stays disabled
# (an app tester's RC pass, 2026-11-12). Said plainly, so an agent asks a person instead of trying again.
NOTE_HEALTH = ("this is iOS's Health access sheet; iOS doesn't let automation change it. Ask a person to choose and "
               "tap Allow.")
NOTE_HEALTH_DONE = "a person dealt with iOS's Health sheet; the screen has changed since: look."
HEALTH_TICK = 0.5  # seconds between the reads that see whether the Health sheet is gone (a call waiting for a person)
# 1.5.1: a tap or long press that changed nothing. "No visible change." alone left agents unsure whether the touch
# landed, so they looked again and retried; the note says it did, and what to try by what was touched.
NOTE_DELIVERED = "the {touch} was delivered, and nothing on screen changed within {seconds:.1f} s. {hint}"
HINT_FIELD = "It is a text field: type_text types into it."
HINT_BARE_POINT = "No element is at that point."
HINT_LOADING = ("If the screen is still loading, wait_for what you expect, then try once more; if it stays the same, "
                "report it as a finding.")
DELIVERED_TOOLS = ("tap", "long_press")


def touch_name(tool: str) -> str:
    """What the touch is called in NOTE_DELIVERED: `tap`, `long press`, or `double tap` (a journey's double_tap stress)."""
    return "long press" if tool == "long_press" else "double tap" if _DOUBLE.get() else "tap"
# v1.6: `force` let through what the checks would have refused. A real touch doesn't make the hidden element act
NOTE_FORCED = "forced ({reason}): the touch may have landed on what is in front."
KEYBOARD_TOOLS = ("tap", "long_press", "toggle", "type_text")  # the touches that close the keyboard before them
TOUCH_FLOOR_TOOLS = ("tap", "long_press", "toggle", "type_text")  # a point target of these has no element the floor can judge
REACH_TOOLS = KEYBOARD_TOOLS  # AGENT-TOOLS §16.2: on a target in the tree but off screen they scroll to it first
SCROLLED_THEN = "Scrolled to it, then "
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
BUTTON_CAPTIONS = {"home": "Home", "lock": "Lock the phone", "volumeUp": "Volume up", "volumeDown": "Volume down"}


def element_name(result: str) -> str:
    """The element's name in an action's first answer line ("Tapped e25 button "Equals" @320,780 95x90." →
    `Equals`), cut to MARK_NAME_MAX; "" when it names none. The live view's `label` (VIEWER-PROTOCOL 1.4): the
    whole answer line covered the keys around a touch and ran off the picture (recording the demo, 2026-11-11)."""
    name = re.search(r'"([^"]+)"', result)
    if name is None:
        return ""
    text = name.group(1)
    return text if len(text) <= MARK_NAME_MAX else text[:MARK_NAME_MAX - 1] + "…"


def scheduled(client_id: str) -> bool:
    """Is this client a scheduled run (`tapwright run-schedule`, §22)?"""
    return client_id.startswith(schedulelib.CLIENT)


def full_row_swipe(items: list[ui.Element], x0: float, y0: float, x1: float, y1: float) -> ui.Element | None:
    """The list row a swipe would run the first action of (review 1.7 B-M7), or None: a swipe that starts on a row (a
    cell), moves more across than up or down, and further than FULL_SWIPE_SHARE of the row's width. A shorter one only
    reveals the row's buttons, and a tap on one of those is policed as any tap."""
    dx, dy = x1 - x0, y1 - y0
    if abs(dx) < abs(dy):
        return None
    row = next((e for e in reversed(items)
                if e.role == "cell" and e.x <= x0 < e.x + e.width and e.y <= y0 < e.y + e.height), None)
    if row is None or row.width <= 0 or abs(dx) <= FULL_SWIPE_SHARE * row.width:
        return None
    return row


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
    elif tool == "pinch":
        text = "Zoom in" if float(args.get("scale") or 1) >= 1 else "Zoom out"
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
    elif tool == "network":
        text = "Set the network"
    elif tool == "press_button":
        text = BUTTON_CAPTIONS.get(str(args.get("name") or ""), "Press a button")
    elif tool == "wait_for":
        return None
    else:
        text = tool.replace("_", " ")
    text = " ".join(text.split())
    return text if len(text) <= CAPTION_MAX else text[:CAPTION_MAX - 1] + "…"


def _health_sheet(items: list) -> bool:
    """Is iOS's Health access sheet on screen? Its own buttons say so: "Turn On All" (or "Turn Off All") with "Allow"
    and "Don't Allow"."""
    said = {" ".join(str(getattr(e, k, "") or "").replace("’", "'").split()).casefold()
            for e in items for k in ("label", "text")}
    return bool({"turn on all", "turn off all"} & said) and "allow" in said and "don't allow" in said


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


def _cut_short(tree: object) -> bool:
    """Was this tree read only in part (runner route version 9: tier shallow or root)?"""
    return getattr(tree, "tier", "full") in ("shallow", "root")


def _seen_text(want: str, seen: "list[ocr.Seen]") -> bool:
    """Does a line read from the picture show this text (the same words, or them inside a longer line)? wait_for's
    fallback on a screen that doesn't answer accessibility reads."""
    w = " ".join(want.casefold().split())
    return bool(w) and any(w in " ".join(s.text.casefold().split()) for s in seen)


# (2.0, review R200v H2) a condition (wait_for, expect, a step's `if`) that a cut-short tree can't decide: an id or label
# missing from it may be among what wasn't read. Callers find a refusal for this reason by these words (a refusal is
# rebuilt with notes on its way out of run_tool, so they are the mark, not its type): a run's step that stops for it
# fails `timeout`, never `expect_failed` (no "Check failed" finding, no route repair).
UNREAD = "this screen's elements couldn't be read"


def unread(error: BaseException) -> bool:
    """Was this refusal a condition no read could decide (UNREAD)?"""
    return isinstance(error, Refused) and error.code == "timeout" and UNREAD in str(error)


# (2.0) after an action on a screen whose tree was cut short: no list of what changed can be read
AFTER_CUT = "The picture shows the screen after it; look reads its text again (as o refs)."
# (2.0) a target by id, label, text or element ref on a screen that gave no elements (kept in ui: a repair reads them)
HINT_NO_ELEMENTS = ui.HINT_NO_ELEMENTS
HINT_PART_ELEMENTS = ui.HINT_PART_ELEMENTS


def cut_refusal(error: BaseException) -> bool:
    """(R200v M1) A `not_found` only because the screen's elements weren't (all) read: no failed route, no break to repair."""
    return isinstance(error, Refused) and error.code == "not_found" and ui.cut_hint(str(error))


# (2.0, R200v M1) audit and ux_check on a screen the runner read only in part. `unsupported`: the report can't be made from
# this read. Not `timeout`, which tells an agent the same call may simply work again: on a playing feed it won't, and a
# report of "no issues" from a cut list would be false; the agent moves to a still screen (or pauses the video) first.
CUT_REPORT = ("{tool}: {unread} within the time given (the screen kept moving, or the app didn't answer the "
              "accessibility read in time), so it can't be checked: a report from part of a screen would say what isn't "
              "known. Pause what plays or go to a still screen, then {tool} again.")


def settled_line(seconds: float) -> str:
    return f"Settled in {seconds:.2f} s."


def _alert_key(alert: ui.Alert | None) -> tuple[str, tuple[str, ...], str] | None:
    """What makes the alert the same one: title, buttons and message (a change in the message alone is a change of the alert)."""
    return (alert.title, alert.buttons, alert.message) if alert else None


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
        if args["text"] == "" and args.get("replace") is False and args.get("submit") is not True:
            # (1.7.9) nothing to do: no keys, nothing cleared, no return. An empty text with replace (the default)
            # clears the field, and with submit then presses the submit key; with replace false and submit it presses
            # the submit key alone (review R179 M3, R179b L2)
            raise Refused("bad_params", "text is empty and replace is false: there is nothing to type. An empty text "
                                        "with replace (the default) clears the field (with submit: true, then presses "
                                        "the submit key); with replace: false and submit: true it presses the submit "
                                        "key alone. A recorded secure field's text isn't saved: put a test password in "
                                        "the script's \"text\" first.")
        for key in ("replace", "submit", "force"):
            if key in args and not isinstance(args[key], bool):
                raise Refused("bad_params", f"{key} must be true or false, not {args[key]!r}")
    if name == "press_button" and args.get("name") not in AGENT_BUTTONS:
        raise Refused("bad_params", f"name must be one of {', '.join(AGENT_BUTTONS)}, not {args.get('name')!r}")
    if name == "pinch":
        for key in ("x", "y", "scale"):
            if (number := _number(args, key)) is not None:
                checked[key] = number
        low, high = PINCH_SCALE
        if "scale" in checked and not (low <= checked["scale"] <= high and checked["scale"] != 1):
            raise Refused("bad_params", f"scale must be from {low:g} to {high:g} and not 1 (above 1 spreads the fingers "
                                        f"to zoom in, below 1 pinches them to zoom out), not {args.get('scale')!r}")
        low, high = PINCH_SECONDS
        if "seconds" in checked and not low <= checked["seconds"] <= high:
            raise Refused("bad_params", f"seconds must be from {low:g} to {high:g}, not {args.get('seconds')!r}")
    if name == "rotate" and args.get("to") not in ("portrait", "landscape_left", "landscape_right"):
        raise Refused("bad_params", f"to must be portrait, landscape_left or landscape_right, not {args.get('to')!r}")
    if name == "alert" and args.get("action") not in ALERT_ACTIONS:
        # never a part of the runner's URL (`/alert/<action>`): only these words reach it
        raise Refused("bad_params", f"action must be one of {', '.join(ALERT_ACTIONS)}, not {args.get('action')!r}")
    if name == "alert" and args.get("name") is not None:
        if not isinstance(args["name"], str) or not args["name"].strip() or args.get("action") == "text":
            raise Refused("bad_params", "name must be a non-empty alert button label, with accept, dismiss or cancel")

    if name == "wait_for":
        if args.get("network_idle"):  # AGENT-TOOLS §5.1: Tapline's; Tapwright can't see the app's requests
            raise Refused("unsupported", "Tapwright can't see the app's network requests, so it can't wait for "
                                         "network_idle; wait_for a text, id or label the loaded screen shows, or use "
                                         "Tapline")
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
    if name == "performance":
        if "launch" in args and not isinstance(args["launch"], bool):
            raise Refused("bad_params", f"launch must be true or false, not {args['launch']!r}")
        if args.get("launch") and (args.get("seconds") is not None or args.get("tap") is not None):
            raise Refused("bad_params", "launch measures until the screen settles; leave seconds and tap out")
        seconds = args.get("seconds")
        if seconds is not None and not performance.SECONDS_MIN <= checked["seconds"] <= performance.SECONDS_MAX:
            raise Refused("bad_params", f"seconds must be from {performance.SECONDS_MIN:g} to "
                                        f"{performance.SECONDS_MAX:g}, not {seconds!r}")
        if args.get("tap") is not None:
            ui.parse_target(args["tap"])  # bad_params, with an example of a target
    if name == "wait":
        seconds = args.get("seconds")  # required (REQUIRED); a bool is a number to Python, a string isn't one to us
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not WAIT_MIN <= seconds <= WAIT_MAX:
            raise Refused("bad_params", f"seconds must be a number from {WAIT_MIN:g} to {WAIT_MAX:g}, not {seconds!r}")
    if name in ("open_app", "close_app") and (not isinstance(args["app"], str) or not args["app"].strip()):
        raise Refused("bad_params", f"app must be a bundle id or an app's name, not {args['app']!r}")
    if name == "open_app":  # §16.3: language, region and text size, checked before anything reaches the phone
        for key in launchlib.KEYS:
            checked.pop(key, None)
        checked.update(launchlib.check(args))
    if name == "network" and args.get("mode") not in network.MODES:
        raise Refused("bad_params", f"mode must be one of {', '.join(network.MODES)}, not {args.get('mode')!r}")
    if name == "toggle" and args.get("value") is not None and not isinstance(args["value"], bool):
        raise Refused("bad_params", f"value must be true or false, not {args['value']!r}")
    return checked


CRASH_NOTE = "Note: " + diagnostics.CRASHED  # how both crash notes begin, as a note line


def _without_crash_note(message: str) -> tuple[str, list[str]]:
    """An error's message without its crash-note lines, and those lines."""
    lines = message.split("\n")
    return "\n".join(ln for ln in lines if not ln.startswith(CRASH_NOTE)), [ln for ln in lines if ln.startswith(CRASH_NOTE)]


def _checked_diag(name: str, args: dict) -> dict:
    """`logs` and `crash` arguments, cleaned: nulls left out, numbers whole, or Refused(bad_params)."""
    given = {k: v for k, v in args.items() if v is not None}
    out: dict = {}
    if name == "logs":
        since = given.get("since")
        if since is not None:
            if (isinstance(since, bool) or not isinstance(since, (int, float)) or not math.isfinite(since)
                    or since < 0):
                raise Refused("bad_params", f"since must be an epoch time in milliseconds, not {since!r}")
            out["since"] = int(since)
        level = given.get("level", "info")
        if level not in diagnostics.LOG_LEVELS:
            raise Refused("bad_params", f"level must be error, warning or info, not {level!r}")
        out["level"] = level
        grep = given.get("grep")
        if grep is not None and not isinstance(grep, str):
            raise Refused("bad_params", f"grep must be a text, not {grep!r}")
        out["grep"] = grep or None
        every = given.get("all", False)
        if not isinstance(every, bool):
            raise Refused("bad_params", f"all must be true or false, not {every!r}")
        out["all"] = every
        limit = given.get("limit", diagnostics.LOG_DEFAULT_LIMIT)
        if (isinstance(limit, bool) or not isinstance(limit, (int, float)) or not math.isfinite(limit)
                or limit != int(limit)
                or not 1 <= limit <= diagnostics.LOG_MAX_LIMIT):
            raise Refused("bad_params", f"limit must be a whole number from 1 to {diagnostics.LOG_MAX_LIMIT}, "
                                        f"not {limit!r}")
        out["limit"] = int(limit)
    else:
        for key in ("all", "pull"):
            if key in given and not isinstance(given[key], bool):
                raise Refused("bad_params", f"{key} must be true or false, not {given[key]!r}")
            out[key] = bool(given.get(key, False))
    return out


class Rescope(Refused):
    """The tree read for a tap didn't name a given app with certainty: _within asks the phone as it always did."""

    def __init__(self) -> None:
        super().__init__("rescope", "the read doesn't say which app is in front")


class DryRun(Refused):
    """A dry run's whole answer (`Would tap ...`), raised where the touch would go out. It is a Refused so the
    controller counts nothing as done: no new screen generation, so the agent's refs stay good."""

    def __init__(self, line: str) -> None:
        super().__init__("dry_run", line)


@dataclasses.dataclass
class GrantCall:
    """What the grants said about one call's touch (§19.3, §12): the notes of grants that matched but didn't cover it,
    and, when one covered it, its note and count. `taken` marks the call the holder belongs to."""
    notes: list[str] = dataclasses.field(default_factory=list)
    asked: str | None = None  # §12 grant_asked: limit reached, quiet hours, ended or revoked
    name: str | None = None  # §12 grant: the grant that let the touch through
    use: str | None = None  # §12 grant_use: "2 of 3 today"
    line: str | None = None  # `Note: allowed by grant "…" (2 of 3 today).`
    draft: str | None = None  # §12 grant_draft: the draftsOnly grant that stopped this touch at a draft
    taken: bool = False
    claimed: "tuple[grantlib.Book, grantlib.Use] | None" = None  # the use a grant gave this call (A-L1: given back...)
    grant_touch: "grantlib.Touch | None" = None  # the claimed use is revalidated at physical dispatch
    confirmed: "Confirm | None" = None  # an approved draft retains the exact digest the person saw until dispatch
    touched: bool = False  # (...when the call is refused before any touch went out)


@dataclasses.dataclass
class Confirm:
    """One request for a person's OK (docs/AGENT-TOOLS.md 11.3) and where it stands: `pending`, then `allowed` or
    `declined`. `until` is the clock time (the controller's) a pending request expires, or an allow lapses."""
    id: str
    tool: str
    key: tuple[str, ...]  # the element: its app, id, role, label and text (Daemon._police_key)
    until: float
    state: str = "pending"
    message: dict | None = None  # what viewers were sent, for one that connects while it is open
    owner: str | None = None  # the agent (client id) whose call asked: only its host's dialog may answer
    dialog: bool = False  # the host was asked too (a `dialog.CONFIRM` ask went to the owner's channel)
    origin: str = ""  # where the answer came from: "chat" or "the live view"
    draft: str | None = None  # a draft's request (§21.3): the draftsOnly grant that stopped it, "" for a route's
    # a draft's request: a keyed digest of the draft and where it goes, as the person was shown them (Daemon._draft_digest).
    # The send goes only while the screen still holds exactly that (Codex, PR #12 cycle 1, H3)
    draft_digest: bytes | None = None
    used: bool = False  # an allow was taken by one call (Daemon._claim): no other call touches under it
    offer: "dict | None" = None  # what the follow-up grant offer (§19.6) needs, kept when the host was asked too
    answered: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)  # set at the first answer


@dataclasses.dataclass
class Need:
    """One "needs a person" ask (§11.5), open while the host's dialog is up: `pending`, `done` (they say they did it;
    the tool looks again) or `cant` (they can't now)."""
    id: str
    owner: str  # the agent (client id) whose call waits
    state: str = "pending"
    answered: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)


@dataclasses.dataclass
class Offer:
    """One question of the grant dialog (§19.6) open now: the grant's (`grant`) or the drafts one (`drafts`). `answer`
    is the word the person gave (`allow`/`decline`, `yes`/`no`), None while open."""
    id: str
    owner: str
    kind: str
    answer: str | None = None
    answered: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)


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
    # the exact bytes the check used: those it read from the reference it compared with, or wrote as the one it saved.
    # A recording keeps these, never the path, so a later write to the file by anyone changes nothing it exports.
    picture: bytes | None = None
    named: int = 0  # a match that names what changed under the threshold: how many names (1.13.1 §14)


@dataclasses.dataclass
class _Flight:
    """A media add in flight (Daemon._media_adding): whether iOS asks for access with it, how many bytes of video it
    carries (copied to the phone first, then imported), and the event set as it goes out to the runner."""
    asks: bool
    video_bytes: int
    went_out: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)


def _unknown_end(task: "asyncio.Future") -> "Refused | None":
    """The add's answer when it ended without the phone's word on what happened (no answer, the runner's own wait ran
    out, a dropped line): it may still have added them."""
    if not task.done() or task.cancelled():
        return None
    e = task.exception()
    return e if isinstance(e, Refused) and e.code in ("timeout", "reconnecting") else None


def _picture_digest(data: bytes | None) -> str | None:
    """The hash of the bytes a check used (None when it used none): two checks of one name in a recording are the same
    baseline only when this is the same."""
    return hashlib.sha256(data).hexdigest() if data is not None else None


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
    typed: str | None = None  # the text really typed (an account's value for its placeholder); never in an answer

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
        return f"Typed into {line}. Value is now {ui.quoted(now.value)}."


class Scrolled(Noted):
    """`scroll`'s result, with a note for the answer when it stopped early."""
    drags: int = 0  # completed input, even when a caller refuses its next action


# What `open_app "Settings"` means: the built-in apps aren't in the installed list.
SYSTEM_APPS = {"Settings": "com.apple.Preferences", "Safari": "com.apple.mobilesafari",
               "Calculator": "com.apple.calculator", "Camera": "com.apple.camera",
               "Photos": "com.apple.mobileslideshow", "Messages": "com.apple.MobileSMS",
               "Mail": "com.apple.mobilemail", "Maps": "com.apple.Maps", "App Store": "com.apple.AppStore",
               "Clock": "com.apple.mobiletimer", "Notes": "com.apple.mobilenotes"}


def app_line(a: dict) -> str:
    return f"{a['bundle_id']}  {a['name']} {a['version']} ({a['build']})"


async def _pinch(s, a: dict) -> str:
    """`pinch` (2.0): two fingers through the hands (Session.pinch); a session without them answers unsupported."""
    pinch = getattr(s, "pinch", None)
    if pinch is None:
        raise Refused("unsupported", "pinch needs touches through CoreDevice HID, which this Tapwright session doesn't "
                                     "have; nothing was touched.")
    x, y = float(a["x"]), float(a["y"])
    w, h = s.screen
    if not (0 <= x <= w and 0 <= y <= h):
        raise Refused("bad_params", f"({x:g}, {y:g}) is off the screen ({w:g} x {h:g} points)")
    return await pinch(x, y, float(a["scale"]), int(float(a.get("seconds", 0.5)) * 1000))


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
    "pinch": (lambda s, a: _pinch(s, a), CAP),
    "alert": (lambda s, a: s.alert(a["action"]), CAP),
    "open_url": (lambda s, a: s.open_url(a["url"]), LONG_CAP),
    "rotate": (lambda s, a: s.rotate(a["to"]), CAP),
}
SPRINGBOARD = "com.apple.springboard"
MEDIA_WATCH = 1.0  # seconds between looks for iOS's photo prompt while media waits on the runner
MEDIA_PEEK = 3.0  # seconds one such look may take (the runner may be busy with the add itself)
MEDIA_KEEP = 600.0  # seconds a cut-short add stays the one a repeated media call picks up
PHOTO_WAIT_MAX = 130.0  # seconds the runner may be held by iOS's photo prompt (its add waits 120 s for the answer)
UPLOAD_RATE = 1024 * 1024  # bytes a second a video's copy to the phone is allowed at least (slow Wi-Fi), before its add
MEDIA_HOLD_SLACK = 5.0  # seconds a sent video add holds the phone beyond its request's own deadline
PHOTO_WAIT_BUSY = ("busy: the phone's runner is waiting for the person to answer iOS's photo prompt (from media) and "
                   "answers nothing else until they do, {left} s at most. Ask your user to answer it on the phone, "
                   "then try again.")
# (1.7.9, review R179 M2) a media add with a video holds the phone for as long as its copy and the library's import
# take: the runner answers nothing else while it imports, so the same wait as for iOS's prompt applies, in its own words
VIDEO_BUSY = ("busy: a video is being added to Photos (from media): it is copied to the phone, then the photo library "
              "imports it, and the phone's runner can't take other calls meanwhile, {left} s at most. Try again once "
              "it is done.")
VIDEO_PROMPT_BUSY = ("busy: a video is being added to Photos (from media), and iOS may be asking the person first "
                     "whether the runner may use Photos: the phone's runner answers nothing else until it is done, "
                     "{left} s at most. If the photo prompt is on the phone, ask your user to answer it; then try "
                     "again.")
VIDEO_HOLDS = frozenset({"copy", "video", "video-prompt"})  # the holds of an add with a video (minutes, not 130 s)
HOLD_WORDS = {"prompt": PHOTO_WAIT_BUSY, "copy": VIDEO_BUSY, "video": VIDEO_BUSY, "video-prompt": VIDEO_PROMPT_BUSY}
# (CodeRabbit, PR #19) one add at a time when a video is in it: whatever the first add's hold budget says, a second add
# would queue behind its import on the runner, and the one hold can't cover both
VIDEO_ADD_GOING = ("busy: another media add with a video is still going on (it is copied to the phone, then the photo "
                   "library imports it), or a video add would queue behind another add that is still going on. Try "
                   "again once it is done.")
EARLIER_UNKNOWN = ("the earlier add of these same pictures or videos ended without an answer from the phone ({why}): the "
                   "photo library may still have added them. Check Photos > Recently Added first; if they aren't "
                   "there, call media again with them and they are added again.")
# what the agent checks when a media write's end isn't known (review R179b L1: a remove isn't an add)
CHECK_ADDED = "check Photos > Recently Added before adding again"
CHECK_REMOVED = "check Photos before calling media again"
# (review R179b L4) a media call that came in while an add held the phone, and found that hold ended by the time it
# could look: nothing of the runner (its version, its access) has been read for it, so it starts nothing
HOLD_ENDED_BUSY = ("busy: another media add held the phone as this call came in, and has just ended; call media again "
                   "now.")
PHOTO_WAIT_FREE = frozenset({"media", "session_report", "viewer"})  # calls that don't need the runner meanwhile
RUNNER_NAME = "WebDriverAgentRunner"  # what iOS's own prompts call the runner ("“WebDriverAgentRunner-Runner” Would Like …")
NEEDS_PHOTO_ANSWER = ("needs_a_person: iOS is asking on the phone whether Tapwright's runner may use Photos. A person "
                      "answers it on the phone (to add pictures: \"Allow\" or \"Limit Access…\"; never needed: full "
                      "access), then call media again. Until they answer (2 minutes at most) the runner answers no other "
                      "call: ask your user now, and wait for them.")
NEEDS_ALERT_ANSWER = ("needs_a_person: iOS is showing an alert on the phone while the runner waits for access to Photos. "
                      "A person answers it on the phone, then call media again.")
PROMPT_FRONT = "(a given app's prompt)"  # Daemon._front: a given app's own system prompt is in front
ALERT_ABOUT = "(an alert about a given app)"  # Daemon._within: another app (Settings) draws an alert that names a given app
NOTE_ALERT_ABOUT = ("the alert was another app's, about one of yours (iOS asks a Delete or a permission question in the app "
                    "that raised it); that app is in front now: go back with open_app.")
PROMPT_RETURN_WAIT = 2.0  # seconds the app may take to come back once its prompt is answered
FRONT_RETRIES, FRONT_RETRY = 16, 0.5  # see Daemon._front: as long as a notification banner stays
FRONT_WATCH = 0.15  # s between front-app questions while a filmstrip films in personal mode
NETWORK_IDLE = 600.0  # s without a call from the agent that set a network condition before it is cleared
NETWORK_CHECK = 5.0  # s between those checks
NETWORK_OWNER_LOST = 20.0  # s without a heartbeat or a call from an agent that sends heartbeats (its process is gone)
NETWORK_MAX = 1800.0  # s a condition may last from the moment it was set, whatever its agent does (30 min)
MIN_VISIBLE = 10.0  # points: less of an element showing than this (high or wide) is no place to touch
# The status bar across the top of a phone held upright: a touch there scrolls the app's list to its top (iOS),
# whatever row lies under it. It is in no app's tree, so a row half under it looked reachable: a tap on Settings'
# "Notifications" row at y 10 scrolled the list instead (seen on the phone 2026-11-11). 50 pt covers the status
# bar of the current iPhones (47 to 54); in landscape an iPhone shows none.
STATUS_BAR = 50.0
MOVES_CONTENT = ("scroll", "swipe", "pinch")  # a drag: identical elements can't be told apart after these
# These may show the same screen again, somewhere else in it, or another: the elements say which. Calling each a
# move gave every element without an identifier a new ref, as a `+` and a `-` line for the same thing (seen on
# the phone 2026-11-10: open_url to the screen already open).
MAY_MOVE_CONTENT = ("back", "open_app", "open_url", "press_button", "rotate")
READS = ("look", "apps", "audit", "ux_check", "filmstrip", "compare_screen", "logs", "crash", "session_report", "map",
         "notifications")
DIAG_TOOLS = ("logs", "crash")  # AGENT-TOOLS §15: the app's log and its crash reports; they read no screen
NO_CRASH_CHECK = frozenset({"logs", "crash", "apps", "close_app"})  # calls after which the app's stopping is no news
# seconds: between looks for the app's process; before the watcher gives up; a first `logs` waits for the stream
LOG_RETRY, LOG_IDLE, LOG_WARMUP = 2.0, 60.0, 0.5
LOG_DRAIN = 1.0  # seconds a log stream may go on after its process is gone: the last lines (the crash's) are still on their way
CRASH_LOOKUP = 15.0  # seconds each step of the optional crash check (the apps' executable names, the report search) may take
LOG_VERIFY_FAILS = 2 # checks of whose process a log stream is reading that may fail in a row before the stream is left
LOG_START_SLACK_MS = 1000  # an entry this much before a process's start time still counts as its own (clocks, rounding)
LOG_CHECK, LOG_BACKOFF_MAX = 3.0, 60.0 # seconds between looks at whether the app's process is still the one read; the longest wait between tries to start a stream
# looks for a report once the app is gone, seconds between them, later calls that look again
CRASH_POLLS, CRASH_POLL, CRASH_LATE_TRIES = 3, 1.0, 3
CRASH_CUT_CODES = ("not_connected", "not_answering", "reconnecting", "timeout")  # errors a crash can end a call with
# The scope of the filmstrip call being run: its tap goes through run_tool with it, like any other touch
_FILM_SCOPE: contextvars.ContextVar["Scope | None"] = contextvars.ContextVar("tapwright_film_scope", default=None)
# Personal mode's given apps for the call being run (set by _within): a picture tool checks the front app again after
# its picture is taken, since the app may have changed after the question before the call
_GIVEN: contextvars.ContextVar["dict[str, str] | None"] = contextvars.ContextVar("tapwright_given", default=None)
# Set by _within for a tap that will read the tree anyway: the scope check is made on that read's own root app
# (_scope_from_read) instead of a separate question to the phone. Holds the given apps.
_SCOPE_BY_READ: contextvars.ContextVar["dict[str, str] | None"] = contextvars.ContextVar("tapwright_scope_by_read", default=None)
# Set by _within for a tap the fork's tap checks itself (route version 4, Session.front_by_touch): the runner touches only
# while a given app is in front, so no question is asked first (1.6 E3). Holds the given apps.
_SCOPE_BY_TOUCH: contextvars.ContextVar["dict[str, str] | None"] = contextvars.ContextVar("tapwright_scope_by_touch", default=None)
# The touches a tap with _SCOPE_BY_READ has made so far, each checked by _touch_in_scope (set in act_as_agent)
_SCOPE_TOUCHED: contextvars.ContextVar["list[bool] | None"] = contextvars.ContextVar("tapwright_scope_touched", default=None)
FILM_LIMITS = {"frames": filmlib.FRAMES, "interval_ms": filmlib.INTERVAL_MS, "frame_width": filmlib.FRAME_WIDTH}
BUNDLE_ID = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
# not on the phone's queue: the first three sleep before they answer, network talks to the instruments service
SPECIAL = ("wait", "wait_for", "performance", "network", "media")
REQUIRED = {"network": ("mode",), "rotate": ("to",), "open_app": ("app",), "close_app": ("app",), "toggle": ("target",), "wait": ("seconds",),
            "type_text": ("text",), "tap": ("target",), "long_press": ("target",),
            "swipe": ("x0", "y0", "x1", "y1"), "pinch": ("x", "y", "scale"), "press_button": ("name",), "alert": ("action",),
            "open_url": ("url",), "compare_screen": ("name",)}


async def _first(timeout: float, *events: asyncio.Event) -> None:
    """Wait until any of the events is set, or `timeout` seconds."""
    waits = [asyncio.create_task(e.wait()) for e in events]
    try:
        await asyncio.wait(waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waits:
            w.cancel()


class Daemon(bugtool.BugTools, exploretool.ExploreTools, journey_tool.JourneyTools, center_tool.CenterTools, scriptrepairrun.RepairTools):
    ceiling = None  # the flags `tapwright serve` was started with (a ceiling.Ceiling); `--brief` is read from it

    def __init__(self, session, control: Controller | None = None, log=print, heartbeat: float = 5.0,
                 backoff: tuple = (1.0, 2.0, 5.0, 10.0), ready_wait: float = 60.0,
                 stream_linger: float = 30.0, stream_retry: float = 1.0, settle_still: float = STILL,
                 settle_first_frame: float = 1.0, settle_fallback: float = 0.6,
                 settle_quiet: float = MIN_QUIET, shot_wait: float = 1.0, shot_fresh: float = 1.0,
                 pace_tick: float | None = PACE_TICK, pace_quiet: tuple[float, float, float] = PACE_QUIET,
                 actions: ActionLog | None = None, audit: "auditlog.AuditLog | None" = None,
                 grant_clock: "Callable[[], datetime.datetime] | None" = None) -> None:
        self.session = session
        self.actions = actions or ActionLog(None)  # the action log (AGENT-TOOLS §12); off unless the server gives one
        # The audit log (§23): every grant use, revocation and person's OK. None (tests): no grant covers anything,
        # since a grant needs its line; the server always gives one. Its lines are scrubbed of account values (§20.3).
        self.audit = audit
        if audit is not None:
            audit.scrub = self.scrub_everywhere
        self._grant_clock = grant_clock  # (tests) the local clock the grants count by; None: the computer's
        self._books: dict[str, grantlib.Book] = {}  # policy file -> its grants (§19)
        self._announced: dict | None = None  # the action message this agent call announced, for its log line
        self._granted_now = False  # this agent call went through on a person's OK (for its log line)
        self._outside_ignored: set[str] = set()  # apps whose forms ignore a touch outside the field: the keyboard reflex goes first
        self._no_done: set[tuple] = set()  # (app, place) where the keyboard showed no Done key (1.9.0, _press_done)
        self._no_dismiss: set[str] = set()  # apps whose keyboard WebDriverAgent couldn't dismiss (see _dismiss_keyboard_now)
        self._recording: dict[str, dict] = {}  # client id -> the recording in progress (AGENT-TOOLS §9.4)
        # while a client records, its calls run one at a time: a check's guard, its save and its registration in the
        # recording are one step, so two overlapping checks can't both pass the guard (Codex, PR #3 cycle 6)
        self._record_locks: dict[str, asyncio.Lock] = {}
        self._batch_ids = itertools.count(1)
        # diagnostics (AGENT-TOOLS §15): the app's log in a ring per bundle id, read by a task per app
        self._rings: dict[str, diagnostics.LogRing] = {}
        self._log_tasks: dict[str, asyncio.Task] = {}
        self._log_pids: dict[str, int] = {}  # the process each task reads
        self._log_tainted: dict[str, Proc] = {}  # the process whose pid another process was seen holding: absence won't vouch for it
        self._log_life: dict[str, tuple[Proc, int]] = {}  # that process, and the ring's `added` count when it was last vouched for
        self.log_retry, self.log_idle, self.log_warmup = LOG_RETRY, LOG_IDLE, LOG_WARMUP
        self.log_check, self.log_drain = LOG_CHECK, LOG_DRAIN
        self.crash_lookup = CRASH_LOOKUP
        self._log_dead: dict[str, str] = {}  # bundle id -> why its log stream can't be started (tries gave up)
        self.crash_polls, self.crash_poll = CRASH_POLLS, CRASH_POLL
        self._opened: str | None = None  # the app open_app last started: the app under test
        self._launched_with: dict[str, dict] = {}  # app -> the language/region/text size its last launch was given
        self._executables: dict[str, str] = {}  # bundle id -> the name of its process, from the installed apps
        self._wall = datetime.datetime.now  # the phone's reports carry local time without a zone
        self._began_wall = self._crash_since = self._wall()  # the session began; the last answer without news of a crash
        self._crash_seen: set[str] = set()  # reports an answer has told of
        self._left_front: str | None = None  # the app a touch found gone from the front and opened again
        # client id -> the app that stopped on that client's call, whose report hasn't reached the phone's folder yet:
        # its late note goes to that client only, on an answer while it drives that app again
        self._crash_pending: dict[str, dict] = {}
        self._diag_count: int | None = None  # the entries or reports the last logs / crash call showed, for the action log
        # the stream's pace (see STILL_FPS): how often it's checked (None: never), and the frame rate the phone
        # streams at now (None until connected)
        self.pace_tick, self.pace_quiet = pace_tick, pace_quiet
        self._fps: int | None = None
        self._person_at = self._changed_at = -math.inf  # monotonic: a person's last input, the screen's last change
        self._pace_seen: Frame | None = None  # the newest frame at the last check
        # screenshots: how long to wait for a stream frame before asking the phone, and how old one may be
        self.shot_wait, self.shot_fresh = shot_wait, shot_fresh
        self._read_root: tuple[tuple[str, str] | None, bool] | None = None  # the last silent read's root app, alert up
        self.unsettled = False  # the last action returned without a settle: the screen may still be moving, for every client
        # settle: how long a still screen is quiet, how long to wait for a first frame, and the
        # blind sleep used when the stream gives no frames at all
        self.settle_still, self.settle_first_frame, self.settle_fallback = settle_still, settle_first_frame, settle_fallback
        self.settle_quiet = settle_quiet
        self.control = control or Controller()
        self.hub = FrameHub()
        self.pump = StreamPump(self._open_stream, self.hub, linger=stream_linger, retry=stream_retry, log=log,
                               suspended=self._fed_by_video)
        self.log = lambda message: log(self.scrub_everywhere(message))  # (what any sink gets holds no account's value)
        self._media_lock = asyncio.Lock()  # makes a media add's lookup, start and claim of the photo wait one step
        # the add that holds the phone, and until when: iOS's photo prompt, or a video's copy and import (1.7.9). The
        # runner answers nothing else meanwhile (Codex, PR #8); `_photo_wait_kind` says which (HOLD_WORDS)
        self._photo_wait: "tuple[asyncio.Future, float] | None" = None
        self._photo_wait_kind = "prompt"
        self._media_flights: "dict[asyncio.Future, _Flight]" = {}  # an add's task -> what it carries, and whether it went out
        self._media_adds: dict[str, tuple[tuple[tuple[str, str], ...], asyncio.Future, float]] = {}  # client -> an add iOS's prompt cut short, by its pictures (name, SHA-256), in order
        self._closing: set[asyncio.Future] = set()  # WebDriverAgent terminates after a close_app's kill, left to finish
        # A trial switch (TAPWRIGHT_AGENT_STREAM=0): tool calls don't open the JPEG stream, so an action settles
        # on a short wait and a tree read. To learn whether the stream is what stalls requests over Wi-Fi.
        self.agent_stream = os.environ.get("TAPWRIGHT_AGENT_STREAM", "1") != "0"
        self._agent_touches = AgentTouches()
        self._turned: tuple | None = None  # ((frame seq, degrees), the turned JPEG): see turned()
        self.heartbeat, self.backoff, self.ready_wait = heartbeat, backoff, ready_wait
        self.state, self.detail = "connecting", None
        self._phone_seen_at: float | None = None  # see phone_last_seen
        self.preset = os.environ.get("TAPWRIGHT_PRESET", "balanced")  # (a trial switch: the stream's preset at start)
        if self.preset not in PRESETS:
            self.preset = "balanced"
        self.ws_clients = 0
        self.ws_pages = 0  # of those, the browser pages (a socket with an Origin): what `viewer` finds open
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
        self._greeted: set[str] = set()  # agents that have had their first answer (it carries what needs a person)
        self._scrolled_first: set[str] = set()  # agents whose action just scrolled to its target (§16.2): its first line says so
        self._policed: tuple | None = None  # (tool, key) of a touch the policy checked before a scroll (§16.2)
        self._hunts: dict[str, list[ui.Element]] = {}  # {agent: the off-screen list when its run of scrolls began}
        # the maps (AGENT-TOOLS §18): learned from what agents see and do, saved in the background (mapkeeper.py)
        self.maps = mapkeeper.Maps()  # (the setter below gives it the accounts' scrub)
        self.accounts = accountlib.Store()  # the test accounts files, read again when they change (§18.6)
        self.otp_codes = otplib.Codes(self.accounts.remember_code)  # one-time codes read, not yet used (§20.4)
        self.code_fields = otplib.FieldWatch()  # when each app with codes on showed its code field (§20.4)
        try:
            self.session.on_tree = self._watch_code_field  # (each read the daemon builds says whether one shows)
            self.session.screen_still = self._still_for_budget  # (2.0: a still screen's read gets the normal budget)
        except AttributeError:
            pass
        # the passcode unlock (§18.6, §11.5; unlock.py): its rules and counts, and the lock screen it types on
        self.unlocker = unlocklib.Unlocker()
        self.lock_screen: "unlocklib.LockScreen | None" = None  # (made from the session when first needed; tests set one)
        # the watchdog (§24.5, watchdog.py): one message per event when the phone has gone or its battery is low
        self.watchdog = watchlib.Watchdog(self._watchdog_say)
        self.watch_tick: float | None = WATCH_TICK if pace_tick is not None else None  # (None: no ticking task)
        self.battery_every = BATTERY_EVERY
        self._watch_notes: dict[str, list[str]] = {}  # {agent: the watchdog's messages its next answer carries}
        self._battery_at = -math.inf
        self._builds: dict[str, tuple[float, str | None]] = {}  # bundle id -> (when read, "1.4.0 (212)"): the app's build
        self._language: dict[str, str] = {}  # bundle id -> the language open_app last started it with, for title hashes
        self._queues: set = set()
        self._draft_key = secrets.token_bytes(32)  # the drafts' digests (_draft_digest, H3): this process's own, kept nowhere
        self._confirms: dict[str, Confirm] = {}  # requests for a person's OK, by id (see _police)
        self._confirm_seq = 0
        self._needs: dict[str, Need] = {}  # "needs a person" asks open now, by id (see _hold_for_person)
        self._offers: dict[str, Offer] = {}  # grant dialog questions open now, by id (see _offer_grant)
        self._offered: set[tuple[str, str, str]] = set()  # (agent, app, word) a grant was offered for: once a session
        self._need_seq = 0
        self._channels: dict[str, dialog.Channel] = {}  # what each agent's host is asked (dialog.py), by client id
        self._granted_by: str | None = None  # where the OK that let this agent call through came from (for its log line)
        self._handlers = {"open_app": self._open_app, "close_app": self._close_app, "toggle": self._toggle,
                          "tap": self._tap, "long_press": self._long_press, "back": self._back, "type_text": self._type_text, "scroll": self._scroll,
                          "alert": self._alert, "swipe": self._swipe}
        self._media_earlier = frozenset(medialib.ledger_ids())  # pictures recorded before this daemon started (media remove)
        self._journeys: dict[str, journey.Journey] = {}  # {agent: its journey} (AGENT-TOOLS §17): one at a time
        # an agent's journey start, stop and stress moments run one at a time, each to its end (its bookkeeping too): a
        # stop that came while a stress set the network would otherwise unregister the journey first (Codex, PR #7)
        self._journey_locks: dict[str, asyncio.Lock] = {}
        self._performed: dict[str, performance.Reading] = {}  # {agent: its last `performance` reading}, for `under`
        self._measured: dict | None = None  # the last reading's numbers, for the action log
        self._network: str | None = None  # the network condition on now (AGENT-TOOLS §16.4): offline, 3g or lossy
        # who set it and when they last called: a condition whose agent has gone quiet for NETWORK_IDLE is cleared, so
        # the phone isn't left slow after that agent's session ends (the daemon itself may live on for others)
        self._network_by: str | None = None
        self._network_seen = 0.0
        self._network_set_at = 0.0  # when it was set (the hard cap NETWORK_MAX counts from here)
        self._network_watch: asyncio.Task | None = None
        self._network_notes: dict[str, str] = {}  # {agent: why its condition ended}, told in its next answer
        # Which condition is on: a number that grows with every one set. A clear that names the number it means ends
        # that condition only, so an agent's cleanup can't end the condition another agent set after it. Setting and
        # clearing run one at a time under the lock, and the check and the clear are one step.
        self._network_gen = 0
        self._network_lock = asyncio.Lock()
        # {agent: when it last said it lives}: an MCP process sends a heartbeat every few seconds (agent_alive) and a
        # goodbye as it ends (agent_ended); a condition whose agent is silent for NETWORK_OWNER_LOST is cleared
        self._agent_alive: dict[str, float] = {}
        # agents that said goodbye (a client id is one MCP process's, never used again): a call of theirs that arrives
        # late, or is still setting a condition when the goodbye comes, must not leave one on the phone
        self._agents_ended: dict[str, None] = {}
        # {client: its tool calls in flight} and {client: when its last call began or ended}: the schedules' busy test
        # (phone_in_use) and the refusal of other agents' actions while a scheduled run drives (review 1.7 B-H1)
        self._calls: dict[str, int] = {}
        self._call_at: dict[str, float] = {}
        self.schedules_folder: pathlib.Path | None = None  # where the schedules are (None: ~/.tapwright/schedules)
        # the device reports a condition that ended by itself or with its tunnel (L6)
        self.session.device.on_network_end = self._network_over

    # ---- who uses the phone, for the schedules (§22.3) ----
    def phone_in_use(self) -> bool:
        """The schedules' busy test (review 1.7 B-H1): a person or an agent drives now (the queue's own 3 s), a client has
        a tool call in flight or made one in the last IN_USE_S, a person acted in the live view in the last IN_USE_S, or
        a scheduled run is going. An agent thinking between its calls still holds the phone; a scheduled run's own
        finished calls don't (only one run goes at a time: the phone lock, schedulerun)."""
        now = time.monotonic()
        return (self.control.driver is not None or now - self._person_at < IN_USE_S
                or any(n > 0 for n in self._calls.values())
                or any(now - at < IN_USE_S for c, at in self._call_at.items() if not scheduled(c)))

    def _someone_else_on_phone(self, client_id: str) -> bool:
        """For a scheduled run's admission (M2): phone_in_use without the run's own activity. A person drives, acted in the
        last IN_USE_S or paused the live view, another client has a call in flight, or an agent (not a scheduled run)
        called in the last IN_USE_S. The queue's own `agent` driver isn't asked: the run's trigger check, just before,
        is that."""
        now = time.monotonic()
        return (self.control.driver == "person" or self.control.paused or now - self._person_at < IN_USE_S
                or any(n > 0 for c, n in self._calls.items() if c != client_id)
                or any(now - at < IN_USE_S for c, at in self._call_at.items() if c != client_id and not scheduled(c)))

    def scheduled_running(self) -> bool:
        """Is a scheduled run's call in flight?"""
        return any(n > 0 and scheduled(c) for c, n in self._calls.items())

    @contextmanager
    def _phone_use(self, client_id: str):
        """Count a tool or external handover, including its quiet period after cancellation or failure."""
        now = time.monotonic()
        for gone in [c for c, at in self._call_at.items() if now - at >= IN_USE_S and not self._calls.get(c)]:
            del self._call_at[gone]
        self._calls[client_id] = self._calls.get(client_id, 0) + 1
        self._call_at[client_id] = now
        try:
            yield
        finally:
            left = self._calls.get(client_id, 1) - 1
            if left > 0:
                self._calls[client_id] = left
            else:
                self._calls.pop(client_id, None)
            self._call_at[client_id] = time.monotonic()

    # ---- status and events ----
    def status(self) -> dict:
        w, h = self.session.screen
        via = (getattr(self.session.device, "via", None) or "").lower()
        via = ("usb" if via == "usb" else "wifi" if via == "local network"
               else "tailscale" if via.startswith("network") else None)
        return {"type": "status", "protocol": PROTOCOL, "tool": "Tapwright", "state": self.state, "detail": self.detail, "via": via,
                "fps": self.hub.fps(), "driver": self.control.driver, "paused": self.control.paused,
                "in_use": self.phone_in_use(),  # (the schedules' busy test, §22.3; `driver` is the queue's own)
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

    def phone_last_seen(self) -> float | None:
        """(1.10.0, review R191 L4) When (monotonic) a phone was last known to be there: now while it is live or locked,
        or stuck on a lock or the passcode prompt; else when that last ended; None: never. The idle stop waits for this to
        be older than its limit, whatever the state's name (a `--address` phone that is away reads `reconnecting`)."""
        if self.state in ("live", "locked") or isinstance(self.stuck, PhoneLocked):
            return time.monotonic()
        return self._phone_seen_at

    def _set_state(self, state: str, detail: str | None = None) -> None:
        changed = (state, detail) != (self.state, self.detail)
        if self.state in ("live", "locked") or isinstance(getattr(self, "stuck", None), PhoneLocked):
            self._phone_seen_at = time.monotonic()  # the phone was there until this change
        self.state, self.detail = state, detail
        watch = getattr(self, "watchdog", None)
        if watch is not None:
            watch.state(state, getattr(self.session.device, "via", None))
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
        # an account's value is scrubbed before it is broadcast or logged, as in an answer (Codex, PR #9 cycle 1)
        scrub = self.scrub_everywhere
        label = scrub(label) if label else label
        caption = scrub(caption) if caption else caption
        reason = scrub(reason) if reason else reason
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
            self._bug_person(kind)
            self.actions.write({"source": "tapwright", "by": "person", "tool": kind, "element": label, "ok": ok,
                                "code": code, "app": self._bundle()})
        else:
            self._announced = message

    # ---- network conditions (AGENT-TOOLS §16.4) ----
    def _network_over(self, why: str) -> None:
        """The condition is over without the daemon asking: its connection died, or the tunnel went (the device
        calls this). The agent that set it is told in its next answer."""
        if self._network is None:
            return
        self.log(f"network: {self._network} ended: {why}")
        self._tell_network_setter(f"the network condition ended: {why}.")
        self._network = None
        self._stop_network_watch()
        self._forget_network_marker()

    def _tell_network_setter(self, text: str) -> None:
        if self._network_by is not None:
            self._network_notes[self._network_by] = text

    def _write_network_marker(self, mode: str) -> None:
        """A note on disk that a condition is on, so a daemon that is killed without clearing it finds it at its next
        start (_clear_leftover_network). Never fails the call."""
        try:
            store.NETWORK_FILE.parent.mkdir(parents=True, exist_ok=True)
            store.NETWORK_FILE.write_text(json.dumps({"mode": mode, "pid": os.getpid()}), encoding="utf-8")
        except OSError as e:
            self.log(f"network: couldn't write {store.NETWORK_FILE}: {e}")

    @staticmethod
    def _forget_network_marker() -> None:
        try:
            store.NETWORK_FILE.unlink(missing_ok=True)
        except OSError:
            pass

    async def _clear_leftover_network(self) -> None:
        """A condition that a daemon left on the phone when it ended without clearing it (killed, crashed, the
        computer lost power): its marker is still on disk. Cleared once the line is up, before any agent's first call,
        so the phone isn't slow for whoever comes next (AGENT-TOOLS §16.4). No marker, or a condition of this very
        daemon's: nothing to do. A failure leaves the marker for the next connect."""
        if self._network is not None or not store.NETWORK_FILE.exists():
            return
        clear = getattr(self.session.device, "clear_leftover", None)
        if clear is None:
            self.log("network: a condition may have been left on the phone, and this device can't clear one")
            return
        try:
            async with self._network_lock:
                await asyncio.wait_for(clear(), 15)
        except Exception as e:
            self.log(f"network: couldn't clear a condition left from before: {e}")
            return
        self._forget_network_marker()
        self.log("network: cleared a condition left on the phone by a daemon that ended without clearing it")

    async def _clear_network(self, gen: int | None = None) -> bool:
        """Back to normal, politely (the phone's condition ends when the connection that holds it closes). With `gen`,
        only the condition that was set under that number: false, and nothing done, when another has replaced it."""
        async with self._network_lock:
            if gen is not None and gen != self._network_gen:
                return False
            self._network = None
            self._stop_network_watch()
            self._forget_network_marker()
            clear = getattr(self.session.device, "clear_network", None)
            if clear is not None:
                await clear()
            return True

    def agent_alive(self, client_id: str) -> None:
        """A heartbeat from an agent's MCP process (one that has said goodbye is not heard again)."""
        if client_id not in self._agents_ended:
            self._agent_alive[client_id] = time.monotonic()

    async def agent_ended(self, client_id: str) -> None:
        """An agent's MCP process ended (its stdio closed, or it said goodbye): the condition it set ends with it, so
        the phone isn't left slow for the next agent. A condition another agent set is not touched. The agent is
        marked first, before anything waits: a `network` call of it that is setting a condition right now sees the
        mark when the phone answers, and clears what it set (_network_tool)."""
        # kept for the daemon's life: a call or a heartbeat of this agent can still arrive or be under way however many
        # agents end after it (Codex, PR #6 cycle 3: a cap of 256 forgot it). An id is a few dozen bytes.
        self._agents_ended[client_id] = None
        self._agent_alive.pop(client_id, None)
        self._network_notes.pop(client_id, None)
        if self._network is not None and self._network_by == client_id:
            self.log(f"network: {self._network} cleared, its agent {client_id} has gone")
            await self._clear_network(self._network_gen)

    def _stop_network_watch(self) -> None:
        watch, self._network_watch, self._network_by = self._network_watch, None, None
        if watch is not None and watch is not asyncio.current_task():
            watch.cancel()

    def _network_seen_by(self, client_id: str) -> None:
        """A call from the agent that set the condition: it is still there."""
        if client_id == self._network_by:
            self._network_seen = time.monotonic()

    async def _watch_network(self) -> None:
        """Clear the condition once the agent that set it has made no call for NETWORK_IDLE seconds: its session has
        most likely ended, and the phone shouldn't stay slow for whoever uses it next (AGENT-TOOLS §16.4)."""
        while self._network is not None:
            await asyncio.sleep(NETWORK_CHECK)
            if self._network is None:
                return
            now, gen = time.monotonic(), self._network_gen
            heard = max(self._agent_alive.get(self._network_by or "", float("-inf")), self._network_seen)
            if now - self._network_set_at > NETWORK_MAX:
                # the hard cap: an agent that keeps calling can't keep the owner's phone slow for good
                self.log(f"network: {self._network} cleared, it had been on for {NETWORK_MAX:g} s")
                self._tell_network_setter(f"the network condition ended after {NETWORK_MAX / 60:g} min; set it again "
                                          "if the test needs it.")
                await self._clear_network(gen)
            elif self._network_by in self._agent_alive and now - heard > NETWORK_OWNER_LOST:
                # an agent that sends heartbeats stopped: its process is gone, killed or cut off, with no goodbye
                self.log(f"network: {self._network} cleared, its agent {self._network_by} stopped sending heartbeats")
                await self._clear_network(gen)
            elif now - self._network_seen > NETWORK_IDLE:
                self.log(f"network: {self._network} cleared, its agent {self._network_by} has been quiet "
                         f"{NETWORK_IDLE:g} s")
                await self._clear_network(gen)

    async def _network_tool(self, client_id: str, args: dict, notes: list) -> dict:
        """`network`: put the whole phone's network in a bad condition, or back to normal. Over Wi-Fi, offline and
        lossy would cut Tapwright's own link, so they are `unsupported`."""
        mode = args["mode"]
        if self.control.paused:
            raise Refused("paused", "paused by the user in the live view; ask them to resume")
        device = self.session.device
        if mode == "normal":
            if self._network is not None and self._network_by not in (None, client_id):
                self._tell_network_setter("another agent set the network back to normal.")  # its condition is ended
            await self._clear_network()
            self.log("network: normal")
            self._announce("agent", "network", label="normal")
            return _answer([network.line(mode)], notes)
        if device.rsd is None:
            raise self._not_connected()
        if mode in network.BREAKING and str(getattr(device, "via", "") or "").lower() != "usb":
            raise Refused("unsupported", f"{mode} would cut Tapwright's own link, which runs over the network here "
                                         f"({getattr(device, 'via', None) or 'not USB'}); connect the phone with a cable, "
                                         "or use 3g.")
        gone = Refused("not_allowed", "this agent's session has ended; the network condition was not set.")
        async with self._network_lock:  # set, replace and clear take turns: the generation says which one is on
            if client_id in self._agents_ended:
                raise gone
            try:
                profile = await device.set_network(mode)
            except ConnectionLost as e:
                self.lost(e)
                raise Refused("reconnecting", "the connection dropped while setting the network; wait a few seconds, "
                                              "then look and set it again") from None
            except DeviceError as e:
                raise Refused("unsupported", f"couldn't set the network to {mode}: {e}") from None
            if client_id in self._agents_ended:
                # the goodbye came while the phone was answering: nobody owns this condition, and nobody would clear it
                if self._network is not None:  # (the phone has one condition: this call's set replaced any other)
                    if self._network_by != client_id:
                        self._tell_network_setter("another agent's call replaced your network condition just as its "
                                                  "session ended; set it again if the test needs it.")
                    self._network = None
                    self._stop_network_watch()
                    self._forget_network_marker()
                try:
                    await device.clear_network()
                except Exception as e:
                    self.log(f"network: couldn't clear a condition its agent had already left: {e}")
                raise gone
            if self._network is not None and self._network_by not in (None, client_id):
                # the phone has one condition: this replaces another agent's, and that agent is told
                self._tell_network_setter(f"another agent set the network to {mode}, replacing yours.")
            self._network_gen += 1
            self._network = mode
            self._network_by, self._network_seen = client_id, time.monotonic()
            self._network_set_at = self._network_seen
            self._network_notes.pop(client_id, None)
            self._write_network_marker(mode)
        if self._network_watch is None:
            self._network_watch = asyncio.create_task(self._watch_network(), name="network-watch")
        self.log(f"network: {mode} ({profile})")
        self._announce("agent", "network", label=mode)
        return _answer([network.line(mode)], notes)

    # ---- the connection ----
    def _photo_wait_left(self) -> float | None:
        """Seconds iOS's photo prompt may still hold the runner (a media add waits on the person), else None."""
        if self._photo_wait is None:
            return None
        task, until = self._photo_wait
        left = until - self.control.clock()
        if task.done() or left <= 0:
            self._photo_wait = None
            return None
        return left

    def _held_busy(self, left: "float | None" = None) -> Refused:
        """`busy`, in the words of what holds the phone now (iOS's photo prompt, or a video being added)."""
        left = self._photo_wait_left() if left is None else left
        words = HOLD_WORDS.get(self._photo_wait_kind, PHOTO_WAIT_BUSY)
        return Refused("busy", words.format(left=max(1, round(left or 1))))

    def _hold(self, task: "asyncio.Future", seconds: float, kind: str) -> None:
        """`task` holds the phone for `seconds` from now: the heartbeat skips its check, no silence is a drop, and other
        calls answer busy (HOLD_WORDS[kind])."""
        self._photo_wait = (task, self.control.clock() + seconds)
        self._photo_wait_kind = kind

    async def _after_photo_wait(self) -> None:
        """Wait until iOS's photo prompt no longer holds the runner (the person answered, or the wait's deadline)."""
        while (left := self._photo_wait_left()) is not None and self._photo_wait is not None:
            await asyncio.wait({self._photo_wait[0]}, timeout=left)

    def lost(self, reason: "str | Exception") -> None:
        """A request found the line dead: the supervisor reconnects. When the request was cut because the app in
        front doesn't answer (AppNotAnswering), that is what agents and the live view are told meanwhile."""
        if self._photo_wait_left() is not None:
            # a request that got no answer while iOS's photo prompt holds the runner: not a dead line, and reconnecting
            # would stop the runner and the add with it. The add reports a real drop itself when it ends.
            self.log(f"no answer while a media add holds the runner ({self._photo_wait_kind}): {reason}")
            return
        stuck = reason if isinstance(reason, AppNotAnswering) else None
        if stuck is not None:
            self.stuck = stuck
        if self.state in ("live", "locked"):
            self.log(f"connection lost: {reason}")
            self._set_state("not-answering" if stuck is not None else "reconnecting", self._redact(str(reason))[:200])
        for view in self.control.clients.values():
            view.dropped = True  # every agent's list is from before the drop
        self._thin_place = None  # (PR #21) the place a thin call's read left is from before the drop too
        # A condition is not dropped here: this is also called for a WebDriverAgent hiccup that the supervisor
        # mends without a new tunnel, and the condition lives on the instruments connection, not on WebDriverAgent.
        # The hold reports its own death (Device._hold_died), and a new tunnel ends it (Device.connect).
        self._lost.set()

    def _not_connected(self) -> Refused:
        """No phone found or not set up: the agent can't fix that by waiting. Otherwise the link
        is coming back."""
        if self.state == "no-device":
            return Refused("not_connected", f"no phone to drive ({self.detail}); fix that, then try again")
        if isinstance(self.stuck, PasscodeNeeded):
            return Refused("locked", "the phone is asking for its passcode to allow UI automation; ask the user "
                                     "to enter it on the phone")
        if isinstance(self.stuck, HELD_BEFORE_START):  # no runner was launched: `unlock` has nothing to type through
            return Refused("locked", f"{self.stuck}; ask the user")
        if isinstance(self.stuck, PhoneLocked):
            return Refused("locked", PHONE_LOCKED)
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
                    if not await self._session_again_on_runner(s, e):
                        self.log(f"restarting WebDriverAgent didn't work ({e}); reconnecting")
                        await s.reset()
                        await s.ensure()
            else:
                await s.ensure()
            s.session_settings = {**PRESETS[self.preset], **NO_IDLE_WAIT}  # (a session re-created later gets them in ensure)
            await s.settings(s.session_settings)
            self._fps = self._preset_fps()
            await s.read_scale(refresh=True)  # once per connection, so every header says @3x, not only SpringBoard's
            await s.read_app_names(refresh=True)  # so the header's App: line says "Settings", not a bundle id

    async def _session_again_on_runner(self, s, error: Exception) -> bool:
        """(1.10.0, plug and forget) A new session timed out on a runner that is still running and still answers /status
        (11-19 05:48 and 05:51: an audit kept WebDriverAgent busy, POST /session got no answer in 30 s, and the reset
        that followed replaced the runner). Ask for the session once more on the same runner before anything is torn
        down: a new runner costs iOS's passcode prompt once its consent is about 8 h old. True when that worked; False
        (nothing changed) when it doesn't apply or didn't work, and the caller resets as before."""
        device = s.device
        alive = getattr(device, "runner_alive", None)
        if not isinstance(error, ConnectionLost) or alive is None or not alive():
            return False
        # its /status asked a few times, as before a runner is replaced (device.REUSE_ASKS; CodeRabbit, PR #23 cycle 1):
        # one missed answer from a busy runner must not cost the runner
        probe = getattr(device, "_still_answers", None) or device.wda_alive
        try:
            if not await probe():
                return False
        except Exception:
            return False
        self.log(f"WebDriverAgent didn't answer ({error}), but its runner still answers: asking it for a session again")
        s.sid = None
        before = getattr(device, "runner", None)
        try:
            await s.ensure()
        except (NotSetUp, PhoneLocked, AppNotAnswering):
            raise
        except Exception as again:
            self.log(f"the runner didn't give a session again ({again})")
            return False
        # (review R191 L1) the second try can still replace the runner (its /status stopped answering meanwhile)
        self.log("kept the running runner" if getattr(device, "runner", None) is before else "replaced the runner")
        return True

    async def supervise(self, stop: asyncio.Event) -> None:
        """Connect, then keep checking; reconnect whenever the line drops. Runs until `stop`."""
        self._agent_touches.start()  # find Tapline's hosts, if any, before the first touch needs them
        pacer = asyncio.create_task(self._pace(stop), name="pace") if self.pace_tick else None
        watch = asyncio.create_task(self._watch_loop(stop), name="loop-watch") if self.pace_tick else None
        dog = asyncio.create_task(self._watch_phone(stop), name="watchdog") if self.watch_tick else None
        try:
            await self._supervise(stop)
        finally:
            for helper in (pacer, watch, dog, *self._log_tasks.values()):
                if helper is not None:
                    helper.cancel()
                    await asyncio.gather(helper, return_exceptions=True)
            self._log_tasks.clear()

    # ---- the watchdog (§24.5) ----
    async def _watch_phone(self, stop: asyncio.Event) -> None:
        """Every `watch_tick` seconds: tell the watchdog the state (a phone gone for its grace is said once), and now
        and then the battery, read over lockdown while the phone is up."""
        while not stop.is_set():
            await _first(self.watch_tick or WATCH_TICK, stop)
            if stop.is_set():
                return
            try:
                await self.watch_once()
            except Exception as e:
                self.log(f"watchdog: {e}")

    async def watch_once(self) -> None:
        self.watchdog.state(self.state, getattr(self.session.device, "via", None))
        now = time.monotonic()
        if self.state in watchlib.UP and now - self._battery_at >= self.battery_every:
            self._battery_at = now
            read = getattr(self.session.device, "battery", None)
            if read is not None:
                try:
                    percent, charging = await asyncio.wait_for(read(), 10)
                except Exception:
                    percent, charging = None, None
                self.watchdog.battery(percent, charging)

    def _watchdog_say(self, kind: str, text: str) -> None:
        """One watchdog message: the log, each host's dialog channel (a `notice`; the MCP server shows it in chat
        while a call is out), and a Note in the next answer of every agent seen so far (once each)."""
        self.log(text)
        for client_id, channel in list(self._channels.items()):
            channel.push({"type": "notice", "id": f"w{secrets.token_hex(4)}", "kind": kind, "message": text})
        for client_id in list(self.control.clients):
            notes = self._watch_notes.setdefault(client_id, [])
            if text not in notes:
                notes.append(text)
                del notes[:-WATCH_NOTES]  # (an agent that never calls again keeps only the newest few)

    def _with_watch_notes(self, client_id: str, text: str) -> str:
        notes = self._watch_notes.pop(client_id, None)
        return "\n".join([text, *(f"Note: {n}" for n in notes if n not in text)]) if notes else text

    # ---- the passcode unlock (§18.6, §11.5) ----
    def _lock_screen(self) -> "unlocklib.LockScreen":
        if self.lock_screen is None:
            self.lock_screen = unlocklib.PhoneLockScreen(self.session)
        return self.lock_screen

    def _passcode_file(self) -> str | None:
        """The vault file the passcode may come from: the one `tapwright serve --accounts` names, never a file a request or
        a call names (review 1.7, H2: a caller's own file would make every guess "a changed passcode")."""
        path = getattr(getattr(self, "ceiling", None), "accounts", None)
        return path if isinstance(path, str) and path else None

    def passcode_for(self) -> str | None:
        """The vault's `phone.passcode`, from serve's own `--accounts` file only: only the tool uses it."""
        path = self._passcode_file()
        if path is None:
            return None
        try:
            loaded = self.accounts.get(path)
        except Exception:
            return None
        if loaded.problem is not None:
            return None
        return loaded.passcode if isinstance(loaded.passcode, str) and loaded.passcode.strip() else None

    async def unlock_phone(self) -> "unlocklib.Result":
        """Unlock a locked phone with the passcode of serve's own vault (unlock.py has the rules). Never while paused or
        while a person drives: they are at the phone. The audit gets `vault` named `phone.passcode` for every passcode
        typed; the log gets fixed words. On success, and when the unlock found the phone not locked (WebDriverAgent said
        so just now), the state is live at once (1.7.8, M2). Its callers: a scheduled run's start (`/api/phone/unlock`),
        a new top-level call to `tapwright serve --phone` (_hold_for_phone), and the agent's `unlock` tool in every mode
        (_unlock_tool; the owner's decision of 2026-11-18). Pause and a person's input are checked here and again right
        before the first key (`may_type`, L2). From the wake to the end of the check no frame reaches an agent or a
        recording (the unlock span, center_tool.py; M1)."""
        if self.control.paused:
            return unlocklib.Result(False, "paused", "paused by the user in the live view; ask them to resume")
        if self.control.person_active():
            return unlocklib.Result(False, "busy", "a person is using the phone; try again in a moment")
        if self.state != "locked":
            return unlocklib.Result(self.state == "live", "not_locked" if self.state == "live" else "not_connected",
                                    "" if self.state == "live" else "the phone isn't connected")
        passcode = self.passcode_for()
        source = unlocklib.Source.of(self._passcode_file())
        generation = self.control.person_generation

        def audit(detail: str) -> None:
            if self.audit is not None:
                self.audit.write("vault", name=unlocklib.AUDIT_NAME, detail=detail)

        def may_type() -> "unlocklib.Result | None":
            if self.control.paused:
                return unlocklib.Result(False, "paused", "paused by the user in the live view; ask them to resume")
            if self.control.person_generation != generation or self.control.person_active():
                return unlocklib.Result(False, "busy", "a person is using the phone; try again in a moment")
            return None

        async def attempt() -> "unlocklib.Result":
            return await self.unlocker.unlock(self._lock_screen(), passcode, audit, source, may_type)
        span = self._unlock_begin()
        try:
            result = await (attempt() if self.control.holds_turn() else self.control.read(attempt))
        except Exception as e:  # (a reconnect under way; a key typed would have been caught in unlock.py itself)
            self.log(f"passcode unlock: couldn't run ({type(e).__name__})")
            return unlocklib.Result(False, "not_run", unlocklib.NOT_RUN)
        finally:
            self._unlock_end(span)
        if result.code == "unlocked":
            self.log("passcode unlock: unlocked the phone with the vault's phone.passcode")
            many = getattr(self.unlocker, "unlocks_last_hour", None)
            count = many() if many is not None else 0
            if count > UNLOCKS_NOTED:  # never limited (1.10.0), but a phone that keeps locking is said (review R191 L3)
                self.log(f"passcode unlock: {count} unlocks in the last hour: the phone keeps locking (Auto-Lock, or "
                         "SpringBoard restarting)")
            if self.state == "locked":
                self._set_state("live")
        elif result.code == "not_locked":
            if self.state == "locked":  # (the unlock read /wda/locked false just now: no wait for the heartbeat)
                self._set_state("live")
        elif result.code not in ("no_passcode",):
            self.log(f"passcode unlock: {result.code}")
        return result

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
                    self.stuck = None  # (as for PhoneNotFound below)
                except PhoneNotFound:
                    self._set_state("no-device", "phone not found: plug it in and unlock it")
                    # (CodeRabbit, PR #23 cycle 1) a lock found before is not a phone that's there: phone_last_seen keeps
                    # the time it was last there (set just now from the old lock), and the idle stop can come
                    self.stuck = None
                except (PhoneLocked, AppNotAnswering) as e:
                    if self.stuck is None:
                        self.log(f"connected, but {e}")
                    self.stuck = e
                    state = "reconnecting" if self._ever_connected else "connecting"
                    self._set_state("not-answering" if isinstance(e, AppNotAnswering) else state, self._redact(str(e))[:200])
                except Exception as e:
                    self.stuck = None
                    self._set_state("reconnecting" if self._ever_connected else "connecting", self._redact(str(e))[:200])
                else:
                    attempt = failures = 0
                    self.stuck = None
                    if not self._ever_connected:  # this daemon's first line to the phone: before any agent's call
                        await self._clear_leftover_network()
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
            if self._photo_wait_left() is not None:
                continue  # a media add holds the runner (iOS's photo prompt, a video's import): no answer is expected, and none is a drop
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
                       policy: "safety.Policy | None" = None, ux_rules: "uxcheck.Rules | None" = None,
                       via: str | None = None, maps: str | None = None, dialog: bool = False,
                       accounts: str | None = None, app_hint: str | None = None, hints: bool | None = None) -> dict:
        """One agent tool call: {"text": str, "image": base64 JPEG or None, "image_size": [w, h] or None}.
        Raises Refused with a code from errors.CODES; any other failure is `internal` (logged).
        Legacy scope/policy arguments are accepted but do not authorize or restrict input.
        A single scoped app can remain navigation metadata for scheduled routes.
        ux_rules, maps and accounts configure QA reports and stored workflow data.
        dialog supports platform moments that require a person's intervention."""
        # Legacy app lists can identify a scheduled route's launch app; they never limit access.
        app_hint = app_hint or (scope.apps[0] if scope is not None and len(scope.apps) == 1 else None)
        floor_rules = policy  # (only explore's and a repair's skip floor reads it: _FLOOR_RULES)
        scope, policy = None, safety.NONE
        left = self._photo_wait_left()
        if left is not None and name not in PHOTO_WAIT_FREE:
            if _JOURNEY_OWN.get() or (_NESTED.get() and self._photo_wait_kind not in VIDEO_HOLDS):
                # a tool's own call (a journey's stress putting the phone back upright): it waits for the person's
                # answer and then runs, instead of being lost from a cleanup that nothing retries (Codex, PR #8 cycle 3).
                # A journey's own restore waits out any hold; another tool's own call (a drive's start, a reflex,
                # performance's tap) waits only for iOS's prompt (130 s at most): a video's copy and import take
                # minutes, so it is told busy at once, in the video's words (review R179b L7)
                await self._after_photo_wait()
            else:
                # it would queue behind the held add, time out, and look like a dead line (Codex, PR #8)
                raise self._held_busy(left)
        if not scheduled(client_id) and name not in SCHEDULE_FREE and self.scheduled_running():
            # a scheduled run drives with no model watching: another agent's action would land in the middle of its
            # route, and its own next step on a screen it didn't leave (review 1.7 B-H1, §22.3)
            raise Refused("busy", SCHEDULED_BUSY)
        if scheduled(client_id) and not self._calls.get(client_id) and self._someone_else_on_phone(client_id):
            # a scheduled run's first call is admitted here, in the same step that counts it as running (no await
            # between): the scheduler's idle status may be stale by now, and another client's call, or a person, has the
            # phone first. Nothing was done; the run waits and asks again (Codex, PR #12 cycle 1, M2)
            raise Refused("busy", schedulelib.ADMISSION_BUSY)
        hint_token = _APP_HINT.set(app_hint)
        rules_token = _FLOOR_RULES.set(floor_rules) if floor_rules is not None else None  # (a nested call keeps its caller's)
        try:
            with self._phone_use(client_id):
                return await self._run_tool_counted(client_id, name, args, scope, policy, ux_rules, via, maps, dialog,
                                                    accounts, hints)
        finally:
            if rules_token is not None:
                _FLOOR_RULES.reset(rules_token)
            _APP_HINT.reset(hint_token)

    async def _run_tool_counted(self, client_id: str, name: str, args: dict, scope: "Scope | None",
                                policy: "safety.Policy | None", ux_rules: "uxcheck.Rules | None", via: str | None,
                                maps: str | None, dialog: bool, accounts: str | None, hints: bool | None = None) -> dict:
        htoken = _HINTS.set(hints) if hints is not None else None
        hide = mapfile.HIDE_AUTO.set(not self._hints_wanted())  # (§30.4) --no-hints: no route that saved itself is shown
        vtoken = _VIA.set(via) if via is not None else None  # how the call came, for the log (session_report)
        mtoken = _MAPS.set(maps) if maps is not None else None
        dtoken = _DIALOG.set(client_id) if dialog and not getattr(self.ceiling, "no_dialog", False) else None  # (serve --no-dialog)
        atoken = _ACCOUNTS.set(accounts) if accounts is not None else None
        rtoken = ui.REDACT.set(self._redactor())  # every screen string is redacted as it is rendered
        try:
            jr = self._journeys.get(client_id)
            if jr is None or name == "journey":
                return await self._run_tool_plain(client_id, name, args, scope, policy, ux_rules)
            return await self._run_tool_in_journey(jr, client_id, name, args, scope, policy, ux_rules)
        finally:
            if via == "call" and not _NESTED.get():
                self._keep_thin_place(client_id)
            if vtoken is not None:
                _VIA.reset(vtoken)
            mapfile.HIDE_AUTO.reset(hide)
            if htoken is not None:
                _HINTS.reset(htoken)
            if mtoken is not None:
                _MAPS.reset(mtoken)
            if dtoken is not None:
                _DIALOG.reset(dtoken)
            ui.REDACT.reset(rtoken)
            if atoken is not None:
                _ACCOUNTS.reset(atoken)

    async def _run_tool_in_journey(self, jr: "journey.Journey", client_id: str, name: str, args: dict,
                                   scope: "Scope | None", policy: "safety.Policy | None",
                                   ux_rules: "uxcheck.Rules | None" = None) -> dict:
        """A call by an agent that has a journey running (§17): checked against what a user could do, answered in the
        user's eyes (ui.USERS_EYES), and counted. The tool's own calls (stress moments, conditions) are `own` (a context
        variable of the task making them, so a call of the agent's made at the same time is checked and counted)."""
        own = _JOURNEY_OWN.get()
        if not own:
            args = self._journey_gate(jr, name, args)
        token = ui.USERS_EYES.set(jr.users_eyes)
        began, outer = self.control.clock(), BATCH.get() is None and not _NESTED.get()
        try:
            answer = await self._run_tool_plain(client_id, name, args, scope, policy, ux_rules)
        except Refused as e:
            if not own and name not in BATCH_TOOLS:
                jr.note_call(name, args, "", self.control.clock() - began, e.code)
            raise
        finally:
            ui.USERS_EYES.reset(token)
        if not own:
            await self._journey_after(client_id, jr, name, args, answer, self.control.clock() - began, outer, scope)
        return answer

    async def _run_tool_plain(self, client_id: str, name: str, args: dict, scope: "Scope | None" = None,
                              policy: "safety.Policy | None" = None, ux_rules: "uxcheck.Rules | None" = None) -> dict:
        scope, policy = None, safety.NONE
        self.last_request = time.monotonic()
        self._network_seen_by(client_id)
        top_level = BATCH.get() is None and not _NESTED.get()  # a batch's own steps were checked with it (batch.check_steps)
        greets = top_level and not _GREETING_HELD.get()
        if top_level and name in params.ALLOWED:
            params.check(name, args)
        wants = self._wants_brief(name, args) if top_level else False  # (bad_params before anything is set)
        # the policy holds for the whole call, a batch's steps included (they call run_tool without one)
        ptoken = _POLICY.set(policy) if policy is not None else None
        utoken = uxcheck._RULES.set(ux_rules) if ux_rules is not None else None
        btoken = _BRIEF.set(wants) if top_level else None
        ftoken = _FIRED.set({}) if top_level else None
        # While a client records, or starts or stops a recording, its outer calls run one at a time (a batch's steps are
        # inside its call): a check's guard, its save and its registration are one step, and the recording a call checks
        # against is the one in force once it holds the lock, read inside it (Codex, PR #3 cycles 6 and 7: a call queued
        # behind a recording's restart checked the old one). Other clients, and a person's input, never wait on it.
        # A journey tool's own calls (its look, its open_app, its network moment) run inside the journey call and
        # never take this lock: the journey call holds its agent's journey lock, and a stop holding this lock while it
        # waits for that one would wait forever (Codex, PR #7 cycle 3). Nothing that holds a journey lock waits for this one.
        outer = _BASELINES.get() is None and not _JOURNEY_OWN.get()
        locks = outer and (client_id in self._recording or name == "record")
        try:
            try:
                if not locks:
                    answer = await self._run_tool_policed(client_id, name, args, scope)
                else:
                    async with self._record_locks.setdefault(client_id, asyncio.Lock()):
                        rec = self._recording.get(client_id)
                        basetoken = _BASELINES.set(rec.setdefault("references", []) if rec is not None else [])
                        try:
                            answer = await self._run_tool_policed(client_id, name, args, scope)
                        finally:
                            _BASELINES.reset(basetoken)
            except Refused as e:
                said = self.scrub_everywhere(str(e))
                if top_level and client_id in self._watch_notes:
                    said = self._with_watch_notes(client_id, said)
                if top_level:
                    said = self._tokens_after(client_id, name, args, said, e.code)
                if said != str(e):  # an account's value is never in an error either
                    raise Refused(e.code, said, draft=e.draft) from None
                raise
            if top_level:
                answer = self._with_network(name, answer)
                if (ended := self._network_notes.pop(client_id, None)) is not None and name != "network":
                    answer = {**answer, "text": f"{answer['text']}\nNote: {ended}"}
            if (said := self.scrub_everywhere(answer["text"])) != answer["text"]:  # ...nor in an answer: `<tester2's username>`
                answer = {**answer, "text": said}
            if top_level and client_id in self._watch_notes:  # the watchdog's messages since this agent's last call
                answer = {**answer, "text": self._with_watch_notes(client_id, answer["text"])}
            if top_level:
                answer = {**answer, "text": self._tokens_after(client_id, name, args, answer["text"], None)}
            return self._first_answer(client_id, answer) if greets else answer
        finally:
            if ptoken is not None:
                _POLICY.reset(ptoken)
            if utoken is not None:
                uxcheck._RULES.reset(utoken)
            if btoken is not None:
                _BRIEF.reset(btoken)
            if ftoken is not None:
                _FIRED.reset(ftoken)

    def _redact(self, text: str) -> str:
        """A screen string without the test accounts' values (the call's accounts file, else the daemon's)."""
        return self.scrub_everywhere(text)

    def _redactor(self) -> "Callable[[str], str] | None":
        """The redactor in force now: this call's (ui.REDACT), else the accounts' scrub; None when no account has a value
        (nothing to redact: every string then goes out as it is)."""
        if (inside := ui.REDACT.get()) is not None:
            return inside
        loaded = self._loaded()
        return self.scrub_everywhere if loaded.redacts or self.accounts._redaction.redacts else None

    @property
    def maps(self) -> "mapkeeper.Maps":
        return self._maps

    @maps.setter
    def maps(self, kept: "mapkeeper.Maps") -> None:
        kept.scrub = self.scrub_everywhere  # whoever builds the maps, what they write holds no account's value
        self._maps = kept

    def scrub_everywhere(self, text: str) -> str:
        """The text without the values of every accounts file this daemon has loaded (the call's, the server's, and any
        earlier one): for what is written outside a call, a log line or a log ring's entry."""
        if getattr(self, "accounts", None) is None:
            return text
        try:
            self._loaded()  # (the call's file and the server's are read, so what they hold is remembered)
        except Exception:
            pass
        return self.accounts.scrub(text)  # one pass over the original text, against every value current and past (cycle 12)

    def _loaded(self) -> "accountlib.Loaded":
        """The test accounts file this call uses (the call's own, else the daemon's), loaded; `accountlib.NONE` with none."""
        return self.accounts.get(_ACCOUNTS.get() or getattr(getattr(self, "ceiling", None), "accounts", None))

    def _wants_brief(self, name: str, args: dict) -> bool:
        """Does this call answer in brief (§13.4)? Its own `brief` decides; without one, the server's `--brief` does.
        `look` never does."""
        if name not in BRIEF_TOOLS:
            return False
        given = args.get("brief")
        if given is None:
            return bool(getattr(self.ceiling, "brief", False))
        if not isinstance(given, bool):
            raise Refused("bad_params", f"brief must be true or false, not {given!r}")
        return given

    def _with_network(self, name: str, answer: dict) -> dict:
        """While a network condition is on, every answer's header carries `Network: 3g (Tapwright, whole phone).`
        (§16.4): as the line after the first. (The `network` call's own answer is that line.)"""
        if self._network is None or name == "network":
            return answer
        lines = answer["text"].split("\n")
        lines.insert(1, network.line(self._network))
        return {**answer, "text": "\n".join(lines)}

    def _first_answer(self, client_id: str, answer: dict) -> dict:
        """The first answer an agent gets carries what only the phone's owner can clear (needs.py), as Note: lines. (A
        refusal says its own: `locked` already names the passcode or the unlock.)"""
        if client_id in self._greeted:
            return answer
        self._greeted.add(client_id)
        # (the passcode prompt is a PhoneLocked too, and says its own note)
        locked = isinstance(self.stuck, PhoneLocked) and not isinstance(self.stuck, PasscodeNeeded)
        lines = [f"Note: {item}" for item in needs.from_state(self.state, self.detail, locked, agent=True)
                 if item not in answer["text"]]
        loaded = self._loaded()
        if (problem := loaded.problem) is not None and problem not in answer["text"]:
            lines.append(f"Note: {problem}")  # the accounts file can't be used: said once, as the first answer
        for said in (loaded.note or "").split("Note: "):  # a plain vault file, a malformed otp setting (§20.2)
            if said.strip() and said.strip() not in answer["text"]:
                lines.append(f"Note: {said.strip()}")
        try:
            book = self._book()
            lines += [line for line in (book.notes() if book is not None else []) if line not in answer["text"]]
        except Exception:
            pass  # (a grant that can't load is said by doctor too; the touch asks a person either way)
        return {**answer, "text": "\n".join([answer["text"], *lines])} if lines else answer

    async def _run_tool_policed(self, client_id: str, name: str, args: dict, scope: "Scope | None") -> dict:
        if name == "journey":  # AGENT-TOOLS §17: its own calls go through run_tool, so personal mode and the policy hold
            return await self._journey_tool(client_id, args, scope)
        if name == "explore":  # AGENT-TOOLS §25: its moves go through run_tool too, one at a time, each an ordinary action
            return await self._explore_tool(client_id, args, scope)
        if name == "bug_report":  # AGENT-TOOLS §29: a page made from a finding, a failed check or the last touches
            if scope is not None and not scope.apps:
                raise Refused("not_allowed", NO_APPS)
            return await self._bug_tool(client_id, args, scope)
        if name == "session_report":  # a read of the log alone: no phone, no app data, and not itself logged
            if scope is not None and not scope.apps:  # personal mode with no app list: nothing runs, this included
                raise Refused("not_allowed", NO_APPS)
            return await self._session_report(client_id, args)
        known = self._hunt_known(client_id, name, args)
        if name == "filmstrip":
            return await self._filmstrip_logged(client_id, args, scope)
        if name in LOGGED:
            started, self._announced, self._granted_now = time.monotonic(), None, False
            aimed = self._aim_for_recording(client_id, name, args)
            # the place the move starts on, for the step a recording saves (§26.6): worked out before the call, which moves it
            ran_on = self._place_before(self.control.client(client_id)) if client_id in self._recording else None
            # (1.8.5, §29.3, §29.6) for a bug report: the touch as a road names it and the place it ran on; a typed value is
            # kept out of every bug report from before it is sent
            trail_from, trail_aim = self._bug_aim(client_id, name, args)
            self._bug_typed(name, args)
            at_turn: list = []  # (the place as the touch got its turn: another agent may have moved the app meanwhile, PR #20)
            recorded_turn = ran_on is not None
            # (PR #21) filled for every touch: the bug trail takes the place at turn as well, recording or not
            turn_token = _PLACE_AT_TURN.set(at_turn)
            typed_secure: list[bool] = []
            secure_token = _TYPED_SECURE.set(typed_secure)
            resolved = _RESOLVED.get()
            own = resolved is None or not _NESTED.get()  # (a nested call's touch is its caller's: performance's tap)
            if own:
                resolved = []
                resolved_token = _RESOLVED.set(resolved)
            said = GrantCall()  # what the grants say about this call's touch, for its log line (§12)
            said_token = _GRANT_CALL.set(said)
            try:
                result = await self._run_tool_inner(client_id, name, args, scope)
            except Refused as e:
                self._give_back(said, e)
                try:
                    self._log_action(name, args, started, e, client=client_id, known_off=known, grants=said)
                except Exception as log_error:  # (the refusal is the answer: a log line that fails never replaces it)
                    self.log(f"the action log skipped a refused {name} ({type(log_error).__name__})")
                raise
            finally:
                _TYPED_SECURE.reset(secure_token)
                _GRANT_CALL.reset(said_token)
                if own:
                    _RESOLVED.reset(resolved_token)
                _PLACE_AT_TURN.reset(turn_token)
            if at_turn and recorded_turn:  # what the touch saw as it went out wins over what was read before its turn
                ran_on = at_turn[0]
            if at_turn and at_turn[0] != trail_from:
                # (PR #21) the bug trail too: the touch ran where it got its turn; a target aimed on the place before isn't
                # kept (the step is then a point, which a path starts after)
                trail_aim = trail_aim if trail_from is None else None
                trail_from = at_turn[0]
            self._log_action(name, args, started, None, client=client_id, answer=result, known_off=known, grants=said)
            self._bug_touch(name, args, trail_aim, trail_from, after=self._place_before(self.control.client(client_id)))
            self._record_step(client_id, name, args, aimed, secure=any(typed_secure), resolved=resolved,
                              place=ran_on[1] if ran_on is not None else None)
            return result
        if (name in READ_LOGGED or name in BATCH_TOOLS) and not _NESTED.get():
            started = time.monotonic()
            try:
                result = await self._policed_other(client_id, name, args, scope)
            except Refused as e:
                self._log_action(name, args, started, e, client=client_id, known_off=known)
                raise
            self._log_action(name, args, started, None, client=client_id, answer=result, known_off=known)
            return result
        return await self._policed_other(client_id, name, args, scope)

    async def _policed_other(self, client_id: str, name: str, args: dict, scope: "Scope | None") -> dict:
        """The tools whose log line is written by the caller (reads, and the batch tools)."""
        if name in BATCH_TOOLS:
            if name in UNLOCK_AT_START:  # serve --phone: a locked phone is unlocked before the first step, never during
                await self._hold_for_phone(client_id, name, dry=bool(args.get("dry_run")))
            return await self._batch_tool(client_id, name, args, None)
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
                                 compared[0].picture if compared else None, fold=BATCH.get() is None)
            return answer
        return await self._run_tool_inner(client_id, name, args, scope)

    def _hunt_known(self, client_id: str, name: str, args: dict) -> bool | None:
        """AGENT-TOOLS §16.1: was this action's target already in the tree's off-screen list before the agent's run
        of scrolls began? (A scroll hunt is exact then, and `session_report` says so without `(maybe)`.) A scroll
        starts a run and keeps the off-screen list as it was; looks leave it; any other call ends it, and the
        action that ends it is the one asked. True only; None when there is nothing to say."""
        if name == "look":
            return None
        if name == "scroll":
            if client_id not in self._hunts:
                self._hunts[client_id] = list(getattr(self.session, "offscreen", []))
            return None
        off = self._hunts.pop(client_id, None)
        raw = args.get("target")
        if not off or not isinstance(raw, dict):
            return None
        try:
            target = ui.parse_target(raw)
            if target.point is not None or target.ref is not None:
                return None
            ui.resolve(off, dataclasses.replace(target, index=0))
        except Refused:
            return None
        return True

    async def _session_report(self, client_id: str, args: dict) -> dict:
        """`session_report` (AGENT-TOOLS §16.1): where this agent's calls went, from the action log."""
        since = args.get("since")
        if since is not None and (isinstance(since, bool) or not isinstance(since, (int, float)) or since < 0
                                  or not math.isfinite(since)):
            raise Refused("bad_params", "since is a time in epoch milliseconds, like 1762950000000.")
        if since is not None:
            since = report.to_epoch_ms(since)
        if self.actions.folder is None:
            return _answer(["The call log is off (tapwright serve --no-log), so there is nothing to report."])
        # the log is read in a worker thread (many files), and only the day files the window can reach
        entries = await asyncio.to_thread(report.read_entries, self.actions.folder,
                                          int(since) if since is not None else None)
        start = int(since) if since is not None else report.client_start(entries, client_id)
        built = report.build(entries, client_id, start)
        if names := self._loaded().names:  # the names of the test accounts, never a value (§18.6)
            built = f"{built}\nAccounts: {', '.join(names)}."
        if others := self._vault_names():  # the vault's other entries, by name and kind (§20.1)
            built = f"{built}\nVault: {', '.join(others)}."
        return _answer([built])

    def _log_action(self, name: str, args: dict, started: float, error: Refused | None,
                    frames: int | None = None, *, client: str | None = None, answer: dict | None = None,
                    known_off: bool | None = None, grants: GrantCall | None = None) -> None:
        """The agent's call, one line in the action log. Typed text only as its length (`chars`), the answer only as
        its length (`answer_chars`), never a picture or log text (AGENT-TOOLS §12, §16.1)."""
        read = name in READ_KINDS
        told = {} if read else (self._announced or {})  # a read carries nothing of the action before it
        message = str(error) if error is not None else ""
        reason = message.split(":", 1)[0] if error is not None and error.code in REASONED else None
        text = args.get("text")
        said = answer["text"] if answer is not None else message
        scrub = self.scrub_everywhere  # an account's value is never in the log (a label, a target's text)
        target = actionlog.target_of(args.get("target"))
        if target is not None:
            target = {k: scrub(v) if isinstance(v, str) else v for k, v in target.items()}
        element = told.get("label")
        self.actions.write({
            "source": "tapwright", "by": "agent", "client": client, "tool": name, "kind": "read" if read else None,
            "target": target,
            "element": scrub(element) if isinstance(element, str) else element, "ok": error is None, "code": error.code if error is not None else None,
            "reason": reason, "ms": round((time.monotonic() - started) * 1000), "via": _VIA.get(),
            "hit_check": error.hit_check if error is not None else None,
            "app": self._bundle(), "chars": len(text) if isinstance(text, str) else None, "frames": frames,
            "answer_chars": len(said) if isinstance(said, str) else None,
            "batch": BATCH.get(), "nested": True if _NESTED.get() else None, "explore": True if _EXPLORING.get() else None,
            "confirmed": True if self._granted_now and not read else None,
            "confirmed_by": self._granted_by if self._granted_now and not read else None,  # "chat" or "the live view"
            # §12 (1.14, 1.14.1): the grant that let the touch through and its count, or why a matching one didn't
            "grant": grants.name if grants is not None and not read and not self._granted_now else None,
            "grant_use": grants.use if grants is not None and not read and not self._granted_now else None,
            "grant_asked": grants.asked if grants is not None and not read and grants.name is None else None,
            "grant_draft": grants.draft if grants is not None and not read else None,  # §21.3: the grant that stopped it at a draft
            "screenshot": True if name == "look" and args.get("screenshot", True) else None,  # (look's default is on)
            "to": args.get("to") if name == "scroll" else None,
            "labels": True if name == "look" and args.get("labels") else None,
            "ocr": True if name == "look" and args.get("ocr") else None,
            "listed": True if not read and error is None and isinstance(said, str) and "Settled in " in said else None,
            "known_off": known_off,
            "measured": self._measured if name == "performance" and error is None else None,
            "count": self._diag_count if name in DIAG_TOOLS and error is None else None})  # counts only, never the text

    def _bundle(self) -> str | None:
        """The bundle id of the app in front at the last read, for the log and a recording."""
        app = getattr(self.session, "app", None)
        return app[1] if isinstance(app, tuple) and len(app) == 2 else None

    # ---- diagnostics (AGENT-TOOLS §15) ----
    def ensure_logs(self, bundle: str) -> None:
        """Start reading that app's log, if it isn't being read (and the phone hasn't been found unable to): it goes
        into the app's ring."""
        task = self._log_tasks.get(bundle)
        if (task is None or task.done()) and bundle not in self._log_dead:
            self._rings.setdefault(bundle, diagnostics.LogRing())
            self._log_tasks[bundle] = asyncio.create_task(self._watch_logs(bundle), name=f"logs-{bundle}")

    def restart_logs(self, bundle: str) -> None:
        """The app was started (open_app): read its new process at once and stop reading the old one. A stream asked
        for a pid may stay open after the process is gone, and a pid may come back for another process, so the old
        reader is never left to find out by itself."""
        task = self._log_tasks.pop(bundle, None)
        if task is not None:
            task.cancel()
        self._log_pids.pop(bundle, None)
        self._log_dead.pop(bundle, None)  # a new start is a new try
        self.ensure_logs(bundle)

    async def _read_log(self, bundle: str, proc: Proc, ring: "diagnostics.LogRing", after, got: list[int]) -> None:
        """One log stream of one process into the ring; `got[0]` counts the entries it gave. `after`: where an earlier
        stream of the same process stopped (the phone replays what it logged just before, which the ring has). An
        entry from before the process started is the previous owner of that pid's, and is dropped."""
        floor = None if proc.started is None else int(proc.started * 1000) - LOG_START_SLACK_MS
        async for item in self.session.device.app_log_stream(proc.pid):
            at, raw, text, entry_pid = item[:4]
            own = bool(item[4]) if len(item) > 4 else True  # (whose code logged it: device.own_line)
            if entry_pid == proc.pid:
                got[0] += 1
                text = self.scrub_everywhere(text)  # a test account's value is never kept, so no retention cut can split one
                if floor is None or at >= floor:
                    # not shown by any answer until the phone's own list has shown the process is the app's (_vouch_logs)
                    ring.add_unseen(diagnostics.LogEntry(at, diagnostics.level_of(raw, text), text, own), after, vouched=False)

    async def _settle_life(self, bundle: str, ring: "diagnostics.LogRing", proc: Proc) -> None:
        """After a stream of `proc` ended, once: were the lines no answer has vouched for the app's? When another process
        has the pid now, its lines (from its start on) are taken back; when the phone can't say who has it, all of them
        are, since nothing vouches for them. Only the ring knows what is vouched for (a `logs` call vouches without the
        watcher looking, so the watcher's own mark can be behind): a vouched line is never taken back."""
        try:
            now = await _wait_for(self.session.device.app_processes(bundle), 5)
            if any(p.same_life(proc) for p in now):
                return
            holder = await _wait_for(self.session.device.process_holder(proc.pid), 5)
        except asyncio.CancelledError:
            raise
        except Exception:
            ring.discard_unvouched()
            return
        if holder is not None:
            self._log_tainted[bundle] = proc
            ring.discard_unvouched(None if holder.started is None else int(holder.started * 1000))
        elif bundle in self._log_tainted and self._log_tainted[bundle].same_life(proc):
            ring.discard_unvouched()  # another process was seen holding the pid: its absence proves nothing
        # (else: gone, with nobody holding the pid, is an ordinary end: its last lines are its own)

    async def _watch_logs(self, bundle: str) -> None:
        """Keep the app's own process's log entries in its ring. An entry of any other pid is never kept (the stream is
        asked for the app's pid alone, and checked again here). While a stream is open, the app's process is looked
        for every `log_check` seconds: when it is another pid, or none, the stream is left (the app started again).
        When the process is gone, look for a new one until `log_idle` seconds have gone by; `ensure_logs` starts the
        task again when it is needed. A stream that can't be started `LOG_STREAM_FAILS` times in a row (no log service
        on this phone) is given up, with waits growing between the tries, and `logs` says so."""
        ring, idle, told, fails = self._rings[bundle], 0.0, False, 0
        while True:
            try:
                procs = await self.session.device.app_processes(bundle)
            except Exception as e:
                if not told:
                    self.log(f"logs: couldn't find {bundle}'s process: {e}")
                    told = True
                procs = []
            if not procs:
                idle += self.log_retry
                if idle > self.log_idle:
                    self._log_pids.pop(bundle, None)
                    return
                await asyncio.sleep(self.log_retry)
                continue
            idle, proc = 0.0, procs[0]
            self._log_pids[bundle] = proc.pid
            prior = self._log_life.get(bundle)
            if prior is not None and not prior[0].same_life(proc):
                # an earlier reader may have taken lines of this process for the old one's (its pid came back): those
                # are from this process's start on
                if proc.started is not None:
                    ring.discard_since(prior[1], int(proc.started * 1000))
                elif proc.pid == prior[0].pid:
                    ring.discard_since(prior[1])
            mark = ring.added  # lines added after this are not yet vouched for as this process's
            self._log_life[bundle] = (proc, mark)
            got: list[int] = [0]
            after = ring.newest()  # (a new process logs later than the old one, so this never drops its entries)
            reader = asyncio.create_task(self._read_log(bundle, proc, ring, after, got), name=f"logs-stream-{bundle}")
            unchecked = 0
            try:
                while not reader.done():
                    await asyncio.wait({reader}, timeout=self.log_check)
                    if reader.done():
                        break
                    asked = ring.added  # (what came before the question is what its answer vouches for)
                    try:
                        now = await _wait_for(self.session.device.app_processes(bundle), 5)
                    except Exception:
                        unchecked += 1
                        if unchecked >= LOG_VERIFY_FAILS:
                            break  # the phone can't say whose the pid is: nothing more is read (and see _settle_life)
                        continue
                    unchecked = 0
                    if any(p.same_life(proc) for p in now):
                        mark = asked  # what came before the question is vouched for
                        ring.vouch(asked)
                        self._log_life[bundle] = (proc, mark)
                        continue
                    # not this process any more: gone, or the pid is another process's now. Its last lines come first,
                    # but only when nobody holds the pid, which is the one case they can't be someone else's
                    try:
                        holder = await _wait_for(self.session.device.process_holder(proc.pid), 5)
                    except Exception:
                        break
                    if holder is None:
                        await asyncio.wait({reader}, timeout=self.log_drain)
                    else:  # seen here, so kept here: the other process may be gone by the time anything looks again
                        self._log_tainted[bundle] = proc
                        # (what a logs call has vouched for since the watcher's last look stays: it was shown to be the app's)
                        ring.discard_unvouched(None if holder.started is None else int(holder.started * 1000))
                    break
            finally:
                reader.cancel()
                await asyncio.wait({reader})
            await self._settle_life(bundle, ring, proc)
            error = reader.exception() if not reader.cancelled() else None
            if error is not None:
                self.log(f"logs: the log stream of {bundle} ended: {error}")
            if error is not None and not got[0]:
                fails += 1
                if fails >= diagnostics.LOG_STREAM_FAILS:
                    self._log_dead[bundle] = str(error) or type(error).__name__
                    self._log_pids.pop(bundle, None)
                    return
            else:
                fails = 0
            await asyncio.sleep(min(self.log_retry * 2 ** fails, LOG_BACKOFF_MAX))

    async def _refresh_logs(self, bundle: str) -> None:
        """Before `logs` answers: read the app's log if it isn't being read, and again if the app started anew (its
        old process's stream says nothing more)."""
        fresh = (bundle not in self._log_tasks or self._log_tasks[bundle].done()) and bundle not in self._log_dead
        self.ensure_logs(bundle)
        if bundle in self._log_tasks and self._connected.is_set():
            try:
                procs = await _wait_for(self.session.device.app_processes(bundle), 5)
            except Exception:
                procs = []
            life = self._log_life.get(bundle)
            if procs and life is not None and not any(p.same_life(life[0]) for p in procs):
                self.restart_logs(bundle)
                fresh = True
        if fresh and self.log_warmup:
            await asyncio.sleep(self.log_warmup)  # the phone replays what the app logged just before
        await self._vouch_logs(bundle)

    async def _vouch_logs(self, bundle: str) -> None:
        """Lines of a stream are shown by no answer until the phone's process list has shown that the process read is
        the app's, after they were read: a pid can go to another app between the watcher's looks. Asked now, so that an
        answer is as good as the phone's word at that moment, and a list that can't be had vouches for nothing."""
        ring, life = self._rings.get(bundle), self._log_life.get(bundle)
        if ring is None or life is None or not ring.unvouched or not self._connected.is_set():
            return
        asked = ring.added
        try:
            procs = await _wait_for(self.session.device.app_processes(bundle), 5)
        except Exception:
            return
        if not any(p.same_life(life[0]) for p in procs):
            if procs:  # the app is another process now: restart_logs reads that one
                return
            try:  # gone: when nobody holds its pid, what it logged last (a crash's lines) is its own; else it is not shown
                holder = await _wait_for(self.session.device.process_holder(life[0].pid), 5)
            except Exception:
                return
            if holder is not None:  # another process holds the pid: what was read since is not the app's, and is taken back
                self._log_tainted[bundle] = life[0]
                ring.discard_unvouched(None if holder.started is None else int(holder.started * 1000))
                return
            if bundle in self._log_tainted and self._log_tainted[bundle].same_life(life[0]):
                ring.discard_unvouched()  # another process was seen holding the pid: its absence proves nothing
                return
        ring.vouch(asked)
        if self._log_life.get(bundle) is life:
            self._log_life[bundle] = (life[0], max(life[1], asked))

    def _diag_app(self, given: dict[str, str] | None) -> tuple[str, str]:
        """(bundle id, how to show it) of the app `logs` and `crash` speak of: the app in front at the last read, else
        the one open_app started, else the only one given. In personal mode only a given app: never another's."""
        front = self._bundle()
        candidates = [b for b in (front if front != SPRINGBOARD else None, self._opened) if b]
        if given is not None and len(given) == 1:
            candidates.append(next(iter(given)))
        for bundle in candidates:
            if given is None or bundle in given:
                name = self.session.app_names.get(bundle)
                return bundle, f"{name} ({bundle})" if name else bundle
        if given is not None and front and front != SPRINGBOARD:
            raise Refused("not_allowed", _outside(front, self.session.app_names.get(front), ", ".join(given.values())))
        raise Refused("not_found", diagnostics.LOGS_NO_APP)

    async def _diag_tool(self, name: str, args: dict, given: dict[str, str] | None) -> dict:
        """`logs` or `crash`. `given`: the apps of a personal-mode agent (None: a dedicated device)."""
        args = _checked_diag(name, args)
        bundle, shown = self._diag_app(given)
        self._diag_count = None
        if name == "logs":
            await self._refresh_logs(bundle)
            ring = self._rings.get(bundle)
            if bundle in self._log_dead and not (ring and ring.entries):
                raise Refused("unsupported", f"{diagnostics.LOGS_NO_SOURCE} ({self._log_dead[bundle]})")
            # (1.9.0 round 2) the app's own lines by default; `all: true`, or --no-hints (1.8.5), shows every line
            own = not args.get("all") and self._hints_wanted()
            text, self._diag_count = diagnostics.logs_answer(self._rings.get(bundle), args.get("since"),
                                                             args["level"], args["grep"], args["limit"], own=own)
            return {"text": text, "image": None, "image_size": None}
        return await self._crash(bundle, shown, args)

    @staticmethod
    async def _phone_files(what: str, call):
        """Await a call to the phone's file or app services, with its errors as Refused: a dropped line is
        `reconnecting`, anything else the phone's services say (an AFC error, a service it doesn't have) is
        `unsupported` with what was being done. Neither is a bug of ours, so neither is `internal`."""
        try:
            return await call
        except (Refused, asyncio.CancelledError):
            raise
        except (ConnectionLost, ConnectionError, TimeoutError, asyncio.TimeoutError) as e:
            raise Refused("reconnecting", f"lost the connection to the phone while trying to {what} ({e}); try again") from None
        except Exception as e:
            raise Refused("unsupported", f"couldn't {what}: {type(e).__name__}: {e}") from None

    async def _executable(self, bundle: str) -> str | None:
        """The name of the app's process (its executable), from the installed apps: crash reports are named after it."""
        if not self._executables.get(bundle):
            for a in await self._phone_files("list the installed apps", self.session.apps()):
                self._executables[a["bundle_id"]] = a.get("executable") or ""
        return self._executables.get(bundle) or None

    async def _crash(self, bundle: str, shown: str, args: dict) -> dict:
        await self.wait_connected()
        exe = await self._executable(bundle)
        if not exe:
            raise Refused("unsupported", diagnostics.CRASH_NO_EXECUTABLE.format(app=shown))
        search = await self._phone_files("read the phone's crash reports",
                                         self.session.device.crash_reports(exe, diagnostics.CRASH_RECENT, bundle))
        infos = [diagnostics.describe_crash(r, exe, self.scrub_everywhere) for r in search.reports]
        skipped = diagnostics.skipped_notes(search)
        # AGENT-TOOLS §15.2: only reports since the session began; `all` is the last 5 of those, not the newest alone
        infos = [i for i in infos if diagnostics.newer(i, self._began_wall)][:diagnostics.CRASH_RECENT if args["all"] else 1]
        self._diag_count = len(infos)
        if not infos:
            return _answer([diagnostics.NONE_SINCE.format(app=shown)], skipped)
        lines: list[str] = []
        for info in infos:
            lines += ["" if lines else None, *diagnostics.crash_lines(info, shown)]
            if args["pull"]:
                lines.append(diagnostics.SAVED.format(path=await self._pull_crash(bundle, info)))
        return _answer([ln for ln in lines if ln is not None], skipped)

    async def _pull_crash(self, bundle: str, info: "diagnostics.CrashInfo") -> str:
        """Copy the whole report from the phone into ~/.tapwright/crashes/<bundle id>/; its path."""
        async def read() -> bytes:
            try:
                return await self.session.device.crash_file(info.remote)
            except CrashTooBig:
                raise Refused("unsupported", f"the crash report {info.filename} is too big to copy (over "
                                             f"{diagnostics.CRASH_MAX_MB} MB)") from None
        data = await self._phone_files("copy the crash report", read())
        path = diagnostics.crash_folder(bundle) / diagnostics.safe_name(info.filename)
        try:
            clean = diagnostics.sanitized_report(data, self.scrub_everywhere)  # no account's value, whatever the app wrote
        except diagnostics.Unsanitizable as e:
            raise Refused("unsupported", f"{info.filename}: {e}") from None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(clean)
        except OSError as e:
            raise Refused("internal", f"couldn't save the crash report to {path.parent}: {e.strerror or e}") from None
        return str(path)

    async def _find_crash(self, bundle: str, polls: int, since: datetime.datetime | None = None):
        """The newest report of that app, not yet told of, from `since` on; looked for `polls` times, as a report
        reaches the phone's folder a moment after the app has gone. None when there is none."""
        try:
            # the whole lookup is optional and bounded, the installed-apps read as well as the report search
            exe = await _wait_for(self._executable(bundle), self.crash_lookup)
        except Exception:
            return None
        if not exe:
            return None
        for n in range(polls):
            try:
                found = await _wait_for(self.session.device.crash_reports(exe, 3, bundle), self.crash_lookup)
            except Exception:
                return None
            for crash in found.reports:
                info = diagnostics.describe_crash(crash, exe, self.scrub_everywhere)
                if info.filename not in self._crash_seen and diagnostics.newer(info, since or self._crash_since):
                    self._crash_seen.add(info.filename)
                    if (seen := _CRASHES.get()) is not None:
                        seen.append(info)
                    return info
            if n + 1 < polls:
                await asyncio.sleep(self.crash_poll)
        return None

    async def _crash_note(self, client_id: str, scope: "Scope | None", name: str, before: str | None,
                          error: Refused | None, pid_before: int | None) -> str | None:
        """_crash_check, which never fails a call: a note is a nicety."""
        try:
            return await self._crash_check(client_id, scope, name, before, error, pid_before)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.log(f"crash check: {e}")
            return None

    async def _crash_check(self, client_id: str, scope: "Scope | None", name: str, before: str | None,
                           error: Refused | None, pid_before: int | None) -> str | None:
        """The note for an answer when the app under test stopped: it was in front (`before`) and is no longer running,
        and a crash report of it has come. Cheap: the phone is asked only when the app left the front (or the call
        ended in an error a crash causes), and then once whether it still runs. A crash whose report hasn't come yet is
        looked for again on this client's next calls while it drives that app (CRASH_LATE_TRIES), and told once, with
        its time, to this client only: another client's call, or this client's call in another app, never gets it."""
        pending = self._crash_pending.get(client_id)
        # the client is in another app when that app is in front (the stopped one isn't, and the home screen is no app)
        elsewhere = [b for b in (before, self._bundle(), self._opened if name == "open_app" else None)
                     if b and b not in (SPRINGBOARD, pending["bundle"] if pending else None)]
        if pending is not None and name not in DIAG_TOOLS and not elsewhere:
            if scope is None or pending["bundle"] in await self._given(scope):
                info = await self._find_crash(pending["bundle"], 1, pending["since"])
                pending["tries"] += 1
                if info is not None or pending["tries"] >= CRASH_LATE_TRIES:
                    self._crash_pending.pop(client_id, None)
                if info is not None:
                    return diagnostics.note_of(info, late=True)
        if name in NO_CRASH_CHECK:
            return None
        target = self._opened if name == "open_app" else before
        left, self._left_front = self._left_front, None  # a touch found the home screen where the app was (_taken_away)
        if not target or target == SPRINGBOARD:
            return None
        cut = error is not None and error.code in CRASH_CUT_CODES
        if self._bundle() == target and not cut and left != target:
            return None
        if scope is not None and target not in await self._given(scope):
            return None
        try:
            pids = await asyncio.wait_for(self.session.device.app_pids(target), 5)
        except Exception:
            return None  # the phone can't say
        pid = None if name == "open_app" else pid_before  # the process the call began with, if it was read
        if pids and (pid is None or pid in pids):
            return None  # it runs: it left the front, or froze
        info = await self._find_crash(target, self.crash_polls)
        if info is not None:
            return diagnostics.note_of(info)
        self._crash_pending[client_id] = {"bundle": target, "tries": 0, "since": self._crash_since}
        return None

    # ---- batches and scripts (AGENT-TOOLS §9) ----
    def _aim_for_recording(self, client_id: str, name: str, args: dict) -> dict | None:
        """While this agent records: the target its step will be saved with, worked out before the call (a ref names
        an element of the list the agent has now). A recording keeps what was recorded, a navigation title too (only
        learned map data keeps titles out, §18.1); a test account's value never (§18.6)."""
        if client_id not in self._recording or name not in batch.STEP_TOOLS or _NESTED.get():
            return None
        if name == "type_text" and args.get("as") is not None:
            routes.recorded_text("", args["as"])  # a name that can't be a variable is refused before the text is typed
        items = self.control.client(client_id).items
        if name == "performance":  # its `tap` is saved by id, label or text like a target
            return self._pre_aim(args.get("tap"), items)
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
        return self._pre_aim(args.get("target"), items, name)

    @staticmethod
    def _pre_aim(target: object, items: list[ui.Element], tool: str | None = None) -> dict | None:
        """The selector worked out before the call. One that holds a test account's value is never worked out from this
        list (it may be stale, or empty before a first look): it is made from the element the touch really aimed at, in
        `_record_step` (Codex, PR #10 cycle 6)."""
        if batch.holds_value(target):
            return None
        return batch.recorded_target(target, items, tool)

    def _record_expect(self, client_id: str, args: dict) -> None:
        """While recording: a `wait_for` that held right after an action becomes that step's `expect`. A wait on a test
        account's value can't be saved (§18.6): the save is refused, naming the step (Codex, PR #10 cycle 6)."""
        rec = self._recording[client_id]
        kind = next((k for k in batch.EXPECT_KINDS if args.get(k) is not None), None)
        if rec["steps"] and kind is not None and rec["steps"][-1]["tool"] == "performance":
            # a performance step takes only `expect.under` (§15.3): the wait is a step of its own
            self._record_step(client_id, "wait_for", args, None)
            return
        if not rec["steps"] or kind is None or "expect" in rec["steps"][-1] or rec["steps"][-1]["tool"] == "compare_screen":
            return
        if isinstance(args[kind], str) and ui.private(args[kind]):
            rec.setdefault("unsaved", []).append(len(rec["steps"]))  # (the step it would be the expectation of)
            return
        if "route" in rec and kind == "id" and isinstance(args[kind], str) and not ui.structural_id(args[kind]):
            return  # (an id that carries content is never written to a route: the step is kept without this check)
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
                        picture: bytes | None,
                        fold: bool = True) -> None:
        """While recording: a `compare_screen` right after an action becomes that step's `expect.matches` (beside the
        text, id or label already expected, if any), when `fold`; otherwise it is a step of its own. Its `name`, the
        `folder` (bundle id) it was compared in and the `picture` (the exact bytes the check used: read from the
        reference it was compared with, or written as the one it saved) are remembered, so `stop` writes those very
        bytes next to the script (§14.3), whatever anyone writes to the store or the script's folder in between. A
        script keeps one picture per name: the same name in another app is refused, and so is the same name against
        other bytes in the same app."""
        rec = self._recording[client_id]
        folder = folder or self._bundle() or "unknown"
        digest = _picture_digest(picture)
        for bundle, seen, before in rec.get("references", ()):
            if _name_key(seen) != _name_key(args["name"]):
                continue
            _refuse_case_only(seen, args["name"])
            if bundle != folder:
                raise Refused("bad_params", f'this recording already compares "{args["name"]}" in {bundle}, and a script '
                                            f"keeps one picture for each name; give the one in {folder} another name")
            if digest is not None and digest != before:
                raise Refused("bad_params", f'record: "{args["name"]}" was already compared against another picture in '
                                            "this recording; give this check another name.")
        extra: dict = {}
        if args.get("threshold") not in (None, compare.DEFAULT_THRESHOLD):  # (the default isn't written down)
            extra["threshold"] = args["threshold"]
        if ignore:
            extra["ignore"] = ignore
        if digest is not None:
            # Bounded: a name has one picture in a recording (the guard above refuses any other), so `pictures` holds at
            # most one PNG per name and `references` one entry per (app, name, picture), however many checks are made.
            rec.setdefault("pictures", {}).setdefault(args["name"], picture)
            if (folder, args["name"], digest) not in rec.setdefault("references", []):
                rec["references"].append((folder, args["name"], digest))
        last = rec["steps"][-1] if fold and rec["steps"] else None
        if last is not None and last["tool"] not in ("compare_screen", "performance") and "matches" not in last.get("expect", {}):
            last["expect"] = {**last.get("expect", {}), "matches": args["name"], **extra}
            return
        step = {"tool": "compare_screen", "name": args["name"], **extra}
        if args.get("width") not in (None, shotlib.DEFAULT_WIDTH):
            step["width"] = int(args["width"])  # (a whole-number float passed the check; a script wants an int)
        rec["steps"].append(step)

    def _typed_account_value(self, rec: dict, step: dict, text: str, secure: bool) -> str | None:
        """The text a recorded `type_text` types, when it is or holds a test account's value (§18.6; Codex, PR #10 cycle 8).
        Text that is exactly an account's username or password is saved as the variable the tools already type
        (`${account.username}` / `${account.password}`, filled from drive's account); that variable is returned. Text that
        merely holds a value, or is the placeholder an answer shows, has no variable to stand for it, and a value that
        doesn't suit the field (a password in a text field) isn't one the variable would type: the step is left unsaved,
        so the save is refused naming it, and its text is cleared. A value from before a reload (not in the file now) is
        refused the same way, whole or held (Codex, PR #10 cycle 9). None for any other text (saved as typed)."""
        if accountlib.mentions(text):
            return None  # the placeholder itself, checked by the typing
        loaded = self._loaded()
        kinds = {kind for _, kind, value in loaded.entries() if value == text}
        if kinds:
            kind = "password" if secure else "username"
            if kind in kinds:
                return "${account." + kind + "}"
        elif not (loaded.holds(text) or self.accounts.holds(text)):
            return None  # (accounts: every value of the daemon's life, current or older)
        rec.setdefault("unsaved_text", []).append(len(rec["steps"]) + 1)
        step["text"] = ""
        return None

    def _record_step(self, client_id: str, name: str, args: dict, aimed: dict | None, secure: bool = False,
                     resolved: list[tuple[ui.Element, list[ui.Element]]] | None = None, place: str | None = None) -> None:
        """An action that succeeded while this agent records becomes a step of its script. `secure`: the field the
        text really went into is a secure one (read by type_text itself, after any scroll); the guess made before
        the call (`next_secure`) counts too, and either hides the text. `resolved`: the element the touch was really
        aimed at, with its list; a selector that holds a test account's value is saved from it (its id, else its label
        or text, else its point), and a step that can't be saved that way makes the save refused (Codex, PR #10 cycle
        6), never left out without a word."""
        rec = self._recording.get(client_id)
        if rec is None or name not in batch.STEP_TOOLS or _NESTED.get():
            return
        key = "tap" if name == "performance" else "target"  # the argument that aims at an element
        step = {"tool": name, **{k: v for k, v in args.items() if k not in (key, "look", "screenshot")}}
        guessed = rec.pop("next_secure", False)
        variable = None
        if name == "type_text" and isinstance(args.get("text"), str) and not ("route" in rec and args.get("as")):
            variable = self._typed_account_value(rec, step, args["text"], guessed or secure)
        if name == "type_text" and (guessed or secure):
            # a password is never written to a file (safety review M4): the user puts a test password in the script
            step["text"] = ""
            step["secure"] = True
            if variable is not None:
                step["text"] = variable  # the account's password, as the variable that types it
            elif isinstance(args.get("text"), str) and accountlib.REFERENCE.fullmatch(args["text"]):
                step["text"] = args["text"]  # an account's placeholder isn't a password: it is what a script should hold
            else:
                rec.setdefault("secure_steps", []).append(len(rec["steps"]) + 1)
        elif variable is not None:
            step["text"] = variable
        step.pop("as", None)
        if variable is not None and "route" in rec and routes.ACCOUNT not in rec["variables"]:
            rec["variables"].append(routes.ACCOUNT)  # a route that types an account's value lists it, so drive is given one
        if name == "type_text" and variable is None and "route" in rec and not step.get("secure") and isinstance(step.get("text"), str):
            step["text"] = routes.recorded_text(step["text"], args.get("as"))  # `${name}`, or the text with a `${` escaped
            if args.get("as") and args["as"] not in rec["variables"]:
                rec["variables"].append(args["as"])
        given = args.get(key)
        if aimed is None and batch.holds_value(given) and resolved:
            found, items = resolved[0]  # the element the touch really went to, in the list it was aimed from
            aimed = batch.recorded_element(found, items, name)
        if "route" in rec and isinstance(aimed, dict) and "x" not in aimed:
            # a route is part of the map: an id that carries content (an event's title), and the label or text of the
            # element that has one, are never written; the point stands for it (review 1.7 B-L3)
            found = resolved[0][0] if resolved and isinstance(resolved[0][0], ui.Element) else None
            if (isinstance(aimed.get("id"), str) and not ui.structural_id(aimed["id"])) or (
                    found is not None and roads.content_id(found)):
                if found is None:
                    rec.setdefault("content_steps", []).append(len(rec["steps"]) + 1)
                    return
                aimed = {"x": round(found.center[0], 1), "y": round(found.center[1], 1)}
        if aimed is not None:
            step[key] = aimed
        elif batch.holds_value(given):
            # a selector that holds an account's value, with no element it reached to save it by: the save is refused
            rec.setdefault("unsaved", []).append(len(rec["steps"]) + 1)
        elif given is not None:
            return  # a target that can't be saved (a ref gone stale): leave the step out rather than guess
        if place is not None:
            step["place"] = place  # where the step ran, so a repair can tell a new screen in the way (§26.6)
        if name in scriptrepair.REPAIRABLE and resolved and isinstance(args.get(key), dict) and args[key].get("x") is None:
            found, items = resolved[0]  # what the touch really aimed at: its facts, with no text in them (§9.4, v1.16)
            step["seen"] = scriptrepair.seen_of(found, items, tuple(self.session.screen or (0.0, 0.0)))
        rec["steps"].append(step)
        rec["app"] = rec["app"] or self._bundle()

    async def _batch_tool(self, client_id: str, name: str, args: dict, scope: "Scope | None") -> dict:
        """`run`, `record` and `replay`."""
        for key in ("trace", "look", "screenshot", "start", "stop", "dry_run", "update_references", "brief"):
            if key in args and not isinstance(args[key], bool):
                raise Refused("bad_params", f"{key} must be true or false, not {args[key]!r}")
        if args.get("update_references") and name != "replay":
            raise Refused("bad_params", "update_references is for replay")
        if name == "record":
            return await self._record(client_id, args, scope)
        if name == "run":
            steps = batch.check_steps(args.get("steps"))
            if args.get("dry_run"):
                return await self._dry_batch(client_id, steps, scope)
            return await self._batch(client_id, steps, scope, args)
        if name == "drive":
            return await self._drive(client_id, args, scope)
        for key in ("name", "path"):
            if args.get(key) is not None and (not isinstance(args[key], str) or not args[key]):
                raise Refused("bad_params", f"{key} must be a non-empty string, not {args[key]!r}")
        if not args.get("name") and not args.get("path"):
            names = batch.saved()
            return _answer([f"Saved scripts: {', '.join(names)}." if names else
                            "No saved scripts yet: record one with record start: true, then stop: true."])
        repair = self._repair_mode(args)  # §26.5: "suggest", "apply" or None; bad_params before anything is touched
        path = batch.resolve(args.get("name"), args.get("path"))
        data = batch.load_script(path)
        token = SCRIPT.set({"path": pathlib.Path(path), "update": bool(args.get("update_references"))})
        # a replay of a saved route runs that route: a grant naming it covers its touches (§19.1). Only the map's own file
        # for it, `routes/<name>.json` in the app's map folder, is that route: a script that names itself so elsewhere is
        # not (review 1.7 A-M1)
        named = self._saved_route_name(path, data)
        running = _ROUTES.set((*_ROUTES.get(), named)) if named else None
        # §21.3 (any version 2 file that says so stops at drafts: it only ever holds back more)
        drafting = _DRAFTS_ROUTE.set(True) if data.get("version") == 2 and data.get("draftsOnly") is True else None
        run_grants = self._grants_run()
        try:
            if repair is not None:
                # (a file that says draftsOnly is never repaired, wherever it lies: like `drafting`, it only holds back more)
                return await self._repair_replay(client_id, data, scope, args, pathlib.Path(path), repair, drafting is not None)
            return await self._replay(client_id, data, scope, args)
        finally:
            SCRIPT.reset(token)
            if drafting is not None:
                _DRAFTS_ROUTE.reset(drafting)
            if running is not None:
                _ROUTES.reset(running)
            if run_grants is not None:
                _RUN_GRANTS.reset(run_grants)

    async def _drive(self, client_id: str, args: dict, scope: "Scope | None") -> dict:
        """`drive` (AGENT-TOOLS §18.4): to a place over the map's proven roads, or by a link. See trip.py. A scheduled run
        (§22.2) opens its route's app first, the one app of its scope, inside this same call (it was admitted, and
        holds the phone, for its whole run: M2): the phone may show any other app or the home screen when it falls due
        (Codex, PR #12 cycle 1, M5)."""
        req = driving.check(args)
        if scheduled(client_id) and (app := _APP_HINT.get()) is not None and not req.dry_run:
            await self.run_tool(client_id, "open_app", {"app": app, "look": False})
        made = trip.Trip(self, client_id, scope, req, args)
        run_grants = self._grants_run()  # a grant revoked while it drives stops it between moves (§19.5)
        try:
            counts = req.route is not None and not req.dry_run and self._hints_wanted()
            try:
                answer = await made.run()
            except Refused as e:
                if counts and made.bundle and made.route_done:
                    # (CodeRabbit, PR #22) the route ran and arrived; a `then` step after it failed: the route worked
                    await self._auto_counted(made.root, made.bundle, req.route, True, made.route_stamp, made.route_end_now)
                elif (counts and made.bundle and made.route_started and e.code in autoroute.FAIL_CODES
                      and not cut_refusal(e)):  # (R200v M1: a screen that couldn't be read is no failed route)
                    # (review 1.9.0 H3) only a failure of the route's own steps counts: not the way to its start
                    said = await self._auto_counted(made.root, made.bundle, req.route, False, made.route_stamp)
                    if said is not None:
                        raise Refused(e.code, f"{e}\nNote: {said}", draft=e.draft) from None
                raise
            if counts and made.bundle:
                end = made.route_end_now
                if made.route_end_moved and end is None:  # a screen the map didn't know yet: the place the arrival saved
                    shown = self.control.client(client_id).place
                    end = shown[1] if shown is not None and shown[0] == made.bundle else None
                await self._auto_counted(made.root, made.bundle, req.route, True, made.route_stamp, end)
            return answer
        finally:
            made.release()
            if run_grants is not None:
                _RUN_GRANTS.reset(run_grants)

    async def _replay(self, client_id: str, data: dict, scope: "Scope | None", args: dict) -> dict:
        """A loaded script's steps, after its fresh start (the SCRIPT context is set by the caller)."""
        steps, notes = await self._replay_prepare(client_id, data, scope)
        return await self._batch(client_id, steps, scope, args, notes)

    async def _replay_prepare(self, client_id: str, data: dict, scope: "Scope | None") -> tuple[list[dict], list[str]]:
        """What a replay does before its steps (also `replay` with `repair`, §26.5): the fresh start, a route's drive to its
        start; the steps as they will run and the notes that go with them."""
        if data.get("fresh", True) and data.get("app"):
            # (these calls are the replay's own: the first-answer note goes on the replay's answer. They are not marked
            # nested: a recording in progress saves the app restart, or a replay of it starts in the wrong app)
            held = _GREETING_HELD.set(True)
            try:
                await self._fresh_start(client_id, data, scope)
            finally:
                _GREETING_HELD.reset(held)
        steps = data["steps"]
        if data.get("version") == 2:  # a route (§18.5): after the fresh start, drive to its start, then its steps
            routes.need(data, {}, None)
            nested = _NESTED.set(True)
            # (review R180b L5) a repair's check replay gates only its repaired steps: the drive to the route's start is the
            # script's own start, as the person wrote it, so the floor doesn't hold it there either (the trial ran it so)
            floor_off = _TOUCH_FLOOR.set(None) if _FLOOR_REPAIRED_ONLY.get() else None
            try:
                await trip.Trip(self, client_id, scope, driving.Request(data["start"], None, {}, None, None, "any", False),
                                {"look": False}).reach()
            finally:
                if floor_off is not None:
                    _TOUCH_FLOOR.reset(floor_off)
                _NESTED.reset(nested)
            steps = routes.substitute(steps, {}, data.get("variables"))
        points = [n for n, step in enumerate(steps, 1)
                  if isinstance(step.get("target"), dict) and step["target"].get("x") is not None]
        notes = [f"Note (step {n}): a point target; it breaks when the layout changes." for n in points]
        if data.get("fresh", True) and data.get("app"):
            notes.append("Note: the app was closed and opened again first (the script's \"fresh\").")
        return steps, notes

    async def _fresh_start(self, client_id: str, data: dict, scope: "Scope | None") -> None:
        """A script's fresh start: the app closed and opened, then step 1's target waited for."""
        # a known start: the app closed and opened again (Tapline reloads its JS instead)
        await self.run_tool(client_id, "close_app", {"app": data["app"]}, scope)
        await self.run_tool(client_id, "open_app", {"app": data["app"], "look": False}, scope)
        # a fresh start takes the app a while (a development build loads its bundle): wait for what step 1 aims
        # at, up to FRESH_WAIT. If it never comes, step 1 says not_found, which is the honest answer.
        first = data["steps"][0]
        aim = None if "if" in first or first.get("optional") else first.get("target")  # (a step that may be skipped isn't waited for)
        kind = next((k for k in ("id", "label", "text") if isinstance(aim, dict) and aim.get(k)), None)
        if kind is not None:
            try:
                await self.run_tool(client_id, "wait_for", {kind: aim[kind], "timeout": FRESH_WAIT, "look": False},
                                    scope)
            except Refused as e:
                if e.code != "timeout":
                    raise

    async def _dry_batch(self, client_id: str, steps: list[dict], scope: "Scope | None") -> dict:
        """`run` with dry_run (§11.4): every step's parameters are checked already; step 1 is resolved and checked like a
        dry run of its own, and nothing touches the phone. Later steps depend on what step 1 does, so they aren't."""
        first, n = steps[0], len(steps)
        if first["tool"] in DRY_TOOLS:
            nested = _NESTED.set(True)  # the dry run's own call: the first-answer note goes on the run's answer
            try:
                answer = await self.run_tool(client_id, first["tool"], {**batch.args_of(first), "dry_run": True}, scope)
            finally:
                _NESTED.reset(nested)
            would = answer["text"].splitlines()[0]
            what = "w" + would[1:] if would.startswith("Would ") else would
        else:
            what = f"would {batch.summary(first)}."
        return _answer([f"Would run {n} step{'s' if n != 1 else ''}; step 1 {what}"])

    async def _route_place(self, scope: "Scope | None") -> tuple[str, str]:
        """(bundle id, place name) of the screen the app is at now, saved if it is new: where a route starts and ends (§18.5)."""
        await self.wait_connected()
        snap = await self._read(lambda: self.session.snapshot())
        if snap.app is None:
            raise Refused("not_found", "no app is in front to record a route in; open_app one first")
        shown, bundle = snap.app
        given = await self._given(scope) if scope is not None else None
        if given is not None and bundle not in given:
            raise Refused("not_allowed", _outside(bundle, shown, ", ".join(given.values())))
        seen = self.maps.observe(self._maps_root(), bundle, snap.tree, arrived=False, lang=self._language.get(bundle))
        if seen is None:
            raise Refused("bad_params", "this screen has nothing to recognise it by, so a route can't start or end here; look, "
                                        "then start from a screen with a title or buttons")
        return bundle, seen.name

    async def _record(self, client_id: str, args: dict, scope: "Scope | None" = None) -> dict:
        if args.get("start") and args.get("stop"):
            raise Refused("bad_params", "record takes start: true or stop: true, not both")
        if args.get("route") is not None and not args.get("start"):
            raise Refused("bad_params", "route goes with start: true, like record {\"start\": true, \"route\": \"sign_in\"}")
        if args.get("start"):
            name = args.get("name") or "recording"
            if not isinstance(name, str):
                raise Refused("bad_params", f"name must be a string, not {name!r}")
            route = args.get("route")
            if route is not None:
                if not isinstance(route, str) or not routes.NAME.match(route):
                    twin = await self._route_twin(route, scope) if isinstance(route, str) else None
                    raise Refused("bad_params", twin or f"a route's name is lower-case ASCII letters, digits and _, not {route!r}")
                self._refuse_held_route(route)
                bundle, start = await self._route_place(scope)
                self._recording[client_id] = {"name": route, "app": bundle, "steps": [], "route": route, "start": start,
                                              "variables": []}
                return _answer([f"Recording route {route} from {start}: every action that succeeds from now on is saved as a "
                                "step; type_text takes as to save its text as a variable. Call record with stop: true to "
                                "save it in the app's map folder."])
            batch.check_name(name)  # ASCII, and no twin of a saved script (the file is written at stop)
            self._recording[client_id] = {"name": name, "app": None, "steps": []}
            return _answer(["Recording: every action that succeeds from now on is saved as a step. Call record with "
                            "stop: true to save the script (under its name, or to a path)."])
        if args.get("stop"):
            rec = self._recording.get(client_id)
            if rec is None:
                raise Refused("bad_params", "nothing is being recorded; start with record start: true")
            self._recheck_typed(rec)
            self._refuse_unsaved(client_id, rec)
            if rec.get("route"):
                # a route's every field is a template at the replay: a literal `${` recorded in a selector, an expectation or
                # a link is written `$${`, so replay gives back the exact text (typed text was written so as it was typed).
                # The copy that is saved is escaped; the recording keeps its raw steps, so a stop that fails (no place to
                # end at) can be tried again without escaping them twice (Codex, PR #10 cycle 15)
                return await self._save_route(client_id, rec, scope, [routes.escaped(step) for step in rec["steps"]])
            path = args.get("path")
            if path is not None and (not isinstance(path, str) or not path):
                raise Refused("bad_params", f"path must be a file path, not {path!r}")
            path = str(batch.writable(path or str(batch.named(rec["name"]))))
            try:
                pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
                # (a backstop: the steps' selectors were chosen without an account's value, §18.6; what is left of the
                # screen's words in them, a typed text or an expectation, is redacted, never a key, a tool or a format,
                # and the result must still be a script that loads: Codex, PR #10 cycle 3)
                saved = batch.saved_script(batch.script(rec["name"], rec["app"], rec["steps"]), self._redact, path)
                pathlib.Path(path).write_text(json.dumps(saved,
                                                         ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            except OSError as e:
                raise Refused("bad_params", f"can't save the script to {path}: {e.strerror or e}") from None
            del self._recording[client_id]
            # the references the steps use travel with the script (§14.3): the exact bytes the checks used, never read
            # from the store again (another client may have rewritten it since)
            wanted = _reference_names(rec["steps"])
            kept = {name: data for name, data in rec.get("pictures", {}).items() if name in wanted}
            folder = compare.script_references(pathlib.Path(path))
            secure = rec.get("secure_steps") or []
            note = ([f"Step{'s' if len(secure) > 1 else ''} {', '.join(map(str, secure))} typed into a secure field: "
                     "the text isn't saved. Put a test password in the file's \"text\" before replaying."]
                    if secure else [])
            try:  # the script is saved already: a copy that fails is said, not an error that loses the answer
                copied = compare.export_to_script(kept, pathlib.Path(path))[1]
            except OSError as e:
                copied = 0
                note.append(f"the reference pictures couldn't be copied to {folder}: {e.strerror or e}; copy them from "
                            f"{compare.references_dir()} by hand, or replay with --update-references.")
            pictures = f", and {copied} reference picture{'s' if copied != 1 else ''} to {folder}" if copied else ""
            return _answer([f"Saved {len(rec['steps'])} steps to {path}{pictures}."], note)
        raise Refused("bad_params", "record takes start: true (with an optional name), or stop: true (with an optional "
                                    "path)")

    def _recheck_typed(self, rec: dict) -> None:
        """The typed text of every step checked again at the save, against the accounts as they are now and every value of
        the daemon's life (Codex, PR #10 cycle 10): a file reloaded after the typing can make a typed literal an account's
        value. Text that is exactly a current value, of the kind the field takes, becomes its variable; text that holds
        any value, or a placeholder, makes the save refused naming the step (`unsaved_text`), never written as the
        placeholder an answer shows. A variable the step already has (`${account.username}`, a route's `${name}`) is
        kept; only the literal text around it is checked."""
        loaded = self._loaded()
        for n, step in enumerate(rec["steps"], 1):
            # every field that isn't typed (a selector, an expectation, an ignore entry, an `if`) is checked as the text it
            # stands for. While recording it is the raw text (a route's `$${` is written only at the save),
            # which is what the decoded literal runs of the saved field are. The save is refused when it holds a value
            # (Codex, PR #10 cycle 13 and 14)
            if any(loaded.holds(s) or self.accounts.holds(s) for s in batch.literal_strings(step)):
                rec.setdefault("unsaved", []).append(n)
            text = step.get("text")
            if step.get("tool") != "type_text" or not isinstance(text, str) or not text:
                continue
            literal, has_variable = batch.literal_runs(text, rec.get("variables") if "route" in rec else None)  # (`$${` decoded)
            kinds = {kind for _, kind, value in loaded.entries() if not has_variable and value == literal[0]}
            if kinds:
                kind = "password" if step.get("secure") else "username"
                if kind in kinds:
                    step["text"] = "${account." + kind + "}"
                    if "route" in rec and routes.ACCOUNT not in rec["variables"]:
                        rec["variables"].append(routes.ACCOUNT)
                    continue
            elif not any(part and (loaded.holds(part) or self.accounts.holds(part)) for part in literal):
                continue
            rec.setdefault("unsaved_text", []).append(n)
            step["text"] = ""

    def _refuse_unsaved(self, client_id: str, rec: dict) -> None:
        """A step that aimed by, or waited for, a test account's value and has nothing else to be saved by makes the save
        refused (§18.6; Codex, PR #10 cycle 6): it is never left out, and the value is never written. The recording
        ends, so it can be made again aiming by an id or a point. The step is named, never the value."""
        typed = sorted(set(rec.get("unsaved_text") or ()))
        if typed:
            del self._recording[client_id]
            many = len(typed) > 1
            raise Refused("bad_params", f"can't save the recording: step{'s' if many else ''} {', '.join(map(str, typed))} "
                                        "typed text that is or holds a test account's value in a way no variable can stand "
                                        "for (only the whole text, a username in a text field or a password in a secure one, "
                                        "is saved, as ${account.username} or ${account.password}). Nothing was saved and the "
                                        "recording has ended: record it again, typing the account's value as the whole text "
                                        "of a step.")
        content = sorted(set(rec.get("content_steps") or ()))
        if content:
            del self._recording[client_id]
            raise Refused("bad_params", f"can't save the route: step{'s' if len(content) > 1 else ''} "
                                        f"{', '.join(map(str, content))} aimed by an id that carries content (a title, a "
                                        "name, a number), which a route never keeps, and there is nothing else to save it "
                                        "by. Nothing was saved and the recording has ended: record it again, aiming at "
                                        "that element by a ref or a point.")
        unsaved = sorted(set(rec.get("unsaved") or ()))
        if not unsaved:
            return
        del self._recording[client_id]
        steps = ", ".join(map(str, unsaved))
        raise Refused("bad_params", f"can't save the recording: step{'s' if len(unsaved) > 1 else ''} {steps} aimed by or "
                                    "waited for a test account's value, which is never saved, and there is nothing else "
                                    "to save it by. Nothing was saved and the recording has ended: record it again, "
                                    "aiming at that element by its id or a point, and waiting for other words.")

    async def _route_twin(self, name: str, scope: "Scope | None") -> str | None:
        """The refusal for a route name that is a saved route's in another case (1.13.1, item 5): it names the existing
        one. None when the app's map has no such route (or the screen can't say which app's map to read)."""
        try:
            bundle, _ = await self._route_place(scope)
        except Refused:
            return None
        other = namelib.twin(name, self.maps.file(self._maps_root(), bundle).load()["routes"])
        return None if other is None else namelib.twin_message("route", name, other)

    async def _save_route(self, client_id: str, rec: dict, scope: "Scope | None", steps: list[dict]) -> dict:
        """`record` with `stop` for a route: the steps (`steps`, as they are written), from the place it started to the
        place it ends at, as a file in the app's map folder (§18.5)."""
        self._refuse_held_route(rec["route"])  # (again at the save: a grant or a schedule may have come meanwhile)
        bundle, end = await self._route_place(scope)
        route = routes.route(rec["route"], rec["app"] or bundle, rec["start"], end, rec["variables"], steps)
        # (a backstop: no step holds an account's value, §18.6; only the screen's words are redacted, and the result is
        # checked as a script before it is saved)
        route = batch.saved_script(route, self._redact, f"route {rec['route']}")
        routes.check(route)
        root = self._maps_root()
        # The last check and the write are one step under the policy file's edit lock, which every grant change takes
        # (§19.5): a grant made while the place was read, or by `tapwright grants` in another process right now, is seen,
        # and the route it names is never replaced (CodeRabbit, PR #12, daemon.py)
        # Every grants file that may name the route is locked (in one order), not only the call's policy's (H2)
        held: list[tuple[grantlib.Book, str]] = []
        try:
            for book in self._route_books():
                token = await asyncio.to_thread(book.take_edit_lock)
                if token is None:
                    raise Refused("busy", f"route {rec['route']} wasn't saved: the policy file is being changed this "
                                          "moment. Stop the recording again in a moment.")
                held.append((book, token))
            try:
                with schedulelib.edit_lock(self.schedules_folder):
                    self._refuse_held_route(rec["route"])
                    path = self.maps.file(root, bundle).write_route(rec["route"], route)
            except schedulelib.ScheduleError as e:
                raise Refused("busy", f"route {rec['route']} wasn't saved: {e}") from None
            except (OSError, ValueError, mapfile.MapError) as e:
                raise Refused("bad_params", f"can't save the route: {getattr(e, 'strerror', None) or e}") from None
        finally:
            for book, token in reversed(held):
                book.release_edit_lock(token)
        self.maps.edit(root, bundle, lambda d: d["routes"].__setitem__(rec["route"], f"routes/{rec['route']}.json"))
        del self._recording[client_id]
        secure = rec.get("secure_steps") or []
        note = ([f"Step{'s' if len(secure) > 1 else ''} {', '.join(map(str, secure))} typed into a secure field: the text isn't "
                 "saved. Use ${account.password} there with a test account (drive's account), or put a test password in the "
                 "file by hand."] if secure else [])
        n = len(rec["steps"])
        return _answer([f"Saved route {rec['route']} ({n} step{'' if n == 1 else 's'}, {rec['start']} > {end}) to {path}."], note)

    async def _batch(self, client_id: str, steps: list[dict], scope: "Scope | None", args: dict,
                     notes: list[str] | None = None) -> dict:
        """Run the steps in order through the ordinary tools; stop at the first that fails or whose expectation
        doesn't hold. One answer for the whole batch: its first line, then what changed since the list the agent
        had before the call, then each step's notes (AGENT-TOOLS §9.3)."""
        view, clock = self.control.client(client_id), self.control.clock
        began, n, ident = clock(), len(steps), f"b{next(self._batch_ids)}"
        traced: list[str] = []
        step_notes = list(notes or [])
        held = [0.0]  # seconds spent waiting for a person (see held()): they don't count toward the batch's limit (§9.3)
        held_token = _HELD.set(held)
        run_grants = self._grants_run()  # (the outermost run's: a grant revoked since stops it between moves, §19.5)
        try:
            return await self._batch_steps(client_id, steps, scope, args, view, clock, began, n, ident, traced,
                                           step_notes, held, held_token)
        finally:
            if run_grants is not None:
                _RUN_GRANTS.reset(run_grants)

    async def _batch_steps(self, client_id: str, steps: list[dict], scope: "Scope | None", args: dict, view, clock,
                           began: float, n: int, ident: str, traced: list[str], step_notes: list[str],
                           held: list[float], held_token) -> dict:
        failed: tuple[str, str] | None = None
        stopped_at_draft = False
        done = 0
        marks: dict[str, int | None] = {}
        set_here: int | None = None  # the generation of the condition this script set and hasn't set back to normal
        i = 0
        start_place = self._place_before(view)  # (1.8.5: where a failed check's path starts, §29.3)
        for i, step in enumerate(steps, 1):
            # when this step began: a failure shows the log from here. The log's times are the phone's clock, which is
            # not this computer's, so the start is the phone's own clock then (or the ring's newest entry, if later)
            if i == 1:
                await self._log_boundary(scope)  # the app's log is read, with the phone's replay, before the first mark
            marks = {b: r.latest() for b, r in self._rings.items()}
            await self._clock_mark(marks)
            if clock() - began - held[0] > batch.BATCH_LIMIT:
                failed = ("timeout", f"Ran {done} of {n} steps; stopped at step {i} ({batch.summary(step)}): the "
                                     f"batch took longer than {batch.BATCH_LIMIT:g} s.")
                break
            if i > 1 and (gone := self._grants_revoked()):
                failed = ("not_allowed", f"Ran {done} of {n} steps; stopped at step {i} ({batch.summary(step)}): "
                                         f"{REVOKED_STOP.format(names=', '.join(json.dumps(g) for g in gone))}")
                break
            t0, token = clock(), BATCH.set({"id": ident, "step": i})
            # (the check replay gates the repaired and inserted steps, and the step an inserted road or scroll leads to, as the
            # trial did: CodeRabbit, PR #20)
            floor_off = (_TOUCH_FLOOR.set(None) if _FLOOR_REPAIRED_ONLY.get() and not scriptrepair.under_repair(steps, i - 1)
                         else None)
            before = len(self._recording[client_id]["steps"]) if client_id in self._recording else None
            try:
                if "if" in step or step.get("optional"):  # one check, right after the step before settled (§9.1)
                    skip = await self._skipped(view, scope, step)
                    if skip is not None:
                        traced.append(batch.skipped(i, skip))  # said whether or not `trace` is on
                        done += 1
                        continue
                for tries in range(REFLEX_TRIES + 1):
                    try:
                        answer = await self.run_tool(client_id, step["tool"], batch.args_of(step), scope)
                        break
                    except Refused as e:
                        if tries == REFLEX_TRIES or e.code not in REFLEX_CODES or not (
                                said := await self._fire_reflexes(client_id, scope)):
                            raise
                        step_notes += said  # a prompt was over its target: closed, and the step goes on (§18.7)
                if step["tool"] == "network":  # a condition this script set, until a step sets it back to normal
                    set_here = self._network_gen if step.get("mode") != "normal" and self._network_by == client_id else None
                recorded = before is not None and len(self._recording[client_id]["steps"]) > before
                lines = answer["text"].splitlines()
                step_notes += [line for line in lines if line.startswith("Reflex: ")]
                step_notes += [line if line.startswith(CRASH_NOTE) else f"Note (step {i}): {line[6:]}"
                               for line in lines if line.startswith("Note: ")]
                if args.get("trace"):
                    traced.append(f"  {i}. {lines[0] if lines else step['tool']} ({clock() - t0:.1f} s)")
                seen_notes: list[str] = []
                if step["tool"] == "performance" and "expect" in step:  # limits, not a screen (PLAN-1.3 item 3)
                    under = step["expect"]["under"]
                    seen_notes += performance.ignored(under)
                    over = performance.failures(self._performed[client_id], under)
                    missed = f"{'; '.join(over)}." if over else None
                else:
                    if (step["tool"] == "open_url" and "expect" in step and self._hints_wanted()
                            and self._link_expect(step["expect"])):
                        # (1.9.0 round 2) a link's expect says when it waited out the app's loading screen
                        missed = await self._expect_after_link(view, scope, step["expect"], seen_notes)
                    else:
                        missed = (await self._expect(client_id, view, scope, step["expect"], seen_notes, fold=recorded)
                                  if "expect" in step else None)
                step_notes += [f"Note (step {i}): {note}" for note in seen_notes]
            except Refused as e:
                if step["tool"] == "wait_for" and step.get("optional") is True and e.code == "timeout":
                    traced.append(batch.skipped(i, batch.optional_wait_skipped(step, unread=unread(e))))  # (1.9.0 round 2)
                    done += 1
                    continue
                message, crash = _without_crash_note(str(e))  # the note goes with the others, at the end
                step_notes += crash
                failed = (e.code, f"Ran {done} of {n} steps; stopped at step {i} ({batch.summary(step)}): {message}")
                stopped_at_draft = e.draft  # (a scheduled run tells a draft stop by it: CodeRabbit, PR #12, trip.py)
                break
            finally:
                BATCH.reset(token)
                if floor_off is not None:
                    _TOUCH_FLOOR.reset(floor_off)
            if missed is not None:
                why = missed if step["tool"] == "performance" else f"its action ran, but {missed}"
                failed = ("expect_failed", f"Ran {done} of {n} steps; stopped at step {i} ({batch.summary(step)}): "
                                           f"{why}")
                break
            done += 1
        _HELD.reset(held_token)
        if failed and set_here is not None and self._network is not None:
            # a script that stops before its own `normal` step would leave the phone slow until the idle clear; one
            # that finishes decides for itself. Only the condition this script set: if another agent has set one since,
            # that one is theirs (the generation says which is on)
            if await self._clear_network(set_here):
                step_notes.append("Note: the network condition this script set was cleared because it stopped at a "
                                  "failing step.")
        first = failed[1] if failed else f"Ran {n} of {n} steps in {clock() - began:.1f} s."
        place_then = view.place  # where the last step's read left the agent, if it read (READ_RUN_STEPS)
        if args.get("look", True):
            view.unlooked = 0  # the block covers the batch: one call, not "N actions ago"
            full = view.seen != self.control.generation or view.dropped or view.read_at == float("-inf")
            answer = await self._after(view, "\n".join([first, *traced]), [], full, moves_content=None)
        else:
            view.unlooked += 1
            answer = _answer([first, *traced])
        if args.get("look", True) and not args.get("trace"):  # the screen it ended on: its place, and the `Place:` line
            answer = await self._learn(view, "run", args, answer, {"from": place_then}, time.monotonic())
        # a failure adds the app's log from the failed step on, after the change block (AGENT-TOOLS §15.4)
        logs = await self._failure_logs(scope, i, marks) if failed else []
        if not _NESTED.get():  # (1.8.5, §29.3) the agent's own run or replay: a failed check is kept for `from: "failure"`
            # (W177b) the place the answer's `Place:` line named: the read after the run, which a thin call's new view needs
            await self._bug_failure_seen(client_id, failed, steps, i, start_place, self._place_before(view), logs,
                                         steps[i - 1].get("expect") if failed and 0 < i <= len(steps) else None,
                                         run_id=ident)
        else:
            self._bug_batch_aims.pop(ident, None)
        answer["text"] = "\n".join([answer["text"], *logs, *step_notes])
        if failed:
            raise Refused(failed[0], answer["text"], draft=stopped_at_draft)
        return await self._add_shot(answer, view, self._shot_args(args, default=False, custom=False))

    @staticmethod
    def held(seconds: float) -> None:
        """Say that the running batch spent `seconds` waiting for a person: for their OK (§11.3) or for a moment that
        needs them to clear (§11.5). That time doesn't count toward the batch's 120 s (§9.3). No batch: nothing."""
        tally = _HELD.get()
        if tally is not None and seconds > 0:
            tally[0] += seconds

    async def _skipped(self, view, scope: "Scope | None", step: dict) -> str | None:
        """Why this step is skipped, or None when it runs (§9.1): its `if` doesn't hold, or it is `optional` and its target
        isn't on screen. One read of the screen with no wait, right after the step before settled; the elements below
        the fold count as there, and the match is `wait_for`'s (`_missing`). The line is `if: "Not now" isn't on
        screen`, `if: "Loading" is on screen` (for `gone`) or `optional: "Not now" isn't on screen`."""
        given = await self._given(scope) if scope is not None else None
        await self._read(lambda: self._silent_read(view))
        if given is not None and self._bundle() not in given:
            # in personal mode what is on screen is told only of a given app, as wait_for does (safety review M3)
            app = getattr(self.session, "app", None)
            raise Refused("not_allowed", _outside(self._bundle(), app[0] if app else None, ", ".join(given.values())))
        there = sorted([*view.items, *self._off_screen_now()], key=lambda e: (e.y, e.x))
        cut = self._tier_now() != "full"
        cond = step.get("if")
        if cond is not None:
            kind = next(k for k in batch.EXPECT_KINDS if cond.get(k) is not None)
            shown = await self._presence(there, ui.parse_target({kind: cond[kind]}), kind, cond[kind], cut,
                                         SKIP_TEXT_WAIT)
            if shown is None:
                # (R200v H2) neither held nor failed: the step isn't run on a guess (a guard keeps its step from running)
                return f"if: {batch.aim_name(kind, cond[kind])} couldn't be checked ({UNREAD})"
            if shown == bool(cond.get("gone")):
                verb = "is" if cond.get("gone") else "isn't"
                return f"if: {batch.aim_name(kind, cond[kind])} {verb} on screen"
        own = step.get("target") if step.get("optional") else None
        if own is not None and self._missing(there, ui.parse_target(own)):
            kind = next(k for k in ("ref", "id", "label", "text") if own.get(k) is not None)
            if cut:
                return f"optional: {batch.aim_name(kind, own[kind])} couldn't be found ({UNREAD})"
            return f"optional: {batch.aim_name(kind, own[kind])} isn't on screen"
        return None
    async def _log_boundary(self, scope: "Scope | None") -> None:
        """Before a batch's first step: start reading the app's log if it isn't read, and wait for the phone's replay of
        what the app logged before. Without that the ring is empty at the step's start, and the replay, arriving after,
        would be shown as logged `since step 1`. Nothing when no app under test is known yet (a ring made later by
        open_app has only the new process's lines)."""
        try:
            given = await self._given(scope) if scope is not None else None
            bundle, _ = self._diag_app(given)
        except Refused:
            return
        task = self._log_tasks.get(bundle)
        if (task is None or task.done()) and bundle not in self._log_dead:
            try:
                await asyncio.wait_for(self._refresh_logs(bundle), 8)
            except Exception:
                pass

    async def _clock_mark(self, marks: dict[str, int | None]) -> None:
        """The boundary of a step, for the log of every app that has one: the phone's own clock now (log times are on
        it), or that ring's newest entry when that is later, whatever its stream still has to replay. Every ring, not
        only the app in front: a step may bring another app to the front, and a failure then shows that app's log from
        the step's start. An old entry's time alone is no boundary: a replay of older lines can still come during the
        step. When the clock can't be read no app has one, and a failure shows none of its log (an unknown start is
        not epoch zero)."""
        if not marks:
            return
        try:
            clock = await _wait_for(self.session.device.phone_time_ms(), 3)
        except asyncio.CancelledError:
            raise
        except Exception:
            clock = None
        # the step began at the phone's clock, or at a ring's newest entry when that is later; never at an old
        # entry alone (the phone may still replay older lines than the newest one during the step)
        for bundle, newest in list(marks.items()):
            marks[bundle] = None if clock is None else max(clock, newest) if newest is not None else clock

    async def _failure_logs(self, scope: "Scope | None", step: int, marks: dict[str, int | None]) -> list[str]:
        """`Logs (since step N):` and up to 20 entries at warning or above, of the app under test (a given app, in
        personal mode), when there are any. `marks`: the newest log time of each ring when the step began (the phone's
        clock, as the entries' own times are). A ring that was empty then holds nothing older than the step."""
        try:
            given = await self._given(scope) if scope is not None else None
            bundle, _ = self._diag_app(given)
        except Refused:
            return []
        try:
            await asyncio.wait_for(self._refresh_logs(bundle), 8)  # the app may have started again as a new process
        except Exception:
            pass
        mark = marks.get(bundle)
        if mark is None:  # where the step began on the phone's clock isn't known: what is older can't be told from what isn't
            return []
        return diagnostics.failure_logs(self._rings.get(bundle), step, mark + 1)

    @staticmethod
    def _link_expect(expect: dict) -> bool:
        """An expect that _expect_after_link checks: one text, id or label, present or gone; nothing else in it."""
        kinds = [k for k in batch.EXPECT_KINDS if expect.get(k) is not None]
        return (len(kinds) == 1 and "value" not in expect and expect.get("matches") is None
                and not batch.shows_text(expect))

    async def _expect_after_link(self, view, scope: "Scope | None", expect: dict, notes: list[str]) -> str | None:
        """(1.9.0 round 2; review Opus 3 H1, M1) An `open_url` step's expect: waited for within its own timeout, as any
        expect is, ending the moment it holds. While waiting, an app on its own loading screen (React Native's "Loading
        from Metro", a loading indicator: reflex.starting's `loading`) is noted, and the answer says it was waited out.
        Nothing more is done: the tool never opens a link the agent didn't ask for. None when it held, else why not."""
        kind = next(k for k in batch.EXPECT_KINDS if expect.get(k) is not None)
        target, gone = ui.parse_target({kind: expect[kind]}), bool(expect.get("gone"))
        began = self.control.clock()
        deadline = began + float(expect.get("timeout", batch.EXPECT_DEFAULT))
        given = await self._given(scope) if scope is not None else None
        loading = 0.0
        while True:
            at = self.control.clock()
            await self._read(lambda: self._silent_read(view))
            if given is not None and self._bundle() not in given:
                app = getattr(self.session, "app", None)
                raise Refused("not_allowed", _outside(self._bundle(), app[0] if app else None, ", ".join(given.values())))
            there = [*view.items, *self._off_screen_now()]
            # (R200v H2) as wait_for: a cut-short tree proves only what it gave, and a text is looked for in the picture
            shown = await self._presence(there, target, kind, expect[kind], self._tier_now() != "full",
                                         deadline - self.control.clock())
            if shown is None and self.control.clock() >= deadline:
                raise Refused("timeout", self._expect_unread(expect))
            if shown is not None and shown != gone:
                if loading > 0:
                    notes.append(f"the app showed its loading screen after the link; the expect waited it out "
                                 f"({self.control.clock() - began:.1f} s).")
                return None
            bundle, tree = self._bundle(), getattr(self.session, "last_tree", None)
            if tree is not None and bundle is not None and reflex.starting(tree, bundle) == "loading":
                loading += max(0.25, self.control.clock() - at)
            if self.control.clock() >= deadline:
                if loading > 0:
                    notes.append("the app was on its loading screen after the link for part of the expect's timeout.")
                return batch.expect_failed(expect)
            await self.control.sleep(0.25)

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
            if done.named:  # the step's note carries what changed under the threshold
                notes.append(done.lines[0])
        if recording and done.reason is None and client_id in self._recording:
            self._record_compare(client_id, {"name": expect["matches"], "threshold": expect.get("threshold")}, kept,
                                 done.folder, done.picture, fold=fold)
        return done.reason

    async def _expect_element(self, client_id: str, view, scope: "Scope | None", expect: dict) -> str | None:
        """Wait up to the expectation's timeout for its text, id or label to hold. None when it did, else why not."""
        kind = next(k for k in batch.EXPECT_KINDS if expect.get(k) is not None)
        timeout = float(expect.get("timeout", batch.EXPECT_DEFAULT))
        if batch.shows_text(expect):
            kind = "id"  # (1.9.0 round 2) the element with that id shows that text: polled as a value is
        if "value" not in expect and not batch.shows_text(expect):
            try:
                await self.run_tool(client_id, "wait_for", {kind: expect[kind], "gone": bool(expect.get("gone")),
                                                            "timeout": timeout, "look": False}, scope)
                return None
            except Refused as e:
                if unread(e):  # (R200v H2) nothing was proven: the step fails as not checked, never as a failed check
                    raise Refused("timeout", self._expect_unread(expect, str(e))) from None
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
            if isinstance(found, ui.Element) and (batch.has_text(found, expect["text"]) if batch.shows_text(expect)
                                                  else batch.has_value(found, expect["value"])):
                return None
            if self.control.clock() >= deadline:
                if found is None and self._tier_now() != "full":
                    # (R200v H2) the element may be among what the cut-short read left out
                    raise Refused("timeout", self._expect_unread(expect))
                return batch.expect_failed(expect)
            await self.control.sleep(0.25)

    @staticmethod
    def _expect_unread(expect: dict, said: str = "") -> str:
        """(R200v H2) Why a step's expect couldn't be checked: the screen's elements weren't read by its deadline. Not a
        failed check (no `expect_failed`, no "Check failed" finding): it carries UNREAD, so a run says it this way."""
        kind = next(k for k in batch.EXPECT_KINDS if expect.get(k) is not None)
        seconds = f"{float(expect.get('timeout', batch.EXPECT_DEFAULT)):g}"
        why = said.split("\n", 1)[0] if said else (f"within {seconds} s {UNREAD}, so whether "
                                                    f"{batch.aim_name(kind, expect[kind])} held isn't known.")
        return f"its expect couldn't be checked: {why}"

    async def _run_tool_inner(self, client_id: str, name: str, args: dict, scope: "Scope | None") -> dict:
        granted: list[bool] = []
        grant_token, scope_token = _GRANTED.set(granted), _SCOPE.set(scope)
        caller_token = _CALLER.set(client_id)
        said = _GRANT_CALL.get()  # (one made for this call's log line; a nested call's own otherwise)
        own_grants = said is None or said.taken
        if own_grants:
            said = GrantCall()
            grants_token = _GRANT_CALL.set(said)
        said.taken = True
        arrival = trace.arrive()  # (None when the trace is off) the touch's trace counts from here, before the front check
        before = self._bundle()  # the app under test when the call began (see _crash_check), and its process if read
        pid_before = self._log_pids.get(before) if before else None
        try:
            # The stream is held open for the length of every tool call; the pump's own linger keeps
            # it open for 30 s after the last one, so the settle always has frames to read.
            # Not when there is no phone: the pump would only retry opening it.
            async with self.pump.watching() if self._connected.is_set() and self.agent_stream else nullcontext():
                await self._hold_for_phone(client_id, name, dry=bool(args.get("dry_run")),
                                           home=name == "press_button" and args.get("name") == "home")
                await self._check_again(client_id, name)  # before personal mode's front check: see there
                try:
                    answer = await self._run_tool(client_id, name, args)
                except Refused as e:
                    # a call the crash cut short ends in an error too: it carries the note as well
                    note = await self._crash_note(client_id, scope, name, before, e, pid_before)
                    if e.code != "dry_run" and (said.notes or note is not None):
                        notes = [*said.notes]  # why a matching grant didn't cover it (§19.3)
                        if note is not None:
                            notes.append(f"Note: {note}")
                        enriched = Refused(e.code, "\n".join([str(e), *notes]), draft=e.draft)
                        enriched.hit_check = e.hit_check
                        e = enriched
                    raise e from None
                note = await self._crash_note(client_id, scope, name, before, None, pid_before)
                if note is not None:
                    answer = {**answer, "text": f"{answer['text']}\nNote: {note}"}
                elif client_id not in self._crash_pending and name not in DIAG_TOOLS:
                    self._crash_since = self._wall()
                if said.notes:  # a grant matched but didn't cover it: said before the person's OK (§19.3)
                    answer = {**answer, "text": "\n".join([answer["text"], *said.notes])}
                if granted:  # a person's OK let a touch through: the answer ends with where it came from
                    self._granted_now, self._granted_by = True, granted[-1]
                    note_of = NOTE_SENT if isinstance(granted[-1], Sent) else NOTE_ALLOWED  # §21.3: a draft a person sent
                    answer = {**answer, "text": f"{answer['text']}\nNote: {note_of.format(origin=str(granted[-1]))}"}
                elif said.line is not None:  # a grant let it through: the answer ends with it and its count
                    answer = {**answer, "text": f"{answer['text']}\n{said.line}"}
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
            trace.leave(arrival)
            _GRANTED.reset(grant_token)
            _SCOPE.reset(scope_token)
            _CALLER.reset(caller_token)
            if own_grants:
                _GRANT_CALL.reset(grants_token)


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
        named = self._alert_names(given, view.alert_now[0])
        if not named:
            return False
        try:
            running = set(await asyncio.wait_for(self.session.device.foreground_apps(), 5))
        except Exception:
            return False  # the phone can't say: refuse, the safe side
        return bool(running & set(named))

    def _alert_names(self, given: dict[str, str], title: str) -> list[str]:
        """The given apps an alert's title names (by the name iOS shows for them)."""
        return [b for b in given if (self.session.app_names.get(b) or given[b].split(" (")[0]) in title]

    async def _alert_about_given(self, given: dict[str, str], view) -> bool:
        """Is the alert on screen another app's, about a given app: Settings' `Delete “Wayfare”?`, drawn by the app in front
        and naming one of the given apps (PLAN-1.6 Part C)? Only `alert` may answer it (see _within). Reads the screen
        first; true only when an alert is up and its title names a given app."""
        await self._read(lambda: self._silent_read(view))
        return view.alert_now is not None and bool(self._alert_names(given, view.alert_now[0]))
    async def _prompt_of_runner(self, view) -> bool:
        """Is the system alert on screen iOS's own question to the runner (WebDriverAgentRunner), such as the photo
        prompt `media` raises? Its title names the runner. Read on the live screen first."""
        await self._read(lambda: self._silent_read(view))
        return view.alert_now is not None and RUNNER_NAME in view.alert_now[0]

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

    def _scope_by_read(self, client_id: str, name: str, args: dict) -> bool:
        """Will this touch's own tree read say which app is in front? A tap on an id, label or text that reads the
        tree before touching (the list isn't current, or the target is missing from it): the scope check is then
        made on that read (_scope_from_read), not by a separate request. A guess: act() decides again and, when it
        doesn't read after all, or the read doesn't name a given app, falls back to the question (Rescope)."""
        if name != "tap" or args.get("dry_run"):
            return False
        try:
            target = ui.parse_target(args.get("target"))
        except Refused:
            return False
        if target is None or target.kind not in ("id", "label", "text"):
            return False
        view = self.control.client(client_id)
        return view.opened or not self._current(view) or self._missing(view.items, target)

    def _scope_by_touch(self, name: str, args: dict) -> bool:
        """Will this tap's own touch check the app in front (1.6 E3)? A tap (not a dry run) on a runner whose fast tap
        takes the given apps (Session.front_by_touch): it touches only while one of them is in front, else nothing is
        touched and _within asks the phone as before (FrontUnchecked). Anything that would act before the tap does the
        same. A banner, a prompt of a given app and an iOS alert all come to the question that way."""
        if name != "tap" or args.get("dry_run"):
            return False
        able = getattr(self.session, "front_by_touch", None)
        return bool(able is not None and able())

    def _scope_from_read(self, given: dict[str, str]) -> None:
        """Personal mode's check for a touch, on the tree just read: the root Application's bundle id names the app
        in front (as a look's `App:` line has always done). Only an app the agent was given, with no alert up
        (an iOS prompt is drawn by SpringBoard and has its own rules), counts; SpringBoard (a banner shows as its
        tree), no bundle id, or anything else is Rescope: the separate question, as before."""
        root, alert = self._read_root or (None, True)
        if root is None or alert or not root[1] or root[1] == SPRINGBOARD or root[1] not in given:
            raise Rescope()

    def _touch_in_scope(self) -> None:
        """Personal mode, a tap whose scope check came from its own read (_SCOPE_BY_READ): just before each touch it
        makes (the session's touch hook, and the keyboard's own dismissal), the newest read since its last touch must
        name a given app (_scope_from_read). A read of another app (a loading wait's poll that found app B, a scroll
        or a keyboard closed on the way), or no read since its last touch, touches nothing: Rescope, the separate
        question, as before (Codex, PR #9 cycle 1). A check that passed is used up by its touch. Elsewhere: nothing."""
        given = _SCOPE_BY_READ.get()
        if given is None:
            return
        touched = _SCOPE_TOUCHED.get()
        try:
            self._scope_from_read(given)
        except Rescope:
            if touched:  # a touch of this call went out before (a keyboard closed): refs from before it are stale
                self.control.touched()
            raise
        self._read_root = None
        if touched is not None:
            touched.append(True)


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
            # what a stopped batch says of the given app itself (its logs since the step, its crash note) was made for
            # that app alone, in personal mode, and stays when its screen is gone (CLI replay reads it)
            app_only = diagnostics.junit_output(answer["text"])
            text = f"{first}Note: {_outside(bundle, named, names, alert)}" + (f"\n{app_only}" if app_only else "")
            return {"text": text, "image": None, "image_size": None}
        return answer

    async def _run_tool(self, client_id: str, name: str, args: dict) -> dict:
        self._scrolled_first.discard(client_id)
        earned: list[str] = []
        token = _REFLEXED.set(earned)
        traced = trace.begin(name)  # (None unless TAPWRIGHT_TRACE=1 and a touch tool)
        try:
            answer = await self._run_tool_core(client_id, name, args)
            if client_id in self._scrolled_first:  # the action scrolled to its target first (§16.2): the first line says so
                answer = {**answer, "text": self._said_scrolled(answer["text"])}
            if earned:  # what the tool did by itself on the way (§18.7), said after the first line
                first, sep, rest = answer["text"].partition("\n")
                answer = {**answer, "text": "\n".join([first, *earned, *([rest] if sep else [])])}
            return answer
        finally:
            trace.end(traced)
            _REFLEXED.reset(token)
            self._scrolled_first.discard(client_id)
            self._policed = None

    @staticmethod
    def _said_scrolled(text: str) -> str:
        """`Tapped e41 button "Save" id=save.` as `Scrolled to it, then tapped e41 button "Save" id=save.`"""
        first, _, rest = text.partition("\n")
        word = first.split(" ", 1)[0]
        if word in ("Tapped", "Long-pressed", "Toggled", "Typed"):
            first = SCROLLED_THEN + first[0].lower() + first[1:]
        else:  # (a toggle that was already as asked: `e5 switch "X" is already true.`)
            first = "Scrolled to it. " + first
        return first + ("\n" + rest if rest else "")

    async def _run_tool_core(self, client_id: str, name: str, args: dict) -> dict:
        if name == "unlock":
            return await self._unlock_tool(client_id)
        if name not in ACTIONS and name not in READS and name not in SPECIAL:
            raise Refused("unsupported", f"no tool named {name!r}; the tools are "
                                         f"{', '.join([*READS, *SPECIAL, *ACTIONS])}")
        missing = [k for k in REQUIRED.get(name, ()) if args.get(k) is None]
        if missing:
            raise Refused("bad_params", f"{name} needs {', '.join(missing)}")
        if name in DIAG_TOOLS:  # a dedicated device; personal mode answers these in _within
            return await self._diag_tool(name, args, None)
        args = self._ocr_target(client_id, args)
        if "index" in args and isinstance(args.get("target"), dict) and "index" not in args["target"]:
            # `index` belongs inside `target`; a first-time agent put it beside it and got an ambiguous error back
            # with no hint (2026-11-12). Taken where it was meant to go.
            args = {**{k: v for k, v in args.items() if k != "index"}, "target": {**args["target"], "index": args["index"]}}
        args = _checked(name, args)
        shot = self._shot_args(args, default=name == "look", custom=name == "look")  # bad width: refused here
        if self.unlocking() and (name in ("filmstrip", "compare_screen") or shot is not None or args.get("ocr")):
            raise Refused("busy", UNLOCK_BUSY)  # (1.7.8, M1: the keypad shows each key as it is touched)
        # A bad target was refused by _checked, before anything is queued or sent. Any tool whose
        # arguments carry `target` goes through here, so the live-read rule in act() covers it too.
        target = args.get("target")
        view = self.control.client(client_id)
        # an agent that hasn't looked yet has no "last look": its first list is whole anyway
        notes = [NOTE_PERSON] if 0 <= view.seen < self.control.person_generation else []
        if name == "close_app" and isinstance(self.stuck, AppNotAnswering):
            return await self._close_stuck(self.stuck, args["app"], notes)
        if name == "press_button" and args.get("name") == "home" and isinstance(self.stuck, AppNotAnswering):
            return await self._home_stuck(self.stuck, notes)
        if (name in FROZEN_TOOLS and isinstance(self.stuck, AppNotAnswering)
                and (frozen := await self._frozen_tool(client_id, name, args, notes, shot)) is not None):
            return frozen
        await self.wait_connected()
        if (taken := self._taken_notes.pop(client_id, None)) is not None:
            notes = [*notes, taken]
        if name == "apps":
            return await self._apps(notes)
        if name == "notifications":  # §24.2: personal mode's apps only (_GIVEN), the whole phone otherwise
            return await self._notifications_tool(client_id, args, notes, _GIVEN.get())
        if name == "map":
            return await self._map(view, args, notes)
        if name == "wait":
            return await self._wait(view, args, notes, shot)
        if name == "wait_for":
            return await self._wait_for(view, args, notes, shot)
        if name == "audit":
            return await self._audit(view, args, notes)
        if name == "ux_check":
            return await self._ux_check(client_id, view, notes)
        if name == "media":
            return await self._media(client_id, args, notes)
        if name == "performance":
            return await self._performance(client_id, args, notes)
        if name == "network":
            return await self._network_tool(client_id, args, notes)
        if name == "filmstrip":
            return await self._filmstrip(client_id, args, notes)
        if name == "compare_screen":
            return await self._compare_screen(view, args, notes, client_id)
        if name == "look":
            await self._settle_pending()
            no_ocr = bool(args.get("ocr")) and not self._ocr_possible()
            if no_ocr and not self._hints_wanted():
                raise Refused("unsupported", ocr.INSTALL)  # (--no-hints: 1.8.5's refusal)
            view.ocr = {}  # refs o1… belong to one look with ocr: true, and go with the next look
            answer = await self._look(view, shot, [], notes)
            # (2.0) the runner learns its routes with the look's read: now the runner's OCR may be known to be there
            no_ocr = no_ocr and not self._ocr_possible()
            # the agent sees what is in front now: typing without a target goes there (1.7.8, X item 4), and not into
            # another app that comes forward after the look (last Opus review, L); a cut-short look too (R200v M3)
            view.aimed_app = self.session.app if isinstance(self.session.app, tuple) else None
            if (tier := self._tier_now()) != "full" and args.get("ocr") is not False:
                # a screen that didn't answer the whole read: its text, read from the picture, comes without asking
                return await self._with_ocr(view, answer, tier=tier)
            if no_ocr:
                # (1.9.0 round 2) the look the agent asked for, with a note: no failed call and its retry
                answer = {**answer, "text": answer["text"] + "\nNote: " + NOTE_NO_OCR}
            given = _GIVEN.get()  # a screen of another app (personal mode withholds it) is no screen of the journey
            if tier == "full" and (given is None or (self.session.app or (None, None))[1] in given):
                # for naming drift on later ux_checks (AGENT-TOOLS §17.3); never a cut-short list (R200v M1)
                uxcheck.note_screen(client_id, view.items)
            if args.get("ocr") and not no_ocr:  # (without the extra: the look and its note, never a read)
                return await self._with_ocr(view, answer)
            if len(view.items) < FEW_ELEMENTS and ocr.available():  # (without the extra, the description says it)
                answer = {**answer, "text": answer["text"] + "\nNote: " + NOTE_FEW}
            return answer
        if name == "press_button" and args.get("name") == "home" and self.state == "locked":
            return await self._home_over_center(notes)
        if self.state == "locked":
            raise Refused("locked", PHONE_LOCKED)
        call, settle = ACTIONS[name]
        if session_module.TRACE:
            self.log(f"trace {name} begins")
        by_ref = target is not None and target.kind == "ref"
        # these tools at a point act on the element under it, so they need a current list too
        by_lookup = target is not None and (target.kind in ("id", "label", "text")
                                            or (name in ("type_text", "toggle", "scroll") and target.kind == "point"))

        fresh, full, box = [True], [False], {}
        dry = bool(args.get("dry_run"))  # resolve and check, but send nothing to the phone (_dry)
        began = time.monotonic()
        # a move that may be a road (§18.2): not in a dry run, not a call the tool makes itself, not a replay's
        box["mapping"] = name in roads.SAVED_TOOLS and not dry and not _NESTED.get() and SCRIPT.get() is None

        async def act():
            trace.mark("act")  # (the queue's turn came)
            self._require_connected()
            await self._quicken()
            s = self.session
            aim = target  # what the touch is aimed at: after a scroll to an off-screen element, that element's ref
            # a list read mid-redraw can miss elements while the screen looks unchanged since: a target missing
            # from it is read for again before it is not_found
            # (the first list after a launch is read again too: the app may still have been drawing, and a touch
            # aimed with it lands under a search field that came after; seen on iOS Settings, 4 taps in 5)
            live = by_lookup and (view.opened or not self._current(view)
                                  or (target.kind != "point" and self._missing(view.items, target)))
            if by_lookup:
                view.opened = False
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
            self._read_root = None
            if live:
                # Resolve on the live tree. The agent's refs follow their elements onto it, so `e3` still
                # means the same element when it moved. The view's freshness doesn't change: a person's
                # action still makes refs stale, and `view.shown` (what the agent was told) stays put.
                if await self._silent_read(view) and self._missing(s.items, target):
                    # the app may have been mid-redraw: one more look, once it is still, before saying so
                    await self._silent_read(view)
            view.done_taken_back = None  # (CodeRabbit, PR #22) a Done's note belongs to the action that pressed it
            if (touched := _SCOPE_BY_TOUCH.get()) is not None:  # personal mode: the tap itself checks the app in front
                s.front_apps = tuple(touched)
            if (scoped := _SCOPE_BY_READ.get()) is not None:  # personal mode: no question was asked before this tap
                if not live:
                    raise Rescope()  # no read came after all: the question comes first
                self._scope_from_read(scoped)
            if by_lookup and target.kind != "point" and not dry and self._missing(s.items, target) and reflex.loading(s.items):
                await self._wait_loading(view, target)  # (§18.7) a loading indicator: what it loads may be the target
                if scoped is not None:
                    # the wait's reads replaced the list: what they found may be another app's screen, and the check
                    # above was on the first read only (Codex, PR #9 cycle 1)
                    self._scope_from_read(scoped)
            if target is not None and target.kind != "point" and name in TOUCH_FLOOR_TOOLS and _TOUCH_FLOOR.get() is not None:
                # (review R180 L6) explore's and a repair's moves: the gate judges the element before the keyboard reflex's
                # touch or a reach scroll goes out, not only before the touch itself (it judges again on the element reached)
                self._floor_pregate(name, args, target)
            if by_lookup and target.kind != "point" and self._missing(s.items, target):
                if self._missing(s.items, dataclasses.replace(target, index=None)):
                    # an alert hides the app's elements: say so, not "not found". (When only the `index` is
                    # out of range the alert's own elements matched, and that miss is the answer.)
                    self._refuse_behind_alert(view, force=False)
                eyes = ui.USERS_EYES.get()  # a user doesn't know what lies behind a sheet or past an edge (§17.1)
                if not eyes and not await self._lift_from_behind(target, view):
                    self._refuse_behind_sheet(target)  # before off_screen: scrolling can't help until the sheet is closed
                if not eyes and not (name == "scroll" and args.get("to") == "target"):  # that scroll is how it comes on screen
                    if name in KEYBOARD_TOOLS and view.read_keyboard and not dry and self._under_keyboard(target):
                        # the keyboard hides it, and a short form can't scroll it up: a person closes the keyboard
                        # first (a first-time agent was told off_screen for Wayfare's "Create account", 2026-11-11)
                        aimed = self._offscreen_under_keyboard(view, target)
                        closed = await self._dismiss_keyboard_now(view, covered=aimed is not None, aimed=aimed, tool=name)
                        if not self._missing(s.items, target):
                            box["closed"] = closed
                    if "closed" not in box:
                        if (pinned := await self._reach(view, client_id, name, args, target, dry)) is not None:
                            box["scrolled"] = True
                            aim = pinned  # the element that came on screen, by its ref
            use = args if aim is target else {**args, "target": aim}
            # a person would close the keyboard before the touch
            spot = aim.point if isinstance(aim, ui.Target) else None  # an agent's point: a raw touch, no hit test
            under_before = self._control_at(spot, s.items) if spot is not None else None
            box["note"] = box.pop("closed", "") or ("" if dry else await self._close_keyboard(view, name, aim))
            if box["note"] and spot is not None and not args.get("force"):
                self._point_kept(view, spot, under_before)
            s.last_point = None
            # the app the touch is meant for (see _taken_away): the newest read's, the reflex's or a target's own read
            # included, before the touch goes out (CodeRabbit, PR #18 cycle 2); else the last snapshot's
            read_app = getattr(s, "read_app", None)
            box["app"], s.top_touch = (read_app if isinstance(read_app, tuple) else s.app), False
            box["mark"] = self._mark()  # where the stream stands as the gesture goes out
            trace.baseline(box["mark"][1])
            trace.mark("resolved")
            # where the app is and what the move aims at, as the agent's list shows them, before the touch (§18.2)
            box["from"] = self._place_before(view)
            if (turned := _PLACE_AT_TURN.get()) is not None and not dry:
                turned.append(box["from"])
            box["aim"] = self._aim(args, s.items) if box["mapping"] and not dry else None
            if not dry and client_id in self._recording and (touched := _RESOLVED.get()) is not None:
                aimed_at = self._aim(use, s.items)  # the element this very touch is aimed at, in the list it is aimed from
                if aimed_at is not None and aimed_at[1] is not None and aimed_at[0] is not None and aimed_at[0].kind != "point":
                    touched.append((aimed_at[1], aimed_at[2]))
            if name == "open_url":  # (1.7.8, bug 7) what Safari showed before, to tell whether the link loaded
                box["url_before"] = (list(s.items), (view.app or (None, None))[1])
            box["back"] = getattr(getattr(s, "last_tree", None), "back", None)  # what `back` would press, before the touch
            box["title_before"] = _title_of(getattr(s, "last_tree", None))  # the title the screen showed before the touch
            box["began"] = time.monotonic()  # the note's "within N s" counts from here, not from the call's return
            if name in DELIVERED_TOOLS and not dry:
                box["hint"] = self._delivered_hint(args)  # what the touch was aimed at, as the list had it
            if scoped is not None:
                # the newest read is the one the touch is aimed from (a keyboard closed or a scroll on the way read
                # again): it names a given app, or nothing is asked of a person and nothing is touched (Codex, PR #9
                # cycle 1). Each touch is checked once more as it goes out (_touch_in_scope), and the runner's own
                # tap checks the app in front too where it can (1.6 E3)
                self._scope_from_read(scoped)
                if not dry and s.front_by_touch():
                    s.front_apps = tuple(scoped)
            handler = self._handlers.get(name)
            people = self.control.person_generation
            await self._last_checks(name, args, None, hit=False)
            touched_first = bool(box.get("input"))  # the keyboard reflex or a scroll to the target touched already
            try:
                out = await (handler(view, use) if handler else call(s, use))
                box["input"] = True  # (it ended: its answer says what it did)
                return out
            except Refused as e:
                if touched_first and "nothing was touched" in str(e):
                    # (last Opus review, M2) a touch on the way went out: the answer never says nothing was touched
                    e.args = (str(e).replace("nothing was touched", "nothing else was touched"),)
                raise
            finally:
                if self.control.person_generation != people:  # a person acted while it waited for an OK (_aside)
                    fresh[0] = False
                if (name in KEYBOARD_TOOLS and not dry and isinstance(box["app"], tuple)
                        and box.get("input")):
                    # the app this touch was meant for (1.7.8, X item 4). A refusal before any input (typing
                    # without a target while another app is in front) keeps the old one: a retry the same way is
                    # refused again, not typed into the app that came forward (CodeRabbit, PR #18)
                    view.aimed_app = box["app"]

        execution_guard = None

        async def guarded_retype(text, field):
            previous = self.session.execution_guard
            self.session.execution_guard = execution_guard
            try:
                await execution_guard()
                if not await self.session.is_focused(field):
                    raise Refused("stale", "the original input no longer has focus; look before typing again")
                return await self.session.retype(text)
            finally:
                self.session.execution_guard = previous

        async def act_as_agent():
            nonlocal execution_guard
            # the touches inside are the agent's: Tapline is told before each one (tapline.py). Set only here, so a
            # person's input from the live view, queued separately, is never announced as the agent's
            notify = self._agent_touches.notify
            grant_call = _GRANT_CALL.get()
            by_read = _SCOPE_BY_READ.get() is not None
            generation = self.control.person_generation

            async def execution_guard() -> None:
                self._require_connected()
                if self.state == "locked":
                    raise Refused("locked", PHONE_LOCKED)
                if self.control.paused:
                    raise Refused("paused", "the phone is paused; ask the user to resume it")
                if self.control.person_generation != generation or self.control.person_active():
                    raise Refused("busy", "a person used the phone before delivery; look again before acting")

            self.session.execution_guard = execution_guard

            async def touching(ms: float) -> None:
                box["input"] = True  # an input of this call is going out (the aimed app is kept then: act)
                if grant_call is not None:
                    grant_call.touched = True  # (a grant's use is never given back once a touch went out: A-L1)
                if by_read:
                    self._touch_in_scope()  # (personal mode, a tap whose scope came from its read: before each touch)
                await notify(ms)
            self.session.touch_hook = touching
            self.session.banner_guard = False  # banners are visible UI, not a scope boundary
            touched_token = _SCOPE_TOUCHED.set([])
            try:
                return await act()
            finally:
                _SCOPE_TOUCHED.reset(touched_token)
                if self.control.holds_turn():  # (an action that let go of its turn and ended without it set nothing now)
                    self.session.touch_hook, self.session.banner_guard = None, False
                    self.session.before_input = None
                    self.session.execution_guard = None
                    self.session.front_apps = None  # (a tap that checked the app in front cleared it already)

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
            if (taken := view.done_taken_back) is not None:  # (1.9.0) what a Done did is said with this refusal, never later
                view.done_taken_back = None
                e.args = (f"{e}\nNote: {taken}",)
            if (e.code == "not_found" and target is not None and target.point is None
                    and (tier := self._tier_now()) != "full"):
                # (2.0) the screen gave no elements (or only some): say so, and how to reach what it shows; a near
                # miss among the elements it did give comes first (review R200v L3)
                near = self._closest(view, target, str(e)) if tier == "shallow" else None
                raise Refused("not_found", "\n".join([str(e), *([near] if near else []),
                                                      HINT_NO_ELEMENTS if tier == "root" else HINT_PART_ELEMENTS])) from None
            if e.code == "not_found" and (near := self._closest(view, target, str(e))):
                raise Refused("not_found", f"{e}\n{near}") from None
            if e.code == "ambiguous" and self._other_app_shown():
                head = str(e).split("\nCandidates:", 1)[0]
                raise Refused("ambiguous", f'{head}\nLook again, then add "index" (0-based) or use a ref.') from None
            # a touch a finger couldn't make: people watching see the step and why (VIEWER-PROTOCOL 1.4 `reason`)
            if e.code == "not_actionable":
                said = self._redact(str(e))  # (before element_name and the caption cut it, §18.6)
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
        if (changed := view.done_taken_back) is not None:  # (1.9.0) what a Done did to a field is said, never silent
            view.done_taken_back = None
            notes = [*notes, changed]
        if fresh[0]:  # its own action keeps what it knew current; after someone else's, it must look
            view.seen = self.control.generation
        # (review R200v M2) the o refs are points of the screen the last reading saw: any action of this client's ends
        # them, `look: false` included (a feed's swipe changes the video; a later tap on o2 would hit the old place)
        view.ocr = {}
        if isinstance(result, Unchanged):  # nothing was done: nothing to settle or diff
            return _answer([str(result)], notes)
        reads_only = name == "alert" and args.get("action") == "text"  # it changes nothing on the phone
        looks = settle is not None and args.get("look", True) and not reads_only
        view.moved = view.moved or not looks
        if not reads_only:
            self.unsettled = not looks
        said_done = self._redact(str(result))  # (before element_name and the caption cut it, §18.6)
        self._announce("agent", name, label=element_name(said_done), caption=step_caption(name, said_done, args),
                       point=self._point(self.session.last_point))
        if not looks and name == "back" and settle is not None:
            # back exists to change the screen: even with `look: false` it checks that something did (the stream's
            # frames settle, as for a look; no tree read), and says no_action when nothing did (V1.6 finding 3)
            moved = await self._settle(self._mark()[0], cap=settle, since=box["mark"][1],
                                       early=box["mark"][0] if settle == CAP else None)
            self.unsettled = False
            if not moved.changed or await self._back_unchanged(view):
                raise Refused("not_actionable", "no_action: nothing changed after back" + self._still_open())
        if not looks:
            view.unlooked += 0 if reads_only else 1
            if not reads_only:
                view.place = None  # the screen moved and nothing was read: where the app is now isn't known
            taken = await self._taken_away(view, box.get("app"))
            self._recheck_later(client_id, box.get("app"), taken)
            quick = _answer([str(result)], [*notes, taken] if taken else notes)
            if READ_RUN_STEPS and box.get("mapping") and BATCH.get() is not None and not taken and not reads_only:
                snap = await self._read(lambda: self.session.snapshot())  # one read for the place this step arrived at
                if snap.app is not None and view.app is not None and snap.app[1] == view.app[1]:
                    view.tree = snap.tree
                    quick = await self._learn(view, name, args, quick, box, began)
            return quick
        mark = box["mark"]  # the stream as the gesture went out; the settle only trusts frames after the return
        # frames during the call count for actions with the normal cap; a launch may show a still splash first
        if isinstance(result, Toggled):
            landed = await self._toggle_landed(view, result)
        elif (isinstance(result, Typed) and args.get("replace", True) and not args.get("submit")
              and (result.typed if result.typed is not None else args["text"]) != ""):
            # (an empty text only cleared the field: an empty field may read as its placeholder, never retyped)
            # the text the keys made (an account's value, not the ${account.…} the agent wrote), as the tree reads it
            typed = result.typed if result.typed is not None else args["text"]
            landed = await self._typed_landed(view, result, typed)
            if landed is None and not result.field.secure and await self._typed_wrong(result, typed) and \
                    await self.control.agent(lambda: guarded_retype(typed, result.field)):
                # the field doesn't hold what was typed: keys went missing (iOS swapping in another keyboard as
                # the field took focus, seen on the phone 2026-11-11: "abc" landed as "a", and the retry with the
                # caret at the start made it "abca"). Cleared and typed once more, and the answer says so.
                notes = [*notes, NOTE_RETYPED]
                landed = await self._typed_landed(view, result, typed)
        else:
            landed = None
        if landed is not None:
            done = Settled(landed[1], True, False)
        else:
            # (a launch settles with the launched app's cap: one known never to stand still waits SHORT_SETTLE)
            done = await self._settle(self._mark()[0], cap=settle, since=mark[1],
                                      early=mark[0] if settle == CAP else None,
                                      **({"bundle": self._opened} if name == "open_app" else {}))
        trace.settled()
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
            if _health_sheet(view.items):  # iOS's own Health access sheet takes touches and changes nothing
                notes = [*notes, NOTE_HEALTH]
            elif box.get("hint"):
                notes = [*notes, NOTE_DELIVERED.format(touch=touch_name(name), seconds=time.monotonic() - box["began"], hint=box["hint"])]
            if name == "open_url" and (said := _url_not_shown(args, box, view.items, view.app, unchanged=True)):
                notes = [*notes, said]
            if name == "open_url" and isinstance(view.app, tuple):
                view.aimed_app = view.app  # where the link went: the app its answer shows (last Opus review, L)
            self._auto_stayed(view, name)
            return await self._add_shot(_answer([str(result), changes.SAME_SCREEN, changes.NO_CHANGE,
                                                 settled_line(done.seconds)], notes), view,
                                        self._cut_shot(args, shot, self._tier_now()), at_read=False)
        to_target = name == "scroll" and args.get("to") == "target"
        # (a scroll to a target read the list after each of its drags, with the refs carried: its last read is no
        # move for the refs, though the content did move since the agent's last answer)
        moves = None if name in MAY_MOVE_CONTENT else name in MOVES_CONTENT and not to_target
        answer = await self._after(view, str(result), notes, full[0], moves, done.seconds,
                                   drifted=to_target or bool(box.get("scrolled")),
                                   snap=landed[0] if landed is not None else None,
                                   hint="" if done.capped else box.get("hint", ""),  # (a screen still moving: no "nothing changed")
                                   watched=time.monotonic() - box["began"], touch=touch_name(name))
        if closed_first == NOTE_KEYBOARD and view.read_keyboard:  # the tap focused another input: the keyboard is up again
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
        if name == "open_url" and (said := _url_not_shown(args, box, view.items, view.app, unchanged=False)):
            answer = {**answer, "text": f"{answer['text']}\nNote: {said}"}
        if name == "open_url" and isinstance(view.app, tuple):
            view.aimed_app = view.app  # where the link went: the app its answer shows (last Opus review, L)
        taken = await self._taken_away(view, box.get("app"), read=self.session.app)
        self._recheck_later(client_id, box.get("app"), taken)
        if taken:  # the screen the agent is shown is the app's, back in front, not the other app's
            return await self._look(view, shot, [str(result)], [*notes, taken])
        answer = await self._learn(view, name, args, answer, box, began)
        return await self._add_shot(answer, view, self._cut_shot(args, shot, getattr(view.tree, "tier", "full")))

    @staticmethod
    def _cut_shot(args: dict, shot: "tuple[int, bool] | None", tier: str) -> "tuple[int, bool] | None":
        """(2.0) An action's picture: on a screen whose tree was cut short (`tier` shallow or root) the answer can't
        list what changed, so the picture after it comes unless the call said `screenshot: false`."""
        if shot is None and tier in ("shallow", "root") and args.get("screenshot") is not False:
            return shotlib.DEFAULT_WIDTH, False
        return shot

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
        self._left_front = bundle  # (if it was a crash that took the app, the answer's crash note is looked for)
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
        why = "the field never showed exactly the typed text"
        while loop.time() - began < TOGGLE_LAND_WAIT:
            snap = await self._read(lambda: self.session.snapshot())
            if snap.tree.alert is not None:
                why = "an alert showed"
                break
            same = [e for e in snap.tree.items if e.role == old.role and (
                e.identifier == old.identifier if old.identifier else e.label == old.label)]
            if len(same) == 1 and not same[0].secure and same[0].value == text:
                if len(snap.tree.items) != len(view.items):
                    why = f"the list changed size ({len(view.items)} -> {len(snap.tree.items)})"
                    break
                return snap, loop.time() - began
            why = (f"{len(same)} matching fields" if len(same) != 1 else "the field is secure" if same[0].secure
                   else f"the field reads {self._redact(same[0].value)!r}, not the typed text")
            await asyncio.sleep(TOGGLE_POLL)
        if session_module.TRACE:  # (a typing that doesn't land here waits for the screen to settle: 1.6 USB took 2.1 s)
            self.log(f"trace type_text not landed after {loop.time() - began:.2f} s: {why}")
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

    # ---- reflexes (AGENT-TOOLS §18.7) ----
    async def _fire_reflexes(self, client_id: str, scope: "Scope | None") -> list[str]:
        """One declared reflex of the app in front whose moment has come, made now: what it waits for is on screen, it is at
        its place, it has fired fewer than `max` times in this call, no permission prompt is up, and the policy has no word on
        what it would touch (checked again here: the file may have changed, and the target may carry another word on
        another screen; it never asks a person). Returns its `Reflex:` line, or nothing. Off in a journey in a user's eyes
        unless its start allowed `reflexes`."""
        jr = self._journeys.get(client_id)
        if ui.USERS_EYES.get() and (jr is None or "reflexes" not in jr.allow):
            return []
        fired = _FIRED.get()
        if fired is None:
            return []
        try:
            given = await self._given(scope) if scope is not None else None
            view = self.control.client(client_id)
            await self._read(lambda: self._silent_read(view))
            bundle = self._bundle()
            if bundle is None or (given is not None and bundle not in given):
                return []
            root = self._maps_root()
            data = self.maps.data(root, bundle)
            if not data["reflexes"]:
                return []
            tree = self.session.last_tree
            place = None
            if tree is not None and places.has_structure(tree):
                place = places.locate(data, places.read_place(tree, self._language.get(bundle)))[0]
            for r in reflex.due(data["reflexes"], view.items, place, fired):
                do = r["do"]
                element = reflex.touched(r, view.items)
                if do.get("tool") != "back" and element is None:
                    continue  # what it would touch isn't on this screen
                call = {} if do["tool"] == "back" else {"target": dict(do["target"])}
                if call and ui.USERS_EYES.get() and element is not None and element.text:
                    call = {"target": {"text": element.text}}  # a user can't see ids: by the words on the glass
                nested = _NESTED.set(True)  # the tool's own step: not recorded, not learned as a road
                apart = _RESOLVED.set(None)  # (its touch is not the one the action it fires inside was aimed at)
                try:
                    await self.run_tool(client_id, do["tool"], {**call, "look": False}, scope)
                except Refused:
                    continue
                finally:
                    _RESOLVED.reset(apart)
                    _NESTED.reset(nested)
                fired[r["name"]] = fired.get(r["name"], 0) + 1
                return [reflex.said(r, reflex.seen(r), reflex.aim(r))]
        except Refused:
            return []
        return []

    async def _wait_loading(self, view, target: ui.Target) -> None:
        """A target that isn't on screen while a loading indicator is: wait for it, up to LOADING_CAP, instead of answering
        `not_found` (§18.7). Ends when the target is there, the indicator is gone, or the cap is reached."""
        clock = self.control.clock
        began = clock()
        while clock() - began < LOADING_CAP:
            await self.control.sleep(LOADING_POLL)
            await self._silent_read(view)
            if not self._missing(view.items, target) or not reflex.loading(view.items):
                break
        if (earned := _REFLEXED.get()) is not None:
            earned.append(reflex.waited(clock() - began))

    # ---- the map (AGENT-TOOLS §18) ----
    def _maps_root(self) -> str | None:
        """The folder this call's maps are kept in: the call's own `--maps`, else serve's, else None (the tool's home)."""
        sent = _MAPS.get()
        if sent:
            return sent
        return getattr(getattr(self, "ceiling", None), "maps", None)

    def _saved_route_name(self, path: object, data: dict) -> str | None:
        """The route a replayed file is (§19.1, review 1.7 A-M1): its name when the file is the map's own
        `routes/<name>.json` for its app, in the maps folder in force; else None (a script that only names itself so)."""
        name, app = data.get("name"), data.get("app")
        if data.get("version") != 2 or not isinstance(name, str) or not routes.NAME.match(name) or not isinstance(app, str):
            return None
        try:
            own = self.maps.file(self._maps_root(), app).routes_dir / f"{name}.json"
            return name if os.path.samefile(str(path), own) else None
        except (OSError, ValueError, TypeError):
            return None

    def _refuse_held_route(self, name: str) -> None:
        if (why := self._route_held(name)) is not None:
            raise Refused("not_allowed", f"route {name} can't be recorded: {why}, and a recording would replace it. Record "
                                         "under another name; only a person changes that grant or schedule.")

    def _route_held(self, name: str) -> str | None:
        """Why `record` may not save a route called `name` (review 1.7 A-M1): an active grant of the policy in force, or a
        schedule, names it, and a recording would put the model's own steps under the person's OK. None when it may. Every
        grants file the daemon knows counts (_route_books), not only the call's: a stricter policy a call sends has no
        file, and turning its own grants off never makes a route the daemon's grants name free to replace (H2)."""
        for book in self._route_books():
            try:
                granted = book.naming_route(name)
            except Exception:  # any doubt keeps the route as it is
                return "the grants can't be read now"
            if granted is not None:
                return f"grant {json.dumps(granted)} names it"
        try:
            for schedule in schedulelib.names(self.schedules_folder):
                try:
                    if schedulelib.load(schedule, self.schedules_folder).route == name:
                        return f"schedule {json.dumps(schedule)} runs it"
                except schedulelib.ScheduleError:
                    continue
        except OSError:
            return "the schedules can't be read now"
        return None

    async def _build_of(self, bundle: str) -> str | None:
        """The app's version and native build number, `1.4.0 (212)`, from the installed apps: a road driven on another build
        is unproven (§18.2). None for an app that isn't in that list (a built-in one) or when the phone can't say. Kept
        for BUILD_TTL seconds; a reinstall is a new build."""
        held = self._builds.get(bundle)
        now = time.monotonic()
        if held is not None and now - held[0] < BUILD_TTL:
            return held[1]
        try:
            installed = await asyncio.wait_for(self.session.apps(), 5)
        except Exception:
            return held[1] if held is not None else None
        for entry in installed:
            version, build = str(entry.get("version") or ""), str(entry.get("build") or "")
            text = f"{version} ({build})" if version and build else version or build or None
            self._builds[entry["bundle_id"]] = (now, text)
        self._builds.setdefault(bundle, (now, None))
        return self._builds[bundle][1]

    def _may_map(self, bundle: str) -> bool:
        """Is this app one the call may know about? (Personal mode: only the apps the agent was given.)"""
        given = _GIVEN.get()
        return given is None or bundle in given

    _thin_place: "tuple[tuple[str, str], int] | None" = None  # (the place, the screen generation) a thin call's read left

    def _keep_thin_place(self, client_id: str) -> None:
        """After a thin call: the place its read left, with the screen generation then, for the next thin call (W177)."""
        try:
            view = self.control.client(client_id)
            if view.place is not None and view.place_seen == self.control.generation and not view.dropped:
                self._thin_place = (view.place, view.place_seen)
        except Exception:  # noqa: BLE001 - a place not kept is a place not known, never a failed call
            pass

    def _place_before(self, view) -> tuple[str, str] | None:
        """The place the app is at as the agent's last read left it, for a move about to be made: only while nothing has
        changed the screen since (a person, a look: false action, a dropped line), and the app is still the one."""
        place = view.place
        if place is None and view.place_seen == -1 and not view.dropped and _VIA.get() == "call":
            # (W177) a thin call is a process of its own, often with a client of its own (its parent process): its view has
            # never read. The place the thin calls' last read left, while nothing has changed the screen since, is its place
            shared = self._thin_place
            if shared is not None and shared[1] == self.control.generation:
                place = shared[0]
            return place if place is not None and place[0] == (getattr(self.session, "app", None) or (None, None))[1] else None
        if place is None or view.place_seen != self.control.generation or view.dropped:
            return None
        return place if place[0] == (getattr(self.session, "app", None) or (None, None))[1] else None

    def _aim(self, args: dict, items: list[ui.Element]) -> tuple[ui.Target | None, ui.Element | None, list[ui.Element]] | None:
        """What a move aims at, as the list shows it before the touch: (the target, the element it names or lies on, the
        list). A road keeps it by id, label or text (never a ref), or as a point where no element is."""
        target = args.get("target")
        if not isinstance(target, ui.Target):
            return None, None, items
        try:
            hit = ui.resolve(items, target)
        except Refused:
            return target, None, items  # the call itself says why
        if isinstance(hit, tuple):
            hit = self._element_at(hit, items)  # a point on an element is that element's road
        return target, hit, list(items)

    async def _learn(self, view, name: str, args: dict, answer: dict, box: dict, began: float) -> dict:
        """The map's part of an action's answer (§18.1, §18.2): where the app is now (a new place is saved), the road
        the move made, and the `Place:` line. Nothing in a replay (it reads the map and never writes it) or in a journey
        in a user's eyes."""
        tree, bundle = view.tree, (view.app or (None, None))[1]
        view.road_known = None
        if SCRIPT.get() is not None or ui.USERS_EYES.get() or tree is None or bundle is None or not self._may_map(bundle):
            return answer
        root = self._maps_root()
        before = box.get("from")
        previous = before[1] if before is not None and before[0] == bundle else None
        # which places' titles the aimed element carries, decided BEFORE this read updates the stored title hashes: a
        # screen that arrives with another title (Chat with Sara, then Omar) would hide a Back button's label (Codex,
        # PR #10 cycle 2)
        box["held"] = self._held_titles(box, root, bundle, previous)
        seen = self.maps.observe(root, bundle, tree, arrived=None, lang=self._language.get(bundle), previous=previous)
        if seen is None:
            view.place = None
            if (_cut_short(tree) and isinstance(view.auto, autoroute.Chain) and view.auto.app == bundle
                    and name not in autoroute.NEUTRAL):
                # (R200v M1) a move whose arrival couldn't be read: where it went isn't known, so no way through it is
                # saved as a route (the next place read would otherwise follow on as if this step had led there)
                view.auto.taint()
            return answer
        view.place, view.place_seen = (bundle, seen.name), self.control.generation
        typed_here = view.typed_at is not None and view.typed_at == before
        if view.typed_at is not None and view.typed_at != view.place:
            view.typed_at = None  # the place changed: what was typed is behind
        if name == "type_text":
            view.typed_at = view.place
        notes: list[str] = []
        if name == "open_url":
            if (said := self.maps.link(root, bundle, seen.name, str(args.get("url")))) is not None:
                notes.append(said)
        move = None
        if box.get("mapping") and previous is not None and previous != seen.name and not typed_here:
            move = self._move_of(name, args, box, root, bundle, (previous, seen.name))
            if move is not None and _GRANTED.get():
                notes.append(NOTE_NOT_ROAD)
            elif move is not None:
                aim = box.get("aim")
                names = self._loaded().clean(roads.names_of(aim[1]) if aim and aim[1] is not None and move["tool"] != "back" else {})
                made = self.maps.record(root, bundle, previous, seen.name, move, names,
                                        round((time.monotonic() - began) * 1000), await self._build_of(bundle), box["held"])
                view.road_known = made is not None and not made.new
                notes.extend(f"road dropped: {a} > {b} (it failed twice)." for a, b in (made.dropped if made else []))
        if not _NESTED.get():
            await self._auto_follow(view, name, args, root, bundle, previous, seen.name, move, typed_here, box.get("aim"))
        if (problem := self.maps.problem_once(root, bundle)) is not None:
            notes.append(problem)
        text = answer["text"]
        lines = text.split("\n")
        if seen.had_map and len(lines) > 1 and lines[1].startswith("Screen "):  # a new screen, not a change (§18.1)
            lines.insert(1, PLACE_NEW if seen.new else f"Place: {seen.name}.")
            text = "\n".join(lines)
        return {**answer, "text": text + "".join(f"\nNote: {n}" for n in notes)} if (notes or text != answer["text"]) else answer

    # ---- 1.9.0, "the Token Update" (AGENT-TOOLS 1.17 §30) ----
    CHAIN_ENDS = ("replay", "record", "explore", "journey", "bug_report", "close_app", "notifications", "unlock")

    async def _auto_follow(self, view, name: str, args: dict, root: str | None, bundle: str, previous: str | None,
                     here: str, move: dict | None, typed_here: bool, aim: tuple | None = None) -> None:
        """The way from the app's start this session is on (autoroute): a navigation move adds a step, anything else breaks
        it from here, and a clean way to a new place is saved as the place's route. Never with --no-hints (1.8.5 saved no
        route by itself). Never fails a call."""
        chain = view.auto
        if not self._hints_wanted():
            view.auto = None
            return
        if chain is None and bundle not in view.auto_begun and name not in autoroute.NEUTRAL:
            # (1.9.0 round 2) the way begins at the session's first screen of the app, whatever showed it (an open_url, a
            # first action), not only after open_app
            chain = view.auto = autoroute.Chain(bundle)
        if not isinstance(chain, autoroute.Chain) or chain.app != bundle or name in autoroute.NEUTRAL:
            return
        view.auto_begun.add(bundle)
        try:
            if chain.here is None:
                chain.begin(here)
                if name != "type_text":  # (review Opus 3 M4) a read after typing is no "before the typing"
                    chain.saw(self._tree_ids(view))
                return
            if name == "run":
                # (review 1.9.0 H1) a run's own steps never reach the chain one by one: any step that isn't navigation (typing
                # above all) breaks the way here, and a run that moved the app leaves the way unknown
                if not autoroute.only_navigation(args.get("steps")) and here == previous == chain.here:
                    chain.taint()
                elif here != chain.here or previous != chain.here:
                    view.auto = None
                return
            if previous is None or previous != chain.here:
                view.auto = None  # something moved the app that the chain didn't see (a look: false, a person)
                return
            if name == "scroll":
                if here == previous:
                    chain.scrolled(autoroute.scroll_step(args))
                    chain.saw(self._tree_ids(view))
                else:
                    chain.taint()
                    chain.moved(here, None)
                return
            if name == "type_text" and here == previous:
                return  # (1.9.0 round 2) typing that stayed is judged at the call's end (_tokens_after): a search or not
            if (name == "tap" and here != previous and not _GRANTED.get() and chain.search is not None
                    and chain.search[0] == len(chain.places) - 1 and not self._submits(aim, bundle)):
                element = aim[1] if aim else None
                way = chain.after_search(here, getattr(element, "identifier", None) if self._kept_id(element) else None)
                if way:
                    chain.saw(self._tree_ids(view))
                    await self._auto_save(root, bundle, chain.start or previous, here, way, derived=True)
                    return
            nav = name in autoroute.NAV_TOOLS and not typed_here and not _GRANTED.get()
            if nav and name == "tap" and here != previous and self._submits(aim, bundle):
                nav = False  # (review 1.9.0 H1) a Save, Submit or confirm tap that leaves a form is never navigation
            if not nav:
                chain.taint()
            way = chain.moved(here, autoroute.step_of(move) if nav else None)
            chain.saw(self._tree_ids(view))
            if way:
                await self._auto_save(root, bundle, chain.start or previous, here, way, derived=chain.derived)
        except Exception as e:  # noqa: BLE001 - a route not saved is a route not known, never a failed call
            self.log(f"auto route not kept: {type(e).__name__}: {self._redact(str(e))}")
            view.auto = None

    @staticmethod
    def _tree_ids(view) -> set:
        """(1.9.0 round 2) The ids a developer wrote in the screen's tree as last read, on and off screen."""
        tree = view.tree
        if tree is None:
            return set()
        return {e.identifier for e in [*tree.items, *getattr(tree, "offscreen", [])]
                if isinstance(e.identifier, str) and e.identifier and ui.structural_id(e.identifier)
                and not ui.private(e.identifier)}

    @staticmethod
    def _kept_id(element) -> bool:
        ident = getattr(element, "identifier", None)
        return isinstance(ident, str) and bool(ident) and ui.structural_id(ident) and not ui.private(ident)

    @staticmethod
    def _is_search(aim: tuple | None) -> bool:
        """(1.9.0 round 2) Was the text typed into a search field: iOS's search field, or an input whose id, label or
        placeholder says search?"""
        element = aim[1] if aim else None
        if element is None or getattr(element, "role", None) != "input":
            return False
        if getattr(element, "type", None) == "SearchField":
            return True
        return Daemon._says_search(element.identifier, element.label, getattr(element, "placeholder", None))

    @staticmethod
    def _says_search(*texts) -> bool:
        """(review Opus 3 L3) "search" as a word or a word's part of an id (`search`, `explore-search`, `searchField`),
        never inside another word (`research-notes`, "Research topic")."""
        for text in texts:
            if isinstance(text, str) and text:
                words = explorefloor.id_words(text).lower().split()
                if "search" in words:
                    return True
        return False

    def _typed_search(self, view) -> bool:
        """The input this session last typed into (view.typed_field) is a search field, as _is_search judges it."""
        typed = view.typed_field
        if not typed:
            return False
        ident, label = typed[0], typed[1]
        shown = next((e for e in view.items if getattr(e, "role", None) == "input"
                      and (e.identifier, e.label) == (ident, label)), None)
        if shown is not None:
            return self._is_search((None, shown, []))
        return self._says_search(ident, label)

    def _auto_stayed(self, view, name: str) -> None:
        """(review 1.9.0 L1) An action answered "No visible change" never reaches _learn: a selection the picture barely
        shows (a radio dot) still breaks the way, as any action that stays on its screen does."""
        chain = view.auto
        if (isinstance(chain, autoroute.Chain) and not _NESTED.get() and SCRIPT.get() is None and name != "scroll"
                and name not in autoroute.NEUTRAL and chain.here is not None and name != "type_text"):
            chain.taint()  # (typing is judged at the call's end, _tokens_after: a search may be kept by its result)

    def _submits(self, aim: tuple | None, bundle: str) -> bool:
        """Does a tap leave a form by a control that may submit it (review 1.9.0 H1)? The floor's own words (Delete, Send,
        Buy...), its submit words on a screen with an input (Save, Submit, Continue, Confirm, Sign in...), or any tap that
        leaves a form (2 or more inputs and a button)."""
        element, items = (aim[1], aim[2]) if aim else (None, [])
        if element is None:
            return False
        floor = explorefloor.Floor(safety.DEFAULT, bundle)
        return (floor.touch(element, items) is not None or floor.may_submit(element, items) is not None
                or self._is_form(items))

    def _held_routes(self, root: str | None, bundle: str) -> tuple[list[dict], set[str]]:
        """The app's saved routes that read (each with its `name`), and every route name its folder holds."""
        return self._held_routes_of(self.maps.file(root, bundle), self.maps.data(root, bundle))

    def _route_cached(self, file, data: dict, name: str) -> dict | None:
        """A route file, read again only when its modification time changed (review 1.9.0 L3)."""
        rel = data.get("routes", {}).get(name)
        try:
            path = (file.folder / rel).resolve() if isinstance(rel, str) else None
            stamp = path.stat().st_mtime_ns if path is not None else None
        except OSError:
            stamp = None
        cache = self.__dict__.setdefault("_route_reads", {})
        key = (str(file.folder), name, mapfile.HIDE_AUTO.get())
        held = cache.get(key)
        if stamp is not None and held is not None and held[0] == stamp:
            return held[1]
        route = file.read_route(data, name)
        if stamp is not None:
            cache[key] = (stamp, route)
        return route

    def _auto_lock(self, root: str | None, bundle: str) -> asyncio.Lock:
        """One lock per map: a route's choice, its write and its publication are one step (CodeRabbit, PR #22)."""
        locks = self.__dict__.setdefault("_auto_locks", {})
        return locks.setdefault(self.maps._key(root, bundle), asyncio.Lock())

    async def _auto_save(self, root: str | None, bundle: str, start: str, end: str, way: list[dict],
                         derived: bool = False) -> str | None:
        """Save `way` as the route to `end` when it is the one to keep (autoroute.decide); its name, or None. The files are
        read and written on a worker thread, never on the phone's event loop (CodeRabbit, PR #22): the map's lock can be
        held by another writer for seconds. The map in memory takes the result afterwards."""
        file = self.maps.file(root, bundle)
        if file.problem is not None:
            return None
        async with self._auto_lock(root, bundle):
            snapshot = copy.deepcopy(self.maps.data(root, bundle))
            try:
                name, dropped = await asyncio.to_thread(self._auto_save_io, file, snapshot, bundle, start, end, way, derived)
            except Exception as e:  # noqa: BLE001 - an optional save that failed never fails the action
                self.log(f"auto route not kept: {type(e).__name__}: {self._redact(str(e))}")
                return None
            held = self.maps.data(root, bundle)["routes"]
            if name is not None:
                held[name] = f"routes/{name}.json"
                self.maps._forgotten_routes.get(self.maps._key(root, bundle), set()).discard(name)  # (saved again: kept)
                self.maps.changed(root, bundle)
            for gone in dropped:  # (Opus 2 M1) a tombstone, so no save under way brings its entry back
                self.maps.forget_route(root, bundle, gone)
            return name

    def _auto_save_io(self, file, data: dict, bundle: str, start: str, end: str,
                      way: list[dict], derived: bool = False) -> tuple[str | None, list[str]]:
        """The worker's part of _auto_save: (the name saved, the routes evicted to keep the cap)."""
        found, taken = self._held_routes_of(file, data)

        def count(v: object) -> int:
            return v if isinstance(v, int) and not isinstance(v, bool) else 0
        held = [autoroute.Held(r["name"], r.get("start"), r.get("end"),
                               len(r["steps"]) if isinstance(r.get("steps"), list) else 0, r.get("auto") is True,
                               count(r.get("failed")), bool(r.get("variables")),
                               autoroute.target_key(r["steps"]) if isinstance(r.get("steps"), list) else None)
                for r in found]
        name = autoroute.decide(held, start, end, way, autoroute.base_name(data, end, way[-1]), taken)
        if name is None or not routes.NAME.match(name) or self._route_held(name) is not None:
            return None, []
        before = next((r for r in found if r["name"] == name), None)
        replaces = file.route_stamp(data, name) if before is not None else None
        others = [r for r in found if r.get("auto") is True and r["name"] != name]
        evict = autoroute.over_cap(others, autoroute.MAX_AUTO - 1)
        if evict:
            # (CodeRabbit, PR #22) a route a grant or a schedule names is never evicted; when the cap can't be kept without
            # one, the new route waits
            free = [r for r in others if self._route_held(r["name"]) is None]
            evict = autoroute.over_cap(free, len(free) - len(evict)) if len(free) >= len(evict) else None
            if evict is None:
                return None, []
        route = {**routes.route(name, bundle, start, end, [], [routes.escaped(step) for step in way]),
                 "auto": True, "used": count(before.get("used")) if before else 0, "failed": 0, "reached": mapfile.now(),
                 **({"derived": True} if derived else {})}
        route = batch.saved_script(route, self._redact, f"route {name}")
        routes.check(route)
        if not file.write_auto_route(name, route, replaces):  # (checked again and written under the map's lock)
            return None, []
        self.log(f"auto route {name} kept ({len(way)} step{'' if len(way) == 1 else 's'}, {start} > {end})")
        return name, [gone for gone in evict if self._drop_route_io(file, gone)]

    def _held_routes_of(self, file, data: dict) -> tuple[list[dict], set[str]]:
        taken = set(data.get("routes", {}))
        try:
            if file.routes_dir.is_dir():
                taken |= {p.stem for p in file.routes_dir.glob("*.json")}
        except OSError:
            pass
        held = []
        for name in list(data.get("routes", {})):
            route = self._route_cached(file, data, name)
            if route is not None:
                held.append({**route, "name": name})
        return held, taken

    @staticmethod
    def _drop_route_io(file, name: str) -> bool:
        """Delete a route's file and its entry in the map's index, under the map's lock. True only when neither is left
        (CodeRabbit, PR #22): a file that couldn't be deleted keeps its entry."""
        try:
            return file.drop_auto_route(name)
        except (OSError, mapfile.MapError):
            return False

    async def _auto_counted(self, root: str | None, bundle: str, name: str, worked: bool,
                            stamp: int | None = None, end: str | None = None) -> str | None:
        """A drive of a route that saved itself ended: it worked (counted as used, its failures start over) or it failed
        (a second failure in a row drops it). The note when it was dropped. Only for the version that ran (CodeRabbit,
        PR #22): `stamp` is its file's modification time as the drive read it; a route replaced meanwhile isn't touched.
        The files are read and written on a worker thread."""
        file = self.maps.file(root, bundle)
        if file.problem is not None:
            return None
        async with self._auto_lock(root, bundle):
            snapshot = copy.deepcopy(self.maps.data(root, bundle))
            try:
                dropped, derived = await asyncio.to_thread(self._auto_counted_io, file, snapshot, name, worked, stamp, end)
            except Exception as e:  # noqa: BLE001
                self.log(f"auto route not counted: {type(e).__name__}")
                return None
            if not dropped:
                return None
            self.maps.forget_route(root, bundle, name)  # (Opus 2 M1) a tombstone, so no save under way brings it back
        called = f"route {name}" if self._plain_name(name) else "the route"  # (review 1.9.0 M1)
        if derived:  # (1.9.0 round 2) a route made from a search goes on its first failure
            return f"{called} dropped (its replay failed; it was made from a search); the next way the app is driven there " \
                   "is kept instead."
        return f"{called} dropped (its replay failed twice); the next way the app is driven there is kept instead."

    def _auto_counted_io(self, file, data: dict, name: str, worked: bool, stamp: int | None,
                         end: str | None = None) -> tuple[bool, bool]:
        """The worker's part of _auto_counted: whether the route was dropped (all under the map's lock: Opus 2 L1), and
        whether it was one made from a search (1.9.0 round 2: dropped on its first failure)."""
        before = file.read_route(data, name)
        derived = isinstance(before, dict) and before.get("derived") is True
        return file.auto_result(name, stamp, worked, autoroute.FAILS, mapfile.now(), end), derived

    def _hints_wanted(self) -> bool:
        """--no-hints is off for this call (the call's and serve's)."""
        return _HINTS.get() is not False and not getattr(self.ceiling, "no_hints", False)

    def _hints_on(self, client_id: str) -> bool:
        """Known: and Hint: lines (§30.2, §30.3): not with --no-hints (the call's or serve's), not for a scheduled run (no
        model reads it), not in a journey in a user's eyes."""
        if _HINTS.get() is False or getattr(self.ceiling, "no_hints", False):
            return False
        jr = self._journeys.get(client_id)  # (§30.2, §30.3) a journey in a user's eyes has no Known: and no hints
        if jr is not None and getattr(jr, "users_eyes", False):
            return False
        # the command line's own calls (`tapwright replay`, `tapwright explore`) are no agent's session
        return not scheduled(client_id) and not ui.USERS_EYES.get() and not client_id.startswith("cli-")

    def _known_names(self, bundle: str, here: str | None) -> list[str]:
        """§30.2: the saved routes that reach a place in one call from where the app is (their start is here, or a way over
        the map leads to it)."""
        if here is None:
            return []
        root = self._maps_root()
        data = self.maps.data(root, bundle)
        if not data.get("routes"):
            return []
        held, _ = self._held_routes(root, bundle)
        edges = self.maps.file(root, bundle).route_edges(data)
        held_word = self._held_word(bundle)
        needs = {r["name"] for r in held if not autoroute.known_ok(r, held_word)}

        def reachable(start: str) -> bool:
            # (review 1.9.0 M2; Opus 2 H1) with nothing the agent hasn't given: no link with a value to fill (`[id]`), no
            # route on the way that does more than navigate or needs a value
            if start == here:
                return True
            way = roads.plan(data, here, start, None, routes=edges)
            if way.kind == "link" and autoroute.needs_value(way.link):
                way = roads.plan(data, here, start, None, by="taps", routes=edges)
            if way.kind == "none":
                return False
            return not any((autoroute.needs_value(s["move"].get("link")) if isinstance(s, dict) else s.name in needs)
                           for s in way.steps)
        # (review 1.9.0 M1) a name the redaction would change is left out, never shown redacted
        return [n for n in autoroute.known(held, here, reachable, held_word) if self._plain_name(n)]

    def _known_entries(self, bundle: str, here: str | None) -> list[tuple[str, str | None]]:
        """(1.9.0 round 2) The Known names, each with its way in ids (autoroute.way_text): an agent sees what one call does
        (`dest_card_lisbon (scroll to dest-card-lisbon, tap)`). A way the redaction would change is left out (the name
        stays)."""
        names = self._known_names(bundle, here)
        if not names:
            return []
        held, _ = self._held_routes(self._maps_root(), bundle)
        steps = {r["name"]: r.get("steps") for r in held}
        out = []
        for name in names:
            way = autoroute.way_text(steps.get(name))
            out.append((name, way if way is not None and self._plain_name(way) else None))
        return out

    @staticmethod
    def _held_word(bundle: str):
        """A test of one target's words against the default skip floor (Delete, Send, Buy...): Known never offers a route
        that touches one (Opus 2 H2)."""
        floor = explorefloor.Floor(safety.DEFAULT, bundle)
        return lambda text: floor.named(text) is not None or floor.named(explorefloor.id_words(text)) is not None

    def _plain_name(self, name: str) -> bool:
        """Would the redaction (§20.3) leave this name as it is?"""
        try:
            return self.scrub_everywhere(name) == name and self._redact(name) == name and not self._loaded().holds(name)
        except Exception:  # noqa: BLE001 - in doubt, left out
            return False

    def _tokens_after(self, client_id: str, name: str, args: dict, text: str, code: str | None) -> str:
        """The end of an agent's own call (1.9.0, §30): the way from the app's start follows what the call did, and the
        answer gets the `Known:` line (the first that shows the app) and at most one `Hint:` line. Never fails a call."""
        if SCRIPT.get() is not None:
            return text  # a replay's own fresh start: its answers are the replay's, not the agent's
        try:
            view = self.control.client(client_id)
            view.calls += 1
            if name in self.CHAIN_ENDS or (name == "drive" and code is not None):
                view.auto = None
            elif name == "drive":
                self._chain_after_drive(view, args)
            elif (name not in autoroute.NEUTRAL and name not in (*autoroute.NAV_TOOLS, "scroll", "run", "open_app", "type_text")
                  and name in ACTIONS and isinstance(view.auto, autoroute.Chain)):
                # (CodeRabbit, PR #22 cycle 2) an action that isn't navigation breaks the way even when it skipped _learn
                # (look: false): a switch flipped unseen still changes where the next tap leads
                view.auto.taint()
            elif name == "type_text" or (name == "run" and not autoroute.only_navigation(args.get("steps"))):
                # (review 1.9.0 H1; CodeRabbit, PR #22) any typing breaks the way, wherever it was made, a run's too (with
                # look: false or trace: true it never reached _learn); a failed run leaves it unknown
                chain = view.auto
                if isinstance(chain, autoroute.Chain):
                    if (name == "type_text" and code is None and chain.broken_at is None and view.place is not None
                            and view.place[1] == chain.here and self._typed_search(view)):
                        chain.searched()  # (1.9.0 round 2) a search: a result tapped next may still be kept, by its id
                    else:
                        chain.taint()  # (more typing in the same search keeps the ids read before the first)
                if name == "run" and code is not None:
                    view.auto = None
            chain = view.auto
            if (isinstance(chain, autoroute.Chain) and chain.search is not None and name not in autoroute.NEUTRAL
                    and name != "scroll" and not (name == "type_text" and self._typed_search(view))):
                # (review Opus 3 L2) any other call after the search (a filter tap that stays, a toggle, typing elsewhere)
                # ends what the read before the typing can vouch for: no result is kept from it
                chain.search = None
            bundle = (view.app or (None, None))[1]
            if name == "logs" and bundle is not None:
                view.logs_mark[bundle] = self._log_mark(bundle)  # read: only an error after this asks for logs again
            if not self._hints_on(client_id):
                return text
            if name == "map" and code is None:
                return self._with_hints(view, name, args, self._map_known(view, args, text), code)
            return self._with_hints(view, name, args, self._with_known(view, self._with_keyboard(view, text)), code)
        except Exception as e:  # noqa: BLE001
            self.log(f"hints left out: {type(e).__name__}")
            return text

    def _chain_after_drive(self, view, args: dict) -> None:
        """After a drive: a route driven from the chain's start carries the chain on from its end; any other drive leaves
        the way unknown."""
        chain, route = view.auto, args.get("route")
        bundle = (view.app or (None, None))[1]
        view.auto = None
        if not (isinstance(chain, autoroute.Chain) and isinstance(route, str) and chain.here == chain.start
                and chain.broken_at is None and bundle == chain.app and view.place is not None):
            return
        root = self._maps_root()
        saved = self.maps.file(root, bundle).read_route(self.maps.data(root, bundle), route)
        if (saved is None or saved.get("auto") is not True or saved.get("start") != chain.start
                or saved.get("end") != view.place[1] or saved.get("variables") or not isinstance(saved.get("steps"), list)
                or not autoroute.only_navigation(saved["steps"], strict=True)):
            # (review 1.9.0 H2; CodeRabbit, PR #22) only a route the tool saved itself, whose taps were judged navigation
            # as they were made, carries the way on: a person's route may type, toggle or tap a submit, which no selector
            # shows; it stays for its own replay
            return
        chain.places.append(view.place[1])
        chain.steps.append(list(saved["steps"]))
        view.auto = chain

    KEYBOARD_NAMES = 3  # (1.9.0 round 2) at most this many covered elements named on the Keyboard: line

    def _with_keyboard(self, view, text: str) -> str:
        """(1.9.0 round 2) `Keyboard: shown` says what the keyboard covers and that a tap there closes it first, so an
        agent neither fights a keyboard that stays up (a multi-line field ignores a touch outside) nor taps around it:
        `Keyboard: shown (covers trip-save; a tap on it tries to close the keyboard first)`. Each by its id when it has one a
        developer wrote, else by its ref. Nothing covered: the line stays as it was."""
        if "Keyboard: shown" not in text:
            return text
        names = self._keyboard_covered(view.items)
        if not names:
            return text
        more = len(names) - self.KEYBOARD_NAMES
        listed = ", ".join(names[:self.KEYBOARD_NAMES]) + (f", +{more} more" if more > 0 else "")
        one = "it" if len(names) == 1 else "one"
        # (review Opus 3 L1) what a tap there does, as it is: a Done in the keyboard's accessory bar is pressed; else the
        # tool tries to close the keyboard, and a tap it can't clear is refused `covered`
        how = ("presses the keyboard's Done first" if self._done_on_keyboard(view) is not None
               else "tries to close the keyboard first")
        line = f"Keyboard: shown (covers {listed}; a tap on {one} {how})"
        return "\n".join(line if row == "Keyboard: shown" else row for row in text.split("\n"))

    @staticmethod
    def _keyboard_covered(items: list) -> list[str]:
        """The listed controls whose centre the keyboard lies over (never its own keys or accessory bar, nor an alert's)."""
        out = []
        for e in items:
            if (getattr(e, "role", None) not in ("button", "link", "cell", "tab", "switch", "input")
                    or getattr(e, "by_keyboard", False) or getattr(e, "in_alert", False)):
                continue
            cx, cy = e.center
            if not any(b.kind == "Keyboard" and b.rect[0] <= cx < b.rect[0] + b.rect[2]
                       and b.rect[1] <= cy < b.rect[1] + b.rect[3] for b in e.covered_by):
                continue
            ident = e.identifier
            named = isinstance(ident, str) and ui.structural_id(ident) and not ui.private(ident)
            out.append(ident if named else e.ref)
        return out

    def _map_known(self, view, args: dict, text: str) -> str:
        """(1.9.0 round 2) `map`'s listing ends with the Known: line too, where the app is now (an agent that calls map
        first learns what one call reaches without a look). Not for `map {"to"}` or an edit."""
        if any(args.get(k) is not None for k in ("to", "name", "link", "place", "reflex", "forget_reflex", "forget")):
            return text
        if view.place is None or not self._may_map(view.place[0]):
            return text
        line = tokentext.known_line(self._known_entries(view.place[0], view.place[1]))
        return f"{text}\n{line}" if line else text

    def _with_known(self, view, text: str) -> str:
        """§30.2: the first answer of the session that shows the app's screen gets `Known:` after the screen's header."""
        bundle = (view.app or (None, None))[1]
        if bundle is None or bundle in view.known_told or not self._may_map(bundle):
            return text
        lines = text.split("\n")
        at = next((i for i, line in enumerate(lines) if line.startswith("Screen ")), None)
        if at is None:
            return text
        here = view.place[1] if view.place is not None and view.place[0] == bundle else None
        if here is None or reflex.starting(view.tree, bundle) is not None:
            # (review 1.9.0 L6; the phone check) where the app is isn't known, or it is still starting ("Loading from
            # Metro…"): the line isn't spent
            return text
        view.known_told.add(bundle)
        line = tokentext.known_line(self._known_entries(bundle, here))
        if line is None:
            return text
        at += 1
        while at < len(lines) and lines[at].startswith(("App: ", "Place: ", "Keyboard: ")):
            at += 1
        lines.insert(at, line)
        return "\n".join(lines)

    def _with_hints(self, view, name: str, args: dict, text: str, code: str | None) -> str:
        """§30.3: at most one `Hint:` line, after the Note: lines, each kind at most once per session."""
        if name in ("tap", "back", "open_url"):
            view.tap_streak = view.tap_streak + 1 if (code is None and view.road_known) else 0
        elif name not in ("look", "wait", "wait_for", "logs"):
            view.tap_streak = 0
        view.road_known = None
        if code in tokentext.SAFETY_CODES:
            return text  # no hint on a safety refusal (needs_confirmation, not_allowed, a floor hold)
        applies = {
            # the app's own error or a crash since logs were last read; never a tool error (not_found and the like)
            "logs": lambda: CRASH_NOTE in text or self._app_errored(view),
            "known_way": lambda: (code is None and view.tap_streak >= tokentext.KNOWN_WAY_TAPS and view.place is not None
                                  and self._known_name(view) is not None),
            # a new screen (the full list) holding an input and a submit-worded button (1.9.0 round 2: §30.3's "2 or
            # more inputs" never fired on a trip form of one field and steppers)
            "form": lambda: (code is None and name not in ("logs", "map", "apps")
                             and any(line.startswith("Screen ") for line in text.split("\n")[:4])
                             and self._is_form_hint(view.items)),
            "screenshot": lambda: (code is None and args.get("screenshot") is True and name in ACTIONS and name != "look"
                                   and changes.NO_CHANGE not in text),
            "long_task": lambda: view.calls >= tokentext.LONG_TASK_CALLS,
        }
        chosen = next((kind for kind in tokentext.ORDER if kind not in view.hints_given and applies[kind]()), None)
        if chosen is None:
            return text
        view.hints_given.add(chosen)
        if chosen == "known_way":
            return f"{text}\n{tokentext.hint(chosen, name=self._known_name(view))}"
        return f"{text}\n{tokentext.hint(chosen)}"

    def _log_mark(self, bundle: str) -> int:
        ring = self._rings.get(bundle)
        return ring.added if ring is not None else 0

    def _app_errored(self, view) -> bool:
        """Has the app in front logged an error of its own since this session's mark (its first answer of the app, or
        its last `logs` read)?"""
        bundle = (view.app or (None, None))[1]
        if bundle is None:
            return False
        ring = self._rings.get(bundle)
        if bundle not in view.logs_mark:
            view.logs_mark[bundle] = ring.added if ring is not None else 0
            return False
        if ring is None:
            return False
        # (CodeRabbit, PR #22) each entry by its own sequence number: one not yet vouched never shifts an old error into
        # the window, and an error after the mark counts once it is shown
        first = ring.added - len(ring.entries)
        mark = view.logs_mark[bundle]
        return any(first + i >= mark and e.level == "error" and e.own for i, e in enumerate(ring.shown()))

    def _known_name(self, view) -> str | None:
        """The saved route that reaches the place the app is at (its replay is the known way's one call)."""
        bundle, place = view.place
        try:
            held, _ = self._held_routes(self._maps_root(), bundle)
        except Exception:  # noqa: BLE001
            return None
        held_word = self._held_word(bundle)
        found = [r for r in held if r.get("end") == place and autoroute.known_ok(r, held_word)
                 and self._plain_name(r["name"])]  # (CodeRabbit, PR #22; Opus 2 L3) as Known: would list it
        if len(found) > 1:
            # (1.9.0 round 2) routes are kept per target: the one whose last step is the move just made, else none (a
            # route via another card would reach another destination's screen)
            chain = view.auto
            last = chain.steps[-1] if isinstance(chain, autoroute.Chain) and chain.steps and chain.steps[-1] else None
            key = autoroute.target_key(last) if last else None
            found = [r for r in found if key is not None and isinstance(r.get("steps"), list)
                     and autoroute.target_key(r["steps"]) == key]
        return found[0]["name"] if len(found) == 1 else None

    FORM_WORDS = tuple(w for w in explorefloor.SUBMIT_WORDS if w != "Share")  # (1.9.0 round 2) a form's own save

    @classmethod
    def _is_form_hint(cls, items: list) -> bool:
        """(1.9.0 round 2, Tapwright's reading of §30.3) A form, for its hint: 1 or more inputs and a button worded as a
        save or submit (Save, Submit, Continue, Sign in, Create, Confirm...), or §30.3's 2 or more inputs and a button. The
        H1 rule (`_is_form`, a tap that leaves a form is no navigation) keeps its own definition."""
        if not any(getattr(e, "role", None) == "input" for e in items):
            return False
        if any(getattr(e, "role", None) == "input" and cls._is_search((None, e, [])) for e in items):
            return False  # (review Opus 3 L4) a screen with a search field is a list to search, not a form to fill
        if cls._is_form(items):
            return True  # (§30.3's own reading still holds: 2 or more inputs and a button)
        return any(getattr(e, "role", None) == "button" and getattr(e, "enabled", True) is not False
                   and any(explorefloor._first(cls.FORM_WORDS, explorefloor._plain(n)) is not None
                           for n in explorefloor.names_of(e)) for e in items)

    @staticmethod
    def _is_form(items: list) -> bool:
        """A form (§30.3): 2 or more inputs and a button."""
        fields = sum(1 for e in items if getattr(e, "role", None) == "input")
        return fields >= 2 and any(getattr(e, "role", None) == "button" for e in items)

    def _held_titles(self, box: dict, root: str | None, bundle: str, previous: str | None) -> frozenset[str]:
        """The places whose title (as the map held it before this read, or as the screen showed it before the touch) is
        the label, text or value of the element the move aimed at."""
        element = (box.get("aim") or (None, None, None))[1]
        if element is None:
            return frozenset()
        texts = (element.label, element.text, element.value)
        held = places.titled_places(self.maps.data(root, bundle), *texts)
        shown = box.get("title_before")
        if previous is not None and shown and any(t and " ".join(t.split()) == shown for t in texts):
            held |= {previous}
        return held

    def _move_of(self, name: str, args: dict, box: dict, root: str | None, bundle: str,
                 between: tuple[str, str]) -> dict | None:
        """The move an action made, as a road keeps it (roads.move_of); None when it isn't one that can be kept (an
        `open_url` whose link matches no pattern, §18.3). A native Back button is labelled with a screen's title, and a
        title is never saved in plain text: its road is `back`, with no name, and any other move aimed by a title is no
        road (Codex, PR #10 cycle 1)."""
        target, element, items = box.get("aim") or (None, None, [])
        if element is not None and any(self._loaded().holds(v) for v in (element.identifier, element.label, element.value)):
            return None  # a field that shows an account's value (or the placeholder an agent may have aimed by) is no name for a road
        data = self.maps.data(root, bundle)
        held = box.get("held") or frozenset()
        if (name == "tap" and element is not None and element.role != "cell"
                and ui.is_back(element, box.get("back"))
                and (element.name == "BackButton"
                     or places.is_title(data, between, element.label, element.text, element.value, held=held))):
            return {"tool": "back"}
        extra = dict(args)
        if name == "open_url":
            extra = {"link": links.canonical(data, str(args.get("url")))}
        move = roads.move_of(name, target, element, items, extra)
        if move is not None and self.maps.titled(data, *between, move, held):
            return None  # Maps.record would refuse it too; no road, and no note
        return move

    def _mapped_look(self, view, text: str) -> tuple[str, list[str]]:
        """A look's text with the `Place:` line in its header (§18.1, once the app has a map), and the notes the map has
        for it. The place is saved when it is new."""
        tree, bundle = view.tree, (view.app or (None, None))[1]
        if SCRIPT.get() is not None or ui.USERS_EYES.get() or tree is None or bundle is None or not self._may_map(bundle):
            view.place = None if tree is None or bundle is None else view.place
            return text, []
        root = self._maps_root()
        previous = view.place[1] if view.place is not None and view.place[0] == bundle else None
        seen = self.maps.observe(root, bundle, tree, arrived=None, lang=self._language.get(bundle), previous=previous)
        view.place, view.place_seen = ((bundle, seen.name) if seen is not None else None), self.control.generation
        if view.typed_at is not None and view.typed_at != view.place:
            view.typed_at = None
        chain = view.auto
        if (seen is not None and chain is None and bundle not in view.auto_begun and self._hints_wanted()
                and not _NESTED.get()):
            # (1.9.0 round 2) the session's first look at an app already in front begins its way, as open_app does
            chain = view.auto = autoroute.Chain(bundle)
        if seen is not None and isinstance(chain, autoroute.Chain) and chain.app == bundle and chain.here is None:
            chain.begin(seen.name)  # (1.9.0, §30) the first screen after open_app is where its way starts
            view.auto_begun.add(bundle)
        if seen is not None and isinstance(chain, autoroute.Chain) and chain.app == bundle and chain.here == seen.name:
            chain.saw(self._tree_ids(view))  # (1.9.0 round 2) what the place holds, for a search's result
        notes = [problem] if (problem := self.maps.problem_once(root, bundle)) is not None else []
        if seen is None or not seen.had_map:
            return text, notes
        lines = text.split("\n")
        at = 2 if len(lines) > 1 and lines[1].startswith("App: ") else 1  # a line of the header, after the app's
        lines.insert(at, PLACE_NEW if seen.new else f"Place: {seen.name}.")
        return "\n".join(lines), notes

    async def _map(self, view, args: dict, notes: list) -> dict:
        """`map` (§18.8): the app's places, roads, routes and reflexes, the way to a place, or an edit of the map. A read:
        it touches nothing on the phone."""
        op = maptool.kind(args)  # bad_params before the phone is asked
        snap = await self._read(lambda: self.session.snapshot())
        if snap.app is None:
            raise Refused("not_found", "no app is in front to map; open_app one first")
        shown, bundle = snap.app
        given = _GIVEN.get()
        if given is not None and bundle not in given:
            raise Refused("not_allowed", _outside(bundle, shown, ", ".join(given.values())))
        root, lang = self._maps_root(), self._language.get(bundle)
        seen = self.maps.observe(root, bundle, snap.tree, arrived=False, lang=lang)
        data, file = self.maps.data(root, bundle), self.maps.file(root, bundle)
        ctx = maptool.Context(bundle, self.session.app_names.get(bundle) or shown, await self._build_of(bundle),
                              seen.name if seen else None, self._loaded().names, file.route_info(data), file.route_edges(data),
                              _POLICY.get(), self._vault_names())
        if op == "list":  # (§30.4) under --no-hints a route that saved itself isn't listed or counted
            lines = maptool.listing({**data, "routes": {n: data["routes"][n] for n in file.visible(data)}}, ctx)
        elif op == "to":
            lines = [maptool.way(data, ctx, args["to"])]
        else:
            lines = [self.maps.edit(root, bundle, lambda d: maptool.edit(d, ctx, args))]
            seen = self.maps.observe(root, bundle, snap.tree, arrived=False, lang=lang)  # a rename changes its name
        view.place, view.place_seen = ((bundle, seen.name) if seen else None), self.control.generation
        if (problem := self.maps.problem_once(root, bundle)) is not None:
            notes = [*notes, problem]
        return _answer(lines, notes)

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

    @property
    def _overridden(self) -> set[str]:
        """The apps launched with a language, region or text size (§16.3) and not closed since: _launched_with's apps."""
        return set(self._launched_with)

    async def _open_app(self, view, a: dict) -> str:
        bundle_id, shown = await self._app(a["app"])
        extra = launchlib.arguments(a)  # §16.3: language, region, text size; only a fresh launch takes them
        before = self._launched_with.get(bundle_id)  # the app may still be running with what its last launch was given
        # an app this daemon launched with them still runs with them: WebDriverAgent would bring that process forward as
        # it is, so a plain open (or text_size: system) closes it and launches it again with the defaults (Codex, PR #6)
        resume = _RESUME.get() and not extra  # (a journey brings its app back from the background: the process stays as it is)
        back = bool(before) and not extra and not resume
        try:
            if extra or back:  # a running app is closed first, as close_app does (WebDriverAgent's terminate alone is slow)
                if (await self._end_app(bundle_id))[1]:
                    await self._tell_runner_closed(bundle_id)
                else:
                    try:
                        await self.session.terminate(bundle_id)
                    except ConnectionLost:
                        raise
                    except DeviceError:
                        pass  # not running
                await self.session.launch(bundle_id, extra or None)
            else:
                await self.session.launch(bundle_id)
        except ConnectionLost:
            raise
        except DeviceError as e:
            raise Refused("not_found", f"couldn't open {bundle_id}: {e}; call apps for the installed apps") from None
        conditions = launchlib.check(a)
        if conditions:
            self._launched_with[bundle_id] = conditions
        elif not resume:
            self._launched_with.pop(bundle_id, None)
        self._opened = bundle_id
        view.opened = True
        view.auto = autoroute.Chain(bundle_id)  # (1.9.0, §30) the way from the app's start begins at what it shows now
        view.auto_begun.add(bundle_id)
        # (1.7.8, X item 4) nothing aimed at or typed into before belongs to the app that is opened: typing without a
        # target goes only to it, and no field of the app before is typed into again
        # (its name alone: `shown` already ends with the bundle id, and the stale-app refusal adds it once more)
        view.aimed_app, view.typed_field = (_bare_name(shown, bundle_id), bundle_id), None
        self.session.typed_into = self.session.typed_place = None
        if a.get("language"):  # the language its titles are kept under (§18.9); a start in the phone's own language clears it
            self._language[bundle_id] = str(a["language"])
        elif not resume:  # (an app brought back as it is keeps the language it runs in)
            self._language.pop(bundle_id, None)
        self._crash_since = self._wall()  # a new start: an earlier crash is old news
        self.restart_logs(bundle_id)
        if back:  # the plain open after one with conditions: they are undone, which the answer says
            return f"Opened {shown},{launchlib.undone(before)}."
        return f"Opened {shown}{launchlib.said(a)}."

    async def _close_app(self, view, a: dict) -> str:
        """End the app through iOS's process control, which doesn't wait on the app, then tell WebDriverAgent so its
        own bookkeeping matches (the app already gone, it has nothing to wait for). WebDriverAgent's terminate alone,
        as before and with no cap, when there is no tunnel, iOS refuses or is slow, TAPWRIGHT_CLOSE=wda, or the kill
        found no process (the app may not have been running, or the kill missed it: its terminate is then quick or
        the right one). On a real app it waited a median of 46 s for the app to finish quitting."""
        bundle_id, shown = await self._app(a["app"], partial=False)
        began = time.monotonic()
        way, found = await self._end_app(bundle_id)
        if found:
            if not await self._tell_runner_closed(bundle_id):
                way += f"; WebDriverAgent's terminate took over {CLOSE_TERMINATE_CAP:g} s and was left to finish"
        else:
            try:
                await self.session.terminate(bundle_id)
            except ConnectionLost:
                raise
            except DeviceError as e:
                if shown == bundle_id:  # named by a bundle id that no installed app and no built-in app name has
                    raise Refused("not_found", f"couldn't close {bundle_id}: {e}; call apps for the installed "
                                               "apps") from None
                raise
        # closed (iOS ended it, or WebDriverAgent's terminate did): the process is gone, and its launch conditions with
        # it. A close that failed leaves the app running as it was launched, so they are kept (Codex, PR #8).
        self._launched_with.pop(bundle_id, None)
        self.log(f"closed {bundle_id} through {way} in {time.monotonic() - began:.1f} s")
        return f"Closed {shown}."

    async def _end_app(self, bundle_id: str) -> tuple[str, int]:
        """Ask iOS's process control to end the app: (the way it went, for the log; how many processes it ended).
        None ended, with no tunnel, TAPWRIGHT_CLOSE=wda, or iOS refusing or slow: WebDriverAgent's terminate is the
        caller's to make."""
        way, device = "WebDriverAgent alone", self.session.device
        if os.environ.get("TAPWRIGHT_CLOSE") == "wda":
            return way + " (TAPWRIGHT_CLOSE=wda)", 0
        if device.rsd is None:
            return way + " (no tunnel)", 0
        guard = getattr(self.session, "execution_guard", None)
        token = device_module.BEFORE_SEND.set(guard)
        try:
            if guard is not None:
                await guard()
            found = await asyncio.wait_for(device.kill_app(bundle_id), CLOSE_KILL_CAP)
        except Refused:
            raise
        except asyncio.TimeoutError:
            return way + f" (iOS process control took over {CLOSE_KILL_CAP:g} s)", 0
        except Exception as e:
            return way + f" (iOS process control failed: {e})", 0
        finally:
            device_module.BEFORE_SEND.reset(token)
        return ("iOS process control", found) if found else (way + " (iOS process control found no process)", 0)

    async def _tell_runner_closed(self, bundle_id: str) -> bool:
        """After a kill that ended the app: tell WebDriverAgent, for at most CLOSE_TERMINATE_CAP. False if it was
        still going by then. Its request is never cancelled (that tears down the connection and leaves the request
        in flight on the phone): it is left to end by its own timeout, and a failure is only logged. The app is
        closed either way."""
        task = asyncio.ensure_future(self.session.terminate(bundle_id))
        self._closing.add(task)

        def ended(done: asyncio.Future) -> None:
            self._closing.discard(done)
            if not done.cancelled() and done.exception() is not None:
                self.log(f"closed {bundle_id}; WebDriverAgent's terminate afterwards failed: {done.exception()}")
        task.add_done_callback(ended)
        finished, _ = await asyncio.wait({task}, timeout=CLOSE_TERMINATE_CAP)
        return bool(finished)

    async def _frozen_tool(self, client_id: str, name: str, args: dict, notes: list,
                           shot: "tuple[int, bool] | None") -> dict | None:
        """(2.0 seam, for the lead to wire once `tapwright/hands.py` is merged) A call in FROZEN_TOOLS while the app in
        front blocks even the runner's bounded routes (`self.stuck` is an AppNotAnswering, state `not-answering`): the
        answer, or None to refuse it `not_answering` as 1.10.0 does. Returns None today: nothing here may use the
        runner, which is what is stuck.

        Once hands exist, every one of these needs no read of the app and no runner:
        - `look`: the picture from the stream (MJPEG over usbmuxd, or the HEVC display stream), and its text by
          `ocr.read` on this computer (the runner's OCR route can't answer while it is stuck); the header says the app
          doesn't answer, as a cut-short look's `ui.ELEMENTS_NONE` line does, with o refs (`view.ocr`) from that text.
        - `tap` / `long_press` at {x, y} (an o ref is one: `_ocr_target` turned it into a point before this), `swipe`,
          `press_button`: through hands at the point, upright only. Before each: the phone not locked and its process
          list holding the stuck app alone (1.10.1's `_blind_tool` `checked()`: /wda/locked can't be asked, so the
          lock state must come from hands or lockdown), the pause and a person's input (`self.control`), personal mode's
          given apps (`_GIVEN`), Tapline told the touch is the agent's (`self._agent_touches.notify`). Never for a dry
          run, `explore` or a repair (`_TOUCH_FLOOR`, `_EXPLORING`): there are no elements to judge.
        - the answer: what was done, `ui.ELEMENTS_NONE`, and the picture after it unless `screenshot: false`.
        1.10.1's `_blind_tool` (`git show wip/1.10.1-speed:tapwright/daemon.py`) has the wording and the failure codes
        (a touch that went out and got no answer is `timeout`, never `internal`)."""
        return None

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
        generation = self.control.person_generation

        async def guard():
            if self.control.paused:
                raise Refused("paused", "the phone is paused; ask the user to resume it")
            if self.control.person_generation != generation or self.control.person_active():
                raise Refused("busy", "a person used the phone before delivery; look again before acting")
            if not self._same_stuck(stuck) or self.session.device is not device:
                raise Refused("reconnecting", "the phone connection changed; look before acting")

        async def close():
            token = device_module.BEFORE_SEND.set(guard)
            try:
                await guard()
                await device.kill_app(stuck.bundle_id)
            finally:
                device_module.BEFORE_SEND.reset(token)

        await self.control.agent(close)
        self.log(f"closed {stuck.bundle_id}, which wasn't answering")
        self.stuck = None
        self._wake.set()
        self._announce("agent", "close_app", label=stuck.name)
        return _answer([f"Closed {stuck.name} ({stuck.bundle_id}), which wasn't answering."], notes)

    def _same_stuck(self, stuck: AppNotAnswering) -> bool:
        """The same app still blocks the phone. Every reconnect attempt stores a new AppNotAnswering for it, so the
        objects differ while the app is the same (1.7.8, bug 4: close_app lost that race for 2 min 40 s)."""
        now = self.stuck
        return isinstance(now, AppNotAnswering) and now.bundle_id == stuck.bundle_id

    async def _home_stuck(self, stuck: AppNotAnswering, notes: list) -> dict:
        """press_button home while the app in front is frozen: WebDriverAgent's home-screen route needs no session
        (a session would wait on the frozen app); asked once, with a short cap."""
        if self.control.paused:
            raise Refused("paused", "paused by the user in the live view; ask them to resume")
        device = self.session.device
        if device.rsd is None:
            raise self._not_connected()
        generation = self.control.person_generation

        async def guard():
            if self.control.paused:
                raise Refused("paused", "the phone is paused; ask the user to resume it")
            if self.control.person_generation != generation or self.control.person_active():
                raise Refused("busy", "a person used the phone before delivery; look again before acting")
            if not self._same_stuck(stuck) or self.session.device is not device:
                raise Refused("reconnecting", "the phone connection changed; look before acting")

        async def home():
            # (Opus review 9) a home press on a locked phone would light the lock screen (and may wake Face ID); to
            # unlock, the agent calls `unlock` (1.7.8). Asked session-less (WebDriverAgent answers it without reading the frozen app); only "not locked" passes
            try:
                locked = (await device.http("GET", "/wda/locked", None, timeout=HOME_LOCK_CAP)).get("value")
            except (ConnectionLost, DeviceError):
                raise Refused("not_answering", f"{stuck}, and the phone didn't say whether it is locked; nothing was "
                                               "pressed. close_app can still close it; then open_app it again.") from None
            if locked is not False:
                raise Refused("locked", "the phone is locked (or didn't say it isn't); call unlock (it types the "
                                        "passcode saved for this phone), or ask the user to unlock it. Nothing was "
                                        "pressed.")
            token = device_module.BEFORE_SEND.set(guard)
            try:
                await guard()
                await device.http("POST", "/wda/homescreen", {}, timeout=HOME_STUCK_CAP)
            finally:
                device_module.BEFORE_SEND.reset(token)

        try:
            await self.control.agent(home)
        except ConnectionLost:
            # no answer within the cap: WebDriverAgent may still carry it out (the `timeout` contract)
            self.log(f"press_button home while {stuck.bundle_id} wasn't answering got no answer in {HOME_STUCK_CAP:g} s")
            raise Refused("not_answering", f"{stuck}, and the phone didn't answer the home press in time; the home "
                                           "screen may still come up: look before acting. close_app can still close "
                                           "it; then open_app it again.") from None
        except DeviceError:
            raise Refused("not_answering", f"{stuck}, and the home screen didn't come up. close_app can still "
                                           "close it; then open_app it again.") from None
        self.log(f"pressed home while {stuck.bundle_id} wasn't answering")
        self._wake.set()
        self._announce("agent", "press_button", label="home")
        return _answer([f"Pressed home while {stuck.name} ({stuck.bundle_id}) wasn't answering; look before acting."],
                       notes)

    async def _unlock_tool(self, client_id: str) -> dict:
        """`unlock` (1.7.8, the owner's decision of 2026-11-18): the agent asks the daemon to unlock a locked phone with
        the passcode saved in serve's own vault (`--accounts FILE`, `phone.passcode`), in every mode: personal,
        dedicated and `--phone` alike. The agent never sees or sends the passcode; every rule of unlock.py holds (only
        iOS's lock screen with its keypad proven, never the XCTest prompt, 2 failed attempts an hour and 3 a day, none
        after a refusal, the audit line with the name only). A call of its own only: as a step of `run`, `replay`, a route,
        `drive` or a journey it is refused, and a lock there still ends them (§19.5). Paused: `paused`; a person at
        the phone: `busy`. No passcode saved: `locked`, asking the user."""
        if not self._top_level() or client_id in self._journeys:
            raise Refused("bad_params", UNLOCK_TOP_ONLY)
        if self.control.paused:
            raise Refused("paused", "paused by the user in the live view; ask them to resume")
        if self.control.person_active():
            raise Refused("busy", "a person is using the phone; try again in a moment")
        if PASSCODE_WORDS in (self.detail or "") or isinstance(self.stuck, PasscodeNeeded):
            raise Refused("locked", unlocklib.XCTEST)  # a person enters it there, never the tool
        if self.state not in ("live", "locked"):
            if isinstance(self.stuck, HELD_BEFORE_START):  # (W185) a person unlocks it; then the runner starts
                raise Refused("locked", f"{self.stuck}; ask the user")
            if isinstance(self.stuck, PhoneLocked):  # the connect found it locked: no session to type through yet
                raise Refused("locked", UNLOCK_NOT_YET)
            await self.wait_connected()
        if self.state == "live":
            # (2.0, phone test 2026-11-26) the heartbeat may not have seen a lock yet: `press_button lock` and `unlock`
            # 2.5 s later answered "The phone isn't locked." on a locked phone. Ask the phone now (about 10 ms).
            try:
                if await self.control.read(lambda: self._connected_only(self.session.locked)):
                    self._set_state("locked", "unlock the phone")
            except (Refused, DeviceError):
                pass  # (it can't say: the state as it was)
        if self.state != "locked":
            return _answer(["The phone isn't locked."], [])
        if self.passcode_for() is None:
            raise Refused("locked", UNLOCK_NO_PASSCODE)
        result = await self.unlock_phone()
        if result.code == "not_locked":
            return _answer(["The phone isn't locked."], [])
        if result.code in ("paused", "busy"):
            raise Refused(result.code, result.message)
        if result.code == "not_connected":  # (a reconnect between the check above and the unlock, L6)
            raise self._not_connected()
        if result.code == "no_passcode":  # (the vault changed between the two reads)
            raise Refused("locked", UNLOCK_NO_PASSCODE)
        if result.code != "unlocked":
            raise Refused("locked", result.message or unlocklib.FAILED)
        self._announce("agent", "unlock", caption="Unlocked the phone")
        said = "Unlocked the phone with the passcode saved in the vault."
        try:
            seen = await self._run_tool_core(client_id, "look", {"screenshot": False})
        except Exception as e:  # (the phone is unlocked and an attempt was used: never `internal` for the look, L7)
            self.log(f"unlock: the look after it failed ({type(e).__name__})")
            return _answer([said + " Look before acting."], [])
        return {**seen, "text": said + "\n" + seen["text"]}

    async def _home_over_center(self, notes: list) -> dict:
        """press_button home while the phone reads as locked. iOS reports Notification Center pulled down over an
        unlocked phone as locked (SpringBoard's lock state; phone check of 1.7.8), and every touch was refused, the home
        press that closes it too. Home is pressed only when a fresh tree proves that sheet (notifications.
        center_over_unlocked: its header and Clear button on the screen, on one row, not a notification's text, and an
        enabled flashlight). A lit lock screen showing the same sheet (a notification woke it, scrolled to its history)
        may pass that proof, and Home there may wake Face ID: under the owner's rule of 2026-11-18 a lock is no longer
        the stop for an agent that may unlock; Pause in the live view is. When the proof fails nothing is pressed, and the
        answer doesn't claim a lock it can't tell from that sheet (L9)."""
        from .notifications import center_over_unlocked
        if self.control.paused:
            raise Refused("paused", "paused by the user in the live view; ask them to resume")
        device = self.session.device
        if device.rsd is None:
            raise self._not_connected()
        generation = self.control.person_generation
        locked = Refused("locked", HOME_NOT_PROVEN)

        async def guard():
            # (L1) It can't see the owner pressing the side button in the milliseconds between the tree read and the
            # press: no lock signal apart from SpringBoard's (which says locked over Notification Center too) is known.
            # The press would then light the lock screen; Pause, checked here, is the owner's stop.
            if self.control.paused:
                raise Refused("paused", "the phone is paused; ask the user to resume it")
            if self.control.person_generation != generation or self.control.person_active():
                raise Refused("busy", "a person used the phone before delivery; look again before acting")
            if self.session.device is not device:
                raise Refused("reconnecting", "the phone connection changed; look before acting")

        async def home():
            try:
                source = await self.session._source(HOME_STUCK_CAP)
            except (ConnectionLost, DeviceError, AppNotAnswering):
                raise locked from None
            if not center_over_unlocked(source):
                raise locked
            token = device_module.BEFORE_SEND.set(guard)
            try:
                await guard()
                await device.http("POST", "/wda/homescreen", {}, timeout=HOME_STUCK_CAP)
            finally:
                device_module.BEFORE_SEND.reset(token)

        try:
            await self.control.agent(home)
        except ConnectionLost:
            raise Refused("timeout", "the phone didn't answer the home press in time; Notification Center may still "
                                     "close: look before acting") from None
        except DeviceError:
            raise Refused("internal", "the home press over Notification Center failed; look before acting") from None
        self.log("pressed home over Notification Center (iOS reported the phone as locked while it was open)")
        self._announce("agent", "press_button", label="home")
        try:  # the sheet is gone: the next call needn't wait for the heartbeat to see the phone unlocked
            if not await self.control.read(lambda: self._connected_only(self.session.locked)) \
                    and self.state == "locked":
                self._set_state("live")
        except Exception:
            pass  # (the heartbeat sets it)
        return _answer(["Pressed home: Notification Center was open (iOS reports the phone as locked while it is); "
                        "look before acting."], notes)

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
            raise Refused("not_found", f"{ref} isn't in your last OCR reading; look with ocr: true again (a screen "
                                       "whose elements weren't read gets o refs with any look).")
        return {**args, "target": {"x": round(point[0], 1), "y": round(point[1], 1)}}

    def _tier_now(self) -> str:
        """How much of the tree the newest read got (Session.last_tier, runner route version 9): "full", "shallow" or
        "root". Always "full" with an older runner."""
        tier = getattr(self.session, "last_tier", "full")
        return tier if tier in ("shallow", "root") else "full"

    def _ocr_possible(self) -> bool:
        """Can text be read from the screen's picture: by the runner (route version 9) or on this computer?"""
        return self._runner_reads_text() or ocr.available()

    def _runner_reads_text(self) -> bool:
        reads = getattr(self.session, "reads_text", None)
        return bool(reads()) if callable(reads) and RUNNER_OCR != "0" else False

    async def _screen_text(self, level: str = "accurate", implicit: bool = False) -> "tuple[list[ocr.Seen], str]":
        """(the text lines the screen shows now, a note for the answer or ""). The runner reads them on the phone
        (route version 9, Apple's Vision: faster than this computer's CPU); RapidOCR reads the picture here when the
        runner can't (an older runner, or its OCR failed: never a failed call). Neither: no lines, and the note says
        how to install the extra. `implicit`: a read the agent didn't ask for (a cut-short look, a wait's picture): a
        broken install here is a note too, never a failed call (review R200v L5)."""
        failed = ""
        if self._runner_reads_text():
            asked = self.hub.mono()
            try:
                seen = await self._read(lambda: self.session.read_text(level))
            except Refused:
                raise
            except Exception as e:  # noqa: BLE001 - the picture is read here instead
                self.log(f"runner OCR failed ({type(e).__name__}: {self._redact(str(e))[:160]}): reading the picture here")
                failed = f"the phone's OCR failed ({type(e).__name__})"
            else:
                if self.unlocked_since(asked):  # (M1 as for a picture: an unlock's keypad is never read)
                    raise Refused("busy", UNLOCK_BUSY)
                return seen, ""
        if not ocr.available():
            return [], (f"{failed}, and " if failed else "no text read from the picture: ") + ocr.INSTALL
        frame, asked = self.hub.latest, self.hub.mono()
        if frame is not None and self.hub.now_ms() - frame.at <= FRESH_FRAME * 1000:
            if self.frame_private(frame):
                raise Refused("busy", UNLOCK_BUSY)
            picture = await jpeg_of(frame)
        else:
            picture = await self._read(self.session.screenshot_png)
            if self.unlocked_since(asked):
                raise Refused("busy", UNLOCK_BUSY)
        try:
            return await asyncio.to_thread(ocr.read, picture, self.session.screen), ""
        except Exception as e:  # noqa: BLE001 - a broken install: the look is still the answer
            if not failed and not implicit:
                raise
            self.log(f"OCR here failed{' too' if failed else ''}: {type(e).__name__}: {e}")
            if not failed:
                return [], f"no text read from the picture: reading it here failed ({type(e).__name__})."
            return [], f"{failed}, and so did reading the picture here ({type(e).__name__})."

    async def _with_ocr(self, view, answer: dict, tier: str = "full") -> dict:
        """`look` with `ocr: true`, and (2.0) every look whose tree was cut short (`tier` shallow or root): the text the
        picture shows that no element carries, as lines with refs o1, o2… after the list (before its notes). On a
        screen with no elements at all (root) the header's `Elements: none` line says the text follows."""
        seen, note = await self._screen_text(implicit=tier != "full")
        # compared in the screen's own words with the tree's; shown as an answer shows text (a password the picture
        # shows is its placeholder, §18.6)
        known = [k.casefold() for e in view.items for k in (e.text, e.label, e.value, e.placeholder) if k]
        width, height = self.session.screen
        top = STATUS_BAR if height > width else 0.0  # the clock and the battery are the phone's, not the app's
        new = [s for s in seen if s.rect[1] >= top and not _said(s.text, known)]
        view.ocr = {f"o{n}": s.center for n, s in enumerate(new, 1)}
        lines = answer["text"].split("\n")
        cut = next((i for i, line in enumerate(lines) if line.startswith("Note: ")), len(lines))
        refs = [dataclasses.replace(s, text=ui.shown(s.text)).line(f"o{n}") for n, s in enumerate(new, 1)]
        none_line = next((i for i, line in enumerate(lines) if line == ui.ELEMENTS_NONE + "."), None)
        if note:
            pass  # nothing was read: the note says why (and how to read it)
        elif tier == "root" and none_line is not None:
            lines[none_line] = ui.ELEMENTS_NONE + ("; text read from the picture:" if new else
                                                   "; no text read from the picture either.")
            lines[cut:cut] = refs
        else:
            lines[cut:cut] = [OCR_HEAD if new else OCR_NONE, *refs]
        text = "\n".join(lines) + (f"\nNote: {note}" if note else "")
        return {**answer, "text": text}

    def _refuse_cut_report(self, tool: str) -> None:
        """(R200v M1) audit and ux_check are made only from a whole tree (CUT_REPORT)."""
        if self._tier_now() != "full":
            raise Refused("unsupported", CUT_REPORT.format(tool=tool, unread=UNREAD[0].upper() + UNREAD[1:]))

    async def _audit(self, view, args: dict, notes: list) -> dict:
        """AGENT-TOOLS §10: XCTest's own accessibility audit of the screen in front, plus `duplicate_id` from the
        tree, as one report. The tree is read first so each issue is named by the agent's current element line."""
        types = list(args.get("types") or audit.TYPES)
        asked = [audit.XCTEST[t] for t in types if t in audit.XCTEST]
        await self._settle_pending()
        await self._read(lambda: self._silent_read(view))
        self._refuse_cut_report("audit")
        issues = await self._read(lambda: self.session.audit(asked)) if asked else []
        app = getattr(self.session, "app", None)
        name = app[0] if app else "the screen"
        text = audit.report(f'"{name}"', issues, view.items, types)
        return {"text": "\n".join([text, *(f"Note: {n}" for n in notes)]), "image": None, "image_size": None}

    UX_AUDIT = ("clipped_text", "contrast", "hit_region")  # the audit types ux_check reports (§17.3)

    async def _ux_check(self, client_id: str, view, notes: list) -> dict:
        """AGENT-TOOLS §17.3: the screen in front against the usability basics (tapwright/uxcheck.py), with the audit's
        cut-off text, contrast and target size. The tree is read first, as `audit` does."""
        await self._settle_pending()
        given = _GIVEN.get()
        session = self.session

        async def front() -> str | None:
            try:
                return await session.foreground_bundle_id() or None
            except ConnectionLost:
                raise  # (the read's own reconnect handles it)
            except DeviceError:
                return None

        async def turn() -> tuple[str | None, list | DeviceError, str | None, UxRead]:
            # One turn of the phone's queue: the app in front, the tree, the audit, and the app in front again. No
            # action or person's input comes between them, so the elements checked are the given app's when both reads
            # name it (Codex, PR #7 cycle 3: reads in separate turns let another client open an unlisted app for the
            # tree and come back before the check; CodeRabbit, PR #7).
            first = await front() if given is not None else None
            await self._silent_read(view)
            whole = self._tier_now() == "full"  # (R200v M1: a cut-short list is refused below, after the app check)
            try:
                found: list | DeviceError = await session.audit([audit.XCTEST[t] for t in self.UX_AUDIT]) if whole else []
            except ConnectionLost:
                raise
            except DeviceError as e:
                found = e
            # What this turn read, kept for this call: the agent's view is shared with its other calls, and a look of
            # theirs can replace it before this one is done (Codex, PR #7 cycle 4)
            seen = UxRead(list(view.items), view.alert_now, tuple(session.screen), getattr(session, "app", None), whole)
            return first, found, (await front() if given is not None else None), seen
        for attempt in (1, 2):
            first, issues, last, seen = await self._read(turn)
            if given is None or (first == last and first in given):
                break
            if first == last == SPRINGBOARD and await self._prompt_of_given(given, seen, read=False):
                break  # a given app's own permission prompt (as this turn read it)
            if attempt == 1 and first == last == SPRINGBOARD:
                # a notification banner over the app makes WebDriverAgent say SpringBoard: _front waits it out, and
                # the turn is read again only when a given app is in front then
                if (await self._front(given)) in given:
                    continue
            # Personal mode: nothing of another app's screen is remembered for naming drift, checked or filed
            shown = last if first == last else (last or first)
            raise Refused("not_allowed", _outside(shown, self.session.app_names.get(shown or ""),
                                                  ", ".join(given.values())))
        if not seen.whole:
            self._refuse_cut_report("ux_check")  # (nothing is checked, filed on the journey or kept for naming drift)
        rows: list[tuple[str, str, str]] = []
        notes = list(notes)
        if isinstance(issues, DeviceError):
            notes.append(f"the phone's accessibility audit isn't available here ({issues}), so cut-off text, contrast and "
                         "target size were not checked.")
        else:
            rows = audit.rows_of(issues, seen.items, list(self.UX_AUDIT))
        rules = uxcheck.rules_in_force()
        result = uxcheck.check(seen.items, seen.screen, client_id, rules, rows)
        app = seen.app
        title = uxcheck.title_of(seen.items)
        screen = f'"{app[0] if app else "the screen"}"' + (f" ({ui.clip(title)[1:-1]})" if title else "")
        # the findings go on the journey's screen these elements are, not on whatever screen another call has made
        # current since (Codex, PR #7 cycle 5)
        uxcheck.tell_journey(client_id, result, screen, "\n".join(e.line() for e in seen.items if e.ref))
        text = uxcheck.report(screen, result, rules)
        return {"text": "\n".join([text, *(f"Note: {n}" for n in notes)]), "image": None, "image_size": None}

    async def _media(self, client_id: str, args: dict, notes: list) -> dict:
        """AGENT-TOOLS §17.6: put test pictures in the phone's photo library (add-only access), or delete what this
        daemon added. Deleting needs full access, and Tapwright never asks for it: it deletes only when the person has
        already given the runner full access themselves (iOS then asks them to confirm each delete), and otherwise names
        the pictures to delete by hand (Codex, PR #7: the runner's delete route asks iOS for full access when it hasn't
        been decided)."""
        add, remove = args.get("add"), args.get("remove")
        if remove is not None and not (isinstance(remove, bool) or remove == "all"):
            raise Refused("bad_params", f'remove must be true or "all", not {remove!r}')
        everything = remove == "all"
        if add is not None and (not isinstance(add, list) or not all(isinstance(p, str) for p in add)):
            raise Refused("bad_params", "add must be a list of paths of pictures or videos on this computer")
        if add and remove:
            raise Refused("bad_params", "media adds or removes in one call, not both: use add, then a separate call "
                                        "with remove: true")
        if not add and not remove:
            raise Refused("bad_params", "media needs add (the paths of pictures or videos) or remove: true")
        if self.control.paused:
            raise Refused("paused", "paused by the user in the live view; ask them to resume")
        if self.state == "locked":
            raise Refused("locked", PHONE_LOCKED)
        pictures: list[tuple[str, bytes] | medialib.Video] = []
        if add:
            try:
                pictures = await asyncio.to_thread(medialib.read_media, add)
            except ValueError as e:
                raise Refused("bad_params", str(e)) from None
        videos = sum(isinstance(p, medialib.Video) for p in pictures)
        if self._photo_wait_left() is not None:
            # an add holds the runner (iOS's photo prompt, or a video's copy and import): nothing can be asked of it (its
            # version, its access) until it is done. An add of the same pictures picks up the held one; anything else
            # waits its turn (Codex, PR #8).
            if not add:
                raise self._held_busy()
            names = [p[0] for p in pictures]
            await self._media_adding(client_id, pictures, names, asks=None)
            return _answer([medialib.added_text(names, videos)], notes)
        await self.wait_connected()
        try:
            version = await self._read(lambda: self.session.runner_version())
        except DeviceError as e:  # the question got no answer: not an old runner, so ask again
            raise Refused("internal", f"couldn't ask the runner which version it is ({e}); try again") from None
        if version < medialib.MIN_TW_VERSION:
            raise Refused("unsupported", medialib.OLD_RUNNER)
        if videos and version < medialib.VIDEO_TW_VERSION:
            raise Refused("unsupported", medialib.old_runner_for_video(version))
        access = await self._read(lambda: self.session.photos_access())
        if add:
            if access.get("add") in ("denied", "restricted"):
                raise Refused("unsupported", "the runner may not add to the photo library: iOS has it denied. Ask the "
                                             "user to allow WebDriverAgentRunner in Settings > Privacy & Security > "
                                             "Photos, then try again.")
            names = [p[0] for p in pictures]
            # iOS asks the person on the first add (add access not decided yet), and the runner's add waits for the
            # answer with the runner's whole queue: nothing else it is asked, an alert read included, is answered
            # meanwhile. So the prompt is known from the access, before anything is sent (Codex, PR #8).
            await self._media_adding(client_id, pictures, names, asks=access.get("add") == "notDetermined")
            return _answer([medialib.added_text(names, videos)], notes)
        held, earlier = medialib.held_by(client_id, self._media_earlier, everything)
        if not held:
            return _answer(["Nothing to remove: this agent has added no pictures or videos that are still recorded "
                            '(remove: "all" takes everything Tapwright ever added, another agent\'s too).'], notes)
        names = [e["name"] for e in held]
        if access.get("full") in ("denied", "restricted"):
            return _answer([medialib.by_hand_text(names, "the runner has no full access to the photo library "
                                                        "(iOS has it denied).")], notes)
        if access.get("full") != "authorized":  # not decided (or unknown): the delete route would ask iOS for it
            return _answer([medialib.by_hand_text(names, medialib.NEVER_ASKS_FULL)], notes)
        try:
            deleted = await self._media_write(lambda: self.session.photos_delete([e["id"] for e in held]),
                                              f"removing the {medialib.named(names)}", check=CHECK_REMOVED)
        except DeviceError:  # (only the "full access" refusal gets here: _media_write words the rest)
            return _answer([medialib.by_hand_text(names, "the runner has no full access to the photo library.")], notes)
        lead = "From an earlier session: " if earlier else ""
        if not deleted:
            return _answer([f"{lead}Removed none of {medialib.named(names)}: the person declined the delete on "
                            "the phone, or they were already gone. Ask them to confirm it on the phone, then call "
                            "again."], notes)
        medialib.drop([e["id"] for e in held])
        return _answer([lead + medialib.removed_text(names[:deleted], names)], notes)

    def _claim(self, task: "asyncio.Future", flight: _Flight) -> None:
        """`task` holds the phone, for as long as what it is doing now may take (1.7.9, review R179 M2, L1, L2):
        - a video's copy to the phone: the copy's budget (UPLOAD_RATE), in a video's own words; iOS asks nothing yet,
        - an add of pictures that iOS asks about: PHOTO_WAIT_MAX, in the prompt's words,
        - an add with a video, once sent: the add's own deadline (Session.photos_timeout: iOS's prompt, then the
          import), in a video's words (and the prompt's, when iOS asks).
        Pictures iOS doesn't ask about hold nothing (the runner answers in a moment). Called when the add starts, when
        it goes out (from that moment, even when the copy's hold ran out: L1), and when a repeated call picks it up
        after its hold ran out."""
        if task.done():
            return
        if not flight.went_out.is_set():
            if flight.video_bytes:
                seconds, kind = flight.video_bytes / UPLOAD_RATE + PHOTO_WAIT_MAX, "copy"
            elif flight.asks:
                seconds, kind = PHOTO_WAIT_MAX, "prompt"
            else:
                return
        elif flight.video_bytes:
            seconds = session_module.photos_timeout(flight.video_bytes, flight.asks) + MEDIA_HOLD_SLACK
            kind = "video-prompt" if flight.asks else "video"
        elif flight.asks:
            seconds, kind = PHOTO_WAIT_MAX, "prompt"
        else:
            return
        other = self._photo_wait[0] if self._photo_wait_left() is not None else None
        if other is not None and other is not task and self._photo_wait is not None:
            # another add holds the phone already (this one began after that hold ran out): keep its words, and hold
            # for as long as either needs
            self._photo_wait = (other, max(self._photo_wait[1], self.control.clock() + seconds))
            return
        self._hold(task, seconds, kind)

    async def _media_adding(self, client_id: str, pictures: "list[tuple[str, bytes] | medialib.Video]",
                            names: list[str], asks: "bool | None" = False) -> None:
        """The add, once. iOS asks the person whether the runner may use Photos and the runner's request waits for the
        answer, so the request runs as a task of its own and this call watches for a system alert in front: when one
        is up it stops waiting at once (needs_a_person), and nobody is held up behind it (`alert` can read the text).
        The request itself is never cancelled or sent twice: it goes on and records its pictures when it finishes, and
        the next media call with the same pictures picks it up instead of adding them again. An add whose end isn't
        known (no answer, the runner's own wait ran out, a dropped line) stays too: the next call with the same
        pictures says so once instead of adding them again (review R179 M1). `asks` None: an add holds the phone, and
        only that add is picked up (whether iOS asks is its own)."""
        # the same pictures, in the same order: by their bytes, not their file names (Codex, PR #8)
        same = tuple((p.name, p.digest) if isinstance(p, medialib.Video) else (p[0], hashlib.sha256(p[1]).hexdigest())
                     for p in pictures)
        video_bytes = sum(p.size for p in pictures if isinstance(p, medialib.Video))
        # Look up, gate, create, register and claim the hold as one step, for every client at once: two calls can't
        # both find nothing and send pictures twice (CodeRabbit, PR #8), and two agents' first adds can't both find no
        # hold and both take it (Codex, PR #8 cycle 4). The lock is let go before the wait on the task.
        async with self._media_lock:
            kept = self._media_adds.get(client_id)
            now = self.control.clock()
            holding = self._photo_wait[0] if self._photo_wait_left() is not None else None
            if holding is not None and (kept is None or kept[1] is not holding):
                # another agent's add holds the runner (iOS's prompt, a video's import): a second add would queue
                # behind it on the runner, past the hold's deadline, and must not take it over (Codex, PR #8 cycle 3)
                raise self._held_busy()
            if kept is not None and not kept[1].done() and kept[0] != same:
                # another add of this client is still going on (waiting on the person, or a video's copy and import):
                # other pictures would queue behind it and be added next to it; one at a time (CodeRabbit, PR #8)
                going = self._media_flights.get(kept[1])
                if going is not None and going.video_bytes:
                    raise Refused("busy", "an add with a video is still going on (it is copied to the phone, then "
                                          "imported); call media again with the others once it is done")
                raise Refused("busy", "an add of other pictures is still waiting for the person's answer on the "
                                      "phone; call media again with those pictures once the person has answered")
            if kept is not None and kept[0] == same and (not kept[1].done() or now - kept[2] < MEDIA_KEEP):
                task = kept[1]
                unknown = _unknown_end(task)
                if unknown is not None:
                    # said once: the next call with them adds them again (the person checked Recently Added)
                    del self._media_adds[client_id]
                    raise Refused("timeout", EARLIER_UNKNOWN.format(
                        why="the line dropped" if unknown.code == "reconnecting" else "no answer in time"))
                flight = self._media_flights.get(task)
                if flight is not None and self._photo_wait_left() is None:
                    self._claim(task, flight)  # its hold ran out while it still goes on (the wait isn't moved: cycle 3)
            else:
                going = [f for t, f in self._media_flights.items() if not t.done()]
                if any(f.video_bytes for f in going) or (video_bytes and going):
                    # an add with a video whose hold ran out (a copy slower than its budget) is still copying or
                    # importing, or a video add would start behind another add: never two at once, since one hold
                    # can't cover both and the second's end would clear it under the first (CodeRabbit, PR #19)
                    raise Refused("busy", VIDEO_ADD_GOING)

                async def gate() -> None:  # a person driving or a pause refuse it, as for a touch; the wait isn't held in the queue
                    return None
                if asks is None:
                    # the hold this call saw has ended (that add finished) and there is nothing to pick up: this call
                    # skipped the runner's version and access reads, so it starts nothing; the retry takes the normal
                    # way (review R179b L4: a v6 runner would get a clip, and a granted access a prompt's words)
                    raise Refused("busy", HOLD_ENDED_BUSY)
                await self.control.agent(gate)
                flight = _Flight(asks=asks, video_bytes=video_bytes)
                own: list[asyncio.Future] = []

                async def sent() -> None:
                    # Right before the add goes out (after a video's copy, which may take minutes): a pause or a person
                    # driving since the gate is checked again, as for a touch. Paused: nothing is sent, the session
                    # removes the staged folder, and the add answers `paused` (CodeRabbit, PR #19). The caller having
                    # left doesn't stop it here: the add was asked for and goes on (STILL_WANTED is the call's, not
                    # the add's).
                    token = STILL_WANTED.set(None)
                    try:
                        await self.control.agent(gate)
                    finally:
                        STILL_WANTED.reset(token)
                    # the add goes out now: the pick-up window and the hold count from here, not from the copy's start
                    # (review R179 L1, L3); iOS asks (when it does) only now
                    flight.went_out.set()
                    if not own:
                        return
                    entry = self._media_adds.get(client_id)
                    if entry is not None and entry[1] is own[0]:
                        self._media_adds[client_id] = (entry[0], own[0], self.control.clock())
                    self._claim(own[0], flight)
                task = asyncio.ensure_future(self._media_added(client_id, pictures, names, sent, asks=flight.asks))
                own.append(task)
                self._media_flights[task] = flight
                task.add_done_callback(lambda done: self._media_flights.pop(done, None))
                task.add_done_callback(lambda done: done.cancelled() or done.exception())  # (a failure is the waiter's, if any)
                self._media_adds[client_id] = (same, task, now)

                def ended(done: asyncio.Future) -> None:
                    if self._photo_wait is not None and self._photo_wait[0] is done:
                        self._photo_wait = None
                        e = None if done.cancelled() else done.exception()
                        if isinstance(e, Refused) and e.code == "reconnecting":
                            self.lost(f"the line dropped while a media add held the runner: {e}")
                task.add_done_callback(ended)
                # the hold starts with the add, once, inside the step: no other add can start before it is claimed
                # (cycle 4)
                self._claim(task, flight)
        if flight is not None and flight.asks and not task.done():
            if not flight.went_out.is_set():
                # still being copied (a video) or encoded: iOS asks once the add goes out
                waiting = asyncio.ensure_future(flight.went_out.wait())
                try:
                    await asyncio.wait({task, waiting}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    waiting.cancel()
            # iOS's prompt is on the phone (or about to be): the add goes on, and finishes when the person answers
            await asyncio.wait({task}, timeout=MEDIA_WATCH)  # (an answer already given lets it finish here)
            if not task.done():
                raise Refused("not_allowed", NEEDS_PHOTO_ANSWER)
        # a video's copy and import ask nothing of the person, and the runner answers no look meanwhile (each would
        # queue on it behind the import): it is waited for, not watched
        peek = flight is None or not flight.video_bytes
        while True:
            done, _ = await asyncio.wait({task}, timeout=MEDIA_WATCH)
            if done:
                break
            if not peek:
                continue
            alert = await self._media_alert()
            if alert is not None:
                named = RUNNER_NAME in alert.title or "photo" in alert.title.lower()
                raise Refused("not_allowed", NEEDS_PHOTO_ANSWER if named else NEEDS_ALERT_ANSWER)
        entry = self._media_adds.get(client_id)
        if entry is not None and entry[1] is task and _unknown_end(task) is None:  # only its own entry
            del self._media_adds[client_id]
        task.result()

    async def _media_alert(self) -> ui.Alert | None:
        """The system alert in front, read while the add waits; None when there is none or the runner can't say in time."""
        try:
            return await asyncio.wait_for(self.session.peek_alert(), MEDIA_PEEK)
        except asyncio.CancelledError:
            raise
        except Exception:
            return None

    async def _media_added(self, client_id: str, pictures: "list[tuple[str, bytes] | medialib.Video]", names: list[str],
                           sent: "Callable[[], Awaitable[None]] | None" = None, asks: bool = False) -> None:
        ids = await self._media_write(lambda: self.session.photos_add(pictures, sent=sent, asks=asks),
                                      f"adding the {medialib.named(names)}", queued=False)
        if len(ids) != len(names):
            if ids:  # what it did make stays recorded, so remove can take it
                medialib.record(client_id, ids, names[:len(ids)])
            raise Refused("internal", f"the phone made {len(ids)} of the {medialib.named(names)} sent. Check Photos > "
                                      "Recently Added.")
        medialib.record(client_id, ids, names)

    async def _media_write(self, call, doing: str, queued: bool = True, check: str = CHECK_ADDED):
        """A write to the phone's photo library, once: sent again after a drop it could be done twice. `queued`: in the
        phone's queue like a touch (a person first); the add waits for iOS's prompt, so it isn't, or it would hold
        every other call back for as long as the person takes. `check`: what the agent checks before calling again
        when the end isn't known (an add's or a remove's own words: review R179b L1)."""
        try:
            if not queued:
                return await self._connected_only(call)
            return await self.control.agent(lambda: self._connected_only(call))  # person-first, like a touch
        except ConnectionLost as e:
            self.lost(e)
            raise Refused("reconnecting", f"the connection dropped while {doing}; it may or may not have happened: "
                                          f"wait a few seconds, then {check}") from None
        except Unanswered as e:  # the line is alive, but the phone may have done it (CodeRabbit, PR #7)
            raise Refused("timeout", f"{e} while {doing}; it may or may not have happened: {check}") from None
        except device_module.RunnerTimedOut as e:
            # the runner's own wait ran out, not a refusal: the library's change may still finish (review R179 M1)
            raise Refused("timeout", f"the phone's runner stopped waiting while {doing} ({e}); the photo library may "
                                     f"still finish it: {check}") from None
        except DeviceError as e:
            if "full access" in str(e).lower():
                raise
            raise Refused("unsupported", f"the phone couldn't do it while {doing}: {e}") from None

    async def _performance(self, client_id: str, args: dict, notes: list) -> dict:
        """PLAN-1.3 item 3: memory, CPU and frames of the app in front while the call runs (`seconds`, around an optional
        `tap`), or from a cold start to the first settled screen (`launch`). The app's own process only. The taps and
        the close and open go through the ordinary tools, so personal mode and the safety policy apply to them."""
        if self.state == "locked":
            raise Refused("locked", PHONE_LOCKED)
        scope, clock = _SCOPE.get(), self.control.clock
        bundle = await self._read(self.session.foreground_bundle_id) or self._bundle()
        if bundle in (None, SPRINGBOARD):
            raise Refused("not_found", NO_APP_TO_MEASURE)
        given = _GIVEN.get()
        if given is not None and bundle not in given:  # read again after personal mode's check: another app may be in front now
            raise Refused("not_allowed", _outside(bundle, self.session.app_names.get(bundle), ", ".join(given.values())))
        launch = bool(args.get("launch"))
        seconds = float(args.get("seconds", performance.SECONDS_DEFAULT))
        meter, opened, unsettled = self.session.device.meter(bundle), None, False
        crashes: list[str] = []  # the crash notes of the calls inside: their report is taken, the outer check won't find it

        def keep_crash(answer: dict) -> None:
            crashes.extend(ln[len("Note: "):] for ln in answer["text"].splitlines()
                           if ln.startswith(CRASH_NOTE) and ln[len("Note: "):] not in crashes)
        token = _NESTED.set(True)
        try:
            # The meter starts before a launch closes the app: a phone that can't sample answers `unsupported` with the
            # app still open (review 2026-11-12). It finds the new process by bundle id after the launch.
            try:
                await meter.start()  # (cancelled or failed, it has stopped its own sampling)
            except DeviceError as e:
                raise Refused("unsupported", f"the phone can't sample the app: {e}") from None
            try:
                if launch:
                    keep_crash(await self.run_tool(client_id, "close_app", {"app": bundle}, scope))
                # the window begins here: what the instruments gave while they were set up, and the app that was
                # running before a launch, are dropped (the instruments stay ready)
                await meter.begin_window(fresh=launch)
                began = clock()
                if launch:
                    answer = await self.run_tool(client_id, "open_app", {"app": bundle}, scope)
                    keep_crash(answer)
                    opened = clock() - began
                    unsettled = NOTE_CAPPED.split("{cap")[0] in answer["text"]
                else:
                    if args.get("tap") is not None:
                        keep_crash(await self.run_tool(client_id, "tap", {"target": args["tap"], "look": False}, scope))
                    await self.control.sleep(max(0.0, seconds - (clock() - began)))
            finally:
                found = await meter.stop()
        finally:
            _NESTED.reset(token)
        if found.failed:  # sampling broke during the window: what was gathered is only part of it
            notes = [*notes, performance.SAMPLING_STOPPED.format(why=found.failed)]
        if found.lost:
            notes = [*notes, performance.SAMPLES_LOST.format(n=found.lost)]
        reading = performance.summarize(found, clock() - began, opened, unsettled)
        self._performed[client_id] = reading
        self._measured = {k: round(v, 2) if isinstance(v, float) else v for k, v in dataclasses.asdict(reading).items()
                          if v is not None and v is not False}
        return _answer(performance.answer_lines(reading), [*notes, *crashes])

    async def _filmstrip_logged(self, client_id: str, args: dict, scope: "Scope | None") -> dict:
        """`filmstrip` as one tool call, with its line in the action log (its tap is logged as a tap, as usual)."""
        started, self._announced, self._granted_now = time.monotonic(), None, False
        token = _FILM_SCOPE.set(scope)
        logged = {**args, "target": args.get("tap")}
        try:
            result = await self._run_tool_inner(client_id, "filmstrip", args, scope)
        except Refused as e:
            self._log_action("filmstrip", logged, started, e, client=client_id)
            raise
        finally:
            _FILM_SCOPE.reset(token)
        made = re.match(r"Filmstrip: (\d+) frames", result["text"])
        self._log_action("filmstrip", logged, started, None, frames=int(made.group(1)) if made else None,
                         client=client_id, answer=result)
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
        if self.frame_private(frame):  # (1.7.8, M1) an unlock began while filming: the strip is refused
            raise Refused("busy", UNLOCK_BUSY)
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
        asked = self.hub.mono()
        shot = await self._read(self.session.screenshot_png)
        if self.unlocked_since(asked):
            raise Refused("busy", UNLOCK_BUSY)
        return after_seq, self.hub.now_ms(), shot

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

    async def _compare_screen(self, view, args: dict, notes: list, client: str | None = None) -> dict:
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
                raise Refused("locked", PHONE_LOCKED)
            done = await self._compare(view, args["name"], update=bool(args.get("update")), width=width,
                                       threshold=args.get("threshold"), ignore=args.get("ignore"), check=check)
            if (held := _COMPARED.get()) is not None:
                held.append(done)
            if check and done.reason is not None:
                raise Refused("expect_failed", done.reason)
        except Refused as e:
            self._log_compare(args["name"], started, e, done, client)
            raise
        said = [done.lines[0]] if check and done.named else []  # a step's note in `run` (the answer's own line is its trace)
        answer = _answer(done.lines, [*done.notes, *said, *notes], done.image)
        self._log_compare(args["name"], started, None, done, client, answer)
        return answer

    def _log_compare(self, name: str, started: float, error: Refused | None, done: "Compared | None",
                     client: str | None = None, answer: dict | None = None) -> None:
        """The action log's line for a picture check: the name and the share that changed, never the picture."""
        self.actions.write({
            "source": "tapwright", "by": "agent", "client": client, "tool": "compare_screen", "kind": "read", "name": name,
            "answer_chars": len(answer["text"]) if answer is not None else len(str(error)) if error is not None else None,
            "percent": round(done.percent, 2) if done is not None and done.percent is not None else None,
            **({"named": done.named} if done is not None and done.named else {}),
            "ok": error is None, "code": error.code if error is not None else None,
            "ms": round((time.monotonic() - started) * 1000), "via": _VIA.get(), "app": self._bundle(),
            "batch": BATCH.get()})

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
        if self._tier_now() != "full" and any(e.get("width") is None for e in ignore or []):
            # (R200v M1) an element to paint out may be among what the cut-short read left out: a reference saved or
            # compared without it would hold (or fail on) what it was meant to ignore. A rect needs no read.
            raise Refused("unsupported", f"compare_screen: {UNREAD} within the time given, so the elements to ignore "
                                         "can't be found. Ignore them by rect ({x, y, width, height}), or go to a still "
                                         "screen first.")
        rects, notes = compare.ignored(view.items, ignore or [])
        if (other := compare.twin(folder, name, script["path"] if script else None)) is not None:  # before any read or write
            raise Refused("bad_params", namelib.twin_message("reference", name, other))
        found = None if (update and not check) or keep else compare.find(name, folder, script["path"] if check and script else None)
        if found is None and check and not keep:
            return Compared("missing", [], notes, compare.missing(name), folder=folder)
        asked = self.hub.mono()
        png = await self._read(self.session.screenshot_png)
        if self.unlocked_since(asked):  # (1.7.8, M1) never a picture of the keypad, saved or compared
            raise Refused("busy", UNLOCK_BUSY)
        if given is not None:  # the picture is saved or compared only if a given app was still in front for it
            await self._still_given(given)
        before, before_bytes = await asyncio.to_thread(compare.read, found) if found is not None else (None, None)
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
                written = await asyncio.to_thread(compare.save, dest, after, trusted)
            except OSError as e:
                raise Refused("internal", f"couldn't save the reference to {dest}: {e.strerror or e}") from None
            return Compared("saved", [f'Saved the reference "{name}" ({after.width}x{after.height} px).'], notes,
                            folder=folder, picture=written)
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
            names = await asyncio.to_thread(compare.named_changes, result, scale, view.items) if result.changed else []
            if names:  # still a match (no script breaks), but a change a person would want to know about is named
                return Compared("matches", [compare.matched_with_changes(name, share, threshold, names)], notes, None,
                                None, result.percent, folder, before_bytes, named=len(names))
            return Compared("matches", [f'Matches the reference "{name}": {share} changed.'], notes, None, None,
                            result.percent, folder, before_bytes)
        reason = compare.failure(name, result, threshold, scale)
        if check:
            if script is not None and (where := await asyncio.to_thread(self._write_diff, script["path"], name, result)):
                reason += f" (diff: {where})"
            return Compared("differs", [], notes, reason, None, result.percent, folder, before_bytes)
        n = len(result.regions)
        lines = [f'Differs from the reference "{name}": {share} changed, in {n} region{"s" if n != 1 else ""} (points):',
                 *compare.region_lines(result.regions, scale, view.items)]
        image = await asyncio.to_thread(result.picture)
        return Compared("differs", lines, notes, reason, image, result.percent, folder, before_bytes)

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
        for _, was, _ in seen:
            if _name_key(was) == _name_key(name):
                _refuse_case_only(was, name)
        other = next((bundle for bundle, was, _ in seen if was == name and bundle != folder), None)
        if other is not None:
            raise Refused("bad_params", f'this recording already compares "{name}" in {other}, and a script keeps one '
                                        f"picture for each name; give the one in {folder} another name")
        wanted = [digest for bundle, was, digest in seen if was == name and bundle == folder]
        if not wanted:
            return
        if hashlib.sha256(compare.encode(picture)).hexdigest() not in wanted:  # (the bytes compare.save would write)
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
        undecided = False  # (2.0, R200v H2) the newest poll's screen couldn't say either way (UNREAD)

        def timed_out() -> Refused:
            if undecided:  # never "it didn't happen", never Done: nothing was proven
                return Refused("timeout", self._unread_message(kind, args[kind], gone, timeout,
                                                               kind == "text" and self._ocr_possible()))
            return Refused("timeout", f'Waited {timeout:g} s for {kind} "{args[kind]}" to '
                                      f"{'disappear' if gone else 'appear'}; it didn't happen.")
        seen = False  # has the screen been read at all during this wait?
        while True:
            left = timeout - (clock() - began)
            if left < WAIT_FOR_MIN_LEFT:
                raise timed_out()
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
                raise timed_out() from None
            seen = True
            # (2.0) on a screen that didn't answer the whole read, a text is looked for in the picture's text too; an id
            # or label it didn't give is neither there nor gone, and the wait goes on (the next read may be whole)
            there = await self._presence(tree.items, target, kind, args[kind], _cut_short(tree),
                                         timeout - (clock() - began))
            undecided = there is None
            if there is not None and there != gone:
                break
            left = timeout - (clock() - began)
            if left < WAIT_FOR_MIN_LEFT:
                raise timed_out()
            await self.control.sleep(min(WAIT_FOR_POLL, left))
        result = f"Done after {clock() - began:.1f} s."
        if not args.get("look", True):
            view.unlooked += 1
            return _answer([result], notes)
        full = view.seen != self.control.generation or view.dropped or view.read_at == float("-inf")
        # the last poll is the answer's tree: no read of its own, only the app lookup the polls left out
        snap = await self._read(lambda: self.session.snapshot_from(tree))
        return await self._add_shot(await self._after(view, result, notes, full, snap=snap), view,
                                    self._cut_shot(args, shot, getattr(tree, "tier", "full")))

    async def _type_text(self, view, a: dict) -> Typed:
        """Type into the target, or into the only input on screen (the fast tree has no focus attribute).
        The target is tapped first by the session, which waits for the keyboard and for focus to arrive."""
        target = a.get("target")
        if target is None:
            await self._silent_read(view)  # which inputs there are must be read now
            # the app this very read names (the session's own `app` is the last snapshot's, from before a switch:
            # CodeRabbit, PR #18); a read whose root names none falls back to it
            read_app = (self._read_root or (None, False))[0]
            front = read_app if isinstance(read_app, tuple) else (
                self.session.app if isinstance(self.session.app, tuple) else None)
            if (view.aimed_app is not None and front is not None and front[1] != view.aimed_app[1]
                    and view.alert_now is None):  # (an OS alert's own field is typed into over any app)
                # (1.7.8, X item 4) another app came to the front since the agent's last touch or open_app (Safari, after
                # a tap in Files): its only input is no field the agent chose
                raise Refused("stale", f"{_app_words(front)} is in front now, not {_app_words(view.aimed_app)}, where "
                                       "your last touch went. Nothing was typed; look, then pass a target.")
            inputs = [e for e in view.items if e.role == "input"]
            if not inputs and self._tier_now() != "full":
                return await self._type_focused(a)  # (2.0) the screen gave no fields: the keys go to the focused one
            if not inputs:
                raise Refused("not_found", "No input on screen")
            if len(inputs) > 1 and sum(e.focused for e in inputs) == 1:
                inputs = [e for e in inputs if e.focused]  # §2: without a target, the focused field
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
        if (typed_secure := _TYPED_SECURE.get()) is not None:
            typed_secure.append(bool(field.secure))  # the field this call types into, for a recording
        forced, at = "", None
        # 1.7.8 (bug 6): a point is where the agent saw the field, and the focusing tap goes there, never to the field's
        # centre (X's composer: its centre was under the app's floating toolbar, and the tap opened the GIF picker)
        point = target.point if target is not None and target.point is not None else None
        # 1.7.8 (bug 5): the runner says this field has the keyboard's focus, so no tap is made (Session types into it
        # as it is): whatever is drawn over it (the keyboard's accessory area, over Safari's address field) is no reason
        # to refuse. Session is told not to tap: if the focus moved meanwhile, it refuses instead.
        no_tap = bool(field.focused) and self.session.tw_version >= 5
        if a.get("force"):
            self._floor_gate("type_text", a, field)  # (`force` changes nothing for the skip floor: review R180 L4)
            if point is not None and (key := next((b for b in field.covered_by if b.kind == "Keyboard"
                                                   and ui._holds(b.rect, point)), None)) is not None:
                # (Opus review 11) a touch there presses a key: it types into the focused field and focuses nothing
                raise Refused("not_actionable", f"covered: ({round(point[0])}, {round(point[1])}) is under {key.line}, "
                                                f"even with force; aim at the part of {field.line(actions=False)} that "
                                                "shows.")
            forced = self._forced(view, field)
            at = point
        else:
            self._guard(view, field)
            # the focusing tap's hit test (Codex, PR #12 cycle 3, M2): typing isn't policed (§11.1), the touch is checked
            aim = None if no_tap else self._typing_aim(point) if point is not None else self._touch_point
            checked, at, _ = await self._last_checks("type_text", a, field, police=False, aim=aim)
            field = checked if checked is not None else field
            at = at or field.center  # an explicit checked point: Session must not choose its right-edge shortcut
        active = _ACCOUNT.get()
        using = None
        if accountlib.mentions(a["text"]):
            using = self._loaded().reference(a["text"], active, bool(field.secure), field.ref, self._app_bundle())
        real = using.value if using is not None else a["text"]
        self._dry(a, f"Would type {len(a['text'])} character{'s' if len(a['text']) != 1 else ''} into {_brief(field)}.")
        if using is not None and using.otp_app is not None:
            self.session.before_input = None  # Notification Center's own gestures are not the pending focusing tap
            code = await self._code_for_typing(using.otp_app, field)  # (from the source apps' notifications only)
            if code is None:
                raise Refused("not_found", f"no one-time code for {using.otp_app}: none arrived in the 2 minutes since "
                                           "the app showed its code field. Nothing was typed.")
            real, using = code.code, dataclasses.replace(using, field=code.source)
        a["text"] = real  # (the call's own copy: the log and a recording keep the placeholder)
        aimed = [at]

        async def checked_again(reread: bool = False) -> None:
            # A code lookup or keyboard dismissal may have changed the field's visible geometry.
            now = next((e for e in self.session.items if field.ref and e.ref == field.ref), None)  # (as listed now)
            if now is None and reread:
                # the field didn't survive the new read (X's composer lays itself out again and again while the
                # keyboard is up): its ref names nothing now, and its old place proves nothing (1.7.8, bug 6)
                raise Refused("stale", f"{field.line(actions=False)} moved as the keyboard closed; look again. "
                                       "Nothing was typed.")
            now = now or field
            if point is not None and not ui._holds(now.rect, point):
                raise Refused("stale", f"{now.line(actions=False)} is no longer under ({round(point[0])}, "
                                       f"{round(point[1])}); look again. Nothing was typed.")
            if not a.get("force"):
                aim = self._typing_aim(point) if point is not None else self._touch_point
                _, aimed[0], _ = await self._last_checks("type_text", a, now, police=False, aim=aim)
                aimed[0] = aimed[0] or now.center
        if using is not None and (using.kind == accountlib.CARD or using.otp_app is not None):
            await checked_again()

        self._touching(field)

        async def typed() -> None:
            if not no_tap and field.placed:  # (the focusing tap: at a place the phone confirms, never the right-end shortcut)
                aimed[0] = aimed[0] or field.center
                await self._hosted_proof(next((e for e in self.session.items if e.ref == field.ref), field), aimed[0])
            more = {"no_tap": True} if no_tap else {}
            await self.session.type_text(a["text"], replace=bool(a.get("replace", True)),
                                         submit=bool(a.get("submit", False)), target=ui.Target(ref=field.ref), at=aimed[0],
                                         **more)
        try:
            await typed()
        except Refused as e:
            # The keyboard was left up for the tap on another input, and this app gave that tap to the keyboard
            # (it closed it, or nothing happened): close it the old way and tap again. Nothing was typed.
            if not (view.read_keyboard and "didn't take focus" in str(e)):
                raise
            await self._close_keyboard(view, "type_text", ui.Target(ref=field.ref), shown_inputs=True)
            await checked_again(reread=True)
            await typed()
        view.typed_field = (field.identifier, field.label, field.rect)  # §21.3: the draft a touch would send is read from it
        # the field's line without the value it had before the keys (1.7.8, X item 5): the answer gives the value only as
        # a read after the typing shows it (Typed.verdict). Safari's address field is another element once the page
        # loads, and its old address read as if it were the new one
        done = Typed(f"Typed into {dataclasses.replace(field, value='').line(actions=False, place=False)}.")
        retyped = NOTE_RETYPED if getattr(self.session, "retyped", False) else ""
        done.field, done.note = field, " ".join(n for n in (forced, retyped) if n)
        done.typed = real
        if using is not None:
            self._audit_typed(using, field)
        return done



    async def _type_focused(self, a: dict) -> Noted:
        """(2.0) type_text without a target on a screen whose tree was cut short (runner route version 9) and showed no
        field: the keys go to whatever has the keyboard's focus (a field the agent tapped at its point first), as a
        hardware keyboard's would. Nothing can be read of the field: it isn't cleared first, and a saved account's
        value, which goes only into a field it was checked against (§20), isn't typed at all."""
        text, submit = str(a["text"]), bool(a.get("submit", False))
        if accountlib.mentions(text):
            raise Refused("not_found", "this screen's elements weren't read, so no field can be checked: a "
                                       "saved account's value is typed only into a field Tapwright can read. Nothing "
                                       "was typed.")
        if not text and not submit:
            raise Refused("not_actionable", "no_action: this screen's elements weren't read, so a field "
                                            "can't be cleared. Nothing was typed.")
        await self._last_checks("type_text", a, None, hit=False, police=False)
        self._dry(a, f"Would type {len(text)} character{'s' if len(text) != 1 else ''} into the focused field (this "
                     "screen's elements weren't read).")
        await self.session.type_text(text, replace=False, submit=submit)
        done = Noted(f"Typed {len(text)} character{'s' if len(text) != 1 else ''}"
                     + (" and pressed return" if submit else "") + " into the focused field.")
        if a.get("replace", True) and text:
            done.note = ("the field wasn't cleared first: this screen's elements weren't read, so nothing "
                         "could tell what it held.")
        return done

    def _typing_aim(self, point: tuple[float, float]) -> "Callable[[ui.Element], tuple[float, float]]":
        """The focusing tap of a type_text at a point: at that point, unless something is drawn over it there (the
        keyboard, a bar, a control of the app's)."""
        def aim(field: ui.Element) -> tuple[float, float]:
            # a bar, or a control the app draws over the field there (X's ComposeButtonBarView: Opus review 11)
            over = next((b for b in field.covered_by if ui._holds(b.rect, point)), None)
            if over is not None:
                raise Refused("not_actionable", f"covered: ({round(point[0])}, {round(point[1])}) is under "
                                                f"{over.line}; aim at the part of {field.line(actions=False)} that shows.")
            return point
        return aim

    async def _hosted_proof(self, element: ui.Element, at: tuple[float, float]) -> None:
        """An element in a view another process draws (ui._place_hosted) is touched only where the phone confirms it
        (1.7.8, bug 3: a share sheet's "View More" was listed 531 points off, and the tap opened the video behind with a
        "Tapped" answer). Its frame was put on the screen by its host's place: the runner's hit test (route version 6,
        which proves the element it names) must answer this very element at the touch point, with the frame XCTest
        gives it. One whose place isn't known is never touched by its frame. Nothing to do for an ordinary element."""
        if not element.placed:
            return
        line = element.line(actions=False)
        if element.placed != "moved" or element.raw_rect is None:
            raise Refused("not_actionable", HOSTED_UNKNOWN.format(line=line))
        try:
            hit = await self.session.hit_at(*at)
        except Refused:
            hit = None
        if hit is None or self.session.tw_version < 6 or not _hit_is(hit, element, getattr(self.session, "last_source", None)):
            raise Refused("not_actionable", HOSTED_UNPROVEN.format(line=line, x=round(at[0]), y=round(at[1])))

    def _vault_names(self) -> tuple[str, ...]:
        """The vault entries that aren't accounts (those are listed as accounts), as `home (address)`: names only."""
        return tuple(n for n in self._loaded().entry_names if not n.endswith(" (account)"))

    def _audit_typed(self, using: "accountlib.Typing", field: ui.Element) -> None:
        """§23: a vault value typed is a `vault` line (the entry; the field and where it went, never the value), a
        one-time code an `otp` line (the app that asked; the source, never the code)."""
        if self.audit is None:
            return
        if using.otp_app is not None:
            self.audit.write("otp", app=using.otp_app, detail=f"from {using.field}")
            return
        where = "secure field" if field.secure else "text field"
        self.audit.write("vault", app=self._app_bundle(), name=using.entry, detail=f"{using.field} into {where}")


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
            if area is None and self._tier_now() != "full":
                pass  # (2.0) the screen gave no scroll view to find: the drag goes through the screen, as without a target
            elif area is None:
                raise Refused("not_actionable", f"no_action: Nothing at ({round(x)}, {round(y)}) takes a scroll.")
        else:
            hit = ui.resolve(view.items, target)
            area = ui.scroll_of(view.items, hit)
            if area is None:
                raise Refused("not_actionable", f"no_action: {hit.line(actions=False)} is not inside a scroll view")
            if isinstance(hit, ui.Element) and hit.placed == "unknown":  # (its raw frame may lie in the app's own list)
                raise Refused("not_actionable", HOSTED_UNKNOWN.format(line=hit.line(actions=False)))
            self._touching(hit if isinstance(hit, ui.Element) else None)
        direction = a.get("direction") or (SCROLL_TO[to][0] if to else "down")  # _checked made `to` and it agree
        top, bottom = s.last_insets
        band = (SCROLL_TOP_INSET if top is None else top,
                s.screen[1] - (SCROLL_BOTTOM_INSET if bottom is None else bottom))
        # (`to` without an `amount` drags as far as a drag may: fewer drags to the end)
        x0, y0, x1, y1 = ui.scroll_area_points(direction, area.rect if area else (0.0, 0.0, *s.screen),
                                               a.get("amount", ui.SCROLL_USABLE if to else 0.8), band)
        bounds = area.rect if area else (0.0, 0.0, *s.screen)
        if area is not None and area.placed:
            # (1.7.8, Opus review 5) a list in a view another process draws: dragged only where the phone confirms the
            # drag starts in it, as a tap is; one whose place isn't known, never
            await self._hosted_proof(area, (x0, y0))
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
        swipes, length = 0, abs(y1 - y0)
        try:
            while swipes < MAX_SCROLL_SWIPES:
                seq, since = self._mark()
                # Where the tree shows how far the end lies, the drags that cover it go out one after the other (a drag
                # doesn't slide on, so each moves the content by the finger's distance); the screen is read once after
                # them. Before, each drag was followed by two waits for the screen and a read (about 2 s a drag on the
                # phone: 7 s for a list of 14 rows, 2026-11-13).
                runs = self._drags_to_the_end(direction, bounds, length) if beyond else 1
                for _ in range(max(1, min(runs, MAX_SCROLL_SWIPES - swipes))):
                    if swipes and area is not None and area.placed:
                        # a drag can move or close a hosted view: each later drag is proven where it starts too
                        # (CodeRabbit, PR #18)
                        await self._hosted_proof(area, (x0, y0))
                    await s.scroll(x0, y0, x1, y1)
                    swipes += 1
                    if not to:
                        break
                if not to or swipes >= MAX_SCROLL_SWIPES:
                    break
                if beyond:
                    await self._silent_read(view)  # waits for the screen to be still itself: no settle of its own first
                    if not self._more_beyond(direction, bounds):
                        break  # the tree said there was more past that edge, and now there is none: reached
                    settled = self._mark()[1]
                else:
                    await self._settle(seq, cap=RESOLVE_SETTLE, since=since)
                    settled = self._mark()[1]
                if alike(settled, previous):
                    break  # the swipe left the screen as the one before did: the end of the list
                previous = settled  # (with no stream there is nothing to compare: every swipe is made)
                if loop.time() - began >= SCROLL_CAP:
                    # the screen still changed after every swipe: more may be loading, or the list is long
                    note = f"the {to} wasn't reached in {SCROLL_CAP:g} s; scroll again to go on."
                    break
        except Refused:
            if swipes:  # earlier drags changed the screen even though this tool call failed
                view.moved = True
                self.control.touched()
            raise
        done = Scrolled(f"Scrolled {area.line(actions=False, place=False) if area else 'the screen'} {direction}.")
        done.drags = swipes
        done.note = note
        return done

    def _drags_to_the_end(self, direction: str, bounds: tuple[float, float, float, float], length: float) -> int:
        """How many drags of `length` bring the farthest element the last read has past the area's top (`up`) or
        bottom (`down`) edge to that edge: at least 1."""
        x, y, w, h = bounds
        need = 0.0
        for e in getattr(self.session, "offscreen", []):
            if e.x + e.width <= x or e.x >= x + w:
                continue
            if direction == "up" and e.center[1] < y:
                need = max(need, y - e.y)
            elif direction == "down" and e.center[1] >= y + h:
                need = max(need, e.y + e.height - (y + h))
        return max(1, math.ceil(need / length)) if length > 0 else 1

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

    def _anywhere(self, view, target: ui.Target, together: bool = False) -> ui.Element:
        """The element a target names, on the agent's list or, failing that, among what the last read found off
        screen. Raises as `resolve` does when it is in neither. `together`: always count through both, in reading
        order (a scroll that follows an off-screen element: as it moves, more of its matches come on screen, and an
        `index` counted in the visible ones alone would name another element)."""
        off = getattr(self.session, "offscreen", [])
        if together and target.ref is None and target.point is None:
            return ui.resolve(sorted([*view.items, *off], key=lambda e: (e.y, e.x)), target)
        if target.text is not None:
            # {text} is an exact match first, then a part of one: an element off screen that is exactly the text
            # comes before one on screen that only contains it (seen on the phone 2026-11-10: "Alert" found the
            # state line, which reads "… alert=none …", and the Alert button below the screen was never reached)
            want = ui._norm(target.text)
            if (not any(want in ui._forms(e.label) | ui._forms(e.value) for e in view.items)
                    and any(want in ui._forms(e.label) | ui._forms(e.value) for e in off)):
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

    async def _scroll_to(self, view, target: ui.Target, together: bool = False) -> Scrolled:
        """`scroll` with `to: "target"` (AGENT-TOOLS v1.6): drag the scroll view that holds the element until the
        element is on screen and clear of the bars. The tree gives the place of an element that is off screen, and
        an eased drag doesn't slide on, so each drag covers a known distance; the screen is read after each one,
        and the next drag covers what is left. It stops when the element is clear, when a drag no longer moves it
        (the end of the list), or at the limits `to: "end"` has. A row a lazy list hasn't made yet is `not_found`."""
        s = self.session
        loop = asyncio.get_running_loop()
        began, note, where, clear = loop.time(), "", None, False
        seen: list[tuple[float, ...]] = []  # where the element was before each drag
        try:
            for drags in range(MAX_SCROLL_SWIPES + 1):
                element = self._anywhere(view, target, together)
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
                step = ui.scroll_step(move, (x, y, w, h))
                if element.placed == "unknown":  # (1.7.8, Opus review 5: never by frames that can't be placed)
                    raise Refused("not_actionable", HOSTED_UNKNOWN.format(line=element.line(actions=False)))
                if area is not None and area.placed:
                    await self._hosted_proof(area, (step[0], step[1]))
                await s.scroll(*step)
                seen.append(element.rect)
                await self._silent_read(view)  # once the screen is still: where the element is now
        except Refused:
            if seen:  # only completed drags count, including a refusal in the following read
                view.moved = True
                self.control.touched()
            raise
        line = element.line(ref=bool(element.ref), actions=False, place=False)
        if clear and not seen:  # nothing was dragged: "Scrolled" would read as if the layout had moved
            return Scrolled(f"{line} is already on screen.")
        done = Scrolled(f"Scrolled {where} to {line}." if clear else f"Scrolled {where} toward {line}.")
        done.drags = len(seen)
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
            if isinstance(field, tuple) or field.in_alert or field.placed == "unknown":
                return False  # (an unknown place: _hosted_proof refuses it, nothing is touched; last Opus review, M2)
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
        aimed = self._keyboard_covers(view, target)
        said = await self._dismiss_keyboard_now(view, covered=aimed is not None, aimed=aimed, tool=name)
        if said == NOTE_KEYBOARD_STAYED and aimed is None and self._hints_wanted():
            # (1.9.0 round 2) the target is clear of it: the touch goes on, nothing failed
            return NOTE_KEYBOARD_STAYS.format(what=KEYBOARD_WHAT.get(name, name))
        return said

    def _keyboard_covers(self, view, target: ui.Target) -> "ui.Element | None":
        """The target's element when the keyboard lies over its centre (1.9.0); only then may a Done close it first."""
        try:
            hit = ui.resolve(view.items, target)
        except Refused:
            return None
        if not isinstance(hit, ui.Element):
            return None  # (a point is the agent's own: nothing more than before is done for it)
        cx, cy = hit.center
        covered = any(b.kind == "Keyboard" and b.rect[0] <= cx < b.rect[0] + b.rect[2]
                      and b.rect[1] <= cy < b.rect[1] + b.rect[3] for b in hit.covered_by)
        return hit if covered else None

    async def _press_done(self, view, app: str | None, aimed: "ui.Element | None" = None,
                          tool: str | None = None) -> bool | None:
        """(1.9.0, §30) A Done that closes the keyboard over a covered target, in a form that ignores a touch outside and a
        drag (Wayfare's trip form: its last field is a multiline Notes). Never a key that names a submit: only a button
        named exactly Done, and only (a) an input accessory's or toolbar's button attached to the keyboard, not one of its
        keys, or (b) the keyboard's Done key while the focused field is multiline (a text view). In (b) Done usually adds a
        line or closes the keyboard, but React Native's `submitBehavior="blurAndSubmit"` also runs the field's own submit
        handler, which no tree shows: the answer always says what Done did to the field. Never return, Go, Send, Search,
        Next or Join. In (b), a new line the key added (the field's value is what it was plus a line
        break, read just before and just after) is taken back with one backspace, and the answer says so
        (`view.done_taken_back`). Whether the keyboard closed; None when nothing was pressed (the 1.8.5 ways decide)."""
        s = self.session
        place = view.place[1] if view.place is not None else None
        accessory = self._done_on_keyboard(view)
        if accessory is not None and (app, place, "accessory") not in self._no_done:  # (a)
            await s.tap(ui.Target(x=accessory.center[0], y=accessory.center[1]))
            await self._silent_read(view)
            if not view.read_keyboard:
                return True
            self._no_done.add((app, place, "accessory"))
            return False
        if tool != "tap" or not self._submit_aimed(aimed, view.items, app):
            # (the multiline exception is for a tap only, never a long press: CodeRabbit, PR #22 cycle 2)
            # (lead's ruling on CodeRabbit 4628458079) in a multiline field Done may run the field's own submit handler: it
            # is pressed only when the agent's tap was going to submit anyway (a submit-worded button the floor doesn't hold)
            return None
        field = await self._focused_input(view)
        if field is None or field.type != "TextView":  # (b) only in a multiline field: elsewhere Done may submit
            return None  # (not remembered: another field focused on this screen is judged afresh; CodeRabbit, PR #22)
        before = field.value
        if not isinstance(before, str) or not before or before == field.placeholder:
            # (review 1.9.0 M7) a value that can't be read (empty, or its placeholder): what Done does to it couldn't be
            # checked, so it isn't pressed
            return None
        where = (app, place, field.identifier, field.label)  # a Done key that didn't close it, for this field
        if where in self._no_done:
            return None
        self._touch_in_scope()  # (it goes out with no touch hook: checked here)
        listed = collections.Counter((e.role, e.label, e.identifier) for e in view.items)
        closed = await s.dismiss_keyboard(DONE_KEYS)
        await self._silent_read(view)
        if view.read_keyboard:
            self._no_done.add(where)
        else:
            # (Opus 2 M2) Done in a multiline field may have run the form's own submit: the tap that follows would submit
            # it again. A settle and a read: anything changed beyond the keyboard closing (the screen, the target moved or
            # gone) stops here, and nothing more is touched
            await self.control.sleep(DONE_SETTLE)
            await self._silent_read(view)
            same = collections.Counter((e.role, e.label, e.identifier) for e in view.items) == listed
            target = next((e for e in view.items if (e.role, e.label, e.identifier) == (aimed.role, aimed.label,
                                                                                         aimed.identifier)), None)
            if not same or target is None or target.rect != aimed.rect or view.read_keyboard:
                view.moved = True
                self.control.touched()
                raise Refused("stale", DONE_CHANGED)
        now = next((e for e in view.items if e.role == "input" and (e.identifier, e.label) == (field.identifier, field.label)), None)
        shown = (now or field).line(ref=False, actions=False)
        if now is not None and isinstance(now.value, str) and now.value != before:
            # (the keyboard as read back decides, not the runner's own word: CodeRabbit, PR #22)
            if now.value == before + "\n" and view.read_keyboard and await s.is_focused(now):
                await s.key("backspace")
                await self._silent_read(view)
                view.done_taken_back = (f"the keyboard's Done key added a line break to {shown}; it was taken back with one "
                                        "backspace.")
            else:  # changed some other way: the field is not touched again, and the answer says so plainly
                view.done_taken_back = (f"the keyboard's Done key changed {shown}: a line break may have been added; "
                                        "check its value.")
        return not view.read_keyboard

    def _submit_aimed(self, aimed: "ui.Element | None", items: list, app: str | None) -> bool:
        """Is the tap's own target a submit button (the H1 fix's submit words: Save, Submit, Continue, Confirm, Sign in...)
        that the floor doesn't hold (Send, Post, Buy, Delete...)?"""
        if aimed is None or aimed.role != "button":
            return False
        floor = explorefloor.Floor(safety.DEFAULT, app or "")
        if floor.touch(aimed, items) is not None:
            return False
        return any(explorefloor._first(explorefloor.SUBMIT_WORDS, explorefloor._plain(n)) is not None
                   for n in explorefloor.names_of(aimed))

    @staticmethod
    def _done_on_keyboard(view) -> "ui.Element | None":
        """A button named exactly Done attached to the keyboard (review 1.9.0 M6): `by_keyboard` (ui.read) says it is in the
        keyboard's own window, in its accessory bar or toolbar, whose bottom touches the keys' top (2 pt). That is the
        one edge it is measured against (CodeRabbit, PR #22 cycle 2: the keyboard's frame here holds the suggestion strip,
        so a second check against it could never pass with the strip shown). Never a sheet's or a form's own button."""
        if not any(b.kind == "Keyboard" for e in view.items for b in e.covered_by):
            return None
        for e in view.items:
            if (e.role == "button" and e.label == "Done" and e.enabled and not e.in_alert and e.by_keyboard
                    and not any(b.kind == "Keyboard" for b in e.covered_by)):
                return e
        return None

    async def _focused_input(self, view) -> "ui.Element | None":
        for e in view.items:
            if e.role == "input" and await self.session.is_focused(e):
                return e
        return None

    async def _dismiss_keyboard_now(self, view, covered: bool = False, aimed: "ui.Element | None" = None,
                                    tool: str | None = None) -> str:
        """Close the keyboard the way a person does: one touch where nothing responds, then WebDriverAgent's own
        dismissal if it is still up; neither presses return. NOTE_KEYBOARD when it ended up closed, else
        NOTE_KEYBOARD_STAYED: the answer says so."""
        s = self.session
        top, bottom = s.last_insets
        top = SCROLL_TOP_INSET if top is None else top
        bottom = s.screen[1] - (SCROLL_BOTTOM_INSET if bottom is None else bottom)
        app = self._bundle()
        # what an open layer hides takes touches too, once it closes (an AutoFill menu hid a card that a "blank" touch
        # then opened, 2026-11-12): keep away from those as well
        hidden = [e for e, _ in getattr(s, "behind", []) if e.actions]  # (element, layer) pairs
        blank = ui.blank_point([*view.items, *hidden], s.screen, top, bottom)
        # A touch outside the field is what a person does. A form that ignores it (seen on the phone: the keyboard stayed up
        # over the next field) gets the keyboard reflex (§18.7): a small drag of its scroll area (on-drag dismissal), then the
        # keyboard's own dismiss key, the touch last; the app is remembered, and the way that worked is said.
        order = ("drag", "dismiss", "blank") if app in self._outside_ignored else ("blank", "drag", "dismiss")
        tried = 0
        before = [(e.role, e.label, e.identifier) for e in view.items]
        # a frame that can't be placed (a hosted view's own space) is no screen place: no blank touch or drag is worked
        # out while one is listed, only the keyboard's own dismiss key (last Opus review, M2)
        unplaced = any(e.placed == "unknown" for e in [*view.items, *hidden])
        for step in order:
            if not view.read_keyboard:
                break
            if unplaced and step != "dismiss":
                continue
            how = None
            if step == "blank" and blank is not None:
                await s.tap(ui.Target(x=blank[0], y=blank[1]))
                await self._silent_read(view)
                if view.read_keyboard and app is not None:
                    self._outside_ignored.add(app)
            elif step == "drag" and (drag := ui.dismiss_drag([*view.items, *hidden], s.screen, top, bottom)) is not None:
                # many lists close the keyboard when dragged (keyboardDismissMode on-drag, as a person flicks a list
                # down to hide it); a short pull down on passive content moves it at most a little, and back. It never
                # starts or ends on a control (1.7.8, X item 1: one opened X's photo picker)
                await s.swipe(*drag, duration_ms=250)
                await self._silent_read(view)
                how = "dragged the form a little"
            elif step == "dismiss" and ((covered and self._hints_wanted()) or app not in self._no_dismiss):
                # (1.9.0) a Done first, for a covered target; never with --no-hints (1.8.5's behaviour). Whatever it did,
                # WebDriverAgent's plain dismissal is still tried once where it hasn't failed (review 1.9.0 L2)
                done = await self._press_done(view, app, aimed, tool) if covered and self._hints_wanted() else None
                if done:
                    how = "the keyboard's Done key"
                elif view.read_keyboard and app not in self._no_dismiss:
                    # WebDriverAgent's dismissal looks for a key that hides the keyboard; an iPhone keyboard often has
                    # none, and the search takes about 4 s: once it failed in an app, it isn't tried there again this session
                    self._touch_in_scope()  # (it goes out with no touch hook: checked here)
                    if await s.dismiss_keyboard():
                        await self._silent_read(view)
                        how = "the keyboard's own dismiss key"
                    elif app is not None:
                        self._no_dismiss.add(app)
                elif done is None:
                    continue
            else:
                continue
            if not view.read_keyboard and how is not None and (tried or order[0] != "blank"):
                if (earned := _REFLEXED.get()) is not None:
                    earned.append(reflex.keyboard(how))
            tried += 1
            if self._reflex_opened(before, view.items):
                # (1.7.8, X item 1) the reflex's own touch opened something (X's photo picker took the place of the
                # keyboard): the touch the agent asked for would land on that, so nothing more goes out
                view.moved = True
                self.control.touched()
                raise Refused("stale", "closing the keyboard opened another screen; nothing else was touched: look "
                                       "again.")
        return NOTE_KEYBOARD_STAYED if view.read_keyboard else NOTE_KEYBOARD

    @staticmethod
    def _reflex_opened(before: list[tuple[str, str, str]], after: list[ui.Element]) -> bool:
        """Did a touch meant only to close the keyboard bring another screen? Fewer than half of what was listed
        (role, label, identifier) is still listed. Closing the keyboard alone shows more, and takes nothing away."""
        if len(before) < REFLEX_SCREEN_MIN:
            return False
        now = {(e.role, e.label, e.identifier) for e in after}
        return sum(key in now for key in before) < len(before) / 2

    def _offscreen_under_keyboard(self, view, target: ui.Target) -> "ui.Element | None":
        """(CodeRabbit, PR #22) The very match the target names (its index too) lies over the keyboard's frame now: only
        then may a Done close it first. A match clipped by a short list above the keyboard is not covered by it."""
        keyboard = next((b for e in view.items for b in e.covered_by if b.kind == "Keyboard"), None)
        if keyboard is None:
            return None
        try:
            hit = ui.resolve(getattr(self.session, "offscreen", []), target)
        except Refused:
            return None
        if not isinstance(hit, ui.Element):
            return None
        cx, cy = hit.center
        kx, ky, kw, kh = keyboard.rect
        return hit if kx <= cx < kx + kw and ky <= cy < ky + kh else None

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
            title, buttons, _message = view.alert_now
            alert = " ".join([ui.quoted(title), *(f"[{b}]" for b in buttons)])
            raise Refused("not_actionable", f"behind_alert: an alert covers it: {alert}; answer it with alert, "
                                            + ("or pass force: true." if force else "or ask the user."))

    async def _lift_from_behind(self, target: ui.Target, view) -> bool:
        """A target the tree put behind a layer, which the runner's hit test (route version 5) says a touch at its centre
        lands on, itself or inside it: the layer was the tree's guess (seen on the phone 2026-11-14: Calendar's search
        results, a full-screen container that starts like a sheet's backdrop, put the search bar's Close behind it). It is
        listed again, with a ref of its own, and the touch goes through every check as any listed element's does. An
        ancestor, another app's element or no answer leaves it behind."""
        behind = getattr(self.session, "behind", [])
        try:
            hit_element = ui.resolve([e for e, _ in behind], dataclasses.replace(target, index=0))
        except Refused:
            return False
        if not isinstance(hit_element, ui.Element):
            return False
        hit = await self.session.hit_at(*hit_element.center)
        app = self._app_bundle()
        if hit is None or (hit.bundle and app and hit.bundle != app):
            return False
        if ui.hit_relation(getattr(self.session, "last_source", None), hit_element, hit) not in ("same", "inside"):
            return False
        trace.note("hit", "lifts an element from behind a guessed layer")
        lifted = copy.copy(hit_element)
        view.top_ref = max(view.top_ref, changes.highest(self.session.items)) + 1
        lifted.ref = f"e{view.top_ref}"
        self.session.items = [*self.session.items, lifted]
        self.session.behind = [(e, f) for e, f in behind if e is not hit_element]
        return True

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

    def _other_app_shown(self) -> bool:
        """Personal mode only: is the app of the last read not one of the given apps? Then its labels are not shown
        (another app may have come to the front since the check before the call)."""
        given = _GIVEN.get()
        return given is not None and self._app_bundle() not in given

    def _closest(self, view, target: ui.Target | None, message: str) -> str:
        """The `Closest:` line of a `not_found` for an id, label or text (AGENT-TOOLS §16.2), or "". An `index` past
        the matches already has them: it adds nothing."""
        if target is None or target.point is not None or target.ref is not None or "out of range" in message:
            return ""
        if self._other_app_shown():
            return ""
        hidden = {id(e) for e, _ in getattr(self.session, "behind", [])}
        off = [] if ui.USERS_EYES.get() else [e for e in getattr(self.session, "offscreen", []) if id(e) not in hidden]
        return closest.find(target, view.items, off, self.session.screen)

    async def _reach(self, view, client_id: str, name: str, args: dict, target: ui.Target, dry: bool) -> ui.Target | None:
        """A target the tree has but the screen doesn't (AGENT-TOOLS §16.2): `tap`, `long_press`, `toggle` and
        `type_text` scroll it into view the way `scroll` with `to: "target"` does, then act, in one call. Under the skip
        floor (explore's and a repair's moves) the dispatch gate judges the target before the scroll, and again the
        element that came on screen as the touch goes out; an agent's own call has no gate (1.7.5). Where no scroll can bring it on screen, or it isn't in the tree at all (a lazy row), it is refused as
        before: `off_screen`, or `not_found` further on. Returns the target to act on when it scrolled (the element
        that came on screen, by ref), else None."""
        if name not in REACH_TOOLS:
            self._refuse_off_screen(target, name)
            return None
        s = self.session
        try:
            # the element _scroll_to will bring on screen: the same lookup, `index` counted through both lists
            hit = self._anywhere(view, target)
        except Refused as e:
            if e.code == "not_found":
                return None  # not off screen either: the action says not_found
            raise
        if not isinstance(hit, ui.Element) or any(hit is e for e in view.items):
            return None  # (on screen already: the action touches it where it is)
        self._floor_gate(name, args, hit)  # (no scroll toward what the floor holds: review R180 L6)
        said = ""
        shown = hit.line(ref=bool(hit.ref), actions=False, place=False)
        area = ui.scroll_holding(view.items, hit, s.screen)
        why = ""
        if area is None:
            why = "it isn't inside a scroll view, so no scroll brings it on screen."
        else:
            view_rect = self._scroll_view_rect(area)
            if view_rect[3] <= 0:
                why = "the scroll view that holds it lies under the bars."
        if why:
            raise Refused("not_actionable", f"off_screen: {hit.line(ref=False, actions=False)} is off screen; "
                                            f"{why} {NOTHING_SCROLLS}")
        if dry:
            dx, dy = ui.scroll_move(hit.rect, view_rect)
            x, y = hit.center[0] + dx, hit.center[1] + dy
            verb = {"tap": "tap", "long_press": "long-press", "toggle": "toggle"}.get(name)
            if verb is None:
                n = len(args["text"])
                self._dry(args, f"Would scroll to it, then type {n} character{'s' if n != 1 else ''} into {shown}.")
            self._dry(args, f"Would scroll to it, then {verb} {shown} at {round(x)},{round(y)}.{said}")
        # `hit` was counted through the screen and what lies past it, so the scroll and the look after it count the
        # same way. An `index` alone doesn't keep the element: as a list scrolls it drops and adds rows, and other
        # matches can come and go, so the count can name another element afterwards (Codex, PR #6 cycles 3 to 8, which
        # also showed that where the matches lie after the scroll can't prove which one it is). So an indexed target
        # follows the element's own identifier when nothing else in the tree has it; without one, the element is
        # scrolled into view and not touched: the agent's next look shows it with a ref.
        follow = target
        if target.index is not None:
            tree = [*view.items, *getattr(s, "offscreen", [])]
            if hit.identifier and sum(e.identifier == hit.identifier for e in tree) == 1:
                follow = ui.Target(id=hit.identifier)
        done = await self._scroll_to(view, follow, together=True)
        if done.drags:
            # This preparatory input already moved the screen. Any later target check or action
            # can refuse, which skips Controller.agent's normal generation increment.
            view.moved = True
            self.control.touched()
        if target.index is not None and follow is target:
            raise Refused("stale", f"scrolled {hit.line(ref=False, actions=False)} into view, but index "
                                   f"{target.index} can't be sure to name it after a scroll (it has no identifier of "
                                   f"its own, and the list's matches move and change as it scrolls), so it wasn't "
                                   f"touched. Look, then target it by its ref.")
        # The element the target names now: on screen after the scroll, it is the one to touch. The action gets its
        # ref, not the target again: an `index` counted in the visible matches alone would point at another element.
        try:
            final = self._anywhere(view, follow, together=True)
        except Refused:
            final = None
        if not isinstance(final, ui.Element) or not any(final is e for e in view.items):
            note = getattr(done, "note", "") or "it is as far into view as it gets."
            raise Refused("not_actionable", f"off_screen: {hit.line(ref=False, actions=False)} is off screen; "
                                            f"scrolling didn't bring it on screen: {note} {NOTHING_SCROLLS}")
        if hit.identifier and (follow is not target or target.id == hit.identifier):
            # Followed by its real identifier: what is touched must still carry it, and nothing else may. An `id`
            # lookup falls back to WebDriverAgent's raw name when no identifier matches, so with the element gone
            # (an app refresh during the scroll) another element whose label mirrors that word would be found
            # (Codex, PR #6 cycle 9).
            tree = [*view.items, *getattr(s, "offscreen", [])]
            if final.identifier != hit.identifier or sum(e.identifier == hit.identifier for e in tree) != 1:
                raise Refused("stale", f"{hit.line(ref=False, actions=False)} isn't the element found after the "
                                       f"scroll (its identifier \"{ui.shown(hit.identifier)}\" is gone or no longer its own), "
                                       f"so nothing was touched. Look, then target what is there.")
        if self._policed is not None and self._police_key(final) != self._police_key(hit):
            self._policed = None  # (a person's OK was for the element checked first; the touch's own checks judge this one)
        self._scrolled_first.add(client_id)
        return ui.Target(ref=final.ref) if final.ref else target

    def _scroll_view_rect(self, area: ui.Element) -> tuple[float, float, float, float]:
        """The part of a scroll view between the bars: where its content is seen and dragged."""
        s = self.session
        top, bottom = s.last_insets
        band = (SCROLL_TOP_INSET if top is None else top, s.screen[1] - (SCROLL_BOTTOM_INSET if bottom is None else bottom))
        x, y, w, h = area.rect
        return x, max(y, band[0]), w, min(y + h, band[1]) - max(y, band[0])

    def _refuse_off_screen(self, target: ui.Target, name: str) -> None:
        """A target on no listed element but on one the last read found past the screen (or its scroll view's
        edge): `off_screen`, not `not_found`. A touch can't reach it, so `force` doesn't either. (Only for the tools
        that don't scroll by themselves: for them a scroll still brings it on screen, and the message says so.)"""
        try:
            hit = ui.resolve(getattr(self.session, "offscreen", []), dataclasses.replace(target, index=0))
        except Refused:
            return
        if isinstance(hit, ui.Element):
            raise Refused("not_actionable", f"off_screen: {hit.line(ref=False, actions=False)} is off screen; "
                                            f"a {name} aims at a point of its target: bring it on screen first "
                                            '(`scroll` with `to: "target"` and this target), or use `tap`, which scrolls to it.')

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

    @staticmethod
    def _control_at(point: tuple[float, float], items: list[ui.Element]) -> ui.Element | None:
        """The front-most element under the point that takes a touch (a scroll view's drag doesn't count), or None."""
        x, y = point
        return next((e for e in reversed(items) if e.actions and e.role != "scroll"
                     and e.x <= x < e.x + e.width and e.y <= y < e.y + e.height), None)

    def _point_kept(self, view, point: tuple[float, float], before: ui.Element | None) -> None:
        """After the keyboard reflex touched the screen (a blank touch, a drag of the form): the agent's point must hold
        what it held when the agent aimed it, by the read the reflex made. A control the reflex moved under the point
        (a drag scrolled a button there) is stale, so a passive touch never becomes a control's (CodeRabbit, PR #18
        cycle 2). `force` touches there anyway."""
        now = self._control_at(point, self.session.items)
        if now is None or (before is not None and _same_control(before, now)
                           and _near_rect(before.rect, now.rect)):
            return
        view.moved = True
        self.control.touched()
        x, y = (round(c) for c in point)
        raise Refused("stale", f"closing the keyboard moved {now.line(ref=False, actions=False, place=False)} under "
                               f"({x}, {y}); nothing else was touched: look again, or pass force: true to touch there "
                               "anyway.")

    def _element_at(self, point: tuple[float, float], items: list[ui.Element] | None = None) -> ui.Element | None:
        """The front-most element of the agent's list (or of `items`) whose rect contains the point (a later one is
        drawn over an earlier one), or None."""
        x, y = point
        return next((e for e in reversed(self.session.items if items is None else items)
                     if e.x <= x < e.x + e.width and e.y <= y < e.y + e.height), None)













    def _book(self) -> "grantlib.Book | None":
        """Legacy grant files are not consulted for device actions."""
        return None

    def _book_of(self, path: str) -> "grantlib.Book":
        """The Book of one policy file, made once."""
        book = self._books.get(path)
        if book is None:
            book = self._books[path] = grantlib.Book(path, self.audit, now=self._grant_clock)
            try:
                book.prune()
                if self.audit is not None:
                    self.audit.prune()
            except OSError:
                pass
        return book

    def _route_books(self) -> "list[grantlib.Book]":
        """Legacy grant files no longer constrain route edits or execution."""
        return []


    def _give_back(self, said: GrantCall, e: Refused) -> None:
        """A call a grant covered that was refused before any touch went out (off screen, stale, covered: §19.4 "a use
        proven not delivered gives its slot back", review 1.7 A-L1): its use is given back, and its log line names no
        grant. A refusal that may come after a touch (a timeout, a dropped line) keeps it: the safe side."""
        if said.claimed is None or said.touched or e.code not in BEFORE_TOUCH:
            return
        book, use = said.claimed
        try:
            book.release(use)
        except Exception as x:  # (kept counted: the safe side)
            self.log(f"grants: {type(x).__name__}: {x}")
            return
        said.claimed, said.name, said.use, said.line = None, None, None, None

    def _grants_revoked(self) -> list[str]:
        """The grants in force when the running batch began that are revoked now: it stops between moves (§19.5). A
        `--revoke-all` after it began stops it too, whatever it began with (review 1.7 A-L2: none, or ones revoked
        already)."""
        held = _RUN_GRANTS.get()
        if held is None:
            return []
        try:
            gone = held[0].revoked_since(held[1]) if held[1] else []
            return gone or (sorted(held[1]) or ["every grant"] if held[0].revoked_after(held[2]) else [])
        except Exception:
            return sorted(held[1])

    def _grants_run(self) -> "contextvars.Token | None":
        """At the start of the outermost batch, drive or replay: the grants in force then, for _grants_revoked."""
        if _RUN_GRANTS.get() is not None or (book := self._book()) is None:
            return None
        try:
            return _RUN_GRANTS.set((book, book.active(), time.time()))
        except Exception:
            return None


    def _police_key(self, element: ui.Element) -> tuple[str, ...]:
        """The element in this app: an OK for "Delete" in one app is no OK for another app's (safety review M1). Its
        words are part of it even when it has an identifier: a control whose id stays while its words change ("Delete
        draft" to "Delete account") is another touch, and an OK for the first is none for it (Codex, PR #9 cycle 1)."""
        return (self._app_bundle() or "", "id", element.identifier or "", "role", element.role,
                "label", element.label, "text", element.text)




    async def _ask_offer(self, owner: str, kind: str, message: str) -> str | None:
        """One question of the grant dialog to the owner's host, waited on up to CONFIRM_TTL (the wait lets go of the
        phone and doesn't count toward a batch's own limit). The answer's word (`allow`/`decline`, `yes`/`no`), or None
        when nobody answered (the dialog is closed)."""
        self._need_seq += 1
        offer = Offer(f"g{self._need_seq}-{secrets.token_hex(4)}", owner, kind)
        self._offers[offer.id] = offer
        self.dialog_channel(owner).push({"type": "ask", "id": offer.id, "kind": kind, "message": message,
                                         "choices": dialog.CHOICES[kind]})
        began = self.control.clock()

        async def waiting() -> None:
            while offer.answer is None and self.control.clock() < began + CONFIRM_TTL:
                try:
                    await asyncio.wait_for(offer.answered.wait(), CONFIRM_TICK)
                except asyncio.TimeoutError:
                    pass
        try:
            await self._aside(waiting)
        finally:
            self.held(self.control.clock() - began)
            self._offers.pop(offer.id, None)
            if offer.answer is None:
                self.dialog_channel(owner).push({"type": "close", "id": offer.id})
        return offer.answer


    async def _aside(self, wait: Callable[[], Awaitable[None]]) -> None:
        """Inside an agent's action: `wait()` for a person's answer without holding the phone (Controller.aside), so a
        pause from the live view, or a person's own input, isn't held up behind a wait of up to 120 s (Codex, PR #9
        cycle 1). What the action set on the session for its touches (Tapline's notice, the banner guard, the runner's
        app check) is set aside meanwhile, so another action's or a person's input isn't taken for this one's, and put
        back once the turn is taken back. Outside an agent's action, just `wait()`."""
        s = self.session
        if not self.control.holds_turn():
            await wait()
            return
        kept = (s.touch_hook, s.banner_guard, s.front_apps, getattr(s, "before_input", None),
                getattr(s, "execution_guard", None))
        s.touch_hook, s.banner_guard, s.front_apps, s.before_input, s.execution_guard = None, False, None, None, None
        try:
            await self.control.aside(wait)
        finally:
            if self.control.holds_turn():  # (when the turn wasn't taken back, another action may hold it now)
                s.touch_hook, s.banner_guard, s.front_apps, s.before_input, s.execution_guard = kept


    def dialog_channel(self, client_id: str) -> dialog.Channel:
        """What this agent's host is asked: made when first needed."""
        return self._channels.setdefault(client_id, dialog.Channel())

    async def dialog_poll(self, client_id: str, since: int, wait: float) -> dict:
        """The host's events for an agent after `since` ({"seq", "events"}), waiting up to `wait` seconds (dialog.py)."""
        return await self.dialog_channel(client_id).poll(since, wait)

    def dialog_answer(self, client_id: str, ask_id: str, answer: object) -> bool:
        """The host's dialog answered for an agent. Only `allow` allows; anything else refuses. False when there is no
        such open request for that agent (an unknown, answered, expired or another agent's id): nothing happens. For a
        "needs a person" ask (§11.5) the answers are `done` and `cant`; anything else is `cant`."""
        offer = self._offers.get(ask_id) if isinstance(ask_id, str) else None
        if offer is not None:  # a question of the grant dialog (§19.6): only the agent whose call waits answers it
            if offer.owner != client_id or offer.answer is not None:
                return False
            yes = "allow" if offer.kind == dialog.GRANT else "yes"
            offer.answer = yes if answer == yes else ("decline" if offer.kind == dialog.GRANT else "no")
            offer.answered.set()
            return True
        need = self._needs.get(ask_id) if isinstance(ask_id, str) else None
        if need is not None:
            if need.owner != client_id or need.state == "cant":
                return False
            need.state = "done" if answer == "done" else "cant"
            need.answered.set()
            return True
        request = self._confirms.get(ask_id) if isinstance(ask_id, str) else None
        if request is None or request.owner != client_id or not request.dialog:
            return False
        return self._resolve_confirm(request, answer == "allow", "chat")

    async def _health_sheet_gone(self) -> bool:
        """Is iOS's Health access sheet off the screen now? (A read through the queue, so a person's input isn't held up.)"""
        return not _health_sheet((await self._read(self.session.refresh)).items)

    def _phone_moment(self, name: str) -> needs.Moment | None:
        """The moment the phone is in that this call can't get past (§11.5): the XCTest passcode prompt refuses every
        call; a locked phone refuses the actions (a look still reads the lock screen). None: nothing for a person."""
        moment = needs.phone_moment(self.state, self.detail)
        if moment is needs.PASSCODE_PROMPT or (moment is needs.UNLOCK and name in ACTIONS and name != "look"):
            return moment
        return None

    @staticmethod
    def _top_level() -> bool:
        """Is the call running now one of its own, not a step of a `run`, `drive`, `replay`, route or journey moment?"""
        return BATCH.get() is None and not _NESTED.get() and not _JOURNEY_OWN.get() and SCRIPT.get() is None

    def _unlocks_itself(self, client_id: str, name: str) -> bool:
        """May this call unlock the locked phone with the vault's passcode by itself (§18.6, §19.5; review 1.7, H1)? Only
        with `tapwright serve --phone` (a phone the user dedicates to automation), at the start of a new top-level call
        that needs the phone, outside a journey, with a passcode in serve's own vault. Everywhere else a call answers
        `locked`, and the agent may call `unlock` (1.7.8, the owner's decision of 2026-11-18: every mode, top-level
        only) or ask a person. With a passcode saved, Pause in the live view is the owner's stop, not the lock."""
        if not getattr(getattr(self, "ceiling", None), "phone", False) or client_id in self._journeys:
            return False
        if needs.phone_moment(self.state, self.detail) is not needs.UNLOCK:
            return False
        if not ((name in ACTIONS and name != "look") or name in UNLOCK_AT_START):
            return False
        return self.passcode_for() is not None

    async def _hold_for_phone(self, client_id: str, name: str, *, dry: bool = False, home: bool = False) -> None:
        """Before a call. A step of a running `run`, `drive`, `replay`, route or journey moment never unlocks and never
        waits out a lock: a lock there ends it with `locked` (the step's own refusal; §19.5). The agent may then call
        `unlock` as a call of its own (1.7.8), or ask a person. (iOS's XCTest prompt is no stop: a step still asks a person to
        enter the passcode there, as in 1.6.) A top-level call to `serve --phone` unlocks a locked phone with the vault's
        passcode (unlock.py; never on the XCTest prompt). Else, for an agent whose host has a dialog: if the phone is
        locked or asking for its passcode, ask a person and wait for it to clear (the daemon looks at the phone again by
        itself). If it doesn't clear, the call answers `locked`: the unlock's own words when it was tried (`the
        passcode in the file was refused`), else its usual ones."""
        if dry:
            return  # inspecting an action never types a passcode or asks a person to unlock the phone
        if not self._top_level():
            moment = self._phone_moment(name)
            if moment is needs.PASSCODE_PROMPT and _DIALOG.get() is not None:
                async def prompt_gone() -> bool:
                    return self._phone_moment(name) is None
                await self._hold_for_person(moment, prompt_gone, nudge=self._asked_for)
            return
        tried: unlocklib.Result | None = None
        if self._unlocks_itself(client_id, name):
            tried = await self.unlock_phone()
            if tried.ok or needs.phone_moment(self.state, self.detail) is None:
                return
            if tried.code in ("busy", "paused"):
                raise Refused(tried.code, tried.message)
        moment = self._phone_moment(name)
        if home and moment is needs.UNLOCK:
            # (1.7.8) iOS reports Notification Center pulled down as locked: press_button home decides from a fresh tree
            # (_home_over_center) at once, instead of asking a person to unlock a phone that may not be locked
            return
        if moment is not None and _DIALOG.get() is not None:
            async def cleared() -> bool:
                return self._phone_moment(name) is None
            if await self._hold_for_person(moment, cleared, nudge=self._asked_for):
                return
        if tried is not None and tried.message:
            raise Refused("locked", tried.message)

    async def _hold_for_person(self, moment: needs.Moment, cleared: Callable[[], Awaitable[bool]],
                               every: float = CONFIRM_TICK, nudge: Callable[[], object] | None = None) -> bool:
        """A call that can't go on without a person (§11.5), for an agent whose host can show a dialog: ask it
        (`Needs a person: unlock the phone. I'll go on by myself when it's unlocked.`), then wait up to 120 s,
        looking every `every` seconds at `cleared()` (an async check of the phone). True when the moment cleared: the
        call goes on. False when they said `Can't now`, or the limit came: the call answers its usual error. The dialog
        is closed by this, whichever way it ended; the wait doesn't count toward a batch's 120 s. `nudge` is called
        when they say `Done` and every few seconds, to make the daemon look at the phone again. No dialog: False."""
        caller = _DIALOG.get()
        if caller is None:
            return False
        self._need_seq += 1
        need = Need(f"n{self._need_seq}-{secrets.token_hex(4)}", caller)
        self._needs[need.id] = need
        self.dialog_channel(caller).push({"type": "ask", "id": need.id, "kind": dialog.NEEDS,
                                          "message": needs.message(moment), "choices": dialog.CHOICES[dialog.NEEDS]})
        began = self.control.clock()
        looked = float("-inf")
        try:
            while True:
                if await cleared():
                    return True
                now = self.control.clock()
                if need.state == "cant" or now >= began + CONFIRM_TTL:
                    return False
                if nudge is not None and (need.state == "done" or now - looked >= NUDGE_EVERY):
                    nudge()
                    looked = now
                if need.state == "done":  # they say it is done: look at once, and go on waiting if it isn't
                    need.state = "pending"
                    need.answered.clear()
                    continue
                try:
                    await asyncio.wait_for(need.answered.wait(), every)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.held(self.control.clock() - began)
            self._needs.pop(need.id, None)
            self.dialog_channel(caller).push({"type": "close", "id": need.id})

    def _answer_confirm(self, message: dict) -> None:
        """A live view's answer to a request. The first one wins; an unknown, answered or expired id is ignored."""
        request = self._confirms.get(message.get("id")) if isinstance(message.get("id"), str) else None
        if request is not None:
            self._resolve_confirm(request, message.get("allow") is True, "the live view")

    def _resolve_confirm(self, request: Confirm, allow: bool, origin: str) -> bool:
        """The answer to a request, from the live view or the host's dialog: the first wins. Every viewer closes its
        prompt, and the host's dialog is taken down when the live view answered. Whether this answer counted."""
        now = self.control.clock()
        if request.state != "pending" or request.until < now:
            return False
        request.state, request.origin = "allowed" if allow else "declined", origin
        if self.audit is not None:  # §23 approval: the policy word, never more of the app's text
            word = (request.message or {}).get("word")
            self.audit.write("approval", app=(request.key[0] if request.key else "") or None,
                             name=word if isinstance(word, str) else None,
                             detail=f"allowed by a person (in {origin})" if allow else "declined")
        # an allow is good for one call within CONFIRM_TTL; a decline holds for DECLINE_HOLD, then may be asked again
        request.until = now + (CONFIRM_TTL if allow else DECLINE_HOLD)
        request.answered.set()
        closed = {"type": "confirm_closed", "id": request.id, "allow": allow}
        self.broadcast({**closed, "by": "chat"} if origin == "chat" else closed)
        if request.dialog and origin != "chat":
            self.dialog_channel(request.owner or "").push({"type": "close", "id": request.id})
        return True

    def _app_bundle(self) -> str | None:
        """The bundle id of the app the last read found in front, for the per-app policy."""
        return (self.session.app or (None, None))[1]

    def _read_bundle(self) -> str | None:
        """The app the newest read names (`Session.read_app`: a live read's root, which `session.app` doesn't follow), else
        the last snapshot's (_app_bundle). What the skip floor judges a touch against (CodeRabbit, PR #20)."""
        read = getattr(self.session, "read_app", None)
        return read[1] if isinstance(read, tuple) and read[1] else self._app_bundle()

    def _touch_point(self, element: ui.Element) -> tuple[float, float] | None:
        """Where a finger would touch an element a bar (or the keyboard) is drawn over its centre: the middle of
        the part that shows after every overlapping bar is cut away, nearest the centre. None when no bar
        covers the centre. Refuses when too little shows."""
        centre = element.center
        # Sibling rectangles are not z-order evidence: button text/icons overlap their parent.
        bars = tuple(b for b in element.covered_by if b.kind in ui.COVERING_BARS)
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
            hint = KEYBOARD_HINT_190 if self._hints_wanted() else KEYBOARD_HINT
            raise Refused("not_actionable", f"covered: {line} is under the keyboard, and this app doesn't close it on a "
                                            f"touch outside the field; {hint}")
        if over[0].kind in ui.COVERING_BARS:
            raise Refused("not_actionable", f"covered: {line} is covered by {over[0].line}; {SCROLL_HINT}")
        raise Refused("not_actionable", f"covered: {line} is covered by {over[0].line}; move or close it first.")



    async def _last_checks(self, tool: str, a: dict, element: "ui.Element | None", *, hit: bool = True, police: bool = True,
                           aim: "Callable[[ui.Element], tuple[float, float] | None] | None" = None,
                           again: "Callable[[], ui.Element] | None" = None, bundle: str | None = None,
                           ) -> "tuple[ui.Element | None, tuple[float, float] | None, str]":
        """Choose a touch point and retain execution ownership until dispatch.

        Accessibility hit identity is incomplete on real devices. An unanswered or
        unrelated hit must not invent a covered-control error or an authorization gate.
        The one exception is `explore`'s and a repair trial's own moves: their skip floor (_floor_gate).
        """
        self._floor_gate(tool, a, element, bundle)
        point = aim(element) if element is not None and hit and aim is not None else None
        return element, point, ""

    def _floor_gate(self, tool: str, a: dict, element: "ui.Element | None", bundle: str | None = None) -> None:
        """The ONE dispatch gate of `explore`'s and a repair trial's moves (§25.5, §26.4; Tapline's lesson: checked on the
        ACTUAL recipient, after resolution and scroll). Nothing when no floor is set (an agent's own call, 1.7.5). Else the
        touch is refused `not_allowed: needs_confirmation` (`explore` and `check` skip it; a dry run is refused the same way,
        so a dry run says what the real touch would meet) when what it goes to is held: an element (or what lies in it, or
        over its centre) with a floor word, a screen the floor holds (a system sheet, Settings, a protected title), a field
        never typed into, another app than the floor's, a point or a swipe (no element to judge), or an alert button other
        than the one `safe_button` picks. `force` changes nothing here."""
        floor = _TOUCH_FLOOR.get()
        if floor is None:
            return

        def held(what: str, word: str | None = None) -> None:
            said = f' (policy word "{word}")' if word else ""
            raise Refused("not_allowed", f"needs_confirmation: {ui.clip(self.scrub_everywhere(what))} is on the skip "
                                         f"floor{said}; explore and repair touch nothing it holds, and ask nobody.")
        if tool == "alert":
            if a.get("action") == "text":
                return
            shown, name = self.session.last_alert, a.get("name")
            if shown is not None and not self._alert_ours(floor.app, shown, bundle):
                held("an alert over another app")  # (review R180 L1: nothing is touched while another app is in front)
            if shown is None or not isinstance(name, str) or explorefloor.safe_button(shown, floor) != name:
                held(f"the alert button {name!r}" if isinstance(name, str) else "an alert answered without a button name")
            return
        if tool == "back":  # (review R180 H1) a way back is a move too: never in another app than the floor's
            if (bundle or self._read_bundle()) != floor.app:
                held("back in another app")
            return
        if tool == "swipe":
            held("a swipe")
        if element is None:
            target = a.get("target")  # (parsed by _checked: a ui.Target)
            if isinstance(target, ui.Target) and target.point is not None and tool in TOUCH_FLOOR_TOOLS:
                held(f"the point ({round(target.point[0])}, {round(target.point[1])})")
            return
        front = bundle or self._read_bundle()  # (the live read's app: a refresh leaves session.app as the last look had it)
        if front != floor.app:
            held(f"{element.role} in another app")
        tree = getattr(self.session, "last_tree", None)
        if tree is not None and (screen := floor.screen(tree, front)) is not None:
            held("this screen", screen.word)
        items = [e for e in self.session.items if not e.in_alert]
        if tool == "type_text" and explorefloor.never_typed(element):
            held(f"{element.role} {element.label or element.identifier}")
        if (hit := floor.touch(element, items)) is not None:
            held(f"{element.role} {explorefloor.name_of(element)}", hit.word)
        if _EXPLORING.get() and tool != "type_text" and (hit := floor.may_submit(element, items)) is not None:
            held(f"{element.role} {explorefloor.name_of(element)}", hit.word)  # (§25.7: a form's submit, unless its id is listed)

    def _alert_ours(self, app: str, alert: ui.Alert, bundle: str | None = None) -> bool:
        """Is this alert one explore (or a repair) in `app` may answer: shown over that app, or its own permission prompt (iOS
        draws it in SpringBoard; its title names the app)? Another app's alert (Safari's, after a link) is never answered
        (review R180 L1). `bundle`: the app in front, when the caller knows it; else the newest read's."""
        front = bundle or self._read_bundle()
        if front == app:
            return True
        name = self.session.app_names.get(app) or ""
        return front in (SPRINGBOARD, PROMPT_FRONT, None) and bool(name) and name in alert.title

    def _floor_pregate(self, tool: str, a: dict, target: ui.Target) -> None:
        """The gate on the element a target names, before anything is touched for it (a keyboard reflex, a reach scroll):
        on the screen or past its edge. A target that doesn't resolve to one element is left to the action to refuse."""
        s = self.session
        try:
            found = ui.resolve([*s.items, *self._off_screen_now()], target)
        except Refused:
            return
        if isinstance(found, ui.Element):
            self._floor_gate(tool, a, found)

    async def _press(self, target: ui.Target, hold_ms: int, proven: bool = False) -> str:
        """One touch on the phone; twice, DOUBLE_TAP_GAP apart, for a journey's `double_tap` stress moment (§17.4).
        `proven`: the point target is an element's visible part, proven like the element (_hosted_proof)."""
        double = _DOUBLE.get()
        was = self._double_aim(target, self.session.items) if double else None
        done = await self.session.tap(target, hold_ms=hold_ms)
        if double:
            await asyncio.sleep(DOUBLE_TAP_GAP)
            # what is there now: the first touch may have put another screen under the finger (read again, police again).
            # The first touch may also have opened another app (a link into Safari): the app in front is read again, and in
            # personal mode a second touch is made only on a given app's screen; the per-app policy is that of the app
            # really in front, since Session.refresh leaves `session.app` as the last look had it (Codex, PR #7 cycle 1)
            try:
                front = await self.session.foreground_bundle_id()  # (not _read: this runs inside the touch's own turn in the queue)
                given = _GIVEN.get()
                if given is not None and front not in given:
                    raise Refused("not_allowed", _outside(front, self.session.app_names.get(front or ""), ", ".join(given.values())))
                again = await self.session.refresh()
                items = again.items
                # the app in front, as just asked, and this read: what the second touch's own scope check looks at
                self._read_root = ((self.session.app_names.get(front or "", ""), front or ""), again.alert is not None)
                try:
                    under = self._double_aim(target, items)
                except Refused as e:
                    if e.code != "not_found":
                        raise
                    under = None  # the control isn't there any more: it changed
                if (under is None) != (was is None) or (under is not None and was is not None and not _same_control(was, under)):
                    # (1.13.1, item 6) the safety check saw only the first touch: a second one needs the same control
                    said = "nothing" if was is None else was.line(ref=False, actions=False, place=False)
                    if under is None:
                        now = "gone"
                    else:
                        shown = under.line(ref=False, actions=False, place=False)
                        now = "another control with the same name" if shown == said else f"now {shown}"
                    if (box := _DOUBLE_CHANGED.get()) is not None:  # (an empty list is still the journey's)
                        box.append(f"{said} is {now}")
                    return done
                await self._last_checks("tap", {}, under, hit=False, bundle=front)  # (a raw second touch: no hit test)
                if under is not None and (target.kind != "point" or proven):
                    # an element another process draws is proven again where the second touch goes: the first touch
                    # may have moved it, and the same name proves no place (CodeRabbit, PR #18)
                    await self._hosted_proof(under, target.point or under.center)
                await self.session.tap(target, hold_ms=hold_ms)
            except Refused:
                self.control.touched()  # the first touch went out: a refused action doesn't advance the generation itself
                raise
        return done

    def _double_aim(self, target: ui.Target, items: list[ui.Element]) -> ui.Element | None:
        """The element a double tap's touch lands on, in `items`: the front-most one under a point, else the one the
        target names. None when a point has no element under it; `not_found` when the target names none."""
        if target.kind == "point":
            return self._element_at((target.x, target.y), items)
        found = ui.resolve(items, target)
        return self._element_at(found, items) if isinstance(found, tuple) else found

    def _touching(self, element: "ui.Element | None") -> None:
        """Right before a touch: the element it goes to, in the list it was chosen from, for a recording (Codex, PR #10
        cycle 7). Every handler says what it finally chose (a toggle prefers a switch, a point has an element under it),
        so a saved selector is never worked out again by another lookup. The last one before the call ends wins."""
        touched = _RESOLVED.get()
        if touched is not None and element is not None:
            touched[:] = [(element, list(self.session.items))]

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
            _, _, said = await self._last_checks(tool, a, under, hit=False)  # (a point is a raw touch: no hit test)
            self._dry(a, f"Would {word} at {found[0]:g},{found[1]:g}.{said}")
            self._touching(under)
            return ui.without_place(await self._press(target, hold_ms))
        self._refuse_disabled(found)  # even `force` doesn't touch what the app has switched off
        if a.get("force"):
            # `force` skips the checks above and the hit test, never the policy
            _, _, said = await self._last_checks(tool, a, found, hit=False)
            await self._hosted_proof(found, found.center)  # (`force` never touches a place that isn't known)
            self._dry(a, f"Would {word} {_brief(found)} at {round(found.center[0])},{round(found.center[1])}.{said}")
            self._touching(found)
            done = Noted(ui.without_place(await self._press(target, hold_ms)))  # at its centre, whatever is drawn there
            done.note = self._forced(view, found)
            return done
        self._guard(view, found)

        found, point, said = await self._last_checks(tool, a, found, aim=self._touch_point)
        at = point or found.center
        await self._hosted_proof(found, at)
        self._dry(a, f"Would {word} {_brief(found)} at {round(at[0])},{round(at[1])}.{said}")
        self._touching(found)
        if point is None:
            return ui.without_place(await self._press(target, hold_ms))
        await self._press(ui.Target(x=point[0], y=point[1]), hold_ms, proven=True)
        return f"{verb} {found.line(actions=False, place=False)} at its visible part ({round(point[0])}, {round(point[1])})."

    def _moved_under(self, view, x: float, y: float) -> bool:
        """Has the picture around a point changed since the list the agent last had? A touch by position is aimed
        at what that list's moment showed there; if something moved in (a sheet, a banner, a scrolled row) the
        touch would land on something else. Only a fresh stream frame against the frame of the agent's last read
        can tell; without both, or after the agent chose an answer without a list (`look: false`), it can't, and
        the touch goes ahead as before. (2.0, lead's decision) Not when the agent's last list of this view was cut
        short: on such a screen (a playing feed) every pixel around a point changes all the time, so the guard would
        refuse every touch by point or o ref and mean nothing (the o refs already end with any action, R200v M2)."""
        if _cut_short(getattr(view, "tree", None)):
            return False
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

    def _delivered_hint(self, a: dict) -> str:
        """The advice that goes with a touch that changed nothing, by what the touch was aimed at in the agent's list:
        a text input, a point with no element under it, or anything else. A target that can't be found gets the
        general advice (the touch itself will say not_found)."""
        try:
            found = ui.resolve(self.session.items, _target(a))
        except Refused:
            return HINT_LOADING
        if isinstance(found, tuple):
            found = self._element_at(found)
            if found is None:
                return HINT_BARE_POINT
        if found.role == "input":
            return HINT_FIELD
        return HINT_LOADING

    async def _tap(self, view, a: dict) -> str:
        return await self._touch(view, a, TAP_MS)

    async def _alert(self, view, a: dict) -> str:
        """Read or answer any current alert. Named buttons support localized system UI."""
        action, name = a["action"], a.get("name")
        if action == "cancel":
            name = name or "Cancel"
        await self._last_checks("alert", a, None, hit=False)  # (first: a dry run meets the same gate, review R180 L5)
        self._dry(a, f"Would {action} alert" + (f" using {name!r}." if name else "."))
        if name is not None:
            return await self.session.alert(action, name=name)
        return await self.session.alert(action)

    async def _swipe(self, view, a: dict) -> str:
        """`swipe`: a drag that barely moves is a touch where it starts, so it goes through the policy as a tap there
        would (safety review H3). A full swipe across a list row runs the row's first action (often Delete or Archive)
        with no button touched, so it needs a person's OK each time (review 1.7 B-M7). The tree is read for every swipe."""
        x0, y0, x1, y1 = (float(a[k]) for k in ("x0", "y0", "x1", "y1"))
        items = (await self.session.refresh()).items
        said = ""
        if math.hypot(x1 - x0, y1 - y0) < SWIPE_IS_TAP:
            # (a touch where it starts, at a point: raw, as a point tap is)
            _, _, said = await self._last_checks("swipe", a, self._element_at((x0, y0), items), hit=False)
        elif (row := full_row_swipe(items, x0, y0, x1, y1)) is not None:
            said = await self._full_swipe_ok(a, row)
        self._dry(a, f"Would swipe from ({x0:g}, {y0:g}) to ({x1:g}, {y1:g}).{said}")
        return await ACTIONS["swipe"][0](self.session, a)

    async def _full_swipe_ok(self, a: dict, row: ui.Element) -> str:
        """A requested full swipe executes without a label-based confirmation."""
        return ""

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
            self._touching(found)  # nothing is touched, but a recording saves this switch, not the generic lookup's element
            return Unchanged(f"{found.line(actions=False, place=False)} is already {'true' if want else 'false'}.")
        force = bool(a.get("force"))

        def aim(switch: ui.Element) -> tuple[float, float] | None:
            spot = self._touch_point(switch)
            return self._switch_control(self.session.items, switch, spot) if switch.width > WIDE_SWITCH * switch.height \
                else spot

        checked, aimed, said = await self._last_checks("toggle", a, found, hit=not force, aim=aim)
        found = checked if checked is not None else found
        if not force:
            point, shown = aimed, self._touch_point(found)
        at = point or found.center
        await self._hosted_proof(found, at)
        self._dry(a, f"Would toggle {_brief(found)} at {round(at[0])},{round(at[1])}.{said}")
        self._touching(found)
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
        return ui.switch_first(items, target)

    async def _back(self, view, a: dict) -> str:
        """Close an open popover by a touch outside it (AGENT-TOOLS v1.4); else tap the navigation bar's back
        button. A bar without one is a root screen (or has only Cancel,
        Done and the like): nothing is tapped. The edge swipe is only for screens with no bar at all."""
        s = self.session
        listed = self._back_from_list(view)
        back = listed if listed is not None else await self._still_back()
        if listed is not None:
            trace.note("back_from_list", True)  # (the timing trace only: no read before the touch)
        if back.dismiss is not None:  # AGENT-TOOLS v1.4: what is open on top closes first, as "up one screen"
            self._dry(a, "Would close the sheet.")
            await s.tap(ui.Target(x=back.dismiss[0], y=back.dismiss[1]))
            return "Closed the sheet."
        behind = getattr(s, "behind", [])
        # A sheet in front with no region of its own to tap. A back button it hides belongs to the screen behind
        # (it can't be reached, and where the backdrop lets a touch through it would leave that screen with the
        # sheet open); one that shows is the sheet's own and is tapped as usual.
        if behind and not (back.point is not None and not any(ui._holds(e.rect, back.point) for e, _ in behind)):
            if any(e.placed == "unknown" for e in (getattr(s, "front_items", None) or view.items)):
                # the sheet's own frames aren't the screen's: beside it or its top edge can't be told (last Opus
                # review, M2)
                raise Refused("not_actionable", HOSTED_SHEET)
            self._dry(a, "Would close the sheet in front.")
            return await self._close_sheet(view)
        if back.hosted:
            raise Refused("not_actionable", HOSTED_BACK)
        if back.point is not None:
            self._dry(a, "Would tap the back button.")
            await self._back_before(view, a, listed is not None)
            await s.tap(ui.Target(x=back.point[0], y=back.point[1]))
            return "Tapped the back button."
        if back.bar:
            raise Refused("not_actionable", "no_action: the navigation bar has no back button")
        width, height = s.screen
        self._dry(a, "Would swipe from the left edge.")
        await self._back_before(view, a, listed is not None)
        await s.swipe(2, height / 2, 0.6 * width, height / 2, 300)
        return "Swiped from the left edge."

    def _back_from_list(self, view) -> ui.Back | None:
        """(2.0, worker H) What `back` would touch, from the agent's own list when nothing can have changed since it was
        read: then reading the tree again before the touch only costs time (0.5-1.0 s on a heavy page, measured on the
        phone 2026-11-26). The same test the tap path uses before it reads live (_current), and stricter: the list's
        tree is the session's newest read (no other read since, of this agent or another), whole, with nothing open on
        top (a sheet or popover is read until it stands still, _still_back); the agent's list is current, the stream's
        newest frame is fresh and like the one the list was read against (_screen_unchanged), and the screen stands
        still now (_proven_still). Any doubt: None, and `back` reads as before."""
        s = self.session
        tree = getattr(view, "tree", None)
        if (tree is None or tree is not getattr(s, "last_tree", None) or getattr(tree, "tier", "full") != "full"
                or tree.back is None or tree.back.dismiss is not None or tree.behind):
            return None
        if view.opened or self.unsettled or not self._current(view) or not self._screen_unchanged(view):
            return None
        try:
            if not self._proven_still(noted=False):
                return None
        except Exception:  # noqa: BLE001 - a picture that can't be compared proves nothing: read as before
            return None
        s.behind, s.front_items = tree.behind, tree.items  # (as back_point leaves them, for `back`'s own checks)
        return tree.back

    async def _back_before(self, view, a: dict, listed: bool = False) -> None:
        """`back` with `look: false`: the state before the touch, to compare with the one after it (see
        `_back_unchanged`): the newest frame, if there is a stream, and a light read of the tree. The edge swipe
        makes the picture move (an interactive pop that snaps back), so movement says nothing: the end state does.
        Kept on the agent's own view, never on the daemon: another agent's back can't take it. (It runs inside the
        touch's turn of the phone's queue, so its read is a direct one.) `listed`: the agent's list was proven the
        screen now (_back_from_list): it is the state before, with no read."""
        view.back_before = None
        if a.get("look", True):
            return
        items = view.tree.items if listed else (await self.session.refresh()).items
        view.back_before = (self.hub.latest, _screen_sig(items))

    async def _back_unchanged(self, view) -> bool:
        """Is the end state of `back`'s touch the one before it? The settled frame against the frame before (when
        both exist) and the tree against the tree before. False when nothing was read before. A picture that
        differs is a change without reading the tree."""
        kept, view.back_before = getattr(view, "back_before", None), None
        if kept is None:
            return False
        was, before = kept
        if was is not None and self.hub.latest is not None and not alike(was, self.hub.latest):
            return False
        # after the touch's turn: the read waits its turn in the phone's queue, as every read does
        tree = await self._read(self.session.refresh)
        if _cut_short(tree):
            return False  # (R200v M1) a tree cut short can't show that nothing changed (two roots always compare equal)
        return _screen_sig(tree.items) == before

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
        began = time.perf_counter()
        proven = self._proven_still()
        trace.note("still_proven", proven)  # (the timing trace only: whether the wait was skipped, and what the proof cost)
        trace.note("proof_ms", round((time.perf_counter() - began) * 1000, 2))
        done = Settled(0.0, False, False) if proven else await self._settle(self._mark()[0], cap=RESOLVE_SETTLE)
        self.unsettled = False
        trace.mark("still")  # (the read's request goes next)
        mark = self._mark()  # taken before the read: a frame that changes meanwhile counts against the list
        tree = await s.refresh()
        trace.mark("read")
        self._read_root = (tree.app, tree.alert is not None)  # what this read says is in front (_scope_from_read)
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

    async def _presence(self, items: list[ui.Element], target: ui.Target, kind: str, want: object, cut: bool,
                        left: float) -> bool | None:
        """(2.0, review R200v H2) Is the text, id or label on screen (True), not on it (False), or can't that be told
        (None)? An element the read gave proves it there, whole tree or not; only a whole tree proves it absent. On a
        tree cut short (`cut`) a text is looked for in the picture's text as well (within `left` seconds), and a picture
        read that failed or ran out of time proves nothing either way."""
        if not self._missing(items, target):
            return True
        if not cut:
            return False
        if kind != "text" or not self._ocr_possible():
            return None
        try:
            found, note = await _wait_for(self._screen_text("fast", implicit=True), max(WAIT_FOR_MIN_LEFT, left))
        except asyncio.TimeoutError:
            return None  # (the time ran out while the picture was read)
        if note:
            return None  # neither the phone nor this computer read it: nothing was seen, and nothing proven absent
        return _seen_text(str(want), found)

    @staticmethod
    def _unread_message(kind: str, want: object, gone: bool, seconds: float, text_too: bool) -> str:
        """The answer when a wait's condition couldn't be decided by its deadline (UNREAD)."""
        picture = " and its picture's text wasn't read either" if text_too else ""
        return (f'Waited {seconds:g} s for {kind} "{want}" to {"disappear" if gone else "appear"}, but {UNREAD} within '
                f"the time given{picture}, so whether it did isn't known. Look (the text in the picture comes as o "
                "refs), then wait for a text, or touch an o ref or a point.")

    async def settle(self, after_seq: int, still: float | None = None, cap: float = CAP,
                     since: Frame | None = None, early: int | None = None) -> tuple[float, bool]:
        """(seconds waited, changed): see _settle."""
        done = await self._settle(after_seq, still, cap, since, early)
        return done.seconds, done.changed

    async def _settle(self, after_seq: int, still: float | None = None, cap: float = CAP,
                      since: Frame | None = None, early: int | None = None, bundle: str | None = None) -> Settled:
        """_settle_frames, with the cap of a screen that never stands still (Session.settles, SettleCap): shorter while
        the app in front is known for it, and learned from how this settle ends. `bundle`: the app whose screen this is
        when the newest read isn't of it (open_app's launch settle: the app launched, known for it from an earlier
        visit); a settle is learned from only when the newest read is of that app (its tier says whether it reads)."""
        caps = getattr(self.session, "settles", None)
        if caps is None:
            return await self._settle_frames(after_seq, still, cap, since, early)
        read = self._read_bundle()
        bundle = bundle or read
        done = await self._settle_frames(after_seq, still, caps.cap(bundle, cap), since, early)
        if (done.still or done.capped) and bundle == read:
            cut = self._tier_now() != "full"
            if caps.settled(bundle, done.capped, cut, done.seconds):
                self.log(f"{bundle}'s screen never stands still: actions settle within {caps.short:g} s"
                         if caps.restless(bundle) else f"{bundle}'s screen stood still: actions settle as usual")
        return done

    async def _settle_frames(self, after_seq: int, still: float | None = None, cap: float = CAP,
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
        After a touch the hands sent (Session.hands_at), "changed, then still" ends it no sooner than HANDS_SETTLE_MIN
        from the touch's end: a row's highlight and the stillness after it come before the page's push.
        `changed` is False only when the answer is certain: 3 frames, all like `since`, and `quiet` seconds.
        A stall, no frames at all (it sleeps `settle_fallback` in total, first-frame wait included), too
        few frames, or the cap all report changed=True, so the tree gets read."""
        gap = self.hub.gap()
        still = max(self.settle_still if still is None else still, 2.5 * gap if gap else 0.0)
        stall = max(3 * gap if gap else 0.0, still)
        loop = asyncio.get_running_loop()
        began = loop.time()
        # "nothing changed" is believed later after a touch the hands just sent (HANDS_QUIET, counted from its end)
        touched = getattr(self.session, "hands_at", None)
        quiet = self.settle_quiet
        floor = began  # (loop time) the earliest "changed, then still" may end the settle
        if isinstance(touched, float) and 0 <= time.monotonic() - touched < HANDS_FRESH:
            since_touch = time.monotonic() - touched
            if self.settle_quiet >= MIN_QUIET:
                quiet = max(quiet, HANDS_QUIET - since_touch)
            # a highlight followed by a moment of stillness is not the touch's end (HANDS_SETTLE_MIN)
            floor = began + max(0.0, HANDS_SETTLE_MIN - since_touch)
        deadline = began + cap
        seq, differs, run, run_start, first_of_run = after_seq, False, 0, began, None
        if early is not None:
            last = None
            for arrived, frame in self.hub.since(early, upto=after_seq):
                if not differs and not alike(frame, since):
                    trace.frame(since, arrived)
                differs = differs or not alike(frame, since)
                if alike(frame, first_of_run):
                    run += 1
                else:
                    run, run_start, first_of_run = 1, arrived, frame
                last = arrived
            if not differs:  # nothing showed during the call: only frames after the return count
                run, run_start, first_of_run = 0, began, None
            elif (gap and last is not None and run >= 2 and began - run_start >= still
                  and began - last <= 1.5 * gap and began >= floor):
                return Settled(0.0, True, False, True)  # changed, then still up to a fresh frame at the return
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
            if not differs and not alike(frame, since):
                trace.frame(since)
            differs = differs or not alike(frame, since)
            if alike(frame, first_of_run):
                run += 1
            else:
                run, run_start, first_of_run = 1, now, frame
            if run >= 2 and now - run_start >= still:
                if (now >= floor) if differs else (seq - after_seq >= 3 and now - began >= quiet):
                    return Settled(now - began, differs, False, True)
        return Settled(loop.time() - began, True, differs)

    def _still_for_budget(self) -> bool:
        """Session.screen_still: the stream shows the screen still now, so a tree read gets the normal budget even while
        its app is on the short one (ReadBudget; review R200v H1). As _proven_still, without its timing notes (the read
        that asks is not the settle they describe)."""
        try:
            return self._proven_still(noted=False)
        except Exception:  # noqa: BLE001 - a picture that can't be compared proves nothing: the short budget stays
            return False

    def _proven_still(self, noted: bool = True) -> bool:
        """Do the stream's recent frames already show what _settle waits for, so a read needn't wait? A read for
        a target asks no "nothing changed" question (its settle has no `since`, so it counts as changed): the
        screen is still when the frames alike span `still` seconds (at least 2.5 frame gaps), as _settle ends it.
        (A longer `quiet` window, as the no-change belief needs, was never met after a tap: the previous touch's
        animation ended inside it, on the phone 2026-11-13, so the wait was never skipped.) Only when the newest
        frame is fresh (within 1.5 frame gaps, and shot_fresh) and every frame in the last `still` seconds is like
        the one that began the window, with at least 2 frames in it. No frames, a stale newest, a history shorter
        than the window, or a change in it: not proven, and the read settles as before.
        The newest frame is compared first: after a touch the change is usually at the end of the window, and each
        frame compared costs a decode (about 2.5 ms, on the event loop)."""
        gap = self.hub.gap()
        age = self.hub.age()
        if noted:
            trace.note("frame_gap_ms", None if gap is None else round(gap * 1000, 1))  # (the timing trace only)
            trace.note("frame_age_ms", None if age is None else round(age * 1000, 1))
        if not gap or age is None or age > min(1.5 * gap, self.shot_fresh):
            return False
        still = max(self.settle_still, 2.5 * gap)
        frames = self.hub.recent()
        start = frames[-1][0] - still  # the window ends at the newest frame, which is fresh
        anchor = next((f for t, f in reversed(frames) if t <= start), None)
        if anchor is None:
            return False  # the history doesn't reach back over the window
        within = [f for t, f in frames if t > start]
        return len(within) >= 2 and all(alike(f, anchor) for f in reversed(within))

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
            if self.frame_private(frame):  # (M1: an unlock ran when it was taken; a Note, the call itself is done)
                raise Refused("busy", UNLOCK_BUSY)
            try:
                source = await jpeg_of(frame) if frame is not None else None  # a video picture is encoded here
            except PictureGone:  # it was one of the older pictures, whose pixels are let go
                source = None
            turn = getattr(self.session, "turn", 0)
            if source is not None and turn and getattr(frame, "picture", None) is None:
                source = await self.turned(frame.seq, source, turn)  # landscape: the stream's frame comes as captured
            if source is None:
                asked = self.hub.mono()
                source = await self._read(self.session.screenshot_png)
                if self.unlocked_since(asked):
                    raise Refused("busy", UNLOCK_BUSY)
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
            view.tree = getattr(self.session, "last_tree", None)  # (the place is worked out from it, below)
            view.opened = False  # the agent's own look is later than the launch's
            return text, image
        try:
            text, image = await self._read(read)
        except Exception as e:
            if not lines:
                raise
            view.moved = True  # no list came back: the screen has probably changed
            view.place = None
            return _answer(lines, [*notes, f"couldn't look afterwards: {e}."])
        text, mapped = self._mapped_look(view, text)
        return await self._add_shot(_answer([*lines, text], [*notes, *mapped], image), view, shot)

    async def _after(self, view, result: str, notes: list, full: bool, moves_content: bool | None = False,
                     seconds: float | None = None, snap: Snapshot | None = None, drifted: bool = False,
                     hint: str = "", watched: float | None = None, touch: str = "tap") -> dict:
        """An action's answer: its result line, then what changed on screen since the agent's last answer
        (`view.shown`), or the full list when the app changed or `full`, then `Settled in` (left out when
        `seconds` is None: `wait` did nothing to settle), then the notes. Refs follow their elements. `hint`: a tap
        or long press's advice for when nothing changed (NOTE_DELIVERED), given with `seconds`; the note's own time
        is `watched`, from the touch to now (the settle's `seconds` start only when the call returns); `touch` names
        the touch in it: tap, long press or double tap."""
        s = self.session
        shorter = _BRIEF.get()  # §13.4: the change block cut to 12 lines, a new screen as its actionable elements
        tail = [] if seconds is None else [settled_line(seconds)]
        app_before, listed_before = view.app, view.shown

        async def read():
            self._mark_read(view)
            read_now = snap or await s.snapshot()  # `snap`: a read the caller already made (wait_for's last poll)
            s.items = read_now.tree.items
            app_changed = read_now.app is not None and view.app is not None and read_now.app != view.app
            head = _furniture(view, read_now.tree.alert, read_now.tree.keyboard)
            seen = ui.on_glass(read_now.tree.items) if ui.USERS_EYES.get() else read_now.tree.items  # (§17.1)
            # (2.0) a tree cut short is no list to measure changes by (what it left out would read as gone): the whole
            # of what it gave is the answer, under the line that says it was cut short
            whole = full or _cut_short(read_now.tree)
            up = changes.update(view.items, seen, app_changed, view.top_ref, view.shown, whole, head, moves_content)
            return read_now, up
        try:
            snap, up = await self._read(read)
        except Exception as e:
            view.moved = True  # no list came back: the screen has probably changed
            return _answer([result, *tail], [*notes, f"couldn't look afterwards: {e}."])
        cut = _cut_short(snap.tree)
        covered = not snap.tree.items and bool(view.shown) and not cut  # the agent saw elements; now there are none
        view.tree, view.place = snap.tree, None  # where the app is now is worked out by _learn, after this answer
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
               if same_app and not full and not cut and not ui.USERS_EYES.get() else [])  # (a user sees nothing change off screen)
        if off and not up.full:
            up.lines = [line for line in up.lines if line != changes.NO_CHANGE] + off
        earlier, view.unlooked = view.unlooked, 0
        if earlier and not up.full:
            # the block is measured from the agent's last list, so it holds those actions' changes too: a
            # first-time agent closed a sheet it had opened with look: false and read "No visible change."
            notes = [*notes, NOTE_SPAN.format(n=earlier + 1)]
        if up.full:
            tree = snap.tree
            parts = (up.items, s.screen, snap.scale, snap.app, tree.alert, tree.keyboard, tree.more,
                     (tree.above, tree.below), getattr(tree, "tier", "full"))
            if shorter:  # the header lines, the elements that matter, the lines after them
                top, listed, rest = ui.describe_parts(*parts)
                head = "\n".join([*top, *briefing.screen(listed, listed_before), *rest])
            else:
                head = ui.describe(*parts)
            # the full list because most of the screen changed: what changed off screen still follows it, under
            # a heading of its own (bare `~` lines after a list read as a second, broken block)
            beyond = [changes.OFF_SCREEN, *off] if off else []
            if cut:
                view.ocr = {}  # (the last reading's o refs are of the screen before the action)
                beyond = [*beyond, AFTER_CUT]
            return _answer([result, head, *beyond, *tail], [*notes, *([NOTE_COVERED] if covered else [])])
        if changes.NO_CHANGE in up.lines and _health_sheet(snap.tree.items):
            # iOS's own Health access sheet takes touches and changes nothing. With a host dialog, a person is asked to
            # choose, and this waits for the sheet to go (§11.5); the agent's list is then from before
            if await self._hold_for_person(needs.HEALTH_SHEET, self._health_sheet_gone, every=HEALTH_TICK):
                view.dropped = True
                notes = [*notes, NOTE_HEALTH_DONE]
            else:
                notes = [*notes, NOTE_HEALTH]
        elif changes.NO_CHANGE in up.lines and hint and seconds is not None:
            notes = [*notes, NOTE_DELIVERED.format(touch=touch, seconds=seconds if watched is None else watched, hint=hint)]
        # (a Place: line goes here, after the first line, when places exist: AGENT-TOOLS §18.1, §13.4)
        return _answer([result, *(briefing.cut(up.lines) if shorter else up.lines), *tail], notes)

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
            return {"type": "error", "code": e.code, "message": self._redact(str(e))}
        except ConnectionLost as e:
            self.lost(e)
            return {"type": "error", "code": "reconnecting",
                    "message": "the connection dropped; your input may not have reached the phone"}
        except (ValueError, KeyError, TypeError, IndexError) as e:
            return {"type": "error", "code": "unsupported", "message": f"bad {kind} message: {e}"}
        except Exception as e:
            return {"type": "error", "message": self._redact(str(e))[:300]}

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
            wanted = {**PRESETS[preset], **NO_IDLE_WAIT}  # fixed before the call: a reconnect inside _read rewrites
            # session_settings with the old preset, and a retry must still send the requested one
            await self._read(lambda: self.session.settings(wanted))
            self.session.session_settings = wanted
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
        try:
            await asyncio.wait_for(self._clear_network(), 10)  # first: the phone is never left in a condition
        except Exception as e:
            self.log(f"closing: network: {e}")
        for closing in list(self._closing):
            closing.cancel()
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
            await asyncio.wait_for(self.maps.flush(), 10)  # what was learned is in the map files before the daemon ends
        except Exception as e:
            self.log(f"closing: the maps: {e}")
        try:
            await asyncio.wait_for(self.session.close(), 10)
        except Exception as e:
            self.log(f"closing: {e}")

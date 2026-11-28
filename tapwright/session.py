"""What an agent does with the phone: look, tap, type, swipe, launch apps. One Session
holds the connection and WebDriverAgent's session, connecting on first use."""
import asyncio
import base64
import binascii
import contextvars
import io
import math
import os
import random
import re
import secrets
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Awaitable, Callable

from . import device as device_module
from . import hands as hands_module
from . import media, motion, ocr, store, trace, ui
from .control import TAP_MS, Refused
from .device import AppNotAnswering, ConnectionLost, Device, DeviceError, NotSetUp, PasscodeNeeded, PhoneLocked, Unanswered
from .stream import NO_IDLE_WAIT

FOCUS_POLL, FOCUS_WAIT = 0.1, 1.5  # seconds: how often, and for how long, typing waits for focus to arrive
AUDIT_TIMEOUT = 60.0  # seconds: XCTest's audit walks the whole screen, element by element
LOCK_WAIT = 5.0  # seconds: /wda/locked answers in about 10 ms, since it doesn't read the app (measured)
FIRST_READ_WAIT = 10.0  # seconds: the window size, the first read of the app; past this the app is stuck
BANNER_ZONE = 200.0  # points from the top: where a notification banner can lie (a touch lower down is never held)
SPRINGBOARD = "com.apple.springboard"
KEY_MS = 60  # ms a typed key counts for in the notice to Tapline (see tapline.py)
SCROLL_MS = 1200  # ms a scroll drag counts for there (an eased drag that ends slowly)
ACTION_WAIT = 10.0  # seconds a touch may take to be answered beyond its own length (a tap answers in about 0.5 s)
BANNER_ID = "NotificationShortLookView"  # the identifier of a notification banner in SpringBoard's tree
BANNER_WAIT, BANNER_POLL = 8.0, 0.5  # seconds: how long a read waits for a banner to leave, and how often it looks
# TAPWRIGHT_TRACE=1: every request to WebDriverAgent is logged with how long it took (for profiling, e.g. over Wi-Fi)
TRACE = os.environ.get("TAPWRIGHT_TRACE") == "1"
APPS_STALE = 30.0  # seconds after which the kept list of installed apps is read again, behind the call that used it
APPLE_APPS = "com.apple."  # Apple's own bundle ids: built-in apps, which no list of installed apps holds, and a few
#                            from the App Store (Pages, TestFlight), which it does
# The passcode unlock's keypad (unlock.PhoneLockScreen.press_keys): its requests write no trace line, neither the TRACE
# log line nor trace.py's request count, so nothing gives the passcode's length (last Opus review of 1.7.8, L4)
QUIET: contextvars.ContextVar[bool] = contextvars.ContextVar("tapwright_quiet_trace", default=False)
READ_AGAIN = 5.0  # seconds: the second try of a question that got no answer (see Session._call)
READ_CUT = 10.0  # seconds: the longest a tree read is waited for before asking why (50 times its usual time)
ORIENTATIONS = {"portrait": "PORTRAIT", "landscape_left": "LANDSCAPE",
                "landscape_right": "UIA_DEVICE_ORIENTATION_LANDSCAPERIGHT"}  # WebDriverAgent's names
ROOT_SIZE = re.compile(r'<XCUIElementTypeApplication[^>]*?\bwidth="([\d.]+)" height="([\d.]+)"')
# How far to turn a captured frame clockwise for each WebDriverAgent rotation z (checked on the phone 2026-11-11 with
# Calculator in each landscape side: the frame turned this way reads upright)
TURN_FOR_Z = {90: 90, 270: 270}
TURN_UNKNOWN = -1  # landscape, but which side is up isn't known: every touch goes through the runner until it is
TURN_RECHECK = 1.0  # seconds: in landscape, how often which side is asked again (the tree can't tell the sides apart)
# Tapwright's own WebDriverAgent build (TAPWRIGHT_WDA=fork) has a fast tap route: one touch, with no element lookup and
# no idle wait (WebDriverAgentLib/Commands/TWFastTapCommands.m in the fork). Asked for once per session; Appium's
# build answers the probe with an error, and taps keep going through W3C actions there. TAPWRIGHT_FAST_TAP=0 turns the
# route off. The route's answer waits until the touch has been played (`wait`): answering as soon as it was queued was
# no faster per tap on the phone (0.466 s against 0.469 s, 2026-11-12), and what came next raced the touch (typing
# looked for the focused field before the tap had focused it, and its keys hit "only one gesture at a time").
FAST_TAP = os.environ.get("TAPWRIGHT_FAST_TAP", "1")
FAST_VERSION_PATH = "/session/{sid}/wda/tw/version"
FAST_TAP_PATH = "/session/{sid}/wda/tw/tap"
# The fork's route version 2 adds a fast drag: one touch path [[t_ms, x, y], ...] played the same way as the fast tap
# (pressed at the first point, moved through each later one at its time, lifted at the last), answering once played.
# Scrolls, swipes and drags send it the very path their W3C actions describe, so a scroll still eases and rests at
# its end and never flings. TAPWRIGHT_FAST_DRAG=0 (or TAPWRIGHT_FAST_TAP=0) keeps drags on W3C actions.
FAST_DRAG = os.environ.get("TAPWRIGHT_FAST_DRAG", "1")
FAST_DRAG_PATH = "/session/{sid}/wda/tw/drag"
# The fork's route version 3 adds the photo routes (AGENT-TOOLS §17.6, `media`): GET access, POST add, POST delete.
PHOTOS_ACCESS_PATH = "/session/{sid}/wda/tw/photos/access"
PHOTOS_ADD_PATH = "/session/{sid}/wda/tw/photos/add"
PHOTOS_DELETE_PATH = "/session/{sid}/wda/tw/photos/delete"
PHOTOS_WAIT = 150.0  # seconds: iOS may be asking the person to allow the runner photo access
# Videos (route version 7): copied into the runner's container first, under this folder (the runner takes only files
# there), and the add waits longer for big clips: the runner's import wait grows a second per 4 MB, to 8 minutes more.
STAGE_ROOT = media.STAGE_ROOT
STAGE_STALE = 3600.0  # seconds: a staged folder older than this is a leftover of an add that timed out; swept
VIDEO_RATE = 4 * 1024 * 1024  # bytes a second the import wait allows for
VIDEO_EXTRA_MAX = 480.0  # seconds that wait may grow by at most
RUNNER_PROMPT_WAIT = 120.0  # seconds the runner waits for the person's answer to iOS's photo prompt, before its change


def photos_timeout(video_bytes: int, asks: bool = False) -> float:
    """How long an add with `video_bytes` of videos may take on the runner: iOS's prompt, then the library's import.
    `asks` (a first video add: iOS asks the person first): the runner waits for the answer (RUNNER_PROMPT_WAIT) and
    only then starts the import's own wait, so both count (1.7.9, review R179b L5)."""
    extra = min(VIDEO_EXTRA_MAX, video_bytes / VIDEO_RATE)
    return PHOTOS_WAIT + extra + (RUNNER_PROMPT_WAIT if asks and video_bytes else 0.0)


def _identifiers(value: object) -> list[str]:
    found = value.get("identifiers") if isinstance(value, dict) else None
    return [i for i in found if isinstance(i, str)] if isinstance(found, list) else []
# The fork's route version 4 (Tapwright 1.6 E2, E3) adds a compact tree read: only what the list reads, as compact JSON
# (ui.xml_of_tree turns it back into the XML /source sends), with "unchanged" when nothing changed since the read whose
# token it is sent. TAPWRIGHT_FAST_READ=0 keeps reads on /source. And the fast tap takes `apps`: the runner touches only
# while one of them is in front (FRONT_REFUSED otherwise), so personal mode's question needs no request of its own.
# TAPWRIGHT_FRONT_IN_TAP=0 keeps that question separate.
FAST_READ = os.environ.get("TAPWRIGHT_FAST_READ", "1")
TREE_PATH = "/wda/tw/tree"  # (without a session, as SOURCE_PATH: the same app is read)
FRONT_IN_TAP = os.environ.get("TAPWRIGHT_FRONT_IN_TAP", "1")
FRONT_REFUSED = "tapwright front:"  # how the fork's tap starts its refusal: another app is in front, nothing touched
# The fork's route version 5 (Tapwright 1.7 part 7) adds the keyboard focus to the compact read ("f": Element.focused)
# and a hit test: GET HIT_PATH?x=&y= answers the element iOS finds at a portrait point, the one a touch there goes to
# (QUALITY-BAR 2.5 R3: a target something the list doesn't show as a bar is drawn over is refused `covered`). One more
# request before a touch on an element; any error, an older runner or landscape means no check (1.6's behaviour).
# TAPWRIGHT_HIT_CHECK=0 turns the check off.
HIT_CHECK = os.environ.get("TAPWRIGHT_HIT_CHECK", "1")
HIT_PATH = "/wda/tw/hit"  # (without a session: it asks XCTest's daemon, not the session's app)
HIT_IDENTITY_REFUSED = "tapwright hit: identity_unproven"  # terminal: the snapshot couldn't be bound to the AX hit
HIT_WAIT = 5.0  # point lookup (0.3 s), direct attributes (2 s), then exact-identity app fallback (2 s)
# Tapwright 2.0 hands (SPEC-2.0 D1): touches and buttons sent from the computer through CoreDevice HID (hands.py) when
# the session has Hands and they can be used; the runner's synthesis otherwise, as before. TAPWRIGHT_HANDS=runner keeps
# everything on the runner. The trace counts a hands gesture as a touch (trace.TOUCH_SUFFIXES).
HANDS = os.environ.get("TAPWRIGHT_HANDS", "auto")
HANDS_PATH = "/hands"
# press_button's names -> hands.BUTTONS' (lock is hands-only: XCTest can't press it)
HAND_BUTTONS = {"home": "home", "lock": "lock", "volumeUp": "volume_up", "volumeDown": "volume_down"}
END_INSET = 40.0  # points from a field's right edge where typing's tap puts the caret at the end of short text
BACKSPACE_MAX = 64  # retyping into the field just typed into deletes up to this many characters with backspaces
# (2.0, runner route version 9) Every accessibility read is bounded on the runner: the tree route answers within a
# budget with what it could read (`tier`: full, shallow, or root = the application node alone), the setting below caps
# every other read, a screen-size route answers without reading the app, and an OCR route reads the screen's text with
# Apple's Vision. A runner older than 9 works as in 1.10.0.
BOUNDED_VERSION = 9
VERSION_PATH = "/wda/tw/version"  # (without a session: asked in ensure, before the session exists)
SCREEN_PATH = "/wda/tw/screen"
OCR_PATH = "/wda/tw/ocr"
OCR_WAIT = 8.0  # seconds the runner's OCR may take (Vision's accurate level: about 0.5-1.5 s, and the shot first)
# BCP 47 codes, comma-separated, in order of preference, for the runner's OCR (Vision's recognitionLanguages), e.g.
# "en-US,ar-SA"; empty: Vision's default (English). Vision's fast level reads Latin script only; a language the level
# doesn't support fails the read, and the picture is then read on this computer instead
OCR_LANGUAGES = os.environ.get("TAPWRIGHT_OCR_LANGUAGES", "").strip()
TIERS = ("full", "shallow", "root")
_TIER_RANK = {"root": 0, "shallow": 1, "full": 2}  # (2.0.3) how much of the tree a read got
# (review R200v M4) the languages Vision's fast level reads (Latin script; Apple's fast revision lists English, French,
# Italian, German, Spanish and Portuguese), by their primary subtag. A fast read asked in any other language fails with
# `unknown error` (RUNNER-V9-PROTOCOL §4), so wait_for's polls read at the accurate level then.
FAST_LANGUAGES = frozenset({"en", "fr", "it", "de", "es", "pt"})


def fast_reads(languages: str) -> bool:
    """Can Vision's fast level read every one of these comma-separated BCP 47 codes ("" = its default, English)?"""
    return all(code.strip().split("-")[0].lower() in FAST_LANGUAGES for code in languages.split(",") if code.strip())


def _env_float(name: str, default: float) -> float:
    """A positive, finite number from the environment, else `default` (CodeRabbit, PR #26: "inf" failed the import)."""
    try:
        value = float(os.environ.get(name, default))
    except ValueError:
        return default
    return value if math.isfinite(value) and value > 0 else default


def _env_ms(name: str, default: int) -> int:
    """A whole number of milliseconds, at least 1, from the environment, else `default` (0.5 gave a 0 ms budget)."""
    value = int(_env_float(name, default))
    return value if value >= 1 else default


AX_TIMEOUT = _env_float("TAPWRIGHT_AX_TIMEOUT", 3.0)  # seconds: the runner's ceiling for any one accessibility read
TREE_BUDGET_MS = _env_ms("TAPWRIGHT_TREE_BUDGET", 3000)  # ms a tree read may take on the runner
# ms for an app whose last read was cut short. (Review R200v H1) The runner gives `full` 60 % of the budget, after
# finding the app and an idle wait of up to a quarter of it, and tries `shallow` only with 0.25 s left past a reserve
# for `root`: at 600 ms nearly every read came back `root`, even of a screen that reads whole in 0.4 s (the home screen's
# tree took 0.4 s in Phase 0). 1500 ms leaves `full` about 0.9 s and room for `shallow`.
SHORT_BUDGET_MS = 1500
QUICK_CUT = 0.25  # a read that comes back with no elements in under this part of its budget wasn't kept waiting by the
#                   app: the app had nothing to answer with yet. It is asked again, RETRY_GAP seconds later, while at
#                   least RETRY_MIN_MS of the budget is left
BY_STILL_FOR = 30.0  # seconds a read may take to answer and still count as the one a still screen was given
BUDGET_GRACE = 5.0  # seconds past its budget a budgeted read's answer is waited for (the runner answers within the
#                     budget, plus up to 0.5 s for the turn of a root and the time to build and carry the answer)


def _env_ms_or_off(name: str, default: int) -> int:
    """As _env_ms, but 0 is kept: it turns off what the variable sizes."""
    try:
        if float(os.environ.get(name, default)) == 0:
            return 0
    except ValueError:
        pass
    return _env_ms(name, default)


# (2.0.3) ms a read is given once more when the normal budget cut it, unless the picture plays. A shopping app's home
# screen (900-1,000 elements, phone 2026-11-26) reads whole every time in 3.5-7.3 s cold by XCTest's snapshot (0.4-0.7 s
# warm), and never within 3 s: on the budgets alone it answered "0 elements" for as long as the agent stayed. 0 (or a
# value not above the normal budget) turns it off.
LONG_BUDGET_MS = _env_ms_or_off("TAPWRIGHT_TREE_LONG_BUDGET", 8000)
# (review R203 M3) the way the long read goes: `xctest` (the default: lists views another process draws) or `direct`
# (the lean direct read: steadier on the measured screen, 4.1-4.2 s, but its whole-tree attempt gets about 60 % of the
# budget). For measuring; TAPWRIGHT_TREE_PATH still forces every read.
def _long_path_of(raw: str) -> str:
    """TAPWRIGHT_TREE_LONG_PATH's way: `direct`, else `xctest` (the default)."""
    return "direct" if raw.strip().lower() == "direct" else "xctest"


LONG_PATH = _long_path_of(os.environ.get("TAPWRIGHT_TREE_LONG_PATH", ""))
LONG_MEMORY = 300.0  # seconds no long try is made for an app after a long-budget read of it was cut, while it never read
#                      whole with it (a paused video: without this every minute cost 3 + 8 s); doubled for each such read
#                      in a row (the lead, after the phone test: the try is the default, and a feed may slip past it)
LONG_MEMORY_MAX = 6 * 3600.0  # the longest that block grows to
# (round G2, phone 2026-11-27) a long read that wasn't whole blocks the next one only on the screen it failed on, while
# its app has had fewer than this many in a row: one page of a shopping app that never reads (its own timeouts) left
# every readable page of the app, the home screen after a relaunch too, at "0 elements" for 60, 120, 240 s; the moment
# the block ran out the home screen read whole in 2.5-5.3 s. From this many in a row on, the block is the app's (an app
# that reads nowhere, a feed whose picture always differs: review R203c H1), with its doubling.
LONG_APP_MISSES = 3
# (2.0.3 round T, review RS-M2) the screens of an app whose long reads failed that are kept (their pictures): a long read
# on any of them is blocked, so going back and forth between two such screens doesn't pay a long read at each switch
LONG_PICS = 4
LONG_RETRY = 60.0  # (review R203 M2) the same for an app that has read whole with the long budget: one cut screen of it
#                    (a video page) mustn't leave its readable screens unread for 5 minutes
LONG_NOTE = ("this screen took about {seconds:.1f} s to read, longer than the usual time given; its elements are listed, "
             "and its next reads are given that time unless something plays on it")


# (2.0.3, review R203 L1) whose call a read belongs to: the daemon sets a fresh list for each tool call, and the
# "took long" note goes into it, for that call's answer only (None: a read of no call, whose note is dropped). Held by
# the call (CodeRabbit, PR #30): two calls that overlap can't take or overwrite each other's note
READ_OWNER: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar("tapwright_read_owner", default=None)
# (2.0.3 round S, the practice S-F1) a read made for an action's own use right before its input (type_text's look for its
# field): the normal budget only, no long try. One such read during type_text on a busy app cost the keys 8 s
NO_LONG: contextvars.ContextVar[bool] = contextvars.ContextVar("tapwright_no_long", default=False)
# (round S, S-F1) the words of a tree read cut while the app was busy redrawing (XCTest lost the application element
# mid-read): it says nothing about whether the screen reads, so a long read cut so isn't counted in the app's row
BUSY_WHY = "is not present in the current view anymore"


def read_wait(budget_ms: int | None, long: bool = False) -> float:
    """Seconds a tree read's answer is waited for before it is asked again: READ_CUT without a budget; with one, the
    budget and BUDGET_GRACE, never more than READ_CUT (over Wi-Fi an answer that never came cost 10 s, 2026-11-26).
    `long` (2.0.3, a read with the long budget): the budget and BUDGET_GRACE, past READ_CUT (the XCTest way ran up to
    2.4 s past its budget; a long read's silence is never asked again: Session._call `once`)."""
    if budget_ms is None:
        return READ_CUT
    return budget_ms / 1000 + BUDGET_GRACE if long else min(READ_CUT, budget_ms / 1000 + BUDGET_GRACE)
RETRY_GAP = 0.25
RETRY_MIN_MS = 300
_AFTER_MS = re.compile(r"\(after (\d+) ms\)")


def _gave_up_ms(why: str) -> float | None:
    """How long the runner says it tried before it gave a read up: the sum of its `why`'s "(after N ms)" (one for each
    tier it tried). None when it doesn't say: nothing is concluded then."""
    found = _AFTER_MS.findall(why or "")
    return float(sum(int(n) for n in found)) if found else None
BUDGET_MEMORY = 60.0  # seconds a cut-short read keeps its app on the short budget (20 until the phone tests of 2026-11-26:
#                       on a feed that never answers, the normal-budget read that asks again cost 2.7 s every 20 s)
# (2.0) how the runner reads a tree (RUNNER-V9-PROTOCOL §1 `path`). Measured on the phone (lead, 2026-11-26): `direct`
# keeps to its budget on TikTok's feed (1500 ms -> 1.43 s) where `xctest` takes 2.9-3.9 s whatever the budget, but on
# a screen that reads (Settings > General, 264 nodes) `xctest` is about 30 % faster (0.97 s against 1.25-1.32 s). So
# the normal budget reads by `xctest` and the short one (an app whose read was just cut, on a screen that isn't still)
# by `direct`: a screen that never answers costs 3-4 s once, then stays bounded. TAPWRIGHT_TREE_PATH=direct|xctest
# forces one (for measuring).
TREE_PATH_FORCED = os.environ.get("TAPWRIGHT_TREE_PATH", "").strip().lower()
TREE_ATTRS = os.environ.get("TAPWRIGHT_TREE_ATTRS", "lean").strip().lower()  # `std` turns the lean read off (version 10)
# (2.0, runner route version 11: RUNNER-V11-PROTOCOL.md) the app in front without reading it (a few ms, where
# /wda/activeAppInfo took 1.7-30 s on TikTok's feed), the screen as a JPEG in about 50 ms (the PNG /screenshot took
# 250-640 ms), and the OCR read's picture in its own answer (`image=1`)
FRONT_VERSION = 11
FRONT_PATH = "/wda/tw/front"
SHOT_PATH = "/wda/tw/shot"
FRONT_WAIT = 3.0  # seconds: the runner's own worst case is about 1.8 s (an AX lookup bounded to 0.5 s, then the daemon's)
SHOT_WAIT = 10.0  # seconds a JPEG shot may take (about 50 ms)
SHOT_QUALITY = 80  # the JPEG quality asked of the runner's shot (shot.render encodes the answer's picture again)
# (2.0, worker H) seconds a read of an app whose screen doesn't answer and keeps moving may be left unasked after a real
# one went out (ReadBudget.plan): on TikTok's feed each read cost its whole budget (1.3-1.4 s at 1500 ms), before and
# after every action. One real short-budget read goes out every UNASKED_FOR seconds, so a screen of that app that reads
# (a profile, while a video plays in it) is found again; a still screen gets the normal budget at once (ReadBudget).
UNASKED_FOR = 8.0
LONG_SILENT_WHY = ("not read: a read of {bundle} with the long budget got no answer in its time, and the runner may "
                   "still be reading it")
UNASKED_WHY = ("not asked: {bundle} is still in front, its screen keeps moving, and its read {age:.1f} s ago gave no "
               "elements in time")


def _rect_within(inner: tuple[float, ...], outer: tuple[float, ...]) -> bool:
    """(round H) Does the frame `inner` lie inside `outer` (within a point, as frames are rounded)?"""
    return (inner[0] >= outer[0] - 1 and inner[1] >= outer[1] - 1 and inner[0] + inner[2] <= outer[0] + outer[2] + 1
            and inner[1] + inner[3] <= outer[1] + outer[3] + 1)


PARTIAL_MIN = 5  # (round H, review RG M3) more elements than this under the root: a shallow answer that gave something


def _has_elements(source: str | None) -> bool:
    """(round G, H) Does a shallow tree's XML hold something the agent can use: an element under the root with a
    label, a name or a value, or more than PARTIAL_MIN elements? An Application and its Window alone is no answer."""
    if not isinstance(source, str):
        return False
    try:
        root = ET.fromstring(source)
    except ET.ParseError:
        return False
    under = [n for n in root.iter() if n is not root]
    return len(under) > PARTIAL_MIN or any(
        (n.get(k) or "").strip() for n in under for k in ("label", "name", "value"))


# (round G, phone 2026-11-27) how recent the runner's answer of the app in front must be for a read to be planned by
# that app: the daemon asks it right before each answer's read, so after an action that changed the app in front the
# read is the new app's (an open_app from a slow app planned the read with the app that was left: 10 s, and a miss
# counted against an app no longer in front)
FRONT_FOR_PLAN = 2.0


def tree_path(budget_ms: int, normal_ms: int, forced: str | None = None) -> str:
    """The runner's way of reading for a read with `budget_ms` (see TREE_PATH_FORCED)."""
    forced = TREE_PATH_FORCED if forced is None else forced
    if forced in ("direct", "xctest"):
        return forced
    if forced == "lean":  # (2.0.1) every read the direct way with the lean set, as 2.0.0 read: faster, and blind to
        return "direct"  # views another process draws inside the app
    return "direct" if budget_ms < normal_ms else "xctest"


class ReadBudget:
    """How long the runner may spend on a tree read of an app (route version 9's `budget`). A screen that never answers
    in time (TikTok's For You feed: a read of one element takes 14 s) would otherwise cost the whole budget on every
    look and every action. So, per bundle id: when a read with the normal budget came back truncated, that app's reads
    use the short budget for BUDGET_MEMORY seconds; a read that comes back full after more than the short budget gives
    it the normal budget again at once (the screen is only slow to read). A full read the short budget would have
    covered changes nothing: a feed that reads whole for a moment after a launch and then not again (TikTok's, phone
    2026-11-26) paid the whole normal budget at every flip.
    A truncated answer to a short-budget read doesn't renew the time: once it runs out, one read with the normal
    budget asks again (a screen that is only slow to read gets its whole tree back within BUDGET_MEMORY seconds).
    A screen that is still (the caller's `still`: the stream's pictures show nothing moving) gets the normal budget even
    then: a playing feed is never still, and a still screen of the same app (a profile, a settings page) is usually one
    that reads whole (review R200v H1). When such a read is cut short too (the feed showing a photo, a paused video:
    still, and it doesn't answer), stillness gives the normal budget again only after BUDGET_MEMORY seconds: on the phone
    every read of that screen cost the normal budget, and none was left unasked (lead, 2026-11-26).
    (2.0.3) The long budget, for a screen that is only slow: a normal-budget read cut short is asked once more in the
    same read with `long_ms` (the caller's long_try; the daemon stops it only when the picture changed across the read
    by more than STILL_BOX). An app whose long read came back whole is slow but readable: its reads are planned with the
    long budget (never the short one, never unasked; the caller's `long_ok` says nothing plays on the screen and its
    time covers the read). Each long read blocks the app's next one
    from the moment it is sent (long_sent): only a whole answer lifts the block, so a long read that is cut, raises, is
    refused, cancelled or never answered keeps it, for `long_memory` seconds (`long_retry` for an app that read whole
    with the long budget before: review R203 M2), doubled for each such read in a row up to `long_memory_max`. Neither the block nor the slow-but-readable mark is a fact about a
    runner session: forget() keeps both (review R203 H2)."""

    def __init__(self, normal_ms: int = TREE_BUDGET_MS, short_ms: int = SHORT_BUDGET_MS,
                 memory: float = BUDGET_MEMORY, clock: Callable[[], float] = time.monotonic,
                 unasked_for: float = UNASKED_FOR, long_ms: int = LONG_BUDGET_MS,
                 long_memory: float = LONG_MEMORY, long_retry: float = LONG_RETRY,
                 long_memory_max: float = LONG_MEMORY_MAX) -> None:
        self.normal_ms, self.short_ms, self.memory, self.clock = normal_ms, short_ms, memory, clock
        self.unasked_for = unasked_for
        self.long_ms, self.long_memory, self.long_retry = long_ms, long_memory, long_retry
        self.long_memory_max = long_memory_max
        self._cut: dict[str, float] = {}  # bundle id -> when a normal-budget read of it was last cut short
        self._still_cut: dict[str, float] = {}  # bundle id -> when a read its stillness gave the normal budget was
        # (the app whose next read has the normal budget because its screen is still, when that was decided)
        self._by_still: tuple[str, float] | None = None
        self._tried: dict[str, float] = {}  # bundle id -> when a real read of it last went out
        self._readable: set[str] = set()  # (2.0.3) bundle ids that read whole with the long budget (the daemon's life)
        # bundle id -> when its newest long read that didn't come back whole went out (the block; a whole answer lifts it)
        self._long_cut: dict[str, float] = {}
        # bundle id -> long reads of it in a row that didn't come back whole (its block doubles with each: LONG_MEMORY,
        # 2x, 4x... up to LONG_MEMORY_MAX, for an app that never read whole with the long budget)
        self._long_misses: dict[str, int] = {}
        # (round T, review RS-M1) bundle id -> long reads of it in a row that didn't come back whole, busy ones included:
        # the exponent of its block (block_span). `_long_misses` is the row toward the app-wide stop, which a busy cut
        # doesn't add to; this one only a whole read resets
        self._long_backoff: dict[str, int] = {}
        # bundle id -> (its block, its count, its pictures, its backoff) as they were before the long read of it now out:
        # put back when the answer is another app's (the phone, 2026-11-26: a notification banner's SpringBoard tree
        # answered a read)
        self._long_before: dict[str, tuple[float | None, int | None, list | None, int | None]] = {}
        # (round G2) bundle id -> the pictures its newest long reads that weren't whole were made on (the daemon's mark;
        # None: none kept), each with when it went out, newest last, LONG_PICS at most (round T, review RS-M2); and
        # `other`, set by the session: does the screen now look like another one than that picture (False: can't tell)?
        self._long_pics: dict[str, list[tuple[object | None, float]]] = {}
        self.other: Callable[[object], bool | None] | None = None

    @property
    def long_on(self) -> bool:
        """Is there a long budget (TAPWRIGHT_TREE_LONG_BUDGET above the normal budget)?"""
        return self.long_ms > self.normal_ms

    def block_span(self, bundle: str) -> float:
        """Seconds a long read of `bundle` that didn't come back whole blocks the next: `long_retry` for an app that read
        whole with the long budget before, else `long_memory`; doubled for each such read before it in a row, at most
        `long_memory_max` (review R203c H1: a playing feed in an app that read whole once got about 50 cut 8 s reads an
        hour at a fixed 60 s)."""
        start = self.long_retry if bundle in self._readable else self.long_memory
        # (round T, review RS-M1) by every cut long read in a row, a busy one too: a redrawing feed cut "busy" each time
        # kept the first span forever
        backoff = max(1, self._long_backoff.get(bundle, 1))
        return min(self.long_memory_max, start * 2 ** min(backoff - 1, 30))

    def misses(self, bundle: str | None) -> int:
        """Long reads of `bundle` in a row that didn't come back whole, the one under way included (at least 1)."""
        return max(1, self._long_misses.get(bundle, 1)) if bundle is not None else 1

    def blocked(self, bundle: str | None) -> bool:
        """Is a long read of `bundle` blocked: one went out within its block_span and didn't come back whole, and (round
        G2) the screen still looks like the one it was made on, or it is the LONG_APP_MISSES-th in a row?"""
        return self.block_state(bundle) in ("same", "app", "unseen")

    def block_state(self, bundle: str | None) -> str:
        """(round G2) "" (no block), "same" (the screen the long read failed on: blocked), "other" (another screen of
        the app: not blocked), "app" (LONG_APP_MISSES or more in a row: blocked on every screen), "unseen" (no picture
        to compare: blocked, as before)."""
        at = self._long_cut.get(bundle) if bundle is not None else None
        if at is None or bundle is None or self.clock() - at >= self.block_span(bundle):
            return ""
        if self.misses(bundle) >= LONG_APP_MISSES and bundle not in self._readable:
            # (round S, S-F1) only for an app none of whose long reads has ever been whole in this service's life: a
            # whole one proves some screens read, and from then on only the screen a read failed on is blocked
            return "app"
        pictures, other = self._long_pics.get(bundle), self.other
        if not pictures or other is None:
            return "unseen"
        # (round T, review RS-M2) each kept screen blocks for its own span: the screen now like any of them is "same"
        span, now, unseen = self.block_span(bundle), self.clock(), False
        for picture, when in pictures:
            if now - when >= span:
                continue
            if picture is None:
                unseen = True  # (a long read made with no picture: blocked on every screen while it lasts, as before)
                continue
            try:
                said = other(picture)
            except Exception:  # noqa: BLE001 - a picture that can't be compared: blocked, as before
                unseen = True
                continue
            # (round H, review RG L4) None: no picture since the kept one (the stream sends none for a while after a
            # long read), or none recent: it can't be told, blocked
            if said is None:
                unseen = True
            elif not said:
                return "same"
        return "unseen" if unseen else "other"

    def slow(self, bundle: str | None) -> bool:
        """Is `bundle` slow but readable now: its reads are planned with the long budget (unless something plays)?"""
        return self.long_on and bundle is not None and bundle in self._readable and not self.blocked(bundle)

    def long_try(self, bundle: str | None) -> bool:
        """May a read of `bundle` that the normal budget cut short be asked once more with the long budget (the caller
        then asks the picture across that read: Daemon._still_across)? Not while it is blocked."""
        return bundle is not None and self.long_on and not self.blocked(bundle)

    def blocked_for(self, bundle: str | None) -> float:
        """Seconds the block of `bundle` lasts still (0: none)."""
        at = self._long_cut.get(bundle) if bundle is not None else None
        if at is None or bundle is None:
            return 0.0
        return max(0.0, self.block_span(bundle) - (self.clock() - at))

    def long_sent(self, bundle: str | None, picture: object | None = None) -> None:
        """A long read of `bundle` goes out now: blocked until a whole answer to it (answered) lifts that. `picture`
        (round G2): the screen it is made on (None: none kept), which the block holds for."""
        if bundle is not None:
            kept = self._long_pics.get(bundle)
            self._long_before[bundle] = (self._long_cut.get(bundle), self._long_misses.get(bundle),
                                         None if kept is None else list(kept), self._long_backoff.get(bundle))
            self._long_pics[bundle] = [*(kept or [])[-(LONG_PICS - 1):], (picture, self.clock())]
            self._long_cut[bundle] = self.clock()
            self._long_misses[bundle] = self._long_misses.get(bundle, 0) + 1  # (a whole answer takes it back to 0)
            self._long_backoff[bundle] = self._long_backoff.get(bundle, 0) + 1

    def long_elsewhere(self, bundle: str) -> None:
        """A long read of `bundle` was answered by another app's tree: it says nothing about `bundle`, whose block and
        count go back to what they were before it (not set, not counted, its mark kept)."""
        before = self._long_before.pop(bundle, None)
        if before is None:
            return
        cut, misses, pictures, backoff = before
        if pictures is None:
            self._long_pics.pop(bundle, None)
        else:
            self._long_pics[bundle] = pictures
        if backoff is None:
            self._long_backoff.pop(bundle, None)
        else:
            self._long_backoff[bundle] = backoff
        if cut is None:
            self._long_cut.pop(bundle, None)
        else:
            self._long_cut[bundle] = cut
        if misses is None:
            self._long_misses.pop(bundle, None)
        else:
            self._long_misses[bundle] = misses

    def plan(self, bundle: str | None, still: Callable[[], bool] | None = None,
             long_ok: bool = True) -> tuple[int, bool]:
        """(the budget in ms, whether the read may be left unasked) for a read of `bundle`. (2.0, worker H) Unasked only
        where the short budget applies (the app's memory of a cut read is fresh, and the screen isn't still) and a real
        read of the app went out within `unasked_for` seconds: the caller then asks the runner only which app is in front
        (route version 11), and makes the `root` answer itself when it is still this app. `long_ok` (2.0.3): nothing
        plays on the screen before the read (Daemon._plays) and the caller's time covers a long read; else a slow app's
        read is planned as in 2.0.2 (review R203 M1)."""
        budget = self.budget(bundle, still, long_ok)
        if bundle is None or budget >= self.normal_ms:
            return budget, False
        at = self._tried.get(bundle)
        return budget, at is not None and self.clock() - at < self.unasked_for

    def tried(self, bundle: str | None) -> None:
        """A real read of `bundle` goes out now."""
        if bundle is not None:
            self._tried[bundle] = self.clock()

    def tried_ago(self, bundle: str | None) -> float | None:
        """Seconds since a real read of `bundle` went out (None: none did)."""
        at = self._tried.get(bundle) if bundle is not None else None
        return None if at is None else self.clock() - at

    def budget(self, bundle: str | None, still: Callable[[], bool] | None = None, long_ok: bool = True) -> int:
        """The budget, in ms, for a read of the app `bundle` (None: not known, the normal budget). `still` is asked only
        when the short budget would apply: True gives the normal one. A slow but readable app gets the long one when
        `long_ok` (nothing plays on the screen, and time for it)."""
        if still is not None:
            # (CodeRabbit, PR #26) a read's own plan: a mark a read that raised left (answered() never took it) isn't
            # this read's. (`short()` asks without `still` and leaves the planned read's mark alone)
            self._by_still = None
        if long_ok and self.slow(bundle):
            return self.long_ms
        if (still is not None and bundle is not None and bundle in self._cut and self.block_state(bundle) == "other"
                and still()):
            # (round G2) a still screen of the app that looks like another than the one its long read failed on: the
            # normal budget, so the long try can follow (the short one's memory is of that other screen too)
            self._by_still = (bundle, self.clock())
            return self.normal_ms
        if bundle is None or bundle not in self._cut:
            return self.normal_ms
        if self.clock() - self._cut[bundle] >= self.memory:
            del self._cut[bundle]
            self._still_cut.pop(bundle, None)
            if still is not None and still():
                self._by_still = (bundle, self.clock())  # (cut short again, this read is the still screen's one too)
            return self.normal_ms
        at = self._still_cut.get(bundle)
        if still is not None and (at is None or self.clock() - at >= self.memory) and still():
            self._by_still = (bundle, self.clock())
            return self.normal_ms
        return min(self.short_ms, self.normal_ms)  # (CodeRabbit, PR #26: a normal budget set lower stays the most)

    def short(self, bundle: str | None) -> bool:
        return self.budget(bundle) < self.normal_ms

    def answered(self, bundle: str | None, truncated: bool, budget_ms: int, took_ms: float | None = None,
                 sent_for: str | None = None, partial: bool = False, busy: bool = False, own_use: bool = False) -> None:
        """A read of `bundle` with `budget_ms` came back truncated or full, after `took_ms` (None: not timed). `sent_for`:
        the app a long read was sent for; an answer naming another app is no long read of either (long_elsewhere).
        `partial` (round G): a truncated answer above the root, with elements (shallow): for a long read, no miss.
        `busy` (round S, S-F1): cut while the app was busy (BUSY_WHY): a long read so is no miss in the app's row (its
        count goes back to what it was); the block of the screen it was made on holds as for any cut long read, and
        (round T, review RS-M1) doubles as for any (its backoff counts it). `own_use` (round T, review RS-L3): a read for
        an action's own use with no long budget (NO_LONG: type_text's field): cut, it leaves a readable app's budget
        memory alone (the short budget would answer a later read with less time "0 elements")."""
        held, self._by_still = self._by_still, None
        # (review K, question 4) a read that raised never came here: what its stillness gave is not the next read's
        by_still = held[0] if held is not None and self.clock() - held[1] < BY_STILL_FOR else None
        if bundle is None:
            return
        long = self.long_on and budget_ms >= self.long_ms
        if long and sent_for is not None and bundle != sent_for:
            # (the phone, 2026-11-26) a banner's SpringBoard tree answered a slow app's planned read in 0.1 s: the app was
            # blocked by its own read, every look for a minute answered "0 elements", and SpringBoard was marked slow.
            # Only a cut or silent long read of the app itself blocks it or makes its block grow
            self.long_elsewhere(sent_for)
            long = False
        elif long and truncated and partial:
            # (round G, phone 2026-11-27) a long read of a readable app that gave elements, only not all (46 of a home
            # screen's 140): counted a miss, it blocked the next 60 s, and the agent's tap by an id that read listed got
            # "this screen's elements weren't read". No block, no count: the block and the count stay as they were
            # (neither started nor lifted: only a whole answer lifts them)
            self.long_elsewhere(bundle)
            long = False
        elif long and truncated and busy:
            before = self._long_before.pop(bundle, None)
            misses = before[1] if before is not None else None
            if misses is None:
                self._long_misses.pop(bundle, None)
            else:
                self._long_misses[bundle] = misses
        elif long and bundle is not None:
            self._long_before.pop(bundle, None)
        if long and truncated:
            self._long_cut[bundle] = self.clock()  # (2.0.3) not even the long budget did: 2.0.2's rules, and the block
            if kept := self._long_pics.get(bundle):
                kept[-1] = (kept[-1][0], self._long_cut[bundle])  # (its screen's block from the answer, as the app's)
        elif long:
            # the app the long read was sent for, which the answer names (or names none)
            self._readable.add(bundle)
            self._long_cut.pop(bundle, None)
            self._long_misses.pop(bundle, None)
            self._long_backoff.pop(bundle, None)
            self._long_pics.pop(bundle, None)
        if not truncated:
            # (review K-H1) the screen in front reads: the next read is asked for real. Left unasked, it was made up
            # as a root with no elements, for a screen that had just given all of them
            self._tried.pop(bundle, None)
            # (CodeRabbit, PR #30) a long read that came back whole in 0.4 s doesn't clear it either: a moving screen of
            # the app is read as in 2.0.2 (the short budget); a still one has the long budget by `_readable`
            if took_ms is None or took_ms >= self.short_ms:
                self._cut.pop(bundle, None)
                self._still_cut.pop(bundle, None)
        elif budget_ms >= self.normal_ms and not (own_use and bundle in self._readable):
            self._cut[bundle] = self.clock()
            if by_still == bundle:
                self._still_cut[bundle] = self.clock()  # its still screen didn't answer either

    def forget(self) -> None:
        self._cut.clear()
        self._still_cut.clear()
        self._by_still = None
        self._tried.clear()
        # (review R203 H2) not `_readable`, `_long_cut`, `_long_misses`, `_long_backoff` nor `_long_pics`: a reconnect changes nothing about an app's
        # screens, and a
        # paused video that restarted the runner was given a new 8 s try every time


SHORT_SETTLE = 0.5  # seconds an action's settle may wait on a screen that never stands still (SettleCap)
READABLE_SETTLE = 1.0  # the same while that app's newest tree is whole: its elements are read after it, and a tree read
#                        in the middle of a page's push lists both pages (a push takes 0.35-0.5 s; a video hides its end)
LAUNCH_SETTLE = 2.0  # the same after its launch: 0.5 s after one the app wasn't up yet (phone, 2026-11-26)


class SettleCap:
    """(2.0, lead's decision) How long an action's answer waits for the screen of an app to stand still. A screen that
    never does (TikTok's playing feed) made every settle wait its whole cap (2 s, 4 s after a launch): a swipe there took
    3.2 s. So, per bundle id: when a settle of the app ran past `short` seconds with the picture still changing (it ended
    by its cap, or stood still only later) while the newest tree of that app was cut short, its next settles wait at most
    `short` seconds; a settle that ends still while the app's newest tree is whole, or a read of it that comes back whole,
    gives it the normal cap again. Pure: the daemon says what happened and asks for the cap.

    (Worker H, phone log 2026-11-26) It switched only on the third swipe on the feed: the first two settles ended still
    (0.77 s and 1.64 s: the feed stands still for a moment while the next video starts), and only a capped settle
    counted, while a still end cleared the memory again. Now a settle still moving at `short` seconds counts (the short
    cap would have ended it there), and a still end on a screen that still doesn't read keeps the short cap."""

    def __init__(self, short: float = SHORT_SETTLE, readable: float = READABLE_SETTLE,
                 launch: float = LAUNCH_SETTLE) -> None:
        self.short, self.readable, self.launch = short, readable, launch
        self._restless: set[str] = set()

    def cap(self, bundle: str | None, cap: float, cut: bool = True, launch: bool = False) -> float:
        """The cap for a settle of `bundle`'s screen whose normal cap is `cap`. `cut`: the app's newest tree was cut
        short (False: it reads, and the settle is given `readable` seconds). `launch`: the settle after its launch."""
        if bundle is None or bundle not in self._restless:
            return cap
        return min(cap, self.launch if launch else self.short if cut else self.readable)

    def settled(self, bundle: str | None, capped: bool, cut: bool, seconds: float = 0.0,
                unread: bool | None = None) -> bool:
        """A settle of `bundle` ended after `seconds`: by its cap with frames still changing (`capped`), else on frames
        that stood still; the newest tree of the app cut short (`cut`). `unread`: that tree gave no elements at all
        (None: as `cut`). True when the app's cap switched (the caller logs it).
        (review K-M2) A settle that stood still, only later than the short cap, counts as moving on a screen that gave
        no elements (a feed between two videos). On one that was read in part (a heavy page) it is an ordinary push:
        counting it capped that app's settles at half a second from then on, so its next page was read mid-push."""
        if bundle is None:
            return False
        nothing_read = cut if unread is None else unread
        moving = capped or (seconds > self.short and nothing_read)  # still changing when the short cap would have ended it
        if moving and cut and bundle not in self._restless:
            self._restless.add(bundle)
            return True
        if not capped and not cut and bundle in self._restless:
            self._restless.discard(bundle)
            return True
        return False

    def read_whole(self, bundle: str | None) -> bool:
        """A read of `bundle` came back whole. Nothing switches (False): its feed may read whole for a moment and then
        not again (TikTok's after a launch, phone 2026-11-26), and each flip back to the normal cap cost a 2 s settle.
        The cap is `readable` while the tree is whole (cap), and the normal one again once a settle ends still."""
        return False

    def restless(self, bundle: str | None) -> bool:
        return bundle is not None and bundle in self._restless

    def forget(self) -> None:
        self._restless.clear()


PINCH_GAP = 60.0  # points between a pinch's two fingers at its narrow end
PINCH_MARGIN = 10.0  # points a pinch's fingers stay inside the screen's edges
PINCH_MOVE_MIN = 4.0  # points each finger must travel: less, and a pinch is a still two-finger press


def pinch_spread(x: float, y: float, scale: float, screen: tuple[float, float]) -> tuple[float, float]:
    """(start, end) distance of each of a pinch's two fingers from (x, y), along the horizontal line through it: from
    PINCH_GAP / 2 out to `scale` times that (scale > 1), or the other way (scale < 1), kept PINCH_MARGIN inside the
    screen. Near an edge the wide end is what fits and the narrow end that divided by the scale, so the fingers still
    move (CodeRabbit, PR #26: both were capped to the room, a still two-finger press); bad_params when they can't move
    PINCH_MOVE_MIN points (nothing is touched)."""
    room = min(x, screen[0] - x) - PINCH_MARGIN
    ratio = max(scale, 1 / scale)
    wide = min(PINCH_GAP / 2 * ratio, room)
    narrow = min(PINCH_GAP / 2, wide / ratio)
    # (review N-M1) what the scale itself asks for is never too little: only the edge's cap can be. A scale of 1.1
    # in the middle of the screen moves each finger 3 points, and was refused as "too near the edge"
    if wide - narrow < min(PINCH_MOVE_MIN, PINCH_GAP / 2 * (ratio - 1)) - 1e-9:
        raise Refused("bad_params", f"({x:g}, {y:g}) is too near the screen's edge for a pinch's two fingers to move; "
                                    "pinch farther from it. Nothing was touched.")
    return (narrow, wide) if scale >= 1 else (wide, narrow)


# (2.0.3, review RP) what is in front, as an answer showed it to the agent or as a touch finds it: the runner's front route,
# and iOS's process list when that names SpringBoard. Never a tree's root: a read during a banner names SpringBoard.
FRONT_APP = "app"  # an app in front
FRONT_HOME = "home"  # SpringBoard's own screen, no app in the foreground (the home screen)
FRONT_OVER = "over"  # SpringBoard over an app: a banner, an alert, Notification or Control Center, an expanded notification
FRONT_UNKNOWN = "unknown"  # the route or the process list couldn't say


@dataclass(frozen=True)
class Front:
    """One of the states above; `app` is the app in front (FRONT_APP) or under SpringBoard (FRONT_OVER). `guessed`
    (review RF H1): FRONT_APP taken from iOS's process list alone because the route couldn't name the app (front_state).
    That list never has SpringBoard, so it can't tell the app alone from a notification over it: the weakest state."""
    kind: str
    app: str | None = None
    guessed: bool = False

    def same(self, other: "Front | None") -> bool:
        """The same state, whether or not either is a guess."""
        return other is not None and self.kind == other.kind and self.app == other.app


@dataclass(frozen=True)
class TouchRules:
    """(2.0.3) What an agent's touches in one action are checked against (Session._front_rules): `seen`, the front state
    the agent's last answer showed it (None: none was recorded), and `element`, whether the action aims at an element by
    ref, id or label from the agent's list (it may then touch SpringBoard's own UI it saw, a banner's row included).
    `flick` (round G): the action is the agent's `swipe` up off the top edge (FLICK_TOP), the way a person sends a banner
    away: on a banner it goes at once. `aimed` (round H): the frame of the element aimed at, as the agent's list has it
    (a hit inside it is that element, not a banner over it)."""
    seen: Front | None
    element: bool = False
    flick: bool = False
    aimed: "tuple[float, float, float, float] | None" = None


# (round G, phone 2026-11-27, iOS 26.5) the hit test at a point of the banner zone: what is there, by SpringBoard's own
# elements. The status bar is SpringBoard's at all times (in an app: [0, 0, 430, 54], the island [122, 11, 156, 37], the
# clock [46, 11, 40, 22], [370, 21, 31, 15]); on the home screen the top edge answers SpringBoard's whole screen
# [0, 0, 430, 932], a widget its own app, an icon SpringBoard's `Icon`. A timer's banner was SpringBoard's `Other`
# [11, 11, 408, 490], over an app and over the home screen alike. So "over" is SpringBoard's element, not an icon, not
# the whole screen, reaching below HIT_BANNER_BELOW, where the status bar ends. While that banner showed, the front route
# and activeAppInfo named the app the whole time: the hit test, 7-32 ms a question (a heavy app too), is the only word.
HIT_BANNER_BELOW = 64.0
HIT_NOTHING = "tapwright hit: no element answered at the point"
# (round H, review RG M6; phone 2026-11-27) the runner says HIT_NOTHING also when its lookup got no answer in 0.3 s: on a
# video app's feed it said so at every point, the status bar's included, 309-342 ms each, for two rounds, then three of
# five points named the app. So it is an answer (nothing there) only above HIT_NOTHING_ABOVE (the screen's top edge,
# above every status-bar frame measured); below, it is BANNER_SLOW, never asked again (round K: the status bar's points
# said it only because they were asked after the app's, which held the runner)
HIT_NOTHING_ABOVE = 11.0
# (round K) a question that got "no element answered" is never asked again: each holds the runner about 2 s, and three
# at a hung app's point cost the connection. One deadline for the one or two questions of a call (the status bar's, then
# the point's when the status bar can't say)
HIT_TRIES, HIT_TRIES_FOR = 1, 1.2
HIT_TRY_MIN = 0.2  # seconds: less than this left of HIT_TRIES_FOR asks no more
# (round H, review RG M5; phone 2026-11-27) after a hit question got no answer in HIT_BANNER_WAIT, no other is sent for
# this long: the runner is still working on it, and a dozen queued ones held it until the watchdog restarted it (the
# phone then asked for its passcode)
HIT_QUIET = 5.0
IN_CALL = "com.apple.InCallService"  # (review RG H2) a call's banner: over, whatever its frame
# (round K, phone 2026-11-27) the status bar's clock, in portrait points, on a phone with an island or a notch: asked
# first over an app (Session.banner_at). SpringBoard answers there in tens of ms as long as nothing is pending (26 ms
# right after a read cut short), a timer's banner covers it ([11, 11, 408, 490]), and an ordinary banner doesn't (its
# plate [8, 56, 414, 73] is below the status bar): so the clock tells whether SpringBoard is answering, and the touch's
# point, asked once after it, tells what is there. A home-button phone's clock isn't there: no witness.
HIT_WITNESS = (66.0, 22.0)
# (round K) a "no element answered" that took at least this long is the runner's 0.3 s wait running out: an app that
# didn't answer (304-342 ms measured); a faster one is SpringBoard saying "nothing here right now" (8-18 ms: a banner
# coming or going)
HIT_NOTHING_SLOW = 0.25
# (round M, review RL H1) two questions, two lines. Is the runner held? Any "no element answered" that took at least
# HIT_NOTHING_SLOW (absolute) sets the hold, whatever the verdict: a hold set by mistake costs one question at the clock,
# a hold missed costs the connection. Is it clear? Only when the clock's first answer was prompt (under
# HIT_WITNESS_PROMPT: a link already slow can't vouch for anything) and the point's and the confirm's "nothing" each
# took at least HIT_NOTHING_MARGIN longer than it (the phone: the clock 26-60 ms, the busy point 304-342 ms)
HIT_WITNESS_PROMPT, HIT_NOTHING_MARGIN = 0.15, 0.2
# (round K) after a slow "no element answered" the runner is held until the app answers XCTest's pending request: every
# hit question and the front route get nothing meanwhile (2.5 s on a shopping app, 9.8 s on a hung feed, measured after
# one question). Requests through accessibility wait for it (Session.channel_free), asking the clock every
# HIT_FREE_EVERY s, HIT_FREE_WAIT s at most a question
HIT_HELD, HIT_FREE_EVERY, HIT_FREE_WAIT = 12.0, 0.7, 1.0
# (2.0.3 round T, review RS-H1) the least pause between two of those clock questions, whatever the last one took: a
# wait for the runner never asks back to back
HIT_FREE_MIN = 0.25
# (round N, phone 2026-11-28 N-F2) a hold that outlasts HIT_HELD is given up: nothing more goes into the runner through
# accessibility (the front route asked into it stalled, was taken for a lost line, and a reconnect and a new session
# followed: 82 s); the app in front, named by iOS's process list (HELD_FRONT_WAIT at most), isn't answering
# (RunnerHeld) until the clock answers again: each later call asks it once, HELD_ASK_WAIT at most
HELD_FRONT_WAIT, HELD_ASK_WAIT = 3.0, 0.45
# (round N) runner 16 times its own point lookup (inside XCTest's accessibility timeout, 0.3 s, instead of a request
# with none: N-F3) and says so after the words of a "no element answered": "(hit 304 ms, client)", with the
# accessibility error's short name when there was one. Judged by that time (the phone's own), at least HIT_PHONE_SLOW is
# slow (the app at that point didn't answer in time), under it fast (nothing is there right now: a banner in motion);
# the link's delay no longer counts. Runner 15, a refusal with no time, or one asked the daemon's way: the daemon's own
# clock and the rules of rounds K-M
HIT_PHONE_SLOW, PHONE_TIME_VERSION = 0.25, 16
# (round O, review RN-M1) runner 16 after c9dd3486 says the lookup's own time, then the whole (the AX timeout's lock,
# install and restore): "(hit 12 ms, all 272 ms, client, ...)". The verdict is the lookup's own time; "all" and the
# error's name go to the trace. A refusal of 4392284f's build (no "all") is read as round N read it
_PHONE_TIME = re.compile(r"no element answered at the point \(hit (\d+(?:\.\d+)?) ms(?:, all (\d+(?:\.\d+)?) ms)?, "
                         r"(client|daemon)(?:, ([^)]+))?\)")
# (round L, review RK M1) how young the clear of a banner wait's last poll must be for the hands' gate that runs again
# right after it (under the hands' lock, a hold known) to take it instead of asking
GATE_FRESH = 1.0
WEB_CONTENT = "com.apple.WebKit."  # (the phone: a web page's content in the zone names WebKit's content process)
HIT_BANNER_WAIT = 1.5  # seconds the gate waits for the hit test's answer before it can't say
BANNER_OVER, BANNER_CLEAR, BANNER_UNSURE, BANNER_UNASKED = "over", "clear", "unsure", "unasked"
BANNER_SLOW = "slow"  # (round H) the app in front is slow to answer: what is at the top can't be told now
FLICK_TOP = 10.0  # points from the top edge within which a flick up ends
# (round H, review RG H1) a flick moves at least this far up and lasts at most this long: a 7 pt "flick" is a tap on the
# banner (SWIPE_IS_TAP), and a slow one a long press (it opens the notification's actions)
FLICK_MIN, FLICK_SECONDS = 40.0, 0.5

# (review RP2 L2, M4) every refusal of the rules starts with its reason word; a screen where the route can't name the app
# says how to go on: open_app names it (and is exempt), then look
UNKNOWN_FRONT = ("unknown_front: couldn't tell which app is in front of the phone, so nothing was touched; open_app the "
                 "app you mean (that names it), then look and try again.")
UNSEEN_FRONT = ("unknown_front: your last look couldn't tell which app was in front, so nothing was touched; look again "
                "(or open_app the app you mean, then look), then try again.")
NO_LOOK = "no_look: no look yet in this session shows what is in front, so nothing was touched; look first."
COVERED_BANNER = ("covered: a notification is over the top of the screen, so nothing was touched; wait a few seconds, "
                  "then look and try again. A timer, an alarm or a call stays until answered: send it away with a quick "
                  "swipe from it up off the top edge, at least 40 pt long and at most 0.5 s (swipe {x0: 215, y0: 70, "
                  "x1: 215, y1: 2}), then look.")
# (round H, review RG M5, M6) the hit test got no answer: the app in front is slow, not a banner
# (round I, the lead on review RH M2) an element is refused too: the phone showed the hit test silent at a banner's own
# points over a slow app, and one touch on a call's banner is too much
COVERED_SLOW = ("covered: what is at the top of the screen can't be told right now, because the app in front is slow to "
                "answer, so nothing was touched; try again in a moment, or aim lower. If a notification is there and "
                "stays, a quick swipe from it up off the top edge sends it away (swipe {x0: 215, y0: 70, x1: 215, "
                "y1: 2}).")
# (round G) the hit test couldn't say what is at the top of the screen, though the route names the app
COVERED_UNSURE = ("covered: can't tell whether a notification is over the top of the screen (the phone's runner didn't "
                  "answer what is there), so nothing was touched; aim at an element (by ref, id or label), or lower.")
# (round N, phone 2026-11-28 N-F5) the hit test answered another process's whole screen, neither SpringBoard nor the app
# in front: a system sheet over the app (a rating prompt's, every point answering it in 11-13 ms). Refused as before,
# with what it is
COVERED_SHEET = "covered: another app's sheet ({bundle}) is over the screen; look again."
SHEET_NOTE = "Note: another app's sheet ({bundle}) is over the screen; look again."
COVERED_HOME = ("covered: a notification may be over the top of the home screen, so nothing was touched; aim at an "
                "element (by ref, id or label), or look again.")
# (review RF H1) the app in front known only from iOS's process list, which can't see a banner
COVERED_GUESS = ("covered: can't tell whether a notification is over the top of {app} (the phone's runner couldn't name "
                 "the app in front), so nothing was touched; aim at an element (by ref, id or label), or lower.")


class _BannerUp(Exception):
    """(review RP2 H1) The front check would wait a banner out: raised inside the hands' `before`, so the wait happens
    with the hands' lock let go (Session._by_hands), and the guard and the check run again right before the touch."""


class RunnerHeld(AppNotAnswering):
    """(2.0.3 round N, N-F2) The runner was held past HIT_HELD by the app's pending accessibility request (the clock
    said a slow "no element answered", or nothing, the whole while): nothing through accessibility is sent into it (the
    front route, a tree read), and the line isn't taken for lost on the strength of such a request: the runner answers.
    The daemon answers `not_answering` until the clock answers again; close_app, open_app and the home button are how
    an agent gets out. The app is named from iOS's process list (the device channel), never from the runner."""

    def __init__(self, bundle_id: str, name: str) -> None:
        super().__init__(bundle_id, name)
        if not bundle_id:  # (iOS's process list couldn't say, and no read named the app)
            self.args = ("the app in front isn't answering, so nothing on the phone can be read",)


# (2.0.3 round Q) No shipped runner answers this way: tw-16.15.3.16 (not shipped) answered "daemon" every time on iOS
# 26.5 (its client lookup raised), so phone_time is None for every refusal 2.0.3 meets and runner 15's rules decide.
# Kept, unchanged, for the runner of 2.0.4.
def phone_time(said: str, version: int) -> float | None:
    """(round N) The phone's own time of a "no element answered", in seconds, from runner 16's words ("(hit 304 ms,
    client...)"); None from an older runner, words with no time, or a lookup asked the daemon's way (runner 15's rules
    then)."""
    if version < PHONE_TIME_VERSION:
        return None
    found = _PHONE_TIME.search(said)
    if found is None or found.group(3) != "client":
        return None
    return float(found.group(1)) / 1000.0


def phone_words(said: str) -> dict:
    """(round O) What a runner-16 refusal says besides the lookup's time, for the trace: {"all": ms, "how": way,
    "err": the error's or the exception's name}, each only when said."""
    found = _PHONE_TIME.search(said)
    if found is None:
        return {}
    told = {"hit": float(found.group(1)), "how": found.group(3)}
    if found.group(2) is not None:
        told["all"] = float(found.group(2))
    if found.group(4):
        told["err"] = found.group(4)
    return told


def slow_nothing(phone: float | None, took: float) -> bool:
    """(round N) Is a "no element answered" slow: by the phone's own time when runner 16 gave it (HIT_PHONE_SLOW), else
    by the daemon's clock (HIT_NOTHING_SLOW, rounds K-M)."""
    return phone >= HIT_PHONE_SLOW if phone is not None else took >= HIT_NOTHING_SLOW


class _HeldUp(Exception):
    """(round L, review RK M1) The hands' gate found the runner held: raised inside `before`, so the wait for it
    (channel_free) happens with the hands' lock let go, then the touch starts again."""


class FrontUnchecked(Refused):
    """A tap whose app-in-front check was left to the fork's tap (`front_apps`) wasn't made: the runner said another
    app is in front, or the touch couldn't go by that route, or something else was about to act first. Nothing was
    touched; the daemon asks the phone its own way, as it did before (daemon.Rescope)."""

    def __init__(self, why: str) -> None:
        super().__init__("rescope", why)


def _field_key(field: ui.Element) -> str | None:
    """What says two reads show the same input: its identifier, else its label (None when it has neither)."""
    name = field.identifier or field.label
    return f"{field.role}:{name}" if name else None


PLACE_SLACK = 2.0  # points a field's frame may differ between two reads (or WebDriverAgent's rect) and be the same place


def _alike(e: ui.Element, field: ui.Element) -> bool:
    """Would e pass for the field by name: an input of its role with its identifier (else, without one, its label)."""
    return e.role == field.role and (e.identifier == field.identifier if field.identifier else e.label == field.label)


def _near(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    return len(a) == len(b) and all(abs(p - q) <= PLACE_SLACK for p, q in zip(a, b))


def _moved_with_content(field: ui.Element, now: ui.Element, before: list[ui.Element], after: list[ui.Element]) -> bool:
    """Did the field move only as the content around it did (the keyboard scrolled the form after the tap)? It moved
    up or down, not sideways and not resized, and another element with a name of its own, the one of its kind in
    both lists, moved by the same distance. A field the app replaced by another with its key elsewhere has none."""
    dy = now.y - field.y
    if abs(now.x - field.x) > PLACE_SLACK or abs(now.width - field.width) > PLACE_SLACK:
        return False

    def key(e: ui.Element) -> tuple[str, str, str]:
        return e.role, e.label, e.identifier

    for e in before:
        if e is field or not (e.identifier or e.label) or _alike(e, field):
            continue
        twins = [a for a in after if key(a) == key(e)]
        if (len(twins) == 1 and sum(1 for b in before if key(b) == key(e)) == 1
                and _near((twins[0].x, twins[0].y, twins[0].width), (e.x, e.y + dy, e.width))):
            return True
    return False


def _sole_input(field: ui.Element, before: list[ui.Element], after: list[ui.Element], strict: bool) -> ui.Element | None:
    """The fresh read's input that is the field, when the field is the only input in the agent's list and there is only
    one in the fresh read too, and it moved up or down only (x within PLACE_SLACK; the same width too, except for the
    check before a clear: a search field narrows as its Cancel button comes in). Else None (last Opus review, M1)."""
    was = [e for e in before if e.role == "input"]
    now = [e for e in after if e.role == "input"]
    if len(was) != 1 or was[0] != field or len(now) != 1 or abs(now[0].x - field.x) > PLACE_SLACK:
        return None
    if not strict and abs(now[0].width - field.width) > PLACE_SLACK:
        return None
    return now[0]


class _FieldGone(Exception):
    """The element WebDriverAgent named a moment ago went stale while it was checked (the app redrew its view). The
    caller says what happened so far (1.7.8, Opus review 8)."""


KEYS ={"backspace": "\b", "enter": "\n"}  # what /wda/keys types for these keys
ROOT_BUNDLE = re.compile(r'<XCUIElementTypeApplication[^>]*?\bbundleId="([^"]*)"')


def _jpeg_of(value: object, what: str) -> bytes:
    """The JPEG bytes of a runner answer's `jpeg` (base64, route version 11). Raises DeviceError when there are none."""
    data = value.get("jpeg") if isinstance(value, dict) else None
    if not isinstance(data, str) or not data:
        raise DeviceError(f"the runner's {what} came without a picture")
    try:
        picture = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise DeviceError(f"the runner's {what} sent a picture that can't be read") from None
    if not picture.startswith(b"\xff\xd8"):
        raise DeviceError(f"the runner's {what} sent no JPEG")
    return picture


def _root_bundle(source: object) -> str | None:
    """The bundle id the tree's root Application names, or None."""
    found = ROOT_BUNDLE.search(source[:2000]) if isinstance(source, str) else None
    return found.group(1) or None if found else None


@dataclass
class Snapshot:
    """One read of the screen: the tree, the foreground app, and the pixel scale."""
    tree: ui.Tree
    app: tuple[str, str] | None
    scale: float | None


class Session:
    def __init__(self, device: Device):
        self.device = device
        self.sid = None
        self.screen = (390.0, 844.0)
        self.scale: float | None = None  # pixels per point, read once per connection; None while unknown
        self.scale_read = False  # whether that read has happened, so an unknown scale isn't asked for again
        self.items: list[ui.Element] = []
        self.app: tuple[str, str] | None = None  # (name, bundle id) at the last read
        # (name, bundle id) the newest read of any kind names (a tree-only read's root; None: it named none)
        self.read_app: tuple[str, str] | None = None
        self.last_alert: ui.Alert | None = None  # the OS alert and keyboard state at the last snapshot
        self.last_keyboard = False
        self.last_tree: ui.Tree | None = None  # the tree of the last read, for the places the daemon recognises
        self.retyped = False  # the last type_text cleared the field and typed once more (see type_text)
        self.last_insets: tuple[float | None, float | None] = (None, None)  # where the top and bottom bars end (y)
        self.last_point: tuple[float, float] | None = None  # where the last tap, swipe or drag began, in points
        self.app_names: dict[str, str] = {}  # bundle id -> display name, from the installed apps
        self.not_installed: set[str] = set()  # bundle ids already looked for in the installed apps and not found
        self._apps: tuple[float, list[dict]] | None = None  # (when, the installed apps as the phone last listed them)
        self._apps_task: asyncio.Task | None = None  # the list being read again behind a call
        self._apps_asked = self._apps_kept = 0  # (how many reads of the list began; which of them the kept list is)
        # (field key, element id) of the input our last type_text verified and typed into, while nothing else has
        # touched the phone since: typing there again needs no tap. Every other action clears it.
        self.typed_into: tuple[str, str] | None = None
        # where that input was listed then (x, y, width; a text view grows as it is typed into): typing there again
        # without a tap needs the target listed at that very place, and its key the only one in the list (1.7.8)
        self.typed_place: tuple[float, float, float] | None = None
        self._placed_proof: str | None = None  # the element this type_text proved by its place in a fresh read
        self.offscreen: list[ui.Element] = []  # what the last read found outside the screen (for `off_screen`)
        # what a sheet hides, each with the element in front that names the sheet (for `covered`)
        self.behind: list[tuple[ui.Element, ui.Element | None]] = []
        self.front_items: list[ui.Element] = []  # the list as back_point last read it
        # Degrees the stream's frames are turned clockwise to stand upright: 0 in portrait. In landscape the tree
        # and window are 932x430 while WebDriverAgent's frames stay as captured (measured 2026-11-11).
        self.turn = 0
        self._turn_asked = -float("inf")
        # Awaited with a touch's length in ms just before each touch while it is set: the daemon sets it for the
        # agent's actions (Tapline is told the touch is the agent's), never for a person's input from the live view
        self.touch_hook = None
        # A one-use authorization check installed by the daemon. It runs at HTTP dispatch, after banner waits,
        # touch_hook, session setup and runner probing, and is retained across a refused fast-route fallback.
        self.before_input = None
        self.execution_guard = None  # persistent ownership check for every mutation in an agent turn
        self.input_point: tuple[float, float] | None = None
        self.banner_guard = False  # set by the daemon for the agent's actions: see _clear_of_banner
        # (2.0.3, safety, review RP) set by the daemon for an agent's action: what its touches are checked against, right
        # before each one goes out (_gate); None: no check (a person's input, an action that changes the app on purpose)
        self.touch_rules: TouchRules | None = None
        self._gate_at: tuple[float, float] | None = None  # the point of the touch whose check is still to come
        self._gate_spent: tuple | None = None  # (CodeRabbit, PR #30) the touch's check that ran, for _regate
        self.found_front: Front | None = None  # (review RP2 M2) the app alone in front, as a touch of this action found it
        self.tw_answered: int | None = None  # (review RP2 M1) the last route version a runner answered (kept across starts)
        self.opened_app: str | None = None  # (review RP2 M4) the app the agent's newest open_app named
        # whether one of the agent's touches since the daemon last cleared it began where a banner comes down: a
        # banner landing between the check and the touch takes it (Daemon._taken_away)
        self.top_touch = False
        self.fast_tap: bool | None = None  # whether this session's runner has the fast tap route (None: not asked yet)
        self.fast_drag: bool | None = None  # and the fast drag route (asked with the tap's)
        self.tw_version = 0  # the fork's route version the runner answered with (0: Appium's build, or not asked yet)
        self.fast_read: bool | None = None  # whether the compact read works on this runner (None: not tried yet)
        self.last_compact: tuple[str, str] | None = None  # (token, XML) of the last compact read: "unchanged" reuses it
        self.last_source: str | None = None  # the XML of the latest tree read, for placing a hit test's element in it
        # Set by the daemon for a personal-mode tap whose app-in-front check the fork's tap makes (see FRONT_IN_TAP):
        # the given bundle ids. While it is set, nothing else may act on the phone (FrontUnchecked); the tap that
        # passes the check clears it.
        self.front_apps: tuple[str, ...] | None = None
        # WebDriverAgent settings every new session gets right after POST /session (ensure): without them a session
        # re-created mid-run waits for animations (a blinking caret) on every read, 1 to 3 s each. The daemon sets
        # the stream's preset into it too (stream.PRESETS), so a re-created session has the current quality as well.
        self.session_settings: dict = dict(NO_IDLE_WAIT)
        # Called with each tree the daemon's reads build (refresh, snapshot): what tells it an app showed a one-time code
        # field (§20.4). Never with Notification Center's tree, which is read raw (notifications.py).
        self.on_tree = None
        # Tapwright 2.0: the HID hands (hands.Hands), set by the server for the real phone; None (tests' fakes, or no
        # hands): every touch goes through the runner. motion_rng shapes human-like taps and swipes (motion.py).
        self.hands: "hands_module.Hands | None" = None
        self.hands_at: float | None = None  # time.monotonic() when the hands last finished a gesture
        self.hands_connection: int | None = None  # Hands.connection as that gesture went out
        self.motion_rng = random.Random()
        # (2.0, route version 9) how the newest tree read came back: "full", "shallow" (part of the tree, the runner's
        # budget ran out) or "root" (the application node alone: the screen doesn't answer accessibility reads); and
        # the runner's short reason for a cut. A runner older than 9 always reads the whole tree: "full".
        self.last_tier = "full"
        self.last_why = ""
        self.budgets = ReadBudget()
        # (2.0.3, brief Q fix 3) apps whose slowness a call's answer was told of (LONG_NOTE): not again until a long read
        # of the app is cut (_long_read); a session's own memory, kept through reconnects as the budgets are
        self.slow_told: set[str] = set()
        # Set by the daemon: does the stream show the screen still right now (Daemon._proven_still)? A still screen's
        # read gets the normal budget even while its app is on the short one (ReadBudget). None: never taken as still.
        self.screen_still: Callable[[], bool] | None = None
        self.budget_app: str | None = None  # the app the newest bounded read was budgeted for or named (L2)
        # (2.0.3) the picture across a read, from the daemon's stream: `still_mark` before the read ((the newest frame,
        # fresh, kept, or None; with `check`, whether something plays (Daemon._plays))), `still_across` after
        # a cut read (may the long try go out: did a fresh frame change no more than STILL_BOX from the kept one?). The
        # runner sends no frames while it reads (review R203 H1). Without them (no daemon) no long read is made.
        self.still_mark: Callable[[bool], Awaitable[tuple[object | None, bool]]] | None = None
        self.still_across: Callable[[object], Awaitable[bool]] | None = None
        # (round G2) does the screen now look like another one than this kept picture (the daemon's stream)? The long
        # read's block holds only on the screen it failed on (ReadBudget.block_state)
        self.other_screen: Callable[[object], bool | None] | None = None
        # (round G) the runner's newest answer of the app in front, and when (front_app): a read is planned by it
        self.front_seen: tuple[str, float] | None = None

        self.center_tier = "full"  # how much of Notification Center the newest read_center got (R200v L4)
        self.settles = SettleCap()  # (2.0) which apps' screens never stand still: their settles wait less
        self.bounded = False  # whether this session's runner said version 9 or later at ensure (bounded reads)
        # whether this session's start had the pixel scale from the runner's screen route (version 9), which doesn't read
        # the app: then nothing asks /wda/screen for it, which reads the app (worker H: on TikTok's feed, the AX timeout)
        self.scale_by_runner = False
        self._made_root: str | None = None  # the newest root made here without asking for the tree (_unasked_root)

    async def ensure(self) -> None:
        """Connect, mount and start WebDriverAgent when needed. Once there is a session this is
        free: a dead line shows up as ConnectionLost on the next request instead."""
        if self.device.rsd is None:
            await self.device.connect()
            await self.device.mount()
        if self.sid is not None:
            return
        runner = store.load_state().get("runner_id")
        if not runner:
            raise NotSetUp("WebDriverAgent isn't set up on this phone yet: run `tapwright setup`")
        await self.device.start_wda(runner)
        # (2.0) the runner's route version, by the route that needs no session and reads nothing of the app. Version 9
        # bounds every accessibility read by twAXTimeout, which each POST /session resets: given as a capability it
        # bounds the session's own start too (RUNNER-V9-PROTOCOL §2), and the screen's size comes without reading the
        # app, so a screen that never answers doesn't hold the connection up
        version = await self._probe_version()
        bounded = version is not None and version >= BOUNDED_VERSION
        wanted = {"twAXTimeout": AX_TIMEOUT} if bounded else {}
        sid = (await self.device.http("POST", "/session", {"capabilities": {"alwaysMatch": wanted}}))["sessionId"]
        # Asked before anything reads the app: once a read hangs behind a frozen app, nothing else gets an answer.
        locked = (await self.device.http("GET", f"/session/{sid}/wda/locked", timeout=LOCK_WAIT)).get("value")
        self.scale_by_runner = False
        try:
            screen = await self._screen_of_runner() if bounded else None
            if screen is None:
                size = (await self.device.http("GET", f"/session/{sid}/window/size", timeout=FIRST_READ_WAIT))["value"]
                screen = (float(size["width"]), float(size["height"]))
        except ConnectionLost as e:
            raise await self._why_stuck(bool(locked), e) from e
        self.screen = screen
        # (review R200 H1) the phone may have turned while the line was down: upright is turn 0, and a landscape side
        # is asked again by the next read (hands map points by `turn`, so a stale one puts a touch elsewhere)
        self.turn = 0 if screen[0] <= screen[1] else TURN_UNKNOWN
        self._turn_asked = -float("inf")
        settings = dict(self.session_settings)
        if bounded:
            settings["twAXTimeout"] = AX_TIMEOUT  # a ceiling for every accessibility read besides the tree's budget
        await self.device.http("POST", f"/session/{sid}/appium/settings", {"settings": settings},
                               timeout=READ_CUT)  # (before sid is committed: no read can run without them)
        self.fast_tap = self.fast_drag = None  # a new session may be a new runner: ask again
        self.tw_version = 0
        if version is not None:  # the session-less route said it (the fork has answered both since route 1): kept
            self.fast_tap, self.fast_drag, self.tw_version = version >= 1, version >= 2, version
            self.tw_answered = version
        self.fast_read, self.last_compact = None, None
        self.bounded, self.last_tier, self.last_why = bounded, "full", ""
        self.budgets.forget()
        self.settles.forget()
        self.budget_app = None
        self.sid = sid  # committed last: a failure above leaves no half-started session

    async def _probe_version(self) -> int | None:
        """The fork's route version by the session-less route (VERSION_PATH; the fork has answered it there and on the
        session's route since its route 1): 0 for a runner without it (Appium's build), None when it can't be told (any
        other error, a malformed answer: then the session starts as 1.10.0's did, and `_fast_routes` asks later)."""
        try:
            answer = await self.device.http("GET", VERSION_PATH, timeout=LOCK_WAIT)
        except DeviceError as e:
            said = str(e).lower()
            unknown = "unknown command" in said or "unknown method" in said
            return 0 if unknown and not isinstance(e, ConnectionLost) else None
        value = answer.get("value") if isinstance(answer, dict) else None
        version = value.get("tapwright") if isinstance(value, dict) else None
        return version if isinstance(version, int) and not isinstance(version, bool) and version >= 0 else None

    async def _screen_of_runner(self) -> tuple[float, float] | None:
        """(width, height) in points from SCREEN_PATH (route version 9), and the pixel scale with it; None when the
        answer can't be used (window/size is asked instead). A dropped line is raised, as from window/size."""
        try:
            answer = await self.device.http("GET", SCREEN_PATH, timeout=LOCK_WAIT)
        except ConnectionLost:
            raise
        except DeviceError:
            return None
        value = answer.get("value", answer) if isinstance(answer, dict) else None
        if not isinstance(value, dict):
            return None
        try:
            w, h = float(value["width"]), float(value["height"])
        except (KeyError, TypeError, ValueError):
            return None
        if w <= 0 or h <= 0:
            return None
        try:
            scale = float(value.get("scale"))
        except (TypeError, ValueError):
            scale = 0.0
        if scale > 0:
            self.scale, self.scale_read = scale, True  # (no read of the app needed for it later either)
            self.scale_by_runner = True
        return w, h

    async def _why_stuck(self, locked: bool, lost: ConnectionLost) -> ConnectionLost:
        """The first read of a fresh WebDriverAgent hung: say why when the phone can tell. A locked phone, or
        the app iOS has in front (from its process list), else the dead line it looked like."""
        if locked:
            return PhoneLocked("the phone is locked")
        try:
            if await self.device.passcode_prompt_up():
                return PasscodeNeeded()  # iOS asks again now and then, also with WebDriverAgent already up
        except Exception:
            pass
        try:
            in_front = await self.device.foreground_apps()
        except Exception:
            return lost
        if not in_front:
            return lost
        bundle = in_front[0]
        if bundle not in self.app_names:
            try:
                await self.read_app_names()
            except Exception:
                pass  # the bundle id names it well enough
        return AppNotAnswering(bundle, self.app_names.get(bundle) or bundle)

    async def reset(self) -> None:
        """Drop the connection; the next request reconnects and starts WebDriverAgent again."""
        try:
            await asyncio.wait_for(self.device.close(), 10)
        except Exception:
            pass
        self.device.rsd = self.device.tunnel = self.device.runner = None
        self.sid = None
        self.runner_held = None  # (round N: a new runner holds nothing)
        self._hold_carried = False  # (round T: nor a hold carried from the old one)
        self._held_until = 0.0  # (CodeRabbit, PR #30: nor the old runner's hold still under HIT_HELD; a new one sets its own)
        self.scale = self.app = self.read_app = None
        self.scale_read = False
        self.items, self.offscreen, self.behind = [], [], []
        self.typed_into = None
        self.last_point = None
        self.last_compact = None
        self.last_source = None

    async def _call(self, method: str, path: str, payload: dict | None = None, timeout: float = 30,
                    again: bool | None = None, once: bool = False) -> dict:
        await self.ensure()
        if self.front_apps is not None and method == "POST" and path != FAST_TAP_PATH:
            # the tap is to check the app in front itself: nothing may act before it has
            raise FrontUnchecked("something was to act before the tap that checks the app in front")
        full = path.replace("{sid}", self.sid)
        try:
            try:
                if not TRACE or QUIET.get():
                    return await self._timed(method, path, full, payload, timeout)
                began = time.monotonic()
                try:
                    return await self._timed(method, path, full, payload, timeout)
                finally:
                    getattr(self.device, "log", lambda *a: None)(
                        f"trace {method} {path.split('?')[0]} {(time.monotonic() - began) * 1000:.0f} ms")
            except ConnectionLost as e:
                # A question that got no answer is asked once more before the line counts as dead. Every request
                # has its own connection, and over Wi-Fi one of them can stall while the phone, WebDriverAgent
                # and the app are fine (seen 2026-11-10: `wda/locked`, which answers in 10 ms, got none in 30 s;
                # tree reads stalled the same way and were answered as a frozen app, with a restart of
                # WebDriverAgent each time). Only questions, and what may be said twice (`again`: settings): an
                # action asked twice could happen twice.
                if once and "didn't answer" in str(e):
                    # (2.0.3, review R203 H2) a long read is never asked again: a second one queues behind the first on
                    # the runner and is answered past its own time too, which took a paused video for a dropped line
                    # (a restarted runner, `not_answering`). Its silence counts as a cut read; the line is not dropped.
                    raise Unanswered(str(e)) from None
                if "didn't answer" not in str(e) or self.device.rsd is None:
                    raise
                if not (method == "GET" if again is None else again):
                    # An action: it can't be sent again. If WebDriverAgent answers a new connection, only this
                    # one request stalled: the session stays, and the caller hears that it isn't known whether
                    # the action happened (before, 30 s of silence and then WebDriverAgent started again).
                    if await self.device.wda_alive():
                        raise Unanswered(f"the phone didn't answer within {timeout:.0f} s") from None
                    raise
                getattr(self.device, "log", lambda *a: None)(f"no answer to {method} {path.split('?')[0]} in {timeout:.0f} s: asked again")
                return await self.device.http(method, full, payload, READ_AGAIN)
        except ConnectionLost:
            self.sid = None  # the next call starts WebDriverAgent again
            raise
        except DeviceError as e:
            if "invalid session" in str(e).lower():  # WebDriverAgent restarted: the session is gone
                self.sid = None
                raise ConnectionLost(str(e)) from e
            raise

    async def _timed(self, method: str, path: str, full: str, payload: dict | None, timeout: float) -> dict:
        """device.http, with the timing trace (trace.py) noting a touch's or a tree read's request and answer."""
        guard = getattr(self, "execution_guard", None)
        if method == "POST" and guard is not None:
            await guard()
        input_request = method == "POST" and (path in (FAST_TAP_PATH, FAST_DRAG_PATH,
            "/session/{sid}/actions", "/session/{sid}/wda/keys", "/session/{sid}/alert/accept",
            "/session/{sid}/alert/dismiss") or path.endswith("/clear"))
        validate = getattr(self, "before_input", None) if input_request else None
        if input_request and getattr(self, "_gate_at", None) is not None:
            await self._gate()  # (2.0.3) the app in front, right before the touch request goes to the runner
        if validate is not None:
            await validate(self.input_point)
        pending = None if QUIET.get() else trace.request(method, path)
        token = device_module.BEFORE_SEND.set(guard if method == "POST" else None)
        try:
            answer = await self.device.http(method, full, payload, timeout)
        except BaseException:
            if pending is not None:
                trace.response(pending, ok=False)
            raise
        finally:
            device_module.BEFORE_SEND.reset(token)
        if validate is not None and self.before_input is validate:
            self.before_input = None
        if pending is not None:
            trace.response(pending, answer)
        return answer

    async def _follow_orientation(self, source: str | None) -> None:
        """Keep `screen` and `turn` with the phone's orientation: the tree's root says the screen's size in points
        now (932x430 in landscape), and WebDriverAgent's rotation says which way it turned (z 90 or 270)."""
        if not isinstance(source, str) or source is getattr(self, "_made_root", None):
            return  # (a root made here from `screen` says nothing new: the rotation question isn't asked for it)
        root = ROOT_SIZE.search(source[:2000])
        if root is None:
            return
        w, h = float(root.group(1)), float(root.group(2))
        if w <= 0 or h <= 0:
            return
        same = (w > h) == (self.screen[0] > self.screen[1])
        if w <= h:  # upright: always turn 0, whatever it was (review R200 H1: a turn left from before a reconnect)
            self.screen, self.turn = (w, h), 0
            return
        if same and not self._turn_due():
            return  # (in landscape the phone may have turned to the other side: asked again now and then)
        self.screen = (w, h)
        await self._ask_turn()

    def _turn_due(self) -> bool:
        """In landscape: is which side is up to be asked again (unknown, or asked more than TURN_RECHECK ago)?"""
        return self.turn == TURN_UNKNOWN or time.monotonic() - self._turn_asked >= TURN_RECHECK

    async def _ask_turn(self) -> None:
        """Which side is up, from WebDriverAgent's rotation (it asks no app), for a landscape screen."""
        self._turn_asked = time.monotonic()
        try:
            z = int((await self._call("GET", "/session/{sid}/rotation", timeout=LOCK_WAIT)).get("value", {}).get("z", 0))
        except (DeviceError, TypeError, ValueError):
            # not guessed (R200 H1): the two sides are 180 degrees apart for a touch sent by hands. Unknown keeps
            # touches on the runner, and the next read asks again
            self.turn = TURN_UNKNOWN
            self._turn_asked = -float("inf")
            return
        self.turn = TURN_FOR_Z.get(z % 360, TURN_UNKNOWN)

    async def _banner_over_an_app(self, source: str | None) -> bool:
        """Is this tree only a notification banner sliding over the app in front? While one shows, WebDriverAgent
        answers with SpringBoard's tree: the banner and nothing of the app (seen on the phone 2026-11-10: the
        answers said "Home screen", the app's elements were `not_found`, and the banner's words, which are the
        owner's, were what the agent got). The app is still in front (the phone's process list says so), and the
        banner leaves by itself in a few seconds: the read waits for that. Not for an alert, which is the
        agent's to answer, nor on the real home screen, where no app is in front."""
        if not source or BANNER_ID not in source or ui.SPRINGBOARD_ROOT not in source[:600] or "XCUIElementTypeAlert" in source:
            return False
        try:
            return bool(await asyncio.wait_for(self.device.foreground_apps(), 5))
        except Exception:
            return False

    # ---- seeing ----
    async def _source(self, timeout: float = 60) -> str:
        """The tree as WebDriverAgent's XML. A read normally takes about 0.2 s; one that hasn't answered within
        READ_CUT seconds is cut, and the phone is asked why: an app that is in front and doesn't answer
        accessibility queries is named (AppNotAnswering), so the caller can say so instead of waiting on it."""
        try:
            # (asked a second time by _call when the first got no answer: a frozen app answers neither)
            began = time.monotonic()
            source = await self._read_tree(timeout)
            await self._follow_orientation(source)
            waited = 0.0
            while await self._banner_over_an_app(source) and waited < min(BANNER_WAIT, timeout):
                await asyncio.sleep(BANNER_POLL)
                waited += BANNER_POLL
                # (CodeRabbit, PR #30) what is left of the read's time: a long read planned on all of it could end past
                # wait_for's deadline and be cut there in flight
                source = await self._read_tree(max(0.1, timeout - (time.monotonic() - began)))
            if waited:
                still = " and it is still there" if await self._banner_over_an_app(source) else ""
                getattr(self.device, "log", lambda *a: None)(
                    f"a notification banner was over the app: the read waited {waited:.1f} s{still}")
            self.last_source = source if isinstance(source, str) else None
            return source
        except (PhoneLocked, AppNotAnswering):
            raise
        except ConnectionLost as e:
            raise await self._why_stuck(False, e) from e

    async def _read_tree(self, timeout: float, center: bool = False):
        """One read of the tree as /source's XML: through the fork's compact read when this runner has it (FAST_READ),
        else /source itself. `center` (Notification Center's read, review R200v L4): with the normal budget, outside the
        budget memory and the "unchanged" token."""
        await self.channel_free()  # (round K: the runner held by the app's pending request: wait for it)
        self.last_tier, self.last_why = "full", ""
        if FAST_READ != "0" and self.fast_read is not False:
            await self.ensure()  # (2.0: a session's start learns the runner's version: no second question then)
            if self.fast_tap is None:
                await self._fast_routes()  # (once per session: the version route doesn't read the app)
            if self.tw_version >= BOUNDED_VERSION and center:
                normal = self.budgets.normal_ms
                source = await self._compact_tree(timeout, since=False, budget_ms=normal, remember=False,
                                                  read_path=tree_path(normal, normal))
            elif self.tw_version >= 4:
                source = await self._bounded_tree(timeout) if self.tw_version >= BOUNDED_VERSION else \
                    await self._compact_tree(timeout)
            else:
                source = None
            if source is not None:
                return source
            # (CodeRabbit, PR #26) a compact answer that was then discarded (a quick give-up whose retry was refused) left
            # its tier: the tree returned is /source's, which is whole
            self.last_tier, self.last_why = "full", ""
        return (await self._call("GET", ui.SOURCE_PATH, timeout=min(timeout, READ_CUT))).get("value")

    async def _bounded_tree(self, timeout: float) -> str | None:
        """(route version 9) The compact read with a budget: the app's own (ReadBudget), by the app the newest read
        found in front. A read cut short on the short budget whose answer names another app than the one the budget
        was for is read again with the normal budget: the short budget was that app's, not this one's."""
        # (review R200v L2) a root the runner couldn't name an app for (RUNNER-V9-PROTOCOL §1: `b` left out) is kept
        # under the app the read was budgeted for, so the short budget still applies to it; and such a root is never
        # taken for another app's
        began = time.monotonic()
        self.budgets.other = self.other_screen  # (round G2: the long read's block holds on its own screen only)
        bundle = (self.read_app[1] if isinstance(self.read_app, tuple) else None) or self.budget_app
        seen = getattr(self, "front_seen", None)
        if (seen is not None and seen[0] not in (SPRINGBOARD, bundle) and time.monotonic() - seen[1] < FRONT_FOR_PLAN):
            # (round G) another app came to the front since the newest read (open_app, a link, a home press): this read
            # is planned by it, and a long read is never sent for, nor counted against, the app that was left
            if TRACE:
                getattr(self.device, "log", lambda *a: None)(
                    f"trace read planned by {seen[0]}, in front now (the newest read was of {bundle})")
            bundle = seen[0]
        normal, long_ms = self.budgets.normal_ms, self.budgets.long_ms
        long_wait = read_wait(long_ms, long=True)
        # (2.0.3) a long read needs the caller's time for it (wait_for's reads may have less); the newest frame is kept
        # to compare with the one after a cut read (Daemon._still_across). Asked only when one could follow.
        # (round S, S-F1) a read for an action's own use right before its input: no long budget at all (NO_LONG)
        slow = self.budgets.slow(bundle) and not NO_LONG.get()
        bundle_long = self.budgets.long_on and timeout >= long_wait and not NO_LONG.get() and (
            bundle is None or slow or self.budgets.long_try(bundle))
        # (the lead, after the phone test) the newest frame is kept, to compare with the frame after a cut read; a slow
        # app's planned long read is given up only when something plays (Daemon._plays: the picture a second ago).
        # (The phone, 2026-11-26) nothing of the stream is asked, and nothing waited for, on a read that can't lead to
        # a long one (the short budget, a read left unasked, a blocked app): on a playing feed those are nearly all
        mark, plays = await self._still_mark(check=True) if bundle_long and slow else (None, False)
        long_ok = slow and not plays and self.still_mark is not None and \
            timeout - (time.monotonic() - began) >= long_wait
        budget, unasked = self.budgets.plan(bundle, still=self.screen_still, long_ok=long_ok)
        if NO_LONG.get():
            # (CodeRabbit, PR #30) type_text's own read for its field: a real read with the normal budget, never the
            # short one nor the root made here without asking (it would find no field and type into whatever has focus)
            budget, unasked = normal, False
        if bundle_long and not slow and budget == normal and not unasked:
            mark, _ = await self._still_mark(check=False)
        if unasked and self.tw_version >= FRONT_VERSION and bundle is not None:
            source = await self._unasked_root(bundle)
            if source is not None:
                return source
        if budget == long_ms and budget > normal:
            # a slow but readable app, nothing playing on its screen: the long budget is this read's own
            source, silent = await self._long_read(timeout, bundle, normal, mark)
            if source is not None:
                if _root_bundle(source) in (None, bundle):  # (not a banner's SpringBoard tree)
                    self._note_long(source, began, tell=False)  # (the agent was told when the try made the first answer)
                self.budget_app = _root_bundle(source) or bundle
                return source
            if silent and bundle is not None:
                # (CodeRabbit, PR #30) no answer in its time: the runner may still be reading it ("a request that timed
                # out keeps running"), and a read sent now would queue behind it, get no answer either, be asked again
                # and drop the line. Answered here as a cut read, the runner asked nothing more
                return self._root_here(bundle, LONG_SILENT_WHY.format(bundle=bundle))
            budget = normal  # (refused or unreadable: counted as cut, and read as in 2.0.2)
            mark = None
            timeout = max(0.1, timeout - (time.monotonic() - began))  # (re-check R203b L-b: what is left of it)
            began = time.monotonic()
        self.budgets.tried(bundle)
        source = await self._compact_tree(timeout, budget_ms=budget, budget_for=bundle,
                                          read_path=tree_path(budget, normal))
        asked, kept_for = budget, bundle
        named = _root_bundle(source)
        if (source is not None and self.last_tier != "full" and budget < normal
                and named is not None and named != bundle):
            # (CodeRabbit, PR #30) whether a long read may follow is the named app's, not the one the read was planned for
            bundle_long = (self.budgets.long_on and timeout >= long_wait and not NO_LONG.get()
                           and (self.budgets.slow(named) or self.budgets.long_try(named)))
            if bundle_long:
                mark, _ = await self._still_mark(check=False)  # (this read has the normal budget: a long one may follow)
            self.budgets.tried(named)
            source = await self._compact_tree(timeout, budget_ms=normal, budget_for=named,
                                              read_path=tree_path(normal, normal))
            asked, kept_for = normal, named
            named = _root_bundle(source)
        app = named or kept_for  # (the app the answer was kept under: _compact_tree's)
        if (source is not None and self.last_tier != "full" and asked == normal
                and self._long_try_now(app, bundle_long, timeout, began, long_wait)
                and await self._still_across(mark)
                and self._time_for_long(app, timeout, began, long_wait)):
            # (2.0.3) the normal budget cut the read: the screen may only be slow to read (a shopping app's home screen,
            # about 1,000 elements: 3.5-7.3 s cold, every time). Asked once more, in this read, with the long budget,
            # unless the picture changed across the read by more than STILL_BOX (a playing feed: 2.0.2's cost there),
            # and not while the app is blocked (ReadBudget.long_try)
            tier, why, compact = self.last_tier, self.last_why, self.last_compact
            began_long = time.monotonic()
            longer, _ = await self._long_read(timeout - (time.monotonic() - began), app, normal, mark)
            # (review R203d L1) another app's tree (a banner's) is no answer for this app's read: its own cut one stands
            if (longer is not None and _root_bundle(longer) in (None, app)
                    and _TIER_RANK.get(self.last_tier, 0) > _TIER_RANK.get(tier, 0)):
                source, named = longer, _root_bundle(longer)
                self._note_long(source, began_long, read_began=began)
            else:
                # no answer in its time, refused, unreadable, or no more than the normal read gave (CodeRabbit, PR #30:
                # a long root after a shallow normal read lost the elements a wait_for had found): the normal read's
                # answer stands; the long read's block stays (answered, or long_sent)
                self.last_tier, self.last_why, self.last_compact = tier, why, compact
        if source is not None:
            self.budget_app = named or bundle
        return source

    def _long_try_now(self, app: str | None, bundle_long: bool, timeout: float, began: float,
                      long_wait: float) -> bool:
        """(2.0.3) May the normal read just cut be asked once more with the long budget, before the stillness across it
        is asked? With TAPWRIGHT_TRACE, why not, in one line with its numbers (re-check R203b M-b; the daemon's own
        lines say why the screen wasn't still)."""
        why = ""
        if not self.budgets.long_on:
            why = (f"the long budget is off (TAPWRIGHT_TREE_LONG_BUDGET {self.budgets.long_ms} ms, normal "
                   f"{self.budgets.normal_ms} ms)")
        elif app is None:
            why = ("the app isn't known (the cut answer names none, and the session has none): no block could count a "
                   "long read")
        elif (state := self.budgets.block_state(app)) in ("same", "app", "unseen"):
            where = {"same": "on this screen, the one it failed on",
                     "app": f"on every screen of it, {self.budgets.misses(app)} in a row weren't",
                     "unseen": "(no picture since the one it failed on, or none to compare)"}[state]
            why = (f"{app} is blocked for {self.budgets.blocked_for(app):.0f} s more after a long read that wasn't "
                   f"whole, {where}")
        elif not bundle_long or timeout - (time.monotonic() - began) < long_wait:
            why = (f"{max(0.0, timeout - (time.monotonic() - began)):.1f} s left of the read's time, a long read needs "
                   f"{long_wait:.0f} s")
        elif self.still_mark is None or self.still_across is None:
            why = "no picture to judge by (a session without the daemon's stream)"
        elif not self.budgets.long_try(app):
            why = f"{app} may not have a long read now"
        if why and TRACE:
            getattr(self.device, "log", lambda *a: None)(f"trace no long try: {why}")
        elif TRACE and app is not None and self.budgets.block_state(app) == "other":
            getattr(self.device, "log", lambda *a: None)(
                f"trace long try on another screen of {app}: its block holds only on the screen a long read failed on")
        return not why

    def _time_for_long(self, app: str | None, timeout: float, began: float, long_wait: float) -> bool:
        """(re-check R203b L-b) Is the long read's time still left once stillness across the read was asked (up to
        STILL_AFTER_READ)? Else wait_for's deadline could fall inside it."""
        left = timeout - (time.monotonic() - began)
        if left >= long_wait:
            return True
        if TRACE:
            getattr(self.device, "log", lambda *a: None)(
                f"trace no long try: {left:.1f} s left after the stillness check, a long read needs {long_wait:.0f} s")
        return False

    async def _long_read(self, timeout: float, app: str | None, normal: int,
                         picture: object | None = None) -> tuple[str | None, bool]:
        """(2.0.3) One read of `app` with the long budget, by TAPWRIGHT_TREE_LONG_PATH (TAPWRIGHT_TREE_PATH forces
        every read). The app is blocked from the moment it goes out (ReadBudget.long_sent): only a whole answer lifts
        that. (the answer, or None, counted as a cut read: the block stays, when it got no answer in its time, was
        refused, or couldn't be used; whether it got no answer: the runner may still be reading it). A dropped line is
        raised as from any read."""
        long_ms = self.budgets.long_ms
        self.budgets.tried(app)
        self.budgets.long_sent(app, picture)  # (round G2: the screen it is made on, which a block would hold for)
        if TRACE and app is not None:
            getattr(self.device, "log", lambda *a: None)(
                f"trace long read of {app}: if it isn't whole, {app} is blocked for "
                f"{self.budgets.block_span(app):.0f} s (long read {self.budgets.misses(app)} in a row that may not be)")
        forced = TREE_PATH_FORCED if TREE_PATH_FORCED in ("direct", "xctest", "lean") else None
        path = tree_path(long_ms, normal) if forced else LONG_PATH
        try:
            source = await self._compact_tree(timeout, budget_ms=long_ms, budget_for=app, read_path=path)
        except Unanswered as e:
            getattr(self.device, "log", lambda *a: None)(
                f"tree read with the long budget ({long_ms} ms) got no answer in its time: counted as cut ({e})")
            self.slow_told.discard(app)  # (brief Q fix 3) a long read of it was cut: the next whole one is news again
            return None, True
        if source is None or self.last_tier != "full":
            self.slow_told.discard(app)
        return source, False

    def _note_long(self, source: str, began: float, tell: bool = True, read_began: float | None = None) -> None:
        """A long read answered: when whole, a log line and (`tell`: the long try made the answer) the note for the call
        that made it (READ_OWNER). (2.0.3, brief Q fix 3: on the phone it came on answer after answer, "about 1 s" for
        quick reads) The note is said once per app, the first time a call's own read of it needed the long try, until a
        long read of it is cut (slow_told), with the time the whole read took (`read_began`: when it began, the cut
        normal read included), never rounded up."""
        if self.last_tier != "full":
            return
        took = time.monotonic() - began
        app = _root_bundle(source) or self.budget_app
        if tell and app not in self.slow_told:
            holder = READ_OWNER.get()
            # (review R203 L1) only the call whose own read it was hears it: a read of no call says nothing
            if holder is not None:
                holder.append(LONG_NOTE.format(seconds=time.monotonic() - (began if read_began is None else read_began)))
                if app is not None:
                    self.slow_told.add(app)
        getattr(self.device, "log", lambda *a: None)(
            f"tree read whole with the long budget ({self.budgets.long_ms} ms) in {took:.1f} s: "
            f"{_root_bundle(source) or self.budget_app} reads slowly")

    async def _still_mark(self, check: bool) -> tuple[object | None, bool]:
        """(2.0.3) The daemon's still_mark: (the newest frame, fresh, kept before the read, or None; with `check`,
        whether something plays: Daemon._plays). Without the daemon: (None, False)."""
        mark = self.still_mark
        return await mark(check) if mark is not None else (None, False)

    async def _still_across(self, mark: object | None) -> bool:
        """(2.0.3) The daemon's still_across: may the long try go out (the picture changed no more than STILL_BOX across
        the read, or nothing could be compared)? Without the daemon: no."""
        across = self.still_across
        return across is not None and bool(await across(mark))

    def take_slow_note(self, owner: list[str] | None = None) -> str | None:
        """(2.0.3) The note that a long read made this call's answer (LONG_NOTE), once, from the call's own holder
        `owner` (READ_OWNER)."""
        return owner.pop(0) if isinstance(owner, list) and owner else None

    async def _unasked_root(self, bundle: str) -> str | None:
        """(2.0, worker H) The `root` answer a read of `bundle` would get, made here without asking the runner for the
        tree (ReadBudget.plan said it may be left unasked): only when the runner's FRONT_PATH says that app is still in
        front, with its pid. The XML is the one a runner's `root` answer becomes (RUNNER-V9-PROTOCOL §1: the
        application node, the screen in points). None (read as before) when another app is in front or it can't say."""
        front = await self.front_app()
        if front is None or front[0] != bundle:
            return None
        # (review K-M1) the screen's size and turn are learned from a real read's root: a root made here would keep
        # the old ones for as long as reads are left unasked, and a touch after the phone was turned would be placed
        # for the old way up. The runner's screen route asks no app; when it shows another shape, or can't say, the
        # read is made for real.
        shape = await self._screen_of_runner()
        if shape is None or (shape[0] > shape[1]) != (self.screen[0] > self.screen[1]):
            return None
        if shape[0] > shape[1] and self._turn_due():
            # (CodeRabbit, PR #26) turned over from one landscape side to the other, the screen keeps its shape: the
            # side is asked again as a real read asks it, else a hands touch goes to the opposite part of the screen
            await self._ask_turn()
        age = self.budgets.tried_ago(bundle) or 0.0
        return self._root_here(bundle, UNASKED_WHY.format(bundle=bundle, age=age), front[1])

    def _root_here(self, bundle: str, why: str, pid: int | None = None) -> str:
        """The `root` answer a read of `bundle` would get, made here (the application node, the screen in points), with
        `why` as the read's reason: for a read left unasked, or a long read that got no answer (2.0.3)."""
        node: dict = {"t": "Application", "r": [0, 0, self.screen[0], self.screen[1]], "b": bundle}
        if pid is not None:
            node["pid"] = pid
        label = self.read_app[0] if isinstance(self.read_app, tuple) and self.read_app[1] == bundle else \
            self.app_names.get(bundle)
        if label and label != bundle:
            node["l"] = label
        source = self._made_root = ui.xml_of_tree(node)
        self.last_tier, self.last_why = "root", why
        self.last_compact = None  # (a cut-short tree is never the "unchanged" baseline)
        self.budget_app = bundle
        if TRACE and not QUIET.get():
            getattr(self.device, "log", lambda *a: None)(f"trace tree {self.last_why}")
        return source

    async def front_app(self) -> tuple[str, int | None] | None:
        """(bundle id, pid or None) of the app in front, by the runner's FRONT_PATH (route version 11): found the way a
        tree read finds it, without reading the app (a few ms). None when this runner is older, the app can't be told,
        or the question failed; a dropped line is raised."""
        if self.tw_version < FRONT_VERSION:
            return None
        await self.channel_free()  # (round K: the runner held by the app's pending request: wait for it)
        sid = self.sid
        try:
            answer = await self._call("GET", FRONT_PATH, timeout=FRONT_WAIT)
        except AppNotAnswering:
            raise
        except ConnectionLost as e:
            if "didn't answer" not in str(e):
                raise  # (round T, review RS-L4: "invalid session" is a restarted runner, no hold; a refused line neither)
            # (round S, S-F2) no answer to the front route: before it is taken for a dropped line, the clock once, in
            # the same session (_call let it go: a new one would be asked of the held runner too, 30 s). The runner
            # answering it "no element answered" is the app holding accessibility (the practice: a look's front route
            # at 12:23:38 into a hold, "connection lost", a reconnect and a new session, 47.9 s): the given-up state,
            # the line and the session kept
            dropped, self.sid = self.sid, sid
            if sid is not None and await self._runner_answers_held():
                held = self.runner_held = await self._given_up()
                self._hold_carried = False
                raise RunnerHeld(held.bundle_id, held.name) from None
            if self.sid == sid:  # (review RS-L4: not over a session another task set meanwhile)
                self.sid = dropped
            raise
        except DeviceError as e:
            getattr(self.device, "log", lambda *a: None)(f"the runner couldn't say which app is in front ({e})")
            return None
        value = answer.get("value", answer) if isinstance(answer, dict) else None
        if not isinstance(value, dict):
            return None
        bundle, pid = value.get("b"), value.get("pid")
        if not isinstance(bundle, str) or not bundle or bundle == "unknown":
            return None
        self.front_seen = (bundle, time.monotonic())  # (round G: the read after it is planned by this app)
        return bundle, pid if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 else None

    async def _compact_tree(self, timeout: float, since: bool = True, budget_ms: int | None = None,
                            budget_for: str | None = None, remember: bool = True,
                            read_path: str | None = None, budget_asked: int | None = None) -> str | None:
        """The tree through the fork's compact read (TREE_PATH), as the XML /source would have sent; None when it
        can't be had that way (the caller reads /source). A dropped line or a frozen app is raised as from /source.
        `since`: send the last read's token, so an unchanged screen comes back as "unchanged" without the tree.
        `budget_ms` (route version 9): the runner answers within about that long with what it could read, and says
        how much (`tier`); a cut-short answer is kept as `last_tier`, never as the token an "unchanged" reuses.
        `budget_for`: the app the budget was chosen for, which the answer is kept under when its root names none.
        `remember` False: the answer feeds no budget memory (Notification Center's read). `read_path`: the runner's way
        of reading (`direct` or `xctest`, tree_path), sent with a budget. `budget_asked`: the budget the caller chose,
        when this read is one asked again within it (QUICK_CUT) and has what is left of it."""
        last = self.last_compact if since else None
        # (route version 10) the direct read asking only for the attributes the list uses is the fastest way on every
        # screen measured (lead, phone, 2026-11-26: Settings > General 543 ms against 942 by xctest and 1269 by direct
        # with the standard set; Settings root 168 / 315 / 184; home screen 206 / 416 / 273) and keeps to its budget.
        # (2.0.1) But it asks the app's own process only: a view another process draws inside the app (an in-app
        # browser sign-in sheet, ASWebAuthenticationSession; most likely the share sheet and the photo picker too)
        # isn't in it, and such a screen read as "0 elements" where 1.10 listed its fields and buttons (2.0.0 sent
        # every budgeted read this way). XCTest's own snapshot follows those views. So the lean set goes only with a
        # read the caller already sends the direct way: the short budget of an app whose screen didn't answer in time
        # (tree_path), or every read with TAPWRIGHT_TREE_PATH=lean.
        lean = (budget_ms is not None and self.tw_version >= 10 and TREE_ATTRS != "std" and read_path == "direct"
                and TREE_PATH_FORCED not in ("direct", "xctest"))
        query = [*([f"budget={budget_ms}"] if budget_ms is not None else []),
                 *([f"path={read_path}"] if budget_ms is not None and read_path else []),
                 *(["attrs=lean"] if lean else []),
                 *([f"since={last[0]}"] if last else [])]
        path = TREE_PATH + ("?" + "&".join(query) if query else "")
        began = time.monotonic()
        try:
            # (2.0.3) a long-budget read is waited for its budget and BUDGET_GRACE, and never asked again (review R203
            # H2: the caller counts its silence as a cut read)
            # (re-check R203b N1: a QUICK_CUT retry inside a long read is the long read: `budget_asked`)
            long = budget_ms is not None and (budget_asked or budget_ms) > self.budgets.normal_ms
            value = (await self._call("GET", path, timeout=min(timeout, read_wait(budget_ms, long)),
                                      **({"once": True} if long else {}))).get("value")
        except (ConnectionLost, Unanswered):
            raise
        except DeviceError as e:
            said = str(e).lower()
            if "unknown command" in said or "unknown method" in said:
                self.fast_read = False  # not this runner's route after all
            getattr(self.device, "log", lambda *a: None)(f"compact read refused ({e}): /source instead")
            return None
        token = value.get("token") if isinstance(value, dict) else None
        if not isinstance(token, str) or not token:
            getattr(self.device, "log", lambda *a: None)("compact read answered without a token: /source instead")
            return None
        if value.get("unchanged") is True:
            if last is not None and last[0] == token:
                if long and remember:
                    named = _root_bundle(last[1]) or budget_for
                    # (2.0.3, re-check R203b N1) an unchanged answer to a long read is a whole one: it lifts the block
                    # long_sent set and keeps the app's mark. Left set, every second look at a readable app's still
                    # screen blocked it for 60 s, and its next cold screen read "0 elements". (Only for a long read:
                    # 2.0.2's reads answered unchanged leave the budget memory as they did.)
                    self.last_tier, self.last_why = "full", ""
                    self.budgets.answered(named, False, budget_asked or budget_ms, (time.monotonic() - began) * 1000,
                                          sent_for=budget_for)
                return last[1]  # (only a whole tree is kept as the token's: unchanged is a full read)
            # an "unchanged" this read can't use (it sent no such token): read the whole tree
            return await self._compact_tree(timeout, since=False, budget_ms=budget_ms, budget_for=budget_for,
                                            remember=remember, read_path=read_path) if since else None
        try:
            source = ui.xml_of_tree(value.get("tree"))
        except ValueError as e:
            getattr(self.device, "log", lambda *a: None)(f"compact read unreadable ({e}): /source instead")
            return None
        self.fast_read = True
        tier = "full"  # (a read without a budget, an older runner's: whole, as 1.10.0 took it)
        if budget_ms is not None:
            tier = value.get("tier") if value.get("tier") in TIERS else "full"
            if tier == "full" and value.get("truncated") is True:
                tier = "shallow"  # (said truncated: never taken for a whole tree)
        why = value.get("why")
        self.last_tier, self.last_why = tier, why if isinstance(why, str) else ""
        if budget_ms is not None:
            took_ms = (time.monotonic() - began) * 1000
            left = int(budget_ms - took_ms - RETRY_GAP * 1000)
            gave_up = _gave_up_ms(self.last_why)
            if tier == "root" and gave_up is not None and gave_up < QUICK_CUT * budget_ms and left >= RETRY_MIN_MS:
                # the runner gave the read up long before its budget: the app didn't keep it waiting, it had nothing to
                # answer with yet (0.5 s after a launch: kAXErrorServerNotFound after 8 ms). Asked again within what is
                # left of the budget, so an app that is only starting isn't taken for one that doesn't answer.
                if TRACE:
                    getattr(self.device, "log", lambda *a: None)(
                        f"trace tree read given up after {gave_up:.0f} ms of {budget_ms}: asked again"
                        f"{': ' + self.last_why[:120] if self.last_why else ''}")
                await asyncio.sleep(RETRY_GAP)
                return await self._compact_tree(timeout, since=False, budget_ms=left, budget_for=budget_for,
                                                remember=remember, read_path=read_path,
                                                budget_asked=budget_asked or budget_ms)
            busy = tier != "full" and BUSY_WHY in self.last_why
            if remember:
                if long and busy and TRACE:
                    getattr(self.device, "log", lambda *a: None)(
                        f"trace long read of {budget_for} cut while the app was busy (its application element "
                        f"\"{BUSY_WHY}\"): not counted in its row")
                self.budgets.answered(_root_bundle(source) or budget_for, tier != "full", budget_asked or budget_ms,
                                      took_ms, sent_for=budget_for if long else None,
                                      partial=tier == "shallow" and _has_elements(source), busy=long and busy,
                                      own_use=NO_LONG.get())
            if tier != "full":
                getattr(self.device, "log", lambda *a: None)(
                    f"tree read cut short ({tier}, budget {budget_ms} ms){': ' + self.last_why if self.last_why else ''}")
        # a cut-short tree is no baseline for "unchanged": the next read asks for the whole tree again
        self.last_compact = (token, source) if tier == "full" else None
        return source

    async def read_center(self, timeout: float = READ_CUT) -> str:
        """Notification Center's tree (SpringBoard's) as XML, read once and kept nowhere: not as the last read, not as the
        compact read's token, never traced but for its timing (notifications.py drops every other app's notification
        from it at once). No banner wait: its notifications are what is read."""
        tier_was, why_was = self.last_tier, self.last_why
        try:
            source = str(await self._read_tree(timeout, center=True) or "")
            self.center_tier = self.last_tier
            return source
        finally:
            self.last_compact = None
            # (R200v L4) not the newest read of the app either: its tier is left as the app's read left it
            self.last_tier, self.last_why = tier_was, why_was

    async def foreground_bundle_id(self) -> str | None:
        """The bundle id of the app in front. (2.0, worker H) By route version 11's FRONT_PATH when the runner has it:
        found as a tree read finds its root's app, without reading the app (a few ms, where /wda/activeAppInfo took
        1.7-30 s on TikTok's feed); activeAppInfo only when that can't say."""
        await self.ensure()
        if (front := await self.front_app()) is not None:
            return front[0]
        try:
            info = (await self._call("GET", "/wda/activeAppInfo")).get("value") or {}
        except ConnectionLost:
            raise
        except DeviceError:
            return None
        return str(info.get("bundleId") or "") or None

    async def read_app_names(self, refresh: bool = False) -> None:
        """Bundle id -> display name for the apps you installed: what the header's `App:` line shows,
        since WebDriverAgent's own name is the bundle id for most apps. Read once per connection
        (`refresh`), and again when an unknown bundle id shows up."""
        if refresh:
            self.not_installed = set()
        try:
            await self._list_apps()
        except ConnectionLost:
            raise
        except Exception:  # a nicety: without names the header shows what WebDriverAgent says
            pass

    async def _app(self, known: tuple[str, str] | None = None) -> tuple[str, str] | None:
        """(name, bundle id) of the foreground app, or None when WebDriverAgent can't say. `known` is the tree
        root's (label, bundle id): the same answer without a request (activeAppInfo cost 70 ms an answer)."""
        if known is not None:
            info = {"name": known[0], "bundleId": known[1]}
        elif self.tw_version >= FRONT_VERSION and (front := await self.front_app()) is not None:
            info = {"name": "", "bundleId": front[0]}  # (route version 11: without reading the app, as foreground_bundle_id)
        else:
            try:
                info = (await self._call("GET", "/wda/activeAppInfo")).get("value") or {}
            except ConnectionLost:
                raise
            except DeviceError:
                return None
        bundle = str(info.get("bundleId") or "")
        if not bundle:
            return None
        if bundle == "com.apple.springboard":
            return "Home screen", bundle
        if bundle not in self.app_names and bundle not in self.not_installed:
            if bundle.startswith(APPLE_APPS):
                self._name_behind(bundle)
            else:
                await self.read_app_names()  # perhaps installed since; if not, don't ask again for this one
                if bundle not in self.app_names:
                    self.not_installed.add(bundle)
        return self.app_names.get(bundle) or str(info.get("name") or bundle), bundle

    async def read_scale(self, refresh: bool = False) -> float | None:
        """Pixels per point, read once: from WebDriverAgent's screen info, else (it only answers in some
        apps) from a screenshot's width against the screen's width in points (1290 / 430 = 3)."""
        if refresh or not self.scale_read:
            self.scale_read = True  # the outcome stays, unknown included: snapshots never ask again
            self.scale = None
            try:
                value = (await self._call("GET", "/session/{sid}/wda/screen")).get("value") or {}
                self.scale = float(value["scale"])
            except ConnectionLost:
                raise
            except (DeviceError, KeyError, TypeError, ValueError):
                self.scale = await self._scale_from_screenshot()
        return self.scale

    async def _scale_from_screenshot(self) -> float | None:
        try:
            png = base64.b64decode((await self._call("GET", "/screenshot", timeout=60))["value"])
            from PIL import Image
            with Image.open(io.BytesIO(png)) as img:
                scale = img.width / self.screen[0]
        except ConnectionLost:
            raise
        except Exception:
            return None  # the header just leaves out "@3x"
        return float(round(scale)) if scale >= 1 else None

    async def snapshot(self, keyboard: bool = False, timeout: float = 60) -> Snapshot:
        """Read the tree, the foreground app and the scale; `items` is the new list, numbered from e1."""
        source = await self._source(timeout)
        return await self.snapshot_from(self._tiered(ui.read(source, self.screen, keyboard=keyboard)))

    async def snapshot_from(self, tree: ui.Tree) -> Snapshot:
        """A snapshot of a tree that was already read (`refresh`): this adds the foreground app, the one
        request a tree-only read leaves out."""
        self.items, self.offscreen, self.behind = tree.items, tree.offscreen, tree.behind
        self.last_insets = (tree.top_inset, tree.bottom_inset)
        known = tree.app
        if known is None and tree.tier != "full" and self.tw_version >= FRONT_VERSION:
            # (2.0, worker H) a cut-short root that names no app: activeAppInfo reads the app (1.7-30 s on TikTok's feed);
            # route version 11 says which app is in front without reading it, and if it can't say, the app isn't known
            front = await self.front_app()
            known = (self.app_names.get(front[0]) or front[0], front[0]) if front is not None else None
            if known is None:
                self.app = self.read_app = None
                self.last_alert, self.last_keyboard = tree.alert, tree.keyboard
                self.last_tree = tree
                self._seen_tree(tree)
                return Snapshot(tree, None, self.scale)
        self.app = self.read_app = await self._app(known)
        self.last_alert, self.last_keyboard = tree.alert, tree.keyboard
        self.last_tree = tree
        self._seen_tree(tree)
        return Snapshot(tree, self.app, self.scale)

    async def screenshot_png(self) -> bytes:
        """WebDriverAgent's /screenshot (PNG, full resolution): the fallback when the stream has no frame."""
        return base64.b64decode((await self._call("GET", "/screenshot", timeout=60))["value"])

    def shoots(self) -> bool:
        """Does the runner take JPEG shots, and send the OCR read's picture with its text (route version 11)? Known
        once the session has started."""
        return self.tw_version >= FRONT_VERSION

    async def shot_jpeg(self, width: int | None = None) -> bytes:
        """(route version 11) The screen as a JPEG by SHOT_PATH, `width` pixels wide (None: the screenshot's own),
        upright as /screenshot's PNG is: about 50 ms, where the PNG took 250-640 ms. Raises DeviceError when the answer
        has no picture that can be read."""
        query = [*([f"width={int(width)}"] if width else []), f"quality={SHOT_QUALITY}"]
        answer = await self._call("GET", f"{SHOT_PATH}?{'&'.join(query)}", timeout=SHOT_WAIT)
        value = answer.get("value", answer) if isinstance(answer, dict) else None
        if isinstance(value, dict) and isinstance(value.get("ms"), dict):
            trace.note("shot_wda_ms", {k: v for k, v in value["ms"].items()
                                       if isinstance(v, (int, float)) and not isinstance(v, bool)})
        return _jpeg_of(value, "shot")

    async def screen_picture(self, width: int | None = None) -> bytes:
        """A picture of the screen now, for an answer when the stream has none: the runner's JPEG shot (route version
        11; `width` pixels wide, None: full size), else /screenshot's PNG. A shot that fails is taken as a PNG."""
        await self.ensure()
        if self.shoots():
            try:
                return await self.shot_jpeg(width)
            except ConnectionLost:
                raise
            except DeviceError as e:
                getattr(self.device, "log", lambda *a: None)(f"the runner's JPEG shot failed ({e}): /screenshot instead")
        return await self.screenshot_png()

    def reads_text(self) -> bool:
        """Can the runner read the screen's text itself (OCR_PATH, route version 9)? Known once a read has asked the
        runner's routes (the first look of a session)."""
        return self.tw_version >= BOUNDED_VERSION

    async def read_text(self, level: str = "accurate") -> "list[ocr.Seen]":
        """The text lines on the screen now, read on the phone by Apple's Vision (OCR_PATH): placed in this session's
        points, in reading order. `level`: "accurate" (a look) or "fast" (wait_for's polls). Raises DeviceError when the
        runner can't (the caller reads the picture on this computer instead)."""
        return (await self._read_text(level, None))[0]

    async def read_text_and_picture(self, level: str = "accurate",
                                    width: int | None = None) -> "tuple[list[ocr.Seen], bytes]":
        """(route version 11) read_text, and the picture the text was read from as a JPEG `width` pixels wide (None:
        full size), in one request (`image=1`): a look whose tree was cut short needs both. Raises DeviceError when the
        runner can't, or its answer has no picture (an older runner ignores `image`)."""
        if not self.shoots():
            raise DeviceError("this runner can't send the OCR read's picture (route version 11)")
        seen, value = await self._read_text(level, width or 0)
        return seen, _jpeg_of(value, "OCR read")

    async def _read_text(self, level: str, image: int | None) -> "tuple[list[ocr.Seen], object]":
        """(the lines, the answer's value): read_text's request; with `image` (a width, 0: full size) it asks for the
        picture too."""
        if level not in ("fast", "accurate"):
            raise ValueError(f"level must be fast or accurate, not {level!r}")
        languages = ",".join(p.strip() for p in OCR_LANGUAGES.split(",") if p.strip())
        if level == "fast" and not fast_reads(languages):
            level = "accurate"  # (review R200v M4) the fast level would fail every read in these languages
        path = f"{OCR_PATH}?level={level}" + (f"&languages={urllib.parse.quote(languages, safe=',-')}" if languages else "")
        if image is not None:
            path += f"&image=1{f'&width={int(image)}' if image else ''}&quality={SHOT_QUALITY}"
        answer = await self._call("GET", path, timeout=OCR_WAIT, again=False)
        value = answer.get("value", answer) if isinstance(answer, dict) else None
        if isinstance(value, dict) and isinstance(value.get("ms"), dict):
            trace.note("ocr_wda_ms", {k: v for k, v in value["ms"].items()
                                      if isinstance(v, (int, float)) and not isinstance(v, bool)})
        try:
            return ocr.of_runner(value, self.screen), value
        except ValueError as e:
            raise DeviceError(f"the runner's OCR answer can't be read: {e}") from None

    async def look(self, screenshot: bool = True, keyboard: bool = False, max_width: int = 400):
        """(text, jpeg bytes or None): the screen as elements, and as a picture."""
        snap = await self.snapshot(keyboard)
        tree, app, scale = snap.tree, snap.app, snap.scale
        image = None
        if screenshot:
            png = base64.b64decode((await self._call("GET", "/screenshot", timeout=60))["value"])
            from PIL import Image
            img = Image.open(io.BytesIO(png)).convert("RGB")
            if img.width > max_width:
                img = img.resize((max_width, round(img.height * max_width / img.width)), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=80)
            image = buf.getvalue()
        return ui.describe(tree.items, self.screen, scale, app, tree.alert, tree.keyboard, tree.more,
                           (tree.above, tree.below), tier=tree.tier), image

    async def back_point(self) -> ui.Back:
        """The top navigation bar: whether there is one, and where its back button is, in points."""
        source = await self._source()
        # what is open on top, as this read shows it: `back` names it when it could close nothing
        tree = ui.read(source, self.screen)
        self.behind, self.front_items = tree.behind, tree.items  # (and what the layer in front shows, for `back`)
        return ui.back_button(source, self.screen)

    async def audit(self, types: list[str]) -> list[dict]:
        """XCTest's accessibility audit of the app in front (iOS 17+), through WebDriverAgent: one dict per issue
        with `auditType`, `compactDescription`, `detailedDescription` and `elementAttributes` (type, label, name,
        rect). `types` are XCUIAccessibilityAuditType names; empty is all of them."""
        found = (await self._call("POST", "/session/{sid}/wda/performAccessibilityAudit", {"auditTypes": types},
                                  timeout=AUDIT_TIMEOUT)).get("value")
        # (kept as XCTest says it, to be matched with the tree's elements: audit.rows_of redacts what it shows)
        return [i for i in found if isinstance(i, dict)] if isinstance(found, list) else []

    async def read_source(self, timeout: float = 60) -> str | None:
        """The tree's XML now, kept as `last_source` only (the agent's list stays as it is): for placing a hit test's
        element that the last read didn't have (Codex, PR #12 cycle 1, M1)."""
        source = await self._source(timeout)
        return source if isinstance(source, str) else None

    async def peek_tree(self, timeout: float = 60) -> ui.Tree:
        """Read the tree and keep nothing of it: `items` and the rest stay as the last read left them (one request). For a
        check right before a touch that must see the screen as it is now without renumbering the agent's list (a draft's
        digest: Codex, PR #12 cycle 1, H3)."""
        return self._tiered(ui.read(await self._source(timeout), self.screen))

    def _tiered(self, tree: ui.Tree) -> ui.Tree:
        """The tree, marked with how much of it the read that made it got (`last_tier`, route version 9)."""
        tree.tier = self.last_tier
        return tree

    async def refresh(self, timeout: float = 60) -> ui.Tree:
        """Read the tree into `items` and nothing else (one request): what resolving a target needs."""
        source = await self._source(timeout)
        tree = self._tiered(ui.read(source, self.screen))
        self.items, self.offscreen, self.behind = tree.items, tree.offscreen, tree.behind
        self.last_insets = (tree.top_inset, tree.bottom_inset)
        self.last_alert, self.last_keyboard = tree.alert, tree.keyboard
        self.last_tree = tree
        self.read_app = tree.app
        self._seen_tree(tree)
        return tree

    def _seen_tree(self, tree: ui.Tree) -> None:
        if self.on_tree is not None:
            try:
                self.on_tree(tree)
            except Exception:  # a watcher's fault never fails a read
                pass

    def _target(self, target: ui.Target) -> tuple[float, float, str]:
        """(x, y, what to say) for a target, on the elements of the last look."""
        hit = ui.resolve(self.items, target)
        if isinstance(hit, tuple):
            return hit[0], hit[1], f"({round(hit[0])}, {round(hit[1])})"
        cx, cy = hit.center
        return cx, cy, hit.line(actions=False)

    # ---- acting ----
    async def _touching(self, ms: float, at: tuple[float, float] | None = None) -> None:
        """Just before a touch that starts at `at`: wait out a notification banner over the top of the screen (for
        the agent's touches), refuse it when another app than the one the agent last saw is in front (_seen_check),
        then tell Tapline the touch is the agent's. The banner wait and the check share one question to the runner
        (FRONT_PATH, a few ms): at most one a touch, none on a read.
        (2.0.3) The check and the touch are two steps: the answer comes a few ms before the touch goes out, after the
        touch hook (Tapline's notice) and the hands' or the runner's own start. A banner or an app that comes to the
        front inside that window isn't caught: the window is made small, not closed."""
        self.input_point = at
        self._banner_seen = False  # (review RH H2) a banner seen at this touch's start: only "clear" ends its wait
        self._banner_until: float | None = None  # (review RI M1) the touch's one banner deadline, across its waits
        self._banner_real = False  # (round L) a banner element seen in this touch's check
        self._banner_moving = False  # (round L) a banner only in motion seen (a fast "no element answered")
        self._wait_clear = None  # (review RK M1) (point, front, when) of the clear that ended this touch's banner wait
        if self.banner_guard and at is not None and self.touch_rules is None:
            await self._clear_of_banner(at)  # (a touch without the agent's rules: the banner wait as before 2.0.3)
        if at is not None:
            self.top_touch = self.top_touch or at[1] < BANNER_ZONE
        if self.touch_hook is not None:
            await self.touch_hook(ms)
        # (review RP M3) the check of the app in front comes after the hook and right before the touch: the hands' `before`
        # (just before the first HID report) or the runner's touch request (_timed), never earlier
        self._gate_at = at if self.touch_rules is not None and at is not None else None
        self._gate_spent = None
        trace.mark("guard")

    async def _gate(self, wait: bool = True) -> None:
        """(2.0.3) The check of a touch that _touching left to the last moment, once: _front_rules for its point. What is
        left between this answer and the finger (review RP2 L6): on the hands path, no await between the check (and the
        guard after it) and the first HID report; the check itself is the route's lookup (a few ms over USB, up to about
        1.8 s on a slow screen) and, when SpringBoard is in front, iOS's process list (about 100-500 ms) after it. On
        the runner path, the touch request's way to the runner and XCTest's own start (tens of ms). Not closed, only
        made as small as the code can make it. `wait` False: a banner to wait out raises _BannerUp (the hands' lock)."""
        at = self._gate_at
        if at is None or self.touch_rules is None:
            return
        # (CodeRabbit, PR #30) what this check was, for a fast touch the runner refuses unplayed (_regate)
        self._gate_spent = (at, getattr(self, "late_check", None))
        try:
            await self._front_rules(at, wait)
            # (round O, review RN-M4) an o ref's text, read again when its check is old. (Round P, review RO-M2) A read
            # made then is followed by the front and banner check once more, so the last thing before the finger is
            # that check (on the hands' path no await between it and the first HID report). Two reads at most: the
            # second check's own banner wait is inside the touch's single deadline, and a third read is never made
            reads = 0
            while reads < 2 and (late := getattr(self, "late_check", None)) is not None and await late(at):
                reads += 1
                await self._front_rules(at, wait)
        except (_BannerUp, _HeldUp):
            raise  # (the gate stays: it runs again once the banner, or the runner, is waited out)
        except BaseException:
            self._gate_at, self._banner_seen, self._banner_until = None, False, None
            self.late_check = None
            raise
        self._gate_at, self._banner_seen, self._banner_until = None, False, None
        self.late_check = None  # (round P, review RO-M1: once an action; a scroll's later drags don't run it again)

    def _regate(self) -> None:
        """(CodeRabbit, PR #30) The runner's fast route refused a touch that the gate passed, before anything was played
        ("invalid argument"): the W3C actions that play it instead are checked again (the app in front, an o ref's text),
        as the time since may have brought another app or a sheet."""
        spent, self._gate_spent = getattr(self, "_gate_spent", None), None
        if spent is not None and self.touch_rules is not None:
            self._gate_at, self.late_check = spent

    async def _wait_banner(self) -> None:
        """(review RP2 H1) Wait a banner out with no lock held, up to BANNER_WAIT; refused `covered` if it stays.
        (Review RI M1) BANNER_WAIT is the touch's in all, across every wait its gate starts (the gate and these polls
        can disagree: "clear" here, silence there), and the guard (a pause, a lock, a person's turn) is asked at every
        poll, so the touch ends within a poll of either."""
        self._banner_seen = True  # (review RH H2) the gate saw one (_BannerUp): only "clear" ends the wait now
        until = getattr(self, "_banner_until", None)
        if until is None:
            until = self._banner_until = time.monotonic() + BANNER_WAIT
        while time.monotonic() < until:
            await asyncio.sleep(BANNER_POLL)
            await self._banner_guard()
            if self._held() and not await self._free_step():
                continue  # (round K, review RK L5: the runner still held; one question at the clock, no more, this poll)
            now = await self.front_state()
            if now is None or now.kind not in (FRONT_OVER, FRONT_APP, FRONT_HOME):
                return
            # (round G) the route names the app (or the home screen) while a banner iOS 26.5 shows: then the hit test
            at = self._gate_at
            rules = self.touch_rules
            app = now.app if now.kind == FRONT_APP else None
            if now.kind != FRONT_OVER:
                said = BANNER_CLEAR if at is None else await self.banner_at(
                    at, app, rules.aimed if rules is not None else None, hands=True)
                if said == BANNER_CLEAR and at is not None:
                    self._wait_clear = (at, now, time.monotonic())  # (review RK M1: the gate after it takes it)
                # (review RH H2) once a banner was seen, only "clear" says it left: a silence or "can't say" waits on
                if said == BANNER_CLEAR or (said != BANNER_OVER and not getattr(self, "_banner_seen", False)):
                    return  # (the gate runs again: the check, then the guard)
        words = self._covered_at_bound()
        self._gate_at, self._banner_seen, self._banner_until = None, False, None
        raise Refused("not_actionable", words)

    def _covered_at_bound(self) -> str:
        """(round L, review RK L5) The words of a touch whose wait reached its bound: the slow app's when its check only
        ever found a banner in motion (a fast "no element answered", the runner not held at the confirm) and never a
        banner's element, else a banner's."""
        moving = getattr(self, "_banner_moving", False) and not getattr(self, "_banner_real", False)
        return COVERED_SLOW if moving else COVERED_BANNER

    async def _banner_guard(self) -> None:
        """(review RI M1) The execution guard (a pause, a lock, a person's turn), asked at each poll of a banner wait: a
        refusal ends the touch, and its gate with it."""
        guard = getattr(self, "execution_guard", None)
        if guard is None:
            return
        try:
            await guard()
        except BaseException:
            self._gate_at, self._banner_seen, self._banner_until = None, False, None
            raise

    def _portrait_point(self, at: tuple[float, float]) -> "tuple[float, float] | None":
        """(round H, review RG M2) The point as the hit route takes it, in portrait points (as Hands maps a turn: to_hid).
        None when the turn isn't known (landscape, which side up not said yet, or a turn the screen's size contradicts)."""
        w, h = min(self.screen), max(self.screen)
        upright = self.screen[0] <= self.screen[1]
        if self.turn == 0 and upright:
            return at
        if self.turn == 90 and not upright:
            return at[1], h - at[0]
        if self.turn == 270 and not upright:
            return w - at[1], at[0]
        return None

    async def banner_at(self, at: tuple[float, float], app: str | None = None,
                        aimed: "tuple[float, float, float, float] | None" = None, hands: bool = False) -> str:
        """(rounds G to K) Is something over the app (`app`, the one the agent saw; None: the home screen) at this point?
        BANNER_OVER: a banner; BANNER_CLEAR: nothing over it; BANNER_UNSURE: it can't be told (another process over the
        app, an error, a malformed or unverified answer, a turn not known); BANNER_SLOW: no answer, the quiet (HIT_QUIET
        after a silence), the runner held (HIT_HELD), or "no element answered" with nothing to tell the app from
        SpringBoard; BANNER_UNASKED: the check is off, or the runner is older or has no such route. One question out at
        a time (the hit lock), and none repeated (HIT_TRIES 1): a question the app doesn't answer holds the runner until
        it does (phone, 2026-11-27: 2.5 to 9.8 s after one question; three cost the connection).
        (Round K) Over an app, upright, a point below the status bar: the status bar's clock (HIT_WITNESS) is asked
        first. A banner there (or a call's, or SpringBoard's whole screen: one coming or going) is over, the point not
        asked. A fast "no element answered" there is a banner coming or going: over. A slow one, or no answer, is slow
        (the runner is held: the hold is set). SpringBoard's status bar there "holds": SpringBoard answers right now.
        Then the touch's point, once: a real answer, _banner_of; a fast "no element answered", over (a banner in
        motion); a slow one, the app didn't answer where SpringBoard would have: clear when the clock held, slow
        otherwise, and the hold is set either way. An ordinary banner sits below the status bar, which keeps answering
        as if nothing were there (its plate [8, 56, 414, 73]): only the point can see it. A phone with no status-bar
        element at the clock (a home-button phone) gets no witness. The home screen, landscape, a turn not known, a
        point in the status bar: no witness, the point once. Trace notes: the point, the frame, the process, never a
        label.
        (Rounds L, M) Two lines (review RK H1, RL H1): any "no element answered" of HIT_NOTHING_SLOW or more sets the
        hold, whatever the verdict; clear is decided strictly: a prompt first answer at the clock (HIT_WITNESS_PROMPT),
        the point's "nothing" HIT_NOTHING_MARGIN slower than it, and the clock asked once more saying "nothing" as
        slowly (a real answer there: the runner isn't held, the point's "nothing" was SpringBoard's, over). A clock
        that answered the status bar but not promptly still lets the point decide, except that its "nothing" is slow. `hands`: the touch goes by the hands; the runner's touch goes through XCTest, held now,
        and would land late (M2): no clear then, slow."""
        self.sheet_over = None  # (round N: another app's sheet over the screen, as this check found it; round O, RN-L1)
        if HIT_CHECK == "0" or self.tw_version < 6:
            return BANNER_UNASKED
        point = self._portrait_point(at)
        if point is None:
            trace.note("banner_turn", {"at": [round(at[0]), round(at[1])], "turn": self.turn})
            return BANNER_UNSURE
        where = {"at": [round(at[0]), round(at[1])]}
        if self._held():
            trace.note("banner_held", where)  # (the runner waits on the app: nothing is asked)
            return BANNER_SLOW
        lock = getattr(self, "_hit_lock", None)
        if lock is None:
            lock = self._hit_lock = asyncio.Lock()
        async with lock:
            if self._held():  # (review RK L4: a hold set while this call waited for the lock)
                trace.note("banner_held", where)
                return BANNER_SLOW
            deadline = time.monotonic() + HIT_TRIES_FOR
            holds = None  # (the clock's frame when SpringBoard's status bar answered there)
            t_w = 0.0  # (review RK H1: the clock's own round trip, which a "no element answered" is judged against)
            witnessing = app is not None and point is at and point[1] >= HIT_BANNER_BELOW
            if witnessing:
                kind, value, t_w = await self._hit_once(HIT_WITNESS, deadline, where)
                if kind in ("silent", "quiet"):
                    return BANNER_SLOW
                if kind == "nothing":
                    if slow_nothing(value, t_w):  # (round N: by the phone's time on runner 16)
                        self._hold("witness", at)
                        return BANNER_SLOW
                    trace.note("banner_moving", {**where, "witness": True})  # (the status bar being redrawn)
                    self._banner_moving = True
                    return BANNER_OVER
                if kind == "unasked":
                    return BANNER_UNASKED
                said, frame = self._witness_of(value, where) if kind == "answer" else (None, None)
                if said == BANNER_OVER:
                    self._banner_real = True
                    return BANNER_OVER
                holds = frame if said == "holds" else None
            kind, value, took = await self._hit_once(point, deadline, where)
            if kind in ("silent", "quiet"):
                return BANNER_SLOW
            if kind == "nothing":
                if slow_nothing(value, took):
                    self._hold("point", at)  # (review RL H1: the runner held, whatever the verdict below)
                if point[1] < HIT_NOTHING_ABOVE:  # (review RH L5: the portrait panel's top edge)
                    return BANNER_CLEAR  # (the screen's top edge: a banner there would have answered)
                if value is not None:
                    # (round Q) No shipped runner answers this way: tw-16.15.3.16 (not shipped) answered `daemon` every
                    # time on iOS 26.5 (Q-F1), so `value` is None here with every runner 2.0.3 uses. Kept for 2.0.4's.
                    # (round N) runner 16 timed its own lookup: the phone's time says it. Fast: nothing is there right
                    # now (a banner in motion). Slow with SpringBoard's status bar answering at the clock: the app didn't
                    # answer where SpringBoard would have: clear by the hands with no confirm and no promptness asked of
                    # the clock (the hold set above); by the runner only once the clock answers again (round O, RN-H1).
                    # Slow with no witness: the app can't be told from SpringBoard, slow (the hold set above either way)
                    ms = round(value * 1000)
                    if value < HIT_PHONE_SLOW:
                        trace.note("banner_moving", {**where, "phone_ms": ms})
                        self._banner_moving = True
                        return BANNER_OVER
                    if holds is None:
                        trace.note("banner_slow", {**where, "phone_ms": ms})
                        return BANNER_SLOW
                    if not hands:
                        # (round O, review RN-H1) the runner's own touch goes through XCTest: that the bounded lookup
                        # left nothing held isn't measured yet (N-F4). The clock asked once more: a real answer proves
                        # the runner free (clear, no hold); "nothing" or a silence: held, slow (round M's refusal)
                        again, answer, t_again = await self._hit_once(HIT_WITNESS, deadline, where,
                                                                      wait=HIT_FREE_WAIT)
                        if again == "answer":
                            self._held_until = 0.0
                            if self._witness_of(answer, where)[0] == BANNER_OVER:
                                self._banner_real = True  # (a banner came to the clock meanwhile)
                                return BANNER_OVER
                            if t_again >= HIT_WITNESS_PROMPT:
                                # (round P, review RO-L1) the runner is free, but an answer this late leaves the time
                                # between the point's answer and the touch too long (the clock can't see an ordinary
                                # banner coming: N-F6): slow, and the gate's next poll asks again
                                trace.note("banner_slow", {**where, "phone_ms": ms, "confirm_ms": round(t_again * 1000)})
                                return BANNER_SLOW
                            trace.note("banner_witness", {**where, "frame": holds, "phone_ms": ms, "free": True})
                            return BANNER_CLEAR
                        if again == "nothing" and not slow_nothing(answer, t_again):
                            # (round P, review RO-L2) a fast "nothing" at the clock: the status bar being redrawn, a
                            # banner in motion, as at the first clock question; no hold
                            self._held_until = 0.0
                            trace.note("banner_moving", {**where, "witness": True, "confirm": True})
                            self._banner_moving = True
                            return BANNER_OVER
                        trace.note("banner_slow", {**where, "phone_ms": ms, "confirm": again})
                        if again == "nothing":
                            self._hold("confirm", at)
                        return BANNER_SLOW
                    trace.note("banner_witness", {**where, "frame": holds, "phone_ms": ms, "hands": hands})
                    return BANNER_CLEAR
                if holds is not None and t_w >= HIT_WITNESS_PROMPT:
                    # (round M, the lead) the status bar answered, but not promptly: its time can't vouch for the
                    # point's, so a "nothing" there is neither a clear nor a banner in motion: slow (no confirm asked)
                    trace.note("banner_slow", {**where, "witness_ms": round(t_w * 1000)})
                    return BANNER_SLOW
                if took < t_w + HIT_NOTHING_MARGIN:
                    # (review RK L1) SpringBoard's own "nothing here right now", every path: a banner in motion
                    trace.note("banner_moving", where)
                    self._banner_moving = True
                    return BANNER_OVER
                if holds is None or not hands:
                    # no witness to tell the app from SpringBoard; or (review RK M2) the touch goes through the runner,
                    # which is held now: it would land seconds late, past any check
                    trace.note("banner_slow", {**where, "hands": hands, "ms": round(t_w * 1000)})
                    return BANNER_SLOW
                # Clear needs three things (review RK H1, RL H1): the clock's prompt answer, SpringBoard's status bar
                # (SpringBoard answers); the point's "nothing" HIT_NOTHING_MARGIN slower than it (the app didn't
                # answer); and the clock asked once more saying "nothing" as slowly (the runner is held by the app's
                # pending request, BRIEF-K F4). The known limit (review RL H2, accepted for 2.0.3 by the lead): while a
                # banner comes or goes the clock too says a fast "nothing" (F2), so two answers delayed on the link by
                # HIT_NOTHING_MARGIN after a prompt first one, in that half second, AND a fast "nothing" at a point
                # below the status bar (not seen on the phone) would read as clear. The runner timing its own lookup
                # (2.0.4) closes it.
                again, _, t_again = await self._hit_once(HIT_WITNESS, deadline, where, wait=HIT_FREE_WAIT)
                if again == "nothing" and t_again >= HIT_NOTHING_SLOW:
                    self._hold("confirm", at)
                if again == "nothing" and t_again >= t_w + HIT_NOTHING_MARGIN:
                    self._hold("point", at)
                    trace.note("banner_witness", {**where, "frame": holds,
                                                  "ms": [round(t_w * 1000), round(took * 1000), round(t_again * 1000)]})
                    return BANNER_CLEAR  # (the app didn't answer where SpringBoard, answering, would have)
                if again == "answer":
                    # the runner isn't held: the point's "nothing" was SpringBoard's own, or the app answered late
                    trace.note("banner_moving", {**where, "confirm": True})
                    self._banner_moving = True
                    return BANNER_OVER
                trace.note("banner_slow", {**where, "confirm": again})
                return BANNER_SLOW
            if kind == "unasked":
                return BANNER_UNASKED
            if kind != "answer":
                return BANNER_UNSURE
            said = self._banner_of(value, at, app, aimed, point is not at)
            if said == BANNER_OVER:
                self._banner_real = True
            return said

    async def _hit_once(self, xy: tuple[float, float], deadline: float, where: dict,
                        wait: float | None = None, sets_quiet: bool = True) -> "tuple[str, object, float]":
        """(round K, L) One hit question at a portrait point, `once`, inside the call's deadline (or `wait` seconds):
        (kind, the answer, the seconds it took, as the daemon saw them). "answer"; "nothing" ("no element answered":
        the caller judges its time against the clock's own round trip, review RK H1; round N: its answer is the phone's
        own time of the lookup, in seconds, from runner 16, else None: phone_time); "unasked" (no such route);
        "error" (another refusal); "silent" (no answer in time: the quiet is set, review RH M1); "quiet" (none sent:
        HIT_QUIET after a silence, or too little time left)."""
        quiet = getattr(self, "_hit_quiet_until", 0.0) - time.monotonic()
        if quiet > 0:
            trace.note("banner_quiet", round(quiet, 1))  # (a hit question got no answer: none is sent)
            return "quiet", None, 0.0
        left = min(HIT_BANNER_WAIT, deadline - time.monotonic()) if wait is None else wait
        if wait is None and left < HIT_TRY_MIN:
            return "quiet", None, 0.0  # (too little of the call's deadline left: not asked)
        began = time.monotonic()
        try:
            answer = await asyncio.wait_for(
                self._call("GET", f"{HIT_PATH}?x={xy[0]:.2f}&y={xy[1]:.2f}", timeout=left, again=False, once=True),
                left + 0.05)
        except ConnectionLost:
            raise
        except Unanswered:
            if sets_quiet:  # (round O, review RN-L3: not after a held state's clock question)
                self._hit_quiet_until = time.monotonic() + HIT_QUIET
            trace.note("banner_silent", {**where, "asked": [round(xy[0]), round(xy[1])]})
            return "silent", None, time.monotonic() - began
        except DeviceError as e:
            said = str(e).lower()
            if HIT_NOTHING in said:
                phone = phone_time(said, self.tw_version)
                if phone is not None:
                    trace.note("hit_phone", {**where, **phone_words(str(e))})  # (round O: the whole and the error too)
                return "nothing", phone, time.monotonic() - began
            return ("unasked" if "unknown command" in said or "unknown method" in said else "error"), None, \
                time.monotonic() - began
        except Exception:  # noqa: BLE001 - no answer in time (or the wait failed): the runner may be on it still
            if sets_quiet:
                self._hit_quiet_until = time.monotonic() + HIT_QUIET
            trace.note("banner_silent", {**where, "asked": [round(xy[0]), round(xy[1])]})
            return "silent", None, time.monotonic() - began
        if xy == HIT_WITNESS:
            self._clock_real_at = time.monotonic()  # (round S: the clock answered: the runner wasn't held then)
        return "answer", answer, time.monotonic() - began

    def _witness_of(self, answer: object, where: dict) -> "tuple[str | None, list | None]":
        """(round K) What the status bar's clock says: (BANNER_OVER, frame) for a banner there by _banner_of's rule, a
        call's banner, or SpringBoard's whole screen (a banner coming or going); ("holds", frame) for SpringBoard's
        status bar (verified, not an icon nor the whole screen, its bottom at or above HIT_BANNER_BELOW): SpringBoard
        answers right now; (None, None) for anything else (no status bar there: no witness)."""
        value = answer.get("value") if isinstance(answer, dict) else None
        hit = ui.hit_of(value)
        if hit is None or not isinstance(value, dict) or value.get("identity") != "verified":
            return None, None
        frame = [round(v) for v in hit.rect]
        noted = {**where, "frame": frame, "b": hit.bundle, "witness": True}
        if hit.bundle == IN_CALL:
            trace.note("banner_hit", noted)
            return BANNER_OVER, frame
        if hit.bundle != SPRINGBOARD or hit.type == "Icon":
            return None, None
        x, y, w, h = hit.rect
        pw, ph = min(self.screen), max(self.screen)
        if (x <= 1 and y <= 1 and w >= pw - 1 and h >= ph - 1) or y + h > HIT_BANNER_BELOW:
            trace.note("banner_hit", noted)
            return BANNER_OVER, frame
        return "holds", frame

    # ---- (round K) the runner held by a question the app didn't answer ----
    def _held(self) -> bool:
        """Is the runner known to be held: a hit question got a slow "no element answered" within HIT_HELD, and no real
        answer has come since (channel_free)? (Round N) Or a hold was given up (RunnerHeld) and the clock hasn't
        answered since."""
        return (getattr(self, "runner_held", None) is not None or getattr(self, "_held_until", 0.0) > time.monotonic()
                or getattr(self, "_hold_carried", False))

    def _hold(self, why: str, at: tuple[float, float]) -> None:
        """A slow "no element answered": XCTest's daemon keeps the app's unanswered request, and every hit question and
        the front route get nothing until the app answers it (phone, 2026-11-27: 2.5 s on a shopping app, 9.8 s on a
        hung feed)."""
        now = time.monotonic()
        self._hold_at, self._held_until = now, now + HIT_HELD  # (round S: when it was set, for hold_standing)
        trace.note("runner_held", {"why": why, "at": [round(at[0]), round(at[1])]})

    async def channel_free(self, until: float | None = None) -> bool:
        """(round K, L) Before a request through XCTest's accessibility (the front route, a tree read) while the runner is
        known to be held: the hit test at the status bar's clock (HIT_WITNESS, portrait points whatever the turn), about
        every HIT_FREE_EVERY seconds, HIT_FREE_WAIT at most a question; only a real answer (an element) ends the hold
        (review RK L3: another refusal or a silence says nothing of it); at HIT_HELD it is given up (the request goes);
        a runner with no such route ends the waiting, the hold with it (nothing could ever be asked). The execution
        guard (a pause, a lock, a person's turn), when the path has one, is asked at each poll. Instead of a front
        question that stalls for 8 s and is taken for a lost connection, the read after a touch on a hung app comes up
        to about 10 s later. `until` (round M: a hands touch's own deadline): False when it came first, the runner
        still held; else True.
        (Round N, N-F2) At HIT_HELD the hold is given up and nothing goes into the runner: RunnerHeld (the app in front
        named from iOS's process list) is raised, and raised again at once by every later call (nothing asked) until
        the clock answers (held_answers, _free_step).
        (2.0.3 round T, review RS-H1) The wait is bounded whatever happens meanwhile: it ends at the hold's limit
        (HIT_HELD from the hold's start, and never more than HIT_HELD from this wait's start), after one question at
        least (a hold carried past its limit: carry_hold); the given-up state set by another call meanwhile is raised at
        the next step; two clock questions are HIT_FREE_MIN apart at least, never back to back."""
        if (held := getattr(self, "runner_held", None)) is not None:
            raise RunnerHeld(held.bundle_id, held.name)
        if not self._held():
            return True
        began = time.monotonic()
        asked_once = False
        while True:
            if (held := getattr(self, "runner_held", None)) is not None:
                trace.note("runner_free", {"given_up_meanwhile": round(time.monotonic() - began, 1)})
                raise RunnerHeld(held.bundle_id, held.name)
            now = time.monotonic()
            limit = min(getattr(self, "_held_until", 0.0), began + HIT_HELD)
            if asked_once and now >= limit:
                break
            if not self._held():  # (a real answer to another call's question ended it meanwhile)
                trace.note("runner_free", round(now - began, 1))
                return True
            if until is not None and now >= until:
                trace.note("runner_free", {"touch_deadline": round(now - began, 1)})
                return False
            guard = getattr(self, "execution_guard", None)
            if guard is not None:
                await guard()
            asked = time.monotonic()
            if await self._free_step(until=until):
                trace.note("runner_free", round(time.monotonic() - began, 1))
                return True
            if until is not None and time.monotonic() >= until:  # (the touch's deadline came while the lock was busy)
                trace.note("runner_free", {"touch_deadline": round(time.monotonic() - began, 1)})
                return False
            asked_once = True
            now = time.monotonic()
            limit = min(getattr(self, "_held_until", 0.0), began + HIT_HELD)
            if now >= limit:
                continue  # (the given-up state of another call first, then the limit: given up)
            pause = max(HIT_FREE_MIN, HIT_FREE_EVERY - (now - asked))
            await asyncio.sleep(min(pause, limit - now, *(() if until is None else (max(0.0, until - now),))))
        trace.note("runner_free", {"gave_up": round(time.monotonic() - began, 1)})
        held = self.runner_held = await self._given_up()
        self._hold_carried = False  # (the given-up state stands in for it)
        raise RunnerHeld(held.bundle_id, held.name)

    async def _given_up(self) -> RunnerHeld:
        """(round N) The runner held past HIT_HELD: the state, its app named by iOS's process list (the device channel,
        what the service asks after a reconnect), else by the newest read or open_app; never by the runner."""
        in_front: list = []
        try:
            # (round O, review RN-L2) the one device question, HELD_FRONT_WAIT at most: the app's name comes from what
            # is known already (the installed apps' names, the newest read), else its bundle id names it
            in_front = list(await asyncio.wait_for(self.device.foreground_apps(), HELD_FRONT_WAIT))
        except Exception:  # noqa: BLE001 - the newest read names it then
            pass
        known = getattr(self, "app", None)
        bundle = (in_front[0] if in_front else known[1] if isinstance(known, tuple) else
                  getattr(self, "opened_app", None) or "")
        names = getattr(self, "app_names", None) or {}
        name = names.get(bundle) or (known[0] if isinstance(known, tuple) and known[1] == bundle else "") or bundle
        trace.note("runner_given_up", {"b": bundle})
        return RunnerHeld(bundle, name)

    def hold_standing(self) -> bool:
        """(2.0.3 round S, S-F2) Does a hold still stand that no real answer of the clock has come after: a slow "no
        element answered" set it (_hold) and the clock, asked since, said nothing real (or wasn't asked)? Then the
        daemon carries it past the call that saw it (carry_hold): a touch refused slow at its banner wait's limit, under
        HIT_HELD, left it standing, and the next call's front route went into the held runner and was taken for a lost
        line. False in the given-up state (RunnerHeld) and once carried."""
        if getattr(self, "runner_held", None) is not None or getattr(self, "_hold_carried", False):
            return False
        if getattr(self, "_held_until", 0.0) <= time.monotonic():
            return False
        return getattr(self, "_clock_real_at", 0.0) < getattr(self, "_hold_at", 0.0)

    def carry_hold(self) -> None:
        """(round S, S-F2; round T, review RS-H2) The standing hold outlives the call that saw it, as it is: still a
        hold (_held stays true past its HIT_HELD, until the clock answers for real), not the given-up state. The next
        call waits out the rest of it first (Daemon._carried_wait: the clock about every HIT_FREE_EVERY, nothing else
        asked), and only once the hold is HIT_HELD old with no real answer is it given up (RunnerHeld, not_answering).
        A hold of a few seconds (a shopping app's first seconds: about 4 s) is waited out as before round S."""
        self._hold_carried = True
        trace.note("runner_held_carried", {"age": round(time.monotonic() - getattr(self, "_hold_at", 0.0), 1)})

    def hold_carried(self) -> bool:
        """(round T) Is a hold carried past the call that saw it, and not given up nor answered since?"""
        return getattr(self, "_hold_carried", False) and getattr(self, "runner_held", None) is None

    def hold_left(self) -> float:
        """(round T) Seconds until the carried hold is HIT_HELD old, from its first slow answer (_hold): <= 0 once it
        is."""
        return getattr(self, "_hold_at", 0.0) + HIT_HELD - time.monotonic()

    async def give_up_carried(self) -> RunnerHeld:
        """(round T) The carried hold reached HIT_HELD with no real answer: the given-up state, named by iOS's process
        list as channel_free's (_given_up)."""
        held = self.runner_held = await self._given_up()
        self._hold_carried = False
        return held

    async def _runner_answers_held(self) -> bool:
        """(round S, S-F2) After a front question that got no answer (taken for a dropped line): one question at the
        clock, HIT_FREE_WAIT at most, no quiet after it. True when the runner answers it "no element answered": the line
        is alive and the app holds accessibility, which is no lost connection (round N part 2)."""
        if self.tw_version < 6:
            return False
        lock = getattr(self, "_hit_lock", None)
        if lock is None:
            lock = self._hit_lock = asyncio.Lock()
        try:
            async with lock:
                kind, phone, took = await self._hit_once(HIT_WITNESS, time.monotonic() + HIT_FREE_WAIT,
                                                         {"at": [66, 22]}, wait=HIT_FREE_WAIT, sets_quiet=False)
        except ConnectionLost:
            return False
        # (round T, review RS-M3) only a slow one, as banner_at judges it: a fast "nothing" at the clock is its status
        # bar being redrawn (a banner in motion), which says nothing of a hold
        return kind == "nothing" and slow_nothing(phone if isinstance(phone, float) else None, took)

    async def held_answers(self, wait: float | None = None) -> bool:
        """(round N) The state a given-up hold left (RunnerHeld): one question at the clock, `wait` (HELD_ASK_WAIT) at
        most. A real answer ends it (True); a slow or fast "no element answered", a silence, or the quiet after one
        leave it (False), and nothing else is asked. True at once when there is no such state. (Round O, review RN-L3)
        Its silence sets no quiet: the next call, or the way out's next question, asks again."""
        if getattr(self, "runner_held", None) is None:
            return True
        return await self._free_step(HELD_ASK_WAIT if wait is None else wait, quiet=False)

    async def _held_wait(self) -> None:
        """(round L, M) A hands touch's wait for a held runner, with the hands' lock let go: inside the touch's single
        deadline (_banner_until, set here if no banner wait set it: BANNER_WAIT); at it, the slow words."""
        until = getattr(self, "_banner_until", None)
        if until is None:
            until = self._banner_until = time.monotonic() + BANNER_WAIT
        if not await self.channel_free(until):
            self._gate_at, self._banner_seen, self._banner_until = None, False, None
            raise Refused("not_actionable", COVERED_SLOW)

    async def _free_step(self, wait: float | None = None, quiet: bool = True, until: float | None = None) -> bool:
        """(round L) One question at the clock while a hold is known, under the hit lock: True when it ended the hold (a
        real answer; or no such route, where nothing could be asked again). `wait`: the question's bound (HIT_FREE_WAIT).
        (Round N) A real answer ends a given-up hold (RunnerHeld) too. `until` (CodeRabbit, PR #30): a touch's deadline,
        checked again once the lock is held (another question may have held it that long): past it, nothing is asked."""
        wait = HIT_FREE_WAIT if wait is None else wait
        lock = getattr(self, "_hit_lock", None)
        if lock is None:
            lock = self._hit_lock = asyncio.Lock()
        async with lock:
            if not self._held():
                return True
            if until is not None and time.monotonic() >= until:
                return False
            kind, _, _ = await self._hit_once(HIT_WITNESS, time.monotonic() + wait, {"at": [66, 22]}, wait=wait,
                                              sets_quiet=quiet)
        if kind in ("answer", "unasked"):
            self._held_until = 0.0
            self._hold_carried = False  # (round T: a carried hold ends as any)
            if getattr(self, "runner_held", None) is not None:
                self.runner_held = None
                trace.note("runner_free", "answered")
            return True
        return False

    def _banner_of(self, answer: object, at: tuple[float, float], app: str | None,
                   aimed: "tuple[float, float, float, float] | None", turned: bool) -> str:
        """(round G, H) What a hit answer says is at the point (see banner_at)."""
        value = answer.get("value") if isinstance(answer, dict) else None
        hit = ui.hit_of(value)
        if hit is None or not isinstance(value, dict) or value.get("identity") != "verified":
            return BANNER_UNSURE
        x, y, w, h = hit.rect
        pw, ph = min(self.screen), max(self.screen)
        if (app is None and aimed is not None and not turned
                and (ui._same_rect(hit.rect, aimed) or _rect_within(hit.rect, aimed))):
            # (review RG M1) on the home screen, the element the agent aimed at, as it saw it. Never over an app
            # (review RH H1): there anything of another process inside the app's element is on top of it
            return BANNER_CLEAR
        where = {"at": [round(at[0]), round(at[1])], "frame": [round(v) for v in hit.rect], "b": hit.bundle}
        if hit.bundle == SPRINGBOARD:
            whole = x <= 1 and y <= 1 and w >= pw - 1 and h >= ph - 1
            if whole and app is not None:
                trace.note("banner_hit", where)  # (round K: over an app, SpringBoard's whole screen is a banner in motion)
                return BANNER_OVER
            # (in landscape there is no status bar to keep clear of: any SpringBoard element but an icon or the screen)
            if hit.type != "Icon" and not whole and (turned or y + h > HIT_BANNER_BELOW):
                trace.note("banner_hit", where)
                return BANNER_OVER
            return BANNER_CLEAR
        if hit.bundle == IN_CALL:
            trace.note("banner_hit", where)  # (review RG H2) a call's banner, whatever its frame
            return BANNER_OVER
        if not hit.bundle:
            trace.note("banner_other", where)  # (review RH L2) no process named: it can't be told, home screen too
            return BANNER_UNSURE
        if app is None or hit.bundle == app or hit.bundle.startswith(WEB_CONTENT) or hit.bundle in ui.KEYBOARD_HOSTS:
            return BANNER_CLEAR  # (the home screen's widgets; the app itself; a web page's content; the keyboard)
        trace.note("banner_other", where)  # (review RG H2) another process over the app: it can't be told
        if x <= 1 and y <= 1 and w >= pw - 1 and h >= ph - 1:
            self.sheet_over = hit.bundle  # (round N, N-F5: its whole screen: a sheet over the app; said so)
        return BANNER_UNSURE

    async def front_state(self) -> "Front | None":
        """(2.0.3) What is in front now (FRONT_*): one question to the front route, and iOS's process list only when it
        names SpringBoard. None only on a runner that answered a route version older than 11 (it can't say in a few
        ms: no check). (Review RP2 M1) A version not known (its question failed, after a reconnect) is asked once; still
        unknown: FRONT_UNKNOWN, never "old". (M4) When the route can't name the app and the agent's newest open_app named
        one that iOS's process list still has in the foreground, that app, marked a guess (review RF H1: the list never
        has SpringBoard, so a notification over the app looks the same)."""
        if not self.tw_version and getattr(self, "fast_tap", None) is None:
            routes = getattr(self, "_fast_routes", None)
            if callable(routes):
                try:
                    await routes()
                except ConnectionLost:
                    raise
                except Exception:  # noqa: BLE001 - not learned: unknown below
                    pass
        version = self.tw_version or getattr(self, "tw_answered", None)
        if version is None or (not self.tw_version and version >= FRONT_VERSION):
            return Front(FRONT_UNKNOWN)  # (never answered, or answered once and not now: can't ask the route)
        if version < FRONT_VERSION:
            return None
        front = await self.front_app()
        if front is None:
            opened = getattr(self, "opened_app", None)
            if opened:
                try:
                    in_front = list(await asyncio.wait_for(self.device.foreground_apps(), 5))
                except Exception:
                    return Front(FRONT_UNKNOWN)
                if in_front == [opened]:
                    return Front(FRONT_APP, opened, guessed=True)
            return Front(FRONT_UNKNOWN)
        if front[0] != SPRINGBOARD:
            return Front(FRONT_APP, front[0])
        try:
            in_front = list(await asyncio.wait_for(self.device.foreground_apps(), 5))
        except Exception:
            return Front(FRONT_UNKNOWN)  # (review RP M2b: the process list failing never lets a touch through)
        under = next((b for b in in_front if b != SPRINGBOARD), None)
        return Front(FRONT_OVER, under) if under else Front(FRONT_HOME)

    def _words(self, front: "Front") -> str:
        """How an answer names a front state."""
        def app(bundle: str | None) -> str:
            name = self.app_names.get(bundle or "") if bundle else None
            return f"{name} ({bundle})" if name and name != bundle else (bundle or "an app")
        if front.kind == FRONT_HOME:
            return "the home screen"
        if front.kind == FRONT_OVER:
            return f"something of the system over {app(front.app)}"
        return app(front.app)

    async def _front_rules(self, at: tuple[float, float], wait: bool = True) -> None:
        """(2.0.3, safety; reviews RP and RP2) May an agent's touch at `at` go out? The owner's phone, 2026-11-26: two
        point taps on a screen whose elements weren't read went, the first to a notification banner that opened another
        app on a chat with a person, the second to that chat's call button: a call was started. When Tapwright can't
        tell that a touch is safe, it doesn't touch. Against what the agent's last answer showed (`touch_rules.seen`),
        `element` meaning the touch is aimed at an element of that answer's list (by ref, id or label):
        - the same app in front (or that app, after the system the agent saw over it has gone): yes (and the agent's
          record becomes that app alone: `found_front`); but when only iOS's process list names it (`guessed`, review RF
          H1), a point, an `o` ref or a text of the picture in the banner zone is refused `covered`;
        - the home screen, seen as the home screen: yes; in the banner zone the hit test says (round G): a banner is
          waited out as over an app, anything else goes; when it can't say, a point there is refused `covered`;
        - SpringBoard over the app: an element-aimed touch when the agent's last answer showed that state; a point,
          an `o` ref or a text of the picture: in the banner zone a banner is waited out (`covered` if it stays),
          anywhere else refused `covered`. Element-aimed when the agent saw the app alone: the zone waits, below it
          `stale`;
        - (round G) the route names the agent's app, the touch starts in the banner zone: the hit test at that point
          (banner_at). SpringBoard's element reaching below the status bar is a banner: waited out as above (`covered`
          if it stays), except the agent's flick up from it; the hit test can't say: a point, an `o` ref or a text of
          the picture is refused `covered`, an element-aimed touch goes;
        - anything else: `stale`;
        - the route or the process list can't say (asked twice, 0.2 s apart), the agent's last answer couldn't:
          `unknown_front`; no answer of the agent recorded anything: `no_look`.
        A runner that answered a route version older than 11 can't say in a few ms: no check (no protection; the docs
        say so). `wait` False: a banner to wait out raises _BannerUp instead (the caller waits with no lock held)."""
        rules = self.touch_rules
        if rules is None:
            return
        if not wait and self._held():
            # (review RK M1) under the hands' lock nothing may wait for the runner: right after this touch's banner wait
            # ended clear, its verdict stands if young; with a banner wait under way, the slow words; else the wait for
            # the runner happens with the hands' lock let go (_HeldUp)
            fresh, self._wait_clear = getattr(self, "_wait_clear", None), None
            if fresh is not None and fresh[0] == at and time.monotonic() - fresh[2] < GATE_FRESH:
                front = fresh[1]
                seen = rules.seen
                if (isinstance(front, Front) and isinstance(seen, Front)
                        and (front.kind == FRONT_APP and seen.app == front.app
                             and (seen.kind == FRONT_APP or seen.kind == FRONT_OVER and not rules.element)
                             or front.kind == FRONT_HOME and seen.kind == FRONT_HOME)):
                    trace.note("gate_fresh", {"at": [round(at[0]), round(at[1])]})
                    if front.kind == FRONT_APP:
                        self.found_front = front
                    return
            if getattr(self, "_banner_until", None) is not None:
                raise Refused("not_actionable", COVERED_SLOW)
            raise _HeldUp()
        now = await self.front_state()
        if now is None:
            return
        if now.kind == FRONT_UNKNOWN:
            await asyncio.sleep(0.2)
            now = await self.front_state()
            if now is None or now.kind == FRONT_UNKNOWN:
                raise Refused("not_actionable", UNKNOWN_FRONT)
        seen = rules.seen
        if seen is None:
            raise Refused("not_actionable", NO_LOOK)
        if seen.kind == FRONT_UNKNOWN:
            raise Refused("not_actionable", UNSEEN_FRONT)
        zone = at[1] < BANNER_ZONE
        until: float | None = None

        async def hold() -> Front:
            """One step of waiting a banner out (rule A): _BannerUp when the caller waits with no lock held, `covered`
            once BANNER_WAIT has passed (by the clock, round L: a poll's question at a held runner takes time too, and
            the touch's bound stays BANNER_WAIT and one question), else a poll later, what is in front then."""
            nonlocal until
            if not wait:
                raise _BannerUp()
            if until is None:
                until = time.monotonic() + BANNER_WAIT
            if time.monotonic() >= until:
                raise Refused("not_actionable", self._covered_at_bound())
            await asyncio.sleep(BANNER_POLL)
            await self._banner_guard()  # (review RI M1: a pause during the wait is heard within a poll)
            if self._held() and not await self._free_step():
                return now  # (round K, review RK L5: the runner held: one question at the clock this poll, no more)
            return await self.front_state() or Front(FRONT_UNKNOWN)
        while True:
            if now.kind == FRONT_APP:
                if seen.kind in (FRONT_APP, FRONT_OVER) and seen.app == now.app:
                    if seen.kind == FRONT_OVER and rules.element:
                        # (CodeRabbit, PR #30) the element was of the list the agent read with the system over the
                        # app (an alert's button, a Control Center tile): that has gone, and its place is the app's now
                        raise Refused("stale", f"{self._words(now)} now, not {self._words(seen)} as your last look "
                                               "showed. Nothing was touched; look again.")
                    if now.guessed and zone and not rules.element:
                        # (review RF H1) only the process list says so: a banner over the top can't be ruled out
                        raise Refused("not_actionable", COVERED_GUESS.format(app=self._words(now)))
                    # (round G, iOS 26.5) the route names the app while a timer's banner is over it: in the zone, the
                    # hit test at the touch's start says whether SpringBoard is there
                    said = await self.banner_at(at, now.app, rules.aimed, hands=not wait) if zone else BANNER_UNASKED
                    if said in (BANNER_OVER, BANNER_SLOW) and rules.flick:
                        # (round G) the flick up that sends it away; (round J, the lead) on "slow" too: on a banner it
                        # sends it away, and with none it is the agent's own swipe of the app's top
                        pass
                    elif said == BANNER_OVER or (getattr(self, "_banner_seen", False)
                                                 and said in (BANNER_SLOW, BANNER_UNSURE)):
                        # (review RH H2) a banner, or after one was seen, anything but "clear": waited out, then covered
                        self._banner_seen = True
                        now = await hold()
                        continue
                    elif said == BANNER_SLOW:
                        raise Refused("not_actionable", COVERED_SLOW)  # (round I: an element too, review RH M2)
                    elif said == BANNER_UNSURE and (not rules.element or getattr(self, "sheet_over", None)):
                        # (CodeRabbit, PR #30) another app's whole-screen sheet: an element of the app under it too
                        sheet = getattr(self, "sheet_over", None)
                        raise Refused("not_actionable", COVERED_SHEET.format(bundle=sheet) if sheet else COVERED_UNSURE)
                    self.found_front = now  # (review RP2 M2: what the agent saw over the app has gone)
                    return
                break
            if now.kind == FRONT_HOME:
                if seen.kind != FRONT_HOME:
                    break
                if zone:
                    # (round G) the hit test tells a banner from the home screen's own top: a banner is waited out as
                    # over an app; the top edge (a pull for Control or Notification Center), the status bar, a widget go
                    said = await self.banner_at(at, None, rules.aimed, hands=not wait)
                    if said in (BANNER_OVER, BANNER_SLOW) and rules.flick:
                        return  # (round G, J) the flick up that sends it away
                    if said == BANNER_OVER and not rules.element and not getattr(self, "_banner_seen", False):
                        # (review RG M1) a point on SpringBoard's own screen can't be told from a banner's
                        raise Refused("not_actionable", COVERED_HOME)
                    if said == BANNER_OVER or (getattr(self, "_banner_seen", False)
                                               and said in (BANNER_SLOW, BANNER_UNSURE)):
                        self._banner_seen = True  # (an element of the look with something else over it: a banner)
                        now = await hold()
                        continue
                    if said == BANNER_SLOW:
                        raise Refused("not_actionable", COVERED_SLOW)  # (round I: an element too)
                    if said in (BANNER_UNSURE, BANNER_UNASKED) and not rules.element:
                        raise Refused("not_actionable", COVERED_HOME)  # (review RP2 M3: it can't tell)
                return
            if now.kind != FRONT_OVER:  # (unknown while a banner was being waited out)
                raise Refused("not_actionable", UNKNOWN_FRONT)
            if seen.app != now.app or seen.kind == FRONT_HOME:
                break
            if rules.element and seen.kind == FRONT_OVER:
                return  # it saw the system over the app, and aims at an element of what it saw
            if not zone:
                if rules.element:  # (it saw the app alone: something of the system came over it since)
                    raise Refused("stale", f"{self._words(now)} now, not {self._words(seen)} as your last look showed. "
                                           "Nothing was touched; look again.")
                # (review RP2 M2) a point, an o ref, a text of the picture: never on the system over the app
                raise Refused("not_actionable", f"covered: {self._words(now)}, so nothing was touched; aim at one of "
                                                "its elements by ref, or look again.")
            # in the banner zone, SpringBoard over the app: a banner may be there; wait it out (rule A), unless the
            # touch is the agent's flick up from it (round G: how a person sends a banner away)
            if rules.flick and await self.banner_at(at, now.app) == BANNER_OVER:
                return
            self._banner_seen = True  # (review RH H2: should the route name the app again, only "clear" ends it)
            self._banner_real = True  # (round L: the route names SpringBoard over the app: a banner's words at the bound)
            now = await hold()
        raise Refused("stale", f"{self._words(now)} is in front now, not {self._words(seen)}, where your last look was. "
                               "Nothing was touched; look again.")

    async def see_front(self, element: bool = False) -> None:
        """(2.0.3) A tool's own read just showed it the screen (Notification Center opened by the tool itself): its
        next touches in this action are checked against what is in front now, not the agent's older answer."""
        if self.touch_rules is not None:
            self.touch_rules = TouchRules(await self.front_state(), element or self.touch_rules.element)

    async def _clear_of_banner(self, at: tuple[float, float]) -> "tuple[str, int | None] | None | bool":
        """A touch near the top of the screen while a notification banner is there lands on the banner: a tap opens
        the notification's app, a drag down opens the notification out (seen on the phone 2026-11-10: a scroll to
        a list's start pulled a Mail banner open). While a banner shows, WebDriverAgent names SpringBoard as the
        app in front though the phone's process list still has the app: then this waits for it to leave, and
        refuses the touch if it doesn't. Touches lower down, and SpringBoard's own screens with no app in front
        (the home screen), are not held up. Returns the last answer of the front route (None: it couldn't say; False:
        it wasn't asked), for _seen_check to use instead of asking again."""
        if at[1] >= BANNER_ZONE:
            return False
        # (review L-M2) the agent's newest read is of SpringBoard itself (Control Center pulled over an app, the home
        # screen): it is aiming at SpringBoard's own screen, and nothing slid over what it aimed at. (2.0.3) Kept for
        # points too: the route names SpringBoard for a banner and for SpringBoard's own screen alike, so a banner over
        # SpringBoard can't be told from it; rule B still refuses a touch whose app the agent didn't see
        if isinstance(self.read_app, tuple) and self.read_app[1] == SPRINGBOARD:
            return False
        waited = 0.0
        front: "tuple[str, int | None] | None | bool" = False
        while True:
            if self.tw_version >= FRONT_VERSION:
                # (review L-M1) only the route that answers in a few ms: when it can't say, the touch goes out.
                # foreground_bundle_id would fall back to activeAppInfo, 1.7-30 s on a screen that doesn't answer
                front = await self.front_app()
                if front is None or front[0] != SPRINGBOARD:
                    return front
            elif await self.foreground_bundle_id() != SPRINGBOARD:
                return False
            try:
                in_front = await asyncio.wait_for(self.device.foreground_apps(), 5)
            except Exception:
                return front  # the phone can't say: WebDriverAgent's answer alone is no reason to refuse
            if not in_front:
                return front  # the home screen itself: nothing lies over an app
            if waited >= BANNER_WAIT:
                raise Refused("not_actionable", "covered: a notification is over the top of the screen, so nothing "
                                                "was touched; wait a few seconds, then try again.")
            await asyncio.sleep(BANNER_POLL)
            waited += BANNER_POLL

    async def tap(self, target: ui.Target, hold_ms: int = TAP_MS) -> str:
        self.typed_into = None
        x, y, what = self._target(target)
        await self._touching(hold_ms, (x, y))
        if not await self._fast_tap(x, y, hold_ms):
            await self._call("POST", "/session/{sid}/actions", ui.tap(x, y, hold_ms), timeout=hold_ms / 1000 + ACTION_WAIT)
        self.last_point = (x, y)
        return f"{'Long-pressed' if hold_ms > 300 else 'Tapped'} {what}."

    async def _fast_tap(self, x: float, y: float, hold_ms: int) -> bool:
        """Touch through the fork's fast tap route when this runner has it (see FAST_TAP). False when the touch is
        left to W3C actions: no route, not portrait (the route's points are portrait screen points), or an error
        that says nothing was played. An error that may come after the touch was played is raised, never retried.
        Hands (HID) go first when they can be used: they need no runner route and work in landscape too."""
        # (getattr: a session without hands, a test's fake among them, runs only the code below, as before 2.0)
        if getattr(self, "hands", None) is not None and await self._hands_tap(x, y, hold_ms):
            return True
        if FAST_TAP == "0" or self.turn != 0 or self.screen[0] > self.screen[1]:
            return False
        await self.ensure()
        await self._fast_routes()
        if not self.fast_tap:
            return False
        body = {"x": x, "y": y, "duration": hold_ms / 1000, "wait": True}
        checked = self.front_apps is not None
        if checked:
            if self.tw_version < 4:
                raise FrontUnchecked("this runner's tap can't check the app in front")
            body["apps"] = list(self.front_apps or ())
        try:
            await self._call("POST", FAST_TAP_PATH, body, timeout=hold_ms / 1000 + ACTION_WAIT)
        except DeviceError as e:
            said = str(e).lower()
            if checked and not isinstance(e, ConnectionLost) and FRONT_REFUSED in said:
                raise FrontUnchecked(str(e)) from None  # nothing was touched: the daemon asks its own way
            if "unknown command" in said or "unknown method" in said:
                self.fast_tap = False  # not this runner's route after all
            elif not ("invalid argument" in said or "too many touches" in said):
                raise  # it may have been played (a timeout, XCTest's own error): touching again could do it twice
            self._regate()  # (nothing was played: the W3C actions are checked again)
            getattr(self.device, "log", lambda *a: None)(f"fast tap refused ({e}): W3C actions instead")
            return False
        if checked:
            self.front_apps = None  # the app in front was one of them as the touch went out: the check is done
        return True

    def hands_on(self) -> bool:
        """May the next touch try the hands? Not with TAPWRIGHT_HANDS=runner, not without Hands, and not while a tap
        is to check the app in front itself (front_apps: only the runner's tap can)."""
        return self.hands is not None and HANDS != "runner" and self.front_apps is None

    async def _by_hands(self, what: str, act: "Callable[[Callable[[], Awaitable[None]]], Awaitable[None]]",
                        input_: bool = True, detail: str = "") -> bool:
        """Play a gesture through the hands: `act(before)` sends it, awaiting `before` right before its first report.
        `before` makes the checks a runner request makes in _timed (the execution guard, then the one-use input check
        for an input), and the trace notes the touch. False, with nothing sent, when the hands can't be used
        (HandsUnavailable): the caller uses the runner, as before. Anything else is raised as it came (ConnectionLost,
        HandsError = Unanswered: it may have been played; or a guard's refusal: nothing was sent)."""
        if not self.hands_on():
            return False
        await self.ensure()  # (the tunnel, and the screen's size from the runner's session)
        validate = self.before_input if input_ else None
        pending: list = []

        async def before() -> None:
            if input_:
                # (2.0.3) the app in front, right before the first HID report; then the guard, after it (review RP2 H1:
                # a pause, a lock or a person's touch during the check stops the touch)
                await self._gate(wait=False)
            guard = getattr(self, "execution_guard", None)
            if guard is not None:
                await guard()
            if validate is not None:
                await validate(self.input_point)
            pending.append(None if QUIET.get() else trace.request("POST", f"{HANDS_PATH}/{what}"))

        began = time.monotonic()
        try:
            while True:
                try:
                    await act(before)
                    break
                except _BannerUp:
                    # (review RP2 H1) nothing was sent; the hands' lock is let go while a banner is waited out (a
                    # person's touch goes first), then the touch starts again: the check and the guard run again
                    await self._wait_banner()
                except _HeldUp:
                    # (review RK M1) the runner held by the app: waited for with the hands' lock let go, then again;
                    # (review RL L2) inside the touch's single BANNER_WAIT, which a new hold doesn't restart
                    await self._held_wait()
        except hands_module.HandsUnavailable:
            return False
        except BaseException:
            if pending:
                trace.response(pending[0], ok=False)
            raise
        if validate is not None and self.before_input is validate:
            self.before_input = None
        if pending:
            trace.response(pending[0], {})
        self.hands_at = time.monotonic()  # (the daemon's settle believes "nothing changed" later after a hands touch)
        # the touch connection it went through, for the verdict that comes after the settle (Hands.judged)
        self.hands_connection = getattr(self.hands, "connection", None)
        if TRACE and not QUIET.get():
            getattr(self.device, "log", lambda *a: None)(
                f"trace hands {what} {(time.monotonic() - began) * 1000:.0f} ms{detail}")
        return True

    async def _hands_tap(self, x: float, y: float, hold_ms: int) -> bool:
        """A tap or long press through the hands. A plain tap (TAP_MS) gets a human hold and lands within a point of
        its target (motion.tap); a long press keeps its hold."""
        hands = self.hands
        if hands is None or not self.hands_on():
            return False
        if hold_ms == TAP_MS:
            hx, hy, held = motion.tap(x, y, self.motion_rng)
            if held:
                x, y, hold_ms = hx, hy, held
        return await self._by_hands("tap", lambda before: hands.tap(x, y, hold_ms, screen=self.screen,
                                                                    turn=self.turn, before=before),
                                    detail=f" at ({x:.1f}, {y:.1f}) hold {hold_ms} ms, turn {self.turn}")

    async def hands_keys(self, text: str) -> bool:
        """Type US-ASCII text into whatever has focus through the HID keyboard (for later use: type_text still goes
        through the runner). False when the hands can't be used; hands.Untypable before anything is sent."""
        hands = self.hands
        if hands is None:
            return False
        hands_module.plan_keys(text)  # (Untypable before any guard or touch hook runs)
        return await self._by_hands("keys", lambda before: hands.keys(text, before=before))

    async def pinch(self, x: float, y: float, scale: float, ms: int = 500) -> str:
        """Two fingers about (x, y), moving apart (scale > 1: zoom in) or together (scale < 1) along a line through it,
        over `ms`. Hands only: Refused(unsupported) when they can't be used (nothing was touched)."""
        hands = self.hands
        if hands is None or not self.hands_on():
            raise Refused("unsupported", "pinch needs touches through CoreDevice HID, which this phone or Tapwright "
                                         "setup doesn't have; nothing was touched.")
        self.typed_into = None
        await self.ensure()
        if hands.why_not(getattr(self.device, "rsd", None)) is not None:  # (review R200 L1: before Tapline hears of it)
            raise Refused("unsupported", "pinch needs touches through CoreDevice HID, which this phone doesn't take; "
                                         "nothing was touched.")
        near, far = pinch_spread(x, y, scale, self.screen)
        try:
            # (CodeRabbit, PR #26) a turn the hands can't map is refused before the touch hook tells anyone of a touch
            hands_module.Hands._mapped(self.screen, self.turn)
        except hands_module.HandsUnavailable as e:
            raise Refused("unsupported", f"pinch needs touches through CoreDevice HID, and they can't be used now "
                                         f"({e.reason}); nothing was touched.") from None
        await self._touching(ms, (x, y))
        ends = [(-1.0, 0.0), (1.0, 0.0)]
        paths = [[(0.0, x + dx * near, y + dy * near), (float(ms), x + dx * far, y + dy * far)] for dx, dy in ends]
        if not await self._by_hands("pinch", lambda before: hands.multi(paths, screen=self.screen, turn=self.turn,
                                                                         before=before)):
            raise Refused("unsupported", "pinch needs touches through CoreDevice HID, and they can't be used now; "
                                         "nothing was touched.")
        self.last_point = (x, y)
        return f"{'spread' if scale >= 1 else 'pinched'} two fingers at ({round(x)}, {round(y)}), " \
               f"{2 * near:.0f} -> {2 * far:.0f} pt apart"

    async def hit_at(self, x: float, y: float) -> ui.Hit | None:
        """The element a touch at (x, y) would land on, as the fork's hit test says (HIT_PATH, route version 5). None
        when it can't be asked or told: the check is off, the phone isn't upright (the route takes portrait points),
        the runner is older, or any error but a dropped line (raised, as the touch would raise it)."""
        if HIT_CHECK == "0" or self.turn != 0 or self.screen[0] > self.screen[1]:
            return None
        await self.ensure()
        def unproven(gate: str) -> Refused:
            refused = Refused("not_actionable", "covered: the runner could not verify the touch recipient; "
                                               "nothing was touched. Look again before another action.")
            refused.hit_check = {"gate": gate, "phase": "runner", "probes": 0}
            return refused

        try:
            await self._fast_routes(strict=True)
        except ConnectionLost:
            raise
        except Exception:
            raise unproven("capability_unproven") from None
        if self.tw_version < 5:
            return None
        strict_proof = self.tw_version >= 6
        # (review RH L3) one hit question out at a time with the banner check's, and none while a silent one may still
        # hold the runner (HIT_QUIET)
        lock = getattr(self, "_hit_lock", None)
        if lock is None:
            lock = self._hit_lock = asyncio.Lock()
        await lock.acquire()
        try:
            if getattr(self, "_hit_quiet_until", 0.0) > time.monotonic() or self._held():
                if strict_proof:
                    raise unproven("hit_quiet" if not self._held() else "hit_held")
                return None
            return await self._hit_at_asked(x, y, strict_proof, unproven)
        finally:
            lock.release()

    async def _hit_at_asked(self, x: float, y: float, strict_proof: bool,
                            unproven: "Callable[[str], Refused]") -> ui.Hit | None:
        """hit_at's question, under the hit lock."""
        began = time.monotonic()
        try:
            # (not asked twice when it gets no answer: a stalled check is skipped, Unanswered, not waited on again;
            # review RI L1: `once`, so its silence is no dropped line either: no /status, no reconnect, the quiet set)
            answer = await self._call("GET", f"{HIT_PATH}?x={x:.2f}&y={y:.2f}", timeout=HIT_WAIT, again=False,
                                      once=True)
        except ConnectionLost:
            raise
        except Exception as e:  # noqa: BLE001 - whatever stopped the check, the touch goes unchecked
            if isinstance(e, DeviceError) and HIT_IDENTITY_REFUSED in str(e):
                raise unproven("identity_unproven") from None
            if isinstance(e, Unanswered):
                self._hit_quiet_until = time.monotonic() + HIT_QUIET  # (review RH L3: the runner may still be on it)
            elif (isinstance(e, DeviceError) and HIT_NOTHING in str(e).lower()
                  and slow_nothing(phone_time(str(e).lower(), self.tw_version), time.monotonic() - began)):
                # (review RK H2) the process at the point didn't answer: the runner is held until it does, and the front
                # route after this must wait for it (no witness here: the absolute time; a hold set by mistake costs one
                # question at the clock)
                self._hold("hit_at", (x, y))
            if strict_proof:
                # Version 6 binds attributes to the AX hit. Every failed attempted proof is terminal, including
                # Unanswered (the runner stayed alive after a timeout), rather than an unchecked ordinary tap.
                raise unproven("hit_timeout" if isinstance(e, Unanswered) else "hit_request_failed") from None
            getattr(self.device, "log", lambda *a: None)(f"hit test refused ({e}): the touch goes unchecked")
            trace.note("hit_ms", round((time.monotonic() - began) * 1000, 1))
            return None
        trace.note("hit_ms", round((time.monotonic() - began) * 1000, 1))
        value = answer.get("value") if isinstance(answer, dict) else None
        if isinstance(value, dict) and isinstance(value.get("ms"), dict):
            trace.note("hit_wda_ms", {k: v for k, v in value["ms"].items()
                                      if isinstance(v, (int, float)) and not isinstance(v, bool)})
        hit = ui.hit_of(value)
        if strict_proof and (hit is None or value.get("identity") != "verified"):
            raise unproven("invalid_hit_proof")
        return hit

    def front_by_touch(self) -> bool:
        """Can the next tap check the app in front itself (personal mode, FRONT_IN_TAP)? Only when it would go through
        the fork's fast tap with `apps`: a runner known to be version 4, in portrait, the fast tap on."""
        return (FRONT_IN_TAP != "0" and FAST_TAP != "0" and self.sid is not None and self.fast_tap is True
                and self.tw_version >= 4 and self.turn == 0 and self.screen[0] <= self.screen[1])

    async def _fast_routes(self, strict: bool = False) -> None:
        """Ask the runner once per session which of the fork's fast routes it has (Appium's build: none). Only an
        answer that says the route is not there is remembered as version 0. A malformed answer or a probe that
        failed for another reason (a timeout, a dropped line) is not remembered (CodeRabbit, PR #7): a touch goes on
        through W3C actions this time and asks again next time, and with `strict` the failure is raised."""
        if self.fast_tap is not None:
            return
        try:
            answer = await self._call("GET", FAST_VERSION_PATH, timeout=LOCK_WAIT)
        except DeviceError as e:
            said = str(e).lower()
            if "unknown command" not in said and "unknown method" not in said:
                if strict:
                    raise
                return  # fast_tap stays None: no fast route for this call, and the next one asks again
            self.fast_tap, self.fast_drag, self.tw_version = False, False, 0
            self.tw_answered = 0  # (an answer: a runner without the fork's routes)
            return
        value = answer.get("value") if isinstance(answer, dict) else None
        version = value.get("tapwright") if isinstance(value, dict) else None
        if not isinstance(version, int) or isinstance(version, bool) or version < 0:
            if strict:
                raise DeviceError("tapwright runner: capability_unproven")
            return  # do not cache a malformed response as proof of a legacy runner
        self.fast_tap, self.fast_drag = version >= 1, version >= 2
        self.tw_version = self.tw_answered = version
        if version >= BOUNDED_VERSION and not self.bounded and self.sid is not None:
            await self._bound_late()

    async def _bound_late(self) -> None:
        """(review R200v L6) ensure's version probe couldn't tell, so the session started as 1.10.0's did; a later probe
        says version 9: the runner's ceiling for every accessibility read is set now (its reads are budgeted from now
        on too), so the session isn't half-bounded. A setting that can't be posted leaves it as it was (asked again by
        the next session)."""
        try:
            await self._call("POST", "/session/{sid}/appium/settings", {"settings": {"twAXTimeout": AX_TIMEOUT}},
                             timeout=READ_CUT)
        except ConnectionLost:
            raise
        except DeviceError as e:
            getattr(self.device, "log", lambda *a: None)(f"the runner's AX timeout couldn't be set ({e})")
            return
        self.bounded = True

    async def runner_version(self) -> int:
        """The fork's route version of this phone's runner (0: not the fork)."""
        await self.ensure()
        await self._fast_routes(strict=True)  # a probe that failed is raised: media answers a retryable error
        return self.tw_version

    async def peek_alert(self, timeout: float = 5.0) -> ui.Alert | None:
        """The system alert on screen now, read apart from `items` (media's watch asks it while the runner waits for
        the person: the list the agent works with isn't touched)."""
        return ui.read(await self._source(timeout), self.screen).alert

    async def photos_access(self) -> dict:
        """The runner's access to the photo library: {"add": "authorized|denied|notDetermined|…", "full": …}."""
        value = (await self._call("GET", PHOTOS_ACCESS_PATH, timeout=LOCK_WAIT)).get("value")
        return value if isinstance(value, dict) else {}

    async def photos_add(self, pictures: "list[tuple[str, bytes] | media.Video]",
                         sent: "Callable[[], Awaitable[None]] | None" = None, asks: bool = False) -> list[str]:
        """Add pictures (file name, bytes) and videos (media.Video) to the photo library with add-only access; the
        local identifiers made, in order. It may wait for the person to allow the runner once. Never sent twice.
        Pictures alone go in route version 3's shape. A video is first copied into the runner's own container over
        house_arrest (STAGE_ROOT/<token>/), and the add names it there (route version 7): the runner adds it by file
        and the library moves it in. The staged folder is removed when the add answered (added or refused) or the copy
        failed; after a timeout (the runner's own, or no answer) the library may still be moving it, so the runner
        removes the files when its change ends, and the next video add sweeps folders older than STAGE_STALE (after a
        dropped line the reconnect stops the runner, and only that sweep removes them). `sent()` is called right before
        the add goes to the runner (after the copy); it may refuse the add (a pause): then nothing is sent and the
        staged folder is removed. `asks`: iOS asks the person first (a first add), so a video add waits for the
        answer and then the import (photos_timeout)."""
        videos = [(i, item) for i, item in enumerate(pictures) if isinstance(item, media.Video)]
        if not videos:
            def encode() -> dict:
                return {"images": [base64.b64encode(data).decode("ascii") for _, data in pictures],
                        "names": [name for name, _ in pictures]}
            body = await asyncio.to_thread(encode)  # (tens of MB: not on the event loop)
            if sent is not None:
                await sent()
            value = (await self._call("POST", PHOTOS_ADD_PATH, body, timeout=PHOTOS_WAIT)).get("value")
            return _identifiers(value)
        runner = store.load_state().get("runner_id")
        if not runner:
            raise NotSetUp("WebDriverAgent isn't set up on this phone yet: run `tapwright setup`")
        folder = f"{STAGE_ROOT}/{secrets.token_hex(8)}"
        files = {i: f"{folder}/{n}{video.path.suffix.lower()}" for n, (i, video) in enumerate(videos, 1)}
        try:
            await self.device.sweep_media(runner, STAGE_STALE)
        except DeviceError:
            pass  # (only old leftovers: never a reason to stop this add)
        # removed afterwards unless the add went out and its end isn't known (no answer, a dropped line, cancelled)
        remove = True
        try:
            await self.device.stage_media(runner, folder, [(video.path, files[i]) for i, video in videos],
                                          stamps=[video.stamp for _, video in videos])

            def encode_items() -> dict:
                return {"items": [{"kind": "video", "file": files[i], "name": item.name}
                                  if isinstance(item, media.Video) else
                                  {"kind": "photo", "data": base64.b64encode(item[1]).decode("ascii"), "name": item[0]}
                                  for i, item in enumerate(pictures)]}
            body = await asyncio.to_thread(encode_items)
            if sent is not None:
                await sent()
            remove = False
            try:
                answer = await self._call("POST", PHOTOS_ADD_PATH, body,
                                          timeout=photos_timeout(sum(video.size for _, video in videos), asks))
            except (Unanswered, ConnectionLost, device_module.RunnerTimedOut):
                # its end isn't known: the library may still be moving the clip (the runner's own wait ran out, or no
                # answer came). The runner removes it when the change ends; the hour-old sweep is the backstop. Removing
                # it now could break an import that would have finished (review R179 M1)
                raise
            except DeviceError:
                remove = True  # the runner answered with a refusal (bad input, no access, unsupported): nothing moves
                raise
            remove = True
            return _identifiers(answer.get("value"))
        finally:
            if remove:
                try:
                    await self.device.unstage_media(runner, folder)
                except DeviceError:
                    pass  # (the runner removed them already, or iOS clears tmp in time)

    async def photos_delete(self, identifiers: list[str]) -> int:
        """Delete pictures by local identifier (needs the runner's full access; iOS asks the person to confirm).
        Returns how many were deleted; a DeviceError whose message says "full access" when that isn't granted."""
        value = (await self._call("POST", PHOTOS_DELETE_PATH, {"identifiers": identifiers},
                                  timeout=PHOTOS_WAIT)).get("value")
        deleted = value.get("deleted") if isinstance(value, dict) else None
        return deleted if isinstance(deleted, int) and not isinstance(deleted, bool) else 0

    async def _drag(self, points: list, w3c: dict, shaped: list | None = None) -> None:
        """Play a finger path, [(t_ms from the press, x, y), ...] in points: through the fork's fast drag route when
        this runner has it (see FAST_DRAG), else as the W3C actions `w3c`, which describe the same path. `shaped`: the
        human-like version of a free swipe (motion.swipe), played instead of `points` when hands play it."""
        if not await self._fast_drag(points, shaped):
            await self._call("POST", "/session/{sid}/actions", w3c, timeout=points[-1][0] / 1000 + ACTION_WAIT)

    async def _fast_drag(self, points: list, shaped: list | None = None) -> bool:
        """False when the drag is left to W3C actions: no route, not portrait, or an error that says nothing was
        played. An error that may come after the drag was played is raised, never retried: it would scroll twice.
        Hands (HID) go first when they can be used."""
        hands = getattr(self, "hands", None)  # (as in _fast_tap: no hands, the code as before 2.0)
        if hands is not None:
            path = shaped or points
            if await self._by_hands("drag", lambda before: hands.path(path, screen=self.screen, turn=self.turn,
                                                                      before=before)):
                return True
        if FAST_DRAG == "0" or FAST_TAP == "0" or self.turn != 0 or self.screen[0] > self.screen[1]:
            return False
        await self.ensure()
        await self._fast_routes()
        if not self.fast_drag:
            return False
        body = {"points": [[round(t), round(x), round(y)] for t, x, y in points]}  # W3C actions round them too
        try:
            await self._call("POST", FAST_DRAG_PATH, body, timeout=points[-1][0] / 1000 + ACTION_WAIT)
        except DeviceError as e:
            said = str(e).lower()
            if "unknown command" in said or "unknown method" in said:
                self.fast_drag = False  # not this runner's route after all
            elif not ("invalid argument" in said or "too many touches" in said):
                raise  # it may have been played (a timeout, XCTest's own error): dragging again could do it twice
            self._regate()  # (nothing was played: the W3C actions are checked again)
            getattr(self.device, "log", lambda *a: None)(f"fast drag refused ({e}): W3C actions instead")
            return False
        return True

    async def type_text(self, text: str, replace: bool = False, submit: bool = False,
                        target: ui.Target | None = None, at: tuple[float, float] | None = None,
                        no_tap: bool = False) -> str:
        """Type into the target (tapped first to focus it, at `at` when given: the part of it that shows),
        or into whatever has focus. A point names the input under it, not a touch: with none there,
        nothing is typed. `no_tap`: the read said the target has the focus, and no focusing tap may be made (something
        is drawn over it); if the focus is elsewhere now, nothing is done."""
        point = None  # the agent's own point: the input under it is the target, in the read and on the phone (1.7.8)
        if target is not None and target.point is not None:
            point = target.point
            field = ui.at_point(self.items, point, "input")
            if field is None:
                x, y = point
                raise Refused("not_actionable", f"no_action: Nothing at ({round(x)}, {round(y)}) takes typing.")
            target = ui.Target(ref=field.ref)
        element, hit, again, at_end, what = None, None, False, False, "the field"
        tapped: tuple[float, float] | None = None  # where the focusing tap went; None when no tap was made
        self._placed_proof = None
        # with the keyboard already up, the focusing tap moves focus from another field, and iOS may swap in that
        # field's keyboard as the keys go out: some went missing on the phone ("abc" landed as "a", 2026-11-11)
        switched = self.last_keyboard
        self.retyped = False
        if target is not None:
            what = self._target(target)[2]
            hit = ui.resolve(self.items, target)
            key = _field_key(hit) if not isinstance(hit, tuple) else None
            focused, self.typed_into = self.typed_into, None
            place, self.typed_place = self.typed_place, None
            try:
                if (key and focused and focused[0] == key and isinstance(hit, ui.Element) and point is None
                        and self._unique(hit) and place is not None and _near((hit.x, hit.y, hit.width), place)
                        and await self._active_element() == focused[1]
                        and await self._where_listed(focused[1], hit)):
                    # still focused by our own typing, nothing done since: no tap. Only for the very field typed into:
                    # its key the one of its kind in the list, listed where it was (1.7.8, Opus review H1: two posts
                    # labelled alike, and the keys for the second went into the first), and the focused element is
                    # still there: an automatic scroll can carry it off and bring another input with its key to its
                    # place (CodeRabbit, PR #18)
                    element, again = focused[1], True
                elif (isinstance(hit, ui.Element) and hit.focused and self.tw_version >= 5
                      and (element := await self._active_element()) is not None
                      and await self._is_field(element, hit, point=point, listed=hit.rect)):
                    # the runner's read says this very field has the keyboard focus (its `f`, never a guess from frames
                    # or labels: 1.6.2): a tap on it would only open iOS's caret menu (Select, Paste). Where the caret is
                    # isn't known, so old text is cleared WebDriverAgent's way, and no focus moved: nothing to retype.
                    # The element focused NOW must be that field (its name, else its place in a fresh read): the app may
                    # have moved the focus since the read, and a clear and the keys would go to another field, a value
                    # of the vault with them (CodeRabbit, PR #12, session.py)
                    switched = False
                elif no_tap:
                    raise Refused("stale", f"the focus moved off {what} since the last read; look again. Nothing was "
                                           "typed.")
                else:
                    element = None  # (a focused element that isn't the field is never typed into: focus it first)
                    # Old text to replace: the tap goes near the field's right end, where iOS puts the caret at the end
                    # of short text, so backspaces can delete it in the same call as the new keys (0.45 s less than
                    # WebDriverAgent's clear, measured on the phone 2026-11-11). Only where the field's middle shows
                    # (no `at`), there is room, and nothing is pressed after the text; the value is read back after.
                    at_end = (replace and not submit and at is None and isinstance(hit, ui.Element) and not hit.secure
                              and hit.value not in ("", hit.placeholder) and len(hit.value) <= BACKSPACE_MAX
                              and hit.width >= 2 * END_INSET)
                    if at_end and isinstance(hit, ui.Element):
                        tapped = (hit.x + hit.width - END_INSET, hit.y + hit.height / 2)
                    elif at is not None:
                        tapped = at
                    else:
                        x, y, _ = self._target(target)
                        tapped = (x, y)
                    await self.tap(target if tapped is None or (at is None and not at_end)
                                   else ui.Target(x=tapped[0], y=tapped[1]))
                    # focus is what counts: with a hardware keyboard no software one shows
                    element = (await self._has_focus(hit, point=point, tapped=tapped) if not isinstance(hit, tuple)
                               else await self._active_element())
                    if element is None and not isinstance(hit, tuple):
                        raise Refused("not_actionable", f"no_action: {what} didn't take focus.")
            except _FieldGone:
                raise Refused("stale", "the focused field went away while it was checked (the app redrew it); look "
                                       "again. Nothing was typed.") from None
        note, keys = "", text + ("\n" if submit else "")
        field = hit if isinstance(hit, ui.Element) else None
        if replace and field is not None and not field.secure and field.value in ("", field.placeholder):
            pass  # empty: nothing to clear (WebDriverAgent reports an empty field's placeholder as its value)
        elif replace and (again or at_end) and field is not None and not field.secure and len(field.value) <= BACKSPACE_MAX:
            keys = "\b" * len(field.value) + keys  # the cursor is at the end of the old text: delete it
        elif replace:
            element = element or await self._active_element()
            if element and field is not None and self._placed_proof != element:
                # 1.7.8 (bug 6, Opus review H2): the clear wipes a field's text, so the field it goes to is proven right
                # before it, by its place in a fresh read, never by a label or an identifier alone: two posts of a
                # thread share a label, or a testID repeated on every row
                try:
                    proven = await self._is_field(element, field, strict=True, point=point, tapped=tapped,
                                                  listed=None if tapped else field.rect)
                except _FieldGone:
                    proven = False
                if not proven:
                    raise Refused("stale", f"the focused field isn't {what} any more; look again. Nothing was cleared "
                                           "or typed.")
            if element:
                await self._call("POST", f"/session/{{sid}}/element/{element}/clear", {})
            else:
                note = " (couldn't find the focused field to clear, so the old text may remain)"
        await self._keys(keys)
        if at_end and element and await self._attribute(element, "value") != text:
            # the caret wasn't at the end (the old text ran past the tap): the field holds a mix. Clear it and
            # type again, the long way; the answer reads the value afterwards either way
            await self._call("POST", f"/session/{{sid}}/element/{element}/clear", {})
            await self._keys(text)
        elif (replace and not submit and switched and not again and element and field is not None and text
              and not field.secure and await self._attribute(element, "value") != text):
            # keys went missing as focus moved between fields: the field holds something else. Cleared and typed
            # once more; the answer says so (Daemon, NOTE_RETYPED). Typing again into the same field isn't checked:
            # its keyboard is already the field's own (and that path is the fast one). Nothing to retype when the
            # text is empty: the clear was all (an empty field may read as its placeholder)
            await self._call("POST", f"/session/{{sid}}/element/{element}/clear", {})
            await self._keys(text)
            self.retyped = True
        if field is not None and element and not submit:  # return may move focus on; keys never do
            self.typed_into = (_field_key(field), element)
            self.typed_place = (field.x, field.y, field.width)
        return f"typed {len(text)} characters" + (" and pressed return" if submit else "") + note

    async def _keys(self, keys: str) -> None:
        """Send keys to the focused field. None at all is nothing to send: the runner's keys route refuses an empty
        list (an empty text with replace only clears the field: review R179 M3)."""
        if not keys:
            return
        await self._touching(KEY_MS * len(keys))
        await self._call("POST", "/session/{sid}/wda/keys", {"value": list(keys)})

    async def retype(self, text: str) -> bool:
        """Clear the focused field and type `text` again, once: the first keys went missing (the daemon saw the
        field holding something else). False when no field has focus."""
        element = await self._active_element()
        if not element:
            return False
        await self._call("POST", f"/session/{{sid}}/element/{element}/clear", {})
        await self._keys(text)
        return True

    async def is_focused(self, field: ui.Element) -> bool:
        """Is this listed input the one WebDriverAgent says has focus (the keyboard's target)? Where its key isn't the
        only one in the list, its place in a fresh read must be where it is listed. An element that went stale while
        it was checked isn't (the caller says what that means)."""
        if field.role != "input":
            return False
        active = await self._active_element()
        try:
            return bool(active) and await self._is_field(active, field, listed=field.rect)
        except _FieldGone:
            return False

    async def dismiss_keyboard(self, keys: tuple[str, ...] = ()) -> bool:
        """WebDriverAgent's own way of closing the keyboard, which presses no return. False when it refuses (the
        keyboard has no key to dismiss it with); a dead line still raises ConnectionLost. `keys` (1.9.0): names of the
        keyboard's keys it may press to close it (its Done key), tried first."""
        self.typed_into = None
        try:
            await self._call("POST", "/session/{sid}/wda/keyboard/dismiss", {"keyNames": list(keys)} if keys else {})
        except ConnectionLost:
            raise
        except DeviceError:
            return False
        return True

    async def _active_element(self) -> str | None:
        """WebDriverAgent's element with the keyboard's focus; None when none has it. WebDriverAgent says that as an
        error (`no such element`), which was passed on as `internal` (2.0.1, seen 2026-11-26: a field of an in-app
        browser sheet takes the focus a moment after the tap, the app around it being idle at once, and the first
        question after the focusing tap came before it). None, so the wait for focus asks again."""
        try:
            focused = (await self._call("GET", "/session/{sid}/element/active")).get("value") or {}
        except ConnectionLost:
            raise
        except DeviceError as e:
            if "no such element" not in str(e).lower():
                raise
            return None
        return focused.get("ELEMENT") or next(iter(focused.values()), None)

    async def _attribute(self, element: str, name: str) -> str | None:
        try:
            value = (await self._call("GET", f"/session/{{sid}}/element/{element}/attribute/{name}")).get("value")
        except ConnectionLost:
            raise
        except DeviceError:
            return None
        return None if value is None else str(value)

    async def _has_focus(self, field: ui.Element, point: tuple[float, float] | None = None,
                         tapped: tuple[float, float] | None = None) -> str | None:
        """Wait until the active element is the target; that element's id, or None when it never is. The keyboard
        can scroll the field after the tap, so the pre-tap rect says nothing: the element's identifier (else its
        label) is compared first when no other listed input has it, then its rect against where a fresh read of the
        tree shows the field now (under the tap, when another input has its key too). Without this, keys would land in
        the old field. An element that goes stale as the field takes focus (React Native redraws it) is "not yet";
        raised (_FieldGone) only when the wait ends on it."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + FOCUS_WAIT
        while True:
            gone = False
            element = await self._active_element()
            try:
                if element and await self._is_field(element, field, point=point, tapped=tapped):
                    return element
            except _FieldGone:
                gone = True
            if loop.time() >= deadline:
                if gone:
                    raise _FieldGone()
                return None
            await asyncio.sleep(FOCUS_POLL)

    def _unique(self, field: ui.Element) -> bool:
        """No other input in the list has this field's identifier (else its label): those alone can tell it."""
        return bool(field.identifier or field.label) and sum(1 for e in self.items if _alike(e, field)) == 1

    async def _is_field(self, element: str, field: ui.Element, strict: bool = False,
                        point: tuple[float, float] | None = None, tapped: tuple[float, float] | None = None,
                        listed: tuple[float, float, float, float] | None = None) -> bool:
        """Is WebDriverAgent's element the listed field? Its identifier (else its label) proves it only where no other
        listed input has it, and never `strict` (right before a clear) or for a point target. Otherwise the field is
        found in a fresh read, as the one input there: under the agent's `point` when there is one; else, when its
        key isn't the only one, under the focusing tap (`tapped`) or where it was `listed` (no tap was made; strict
        too); and the element's rect must be that input's (XCTest's own frame against XCTest's own frame for content
        another process draws). Raises _FieldGone when the element went stale meanwhile (1.7.8, Opus review H1, H2, 6)."""
        unique = self._unique(field)
        wanted = field.identifier or field.label
        if wanted and unique and not strict and point is None:
            got = await self._attribute(element, "name") if field.identifier else None
            if got is None and not field.identifier:
                got = await self._attribute(element, "label")
            if got == wanted:
                return True
        at = point if point is not None else None if unique else tapped
        place = None if at is not None else listed if (strict or not unique) else None
        if not unique and at is None and place is None:
            return False  # another input has its key, and nothing says where this one is
        seen: list[ui.Element] = []
        now = await self._field_now(field, at, place, seen)
        if now is None and point is None and tapped is not None and at == tapped:
            # nothing under the tap: the keyboard lifted the field (a chat composer with no label). With one input on
            # the screen before and after, the focus can have gone nowhere else (last Opus review, M1)
            now = _sole_input(field, self.items, seen, strict)
        if now is None:
            return False
        if (strict and at is None and place is None and tapped is not None
                and not ui._holds(now.rect, tapped) and not _moved_with_content(field, now, self.items, seen)
                and _sole_input(field, self.items, seen, strict) is not now):
            # a clear after a focusing tap, the field's key the only one: the one input with it is the tapped one only
            # where it is still under the tap, or where the content around it moved with it (the keyboard scrolled the
            # form up). Another input with its key elsewhere isn't (CodeRabbit, PR #18)
            return False
        got_rect = await self._rect_of(element)
        if got_rect is None or not _near(got_rect, now.raw_rect or now.rect):
            return False
        if strict or at is not None or place is not None:
            self._placed_proof = element
        return True

    async def _rect_of(self, element: str) -> tuple[float, ...] | None:
        """XCTest's frame of WebDriverAgent's element; None when it won't say. Raises _FieldGone when the element went
        stale: the one WebDriverAgent named a moment ago is gone (the app redrew its view: X's composer opened its GIF
        picker, 2026-11-17). Was `internal` (1.7.8, bug 6); the caller words it."""
        try:
            box = (await self._call("GET", f"/session/{{sid}}/element/{element}/rect")).get("value") or {}
        except ConnectionLost:
            raise
        except DeviceError as e:
            if "stale" in str(e).lower():
                raise _FieldGone() from None
            return None
        return tuple(float(box.get(k, float("inf"))) for k in ("x", "y", "width", "height"))

    async def _where_listed(self, element: str, field: ui.Element) -> bool:
        """Is WebDriverAgent's element where the newest read lists the field (its x, y and width: a text view grows as
        it is typed into; XCTest's own frame for content another process draws)?"""
        got = await self._rect_of(element)
        return got is not None and _near(got[:3], (field.raw_rect or field.rect)[:3])

    async def _field_now(self, field: ui.Element, at: tuple[float, float] | None = None,
                         place: tuple[float, float, float, float] | None = None,
                         seen: list[ui.Element] | None = None) -> ui.Element | None:
        """The field as the tree shows it now: the one input with its identifier (else its label), or None. With `at`,
        the one input under that point; with `place`, the one input listed there (its x, y and width: a text view grows
        as it is typed into). Another input there too, or none with the field's key: None. `seen` gets the whole
        fresh list."""
        source = await self._source()
        if not isinstance(source, str):
            return None
        fresh = ui.read(source, self.screen).items
        if seen is not None:
            seen.extend(fresh)
        inputs = [e for e in fresh if e.role == field.role]
        if at is not None:
            inputs = [e for e in inputs if ui._holds(e.rect, at)]
        elif place is not None:
            inputs = [e for e in inputs if _near((e.x, e.y, e.width), place[:3])]
        same = [e for e in inputs if _alike(e, field)]
        if len(same) != 1 or ((at is not None or place is not None) and len(inputs) != 1):
            return None
        return same[0]

    async def swipe(self, x0: float, y0: float, x1: float, y1: float, duration_ms: int = 350, hold_ms: int = 0) -> str:
        self.typed_into = None
        await self._touching(duration_ms + hold_ms, (x0, y0))
        # a free swipe through the hands moves as a person's finger does (motion.swipe); one that rests at its end
        # (hold_ms) keeps the straight shape, as scroll drags keep theirs
        shaped = (motion.swipe(x0, y0, x1, y1, duration_ms, self.motion_rng)
                  if getattr(self, "hands", None) is not None and motion.HUMAN and not hold_ms else None)
        await self._drag(ui.swipe_points(x0, y0, x1, y1, duration_ms, hold_ms),
                         ui.swipe(x0, y0, x1, y1, duration_ms, hold_ms), shaped)
        self.last_point = (x0, y0)
        return f"swiped ({round(x0)}, {round(y0)}) -> ({round(x1)}, {round(y1)})"

    async def scroll(self, x0: float, y0: float, x1: float, y1: float) -> str:
        """A scroll drag that stops where the finger does (ui.scroll_drag)."""
        self.typed_into = None
        await self._touching(SCROLL_MS, (x0, y0))
        await self._drag(ui.scroll_drag_points(x0, y0, x1, y1), ui.scroll_drag(x0, y0, x1, y1))
        self.last_point = (x0, y0)
        return f"scrolled ({round(x0)}, {round(y0)}) -> ({round(x1)}, {round(y1)})"

    async def path(self, points: list) -> str:
        """Replay a finger path: [(t_ms from 0, x, y), ...] in points."""
        self.typed_into = None
        await self._touching(points[-1][0], (points[0][1], points[0][2]))
        await self._drag(ui.path_points(points), ui.path(points))
        self.last_point = (points[0][1], points[0][2])
        return f"dragged through {len(points)} points"

    async def press_button(self, name: str) -> str:
        """A hardware button: through the hands when they can be used (lock only there: XCTest can't press it), else
        the runner (home by its home-screen route, which waits for the home screen)."""
        self.typed_into = None
        hands, hand = getattr(self, "hands", None), HAND_BUTTONS.get(name)
        if hands is not None and hand is not None and await self._by_hands(
                "button", lambda before: hands.button(hand, before=before), input_=False):
            return f"pressed {name}"
        if name == "lock":
            raise Refused("unsupported", "lock needs CoreDevice HID, which this phone or Tapwright setup doesn't have "
                                         "(the runner can't press it); nothing was pressed.")
        if name == "home":  # pressButton "home" does nothing on iOS 26 (measured); this one waits for the home screen
            await self._call("POST", "/wda/homescreen", {})
        else:
            await self._call("POST", "/session/{sid}/wda/pressButton", {"name": name})
        return f"pressed {name}"

    async def rotate(self, to: str) -> str:
        """Turn the phone (XCUIDevice's orientation, no touch). The screen's new size comes with the next read."""
        await self._call("POST", "/session/{sid}/orientation", {"orientation": ORIENTATIONS[to]})
        # which side is up is asked again by the next read; until then no touch is mapped by the old side (R200 H1)
        self.turn = TURN_UNKNOWN
        self._turn_asked = -float("inf")
        return f"rotated to {to.replace('_', ' ')}"

    async def key(self, name: str) -> str:
        """backspace or enter, into whatever field has focus."""
        if name not in KEYS:
            raise Refused("bad_params", f"key must be one of {', '.join(KEYS)}, not {name!r}")
        self.typed_into = None  # a backspace moves the cursor's text; enter may move focus
        await self._touching(KEY_MS)
        await self._call("POST", "/session/{sid}/wda/keys", {"value": [KEYS[name]]})
        return f"pressed {name}"

    async def settings(self, values: dict) -> None:
        """WebDriverAgent settings, e.g. the MJPEG stream's frame rate and size."""
        await self._call("POST", "/session/{sid}/appium/settings", {"settings": values}, timeout=READ_CUT, again=True)

    async def locked(self) -> bool:
        return bool((await self._call("GET", "/session/{sid}/wda/locked", timeout=LOCK_WAIT)).get("value"))

    async def alert(self, action: str, *, name: str | None = None) -> str:
        """Read or answer the system alert, optionally selecting an exact localized button."""
        if action not in ("accept", "dismiss", "text", "cancel"):
            raise Refused("bad_params", f"action must be one of accept, dismiss, text, cancel, not {action!r}")
        if name is not None and (not isinstance(name, str) or not name.strip() or action == "text"):
            raise Refused("bad_params", "name must identify an alert button for accept, dismiss or cancel")
        if action != "text":
            self.typed_into = None
        try:
            if action == "text":
                return (await self._call("GET", "/session/{sid}/alert/text")).get("value") or "(no alert)"
            await self._touching(TAP_MS)
            chosen = "dismiss" if action == "cancel" else action
            button = (name or "Cancel") if action == "cancel" else name
            await self._call("POST", f"/session/{{sid}}/alert/{chosen}",
                             {"name": button} if button is not None else {}, again=False)
        except DeviceError as e:
            if "no such alert" in str(e).lower() and not isinstance(e, ConnectionLost):
                raise Refused("not_found", "no alert on screen.") from e
            raise
        # The runner acknowledges the button request, not the resulting screen state.
        named = f" for {ui.quoted(button)}" if button is not None else ""
        return f"Sent alert {action} request{named}. Check the screen to confirm the result."

    # ---- apps ----
    def warm_hands(self) -> "asyncio.Task | None":
        """Open CoreDevice HID behind the caller, so the first touch finds it open (Hands.warm). None when touches
        don't go through the hands here."""
        if self.hands is None or HANDS == "runner":
            return None
        task = asyncio.ensure_future(self.hands.warm())
        task.add_done_callback(lambda done: done.cancelled() or done.exception())  # (never a failed connect)
        return task

    async def apps(self, fresh: bool = False) -> list[dict]:
        """The apps you installed. Asking the phone takes about a second (2.0: 0.9-1.1 s on USB, measured), and
        open_app, close_app, a crash check and a header's name all need the list, so the last answer is kept and
        given at once. One older than APPS_STALE is read again behind the call that used it, for the next one.
        `fresh`: ask the phone now (what was looked for isn't in the kept list and may have been installed since)."""
        await self.ensure()
        kept = self._apps
        if fresh or kept is None:
            return await self._list_apps()
        if time.monotonic() - kept[0] > APPS_STALE and (self._apps_task is None or self._apps_task.done()):
            self._apps_task = asyncio.ensure_future(self._list_apps())
            self._apps_task.add_done_callback(lambda done: done.cancelled() or done.exception())  # (never fails a call)
        return kept[1]

    def _name_behind(self, bundle: str) -> None:
        """An Apple app the kept list doesn't hold is nearly always a built-in one, which the phone's list of
        installed apps never holds: the list is read again behind this answer instead of before it (asking takes
        about a second, and every first look at a built-in app paid it). One of Apple's installed since (Pages,
        TestFlight) has its name from the next answer on; it isn't asked for again either way."""
        if self._apps_task is not None and not self._apps_task.done():
            return  # a read is on its way, begun before this app showed: the next answer asks again
        self.not_installed.add(bundle)
        self._apps_task = asyncio.ensure_future(self._list_apps())
        self._apps_task.add_done_callback(lambda done: done.cancelled() or done.exception())  # (a nicety: never fails)

    async def _list_apps(self) -> list[dict]:
        self._apps_asked += 1
        mine = self._apps_asked
        apps = await self.device.user_apps()
        if mine >= self._apps_kept:  # (review K, question 6: a read begun earlier never replaces a later one's list)
            self._apps_kept = mine
            self._apps = (time.monotonic(), apps)
            self.app_names = {a["bundle_id"]: a["name"] for a in apps if a["name"]}
        return apps

    async def launch(self, bundle_id: str, arguments: list[str] | None = None) -> str:
        """Launch the app (or bring it to the front). `arguments` are launch arguments for the app, e.g. Apple's
        -AppleLanguages (launch.py); WebDriverAgent applies them only to a launch, so the caller closes a running app first."""
        self.typed_into = None
        body: dict = {"bundleId": bundle_id}
        if arguments:
            body["arguments"] = arguments
        await self._call("POST", "/session/{sid}/wda/apps/launch", body, timeout=60)
        return f"launched {bundle_id}"

    async def terminate(self, bundle_id: str) -> str:
        self.typed_into = None
        await self._call("POST", "/session/{sid}/wda/apps/terminate", {"bundleId": bundle_id})
        return f"closed {bundle_id}"

    async def open_url(self, url: str) -> str:
        self.typed_into = None
        await self._call("POST", "/session/{sid}/url", {"url": url})
        return f"opened {url}"

    async def close(self) -> None:
        if self.hands is not None:
            try:
                await asyncio.wait_for(self.hands.close(), 5)  # (before the tunnel goes: its services close cleanly)
            except Exception:
                pass
        await self.device.close()

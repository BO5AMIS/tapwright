"""What an agent does with the phone: look, tap, type, swipe, launch apps. One Session
holds the connection and WebDriverAgent's session, connecting on first use."""
import asyncio
import base64
import binascii
import contextvars
import io
import os
import random
import re
import secrets
import time
import urllib.parse
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
# (review R200v M4) the languages Vision's fast level reads (Latin script; Apple's fast revision lists English, French,
# Italian, German, Spanish and Portuguese), by their primary subtag. A fast read asked in any other language fails with
# `unknown error` (RUNNER-V9-PROTOCOL §4), so wait_for's polls read at the accurate level then.
FAST_LANGUAGES = frozenset({"en", "fr", "it", "de", "es", "pt"})


def fast_reads(languages: str) -> bool:
    """Can Vision's fast level read every one of these comma-separated BCP 47 codes ("" = its default, English)?"""
    return all(code.strip().split("-")[0].lower() in FAST_LANGUAGES for code in languages.split(",") if code.strip())


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except ValueError:
        return default
    return value if value > 0 else default


AX_TIMEOUT = _env_float("TAPWRIGHT_AX_TIMEOUT", 3.0)  # seconds: the runner's ceiling for any one accessibility read
TREE_BUDGET_MS = int(_env_float("TAPWRIGHT_TREE_BUDGET", 3000))  # ms a tree read may take on the runner
# ms for an app whose last read was cut short. (Review R200v H1) The runner gives `full` 60 % of the budget, after
# finding the app and an idle wait of up to a quarter of it, and tries `shallow` only with 0.25 s left past a reserve
# for `root`: at 600 ms nearly every read came back `root`, even of a screen that reads whole in 0.4 s (the home screen's
# tree took 0.4 s in Phase 0). 1500 ms leaves `full` about 0.9 s and room for `shallow`.
SHORT_BUDGET_MS = 1500
QUICK_CUT = 0.25  # a read that comes back with no elements in under this part of its budget wasn't kept waiting by the
#                   app: the app had nothing to answer with yet. It is asked again, RETRY_GAP seconds later, while at
#                   least RETRY_MIN_MS of the budget is left
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
UNASKED_WHY = ("not asked: {bundle} is still in front, its screen keeps moving, and its read {age:.1f} s ago gave no "
               "elements in time")


def tree_path(budget_ms: int, normal_ms: int, forced: str | None = None) -> str:
    """The runner's way of reading for a read with `budget_ms` (see TREE_PATH_FORCED)."""
    forced = TREE_PATH_FORCED if forced is None else forced
    if forced in ("direct", "xctest"):
        return forced
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
    every read of that screen cost the normal budget, and none was left unasked (lead, 2026-11-26)."""

    def __init__(self, normal_ms: int = TREE_BUDGET_MS, short_ms: int = SHORT_BUDGET_MS,
                 memory: float = BUDGET_MEMORY, clock: Callable[[], float] = time.monotonic,
                 unasked_for: float = UNASKED_FOR) -> None:
        self.normal_ms, self.short_ms, self.memory, self.clock = normal_ms, short_ms, memory, clock
        self.unasked_for = unasked_for
        self._cut: dict[str, float] = {}  # bundle id -> when a normal-budget read of it was last cut short
        self._still_cut: dict[str, float] = {}  # bundle id -> when a read its stillness gave the normal budget was
        self._by_still: str | None = None  # the app whose next read has the normal budget because its screen is still
        self._tried: dict[str, float] = {}  # bundle id -> when a real read of it last went out

    def plan(self, bundle: str | None, still: Callable[[], bool] | None = None) -> tuple[int, bool]:
        """(the budget in ms, whether the read may be left unasked) for a read of `bundle`. (2.0, worker H) Unasked only
        where the short budget applies (the app's memory of a cut read is fresh, and the screen isn't still) and a real
        read of the app went out within `unasked_for` seconds: the caller then asks the runner only which app is in front
        (route version 11), and makes the `root` answer itself when it is still this app."""
        budget = self.budget(bundle, still)
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

    def budget(self, bundle: str | None, still: Callable[[], bool] | None = None) -> int:
        """The budget, in ms, for a read of the app `bundle` (None: not known, the normal budget). `still` is asked only
        when the short budget would apply: True gives the normal one."""
        if bundle is None or bundle not in self._cut:
            return self.normal_ms
        if self.clock() - self._cut[bundle] >= self.memory:
            del self._cut[bundle]
            self._still_cut.pop(bundle, None)
            if still is not None and still():
                self._by_still = bundle  # (cut short again, this read is the still screen's one too)
            return self.normal_ms
        at = self._still_cut.get(bundle)
        if still is not None and (at is None or self.clock() - at >= self.memory) and still():
            self._by_still = bundle
            return self.normal_ms
        return self.short_ms

    def short(self, bundle: str | None) -> bool:
        return self.budget(bundle) < self.normal_ms

    def answered(self, bundle: str | None, truncated: bool, budget_ms: int, took_ms: float | None = None) -> None:
        """A read of `bundle` with `budget_ms` came back truncated or full, after `took_ms` (None: not timed)."""
        by_still, self._by_still = self._by_still, None
        if bundle is None:
            return
        if not truncated:
            if took_ms is None or took_ms >= self.short_ms:
                self._cut.pop(bundle, None)
                self._still_cut.pop(bundle, None)
        elif budget_ms >= self.normal_ms:
            self._cut[bundle] = self.clock()
            if by_still == bundle:
                self._still_cut[bundle] = self.clock()  # its still screen didn't answer either

    def forget(self) -> None:
        self._cut.clear()
        self._still_cut.clear()
        self._by_still = None
        self._tried.clear()


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

    def settled(self, bundle: str | None, capped: bool, cut: bool, seconds: float = 0.0) -> bool:
        """A settle of `bundle` ended after `seconds`: by its cap with frames still changing (`capped`), else on frames
        that stood still; the newest tree of the app cut short (`cut`). True when the app's cap switched (the caller
        logs it)."""
        if bundle is None:
            return False
        moving = capped or seconds > self.short  # still changing when the short cap would have ended it
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


def pinch_spread(x: float, y: float, scale: float, screen: tuple[float, float]) -> tuple[float, float]:
    """(start, end) distance of each of a pinch's two fingers from (x, y), along the horizontal line through it: from
    PINCH_GAP / 2 out to `scale` times that (scale > 1), or the other way (scale < 1), kept PINCH_MARGIN inside the
    screen (so a pinch near an edge spreads less than asked)."""
    room = max(1.0, min(x, screen[0] - x) - PINCH_MARGIN)
    narrow = min(PINCH_GAP / 2, room)
    wide = min(narrow * max(scale, 1 / scale), room)
    return (narrow, wide) if scale >= 1 else (wide, narrow)


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
        self.motion_rng = random.Random()
        # (2.0, route version 9) how the newest tree read came back: "full", "shallow" (part of the tree, the runner's
        # budget ran out) or "root" (the application node alone: the screen doesn't answer accessibility reads); and
        # the runner's short reason for a cut. A runner older than 9 always reads the whole tree: "full".
        self.last_tier = "full"
        self.last_why = ""
        self.budgets = ReadBudget()
        # Set by the daemon: does the stream show the screen still right now (Daemon._proven_still)? A still screen's
        # read gets the normal budget even while its app is on the short one (ReadBudget). None: never taken as still.
        self.screen_still: Callable[[], bool] | None = None
        self.budget_app: str | None = None  # the app the newest bounded read was budgeted for or named (L2)
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
        self.scale = self.app = self.read_app = None
        self.scale_read = False
        self.items, self.offscreen, self.behind = [], [], []
        self.typed_into = None
        self.last_point = None
        self.last_compact = None
        self.last_source = None

    async def _call(self, method: str, path: str, payload: dict | None = None, timeout: float = 30,
                    again: bool | None = None) -> dict:
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
        if same and self.turn != TURN_UNKNOWN and time.monotonic() - self._turn_asked < TURN_RECHECK:
            return  # (in landscape the phone may have turned to the other side: asked again now and then)
        self.screen = (w, h)
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
            source = await self._read_tree(timeout)
            await self._follow_orientation(source)
            waited = 0.0
            while await self._banner_over_an_app(source) and waited < min(BANNER_WAIT, timeout):
                await asyncio.sleep(BANNER_POLL)
                waited += BANNER_POLL
                source = await self._read_tree(timeout)
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
        return (await self._call("GET", ui.SOURCE_PATH, timeout=min(timeout, READ_CUT))).get("value")

    async def _bounded_tree(self, timeout: float) -> str | None:
        """(route version 9) The compact read with a budget: the app's own (ReadBudget), by the app the newest read
        found in front. A read cut short on the short budget whose answer names another app than the one the budget
        was for is read again with the normal budget: the short budget was that app's, not this one's."""
        # (review R200v L2) a root the runner couldn't name an app for (RUNNER-V9-PROTOCOL §1: `b` left out) is kept
        # under the app the read was budgeted for, so the short budget still applies to it; and such a root is never
        # taken for another app's
        bundle = (self.read_app[1] if isinstance(self.read_app, tuple) else None) or self.budget_app
        normal = self.budgets.normal_ms
        budget, unasked = self.budgets.plan(bundle, still=self.screen_still)
        if unasked and self.tw_version >= FRONT_VERSION and bundle is not None:
            source = await self._unasked_root(bundle)
            if source is not None:
                return source
        self.budgets.tried(bundle)
        source = await self._compact_tree(timeout, budget_ms=budget, budget_for=bundle,
                                          read_path=tree_path(budget, normal))
        named = _root_bundle(source)
        if (source is not None and self.last_tier != "full" and budget < normal
                and named is not None and named != bundle):
            self.budgets.tried(named)
            source = await self._compact_tree(timeout, budget_ms=normal, budget_for=named,
                                              read_path=tree_path(normal, normal))
            named = _root_bundle(source)
        if source is not None:
            self.budget_app = named or bundle
        return source

    async def _unasked_root(self, bundle: str) -> str | None:
        """(2.0, worker H) The `root` answer a read of `bundle` would get, made here without asking the runner for the
        tree (ReadBudget.plan said it may be left unasked): only when the runner's FRONT_PATH says that app is still in
        front, with its pid. The XML is the one a runner's `root` answer becomes (RUNNER-V9-PROTOCOL §1: the
        application node, the screen in points). None (read as before) when another app is in front or it can't say."""
        front = await self.front_app()
        if front is None or front[0] != bundle:
            return None
        node: dict = {"t": "Application", "r": [0, 0, self.screen[0], self.screen[1]], "b": bundle}
        if front[1] is not None:
            node["pid"] = front[1]
        label = self.read_app[0] if isinstance(self.read_app, tuple) and self.read_app[1] == bundle else \
            self.app_names.get(bundle)
        if label and label != bundle:
            node["l"] = label
        source = self._made_root = ui.xml_of_tree(node)
        age = self.budgets.tried_ago(bundle) or 0.0
        self.last_tier, self.last_why = "root", UNASKED_WHY.format(bundle=bundle, age=age)
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
        try:
            answer = await self._call("GET", FRONT_PATH, timeout=FRONT_WAIT)
        except ConnectionLost:
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
        # with the standard set; Settings root 168 / 315 / 184; home screen 206 / 416 / 273), gives the same nodes (the
        # same token) and keeps to its budget. So with a version 10 runner every budgeted read goes that way.
        lean = (budget_ms is not None and self.tw_version >= 10 and TREE_ATTRS != "std"
                and TREE_PATH_FORCED not in ("direct", "xctest"))
        if lean:
            read_path = "direct"
        query = [*([f"budget={budget_ms}"] if budget_ms is not None else []),
                 *([f"path={read_path}"] if budget_ms is not None and read_path else []),
                 *(["attrs=lean"] if lean else []),
                 *([f"since={last[0]}"] if last else [])]
        path = TREE_PATH + ("?" + "&".join(query) if query else "")
        began = time.monotonic()
        try:
            value = (await self._call("GET", path, timeout=min(timeout, READ_CUT))).get("value")
        except ConnectionLost:
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
            if remember:
                self.budgets.answered(_root_bundle(source) or budget_for, tier != "full", budget_asked or budget_ms,
                                      took_ms)
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
        the agent's touches), then tell Tapline the touch is the agent's."""
        self.input_point = at
        if self.banner_guard and at is not None:
            await self._clear_of_banner(at)
            self.top_touch = self.top_touch or at[1] < BANNER_ZONE
        if self.touch_hook is not None:
            await self.touch_hook(ms)
        trace.mark("guard")

    async def _clear_of_banner(self, at: tuple[float, float]) -> None:
        """A touch near the top of the screen while a notification banner is there lands on the banner: a tap opens
        the notification's app, a drag down opens the notification out (seen on the phone 2026-11-10: a scroll to
        a list's start pulled a Mail banner open). While a banner shows, WebDriverAgent names SpringBoard as the
        app in front though the phone's process list still has the app: then this waits for it to leave, and
        refuses the touch if it doesn't. Touches lower down, and SpringBoard's own screens with no app in front
        (the home screen), are not held up."""
        if at[1] >= BANNER_ZONE:
            return
        waited = 0.0
        while await self.foreground_bundle_id() == SPRINGBOARD:
            try:
                in_front = await asyncio.wait_for(self.device.foreground_apps(), 5)
            except Exception:
                return  # the phone can't say: WebDriverAgent's answer alone is no reason to refuse
            if not in_front:
                return  # the home screen itself: nothing lies over an app
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
            guard = getattr(self, "execution_guard", None)
            if guard is not None:
                await guard()
            if validate is not None:
                await validate(self.input_point)
            pending.append(None if QUIET.get() else trace.request("POST", f"{HANDS_PATH}/{what}"))

        began = time.monotonic()
        try:
            await act(before)
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
        began = time.monotonic()
        try:
            # (not asked twice when it gets no answer: a stalled check is skipped, Unanswered, not waited on again)
            answer = await self._call("GET", f"{HIT_PATH}?x={x:.2f}&y={y:.2f}", timeout=HIT_WAIT, again=False)
        except ConnectionLost:
            raise
        except Exception as e:  # noqa: BLE001 - whatever stopped the check, the touch goes unchecked
            if isinstance(e, DeviceError) and HIT_IDENTITY_REFUSED in str(e):
                raise unproven("identity_unproven") from None
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
            return
        value = answer.get("value") if isinstance(answer, dict) else None
        version = value.get("tapwright") if isinstance(value, dict) else None
        if not isinstance(version, int) or isinstance(version, bool) or version < 0:
            if strict:
                raise DeviceError("tapwright runner: capability_unproven")
            return  # do not cache a malformed response as proof of a legacy runner
        self.fast_tap, self.fast_drag = version >= 1, version >= 2
        self.tw_version = version
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
        focused = (await self._call("GET", "/session/{sid}/element/active")).get("value") or {}
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
        apps = await self.device.user_apps()
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

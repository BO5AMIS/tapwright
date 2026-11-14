"""Notification Center, read (AGENT-TOOLS §24.2, §20.4, §22.1): the `notifications` tool, one-time codes and the
notification trigger all read it through `parse` here.

Notification Center shows EVERY app's notifications, and only some of them may ever leave the process: the given apps'
(personal mode, `--apps`), the whole phone's (`--dedicated`, `--phone`), a one-time code setting's source apps, or a
notification trigger's app. So `parse` takes a `keep` test and drops every other app's notification while it walks the
tree: what it returns holds nothing else, and the tree's text is never kept, logged or traced (the daemon reads it with
the session's raw read and lets it go at once).

The tree's shape, as read on the phone (iOS 26, 2026-11-14, from a capture with every text replaced). SpringBoard is the
root (`bundleId="com.apple.springboard"`). Each notification is a `NotificationShortLookView` whose label is
`<app>[, <more>], <time>, <title>, <body>[, <more>]`; inside it, the button `ShortLook.Platter.Content.Seamless` carries
the same label from the time on, the static text `NotificationTitle` the title, and the static texts `TextContent.*` the
body. So the part of the label before the button's label is the app (its first piece: a stack adds a piece after the
name), the button's first piece is the time (`now`, `12 minutes ago`, `1 hour ago`, `5m ago`, `09:41`, `Yesterday`), and
the title and body are their own texts (they may hold ", "). A notification without that button (a banner over an app,
seen 2026-11-10 as `Mail, now`) is read from its static texts in order, else its label split at ", ". Notification
Center may write the app's name in another case than its home screen name, so names are compared ignoring case. The app
must be the shown name of exactly ONE app (the installed apps' names, and iOS's own apps below): a notification whose app
names no app, or two, is dropped, since it can't be proven to be a kept app's.
"""
from __future__ import annotations

import dataclasses
import datetime
import hashlib
import hmac
import re
from collections.abc import Callable, Mapping
from xml.etree import ElementTree as ET

MAX = 20  # §24.2: at most 20, newest first
NOTIFICATION_IDS = ("NotificationShortLookView", "NotificationCell", "NotificationListCell", "NotificationContentView")
TITLE_MAX = 80
BODY_MAX = 200
# iOS's own apps, by the name Notification Center shows (they aren't in the installed-apps list the daemon keeps)
SYSTEM_APPS = {
    "Messages": "com.apple.MobileSMS", "Mail": "com.apple.mobilemail", "Calendar": "com.apple.mobilecal",
    "Reminders": "com.apple.reminders", "Notes": "com.apple.mobilenotes", "Phone": "com.apple.mobilephone",
    "FaceTime": "com.apple.facetime", "Photos": "com.apple.mobileslideshow", "Safari": "com.apple.mobilesafari",
    "Settings": "com.apple.Preferences", "Wallet": "com.apple.Passbook", "App Store": "com.apple.AppStore",
    "TestFlight": "com.apple.TestFlight", "Find My": "com.apple.findmy", "Health": "com.apple.Health",
    "Home": "com.apple.Home", "Clock": "com.apple.mobiletimer", "Shortcuts": "com.apple.shortcuts",
    "Weather": "com.apple.weather", "Fitness": "com.apple.Fitness", "Files": "com.apple.DocumentsApp",
}
_AGO = re.compile(r"^(\d{1,3})\s*(m|mins?|minutes?|h|hrs?|hours?|d|days?|w|wks?|weeks?)\.?\s+ago$", re.I)
_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})(?:\s*([AP]M))?$", re.I)
_DAYS = ("Yesterday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
UNIT = {"m": 60, "h": 3600, "d": 86400, "w": 604800}  # by the unit's first letter


@dataclasses.dataclass(frozen=True)
class Notification:
    bundle: str
    app: str  # the app's home screen name (Notification Center may write it in capitals)
    when: str  # as it shows it: "now", "5m ago", "09:41", "Yesterday", or "" when it shows none
    age: tuple[float, float] | None  # (at least, at most) seconds old, when `when` says
    title: str = dataclasses.field(repr=False)
    body: str = dataclasses.field(repr=False)
    center: tuple[float, float]  # where a tap opens it, in points

    def key(self, secret: bytes) -> str:
        """What tells one notification from another for the trigger (§22.1), as a digest: never its text. An HMAC under
        the trigger's own random key (schedules.trigger_secret), not a plain hash: a plain hash of "Your code is 123456"
        is found again by trying the million codes (review 1.7, L5)."""
        if not secret:
            raise ValueError("a notification digest needs the trigger's key")
        text = "\0".join((self.bundle, self.title, self.body)).encode()
        return hmac.new(secret, text, hashlib.sha256).hexdigest()[:24]


def is_time(piece: str) -> bool:
    piece = piece.strip()
    return piece.lower() == "now" or bool(_AGO.match(piece) or _CLOCK.match(piece)) or piece in _DAYS


def age_of(piece: str, now: datetime.datetime | None = None) -> tuple[float, float] | None:
    """(at least, at most) seconds old, from Notification Center's time; None when it doesn't say (a date)."""
    piece = piece.strip()
    if piece.lower() == "now":
        return 0.0, 60.0
    if m := _AGO.match(piece):
        n, unit = int(m.group(1)), UNIT[m.group(2)[0].lower()]
        return float(n * unit), float((n + 1) * unit)
    now = now or datetime.datetime.now()
    midnight = (now - now.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds()
    if m := _CLOCK.match(piece):
        hour, minute = int(m.group(1)) % (12 if m.group(3) else 24), int(m.group(2))
        if m.group(3) and m.group(3).upper() == "PM":
            hour += 12
        seconds = midnight - (hour * 3600 + minute * 60)
        if seconds < 0:
            return None
        return max(0.0, seconds - 60), seconds + 60
    if piece == "Yesterday":
        return midnight, midnight + 86400
    return None


def _rect(node: ET.Element) -> tuple[float, float, float, float]:
    try:
        return tuple(float(node.get(k) or 0) for k in ("x", "y", "width", "height"))  # type: ignore[return-value]
    except ValueError:
        return 0.0, 0.0, 0.0, 0.0


def _is_notification(node: ET.Element) -> bool:
    name = node.get("name") or ""
    return name in NOTIFICATION_IDS


SEAMLESS = "ShortLook.Platter.Content.Seamless"


def _named(node: ET.Element, name: str) -> ET.Element | None:
    return next((n for n in node.iter() if n is not node and n.get("name") == name), None)


def _said(node: ET.Element) -> str:
    return (node.get("label") or node.get("value") or "").strip()


def _parts(node: ET.Element) -> tuple[list[str], str, str, str] | None:
    """(the app's possible names, time, title, body) of a notification of iOS 26's shape (the module's docstring), or
    None when it hasn't that shape."""
    button = _named(node, SEAMLESS)
    label = (node.get("label") or "").strip()
    rest = (button.get("label") or "").strip() if button is not None else ""
    if not rest or not label.endswith(", " + rest):
        return None
    before = label[:-len(rest) - 2]
    when = rest.split(", ")[0].strip()
    if not is_time(when):
        when = ""
    title_node = _named(node, "NotificationTitle")
    title = _said(title_node) if title_node is not None else ""
    body = ", ".join(t for t in (_said(n) for n in node.iter()
                                 if n is not node and (n.get("name") or "").startswith("TextContent")) if t)
    return [before, before.split(", ")[0].strip()], when, title, body


def _pieces(node: ET.Element) -> list[str]:
    """The notification's words in order: its static texts, else its label split at ", "."""
    texts: list[str] = []
    for inner in node.iter():
        if inner is node or not (inner.get("type") or inner.tag).endswith("StaticText"):
            continue
        said = (inner.get("label") or inner.get("value") or "").strip()
        if said and said not in texts:
            texts.append(said)
    if texts:
        return texts
    label = (node.get("label") or "").strip()
    return [p.strip() for p in label.split(", ")] if label else []


def _cut(text: str, most: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= most else text[:most - 1] + "…"


def names_to_bundles(installed: Mapping[str, str]) -> dict[str, str | None]:
    """{shown name, case folded: bundle id} of every app a notification may name; None for a name two apps share, in
    any case (it proves neither). `installed`: {bundle id: name} of the installed apps (the daemon's list)."""
    found: dict[str, str | None] = {}
    for bundle, name in [*((b, n) for n, b in SYSTEM_APPS.items()), *installed.items()]:
        name = (name or "").strip().casefold()
        if not name:
            continue
        if name in found and found[name] != bundle:
            found[name] = None
        else:
            found[name] = bundle
    return found


def parse(source: str, installed: Mapping[str, str], keep: Callable[[str], bool],
          now: datetime.datetime | None = None) -> list[Notification]:
    """The notifications in Notification Center's tree (SpringBoard's), of the apps `keep` says yes to ONLY, newest
    first, at most MAX. Every other app's notification, and one whose app can't be told, is dropped here, before
    anything else sees it. A tree that isn't XML is no notifications."""
    try:
        root = ET.fromstring(source)
    except ET.ParseError:
        return []
    names = names_to_bundles(installed)
    shown = {**{b: n for n, b in SYSTEM_APPS.items()}, **{b: n for b, n in installed.items() if n}}  # (its home screen name)
    found: list[tuple[int, Notification]] = []

    def walk(node: ET.Element) -> None:
        for child in node:
            if _is_notification(child):
                parts = _parts(child)
                if parts is not None:
                    tried, when, title, body = parts
                    app = next((a for a in tried if a and names.get(a.casefold()) is not None), tried[-1])
                else:
                    pieces = _pieces(child)
                    app, rest = (pieces[0], pieces[1:]) if pieces else ("", [])
                    when = next((p for p in rest if is_time(p)), "")
                    if when:
                        rest.remove(when)
                    title, body = (rest[0] if rest else ""), ", ".join(rest[1:])
                bundle = names.get(app.casefold()) if app else None
                if bundle is not None and keep(bundle):
                    x, y, w, h = _rect(child)
                    found.append((len(found), Notification(
                        bundle, shown.get(bundle) or app, when, age_of(when, now) if when else None,
                        _cut(title, TITLE_MAX), _cut(body, BODY_MAX), (x + w / 2, y + h / 2))))
                continue  # (a notification's own parts are never a second notification)
            walk(child)
    walk(root)
    found.sort(key=lambda p: (p[1].age is None, p[1].age[0] if p[1].age else 0.0, p[0]))
    return [n for _, n in found[:MAX]]


OPEN_ACTION = "swipe-action-button-identifier"


def open_action(source: str, y: float, width: float) -> tuple[float, float] | None:
    """Where to touch the Open action iOS 26 shows on a notification's row after a first tap on it (seen on the phone
    2026-11-14: the tap doesn't open the app; it shows the row's actions, Open on the left). Told by its identifier and
    its place (the left half, on the row the tap was on), never by its words, which follow the phone's language. None
    when there is none (the tree isn't XML, or no such action is on that row)."""
    try:
        root = ET.fromstring(source)
    except ET.ParseError:
        return None
    for node in root.iter():
        if node.get("name") != OPEN_ACTION:
            continue
        x, top, w, h = _rect(node)
        if w > 0 and h > 0 and x + w / 2 < width / 2 and top <= y <= top + h:
            return x + w / 2, top + h / 2
    return None


def is_center(source: str) -> bool:
    """Is this SpringBoard's tree (what Notification Center is)?"""
    return 'bundleId="com.apple.springboard"' in source[:800]


# ---- one-time codes (§20.4, §24.3) ----------------------------------------------------------------------------------------
CODE = re.compile(r"(?<![\d])(\d{4,8})(?![\d])")
SPLIT_CODE = re.compile(r"(?<![\d])(\d{3})[ -](\d{3})(?![\d])")  # `123 456`, `123-456`
CODE_SHAPE = re.compile(r"^[A-Za-z0-9]{4,12}$")
WINDOW = 120.0  # §20.4: within 2 minutes after the app showed its code field


def candidates(text: str, pattern: "re.Pattern[str] | None" = None) -> list[str]:
    """The codes a text holds: the setting's pattern's (its first group, else the match), else every 4 to 8 digit
    number, else a `123 456` one. Distinct, in order."""
    found: list[str] = []
    if pattern is not None:
        for m in pattern.finditer(text):
            code = m.group(1) if m.groups() and m.group(1) is not None else m.group(0)
            if CODE_SHAPE.match(code) and code not in found:
                found.append(code)
        return found
    for m in CODE.finditer(text):
        if m.group(1) not in found:
            found.append(m.group(1))
    if not found:
        for m in SPLIT_CODE.finditer(text):
            if m.group(1) + m.group(2) not in found:
                found.append(m.group(1) + m.group(2))
    return found


def matches(n: Notification, match: str) -> bool:
    """The setting's text match, ignoring case, in the title (often the sender) or the body."""
    return match.casefold() in f"{n.title}\n{n.body}".casefold()


def find_code(items: list[Notification], sources: tuple[str, ...], match: str, pattern: "re.Pattern[str] | None",
              field_at: float, now: float, used: Callable[[str], bool]) -> tuple[str, str] | None:
    """(code, source bundle) of the newest notification from a source app that matches, holds exactly one code that
    wasn't handed over before, arrived within WINDOW seconds and not before the app showed its code field (`field_at`,
    wall clock; to the minute, as Notification Center tells the time). None when there is none, or the field showed
    more than WINDOW seconds ago."""
    if now - field_at > WINDOW:
        return None
    for n in items:  # newest first
        if n.bundle not in sources or n.age is None or n.age[0] > WINDOW:
            continue
        if now - n.age[0] < field_at - 1.0:  # the latest it can have arrived is before the field showed
            continue
        if not matches(n, match):
            continue
        # the body says the code; the title is the sender, often a phone number (`+1 555 0100` holds `0100`)
        codes = [c for c in candidates(n.body or n.title, pattern) if not used(c)]
        if len(codes) == 1:
            return codes[0], n.bundle
    return None


def code_like_field(element: object) -> bool:
    """Does this input look like a one-time code field (its id, label, value or placeholder says code, OTP,
    verification, one-time, 2FA, PIN)? What tells the daemon when the app showed its code field."""
    words = " ".join(str(getattr(element, k, "") or "") for k in ("identifier", "label", "value", "placeholder"))
    return bool(CODE_FIELD.search(words))


ONE_LINE_INPUTS = ("TextField", "SecureTextField")  # XCUIElementTypes of a one-line input (a TextView is many lines)


def otp_target(element: object) -> bool:
    """May `${otp.<app>}` be typed into this field (§20.4: "a code field of the app"; review 1.7, H3)? Only a one-line
    input that says it takes a code by its id, label or placeholder (as code_like_field reads them). Its value counts
    only while it is the placeholder: text typed into a chat's field ("code") never makes it a code field."""
    if getattr(element, "role", "") != "input" or getattr(element, "type", "") not in ONE_LINE_INPUTS:
        return False
    keys = ["identifier", "name", "label", "placeholder"]
    if getattr(element, "shows_placeholder", False):
        keys.append("value")
    words = " ".join(str(getattr(element, k, "") or "") for k in keys)
    return bool(CODE_FIELD.search(words))


CODE_FIELD = re.compile(r"(?i)(\bcode\b|\botp\b|one[- ]?time|verif|\b2fa\b|\bpin\b|passcode|security code|"
                        r"oneTimeCode|otp[-_]|[-_]otp|code[-_]|[-_]code)")

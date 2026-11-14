"""Notification Center, read (AGENT-TOOLS §24.2, §20.4, §22.1): the `notifications` tool, one-time codes and the
notification trigger all read it through `parse` here.

Notification Center shows EVERY app's notifications, and only some of them may ever leave the process: the given apps'
(personal mode, `--apps`), the whole phone's (`--dedicated`, `--phone`), a one-time code setting's source apps, or a
notification trigger's app. So `parse` takes a `keep` test and drops every other app's notification while it walks the
tree: what it returns holds nothing else, and the tree's text is never kept, logged or traced (the daemon reads it with
the session's raw read and lets it go at once).

The tree's shape (ASSUMED: tests/data holds no Notification Center capture; check it on the phone). SpringBoard is the
root (`bundleId="com.apple.springboard"`). Each notification is an element whose identifier is one of NOTIFICATION_IDS
(a banner over an app is `NotificationShortLookView`, seen on the phone 2026-11-10 with the label `Mail, now`). Its
words are its static texts in order when it has any, else its label split at ", ": the app's shown name first, then the
time (`now`, `5m ago`, `2h ago`, `09:41`, `Yesterday`), then the title, then the body. The app is the FIRST piece only,
and only when it is exactly the shown name of one app (the installed apps' names, and iOS's own apps below): a
notification whose first piece names no app, or two, is dropped, since it can't be proven to be a kept app's.
"""
from __future__ import annotations

import dataclasses
import datetime
import hashlib
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
_AGO = re.compile(r"^(\d{1,3})\s*(m|min|h|hr|d)\.?\s+ago$", re.I)
_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})(?:\s*([AP]M))?$", re.I)
_DAYS = ("Yesterday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
UNIT = {"m": 60, "min": 60, "h": 3600, "hr": 3600, "d": 86400}


@dataclasses.dataclass(frozen=True)
class Notification:
    bundle: str
    app: str  # the name Notification Center shows
    when: str  # as it shows it: "now", "5m ago", "09:41", "Yesterday", or "" when it shows none
    age: tuple[float, float] | None  # (at least, at most) seconds old, when `when` says
    title: str = dataclasses.field(repr=False)
    body: str = dataclasses.field(repr=False)
    center: tuple[float, float]  # where a tap opens it, in points

    def key(self) -> str:
        """What tells one notification from another for the trigger (§22.1), as a digest: never its text."""
        return hashlib.sha256("\0".join((self.bundle, self.title, self.body)).encode()).hexdigest()[:24]


def is_time(piece: str) -> bool:
    piece = piece.strip()
    return piece.lower() == "now" or bool(_AGO.match(piece) or _CLOCK.match(piece)) or piece in _DAYS


def age_of(piece: str, now: datetime.datetime | None = None) -> tuple[float, float] | None:
    """(at least, at most) seconds old, from Notification Center's time; None when it doesn't say (a date)."""
    piece = piece.strip()
    if piece.lower() == "now":
        return 0.0, 60.0
    if m := _AGO.match(piece):
        n, unit = int(m.group(1)), UNIT[m.group(2).lower()]
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
    """{shown name: bundle id} of every app a notification may name; None for a name two apps share (it proves
    neither). `installed`: {bundle id: name} of the installed apps (the daemon's list)."""
    found: dict[str, str | None] = {}
    for bundle, name in [*((b, n) for n, b in SYSTEM_APPS.items()), *installed.items()]:
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
    found: list[tuple[int, Notification]] = []

    def walk(node: ET.Element) -> None:
        for child in node:
            if _is_notification(child):
                pieces = _pieces(child)
                bundle = names.get(pieces[0]) if pieces else None
                if bundle is not None and keep(bundle):
                    rest = pieces[1:]
                    when = next((p for p in rest if is_time(p)), "")
                    if when:
                        rest.remove(when)
                    x, y, w, h = _rect(child)
                    found.append((len(found), Notification(
                        bundle, pieces[0], when, age_of(when, now) if when else None,
                        _cut(rest[0], TITLE_MAX) if rest else "", _cut(", ".join(rest[1:]), BODY_MAX),
                        (x + w / 2, y + h / 2))))
                continue  # (a notification's own parts are never a second notification)
            walk(child)
    walk(root)
    found.sort(key=lambda p: (p[1].age is None, p[1].age[0] if p[1].age else 0.0, p[0]))
    return [n for _, n in found[:MAX]]


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


CODE_FIELD = re.compile(r"(?i)(\bcode\b|\botp\b|one[- ]?time|verif|\b2fa\b|\bpin\b|passcode|security code|"
                        r"oneTimeCode|otp[-_]|[-_]otp|code[-_]|[-_]code)")

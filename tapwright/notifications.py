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
import unicodedata
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

    def same(self, other: "Notification") -> bool:
        """The same notification in another read of Notification Center: its app, title and body (its place may move)."""
        return (self.bundle, self.title, self.body) == (other.bundle, other.title, other.body)

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


def shown(text: str, most: int) -> str:
    """A title or body as an answer shows it: whitespace collapsed and cut at `most` (TITLE_MAX, BODY_MAX). Only for text
    already redacted: a cut before the redactor could leave part of a value it no longer finds (§20.3, "redact before any
    cut"; Codex, PR #12 cycle 1, H5)."""
    return _cut(text, most)


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
    names_shown = {**{b: n for n, b in SYSTEM_APPS.items()}, **{b: n for b, n in installed.items() if n}}  # (home screen name)
    found: list[tuple[int, Notification]] = []

    def walk(node: ET.Element) -> None:
        for child in node:
            if _is_notification(child):
                parts = _parts(child)
                if parts is not None:
                    tried, when, title, body = parts
                    app = next((a for a in tried if a and names.get(a.casefold()) is not None), tried[-1])
                else:
                    # the app only from the label's first piece (review 1.7 B-L1): a banner's static texts are its
                    # title and body, so a third party's notification titled "Messages" is never Messages' own
                    label = (child.get("label") or "").strip()
                    app = label.split(", ")[0].strip() if label else ""
                    pieces = _pieces(child)
                    rest = pieces[1:] if pieces and pieces[0].casefold() == app.casefold() else list(pieces)
                    when = next((p for p in rest if is_time(p)), "")
                    if when:
                        rest.remove(when)
                    title, body = (rest[0] if rest else ""), ", ".join(rest[1:])
                bundle = names.get(app.casefold()) if app else None
                if bundle is not None and keep(bundle):
                    x, y, w, h = _rect(child)
                    # the whole title and body: they are cut only as an answer shows them, after the redactor (H5)
                    found.append((len(found), Notification(
                        bundle, names_shown.get(bundle) or app, when, age_of(when, now) if when else None,
                        title, body, (x + w / 2, y + h / 2))))
                continue  # (a notification's own parts are never a second notification)
            walk(child)
    walk(root)
    found.sort(key=lambda p: (p[1].age is None, p[1].age[0] if p[1].age else 0.0, p[0]))
    return [n for _, n in found[:MAX]]


OPEN_ACTION = "swipe-action-button-identifier"
# The Open action's words in the languages we can name (review 1.7 B-M8), case folded. Any other words are Open only when
# the row has one action, on its left; words that clear, delete or change settings are never Open.
OPEN_WORDS = frozenset({"open", "\u00f6ffnen", "ouvrir", "abrir", "apri", "openen", "\u00f6ppna", "\u00e5bn",
                        "\u00e5pne", "avaa", "otw\u00f3rz"})
NOT_OPEN_WORDS = frozenset({"clear", "clear all", "delete", "remove", "options", "mute", "turn off", "dismiss",
                            "l\u00f6schen", "effacer", "borrar", "eliminar", "cancella", "wissen", "verwijderen"})


def open_action(source: str, y: float, width: float) -> tuple[float, float] | None:
    """Where to touch the Open action iOS 26 shows on a notification's row after a first tap on it (seen on the phone
    2026-11-14: the tap doesn't open the app; it shows the row's actions, Open on the left). Told by its identifier, its
    place (the left half, on the row the tap was on) AND its words: an Open word (OPEN_WORDS), or, in a language not
    listed, the row's only action. Words that clear or delete are never touched. None when there is none or it can't be
    told (the tree isn't XML, no such action on that row, or several with words that aren't Open's): nothing is touched
    rather than something else."""
    try:
        root = ET.fromstring(source)
    except ET.ParseError:
        return None
    row: list[tuple[str, tuple[float, float, float, float]]] = []
    for node in root.iter():
        if node.get("name") != OPEN_ACTION:
            continue
        x, top, w, h = _rect(node)
        if w > 0 and h > 0 and top <= y <= top + h:
            row.append((" ".join(_said(node).split()).casefold(), (x, top, w, h)))
    left = [(words, r) for words, r in row if r[0] + r[2] / 2 < width / 2]
    if len(left) != 1:
        return None
    words, (x, top, w, h) = left[0]
    if words in NOT_OPEN_WORDS or not (words in OPEN_WORDS or len(row) == 1):
        return None
    return x + w / 2, top + h / 2


def rows_at(source: str, point: tuple[float, float]) -> int:
    """How many notifications (of any app: only their frames are looked at, never their words) hold the point."""
    try:
        root = ET.fromstring(source)
    except ET.ParseError:
        return 0
    count = 0
    for node in root.iter():
        if _is_notification(node):
            x, y, w, h = _rect(node)
            if w > 0 and h > 0 and x <= point[0] < x + w and y <= point[1] < y + h:
                count += 1
    return count


CENTER_TITLE = "notification center"
LISTS = ("ScrollView", "CollectionView", "Table")


def _kind(node: ET.Element) -> str:
    return (node.get("type") or node.tag).removeprefix("XCUIElementType")


def is_center(source: str) -> bool:
    """Is Notification Center open in this tree, proven (review 1.7 B-M3)? SpringBoard's tree is not enough: the home
    screen and a banner are SpringBoard's too, and a swipe that was swallowed (Siri, Control Center, an alert) would read
    as "no notifications". Proof is SpringBoard at the root and either Notification Center's own title, or a
    notification inside a list (Notification Center's scroll view; a banner over an app is in none). On a doubt the
    callers say NOT_OPENED and read nothing."""
    if 'bundleId="com.apple.springboard"' not in source[:800]:
        return False
    try:
        root = ET.fromstring(source)
    except ET.ParseError:
        return False
    for node in root.iter():
        if " ".join((node.get("label") or node.get("name") or "").split()).casefold() == CENTER_TITLE:
            return True
    parents = {c: up for up in root.iter() for c in up}
    for node in root.iter():
        if _is_notification(node):
            up = parents.get(node)
            while up is not None:
                if _kind(up) in LISTS:
                    return True
                up = parents.get(up)
    return False


# ---- one-time codes (§20.4, §24.3) ----------------------------------------------------------------------------------------
CODE = re.compile(r"(?<![\d])(\d{4,8})(?![\d])")
# `123 456`, `123-456`, and the same with any digits Python's \d takes (Arabic-Indic `١٢٣ ٤٥٦`, full-width…) and any
# space or dash between them (no-break and thin spaces, U+2010–U+2015, the minus sign); never a line break
SPLIT_SEPARATORS = "[^\\S\\n\\r\\u2028\\u2029\\x85]|[-\\u2010-\\u2015\\u2212\\ufe58\\ufe63\\uff0d]"
SPLIT_CODE = re.compile(rf"(?<![\d])(\d{{3}})(?:{SPLIT_SEPARATORS})(\d{{3}})(?![\d])")


def ascii_digits(text: str) -> str:
    """Every Unicode decimal digit as its ASCII one (`١٢٣` is `123`); anything else as it is."""
    return "".join(str(unicodedata.decimal(c)) if c.isdecimal() and not c.isascii() else c for c in text)


def code_forms(code: str) -> list[str]:
    """Every way a code is written that candidates() takes it from (Codex, PR #12 cycles 1 and 2, H4, H6): the code as
    read, the same in ASCII digits, and for six digits `123 456` and `123-456` in both. What the redactor must know, not
    only the code typed. The exact text a notification showed it as is added by codes_to_hide (code_spans)."""
    forms = [code]
    for digits in dict.fromkeys((code, ascii_digits(code))):
        if digits not in forms:
            forms.append(digits)
        if len(digits) == 6 and digits.isdecimal():
            forms += [f"{digits[:3]} {digits[3:]}", f"{digits[:3]}-{digits[3:]}"]
    return list(dict.fromkeys(forms))


def code_spans(text: str) -> list[str]:
    """The exact text every split code in `text` is written as (`١٢٣ ٤٥٦`, `123 456`): the redactor hides what was
    shown, whatever digits and separator it used (Codex, PR #12 cycle 2, H6)."""
    return list(dict.fromkeys(m.group(0) for m in SPLIT_CODE.finditer(text)))
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


def codes_to_hide(n: Notification, pattern: "re.Pattern[str] | None" = None) -> list[str]:
    """Every code a matching notification may hold, for the redactor (Codex, PR #12 cycle 1, H4): what candidates() finds
    in the title, in the body and in both, and every `123 456` one as well (candidates() looks for those only when there
    is no plain number, and a sender's `+1 555 0100` is one). Wider than find_code's: hiding one more number costs
    nothing; showing a code is the leak."""
    found: list[str] = []
    for text in (n.title, n.body, f"{n.title}\n{n.body}"):
        for code in candidates(text, pattern):
            if code not in found:
                found.append(code)
    if pattern is None:
        for m in SPLIT_CODE.finditer(f"{n.title}\n{n.body}"):
            if m.group(1) + m.group(2) not in found:
                found.append(m.group(1) + m.group(2))
        for span in code_spans(f"{n.title}\n{n.body}"):  # (as shown: H6)
            if span not in found:
                found.append(span)
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

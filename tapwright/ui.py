"""The screen as an agent reads it, and the gestures it asks for. Pure functions over
WebDriverAgent's accessibility tree (`SOURCE_PATH`, XML), so they're testable without a phone.

An element line looks like:
    e12 button "Log in" id=login-button @24,610 354x48 [tap]
Positions and sizes are in points (the phone's coordinate space, what taps use).
Format and vocabulary: docs/AGENT-TOOLS.md §3.
"""
import json
import math
import re
from dataclasses import dataclass, field
from typing import NamedTuple
from xml.etree import ElementTree as ET

from .control import TAP_MS, Refused
from .device import DeviceError

# The tree read, measured in Task 0: 0.1-0.4 s where plain JSON took 0.5-9 s. It has no `visible`
# or focus attribute, so visibility comes from the rect and a focused input can't be known.
SOURCE_PATH = "/source?format=xml&excluded_attributes=visible,accessible"

# Element types worth listing even without a label (things an agent can act on).
INTERACTIVE = {"Button", "TextField", "SecureTextField", "TextView", "SearchField", "Switch", "Slider",
               "Cell", "Link", "Tab", "TabBar", "SegmentedControl", "PickerWheel", "Stepper", "Toggle",
               "MenuItem", "Image", "Icon", "Key"}
# Containers whose own label repeats their children's text: listed only with an identifier.
# Scroll containers are the exception: they are always listed, so `scroll` can name its area.
# An Alert is never listed; the header describes it and its buttons stay in the list.
CONTAINERS = {"Application", "Window", "Other", "Group", "ScrollView", "Table", "CollectionView",
              "NavigationBar", "Toolbar", "StatusBar", "Keyboard", "WebView", "Sheet", "Alert"}
MAX_ELEMENTS = 200
MAX_OFFSCREEN = 300  # elements kept from outside the screen, for an `off_screen` answer instead of `not_found`
TEXT_LIMIT = 200  # AGENT-TOOLS v1.5: at 80, a first-time agent spent a third of its calls reading one cut state line
# Views that clip their children: what is scrolled out of them is still in the tree, but not on screen.
CLIPPING = {"ScrollView", "Table", "CollectionView", "WebView"}
# Characters XML 1.0 forbids, raw or as references; WebDriverAgent sometimes sends them in labels.
_RAW_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_CONTROL_REFS = re.compile(r"&#(?:[xX]([0-9a-fA-F]+)|([0-9]+));")

# XCUIElementType (without the prefix) -> role, docs/AGENT-TOOLS.md §3.3. Anything else: the type lowercased.
# A StaticText with the Header trait is a `header`; a Button inside a TabBar is a `tab`.
ROLES = {"Button": "button", "Key": "button", "Link": "link", "StaticText": "text",
         "TextField": "input", "SecureTextField": "input", "SearchField": "input", "TextView": "input",
         "Switch": "switch", "Toggle": "switch", "Slider": "slider", "Image": "image", "Icon": "image",
         "Tab": "tab", "Cell": "cell", "ScrollView": "scroll", "Table": "scroll", "CollectionView": "scroll"}
# What an agent can do with an element, inferred from its role.
ROLE_ACTIONS = {"button": ("tap",), "link": ("tap",), "cell": ("tap",), "tab": ("tap",), "input": ("tap", "type"),
                "switch": ("toggle",), "scroll": ("scroll",)}

_BIDI_MARKS = dict.fromkeys(map(ord, "‎‏‪‫‬‭‮⁦⁧⁨⁩"))


def _text(v) -> str:
    """The text, without the invisible direction marks iOS puts around numbers ("‎42")."""
    return "" if v is None else str(v).translate(_BIDI_MARKS).strip()


def clip(text: str, limit: int = TEXT_LIMIT) -> str:
    """One line, at most `limit` characters, as a JSON string. A cut ends, inside the quotes, with how many
    characters are missing: `…(+57)` (AGENT-TOOLS v1.5)."""
    flat = " ".join(text.split())
    missing = len(flat) - limit
    return json.dumps(flat[:limit] + (f"…(+{missing})" if missing > 0 else ""), ensure_ascii=False)


@dataclass(frozen=True)
class Bar:
    """A tab bar, toolbar or navigation bar iOS draws over the content: its kind (the XCUIElementType
    without its prefix), its rect, and its element line (without a ref or actions) for messages."""
    kind: str
    rect: tuple[float, float, float, float]
    line: str


@dataclass
class Element:
    ref: str
    role: str
    label: str = ""
    identifier: str = ""  # testID in React Native, accessibilityIdentifier in UIKit
    value: str = ""
    rect: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)  # x, y, width, height in points
    enabled: bool = True
    selected: bool = False
    checked: bool | None = None  # a switch's state; None for everything else
    actions: tuple[str, ...] = ()
    type: str = ""  # the XCUIElementType without its prefix
    in_alert: bool = False  # inside an OS alert
    focused: bool = False  # the keyboard's target; this tree read can't tell, so always False
    name: str = ""  # WebDriverAgent's raw name: what `id` matches, even when it only mirrors the label
    secure: bool = False  # a password field: its value is never shown
    covered_by: tuple[Bar, ...] = ()  # bars over this element's centre that aren't its ancestors
    placeholder: str = ""  # an input's placeholder: its value while it's empty, as WebDriverAgent reports it

    @property
    def x(self) -> float:
        return self.rect[0]

    @property
    def y(self) -> float:
        return self.rect[1]

    @property
    def width(self) -> float:
        return self.rect[2]

    @property
    def height(self) -> float:
        return self.rect[3]

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.width / 2, self.y + self.height / 2

    @property
    def text(self) -> str:
        """What the screen shows for it: the label, else the value."""
        return self.label or self.value

    def line(self, ref: bool = True, actions: bool = True) -> str:
        """`<ref> <role> "<text>" [id=…] [value="…"] [disabled] [selected] [checked=…] @x,y WxH [actions]`.
        `ref=False` leaves the ref out (a gone element has no ref any more); `actions=False` leaves
        out the `[actions]`. The label is the quoted text, so there is no separate `label=`."""
        parts = [self.ref] if ref else []
        parts.append(self.role)
        if self.text:
            parts.append(clip(self.text))
        if self.identifier:
            parts.append(f"id={self.identifier}")
        # a switch's value ("1", "0") is already `checked=`
        if self.value and self.value != self.text and self.checked is None:
            parts.append(f"value={clip(self.value)}")
        if not self.enabled:
            parts.append("disabled")
        if self.selected:
            parts.append("selected")
        if self.checked is not None:
            parts.append(f"checked={'true' if self.checked else 'false'}")
        parts.append(f"@{round(self.x)},{round(self.y)} {round(self.width)}x{round(self.height)}")
        if actions and self.actions:
            parts.append(f"[{', '.join(self.actions)}]")
        return " ".join(parts)


@dataclass(frozen=True)
class Alert:
    """An OS alert on screen: its title, its button labels, and where it is."""
    title: str
    buttons: tuple[str, ...]
    rect: tuple[float, float, float, float]


@dataclass
class Tree:
    """One read of the accessibility tree."""
    items: list[Element]
    more: int = 0  # elements past MAX_ELEMENTS, not listed
    alert: Alert | None = None
    keyboard: bool = False
    top_inset: float | None = None  # where the bars along the top edge end (y), None when there are none
    bottom_inset: float | None = None  # how far the bars along the bottom edge reach up from it
    app: tuple[str, str] | None = None  # (label, bundle id) from the root Application, when it says
    # what would be listed but lies outside the screen or its scroll view (a target naming one is off_screen)
    offscreen: list[Element] = field(default_factory=list)
    # how many elements lie wholly above and wholly below the screen, for the header (AGENT-TOOLS v1.5): every
    # one, not only the first MAX_OFFSCREEN, with duplicates folded as in the list; none under an open sheet
    above: int = 0
    below: int = 0
    # what an open sheet or other layer hides, each with the line of what is in front of it (a target naming
    # one is covered); what is off screen under a presented sheet is here too
    behind: list[tuple[Element, Element | None]] = field(default_factory=list)  # (hidden, what is in front)


BARS = {"NavigationBar", "StatusBar", "Toolbar", "TabBar"}  # what iOS draws over the content's edges
COVERING_BARS = {"NavigationBar", "Toolbar", "TabBar", "Keyboard"}  # what a touch at an element's centre can land on instead
# A layer is a later child of some node, at most LAYER_MAX_DEPTH levels below the Application (deeper is React
# Native's own nesting, where a header and a body both fill the screen), covering LAYER_COVER of the screen.
# It hides what is under it in its earlier siblings by one of two paths:
#   presentation: it has a backdrop before its other listed content (an empty Button or Other at its top-left,
#     BACKDROP_WIDTH of the screen's width and BACKDROP_HEIGHT of its height: UIKit's dimming view, an RN Modal's
#     backdrop) and something with actions in it: what is inside its rect is hidden;
#   opaque content: no backdrop, and what it lists fills LAYER_FILL of the screen: what is inside those
#     listed rects is hidden.
LAYER_MAX_DEPTH = 6
LAYER_COVER = 0.95
LAYER_FILL = 0.5
BACKDROP_WIDTH = 0.95
BACKDROP_HEIGHT = 0.4
# A label or picture drawn over a button (React Native) lets the touch through, so it isn't a cover. Known limit:
# XCUITest can't see gesture recognisers, so a text or image with a tap handler isn't detected as a cover either.
PASSIVE = {"text", "header", "image"}
OVERLAY_MAX = 0.95  # a later element covers an earlier one like a bar, unless it takes this share of the screen (a layer)
WRAPPER = 0.9  # a layer's listed descendant this big, and not a scroll view, is a transparent wrapper: no content
# A scroll drags fast, then eases over its last stretch and rests before the finger lifts, so the content stops
# where the finger does instead of flinging: a drifting list would make the next tap land on the wrong row. iOS
# judges a flick by the finger's speed over that last stretch: ending at 330 pt/s or slower stopped in place every
# time, 440 pt/s flung, whatever the speed before. A plain 600 ms drag with a 150 ms rest slid 50-130 pt on after
# 410 pt and flung off the list after 655 pt (lab, iPhone 14 Pro Max, iOS 26.5, 2026-11-10).
SCROLL_FAST_MS = 300  # the drag up to its last stretch
SCROLL_EASE_MS = 150  # the last stretch
SCROLL_EASE_PT = 37.5  # at most this long (250 pt/s over SCROLL_EASE_MS)
SCROLL_EASE_SHARE = 0.15  # and at most this share of the drag
SCROLL_HOLD_MS = 80  # the rest before the finger lifts
BAR_EDGE = 0.15  # a bar counts as a top or bottom bar when it starts in the top, or ends in the bottom, 15% of the screen


def _rect(node: ET.Element) -> tuple[float, float, float, float]:
    """The node's frame; anything unreadable (not a number, inf, nan) makes it zero-size."""
    try:
        rect = tuple(float(node.get(key, 0)) for key in ("x", "y", "width", "height"))
    except ValueError:
        return 0.0, 0.0, 0.0, 0.0
    return rect if all(math.isfinite(v) for v in rect) else (0.0, 0.0, 0.0, 0.0)  # type: ignore[return-value]


def _same_rect(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    """Equal within a point: WebDriverAgent rounds frames to whole points."""
    return all(abs(p - q) <= 1.0 for p, q in zip(a, b))


def _duplicate(parent: Element, child: Element) -> bool:
    """Is `child` only the parent listed a second time? The same quoted text and rect (within a point), whatever
    the roles. Identifier, value and checked only keep both when both carry one and they differ; selected and
    enabled always compare. Elements without text are never collapsed."""
    def clash(a: object, b: object) -> bool:
        return a not in ("", None) and b not in ("", None) and a != b
    return bool(child.text and parent.text == child.text and _same_rect(parent.rect, child.rect)
                and (parent.selected, parent.enabled) == (child.selected, child.enabled)
                and not clash(parent.identifier, child.identifier) and not clash(parent.value, child.value)
                and not clash(parent.checked, child.checked))


def _overlap(a: tuple[float, ...], b: tuple[float, ...]) -> tuple[float, float, float, float]:
    """The part of rect `a` inside rect `b` (zero-size when they don't meet)."""
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    return (x0, y0, max(0.0, x1 - x0), max(0.0, y1 - y0))


def _union_area(rects: list[tuple[float, float, float, float]]) -> float:
    """The area the rects cover together (no double counting)."""
    rects = [r for r in rects if r[2] > 0 and r[3] > 0]
    xs = sorted({v for r in rects for v in (r[0], r[0] + r[2])})
    total = 0.0
    for left, right in zip(xs, xs[1:]):
        spans = sorted((r[1], r[1] + r[3]) for r in rects if r[0] <= left and r[0] + r[2] >= right)
        end = -math.inf
        for top, bottom in spans:
            total += (right - left) * max(0.0, bottom - max(top, end))
            end = max(end, bottom)
    return total


def _distance(point: tuple[float, float], rect: tuple[float, float, float, float]) -> float:
    """How far a point is from a rect (0 inside it)."""
    dx = max(rect[0] - point[0], 0.0, point[0] - rect[0] - rect[2])
    dy = max(rect[1] - point[1], 0.0, point[1] - rect[1] - rect[3])
    return math.hypot(dx, dy)


def visible_part(rect: tuple[float, float, float, float], bars: tuple[Bar, ...],
                 screen: tuple[float, float] | None = None) -> tuple[float, float, float, float]:
    """What of `rect` a finger could reach: the rect inside the screen, minus every bar. Where a bar
    leaves several strips (a bar across the middle), the strip holding the rect's centre, or else the
    nearest one, stays. Zero-size when nothing is left."""
    centre = (rect[0] + rect[2] / 2, rect[1] + rect[3] / 2)
    part = _overlap(rect, (0.0, 0.0, *screen)) if screen else rect
    for bar in bars:
        cut = _overlap(part, bar.rect)
        if cut[2] <= 0 or cut[3] <= 0:
            continue
        x, y, w, h = part
        strips = [r for r in ((x, y, w, cut[1] - y), (x, cut[1] + cut[3], w, y + h - cut[1] - cut[3]),
                              (x, y, cut[0] - x, h), (cut[0] + cut[2], y, x + w - cut[0] - cut[2], h))
                  if r[2] > 0 and r[3] > 0]
        if not strips:
            return (x, y, 0.0, 0.0)
        part = min(strips, key=lambda r: (_distance(centre, r), -r[2] * r[3]))
    return part


def _clean(source: str) -> str:
    """The XML without characters XML 1.0 forbids."""
    def drop(m: re.Match) -> str:
        code = int(m.group(1), 16) if m.group(1) else int(m.group(2))
        return "" if code < 0x20 and code not in (9, 10, 13) else m.group(0)
    return _CONTROL_REFS.sub(drop, _RAW_CONTROLS.sub("", source))


def _kind(node: ET.Element) -> str:
    return _text(node.get("type") or node.tag).removeprefix("XCUIElementType")


def _parse(source: str, screen: tuple[float, float] | None) -> ET.Element:
    """WebDriverAgent's XML as a tree, with every frame in the screen's coordinates. Raises DeviceError."""
    try:
        root = ET.fromstring(_clean(source))
    except ET.ParseError as e:
        raise DeviceError("WebDriverAgent sent an unreadable element tree") from e
    if screen is not None and screen[0] > screen[1]:
        _upright_keyboard(root, screen)
    return root


def _upright_keyboard(root: ET.Element, screen: tuple[float, float]) -> None:
    """In landscape the keyboard's window holds a view of the portrait screen's size (430x932 on a 932x430
    screen), and everything in it is framed as if the phone were upright: seen on the phone 2026-11-11, the
    keyboard at (2, 117, 162, 698) turned left and (266, 117, 162, 698) turned right, where it is drawn as the
    bottom strip (0, 266, 932, 164) either way. Its frames are turned into the screen's, in place. Which way the
    phone is turned shows in which edge of that view the keys hug, as they are at the bottom of the screen."""
    width, height = screen
    parents = {child: up for up in root.iter() for child in up}
    for view in list(root.iter()):
        x, y, w, h = _rect(view)
        up = parents.get(view)
        if not (_same_rect((x, y, w, h), (0, 0, height, width)) and up is not None
                and _same_rect(_rect(up), (0, 0, width, height))):
            continue
        keys = next((_rect(n) for n in view.iter() if _kind(n) == "Keyboard" and _rect(n)[2] > 0), None)
        if keys is None:
            continue  # nothing shows which way it is turned
        hug_left = keys[0] + keys[2] / 2 < height / 2

        def turn(r: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
            x, y, w, h = r
            return (y, height - x - w, h, w) if hug_left else (width - y - h, x, h, w)
        for n in view.iter():
            for key, value in zip(("x", "y", "width", "height"), turn(_rect(n))):
                n.set(key, f"{value:g}")


def _alert(node: ET.Element) -> Alert:
    buttons = [_text(n.get("label")) or _text(n.get("name")) for n in node.iter() if _kind(n) == "Button"]
    return Alert(_text(node.get("label")) or _text(node.get("name")), tuple(b for b in buttons if b), _rect(node))


def read(source: str, screen: tuple[float, float] | None = None, keyboard: bool = False) -> Tree:
    """Flatten WDA's XML tree into the elements worth showing, in reading order, numbered e1...
    Skips what has no size or whose centre (where a tap would land) is clipped by the screen or a
    scroll container, and the keyboard's keys unless `keyboard`. Raises DeviceError on a broken tree."""
    tree = Tree([])
    bars: list[tuple[bool, float]] = []  # (at the top?, how far it reaches in) of each bar seen
    # Everything below is numbered by visit order, so "what is inside this node" is a range of numbers.
    found: list[tuple[Element, int, tuple[int, ...]]] = []  # each listed element, its number, the bars it is inside
    covering: list[tuple[int, Bar]] = []  # the bars iOS draws over content, by number
    # number ranges a later layer covers, where, whether it is a presentation (which hides bars too), and the
    # layer's own range
    hidden: list[tuple[int, int, tuple[float, float, float, float], bool, tuple[int, int]]] = []
    off_numbers: list[int] = []  # where in the visit order each entry of tree.offscreen lies
    beyond: list[tuple[int, bool]] = []  # (its place in the visit order, below?) of each element past the top or bottom

    def tally(plain: Element | None, over: Element | None, number: int) -> None:
        """Count an element that would be listed but lies wholly above or below the screen."""
        if plain is None or screen is None or (over is not None and _duplicate(over, plain)):
            return
        if plain.y + plain.height <= 0:
            beyond.append((number, False))
        elif plain.y >= screen[1]:
            beyond.append((number, True))

    def tally_all(node: ET.Element, over: Element | None, number: int) -> None:
        if _kind(node) == "Keyboard":
            return
        plain = _plain(node)
        tally(plain, over, number)
        for child in node:
            tally_all(child, plain or over, number)
    visits = [0]
    numbered: dict[int, ET.Element] = {}  # each visited node by its number...
    ends: dict[int, int] = {}  # ...and the number after its subtree
    screen_rect = (0.0, 0.0, *screen) if screen else None

    def walk(node: ET.Element, clip: tuple[float, float, float, float], in_keyboard: bool, in_tab_bar: bool,
             in_alert: bool, parent: Element | None = None, inside: tuple[int, ...] = (), depth: int = -1,
             keys_top: float | None = None) -> None:
        kind = _kind(node)
        depth = 0 if kind == "Application" else depth  # levels below the application
        x, y, w, h = rect = _rect(node)
        label, name, value = _text(node.get("label")), _text(node.get("name")), _text(node.get("value"))
        traits = {t.strip() for t in (node.get("traits") or "").split(",")}
        left, top, right, bottom = clip
        # The keyboard's window holds more than the Keyboard node: the views around it, and the globe and
        # dictation keys beside or below the keys. None of that is the app's (seen on the phone 2026-11-10: four
        # such lines in every answer that showed or hid the keyboard). What sits above the keys there stays
        # listed: an app's own bar over the keyboard ("Done") lives in the same window.
        keys_top = keyboard_windows.get(node, keys_top)
        in_keyboard = in_keyboard or (keys_top is not None and y >= keys_top - 1 and node not in keyboard_holders)
        if w > 0 and h > 0 and (x + w <= left or y + h <= top or x >= right or y >= bottom):
            if screen_rect is not None and not in_keyboard:
                before = len(tree.offscreen)
                tally_all(node, parent, visits[0])
                _offscreen(node, tree.offscreen, parent)
                off_numbers.extend([visits[0]] * (len(tree.offscreen) - before))
                visits[0] += 1  # the subtree takes a place in the order, so a later layer's range holds it
            return  # nothing in this subtree is on screen
        number = visits[0]
        visits[0] += 1
        numbered[number] = node
        cx, cy = x + w / 2, y + h / 2
        visible = w > 0 and h > 0 and left <= cx < right and top <= cy < bottom
        if kind == "Alert" and visible and tree.alert is None:
            tree.alert = _alert(node)
        if kind == "Keyboard" and w > 0 and h > 0:
            tree.keyboard = True  # it meets the screen (a node wholly off it returned above): a parked one doesn't
        if kind in BARS and visible and screen:
            if y < screen[1] * BAR_EDGE:
                bars.append((True, y + h))
            elif y + h > screen[1] * (1 - BAR_EDGE):
                bars.append((False, screen[1] - y))
        in_keyboard = in_keyboard or kind == "Keyboard"
        in_alert = in_alert or kind == "Alert"
        # WDA mirrors the label into name when there is no identifier
        identifier = "" if name == label else name
        role = ROLES.get(kind, kind.lower())
        if kind in COVERING_BARS:
            inside += (number,)  # a bar's own contents and the bar itself aren't covered by it
            if visible:
                cover = _keyboard_view(node, parents, screen_rect) if kind == "Keyboard" else rect
                covering.append((number, Bar(kind, cover, Element("", role, label, identifier, rect=cover)
                                             .line(ref=False, actions=False))))
                if kind == "Keyboard" and screen and cover[1] < screen[1]:
                    bars.append((False, screen[1] - cover[1]))  # a swipe on the keys scrolls nothing: stay above
        if kind == "Button" and in_tab_bar:
            role = "tab"
        elif kind == "StaticText" and "Header" in traits:
            role = "header"
        if kind == "Alert":
            worth = False
        elif role == "scroll":
            worth = True
        else:
            worth = (kind in INTERACTIVE or label or value or identifier) and not (kind in CONTAINERS and not identifier)
        # ...nor is a view of that window that reaches down into the keys (`inputView` on the phone, around the
        # keys but not above them in the tree); a bar that ends where the keys begin is the app's
        worth = worth and identifier not in IOS_NOISE and node not in keyboard_holders and not (
            keys_top is not None and not in_keyboard and y + h > keys_top + 1)
        # ...nor a plain view of exactly the keyboard's size and place in another window (`inputView`, in the
        # text effects window on the phone)
        if worth and kind == "Other" and not label and not value and any(_same_rect(rect, r) for r in keyboard_rects):
            worth = False
        listed = parent  # the nearest listed ancestor, for what this node's children are compared with
        if visible and worth and (keyboard or not in_keyboard):
            enabled = node.get("enabled", "true") != "false" and "NotEnabled" not in traits
            checked = (value == "1") if role == "switch" else None
            candidate = Element("", role, label, identifier, value, rect, enabled,
                                "Selected" in traits, checked, ROLE_ACTIONS.get(role, ()), kind, in_alert, False, name,
                                kind == "SecureTextField", placeholder=_text(node.get("placeholderValue")))
            if parent and _duplicate(parent, candidate):
                # the same thing as its parent, exposed twice (React Native does): list it once, the one
                # that takes an action; both or neither, the ancestor
                if candidate.actions and not parent.actions:
                    i = next(i for i, f in enumerate(found) if f[0] is parent)
                    found[i] = (candidate, found[i][1], found[i][2])  # in the parent's place and span
                    listed = candidate
            else:
                listed = candidate
                found.append((listed, number, inside))
        elif worth and not visible and screen_rect is not None and not in_keyboard and kind != "Alert":
            plain = _plain(node)
            tally(plain, parent, number)
            if (plain is not None and len(tree.offscreen) < MAX_OFFSCREEN
                    and not (tree.offscreen and _duplicate(tree.offscreen[-1], plain))):
                tree.offscreen.append(plain)  # its centre is past the screen or its scroll view's edge
                off_numbers.append(number)
        if kind in CLIPPING:
            clip = (max(left, x), max(top, y), min(right, x + w), min(bottom, y + h))
        spans = []  # the numbers each child's subtree used
        for child in node:
            start = visits[0]
            walk(child, clip, in_keyboard, in_tab_bar or kind == "TabBar", in_alert, listed, inside, depth + 1, keys_top)
            spans.append((start, visits[0]))
        ends[number] = visits[0]
        if screen_rect and depth + 1 <= LAYER_MAX_DEPTH:
            for k, child in enumerate(node):
                if k and spans[k][1] > spans[k][0]:  # the earlier children, under what the layer hides
                    rects, presented = _layer(child, spans[k], found, screen_rect, numbered, ends)
                    hidden.extend((spans[0][0], spans[k][0], r, presented, spans[k]) for r in rects)

    inf = math.inf
    root = _parse(source, screen)
    parents = {child: up for up in root.iter() for child in up}
    # On the phone the keyboard has a window of its own: Window > a full-screen view > [an empty view, the strip
    # with the globe key, the views holding the Keyboard node].
    keyboard_holders: set[ET.Element] = set()  # the views a keyboard sits in, below its window
    keyboard_windows: dict[ET.Element, float] = {}  # the highest of them, and where its keys begin
    for n in root.iter():
        if _kind(n) == "Keyboard" and _rect(n)[3] > 0:
            window, up = n, parents.get(n)
            while up is not None and _kind(up) not in ("Window", "Application"):
                keyboard_holders.add(up)
                window, up = up, parents.get(up)
            if window is not n:
                keyboard_windows[window] = min(_rect(n)[1], keyboard_windows.get(window, math.inf))
    keyboard_rects = [_keyboard_view(n, parents, screen_rect) for n in root.iter()
                      if _kind(n) == "Keyboard" and _rect(n)[3] > 0]
    if _kind(root) == "Application" and (bundle := _text(root.get("bundleId"))):
        tree.app = (_text(root.get("label")) or _text(root.get("name")) or bundle, bundle)
    walk(root,(0.0, 0.0, screen[0], screen[1]) if screen else (-inf, -inf, inf, inf), False, False, False)

    keyboards = {n for n, bar in covering if bar.kind == "Keyboard"}

    def over(number: int, point: tuple[float, float], barlike: bool) -> tuple[int, int] | None:
        """The range of the layer that hides it, else None. Opaque content leaves bars and their children alone
        (the cover logic handles touches near them); a presentation dims them. The keyboard is always in front."""
        return next((layer for a, b, rect, presented, layer in hidden
                     if a <= number < b and _holds(rect, point) and (presented or not barlike)), None)
    shown: list[tuple[Element, int, tuple[int, ...]]] = []
    under: list[tuple[Element, tuple[int, int]]] = []  # what a layer hides, with that layer's range
    for e, number, inside in found:
        layer = None if keyboards & set(inside) else over(number, e.center, bool(inside) or e.type in BARS)
        if layer is None:
            shown.append((e, number, inside))
        else:
            under.append((e, layer))
    live = [(n, bar) for n, bar in covering if bar.kind == "Keyboard"
            or over(n, (bar.rect[0] + bar.rect[2] / 2, bar.rect[1] + bar.rect[3] / 2), False) is None]
    bar_numbers = {n for n, _ in covering}
    area = screen[0] * screen[1] if screen else math.inf
    ahead = [(o, m, set(within)) for o, m, within in shown]
    for e, number, inside in shown[:MAX_ELEMENTS]:
        bars_over = [(n, bar) for n, bar in live if n not in inside and _meets(e.rect, bar.rect)]
        over = {n for n, _ in bars_over}
        # then every later listed element that is neither its descendant nor a layer, nor part of a bar over it
        later = tuple(Bar(o.type, o.rect, o.line(ref=False, actions=False)) for o, m, within in ahead
                      if m >= ends[number] and m not in bar_numbers and not over & within
                      and o.role not in PASSIVE and o.width * o.height < OVERLAY_MAX * area and _meets(e.rect, o.rect))
        e.covered_by = tuple(bar for _, bar in bars_over) + later
    tree.items = [e for e, _, _ in shown[:MAX_ELEMENTS]]
    tree.more = max(0, len(shown) - MAX_ELEMENTS)
    for i, e in enumerate(tree.items, 1):
        e.ref = f"e{i}"

    def front(layer: tuple[int, int]) -> Element | None:
        """What the layer shows first with a name: what the agent closes or answers. The element, not its
        line: its ref here is this read's, and the agent's list may know it under another."""
        inside = [e for e, n, _ in shown[:MAX_ELEMENTS] if layer[0] <= n < layer[1]]
        return next((e for e in inside if e.label or e.identifier), inside[0] if inside else None)
    tree.behind = [(e, front(layer)) for e, layer in under[:MAX_OFFSCREEN]]
    presented = [(a, b, layer) for a, b, _, is_presented, layer in hidden if is_presented]
    open_side = [below for n, below in beyond if not any(a <= n < b for a, b, _ in presented)]
    tree.below = sum(open_side)
    tree.above = len(open_side) - tree.below
    for e, n in zip(tree.offscreen, off_numbers):  # off screen and under a sheet: closing the sheet comes first
        layer = next((layer for a, b, layer in presented if a <= n < b), None)
        if layer is not None and len(tree.behind) < MAX_OFFSCREEN:
            tree.behind.append((e, front(layer)))
    for top, reach in ([] if tree.alert else bars):  # bars behind an alert aren't what a swipe would meet
        if top:
            tree.top_inset = max(tree.top_inset or 0.0, reach)
        else:
            tree.bottom_inset = max(tree.bottom_inset or 0.0, reach)
    return tree


def _plain(node: ET.Element) -> Element | None:
    """The element a node would be listed as, without what only a visible one gets (covers, duplicate folding):
    for the off-screen list. None when it wouldn't be listed."""
    kind = _kind(node)
    _, _, w, h = rect = _rect(node)
    label, name, value = _text(node.get("label")), _text(node.get("name")), _text(node.get("value"))
    traits = {t.strip() for t in (node.get("traits") or "").split(",")}
    identifier = "" if name == label else name
    role = "header" if kind == "StaticText" and "Header" in traits else ROLES.get(kind, kind.lower())
    worth = role == "scroll" or ((kind in INTERACTIVE or label or value or identifier)
                                 and not (kind in CONTAINERS and not identifier))
    worth = worth and identifier not in IOS_NOISE
    if not worth or w <= 0 or h <= 0 or kind in ("Alert", "Keyboard"):
        return None
    enabled = node.get("enabled", "true") != "false" and "NotEnabled" not in traits
    return Element("", role, label, identifier, value, rect, enabled, "Selected" in traits,
                   (value == "1") if role == "switch" else None, ROLE_ACTIONS.get(role, ()), kind, False, False, name,
                   kind == "SecureTextField", placeholder=_text(node.get("placeholderValue")))


def _offscreen(node: ET.Element, out: list[Element], over: Element | None = None) -> None:
    """Add a subtree that lies wholly outside the screen (or its scroll view) to `out`, in reading order, up to
    MAX_OFFSCREEN. A keyboard's keys are never targets: its subtree is skipped (a parked one sits below the screen).
    An element that only repeats the one it is in (`over`; React Native exposes a text twice) is added once, as
    on screen: seen on the phone 2026-11-10 as every off-screen change said twice."""
    if len(out) >= MAX_OFFSCREEN or _kind(node) == "Keyboard":
        return
    plain = _plain(node)
    if plain is not None and over is not None and _duplicate(over, plain):
        plain = None
    if plain is not None:
        out.append(plain)
    for child in node:
        _offscreen(child, out, plain or over)


def _keyboard_view(keyboard: ET.Element, parents: dict[ET.Element, ET.Element],
                   screen: tuple[float, float, float, float] | None) -> tuple[float, float, float, float]:
    """Where the keyboard is drawn: its highest ancestor smaller than the screen. The strip of suggestions above
    the keys and the globe and dictation keys below them are the keyboard's too, outside its own rect."""
    rect = _rect(keyboard)
    if screen is None:
        return rect
    up = parents.get(keyboard)
    while up is not None and _kind(up) not in ("Window", "Application"):
        r = _rect(up)
        if r[2] * r[3] >= LAYER_COVER * screen[2] * screen[3]:
            break
        rect, up = r, parents.get(up)
    return rect


def _holds(rect: tuple[float, float, float, float], point: tuple[float, float]) -> bool:
    return rect[0] <= point[0] < rect[0] + rect[2] and rect[1] <= point[1] < rect[1] + rect[3]


def _meets(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    """Do two rects share some area?"""
    o = _overlap(a, b)
    return o[2] > 0 and o[3] > 0


def _backdrop(span: tuple[int, int], found: list[tuple[Element, int, tuple[int, ...]]],
              screen: tuple[float, float, float, float], numbered: dict[int, ET.Element], ends: dict[int, int]) -> int | None:
    """The number of the backdrop the layer (its nodes numbered `span`) holds before its other listed content,
    else None. A childless Button or Other (it may itself be listed) at the top-left, nearly as wide as the
    screen and a good part as tall. Listed content that wraps it doesn't come before it."""
    listed = [number for _, number, _ in found if span[0] <= number < span[1]]
    for n in range(span[0], span[1]):
        node = numbered.get(n)
        if node is None or _kind(node) not in ("Button", "Other") or len(node):
            continue
        x, y, w, h = _rect(node)
        if (x <= 1 and y <= 1 and w >= BACKDROP_WIDTH * screen[2] and h >= BACKDROP_HEIGHT * screen[3]
                and not any(m < n and ends[m] <= n for m in listed)):
            return n
    return None


def _layer(child: ET.Element, span: tuple[int, int], found: list[tuple[Element, int, tuple[int, ...]]],
           screen: tuple[float, float, float, float], numbered: dict[int, ET.Element], ends: dict[int, int]
           ) -> tuple[list[tuple[float, float, float, float]], bool]:
    """The places a later sibling hides in the earlier ones (empty when it is no layer; see LAYER_MAX_DEPTH, the
    caller checks the depth), and whether it is a presentation. With a backdrop and something to act on: its whole
    rect, and bars are hidden too (a sheet dims them). With no backdrop: the rects of what it lists, when they
    fill half the screen, and bars stay. The keyboard's window is no layer: it starts with an empty full-screen
    view and has keys outside the keyboard ("Next keyboard"), yet it hides nothing but the keys' own area.
    One holding a Popover (the share sheet) is a presentation even with nothing listed to act on: its share
    targets list as text."""
    area = screen[2] * screen[3]
    if area <= 0 or (a := _overlap(_rect(child), screen))[2] * a[3] < LAYER_COVER * area:
        return [], False
    kinds = {_kind(n) for n in child.iter()}
    if "Keyboard" in kinds:
        return [], False
    if "Popover" in kinds:  # a share sheet or popover: a touch outside it closes it, never reaches the app
        return [_rect(child)], True
    # what the layer lists below itself, and itself when it is a scroll view (a scroll view is content)
    inner = [e for e, number, _ in found if (span[0] < number < span[1] or (number == span[0] and e.role == "scroll"))
             and (e.role == "scroll" or (o := _overlap(e.rect, screen))[2] * o[3] < WRAPPER * area)]
    if (dim := _backdrop(span, found, screen, numbered, ends)) is not None:
        acts = any(e.actions for e, number, _ in found if span[0] < number < span[1] and number != dim)
        return ([_rect(child)] if acts else []), True
    if _union_area([_overlap(e.rect, screen) for e in inner]) >= LAYER_FILL * area:
        return [e.rect for e in inner], False
    return [], False


def elements(source: str, screen: tuple[float, float] | None = None, keyboard: bool = False) -> list[Element]:
    return read(source, screen, keyboard).items


def alert_line(alert: Alert) -> str:
    return f"Alert: {json.dumps(alert.title, ensure_ascii=False)} " + " ".join(f"[{b}]" for b in alert.buttons)


def describe(items: list[Element], screen: tuple[float, float], scale: float | None = None,
             app: tuple[str, str] | None = None, alert: Alert | None = None, keyboard: bool = False,
             more: int = 0, off: tuple[int, int] = (0, 0)) -> str:
    """The full list (docs/AGENT-TOOLS.md §3.2): the header lines, then one line per element.
    `app` is (name, bundle id); `off` is how many elements lie (above, below) the screen."""
    size = f"{round(screen[0])}x{round(screen[1])} pt" + (f" @{scale:g}x" if scale else "")
    beyond = ", ".join(f"+{n} {side}" for n, side in ((off[1], "below"), (off[0], "above")) if n > 0)
    lines = [f"Screen {size} · {len(items)} elements" + (f" ({beyond})" if beyond else "")]
    if app:
        lines.append(f"App: {app[0]} ({app[1]})")
    if alert:
        lines.append(alert_line(alert))
    if keyboard:
        lines.append("Keyboard: shown")
    lines.extend(e.line() for e in items)
    if more:
        lines.append(f"(+{more} more below; scroll)")
    if not items:
        lines.append("(nothing visible; the app may still be loading)")
    return "\n".join(lines)


@dataclass(frozen=True)
class Target:
    """What a tool acts on (docs/AGENT-TOOLS.md §2): exactly one of `ref`, `id`, `label`, `text`,
    or the point `x`, `y`; `index` (0-based, reading order) only with id, label or text."""
    ref: str | None = None
    id: str | None = None
    label: str | None = None
    text: str | None = None
    x: float | None = None
    y: float | None = None
    index: int | None = None

    @property
    def kind(self) -> str:
        """"ref", "id", "label", "text" or "point"."""
        for name in ("ref", "id", "label", "text"):
            if getattr(self, name) is not None:
                return name
        return "point"

    @property
    def point(self) -> tuple[float, float] | None:
        return None if self.x is None or self.y is None else (self.x, self.y)


_TARGET_KEYS = ("ref", "id", "label", "text", "x", "y", "index")
TARGET_HELP = ('a target is exactly one of {"ref": "e3"}, {"id": "login-button"}, {"label": "Log in"}, '
               '{"text": "Log in"} or {"x": 120, "y": 300}, plus an optional "index" with id, label or text')


def _number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def parse_target(raw: dict | None) -> Target:
    """A target from a tool's arguments. Raises Refused("bad_params") for anything else: no target,
    two kinds, half a point, `index` with a ref or a point, a wrong type, an unknown key."""
    if not isinstance(raw, dict):
        raise Refused("bad_params", f"target must be an object; {TARGET_HELP}")
    given = {k: v for k, v in raw.items() if v is not None}  # a JSON null is the same as absent
    unknown = sorted(set(given) - set(_TARGET_KEYS))
    if unknown:
        raise Refused("bad_params", f"unknown target key {unknown[0]!r}; {TARGET_HELP}")
    kinds = [k for k in ("ref", "id", "label", "text") if k in given]
    if ("x" in given) != ("y" in given):
        raise Refused("bad_params", f"a point needs both x and y; {TARGET_HELP}")
    if "x" in given:
        kinds.append("point")
    if len(kinds) != 1:
        raise Refused("bad_params", f"a target takes exactly one kind, not {len(kinds)}; {TARGET_HELP}")
    for k in ("ref", "id", "label", "text"):
        if k in given and (not isinstance(given[k], str) or not given[k].strip()):
            raise Refused("bad_params", f"target {k} must be a non-empty string")
    for k in ("x", "y"):
        if k in given and not _number(given[k]):
            raise Refused("bad_params", f"target {k} must be a number in points")
    index = given.get("index")
    if index is not None:
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise Refused("bad_params", "target index must be a whole number, 0 or more")
        if kinds[0] in ("ref", "point"):
            raise Refused("bad_params", f"target index goes with id, label or text, not with {kinds[0]}")
    return Target(**{k: (float(v) if k in ("x", "y") else v) for k, v in given.items()})


MAX_CANDIDATES = 5


def _norm(text: str) -> str:
    return text.strip().casefold()


def _inside(inner: Element, outer: Element) -> bool:
    return (outer.x <= inner.x and outer.y <= inner.y and inner.x + inner.width <= outer.x + outer.width
            and inner.y + inner.height <= outer.y + outer.height)


def _actionable(e: Element) -> bool:
    return any(a in e.actions for a in ("tap", "type", "toggle"))


def _without_nested(hits: list[Element]) -> list[Element]:
    """Of two matches where one's rect contains the other's, only the actionable one stays (a button
    and its own text child are one match). Both or neither actionable: both stay."""
    drop = set()
    for a in hits:
        for b in hits:
            if a is not b and _inside(b, a) and _actionable(a) != _actionable(b):
                drop.add(id(b if _actionable(a) else a))
    return [e for e in hits if id(e) not in drop]


def more_candidates(total: int, shown: int) -> list[str]:
    """The closing line of an `ambiguous` message that lists only some of the matches."""
    return [f'  (+{total - shown} more; use a ref or "index")'] if total > shown else []


def resolve(items: list[Element], target: Target) -> Element | tuple[float, float]:
    """The element a target names on the agent's list, or its point (a touch at x, y hits whatever
    is there, so a point never becomes an element). Several matches without an `index` are
    `ambiguous`, with candidates; none, or an `index` past the matches, are `not_found`."""
    if target.point is not None:
        return target.point
    if target.ref is not None:
        for e in items:
            if e.ref == target.ref:
                return e
        raise Refused("not_found", f"{target.ref} isn't on screen any more; look again.")
    if target.id is not None:
        # a real identifier first; WebDriverAgent's raw name (it mirrors the label) only if none match
        hits = [e for e in items if e.identifier == target.id] or [e for e in items if e.name == target.id]
        what, miss = f'id "{target.id}"', f'nothing on screen has id "{target.id}"; look, then pick another target.'
    elif target.label is not None:
        hits = _without_nested([e for e in items if _norm(e.label) == _norm(target.label)])
        what = f'label "{target.label}"'
        miss = f'nothing on screen has label "{target.label}"; use {{text}} for a partial match, or look.'
    else:
        want = _norm(target.text)
        hits = [e for e in items if _norm(e.label) == want or _norm(e.value) == want]
        if not hits:
            hits = [e for e in items if want in _norm(e.label) or want in _norm(e.value)]
        hits = _without_nested(hits)
        what, miss = f'text "{target.text}"', f'nothing on screen shows "{target.text}"; look, then pick another target.'
    if not hits:
        raise Refused("not_found", miss)
    where = ""
    if len(hits) > 1 and any(e.in_alert for e in hits):
        # AGENT-TOOLS v1.5: an alert's own Cancel, not the Cancel behind it. When some matches are in the
        # alert, only those count, and `index` picks among them
        hits, where = [e for e in hits if e.in_alert], " in the alert (matches behind it don't count)"
    if target.index is not None:
        if target.index >= len(hits):
            raise Refused("not_found", f"index {target.index} is out of range: only {len(hits)} "
                                       f"element{'s' if len(hits) != 1 else ''} match{'es' if len(hits) == 1 else ''} "
                                       f"{what}{where}")
        return hits[target.index]

    if len(hits) > 1:
        shown = hits[:MAX_CANDIDATES]
        raise Refused("ambiguous", f"{len(hits)} elements match {what}.\nCandidates:\n"
                      + "\n".join([*(f"  {e.line()}" for e in shown), *more_candidates(len(hits), len(shown)),
                                   'Retry with one of the refs, or add "index" (0-based).']))
    return hits[0]


class Back(NamedTuple):
    bar: bool  # a NavigationBar is on screen
    point: tuple[float, float] | None  # the centre of its back button, if it has one
    dismiss: tuple[float, float] | None = None  # where a touch closes the popover that is open on top, if one is


DISMISS_MARGIN = 60.0  # points a closing touch keeps from the screen's top (the status bar) and from the sheet


# Buttons that sit at a bar's left edge but do something other than go back.
NOT_BACK = {"cancel", "close", "done", "save", "edit", "delete", "clear", "discard", "menu", "sign out"}


def back_button(source: str, screen: tuple[float, float]) -> Back:
    """Where to tap to go back, from WebDriverAgent's XML. NavigationBar is a container, so it isn't in
    the element list. In the top bar, in order: a Button named `BackButton` or labelled "Back"; else the
    leftmost Button in the bar's left quarter, unless it is Cancel, Done, Menu or another button that
    does something else; else no point (a bar without a back button is usually a root screen)."""
    root = _parse(source, screen)

    def on_screen(rect: tuple[float, float, float, float]) -> bool:
        x, y, w, h = rect
        return w > 0 and h > 0 and 0 <= x + w / 2 < screen[0] and 0 <= y + h / 2 < screen[1]

    if (dismiss := _dismiss_point(root, screen)) is not None:
        return Back(False, None, dismiss)  # what is open on top closes first; the bar behind it can't be reached
    bars = [n for n in root.iter() if _kind(n) == "NavigationBar" and on_screen(_rect(n))]
    if not bars:
        return Back(False, None)
    bar = min(bars, key=lambda n: _rect(n)[1])
    bar_x, _, bar_w, _ = _rect(bar)
    buttons = [n for n in bar.iter() if _kind(n) == "Button" and on_screen(_rect(n))]

    def centre(n: ET.Element) -> tuple[float, float]:
        x, y, w, h = _rect(n)
        return x + w / 2, y + h / 2

    for n in buttons:
        if _text(n.get("name")) == "BackButton" or _text(n.get("label")).lower() == "back":
            return Back(True, centre(n))
    left = [n for n in buttons if centre(n)[0] < bar_x + bar_w / 4]
    left.sort(key=lambda n: centre(n)[0])
    for n in left[:1]:
        if (_text(n.get("label")) or _text(n.get("name"))).lower() not in NOT_BACK:
            return Back(True, centre(n))
    return Back(True, None)


def _dismiss_point(root: ET.Element, screen: tuple[float, float]) -> tuple[float, float] | None:
    """Where a person taps to close an open popover (iOS 26's share sheet): on its "dismiss popup" region, in the
    middle of the part above the sheet, or below it when there is no room above. None when no popover is open."""
    region = next((n for n in root.iter() if _text(n.get("name")) == "PopoverDismissRegion"), None)
    sheets = [n for n in root.iter() if _kind(n) == "Popover" and _rect(n)[2] > 0 and _rect(n)[3] > 0]
    if region is None or not sheets:
        return None
    rx, ry, rw, rh = _overlap(_rect(region), (0.0, 0.0, *screen))
    _, sy, _, sh = _rect(sheets[-1])
    above, below = sy - ry, (ry + rh) - (sy + sh)
    if above >= 2 * DISMISS_MARGIN:
        return rx + rw / 2, ry + max(above / 2, DISMISS_MARGIN)
    if below >= 2 * DISMISS_MARGIN:
        return rx + rw / 2, sy + sh + below / 2
    return None


def at_point(items: list[Element], point: tuple[float, float], role: str) -> Element | None:
    """The front-most listed element of `role` whose rect contains the point (a later element in
    reading order is drawn over an earlier one), or None. The far edge is outside, so two fields that
    share an edge don't both claim it. For tools that act on an element, not a touch."""
    x, y = point
    for e in reversed(items):
        if e.role == role and e.x <= x < e.x + e.width and e.y <= y < e.y + e.height:
            return e
    return None


BLANK_GRID = 40.0  # points between the points blank_point tries when no label offers one
# how far a text's centre must be from any image for blank_point to rest a finger there: a
# floating button takes touches well outside its icon (Expo's dev tools gear, a 26 pt image, took them 20-30 pt
# around it on the phone 2026-11-11, and opened the developer menu)
BLANK_CLEAR = 44.0


# iOS's own views that carry an identifier but nothing to read or act on: listing them is noise (a first-time agent
# asked what `image id=AdditionalDimmingOverlay` was, 2026-11-11; it is the dimming under the navigation bar)
IOS_NOISE = frozenset({"AdditionalDimmingOverlay"})
SPRINGBOARD_ROOT = 'bundleId="com.apple.springboard"'  # in the tree's first element when SpringBoard is in front
SHEET_BESIDE = 60.0  # points of screen a sheet must leave free on a side for a touch beside it
SHEET_GRIP = 14.0  # how far below a sheet's top edge a finger takes hold to pull it down
SHEET_PULL = 0.7  # the pull, in sheet heights (at least SHEET_PULL_MIN points, and not past the screen)
SHEET_PULL_MIN = 250.0
SHEET_MIN_TOP = 20.0  # a sheet's top edge is below the status bar; what starts higher fills the screen


def sheet_box(front: Element | None, items: list[Element],
              screen: tuple[float, float] | None = None) -> tuple[float, float, float, float] | None:
    """The frame of the sheet that is open in front: the largest listed element that holds another listed
    element and doesn't fill the screen. What is first in front is often the sheet's backdrop, which does fill
    it (seen on the phone 2026-11-10: `lab-sheet2-backdrop @0,0 430x932`, then `lab-sheet2 @0,690 430x242`); the
    sheet is then the panel inside. With no such element, the front element's own frame, else the frame around
    everything listed. None with nothing to go by."""
    def fills(e: Element) -> bool:
        return screen is not None and e.width * e.height >= LAYER_COVER * screen[0] * screen[1]

    def holds_another(e: Element) -> bool:
        return any(o is not e and o.rect != e.rect and _holds(e.rect, o.center) for o in items)
    panels = [e for e in items if e.width > 0 and e.height > 0 and not fills(e) and holds_another(e)]
    if front is not None and not fills(front) and holds_another(front):
        return front.rect
    if panels:
        return max(panels, key=lambda e: e.width * e.height).rect
    if front is not None:
        return front.rect
    rects = [e.rect for e in items if e.width > 0 and e.height > 0]
    if not rects:
        return None
    x0, y0 = min(r[0] for r in rects), min(r[1] for r in rects)
    return x0, y0, max(r[0] + r[2] for r in rects) - x0, max(r[1] + r[3] for r in rects) - y0


def beside_sheet(box: tuple[float, float, float, float], screen: tuple[float, float], top: float,
                 bottom: float) -> tuple[float, float] | None:
    """Where a person taps to close a sheet from outside it: the middle of the screen it leaves free above it,
    else below it (between `top` and `bottom`, the bars' edges). None when it leaves less than SHEET_BESIDE
    points either way: a full-screen one has no outside."""
    x, y, w, h = box
    across = min(max(x + w / 2, 0.0), screen[0] - 1)
    if y - top >= SHEET_BESIDE:
        return across, (top + y) / 2
    if bottom - (y + h) >= SHEET_BESIDE:
        return across, (y + h + bottom) / 2
    return None


def sheet_pull(box: tuple[float, float, float, float],
               screen: tuple[float, float]) -> tuple[float, float, float, float] | None:
    """The drag that pulls a sheet down to close it, as x0, y0, x1, y1: from just inside its top edge, straight
    down. None for something that starts at the top of the screen: that is no sheet to pull, and a drag from
    the screen's top edge opens Notification Center, which is the owner's."""
    x, y, w, h = box
    if y < SHEET_MIN_TOP:
        return None
    across = min(max(x + w / 2, 0.0), screen[0] - 1)
    start = min(y + SHEET_GRIP, screen[1] - 2)
    return across, start, across, min(start + max(SHEET_PULL * h, SHEET_PULL_MIN), screen[1] - 2)


def blank_point(items: list[Element], screen: tuple[float, float], top: float,
                bottom: float) -> tuple[float, float] | None:
    """A point to touch that does nothing but take focus away from a text field: between `top` and `bottom` (the
    bars' edges, y), inside no element that takes an action and no bar that covers content. The centre of the
    first passive label or header there that is free and BLANK_CLEAR from every image (a finger rests on text
    without pressing anything; a floating button can show as only its icon, and takes touches around it), else
    the free point of a coarse grid farthest from what takes a touch, images and the band's edges (floating
    buttons sit near edges and corners), else None. A scroll view doesn't count as one that takes a touch: its
    empty area does nothing, and one often fills the whole screen."""
    blocks = [e.rect for e in items if e.actions and e.role != "scroll" and not e.in_alert]
    # the text edit menu over a field (Select, Paste) lists its items as `menuitem`, with no action: a touch on one
    # selects or pastes (blank_point picked "Select" on the phone 2026-11-11, both orientations)
    blocks += [e.rect for e in items if e.role == "menuitem" and not e.in_alert]
    blocks += [e.rect for e in items if e.type in COVERING_BARS]
    blocks += [bar.rect for e in items for bar in e.covered_by if bar.kind in COVERING_BARS]
    icons = [e.rect for e in items if e.role == "image" and not e.in_alert]

    def free(x: float, y: float) -> bool:
        return (0 <= x < screen[0] and top <= y < bottom and not any(_holds(r, (x, y)) for r in blocks))

    def clearance(x: float, y: float, rects: list[tuple[float, float, float, float]]) -> float:
        """How far the point is from the nearest of `rects`."""
        return min((math.hypot(max(r[0] - x, 0.0, x - r[0] - r[2]), max(r[1] - y, 0.0, y - r[1] - r[3]))
                    for r in rects), default=math.inf)
    for e in items:
        if (e.role in ("text", "header") and not e.in_alert and free(*e.center)
                and clearance(*e.center, icons) >= BLANK_CLEAR):
            return e.center
    best, room = None, -1.0
    y = top + BLANK_GRID / 2
    while y < bottom:
        x = BLANK_GRID / 2
        while x < screen[0]:
            if free(x, y) and (r := min(clearance(x, y, blocks + icons), x, screen[0] - x, y - top, bottom - y)) > room:
                best, room = (x, y), r
            x += BLANK_GRID
        y += BLANK_GRID
    return best


def _pointer(steps: list[dict]) -> dict:
    return {"actions": [{"type": "pointer", "id": "finger", "parameters": {"pointerType": "touch"}, "actions": steps}]}


def tap(x: float, y: float, hold_ms: int = TAP_MS) -> dict:
    """W3C actions for a tap (or a long press with a longer hold)."""
    return _pointer([{"type": "pointerMove", "duration": 0, "x": round(x), "y": round(y)},
                     {"type": "pointerDown", "button": 0},
                     {"type": "pause", "duration": hold_ms},
                     {"type": "pointerUp", "button": 0}])


def swipe(x0: float, y0: float, x1: float, y1: float, duration_ms: int = 350, hold_ms: int = 0) -> dict:
    """W3C actions for a drag from one point to another. With `hold_ms` the finger rests at the end before it
    lifts, so a scroll stops where the finger does instead of flinging."""
    return _pointer([{"type": "pointerMove", "duration": 0, "x": round(x0), "y": round(y0)},
                     {"type": "pointerDown", "button": 0},
                     {"type": "pause", "duration": 50},
                     {"type": "pointerMove", "duration": duration_ms, "x": round(x1), "y": round(y1)},
                     *([{"type": "pause", "duration": hold_ms}] if hold_ms else []),
                     {"type": "pointerUp", "button": 0}])


def scroll_drag(x0: float, y0: float, x1: float, y1: float) -> dict:
    """W3C actions for a scroll that stops where the finger does: a fast drag to the last stretch, that stretch
    slowly, then a rest (see SCROLL_EASE_MS)."""
    length = math.hypot(x1 - x0, y1 - y0)
    share = min(SCROLL_EASE_PT, SCROLL_EASE_SHARE * length) / length if length else 0.0
    mx, my = x1 - (x1 - x0) * share, y1 - (y1 - y0) * share
    return _pointer([{"type": "pointerMove", "duration": 0, "x": round(x0), "y": round(y0)},
                     {"type": "pointerDown", "button": 0},
                     {"type": "pause", "duration": 50},
                     {"type": "pointerMove", "duration": SCROLL_FAST_MS, "x": round(mx), "y": round(my)},
                     {"type": "pointerMove", "duration": SCROLL_EASE_MS, "x": round(x1), "y": round(y1)},
                     {"type": "pause", "duration": SCROLL_HOLD_MS},
                     {"type": "pointerUp", "button": 0}])


def path(points: list) -> dict:
    """W3C actions replaying a finger path: [(t_ms from 0, x, y), ...] in points, at its own pace."""
    _, x0, y0 = points[0]
    steps = [{"type": "pointerMove", "duration": 0, "x": round(x0), "y": round(y0)},
             {"type": "pointerDown", "button": 0}]
    prev = points[0][0]
    for t, x, y in points[1:]:
        steps.append({"type": "pointerMove", "duration": max(int(t - prev), 10), "x": round(x), "y": round(y)})
        prev = t
    steps.append({"type": "pointerUp", "button": 0})
    return _pointer(steps)


def scroll_points(direction: str, screen: tuple[float, float], amount: float = 0.5) -> tuple[float, ...]:
    """Swipe endpoints that scroll the content `direction` by `amount` of the screen.
    "down" shows what is below (the finger moves up)."""
    w, h = screen
    d = max(0.1, min(amount, 0.8))
    cx, cy = w / 2, h / 2
    if direction == "down":
        return cx, cy + h * d / 2, cx, cy - h * d / 2
    if direction == "up":
        return cx, cy - h * d / 2, cx, cy + h * d / 2
    if direction == "right":
        return cx + w * d / 2, cy, cx - w * d / 2, cy
    if direction == "left":
        return cx - w * d / 2, cy, cx + w * d / 2, cy
    raise Refused("bad_params", f"direction must be up, down, left or right, not {direction!r}")


SCROLL_USABLE = 0.84  # a scroll never spans more than this share of its area: the finger stays off the edges


def scroll_area_points(direction: str, rect: tuple[float, float, float, float], amount: float = 0.8,
                       band: tuple[float, float] | None = None) -> tuple[float, ...]:
    """Swipe endpoints that scroll the content of `rect` (x, y, w, h in points) by `amount` of it,
    centred in it and at most SCROLL_USABLE of it. "down" shows what is below (the finger moves up).
    `band` is the (top, bottom) y between the bars: `rect` is cut to it first, so a swipe never starts
    or ends on a navigation, tab or tool bar."""
    x, y, w, h = rect
    if band is not None:
        top, bottom = max(y, band[0]), min(y + h, band[1])
        if bottom > top:
            y, h = top, bottom - top
    cx, cy = x + w / 2, y + h / 2
    length = w if direction in ("left", "right") else h
    half = min(max(amount, 0.1) * length, length * SCROLL_USABLE) / 2
    if direction == "down":
        return cx, cy + half, cx, cy - half
    if direction == "up":
        return cx, cy - half, cx, cy + half
    if direction == "right":
        return cx + half, cy, cx - half, cy
    if direction == "left":
        return cx - half, cy, cx + half, cy
    raise ValueError(f"direction must be up, down, left or right, not {direction!r}")


SCROLL_MARGIN = 8.0  # points an element is brought past the edge it came in over, so it isn't flush with a bar
SCROLL_MIN_DRAG = 20.0  # a shorter drag may land as a tap


def scroll_move(element: tuple[float, float, float, float], view: tuple[float, float, float, float],
                margin: float = SCROLL_MARGIN) -> tuple[float, float]:
    """How far the content must move (dx, dy; down and right are positive) for the element's rect to lie inside
    the view's: (0, 0) when it already does. An element larger than the view is brought to the view's start."""
    def along(lo: float, size: float, start: float, room: float) -> float:
        if size > room:
            return 0.0 if abs(lo - start) <= 1.0 else start - lo
        gap = min(margin, (room - size) / 2)
        if lo < start:
            return start - lo + gap
        if lo + size > start + room:
            return start + room - (lo + size) - gap
        return 0.0
    ex, ey, ew, eh = element
    vx, vy, vw, vh = view
    return along(ex, ew, vx, vw), along(ey, eh, vy, vh)


def scroll_step(move: tuple[float, float], view: tuple[float, float, float, float]) -> tuple[float, ...]:
    """The drag (x0, y0, x1, y1) that moves the content toward `move`, along the axis that needs more: the finger
    goes the way the content must, centred in the view, at most SCROLL_USABLE of it and at least SCROLL_MIN_DRAG."""
    vx, vy, vw, vh = view
    cx, cy = vx + vw / 2, vy + vh / 2
    dx, dy = move
    vertical = abs(dy) >= abs(dx)
    want, room = (dy, vh) if vertical else (dx, vw)
    length = min(max(abs(want), SCROLL_MIN_DRAG), room * SCROLL_USABLE)
    half = math.copysign(length, want) / 2
    return (cx, cy - half, cx, cy + half) if vertical else (cx - half, cy, cx + half, cy)


def scroll_holding(items: list[Element], element: Element, screen: tuple[float, float]) -> Element | None:
    """The scroll view that brings an element into view: the one around it; else, for an element above or below
    the screen, the largest scroll view in its column (the page; a carousel in that column isn't its parent);
    else, for one to the left or right, the smallest in its row. None when there is none."""
    cx, cy = element.center
    scrolls = [e for e in items if e.role == "scroll"]
    if not element.ref and 0 <= cx < screen[0] and 0 <= cy < screen[1]:
        # Not listed, though its centre is on the screen: a scroll view inside the screen cuts it off (a card
        # peeking in at a carousel's edge). That one holds it: the smallest whose row or column it lies in while
        # its centre is outside.
        def cuts(e: Element) -> bool:
            in_x, in_y = e.x <= cx <= e.x + e.width, e.y <= cy <= e.y + e.height
            return in_x != in_y
        cutting = [e for e in scrolls if cuts(e)]
        if cutting:
            return min(cutting, key=lambda e: e.width * e.height)
    inside = scroll_of(items, element)
    if inside is not None:
        return inside
    if 0 <= cx < screen[0]:
        column = [e for e in scrolls if e.x <= cx <= e.x + e.width]
        return max(column, key=lambda e: e.width * e.height, default=None)
    row = [e for e in scrolls if e.y <= cy <= e.y + e.height]
    return min(row, key=lambda e: e.width * e.height, default=None)


def scroll_container(items: list[Element], screen: tuple[float, float]) -> Element | None:
    """The innermost (smallest) `scroll` element whose rect holds the centre of the screen, else None."""
    return scroll_at(items, (screen[0] / 2, screen[1] / 2))


def scroll_at(items: list[Element], point: tuple[float, float]) -> Element | None:
    """The innermost (smallest) `scroll` element whose rect holds the point, else None."""
    x, y = point
    around = [e for e in items if e.role == "scroll" and e.x <= x <= e.x + e.width and e.y <= y <= e.y + e.height]
    return min(around, key=lambda e: e.width * e.height, default=None)


def scroll_of(items: list[Element], element: Element) -> Element | None:
    """The scroll view an element is, or else the smallest one around it; None when it isn't in any."""
    if element.role == "scroll":
        return element
    around = [e for e in items if e.role == "scroll" and _inside(element, e)]
    return min(around, key=lambda e: e.width * e.height, default=None)


def scroll_swipe(x: float, y: float, dx: float, dy: float, screen: tuple[float, float],
                 minimum: float = 20.0) -> tuple[float, ...]:
    """Swipe endpoints, in points, for a wheel scroll at x, y (0..1) by dx, dy (fractions of the
    screen, each at most 0.8). dy > 0 shows content further down, so the finger moves up.
    The finger keeps the full distance: near an edge the start moves inward instead of the
    end being cut short. A scroll shorter than `minimum` points is stretched to it, so it
    never lands as a tap."""
    w, h = screen

    def inside(v: float, size: float) -> float:
        return min(max(v, 1.0), size - 1.0)

    def place(start: float, move: float, size: float) -> tuple[float, float]:
        start = inside(start, size)
        end = start + move
        if end > size - 1.0:
            end = size - 1.0
            start = end - move
        elif end < 1.0:
            end = 1.0
            start = end - move
        return inside(start, size), inside(end, size)
    dx, dy = max(min(dx, 0.8), -0.8), max(min(dy, 0.8), -0.8)
    mx, my = -dx * w, -dy * h
    length = math.hypot(mx, my)
    if length == 0:
        raise ValueError("a scroll needs dx or dy")
    if length < minimum:
        mx, my = mx * minimum / length, my * minimum / length
    x0, x1 = place(x * w, mx, w)
    y0, y1 = place(y * h, my, h)
    return x0, y0, x1, y1

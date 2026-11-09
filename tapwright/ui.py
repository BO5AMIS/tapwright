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
from dataclasses import dataclass
from typing import NamedTuple
from xml.etree import ElementTree as ET

from .control import Refused
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
TEXT_LIMIT = 80
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
    """One line, at most `limit` characters, as a JSON string."""
    return json.dumps(" ".join(text.split())[:limit], ensure_ascii=False)


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


BARS = {"NavigationBar", "StatusBar", "Toolbar", "TabBar"}  # what iOS draws over the content's edges
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
    """Is `child` only the parent listed a second time? Only when nothing would be lost: the same role,
    text, state and rect (within a point), no identifier the parent lacks, no action it lacks. Elements
    without text are never collapsed."""
    return bool(child.text and (parent.role, parent.text, parent.value, parent.checked, parent.selected,
                                parent.enabled) == (child.role, child.text, child.value, child.checked,
                                                    child.selected, child.enabled)
                and child.identifier in ("", parent.identifier) and set(child.actions) <= set(parent.actions)
                and _same_rect(parent.rect, child.rect))


def _clean(source: str) -> str:
    """The XML without characters XML 1.0 forbids."""
    def drop(m: re.Match) -> str:
        code = int(m.group(1), 16) if m.group(1) else int(m.group(2))
        return "" if code < 0x20 and code not in (9, 10, 13) else m.group(0)
    return _CONTROL_REFS.sub(drop, _RAW_CONTROLS.sub("", source))


def _kind(node: ET.Element) -> str:
    return _text(node.get("type") or node.tag).removeprefix("XCUIElementType")


def _alert(node: ET.Element) -> Alert:
    buttons = [_text(n.get("label")) or _text(n.get("name")) for n in node.iter() if _kind(n) == "Button"]
    return Alert(_text(node.get("label")) or _text(node.get("name")), tuple(b for b in buttons if b), _rect(node))


def read(source: str, screen: tuple[float, float] | None = None, keyboard: bool = False) -> Tree:
    """Flatten WDA's XML tree into the elements worth showing, in reading order, numbered e1...
    Skips what has no size or whose centre (where a tap would land) is clipped by the screen or a
    scroll container, and the keyboard's keys unless `keyboard`. Raises DeviceError on a broken tree."""
    tree = Tree([])
    bars: list[tuple[bool, float]] = []  # (at the top?, how far it reaches in) of each bar seen

    def walk(node: ET.Element, clip: tuple[float, float, float, float], in_keyboard: bool, in_tab_bar: bool,
             in_alert: bool, parent: Element | None = None) -> None:
        kind = _kind(node)
        x, y, w, h = rect = _rect(node)
        label, name, value = _text(node.get("label")), _text(node.get("name")), _text(node.get("value"))
        traits = {t.strip() for t in (node.get("traits") or "").split(",")}
        left, top, right, bottom = clip
        if w > 0 and h > 0 and (x + w <= left or y + h <= top or x >= right or y >= bottom):
            return  # nothing in this subtree is on screen
        cx, cy = x + w / 2, y + h / 2
        visible = w > 0 and h > 0 and left <= cx < right and top <= cy < bottom
        if kind == "Alert" and visible and tree.alert is None:
            tree.alert = _alert(node)
        if kind == "Keyboard" and visible:
            tree.keyboard = True
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
        listed = parent  # the nearest listed ancestor, for what this node's children are compared with
        if visible and worth and (keyboard or not in_keyboard):
            enabled = node.get("enabled", "true") != "false" and "NotEnabled" not in traits
            checked = (value == "1") if role == "switch" else None
            candidate = Element(f"e{len(tree.items) + 1}", role, label, identifier, value, rect, enabled,
                                "Selected" in traits, checked, ROLE_ACTIONS.get(role, ()), kind, in_alert, False, name,
                                kind == "SecureTextField")
            if parent and _duplicate(parent, candidate):
                pass  # the same thing as its parent, exposed twice (React Native does): list it once
            elif len(tree.items) < MAX_ELEMENTS:
                listed = candidate
                tree.items.append(listed)
            else:
                tree.more += 1
        if kind in CLIPPING:
            clip = (max(left, x), max(top, y), min(right, x + w), min(bottom, y + h))
        for child in node:
            walk(child, clip, in_keyboard, in_tab_bar or kind == "TabBar", in_alert, listed)

    inf = math.inf
    try:
        root = ET.fromstring(_clean(source))
    except ET.ParseError as e:
        raise DeviceError("WebDriverAgent sent an unreadable element tree") from e
    walk(root, (0.0, 0.0, screen[0], screen[1]) if screen else (-inf, -inf, inf, inf), False, False, False)
    for top, reach in ([] if tree.alert else bars):  # bars behind an alert aren't what a swipe would meet
        if top:
            tree.top_inset = max(tree.top_inset or 0.0, reach)
        else:
            tree.bottom_inset = max(tree.bottom_inset or 0.0, reach)
    return tree


def elements(source: str, screen: tuple[float, float] | None = None, keyboard: bool = False) -> list[Element]:
    return read(source, screen, keyboard).items


def alert_line(alert: Alert) -> str:
    return f"Alert: {json.dumps(alert.title, ensure_ascii=False)} " + " ".join(f"[{b}]" for b in alert.buttons)


def describe(items: list[Element], screen: tuple[float, float], scale: float | None = None,
             app: tuple[str, str] | None = None, alert: Alert | None = None, keyboard: bool = False,
             more: int = 0) -> str:
    """The full list (docs/AGENT-TOOLS.md §3.2): the header lines, then one line per element.
    `app` is (name, bundle id)."""
    size = f"{round(screen[0])}x{round(screen[1])} pt" + (f" @{scale:g}x" if scale else "")
    lines = [f"Screen {size} · {len(items)} elements"]
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
        if not items:
            raise Refused("not_found", f"no {target.ref}: you haven't looked yet; look first")
        raise Refused("not_found", f"{target.ref} is no longer on the screen; look again")
    if target.id is not None:
        # a real identifier first; WebDriverAgent's raw name (it mirrors the label) only if none match
        hits = [e for e in items if e.identifier == target.id] or [e for e in items if e.name == target.id]
        what, hint = f'id "{target.id}"', 'look for its id, or use {"label": …} or {"text": …}'
    elif target.label is not None:
        hits = _without_nested([e for e in items if _norm(e.label) == _norm(target.label)])
        what, hint = f'label "{target.label}"', 'use {"text": …} for a partial match, or look'
    else:
        want = _norm(target.text)
        hits = [e for e in items if _norm(e.label) == want or _norm(e.value) == want]
        if not hits:
            hits = [e for e in items if want in _norm(e.label) or want in _norm(e.value)]
        hits = _without_nested(hits)
        what, hint = f'text "{target.text}"', "scroll if it may be off screen, or look"
    if not hits:
        raise Refused("not_found", f"nothing on screen has {what}; {hint}")
    if target.index is not None:
        if target.index >= len(hits):
            raise Refused("not_found", f"index {target.index} is out of range: only {len(hits)} "
                                       f"element{'s' if len(hits) != 1 else ''} match {what}")
        return hits[target.index]
    if len(hits) > 1:
        shown = hits[:MAX_CANDIDATES]
        raise Refused("ambiguous", f"{len(hits)} elements match {what}"
                      + (f", showing {len(shown)}" if len(hits) > len(shown) else "")
                      + "; use one of their refs, or add index (0 is the first). Candidates:\n"
                      + "\n".join([*(e.line() for e in shown), *more_candidates(len(hits), len(shown))]))
    return hits[0]


class Back(NamedTuple):
    bar: bool  # a NavigationBar is on screen
    point: tuple[float, float] | None  # the centre of its back button, if it has one


# Buttons that sit at a bar's left edge but do something other than go back.
NOT_BACK = {"cancel", "close", "done", "save", "edit", "delete", "clear", "discard", "menu", "sign out"}


def back_button(source: str, screen: tuple[float, float]) -> Back:
    """Where to tap to go back, from WebDriverAgent's XML. NavigationBar is a container, so it isn't in
    the element list. In the top bar, in order: a Button named `BackButton` or labelled "Back"; else the
    leftmost Button in the bar's left quarter, unless it is Cancel, Done, Menu or another button that
    does something else; else no point (a bar without a back button is usually a root screen)."""
    try:
        root = ET.fromstring(_clean(source))
    except ET.ParseError as e:
        raise DeviceError("WebDriverAgent sent an unreadable element tree") from e

    def on_screen(rect: tuple[float, float, float, float]) -> bool:
        x, y, w, h = rect
        return w > 0 and h > 0 and 0 <= x + w / 2 < screen[0] and 0 <= y + h / 2 < screen[1]

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


def at_point(items: list[Element], point: tuple[float, float], role: str) -> Element | None:
    """The front-most listed element of `role` whose rect contains the point (a later element in
    reading order is drawn over an earlier one), or None. The far edge is outside, so two fields that
    share an edge don't both claim it. For tools that act on an element, not a touch."""
    x, y = point
    for e in reversed(items):
        if e.role == role and e.x <= x < e.x + e.width and e.y <= y < e.y + e.height:
            return e
    return None


def _pointer(steps: list[dict]) -> dict:
    return {"actions": [{"type": "pointer", "id": "finger", "parameters": {"pointerType": "touch"}, "actions": steps}]}


def tap(x: float, y: float, hold_ms: int = 60) -> dict:
    """W3C actions for a tap (or a long press with a longer hold)."""
    return _pointer([{"type": "pointerMove", "duration": 0, "x": round(x), "y": round(y)},
                     {"type": "pointerDown", "button": 0},
                     {"type": "pause", "duration": hold_ms},
                     {"type": "pointerUp", "button": 0}])


def swipe(x0: float, y0: float, x1: float, y1: float, duration_ms: int = 350) -> dict:
    """W3C actions for a drag from one point to another."""
    return _pointer([{"type": "pointerMove", "duration": 0, "x": round(x0), "y": round(y0)},
                     {"type": "pointerDown", "button": 0},
                     {"type": "pause", "duration": 50},
                     {"type": "pointerMove", "duration": duration_ms, "x": round(x1), "y": round(y1)},
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

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
ROLES = {"Button": "button", "Link": "link", "StaticText": "text",
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


def _rect(node: ET.Element) -> tuple[float, float, float, float]:
    """The node's frame; anything unreadable (not a number, inf, nan) makes it zero-size."""
    try:
        rect = tuple(float(node.get(key, 0)) for key in ("x", "y", "width", "height"))
    except ValueError:
        return 0.0, 0.0, 0.0, 0.0
    return rect if all(math.isfinite(v) for v in rect) else (0.0, 0.0, 0.0, 0.0)  # type: ignore[return-value]


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

    def walk(node: ET.Element, clip: tuple[float, float, float, float], in_keyboard: bool, in_tab_bar: bool,
             in_alert: bool) -> None:
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
        if visible and worth and (keyboard or not in_keyboard):
            if len(tree.items) < MAX_ELEMENTS:
                enabled = node.get("enabled", "true") != "false" and "NotEnabled" not in traits
                checked = (value == "1") if role == "switch" else None
                tree.items.append(Element(f"e{len(tree.items) + 1}", role, label, identifier, value, rect, enabled,
                                          "Selected" in traits, checked, ROLE_ACTIONS.get(role, ()), kind,
                                          in_alert, False, name))
            else:
                tree.more += 1
        if kind in CLIPPING:
            clip = (max(left, x), max(top, y), min(right, x + w), min(bottom, y + h))
        for child in node:
            walk(child, clip, in_keyboard, in_tab_bar or kind == "TabBar", in_alert)

    inf = math.inf
    try:
        root = ET.fromstring(_clean(source))
    except ET.ParseError as e:
        raise DeviceError("WebDriverAgent sent an unreadable element tree") from e
    walk(root, (0.0, 0.0, screen[0], screen[1]) if screen else (-inf, -inf, inf, inf), False, False, False)
    return tree


def elements(source: str, screen: tuple[float, float] | None = None, keyboard: bool = False) -> list[Element]:
    return read(source, screen, keyboard).items


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
        lines.append(f"Alert: {json.dumps(alert.title, ensure_ascii=False)} "
                     + " ".join(f"[{b}]" for b in alert.buttons))
    if keyboard:
        lines.append("Keyboard: shown")
    lines.extend(e.line() for e in items)
    if more:
        lines.append(f"(+{more} more below; scroll)")
    if not items:
        lines.append("(nothing visible; the app may still be loading)")
    return "\n".join(lines)


class NotFound(Exception):
    pass


def find(items: list[Element], ref: str | None = None, label: str | None = None, id: str | None = None,
         index: int = 0) -> Element:
    """An element by ref (from the last look), accessibility id, or label/value text:
    exact match first, then case-insensitive substring. `index` picks among several."""
    if ref:
        for e in items:
            if e.ref == ref:
                return e
        raise NotFound(f"no {ref} on the last screen; look again")
    if id:
        hits = [e for e in items if id in (e.name, e.identifier)]
    elif label:
        want = label.strip().lower()
        hits = [e for e in items if e.label.lower() == want or e.value.lower() == want]
        if not hits:
            hits = [e for e in items if want in e.label.lower() or want in e.value.lower()]
    else:
        raise Refused("bad_params", "give a ref, an id, or a label")
    if not hits:
        raise NotFound(f"nothing matches {'id=' + id if id else repr(label)} on the screen")
    if index >= len(hits):
        raise NotFound(f"only {len(hits)} match; index {index} is out of range")
    # Prefer enabled, actionable matches when the agent didn't pick one.
    if index == 0 and len(hits) > 1:
        hits.sort(key=lambda e: (not e.enabled, e.type not in INTERACTIVE))
    return hits[index]


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

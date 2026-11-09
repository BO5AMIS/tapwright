"""The screen as an agent reads it, and the gestures it asks for. Pure functions over
WebDriverAgent's accessibility tree (`/source?format=json`), so they're testable
without a phone.

An element line looks like:
    e12 Button "Log in" id=login-button @24,610 354x48
Positions and sizes are in points (the phone's coordinate space, what taps use).
"""
import math
from dataclasses import dataclass

# Element types worth listing even without a label (things an agent can act on).
INTERACTIVE = {"Button", "TextField", "SecureTextField", "TextView", "SearchField", "Switch", "Slider",
               "Cell", "Link", "Tab", "TabBar", "SegmentedControl", "PickerWheel", "Stepper", "Toggle",
               "MenuItem", "Image", "Icon", "Key"}
# Containers whose own label repeats their children's text: listed only with an identifier.
CONTAINERS = {"Application", "Window", "Other", "Group", "ScrollView", "Table", "CollectionView",
              "NavigationBar", "Toolbar", "StatusBar", "Keyboard", "WebView", "Sheet", "Alert"}
MAX_ELEMENTS = 200


@dataclass
class Element:
    ref: str
    type: str
    label: str = ""
    name: str = ""  # accessibility identifier (testID in React Native, accessibilityIdentifier in UIKit)
    value: str = ""
    x: float = 0
    y: float = 0
    width: float = 0
    height: float = 0
    enabled: bool = True

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.width / 2, self.y + self.height / 2

    def line(self) -> str:
        parts = [self.ref, self.type]
        if self.label:
            parts.append(f'"{self.label[:120]}"')
        if self.name and self.name != self.label:
            parts.append(f"id={self.name}")
        if self.value and self.value != self.label:
            parts.append(f'value="{self.value[:80]}"')
        if not self.enabled:
            parts.append("disabled")
        parts.append(f"@{round(self.x)},{round(self.y)} {round(self.width)}x{round(self.height)}")
        return " ".join(parts)


def _truthy(v) -> bool:
    return v not in (False, 0, "0", "false", "False", None)


_BIDI_MARKS = dict.fromkeys(map(ord, "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"))


def _text(v) -> str:
    """The text, without the invisible direction marks iOS puts around numbers ("\u200e42")."""
    return "" if v is None else str(v).translate(_BIDI_MARKS).strip()


def elements(source: dict, screen: tuple[float, float] | None = None, keyboard: bool = False) -> list[Element]:
    """Flatten WDA's tree into the elements worth showing, in reading order, numbered e1...
    Skips invisible and off-screen ones, and the keyboard's keys unless `keyboard`."""
    out: list[Element] = []

    def walk(node: dict, in_keyboard: bool) -> None:
        kind = _text(node.get("type")).removeprefix("XCUIElementType")
        rect = node.get("rect") or {}
        x, y = float(rect.get("x", 0)), float(rect.get("y", 0))
        w, h = float(rect.get("width", 0)), float(rect.get("height", 0))
        label, name, value = _text(node.get("label")), _text(node.get("name")), _text(node.get("value"))
        visible = _truthy(node.get("isVisible", True)) and w > 0 and h > 0
        if screen and (x + w <= 0 or y + h <= 0 or x >= screen[0] or y >= screen[1]):
            visible = False
        in_keyboard = in_keyboard or kind == "Keyboard"
        if name == label:
            name_shown = ""  # WDA mirrors the label into name when there is no identifier
        else:
            name_shown = name
        worth = (kind in INTERACTIVE or label or value or name_shown) and not (kind in CONTAINERS and not name_shown)
        if visible and worth and (keyboard or not in_keyboard) and len(out) < MAX_ELEMENTS:
            out.append(Element(f"e{len(out) + 1}", kind, label, name_shown, value, x, y, w, h,
                               _truthy(node.get("isEnabled", True))))
        for child in node.get("children") or []:
            walk(child, in_keyboard)

    walk(source, False)
    return out


def describe(items: list[Element], screen: tuple[float, float]) -> str:
    head = f"screen {round(screen[0])}x{round(screen[1])} pt, {len(items)} elements"
    return "\n".join([head] + [e.line() for e in items])


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
        hits = [e for e in items if e.name == id]
    elif label:
        want = label.strip().lower()
        hits = [e for e in items if e.label.lower() == want or e.value.lower() == want]
        if not hits:
            hits = [e for e in items if want in e.label.lower() or want in e.value.lower()]
    else:
        raise ValueError("give a ref, an id, or a label")
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
    raise ValueError(f"direction must be up, down, left or right, not {direction!r}")


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

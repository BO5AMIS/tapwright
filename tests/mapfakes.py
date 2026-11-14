"""A fake phone whose screens are real accessibility trees (ui.read of XML), so the daemon can recognise places."""
from xml.etree import ElementTree as ET

from fakes import FakeSession

from tapwright import ui
from tapwright.session import Snapshot

APP = ("Example", "com.example.app")
SCREEN = (400.0, 800.0)


def screen(title: str, ids: tuple[str, ...] = (), rows: tuple[str, ...] = (), tab: str | None = None, alert: str | None = None,
           texts: tuple[str, ...] = (), inputs: tuple[str, ...] = (), bundle: str = APP[1], secure: tuple[str, ...] = (), back: str | None = None,
           labels: dict[str, str] | None = None) -> ui.Tree:
    """A screen: a navigation bar with `title`, a table of `rows` (cells without identifiers), buttons with these `ids`
    (their label is "Label of <id>"), static `texts`, a tab bar with `tab` selected, perhaps an alert."""
    def node(kind, **attrs):
        return ET.Element(f"XCUIElementType{kind}", {"type": f"XCUIElementType{kind}", "enabled": "true", **{k: str(v) for k, v in attrs.items()}})
    app = node("Application", bundleId=bundle, label=APP[0], x=0, y=0, width=400, height=800)
    bar = node("NavigationBar", name=title, x=0, y=40, width=400, height=60)
    bar.append(node("StaticText", label=title, name=title, value=title, x=10, y=50, width=200, height=30, traits="StaticText,Header"))
    if back:  # a native Back button: the leading button of the bar, labelled with the screen under it
        bar.append(node("Button", label=back, name=back, x=10, y=50, width=60, height=30))
    table = node("Table", name="body", x=0, y=110, width=400, height=500)
    for i, row in enumerate(rows):
        table.append(node("Cell", label=row, name=row, x=0, y=110 + 50 * i, width=400, height=48))
    app.extend([bar, table])
    for n, i in enumerate(ids):
        app.append(node("Button", label=(labels or {}).get(i, f"Label of {i}"), name=i, x=10, y=620 + 44 * n, width=200, height=40))
    for n, i in enumerate(inputs):
        app.append(node("TextField", label=f"Field {i}", name=i, value="", x=10, y=540 + 44 * n, width=300, height=40))
    for n, i in enumerate(secure):
        app.append(node("SecureTextField", label=f"Field {i}", name=i, value="", x=10, y=496 - 44 * n, width=300, height=40))
    for n, t in enumerate(texts):
        app.append(node("StaticText", label=t, name=t, value=t, x=10, y=700 + 20 * n, width=300, height=18, traits="StaticText"))
    if tab:
        bar2 = node("TabBar", x=0, y=740, width=400, height=60)
        for n, name in enumerate(("home", "settings")):
            bar2.append(node("Button", label=name.title(), name=f"tab-{name}", x=20 + 150 * n, y=745, width=120, height=50,
                             **({"traits": "Selected"} if name == tab else {})))
        app.append(bar2)
    if alert:
        box = node("Alert", label=alert, x=50, y=250, width=300, height=200)
        box.append(node("Button", label="OK", name="OK", x=60, y=400, width=280, height=40))
        app.append(box)
    return ui.read(ET.tostring(app, encoding="unicode"), SCREEN)


class MapSession(FakeSession):
    """`screens` by name; `at` is the one on the phone. A tap (or back, or open_url) on an element moves to `goes[(screen,
    id-or-label)]`; everything else stays where it is."""

    def __init__(self, screens: dict[str, ui.Tree], start: str, goes: dict | None = None) -> None:
        super().__init__()
        self.screens, self.at, self.goes = screens, start, dict(goes or {})
        self.last_tree: ui.Tree | None = None
        self.foreground = APP[1]

    def _show(self) -> ui.Tree:
        tree = self.screens[self.at]
        self.items, self.last_tree = list(tree.items), tree
        self.last_insets = (tree.top_inset, tree.bottom_inset)
        self.last_alert, self.last_keyboard = tree.alert, tree.keyboard
        return tree

    async def look(self, screenshot=True):
        self._record("look")
        tree = self._show()
        text = ui.describe(tree.items, self.screen, 3.0, self.app, tree.alert, tree.keyboard, 0, (0, 0))
        return text, (b"JPEG" if screenshot else None)

    async def snapshot(self, keyboard=False, timeout=None):
        self._record("snapshot")
        return Snapshot(self._show(), self.app, 3.0)

    async def refresh(self, timeout=None):
        self._record("refresh")
        return self._show()

    def _go(self, key) -> None:
        if (self.at, key) in self.goes:
            self.at = self.goes[(self.at, key)]

    async def tap(self, target, hold_ms=60):
        hit = ui.resolve(self.items, target)
        text = await super().tap(target, hold_ms)
        self._go(f"point:{round(hit[0])},{round(hit[1])}" if isinstance(hit, tuple) else hit.identifier or hit.label)
        return text

    async def swipe(self, x0, y0, x1, y1, duration_ms=350, hold_ms=0):
        text = await super().swipe(x0, y0, x1, y1, duration_ms, hold_ms)
        self._go("swipe")
        return text

    async def open_url(self, url):
        text = await super().open_url(url)
        self._go(url)
        return text

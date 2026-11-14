"""A fake phone for the explorer (tapwright/explore.py): screens are real accessibility trees (mapfakes.screen), a tap moves
between them by a table, and everything the explorer asks the phone is recorded, so a test can say what was touched, in
what order, and what was never touched."""
from xml.etree import ElementTree as ET

from mapfakes import APP, SCREEN, screen

from tapwright import explore, mapkeeper, policy, ui
from tapwright.explore import Check, CrashFacts, Moved, PhoneStop, Watch

OTHER = "com.apple.mobilesafari"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class FakePhone:
    """`screens` by name; `goes[(screen, key)]` is where a tap on the element with that id (else label) leads. `back_to` is
    where `back` leads from a screen. A screen built with another bundle is another app in front."""

    def __init__(self, screens: dict[str, ui.Tree], start: str, goes: dict | None = None, back_to: dict | None = None,
                 clock: Clock | None = None, per_move: float = 1.0) -> None:
        self.screens, self.at, self.start = screens, start, start
        self.goes, self.back_to = dict(goes or {}), dict(back_to or {})
        self.screen = SCREEN
        self.clock, self.per_move = clock or Clock(), per_move
        self.touched: list[tuple[str, str]] = []  # (screen, key) of every tap the phone received
        self.foreign_touched: list[tuple[str, str]] = []  # a tap while another app was in front: must stay empty
        self.backs = 0
        self.opens = 0
        self.urls: list[str] = []
        self.links: dict[str, str] = {}
        self.answered: list[str] = []
        self.alert_to: dict[str, str] = {}
        self.crashes: set[tuple[str, str]] = set()
        self.errors: dict[tuple[str, str], tuple[str, ...]] = {}
        self.refuse: dict[tuple[str, str], Moved] = {}
        self.slow: dict[tuple[str, str], float] = {}
        self.needs: dict[str, str] = {}  # key -> "needs_person" | "blocked" (what the dry run says)
        self.checked: list[str] = []
        self.events: list[tuple[str, str]] = []
        self.pause_after: int | None = None
        self.pause_reason = "paused"
        self.waited_s = 0.0
        self.waiting_at: int | None = None
        self.scrolls = 0
        self.fail_back: set[str] = set()
        self.reads = 0

    def _tick(self) -> None:
        self.clock.t += self.per_move

    async def read(self) -> ui.Tree:
        self.reads += 1
        return self.screens[self.at]

    def _element(self, target: ui.Target) -> ui.Element:
        found = ui.resolve(self.screens[self.at].items, target)
        assert isinstance(found, ui.Element), "the explorer touches an element, never a point"
        return found

    def _foreign(self) -> bool:
        tree = self.screens[self.at]
        return tree.app is None or tree.app[1] != APP[1]

    async def gate(self) -> None:
        if self.waiting_at is not None and len(self.touched) == self.waiting_at:
            self.waited_s += 100.0  # a person drove for a while: the explorer waited
            self.clock.t += 100.0
            self.waiting_at = None
        if self.pause_after is not None and len(self.touched) >= self.pause_after:
            raise PhoneStop(self.pause_reason)

    async def tap(self, target: ui.Target) -> Moved:
        el = self._element(target)
        key = el.identifier or el.label
        if self._foreign():
            self.foreign_touched.append((self.at, key))
        self.touched.append((self.at, key))
        self._tick()
        slot = (self.at, key)
        if slot in self.refuse:
            return self.refuse[slot]
        self.events.append(slot)
        if slot in self.goes:
            self.at = self.goes[slot]
        return Moved(settled=self.slow.get(slot, 0.4))

    async def back(self) -> Moved:
        self.backs += 1
        self._tick()
        if self.at in self.back_to and self.at not in self.fail_back:
            self.at = self.back_to[self.at]
        return Moved(settled=0.3)

    async def scroll(self) -> Moved:
        self.scrolls += 1
        self._tick()
        return Moved()

    async def scroll_to_start(self) -> Moved:
        self._tick()
        return Moved()

    async def open_app(self) -> Moved:
        self.opens += 1
        self._tick()
        self.at = self.start
        return Moved(settled=1.0)

    async def open_url(self, link: str) -> Moved:
        self.urls.append(link)
        self._tick()
        if link in self.links:
            self.at = self.links[link]
        return Moved()

    async def alert(self, button: str) -> Moved:
        self.answered.append(button)
        self._tick()
        self.at = self.alert_to.get(self.at, self.at)
        return Moved()

    async def check(self, target: ui.Target) -> Check:
        el = self._element(target)
        key = el.identifier or el.label
        self.checked.append(key)
        return Check(self.needs[key], "Delete") if key in self.needs else Check()

    def mark(self) -> int:
        return len(self.events)

    async def watch(self, mark: int) -> Watch:
        since = self.events[mark:]
        crash = next((CrashFacts("EXC_BAD_ACCESS", ("main", "App.boom")) for s in since if s in self.crashes), None)
        errors = tuple(line for s in since for line in self.errors.get(s, ()))
        return Watch(errors, crash)

    def waited(self) -> float:
        return self.waited_s


def standard() -> tuple[dict[str, ui.Tree], dict, dict]:
    """A small app: Home -> Settings -> Language, Home -> Trips (a list of three rows) -> a trip, Home -> About."""
    screens = {
        "home": screen("Home", ids=("go-settings", "go-trips", "go-about")),
        "settings": screen("Settings", ids=("go-language",), back="Home"),
        "language": screen("Language", ids=("pick-ar",), back="Settings"),
        "trips": screen("Trips", ids=("filter-trips",), rows=("Trip A", "Trip B", "Trip C"), back="Home"),
        "trip": screen("Detail", ids=("trip-notes",), back="Trips"),
        "about": screen("About", ids=("about-version",), back="Home"),
    }
    goes = {("home", "go-settings"): "settings", ("home", "go-trips"): "trips", ("home", "go-about"): "about",
            ("settings", "go-language"): "language", ("trips", "Trip A"): "trip", ("trips", "Trip B"): "trip"}
    back_to = {"settings": "home", "language": "settings", "trips": "home", "trip": "trips", "about": "home"}
    return screens, goes, back_to


def identity(**more) -> explore.Identity:
    from tapwright import findings
    return explore.Identity(APP[1], "1.5.0", "1.5.0 (220)", findings.Device("iPhone 14", "26.5"), "1.8.0", **more)


def mapsport(tmp_path, app: str = APP[1]) -> tuple[mapkeeper.Maps, explore.MapsPort]:
    maps = mapkeeper.Maps(str(tmp_path))
    return maps, explore.MapsPort(maps, str(tmp_path), app)


def build(tmp_path, phone: FakePhone, rules: policy.Policy = policy.DEFAULT, who: explore.Identity | None = None, port=None,
          **options) -> tuple[explore.Explorer, mapkeeper.Maps]:
    maps, own = mapsport(tmp_path)
    return explore.Explorer(phone, port or own, rules, who or identity(), clock=phone.clock, **options), maps


def grid(title: str, buttons: tuple[tuple[str, str], ...] = (), inputs: tuple[str, ...] = (), secure: tuple[str, ...] = (),
         switches: tuple[tuple[str, str], ...] = (), cells: tuple[tuple[str, tuple[str, ...]], ...] = (), back: str | None = None,
         bundle: str = APP[1], alert: tuple[str, tuple[str, ...]] | None = None, others: tuple[str, ...] = (),
         headers: tuple[str, ...] = (), popover: bool = False) -> ui.Tree:
    """A screen of many controls in two columns: `buttons` as (id, label) (an empty label is an icon-only button), text
    `inputs` and `secure` fields, `switches` as (id, label), `cells` as (row label, nested button labels), a native Back, an
    alert as (title, buttons), `others` (named Other elements, for a system sheet) and `headers` (static texts with the Header trait)."""
    def node(kind, **attrs):
        return ET.Element(f"XCUIElementType{kind}", {"type": f"XCUIElementType{kind}", "enabled": "true", **{k: str(v) for k, v in attrs.items()}})
    app = node("Application", bundleId=bundle, label=APP[0], x=0, y=0, width=400, height=800)
    bar = node("NavigationBar", name=title, x=0, y=40, width=400, height=60)
    bar.append(node("StaticText", label=title, name=title, value=title, x=10, y=50, width=200, height=30, traits="StaticText,Header"))
    if back:
        bar.append(node("Button", label=back, name=back, x=10, y=50, width=60, height=30))
    app.append(bar)
    n = 0

    def place() -> tuple[int, int]:
        nonlocal n
        n += 1
        return 10 + 200 * ((n - 1) % 2), 110 + 44 * ((n - 1) // 2)
    for ident, label in buttons:
        x, y = place()
        app.append(node("Button", label=label, name=ident, x=x, y=y, width=180, height=40))
    for ident in inputs:
        x, y = place()
        app.append(node("TextField", label=f"Field {ident}", name=ident, value="", x=x, y=y, width=180, height=40))
    for ident in secure:
        x, y = place()
        app.append(node("SecureTextField", label=f"Field {ident}", name=ident, value="", x=x, y=y, width=180, height=40))
    for ident, label in switches:
        x, y = place()
        app.append(node("Switch", label=label, name=ident, value="0", x=x, y=y, width=180, height=40))
    for label, nested in cells:
        x, y = place()
        cell = node("Cell", label=label, name=label, x=x, y=y, width=180, height=40)
        for k, inner in enumerate(nested):
            cell.append(node("Button", label=inner, name=inner, x=x + 4, y=y + 2, width=60, height=30))
        app.append(cell)
    for name in others:
        x, y = place()
        app.append(node("Other", name=name, label=name, x=x, y=y, width=180, height=40))
    for text in headers:
        x, y = place()
        app.append(node("StaticText", label=text, name=text, value=text, x=x, y=y, width=180, height=30, traits="StaticText,Header"))
    if popover:  # how iOS 26 draws a share sheet: a Popover over a PopoverDismissRegion
        app.append(node("Other", name="PopoverDismissRegion", x=0, y=0, width=400, height=800))
        sheet = node("Popover", x=20, y=450, width=360, height=330)
        sheet.append(node("Button", label="Copy", name="Copy", x=30, y=460, width=100, height=40))
        app.append(sheet)
    if alert:
        box = node("Alert", label=alert[0], x=50, y=250, width=300, height=200)
        for k, name in enumerate(alert[1]):
            box.append(node("Button", label=name, name=name, x=60, y=330 + 44 * k, width=280, height=40))
        app.append(box)
    return ui.read(ET.tostring(app, encoding="unicode"), SCREEN)

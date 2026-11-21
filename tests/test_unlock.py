"""The passcode unlock (AGENT-TOOLS §18.6, §11.5, §24.5; unlock.py). With fakes: no phone. The passcode is typed only
when the lock screen's passcode entry is proven in SpringBoard's tree, never into an app or the XCTest prompt, at most
twice an hour when it doesn't unlock (1.10.0: one that unlocks isn't limited), never again after a refusal, and it never appears in what comes back. All values are synthetic."""
import asyncio

import pytest

from tapwright import ui, unlock

PASSCODE = "482913"  # synthetic
SB = unlock.SPRINGBOARD


def window(name: str = "SBCoverSheetWindow") -> ui.Element:
    """A window of SpringBoard's tree, as the runner lists it (the lock screen's is the cover sheet)."""
    return ui.Element(f"w-{name}", "window", "", name, rect=(0.0, 0.0, 430.0, 932.0), type="Window", name=name)


COVER = "SBCoverSheetWindow"


def keypad(labels=True, skip: str = "", twice: str = "", extra=(), cover=True, keys_in: str | None = COVER) -> list[ui.Element]:
    """The lock screen's keypad as SpringBoard's tree shows it: ten buttons, `2, ABC` style labels, in the cover sheet's
    window (1.10.0: find_entry requires it; cover=False leaves the window element out). `keys_in`: the raw window the keys
    lie in, as ui.read marks it (Element.window; None: not known)."""
    letters = {"2": "ABC", "3": "DEF", "4": "GHI", "5": "JKL", "6": "MNO", "7": "PQRS", "8": "TUV", "9": "WXYZ"}
    items = []
    for i, d in enumerate("1234567890"):
        if d in skip:
            continue
        label = f"{d}, {letters[d]}" if labels and d in letters else d
        rect = (40.0 + (i % 3) * 110, 300.0 + (i // 3) * 90, 80.0, 80.0)
        items.append(ui.Element(f"e{i + 1}", "button", label, rect=rect, actions=("tap",), type="Button", window=keys_in))
        if d in twice:
            items.append(ui.Element(f"x{i + 1}", "button", label, rect=(5, 5, 20, 20), actions=("tap",), type="Button",
                                    window=keys_in))
    return [*([window()] if cover else []), *items, *extra]


SCREEN = (430.0, 932.0)  # the phone's screen in points (iPhone15,3)


class Screen:
    """A fake lock screen. Keys pressed are mapped back to digits; the right passcode unlocks it."""

    def __init__(self, items=None, front=SB, xctest=False, locked=True, right=PASSCODE, alert=None,
                 locks_again=False):
        self.items = keypad() if items is None else items
        self.front, self.xctest, self.is_locked, self.right, self.alert = front, xctest, locked, right, alert
        self.locks_again = locks_again
        self.pressed: list[str] = []  # each attempt's digits
        self.secrets: list[str] = []
        self.calls: list[str] = []

    async def locked(self):
        return self.is_locked

    async def wake(self):
        self.calls.append("wake")

    async def reveal(self):
        self.calls.append("reveal")

    async def lock_tree(self):
        self.calls.append("tree")
        return ui.Tree(list(self.items), alert=self.alert), self.front

    async def xctest_prompt(self):
        return self.xctest

    def screen_size(self):
        return SCREEN

    async def press_keys(self, points):
        centres = {e.center: e.label[0] for e in self.items if e.label}
        typed = "".join(centres[p] for p in points)
        self.pressed.append(typed)
        if typed == self.right:
            self.is_locked = False

    async def type_secret(self, text):
        self.secrets.append(text)
        if text == self.right:
            self.is_locked = False


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.t += s


def unlocker(clock=None, path=None):
    """An Unlocker whose record of attempts (unlock.Ledger, on disk: the test's own, conftest.py) runs on the fake clock."""
    clock = clock or Clock()
    return unlock.Unlocker(clock=clock, sleep=clock.sleep, confirm_wait=1.0, poll=0.2,
                           ledger=unlock.Ledger(path, wall=clock)), clock


def run(coro):
    return asyncio.run(coro)


def test_the_right_passcode_on_a_proven_lock_screen_unlocks_and_is_audited():
    u, _ = unlocker()
    screen, audited = Screen(), []
    r = run(u.unlock(screen, PASSCODE, audited.append))
    assert (r.ok, r.code) == (True, "unlocked") and screen.pressed == [PASSCODE] and not screen.is_locked
    assert screen.calls[0] == "wake" and audited == ["typed on the lock screen; unlocked"]
    assert PASSCODE not in repr(r) and PASSCODE not in " ".join(audited)


def test_an_unlocked_phone_types_nothing():
    u, _ = unlocker()
    screen = Screen(locked=False)
    r = run(u.unlock(screen, PASSCODE))
    assert (r.ok, r.code) == (True, "not_locked") and screen.pressed == [] and screen.calls == []


def test_no_passcode_types_nothing():
    u, _ = unlocker()
    screen = Screen()
    r = run(u.unlock(screen, None))
    assert (r.ok, r.code, r.message) == (False, "no_passcode", unlock.NO_PASSCODE) and screen.pressed == []


@pytest.mark.parametrize("front", ["com.example.calculator", "com.apple.mobilesafari", None])
def test_never_into_an_app_even_one_showing_a_keypad(front):
    u, _ = unlocker()
    screen = Screen(front=front)  # a calculator-like app with ten digit buttons
    r = run(u.unlock(screen, PASSCODE))
    assert (r.ok, r.code) == (False, "not_proven") and screen.pressed == [] and screen.secrets == []
    assert "reveal" in screen.calls  # it looked again after the swipe up, and still found no lock screen


@pytest.mark.parametrize("items", [
    keypad(skip="7"),  # a key missing: not the keypad
    keypad(twice="5"),  # a digit twice: which one is the key isn't certain
    [],  # nothing
    keypad(extra=(ui.Element("z", "text", "Enter iPhone Passcode for \"XCTest\""),)),  # the XCTest prompt's words
], ids=["missing-key", "key-twice", "empty", "xctest-words"])
def test_nothing_is_typed_unless_the_whole_keypad_is_proven(items):
    u, _ = unlocker()
    screen = Screen(items=items)
    r = run(u.unlock(screen, PASSCODE))
    assert (r.ok, r.code) == (False, "not_proven") and screen.pressed == [] and screen.secrets == []
    assert u.tries_left() == 2  # nothing typed: no attempt counted


def test_an_alert_over_the_lock_screen_is_not_the_lock_screen():
    u, _ = unlocker()
    screen = Screen(alert=ui.Alert("Low Battery", ("OK",), (20.0, 300.0, 300.0, 150.0)))
    r = run(u.unlock(screen, PASSCODE))
    assert r.code == "not_proven" and screen.pressed == []


def test_the_xctest_prompt_never_gets_the_passcode():
    """§18.6: only the lock screen's keypad; §11.5 lists the XCTest prompt as a person's moment. So: `locked` for the
    owner, nothing typed, no attempt counted."""
    u, _ = unlocker()
    screen = Screen(xctest=True)
    r = run(u.unlock(screen, PASSCODE))
    assert (r.ok, r.code) == (False, "xctest_prompt") and screen.pressed == [] and screen.secrets == []
    assert "never types the passcode there" in r.message and u.tries_left() == 2


def test_a_refused_passcode_stops_and_is_never_typed_again():
    u, clock = unlocker()
    screen, audited = Screen(right="000000"), []
    r = run(u.unlock(screen, PASSCODE, audited.append))
    assert (r.ok, r.code, r.message) == (False, "refused", unlock.REFUSED) and screen.pressed == [PASSCODE]
    clock.t += 7200  # even hours later, the same passcode isn't tried again: iOS's lockout must never be reached
    again = run(u.unlock(screen, PASSCODE, audited.append))
    assert (again.code, again.message) == ("refused", unlock.REFUSED) and screen.pressed == [PASSCODE]
    assert audited == ["typed on the lock screen; refused"]


def test_a_changed_passcode_in_the_file_may_be_tried_after_a_refusal():
    """The refusal is tied to the vault file (path, modification time, size), never to the passcode: a changed file
    may be tried once."""
    u, _ = unlocker()
    screen = Screen(right="135790")
    assert run(u.unlock(screen, PASSCODE, source=unlock.Source("v.json", 1, 10))).code == "refused"
    assert run(u.unlock(screen, "135790", source=unlock.Source("v.json", 2, 10))).code == "unlocked"


def test_an_unlock_that_works_is_never_limited():
    """(1.10.0, the owner's decision) iOS's lockout counts only wrong passcodes: a phone left plugged in may lock and be
    unlocked as often as it likes. The wakes that type are not counted either."""
    u, clock = unlocker()
    screen = Screen()
    for _ in range(20):
        screen.is_locked = True
        assert run(u.unlock(screen, PASSCODE)).code == "unlocked"
        clock.t += 60
    assert len(screen.pressed) == 20 and u.tries_left() == 2


def _refused_then_reset(u, clock, screen) -> None:
    assert run(u.unlock(screen, PASSCODE)).code == "refused"
    assert run(u.ledger.reset())[0]  # a person checked the vault and cleared the refusal
    clock.t += 60


def test_at_most_two_failed_attempts_an_hour_even_after_a_reset():
    u, clock = unlocker()
    screen = Screen(right="000000")
    _refused_then_reset(u, clock, screen)
    _refused_then_reset(u, clock, screen)
    r = run(u.unlock(screen, PASSCODE))
    assert (r.ok, r.code) == (False, "limit") and len(screen.pressed) == 2 and r.message == unlock.LIMITED and "2 attempts this hour" in r.message
    clock.t += 3600
    screen.right = PASSCODE  # the phone's passcode is the file's again
    assert run(u.unlock(screen, PASSCODE)).code == "unlocked" and len(screen.pressed) == 3


def test_at_most_three_failed_attempts_a_day():
    u, clock = unlocker()
    screen = Screen(right="000000")
    for _ in range(3):
        _refused_then_reset(u, clock, screen)
        clock.t += 3600
    r = run(u.unlock(screen, PASSCODE))
    assert (r.ok, r.code) == (False, "limit") and len(screen.pressed) == 3 and r.message == unlock.LIMITED_DAY


def test_successes_between_failures_do_not_count():
    u, clock = unlocker()
    screen = Screen(right="000000")
    _refused_then_reset(u, clock, screen)
    screen.right = PASSCODE
    for _ in range(5):
        screen.is_locked = True
        assert run(u.unlock(screen, PASSCODE)).code == "unlocked"
    assert u.tries_left() == 1  # only the refused one counts


def test_a_typing_error_counts_as_a_refusal_and_never_echoes_what_was_sent():
    class Failing(Screen):
        async def press_keys(self, points):
            raise RuntimeError(f"WebDriverAgent: HTTP 500 for {PASSCODE}")
    u, _ = unlocker()
    screen = Failing()
    r = run(u.unlock(screen, PASSCODE))
    assert (r.ok, r.code, r.message) == (False, "failed", unlock.FAILED) and PASSCODE not in repr(r)
    assert run(u.unlock(screen, PASSCODE)).code == "refused"  # no second try with that passcode


def test_a_passcode_with_letters_goes_only_into_the_secure_passcode_field():
    field = ui.Element("p1", "input", "Passcode field", rect=(20, 200, 300, 40), secure=True, type="SecureTextField",
                       window=COVER)
    u, _ = unlocker()
    screen = Screen(items=[window(), field], right="abc-123x")
    r = run(u.unlock(screen, "abc-123x"))
    assert r.code == "unlocked" and screen.secrets == ["abc-123x"] and screen.pressed == []
    u2, _ = unlocker()
    screen2 = Screen(items=keypad(), right="abc-123x")  # a number keypad and a passcode with letters: nothing typed
    r2 = run(u2.unlock(screen2, "abc-123x"))
    assert r2.code == "not_proven" and screen2.secrets == [] and screen2.pressed == []


def test_a_phone_that_unlocks_on_waking_types_nothing():
    class Wakes(Screen):
        async def wake(self):
            self.is_locked = False
    u, _ = unlocker()
    screen = Wakes()
    assert run(u.unlock(screen, PASSCODE)).code == "not_locked" and screen.pressed == []


def test_find_entry_needs_springboard_and_the_keypad():
    tree = ui.Tree(keypad())
    assert unlock.find_entry(tree, SB) is not None
    assert unlock.find_entry(tree, "com.example.app") is None
    assert unlock.find_entry(ui.Tree(keypad(labels=False)), SB) is not None  # bare digit labels


# ---- iOS 26: the keypad listed off the screen (1.8.5) ----
# SpringBoard's tree on iPhone15,3 iOS 26.5 right after the wake (2026-11-19): the passcode view is on the screen, but
# under a container whose frame lies outside it, so every key is in the tree's off-screen list. (No notification text
# was on it; the clock reads a placeholder time.)
IOS26_ITEMS = [
    ("other", "Other", "jindo-container-view:1", "", (128, 11, 168, 37)),
    ("other", "Other", "regular.view", "Swipe up to unlock", (128, 11, 168, 37)),
    ("other", "Other", "proud-lock", "Swipe up to unlock", (128, 22, 13, 16)),
    ("window", "Window", "SBCoverSheetWindow", "", (0, 0, 430, 932)),
    ("text", "StaticText", "", "0:00 AM", (48, 18, 41, 23)),
    ("other", "Other", "2 of 3 Wi-Fi bars", "", (343, 21, 20, 15)),
]
IOS26_OFF = [
    ("button", "Button", "flashlight-orb-button", "Flashlight", (36, -134, 94, 94)),
    ("button", "Button", "system-orb-button-com.apple.camera.deeplink.button", "Camera", (300, -134, 94, 94)),
    ("text", "StaticText", "", "Enter Passcode", (137, 175, 156, 27)),
    *[("button", "Key", "", d, (47 + 112 * (i % 3), 317 + 108 * (i // 3), 112, 108)) for i, d in enumerate("123456789")],
    ("button", "Key", "", "0", (159, 641, 112, 108)),
    ("button", "Button", "", "Emergency", (47, 803, 112, 108)),
    ("text", "StaticText", "", "Emergency", (57, 846, 92, 22)),
    ("button", "Button", "", "Cancel", (271, 803, 112, 108)),
    ("text", "StaticText", "", "Cancel", (299, 846, 56, 22)),
]


def ios26(rows):
    return [ui.Element(f"e{i}", role, label, ident, rect=tuple(float(v) for v in rect), type=kind, name=ident or label,
                       window=COVER) for i, (role, kind, ident, label, rect) in enumerate(rows)]


def ios26_tree(off=None):
    field = ui.Element("f", "input", "Passcode field", rect=(0, 220, 430, 13), secure=True, type="SecureTextField",
                       name="Passcode field", window=COVER)
    return ui.Tree(ios26(IOS26_ITEMS), offscreen=[*ios26(IOS26_OFF if off is None else off), field],
                   app=(SB, SB))


def face(frame):
    """The key's round face, as the hit test answers on iOS 26: an unnamed Other inside the key's frame."""
    x, y, w, h = frame
    return ("", "", (x + 12, y + 10, w - 24, h - 20))


class Ios26(Screen):
    """The iOS 26 lock screen: keys in the off-screen list; the hit test lands on each key's unnamed face."""

    def __init__(self, hits=None, **kw):
        super().__init__(**kw)
        self.tree = ios26_tree()
        self.hits = hits  # (x, y) -> (name, label, rect), or None for the keys' faces
        self.probed: list[tuple[float, float]] = []

    async def lock_tree(self):
        self.calls.append("tree")
        return self.tree, self.front

    def _keys(self):
        return {e.center: e for e in self.tree.offscreen if e.type == "Key"}

    async def press_keys(self, points):
        keys = self._keys()
        typed = "".join(keys[p].label for p in points)
        self.pressed.append(typed)
        if typed == self.right:
            self.is_locked = False

    async def key_at(self, x, y, frames):
        self.probed.append((x, y))
        if self.hits is not None:
            said = self.hits.get((x, y))
        else:
            key = self._keys().get((x, y))
            said = face(key.rect) if key is not None else None
        return None if said is None else unlock.hit_key(*said, x, y, frames)


def test_ios26_keypad_listed_off_screen_is_found_but_marked_for_the_hit_test():
    entry = unlock.find_entry(ios26_tree(), SB, SCREEN)
    assert entry is not None and not entry.on_screen and sorted(entry.keys) == sorted(unlock.DIGITS)
    assert entry.keys["1"] == (103.0, 371.0) and entry.frames["0"] == (159.0, 641.0, 112.0, 108.0)
    assert unlock.find_entry(ios26_tree(), "com.example.app", SCREEN) is None
    on = unlock.find_entry(ui.Tree(keypad()), SB)
    assert on is not None and on.on_screen  # a keypad the tree lists on the screen needs no hit test


def test_ios26_unlocks_after_the_hit_test_confirms_the_keys():
    u, _ = unlocker()
    screen, audited = Ios26(), []
    r = run(u.unlock(screen, PASSCODE, audited.append))
    assert (r.ok, r.code) == (True, "unlocked") and screen.pressed == [PASSCODE]
    assert "reveal" not in screen.calls  # found after the wake: no swipe that could close it
    keys = {e.label: e.center for e in screen.tree.offscreen if e.type == "Key"}
    assert screen.probed == [keys[d] for d in unlock.PROBE_KEYS]  # the same keys every time, never the passcode's


@pytest.mark.parametrize("wrong", ["elsewhere", "other-key", "no-answer", "named-other", "raises"])
def test_ios26_keys_the_hit_test_does_not_confirm_get_nothing_typed(wrong):
    tree = ios26_tree()
    keys = {e.label: e for e in tree.offscreen if e.type == "Key"}
    hits = {e.center: face(e.rect) for e in keys.values()}
    one = keys["3"].center
    if wrong == "elsewhere":
        hits[one] = ("", "", (0, 0, 430, 932))  # a view over the whole screen: not inside one key
    elif wrong == "other-key":
        hits[one] = face(keys["2"].rect)  # lands on the face of another key
    elif wrong == "no-answer":
        hits[one] = None
    elif wrong == "named-other":
        hits[one] = ("Emergency", "Emergency", keys["3"].rect)

    class Wrong(Ios26):
        async def key_at(self, x, y, frames):
            if wrong == "raises":
                raise RuntimeError("hit test failed")
            return await super().key_at(x, y, frames)
    u, _ = unlocker()
    screen = Wrong(hits=hits)
    r = run(u.unlock(screen, PASSCODE))
    assert (r.ok, r.code) == (False, "not_proven") and screen.pressed == [] and u.tries_left() == 2


@pytest.mark.parametrize("change", ["key-twice", "key-missing", "above-screen", "xctest-words"])
def test_ios26_off_screen_keypad_still_needs_every_proof(change):
    off = list(IOS26_OFF)
    if change == "key-twice":
        off.append(("button", "Key", "", "5", (5, 5, 20, 20)))
    elif change == "key-missing":
        off = [row for row in off if row[3] != "7"]
    elif change == "above-screen":
        off = [(r[0], r[1], r[2], r[3], (r[4][0], r[4][1] - 1000, r[4][2], r[4][3])) if r[1] == "Key" else r
               for r in off]
    elif change == "xctest-words":
        off.append(("text", "StaticText", "", "Enter iPhone Passcode for \"XCTest\"", (20, 100, 390, 30)))
    tree = ios26_tree(off)
    entry = unlock.find_entry(tree, SB, SCREEN)
    assert entry is None or not entry.keys  # at most the secure field, which a digit passcode never goes into
    u, _ = unlocker()
    screen = Ios26()
    screen.tree = tree
    r = run(u.unlock(screen, PASSCODE))
    assert r.code == "not_proven" and screen.pressed == [] and screen.secrets == [] and screen.probed == []


def test_hit_key_reads_a_named_key_or_one_unnamed_face():
    frames = {"1": (47.0, 317.0, 112.0, 108.0), "2": (159.0, 317.0, 112.0, 108.0)}
    assert unlock.hit_key("", "", (59.0, 327.0, 88.0, 88.0), 103, 371, frames) == "1"  # seen on the phone
    assert unlock.hit_key("1", "1", frames["1"], 103, 371, frames) == "1"
    assert unlock.hit_key("2", "2, ABC", frames["2"], 103, 371, frames) is None  # names a key the point isn't on
    assert unlock.hit_key("", "", (59.0, 327.0, 88.0, 88.0), 300, 371, frames) is None  # the point isn't on the hit
    assert unlock.hit_key("", "", (40.0, 317.0, 240.0, 108.0), 103, 371, frames) is None  # spans two keys
    assert unlock.hit_key("Cancel", "Cancel", (59.0, 327.0, 88.0, 88.0), 103, 371, frames) is None


# ---- review R185b L1: an off-screen keypad's keys must lie on the glass, every edge ----

@pytest.mark.parametrize("change", ["past-right", "past-bottom", "no-size"])
def test_ios26_keys_past_the_right_or_bottom_edge_or_an_unknown_screen_get_nothing_typed(change):
    off = list(IOS26_OFF)
    if change == "past-right":
        off = [(r[0], r[1], r[2], r[3], (r[4][0] + 1000, r[4][1], r[4][2], r[4][3])) if r[1] == "Key" else r for r in off]
    elif change == "past-bottom":
        off = [(r[0], r[1], r[2], r[3], (r[4][0], r[4][1] + 2000, r[4][2], r[4][3])) if r[1] == "Key" else r for r in off]
    tree = ios26_tree(off)
    size = None if change == "no-size" else SCREEN
    entry = unlock.find_entry(tree, SB, size)
    assert entry is None or not entry.keys

    class Off(Ios26):
        async def key_at(self, x, y, frames):  # a runner whose hit test answers the off-screen key itself
            self.probed.append((x, y))
            key = self._keys().get((x, y))
            return key.label if key is not None else None

        def screen_size(self):
            return size
    u, _ = unlocker()
    screen = Off()
    screen.tree = tree
    r = run(u.unlock(screen, PASSCODE))
    assert r.code == "not_proven" and screen.pressed == [] and screen.probed == [] and u.tries_left() == 2


# ---- 1.10.0: SpringBoard's other passcode keypad (the lead's note, 2026-11-19 ~19:45) ----
# After Software Update > Later, iOS showed "Enter Passcode to Update Tonight": window SBTransientOverlayWindow, a
# "Passcode field", all ten digit keys and "Remind Me Later". Typing the passcode there schedules an iOS update. The tree
# below is that screen's, ids and labels only (frames are placeholders in the keypad's usual places).
def update_tonight_tree(cover: bool = False) -> ui.Tree:
    field = ui.Element("f", "input", "Passcode field", rect=(0.0, 220.0, 430.0, 13.0), secure=True,
                       type="SecureTextField", name="Passcode field")
    title = ui.Element("t", "text", "Enter Passcode to Update Tonight", rect=(40.0, 150.0, 350.0, 27.0),
                       type="StaticText", name="Enter Passcode to Update Tonight")
    later = ui.Element("l", "button", "Remind Me Later", rect=(120.0, 820.0, 190.0, 44.0), actions=("tap",),
                       type="Button", name="Remind Me Later")
    items = [*([window()] if cover else []), window("SBTransientOverlayWindow"), title, field,
             *keypad(cover=False, keys_in="SBTransientOverlayWindow"), later]
    return ui.Tree(items, app=(SB, SB))


def test_the_update_tonight_keypad_is_never_the_lock_screen():
    assert unlock.find_entry(update_tonight_tree(), SB, SCREEN) is None
    assert unlock.find_entry(update_tonight_tree(cover=True), SB, SCREEN) is None  # even over the cover sheet
    assert unlock.foreign_keypad(update_tonight_tree())


@pytest.mark.parametrize("what", ["overlay-window", "update-words", "remind-me-later"])
def test_each_sign_of_another_keypad_alone_refuses(what):
    extra = {"overlay-window": [window("SBTransientOverlayWindow")],
             "update-words": [ui.Element("t", "text", "Enter Passcode to Update Tonight", type="StaticText")],
             "remind-me-later": [ui.Element("b", "button", "Remind Me Later", type="Button")]}[what]
    assert unlock.find_entry(ui.Tree(keypad(extra=extra)), SB) is None


def test_a_keypad_without_the_lock_screen_window_is_not_the_lock_screen():
    assert unlock.find_entry(ui.Tree(keypad(cover=False)), SB) is None
    assert unlock.find_entry(ui.Tree(keypad()), SB) is not None


class UpdateTonight(Screen):
    def __init__(self, cover=False, **kw):
        super().__init__(**kw)
        self.tree = update_tonight_tree(cover)

    async def lock_tree(self):
        self.calls.append("tree")
        return self.tree, self.front


@pytest.mark.parametrize("cover", [False, True])
def test_unlock_never_types_on_the_update_tonight_screen(cover):
    u, _ = unlocker()
    screen, audited = UpdateTonight(cover=cover), []
    r = run(u.unlock(screen, PASSCODE, audited.append))
    assert (r.ok, r.code) == (False, "update_prompt") and "Remind Me Later" in r.message
    assert screen.pressed == [] and screen.secrets == [] and audited == [] and u.tries_left() == 2
    assert PASSCODE not in r.message


def test_an_update_keypad_that_comes_up_before_the_keys_stops_the_unlock():
    """The last check before the first key reads the screen again: the lock screen was proven, then the update keypad
    came up over it."""
    class Switching(Screen):
        reads = 0

        async def lock_tree(self):
            self.calls.append("tree")
            self.reads += 1
            if self.reads == 1:
                return ui.Tree(list(self.items)), self.front
            return update_tonight_tree(cover=True), self.front
    u, _ = unlocker()
    screen = Switching()
    r = run(u.unlock(screen, PASSCODE))
    assert r.code == "update_prompt" and screen.pressed == [] and u.tries_left() == 2

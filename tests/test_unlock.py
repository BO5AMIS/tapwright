"""The passcode unlock (AGENT-TOOLS §18.6, §11.5, §24.5; unlock.py). With fakes: no phone. The passcode is typed only
when the lock screen's passcode entry is proven in SpringBoard's tree, never into an app or the XCTest prompt, at most
twice an hour, never again after a refusal, and it never appears in what comes back. All values are synthetic."""
import asyncio

import pytest

from tapwright import ui, unlock

PASSCODE = "482913"  # synthetic
SB = unlock.SPRINGBOARD


def keypad(labels=True, skip: str = "", twice: str = "", extra=()) -> list[ui.Element]:
    """The lock screen's keypad as SpringBoard's tree shows it: ten buttons, `2, ABC` style labels."""
    letters = {"2": "ABC", "3": "DEF", "4": "GHI", "5": "JKL", "6": "MNO", "7": "PQRS", "8": "TUV", "9": "WXYZ"}
    items = []
    for i, d in enumerate("1234567890"):
        if d in skip:
            continue
        label = f"{d}, {letters[d]}" if labels and d in letters else d
        rect = (40.0 + (i % 3) * 110, 300.0 + (i // 3) * 90, 80.0, 80.0)
        items.append(ui.Element(f"e{i + 1}", "button", label, rect=rect, actions=("tap",), type="Button"))
        if d in twice:
            items.append(ui.Element(f"x{i + 1}", "button", label, rect=(5, 5, 20, 20), actions=("tap",), type="Button"))
    return [*items, *extra]


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

    async def press_keys(self, points):
        centres = {e.center: (e.label or "")[0] for e in self.items}
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


def unlocker(clock=None):
    clock = clock or Clock()
    return unlock.Unlocker(clock=clock, sleep=clock.sleep, confirm_wait=1.0, poll=0.2), clock


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
    u, _ = unlocker()
    screen = Screen(right="135790")
    assert run(u.unlock(screen, PASSCODE)).code == "refused"
    assert run(u.unlock(screen, "135790")).code == "unlocked"


def test_at_most_two_attempts_an_hour():
    u, clock = unlocker()
    screen = Screen()
    for _ in range(2):
        screen.is_locked = True
        assert run(u.unlock(screen, PASSCODE)).code == "unlocked"
        clock.t += 60
    screen.is_locked = True
    r = run(u.unlock(screen, PASSCODE))
    assert (r.ok, r.code) == (False, "limit") and len(screen.pressed) == 2 and "twice this hour" in r.message
    clock.t += 3600
    assert run(u.unlock(screen, PASSCODE)).code == "unlocked" and len(screen.pressed) == 3


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
    field = ui.Element("p1", "input", "Passcode field", rect=(20, 200, 300, 40), secure=True, type="SecureTextField")
    u, _ = unlocker()
    screen = Screen(items=[field], right="abc-123x")
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

"""CodeRabbit's first cycle on PR #23 (1.9.2) and the lead's phone check of `service install`. Fakes only: no phone, no
daemon, no Task Scheduler."""
import asyncio
import time

import pytest
from fakes import FakeSession
from test_daemon import finish, make, run, started
from test_service import Tasks, _svc
from test_unlock import PASSCODE, SB, Screen, keypad, unlocker

from tapwright import descriptions, service, ui, unlock
from tapwright.device import ConnectionLost, PhoneLocked, PhoneNotFound


# ---- 4636616273 (major): the keys typed are the final read's ----
class Moving(Screen):
    """The keypad moves between the first read and the last one before the keys."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.reads = 0
        self.moved = [ui.Element(e.ref, e.role, e.label, e.identifier, rect=(e.rect[0], e.rect[1] + 50, e.rect[2],
                                 e.rect[3]), actions=e.actions, type=e.type, name=e.name, window=e.window)
                      for e in self.items]

    async def lock_tree(self):
        self.calls.append("tree")
        self.reads += 1
        if self.reads > 1:
            self.items = self.moved  # (press_keys maps the touches on the keypad as it is now)
        return ui.Tree(list(self.items)), self.front


def test_a_keypad_that_moved_before_the_keys_is_typed_where_it_is_now():
    u, _ = unlocker()
    screen = Moving()
    r = asyncio.run(u.unlock(screen, PASSCODE))
    assert (r.ok, r.code) == (True, "unlocked") and screen.pressed == [PASSCODE] and screen.reads >= 2


def test_a_final_read_that_is_no_longer_usable_types_nothing():
    """The final read still shows the lock screen's window but no whole keypad any more: nothing is typed."""
    class Shrinking(Screen):
        reads = 0

        async def lock_tree(self):
            self.calls.append("tree")
            self.reads += 1
            return ui.Tree(keypad() if self.reads == 1 else keypad(skip="7")), self.front
    u, _ = unlocker()
    screen = Shrinking()
    r = asyncio.run(u.unlock(screen, PASSCODE))
    assert r.code == "not_proven" and screen.pressed == [] and u.tries_left() == 2


# ---- 4636616263: a notification about an update tonight is still the lock screen ----
@pytest.mark.parametrize("words", ["Install the update tonight", "Software Update: iOS 26.6 will update tonight"])
def test_notification_text_about_updating_tonight_is_still_the_lock_screen(words):
    text = ui.Element("n", "text", words, type="StaticText", name=words)
    tree = ui.Tree(keypad(extra=(text,)))
    assert unlock.foreign_kind(tree) is None and unlock.find_entry(tree, SB) is not None


def test_the_update_screens_title_still_refuses():
    text = ui.Element("t", "text", "Enter Passcode to Update Tonight", type="StaticText")
    assert unlock.foreign_kind(ui.Tree(keypad(extra=(text,)))) == "text"


# ---- 4636616243: the hints-on `unlock` description is the 1.9.2 one ----
def test_unlock_gives_the_new_words_with_hints_and_the_old_without():
    on, off = descriptions.describe("unlock", True), descriptions.describe("unlock", False)
    assert on == descriptions.SHARED_192["unlock"]
    assert "An unlock that works isn't limited" in on and "Enter Passcode to Update Tonight" in on
    assert off == descriptions.EXTRA_185.get("unlock", descriptions.EXTRA["unlock"])
    assert "An unlock that works isn't limited" not in off


# ---- 4636616251: systemd's `$` and `%` ----
def test_a_dollar_or_percent_in_a_systemd_argument_is_escaped():
    assert service._quote("/home/alice/vault-${SITE}.json") == '"/home/alice/vault-$${SITE}.json"'
    assert service._quote("/home/a b/50%.json") == '"/home/a b/50%%.json"'
    assert service._quote("/home/alice/vault.json") == "/home/alice/vault.json"  # plain: unchanged


# ---- 4636616222: a phone gone after a lock lets the idle stop come ----
def test_a_phone_not_found_after_a_lock_no_longer_reads_as_present():
    async def go():
        s = FakeSession()
        s.stuck = PhoneLocked("the phone is locked")
        d = make(s)
        stop, task = await started(d, until=())
        for _ in range(300):
            if isinstance(d.stuck, PhoneLocked):
                break
            await asyncio.sleep(0.01)
        assert d.phone_last_seen() is not None and time.monotonic() - d.phone_last_seen() < 1
        s.stuck = PhoneNotFound("no phone on USB")  # unplugged
        for _ in range(300):
            if d.state == "no-device":
                break
            await asyncio.sleep(0.01)
        assert d.state == "no-device" and d.stuck is None
        seen = d.phone_last_seen()
        await asyncio.sleep(0.05)
        assert seen is not None and d.phone_last_seen() == seen  # the last real time, not now
        await finish(stop, task)
    run(go)


# ---- 4636616234: the runner's /status is asked a few times before the reset ----
class _Busy(FakeSession):
    def __init__(self):
        super().__init__()
        self.failed = False
        self.device.runner = object()
        self.device.runner_alive = lambda: True
        self.answers = iter([False, True])  # a busy runner misses one /status, then answers

        async def still_answers():
            return any(next(self.answers, True) for _ in range(3))
        self.device._still_answers = still_answers

        async def wda_alive():
            return next(self.answers, True)
        self.device.wda_alive = wda_alive

    async def ensure(self):
        if not self.failed:
            self.failed = True
            self._record("ensure")
            raise ConnectionLost("WebDriverAgent didn't answer POST /session within 30 s")
        await super().ensure()


def test_one_missed_status_does_not_cost_the_runner():
    async def go():
        s = _Busy()
        d = make(s)
        await d._connect()
        assert "reset" not in s.calls
    run(go)


def test_the_runner_reuse_uses_the_devices_repeated_probe():
    import inspect
    from tapwright.daemon import Daemon
    assert "_still_answers" in inspect.getsource(Daemon._session_again_on_runner)


# ---- the lead's phone check: a first start took about 13 s ----
def test_install_waits_30_s_and_says_still_starting_not_a_failure():
    assert service.START_WAIT == 30.0
    text, code = _svc(Tasks(), running=None).install(["serve", "--keep-alive"])
    assert code == 0 and "still starting" in text and "no daemon answered" not in text


def test_a_daemon_that_answers_after_13_s_is_reported_up():
    answers = [None] * 27 + [{"port": 1}]  # (polled every 0.5 s: up at about 13 s)
    text, code = _svc(Tasks(), answers=[None, *answers]).install(["serve", "--keep-alive"])
    assert code == 0 and "the daemon answers" in text

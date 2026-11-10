"""Starting and stopping Screen Mirroring through Control Center, with a fake phone that shows one screen per read."""
import asyncio

import pytest
from fakes import button, el

from tapwright import mirror, ui


class Phone:
    """Shows the next of `screens` at each refresh; records what was done."""

    def __init__(self, *screens):
        self.screen = (430.0, 932.0)
        self.screens = list(screens)
        self.items: list = []
        self.done: list = []

    async def swipe(self, x0, y0, x1, y1, duration_ms=350, hold_ms=0):
        self.done.append(("swipe", round(x0), round(y0), round(x1), round(y1)))

    async def refresh(self, timeout=60):
        self.items = self.screens.pop(0)
        return ui.Tree(self.items)

    async def tap(self, target, hold_ms=20):
        hit = ui.resolve(self.items, target)
        self.done.append(("tap", hit.label))

    async def press_button(self, name):
        self.done.append(("button", name))


@pytest.fixture(autouse=True)
def no_waits(monkeypatch):
    async def instant(_seconds):
        return None
    monkeypatch.setattr(mirror.asyncio, "sleep", instant)


CENTRE = [el("e1", "image", "Screen Mirroring", rect=(132, 311, 78, 78)),
          button("e2", "Screen Mirroring", rect=(134, 313, 74, 74)), button("e3", "Flashlight")]
LIST = [el("e1", "header", "Screen Mirroring"), button("e2", "Living Room"), button("e3", "Tapwright")]
ON = [el("e1", "header", "Screen Mirroring"), button("e2", "Tapwright", value="1", selected=True),
      button("e3", "Stop Mirroring")]


def test_start_opens_control_centre_picks_the_receiver_and_goes_home():
    phone = Phone(CENTRE, LIST)
    assert asyncio.run(mirror.start(phone, "tapwright")) == "Mirroring to tapwright."
    assert phone.done == [("swipe", 400, 4, 400, 424), ("tap", "Screen Mirroring"), ("tap", "Tapwright"),
                          ("button", "home")]


def test_start_touches_nothing_more_when_a_step_shows_something_else():
    phone = Phone([button("e1", "Flashlight")])  # Control Center without the control, or it didn't open
    with pytest.raises(mirror.MirrorError) as e:
        asyncio.run(mirror.start(phone, "Tapwright"))
    assert e.value.code == "unsupported" and phone.done[-1] == ("button", "home") and len(phone.done) == 2
    phone = Phone(CENTRE, [el("e1", "header", "Screen Mirroring"), button("e2", "Living Room")])
    with pytest.raises(mirror.MirrorError) as e:  # our receiver isn't offered: nobody else's is picked
        asyncio.run(mirror.start(phone, "Tapwright"))
    assert "same Wi-Fi" in str(e.value) and ("tap", "Living Room") not in phone.done


def test_start_does_nothing_when_it_is_already_mirroring_to_us():
    phone = Phone(CENTRE, ON)
    assert asyncio.run(mirror.start(phone, "Tapwright")) == "Already mirroring to Tapwright."
    assert ("tap", "Tapwright") not in phone.done


def test_stop_taps_stop_mirroring_only_when_it_is_there():
    phone = Phone(CENTRE, ON)
    assert asyncio.run(mirror.stop(phone)) == "Stopped mirroring."
    assert ("tap", "Stop Mirroring") in phone.done and phone.done[-1] == ("button", "home")
    phone = Phone(CENTRE, LIST)
    assert asyncio.run(mirror.stop(phone)) == "The phone wasn't mirroring."
    assert [d for d in phone.done if d[0] == "tap"] == [("tap", "Screen Mirroring")]

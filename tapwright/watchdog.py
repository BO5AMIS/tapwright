"""The watchdog (AGENT-TOOLS §24.5, Tapwright 1.7): says in chat when the phone has gone, during an unattended run.

It watches the daemon's own state and the phone's battery, and says ONE message per event, then nothing more until
that event has cleared:
  - **gone:** the phone hasn't been reachable for GRACE seconds after it was (a short drop is a reconnect, not an
    event). It says how it went, from how it was reached: over USB, unplugged; over Wi-Fi, the Wi-Fi is down or the
    phone left the network.
  - **back:** the phone answers again, after a `gone` was said.
  - **battery low:** under LOW percent (read over lockdown, when the phone says it); said again only after it has
    been charged back over RECOVER percent.

Pure logic: the daemon feeds it its state, the way the phone was reached and the battery; `say(kind, text)` is how
a message goes out (the log, the hosts' dialog channels, a Note in each agent's next answer).
"""
from __future__ import annotations

import time
from collections.abc import Callable

GRACE = 60.0  # seconds the phone must stay gone before it is said
LOW = 20  # percent: the battery under this is said
RECOVER = 30  # percent: a battery said low is said again only after it got over this
UP = ("live", "locked")  # the daemon's states with the phone reachable (a locked phone still answers)

GONE_USB = ("Watchdog: the phone has gone: it was connected over USB and doesn't answer now (unplugged?). Runs that "
            "need it will fail until it is back; plug it in.")
GONE_WIFI = ("Watchdog: the phone has gone: it was connected over Wi-Fi and doesn't answer now (the Wi-Fi is down, "
             "or the phone left the network). Runs that need it will fail until it is back.")
GONE = "Watchdog: the phone has gone: it doesn't answer now. Runs that need it will fail until it is back."
BACK = "Watchdog: the phone is back (over {via})."
BATTERY = ("Watchdog: the phone's battery is at {percent}%: it may switch itself off during a long run. Put it on a "
           "charger.")


def gone_text(via: str | None) -> str:
    """How the phone went, from how it was reached (Device.via: "USB", "local network", "network <host>:<port>")."""
    if not via:
        return GONE
    return GONE_USB if via.strip().casefold() == "usb" else GONE_WIFI


class Watchdog:
    def __init__(self, say: Callable[[str, str], None], clock: Callable[[], float] = time.monotonic,
                 grace: float = GRACE, low: int = LOW, recover: int = RECOVER) -> None:
        self.say, self.clock, self.grace, self.low, self.recover = say, clock, grace, low, recover
        self.ever_up = False
        self.via: str | None = None  # how the phone was last reached ("usb", "wifi", an address)
        self.gone_since: float | None = None
        self.gone_said = False
        self.battery_said = False

    def state(self, state: str, via: str | None = None) -> None:
        """The daemon's state now, and how the phone is reached when it is up. Call it on every change and on a tick."""
        now = self.clock()
        if state in UP:
            if via:
                self.via = via
            self.ever_up = True
            self.gone_since = None
            if self.gone_said:
                self.gone_said = False
                self._say("back", BACK.format(via=self.via or "the network"))
            return
        if not self.ever_up:
            return  # (not yet connected once: doctor and the first answer say why)
        if self.gone_since is None:
            self.gone_since = now
        if not self.gone_said and now - self.gone_since >= self.grace:
            self.gone_said = True
            self._say("gone", gone_text(self.via))

    def battery(self, percent: int | None, charging: bool | None = None) -> None:
        """The battery's level, when the phone said it (None: unknown, nothing changes)."""
        if percent is None:
            return
        if percent < self.low and not self.battery_said and charging is not True:
            self.battery_said = True
            self._say("battery", BATTERY.format(percent=percent))
        elif percent >= self.recover:
            self.battery_said = False

    def _say(self, kind: str, text: str) -> None:
        try:
            self.say(kind, text)
        except Exception:
            pass  # (a message that can't go out never stops the daemon)

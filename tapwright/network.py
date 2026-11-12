"""`network` (docs/AGENT-TOOLS.md §16.4): which of the phone's developer condition profiles each mode uses, and the line
that says a condition is on. The phone is reached in device.py (Device.set_network).

Apple's condition inducer (Instruments' ConditionInducer channel, the "Network Link" conditions of Xcode's Devices
window) offers groups of profiles, listed by the phone itself (`pymobiledevice3 developer dvt condition list`). The
network group is `SlowNetworkCondition`. The identifiers below are the ones Xcode's Network Link profiles carry; this
file was written without a phone to list them from, so the choice is made against the phone's own list at run time
(pick_profile): the first identifier a mode names that the phone offers, else a profile whose description holds one
of the mode's words. A mode with no match is `unsupported`, naming what the phone offers.

  offline  SlowNetwork100PctLoss    "100% Loss"
  3g       SlowNetwork3GGood        "3G Good" (about 780 kbit/s, 100 ms), else any "3G" profile
  lossy    SlowNetworkVeryBadNetwork "Very Bad Network" (a little under 1 Mbit/s, 500 ms, 10% loss)
"""
from __future__ import annotations

MODES = ("offline", "3g", "lossy", "normal")
BREAKING = ("offline", "lossy")  # would cut Tapwright's own link over Wi-Fi (§16.4)

# mode -> (identifiers in order of preference, words a description may hold when none of them is offered)
PROFILES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "offline": (("SlowNetwork100PctLoss",), ("100% loss", "100 pct loss")),
    "3g": (("SlowNetwork3GGood", "SlowNetwork3GAverage", "SlowNetwork3G", "SlowNetwork3GBad"), ("3g",)),
    "lossy": (("SlowNetworkVeryBadNetwork",), ("very bad",)),
}


def line(mode: str) -> str:
    """The one line an answer carries while a condition is on, and the answer of the call that set it."""
    return f"Network: {mode} (Tapwright, whole phone)."


def profiles(groups: list) -> list[tuple[str, str]]:
    """(identifier, description) of every profile in the phone's list of condition groups."""
    found: list[tuple[str, str]] = []
    for group in groups if isinstance(groups, list) else []:
        for profile in (group.get("profiles") if isinstance(group, dict) else None) or []:
            if isinstance(profile, dict) and isinstance(profile.get("identifier"), str):
                found.append((profile["identifier"], str(profile.get("description") or profile.get("name") or "")))
    return found


def pick_profile(mode: str, groups: list) -> str | None:
    """The identifier of the phone's profile for `mode`, or None when it offers none."""
    ids, words = PROFILES[mode]
    offered = profiles(groups)
    have = {identifier for identifier, _ in offered}
    for identifier in ids:
        if identifier in have:
            return identifier
    for identifier, description in offered:
        text = f"{identifier} {description}".lower()
        if any(w in text for w in words):
            return identifier
    return None

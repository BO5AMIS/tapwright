"""Human-like finger paths (SPEC-2.0 D6): pure functions, no I/O.

A free swipe goes down, rests a moment (20-40 ms), then moves with a minimum-jerk speed profile (slow start, fast
middle, slow end) along a slight arc, and ends exactly at its target. A tap holds 60-110 ms and lands within 1.5 pt of
its target. Everything comes from the `random.Random` given, so a seeded one gives the same path every time.

`TAPWRIGHT_HUMAN=0` turns this off: `swipe` then returns the straight path `ui.swipe_points` describes, and `tap`
returns the target and hold unchanged. Scroll drags never come here: they keep `ui.scroll_drag_points`' own shape (an
eased end and a rest, so a list stops where the finger does and never flings)."""
import math
import os
import random

HUMAN = os.environ.get("TAPWRIGHT_HUMAN", "1") != "0"
DWELL_MS = (20.0, 40.0)  # the rest after the finger goes down, before it moves
TAP_HOLD_MS = (60, 110)
TAP_OFF_PT = 1.0  # a tap lands at most this far from its target (the spec's bound is 1.5 pt)
TAP_OFF_MAX = 1.5
STEP_MS = 12.0  # one point about every 12 ms along the move (hands.py fills in finer steps where a gap is longer)
ARC_SHARE = (0.015, 0.045)  # how far the middle of the path bows off the straight line, as a share of its length
ARC_MAX_PT = 8.0
JITTER_MS = 1.5  # each intermediate point's time moves by at most this much (times stay increasing)
SWIPE_PRESS_MS = 50  # ui.SWIPE_PRESS_MS: the straight path's press before it moves (kept in step with ui.py)


def min_jerk(s: float) -> float:
    """Share of the way covered at share `s` of the time, for a minimum-jerk movement: 10s^3 - 15s^4 + 6s^5."""
    s = min(1.0, max(0.0, s))
    return s * s * s * (10 - 15 * s + 6 * s * s)


def tap_hold(rng: random.Random) -> int:
    """How long a human tap holds, in ms: 60-110."""
    return rng.randint(*TAP_HOLD_MS)


def tap(x: float, y: float, rng: random.Random, human: bool | None = None) -> tuple[float, float, int]:
    """(x, y, hold_ms) for a tap at (x, y): a little off the target (never more than TAP_OFF_MAX points) and a human
    hold. With humans off: the target itself and 0, which says "keep the caller's own hold"."""
    if not (HUMAN if human is None else human):
        return x, y, 0
    r = rng.uniform(0.0, TAP_OFF_PT)
    a = rng.uniform(0.0, 2 * math.pi)
    return x + r * math.cos(a), y + r * math.sin(a), tap_hold(rng)


def swipe(x0: float, y0: float, x1: float, y1: float, ms: float, rng: random.Random,
          human: bool | None = None) -> list[tuple[float, float, float]]:
    """A free swipe from (x0, y0) to (x1, y1) moving for `ms`: timed points [(t_ms, x, y), ...] from t = 0, the first
    at the start and the last exactly at the end, times increasing. Human: a 20-40 ms rest after the press, a
    minimum-jerk move along a slight arc (which side from `rng`). Not human: the straight path ui.swipe_points gives."""
    ms = max(float(ms), 1.0)
    if not (HUMAN if human is None else human):
        return [(0.0, x0, y0), (SWIPE_PRESS_MS + ms, x1, y1)]
    dwell = rng.uniform(*DWELL_MS)
    length = math.hypot(x1 - x0, y1 - y0)
    out: list[tuple[float, float, float]] = [(0.0, x0, y0), (dwell, x0, y0)]
    if length == 0:
        out.append((dwell + ms, x1, y1))
        return out
    # the unit normal to the line, and how far the middle bows along it (either side)
    nx, ny = -(y1 - y0) / length, (x1 - x0) / length
    bow = min(rng.uniform(*ARC_SHARE) * length, ARC_MAX_PT) * rng.choice((-1.0, 1.0))
    steps = max(4, math.ceil(ms / STEP_MS))
    prev = dwell
    for i in range(1, steps):
        s = i / steps
        share = min_jerk(s)
        off = bow * math.sin(math.pi * share)
        t = dwell + ms * s + rng.uniform(-JITTER_MS, JITTER_MS)
        t = max(t, prev + 0.5)  # never back in time, never two points at once
        t = min(t, dwell + ms - 0.5 * (steps - i))  # and room left for those after it
        prev = t
        out.append((t, x0 + (x1 - x0) * share + nx * off, y0 + (y1 - y0) * share + ny * off))
    out.append((dwell + ms, x1, y1))
    return out


def deviation(points: list[tuple[float, float, float]]) -> float:
    """The farthest any point lies from the straight line between the first and last (for checks and tests)."""
    _, x0, y0 = points[0]
    _, x1, y1 = points[-1]
    length = math.hypot(x1 - x0, y1 - y0)
    if length == 0:
        return max(math.hypot(x - x0, y - y0) for _, x, y in points)
    return max(abs((x1 - x0) * (y0 - y) - (x0 - x) * (y1 - y0)) / length for _, x, y in points)

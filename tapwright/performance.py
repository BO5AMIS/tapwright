"""`performance` (PLAN-1.3 item 3): what the samples of the app's own process add up to, how the answer says it, and
how `expect: {"under": {...}}` checks it. No phone here: device.AppMeter takes the samples, the daemon asks for them.

All the words an agent reads are constants, so they can be adjusted in one place."""
from __future__ import annotations

import dataclasses
import math

SECONDS_MIN, SECONDS_MAX, SECONDS_DEFAULT = 0.5, 30.0, 3.0
SAMPLE_MS = 500  # how often the process is sampled (sysmontap)
DROP_FPS = 45.0  # graphics gives one frame rate per second, not frame times: a second under this counts as a drop
DROP_MS = 50  # the plan's drop (a frame interval over 50 ms); used where a sample gives frame times (none does yet)

OPENED = "Opened in {seconds} s (to the first settled screen)."
OPENED_UNSETTLED = "Opened in {seconds} s (the screen was still changing when the wait ended)."
MEASURED = "Measured {seconds:.1f} s."
MEMORY = "Memory {avg:.0f} MB (peak {peak:.0f})."
CPU = "CPU {avg:.0f}% (peak {peak:.0f}%)."
FRAMES = "Frames {fps:.0f} fps (lowest {low:.0f}); {drops} {drop} {rule}."
# Agreed with Tapline: Tapline's `drops over 50 ms` stays Tapline's; Tapwright counts slow seconds, and `drops` in
# `under` counts those. The plan's rule is a frame over 50 ms (DROP_RULE_TIMED), which needs frame times. graphics gives one frame rate a
# second, so a drop is a second under 45 fps (DROP_RULE_SAMPLED), and the answer says which rule it used.
DROP_RULE_TIMED = f"over {DROP_MS} ms"
DROP_RULE_SAMPLED = f"under {DROP_FPS:.0f} fps"
DROP_RULE = DROP_RULE_SAMPLED
FRAMES_STILL = "Frames: none drawn (the screen didn't change)."
NOT_MEASURED = "Not measured here: {things}."
NOTHING = "Nothing could be measured."

# `expect: {"under": {...}}`: the keys Tapwright checks, and the keys only Tapline measures (accepted, not checked)
UNDER_KEYS = ("launch_s", "memory_mb", "cpu_percent", "fps_min", "drops")
UNDER_OTHER = ("js_heap_mb", "busy_ms")
UNDER_IGNORED = "{key} isn't measured by Tapwright; not checked."  # a note on the answer (the batch adds `Note: `)
UNDER_RULE = ("under takes launch_s, memory_mb, cpu_percent, fps_min, drops (js_heap_mb and busy_ms are Tapline's), "
              'each a number, like {"launch_s": 2.0, "memory_mb": 300}')
UNDER_ONLY = "expect's under goes with a performance step, and a performance step takes only under"
FAIL_LAUNCH = "opened in {got} s, over {limit} s"
FAIL_MEMORY = "memory peaked at {got:.0f} MB, over {limit} MB"
FAIL_CPU = "CPU averaged {got:.0f}%, over {limit}%"
FAIL_FPS = "frames dipped to {got:.0f} fps, under {limit} fps"
FAIL_DROPS = "{got} drops, over {limit}"
FAIL_UNMEASURED = "{what} wasn't measured, so it can't be checked"
UNMEASURED_WHAT = {"launch_s": "the launch (the step has no launch: true)", "memory_mb": "memory", "cpu_percent": "CPU",
                   "fps_min": "frames", "drops": "frames"}


@dataclasses.dataclass
class Measurement:
    """The samples of the app's own process while the call ran: MB of physical footprint, CPU % (of one core, as
    iOS counts it), and the display's frames per second, one number per second."""
    memory_mb: list[float] = dataclasses.field(default_factory=list)
    cpu_pct: list[float] = dataclasses.field(default_factory=list)
    fps: list[float] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Reading:
    """What a call found: the launch time when it launched, the window it measured, and the sums. A `None` is
    something that couldn't be measured."""
    opened_s: float | None = None
    unsettled: bool = False  # the launch's wait ended with the screen still changing
    seconds: float = 0.0
    memory_avg: float | None = None
    memory_peak: float | None = None
    cpu_avg: float | None = None
    cpu_peak: float | None = None
    fps_avg: float | None = None
    fps_min: float | None = None
    drops: int | None = None
    still: bool = False  # frames were sampled and none was drawn

    def missing(self) -> list[str]:
        out = []
        if self.memory_avg is None:
            out.append("memory")
        if self.cpu_avg is None:
            out.append("CPU")
        if self.fps_avg is None and not self.still:
            out.append("frames")
        return out


def summarize(m: Measurement, seconds: float, opened_s: float | None = None, unsettled: bool = False) -> Reading:
    """The numbers of a Measurement. A second with no frame at all is a still screen, not a slow one: it is left out
    of the frame rate and the drops."""
    r = Reading(opened_s=opened_s, unsettled=unsettled, seconds=seconds)
    if m.memory_mb:
        r.memory_avg, r.memory_peak = sum(m.memory_mb) / len(m.memory_mb), max(m.memory_mb)
    if m.cpu_pct:
        r.cpu_avg, r.cpu_peak = sum(m.cpu_pct) / len(m.cpu_pct), max(m.cpu_pct)
    drawn = [f for f in m.fps if f > 0]
    if drawn:
        r.fps_avg, r.fps_min = sum(drawn) / len(drawn), min(drawn)
        r.drops = sum(1 for f in drawn if f < DROP_FPS)
    elif m.fps:
        r.still = True
    return r


def _seconds(x: float) -> str:
    return f"{x:.1f}" if round(x, 1) == x else f"{x:g}"


def answer_lines(r: Reading) -> list[str]:
    """The answer as the plan words it: one sentence, then the numbers, then what couldn't be measured."""
    if r.opened_s is not None:
        first = (OPENED_UNSETTLED if r.unsettled else OPENED).format(seconds=f"{r.opened_s:.2f}")
    else:
        first = MEASURED.format(seconds=r.seconds)
    numbers = []
    if r.memory_avg is not None and r.memory_peak is not None:
        numbers.append(MEMORY.format(avg=r.memory_avg, peak=r.memory_peak))
    if r.cpu_avg is not None and r.cpu_peak is not None:
        numbers.append(CPU.format(avg=r.cpu_avg, peak=r.cpu_peak))
    if r.fps_avg is not None and r.fps_min is not None and r.drops is not None:
        numbers.append(FRAMES.format(fps=r.fps_avg, low=r.fps_min, drops=r.drops, drop="second" if r.drops == 1 else "seconds",
                                     rule=DROP_RULE))
    elif r.still:
        numbers.append(FRAMES_STILL)
    lines = [first]
    if numbers:
        lines.append(" ".join(numbers))
    elif r.opened_s is None:
        lines.append(NOTHING)
    if (gone := r.missing()):
        lines.append(NOT_MEASURED.format(things=", ".join(gone)))
    return lines


def check_under(under: object) -> dict[str, float]:
    """`under` as given, checked: an object of known keys with non-negative numbers. Raises ValueError with the
    sentence the agent reads (the caller makes it bad_params)."""
    if not isinstance(under, dict) or not under:
        raise ValueError(UNDER_RULE)
    for key, limit in under.items():
        if key not in UNDER_KEYS and key not in UNDER_OTHER:
            raise ValueError(f"under has no {key!r}; {UNDER_RULE}")
        if isinstance(limit, bool) or not isinstance(limit, (int, float)) or not math.isfinite(limit) or limit < 0:
            raise ValueError(f"under's {key} must be a number of 0 or more, not {limit!r}")
    return dict(under)


def ignored(under: dict) -> list[str]:
    """What a check leaves out, as notes: the keys only Tapline measures."""
    return [UNDER_IGNORED.format(key=k) for k in under if k in UNDER_OTHER]


def failures(r: Reading, under: dict) -> list[str]:
    """Why the reading isn't under its limits, one reason each (empty: it is). A limit whose number wasn't measured
    fails: a check that can't be made doesn't pass."""
    out: list[str] = []
    for key in UNDER_KEYS:
        if key not in under:
            continue
        limit = under[key]
        got = {"launch_s": r.opened_s, "memory_mb": r.memory_peak, "cpu_percent": r.cpu_avg, "fps_min": r.fps_min,
               "drops": r.drops}[key]
        if got is None:
            if key in ("fps_min", "drops") and r.still:
                continue  # nothing drawn: no slow frame (fps_min has nothing to be under, either)
            out.append(FAIL_UNMEASURED.format(what=UNMEASURED_WHAT[key]))
        elif key == "launch_s" and got > limit:
            out.append(FAIL_LAUNCH.format(got=_seconds(got), limit=_seconds(limit)))
        elif key == "memory_mb" and got > limit:
            out.append(FAIL_MEMORY.format(got=got, limit=f"{limit:g}"))
        elif key == "cpu_percent" and got > limit:
            out.append(FAIL_CPU.format(got=got, limit=f"{limit:g}"))
        elif key == "fps_min" and got < limit:
            out.append(FAIL_FPS.format(got=got, limit=f"{limit:g}"))
        elif key == "drops" and got > limit:
            out.append(FAIL_DROPS.format(got=got, limit=f"{limit:g}"))
    return out

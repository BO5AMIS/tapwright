"""An opt-in timing trace (TAPWRIGHT_TRACE=1 for `tapwright serve`, read once at start): one JSON line per agent touch
and per tree read in ~/.tapwright/trace.jsonl, for 1.6's measurements E1a (where a touch's time goes) and E2 (where
a read's time goes). Off, every hook is a single boolean test and nothing is written. The hooks only note times: no
request goes to the phone for them and no answer depends on them (a write that fails is dropped).

A touch line: {"tool", "kind": "touch", "t0", "phases": {...}} with phase offsets in ms from t0 (the call's arrival,
before personal mode's front-app check; each phase is the first time it was reached):
  scope     the front-app check (personal mode; else the hold for the phone) is done and the tool starts
  act       the action got its turn on the phone's queue (scope..act is the wait for the queue)
  still     the silent read's wait for a still screen is over and its request is about to go (only when one is made)
  read      that tree read is done and the list carried over (still..read is the phone's tree dump)
  resolved  the target is resolved, scrolled to and checked: the touch is about to be prepared
  policy    the safety policy has had its say on the touch
  guard     the banner guard (a touch near the top asks the front app again) and Tapline's notice are done
  sent      the request for the touch went to WebDriverAgent (the first touch of the call)
  wda       WebDriverAgent's answer came (`wda_value`: the numbers in the answer, if the runner sends any timing)
  wda_last  only with several touches in one call (a scroll to an end drags up to 15 times, each ~0.8 s): the answer
            of the latest one; `still` after it is then the real wait for the screen
  frame     the first stream frame that differs from the one before the touch (it can come before `wda`: the
            call returns about 0.2 s after the effect shows)
  settled   the settle ended
  returned  the call returned
A read line: {"tool": "read", "kind": "read", "t0", "bytes", "phases": {sent, received, parsed, built}}: the request
went out, the answer's bytes came, the XML was parsed, the element list was built."""
import contextvars
import json
import math
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

TOUCH_TOOLS = ("tap", "long_press", "toggle", "type_text", "swipe", "scroll")
PHASES = {"touch": ("scope", "act", "still", "read", "resolved", "policy", "guard", "sent", "wda", "wda_last", "frame", "settled",
                    "returned"),
          "read": ("sent", "received", "parsed", "built")}
TOUCH_SUFFIXES = ("/wda/tw/tap", "/wda/tw/drag", "/actions")  # the routes a touch goes by (fast tap, fast drag, W3C)
READ_SUFFIX = "/wda/tw/tree"  # the fork's compact read (route version 4): a tree read as /source is

_path: Path | None = None  # where lines go; None: off
_active: contextvars.ContextVar["Rec | None"] = contextvars.ContextVar("tapwright_trace", default=None)
_arrival: contextvars.ContextVar[float | None] = contextvars.ContextVar("tapwright_trace_arrival", default=None)
_read: "Rec | None" = None  # the tree read in progress (its answer came, its list isn't built yet)
_size = 0  # the byte size of the newest answer the device read


@dataclass
class Rec:
    tool: str
    kind: str
    t0: float = field(default_factory=time.monotonic)
    phases: dict[str, float] = field(default_factory=dict)  # ms from t0, the first time each was marked
    extra: dict = field(default_factory=dict)
    baseline: object = None  # a touch: the frame before it (what the settle compares frames with)
    touches: int = 0

    def mark(self, name: str, at: float | None = None) -> None:
        if name not in self.phases:
            self.phases[name] = round(max(0.0, ((time.monotonic() if at is None else at) - self.t0) * 1000), 3)


def enable(path: Path) -> None:
    global _path
    _path = Path(path)


def disable() -> None:
    global _path, _read
    _path, _read = None, None


def on() -> bool:
    return _path is not None


def enable_from_env(path: Path, env: dict | None = None) -> bool:
    """Turn the trace on for the process when TAPWRIGHT_TRACE=1 (called once, as the daemon starts)."""
    if (os.environ if env is None else env).get("TAPWRIGHT_TRACE") == "1":
        enable(path)
    return on()


def _write(rec: Rec) -> None:
    line = {"tool": rec.tool, "kind": rec.kind, "t0": round(rec.t0, 6), **rec.extra, "phases": rec.phases}
    try:
        if _path is None:
            return
        _path.parent.mkdir(parents=True, exist_ok=True)
        with _path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, separators=(",", ":")) + "\n")
    except (OSError, TypeError, ValueError):
        pass  # a trace must never change an answer


# ---- a touch tool call (the daemon's _run_tool) ----
def arrive() -> contextvars.Token | None:
    """A tool call arrives (before personal mode's front-app check): the touch's t0 counts from here. Pair with leave()."""
    if _path is None:
        return None
    return _arrival.set(time.monotonic())


def leave(token: contextvars.Token | None) -> None:
    if token is not None:
        _arrival.reset(token)


def mark(name: str) -> None:
    """The traced touch reached `name` (the first time only). Nothing when the trace is off or no touch is traced."""
    if _path is None:
        return
    rec = _active.get()
    if rec is not None:
        rec.mark(name)


def begin(tool: str) -> tuple[Rec, contextvars.Token] | None:
    if _path is None or tool not in TOUCH_TOOLS:
        return None
    rec = Rec(tool, "touch")
    arrived = _arrival.get()
    if arrived is not None:
        rec.t0 = arrived
    rec.mark("scope")
    return rec, _active.set(rec)


def end(started: tuple[Rec, contextvars.Token] | None) -> None:
    """The call returns: written only when a touch went out in it (a call refused before any touch is no sample)."""
    if started is None:
        return
    rec, token = started
    _active.reset(token)
    rec.mark("returned")
    if "sent" in rec.phases:
        rec.extra["touches"] = rec.touches
        _write(rec)


def baseline(frame_before: object) -> None:
    """The frame before the touch: `frame` counts from the first later frame that is unlike it."""
    rec = _active.get()
    if rec is not None:
        rec.baseline = frame_before


def frame(since: object, at: float | None = None) -> None:
    """The settle found a frame unlike `since`, which arrived at `at` (monotonic seconds)."""
    rec = _active.get()
    if rec is not None and since is not None and rec.baseline is since:
        rec.mark("frame", at)


def note(key: str, value: object) -> None:
    """A fact about the traced touch (the first time only), written in its line next to the phases."""
    if _path is None:
        return
    rec = _active.get()
    if rec is not None and key not in rec.extra:
        rec.extra[key] = value


def settled() -> None:
    rec = _active.get()
    if rec is not None:
        rec.mark("settled")


# ---- WebDriverAgent requests (the session's _call) and the device's bytes ----
def note_bytes(count: int) -> None:
    global _size
    if _path is not None:
        _size = count


class Pending:
    """A request in flight that a trace is timing."""
    __slots__ = ("rec", "touch")

    def __init__(self, rec: Rec, touch: bool) -> None:
        self.rec, self.touch = rec, touch


def request(method: str, path: str) -> Pending | None:
    """Called as a request goes out. None when off, or when it is neither a touch of a traced call nor a tree read."""
    global _read
    if _path is None:
        return None
    route = path.split("?", 1)[0]
    if method == "POST" and route.endswith(TOUCH_SUFFIXES):
        rec = _active.get()
        if rec is None:
            return None
        rec.touches += 1
        rec.mark("sent")
        return Pending(rec, True)
    if method == "GET" and (route == "/source" or route.endswith(READ_SUFFIX)):
        _read = rec = Rec("read", "read")
        rec.mark("sent")
        return Pending(rec, False)
    return None


def response(pending: Pending | None, answer: object = None, ok: bool = True) -> None:
    global _read
    if pending is None:
        return
    rec = pending.rec
    if pending.touch:
        rec.mark("wda")
        if rec.touches > 1:  # a scroll's drags go one after the other: `wda` is the first answer, this the latest
            rec.phases["wda_last"] = round(max(0.0, (time.monotonic() - rec.t0) * 1000), 3)
        value = answer.get("value") if isinstance(answer, dict) else None
        if isinstance(value, dict) and "wda_value" not in rec.extra:
            numbers = {k: v for k, v in value.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
            if numbers:
                rec.extra["wda_value"] = numbers  # whatever timing the fork's route reports, as it sends it
    elif ok:
        rec.mark("received")
        rec.extra["bytes"] = _size
        value = answer.get("value") if isinstance(answer, dict) else None
        if isinstance(value, dict) and "token" in value:  # the compact read: whether it changed, the runner's own times
            rec.extra["compact"] = "unchanged" if value.get("unchanged") is True else "tree"
            if isinstance(value.get("ms"), dict):
                rec.extra["wda_ms"] = {k: v for k, v in value["ms"].items()
                                       if isinstance(v, (int, float)) and not isinstance(v, bool)}
    elif _read is rec:
        _read = None


def parsed() -> None:
    if _read is not None:
        _read.mark("parsed")


def built() -> None:
    """The element list is built: the read's line is written."""
    global _read
    rec, _read = _read, None
    if rec is not None and "received" in rec.phases:
        rec.mark("parsed")  # (a parse that went unnoticed ends where the build does)
        rec.mark("built")
        _write(rec)


# ---- the report ----
def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def summarize(lines: list[dict]) -> dict[str, dict]:
    """{tool: {"n", "phases": {phase: (median, p90)}, "bytes": (median, p90) or None}}, tools in the order first seen."""
    out: dict[str, dict] = {}
    for line in lines:
        tool, phases = line.get("tool"), line.get("phases")
        if not isinstance(tool, str) or not isinstance(phases, dict):
            continue
        entry = out.setdefault(tool, {"kind": line.get("kind", "touch"), "n": 0, "samples": {}, "sizes": []})
        entry["n"] += 1
        for name, ms in phases.items():
            if isinstance(ms, (int, float)) and not isinstance(ms, bool):
                entry["samples"].setdefault(name, []).append(float(ms))
        if isinstance(line.get("bytes"), int):
            entry["sizes"].append(float(line["bytes"]))
    result: dict[str, dict] = {}
    for tool, entry in out.items():
        order = PHASES.get(entry["kind"], ())
        names = sorted(entry["samples"], key=lambda p: order.index(p) if p in order else len(order))
        stats = {p: (statistics.median(entry["samples"][p]), _percentile(entry["samples"][p], 0.9)) for p in names}
        sizes = entry["sizes"]
        result[tool] = {"n": entry["n"], "phases": stats,
                        "bytes": (statistics.median(sizes), _percentile(sizes, 0.9)) if sizes else None}
    return result


def load(path: Path) -> list[dict]:
    lines: list[dict] = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            found = json.loads(raw)
        except ValueError:
            continue  # a half-written last line
        if isinstance(found, dict):
            lines.append(found)
    return lines


def table(lines: list[dict]) -> str:
    rows = [f"{'tool':<12}{'n':>5}  {'phase':<10}{'median':>10}{'p90':>10}   (ms from the call's arrival; bytes for a read's answer)"]
    for tool, entry in summarize(lines).items():
        first = True
        shown = [(name, *stat) for name, stat in entry["phases"].items()]
        if entry["bytes"] is not None:
            shown.append(("bytes", *entry["bytes"]))
        for name, median, p90 in shown:
            digits = 0 if name == "bytes" else 1
            rows.append(f"{tool if first else '':<12}{entry['n'] if first else '':>5}  {name:<10}"
                        f"{median:>10.{digits}f}{p90:>10.{digits}f}")
            first = False
    return "\n".join(rows) if len(rows) > 1 else "no trace lines"


def main(argv: list[str] | None = None) -> int:
    """python -m tapwright.trace [FILE]: the table of ~/.tapwright/trace.jsonl (or FILE)."""
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] in ("-h", "--help"):
        print(main.__doc__)
        return 0
    from . import store
    path = Path(args[0]) if args else store.HOME / "trace.jsonl"
    if not path.exists():
        print(f"no trace at {path}: start `tapwright serve` with TAPWRIGHT_TRACE=1, use the phone, then run this again")
        return 1
    print(table(load(path)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

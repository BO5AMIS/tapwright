"""The reproduction check of a kept bug (AGENT-TOOLS 1.16.2 §29.4): `repro.json` run N times, counting the runs that show the
same bug. The search for what a run shows is here, over a narrow `Port` (the daemon's side is `DaemonCheck` below; tests
drive a fake), so the rules can be read in one place:

- A run: the app is reset (Tapwright's own reset, a relaunch with `open_app`, which keeps the app's data, §29.8), driven to the
  script's `start` over the map, and the steps run one at a time. At most 60 seconds a run. A run that couldn't start (no
  reset or no way to the start) isn't made; when none could, the bug isn't checked.
- The same bug: what the run ends with has the bug's signature (§29.5): kind, place key, normalized `got`, and for a crash
  the exception type and the top app frame's symbol. `expect_failed` is the same `expect` failing at the same step; `a11y`
  and `ux` the same first issue on the same place; `observed` every step ran and the app ended on the same place key.
  `unreachable` has no steps and isn't checked.
- Safety (Tapline's lesson, PLAN-1.8.5 C1): every move goes through the ONE dispatch gate of 1.8.0 (the skip floor, checked
  on the element the touch really goes to after resolution and scroll, `daemon._TOUCH_FLOOR`), with nobody asked and no
  grant. The check stops at the first step that would need a person: a `${typed}` step or a step whose target holds a
  policy or floor word is known before any move, so nothing is touched at all; a hold the gate finds at dispatch stops the
  run there, with no move of that step. A person on the phone goes first (the explorer's gate): `busy` after 30 s of their
  input, `paused`, `locked`.
- Tapwright has no `app_command reset`: `reset: "app"` can't be done, and the bug says so instead of being checked.
"""
from __future__ import annotations

import dataclasses
import re
from typing import Callable, Protocol

from . import bug, explore, findings

RUNS_DEFAULT = 3
RUNS_MAX = 10
RUN_SECONDS = 60.0
RESET_OWN = "relaunch only, data kept"
UNREACHABLE = "an unreachable place has no steps"
NO_START = "the screen it started on isn't known"
NO_KEY = "the screen's key isn't known"  # (an `observed` bug with no place key: any end would count, review R185 L11)
NO_RESET = "no reset reached a known place"
NO_APP_RESET = ('reset "app" clears the app\'s data with app_command reset, which Tapwright doesn\'t have; reset "own" '
                "relaunches the app and keeps its data")
PENDING = "the check didn't finish"
TYPED = "a typed value"
STOPS = ("paused", "busy", "locked")
_VARIABLE = re.compile(r"(?<!\$)\$\{")
_POLICY_WORD = re.compile(r'policy word "([^"]*)"')
_BROKE = re.compile(r"^Ran \d+ of \d+ steps; stopped at step \d+ \(.*?\): ", re.S)
_PATTERN_PARAM = re.compile(r"/\[[A-Za-z_][A-Za-z0-9_]*\](?=/|$)")


class Stopped(Exception):
    """A person, a pause or a locked phone stopped the check (§29.4): the bug says `not checked: stopped (<code>)`."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclasses.dataclass(frozen=True)
class Done:
    """One step's end: its error code (None: it ran), the reason the tool gave, and the seconds it took to settle."""
    code: str | None = None
    message: str = ""
    seconds: float = 0.0


@dataclasses.dataclass(frozen=True)
class Seen:
    """What the app did during a step: its error-level log lines, a crash (reason, frames), another app in front."""
    errors: tuple[str, ...] = ()
    crash: tuple[str, tuple[str, ...]] | None = None
    foreign: bool = False


class Port(Protocol):
    def clock(self) -> float: ...
    async def gate(self) -> None: ...  # a person first; raises Stopped
    async def reset(self) -> bool: ...
    async def reach(self, place: str) -> bool: ...
    async def place_key(self) -> dict | None: ...
    def mark(self) -> int: ...
    async def step(self, step: dict) -> Done: ...
    async def seen(self, mark: int) -> Seen: ...
    async def issues(self, kind: str) -> tuple[str, ...]: ...
    async def has_back(self) -> bool: ...


@dataclasses.dataclass(frozen=True)
class Target:
    """What a run must show to be the same bug."""
    kind: str
    signature: str
    place_key: dict | None
    got: str
    start: str | None
    steps: list[dict]


def runs_of(verify: object, default: int) -> int:
    """`verify` as a number of runs: `true` is 3, `false`, 0 or none is none, else 1 to 10; absent is the source's default."""
    if verify is None:
        return default
    if verify is True:
        return RUNS_DEFAULT
    if verify is False:
        return 0
    return int(verify)  # type: ignore[call-overload]


def timeout_for(args: dict) -> float:
    """How long a client waits for `bug_report`'s answer: each run's 60 s with its reset and the way to its start, and the
    export's push when there is one."""
    verify = args.get("verify")
    runs = RUNS_DEFAULT if verify is True else verify if isinstance(verify, int) and not isinstance(verify, bool) else 0
    if verify is None and (args.get("finding") is not None or args.get("from") == "failure"):
        runs = RUNS_DEFAULT
    return 120.0 + runs * (RUN_SECONDS + 60.0) + (600.0 if args.get("export") is not None else 0.0)


def not_checked(why: str) -> dict:
    return {"checked": False, "why": why}


def target_of(finding: dict, meta: dict, script: dict | None) -> Target:
    """The bug's own facts and its script's start and steps (the finding's path when the script has none)."""
    steps = script.get("steps") if isinstance(script, dict) and isinstance(script.get("steps"), list) else None
    if steps is None:
        steps = list(finding.get("path") or [])
    start = script.get("start") if isinstance(script, dict) and isinstance(script.get("start"), str) else None
    return Target(finding["kind"], str(meta.get("signature") or ""), meta.get("place_key"), finding["got"],
                  start or finding.get("path_from"), [s for s in steps if isinstance(s, dict)])


def why_not(target: Target, reset: str) -> str | None:
    """Why the bug can't be checked at all, before anything is touched; None when it can."""
    if target.kind == "unreachable":
        return UNREACHABLE
    if reset == "app":
        return NO_APP_RESET
    if not target.start:
        return NO_START
    if target.kind == "observed" and target.place_key is None:
        return NO_KEY
    return None


def person_step(steps: list[dict], word_of: Callable[[dict], str | None]) -> tuple[int, str] | None:
    """The first step that would need a person (§29.4), known before any move: a `${typed}` value (or any value a person
    must give), or a target that holds a policy or floor word. (its number from 1, why), or None."""
    for i, step in enumerate(steps, 1):
        if step.get("tool") == "type_text" and (not isinstance(step.get("text"), str) or _VARIABLE.search(step["text"])):
            return i, TYPED
        word = word_of(step)
        if word is not None:
            return i, f'the policy word "{word}"'
    return None


def unaimed(steps: list[dict]) -> str | None:
    """Why the steps can't be run as they are written, or None: a row of a list named by its place (§18.1) or a step with no
    tool is written for a person, not for a run."""
    for i, step in enumerate(steps, 1):
        if step.get("row") is True or not isinstance(step.get("tool"), str):
            return f"step {i} names a row of a list by its place, which a run can't aim at"
        if step.get("tool") == "open_url" and isinstance(step.get("url"), str) and _PATTERN_PARAM.search(step["url"]):
            # (§18.3, review R185 H1: a link is kept only as its place's pattern; a value in it isn't kept)
            return f"step {i} opens a link whose value isn't kept ({step['url']}), which a run can't fill in"
    return None


def needs_person(at: int, why: str) -> dict:
    return not_checked(f"step {at} needs a person ({why})")


def _same(target: Target, kind: str, key: dict | None, got: str, reason: str | None = None,
          frames: tuple[str, ...] = ()) -> bool:
    """Does what a run ended with have the bug's signature (§29.5)? A bug with no place key compares with none."""
    if kind != target.kind or not target.signature:
        return False
    got = "\n".join(bug.strip_paths(line) for line in got.split("\n"))  # (as the bug's own `got` was: review R185 M2)
    sig = bug.signature(kind, key if target.place_key is not None else None, got, reason, frames)
    return sig == target.signature


async def one_run(port: Port, target: Target, steps: list[dict], scrub: Callable[[str], str]) -> bool | tuple[int, str]:
    """One run after its reset: True when it showed the bug, False when it didn't, (step, why) when a step needs a person."""
    began = port.clock()
    safe = findings.safe_scrub(scrub)
    for i, step in enumerate(steps, 1):
        if port.clock() - began > RUN_SECONDS:
            return False
        await port.gate()
        key = await port.place_key()
        mark = port.mark()
        t0 = port.clock()
        done = await port.step(step)
        seconds = done.seconds or (port.clock() - t0)
        if done.code in STOPS:
            raise Stopped(done.code)  # type: ignore[arg-type]
        if done.code == "not_allowed" and "needs_confirmation" in done.message:
            word = _POLICY_WORD.search(done.message)  # the gate held the actual recipient: no move of this step was made
            return i, f'the policy word "{word.group(1)}"' if word else "the skip floor holds what it touches"
        seen = await port.seen(mark)
        if seen.crash is not None:
            reason, frames = seen.crash
            reason = safe(reason)
            return _same(target, "crash", key, safe(f"the app crashed: {reason}"), bug.strip_paths(reason),
                         tuple(bug.strip_paths(safe(f)) for f in frames))
        if seen.errors and _same(target, "js_error", key, safe(seen.errors[0])):
            return True
        if done.code == "expect_failed":
            return target.kind == "expect_failed" and i == len(steps)
        if done.code == "not_actionable":
            detail = done.message.split(":", 1)[0].strip().lower()
            return _same(target, "refused", key, f"not_actionable: {detail}")
        if done.code is not None:
            return False  # (a step that failed another way: the run didn't reach the bug)
        if seen.foreign:
            return _same(target, "dead_end", key, "it left the app")
        if seconds > explore.SLOW_SECONDS and _same(target, "slow", key, f"{seconds:.1f} s to settle"):
            return True
    key = await port.place_key()
    if target.kind == "observed":
        return target.place_key is None or key == target.place_key
    if target.kind in ("a11y", "ux"):
        issues = await port.issues(target.kind)
        return bool(issues) and _same(target, target.kind, key, safe("\n".join(issues[:findings.MAX_ISSUES])))
    if target.kind == "dead_end" and not target.got.startswith("it left the app"):
        # a place with no way back but a reset (§25.8): the app is on that place and it shows no back
        return (target.place_key is None or key == target.place_key) and not await port.has_back()
    return False


async def check(port: Port, target: Target, steps: list[dict], runs: int, *, build: str, when: Callable[[], str],
                scrub: Callable[[str], str]) -> dict:
    """The check (§29.4): `{"n", "of", "build", "when", "reset"}`, or `{"checked": false, "why"}`. `steps` are the script's
    steps ready to run (substituted, checked). Raises Stopped when a person, a pause or a locked phone stops it."""
    made = shown = 0
    for _ in range(runs):
        await port.gate()
        if not await port.reset() or not await port.reach(target.start or ""):
            continue  # a run that couldn't start isn't made
        made += 1
        result = await one_run(port, target, steps, scrub)
        if isinstance(result, tuple):
            return needs_person(*result)
        shown += 1 if result else 0
    if made == 0:
        return not_checked(NO_RESET)
    return {"n": shown, "of": made, "build": build, "when": when(), "reset": RESET_OWN}


# ---- the daemon's side ----

def _d():
    from . import daemon
    return daemon


class DaemonCheck:
    """`Port` over the daemon: every move is the agent's own call through `run_tool` and the batch (the action log, Pause, a
    person first, the dispatch gate the caller holds), one at a time. Reads are silent (no picture)."""

    def __init__(self, daemon, client_id: str, scope, app: str, place_key_of: Callable[[str | None, object], dict | None]) -> None:
        from .exploretool import DaemonPhone
        self.d, self.cid, self.scope, self.app = daemon, client_id, scope, app
        self.phone = DaemonPhone(daemon, client_id, scope, app)
        self._key_of = place_key_of

    def clock(self) -> float:
        return self.d.control.clock()

    async def gate(self) -> None:
        from .explore import PhoneStop
        try:
            await self.phone.gate()
        except PhoneStop as e:
            raise Stopped(str(e) or "busy") from None

    async def _call(self, name: str, args: dict) -> bool:
        from .control import Refused
        try:
            await self.d.run_tool(self.cid, name, args, self.scope)
            return True
        except Refused as e:
            if e.code in STOPS:
                raise Stopped(e.code) from None
            return False

    async def reset(self) -> bool:
        await self._call("close_app", {"app": self.app})
        return await self._call("open_app", {"app": self.app, "look": False})

    async def reach(self, place: str) -> bool:
        from . import driving, trip
        from .control import Refused
        made = trip.Trip(self.d, self.cid, self.scope, driving.Request(place, None, {}, None, None, "any", False),
                         {"look": False})
        try:
            await made.reach()
            return True
        except Refused as e:
            if e.code in STOPS:
                raise Stopped(e.code) from None
            return False
        finally:
            made.release()

    async def _tree(self):
        view = self.d.control.client(self.cid)
        await self.d._read(lambda: self.d._silent_read(view))
        return getattr(self.d.session, "last_tree", None)

    async def place_key(self) -> dict | None:
        try:
            tree = await self._tree()
        except Exception:  # noqa: BLE001 - a screen that can't be read has no key
            return None
        if tree is None or self.d._bundle() != self.app:
            return None
        return self._key_of(self.d._language.get(self.app), tree)

    def mark(self) -> int:
        return self.phone.mark()

    async def step(self, step: dict) -> Done:
        from .control import Refused
        try:
            await self.d._batch(self.cid, [step], self.scope, {"look": False}, [])
        except Refused as e:
            first = str(e).split("\n", 1)[0]
            return Done(e.code, _BROKE.sub("", first, count=1))
        return Done()

    async def seen(self, mark: int) -> Seen:
        watch = await self.phone.watch(mark)
        crash = (watch.crash.reason, tuple(watch.crash.frames)) if watch.crash is not None else None
        app = self.d.control.client(self.cid).app
        return Seen(tuple(watch.errors), crash, app is not None and app[1] not in (self.app, _d().SPRINGBOARD))

    async def issues(self, kind: str) -> tuple[str, ...]:
        from .control import Refused
        try:
            return await (self.phone.audit() if kind == "a11y" else self.phone.ux())
        except Refused as e:
            if e.code in STOPS:
                raise Stopped(e.code) from None
            return ()

    async def has_back(self) -> bool:
        try:
            tree = await self._tree()
        except Exception:  # noqa: BLE001
            return True  # (can't tell: not the same dead end)
        back = getattr(tree, "back", None)
        return back is not None and (back.point is not None or back.dismiss is not None)

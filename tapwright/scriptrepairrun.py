"""Self-repairing tests inside the daemon (docs/AGENT-TOOLS.md §26): `replay` with `repair`. A mixin of Daemon, kept apart from
daemon.py. The pure part (the score, what is never repaired, the candidates, the files, adopting) is scriptrepair.py and
repairanalysis.py; this is the run.

`suggest` runs the replay as `replay` does; at the first step that stops it ranks the candidates without touching any of
them and adds them after the usual failure text. Nothing is written.

`apply` runs the replay and repairs as it goes: at a step that may be repaired (§26.1) it tries up to 3 candidates, each a
real touch through the checks of any touch (§11). A candidate is accepted only when its own check holds right after it (the
step's `expect`, or without one the target of the next step resolves to exactly one element) and then the whole script runs to
its end (§26.4); the run goes on from the step, so that run is the second check. At most 5 repairs are made. When the whole run
passes it writes `<script>.repaired.json` and `<script>.repair.md` beside the script, and teaches the map the roads that moved;
when it fails it writes nothing. Adopting is not here: it is the command line's (`scriptrepair.adopt`).

Every trial is a line of kind `repair` in the audit log (§26.7): `step 4: tap id=login-btn to id=login-button (score 0.93),
accepted`, or `…, rejected (<why>)`. When the accepted line can't be written the repair isn't made.
"""
from __future__ import annotations

import dataclasses
import pathlib
import re

from . import __version__, batch, closest, driving, explorefloor, places, repairanalysis, roads, scriptrepair, ui
from .control import Refused
from .repairanalysis import Break, Screen
from .scriptrepair import Candidate, Repair, Work

ABORT = ("paused", "locked", "busy", "reconnecting")  # a person's stop is never "a candidate that failed the check"
MODES = ("suggest", "apply")
NO_AUDIT = "the audit log couldn't be written, so no repair was made"
CHECK_WAIT = 2.0  # seconds the next step's target is waited for after a repair (the default expect timeout, §9.2)


def _d():
    from . import daemon
    return daemon


def plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


@dataclasses.dataclass
class Tried:
    """One candidate, tried: whether it passed its own check, why not, the steps it put in, and how it was accepted."""
    ok: bool
    why: str = ""
    accepted: str = ""
    inserted: list[dict] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Run:
    """The state of one `apply` run."""
    script: pathlib.Path
    name: str
    bundle: str
    work: list[Work]
    began: float
    notes: list[str] = dataclasses.field(default_factory=list)
    repairs: list[Repair] = dataclasses.field(default_factory=list)
    learn: list[tuple] = dataclasses.field(default_factory=list)
    accepted: list[tuple[int, str]] = dataclasses.field(default_factory=list)  # (the original step, its phrase), for a later rejection
    start: int = 0  # where the run goes on from, in `work`


def renumbered(text: str, offset: int, total: int) -> str:
    """The failing answer of a run that began at step `offset + 1`, as the steps stand now: `Ran 3 of 7 steps; stopped at step 4`,
    the `Note (step 2):` lines and a trace's numbers."""
    out = []
    for n, line in enumerate(text.split("\n")):
        if n == 0:
            line = re.sub(r"^(Ran )(\d+)( of )(\d+)( steps; stopped at step )(\d+)",
                          lambda m: f"{m[1]}{int(m[2]) + offset}{m[3]}{total}{m[5]}{int(m[6]) + offset}", line)
        line = re.sub(r"^(Note \(step )(\d+)(\):)", lambda m: f"{m[1]}{int(m[2]) + offset}{m[3]}", line)
        line = re.sub(r"^(  )(\d+)(\. )", lambda m: f"{m[1]}{int(m[2]) + offset}{m[3]}", line)
        out.append(line)
    return "\n".join(out)


class RepairTools:
    """Mixed into Daemon. Needs: self.control, self.session, self.maps, self.audit, self._batch, self._read, self._silent_read,
    self._given, self._bundle, self._maps_root, self._build_of, self._language, self._may_map, self._off_screen_now,
    self._loaded, self.scrub_everywhere, self._replay_prepare."""

    # ---- the call ----
    @staticmethod
    def _repair_mode(args: dict) -> str | None:
        """`bad_params` for a `repair` that isn't `suggest` or `apply`, and for one beside `update_references`."""
        mode = args.get("repair")
        if mode is None:
            return None
        if mode not in MODES:
            raise Refused("bad_params", f'repair must be "suggest" or "apply", not {mode!r}')
        if args.get("update_references"):
            raise Refused("bad_params", "repair and update_references can't be used together: one repairs a script, the "
                                        "other saves its reference pictures")
        return mode

    async def _repair_replay(self, client_id: str, data: dict, scope, args: dict, path: pathlib.Path, mode: str,
                             drafts_only: bool) -> dict:
        """A loaded script's replay with `repair` (§26.5)."""
        steps, notes = await self._replay_prepare(client_id, data, scope)
        bundle = self._bundle() or ""
        if mode == "suggest":
            return await self._suggest(client_id, data, steps, notes, scope, args, bundle, drafts_only)
        return await self._apply(client_id, data, steps, notes, scope, args, path, bundle, drafts_only)

    # ---- suggest ----
    async def _suggest(self, client_id, data, steps, notes, scope, args, bundle, drafts_only) -> dict:
        try:
            return await self._batch(client_id, steps, scope, args, notes)
        except Refused as e:
            brk = repairanalysis.parse_break(str(e), e.code, steps)
            if brk is None:
                raise
            screen = await self._repair_screen(client_id, scope, bundle, steps[brk.index - 1])
            analysis = (repairanalysis.analyse(steps, brk.index - 1, brk, screen, drafts_only) if screen is not None
                        else scriptrepair.Analysis("none", reason="the app that was in front isn't in front now"))
            lines = scriptrepair.suggestion(analysis, screen.here if screen is not None else None)
            raise Refused(e.code, "\n".join([str(e), *lines])) from None

    # ---- apply ----
    async def _apply(self, client_id, data, steps, notes, scope, args, path, bundle, drafts_only) -> dict:
        clock = self.control.clock
        run = Run(path, str(data.get("name") or path.stem), bundle,
                  [Work(step, orig, n) for n, (step, orig) in enumerate(zip(steps, data["steps"]), 1)], clock(), list(notes))
        total = len(data["steps"])
        try:
            while True:
                offset = run.start
                suffix = [w.step for w in run.work[run.start:]]
                if not suffix:  # the repaired step was the last: nothing is left to run (it had its own check)
                    return await self._passed(client_id, data, run, {"text": "", "image": None, "image_size": None}, offset, total)
                try:
                    answer = await self._batch(client_id, suffix, scope, args, [])
                except Refused as e:
                    if e.code in ABORT:
                        raise
                    brk = repairanalysis.parse_break(str(e), e.code, suffix)
                    if brk is None:
                        raise self._stopped(run, e, offset) from None
                    if not await self._repair_one(client_id, scope, run, brk, offset, drafts_only):
                        raise self._stopped(run, e, offset) from None
                    continue
                return await self._passed(client_id, data, run, answer, offset, total)
        except Refused as e:
            if getattr(e, "tidied", False):
                raise
            raise self._stopped(run, e, 0) from None

    def _stopped(self, run: Run, e: Refused, offset: int) -> Refused:
        """The run ended on a step that isn't repaired: the usual failure, numbered as the steps stand now. What was accepted
        before it isn't kept (§26.5): each accepted repair's line gets its `rejected` line, and nothing is written."""
        text = renumbered(str(e), offset, len(run.work))
        found = re.match(r"Ran \d+ of \d+ steps; stopped at step (\d+)", text)
        at = found.group(1) if found else "?"
        for step, phrase in run.accepted:
            self._repair_audit(run, f"step {step}: {phrase}, rejected (the run failed at step {at})")
        lines = list(run.notes)
        if run.repairs:
            n = len(run.repairs)
            lines.append(f"Note: {n} {plural(n, 'repair was', 'repairs were')} made before it stopped; nothing was written.")
        out = Refused(e.code, "\n".join([text, *lines]) if lines else text)
        out.tidied = True  # type: ignore[attr-defined]
        run.accepted.clear()
        return out

    def _repair_audit(self, run: Run, detail: str) -> bool:
        """One line of kind `repair` (§26.7). False when it isn't on disk."""
        log = getattr(self, "audit", None)
        if log is None:
            return False
        return log.write("repair", name=run.name, app=run.bundle or None, detail=detail)

    async def _repair_one(self, client_id: str, scope, run: Run, brk: Break, offset: int, drafts_only: bool) -> bool:
        """Try to repair the break; True when a repair was made and the run goes on from after it."""
        at = offset + brk.index - 1
        absolute = Break(at + 1, brk.code, brk.reason)
        if len(run.repairs) >= scriptrepair.MAX_REPAIRS:
            run.notes.append(f"Note: {scriptrepair.MAX_REPAIRS} repairs were made and the script still breaks; more than "
                             f"{scriptrepair.MAX_REPAIRS} breaks are left to a person.")
            return False
        if getattr(self, "audit", None) is None:
            run.notes.append(f"Note: {NO_AUDIT}.")
            return False
        all_steps = [w.step for w in run.work]
        screen = await self._repair_screen(client_id, scope, run.bundle, all_steps[at])
        if screen is None:
            run.notes.append("Note: no repair was made: the app the script runs in isn't in front now.")
            return False
        analysis = repairanalysis.analyse(all_steps, at, absolute, screen, drafts_only)
        if analysis.kind != "ok":
            why = analysis.reason[:1].upper() + analysis.reason[1:]
            run.notes.append(f"Note: no repair was made. {why}.")
            return False
        started = await self._repair_where()
        original = run.work[at]
        for candidate in analysis.list[:scriptrepair.MAX_CANDIDATES]:
            tried = await self._try(client_id, scope, run, at, candidate, all_steps)
            record = self._record_of(run, original, candidate, tried)
            phrase = scriptrepair.phrase(record)
            if not tried.ok:
                self._repair_audit(run, f"step {record.step}: {phrase}, rejected ({tried.why})")
                if not await self._back_at_start(client_id, scope, run, started, screen):
                    run.notes.append("Note: a repair was tried and rejected, and the app couldn't be taken back to where the "
                                     "step started; the repair stopped.")
                    return False
                continue
            if not self._repair_audit(run, f"step {record.step}: {phrase}, accepted"):
                run.notes.append(f"Note: {NO_AUDIT}.")  # the line comes before the repair counts (§26.7)
                return False
            run.accepted.append((record.step, phrase))
            run.repairs.append(record)
            self._take(run, at, original, candidate, tried, record, (await self._repair_where())[0])
            run.start = at + len(tried.inserted) + 1
            return True
        n = min(len(analysis.list), scriptrepair.MAX_CANDIDATES)
        run.notes.append(f"Note: {plural(n, 'the candidate was', f'the first {n} candidates were' if n > 1 else '')} tried and none "
                         "passed the check.")
        return False

    # ---- one candidate ----
    async def _try(self, client_id: str, scope, run: Run, at: int, cand: Candidate, all_steps: list[dict]) -> Tried:
        """Run one candidate and check it (§26.4, rule 1)."""
        step = run.work[at].step
        before: list[dict] = []
        if cand.kind == "road":
            before = list(cand.moves)
        elif cand.kind == "scroll":
            made = await self._scroll_toward(client_id, scope, step, before)
            if made is not None:
                return Tried(False, made, inserted=before)
        replacement = step
        if cand.kind == "target" and cand.target is not None:
            replacement = {k: v for k, v in step.items() if k != "seen"}
            replacement["target"] = cand.target
            if cand.seen:
                replacement["seen"] = cand.seen
        ok, why = await self._run_steps(client_id, scope, [*before, replacement])
        if not ok:
            return Tried(False, why, inserted=before)
        checked = await self._check_one(client_id, scope, step, all_steps[at + 1] if at + 1 < len(all_steps) else None)
        if checked is not None:
            return Tried(False, checked, inserted=before)
        if "expect" in step:
            how = f"the step's expect {self._expect_text(step['expect'])} held"
        else:
            nxt = repairanalysis.next_target(all_steps[at + 1] if at + 1 < len(all_steps) else None) or {}
            how = f"the next step's target ({scriptrepair.aim_of(nxt)}) resolved to one element"
        return Tried(True, accepted=f"{how}, and the whole run passed.", inserted=before)

    @staticmethod
    def _expect_text(expect: dict) -> str:
        for key in ("text", "id", "label", "matches"):
            if isinstance(expect.get(key), str):
                return f"({key} {expect[key]!r})".replace("'", '"')
        return ""

    async def _run_steps(self, client_id: str, scope, steps: list[dict]) -> tuple[bool, str]:
        """Steps run as the batch runs them (every touch through the checks of any touch): (held, why not)."""
        try:
            await self._batch(client_id, steps, scope, {"look": False}, [])
        except Refused as e:
            if e.code in ABORT:
                raise
            found = repairanalysis.parse_break(str(e), e.code, steps)
            line = found.reason if found is not None else str(e).split("\n")[0]
            return False, " ".join(line.split())[:120]
        return True, ""

    async def _scroll_toward(self, client_id: str, scope, step: dict, before: list[dict]) -> str | None:
        """Scroll down, at most 5 times, until the step's target shows (§26.1); the steps made are put in `before`. None when
        it shows as one element, else why not."""
        view = self.control.client(client_id)
        target = step["target"]
        for _ in range(scriptrepair.MAX_SCROLLS):
            if await self._shows(client_id, scope, view, target, step["tool"]):
                break
            ok, why = await self._run_steps(client_id, scope, [{"tool": "scroll", "direction": "down"}])
            if not ok:
                return why
            before.append({"tool": "scroll", "direction": "down"})
        if not await self._shows(client_id, scope, view, target, step["tool"]):
            return f"{scriptrepair.aim_of(target)} didn't show after {scriptrepair.MAX_SCROLLS} scrolls"
        return None

    async def _read_now(self, client_id: str, scope, view) -> list[ui.Element] | None:
        """The screen as it is now once it is still, or None when the app in front isn't one this call may read."""
        await self._read(lambda: self._silent_read(view))
        given = await self._given(scope) if scope is not None else None
        if given is not None and self._bundle() not in given:
            return None
        return list(view.items)

    async def _shows(self, client_id: str, scope, view, target: dict, tool: str) -> bool:
        items = await self._read_now(client_id, scope, view)
        if items is None:
            return False
        try:
            return isinstance(ui.replayed(items, ui.parse_target(target), tool), ui.Element)
        except Refused:
            return False

    async def _check_one(self, client_id: str, scope, step: dict, following: dict | None) -> str | None:
        """§26.4, rule 1 after the step ran: its `expect` held (the batch checked it), or the target of the next step resolves to
        exactly one element once the screen has settled. None when it holds, else why not."""
        if "expect" in step:
            return None
        target = repairanalysis.next_target(following)
        if target is None:
            return "no check: the step has no expect and no target follows it"
        view, clock = self.control.client(client_id), self.control.clock
        deadline, tool = clock() + CHECK_WAIT, (following or {}).get("tool")
        while True:
            items = await self._read_now(client_id, scope, view)
            if items is None:
                return "the app in front isn't one this call may read"
            try:
                found = ui.replayed(items, ui.parse_target(target), tool if tool == "toggle" else None)
                if isinstance(found, ui.Element):
                    return None
                why = "isn't an element"
            except Refused as e:
                why = "isn't on the screen" if e.code == "not_found" else "matches more than one element"
            if clock() >= deadline:
                return f"the next step's target {scriptrepair.aim_of(target)} {why}"
            await self.control.sleep(0.25)

    def _record_of(self, run: Run, original: Work, cand: Candidate, tried: Tried) -> Repair:
        scrub = self.scrub_everywhere
        orig = original.orig or original.step
        was = batch.redacted_content(orig.get("target"), scrub) if isinstance(orig.get("target"), dict) else None
        return Repair(original.index or 0, str(orig.get("tool")), cand.kind, was, cand.target, cand.seen, cand.score,
                      cand.parts, cand.place, cand.place_from, scrub(cand.shown), scrub(cand.why), scrub(tried.accepted),
                      tried.inserted if cand.kind != "target" else [], cand.names)

    def _take(self, run: Run, at: int, original: Work, cand: Candidate, tried: Tried, record: Repair,
              arrived: str | None) -> None:
        """An accepted repair joins the steps: a new target for the step, or the moves before it. `arrived`: the place the app is
        on after the step, which the map's road must lead to for it to learn the repair."""
        if cand.kind == "target" and cand.target is not None:
            step = {k: v for k, v in original.step.items() if k not in ("seen", "target")}
            step["target"] = cand.target
            if cand.seen:
                step["seen"] = cand.seen
            marker: dict = {"was": record.was, **({"score": round(cand.score, 2)} if cand.score is not None else {})}
            run.work[at] = Work(step, original.orig, original.index, marker)
            run.learn.append((record, original.step, cand, arrived))
        else:
            added = [Work(m, None, None, {"inserted": True}) for m in tried.inserted]
            run.work[at:at + 1] = [*added, original]

    # ---- the screen ----
    async def _repair_screen(self, client_id: str, scope, bundle: str, step: dict) -> Screen | None:
        """What the analysis reads: the screen the break is on. None when another app is in front than the script's (nothing of
        another app is read into a repair)."""
        view = self.control.client(client_id)
        items = await self._read_now(client_id, scope, view)
        if items is None or (bundle and self._bundle() != bundle):
            return None
        tree = getattr(self.session, "last_tree", None)
        root, shown = self._maps_root(), self._bundle()
        here = None
        if tree is not None and shown and self._may_map(shown) and places.has_structure(tree):
            here = places.locate(self.maps.data(root, shown), places.read_place(tree, self._language.get(shown)))[0]
        near: list[ui.Element] = []
        try:
            aim = step.get("target") if isinstance(step.get("target"), dict) else None
            near = closest.nearest(ui.parse_target({k: v for k, v in aim.items() if k != "index"}), items) if aim else []
        except Refused:
            pass
        off = self._off_screen_now()
        screen = tuple(self.session.screen or (0.0, 0.0))
        build = await self._build_of(shown) if shown else None

        def way(a: str, b: str) -> list[tuple[dict, dict]] | None:
            return self._proven_taps(root, shown, a, b, build)
        policy = _d()._POLICY.get()
        return Screen(items, screen, explorefloor.Floor(policy, shown or ""), getattr(tree, "back", None), here, near,
                      any(e.role == "scroll" for e in items) or any(e.y >= screen[1] for e in off), way,
                      self.scrub_everywhere)

    def _proven_taps(self, root, bundle, a: str, b: str, build) -> list[tuple[dict, dict]] | None:
        """The way from place `a` to place `b` that a repair may put in front of a step: a chain of proven roads, every move a
        tap (or long press, or toggle) aimed at an element by name (§18.2). No link, no route, no back, no swipe, no point."""
        if not bundle or not self._may_map(bundle):
            return None
        plan = roads.plan(self.maps.data(root, bundle), a, b, build, "taps", routes=(), tool=roads.TOOL)
        if plan.kind != "roads" or not plan.proven or plan.routes:
            return None
        out: list[tuple[dict, dict]] = []
        for road in plan.roads:
            move = road["move"]
            if move.get("tool") not in roads.TARGETED or not isinstance(move.get("target"), dict) or "x" in move["target"]:
                return None
            try:
                tool, call = driving.call_of(move, {}, road.get("names"), False)
            except driving.Unaimable:
                return None
            out.append(({"tool": tool, **call}, dict(road.get("names") or {})))
        return out or None

    async def _repair_where(self) -> tuple[str | None, str | None]:
        """(the known place, the screen's signature) the app is on, to go back to after a trial that failed."""
        tree = getattr(self.session, "last_tree", None)
        shown = self._bundle()
        if tree is None or not shown or not places.has_structure(tree):
            return None, None
        read = places.read_place(tree, self._language.get(shown))
        place = places.locate(self.maps.data(self._maps_root(), shown), read)[0] if self._may_map(shown) else None
        return place, str(read.key.get("signature"))

    async def _back_at_start(self, client_id: str, scope, run: Run, started: tuple[str | None, str | None],
                             screen: Screen) -> bool:
        """After a trial that failed (§26.4): the app back on the place the step started on, by a proven road, else by `back`.
        True when it is there, or never left."""
        view = self.control.client(client_id)

        async def moved() -> bool:
            if await self._read_now(client_id, scope, view) is None:
                return True
            place, signature = await self._repair_where()
            if started[0] is not None and place is not None:
                return place != started[0]
            return signature != started[1]
        if not await moved():
            return True
        place, _ = await self._repair_where()
        if started[0] is not None and place is not None:
            road = self._proven_taps(self._maps_root(), self._bundle(), place, started[0], await self._build_of(self._bundle() or ""))
            if road is not None:
                ok, _ = await self._run_steps(client_id, scope, [m for m, _ in road])
                if ok and not await moved():
                    return True
        ok, _ = await self._run_steps(client_id, scope, [{"tool": "back"}])
        return ok and not await moved()

    # ---- the end ----
    async def _passed(self, client_id: str, data: dict, run: Run, answer: dict, offset: int, total: int) -> dict:
        """The whole script ran to its end. With no repair made it is a plain replay's answer; else the files are written and the
        map learns the roads that moved (§26.5)."""
        if not run.repairs:
            if run.notes:
                answer = {**answer, "text": "\n".join([answer["text"], *run.notes])}
            return answer
        clock = self.control.clock
        build = await self._build_of(run.bundle) if run.bundle else None
        at = scriptrepair.utc_now()
        scrub = self.scrub_everywhere
        repaired = scriptrepair.repaired_script(data, run.work, run.script, at, build, len(run.repairs), __version__, scrub)
        markdown = scrub(scriptrepair.repair_markdown(run.name, run.repairs, at, __version__, build))
        seconds = clock() - run.began
        n, k = total, len(run.repairs)
        first = f"Ran {n} of {n} steps in {seconds:.1f} s, with {k} {plural(k, 'repair', 'repairs')} (not adopted)."
        said = "Repaired: " + "; ".join(f"step {r.step} {scriptrepair.phrase(r)}" for r in run.repairs) + "."
        try:
            written = scriptrepair.write_files(run.script, repaired, markdown)
            wrote = f"Wrote {written[0]} and {written[1]}. The original is unchanged."
        except OSError as e:
            wrote = f"Note: couldn't write the repaired files ({e.strerror or e}); nothing was kept."
            written = []
        if written:
            self._learn_repairs(run, build)
        rest = answer["text"].split("\n")[1:]
        return {**answer, "text": "\n".join([first, said, wrote, *rest, *run.notes])}

    def _learn_repairs(self, run: Run, build: str | None) -> None:
        """A repair that passed becomes known on the map for this build (§26.5): a road that has the old move takes the new
        target in its `move` and `names`, and is proven for the build. A repaired step with no road leaves the map as it was.
        Only `apply` that passed writes the map, and only roads."""
        root, bundle = self._maps_root(), run.bundle
        if not bundle or not self._may_map(bundle):
            return
        data = self.maps.data(root, bundle)
        for record, step, cand, arrived in run.learn:
            place = step.get("place") if isinstance(step.get("place"), str) else None
            old_move = {"tool": record.tool, "target": step.get("target")}
            road = next((r for r in data["roads"] if place is not None and r["from"] == place
                         and roads._key(r["move"]) == roads._key(old_move)), None)
            if road is None or cand.target is None or road["to"] != arrived:
                continue  # no road has this move, or the repaired step didn't lead where the road does
            names = self._loaded().clean(dict(cand.names))
            self.maps.repair(root, bundle, place, road["move"], road["to"], {"tool": record.tool, "target": cand.target}, names,
                             int(road.get("ms", 0) or 0), build)

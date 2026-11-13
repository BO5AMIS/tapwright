"""The `journey` tool inside the daemon (docs/AGENT-TOOLS.md §17.1, §17.4, §17.5): start, stop, stress moments, findings,
and the checks and measuring around every call an agent makes while its journey runs. A mixin of Daemon, kept apart
from daemon.py; it uses the daemon's own tools (`run_tool`) for everything it does to the phone, so personal mode, the
safety policy and the action log hold for it as for the agent's own calls. State and numbers: journey.py."""
from __future__ import annotations

import asyncio
import datetime
import json
import logging

from . import journey, journey_export, journey_report, launch as launchlib, network, ui, uxcheck
from .control import Refused
from .device import ConnectionLost

HIDDEN = "in a journey, ids and labels are hidden; use a ref or the visible text"
NO_JUMP = ('in a journey, scroll to: "target" is off (a user can\'t jump to what is out of sight); scroll with a '
           "direction, or to: start or end, and read the screen")
NO_URL = ('blocked: in a journey, open_url is refused unless the journey\'s start allows it (allow: ["open_url"]); '
          "reach the screen by touch, as a user would.")
NO_JOURNEY = "no journey is running; start one with journey start: true and a goal"
MODES = ("start", "stop", "stress", "finding")
KEYS = {"start": ("goal", "persona", "conditions", "allow", "eyes"), "stop": ("reached", "why", "export"),
        "stress": ("mode", "target", "survives", "done"), "finding": ()}
log = logging.getLogger("tapwright")


def _d():
    from . import daemon
    return daemon


class JourneyTools:
    """Mixed into Daemon. Needs: self._journeys, self.run_tool, self.control, self.session, self.hub, self._read."""

    # ---- around every call of an agent with a journey ----
    def _journey_gate(self, jr: journey.Journey, name: str, args: dict) -> dict:
        """What a user could not do, refused before the call; the journey's conditions added to `open_app`."""
        if name == "open_app":
            given = {k: v for k, v in jr.conditions.items() if k in launchlib.KEYS and args.get(k) is None}
            args = {**args, **given} if given else args
        if name == "open_url":
            if "open_url" not in jr.allow and jr.users_eyes:
                raise Refused("not_allowed", NO_URL)
            if "open_url" in jr.allow:
                jr.shortcuts.append(f"open_url {args.get('url')}")
        if not jr.users_eyes:
            return args
        if journey.hidden(name, args):
            raise Refused("bad_params", HIDDEN)
        if name == "scroll" and args.get("to") == "target":
            raise Refused("bad_params", NO_JUMP)
        return args

    async def _journey_after(self, client_id: str, jr: journey.Journey, name: str, args: dict, answer: dict,
                             seconds: float, outer: bool, scope=None) -> None:
        """Count a call that succeeded, and keep a picture of the screen after an action."""
        text = answer.get("text") or ""
        if name in _d().BATCH_TOOLS:
            entered = jr.see(text)  # (its steps counted themselves)
        else:
            entered = jr.note_call(name, args, text, seconds, None)
        if entered and await self._journey_front_ok(jr, scope, text):  # for naming drift across the journey's screens (§17.3)
            uxcheck.note_screen(client_id, self.control.client(client_id).items)
        if outer and name in _d().JOURNEY_FRAMES and jr.current is not None and (entered or name != "wait"):
            await self._journey_frame(jr, name, text, scope)

    async def _journey_front_ok(self, jr: journey.Journey, scope, text: str) -> bool:
        """May the screen now go into the report? Personal mode: only when the app in front is one the agent was given
        (and never when the answer says `outside_apps`). Dedicated: only the journey's app, or iOS's prompt for it.
        The app is the answer's own `App:` line, else the phone's foreground app, else the last read's."""
        d = _d()
        if "outside_apps" in text:
            return False
        shown = d.APP_LINE.search(text)
        bundle = shown.group(2) if shown else None
        if bundle is None:
            try:
                bundle = await self._read(self.session.foreground_bundle_id)
            except Exception:  # noqa: BLE001 - the phone can't say: the last read does
                bundle = None
        if bundle is None:
            bundle = (self.session.app or (None, None))[1]
        return await self._journey_bundle_ok(jr, scope, bundle)

    async def _journey_bundle_ok(self, jr: journey.Journey, scope, bundle: str | None) -> bool:
        """Is a screen of this app one the report may hold? (The rule of _journey_front_ok, for a bundle already known.)"""
        d = _d()
        if scope is not None:
            return bundle in await self._given(scope)
        if jr.app is None or bundle == jr.app:
            return True
        return bundle == d.SPRINGBOARD and await self._prompt_of_given(
            {jr.app: jr.app}, self.control.client(self._journey_owner(jr)), read=False)

    async def _front_now(self) -> str | None:
        """The app in front, read through the phone's queue now; None when the phone can't say."""
        try:
            return await self._read(self.session.foreground_bundle_id) or None
        except Refused:
            return None

    def _journey_owner(self, jr: journey.Journey) -> str:
        return next((c for c, j in self._journeys.items() if j is jr), "")

    async def _journey_frame(self, jr: journey.Journey, name: str, text: str, scope=None) -> None:
        """A picture of the settled screen for the report: the live stream's newest frame, else a screenshot. Never an
        error: a picture that can't be had is a gap in the report. Left out (and counted) while another app is in front."""
        d = _d()
        if jr.current is None:
            return
        if len(jr.frames) >= journey.MAX_FRAMES:
            jr.over_cap += 1
            return
        if not await self._journey_front_ok(jr, scope, text):
            jr.left_out += 1
            return
        try:
            if scope is not None:
                # Personal mode: the picture is the phone's own screenshot, taken in one turn of the phone's queue
                # between two reads of the app in front, and kept only when both name the same given app. One turn: no
                # action or person's input can come between them (separate reads let a queued action switch to another
                # app and back, Codex, PR #7 cycle 2). A stream frame could be one from before the check, or the next
                # one the stream sends, of another app (cycle 1).
                session = self.session

                async def front() -> str | None:
                    try:
                        return await session.foreground_bundle_id() or None
                    except ConnectionLost:
                        raise  # (the read's own reconnect handles it)
                    except Exception:  # noqa: BLE001 - the phone can't say: no scope, no picture
                        return None

                async def capture() -> tuple[str | None, bytes, str | None]:
                    first = await front()
                    shot = await session.screenshot_png()
                    return first, shot, await front()
                before, source, after = await self._read(capture)
                if before is None or after != before or not await self._journey_bundle_ok(jr, scope, before):
                    jr.left_out += 1
                    return
            else:
                frame = await self._newest_frame()
                source = None
                if frame is not None:
                    try:
                        source = await d.jpeg_of(frame)
                    except d.PictureGone:
                        source = None
                    turn = getattr(self.session, "turn", 0)
                    if source is not None and turn and getattr(frame, "picture", None) is None:
                        source = await self.turned(frame.seq, source, turn)
                if source is None:
                    source = await self._read(self.session.screenshot_png)
                # Dedicated: the frame can be the next one the stream sends; the app is read again after it, and the
                # picture kept only for the journey's app (the last read's app stands in when the phone can't say)
                after = await self._front_now() or (self.session.app or (None, None))[1]
                if not await self._journey_bundle_ok(jr, scope, after):
                    jr.left_out += 1
                    return
            path = jr.folder / "screens" / f"{len(jr.frames) + 1:03d}.jpg"
            await asyncio.to_thread(journey_report.store_shot, source, path)
        except Exception as e:  # noqa: BLE001
            self.log(f"journey: no picture after {name}: {e}")
            return
        point = self.session.last_point if name in journey.TAPS else None
        caption = (text.splitlines() or [name])[0][:100]
        jr.frames.append(journey.Frame(path, caption, point, tuple(self.session.screen) if self.session.screen else None,
                                       jr.current.index, jr.elapsed(self.control.clock())))

    # ---- the tool ----
    async def _journey_tool(self, client_id: str, args: dict, scope) -> dict:
        given = [m for m in MODES if args.get(m) not in (None, False)]
        for m in MODES[:2]:
            if m in args and args[m] not in (None, True, False):
                raise Refused("bad_params", f"{m} must be true, not {args[m]!r}")
        if len(given) > 1:
            raise Refused("bad_params", f"journey takes one of {', '.join(MODES)} at a time, not {' and '.join(given)}")
        if not given:
            if args.get("eyes") is not None and client_id in self._journeys:
                return self._journey_eyes(self._journeys[client_id], args["eyes"])
            raise Refused("bad_params", "journey needs start: true (with a goal), stop: true (with reached), stress, "
                                        "finding or eyes")
        mode = given[0]
        stray = [k for k in args if k not in (mode, *KEYS[mode], *(("eyes",) if mode == "start" else ())) and args[k] is not None
                 and args[k] is not False and not (k == "done" and mode == "stress")]
        if stray:
            raise Refused("bad_params", f"journey {mode} doesn't take {', '.join(stray)}; it takes "
                                        f"{', '.join(KEYS[mode]) or 'nothing more'}")
        if mode in ("start", "stop", "stress"):
            async with self._journey_locks.setdefault(client_id, asyncio.Lock()):
                if mode == "start":
                    return await self._journey_start(client_id, args, scope)
                jr = self._journeys.get(client_id)  # (read inside: a stop may have ended it while this waited)
                if jr is None:
                    raise Refused("bad_params", NO_JOURNEY)
                if mode == "stop":
                    return await self._journey_stop(client_id, jr, args, scope)
                return await self._journey_stress(client_id, jr, args, scope)
        jr = self._journeys.get(client_id)
        if jr is None:
            raise Refused("bad_params", NO_JOURNEY)
        if mode == "finding":
            filed = jr.add_finding(args["finding"])
            n = len(jr.findings)
            where = f', screen "{filed["screen"]}"' if filed.get("screen") else ""
            return _d()._answer([f"Finding {n} filed: {filed['severity']}{where}: {filed['what']}"])
        raise Refused("bad_params", f"journey: unknown mode {mode}")  # (MODES are all handled above)

    def _journey_eyes(self, jr: journey.Journey, eyes: object) -> dict:
        if eyes not in ("user", "agent"):
            raise Refused("bad_params", f'eyes must be "user" or "agent", not {eyes!r}')
        if eyes == jr.eyes:
            return _d()._answer([f"Already in {'a user' if eyes == 'user' else 'the agent'}'s eyes."])
        jr.eyes = eyes
        jr.eyes_switches.append((jr.calls, eyes))
        if eyes == "agent":
            return _d()._answer(["Now in the agent's eyes: ids, labels and off-screen elements are shown, and the shortcuts "
                                 "are open. The report says so."])
        return _d()._answer(["Back in a user's eyes."])

    async def _calls_of_its_own(self, client_id: str, jr: journey.Journey, scope, name: str, args: dict) -> dict:
        """One of the tool's own calls: through run_tool (so scope, policy and log apply), not counted as the agent's."""
        d = _d()
        token, own = d._NESTED.set(True), d._JOURNEY_OWN.set(True)  # (of this task only: another call of the agent still goes through the gate)
        try:
            return await self.run_tool(client_id, name, args, scope)
        finally:
            d._JOURNEY_OWN.reset(own)
            d._NESTED.reset(token)

    async def _journey_start(self, client_id: str, args: dict, scope) -> dict:
        if client_id in self._journeys:
            raise Refused("bad_params", f'a journey is already running ("{self._journeys[client_id].goal}"); stop it '
                                        "first with journey stop: true and reached")
        ask = journey.check_start(args)
        cond = ask["conditions"]
        launch = launchlib.check({k: cond.get(k) for k in launchlib.KEYS})
        mode = cond.get("network")
        if mode is not None and mode not in network.MODES:
            raise Refused("bad_params", f"conditions network must be one of {', '.join(network.MODES)}, not {mode!r}")
        now = datetime.datetime.now()
        base = journey.reports_dir()
        folder = base / f"{now:%Y%m%d-%H%M%S}-{journey.slug(ask['goal'])}"
        try:
            folder.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            folder = folder.with_name(folder.name + f"-{self.control.clock():.0f}")
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise Refused("unsupported", f"can't write reports to {base}: {e.strerror or e}") from None
        jr = journey.Journey(ask["goal"], ask["persona"], {**launch, **({"network": mode} if mode else {})}, ask["allow"],
                             ask["eyes"], self.control.clock(), now, folder, app=self._opened or self._bundle())
        uxcheck.forget(client_id)  # the naming memory is this journey's alone
        self._journeys[client_id] = jr
        journey.running[client_id] = jr
        try:
            if mode not in (None, "normal"):
                await self._calls_of_its_own(client_id, jr, scope, "network", {"mode": mode})
                jr.network_set = True
            if launch and jr.app:  # the app starts again with them; a first open_app later gets them too
                await self._calls_of_its_own(client_id, jr, scope, "open_app", {"app": jr.app, "look": False, **launch})
                jr.launched_with = dict(launch)
            looked = await self._calls_of_its_own(client_id, jr, scope, "look", {"screenshot": False})
        except BaseException:  # (a cancelled start too: no journey left registered, no empty folder)
            try:
                await self._journey_undo(client_id, jr, scope, [])
            finally:  # (even when the undo raises: the next start must not find this journey registered)
                self._journeys.pop(client_id, None)
                journey.running.pop(client_id, None)
                try:
                    folder.rmdir()
                except OSError:
                    pass
            raise
        jr.see(looked["text"])
        await self._journey_frame(jr, "look", looked["text"], scope)
        who =jr.persona or "no persona"
        eyes = "in a user's eyes" if jr.users_eyes else "in the agent's eyes"
        lines = [f'Journey started: "{jr.goal}" as {who}, {eyes}.']
        if launch and not jr.launched_with:
            lines.append("Note: " + launchlib.said(launch).strip() + " will apply when you open_app the app.")
        return {**looked, "text": "\n".join([*lines, looked["text"]])}

    async def _journey_undo(self, client_id: str, jr: journey.Journey, scope, notes: list[str]) -> None:
        """What the start changed, put back: the network, and the app's launch arguments (it is closed and opened plain)."""
        if jr.network_set and self._network is not None and self._network_by == client_id:
            try:
                await self._clear_network()
                notes.append("the network condition was set back to normal.")
            except Exception as e:  # noqa: BLE001
                notes.append(f"couldn't set the network back to normal: {e}")
        jr.network_set = False
        if jr.launched_with and jr.app:
            try:
                await self._calls_of_its_own(client_id, jr, scope, "close_app", {"app": jr.app})
                await self._calls_of_its_own(client_id, jr, scope, "open_app", {"app": jr.app, "look": False})
                notes.append("the app was opened again without the journey's language, region and text size.")
            except Refused as e:
                notes.append(f"couldn't open the app again without the conditions: {e}")
        jr.launched_with = {}

    # ---- stop and the report ----
    async def _journey_stop(self, client_id: str, jr: journey.Journey, args: dict, scope) -> dict:
        reached, why = args.get("reached"), args.get("why")
        if not isinstance(reached, bool):
            raise Refused("bad_params", "journey stop needs reached: true or false")
        if why is not None and not isinstance(why, str):
            raise Refused("bad_params", "why must be text")
        spec = journey_export.check(args["export"]) if args.get("export") is not None else None
        notes: list[str] = []
        await self._journey_undo(client_id, jr, scope, notes)
        elapsed = jr.elapsed(self.control.clock())
        self._house_rules(jr, reached)
        self._journeys.pop(client_id, None)
        journey.running.pop(client_id, None)
        uxcheck.forget(client_id)
        written = None
        try:  # the journey is over now: nothing below may lose the answer
            written = await asyncio.to_thread(journey_report.write, jr, reached, why, elapsed)
        except Exception as e:  # noqa: BLE001 - OSError, or text the files can't hold
            notes.append(f"couldn't write the report to {jr.folder}: {getattr(e, 'strerror', None) or e}")
        first, second = journey.summary_line(jr, reached, elapsed), journey.findings_line(jr, jr.folder)
        summary = "\n".join([first, second])
        lines = [first, *([f"Why: {why}"] if why else []), second]
        if spec is not None and written is not None:
            try:
                lines.append(await asyncio.to_thread(journey_export.export, spec, written["folder"], summary))
            except Refused as e:
                notes.append(f"export failed, the report is still on this computer: {e}")
            except Exception as e:  # noqa: BLE001
                notes.append(f"export failed, the report is still on this computer: {e}")
        pictures = []
        for path in (written["key"] if written else []):
            try:
                data, size = journey_report.key_picture(path)
                pictures.append((data, size))
            except Exception:  # noqa: BLE001
                continue
        answer = _d()._answer(lines, notes, pictures[0][0] if pictures else None)
        if len(pictures) > 1:
            import base64
            answer["images"] = [base64.b64encode(pictures[1][0]).decode()]
        return answer

    def _house_rules(self, jr: journey.Journey, reached: bool) -> None:
        """The user's house rules (uxcheck.rules_in_force) in the report, each named; `max_taps_to_goal` is checked here:
        a journey that reached its goal in more taps breaks it, and that is a (high) finding. `way_back` shows the
        screens ux_check found without a way back; the text rules are the agent's to judge."""
        rules = uxcheck.rules_in_force()
        if not rules:
            return
        lines = [f"- {t} (judged by the agent from the screens)" for t in rules.texts]
        if rules.max_taps_to_goal is not None:
            limit = rules.max_taps_to_goal
            if reached and jr.taps > limit:
                said = f"reached the goal in {journey.plural(jr.taps, 'tap')}, more than the {limit} the rule allows"
                try:
                    jr.add_finding({"severity": "high", "kind": "max_taps_to_goal", "what": said,
                                    "basis": f"house rule: max_taps_to_goal {limit}"})
                except Refused:  # (the journey keeps at most 100 findings: the rule is still said, never raised)
                    said += " (not filed as a finding: the journey already holds the most it keeps)"
                lines.append(f"- max_taps_to_goal: {limit}: **broken**, {said}.")
            elif reached:
                lines.append(f"- max_taps_to_goal: {limit}: kept, reached in {journey.plural(jr.taps, 'tap')}.")
            else:
                lines.append(f"- max_taps_to_goal: {limit}: not applicable, the goal wasn't reached.")
        if rules.way_back:
            broken = sorted({f.get("screen_index") for f in jr.findings if f.get("kind") == "way_back"} - {None})
            lines.append("- way_back: " + (f"**broken** on screen{'s' if len(broken) != 1 else ''} "
                                           f"{', '.join(map(str, broken))}." if broken else
                                           "no screen ux_check read broke it."))
        jr.extra_sections.append(("House rules", lines))

    # ---- stress moments (§17.4) ----
    async def _journey_stress(self, client_id: str, jr: journey.Journey, args: dict, scope) -> dict:
        d = _d()
        kind = args["stress"]
        if kind not in journey.STRESSES:
            raise Refused("bad_params", f"stress must be one of {', '.join(journey.STRESSES)}, not {kind!r}")
        survives = journey.check_survives(args.get("survives"))
        label, target, mode = kind, args.get("target"), args.get("mode")
        if args.get("done") is not None and not isinstance(args["done"], bool):
            raise Refused("bad_params", "done must be true or false")
        if kind == "network":
            if mode not in network.MODES:
                raise Refused("bad_params", f"stress network needs mode: one of {', '.join(network.MODES)}")
            label = f"network {mode}"
        elif mode is not None:
            raise Refused("bad_params", "mode is for stress network")
        if kind == "double_tap":
            if target is None:
                raise Refused("bad_params", "stress double_tap needs a target")
            ui.parse_target(target)
            if jr.users_eyes and journey.hidden("tap", {"target": target}):
                raise Refused("bad_params", HIDDEN)
        elif target is not None:
            raise Refused("bad_params", "target is for stress double_tap")
        bundle = jr.app or self._opened or self._bundle()
        if kind in ("background", "relaunch") and not bundle:
            raise Refused("bad_params", "no app is open to leave; open_app the app first")
        if survives and "goal_step" in survives and not any(s.index == survives["goal_step"] for s in jr.screens):
            raise Refused("bad_params", f"survives goal_step {survives['goal_step']}: the journey has "
                                        f"{journey.plural(len(jr.screens), 'screen')} so far")
        run = lambda name, a: self._calls_of_its_own(client_id, jr, scope, name, a)  # noqa: E731
        if not args.get("done"):
            if kind == "background":
                await run("press_button", {"name": "home", "look": False})
                await self.control.sleep(d.STRESS_BACKGROUND_S)
                resume = d._RESUME.set(True)  # the same process, in front again: its language and text size stay
                try:
                    await run("open_app", {"app": bundle, "look": False})
                finally:
                    d._RESUME.reset(resume)
            elif kind == "network":
                await run("network", {"mode": mode})
                jr.network_set = mode != "normal"
            elif kind == "double_tap":
                token = d._DOUBLE.set(True)
                try:
                    await run("tap", {"target": target, "look": False})
                finally:
                    d._DOUBLE.reset(token)
            elif kind == "rotate":
                await run("rotate", {"to": "landscape_left", "look": False})
                try:
                    await self.control.sleep(d.STRESS_ROTATE_S)
                finally:
                    await run("rotate", {"to": "portrait", "look": False})
            else:  # relaunch
                await run("close_app", {"app": bundle})
                # a new start is a launch with the journey's conditions (the tool's own call skips the gate that adds them)
                await run("open_app", {"app": bundle, "look": False,
                                       **{k: v for k, v in jr.conditions.items() if k in launchlib.KEYS}})
        looked = await run("look", {"screenshot": False})
        jr.see(looked["text"])
        line = f"Stress: {label}"
        if survives:
            ok, said = self._survives(client_id, jr, survives, looked["text"])
            line += f"; it {'survived' if ok else 'didn' + chr(39) + 't survive'}: {said}"
        else:
            line += "."
        jr.stress.append(line)
        await self._journey_frame(jr, "look", looked["text"], scope)
        return {**looked, "text": "\n".join([line, looked["text"]])}

    def _survives(self, client_id: str, jr: journey.Journey, survives: dict, text: str) -> tuple[bool, str]:
        """Is what had to survive still there? `text`: on the glass (the field's value counts); `goal_step`: the screen
        now is that screen of the journey."""
        if "text" in survives:
            want = survives["text"]
            items = self.control.client(client_id).items
            items = ui.on_glass(items) if jr.users_eyes else items
            try:
                hit = ui.resolve(items, ui.Target(text=want))
            except Refused as e:
                hit = None if e.code == "not_found" else True  # (several matches: it is there)
            quoted = json.dumps(want, ensure_ascii=False)
            if hit is None:
                return False, f"text {quoted} is no longer on screen."
            where = "in the field" if isinstance(hit, ui.Element) and hit.role == "input" else "on screen"
            return True, f"text {quoted} is still {where}."
        step = next(s for s in jr.screens if s.index == survives["goal_step"])
        if journey.alike(step.signature, journey.signature(text)) >= journey.NEW_SCREEN:
            return True, f'screen {step.index} ("{step.title}") is showing again.'
        return False, f'the screen isn\'t screen {step.index} ("{step.title}") any more.'

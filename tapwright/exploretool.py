"""The `explore` tool inside the daemon (docs/AGENT-TOOLS.md §25): the call, its checks, the phone side of the search engine
(explore.py), the report (explorereport.py) and the answer. A mixin of Daemon, kept apart from daemon.py.

Every move the search makes is an ordinary action through `Daemon.run_tool` (the agent's scope, the policy, the action log), made
one at a time as the agent's own calls are, so a person's touch, a pause or a locked phone are the controller's: the phone side
below turns them into `PhoneStop` between steps (§25.9). What `explore` adds is narrowing: it asks nobody (a touch the policy holds
is refused as `needs_confirmation`, with no dialog and no live-view request) and no grant is looked at (`daemon._EXPLORING`).
"""
from __future__ import annotations

import asyncio
import dataclasses
import datetime
import io
import itertools
import re
import time
from pathlib import Path

from PIL import Image

from . import __version__, accounts as accountlib, explore, explorereport, findings, journey_report, mapkeeper, store, ui
from .control import AGENT_MAX_WAIT_S, Refused
from .explore import Check, CrashFacts, Moved, PhoneStop, Watch

NO_GUIDE = "guide isn't offered yet; explore chooses by itself."  # §25.1
NOT_A_STEP = "explore is a tool of its own, never a step of run, a script or a route"  # §25.1
IN_JOURNEY = ("in a journey, explore is off: a user can't run a search of the whole app; finish the journey, then use explore")
ALREADY = "another explore is running on this phone; wait for it to end."
KEYS = ("minutes", "steps", "depth", "from", "focus", "fill", "continue", "account", "guide")
FILL_HELP = 'fill must be "none", "test-data" or "fixture:<name>.<field>"'
FIXTURE = re.compile(r"^fixture:([A-Za-z0-9_]{1,64})\.([A-Za-z0-9_]{1,64})$")
READ_TRIES = 5  # a look that hits a dropped line is made again this many times, a second apart
POLICY_WORD = re.compile(r'policy word "([^"]*)"')
DETAIL = re.compile(r"^[a-z_]{1,40}$")
SETTLED = re.compile(r"Settled in (\d+(?:\.\d+)?) s")
AWAY = "came to the front after the touch"  # the daemon's note when another app took the front (NOTE_TAKEN)


def _d():
    from . import daemon
    return daemon


@dataclasses.dataclass(frozen=True)
class Args:
    budget: explore.Budget
    start: str | None
    focus: str | None
    fill: str
    keep_going: bool
    account: str | None


def parse(args: dict) -> Args:
    """The call's arguments, checked (§25.2): `bad_params` names what is allowed; any `guide` but `"none"` is `unsupported`."""
    unknown = [k for k in args if k not in KEYS]
    if unknown:
        raise Refused("bad_params", f"explore doesn't take {', '.join(unknown)}; it takes {', '.join(KEYS)}.")
    if args.get("guide") not in (None, "none"):
        raise Refused("unsupported", NO_GUIDE)
    for key in ("from", "focus", "account"):
        value = args.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise Refused("bad_params", f"{key} must be a non-empty name, not {value!r}")
    keep = args.get("continue", False)
    if not isinstance(keep, bool):
        raise Refused("bad_params", f"continue must be true or false, not {keep!r}")
    fill = args.get("fill", "none")
    if fill is None:
        fill = "none"
    if not isinstance(fill, str) or (fill not in ("none", "test-data") and not FIXTURE.match(fill)):
        raise Refused("bad_params", f"{FILL_HELP}, not {fill!r}")
    given = {k: args[k] for k in ("minutes", "steps", "depth") if args.get(k) is not None}
    try:
        budget = explore.Budget(**given).check()
    except ValueError as e:
        raise Refused("bad_params", str(e)) from None
    return Args(budget, args.get("from"), args.get("focus"), fill, keep, args.get("account"))


def timeout_for(args: dict) -> float:
    """How long the MCP client waits for the daemon's answer: the time budget and a minute for the report (§25.2)."""
    minutes = args.get("minutes")
    return (float(minutes) if isinstance(minutes, (int, float)) and not isinstance(minutes, bool) else explore.MINUTES) * 60 + 60


def jpeg_of(png: bytes) -> bytes:
    """A screenshot as the JPEG a finding keeps (the width a report's pictures have)."""
    img = Image.open(io.BytesIO(png))
    img.draft("RGB", (journey_report.SHOT_WIDTH, journey_report.SHOT_WIDTH * 2))
    img = img.convert("RGB")
    if img.width > journey_report.SHOT_WIDTH:
        img = img.resize((journey_report.SHOT_WIDTH, max(1, round(img.height * journey_report.SHOT_WIDTH / img.width))))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=72)
    return out.getvalue()


def issues_of(text: str) -> tuple[str, ...]:
    """The issue lines of an `audit` or `ux_check` answer: nothing for `no issues found` or `0 findings`."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines or "no issues found" in lines[0] or re.search(r": 0 findings?\.", lines[0]):
        return ()
    skip = ("Note:", "Measured:", "House rules", "(+", "Audit of", "UX check of")
    return tuple(ln for ln in lines if not ln.startswith(skip))


class DaemonPhone:
    """`explore.Phone` over the daemon: every move is `run_tool` of the agent (its scope and policy; each in the action log).
    The agent's own list is what refs mean, so a read is a `look` (no picture) and the tree is the one that look made."""

    def __init__(self, daemon, client_id: str, scope, bundle: str) -> None:
        self.d, self.cid, self.scope, self.bundle = daemon, client_id, scope, bundle
        self.screen = tuple(daemon.session.screen)
        self._waited = 0.0

    # ---- reading ----
    async def read(self) -> ui.Tree:
        for attempt in range(READ_TRIES):
            try:
                await self.d.run_tool(self.cid, "look", {"screenshot": False}, self.scope)
            except Refused as e:
                if e.code == "not_allowed" and "outside_apps" in str(e):  # personal mode withholds another app's screen
                    return ui.Tree([], app=("", "outside"))
                if e.code == "locked":
                    raise PhoneStop("locked") from None
                if e.code in ("reconnecting", "timeout") and attempt + 1 < READ_TRIES:
                    await self.d.control.sleep(1.0)
                    continue
                raise
            tree = self.d.control.client(self.cid).tree
            if tree is None:
                raise Refused("internal", "explore: the phone gave no screen to read. This is a bug; report it.")
            return self._own(tree)
        raise Refused("reconnecting", "the phone didn't answer; try again")

    def _own(self, tree: ui.Tree) -> ui.Tree:
        """Another app's screen never goes further than here: its elements are dropped, only that it is another app stays."""
        if tree.app is not None and tree.app[1] != self.bundle:
            return dataclasses.replace(tree, items=[], offscreen=[], behind=[])
        return tree

    # ---- moves ----
    def _moved(self, e: Refused | None, took: float) -> Moved:
        if e is None:
            return Moved(settled=took)
        code = "timeout" if e.code == "reconnecting" else e.code  # (nothing is learned from a dropped line: look, go on)
        word = str(e).split(":", 1)[0].strip().lower()
        return Moved(code, word if DETAIL.match(word) else "", took)

    async def _act(self, name: str, args: dict) -> Moved:
        began = self.d.control.clock()
        try:
            answer = await self.d.run_tool(self.cid, name, {**args, "look": False}, self.scope)
        except Refused as e:
            return self._moved(e, self.d.control.clock() - began)
        moved = self._moved(None, self.d.control.clock() - began)
        # a touch after which another app came to the front: the daemon brought this one back, the search records the exit (§25.7)
        return dataclasses.replace(moved, left=AWAY in answer["text"]) if name in ("tap", "toggle") else moved

    @staticmethod
    def _aim(target: ui.Target) -> dict:
        return {"ref": target.ref}

    async def tap(self, target: ui.Target) -> Moved:
        return await self._act("tap", {"target": self._aim(target)})

    async def back(self) -> Moved:
        return await self._act("back", {})

    async def scroll(self) -> Moved:
        return await self._act("scroll", {"direction": "down"})

    async def scroll_to_start(self) -> Moved:
        return await self._act("scroll", {"to": "start"})

    async def open_app(self) -> Moved:
        return await self._act("open_app", {"app": self.bundle})

    async def open_url(self, link: str) -> Moved:
        return await self._act("open_url", {"url": link})

    async def alert(self, button: str) -> Moved:
        """A permission prompt is never allowed: `dismiss` presses the alert's denial; an alert with one button is accepted."""
        shown = self.d.control.client(self.cid).alert
        only = shown is not None and len(shown[1]) == 1
        return await self._act("alert", {"action": "accept" if only else "dismiss"})

    async def type_text(self, target: ui.Target, text: str) -> Moved:
        return await self._act("type_text", {"target": self._aim(target), "text": text})  # (never `submit`, §25.6)

    async def toggle(self, target: ui.Target, value: bool) -> Moved:
        return await self._act("toggle", {"target": self._aim(target), "value": value})

    async def drive(self, place: str, account: str) -> Moved:
        return await self._act("drive", {"to": place, "account": account})

    async def check(self, target: ui.Target, tool: str = "tap") -> Check:
        """The check of `dry_run` (§11.4): a person's OK needed, blocked, or fine. Anything else the dry run refuses is the real
        touch's to say (a finding), so it is fine here."""
        args: dict = {"target": self._aim(target), "dry_run": True}
        if tool == "type_text":
            args["text"] = "Test"
        elif tool == "toggle":
            args["value"] = True
        try:
            await self.d.run_tool(self.cid, tool, {**args, "look": False}, self.scope)
        except Refused as e:
            said = str(e)
            word = POLICY_WORD.search(said)
            if e.code == "not_allowed" and said.startswith("blocked"):
                return Check("blocked", word.group(1) if word else None)
            if e.code == "not_allowed" and "needs_confirmation" in said:
                return Check("needs_person", word.group(1) if word else None)
            if e.code == "dry_run" and "needs a person's OK" in said:
                return Check("needs_person", word.group(1) if word else None)
            if e.code in ("paused", "busy", "locked"):
                raise PhoneStop(e.code) from None
        return Check()

    # ---- what the app did ----
    def mark(self) -> int:
        ring = self.d._rings.get(self.bundle)
        return ring.added if ring is not None else 0

    async def watch(self, mark: int) -> Watch:
        """The app's error-level log lines since `mark`, and whether it crashed (it isn't running and a report of it came)."""
        d = self.d
        errors: tuple[str, ...] = ()
        try:
            await d._refresh_logs(self.bundle)
            ring = d._rings.get(self.bundle)
            if ring is not None and ring.added > mark:
                shown = ring.shown()
                new = shown[max(0, len(shown) - max(0, ring.added - mark - ring.unvouched)):]
                errors = tuple(self.d.scrub_everywhere(e.text) for e in new if e.level == "error")
        except Exception:  # noqa: BLE001 - no log is no finding
            errors = ()
        crash = None
        app = self.d.control.client(self.cid).app
        if app is None or app[1] != self.bundle:  # the app isn't in front: did it stop?
            try:
                if not await asyncio.wait_for(d.session.device.app_pids(self.bundle), 5):
                    info = await d._find_crash(self.bundle, d.crash_polls)
                    if info is not None:
                        crash = CrashFacts(info.reason, tuple(info.frames))
            except Exception:  # noqa: BLE001 - the phone can't say: no crash is claimed
                crash = None
        return Watch(errors, crash)

    async def picture(self) -> bytes | None:
        """A picture of the screen, only while the explored app is in front, taken in one turn between two reads of that (§27.2:
        no picture of an app not given)."""
        session = self.d.session

        async def capture() -> tuple[str | None, bytes, str | None]:
            first = await session.foreground_bundle_id() or None
            shot = await session.screenshot_png()
            return first, shot, await session.foreground_bundle_id() or None
        before, png, after = await self.d._read(capture)
        if before != self.bundle or after != before:
            return None
        return await asyncio.to_thread(jpeg_of, png)

    async def audit(self) -> tuple[str, ...]:
        answer = await self.d.run_tool(self.cid, "audit", {}, self.scope)
        return issues_of(answer["text"])

    async def ux(self) -> tuple[str, ...]:
        answer = await self.d.run_tool(self.cid, "ux_check", {}, self.scope)
        return issues_of(answer["text"])

    # ---- a person first (§25.9) ----
    def waited(self) -> float:
        return self._waited

    async def gate(self) -> None:
        """Between steps: Pause stops the run, a locked phone stops it, and a person driving is waited out until they have been
        quiet (the controller's 3 s); after AGENT_MAX_WAIT_S of their input it stops `busy`. The wait isn't counted in `minutes`."""
        control = self.d.control
        began = control.clock()
        try:
            while True:
                if control.paused:
                    raise PhoneStop("paused")
                if self.d.state == "locked":
                    raise PhoneStop("locked")
                if not control.person_active():
                    return
                if control.clock() - began >= AGENT_MAX_WAIT_S:
                    raise PhoneStop("busy")
                await control.sleep(0.25)
        finally:
            self._waited += control.clock() - began


class ExploreTools:
    """Mixed into Daemon. Needs: self.run_tool, self.control, self.session, self.maps, self._loaded, self._given, self._read."""
    explore_home: Path | None = None  # where the reports go; None: ~/.tapwright/explore
    _explore_active: str | None = None
    _explore_seq = itertools.count(1)

    async def _explore_app(self, scope) -> str:
        """The ONE app to explore (§25.1): the app in front, else the one `open_app` started, else the only one given. Personal
        mode: only an app the agent was given, and another app in front is `outside_apps`."""
        d = _d()
        given = await self._given(scope) if scope is not None else None
        try:
            front = await self._front(given) if given is not None else await self._front_now()
        except Refused:
            front = None
        if given is not None and front not in (None, d.SPRINGBOARD) and front not in given and front != d.PROMPT_FRONT:
            raise Refused("not_allowed", d._outside(front, self.session.app_names.get(front or ""), ", ".join(given.values())))
        candidates = [b for b in (front if front not in (None, d.SPRINGBOARD, getattr(d, "PROMPT_FRONT", None)) else None,
                                  self._opened) if b]
        if given is not None and len(given) == 1:
            candidates.append(next(iter(given)))
        for bundle in candidates:
            if given is None or bundle in given:
                return bundle
        raise Refused("not_found", "no app to explore: open the app with open_app first")

    def _explore_fill(self, parsed: Args, bundle: str) -> tuple[explore.Fill, str | None]:
        """`fill` and `account` checked against the vault (§25.6, §25.8): what they name must be marked `"explore": true`."""
        loaded = self._loaded()
        fill = explore.Fill()
        if parsed.fill == "test-data":
            fill = explore.Fill("test-data")
        elif parsed.fill != "none":
            name, field = FIXTURE.match(parsed.fill).groups()  # type: ignore[union-attr]
            self._vault_ready(loaded)
            entry = loaded.vault.get(name)
            if entry is None or entry.kind != accountlib.FIXTURE:
                raise Refused("bad_params", f'fill: "{name}" isn\'t a vault entry of the kind fixture')
            if not entry.explore:
                raise Refused("bad_params", f'fill: the fixture "{name}" isn\'t marked "explore": true in the vault file')
            if field not in entry.values:
                raise Refused("bad_params", f'fill: the fixture "{name}" has no field "{field}"')
            if entry.apps is not None and bundle not in entry.apps:
                raise Refused("not_allowed", f'fill: the fixture "{name}" may be typed only in {", ".join(entry.apps)}')
            fill = explore.Fill("fixture", f"${{vault.{name}.{field}}}")
        account = None
        if parsed.account is not None:
            self._vault_ready(loaded)
            loaded.account(parsed.account, bundle)  # not_found, not_allowed and bad_params as `drive` gives them
            entry = loaded.vault.get(parsed.account)
            if entry is None or not entry.explore:
                raise Refused("bad_params", f'account "{parsed.account}" isn\'t marked "explore": true in the vault file')
            account = parsed.account
        return fill, account

    @staticmethod
    def _vault_ready(loaded) -> None:
        if not loaded.path:
            raise Refused("bad_params", accountlib.NO_FILE)
        if loaded.problem is not None:
            raise Refused("bad_params", loaded.problem)

    def _explore_identity(self, bundle: str, build: str | None) -> explore.Identity:
        text = build or "unknown"
        version = text.split(" (")[0] if build else "unknown"
        rsd = getattr(self.session.device, "rsd", None)
        model, system = getattr(rsd, "product_type", None), getattr(rsd, "product_version", None)
        device = findings.Device(str(model) if isinstance(model, str) and model else "iPhone",
                                 f"iOS {system}" if isinstance(system, str) and system else "iOS")
        return explore.Identity(bundle, version, text, device, __version__, "tapwright", self.scrub_everywhere, False)

    async def _explore_tool(self, client_id: str, args: dict, scope) -> dict:
        started = time.monotonic()
        self._announced = None
        try:
            answer = await self._explore_run(client_id, args, scope)
        except Refused as e:
            self._log_action("explore", args, started, e, client=client_id)
            raise
        self._log_action("explore", args, started, None, client=client_id, answer=answer)
        return answer

    async def _explore_run(self, client_id: str, args: dict, scope) -> dict:
        d = _d()
        if d._NESTED.get() or d.BATCH.get() is not None:
            raise Refused("bad_params", NOT_A_STEP)
        if client_id in self._journeys:
            raise Refused("not_allowed", IN_JOURNEY)
        if scope is not None and not scope.apps:
            raise Refused("not_allowed", d.NO_APPS)
        parsed = parse(args)
        bundle = await self._explore_app(scope)
        fill, account = self._explore_fill(parsed, bundle)
        root = self._maps_root()
        places = self.maps.data(root, bundle).get("places", {})
        for key, name in (("from", parsed.start), ("focus", parsed.focus)):
            if name is not None and name not in places:
                raise Refused("not_found", f'{key}: no place called "{name}"; the places are: '
                                           f'{", ".join(sorted(places)[:30]) or "none yet (drive or explore first)"}')
        if self._explore_active is not None:
            raise Refused("busy", ALREADY)
        self._explore_active = client_id
        build = await self._build_of(bundle)
        who = self._explore_identity(bundle, build)
        self.ensure_logs(bundle)
        phone = DaemonPhone(self, client_id, scope, bundle)
        channel = self.dialog_channel(client_id)

        def progress(line: str) -> None:
            channel.push({"type": "progress", "id": f"p{next(self._explore_seq)}", "message": line})
        explorer = explore.Explorer(phone, explore.MapsPort(self.maps, root, bundle), d._POLICY.get(), who, budget=parsed.budget,
                                    start_place=parsed.start, focus=parsed.focus, continue_after_crash=parsed.keep_going,
                                    clock=self.control.clock, progress=progress, fill=fill, account=account)
        tokens = (d._NESTED.set(True), d._EXPLORING.set(True), d._DIALOG.set(None))  # nobody is asked; each move is the tool's own
        began = datetime.datetime.now(datetime.timezone.utc)
        try:
            result = await explorer.run()
        finally:
            d._DIALOG.reset(tokens[2])
            d._EXPLORING.reset(tokens[1])
            d._NESTED.reset(tokens[0])
            self._explore_active = None
        await self.maps.flush()
        folder, note = await self._explore_write(result, who, parsed.budget, began)
        if result.failure is not None:
            raise Refused(result.failure.code, self.scrub_everywhere(f"{result.failure} {explorereport.done_line(result, folder)}"))
        if result.stopped in explorereport.ERRORS:
            raise Refused(result.stopped, explorereport.error_text(result, folder, self.scrub_everywhere))
        text = explorereport.answer(result, folder, self.scrub_everywhere)
        return {"text": text + (f"\nNote: {note}" if note else ""), "image": None, "image_size": None}

    async def _explore_write(self, result: explore.Result, who: explore.Identity, budget: explore.Budget,
                             began: datetime.datetime) -> tuple[Path | None, str | None]:
        """The run's folder (§25.10), or why none could be written; the run is never lost to a disk that refuses."""
        root = self.explore_home if self.explore_home is not None else store.HOME / "explore"
        ended = datetime.datetime.now(datetime.timezone.utc)
        try:
            folder = await asyncio.to_thread(explorereport.write, root, result, who, budget, began, ended, self.scrub_everywhere)
        except OSError as e:
            return None, self.scrub_everywhere(f"the report couldn't be written: {e.strerror or e}")
        return folder, None

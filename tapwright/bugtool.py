"""The `bug_report` tool inside the daemon (AGENT-TOOLS 1.16.2 §29): one page a person hands to whoever fixes the bug, made
from a finding of an explore report, from the session's last failed check, or from what the session's touches were
(`from: "now"`). A mixin of Daemon, kept apart from daemon.py; the making is bugmake.py, the folder bugstore.py.

What the daemon keeps for it: every value typed in this session, registered before it is sent (accounts.Store.remember_typed,
never the explorer's public fake values), the last touches of the session (`_bug_trail`, what the action log says: a step as a
road names it, the place it ran on, never a typed value), and each agent's last failed `run` or `replay` (`_bug_failures`).

Part 2 of 1.8.5 adds the reproduction check (§29.4) and the export (§29.7): until then `verify` and `export` are refused as
`unsupported`, and a bug made with the default check says `not checked` with the reason. `BugTools._bug_check` and
`BugTools._bug_export` are the seams.
"""
from __future__ import annotations

import asyncio
import collections
import datetime
from pathlib import Path

from . import bugmake, bugstore, explore, explorefloor, findings, store, ui
from .control import Refused

KEYS = ("finding", "run", "from", "note", "expected", "verify", "reset", "account", "bug", "with_picture", "export")
TRAIL = 50  # the touches the daemon keeps for `from: "now"` (it uses the last 10 of the app)
NOT_BUILT = "part of 1.8.5, not built yet: the bug was not made; leave {what} out to make it now"
CHECK_NOT_BUILT = "the reproduction check is part of 1.8.5, not built yet"
NO_LOG = ('from "now" needs the action log, and it is off (tapwright serve --no-log): the last touches aren\'t known, so '
          "nothing can say how to see it.")
NO_TOUCH = 'from "now": no touch in the action log of this app yet; drive to the bug first, then report it.'
NO_FAILURE = 'from "failure": this session\'s last run or replay didn\'t fail a check (expect_failed); there is none to report.'


def _d():
    from . import daemon
    return daemon


def utc_ms(when: datetime.datetime) -> str:
    when = when.astimezone(datetime.timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:%S") + f".{when.microsecond // 1000:03d}Z"


def parse(args: dict) -> dict:
    """The call's arguments, checked (§29.3). `bad_params` names what is wrong; `verify` (a number of runs) and `export` are
    `unsupported` until part 2."""
    unknown = [k for k in args if k not in KEYS]
    if unknown:
        raise Refused("bad_params", f"bug_report doesn't take {', '.join(unknown)}; it takes {', '.join(KEYS)}.")
    given = [k for k in ("finding", "from", "bug") if args.get(k) is not None]
    if len(given) != 1:
        raise Refused("bad_params", 'give exactly one of finding (with run), from ("now" or "failure") and bug.')
    source = given[0]
    if source == "from" and args["from"] not in ("now", "failure"):
        raise Refused("bad_params", f'from is "now" or "failure", not {args["from"]!r}')
    if args.get("run") is not None and source != "finding":
        raise Refused("bad_params", "run goes with finding")
    if source != "from" or args["from"] != "now":
        for key in ("note", "expected"):
            if args.get(key) is not None:
                raise Refused("bad_params", f'{key} goes with from "now"')
    for key in ("with_picture",):
        if args.get(key) is not None and not isinstance(args[key], bool):
            raise Refused("bad_params", f"{key} must be true or false")
    verify = args.get("verify")
    if verify is not None and (not isinstance(verify, (bool, int)) or (not isinstance(verify, bool) and not 0 <= verify <= 10)):
        raise Refused("bad_params", "verify is how many runs check it: 1 to 10, true for 3, false or 0 for none")
    if args.get("reset") not in (None, "own", "app"):
        raise Refused("bad_params", f'reset is "own" or "app", not {args["reset"]!r}')
    if args.get("account") is not None and not isinstance(args["account"], str):
        raise Refused("bad_params", "account is a vault account's name")
    if verify not in (None, False, 0):
        raise Refused("unsupported", "verify: the reproduction check is " + NOT_BUILT.format(what="verify"))
    if args.get("export") is not None:
        raise Refused("unsupported", "export is " + NOT_BUILT.format(what="export").replace("the bug was not made; ", ""))
    if source == "finding" and (not isinstance(args["finding"], str) or not args["finding"].strip()):
        raise Refused("bad_params", 'finding is a finding\'s id, like "f3"')
    if source == "bug" and (not isinstance(args["bug"], str) or not args["bug"].strip()):
        raise Refused("bad_params", 'bug is a kept bug\'s id, like "bug_7bc2e91c152d416e"')
    if args.get("run") is not None and (not isinstance(args["run"], str) or not args["run"].strip()):
        raise Refused("bad_params", 'run is an explore report folder or "last"')
    return {"source": source, "checked": verify is None and not (source == "from" and args["from"] == "now")}


class BugTools:
    """Mixed into Daemon. Needs: self.accounts, self.maps, self.session, self.control, self.actions, self._loaded,
    self._maps_root, self._build_of, self._bundle, self._opened, self._rings, self.ensure_logs, self._refresh_logs."""
    bug_home: Path | None = None  # where the bugs go; None: ~/.tapwright/bugs
    _bug_trail_q: "collections.deque | None" = None
    _bug_failures_d: dict | None = None

    # ---- what the session keeps for it ----
    @property
    def _bug_trail(self) -> collections.deque:
        if self._bug_trail_q is None:
            self._bug_trail_q = collections.deque(maxlen=TRAIL)
        return self._bug_trail_q

    @property
    def _bug_failures(self) -> dict:
        if self._bug_failures_d is None:
            self._bug_failures_d = {}
        return self._bug_failures_d

    def _bug_typed(self, name: str, args: dict) -> None:
        """Before a `type_text` is sent: its value is kept out of every bug report (§29.6). The explorer's fake values are
        public (§25.6) and aren't kept."""
        if name == "type_text" and not _d()._EXPLORING.get():
            try:
                self.accounts.remember_typed(args.get("text"))
            except Exception:  # noqa: BLE001 - the typing goes on; the report's gate still greps for vault values
                pass

    def _bug_aim(self, client_id: str, name: str, args: dict) -> tuple[tuple[str, str] | None, dict | None]:
        """Before a touch: the place it starts on and its target as a recording would save it (id, else label, else text;
        a ref turned into the element it names). Never fails the touch."""
        if name not in bugmake.NOW_TOUCHES or args.get("dry_run"):
            return None, None
        try:
            view = self.control.client(client_id)
            from . import batch
            aimed = None if batch.holds_value(args.get("target")) else batch.recorded_target(args.get("target"), view.items, name)
            return self._place_before(view), aimed
        except Exception:  # noqa: BLE001 - a touch that can't be named starts the path after it
            return None, None

    async def _bug_failure_seen(self, client_id: str, failed: tuple[str, str] | None, steps: list[dict], at: int,
                                start: tuple[str, str] | None, end: tuple[str, str] | None, logs: list[str],
                                expect: dict | None) -> None:
        """The end of an agent's own `run` or `replay`: one that stopped `expect_failed` is kept for `from: "failure"`;
        any other end clears it (only the last one counts, §29.3)."""
        if failed is None or failed[0] != "expect_failed" or not isinstance(expect, dict):
            self._bug_failed(client_id, None)
            return
        try:
            bundle = self._bundle()
            if bundle is None:
                self._bug_failed(client_id, None)
                return
            who = self._explore_identity(bundle, await self._build_of(bundle))
            identity = {"app_version": who.app_version, "build": explore._build_number(who.build),
                        "tool_version": who.tool_version, "device": {"model": who.device.model, "os": who.device.os}}
            shown = ", ".join(f"{k} {v!r}" for k, v in expect.items() if k in ("text", "id", "label", "matches", "gone"))
            got = failed[1].split("\n", 1)[0]
            got = got.split(": its action ran, but ", 1)[-1] if ": its action ran, but " in got else got
            record = bugmake.failure_record(
                app=bundle, identity=identity, when=utc_ms(datetime.datetime.now(datetime.timezone.utc)), steps=steps,
                failed_at=at, expected=f"step {at} holds its check ({shown or 'expect'})", got=got,
                path_from=start[1] if start is not None and start[0] == bundle else None,
                place=end[1] if end is not None and end[0] == bundle else None, logs=logs)
        except Exception:  # noqa: BLE001 - a failure that can't be kept is no failure to report
            record = None
        self._bug_failed(client_id, record)

    def _bug_touch(self, name: str, args: dict, aimed: dict | None, place: tuple[str, str] | None, by: str = "agent") -> None:
        """A touch that went through, as `from: "now"` reads it (§29.3): never a typed value, a target as a road names it."""
        if name not in bugmake.NOW_TOUCHES or args.get("dry_run"):
            return
        bundle = self._bundle()
        entry: dict = {"tool": name, "ok": True, "app": bundle, "by": by,
                       "place": place[1] if place is not None and place[0] == bundle else None}
        if isinstance(aimed, dict) and any(isinstance(aimed.get(k), str) and aimed[k] for k in ("id", "label", "text")):
            entry["target"] = {k: v for k, v in aimed.items() if k in ("id", "label", "text", "index")}
        elif name in bugmake.ELEMENT_TOOLS or args.get("target") is not None:
            entry["target"] = {"point": True}  # (a point or a ref that can't be named: the path starts after it)
        for key in ("direction", "to", "url", "x0", "y0", "x1", "y1"):
            if args.get(key) is not None:
                entry[key] = args[key]
        self._bug_trail.append(entry)

    def _bug_person(self, kind: str) -> None:
        """A person's input in the live view: it has no name to write down (a point), so a path starts after it."""
        self._bug_trail.append({"tool": "tap", "ok": True, "app": self._bundle(), "by": "person", "target": {"point": True}})

    def _bug_failed(self, client_id: str, record: dict | None) -> None:
        if record is None:
            self._bug_failures.pop(client_id, None)
        else:
            self._bug_failures[client_id] = record

    # ---- the tool ----
    def _bugs(self) -> bugstore.BugStore:
        return bugstore.BugStore(self.bug_home if self.bug_home is not None else store.HOME / "bugs")

    def _bug_explore_home(self) -> Path:
        home = getattr(self, "explore_home", None)
        return home if home is not None else store.HOME / "explore"

    def _bug_context(self) -> bugmake.Context:
        try:
            self._loaded()  # (the live values: a vault file changed since is read now, §29.6)
        except Exception:  # noqa: BLE001
            pass
        root = self._maps_root()
        d = _d()
        return bugmake.Context(self._bugs(), self.accounts.bug_scrub, self.accounts.bug_gate,
                               lambda app: self.maps.data(root, app), d._FLOOR_RULES.get() or d.safety.NONE)

    async def _bug_tool(self, client_id: str, args: dict, scope) -> dict:
        d = _d()
        if d._NESTED.get() or d.BATCH.get() is not None:
            raise Refused("bad_params", "bug_report is a tool of its own, never a step of run or a script")
        parsed = parse(args)
        ctx = self._bug_context()
        reproduced = {"checked": False, "why": CHECK_NOT_BUILT} if parsed["checked"] else {"checked": False, "why": "not asked"}
        with_picture = bool(args.get("with_picture"))
        try:
            if parsed["source"] == "bug":
                made = await self._bug_again(ctx, args["bug"].strip(), with_picture)
            elif parsed["source"] == "finding":
                got = await asyncio.to_thread(self._bug_run_finding, args.get("run") or "last", args["finding"].strip())
                made = await asyncio.to_thread(bugmake.from_run, ctx, got, with_picture=with_picture, reproduced=reproduced)
            elif args["from"] == "failure":
                failure = self._bug_failures.get(client_id)
                if failure is None:
                    raise Refused("not_found", NO_FAILURE)
                made = await asyncio.to_thread(bugmake.from_failure, ctx, failure, with_picture=with_picture,
                                               reproduced=reproduced)
            else:
                made = await self._bug_now(ctx, client_id, scope, args, with_picture)
        except bugmake.BugError as e:
            raise Refused(e.code, ctx.safe()(str(e))) from None
        except bugstore.BugGateError as e:
            raise Refused("not_allowed", f"the bug was not made: {e} Nothing was written.") from None
        except findings.FindingError as e:
            raise Refused("bad_params", ctx.safe()(f"the finding can't be made into a bug: {e}")) from None
        text = bugmake.answer(made, ctx.scrub, reason_shown=parsed["checked"] or parsed["source"] == "bug",
                              home=Path.home())
        return {"text": text, "image": None, "image_size": None}

    def _bug_app_hint(self) -> str | None:
        front = self._bundle()
        d = _d()
        return front if front and front != d.SPRINGBOARD else self._opened

    def _bug_run_finding(self, run: str, finding_id: str) -> bugmake.RunFinding:
        home = self._bug_explore_home()
        if run == "last":
            app = self._bug_app_hint()
            folder = bugmake.newest_run(home, app) if app else None
            if folder is None:
                raise bugmake.BugError("not_found", f"no explore report of {app or 'the app in front'} yet; explore first, "
                                                    "or give run the report's folder.")
        else:
            given = Path(run).expanduser()
            folder = given if given.is_absolute() else home / given
        return bugmake.read_run(home, folder, finding_id)

    async def _bug_again(self, ctx: bugmake.Context, bug_id: str, with_picture: bool) -> bugstore.Made:
        """A kept bug (§29.3 `bug`): its answer again, and with `with_picture` its own private picture added while its source
        file still exists. An id two apps hold is `ambiguous` unless the app in front holds it (§29.7, 1.16.2)."""
        bugs = ctx.store
        holders = await asyncio.to_thread(bugs.holders, bug_id)
        if not holders:
            raise Refused("not_found", f'no bug "{bug_id}" is kept; tapwright bug list names them.')
        app = holders[0]
        if len(holders) > 1:
            front = self._bug_app_hint()
            if front not in holders:
                raise Refused("ambiguous", f"{bug_id} is a bug of {', '.join(holders)}: open the one meant with open_app first.")
            app = front
        held = bugs.read(app, bug_id)
        if held is None:
            raise Refused("not_found", f'no bug "{bug_id}" is kept.')
        if with_picture and not held.meta.get("picture_included") and isinstance(held.finding.get("picture"), dict):
            picture = bugmake.picture_again(self._bug_explore_home(), held.meta.get("picture_source"))
            if picture is None:
                raise Refused("not_found", f"{bug_id}: its picture's source file is gone, so it can't be added.")
            return await asyncio.to_thread(bugs.add_picture, held, picture, ctx.gate)
        return bugstore.Made(held.meta, held.folder, False, held.title, held.finding)

    async def _bug_now(self, ctx: bugmake.Context, client_id: str, scope, args: dict, with_picture: bool) -> bugstore.Made:
        """`from: "now"` (§29.3): the screen now and the last touches of the action log."""
        note = bugmake.check_note(args.get("note"), "note", True)
        expected = bugmake.check_note(args.get("expected"), "expected", False)
        if self.actions.folder is None:
            raise Refused("unsupported", NO_LOG)
        snap = await self._read(lambda: self.session.snapshot())
        if snap.app is None:
            raise Refused("not_found", "no app is in front: open the app the bug is in first")
        bundle = snap.app[1]
        d = _d()
        if bundle == d.SPRINGBOARD:
            raise Refused("not_found", "the home screen is in front: open the app the bug is in first")
        place = key = None
        root = self._maps_root()
        if self._may_map(bundle):
            seen = self.maps.observe(root, bundle, snap.tree, arrived=False, lang=self._language.get(bundle))
            if seen is not None:
                place = seen.name
                key = bugmake.place_key_of(ctx, bundle, place)
        observed = bugmake.observed_path(list(self._bug_trail), bundle, place)
        if observed is None:
            raise Refused("not_found", NO_TOUCH)
        build = await self._build_of(bundle)
        who = self._explore_identity(bundle, build)
        identity = {"app_version": who.app_version, "build": explore._build_number(who.build), "tool_version": who.tool_version,
                    "device": {"model": who.device.model, "os": who.device.os}}
        picture = None
        try:
            from .exploretool import DaemonPhone
            picture = await DaemonPhone(self, client_id, scope, bundle).picture()
        except Refused:
            raise
        except Exception:  # noqa: BLE001 - a picture that can't be had is a gap in the page, not a reason to lose the bug
            picture = None
        when = utc_ms(datetime.datetime.now(datetime.timezone.utc))
        return await asyncio.to_thread(
            bugmake.from_now, ctx, app=bundle, note=note, expected=expected, identity=identity, when=when,
            observed=observed, place=place, place_key=key, logs=await self._bug_logs(bundle), picture=picture,
            system=system_of(snap.tree), with_picture=with_picture)

    async def _bug_logs(self, bundle: str) -> list[str]:
        """The app's last 20 log lines of any level (§29.3), each `HH:MM:SS.mmm level text`; redacted when the bug is made."""
        try:
            self.ensure_logs(bundle)
            await self._refresh_logs(bundle)
            ring = self._rings.get(bundle)
        except Exception:  # noqa: BLE001 - no log is no log lines
            return []
        if ring is None:
            return []
        out = []
        for e in ring.shown()[-findings.MAX_LOGS:]:
            at = datetime.datetime.fromtimestamp(e.at / 1000, datetime.timezone.utc)
            out.append(f"{at.strftime('%H:%M:%S')}.{at.microsecond // 1000:03d} {e.level} {e.text}")
        return out

    # ---- part 2's seams (§29.4, §29.7) ----
    async def _bug_check(self, held: bugstore.Kept, runs: int, reset: str, account: str | None) -> dict:
        """The reproduction check (§29.4): `{"n", "of", "build", "when", "reset"}` or `{"checked": false, "why"}`. Part 2."""
        raise Refused("unsupported", "verify: the reproduction check is " + NOT_BUILT.format(what="verify"))

    async def _bug_export(self, held: bugstore.Kept, export: dict) -> str:
        """The export (§29.7): a GitHub issue or a `.zip`, each re-gated, with its audit line. Part 2."""
        raise Refused("unsupported", "export is part of 1.8.5, not built yet")


def system_of(tree: ui.Tree) -> str | None:
    """The kind of a system sheet or prompt in front (§29.8), never its content: the share sheet in the app's tree, an
    alert that asks for a permission. An app's own alert is no system sheet."""
    if explorefloor.system_sheet(tree):
        return "share sheet"
    alert = tree.alert
    if alert is not None:
        words = f"{alert.title} {alert.message}"
        if "Would Like to" in words or "would like to" in words or "Allow “" in words or 'Allow "' in words:
            return "permission prompt"
    return None

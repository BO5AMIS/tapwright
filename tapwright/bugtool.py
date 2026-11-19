"""The `bug_report` tool inside the daemon (AGENT-TOOLS 1.16.2 §29): one page a person hands to whoever fixes the bug, made
from a finding of an explore report, from the session's last failed check, or from what the session's touches were
(`from: "now"`). A mixin of Daemon, kept apart from daemon.py; the making is bugmake.py, the folder bugstore.py.

What the daemon keeps for it: every value typed in this session, registered before it is sent (accounts.Store.remember_typed,
never the explorer's public fake values), the last touches of the session (`_bug_trail`, what the action log says: a step as a
road names it, the place it ran on, never a typed value), and each agent's last failed `run` or `replay` (`_bug_failures`).

The call makes the bug first, then checks it (§29.4, `_bug_check`: bugcheck.py, every move through the one dispatch gate),
then exports it when asked (§29.7, `_bug_export`: bugexport.py, re-gated, with its audit line). A check that can't run never
stops the bug from being made: the page says why.
"""
from __future__ import annotations

import asyncio
import collections
import datetime
import json
from pathlib import Path

from . import (batch, bugcheck, bugexport, bugmake, bugstore, explore, explorefloor, findings, links, places, roads, routes,
               store, ui)
from .control import Refused

KEYS = ("finding", "run", "from", "note", "expected", "verify", "reset", "account", "bug", "with_picture", "export")
TRAIL = 50  # the touches the daemon keeps for `from: "now"` (it uses the last 10 of the app)
NOT_ASKED = bugmake.bug.NOT_ASKED
NO_PHONE = "no phone connected"
CHECK_BUSY = "stopped (busy): an explore or another bug check is running on this phone"
THIN = "\x00thin-call"  # (W177) the key of the thin calls' last failed check: no client id can be it
BATCHES_KEPT = 8 # the runs whose touches are kept by step, for the failure of the one that ends (`_bug_batch_aims`)
ACCOUNT_NOTE = ("Note: account isn't used: the check relaunches the app, which keeps its data (a sign-in included), and never "
                "signs in.")
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
    """The call's arguments, checked (§29.3), before anything is made. `bad_params` names what is wrong. The answer: the
    source, `runs` (how many reproduction runs: the source's default when `verify` isn't given) and `export` (checked)."""
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
    export = bugexport.check(args["export"]) if args.get("export") is not None else None
    if source == "finding" and (not isinstance(args["finding"], str) or not args["finding"].strip()):
        raise Refused("bad_params", 'finding is a finding\'s id, like "f3"')
    if source == "bug" and (not isinstance(args["bug"], str) or not args["bug"].strip()):
        raise Refused("bad_params", 'bug is a kept bug\'s id, like "bug_7bc2e91c152d416e"')
    if args.get("run") is not None and (not isinstance(args["run"], str) or not args["run"].strip()):
        raise Refused("bad_params", 'run is an explore report folder or "last"')
    now = source == "from" and args["from"] == "now"
    default = 0 if now or source == "bug" else bugcheck.RUNS_DEFAULT
    return {"source": source, "runs": bugcheck.runs_of(verify, default), "export": export,
            "reset": args.get("reset") or "own", "account": args.get("account")}


class BugTools:
    """Mixed into Daemon. Needs: self.accounts, self.maps, self.session, self.control, self.actions, self._loaded,
    self._maps_root, self._build_of, self._bundle, self._opened, self._rings, self.ensure_logs, self._refresh_logs."""
    bug_home: Path | None = None  # where the bugs go; None: ~/.tapwright/bugs
    _bug_trail_q: "collections.deque | None" = None
    _bug_failures_d: dict | None = None
    _bug_batch_aims_d: dict | None = None

    @property
    def _bug_batch_aims(self) -> dict:
        """{run id: {step number: its trail entry}}: how each touch of a running `run` or `replay` was named (review R185
        M1, M4), for the failure kept when it ends."""
        if self._bug_batch_aims_d is None:
            self._bug_batch_aims_d = {}
        return self._bug_batch_aims_d

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
        a ref turned into the element it names). A row of a list with no id of its own, or one whose id carries content,
        is the n-th row, never its text (§29.1, §18.1; review R185 M1). A tool's own move (explore's, a check's, a
        drive's) is not the agent's: nothing (L7). Never fails the touch."""
        d = _d()
        if name not in bugmake.NOW_TOUCHES or args.get("dry_run") or d._NESTED.get() or d._EXPLORING.get():
            return None, None
        try:
            view = self.control.client(client_id)
            target = args.get("target")
            if batch.holds_value(target):
                return self._place_before(view), None
            row = self._bug_row(view.items, target, name)
            aimed = row if row is not None else batch.recorded_target(target, view.items, name)
            return self._place_before(view), aimed
        except Exception:  # noqa: BLE001 - a touch that can't be named starts the path after it
            return None, None

    @staticmethod
    def _bug_row(items: list, target: object, tool: str) -> dict | None:
        """`{"row": true, "index": n}` when the target resolves to a row whose words are content (roads.move_of's rule:
        a cell with no id, or an element whose id carries content), else None."""
        if not isinstance(target, dict) or target.get("x") is not None:
            return None
        try:
            found = ui.replayed(items, ui.parse_target(target), tool)
        except Refused:
            return None
        if not isinstance(found, ui.Element):
            return None
        if not ((found.role == "cell" and not found.identifier) or roads.content_id(found)):
            return None
        cells = [e for e in items if e.role == "cell" and not e.in_alert]
        index = next((n for n, e in enumerate(cells, 1) if e is found or e.ref == found.ref), 1)
        return {"row": True, "index": index}

    async def _bug_failure_seen(self, client_id: str, failed: tuple[str, str] | None, steps: list[dict], at: int,
                                start: tuple[str, str] | None, end: tuple[str, str] | None, logs: list[str],
                                expect: dict | None, run_id: str | None = None) -> None:
        """The end of an agent's own `run` or `replay`: one that stopped `expect_failed` is kept for `from: "failure"`;
        any other end clears it (only the last one counts, §29.3)."""
        aims = self._bug_batch_aims.pop(run_id, {}) if run_id is not None else {}
        if failed is None or failed[0] != "expect_failed" or not isinstance(expect, dict):
            self._bug_failed(client_id, None)
            return
        record = None
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
            # (W177b) the place the run's answer named (its read after the run), else the newest read's; and the app's own
            # log lines, as from "now" keeps them, never the run's whole "Logs (since step n):" block with iOS's lines
            place = end[1] if end is not None and end[0] == bundle else self._bug_place_read(bundle)
            record = bugmake.failure_record(
                app=bundle, identity=identity, when=utc_ms(datetime.datetime.now(datetime.timezone.utc)), steps=steps,
                failed_at=at, expected=f"step {at} holds its check ({shown or 'expect'})", got=got,
                path_from=start[1] if start is not None and start[0] == bundle else None,
                place=place, logs=await self._bug_logs(bundle), aims=aims,
                link_of=lambda url: self._bug_link(bundle, url))
        except Exception as e:  # noqa: BLE001 - a failure that can't be kept is no failure to report; said in serve.log
            record = None
            try:
                said = findings.safe_scrub(self.accounts.bug_scrub)(str(e))[:200]
            except Exception:  # noqa: BLE001
                said = ""
            self.log(f'bug_report: a failed check couldn\'t be kept for from "failure" ({type(e).__name__}: {said})')
        self._bug_failed(client_id, record)

    def _bug_place_read(self, bundle: str) -> str | None:
        """The place the newest read of the screen is (the session's last tree, as a look would save it), or None."""
        tree = getattr(self.session, "last_tree", None)
        if tree is None or tree.app is None or tree.app[1] != bundle or not self._may_map(bundle):
            return None
        try:
            seen = self.maps.observe(self._maps_root(), bundle, tree, arrived=False, lang=self._language.get(bundle))
        except Exception:  # noqa: BLE001 - a place that can't be read is a place not known
            return None
        return seen.name if seen is not None else None

    def _bug_failed(self, client_id: str, record: dict | None) -> None:
        """Keep (or clear) a client's last failed check. A thin `tapwright call` is a process of its own, and its default
        client is its parent process (a new shell for each command): its `run` and its `bug_report` reach the daemon as two
        clients. So a run that came by the thin call is also kept as the thin calls' last one (`THIN`), which a thin call
        with none of its own reads (W177: every from "failure" was not_found on the phone)."""
        for key in (client_id, THIN) if _d()._VIA.get() == "call" else (client_id,):
            if record is None:
                self._bug_failures.pop(key, None)
            else:
                self._bug_failures[key] = record

    def _bug_failure_of(self, client_id: str) -> dict | None:
        own = self._bug_failures.get(client_id)
        if own is None and _d()._VIA.get() == "call":
            return self._bug_failures.get(THIN)
        return own

    def _bug_touch(self, name: str, args: dict, aimed: dict | None, place: tuple[str, str] | None, by: str = "agent",
                   after: tuple[str, str] | None = None) -> None:
        """A touch that went through, as `from: "now"` reads it (§29.3): never a typed value, a target as a road names it,
        a content row as the n-th row (M1), a link only as its place's pattern (§18.3: a link may carry a secret in its
        query or its path; review R185 H1). A tool's own move is not the agent's (L7). `after`: the place the touch's own
        read left, which is where the next touch starts when that one's isn't known (W177)."""
        d = _d()
        if name not in bugmake.NOW_TOUCHES or args.get("dry_run") or d._NESTED.get() or d._EXPLORING.get():
            return
        bundle = self._bundle()
        entry: dict = {"tool": name, "ok": True, "app": bundle, "by": by,
                       "place": place[1] if place is not None and place[0] == bundle else None,
                       "after": after[1] if after is not None and after[0] == bundle else None}
        if isinstance(aimed, dict) and aimed.get("row") is True:
            entry["target"] = {"row": True, "index": aimed.get("index", 1)}
        elif isinstance(aimed, dict) and any(isinstance(aimed.get(k), str) and aimed[k] for k in ("id", "label", "text")):
            entry["target"] = {k: v for k, v in aimed.items() if k in ("id", "label", "text", "index")}
        elif name in bugmake.ELEMENT_TOOLS or args.get("target") is not None:
            entry["target"] = {"point": True}  # (a point or a ref that can't be named: the path starts after it)
        for key in ("direction", "to", "x0", "y0", "x1", "y1"):
            if args.get(key) is not None:
                entry[key] = args[key]
        if name == "open_url":
            url = args.get("url")
            entry["link"] = self._bug_link(bundle, url) if isinstance(url, str) else None
            entry["link_cut"] = isinstance(url, str) and ("?" in url or "#" in url)
        self._bug_trail.append(entry)
        run = d.BATCH.get()
        if isinstance(run, dict) and isinstance(run.get("id"), str):
            aims = self._bug_batch_aims
            aims.setdefault(run["id"], {})[run.get("step")] = entry
            while len(aims) > BATCHES_KEPT:
                aims.pop(next(iter(aims)))

    def _bug_link(self, bundle: str | None, url: str) -> str | None:
        """The pattern of a place's link of the app that `url` matches (links.canonical), or None: a link is never kept as
        it was opened (§18.3)."""
        if not bundle:
            return None
        try:
            data = self.maps.data(self._maps_root(), bundle)
        except Exception:  # noqa: BLE001 - no map: no pattern, so the link isn't kept
            return None
        if not isinstance(data, dict) or not isinstance(data.get("places"), dict):
            return None
        try:
            return links.canonical(data, url)
        except Exception:  # noqa: BLE001
            return None

    async def _bug_named(self, app: str) -> bool:
        """Is the app one of the daemon's test apps (review R185 H2)? `serve --apps` names them; `--dedicated` names every app
        (a phone kept for testing); with none named, no app is one. `--phone` names none by itself (PR #21): it is the phone
        mode (unlock, whole-phone control), not a list of test apps, so with it only the apps of `--apps` are. A name in
        the list is looked up among the installed apps."""
        ceiling = getattr(self, "ceiling", None)
        if ceiling is None:
            return False
        if getattr(ceiling, "dedicated", False):
            return True
        named = tuple(getattr(ceiling, "apps", ()) or ())
        if app in named:
            return True
        d = _d()
        for entry in named:
            if d.BUNDLE_ID.match(entry):
                continue
            try:
                bundle, _ = await self._app(entry, partial=False)
            except Exception:  # noqa: BLE001 - a name that can't be found names no app
                continue
            if bundle == app:
                return True
        return False

    def _bug_person(self, kind: str) -> None:
        """A person's input in the live view: it has no name to write down (a point), so a path starts after it."""
        self._bug_trail.append({"tool": "tap", "ok": True, "app": self._bundle(), "by": "person", "target": {"point": True}})

    # ---- the tool ----
    _bug_pruned: bool = False

    def _bugs(self) -> bugstore.BugStore:
        bugs = bugstore.BugStore(self.bug_home if self.bug_home is not None else store.HOME / "bugs")
        if not self._bug_pruned:  # (§29.2: retention at start too; the daemon's first use of the bugs is its start for them)
            self._bug_pruned = True
            try:
                for app in bugs.apps():
                    bugs.prune(app)
            except OSError:
                pass
        return bugs

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
        runs = parsed["runs"]
        # (the bug is written first; a check that runs replaces this, a check that can't says why: §29.3, §29.4)
        reproduced = bugcheck.not_checked(bugcheck.PENDING if runs else NOT_ASKED)
        with_picture = bool(args.get("with_picture"))
        try:
            if parsed["source"] == "bug":
                made = await self._bug_again(ctx, args["bug"].strip(), with_picture)
            elif parsed["source"] == "finding":
                got = await asyncio.to_thread(self._bug_run_finding, args.get("run") or "last", args["finding"].strip())
                named = await self._bug_named(str(got.raw.get("app")))
                made = await asyncio.to_thread(bugmake.from_run, ctx, got, with_picture=with_picture, reproduced=reproduced,
                                               named=named)
            elif args["from"] == "failure":
                failure = self._bug_failure_of(client_id)
                if failure is None:
                    raise Refused("not_found", NO_FAILURE)
                named = await self._bug_named(failure["finding"]["app"])
                made = await asyncio.to_thread(bugmake.from_failure, ctx, failure, with_picture=with_picture,
                                               reproduced=reproduced, named=named)
            else:
                made = await self._bug_now(ctx, client_id, scope, args, with_picture, reproduced)
            notes: list[str] = []
            if runs:
                made = await self._bug_checked(ctx, client_id, scope, made, runs, parsed, notes)
        except bugmake.BugError as e:
            raise Refused(e.code, ctx.safe()(str(e))) from None
        except bugstore.BugGateError as e:
            if e.update:
                raise Refused("not_allowed", f"the bug was kept as it was: {e} Nothing was written.") from None
            raise Refused("not_allowed", f"the bug was not made: {e} Nothing was written.") from None
        except bugstore.BugFolderError as e:
            raise Refused("not_allowed", ctx.safe()(str(e))) from None
        except findings.FindingError as e:
            raise Refused("bad_params", ctx.safe()(f"the finding can't be made into a bug: {e}")) from None
        now_unasked = parsed["source"] == "from" and args["from"] == "now" and not runs
        text = bugmake.answer(made, ctx.scrub, reason_shown=not now_unasked, home=Path.home())
        if parsed["export"] is not None:
            held = ctx.store.read(made.finding["app"], made.meta["id"])
            if held is None:
                raise Refused("not_found", f"{made.meta['id']} can't be read back from its folder to export it.")
            try:
                said = await self._bug_export(held, parsed["export"])
            except Refused as e:
                # (the bug is made and kept: the refusal says so, and nothing was sent)
                raise Refused(e.code, ctx.safe()(f"{e}\n{text}")) from None
            notes.append(ctx.safe()(said))
        return {"text": "\n".join([text, *notes]), "image": None, "image_size": None}

    async def _bug_checked(self, ctx: bugmake.Context, client_id: str, scope, made: bugstore.Made, runs: int, parsed: dict,
                           notes: list[str]) -> bugstore.Made:
        """The reproduction check of the bug just made or named (§29.4), written into its folder. A check that can't run is
        written as `not checked` with why, unless the bug holds a real check already (then that one stays and a note says
        why this one didn't run)."""
        held = ctx.store.read(made.finding["app"], made.meta["id"])
        if held is None:
            raise Refused("not_found", f"{made.meta['id']} can't be read back from its folder to check it.")
        if parsed["account"] is not None:
            notes.append(ACCOUNT_NOTE)
        # (retention never removes a bug a check holds, review R185 M5; each check's hold is its own and lasts until its
        # result is written, PR #21)
        with ctx.store.hold(held.app, held.id):
            result = await self._bug_check(held, runs, parsed["reset"], parsed["account"], client_id=client_id,
                                           scope=scope)
            # (PR #21) whether a check that couldn't run keeps the last check is decided in the store, under its lock
            checked = await asyncio.to_thread(ctx.store.checked, held, result, ctx.gate, new=made.new,
                                              scrub=ctx.safe())
        if checked is not None and result.get("checked") is False and \
                (checked.meta.get("reproduced") or {}).get("checked") is not False:
            notes.append(f"Note: not checked this time ({result['why']}); the page keeps the last check.")
            return checked
        if checked is None:
            notes.append(f"Note: {held.id} was removed (or made again from another report) while it was checked; the "
                         "check wasn't written.")
            return made
        return checked

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
        file still exists. An id two apps hold is the bug of the call's app when that is one of them (the app the command
        line named: PR #21), else of the app in front when that is one, else `ambiguous` (§29.7, 1.16.2)."""
        bugs = ctx.store
        holders = await asyncio.to_thread(bugs.holders, bug_id)
        if not holders:
            raise Refused("not_found", f'no bug "{bug_id}" is kept; tapwright bug list names them.')
        app = holders[0]
        hint = _d()._APP_HINT.get()
        if len(holders) > 1 and hint in holders:
            app = hint  # (PR #21) the call's own app (the command line's --app, an agent's one app) names which one
        elif len(holders) > 1:
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
            return await asyncio.to_thread(bugs.add_picture, held, picture, ctx.gate, ctx.safe())
        return bugstore.Made(held.meta, held.folder, False, held.title, held.finding)

    async def _bug_now(self, ctx: bugmake.Context, client_id: str, scope, args: dict, with_picture: bool,
                       reproduced: dict | None = None) -> bugstore.Made:
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
        named = await self._bug_named(bundle)
        build = await self._build_of(bundle)
        who = self._explore_identity(bundle, build)
        identity = {"app_version": who.app_version, "build": explore._build_number(who.build), "tool_version": who.tool_version,
                    "device": {"model": who.device.model, "os": who.device.os}}
        picture = None
        taken = None  # (PR #21) the system sheet in front as the picture was taken
        try:
            from .exploretool import DaemonPhone
            phone = DaemonPhone(self, client_id, scope, bundle)
            picture = await phone.picture()
            taken = phone.picture_system
        except Refused:
            raise
        except Exception:  # noqa: BLE001 - a picture that can't be had is a gap in the page, not a reason to lose the bug
            picture = None
        when = utc_ms(datetime.datetime.now(datetime.timezone.utc))
        return await asyncio.to_thread(
            bugmake.from_now, ctx, app=bundle, note=note, expected=expected, identity=identity, when=when,
            observed=observed, place=place, place_key=key, logs=await self._bug_logs(bundle) if named else [],
            picture=picture, system=system_of(snap.tree) or taken, with_picture=with_picture, reproduced=reproduced, named=named)

    async def _bug_logs(self, bundle: str) -> list[str]:
        """The app's last 20 log lines of any level (§29.3), each `HH:MM:SS.mmm level text`; redacted when the bug is made.
        Only the app's own lines: what a system framework in its process logged (XCTest answering the runner's reads,
        preferences, power logging) is no line of the app (W177)."""
        try:
            self.ensure_logs(bundle)
            await self._refresh_logs(bundle)
            ring = self._rings.get(bundle)
        except Exception:  # noqa: BLE001 - no log is no log lines
            return []
        if ring is None:
            return []
        out = []
        for e in [e for e in ring.shown() if e.own][-findings.MAX_LOGS:]:
            at = datetime.datetime.fromtimestamp(e.at / 1000, datetime.timezone.utc)
            out.append(f"{at.strftime('%H:%M:%S')}.{at.microsecond // 1000:03d} {e.level} {e.text}")
        return out

    # ---- the reproduction check (§29.4) and the export (§29.7) ----
    async def _bug_check(self, held: bugstore.Kept, runs: int, reset: str, account: str | None, *, client_id: str = "",
                         scope=None) -> dict:
        """The reproduction check (§29.4): `{"n", "of", "build", "when", "reset"}` or `{"checked": false, "why"}`; never
        raises for a check that can't run (the bug is made already). Every move is the agent's own call under the skip floor,
        the ONE dispatch gate of 1.8.0 (`daemon._TOUCH_FLOOR`, judged on the element the touch really goes to), with nobody
        asked: a step the floor holds is refused at dispatch and stops the check. A `${typed}` step or a step whose target
        holds a policy or floor word stops it before anything is touched. `account` isn't used: the relaunch keeps data."""
        d = _d()
        app = held.app
        try:
            script = json.loads((held.folder / "repro.json").read_text("utf-8"))
        except (OSError, ValueError, RecursionError):
            script = None
        target = bugcheck.target_of(held.finding, held.meta, script if isinstance(script, dict) else None)
        if (why := bugcheck.why_not(target, reset)) is not None:
            return bugcheck.not_checked(why)
        rules = d._FLOOR_RULES.get() or d.safety.NONE  # (the caller's policy file only adds words to the floor)
        floor = explorefloor.Floor(rules, app)
        if (stop := bugcheck.person_step(target.steps, lambda s: bugmake.person_word(floor, s))) is not None:
            return bugcheck.needs_person(*stop)  # (nothing is touched: the check would stop there anyway)
        if (why := bugcheck.unaimed(target.steps)) is not None:
            return bugcheck.not_checked(why)
        try:
            declared = script.get("variables") if isinstance(script, dict) and isinstance(script.get("variables"), list) else []
            steps = routes.substitute(target.steps, {}, [v for v in declared if isinstance(v, str)])
            batch.check_steps(steps, script=True)
        except Refused as e:
            return bugcheck.not_checked(f"its script can't be run ({e.code})")
        if self.state == "locked":
            return bugcheck.not_checked("stopped (locked)")
        if self.state != "live":
            return bugcheck.not_checked(NO_PHONE)
        if self._explore_active is not None:
            return bugcheck.not_checked(CHECK_BUSY)

        def key_of(lang: str | None, tree) -> dict | None:
            if tree is None or not places.has_structure(tree):
                return None
            key = places.read_place(tree, lang).key
            return {"signature": key["signature"]} if isinstance(key.get("signature"), str) else None
        tokens: tuple = ()
        try:
            # (set inside the try: a port that can't be made never leaves the busy mark set, review R185 L1)
            self._explore_active = client_id or "bug_report"  # (no explore starts on the phone while the check runs)
            port = bugcheck.DaemonCheck(self, client_id, scope, app, key_of)
            tokens = (d._NESTED.set(True), d._DIALOG.set(None), d._CRASHES.set(port.phone.crashes),
                      d._TOUCH_FLOOR.set(floor))
            self.ensure_logs(app)
            build = await self._build_of(app) or "unknown"
            return await bugcheck.check(port, target, steps, runs, build=build,
                                        when=lambda: utc_ms(datetime.datetime.now(datetime.timezone.utc)),
                                        scrub=self.accounts.bug_scrub)
        except bugcheck.Stopped as e:
            return bugcheck.not_checked(f"stopped ({e.code})")
        except Refused as e:
            return bugcheck.not_checked(f"stopped ({e.code})")
        except Exception as e:  # noqa: BLE001 - a check that can't run never stops the bug (§29.3)
            return bugcheck.not_checked(f"it couldn't run ({type(e).__name__})")
        finally:
            if tokens:
                d._TOUCH_FLOOR.reset(tokens[3])
                d._CRASHES.reset(tokens[2])
                d._DIALOG.reset(tokens[1])
                d._NESTED.reset(tokens[0])
            self._explore_active = None

    async def _bug_export(self, held: bugstore.Kept, export: dict) -> str:
        """The export (§29.7): a GitHub issue or a `.zip`, every text it sends grepped again with the live values (§29.6), and
        one audit line (kind `export`) for each, a refused one too. No audit log: nothing is sent."""
        ctx = self._bug_context()
        log = getattr(self, "audit", None)

        def audit(detail: str) -> bool:
            return bool(log is not None and log.write("export", name="bug_report", app=held.app, detail=detail))
        return await asyncio.to_thread(bugexport.export, held, export, ctx.gate, audit, Path.home())


system_of = explorefloor.system_of  # (§29.8: the kind of a system sheet or prompt in front)

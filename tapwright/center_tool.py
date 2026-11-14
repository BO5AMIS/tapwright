"""Notification Center inside the daemon (docs/AGENT-TOOLS.md §24.2, §20.4, §22.1): the `notifications` tool, one-time
codes read from it, and the notification trigger's check. A mixin of Daemon, kept apart from daemon.py.

Privacy (decided for 1.7, W134):
- Notification Center shows every app's notifications. Only the given apps' (personal mode), the whole phone's
  (`--dedicated`, `--phone`), a code setting's SOURCE apps, or a trigger's app ever leave the reader
  (notifications.parse drops the rest while it walks the tree). The tree itself is read raw (Session.read_center), never
  kept, logged or traced; the answer, the log, the audit and the live view's caption carry only what was kept.
- The screen shows Notification Center for about a second (one swipe, one read, one swipe back). The live view shows the
  phone as always, so a person watching it sees what the phone's own screen shows; nothing more is taken: no screenshot
  while it is open, no picture in any answer of this tool, and the caption says only `Read notifications`. Every answer
  says Notification Center was on screen. A person who doesn't want that pauses the live view (`paused`), and nothing
  is read; a person or an agent using the phone makes the trigger's check wait.
- A one-time code is never shown: every code a source app's matching notification holds joins the redactor the moment it
  is read, before any text is made, so the `notifications` tool shows `<one-time code>` too.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from . import notifications as notes_lib, otp as otplib, ui
from .control import Refused

SPRINGBOARD = "com.apple.springboard"
CENTER_X = 0.3  # across the screen, where the swipe down starts: the left half opens Notification Center (the right, Control Center)
CENTER_PULL = 0.6  # how far down the swipe goes, in screen heights
CENTER_SWIPE_MS = 300
CENTER_OPEN_WAIT = 0.6  # seconds for Notification Center to come down before its tree is read
CENTER_CLOSE_WAIT = 0.4  # seconds for it to go up again before the app in front is asked
OPEN_WAIT = 1.0  # seconds for an opened notification's app to come to the front
FIELD_MARGIN = 5.0  # seconds: a code field this daemon didn't see counts from 2 minutes ago less this (the read's own time)
NOTE_ON_SCREEN = ("Notification Center was on the phone's screen for about a second while it was read (the live view "
                  "showed it too); only {whose} notifications were read out.")
FROM_OTHERS_APPS = ("com.apple.MobileSMS", "com.apple.mobilemail")  # their notifications are others' words (§21.1)
NOT_OPENED = "Notification Center didn't open, so nothing was read."


class CenterTools:
    """Mixed into Daemon. Needs: self.session, self.control, self.accounts, self.otp_codes, self.code_fields, self.audit,
    self.broadcast, self.scrub_everywhere, self._loaded, self.wait_connected."""

    # ---- the code field (§20.4: a code counts only within 2 minutes after the app showed it) ----
    def _watch_code_field(self, tree: ui.Tree) -> None:
        """Session.on_tree: a read of an app whose code setting is on notes whether it shows a code field now."""
        app = tree.app[1] if tree.app else None
        if not app:
            return
        if app not in self._loaded().otp:
            return
        self.code_fields.seen(app, any(e.role == "input" and notes_lib.code_like_field(e) for e in tree.items))

    # ---- the read ----
    async def _on_phone(self, fn):
        """Inside an agent's action already (type_text's code): run it; else take the queue's turn as an action does
        (`paused` while the live view is paused; it waits while a person drives)."""
        if self.control.holds_turn():
            return await fn()
        return await self.control.agent(fn)

    async def _read_center(self, keep: Callable[[str], bool], open_index: int | None = None,
                           caption: str = "Read notifications") -> "tuple[list[notes_lib.Notification], notes_lib.Notification | None]":
        """Open Notification Center, read the notifications of the apps `keep` says yes to (every other app's is dropped
        in the parse), and close it again with the app that was in front back in front. `open_index` (1-based): tap that
        one instead of closing, which opens it in its app. Runs on the phone's queue (the caller holds the turn)."""
        s = self.session
        if not getattr(s, "app_names", None):
            await s.read_app_names()
        before = await s.foreground_bundle_id()
        w, h = s.screen
        self.broadcast({"type": "action", "by": "agent", "kind": "notifications", "at": int(time.time() * 1000),
                        "ok": True, "caption": caption})
        await s.swipe(w * CENTER_X, 1.0, w * CENTER_X, h * CENTER_PULL, duration_ms=CENTER_SWIPE_MS)
        await asyncio.sleep(CENTER_OPEN_WAIT)
        opened: notes_lib.Notification | None = None
        try:
            source = await s.read_center()
            if not notes_lib.is_center(source):
                raise Refused("not_found", NOT_OPENED)
            found = notes_lib.parse(source, dict(getattr(s, "app_names", {}) or {}), keep)
            del source  # (every other app's notification goes with it)
            if open_index is not None and 1 <= open_index <= len(found):
                chosen = found[open_index - 1]
                await s.tap(ui.Target(x=chosen.center[0], y=chosen.center[1]))
                await asyncio.sleep(OPEN_WAIT)
                if await s.foreground_bundle_id() != chosen.bundle:
                    # iOS 26 answers the first tap with the row's actions; its Open action opens the app
                    after = await s.read_center()
                    spot = notes_lib.open_action(after, chosen.center[1], w) if notes_lib.is_center(after) else None
                    del after
                    if spot is not None:
                        await s.tap(ui.Target(x=spot[0], y=spot[1]))
                        await asyncio.sleep(OPEN_WAIT)
                if await s.foreground_bundle_id() != chosen.bundle:
                    # Its app didn't come to the front: Notification Center must never stay open (the phone locks
                    # soon after, as it does on the lock screen), so it is closed, and the call says so.
                    raise Refused("not_found", f"n{open_index} didn't open: its app didn't come to the front, so "
                                               f"Notification Center was closed again and the app you were in is back. "
                                               f"Open {chosen.app} with open_app instead.")
                opened = chosen
        finally:
            if opened is None:
                await self._close_center(before)
        return found, opened

    async def _close_center(self, before: str | None) -> None:
        """Notification Center up again (a swipe up from the bottom edge), and the app that was in front back."""
        s = self.session
        w, h = s.screen
        try:
            if await s.foreground_bundle_id() == SPRINGBOARD:  # (it opened: else a swipe up would leave the app)
                await s.swipe(w / 2, h - 2.0, w / 2, h * 0.3, duration_ms=250)
                await asyncio.sleep(CENTER_CLOSE_WAIT)
            front = await s.foreground_bundle_id()
            if before and before != SPRINGBOARD and front != before:
                await s.launch(before)  # (it was running: this brings it back as it was)
            elif (not before or before == SPRINGBOARD) and front == SPRINGBOARD:
                await s.press_button("home")  # the home screen, with nothing pulled down over it
        except Refused:
            raise
        except Exception as e:  # the answer still says what was read; the log says why it stayed open
            self.log(f"notifications: couldn't close Notification Center ({type(e).__name__})")

    # ---- the tool (§24.2) ----
    async def _notifications_tool(self, client_id: str, args: dict, notes: list[str], given: "dict[str, str] | None") -> dict:
        """`notifications`: the given apps' notifications (all, with --dedicated or --phone), newest first, at most 20;
        `{open: n}` opens the n-th. `given`: personal mode's apps (None: the whole phone)."""
        if client_id in getattr(self, "_journeys", {}):
            raise Refused("bad_params", "notifications isn't part of a journey: a user's-eyes run stays in the app")
        n = args.get("open")
        if n is not None and (isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= notes_lib.MAX):
            raise Refused("bad_params", f"open is the number of a notification in the list, 1 to {notes_lib.MAX} (n1 is 1), "
                                        f"not {n!r}")
        await self.wait_connected()
        keep = (lambda b: b in given) if given is not None else (lambda b: True)
        found, opened = await self._on_phone(lambda: self._read_center(
            keep, n, caption="Open a notification" if n is not None else "Read notifications"))
        self._hide_codes(found)
        whose = "your apps'" if given is not None else "the phone's"
        apps = "your apps" if given is not None else "the phone's apps"
        note = NOTE_ON_SCREEN.format(whose=whose)
        if n is not None:
            if opened is None:
                raise Refused("not_found", f"there {'is' if len(found) == 1 else 'are'} {len(found)} notification"
                                           f"{'' if len(found) == 1 else 's'} from {apps} now, so n{n} isn't "
                                           f"there; nothing was opened. Call notifications to read the list again.\n"
                                           f"Note: {note}")
            return {"text": self.scrub_everywhere("\n".join([
                f"Opened n{n}: {self._notification_line(opened, n)}", *(f"Note: {x}" for x in [*notes, note]),
                "Note: look shows the screen it opened."])), "image": None, "image_size": None}
        names = sorted({x.app for x in found})
        head = (f"Notifications: {len(found)} from {', '.join(names)} (newest first)." if found else
                f"Notifications: none from {apps}.")
        lines = [head, *(self._notification_line(x, i) for i, x in enumerate(found, 1))]
        return {"text": self.scrub_everywhere("\n".join([*lines, *(f"Note: {x}" for x in [*notes, note])])),
                "image": None, "image_size": None}

    def _notification_line(self, x: "notes_lib.Notification", i: int) -> str:
        """`n1 Wayfare (com.example.wayfare) 2m ago "Trip booked" "Your trip to Lisbon is confirmed."`, redacted."""
        scrub = self.scrub_everywhere
        marked = " (from others)" if x.bundle in FROM_OTHERS_APPS else ""
        when = f" {x.when}" if x.when else ""
        body = f' "{scrub(x.body)}"' if x.body else ""
        return f'n{i} {scrub(x.app)} ({x.bundle}){when}{marked} "{scrub(x.title)}"{body}'

    def _hide_codes(self, found: "list[notes_lib.Notification]") -> None:
        """Every code a source app's matching notification holds joins the redactor before any text is made (§20.4)."""
        for app, setting in self._loaded().otp.items():
            for x in found:
                if x.bundle in setting.sources and notes_lib.matches(x, setting.match):
                    for code in notes_lib.candidates(f"{x.title}\n{x.body}", setting.pattern):
                        self.accounts.remember_code(code)

    # ---- one-time codes (§20.4, §24.3, §18.11) ----
    async def _code_from_center(self, app: str, field_at: float | None = None) -> "otplib.Code | None":
        """Read the setting's SOURCE apps' notifications only, and put the one code that counts for `app` (once, within 2
        minutes after its code field showed). The code joins the redactor at once. None when there is none."""
        setting = self._loaded().otp.get(app)
        if setting is None:
            return None
        field_at = field_at if field_at is not None else (self.code_fields.shown_at(app) or time.time())
        try:
            found, _ = await self._on_phone(lambda: self._read_center(lambda b: b in setting.sources,
                                                                       caption="Read a one-time code"))
        except Refused as e:
            if e.code in ("busy", "paused", "locked"):
                raise
            return None
        self._hide_codes(found)
        got = notes_lib.find_code(found, setting.sources, setting.match, setting.pattern, field_at, time.time(),
                                  self.otp_codes.used)
        if got is None:  # (why, never what: counts and ages only)
            self.log(f"one-time code for {app}: none taken from {len(found)} notification(s) of its source apps "
                     f"(ages {[x.when for x in found][:5]}, the field {int(time.time() - field_at)} s ago)")
            return None
        self.otp_codes.put(app, got[0], got[1], field_at)
        return self.otp_codes.take(app)

    async def _code_for_typing(self, app: str) -> "otplib.Code | None":
        """`${otp.<app>}` in a step: a code already read for it, else one read now. The field being typed into is a code
        field showing now (if no read noted it earlier)."""
        held = self.otp_codes.take(app)
        if held is not None:
            return held
        self.code_fields.seen(app, True)
        return await self._code_from_center(app)

    async def otp_handover(self, app: str) -> "otplib.Code | None":
        """`POST /otp` (§18.11): a code for Tapline's host, only for an app whose setting is on in the vault file this
        daemon was started with, only while that app is in front (its code field is up), handed over once. The audit
        gets an `otp` line with the app and the source, never the code. None (the route's 404) otherwise."""
        path = getattr(getattr(self, "ceiling", None), "accounts", None)
        if not path or app not in self.accounts.get(path).otp:
            return None
        found = self.otp_codes.take(app)
        if found is None:
            try:
                # One read of the screen, which waits out a notification banner over the app (the code's own banner,
                # seen in the joint check 2026-11-14: while it shows, iOS says SpringBoard is in front), tells both
                # which app is in front and whether its code field shows (the read notes it, _watch_code_field).
                seen_before = self.code_fields.shown_at(app)
                tree = await self._on_phone(self.session.refresh)
                if not tree.app or tree.app[1] != app:
                    self.log(f"one-time code for {app}: none handed over, the app isn't in front")
                    return None
                field_at = self.code_fields.shown_at(app)
                if field_at is None:
                    self.log(f"one-time code for {app}: none handed over, no code field shows")
                    return None
                if seen_before is None:
                    # Tapline's host drove the app to its code field, so this daemon never saw it before: a code field
                    # showing now counts from 2 minutes ago, so a code that came at most 2 minutes ago is taken
                    field_at = time.time() - notes_lib.WINDOW + FIELD_MARGIN
                found = await self._code_from_center(app, field_at)
            except Exception as e:  # busy, paused, a dropped line: there is no code to give
                self.log(f"one-time code for {app}: none handed over ({type(e).__name__}: {getattr(e, 'code', '')})")
                return None
        if found is not None and self.audit is not None:
            self.audit.write("otp", app=app, detail=f"from {found.source}")
        return found

    # ---- the notification trigger (§22.1) ----
    async def installed_names(self) -> dict[str, str]:
        """{bundle id: home screen name} of the installed apps (`serve --apps` and a call may name an app either way);
        empty when the phone can't say."""
        s = self.session
        try:
            if not getattr(s, "app_names", None):
                await s.read_app_names()
        except Exception:
            return {}
        return dict(getattr(s, "app_names", {}) or {})

    async def notification_keys(self, app: str) -> list[str]:
        """The digests of `app`'s notifications in Notification Center now, newest first (never their text): what a
        notification trigger's check compares with what it saw before. Refused `busy` while a person or an agent uses
        the phone, and `paused` while the live view is paused: the check never reads then."""
        if self.control.paused:
            raise Refused("paused", "paused by the user in the live view")
        if self.control.driver is not None:
            raise Refused("busy", "the phone is in use; the check waits")
        await self.wait_connected()
        found, _ = await self._on_phone(lambda: self._read_center(lambda b: b == app, caption="Check for notifications"))
        return [x.key() for x in found]

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
- A video recording of the live view (`tapwright record-video`, 1.7.7) leaves those moments out, failing closed: the
  daemon notes, on the hub's monotonic clock, when Notification Center may be open (`_center_spans`: from before the
  swipe until the close is shown to have worked, plus CENTER_HIDE_AFTER_S for late and re-sent frames), and
  `GET /stream?for=recording` sends no frame that arrived inside one, none whose arrival it can't tell, and never the
  picture that was newest when the span ended (a feeder that sends it again). The recording holds the last picture
  from before instead.
- A one-time code is never shown: every code a source app's matching notification holds joins the redactor the moment it
  is read, before any text is made, so the `notifications` tool shows `<one-time code>` too.
"""
from __future__ import annotations

import asyncio
import time
import weakref
from collections.abc import Callable

from . import notifications as notes_lib, otp as otplib, ui
from .control import Refused
from .errors import PHONE_LOCKED

SPRINGBOARD = "com.apple.springboard"
CENTER_X = 0.3  # across the screen, where the swipe down starts: the left half opens Notification Center (the right, Control Center)
CENTER_PULL = 0.6  # how far down the swipe goes, in screen heights
CENTER_SWIPE_MS = 300
CENTER_OPEN_WAIT = 0.6  # seconds for Notification Center to come down before its tree is read
CENTER_CLOSE_WAIT = 0.4  # seconds for it to go up again before the app in front is asked
CENTER_HIDE_AFTER_S = 3.0  # a recording leaves out frames this long after Notification Center's close was confirmed: it
# slides up, WebDriverAgent's frames queue on a slow link, and AirPlay sends pictures again
OPEN_WAIT = 1.0  # seconds for an opened notification's app to come to the front
FIELD_MARGIN = 5.0  # seconds: a code field this daemon didn't see counts from 2 minutes ago less this (the read's own time)
NOTE_ON_SCREEN = ("Notification Center was on the phone's screen for about a second while it was read (the live view "
                  "showed it too); only {whose} notifications were read out.")
FROM_OTHERS_APPS = ("com.apple.MobileSMS", "com.apple.mobilemail")  # their notifications are others' words (§21.1)
NOT_OPENED = "Notification Center didn't open, so nothing was read."
CENTER_CUT = ("not all of Notification Center's elements were read within the time given, so more notifications may be "
              "there than are listed; call notifications again.")  # (2.0, review R200v L4)
TRIGGER_CLIENT = "notification-trigger"  # the trigger's check: never a client with calls of its own
NOT_CLOSED = ("Notification Center may still be on the phone's screen: closing it didn't work, so nothing read is "
              "answered. Look at the screen, and close it (or ask the user to) before anything else.")


class CenterTools:
    """Mixed into Daemon. Needs: self.session, self.control, self.accounts, self.otp_codes, self.code_fields, self.audit,
    self.broadcast, self.scrub_everywhere, self._loaded, self.wait_connected, self.phone_in_use."""

    # ---- the code field (§20.4: a code counts only within 2 minutes after the app showed it) ----
    def _watch_code_field(self, tree: ui.Tree) -> None:
        """Session.on_tree: a read of an app whose code setting is on notes whether it shows a code field now."""
        app = tree.app[1] if tree.app else None
        if not app:
            return
        if app not in self._loaded().otp:
            return
        # the same strict test as typing's (otp_target): a one-line field that says code by its own words, never an
        # input whose typed text says "code" (Codex, PR #12 cycle 1, M3)
        self.code_fields.seen(app, any(notes_lib.otp_target(e) for e in tree.items))

    # ---- when Notification Center may be on screen (a recording of the live view leaves those frames out) ----
    def _center_begin(self) -> None:
        spans = self.__dict__.setdefault("_center_spans", [])
        if not spans or spans[-1][1] is not None:  # (a span still open from a close that failed goes on)
            spans.append([self.hub.mono(), None])

    def _center_end(self, closed: bool) -> None:
        """Closed (or its app opened): the span ends CENTER_HIDE_AFTER_S later. Not shown closed: it stays open, so no
        frame is recorded. Either way the picture that is newest now never reaches a recording."""
        spans = self.__dict__.setdefault("_center_spans", [])
        if spans:  # every picture that arrived since the span began, and the one that is newest now, is excluded for good
            start = spans[-1][0]
            for arrived, frame in self.hub.recent():
                if arrived >= start:
                    self._exclude_picture(getattr(frame, "picture", None))
        self._exclude_picture(getattr(self.hub.latest, "picture", None))
        if closed and spans and spans[-1][1] is None:
            spans[-1][1] = self.hub.mono() + CENTER_HIDE_AFTER_S
        cut = self.hub.mono() - 3600.0
        spans[:] = [s for s in spans if s[1] is None or s[1] >= cut]

    def _exclude_picture(self, picture) -> None:
        """A picture of Notification Center never reaches a recording, however often a feeder publishes it again and
        whenever that is: the exclusion follows the picture itself (weakly: a picture nobody holds can't come back)."""
        if picture is not None:
            self.__dict__.setdefault("_center_pictures", weakref.WeakSet()).add(picture)

    def center_covers(self, mono: float) -> bool:
        """Was Notification Center possibly on the phone's screen at `mono` (the hub's monotonic seconds)?"""
        return any(start <= mono and (end is None or mono <= end)
                   for start, end in self.__dict__.get("_center_spans", ()))

    # ---- while the phone is being unlocked (1.7.8, last Opus review M1): iOS's keypad lights each key as it is touched,
    # and its dots show the length, so from the wake to the end of the check no frame reaches an agent (filmstrip,
    # compare_screen, a screenshot, a journey's picture) or a recording. The owner's own live view still shows it.
    def _unlock_begin(self) -> list:
        """A span opens now; give it back to _unlock_end."""
        span = [self.hub.mono(), None]
        self.__dict__.setdefault("_unlock_spans", []).append(span)
        return span

    def _unlock_end(self, span: list) -> None:
        """The unlock ended: every picture that came since the span began, and the newest one now, is left out for good
        (from recordings and agents alike); the span ends CENTER_HIDE_AFTER_S later (frames queue on a slow link)."""
        start = span[0]
        for arrived, frame in self.hub.recent():
            if arrived >= start:
                self._exclude_unlock_picture(getattr(frame, "picture", None))
        self._exclude_unlock_picture(getattr(self.hub.latest, "picture", None))
        span[1] = self.hub.mono() + CENTER_HIDE_AFTER_S
        spans = self.__dict__.setdefault("_unlock_spans", [])
        cut = self.hub.mono() - 3600.0
        spans[:] = [s for s in spans if s[1] is None or s[1] >= cut]

    def _exclude_unlock_picture(self, picture) -> None:
        if picture is not None:
            self.__dict__.setdefault("_unlock_pictures", weakref.WeakSet()).add(picture)
            self._exclude_picture(picture)  # (and from recordings, as a Notification Center picture is)

    def unlocking(self) -> bool:
        """Is an unlock running now (its span open)?"""
        return any(end is None for _, end in self.__dict__.get("_unlock_spans", ()))

    def unlock_covers(self, mono: float) -> bool:
        """Was the phone possibly being unlocked at `mono` (the hub's monotonic seconds)?"""
        return any(start <= mono and (end is None or mono <= end)
                   for start, end in self.__dict__.get("_unlock_spans", ()))

    def unlocked_since(self, mono: float) -> bool:
        """Did an unlock run at any moment since `mono` (a screenshot asked of the phone then may show its keypad)?"""
        return any(end is None or end >= mono for _, end in self.__dict__.get("_unlock_spans", ()))

    def center_since(self, mono: float) -> bool:
        """Was Notification Center possibly open at any moment since `mono` (a screenshot asked of the phone then may show it)?"""
        return any(end is None or end >= mono for _, end in self.__dict__.get("_center_spans", ()))

    def recording_covers(self, mono: float) -> bool:
        """Are a recording's frames being left out at `mono` (Notification Center open, or the phone being unlocked)?"""
        return self.center_covers(mono) or self.unlock_covers(mono)

    def frame_private(self, frame) -> bool:
        """May `frame` not reach an agent, because the phone was (or may have been) being unlocked? Yes while an unlock
        runs, when it arrived inside an unlock's span, when it is a picture of one, and when the hub can't say when it
        arrived while an unlock's span is known (fail-closed)."""
        if self.unlocking():
            return True
        if frame is None:
            return False
        picture = getattr(frame, "picture", None)
        if picture is not None and picture in self.__dict__.get("_unlock_pictures", ()):
            return True
        arrived = self.hub.arrival_of(frame.seq)
        if arrived is None:
            return bool(self.__dict__.get("_unlock_spans"))
        return self.unlock_covers(arrived)

    def recording_hides(self, hub, frame) -> bool:
        """Must a recording of the live view leave `frame` out? Yes when it arrived while Notification Center may have
        been open or the phone was being unlocked, when the hub can't say when it arrived, and when it is the picture
        that was newest at the end of a read or an unlock (a feeder sends a still picture again, with a new arrival
        time)."""
        arrived = hub.arrival_of(frame.seq)
        if arrived is None:
            return True
        picture = getattr(frame, "picture", None)
        if picture is not None and picture in self.__dict__.get("_center_pictures", ()):
            return True
        if self.unlocking() or self.recording_covers(arrived):
            self._exclude_picture(picture)  # (a picture that came while it was open stays out when it comes again)
            return True
        return False

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
        home_ok = True
        if before == SPRINGBOARD:
            # A notification banner over an app makes WebDriverAgent say SpringBoard (Daemon._front): the process list
            # says which app is really in front, and that one comes back after. When it can't say, the home button is
            # never pressed (it would throw an app away); the swipe up alone closes Notification Center (B-M4).
            try:
                running = list(await asyncio.wait_for(s.device.foreground_apps(), 5))
            except Exception:
                running, home_ok = [], False
            if len(running) == 1:
                before = running[0]
            elif running:
                home_ok = False
        w, h = s.screen
        names = dict(getattr(s, "app_names", {}) or {})
        self.broadcast({"type": "action", "by": "agent", "kind": "notifications", "at": int(time.time() * 1000),
                        "ok": True, "caption": caption})
        opened: notes_lib.Notification | None = None
        self._center_begin()
        try:
            # (inside the try: a cancelled call, or a swipe that raised after the gesture went out, still closes it)
            await s.swipe(w * CENTER_X, 1.0, w * CENTER_X, h * CENTER_PULL, duration_ms=CENTER_SWIPE_MS)
            await asyncio.sleep(CENTER_OPEN_WAIT)
            source = await s.read_center()
            if not notes_lib.is_center(source):
                raise Refused("not_found", NOT_OPENED)
            see = getattr(s, "see_front", None)
            if callable(see):
                rules_before = getattr(s, "touch_rules", None)
                # (2.0.3, review RP L3) the tool's own touches here (a notification's row, the closing swipe) are checked
                # against what it just opened and read, not the agent's older answer: it can close what it opened
                await see(element=True)
            found = notes_lib.parse(source, names, keep)
            del source  # (every other app's notification goes with it)
            # (2.0, R200v L4) the runner may have cut the read short: what it left out may hold notifications
            self._center_cut = getattr(s, "center_tier", "full") != "full"
            if open_index is not None and 1 <= open_index <= len(found):
                chosen = await self._same_notification(found[open_index - 1], open_index, names, keep)
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
            shown_closed = False
            try:
                if opened is None:
                    await self._close_center(before, home_ok)
                shown_closed = True
            finally:
                self._center_end(shown_closed)
                if "rules_before" in locals() and hasattr(s, "touch_rules"):
                    # (review RP2 L1) the tool's looser rule ends with the center: the agent's own rules again
                    s.touch_rules = rules_before
        return found, opened

    async def _same_notification(self, chosen: "notes_lib.Notification", n: int, names: dict[str, str],
                           keep: Callable[[str], bool]) -> "notes_lib.Notification":
        """Right before `open` taps: read Notification Center again and find the same notification (its app, title and
        body). A new one coming in moves the rows, so a tap on the first read's place could open another app's (B-M8).
        Refused when it is gone, is there twice, moved, or its place holds more than one notification."""
        again = await self.session.read_center()
        try:
            if not notes_lib.is_center(again):
                raise Refused("not_found", NOT_OPENED)
            now = [x for x in notes_lib.parse(again, names, keep) if x.same(chosen)]
            moved = len(now) != 1 or abs(now[0].center[0] - chosen.center[0]) > 2 or \
                abs(now[0].center[1] - chosen.center[1]) > 2 or notes_lib.rows_at(again, now[0].center) != 1
        finally:
            del again
        if moved:
            raise Refused("not_found", f"n{n} moved or changed while Notification Center was open (a new notification "
                                       f"came in), so nothing was opened. Call notifications to read the list again.")
        return now[0]

    async def _close_center(self, before: str | None, home_ok: bool = True) -> None:
        """Notification Center up again (a swipe up from the bottom edge), and the app that was in front back. The home
        button only when the home screen was in front before (`home_ok`: never when an app may be under a banner).
        Each step is tried even when the one before failed (a swipe that raised still leaves the app's launch to try),
        and the end is checked: the app back in front, or, from the home screen, Notification Center no longer there.
        When that can't be shown, `internal` with NOT_CLOSED, never an answer as if it closed (Codex, PR #12 cycle 2, M6)."""
        s = self.session
        w, h = s.screen
        failed: list[str] = []
        try:
            if await s.foreground_bundle_id() == SPRINGBOARD:  # (it opened: else a swipe up would leave the app)
                await s.swipe(w / 2, h - 2.0, w / 2, h * 0.3, duration_ms=250)
                await asyncio.sleep(CENTER_CLOSE_WAIT)
        except Refused:
            raise
        except Exception as e:  # noqa: BLE001 - the app's launch below may still close it
            failed.append(type(e).__name__)
        try:
            front = await s.foreground_bundle_id()
            pressed = False
            if before and before != SPRINGBOARD and front != before:
                await s.launch(before)  # (it was running: this brings it back as it was)
            elif home_ok and (not before or before == SPRINGBOARD) and front == SPRINGBOARD:
                await s.press_button("home")  # the home screen, with nothing pulled down over it
                pressed = True
            # the check: the app that was in front is again, or (from the home screen) no Notification Center is read
            if before and before != SPRINGBOARD:
                closed = await s.foreground_bundle_id() == before
                if closed and failed:  # (the app came back without the swipe: the read says whether it is still down)
                    closed = not notes_lib.is_center(await s.read_center())
            else:
                closed = not notes_lib.is_center(await s.read_center())
                if not closed and pressed:
                    # (CodeRabbit, PR #26) through the hands the press answers as it is pressed (the runner's route
                    # waited for the home screen): the center may still be going up. Read once more after it has time
                    await asyncio.sleep(CENTER_CLOSE_WAIT)
                    closed = not notes_lib.is_center(await s.read_center())
        except Refused:
            raise
        except Exception as e:  # noqa: BLE001 - not shown closed
            failed.append(type(e).__name__)
            closed = False
        if failed:
            self.log(f"notifications: closing Notification Center: {', '.join(failed)}")
        if not closed:
            self.log("notifications: Notification Center may still be open")
            raise Refused("internal", NOT_CLOSED)

    # ---- the tool (§24.2) ----
    async def _notifications_tool(self, client_id: str, args: dict, notes: list[str], given: "dict[str, str] | None") -> dict:
        """`notifications`: the given apps' notifications (all, with --dedicated or --phone), newest first, at most 20;
        `{open: n}` opens the n-th. `given`: personal mode's apps (None: the whole phone)."""
        if client_id in getattr(self, "_journeys", {}):
            raise Refused("bad_params", "notifications isn't part of a journey: a user's-eyes run stays in the app")
        if self.state == "locked":  # (as actions are: a locked phone is never swiped, B-L2)
            raise Refused("locked", PHONE_LOCKED)
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
            if opened is None and getattr(self, "_center_cut", False):
                # (CodeRabbit, PR #26) n may be among what the cut-short read left out: never told absent
                raise Refused("not_found", f"{len(found)} notification{'' if len(found) == 1 else 's'} from {apps} "
                                           f"{'was' if len(found) == 1 else 'were'} read, so n{n} couldn't be checked; "
                                           f"nothing was opened. Call notifications to read the list again.\n"
                                           f"Note: {CENTER_CUT}\nNote: {note}")
            if opened is None:
                raise Refused("not_found", f"there {'is' if len(found) == 1 else 'are'} {len(found)} notification"
                                           f"{'' if len(found) == 1 else 's'} from {apps} now, so n{n} isn't "
                                           f"there; nothing was opened. Call notifications to read the list again.\n"
                                           f"Note: {note}")
            return {"text": self.scrub_everywhere("\n".join([
                f"Opened n{n}: {self._notification_line(opened, n)}", *(f"Note: {x}" for x in [*notes, note]),
                "Note: look shows the screen it opened."])), "image": None, "image_size": None}
        names = sorted({x.app for x in found})
        cut = getattr(self, "_center_cut", False)
        head = (f"Notifications: {len(found)} from {', '.join(names)} (newest first)." if found else
                f"Notifications: none read from {apps}." if cut else f"Notifications: none from {apps}.")
        if cut:  # (R200v L4) never a list that looks whole when it isn't
            notes = [*notes, CENTER_CUT]
        lines = [head, *(self._notification_line(x, i) for i, x in enumerate(found, 1))]
        return {"text": self.scrub_everywhere("\n".join([*lines, *(f"Note: {x}" for x in [*notes, note])])),
                "image": None, "image_size": None}

    def _notification_line(self, x: "notes_lib.Notification", i: int) -> str:
        """`n1 Wayfare (com.example.wayfare) 2m ago "Trip booked" "Your trip to Lisbon is confirmed."`, redacted."""
        scrub = self.scrub_everywhere
        marked = " (from others)" if x.bundle in FROM_OTHERS_APPS else ""
        when = f" {x.when}" if x.when else ""
        # redacted whole, THEN collapsed and cut (§20.3; Codex, PR #12 cycle 1, H5)
        body = f' "{notes_lib.shown(scrub(x.body), notes_lib.BODY_MAX)}"' if x.body else ""
        return f'n{i} {scrub(x.app)} ({x.bundle}){when}{marked} "{notes_lib.shown(scrub(x.title), notes_lib.TITLE_MAX)}"{body}'

    def _hide_codes(self, found: "list[notes_lib.Notification]") -> None:
        """Every code a source app's matching notification holds joins the redactor before any text is made (§20.4)."""
        for app, setting in self._loaded().otp.items():
            for x in found:
                if x.bundle in setting.sources and notes_lib.matches(x, setting.match):
                    for code in notes_lib.codes_to_hide(x, setting.pattern):  # (every form of each: H4)
                        self.accounts.remember_code(code)

    # ---- one-time codes (§20.4, §24.3, §18.11) ----
    async def _code_from_center(self, app: str, field_at: float | None = None) -> "otplib.Code | None":
        """Read the setting's SOURCE apps' notifications only, and put the one code that counts for `app` (once, within 2
        minutes after its code field showed). The code joins the redactor at once. None when there is none."""
        setting = self._loaded().otp.get(app)
        if setting is None:
            return None
        field_at = field_at if field_at is not None else (self.code_fields.shown_at(app) or time.time())
        # Both callers know the code field shows NOW (it is being typed into, or /otp's read just saw it), so a code
        # counts when it came after the field was first seen and at most 2 minutes ago: a field left open longer
        # (seen in the joint check 2026-11-14: Verify stayed up 3 minutes) still takes a fresh code, never an old one.
        field_at = max(field_at, time.time() - notes_lib.WINDOW + FIELD_MARGIN)
        try:
            found, _ = await self._on_phone(lambda: self._read_center(lambda b: b in setting.sources,
                                                                       caption="Read a one-time code"))
        except Refused as e:
            if e.code in ("busy", "paused", "locked", "internal"):  # (internal: it may still be open, NOT_CLOSED)
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

    async def _code_for_typing(self, app: str, field: "ui.Element") -> "otplib.Code | None":
        """An explicit `${otp.<app>}` request: consume its held code or read its configured source.
        Custom/localized field labels do not override the caller's explicit selection."""
        held = self.otp_codes.take(app)
        if held is not None:
            return held
        self.code_fields.seen(app, True)
        return await self._code_from_center(app)

    async def otp_handover(self, app: str, caller: str = "external") -> "otplib.Code | None":
        """`POST /otp` (§18.11): a code for Tapline's host, only for an app whose setting is on in the vault file this
        daemon was started with, only while that app is in front (its code field is up), handed over once. The audit
        gets an `otp` line with the app and the source, never the code. None (the route's 404) otherwise."""
        path = getattr(getattr(self, "ceiling", None), "accounts", None)
        if not path or app not in self.accounts.get(path).otp:
            return None
        if self.scheduled_running():
            raise Refused("busy", "a scheduled run is using the phone; try the one-time code again when it ends")

        async def in_one_turn() -> "otplib.Code | None":
            # One read of the screen, which waits out a notification banner over the app (the code's own banner, seen in
            # the joint check 2026-11-14: while it shows, iOS says SpringBoard is in front), tells both which app is in
            # front and whether its code field shows NOW, by the strict test typing uses (M3). The check and the read of
            # the code are one turn on the phone: nobody switches apps between them (CodeRabbit, PR #12, center_tool).
            seen_before = self.code_fields.shown_at(app)
            tree = await self.session.refresh()
            if not tree.app or tree.app[1] != app:
                self.log(f"one-time code for {app}: none handed over, the app isn't in front")
                return None
            if not any(notes_lib.otp_target(e) for e in tree.items):
                self.log(f"one-time code for {app}: none handed over, no code field shows")
                return None
            held = self.otp_codes.take(app)  # (one read for typing a moment ago, not typed)
            if held is not None:
                return held
            field_at = self.code_fields.shown_at(app)
            if seen_before is None or field_at is None:
                # Tapline's host drove the app to its code field, unseen by this daemon: this read's sighting isn't
                # when it appeared, so the field counts from 2 minutes ago
                field_at = time.time() - notes_lib.WINDOW + FIELD_MARGIN
            return await self._code_from_center(app, field_at)
        try:
            # Admission and counting have no await between them. This external caller's read must prevent a
            # schedule admitted from stale idle status, and its quiet period covers the host typing the code next.
            # The scheduler's own notification trigger deliberately does not use this external-call accounting.
            with self._phone_use(f"otp:{caller}"):
                found = await self._on_phone(in_one_turn)
        except Refused as e:
            if e.code == "internal":
                # Notification Center couldn't be shown closed (NOT_CLOSED): never swallowed as "no code". /otp answers
                # this error and hands no code over, so the host knows the phone wasn't put back (Codex, PR #12 cycle 3, M1)
                self.log(f"one-time code for {app}: none handed over (internal: {e})")
                raise
            self.log(f"one-time code for {app}: none handed over ({type(e).__name__}: {e.code})")
            return None
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
        the phone, and `paused` while the live view is paused: the check never reads then. In use is the schedules' own
        test (Daemon.phone_in_use: a call in flight or in the last 90 s, a person's input in the last 90 s, a scheduled
        run going), not the queue's 3 s (review 1.7 B-H1)."""
        if self.control.paused:
            raise Refused("paused", "paused by the user in the live view")
        if self.phone_in_use():
            raise Refused("busy", "the phone is in use; the check waits")
        if self.state == "locked":  # (a locked phone is never swiped: the lock screen, then its passcode screen)
            raise Refused("locked", PHONE_LOCKED)
        from . import schedules
        secret = schedules.trigger_secret()  # (the digests are HMACs under it: never a plain hash of a code, L5)
        if secret is None:
            raise Refused("internal", f"the notification trigger's key at {schedules.trigger_key_path()} can't be read or "
                                      "made; nothing was read")
        await self.wait_connected()
        # the trigger's app as written in the schedule, a bundle id or a name (review 1.7 B-L6: a name never matched a
        # bundle id, so `--on-notification Wayfare` polled forever): one installed app, or refused
        from .server import resolve_app
        bundle, why = resolve_app(app, await self.installed_names())
        if bundle is None:
            raise Refused("not_found", f"the trigger's app: {why}; nothing was read")
        async def admitted() -> "list[notes_lib.Notification]":
            # The decision above was made before awaits (connecting, the installed apps): an agent's call may have
            # started or ended since. It is made again here, inside the phone's turn, where nothing else runs before the
            # read: another client's call in flight or in the last 90 s, or a person, means busy (Codex, PR #12 cycle 2,
            # M4). The check's own turn isn't counted against it.
            if self.control.paused:
                raise Refused("paused", "paused by the user in the live view")
            if self._someone_else_on_phone(TRIGGER_CLIENT):
                raise Refused("busy", "the phone is in use; the check waits")
            found, _ = await self._read_center(lambda b: b == bundle, caption="Check for notifications")
            return found
        if self.phone_in_use():  # (and once more right before the turn is asked for)
            raise Refused("busy", "the phone is in use; the check waits")
        found = await self._on_phone(admitted)
        return [x.key(secret) for x in found]

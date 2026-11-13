"""The host's dialog (AGENT-TOOLS 1.13 section 11.3, 11.5).

The daemon holds the phone and decides that a person must answer; the MCP server is the one that can ask the host
(elicitation is a request from an MCP server to its client). So the daemon keeps, for each agent that can show a dialog,
a channel of events: `ask` (show this) and `close` (take it down). The MCP server long-polls it while a call is in
flight and answers with `POST /api/dialog_answer`. This module holds what both sides share: the words, the choices and
the channel."""
from __future__ import annotations

import asyncio
import unicodedata

ALLOW, DENY = "Allow once", "Don't allow"  # a person's OK: the one required field's choices, no default
DONE, CANT = "Done", "Can't now"  # a "needs a person" moment's
CONFIRM, NEEDS = "confirm", "needs"  # the kinds of ask
CHOICES = {CONFIRM: [ALLOW, DENY], NEEDS: [DONE, CANT]}
# what the MCP server sends back for each choice; anything else, and no answer at all, is the refusal
ANSWERS = {ALLOW: "allow", DENY: "decline", DONE: "done", CANT: "cant"}
CLIP = 80  # characters of the app's own text a caption keeps
KEEP = 50  # events a channel remembers
MAX_POLL = 25.0  # seconds a poll may wait


def clean(text: str | None, limit: int = CLIP) -> str:
    """Text from the app, safe to show in the host's dialog: control and format characters gone (the ones that hide or
    reorder text too), whitespace made one space, double quotes made single so it can't end its own quotation, cut to
    `limit` characters."""
    spaced = "".join(" " if ch.isspace() else ch for ch in (text or ""))
    plain = "".join(ch for ch in spaced if not unicodedata.category(ch).startswith("C")).replace('"', "'")
    plain = " ".join(plain.split())
    return plain if len(plain) <= limit else plain[:limit - 1].rstrip() + "…"


VERBS = {"tap": "tap", "long_press": "long-press", "toggle": "toggle", "swipe": "tap", "alert": "press"}


def caption(tool: str, label: str, app: str | None, word: str) -> str:
    """The tool's own words for a policy request: `Allow: tap "Delete trip" in Wayfare? (policy word "Delete")`."""
    where = clean(app) if app else "the app in front"
    return f'Allow: {VERBS.get(tool, tool)} "{clean(label)}" in {where}? (policy word "{clean(word)}")'


class Channel:
    """The events for one agent's host, oldest first, each with a sequence number. A poll says the last number it has."""

    def __init__(self) -> None:
        self.events: list[tuple[int, dict]] = []
        self.seq = 0
        self._changed = asyncio.Event()

    def push(self, event: dict) -> None:
        self.seq += 1
        self.events.append((self.seq, event))
        del self.events[:-KEEP]
        woken, self._changed = self._changed, asyncio.Event()
        woken.set()

    def after(self, since: int) -> list[dict]:
        return [event for n, event in self.events if n > since]

    async def poll(self, since: int, wait: float) -> dict:
        """The events after `since`, waiting up to `wait` seconds for one; and the number to poll from next."""
        if not self.after(since) and wait > 0:
            try:
                await asyncio.wait_for(self._changed.wait(), min(wait, MAX_POLL))
            except asyncio.TimeoutError:
                pass
        return {"seq": self.seq, "events": self.after(since)}

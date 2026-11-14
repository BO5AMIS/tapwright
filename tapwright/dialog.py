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
GRANT_ALLOW = "Allow"  # §19.6: the grant's question (the choices are Allow and Don't allow, no default)
YES, NO = "Yes", "No"  # §19.6: the separate drafts question; it defaults to No
DRAFTS_QUESTION = "Send these without showing you each one first?"
CONFIRM, NEEDS, GRANT, DRAFTS = "confirm", "needs", "grant", "drafts"  # the kinds of ask
CHOICES = {CONFIRM: [ALLOW, DENY], NEEDS: [DONE, CANT], GRANT: [GRANT_ALLOW, DENY], DRAFTS: [NO, YES]}
# what the MCP server sends back for each choice; anything else, and no answer at all, is the refusal
ANSWERS = {ALLOW: "allow", DENY: "decline", DONE: "done", CANT: "cant", GRANT_ALLOW: "allow", YES: "yes", NO: "no"}
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


DRAFT = "draft"  # §21.3: the draft's ask; its choices come with it (`Send` and `Don't send`)
DRAFT_LIMIT = 500  # characters of the draft the dialog shows


def draft_text(text: str, scrub=None) -> str:
    """A draft as the dialog shows it (§21.3): redacted of account and vault values FIRST (so no half of one is left by
    the cut), control characters gone, whitespace made one space, double quotes single (it can't end its own quotation),
    cut at DRAFT_LIMIT characters with `…(+N)`."""
    redacted = scrub(text) if scrub is not None else text
    spaced = "".join(" " if ch.isspace() else ch for ch in redacted)
    plain = " ".join("".join(ch for ch in spaced if not unicodedata.category(ch).startswith("C")).replace('"', "'").split())
    return plain if len(plain) <= DRAFT_LIMIT else f"{plain[:DRAFT_LIMIT]}…(+{len(plain) - DRAFT_LIMIT})"


ONE_TIME_CODE = "A one-time code is in this draft."  # said whenever a draft holds one (review 1.7, H3)


def draft_caption(word: str, app: str | None, text: str | None, code: bool = False) -> str:
    """`Send this in Wayfare? "<the field's text>"`; the word is the policy word that matched. `code`: the draft holds a
    one-time code, which the text shows only as its placeholder, so the caption says it in plain words."""
    where = clean(app) if app else "the app in front"
    said = f' "{text}"' if text is not None else " (no text found in the field)"
    warn = f" {ONE_TIME_CODE}" if code else ""
    return f"{word[:1].upper()}{word[1:]} this in {where}?{said}{warn}"


def draft_choices(word: str) -> list[str]:
    return [f"{word[:1].upper()}{word[1:]}", f"Don't {word.lower()}"]


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

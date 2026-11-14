"""The MCP server's side of the host's dialog (AGENT-TOOLS 1.13 section 11.3, 11.5; see dialog.py for the daemon's).

While a tool call is in flight, this polls the daemon for `ask` events, shows each one to the person through the host
(MCP elicitation, form mode), and posts the answer back. A `close` event, sent when the live view answered first or the
moment cleared, aborts the dialog. One dialog is up at a time; a second waits behind the first. Only the one required
choice counts: `accept` with `Allow once` (or `Done`) is the yes; `decline`, `cancel`, an error, or `accept` without
the choice is the no. Nothing the model writes can answer a dialog: the host draws it and the person answers it."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Literal

from mcp.server.elicitation import AcceptedElicitation
from mcp.types import ClientCapabilities, ElicitationCapability, FormElicitationCapability
from pydantic import BaseModel, Field

from . import dialog

logger = logging.getLogger("tapwright")

POLL_WAIT = 20.0  # seconds one poll to the daemon waits for an event
RETRY = 1.0  # seconds before the next poll after one that failed (the daemon is starting, or restarted)
EXTRA_WAIT = 300.0  # seconds a call with a dialog may take beyond its usual limit: a person is answering (120 s each)


class Confirm(BaseModel):
    """A person's OK: one required field with two choices and no default."""
    answer: Literal["Allow once", "Don't allow"] = Field(title="Your answer")


class Needs(BaseModel):
    """A moment that needs a person: one required field with two choices and no default."""
    answer: Literal["Done", "Can't now"] = Field(title="Your answer")


class GrantAsk(BaseModel):
    """§19.6, the grant's question: one required field, the choices Allow and Don't allow, no default."""
    answer: Literal["Allow", "Don't allow"] = Field(title="Your answer")


class DraftsAsk(BaseModel):
    """§19.6, the separate second question for a word that reaches other people: it defaults to No."""
    answer: Literal["No", "Yes"] = Field(default="No", title="Send without showing drafts?")


def draft_model(choices: list[str]) -> type[BaseModel]:
    """§21.3, a draft's question: one required field with the two words the daemon sent (`Send`, `Don't send`), no default."""
    from pydantic import create_model
    return create_model("DraftAsk", answer=(Literal[tuple(choices)], Field(title="Your answer")))  # type: ignore[valid-type]


MODELS: dict[str, type[BaseModel]] = {dialog.CONFIRM: Confirm, dialog.NEEDS: Needs, dialog.GRANT: GrantAsk,
                                      dialog.DRAFTS: DraftsAsk}
REFUSAL = {dialog.CONFIRM: "decline", dialog.NEEDS: "cant", dialog.GRANT: "decline", dialog.DRAFTS: "no"}
FORM = ClientCapabilities(elicitation=ElicitationCapability(form=FormElicitationCapability()))


def supports_form(ctx: object) -> bool:
    """Does this call's client say it can show an elicitation form? False outside a request (a test, a direct call).

    The SDK's check (`Connection.check_capability`) only asks that `elicitation` was declared, so `elicitation: {}`
    (older clients' word for form mode) counts. The SDK sets no timeout on the request (`send_request` passes none), so
    a person taking the full 120 s still answers; tests/test_hostdialog.py pins both."""
    try:
        return bool(ctx.request_context.session.check_client_capability(FORM))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return False


def has_progress_token(ctx: object) -> bool:
    """Did the client of this call give a progress token (so it takes progress notifications)? False outside a request."""
    try:
        return ctx.request_context.meta.progress_token is not None  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return False


class HostDialogs:
    """The poll loop and the dialogs for one MCP server (one agent). `poll(since)` and `answer(id, answer)` reach the daemon."""

    def __init__(self, poll: Callable[[int, float], dict], answer: Callable[[str, str], object],
                 enabled: bool = True) -> None:
        self.enabled = enabled
        self._poll, self._answer = poll, answer
        self._calls: list[object] = []  # the contexts of the calls in flight
        self._pump: asyncio.Task | None = None
        self._shown: dict[str, asyncio.Task] = {}
        self._lock: asyncio.Lock | None = None
        self._since = 0
        self._progressed = 0

    @asynccontextmanager
    async def during(self, ctx: object, progress: bool = False) -> AsyncIterator[bool]:
        """Around one call to the daemon: yields whether the host can show a dialog (then the call says so, and the
        daemon waits for a person). The poll runs while any such call is in flight. `progress`: a long call (`explore`) whose
        client gave a progress token is polled too, for its progress lines (AGENT-TOOLS §25.2), with no dialog."""
        form = self.enabled and supports_form(ctx)
        if not form and not (progress and self.enabled and has_progress_token(ctx)):
            yield False
            return
        self._calls.append(ctx)
        if self._pump is None or self._pump.done():
            self._pump = asyncio.create_task(self._run(), name="tapwright-dialog-poll")
        try:
            yield form
        finally:
            self._calls.remove(ctx)
            if not self._calls:
                await self._stop()

    async def _stop(self) -> None:
        tasks = [t for t in (self._pump, *self._shown.values()) if t is not None]
        self._pump, self._shown = None, {}
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _run(self) -> None:
        while True:
            try:
                got = await asyncio.to_thread(self._poll, self._since, POLL_WAIT)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001  the daemon isn't there for a moment: try again
                await asyncio.sleep(RETRY)
                continue
            if not isinstance(got, dict):
                await asyncio.sleep(RETRY)
                continue
            seq = got.get("seq")
            if isinstance(seq, int) and seq < self._since:  # a daemon that started again numbers from the start
                self._since = 0
                continue
            for event in got.get("events") or []:
                self._handle(event)
            if isinstance(seq, int):
                self._since = seq

    def _handle(self, event: object) -> None:
        if not isinstance(event, dict) or not isinstance(event.get("id"), str):
            return
        ask_id = event["id"]
        if event.get("type") == "progress" and self._calls:  # an explore's `explored 23 places, 4 findings` (§25.2)
            self._progressed += 1
            asyncio.create_task(self._progress(self._progressed, str(event.get("message") or ""), self._calls[0]),
                                name=f"tapwright-progress-{ask_id}")
            return
        if event.get("type") == "notice" and self._calls:  # the watchdog (§24.5): said in chat, nothing to answer
            asyncio.create_task(self._notice(str(event.get("message") or ""), self._calls[0]),
                                name=f"tapwright-notice-{ask_id}")
            return
        if event.get("type") == "ask" and ask_id not in self._shown and self._calls:
            self._shown[ask_id] = asyncio.create_task(self._show(event, self._calls[0]), name=f"tapwright-ask-{ask_id}")
        elif event.get("type") == "close":
            task = self._shown.pop(ask_id, None)
            if task is not None:
                task.cancel()

    @staticmethod
    async def _progress(count: int, message: str, ctx: object) -> None:
        """One progress notification (never part of the final answer); a client that takes none loses nothing."""
        try:
            await ctx.report_progress(float(count), message=message)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    async def _notice(message: str, ctx: object) -> None:
        """A message for the person, with no answer: an MCP log message at warning level (the host shows it in chat
        where it shows them). The daemon also puts it in the agent's next answer, so a host that shows none loses
        nothing."""
        if not message:
            return
        try:
            await ctx.session.send_log_message(level="warning", data=message, logger="tapwright")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001  a host that takes no log messages: the answer's Note still says it
            pass

    async def _show(self, event: dict, ctx: object) -> None:
        choices = event.get("choices")
        drafting = (event.get("kind") == dialog.DRAFT and isinstance(choices, list) and len(choices) == 2
                    and all(isinstance(c, str) and c for c in choices))
        kind = dialog.DRAFT if drafting else event.get("kind") if event.get("kind") in MODELS else dialog.CONFIRM
        if self._lock is None:
            self._lock = asyncio.Lock()
        try:
            async with self._lock:  # one dialog at a time: the next waits behind this one
                value = await self._ask(kind, str(event.get("message") or ""), ctx, choices if drafting else None)
            await asyncio.to_thread(self._answer, event["id"], value)
        except asyncio.CancelledError:  # the live view answered first, or the moment cleared: nothing to say
            raise
        except Exception:  # noqa: BLE001  the answer didn't get there: the daemon's own limit ends the wait
            logger.exception("the host dialog's answer didn't reach tapwright serve")
        finally:
            self._shown.pop(event["id"], None)

    @staticmethod
    async def _ask(kind: str, message: str, ctx: object, choices: list[str] | None = None) -> str:
        """What the person said, as the daemon's word for it. The refusal for anything but the plain yes. A draft's
        question (§21.3) has the daemon's own two words: the first (`Send`) is the yes."""
        if kind == dialog.DRAFT and choices:
            try:
                result = await ctx.elicit(message, draft_model(choices))  # type: ignore[attr-defined]
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                return "decline"
            said = getattr(result, "data", None)
            return "allow" if isinstance(result, AcceptedElicitation) and getattr(said, "answer", "") == choices[0] else "decline"
        try:
            result = await ctx.elicit(message, MODELS[kind])  # type: ignore[attr-defined]
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001  the host failed to show it, or said no in an odd way
            return REFUSAL[kind]
        if isinstance(result, AcceptedElicitation):
            return dialog.ANSWERS.get(getattr(result.data, "answer", ""), REFUSAL[kind])
        return REFUSAL[kind]

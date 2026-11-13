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


MODELS: dict[str, type[BaseModel]] = {dialog.CONFIRM: Confirm, dialog.NEEDS: Needs}
REFUSAL = {dialog.CONFIRM: "decline", dialog.NEEDS: "cant"}
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

    @asynccontextmanager
    async def during(self, ctx: object) -> AsyncIterator[bool]:
        """Around one call to the daemon: yields whether the host can show a dialog (then the call says so, and the
        daemon waits for a person). The poll runs while any such call is in flight."""
        if not self.enabled or not supports_form(ctx):
            yield False
            return
        self._calls.append(ctx)
        if self._pump is None or self._pump.done():
            self._pump = asyncio.create_task(self._run(), name="tapwright-dialog-poll")
        try:
            yield True
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
        if event.get("type") == "ask" and ask_id not in self._shown and self._calls:
            self._shown[ask_id] = asyncio.create_task(self._show(event, self._calls[0]), name=f"tapwright-ask-{ask_id}")
        elif event.get("type") == "close":
            task = self._shown.pop(ask_id, None)
            if task is not None:
                task.cancel()

    async def _show(self, event: dict, ctx: object) -> None:
        kind = event.get("kind") if event.get("kind") in MODELS else dialog.CONFIRM
        if self._lock is None:
            self._lock = asyncio.Lock()
        try:
            async with self._lock:  # one dialog at a time: the next waits behind this one
                value = await self._ask(kind, str(event.get("message") or ""), ctx)
            await asyncio.to_thread(self._answer, event["id"], value)
        except asyncio.CancelledError:  # the live view answered first, or the moment cleared: nothing to say
            raise
        except Exception:  # noqa: BLE001  the answer didn't get there: the daemon's own limit ends the wait
            logger.exception("the host dialog's answer didn't reach tapwright serve")
        finally:
            self._shown.pop(event["id"], None)

    @staticmethod
    async def _ask(kind: str, message: str, ctx: object) -> str:
        """What the person said, as the daemon's word for it. The refusal for anything but the plain yes."""
        try:
            result = await ctx.elicit(message, MODELS[kind])  # type: ignore[attr-defined]
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001  the host failed to show it, or said no in an odd way
            return REFUSAL[kind]
        if isinstance(result, AcceptedElicitation):
            return dialog.ANSWERS.get(getattr(result.data, "answer", ""), REFUSAL[kind])
        return REFUSAL[kind]

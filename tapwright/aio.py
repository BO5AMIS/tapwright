"""Waiting that is safe to cancel, for daemon.py and device.py alike."""
from __future__ import annotations

import asyncio
from typing import Awaitable, TypeVar

_T = TypeVar("_T")


def _retrieved(task: asyncio.Future) -> None:
    """A finished child's error is read, so it is never logged as `never retrieved`."""
    if not task.cancelled():
        task.exception()


async def _join(inner: asyncio.Future) -> None:
    """Cancel the child and wait until it has ended (its own cleanup included), whatever it ended with."""
    inner.cancel()
    while not inner.done():
        try:
            await asyncio.wait({inner})
        except asyncio.CancelledError:
            # the caller is cancelled again while the child still cleans up: the child is waited for all the same,
            # then the cancel goes on (it is raised by the caller's own `raise`, after this)
            inner.cancel()
            inner.add_done_callback(_retrieved)
            raise
    _retrieved(inner)


async def wait_for(awaitable: Awaitable[_T], seconds: float) -> _T:
    """`asyncio.wait_for`, except that
    - a cancel of the caller is never lost (before Python 3.12, wait_for gives its result and drops the cancel when
      the two come in the same loop turn, and a loop that waits for something again then never ends: found in
      `_watch_logs`, which `supervise` cancels), and
    - the child is always cancelled and joined before this returns or raises, and its error is always retrieved
      (cleanup of a DVT session that is still running must not outlive the caller)."""
    inner = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait({inner}, timeout=seconds)
    except BaseException:  # the caller was cancelled (or the loop is closing)
        await _join_quietly(inner)
        raise
    if not done:
        await _join(inner)
        raise asyncio.TimeoutError
    return inner.result()


async def _join_quietly(inner: asyncio.Future) -> None:
    """_join for a caller that is being cancelled: the cancel is already on its way, and stays."""
    try:
        await _join(inner)
    except asyncio.CancelledError:
        pass

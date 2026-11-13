"""The cancellation-safe wait that daemon.py and device.py share (Codex, PR #4): the child is always cancelled AND
joined, its error is always retrieved, and a cancel of the caller is never lost. Runs on Python 3.10 too."""
import asyncio
import gc

import pytest

from tapwright import aio


def run(go):
    asyncio.run(go())


def test_a_result_comes_back_and_an_error_of_the_child_is_raised():
    async def ok():
        await asyncio.sleep(0)
        return 7

    async def bad():
        raise ValueError("no")

    async def go():
        assert await aio.wait_for(ok(), 1) == 7
        with pytest.raises(ValueError):
            await aio.wait_for(bad(), 1)
    run(go)


def test_a_timeout_waits_for_the_childs_slow_cleanup_before_it_raises():
    log: list[str] = []

    async def child():
        try:
            await asyncio.sleep(60)
        finally:
            await asyncio.sleep(0.05)  # asynchronous cleanup, as a DVT session's close
            log.append("cleaned")

    async def go():
        with pytest.raises(asyncio.TimeoutError):
            await aio.wait_for(child(), 0.01)
        assert log == ["cleaned"]  # joined: nothing is left running behind the caller
    run(go)


def test_a_cancel_of_the_caller_joins_the_child_and_is_not_lost():
    log: list[str] = []

    async def child():
        try:
            await asyncio.sleep(60)
        finally:
            await asyncio.sleep(0.05)
            log.append("cleaned")

    async def go():
        caller = asyncio.create_task(aio.wait_for(child(), 60))
        await asyncio.sleep(0.01)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert log == ["cleaned"]
    run(go)


def test_a_child_that_fails_in_the_turn_of_the_cancel_is_still_retrieved_and_the_cancel_is_kept():
    seen: list[dict] = []

    async def go():
        asyncio.get_running_loop().set_exception_handler(lambda loop, context: seen.append(context))
        go_on = asyncio.Event()

        async def child():
            await go_on.wait()
            raise RuntimeError("cleanup failed")
        caller = asyncio.create_task(aio.wait_for(child(), 60))
        await asyncio.sleep(0.01)
        go_on.set()  # the child fails ...
        caller.cancel()  # ... in the same loop turn the caller is cancelled
        with pytest.raises(asyncio.CancelledError):
            await caller
        gc.collect()
        await asyncio.sleep(0)
        gc.collect()
    run(go)
    assert seen == []  # no "Task exception was never retrieved"


def test_a_child_that_ignores_the_first_cancel_is_still_joined():
    log: list[str] = []

    async def child():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)  # finishes its work before it lets the cancel through
            log.append("finished")
            raise

    async def go():
        with pytest.raises(asyncio.TimeoutError):
            await aio.wait_for(child(), 0.01)
        assert log == ["finished"]
    run(go)


def test_a_second_cancel_of_the_caller_still_waits_for_the_childs_nested_cleanup():
    """Codex, PR #4 cycle 2: the second cancel made the helper return while the child's next cleanup still ran."""
    log: list[str] = []

    async def child():
        try:
            await asyncio.sleep(60)
        finally:
            try:
                await asyncio.sleep(0.05)
            finally:
                await asyncio.sleep(0.05)
                log.append("inner cleaned")
            log.append("outer cleaned")

    async def go():
        caller = asyncio.create_task(aio.wait_for(child(), 60))
        await asyncio.sleep(0.01)
        caller.cancel()
        await asyncio.sleep(0.02)  # the first cleanup is running
        caller.cancel()  # and the caller is cancelled again
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert log == ["inner cleaned", "outer cleaned"]  # joined: nothing of the child runs after the caller is gone
    run(go)


def test_a_timeout_joined_while_the_caller_is_cancelled_twice_keeps_the_cancel():
    log: list[str] = []

    async def child():
        try:
            await asyncio.sleep(60)
        finally:
            await asyncio.sleep(0.1)
            log.append("cleaned")

    async def go():
        caller = asyncio.create_task(aio.wait_for(child(), 0.01))
        await asyncio.sleep(0.03)  # timed out; the join is waiting for the cleanup
        caller.cancel()
        await asyncio.sleep(0.01)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert log == ["cleaned"]
    run(go)

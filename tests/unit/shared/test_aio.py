"""``wait_released``: a cancelled waiter never cancels the work it waited on."""

from __future__ import annotations

import asyncio

import pytest

from oce.shared.aio import wait_released


async def test_result_and_exception_reach_the_waiter() -> None:
    done = asyncio.get_running_loop().create_future()
    done.set_result(3)
    assert await wait_released(done) == 3

    async def fail() -> int:
        raise ValueError("provider failed")

    with pytest.raises(ValueError, match="provider failed"):
        await wait_released(asyncio.create_task(fail()))


async def test_cancelling_the_waiter_releases_it_and_leaves_the_task_running() -> None:
    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, object]] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    try:
        gate = asyncio.Event()

        async def request() -> int:
            await gate.wait()
            raise RuntimeError("failed after the waiter left")

        task = asyncio.create_task(request())
        # The owner consumes the outcome of work nobody waits on any more.
        task.add_done_callback(lambda done: done.exception())
        waiter = asyncio.create_task(wait_released(task))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not task.done()

        gate.set()
        with pytest.raises(RuntimeError):
            await task
        await asyncio.sleep(0)
        assert unhandled == []
    finally:
        loop.set_exception_handler(previous)

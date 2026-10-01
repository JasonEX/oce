"""Asyncio helpers for work that outlives the callers waiting on it."""

from __future__ import annotations

import asyncio
from typing import TypeVar

T = TypeVar("T")


async def wait_released(task: asyncio.Future[T]) -> T:
    """Await ``task``; cancelling the caller releases the wait, never the task.

    This is the contract an in-flight provider request needs: a waiter that
    stops caring (a decisive SQL answer, a disconnected client) must not
    cancel an HTTP call mid-response, because the pooled connection would not
    be returned. ``asyncio.shield`` gives the same guarantee but, since
    Python 3.14, reports an exception the task raises after its waiter left as
    an unhandled error even when the owner consumes it; the owner of a
    released task retrieves its outcome with a done callback instead.
    """
    if not task.done():
        released = asyncio.get_running_loop().create_future()

        def wake(_: asyncio.Future[T]) -> None:
            if not released.done():
                released.set_result(None)

        task.add_done_callback(wake)
        try:
            await released
        finally:
            task.remove_done_callback(wake)
    return task.result()

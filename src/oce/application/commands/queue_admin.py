"""Queue administration: realign the queue with the database, or purge it.

The queue is a projection of the database's pending blobs, not the
authority, and the two drift: after a blob is deleted or rebuilt its old
message is still in flight and a worker pops it for nothing, and worse, a
leftover in the pending sentinel set keeps that blob from ever being
enqueued again. ``mode="sync"`` (default) drops queue entries the database
does not list as pending and enqueues the ones it misses; ``mode="purge"``
empties the queue and re-enqueues everything pending. The worker's maintenance
context drains active batches and serializes resets with its lifecycle.
``requeue=False`` only cleans immediately; periodic worker replay still restores
missing delivery for durable pending blobs.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass
from typing import Literal

from oce.application.queue import Queue
from oce.application.uow import UnitOfWorkFactory


@dataclass(frozen=True)
class ResetQueueCommand:
    """Reset the queue to the database's pending blobs; ``requeue=False`` only cleans."""

    mode: Literal["sync", "purge"] = "sync"
    requeue: bool = True


@dataclass(frozen=True)
class ResetQueueResult:
    """Entries removed, entries enqueued, and the final main queue length."""

    removed: int
    requeued: int
    queue_size: int
    db_pending: int


class ResetQueueCommandHandler:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        queue: Queue | None = None,
        maintenance: Callable[[], AbstractAsyncContextManager[None]] | None = None,
    ) -> None:
        # The queue exists exactly when the worker does; ``maintenance`` is
        # that worker's context, so a reset never races a consuming batch.
        self._uow_factory = uow_factory
        self._queue = queue
        self._maintenance = maintenance or nullcontext

    async def handle(self, command: ResetQueueCommand) -> ResetQueueResult:
        async with self._maintenance():
            return await self._reset(command)

    async def _reset(self, command: ResetQueueCommand) -> ResetQueueResult:
        if self._queue is None:
            return ResetQueueResult(0, 0, 0, 0)

        async with self._uow_factory() as uow:
            pending = set(await uow.blobs.list_pending_names())

        if command.mode == "purge":
            removed = await self._queue.purge()
        else:
            removed = await self._queue.retain(pending)

        requeued = 0
        if command.requeue:
            inflight = await self._queue.inflight_set()
            for blob_name in sorted(pending - inflight):
                await self._queue.enqueue(blob_name)
                requeued += 1

        return ResetQueueResult(
            removed=removed,
            requeued=requeued,
            queue_size=await self._queue.size(),
            db_pending=len(pending),
        )

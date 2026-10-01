"""Queue health: main queue length, in-flight count, database pending count.

Without a queue (personal mode, or the worker disabled) the snapshot is
``enabled=False`` with zeros. ``worker_state`` is the consumer pool's
lifecycle state, ``disabled`` when there is no worker.
"""

from __future__ import annotations

from dataclasses import dataclass

from oce.application.queue import Queue
from oce.application.uow import UnitOfWorkFactory
from oce.application.worker import EmbedWorker


@dataclass(frozen=True)
class QueueStatusQuery:
    pass


@dataclass(frozen=True)
class QueueStatusResult:
    enabled: bool
    main_size: int
    inflight: int
    db_pending: int
    worker_state: str = "disabled"


class QueueStatusQueryHandler:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        queue: Queue | None = None,
        worker: EmbedWorker | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._queue = queue
        self._worker = worker

    async def handle(self, _query: QueueStatusQuery) -> QueueStatusResult:
        if self._queue is None:
            return QueueStatusResult(
                enabled=False, main_size=0, inflight=0, db_pending=0
            )
        async with self._uow_factory() as uow:
            db_pending = len(await uow.blobs.list_pending_names())
        return QueueStatusResult(
            enabled=True,
            main_size=await self._queue.size(),
            inflight=len(await self._queue.inflight_set()),
            db_pending=db_pending,
            worker_state=(
                self._worker.state.value if self._worker is not None else "disabled"
            ),
        )

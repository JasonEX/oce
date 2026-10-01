"""EmbedWorker: consume the embedding queue and embed pending blobs.

    dequeue_many(blob_names) -> prepare -> write_vectors -> complete
    success: ack each blob; batch failure: retry each blob alone and count
    the failure on the ones that still fail

The database owns pending work; Redis is its delivery projection. Startup
and periodic replay repair work that committed before an enqueue failed.
Every batch builds its own pipeline inside its own unit of work.

The consumer pool is an explicit state machine (docs/runtime-lifecycle.md):

    stopped     -> recovering   start: recover processing, bounded replay
    recovering  -> running      consumers and periodic replay started
    recovering  -> stopped      recovery failed; start may be retried
    running     -> draining     maintenance: no new batches, active ones finish
    draining    -> maintenance  every active batch committed and acked
    maintenance -> recovering   maintenance done; consumption resumes
    stopped     -> maintenance  maintenance on a worker that was not running
    maintenance -> stopped      ...which stays stopped afterwards
    running     -> stopped      shutdown cancels the consumers

One lifecycle lock serializes every transition, so maintenance and shutdown
never interleave.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum

from loguru import logger

from oce.application.commands.ingest import PipelineFactory, _index_pending_batch
from oce.application.queue import Queue
from oce.application.uow import UnitOfWorkFactory
from oce.domain.blob.blob import BlobStatus
from oce.shared.aio import wait_released

_REPLAY_PAGE_SIZE = 100
_REPLAY_MAX_PAGES = 4
_REPLAY_INTERVAL_SECONDS = 30.0


class WorkerState(StrEnum):
    STOPPED = "stopped"
    RECOVERING = "recovering"
    RUNNING = "running"
    DRAINING = "draining"
    MAINTENANCE = "maintenance"


_TRANSITIONS: dict[WorkerState, frozenset[WorkerState]] = {
    WorkerState.STOPPED: frozenset({WorkerState.RECOVERING, WorkerState.MAINTENANCE}),
    WorkerState.RECOVERING: frozenset({WorkerState.RUNNING, WorkerState.STOPPED}),
    WorkerState.RUNNING: frozenset({WorkerState.DRAINING, WorkerState.STOPPED}),
    WorkerState.DRAINING: frozenset({WorkerState.MAINTENANCE}),
    WorkerState.MAINTENANCE: frozenset({WorkerState.RECOVERING, WorkerState.STOPPED}),
}


class EmbedWorker:
    def __init__(
        self,
        *,
        queue: Queue,
        uow_factory: UnitOfWorkFactory,
        pipeline_factory: PipelineFactory,
        concurrency: int = 2,
        blob_batch_size: int = 16,
        max_retries: int = 3,
    ) -> None:
        if blob_batch_size < 1:
            raise ValueError("blob_batch_size must be positive")
        self._queue = queue
        self._uow_factory = uow_factory
        self._pipeline_factory = pipeline_factory
        self._concurrency = max(1, concurrency)
        self._blob_batch_size = blob_batch_size
        self._max_retries = max_retries
        self._state = WorkerState.STOPPED
        self._tasks: list[asyncio.Task[None]] = []
        self._replay_task: asyncio.Task[None] | None = None
        self._replay_after: str | None = None
        self._lifecycle_lock = asyncio.Lock()

    @property
    def state(self) -> WorkerState:
        return self._state

    @property
    def is_running(self) -> bool:
        return self._state is WorkerState.RUNNING

    def _enter(self, state: WorkerState) -> None:
        if state not in _TRANSITIONS[self._state]:
            raise RuntimeError(f"EmbedWorker cannot go from {self._state} to {state}")
        logger.debug("EmbedWorker {} -> {}", self._state, state)
        self._state = state

    async def start(self) -> None:
        """Recover processing and a bounded pending portion; periodic replay continues."""
        async with self._lifecycle_lock:
            if self._state is WorkerState.STOPPED:
                await self._start_locked(replay_pending=True)

    async def _start_locked(self, *, replay_pending: bool) -> None:
        self._enter(WorkerState.RECOVERING)
        try:
            recovered = await self._queue.recover_processing()
            if recovered:
                logger.info("EmbedWorker recovered {} in-flight tasks", recovered)
            if replay_pending:
                await self._replay_pending()
        except BaseException:
            self._enter(WorkerState.STOPPED)
            raise
        self._enter(WorkerState.RUNNING)
        self._tasks = [
            asyncio.create_task(self._loop(i)) for i in range(self._concurrency)
        ]
        self._replay_task = asyncio.create_task(self._replay_loop())
        logger.info("EmbedWorker started with {} consumers", self._concurrency)

    async def stop(self) -> None:
        """Cancel owned tasks; processing messages remain recoverable on restart."""
        async with self._lifecycle_lock:
            if self._state is WorkerState.RUNNING:
                self._enter(WorkerState.STOPPED)
                await self._halt(cancel=True)
                logger.info("EmbedWorker stopped")

    async def _halt(self, *, cancel: bool) -> None:
        """Wait for the consumers to exit; ``cancel=False`` lets active batches finish.

        The caller has already left ``running``, so no consumer takes a new
        batch and the replay loop stops publishing.
        """
        if self._replay_task is not None:
            self._replay_task.cancel()
            await asyncio.gather(self._replay_task, return_exceptions=True)
            self._replay_task = None
        if cancel:
            for task in self._tasks:
                task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    @asynccontextmanager
    async def maintenance(self) -> AsyncIterator[None]:
        """Pause intake, finish active batches, and resume after queue maintenance."""
        async with self._lifecycle_lock:
            resume = self._state is WorkerState.RUNNING
            try:
                if resume:
                    self._enter(WorkerState.DRAINING)
                    drain = asyncio.create_task(self._halt(cancel=False))
                    try:
                        await wait_released(drain)
                    except asyncio.CancelledError:
                        # A disconnected maintenance caller cannot cancel a
                        # batch or recover its delivery before its
                        # transaction finishes.
                        await drain
                        raise
                    finally:
                        self._enter(WorkerState.MAINTENANCE)
                else:
                    self._enter(WorkerState.MAINTENANCE)
                yield
            finally:
                if resume:
                    # The reset command owns immediate requeue policy. Normal
                    # periodic replay will still repair durable pending work.
                    await self._start_locked(replay_pending=False)
                else:
                    self._enter(WorkerState.STOPPED)

    async def _replay_pending(self) -> None:
        for _ in range(_REPLAY_MAX_PAGES):
            async with self._uow_factory() as uow:
                names = await uow.blobs.list_pending_names(
                    limit=_REPLAY_PAGE_SIZE, after=self._replay_after
                )
            if not names:
                self._replay_after = None
                return
            for blob_name in names:
                # Enqueue deduplication also covers processing messages. A
                # completion racing this page only causes an empty future run.
                await self._queue.enqueue(blob_name)
            self._replay_after = names[-1]
            if len(names) < _REPLAY_PAGE_SIZE:
                self._replay_after = None
                return

    async def _replay_loop(self) -> None:
        while self._state is WorkerState.RUNNING:
            await asyncio.sleep(_REPLAY_INTERVAL_SECONDS)
            if self._state is not WorkerState.RUNNING:
                return
            try:
                await self._replay_pending()
            except Exception as exc:
                logger.warning("EmbedWorker pending replay failed: {}", exc)

    async def _loop(self, worker_id: int) -> None:
        """One consumer: dequeue, embed, ack or fail."""
        while self._state is WorkerState.RUNNING:
            try:
                blob_names = await self._queue.dequeue_many(
                    self._blob_batch_size,
                    timeout=5,
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("worker#{} dequeue failed: {}", worker_id, e)
                await asyncio.sleep(1)
                continue

            if not blob_names:
                await asyncio.sleep(0.05)
                continue
            if self._state is not WorkerState.RUNNING:
                # Intake may have been paused while dequeue was blocked. The
                # delivery remains in processing for recovery after maintenance.
                return

            try:
                await self._process_batch(worker_id, blob_names)
            except asyncio.CancelledError:
                break

    async def _process_batch(self, worker_id: int, blob_names: list[str]) -> None:
        """Embed the batch at once; on failure isolate each blob so healthy ones are not blamed."""
        try:
            embedded = await self._embed(blob_names)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if len(blob_names) == 1:
                await self._handle_failure(worker_id, blob_names[0], exc)
                return
            logger.warning(
                "worker#{} batch of {} blobs failed; isolating individually: {}",
                worker_id,
                len(blob_names),
                exc,
            )
            for blob_name in blob_names:
                await self._process_batch(worker_id, [blob_name])
            return

        for blob_name in blob_names:
            await self._ack(worker_id, blob_name)
        logger.debug(
            "worker#{} processed {} blobs ({} chunks embedded)",
            worker_id,
            len(blob_names),
            embedded,
        )

    async def _embed(self, blob_names: list[str]) -> int:
        # The embedding round trip runs between two short transactions. Vector
        # writes are content-addressed and idempotent, so the per-blob retry
        # after a failure may upsert the same rows again; the retry state is
        # the worker's, so a failed batch is not marked errored here.
        return await _index_pending_batch(
            self._uow_factory,
            self._pipeline_factory,
            blob_names,
            mark_failures=False,
        )

    async def _ack(self, worker_id: int, blob_name: str) -> None:
        try:
            await self._queue.ack(blob_name)
        except Exception as exc:
            # The database and vector writes succeeded; a failed ack is not an
            # indexing failure. An unacknowledged delivery can be replayed
            # safely by processing recovery on the next start.
            logger.error(
                "worker#{} ack failed for blob {}: {}",
                worker_id,
                blob_name[:12],
                exc,
            )

    async def _handle_failure(
        self,
        worker_id: int,
        blob_name: str,
        error: Exception,
    ) -> None:
        logger.warning(
            "worker#{} process failed for blob {}: {}",
            worker_id,
            blob_name[:12],
            error,
        )
        try:
            should_retry = False
            async with self._uow_factory() as uow:
                blob = await uow.blobs.get(blob_name)
                if blob is not None and blob.status is BlobStatus.PENDING:
                    exceeded = blob.increment_retry(self._max_retries)
                    if exceeded:
                        blob.mark_error(str(error))
                    await uow.blobs.save(blob)
                    if exceeded:
                        await uow.blobs.delete_staging(blob_name)
                        logger.error(
                            "worker#{} blob {} retry limit exceeded; "
                            "marked error and removed staging",
                            worker_id,
                            blob_name[:12],
                        )
                    else:
                        should_retry = True
                        logger.info(
                            "worker#{} blob {} retry_count={}; staging retained",
                            worker_id,
                            blob_name[:12],
                            blob.retry_count,
                        )
                    await uow.commit()
            # Keep the processing sentinel until retry state commits so the
            # replay loop cannot publish a blob still owned by this batch.
            await self._queue.fail(blob_name)
            if should_retry:
                await self._queue.enqueue(blob_name)
        except Exception as recovery_error:
            logger.error(
                "worker#{} failure recovery failed: {}",
                worker_id,
                recovery_error,
            )

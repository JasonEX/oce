"""EmbedWorker: consume the embedding queue and embed pending blobs.

    dequeue_many(blob_names) -> IndexingPipeline.embed_pending(blob_names)
    success: ack each blob; batch failure: retry each blob alone and count
    the failure on the ones that still fail

N consumer coroutines run in parallel; ``stop()`` sets a flag and each loop
exits at its next dequeue timeout. Every batch builds its own pipeline inside
its own unit of work, so coroutines share no mutable state.
"""

from __future__ import annotations

import asyncio

from loguru import logger

from oce.application.commands.ingest import PipelineFactory
from oce.application.queue import Queue
from oce.application.uow import UnitOfWorkFactory


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
        self._running = False
        self._tasks: list[asyncio.Task] = []

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        """Recover what a crashed run left in processing, then start the consumers."""
        if self._running:
            return
        self._running = True
        recovered = await self._queue.recover_processing()
        if recovered:
            logger.info("EmbedWorker recovered {} in-flight tasks", recovered)
        self._tasks = [
            asyncio.create_task(self._loop(i)) for i in range(self._concurrency)
        ]
        logger.info("EmbedWorker started with {} consumers", self._concurrency)

    async def stop(self) -> None:
        """Set the stop flag, cancel the consumers and wait for them."""
        self._running = False
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []
        logger.info("EmbedWorker stopped")

    async def _loop(self, worker_id: int) -> None:
        """One consumer: dequeue, embed, ack or fail."""
        while self._running:
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
        # Vector writes are content-addressed and idempotent, so the per-blob
        # retry after a failed transaction may upsert the same rows again.
        async with self._uow_factory() as uow:
            pipeline = self._pipeline_factory(uow)
            embedded = await pipeline.embed_pending(
                blob_names,
                mark_failures=False,
            )
            await uow.commit()
        return embedded

    async def _ack(self, worker_id: int, blob_name: str) -> None:
        try:
            await self._queue.ack(blob_name)
        except Exception as exc:
            # The database and vector writes succeeded; a failed ack is not an
            # indexing failure. The message stays in processing and
            # recover_processing handles it safely on the next start.
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
            await self._queue.fail(blob_name)
            should_retry = False
            async with self._uow_factory() as uow:
                blob = await uow.blobs.get(blob_name)
                if blob:
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
            if should_retry:
                await self._queue.enqueue(blob_name)
        except Exception as recovery_error:
            logger.error(
                "worker#{} failure recovery failed: {}",
                worker_id,
                recovery_error,
            )

"""EmbedWorker — 消费嵌入队列，对 blob 补算向量

流程
----
    dequeue_many(blob_names) → IndexingPipeline.embed_pending(blob_names)
    成功 → 逐条 ack；整批异常 → 逐条隔离重试，失败项递增 retry_count

并发
----
启动 N 个 worker 协程并行消费（concurrency 可配）。
每个协程一个消费循环，stop() 置标志后协程在下次 dequeue 超时自然退出。
每个批次在自己的 UoW 内构造独立 IndexingPipeline，协程间不共享可变状态。
"""

from __future__ import annotations

import asyncio

from loguru import logger

from oce.application.commands.ingest import PipelineFactory
from oce.application.queue import Queue
from oce.application.uow import UnitOfWorkFactory


class EmbedWorker:
    """异步嵌入 worker（持有 queue + uow_factory + pipeline 工厂）"""

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
        """启动前先恢复上次崩溃残留，再拉起 N 个消费协程"""
        if self._running:
            return
        self._running = True
        recovered = await self._queue.recover_processing()
        if recovered:
            logger.info("EmbedWorker: 恢复 {} 条处理中残留任务", recovered)
        self._tasks = [
            asyncio.create_task(self._loop(i)) for i in range(self._concurrency)
        ]
        logger.info("EmbedWorker 启动，{} 个消费协程", self._concurrency)

    async def stop(self) -> None:
        """停止：置标志 + 取消协程并等待退出"""
        self._running = False
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []
        logger.info("EmbedWorker 已停止")

    async def _loop(self, worker_id: int) -> None:
        """单个消费协程：取任务 → 嵌入 → ack/fail"""
        while self._running:
            try:
                blob_names = await self._queue.dequeue_many(
                    self._blob_batch_size,
                    timeout=5,
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("worker#{} dequeue 异常: {}", worker_id, e)
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
        """优先整批处理；失败时逐条隔离，避免健康 blob 被共同记为失败。"""
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
        # 外部向量写入是内容寻址幂等的；事务失败后的逐条回退可安全重复 upsert。
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
            # DB/向量写入已经完成，不能把队列确认失败误记成索引失败。消息留在
            # processing，进程重启时 recover_processing 会再次安全处理。
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

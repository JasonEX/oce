"""监控指标的异步落库 sink：内存缓冲 + 后台批量 flush。

写库失败只记日志并把样本留在有界缓冲中，绝不把异常传播到主链路。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from oce.infrastructure.metrics.periodic import PeriodicTask
from oce.infrastructure.persistence.models import (
    ApiCallMetricModel,
    ResourceSampleModel,
    RetrievalMetricModel,
    TokenUsageMetricModel,
)
from oce.shared.metrics import (
    ApiCallRecord,
    ResourceSampleRecord,
    RetrievalMetricRecord,
    TokenUsageRecord,
)


@dataclass(frozen=True)
class _MetricBatch:
    api: list[ApiCallRecord]
    token: list[TokenUsageRecord]
    resource: list[ResourceSampleRecord]
    retrieval: list[RetrievalMetricRecord]

    def __len__(self) -> int:
        return (
            len(self.api) + len(self.token) + len(self.resource) + len(self.retrieval)
        )


class SqlMetricsSink(PeriodicTask):
    """采集到的指标先入内存 deque，后台协程按间隔批量写库。

    ``deque(maxlen)`` 满时自动丢弃最旧样本，防止事件循环阻塞时缓冲无界增长。
    """

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        flush_interval_seconds: float = 5.0,
        max_buffer: int = 500,
    ) -> None:
        super().__init__(interval_seconds=flush_interval_seconds, name="metrics flush")
        self._session_factory = session_factory
        self._api: deque[ApiCallRecord] = deque(maxlen=max_buffer)
        self._token: deque[TokenUsageRecord] = deque(maxlen=max_buffer)
        self._resource: deque[ResourceSampleRecord] = deque(maxlen=max_buffer)
        self._retrieval: deque[RetrievalMetricRecord] = deque(maxlen=max_buffer)

    def record_api_call(self, record: ApiCallRecord) -> None:
        self._api.append(record)

    def record_token_usage(self, record: TokenUsageRecord) -> None:
        self._token.append(record)

    def record_resource_sample(self, record: ResourceSampleRecord) -> None:
        self._resource.append(record)

    def record_retrieval(self, record: RetrievalMetricRecord) -> None:
        self._retrieval.append(record)

    async def stop(self) -> None:
        await super().stop()
        await self._flush_once()

    async def _tick(self) -> None:
        await self._flush_once()

    def _drain(self) -> _MetricBatch:
        """原子取出缓冲；失败时可按原始 record 安全放回。"""
        batch = _MetricBatch(
            api=list(self._api),
            token=list(self._token),
            resource=list(self._resource),
            retrieval=list(self._retrieval),
        )
        self._api.clear()
        self._token.clear()
        self._resource.clear()
        self._retrieval.clear()
        return batch

    @staticmethod
    def _restore_queue(queue: deque, drained: list) -> None:
        """合并失败批次和期间新到样本，超限时保留最新记录。"""
        current = list(queue)
        queue.clear()
        queue.extend([*drained, *current])

    def _restore(self, batch: _MetricBatch) -> None:
        self._restore_queue(self._api, batch.api)
        self._restore_queue(self._token, batch.token)
        self._restore_queue(self._resource, batch.resource)
        self._restore_queue(self._retrieval, batch.retrieval)

    @staticmethod
    def _to_rows(batch: _MetricBatch) -> list:
        rows: list = []
        for r in batch.api:
            rows.append(
                ApiCallMetricModel(
                    ts=r.ts,
                    endpoint=r.endpoint,
                    method=r.method,
                    status_code=r.status_code,
                    latency_ms=r.latency_ms,
                    error_type=r.error_type,
                )
            )
        for r in batch.token:
            rows.append(
                TokenUsageMetricModel(
                    ts=r.ts,
                    kind=r.kind,
                    model=r.model,
                    credential_id=r.credential_id,
                    prompt_tokens=r.prompt_tokens,
                    completion_tokens=r.completion_tokens,
                    total_tokens=r.total_tokens,
                )
            )
        for r in batch.resource:
            rows.append(
                ResourceSampleModel(
                    ts=r.ts,
                    disk_data_bytes=r.disk_data_bytes,
                    disk_free_bytes=r.disk_free_bytes,
                    disk_total_bytes=r.disk_total_bytes,
                    mem_rss_bytes=r.mem_rss_bytes,
                    mem_percent=r.mem_percent,
                    cpu_percent=r.cpu_percent,
                )
            )
        for r in batch.retrieval:
            s = r.stages
            rows.append(
                RetrievalMetricModel(
                    ts=r.ts,
                    source=r.source,
                    scope_size=r.scope_size,
                    hit_count=r.hit_count,
                    total_ms=r.total_ms,
                    intent=r.intent,
                    path_boosted=r.path_boosted,
                    rerank_route=r.rerank_route,
                    head_slots=r.head_slots,
                    query_text=r.query_text,
                    rewrite_ms=s.get("rewrite"),
                    embed_ms=s.get("embed"),
                    dense_ms=s.get("dense"),
                    exact_ms=s.get("exact"),
                    path_ms=s.get("path"),
                    path_lookup_ms=s.get("path_lookup"),
                    lexical_ms=s.get("lexical"),
                    fuse_ms=s.get("fuse"),
                    rerank_ms=s.get("rerank"),
                    llm_rerank_ms=s.get("llm_rerank"),
                    select_ms=s.get("select"),
                    expand_ms=s.get("expand"),
                )
            )
        return rows

    async def _flush_once(self) -> None:
        batch = self._drain()
        if not len(batch):
            return
        try:
            async with self._session_factory() as session:
                rows = self._to_rows(batch)
                session.add_all(rows)
                await session.commit()
        except Exception as exc:
            self._restore(batch)
            logger.warning(
                "metrics flush failed; retained buffered rows for a later flush: {}",
                exc,
            )

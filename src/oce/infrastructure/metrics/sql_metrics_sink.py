"""Metrics sink that buffers in memory and flushes to SQL in the background.

A failed flush is logged and the rows stay in the bounded buffer; nothing
propagates to the request path.
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
from oce.shared.database.session import Base
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
    """Records queue in bounded deques; a background task writes them in batches.

    A full deque drops its oldest record, so a blocked event loop cannot grow
    the buffer without bound.
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
        """Take every buffered record at once; ``_restore`` puts a failed batch back."""
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
        """Put a failed batch ahead of what arrived meanwhile; the newest survive overflow."""
        current = list(queue)
        queue.clear()
        queue.extend([*drained, *current])

    def _restore(self, batch: _MetricBatch) -> None:
        self._restore_queue(self._api, batch.api)
        self._restore_queue(self._token, batch.token)
        self._restore_queue(self._resource, batch.resource)
        self._restore_queue(self._retrieval, batch.retrieval)

    @staticmethod
    def _to_rows(batch: _MetricBatch) -> list[Base]:
        rows: list[Base] = []
        for call in batch.api:
            rows.append(
                ApiCallMetricModel(
                    ts=call.ts,
                    endpoint=call.endpoint,
                    method=call.method,
                    status_code=call.status_code,
                    latency_ms=call.latency_ms,
                    error_type=call.error_type,
                )
            )
        for usage in batch.token:
            rows.append(
                TokenUsageMetricModel(
                    ts=usage.ts,
                    kind=usage.kind,
                    model=usage.model,
                    credential_id=usage.credential_id,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    total_tokens=usage.total_tokens,
                )
            )
        for sample in batch.resource:
            rows.append(
                ResourceSampleModel(
                    ts=sample.ts,
                    disk_data_bytes=sample.disk_data_bytes,
                    disk_free_bytes=sample.disk_free_bytes,
                    disk_total_bytes=sample.disk_total_bytes,
                    mem_rss_bytes=sample.mem_rss_bytes,
                    mem_percent=sample.mem_percent,
                    cpu_percent=sample.cpu_percent,
                )
            )
        for retrieval in batch.retrieval:
            stages = retrieval.stages
            rows.append(
                RetrievalMetricModel(
                    ts=retrieval.ts,
                    source=retrieval.source,
                    scope_size=retrieval.scope_size,
                    hit_count=retrieval.hit_count,
                    total_ms=retrieval.total_ms,
                    intent=retrieval.intent,
                    path_boosted=retrieval.path_boosted,
                    rerank_route=retrieval.rerank_route,
                    dense_route=retrieval.dense_route,
                    head_slots=retrieval.head_slots,
                    exact_definitions=retrieval.exact_definitions,
                    definition_sites=retrieval.definition_sites,
                    relation_hits=retrieval.relation_hits,
                    relation_chars=retrieval.relation_chars,
                    lane_failures=(
                        ",".join(
                            f"{lane}:{error}"
                            for lane, error in sorted(retrieval.lane_failures.items())
                        )
                        or None
                    ),
                    query_text=retrieval.query_text,
                    rewrite_ms=stages.get("rewrite"),
                    embed_ms=stages.get("embed"),
                    dense_ms=stages.get("dense"),
                    exact_ms=stages.get("exact"),
                    path_ms=stages.get("path"),
                    path_lookup_ms=stages.get("path_lookup"),
                    lexical_ms=stages.get("lexical"),
                    fuse_ms=stages.get("fuse"),
                    rerank_ms=stages.get("rerank"),
                    llm_rerank_ms=stages.get("llm_rerank"),
                    select_ms=stages.get("select"),
                    expand_ms=stages.get("expand"),
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

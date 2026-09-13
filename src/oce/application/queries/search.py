"""The retrieval read path: ``SearchQuery`` and its handler."""

from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter

from oce.application.messages import Query
from oce.domain.services.retrieval import RetrievalPipeline
from oce.domain.services.search import SearchHit, SearchScope
from oce.shared.metrics import (
    MetricsSink,
    NoopMetricsSink,
    RetrievalAudit,
    RetrievalMetricRecord,
)


@dataclass(frozen=True)
class SearchQuery(Query):
    query: str
    scope: SearchScope | None = None
    source: str = "retrieval"


@dataclass(frozen=True)
class SearchResult:
    hits: list[SearchHit] = field(default_factory=list)


class SearchQueryHandler:
    """Run the pipeline and, when auditing is on, report one retrieval record.

    The audit collects stage timings and routing evidence; the record goes to
    the side-channel sink and never affects the request itself.
    """

    def __init__(
        self,
        pipeline: RetrievalPipeline,
        *,
        metrics: MetricsSink | None = None,
        retrieval_audit_enabled: bool = False,
        store_query_text: bool = False,
    ) -> None:
        self.pipeline = pipeline
        self.metrics = metrics or NoopMetricsSink()
        self.retrieval_audit_enabled = retrieval_audit_enabled
        self.store_query_text = store_query_text

    async def handle(self, query: SearchQuery) -> SearchResult:
        if not self.retrieval_audit_enabled:
            hits = await self.pipeline.search(query.query, query.scope)
            return SearchResult(hits=hits)

        audit = RetrievalAudit()
        started = perf_counter()
        hits = await self.pipeline.search(query.query, query.scope, audit=audit)
        total_ms = int((perf_counter() - started) * 1000)
        self.metrics.record_retrieval(
            RetrievalMetricRecord(
                source=query.source,
                hit_count=len(hits),
                total_ms=total_ms,
                scope_size=audit.scope_size,
                intent=audit.intent,
                path_boosted=audit.path_boosted,
                rerank_route=audit.rerank_route,
                dense_route=audit.dense_route,
                head_slots=audit.head_slots,
                exact_definitions=audit.exact_definitions,
                definition_sites=audit.definition_sites,
                relation_hits=sum(audit.relation_counts.values()),
                relation_chars=audit.relation_chars,
                query_text=query.query if self.store_query_text else None,
                stages=dict(audit.stages),
                lane_failures=dict(audit.lane_failures),
            )
        )
        return SearchResult(hits=hits)

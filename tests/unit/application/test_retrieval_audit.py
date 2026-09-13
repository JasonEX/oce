"""Retrieval audit: stage timings fill the audit and the handler reports per source."""

from __future__ import annotations

from oce.application.queries.search import SearchQuery, SearchQueryHandler
from oce.domain.services.retrieval import RetrievalPipeline
from oce.domain.services.search import SearchHit, SearchScope
from oce.shared.config.settings import RetrievalSettings
from oce.shared.metrics import RetrievalAudit, RetrievalMetricRecord
from tests.fakes.indexing import ConstantEmbedder
from tests.fakes.retrieval import FakeExactSearchStore, FakeSearchStore


class RecordingSink:
    """Sink capturing only retrieval records."""

    def __init__(self) -> None:
        self.retrieval: list[RetrievalMetricRecord] = []

    def record_retrieval(self, record: RetrievalMetricRecord) -> None:
        self.retrieval.append(record)


def _hit() -> SearchHit:
    return SearchHit(
        blob_name="h1",
        path="src/main.py",
        content="def main(): pass",
        score=0.9,
        start_line=1,
        end_line=1,
    )


def _pipeline(hits: list[SearchHit]) -> RetrievalPipeline:
    return RetrievalPipeline(
        embedder=ConstantEmbedder(),
        store=FakeSearchStore(hits=hits),
        settings=RetrievalSettings(),
    )


def _handler(hits, **kwargs) -> tuple[SearchQueryHandler, RecordingSink]:
    sink = RecordingSink()
    handler = SearchQueryHandler(_pipeline(hits), metrics=sink, **kwargs)
    return handler, sink


class TestPipelineAuditFill:
    async def test_stages_and_scope_filled(self):
        audit = RetrievalAudit()
        await _pipeline([_hit()]).search(
            "main entry", SearchScope(frozenset({"h1", "h2"})), audit=audit
        )

        # No rewriter: only the core stages run; embedding and dense recall time separately.
        assert "embed" in audit.stages
        assert "dense" in audit.stages
        assert "select" in audit.stages
        assert all(v >= 0 for v in audit.stages.values())
        assert audit.scope_size == 2
        assert audit.intent == "feature"
        assert audit.path_boosted is False
        assert audit.rerank_route == "skip:too_few_candidates"
        assert audit.head_slots == 0

    async def test_audit_none_is_zero_overhead(self):
        # Without an audit nothing is timed and nothing fails.
        hits = await _pipeline([_hit()]).search("main entry")
        assert len(hits) == 1


class TestHandlerReporting:
    async def test_failed_lanes_reach_metric_record(self) -> None:
        sink = RecordingSink()
        pipeline = RetrievalPipeline(
            embedder=ConstantEmbedder(),
            store=FakeSearchStore(hits=[_hit()]),
            exact_store=FakeExactSearchStore(
                error=RuntimeError("symbol table unavailable")
            ),
            settings=RetrievalSettings(),
        )
        handler = SearchQueryHandler(
            pipeline, metrics=sink, retrieval_audit_enabled=True
        )

        result = await handler.handle(
            SearchQuery("Where is `main` defined?", SearchScope(frozenset({"h1"})))
        )

        assert result.hits
        assert sink.retrieval[0].lane_failures == {"exact": "RuntimeError"}
        assert sink.retrieval[0].query_text is None

    async def test_reports_source_and_hit_count(self):
        handler, sink = _handler([_hit()], retrieval_audit_enabled=True)
        await handler.handle(
            SearchQuery("main entry", SearchScope(frozenset({"h1"})), source="overview")
        )

        assert len(sink.retrieval) == 1
        rec = sink.retrieval[0]
        assert rec.source == "overview"
        assert rec.hit_count == 1
        assert rec.total_ms >= 0
        assert "select" in rec.stages
        assert rec.head_slots == 0

    async def test_empty_return_recorded_as_zero(self):
        handler, sink = _handler([], retrieval_audit_enabled=True)
        result = await handler.handle(
            SearchQuery("main entry", SearchScope(frozenset({"h1"})))
        )

        assert result.hits == []
        assert len(sink.retrieval) == 1
        assert sink.retrieval[0].hit_count == 0  # an empty answer is reported

    async def test_disabled_does_not_report(self):
        handler, sink = _handler([_hit()], retrieval_audit_enabled=False)
        result = await handler.handle(SearchQuery("main entry"))

        assert len(result.hits) == 1
        assert sink.retrieval == []  # nothing is reported when auditing is off

    async def test_query_text_switch(self):
        on_handler, on_sink = _handler(
            [_hit()], retrieval_audit_enabled=True, store_query_text=True
        )
        off_handler, off_sink = _handler(
            [_hit()], retrieval_audit_enabled=True, store_query_text=False
        )
        scope = SearchScope(frozenset({"h1"}))
        await on_handler.handle(SearchQuery("secret query", scope))
        await off_handler.handle(SearchQuery("secret query", scope))

        assert on_sink.retrieval[0].query_text == "secret query"
        assert (
            off_sink.retrieval[0].query_text is None
        )  # the query text is not stored by default

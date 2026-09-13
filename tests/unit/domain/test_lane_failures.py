"""A failing lane degrades the answer and leaves a trace in the audit.

Every recall and relation lane absorbs its own exception so the request
still answers from the other lanes. That is the right behaviour for a
flaky store and the wrong one for a programming error, so the pipeline
writes every absorbed failure into the audit, from where it reaches
``retrieval_metrics.lane_failures``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

import pytest

from oce.domain.services.query_classifier import QueryIntent
from oce.domain.services.retrieval import RetrievalPipeline, RetrievalState
from oce.domain.services.retrieval.rank import Ranker
from oce.domain.services.retrieval.state import lane_failed
from oce.domain.services.retrieval_strategy import get_strategy
from oce.domain.services.search import DefinitionHit, SearchHit, SearchScope
from oce.shared.config.settings import RetrievalSettings
from oce.shared.metrics import RetrievalAudit
from tests.fakes.retrieval import FakeEmbedder, FakeExactSearchStore, FakeSearchStore

BLOB = "a" * 64


def _hit(path: str, content: str) -> SearchHit:
    return SearchHit(
        blob_name=BLOB,
        path=path,
        content=content,
        score=0.9,
        content_hash="h" + path,
        start_line=1,
        end_line=1,
    )


class BrokenExactStore:
    async def search_exact(self, *, identifiers, scope, top_k=50, kinds=None):
        raise RuntimeError("symbol table unavailable")

    async def find_definitions(
        self, *, identifiers, scope, max_per_identifier=3, enclosing=None
    ):
        raise RuntimeError("symbol table unavailable")


class BrokenLexicalStore:
    async def search_lexical(self, *, terms, phrases, scope, top_k=30, required=()):
        raise TimeoutError()


class BrokenPathLookupStore:
    async def match_paths(self, *, filenames, paths, scope, limit=20):
        raise ValueError("bad pattern")


def test_lane_failed_records_the_exception_type_only():
    audit = RetrievalAudit()
    state = RetrievalState(query="q", scope=None, audit=audit)

    lane_failed(state, "exact", RuntimeError("secret sql text"))

    assert audit.lane_failures == {"exact": "RuntimeError"}


def test_lane_failed_without_an_audit_is_silent():
    state = RetrievalState(query="q", scope=None)

    lane_failed(state, "exact", RuntimeError("boom"))

    assert state.audit is None


async def test_failed_sql_lanes_are_skipped_and_named_in_the_audit():
    dense = _hit("src/settings.py", "def load_settings():\n    return {}")
    pipeline = RetrievalPipeline(
        embedder=FakeEmbedder(),
        store=FakeSearchStore([dense]),
        exact_store=BrokenExactStore(),
        lexical_store=BrokenLexicalStore(),
        path_lookup_store=BrokenPathLookupStore(),
        settings=RetrievalSettings(confidence_floor=0.0),
    )
    audit = RetrievalAudit()

    hits = await pipeline.search(
        "Where is `load_settings` in settings.py used?",
        SearchScope(frozenset({BLOB})),
        audit=audit,
    )

    assert [hit.path for hit in hits if hit.role == "primary"] == ["src/settings.py"]
    # The related-definitions lane reads the same broken store during expand.
    assert audit.lane_failures == {
        "exact": "RuntimeError",
        "lexical": "TimeoutError",
        "path_lookup": "ValueError",
        "related": "RuntimeError",
    }


async def test_a_healthy_request_records_no_lane_failures():
    dense = _hit("src/settings.py", "def load_settings():\n    return {}")
    pipeline = RetrievalPipeline(
        embedder=FakeEmbedder(),
        store=FakeSearchStore([dense]),
        settings=RetrievalSettings(confidence_floor=0.0),
    )
    audit = RetrievalAudit()

    await pipeline.search(
        "How are settings loaded?", SearchScope(frozenset({BLOB})), audit=audit
    )

    assert audit.lane_failures == {}


async def test_relation_lane_failure_is_named_by_its_role():
    class BrokenRelationStore:
        async def find_callers(self, *, identifiers, scope, limit=8):
            raise RuntimeError("callers unavailable")

        async def find_test_uses(self, *, identifiers, scope, limit=8):
            return []

        async def find_implementations(self, *, identifiers, scope, limit=8):
            return []

        async def find_reexports(self, *, identifiers, scope, limit=4):
            return []

        async def defined_identifiers(self, occurrences, scope):
            return {}

    pipeline = RetrievalPipeline(
        embedder=FakeEmbedder(),
        store=FakeSearchStore(),
        relation_store=BrokenRelationStore(),
        settings=RetrievalSettings(
            related_definitions_enabled=False, merge_adjacent_enabled=False
        ),
    )
    audit = RetrievalAudit()
    state = RetrievalState(
        query="Where is `build` used?",
        scope=SearchScope(frozenset({BLOB})),
        audit=audit,
        intent=QueryIntent.REFERENCE,
        strategy=get_strategy(QueryIntent.REFERENCE),
        lookup_identifiers=("build",),
        selected=[_hit("src/a.py", "def build():\n    pass")],
    )

    await pipeline.expander.expand(state)

    assert audit.lane_failures == {"caller": "RuntimeError"}


async def test_rank_programming_error_propagates(monkeypatch):
    dense = _hit("src/impl.rs", "impl IntoResponse for StatusCode {}")
    pipeline = RetrievalPipeline(
        embedder=FakeEmbedder(),
        store=FakeSearchStore([dense]),
        settings=RetrievalSettings(confidence_floor=0.0),
    )

    async def broken(self, state, others):
        raise RuntimeError("inherit rows unavailable")

    monkeypatch.setattr(Ranker, "implementor_keys", broken)
    audit = RetrievalAudit()

    with pytest.raises(RuntimeError):
        await pipeline.search(
            "Where is `IntoResponse` implemented for `StatusCode`?",
            SearchScope(frozenset({BLOB})),
            audit=audit,
        )


async def test_related_refresh_failure_keeps_preview_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = replace(
        _hit("src/helper.py", "def helper():\n    return '" + "h" * 740 + "'\n"),
        blob_name="b" * 64,
        end_line=2,
    )
    definition = DefinitionHit(
        identifier="helper", kind="definition", hit=helper, start_line=1, end_line=2
    )
    store = FakeExactSearchStore()
    calls = 0

    async def find_definitions(
        *,
        identifiers: Sequence[str],
        scope: SearchScope,
        max_per_identifier: int = 3,
        enclosing: Sequence[str] | None = None,
    ) -> list[DefinitionHit]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise TimeoutError("related refresh timed out")
        return [definition]

    monkeypatch.setattr(store, "find_definitions", find_definitions)
    settings = RetrievalSettings(max_context_chars=3_000)
    pipeline = RetrievalPipeline(
        embedder=FakeEmbedder(),
        store=FakeSearchStore(),
        exact_store=store,
        settings=settings,
    )
    audit = RetrievalAudit()
    head = _hit("src/a.py", "a" * 1_400)
    state = RetrievalState(
        query="How does helper work?",
        scope=SearchScope(frozenset({BLOB, helper.blob_name})),
        audit=audit,
        strategy=get_strategy(QueryIntent.FEATURE),
        lookup_identifiers=("helper",),
        selected=[head, _hit("src/b.py", "b" * 1_400)],
    )

    await pipeline.expander.expand(state)

    assert calls == 2
    assert state.selected[0] == head
    assert [hit.path for hit in state.related] == ["src/helper.py"]
    assert audit.lane_failures == {"related": "TimeoutError"}
    assert sum(len(hit.content) for hit in (*state.selected, *state.related)) <= 3_000

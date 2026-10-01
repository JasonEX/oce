"""Request-scoped raw evidence reuse and ordinary relation budget boundaries."""

from __future__ import annotations

from dataclasses import replace

import pytest

from oce.domain.services.query_classifier import QueryIntent
from oce.domain.services.query_evidence import extract_query_evidence
from oce.domain.services.relations import RelatedOccurrence
from oce.domain.services.reranker import NoopReranker
from oce.domain.services.retrieval.chain import CallChainTracer
from oce.domain.services.retrieval.expand import Expander
from oce.domain.services.retrieval.names import split_qualified_identifiers
from oce.domain.services.retrieval.priors import source_priority_factor
from oce.domain.services.retrieval.rank import Ranker
from oce.domain.services.retrieval.state import RetrievalState
from oce.domain.services.retrieval_strategy import get_strategy
from oce.domain.services.search import DefinitionHit, SearchHit, SearchScope
from oce.shared.config.settings import RetrievalSettings
from oce.shared.metrics import RetrievalAudit
from tests.fakes.retrieval import FakeEvidenceStore, FakeRelationStore


def _hit(name: str, content: str, *, score: float = 0.8) -> SearchHit:
    return SearchHit(
        blob_name=name,
        path=f"src/{name}.py",
        content=content,
        score=score,
        content_hash=f"chunk-{name}",
        end_line=len(content.splitlines()),
    )


def _state(
    query: str, intent: QueryIntent, hits: list[SearchHit], scope: SearchScope
) -> RetrievalState:
    evidence = extract_query_evidence(query)
    identifiers, qualifiers = split_qualified_identifiers(evidence.identifiers)
    return RetrievalState(
        query=query,
        scope=scope,
        audit=RetrievalAudit(),
        intent=intent,
        evidence=evidence,
        lookup_identifiers=identifiers,
        qualifiers=qualifiers,
        strategy=get_strategy(intent),
        exact=list(hits),
        use_sites=list(hits),
        candidates=list(hits),
        selected=list(hits),
    )


@pytest.mark.parametrize("rerank", [False, True])
async def test_restored_reference_head_reuses_implementation_facts(
    rerank: bool,
) -> None:
    implementation = _hit("status", "impl IntoResponse for StatusCode {}", score=0.4)
    other = _hit("other", "impl IntoResponse for OtherType {}", score=0.9)
    store = FakeRelationStore(
        [
            RelatedOccurrence(
                "IntoResponse", "inherit", implementation, 1, 1, "StatusCode"
            )
        ]
    )
    ranker = Ranker(
        settings=RetrievalSettings(_env_file=None),
        priority_factor=source_priority_factor,
        exact_store=None,
        relation_store=store,
        reranker=NoopReranker() if rerank else None,
        llm_reranker=None,
    )
    scope = SearchScope(frozenset({implementation.blob_name, other.blob_name}))
    state = _state(
        "Where is `IntoResponse` implemented for `StatusCode`?",
        QueryIntent.REFERENCE,
        [other, implementation],
        scope,
    )

    await ranker.rank(state)

    assert state.candidates[0] == implementation
    assert store.implementation_requests == [(("IntoResponse",), 200)]
    # A new request resolves its own scope; no facts survive in the ranker.
    next_state = _state(
        state.query, state.intent, [other], SearchScope(frozenset({"other"}))
    )
    await ranker.rank(next_state)
    assert len(store.implementation_requests) == 2
    assert next_state.implementor_keys == frozenset()


async def test_failed_implementation_lookup_is_audited_once_per_request() -> None:
    hit = _hit("status", "impl IntoResponse for StatusCode {}")
    store = FakeRelationStore(error=RuntimeError("unavailable"))
    state = _state(
        "Where is `IntoResponse` implemented for `StatusCode`?",
        QueryIntent.REFERENCE,
        [hit],
        SearchScope(frozenset({hit.blob_name})),
    )
    ranker = Ranker(
        settings=RetrievalSettings(_env_file=None),
        priority_factor=source_priority_factor,
        exact_store=None,
        relation_store=store,
        reranker=None,
        llm_reranker=None,
    )

    await ranker.rank(state)

    assert store.implementation_requests == [(("IntoResponse",), 200)]
    assert state.candidates == [hit]
    assert state.audit is not None
    assert state.audit.lane_failures == {"implementors": "RuntimeError"}


@pytest.mark.parametrize("call_error", [None, RuntimeError("unavailable")])
async def test_expansion_reuses_facts_and_drops_relations_of_trimmed_sources(
    call_error: Exception | None,
) -> None:
    primary = _hit("primary", "RetainedHelper\n" + "x" * 4985)
    tail = _hit("tail", "DiscardedHelper\n" + "y" * 6484)
    retained = _hit(
        "retained", "\n".join("RetainedHelper " + "r" * 90 for _ in range(12))
    )
    discarded = _hit(
        "discarded", "\n".join("DiscardedHelper " + "d" * 90 for _ in range(12))
    )
    store = FakeEvidenceStore(
        [
            DefinitionHit("RetainedHelper", "definition", retained, 1, 12),
            DefinitionHit("DiscardedHelper", "definition", discarded, 1, 12),
        ],
        {
            (primary.blob_name, 1, primary.end_line): [("RetainedHelper", 1, "entry")],
            (tail.blob_name, 1, tail.end_line): [("DiscardedHelper", 1, "worker")],
        },
        call_error=call_error,
    )
    settings = RetrievalSettings(_env_file=None, merge_adjacent_enabled=False)
    expander = Expander(
        settings=settings,
        exact_store=store,
        relation_store=None,
        chain=CallChainTracer(store, settings),
    )
    scope = SearchScope(
        frozenset(hit.blob_name for hit in (primary, tail, retained, discarded))
    )
    state = _state(
        "Where is `EntryPoint` defined?", QueryIntent.SYMBOL, [primary, tail], scope
    )

    await expander.expand(state)

    assert state.selected == [primary]
    assert [hit.blob_name for hit in state.related] == [retained.blob_name]
    assert len(store.definition_requests) == 1
    assert store.call_requests == [
        (primary.blob_name, 1, primary.end_line),
        (tail.blob_name, 1, tail.end_line),
    ]
    if call_error is not None:
        assert state.audit is not None
        assert state.audit.lane_failures == {"related": "RuntimeError"}
    # Raw facts are not a cross-request cache, even on the same expander.
    next_state = _state(state.query, state.intent, [primary, tail], scope)
    await expander.expand(next_state)
    assert len(store.definition_requests) == 2
    assert len(store.call_requests) == 4


@pytest.mark.parametrize("relation_cap", [0, 20, 100, 6_000])
async def test_ordinary_relations_respect_zero_low_and_default_caps(
    relation_cap: int,
) -> None:
    primary = _hit("primary", "TargetHelper()")
    related = _hit("helper", "def TargetHelper():\n    return True")
    store = FakeEvidenceStore(
        [DefinitionHit("TargetHelper", "definition", related, 1, 2)]
    )
    settings = RetrievalSettings(_env_file=None, relation_reserve_chars=relation_cap)
    expander = Expander(
        settings=settings,
        exact_store=store,
        relation_store=None,
        chain=CallChainTracer(store, settings),
    )
    state = _state(
        "Where is `EntryPoint` defined?",
        QueryIntent.SYMBOL,
        [primary],
        SearchScope(frozenset({primary.blob_name, related.blob_name})),
    )

    await expander.expand(state)

    assert state.selected == [primary]
    assert sum(len(hit.content) for hit in state.related) <= relation_cap
    if relation_cap < len(related.content):
        assert state.related == []
    else:
        assert [hit.content for hit in state.related] == [related.content]
    if relation_cap == 0:
        assert store.definition_requests == []
        assert store.call_requests == []


async def test_zero_ordinary_relation_cap_preserves_the_separate_chain_budget() -> None:
    primary = _hit("entry", "def entry():\n    return target()")
    target = _hit("target", "def target():\n    return True")
    start = DefinitionHit("entry", "definition", primary, 1, 2)
    destination = DefinitionHit("target", "definition", target, 1, 2)
    store = FakeEvidenceStore(
        [destination], {(primary.blob_name, 1, 2): [("target", 2, "entry")]}
    )
    settings = RetrievalSettings(
        _env_file=None,
        relation_reserve_chars=0,
        call_chain_max_chars=len(target.content),
    )
    expander = Expander(
        settings=settings,
        exact_store=store,
        relation_store=FakeRelationStore(),
        chain=CallChainTracer(store, settings),
    )
    state = _state(
        "Trace `entry` through its callees.",
        QueryIntent.CALL_CHAIN,
        [primary],
        SearchScope(frozenset({primary.blob_name, target.blob_name})),
    )
    state.endpoints = [("entry", [start])]

    await expander.expand(state)

    assert state.selected == [primary]
    assert state.related == [replace(target, score=0.0, role="chain", hop=1)]
    assert (
        sum(len(hit.content) for hit in state.related) <= settings.call_chain_max_chars
    )
    assert state.audit is not None
    assert state.audit.relation_counts == {"chain": 1}
    assert state.audit.relation_chars == len(target.content)

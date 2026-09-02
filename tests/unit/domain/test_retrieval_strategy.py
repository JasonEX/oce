from oce.domain.services.query_classifier import QueryIntent
from oce.domain.services.retrieval_strategy import should_use_llm_rerank


def test_semantic_intent_uses_adaptive_llm_rerank():
    assert should_use_llm_rerank(QueryIntent.CALL_CHAIN, 2)
    assert should_use_llm_rerank(QueryIntent.FEATURE, 2)
    assert should_use_llm_rerank(QueryIntent.OVERVIEW, 2)
    assert should_use_llm_rerank(QueryIntent.COMPOUND, 2)


def test_exact_symbol_evidence_skips_adaptive_llm_rerank():
    assert not should_use_llm_rerank(
        QueryIntent.SYMBOL,
        2,
        has_exact_hits=True,
    )


def test_symbol_without_exact_evidence_uses_adaptive_llm_rerank():
    assert should_use_llm_rerank(
        QueryIntent.SYMBOL,
        2,
        has_exact_hits=False,
    )


def test_path_evidence_skips_adaptive_llm_rerank():
    assert not should_use_llm_rerank(
        QueryIntent.PATH,
        2,
        has_path_hits=True,
    )


def test_reference_queries_preserve_occurrence_coverage():
    assert not should_use_llm_rerank(QueryIntent.REFERENCE, 10)


def test_always_policy_includes_reference_queries():
    assert should_use_llm_rerank(
        QueryIntent.REFERENCE,
        2,
        policy="always",
        has_exact_hits=True,
    )


def test_single_candidate_does_not_escalate():
    assert not should_use_llm_rerank(
        QueryIntent.OVERVIEW,
        1,
        policy="always",
    )

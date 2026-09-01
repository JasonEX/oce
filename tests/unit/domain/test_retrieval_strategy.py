from oce.domain.services.query_classifier import QueryIntent
from oce.domain.services.retrieval_strategy import should_use_llm_rerank


def test_complex_intent_escalates_even_with_clear_scores():
    assert should_use_llm_rerank(QueryIntent.CALL_CHAIN, [0.95, 0.40])


def test_ambiguous_feature_escalates():
    assert should_use_llm_rerank(QueryIntent.FEATURE, [0.80, 0.75])


def test_confident_exact_symbol_does_not_escalate():
    assert not should_use_llm_rerank(
        QueryIntent.SYMBOL,
        [1.0, 0.99],
        exact_confidence=0.95,
    )


def test_confident_path_does_not_escalate():
    assert not should_use_llm_rerank(
        QueryIntent.PATH,
        [0.90, 0.89],
        path_confidence=0.90,
    )


def test_single_candidate_does_not_escalate():
    assert not should_use_llm_rerank(QueryIntent.OVERVIEW, [0.80])

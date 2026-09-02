"""plan_rerank：授权与路由分离，两种 reranker 共用确定性证据。"""

import pytest

from oce.domain.services.query_classifier import QueryIntent
from oce.domain.services.retrieval_strategy import RerankDecision, plan_rerank


@pytest.mark.parametrize(
    "intent,evidence,expected",
    [
        (
            QueryIntent.SYMBOL,
            {"has_exact_hits": True},
            (False, False, "exact_definition"),
        ),
        (QueryIntent.SYMBOL, {}, (True, True, "no_structural_evidence")),
        (QueryIntent.PATH, {"has_path_hits": True}, (False, False, "path_evidence")),
        (QueryIntent.PATH, {}, (True, True, "no_structural_evidence")),
        (QueryIntent.REFERENCE, {}, (True, False, "reference_keep_coverage")),
        (QueryIntent.CALL_CHAIN, {}, (True, True, "semantic")),
        (QueryIntent.FEATURE, {}, (True, True, "semantic")),
        (QueryIntent.OVERVIEW, {}, (True, True, "semantic")),
        (QueryIntent.COMPOUND, {"has_exact_hits": True}, (True, True, "semantic")),
    ],
)
def test_adaptive_decision_table(intent, evidence, expected):
    decision = plan_rerank(intent, 10, **evidence)

    assert (decision.dedicated, decision.llm, decision.reason) == expected


def test_single_candidate_skips_every_reranker():
    decision = plan_rerank(
        QueryIntent.OVERVIEW, 1, dedicated_policy="always", llm_policy="always"
    )

    assert decision == RerankDecision(False, False, "too_few_candidates")
    assert decision.route == "skip:too_few_candidates"


def test_always_policy_overrides_structural_skips_per_model():
    decision = plan_rerank(
        QueryIntent.SYMBOL,
        5,
        has_exact_hits=True,
        dedicated_policy="always",
    )

    assert (decision.dedicated, decision.llm) == (True, False)
    assert decision.route == "dedicated"


def test_policy_cannot_enable_an_unauthorized_model():
    decision = plan_rerank(
        QueryIntent.FEATURE,
        5,
        dedicated_enabled=False,
        llm_enabled=False,
        dedicated_policy="always",
        llm_policy="always",
    )

    assert decision == RerankDecision(False, False, "no_reranker_enabled")


def test_route_label_lists_applied_rerankers():
    assert plan_rerank(QueryIntent.FEATURE, 5).route == "dedicated+llm"
    assert plan_rerank(QueryIntent.REFERENCE, 5).route == "dedicated"
    assert plan_rerank(QueryIntent.FEATURE, 5, llm_enabled=False).route == "dedicated"


@pytest.mark.parametrize("field", ["dedicated_policy", "llm_policy"])
def test_unsupported_policy_is_rejected(field):
    with pytest.raises(ValueError, match="Unsupported .* rerank policy"):
        plan_rerank(QueryIntent.FEATURE, 5, **{field: "sometimes"})

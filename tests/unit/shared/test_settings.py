"""Configuration defaults for optional retrieval model stages."""

from oce.shared.config.settings import LLMSettings, RerankSettings, RetrievalSettings


def test_rerank_defaults_require_explicit_external_model_opt_in():
    llm = LLMSettings(_env_file=None)
    rerank = RerankSettings(_env_file=None)
    retrieval = RetrievalSettings(_env_file=None)

    assert llm.rerank_enabled is False
    assert rerank.enabled is False
    assert retrieval.llm_rerank_policy == "adaptive"
    assert llm.rerank_timeout_seconds == 15.0
    assert rerank.top_n == 50

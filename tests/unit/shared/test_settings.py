"""Configuration defaults for optional retrieval model stages."""

from oce.shared.config.settings import LLMSettings, RerankSettings, RetrievalSettings


def test_rerank_defaults_require_explicit_external_model_opt_in():
    llm = LLMSettings(_env_file=None)
    rerank = RerankSettings(_env_file=None)
    retrieval = RetrievalSettings(_env_file=None)

    assert llm.rerank_enabled is False
    assert rerank.enabled is False
    assert rerank.instruction
    assert retrieval.rerank_policy == "adaptive"
    assert retrieval.llm_rerank_policy == "adaptive"
    assert retrieval.path_top_k == 20
    assert llm.rerank_timeout_seconds == 15.0
    assert rerank.top_n == 50


def test_rerank_instruction_and_policy_read_their_documented_environment_names(
    monkeypatch,
):
    monkeypatch.setenv("RERANK_INSTRUCTION", "Judge repository code relevance")
    monkeypatch.setenv("RETRIEVAL_RERANK_POLICY", "always")
    monkeypatch.setenv("RETRIEVAL_PATH_TOP_K", "32")

    assert (
        RerankSettings(_env_file=None).instruction == "Judge repository code relevance"
    )
    assert RetrievalSettings(_env_file=None).rerank_policy == "always"
    assert RetrievalSettings(_env_file=None).path_top_k == 32

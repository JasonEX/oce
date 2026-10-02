"""Chat-LLM reranking preserves candidates and has bounded failure semantics."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from oce.domain.services.llm.reranker import LLMReranker
from oce.domain.services.reranker import Reranker
from oce.domain.services.retrieval import RetrievalPipeline
from oce.domain.services.search import SearchHit
from oce.shared.config.settings import RetrievalSettings
from oce.shared.metrics import RetrievalAudit
from tests.fakes.llm import FakeLLM
from tests.fakes.retrieval import FakeEmbedder, FakeSearchStore


def _hit(name: str) -> SearchHit:
    return SearchHit(
        blob_name=name.ljust(64, "x"),
        path=f"src/{name}.py",
        content=f"def {name}(): pass",
        score=0.5,
    )


async def _retrieve(
    reranker: LLMReranker, hits: list[SearchHit]
) -> tuple[list[SearchHit], RetrievalAudit]:
    pipeline = RetrievalPipeline(
        embedder=FakeEmbedder(),
        store=FakeSearchStore(hits),
        llm_reranker=reranker,
        settings=RetrievalSettings(_env_file=None, llm_rerank_policy="always"),
    )
    audit = RetrievalAudit()
    result = await pipeline.search("find the implementation", audit=audit)
    return result, audit


async def test_promotes_model_choices_without_pruning_candidates():
    client = FakeLLM("3\n1")
    reranker = LLMReranker(
        client,
        max_candidates=3,
        output_top_k=2,
    )
    hits = [_hit(name) for name in ("a", "b", "c", "d")]

    result = await reranker.rerank("find the implementation", hits)

    assert [hit.path for hit in result] == [
        "src/c.py",
        "src/a.py",
        "src/b.py",
        "src/d.py",
    ]
    assert len(result) == len(hits)
    assert client.kwargs["max_tokens"] == 128


async def test_promotion_count_never_exceeds_model_window():
    client = FakeLLM("2\n1")
    reranker = LLMReranker(
        client,
        max_candidates=2,
        output_top_k=10,
    )
    hits = [_hit(name) for name in ("a", "b", "c")]

    result = await reranker.rerank("query", hits)

    assert [hit.path for hit in result] == ["src/b.py", "src/a.py", "src/c.py"]
    assert "choose at most 2" in client.messages[1]["content"]
    assert client.kwargs["max_tokens"] == 128


async def test_invalid_output_preserves_the_full_candidate_set_and_is_audited():
    client = FakeLLM("not an index")
    reranker = LLMReranker(client, output_top_k=2)
    hits = [_hit(name) for name in ("a", "b", "c")]

    result, audit = await _retrieve(reranker, hits)

    assert result == hits
    assert audit.lane_failures == {"llm_rerank": "ValueError"}


async def test_model_failure_preserves_the_full_candidate_set():
    client = FakeLLM(error=RuntimeError("provider unavailable"))
    reranker = LLMReranker(client, output_top_k=1)
    hits = [_hit(name) for name in ("a", "b", "c")]

    result, audit = await _retrieve(reranker, hits)

    assert result == hits
    assert audit.lane_failures == {"llm_rerank": "RuntimeError"}


async def test_deadline_preserves_original_order():
    client = FakeLLM("2\n1", delay=0.05)
    reranker = LLMReranker(client, timeout_seconds=0.001)
    hits = [_hit(name) for name in ("a", "b")]

    result, audit = await _retrieve(reranker, hits)

    assert result == hits
    assert audit.lane_failures == {"llm_rerank": "TimeoutError"}


async def test_caller_cancellation_propagates_without_degradation():
    reranker = LLMReranker(FakeLLM(error=asyncio.CancelledError()))
    hits = [_hit("a"), _hit("b")]

    with pytest.raises(asyncio.CancelledError):
        await _retrieve(reranker, hits)


@pytest.mark.parametrize("dedicated_fails", [False, True])
async def test_cascade_preserves_each_stage_input_and_continues_after_failure(
    dedicated_fails: bool,
) -> None:
    hits = [_hit(name) for name in ("a", "b", "c")]
    dedicated = Mock(spec=Reranker)
    dedicated.rerank = AsyncMock(
        side_effect=RuntimeError("provider failed") if dedicated_fails else None,
        return_value=list(reversed(hits)),
    )
    llm = LLMReranker(
        FakeLLM("2\n1") if dedicated_fails else FakeLLM(error=TimeoutError())
    )
    pipeline = RetrievalPipeline(
        embedder=FakeEmbedder(),
        store=FakeSearchStore(hits),
        reranker=dedicated,
        llm_reranker=llm,
        settings=RetrievalSettings(
            _env_file=None, rerank_policy="always", llm_rerank_policy="always"
        ),
    )
    audit = RetrievalAudit()

    result = await pipeline.search("find the implementation", audit=audit)

    assert result == (
        [hits[1], hits[0], hits[2]] if dedicated_fails else list(reversed(hits))
    )
    assert audit.rerank_route == "dedicated+llm"
    assert audit.lane_failures == (
        {"rerank": "RuntimeError"}
        if dedicated_fails
        else {"llm_rerank": "TimeoutError"}
    )

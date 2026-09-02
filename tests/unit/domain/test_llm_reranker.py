"""Chat-LLM reranking preserves candidates and has bounded failure semantics."""

from __future__ import annotations

import asyncio

from oce.domain.services.llm.reranker import LLMReranker
from oce.domain.services.search import SearchHit


def _hit(name: str) -> SearchHit:
    return SearchHit(
        blob_name=name.ljust(64, "x"),
        path=f"src/{name}.py",
        content=f"def {name}(): pass",
        score=0.5,
    )


class FakeLLM:
    def __init__(
        self,
        response: str = "",
        *,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.response = response
        self.error = error
        self.delay = delay
        self.messages: list[dict[str, str]] = []
        self.kwargs: dict = {}

    async def chat(self, messages, **kwargs) -> str:
        self.messages = messages
        self.kwargs = kwargs
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.response


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


async def test_invalid_or_partial_output_preserves_the_full_candidate_set():
    client = FakeLLM("not an index")
    reranker = LLMReranker(client, output_top_k=2)
    hits = [_hit(name) for name in ("a", "b", "c")]

    result = await reranker.rerank("query", hits)

    assert result == hits


async def test_model_failure_preserves_the_full_candidate_set():
    client = FakeLLM(error=RuntimeError("provider unavailable"))
    reranker = LLMReranker(client, output_top_k=1)
    hits = [_hit(name) for name in ("a", "b", "c")]

    result = await reranker.rerank("query", hits)

    assert result == hits


async def test_deadline_preserves_original_order():
    client = FakeLLM("2\n1", delay=0.05)
    reranker = LLMReranker(client, timeout_seconds=0.001)
    hits = [_hit(name) for name in ("a", "b")]

    result = await reranker.rerank("query", hits)

    assert result == hits

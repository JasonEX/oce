"""OpenAI-compatible embedding input budgeting tests."""

from __future__ import annotations

import asyncio
import math

import pytest

from oce.infrastructure.embed.openai_embedder import OpenAIEmbedder
from tests.fakes.embedding import FakeEmbeddingClient


def _make_embedder(**kwargs) -> tuple[OpenAIEmbedder, FakeEmbeddingClient]:
    client = FakeEmbeddingClient()
    embedder = OpenAIEmbedder(
        client,
        "test-model",
        2,
        max_batch_size=kwargs.get("max_batch_size", 32),
        max_concurrency=kwargs.get("max_concurrency", 1),
        max_batch_chars=kwargs.get("max_batch_chars", 32_000),
        max_input_chars=kwargs.get("max_input_chars", 8_000),
        input_overlap_chars=kwargs.get("input_overlap_chars", 0),
    )
    return embedder, client


@pytest.mark.asyncio
async def test_batching_respects_item_and_total_character_limits():
    embedder, client = _make_embedder(
        max_batch_size=3,
        max_batch_chars=6,
        max_input_chars=6,
    )

    await embedder.embed_documents(["aaaa", "bbbb", "cc"])

    assert client.embeddings.calls == [["aaaa"], ["bbbb", "cc"]]


@pytest.mark.asyncio
async def test_long_input_is_split_and_pooled_to_one_normalized_vector():
    embedder, client = _make_embedder(
        max_batch_chars=6,
        max_input_chars=5,
    )

    vectors = await embedder.embed_documents(["abcdefghij"])

    assert client.embeddings.calls == [["abcde"], ["fghij"]]
    assert len(vectors) == 1
    assert vectors[0] == pytest.approx([1 / math.sqrt(2), 1 / math.sqrt(2)])


@pytest.mark.asyncio
async def test_short_input_preserves_provider_vector():
    embedder, _ = _make_embedder()

    assert await embedder.embed_query("abc") == [1.0, 0.0]


def test_invalid_input_budget_is_rejected():
    client = FakeEmbeddingClient()

    with pytest.raises(ValueError, match="character budgets"):
        OpenAIEmbedder(
            client,
            "test-model",
            2,
            max_batch_chars=4,
            max_input_chars=5,
        )


@pytest.mark.asyncio
async def test_embed_reports_usage_with_model_and_credential_id():
    """A successful embed reports on_usage(credential_id, 'embed', model, total_tokens, 0)."""
    captured: list[tuple] = []

    async def _on_usage(cid, kind, model, prompt, completion):
        captured.append((cid, kind, model, prompt, completion))

    client = FakeEmbeddingClient()
    embedder = OpenAIEmbedder(
        client,
        "test-model",
        2,
        max_concurrency=1,
        credential_id=9,
        on_usage=_on_usage,
    )

    await embedder.embed_query("abc")

    # The fake client reports total_tokens = len("abc") = 3; embed has no completion.
    assert captured == [(9, "embed", "test-model", 3, 0)]


async def test_query_embedding_input_is_capped_before_the_instruction():
    client = FakeEmbeddingClient()
    embedder = OpenAIEmbedder(
        client,
        "test-model",
        2,
        max_batch_size=32,
        max_concurrency=1,
        max_batch_chars=32_000,
        max_input_chars=8_000,
        input_overlap_chars=0,
        query_instruction="Instruct: ",
        max_query_chars=20,
    )
    await embedder.embed_query("a" * 100)
    sent = client.embeddings.calls[0][0]
    assert sent == "Instruct: " + "a" * 20

    unlimited = OpenAIEmbedder(
        client,
        "test-model",
        2,
        max_batch_size=32,
        max_concurrency=1,
        max_batch_chars=32_000,
        max_input_chars=8_000,
        input_overlap_chars=0,
    )
    await unlimited.embed_query("b" * 100)
    assert client.embeddings.calls[1][0] == "b" * 100


async def test_provider_concurrency_limit_is_shared_by_document_and_query_calls() -> (
    None
):
    client = FakeEmbeddingClient(blocked=True)
    embedder = OpenAIEmbedder(
        client, "test-model", 2, max_batch_size=1, max_concurrency=2
    )
    documents = [
        asyncio.create_task(embedder.embed_documents(["aaa", "bbb"])) for _ in range(3)
    ]
    queries = [asyncio.create_task(embedder.embed_query("ggg")) for _ in range(5)]
    async with asyncio.timeout(1):
        await client.embeddings.started.get()
        await client.embeddings.started.get()
    await asyncio.sleep(0)
    assert client.embeddings.active == 2

    client.embeddings.release.set()
    assert await asyncio.gather(*documents) == [[[1.0, 0.0], [1.0, 0.0]]] * 3
    assert await asyncio.gather(*queries) == [[0.0, 1.0]] * 5
    assert len(client.embeddings.calls) == 11
    assert client.embeddings.peak_active == 2

"""Candidate-preserving reranking.

A reranker compares recalled chunks against each other and reorders them;
``NoopReranker`` keeps the recall order.
"""

from __future__ import annotations

from typing import Protocol

from oce.domain.services.search import SearchHit


class Reranker(Protocol):
    """Reorder candidates without adding or dropping any.

    An implementation may change the order or the scores but must return
    exactly the input candidates; deduplication, coverage and the context
    budget belong to the selector. Runtime resources (HTTP clients, ONNX
    sessions) are owned and closed by the composition root, not by this
    protocol.
    """

    async def rerank(self, query: str, hits: list[SearchHit]) -> list[SearchHit]:
        """The same candidates in a new order."""
        ...


class NoopReranker:
    """Keeps the recall order."""

    async def rerank(self, query: str, hits: list[SearchHit]) -> list[SearchHit]:
        return hits

    async def close(self) -> None:
        return None

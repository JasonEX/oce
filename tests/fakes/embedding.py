"""Deterministic embeddings for tests that need vectors with some meaning.

A hashed bag of identifier tokens: two texts that share names get a higher
cosine than two that do not, which is enough for dense recall to behave
sensibly without any model, and every call is reproducible.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from types import SimpleNamespace
from typing import Any

import httpx

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}")


def term_vector(text: str, dimensions: int) -> list[float]:
    vector = [0.0] * dimensions
    for match in _TOKEN.finditer(text):
        digest = hashlib.blake2b(match.group().lower().encode(), digest_size=4)
        vector[int.from_bytes(digest.digest(), "big") % dimensions] += 1.0
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


class TermEmbedder:
    """An ``Embedder`` over ``term_vector``; documents and queries share the space."""

    def __init__(self, dimensions: int = 32) -> None:
        self.dimensions = dimensions
        self.document_calls: list[list[str]] = []
        self.query_calls: list[str] = []

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls.append(list(texts))
        return [term_vector(text, self.dimensions) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        self.query_calls.append(text)
        return term_vector(text, self.dimensions)


class LengthEmbedder:
    """Length vectors with separate query/document and close-call recording."""

    def __init__(self) -> None:
        self.query_calls: list[str] = []
        self.document_calls: list[list[str]] = []
        self.closed = False

    async def embed_query(self, text: str) -> list[float]:
        self.query_calls.append(text)
        return [float(len(text))]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls.append(texts)
        return [[float(len(text))] for text in texts]

    async def close(self) -> None:
        self.closed = True


class ControlledEmbedder(LengthEmbedder):
    """Expose each pending query so tests choose its completion order and outcome."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: asyncio.Queue[asyncio.Future[list[float]]] = asyncio.Queue()
        self.cancelled_calls = 0

    async def embed_query(self, text: str) -> list[float]:
        self.query_calls.append(text)
        response: asyncio.Future[list[float]] = (
            asyncio.get_running_loop().create_future()
        )
        self.requests.put_nowait(response)
        try:
            return await response
        except asyncio.CancelledError:
            self.cancelled_calls += 1
            raise


class FakeEmbeddingEndpoint:
    """An OpenAI-shaped endpoint with a gate and provider-concurrency recording."""

    def __init__(self, *, blocked: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.active = 0
        self.peak_active = 0
        self.cancelled_calls = 0
        self.started: asyncio.Queue[None] = asyncio.Queue()
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        texts = list(kwargs["input"])
        self.calls.append(texts)
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        self.started.put_nowait(None)
        try:
            await self.release.wait()
            return SimpleNamespace(
                data=[
                    SimpleNamespace(
                        index=index,
                        embedding=[1.0, 0.0] if text[0] < "f" else [0.0, 1.0],
                    )
                    for index, text in enumerate(texts)
                ],
                usage=SimpleNamespace(total_tokens=sum(map(len, texts))),
            )
        except asyncio.CancelledError:
            self.cancelled_calls += 1
            raise
        finally:
            self.active -= 1


class FakeEmbeddingClient:
    """The client owned by an OpenAIEmbedder, including its endpoint and close."""

    def __init__(
        self,
        *,
        blocked: bool = False,
        endpoint: FakeEmbeddingEndpoint | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.embeddings = endpoint or FakeEmbeddingEndpoint(blocked=blocked)
        self._http_client = http_client
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        if self._http_client is not None:
            await self._http_client.aclose()

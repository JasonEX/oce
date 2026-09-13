"""Deterministic embeddings for tests that need vectors with some meaning.

A hashed bag of identifier tokens: two texts that share names get a higher
cosine than two that do not, which is enough for dense recall to behave
sensibly without any model, and every call is reproducible.
"""

from __future__ import annotations

import hashlib
import math
import re

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

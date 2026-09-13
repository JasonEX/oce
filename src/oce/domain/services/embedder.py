"""Text embedding protocol.

Any OpenAI-compatible service, local model or other API implements it;
``embed_documents`` and ``embed_query`` must share one model and dimension.
"""

from __future__ import annotations

from typing import Protocol


class Embedder(Protocol):
    """Asynchronous embedder."""

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """One vector per input text, in order."""
        ...

    async def embed_query(self, text: str) -> list[float]:
        """The query vector, in the document space."""
        ...

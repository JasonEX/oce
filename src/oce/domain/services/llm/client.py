"""The chat protocol shared by the LLM reranker and the query rewriter."""

from __future__ import annotations

from typing import Any, Protocol


class LLMClient(Protocol):
    """A chat completion client.

    Extra parameters (model, temperature, max_tokens) pass through ``kwargs``
    so reranking and rewriting can each tune their own calls.
    """

    async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        """The model's reply to ``messages``."""
        ...

"""Asynchronous rerank client in the SiliconFlow/Cohere style."""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

import httpx
from loguru import logger

from oce.domain.services.search import SearchHit
from oce.shared.metrics import UsageCallback, coerce_token_count


class OpenAIReranker:
    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        model: str,
        top_n: int = 10,
        min_score: float = 0.05,
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
        instruct: str | None = None,
        credential_id: int = 0,
        on_usage: UsageCallback | None = None,
        max_query_chars: int = 2_400,
    ) -> None:
        if max_query_chars < 1:
            raise ValueError("max_query_chars must be positive")
        self._endpoint = endpoint
        self._api_key = api_key
        self._model = model
        self._top_n = top_n
        self._min_score = min_score
        self._instruct = instruct
        self._max_query_chars = max_query_chars
        self._credential_id = credential_id
        self._on_usage = on_usage
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout))

    async def rerank(self, query: str, hits: list[SearchHit]) -> list[SearchHit]:
        if not hits:
            return []
        body: dict[str, Any] = {
            "model": self._model,
            "query": query[: self._max_query_chars],
            "documents": [self._document_text(hit) for hit in hits],
            "top_n": min(self._top_n, len(hits)),
            "return_documents": False,
        }
        if self._instruct:
            body["instruction"] = self._instruct
        try:
            response = await self._client.post(
                self._endpoint,
                json=body,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Rerank request failed; using retrieval order: {}", exc)
            return hits

        raw_results = payload.get("results", [])
        if not isinstance(raw_results, list):
            logger.warning("Rerank response has no result list; using retrieval order")
            return hits

        ranked: list[tuple[int, float]] = []
        seen: set[int] = set()
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            index = item.get("index")
            raw_score = item.get("relevance_score", item.get("score", 0.0))
            if raw_score is None:
                continue
            try:
                score = float(raw_score)
            except (TypeError, ValueError):
                continue
            if (
                isinstance(index, int)
                and 0 <= index < len(hits)
                and index not in seen
                and math.isfinite(score)
                and score >= self._min_score
            ):
                seen.add(index)
                ranked.append((index, score))
        ranked.sort(key=lambda pair: pair[1], reverse=True)

        promoted = ranked[: self._top_n]
        output = [replace(hits[index], score=score) for index, score in promoted]

        if self._on_usage is not None:
            meta = payload.get("meta") or {}
            token_meta = meta.get("tokens") or {}
            tokens = sum(
                coerce_token_count(token_meta.get(key, 0))
                for key in ("input_tokens", "output_tokens", "image_tokens")
            )
            # Reranking has no prompt/completion split: the total is prompt.
            try:
                await self._on_usage(
                    self._credential_id,
                    "rerank",
                    self._model,
                    tokens,
                    0,
                )
            except Exception as exc:
                logger.warning(
                    "Rerank usage reporting failed: {}",
                    type(exc).__name__,
                )
        # The reranker promotes a scored head; filtering and selection own pruning.
        # Preserve provider-omitted, below-threshold, and out-of-window hits in
        # their original relative order so coverage selection can still fill its
        # path and character budget.
        promoted_indices = {index for index, _score in promoted}
        output.extend(
            hit for index, hit in enumerate(hits) if index not in promoted_indices
        )
        return output

    @staticmethod
    def _document_text(hit: SearchHit) -> str:
        if not hit.path:
            return hit.content
        header = f"File: {hit.path}\nLines: {hit.start_line}-{hit.end_line}"
        if hit.context:
            header += f"\nContext: {hit.context}"
        return f"{header}\n\n{hit.content}"

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

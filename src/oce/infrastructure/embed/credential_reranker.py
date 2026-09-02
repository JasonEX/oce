"""Lazy rerank runtime configured by the active database credential."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from oce.domain.services.reranker import NoopReranker, Reranker
from oce.domain.services.search import SearchHit
from oce.infrastructure.delegate_runtime import SwappableDelegate
from oce.infrastructure.embed.openai_reranker import OpenAIReranker, UsageCallback
from oce.infrastructure.persistence.active_credential import resolve_active_credential
from oce.shared.config.settings import RerankSettings


@dataclass(frozen=True)
class RerankRuntimeConfig:
    endpoint: str
    api_key: str
    model: str
    top_n: int
    min_score: float
    timeout_seconds: float
    credential_id: int = 0


class CredentialConfiguredReranker(SwappableDelegate[Reranker]):
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        fallback: RerankSettings,
        *,
        fallback_embedding_key: str | None,
        on_usage: UsageCallback | None = None,
    ) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._fallback = fallback
        self._fallback_embedding_key = fallback_embedding_key
        self._on_usage = on_usage

    async def rerank(self, query: str, hits: list[SearchHit]) -> list[SearchHit]:
        delegate = await self._acquire()
        try:
            return await delegate.rerank(query, hits)
        finally:
            await self._release(delegate)

    async def _create_delegate(self) -> Reranker:
        return self._build_delegate(await self._resolve_config())

    async def _resolve_config(self) -> RerankRuntimeConfig | None:
        if not self._fallback.enabled:
            return None
        credential = await resolve_active_credential(
            self._session_factory,
            "rerank",
            require_endpoint_and_model=True,
        )
        if credential is not None:
            return RerankRuntimeConfig(
                endpoint=credential.endpoint,
                api_key=credential.api_key,
                model=credential.model,
                top_n=credential.top_n or self._fallback.top_n,
                min_score=(
                    credential.min_score
                    if credential.min_score is not None
                    else self._fallback.min_score
                ),
                timeout_seconds=float(credential.timeout_seconds),
                credential_id=credential.id,
            )

        key = (
            self._fallback.api_key.get_secret_value()
            if self._fallback.api_key is not None
            else self._fallback_embedding_key
        )
        if not key:
            return None
        return RerankRuntimeConfig(
            endpoint=self._fallback.endpoint,
            api_key=key,
            model=self._fallback.model,
            top_n=self._fallback.top_n,
            min_score=self._fallback.min_score,
            timeout_seconds=self._fallback.timeout_seconds,
        )

    def _build_delegate(self, config: RerankRuntimeConfig | None) -> Reranker:
        if config is None:
            return NoopReranker()
        return OpenAIReranker(
            endpoint=config.endpoint,
            api_key=config.api_key,
            model=config.model,
            top_n=config.top_n,
            min_score=config.min_score,
            timeout=config.timeout_seconds,
            instruct=self._fallback.instruction or None,
            credential_id=config.credential_id,
            on_usage=self._on_usage,
        )

    async def reload(self) -> None:
        await self.activate_prepared(await self.prepare_reload())

    async def prepare_reload(self) -> Reranker:
        return self._build_delegate(await self._resolve_config())

    async def activate_prepared(self, replacement: Reranker) -> None:
        await self._activate(replacement)

    async def discard_prepared(self, replacement: Reranker) -> None:
        await self._close_delegate(replacement)

"""Lazy chat-LLM client resolved from ``model_credentials`` by kind.

``llm_rerank`` and ``query_rewrite`` each hold one instance that resolves its
own active credential and falls back to the LLM_* settings. Both implement
``LLMClient.chat`` for the domain reranker and rewriter. The two kinds rate
limit independently: sharing one key does not share the TPM budget, an
accepted trade for managing and rotating each use on its own.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from oce.infrastructure.delegate_runtime import SwappableDelegate
from oce.infrastructure.llm.openai_compatible_client import OpenAICompatibleLLMClient
from oce.infrastructure.persistence.active_credential import resolve_active_credential
from oce.shared.config.settings import LLMSettings
from oce.shared.errors import ServiceNotReadyError
from oce.shared.metrics import UsageCallback


@dataclass(frozen=True)
class LLMRuntimeConfig:
    api_key: str
    base_url: str
    model: str | None
    proxy: str | None
    tpm_limit: int | None
    timeout_seconds: float
    credential_id: int = 0


@dataclass(frozen=True)
class _ActiveLLM:
    """One resolved credential and the HTTP client built from it, swapped as a unit."""

    client: OpenAICompatibleLLMClient
    config: LLMRuntimeConfig

    async def close(self) -> None:
        await self.client.close()


class CredentialConfiguredLLMClient(SwappableDelegate[_ActiveLLM]):
    """Resolve one kind's active credential and reuse its chat client."""

    def __init__(
        self,
        kind: str,
        session_factory: Callable[[], AsyncSession],
        fallback: LLMSettings,
        *,
        fallback_model: str,
        on_usage: UsageCallback | None = None,
    ) -> None:
        super().__init__()
        self._kind = kind
        self._session_factory = session_factory
        self._fallback = fallback
        self._fallback_model = fallback_model
        self._on_usage = on_usage

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        **kwargs: Any,
    ) -> str:
        active = await self._acquire()
        try:
            # The credential's model wins, then the caller's, then the default.
            resolved = active.config.model or model or self._fallback_model
            return await active.client.chat(messages, model=resolved, **kwargs)
        finally:
            await self._release(active)

    async def _create_delegate(self) -> _ActiveLLM:
        config = await self._resolve_config()
        return _ActiveLLM(self._build_delegate(config), config)

    async def _resolve_config(self) -> LLMRuntimeConfig:
        credential = await resolve_active_credential(self._session_factory, self._kind)
        fb = self._fallback
        if credential is not None and credential.api_key:
            return LLMRuntimeConfig(
                api_key=credential.api_key,
                base_url=credential.endpoint or fb.base_url,
                model=credential.model,
                proxy=fb.proxy,
                tpm_limit=(
                    credential.tpm_limit
                    if credential.tpm_limit is not None
                    else fb.tpm_limit
                ),
                timeout_seconds=float(credential.timeout_seconds),
                credential_id=credential.id,
            )

        fallback_key = fb.api_key.get_secret_value() if fb.api_key is not None else ""
        if not fallback_key:
            raise ServiceNotReadyError(
                f"No active {self._kind} credential or LLM_API_KEY is configured"
            )
        return LLMRuntimeConfig(
            api_key=fallback_key,
            base_url=fb.base_url,
            model=None,
            proxy=fb.proxy,
            tpm_limit=fb.tpm_limit,
            timeout_seconds=fb.timeout_seconds,
            credential_id=0,
        )

    def _build_delegate(self, config: LLMRuntimeConfig) -> OpenAICompatibleLLMClient:
        return OpenAICompatibleLLMClient(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            proxy=config.proxy,
            tpm_limit=config.tpm_limit,
            on_usage=self._on_usage,
            credential_id=config.credential_id,
            usage_kind=self._kind,
        )

    async def reload(self) -> None:
        """Resolve the credential again; the previous client closes once idle."""
        config = await self._resolve_config()
        await self._activate(_ActiveLLM(self._build_delegate(config), config))

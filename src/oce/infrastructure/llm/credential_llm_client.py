"""按 kind 从 model_credentials 解析的懒加载 chat-LLM 客户端。

llm_rerank / query_rewrite 各持一个实例（kind 不同），从凭证表解析自己的
active 凭证；取不到回落 LLMSettings（env）。实现 LLMClient.chat 协议，交给
domain 层的 reranker / rewriter 复用。

两个 kind 各自独立限流：若共用同一把 key，TPM 预算不共享（可接受的取舍，换取
按用途独立管理/轮换）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

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
    """解析某个 kind 的 active 凭证并复用其 chat client。"""

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
        **kwargs,
    ) -> str:
        active = await self._acquire()
        try:
            # 凭证 model 优先；其次调用方传入的 model；最后回落 env 默认模型。
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

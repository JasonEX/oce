"""Lazy embedding runtime configured by the active database credential."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from oce.infrastructure.delegate_runtime import SwappableDelegate
from oce.infrastructure.embed.openai_embedder import OpenAIEmbedder, embedding_base_url
from oce.infrastructure.persistence.active_credential import resolve_active_credential
from oce.shared.config.settings import EmbeddingSettings
from oce.shared.errors import ServiceNotReadyError
from oce.shared.index_profile import EmbeddingIndexProfile, profile_value_hash
from oce.shared.metrics import UsageCallback


@dataclass(frozen=True)
class EmbeddingRuntimeConfig:
    endpoint: str
    api_key: str
    model: str
    dimensions: int
    max_batch_size: int
    max_batch_chars: int
    max_input_chars: int
    input_overlap_chars: int
    max_concurrency: int
    timeout_seconds: float
    proxy: str | None
    query_instruction: str
    max_query_chars: int = 0
    credential_id: int = 0

    def normalized_endpoint(self) -> str:
        return embedding_base_url(self.endpoint)

    def indexed_vector_identity(self) -> tuple[object, ...]:
        """Fields whose change invalidates every stored document vector."""
        return (
            self.normalized_endpoint(),
            self.model,
            self.dimensions,
            self.max_input_chars,
            self.input_overlap_chars,
        )


@dataclass(frozen=True)
class PreparedEmbeddingReload:
    delegate: OpenAIEmbedder
    config: EmbeddingRuntimeConfig


class CredentialConfiguredEmbedder(SwappableDelegate[OpenAIEmbedder]):
    """Resolve one active credential, then reuse its OpenAI-compatible client."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        fallback: EmbeddingSettings,
        *,
        expected_dimensions: int,
        on_usage: UsageCallback | None = None,
        on_index_profile: (
            Callable[[EmbeddingIndexProfile], Awaitable[object]] | None
        ) = None,
    ) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._fallback = fallback
        self._expected_dimensions = expected_dimensions
        self._on_usage = on_usage
        self._on_index_profile = on_index_profile
        self._config: EmbeddingRuntimeConfig | None = None

    @property
    def enabled(self) -> bool:
        return self._fallback.enabled

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        delegate = await self._acquire()
        try:
            return await delegate.embed_documents(texts)
        finally:
            await self._release(delegate)

    async def embed_query(self, text: str) -> list[float]:
        delegate = await self._acquire()
        try:
            return await delegate.embed_query(text)
        finally:
            await self._release(delegate)

    async def _create_delegate(self) -> OpenAIEmbedder:
        config = await self._resolve_config()
        await self._validate_index_profile(config)
        self._config = config
        return self._build_delegate(config)

    def _build_delegate(self, config: EmbeddingRuntimeConfig) -> OpenAIEmbedder:
        return OpenAIEmbedder.from_endpoint(
            endpoint=config.endpoint,
            api_key=config.api_key,
            model=config.model,
            dimensions=config.dimensions,
            max_batch_size=config.max_batch_size,
            max_batch_chars=config.max_batch_chars,
            max_input_chars=config.max_input_chars,
            input_overlap_chars=config.input_overlap_chars,
            max_concurrency=config.max_concurrency,
            timeout=config.timeout_seconds,
            proxy=config.proxy,
            query_instruction=config.query_instruction,
            max_query_chars=config.max_query_chars,
            credential_id=config.credential_id,
            on_usage=self._on_usage,
        )

    async def _resolve_config(self) -> EmbeddingRuntimeConfig:
        fb = self._fallback
        credential = await resolve_active_credential(
            self._session_factory,
            "embed",
            require_endpoint_and_model=True,
        )
        # ``require_endpoint_and_model`` filtered null columns in SQL; the
        # checks below restate that for the type checker.
        if (
            credential is None
            or credential.endpoint is None
            or credential.model is None
        ):
            key = fb.api_key.get_secret_value() if fb.api_key is not None else ""
            if not key:
                raise ServiceNotReadyError(
                    "No active embedding credential or EMBED_API_KEY is configured"
                )
            config = EmbeddingRuntimeConfig(
                endpoint=fb.endpoint,
                api_key=key,
                model=fb.model,
                dimensions=fb.dimensions,
                max_batch_size=fb.max_batch_size,
                max_batch_chars=fb.max_batch_chars,
                max_input_chars=fb.max_input_chars,
                input_overlap_chars=fb.input_overlap_chars,
                max_concurrency=fb.max_concurrency,
                timeout_seconds=fb.timeout_seconds,
                proxy=fb.proxy,
                query_instruction=fb.query_instruction,
                max_query_chars=fb.max_query_chars,
            )
        else:
            # Kind-specific columns may be NULL (a minimal row with only
            # endpoint and model); each falls back on its own.
            def pick(value: int | None, default: int) -> int:
                return value if value is not None else default

            config = EmbeddingRuntimeConfig(
                endpoint=credential.endpoint,
                api_key=credential.api_key,
                model=credential.model,
                dimensions=pick(credential.dimensions, fb.dimensions),
                max_batch_size=pick(credential.max_batch_size, fb.max_batch_size),
                max_batch_chars=pick(credential.max_batch_chars, fb.max_batch_chars),
                max_input_chars=pick(credential.max_input_chars, fb.max_input_chars),
                input_overlap_chars=pick(
                    credential.input_overlap_chars, fb.input_overlap_chars
                ),
                max_concurrency=fb.max_concurrency,
                timeout_seconds=float(credential.timeout_seconds),
                proxy=fb.proxy,
                query_instruction=fb.query_instruction,
                max_query_chars=fb.max_query_chars,
                credential_id=credential.id,
            )

        if config.dimensions != self._expected_dimensions:
            raise ServiceNotReadyError(
                "Embedding credential dimensions do not match EMBED_DIMENSIONS"
            )
        return config

    async def close(self) -> None:
        self._config = None
        await super().close()

    async def validate_prepared(self, replacement: PreparedEmbeddingReload) -> None:
        await self._validate_index_profile(replacement.config)

    async def _validate_index_profile(self, config: EmbeddingRuntimeConfig) -> None:
        if self._on_index_profile is not None:
            await self._on_index_profile(self.index_profile_for_config(config))

    async def resolve_index_profile(self) -> EmbeddingIndexProfile:
        if not self.enabled:
            return EmbeddingIndexProfile(enabled=False)
        return self.index_profile_for_config(await self._resolve_config())

    @staticmethod
    def index_profile_for_config(
        config: EmbeddingRuntimeConfig,
    ) -> EmbeddingIndexProfile:
        return EmbeddingIndexProfile(
            enabled=True,
            endpoint_hash=profile_value_hash(config.normalized_endpoint()),
            model=config.model,
            dimensions=config.dimensions,
            query_instruction_hash=profile_value_hash(config.query_instruction),
            max_input_chars=config.max_input_chars,
            input_overlap_chars=config.input_overlap_chars,
        )

    def _ensure_reload_compatible(self, config: EmbeddingRuntimeConfig) -> None:
        if self._config is None:
            return
        if config.indexed_vector_identity() != self._config.indexed_vector_identity():
            raise ServiceNotReadyError(
                "Embedding model or document preprocessing changed; rebuild from clean "
                "metadata and vector storage, then resync clients before reloading"
            )

    async def prepare_reload(self) -> PreparedEmbeddingReload:
        config = await self._resolve_config()
        async with self._lock:
            self._ensure_reload_compatible(config)
        return PreparedEmbeddingReload(self._build_delegate(config), config)

    async def activate_prepared(self, replacement: PreparedEmbeddingReload) -> None:
        self._config = replacement.config
        await self._activate(replacement.delegate)

    async def discard_prepared(self, replacement: PreparedEmbeddingReload) -> None:
        await replacement.delegate.close()

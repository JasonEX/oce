"""Embedding credential command tests."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from oce.application.commands.credentials import (
    ReloadEmbeddingCredentialsCommand,
    ReloadEmbeddingCredentialsCommandHandler,
)
from oce.application.container import _CredentialRuntime
from oce.infrastructure.embed.credential_embedder import CredentialConfiguredEmbedder
from oce.infrastructure.embed.credential_reranker import CredentialConfiguredReranker
from oce.infrastructure.llm.credential_llm_client import CredentialConfiguredLLMClient
from oce.shared.config.settings import EmbeddingSettings
from oce.shared.errors import ServiceNotReadyError
from oce.shared.index_profile import EmbeddingIndexProfile


class _Runtime:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def reload(self) -> None:
        if self.error is not None:
            raise self.error


async def test_reload_credentials_reports_success():
    result = await ReloadEmbeddingCredentialsCommandHandler(_Runtime()).handle(
        ReloadEmbeddingCredentialsCommand()
    )

    assert result.reloaded is True
    assert result.reason is None


async def test_reload_credentials_reports_missing_configuration():
    runtime = _Runtime(ServiceNotReadyError("no credential"))

    result = await ReloadEmbeddingCredentialsCommandHandler(runtime).handle(
        ReloadEmbeddingCredentialsCommand()
    )

    assert result.reloaded is False
    assert result.reason == "[SERVICE_NOT_READY] no credential"


async def test_combined_reload_keeps_both_delegates_when_prepare_fails():
    class Runtime:
        enabled = True

        def __init__(
            self,
            *,
            prepare_error: Exception | None = None,
            validation_error: Exception | None = None,
        ) -> None:
            self.prepare_error = prepare_error
            self.validation_error = validation_error
            self.prepared = SimpleNamespace(config=object())
            self.activated = False
            self.discarded = False

        async def prepare_reload(self):
            if self.prepare_error is not None:
                raise self.prepare_error
            return self.prepared

        async def activate_prepared(self, replacement):
            assert replacement is self.prepared
            self.activated = True
            return 1

        async def discard_prepared(self, replacement):
            assert replacement is self.prepared
            self.discarded = True

        async def validate_prepared(self, replacement):
            assert replacement is self.prepared
            if self.validation_error is not None:
                raise self.validation_error

    embedder = Runtime()
    reranker = Runtime(prepare_error=ServiceNotReadyError("invalid reranker"))

    with pytest.raises(ServiceNotReadyError, match="invalid reranker"):
        await _CredentialRuntime(embedder, reranker).reload()

    assert embedder.activated is False
    assert embedder.discarded is True
    assert reranker.activated is False


async def test_combined_reload_clears_query_cache_after_embedding_activation():
    class Runtime:
        enabled = True

        async def prepare_reload(self):
            return SimpleNamespace(config=object())

        async def activate_prepared(self, _replacement):
            return 1

        async def discard_prepared(self, _replacement):
            raise AssertionError("successful reload must not discard")

        async def validate_prepared(self, _replacement):
            return None

    class Cache:
        def __init__(self) -> None:
            self.clears = 0

        async def clear_query_cache(self):
            self.clears += 1

    cache = Cache()

    await _CredentialRuntime(Runtime(), Runtime(), query_cache=cache).reload()

    assert cache.clears == 1


async def test_combined_reload_without_reranker_only_touches_embedder():
    class Runtime:
        enabled = True

        def __init__(self) -> None:
            self.activated = False

        async def prepare_reload(self):
            return SimpleNamespace(config=object())

        async def validate_prepared(self, _replacement):
            return None

        async def activate_prepared(self, _replacement):
            self.activated = True

        async def discard_prepared(self, _replacement):
            raise AssertionError("successful reload must not discard")

    embedder = Runtime()

    await _CredentialRuntime(embedder).reload()

    assert embedder.activated is True


async def test_combined_reload_discards_candidates_on_index_profile_mismatch():
    class Runtime:
        enabled = True

        def __init__(self, *, validation_error: Exception | None = None) -> None:
            self.prepared = SimpleNamespace(config=object())
            self.validation_error = validation_error
            self.discarded = False
            self.activated = False

        async def prepare_reload(self):
            return self.prepared

        async def activate_prepared(self, _replacement):
            self.activated = True
            return 1

        async def discard_prepared(self, replacement):
            assert replacement is self.prepared
            self.discarded = True

        async def validate_prepared(self, replacement):
            assert replacement is self.prepared
            if self.validation_error is not None:
                raise self.validation_error

    embedder = Runtime(validation_error=ServiceNotReadyError("index profile mismatch"))
    reranker = Runtime()

    with pytest.raises(ServiceNotReadyError, match="index profile mismatch"):
        await _CredentialRuntime(embedder, reranker).reload()

    assert embedder.discarded is True
    assert reranker.discarded is True
    assert embedder.activated is False
    assert reranker.activated is False


async def test_combined_reload_serializes_generation_activation() -> None:
    embedder = AsyncMock(spec=CredentialConfiguredEmbedder)
    embedder.enabled = True
    embedder.prepare_reload.side_effect = ["a", "b"]
    reranker = AsyncMock(spec=CredentialConfiguredReranker)
    reranker.prepare_reload.side_effect = ["a", "b"]
    first_swapped = asyncio.Event()
    release_first = asyncio.Event()
    activations: list[tuple[str, str]] = []

    async def activate_embedding(generation: str) -> None:
        activations.append(("embed", generation))
        if generation == "a":
            first_swapped.set()
            await release_first.wait()

    async def activate_reranker(generation: str) -> None:
        activations.append(("rerank", generation))

    embedder.activate_prepared.side_effect = activate_embedding
    reranker.activate_prepared.side_effect = activate_reranker
    runtime = _CredentialRuntime(embedder, reranker)
    first = asyncio.create_task(runtime.reload())
    second = None
    try:
        await asyncio.wait_for(first_swapped.wait(), 1)
        second = asyncio.create_task(runtime.reload())
        await asyncio.sleep(0)
        assert embedder.prepare_reload.await_count == 1
        release_first.set()
        await asyncio.wait_for(asyncio.gather(first, second), 1)
        assert activations == [
            ("embed", "a"),
            ("rerank", "a"),
            ("embed", "b"),
            ("rerank", "b"),
        ]
    finally:
        release_first.set()
        await asyncio.gather(first, *([second] if second is not None else []))


async def test_disabled_embedding_reload_validates_profile_and_reloads_llm() -> None:
    profile_check = AsyncMock()
    embedder = CredentialConfiguredEmbedder(
        lambda: None,
        EmbeddingSettings(enabled=False),
        expected_dimensions=1024,
        on_index_profile=profile_check,
    )
    llm = AsyncMock(spec=CredentialConfiguredLLMClient)
    llm.kind = "llm_rerank"
    cache = AsyncMock()
    ready = AsyncMock()
    try:
        assert (
            await _CredentialRuntime(
                embedder, llm_clients=[llm], query_cache=cache, on_ready=ready
            ).reload()
            is None
        )
        profile_check.assert_awaited_once_with(EmbeddingIndexProfile(enabled=False))
        llm.reload.assert_awaited_once()
        cache.clear_query_cache.assert_not_awaited()
        ready.assert_awaited_once()
        assert embedder._delegate is None
    finally:
        await embedder.close()


async def test_llm_reload_failure_reports_partial_result_and_keeps_other_clients() -> (
    None
):
    embedder = AsyncMock(spec=CredentialConfiguredEmbedder)
    embedder.enabled = True
    failed = AsyncMock(spec=CredentialConfiguredLLMClient)
    failed.kind = "llm_rerank"
    failed.reload.side_effect = ServiceNotReadyError("missing key")
    successful = AsyncMock(spec=CredentialConfiguredLLMClient)
    successful.kind = "query_rewrite"
    ready = AsyncMock()
    runtime = _CredentialRuntime(
        embedder, llm_clients=[failed, successful], on_ready=ready
    )

    result = await ReloadEmbeddingCredentialsCommandHandler(runtime).handle(
        ReloadEmbeddingCredentialsCommand()
    )

    assert result.reloaded is False
    assert result.reason == (
        "Model credentials only partially reloaded: llm_rerank (ServiceNotReadyError)"
    )
    embedder.activate_prepared.assert_awaited_once()
    successful.reload.assert_awaited_once()
    ready.assert_awaited_once()

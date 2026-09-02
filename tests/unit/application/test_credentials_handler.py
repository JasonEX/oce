"""Embedding credential command tests."""

from types import SimpleNamespace

import pytest

from oce.application.commands.credentials import (
    ReloadEmbeddingCredentialsCommand,
    ReloadEmbeddingCredentialsCommandHandler,
)
from oce.application.container import _CredentialRuntime
from oce.shared.errors import ServiceNotReadyError


class _Runtime:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def reload(self) -> int:
        if self.error is not None:
            raise self.error
        return 1


async def test_reload_credentials_returns_runtime_size():
    result = await ReloadEmbeddingCredentialsCommandHandler(_Runtime()).handle(
        ReloadEmbeddingCredentialsCommand()
    )

    assert result.reloaded is True
    assert result.pool_size == 1


async def test_reload_credentials_reports_missing_configuration():
    runtime = _Runtime(ServiceNotReadyError("no credential"))

    result = await ReloadEmbeddingCredentialsCommandHandler(runtime).handle(
        ReloadEmbeddingCredentialsCommand()
    )

    assert result.reloaded is False
    assert result.reason == "[SERVICE_NOT_READY] no credential"


async def test_combined_reload_keeps_both_delegates_when_prepare_fails():
    class Runtime:
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

    result = await _CredentialRuntime(
        Runtime(),
        Runtime(),
        query_cache=cache,
    ).reload()

    assert result == 1
    assert cache.clears == 1


async def test_combined_reload_discards_candidates_on_index_profile_mismatch():
    class Runtime:
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

"""Database-backed embedding credential resolution tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from oce.infrastructure.embed.credential_embedder import (
    CredentialConfiguredEmbedder,
    EmbeddingRuntimeConfig,
)
from oce.infrastructure.persistence.models import ModelCredentialModel
from oce.shared.config.settings import EmbeddingSettings
from oce.shared.database.session import Base
from oce.shared.errors import ServiceNotReadyError
from tests.fakes.embedding import FakeEmbeddingClient, FakeEmbeddingEndpoint


async def _runtime():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def test_active_credential_batch_settings_used():
    engine, sessions = await _runtime()
    async with sessions() as session:
        session.add(
            ModelCredentialModel(
                kind="embed",
                provider="siliconflow",
                name="primary",
                api_key="database-key",
                api_key_hash="hash",
                priority=10,
                endpoint="https://example.test/v1/embeddings",
                model="embedding-model",
                dimensions=1024,
                max_batch_size=8,
                max_batch_chars=24_000,
            )
        )
        await session.commit()

    embedder = CredentialConfiguredEmbedder(
        sessions,
        EmbeddingSettings(api_key="fallback-key"),
        expected_dimensions=1024,
    )
    config = await embedder._resolve_config()

    assert config.api_key == "database-key"
    assert config.max_batch_size == 8
    assert config.max_batch_chars == 24_000
    await engine.dispose()


async def test_environment_settings_are_used_without_active_credential():
    engine, sessions = await _runtime()
    settings = EmbeddingSettings(
        api_key="fallback-key",
        max_batch_size=7,
        max_batch_chars=31_000,
        query_instruction="Represent this query: ",
    )
    embedder = CredentialConfiguredEmbedder(
        sessions,
        settings,
        expected_dimensions=1024,
    )

    config = await embedder._resolve_config()

    assert config.api_key == "fallback-key"
    assert config.max_batch_size == 7
    assert config.max_batch_chars == 31_000
    assert config.query_instruction == "Represent this query: "
    profile = embedder.index_profile_for_config(config)
    assert profile.model == settings.model
    assert profile.dimensions == 1024
    assert profile.endpoint_hash is not None
    assert settings.endpoint not in str(profile)
    assert settings.query_instruction not in str(profile)
    assert (
        profile.fingerprint
        == embedder.index_profile_for_config(
            replace(config, api_key="rotated-key")
        ).fingerprint
    )
    assert (
        profile.fingerprint
        != embedder.index_profile_for_config(
            replace(config, model="different-model")
        ).fingerprint
    )
    delegate = embedder._build_delegate(config)
    assert delegate._query_instruction == "Represent this query: "
    await delegate.close()
    await engine.dispose()


async def test_reload_allows_key_rotation_but_rejects_vector_semantic_changes():
    engine, sessions = await _runtime()
    embedder = CredentialConfiguredEmbedder(
        sessions,
        EmbeddingSettings(api_key="fallback-key"),
        expected_dimensions=1024,
    )
    current = await embedder._resolve_config()
    embedder._config = current

    class Delegate:
        async def close(self):
            pass

    embedder._build_delegate = lambda _config: Delegate()

    rotated = replace(current, api_key="rotated-key", timeout_seconds=45.0)

    async def resolve_rotated():
        return rotated

    embedder._resolve_config = resolve_rotated
    prepared = await embedder.prepare_reload()
    assert prepared.config.api_key == "rotated-key"
    await embedder.discard_prepared(prepared)

    changed_model = replace(current, model="different-model")

    async def resolve_changed_model():
        return changed_model

    embedder._resolve_config = resolve_changed_model
    with pytest.raises(ServiceNotReadyError, match="clean metadata and vector storage"):
        await embedder.prepare_reload()

    await engine.dispose()


async def test_lazy_delegate_cannot_activate_before_persisted_profile_validation():
    engine, sessions = await _runtime()
    validated = []

    async def reject(profile):
        validated.append(profile)
        raise ServiceNotReadyError("index profile mismatch")

    embedder = CredentialConfiguredEmbedder(
        sessions,
        EmbeddingSettings(api_key="fallback-key"),
        expected_dimensions=1024,
        on_index_profile=reject,
    )

    class Delegate:
        async def embed_documents(self, _texts):
            raise AssertionError("delegate must not run before profile validation")

        async def close(self):
            pass

    embedder._build_delegate = lambda _config: Delegate()

    with pytest.raises(ServiceNotReadyError, match="index profile mismatch"):
        await embedder.embed_documents(["source"])

    assert validated[0].model == embedder._fallback.model
    assert embedder._delegate is None
    await engine.dispose()


async def test_reload_waits_for_inflight_request_before_closing_old_client():
    engine, sessions = await _runtime()
    embedder = CredentialConfiguredEmbedder(
        sessions,
        EmbeddingSettings(api_key="fallback-key"),
        expected_dimensions=1024,
    )
    started = asyncio.Event()
    release = asyncio.Event()

    class Delegate:
        def __init__(self, blocking: bool = False) -> None:
            self.blocking = blocking
            self.closed = False

        async def embed_documents(self, _texts):
            started.set()
            if self.blocking:
                await release.wait()
            return [[1.0]]

        async def close(self):
            self.closed = True

    old = Delegate(blocking=True)
    replacement = Delegate()
    embedder._delegate = old

    async def resolve_config():
        return object()

    embedder._resolve_config = resolve_config
    embedder._build_delegate = lambda _config: replacement

    request = asyncio.create_task(embedder.embed_documents(["source"]))
    await started.wait()
    prepared = await embedder.prepare_reload()
    await embedder.validate_prepared(prepared)
    await embedder.activate_prepared(prepared)

    assert old.closed is False
    assert embedder._delegate is replacement

    release.set()
    assert await request == [[1.0]]
    assert old.closed is True
    await embedder.close()
    await engine.dispose()


async def test_credential_id_and_usage_callback_wired_through():
    """The credential id lands in the config and passes to the delegate with on_usage."""
    engine, sessions = await _runtime()
    async with sessions() as session:
        credential = ModelCredentialModel(
            kind="embed",
            provider="siliconflow",
            name="primary",
            api_key="database-key",
            api_key_hash="hash",
            priority=10,
            endpoint="https://example.test/v1/embeddings",
            model="embedding-model",
            dimensions=1024,
        )
        session.add(credential)
        await session.commit()
        credential_id = credential.id

    async def _cb(*_args):
        return None

    embedder = CredentialConfiguredEmbedder(
        sessions,
        EmbeddingSettings(api_key="fallback-key"),
        expected_dimensions=1024,
        on_usage=_cb,
    )
    config = await embedder._resolve_config()
    assert config.credential_id == credential_id

    delegate = embedder._build_delegate(config)
    assert delegate._credential_id == credential_id
    assert delegate._on_usage is _cb
    await delegate.close()
    await engine.dispose()


async def test_hot_reload_generations_share_provider_budget_without_cancelling_old_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions = await _runtime()
    endpoint = FakeEmbeddingEndpoint(blocked=True)
    clients: list[FakeEmbeddingClient] = []

    def build_client(**kwargs: Any) -> FakeEmbeddingClient:
        client = FakeEmbeddingClient(
            endpoint=endpoint, http_client=kwargs["http_client"]
        )
        clients.append(client)
        return client

    monkeypatch.setattr(
        "oce.infrastructure.embed.openai_embedder.AsyncOpenAI", build_client
    )
    embedder = CredentialConfiguredEmbedder(
        sessions,
        EmbeddingSettings(
            api_key="fallback-key", dimensions=2, max_batch_size=1, max_concurrency=2
        ),
        expected_dimensions=2,
    )
    old_call = asyncio.create_task(embedder.embed_documents(["aaa", "bbb"]))
    new_calls = []
    try:
        async with asyncio.timeout(2):
            await endpoint.started.get()
            await endpoint.started.get()
        current = await embedder._resolve_config()

        async def resolve_rotated() -> EmbeddingRuntimeConfig:
            return replace(current, api_key="rotated-key")

        monkeypatch.setattr(embedder, "_resolve_config", resolve_rotated)
        prepared = await embedder.prepare_reload()
        await embedder.validate_prepared(prepared)
        await embedder.activate_prepared(prepared)
        assert len(clients) == 2
        assert not clients[0].closed

        new_calls = [
            asyncio.create_task(embedder.embed_documents(["ccc", "ddd"])),
            asyncio.create_task(embedder.embed_query("ggg")),
        ]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert endpoint.active == 2
        assert endpoint.calls == [["aaa"], ["bbb"]]
        assert endpoint.peak_active == 2

        endpoint.release.set()
        assert await old_call == [[1.0, 0.0], [1.0, 0.0]]
        assert await asyncio.gather(*new_calls) == [
            [[1.0, 0.0], [1.0, 0.0]],
            [0.0, 1.0],
        ]
        assert clients[0].closed
        assert not clients[1].closed
        assert endpoint.peak_active == 2
        assert endpoint.cancelled_calls == 0
    finally:
        endpoint.release.set()
        await asyncio.gather(old_call, *new_calls, return_exceptions=True)
        await embedder.close()
        await engine.dispose()

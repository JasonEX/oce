"""按 kind 解析的 chat-LLM 凭证客户端测试。"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from oce.infrastructure.llm.credential_llm_client import CredentialConfiguredLLMClient
from oce.infrastructure.persistence.models import ModelCredentialModel
from oce.shared.config.settings import LLMSettings
from oce.shared.database.session import Base
from oce.shared.errors import ServiceNotReadyError


async def _runtime():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def test_resolves_active_credential_for_kind():
    engine, sessions = await _runtime()
    async with sessions() as session:
        cred = ModelCredentialModel(
            kind="llm_rerank",
            name="rk",
            api_key="db-key",
            api_key_hash="h1",
            priority=5,
            endpoint="https://db.test/v1",
            model="db-model",
            tpm_limit=999,
            timeout_seconds=30,
        )
        session.add(cred)
        # 不同 kind 的行即使优先级更高也不应被 llm_rerank 选中
        session.add(
            ModelCredentialModel(
                kind="query_rewrite",
                name="i",
                api_key="other",
                api_key_hash="h2",
                priority=1,
                endpoint="https://x.test/v1",
                model="m",
            )
        )
        await session.commit()
        cid = cred.id

    client = CredentialConfiguredLLMClient(
        "llm_rerank",
        sessions,
        LLMSettings(api_key="env-key"),
        fallback_model="env-model",
    )
    config = await client._resolve_config()

    assert config.api_key == "db-key"
    assert config.base_url == "https://db.test/v1"
    assert config.model == "db-model"
    assert config.tpm_limit == 999
    assert config.credential_id == cid
    await engine.dispose()


async def test_falls_back_to_env_without_credential():
    engine, sessions = await _runtime()
    client = CredentialConfiguredLLMClient(
        "query_rewrite",
        sessions,
        LLMSettings(
            api_key="env-key",
            base_url="https://env.test/v1",
            tpm_limit=12_345,
            timeout_seconds=17,
        ),
        fallback_model="env-model",
    )
    config = await client._resolve_config()

    assert config.api_key == "env-key"
    assert config.base_url == "https://env.test/v1"
    assert config.model is None
    assert config.tpm_limit == 12_345
    assert config.timeout_seconds == 17
    assert config.credential_id == 0
    delegate = client._build_delegate(config)
    assert delegate._usage_kind == "query_rewrite"
    await delegate.close()
    await engine.dispose()


async def test_missing_database_and_environment_credential_fails_locally():
    engine, sessions = await _runtime()
    client = CredentialConfiguredLLMClient(
        "query_rewrite",
        sessions,
        LLMSettings(api_key=""),
        fallback_model="env-model",
    )

    with pytest.raises(
        ServiceNotReadyError, match="No active query_rewrite credential"
    ):
        await client._resolve_config()

    await engine.dispose()


async def test_build_delegate_wires_credential_id_and_usage():
    engine, sessions = await _runtime()

    async def _cb(*_args):
        return None

    client = CredentialConfiguredLLMClient(
        "query_rewrite",
        sessions,
        LLMSettings(api_key="env-key"),
        fallback_model="env-model",
        on_usage=_cb,
    )
    config = await client._resolve_config()
    delegate = client._build_delegate(config)

    assert delegate._credential_id == 0
    assert delegate._on_usage is _cb
    await delegate.close()
    await engine.dispose()


async def test_chat_model_precedence():
    """凭证 model > 调用方 model > fallback_model。"""
    engine, sessions = await _runtime()
    async with sessions() as session:
        session.add(
            ModelCredentialModel(
                kind="llm_rerank",
                name="rk",
                api_key="db-key",
                api_key_hash="h1",
                priority=5,
                endpoint="https://db.test/v1",
                model="db-model",
            )
        )
        await session.commit()

    captured: dict[str, str] = {}

    class _FakeDelegate:
        async def chat(self, messages, model=None, **kwargs):
            captured["model"] = model
            return "ok"

        async def close(self) -> None:
            return None

    client = CredentialConfiguredLLMClient(
        "llm_rerank",
        sessions,
        LLMSettings(api_key="env-key"),
        fallback_model="env-model",
    )
    client._build_delegate = lambda config: _FakeDelegate()

    # 凭证 model 存在 → 覆盖调用方传入的 model
    await client.chat([{"role": "user", "content": "x"}], model="call-model")
    assert captured["model"] == "db-model"
    await client.close()
    await engine.dispose()


async def test_reload_defers_closing_in_flight_client_until_release(monkeypatch):
    engine, sessions = await _runtime()
    client = CredentialConfiguredLLMClient(
        "llm_rerank",
        sessions,
        LLMSettings(api_key="env-key"),
        fallback_model="env-model",
    )

    class _FakeDelegate:
        def __init__(self) -> None:
            self.closed = 0

        async def close(self) -> None:
            self.closed += 1

    delegates: list[_FakeDelegate] = []

    def build(_config):
        delegate = _FakeDelegate()
        delegates.append(delegate)
        return delegate

    monkeypatch.setattr(client, "_build_delegate", build)
    active = await client._acquire()

    await client.reload()

    assert len(delegates) == 2
    assert delegates[0].closed == 0
    await client._release(active)
    assert delegates[0].closed == 1

    await client.close()
    assert delegates[1].closed == 1
    await engine.dispose()


async def test_reload_closes_idle_client_immediately(monkeypatch):
    engine, sessions = await _runtime()
    client = CredentialConfiguredLLMClient(
        "query_rewrite",
        sessions,
        LLMSettings(api_key="env-key"),
        fallback_model="env-model",
    )

    class _FakeDelegate:
        def __init__(self) -> None:
            self.closed = 0

        async def close(self) -> None:
            self.closed += 1

    delegates: list[_FakeDelegate] = []

    def build(_config):
        delegate = _FakeDelegate()
        delegates.append(delegate)
        return delegate

    monkeypatch.setattr(client, "_build_delegate", build)
    active = await client._acquire()
    await client._release(active)

    await client.reload()

    assert delegates[0].closed == 1
    await client.close()
    assert delegates[1].closed == 1
    await engine.dispose()

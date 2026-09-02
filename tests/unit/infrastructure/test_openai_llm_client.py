"""OpenAICompatibleLLMClient.chat 用量上报测试。

mock httpx，验证 chat 成功后按响应真实 usage 旁路上报；缺 usage 字段不臆造、
无回调时零开销。LLM 无 DB 凭证，credential_id 恒为 0。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import oce.infrastructure.llm.openai_compatible_client as llm_mod
from oce.infrastructure.llm.openai_compatible_client import OpenAICompatibleLLMClient


def _fake_response(payload: dict):
    resp = MagicMock()
    resp.json = MagicMock(return_value=payload)
    resp.raise_for_status = MagicMock(return_value=None)
    resp.status_code = 200
    return resp


class _FakeAsyncClient:
    """够 chat() 用的长连接 httpx.AsyncClient 替身。"""

    def __init__(self, payload: dict, **_: object) -> None:
        self._payload = payload
        self.closed = False
        self.posts = 0

    async def post(self, url, json=None, headers=None):
        self.posts += 1
        return _fake_response(self._payload)

    async def aclose(self) -> None:
        self.closed = True


def _patch_httpx(monkeypatch, payload: dict) -> None:
    monkeypatch.setattr(
        llm_mod.httpx, "AsyncClient", lambda **kw: _FakeAsyncClient(payload, **kw)
    )


@pytest.mark.asyncio
async def test_chat_reports_usage_on_success(monkeypatch):
    captured: list[tuple] = []

    async def _on_usage(cid, kind, model, prompt, completion):
        captured.append((cid, kind, model, prompt, completion))

    _patch_httpx(
        monkeypatch,
        {
            "choices": [{"message": {"content": "hello"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 5},
        },
    )
    client = OpenAICompatibleLLMClient(api_key="sk", on_usage=_on_usage)

    content = await client.chat([{"role": "user", "content": "hi"}], model="test-llm")

    assert content == "hello"
    # LLM 无凭证：credential_id 恒 0；prompt/completion 按响应真实值
    assert captured == [(0, "llm", "test-llm", 12, 5)]


@pytest.mark.asyncio
async def test_chat_without_usage_does_not_report(monkeypatch):
    captured: list[tuple] = []

    async def _on_usage(cid, kind, model, prompt, completion):
        captured.append((cid, kind, model, prompt, completion))

    _patch_httpx(monkeypatch, {"choices": [{"message": {"content": "hi"}}]})
    client = OpenAICompatibleLLMClient(api_key="sk", on_usage=_on_usage)

    await client.chat([{"role": "user", "content": "hi"}], model="test-llm")

    assert captured == []  # 缺 usage 字段：不臆造、不上报


@pytest.mark.asyncio
async def test_chat_without_callback_is_inert(monkeypatch):
    _patch_httpx(
        monkeypatch,
        {
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    )
    client = OpenAICompatibleLLMClient(api_key="sk")  # on_usage=None

    assert (
        await client.chat([{"role": "user", "content": "hi"}], model="test-llm") == "hi"
    )


@pytest.mark.asyncio
async def test_usage_failure_does_not_fail_successful_chat(monkeypatch):
    async def _on_usage(*_args):
        raise RuntimeError("metrics unavailable")

    _patch_httpx(
        monkeypatch,
        {
            "choices": [{"message": {"content": "answer"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": "malformed"},
        },
    )
    client = OpenAICompatibleLLMClient(
        api_key="sk",
        on_usage=_on_usage,
        usage_kind="llm_rerank",
    )

    content = await client.chat(
        [{"role": "user", "content": "hi"}],
        model="test-llm",
    )

    assert content == "answer"


@pytest.mark.asyncio
async def test_proxy_does_not_disable_tls_verification(monkeypatch):
    captured: dict[str, object] = {}
    payload = {"choices": [{"message": {"content": "hi"}}]}

    def factory(**kwargs):
        captured.update(kwargs)
        return _FakeAsyncClient(payload, **kwargs)

    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", factory)
    client = OpenAICompatibleLLMClient(
        api_key="sk",
        proxy="http://proxy.test:8080",
    )

    assert await client.chat([{"role": "user", "content": "hi"}], model="m") == "hi"
    assert captured["proxy"] == "http://proxy.test:8080"
    assert "verify" not in captured


@pytest.mark.asyncio
async def test_reuses_connection_pool_and_closes_it(monkeypatch):
    instances: list[_FakeAsyncClient] = []
    payload = {"choices": [{"message": {"content": "hi"}}]}

    def factory(**kwargs):
        instance = _FakeAsyncClient(payload, **kwargs)
        instances.append(instance)
        return instance

    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", factory)
    client = OpenAICompatibleLLMClient(api_key="sk")

    await client.chat([{"role": "user", "content": "one"}], model="m")
    await client.chat([{"role": "user", "content": "two"}], model="m")
    await client.close()

    assert len(instances) == 1
    assert instances[0].posts == 2
    assert instances[0].closed is True

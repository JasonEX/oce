"""OpenAI-compatible LLM client implementation."""

from __future__ import annotations

import asyncio

import httpx
from loguru import logger

from oce.infrastructure.llm.rate_limiter import TokenRateLimiter, estimate_tokens
from oce.shared.metrics import UsageCallback, coerce_token_count

# 输出 token 也计入 TPM。rerank 只回编号（实测约 34 token），按此量级留余量，
# 不按 max_tokens 记账，否则预算瞬间被 8000 占满。
_OUTPUT_TOKEN_ALLOWANCE = 256
# 限流器按估算值排队，估算偏松时仍可能 429，退避重试兜底
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 20.0


class OpenAICompatibleLLMClient:
    """OpenAI 兼容的 LLM 聊天客户端（/v1/chat/completions），rerank / rewrite 共用。

    持有一个长期 httpx 连接池；凭据热重载时由上层整体替换实例并在空闲后 close。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.siliconflow.cn/v1",
        timeout: float = 120.0,
        proxy: str | None = None,
        tpm_limit: int | None = None,
        on_usage: UsageCallback | None = None,
        credential_id: int = 0,
        usage_kind: str = "llm",
    ):
        """
        Args:
            tpm_limit: 接口 TPM 上限。给定时请求前排队，避免 429 让上层静默降级。
            on_usage: 可选用量回调；每次成功 chat 后按真实 usage 上报，None 时零开销。
            credential_id: 解析到的 DB 凭证 id，随用量上报；纯 env 回落时为 0。
            usage_kind: 用量归因阶段，如 llm_rerank 或 query_rewrite。
        """
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self.proxy = proxy
        self._on_usage = on_usage
        self._credential_id = credential_id
        self._usage_kind = usage_kind
        self._limiter = (
            TokenRateLimiter(tpm_limit) if tpm_limit and tpm_limit > 0 else None
        )
        client_kwargs: dict = {"timeout": timeout}
        if proxy:
            client_kwargs["proxy"] = proxy
        self._client = httpx.AsyncClient(**client_kwargs)

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float = 0.1,
        max_tokens: int = 8000,
        **kwargs,
    ) -> str:
        """
        发送聊天请求。

        Args:
            messages: 消息列表，格式 [{"role": "user", "content": "..."}]
            model: 模型名称
            temperature: 温度参数（0.1 = 更确定性）
            max_tokens: 最大生成 token 数。思维链也计入该预算，作为兜底放宽，
                避免推理输出把 content 挤空。

        Returns:
            模型响应内容
        """
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        payload: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
            **kwargs,
        }

        # 统一关闭思维链；调用方显式传 thinking 时（kwargs 已合并进 payload）不覆盖。
        if "openrouter.ai" in self.base_url:
            payload.setdefault(
                "reasoning",
                {
                    "enabled": False,
                    "effort": "none",
                },
            )
        else:
            payload.setdefault("thinking", {"type": "disabled"})

        # 输入按 prompt 估算，输出按固定余量记账
        estimated = (
            sum(estimate_tokens(m.get("content", "")) for m in messages)
            + _OUTPUT_TOKEN_ALLOWANCE
        )

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            if self._limiter is not None:
                waited = await self._limiter.acquire(estimated)
                if waited > 0:
                    logger.debug(
                        "TPM limiter delayed request by {:.1f}s (est {} tokens)",
                        waited,
                        estimated,
                    )
            try:
                response = await self._client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()
            except httpx.HTTPStatusError as e:
                # 429 说明估算偏松或有其他调用方共用配额，退避后重试；
                # 直接抛出会让 reranker 静默退回原始顺序。
                if e.response.status_code == 429 and attempt < _MAX_ATTEMPTS:
                    logger.warning(
                        "LLM 429, retry {}/{} after {}s",
                        attempt,
                        _MAX_ATTEMPTS,
                        _RETRY_BACKOFF_SECONDS,
                    )
                    await asyncio.sleep(_RETRY_BACKOFF_SECONDS)
                    continue
                logger.error("LLM API error: status={}", e.response.status_code)
                raise
            except httpx.TimeoutException:
                logger.error("LLM API timeout after {}s", self.timeout)
                raise
            except Exception as e:
                logger.error("LLM client error: {}", type(e).__name__)
                raise

            message = data["choices"][0]["message"]
            content = message["content"]
            if content is None:
                content = message["reasoning"]
            await self._report_usage(model, data.get("usage") or {})
            return content

        raise RuntimeError("LLM chat exhausted retries without a response")

    async def _report_usage(self, model: str, usage: dict) -> None:
        """按真实 usage 旁路上报；缺字段则跳过，回调失败不影响响应。"""
        if self._on_usage is None:
            return
        prompt = coerce_token_count(usage.get("prompt_tokens", 0))
        completion = coerce_token_count(usage.get("completion_tokens", 0))
        if not (prompt or completion):
            return
        try:
            await self._on_usage(
                self._credential_id, self._usage_kind, model, prompt, completion
            )
        except Exception as exc:
            logger.warning("LLM usage reporting failed: {}", type(exc).__name__)

    async def close(self) -> None:
        await self._client.aclose()

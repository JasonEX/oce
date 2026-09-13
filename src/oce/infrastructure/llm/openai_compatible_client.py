"""OpenAI-compatible LLM client implementation."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from loguru import logger

from oce.infrastructure.llm.rate_limiter import TokenRateLimiter, estimate_tokens
from oce.shared.metrics import UsageCallback, coerce_token_count

# Output tokens count against TPM too. A rerank reply is a list of ids
# (about 34 tokens), so a small allowance is reserved rather than
# max_tokens, which would consume 8000 of the budget per call.
_OUTPUT_TOKEN_ALLOWANCE = 256
# The limiter queues on estimates; a loose estimate can still hit 429, so a
# backoff retry backs it up.
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 20.0


class OpenAICompatibleLLMClient:
    """OpenAI-compatible chat client (``/v1/chat/completions``) shared by rerank and rewrite.

    It owns one long-lived httpx pool; a credential reload replaces the whole
    instance and closes this one once idle.
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
            tpm_limit: TPM limit; when given, requests queue instead of
                hitting 429 and silently degrading the caller.
            on_usage: usage callback reported after each successful chat.
            credential_id: credential row id for usage attribution; 0 for
                the environment fallback.
            usage_kind: the stage the usage belongs to (llm_rerank, query_rewrite).
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
        **kwargs: Any,
    ) -> str:
        """The model's reply.

        ``max_tokens`` is generous on purpose: reasoning output counts against
        it, and a tight budget lets reasoning crowd out the content.
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

        # Reasoning is off unless the caller passed its own setting in kwargs.
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

        # Input is estimated from the prompt, output as a fixed allowance.
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
                # 429: the estimate was loose or another caller shares the
                # quota. Raising would make the reranker silently keep the
                # input order, so back off and retry.
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
        """Report usage on the side channel; missing fields skip, a failing callback is logged."""
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

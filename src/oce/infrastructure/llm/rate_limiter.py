"""Tokens-per-minute limiting for LLM calls.

OpenAI-compatible gateways meter tokens over a 60-second sliding window and
return 429 above it (SiliconFlow code 50602). One rerank call can reach 16k
tokens, so 60k TPM allows three or four; the client must queue rather than
retry after the fact, because a failed retry makes the reranker keep the
input order and quietly mixes unreranked queries into an evaluation.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque


def estimate_tokens(text: str) -> int:
    """Rough token estimate, erring high: one per CJK character, three characters per token otherwise."""
    cjk = 0
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff":
            cjk += 1
    other = len(text) - cjk
    return cjk + other // 3 + 1


class TokenRateLimiter:
    """Sliding-window TPM limiter shared across coroutines."""

    def __init__(
        self,
        tokens_per_minute: int,
        window_seconds: float = 60.0,
        safety_ratio: float = 0.9,
    ) -> None:
        """``safety_ratio`` discounts the budget to absorb estimation error."""
        self.budget = max(1, int(tokens_per_minute * safety_ratio))
        self.window = window_seconds
        self._events: deque[tuple[float, int]] = deque()
        self._used = 0
        self._lock = asyncio.Lock()

    def _evict(self, now: float) -> None:
        """Drop bookings that slid out of the window."""
        while self._events and now - self._events[0][0] >= self.window:
            _, tokens = self._events.popleft()
            self._used -= tokens

    async def acquire(self, tokens: int) -> float:
        """Book ``tokens``, waiting for the window to slide; returns the seconds waited."""
        # A request larger than the whole budget books the budget, or it
        # would wait forever.
        need = min(max(tokens, 1), self.budget)
        waited = 0.0

        while True:
            async with self._lock:
                now = time.monotonic()
                self._evict(now)
                if self._used + need <= self.budget:
                    self._events.append((now, need))
                    self._used += need
                    return waited
                # Room appears once the oldest booking leaves the window.
                sleep_for = self.window - (now - self._events[0][0])

            sleep_for = max(sleep_for, 0.05)
            waited += sleep_for
            await asyncio.sleep(sleep_for)

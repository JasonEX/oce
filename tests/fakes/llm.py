"""A scripted chat client for model parsing, deadlines and failure propagation."""

from __future__ import annotations

import asyncio
from typing import Any


class FakeLLM:
    def __init__(
        self,
        response: str = "",
        *,
        error: BaseException | None = None,
        delay: float = 0.0,
    ) -> None:
        self.response = response
        self.error = error
        self.delay = delay
        self.messages: list[dict[str, str]] = []
        self.kwargs: dict[str, Any] = {}

    async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        self.messages = messages
        self.kwargs = kwargs
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.response

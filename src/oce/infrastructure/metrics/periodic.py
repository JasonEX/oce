"""Background loop shared by the monitoring side tasks.

Each tick is isolated: an exception is logged and the loop continues, because
monitoring must never take the process down or block the request path.
"""

from __future__ import annotations

import asyncio

from loguru import logger


class PeriodicTask:
    def __init__(self, *, interval_seconds: float, name: str) -> None:
        self._interval = interval_seconds
        self._name = name
        self._task: asyncio.Task | None = None
        self._running = False

    async def _tick(self) -> None:
        raise NotImplementedError

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._interval)
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("{} loop error: {}", self._name, exc)

"""Periodic deletion of monitoring rows older than ``retention_days``.

Only the four monitoring tables are touched; chain and blob garbage
collection is the separate GC command. A failed cleanup is logged and never
affects the request path.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from loguru import logger
from sqlalchemy import CursorResult, delete
from sqlalchemy.ext.asyncio import AsyncSession

from oce.infrastructure.metrics.periodic import PeriodicTask
from oce.infrastructure.persistence.models import (
    ApiCallMetricModel,
    ResourceSampleModel,
    RetrievalMetricModel,
    TokenUsageMetricModel,
)

_MODELS = (
    ApiCallMetricModel,
    TokenUsageMetricModel,
    ResourceSampleModel,
    RetrievalMetricModel,
)


class MonitoringCleaner(PeriodicTask):
    """Delete expired monitoring rows on an interval; runs in both modes."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        retention_days: int,
        interval_seconds: float,
    ) -> None:
        super().__init__(interval_seconds=interval_seconds, name="monitoring cleanup")
        self._session_factory = session_factory
        self._retention_days = retention_days

    async def _tick(self) -> None:
        await self._cleanup_once()

    async def _cleanup_once(self) -> int:
        """Delete rows older than the retention period; returns the count, 0 on failure."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._retention_days)
        deleted = 0
        try:
            async with self._session_factory() as session:
                for model in _MODELS:
                    # DML statements return a cursor result; the session API
                    # is typed against the generic ``Result``.
                    result = cast(
                        CursorResult[Any],
                        await session.execute(delete(model).where(model.ts < cutoff)),
                    )
                    deleted += result.rowcount or 0
                await session.commit()
        except Exception as exc:
            logger.warning("monitoring cleanup failed: {}", exc)
            return 0
        if deleted:
            logger.info("monitoring cleanup removed {} expired rows", deleted)
        return deleted

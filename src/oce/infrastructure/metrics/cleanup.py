"""监控数据清理任务：后台周期删除超过 retention_days 的监控行。

只清监控四表（api_call / token / resource / retrieval），按 ``ts < now - retention_days``。
GC（过期 chain、孤儿 blob）不在此处——那是独立流程，待专门确认后落地。

旁路：清理失败只记日志、绝不影响主链路。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import delete
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
    """按 retention_days 周期清理监控四表的过期行；个人 / 服务模式都跑。"""

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
        """删除所有 ts 早于保留期的监控行，返回删除总数。失败只记日志、返回 0。"""
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._retention_days)
        deleted = 0
        try:
            async with self._session_factory() as session:
                for model in _MODELS:
                    result = await session.execute(
                        delete(model).where(model.ts < cutoff)
                    )
                    deleted += result.rowcount or 0
                await session.commit()
        except Exception as exc:  # 旁路：清理失败不影响主链路
            logger.warning("monitoring cleanup failed: {}", exc)
            return 0
        if deleted:
            logger.info("monitoring cleanup removed {} expired rows", deleted)
        return deleted

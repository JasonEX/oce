"""Monitoring statistics for ``/admin/stats``.

The handler forwards to the injected reader; the read model and reader port
live in ``shared.metrics_read``.
"""

from __future__ import annotations

from dataclasses import dataclass

from oce.application.messages import Query
from oce.shared.metrics_read import MonitoringStats, MonitoringStatsReader


@dataclass(frozen=True)
class MonitoringStatsQuery(Query):
    window_hours: int = 24


class MonitoringStatsQueryHandler:
    def __init__(self, reader: MonitoringStatsReader) -> None:
        self._reader = reader

    async def handle(self, query: MonitoringStatsQuery) -> MonitoringStats:
        return await self._reader.read(query.window_hours)

"""Read model and reader port of ``/admin/stats``.

``metrics.py`` writes; this module only defines the aggregated result types
and the reader protocol that infrastructure implements in SQL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class ApiCallStats:
    count: int = 0
    error_count: int = 0
    avg_latency_ms: float = 0.0
    p50_latency_ms: int = 0
    p95_latency_ms: int = 0
    max_latency_ms: int = 0


@dataclass(frozen=True)
class TokenKindStats:
    kind: str
    calls: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class RetrievalStats:
    count: int = 0
    empty_count: int = 0
    empty_rate: float = 0.0


@dataclass(frozen=True)
class ResourceSnapshot:
    ts: datetime | None = None
    mem_rss_bytes: int = 0
    mem_percent: float = 0.0
    cpu_percent: float = 0.0
    disk_free_bytes: int = 0
    disk_total_bytes: int = 0
    disk_data_bytes: int = 0


@dataclass(frozen=True)
class MonitoringStats:
    window_hours: int
    api_calls: ApiCallStats = field(default_factory=ApiCallStats)
    tokens: tuple[TokenKindStats, ...] = ()
    tokens_total: int = 0
    retrieval: RetrievalStats = field(default_factory=RetrievalStats)
    resource: ResourceSnapshot | None = None


class MonitoringStatsReader(Protocol):
    """Aggregate the four monitoring tables over a time window."""

    async def read(self, window_hours: int) -> MonitoringStats: ...

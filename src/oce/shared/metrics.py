"""Monitoring ports.

Collectors (the HTTP middleware, model clients, the resource sampler) depend
only on the protocol and record types here; the composition root injects the
sink. Every ``record_*`` is synchronous, non-blocking and never raises:
monitoring is a side channel that must not slow or break the request path.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import perf_counter
from typing import Protocol


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Usage callback of the model clients: (credential_id, kind, model,
# prompt_tokens, completion_tokens). credential_id 0 means the environment
# fallback with no credential row; the sink stores it as None.
UsageCallback = Callable[[int, str, str, int, int], Awaitable[None]]


def coerce_token_count(value: object) -> int:
    """A provider usage field as a non-negative int; missing or malformed is 0."""
    if not isinstance(value, (int, float, str)):
        return 0
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class ApiCallRecord:
    endpoint: str
    method: str
    status_code: int
    latency_ms: int
    error_type: str | None = None
    ts: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class TokenUsageRecord:
    kind: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    credential_id: int | None = None
    ts: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class ResourceSampleRecord:
    disk_data_bytes: int
    disk_free_bytes: int
    disk_total_bytes: int
    mem_rss_bytes: int
    mem_percent: float
    cpu_percent: float
    ts: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class RetrievalMetricRecord:
    """One retrieval's audit record; ``hit_count`` 0 is an empty answer, stages are in ms."""

    source: str
    hit_count: int
    total_ms: int
    scope_size: int | None = None
    intent: str | None = None
    path_boosted: bool = False
    rerank_route: str | None = None
    # ``dense`` when vector recall ran, ``skip:<evidence>`` when the SQL lanes had
    # already answered and the embedding round trip was not awaited.
    dense_route: str | None = None
    head_slots: int = 0
    # Structural evidence the routing saw: definitions of the queried names and
    # the largest number of places declaring one of them (ambiguity).
    exact_definitions: int = 0
    definition_sites: int = 0
    # Relation sections appended after the primary results, and their size.
    relation_hits: int = 0
    relation_chars: int = 0
    query_text: str | None = None
    stages: dict[str, int] = field(default_factory=dict)
    # Recall or relation lanes that failed and were skipped, by exception type.
    # An HTTP 200 with a non-empty map answered from fewer lanes than planned.
    lane_failures: dict[str, str] = field(default_factory=dict)
    ts: datetime = field(default_factory=_now)


@dataclass
class RetrievalAudit:
    """Mutable collector the pipeline fills; the domain never sees the sink.

    ``with audit.stage("dense"):`` measures wall time around awaits; a stage
    timed twice (several facets) accumulates.
    """

    intent: str | None = None
    path_boosted: bool = False
    # dedicated / dedicated+llm / skip:<reason>; None when rerank never ran.
    rerank_route: str | None = None
    # dense / skip:exact_definition / skip:path_evidence / skip:use_sites; None
    # when recall never ran.
    dense_route: str | None = None
    # Head slots the deterministic symbol/path answer actually took.
    head_slots: int = 0
    # Definitions found for the named symbols, and how many places declare
    # the most ambiguous one.
    exact_definitions: int = 0
    definition_sites: int = 0
    # Relation sections: excerpts per lane and their total size.
    relation_counts: dict[str, int] = field(default_factory=dict)
    relation_chars: int = 0
    scope_size: int | None = None
    stages: dict[str, int] = field(default_factory=dict)
    # Lanes whose failure the pipeline absorbed, mapped to the exception type.
    # Degradation stays invisible to the caller, so it is recorded here.
    lane_failures: dict[str, str] = field(default_factory=dict)

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        start = perf_counter()
        try:
            yield
        finally:
            elapsed = int((perf_counter() - start) * 1000)
            self.record(name, elapsed)

    def record(self, name: str, elapsed_ms: int) -> None:
        """Add a stage duration measured elsewhere (a task awaited later)."""
        self.stages[name] = self.stages.get(name, 0) + elapsed_ms


class MetricsSink(Protocol):
    """The collection port; every ``record_*`` is synchronous and never raises."""

    def record_api_call(self, record: ApiCallRecord) -> None: ...

    def record_token_usage(self, record: TokenUsageRecord) -> None: ...

    def record_resource_sample(self, record: ResourceSampleRecord) -> None: ...

    def record_retrieval(self, record: RetrievalMetricRecord) -> None: ...


class ManagedMetricsSink(MetricsSink, Protocol):
    """A sink the composition root starts before serving and stops on shutdown."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class NoopMetricsSink:
    """The sink when monitoring is off; it also satisfies the lifecycle protocol."""

    def record_api_call(self, record: ApiCallRecord) -> None:
        return None

    def record_token_usage(self, record: TokenUsageRecord) -> None:
        return None

    def record_resource_sample(self, record: ResourceSampleRecord) -> None:
        return None

    def record_retrieval(self, record: RetrievalMetricRecord) -> None:
        return None

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

"""Read models and ports for authoritative index operational state."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class MetadataIndexStats:
    blobs_total: int = 0
    blobs_ready: int = 0
    blobs_pending: int = 0
    blobs_error: int = 0
    chunks_total: int = 0
    chunks_embedded: int = 0
    blob_chunk_links: int = 0
    symbol_occurrences: int = 0
    chains: int = 0
    chain_members: int = 0
    staging_blobs: int = 0


@dataclass(frozen=True)
class IndexStoreStats:
    enabled: bool
    available: bool
    collection_name: str | None = None
    exists: bool | None = None
    entities: int | None = None
    error_type: str | None = None


@dataclass(frozen=True)
class QueryCacheStats:
    enabled: bool
    entries: int
    max_entries: int
    ttl_seconds: float
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    invalidations: int = 0


@dataclass(frozen=True)
class IndexStats:
    metadata: MetadataIndexStats = field(default_factory=MetadataIndexStats)
    dense: IndexStoreStats = field(
        default_factory=lambda: IndexStoreStats(False, False)
    )
    path: IndexStoreStats = field(default_factory=lambda: IndexStoreStats(False, False))
    query_cache: QueryCacheStats = field(
        default_factory=lambda: QueryCacheStats(False, 0, 0, 0.0)
    )


class MetadataIndexStatsReader(Protocol):
    async def read(self) -> MetadataIndexStats: ...


class IndexStoreStatsProvider(Protocol):
    async def index_stats(self) -> IndexStoreStats: ...


class QueryCacheStatsProvider(Protocol):
    async def query_cache_stats(self) -> QueryCacheStats: ...

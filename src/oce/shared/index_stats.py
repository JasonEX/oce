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
class RetrievalRuntimeProfile:
    embedding_enabled: bool = True
    semantic_chunking_enabled: bool = True
    exact_enabled: bool = True
    path_index_enabled: bool = True
    source_priority_enabled: bool = True
    coverage_selection_enabled: bool = True
    query_decomposition_enabled: bool = True
    api_rerank_enabled: bool = False
    llm_rerank_enabled: bool = False
    query_rewrite_enabled: bool = False
    intent_classification_enabled: bool = False


@dataclass(frozen=True)
class IndexProfileStats:
    state: str
    fingerprint: str | None = None
    schema_version: int | None = None
    embedding_enabled: bool | None = None
    embedding_model: str | None = None
    embedding_dimensions: int | None = None


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
    runtime: RetrievalRuntimeProfile = field(default_factory=RetrievalRuntimeProfile)
    profile: IndexProfileStats = field(
        default_factory=lambda: IndexProfileStats("uninitialized")
    )


class MetadataIndexStatsReader(Protocol):
    async def read(self) -> MetadataIndexStats: ...


class IndexStoreStatsProvider(Protocol):
    async def index_stats(self) -> IndexStoreStats: ...


class QueryCacheStatsProvider(Protocol):
    async def query_cache_stats(self) -> QueryCacheStats: ...


class IndexProfileStatsProvider(Protocol):
    async def index_profile_stats(self) -> IndexProfileStats: ...

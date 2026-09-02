"""Index stats query composes sources without hiding store failures."""

from oce.application.queries.index_stats import IndexStatsQuery, IndexStatsQueryHandler
from oce.shared.index_stats import (
    IndexProfileStats,
    IndexStoreStats,
    MetadataIndexStats,
    QueryCacheStats,
    RetrievalRuntimeProfile,
)


class MetadataReader:
    async def read(self):
        return MetadataIndexStats(blobs_total=4, blobs_ready=3)


class Store:
    def __init__(self, result=None, error=None) -> None:
        self.result = result
        self.error = error

    async def index_stats(self):
        if self.error is not None:
            raise self.error
        return self.result


class Cache:
    async def query_cache_stats(self):
        return QueryCacheStats(True, 2, 256, 600.0, hits=3, misses=1)


class Profile:
    async def index_profile_stats(self):
        return IndexProfileStats(
            "compatible",
            fingerprint="a" * 64,
            embedding_model="embedding-v1",
        )


async def test_index_stats_preserve_partial_store_availability():
    handler = IndexStatsQueryHandler(
        MetadataReader(),
        Store(error=RuntimeError("milvus unavailable")),
        Store(IndexStoreStats(True, False, "paths", error_type="NotInitialized")),
        Cache(),
        RetrievalRuntimeProfile(exact_enabled=False),
        Profile(),
    )

    result = await handler.handle(IndexStatsQuery())

    assert result.metadata.blobs_total == 4
    assert result.dense.available is False
    assert result.dense.error_type == "RuntimeError"
    assert result.path.collection_name == "paths"
    assert result.path.error_type == "NotInitialized"
    assert result.query_cache.hits == 3
    assert result.runtime.exact_enabled is False
    assert result.runtime.llm_rerank_policy == "adaptive"
    assert result.profile.state == "compatible"
    assert result.profile.embedding_model == "embedding-v1"


async def test_index_stats_marks_missing_path_provider_disabled():
    handler = IndexStatsQueryHandler(
        MetadataReader(),
        Store(IndexStoreStats(True, True, "dense", True, 10)),
        None,
        Cache(),
        RetrievalRuntimeProfile(),
        Profile(),
    )

    result = await handler.handle(IndexStatsQuery())

    assert result.path.enabled is False
    assert result.path.available is False

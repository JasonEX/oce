"""Compose metadata, vector-store, and query-cache operational state."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from oce.application.messages import Query
from oce.shared.index_stats import (
    IndexStats,
    IndexStoreStats,
    IndexStoreStatsProvider,
    MetadataIndexStatsReader,
    QueryCacheStatsProvider,
    RetrievalRuntimeProfile,
)


@dataclass(frozen=True)
class IndexStatsQuery(Query):
    pass


class IndexStatsQueryHandler:
    def __init__(
        self,
        metadata_reader: MetadataIndexStatsReader,
        dense: IndexStoreStatsProvider,
        path: IndexStoreStatsProvider | None,
        query_cache: QueryCacheStatsProvider,
        runtime: RetrievalRuntimeProfile,
    ) -> None:
        self._metadata_reader = metadata_reader
        self._dense = dense
        self._path = path
        self._query_cache = query_cache
        self._runtime = runtime

    async def handle(self, _query: IndexStatsQuery) -> IndexStats:
        metadata = await self._metadata_reader.read()
        dense, path, query_cache = await asyncio.gather(
            self._safe_store_stats(self._dense),
            self._safe_store_stats(self._path),
            self._query_cache.query_cache_stats(),
        )
        return IndexStats(
            metadata=metadata,
            dense=dense,
            path=path,
            query_cache=query_cache,
            runtime=self._runtime,
        )

    @staticmethod
    async def _safe_store_stats(
        provider: IndexStoreStatsProvider | None,
    ) -> IndexStoreStats:
        if provider is None:
            return IndexStoreStats(enabled=False, available=False)
        try:
            return await provider.index_stats()
        except Exception as exc:
            return IndexStoreStats(
                enabled=True,
                available=False,
                error_type=type(exc).__name__,
            )

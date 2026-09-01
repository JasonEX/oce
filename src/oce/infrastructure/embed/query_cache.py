"""Bounded process-local cache for query vectors, never document vectors."""

from __future__ import annotations

import asyncio
import hashlib
from collections import OrderedDict
from collections.abc import Callable
from time import monotonic

from oce.domain.services.embedder import Embedder
from oce.shared.index_stats import QueryCacheStats


class QueryCachingEmbedder:
    """Cache query vectors without retaining query text or retrieval results."""

    def __init__(
        self,
        delegate: Embedder,
        *,
        max_entries: int,
        ttl_seconds: float,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if max_entries < 0 or ttl_seconds < 0:
            raise ValueError("Query cache limits must not be negative")
        self._delegate = delegate
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._cache: OrderedDict[bytes, tuple[float, tuple[float, ...]]] = OrderedDict()
        self._lock = asyncio.Lock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._invalidations = 0

    @property
    def enabled(self) -> bool:
        return self._max_entries > 0 and self._ttl_seconds > 0

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._delegate.embed_documents(texts)

    async def embed_query(self, text: str) -> list[float]:
        if not self.enabled:
            return await self._delegate.embed_query(text)

        key = hashlib.sha256(text.encode("utf-8")).digest()
        now = self._clock()
        async with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached[0] > now:
                self._cache.move_to_end(key)
                self._hits += 1
                return list(cached[1])
            if cached is not None:
                del self._cache[key]
            self._misses += 1

        vector = await self._delegate.embed_query(text)
        async with self._lock:
            self._cache[key] = (
                self._clock() + self._ttl_seconds,
                tuple(vector),
            )
            self._cache.move_to_end(key)
            while len(self._cache) > self._max_entries:
                self._cache.popitem(last=False)
                self._evictions += 1
        return vector

    async def clear_query_cache(self) -> None:
        async with self._lock:
            self._cache.clear()
            self._invalidations += 1

    async def query_cache_stats(self) -> QueryCacheStats:
        async with self._lock:
            self._prune_expired(self._clock())
            return QueryCacheStats(
                enabled=self.enabled,
                entries=len(self._cache),
                max_entries=self._max_entries,
                ttl_seconds=self._ttl_seconds,
                hits=self._hits,
                misses=self._misses,
                evictions=self._evictions,
                invalidations=self._invalidations,
            )

    def _prune_expired(self, now: float) -> None:
        expired = [
            key for key, (expires_at, _) in self._cache.items() if expires_at <= now
        ]
        for key in expired:
            del self._cache[key]

    async def close(self) -> None:
        await self.clear_query_cache()
        close = getattr(self._delegate, "close", None)
        if close is not None:
            await close()

"""Query vector caching is bounded and never retains source queries."""

from oce.infrastructure.embed.query_cache import QueryCachingEmbedder


class FakeEmbedder:
    def __init__(self) -> None:
        self.query_calls: list[str] = []
        self.document_calls: list[list[str]] = []
        self.closed = False

    async def embed_query(self, text: str) -> list[float]:
        self.query_calls.append(text)
        return [float(len(text))]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls.append(texts)
        return [[float(len(text))] for text in texts]

    async def close(self) -> None:
        self.closed = True


def _cache(delegate, clock, *, max_entries=2, ttl_seconds=10):
    return QueryCachingEmbedder(
        delegate,
        max_entries=max_entries,
        ttl_seconds=ttl_seconds,
        clock=lambda: clock[0],
    )


async def test_repeated_query_uses_hashed_lru_entry():
    delegate = FakeEmbedder()
    cache = _cache(delegate, [0.0])

    first = await cache.embed_query("private source question")
    first.append(999.0)
    second = await cache.embed_query("private source question")
    stats = await cache.query_cache_stats()

    assert second == [23.0]
    assert delegate.query_calls == ["private source question"]
    assert all(isinstance(key, bytes) for key in cache._cache)
    assert stats.hits == 1
    assert stats.misses == 1


async def test_expiry_and_lru_eviction_force_new_embedding():
    delegate = FakeEmbedder()
    clock = [0.0]
    cache = _cache(delegate, clock, max_entries=1)

    await cache.embed_query("first")
    await cache.embed_query("second")
    await cache.embed_query("first")
    clock[0] = 20.0
    await cache.embed_query("first")
    stats = await cache.query_cache_stats()

    assert delegate.query_calls == ["first", "second", "first", "first"]
    assert stats.evictions == 2
    assert stats.misses == 4


async def test_documents_bypass_cache_and_clear_invalidates_queries():
    delegate = FakeEmbedder()
    cache = _cache(delegate, [0.0])

    await cache.embed_query("query")
    await cache.embed_documents(["source", "source"])
    await cache.embed_documents(["source", "source"])
    await cache.clear_query_cache()
    await cache.embed_query("query")
    stats = await cache.query_cache_stats()

    assert len(delegate.document_calls) == 2
    assert delegate.query_calls == ["query", "query"]
    assert stats.invalidations == 1


async def test_zero_capacity_disables_cache_and_close_delegates():
    delegate = FakeEmbedder()
    cache = _cache(delegate, [0.0], max_entries=0)

    await cache.embed_query("query")
    await cache.embed_query("query")
    await cache.close()
    stats = await cache.query_cache_stats()

    assert delegate.query_calls == ["query", "query"]
    assert delegate.closed is True
    assert stats.enabled is False
    assert stats.entries == 0

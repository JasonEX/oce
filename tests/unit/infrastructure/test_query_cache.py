"""Query vector caching is bounded and never retains source queries."""

import asyncio
import gc

import pytest

from oce.infrastructure.embed.query_cache import QueryCachingEmbedder
from tests.fakes.embedding import ControlledEmbedder, LengthEmbedder


def _cache(delegate, clock, *, max_entries=2, ttl_seconds=10):
    return QueryCachingEmbedder(
        delegate,
        max_entries=max_entries,
        ttl_seconds=ttl_seconds,
        clock=lambda: clock[0],
    )


async def test_repeated_query_uses_hashed_lru_entry():
    delegate = LengthEmbedder()
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
    delegate = LengthEmbedder()
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
    delegate = LengthEmbedder()
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
    delegate = LengthEmbedder()
    cache = _cache(delegate, [0.0], max_entries=0)

    await cache.embed_query("query")
    await cache.embed_query("query")
    await cache.close()
    stats = await cache.query_cache_stats()

    assert delegate.query_calls == ["query", "query"]
    assert delegate.closed is True
    assert stats.enabled is False
    assert stats.entries == 0


async def test_concurrent_queries_share_one_call_and_return_independent_vectors() -> (
    None
):
    delegate = ControlledEmbedder()
    cache = _cache(delegate, [0.0])
    queries = [asyncio.create_task(cache.embed_query("same query")) for _ in range(12)]
    async with asyncio.timeout(1):
        response = await delegate.requests.get()
    await asyncio.sleep(0)
    assert delegate.query_calls == ["same query"]

    response.set_result([3.0])
    vectors = await asyncio.gather(*queries)
    vectors[0].append(999.0)
    assert vectors[1:] == [[3.0]] * 11
    assert await cache.embed_query("same query") == [3.0]
    stats = await cache.query_cache_stats()
    assert stats.misses == 12
    assert stats.hits == 1
    assert cache._pending == {}


async def test_failed_shared_query_is_not_cached_and_can_be_retried() -> None:
    delegate = ControlledEmbedder()
    cache = _cache(delegate, [0.0])
    queries = [asyncio.create_task(cache.embed_query("query")) for _ in range(3)]
    async with asyncio.timeout(1):
        response = await delegate.requests.get()
    await asyncio.sleep(0)
    response.set_exception(RuntimeError("provider failed"))
    errors = await asyncio.gather(*queries, return_exceptions=True)
    assert all(isinstance(error, RuntimeError) for error in errors)
    assert delegate.query_calls == ["query"]
    assert (await cache.query_cache_stats()).entries == 0

    retry = asyncio.create_task(cache.embed_query("query"))
    async with asyncio.timeout(1):
        response = await delegate.requests.get()
    response.set_result([2.0])
    assert await retry == [2.0]
    assert delegate.query_calls == ["query", "query"]


async def test_cancelling_one_waiter_preserves_shared_request_and_other_waiters() -> (
    None
):
    delegate = ControlledEmbedder()
    cache = _cache(delegate, [0.0])
    first = asyncio.create_task(cache.embed_query("query"))
    second = asyncio.create_task(cache.embed_query("query"))
    async with asyncio.timeout(1):
        response = await delegate.requests.get()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not response.cancelled()

    response.set_result([4.0])
    assert await second == [4.0]
    assert await cache.embed_query("query") == [4.0]
    assert delegate.query_calls == ["query"]
    assert delegate.cancelled_calls == 0


async def test_released_request_failure_is_consumed_and_releases_admission() -> None:
    delegate = ControlledEmbedder()
    cache = _cache(delegate, [0.0], max_entries=1)
    loop = asyncio.get_running_loop()
    unhandled: list[dict] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    try:
        query = asyncio.create_task(cache.embed_query("query"))
        async with asyncio.timeout(1):
            response = await delegate.requests.get()
        query.cancel()
        with pytest.raises(asyncio.CancelledError):
            await query
        response.set_exception(RuntimeError("provider failed after release"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        gc.collect()
        assert cache._pending == {}
        assert unhandled == []

        retry = asyncio.create_task(cache.embed_query("query"))
        async with asyncio.timeout(1):
            response = await delegate.requests.get()
        response.set_result([5.0])
        assert await retry == [5.0]
    finally:
        loop.set_exception_handler(previous_handler)


async def test_clear_separates_generations_without_cancelling_old_request() -> None:
    delegate = ControlledEmbedder()
    cache = _cache(delegate, [0.0])
    old = asyncio.create_task(cache.embed_query("query"))
    async with asyncio.timeout(1):
        old_response = await delegate.requests.get()
    await cache.clear_query_cache()
    new = asyncio.create_task(cache.embed_query("query"))
    async with asyncio.timeout(1):
        new_response = await delegate.requests.get()
    assert not old_response.cancelled()

    new_response.set_result([2.0])
    assert await new == [2.0]
    old_response.set_result([1.0])
    assert await old == [1.0]
    assert await cache.embed_query("query") == [2.0]
    assert delegate.query_calls == ["query", "query"]
    assert delegate.cancelled_calls == 0
    assert (await cache.query_cache_stats()).invalidations == 1


async def test_inflight_admission_is_bounded_across_clear_and_waiter_cancellation() -> (
    None
):
    delegate = ControlledEmbedder()
    cache = _cache(delegate, [0.0], max_entries=1)
    old = asyncio.create_task(cache.embed_query("query"))
    async with asyncio.timeout(1):
        old_response = await delegate.requests.get()
    await cache.clear_query_cache()
    new = asyncio.create_task(cache.embed_query("query"))
    cancelled = asyncio.create_task(cache.embed_query("other query"))
    await asyncio.sleep(0)
    assert len(cache._pending) == 1
    assert delegate.query_calls == ["query"]
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled

    old_response.set_result([1.0])
    assert await old == [1.0]
    async with asyncio.timeout(1):
        new_response = await delegate.requests.get()
    assert len(cache._pending) == 1
    assert delegate.query_calls == ["query", "query"]
    new_response.set_result([2.0])
    assert await new == [2.0]
    assert await cache.embed_query("query") == [2.0]


async def test_close_drains_released_request_before_closing_delegate() -> None:
    delegate = ControlledEmbedder()
    cache = _cache(delegate, [0.0])
    query = asyncio.create_task(cache.embed_query("query"))
    async with asyncio.timeout(1):
        response = await delegate.requests.get()
    query.cancel()
    with pytest.raises(asyncio.CancelledError):
        await query
    close = asyncio.create_task(cache.close())
    await asyncio.sleep(0)
    assert not delegate.closed
    assert not response.cancelled()

    response.set_result([1.0])
    await close
    assert delegate.closed
    assert (await cache.query_cache_stats()).entries == 0
    with pytest.raises(RuntimeError, match="closed"):
        await cache.embed_query("query")

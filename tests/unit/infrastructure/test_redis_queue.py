from unittest.mock import AsyncMock

import pytest

from oce.infrastructure.queue.redis_queue import RedisQueue


async def test_close_releases_redis_pool():
    redis = AsyncMock()
    queue = RedisQueue(redis, "oce:test")

    await queue.close()

    redis.aclose.assert_awaited_once_with()


async def test_dequeue_many_blocks_for_first_item_then_drains_available_items():
    redis = AsyncMock()
    redis.brpoplpush.return_value = "first"
    redis.rpoplpush.side_effect = ["second", "third"]
    queue = RedisQueue(redis, "oce:test")

    result = await queue.dequeue_many(3, timeout=7)

    assert result == ["first", "second", "third"]
    redis.brpoplpush.assert_awaited_once_with(
        "oce:test",
        "oce:test:processing",
        timeout=7,
    )
    assert redis.rpoplpush.await_count == 2


async def test_dequeue_many_stops_when_backlog_is_empty():
    redis = AsyncMock()
    redis.brpoplpush.return_value = "first"
    redis.rpoplpush.return_value = None
    queue = RedisQueue(redis, "oce:test")

    result = await queue.dequeue_many(4)

    assert result == ["first"]
    redis.rpoplpush.assert_awaited_once_with(
        "oce:test",
        "oce:test:processing",
    )


async def test_dequeue_many_returns_empty_after_timeout_without_draining():
    redis = AsyncMock()
    redis.brpoplpush.return_value = None
    queue = RedisQueue(redis, "oce:test")

    result = await queue.dequeue_many(4)

    assert result == []
    redis.rpoplpush.assert_not_awaited()


async def test_dequeue_many_rejects_non_positive_batch_size():
    redis = AsyncMock()
    queue = RedisQueue(redis, "oce:test")

    with pytest.raises(ValueError, match="max_items must be positive"):
        await queue.dequeue_many(0)

    redis.brpoplpush.assert_not_awaited()

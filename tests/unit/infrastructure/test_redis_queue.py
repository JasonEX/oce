from unittest.mock import AsyncMock

import pytest

from oce.infrastructure.queue.redis_queue import (
    _ACK_LUA,
    _RECOVER_PROCESSING_LUA,
    _RETAIN_LUA,
    RedisQueue,
)


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


@pytest.mark.parametrize("operation", ["ack", "fail"])
async def test_delivery_release_is_one_atomic_redis_operation(operation):
    redis = AsyncMock()
    queue = RedisQueue(redis, "oce:test")

    await getattr(queue, operation)("blob")

    redis.eval.assert_awaited_once_with(
        _ACK_LUA, 2, "oce:test:processing", "oce:test:pending", "blob"
    )
    redis.lrem.assert_not_awaited()
    redis.srem.assert_not_awaited()


async def test_retain_repairs_sentinel_even_when_lists_need_no_removal():
    redis = AsyncMock()
    redis.eval.return_value = 0
    queue = RedisQueue(redis, "oce:test")

    assert await queue.retain({"live", "orphan-sentinel"}) == 0

    redis.eval.assert_awaited_once_with(
        _RETAIN_LUA,
        3,
        "oce:test",
        "oce:test:processing",
        "oce:test:pending",
        "live",
        "orphan-sentinel",
    )
    redis.pipeline.assert_not_called()


async def test_processing_recovery_and_sentinel_rebuild_are_one_transition():
    redis = AsyncMock()
    redis.eval.return_value = 2
    queue = RedisQueue(redis, "oce:test")

    assert await queue.recover_processing() == 2

    redis.eval.assert_awaited_once_with(
        _RECOVER_PROCESSING_LUA,
        3,
        "oce:test:processing",
        "oce:test",
        "oce:test:pending",
    )
    redis.pipeline.assert_not_called()

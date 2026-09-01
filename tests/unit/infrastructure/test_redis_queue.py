from unittest.mock import AsyncMock

from oce.infrastructure.queue.redis_queue import RedisQueue


async def test_close_releases_redis_pool():
    redis = AsyncMock()
    queue = RedisQueue(redis, "oce:test")

    await queue.close()

    redis.aclose.assert_awaited_once_with()

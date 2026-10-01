"""Reliable Redis task queue without a dead-letter list.

Keys: ``{name}`` is the main list (LPUSH in, BRPOPLPUSH out);
``{name}:processing`` holds messages a worker took until it acks them, so a
crash leaves them recoverable; ``{name}:pending`` is the in-flight sentinel
set over both lists, giving O(1) deduplication on enqueue.

BRPOPLPUSH moves a message from the main list to processing atomically, so a
worker crash loses nothing; only an ack removes it. On failure ``fail``
clears the in-flight state and the worker decides from the database retry
count whether to enqueue again.

A client re-uploading one file enqueues it every time; a plain LPUSH once
grew to 149K messages for 5K unready blobs. Enqueue runs a Lua script that
adds to the sentinel set first and pushes only when the add was new, so a
blob is in flight at most once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from redis.asyncio import Redis

# SADD the sentinel first; LPUSH only when it was new. Returns 1 when
# enqueued, 0 when already in flight.
_ENQUEUE_DEDUP_LUA = """
local added = redis.call('SADD', KEYS[1], ARGV[1])
if added == 1 then
    redis.call('LPUSH', KEYS[2], ARGV[1])
end
return added
"""

_ACK_LUA = """
redis.call('LREM', KEYS[1], 1, ARGV[1])
redis.call('SREM', KEYS[2], ARGV[1])
return 1
"""

_RECOVER_PROCESSING_LUA = """
local recovered = redis.call('LLEN', KEYS[1])
while redis.call('RPOPLPUSH', KEYS[1], KEYS[2]) do end
redis.call('DEL', KEYS[3])
for _, name in ipairs(redis.call('LRANGE', KEYS[2], 0, -1)) do
    redis.call('SADD', KEYS[3], name)
end
return recovered
"""

_RETAIN_LUA = """
local allowed = {}
for _, name in ipairs(ARGV) do allowed[name] = true end
local surviving = {}
local removed = 0
for i = 1, 2 do
    local items = redis.call('LRANGE', KEYS[i], 0, -1)
    redis.call('DEL', KEYS[i])
    for _, name in ipairs(items) do
        if allowed[name] then
            redis.call('RPUSH', KEYS[i], name)
            surviving[name] = true
        else
            removed = removed + 1
        end
    end
end
redis.call('DEL', KEYS[3])
for name, _ in pairs(surviving) do redis.call('SADD', KEYS[3], name) end
return removed
"""


class RedisQueue:
    """Queue over an injected redis client built with ``decode_responses=True``."""

    def __init__(self, redis: Redis, name: str) -> None:
        self._redis = redis
        self._name = name
        self._processing = f"{name}:processing"
        self._pending = f"{name}:pending"  # in-flight sentinel set

    async def close(self) -> None:
        """Release the Redis connection pool owned by this queue."""
        await self._redis.aclose()

    async def enqueue(self, blob_name: str) -> None:
        """Enqueue a blob unless it is already in flight."""
        await self._redis.eval(
            _ENQUEUE_DEDUP_LUA,
            2,
            self._pending,
            self._name,
            blob_name,
        )

    async def dequeue_many(
        self,
        max_items: int,
        timeout: int = 5,
    ) -> list[str]:
        """Block for the first message, then fill a bounded batch without blocking."""
        if max_items < 1:
            raise ValueError("max_items must be positive")

        # The sentinel set is untouched: a message in processing is still in flight.
        first = await self._redis.brpoplpush(
            self._name,
            self._processing,
            timeout=timeout,
        )
        if first is None:
            return []

        # The client is built with ``decode_responses=True``; ``str`` only
        # narrows the driver's ``bytes | str`` return type.
        items = [str(first)]
        while len(items) < max_items:
            try:
                blob_name = await self._redis.rpoplpush(self._name, self._processing)
            except Exception as exc:
                # Confirmed claims already have an owner. A response lost after
                # Redis moved another item is recovered by restart/maintenance.
                logger.warning(
                    "Queue batch fill failed; processing {} confirmed claims: {}",
                    len(items),
                    type(exc).__name__,
                )
                break
            if blob_name is None:
                break
            items.append(str(blob_name))
        return items

    async def ack(self, blob_name: str) -> None:
        """Drop the blob from processing and from the sentinel set."""
        await self._redis.eval(_ACK_LUA, 2, self._processing, self._pending, blob_name)

    async def fail(self, blob_name: str) -> None:
        """Failure clears the in-flight state exactly like completion; the worker decides on a retry."""
        await self.ack(blob_name)

    async def size(self) -> int:
        """Messages waiting in the main list."""
        return await self._redis.llen(self._name)

    async def recover_processing(self) -> int:
        """Move what a crashed run left in processing back to the main list.

        The sentinel set is rebuilt from both lists afterwards so it covers
        every blob in flight, including data written by older versions.
        """
        # Producers remain live during maintenance: the sentinel rebuild must
        # share one Redis transition with the list moves.
        return int(
            await self._redis.eval(
                _RECOVER_PROCESSING_LUA,
                3,
                self._processing,
                self._name,
                self._pending,
            )
        )

    async def inflight_set(self) -> set[str]:
        """Blob names in flight, read from the sentinel set."""
        return {str(item) for item in await self._redis.smembers(self._pending)}

    async def inflight_count(self) -> int:
        return int(await self._redis.scard(self._pending))

    async def purge(self) -> int:
        """Delete all three keys; returns how many messages were in the two lists.

        The sentinel set goes too, or those blobs could never be enqueued again.
        """
        pipe = self._redis.pipeline()
        pipe.llen(self._name)
        pipe.llen(self._processing)
        main_len, processing_len = await pipe.execute()

        pipe = self._redis.pipeline()
        pipe.delete(self._name)
        pipe.delete(self._processing)
        pipe.delete(self._pending)
        await pipe.execute()
        return int(main_len) + int(processing_len)

    async def retain(self, blob_names: set[str]) -> int:
        """Rebuild the lists and the sentinel keeping only ``blob_names``; returns the removed count.

        Filtering and sentinel rebuild share one Redis transition so producers
        can keep enqueueing during maintenance. The worker must be stopped:
        a processing delivery removed here must have no active batch owner.
        """
        return int(
            await self._redis.eval(
                _RETAIN_LUA,
                3,
                self._name,
                self._processing,
                self._pending,
                *sorted(blob_names),
            )
        )

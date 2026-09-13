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
            blob_name = await self._redis.rpoplpush(self._name, self._processing)
            if blob_name is None:
                break
            items.append(str(blob_name))
        return items

    async def ack(self, blob_name: str) -> None:
        """Drop the blob from processing and from the sentinel set."""
        await self._redis.lrem(self._processing, 1, blob_name)
        await self._redis.srem(self._pending, blob_name)

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
        n = 0
        while True:
            blob_name = await self._redis.rpoplpush(self._processing, self._name)
            if blob_name is None:
                break
            n += 1

        # Rebuild the sentinel from both lists (processing should be empty by
        # now; reading it costs one LRANGE).
        pipe = self._redis.pipeline()
        pipe.lrange(self._name, 0, -1)
        pipe.lrange(self._processing, 0, -1)
        main, processing = await pipe.execute()
        all_inflight = set(main) | set(processing)
        if all_inflight:
            # Replace the set in one pipeline: DELETE then SADD.
            pipe = self._redis.pipeline()
            pipe.delete(self._pending)
            pipe.sadd(self._pending, *all_inflight)
            await pipe.execute()
        else:
            await self._redis.delete(self._pending)
        return n

    async def inflight_set(self) -> set[str]:
        """Blob names in flight, read from the sentinel set."""
        return {str(item) for item in await self._redis.smembers(self._pending)}

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

        LREM per entry is O(n*m) on tens of thousands of messages, so both
        lists are read, filtered in memory and rewritten. The queue is empty
        between DELETE and RPUSH, which is why the worker must be stopped.
        """
        pipe = self._redis.pipeline()
        pipe.lrange(self._name, 0, -1)
        pipe.lrange(self._processing, 0, -1)
        main_items, processing_items = await pipe.execute()

        # RPUSH keeps the original order: BRPOPLPUSH pops from the tail, so
        # the head of LRANGE is the end consumed last.
        kept_main = [item for item in main_items if item in blob_names]
        kept_processing = [item for item in processing_items if item in blob_names]
        removed = (len(main_items) - len(kept_main)) + (
            len(processing_items) - len(kept_processing)
        )
        if removed == 0:
            return 0

        pipe = self._redis.pipeline()
        pipe.delete(self._name)
        if kept_main:
            pipe.rpush(self._name, *kept_main)
        pipe.delete(self._processing)
        if kept_processing:
            pipe.rpush(self._processing, *kept_processing)
        pipe.delete(self._pending)
        surviving = set(kept_main) | set(kept_processing)
        if surviving:
            pipe.sadd(self._pending, *surviving)
        await pipe.execute()
        return removed

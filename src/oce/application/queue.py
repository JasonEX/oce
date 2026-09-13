"""The task queue port; infrastructure provides the implementation."""

from __future__ import annotations

from typing import Protocol


class Queue(Protocol):
    """Reliable blob queue with deduplication."""

    async def enqueue(self, blob_name: str) -> None:
        """Enqueue a blob; a blob already in flight is not enqueued twice."""
        ...

    async def dequeue_many(
        self,
        max_items: int,
        timeout: int = 5,
    ) -> list[str]:
        """Block for the first message, then fill a bounded batch without blocking."""
        ...

    async def ack(self, blob_name: str) -> None:
        """Acknowledge completion: drop the blob from the processing list."""
        ...

    async def fail(self, blob_name: str) -> None:
        """Report failure: drop the in-flight state; the retry decision is the worker's."""
        ...

    async def size(self) -> int:
        """Messages waiting in the main queue."""
        ...

    async def recover_processing(self) -> int:
        """Move messages a crashed worker left in processing back to the queue."""
        ...

    async def inflight_set(self) -> set[str]:
        """Blob names in the main queue or in processing."""
        ...

    async def purge(self) -> int:
        """Drop every queue key; returns how many messages were in flight.

        For when the queue and the database no longer correspond (the blobs
        were deleted and rebuilt wholesale). The caller ensures no worker is
        consuming.
        """
        ...

    async def retain(self, blob_names: set[str]) -> int:
        """Keep only the given blobs; returns how many entries were removed.

        The database is the authority on pending work: a message for a blob
        that vanished or completed costs a worker an empty run, and a leftover
        in the pending sentinel set blocks that blob from being enqueued again.
        """
        ...

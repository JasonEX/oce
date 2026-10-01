"""In-memory delivery projection with the production queue's dedup semantics."""

from __future__ import annotations

import asyncio


class FakeQueue:
    def __init__(
        self, main: list[str] | None = None, processing: list[str] | None = None
    ) -> None:
        self.main = list(main or [])
        self.processing = list(processing or [])
        self.pending = set(self.main) | set(self.processing)
        self.acked: list[str] = []
        self.failed: list[str] = []
        self._condition = asyncio.Condition()

    async def enqueue(self, blob_name: str) -> None:
        async with self._condition:
            if blob_name not in self.pending:
                self.pending.add(blob_name)
                self.main.insert(0, blob_name)
                self._condition.notify()

    async def dequeue_many(self, max_items: int, timeout: int = 5) -> list[str]:
        async with self._condition:
            try:
                await asyncio.wait_for(
                    self._condition.wait_for(lambda: bool(self.main)), timeout
                )
            except TimeoutError:
                return []
            names = []
            while self.main and len(names) < max_items:
                name = self.main.pop()
                self.processing.insert(0, name)
                names.append(name)
            return names

    async def ack(self, blob_name: str) -> None:
        if blob_name in self.processing:
            self.processing.remove(blob_name)
        self.pending.discard(blob_name)
        self.acked.append(blob_name)

    async def fail(self, blob_name: str) -> None:
        await self.ack(blob_name)
        self.failed.append(blob_name)

    async def size(self) -> int:
        return len(self.main)

    async def close(self) -> None:
        pass

    async def inflight_set(self) -> set[str]:
        return set(self.pending)

    async def recover_processing(self) -> int:
        recovered = len(self.processing)
        while self.processing:
            self.main.insert(0, self.processing.pop())
        self.pending = set(self.main)
        return recovered

    async def purge(self) -> int:
        removed = len(self.main) + len(self.processing)
        self.main.clear()
        self.processing.clear()
        self.pending.clear()
        return removed

    async def retain(self, blob_names: set[str]) -> int:
        kept_main = [item for item in self.main if item in blob_names]
        kept_processing = [item for item in self.processing if item in blob_names]
        removed = (len(self.main) - len(kept_main)) + (
            len(self.processing) - len(kept_processing)
        )
        self.main = kept_main
        self.processing = kept_processing
        self.pending = set(kept_main) | set(kept_processing)
        return removed

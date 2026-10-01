"""Garbage collection of expired data; dry run by default.

An expired chain (``updated_at`` older than ``now - ttl_days``) is removed as
a checkpoint grouping without touching its blobs. An expired blob
(``last_seen`` older than the TTL, referenced by no chain, not in the queue's
in-flight set) is removed through ``DeleteBlobsCommand`` together with its
vectors and path documents. Blobs referenced by a chain deleted in this very
run wait for the next run, so a live chain never points at a deleted index;
in-flight blobs are skipped so nothing being embedded disappears.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass

from oce.application.commands.ingest import (
    DeleteBlobsCommand,
    DeleteBlobsCommandHandler,
)
from oce.application.queue import Queue
from oce.application.uow import UnitOfWorkFactory


@dataclass(frozen=True)
class GcCommand:
    ttl_days: int = 30
    dry_run: bool = True
    limit: int = 1000


@dataclass(frozen=True)
class GcResult:
    dry_run: bool
    ttl_days: int
    expired_chains: int
    expired_blobs: int
    deletable_blobs: int
    skipped_inflight: int
    deleted_chains: int
    deleted_blobs: int


class GcCommandHandler:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        delete_blobs: DeleteBlobsCommandHandler,
        queue: Queue | None = None,
        maintenance: Callable[[], AbstractAsyncContextManager[None]] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._delete_blobs = delete_blobs
        self._queue = queue
        self._maintenance = maintenance or nullcontext

    async def handle(self, command: GcCommand) -> GcResult:
        if command.dry_run:
            return await self._collect(command)
        async with self._maintenance():
            return await self._collect(command)

    async def _collect(self, command: GcCommand) -> GcResult:
        async with self._uow_factory() as uow:
            expired_chains = list(await uow.chains.find_expired(command.ttl_days))
            expired_blobs = list(
                await uow.blobs.find_expired(command.ttl_days, batch_size=command.limit)
            )

        inflight: set[str] = (
            await self._queue.inflight_set() if self._queue is not None else set()
        )
        deletable = [name for name in expired_blobs if name not in inflight]
        skipped = len(expired_blobs) - len(deletable)

        if command.dry_run:
            return GcResult(
                dry_run=True,
                ttl_days=command.ttl_days,
                expired_chains=len(expired_chains),
                expired_blobs=len(expired_blobs),
                deletable_blobs=len(deletable),
                skipped_inflight=skipped,
                deleted_chains=0,
                deleted_blobs=0,
            )

        async with self._uow_factory() as uow:
            for chain_id in expired_chains:
                await uow.chains.delete(chain_id)
            await uow.commit()

        deleted_blobs = 0
        if deletable:
            deleted_blobs = await self._delete_blobs.handle(
                DeleteBlobsCommand(tuple(deletable), ttl_days=command.ttl_days)
            )

        return GcResult(
            dry_run=False,
            ttl_days=command.ttl_days,
            expired_chains=len(expired_chains),
            expired_blobs=len(expired_blobs),
            deletable_blobs=len(deletable),
            skipped_inflight=skipped,
            deleted_chains=len(expired_chains),
            deleted_blobs=deleted_blobs,
        )

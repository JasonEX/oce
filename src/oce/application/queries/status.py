"""Blob status and retrieval scope queries."""

from __future__ import annotations

from dataclasses import dataclass

from oce.application.messages import Query
from oce.application.uow import UnitOfWorkFactory
from oce.domain.chain.chain import Chain
from oce.domain.repositories import BlobRepository
from oce.domain.services.search import SearchScope
from oce.shared.errors import (
    InvalidCheckpointTokenError,
    NeedsResetError,
    ScopeRequiredError,
)


@dataclass(frozen=True)
class FindMissingQuery(Query):
    blob_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class FindMissingResult:
    unknown: tuple[str, ...] = ()
    nonindexed: tuple[str, ...] = ()


async def _classify(
    blob_repo: BlobRepository, blob_names: tuple[str, ...]
) -> FindMissingResult:
    if not blob_names:
        return FindMissingResult()
    exists = await blob_repo.exists_many(blob_names)
    unknown = tuple(name for name in blob_names if not exists.get(name, False))
    existing = [name for name in blob_names if exists.get(name, False)]
    blobs = await blob_repo.get_many(existing)
    nonindexed = tuple(name for name in existing if not blobs[name].is_ready())
    return FindMissingResult(unknown, nonindexed)


class FindMissingQueryHandler:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def handle(self, query: FindMissingQuery) -> FindMissingResult:
        async with self._uow_factory() as uow:
            return await _classify(uow.blobs, query.blob_names)


@dataclass(frozen=True)
class BlobStatusQuery(Query):
    blob_names: tuple[str, ...] = ()
    checkpoint_id: str | None = None


@dataclass(frozen=True)
class BlobStatusResult:
    unknown: tuple[str, ...] = ()
    nonindexed: tuple[str, ...] = ()
    checkpoint_not_found: bool = False


class BlobStatusQueryHandler:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def handle(self, query: BlobStatusQuery) -> BlobStatusResult:
        async with self._uow_factory() as uow:
            missing = await _classify(uow.blobs, query.blob_names)
            checkpoint_not_found = False
            if query.checkpoint_id:
                parsed = Chain.parse_checkpoint_token(query.checkpoint_id)
                checkpoint_not_found = not (
                    parsed is not None and await uow.chains.exists(parsed[0], parsed[1])
                )
        return BlobStatusResult(
            missing.unknown,
            missing.nonindexed,
            checkpoint_not_found,
        )


@dataclass(frozen=True)
class ResolveScopeQuery(Query):
    checkpoint_id: str | None = None
    added_blobs: tuple[str, ...] = ()
    deleted_blobs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolveScopeResult:
    scope: SearchScope


class ResolveScopeQueryHandler:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def handle(self, query: ResolveScopeQuery) -> ResolveScopeResult:
        """Resolve ``(checkpoint members | added) - deleted`` into the retrieval scope.

        Whole-index retrieval is disabled: the client must declare a working
        set through ``checkpoint_id`` or ``added_blobs``; ``deleted_blobs``
        only subtracts and declares nothing. An invalid checkpoint (malformed,
        unknown chain, stale version) is an error rather than a silently
        changed scope. The result is always a frozenset; an empty one means an
        empty working set and an empty answer, never the whole index.
        """
        base: set[str] = set()
        chain_id: str | None = None
        chain_version: int | None = None
        if query.checkpoint_id:
            parsed = Chain.parse_checkpoint_token(query.checkpoint_id)
            if parsed is None:
                raise InvalidCheckpointTokenError(query.checkpoint_id)
            chain_id, expected_version = parsed
            async with self._uow_factory() as uow:
                chain = await uow.chains.get(chain_id)
                if chain is None or chain.version != expected_version:
                    raise NeedsResetError(
                        "checkpoint chain not found or version outdated"
                    )
                base = set(chain.members)
                chain_version = chain.version
        elif not query.added_blobs:
            # Neither a checkpoint nor added_blobs: refuse whole-index retrieval.
            raise ScopeRequiredError()
        added = frozenset(query.added_blobs)
        deleted = frozenset(query.deleted_blobs)
        blob_names = frozenset((base | set(added)) - set(deleted))
        return ResolveScopeResult(
            SearchScope(
                blob_names=blob_names,
                chain_id=chain_id,
                chain_version=chain_version,
                added_blob_names=added,
                deleted_blob_names=deleted,
            )
        )

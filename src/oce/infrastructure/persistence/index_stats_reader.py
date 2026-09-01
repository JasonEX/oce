"""SQL metadata index counters for SQLite and PostgreSQL."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oce.infrastructure.persistence.models import (
    BlobChunkModel,
    BlobModel,
    BlobStagingModel,
    ChainMemberModel,
    ChainModel,
    ChunkModel,
    SymbolOccurrenceModel,
)
from oce.shared.index_stats import MetadataIndexStats


def _count(model, *predicates):
    return select(func.count()).select_from(model).where(*predicates).scalar_subquery()


class SqlMetadataIndexStatsReader:
    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def read(self) -> MetadataIndexStats:
        statement = select(
            _count(BlobModel),
            _count(BlobModel, BlobModel.status == "ready"),
            _count(BlobModel, BlobModel.status == "pending"),
            _count(BlobModel, BlobModel.status == "error"),
            _count(ChunkModel),
            _count(ChunkModel, ChunkModel.embedded.is_(True)),
            _count(BlobChunkModel),
            _count(SymbolOccurrenceModel),
            _count(ChainModel),
            _count(ChainMemberModel),
            _count(BlobStagingModel),
        )
        async with self._session_factory() as session:
            row = (await session.execute(statement)).one()
        return MetadataIndexStats(*(int(value or 0) for value in row))

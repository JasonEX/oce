"""SQL metadata index counters for SQLite and PostgreSQL."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError, OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from oce.infrastructure.persistence.lexical_index import (
    lexical_row_count_statement,
)
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
            lexical = await self._lexical_count(session)
        return MetadataIndexStats(
            *(int(value or 0) for value in row), lexical_documents=lexical
        )

    @staticmethod
    async def _lexical_count(session: AsyncSession) -> int:
        # Stats are observational: an unavailable auxiliary term table must not
        # hide the counts from the ordinary metadata tables.
        try:
            value = await session.scalar(lexical_row_count_statement())
        except (OperationalError, ProgrammingError, DBAPIError):
            await session.rollback()
            return 0
        return int(value or 0)

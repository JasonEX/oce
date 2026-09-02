"""Transactional SQL projection for structural symbol evidence."""

from __future__ import annotations

from collections.abc import Sequence

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from oce.domain.blob.blob import Blob
from oce.domain.chunk import Chunk
from oce.domain.services.symbols import SymbolProvider
from oce.infrastructure.persistence.dialect import upsert_insert
from oce.infrastructure.persistence.models import SymbolOccurrenceModel


class SqlSymbolProjection:
    """Materialize symbols once when an immutable blob receives its chunks."""

    def __init__(self, session: AsyncSession, provider: SymbolProvider) -> None:
        self._session = session
        self._provider = provider

    async def index(self, blob: Blob, chunks: Sequence[Chunk]) -> None:
        values = []
        for chunk in chunks:
            occurrences = self._provider.extract(
                content=chunk.content,
                language=blob.language,
                start_line=chunk.start_line,
                end_line=chunk.end_line,
            )
            values.extend(
                {
                    "identifier": occurrence.identifier,
                    "blob_name": blob.blob_name,
                    "content_hash": chunk.content_hash,
                    "kind": occurrence.kind,
                    "start_line": occurrence.start_line,
                    "end_line": occurrence.end_line,
                }
                for occurrence in occurrences
            )

        if not values:
            return
        stmt = upsert_insert(self._session)(SymbolOccurrenceModel).values(values)
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["identifier", "blob_name", "content_hash", "kind"]
        )
        await self._session.execute(stmt)
        logger.debug(
            "Indexed {} symbol occurrences for blob {}",
            len(values),
            blob.blob_name[:12],
        )

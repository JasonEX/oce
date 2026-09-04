"""Transactional SQL projection for structural symbol evidence."""

from __future__ import annotations

import bisect
from collections.abc import Sequence

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from oce.domain.blob.blob import Blob
from oce.domain.chunk import Chunk
from oce.domain.services.symbols import SymbolProvider
from oce.infrastructure.astchunk.symbol_provider import (
    PROSE_SUFFIXES,
    is_prose_language,
)
from oce.infrastructure.persistence.dialect import upsert_insert
from oce.infrastructure.persistence.models import SymbolOccurrenceModel


class SqlSymbolProjection:
    """Materialize symbols once when an immutable blob receives its chunks.

    The provider reads the whole file so declarations are found with their
    real spans; each occurrence is then attributed to the chunk whose lines
    contain its first line.
    """

    def __init__(self, session: AsyncSession, provider: SymbolProvider) -> None:
        self._session = session
        self._provider = provider

    async def index(self, blob: Blob, chunks: Sequence[Chunk], content: str) -> None:
        if not chunks:
            return
        if blob.path.lower().endswith(PROSE_SUFFIXES) or is_prose_language(
            blob.language
        ):
            # Documentation quotes code; the exact index must not read a
            # fenced example as a project definition.
            return
        ordered = sorted(chunks, key=lambda chunk: chunk.start_line)
        starts = [chunk.start_line for chunk in ordered]
        occurrences = self._provider.extract(content=content, language=blob.language)

        values = []
        seen: set[tuple[str, str, str]] = set()
        for occurrence in occurrences:
            index = bisect.bisect_right(starts, occurrence.start_line) - 1
            if index < 0:
                continue
            chunk = ordered[index]
            if occurrence.start_line > chunk.end_line:
                # The line fell in a gap the chunker dropped (over-long line).
                continue
            key = (occurrence.identifier, chunk.content_hash, occurrence.kind)
            if key in seen:
                continue
            seen.add(key)
            values.append(
                {
                    "identifier": occurrence.identifier,
                    "blob_name": blob.blob_name,
                    "content_hash": chunk.content_hash,
                    "kind": occurrence.kind,
                    "start_line": occurrence.start_line,
                    "end_line": occurrence.end_line,
                }
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

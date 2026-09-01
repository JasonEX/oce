"""SQL source-chunk lookup for path-only retrieval hits."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oce.domain.services.search import SearchHit
from oce.infrastructure.persistence.models import (
    BlobChunkModel,
    BlobModel,
    ChunkModel,
)


class SqlPathContentStore:
    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_representative_chunks(
        self,
        blob_names: Sequence[str],
    ) -> list[SearchHit]:
        names = tuple(dict.fromkeys(blob_names))
        if not names:
            return []
        ranked_links = (
            select(
                BlobChunkModel.blob_name.label("blob_name"),
                BlobChunkModel.content_hash.label("content_hash"),
                BlobChunkModel.start_line.label("start_line"),
                BlobChunkModel.end_line.label("end_line"),
                func.row_number()
                .over(
                    partition_by=BlobChunkModel.blob_name,
                    order_by=BlobChunkModel.chunk_index,
                )
                .label("position"),
            )
            .where(BlobChunkModel.blob_name.in_(names))
            .subquery()
        )
        statement = (
            select(
                BlobModel.blob_name,
                BlobModel.path,
                ChunkModel.content_hash,
                ChunkModel.content,
                ranked_links.c.start_line,
                ranked_links.c.end_line,
            )
            .join(ranked_links, ranked_links.c.blob_name == BlobModel.blob_name)
            .join(ChunkModel, ChunkModel.content_hash == ranked_links.c.content_hash)
            .where(BlobModel.status == "ready", ranked_links.c.position == 1)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()

        first_by_blob: dict[str, SearchHit] = {}
        for row in rows:
            first_by_blob.setdefault(
                row.blob_name,
                SearchHit(
                    blob_name=row.blob_name,
                    path=row.path,
                    content_hash=row.content_hash,
                    content=row.content,
                    start_line=row.start_line,
                    end_line=row.end_line,
                    score=0.0,
                ),
            )
        return [first_by_blob[name] for name in names if name in first_by_blob]

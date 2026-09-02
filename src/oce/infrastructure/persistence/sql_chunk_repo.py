"""SQLAlchemy chunk 元数据仓储。向量只写入 Milvus。"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from oce.domain.blob.blob import BlobStatus
from oce.domain.chunk import Chunk, LocatedChunk
from oce.domain.repositories import ChunkRepository
from oce.infrastructure.persistence.dialect import upsert_insert
from oce.infrastructure.persistence.models import BlobChunkModel, BlobModel, ChunkModel


class SqlChunkRepository(ChunkRepository):
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def save_many(self, chunks: Sequence[Chunk]) -> None:
        if not chunks:
            return
        values = [
            {
                "content_hash": chunk.content_hash,
                "content": chunk.content,
                "content_size": len(chunk.content.encode("utf-8")),
                "chunk_type": chunk.chunk_type,
                "embedded": False,  # 新切的 chunk 默认未嵌入
            }
            for chunk in chunks
        ]
        stmt = upsert_insert(self.session)(ChunkModel).values(values)
        stmt = stmt.on_conflict_do_nothing(index_elements=["content_hash"])
        await self.session.execute(stmt)

    async def mark_embedded(self, content_hashes: Sequence[str]) -> None:
        """标记 chunks 已嵌入到 Milvus"""
        if not content_hashes:
            return
        stmt = (
            update(ChunkModel)
            .where(ChunkModel.content_hash.in_(content_hashes))
            .values(embedded=True)
        )
        await self.session.execute(stmt)

    async def find_pending_for_blobs(
        self,
        blob_names: Sequence[str],
        limit: int | None = None,
    ) -> list[LocatedChunk]:
        if not blob_names:
            return []

        stmt = (
            select(
                BlobChunkModel.blob_name,
                ChunkModel.content_hash,
                BlobModel.path,
                ChunkModel.content,
                BlobChunkModel.start_line,
                BlobChunkModel.end_line,
            )
            .join(ChunkModel, ChunkModel.content_hash == BlobChunkModel.content_hash)
            .join(BlobModel, BlobModel.blob_name == BlobChunkModel.blob_name)
            .where(
                BlobChunkModel.blob_name.in_(blob_names),
                BlobModel.status == BlobStatus.PENDING.value,
            )
            .order_by(BlobChunkModel.blob_name, BlobChunkModel.chunk_index)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = (await self.session.execute(stmt)).all()
        return [
            LocatedChunk(
                blob_name=row.blob_name,
                content_hash=row.content_hash,
                path=row.path,
                content=row.content,
                start_line=row.start_line,
                end_line=row.end_line,
            )
            for row in rows
        ]

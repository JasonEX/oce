"""SQLAlchemy Blob 聚合仓储。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from oce.domain.blob.blob import Blob, BlobStatus
from oce.domain.chunk import ChunkRef
from oce.domain.repositories import BlobRepository
from oce.infrastructure.persistence.dialect import upsert_insert
from oce.infrastructure.persistence.lexical_index import delete_lexical_rows
from oce.infrastructure.persistence.models import (
    BlobChunkModel,
    BlobModel,
    BlobStagingModel,
    ChainMemberModel,
    ChunkModel,
)


class SqlBlobRepository(BlobRepository):
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, blob_name: str) -> Blob | None:
        row = (
            await self.session.execute(
                select(BlobModel).where(BlobModel.blob_name == blob_name)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return self._row_to_domain(row, await self._load_chunks(blob_name))

    async def get_many(self, blob_names: Sequence[str]) -> dict[str, Blob]:
        if not blob_names:
            return {}
        rows = (
            (
                await self.session.execute(
                    select(BlobModel).where(BlobModel.blob_name.in_(blob_names))
                )
            )
            .scalars()
            .all()
        )
        chunks = await self._load_chunks_many(blob_names)
        return {
            row.blob_name: self._row_to_domain(row, chunks.get(row.blob_name, []))
            for row in rows
        }

    async def exists_many(self, blob_names: Sequence[str]) -> dict[str, bool]:
        if not blob_names:
            return {}
        rows = await self.session.execute(
            select(BlobModel.blob_name).where(BlobModel.blob_name.in_(blob_names))
        )
        existing = set(rows.scalars())
        return {name: name in existing for name in blob_names}

    async def save(self, blob: Blob) -> None:
        await self.save_many([blob])

    async def save_many(self, blobs: Sequence[Blob]) -> None:
        if not blobs:
            return
        values = [
            {
                "blob_name": blob.blob_name,
                "path": blob.path,
                "content_size": blob.content_size,
                "language": blob.language,
                "file_type": blob.file_type,
                "status": blob.status.value,
                "retry_count": blob.retry_count,
                "last_seen": blob.last_seen,
                "created_at": blob.created_at,
                "error_message": blob.error_message,
            }
            for blob in blobs
        ]
        stmt = upsert_insert(self.session)(BlobModel).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["blob_name"],
            set_={
                "path": stmt.excluded.path,
                "content_size": stmt.excluded.content_size,
                "language": stmt.excluded.language,
                "file_type": stmt.excluded.file_type,
                "status": stmt.excluded.status,
                "retry_count": stmt.excluded.retry_count,
                "last_seen": stmt.excluded.last_seen,
                "error_message": stmt.excluded.error_message,
            },
        )
        await self.session.execute(stmt)
        for blob in blobs:
            await self._save_blob_chunks(blob.blob_name, blob.chunks)

    async def delete(self, blob_name: str) -> None:
        await self.delete_many([blob_name])

    async def delete_many(self, blob_names: Sequence[str]) -> None:
        if not blob_names:
            return
        content_hashes = list(
            (
                await self.session.execute(
                    select(BlobChunkModel.content_hash)
                    .where(BlobChunkModel.blob_name.in_(blob_names))
                    .distinct()
                )
            ).scalars()
        )
        await self.session.execute(
            delete(BlobChunkModel).where(BlobChunkModel.blob_name.in_(blob_names))
        )
        await self.session.execute(
            delete(BlobModel).where(BlobModel.blob_name.in_(blob_names))
        )
        if content_hashes:
            referenced = select(BlobChunkModel.content_hash).where(
                BlobChunkModel.content_hash == ChunkModel.content_hash
            )
            await self.session.execute(
                delete(ChunkModel).where(
                    ChunkModel.content_hash.in_(content_hashes),
                    ~referenced.exists(),
                )
            )
            # The term index has no foreign key (FTS5 virtual table); drop the
            # documents of chunks that just became unreferenced.
            await delete_lexical_rows(self.session, content_hashes)

    async def find_pending(self, blob_names: Sequence[str] | None = None) -> list[Blob]:
        stmt = select(BlobModel).where(BlobModel.status == BlobStatus.PENDING.value)
        if blob_names is not None:
            if not blob_names:
                return []
            stmt = stmt.where(BlobModel.blob_name.in_(blob_names))
        rows = (await self.session.execute(stmt)).scalars().all()
        chunks = await self._load_chunks_many([row.blob_name for row in rows])
        return [self._row_to_domain(row, chunks.get(row.blob_name, [])) for row in rows]

    async def list_ready_names(self, limit: int) -> list[str]:
        if limit < 1:
            return []
        result = await self.session.execute(
            select(BlobModel.blob_name)
            .where(BlobModel.status == BlobStatus.READY.value)
            .order_by(BlobModel.blob_name)
            .limit(limit)
        )
        return list(result.scalars())

    async def find_expired(self, ttl_days: int, batch_size: int = 1000) -> list[str]:
        threshold = datetime.now(timezone.utc) - timedelta(days=ttl_days)
        referenced = select(ChainMemberModel.chain_id).where(
            ChainMemberModel.blob_name == BlobModel.blob_name
        )
        rows = await self.session.execute(
            select(BlobModel.blob_name)
            .where(
                BlobModel.last_seen < threshold,
                ~referenced.exists(),
            )
            .limit(batch_size)
        )
        return list(rows.scalars())

    # ── blob_staging 操作 ────────────────────────────────────────────────

    async def get_staging(self, blob_name: str) -> str | None:
        """读取 staging 原文；空文件存的是空串，只有缺行才返回 None。"""
        result = await self.session.execute(
            select(BlobStagingModel.content).where(
                BlobStagingModel.blob_name == blob_name
            )
        )
        return result.scalar_one_or_none()

    async def save_staging(self, blob_name: str, content: str) -> None:
        """保存 staging 原文，已存在则跳过（UPSERT 幂等）"""
        stmt = (
            upsert_insert(self.session)(BlobStagingModel)
            .values(blob_name=blob_name, content=content)
            .on_conflict_do_nothing(index_elements=["blob_name"])
        )
        await self.session.execute(stmt)

    async def delete_staging(self, blob_name: str) -> None:
        """删除 staging 原文（worker 消费完后调用）"""
        await self.session.execute(
            delete(BlobStagingModel).where(BlobStagingModel.blob_name == blob_name)
        )

    async def _load_chunks(self, blob_name: str) -> list[ChunkRef]:
        return (await self._load_chunks_many([blob_name])).get(blob_name, [])

    async def _load_chunks_many(
        self, blob_names: Sequence[str]
    ) -> dict[str, list[ChunkRef]]:
        if not blob_names:
            return {}
        rows = (
            await self.session.execute(
                select(BlobChunkModel)
                .where(BlobChunkModel.blob_name.in_(blob_names))
                .order_by(BlobChunkModel.blob_name, BlobChunkModel.chunk_index)
            )
        ).scalars()
        result: dict[str, list[ChunkRef]] = {}
        for row in rows:
            result.setdefault(row.blob_name, []).append(
                ChunkRef(
                    row.content_hash,
                    row.start_line,
                    row.end_line,
                    context=row.context,
                )
            )
        return result

    async def _save_blob_chunks(
        self, blob_name: str, chunks: Sequence[ChunkRef]
    ) -> None:
        if not chunks:
            return
        values = [
            {
                "blob_name": blob_name,
                "content_hash": chunk.content_hash,
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
                "chunk_index": index,
                "context": chunk.context,
            }
            for index, chunk in enumerate(chunks)
        ]
        stmt = upsert_insert(self.session)(BlobChunkModel).values(values)
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["blob_name", "content_hash", "start_line", "end_line"]
        )
        await self.session.execute(stmt)

    async def list_pending_names(self) -> list[str]:
        """全部 pending blob 名。队列对账要全集，且只需要标识不需要聚合。"""
        result = await self.session.execute(
            select(BlobModel.blob_name).where(
                BlobModel.status == BlobStatus.PENDING.value
            )
        )
        return list(result.scalars())

    async def find_stale_with_staging(
        self,
        stale_hours: int = 24,
        limit: int = 100,
    ) -> list[str]:
        """查找有 staging 但长时间未处理的 pending blob(用于重新入队或清理)"""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=stale_hours)
        result = await self.session.execute(
            select(BlobModel.blob_name)
            .join(BlobStagingModel, BlobStagingModel.blob_name == BlobModel.blob_name)
            .where(
                BlobModel.status == BlobStatus.PENDING.value,
                BlobStagingModel.created_at < cutoff,
            )
            .limit(limit)
        )
        return list(result.scalars())

    @staticmethod
    def _row_to_domain(row: BlobModel, chunks: list[ChunkRef]) -> Blob:
        return Blob(
            blob_name=row.blob_name,
            path=row.path,
            status=BlobStatus(row.status),
            chunks=chunks,
            content_size=row.content_size,
            language=row.language,
            file_type=row.file_type,
            retry_count=row.retry_count,
            last_seen=row.last_seen,
            created_at=row.created_at,
            error_message=row.error_message,
        )

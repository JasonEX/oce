"""基于 PostgreSQL symbol_occurrences 表的精确标识符召回。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy import and_, case, exists, or_, select
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from oce.domain.services.search import SearchHit, SearchScope
from oce.infrastructure.persistence.models import (
    BlobModel,
    ChainMemberModel,
    ChainModel,
    ChunkModel,
    SymbolOccurrenceModel,
)


_SCOPE_BATCH_SIZE = 500
_RELATIONAL_DELTA_LIMIT = 500


class SymbolSearchStore:
    """通过 symbol_occurrences 倒排索引进行精确标识符召回。"""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        timeout_seconds: float = 2.0,
    ) -> None:
        self._session_factory = session_factory
        self._timeout_seconds = timeout_seconds

    async def search_exact(
        self,
        *,
        identifiers: Sequence[str],
        scope: SearchScope,
        top_k: int = 50,
    ) -> list[SearchHit]:
        """查询工作集内的标识符，按 endpoint > definition 排序。"""
        identifiers = tuple(dict.fromkeys(item for item in identifiers if item))
        if not identifiers or top_k <= 0 or not scope.blob_names:
            return []

        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with self._session_factory() as session:
                    rows = await self._query_scope(session, identifiers, scope, top_k)
        except TimeoutError:
            return []
        return self._rows_to_hits(rows, top_k)

    async def _query_scope(
        self,
        session: AsyncSession,
        identifiers: Sequence[str],
        scope: SearchScope,
        top_k: int,
    ) -> list[Row[Any]]:
        delta_size = len(scope.added_blob_names) + len(scope.deleted_blob_names)
        if (
            scope.chain_id is not None
            and scope.chain_version is not None
            and delta_size <= _RELATIONAL_DELTA_LIMIT
        ):
            rows = await self._query_relational_scope(
                session, identifiers, scope, top_k
            )
            current_version = await session.scalar(
                select(ChainModel.version).where(ChainModel.chain_id == scope.chain_id)
            )
            if current_version == scope.chain_version:
                return rows

        # Added-only scopes and unusually large request deltas use bounded
        # statements.  This is also the consistency fallback if a checkpoint
        # changes between scope resolution and exact recall.
        rows: list[Row[Any]] = []
        names = sorted(scope.blob_names)
        for offset in range(0, len(names), _SCOPE_BATCH_SIZE):
            batch = names[offset : offset + _SCOPE_BATCH_SIZE]
            rows.extend(
                await self._query_rows(
                    session,
                    identifiers,
                    SymbolOccurrenceModel.blob_name.in_(batch),
                    top_k,
                )
            )
        return rows

    async def _query_relational_scope(
        self,
        session: AsyncSession,
        identifiers: Sequence[str],
        scope: SearchScope,
        top_k: int,
    ) -> list[Row[Any]]:
        member_exists = exists(
            select(1)
            .select_from(ChainMemberModel)
            .join(ChainModel, ChainModel.chain_id == ChainMemberModel.chain_id)
            .where(
                ChainMemberModel.chain_id == scope.chain_id,
                ChainModel.version == scope.chain_version,
                ChainMemberModel.blob_name == SymbolOccurrenceModel.blob_name,
            )
        )
        membership = member_exists
        if scope.added_blob_names:
            membership = or_(
                membership,
                SymbolOccurrenceModel.blob_name.in_(scope.added_blob_names),
            )
        if scope.deleted_blob_names:
            membership = and_(
                membership,
                SymbolOccurrenceModel.blob_name.not_in(scope.deleted_blob_names),
            )
        return await self._query_rows(session, identifiers, membership, top_k)

    async def _query_rows(
        self,
        session: AsyncSession,
        identifiers: Sequence[str],
        scope_predicate: ColumnElement[bool],
        top_k: int,
    ) -> list[Row[Any]]:
        kind_priority = case(
            (SymbolOccurrenceModel.kind == "endpoint", 3),
            (SymbolOccurrenceModel.kind == "definition", 2),
            else_=1,
        )
        stmt = (
            select(
                SymbolOccurrenceModel.content_hash,
                SymbolOccurrenceModel.kind,
                SymbolOccurrenceModel.blob_name,
                BlobModel.path,
                ChunkModel.content,
                SymbolOccurrenceModel.start_line,
                SymbolOccurrenceModel.end_line,
            )
            .join(
                ChunkModel,
                SymbolOccurrenceModel.content_hash == ChunkModel.content_hash,
            )
            .join(BlobModel, SymbolOccurrenceModel.blob_name == BlobModel.blob_name)
            .where(
                SymbolOccurrenceModel.identifier.in_(identifiers),
                BlobModel.status == "ready",
                scope_predicate,
            )
            .order_by(
                kind_priority.desc(),
                BlobModel.path,
                SymbolOccurrenceModel.start_line,
            )
            .limit(max(top_k * 20, top_k))
        )
        return list((await session.execute(stmt)).all())

    def _rows_to_hits(self, rows: Sequence[Row[Any]], top_k: int) -> list[SearchHit]:
        hits: list[SearchHit] = []
        seen: set[tuple[str, str, int, int]] = set()
        for row in rows:
            key = (row.blob_name, row.content_hash, row.start_line, row.end_line)
            if key in seen:
                continue
            seen.add(key)
            hits.append(
                SearchHit(
                    blob_name=row.blob_name,
                    path=row.path,
                    content=row.content,
                    score=self._score_by_kind(row.kind),
                    content_hash=row.content_hash,
                    start_line=row.start_line,
                    end_line=row.end_line,
                )
            )
        hits.sort(key=lambda hit: (-hit.score, hit.path, hit.start_line))
        return hits[:top_k]

    @staticmethod
    def _score_by_kind(kind: str) -> float:
        """按 kind 分配优先级分数。"""
        if kind == "endpoint":
            return 1.0
        if kind == "definition":
            return 0.95
        return 0.85

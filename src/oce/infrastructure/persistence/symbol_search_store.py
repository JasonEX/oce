"""Exact identifier recall and definition lookup over ``symbol_occurrences``."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy import and_, case, func, select
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from oce.domain.blob.blob import BlobStatus
from oce.domain.services.search import DefinitionHit, SearchHit, SearchScope
from oce.domain.services.symbols import DEFINITION_KINDS
from oce.infrastructure.persistence.models import (
    BlobChunkModel,
    BlobModel,
    ChunkModel,
    SymbolOccurrenceModel,
)
from oce.infrastructure.persistence.scope_filter import run_scoped

# 结构证据的优先级分：endpoint > definition > import；SQL 排序与命中打分共用一份。
_KIND_SCORES = {"endpoint": 1.0, "definition": 0.95, "call": 0.9}
_DEFAULT_KIND_SCORE = 0.85


def _kind_priority() -> ColumnElement[float]:
    return case(
        *(
            (SymbolOccurrenceModel.kind == kind, score)
            for kind, score in _KIND_SCORES.items()
        ),
        else_=_DEFAULT_KIND_SCORE,
    )


def _occurrence_rows(
    identifiers: Sequence[str],
    scope_predicate: ColumnElement[bool],
    limit: int,
    kinds: Sequence[str] | None,
):
    """Occurrence rows joined to the chunk occurrence that contains them.

    ``blob_chunks`` carries the chunk span and scope context; the line
    containment condition picks the right occurrence when identical chunk text
    appears twice in one file.
    """
    stmt = (
        select(
            SymbolOccurrenceModel.identifier,
            SymbolOccurrenceModel.kind,
            SymbolOccurrenceModel.blob_name,
            SymbolOccurrenceModel.content_hash,
            SymbolOccurrenceModel.start_line.label("def_start"),
            SymbolOccurrenceModel.end_line.label("def_end"),
            BlobModel.path,
            ChunkModel.content,
            BlobChunkModel.start_line,
            BlobChunkModel.end_line,
            BlobChunkModel.context,
        )
        .join(ChunkModel, SymbolOccurrenceModel.content_hash == ChunkModel.content_hash)
        .join(BlobModel, SymbolOccurrenceModel.blob_name == BlobModel.blob_name)
        .join(
            BlobChunkModel,
            and_(
                BlobChunkModel.blob_name == SymbolOccurrenceModel.blob_name,
                BlobChunkModel.content_hash == SymbolOccurrenceModel.content_hash,
                BlobChunkModel.start_line <= SymbolOccurrenceModel.start_line,
                BlobChunkModel.end_line >= SymbolOccurrenceModel.start_line,
            ),
        )
        .where(
            SymbolOccurrenceModel.identifier.in_(identifiers),
            BlobModel.status == BlobStatus.READY.value,
            scope_predicate,
        )
        .order_by(
            _kind_priority().desc(),
            BlobModel.path,
            SymbolOccurrenceModel.start_line,
        )
        .limit(limit)
    )
    if kinds is not None:
        stmt = stmt.where(SymbolOccurrenceModel.kind.in_(kinds))
    return stmt


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
        kinds: Sequence[str] | None = None,
    ) -> list[SearchHit]:
        """查询工作集内的标识符，按 endpoint > definition > import 排序。

        同一标识符在 scope 内出现越多，单条命中的证据越弱：``setup`` 定义 200 次
        时不应把 200 个 chunk 都推到 dense 结果前面，因此按出现次数对数衰减。
        """
        identifiers = tuple(dict.fromkeys(item for item in identifiers if item))
        if not identifiers or top_k <= 0 or not scope.blob_names:
            return []
        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with self._session_factory() as session:
                    rows = await run_scoped(
                        session,
                        scope,
                        SymbolOccurrenceModel.blob_name,
                        lambda predicate: _occurrence_rows(
                            identifiers, predicate, max(top_k * 20, top_k), kinds
                        ),
                    )
        except TimeoutError:
            return []
        return self._rows_to_hits(rows, top_k)

    async def find_definitions(
        self,
        *,
        identifiers: Sequence[str],
        scope: SearchScope,
        max_per_identifier: int = 3,
    ) -> list[DefinitionHit]:
        identifiers = tuple(dict.fromkeys(item for item in identifiers if item))
        if not identifiers or not scope.blob_names:
            return []
        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with self._session_factory() as session:
                    counts = await self._definition_counts(session, identifiers, scope)
                    wanted = tuple(
                        identifier
                        for identifier in identifiers
                        if 0 < counts.get(identifier, 0) <= max_per_identifier
                    )
                    if not wanted:
                        return []
                    rows = await run_scoped(
                        session,
                        scope,
                        SymbolOccurrenceModel.blob_name,
                        lambda predicate: _occurrence_rows(
                            wanted,
                            predicate,
                            len(wanted) * max_per_identifier * 2,
                            DEFINITION_KINDS,
                        ),
                    )
        except TimeoutError:
            return []

        order = {identifier: index for index, identifier in enumerate(identifiers)}
        definitions: list[DefinitionHit] = []
        seen: set[tuple[str, str, int]] = set()
        for row in rows:
            key = (row.identifier, row.blob_name, row.def_start)
            if key in seen:
                continue
            seen.add(key)
            definitions.append(
                DefinitionHit(
                    identifier=row.identifier,
                    kind=row.kind,
                    hit=self._row_hit(row, self._score_by_kind(row.kind)),
                    start_line=row.def_start,
                    end_line=row.def_end,
                )
            )
        definitions.sort(key=lambda item: (order[item.identifier], item.hit.path))
        return definitions

    @staticmethod
    async def _definition_counts(
        session: AsyncSession,
        identifiers: Sequence[str],
        scope: SearchScope,
    ) -> dict[str, int]:
        def build(predicate: ColumnElement[bool]):
            return (
                select(
                    SymbolOccurrenceModel.identifier,
                    func.count().label("total"),
                )
                .join(BlobModel, SymbolOccurrenceModel.blob_name == BlobModel.blob_name)
                .where(
                    SymbolOccurrenceModel.identifier.in_(identifiers),
                    SymbolOccurrenceModel.kind.in_(DEFINITION_KINDS),
                    BlobModel.status == BlobStatus.READY.value,
                    predicate,
                )
                .group_by(SymbolOccurrenceModel.identifier)
            )

        counts: dict[str, int] = {}
        for row in await run_scoped(
            session, scope, SymbolOccurrenceModel.blob_name, build
        ):
            counts[row.identifier] = counts.get(row.identifier, 0) + int(row.total)
        return counts

    def _rows_to_hits(self, rows: Sequence[Row[Any]], top_k: int) -> list[SearchHit]:
        # Damping measures how ambiguous a name is, i.e. how many places declare
        # it. Call sites and imports are usage, not ambiguity: a function called
        # from forty files is still one unambiguous definition.
        occurrences: set[tuple[str, str, int]] = set()
        per_identifier: dict[str, int] = {}
        usage_only: dict[str, int] = {}
        for row in rows:
            occurrence = (row.identifier, row.blob_name, row.def_start)
            if occurrence in occurrences:
                continue
            occurrences.add(occurrence)
            counter = per_identifier if row.kind in DEFINITION_KINDS else usage_only
            counter[row.identifier] = counter.get(row.identifier, 0) + 1
        for identifier, count in usage_only.items():
            per_identifier.setdefault(identifier, count)

        # One chunk may hold several matched identifiers; it keeps its best score.
        best: dict[tuple[str, str, int, int], SearchHit] = {}
        for row in rows:
            key = (row.blob_name, row.content_hash, row.start_line, row.end_line)
            score = self._score_by_kind(row.kind) / (
                1.0 + math.log(per_identifier[row.identifier])
            )
            current = best.get(key)
            if current is None or score > current.score:
                best[key] = self._row_hit(row, score)

        hits = list(best.values())
        hits.sort(key=lambda hit: (-hit.score, hit.path, hit.start_line))
        return hits[:top_k]

    @staticmethod
    def _row_hit(row: Row[Any], score: float) -> SearchHit:
        return SearchHit(
            blob_name=row.blob_name,
            path=row.path,
            content=row.content,
            score=score,
            content_hash=row.content_hash,
            start_line=row.start_line,
            end_line=row.end_line,
            context=row.context,
        )

    @staticmethod
    def _score_by_kind(kind: str) -> float:
        return _KIND_SCORES.get(kind, _DEFAULT_KIND_SCORE)

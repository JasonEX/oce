"""Exact identifier recall and definition lookup over ``symbol_occurrences``."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from oce.domain.blob.blob import BlobStatus
from oce.domain.services.relations import RelatedOccurrence
from oce.domain.services.search import DefinitionHit, SearchHit, SearchScope
from oce.domain.services.symbols import (
    CALL_KIND,
    DEFINITION_KINDS,
    INHERIT_KIND,
    REEXPORT_KIND,
)
from oce.domain.services.test_paths import TEST_DIRECTORIES, is_test_path
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
    path_predicate: ColumnElement[bool] | None = None,
    partition_by: Sequence[ColumnElement[Any]] = (),
    partition_limit: int = 1,
):
    """Occurrence rows joined to the chunk occurrence that contains them.

    ``blob_chunks`` carries the chunk span and scope context; the line
    containment condition picks the right occurrence when identical chunk text
    appears twice in one file.
    """
    columns = (
        SymbolOccurrenceModel.identifier,
        SymbolOccurrenceModel.kind,
        SymbolOccurrenceModel.blob_name,
        SymbolOccurrenceModel.content_hash,
        SymbolOccurrenceModel.start_line.label("def_start"),
        SymbolOccurrenceModel.end_line.label("def_end"),
        SymbolOccurrenceModel.enclosing,
        BlobModel.path,
        ChunkModel.content,
        BlobChunkModel.start_line,
        BlobChunkModel.end_line,
        BlobChunkModel.context,
    )
    stmt = (
        select(
            *columns,
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
    )
    if kinds is not None:
        stmt = stmt.where(SymbolOccurrenceModel.kind.in_(kinds))
    if path_predicate is not None:
        stmt = stmt.where(path_predicate)
    if partition_by:
        ranked = (
            stmt.add_columns(
                func.row_number()
                .over(
                    partition_by=partition_by,
                    order_by=(
                        _kind_priority().desc(),
                        BlobModel.path,
                        SymbolOccurrenceModel.start_line,
                    ),
                )
                .label("_partition_rank")
            )
            .add_columns(
                case((_test_path_predicate(), 1), else_=0).label("_test_priority")
            )
            .subquery()
        )
        stmt = (
            select(*(ranked.c[column.key] for column in columns))
            .where(ranked.c._partition_rank <= partition_limit)
            .order_by(
                ranked.c._test_priority,
                ranked.c.path,
                ranked.c.def_start,
            )
            .limit(limit)
        )
        return stmt
    return stmt.order_by(
        _kind_priority().desc(),
        BlobModel.path,
        SymbolOccurrenceModel.start_line,
    ).limit(limit)


# SQL prefilter for test files; ``is_test_path`` makes the final decision.
def _test_path_predicate() -> ColumnElement[bool]:
    lowered = func.lower(BlobModel.path)
    return or_(
        lowered.like("%test%"),
        lowered.like("%spec%"),
        lowered.like("%conftest%"),
        *(
            lowered.like(f"{directory}/%") | lowered.like(f"%/{directory}/%")
            for directory in TEST_DIRECTORIES
        ),
    )


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

    # ── relation lookups ────────────────────────────────────────────────

    async def find_callers(
        self,
        *,
        identifiers: Sequence[str],
        scope: SearchScope,
        limit: int = 8,
    ) -> list[RelatedOccurrence]:
        """One call site per (file, enclosing definition), source files first."""
        rows = await self._occurrences(
            identifiers,
            scope,
            (CALL_KIND,),
            limit,
            partition_by=(
                SymbolOccurrenceModel.blob_name,
                SymbolOccurrenceModel.enclosing,
            ),
        )
        return _group(rows, key=lambda row: (row.blob_name, row.enclosing), limit=limit)

    async def find_test_uses(
        self,
        *,
        identifiers: Sequence[str],
        scope: SearchScope,
        limit: int = 8,
    ) -> list[RelatedOccurrence]:
        """Occurrences in test files; every file gets one excerpt before any gets two."""
        rows = await self._occurrences(
            identifiers,
            scope,
            None,
            limit,
            path_predicate=_test_path_predicate(),
            partition_by=(SymbolOccurrenceModel.blob_name,),
            partition_limit=max(limit, 1),
        )
        rows = [row for row in rows if is_test_path(row.path)]
        per_file = _group(rows, key=lambda row: row.blob_name, limit=limit)
        if len(per_file) >= limit:
            return per_file
        seen = {(item.hit.blob_name, item.enclosing) for item in per_file}
        second = [
            item
            for item in _group(
                rows, key=lambda row: (row.blob_name, row.enclosing), limit=limit * 2
            )
            if (item.hit.blob_name, item.enclosing) not in seen
        ]
        return [*per_file, *second][:limit]

    async def find_implementations(
        self,
        *,
        identifiers: Sequence[str],
        scope: SearchScope,
        limit: int = 8,
    ) -> list[RelatedOccurrence]:
        rows = await self._occurrences(
            identifiers,
            scope,
            (INHERIT_KIND,),
            limit,
            partition_by=(
                SymbolOccurrenceModel.blob_name,
                SymbolOccurrenceModel.enclosing,
            ),
        )
        return _group(rows, key=lambda row: (row.blob_name, row.enclosing), limit=limit)

    async def find_reexports(
        self,
        *,
        identifiers: Sequence[str],
        scope: SearchScope,
        limit: int = 4,
    ) -> list[RelatedOccurrence]:
        rows = await self._occurrences(
            identifiers,
            scope,
            (REEXPORT_KIND,),
            limit,
            partition_by=(SymbolOccurrenceModel.blob_name,),
        )
        return _group(rows, key=lambda row: (row.blob_name, row.def_start), limit=limit)

    async def defined_identifiers(
        self,
        occurrences: Sequence[tuple[str, str]],
        scope: SearchScope,
    ) -> dict[tuple[str, str], tuple[str, ...]]:
        rows = await self._rows_for_pairs(
            occurrences,
            scope,
            (
                SymbolOccurrenceModel.blob_name,
                SymbolOccurrenceModel.content_hash,
                SymbolOccurrenceModel.identifier,
            ),
            SymbolOccurrenceModel.kind.in_(DEFINITION_KINDS),
        )
        names: dict[tuple[str, str], list[str]] = {}
        for blob_name, content_hash, identifier in rows:
            bucket = names.setdefault((str(blob_name), str(content_hash)), [])
            if identifier not in bucket:
                bucket.append(str(identifier))
        return {key: tuple(value) for key, value in names.items()}

    async def _occurrences(
        self,
        identifiers: Sequence[str],
        scope: SearchScope,
        kinds: Sequence[str] | None,
        limit: int,
        *,
        path_predicate: ColumnElement[bool] | None = None,
        partition_by: Sequence[ColumnElement[Any]] = (),
        partition_limit: int = 1,
    ) -> list[Row[Any]]:
        identifiers = tuple(dict.fromkeys(item for item in identifiers if item))
        if not identifiers or limit <= 0 or not scope.blob_names:
            return []
        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with self._session_factory() as session:
                    return list(
                        await run_scoped(
                            session,
                            scope,
                            SymbolOccurrenceModel.blob_name,
                            lambda predicate: _occurrence_rows(
                                identifiers,
                                predicate,
                                max(limit * 25, 200),
                                kinds,
                                path_predicate,
                                partition_by,
                                partition_limit,
                            ),
                        )
                    )
        except TimeoutError:
            return []

    async def occurrence_kinds(
        self,
        occurrences: Sequence[tuple[str, str]],
        scope: SearchScope,
    ) -> dict[tuple[str, str], frozenset[str]]:
        rows = await self._rows_for_pairs(
            occurrences,
            scope,
            (
                SymbolOccurrenceModel.blob_name,
                SymbolOccurrenceModel.content_hash,
                SymbolOccurrenceModel.kind,
            ),
            None,
        )
        kinds: dict[tuple[str, str], set[str]] = {}
        for blob_name, content_hash, kind in rows:
            key = (str(blob_name), str(content_hash))
            kinds.setdefault(key, set()).add(str(kind))
        return {key: frozenset(value) for key, value in kinds.items()}

    async def _rows_for_pairs(
        self,
        occurrences: Sequence[tuple[str, str]],
        scope: SearchScope,
        columns: Sequence[Any],
        extra_predicate: ColumnElement[bool] | None,
    ) -> Sequence[Row[Any]]:
        """Distinct ``columns`` of the scoped occurrence rows of given chunk pairs."""
        pairs = tuple(
            dict.fromkeys(
                (blob_name, content_hash)
                for blob_name, content_hash in occurrences
                if blob_name and content_hash
            )
        )
        if not pairs or not scope.blob_names:
            return []
        pair_predicate = or_(
            *(
                and_(
                    SymbolOccurrenceModel.blob_name == blob_name,
                    SymbolOccurrenceModel.content_hash == content_hash,
                )
                for blob_name, content_hash in pairs
            )
        )

        def build(predicate: ColumnElement[bool]):
            stmt = (
                select(*columns)
                .join(BlobModel, SymbolOccurrenceModel.blob_name == BlobModel.blob_name)
                .join(
                    BlobChunkModel,
                    and_(
                        BlobChunkModel.blob_name == SymbolOccurrenceModel.blob_name,
                        BlobChunkModel.content_hash
                        == SymbolOccurrenceModel.content_hash,
                    ),
                )
                .where(
                    pair_predicate,
                    BlobModel.status == BlobStatus.READY.value,
                    predicate,
                )
                .distinct()
            )
            if extra_predicate is not None:
                stmt = stmt.where(extra_predicate)
            return stmt

        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with self._session_factory() as session:
                    return await run_scoped(
                        session, scope, SymbolOccurrenceModel.blob_name, build
                    )
        except TimeoutError:
            return []

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
        # Usage evidence is diversified per file before the window closes: a
        # test module that calls the symbol in forty chunks must not push the
        # one import in every other file past ``top_k``. Definitions keep
        # their order so overloads in one file stay together.
        definition_keys = {
            (row.blob_name, row.content_hash, row.start_line, row.end_line)
            for row in rows
            if row.kind in DEFINITION_KINDS
        }
        rank_in_file: dict[str, int] = {}
        ordered: list[tuple[int, int, SearchHit]] = []
        for index, hit in enumerate(hits):
            key = (hit.blob_name, hit.content_hash, hit.start_line, hit.end_line)
            if key in definition_keys:
                ordered.append((0, index, hit))
                continue
            rank = rank_in_file.get(hit.blob_name, 0)
            rank_in_file[hit.blob_name] = rank + 1
            ordered.append((rank, index, hit))
        ordered.sort(key=lambda item: (item[0], item[1]))
        return [item[2] for item in ordered][:top_k]

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


def _group(
    rows: Sequence[Row[Any]],
    *,
    key: Callable[[Row[Any]], object],
    limit: int,
) -> list[RelatedOccurrence]:
    """First occurrence per key, source files before tests, then path order."""
    ordered = sorted(
        rows, key=lambda row: (is_test_path(row.path), row.path, row.def_start)
    )
    seen: set[object] = set()
    grouped: list[RelatedOccurrence] = []
    for row in ordered:
        group_key = key(row)
        if group_key in seen:
            continue
        seen.add(group_key)
        grouped.append(
            RelatedOccurrence(
                identifier=row.identifier,
                kind=row.kind,
                hit=SymbolSearchStore._row_hit(
                    row, SymbolSearchStore._score_by_kind(row.kind)
                ),
                line=row.def_start,
                end_line=row.def_end,
                enclosing=row.enclosing or "",
            )
        )
        if len(grouped) >= limit:
            break
    return grouped

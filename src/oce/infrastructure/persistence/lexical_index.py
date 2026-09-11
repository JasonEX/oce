"""Chunk term index: SQLite FTS5 in personal mode, tsvector + GIN in PostgreSQL.

Both back ends store the same document (``build_lexical_document``) keyed by
content hash, so one row serves every file containing that chunk. Ranking is
constrained by ``blob_chunks`` membership before ``LIMIT``; a small workspace
therefore never scans and widens candidates from unrelated indexed projects.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any

from sqlalchemy import (
    bindparam,
    column,
    func,
    literal_column,
    select,
    table,
    text,
)
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from oce.domain.blob.blob import BlobStatus
from oce.domain.chunk import Chunk
from oce.domain.services.lexical import build_lexical_document, phrase_tokens
from oce.domain.services.search import SearchHit, SearchScope
from oce.infrastructure.persistence.models import BlobChunkModel, BlobModel, ChunkModel
from oce.infrastructure.persistence.scope_filter import run_scoped

TABLE_NAME = "chunk_lexical"
_PHRASE_BONUS = 0.5

_LEXICAL = table(
    TABLE_NAME,
    column("content_hash"),
    column("terms"),
    column("terms_tsv"),
)


def create_lexical_table(connection: Connection) -> None:
    """DDL for the current dialect; idempotent so tests and migrations share it."""
    if connection.dialect.name == "sqlite":
        connection.execute(
            text(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {TABLE_NAME} USING fts5("
                "content_hash UNINDEXED, terms, tokenize='unicode61')"
            )
        )
        return
    connection.execute(
        text(
            f"CREATE TABLE IF NOT EXISTS {TABLE_NAME} ("
            "content_hash VARCHAR(64) PRIMARY KEY, "
            "terms TEXT NOT NULL, "
            "terms_tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', terms)) STORED)"
        )
    )
    connection.execute(
        text(
            f"CREATE INDEX IF NOT EXISTS ix_{TABLE_NAME}_tsv "
            f"ON {TABLE_NAME} USING GIN (terms_tsv)"
        )
    )


def drop_lexical_table(connection: Connection) -> None:
    connection.execute(text(f"DROP TABLE IF EXISTS {TABLE_NAME}"))


async def delete_lexical_rows(
    session: AsyncSession, content_hashes: Sequence[str]
) -> None:
    """Remove documents of chunks that no longer exist in ``chunks``."""
    if not content_hashes:
        return
    hashes = list(dict.fromkeys(content_hashes))
    for offset in range(0, len(hashes), 500):
        batch = hashes[offset : offset + 500]
        await session.execute(
            text(
                f"DELETE FROM {TABLE_NAME} WHERE content_hash IN :hashes "
                "AND NOT EXISTS (SELECT 1 FROM chunks "
                f"WHERE chunks.content_hash = {TABLE_NAME}.content_hash)"
            ).bindparams(bindparam("hashes", expanding=True)),
            {"hashes": batch},
        )


class SqlLexicalProjection:
    """Write one term document per new chunk inside the indexing transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def index(self, chunks: Sequence[Chunk]) -> None:
        unique = {chunk.content_hash: chunk for chunk in chunks}
        if not unique:
            return
        hashes = list(unique)
        existing = set(
            (
                await self._session.execute(
                    text(
                        f"SELECT content_hash FROM {TABLE_NAME} "
                        "WHERE content_hash IN :hashes"
                    ).bindparams(bindparam("hashes", expanding=True)),
                    {"hashes": hashes},
                )
            ).scalars()
        )
        rows = [
            {
                "content_hash": chunk.content_hash,
                "terms": build_lexical_document(chunk.content),
            }
            for chunk in unique.values()
            if chunk.content_hash not in existing
        ]
        if not rows:
            return
        statement = (
            f"INSERT INTO {TABLE_NAME} (content_hash, terms) "
            "VALUES (:content_hash, :terms)"
        )
        if self._session.get_bind().dialect.name == "postgresql":
            # Multiple worker transactions may discover the same immutable
            # content concurrently. The primary key resolves that race.
            statement += " ON CONFLICT (content_hash) DO NOTHING"
        await self._session.execute(text(statement), rows)


class SqlLexicalSearchStore:
    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def search_lexical(
        self,
        *,
        terms: Sequence[str],
        phrases: Sequence[str],
        scope: SearchScope,
        top_k: int = 30,
        required: Sequence[str] = (),
    ) -> list[SearchHit]:
        term_list = [term for term in dict.fromkeys(terms) if term.isalnum()]
        required_list = [term for term in dict.fromkeys(required) if term.isalnum()]
        phrase_lists = [
            list(tokens)
            for tokens in (phrase_tokens(phrase) for phrase in phrases)
            if tokens
        ]
        if (not term_list and not phrase_lists) or top_k <= 0 or not scope.blob_names:
            return []
        # Retrieval and startup warm-up own their deadlines. A second timer
        # here can cancel SQLAlchemy again while it returns a connection.
        async with self._session_factory() as session:
            dialect = session.get_bind().dialect.name
            query = _build_query(
                dialect, term_list, phrase_lists, required=required_list
            )
            # One lexical document may occur in several files. A small
            # scoped surplus preserves those occurrences without the
            # old global 300/3000-row widening loop.
            limit = top_k * 3
            ranked = await self._ranked_candidates(
                session, dialect, query, scope, limit
            )
            hits = await self._resolve(session, scope, ranked, top_k)

        lowered = [phrase.lower() for phrase in phrases]
        if not hits:
            return []
        best = max(hit.score for hit in hits) or 1.0
        rescored = [
            replace(
                hit,
                score=hit.score / best
                + (
                    _PHRASE_BONUS
                    if any(phrase in hit.content.lower() for phrase in lowered)
                    else 0.0
                ),
            )
            for hit in hits
        ]
        rescored.sort(key=lambda hit: -hit.score)
        return rescored[:top_k]

    @staticmethod
    async def _ranked_candidates(
        session: AsyncSession,
        dialect: str,
        query: str,
        scope: SearchScope,
        limit: int,
    ) -> dict[str, float]:
        def build(scope_predicate: ColumnElement[bool]):
            # Materialize scoped hashes once. Correlating both this lookup and
            # checkpoint membership repeats B-tree probes for global FTS hits.
            member = _LEXICAL.c.content_hash.in_(
                select(BlobChunkModel.content_hash)
                .select_from(BlobChunkModel)
                .join(BlobModel, BlobModel.blob_name == BlobChunkModel.blob_name)
                .where(
                    BlobModel.status == BlobStatus.READY.value,
                    scope_predicate,
                )
            )
            if dialect == "sqlite":
                score = literal_column(f"-bm25({TABLE_NAME})").label("score")
                matches = _LEXICAL.c.terms.match(query)
            else:
                parsed = func.to_tsquery("simple", query)
                score = func.ts_rank_cd(_LEXICAL.c.terms_tsv, parsed).label("score")
                matches = _LEXICAL.c.terms_tsv.op("@@")(parsed)
            return (
                select(_LEXICAL.c.content_hash, score)
                .where(matches, member)
                .order_by(score.desc(), _LEXICAL.c.content_hash)
                .limit(limit)
            )

        rows = await run_scoped(session, scope, BlobChunkModel.blob_name, build)
        ranked: dict[str, float] = {}
        for row in rows:
            score = float(row.score)
            ranked[row.content_hash] = max(score, ranked.get(row.content_hash, score))
        return dict(
            sorted(ranked.items(), key=lambda item: (-item[1], item[0]))[:limit]
        )

    @staticmethod
    async def _resolve(
        session: AsyncSession,
        scope: SearchScope,
        ranked: dict[str, float],
        top_k: int,
    ) -> list[SearchHit]:
        if not ranked:
            return []
        hashes = list(ranked)

        def build(predicate: ColumnElement[bool]):
            return (
                select(
                    BlobChunkModel.blob_name,
                    BlobChunkModel.content_hash,
                    BlobChunkModel.start_line,
                    BlobChunkModel.end_line,
                    BlobChunkModel.context,
                    BlobModel.path,
                    ChunkModel.content,
                )
                .join(BlobModel, BlobModel.blob_name == BlobChunkModel.blob_name)
                .join(
                    ChunkModel, ChunkModel.content_hash == BlobChunkModel.content_hash
                )
                .where(
                    BlobChunkModel.content_hash.in_(hashes),
                    BlobModel.status == BlobStatus.READY.value,
                    predicate,
                )
            )

        rows = await run_scoped(session, scope, BlobChunkModel.blob_name, build)
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
                    score=ranked[row.content_hash],
                    content_hash=row.content_hash,
                    start_line=row.start_line,
                    end_line=row.end_line,
                    context=row.context,
                )
            )
        hits.sort(key=lambda hit: (-hit.score, hit.path, hit.start_line))
        return hits[: max(top_k * 3, top_k)]


def _build_query(
    dialect: str,
    terms: list[str],
    phrases: list[list[str]],
    *,
    required: Sequence[str] = (),
) -> str:
    """OR query in the dialect's full-text syntax; phrases keep token order.

    ``required`` becomes a conjunct: a row must contain one of those tokens,
    yet BM25/ts_rank still see every term, so the identifier gate does not
    flatten the ranking among the rows that pass it.
    """
    if dialect == "sqlite":
        parts = [f'"{" ".join(tokens)}"' for tokens in phrases]
        parts.extend(f'"{term}"' for term in terms)
        ranked = " OR ".join(parts)
        if not required:
            return ranked
        gate = " OR ".join(f'"{term}"' for term in required)
        return f"({gate}) AND ({ranked})"
    parts = [" <-> ".join(tokens) for tokens in phrases]
    parts.extend(terms)
    ranked = " | ".join(f"({part})" if " " in part else part for part in parts)
    if not required:
        return ranked
    gate = " | ".join(required)
    return f"({gate}) & ({ranked})"


def lexical_row_count_statement() -> Any:
    return text(f"SELECT COUNT(*) FROM {TABLE_NAME}")

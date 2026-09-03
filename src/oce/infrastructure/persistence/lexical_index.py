"""Chunk term index: SQLite FTS5 in personal mode, tsvector + GIN in PostgreSQL.

Both back ends store the same document (``build_lexical_document``) keyed by
content hash, so one row serves every file containing that chunk. Scope is
applied after ranking through the ordinary ``blob_chunks`` relation; the
candidate window is widened once when the scope is a small slice of a large
shared index.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any

from sqlalchemy import bindparam, select, text
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
_MIN_CANDIDATES = 300
_WIDEN_FACTOR = 10
_PHRASE_BONUS = 0.5


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
    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        timeout_seconds: float = 2.0,
    ) -> None:
        self._session_factory = session_factory
        self._timeout_seconds = timeout_seconds

    async def search_lexical(
        self,
        *,
        terms: Sequence[str],
        phrases: Sequence[str],
        scope: SearchScope,
        top_k: int = 30,
    ) -> list[SearchHit]:
        term_list = [term for term in dict.fromkeys(terms) if term.isalnum()]
        phrase_lists = [
            list(tokens)
            for tokens in (phrase_tokens(phrase) for phrase in phrases)
            if tokens
        ]
        if (not term_list and not phrase_lists) or top_k <= 0 or not scope.blob_names:
            return []
        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with self._session_factory() as session:
                    dialect = session.get_bind().dialect.name
                    query = _build_query(dialect, term_list, phrase_lists)
                    limit = max(_MIN_CANDIDATES, top_k * 10)
                    ranked = await self._ranked_candidates(
                        session, dialect, query, limit
                    )
                    hits = await self._resolve(session, scope, ranked, top_k)
                    if len(hits) < top_k and len(ranked) >= limit:
                        ranked = await self._ranked_candidates(
                            session, dialect, query, limit * _WIDEN_FACTOR
                        )
                        hits = await self._resolve(session, scope, ranked, top_k)
        except TimeoutError:
            return []

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
        session: AsyncSession, dialect: str, query: str, limit: int
    ) -> dict[str, float]:
        if dialect == "sqlite":
            statement = text(
                f"SELECT content_hash, -bm25({TABLE_NAME}) AS score FROM {TABLE_NAME} "
                f"WHERE {TABLE_NAME} MATCH :query ORDER BY bm25({TABLE_NAME}) LIMIT :limit"
            )
        else:
            statement = text(
                "SELECT content_hash, ts_rank_cd(terms_tsv, q) AS score "
                f"FROM {TABLE_NAME}, to_tsquery('simple', :query) AS q "
                "WHERE terms_tsv @@ q ORDER BY score DESC LIMIT :limit"
            )
        rows = (
            await session.execute(statement, {"query": query, "limit": limit})
        ).all()
        ranked: dict[str, float] = {}
        for row in rows:
            ranked.setdefault(row.content_hash, float(row.score))
        return ranked

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


def _build_query(dialect: str, terms: list[str], phrases: list[list[str]]) -> str:
    """OR query in the dialect's full-text syntax; phrases keep token order."""
    if dialect == "sqlite":
        parts = [f'"{" ".join(tokens)}"' for tokens in phrases]
        parts.extend(f'"{term}"' for term in terms)
        return " OR ".join(parts)
    parts = [" <-> ".join(tokens) for tokens in phrases]
    parts.extend(terms)
    return " | ".join(f"({part})" if " " in part else part for part in parts)


def lexical_row_count_statement() -> Any:
    return text(f"SELECT COUNT(*) FROM {TABLE_NAME}")

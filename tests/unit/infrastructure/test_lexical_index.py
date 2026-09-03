"""SQLite FTS5 term index: projection, scoped search, phrase bonus, cleanup."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from oce.domain.blob.blob import Blob, BlobStatus
from oce.domain.chunk import Chunk
from oce.domain.services.search import SearchScope
from oce.infrastructure.persistence.lexical_index import (
    SqlLexicalProjection,
    SqlLexicalSearchStore,
    create_lexical_table,
)
from oce.infrastructure.persistence.sql_blob_repo import SqlBlobRepository
from oce.infrastructure.persistence.sql_chain_repo import SqlChainRepository
from oce.infrastructure.persistence.sql_chunk_repo import SqlChunkRepository
from oce.shared.database.session import Base
from tests.conftest import make_sha256


@pytest.fixture
async def sessions():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(create_lexical_table)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _index(session: AsyncSession, specs):
    """specs: (label, path, content). Returns blob names in order."""
    chunk_repo = SqlChunkRepository(session)
    blob_repo = SqlBlobRepository(session)
    projection = SqlLexicalProjection(session)
    names = []
    for label, path, content in specs:
        chunk = Chunk(
            Chunk.compute_hash(content), path, content, 1, content.count("\n") + 1
        )
        await chunk_repo.save_many([chunk])
        await projection.index([chunk])
        name = make_sha256(f"blob-{label}")
        await blob_repo.save(
            Blob(
                name, path, BlobStatus.READY, chunks=[chunk.to_ref()], language="python"
            )
        )
        names.append(name)
    await session.commit()
    return names


async def test_error_text_and_identifiers_are_found_in_scope(sessions):
    async with sessions() as session:
        names = await _index(
            session,
            [
                (
                    "a",
                    "src/a.py",
                    'def load():\n    raise ValueError("invalid compressor for dtype")',
                ),
                (
                    "b",
                    "src/b.py",
                    "def parse_config(path):\n    return read_toml(path)",
                ),
                ("c", "src/c.py", "def unrelated():\n    return 1"),
            ],
        )
    store = SqlLexicalSearchStore(sessions)

    hits = await store.search_lexical(
        terms=("valueerror", "compressor", "dtype"),
        phrases=("invalid compressor for dtype",),
        scope=SearchScope(frozenset(names)),
        top_k=5,
    )
    assert [hit.path for hit in hits] == ["src/a.py"]
    assert hits[0].score > 1.0  # phrase bonus on top of the normalized rank

    hits = await store.search_lexical(
        terms=("parseconfig",),
        phrases=(),
        scope=SearchScope(frozenset(names)),
    )
    assert [hit.path for hit in hits] == ["src/b.py"]

    # CamelCase in the query still meets snake_case in the source.
    hits = await store.search_lexical(
        terms=("parse", "config"),
        phrases=(),
        scope=SearchScope(frozenset(names[1:2])),
    )
    assert [hit.path for hit in hits] == ["src/b.py"]

    # Out-of-scope files never surface.
    hits = await store.search_lexical(
        terms=("compressor",),
        phrases=(),
        scope=SearchScope(frozenset(names[1:])),
    )
    assert hits == []


async def test_required_identifier_gates_rows_but_not_their_ranking(sessions):
    async with sessions() as session:
        names = await _index(
            session,
            [
                (
                    "json-heavy",
                    "src/serializer.py",
                    "def dump(json_value):\n"
                    "    json.dumps(json_value)\n"
                    "    json.loads(json_value)\n"
                    "    return json_value",
                ),
                (
                    "call-site",
                    "src/view.py",
                    "def handle(request):\n"
                    "    payload = request.get_json()\n"
                    "    return json.dumps(payload)",
                ),
                (
                    "call-site-only",
                    "src/other.py",
                    "def other(request):\n    return request.get_json()",
                ),
            ],
        )
    store = SqlLexicalSearchStore(sessions)
    scope = SearchScope(frozenset(names))
    terms = ("getjson", "get", "json")

    ungated = await store.search_lexical(terms=terms, phrases=(), scope=scope)
    assert "src/serializer.py" in {hit.path for hit in ungated}

    gated = await store.search_lexical(
        terms=terms, phrases=(), scope=scope, required=("getjson",)
    )
    assert {hit.path for hit in gated} == {"src/view.py", "src/other.py"}
    # The gate only decides admission: rows that pass keep the relative
    # order the full term set gave them.
    surviving = [hit.path for hit in ungated if hit.path != "src/serializer.py"]
    assert [hit.path for hit in gated] == surviving


async def test_scope_uses_chain_membership(sessions):
    async with sessions() as session:
        names = await _index(
            session,
            [
                ("a", "src/a.py", "def alpha_fn(): pass"),
                ("b", "src/b.py", "def alpha_fn(): pass"),
            ],
        )
        chain = await SqlChainRepository(session).create([names[0]])
        await session.commit()
    store = SqlLexicalSearchStore(sessions)
    hits = await store.search_lexical(
        terms=("alphafn",),
        phrases=(),
        scope=SearchScope(
            frozenset(names[:1]), chain_id=chain.chain_id, chain_version=chain.version
        ),
    )
    assert [hit.path for hit in hits] == ["src/a.py"]


async def test_scope_is_applied_before_the_lexical_limit(sessions):
    async with sessions() as session:
        specs = [
            (f"noise-{index}", f"noise/{index}.py", "needle needle needle")
            for index in range(12)
        ]
        specs.append(("target", "src/target.py", "needle"))
        names = await _index(session, specs)
    store = SqlLexicalSearchStore(sessions)

    hits = await store.search_lexical(
        terms=("needle",),
        phrases=(),
        scope=SearchScope(frozenset({names[-1]})),
        top_k=1,
    )

    assert [hit.path for hit in hits] == ["src/target.py"]


async def test_projection_is_idempotent_and_rows_follow_chunk_deletion(sessions):
    async with sessions() as session:
        content = "def keep_me(): pass"
        names = await _index(session, [("a", "src/a.py", content)])
        projection = SqlLexicalProjection(session)
        chunk = Chunk(Chunk.compute_hash(content), "src/a.py", content, 1, 1)
        await projection.index([chunk])
        await session.commit()
        count = await session.scalar(text("SELECT COUNT(*) FROM chunk_lexical"))
        assert count == 1

        await SqlBlobRepository(session).delete_many(names)
        await session.commit()
        count = await session.scalar(text("SELECT COUNT(*) FROM chunk_lexical"))
        assert count == 0


async def test_projection_deduplicates_equal_chunks_in_one_batch(sessions):
    content = "def shared(): pass"
    content_hash = Chunk.compute_hash(content)
    chunks = [
        Chunk(content_hash, "src/a.py", content, 1, 1),
        Chunk(content_hash, "src/b.py", content, 5, 5),
    ]
    async with sessions() as session:
        await SqlLexicalProjection(session).index(chunks)
        await session.commit()
        count = await session.scalar(text("SELECT COUNT(*) FROM chunk_lexical"))
    assert count == 1

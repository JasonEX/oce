"""Index real files through the SQL unit of work, then retrieve through the SQL stores.

Dense recall is faked; everything the migration creates (chunk context, symbol
occurrences, the term index) is exercised for real, end to end through the
retrieval state machine and the formatter.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from oce.application.service import compute_blob_name
from oce.domain.services.formatter import RELATED_HEADER, format_retrieval
from oce.domain.services.indexing import IndexingPipeline
from oce.domain.services.retrieval import RetrievalPipeline
from oce.domain.services.search import SearchScope
from oce.infrastructure.astchunk.symbol_provider import TreeSitterSymbolProvider
from oce.infrastructure.chunkers.factory import build_chunker
from oce.infrastructure.persistence.lexical_index import (
    SqlLexicalSearchStore,
    create_lexical_table,
)
from oce.infrastructure.persistence.path_lookup_store import SqlPathLookupStore
from oce.infrastructure.persistence.symbol_search_store import SymbolSearchStore
from oce.infrastructure.persistence.uow import SqlAlchemyUnitOfWork
from oce.infrastructure.regex_symbol_provider import RegexSymbolProvider
from oce.shared.config.settings import RetrievalSettings
from oce.shared.database.session import Base
from tests.fakes.indexing import ConstantEmbedder, RecordingVectorIndex

FILES = {
    "src/app/service.py": (
        "from src.app.base import BaseService\n"
        "from src.app.errors import PoolExhausted\n"
        "\n"
        "\n"
        "class UserService(BaseService):\n"
        "    def authenticate(self, token):\n"
        "        if not self.pool.acquire():\n"
        '            raise PoolExhausted("connection pool exhausted for user service")\n'
        "        return self.verify_bearer_token(token)\n"
        "\n"
        "    def verify_bearer_token(self, token):\n"
        '        return token.startswith("Bearer ")\n'
    ),
    "src/app/base.py": (
        "class BaseService:\n"
        "    pool = None\n"
        "\n"
        "    def start(self):\n"
        "        return True\n"
    ),
    "src/app/errors.py": "class PoolExhausted(RuntimeError):\n    pass\n",
    "src/app/unrelated.py": "def add(a, b):\n    return a + b\n",
}


class DenseFromRecords:
    """Fake dense store: returns every indexed chunk in insertion order."""

    def __init__(self, index: RecordingVectorIndex):
        self.index = index

    async def search(
        self, *, query_vector, allowed_blob_names=None, top_k=50, vector_threshold=0.0
    ):
        from oce.domain.services.search import SearchHit

        allowed = set(allowed_blob_names or [])
        hits = []
        for position, record in enumerate(self.index.records):
            if allowed and record.blob_name not in allowed:
                continue
            hits.append(
                SearchHit(
                    blob_name=record.blob_name,
                    path=record.path,
                    content=record.content,
                    score=0.9 - position * 0.01,
                    content_hash=record.content_hash,
                    start_line=record.start_line,
                    end_line=record.end_line,
                    context=record.context,
                )
            )
        return hits[:top_k]


@pytest.fixture
async def indexed():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(create_lexical_table)
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    vector_index = RecordingVectorIndex()
    provider = TreeSitterSymbolProvider(RegexSymbolProvider())
    chunker = build_chunker(
        semantic_enabled=True,
        semantic_max_chunk_chars=120,
        recursive_chunk_size=6000,
        recursive_chunk_overlap=200,
    )
    names = {}
    async with SqlAlchemyUnitOfWork(sessions, provider) as uow:
        pipeline = IndexingPipeline(
            chunker=chunker,
            embedder=ConstantEmbedder(dimensions=2),
            vector_index=vector_index,
            blob_repo=uow.blobs,
            chunk_repo=uow.chunks,
            symbol_projection=uow.symbols,
            lexical_projection=uow.lexical,
        )
        for path, content in FILES.items():
            name = compute_blob_name(path, content)
            names[path] = name
            await pipeline.ingest(name, path, content)
        await pipeline.embed_pending(list(names.values()))
        await uow.commit()
    yield sessions, vector_index, names
    await engine.dispose()


def _pipeline(sessions, vector_index, **overrides):
    # Two primary slots: the fake dense store returns every chunk, so a larger
    # budget would select the whole repository and leave nothing to pull in.
    settings = RetrievalSettings(confidence_floor=0.0, final_select_k=2, **overrides)
    return RetrievalPipeline(
        embedder=ConstantEmbedder(dimensions=2),
        store=DenseFromRecords(vector_index),
        exact_store=SymbolSearchStore(sessions),
        lexical_store=SqlLexicalSearchStore(sessions),
        path_lookup_store=SqlPathLookupStore(sessions),
        settings=settings,
    )


async def test_error_text_and_traceback_find_the_failing_code(indexed):
    sessions, vector_index, names = indexed
    scope = SearchScope(frozenset(names.values()))
    hits = await _pipeline(sessions, vector_index).search(
        "Requests fail with the following error:\n"
        "Traceback (most recent call last):\n"
        '  File "/srv/app/src/app/service.py", line 8, in authenticate\n'
        "PoolExhausted: connection pool exhausted for user service\n",
        scope,
    )
    primary = [hit for hit in hits if hit.role == "primary"]
    assert primary[0].path == "src/app/service.py"
    assert "connection pool exhausted" in primary[0].content
    # The exception class named by the traceback is an exact hit. A compound
    # issue query stays focused on localization instead of fanning out through
    # every identifier mentioned by the selected code.
    assert "src/app/errors.py" in {hit.path for hit in hits}
    text = format_retrieval(hits)
    assert RELATED_HEADER not in text


async def test_call_chain_appends_related_definition(indexed):
    sessions, vector_index, names = indexed
    scope = SearchScope(frozenset(names.values()))
    hits = await _pipeline(sessions, vector_index).search(
        "How does `authenticate` call its base service?", scope
    )

    # A one-ended trace lists what ``authenticate`` calls as chain hops; the
    # remaining referenced definitions follow as related excerpts. The
    # exception class it raises is shown once, as a signature excerpt.
    excerpts = [hit for hit in hits if hit.role in ("related", "chain")]
    assert "src/app/base.py" in {hit.path for hit in hits}
    errors = [hit for hit in excerpts if hit.path == "src/app/errors.py"]
    assert len(errors) == 1
    assert errors[0].content.startswith("class PoolExhausted")
    text = format_retrieval(hits)
    assert "Path: src/app/errors.py" in text


async def test_symbol_lookup_reaches_definition_with_chunk_context(indexed):
    sessions, vector_index, names = indexed
    scope = SearchScope(frozenset(names.values()))
    hits = await _pipeline(sessions, vector_index).search(
        "Where is `verify_bearer_token` defined?", scope
    )
    primary = [hit for hit in hits if hit.role == "primary"]
    assert primary[0].path == "src/app/service.py"
    assert "def verify_bearer_token" in primary[0].content
    # Chunk context survives SQL round trips when the method was split from its class.
    contexts = {hit.context for hit in primary if hit.path == "src/app/service.py"}
    assert any(
        ctx and ctx.startswith("class UserService(BaseService):") for ctx in contexts
    ) or any("class UserService" in hit.content for hit in primary)
    # Focused symbol lookups answer the requested definition directly; the
    # relations that follow (callers, the definitions its body calls, tests)
    # are labelled sections, never more primary results.
    assert hits[0].role == "primary"
    assert all(
        hit.role in ("related", "caller", "implementation", "test", "reexport")
        for hit in hits[len(primary) :]
    )


async def test_lexical_recall_alone_finds_call_sites(indexed):
    sessions, vector_index, names = indexed
    scope = SearchScope(frozenset(names.values()))
    store = SqlLexicalSearchStore(sessions)
    hits = await store.search_lexical(
        terms=("startswith", "bearer"), phrases=(), scope=scope, top_k=5
    )
    assert hits and hits[0].path == "src/app/service.py"

"""Kinds, frequency damping, and definition lookup over symbol_occurrences."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from oce.domain.blob.blob import Blob, BlobStatus
from oce.domain.chunk import Chunk
from oce.domain.services.search import SearchScope
from oce.infrastructure.astchunk.symbol_provider import TreeSitterSymbolProvider
from oce.infrastructure.persistence.sql_blob_repo import SqlBlobRepository
from oce.infrastructure.persistence.sql_chunk_repo import SqlChunkRepository
from oce.infrastructure.persistence.sql_symbol_projection import SqlSymbolProjection
from oce.infrastructure.persistence.symbol_search_store import SymbolSearchStore
from oce.infrastructure.regex_symbol_provider import RegexSymbolProvider
from oce.shared.database.session import Base
from tests.conftest import make_sha256


@pytest.fixture
async def sessions():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _index_files(session: AsyncSession, files: dict[str, str]) -> dict[str, str]:
    """Index each file as two line-based chunks so definitions cross chunks."""
    projection = SqlSymbolProjection(
        session, TreeSitterSymbolProvider(RegexSymbolProvider())
    )
    chunk_repo = SqlChunkRepository(session)
    blob_repo = SqlBlobRepository(session)
    names: dict[str, str] = {}
    for path, content in files.items():
        lines = content.splitlines()
        half = max(1, len(lines) // 2)
        chunks = []
        for start, end in ((1, half), (half + 1, len(lines))):
            if end < start:
                continue
            text = "\n".join(lines[start - 1 : end])
            chunks.append(
                Chunk(
                    Chunk.compute_hash(text),
                    path,
                    text,
                    start,
                    end,
                    context=f"ctx:{path}",
                )
            )
        await chunk_repo.save_many(chunks)
        name = make_sha256(path)
        blob = Blob(
            name,
            path,
            BlobStatus.READY,
            chunks=[c.to_ref() for c in chunks],
            language="python",
        )
        await blob_repo.save(blob)
        await projection.index(blob, chunks, content)
        names[path] = name
    await session.commit()
    return names


_FILES = {
    "src/base.py": "import os\n\n\nclass BaseService:\n    limit = 3\n\n    def start(self):\n        return self.limit\n",
    "src/svc.py": "from src.base import BaseService\n\n\nclass Service(BaseService):\n    def run(self):\n        return start_all()\n",
    "src/util.py": "def start_all():\n    return 1\n\n\ndef helper():\n    return 2\n\n\ndef helper():\n    return 3\n",
}


async def test_kinds_filter_and_chunk_span_alignment(sessions):
    async with sessions() as session:
        names = await _index_files(session, _FILES)
    store = SymbolSearchStore(sessions)
    scope = SearchScope(frozenset(names.values()))

    definitions = await store.search_exact(
        identifiers=["BaseService"], scope=scope, kinds=("endpoint", "definition")
    )
    assert [hit.path for hit in definitions] == ["src/base.py"]
    # Hit lines are the chunk's lines, not the definition's, so the formatter
    # prints the content at the right numbers; context rides along.
    assert definitions[0].start_line == 1
    assert definitions[0].context == "ctx:src/base.py"

    everything = await store.search_exact(identifiers=["BaseService"], scope=scope)
    assert {hit.path for hit in everything} == {"src/base.py", "src/svc.py"}
    # The import occurrence ranks below the definition.
    assert everything[0].path == "src/base.py"


async def test_frequency_damping_and_definition_lookup(sessions):
    async with sessions() as session:
        names = await _index_files(session, _FILES)
    store = SymbolSearchStore(sessions)
    scope = SearchScope(frozenset(names.values()))

    hits = await store.search_exact(identifiers=["helper", "start_all"], scope=scope)
    by_path_start = {(hit.path, hit.start_line): hit.score for hit in hits}
    # ``helper`` is defined twice, so each hit carries less evidence than the
    # single ``start_all`` definition.
    assert by_path_start[("src/util.py", 1)] > max(
        score for (path, start), score in by_path_start.items() if start != 1
    )

    definitions = await store.find_definitions(
        identifiers=["start_all", "BaseService", "helper", "missing"],
        scope=scope,
        max_per_identifier=1,
    )
    found = [(d.identifier, d.hit.path, d.start_line, d.end_line) for d in definitions]
    assert ("start_all", "src/util.py", 1, 2) in found
    assert ("BaseService", "src/base.py", 4, 8) in found
    # Two definitions exceed max_per_identifier=1; unknown names are absent.
    assert not any(item[0] in {"helper", "missing"} for item in found)
    # Ordered by the caller's identifier order.
    assert [d.identifier for d in definitions] == ["start_all", "BaseService"]

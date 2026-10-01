"""Full path semantics work against an existing bounded-text Milvus schema."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from pymilvus import MilvusClient
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from oce.api.schemas import BlobInput
from oce.application.commands.ingest import build_pipeline_factory
from oce.domain.blob.blob import BlobStatus
from oce.domain.chunk import RecursiveChunker
from oce.domain.services.path_document_builder import build_path_document
from oce.infrastructure.milvus3.path_index import PathIndexClient
from oce.infrastructure.milvus3.schema import create_path_collection_schema
from oce.infrastructure.persistence.lexical_index import create_lexical_table
from oce.infrastructure.persistence.models import BlobModel
from oce.infrastructure.persistence.path_content_store import SqlPathContentStore
from oce.infrastructure.persistence.sql_blob_repo import SqlBlobRepository
from oce.infrastructure.persistence.uow import SqlAlchemyUnitOfWork
from oce.infrastructure.regex_symbol_provider import RegexSymbolProvider
from oce.shared.config.settings import DatabaseSettings, MilvusSettings
from oce.shared.database.session import Base, create_engine
from oce.shared.path_limits import MAX_SOURCE_PATH_CHARS
from tests.conftest import make_sha256
from tests.fakes.indexing import ConstantEmbedder, RecordingVectorIndex


def test_source_path_capacity_matches_api_and_sql() -> None:
    path = "a" * MAX_SOURCE_PATH_CHARS
    assert BlobInput(path=path, content="code").path == path
    with pytest.raises(ValidationError):
        BlobInput(path=path + "a", content="code")
    assert BlobModel.__table__.c.path.type.length == MAX_SOURCE_PATH_CHARS


async def test_legacy_sqlite_path_above_upload_limit_remains_readable(
    tmp_path: Path,
) -> None:
    engine = create_engine(
        DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    name = make_sha256("legacy-long-path")
    path = "a" * (MAX_SOURCE_PATH_CHARS + 1)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            # SQLite historically accepted values beyond VARCHAR's declared length.
            await session.execute(
                insert(BlobModel).values(
                    blob_name=name,
                    path=path,
                    content_size=0,
                    file_type="text",
                    status="ready",
                )
            )
            await session.commit()
        async with sessions() as session:
            blob = await SqlBlobRepository(session).get(name)
            assert blob is not None and blob.path == path and blob.is_ready()
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "path",
    ["/".join(["a" * 190] * 3 + ["file.py"]), "/".join(["源" * 80] * 8 + ["file.py"])],
)
async def test_existing_path_collection_keeps_full_embedding_and_sql_paths(
    tmp_path: Path, path: str
) -> None:
    endpoint = str(tmp_path / "vectors.db")
    legacy = MilvusClient(endpoint)
    try:
        # These VARCHAR capacities are the existing production schema. Opening
        # the same collection proves the fix requires no rebuild or ALTER.
        legacy.create_collection("paths", schema=create_path_collection_schema(4))
    finally:
        legacy.close()
    paths = PathIndexClient(
        MilvusSettings(
            endpoint=endpoint, path_collection_name="paths", dense_index_type="FLAT"
        ),
        dense_dim=4,
    )
    engine = create_engine(
        DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path / 'metadata.db'}")
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    provider = RegexSymbolProvider()
    embedder = ConstantEmbedder()
    factory = build_pipeline_factory(
        chunker=RecursiveChunker(),
        embedder=embedder,
        vector_index=RecordingVectorIndex(),
        path_store=paths,
    )
    name = make_sha256(path)
    try:
        await paths.initialize()
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.run_sync(create_lexical_table)
        async with SqlAlchemyUnitOfWork(sessions, provider) as uow:
            await factory(uow).ingest(name, path, "def work():\n    return 1")
            await uow.commit()
        async with SqlAlchemyUnitOfWork(sessions, provider) as uow:
            pipeline = factory(uow)
            batch = await pipeline.prepare([name])
            await uow.commit()
        assert batch is not None
        await pipeline.write_vectors(batch)
        async with SqlAlchemyUnitOfWork(sessions, provider) as uow:
            await factory(uow).complete(batch)
            await uow.commit()
            blob = await uow.blobs.get(name)
            assert (
                blob is not None
                and blob.status == BlobStatus.READY
                and blob.path == path
            )
        assert [build_path_document(path)] in embedder.calls
        results = await paths.search_paths([1.0] * 4, allowed_blob_names=[name])
        assert [result.blob_name for result in results] == [name]
        full = await SqlPathContentStore(sessions).get_representative_chunks([name])
        assert full[0].path == path
        stored = await paths._call(
            "query",
            "paths",
            filter=f'blob_name == "{name}"',
            output_fields=["path", "path_document"],
        )
        assert len(stored[0]["path"].encode("utf-8")) <= 512
        assert len(stored[0]["path_document"].encode("utf-8")) <= 2048
        assert path.startswith(stored[0]["path"])
    finally:
        await paths.close()
        await engine.dispose()

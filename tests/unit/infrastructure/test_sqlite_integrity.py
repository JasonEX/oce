"""Production SQLite connections enforce metadata ownership and write order."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from oce.application.commands.checkpoint import (
    CheckpointCommand,
    CheckpointCommandHandler,
)
from oce.application.commands.gc import GcCommand, GcCommandHandler
from oce.application.commands.ingest import (
    DeleteBlobsCommand,
    DeleteBlobsCommandHandler,
    build_pipeline_factory,
)
from oce.domain.blob.blob import Blob, BlobStatus
from oce.domain.chain.chain import Chain
from oce.domain.chunk import Chunk, RecursiveChunker
from oce.domain.services.search import SearchScope, VectorRecord
from oce.infrastructure.persistence.lexical_index import create_lexical_table
from oce.infrastructure.persistence.models import (
    BlobChunkModel,
    BlobModel,
    BlobStagingModel,
    ChainMemberModel,
    ChainModel,
    ChunkModel,
    SymbolOccurrenceModel,
)
from oce.infrastructure.persistence.uow import SqlAlchemyUnitOfWork
from oce.infrastructure.regex_symbol_provider import RegexSymbolProvider
from oce.shared.config.settings import DatabaseSettings
from oce.shared.database.session import Base, create_engine
from oce.shared.errors import ServiceNotReadyError
from tests.conftest import make_sha256
from tests.fakes.indexing import ConstantEmbedder, RecordingVectorIndex


@pytest.fixture
async def sql_sessions(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(
        DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path / 'metadata.db'}")
    )
    try:
        async with engine.begin() as connection:
            assert await connection.scalar(text("PRAGMA foreign_keys")) == 1
            await connection.run_sync(Base.metadata.create_all)
            await connection.run_sync(create_lexical_table)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def test_production_sqlite_deletes_owned_rows_and_preserves_shared_chunks(
    sql_sessions: async_sessionmaker[AsyncSession],
) -> None:
    sessions = sql_sessions
    provider = RegexSymbolProvider()
    content = "def run():\n    return 1"
    shared = Chunk(Chunk.compute_hash(content), "a.py", content, 1, 2)
    names = [make_sha256("first"), make_sha256("second")]
    async with SqlAlchemyUnitOfWork(sessions, provider) as uow:
        await uow.chunks.save_many([shared])
        for name, path in zip(names, ("a.py", "b.py"), strict=True):
            blob = Blob(
                name,
                path,
                BlobStatus.READY,
                chunks=[shared.to_ref()],
                language="python",
            )
            await uow.blobs.save(blob)
            await uow.blobs.save_staging(name, content)
            await uow.symbols.index(blob, [shared], content)
        await uow.lexical.index([shared])
        await uow.commit()
    async with SqlAlchemyUnitOfWork(sessions, provider) as uow:
        await uow.blobs.delete(names[0])
        await uow.commit()
    async with sessions() as session:
        for model in (
            BlobModel,
            BlobChunkModel,
            BlobStagingModel,
            SymbolOccurrenceModel,
            ChunkModel,
        ):
            assert await session.scalar(select(func.count()).select_from(model)) == 1
        assert list(await session.scalars(select(BlobModel.blob_name))) == names[1:]
        assert list(await session.execute(text("PRAGMA foreign_key_check"))) == []
    async with SqlAlchemyUnitOfWork(sessions, provider) as uow:
        await uow.blobs.delete(names[1])
        await uow.commit()
    async with sessions() as session:
        for model in (BlobStagingModel, SymbolOccurrenceModel, ChunkModel):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
        assert await session.scalar(text("SELECT count(*) FROM chunk_lexical")) == 0


@pytest.mark.parametrize("failed_store", ["dense", "path"])
async def test_gc_retries_a_failed_delete_without_exposing_or_losing_its_identity(
    sql_sessions: async_sessionmaker[AsyncSession], failed_store: str
) -> None:
    provider = RegexSymbolProvider()

    def factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(sql_sessions, provider)

    content = "def run():\n    return 1"
    chunk = Chunk(Chunk.compute_hash(content), "a.py", content, 1, 2)
    name = make_sha256("delete-retry")
    blob = Blob(name, "a.py", BlobStatus.READY, [chunk.to_ref()], language="python")
    async with factory() as uow:
        await uow.chunks.save_many([chunk])
        await uow.blobs.save(blob)
        await uow.blobs.save_staging(name, content)
        await uow.symbols.index(blob, [chunk], content)
        await uow.lexical.index([chunk])
        await uow.commit()
    index = RecordingVectorIndex()
    await index.upsert(
        [
            VectorRecord(
                make_sha256("vector"),
                chunk.content_hash,
                name,
                "a.py",
                content,
                1,
                2,
                [1.0],
            )
        ]
    )
    path = AsyncMock()
    delete_vectors = index.delete
    if failed_store == "dense":
        index.delete = AsyncMock(side_effect=RuntimeError("delete unavailable"))
    else:
        path.delete_by_blob_names.side_effect = RuntimeError("delete unavailable")
    handler = DeleteBlobsCommandHandler(factory, index, path)
    pipelines = build_pipeline_factory(
        chunker=RecursiveChunker(),
        embedder=ConstantEmbedder(),
        vector_index=index,
    )
    with pytest.raises(RuntimeError, match="delete unavailable"):
        await handler.handle(DeleteBlobsCommand((name,)))
    async with factory() as uow:
        retained = await uow.blobs.get(name)
        assert retained is not None and retained.status == BlobStatus.DELETING
        assert await uow.blobs.ready_names(SearchScope(frozenset({name}))) == set()
        # This newly touched identity is retried independently of the normal TTL.
        assert await uow.blobs.find_expired(30) == [name]
        with pytest.raises(ServiceNotReadyError, match="deletion is in progress"):
            await pipelines(uow).ingest(name, "a.py", content)
        with pytest.raises(ServiceNotReadyError, match="deletion is in progress"):
            await uow.blobs.save(blob)
        await uow.rollback()
    if failed_store == "dense":
        index.delete = delete_vectors
    else:
        path.delete_by_blob_names.side_effect = None
    result = await GcCommandHandler(factory, handler).handle(GcCommand(dry_run=False))
    assert result.deleted_blobs == 1
    assert index.records == []
    async with sql_sessions() as session:
        for model in (BlobModel, BlobStagingModel, SymbolOccurrenceModel, ChunkModel):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
    async with factory() as uow:
        await pipelines(uow).ingest(name, "a.py", content)
        await uow.commit()
        uploaded = await uow.blobs.get(name)
        assert uploaded is not None and uploaded.status == BlobStatus.PENDING
        assert await uow.blobs.get_staging(name) == content


async def test_prepare_and_new_checkpoint_protect_against_a_stale_gc_snapshot(
    sql_sessions: async_sessionmaker[AsyncSession],
) -> None:
    provider = RegexSymbolProvider()

    def factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(sql_sessions, provider)

    name = make_sha256("stale-gc")
    blob = Blob(
        name,
        "a.py",
        language="python",
        last_seen=datetime.now(timezone.utc) - timedelta(days=40),
    )
    async with factory() as uow:
        await uow.blobs.save(blob)
        await uow.blobs.save_staging(name, "def run():\n    return 1")
        await uow.commit()
    pipelines = build_pipeline_factory(
        chunker=RecursiveChunker(),
        embedder=ConstantEmbedder(),
        vector_index=RecordingVectorIndex(),
    )
    async with factory() as uow:
        stale = await uow.blobs.find_expired(30)
        assert stale == [name]
        assert await pipelines(uow).prepare([name]) is not None
        await uow.commit()
    async with factory() as uow:
        assert await uow.blobs.mark_deleting(stale, ttl_days=30) == []
        await uow.chains.create([name])
        await uow.commit()
    async with factory() as uow:
        assert await uow.blobs.mark_deleting(stale) == []


@pytest.mark.parametrize("existing_checkpoint", [False, True])
async def test_checkpoint_rejects_deleting_members_without_committing_partial_state(
    sql_sessions: async_sessionmaker[AsyncSession], existing_checkpoint: bool
) -> None:
    provider = RegexSymbolProvider()

    def factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(sql_sessions, provider)

    active, deleting, missing = [
        make_sha256(label) for label in ("active", "gone", "missing")
    ]
    async with factory() as uow:
        await uow.blobs.save(Blob(active, "active.py", BlobStatus.PENDING))
        await uow.blobs.save(Blob(deleting, "gone.py", BlobStatus.READY))
        assert await uow.blobs.mark_deleting([deleting]) == [deleting]
        await uow.commit()
    handler = CheckpointCommandHandler(factory)
    token = None
    if existing_checkpoint:
        token = (
            await handler.handle(CheckpointCommand(added_blobs=(active, missing)))
        ).new_checkpoint_id
    with pytest.raises(ServiceNotReadyError, match="retry the checkpoint"):
        await handler.handle(
            CheckpointCommand(checkpoint_id=token, added_blobs=(deleting,))
        )
    async with factory() as uow:
        assert uow.session is not None
        assert await uow.session.scalar(
            select(func.count()).select_from(ChainModel)
        ) == int(existing_checkpoint)
        assert await uow.session.scalar(
            select(func.count()).select_from(ChainMemberModel)
        ) == (2 if existing_checkpoint else 0)
        if token is not None:
            parsed = Chain.parse_checkpoint_token(token)
            assert parsed is not None
            chain = await uow.chains.get(parsed[0])
            assert chain is not None and chain.version == 1
            assert chain.members == {active, missing}
        assert (await uow.blobs.get(deleting)).status == BlobStatus.DELETING


@pytest.mark.parametrize("winner", [BlobStatus.READY, BlobStatus.DELETING])
async def test_prepare_does_not_rewrite_a_blob_that_left_pending_before_its_write_lock(
    sql_sessions: async_sessionmaker[AsyncSession], winner: BlobStatus
) -> None:
    provider = RegexSymbolProvider()
    name = make_sha256("prepare-snapshot")
    index = RecordingVectorIndex()
    pipelines = build_pipeline_factory(
        chunker=RecursiveChunker(), embedder=ConstantEmbedder(), vector_index=index
    )
    async with SqlAlchemyUnitOfWork(sql_sessions, provider) as uow:
        await pipelines(uow).ingest(name, "a.py", "def work():\n    return 1")
        await uow.commit()
    async with SqlAlchemyUnitOfWork(sql_sessions, provider) as uow:
        touch = uow.blobs.touch

        async def finish_before_touch(blob_name: str) -> None:
            assert uow.session is not None
            await uow.session.execute(
                update(BlobModel)
                .where(BlobModel.blob_name == blob_name)
                .values(status=winner.value)
            )
            await uow.blobs.delete_staging(blob_name)
            await touch(blob_name)

        uow.blobs.touch = AsyncMock(side_effect=finish_before_touch)
        assert await pipelines(uow).prepare([name]) is None
        await uow.commit()
        retained = await uow.blobs.get(name)
        assert retained is not None and retained.status == winner
    assert index.records == []

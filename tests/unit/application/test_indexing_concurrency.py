"""Concurrent upload/index completion over the production SQL transaction graph."""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker

from oce.application.commands.ingest import (
    BlobIngest,
    EmbedPendingCommand,
    EmbedPendingCommandHandler,
    IngestBlobsCommand,
    IngestBlobsCommandHandler,
    build_pipeline_factory,
)
from oce.application.worker import EmbedWorker
from oce.domain.blob.blob import Blob, BlobStatus
from oce.domain.chunk import RecursiveChunker
from oce.infrastructure.persistence.sql_blob_repo import SqlBlobRepository
from oce.infrastructure.persistence.uow import SqlAlchemyUnitOfWork
from oce.infrastructure.regex_symbol_provider import RegexSymbolProvider
from oce.shared.config.settings import DatabaseSettings
from oce.shared.database.session import Base, create_engine
from tests.fakes.indexing import ConstantEmbedder, RecordingVectorIndex
from tests.fakes.queue import FakeQueue
from tests.unit.application.fakes import blob_name


async def test_reupload_cannot_restore_pending_after_worker_completion(
    tmp_path, monkeypatch
):
    engine = create_engine(
        DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path / 'metadata.db'}")
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    def uow_factory():
        return SqlAlchemyUnitOfWork(sessions, RegexSymbolProvider())

    vectors = RecordingVectorIndex()
    pipelines = build_pipeline_factory(
        chunker=RecursiveChunker(),
        embedder=ConstantEmbedder(),
        vector_index=vectors,
        lexical_enabled=False,
    )
    path, content = "src/a.py", "def a(): return 1"
    name = blob_name(path, content)
    command = IngestBlobsCommand((BlobIngest(name, path, content),))
    await IngestBlobsCommandHandler(uow_factory, pipelines).handle(command)
    queue = FakeQueue(processing=[name])

    upload_loaded = asyncio.Event()
    resume_upload = asyncio.Event()
    worker_committed = asyncio.Event()
    resume_ack = asyncio.Event()
    original_get = SqlBlobRepository.get
    original_ack = queue.ack

    async def gated_get(repo, requested_name):
        result = await original_get(repo, requested_name)
        if asyncio.current_task() is upload_task and not upload_loaded.is_set():
            upload_loaded.set()
            await resume_upload.wait()
        return result

    async def gated_ack(requested_name):
        worker_committed.set()
        await resume_ack.wait()
        await original_ack(requested_name)

    monkeypatch.setattr(SqlBlobRepository, "get", gated_get)
    monkeypatch.setattr(queue, "ack", gated_ack)
    upload_task = asyncio.create_task(
        IngestBlobsCommandHandler(uow_factory, pipelines, queue).handle(command)
    )
    worker = EmbedWorker(
        queue=queue, uow_factory=uow_factory, pipeline_factory=pipelines
    )
    worker_task = None
    try:
        await asyncio.wait_for(upload_loaded.wait(), 2)
        worker_task = asyncio.create_task(worker._process_batch(0, [name]))
        await asyncio.wait_for(worker_committed.wait(), 2)
        resume_upload.set()
        await upload_task
        resume_ack.set()
        await worker_task

        async with uow_factory() as uow:
            blob = await uow.blobs.get(name)
            assert blob.status == BlobStatus.READY
            assert len(blob.chunks) == 1
            assert await uow.blobs.get_staging(name) is None
        assert len(vectors.records) == 1
        assert queue.pending == set()
        assert queue.main == []
    finally:
        resume_upload.set()
        resume_ack.set()
        tasks = [upload_task] + ([worker_task] if worker_task is not None else [])
        await asyncio.gather(*tasks, return_exceptions=True)
        await engine.dispose()


async def test_pending_name_pages_preserve_order_and_exclude_ready(tmp_path):
    engine = create_engine(
        DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path / 'metadata.db'}")
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    names = [f"{index:064x}" for index in range(6)]
    try:
        async with sessions() as session:
            repo = SqlBlobRepository(session)
            await repo.save_many(
                [
                    Blob(
                        name,
                        f"src/{index}.py",
                        status=(
                            BlobStatus.READY if name == names[2] else BlobStatus.PENDING
                        ),
                    )
                    for index, name in enumerate(reversed(names))
                ]
            )
            await session.commit()
        async with sessions() as session:
            repo = SqlBlobRepository(session)
            expected = [name for name in names if name != names[2]]
            assert await repo.list_pending_names() == expected
            collected = []
            after = None
            while page := await repo.list_pending_names(limit=2, after=after):
                assert len(page) <= 2
                collected.extend(page)
                after = page[-1]
            assert collected == expected
    finally:
        await engine.dispose()


async def test_metadata_writes_proceed_while_an_upload_waits_on_embedding(tmp_path):
    """The embedding round trip holds no metadata transaction.

    SQLite has one writer. Indexing used to keep its transaction open across
    the remote embedding call, so another request's write (an upload, a
    retrieval indexing its added files, the monitoring flush) waited out the
    busy timeout and failed with "database is locked".
    """
    engine = create_engine(
        DatabaseSettings(url=f"sqlite+aiosqlite:///{tmp_path / 'metadata.db'}")
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    def uow_factory():
        return SqlAlchemyUnitOfWork(sessions, RegexSymbolProvider())

    other_path, other_content = "src/other.py", "def other(): return 2"
    other_name = blob_name(other_path, other_content)
    concurrent_writes: list[str] = []

    class WritingEmbedder(ConstantEmbedder):
        async def embed_documents(self, texts: list[str]) -> list[list[float]]:
            # Another request uploads a file while this embedding is in flight.
            async with uow_factory() as uow:
                await uow.blobs.save(Blob(other_name, other_path))
                await uow.commit()
            concurrent_writes.append(other_name)
            return await super().embed_documents(texts)

    pipelines = build_pipeline_factory(
        chunker=RecursiveChunker(),
        embedder=WritingEmbedder(),
        vector_index=RecordingVectorIndex(),
        lexical_enabled=False,
    )
    path, content = "src/a.py", "def a(): return 1"
    name = blob_name(path, content)
    try:
        await IngestBlobsCommandHandler(uow_factory, pipelines).handle(
            IngestBlobsCommand((BlobIngest(name, path, content),))
        )
        # Bounded well below the 5 s busy timeout the old code waited out.
        result = await asyncio.wait_for(
            EmbedPendingCommandHandler(uow_factory, pipelines).handle(
                EmbedPendingCommand((name,))
            ),
            timeout=2,
        )

        assert result.embedded_count == 1
        assert concurrent_writes == [other_name]
        async with uow_factory() as uow:
            blob = await uow.blobs.get(name)
            assert blob is not None and blob.status == BlobStatus.READY
            assert await uow.blobs.get_staging(name) is None
            assert await uow.blobs.get(other_name) is not None
    finally:
        await engine.dispose()

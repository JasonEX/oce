"""Durable replay and queue maintenance under the worker's lifecycle owner."""

from __future__ import annotations

import asyncio

import pytest

from oce.application.commands.ingest import (
    BlobIngest,
    IngestBlobsCommand,
    IngestBlobsCommandHandler,
    build_pipeline_factory,
)
from oce.application.commands.queue_admin import (
    ResetQueueCommand,
    ResetQueueCommandHandler,
)
from oce.application.worker import EmbedWorker
from oce.domain.blob.blob import BlobStatus
from oce.domain.chunk import RecursiveChunker
from tests.fakes.indexing import ConstantEmbedder, RecordingVectorIndex
from tests.fakes.queue import FakeQueue
from tests.unit.application.fakes import FakeUnitOfWorkFactory, blob_name


def _runtime():
    factory = FakeUnitOfWorkFactory()
    queue = FakeQueue()
    embedder = ConstantEmbedder()
    pipelines = build_pipeline_factory(
        chunker=RecursiveChunker(),
        embedder=embedder,
        vector_index=RecordingVectorIndex(),
    )
    worker = EmbedWorker(
        queue=queue,
        uow_factory=factory,
        pipeline_factory=pipelines,
        concurrency=1,
    )
    return factory, queue, embedder, pipelines, worker


def _upload():
    path, content = "src/a.py", "def a(): return 1"
    name = blob_name(path, content)
    return name, IngestBlobsCommand((BlobIngest(name, path, content),))


def _on_ack(queue, monkeypatch):
    acknowledged = asyncio.Event()
    original = queue.ack

    async def ack(name):
        await original(name)
        acknowledged.set()

    monkeypatch.setattr(queue, "ack", ack)
    return acknowledged


async def _wait_for_pause(worker):
    while worker.is_running:
        await asyncio.sleep(0)


async def test_start_replays_committed_upload_after_enqueue_failure(monkeypatch):
    factory, queue, _, pipelines, worker = _runtime()
    name, command = _upload()
    original_enqueue = queue.enqueue

    async def unavailable(_name):
        raise OSError("Redis unavailable")

    monkeypatch.setattr(queue, "enqueue", unavailable)
    with pytest.raises(OSError, match="Redis unavailable"):
        await IngestBlobsCommandHandler(factory, pipelines, queue).handle(command)
    assert factory.uow.commits == 1
    assert name in factory.uow.blobs.staging
    monkeypatch.setattr(queue, "enqueue", original_enqueue)
    acknowledged = _on_ack(queue, monkeypatch)
    try:
        await worker.start()
        await asyncio.wait_for(acknowledged.wait(), 2)
        assert factory.uow.blobs.blobs[name].status == BlobStatus.READY
        assert queue.acked == [name]
    finally:
        await worker.stop()


async def test_runtime_replay_recovers_upload_without_restarting(monkeypatch):
    monkeypatch.setattr("oce.application.worker._REPLAY_INTERVAL_SECONDS", 0.01)
    factory, queue, _, pipelines, worker = _runtime()
    name, command = _upload()
    original_enqueue = queue.enqueue

    async def fail_once(requested_name):
        monkeypatch.setattr(queue, "enqueue", original_enqueue)
        raise OSError("Redis unavailable")

    acknowledged = _on_ack(queue, monkeypatch)
    try:
        await worker.start()
        monkeypatch.setattr(queue, "enqueue", fail_once)
        with pytest.raises(OSError, match="Redis unavailable"):
            await IngestBlobsCommandHandler(factory, pipelines, queue).handle(command)
        await asyncio.wait_for(acknowledged.wait(), 2)
        assert factory.uow.blobs.blobs[name].status == BlobStatus.READY
        assert queue.acked == [name]
    finally:
        await worker.stop()
    assert worker._tasks == []
    assert worker._replay_task is None


@pytest.mark.parametrize("failure_stage", ["recovery", "replay"])
async def test_failed_start_can_be_retried(failure_stage, monkeypatch):
    factory, queue, _, pipelines, worker = _runtime()
    name, command = _upload()
    await IngestBlobsCommandHandler(factory, pipelines).handle(command)
    method = "recover_processing" if failure_stage == "recovery" else "enqueue"
    original = getattr(queue, method)

    async def unavailable(*_args):
        raise OSError("Redis unavailable")

    monkeypatch.setattr(queue, method, unavailable)
    with pytest.raises(OSError, match="Redis unavailable"):
        await worker.start()
    assert not worker.is_running
    assert worker._tasks == []
    assert worker._replay_task is None

    monkeypatch.setattr(queue, method, original)
    acknowledged = _on_ack(queue, monkeypatch)
    try:
        await worker.start()
        await asyncio.wait_for(acknowledged.wait(), 2)
        assert factory.uow.blobs.blobs[name].status == BlobStatus.READY
    finally:
        await worker.stop()


async def test_replay_pages_do_not_duplicate_processing_work(monkeypatch):
    monkeypatch.setattr("oce.application.worker._REPLAY_PAGE_SIZE", 2)
    factory, queue, _, pipelines, worker = _runtime()
    names = []
    for index in range(5):
        path, content = f"src/{index}.py", f"def a{index}(): return 1"
        name = blob_name(path, content)
        names.append(name)
        await IngestBlobsCommandHandler(factory, pipelines).handle(
            IngestBlobsCommand((BlobIngest(name, path, content),))
        )
    await queue.enqueue(names[0])
    await queue.dequeue_many(1)
    await worker._replay_pending()
    assert queue.processing == [names[0]]
    assert sorted(queue.main) == sorted(names[1:])
    assert queue.pending == set(names)


async def test_replay_caps_work_and_resumes_then_wraps_cursor(monkeypatch):
    monkeypatch.setattr("oce.application.worker._REPLAY_PAGE_SIZE", 2)
    monkeypatch.setattr("oce.application.worker._REPLAY_MAX_PAGES", 2)
    factory, queue, _, pipelines, worker = _runtime()
    for index in range(7):
        path, content = f"src/{index}.py", f"def a{index}(): return 1"
        name = blob_name(path, content)
        await IngestBlobsCommandHandler(factory, pipelines).handle(
            IngestBlobsCommand((BlobIngest(name, path, content),))
        )
    names = sorted(factory.uow.blobs.blobs)
    pages = []
    original = factory.uow.blobs.list_pending_names

    async def read_page(*, limit=None, after=None):
        pages.append((limit, after))
        return await original(limit=limit, after=after)

    monkeypatch.setattr(factory.uow.blobs, "list_pending_names", read_page)
    await worker._replay_pending()
    assert queue.pending == set(names[:4])
    assert pages == [(2, None), (2, names[1])]
    assert worker._replay_after == names[3]

    await worker._replay_pending()
    assert queue.pending == set(names)
    assert pages[2:] == [(2, names[3]), (2, names[5])]
    assert worker._replay_after is None

    await worker._replay_pending()
    assert pages[4] == (2, None)
    assert len(queue.main) == len(names)


async def test_failure_commits_retry_state_before_releasing_delivery(monkeypatch):
    factory, queue, _, pipelines, worker = _runtime()
    name, command = _upload()
    await IngestBlobsCommandHandler(factory, pipelines, queue).handle(command)
    await queue.dequeue_many(1)
    original_fail = queue.fail

    async def fail(requested_name):
        assert factory.uow.blobs.blobs[name].retry_count == 1
        assert factory.uow.commits == 2
        assert queue.processing == [name]
        await original_fail(requested_name)

    monkeypatch.setattr(queue, "fail", fail)
    await worker._handle_failure(0, name, RuntimeError("provider failed"))
    assert queue.main == [name]
    assert queue.processing == []


async def test_maintenance_finishes_active_batch_and_resumes_after_reset(monkeypatch):
    factory, queue, embedder, pipelines, worker = _runtime()
    name, command = _upload()
    await IngestBlobsCommandHandler(factory, pipelines, queue).handle(command)
    embedding = asyncio.Event()
    complete_embedding = asyncio.Event()
    original_embed = embedder.embed_documents

    async def embed(texts):
        embedding.set()
        await complete_embedding.wait()
        return await original_embed(texts)

    monkeypatch.setattr(embedder, "embed_documents", embed)
    handler = ResetQueueCommandHandler(
        factory,
        queue,
        worker_running=lambda: worker.is_running,
        maintenance=worker.maintenance,
    )
    reset = None
    try:
        await worker.start()
        await asyncio.wait_for(embedding.wait(), 2)
        reset = asyncio.create_task(handler.handle(ResetQueueCommand(mode="purge")))
        await asyncio.wait_for(_wait_for_pause(worker), 2)
        assert not reset.done()
        assert not worker.is_running
        assert queue.processing == [name]
        complete_embedding.set()
        result = await asyncio.wait_for(reset, 2)
        assert result.db_pending == 0
        assert factory.uow.blobs.blobs[name].status == BlobStatus.READY
        assert queue.acked == [name]
        assert worker.is_running
    finally:
        complete_embedding.set()
        if reset is not None:
            await asyncio.gather(reset, return_exceptions=True)
        await worker.stop()


async def test_maintenance_does_not_undo_immediate_no_requeue_policy(monkeypatch):
    factory, queue, _, pipelines, worker = _runtime()
    name, command = _upload()
    handler = ResetQueueCommandHandler(
        factory,
        queue,
        worker_running=lambda: worker.is_running,
        maintenance=worker.maintenance,
    )

    async def idle_consumer(_worker_id):
        await asyncio.Event().wait()

    monkeypatch.setattr(worker, "_loop", idle_consumer)
    try:
        await worker.start()
        # Exercise maintenance after the worker was already running, without
        # a consumer owning this newly committed delivery yet.
        worker._tasks[0].cancel()
        await asyncio.gather(*worker._tasks, return_exceptions=True)
        await IngestBlobsCommandHandler(factory, pipelines, queue).handle(command)
        result = await handler.handle(ResetQueueCommand(mode="purge", requeue=False))
        assert result.removed == 1
        assert result.requeued == 0
        assert queue.main == []
        assert factory.uow.blobs.blobs[name].status == BlobStatus.PENDING
        assert worker.is_running
    finally:
        await worker.stop()


async def test_maintenance_restores_worker_when_reset_fails(monkeypatch):
    factory, queue, _, _, worker = _runtime()
    handler = ResetQueueCommandHandler(
        factory,
        queue,
        worker_running=lambda: worker.is_running,
        maintenance=worker.maintenance,
    )

    async def unavailable():
        raise OSError("Redis unavailable")

    monkeypatch.setattr(queue, "purge", unavailable)
    try:
        await worker.start()
        with pytest.raises(OSError, match="Redis unavailable"):
            await handler.handle(ResetQueueCommand(mode="purge"))
        assert worker.is_running
        assert len(worker._tasks) == 1
    finally:
        await worker.stop()


async def test_cancelled_reset_drains_without_cancelling_batch_and_resumes(monkeypatch):
    factory, queue, embedder, pipelines, worker = _runtime()
    name, command = _upload()
    await IngestBlobsCommandHandler(factory, pipelines, queue).handle(command)
    embedding = asyncio.Event()
    complete_embedding = asyncio.Event()
    embedding_cancelled = asyncio.Event()
    original_embed = embedder.embed_documents

    async def embed(texts):
        embedding.set()
        try:
            await complete_embedding.wait()
        except asyncio.CancelledError:
            embedding_cancelled.set()
            raise
        return await original_embed(texts)

    monkeypatch.setattr(embedder, "embed_documents", embed)
    handler = ResetQueueCommandHandler(
        factory,
        queue,
        worker_running=lambda: worker.is_running,
        maintenance=worker.maintenance,
    )
    reset = None
    try:
        await worker.start()
        await asyncio.wait_for(embedding.wait(), 2)
        reset = asyncio.create_task(handler.handle(ResetQueueCommand(mode="purge")))
        await asyncio.wait_for(_wait_for_pause(worker), 2)
        reset.cancel()
        await asyncio.sleep(0)
        assert not reset.done()
        assert not embedding_cancelled.is_set()
        assert queue.processing == [name]
        complete_embedding.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(reset, 2)
        assert not embedding_cancelled.is_set()
        assert factory.uow.blobs.blobs[name].status == BlobStatus.READY
        assert queue.acked == [name]
        assert worker.is_running
    finally:
        complete_embedding.set()
        if reset is not None:
            await asyncio.gather(reset, return_exceptions=True)
        await worker.stop()

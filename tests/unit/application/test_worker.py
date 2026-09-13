"""Worker batching and retry-state tests."""

from __future__ import annotations

from oce.application.commands.ingest import (
    BlobIngest,
    IngestBlobsCommand,
    IngestBlobsCommandHandler,
    build_pipeline_factory,
)
from oce.application.worker import EmbedWorker
from oce.domain.blob.blob import BlobStatus
from oce.domain.chunk import RecursiveChunker
from tests.fakes.indexing import ConstantEmbedder
from tests.fakes.retrieval import FakeSearchStore
from tests.unit.application.fakes import (
    FakeUnitOfWorkFactory,
    blob_name,
)


class FailingEmbedder:
    async def embed_documents(self, _texts):
        raise RuntimeError("provider failed")


class RecordingEmbedder(ConstantEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.document_calls: list[list[str]] = []

    async def embed_documents(self, texts):
        self.document_calls.append(list(texts))
        return await super().embed_documents(texts)


class SelectiveFailingEmbedder(RecordingEmbedder):
    async def embed_documents(self, texts):
        values = list(texts)
        self.document_calls.append(values)
        if any("poison" in text for text in values):
            raise RuntimeError("poison input")
        return [[1.0] * 4 for _ in values]


class RetryQueue:
    def __init__(self, blob_names: list[str]) -> None:
        self.blob_names = blob_names
        self.worker = None
        self.dequeue_count = 0
        self.acked: list[str] = []
        self.failed: list[str] = []
        self.enqueued: list[str] = []

    async def dequeue_many(self, max_items: int, timeout=5):
        self.dequeue_count += 1
        if self.dequeue_count == 1:
            return self.blob_names[:max_items]
        self.worker._running = False
        return []

    async def ack(self, blob_name: str) -> None:
        self.acked.append(blob_name)

    async def fail(self, blob_name: str) -> None:
        self.failed.append(blob_name)

    async def enqueue(self, blob_name: str) -> None:
        self.enqueued.append(blob_name)


async def _ingest(
    factory: FakeUnitOfWorkFactory,
    path: str,
    content: str,
) -> str:
    name = blob_name(path, content)
    pipelines = build_pipeline_factory(
        chunker=RecursiveChunker(),
        embedder=ConstantEmbedder(),
        vector_index=FakeSearchStore(),
    )
    await IngestBlobsCommandHandler(factory, pipelines).handle(
        IngestBlobsCommand((BlobIngest(name, path, content),))
    )
    return name


async def _run_failure(max_retries: int):
    factory = FakeUnitOfWorkFactory()
    path = "src/failing.py"
    content = "print('failing')"
    name = await _ingest(factory, path, content)
    queue = RetryQueue([name])
    worker = EmbedWorker(
        queue=queue,
        uow_factory=factory,
        pipeline_factory=build_pipeline_factory(
            chunker=RecursiveChunker(),
            embedder=FailingEmbedder(),
            vector_index=FakeSearchStore(),
        ),
        max_retries=max_retries,
    )
    queue.worker = worker
    worker._running = True
    await worker._loop(0)
    return factory, queue, name


async def test_worker_embeds_multiple_blobs_in_one_model_batch():
    factory = FakeUnitOfWorkFactory()
    names = [
        await _ingest(factory, "src/one.py", "def one(): pass"),
        await _ingest(factory, "src/two.py", "def two(): pass"),
    ]
    embedder = RecordingEmbedder()
    queue = RetryQueue(names)
    worker = EmbedWorker(
        queue=queue,
        uow_factory=factory,
        pipeline_factory=build_pipeline_factory(
            chunker=RecursiveChunker(),
            embedder=embedder,
            vector_index=FakeSearchStore(),
        ),
        blob_batch_size=16,
    )
    queue.worker = worker
    worker._running = True

    await worker._loop(0)

    assert [len(call) for call in embedder.document_calls] == [2]
    assert queue.acked == names
    assert queue.failed == []
    assert all(
        factory.uow.blobs.blobs[name].status == BlobStatus.READY for name in names
    )


async def test_worker_isolates_failed_batch_without_penalizing_healthy_blob():
    factory = FakeUnitOfWorkFactory()
    healthy = await _ingest(factory, "src/healthy.py", "def healthy(): pass")
    poison = await _ingest(factory, "src/poison.py", "def poison(): pass")
    embedder = SelectiveFailingEmbedder()
    queue = RetryQueue([healthy, poison])
    worker = EmbedWorker(
        queue=queue,
        uow_factory=factory,
        pipeline_factory=build_pipeline_factory(
            chunker=RecursiveChunker(),
            embedder=embedder,
            vector_index=FakeSearchStore(),
        ),
        blob_batch_size=16,
        max_retries=2,
    )
    queue.worker = worker
    worker._running = True

    await worker._loop(0)

    assert [len(call) for call in embedder.document_calls] == [2, 1, 1]
    assert queue.acked == [healthy]
    assert queue.failed == [poison]
    assert queue.enqueued == [poison]
    assert factory.uow.blobs.blobs[healthy].status == BlobStatus.READY
    assert factory.uow.blobs.blobs[healthy].retry_count == 0
    assert factory.uow.blobs.blobs[poison].status == BlobStatus.PENDING
    assert factory.uow.blobs.blobs[poison].retry_count == 1


async def test_worker_requeues_pending_blob_before_retry_limit():
    factory, queue, name = await _run_failure(max_retries=2)

    blob = factory.uow.blobs.blobs[name]
    assert blob.status == BlobStatus.PENDING
    assert blob.retry_count == 1
    assert queue.failed == [name]
    assert queue.acked == []
    assert queue.enqueued == [name]
    assert name in factory.uow.blobs.staging


async def test_worker_marks_error_and_cleans_staging_at_retry_limit():
    factory, queue, name = await _run_failure(max_retries=1)

    blob = factory.uow.blobs.blobs[name]
    assert blob.status == BlobStatus.ERROR
    assert blob.error_message == "provider failed"
    assert queue.acked == []
    assert queue.enqueued == []
    assert name not in factory.uow.blobs.staging

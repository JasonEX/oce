"""索引命令处理器测试。"""

from __future__ import annotations

import pytest

from oce.application.commands.ingest import (
    BlobIngest,
    DeleteBlobsCommand,
    DeleteBlobsCommandHandler,
    EmbedPendingCommand,
    EmbedPendingCommandHandler,
    IngestBlobsCommand,
    IngestBlobsCommandHandler,
    build_pipeline_factory,
)
from oce.domain.blob.blob import Blob, BlobStatus
from oce.domain.chunk import RecursiveChunker
from tests.unit.application.fakes import (
    FakeEmbedder,
    FakeSearchStore,
    FakeUnitOfWorkFactory,
    blob_name,
)


@pytest.fixture
def dependencies():
    factory = FakeUnitOfWorkFactory()
    index = FakeSearchStore()
    pipelines = build_pipeline_factory(
        chunker=RecursiveChunker(chunk_size=6000, chunk_overlap=200),
        embedder=FakeEmbedder(),
        vector_index=index,
    )
    return factory, pipelines, index


class RecordingQueue:
    def __init__(self, factory):
        self.factory = factory
        self.enqueued: list[str] = []
        self.commit_counts: list[int] = []

    async def enqueue(self, blob_name: str) -> None:
        self.enqueued.append(blob_name)
        self.commit_counts.append(self.factory.uow.commits)


async def _ingest(factory, pipelines, name, path, content, queue=None):
    await IngestBlobsCommandHandler(factory, pipelines, queue).handle(
        IngestBlobsCommand((BlobIngest(name, path, content),))
    )


async def test_ingest_stages_pending_blob(dependencies):
    """异步模式：ingest 只写元数据和 staging，切块留给 embed_pending"""
    factory, pipelines, _ = dependencies
    content = "\n".join(f"line{i}" for i in range(100))
    name = blob_name("src/a.py", content)

    await _ingest(factory, pipelines, name, "src/a.py", content)

    assert factory.uow.blobs.blobs[name].status == BlobStatus.PENDING
    assert factory.uow.blobs.staging[name] == content
    assert factory.uow.commits == 1


async def test_ingest_enqueues_pending_blob_after_commit(dependencies):
    factory, pipelines, _ = dependencies
    queue = RecordingQueue(factory)
    content = "print('queued')"
    name = blob_name("src/queued.py", content)

    await _ingest(factory, pipelines, name, "src/queued.py", content, queue)

    assert queue.enqueued == [name]
    assert queue.commit_counts == [1]


async def test_ingest_does_not_stage_or_enqueue_ignored_blob(dependencies):
    factory, pipelines, _ = dependencies
    queue = RecordingQueue(factory)
    content = '{"version": 3}'
    name = blob_name("dist/app.js.map", content)

    await _ingest(factory, pipelines, name, "dist/app.js.map", content, queue)

    assert factory.uow.blobs.blobs[name].status == BlobStatus.READY
    assert name not in factory.uow.blobs.staging
    assert queue.enqueued == []


async def test_ingest_blank_content_is_ready(dependencies):
    """空内容在 embed_pending 后直接标记 READY"""
    factory, pipelines, _ = dependencies
    content = "\n\n   \n"
    name = blob_name("src/blank.py", content)

    await _ingest(factory, pipelines, name, "src/blank.py", content)
    assert factory.uow.blobs.blobs[name].status == BlobStatus.PENDING

    await EmbedPendingCommandHandler(factory, pipelines).handle(
        EmbedPendingCommand((name,))
    )
    assert factory.uow.blobs.blobs[name].status == BlobStatus.READY


async def test_embed_pending_writes_vector_and_marks_ready(dependencies):
    """embed_pending 完成切块、嵌入，并标记 blob 为 ready"""
    factory, pipelines, index = dependencies
    content = "print('hello')"
    path = "src/hello.py"
    name = blob_name(path, content)

    await _ingest(factory, pipelines, name, path, content)
    result = await EmbedPendingCommandHandler(factory, pipelines).handle(
        EmbedPendingCommand((name,))
    )

    assert result.embedded_count == 1
    assert factory.uow.blobs.blobs[name].status == BlobStatus.READY
    assert len(index.upserted) == 1
    assert index.upserted[0].blob_name == name


async def test_embed_handler_propagates_disabled_runtime_state(dependencies):
    factory, pipelines, index = dependencies
    content = "print('later')"
    path = "src/later.py"
    name = blob_name(path, content)
    await _ingest(factory, pipelines, name, path, content)
    disabled = build_pipeline_factory(
        chunker=RecursiveChunker(chunk_size=6000, chunk_overlap=200),
        embedder=FakeEmbedder(),
        vector_index=index,
        embedding_enabled=False,
    )

    result = await EmbedPendingCommandHandler(factory, disabled).handle(
        EmbedPendingCommand((name,))
    )

    assert result.embedded_count == 0
    assert factory.uow.blobs.blobs[name].status == BlobStatus.PENDING
    assert name in factory.uow.blobs.staging
    assert index.upserted == []


async def test_embed_pending_limits_vector_batches(dependencies):
    """embed_pending 成功处理多块内容"""
    factory, pipelines, _ = dependencies
    path = "src/large.py"
    # 每行约 10 字符，1000 行 > 6000 chunk_size，确保切成多块
    content = "\n".join(f"line{i}" for i in range(1000))
    name = blob_name(path, content)

    await _ingest(factory, pipelines, name, path, content)
    result = await EmbedPendingCommandHandler(factory, pipelines).handle(
        EmbedPendingCommand((name,))
    )

    assert result.embedded_count > 1
    assert factory.uow.blobs.blobs[name].status == BlobStatus.READY


async def test_embed_failure_commits_error_state(dependencies):
    """嵌入失败时，blob 状态标记为 ERROR 并提交"""

    class FailingEmbedder:
        async def embed_documents(self, _texts):
            raise RuntimeError("provider failed")

    factory, pipelines, index = dependencies
    content = "\n".join(f"print({i})" for i in range(50))
    path = "src/broken.py"
    name = blob_name(path, content)
    await _ingest(factory, pipelines, name, path, content)
    failing = build_pipeline_factory(
        chunker=RecursiveChunker(chunk_size=6000, chunk_overlap=200),
        embedder=FailingEmbedder(),
        vector_index=index,
    )

    with pytest.raises(RuntimeError, match="provider failed"):
        await EmbedPendingCommandHandler(factory, failing).handle(
            EmbedPendingCommand((name,))
        )

    assert factory.uow.blobs.blobs[name].status == BlobStatus.ERROR
    assert factory.uow.commits == 2  # ingest + error commit


async def test_delete_commits_metadata_before_deleting_vectors(dependencies):
    factory, _, index = dependencies
    name = blob_name("src/deleted.py", "content")
    factory.uow.blobs.blobs[name] = Blob(
        blob_name=name,
        path="src/deleted.py",
        status=BlobStatus.READY,
    )

    await DeleteBlobsCommandHandler(factory, index).handle(DeleteBlobsCommand((name,)))

    assert name not in factory.uow.blobs.blobs
    assert factory.uow.commits == 1
    assert index.deleted == [name]

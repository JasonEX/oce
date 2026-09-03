"""索引写路径命令。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from loguru import logger

from oce.application.messages import Command
from oce.application.queue import Queue
from oce.application.uow import UnitOfWork, UnitOfWorkFactory
from oce.domain.blob.blob import BlobStatus
from oce.domain.chunk import Chunker
from oce.domain.services.embedder import Embedder
from oce.domain.services.indexing import IndexingPipeline
from oce.domain.services.path_search import PathSearchStore
from oce.domain.services.search import VectorIndex

# 每个用例在自己的 UoW 内构造 pipeline，仓储绑定当前事务，协程间不共享可变状态。
PipelineFactory = Callable[[UnitOfWork], IndexingPipeline]


def build_pipeline_factory(
    *,
    chunker: Chunker,
    embedder: Embedder,
    vector_index: VectorIndex,
    path_store: PathSearchStore | None = None,
    embedding_enabled: bool = True,
    lexical_enabled: bool = True,
) -> PipelineFactory:
    def build(uow: UnitOfWork) -> IndexingPipeline:
        return IndexingPipeline(
            chunker=chunker,
            embedder=embedder,
            vector_index=vector_index,
            blob_repo=uow.blobs,
            chunk_repo=uow.chunks,
            symbol_projection=uow.symbols,
            path_store=path_store,
            embedding_enabled=embedding_enabled,
            lexical_projection=uow.lexical if lexical_enabled else None,
        )

    return build


@dataclass(frozen=True)
class BlobIngest:
    """One uploaded file inside an ``IngestBlobsCommand``."""

    blob_name: str
    path: str
    content: str


@dataclass(frozen=True)
class IngestBlobsCommand(Command):
    blobs: tuple[BlobIngest, ...]


class IngestBlobsCommandHandler:
    """Write metadata and staging for a batch in one transaction, then enqueue."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        pipeline_factory: PipelineFactory,
        queue: Queue | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._pipeline_factory = pipeline_factory
        self._queue = queue

    async def handle(self, command: IngestBlobsCommand) -> None:
        if not command.blobs:
            return
        pending_names: list[str] = []
        async with self._uow_factory() as uow:
            pipeline = self._pipeline_factory(uow)
            for blob in command.blobs:
                await pipeline.ingest(blob.blob_name, blob.path, blob.content)
                stored = await uow.blobs.get(blob.blob_name)
                if stored is not None and stored.status == BlobStatus.PENDING:
                    pending_names.append(blob.blob_name)
            await uow.commit()
        if self._queue is not None:
            for blob_name in pending_names:
                await self._queue.enqueue(blob_name)


@dataclass(frozen=True)
class EmbedPendingCommand(Command):
    blob_names: tuple[str, ...] | None = None


@dataclass(frozen=True)
class EmbedPendingResult:
    embedded_count: int


class EmbedPendingCommandHandler:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        pipeline_factory: PipelineFactory,
        *,
        blob_batch_size: int = 32,
    ) -> None:
        if blob_batch_size < 1:
            raise ValueError("blob_batch_size must be positive")
        self._uow_factory = uow_factory
        self._pipeline_factory = pipeline_factory
        self._blob_batch_size = blob_batch_size

    async def handle(self, command: EmbedPendingCommand) -> EmbedPendingResult:
        names = command.blob_names
        if names is None:
            groups: list[tuple[str, ...] | None] = [None]
        else:
            groups = [
                names[offset : offset + self._blob_batch_size]
                for offset in range(0, len(names), self._blob_batch_size)
            ]

        embedded = 0
        for group in groups:
            async with self._uow_factory() as uow:
                pipeline = self._pipeline_factory(uow)
                # 失败时 pipeline 已把 blob 置 error，提交后再抛出，错误状态才可见。
                try:
                    embedded += await pipeline.embed_pending(group)
                finally:
                    await uow.commit()
        return EmbedPendingResult(embedded)


@dataclass(frozen=True)
class DeleteBlobsCommand(Command):
    blob_names: tuple[str, ...]


class DeleteBlobsCommandHandler:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        vector_index: VectorIndex,
        path_store: PathSearchStore | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._vector_index = vector_index
        self._path_store = path_store

    async def handle(self, command: DeleteBlobsCommand) -> None:
        if not command.blob_names:
            return
        async with self._uow_factory() as uow:
            await uow.blobs.delete_many(command.blob_names)
            await uow.commit()
        await self._vector_index.delete(list(command.blob_names))
        if self._path_store is not None:
            try:
                await self._path_store.delete_by_blob_names(list(command.blob_names))
            except Exception as exc:
                # 路径索引删除失败不阻塞删除主流程，仅记日志
                logger.warning(
                    "path index delete failed for {} blobs: {}",
                    len(command.blob_names),
                    exc,
                )

"""The stable application API used by HTTP, the CLI and the evaluators."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from oce.application.commands.checkpoint import CheckpointCommand, CheckpointResult
from oce.application.commands.credentials import (
    ReloadEmbeddingCredentialsCommand,
    ReloadEmbeddingCredentialsResult,
)
from oce.application.commands.gc import GcCommand, GcResult
from oce.application.commands.ingest import (
    BlobIngest,
    EmbedPendingCommand,
    IngestBlobsCommand,
)
from oce.application.commands.queue_admin import ResetQueueCommand, ResetQueueResult
from oce.application.commands.requeue import RequeueStaleCommand, RequeueStaleResult
from oce.application.queries.index_stats import IndexStatsQuery
from oce.application.queries.queue import QueueStatusQuery, QueueStatusResult
from oce.application.queries.search import SearchQuery
from oce.application.queries.stats import MonitoringStatsQuery
from oce.application.queries.status import (
    BlobStatusQuery,
    BlobStatusResult,
    FindMissingQuery,
    FindMissingResult,
    ResolveScopeQuery,
    ResolveScopeResult,
)
from oce.application.use_cases import ApplicationCommands, ApplicationQueries
from oce.domain.services.formatter import format_retrieval
from oce.domain.services.search import SearchHit
from oce.shared.index_stats import IndexStats
from oce.shared.metrics_read import MonitoringStats
from oce.shared.model_credentials import (
    CredentialAdminStore,
    CredentialCreate,
    CredentialPatch,
    CredentialRecord,
)


def compute_blob_name(path: str, content: str) -> str:
    """The content address the ACE client computes: ``sha256(path + content)``."""
    return hashlib.sha256(f"{path}{content}".encode()).hexdigest()


@dataclass(frozen=True)
class BlobUpload:
    path: str
    content: str


@dataclass(frozen=True)
class BatchUploadResult:
    blob_names: tuple[str, ...]
    embedded_count: int


@dataclass(frozen=True)
class RetrievalResult:
    hits: tuple[SearchHit, ...]
    formatted_retrieval: str
    elapsed_ms: int


class RetrievalApplication:
    """Use-case orchestration across commands and queries; transports only map DTOs."""

    def __init__(
        self,
        commands: ApplicationCommands,
        queries: ApplicationQueries,
        *,
        credentials: CredentialAdminStore,
        background_indexing: bool = False,
        require_index_ready: Callable[[], None] | None = None,
    ) -> None:
        self._commands = commands
        self._queries = queries
        self._credentials = credentials
        self._background_indexing = background_indexing
        self._require_index_ready = require_index_ready

    async def find_missing(self, blob_names: list[str]) -> FindMissingResult:
        return await self._queries.find_missing.handle(
            FindMissingQuery(tuple(blob_names))
        )

    async def reload_embedding_credentials(
        self,
    ) -> ReloadEmbeddingCredentialsResult:
        return await self._commands.reload_credentials.handle(
            ReloadEmbeddingCredentialsCommand()
        )

    async def batch_upload(
        self,
        blobs: list[BlobUpload],
        *,
        checkpoint_id: str | None = None,
    ) -> BatchUploadResult:
        if self._require_index_ready is not None:
            self._require_index_ready()
        items = tuple(
            BlobIngest(
                compute_blob_name(blob.path, blob.content),
                blob.path,
                blob.content,
            )
            for blob in blobs
        )
        names = [item.blob_name for item in items]
        await self._commands.ingest.handle(IngestBlobsCommand(items))
        embedded_count = 0
        if not self._background_indexing:
            embedded = await self._commands.embed_pending.handle(
                EmbedPendingCommand(tuple(names))
            )
            embedded_count = embedded.embedded_count
        if checkpoint_id:
            # Register the uploaded blobs in the existing checkpoint chain; a
            # non-empty checkpoint_id only advances a chain, never creates one.
            await self._commands.checkpoint.handle(
                CheckpointCommand(checkpoint_id, tuple(names), ())
            )
        return BatchUploadResult(tuple(names), embedded_count)

    async def retrieve(
        self,
        information_request: str,
        *,
        checkpoint_id: str | None = None,
        added_blobs: list[str] | None = None,
        deleted_blobs: list[str] | None = None,
    ) -> RetrievalResult:
        started = time.perf_counter()
        added = tuple(added_blobs or ())
        deleted = tuple(deleted_blobs or ())
        scope = await self._prepare_scope(checkpoint_id, added, deleted)
        result = await self._queries.search.handle(
            SearchQuery(information_request, scope.scope, source="retrieval")
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return RetrievalResult(
            hits=tuple(result.hits),
            formatted_retrieval=format_retrieval(result.hits),
            elapsed_ms=elapsed_ms,
        )

    async def _prepare_scope(
        self,
        checkpoint_id: str | None,
        added: tuple[str, ...],
        deleted: tuple[str, ...],
    ) -> ResolveScopeResult:
        # deleted_blobs only narrows this request's scope; nothing is deleted on
        # the server. Physical cleanup is the GC command's job.
        if added and not self._background_indexing:
            await self._commands.embed_pending.handle(EmbedPendingCommand(added))
        return await self._queries.resolve_scope.handle(
            ResolveScopeQuery(checkpoint_id, added, deleted)
        )

    async def checkpoint(
        self,
        *,
        checkpoint_id: str | None,
        added_blobs: list[str],
        deleted_blobs: list[str],
    ) -> CheckpointResult:
        if self._require_index_ready is not None:
            self._require_index_ready()
        return await self._commands.checkpoint.handle(
            CheckpointCommand(
                checkpoint_id,
                tuple(added_blobs),
                tuple(deleted_blobs),
            )
        )

    async def blob_status(
        self,
        *,
        blob_names: list[str],
        checkpoint_id: str | None,
    ) -> BlobStatusResult:
        return await self._queries.blob_status.handle(
            BlobStatusQuery(tuple(blob_names), checkpoint_id)
        )

    async def monitoring_stats(self, *, window_hours: int = 24) -> MonitoringStats:
        return await self._queries.monitoring_stats.handle(
            MonitoringStatsQuery(window_hours)
        )

    async def index_stats(self) -> IndexStats:
        return await self._queries.index_stats.handle(IndexStatsQuery())

    async def queue_status(self) -> QueueStatusResult:
        return await self._queries.queue_status.handle(QueueStatusQuery())

    async def reset_queue(
        self, *, mode: Literal["sync", "purge"] = "sync", requeue: bool = True
    ) -> ResetQueueResult:
        return await self._commands.reset_queue.handle(ResetQueueCommand(mode, requeue))

    async def requeue_stale(
        self, *, stale_hours: int = 24, limit: int = 100
    ) -> RequeueStaleResult:
        return await self._commands.requeue_stale.handle(
            RequeueStaleCommand(stale_hours, limit)
        )

    async def run_gc(
        self, *, ttl_days: int = 30, dry_run: bool = True, limit: int = 1000
    ) -> GcResult:
        return await self._commands.gc.handle(GcCommand(ttl_days, dry_run, limit))

    async def list_credentials(self) -> list[CredentialRecord]:
        return await self._credentials.list()

    async def create_credential(self, data: CredentialCreate) -> CredentialRecord:
        return await self._credentials.create(data)

    async def update_credential(
        self, credential_id: int, changes: CredentialPatch
    ) -> CredentialRecord | None:
        return await self._credentials.update(credential_id, changes)

    async def delete_credential(self, credential_id: int) -> bool:
        return await self._credentials.delete(credential_id)

    async def duplicate_credential(
        self, credential_id: int, changes: CredentialPatch
    ) -> CredentialRecord | None:
        return await self._credentials.duplicate(credential_id, changes)

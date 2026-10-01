"""Explicit read and write use cases assembled by the composition root.

The application keeps command/query boundaries without runtime registration:
missing dependencies and incompatible handler results are type errors.
"""

from __future__ import annotations

from dataclasses import dataclass

from oce.application.commands.checkpoint import CheckpointCommandHandler
from oce.application.commands.credentials import (
    ReloadEmbeddingCredentialsCommandHandler,
)
from oce.application.commands.gc import GcCommandHandler
from oce.application.commands.ingest import (
    EmbedPendingCommandHandler,
    IngestBlobsCommandHandler,
)
from oce.application.commands.queue_admin import ResetQueueCommandHandler
from oce.application.commands.requeue import RequeueStaleCommandHandler
from oce.application.queries.index_stats import IndexStatsQueryHandler
from oce.application.queries.queue import QueueStatusQueryHandler
from oce.application.queries.search import SearchQueryHandler
from oce.application.queries.stats import MonitoringStatsQueryHandler
from oce.application.queries.status import (
    BlobStatusQueryHandler,
    FindMissingQueryHandler,
    ResolveScopeQueryHandler,
)


@dataclass(frozen=True)
class ApplicationCommands:
    ingest: IngestBlobsCommandHandler
    embed_pending: EmbedPendingCommandHandler
    checkpoint: CheckpointCommandHandler
    reload_credentials: ReloadEmbeddingCredentialsCommandHandler
    gc: GcCommandHandler
    requeue_stale: RequeueStaleCommandHandler
    reset_queue: ResetQueueCommandHandler


@dataclass(frozen=True)
class ApplicationQueries:
    find_missing: FindMissingQueryHandler
    blob_status: BlobStatusQueryHandler
    resolve_scope: ResolveScopeQueryHandler
    search: SearchQueryHandler
    monitoring_stats: MonitoringStatsQueryHandler
    index_stats: IndexStatsQueryHandler
    queue_status: QueueStatusQueryHandler

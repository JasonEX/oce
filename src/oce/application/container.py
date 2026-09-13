"""Process-level composition root.

Each subsystem is assembled by one builder that takes the settings it reads
and the session factory it binds to, and returns the objects it created as a
small immutable record. ``Container`` composes those records into the object
graph the ASGI lifespan starts, probes, stops and serves. Builders are plain
functions on purpose: a test can assemble the same graph against a temporary
database and an embedded vector store without touching process-global state.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache, partial

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from oce.application.bus import CommandBus, QueryBus
from oce.application.commands.checkpoint import (
    CheckpointCommand,
    CheckpointCommandHandler,
)
from oce.application.commands.credentials import (
    ReloadEmbeddingCredentialsCommand,
    ReloadEmbeddingCredentialsCommandHandler,
)
from oce.application.commands.gc import GcCommand, GcCommandHandler
from oce.application.commands.ingest import (
    DeleteBlobsCommand,
    DeleteBlobsCommandHandler,
    EmbedPendingCommand,
    EmbedPendingCommandHandler,
    IngestBlobsCommand,
    IngestBlobsCommandHandler,
    PipelineFactory,
    build_pipeline_factory,
)
from oce.application.commands.queue_admin import (
    ResetQueueCommand,
    ResetQueueCommandHandler,
)
from oce.application.commands.requeue import (
    RequeueStaleCommand,
    RequeueStaleCommandHandler,
)
from oce.application.credential_admin import (
    CreateCredentialCommand,
    CreateCredentialCommandHandler,
    DeleteCredentialCommand,
    DeleteCredentialCommandHandler,
    DuplicateCredentialCommand,
    DuplicateCredentialCommandHandler,
    ListCredentialsQuery,
    ListCredentialsQueryHandler,
    UpdateCredentialCommand,
    UpdateCredentialCommandHandler,
)
from oce.application.index_lifecycle import IndexLifecycleManager
from oce.application.queries.index_stats import IndexStatsQuery, IndexStatsQueryHandler
from oce.application.queries.queue import QueueStatusQuery, QueueStatusQueryHandler
from oce.application.queries.search import SearchQuery, SearchQueryHandler
from oce.application.queries.stats import (
    MonitoringStatsQuery,
    MonitoringStatsQueryHandler,
)
from oce.application.queries.status import (
    BlobStatusQuery,
    BlobStatusQueryHandler,
    FindMissingQuery,
    FindMissingQueryHandler,
    ResolveScopeQuery,
    ResolveScopeQueryHandler,
)
from oce.application.service import RetrievalApplication
from oce.application.uow import UnitOfWorkFactory
from oce.application.warmup import warm_retrieval_stores
from oce.application.worker import EmbedWorker
from oce.domain.chunk import Chunker
from oce.domain.services.llm.reranker import LLMReranker
from oce.domain.services.llm.rewriter import QueryRewriter
from oce.domain.services.reranker import Reranker
from oce.domain.services.retrieval import RetrievalPipeline
from oce.infrastructure.astchunk.symbol_provider import TreeSitterSymbolProvider
from oce.infrastructure.chunkers.factory import build_chunker
from oce.infrastructure.embed.credential_embedder import CredentialConfiguredEmbedder
from oce.infrastructure.embed.credential_reranker import CredentialConfiguredReranker
from oce.infrastructure.embed.local_onnx_reranker import LocalOnnxReranker
from oce.infrastructure.embed.query_cache import QueryCachingEmbedder
from oce.infrastructure.llm.credential_llm_client import CredentialConfiguredLLMClient
from oce.infrastructure.metrics.cleanup import MonitoringCleaner
from oce.infrastructure.metrics.resource_sampler import (
    ResourceSampler,
    build_psutil_collector,
)
from oce.infrastructure.metrics.sql_metrics_sink import SqlMetricsSink
from oce.infrastructure.metrics.stats_store import SqlMonitoringStatsReader
from oce.infrastructure.milvus3.path_index import PathIndexClient
from oce.infrastructure.milvus3.search_store import Milvus3SearchStore
from oce.infrastructure.persistence.credential_admin_store import (
    SqlCredentialAdminStore,
)
from oce.infrastructure.persistence.index_profile_store import SqlIndexProfileStore
from oce.infrastructure.persistence.index_stats_reader import (
    SqlMetadataIndexStatsReader,
)
from oce.infrastructure.persistence.lexical_index import SqlLexicalSearchStore
from oce.infrastructure.persistence.path_content_store import SqlPathContentStore
from oce.infrastructure.persistence.path_lookup_store import SqlPathLookupStore
from oce.infrastructure.persistence.symbol_search_store import SymbolSearchStore
from oce.infrastructure.persistence.uow import SqlAlchemyUnitOfWork
from oce.infrastructure.queue.redis_queue import RedisQueue
from oce.infrastructure.regex_symbol_provider import RegexSymbolProvider
from oce.shared.config import Settings, get_settings
from oce.shared.database.session import async_session_factory
from oce.shared.errors import ServiceNotReadyError
from oce.shared.index_profile import IndexDataProbe
from oce.shared.index_stats import RetrievalRuntimeProfile
from oce.shared.logging import DATA_DIR_ENV
from oce.shared.metrics import (
    ManagedMetricsSink,
    NoopMetricsSink,
    TokenUsageRecord,
    UsageCallback,
)

SessionFactory = async_sessionmaker[AsyncSession]


async def record_token_usage(
    metrics: ManagedMetricsSink,
    credential_id: int,
    kind: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> None:
    """Bridge model-client usage callbacks to the metrics sink.

    ``credential_id`` 0 means the client fell back to environment settings and
    has no credential row; it is stored as ``None``. Monitoring is a side
    channel, so any failure is logged and never raised to the caller.
    """
    try:
        metrics.record_token_usage(
            TokenUsageRecord(
                kind=kind,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
                credential_id=credential_id or None,
            )
        )
    except Exception as exc:
        logger.warning("record token usage failed: {}", exc)


class _CredentialRuntime:
    """Reload embedder and reranker credentials as one unit, then refresh LLM clients."""

    def __init__(
        self,
        embedder: CredentialConfiguredEmbedder,
        reranker: CredentialConfiguredReranker | None = None,
        llm_clients: Sequence[CredentialConfiguredLLMClient] = (),
        query_cache: QueryCachingEmbedder | None = None,
    ) -> None:
        self._embedder = embedder
        self._reranker = reranker
        self._llm_clients = tuple(llm_clients)
        self._query_cache = query_cache

    async def reload(self) -> None:
        embedding_replacement = await self._embedder.prepare_reload()
        # The reranker replacement exists exactly when a credential-backed
        # reranker is configured; both are discarded together on failure.
        rerank_replacement: tuple[CredentialConfiguredReranker, Reranker] | None
        rerank_replacement = None
        if self._reranker is not None:
            try:
                prepared = await self._reranker.prepare_reload()
            except Exception:
                await self._embedder.discard_prepared(embedding_replacement)
                raise
            rerank_replacement = (self._reranker, prepared)
        try:
            await self._embedder.validate_prepared(embedding_replacement)
        except Exception:
            await self._embedder.discard_prepared(embedding_replacement)
            if rerank_replacement is not None:
                await rerank_replacement[0].discard_prepared(rerank_replacement[1])
            raise
        try:
            await self._embedder.activate_prepared(embedding_replacement)
        except Exception:
            if rerank_replacement is not None:
                await rerank_replacement[0].discard_prepared(rerank_replacement[1])
            raise
        if self._query_cache is not None:
            await self._query_cache.clear_query_cache()
        if rerank_replacement is not None:
            await rerank_replacement[0].activate_prepared(rerank_replacement[1])
        # LLM clients have no prepare/activate phases: reload swaps the
        # delegate atomically. A failed refresh is logged and never rolls
        # back the embedder and reranker that were already activated.
        for client in self._llm_clients:
            try:
                await client.reload()
            except Exception as exc:
                logger.warning("LLM client reload failed: {}", exc)


# ── subsystem builders ───────────────────────────────────────────────────


@dataclass(frozen=True)
class MonitoringRuntime:
    """Side-channel collectors; every field is inert when monitoring is off."""

    metrics: ManagedMetricsSink
    resource_sampler: ResourceSampler | None
    cleaner: MonitoringCleaner | None
    # Passed to the model clients; None makes them skip the callback entirely.
    token_usage: UsageCallback | None


def build_monitoring(
    settings: Settings, session_factory: SessionFactory, *, data_dir: str | None
) -> MonitoringRuntime:
    monitoring = settings.monitoring
    if not monitoring.enabled:
        return MonitoringRuntime(NoopMetricsSink(), None, None, None)
    metrics = SqlMetricsSink(
        session_factory,
        flush_interval_seconds=monitoring.flush_interval_seconds,
        max_buffer=monitoring.flush_max_buffer,
    )
    return MonitoringRuntime(
        metrics=metrics,
        resource_sampler=ResourceSampler(
            metrics,
            interval_seconds=monitoring.resource_sample_interval_seconds,
            collector=build_psutil_collector(data_dir),
        ),
        cleaner=MonitoringCleaner(
            session_factory,
            retention_days=monitoring.retention_days,
            interval_seconds=monitoring.cleanup_interval_seconds,
        ),
        token_usage=partial(record_token_usage, metrics),
    )


@dataclass(frozen=True)
class VectorStores:
    """The Milvus collections plus the SQL store that gives path hits a chunk."""

    search_store: Milvus3SearchStore
    path_index: PathIndexClient | None
    path_content_store: SqlPathContentStore | None

    @property
    def artifact_probes(self) -> tuple[IndexDataProbe, ...]:
        probes: list[IndexDataProbe] = [self.search_store]
        if self.path_index is not None:
            probes.append(self.path_index)
        return tuple(probes)


def build_vector_stores(
    settings: Settings, session_factory: SessionFactory
) -> VectorStores:
    # The single source of the vector dimension: both collections and the
    # credential validation read it from EMBED_DIMENSIONS.
    dense_dim = settings.embedding.dimensions
    path_index = None
    path_content_store = None
    if settings.retrieval.path_index_enabled:
        path_index = PathIndexClient(settings.milvus, dense_dim=dense_dim)
        path_content_store = SqlPathContentStore(session_factory)
    return VectorStores(
        search_store=Milvus3SearchStore(settings.milvus, dense_dim=dense_dim),
        path_index=path_index,
        path_content_store=path_content_store,
    )


@dataclass(frozen=True)
class ModelRuntime:
    """Every model client, each resolved lazily from ``model_credentials``."""

    embedding_runtime: CredentialConfiguredEmbedder
    embedder: QueryCachingEmbedder
    # ``RERANK_ENABLED`` authorizes the dedicated stage; the provider decides
    # whether data leaves the process. None means the stage is not authorized,
    # which the pipeline audit tells apart from a policy skip.
    reranker: LocalOnnxReranker | CredentialConfiguredReranker | None
    credential_reranker: CredentialConfiguredReranker | None
    llm_clients: tuple[CredentialConfiguredLLMClient, ...]
    llm_reranker: LLMReranker | None
    query_rewriter: QueryRewriter | None
    credentials: _CredentialRuntime

    async def close(self) -> None:
        await self.embedder.close()
        if self.reranker is not None:
            await self.reranker.close()
        for client in self.llm_clients:
            await client.close()


def build_model_runtime(
    settings: Settings,
    session_factory: SessionFactory,
    *,
    token_usage: UsageCallback | None,
    index_lifecycle: IndexLifecycleManager,
) -> ModelRuntime:
    embedding_runtime = CredentialConfiguredEmbedder(
        session_factory,
        settings.embedding,
        expected_dimensions=settings.embedding.dimensions,
        on_usage=token_usage,
        on_index_profile=index_lifecycle.ensure_compatible,
    )
    embedder = QueryCachingEmbedder(
        embedding_runtime,
        max_entries=settings.embedding.query_cache_max_entries,
        ttl_seconds=settings.embedding.query_cache_ttl_seconds,
    )

    rerank = settings.rerank
    reranker: LocalOnnxReranker | CredentialConfiguredReranker | None = None
    credential_reranker: CredentialConfiguredReranker | None = None
    if rerank.enabled and rerank.provider == "local":
        reranker = LocalOnnxReranker(
            model_dir=rerank.local_model_dir,
            model_file=rerank.local_model_file,
            candidates=rerank.local_candidates,
            max_doc_chars=rerank.local_max_doc_chars,
            max_query_chars=rerank.max_query_chars,
            max_tokens=rerank.local_max_tokens,
            batch_size=rerank.local_batch_size,
            threads=rerank.local_threads,
        )
    elif rerank.enabled:
        credential_reranker = reranker = CredentialConfiguredReranker(
            session_factory,
            rerank,
            fallback_embedding_key=(
                settings.embedding.api_key.get_secret_value()
                if settings.embedding.api_key is not None
                else None
            ),
            on_usage=token_usage,
        )

    # LLM reranking and query rewriting resolve credentials by their own
    # kind (with the LLM_* settings as the fallback), so they are configured
    # independently of each other.
    llm_clients: list[CredentialConfiguredLLMClient] = []
    llm_reranker: LLMReranker | None = None
    if settings.llm.rerank_enabled:
        rerank_llm = CredentialConfiguredLLMClient(
            "llm_rerank",
            session_factory,
            settings.llm,
            fallback_model=settings.llm.model,
            on_usage=token_usage,
        )
        llm_clients.append(rerank_llm)
        llm_reranker = LLMReranker(
            client=rerank_llm,
            model=settings.llm.model,
            max_candidates=settings.llm.max_candidates,
            output_top_k=settings.llm.output_top_k,
            snippet_chars=settings.llm.snippet_chars,
            timeout_seconds=settings.llm.rerank_timeout_seconds,
        )
    query_rewriter: QueryRewriter | None = None
    if settings.retrieval.query_rewrite_enabled:
        rewrite_llm = CredentialConfiguredLLMClient(
            "query_rewrite",
            session_factory,
            settings.llm,
            fallback_model=settings.retrieval.query_rewrite_model,
            on_usage=token_usage,
        )
        llm_clients.append(rewrite_llm)
        query_rewriter = QueryRewriter(
            client=rewrite_llm,
            model=settings.retrieval.query_rewrite_model,
            num_rewrites=settings.retrieval.query_rewrite_num,
        )
    return ModelRuntime(
        embedding_runtime=embedding_runtime,
        embedder=embedder,
        reranker=reranker,
        credential_reranker=credential_reranker,
        llm_clients=tuple(llm_clients),
        llm_reranker=llm_reranker,
        query_rewriter=query_rewriter,
        credentials=_CredentialRuntime(
            embedding_runtime,
            credential_reranker,
            llm_clients,
            query_cache=embedder,
        ),
    )


@dataclass(frozen=True)
class IndexingRuntime:
    """The write path: symbol extraction, chunking and per-transaction pipelines."""

    symbol_provider: TreeSitterSymbolProvider
    chunker: Chunker
    uow_factory: UnitOfWorkFactory
    pipeline_factory: PipelineFactory


def build_indexing_runtime(
    settings: Settings,
    session_factory: SessionFactory,
    *,
    models: ModelRuntime,
    stores: VectorStores,
) -> IndexingRuntime:
    # tree-sitter reads definitions and imports with real spans; regex stays
    # the fallback for grammars the pack cannot load and detects endpoints.
    symbol_provider = TreeSitterSymbolProvider(RegexSymbolProvider())
    chunker = build_chunker(
        semantic_enabled=settings.chunking.semantic_enabled,
        semantic_max_chunk_chars=settings.chunking.semantic_max_chunk_chars,
        recursive_chunk_size=settings.chunking.recursive_chunk_size,
        recursive_chunk_overlap=settings.chunking.recursive_chunk_overlap,
    )

    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory, symbol_provider)

    return IndexingRuntime(
        symbol_provider=symbol_provider,
        chunker=chunker,
        uow_factory=uow_factory,
        pipeline_factory=build_pipeline_factory(
            chunker=chunker,
            embedder=models.embedder,
            vector_index=stores.search_store,
            path_store=stores.path_index,
            embedding_enabled=settings.embedding.enabled,
            lexical_enabled=settings.retrieval.lexical_enabled,
        ),
    )


def build_retrieval_pipeline(
    settings: Settings,
    session_factory: SessionFactory,
    *,
    models: ModelRuntime,
    stores: VectorStores,
    lexical_store: SqlLexicalSearchStore | None,
) -> RetrievalPipeline:
    # One store serves both the exact lane and the relation lanes; they read
    # the same occurrence table with the same timeout.
    symbol_store = SymbolSearchStore(
        session_factory,
        timeout_seconds=settings.retrieval.exact_timeout_seconds,
    )
    return RetrievalPipeline(
        embedder=models.embedder,
        store=stores.search_store,
        reranker=models.reranker,
        llm_reranker=models.llm_reranker,
        # The LLM reranker's window: exact hits must land inside it to be
        # reranked at all, so the merge step reserves them a share.
        rerank_window=(
            settings.llm.max_candidates if models.llm_reranker is not None else None
        ),
        query_rewriter=models.query_rewriter,
        path_store=stores.path_index,
        path_content_store=stores.path_content_store,
        exact_store=symbol_store,
        relation_store=symbol_store,
        lexical_store=lexical_store,
        path_lookup_store=(
            SqlPathLookupStore(session_factory)
            if settings.retrieval.path_lookup_enabled
            else None
        ),
        settings=settings.retrieval,
    )


def _build_redis_queue(settings: Settings) -> RedisQueue:
    import redis.asyncio as redis

    client = redis.from_url(
        settings.redis.url,
        decode_responses=True,
        encoding="utf-8",
        max_connections=20,  # 8 workers plus headroom
        socket_timeout=10.0,
        socket_connect_timeout=5.0,
        socket_keepalive=True,
        health_check_interval=30,
        retry_on_timeout=True,
    )
    return RedisQueue(client, settings.redis.queue_name)


def _worker_concurrency(settings: Settings) -> int:
    """Worker concurrency stays below the DB pool (one connection is kept for
    requests) and the embedding concurrency limit."""
    db_worker_capacity = max(1, settings.database.pool_size - 1)
    concurrency = min(
        settings.worker.concurrency,
        db_worker_capacity,
        settings.embedding.max_concurrency,
    )
    if concurrency != settings.worker.concurrency:
        logger.warning(
            "Worker concurrency reduced from {} to {} to match DB and embedding limits",
            settings.worker.concurrency,
            concurrency,
        )
    return concurrency


def _runtime_profile(settings: Settings) -> RetrievalRuntimeProfile:
    retrieval = settings.retrieval
    rerank = settings.rerank
    local = rerank.enabled and rerank.provider == "local"
    return RetrievalRuntimeProfile(
        embedding_enabled=settings.embedding.enabled,
        embedding_query_char_limit=settings.embedding.max_query_chars,
        semantic_chunking_enabled=settings.chunking.semantic_enabled,
        exact_enabled=retrieval.exact_enabled,
        path_index_enabled=retrieval.path_index_enabled,
        source_priority_enabled=retrieval.source_priority_enabled,
        coverage_selection_enabled=retrieval.coverage_selection_enabled,
        query_decomposition_enabled=retrieval.query_decomposition_enabled,
        lexical_enabled=retrieval.lexical_enabled,
        path_lookup_enabled=retrieval.path_lookup_enabled,
        merge_adjacent_enabled=retrieval.merge_adjacent_enabled,
        related_definitions_enabled=retrieval.related_definitions_enabled,
        rerank_enabled=rerank.enabled,
        rerank_provider=rerank.provider if rerank.enabled else "none",
        rerank_candidate_limit=(
            rerank.local_candidates if local else rerank.top_n if rerank.enabled else 0
        ),
        rerank_query_char_limit=rerank.max_query_chars if rerank.enabled else 0,
        rerank_document_char_limit=rerank.local_max_doc_chars if local else 0,
        rerank_token_limit=rerank.local_max_tokens if local else 0,
        rerank_policy=retrieval.rerank_policy,
        llm_rerank_enabled=settings.llm.rerank_enabled,
        llm_rerank_policy=retrieval.llm_rerank_policy,
        query_rewrite_enabled=retrieval.query_rewrite_enabled,
        decisive_skips_dense=retrieval.decisive_skips_dense,
    )


# ── the container ────────────────────────────────────────────────────────


class Container:
    """The object graph of one process; ``get_container`` holds the one instance.

    ``settings`` and ``session_factory`` default to the process configuration
    and the shared engine; a test passes its own to assemble the same graph
    against a temporary database.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        session_factory: SessionFactory | None = None,
    ) -> None:
        settings = settings if settings is not None else get_settings()
        sessions = (
            session_factory if session_factory is not None else async_session_factory
        )
        self._settings = settings
        # Exposed for probes that read the metadata store the graph writes to.
        self.session_factory = sessions

        monitoring = build_monitoring(
            settings, sessions, data_dir=os.environ.get(DATA_DIR_ENV)
        )
        self.metrics = monitoring.metrics
        self.resource_sampler = monitoring.resource_sampler
        self.monitoring_cleaner = monitoring.cleaner

        stores = build_vector_stores(settings, sessions)
        self.search_store = stores.search_store
        self.path_index = stores.path_index
        self.path_content_store = stores.path_content_store
        self.index_lifecycle = IndexLifecycleManager(
            SqlIndexProfileStore(sessions), settings, stores.artifact_probes
        )

        models = build_model_runtime(
            settings,
            sessions,
            token_usage=monitoring.token_usage,
            index_lifecycle=self.index_lifecycle,
        )
        self._models = models
        self.embedding_runtime = models.embedding_runtime
        self.embedder = models.embedder
        self.reranker = models.reranker
        self.credential_reranker = models.credential_reranker
        self.llm_clients = list(models.llm_clients)
        self.llm_reranker = models.llm_reranker
        self.query_rewriter = models.query_rewriter

        indexing = build_indexing_runtime(
            settings, sessions, models=models, stores=stores
        )
        self.symbol_provider = indexing.symbol_provider
        self.chunker = indexing.chunker
        self._uow_factory = indexing.uow_factory

        self.queue: RedisQueue | None = None
        self.worker: EmbedWorker | None = None
        if settings.worker.enabled:
            self.queue = _build_redis_queue(settings)
            self.worker = EmbedWorker(
                queue=self.queue,
                uow_factory=indexing.uow_factory,
                pipeline_factory=indexing.pipeline_factory,
                concurrency=_worker_concurrency(settings),
                blob_batch_size=settings.worker.blob_batch_size,
                max_retries=settings.worker.max_retries,
            )

        self.lexical_store: SqlLexicalSearchStore | None = (
            SqlLexicalSearchStore(sessions)
            if settings.retrieval.lexical_enabled
            else None
        )
        self.application = RetrievalApplication(
            self._build_command_bus(settings, sessions, indexing),
            self._build_query_bus(settings, sessions, indexing, models, stores),
            background_indexing=self.queue is not None,
        )

    def _build_command_bus(
        self,
        settings: Settings,
        sessions: SessionFactory,
        indexing: IndexingRuntime,
    ) -> CommandBus:
        uow_factory = indexing.uow_factory
        delete_blobs = DeleteBlobsCommandHandler(
            uow_factory, self.search_store, path_store=self.path_index
        )
        credential_admin_store = SqlCredentialAdminStore(sessions)
        bus = CommandBus()
        bus.register(
            IngestBlobsCommand,
            IngestBlobsCommandHandler(
                uow_factory, indexing.pipeline_factory, self.queue
            ),
        )
        bus.register(
            EmbedPendingCommand,
            EmbedPendingCommandHandler(
                uow_factory,
                indexing.pipeline_factory,
                blob_batch_size=settings.worker.blob_batch_size,
            ),
        )
        bus.register(DeleteBlobsCommand, delete_blobs)
        bus.register(
            ReloadEmbeddingCredentialsCommand,
            ReloadEmbeddingCredentialsCommandHandler(self._models.credentials),
        )
        bus.register(CheckpointCommand, CheckpointCommandHandler(uow_factory))
        bus.register(
            RequeueStaleCommand, RequeueStaleCommandHandler(uow_factory, self.queue)
        )
        bus.register(
            ResetQueueCommand,
            ResetQueueCommandHandler(
                uow_factory,
                self.queue,
                worker_running=lambda: (
                    self.worker is not None and self.worker.is_running
                ),
            ),
        )
        bus.register(
            CreateCredentialCommand,
            CreateCredentialCommandHandler(credential_admin_store),
        )
        bus.register(
            UpdateCredentialCommand,
            UpdateCredentialCommandHandler(credential_admin_store),
        )
        bus.register(
            DeleteCredentialCommand,
            DeleteCredentialCommandHandler(credential_admin_store),
        )
        bus.register(
            DuplicateCredentialCommand,
            DuplicateCredentialCommandHandler(credential_admin_store),
        )
        bus.register(GcCommand, GcCommandHandler(uow_factory, delete_blobs, self.queue))
        return bus

    def _build_query_bus(
        self,
        settings: Settings,
        sessions: SessionFactory,
        indexing: IndexingRuntime,
        models: ModelRuntime,
        stores: VectorStores,
    ) -> QueryBus:
        uow_factory = indexing.uow_factory
        monitoring = settings.monitoring
        bus = QueryBus()
        bus.register(
            SearchQuery,
            SearchQueryHandler(
                build_retrieval_pipeline(
                    settings,
                    sessions,
                    models=models,
                    stores=stores,
                    lexical_store=self.lexical_store,
                ),
                metrics=self.metrics,
                retrieval_audit_enabled=(
                    monitoring.enabled and monitoring.retrieval_audit_enabled
                ),
                store_query_text=monitoring.store_query_text,
            ),
        )
        bus.register(FindMissingQuery, FindMissingQueryHandler(uow_factory))
        bus.register(BlobStatusQuery, BlobStatusQueryHandler(uow_factory))
        bus.register(ResolveScopeQuery, ResolveScopeQueryHandler(uow_factory))
        bus.register(
            MonitoringStatsQuery,
            MonitoringStatsQueryHandler(SqlMonitoringStatsReader(sessions)),
        )
        bus.register(
            IndexStatsQuery,
            IndexStatsQueryHandler(
                SqlMetadataIndexStatsReader(sessions),
                self.search_store,
                self.path_index,
                self.embedder,
                _runtime_profile(settings),
                self.index_lifecycle,
            ),
        )
        bus.register(
            ListCredentialsQuery,
            ListCredentialsQueryHandler(SqlCredentialAdminStore(sessions)),
        )
        bus.register(QueueStatusQuery, QueueStatusQueryHandler(uow_factory, self.queue))
        return bus

    async def ensure_index_compatible(self) -> bool:
        """Validate persisted artifacts before workers or data-plane traffic start."""
        if not self.embedding_runtime.enabled:
            await self.index_lifecycle.ensure_compatible(
                await self.embedding_runtime.resolve_index_profile()
            )
            return True
        try:
            replacement = await self.embedding_runtime.prepare_reload()
        except ServiceNotReadyError as exc:
            logger.warning("Index profile check deferred: {}", exc)
            return False
        try:
            await self.embedding_runtime.validate_prepared(replacement)
        except Exception:
            await self.embedding_runtime.discard_prepared(replacement)
            raise
        await self.embedding_runtime.activate_prepared(replacement)
        return True

    async def warm_up(self) -> dict[str, int]:
        """Finish bounded storage probes before accepting the first request."""
        return await warm_retrieval_stores(
            uow_factory=self._uow_factory,
            search_store=self.search_store,
            dimensions=self._settings.embedding.dimensions,
            path_store=self.path_index,
            lexical_store=self.lexical_store,
        )

    async def close(self) -> None:
        if self.worker is not None:
            await self.worker.stop()
        if self.resource_sampler is not None:
            await self.resource_sampler.stop()
        if self.monitoring_cleaner is not None:
            await self.monitoring_cleaner.stop()
        await self.metrics.stop()
        await self.search_store.close()
        if self.path_index is not None:
            await self.path_index.close()
        await self._models.close()
        if self.queue is not None:
            await self.queue.close()


@lru_cache
def get_container() -> Container:
    return Container()

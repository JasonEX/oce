"""进程级 composition root。"""

from __future__ import annotations

import os
from collections.abc import Sequence
from functools import lru_cache, partial

from loguru import logger

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
from oce.application.warmup import warm_retrieval_stores
from oce.application.worker import EmbedWorker
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
from oce.shared.index_stats import RetrievalRuntimeProfile
from oce.shared.logging import DATA_DIR_ENV
from oce.shared.metrics import MetricsSink, NoopMetricsSink, TokenUsageRecord


async def record_token_usage(
    metrics: MetricsSink,
    credential_id: int,
    kind: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> None:
    """把 embedder/reranker/llm 的真实用量桥接到 sink（UsageCallback 形状）。

    credential_id=0（无凭证，如 env 回落）归一为 None；旁路容错：任何异常只记日志，
    绝不抛回主链路。
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
        rerank_replacement = None
        if self._reranker is not None:
            try:
                rerank_replacement = await self._reranker.prepare_reload()
            except Exception:
                await self._embedder.discard_prepared(embedding_replacement)
                raise
        try:
            await self._embedder.validate_prepared(embedding_replacement)
        except Exception:
            await self._embedder.discard_prepared(embedding_replacement)
            if self._reranker is not None:
                await self._reranker.discard_prepared(rerank_replacement)
            raise
        try:
            await self._embedder.activate_prepared(embedding_replacement)
        except Exception:
            if self._reranker is not None:
                await self._reranker.discard_prepared(rerank_replacement)
            raise
        if self._query_cache is not None:
            await self._query_cache.clear_query_cache()
        if self._reranker is not None:
            await self._reranker.activate_prepared(rerank_replacement)
        # LLM 客户端无预备/激活两阶段（reload 仅原子替换 delegate）；旁路容错，
        # 单个刷新失败不回滚已激活的 embedder/reranker，只记日志。
        for client in self._llm_clients:
            try:
                await client.reload()
            except Exception as exc:
                logger.warning("LLM client reload failed: {}", exc)


class Container:
    def __init__(self) -> None:
        settings = get_settings()
        monitoring = settings.monitoring
        dense_dim = settings.embedding.dimensions

        self.metrics: MetricsSink = (
            SqlMetricsSink(
                async_session_factory,
                flush_interval_seconds=monitoring.flush_interval_seconds,
                max_buffer=monitoring.flush_max_buffer,
            )
            if monitoring.enabled
            else NoopMetricsSink()
        )
        self.resource_sampler = (
            ResourceSampler(
                self.metrics,
                interval_seconds=monitoring.resource_sample_interval_seconds,
                collector=build_psutil_collector(os.environ.get(DATA_DIR_ENV)),
            )
            if monitoring.enabled
            else None
        )
        self.monitoring_cleaner = (
            MonitoringCleaner(
                async_session_factory,
                retention_days=monitoring.retention_days,
                interval_seconds=monitoring.cleanup_interval_seconds,
            )
            if monitoring.enabled
            else None
        )
        # 监控关闭时传 None，采集侧判空直接跳过（零开销）。
        token_usage_cb = (
            partial(record_token_usage, self.metrics) if monitoring.enabled else None
        )

        self.search_store = Milvus3SearchStore(settings.milvus, dense_dim=dense_dim)
        self.path_index: PathIndexClient | None = None
        self.path_content_store: SqlPathContentStore | None = None
        if settings.retrieval.path_index_enabled:
            self.path_index = PathIndexClient(settings.milvus, dense_dim=dense_dim)
            self.path_content_store = SqlPathContentStore(async_session_factory)

        artifact_probes = [self.search_store]
        if self.path_index is not None:
            artifact_probes.append(self.path_index)
        self.index_lifecycle = IndexLifecycleManager(
            SqlIndexProfileStore(async_session_factory),
            settings,
            artifact_probes,
        )

        self.embedding_runtime = CredentialConfiguredEmbedder(
            async_session_factory,
            settings.embedding,
            expected_dimensions=dense_dim,
            on_usage=token_usage_cb,
            on_index_profile=self.index_lifecycle.ensure_compatible,
        )
        self.embedder = QueryCachingEmbedder(
            self.embedding_runtime,
            max_entries=settings.embedding.query_cache_max_entries,
            ttl_seconds=settings.embedding.query_cache_ttl_seconds,
        )
        # RERANK_ENABLED 授权专用重排阶段；是否外发由 provider 决定。
        # 未启用时不构造实现，pipeline 与审计据此区分未启用和按策略跳过。
        self.reranker: Reranker | None = None
        self.credential_reranker: CredentialConfiguredReranker | None = None
        if settings.rerank.enabled and settings.rerank.provider == "local":
            self.reranker = LocalOnnxReranker(
                model_dir=settings.rerank.local_model_dir,
                model_file=settings.rerank.local_model_file,
                candidates=settings.rerank.local_candidates,
                max_doc_chars=settings.rerank.local_max_doc_chars,
                max_query_chars=settings.rerank.max_query_chars,
                max_tokens=settings.rerank.local_max_tokens,
                batch_size=settings.rerank.local_batch_size,
                threads=settings.rerank.local_threads,
            )
        elif settings.rerank.enabled:
            self.credential_reranker = self.reranker = CredentialConfiguredReranker(
                async_session_factory,
                settings.rerank,
                fallback_embedding_key=(
                    settings.embedding.api_key.get_secret_value()
                    if settings.embedding.api_key is not None
                    else None
                ),
                on_usage=token_usage_cb,
            )

        # LLM 重排与查询改写各自按 kind 解析凭证（env 兜底），可分别配置。
        self.llm_clients: list[CredentialConfiguredLLMClient] = []
        self.llm_reranker: LLMReranker | None = None
        if settings.llm.rerank_enabled:
            rerank_llm = CredentialConfiguredLLMClient(
                "llm_rerank",
                async_session_factory,
                settings.llm,
                fallback_model=settings.llm.model,
                on_usage=token_usage_cb,
            )
            self.llm_clients.append(rerank_llm)
            self.llm_reranker = LLMReranker(
                client=rerank_llm,
                model=settings.llm.model,
                max_candidates=settings.llm.max_candidates,
                output_top_k=settings.llm.output_top_k,
                snippet_chars=settings.llm.snippet_chars,
                timeout_seconds=settings.llm.rerank_timeout_seconds,
            )
        self.query_rewriter: QueryRewriter | None = None
        if settings.retrieval.query_rewrite_enabled:
            rewrite_llm = CredentialConfiguredLLMClient(
                "query_rewrite",
                async_session_factory,
                settings.llm,
                fallback_model=settings.retrieval.query_rewrite_model,
                on_usage=token_usage_cb,
            )
            self.llm_clients.append(rewrite_llm)
            self.query_rewriter = QueryRewriter(
                client=rewrite_llm,
                model=settings.retrieval.query_rewrite_model,
                num_rewrites=settings.retrieval.query_rewrite_num,
            )
        credential_runtime = _CredentialRuntime(
            self.embedding_runtime,
            self.credential_reranker,
            self.llm_clients,
            query_cache=self.embedder,
        )

        # tree-sitter reads definitions and imports with real spans; regex stays
        # the fallback for grammars the pack cannot load and detects endpoints.
        self.symbol_provider = TreeSitterSymbolProvider(RegexSymbolProvider())
        self._uow_factory = lambda: SqlAlchemyUnitOfWork(
            async_session_factory,
            self.symbol_provider,
        )
        self.chunker = build_chunker(
            semantic_enabled=settings.chunking.semantic_enabled,
            semantic_max_chunk_chars=settings.chunking.semantic_max_chunk_chars,
            recursive_chunk_size=settings.chunking.recursive_chunk_size,
            recursive_chunk_overlap=settings.chunking.recursive_chunk_overlap,
        )
        pipeline_factory = build_pipeline_factory(
            chunker=self.chunker,
            embedder=self.embedder,
            vector_index=self.search_store,
            path_store=self.path_index,
            embedding_enabled=settings.embedding.enabled,
            lexical_enabled=settings.retrieval.lexical_enabled,
        )

        self.queue: RedisQueue | None = None
        self.worker: EmbedWorker | None = None
        if settings.worker.enabled:
            self.queue = _build_redis_queue(settings)
            self.worker = EmbedWorker(
                queue=self.queue,
                uow_factory=self._uow_factory,
                pipeline_factory=pipeline_factory,
                concurrency=_worker_concurrency(settings),
                blob_batch_size=settings.worker.blob_batch_size,
                max_retries=settings.worker.max_retries,
            )

        delete_blobs_handler = DeleteBlobsCommandHandler(
            self._uow_factory,
            self.search_store,
            path_store=self.path_index,
        )
        credential_admin_store = SqlCredentialAdminStore(async_session_factory)

        command_bus = CommandBus()
        command_bus.register(
            IngestBlobsCommand,
            IngestBlobsCommandHandler(self._uow_factory, pipeline_factory, self.queue),
        )
        command_bus.register(
            EmbedPendingCommand,
            EmbedPendingCommandHandler(
                self._uow_factory,
                pipeline_factory,
                blob_batch_size=settings.worker.blob_batch_size,
            ),
        )
        command_bus.register(DeleteBlobsCommand, delete_blobs_handler)
        command_bus.register(
            ReloadEmbeddingCredentialsCommand,
            ReloadEmbeddingCredentialsCommandHandler(credential_runtime),
        )
        command_bus.register(
            CheckpointCommand, CheckpointCommandHandler(self._uow_factory)
        )
        command_bus.register(
            RequeueStaleCommand,
            RequeueStaleCommandHandler(self._uow_factory, self.queue),
        )
        command_bus.register(
            ResetQueueCommand,
            ResetQueueCommandHandler(
                self._uow_factory,
                self.queue,
                worker_running=lambda: (
                    self.worker is not None and self.worker.is_running
                ),
            ),
        )
        command_bus.register(
            CreateCredentialCommand,
            CreateCredentialCommandHandler(credential_admin_store),
        )
        command_bus.register(
            UpdateCredentialCommand,
            UpdateCredentialCommandHandler(credential_admin_store),
        )
        command_bus.register(
            DeleteCredentialCommand,
            DeleteCredentialCommandHandler(credential_admin_store),
        )
        command_bus.register(
            DuplicateCredentialCommand,
            DuplicateCredentialCommandHandler(credential_admin_store),
        )
        command_bus.register(
            GcCommand,
            GcCommandHandler(self._uow_factory, delete_blobs_handler, self.queue),
        )

        query_bus = QueryBus()
        self.lexical_store: SqlLexicalSearchStore | None = (
            SqlLexicalSearchStore(async_session_factory)
            if settings.retrieval.lexical_enabled
            else None
        )
        # One store serves both the exact lane and the relation lanes; they
        # read the same occurrence table with the same timeout.
        symbol_store = SymbolSearchStore(
            async_session_factory,
            timeout_seconds=settings.retrieval.exact_timeout_seconds,
        )
        query_bus.register(
            SearchQuery,
            SearchQueryHandler(
                RetrievalPipeline(
                    embedder=self.embedder,
                    store=self.search_store,
                    reranker=self.reranker,
                    llm_reranker=self.llm_reranker,
                    rerank_window=(
                        settings.llm.max_candidates
                        if self.llm_reranker is not None
                        else None
                    ),
                    query_rewriter=self.query_rewriter,
                    path_store=self.path_index,
                    path_content_store=self.path_content_store,
                    exact_store=symbol_store,
                    relation_store=symbol_store,
                    lexical_store=self.lexical_store,
                    path_lookup_store=(
                        SqlPathLookupStore(async_session_factory)
                        if settings.retrieval.path_lookup_enabled
                        else None
                    ),
                    settings=settings.retrieval,
                ),
                metrics=self.metrics,
                retrieval_audit_enabled=(
                    monitoring.enabled and monitoring.retrieval_audit_enabled
                ),
                store_query_text=monitoring.store_query_text,
            ),
        )
        query_bus.register(FindMissingQuery, FindMissingQueryHandler(self._uow_factory))
        query_bus.register(BlobStatusQuery, BlobStatusQueryHandler(self._uow_factory))
        query_bus.register(
            ResolveScopeQuery, ResolveScopeQueryHandler(self._uow_factory)
        )
        query_bus.register(
            MonitoringStatsQuery,
            MonitoringStatsQueryHandler(
                SqlMonitoringStatsReader(async_session_factory)
            ),
        )
        query_bus.register(
            IndexStatsQuery,
            IndexStatsQueryHandler(
                SqlMetadataIndexStatsReader(async_session_factory),
                self.search_store,
                self.path_index,
                self.embedder,
                _runtime_profile(settings),
                self.index_lifecycle,
            ),
        )
        query_bus.register(
            ListCredentialsQuery,
            ListCredentialsQueryHandler(credential_admin_store),
        )
        query_bus.register(
            QueueStatusQuery,
            QueueStatusQueryHandler(self._uow_factory, self.queue),
        )

        self.application = RetrievalApplication(
            command_bus,
            query_bus,
            background_indexing=self.queue is not None,
        )

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
        settings = get_settings()
        return await warm_retrieval_stores(
            uow_factory=self._uow_factory,
            search_store=self.search_store,
            dimensions=settings.embedding.dimensions,
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
        await self.embedder.close()
        if self.reranker is not None:
            await self.reranker.close()
        for client in self.llm_clients:
            await client.close()
        if self.queue is not None:
            await self.queue.close()


def _build_redis_queue(settings: Settings) -> RedisQueue:
    import redis.asyncio as redis

    client = redis.from_url(
        settings.redis.url,
        decode_responses=True,
        encoding="utf-8",
        max_connections=20,  # 8 workers + 余量
        socket_timeout=10.0,
        socket_connect_timeout=5.0,
        socket_keepalive=True,
        health_check_interval=30,
        retry_on_timeout=True,
    )
    return RedisQueue(client, settings.redis.queue_name)


def _worker_concurrency(settings: Settings) -> int:
    """Worker 并发不能超过 DB 连接池（留一条给请求）和 embedding 并发上限。"""
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
        rerank_enabled=settings.rerank.enabled,
        rerank_provider=settings.rerank.provider if settings.rerank.enabled else "none",
        rerank_candidate_limit=(
            settings.rerank.local_candidates
            if settings.rerank.enabled and settings.rerank.provider == "local"
            else settings.rerank.top_n
            if settings.rerank.enabled
            else 0
        ),
        rerank_query_char_limit=(
            settings.rerank.max_query_chars if settings.rerank.enabled else 0
        ),
        rerank_document_char_limit=(
            settings.rerank.local_max_doc_chars
            if settings.rerank.enabled and settings.rerank.provider == "local"
            else 0
        ),
        rerank_token_limit=(
            settings.rerank.local_max_tokens
            if settings.rerank.enabled and settings.rerank.provider == "local"
            else 0
        ),
        rerank_policy=retrieval.rerank_policy,
        llm_rerank_enabled=settings.llm.rerank_enabled,
        llm_rerank_policy=retrieval.llm_rerank_policy,
        query_rewrite_enabled=retrieval.query_rewrite_enabled,
        decisive_skips_dense=retrieval.decisive_skips_dense,
    )


@lru_cache
def get_container() -> Container:
    return Container()

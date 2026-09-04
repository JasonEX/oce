"""ACE 兼容 HTTP DTO。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


def _none_to_empty_string(value: Any) -> Any:
    return "" if value is None else value


def _none_to_empty_list(value: Any) -> Any:
    return [] if value is None else value


class FindMissingRequest(BaseModel):
    mem_object_names: list[str] = Field(default_factory=list)


class FindMissingResponse(BaseModel):
    unknown_memory_names: list[str] = Field(default_factory=list)
    nonindexed_blob_names: list[str] = Field(default_factory=list)


class BlobInput(BaseModel):
    content: str
    path: str = Field(min_length=1)


class BatchUploadRequest(BaseModel):
    blobs: list[BlobInput] = Field(default_factory=list)
    checkpoint_id: str = ""

    _normalize_checkpoint = field_validator("checkpoint_id", mode="before")(
        _none_to_empty_string
    )


class BatchUploadResponse(BaseModel):
    blob_names: list[str] = Field(default_factory=list)


class ReloadCredentialsResponse(BaseModel):
    reloaded: bool
    reason: str | None = None


class BlobsPayload(BaseModel):
    checkpoint_id: str = ""
    added_blobs: list[str] = Field(default_factory=list)
    deleted_blobs: list[str] = Field(default_factory=list)

    _normalize_checkpoint = field_validator("checkpoint_id", mode="before")(
        _none_to_empty_string
    )
    _normalize_lists = field_validator("added_blobs", "deleted_blobs", mode="before")(
        _none_to_empty_list
    )


class CodebaseRetrievalRequest(BaseModel):
    information_request: str = Field(min_length=1)
    blobs: BlobsPayload = Field(default_factory=BlobsPayload)
    chat_history: list[Any] = Field(default_factory=list)


class CodebaseRetrievalResponse(BaseModel):
    formatted_retrieval: str
    codebase_retrieval_elapsed_ms: int


class CheckpointBlobsRequest(BaseModel):
    blobs: BlobsPayload = Field(default_factory=BlobsPayload)


class CheckpointBlobsResponse(BaseModel):
    new_checkpoint_id: str


class BlobStatusRequest(BaseModel):
    blobs: BlobsPayload = Field(default_factory=BlobsPayload)


class BlobStatusResponse(BaseModel):
    unknown_blob_names: list[str] = Field(default_factory=list)
    nonindexed_blob_names: list[str] = Field(default_factory=list)
    checkpoint_not_found: bool = False


class ApiCallStatsResponse(BaseModel):
    count: int = 0
    error_count: int = 0
    avg_latency_ms: float = 0.0
    p50_latency_ms: int = 0
    p95_latency_ms: int = 0
    max_latency_ms: int = 0


class TokenKindStatsResponse(BaseModel):
    kind: str
    calls: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class RetrievalStatsResponse(BaseModel):
    count: int = 0
    empty_count: int = 0
    empty_rate: float = 0.0


class ResourceSnapshotResponse(BaseModel):
    ts: datetime | None = None
    mem_rss_bytes: int = 0
    mem_percent: float = 0.0
    cpu_percent: float = 0.0
    disk_free_bytes: int = 0
    disk_total_bytes: int = 0
    disk_data_bytes: int = 0


class MonitoringStatsResponse(BaseModel):
    window_hours: int
    api_calls: ApiCallStatsResponse = Field(default_factory=ApiCallStatsResponse)
    tokens: list[TokenKindStatsResponse] = Field(default_factory=list)
    tokens_total: int = 0
    retrieval: RetrievalStatsResponse = Field(default_factory=RetrievalStatsResponse)
    resource: ResourceSnapshotResponse | None = None


class MetadataIndexStatsResponse(BaseModel):
    blobs_total: int = 0
    blobs_ready: int = 0
    blobs_pending: int = 0
    blobs_error: int = 0
    chunks_total: int = 0
    chunks_embedded: int = 0
    blob_chunk_links: int = 0
    symbol_occurrences: int = 0
    chains: int = 0
    chain_members: int = 0
    staging_blobs: int = 0
    lexical_documents: int = 0


class IndexStoreStatsResponse(BaseModel):
    enabled: bool
    available: bool
    collection_name: str | None = None
    exists: bool | None = None
    entities: int | None = None
    error_type: str | None = None


class QueryCacheStatsResponse(BaseModel):
    enabled: bool
    entries: int
    max_entries: int
    ttl_seconds: float
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    invalidations: int = 0


class RetrievalRuntimeProfileResponse(BaseModel):
    embedding_enabled: bool
    embedding_query_char_limit: int = 0
    semantic_chunking_enabled: bool
    exact_enabled: bool
    path_index_enabled: bool
    source_priority_enabled: bool
    coverage_selection_enabled: bool
    query_decomposition_enabled: bool
    lexical_enabled: bool
    path_lookup_enabled: bool
    merge_adjacent_enabled: bool
    related_definitions_enabled: bool
    rerank_enabled: bool
    rerank_provider: str = "none"
    rerank_candidate_limit: int = 0
    rerank_query_char_limit: int = 0
    rerank_document_char_limit: int = 0
    rerank_token_limit: int = 0
    rerank_policy: str
    llm_rerank_enabled: bool
    llm_rerank_policy: str
    query_rewrite_enabled: bool


class IndexProfileStatsResponse(BaseModel):
    state: str
    fingerprint: str | None = None
    schema_version: int | None = None
    embedding_enabled: bool | None = None
    embedding_fingerprint: str | None = None
    embedding_model: str | None = None
    embedding_dimensions: int | None = None


class IndexStatsResponse(BaseModel):
    metadata: MetadataIndexStatsResponse
    dense: IndexStoreStatsResponse
    path: IndexStoreStatsResponse
    query_cache: QueryCacheStatsResponse
    runtime: RetrievalRuntimeProfileResponse
    profile: IndexProfileStatsResponse


# 凭据用途：embed/rerank 走 REST；llm_rerank/query_rewrite 走 chat。
CredentialKind = Literal["embed", "rerank", "llm_rerank", "query_rewrite"]


class CredentialResponse(BaseModel):
    """凭据视图：脱敏，只暴露 api_key 尾 4 位。kind 专属参数对其它 kind 为 None。"""

    id: int
    # 历史数据库中可能仍有已停用的 kind；列表响应保持可读，写入 DTO 只接受当前 kind。
    kind: str
    provider: str | None = None
    name: str
    status: str
    priority: int
    endpoint: str | None = None
    model: str | None = None
    timeout_seconds: int
    note: str | None = None
    dimensions: int | None = None
    max_batch_size: int | None = None
    max_batch_chars: int | None = None
    max_input_chars: int | None = None
    input_overlap_chars: int | None = None
    top_n: int | None = None
    min_score: float | None = None
    tpm_limit: int | None = None
    api_key_last4: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CredentialListResponse(BaseModel):
    credentials: list[CredentialResponse] = Field(default_factory=list)


class CredentialPatchRequest(BaseModel):
    """字段覆盖：省略即不改（更新）或继承源行（复制）；api_key 提供则同步刷新 hash。

    复制时省略 api_key 即复用源 key，配合覆盖 kind/model 可把某把 key 的通道复制成
    别的用途（如复制 embed 行改成 rerank），不再撞唯一约束。
    """

    kind: CredentialKind | None = None
    name: str | None = Field(default=None, min_length=1)
    api_key: str | None = Field(default=None, min_length=1)
    provider: str | None = None
    status: Literal["active", "disabled"] | None = None
    priority: int | None = None
    endpoint: str | None = None
    model: str | None = None
    timeout_seconds: int | None = None
    note: str | None = None
    dimensions: int | None = None
    max_batch_size: int | None = None
    max_batch_chars: int | None = None
    max_input_chars: int | None = None
    input_overlap_chars: int | None = None
    top_n: int | None = None
    min_score: float | None = None
    tpm_limit: int | None = None


class CredentialCreateRequest(CredentialPatchRequest):
    kind: CredentialKind
    name: str = Field(min_length=1)
    api_key: str = Field(min_length=1)
    status: Literal["active", "disabled"] = "active"
    priority: int = 100
    timeout_seconds: int = 30


class QueueStatusResponse(BaseModel):
    enabled: bool
    main_size: int = 0
    inflight: int = 0
    db_pending: int = 0


class QueueResetRequest(BaseModel):
    mode: Literal["sync", "purge"] = "sync"
    requeue: bool = True


class QueueResetResponse(BaseModel):
    removed: int = 0
    requeued: int = 0
    queue_size: int = 0
    db_pending: int = 0


class RequeueStaleRequest(BaseModel):
    stale_hours: int = 24
    limit: int = 100


class RequeueStaleResponse(BaseModel):
    requeued_count: int = 0


class GcRequest(BaseModel):
    ttl_days: int = 30
    dry_run: bool = True
    limit: int = 1000


class GcResponse(BaseModel):
    dry_run: bool
    ttl_days: int
    expired_chains: int = 0
    expired_blobs: int = 0
    deletable_blobs: int = 0
    skipped_inflight: int = 0
    deleted_chains: int = 0
    deleted_blobs: int = 0

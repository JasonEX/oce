"""PostgreSQL/SQLite metadata models for blobs, chunks, and checkpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from oce.shared.database.session import Base

# SQLite autoincrement only works on INTEGER primary keys.
_AutoId = BigInteger().with_variant(Integer, "sqlite")


def _timestamp(**kwargs: Any) -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), **kwargs
    )


class ModelCredentialModel(Base):
    """One model credential: a (kind, account) channel.

    ``kind`` is embed, rerank, llm_rerank or query_rewrite. One key may serve
    several kinds and models: the unique constraint is (kind, model,
    api_key_hash), so only an exact duplicate is rejected. ``endpoint`` is the
    full URL for embed/rerank (/v1/embeddings, /v1/rerank) and the base URL
    (/v1) for the chat kinds. Resolution picks the active row with the lowest
    priority per kind and falls back to the environment; kind-specific columns
    are NULL for other kinds and fall back field by field.
    """

    __tablename__ = "model_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16))
    # Provider label (siliconflow), for grouping and duplication only.
    provider: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    api_key: Mapped[str] = mapped_column(String(512))
    api_key_hash: Mapped[str] = mapped_column(String(64))
    endpoint: Mapped[str | None] = mapped_column(String(512))
    model: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="active")
    priority: Mapped[int] = mapped_column(Integer, default=100)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=30)
    note: Mapped[str | None] = mapped_column(Text)

    # embed only
    dimensions: Mapped[int | None] = mapped_column(Integer)
    max_batch_size: Mapped[int | None] = mapped_column(Integer)
    max_batch_chars: Mapped[int | None] = mapped_column(Integer)
    max_input_chars: Mapped[int | None] = mapped_column(Integer)
    input_overlap_chars: Mapped[int | None] = mapped_column(Integer)

    # rerank (API) only
    top_n: Mapped[int | None] = mapped_column(Integer)
    min_score: Mapped[float | None]

    # chat kinds only: llm_rerank, query_rewrite
    tpm_limit: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = _timestamp()
    updated_at: Mapped[datetime] = _timestamp()

    __table_args__ = (
        UniqueConstraint(
            "kind",
            "model",
            "api_key_hash",
            name="uq_model_credentials_kind_model_key",
        ),
        Index(
            "idx_model_credentials_kind_status_priority",
            "kind",
            "status",
            "priority",
        ),
    )


class IndexProfileModel(Base):
    """Singleton identity of the metadata and vector artifacts in this deployment."""

    __tablename__ = "index_profiles"

    profile_key: Mapped[str] = mapped_column(String(16), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    profile_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _timestamp()


class BlobModel(Base):
    __tablename__ = "blobs"

    blob_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    path: Mapped[str] = mapped_column(String(1024))
    content_size: Mapped[int] = mapped_column(Integer)
    language: Mapped[str | None] = mapped_column(String(32))
    file_type: Mapped[str] = mapped_column(String(16), default="text")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    retry_count: Mapped[int] = mapped_column(Integer, server_default="0")
    last_seen: Mapped[datetime] = _timestamp()
    created_at: Mapped[datetime] = _timestamp()
    error_message: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_blobs_status", "status"),
        Index("ix_blobs_last_seen", "last_seen"),
        Index("ix_blobs_language", "language"),
        Index("ix_blobs_retry_count", "retry_count"),
    )


class BlobStagingModel(Base):
    __tablename__ = "blob_staging"

    blob_name: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("blobs.blob_name", ondelete="CASCADE"),
        primary_key=True,
    )
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _timestamp()

    __table_args__ = (Index("ix_blob_staging_created_at", "created_at"),)


class ChunkModel(Base):
    __tablename__ = "chunks"

    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    content: Mapped[str] = mapped_column(Text)
    content_size: Mapped[int] = mapped_column(Integer)
    chunk_type: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = _timestamp()
    embedded: Mapped[bool] = mapped_column(Boolean, server_default="false")

    __table_args__ = (
        Index("ix_chunks_chunk_type", "chunk_type"),
        Index("ix_chunks_embedded", "embedded"),
    )


class BlobChunkModel(Base):
    __tablename__ = "blob_chunks"

    id: Mapped[int] = mapped_column(_AutoId, primary_key=True, autoincrement=True)
    blob_name: Mapped[str] = mapped_column(
        String(64), ForeignKey("blobs.blob_name", ondelete="CASCADE")
    )
    content_hash: Mapped[str] = mapped_column(
        String(64), ForeignKey("chunks.content_hash", ondelete="CASCADE")
    )
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    chunk_index: Mapped[int] = mapped_column(Integer)
    # Enclosing scope chain of this occurrence, not of the content-addressed chunk.
    context: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "blob_name",
            "content_hash",
            "start_line",
            "end_line",
            name="uq_blob_chunks_span",
        ),
        Index("ix_blob_chunks_blob_name", "blob_name"),
        Index("ix_blob_chunks_content_hash", "content_hash"),
        Index("ix_blob_chunks_blob_index", "blob_name", "chunk_index"),
    )


class ChainModel(Base):
    __tablename__ = "chains"

    chain_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    description: Mapped[str | None] = mapped_column(String(512))
    total_blobs: Mapped[int] = mapped_column(Integer, default=0)
    total_chunks: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _timestamp()
    updated_at: Mapped[datetime] = _timestamp(onupdate=func.now())

    __table_args__ = (Index("ix_chains_updated_at", "updated_at"),)


class ChainMemberModel(Base):
    __tablename__ = "chain_members"

    chain_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("chains.chain_id", ondelete="CASCADE"),
        primary_key=True,
    )
    blob_name: Mapped[str] = mapped_column(String(64), primary_key=True)

    __table_args__ = (Index("ix_chain_members_blob_name", "blob_name"),)


class SymbolOccurrenceModel(Base):
    __tablename__ = "symbol_occurrences"

    id: Mapped[int] = mapped_column(_AutoId, primary_key=True, autoincrement=True)
    identifier: Mapped[str] = mapped_column(String(256))
    blob_name: Mapped[str] = mapped_column(
        String(64), ForeignKey("blobs.blob_name", ondelete="CASCADE")
    )
    content_hash: Mapped[str] = mapped_column(
        String(64), ForeignKey("chunks.content_hash", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(16))
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    # Innermost enclosing definition; empty (not NULL, so the unique key
    # deduplicates) at module level or when unknown.
    enclosing: Mapped[str] = mapped_column(String(256), server_default="", default="")
    created_at: Mapped[datetime] = _timestamp()

    __table_args__ = (
        Index("idx_so_identifier", "identifier"),
        Index("idx_so_blob_name", "blob_name"),
        Index("idx_so_identifier_kind", "identifier", "kind"),
        Index("idx_so_content_hash", "content_hash"),
        UniqueConstraint(
            "identifier",
            "blob_name",
            "content_hash",
            "kind",
            "enclosing",
            name="uq_symbol_occurrences_key",
        ),
    )


class ApiCallMetricModel(Base):
    """One row per HTTP request: latency and status."""

    __tablename__ = "api_call_metrics"

    id: Mapped[int] = mapped_column(_AutoId, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = _timestamp()
    endpoint: Mapped[str] = mapped_column(String(128))
    method: Mapped[str] = mapped_column(String(8))
    status_code: Mapped[int] = mapped_column(Integer)
    latency_ms: Mapped[int] = mapped_column(Integer)
    error_type: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        Index("ix_api_call_metrics_ts", "ts"),
        Index("ix_api_call_metrics_endpoint", "endpoint"),
    )


class TokenUsageMetricModel(Base):
    """One row per model call: token usage of embed, rerank and rewrite."""

    __tablename__ = "token_usage_metrics"

    id: Mapped[int] = mapped_column(_AutoId, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = _timestamp()
    kind: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(128))
    credential_id: Mapped[int | None] = mapped_column(Integer)
    prompt_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    total_tokens: Mapped[int] = mapped_column(Integer, server_default="0")

    __table_args__ = (
        Index("ix_token_usage_metrics_ts", "ts"),
        Index("ix_token_usage_metrics_kind", "kind"),
        Index("ix_token_usage_metrics_credential_id", "credential_id"),
    )


class ResourceSampleModel(Base):
    """One row per resource sample: disk, memory and CPU."""

    __tablename__ = "resource_samples"

    id: Mapped[int] = mapped_column(_AutoId, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = _timestamp()
    disk_data_bytes: Mapped[int] = mapped_column(BigInteger)
    disk_free_bytes: Mapped[int] = mapped_column(BigInteger)
    disk_total_bytes: Mapped[int] = mapped_column(BigInteger)
    mem_rss_bytes: Mapped[int] = mapped_column(BigInteger)
    mem_percent: Mapped[float]
    cpu_percent: Mapped[float]

    __table_args__ = (Index("ix_resource_samples_ts", "ts"),)


class RetrievalMetricModel(Base):
    """One row per retrieval: stage timings and routing evidence; ``hit_count`` 0 is an empty answer."""

    __tablename__ = "retrieval_metrics"

    id: Mapped[int] = mapped_column(_AutoId, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = _timestamp()
    source: Mapped[str] = mapped_column(String(32))
    scope_size: Mapped[int | None] = mapped_column(Integer)
    hit_count: Mapped[int] = mapped_column(Integer)
    total_ms: Mapped[int] = mapped_column(Integer)
    intent: Mapped[str | None] = mapped_column(String(32))
    path_boosted: Mapped[bool] = mapped_column(Boolean, server_default="false")
    rerank_route: Mapped[str | None] = mapped_column(String(48))
    dense_route: Mapped[str | None] = mapped_column(String(48))
    head_slots: Mapped[int] = mapped_column(Integer, server_default="0")
    # Structural evidence the router saw and the size of the relation
    # sections, for calibrating the adaptive thresholds offline.
    exact_definitions: Mapped[int] = mapped_column(Integer, server_default="0")
    definition_sites: Mapped[int] = mapped_column(Integer, server_default="0")
    relation_hits: Mapped[int] = mapped_column(Integer, server_default="0")
    relation_chars: Mapped[int] = mapped_column(Integer, server_default="0")
    # ``lane:ExceptionType`` pairs, comma separated; NULL when every lane ran.
    lane_failures: Mapped[str | None] = mapped_column(Text)
    query_text: Mapped[str | None] = mapped_column(Text)
    # Stage timings in ms; a stage that never ran stays NULL rather than 0.
    rewrite_ms: Mapped[int | None] = mapped_column(Integer)
    embed_ms: Mapped[int | None] = mapped_column(Integer)
    dense_ms: Mapped[int | None] = mapped_column(Integer)
    exact_ms: Mapped[int | None] = mapped_column(Integer)
    path_ms: Mapped[int | None] = mapped_column(Integer)
    path_lookup_ms: Mapped[int | None] = mapped_column(Integer)
    lexical_ms: Mapped[int | None] = mapped_column(Integer)
    fuse_ms: Mapped[int | None] = mapped_column(Integer)
    rerank_ms: Mapped[int | None] = mapped_column(Integer)
    llm_rerank_ms: Mapped[int | None] = mapped_column(Integer)
    select_ms: Mapped[int | None] = mapped_column(Integer)
    expand_ms: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        Index("ix_retrieval_metrics_ts", "ts"),
        Index("ix_retrieval_metrics_source", "source"),
        Index("ix_retrieval_metrics_hit_count", "hit_count"),
    )

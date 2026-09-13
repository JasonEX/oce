"""Application settings; each group reads its own environment-variable prefix."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Every group reads the same files, .env.local overriding .env, so no group
# misses a local override.
_ENV_FILES = (".env", ".env.local")

# Per-query routing policy of a reranker; authorization is the *_ENABLED switch.
RerankPolicy = Literal["adaptive", "always"]


def _settings_config(env_prefix: str = "") -> SettingsConfigDict:
    return SettingsConfigDict(
        env_prefix=env_prefix,
        env_file=_ENV_FILES,
        env_file_encoding="utf-8",
        extra="ignore",
    )


class DatabaseSettings(BaseSettings):
    """Metadata database (PostgreSQL, or SQLite in personal mode)."""

    model_config = _settings_config("DB_")

    url: str = Field(
        default="postgresql+asyncpg://oce:oce@localhost:5432/oce",
        description="Database connection URL",
    )
    pool_size: int = Field(default=5, ge=1, le=100, description="Connection pool size")
    max_overflow: int = Field(
        default=5, ge=0, le=100, description="Connection pool overflow limit"
    )
    echo: bool = Field(default=False, description="Log every SQL statement")

    @property
    def is_sqlite(self) -> bool:
        return self.url.startswith("sqlite")


class MilvusSettings(BaseSettings):
    """Milvus 3.0 dense vector store; the dimension comes from EmbeddingSettings."""

    model_config = _settings_config("MILVUS_")

    endpoint: str = Field(
        default="http://localhost:19530",
        description="Milvus endpoint: an HTTP address, or a local Milvus Lite file path",
    )
    token: str | None = Field(
        default=None, description="Authentication token (Zilliz Cloud)"
    )

    collection_name: str = Field(default="oce_chunks", description="Collection name")
    path_collection_name: str = Field(
        default="oce_paths_v1",
        description="Collection name of the path index",
    )

    dense_index_type: str = Field(default="HNSW", description="Dense vector index type")
    dense_metric_type: str = Field(default="COSINE", description="Dense vector metric")

    hnsw_m: int = Field(default=16, ge=4, le=64, description="HNSW M parameter")
    hnsw_ef_construction: int = Field(
        default=256, ge=8, le=512, description="HNSW efConstruction"
    )
    hnsw_ef_search: int = Field(
        default=64, ge=8, le=2048, description="HNSW ef at search time"
    )


class EmbeddingSettings(BaseSettings):
    """Embedding model settings."""

    model_config = _settings_config("EMBED_")

    enabled: bool = Field(
        default=True, description="Embed chunks; when off, files are only chunked"
    )
    endpoint: str = Field(
        default="https://api.siliconflow.cn/v1/embeddings",
        description="OpenAI-compatible embedding endpoint",
    )
    api_key: SecretStr | None = Field(default=None, description="Embedding API key")
    model: str = Field(default="Qwen/Qwen3-Embedding-4B", description="Embedding model")
    dimensions: int = Field(
        default=1024,
        ge=1,
        description="Vector dimension, also the dimension of both Milvus collections",
    )
    max_batch_size: int = Field(
        default=32, ge=1, le=256, description="Texts per request"
    )
    max_batch_chars: int = Field(
        default=32_000,
        ge=1,
        description="Character budget of one request's input array",
    )
    max_input_chars: int = Field(
        default=8_000, ge=1, description="Character limit of one model input"
    )
    input_overlap_chars: int = Field(
        default=400, ge=0, description="Overlap between the segments of a long input"
    )
    max_concurrency: int = Field(
        default=4, ge=1, le=32, description="Maximum concurrent requests"
    )
    timeout_seconds: float = Field(
        default=60.0, gt=0, description="Request timeout in seconds"
    )
    proxy: str | None = Field(default=None, description="Optional HTTP proxy")
    query_instruction: str = Field(
        default="",
        description="Instruction prepended to every query; empty sends none",
    )
    # Embedding a whole issue text is slow and dilutes the vector; the title
    # and description come first. The ablation data lives in
    # benchmarks/results rather than in configuration code.
    max_query_chars: int = Field(
        default=3_000,
        ge=0,
        description="Character limit of the query embedding input; 0 for unlimited",
    )
    query_cache_max_entries: int = Field(
        default=256,
        ge=0,
        le=10_000,
        description="In-process query vector LRU capacity; 0 disables it",
    )
    query_cache_ttl_seconds: float = Field(
        default=600.0,
        ge=0,
        description="Query vector cache TTL in seconds; 0 disables it",
    )


class RerankSettings(BaseSettings):
    """Dedicated reranker settings."""

    model_config = _settings_config("RERANK_")

    enabled: bool = Field(
        default=False,
        description="Authorize the dedicated rerank stage",
    )
    # api: a remote cross-encoder (the query and candidate source leave the
    # process); local: an in-process ONNX cross-encoder that needs
    # `uv sync --extra local-rerank` and a local model directory.
    provider: Literal["api", "local"] = Field(
        default="api", description="How the dedicated reranker is provided"
    )
    endpoint: str = Field(
        default="https://api.siliconflow.cn/v1/rerank",
        description="Rerank endpoint",
    )
    api_key: SecretStr | None = Field(
        default=None, description="Falls back to the embedding key when empty"
    )
    model: str = Field(default="Qwen/Qwen3-Reranker-0.6B", description="Rerank model")
    top_n: int = Field(
        default=50,
        ge=1,
        le=100,
        description="Most candidates the dedicated reranker may move to the head",
    )
    min_score: float = Field(
        default=0.05, ge=0.0, le=1.0, description="Minimum rerank score"
    )
    timeout_seconds: float = Field(
        default=60.0, gt=0, description="Request timeout in seconds"
    )
    # A cross-encoder rereads the query for every candidate; on the 0.6B
    # benchmark a 25K-character issue took one call close to 15 s. Truncation
    # keeps the problem statement at the top.
    max_query_chars: int = Field(
        default=2_400,
        ge=200,
        description="Character limit of the query sent to the reranker",
    )
    # The local ONNX window is deliberately smaller than the remote default to
    # bound CPU latency.
    local_model_dir: str = Field(
        default="~/.cache/oce/models/jina-reranker-v2-base-multilingual",
        description="Local reranker model directory (model_int8.onnx and tokenizer.json)",
    )
    local_model_file: str = Field(
        default="model_int8.onnx",
        description="ONNX file name inside the model directory",
    )
    local_candidates: int = Field(
        default=16, ge=1, le=100, description="Candidates the local reranker scores"
    )
    local_max_doc_chars: int = Field(
        default=800,
        ge=100,
        description="Character limit per candidate sent to the local reranker",
    )
    local_max_tokens: int = Field(
        default=512, ge=64, le=8192, description="Token limit of query plus candidate"
    )
    local_batch_size: int = Field(
        default=4, ge=1, le=64, description="Inference batch size"
    )
    # On hybrid big/little CPUs onnxruntime is slower with every logical core.
    local_threads: int = Field(
        default=0,
        ge=0,
        le=128,
        description="Inference threads; 0 uses half the cores, at most 8",
    )
    # The Qwen3-Reranker model card reports 1%-5% gains from task
    # instructions and recommends English for multilingual use; clear it for
    # providers that do not support one.
    instruction: str = Field(
        default=(
            "Given a code search query, judge whether the code snippet implements, "
            "defines, or directly answers what the query asks for"
        ),
        description="Task instruction sent with every request; empty sends none",
    )


class ChunkingSettings(BaseSettings):
    """Source chunking composition."""

    model_config = _settings_config("CHUNKING_")

    semantic_enabled: bool = Field(
        default=True,
        description="Use cAST and the structured chunkers; off routes everything to the recursive chunker",
    )
    semantic_max_chunk_chars: int = Field(
        default=1500,
        gt=0,
        description="Non-whitespace character budget of a cAST semantic chunk",
    )
    recursive_chunk_size: int = Field(
        default=6000,
        gt=0,
        description="Target size of a recursive fallback chunk in characters",
    )
    recursive_chunk_overlap: int = Field(
        default=200,
        ge=0,
        description="Boundary search overlap of the recursive splitter in characters",
    )


class LLMSettings(BaseSettings):
    """Environment fallback shared by the chat-LLM features.

    LLM reranking and query rewriting build one client per kind; without a
    matching ``model_credentials`` row both fall back to these LLM_* values.
    """

    model_config = _settings_config("LLM_")

    rerank_enabled: bool = Field(
        default=False,
        description="Authorize the chat-LLM rerank stage",
    )
    model: str = Field(default="Qwen/Qwen2.5-7B-Instruct", description="LLM model")
    api_key: SecretStr | None = Field(default=None, description="LLM API Key")
    base_url: str = Field(
        default="https://api.siliconflow.cn/v1",
        description="LLM API Base URL",
    )
    proxy: str | None = Field(default=None, description="HTTP proxy for the LLM API")
    timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        description="Timeout of one LLM HTTP request in seconds",
    )
    rerank_timeout_seconds: float = Field(
        default=15.0,
        gt=0,
        description="End-to-end limit of chat-LLM reranking; a timeout keeps the original order",
    )
    max_candidates: int = Field(
        default=50,
        ge=10,
        le=100,
        description="Most candidates sent to the LLM reranker",
    )
    output_top_k: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Most candidates the LLM may move to the head",
    )
    # The median chunk is about 1,560 characters and 99% exceed 400; a shorter
    # cut shows the LLM only the opening of each snippet.
    snippet_chars: int = Field(
        default=1600,
        ge=200,
        le=4000,
        description="Character limit per candidate sent to the LLM",
    )
    # One rerank call may reach 16k tokens; without a limiter a dozen queries
    # produce consecutive 429s and a silent fallback to the original order.
    tpm_limit: int = Field(
        default=60_000,
        ge=1_000,
        description="TPM limit of the LLM API; the client queues on a sliding window",
    )


class RetrievalSettings(BaseSettings):
    """Retrieval pipeline settings."""

    model_config = _settings_config("RETRIEVAL_")

    default_top_k: int = Field(
        default=50, ge=1, le=200, description="Dense recall size"
    )
    vector_threshold: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Milvus dense similarity threshold; no prefilter by default",
    )
    final_select_k: int = Field(
        default=10, ge=1, le=50, description="Final result count"
    )

    rrf_k: int = Field(
        default=60, ge=1, description="Reciprocal rank fusion smoothing constant"
    )

    confidence_floor: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Recall confidence floor applied before any model reranking",
    )

    # Both rerankers only reorder; neither prunes candidates. RERANK_ENABLED and
    # LLM_RERANK_ENABLED authorize the stages (api/chat send data out, local
    # does not); these policies only decide which queries an enabled model
    # sees: adaptive skips when exact/path evidence already answered, always
    # is for quality-first runs and reproducible comparisons.
    rerank_policy: RerankPolicy = Field(
        default="adaptive",
        description="Routing policy of the dedicated reranker",
    )
    llm_rerank_policy: RerankPolicy = Field(
        default="adaptive",
        description="Routing policy of the chat-LLM reranker",
    )

    exact_timeout_seconds: float = Field(
        default=2.0,
        gt=0.0,
        description="SQL exact identifier recall timeout; vector recall answers after it",
    )
    exact_enabled: bool = Field(
        default=True,
        description="Enable SQL exact identifier recall",
    )

    source_priority_enabled: bool = Field(
        default=True,
        description="Demote documentation, tests and barrel files by the source prior",
    )
    coverage_selection_enabled: bool = Field(
        default=True,
        description="Use the focused/coverage selector; off selects plain Top-K",
    )

    query_decomposition_enabled: bool = Field(
        default=True, description="Decompose multi-sentence requests into facets"
    )
    query_max_queries: int = Field(
        default=4, ge=1, le=8, description="Total of the original query and its facets"
    )
    query_min_facet_chars: int = Field(
        default=8, ge=1, description="Minimum facet length in characters"
    )
    query_facet_weight: float = Field(
        default=0.75, gt=0.0, le=1.0, description="Fusion weight of a facet"
    )
    per_query_top_k: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Recall size per facet when several are fused",
    )

    # Selection and coverage: the character budget is a hard limit,
    # final_select_k a soft one.
    max_chunks_per_path: int = Field(
        default=2, ge=1, le=20, description="Most chunks returned per file"
    )
    focused_max_chunks_per_path: int = Field(
        default=4,
        ge=1,
        le=20,
        description="Most chunks returned per file in focused mode",
    )
    max_context_chars: int = Field(
        default=32_000, ge=1, description="Hard character budget of the returned code"
    )
    focused_max_context_chars: int = Field(
        default=12_000,
        ge=1,
        description="Hard character budget of focused symbol/path requests",
    )
    overlap_threshold: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description="Overlap threshold for suppressing same-file chunks",
    )

    # Query rewrite (LLM-based query expansion for better recall)
    # Off by default: it only helps special cases such as cross-language file
    # names and does little for ordinary retrieval.
    query_rewrite_enabled: bool = Field(
        default=False, description="Rewrite queries with the LLM"
    )
    query_rewrite_model: str = Field(
        default="Qwen/Qwen2.5-7B-Instruct",
        description="LLM model used for query rewriting",
    )
    query_rewrite_num: int = Field(
        default=3, ge=1, le=5, description="Number of rewritten queries"
    )

    # Path index: a separate collection for file-name requests.
    path_index_enabled: bool = Field(
        default=True, description="Enable the path index for file-name requests"
    )
    path_top_k: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Files recalled from the path index per query variant",
    )
    # Path evidence is a bounded boost on fused candidates, never a
    # replacement for content hits, so the right chunk is not displaced.
    path_boost_weight: float = Field(
        default=0.5,
        ge=0.0,
        le=2.0,
        description="Boost weight of path evidence on same-file chunks",
    )
    # Exact path lookup: file names and paths in the request (traceback
    # frames included) are suffix-matched inside the scope without an
    # embedding; hits share the path boost weight.
    path_lookup_enabled: bool = Field(
        default=True, description="Enable SQL exact path/file-name suffix matching"
    )

    # Lexical recall: a full-text index over chunk tokens that reaches error
    # text and call sites dense recall is blind to. This is the capability
    # switch; symbol/path requests only run it when structural evidence is
    # missing, semantic requests always.
    lexical_enabled: bool = Field(
        default=True, description="Allow lexical recall per query"
    )
    lexical_top_k: int = Field(
        default=30, ge=1, le=200, description="Lexical recall size"
    )
    lexical_weight: float = Field(
        default=1.0,
        gt=0.0,
        le=2.0,
        description="Weight of lexical hits in reciprocal rank fusion",
    )
    lexical_timeout_seconds: float = Field(
        default=2.0,
        gt=0.0,
        description="Lexical recall timeout; the other lanes answer after it",
    )

    # Deterministic answers do not wait for the embedding: a symbol's exact
    # definition, a path request's SQL path hit and a reference request's
    # call/inherit sites all come from SQL lanes, dense recall would only
    # append an evidence-free semantic tail, and the remote embedding round
    # trip is the slowest stage with a long tail. The gate is a binary
    # structural fact, never a score; import-only evidence does not count.
    decisive_skips_dense: bool = Field(
        default=True, description="Skip dense recall once the SQL lanes have answered"
    )

    # Source head slots: the first N results of a semantic request go to
    # source files the prior did not demote. A multiplicative prior is too weak
    # on normalized RRF (a test chunk in both the dense and lexical list stays
    # first) and a reranker is not always enabled; requests that ask about
    # tests are exempt. 0 disables.
    source_head_slots: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Head slots reserved for source files on semantic requests",
    )
    # A chunk whose only symbol evidence is imports is a file header (use/import
    # lines, a license comment, a module docstring): it names every module the
    # file touches, so it sits close to "architecture/flow" wording in vector
    # space while implementing none of it. Such chunks yield the head slots;
    # chunks with no symbol evidence at all are untouched. Net zero on the
    # three older benches (2026-09-04); on the project_cases judge
    # (2026-09-08) the one distracting head (an import header on a call-chain
    # query) disappeared and the other four suites did not move. On by default.
    head_skips_import_headers: bool = Field(
        default=True,
        description="Let import-only file headers yield the source head slots",
    )
    # Reference requests: chunks with exact/lexical use evidence fill the head
    # slots by prior tier; use sites that live only in tests, examples or
    # __init__ files still beat documentation without evidence.
    reference_head_fallback: bool = Field(
        default=True,
        description="Fill reference head slots by prior tier when no source use site exists",
    )
    # Compound (issue-text) requests: the declarations of traceback frames
    # (function plus the file declaring it) and of the title's identifiers
    # (declared in at most three places) take protected head slots; names
    # that only appear in the body (a minimal example's helpers, fixtures)
    # anchor nothing. 0 disables. Anchoring on any named identifier once
    # regressed by locking onto a setup call in a reproduction, so only
    # these two structural facts count.
    compound_anchor_slots: int = Field(
        default=3,
        ge=0,
        le=5,
        description="Head slots reserved for frame/title anchors on compound requests",
    )

    # Hub lane: declared names the words of an overview or symbol-free
    # call-chain request spell (``Router``, ``register_checker``,
    # ``createSlice``) ranked by referencing files; the largest declaration
    # takes a protected head slot, package names (also directories) never
    # lead, and names declared in more than hub_max_definitions places are
    # too common. Paired rerun 2026-09-09: curated overview nDCG@10 67.8 to
    # 74.3 but the sealed held-out semantic set fell (overview 66.4 to 54.1,
    # call-chain 85.6 to 78.2), so it ships off (0) as a measurable switch.
    hub_head_slots: int = Field(
        default=0,
        ge=0,
        le=5,
        description="Head slots reserved for hub declarations on semantic requests; 0 disables",
    )
    hub_max_definitions: int = Field(
        default=3,
        ge=1,
        le=20,
        description="Most declarations a hub name may have inside the scope",
    )

    # Working-set prior: the files in a request's added_blobs are the ones the
    # user is editing. A large delta (a first full sync) carries no
    # information and skips the prior.
    working_set_boost: float = Field(
        default=1.15,
        ge=1.0,
        le=2.0,
        description="Multiplicative boost of hits inside added_blobs",
    )
    working_set_boost_max_blobs: int = Field(
        default=50,
        ge=0,
        description="Skip the boost above this many added_blobs; 0 disables",
    )

    # Result shaping: touching same-file chunks merge; a second hop pulls
    # definition excerpts of referenced symbols.
    merge_adjacent_enabled: bool = Field(
        default=True, description="Merge adjacent or overlapping same-file chunks"
    )
    related_definitions_enabled: bool = Field(
        default=True,
        description="Append related definition excerpts to relation-oriented requests",
    )
    related_source_hits: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Leading primary hits mined for referenced identifiers",
    )
    related_max_symbols: int = Field(
        default=8,
        ge=1,
        le=50,
        description="Most symbols whose definitions are appended",
    )
    related_max_definitions_per_symbol: int = Field(
        default=3,
        ge=1,
        le=20,
        description="Symbols declared in more places than this are too common",
    )
    related_snippet_lines: int = Field(
        default=12, ge=1, le=200, description="Most lines per definition excerpt"
    )
    related_max_chars: int = Field(
        default=4_000,
        ge=1,
        description="Character cap of definition excerpts, also bounded by the remaining budget",
    )

    # Relation lanes (callers, implementations and subclasses, tests,
    # re-exports) each have their own slots and character cap and are returned
    # as sections after the primary results. relation_reserve_chars is an
    # upper bound once novel relation evidence exists, never an unconditional
    # deduction from the primary budget.
    relation_reserve_chars: int = Field(
        default=6_000, ge=0, description="Character cap of the relation sections"
    )
    relation_snippet_lines: int = Field(
        default=10, ge=1, le=200, description="Most lines per relation excerpt"
    )
    callers_enabled: bool = Field(
        default=True, description="Append the callers section"
    )
    callers_max: int = Field(
        default=4, ge=1, le=20, description="Most callers appended"
    )
    callers_max_chars: int = Field(
        default=2_400, ge=1, description="Character cap of the callers section"
    )
    call_chain_max_hops: int = Field(
        default=1,
        ge=1,
        le=3,
        description="Most upstream hops through unique enclosing definitions",
    )
    # Two-endpoint chains ("how does A reach B"): a bounded search downward
    # from A's declaration along called names, following only definitions
    # that resolve uniquely in scope (or sit in the caller's file), stopping
    # at B. Depth, calls examined per definition and definitions expanded are
    # fixed bounds, not tunable thresholds.
    call_chain_max_depth: int = Field(
        default=4,
        ge=1,
        le=8,
        description="Most downward hops of a two-endpoint chain search",
    )
    # At most two excerpts per hop (declaration header plus the handover
    # window); four hops need about 3,000 characters.
    call_chain_max_chars: int = Field(
        default=3_600, ge=1, description="Character cap of the call path section"
    )
    implementations_enabled: bool = Field(
        default=True, description="Append the implementations section"
    )
    implementations_max: int = Field(
        default=4,
        ge=1,
        le=20,
        description="Most implementations or subclasses appended",
    )
    implementations_max_chars: int = Field(
        default=1_600, ge=1, description="Character cap of the implementations section"
    )
    tests_enabled: bool = Field(default=True, description="Append the tests section")
    tests_max: int = Field(
        default=3, ge=1, le=20, description="Most test excerpts appended"
    )
    tests_max_chars: int = Field(
        default=2_400, ge=1, description="Character cap of the tests section"
    )
    reexports_enabled: bool = Field(
        default=True, description="Append the re-exports section"
    )
    reexports_max: int = Field(
        default=2, ge=1, le=10, description="Most re-exports appended"
    )
    reexports_max_chars: int = Field(
        default=600, ge=1, description="Character cap of the re-exports section"
    )
    # When a symbol is declared in more places than the head holds, let the
    # adaptive route run the dedicated reranker over the tail. No offline
    # labels support enabling it yet; it stays as a switch awaiting calibration.
    rerank_ambiguous_definitions: bool = Field(
        default=False,
        description="Rerank the tail of symbol requests whose name exceeds the head slots",
    )


class RedisSettings(BaseSettings):
    """Redis task queue settings."""

    model_config = _settings_config("REDIS_")

    url: str = Field(
        default="redis://localhost:6379/0", description="Redis connection URL"
    )
    queue_name: str = Field(
        default="oce:embed_queue", description="Embedding queue name"
    )


class WorkerSettings(BaseSettings):
    """Background embedding worker settings."""

    model_config = _settings_config("WORKER_")

    enabled: bool = Field(default=True, description="Run the background worker")
    concurrency: int = Field(
        default=2, ge=1, le=32, description="Concurrent consumer coroutines"
    )
    blob_batch_size: int = Field(
        default=16,
        ge=1,
        le=256,
        description="Most blobs one consumer handles per batch",
    )
    max_retries: int = Field(default=3, ge=1, le=10, description="Retry limit per blob")


class LogSettings(BaseSettings):
    """Logging settings."""

    model_config = _settings_config("LOG_")

    file_enabled: bool = Field(default=False, description="Write logs to a file")
    file_path: str | None = Field(
        default=None,
        description="Log file path; derived from the data directory when None",
    )
    rotation: str = Field(
        default="100 MB", description="Rotation: '1 day' by time or '100 MB' by size"
    )
    retention: str = Field(
        default="30 days", description="Retention: '30 days' or '10 files'"
    )
    format_json: bool = Field(
        default=False, description="Serialize log records as JSON"
    )
    level: str = Field(default="INFO", description="Log level (WARNING/INFO/DEBUG)")


class MonitoringSettings(BaseSettings):
    """Monitoring: API call, token and resource collection."""

    model_config = _settings_config("MONITORING_")

    enabled: bool = Field(
        default=True, description="Collect and persist monitoring metrics"
    )
    flush_interval_seconds: float = Field(
        default=5.0, gt=0, description="Seconds between buffered batch writes"
    )
    flush_max_buffer: int = Field(
        default=500, ge=1, description="Buffer limit per metric kind"
    )
    resource_sample_interval_seconds: float = Field(
        default=60.0, gt=0, description="Seconds between resource samples"
    )
    retention_days: int = Field(
        default=30, ge=1, description="Days of monitoring data retained"
    )
    cleanup_interval_seconds: float = Field(
        default=3600.0, gt=0, description="Seconds between monitoring cleanup runs"
    )
    retrieval_audit_enabled: bool = Field(
        default=True, description="Record per-stage retrieval timings and empty results"
    )
    store_query_text: bool = Field(
        default=False,
        description="Store the query text in retrieval audits; off for privacy",
    )


class Settings(BaseSettings):
    """Root settings aggregating every group."""

    model_config = _settings_config()

    # API
    api_key: str = Field(
        default="sk-opencontextengine",
        description="API key; personal mode uses the value the client expects, service mode needs a strong random one",
    )
    admin_api_key: str = Field(
        default="",
        description="Admin API key; empty falls back to API_KEY, set makes admin routes accept only this key",
    )
    cors_origins: str = Field(
        default="https://oce-ai.github.io",
        description="Browser origins allowed to call the API, comma separated; the default admits the official admin panel, empty disables CORS",
    )

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    milvus: MilvusSettings = Field(default_factory=MilvusSettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    rerank: RerankSettings = Field(default_factory=RerankSettings)
    chunking: ChunkingSettings = Field(default_factory=ChunkingSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    log: LogSettings = Field(default_factory=LogSettings)
    monitoring: MonitoringSettings = Field(default_factory=MonitoringSettings)


@lru_cache
def get_settings() -> Settings:
    """The process-wide settings instance."""
    return Settings()

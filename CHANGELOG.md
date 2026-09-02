# Changelog

本项目版本遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。
变更条目由 `scripts/generate_changelog.py` 生成。

## [Unreleased]

### Added

- **index lifecycle**: bind persisted artifacts to the server source-admission policy version so tightened filters require a clean resync
- **evaluation**: add production-wired ablation switches for semantic chunking, exact recall, source priority, and coverage selection
- **indexing**: persist a secret-free model/chunker/vector-store fingerprint and fail closed on incompatible reuse
- **embedding**: add a bounded TTL query-vector cache that stores hashed keys and clears on credential reload
- **admin**: expose authoritative metadata, dense/path collection, and query-cache index statistics

### Security

- **development compose**: bind database, queue, object-store, and Milvus ports to localhost instead of every host interface

### Fixed

- **chunking**: skip whitespace-only cAST inputs instead of raising on an out-of-range EOF chunk
- **migrations**: verify the complete SQLite migration chain can roll back to base and upgrade to head again
- **admission**: reject common environment, key, and credential files before indexing
- **embedding**: apply query instructions in the credential-backed client and reject incompatible hot reloads
- **indexing**: keep blobs retryable when an enabled path index cannot be written instead of marking an incomplete index ready
- **llm**: preserve TLS verification through proxies, keep source queries out of logs, and degrade intent failures to heuristic routing
- **llm**: fail locally when an enabled LLM feature has no usable credential
- **monitoring**: retain buffered samples after transient persistence failures
- **monitoring**: attribute chat-model token usage to rerank and query-rewrite stages

### Changed

- **symbols**: inject the production regex symbol provider through a domain protocol instead of constructing parsing policy inside SQL persistence
- **config**: default intent classification off and keep all optional LLM calls off in newly generated personal configurations
- **compose**: keep the Milvus data port internal to the service network
- **retrieval**: use one canonical intent model and remove strategy options not connected to the pipeline
- **retrieval**: preserve exact identifier recall for large checkpoints through relational or bounded-batch scope filtering
- **retrieval**: route queries from deterministic signals instead of a mandatory LLM intent classifier
- **retrieval**: compose candidate-preserving dedicated and chat-LLM rerankers with structural adaptive routing, bounded fallback, and task-aware selection

## [0.2.0] - 2026-08-30

### Added

- **credentials**: redesign into multi-kind model_credentials table
- **cors**: default CORS_ORIGINS to official admin panel
- **cors**: allow admin frontend cross-origin requests
- **api**: expose public /version endpoint
- **admin**: add credential/queue/GC admin API and retire overview/paths endpoints
- **config**: preset personal-mode auth key and default to Qwen3-Embedding-4B
- **monitoring**: add read-only /admin/stats aggregation endpoint
- **monitoring**: add background cleanup of expired monitoring rows
- **monitoring**: add background resource sampler (disk/memory/cpu)
- **monitoring**: add HTTP call metrics middleware
- **monitoring**: collect token usage from embedder, reranker and LLM client
- **monitoring**: add metrics foundation and retrieval stage audit
- **batch-upload**: accept optional checkpoint_id to register uploaded blobs
- **retrieval**: require explicit working-set scope; drop delete side effects
- **logging**: add file logging with rotation and retention

### Fixed

- **llm**: disable reasoning for openrouter, fallback to reasoning content

### Changed

- sync README/AGENTS with current API, credentials, monitoring
- **env**: backfill logging/monitoring/admin/cors entries in .env.example

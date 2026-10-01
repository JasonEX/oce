# Changelog

This project follows [Semantic Versioning](https://semver.org/).
Unreleased entries summarize the current changes since the latest release.
[`scripts/generate_changelog.py`](scripts/generate_changelog.py) can generate a
draft from Conventional Commits; review it against the final implementation.

## [Unreleased]

### Added

- **indexing**: store enclosing scope context on cAST chunks and embed `File + Context + code`; persist a secret-free index profile and reject incompatible startup or model reloads.
- **retrieval**: add scoped SQL lexical recall, exact path lookup, traceback/title anchors, qualified-symbol evidence and bounded working-set/source priors.
- **relations**: record call, enclosing-definition, re-export and inheritance evidence; return bounded callers, implementations, tests, re-exports, related definitions and call-chain sections.
- **reranking**: support an optional in-process ONNX provider alongside the API reranker and chat-LLM cascade. Both rerankers remain disabled by default; enabling local reranking does not authorize external calls.
- **embedding**: bound long query inputs, coalesce concurrent identical queries, share concurrency limits across credential generations, and cache only query hashes/vectors in an optional TTL LRU.
- **monitoring**: expose safe index/runtime provenance, retrieval stages, lane failures, rerank decisions, declaration counts and relation budgets through existing admin metrics.
- **evaluation**: add reviewed multilingual semantic/relation suites, CodeSearchNet guards, wording/layout controls and external issue profiles driven by the released Rust client; archive paired measurements separately from implementation microbenchmarks.

### Fixed

- **persistence**: enable SQLite foreign keys and remove historical orphan metadata with migration `b8c9d0e1f2a3`; retain live metadata. Use WAL/busy timeouts and bounded scope queries for personal-mode concurrency.
- **deletion**: retain `DELETING` identities until dense and path cleanup succeed, retry failed cleanup, and recheck activity/checkpoint references. Reject stale indexing/checkpoint writes without changing legal pending or absent checkpoint membership.
- **indexing**: keep embedding round trips outside metadata transactions, reread pending identities after lock/touch, and prevent duplicate uploads from resetting their state. Recover pending Redis delivery from durable SQL truth.
- **queue**: preserve confirmed batch claims when a later Redis fill fails; atomically update queue projections during maintenance. Count queue state without loading every identity.
- **credentials**: serialize runtime reloads, validate explicitly disabled embedding without activating credentials, and report partial LLM reload failures through the existing response fields.
- **configuration**: parse personal-mode `.env` and `.env.local` together so local values override base values while process variables retain priority; preserve explicit `--env-file` precedence and interpolation.
- **paths**: enforce the 1024-character upload limit, keep full embedding/result paths, and fit UTF-8 diagnostic text into existing Milvus fields. Existing longer SQLite paths remain readable.
- **retrieval**: freeze ready scope and route/title evidence, report failed lanes rather than treating them as empty matches, and release shared embedding work without cancelling provider requests.
- **budgets**: enforce the hard code-content budget in both selectors and account for adjacent-merge separators; clip a leading excerpt when required while preserving complete lines where possible.
- **audit**: count recorded declarations before chunk deduplication and recall limits; keep these counts diagnostic rather than adding uncalibrated rerank inputs.
- **symbols/routing**: correct whole-token/private-name extraction, qualified endpoints and symbol kinds; route Chinese flow constructions without treating single characters as call-chain evidence.
- **runtime**: move resource sampling off the event loop and retain bounded startup probes. Apply explicit local dense-index type changes without discarding vectors; fail initialization when verification/build fails.

### Changed

- **architecture**: use explicit Container ownership and worker lifecycle states, direct application use cases, one shared short-transaction indexing flow, and fixed retrieval stages with frozen route/plan/recall evidence.
- **reranking**: separate feature authorization from per-query adaptive/always policies; use deterministic route/candidate/exact/path evidence and preserve protected structural heads.
- **documentation**: organize bilingual user guides, retrieval/runtime contracts, developer constraints and dated evaluation evidence through one documentation index.
- **compatibility**: source admission is version 2, scope-aware chunking is version 4, embedding input is version 2 and symbol extraction is version 5. Incompatible persisted profiles require a new data directory and full client resync; the orphan-cleanup migration alone does not change the index fingerprint.

### Removed

- **experiments**: retire negative hub recall, uncalibrated ambiguous-definition routing and confidence-floor branches. Import-header/reference fallback rules are fixed behavior rather than stale configuration switches.
- **architecture**: remove the generic message bus, forwarding-only credential handlers, unused path-result text and the domain same-transaction indexing convenience flow; production callers share application orchestration.

## [0.3.0] - 2026-09-02

### Added

- **evaluation**: add a pinned 30-query symbol/path/reference suite that verifies adaptive rerank routes and stage latency against the production client and local audit store
- **evaluation**: add a source-pinned SWE-bench Verified and SWE-Explore issue-resolution benchmark with separate gold-edit diagnostics and official SWE-Explore context metrics
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

- **reranking**: separate model authorization from deterministic per-query routing, persist the chosen route, and pass a code-search task instruction to instruction-aware dedicated rerankers
- **intent routing**: classify multi-facet issue text as compound, require complete English call-verb tokens, and exclude path tokens from exact-identifier evidence
- **distribution**: publish releases only as the fork-owned `ghcr.io/jasonex/oce` image and stop publishing this fork to PyPI
- **chunking**: centralize source-aligned span emission across cAST and document fallbacks, and bump the chunker profile so existing indexes require a clean resync
- **symbols**: inject the production regex symbol provider through a domain protocol instead of constructing parsing policy inside SQL persistence
- **config**: remove mandatory LLM intent classification and keep all optional LLM calls off in newly generated personal configurations
- **compose**: keep the Milvus data port internal to the service network
- **retrieval**: use one canonical intent model and remove strategy options not connected to the pipeline
- **retrieval**: preserve exact identifier recall for large checkpoints through relational or bounded-batch scope filtering
- **retrieval**: route queries from deterministic signals instead of a mandatory LLM intent classifier
- **retrieval**: compose candidate-preserving dedicated and chat-LLM rerankers with structural adaptive routing, bounded fallback, task-aware selection, and benchmark-backed interactive/quality deployment guidance

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

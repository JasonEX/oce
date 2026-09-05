# Changelog

本项目版本遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。
变更条目由 `scripts/generate_changelog.py` 生成。

## [Unreleased]

### Added

- **retrieval**: append relation sections after the primary results: callers grouped per enclosing definition, implementations and subclasses, tests exercising the symbol, and barrel re-exports, each with its own slot and character cap, deduplicated against the primary spans and rendered as fixed-order sections (`RETRIEVAL_CALLERS_*`, `RETRIEVAL_IMPLEMENTATIONS_*`, `RETRIEVAL_TESTS_*`, `RETRIEVAL_REEXPORTS_*`, `RETRIEVAL_RELATION_RESERVE_CHARS`, `RETRIEVAL_RELATION_SNIPPET_LINES`)
- **retrieval**: resolve qualified names (`Session.get`, `Context.ShouldBindJSON`) to the declaration inside the named scope, order same-named overloads by the parameter types the request spells out, and give every declaring file a head slot before any file gets a second
- **retrieval**: route "which tests cover X" and "which classes implement X" to use-site retrieval, with evidenced test files taking the head for test questions
- **symbols**: record the enclosing definition of every occurrence, barrel re-exports (`export {} from`, `pub use`, relative and self-package imports in `__init__.py`/`index.ts`/`mod.rs`), and `extends`/`implements`/trait-impl edges (`SYMBOL_EXTRACTION_VERSION` 5; existing indexes must be rebuilt)
- **monitoring**: record the structural evidence the router saw (`exact_definitions`, `definition_sites`) and the size of the appended relation sections (`relation_hits`, `relation_chars`) per retrieval, and add an opt-in `RETRIEVAL_RERANK_AMBIGUOUS_DEFINITIONS` route for symbol requests whose name is declared in more places than the head holds
- **evaluation**: add `project_cases`, 35 LLM-assisted, tool-verified relation cases (reference, call-chain, test mapping, re-export, multi-implementation) with distractor files and one derived error class per case, and `csn_queries`, an 80-query CodeSearchNet docstring-to-function guard over 8 pinned repositories in four languages
- **symbols**: record call sites (`kind=call`) from tree-sitter for every grammar, extract Bash functions and JavaScript prototype/CommonJS assignments, and retry a transiently unavailable grammar instead of pinning the regex fallback for the process
- **rerank**: add an in-process ONNX cross-encoder provider (`RERANK_PROVIDER=local`, `uv sync --extra local-rerank`) so reranking can run without sending queries or source to a model endpoint
- **embedding**: cap the query text sent for embedding (`EMBED_MAX_QUERY_CHARS`, default 3,000 characters)
- **evaluation**: extend the curated black-box corpus to Go, C, C#, JavaScript, Java, and Bash (13 snapshots, 40 anchors, 39 reviewed semantic cases) with per-language report columns
- **retrieval**: add SQL lexical recall (SQLite FTS5 / PostgreSQL tsvector) over sub-word chunk terms, fused by rank with dense results
- **retrieval**: recover traceback frames, error phrases, and filenames from requests as exact path, symbol, and phrase evidence
- **retrieval**: append signature excerpts of definitions referenced by the top results and merge touching spans of one file
- **retrieval**: apply a bounded working-set prior to files the request just added
- **indexing**: embed cAST chunks with their enclosing scope chain and expose it as a `Context:` line and to both rerankers
- **symbols**: extract definitions, endpoints, and imports with tree-sitter from whole files, with regex fallback and frequency-damped exact scores
- **evaluation**: add a `standard` issue profile and Chinese variants of the routing queries
- **evaluation**: add six reviewed function anchors (96 routing queries), a reference definition-first diagnostic, and p50/p95 latency for issue runs
- **evaluation**: report head-of-list quality (Top-1, MRR, nDCG@100, first useful hit) next to recall in both benchmark comparison tables, and add an adversarial SQLite regression guard for symbol/path/reference head order

### Fixed

- **symbols**: stop recording CommonJS `var X = require(...)` aliases, Rust `impl Trait for Type` blocks, `let` bindings, and TypeScript/JavaScript `export { x } from` re-exports as definitions, and record no symbols at all for Markdown/reStructuredText/plain-text files whose fenced examples were read as project declarations (`SYMBOL_EXTRACTION_VERSION` 4; existing indexes must be rebuilt)
- **retrieval**: diversify exact use-site evidence per file before the candidate window closes, so a test module that calls a symbol in every chunk no longer pushes the one import in each other file out of the reference head
- **milvus**: flush Milvus Lite before the first search that follows a write so scoped dense and path searches stay on the HNSW index; an incremental upload of 1.4K blobs had raised workspace search latency from about 30 ms to about 800 ms until the growing segment was sealed
- **indexing**: embed pending chunks in pages of 256 instead of 64 so the embedder's concurrent batches are actually used during synchronous uploads (about 14 chunks/s before)
- **persistence**: open personal-mode SQLite in WAL mode with a busy timeout, so the metrics sink and concurrent readers no longer fail with "database is locked" during batch uploads
- **evaluation**: retry the idempotent black-box `sync` on transient transport resets (`run_client(..., retries=4)`), so a momentarily busy server no longer aborts a whole suite, while `retrieve` stays single-shot; and read the offline routing/evidence view from the `retrieval` metrics source (`benchmarks.internal.rerank_evidence --source`)

### Changed

- **retrieval**: when a reference question has no evidenced use site in undemoted source, fill the head slots with evidenced use sites in test, example, or barrel files ordered by prior instead of leaving the slots to documentation without occurrence evidence (`RETRIEVAL_REFERENCE_HEAD_FALLBACK`); add two measured-neutral ablation switches that stay off: `RETRIEVAL_HEAD_SKIPS_IMPORT_HEADERS` (import-only file headers yield source head slots) and `RETRIEVAL_COMPOUND_ANCHOR_SLOTS` (protected slots for the definitions an issue text names)
- **retrieval**: classify call-chain requests by their verb even without a symbol anchor, treat dotted qualified names (`Context.ShouldBindJSON`) as symbols rather than file names, decide overview before path, and only let file/config nouns imply a path request in short questions
- **retrieval**: neutralize the source prior only for questions that ask for tests, and demote `samples/` like `examples/`
- **retrieval**: damp exact-symbol scores by how many places declare a name, not by how often it is used
- **retrieval**: restructure the pipeline as an explicit `RetrievalState` machine (route → plan → recall → fuse → prior → rerank → select → expand)
- **retrieval**: route lexical recall to queries that benefit from it, preserve exact symbol/path answers in fixed head slots, give focused queries a smaller context budget, and constrain related definitions to relationship-oriented queries and the remaining context budget
- **rerank**: cap the query text sent to the dedicated reranker (`RERANK_MAX_QUERY_CHARS`, default 2,400) so long issue texts no longer multiply reranker latency, and reapply the source and reference head slots after model reranking
- **retrieval**: rank a file whose whole path is the tail of the request above sibling files that only share the two-segment suffix in exact path lookup, and treat `__tests__`, `__testfixtures__`, `*.test-d.ts`, `*.spec.*`, and `*_test.go` as test files in the source prior
- **monitoring**: persist lexical, exact-path, and related-definition stage latency plus the number of deterministic symbol/path head slots in retrieval audits
- **retrieval**: extend the source prior to change logs, singular `doc/` and `examples/` directories, configuration files, `.pyi` stubs, and `__init__.py` barrels, keep it active for compound issue text that merely mentions file names, and neutralize it only for short questions that ask about tests
- **retrieval**: reserve bounded head slots for implementation files on semantic requests and for lexically verified use sites ahead of the declaration on reference requests; start exact, path-lookup, and lexical SQL recall before the query embedding round trip
- **retrieval**: gate reference-query lexical recall on the whole-identifier surrogate so call sites outrank chunks that only share sub-words, and backfill an exact SQL path match for path requests the content index never mentions
- **index lifecycle**: bump schema, chunker, embedding, and symbol versions; existing indexes require a clean data directory and full resync

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

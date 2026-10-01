<div align="center">

<img src="assets/opencontextengine-logo.svg" alt="OpenContextEngine" width="75%"/>

# OpenContextEngine

**Self-hosted, ACE-compatible code retrieval for AI coding agents.**

Dense + exact + lexical + path retrieval · semantic chunking · optional reranking

[English](README.md) · [简体中文](README.zh-CN.md)

[![CI](https://img.shields.io/github/actions/workflow/status/JasonEX/oce/ci.yml?branch=master&logo=github&label=CI)](https://github.com/JasonEX/oce/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-ghcr.io-2496ED?logo=docker&logoColor=white)](https://github.com/JasonEX/oce/pkgs/container/oce)
[![FastAPI](https://img.shields.io/badge/API-FastAPI-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Milvus](https://img.shields.io/badge/Vectors-Milvus%203.0-00A1EA.svg)](https://milvus.io/)
[![ACE](https://img.shields.io/badge/ACE-compatible-success.svg)](#api)

</div>

OpenContextEngine is a self-hosted, ACE-compatible code retrieval service for AI coding
agents. It combines semantic vectors, exact symbols, lexical text, and file paths to
return source context from a client-declared workspace.

Use **personal mode** (SQLite + embedded Milvus Lite, synchronous indexing) on one machine.
Use **service mode** (PostgreSQL + Milvus 3.0 + Redis, background indexing) when multiple
users or machines share an index. Both use the same retrieval and indexing contracts.

The server and client are maintained separately:

- Server: <https://github.com/JasonEX/oce>
- Client: <https://github.com/JasonEX/oce-client>

This is the refactored successor to the earlier ACE service. See the original
[linux.do discussion](https://linux.do/t/topic/2308140/125) for background.

## Features

- **Hybrid retrieval** — combines independent dense, exact symbol, SQL lexical, path index, and exact path lookup lanes according to query intent.
- **Semantic source context** — cAST/tree-sitter chunking preserves code boundaries and enclosing scope; results can include related definitions, callers, implementations, tests, and re-exports.
- **Optional reranking** — an API or local ONNX reranker and a chat LLM can run individually or as a cascade; final selection applies task-specific code character budgets.
- **ACE-compatible API and MCP client** — bearer-authenticated upload, checkpoints, and retrieval, with workspace scoping.
- **Operations and monitoring** — separate admin authentication for model credentials, queue recovery, garbage collection, and index/call/token/resource statistics.
- **[Black-box benchmarks](benchmarks/README.md)** — released clients and stable APIs evaluate multilingual lookups, reviewed architecture queries, and source-pinned issue tasks.

<details>
<summary><strong>Table of contents</strong></summary>

- [Requirements](#requirements)
- [Personal mode](#personal-mode)
- [Service mode](#service-mode)
- [Optional models](#optional-models)
- [Operations](#operations)
- [Client and MCP](#client-and-mcp)
- [API](#api)
- [Architecture and retrieval](#architecture-and-retrieval)
- [Tests](#tests)
- [License](#license)

</details>

## Requirements

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/)
- An embedding endpoint and its credentials for the default vector-enabled configuration

Personal mode requires no separate database, vector, or queue service. Service mode uses
PostgreSQL 16, Milvus 3.0, and Redis; `docker-compose.dev.yml` provides the development stack.

## Personal mode

Install the CLI and generate its configuration:

```powershell
uv tool install "git+https://github.com/JasonEX/oce.git"
oce init                    # writes ~/.oce/data/.env
```

This fork does not publish to PyPI. The command installs the current Git source; released
server images are available on GHCR.

Edit `~/.oce/data/.env` and set the embedding key. The default endpoint uses SiliconFlow
and Qwen3-Embedding-4B with 1024-dimensional vectors:

```dotenv
EMBED_API_KEY=your_embedding_service_key
# Change these when using another endpoint or model.
EMBED_ENDPOINT=https://api.siliconflow.cn/v1/embeddings
EMBED_MODEL=Qwen/Qwen3-Embedding-4B
EMBED_DIMENSIONS=1024
```

Then start the service:

```powershell
oce serve                   # http://127.0.0.1:8986
```

`oce serve` applies database migrations and supplies the personal-mode defaults: a SQLite
database and Milvus Lite file in the data directory, with `WORKER_ENABLED=false`. Uploads
are indexed synchronously. The generated API key is `sk-opencontextengine`, matching the
client default. Set a strong `API_KEY` and the same client `OCE_API_KEY` when exposing the
service beyond your machine.

A sibling `.env.local` overrides the data directory's `.env`; existing process variables
win over both. `${VAR}` references follow file order across these two files and use the
process value when one is present. Useful options:

| Option | Purpose |
| --- | --- |
| `--data-dir <path>` | Database, vector file, and configuration directory; default `~/.oce/data` |
| `--env-file <path>` | Load a selected file instead of the personal files; its supplied values override process variables |
| `--host <addr>` / `--port <n>` | Bind address; default `127.0.0.1:8986` |
| `--reload` | Enable Uvicorn's development reload |

`oce version` and `oce --version` print the version. `oce -v serve` enables INFO logs;
`oce -vv serve` enables DEBUG; the CLI default is WARNING.

For a temporary run without installing:

```powershell
uvx --from "git+https://github.com/JasonEX/oce.git" oce serve
```

## Service mode

The root Compose setup starts the application, PostgreSQL, Redis, and Milvus dependencies:

```powershell
git clone https://github.com/JasonEX/oce.git
Set-Location oce
Copy-Item .env.example .env
# Set API_KEY, ADMIN_API_KEY, EMBED_API_KEY, POSTGRES_PASSWORD, and REDIS_PASSWORD.
docker compose up -d
```

Only the application API is published to the host. The application container applies
migrations on startup. Keep real credentials outside the repository. Compose injects
`.env` into the application environment; to use another file, configure Compose's
`env_file` explicitly.

To run the application from a source checkout with only the dependencies in Docker:

```powershell
uv sync --extra dev
docker compose -f docker-compose.dev.yml up -d
# Set DB_URL and REDIS_URL to the development file's host ports and credentials.
# DB: 127.0.0.1:25432; Redis: 127.0.0.1:26379; Milvus: 127.0.0.1:19530.
uv run alembic upgrade head
uv run uvicorn oce.main:app --host 127.0.0.1 --port 8986
```

All settings groups read `.env` and then `.env.local` from the source run's working
directory, with process variables taking priority. See [.env.example](.env.example) for
the available configuration and defaults.

A published image can also be used in your own orchestration:

```powershell
docker pull ghcr.io/jasonex/oce:latest
```

For a fixed deployment, select a versioned image. Supply `DB_URL`, `REDIS_URL`, and
`MILVUS_ENDPOINT`; the image listens on container port `8986`.

## Optional models

Embedding sends admitted source chunks and semantic queries to `EMBED_ENDPOINT`. An API
reranker sends queries and candidate source to `RERANK_ENDPOINT`. Chat reranking sends
queries and candidate snippets to its LLM endpoint; query rewriting sends the query alone.
Use endpoints approved to receive that data. `RERANK_PROVIDER=local` performs reranking in-process without external calls.

Both reranking stages are disabled by default. To enable the API reranker:

```dotenv
RERANK_ENABLED=true
RERANK_PROVIDER=api
RERANK_API_KEY=your_rerank_service_key
RERANK_ENDPOINT=https://provider.example.com/v1/rerank
RERANK_MODEL=Qwen/Qwen3-Reranker-0.6B
RERANK_TOP_N=50
RERANK_MAX_QUERY_CHARS=2400
RETRIEVAL_RERANK_POLICY=adaptive
# Clear RERANK_INSTRUCTION for providers that do not support task instructions.
```

A chat LLM can run alone or after the dedicated reranker. This example limits its second
stage to 20 candidates; the configuration default is 50:

```dotenv
LLM_RERANK_ENABLED=true
LLM_API_KEY=your_llm_service_key
LLM_BASE_URL=https://provider.example.com/v1
LLM_MODEL=your_chat_model
LLM_MAX_CANDIDATES=20
LLM_RERANK_TIMEOUT_SECONDS=15
RETRIEVAL_LLM_RERANK_POLICY=adaptive
```

`RERANK_ENABLED` and `LLM_RERANK_ENABLED` authorize the stages. Their `RETRIEVAL_*_POLICY`
settings route individual queries: `adaptive` skips calls when deterministic evidence is
sufficient, while `always` runs on result sets with at least two candidates. Both stages
preserve candidates outside their ranking window. Query rewriting is separately opt-in
through `RETRIEVAL_QUERY_REWRITE_ENABLED=false` by default.

For a local ONNX reranker, install the optional dependencies from a source checkout and
provide the model files yourself:

```powershell
uv sync --extra local-rerank
# Run the installed source environment with uv run oce serve.
```

```dotenv
RERANK_ENABLED=true
RERANK_PROVIDER=local
RERANK_LOCAL_MODEL_DIR=/path/to/model-directory
# The directory contains model_int8.onnx and tokenizer.json by default.
```

Model licenses are separate from the server license. The benchmarked
[`jinaai/jina-reranker-v2-base-multilingual`](https://huggingface.co/jinaai/jina-reranker-v2-base-multilingual)
model uses CC-BY-NC-4.0; check its usage rights or choose a compatible export.

The [round-three report](benchmarks/results/utility-round3-2026-09-09.md) records the
local reranker's quality regressions and CPU latency for that historical configuration; evaluate optional models on your own workload. See the
[historical results index](benchmarks/results/README.md) for dated comparisons.

## Operations

### Admin panel and credentials

The official panel is <https://oce-ai.github.io/oce-admin>. Set a dedicated
`ADMIN_API_KEY`, enter the service URL and key in the panel, then manage credentials,
queue, GC, and statistics. An empty admin key falls back to `API_KEY`. The panel stores
its key in browser local storage; keep it out of URLs and logs. The official panel origin
is allowed by default. Set comma-separated `CORS_ORIGINS` for another origin, or an empty
value to disable CORS.

Credentials are selected by kind (`embed`, `rerank`, `llm_rerank`, `query_rewrite`), using
the active row with the lowest `priority`. Without a matching active row, the client
falls back to `EMBED_*`, `RERANK_*`, or `LLM_*`; an empty API rerank key falls back to the
embedding key. Credential responses expose only the last four key characters.

After credential edits, call `POST /admin/credentials/reload`. Compatible key, timeout,
and batching changes take effect without restarting. The response is
`{"reloaded": true, "reason": null}` on success. Check `reloaded` and `reason`: an
incompatible profile is rejected, and an LLM refresh failure can report a partial reload
while already-activated clients remain active. Enabling or disabling model stages through
environment configuration requires a restart. Reload refreshes database credentials; it
does not reread environment files. A successful reload validates local configuration and
index compatibility without probing the remote provider's key or availability.

### Index compatibility and query cache

OCE persists a secret-free index profile and checks it before admitting indexing work.
Changes to embedding identity, dimensions, document input semantics, or chunking/index
semantics require a new data directory, or new SQL storage and Milvus collections in
service mode, followed by full client resynchronization. Incompatible startup or reload
fails closed and preserves the old data. `EMBED_DIMENSIONS` is the common dimension for
both vector collections and credential validation.

Source-admission version 2 includes ordinary directories ending in `-retrieval-eval`.
Version 1 indexes require fresh storage and full resynchronization. Changing
`MILVUS_DENSE_INDEX_TYPE` explicitly rebuilds the local dense index while preserving its
vectors; this is distinct from changing the embedding identity.

Repeated semantic queries use an in-process query-vector LRU, defaulting to 256 entries
and a 600-second TTL (`EMBED_QUERY_CACHE_MAX_ENTRIES`, `EMBED_QUERY_CACHE_TTL_SECONDS`).
Set either to `0` to disable it. It stores query hashes and vectors; source vectors stay
in Milvus and retrieval results are not cached. Compatible embedding credential reloads
clear this cache. Monitoring query text is separately disabled by default
(`MONITORING_STORE_QUERY_TEXT=false`).

### Queue and garbage collection

In service mode, SQL pending blobs and staged source are the durable work record; Redis
carries deliveries. `GET /admin/queue` reports `main_size`, `inflight`, `db_pending`, and
`worker_state`. `inflight` counts unacknowledged delivery identities, including queued
and processing entries. With the worker disabled, the endpoint reports `enabled=false`
and zero queue counts; use `/admin/index-stats` for metadata counts.

`POST /admin/queue/reset` accepts `{"mode":"sync","requeue":true}` by default. `sync`
removes stale queue entries and restores pending delivery; `purge` clears the queue before
restoring pending work. `requeue=false` suppresses that immediate restoration, but worker
replay can enqueue durable pending work later. Resets drain active batches before mutation.
`POST /admin/queue/requeue-stale` accepts `stale_hours` (default 24) and `limit` (default 100)
for aged pending blobs with staging.

GC is an explicit admin operation; preview it before deleting:

```powershell
# Use the server API_KEY instead if ADMIN_API_KEY is unset.
$adminHeaders = @{ Authorization = "Bearer $env:ADMIN_API_KEY" }
$gc = @{ ttl_days = 30; dry_run = $true; limit = 1000 } | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8986/admin/gc `
  -Method Post -Headers $adminHeaders -ContentType application/json -Body $gc
# Repeat with dry_run = $false to apply the collection.
```

The API defaults to `ttl_days=30`, `dry_run=true`, and `limit=1000`; TTL must be at least
one day. Real GC drains worker batches, skips inflight identities, and rechecks recent
activity and checkpoint references before deleting expired blobs. Blobs released by a
checkpoint deleted in this run are collected on a later run.

A failed vector cleanup retains the blob as `deleting` for a later GC retry. It remains
excluded from retrieval; uploads and checkpoints that reuse that identity receive
retryable HTTP 503 until deletion finishes. `/admin/index-stats` includes
`metadata.blobs_deleting` alongside ready, pending, and error counts. After cleanup, the
same source can be uploaded again. State transitions and recovery details are in
[docs/runtime-lifecycle.md](docs/runtime-lifecycle.md).

## Client and MCP

The standalone Rust client scans the workspace, uploads changes, maintains checkpoints,
and retrieves code context. Download a Windows, Linux, or macOS archive from the
[client releases](https://github.com/JasonEX/oce-client/releases) and put `oce-client` on
`PATH`, or build it from source:

```powershell
cargo install --git https://github.com/JasonEX/oce-client --locked
$env:OCE_API_URL = "http://127.0.0.1:8986"
$env:OCE_API_KEY = "sk-opencontextengine"  # use the server API_KEY in service mode
$env:OCE_WORKSPACE = (Get-Location).Path

oce-client sync
oce-client retrieve "Where is request authentication implemented?"
```

The PyPI package `opencontextengine-client` is the superseded 0.1 client. For an AI coding
tool supporting MCP, use the same binary as a stdio server:

```powershell
oce-client mcp --workspace C:\path\to\workspace
```

It builds the initial index in the background, watches changes, and exposes the
`codebase-retrieval` tool. For multiple workspaces, repeat `--workspace`; tool calls must
include the matching `workspace_folder`. `OCE_API_URL`, `OCE_API_KEY`, and
`OCE_WORKSPACE`/`OCE_WORKSPACES` provide environment equivalents. Keep credentials in
process environment variables or a secret manager.

## API

- **Public:** `GET /health`, `GET /version` require no authentication. `/health` reports liveness; it does not probe model providers.
- **Data plane:** `Authorization: Bearer <API_KEY>`.
- **Admin (`/admin/*`):** `Authorization: Bearer <ADMIN_API_KEY>`, falling back to `API_KEY` when empty.

### Data-plane endpoints

| POST path | Request fields | Response fields |
| --- | --- | --- |
| `/find-missing` | `mem_object_names` | `unknown_memory_names`, `nonindexed_blob_names` |
| `/batch-upload` | `blobs: [{path, content}]`, optional `checkpoint_id` | `blob_names` |
| `/agents/codebase-retrieval` | `information_request`, `blobs`, optional `chat_history` | `formatted_retrieval`, `codebase_retrieval_elapsed_ms` |
| `/agents/blob-status` | `blobs` (checks `added_blobs` and `checkpoint_id`) | `unknown_blob_names`, `nonindexed_blob_names`, `checkpoint_not_found` |
| `/checkpoint-blobs` | `blobs` | `new_checkpoint_id` |

The shared `blobs` payload contains `checkpoint_id`, `added_blobs`, and `deleted_blobs`.
Blob names are SHA-256 of UTF-8 `path + content`. New uploads require paths of 1–1024
characters; existing longer SQLite paths remain readable. SQL and returned context preserve
the full path; the bounded strings stored
beside path vectors are diagnostic previews, while path embedding uses the full path
document. Admission skips dependency/build/cache directories, secret files, binary and
non-source artifacts; safe templates such as `.env.example`, project manifests, and test
fixtures have exemptions. Skipped uploads become empty ready blobs to avoid repeated uploads.

Background upload completion returns blob identities before indexing may finish. Use
`/find-missing` or `/agents/blob-status` to check readiness. Checkpoints can include pending
identities; retrieval admits only ready metadata.

Retrieval scope is `(checkpoint members ∪ added_blobs) − deleted_blobs`, restricted to
ready blobs. Declare a valid checkpoint or a non-empty added list; an empty resolved scope
returns an empty answer. `deleted_blobs` only narrows retrieval or updates checkpoint
membership through `/checkpoint-blobs`; physical cleanup belongs to GC. Retrieval with
missing scope or malformed checkpoint tokens returns HTTP 400; missing or outdated
checkpoints return 404.

```powershell
$headers = @{ Authorization = "Bearer $env:API_KEY" }
$body = @{
  information_request = "Where is request authentication implemented?"
  blobs = @{
    checkpoint_id = ""
    added_blobs = @("<blob-name-from-batch-upload>")
    deleted_blobs = @()
  }
} | ConvertTo-Json -Depth 4
Invoke-RestMethod http://127.0.0.1:8986/agents/codebase-retrieval `
  -Method Post -Headers $headers -ContentType application/json -Body $body
```

### Admin endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` / `POST` | `/admin/credentials` | List masked credentials / create a credential |
| `PATCH` / `DELETE` | `/admin/credentials/{id}` | Update / delete a credential |
| `POST` | `/admin/credentials/{id}/duplicate` | Clone a credential with optional overrides |
| `POST` | `/admin/credentials/reload` | Reload active runtime credentials |
| `GET` | `/admin/queue` | Queue counts and worker state |
| `POST` | `/admin/queue/reset` | Synchronize or purge queue delivery |
| `POST` | `/admin/queue/requeue-stale` | Enqueue aged pending blobs |
| `POST` | `/admin/gc` | Preview or collect expired checkpoints and blobs |
| `GET` | `/admin/stats` | Call, token, retrieval, and resource metrics |
| `GET` | `/admin/index-stats` | Metadata counts, dense/path stores, query cache, runtime settings, and index profile |

## Architecture and retrieval

Dependencies point inward: `shared ← domain ← application ← api`. Infrastructure
implements protocols and is assembled by `application/container.py`; the application
layer owns use cases and transaction boundaries. SQL stores metadata, symbols, and lexical
evidence (SQLite FTS5 / PostgreSQL `tsvector`); Milvus stores dense and path vectors.

Retrieval follows one fixed sequence: route → plan → recall → fuse → prior → rerank →
select → expand. SQL evidence begins before the query embedding round trip, and decisive
symbol, path, or use-site evidence can answer without waiting for dense recall. Results
use focused selection for symbol/path requests and coverage selection for broader tasks,
with default code content budgets of 12,000 and 32,000 characters respectively. Headers,
line numbers, and `Context:` labels add formatting outside that code content budget.

Relations use indexed names, occurrences, and enclosing declarations with ambiguity
limits; they do not bind dynamic receiver types to implementations. Optional lanes can
fail independently, with failures recorded in retrieval audit metrics. See
[the retrieval design](docs/retrieval-pipeline.md) for intent gates, head rules, relation
budgets, settings, and dated evaluation history, and
[the runtime lifecycle](docs/runtime-lifecycle.md) for indexing, recovery, and ownership.
The [documentation index](docs/README.md) links the maintained design and operator guides.

## Tests

From a source checkout, install development dependencies and run files independently so
Milvus Lite and tree-sitter runtimes are released between processes:

```powershell
uv sync --extra dev
uv run pytest tests/unit/application/test_service.py -q
uv run pytest tests/unit/domain/test_retrieval.py -q
uv run pytest tests/unit/infrastructure/test_milvus3.py -q
uv run pytest tests/unit/test_smoke_personal_mode.py -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

The smoke test uses the real container, migrations, Milvus Lite, and HTTP routes against
temporary storage and an in-process embedding endpoint. Avoid running the entire
`tests/unit/infrastructure` directory in one process on memory-constrained machines.
Pure retrieval refactors use `benchmarks.internal.retrieval_equivalence` for preservation;
product quality and latency are measured through [black-box benchmarks](benchmarks/README.md).

## License

Apache-2.0. OpenContextEngine is independent of Augment Code Inc.

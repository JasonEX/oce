# Retrieval benchmark system

The benchmark code is versioned with OCE, but product-utility evaluation is an
external consumer of the service. It reports observations and does not turn a
metric threshold into a release decision.

## Architecture boundary

```text
benchmarks/blackbox
        |
        | production CLI and stable JSON output
        v
  oce-client binary
        |
        | ACE-compatible HTTP API
        v
     OCE server

optional provenance only:
benchmarks/blackbox --> /version, /admin/index-stats, /admin/stats
```

Code under [`blackbox`](blackbox) must not import `oce`, database drivers,
SQLAlchemy, or Milvus. It never reads server persistence and does not require a
benchmark-only API. The optional admin calls expose feature switches and
aggregate model usage; they do not expose query text or individual retrieval
traces. [`test_blackbox_boundary.py`](../tests/unit/benchmarks/test_blackbox_boundary.py)
enforces this dependency rule.

[`internal`](internal) contains implementation microbenchmarks. Those may import
server modules, but their results diagnose a component and cannot support a
product-utility claim. This distinction keeps a fast Milvus experiment useful
without coupling the black-box evaluator to the implementation under test.

The black-box layer has four small infrastructure modules:

- [`harness.py`](blackbox/harness.py) owns the released-client subprocess,
  stable response parsing, safe runtime provenance, and paired-result identity.
- [`corpus.py`](blackbox/corpus.py) owns immutable Git snapshots.
- [`prewarm.py`](blackbox/prewarm.py) owns optional bounded index setup through
  client admission and production upload APIs.
- [`swe_data.py`](blackbox/swe_data.py) owns the pinned SWE-bench/SWE-Explore
  acquisition and truth conversion; curated suites do not depend on it.

The default cache is `~/.cache/oce/retrieval-bench-v1`. Datasets, repositories,
client state, and raw results stay outside this repository.

## Evaluation layers

| Suite | Current coverage | Primary question | Main metrics |
| --- | --- | --- | --- |
| `short_queries` | 22 anchors, 7 snapshots, 132 English/Chinese queries | Are known symbols, paths, and references at the head? | Top-1, MRR, Hit@10, path recall, reference definition-first rate |
| `semantic_queries` | 21 reviewed queries, 7 snapshots, balanced feature/overview/call-chain intents | Does broad retrieval return the right architectural owners compactly? | graded nDCG@10, weighted Recall@5/@10, primary Top-1, characters, latency |
| `swe_explore` | real issue text and trajectory/edit truth; 5/13/53/451-case profiles | Does issue-level retrieval reach useful context and likely edit locations? | official SWE-Explore metrics, edit/core Top-1 and Recall@10, characters, latency |

[`curated_corpus.json`](blackbox/curated_corpus.json) pins five Python snapshots
from the development issue set, Redux Toolkit v2.2.7 for TypeScript, and axum
v0.7.9 for Rust. [`short_query_anchors.json`](blackbox/short_query_anchors.json)
and [`semantic_cases.json`](blackbox/semantic_cases.json) reference those IDs;
the evaluator refuses unknown snapshots, mutable revisions, missing truth files,
or comparisons with different truth digests or ordered case IDs.

Use the layers at different cadences:

1. During implementation, run the relevant unit/regression test and the affected
   black-box suite.
2. Before changing a default retrieval strategy, run paired variants on all
   three development suites. Repeat any variant involving an external model at
   least twice.
3. Before making a broader product claim, add the `standard` 53-issue profile.
   Use the 451-case `verified` profile for milestone studies, not routine edits.

The curated truth is deliberately small and reviewable. It is suitable for
regression and ablation work, but it is not an independently reviewed benchmark.
There is not yet a downstream agent task-success suite or an ACE head-to-head
evaluation, so retrieval scores must not be presented as either result.

## Prepare and validate

Validate the reviewed query sets and prepare their pinned snapshots:

```bash
uv run python -m benchmarks.blackbox.short_queries check
uv run python -m benchmarks.blackbox.semantic_queries check
```

Prepare an issue profile separately:

```bash
uv run python -m benchmarks.blackbox.swe_explore \
  --profile development prepare
```

On a fresh personal-mode server, synchronous embedding of a large repository may
outlast the client's ordinary request timeout. Prewarm the shared curated corpus
before running `short_queries` or `semantic_queries`:

```bash
export OCE_API_KEY=...
uv run python -m benchmarks.blackbox.prewarm
```

The SWE profiles use issue-specific snapshots, so prewarm the selected profile
separately before paired issue runs:

```bash
export OCE_API_KEY=...
uv run python -m benchmarks.blackbox.swe_explore \
  --profile development prewarm
```

Both commands ask the released client for each workspace's admitted file list, then
use the production `/find-missing` and `/batch-upload` endpoints with smaller
batches. It therefore shares the client's ignore, sensitive-file, encoding,
size, and symlink policy while remaining setup rather than a client-throughput
measurement. The subsequent run still performs a complete real-client sync and
emits no quality report if a workspace fails to sync.

## Run paired variants

Start or restart OCE separately with the configuration being evaluated. Provide
the data key and, optionally, the admin key:

For a reranking change, the minimum useful matrix is `none` (both rerankers
disabled), `dedicated-always`, and `dedicated-adaptive`. Add an
`adaptive-cascade` run only when measuring the incremental value of chat-LLM
reranking. This separates three questions: whether reranking beats disabled,
whether adaptive preserves the quality of always, and whether chat adds enough
utility above the dedicated model. Run external-model variants at least twice.

```bash
export OCE_API_KEY=...
export OCE_ADMIN_API_KEY=...

uv run python -m benchmarks.blackbox.short_queries run \
  --label adaptive-r1 \
  --metadata deployment=personal \
  --output /tmp/oce-short-adaptive-r1.json

uv run python -m benchmarks.blackbox.semantic_queries run \
  --label adaptive-r1 \
  --metadata deployment=personal \
  --output /tmp/oce-semantic-adaptive-r1.json

uv run python -m benchmarks.blackbox.swe_explore \
  --profile development run \
  --label adaptive-r1 \
  --metadata deployment=personal \
  --output /tmp/oce-swe-adaptive-r1.json
```

`OCE_ADMIN_API_KEY` is optional. When present, the harness reads the runtime
retrieval switches, active index profile, store availability, and query-cache
state, then snapshots `/admin/stats` around retrieval to report only the
aggregate model-call/token delta. Automatic runner-environment metadata uses a
non-secret allowlist and excludes keys and endpoint URLs; it is not presented as
authoritative server configuration. Explicit `--metadata` remains the caller's
responsibility. Result JSON also records benchmark timing controls and contains
ranked paths and line spans, not retrieved source text or credentials.

The default six-second settling interval exceeds OCE's default five-second
metrics flush period. If the benchmark server uses a longer
`MONITORING_FLUSH_INTERVAL_SECONDS`, pass a correspondingly larger
`--metrics-settle-seconds`; this delay applies only when an admin key is present.

Use an otherwise idle benchmark server for latency and model-usage comparisons.
The admin counters are aggregate observations, so concurrent traffic would be
included in their before/after delta.

Compare like-for-like runs:

```bash
uv run python -m benchmarks.blackbox.short_queries compare \
  /tmp/oce-short-none-r1.json /tmp/oce-short-adaptive-r1.json

uv run python -m benchmarks.blackbox.semantic_queries compare \
  /tmp/oce-semantic-none-r1.json /tmp/oce-semantic-adaptive-r1.json

uv run python -m benchmarks.blackbox.swe_explore compare \
  /tmp/oce-swe-none-r1.json /tmp/oce-swe-adaptive-r1.json
```

A retrieval error remains in the denominator as zero utility; it is never
dropped from quality aggregates. Latency percentiles use successful calls, with
the error count shown alongside them. This separates system reliability from
the latency distribution without hiding either.

## Reading each suite

### Short structural queries

Each anchor expands into `symbol`, `path`, and `reference` queries in English and
Chinese. Definition and path truth is manually pinned. Reference truth is a
file-level lexical derivation over tracked, non-test source files; it is useful
for regression but is not a semantic call graph. `definition_top1` shows when a
reference query incorrectly answers with the declaration file.

This suite measures observable utility only. Whether the server internally
selected `skip:exact_definition`, `skip:path_evidence`, or a reranker is covered
by the retrieval strategy and pipeline unit tests. The benchmark intentionally
does not read `retrieval_metrics` or duplicate the server's routing state
machine.

### Reviewed semantic queries

Every snapshot has one feature, one architecture overview, and one call-chain
query. Relevant files carry grades 1-3 and a short role. nDCG rewards placing
the primary owners first; weighted recall rewards recovering the supporting
files. Reports split metrics by intent and by Python/TypeScript/Rust so an
overall gain cannot conceal a language or task regression.

The truth records architectural ownership, not every acceptable context file.
Review per-case ranked paths before changing grades; do not tune truth to favor a
specific retrieval variant.

### SWE-Explore issue retrieval

The harness joins pinned, SHA256-verified copies of:

- [SWE-bench Verified](https://huggingface.co/datasets/SWE-bench/SWE-bench_Verified),
  for issue text, base revisions, and accepted patches;
- [SWE-Explore](https://huggingface.co/datasets/SWE-Explore-Bench/SWE-Explore-Bench),
  for core files and regions from successful issue-solving trajectories; and
- [the reference evaluator](https://github.com/Qiushao-E/SWE-Explore-Bench/blob/5602f031f2d9562d0a805f83402b536e831a5a11/quality/bench_metrics.py),
  for the published line/ranking/context-efficiency formulas.

| Profile | Current size | Purpose |
| --- | ---: | --- |
| `pilot` | 5 issues / 5 repositories | end-to-end smoke |
| `development` | 13 issues / 5 repositories | paired strategy iteration |
| `standard` | 53 issues / 12 repositories | broader repository mix |
| `verified` | 451 joined issues | milestone evaluation |

`edit_*` measures gold-patch files and changed base-tree lines. `core_*` and
official `swe_explore_*` measure useful trajectory context. Neither truth source
is complete: accepted edits omit explanatory context, while trajectories are
model-dependent and sometimes mark coarse full-file regions. Interpret both
axes together.

SWE-Explore is CC BY-NC-ND 4.0. Review its terms before using the downloaded
data outside internal product evaluation.

## Internal component diagnostics

The Milvus workspace-filter microbenchmark is intentionally server-coupled:

```bash
uv run python -m benchmarks.internal.milvus_scope
```

It creates a temporary Milvus Lite database and emits one JSON observation. A
dated host-specific sample is retained in
[`results/milvus-lite-scope-2026-09-01.json`](results/milvus-lite-scope-2026-09-01.json).
It is not a release threshold or evidence of end-to-end retrieval quality.

The first black-box baseline, including the Milvus Lite flush and SQLite WAL findings it
surfaced, is in [`results/blackbox-baseline-2026-09-03.md`](results/blackbox-baseline-2026-09-03.md).
Earlier adaptive-rerank and head-order observations are retained in
[`results/swe-explore-development-2026-09-02.md`](results/swe-explore-development-2026-09-02.md)
and
[`results/swe-explore-development-2026-09-03.md`](results/swe-explore-development-2026-09-03.md).
Their raw result schema predates the current suite/truth identity contract, so use the reports
as narrative history rather than inputs to the current `compare` commands.

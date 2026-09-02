# Retrieval infrastructure benchmarks

These benchmarks are lightweight engineering diagnostics. They report observations and do
not turn thresholds into release decisions.

## Issue-resolution retrieval

[`swe_explore.py`](swe_explore.py) evaluates the production server through production
upload APIs and the real `oce-client` checkpoint/retrieval path. It joins two source-pinned
public datasets:

- [SWE-bench Verified](https://huggingface.co/datasets/SWE-bench/SWE-bench_Verified)
  supplies 500 expert-validated issues, the repository base commit, and the accepted patch.
- [SWE-Explore](https://huggingface.co/datasets/SWE-Explore-Bench/SWE-Explore-Bench)
  supplies core files and regions aggregated from successful issue-solving trajectories.
- [SWE-Explore's reference evaluator](https://github.com/Qiushao-E/SWE-Explore-Bench/blob/5602f031f2d9562d0a805f83402b536e831a5a11/quality/bench_metrics.py)
  supplies the official line coverage, ranking, and context-efficiency formulas.

The harness pins both dataset revisions and the evaluator revision, then verifies their
SHA256 checksums before use. It executes that pinned reference metric module locally during
scoring; it never fetches an unversioned evaluator.
Downloaded data, Git repositories, client state, and result files default to
`~/.cache/oce/swe-explore-v1`; none are copied into this Apache-2.0 repository.
SWE-Explore is licensed CC BY-NC-ND 4.0, so review its terms before using the data outside
internal product evaluation.

Three deterministic profiles trade iteration speed for coverage:

| Profile | Current size | Purpose |
| --- | ---: | --- |
| `pilot` | 5 issues / 5 repositories | End-to-end smoke and configuration checks |
| `development` | 13 issues / 5 repositories | Fast paired strategy experiments |
| `verified` | 451 joined issues | Full evaluation over the Verified/SWE-Explore intersection |

The two smaller profiles take a SHA256-stable sample from Flask, Requests, pytest, Pylint,
and xarray. They are development samples, not substitutes for the full profile. Run all
variants over the same ordered case IDs; `compare` refuses mismatched result sets.

Prepare the pinned repositories at each issue's base commit:

```bash
uv run python benchmarks/swe_explore.py --profile development prepare
```

On a fresh personal-mode server, a large repository can exceed the client's fixed HTTP
request timeout while synchronous embedding is still running. Start OCE with the common
embedding/chunking configuration, then prewarm the index in bounded serial batches before
starting paired runs:

```bash
export OCE_API_KEY=...
uv run python benchmarks/swe_explore.py --profile development prewarm
```

This setup step uses the production `/find-missing` and `/batch-upload` endpoints, but its
batch size and long timeout are benchmark controls; it is not a measurement of ordinary
client sync throughput. The subsequent `run` still requires a complete real-client sync to
create the checkpoint, and refuses to emit quality metrics if any workspace cannot sync.

Start or restart OCE separately with the reranking configuration being evaluated. Then
export the matching client/admin keys and non-secret model settings before running the
benchmark:

```bash
export OCE_API_KEY=...
export OCE_ADMIN_API_KEY=...
uv run python benchmarks/swe_explore.py --profile development run \
  --label dedicated-rerank \
  --metadata deployment=personal \
  --output /tmp/oce-dedicated-rerank.json
```

`OCE_ADMIN_API_KEY` is optional. When present, the harness snapshots `/admin/stats` after
all syncing and reports only the retrieval-phase model-call/token delta. It also reads the
server-owned retrieval switches from `/admin/index-stats`, so feature provenance does not
depend only on caller metadata. The report records the harness commit and dirty-worktree
state plus a safe allowlist of model settings. Automatic metadata capture excludes API keys
and endpoint URLs; values passed explicitly through `--metadata` remain the caller's
responsibility. Result JSON contains ranked paths and line spans, not retrieved source text.

Compare paired runs:

```bash
uv run python benchmarks/swe_explore.py compare \
  /tmp/oce-no-rerank.json \
  /tmp/oce-dedicated-rerank.json \
  /tmp/oce-chat-rerank.json
```

The report keeps two notions of relevance separate:

- `edit_*` measures whether retrieval reaches files and changed base-tree lines derived
  from the SWE-bench gold patch. These Top-1, file Recall@10, region Recall@10, and MRR
  fields are OCE diagnostics because a gold edit is objective but incomplete context.
- `swe_explore_*` comes directly from the checksum-pinned official reference evaluator. It
  includes line precision/recall/F1, file and region hit/noise rates, weighted core coverage,
  context efficiency, Recall/nDCG at 100/300/500 lines, and first useful hit.

The report also retains `core_*` Top-1/MRR diagnostics for quick debugging, alongside
latency, returned characters, errors, and optional model usage. Trajectory context is useful
empirical evidence but is model-dependent and sometimes uses coarse full-file regions.
Interpret the official and edit axes together, repeat promising findings on `verified`, and
keep downstream agent task success as a separate evaluation. The harness does not label a
strategy GO/NO-GO or protect reports against modification; provenance is supplied by fixed
source revisions, case IDs, runtime metadata, and ordinary version control.

The first five-variant development observation and the resulting deployment recommendation
are retained in
[`results/swe-explore-development-2026-09-02.md`](results/swe-explore-development-2026-09-02.md).

## Milvus workspace scope

Measure the production dense and path search calls with 2,000, 10,000, and 50,000 SHA256
blob names in the workspace filter:

```bash
uv run python benchmarks/milvus_scope.py
```

The command builds a temporary Milvus Lite database, prints progress to stderr, and emits
one JSON result to stdout. Use `--iterations` and `--warmups` to trade runtime for a larger
sample. No database or report is retained unless the caller redirects the JSON explicitly.

A raw diagnostic run from the current development environment is retained in
[`results/milvus-lite-scope-2026-09-01.json`](results/milvus-lite-scope-2026-09-01.json).
It is a host-specific observation, not a release threshold: all samples stayed in scope and
returned the target, while the 50,000-member filter reached 3.4 MB and roughly 280 ms p95.

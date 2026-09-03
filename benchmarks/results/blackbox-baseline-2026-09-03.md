# Black-box suite baseline — 2026-09-03

First runs of the three `benchmarks/blackbox` suites against the production server on the
shared personal-mode index, all model rerankers disabled. This is a development observation
that establishes the baseline for the new suites and records what their first use surfaced.

## System under test

- OCE `0.3.0`, `9478df4` plus the uncommitted change set under *Unreleased* in the changelog;
  `oce-client 0.2.0`.
- Index: the 13 `development` issue snapshots plus the curated corpus (Redux Toolkit v2.2.7,
  axum v0.7.9) prewarmed through `benchmarks.blackbox.prewarm`: 8,239 blobs, 19,274 chunks,
  24,413 vectors, 107,461 symbol occurrences.
- Qwen3-Embedding-4B, 1,024 dimensions; query-vector cache disabled; `RERANK_ENABLED=false`,
  `LLM_RERANK_ENABLED=false`.

## What the first run surfaced

**Milvus Lite growing segments.** Right after the prewarm uploaded 1,451 blobs, every scoped
dense and path search took 25× longer (workspace search about 30 ms → 800 ms; a compound issue
query 1.0 s → 4.6 s). Reproduced offline: on the same collection a 3,309-blob scoped search
took 780 ms before `flush` and 50 ms after. Fresh rows sit in an unindexed growing segment that
Lite scans row by row. The adapter now flushes after every upsert and delete on Milvus Lite.
Any personal-mode client sync that added rows could have triggered this slowdown.

**SQLite lock contention.** During batch uploads the metrics sink logged
`database is locked` on every flush; the personal-mode database used the default rollback
journal. It now opens in WAL mode with a 5 s busy timeout.

**Language-specific misses.** The TypeScript and Rust anchors exposed two gaps the Python
sets could not: `axum/src/routing/mod.rs` lost the path and symbol head to
`axum-extra/src/routing/mod.rs` because exact path lookup scored both two-segment suffix
matches equally, and Redux Toolkit reference queries led with `*.test-d.ts`, `__tests__`, and
`__testfixtures__` files the source prior did not recognise as tests. Both are fixed (`v7`).

## Results

Short structural queries (132; 22 anchors, 7 snapshots):

| Variant | Top-1 | MRR | Symbol Top-1 | Path Top-1 | Reference Top-1 | Ref def-first | Python Top-1 | TS Top-1 | Rust Top-1 | p50 ms | p95 ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `v5`, unflushed Milvus | 91.7% | 0.943 | 95.5% | 95.5% | 84.1% | 0.0% | 96.9% | 77.8% | 77.8% | 1,199 | 1,620 |
| `v6`, flush + WAL | 91.7% | 0.943 | 95.5% | 95.5% | 84.1% | 0.0% | 96.9% | 77.8% | 77.8% | 380 | 730 |
| `v7`, path tail + test conventions | 94.7% | 0.959 | 95.5% | 100% | 88.6% | 0.0% | 96.9% | 88.9% | 88.9% | 380 | 817 |

Reviewed semantic queries (21; feature / overview / call-chain × 7 snapshots):

| Variant | Primary Top-1 | MRR | nDCG@10 | Weighted R@5 | Weighted R@10 | Feature | Overview | Call-chain | Python | TS | Rust | Chars | p50 ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `v6` | 81.0% | 0.923 | 72.5% | 58.8% | 71.1% | 84.8% | 57.5% | 75.2% | 73.7% | 76.6% | 62.5% | 29,045 | 522 |
| `v7` | 81.0% | 0.923 | 72.9% | 58.8% | 72.3% | 86.1% | 57.5% | 75.2% | 73.7% | 79.5% | 62.5% | 29,228 | 485 |

SWE-Explore `development` (13 issues):

| Variant | Edit Top-1 | Core Top-1 | Edit file R@10 | Core file R@10 | nDCG@100 | nDCG@500 | First useful hit | Ctx efficiency | p50 ms | p95 ms | Chars |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `v5`, unflushed Milvus | 38.5% | 61.5% | 69.2% | 61.8% | 60.7% | 70.6% | 80.0% | 25.7% | 4,648 | 7,844 | 23,371 |
| `v6` | 38.5% | 61.5% | 69.2% | 61.8% | 60.7% | 70.6% | 80.0% | 25.7% | 1,427 | 3,061 | 23,371 |
| `v7` | 38.5% | 61.5% | 69.2% | 64.4% | 60.7% | 68.7% | 80.0% | 26.2% | 1,409 | 2,941 | 23,608 |

The issue rows cannot be passed to the current `compare` command together with the earlier
`swe_explore.py` results in
[`swe-explore-development-2026-09-03.md`](swe-explore-development-2026-09-03.md): those raw
results predate the current suite field, truth digest, and ordered-case identity contract.
Their aggregate numbers remain useful as narrative context, not as a paired machine
comparison. Issue p50 is about 0.4 s above the pre-prewarm runs; the flushed segments have
not been compacted, which the offline check showed removes the remainder.

## Reading

- Overview intent is the weakest semantic class (57.5% nDCG@10): `pytest-startup-collection`
  leads with a how-to document and `axum-handler` with `lib.rs`; both are cases where the
  primary owners are spread over several modules. Rust trails Python and TypeScript.
- Reference truth is file-level and lexical; the remaining reference misses are either a
  legitimate use in a file the truth excludes, or scripts that mention a common class name.
- The suites are sufficient as a paired regression gate for ranking changes. They are not a
  release claim: 7 snapshots, 21 reviewed semantic cases, 13 issues, one embedding model.

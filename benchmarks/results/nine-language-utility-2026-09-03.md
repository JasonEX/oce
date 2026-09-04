# Nine-language utility round — 2026-09-03

Development observation on the black-box suites after extending the curated corpus to nine
languages. It records what the wider corpus exposed, which changes were kept, and the
paired numbers for the no-reranker default, the in-process ONNX reranker, and the API
reranker. It is not a release gate.

## System under test

- OCE `0.3.0` development tree based on `3f1cccc`, with this round's change set
  documented under *Unreleased* in the changelog; `oce-client 0.2.0`.
- Fresh index (`SYMBOL_EXTRACTION_VERSION` 3): 13 `development` issue snapshots plus the
  13-snapshot curated corpus, 9,676 blobs, 28,770 vectors, about 234K symbol occurrences of
  which 111K are call sites.
- Qwen3-Embedding-4B, 1,024 dimensions; query-vector cache disabled; chat LLM disabled.
- Rerankers: none; `RERANK_PROVIDER=local` with the
  [`jinaai/jina-reranker-v2-base-multilingual`](https://huggingface.co/jinaai/jina-reranker-v2-base-multilingual)
  int8 ONNX export (16 candidates, 800 characters per candidate, 8 threads, CPU only;
  the evaluation model is CC-BY-NC-4.0 and is not a general commercial deployment recommendation);
  `RERANK_PROVIDER=api` with Qwen3-Reranker-0.6B (50 candidates).

## What the nine-language corpus exposed

| Finding | Evidence | Change |
| --- | --- | --- |
| C# and Bash had no tree-sitter symbols | C# grammar download failed once and stayed disabled for the process; Bash names functions with a `word` node the declaration reader ignored | retry grammar loads up to three times; accept `word` leaves |
| JavaScript prototype/CommonJS APIs were invisible | `app.use = function use()`, `exports.query = function()` are `assignment_expression`, not declarations | assigned function names become definitions; `module.exports = x` is skipped |
| Trace/flow questions were routed as symbol or path | `Trace gin's JSON binding from Context.ShouldBindJSON …` matched the filename pattern and had no call verb | dotted qualified names are symbols, recognised engineering extensions identify files, `trace` is a call verb, call verbs route without a symbol anchor, and explicit architecture/lifecycle cues precede call/path cues (240/240 routing intents and 39/39 semantic intents now agree) |
| Framework vocabulary neutralised the source prior | "How does bats run a single test function …" put `test/bats.bats` first | only questions that ask *for* tests get a neutral prior |
| Benchmarks and editor glue outranked implementations | `asv_bench/benchmarks/*.py` led xarray reference queries; `elisp/pylint.el` led a pylint issue | benchmark directories count as tests; files in languages the index does not parse get 0.85 |
| Path tails tied | `axum-extra/src/routing/mod.rs` tied `axum/src/routing/mod.rs` | whole-path tail outranks a shared suffix |
| Synchronous uploads embedded 64 chunks at a time, serially | 14 chunks/s; the provider's four concurrent batches were never used | pages of 256 chunks, about 3.5× faster resync |
| Milvus Lite growing segments survive restarts | scoped dense search 30 ms → 800 ms after any upload, also right after a restart | flush before the first search after a write or after start |

## Ablations on the new index (no reranker)

| Variant | Short Top-1 | Ref Top-1 | Sem nDCG@10 | Edit file R@10 | Issue nDCG@100 | Issue nDCG@500 | First hit | Issue p95 ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| old index, `v7` (start of day) | 91.7% | 82.5% | 63.3% | 69.2% | 60.7% | 68.7% | 80.0% | 2,941 |
| new index, call hop on, no query cap | 92.9% | 86.2% | 73.5% | 80.8% | 48.5% | 64.3% | 80.8% | 3,108 |
| + `EMBED_MAX_QUERY_CHARS=3000` | 92.9% | 86.2% | 73.5% | 80.8% | 54.5% | 71.5% | 87.7% | 2,143 |
| + call hop off | 95.4% | 93.8% | 73.3% | 80.8% | 59.3% | 78.0% | 93.8% | 2,024 |
| + benchmark/unparsed-language prior (`v10`) | 95.4% | 93.8% | 74.0% | 80.8% | 67.0% | 80.7% | 94.6% | 2,029 |
| + final intent/source-head review (`v12`, default) | 95.4% | 93.8% | 74.2% | 80.8% | 67.0% | 80.7% | 94.6% | 2,029 |

The call hop raised nothing that the suites measure and cost head order, so the experimental
runtime path was removed rather than retained behind a disabled setting. The query cap is the single largest issue-level gain and also
cuts p95 by almost a second; it loses one long xarray issue whose relevant detail sits after
3,000 characters.

## Rerankers on the new default

| Variant | Short Top-1 | Ref Top-1 | Short p95 | Sem nDCG@10 | Sem p50 | Edit Top-1 | Core Top-1 | Edit file R@10 | Issue nDCG@100 | Issue nDCG@500 | First hit | Issue p50 | Issue p95 | Egress |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| none | 95.4% | 93.8% | 496 | 74.2% | 529–549 | 30.8% | 76.9% | 80.8% | 67.0% | 80.7% | 94.6% | 1,192 | 2,029 | embeddings only |
| local ONNX | 95.4% | 93.8% | 1,480 | 73.7% | 1,654 | 61.5% | 84.6% | 84.6% | 83.8% | 88.9% | 94.6% | 2,764 | 3,583 | embeddings only |
| local ONNX + call hop | – | – | – | 73.0% | 1,561 | 61.5% | 84.6% | 76.9% | 79.5% | 84.5% | 88.5% | 2,897 | 3,850 | embeddings only |
| API Qwen3-Reranker-0.6B | 94.2% | 90.0% | 1,331 | 69.7% | 1,483 | 53.8% | 92.3% | 84.6% | 82.6% | 87.5% | 99.2% | 2,987 | 3,947 | query + candidates |

In this single development run, the in-process reranker materially improved issue ranking over
no reranker (Edit Top-1 30.8% → 61.5%, nDCG@500 80.7% → 88.9%), preserved short-query
Top-1, and sent no rerank input outside the machine. It did not improve the smaller semantic
suite (74.2% → 73.7%), so the evidence supports a complex-issue opt-in rather than a universal
quality claim. It also outperformed the API reranker on the issue and aggregate semantic
metrics measured here. Per-language semantic nDCG@10 for the local variant: Python 76.0,
TS 74.4, JS 85.7, Rust 67.0, Go 70.3, C 70.8, C# 65.7, Java 88.1, Bash 55.5.

## Language view of the default (no reranker), start of day → now

| Language | Short Top-1 | Semantic nDCG@10 |
| --- | ---: | ---: |
| Python | 96.9% → 97.9% | 73.3% → 77.3% |
| TypeScript | 88.9% → 88.9% | 80.2% → 79.7% |
| JavaScript | 61.1% → 72.2% | 85.3% → 85.3% |
| Rust | 88.9% → 88.9% | 62.5% → 62.6% |
| Go | 100% → 100% | 39.9% → 66.1% |
| C | 100% → 100% | 71.9% → 81.4% |
| C# | 100% → 100% | 22.1% → 57.9% |
| Java | 100% → 100% | 55.3% → 76.7% |
| Bash | 66.7% → 100% | 39.5% → 68.9% |

## Decisions

- Default: no model reranking, `EMBED_MAX_QUERY_CHARS=3000`, call hop off, the extended
  prior. Every suite is up against the start of the day and latency is down.
- Promising complex-issue opt-in for further validation: `RERANK_ENABLED=true RERANK_PROVIDER=local`.
  It needs the
  `local-rerank` extra and a model directory, adds about 1.6 s median on issue-length
  requests and about 0.9 s p95 on short ones, and keeps all data on the machine. Model
  licensing remains an independent deployment constraint.
- Not retained: the call hop in any configuration measured here. The API reranker remains an
  alternative only when external processing is acceptable and the local runtime/model is not.

## Limits

Thirteen issues, 39 reviewed semantic cases, 40 anchors, one embedding model, one CPU. The
final no-reranker semantic run was repeated with identical quality; provider comparisons and
issue results still have one run per variant, so their deltas are development signals rather
than variance-qualified release claims. The local reranker latency was measured on a 16-core
laptop; slower hosts should lower `RERANK_LOCAL_CANDIDATES`. Raw JSON is under
`~/.cache/oce/bench-runs`.

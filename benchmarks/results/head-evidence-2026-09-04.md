# Head evidence round — 2026-09-04

Development observation after the nine-language round. The failing cases of the
2026-09-03 runs were read one by one; this round fixes the index facts they exposed,
adds two evidence rules for the head slots, and reports each change as its own ablation.
It is not a release gate.

## System under test

- OCE `0.3.0` development tree on `05b8615` plus this round's uncommitted change set;
  `oce-client 0.2.0`; Qwen3-Embedding-4B (1,024 d); query-vector cache off; chat LLM off.
- Three fresh indexes of the same 13 + 13 snapshots (9,676 blobs):
  `v8` (`SYMBOL_EXTRACTION_VERSION` 3, the 2026-09-03 baseline), `v9` (CommonJS
  `require` aliases, Rust trait impls and `let` bindings are no longer definitions),
  `v10` (`v9` plus TS/JS re-exports are not definitions and prose files record no symbols).
- Local reranker: `jinaai/jina-reranker-v2-base-multilingual` int8 ONNX, 16 candidates.
  `BAAI/bge-reranker-v2-m3` int8 was also measured on `v8` and rejected (below).

## What the failing cases were

| Suite | Failures on `v8` | Cause found in the index or pipeline |
| --- | --- | --- |
| short (11/240) | `Route`, `Layer`, `compileETag`, `Router`, `Flask`, `configureStore` anchors | `var Route = require('./route')` recorded as a definition of `Route` (3 files tie); `impl<S> RouterExt<S> for Router<S>` and `let _: Router = …` recorded as definitions of `Router`; `export { configureStore } from` and fenced `configureStore(...)` in `.md/.mdx` recorded as definitions (5 declaring files, exact score damped 2.6×); `Flask` use sites exist only in tests/examples, and forty test chunks filled the exact window before the imports in `src/flask/*.py` |
| semantic (10/39) | 5 overview, 4 feature, 1 call-chain with Top-1 = 0 | import-only file headers (`run.py:1-31`, `method_routing.rs:1-24`, `ServiceCollectionExtensions.cs:1-17`) in the source head; single-sentence multi-facet overviews |
| issues (3/13) | requests-1724, requests-2931, xarray-6992 | vendored `urllib3` frames; `auth.py:1-59` header leading because it imports `to_native_string`; no structural head for identifiers the issue names |

## Index fixes (`v8` → `v10`, query-time rules off)

| Suite | Metric | `v8` | `v9` | `v10` |
| --- | --- | ---: | ---: | ---: |
| short 240 | Top-1 | 95.4% | 98.3% | **100.0%** |
| short | Reference Top-1 | 93.8% | 95.0% | **100.0%** |
| short | Symbol Top-1 | 92.5% | 100.0% | 100.0% |
| semantic 39 | Primary Top-1 | 74.4% | 74.4% | 74.4% |
| semantic | MRR | 0.914 | 0.905 | **0.922** |
| semantic | nDCG@10 | 74.2% | 73.3% | **74.6%** |
| issues 13 | Edit / Core Top-1 | 30.8% / 76.9% | 30.8% / 76.9% | 30.8% / 76.9% |
| issues | nDCG@500 | 80.7% | 76.1% | 77.5% |
| issues | first useful hit | 94.6% | 88.5% | 87.7% |

`v9` also carries the exact-lane change (use-site evidence diversified per file); on `v9`
alone it flipped both `Flask` reference anchors (`README.rst` first → `src/flask/blueprints.py`).
The remaining two anchors (`configureStore`) needed the re-export and prose fixes in `v10`.

The issue suite moved on three of thirteen issues between index builds: pytest-10356 rose
(nDCG@500 0.57 → 1.00), requests-5414 and pytest-10081 fell (two core files swapped
ranks; `unittest.py:301` left the top ten). The identifiers those two issues name have no
exact definitions in either index, so the swap is dense/ANN variance of a rebuilt index, not
a symbol change. Identical-configuration reruns of this suite move about one issue.

## Query-time rules (ablations)

Measured on `v9` (`v15-*`, header rule and reference fallback) and `v10` (`v16-*`, all
defaults); compound anchors on `v9` (`v14-*`).

| Rule | Short Top-1 | Sem nDCG@10 | Issue nDCG@500 | Cases moved | Decision |
| --- | ---: | ---: | ---: | --- | --- |
| import-only headers yield source head slots | 0 | −0.1 (2 down, 1 up) | +0.2 / +0.5 | `serve.rs:1-68` and `indexing.py:1` (grade-3 files) replaced by body chunks of grade-2 files; `configureStore.mdx` replaced by `devtoolsExtension.ts` | **off by default** (`RETRIEVAL_HEAD_SKIPS_IMPORT_HEADERS`) |
| reference head falls back to evidenced test/example use sites by prior | 0 after the index fixes | 0 | 0 | would have fixed the `README.rst`-first `Flask` case on `v8`; unit-tested | **on** (`RETRIEVAL_REFERENCE_HEAD_FALLBACK`) |
| compound anchors: protected slots for definitions the issue names | – | – | −1.0 (76.1 → 75.1) | xarray-7233 anchored `assign_coords`/`to_dataset` from the MVCE, not the fix | **off by default** (`RETRIEVAL_COMPOUND_ANCHOR_SLOTS=0`) |

## Rerankers on `v10` defaults

| Variant | Short Top-1 | Short p95 | Sem Top-1 | Sem nDCG@10 | Sem p50 | Edit Top-1 | Core Top-1 | Issue nDCG@100 | Issue nDCG@500 | Issue p50 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| none | 100.0% | 573 | 74.4% | 74.5% | 540 | 30.8% | 76.9% | 61.3% | 78.0% | 1,192 |
| local jina v2 | 100.0% | 1,638 | 69.2% | 73.0% | 1,717 | 61.5% | 76.9% | 75.8% | 81.9% | 3,062 |

Latency columns of the `v15` runs are omitted: a second index was being built on the same
host while they ran.

### bge-reranker-v2-m3 (on `v8`, same adapter, 16 candidates)

| Reranker | Short Top-1 | Short p95 | Sem Top-1 | Sem nDCG@10 | Edit Top-1 | Core Top-1 | Issue nDCG@500 | Issue p50 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| jina v2 int8 | 95.4% | 1,487 | 74.4% | 73.7% | 61.5% | 76.9% | 86.9% | 2,807 |
| bge-v2-m3 int8 | 95.0% | 3,143 | 56.4% | 65.3% | 23.1% | 46.2% | 63.3% | 5,022 |

A general multilingual text reranker with a higher BEIR score is worse than the smaller
model on every code suite and twice as slow. Not pursued.

## Decisions

- Keep the symbol extraction fixes; they correct index facts (a `require` alias, a trait
  impl, a re-export and a fenced example are not declarations) and are the source of the
  short-suite gain. Indexes built with version 3 fail closed and must be rebuilt.
- Keep per-file diversification of use-site evidence in the exact lane.
- Keep the reference head fallback on; leave the header rule and compound anchors as
  ablation switches, off.
- The 13-issue suite cannot separate a one-point change from rebuild variance; issue-level
  claims need the larger `standard` profile or repeated builds.

## Limits

Same corpora as the nine-language round; every failing case above was inspected before the
rules were written, so this round is development observation, not a held-out validation. A
held-out set of repositories and queries is the next step before any of these rules is
called a general improvement.

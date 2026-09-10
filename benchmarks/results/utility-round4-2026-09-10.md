# Round 4 correction and utility review — 2026-09-10

This report supersedes the rejected round-4 candidate. The final change retains
qualified-endpoint lookup, bounded startup probes, and support for applying an
explicit local index-type change. It does **not** change the default index or
add a semantic file head lane. The results below compare the final working tree
with commit `2238f0b1808de778762f3c813fa3848fade98832`.

The review accepts these bounded correctness fixes: the endpoint regression
fails on the old source and passes on the candidate, and the nine paired suites
show no utility regression. The failed default FLAT and semantic head changes
are excluded. This is not a claim of broad retrieval or latency improvement.

## Final implementation

- A qualified call-chain endpoint is looked up using its recorded `enclosing`
  before counting same-named definitions. Workspace scope and ambiguity checks
  still apply; a name cannot be rescued by declarations outside the request scope.
- Startup obtains at most 256 ready blob names through the repository contract.
  Dense, path and lexical probes finish before the lifespan yields. Sampling and
  each probe have a 10-second bound; errors are logged and cancellation propagates.
  A failed probe can leave a store cold, so this is not a guarantee that every
  storage failure blocks application startup. No embedding request is made.
- `MILVUS_DENSE_INDEX_TYPE` remains the only index-type setting, defaulting to
  HNSW. An explicit local type change rebuilds the index while retaining vectors;
  construction and search parameters match the chosen type. Missing index-type
  metadata or failed creation cannot mark the collection initialized. The change
  does not repair filtered HNSW recall or establish FLAT as a safe default.

The file-level vector lane, protected semantic file slots, first-declaration
substitution, macro-token call inference, underscore-based package identity,
first-chunk-without-symbols header inference, and universal locale/declaration
file penalties were removed. The auxiliary Markdown query-planner experiment
was also removed. No index-profile version changes or re-embedding are needed.
No repository name, benchmark case, or truth label enters production routing.

## Paired evaluation controls

All product runs use the published `oce-client` 0.2.0 and the stable HTTP API.
The baseline is a pristine detached checkout. The candidate is the final source
tree, hashed before and after each run. Corpus/truth manifests are also hashed
and unchanged. The candidate production-source manifest SHA-256 is
`b35edd22573850d4107ef7e5b86e71d9ae7e767798f848730fa8f126f58f298c`.
Query-vector caching and both rerankers are disabled for the pair.

The main pair is `base-hnsw-final` versus `corrected-final`, sequentially using
the **same existing HNSW index**, source vectors, SQLite data and pinned corpus.
The index is not rebuilt between these two runs. HNSW settings are M=16,
efConstruction=256 and search ef=64 (with the existing top-k lower bound).
Earlier HNSW rebuilds produced small baseline differences, so their scores must
not be counted as final candidate gains. Archived round-3 scores also have a
different physical index/corpus state and are not the paired denominator.

The four required suites are complemented by CSN, adopted upstream cases and
previously used held-out cases. The latter have already influenced development
and are **not** untouched validation sets. All paired comparator identity checks
passed. Characters and p50 are reported separately, without a composite score.

## Final metric vector

| Suite | Cases | Metric | Baseline | Candidate | Delta (points) |
|---|---:|---|---:|---:|---:|
| Project relations | 35 | `primary_top1` | 88.571% | 88.571% | +0.000 |
| Project relations | 35 | `primary_hit_at_3` | 97.143% | 97.143% | +0.000 |
| Project relations | 35 | `relation_recall` | 94.333% | 94.333% | +0.000 |
| Project relations | 35 | `hop_recall` | 98.333% | 98.333% | +0.000 |
| Project relations | 35 | `test_recall` | 100.000% | 100.000% | +0.000 |
| Project relations | 35 | `distractor_head` | 0.000% | 0.000% | +0.000 |
| Short queries | 240 | `top1` | 100.000% | 100.000% | +0.000 |
| Short queries | 240 | `mrr` | 100.000% | 100.000% | +0.000 |
| Semantic | 39 | `top1_primary` | 74.359% | 76.923% | +2.564 |
| Semantic | 39 | `ndcg_at_10` | 74.656% | 76.008% | +1.352 |
| Semantic | 39 | `weighted_recall_at_10` | 78.341% | 79.110% | +0.769 |
| CodeSearchNet | 80 | `region_top1` | 63.750% | 63.750% | +0.000 |
| CodeSearchNet | 80 | `region_hit_at_10` | 83.750% | 83.750% | +0.000 |
| SWE development | 13 | `edit_top1` | 46.154% | 46.154% | +0.000 |
| SWE development | 13 | `core_top1` | 76.923% | 76.923% | +0.000 |
| SWE development | 13 | `swe_explore_ndcg_at_100` | 62.358% | 62.358% | +0.000 |
| SWE development | 13 | `core_file_recall_at_10` | 69.744% | 69.744% | +0.000 |
| Upstream relations | 6 | `primary_hit_at_3` | 83.333% | 83.333% | +0.000 |
| Upstream relations | 6 | `relation_recall` | 81.944% | 81.944% | +0.000 |
| Upstream relations | 6 | `distractor_head` | 0.000% | 0.000% | +0.000 |
| Upstream semantic | 6 | `top1_primary` | 33.333% | 33.333% | +0.000 |
| Upstream semantic | 6 | `ndcg_at_10` | 32.294% | 32.294% | +0.000 |
| Previously used held-out relations | 24 | `primary_hit_at_3` | 95.833% | 95.833% | +0.000 |
| Previously used held-out relations | 24 | `relation_recall` | 96.181% | 97.569% | +1.389 |
| Previously used held-out relations | 24 | `distractor_head` | 0.000% | 0.000% | +0.000 |
| Previously used held-out semantic | 18 | `top1_primary` | 66.667% | 66.667% | +0.000 |
| Previously used held-out semantic | 18 | `ndcg_at_10` | 74.632% | 74.632% | +0.000 |

## Output size and latency

| Suite | Mean characters, baseline → candidate | p50 ms, baseline → candidate |
|---|---:|---:|
| Project relations | 19080 → 19080 | 376 → 613 |
| Short queries | 11007 → 11007 | 279 → 346 |
| Semantic | 30074 → 29934 | 1467 → 2802 |
| CodeSearchNet | 24701 → 24701 | 1300 → 2128 |
| SWE development | 25627 → 25627 | 1797 → 2371 |
| Upstream relations | 26328 → 26328 | 1278 → 2480 |
| Upstream semantic | 28266 → 28266 | 1349 → 1573 |
| Previously used held-out relations | 17213 → 17326 | 464 → 423 |
| Previously used held-out semantic | 25623 → 25623 | 2348 → 2566 |

The runs are sequential on a shared host. Startup probes deliberately move
cold initialization before request acceptance; these tables do not establish a
latency improvement. Monitoring uses rolling counters and some raw external
model-call deltas are negative, so they are excluded from usage/cost conclusions.

### Reverse baseline check

The unchanged baseline was run again after the candidate, using the same index.
Paired identity checks passed for the three sequential measurements.

| Suite | Initial baseline p50 | Candidate p50 | Repeated baseline p50 |
|---|---:|---:|---:|
| short | 279 | 346 | 348 |
| semantic | 1467 | 2802 | 2311 |

The repeated short baseline has the same utility and character counts as both
earlier runs. Its p50 closely matches the candidate, so the initial latency
difference cannot establish a code regression or speedup. Semantic latency
remains variable. Both baseline semantic runs have identical utility; the
candidate alone improves `xarray-indexing-overview`. That question does not use
the changed endpoint lookup, and the candidate log also contains a lexical
timeout. The observed single-case gain is retained but is not attributed to the
endpoint fix or claimed as a demonstrated broad improvement.

## Per-case review

The following lists every change in the primary/head or main ranking metrics
used above. Full per-case results, including relation error classes, are retained
in the raw JSON; an empty list for a suite means these metrics were unchanged.

- **Project relations:** unchanged.
- **Short queries:** unchanged.
- **Semantic:** `xarray-indexing-overview` `top1_primary` 0.000000 → 1.000000; `xarray-indexing-overview` `ndcg_at_10` 0.457010 → 0.984276; `xarray-indexing-overview` `weighted_recall_at_10` 0.700000 → 1.000000
- **CodeSearchNet:** unchanged.
- **SWE development:** unchanged.
- **Upstream relations:** unchanged.
- **Upstream semantic:** unchanged.
- **Previously used held-out relations:** `fastify-send-to-onsend-chain` `relation_recall` 0.666667 → 1.000000
- **Previously used held-out semantic:** unchanged.

## Rejected defaults and failed attempts

The repaired-but-still-default-FLAT candidate (`corrected-1`) was evaluated
against `base-hnsw-2` on the same stored vectors. It was not accepted:

| Metric | HNSW baseline | Default FLAT + prose planner |
|---|---:|---:|
| Project primary Hit@3 | 97.14% | 97.14% |
| Project distractor in head | 0% | 2.86% |
| CSN region Top-1 | 62.5% | 67.5% |
| CSN region Hit@10 | 82.5% | 81.25% |
| SWE edit Top-1 | 38.46% | 30.77% |
| SWE core Top-1 | 84.62% | 69.23% |
| SWE nDCG@100 | 62.16% | 53.01% |

Two pylint issues lost their useful head result, while a pytest issue improved.
Neither the CSN gain nor the pytest gain compensates for those losses. The
project distractor was `gin-servehttp-handler-chain`: its existing three-symbol
question is routed as compound, and complete dense recall changed its head.
That classifier limitation is not addressed by adding a benchmark-specific rule.

Switching the same intermediate source back to HNSW recovered SWE nDCG@100 to
62.36%, but rebuilding an approximate index can change its baseline. That
`corrected-hnsw-ablation` run is diagnostic only; its next suite stopped because
an earlier diagnostic client left an untracked state directory in a snapshot.
The directory was moved outside the snapshot and the final full pair was restarted.
The auxiliary prose planner was withdrawn along with the default index change.

`base-hnsw-1` was invalidated because its manually reconstructed HNSW index used
efConstruction=200 instead of the default 256. An attempted data-directory copy
was rejected by the stored index-profile identity; the guard was not bypassed.
These attempts are retained as diagnostics and are not release evidence.

## Fresh source-reviewed smoke

A separate itsdangerous 2.2.0 snapshot is pinned to
`096c8d42545d3b68ea21a4f890fb2b2d8979c0bd`. Two semantic and two relation cases were
labeled from source and their manifests hashed before their first retrieval run.
They use a separate data directory and the same fixed index for baseline and
candidate. Labels were produced during this development review, not by an
independent blind annotator. Four cases cannot establish generalization.

- **Relations:** `primary_hit_at_3` 0.5000 → 0.5000, `relation_recall` 0.5000 → 0.5000, `distractor_head` 0.0000 → 0.0000.
- **Semantic:** `top1_primary` 1.0000 → 1.0000, `ndcg_at_10` 0.9588 → 0.9588.

The relation smoke remains weak in absolute terms: primary Top-1 is 0/2 and
primary Hit@3 is 1/2 for both versions. It supplies no evidence that these
changes solve broader relation retrieval.

## Qualified-endpoint regression and an invalid product probe

A frozen synthetic black-box probe used two small projects with 49 declarations
of each method name, including a renamed control. Both versions already returned
the complete entry and target code in the primary results. The probe incorrectly
required a separate Hop section (and used one-ended Hop numbering for a two-ended
path), while the formatter omits already-shown regions. Its two `chain_complete:
false` results are **invalid as a measure of product gain**, not two demonstrated
candidate regressions. The sources and results are preserved; no production rule
or probe query was changed to obtain a pass.

A separate implementation regression exercises the real tree-sitter projection,
SQLite symbol store and endpoint resolver together. With 42 declarations per
leaf, the unqualified 40-declaration cap rejects both names. The old resolver
returns no endpoints; the candidate resolves exactly `Gate.enter_request` and
`Sink.handle_request`. The same test fails against the pristine baseline and
passes against the final source. This establishes the qualified-lookup defect
and its repair, while remaining distinct from a black-box utility gain.

## Verification and limits

The correction passed all 98 unit-test files in separate processes (826 tests)
before the final scope reduction. The final affected Milvus tests were rerun,
and the restored planner plus classifier/retrieval regressions were checked
again. All 14 symbol-store tests, including the new SQL/pipeline regression,
passed on the final source. Six real Milvus integration tests passed. The new integration case builds
HNSW, explicitly switches to FLAT, verifies all 192 stored rows remain, and checks
the scoped top-20 against an independently calculated cosine-order oracle.
Warmup tests cover failed sampling/probes, timeouts, shutdown cancellation and
lifespan readiness ordering. SQL tests cover qualification before the count cap
and excluded workspace scope. No-symbol script/CSS/runtime content and quoted
macro tokens have regression guards against the rejected semantic assumptions.
Ruff, formatting, lock consistency and diff-whitespace checks passed.

These are retrieval utility and correctness results, not downstream agent task
success or evidence of superiority over ACE. Acceptance rests on the complete
metric vector and the verified correctness defects. The small observed gains
are not presented as a measured broad retrieval improvement.

Raw artifacts and provenance are under
`~/.cache/oce/bench-runs/round4-correction/`:

- `base-hnsw-final-*.json`, `corrected-final-*.json`, `final-comparison-*.md`;
- `base-postcheck-*.json`, `reverse-baseline-*.md`;
- `fresh-{base,candidate}-*.json`, `fresh-frozen-truth.json`;
- `qualification-frozen.json`, `qual-*-qualification*`,
  `qualification-baseline-regression.log` and `qualification-candidate-regression.log`;
- `*-provenance.json`, server/suite logs and `unit-files/summary.json`;
- rejected `corrected-1-*`, `corrected-hnsw-ablation-*`, `base-hnsw-1-INVALID.txt`;
- `original-candidate.tar.gz` and `original-candidate.diff` preserve the rejected tree.

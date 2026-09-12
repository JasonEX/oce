# Retrieval state-machine simplification

Status: implementation and validation complete. The candidate is smaller and
faster, but its retrieval-quality regressions do not support adoption as a
quality-preserving default release. Changes remain uncommitted.

## Change and decision boundary

The pipeline keeps its fixed route → plan → recall → fuse → rank → select →
expand flow. This change narrows what each stage is allowed to decide:

- Routing groups directly coordinated definition targets. Parameter types and
  other prose mentions remain evidence rather than gaining definition-target
  priority because of a particular English phrasing.
- Dense, exact, lexical, SQL path lookup and semantic path recall enter separate
  rank lists. RRF combines their positions; SQL match grades and vector scores
  are no longer compared or added directly. A file's path evidence supplies one
  representative chunk rather than a raw-score boost to every chunk.
- Protected heads require requested structural facts: definitions, explicit
  paths, recorded references/implementations, or call-chain/traceback anchors.
  Ordinary semantic queries no longer have forced source-head slots. Test-name
  distance, filename/package affinity, import-header exceptions and the
  unparsed-language multiplier are removed. Bounded source/test/doc/vendor and
  working-set priors remain ordinary ranking inputs.
- Expansion prepares bounded excerpts and their source dependencies once.
  One assembly decides which primary suffix can be replaced by relation
  evidence under the overall and per-section character caps. Inferred evidence
  cannot outlive the primary excerpt that justified it. Call-chain headers and
  handover windows use the same decision, including already displayed hops.
  There is no preview assembly followed by a second assembly or derived
  quarter-budget/minimum-budget rule.

The change preserves qualified scope isolation, call/import/inherit evidence,
SQL scope filters, traversal/fanout limits, provider authorization and the
embedding-task release rule. These encode correctness or resource bounds and
were not removed merely because a regression test exercises one example.

Four retrieval settings are retired: `RETRIEVAL_SOURCE_HEAD_SLOTS`,
`RETRIEVAL_HEAD_SKIPS_IMPORT_HEADERS`, `RETRIEVAL_REFERENCE_HEAD_FALLBACK`, and
`RETRIEVAL_PATH_BOOST_WEIGHT`. Existing HTTP fields and errors remain unchanged.
The context limit counts source content; formatter paths, scope labels and line
numbers add output characters. Benchmarks report full returned characters.
`RETRIEVAL_RELATION_RESERVE_CHARS` now caps admitted ordinary relation content;
zero disables those sections. Unused capacity stays available to primary
content. Call chains retain their own cap under the same overall content budget.

## Index behavior and compatibility

An ordinary directory ending in `-retrieval-eval` is now admitted like other
source directories. The previous suffix exclusion was active in indexing, so
source-admission version advances from 1 to 2. Existing version-1 data requires
a fresh data directory and full client resync; startup continues to reject an
incompatible profile. The packaged-server smoke explicitly synchronizes and
retrieves source under `audit-retrieval-eval/src/`.

The final paired baseline is commit `879bc2e5322edd2f3763c7d71deeee891550f5c7`
with `source_filter.py` and `index_profile.py` synchronized to admission v2.
It also shares a symbol-projection insertion fix discovered
during staging: a single large file exceeded SQLite's SQL parameter limit.
Unchanged occurrence rows now use dialect-managed parameter sets instead of one
unbounded multi-row INSERT. This changes neither extraction nor the index profile.
All retrieval code remains byte-identical to that commit. Both variants use the
same newly built index, as recorded in the
[shared-index control](simplification-index-control-2026-09-10.md).
This isolates retrieval changes; it does not measure the separate utility gain
from newly admitted files. The pristine commit's earlier 613-query run is
retained as historical evidence, not substituted for this control.

## Validation protocol

All product measurements use the released `oce-client` v0.2.0 and stable HTTP
API against personal mode (SQLite and Milvus Lite). The evaluator imports no
service implementation and reads no database.
Client bytes, model configuration, index fingerprint, source revisions, ordered
case IDs, evaluator files and production hashes are captured in artifacts.
Query-vector caching is disabled. Larger upload batches and embedding
concurrency apply only to staging; quality/latency measurements use normal
query settings after staging stops.

The 11 existing suites are development regression evidence. Their existing
questions, labels and snapshots have not been changed. Known project cases
remain the primary relation guard; public CSN and SWE development are separate
semantic/issue guards. Metrics are reported as a vector, with distractor heads,
character cost and latency visible. No aggregate success score or post-result
regression tolerance is introduced.

Before production edits, a new 20-case batch was selected by a fixed hash from
seven repositories absent from the inspected development corpora. It uses
native SWE-bench questions and pinned SWE-Explore labels. The
[frozen protocol](simplification-validation-protocol-2026-09-10.md) permits only
operational metadata during development, then aggregate-only inspection after
source freeze. This is a blinded check for this change, not an independent
human audit or proof against earlier model exposure. It covers Python issue
retrieval, not general reference/call-chain behavior across languages.

## Complexity change

| Measure | Baseline | Candidate | Change |
| --- | ---: | ---: | ---: |
| Production Python lines | 21,296 | 20,771 | -525 |
| `retrieval.py` lines | 2,039 | 1,816 | -223 |
| `ranking.py` lines | 530 | 281 | -249 |
| Retrieval settings | 62 | 58 | -4 |

Line counts describe the implementation surface, not a quality metric.

## Implementation checks

- Python 3.13 and Python 3.11: each passes 878 unit tests, with one skip, across
  100 separate test-file processes. The skip is a PostgreSQL model test because
  no PostgreSQL service is configured. The executed tests include real SQLite retrieval
  regression checks, negative evidence-priority cases, score-rescaling
  invariance, source-dependency and context-budget boundaries.
- Wheel and sdist contain the current Python source; unpacked imports succeed.
  OpenAPI SHA-256 matches the baseline. A packaged server passes real-client
  sync/retrieve smoke tests, including newly admitted source and Chinese
  coordinated definition targets.
- Existing corpus fixtures are retained. One old meta-test that required an
  unprioritized fusion failure is replaced by assertions about the fixture's
  adversarial facts; it no longer requires a particular implementation to fail.
  Method-only tests for the removed `occurrence_kinds` API are removed with it.

Raw artifacts are under
`~/.cache/oce/bench-runs/state-machine-simplification-2026-09-10/`.
The completed measurement tables and artifact identities follow below.

Staging retained one HTTP 500 failure caused by the single-file SQL parameter
overflow. The old insertion path fails a synthetic 400-symbol test with a
999-variable limit; the repaired path inserts all symbols and remains idempotent.
The failed upload happened before query evaluation. Staging resumes through the
normal missing-blob/upload API, and implementation/package checks pass again
for the repaired source. The failure is operational evidence, not a scored
retrieval error or a reason to remove any frozen case.

The first final-repeat attempt (`candidate-final-2`) also records one released-
client transport send failure in the 72-case query-variant suite (71 successes).
No corresponding server HTTP failure response was observed; the underlying
transport cause remains undetermined. That incomplete attempt is retained.
`candidate-final-3` repeats all 11 suites before the common baseline runs, keeping
the planned candidate → baseline order and avoiding a stitched error-free run.

LLM invocation completion must be distinguished from HTTP retrieval success.
The standalone LLM ablation reaches its 15-second stage limit in 46 of 76
invocations; the serial cascade does so in 41 of 76. These requests return the
preceding retrieval order, so their quality is a deployed fallback mixture.
An initial external log summarizer missed the separate `LLM rerank exceeded`
branch; the corrected records retain these deadline events. Token totals cover
reported usage, not necessarily cancelled upstream work. Embedding and dedicated
rerank providers also omit most token counts; zero does not mean free usage.

## Final paired development comparison

The complete final pair is `candidate-final-3` followed by `baseline-common-2`:
11 suites and 613 successful retrieval requests per version, with zero query
errors in these completed attempts. Both use the same complete index and query
configuration. The separate failed candidate attempt remains in the operational
record above. The first shared-index pair preceded completion of staging; changes
between that pair and this pair are not attributed to code alone.

| Suite | Metric | Baseline | Candidate | Delta |
| --- | --- | ---: | ---: | ---: |
| short (240) | Top-1 | 1.0000 | 0.8833 | -0.1167 |
| short (240) | MRR | 1.0000 | 0.9272 | -0.0728 |
| semantic (39) | nDCG@10 | 0.7297 | 0.7293 | -0.0004 |
| project (35) | primary Top-1 | 0.9429 | 0.8857 | -0.0571 |
| project (35) | primary Hit@3 | 0.9714 | 0.9714 | +0.0000 |
| project (35) | relation recall | 0.9529 | 0.9281 | -0.0248 |
| project (35) | test recall | 1.0000 | 1.0000 | +0.0000 |
| project (35) | distractor head | 0.0000 | 0.0286 | +0.0286 |
| csn (80) | region Top-1 | 0.6250 | 0.6000 | -0.0250 |
| csn (80) | region MRR | 0.7243 | 0.6980 | -0.0263 |
| swe (13) | core Top-1 | 0.8462 | 0.8462 | +0.0000 |
| swe (13) | edit Top-1 | 0.5385 | 0.5385 | +0.0000 |
| swe (13) | nDCG@100 | 0.7303 | 0.6818 | -0.0485 |
| upproject (6) | primary Top-1 | 0.8333 | 0.8333 | +0.0000 |
| upproject (6) | primary Hit@3 | 1.0000 | 1.0000 | +0.0000 |
| upproject (6) | relation recall | 0.7778 | 0.7778 | +0.0000 |
| upproject (6) | test recall | 1.0000 | 1.0000 | +0.0000 |
| upproject (6) | distractor head | 0.0000 | 0.1667 | +0.1667 |
| upsemantic (6) | nDCG@10 | 0.3772 | 0.3733 | -0.0039 |
| heldout (24) | primary Top-1 | 0.9167 | 0.7500 | -0.1667 |
| heldout (24) | primary Hit@3 | 0.9583 | 0.7917 | -0.1667 |
| heldout (24) | relation recall | 0.9549 | 0.9861 | +0.0312 |
| heldout (24) | test recall | 1.0000 | 1.0000 | +0.0000 |
| heldout (24) | distractor head | 0.0000 | 0.0000 | +0.0000 |
| heldsem (18) | nDCG@10 | 0.7229 | 0.6763 | -0.0465 |
| variants (72) | primary Top-1 | 1.0000 | 0.9167 | -0.0833 |
| variants (72) | primary Hit@3 | 1.0000 | 1.0000 | +0.0000 |
| variants (72) | relation recall | 1.0000 | 0.9806 | -0.0194 |
| variants (72) | test recall | 1.0000 | 1.0000 | +0.0000 |
| variants (72) | distractor head | 0.0000 | 0.0833 | +0.0833 |
| layout (80) | Top-1 | 1.0000 | 1.0000 | +0.0000 |
| layout (80) | Hit@3 | 1.0000 | 1.0000 | +0.0000 |

| Suite | Mean full characters, baseline → candidate | p50 ms, baseline → candidate |
| --- | ---: | ---: |
| short | 10962 → 10734 | 854 → 522 |
| semantic | 30507 → 30766 | 5007 → 2575 |
| project | 18983 → 19023 | 1080 → 927 |
| csn | 25526 → 25528 | 4498 → 2300 |
| swe | 28146 → 28746 | 4532 → 2178 |
| upproject | 25036 → 24734 | 4764 → 2186 |
| upsemantic | 28957 → 28200 | 4619 → 1856 |
| heldout | 17168 → 17307 | 1210 → 510 |
| heldsem | 24707 → 25179 | 5048 → 2469 |
| variants | 18654 → 17951 | 1009 → 899 |
| layout | 1132 → 1153 | 804 → 368 |

Semantic breakdown (nDCG@10 and mean full characters):

| Suite | Group (cases) | Baseline | Candidate | Delta | Full characters, baseline → candidate |
| --- | --- | ---: | ---: | ---: | ---: |
| semantic | kind: feature (13) | 0.7036 | 0.7121 | +0.0085 | 31180 → 30786 |
| semantic | kind: overview (13) | 0.6497 | 0.6188 | -0.0308 | 30205 → 30292 |
| semantic | kind: call_chain (13) | 0.8358 | 0.8569 | +0.0211 | 30138 → 31221 |
| semantic | code_language: python (15) | 0.7662 | 0.7801 | +0.0139 | 33960 → 34232 |
| semantic | code_language: typescript (3) | 0.7873 | 0.7726 | -0.0148 | 26909 → 28764 |
| semantic | code_language: javascript (3) | 0.9065 | 0.9065 | +0.0000 | 24787 → 24787 |
| semantic | code_language: rust (3) | 0.6095 | 0.6342 | +0.0248 | 26119 → 26244 |
| semantic | code_language: go (3) | 0.6445 | 0.6305 | -0.0139 | 22075 → 22025 |
| semantic | code_language: c (3) | 0.6286 | 0.5498 | -0.0788 | 34333 → 36266 |
| semantic | code_language: csharp (3) | 0.6207 | 0.6166 | -0.0041 | 19727 → 16415 |
| semantic | code_language: java (3) | 0.8052 | 0.8173 | +0.0121 | 33731 → 35184 |
| semantic | code_language: bash (3) | 0.6525 | 0.6525 | +0.0000 | 39115 → 39115 |
| heldsem | kind: feature (6) | 0.6227 | 0.5066 | -0.1161 | 23753 → 23522 |
| heldsem | kind: overview (4) | 0.6755 | 0.6747 | -0.0008 | 24252 → 24671 |
| heldsem | kind: call_chain (4) | 0.8374 | 0.8651 | +0.0277 | 28491 → 30398 |
| heldsem | kind: issue (4) | 0.8058 | 0.7438 | -0.0621 | 22808 → 22952 |
| heldsem | code_language: python (5) | 0.8603 | 0.7437 | -0.1166 | 25105 → 25114 |
| heldsem | code_language: javascript (5) | 0.4832 | 0.4307 | -0.0525 | 24754 → 25017 |
| heldsem | code_language: go (4) | 0.7710 | 0.7418 | -0.0292 | 21990 → 22722 |
| heldsem | code_language: java (4) | 0.8025 | 0.8337 | +0.0312 | 26867 → 27919 |
| upsemantic | kind: feature (4) | 0.5391 | 0.5333 | -0.0058 | 24895 → 24547 |
| upsemantic | kind: overview (2) | 0.0532 | 0.0532 | +0.0000 | 37081 → 35506 |
| upsemantic | code_language: typescript (6) | 0.3772 | 0.3733 | -0.0039 | 28957 → 28200 |


The candidate is smaller and faster in this pair, but is not a demonstrated
quality-preserving replacement. Short-query loss is entirely in references:
reference Top-1 is 1.00 → 0.65, while symbol and path Top-1 remain 1.00. The main
relation suite keeps primary Hit@3 at 34/35, but loses relation recall and gains
one head distractor. Test recall remains complete while four heldout test-mapping
cases lose Hit@3 (Click, Fastify, and two Chi cases). Thus coverage alone does not
establish correct head order.

Known final-case diagnostics retain both directions: the Flask WSGI chain and
two Jsoup chains close, while the Express delegation chain and Gson implementation
coverage regress. The Requests redirect/cookie test case also loses supporting
relation evidence. Redux use/test ordering changes, the Axum implementation case
adds a re-export head distractor, and the upstream first-run settings use case
adds a head distractor. These are recorded in the per-case comparison artifact;
no new rules or truth edits are derived from them. Query variants are correlated
wordings of existing cases, not independent replications.

All 11 observed p50 values decrease. Character cost moves in both directions and
is not reduced uniformly. This is a sequential, controlled-index observation;
it does not isolate every source of wall-time variation or establish a universal
latency guarantee. Quality, head distractors, characters and time remain separate
judgments rather than a combined score.

## Development diagnostics before the final comparison

The first shared-index pair completed all 613 requests without errors. Its
results do not support a no-regression claim. They will be retained separately
from the final repeat after all staging is complete.

The short-query loss is confined to reference questions: 26 cases lose Top-1.
Twelve first results are in directories excluded by this suite's reference
truth, and fourteen are in the symbol's declaration file, which this truth
also excludes in its entirety. Recorded calls in those files can be real
reference evidence. This explains the scoring boundary; it does not establish
that every first result is a good answer, and the reported loss is retained.
Symbol and explicit-path Top-1 remain 100% in this first pair.

The relation suite also exposes tradeoffs that cannot be dismissed by that
short-query boundary. The Express delegation question is classified as
`feature` in both versions; removing weak semantic heads exposes its missing
router hop. The new assembly closes a previously incomplete Flask chain, while
Gson implementation coverage loses two labeled regions. Runtime-test and type-
test order changes affect the Redux case. One Axum re-export distractor enters
the first three results despite the requested implementation staying first.
These observations concern known development cases and do not supply new
rules, weights, prompts or labels for the candidate.


The three heldout Hit@3 losses are test-mapping cases (Fastify's hook runner
and Chi's Mount/Walk). Their labeled test regions remain retrieved, but move
below the first three results after the test-name-distance rule is removed.
Thus full test recall and head accuracy diverge. The six corresponding Redux
query variants repeat the runtime-test/type-test ordering change; the Axum
variants repeat the same re-export distractor. These are correlated variants,
not six independent demonstrations of either success or failure.


## Optional-capability deletion trials

Six predeclared variants completed 936 retrieval requests without HTTP/client
errors. Each uses the same 76 known semantic/issue questions (39 main, 18
heldout, 6 upstream, 13 SWE development); path-vector removal and query rewrite
also use all 240 short questions. The reference is the completed
`candidate-final-3` default run. Provider availability remains separate from
per-query routing. No prompts, weights or deadlines were tuned to these results.

These are single-pass, development-set point estimates. The unchanged candidate's
successful SWE suite in the preceding incomplete attempt scores 0.6421 nDCG@100;
the complete retry scores 0.6818, with two cases changing. Source, runtime and
case IDs match. The cause of this repeat variation was not isolated. Small
semantic/issue differences must not be interpreted as stable causal gains.

| Variant | Suite | Metric | Default | Variant | p50 ms, default → variant | Full characters, default → variant |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| no-path-vector | semantic | ndcg_at_10 | 0.7293 | 0.7297 | 2575 → 2594 | 30766 → 30503 |
| no-path-vector | heldsem | ndcg_at_10 | 0.6763 | 0.6901 | 2469 → 2530 | 25179 → 25319 |
| no-path-vector | upsemantic | ndcg_at_10 | 0.3733 | 0.3733 | 1856 → 1918 | 28200 → 28200 |
| no-path-vector | swe | swe_explore_ndcg_at_100 | 0.6818 | 0.6049 | 2178 → 2195 | 28746 → 28399 |
| no-path-vector | short | top1 | 0.8833 | 0.8833 | 522 → 511 | 10734 → 10734 |
| no-source-prior | semantic | ndcg_at_10 | 0.7293 | 0.5767 | 2575 → 2587 | 30766 → 31733 |
| no-source-prior | heldsem | ndcg_at_10 | 0.6763 | 0.5437 | 2469 → 2496 | 25179 → 26052 |
| no-source-prior | upsemantic | ndcg_at_10 | 0.3733 | 0.3269 | 1856 → 1773 | 28200 → 26705 |
| no-source-prior | swe | swe_explore_ndcg_at_100 | 0.6818 | 0.4405 | 2178 → 2159 | 28746 → 29251 |
| rewrite | semantic | ndcg_at_10 | 0.7293 | 0.7198 | 2575 → 2622 | 30766 → 30808 |
| rewrite | heldsem | ndcg_at_10 | 0.6763 | 0.7223 | 2469 → 3354 | 25179 → 25653 |
| rewrite | upsemantic | ndcg_at_10 | 0.3733 | 0.2898 | 1856 → 1897 | 28200 → 28080 |
| rewrite | swe | swe_explore_ndcg_at_100 | 0.6818 | 0.4963 | 2178 → 4208 | 28746 → 29566 |
| rewrite | short | top1 | 0.8833 | 0.8792 | 522 → 1699 | 10734 → 10637 |
| dedicated | semantic | ndcg_at_10 | 0.7293 | 0.6919 | 2575 → 3614 | 30766 → 31601 |
| dedicated | heldsem | ndcg_at_10 | 0.6763 | 0.6467 | 2469 → 3293 | 25179 → 26625 |
| dedicated | upsemantic | ndcg_at_10 | 0.3733 | 0.6404 | 1856 → 2991 | 28200 → 27797 |
| dedicated | swe | swe_explore_ndcg_at_100 | 0.6818 | 0.4114 | 2178 → 3556 | 28746 → 30518 |
| llm | semantic | ndcg_at_10 | 0.7293 | 0.7795 | 2575 → 17327 | 30766 → 31231 |
| llm | heldsem | ndcg_at_10 | 0.6763 | 0.8043 | 2469 → 16837 | 25179 → 25256 |
| llm | upsemantic | ndcg_at_10 | 0.3733 | 0.4067 | 1856 → 16779 | 28200 → 27812 |
| llm | swe | swe_explore_ndcg_at_100 | 0.6818 | 0.7379 | 2178 → 16613 | 28746 → 30858 |
| cascade | semantic | ndcg_at_10 | 0.7293 | 0.7540 | 2575 → 17980 | 30766 → 31634 |
| cascade | heldsem | ndcg_at_10 | 0.6763 | 0.6979 | 2469 → 17586 | 25179 → 26848 |
| cascade | upsemantic | ndcg_at_10 | 0.3733 | 0.6355 | 1856 → 17540 | 28200 → 29453 |
| cascade | swe | swe_explore_ndcg_at_100 | 0.6818 | 0.6722 | 2178 → 17816 | 28746 → 30726 |


The LLM rows measure the deployed combination of completed reranks and deadline
fallbacks, not a fully completed model trial:

| Mode | LLM stage invocations | Completed usage records | 15-second deadline fallbacks | Reported LLM tokens |
| --- | ---: | ---: | ---: | ---: |
| LLM only | 76 | 30 | 46 | 585,984 |
| Dedicated then LLM | 76 | 35 | 41 | 679,640 |

Query rewrite records 278 model-usage events and 101,918 reported tokens across
its 316 retrieval requests, plus 1,176 embedding-call records (371 for the
same default query set). Dedicated reranking records 76 calls in each applicable
76-query run. Embedding and dedicated-rerank token counts are mostly unreported;
LLM totals exclude unknown usage from cancelled requests. These are observed
usage records rather than complete billed costs. No upstream 429 retries or
API-error fallbacks were recorded in these trials.

The resulting decisions introduce no further production or default changes:

- Retain the bounded source prior. Removing it substantially reduces all four
  semantic/issue suites. This supports an ordinary prior; it does not justify
  restoring the removed forced semantic-head rules.
- Retain path-vector recall as an independent lane. Its removal has mixed
  semantic effects and lowers SWE nDCG in comparison with both default
  observations, while exact short queries are unchanged. The gain size remains
  uncertain under the observed repeat variation.
- Retain optional query rewrite. It helps heldout semantics but hurts the other
  semantic/issue suites and short Top-1, with additional cost. Neither general
  removal nor default enablement follows from this vector.
- Retain the dedicated provider. Its large gain on the six upstream queries
  supplies a local use case, although the other suites regress. One project's
  six questions do not establish broad provider superiority.
- Retain optional LLM reranking. The deployed fallback mixture improves these
  four point estimates, but full model efficacy and total billed cost remain
  inconclusive because most invocations hit the stage deadline.
- Retain the existing independently authorised serial composition without
  promoting it. It trails LLM-only on main/heldout semantics and SWE, while
  leading it on upstream semantics; dedicated-only has the opposite tradeoff.
  Neither one provider uniformly dominates the vector, and the cascade has no
  established general incremental benefit. Removing this composition would
  require a new provider-precedence or mutual-exclusion rule. This evidence
  does not justify that change or a new query-specific routing heuristic.

## Frozen external validation

Both frozen implementations complete all 20 native issue queries without errors.
The aggregate exporter verifies source hashes, client bytes, evaluator and
manifest hashes, runtime/index fingerprints and collection entity counts.
Only this aggregate was opened; question text, ground-truth regions and individual
external results were not inspected. No production changes follow the freeze.

| Metric | Baseline | Candidate | Delta |
| --- | ---: | ---: | ---: |
| Core Top-1 | 0.7000 | 0.6000 | -0.1000 |
| Edit Top-1 | 0.6000 | 0.4500 | -0.1500 |
| Core MRR | 0.7542 | 0.6663 | -0.0879 |
| Edit MRR | 0.6517 | 0.5475 | -0.1042 |
| nDCG@100 | 0.6280 | 0.5095 | -0.1185 |
| nDCG@300 | 0.7107 | 0.6002 | -0.1104 |
| nDCG@500 | 0.7245 | 0.6072 | -0.1173 |
| Core file recall@10 | 0.5383 | 0.5150 | -0.0233 |
| Core region recall@10 | 0.4425 | 0.4317 | -0.0108 |
| Edit file recall@10 | 0.7500 | 0.8000 | +0.0500 |
| Edit region recall@10 | 0.3967 | 0.3967 | +0.0000 |
| Weighted core coverage | 0.0451 | 0.0536 | +0.0086 |
| Context efficiency | 0.1683 | 0.1718 | +0.0035 |
| Noise file rate (lower better) | 0.8424 | 0.8361 | -0.0063 |
| Noise region rate (lower better) | 0.8349 | 0.8313 | -0.0036 |
| Mean full characters | 29823 | 30637 | +814 |
| p50 milliseconds | 4767 | 2180 | -2587 |
| p95 milliseconds | 5307 | 2932 | -2375 |

Core Top-1 decreases from 14/20 to 12/20; edit Top-1 from 12/20 to 9/20.
nDCG falls at all three cutoffs, while some coverage/efficiency point estimates
improve. The candidate returns about 814 more characters per query on average.
Median elapsed time decreases by about 54%, but this does not resolve the head-
accuracy and ranking losses. Both runs record 80 embedding calls and no rewrite
or rerank calls; provider token usage is unreported.

This 20-case, seven-repository Python batch supplies a check outside the inspected
development task set. It does not provide statistical proof of general superiority,
multilingual reference coverage, or agent task success. The observed direction
does not support the claim that this bundled simplification improves generalisation.
It also cannot identify which individual removed rule or refactored stage caused
the loss, because the main candidate changes several stages together.

## Evidence identities

The candidate was frozen at `2026-09-11T01:52:49.170011+00:00`. Source-tree hashes
below are SHA-256 of the sorted path-to-file-hash map; evaluator and manifest-map
hashes use the same canonical JSON encoding.

| Evidence | SHA-256 |
| --- | --- |
| Candidate production source | `35d6c663a2fe690ed72696bd5bb71fcacfb7c6df245b04d817cf1181fb37f178` |
| Shared-control baseline source | `5aacacb02a23019d207a4edf31b877392ca1c4780868c87f881c936e4faf0ca7` |
| Frozen 20-case manifest | `1c6f75592e3a2a62f1a14c3724cf45582d81dd6f1f0dda936e89ee787c74e130` |
| Evaluator Python map | `84015ff1597c69ad5e28fd6d15e8e1ffb13666b0494a957e236342f0c232d87e` |
| All evaluator JSON manifests | `a5089695f9d110f26df6953d9f224f9953cf792406464da67fee0d391d88cec5` |
| Released client binary | `ae27c49198e946730f7eccd5596b814f412cd0aa89b823ebb928561872b085fc` |
| Candidate wheel | `f2031920f6e8aa9a1fd6488b4ca9d3f3f154b1a25347d9e8a47fcb5091748f76` |
| Candidate sdist | `8341bf2ef0f466357e2f9344b885351a58ac5a88f3f590f9a8d2f285a9ee8037` |
| blind-common-final raw report | `803aee003143873aa47cd8df90b6c9452c940f41fe9e093ddc51f7052a4f21c2` |
| blind-candidate-final raw report | `33c8c06d895d82deb7bfd934af298503453e43fb232bc0138c5d62f6b1ab5e3b` |

The index uses `Qwen3-Embedding-4B` at 1,024 dimensions, with 184,046 dense
entities and 37,968 path entities. Its fingerprint is
`ca1a70df0d12599cb305aede959e7bb99c46991fe0472faa7e71d50cb4862b1c`.
The configured optional models are `Qwen3-Reranker-0.6B` and
`deepseek-v4-flash`; their availability is confined to the corresponding ablations.
Unit checks ran on Python 3.13.5 and 3.11.14. OpenAPI SHA-256 remains
`25bfb1ebb58fbb6f15f98ec02e296a94b1f2d4c24d6cbc70940b8e341c3a7fad`.

The artifact directory contains `candidate-final-freeze.json`,
`final-identities.json`, `evidence-manifest.json`, the two unit proof files,
`package-projection-proof.json`, `package-projection-runtime-proof.json`,
`baseline-common-2-vs-candidate-final-3.json`, `ablation-summary.json`,
`capability-decisions.json` and `blind-aggregate-only.json`. Raw failed attempts
and successful reports remain separately identified.
Wheel/sdist identities refer to the package used for source-frozen runtime
validation. Final report and README editorial updates followed that freeze;
the packaged Python remains byte-identical to the final checkout.

## Engineering conclusion

The implementation removes 525 production Python lines and four retrieval settings,
unifies fusion and context admission, and fixes the large-file SQL insertion failure.
The checked candidate passes implementation/package checks and lowers observed
query latency. Its default retrieval quality is not preserved: the main relation
guard gains head distractors and loses relation recall, several development guards
regress, and the frozen external batch loses head accuracy and nDCG.

This candidate should not be adopted as a quality-preserving default release.
The short-query and heldout-semantic losses exceed the pre-existing documented
tolerances, and the main relation head-distractor rate rises. Implementation
checks do not override those utility limits.
It remains an uncommitted, reviewable implementation and a completed negative
utility experiment. The evidence does not justify deleting all ordinary priors
or optional model capabilities. Any further retrieval-policy iteration needs a
new validation boundary; this external batch is now exposed at aggregate level
and must not be presented as untouched validation for tuning derived from it.
No release, commit or push was performed.

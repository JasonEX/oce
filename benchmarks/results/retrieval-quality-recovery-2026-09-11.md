# Retrieval quality recovery

Status: **C12 implementation and assessment are complete. Quality acceptance
remains negative: development guards fail and the new validation batch declines.
Changes remain uncommitted.** This follow-up addresses the regressions measured in the
[state-machine simplification study](state-machine-simplification-2026-09-10.md).
The preceding study and its failed candidate remain historical evidence.

## C12 final assessment: quality acceptance remains negative

The general correctness and SQL repairs are implemented and tested, but **C12
is not accepted as a quality-preserving replacement for the controlled
baseline**. Development coverage gains do not erase failed head-order guards,
and the new validation batch also declines. The source remains at its pre-query
freeze; no validation questions, truth regions or individual results were
inspected to revise the code. No commit, tag or push has been made.

The two final runs each complete the new 18-case validation and the historical
20-case regression with zero HTTP errors, normal shutdown and matching frozen
source, client, evaluator, manifest, runtime and index proofs. The new batch
is case-disjoint from development but shares six repositories. It is an
operator-blinded public-data assessment, not an independent human audit or a
test of unseen repositories. The historical batch had already exposed aggregate
results and is not fresh validation.

| Metric | New 18: control | New 18: C12 | Historical 20: control | Historical 20: C12 |
|---|---:|---:|---:|---:|
| `core_top1` | 0.7222 | 0.6667 | 0.7000 | 0.7000 |
| `edit_top1` | 0.6111 | 0.5556 | 0.6000 | 0.5500 |
| `core_mrr` | 0.8333 | 0.7917 | 0.7542 | 0.7417 |
| `edit_mrr` | 0.7579 | 0.7222 | 0.6517 | 0.6071 |
| `core_file_recall_at_10` | 0.7157 | 0.7157 | 0.5483 | 0.5233 |
| `edit_file_recall_at_10` | 0.9815 | 0.9815 | 0.7500 | 0.7500 |
| `core_region_recall_at_10` | 0.5954 | 0.5815 | 0.4525 | 0.4400 |
| `edit_region_recall_at_10` | 0.4095 | 0.4095 | 0.3967 | 0.3717 |
| `swe_explore_ndcg_at_100` | 0.5700 | 0.4996 | 0.6691 | 0.6315 |
| `swe_explore_recall_at_100` | 0.0542 | 0.0424 | 0.0579 | 0.0650 |
| `mean_returned_chars` | 32119.5 | 32202.9 | 30922.5 | 31583.3 |
| `p50_elapsed_ms` | 2849 | 2395 | 2704 | 2410 |

On the new batch, core Top-1 decreases from **13/18 to 12/18**, edit Top-1
from **11/18 to 10/18**, and nDCG@100 from **0.5700 to 0.4996**. On the
historical batch, core Top-1 remains **14/20**; edit Top-1 decreases from
**12/20 to 11/20**, and nDCG@100 from **0.6691 to 0.6315**. The historical
batch has some recall/coverage gains, retained in the full aggregate artifact,
but the vectors do not support a broad quality-improvement claim.

C12 emits **three lexical-timeout fallback warnings** across its combined
validation/regression run; the control emits none. Both have zero observed
symbol timeout, lexical failure or connection-cleanup warnings and one dense
warm-up failure each. Because only aggregate validation outputs were inspected,
no timeout is assigned to an individual sealed case and no counterfactual effect
is inferred. These are material runtime limitations. Lower observed p50 values
include these fallbacks and are not evidence of a uniformly faster complete
retrieval path. The moving 24-hour usage deltas remain INVALID for token,
call-count and cost comparisons.

Implementation verification is complete on the frozen source: **941 passed,
one PostgreSQL-related skip per Python version (3.13 and 3.11)**, using 100
separate test-file processes per version. Ruff, format and lock checks pass.
Wheel/sdist source identity, unpacked import, unchanged OpenAPI, fresh packaged
server sync/retrieve checks and normal shutdown pass. Final source and test
hashes still match the unit proofs and pre-query freeze. Live PostgreSQL was
not qualified. The earlier admission-profile change remains version 1 → 2;
an old incompatible index requires a fresh data directory and full client resync.

The fixed state machine, rank-based fusion and single context assembly remain.
The measured fixes cover use-site membership, same-file consumer ownership,
qualified caller priority and scoped SQL execution; the complete ranking bundle
still needs further work before it can be treated as a quality-preserving
default. This assessment does not claim direct ACE superiority or downstream
agent task success.

Current durable evidence:

- [Source, unit, package and artifact proof](issue-quality-recovery-2026-09-11-data/c12-final-evidence.json)
- [Full development vector and strata](issue-quality-recovery-2026-09-11-data/cycle2-c12-development-comparison.json)
- [New 18-case validation aggregates](issue-quality-recovery-2026-09-11-data/cycle2-c12-validation-aggregate.json)
- [Historical 20-case regression aggregates](issue-quality-recovery-2026-09-11-data/cycle2-c12-regression-aggregate.json)
- [Development runtime audit](issue-quality-recovery-2026-09-11-data/cycle2-c12-development-lane-health.json) and [validation runtime audit](issue-quality-recovery-2026-09-11-data/cycle2-c12-validation-lane-health.json)
- [Pre-validation assessment decision](issue-quality-recovery-2026-09-11-data/cycle2-c12-assessment-decision.json) and [development control timeout audit](issue-quality-recovery-2026-09-11-data/c12-baseline-timeout-development-audit.json)

Full raw logs, source snapshots, packaged checks and digest proofs remain in
`~/.cache/oce/bench-runs/issue-quality-recovery-2026-09-11/`. Failed earlier
attempts are retained below and in their original artifacts.

## C12 completed development comparison

Both `cycle2-baseline-sql2-main` and `cycle2-c12-main` complete all **631
queries**, with zero HTTP errors and normal shutdown. The strict comparison
verifies exact source/client/evaluator/manifest identity and matching index
profile, dense/path counts and runtime configuration. Query caching and the
optional model stages are disabled. The control shares both SQL repairs.

| Suite | Metric | Shared-SQL control | C12 |
|---|---|---:|---:|
| short | `top1` | 1.0000 | 0.9375 |
| short | `mrr` | 1.0000 | 0.9688 |
| short | `path_recall_at_10` | 0.8936 | 0.8744 |
| semantic | `top1_primary` | 0.6923 | 0.6923 |
| semantic | `ndcg_at_10` | 0.7312 | 0.7371 |
| project | `primary_top1` | 0.9429 | 0.8857 |
| project | `primary_hit_at_3` | 0.9714 | 0.9714 |
| project | `relation_recall` | 0.9433 | 0.9586 |
| project | `test_recall` | 1.0000 | 1.0000 |
| project | `distractor_head` | 0.0000 | 0.0000 |
| csn | `region_top1` | 0.6375 | 0.6625 |
| csn | `region_mrr` | 0.7305 | 0.7504 |
| csn | `region_hit_at_10` | 0.8625 | 0.8750 |
| swe | `core_top1` | 0.7692 | 0.7692 |
| swe | `edit_top1` | 0.4615 | 0.4615 |
| swe | `swe_explore_ndcg_at_100` | 0.6534 | 0.6534 |
| upproject | `primary_hit_at_3` | 1.0000 | 1.0000 |
| upproject | `relation_recall` | 0.7778 | 0.7639 |
| upproject | `distractor_head` | 0.0000 | 0.0000 |
| upsemantic | `ndcg_at_10` | 0.3772 | 0.3733 |
| heldout | `primary_hit_at_3` | 0.9583 | 0.9583 |
| heldout | `relation_recall` | 0.9757 | 0.9861 |
| heldout | `test_recall` | 1.0000 | 1.0000 |
| heldout | `distractor_head` | 0.0000 | 0.0000 |
| heldsem | `ndcg_at_10` | 0.7216 | 0.7290 |
| variants | `primary_top1` | 1.0000 | 0.9167 |
| variants | `primary_hit_at_3` | 1.0000 | 1.0000 |
| variants | `relation_recall` | 1.0000 | 1.0000 |
| variants | `test_recall` | 1.0000 | 1.0000 |
| variants | `distractor_head` | 0.0000 | 0.0000 |
| layout | `top1` | 1.0000 | 1.0000 |
| issue-dev | `core_top1` | 0.7222 | 0.7778 |
| issue-dev | `edit_top1` | 0.5000 | 0.5556 |
| issue-dev | `swe_explore_ndcg_at_100` | 0.6405 | 0.6860 |
| issue-dev | `core_region_recall_at_10` | 0.5083 | 0.4898 |

| Suite | Control p50 (ms) | C12 p50 (ms) | Control chars | C12 chars |
|---|---:|---:|---:|---:|
| short | 83 | 83 | 10979.6 | 11054.4 |
| semantic | 627 | 652 | 30631.8 | 30690.5 |
| project | 516 | 515 | 19101.2 | 18273.3 |
| csn | 644 | 691 | 25488.9 | 25558.6 |
| swe | 1827 | 1894 | 28883.8 | 28896.2 |
| upproject | 735 | 685 | 24976.7 | 24448.5 |
| upsemantic | 726 | 927 | 29421.0 | 29172.7 |
| heldout | 525 | 583 | 17090.9 | 16922.0 |
| heldsem | 664 | 652 | 24747.1 | 25355.0 |
| variants | 494 | 493 | 18679.2 | 16624.5 |
| layout | 469 | 463 | 1131.5 | 1152.6 |
| issue-dev | 2909 | 2393 | 31947.2 | 32482.6 |

C12 improves main/heldout relation coverage, CSN and native issue-development
Top-1/nDCG. Main-project Hit@3 and test recall remain unchanged, with zero head
distractors across all four relation suites. The correlated variants recover
complete relation recall. The original SWE development profile retains its
core/edit Top-1 and nDCG. These are known development results.

**The complete utility guards still fail.** Short Top-1 loses 15/240 and
main-project Top-1 loses 2/35, both beyond the existing tolerances. Every
remaining losing head matches C10: the audited same-file reference/truth
mismatch, the Flask prose head, and the runtime/type-test ordering remain.
The Express qualified-caller error is fixed. The six wording losses are one
correlated test-mapping requirement group. No labels, tolerances, whole-file
self-reference penalties or filename/test-name-length rules were changed to
remove these losses.

Other negative dimensions remain visible: upstream relation recall, short path
recall and native diagnostic core-region recall decrease. Semantic nDCG drops
by 0.0018 for overview (13 cases), 0.0208 for C# (3 cases), and 0.0015 for Java
(3 cases). The upstream semantic feature stratum drops by 0.0058 (4 cases).
The complete strata are retained in `cycle2-c12-development-comparison.json`.
Returned characters and p50 are separate measurements; against the shared SQL
repair, latency is mixed rather than uniformly better. The moving 24-hour model
usage deltas remain INVALID for token, call-count or cost claims.

The complete runtime audit records one dense warm-up failure for each source.
C12 records zero symbol/lexical timeout or failure warnings and zero connection
cleanup warnings. The control records one exact-symbol timeout. Matching the
631 sequential HTTP completions to case order associates it with the existing
`pylint-dev__pylint-7080` development case; both versions retain the same first
answer and core/edit Top-1 and nDCG there. Its counterfactual effect on the full
response remains unknown. Neither this fallback nor the warm-up failures are
erased by successful HTTP responses. The source-bound health and timeout audit
are retained as `cycle2-c12-development-lane-health.json` and
`c12-baseline-timeout-development-audit.json`.

The explicit decision in `cycle2-c12-assessment-decision.json` freezes C12 for
**independent assessment, not default-release acceptance**. The source freeze
`cycle2-final-freeze.json` precedes any request on the 18 preselected validation
cases. These cases are disjoint from development but share six repositories.
The previously aggregate-exposed 20-case batch is a separate regression check.
Only aggregate validation results were opened after both sources completed;
source, questions and truth remain fixed. Results are reported above.

Relative to the original commit, current production Python decreases from
21,296 to 21,035 lines; `retrieval.py` from 2,039 to 1,928; `ranking.py` from
530 to 408. Retrieval settings decrease from 62 to 58. These describe the
maintenance surface, not a quality score. The detailed implementation and
failed earlier attempts below remain historical evidence.

## Decision boundary

The target is to recover useful retrieval while retaining the fixed state
machine, rank-based fusion, unified context assembly, source-admission fix and
bounded SQL insertion. Repository names, benchmark question strings and test
name length must not determine ranking. Questions, labels and evaluators stay
unchanged. Provider availability and model-routing defaults stay separate.

The controlled reference is commit `879bc2e5322edd2f3763c7d71deeee891550f5c7`
with the preceding study's common admission-v2 and SQL insertion fixes. The
unsuccessful simplified source is also retained as an intermediate comparison.
All variants use the same fully staged index and released `oce-client` v0.2.0
through stable HTTP APIs, with query-vector caching disabled.

The 11 existing suites remain development regression evidence. The preceding
20-case external batch has already exposed aggregate results; any repeat is a
regression check, not a new blind validation. No questions or individual results
from that batch are needed to design these repairs. Existing suite tolerances,
relation recall, distractor heads, returned characters and latency remain
separate acceptance dimensions.

## Diagnosed stage contracts

- Recall must carry retrieved call-site content into fusion. A use-site flag
  cannot preserve a call whose chunk is absent from the mixed exact window and
  semantic recall.
- Reference ordering must distinguish consumers from a declaration's own
  self-references. Test calls do not displace project source references unless
  tests are requested.
- Recall agreement ranks relevance within an evidence category; it does not
  make documentation or tests the preferred answer to an implementation
  request. Bounded source coverage can prepare the model window while allowing
  an enabled semantic reranker to own the final semantic order.
- Structural answers must survive selection and final context assembly, not
  merely an earlier ranking stage. File diversity applies after these bounded
  answers; the hard character limit still applies to all results.
- A request for implementations needs actual inherited/implemented occurrences
  in primary recall. If a subtype is named, another subtype's implementation
  cannot substitute for it merely by repeating the interface name.
- File evidence ranks files. Its vote must be identical for each already
  recalled chunk in that file; assigning the whole vote to one representative
  can displace the method the user needs.
- A type mentioned inside a behavior description is contextual evidence.
  Giving its declaration another RRF vote can move a class header ahead of
  the method performing the operation. SQL reference rows likewise establish
  membership, but their enumeration order does not resolve a contextual
  reference question.
- A leading named subject in a behavior description still supplies a useful
  definition vote. This does not change the semantic intent, coverage budget,
  dense participation or model ranking. Other mentioned operands receive no
  extra definition vote.
- RRF rewards agreement between recall lanes. A caller present in both dense
  and lexical recall can consequently bury the strongest lexical caller.
  Ordinary reference requests preserve the fused leader and one lexical head
  from the same strongest evidence category. The slot cannot promote a test,
  local self-use or bare mention over a stronger project consumer.
- Test selection requires actual use evidence. Within those candidates, a
  recorded declaration naming the requested subject can distinguish direct
  tests from incidental setup calls; test-name length supplies no preference.

The diagnostics retain successive source snapshots and every raw attempt.
The `candidate-3` attempt stopped on one client transport error in the short
suite; the same source completed its short-suite retry and remaining suites
under `candidate-3-remainder`. No corresponding server HTTP error established
the cause, so that failed attempt is not counted as a clean run. An isolated
unit-check checkout initially omitted existing modified benchmark support
files; its collection failure and the successful correction are retained too.
The last internal stage-trace attempt also had a client sync transport failure
before retrieval; the separately labelled retry succeeded. These traces inspect
one already-known development query and diagnose stages; they are not product
utility measurements.

## C6 paired development results

`baseline-recovery-1` and `candidate-6` each completed all 11 suites, 613 queries
per source, with zero query errors. The fresh baseline reproduced the preceding
controlled baseline's recorded key quality metrics exactly in all 11 suites.
The runner captured immutable source, evaluator, manifest and client hashes;
comparison also checks case identity, source revisions, runtime configuration
and the shared index fingerprint. No indexing ran during these query passes.
Dedicated reranking, chat reranking and query rewriting were disabled in this
pair; this evaluates the default orchestration. Optional model-enabled behavior
has unit coverage here, but the earlier simplification ablations are not a new
qualification of this C6 candidate with those capabilities enabled.

| Suite | Metric | Controlled baseline | Failed simplification | C6 candidate |
|---|---|---:|---:|---:|
| Short queries | `top1` | 1.0000 | 0.8833 | 1.0000 |
| Short queries | `mrr` | 1.0000 | 0.9272 | 1.0000 |
| Short queries | `path_recall_at_10` | 0.8942 | 0.8789 | 0.8746 |
| Semantic | `top1_primary` | 0.7179 | 0.6667 | 0.6923 |
| Semantic | `mrr` | 0.8748 | 0.8577 | 0.8620 |
| Semantic | `ndcg_at_10` | 0.7297 | 0.7293 | 0.7282 |
| Project relations | `primary_top1` | 0.9429 | 0.8857 | 0.9143 |
| Project relations | `primary_hit_at_3` | 0.9714 | 0.9714 | 0.9714 |
| Project relations | `relation_recall` | 0.9529 | 0.9281 | 0.9586 |
| Project relations | `test_recall` | 1.0000 | 1.0000 | 1.0000 |
| Project relations | `distractor_head` | 0.0000 | 0.0286 | 0.0000 |
| CodeSearchNet | `region_top1` | 0.6250 | 0.6000 | 0.6375 |
| CodeSearchNet | `region_mrr` | 0.7243 | 0.6980 | 0.7347 |
| CodeSearchNet | `region_hit_at_10` | 0.8625 | 0.8375 | 0.8750 |
| SWE development | `core_top1` | 0.8462 | 0.8462 | 0.8462 |
| SWE development | `edit_top1` | 0.5385 | 0.5385 | 0.5385 |
| SWE development | `swe_explore_ndcg_at_100` | 0.7303 | 0.6818 | 0.7675 |
| Held-out relations | `primary_top1` | 0.9167 | 0.7500 | 0.9167 |
| Held-out relations | `primary_hit_at_3` | 0.9583 | 0.7917 | 0.9583 |
| Held-out relations | `relation_recall` | 0.9549 | 0.9861 | 0.9861 |
| Held-out relations | `test_recall` | 1.0000 | 1.0000 | 1.0000 |
| Held-out relations | `distractor_head` | 0.0000 | 0.0000 | 0.0000 |
| Held-out semantics | `ndcg_at_10` | 0.7229 | 0.6763 | 0.7329 |
| Upstream relations | `primary_hit_at_3` | 1.0000 | 1.0000 | 1.0000 |
| Upstream relations | `relation_recall` | 0.7778 | 0.7778 | 0.7639 |
| Upstream relations | `distractor_head` | 0.0000 | 0.1667 | 0.0000 |
| Upstream semantics | `ndcg_at_10` | 0.3772 | 0.3733 | 0.3733 |
| Question variants | `primary_top1` | 1.0000 | 0.9167 | 0.9167 |
| Question variants | `primary_hit_at_3` | 1.0000 | 1.0000 | 1.0000 |
| Question variants | `relation_recall` | 1.0000 | 0.9806 | 1.0000 |
| Question variants | `distractor_head` | 0.0000 | 0.0833 | 0.0000 |
| Layout controls | `top1` | 1.0000 | 1.0000 | 1.0000 |

The failed simplification column is the previous study's completed attempt;
it is historical context, while the baseline and repaired columns are the new pair.

| Suite | Baseline p50 (ms) | Candidate p50 (ms) | Baseline mean chars | Candidate mean chars |
|---|---:|---:|---:|---:|
| short | 878 | 540 | 10961.5 | 11038.7 |
| semantic | 5253 | 2598 | 30507.4 | 31148.6 |
| project | 1093 | 949 | 18982.9 | 18315.8 |
| csn | 4574 | 2477 | 25526.3 | 25528.9 |
| swe | 4701 | 2187 | 27650.8 | 27825.7 |
| heldout | 1220 | 854 | 17168.0 | 17136.7 |
| heldsem | 5079 | 2565 | 24707.0 | 25289.7 |
| upproject | 4888 | 2349 | 25036.2 | 24508.0 |
| upsemantic | 4598 | 2011 | 28957.2 | 28880.5 |
| variants | 1076 | 926 | 18653.6 | 16612.0 |
| layout | 769 | 401 | 1131.5 | 1152.6 |

The short suite recovers its first-answer precision and reciprocal rank. The
main and held-out relation suites recover the earlier Hit@3/coverage losses;
head distractors are zero in all four relation suites. CSN function Top-1 and
MRR exceed the controlled baseline. SWE development retains its core/edit
Top-1 and improves nDCG@100. These are improvements on known development cases, not a
new generalisation or downstream task-success result.

Remaining differences are retained:

- Main-project primary Top-1 is one case below baseline. A runtime-test request
  puts a valid type-test region first; the expected runtime test remains in
  the first three. Its six correlated wording variants make the grouped
  primary Top-1 rate 11/12 rather than 12/12. They are one requirement group,
  not six independent failures. No test-framework/path penalty was added.
- Short-suite path recall in the first ten is lower despite unchanged first
  answer precision. Upstream relation recall is also slightly lower, with
  unchanged primary Hit@3 and complete test recall.
- The main semantic suite loses one primary Top-1: the jq overview query
  ranks its manual before the execution source, which remains second. Overall
  semantic nDCG stays within the existing one-point tolerance, but this does
  not imply every language or intent improves. The negative strata
  below remain visible; small strata have few observations.

| Semantic suite | Stratum | Cases | Baseline nDCG@10 | Candidate nDCG@10 | Change |
|---|---|---:|---:|---:|---:|
| semantic | by_kind: overview | 13 | 0.6497 | 0.6302 | -0.0194 |
| semantic | by_code_language: typescript | 3 | 0.7873 | 0.7726 | -0.0148 |
| semantic | by_code_language: c | 3 | 0.6286 | 0.5498 | -0.0788 |
| semantic | by_code_language: java | 3 | 0.8052 | 0.7360 | -0.0692 |

The existing benchmark policy allows one short query, one semantic nDCG point,
one development issue, and about one main-project case of variation; the
main-project distractor rate may not rise. The development primary guards above
fit those existing tolerances; the external regression below does not. The wording, path-coverage and per-stratum
changes are still limitations; no new tolerance has been invented for them.
Latency and character cost are reported separately. The lower observed p50s
come from this local sequence with query-vector caching disabled and an external
embedding service; they do not establish a production speedup guarantee.

## Previously exposed external regression

The existing 20-case external batch was replayed only after the checked source
and package were frozen. Its questions and per-case results were not inspected
for this repair. Because the preceding study exposed its aggregates, this is a
regression check, not a fresh blind test. The two runs use the same manifest,
client, source revisions, runtime configuration and index; both have zero errors.

| Metric | Controlled baseline | C6 candidate | Change |
|---|---:|---:|---:|
| `core_top1` | 0.7000 | 0.6000 | -0.1000 |
| `edit_top1` | 0.6000 | 0.4500 | -0.1500 |
| `core_mrr` | 0.7542 | 0.6600 | -0.0942 |
| `edit_mrr` | 0.6517 | 0.5255 | -0.1262 |
| `swe_explore_ndcg_at_100` | 0.6280 | 0.4904 | -0.1375 |
| `core_file_recall_at_10` | 0.5483 | 0.4983 | -0.0500 |
| `core_region_recall_at_10` | 0.4525 | 0.4150 | -0.0375 |
| `edit_file_recall_at_10` | 0.7500 | 0.7500 | +0.0000 |
| `edit_region_recall_at_10` | 0.3967 | 0.3717 | -0.0250 |
| `mean_returned_chars` | 30523.6000 | 31339.2500 | +815.6500 |
| `p50_elapsed_ms` | 4695.0000 | 2120.0000 | -2575.0000 |

The external result rejects C6 as a quality-preserving default: core Top-1
loses 2/20 issues, edit Top-1 loses 3/20, and nDCG@100 falls from 0.6280 to
0.4904. Improvements in the development suites cannot compensate for this
loss. Lower latency also does not change that conclusion.

Only the sealed aggregate export was used for this comparison. This C6 source
snapshot remained unchanged after freeze. Its questions and per-case outputs
remain uninspected. Further repairs start a separate development cycle and
require a fresh validation batch, as the original protocol requires.

## Implementation and validation

The fixed stage order, rank-based fusion and unified hard character budget are
retained. Recovery changes add bounded evidence contracts to the existing path;
there is no new query classifier model, database schema, provider-score mixture,
filename blacklist, benchmark-specific condition or additional production module.
The common admission and SQL insertion fixes remain part of both controls.
No additional index-profile version change is needed for these ranking/selection
repairs; the preceding source-admission change still requires its recorded
fail-closed profile migration.

| Size measure | Controlled baseline | Failed simplification | C6 candidate |
|---|---:|---:|---:|
| Production Python lines | 21298 | 20771 | 21024 |
| Production Python files | 166 | 166 | 166 |
| Application retrieval lines | 2039 | 1816 | 1933 |
| Domain ranking lines | 530 | 281 | 389 |

Validation completed against the frozen C6 source:

- Python 3.13 and 3.11: **909 passed, one skipped per version**, with each test
  file run in its own process. The skipped database test requires PostgreSQL,
  which is not configured here; live PostgreSQL behavior is not qualified.
- The real SQLite retrieval regression file is included in those passes.
  Added checks cover use-site membership, explicit implementors, protected
  selection/assembly, direct test subjects, equal file votes, contextual types,
  leading subjects and bounded lexical heads with score-scale/tier invariance.
- Ruff lint and format, lock consistency and `git diff --check` pass. A missing
  import-group blank line in the final added test was corrected; its AST was
  unchanged and all 30 tests in that file were rerun on both Python versions.
  Original full-run proofs and reconciled proofs are both retained.
- Wheel and sdist contain the exact final Python source. Importing the unpacked
  wheel succeeds, its OpenAPI schema matches the controlled API, and the obsolete
  domain retrieval module is absent.
- The unpacked wheel served a fresh personal-mode index. Released `oce-client`
  v0.2.0 successfully synced source under an admitted audit directory, retrieved
  its definition and retrieved both targets in a Chinese multi-symbol request.

Frozen C6 production source-set SHA-256: `624355ede850f66bb52058ca522b83b96537accf4f97c18e165dc36c1a518d02`.

Wheel SHA-256: `66da8efecdb3d073c432f68b494629d683d4ca965f0bb926b0913bd0a83cd90f`.

Sdist SHA-256: `4359c2b84893bde7cac3a7e2388aaa0203958874c09f1db7eaea5efe49583208`.

Artifacts are local verification products, not a published release. All raw
attempts, source snapshots, stage traces, unit logs, packaged smoke responses,
comparisons and digest proofs remain under
`~/.cache/oce/bench-runs/retrieval-quality-recovery-2026-09-11/`. Key entries are
`candidate-final-freeze.json`, `baseline-replication.json`,
`candidate-6-comparison.json`, `semantic-strata.json`,
`regression-aggregate-only.json`, `unit-candidate-6-python{313,311}/`,
`package-final-proof.json`, and `package-final-runtime-proof.json`. The frozen runner is `run_pair.py`; its source parameter,
ordered suites and runtime inputs are captured in each provenance record.

## Cycle 2 development record

The existing 13 issue-development cases contain no C6 head/ranking losses that
explain the exposed external failure. Before further production changes,
additional native issue cases were split by a fixed hash into 18 diagnostic
and 18 sealed validation cases across six repositories. Selection excluded
the previous 20-case batch and previously run issue IDs. Repositories overlap
between the two new groups; this is a case split, not repository-held-out
validation or a complete historical exposure audit. No query text or relevance
label drove selection.

The exact frozen inputs are retained for reproduction:

- [Development selection](issue-quality-recovery-2026-09-11-data/issues-development.json)
- [Sealed validation selection](issue-quality-recovery-2026-09-11-data/issues-validation.json)
- [Query-free index corpus](issue-quality-recovery-2026-09-11-data/issues-corpus.json)

While the new corpus was being indexed, synthetic probes identified two shared
stage defects. With identical recall inputs, prepending `Explain` to an overview
removed its component declaration vote. In a call-chain query, a caller's SQL
use row supplied another relevance vote even though it did not establish a hop
on the requested path. C7 admits overview component declarations regardless of
instruction position and lets call-chain declarations vote while retaining use
chunks as membership evidence. Endpoint protection and call-edge tracing remain
separate. No filename or repository rule was added.

The overview-only and combined development snapshots are retained separately.
The combined source-set SHA-256 is
`467b9fe14ffd1a20e0bd995e9e940ae836351e0e3779207ea0f2ba29bcac2469`.
It passes **912 unit checks with one PostgreSQL-related skip on each of Python
3.13 and 3.11**, using file-isolated processes. These are implementation checks;
no cycle-2 blackbox result or final qualification is claimed yet. Import-only
source-head eligibility has not changed. The new native diagnostic pair still
uses frozen C6 and controlled-baseline sources.

One index setup attempt used a new filesystem endpoint and was rejected by the
profile check before indexing. A later concurrency adjustment interrupted an
active upload because a process-group signal was forwarded twice by its launcher.
The interrupted working index and logs were retained, and a fresh copy of the
pre-cycle-2 backup replaced it. Shutdown now signals only the launcher and allows
the service to finish cleanup. Setup concurrency is separate from the unchanged
query runtime. No failed setup attempt is counted as a quality result. Cycle-2
raw evidence is under `~/.cache/oce/bench-runs/issue-quality-recovery-2026-09-11/`.

The next setup run completed 108 upload batches (7,945 files) before an upstream
embedding HTTP 502 failed batch 109. The service completed normal storage cleanup
and shutdown. Its index and logs were copied before resuming through the existing
non-ready-file retry path. The resumed inventory skipped the first seven complete
snapshots and retried the failed 99-file batch. Preparation now permits at most two
additional retries for this specific upstream failure and retains every attempt.
A query-free released-client/HTTP inventory must confirm zero non-ready files
across the full corpus before the diagnostic pair starts. This interruption is
setup evidence, not a retrieval quality result.

The resumed preparation encountered two further upstream 502 failures: its first
attempt failed on batch 10 (61 files), and the first retry failed on batch 226
(19 files). The final configured retry skipped the first 33 complete snapshots,
retried the failed files, and uploaded the remaining 833 files in 31 batches.
At `2026-09-11T16:14:17Z`, the independent released-client/HTTP inventory confirmed
all 36 snapshots and 38,845 unique admitted files, with **zero non-ready files**.
The setup service then completed normal shutdown. The native development pair
completed with zero query errors for both versions. Core Top-1 is 13/18 for the
baseline and 12/18 for C6; edit Top-1 is 9/18 and 8/18; nDCG@100 is 0.6748 and
0.5758. These are additional development diagnostics, not sealed validation.

The three largest losses were traced through each actual source version. One
Sphinx query recalls the same implementation and translation resources in both
versions, but C6 grants the translation resources source coverage slots. A Django
query recalls a real call/inherit consumer in the declaration's file; C6 treats
that file identity as self-use and puts unrelated import headers ahead of it.
The remaining Django loss leads with a relevant method instead of the baseline's
class header; its lower nDCG remains a measured development tradeoff.

C8 retains the C7 changes and adds two bounded ranking corrections. Source
coverage uses the existing language mapping to identify code candidates; unknown
formats retain their fused scores and remain candidates. This is a file-type
heuristic, with the limitation that unknown code formats also receive no reserved
source slots. Reference ordering removes its declaring-file tier: sharing a file
does not establish ownership by a declaration. Direct declaration chunks remain
last. No per-extension blacklist, unsupported-format score multiplier or SQL
header lookup was added. Source-bound C8 traces restore the Sphinx implementation
head and place the Django consumer first, but those traces are not a utility
qualification. The pre-existing broad reference classification of the Django
issue remains a limitation shared with baseline.

C8 source-set SHA-256 is
`b826d912c6783e8b5a50ede64e6bb701c30724f42e0c5203c5225f388f0af275`.
It passes **917 unit checks and one PostgreSQL-related skip on each of Python
3.13 and 3.11**, with each file run separately. Wheel/sdist source identity,
unpacked import, OpenAPI compatibility, Ruff, format, lock and diff checks pass.
Full paired development and post-freeze validation remain pending.

The controlled baseline on the enlarged shared index completed all 613 cases
with zero HTTP errors, but its server recorded **158 lexical timeout warnings,
four lexical failures, four connection-termination errors, and four pending-task
destruction warnings**. Short-query Top-1 is 0.9875, versus 1.0 in the earlier
control; CodeSearchNet Top-1 is 0.4750 versus 0.6250; SWE development core Top-1
is 0.6154 versus 0.8462. Source, evaluator, manifests, client and runtime controls
match; the admitted corpus has expanded. HTTP success does not establish that
every recall lane was healthy. All raw results and the normal-shutdown proof are
retained. A gain against this damaged control alone would not establish a ranking
repair.

An isolated temporary SQLite pool reproduced the cleanup defect. With both
configured deadlines at two seconds and a controlled 20 ms start-time gap, both
repeats raised `ValueError: Connection closed`, left one connection checked out,
and made the next query time out waiting for the pool. With only the caller's
deadline, both repeats raised `TimeoutError`, returned the pool slot, and allowed
the next query. Near-simultaneous timers did not reproduce the fault; these raw
negative and positive observations are all retained. One unbounded-pool probe
ended with a pool timeout, and the before-fix pytest reproduction failed and
needed an interrupt during teardown. Neither attempt is omitted.

The current development tree therefore removes the lexical store's duplicate
timer and its composition argument. Retrieval and startup warm-up retain their
existing deadlines. The actual-connection regression and all six existing lexical
checks pass after the change. SQLite cleanup can still finish after the deadline;
this repair does not make its worker query interruptible or claim a hard two-second
response limit. These small, isolated sleep-based probes did not use the evaluation
database; the baseline continued separately. These initial checks did not qualify retrieval utility; the subsequent C9
implementation checks and paired study are recorded below.

The follow-up SQL comparison used the closed evaluation database in read-only
mode. One input came from a complete C8 stage trace whose client timed out while
Milvus loaded; the server finished that request and shut down normally. The other
was a deterministic C8 replay of an already-traced development question and its
released-client checkpoint, with the route checked against the previous trace.
Neither observation is a blackbox utility result. The first input has an
identifier gate; the second is a broad semantic query. Each SQL variant ran three
times in rotating order, with all timings retained:

| Input | Original nested EXISTS, ms | Both membership subqueries as IN, ms |
|---|---|---|
| Identifier-gated, 4,884 files | 4277, 293, 296 | 173, 170, 166 |
| Broad semantic, 5,260 files | 5763, 1308, 1339 | 493, 569, 512 |

For both inputs, the original and chosen variant return exactly the same 90
content hashes, scores and order. Cache effects are visible, so the first-pass
ratios are not general latency claims. Changing only chain membership exceeded
the 30-second query bound in all six attempts; changing only hash membership was
slower than the chosen variant. The current code changes both together. Shared
exact, definition, caller, implementation, reexport and path queries also returned
identical results in a bounded read-only check. Some small exact queries cost
additional milliseconds; no claim of universal speedup is made.

C9 retains C8 and applies the two SQL membership changes plus caller-owned lexical
timeouts. Its source-set SHA-256 is
`f61b6e4d3146bdf9dac200232c222ceb0a7bf78b4384ce95464432713d9d97d2`.
All **920 unit checks pass, with one PostgreSQL-related skip on each of Python
3.13 and 3.11**. The new tests cover actual connection cleanup, checkpoint request
deltas, and fallback to the resolved scope after a checkpoint version changes.
Wheel/sdist source identity, unpacked import, OpenAPI compatibility, static checks
and released-client packaged smoke pass. The first smoke launcher selected an
empty virtual environment and failed to import `httpx`; using the project
interpreter completed it. Both logs are retained.

To separate ranking changes from runtime availability, the same SQL/runtime fix
has been applied to an isolated copy of the original control. Exactly three
production files change: `lexical_index.py`, `scope_filter.py`, and the lexical
constructor argument in `container.py`. The shared SQL files are byte-identical
to C9, while the original control's ranking and retrieval orchestration remain.
The new control source-set SHA-256 is
`db722e0d2ca67e3c975e768f3da5e8007d2573b4fca3eedc7e4cc53be2d88b82`.
Its lexical, symbol, path and SQL integration checks pass on both Python versions.
The original damaged control is preserved. A fresh 631-case development pair
(11 existing suites plus 18 native diagnostic cases) is now running with this
shared SQL repair. Sealed validation remains unopened and cannot start before
the final development choice and source freeze.

The supplementary model-usage deltas are **INVALID for cost or call-count
comparison in cycle 2**. The frozen evaluator subtracts two `/admin/stats`
responses, each covering a moving 24-hour window; these totals are not monotonic.
The repaired baseline's upstream relation batch already reports an impossible
embedding delta of -1 call and -7,320 prompt tokens. Older records leaving the
window can offset new usage. Raw counters are retained without clamping, and
positive deltas are not assumed complete either. Retrieval relevance metrics,
returned characters and client-measured elapsed times do not use these counters.
The frozen evaluator remains unchanged during the pair.

The repaired control completed all 631 development cases with no HTTP errors,
lexical timeout/failure warnings, interrupted connection cleanup, pending-task
destruction or unreturned-connection messages. C9's matching batch is still in
progress and has exposed a short-query regression: Top-1 falls from 1.0000 to
0.9375, MRR from 1.0000 to 0.96875, while p50 falls from 1,481 to 583 ms. **C9 is
not frozen for validation.** Faster responses do not qualify this result.

All 15 changed heads are reference questions returning the declaration's file.
The frozen short-query evaluator excludes that entire file from reference truth,
although the questions do not ask for references in another file. Its
`definition_top1` field measures file identity, not whether the returned region
is a declaration. A post-hoc source audit found visible code references in 14
heads: `get_config` constructs `Config`, `node.getValue` accepts `Params`, Rust
blocks implement the requested traits, and other returned methods construct or
refer to their enclosing types. These observations do not change the frozen
score, create an independent truth set, or waive a tolerance. The source-bound
case audit is retained as `c9-short-reference-audit.json` in the cycle-2 evidence
directory. Restoring a whole-file penalty would repeat the unsupported ownership
inference that displaced the independent Django `IndexColumns` consumer.

The remaining Flask head is a class docstring. The initial source audit
incorrectly attributed its admission to ancestry alone. Full-span inspection
shows the complete case-sensitive word `Blueprint` in prose on lines 140, 148
and 149. That initial causal claim was false; its correction is retained in
`c9-short-reference-audit-correction.json`, with source hashes and exact lines.
The generic ancestry-only admission bug is separately demonstrated by synthetic
checks, but fixing it does **not** remove this Flask prose head. The original
C9 audit file remains unchanged as a record of the mistaken diagnosis, not as
a supported cause. Sealed validation has not started.

C9 then completed all 631 development queries and shut down normally. Its native
18-case diagnostic group improves core Top-1 from 13/18 to 14/18, edit Top-1 from
9/18 to 10/18, and nDCG@100 from 0.6621 to 0.7025. The Django same-file consumer
now leads. Core-region recall decreases from 0.5083 to 0.4898; returned characters
increase from 31,181.6 to 31,727.3. These are separate tradeoffs. The final native
batch records **one lexical timeout fallback**, with no lexical failure or
connection-cleanup error; earlier zero-warning progress was provisional.
The full vector, strata, native diagnostic comparison and source-bound lane
health are retained in `cycle2-c9-development-comparison.json`,
`c9-diagnostic-comparison.json` and `cycle2-c9-development-lane-health.json`.

C10 changes only the reference body-evidence predicate in production. Before the
fix, four synthetic checks fail: bare and qualified names present only in ancestry,
and unrelated class prose displacing an actual consumer in either the same file
or another file. Two scope-disambiguation controls pass. After the fix all 119
checks in the structural-evidence and retrieval-stage files pass. SQL occurrences
and body-leaf qualification retain their existing paths. No index semantics
change, so the existing index profile remains valid. Full C10 qualification is
pending; sealed validation remains unopened. Its completed short suite retains
exactly the same first returned spans as C9: Top-1 is still 0.9375 and MRR 0.96875.
This repair has not established a measured head-quality gain on that suite.

C10 implementation checks are complete on source-set SHA-256
`e3a436f079894267b4e023974e88ffb4657aaa04b983c54fbb4804386f41a1a8`:
**926 unit checks pass with one PostgreSQL-related skip on each of Python 3.13
and 3.11**, with all 100 unit-test files run separately. Ruff, format, lock and
diff checks pass. Wheel/sdist source identity, unpacked import, unchanged OpenAPI
and a fresh packaged server driven by released `oce-client` pass; that server
shuts down normally. `c10-implementation-proof.json` binds these artifacts.
The matching full development run has started without other test or server loads.

The C9 main-project Top-1 losses also need separate interpretation. RTK returns
`isFulfilled` / `isAsyncThunkAction` bodies that actually call `isAnyOf`, in the
same file as its declaration; the benchmark's preferred consumer is second.
The test-mapping case puts a type test ahead of a runtime test, also second, with
six correlated wording variants. Express instead returns a `tryRender` region
containing `view.render` before the requested `app.render` caller. That is a
remaining receiver-disambiguation limitation, not a claim that both regions are
equally correct. Main-project Hit@3 and test recall stay unchanged, while relation
recall improves. These facts do not turn three Top-1 losses into a tolerance pass.
C10's final vector must still be measured rather than inferred from this audit.

## C10 completed development comparison

All 631 queries complete with no HTTP errors, lexical timeout/failure warnings,
connection-cleanup errors or pending-task destruction, followed by normal shutdown.
The healthy control is the same `baseline-sql-control` used for C9. Source, client,
evaluator, manifests, index profile, dense/path counts and retrieval configuration
are checked by `cycle2-c10-development-comparison.json`; its full strata and the
separate `cycle2-c10-development-lane-health.json` are retained.

| Suite | Metric | Healthy control | C10 |
|---|---|---:|---:|
| short | `top1` | 1.0000 | 0.9375 |
| short | `mrr` | 1.0000 | 0.9688 |
| short | `path_recall_at_10` | 0.8936 | 0.8738 |
| semantic | `ndcg_at_10` | 0.7312 | 0.7371 |
| project | `primary_top1` | 0.9429 | 0.8571 |
| project | `primary_hit_at_3` | 0.9714 | 0.9714 |
| project | `relation_recall` | 0.9210 | 0.9457 |
| project | `test_recall` | 1.0000 | 1.0000 |
| project | `distractor_head` | 0.0000 | 0.0000 |
| csn | `region_top1` | 0.6375 | 0.6750 |
| csn | `region_mrr` | 0.7305 | 0.7618 |
| swe | `core_top1` | 0.7692 | 0.7692 |
| swe | `edit_top1` | 0.4615 | 0.4615 |
| swe | `swe_explore_ndcg_at_100` | 0.6534 | 0.6534 |
| upproject | `primary_hit_at_3` | 1.0000 | 1.0000 |
| upproject | `relation_recall` | 0.7778 | 0.7639 |
| upsemantic | `ndcg_at_10` | 0.3772 | 0.3733 |
| heldout | `primary_hit_at_3` | 0.9583 | 0.9583 |
| heldout | `relation_recall` | 0.9757 | 0.9861 |
| heldsem | `ndcg_at_10` | 0.7216 | 0.7290 |
| variants | `primary_top1` | 1.0000 | 0.9167 |
| variants | `primary_hit_at_3` | 1.0000 | 1.0000 |
| variants | `relation_recall` | 1.0000 | 0.9972 |
| layout | `top1` | 1.0000 | 1.0000 |
| issue-dev | `core_top1` | 0.7222 | 0.7778 |
| issue-dev | `edit_top1` | 0.5000 | 0.5556 |
| issue-dev | `swe_explore_ndcg_at_100` | 0.6621 | 0.7025 |
| issue-dev | `core_region_recall_at_10` | 0.5083 | 0.4898 |

| Suite | Control p50 (ms) | C10 p50 (ms) | Control chars | C10 chars |
|---|---:|---:|---:|---:|
| short | 1481 | 600 | 10957.5 | 10982.6 |
| semantic | 3892 | 2733 | 29951.6 | 29738.5 |
| project | 1841 | 1420 | 18596.4 | 17810.7 |
| csn | 3974 | 2948 | 25192.0 | 25269.5 |
| swe | 3925 | 2579 | 28487.2 | 28896.2 |
| upproject | 3629 | 2160 | 24133.8 | 24448.5 |
| upsemantic | 4358 | 2740 | 29421.0 | 29172.7 |
| heldout | 1804 | 1638 | 16847.5 | 16573.6 |
| heldsem | 3864 | 2705 | 24210.4 | 24967.7 |
| variants | 1741 | 690 | 18679.2 | 16559.9 |
| layout | 1351 | 528 | 1131.5 | 1152.6 |
| issue-dev | 4961 | 3486 | 31181.6 | 31673.7 |

The native issue target and several coverage metrics improve, but the frozen
short-suite and main-project Top-1 losses exceed their existing tolerances.
Implementation checks are complete; this is **not a full utility acceptance**.
The short-suite file-level truth problem, the Flask prose head, the Express
receiver ambiguity and the runtime/type-test order are recorded above. No new
tolerance is introduced. A source-bound trace of the known Express development
case is next; sealed validation remains unused while that development decision
is open. The C10 source and all completed measurements are retained unchanged.

## Qualified caller evidence after C10

A released-client HTTP trace of the frozen Express development query reproduces
the wrong head on the exact C10 source. It records `lib/application.js:612-661`
and `lib/response.js:992-1052` in the SQL use-site list, with no enclosing context.
The first body calls `view.render` while mentioning `app` elsewhere; the second
contains the requested `app.render` call. Lexical recall prefers the second, but
dense recall prefers the first and RRF puts it slightly ahead. The head policy
then gives both the same call tier. The source, lane bodies, stage order, response
and normal shutdown are retained under `trace-c10-express-qualified-*`. This is
internal diagnosis of a known development case, not a utility or timing result.

C11 keeps all possible SQL use-site candidates. Within head ranking, a qualified
call now needs the existing complete-name/body-scope predicate to receive the
call tier; a leaf call admitted only by an owner mentioned elsewhere in its
chunk retains membership without that priority. Unqualified calls retain their
SQL evidence. This reuses the same name-resolution contract as reference
admission and introduces no new query rule, path penalty, lookup or setting.

Seven synthetic checks fail on C10 and pass after the change. They cover full
qualified spelling, enclosing scope, module scope, source and test requests,
and pipeline retention of the uncertain candidate behind the evidenced caller.
All 126 checks in the two affected test files pass. A released-client HTTP trace
on the exact C11 source returns `lib/response.js:992-1052` first while retaining
`lib/application.js:612-661` second. The trace confirms this known development
repair; it does not substitute for a complete utility comparison.

All **933 unit checks pass, with one PostgreSQL-related skip, on each of Python
3.13 and 3.11**, using 100 separate test-file processes per version. Formatting,
lint, lock and diff checks pass. The wheel and sdist contain the current source,
the unpacked OpenAPI matches the baseline, and a packaged server passes
released-client HTTP sync/retrieve checks and shuts down normally.
`c11-implementation-proof.json` binds these checks and the trace to source-set
SHA-256 `3348cfb73406a60f28a9f3037676c0cc41ece6925a830220a18cc69a10dd5153`.
Wheel SHA-256 is
`91cd798d8c1f1e8bcd494daa21d17766ad4253699fb09d5b13ca5c1bb75ae9f6`.
The subsequent development attempt is recorded below; validation remains sealed.

## C11 completed attempt: incomplete provenance and negative guards

All 631 queries return without HTTP errors, followed by normal shutdown. The
strict paired exporter then rejects the run because the short-suite artifact has
`runtime.server_configuration: null`. The evaluator could not capture that
snapshot; the retained startup log also contains a dense warm-up timeout, but it
does not establish the cause of the missing snapshot. No configuration is copied
from another suite or later request to fill this gap. The normal strict exporter
and freeze requirements remain unchanged.

`cycle2-c11-development-observations-incomplete.json` is a separate diagnostic
export, explicitly marked ineligible for qualification. It retains every summary
and stratum, verifies source/client/truth identity and the available runtime
snapshots, and labels the short suite as missing runtime provenance. Its values
below are observations from the incomplete attempt, not a complete acceptance.

| Suite | Metric | Healthy control | C11 attempt |
|---|---|---:|---:|
| short* | `top1` | 1.0000 | 0.9375 |
| short* | `mrr` | 1.0000 | 0.9688 |
| short* | `path_recall_at_10` | 0.8936 | 0.8738 |
| semantic | `ndcg_at_10` | 0.7312 | 0.7371 |
| project | `primary_top1` | 0.9429 | 0.8857 |
| project | `primary_hit_at_3` | 0.9714 | 0.9714 |
| project | `relation_recall` | 0.9210 | 0.9210 |
| project | `test_recall` | 1.0000 | 1.0000 |
| project | `distractor_head` | 0.0000 | 0.0571 |
| csn | `region_top1` | 0.6375 | 0.6625 |
| csn | `region_mrr` | 0.7305 | 0.7493 |
| swe | `core_top1` | 0.7692 | 0.7692 |
| swe | `edit_top1` | 0.4615 | 0.4615 |
| swe | `swe_explore_ndcg_at_100` | 0.6534 | 0.6534 |
| upproject | `primary_hit_at_3` | 1.0000 | 1.0000 |
| upproject | `relation_recall` | 0.7778 | 0.7639 |
| upsemantic | `ndcg_at_10` | 0.3772 | 0.3733 |
| heldout | `primary_hit_at_3` | 0.9583 | 0.9583 |
| heldout | `relation_recall` | 0.9757 | 0.9861 |
| heldsem | `ndcg_at_10` | 0.7216 | 0.7290 |
| variants | `primary_top1` | 1.0000 | 0.9167 |
| variants | `primary_hit_at_3` | 1.0000 | 1.0000 |
| variants | `relation_recall` | 1.0000 | 0.9139 |
| layout | `top1` | 1.0000 | 1.0000 |
| issue-dev | `core_top1` | 0.7222 | 0.7778 |
| issue-dev | `edit_top1` | 0.5000 | 0.5556 |
| issue-dev | `swe_explore_ndcg_at_100` | 0.6621 | 0.7025 |
| issue-dev | `core_region_recall_at_10` | 0.5083 | 0.4898 |

*Short-suite runtime provenance is missing.*

| Suite | Control p50 (ms) | C11 p50 (ms) | Control chars | C11 chars |
|---|---:|---:|---:|---:|
| short* | 1481 | 632 | 10957.5 | 10895.4 |
| semantic | 3892 | 2924 | 29951.6 | 29269.7 |
| project | 1841 | 1661 | 18596.4 | 16905.6 |
| csn | 3974 | 3275 | 25192.0 | 24518.3 |
| swe | 3925 | 2929 | 28487.2 | 28581.1 |
| upproject | 3629 | 2290 | 24133.8 | 23572.2 |
| upsemantic | 4358 | 2810 | 29421.0 | 29172.7 |
| heldout | 1804 | 1735 | 16847.5 | 16378.4 |
| heldsem | 3864 | 3416 | 24210.4 | 23197.1 |
| variants | 1741 | 1411 | 18679.2 | 15860.7 |
| layout | 1351 | 513 | 1131.5 | 1152.6 |
| issue-dev | 4961 | 3655 | 31181.6 | 31673.7 |

The Express qualified-caller head is repaired in this attempt, while the two
remaining main-project Top-1 losses and the short-suite losses still exceed the
existing tolerances. Main-project head distractors additionally rise from zero
to two. The affected cases ask for definitions (`Flask.make_response` and the
pytest parser), outside the reference-only functional change from C10 to C11.
Other changed relation tails also occur in unchanged branches. Their differences
cannot yet be attributed to the C11 ranking change.

Project relation recall falls from C10's 0.9457 to 0.9210, and the 72 correlated
variants fall from 0.9972 to 0.9139. Native issue-development core/edit Top-1 and
nDCG stay at C10's improved values, with the same core-region recall loss relative
to the control. These gains do not offset the failed guard dimensions.

The lane-health export records zero lexical timeout/failure warnings and zero
connection-cleanup, pending-task or unreturned-connection messages. This is not
proof that all SQL work completed: several symbol/relation methods swallow their
own two-second timeout and return empty results. The two newly exposed definition
cases each take just over two seconds in C11. That motivates instrumentation of
actual deadline expiry; timing alone does not prove that this is their cause.

A repeated, source-bound HTTP trace of one known development definition query
is next, on C11 and C10. It records unchanged settings, lane content and actual
symbol-store timer expiry without changing production behavior. The first
instrumented attempt imported persistence before the CLI configured personal
mode and failed before any query; that diagnostic failure is retained, and the
instrumentation now defers its import until retrieval begins. No validation
freeze or validation request has occurred.

## C12: scope filtering before global symbol probes

Three repeated requests on C11 and three on C10 all return the same 16 positions
and 13,319 characters as the C10 formal definition case. Their source hashes and
settings are verified. None expires a symbol-store timer; the related definition
lookup takes roughly 1.65–1.86 seconds, close to its two-second limit. Thus the
original C11 five-position response is not reproduced by these repeats, and its
unlogged timeout remains a hypothesis rather than a directly observed event.

A read-only profile of the captured development expansion isolates about 1.25
seconds in declaration counting and 0.40 seconds in fetching declaration content.
SQLite's query plan starts from all ready blobs, probes each requested identifier
in each file, and only then rejects out-of-scope occurrences. Applying the same
scope predicate to the already joined blob column filters files before those
probes. In three read-only repetitions, counting drops from 1,241–1,268 ms to
24–27 ms and fetching from 394–413 ms to 22–24 ms. The exact ordered rows are
identical in each pair. These are internal SQL diagnostics, not product latency
or utility results; the original plans and full query records are retained in
`c11-symbol-sql-profile.json`, `c11-symbol-sql-plans.json` and
`c11-symbol-sql-scope-ablation.json`.

C12 applies that equivalent predicate placement in all five symbol-store calls
to the shared scope helper. It preserves requested scopes, ready status, limits,
ordering, index contents and the existing two-second deadline. Six timeout
fallbacks now emit a warning without logging query text or returning a different
fallback value. No retrieval-ranking rule, model setting or index-profile version
is changed by C12.

Eight intended regression checks fail on C11 and pass after the repair. Two use
a deterministic SQLite instruction budget with 2,000 unrelated ready files;
six verify that a deadline still returns the existing empty fallback and records
its missing evidence. The first logging test attempt had an instrumentation
setup error, retained separately; the corrected pre-change test run fails on
all eight intended contracts. All 21 symbol-store checks pass after the change.
A read-only rerun of the captured expansion returns the same 16 filtered
definitions in about 99 ms. Formatting, lint, lock and diff checks pass.

All **941 unit checks pass with one PostgreSQL-related skip on each of Python
3.13 and 3.11**, with the 100 test files run in separate processes. Wheel/sdist
source identity, unpacked import, unchanged OpenAPI and the packaged server's
released-client sync/retrieve smoke pass, followed by normal shutdown.
`c12-implementation-proof.json` binds these artifacts to the final source-set
SHA-256 `23a7617dcb0c7e21095fb6098b7a9613100c9ab465f84550d9c4c5b32fa89cc2`.
Wheel SHA-256 is
`63a073db91ac63d2dbe4d7de4428a2a8d6d949b75c69bdfb1ed00ab89a4e17ac`.

Three actual HTTP requests on that source return the complete known development
case's 16 positions and 13,319 characters, with exactly the same ordered spans
as C10's complete formal response. No symbol-store timer expires, and the server
shuts down normally. This trace verifies the recorded development behavior; it
does not establish independent utility or a general latency improvement.

The new `baseline-sql2-control` shares this SQL repair and the earlier lexical
repair, retaining the original ranking rules. Its source-set SHA-256 is
`6991af689d2b98ead75a05e527502c93ef30290094093c4ab2d65278fbfa795f`.
Relative to the original controlled baseline, exactly four production files
differ: the lexical constructor in `container.py`, `lexical_index.py`,
`scope_filter.py` and `symbol_search_store.py`. The shared SQL implementations
match C12; the control additionally retains the unchanged occurrence method
required by its original ranking. The lexical, symbol, path and SQL integration
test files pass on both Python versions.

Both sources completed the same 631-case development sequence on the shared
index; the current comparison is presented at the start of this report. The runner first captures runtime configuration without issuing a query,
then requires every suite to capture matching provenance; missing metadata stops
the attempt. This orchestration check does not change the frozen evaluator,
questions, labels or tolerances. C12 was subsequently frozen for the independent
assessment described at the start of this report.

This report does not claim an ACE comparison or downstream agent task success.
The preceding negative study remains unchanged, and no commit, tag or push is
part of this repair.

# Retrieval repair isolation

Status: foundation repairs validated; none of the three stage hypotheses adopted.
The three historical lexical timeouts remain unreproduced and their cause is
unresolved. Retrieval quality is not fully repaired, and no new default ranking
is release-qualified by this work.

## Retained foundation

The worktree retains the ranking from `879bc2e` with seven production files
carrying shared repairs: scope-filtered lexical retrieval, bounded checkpoint
membership, early file scoping for symbol SQL, observable symbol timeouts, one
caller-owned lexical deadline, batched symbol insertion, and admission of ordinary
source directories ending in `-retrieval-eval`. The complete C12 worktree, source
proof and binary patch were saved before the split; its ranking and assembly
changes remain archived experiments.

The final source-set SHA-256 is
`8e6e3bccbbc61d032721e6235041b2c9d123bef4d3442448e9592cfee7a10a38`.
Before formatting it matched the measured SQL control byte for byte:
`6991af689d2b98ead75a05e527502c93ef30290094093c4ab2d65278fbfa795f`.
The only subsequent production change removes an extra blank line in
`symbol_search_store.py`; all 166 Python modules have identical ASTs to the
control. [Final source proof](retrieval-isolation-2026-09-11-data/foundation-final-source.json)
binds this equivalence. Source admission is version 2; an incompatible version 1
index requires a new data directory and complete client resynchronization.

## Lexical timeout investigation

The exposed 18-case validation and 20-case historical regression were promoted
to development evidence before inspecting individual requests. Their manifests
and original results remain unchanged and cannot become independent validation
again after this diagnosis.

Sequential HTTP-response mapping associates the three historical C12 warnings
with `astropy__astropy-13398`, `astropy__astropy-12907` and
`django__django-16333`, among the first four requests. This is temporal association,
not causal attribution. Both dense and path startup warm-up timed out in that
historical run; readiness did not establish that all native storage was warm.

The first tracing attempt incorrectly called a static SQL method and attached
query context too late. It is retained as INVALID. Corrected instrumentation
passed all nine lexical-store checks, then repeated the four actual requests
three times through the released client. All 12 searches returned 30 lexical
hits, without an error or timeout. The first astropy and django searches took
about 1,231 and 1,541 ms; their later repetitions took 258–262 and 558–559 ms.
That establishes a first-request cost in this run, not the cause of the old loss.
[HTTP repetition evidence](retrieval-isolation-2026-09-11-data/lexical-http-repeat.json)
retains every measured call.

A read-only workload then ran the captured inputs sequentially, four at a time,
and sequentially again, keeping the production two-second deadline. All 27 calls
returned identical ordered results and scores; the slowest was 663 ms.
[Pressure profile](retrieval-isolation-2026-09-11-data/lexical-pressure-profile.json)
does not prove that contention was absent historically. A further SQL ablation
moved lexical membership from the joined chunk column to the blob column. All 24
result sets were identical, while SQLite instruction counts and warm timings were
effectively unchanged; that extra query change was not adopted.

The retained cancellation regression keeps an actual SQLite worker busy after
the caller deadline. It verifies that the connection returns to a one-connection
pool, the next query succeeds, and cleanup logs no termination error. This
supports removing the duplicate timer; it does not explain the later C12
warnings, which already used that repair. Production deadlines have not increased.
Startup/cache pressure and scheduling delays remain possible explanations without
enough evidence to identify the historical cause.

## Single-stage development screen

Each variant starts from the foundation and changes one stage; AST checks record
the changed methods. Fusion normalizes dense, lexical, exact and file evidence by
rank, without C12's recall/head/assembly changes. The head variant removes soft
semantic source-head promotion while retaining priors and deterministic heads.
The assembly variant preserves primary context when ordinary relation sections
need space; explicit chain behavior is unchanged. This screens three concrete
hypotheses, not the complete factorial decomposition of C12.

All variants use the same prepared index sequentially, the same released client,
unchanged evaluator/questions, disabled query cache and disabled model rerankers.
The 18 promoted issues and 35 original project cases yield 212 new blackbox
requests. All succeed, without request-time lexical or symbol timeout warnings.
[Protocol](retrieval-isolation-2026-09-11-data/stage-ablation-protocol.json) and
[case-level comparison](retrieval-isolation-2026-09-11-data/stage-comparison.json)
preserve inputs and outputs.

| Metric | Foundation | Fusion only | Heads only | Assembly only |
|---|---:|---:|---:|---:|
| Issue edit Top-1 | 0.6111 | 0.5556 | 0.6111 | 0.6111 |
| Issue edit MRR | 0.7579 | 0.7148 | 0.7255 | 0.7579 |
| Issue nDCG@100 | 0.5700 | 0.5233 | 0.5721 | 0.5700 |
| Issue edit file recall@10 | 0.9815 | 0.9815 | 0.9815 | 0.9815 |
| Issue mean characters | 32,119.5 | 31,675.8 | 32,119.5 | 32,345.1 |
| Project primary Top-1 | 0.9429 | 0.9429 | 0.9429 | 0.9429 |
| Project primary Hit@3 | 0.9714 | 0.9714 | 0.9714 | 0.9714 |
| Project relation recall | 0.9433 | 0.9281 | 0.9433 | 0.9433 |
| Project distractor head | 0 | 0 | 0 | 0 |
| Project mean characters | 19,101.2 | 18,658.3 | 19,101.2 | 19,294.7 |

Fusion loses the first useful issue answer for `django__django-15278` and reduces
project relation coverage, so it is rejected. The head variant's small nDCG@100
increase accompanies lower MRR and lower nDCG@300/500, with no project gain.
Preserving the primary tail leaves the main relevance metrics unchanged and
increases characters. Neither establishes enough benefit to adopt. No new
ranking enters the worktree, and no fresh independent validation set was consumed
for these rejected directions.

Latency remains separate. Three screen servers recorded dense startup warm-up
timeouts, and first-request behavior varies. Several native token deltas are
negative when differencing moving 24-hour aggregate windows; those values cannot
serve as run-specific token accounting and are excluded from conclusions. No
speedup or cost reduction is claimed.

## Reference contract and remaining quality limits

The separate [v2 contract](reference-region-contract-v2-2026-09-11.md) and
[manifest](../blackbox/reference_regions_v2_2026_09_11.json) cover 18 unchanged
English/Chinese questions, nine symbol families, seven languages and 130 reviewed
use lines. Same-file uses can count; an import, declaration name or prose mention
does not establish a use. The initial incomplete catalog and its source-audit
correction are preserved. This is development annotation work, not blind truth.
Old short-query scores remain intact. Differences between the file and region
metrics do not establish a retrieval improvement.

The final foundation wheel returns the `Blueprint` consumer in
`Flask.register_blueprint` first for both wordings. Its project run again places
the runtime `configureStore` test before the type test. Those C12 regressions
are not retained in the worktree. The v2 catalog still exposes foundation limits,
including import-only or prose heads on other queries. Measured catalog Top-1 is
0.6111 and Hit@3 is 0.8333. An unlisted valid use must be audited, not automatically
called irrelevant. The foundation is a validated starting point, not a solution
to all retrieval-quality problems.

## Final validation and evidence

Both Python 3.13 and 3.11 pass **852 unit checks**, running each of the 99 files
separately. Each has one PostgreSQL-specific skip because that backend is not
configured. **Six real Milvus Lite integration checks** pass. Ruff check/format,
lock consistency and whitespace checks pass. Wheel and sdist match all final
production Python files; unpacked-wheel import succeeds and its OpenAPI schema
matches the foundation. The wheel serves all 18 v2 requests through the released
client with stable runtime/index metadata, then shuts down normally. PostgreSQL
service-mode integration was not exercised. The [final proof](retrieval-isolation-2026-09-11-data/final-proof.json)
binds the source, unit logs, package and runtime artifacts.

The previously measured foundation's four canonical guard reports are retained
with verified source identity: short 240, semantic 39, project 35 and development
SWE 13 cases, all without request errors. These are reused historical evidence,
not newly rerun suites. [Retained guard proof](retrieval-isolation-2026-09-11-data/retained-foundation-guards.json)
and the new paired screen supplement the final implementation/package checks.

Durable summaries and source proofs are in
`benchmarks/results/retrieval-isolation-2026-09-11-data/`. Full native reports,
SQL traces, invalid attempts, C12 backup, experimental source copies, per-file
logs and packages remain under
`~/.cache/oce/bench-runs/retrieval-isolation-2026-09-11/`.
No commit, tag, push or deployment was performed.

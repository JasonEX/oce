# Reliability fixes and simplification, 2026-10-01

The changes repair ownership, persistence, configuration and budget boundaries while
preserving measured retrieval quality. The paired blackbox run preserved all 327
cases' status, retrieved paths and line spans, quality metrics and character counts.
The frozen internal replay preserved 214 of 215 hit sequences; its one changed
excerpt now fits the configured character limit.

## Implemented changes

| Problem | Result |
| --- | --- |
| SQLite connections did not enforce metadata foreign keys | Production connections enable foreign keys; migration `b8c9d0e1f2a3` removes historical orphan staging, symbol, chunk-link and lexical rows while preserving live data. |
| SQL deletion could finish before failed vector cleanup | The existing blob lifecycle retains `DELETING` until dense and path cleanup both succeed. GC retries these identities, rechecks activity and checkpoint references, and uses worker maintenance for real deletion. |
| Checkpoint/indexing races could reuse an identity being deleted | Prepare locks/touches and rereads pending identities; stale aggregate saves and checkpoint updates reject `DELETING`. Checkpoint failure rolls back membership and version changes. Pending and absent checkpoint identities remain valid. |
| A Redis batch-fill failure stranded already confirmed claims | Confirmed claims are returned to their worker; a failure before any confirmed claim still propagates. |
| Concurrent model reloads could mix generations or report partial success as complete | One runtime lock serializes reload. Disabled embedding validates its disabled profile without activating credentials. Partial LLM failures return the existing `reloaded=false`/`reason` fields. |
| Metrics collection blocked the event loop; queue counts loaded whole identity sets | Resource collection runs in a thread. Queue status uses SQL `COUNT` and Redis `SCARD`. |
| Personal mode promoted `.env` before `.env.local` could override it | Both files enter one ordered dotenv parse. Process variables retain priority; explicit `--env-file` retains its existing priority. Cross-file and repeated-key interpolation are covered. |
| Selectors or adjacent merging could exceed the code budget | Both production selectors bound leading excerpts, preserving complete lines where possible. Merging includes separator cost and retains original chunks when merging would exceed the limit. |
| Milvus path text capacity was smaller than supported source paths | Upload paths are bounded at the existing SQL capacity of 1024 characters. Full path documents remain embedding inputs; bounded UTF-8 diagnostics fit existing collections. SQL supplies complete result paths. Legacy SQLite paths remain readable. |
| Scope could include partial writes or widen after a pending blob became ready | READY metadata gates the resolved scope. Existing exclusions freeze non-ready identities; SQL reuses the common scope filter and checkpoint relation. |
| Declaration audit fields counted recalled chunks | Audit counts recorded declarations per leaf name before chunk deduplication/recall limits; `definition_sites` is the maximum per-name count. Failures are audited while preserving exact answers. |
| Indexing had duplicated production orchestration and a separate same-transaction convenience flow | Sync and worker callers share one short-transaction application function with explicit failure policy. The domain convenience method is removed; SQL fixtures and internal replay use production handlers. |
| Retrieval reparsed the issue title and silently probed required protocol capabilities | Title evidence is frozen in route. Required protocol methods are called directly and failures remain visible through `lane_failed`. |

The retrieval docs and AGENTS now describe the implemented rerank policy inputs:
declaration counts and head slots are audit evidence for calibration. Source ranking,
exact fusion and rerank policies retain their existing behavior. The LangChain
fallback dependency remains pending a separate per-language equivalence and cost
assessment.

## Validation

- Unit tests: 107 independently run files, initially 945 passed and 1 skipped.
  The final CLI/path compatibility run passed 16 cases, replacing 10 previously
  verified cases and adding 6: **951 unique passed, 1 skipped**. The skipped test
  needs a configured PostgreSQL instance; PostgreSQL concurrency was reviewed
  against transaction semantics, while SQLite races were exercised directly.
- Six Milvus Lite integration tests passed. Real Container/HTTP smoke, failed
  deletion recovery, orphan migration, long Unicode paths and checkpoint rollback
  are covered by the unit suite.
- Ruff check/format, mypy (164 source files), lock and diff checks passed.
  Final wheel/sdist built; importing the packaged app, loading a legacy long path
  and migrating a fresh packaged database to the new head passed.
- Frozen replay: 180 files, 43 queries, 5 profiles. Hits were identical in 214/215
  pairs, including every default-profile pair. Under the 3000-character profile,
  the `Milvus3Client` excerpt changed from 3018 to 2949 characters, retaining its
  path, score and start line. Strict records were identical in 174/215 pairs:
  40 differences were audit corrections only, and one also contained this bounded
  excerpt. Routing, relation fields, stage names and lane failures were identical.

## Product blackbox pair

| Suite | Cases | Quality, identical in both runs | Mean characters, identical | p50 ms, baseline to candidate |
| --- | ---: | --- | ---: | ---: |
| short_queries | 240 | Top-1 / MRR 1.0000 | 11043.41 | 41 to 41 |
| semantic_queries | 39 | nDCG@10 0.7526 | 30410.13 | 559 to 507 |
| project_cases | 35 | Hit@3 0.9714; relation recall 0.9338; test recall 1.0000; distractor head 0 | 19244.43 | 68 to 70 |
| swe_explore, development | 13 | edit Top-1 0.4615; core Top-1 0.7692; nDCG@100 0.6237 | 24952.62 | 2092 to 1888 |

The released oce-client 0.2.0 and stable HTTP API drove both servers with unchanged
truth and case manifests. Both runs reused one physical vector index and separate
SQL/client-state copies. API preflight covered 26 snapshot entries and 9676 distinct
blobs, with zero missing/nonindexed identities. Sync uploaded zero blobs. Index
profile, dense entities (346265) and path entities (57751) remained identical.
Worker, rerankers, rewrite and query-vector cache were disabled. Original SQL and
client states were preserved; both test servers are stopped.

This was one sequential pair with remote embedding. The p50 values are observations;
they do not establish a repeatable speedup. Startup prewarm timeouts and local gRPC
warnings preceded scoring and are recorded in the evidence; API readiness was
confirmed before scoring. Existing project redundancy and SWE edit retrieval limits
remain. Blackbox reports record paths/spans, not bytewise text or scores; frozen
replay supplies that separate evidence.

Scoring used candidate source digest
`d8a90c90b542b7a930ad0ac5b92208d718ea29dcb75166fea75eb75bb547b325`
against archived commit `6e2d815d62787651f81ac8a0e4ac48c0e85724a9`.
The final dotenv interpolation correction and removal of the redundant domain path
limit landed after scoring and have dedicated compatibility tests. Retrieval,
indexing, SQL and runtime orchestration files stayed unchanged after scoring;
the final whole tree was not scored again.

## Remaining boundaries and evidence

Redis can lose a response after moving an unconfirmed delivery to processing; the
existing restart/maintenance recovery owns that case. Recorded symbol counts inherit
the projection's same-chunk/name/enclosing deduplication and describe indexed sites.
Historical chunk-count audit rows cannot be mixed with corrected declaration counts
for calibration.

Evidence is under [the companion directory](reliability-simplification-2026-10-01-data/):
[blackbox summary](reliability-simplification-2026-10-01-data/blackbox-summary.json),
[raw reports](reliability-simplification-2026-10-01-data/blackbox-raw-reports.tar.gz),
[frozen summary](reliability-simplification-2026-10-01-data/frozen-summary.json),
[unit summary](reliability-simplification-2026-10-01-data/unit-summary.json), and
[SHA256 manifest](reliability-simplification-2026-10-01-data/sha256.json).
The archive contains reports and provenance; database files, complete server logs
and credentials are excluded.

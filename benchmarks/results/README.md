# Evaluation archive

The [benchmark guide](../README.md) defines the current suites, truth contracts,
commands and acceptance rules. Reports here preserve dated evidence, including
rejected candidates. Their source revisions, index identities and run-time status
remain historical facts; publication of a report does not adopt its candidate.
An old reference to "current", "foundation", "uncommitted" or "held-out" applies
to that run. Previously exposed held-out manifests are development regression
sets today.

## Recent verification

| Date | Report | What it establishes |
| --- | --- | --- |
| 2026-10-01 | [Reliability fixes and simplification](reliability-simplification-2026-10-01.md) | Persistence/runtime fixes, bounded excerpts, 951 unit passes and 6 integration passes; 327 black-box cases preserve measured quality and returned regions. The report distinguishes the scored source from final CLI/path compatibility edits. |
| 2026-10-01 | [Structural refactor and routing decisions](principled-refactor-2026-10-01.md) | Frozen refactor equivalence and paired retrieval experiments. Chinese flow constructions were adopted; uniform exact RRF and removal of tie rules were rejected. |
| 2026-09-30 | [Runtime ownership](runtime-ownership-2026-09-30.md) | Lifecycle and concurrency changes, frozen retrieval preservation, and the limits of that evidence. |
| 2026-09-13 | [Retrieval refactor verification](retrieval-refactor-2026-09-13.md) | Frozen-corpus equivalence and explicit degraded-lane behavior; preservation rather than a utility gain. |

The reliability report links its sanitized JSON summaries, frozen dumps, raw
report archive and SHA-256 manifest. Comparisons require matching corpus, ordered
cases, truth and physical index state; scores from separate rows in this index
are not automatically comparable. A sequential timing pair alone does not prove
a latency improvement.

## September decisions and experiments

| Date | Report | Recorded scope or decision |
| --- | --- | --- |
| 2026-09-11 | [Version decision](version-decision-2026-09-11.md) | Production selected `33ef932`, with `ac5c9e1` retrieval behavior and shared `1fc581f` repairs. The failed ranking candidate remains on `archive/retrieval-current-20260911`; no fresh validation set was consumed. |
| 2026-09-11 | [Reference region contract v2](reference-region-contract-v2-2026-09-11.md) | Source-use region truth, including uses in the declaration file; a separate development catalog that leaves old short-query truth unchanged. |
| 2026-09-11 | [Reference head experiment](reference-heads-2026-09-11.md) | Catalog gains did not outweigh Bash and qualified-caller regressions; candidate rejected. |
| 2026-09-11 | [Repair isolation](retrieval-isolation-2026-09-11.md) | Shared correctness repairs retained; three stage hypotheses were not adopted. Historical lexical timeouts remained unreproduced. |
| 2026-09-11 | [Retrieval quality recovery](retrieval-quality-recovery-2026-09-11.md) | C6–C12 experiments and failed final quality acceptance; candidates remain historical evidence. |
| 2026-09-10 | [State-machine simplification](state-machine-simplification-2026-09-10.md) | Smaller/faster candidate with head-order and ranking regressions; rejected as a quality-preserving default. |
| 2026-09-10 | [Frozen validation protocol](simplification-validation-protocol-2026-09-10.md) | Selection and exposure rules recorded before inspecting the external batch. |
| 2026-09-10 | [Shared-index control](simplification-index-control-2026-09-10.md) | Source admission, SQL repairs and physical-index controls for that frozen comparison. |
| 2026-09-10 | [Relation follow-up](state-machine-followup-2026-09-10.md) | Test/implementor evidence repairs with repeated results; earlier semantic losses remain visible. |
| 2026-09-10 | [State-machine study](state-machine-2026-09-10.md) | Wording/layout gains and semantic guard failures; unit success did not qualify utility. |
| 2026-09-10 | [Round 4 correction](utility-round4-2026-09-10.md) | Qualified endpoints, bounded startup probes and explicit local index-type changes retained; default FLAT and semantic head changes rejected. |
| 2026-09-09 | [Upstream question supplement](upstream-supplement-2026-09-09.md) | Repeated baseline on adapted TypeScript/Rust questions; exposed IPC and architectural-owner gaps. |
| 2026-09-09 | [Utility round 3](utility-round3-2026-09-09.md) | Per-package paired observations, including negative local-reranker/hub variants. Subsequent retirement and version decisions supersede its feature status. |
| 2026-09-08 | [Utility round 2](utility-round2-2026-09-08.md) | Deterministic waits, reference heads, call chains and budgets, with development and then-unexposed external evidence. |

## Earlier baseline studies

| Date | Report | Recorded focus |
| --- | --- | --- |
| 2026-09-04 | [Head evidence](head-evidence-2026-09-04.md) | Evidence and protected heads. |
| 2026-09-04 | [Relation round](relation-round-2026-09-04.md) | Relation retrieval and returned context. |
| 2026-09-03 | [Nine-language utility](nine-language-utility-2026-09-03.md) | Extraction/routing fixes, query limits, call-hop ablation and API/local reranking. |
| 2026-09-03 | [Black-box baseline](blackbox-baseline-2026-09-03.md) | Initial utility evidence, including Milvus Lite flush and SQLite WAL findings. |
| 2026-09-03 | [Head-order study](swe-explore-development-2026-09-03.md) | Recall versus head quality and reranker observations. |
| 2026-09-02 | [Adaptive rerank study](swe-explore-development-2026-09-02.md) | Repeated issue/routing development comparisons. |

Some early raw result schemas predate the current suite/truth identity contract;
use their reports as narrative history rather than feeding those files to current
`compare` commands. The [Milvus Lite scope sample](milvus-lite-scope-2026-09-01.json)
is a host-specific component diagnostic, not a release threshold or product result.

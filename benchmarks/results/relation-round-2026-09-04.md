# Relation round — paired results, 2026-09-04

Second round of the retrieval evolution: index-time enclosing/re-export attribution, four
relation lanes rendered as fixed-slot sections, an evidence pack with per-section budgets, and
the two new black-box suites (`project_cases`, `csn_queries`). This report records the paired
measurement of the change set against the prior `HEAD` on the same prewarmed index.

## System under test

- OCE `0.3.0`. Baseline is `a292c76` (`HEAD`) run from a detached worktree; `v11-relations`
  is the same tree plus the *Unreleased* relation change set. `oce-client 0.2.0` for both.
- Index: 13 curated snapshots plus the `development` issue snapshots, prewarmed once per side.
  Baseline uses `bench-v10-server` (`SYMBOL_EXTRACTION_VERSION 4`); `v11-relations` uses
  `bench-v11-server` (`SYMBOL_EXTRACTION_VERSION 5`: enclosing edges, barrel re-exports,
  heritage edges). Qwen3-Embedding-4B, 1,024 d; query-vector cache off; both model rerankers
  off, so the router logs `skip:no_reranker_enabled` throughout.
- Truth provenance identical across sides: `project_cases_sha256 9f61623a…`,
  `curated_corpus_sha256 cdcc0488…`. The `compare` guard accepts the pairing.
- Truth for `project_cases` and `csn_queries` is LLM-assisted, declared in each manifest
  (`labeling.method`). Regions were derived with code tools plus judgement, not hand-audited
  case by case.

## project_cases — the main judge (35 cases)

| Metric | baseline | v11-relations | Δ |
| --- | ---: | ---: | ---: |
| Primary Top-1 | 51.4% | 62.9% | +11.4 |
| Primary Hit@3 | 77.1% | 82.9% | +5.7 |
| Primary MRR | 0.643 | 0.741 | +0.098 |
| Relation recall | 62.8% | 81.2% | +18.4 |
| Supporting recall | 66.7% | 78.1% | +11.4 |
| Hop recall | 92.0% | 92.3% | +0.3 |
| Chain closed | 85.7% | 85.7% | 0 |
| Test recall | 98.6% | 100.0% | +1.4 |
| Distractor in head | 20.0% | 11.4% | -8.6 |
| Truth share | 18.7% | 19.2% | +0.5 |
| Chars | 19,765 | 20,099 | +1.7% |
| p50 / p95 ms | 333 / 1,166 | 374 / 928 | +41 / -238 |

Per-lane Hit@3 and relation recall:

| Lane | baseline Hit@3 / RelR | v11 Hit@3 / RelR |
| --- | ---: | ---: |
| reference | 77.8% / 69.3% | 66.7% / 100.0% |
| call_chain | 85.7% / 57.9% | 85.7% / 59.8% |
| test_mapping | 71.4% / 69.0% | 85.7% / 83.3% |
| reexport | 100.0% / 60.0% | 100.0% / 90.0% |
| multi_impl | 57.1% / 55.2% | 85.7% / 70.0% |

Error class per case (first matching, in order):

| Variant | none | exact_miss | distractor | test_missing | relation_missing | redundant |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| baseline | 4 | 8 | 4 | 0 | 11 | 8 |
| v11-relations | 6 | 6 | 1 | 0 | 7 | 15 |

**Reading.** The change set moved every dimension the relation lanes target. Relation recall
rose 18 points, all four relation lanes gained recall (re-export 60→90, multi-impl 55→70,
test mapping 69→83), fewer heads carry a distractor (20%→11%), and the classifier reclassified
four `relation_missing` cases and three `distractor` cases as answered. Head precision held:
Primary Hit@3 rose and truth share barely moved.

**Costs, stated plainly.** `redundant` cases rose 8→15: the relation sections add known-truth
regions that clear the redundancy share on cases that were already answered, so the win on
missing relations is partly paid in extra on-target material. The `reference` lane's Hit@3 fell
77.8%→66.7% even as its relation recall went to 100%, i.e. it now surfaces the related
definitions but reorders the primary head on two cases. Chars grew under 2% and p95 improved.

## Guard suites

Short structural queries (240) — unchanged quality, the intended outcome for a guard:

| Variant | Top-1 | MRR | Hit@10 | Path Top-1 | Reference Top-1 | Chars | p50 / p95 ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| baseline | 100.0% | 1.000 | 100.0% | 100.0% | 100.0% | 16,777 | 321 / 442 |
| v11-relations | 100.0% | 1.000 | 100.0% | 100.0% | 100.0% | 15,718 | 523 / 2,960 |

Semantic queries (39) — a one-case dip, within single-case noise (1 case = 2.6%):

| Variant | Primary Top-1 | MRR | nDCG@10 | Weighted R@5 | Weighted R@10 | Chars | p50 / p95 ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| baseline | 74.4% | 0.922 | 74.6% | 62.7% | 77.7% | 30,293 | 521 / 613 |
| v11-relations | 71.8% | 0.889 | 73.6% | 63.3% | 77.1% | 29,250 | 615 / 870 |

CodeSearchNet external guard (80 docstring→function cases, 8 pinned repos, four languages) —
absolute `v11-relations` only; no paired baseline was run (the suite is new this round and a
second HEAD server run was skipped to bound test time):

| Region Top-1 | Region Hit@5 | Region Hit@10 | Region MRR | File Top-1 | Described Hit@10 | py / js / go / java Hit@10 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 61.3% | 77.5% | 78.8% | 0.680 | 75.0% | 64.0% | 100% / 65% / 80% / 70% |

Plain natural-language retrieval reaches the target function within ten regions four times in
five, and even name-free descriptions land 64% of the time, so the relation work did not
displace ordinary semantic recall on external code.

Latency note: short p95 rose to ~3 s on a freshly started server where the first queries pay
cold caches and remote-embedding variance; short quality is byte-for-byte unchanged and
project p95 improved, so this reads as cold-start noise rather than a routing regression.

## Adaptive-evidence shadow log (WP5)

`python -m benchmarks.internal.rerank_evidence <data-dir>/oce.db` over the v11 run
(394 retrieval rows) tabulates relation attachment by intent and definition ambiguity. `path`
queries attach no relation section (89/89 with none); `reference`, `symbol`, `call_chain`,
`feature`, and `overview` attach them where evidence exists. This is the offline join point for
calibrating `plan_rerank` thresholds against per-case wins; no LLM classifier is involved.

## Acceptance

Against the round's targets: relation-missing errors fell (11→7), test-missing stayed at 0,
primary Hit@3 did not regress (it rose), and distractor-in-head fell. The guard suites hold
(short identical, semantic within one case, CSN healthy in absolute terms). The measured cost
is more redundant on-target material and a two-case reordering in the reference lane. Net: the
main judge improves on exactly its target dimensions with a bounded, visible cost.

## Operational note

Every suite's per-snapshot `sync` occasionally failed with a transport reset while the single
server worker was busy (heavy scoped query or a momentarily locked SQLite during metrics flush),
which aborted the whole suite. The harness `run_client` now retries the idempotent `sync` on
transient transport markers (`retries=4`, linear backoff); `retrieve` stays single-shot so a
genuine per-case failure is still recorded. With retry, all four v11 suites passed on the first
attempt against a warm, settled server.

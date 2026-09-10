# Upstream question supplement: initial utility baseline

Twelve adapted questions expose useful gaps in the current retrieval behavior.
Across two identical-config runs, only one of four IPC chains returns every
labeled hop, and only two of six semantic queries put a primary implementation
file first. All per-case quality metrics are identical between runs. These are
development baseline observations on one mixed TypeScript/Rust project, not an
accuracy improvement, a broad product estimate, or an upstream comparison.

## Scope and provenance

- Server source: `3b0f74357d9f3e69a6c3586e6b467ed16e90cdf7`. This change adds
  benchmark data and documentation; retrieval implementation and scoring are
  unchanged by this task. The runtime records a dirty harness workspace; see
  the concurrent-edit note below when reproducing this baseline.
- Question source: `oce-ai/oce-benchmark` commit
  `d4f10554a18e31599d1e46d5d56da6588d4aa86c`,
  `benchmarks/cc-switch-retrieval-benchmark.jsonl`. Its SHA-256 and each question
  ID/adaptation are recorded in the manifests.
- Source snapshot: `farion1231/cc-switch` commit
  `40cac1a68edf8c9e7b3a89125cf40bb93a348404`. The released `oce-client 0.2.0`
  admitted 1,030 files (16,498,946 content bytes), including all 29 truth files.
  Prewarming uploaded all 1,030 blobs through the stable API in 85 batches.
- Labels were derived from source before retrieval, with LLM assistance and no
  independent human review. Both query wording and answer ownership were
  reviewed; the upstream file lists were not copied as authoritative answers.
  The question/corpus digests remained unchanged throughout all four runs.
- Runtime: Qwen3-Embedding-4B, 1,024 dimensions, compatible existing index
  profile `2c7e083e241a59b6c3546a5eb7d802daf936838fb3aac069e743062a420eb6a8`;
  query-vector cache off, both model rerankers off, query rewriting off.
  Retrieval settings use the current defaults. The index is `bench-v11-server`,
  extended with this snapshot; each request is scoped by the released client.
- Runs finished at 2026-09-10 03:36–03:37 UTC (September 9 in the workspace time
  zone), after prewarming. All 24 requests completed successfully. This is two
  repeated observations of one implementation, not a before/after experiment.

A separate source edit appeared in the shared workspace during the first run
(`symbol_provider.py`, observed mtime 03:36:38 UTC); additional source and index
profile edits appeared at 03:38:41, after all runs. The baseline server started
at 03:24:58 without `--reload`, had already indexed the snapshot, and retained
its loaded implementation. All four client syncs uploaded zero blobs, and all
four reports capture the same live compatible index fingerprint. Reproduce
with the pinned commit plus these manifests in a clean checkout, not the
subsequently modified working tree.

## Utility results

Rates and recalls below use the existing evaluators' per-case macro averages.
Chain coverage is restricted to the four `call_chain` cases; the two reference
cases must not inflate its denominator. There are no test-mapping cases.

| Metric | Run 1 | Run 2 |
| --- | ---: | ---: |
| Relation primary Top-1 / Hit@3 | 5/6 | 5/6 |
| Primary region recall | 72.2% | 72.2% |
| Relation recall | 81.9% | 81.9% |
| Supporting region recall | 100% | 100% |
| Call-chain hop recall | 72.9% | 72.9% |
| Call chains with every labeled hop present | 1/4 | 1/4 |
| Relation cases with a labeled distractor in top 3 | 1/6 | 1/6 |
| Semantic primary file Top-1 | 2/6 | 2/6 |
| Semantic nDCG@10 | 35.1% | 35.1% |
| Semantic weighted recall@10 | 47.8% | 47.8% |

The two reference cases retrieve their required regions somewhere in the full
response, but only one leads with the correct region. For the four chains,
finding the Rust endpoint produces 4/4 primary Hit@3 despite missing frontend
steps in three cases. That is why head hit rate alone is insufficient.

### Relation cases

The following findings occur in both runs. Ranks refer to returned regions.

| Upstream ID / adapted case | Hit@3 | Hop coverage | Observation |
| --- | ---: | ---: | --- |
| Q41 / `cc-switch-add-provider-ipc` | Yes | 1/2 | Rust handler and Tauri registration are present; `providersApi.add`'s invoke at `src/lib/api/providers.ts:63` is absent. |
| Q42 / `cc-switch-auth-login-ipc` | Yes | 2/2 | Frontend invocation, registration, and Rust dispatcher are all present. |
| Q46 / `cc-switch-delete-profile-ipc` | Yes | 3/4 | Mutation, API wrapper, and Rust handler are present; the confirmation callback at `ProfileManageDialog.tsx:68–72` is absent. |
| Q47 / `cc-switch-enable-prompt-ipc` | Yes | 2/3 | API wrapper and Rust handler are present; both `toggleEnabled` invocation regions are absent. The third result is the right hook file but lines 116–161, stopping before the required branch call. |
| Q70 / `cc-switch-first-run-settings-use` | Yes | N/A | The first-run component is first, but the general settings hook `src/hooks/useSettings.ts` is second despite the question's component constraint. |
| Q73 / `cc-switch-session-search-use` | No | N/A | `SessionManagerPage.tsx` is first at lines 842–907; the requested use site at lines 243–250 appears only at region rank 21. A file-only metric would conceal this head failure. |

The existing dominant error classifier reports four `redundant`, one
`distractor`, and one `exact_miss`. Its `relation_missing` branch checks
supporting recall, so it does not classify the absent primary frontend hops as
relation misses here. Read hop coverage and missing regions alongside that
label; do not interpret zero `relation_missing` labels as complete chains.
Likewise, the 12.9% truth-region share uses narrow labeled call sites and counts
regions, not useful characters.

### Semantic cases

Ranks below are unique-file ranks, as used by the semantic evaluator. “Absent”
means absent from the full returned file list in both runs. These metrics judge
file ownership, not whether the returned excerpt explains the implementation.

| Upstream ID / adapted case | nDCG@10 | Primary owner result |
| --- | ---: | --- |
| Q10 / `cc-switch-auto-launch` | 95.6% | `auto_launch.rs` is first; settings commands are third. |
| Q26 / `cc-switch-webdav-scheduling` | 10.6% | Upload transport is first, but `webdav_auto_sync.rs` and shared trigger policy are absent. |
| Q76 / `cc-switch-endpoint-speedtest` | 10.2% | `speedtest.rs` is absent; provider commands are ninth. The response starts with stream-check code. |
| Q81 / `cc-switch-proxy-error-response` | 45.0% | `error_mapper.rs` is fourth and the actual response constructor in `error.rs` is fifth. |
| Q83 / `cc-switch-provider-failover` | 49.3% | `forwarder.rs` is first; the switch manager, ordered router, and hot-switch service are absent. |
| Q91 / `cc-switch-sync-protocol` | 0.0% | Shared protocol and both transport consumers are absent; settings UI leads. |

## Characters and latency

These are observations after indexing, with no concurrent benchmark requests.
Two small sequential runs against a remote embedder do not establish a
performance gain. The initial corpus indexing time is excluded.

| Suite | Mean characters, run 1 / 2 | p50 ms, run 1 / 2 | p95 ms, run 1 / 2 |
| --- | ---: | ---: | ---: |
| Relation | 25,303 / 25,012 | 1,313 / 1,268 | 3,813 / 1,336 |
| Semantic | 27,954 / 27,954 | 1,307 / 1,265 | 1,403 / 1,355 |

Q41 changed response length while its quality metrics stayed identical. Do not
use that or the faster second-run tail as a quality or speed improvement.

## What to investigate next

Prioritize the observed utility failures: correct use-site regions in the
head, frontend steps missing from otherwise successful IPC retrieval, and
semantic queries that return UI/transport neighbors while omitting the actual
backend implementation. This baseline establishes symptoms, not their internal
causes. Investigate routing, recall, selection, and expansion before choosing a
production change; avoid project-specific path boosts or query exceptions.

Use this supplement alongside the existing project/semantic and public guards
when testing those changes. Optimize latency after measurable utility gains,
while continuing to report output size and time separately. These twelve
source-reviewed questions are now a development set, not a sealed test or
evidence of agent task success.

## Reproduction and validation

See the [supplement commands](../README.md#upstream-question-supplement).
Both manifest checks passed, as did all ten focused project/semantic/boundary
unit tests. Whole-repository Ruff checks passed before the runs. At final
verification, the concurrent source/test edits produced a Ruff undefined-name
error and two formatting failures outside `benchmarks`; this is not a clean
whole-tree validation. The final checks of this task's scope passed:
`ruff check benchmarks`, `ruff format --check benchmarks`, the same ten
benchmark tests, and `git diff --check -- benchmarks`. The original suite
manifests and evaluator implementations are unchanged.

Truth SHA-256:

```text
upstream_corpus.json         7ad6ae15760dbbd0a04e5f5767b6c58883e62e898be33b36d82504aba77167b4
upstream_project_cases.json  9aeeccf1d4ef8695520b98a864ef12d1107265000e12a9644feb0ccb336e9084
upstream_semantic_cases.json 0d80f2e855b863eb495c52b137d6608f7a53f1fdfced4d033fb9d1804e4a6abc
```

Raw reports are local artifacts under
`~/.cache/oce/bench-runs/upstream-adoption/`. They include per-case retrieved
regions, scores, runtime/index metadata, and client binary digest. SHA-256:

```text
baseline-1-project_cases.json  fe0591fae47ad2f5fe2c8006c8f397142aa9447ac954bac7eb2b97da26bb4fdf
baseline-2-project_cases.json  c290293b4a0a356ce1e18f363300bbfce0a28ca7388bdb38245169f73f5e0a57
baseline-1-semantic_cases.json 092e477f1564cbbe35db1375b31b5fe0146eec58982d0faa19d02f4895abdd83
baseline-2-semantic_cases.json f5a2dd049ff2399b2de982c8df64730d1ad4696b5f1b31b4cd764d6adac71e2a
```

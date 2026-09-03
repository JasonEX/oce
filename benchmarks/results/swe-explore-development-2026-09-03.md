# Head-order utility study — 2026-09-03

This is a development observation, not a release gate. It first evaluates the production OCE
server through the production Rust client on one prewarmed personal-mode index with every model
reranker disabled, then reruns the selected deterministic baseline with the dedicated reranker
and the optional chat cascade.

The study follows the September 2 lexical-fusion regression: that change lifted Edit file
Recall@10 from 30.8% to 65.4% while dropping short-query Symbol Top-1 from 100% to 65% and
nDCG@100 from 28.0% to 21.9%. This round kept the recall and repaired the head of the list.

## System under test

- OCE `0.3.0`, `5e51b04` plus the uncommitted change set documented in the changelog under
  *Unreleased*; `oce-client 0.2.0`.
- One prewarmed personal-mode index shared by every run: 6,788 tracked text blobs, 15,968
  chunks, 89,327 symbol occurrences across the 13 `development` snapshots. No index version
  changed, so every variant reused the same vectors, chunks, and symbol rows.
- Qwen3-Embedding-4B, 1,024 dimensions; process-local query-vector cache disabled.
- In the deterministic section, `RERANK_ENABLED=false` and `LLM_RERANK_ENABLED=false`; every
  retrieval route reads `skip:no_reranker_enabled`. The later model section states its own
  reranker configuration. Query embeddings remain enabled in every variant.
- Query rewrite disabled; semantic chunking, exact recall, intent-routed lexical recall, path
  recall, path lookup, source priority, coverage selection, adjacent merging, and budgeted
  related definitions enabled.

## Query sets

- 13 issue-resolution queries from the deterministic `development` slice of SWE-Explore,
  scored by the checksum-pinned official evaluator. All 13 are classified `compound`.
- 96 short queries expanded from 16 reviewed definition anchors in the same snapshots: 10
  classes and 6 module-level `snake_case` functions, each asked as a symbol, path, and
  reference question in English and Chinese. The six function anchors were added this round
  because single-word class names cannot exercise whole-identifier matching, and a
  `definition_top1` diagnostic now separates "answered with the declaration" from other
  reference misses.

Intermediate issue variants and the `v5` model candidate were repeated twice. The final
`reviewed` boundary run was repeated once: its no-reranker order is deterministic, and its
dedicated-reranker issue metrics matched both preceding `v5` repeats.

## Variants

| Variant | Change relative to the previous row |
| --- | --- |
| `parent` | `5a57ef4`: dense + exact, no lexical recall |
| `fusion-fix` | `5e51b04`: lexical recall, fixed symbol/path head slots, focused budget, budgeted related definitions |
| `v2-heads` | reference lexical recall gated on the whole identifier; SQL lanes start before the embedding round trip; source head slots and reference use-site head (inactive on issues, see below) |
| `v3-prior` | source prior stays active for compound issue text that only mentions file names; test-neutral prior limited to short questions; reference head excludes the declaring file |
| `v4-prior2` | source prior extended to change logs, `doc/`, `examples/`, configuration files, `.pyi` stubs, `__init__.py` barrels; root `README` only keeps full weight |
| `reviewed` | root `README` keeps its multiplicative weight but cannot consume an implementation head slot; reference heads require exact or whole-identifier lexical occurrence evidence |

`v2-heads` changed nothing on the issue set because every issue text triggered the path index
heuristic, which switched the prior to neutral and skipped the source head. A targeted
ablation made that visible; `v3-prior` is the fix.

## Issue-resolution retrieval (13 issues, official SWE-Explore metrics)

| Variant | Edit Top-1 | Core Top-1 | Edit file R@10 | Core file R@10 | nDCG@100 | nDCG@500 | First useful hit | Ctx efficiency | p50 ms | p95 ms | Chars |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `parent` | 7.7% | 38.5% | 30.8% | 43.8% | 28.0% | 37.6% | 50.0% | 20.9% | – | – | 22,936 |
| `fusion-fix` | 15.4% | 23.1% | 65.4% | 57.3% | 23.9% | 44.2% | 66.2% | 23.5% | – | – | 27,081 |
| `v2-heads` | 15.4% | 23.1% | 65.4% | 57.3% | 23.9% | 44.2% | 66.2% | 23.5% | 1,001 | 2,399 | 27,081 |
| `v3-prior` | 23.1% | 30.8% | 65.4% | 59.2% | 34.5% | 56.6% | 73.8% | 24.2% | 987 | 2,485 | 24,209 |
| `v4-prior2` | 30.8% | 53.8% | 69.2% | 64.4% | 53.0% | 66.6% | 79.2% | 26.4% | 1,003–1,023 | 2,552–3,490 | 23,412 |
| `reviewed` | 38.5% | 61.5% | 69.2% | 64.4% | 60.7% | 68.7% | 80.0% | 26.4% | 1,079 | 2,378 | 23,412 |

Against `parent`, `reviewed` raises every head-of-list and recall metric while returning
about the same number of characters. Against the September 2 model study, its nDCG@500 of
68.7% sits above the dedicated Qwen3-Reranker-0.6B variant (61.7%, 4.4 s mean) and below the
dedicated → chat cascade (81.1%, 10.1 s mean), with no reranker call and a 1.0 s median.

Per-stage audit for issue queries: query embedding ≈ 800 ms, lexical FTS ≈ 290 ms, dense
≈ 100 ms, path index ≈ 70 ms. Starting the SQL lanes before the embedding round trip moved
the median from about 1.29 s to about 1.0 s. The p95 is the longest issue text (24.8K
characters) and is embedding-bound.

## Short-query routing (96 queries)

| Variant | Top-1 | MRR | Symbol Top-1 | Path Top-1 | Reference Top-1 | Ref def-first | Hit@10 | p50 ms | p95 ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `fusion-fix` | 76.0% | 0.850 | 100% | 100% | 28.1% | 53.1% | 100% | 356 | 599 |
| `v2-heads` | 78.1% | 0.864 | 100% | 100% | 34.4% | 40.6% | 100% | 321 | 508 |
| `v3-prior` | 86.5% | 0.917 | 100% | 100% | 59.4% | 0.0% | 100% | 325 | 593 |
| `v4-prior2` | 92.7% | 0.954 | 100% | 100% | 78.1% | 0.0% | 100% | 312 | 540 |
| `reviewed` | 96.9% | 0.977 | 100% | 100% | 90.6% | 0.0% | 100% | 356 | 540 |

The original 60-query set (10 class anchors) read Top-1 60.0% / Reference Top-1 0% on
`parent` and 78.3% / 35% on `fusion-fix`; it is not directly comparable with the 96-query
rows because `compare` refuses mismatched case sets.

Reference truth is every non-test Python file that names the identifier, excluding the
declaring file. On `reviewed`, the three remaining misses are the English `Blueprint` query
and both `Parser` variants; all still hit a truth file within the first seven positions.
English and Chinese aggregate Top-1 are 95.8% and 97.9%, respectively.

## Dedicated reranker on the new baseline

The September 2 study measured Qwen3-Reranker-0.6B against the pre-fusion pipeline. It was
rerun on `v4-prior2` with `RETRIEVAL_RERANK_POLICY=adaptive`, 50 candidates, and the default
instruction. The first run exposed two behaviours of the small cross-encoder: it led with
tests, change logs, and issue templates on 8 of 13 issues, and it answered 84% of reference
questions with the symbol's declaration. One issue text of 24.8K characters also made a
single reranker call take 14.7 s, because a cross-encoder re-reads the query for every
candidate. `v5` reapplies the source and reference head tiers after the model runs (keeping
the model's order inside each tier) and caps the query sent to the reranker at 2,400
characters (`RERANK_MAX_QUERY_CHARS`).

| Variant | Edit Top-1 | Core Top-1 | Edit file R@10 | Core file R@10 | nDCG@100 | nDCG@500 | First useful hit | Ctx efficiency | p50 ms | p95 ms | Chars |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `reviewed`, no reranker | 38.5% | 61.5% | 69.2% | 64.4% | 60.7% | 68.7% | 80.0% | 26.4% | 1,079 | 2,378 | 23,412 |
| `v4-prior2` + 0.6B adaptive | 30.8% | 61.5% | 84.6% | 83.8% | 43.3% | 70.7% | 90.8% | 29.7% | 2,949 | 17,329 | 28,004 |
| `reviewed` + 0.6B adaptive | 61.5% | 92.3% | 84.6% | 79.7% | 86.4% | 93.2% | 99.2% | 28.4% | 3,026 | 4,537 | 27,991 |

| Variant | Top-1 | MRR | Reference Top-1 | Ref def-first | Skip rate | p50 ms | p95 ms | Rerank ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `reviewed`, no reranker | 96.9% | 0.977 | 90.6% | 0.0% | 100% | 356 | 540 | – |
| `v4-prior2` + 0.6B adaptive | 71.9% | 0.821 | 15.6% | 84.4% | 66.7% | 364 | 1,245 | 737 |
| `reviewed` + 0.6B adaptive | 100% | 1.000 | 100% | 0.0% | 66.7% | 422 | 1,333 | 776 |

With the tiers enforced, the 0.6B reranker reaches nDCG@500 93.2% and first useful hit
99.2% on the 13 issues, above the September 2 dedicated → chat cascade (81.1% at 10.1 s) at a
median of about 3.0 s with no chat tokens. Reranker time per issue is now 1.0–2.4 s and no
longer depends on issue length. Symbol and path queries still skip the model entirely.

### Adding the chat-LLM cascade on top

The immediately preceding `v5` candidate was also run with `LLM_RERANK_ENABLED=true`
(deepseek-v4-flash, 20 candidates, adaptive, 15 s end-to-end deadline) behind the dedicated
reranker, twice. Its dedicated-only issue metrics match `reviewed`; this optional cascade was
not rerun after the final reference-head boundary because all issue queries are compound.

| Variant | Edit Top-1 | Core Top-1 | nDCG@100 | nDCG@500 | First useful hit | p50 ms | p95 ms | Chat calls completed | Chat tokens/run |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| preceding `v5` + 0.6B adaptive | 61.5% | 92.3% | 86.4% | 93.2% | 99.2% | 3,011 | 4,658 | 0/0 | 0 |
| preceding `v5` + 0.6B + chat cascade (×2) | 76.9% | 100% | 94.4% | 94.8% | 100% | 17,517–17,627 | 19,669–19,862 | 6/13 | 113.6K |

Only 6 of 13 chat calls completed inside the 15 s deadline in each repeat; the other 7 hit
the deadline and kept the dedicated order, so the reported gain comes from six queries. The
median request time rose from about 3 s to about 17.5 s. The routing set was unchanged
(chat adaptive makes no calls on symbol, path, or reference queries).

## Interpretation and decision

- Keep the deterministic routing, prior, bounded-head, SQL-overlap, and query-cap changes as
  defaults. The deterministic comparison improves every reported head-of-list metric without a
  reported recall regression and returns about the same or less context; SQL overlap lowers the
  measured median relative to the sequential ablation, while the query cap removes the
  reranker's issue-length latency spike.
- The mechanism is consistent with the project rule that new evidence enters as a lane: the
  source head and the reference use-site head are bounded slot rules, not score mixing. A
  reranker is trusted within each evidence tier; it cannot erase the tier boundary.
- The source prior extension is the single largest contributor on these issues (Core Top-1
  30.8% → 53.8% before the final boundary review). On these snapshots, change logs,
  configuration, stubs, and barrels describe code more often than they implement the fix.
- `RETRIEVAL_SOURCE_HEAD_SLOTS=0` and `RETRIEVAL_SOURCE_PRIORITY_ENABLED=false` remain
  available for ablation.
- Do not recommend the chat-LLM cascade for interactive use on this baseline. It adds 1.6
  points nDCG@500 for a sixfold latency increase, more than half of its calls time out, and
  it consumes about 114K chat tokens per 13 queries. The dedicated reranker now captures
  nearly all of the ranking gain the cascade used to provide.
- The dedicated 0.6B reranker stays the recommended interactive opt-in. On this baseline it
  adds about 1.9 s median latency for roughly +25 points nDCG@500 and +19 points first useful
  hit on issues, and +9 points reference Top-1 on short queries. The head tiers must stay
  applied after the model; without them it regressed both sets. `RERANK_ENABLED` remains a
  data-egress authorization and stays off by default.

## Limits

Thirteen Python issues from five repositories and sixteen anchors from the same snapshots.
Every variant used one embedding model and one provider. Reference truth is file-level and
lexical. The benchmark measures retrieval output, not downstream patch success. The chat-LLM cascade
was rerun with one chat model and one provider; its timeout rate is provider-dependent.
Raw JSON, service logs, and SQLite state remain under `~/.cache/oce`.

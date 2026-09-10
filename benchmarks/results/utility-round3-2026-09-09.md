# Utility round 3 — paired results, 2026-09-09

Fourth round of the retrieval evolution, aimed at the vector-backed intents after round 2 had
saturated the deterministic ones. Six work packages were planned: a sealed held-out set for the
intents this round touches, reranker routing, issue-style heads, an entry-point lane for overviews,
redundancy, and the defects the round-2 diffs had exposed. Every package was measured as its own
paired cycle on the same index; two of them (the local reranker, the hub lane) measured negative
and ship off. The reviewed final tree was run across every guard and both held-out sets.

## System under test

- Baseline `r3base` is `HEAD b549f66` (`feat(retrieval): improve deterministic relation retrieval`)
  run from a clean tree on 2026-09-09 01:44. The pre-review candidate `r3f` is the same tree plus this
  round's change set, run 2026-09-09 04:50 after five intermediate cycles (`r3a` … `r3e`) that each
  isolated one change. `r3-reviewed` is the final tree after code-review fixes, run at 17:54–18:01;
  the tables below use that final run. Quality is paired with the same baseline; latency is shown as
  an observation because the remote provider's hour changed.
- Index: `bench-v11-server` (13 curated snapshots, 8 CodeSearchNet snapshots, the `development`
  issue snapshots, the four round-2 held-out snapshots). No index version bump and no migration in
  this round, so the HEAD tree opens the same database for the held-out baseline.
- Qwen3-Embedding-4B (1,024 d) remote embedder, query-vector cache off, both model rerankers off
  except in the `r3rr` ablation, audit text on, `oce-client 0.2.0`.
- Truth digests identical across sides: `project_cases 9f61623a…`, `semantic_cases aeb73cbe…`,
  `heldout_cases 94b373d4…`, `heldout_corpus 7aca5fcb…`. The new held-out semantic manifest
  `heldout_semantic_cases.json` is `32e93166…`; it was written from a read of the four repositories
  before any round-3 change was measured, run once per side, and never compared against retrieval
  output while the changes were being made.
- Same-config noise from round 2 still applies: semantic nDCG@10 moves on about 15 of 39 cases
  between identical runs (±0.5 points), CSN Region Hit@10 is byte-stable, the short suite is
  byte-identical.

## What changed (each change measured as its own cycle)

1. **Issue-style requests are anchored on their deterministic facts.** Every traceback frame
   (Python, IPython `File path:line, in Func`, Node) is resolved to the declaration of that function
   in that file whose span contains the frame's line (`BaseAdapter.send` vs `HTTPAdapter.send`);
   the title's identifiers are resolved with their qualifier pinned strictly (`unittest.skip` names
   the standard library and anchors nothing). Those declarations take protected head slots in trace
   order (`RETRIEVAL_COMPOUND_ANCHOR_SLOTS`, 0 → 3). The remaining declarations of an issue's many
   identifiers join fusion by rank instead of outbidding the fused order by their raw exact score.
2. **Qualified names are pinned by structure first.** The recorded enclosing declaration decides
   (`route` inside `Router`), then a declaration line naming both scope and leaf
   (`app.render = function render`), then the chunk text; a path component must equal the qualifier
   whole, so `test/app.render.js` no longer passes for the scope `app`. Use-site batches are pinned by
   structure or text but never by the declaration stage, and a qualified reference orders chunks that
   name the qualifier ahead of bare mentions.
3. **Overloads are ordered by their parameter list only.** The signature window used to take three
   lines and read the body's first statement, which tied `fromJson(Reader, TypeToken)` with
   `fromJson(JsonReader, TypeToken)` through `JsonReader jsonReader = …`.
4. **Test questions lead with the test named after the symbol.** Among test files with the same path
   evidence, the chunk whose declared test name is closest to the symbol (`TestWalker` for `Walk`)
   comes first, then call sites, then mentions, then module headers. Fused file order only breaks
   ties inside one evidence tier. A first attempt ("earliest chunk in the file") put two import
   headers in the head and was replaced before shipping.
5. **Redundancy.** Callers exclude test files (the test section shows them); related definitions
   never come from test files; a symbol answer appends one test excerpt; the reference answer appends
   its own resolved declaration instead of re-querying under the ambiguity bound that dropped
   `render`; vendored directories and `*.config.*` files take the supporting-material prior.
6. **A reference answer decided by SQL use sites skips adaptive dedicated reranking**
   (`rerank_route = skip:deterministic`; symbol/path retain their existing structural skip routes,
   and the `always` policy still runs).
7. **Hub lane** (`RETRIEVAL_HUB_HEAD_SLOTS`), **shipped off**: see the ablation below.

Defects found by reading per-case diffs during the round, each reverted or fixed before the final
cycle: the "earliest chunk" test rule (import headers led), a recursion guard on callers that dropped
`Command.invoke` → `ctx.invoke` from the held-out click chain, a `LIMIT` on the package-name lookup
that let `pylint/lint` pass as a declaration, and a 900-spelling `IN` list that made SQLite take
three seconds until the spellings were prefiltered through the unscoped identifier index.

## project_cases — the main judge

| Metric | base | round 3 | Δ |
| --- | ---: | ---: | ---: |
| Primary Top-1 | 80.0% | 88.6% | +8.6 |
| Primary Hit@3 | 94.3% | 97.1% | +2.8 |
| Primary MRR | 0.867 | 0.929 | +0.062 |
| Relation recall | 91.5% | 94.3% | +2.8 |
| Supporting recall | 93.8% | 93.8% | 0 |
| Chain closed | 94.3% | 94.3% | 0 |
| Test recall | 100.0% | 100.0% | 0 |
| Distractor in head | 2.9% | 2.9% | 0 |
| Truth share | 31.7% | 32.2% | +0.5 |
| Chars | 19054 | 18803 | -251 |
| p50 ms | 580 | 565 | -15 |
| p95 ms | 2953 | 3328 | +375 |

Per lane (Hit@3 / relation recall): reference 100/100 → 100/100, call_chain 100/86.0 → 100/86.0,
test_mapping 85.7/92.9 → 85.7/92.9, reexport 100/90.0 → 100/90.0, multi_impl 85.7/85.7 → 100/100.

Error classes: none 12 → 13, exact_miss 2 → 1, distractor 1 → 1, relation_missing 2 → 2,
redundant 18 → 18.

Per-case changes:

- `gson-fromjson-jsonreader-overload` (multi_impl): Top-1 0→1, Hit@3 0→1, relation recall 0→1,
  exact_miss→redundant (the parameter-list signature window)
- `express-app-render-callers` (reference): Top-1 0→1; the declaration `app.render = function
  render` is now the pinned declaration, `res.render`'s call to `app.render` leads, `view.js` (a
  must-not file) left the head
- `axum-router-route-chain` (call_chain): Top-1 0→1; `route` resolves to the declaration enclosed by
  `Router`, not to `Resource::route` in `axum-extra`
- `flask-wsgi-dispatch-chain` (call_chain) had distractor head 1→0 in `r3f`, but the reviewed run
  returned the baseline order and 24,976 characters. The vector-backed tail is not stable enough to
  count that single-cycle improvement; the final error class remains `distractor`.
- `requests-get-to-adapter-send` (call_chain): redundant→none
- `requests-prepared-request-reexport` (reexport): truth share 0.17→0.22, chars 7728→7149 (one
  test excerpt instead of three, no fixture from `conftest.py` among related definitions)
- `pytest-approx-tests` (test_mapping): truth share 0.46→0.36; still `exact_miss`, its labelled
  region uses `approx` only through a fixture and carries no occurrence of the name

## Guard suites

### short_queries (240)

| Variant | Top-1 | MRR | Hit@10 | Chars | Hits | p50 | p95 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| base | 100.0% | 1.000 | 100.0% | 11244 | 9 | 315 | 1071 |
| round 3 | 100.0% | 1.000 | 100.0% | 11068 | 8 | 296 | 932 |

Quality byte-identical; the symbol answers carry one test excerpt instead of three.

### semantic_queries (39)

| Variant | Primary Top-1 | MRR | nDCG@10 | W-R@10 | Feature | Overview | Call-chain | Chars | p50 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| base | 74.4% | 0.873 | 74.3% | 79.8% | 73.5% | 67.8% | 81.7% | 30092 | 1895 |
| round 3 | 74.4% | 0.873 | 74.1% | 79.1% | 72.8% | 67.8% | 81.7% | 30022 | 1485 |

One case moved (`bats-test-execution` 0.45→0.36, identical head, a lower position shifted), inside
the measured same-config noise.

### csn_queries (80)

| Variant | Region Top-1 | Region Hit@10 | MRR | File Top-1 | Described Hit@10 | Chars | p50 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| base | 62.5% | 83.8% | 0.720 | 75.0% | 76.0% | 24470 | 1768 |
| round 3 | 62.5% | 83.8% | 0.720 | 75.0% | 76.0% | 24582 | 1408 |

All scored quality fields are identical per case.

### swe_explore development (13)

| Variant | Edit Top-1 | Core Top-1 | Edit file R@10 | Core file R@10 | nDCG@100 | nDCG@500 | First hit | Chars | p50 | p95 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| base | 30.8% | 69.2% | 80.8% | 71.7% | 62.5% | 78.1% | 89.3% | 26225 | 1957 | 3226 |
| round 3 | 38.5% | 84.6% | 76.9% | 64.0% | 70.2% | 83.6% | 91.6% | 25320 | 1823 | 2485 |

Per-issue: `pydata__xarray-7233` Edit Top-1 0→1, Core Top-1 0→1, nDCG@100 0→1.00 (the title's
`ds.Coarsen.construct` pins `construct` to `Coarsen` and its declaration leads);
`pydata__xarray-6721` Core Top-1 0→1 (IPython frames: `Dataset.chunks`, `get_chunksizes`,
`Variable.data`); `psf__requests-1724` nDCG@500 0.25→0.63 (frames `api.request`, `Session.request`,
`HTTPAdapter.send`) with core file R@10 0.75→0.50; `pylint-dev__pylint-6528` core file R@10
0.75→0.50; `pytest-dev__pytest-10081` core file R@10 0.50→0.00 (its title's `unittest.skip` now
anchors nothing, and rank fusion moved `test_unittest.py` out of the first ten).

**Cost, stated plainly.** Core file recall at ten fell 71.7%→64.0% on this suite: the anchor slots
hold frames that are part of the failing path but not always of the truth's core set, and they push
the vector tail down by up to three positions.

## Latency judge

Per-intent stage means from `retrieval_metrics`, both cycles the same day (the baseline hour was
slower at the provider: 48 of 60 feature requests took over 1.5 s against 5 of 60 later, so the
absolute totals are not a code effect; the shape is).

```
base                                          round 3
intent      n  slow  embed  total  dense      intent      n  slow  embed  total  dense
call_chain  21   16    419   1941    151      call_chain  21    5    375   1347     59
compound    33   26   1168   2182    175      compound    33   12    738   1397    103
feature     60   48    404   2132     52      feature     60    5    301   1198     27
overview    13   11    369   1842     49      overview    13    0    296   1237     33
path        89    0    392     90     50      path        89    0    346     70     28
reference   97    3    432    766     40      reference   97    0    372    577     26
symbol      94    1    345    345     39      symbol      94    0    310    274     20
```

The deterministic intents behave as in round 2 (path ~70 ms, symbol ~270 ms, reference ~580 ms,
none over 1.5 s). The hub lookup, when enabled, adds about 150 ms of SQL that overlaps the embedding
round trip; with the lane off it costs nothing.

## Held-out — sealed paired runs

`HEAD` (b549f66) was served from a detached worktree against the same index; the final tree was run
afterwards with the same manifests and repeated after review.

### Relation set (round 2's 24 sealed cases, now a second use)

| Metric | HEAD | round 3 |
| --- | ---: | ---: |
| Primary Top-1 | 83.3% | 87.5% |
| Primary Hit@3 | 91.7% | 95.8% |
| Primary MRR | 0.876 | 0.914 |
| Relation recall | 97.6% | 97.6% |
| Distractor in head | 0.0% | 0.0% |
| Truth share | 31.8% | 31.9% |
| Chars | 17786 | 17471 |
| p50 ms | 284 | 359 |

test_mapping Hit@3 80.0%→100%; error classes none 13→15, exact_miss 2→1, redundant 8→7. Per case:
`chi-walk-tests` Top-1/Hit@3 0→1 (`TestWalker` leads `tree_test.go`), `click-parse-args-definitions`
redundant→none, three multi-impl/test cases lost or gained a few points of truth share as their
relation sections changed size.

### Semantic set (18 new sealed cases: 6 feature, 4 overview, 4 call-chain, 4 issue)

| Metric | HEAD | round 3 |
| --- | ---: | ---: |
| Primary Top-1 | 66.7% | 66.7% |
| nDCG@10 | 74.4% | 74.4% |
| Feature / Overview / Call-chain / Issue nDCG | 62.3 / 66.4 / 85.6 / 89.3 | 62.3 / 66.4 / 85.6 / 89.3 |
| Chars | 25988 | 25883 |
| p50 ms | 1233 | 1488 |

Scored quality is identical per case: the shipped change set does not touch feature/overview
requests and the four
issue-style requests carry no traceback and no title identifier the anchor rule accepts. This set is
where the hub lane failed (below); it is the reason the lane ships off.

## Ablations

### Hub lane (`RETRIEVAL_HUB_HEAD_SLOTS=2`, cycles `r3b`/`r3d`)

The lane joins the request's words into the identifier spellings a declaration could use
(`Router`, `register_checker`, `createSlice`), fetches the scope's declarations of those spellings
with the number of files that call, import or extend each, and gives the most widely referenced
non-package names protected head slots. On the curated semantic suite it is the largest single move
of the round: overview nDCG@10 67.8→74.3 (`pytest-startup-collection-overview` 0.35→0.85,
`axum-handler-overview` 0.24→0.59, `gin-request-overview` 0.60→0.93, `jq-pipeline-overview`
0.48→0.78; `pylint-run-overview` 0.97→0.59 and `express-architecture-overview` 0.72→0.51 against),
call-chain 81.7→84.1. Enabled for feature questions too it displaced the implementing function
(feature 73.5→68.8, CSN Region Top-1 62.5→45.0 before a question-form gate, 60.0 after), so the
feature switch was already off. On the sealed held-out semantic set with the lane on: overview
66.4→54.1, call-chain 85.6→78.2, feature and issue unchanged (`jsoup-parsing-overview` 0.90→0.61,
`jsoup-connect-get-chain` 0.76→0.55, `chi-routing-overview` 0.74→0.67, `click-arg-parsing-overview`
0.96→0.88). A head lane that wins on the curated overview cases and loses on four unseen repositories
is fitting the curated set; it ships at `0` with the code retained behind the switch.

### Local reranker on top of the structural lanes (`r3rr`: `RERANK_ENABLED=true RERANK_PROVIDER=local`)

| Suite | without | with local jina |
| --- | ---: | ---: |
| semantic nDCG@10 | 74.9% | 72.9% |
| CSN Region Hit@10 | 78.8% | 73.8% |
| SWE nDCG@100 / Core Top-1 | 74.3% / 84.6% | 62.0% / 69.2% |
| project Top-1 | 85.7% | 85.7% |
| feature / overview / compound total ms | 1182 / 1213 / 1364 | 2154 / 2181 / 2565 |

In the nine-language round the same model lifted issue ranking when nothing else ordered the head;
with frame anchors and structural heads in place it only reorders the tail, and it does so worse than
fusion. It stays off; deterministic requests skip it under any adaptive policy.

### Anchor order (`r3b` fused order vs `r3c` trace order)

Ordering an issue's anchors by their fused rank scored SWE nDCG@100 74.3 against 70.2 for trace
order, entirely from `pydata__xarray-6721` (0.53 vs 0.01), whose official score flips on a rank-1/3
swap between `variable.py` and `dataset.py`. Trace order (the API the reporter called first, then
the code it delegated to) is the explainable rule and ships.

## Post-review hardening and verification

Code review found three boundary defects before commit:

1. The callers query negated a broad SQL `%test%` / `%spec%` prefilter. That also removed ordinary
   source paths such as `src/contest/` and `src/special/`. Callers now fetch the diversified rows and
   apply the shared `is_test_path` predicate as the final decision.
2. Test-question heads used a file's first fused appearance before the evidence tier. An import
   header in an earlier file could therefore lead a named test in a later file. Evidence tier and
   test-name distance now decide first; fused file order is the tie-breaker.
3. Node frames prefixed by `async` or `new` yielded a path but no function anchor. Both prefixes are
   now parsed and covered by extraction regressions.

The final tree completed 449/449 black-box requests through the release `oce-client`: project 35,
short 240, semantic 39, SWE development 13, CSN 80, held-out relation 24, and held-out semantic 18.
Short quality is still 100% Top-1/MRR/Hit@10. Semantic ranking, SWE, CSN, and both held-out quality
vectors reproduce `r3f`; the held-out relation set keeps test-mapping Hit@3 at 100%. Project Top-1,
Hit@3 and relation recall reproduce `r3f`, while the Flask tail fluctuation described above leaves
`distractor_head` equal to baseline rather than below it. One lexical lookup hit its two-second
timeout and used the designed fallback; no request failed. Final result JSONs use the prefix
`~/.cache/oce/bench-runs/round3/reviewed-`.
The hub SQL store is also covered end to end for scoped definitions, distinct-file fan-in, package
name detection, and the ambiguity bound. The final unit run executed all 97 test files: 805 passed
and the PostgreSQL-only model test skipped because no PostgreSQL connection was configured.

## Acceptance

On the main judge: Top-1 80.0→88.6, Hit@3 94.3→97.1 and relation recall 91.5→94.3; distractor head
does not rise but the earlier 2.9→0 single-run improvement did not reproduce. Guards hold: short
quality is identical, CSN is identical, semantic stays inside measured noise, SWE nDCG@100
62.5→70.2 and Core Top-1 69.2→84.6 with core file recall at ten 71.7→64.0 as the stated cost.
Held-out relation confirms the direction (Top-1 83.3→87.5, Hit@3 91.7→95.8); the new semantic set
is unchanged by the shipped set and vetoed the hub lane.

Known limits carried forward: `axum-router-route-chain` closes no chain because `route`'s body is a
`tap_inner!` macro whose calls the extractor does not record (an index fact, `SYMBOL_EXTRACTION`
change needed); `pytest-parser-reexport` records `from _pytest… import Parser` in `pytest/__init__.py`
as an import, not a re-export (index rule); `pytest-approx-tests` labels a region with no occurrence
of the name. Result JSONs: `~/.cache/oce/bench-runs/round3/{r3base,r3a,r3b,r3c,r3d,r3f,r3rr,
heldout3-head,heldout3-new,heldout3-final}-*.json`.

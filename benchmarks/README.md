# Retrieval benchmark system

The benchmark code is versioned with OCE, but product-utility evaluation is an
external consumer of the service. It reports observations and does not turn a
metric threshold into a release decision.

## Architecture boundary

```text
benchmarks/blackbox
        |
        | production CLI and stable JSON output
        v
  oce-client binary
        |
        | ACE-compatible HTTP API
        v
     OCE server

optional provenance only:
benchmarks/blackbox --> /version, /admin/index-stats, /admin/stats
```

Code under [`blackbox`](blackbox) must not import `oce`, database drivers,
SQLAlchemy, or Milvus. It never reads server persistence and does not require a
benchmark-only API. The optional admin calls expose feature switches and
aggregate model usage; they do not expose query text or individual retrieval
traces. [`test_blackbox_boundary.py`](../tests/unit/benchmarks/test_blackbox_boundary.py)
enforces this dependency rule.

[`internal`](internal) contains implementation microbenchmarks. Those may import
server modules, but their results diagnose a component and cannot support a
product-utility claim. This distinction keeps a fast Milvus experiment useful
without coupling the black-box evaluator to the implementation under test.

The black-box layer has five infrastructure modules:

- [`harness.py`](blackbox/harness.py) owns the released-client subprocess,
  stable response parsing, safe runtime provenance, and paired-result identity.
- [`corpus.py`](blackbox/corpus.py) owns immutable Git snapshots.
- [`prewarm.py`](blackbox/prewarm.py) owns optional bounded index setup through
  client admission and production upload APIs.
- [`swe_data.py`](blackbox/swe_data.py) owns the pinned SWE-bench/SWE-Explore
  acquisition and truth conversion; curated suites do not depend on it.
- [`csn_data.py`](blackbox/csn_data.py) owns the pinned CodeSearchNet parquet
  files and the deterministic function sample behind `csn_queries`.

The default cache is `~/.cache/oce/retrieval-bench-v1`. Datasets, repositories,
client state, and raw results stay outside this repository.

## Evaluation layers

| Suite | Current coverage | Primary question | Main metrics |
| --- | --- | --- | --- |
| `short_queries` | 40 anchors, 13 snapshots, 240 English/Chinese queries | Are known symbols, paths, and references at the head? | Top-1, MRR, Hit@10, path recall, reference definition-first rate |
| `semantic_queries` | 39 reviewed queries, 13 snapshots, balanced feature/overview/call-chain intents | Does broad retrieval return the right architectural owners compactly? | graded nDCG@10, weighted Recall@5/@10, primary Top-1, characters, latency |
| `swe_explore` | real issue text and trajectory/edit truth; 5/13/53/451-case profiles | Does issue-level retrieval reach useful context and likely edit locations? | official SWE-Explore metrics, edit/core Top-1 and Recall@10, characters, latency |
| `project_cases` | 35 relation cases, 10 snapshots: reference, call-chain, test mapping, re-export, multi-implementation | Does the answer close the relation an edit needs: callers, hops, tests, public entry, right overload? | primary Hit@3/MRR, relation and supporting recall, hop coverage, chain closure, test recall, distractor-in-head rate, truth-region share, one error class per case |
| `csn_queries` | 80 docstring queries, 8 pinned CodeSearchNet repositories, 4 languages | External sanity guard: does a plain description still reach its function? | region Top-1/Hit@5/Hit@10/MRR, file Top-1, split by whether the query names the function |

The 240 short queries are variants of 40 anchors, not 240 independent needs.
[`query_variants.json`](blackbox/query_variants.json) fixes 12 needs with six
wordings each (72 queries): quoted/unquoted identifiers, nominal and imperative
forms, Chinese and an informational preamble. It inherits reviewed region truth
from `project_cases` unchanged. `project_cases` reports equal-need means and each
need's worst wording, together with wording and repository breakdowns. All forms
of a need belong to one split. These assistant-authored forms are development
controls and are not independent human annotations or untouched held-out data.

[`layout_controls.py`](blackbox/layout_controls.py) generates four synthetic needs
in Python and TypeScript over 20 code layouts (80 queries). Names, module filenames
(including `index.ts`, `types.ts`, and `__init__.py`) and declaration offsets vary;
imports and target line labels change with them. Its spec digest includes source,
queries and truth, and evaluation uses the released client. This isolates layout
sensitivity; it does not measure real-project or downstream agent success.

```bash
uv run python -m benchmarks.blackbox.project_cases --cases benchmarks/blackbox/query_variants.json run --api-url http://127.0.0.1:8986 --label candidate --output /tmp/query-variants.json
uv run python -m benchmarks.blackbox.layout_controls run --api-url http://127.0.0.1:8986 --label candidate --output /tmp/layout-controls.json
```

`project_cases` is the main judge for relation work; the public suites are
guard rails. Its truth ([`project_cases.json`](blackbox/project_cases.json))
was labeled with LLM assistance: enclosing-definition spans were computed with
AST/brace tools on the pinned snapshots, use sites were collected with `grep`,
and each region was reviewed once. The manifest records the method, and the
`labeling.method` field is required so the provenance travels with the truth.
Every case carries `must_not_paths`, files that must not lead the answer, so a
lane cannot buy recall by returning more. Each case is assigned one error class
in severity order: `exact_miss` (no primary region in the first three),
`distractor` (a must-not file in the first three), `test_missing`,
`relation_missing`, `redundant` (fewer than a quarter of the returned regions
overlap truth), else `none`.

`csn_queries` replaces the CrossCodeEval/RepoBench-R guard rails that were
planned first: CrossCodeEval's raw repositories are available only on request
and RepoBench-R does not pin repository commits, so neither can be replayed
behind the black-box boundary. CodeSearchNet permalinks carry the commit, and
the 8 repositories are chosen by a salted hash among moderately sized,
reachable ones; the parquet files are needed only to regenerate the manifest
(`csn_queries select`). The CodeSearchNet challenge itself is archived, and the
dataset license is listed as "other" on Hugging Face; review it before any use
beyond internal evaluation.

[`curated_corpus.json`](blackbox/curated_corpus.json) pins the 13 repository snapshots
used by the curated suites. [`short_query_anchors.json`](blackbox/short_query_anchors.json)
and [`semantic_cases.json`](blackbox/semantic_cases.json) reference those IDs;
the evaluator refuses unknown snapshots, mutable revisions, missing truth files,
or comparisons with different truth digests or ordered case IDs.

Use the layers at different cadences:

1. During implementation, run the relevant unit/regression test and the affected
   black-box suite.
2. Before changing a default retrieval strategy, run paired variants on
   `short_queries`, `semantic_queries`, `project_cases`, and the `development`
   issue profile. Repeat any variant involving an external model at least
   twice. `csn_queries` is a guard rail for plain semantic retrieval; run it
   when chunking, embedding input, or dense fusion changes.
3. Before making a broader product claim, add the `standard` 53-issue profile.
   Use the 451-case `verified` profile for milestone studies, not routine edits.

The curated truth is deliberately small and reviewable. It is suitable for
regression and ablation work, but it is not an independently reviewed benchmark.
The historical `heldout_*` manifests have also informed the September 10 routing
review and are now development regression sets. Their filenames and frozen truth
remain for paired replay; they no longer provide untouched validation evidence.

Work on utility first: locate the right implementation, return the requested use
sites, and cover the required relation steps. Optimize latency after those gains
are established. Keep characters and latency visible throughout; a faster answer
does not compensate for missing evidence.

Release judgement uses the metric vector, not one score. The target category of
a change must improve; every other suite must stay inside its tolerance; the
distractor-in-head rate of `project_cases` must not rise; characters and p50
latency are checked separately. The tolerances that have held so far are one
short-suite query (0.4 points Top-1), one semantic nDCG@10 point, one issue on
the development profile, and the same run-to-run noise on `project_cases`
(one case, about 3 points on any rate).
There is not yet a downstream agent task-success suite or an ACE head-to-head
evaluation, so retrieval scores must not be presented as either result.

The [September 11 version decision](results/version-decision-2026-09-11.md)
compares `ac5c9e1` plus the shared repairs from `1fc581f` with the archived current
version. The current version fails two semantic guards, so the default source
restores that foundation. Its wording and layout gains remain available on the
archive branch for further research. This decision supersedes the older reports'
working-tree status; their historical use of "foundation" can refer to a different
source revision. No fresh validation set was consumed by this decision.

The [September 10 state-machine evaluation](results/state-machine-2026-09-10.md)
records the repeated baseline/candidate comparison, wording and layout controls,
and separate source-prior ablations. It retains the semantic regressions that
exceed the current tolerance; passing unit checks is not a utility qualification.
The [follow-up evaluation](results/state-machine-followup-2026-09-10.md) records
the subsequent test-to-implementation and explicit-implementor repairs. It keeps
the earlier semantic losses visible without restoring filename-specific priors
to recover scores on those cases.
The [global simplification evaluation](results/state-machine-simplification-2026-09-10.md)
records the subsequent unified fusion/budget candidate, six optional-capability
ablations, and a frozen 20-issue external comparison. It retains lower observed
latency alongside head-order and ranking regressions; the candidate is not
qualified as a quality-preserving default release.
These reports preserve their original run-time status and source hashes.
Statements about uncommitted work or publication describe the recorded run;
archiving a report does not adopt its candidate or change its original scores.
The [retrieval quality recovery](results/retrieval-quality-recovery-2026-09-11.md)
records the completed C6–C12 experiments, including the failed final validation.
Those archived candidates do not describe the current working tree.
The [repair isolation](results/retrieval-isolation-2026-09-11.md) retains the
original ranking with shared SQL, cancellation, insertion and admission repairs.
Three separate stage hypotheses were screened on 18 development issues and 35
project cases; none established enough benefit to adopt. Historical lexical
timeouts remain unreproduced, so the report makes no root-cause or speedup claim.
The [reference head experiment](results/reference-heads-2026-09-11.md) follows the
separate foundation commit `1fc581f`. The v2 development catalog improves, but
six Bash reference regressions and a wrong qualified caller prevent adoption.
Its four paired guards, source audit of legacy-label conflicts, and opt-in SQL
timeout observer are retained; no fresh validation batch was consumed.

The [reference region contract v2](results/reference-region-contract-v2-2026-09-11.md)
uses actual source-use lines, including uses in a declaration's file. Its separate
[manifest](blackbox/reference_regions_v2_2026_09_11.json) is a reviewed development
catalog; old short-query labels and scores remain unchanged. Run it with:

```bash
uv run python -m benchmarks.blackbox.project_cases --cases benchmarks/blackbox/reference_regions_v2_2026_09_11.json run --api-url http://127.0.0.1:8986 --label reference-v2 --output /tmp/reference-v2.json
```

### Upstream question supplement

[`upstream_project_cases.json`](blackbox/upstream_project_cases.json) adds four
TypeScript-to-Rust/Tauri relation questions and two frontend usage questions.
[`upstream_semantic_cases.json`](blackbox/upstream_semantic_cases.json) adds six
backend feature/architecture questions. These adapt question ideas from
[`oce-ai/oce-benchmark`](https://github.com/oce-ai/oce-benchmark/tree/d4f10554a18e31599d1e46d5d56da6588d4aa86c),
with upstream commit, JSONL digest, question IDs, and adaptation notes recorded in
the manifests. They use the existing released-client runners and scoring.

The single snapshot in [`upstream_corpus.json`](blackbox/upstream_corpus.json)
pins `farion1231/cc-switch` at `40cac1a68edf8c9e7b3a89125cf40bb93a348404`.
It contains both TypeScript and Rust; the harness's snapshot-level `typescript`
bucket must not be interpreted as a per-language result.

Labels were source-reviewed before retrieval, with LLM assistance rather than
independent human review. Corrections include `useSettingsQuery` instead of
`useSettings`, the omitted profile mutation layer, the actual prompt hook calls,
the HTTP response constructor, and the retry owner for failover. Relation scoring
requires line overlap with call sites/handlers; semantic scoring remains graded
file-owner ranking. `source_evidence` in the semantic manifest records the spans
used to audit labels and is not scored. Neither metric establishes answer
correctness or downstream task success. For IPC chains, hop coverage means the
required pieces were returned, not that static analysis resolved the IPC edge;
Tauri registration is supporting evidence and is not counted as a call hop.

Keep this supplement separate from the original suites so their historical
scores remain comparable. This is a development set once its failures are used
to guide changes, not a sealed held-out test. Run it alongside the existing
guards when changing IPC/callback relation retrieval or the semantic behavior
these questions exercise. Validate and prewarm it with:

```bash
uv run python -m benchmarks.blackbox.project_cases \
  --cases benchmarks/blackbox/upstream_project_cases.json \
  --corpus benchmarks/blackbox/upstream_corpus.json check
uv run python -m benchmarks.blackbox.semantic_queries \
  --cases benchmarks/blackbox/upstream_semantic_cases.json \
  --corpus benchmarks/blackbox/upstream_corpus.json check
uv run python -m benchmarks.blackbox.prewarm \
  --corpus benchmarks/blackbox/upstream_corpus.json
```

For each runner, keep the same `--cases` and `--corpus` arguments and replace
`check` with `run --label <variant> --output <result.json>`. Use `OCE_API_KEY`
and `--api-url` as in the ordinary suites. Review per-case region misses,
relation/hop recall, distractors and semantic owner ranks before latency;
do not fold these twelve questions into a combined product score.
The [initial repeated baseline](results/upstream-supplement-2026-09-09.md)
records the current gaps and all truth/result digests.

## Prepare and validate

Validate the reviewed query sets and prepare their pinned snapshots:

```bash
uv run python -m benchmarks.blackbox.short_queries check
uv run python -m benchmarks.blackbox.semantic_queries check
uv run python -m benchmarks.blackbox.project_cases check
uv run python -m benchmarks.blackbox.csn_queries check
```

Prepare an issue profile separately:

```bash
uv run python -m benchmarks.blackbox.swe_explore \
  --profile development prepare
```

On a fresh personal-mode server, synchronous embedding of a large repository may
outlast the client's ordinary request timeout. Prewarm the shared curated corpus
before running `short_queries` or `semantic_queries`:

```bash
export OCE_API_KEY=...
uv run python -m benchmarks.blackbox.prewarm
```

The SWE profiles use issue-specific snapshots, so prewarm the selected profile
separately before paired issue runs:

```bash
export OCE_API_KEY=...
uv run python -m benchmarks.blackbox.swe_explore \
  --profile development prewarm
```

Both commands ask the released client for each workspace's admitted file list, then
use the production `/find-missing` and `/batch-upload` endpoints with smaller
batches. It therefore shares the client's ignore, sensitive-file, encoding,
size, and symlink policy while remaining setup rather than a client-throughput
measurement. The subsequent run still performs a complete real-client sync and
emits no quality report if a workspace fails to sync.

## Run paired variants

Start or restart OCE separately with the configuration being evaluated. Provide
the data key and, optionally, the admin key:

For a reranking change, the minimum useful matrix is `none` (both rerankers
disabled), `dedicated-always`, and `dedicated-adaptive`. Add an
`adaptive-cascade` run only when measuring the incremental value of chat-LLM
reranking. This separates three questions: whether reranking beats disabled,
whether adaptive preserves the quality of always, and whether chat adds enough
utility above the dedicated model. Run external-model variants at least twice.

```bash
export OCE_API_KEY=...
export OCE_ADMIN_API_KEY=...

uv run python -m benchmarks.blackbox.short_queries run \
  --label adaptive-r1 \
  --metadata deployment=personal \
  --output /tmp/oce-short-adaptive-r1.json

uv run python -m benchmarks.blackbox.semantic_queries run \
  --label adaptive-r1 \
  --metadata deployment=personal \
  --output /tmp/oce-semantic-adaptive-r1.json

uv run python -m benchmarks.blackbox.swe_explore \
  --profile development run \
  --label adaptive-r1 \
  --metadata deployment=personal \
  --output /tmp/oce-swe-adaptive-r1.json

uv run python -m benchmarks.blackbox.project_cases run \
  --label adaptive-r1 \
  --metadata deployment=personal \
  --output /tmp/oce-project-adaptive-r1.json

# CodeSearchNet snapshots are not part of the curated corpus: prewarm them first.
uv run python -m benchmarks.blackbox.csn_queries prewarm
uv run python -m benchmarks.blackbox.csn_queries run \
  --label adaptive-r1 \
  --metadata deployment=personal \
  --output /tmp/oce-csn-adaptive-r1.json
```

`OCE_ADMIN_API_KEY` is optional. When present, the harness reads the runtime
retrieval switches, active index profile, store availability, and query-cache
state, then snapshots `/admin/stats` around retrieval to report only the
aggregate model-call/token delta. Automatic runner-environment metadata uses a
non-secret allowlist and excludes keys and endpoint URLs; it is not presented as
authoritative server configuration. Explicit `--metadata` remains the caller's
responsibility. Result JSON also records benchmark timing controls and contains
ranked paths and line spans, not retrieved source text or credentials.

The default six-second settling interval exceeds OCE's default five-second
metrics flush period. If the benchmark server uses a longer
`MONITORING_FLUSH_INTERVAL_SECONDS`, pass a correspondingly larger
`--metrics-settle-seconds`; this delay applies only when an admin key is present.

Use an otherwise idle benchmark server for latency and model-usage comparisons.
The admin counters are aggregate observations, so concurrent traffic would be
included in their before/after delta.

Compare like-for-like runs:

```bash
uv run python -m benchmarks.blackbox.short_queries compare \
  /tmp/oce-short-none-r1.json /tmp/oce-short-adaptive-r1.json

uv run python -m benchmarks.blackbox.semantic_queries compare \
  /tmp/oce-semantic-none-r1.json /tmp/oce-semantic-adaptive-r1.json

uv run python -m benchmarks.blackbox.swe_explore compare \
  /tmp/oce-swe-none-r1.json /tmp/oce-swe-adaptive-r1.json
```

A retrieval error remains in the denominator as zero utility; it is never
dropped from quality aggregates. Latency percentiles use successful calls, with
the error count shown alongside them. This separates system reliability from
the latency distribution without hiding either.

## Reading each suite

### Short structural queries

Each anchor expands into `symbol`, `path`, and `reference` queries in English and
Chinese. Definition and path truth is manually pinned. Reference truth is a
file-level lexical derivation over tracked, non-test source files; it is useful
for regression but is not a semantic call graph. `definition_top1` shows when a
reference query incorrectly answers with the declaration file.

This suite measures observable utility only. Whether the server internally
selected `skip:exact_definition`, `skip:path_evidence`, or a reranker is covered
by the retrieval strategy and pipeline unit tests. The benchmark intentionally
does not read `retrieval_metrics` or duplicate the server's routing state
machine.

### Reviewed semantic queries

Every snapshot has one feature, one architecture overview, and one call-chain
query. Relevant files carry grades 1-3 and a short role. nDCG rewards placing
the primary owners first; weighted recall rewards recovering the supporting
files. Reports split metrics by intent and by Python/TypeScript/Rust so an
overall gain cannot conceal a language or task regression.

The truth records architectural ownership, not every acceptable context file.
Review per-case ranked paths before changing grades; do not tune truth to favor a
specific retrieval variant.

### Project relation cases

Regions are scored by line overlap with the `Path:`/`Lines:` sections of the
returned text, so evidence appended after the primary results (callers,
implementations, tests, re-exports, related definitions) counts toward
relation recall exactly as an agent reads it. `primary_hit_at_3` is the head
line for every kind; `relation_recall` and `supporting_recall` measure
closure; `hop_recall`/`chain_closed` apply to call-chain cases whose regions
carry `hop` numbers; `test_recall` is file-level over `test_paths`;
`distractor_head` is the precision guard; `truth_region_share` is the
fraction of returned regions that overlap any truth region or test file.
Read the per-kind table and the error-class distribution before the means.

### SWE-Explore issue retrieval

The harness joins pinned, SHA256-verified copies of:

- [SWE-bench Verified](https://huggingface.co/datasets/SWE-bench/SWE-bench_Verified),
  for issue text, base revisions, and accepted patches;
- [SWE-Explore](https://huggingface.co/datasets/SWE-Explore-Bench/SWE-Explore-Bench),
  for core files and regions from successful issue-solving trajectories; and
- [the reference evaluator](https://github.com/Qiushao-E/SWE-Explore-Bench/blob/5602f031f2d9562d0a805f83402b536e831a5a11/quality/bench_metrics.py),
  for the published line/ranking/context-efficiency formulas.

| Profile | Current size | Purpose |
| --- | ---: | --- |
| `pilot` | 5 issues / 5 repositories | end-to-end smoke |
| `development` | 13 issues / 5 repositories | paired strategy iteration |
| `standard` | 53 issues / 12 repositories | broader repository mix |
| `verified` | 451 joined issues | milestone evaluation |

An external case manifest can freeze a selection before implementation:

```bash
uv run python -m benchmarks.blackbox.swe_explore --case-manifest benchmarks/blackbox/swe_validation_2026_09_10.json run --api-url http://127.0.0.1:8986 --output /tmp/swe-validation.json
```

Each entry binds the native issue, repository revision, query and truth digest. The
runner rejects drift instead of silently substituting another case. The
[September 10 validation protocol](results/simplification-validation-protocol-2026-09-10.md)
defines this selection's exposure boundary and aggregate-only review. Once results
have informed a change, treat the set as development evidence in later iterations.
This specific batch has now been evaluated and its aggregate results inspected;
it must not be presented as untouched validation for further tuning.

`edit_*` measures gold-patch files and changed base-tree lines. `core_*` and
official `swe_explore_*` measure useful trajectory context. Neither truth source
is complete: accepted edits omit explanatory context, while trajectories are
model-dependent and sometimes mark coarse full-file regions. Interpret both
axes together.

SWE-Explore is CC BY-NC-ND 4.0. Review its terms before using the downloaded
data outside internal product evaluation.

## Internal component diagnostics

The Milvus workspace-filter microbenchmark is intentionally server-coupled:

```bash
uv run python -m benchmarks.internal.milvus_scope
```

It creates a temporary Milvus Lite database and emits one JSON observation. A
second internal tool reads a server's `retrieval_metrics` table and tabulates the
rerank route by intent and definition ambiguity next to the appended relation
sections; it is the shadow log the adaptive thresholds are calibrated from:

```bash
uv run python -m benchmarks.internal.rerank_evidence ~/.oce/data/oce.db --since-hours 24
```

A
dated host-specific sample is retained in
[`results/milvus-lite-scope-2026-09-01.json`](results/milvus-lite-scope-2026-09-01.json).
It is not a release threshold or evidence of end-to-end retrieval quality.

The first black-box baseline, including the Milvus Lite flush and SQLite WAL findings it
surfaced, is in [`results/blackbox-baseline-2026-09-03.md`](results/blackbox-baseline-2026-09-03.md).
The nine-language round that followed (extraction fixes, routing fixes, query cap, call-hop
ablation, and the in-process ONNX reranker against the API reranker) is in
[`results/nine-language-utility-2026-09-03.md`](results/nine-language-utility-2026-09-03.md).
Earlier adaptive-rerank and head-order observations are retained in
[`results/swe-explore-development-2026-09-02.md`](results/swe-explore-development-2026-09-02.md)
and
[`results/swe-explore-development-2026-09-03.md`](results/swe-explore-development-2026-09-03.md).
Their raw result schema predates the current suite/truth identity contract, so use the reports
as narrative history rather than inputs to the current `compare` commands.

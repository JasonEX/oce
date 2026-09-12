# Reference head evidence experiment

Status: foundation repairs committed as `1fc581f`; the reference candidate is
rejected. The development gain does not satisfy the agreed guard conditions.
No fresh validation batch was consumed, and the default ranking remains the
foundation. The candidate is available as a
[reviewable patch](reference-heads-2026-09-11-data/rejected-candidate.patch).

## Committed foundation and isolated change

The foundation commit contains the seven shared production repairs and their
tests, plus the SQL/deadline and index-compatibility documentation. It excludes
the archived ranking experiments and benchmark changes. Its production source
SHA-256 remains
`8e6e3bccbbc61d032721e6235041b2c9d123bef4d3442448e9592cfee7a10a38`, matching
the [previous source-bound validation](retrieval-isolation-2026-09-11.md).
The commit does not claim that the historical timeouts were fixed.

The isolated candidate changes only reference head evidence and ordering.
It checks the syntax of already recalled fragments, recognizes body uses and
quoted Python type annotations, excludes import/prose-only evidence from head
slots, and removes the blanket penalty for uses in a declaration's file.
The existing symbol provider implements a small verifier protocol; composition
and the prior stage supply its facts to pure ranking. Persisted extraction,
index fingerprints, routing, recall, fusion, selection and relation assembly
are unchanged. Tests and override-specific head branches are also unchanged.

The [frozen source proof](reference-heads-2026-09-11-data/candidate-frozen-source.json)
identifies six changed production files and source SHA-256
`bce4d190e402852a2111348986a86f24a7f21487ca008973d438d12138074a32`.
The [protocol](reference-heads-2026-09-11-data/protocol.json) was recorded before
implementation. All utility runs used the released `oce-client`, unchanged
blackbox evaluators and the same prepared index, sequentially, with model
rerankers and query cache disabled. No database reads or server imports were
used to compute utility scores.

## Development evidence

The frozen v2 catalog has 18 questions covering nine reference families. Both
runs completed all requests successfully. Head tracing is internal diagnosis;
the scores below come from the native blackbox reports.

| Metric | Foundation | Candidate |
|---|---:|---:|
| Catalog primary Top-1 | 0.6111 | 0.8333 |
| Catalog primary Hit@3 | 0.8333 | 0.9444 |
| Catalog primary MRR | 0.6852 | 0.8704 |
| Catalog relation recall | 0.4765 | 0.4695 |
| Mean characters | 22,225.9 | 22,176.9 |

[Case-level comparison](reference-heads-2026-09-11-data/development-comparison.json)
preserves every result. Subsequent source inspection found an actual use in all
18 candidate heads. Three were absent from the bounded catalog: `current_app:
"Flask"` in `globals.py:46` for both wordings, and the `TypeToken` type/subclass/
constructor uses in `TypeTokenTest.java:300–342`. This
[source audit](reference-heads-2026-09-11-data/development-head-source-audit.json)
is post-hoc development evidence. It does not change the frozen score to 100%,
relabel cases, or provide independent validation.

## Four paired guards

Both sides completed 240 short queries, 39 semantic queries, 35 project cases
and 13 development issues: 654 native retrieval requests in these paired guards.
All had successful HTTP responses. A foundation exact-lookup warning is examined
separately below. Truth digests, ordered cases, client identity, runtime settings,
index profiles and entity counts matched across each pair.

| Metric | Foundation | Candidate |
|---|---:|---:|
| Short Top-1 | 1.0000 | 0.8458 |
| Short MRR | 1.0000 | 0.9217 |
| Semantic nDCG@10 | 0.7312 | 0.7312 |
| Project primary Top-1 | 0.9429 | 0.9143 |
| Project primary Hit@3 | 0.9714 | 0.9714 |
| Project relation recall | 0.9433 | 0.9433 |
| Project test recall | 1.0000 | 1.0000 |
| Project distractor head | 0 | 0 |
| SWE edit Top-1 | 0.4615 | 0.4615 |
| SWE nDCG@100 | 0.6534 | 0.6534 |

The [comparison](reference-heads-2026-09-11-data/guard-comparison.json) retains
all changed cases and per-kind/language aggregates. All 39 semantic responses
have identical returned regions. Short symbol and path queries retain perfect
Top-1; the 37 losses are reference questions. The
[source audit of those losses](reference-heads-2026-09-11-data/short-failure-source-audit.json)
separates them as follows, without revising the old labels:

- **30 valid same-file uses** fail the old contract because it excludes the
  whole declaration file. Examples include `Request.prepare` constructing a
  `PreparedRequest` and `as_dataset` constructing a `Dataset`.
- **One valid test use** of `TypeToken` fails because the old file catalog
  excludes test directories.
- **Six actual regressions** are the English/Chinese Bash queries for three
  functions. Their heads contain declarations, not calls. Bash commands are
  not identifier leaves in this verifier, and extensionless executable chunks
  may have no inferred language. Removing the old head policy when no usable
  syntax facts exist exposes the declaration again.

The project loss is also substantive. For `express-app-render-callers`,
`lib/application.js:612–661` moves above the real caller in `lib/response.js`.
Its call is `view.render` at line 657, not `app.render`. The SQL use batch
supplies leaf-call evidence, and a nearby `app` mention is insufficient to prove
the receiver. Removing the file penalty exposes that ambiguity at the head.
The unchanged file-based distractor metric does not detect this wrong receiver.

These findings reject the current candidate independently of the obsolete
file-level labels. Future work must handle unavailable syntax evidence and
qualified receivers before repeating the development guards. The current
scores must not be repaired by changing labels or tolerances, and no new
validation questions were opened for this failing candidate.

## Runtime observations and continued timeout capture

No lexical timeout occurred in the 690 development-plus-guard requests. One
exact lookup timed out during the foundation SWE run. Sequential response
mapping associates it with `pylint-dev__pylint-7080`; that mapping alone does
not identify a cause. [Runtime logs and their hashes](reference-heads-2026-09-11-data/runtime-audit.json)
retain the warning and every server's normal shutdown.

Two additional released-client repetitions of that issue used opt-in SQL
instrumentation. Both completed without warnings. Each triggered 154 exact
searches; the query has 153 extracted identifiers and a compound route.
Across the repetitions, the longest exact operation was 510 ms, connection
acquisition reached 499 ms, event-loop delay reached 287 ms, and the longest
individual SQL statement took 187 ms. Acquisition includes opening a connection
when needed; it is not a pure queue-wait measure. These observations expose
where time was spent in the repeat, without proving the cause of the original
warning or of the three historical lexical timeouts.

Both repeated foundation responses have exactly the candidate's returned
regions and 25,877 characters. The original foundation response had 26,119
characters. This is the only differing SWE response, and its compound route
does not run the new reference rule. Consequently, the SWE character/noise
differences are not credited to this candidate.
[Repeat evidence](reference-heads-2026-09-11-data/exact-repeat-summary.json)
and the full hashed-statement trace are preserved.

The reusable [internal observer](../internal/retrieval_sql_trace.py) records
SQL duration, connection acquisition and scheduling delay. It is opt-in, writes
query/statement hashes rather than raw text or parameters, and does not change
deadlines. Two real SQLite microprobes checked SQL timing, pool return and
detection of an injected scheduling delay. To capture a future occurrence:

```bash
OCE_TIMEOUT_CAPTURE=/tmp/oce-sql-timing.jsonl uv run python -c \
  'import benchmarks.internal.retrieval_sql_trace; from oce.cli import main; main()' \
  -v serve --data-dir /path/to/data --env-file /path/to/server.env
```

Latency and characters remain separate observations. The moving 24-hour model
usage counters produced negative token deltas and cannot establish run-specific
cost. No speedup or cost reduction is claimed.

## Verification and retained state

The rejected candidate passes **206 focused checks across seven files**,
including the real SQLite retrieval regression and the existing tree-sitter
extraction checks. Three test doubles were corrected to distinguish definition
and use-kind requests; their assertions were retained. These checks establish
implementation behavior, not acceptance in light of the blackbox failures.
[Source-bound test proof](reference-heads-2026-09-11-data/candidate-checks/proof.json)
and the logs are retained. A full candidate release/package qualification was
not run after rejection.

The committed foundation retains the previous same-source validation: 852 unit
passes and one PostgreSQL skip on each of Python 3.13 and 3.11, six real Milvus
Lite integration passes, and an 18-query packaged-client smoke. Current Ruff,
lock and whitespace checks pass. PostgreSQL service-mode integration remains
unverified, and admission version 1 indexes still require a new data directory
and full client resynchronization.

The [final proof](reference-heads-2026-09-11-data/final-proof.json) binds the
foundation commit, unchanged production source, candidate archive and artifacts.
All benchmark servers shut down normally. The reference implementation was not
copied into the production tree. Reports and the internal observer remain
uncommitted; no push, tag or deployment was performed.

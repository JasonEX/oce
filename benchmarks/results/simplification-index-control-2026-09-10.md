# Shared-index control for the frozen validation

Recorded before producing or inspecting any result on the new external batch.
The [frozen selection and exposure protocol](simplification-validation-protocol-2026-09-10.md)
and its case manifest remain unchanged.

The retrieval comparison uses the same freshly built index for both variants.
Its baseline is `879bc2e5322edd2f3763c7d71deeee891550f5c7`. The first pair used
two files synchronized to the candidate:

- `src/oce/domain/services/source_filter.py`: admit ordinary directories whose
  names end in `-retrieval-eval`.
- `src/oce/shared/index_profile.py`: record that actual admission behavior as
  source-admission version 2.

Before the final pair, preparation exposed a single-file SQL parameter overflow.
The final shared control also synchronizes
`src/oce/infrastructure/persistence/sql_symbol_projection.py`: unchanged rows are
passed as parameter sets for dialect-managed insertion instead of one unbounded
multi-row statement. A synthetic 400-symbol test under SQLite's 999-variable
limit reproduces the old failure and verifies complete, idempotent insertion
after the fix. The repair changes neither extraction nor index semantics.
Its timing, hashes and failed preparation attempt are recorded in
`projection-insert-control.json`, before any external validation query.

All other baseline production files, including the entire retrieval pipeline,
remain byte-identical to the commit. Both variants therefore declare the same
index semantics and use the same source vectors, SQL projections, index build,
embedding model and released client. This applies the admission change itself
to both variants; it does not bypass the profile compatibility check.

This comparison isolates retrieval orchestration, ranking and context assembly.
It does not estimate the separate utility gain from admitting previously
excluded files. That behavior has a distinct packaged-server smoke test through
the released client's sync and retrieve commands. The original unmodified
commit's 613-query run is retained separately and will not be presented as the
shared-index baseline.

The adjustment avoids duplicate embedding work and removes differences between
independent index builds from the paired comparison. Interrupted preparation
attempts remain in the operational record and supply no quality or latency
results. Query measurements run after staging has stopped, with the normal
query configuration. A larger upload batch is used only during staging.

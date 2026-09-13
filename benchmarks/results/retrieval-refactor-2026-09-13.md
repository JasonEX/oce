# Retrieval refactor verification — 2026-09-13

Baseline: `b2cd4b5f947179b9356cb0a4ea66a3df1972e33d`. Candidate: the source tree
in this commit, including the review fixes to optional-package type checking
and related-definition refresh failure handling.

## Frozen-corpus equivalence

Both revisions ran the new `benchmarks.internal.retrieval_equivalence` harness
against the baseline's frozen `src/oce` directory. Indexing used real
tree-sitter symbols, SQLite lexical and path stores, and deterministic term
vectors in place of embedding and dense/path vector stores.

| Evidence | Result |
| --- | --- |
| Frozen corpus | 162 files |
| Corpus digest | `3a6a74eab20de538294c37f6bb32d323a5e2fc5d1b5c1ee7226db88b5d638473` |
| Queries / settings profiles | 43 / 7 |
| Identical result and deterministic audit records | 301 / 301 |
| Recorded lane failures | None in either dump |
| Canonical result-array SHA-256 | `fa5dee6922e48f66396cbfe718ce0d1017e4d11192859594a6db5049ff0fafc2` |

The canonical hash uses UTF-8 JSON with sorted keys, `ensure_ascii=False` and
`separators=(",", ":")`. Each hit records its path, span, role, hop, context,
score and content digest. Audit comparisons include route decisions, head
slots, definition evidence, relation counts/budget, stage names, scope size,
path routing and lane failures. The baseline predates the lane-failure field;
the harness treats its absence as an empty map.

This checks implementation equivalence on the fixed corpus and profiles.
It does not measure product utility or latency.

## Review checks

- Ruff check/format, mypy over 162 source files, lock and whitespace checks passed.
- Mypy passed with `dev` dependencies alone and with `local-rerank` installed.
- The 100 unit-test files ran in separate processes. The final per-file results,
  including focused reruns after review fixes, contain 849 passed and 1 skipped.
- All 6 Milvus Lite integration tests passed, including scoped search, index
  rebuilding with preserved vectors, deletion and the path index lifecycle.
- The assembled personal-mode smoke passed over migrated SQLite, Milvus Lite,
  an in-process embedding HTTP endpoint, authenticated retrieval and metrics.
- Sdist and wheel builds passed. Imports and migrations from the extracted
  wheel passed; the retrieval package, Alembic template and new migration ship.
- A failing related-definition refresh was reproduced before its fix. The
  regression now preserves the primary head and successful preview evidence,
  records `related:TimeoutError`, and keeps the response within its budget.
- Non-empty lane failures were verified through the application metric record
  and an actual SQL metrics flush.

To reproduce the equivalence comparison, extract the baseline's `src/oce` to
a temporary directory, use that same directory as `--corpus` for both dumps,
and set `PYTHONPATH` to the baseline's `src` for the first dump. Run the
candidate dump from this checkout, then compare the two JSON files with the
harness's `compare` command.

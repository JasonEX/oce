# Documentation

Start with the [English README](../README.md) or [中文使用说明](../README.zh-CN.md)
for installation, personal/service deployment, client setup and HTTP operations.

| Topic | Maintained reference |
| --- | --- |
| Configuration and model data exposure | [`.env.example`](../.env.example); deployment examples in the README |
| Retrieval stages, evidence, budgets and experiment retirement | [Retrieval pipeline](retrieval-pipeline.md) |
| Container ownership, worker, indexing, credentials and deletion | [Runtime lifecycle](runtime-lifecycle.md) |
| Evaluation contracts and reproducible commands | [Benchmark guide](../benchmarks/README.md) |
| Dated measurements and rejected candidates | [Evaluation archive](../benchmarks/results/README.md) |
| Development constraints and validation | [AGENTS.md](../AGENTS.md) |
| Changes awaiting release and published versions | [CHANGELOG.md](../CHANGELOG.md) |

The README owns user-facing instructions; the two design documents own their
subsystem contracts. AGENTS records constraints for implementation work, rather
than duplicating the full design. CLAUDE points to AGENTS so both agents use the
same rules.

Archived evaluations retain their original source identities, measurements and
decisions. A dated report describes its measured candidate; it does not establish
the behavior or release status of the current checkout. Use the archive index to
find the relevant run, then check its scope and limitations before comparing it.

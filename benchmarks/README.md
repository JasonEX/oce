# Retrieval infrastructure benchmarks

These benchmarks are lightweight engineering diagnostics. They report observations and do
not turn thresholds into release decisions.

## Milvus workspace scope

Measure the production dense and path search calls with 2,000, 10,000, and 50,000 SHA256
blob names in the workspace filter:

```bash
uv run python benchmarks/milvus_scope.py
```

The command builds a temporary Milvus Lite database, prints progress to stderr, and emits
one JSON result to stdout. Use `--iterations` and `--warmups` to trade runtime for a larger
sample. No database or report is retained unless the caller redirects the JSON explicitly.

A raw diagnostic run from the current development environment is retained in
[`results/milvus-lite-scope-2026-09-01.json`](results/milvus-lite-scope-2026-09-01.json).
It is a host-specific observation, not a release threshold: all samples stayed in scope and
returned the target, while the 50,000-member filter reached 3.4 MB and roughly 280 ms p95.

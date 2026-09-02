# SWE-Explore development rerank study — 2026-09-02

This is an engineering observation, not a release gate or a claim about all models. It uses
the deterministic 13-issue `development` profile from five repositories. Issue text, base
commits, and edit patches come from SWE-bench Verified; successful-trajectory core files and
regions come from SWE-Explore. The benchmark harness pins and checksums both sources plus
SWE-Explore's official `compute_region_metrics` implementation at `5602f031`.

All variants used the same prepared snapshots, index, Qwen3-Embedding-4B embeddings, OCE
working tree based on `dbb9c4f`, and `oce-client 0.1.2`. Query routing used the production
deterministic analyzer and query rewrite was disabled. The optional rerank models were
Qwen3-Reranker-0.6B and
`deepseek-v4-flash`; chat reranking used the `adaptive` policy and a 15-second deadline.
The stored ranked spans from those paired runs were then scored with the pinned official
evaluator; no retrieval output was regenerated during metric alignment.

## Edit-location evidence

| Variant | Top-1 | File R@10 | Region R@10 | MRR |
| --- | ---: | ---: | ---: | ---: |
| No rerank | 38.5% | 53.8% | 23.1% | 41.8% |
| Dedicated rerank, 50 candidates | 30.8% | 65.4% | 47.3% | 46.8% |
| Chat only, adaptive, 20 candidates | 23.1% | 61.5% | 42.3% | 37.9% |
| Chat only, adaptive, 50 candidates | 46.2% | 61.5% | 36.5% | 52.6% |
| Dedicated 50 → chat 20 | 38.5% | 65.4% | 49.9% | 50.6% |

## Official SWE-Explore coverage and efficiency

| Variant | Line precision | Line recall | F1 | File hit | Region hit | WCC | Context efficiency |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| No rerank | 9.8% | 4.2% | 5.4% | 60.8% | 39.6% | 7.7% | 24.1% |
| Dedicated rerank, 50 candidates | 12.9% | 5.5% | 7.3% | 70.0% | 44.7% | 12.1% | 27.5% |
| Chat only, adaptive, 20 candidates | 9.8% | 4.4% | 5.7% | 62.7% | 46.7% | 14.3% | 23.7% |
| Chat only, adaptive, 50 candidates | 10.7% | 4.7% | 6.1% | 60.8% | 44.7% | 14.6% | 24.1% |
| Dedicated 50 → chat 20 | 13.0% | 5.8% | 7.6% | 71.9% | 50.5% | 15.2% | 29.0% |

WCC is the official weighted core coverage metric. Low line recall is expected when a
10-chunk retriever is scored against occasionally coarse trajectory regions; comparisons
remain paired on identical cases and context limits.

## Official SWE-Explore ranking under a 500-line budget

| Variant | nDCG@500 | Recall@500 | First useful hit |
| --- | ---: | ---: | ---: |
| No rerank | 61.9% | 3.7% | 74.6% |
| Dedicated rerank, 50 candidates | 68.7% | 5.5% | 81.5% |
| Chat only, adaptive, 20 candidates | 72.8% | 4.4% | 83.1% |
| Chat only, adaptive, 50 candidates | 76.3% | 4.7% | 83.8% |
| Dedicated 50 → chat 20 | 79.7% | 5.8% | 88.5% |

## Operational evidence

| Variant | Mean latency | Returned chars | Successful chat calls | Reported chat tokens |
| --- | ---: | ---: | ---: | ---: |
| No rerank | 2.864 s | 22,237 | 0 | 0 |
| Dedicated rerank, 50 candidates | 3.945 s | 27,865 | 0 | 0 |
| Chat only, adaptive, 20 candidates | 5.287 s | 23,027 | 8 | 68,981 |
| Chat only, adaptive, 50 candidates | 9.503 s | 22,636 | 5 | 77,611 |
| Dedicated 50 → chat 20 | 7.673 s | 27,684 | 8 | 76,672 |

The adaptive route attempted chat reranking for 11 of 13 issue-style queries. Service logs
showed 3 timeouts at 20 candidates, 6 at 50 candidates, and 3 in the cascade. Token metrics
cover successful responses only; a provider may still bill work that the local 15-second
deadline cancels. Dedicated-reranker usage does not expose token counts.

A repeated chat-20 run changed edit Top-1 from 30.8% to 23.1% and edit region recall from
34.6% to 42.3%, while its core metrics stayed unchanged. This is enough nondeterminism to
require repeated runs before drawing small-difference conclusions.

## Decision

- Keep both optional rerankers disabled by default. The sample is too small to change a
  product-wide data-egress, latency, and cost boundary.
- Use the dedicated reranker as the first interactive opt-in. It improved official line
  precision/recall, file/region hit rate, ranking, and context efficiency for about 1.1
  seconds mean overhead in this run.
- Treat dedicated-50 → adaptive-chat-20 as a quality-first mode. It led most context and
  ranking metrics, but mean latency was about 2.7 times the no-rerank baseline and successful
  chat responses alone consumed about 5,900 tokens per issue.
- Do not recommend chat-only as the general default. The tested model was slower, timed out
  frequently, and was less stable; the 50-candidate window did improve edit Top-1/MRR, so it
  remains a valid quality-oriented experiment rather than a value to delete.
- Before changing behavioral defaults, repeat candidate variants and run the full
  `verified` profile. Keep downstream agent task success as a separate experiment.

Raw result JSON remains outside the repository because it is a local experimental artifact.
This report retains aggregate evidence and limitations without embedding dataset text or
retrieved source code.

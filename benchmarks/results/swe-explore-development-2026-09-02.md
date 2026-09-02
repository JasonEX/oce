# Adaptive rerank development study — 2026-09-02

This is a development observation, not a release gate or a claim about all repositories,
models, or providers. It evaluates the production OCE server through the production Rust
client and keeps failures, timeouts, latency, and model usage visible.

The study uses two complementary query sets:

- 13 issue-resolution queries from the deterministic `development` slice of
  [SWE-Explore](https://arxiv.org/abs/2606.07297). The harness executes the checksum-pinned
  official evaluator and reports its line-budget ranking metrics.
- 30 short queries expanded from 10 reviewed definition anchors in the same pinned snapshots:
  10 symbol definitions, 10 concrete paths, and 10 reference lookups. This set directly
  checks intent, route, and executed-stage conformance through the local retrieval audit.

The six variants were run twice, in forward order and then reverse order. In total, 156
issue retrievals and 360 short-query retrievals completed successfully. Raw JSON, service
logs, cloned snapshots, and SQLite state remain under `~/.cache/oce`; the repository retains
only this aggregate report and the source-pinned query manifest.

## System under test

- OCE `0.2.0`, based on `ec3811b` plus the change set documented here; `oce-client 0.2.0`.
- One prewarmed personal-mode index shared by every run: 6,788 unique tracked text blobs and
  52,090 symbol occurrences across the 13 snapshots.
- Qwen3-Embedding-4B, 1,024 dimensions; process-local query-vector cache disabled.
- Qwen3-Reranker-0.6B, 50 candidates, with the default English code-search instruction.
- `deepseek-v4-flash`, 20 candidates, adaptive policy, and a 15-second end-to-end deadline.
- Query rewrite disabled; semantic chunking, exact recall, path recall, source priority,
  query decomposition, and task-aware selection enabled.

The dedicated reranker endpoint also passed a direct request smoke with the configured
instruction. This verifies provider compatibility, not the instruction's isolated quality
effect; no with/without-instruction ablation was run.

## Issue-resolution retrieval

Values are means of two runs. All 13 issue texts were classified as `compound`, so
`dedicated:adaptive` and `dedicated:always` deliberately execute the same semantic path.
Small differences between policy labels are provider variation, not a routing effect.

| Variant | nDCG@500 | First useful hit | Edit Top-1 | Core Top-1 | Mean latency | Dedicated calls/run | Chat completed/attempted | Chat tokens/run |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| No rerank | 37.6% | 50.0% | 7.7% | 38.5% | 1.416 s | 0 | 0/0 | 0 |
| Dedicated, adaptive | 61.7% | 79.2% | 23.1% | 69.2% | 4.373 s | 13 | 0/0 | 0 |
| Dedicated, always | 61.7% | 79.2% | 23.1% | 69.2% | 4.355 s | 13 | 0/0 | 0 |
| Chat only, adaptive | 62.9% | 72.3% | 23.1% | 69.2% | 7.735 s | 0 | 8/13 | 69,690 |
| Dedicated adaptive → chat adaptive | 81.1% | 90.8% | 38.5% | 92.3% | 10.112 s | 13 | 10/13 | 92,602 |
| Dedicated always → chat adaptive | 80.9% | 90.8% | 38.5% | 92.3% | 10.096 s | 13 | 10/13 | 92,734 |

Against no reranking, the dedicated reranker increased mean nDCG@500 by 24.1 percentage
points and first useful hit by 29.2 points. At case level it improved nDCG@500 on 8 of 13
issues, was unchanged on 3, and regressed on 2; it is a strong mean improvement, not a
per-query guarantee. The cascade improved nDCG@500 on 9 issues, was unchanged on 3, and
regressed on 1.

Every chat-enabled issue run attempted 13 calls. Chat-only timed out on 5 per run; the
cascade timed out on 3 per run. OCE safely retained the previous ranking in every timeout,
so all retrieval requests still succeeded. Usage metrics count completed responses only;
an upstream provider may still charge work cancelled by the local deadline.

## Short-query routing

Values are means of two post-fix runs. `Path R@10` is recall over expected paths; reference
queries may have several regex-derived source paths. All variants reached 100% intent,
route, and executed-stage conformance, and every query had at least one expected path in the
first 10 results.

| Variant | Top-1 | Hit@10 | MRR | Path R@10 | Skip rate | Mean latency | p95 latency | Model calls/run |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| No rerank | 60.0% | 100% | 0.769 | 78.4% | 100% | 419 ms | 741 ms | 0 |
| Dedicated, adaptive | 63.3% | 100% | 0.781 | 76.6% | 66.7% | 699 ms | 1,555 ms | 10 dedicated |
| Dedicated, always | 63.3% | 100% | 0.775 | 76.6% | 0% | 1,320 ms | 1,920 ms | 30 dedicated |
| Chat only, adaptive | 60.0% | 100% | 0.769 | 78.4% | 100% | 388 ms | 667 ms | 0 |
| Dedicated adaptive + chat adaptive | 63.3% | 100% | 0.781 | 76.6% | 66.7% | 718 ms | 1,543 ms | 10 dedicated |
| Dedicated always + chat adaptive | 63.3% | 100% | 0.775 | 76.6% | 0% | 1,321 ms | 1,968 ms | 30 dedicated |

For each adaptive dedicated run, the observed routes were exactly 10
`skip:exact_definition`, 10 `skip:path_evidence`, and 10 `dedicated` reference routes. Chat
adaptive made zero calls on this set. Compared with dedicated-always, dedicated-adaptive
skipped 20 of 30 calls and reduced mean latency by 47.0% without reducing Top-1 or Hit@10 in
this sample.

Reference queries expose a remaining objective trade-off. Dedicated reranking moved their
Top-1 from 0% to 10% and MRR from 0.406 to 0.442, but regex-derived expected-path recall fell
from 35.3% to 29.7%. The current evidence is too small and the reference truth too lexical to
justify reversing the planned route. A larger semantic reference set should decide whether
reference mode prioritizes the first strong occurrence, broad occurrence coverage, or offers
both selection modes.

## Interpretation and decision

- Keep `RERANK_ENABLED=false` and `LLM_RERANK_ENABLED=false` as distribution defaults. They
  are explicit authorization boundaries for sending queries and candidate source to another
  model endpoint; a development sample cannot grant that authorization for users.
- Keep `RETRIEVAL_RERANK_POLICY=adaptive` as the policy default once a dedicated endpoint is
  authorized. The short-query study directly validates its structural skips and shows a
  large latency reduction relative to `always` on this workload.
- Recommend the dedicated reranker as the first interactive opt-in. Its mean semantic gains
  were large and repeatable here, while latency was materially below either chat mode.
- Keep the bounded dedicated → chat cascade as an explicit quality-first mode. It led the
  semantic metrics, but averaged about 10.1 seconds, consumed roughly 92.6k reported chat
  tokens per 13-query run, and still timed out on 3 of 13 calls.
- Do not recommend chat-only as the normal path for the tested model. It was slower than the
  dedicated reranker, had more timeouts, and produced a lower first-useful-hit score.
- Do not add a trained router or threshold raw retrieval scores yet. The current operators
  do not share a calibrated score space, and there is not enough labeled OCE routing data.
  Exact/path evidence is deterministic, auditable, and sufficient for the current policy.
- Before changing model authorization defaults, repeat the leading variants on the 451-case
  `verified` profile and measure downstream agent task success separately.

## Relationship to external evidence

[SWE-Explore](https://arxiv.org/abs/2606.07297) motivates evaluating ranked code regions
under line budgets rather than treating repository exploration as an opaque solved/unsolved
step. [Adaptive Re-Ranking](https://arxiv.org/abs/2606.25249) and
[AgentIR](https://arxiv.org/abs/2605.25092) provide independent evidence that per-query
cascades can save substantial work, but they use trained routers or calibrated first-stage
confidence on different retrieval workloads. They support the design direction, not OCE's
particular deterministic rules.

The official
[Qwen3-Reranker model card](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B)
describes an instruction-aware yes/no relevance judge and reports typical 1–5% gains from
task instructions across its downstream evaluation. OCE now passes a code-search
instruction, but this study attributes only the complete configured system's result; it does
not claim that 1–5% locally without an instruction ablation.

## Limits

The issue slice contains only 13 Python tasks from five repositories. The routing set contains
10 Python anchors and derives reference paths lexically rather than from an LSP or semantic
graph. Both repeats used one embedding model, one dedicated reranker, one chat model, and one
provider environment. The benchmark measures retrieval output, not downstream patch success.
These constraints are why the result changes opt-in guidance and routing behavior, but not
the default data-egress authorization.

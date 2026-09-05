"""Evaluate OCE on real issue-resolution context from SWE-bench and SWE-Explore.

The source datasets are downloaded at pinned revisions and verified by SHA256. They are
kept outside the repository because SWE-Explore is distributed under CC BY-NC-ND 4.0.
This harness reports observations; it deliberately does not implement a release gate.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from typing import Any, cast

from benchmarks.blackbox.corpus import prepare_snapshots, snapshot_path
from benchmarks.blackbox.harness import (
    DEFAULT_WORKDIR,
    RetrievedRegion,
    admin_stats,
    ensure_comparable,
    model_usage_delta,
    parse_retrieved_regions,
    percentile,
    resolve_client_binary,
    run_client,
    runtime_metadata,
)
from benchmarks.blackbox.prewarm import prewarm_snapshots
from benchmarks.blackbox.swe_data import (
    SWE_BENCH_REVISION,
    SWE_EXPLORE_METRICS_REVISION,
    SWE_EXPLORE_REVISION,
    BenchmarkCase,
    Region,
    load_cases,
    prepare_datasets,
    snapshots_for_cases,
)

SWE_EXPLORE_METRIC_NAMES = (
    "precision",
    "recall",
    "f1_score",
    "hit_file_rate",
    "noise_file_rate",
    "hit_region_rate",
    "noise_region_rate",
    "weighted_core_coverage",
    "context_efficiency",
    "ndcg_at_100",
    "ndcg_at_300",
    "ndcg_at_500",
    "recall_at_100",
    "recall_at_300",
    "recall_at_500",
    "first_useful_hit",
)
MetricComputer = Callable[
    [list[tuple[str, int, int]], dict[str, object], Path | None],
    tuple[dict[str, float], dict[str, Any]],
]


def load_official_metric_computer(workdir: Path) -> MetricComputer:
    """Load the checksum-pinned SWE-Explore reference metric implementation."""
    _verified, _explore, path = prepare_datasets(workdir)
    spec = importlib.util.spec_from_file_location("oce_swe_explore_metrics", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load SWE-Explore metrics from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    computer = getattr(module, "compute_region_metrics", None)
    if not callable(computer):
        raise RuntimeError("SWE-Explore metrics module lacks compute_region_metrics")
    return cast(MetricComputer, computer)


def prewarm_index(args: argparse.Namespace) -> dict[str, object]:
    workdir = args.workdir.expanduser().resolve()
    cases = load_cases(workdir, args.profile)
    if args.limit is not None:
        cases = cases[: args.limit]
    binary = resolve_client_binary(args.client_binary)
    api_key = os.environ.get("OCE_API_KEY", "sk-opencontextengine")
    result = prewarm_snapshots(
        snapshots_for_cases(cases),
        workdir=workdir,
        binary=binary,
        api_url=args.api_url,
        api_key=api_key,
        max_batch_blobs=args.max_batch_blobs,
        max_batch_bytes=args.max_batch_bytes,
        timeout_seconds=args.timeout_seconds,
    )
    return {
        "profile": args.profile,
        "cases": len(cases),
        **result,
    }


def _overlaps(left: Region, right: RetrievedRegion) -> bool:
    return (
        left.path == right.path and left.start <= right.end and right.start <= left.end
    )


def _file_metrics(
    paths: set[str], retrieved: Sequence[RetrievedRegion]
) -> dict[str, float]:
    ranked_paths = [hit.path for hit in retrieved[:10]]
    first = next(
        (rank for rank, path in enumerate(ranked_paths, 1) if path in paths), None
    )
    return {
        "top1": float(bool(ranked_paths and ranked_paths[0] in paths)),
        "file_recall_at_10": len(paths.intersection(set(ranked_paths))) / len(paths),
        "mrr": 0.0 if first is None else 1.0 / first,
    }


def score_case(
    case: BenchmarkCase, retrieved: Sequence[RetrievedRegion]
) -> dict[str, float]:
    top_ten = retrieved[:10]
    edit = _file_metrics({region.path for region in case.edit_regions}, top_ten)
    core = _file_metrics(set(case.core_files), top_ten)
    edit["region_recall_at_10"] = fmean(
        float(any(_overlaps(region, hit) for hit in top_ten))
        for region in case.edit_regions
    )
    core["region_recall_at_10"] = (
        fmean(
            float(any(_overlaps(region, hit) for hit in top_ten))
            for region in case.core_regions
        )
        if case.core_regions
        else 0.0
    )
    return {f"edit_{key}": value for key, value in edit.items()} | {
        f"core_{key}": value for key, value in core.items()
    }


def score_swe_explore(
    case: BenchmarkCase,
    retrieved: Sequence[RetrievedRegion],
    repo_dir: Path,
    computer: MetricComputer,
) -> dict[str, float]:
    """Score with the official SWE-Explore reference implementation."""
    if case.explore_truth is None:
        raise ValueError(f"{case.instance_id} has no SWE-Explore ground truth")
    predictions = [(item.path, item.start, item.end) for item in retrieved]
    metrics, diagnostics = computer(predictions, case.explore_truth, repo_dir)
    if diagnostics:
        raise RuntimeError(
            f"SWE-Explore metric diagnostics for {case.instance_id}: {diagnostics}"
        )
    missing = set(SWE_EXPLORE_METRIC_NAMES) - metrics.keys()
    if missing:
        raise RuntimeError(f"SWE-Explore metrics missing fields: {sorted(missing)}")
    return {
        f"swe_explore_{name}": float(metrics[name]) for name in SWE_EXPLORE_METRIC_NAMES
    }


def _aggregate(results: Sequence[dict[str, object]]) -> dict[str, object]:
    metric_names = (
        "edit_top1",
        "edit_file_recall_at_10",
        "edit_region_recall_at_10",
        "edit_mrr",
        "core_top1",
        "core_file_recall_at_10",
        "core_region_recall_at_10",
        "core_mrr",
        *(f"swe_explore_{name}" for name in SWE_EXPLORE_METRIC_NAMES),
    )
    successful = [result for result in results if result["status"] == "ok"]
    return {
        "cases": len(results),
        "successful_cases": len(successful),
        "error_cases": len(results) - len(successful),
        **{
            name: fmean(
                float(result.get("metrics", {}).get(name, 0.0)) for result in results
            )
            for name in metric_names
        },
        "mean_elapsed_ms": (
            fmean(float(result["elapsed_ms"]) for result in successful)
            if successful
            else None
        ),
        # An agent waits on the slow tail, not the mean; both percentiles are
        # part of the acceptance line for any default change.
        "p50_elapsed_ms": percentile([int(r["elapsed_ms"]) for r in successful], 50),
        "p95_elapsed_ms": percentile([int(r["elapsed_ms"]) for r in successful], 95),
        "mean_returned_chars": fmean(
            float(result.get("returned_chars", 0)) for result in results
        ),
    }


def run_benchmark(args: argparse.Namespace) -> dict[str, object]:
    workdir = args.workdir.expanduser().resolve()
    cases = load_cases(workdir, args.profile)
    if args.limit is not None:
        cases = cases[: args.limit]
    prepare_snapshots(workdir, snapshots_for_cases(cases))
    binary = resolve_client_binary(args.client_binary)
    api_key = os.environ.get("OCE_API_KEY", "sk-opencontextengine")
    admin_key = os.environ.get("OCE_ADMIN_API_KEY")
    state_dir = workdir / "client-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    official_metric_computer = load_official_metric_computer(workdir)

    sync_results: dict[str, dict[str, object]] = {}
    sync_errors: dict[str, Exception] = {}
    for number, case in enumerate(cases, 1):
        print(f"[sync {number}/{len(cases)}] {case.instance_id}", file=sys.stderr)
        started = time.perf_counter()
        try:
            response: dict[str, object] | None = None
            for attempt in range(1, args.sync_attempts + 1):
                try:
                    response = run_client(
                        binary,
                        snapshot_path(workdir, case.instance_id),
                        state_dir / f"{case.instance_id}.sqlite3",
                        args.api_url,
                        api_key,
                        ("sync", "--json"),
                        retries=4,
                    )
                    break
                except Exception:
                    if attempt == args.sync_attempts:
                        raise
                    print(f"  retrying sync after attempt {attempt}", file=sys.stderr)
                    time.sleep(args.sync_retry_seconds)
            if response is None:  # pragma: no cover - loop invariant
                raise RuntimeError("sync completed without a response")
            uploaded = response.get("uploaded_blob_names", ())
            sync_results[case.instance_id] = {
                "status": "ok",
                "uploaded_blobs": len(uploaded) if isinstance(uploaded, list) else None,
                "attempts": attempt,
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
            }
        except Exception as exc:
            sync_errors[case.instance_id] = exc
            sync_results[case.instance_id] = {
                "status": "error",
                "error_type": type(exc).__name__,
                "error": str(exc).replace(api_key, "[REDACTED]")[:1000],
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
            }

    if sync_errors:
        failed = ", ".join(sorted(sync_errors))
        raise RuntimeError(
            f"benchmark sync incomplete for {failed}; no quality report was produced"
        )

    if admin_key:
        time.sleep(args.metrics_settle_seconds)
    stats_before = admin_stats(args.api_url, admin_key)
    results: list[dict[str, object]] = []
    for number, case in enumerate(cases, 1):
        print(f"[retrieve {number}/{len(cases)}] {case.instance_id}", file=sys.stderr)
        state_path = state_dir / f"{case.instance_id}.sqlite3"
        started = time.perf_counter()
        try:
            response = run_client(
                binary,
                snapshot_path(workdir, case.instance_id),
                state_path,
                args.api_url,
                api_key,
                ("retrieve", case.query, "--json"),
            )
            formatted = response.get("formatted_retrieval")
            elapsed_ms = response.get("elapsed_ms")
            if not isinstance(formatted, str) or not isinstance(elapsed_ms, int):
                raise RuntimeError("oce-client retrieve returned an invalid payload")
            retrieved = parse_retrieved_regions(formatted)
            results.append(
                {
                    "instance_id": case.instance_id,
                    "repo": case.repo,
                    "base_commit": case.base_commit,
                    "status": "ok",
                    "retrieved": [asdict(item) for item in retrieved],
                    "metrics": score_case(case, retrieved)
                    | score_swe_explore(
                        case,
                        retrieved,
                        snapshot_path(workdir, case.instance_id),
                        official_metric_computer,
                    ),
                    "elapsed_ms": elapsed_ms,
                    "wall_elapsed_ms": int((time.perf_counter() - started) * 1000),
                    "returned_chars": len(formatted),
                }
            )
        except Exception as exc:
            message = str(exc).replace(api_key, "[REDACTED]")
            results.append(
                {
                    "instance_id": case.instance_id,
                    "repo": case.repo,
                    "base_commit": case.base_commit,
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "error": message[:1000],
                    "wall_elapsed_ms": int((time.perf_counter() - started) * 1000),
                }
            )

    if admin_key:
        time.sleep(args.metrics_settle_seconds)
    stats_after = admin_stats(args.api_url, admin_key)
    summary = _aggregate(results)
    summary["external_model_tokens"] = model_usage_delta(stats_before, stats_after)
    return {
        "schema_version": 3,
        "suite": "swe_explore",
        "label": args.label,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "profile": args.profile,
        "controls": {
            "metrics_settle_seconds": args.metrics_settle_seconds,
            "sync_attempts": args.sync_attempts,
            "sync_retry_seconds": args.sync_retry_seconds,
        },
        "source_revisions": {
            "swe_bench_verified": SWE_BENCH_REVISION,
            "swe_explore": SWE_EXPLORE_REVISION,
            "swe_explore_metrics": SWE_EXPLORE_METRICS_REVISION,
        },
        "runtime": runtime_metadata(
            binary=binary,
            api_url=args.api_url,
            admin_key=admin_key,
            extra_metadata=args.metadata,
        ),
        "case_ids": [case.instance_id for case in cases],
        "sync": sync_results,
        "summary": summary,
        "cases": results,
    }


def _percent(value: object) -> str:
    return "-" if value is None else f"{float(value) * 100:.1f}%"


def _number(value: object) -> str:
    return "-" if value is None else f"{float(value):.0f}"


def compare(paths: Iterable[Path]) -> str:
    # Head-of-list quality (Top-1, nDCG@100) sits next to the line-budget
    # metrics on purpose: a change that lifts recall while pushing the answer
    # out of the first slots must be visible in the same row.
    headers = (
        "Variant",
        "OK",
        "Edit Top-1",
        "Core Top-1",
        "Edit file R@10",
        "Edit region R@10",
        "Core file R@10",
        "Core region R@10",
        "SWE line R",
        "SWE nDCG@100",
        "SWE nDCG@500",
        "SWE first hit",
        "SWE efficiency",
        "Latency ms",
        "p50 ms",
        "p95 ms",
        "Chars",
    )
    loaded = [(path, json.loads(path.read_text(encoding="utf-8"))) for path in paths]
    ensure_comparable([value for _path, value in loaded], suite="swe_explore")
    rows: list[tuple[str, ...]] = []
    for path, value in loaded:
        summary = value["summary"]
        rows.append(
            (
                str(value.get("label", path.stem)),
                f"{summary['successful_cases']}/{summary['cases']}",
                _percent(summary["edit_top1"]),
                _percent(summary.get("core_top1")),
                _percent(summary["edit_file_recall_at_10"]),
                _percent(summary["edit_region_recall_at_10"]),
                _percent(summary["core_file_recall_at_10"]),
                _percent(summary["core_region_recall_at_10"]),
                _percent(summary.get("swe_explore_recall")),
                _percent(summary.get("swe_explore_ndcg_at_100")),
                _percent(summary.get("swe_explore_ndcg_at_500")),
                _percent(summary.get("swe_explore_first_useful_hit")),
                _percent(summary.get("swe_explore_context_efficiency")),
                _number(summary.get("mean_elapsed_ms")),
                _number(summary.get("p50_elapsed_ms")),
                _number(summary.get("p95_elapsed_ms")),
                _number(summary.get("mean_returned_chars")),
            )
        )
    output = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    output.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(output)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, default=DEFAULT_WORKDIR)
    parser.add_argument(
        "--profile",
        choices=("pilot", "development", "standard", "verified"),
        default="development",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser(
        "prepare", help="download data and create Git snapshots"
    )
    prepare.set_defaults(handler="prepare")

    prewarm = subparsers.add_parser(
        "prewarm", help="serially upload small batches before a retrieval experiment"
    )
    prewarm.add_argument("--api-url", default="http://127.0.0.1:8986")
    prewarm.add_argument("--client-binary", type=Path)
    prewarm.add_argument("--limit", type=int)
    prewarm.add_argument("--max-batch-blobs", type=int, default=16)
    prewarm.add_argument("--max-batch-bytes", type=int, default=256 * 1024)
    prewarm.add_argument("--timeout-seconds", type=float, default=600.0)
    prewarm.set_defaults(handler="prewarm")

    run = subparsers.add_parser("run", help="sync snapshots and execute retrieval")
    run.add_argument("--api-url", default="http://127.0.0.1:8986")
    run.add_argument("--client-binary", type=Path)
    run.add_argument("--label", required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--limit", type=int)
    run.add_argument("--metrics-settle-seconds", type=float, default=6.0)
    run.add_argument("--sync-attempts", type=int, default=4)
    run.add_argument("--sync-retry-seconds", type=float, default=10.0)
    run.add_argument(
        "--metadata",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="record non-secret runtime metadata; may be repeated",
    )
    run.set_defaults(handler="run")

    compare_parser = subparsers.add_parser("compare", help="compare result JSON files")
    compare_parser.add_argument("results", type=Path, nargs="+")
    compare_parser.set_defaults(handler="compare")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.handler == "prepare":
        workdir = args.workdir.expanduser().resolve()
        cases = load_cases(workdir, args.profile)
        prepare_snapshots(workdir, snapshots_for_cases(cases))
        print(
            json.dumps(
                {
                    "profile": args.profile,
                    "cases": len(cases),
                    "repositories": sorted({case.repo for case in cases}),
                    "workdir": str(workdir),
                },
                indent=2,
            )
        )
        return 0
    if args.handler == "prewarm":
        if args.limit is not None and args.limit < 1:
            raise ValueError("--limit must be positive")
        if (
            args.max_batch_blobs < 1
            or args.max_batch_bytes < 1
            or args.timeout_seconds <= 0
        ):
            raise ValueError("prewarm batch and timeout settings must be positive")
        print(json.dumps(prewarm_index(args), indent=2))
        return 0
    if args.handler == "run":
        if args.limit is not None and args.limit < 1:
            raise ValueError("--limit must be positive")
        if (
            args.metrics_settle_seconds < 0
            or args.sync_attempts < 1
            or args.sync_retry_seconds < 0
        ):
            raise ValueError(
                "settle/retry settings must be non-negative and attempts positive"
            )
        result = run_benchmark(args)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result["summary"], indent=2))
        return 0
    print(compare(args.results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

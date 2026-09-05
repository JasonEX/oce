"""External sanity guard: docstring-to-function retrieval on pinned CodeSearchNet repos.

The suite asks the natural-language description of a function and checks
whether the returned regions reach the function's own lines. It exists to
catch a regression in plain semantic retrieval that the relation-oriented
project cases would not notice; it is not a project-level precision claim.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean

from benchmarks.blackbox.corpus import (
    LANGUAGES,
    RepositorySnapshot,
    load_corpus,
    prepare_snapshots,
    snapshot_path,
)
from benchmarks.blackbox.csn_data import (
    FunctionCase,
    select_cases,
    write_manifest,
)
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
    sha256_file,
)
from benchmarks.blackbox.prewarm import prewarm_snapshots

DEFAULT_MANIFEST = Path(__file__).with_name("csn_cases.json")
METRIC_NAMES = (
    "region_top1",
    "region_hit_at_5",
    "region_hit_at_10",
    "region_mrr",
    "file_top1",
    "file_hit_at_10",
)


def load_manifest(
    path: Path,
) -> tuple[dict[str, RepositorySnapshot], tuple[FunctionCase, ...]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or not isinstance(value.get("snapshots"), list)
        or not isinstance(value.get("cases"), list)
    ):
        raise ValueError("CodeSearchNet manifest must use schema_version 1")
    snapshots = {
        snapshot.id: snapshot for snapshot in load_corpus_payload(value["snapshots"])
    }
    cases: list[FunctionCase] = []
    seen: set[str] = set()
    for raw in value["cases"]:
        case = FunctionCase(**raw)
        if (
            case.id in seen
            or case.instance_id not in snapshots
            or not case.query
            or case.start < 1
            or case.end < case.start
        ):
            raise ValueError(f"invalid or duplicate CodeSearchNet case: {case.id!r}")
        seen.add(case.id)
        cases.append(case)
    if not cases:
        raise ValueError("CodeSearchNet manifest is empty")
    return snapshots, tuple(cases)


def load_corpus_payload(raw: list[object]) -> tuple[RepositorySnapshot, ...]:
    """Validate embedded snapshots with the corpus rules without a second file."""
    scratch = Path(os.environ.get("TMPDIR", "/tmp")) / "oce-csn-snapshots.json"
    scratch.write_text(
        json.dumps({"schema_version": 1, "snapshots": raw}), encoding="utf-8"
    )
    try:
        return load_corpus(scratch)
    finally:
        scratch.unlink(missing_ok=True)


def _covers(region: RetrievedRegion, case: FunctionCase) -> bool:
    return (
        region.path == case.path
        and region.start <= case.end
        and region.end >= case.start
    )


def score_case(
    case: FunctionCase, retrieved: Sequence[RetrievedRegion]
) -> dict[str, float]:
    region_rank = next(
        (rank for rank, region in enumerate(retrieved, 1) if _covers(region, case)),
        None,
    )
    files = list(dict.fromkeys(region.path for region in retrieved))
    file_rank = next(
        (rank for rank, path in enumerate(files, 1) if path == case.path), None
    )
    return {
        "region_top1": float(region_rank == 1),
        "region_hit_at_5": float(region_rank is not None and region_rank <= 5),
        "region_hit_at_10": float(region_rank is not None and region_rank <= 10),
        "region_mrr": 0.0 if region_rank is None else 1.0 / region_rank,
        "file_top1": float(file_rank == 1),
        "file_hit_at_10": float(file_rank is not None and file_rank <= 10),
    }


def aggregate(results: Sequence[dict[str, object]]) -> dict[str, object]:
    successful = [result for result in results if result["status"] == "ok"]
    elapsed = [int(result["elapsed_ms"]) for result in successful]
    return {
        "cases": len(results),
        "successful_cases": len(successful),
        "error_cases": len(results) - len(successful),
        **{
            name: (
                fmean(
                    float(dict(result.get("metrics", {})).get(name, 0.0))  # type: ignore[arg-type]
                    for result in results
                )
                if results
                else None
            )
            for name in METRIC_NAMES
        },
        "mean_elapsed_ms": fmean(elapsed) if elapsed else None,
        "p50_elapsed_ms": percentile(elapsed, 50),
        "p95_elapsed_ms": percentile(elapsed, 95),
        "mean_returned_chars": (
            fmean(float(result.get("returned_chars", 0)) for result in results)  # type: ignore[arg-type]
            if results
            else None
        ),
    }


def _reachable(repo: str) -> bool:
    completed = subprocess.run(
        ["git", "ls-remote", "--exit-code", f"https://github.com/{repo}.git", "HEAD"],
        capture_output=True,
        timeout=120,
    )
    return completed.returncode == 0


def select_manifest(args: argparse.Namespace) -> dict[str, object]:
    reachable: Callable[[str], bool] = _reachable
    snapshots, cases = select_cases(
        args.workdir.expanduser().resolve(),
        repositories_per_language=args.repositories_per_language,
        cases_per_repository=args.cases_per_repository,
        reachable=reachable,
    )
    write_manifest(args.manifest.expanduser().resolve(), snapshots, cases)
    return {
        "snapshots": len(snapshots),
        "cases": len(cases),
        "languages": dict(Counter(snapshot.code_language for snapshot in snapshots)),
        "name_in_query": sum(case.name_in_query for case in cases),
    }


def prepare(workdir: Path, manifest: Path):
    snapshots, cases = load_manifest(manifest)
    prepare_snapshots(workdir, tuple(snapshots.values()))
    for case in cases:
        file = snapshot_path(workdir, case.instance_id) / case.path
        if not file.is_file():
            raise ValueError(f"{case.id}: missing {case.path}")
        line_count = len(
            file.read_text(encoding="utf-8", errors="replace").splitlines()
        )
        if case.end > line_count:
            raise ValueError(f"{case.id}: {case.path} ends after line {line_count}")
    return snapshots, cases


def run_benchmark(args: argparse.Namespace) -> dict[str, object]:
    workdir = args.workdir.expanduser().resolve()
    manifest = args.manifest.expanduser().resolve()
    snapshots, cases = prepare(workdir, manifest)
    binary = resolve_client_binary(args.client_binary)
    api_key = os.environ.get("OCE_API_KEY", "sk-opencontextengine")
    admin_key = os.environ.get("OCE_ADMIN_API_KEY")
    state_dir = workdir / "csn-client-state"
    state_dir.mkdir(parents=True, exist_ok=True)

    sync: dict[str, dict[str, object]] = {}
    for number, (instance_id, snapshot) in enumerate(snapshots.items(), 1):
        print(f"[sync {number}/{len(snapshots)}] {instance_id}", file=sys.stderr)
        response = run_client(
            binary,
            snapshot_path(workdir, snapshot.id),
            state_dir / f"{instance_id}.sqlite3",
            args.api_url,
            api_key,
            ("sync", "--json"),
            retries=4,
        )
        uploaded = response.get("uploaded_blob_names", ())
        sync[instance_id] = {
            "status": "ok",
            "uploaded_blobs": len(uploaded) if isinstance(uploaded, list) else None,
        }

    if admin_key:
        time.sleep(args.metrics_settle_seconds)
    stats_before = admin_stats(args.api_url, admin_key)
    results: list[dict[str, object]] = []
    for number, case in enumerate(cases, 1):
        print(f"[retrieve {number}/{len(cases)}] {case.id}", file=sys.stderr)
        started = time.perf_counter()
        base: dict[str, object] = {
            "id": case.id,
            "instance_id": case.instance_id,
            "code_language": snapshots[case.instance_id].code_language,
            "name_in_query": case.name_in_query,
        }
        try:
            response = run_client(
                binary,
                snapshot_path(workdir, case.instance_id),
                state_dir / f"{case.instance_id}.sqlite3",
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
                    **base,
                    "status": "ok",
                    "retrieved": [asdict(region) for region in retrieved],
                    "metrics": score_case(case, retrieved),
                    "returned_chars": len(formatted),
                    "hit_count": len(retrieved),
                    "elapsed_ms": elapsed_ms,
                    "wall_elapsed_ms": int((time.perf_counter() - started) * 1000),
                }
            )
        except Exception as exc:
            results.append(
                {
                    **base,
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "error": str(exc).replace(api_key, "[REDACTED]")[:1000],
                    "wall_elapsed_ms": int((time.perf_counter() - started) * 1000),
                }
            )

    if admin_key:
        time.sleep(args.metrics_settle_seconds)
    stats_after = admin_stats(args.api_url, admin_key)
    summary = aggregate(results)
    summary["external_model_tokens"] = model_usage_delta(stats_before, stats_after)
    languages = [
        language
        for language in LANGUAGES
        if any(result["code_language"] == language for result in results)
    ]
    return {
        "schema_version": 1,
        "suite": "csn_queries",
        "label": args.label,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "controls": {"metrics_settle_seconds": args.metrics_settle_seconds},
        "source_revisions": {"csn_cases_sha256": sha256_file(manifest)},
        "snapshots": [asdict(snapshot) for snapshot in snapshots.values()],
        "runtime": runtime_metadata(
            binary=binary,
            api_url=args.api_url,
            admin_key=admin_key,
            extra_metadata=args.metadata,
        ),
        "case_ids": [case.id for case in cases],
        "sync": sync,
        "summary": summary,
        "by_code_language": {
            language: aggregate(
                [result for result in results if result["code_language"] == language]
            )
            for language in languages
        },
        "by_name_in_query": {
            label: aggregate(
                [result for result in results if result["name_in_query"] is flag]
            )
            for label, flag in (("named", True), ("described", False))
        },
        "cases": results,
    }


def _percent(value: object) -> str:
    return "-" if value is None else f"{float(value) * 100:.1f}%"


def _number(value: object) -> str:
    return "-" if value is None else f"{float(value):.0f}"


def compare(paths: Iterable[Path]) -> str:
    loaded = [(path, json.loads(path.read_text(encoding="utf-8"))) for path in paths]
    ensure_comparable([value for _path, value in loaded], suite="csn_queries")
    languages = [
        language
        for language in LANGUAGES
        if language in loaded[0][1].get("by_code_language", {})
    ]
    headers = (
        "Variant",
        "OK",
        "Region Top-1",
        "Region Hit@5",
        "Region Hit@10",
        "Region MRR",
        "File Top-1",
        "Described Hit@10",
        *(f"{language} Hit@10" for language in languages),
        "Chars",
        "p50 ms",
        "p95 ms",
    )
    rows: list[tuple[str, ...]] = []
    for path, value in loaded:
        summary = value["summary"]
        rows.append(
            (
                str(value.get("label", path.stem)),
                f"{summary['successful_cases']}/{summary['cases']}",
                _percent(summary["region_top1"]),
                _percent(summary["region_hit_at_5"]),
                _percent(summary["region_hit_at_10"]),
                "-"
                if summary["region_mrr"] is None
                else f"{float(summary['region_mrr']):.3f}",
                _percent(summary["file_top1"]),
                _percent(value["by_name_in_query"]["described"]["region_hit_at_10"]),
                *(
                    _percent(value["by_code_language"][language]["region_hit_at_10"])
                    for language in languages
                ),
                _number(summary["mean_returned_chars"]),
                _number(summary["p50_elapsed_ms"]),
                _number(summary["p95_elapsed_ms"]),
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
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    subparsers = parser.add_subparsers(dest="command", required=True)

    select = subparsers.add_parser(
        "select", help="regenerate the manifest from the pinned parquet files"
    )
    select.add_argument("--repositories-per-language", type=int, default=2)
    select.add_argument("--cases-per-repository", type=int, default=10)
    select.set_defaults(handler="select")

    check = subparsers.add_parser("check", help="prepare snapshots and validate truth")
    check.set_defaults(handler="check")

    prewarm = subparsers.add_parser("prewarm", help="upload the snapshots in batches")
    prewarm.add_argument("--api-url", default="http://127.0.0.1:8986")
    prewarm.add_argument("--client-binary", type=Path)
    prewarm.add_argument("--max-batch-blobs", type=int, default=16)
    prewarm.add_argument("--max-batch-bytes", type=int, default=256 * 1024)
    prewarm.add_argument("--timeout-seconds", type=float, default=600.0)
    prewarm.set_defaults(handler="prewarm")

    run = subparsers.add_parser("run", help="sync snapshots and execute the cases")
    run.add_argument("--api-url", default="http://127.0.0.1:8986")
    run.add_argument("--client-binary", type=Path)
    run.add_argument("--metrics-settle-seconds", type=float, default=6.0)
    run.add_argument("--label", required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--metadata", action="append", default=[], metavar="KEY=VALUE")
    run.set_defaults(handler="run")

    comparison = subparsers.add_parser("compare", help="compare paired result files")
    comparison.add_argument("results", type=Path, nargs="+")
    comparison.set_defaults(handler="compare")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.handler == "select":
        print(json.dumps(select_manifest(args), indent=2))
        return 0
    if args.handler == "check":
        snapshots, cases = prepare(
            args.workdir.expanduser().resolve(), args.manifest.expanduser().resolve()
        )
        print(
            json.dumps(
                {
                    "cases": len(cases),
                    "snapshots": len(snapshots),
                    "languages": dict(
                        Counter(
                            snapshot.code_language for snapshot in snapshots.values()
                        )
                    ),
                    "name_in_query": sum(case.name_in_query for case in cases),
                },
                indent=2,
            )
        )
        return 0
    if args.handler == "prewarm":
        workdir = args.workdir.expanduser().resolve()
        snapshots, _cases = load_manifest(args.manifest.expanduser().resolve())
        result = prewarm_snapshots(
            tuple(snapshots.values()),
            workdir=workdir,
            binary=resolve_client_binary(args.client_binary),
            api_url=args.api_url,
            api_key=os.environ.get("OCE_API_KEY", "sk-opencontextengine"),
            max_batch_blobs=args.max_batch_blobs,
            max_batch_bytes=args.max_batch_bytes,
            timeout_seconds=args.timeout_seconds,
        )
        print(json.dumps(result, indent=2))
        return 0
    if args.handler == "run":
        if args.metrics_settle_seconds < 0:
            raise ValueError("--metrics-settle-seconds must not be negative")
        result = run_benchmark(args)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result["summary"], indent=2))
        return 0
    print(compare(args.results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

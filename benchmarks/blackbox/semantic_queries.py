"""Evaluate reviewed feature, overview, and call-chain queries as a black box."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from typing import Literal, cast

from benchmarks.blackbox.corpus import (
    LANGUAGE_LABELS,
    LANGUAGES,
    CodeLanguage,
    RepositorySnapshot,
    load_corpus,
    prepare_snapshots,
    select_snapshots,
    snapshot_path,
)
from benchmarks.blackbox.harness import (
    DEFAULT_WORKDIR,
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

DEFAULT_CASES = Path(__file__).with_name("semantic_cases.json")
DEFAULT_CORPUS = Path(__file__).with_name("curated_corpus.json")
QueryKind = Literal["feature", "overview", "call_chain"]
_KINDS: tuple[QueryKind, ...] = ("feature", "overview", "call_chain")


@dataclass(frozen=True)
class RelevantPath:
    path: str
    grade: int
    role: str


@dataclass(frozen=True)
class SemanticCase:
    id: str
    instance_id: str
    kind: QueryKind
    query: str
    rationale: str
    relevant_paths: tuple[RelevantPath, ...]


def load_manifest(path: Path) -> tuple[SemanticCase, ...]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or not isinstance(value.get("cases"), list)
    ):
        raise ValueError("semantic case manifest must use schema_version 1")

    cases: list[SemanticCase] = []
    seen_ids: set[str] = set()
    for raw in value["cases"]:
        if not isinstance(raw, dict) or not isinstance(raw.get("relevant_paths"), list):
            raise ValueError("invalid semantic case")
        kind = str(raw.get("kind"))
        if kind not in _KINDS:
            raise ValueError(f"unsupported semantic query kind: {kind!r}")
        paths: list[RelevantPath] = []
        seen_paths: set[str] = set()
        for item in raw["relevant_paths"]:
            if not isinstance(item, dict):
                raise ValueError("invalid relevant path")
            relevant = RelevantPath(
                path=str(item.get("path", "")),
                grade=int(item.get("grade", 0)),
                role=str(item.get("role", "")),
            )
            if (
                not relevant.path
                or relevant.grade not in {1, 2, 3}
                or not relevant.role
                or relevant.path in seen_paths
            ):
                raise ValueError(
                    f"invalid or duplicate relevant path: {relevant.path!r}"
                )
            seen_paths.add(relevant.path)
            paths.append(relevant)
        case = SemanticCase(
            id=str(raw.get("id", "")),
            instance_id=str(raw.get("instance_id", "")),
            kind=cast(QueryKind, kind),
            query=str(raw.get("query", "")),
            rationale=str(raw.get("rationale", "")),
            relevant_paths=tuple(paths),
        )
        if (
            not case.id
            or case.id in seen_ids
            or not case.instance_id
            or not case.query
            or not case.rationale
            or not case.relevant_paths
            or max(item.grade for item in case.relevant_paths) != 3
        ):
            raise ValueError(f"invalid or duplicate semantic case: {case.id!r}")
        seen_ids.add(case.id)
        cases.append(case)
    if not cases:
        raise ValueError("semantic case manifest is empty")
    return tuple(cases)


def prepare_cases(
    workdir: Path, manifest: Path, corpus_path: Path
) -> tuple[tuple[SemanticCase, ...], dict[str, RepositorySnapshot]]:
    cases = load_manifest(manifest)
    selected = select_snapshots(
        load_corpus(corpus_path), (case.instance_id for case in cases)
    )
    prepare_snapshots(workdir, tuple(selected.values()))
    for case in cases:
        root = snapshot_path(workdir, case.instance_id)
        missing_paths = [
            item.path
            for item in case.relevant_paths
            if not (root / item.path).is_file()
        ]
        if missing_paths:
            raise ValueError(f"{case.id} has missing relevant paths: {missing_paths}")
    return cases, selected


def _dcg(grades: Sequence[int]) -> float:
    return sum(
        (2**grade - 1) / math.log2(rank + 1) for rank, grade in enumerate(grades, 1)
    )


def score_paths(
    relevant: Sequence[RelevantPath], retrieved: Sequence[str]
) -> dict[str, float]:
    grades = {item.path: item.grade for item in relevant}
    ranked = list(dict.fromkeys(retrieved[:10]))
    ranked_grades = [grades.get(path, 0) for path in ranked]
    first_rank = next(
        (rank for rank, grade in enumerate(ranked_grades, 1) if grade), None
    )
    total_weight = sum(grades.values())
    ideal = sorted(grades.values(), reverse=True)
    primary = {path for path, grade in grades.items() if grade == 3}
    return {
        "top1_relevant": float(bool(ranked_grades) and ranked_grades[0] > 0),
        "top1_primary": float(bool(ranked) and ranked[0] in primary),
        "mrr": 0.0 if first_rank is None else 1.0 / first_rank,
        "hit_at_5": float(any(ranked_grades[:5])),
        "weighted_recall_at_5": sum(ranked_grades[:5]) / total_weight,
        "weighted_recall_at_10": sum(ranked_grades) / total_weight,
        "primary_recall_at_10": len(primary.intersection(ranked)) / len(primary),
        "ndcg_at_10": _dcg(ranked_grades) / _dcg(ideal[:10]),
    }


def aggregate(results: Sequence[dict[str, object]]) -> dict[str, object]:
    successful = [result for result in results if result["status"] == "ok"]
    metric_names = (
        "top1_relevant",
        "top1_primary",
        "mrr",
        "hit_at_5",
        "weighted_recall_at_5",
        "weighted_recall_at_10",
        "primary_recall_at_10",
        "ndcg_at_10",
    )
    elapsed = [int(result["elapsed_ms"]) for result in successful]
    return {
        "cases": len(results),
        "successful_cases": len(successful),
        "error_cases": len(results) - len(successful),
        **{
            name: (
                fmean(
                    float(result.get("metrics", {}).get(name, 0.0))
                    for result in results
                )
                if results
                else None
            )
            for name in metric_names
        },
        "mean_elapsed_ms": fmean(elapsed) if elapsed else None,
        "p50_elapsed_ms": percentile(elapsed, 50),
        "p95_elapsed_ms": percentile(elapsed, 95),
        "mean_returned_chars": (
            fmean(float(result.get("returned_chars", 0)) for result in results)
            if results
            else None
        ),
        "mean_hit_count": (
            fmean(float(result.get("hit_count", 0)) for result in results)
            if results
            else None
        ),
    }


def run_benchmark(args: argparse.Namespace) -> dict[str, object]:
    workdir = args.workdir.expanduser().resolve()
    manifest = args.cases.expanduser().resolve()
    corpus_path = args.corpus.expanduser().resolve()
    cases, snapshots = prepare_cases(workdir, manifest, corpus_path)
    binary = resolve_client_binary(args.client_binary)
    api_key = os.environ.get("OCE_API_KEY", "sk-opencontextengine")
    admin_key = os.environ.get("OCE_ADMIN_API_KEY")
    state_dir = workdir / "semantic-client-state"
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
                    "id": case.id,
                    "instance_id": case.instance_id,
                    "kind": case.kind,
                    "code_language": snapshots[case.instance_id].code_language,
                    "status": "ok",
                    "relevant_path_count": len(case.relevant_paths),
                    "retrieved": [asdict(region) for region in retrieved],
                    "metrics": score_paths(
                        case.relevant_paths, [region.path for region in retrieved]
                    ),
                    "returned_chars": len(formatted),
                    "hit_count": len(retrieved),
                    "elapsed_ms": elapsed_ms,
                    "wall_elapsed_ms": int((time.perf_counter() - started) * 1000),
                }
            )
        except Exception as exc:
            results.append(
                {
                    "id": case.id,
                    "instance_id": case.instance_id,
                    "kind": case.kind,
                    "code_language": snapshots[case.instance_id].code_language,
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
    return {
        "schema_version": 1,
        "suite": "semantic_queries",
        "label": args.label,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "controls": {"metrics_settle_seconds": args.metrics_settle_seconds},
        "source_revisions": {
            "curated_corpus_sha256": sha256_file(corpus_path),
            "semantic_cases_sha256": sha256_file(manifest),
        },
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
        "by_kind": {
            kind: aggregate([result for result in results if result["kind"] == kind])
            for kind in _KINDS
        },
        "by_code_language": {
            language: aggregate(
                [result for result in results if result["code_language"] == language]
            )
            for language in _present_languages(results)
        },
        "cases": results,
    }


def _percent(value: object) -> str:
    return "-" if value is None else f"{float(value) * 100:.1f}%"


def _number(value: object) -> str:
    return "-" if value is None else f"{float(value):.0f}"


def _model_calls(summary: dict[str, object], kind: str) -> str:
    usage = summary.get("external_model_tokens")
    if not isinstance(usage, dict) or not isinstance(usage.get(kind), dict):
        return "-"
    return str(int(usage[kind].get("calls", 0)))


def _present_languages(results: Sequence[dict[str, object]]) -> list[CodeLanguage]:
    present = {str(result.get("code_language", "")) for result in results}
    return [language for language in LANGUAGES if language in present]


def _reported_languages(report: dict[str, object]) -> list[CodeLanguage]:
    by_language = report.get("by_code_language")
    present = set(by_language) if isinstance(by_language, dict) else set()
    return [language for language in LANGUAGES if language in present]


def compare(paths: Iterable[Path]) -> str:
    loaded = [(path, json.loads(path.read_text(encoding="utf-8"))) for path in paths]
    ensure_comparable([value for _path, value in loaded], suite="semantic_queries")
    languages = _reported_languages(loaded[0][1])

    headers = (
        "Variant",
        "OK",
        "Primary Top-1",
        "MRR",
        "nDCG@10",
        "Weighted R@5",
        "Weighted R@10",
        "Feature nDCG",
        "Overview nDCG",
        "Call-chain nDCG",
        *(f"{LANGUAGE_LABELS[language]} nDCG" for language in languages),
        "Chars",
        "p50 ms",
        "p95 ms",
        "Rerank calls",
        "Chat calls",
    )
    rows: list[tuple[str, ...]] = []
    for path, value in loaded:
        summary = value["summary"]
        by_kind = value["by_kind"]
        by_language = value["by_code_language"]
        rows.append(
            (
                str(value.get("label", path.stem)),
                f"{summary['successful_cases']}/{summary['cases']}",
                _percent(summary["top1_primary"]),
                f"{float(summary['mrr']):.3f}",
                _percent(summary["ndcg_at_10"]),
                _percent(summary["weighted_recall_at_5"]),
                _percent(summary["weighted_recall_at_10"]),
                _percent(by_kind["feature"]["ndcg_at_10"]),
                _percent(by_kind["overview"]["ndcg_at_10"]),
                _percent(by_kind["call_chain"]["ndcg_at_10"]),
                *(
                    _percent(by_language[language]["ndcg_at_10"])
                    for language in languages
                ),
                _number(summary["mean_returned_chars"]),
                _number(summary["p50_elapsed_ms"]),
                _number(summary["p95_elapsed_ms"]),
                _model_calls(summary, "rerank"),
                _model_calls(summary, "llm_rerank"),
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
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="prepare snapshots and validate truth")
    check.set_defaults(handler="check")

    run = subparsers.add_parser(
        "run", help="sync snapshots and execute reviewed queries"
    )
    run.add_argument("--api-url", default="http://127.0.0.1:8986")
    run.add_argument("--client-binary", type=Path)
    run.add_argument("--metrics-settle-seconds", type=float, default=6.0)
    run.add_argument("--label", required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument(
        "--metadata",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="record non-secret runtime metadata; may be repeated",
    )
    run.set_defaults(handler="run")

    comparison = subparsers.add_parser("compare", help="compare paired result files")
    comparison.add_argument("results", type=Path, nargs="+")
    comparison.set_defaults(handler="compare")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.handler == "check":
        cases, snapshots = prepare_cases(
            args.workdir.expanduser().resolve(),
            args.cases.expanduser().resolve(),
            args.corpus.expanduser().resolve(),
        )
        print(
            json.dumps(
                {
                    "cases": len(cases),
                    "kinds": dict(Counter(case.kind for case in cases)),
                    "instances": len(snapshots),
                    "code_languages": dict(
                        Counter(
                            snapshot.code_language for snapshot in snapshots.values()
                        )
                    ),
                    "relevant_paths": sum(len(case.relevant_paths) for case in cases),
                },
                indent=2,
            )
        )
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

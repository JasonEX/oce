"""Evaluate deterministic per-query rerank routing on short code lookups.

The issue benchmark measures semantic retrieval.  This companion benchmark uses
curated symbols from the same pinned snapshots to make structural skip decisions
observable: symbol definitions, concrete paths, and symbol references.  It drives
the production Rust client and reads only the local personal-mode audit database;
no benchmark-only API is added to OCE.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from typing import Literal

from benchmarks.swe_explore import (
    DEFAULT_WORKDIR,
    SWE_BENCH_REVISION,
    SWE_EXPLORE_METRICS_REVISION,
    SWE_EXPLORE_REVISION,
    _client_binary,
    _client_version,
    _git,
    _metadata,
    _run_client,
    _server_retrieval_profile,
    _server_version,
    load_cases,
    parse_retrieved_regions,
    prepare_snapshots,
)

DEFAULT_CASES = Path(__file__).with_name("rerank_routing_cases.json")
QueryKind = Literal["symbol", "path", "reference"]
_REFERENCE_EXCLUDED_PARTS = frozenset(
    {"doc", "docs", "example", "examples", "test", "tests", "testing"}
)


@dataclass(frozen=True)
class RoutingAnchor:
    id: str
    instance_id: str
    identifier: str
    definition_path: str


@dataclass(frozen=True)
class RoutingCase:
    id: str
    instance_id: str
    kind: QueryKind
    query: str
    expected_intent: str
    expected_paths: tuple[str, ...]
    # Every anchor is asked in English and Chinese; the routing rules and the
    # cross-language embedding path must agree on both.
    language: str = "en"
    # The anchor's declaration file. Reference truth excludes it, so a
    # reference query that leads with it is a distinct failure mode
    # ("answered the definition") worth reporting apart from noise.
    definition_path: str = ""


@dataclass(frozen=True)
class AuditRow:
    id: int
    intent: str | None
    rerank_route: str | None
    total_ms: int
    rerank_ms: int | None
    llm_rerank_ms: int | None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_anchors(path: Path) -> tuple[RoutingAnchor, ...]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") != 1 or not isinstance(value.get("anchors"), list):
        raise ValueError("routing case manifest must use schema_version 1")
    anchors: list[RoutingAnchor] = []
    seen: set[str] = set()
    for raw in value["anchors"]:
        try:
            anchor = RoutingAnchor(
                id=str(raw["id"]),
                instance_id=str(raw["instance_id"]),
                identifier=str(raw["identifier"]),
                definition_path=str(raw["definition_path"]),
            )
        except (KeyError, TypeError) as exc:
            raise ValueError("invalid routing anchor") from exc
        if not all(asdict(anchor).values()) or anchor.id in seen:
            raise ValueError(f"invalid or duplicate routing anchor: {anchor.id!r}")
        seen.add(anchor.id)
        anchors.append(anchor)
    if not anchors:
        raise ValueError("routing case manifest is empty")
    return tuple(anchors)


def _tracked_python_paths(root: Path) -> Iterable[Path]:
    completed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    for raw in completed.stdout.split(b"\0"):
        if not raw:
            continue
        relative = Path(raw.decode("utf-8"))
        if relative.suffix != ".py" or _REFERENCE_EXCLUDED_PARTS.intersection(
            relative.parts
        ):
            continue
        yield relative


def _reference_paths(
    root: Path, identifier: str, definition_path: str
) -> tuple[str, ...]:
    pattern = re.compile(rf"(?<![A-Za-z0-9_$]){re.escape(identifier)}(?![A-Za-z0-9_$])")
    paths: list[str] = []
    for relative in _tracked_python_paths(root):
        normalized = relative.as_posix()
        if normalized == definition_path:
            continue
        try:
            text = (root / relative).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if pattern.search(text):
            paths.append(normalized)
    return tuple(sorted(paths))


def expand_cases(
    workdir: Path, anchors: Sequence[RoutingAnchor]
) -> tuple[RoutingCase, ...]:
    cases: list[RoutingCase] = []
    definition_pattern = r"(?m)^(?:async\s+def|def|class)\s+{identifier}\b"
    for anchor in anchors:
        root = workdir / "snapshots" / anchor.instance_id
        target = root / anchor.definition_path
        if not target.is_file():
            raise ValueError(f"missing definition target: {target}")
        content = target.read_text(encoding="utf-8")
        if not re.search(
            definition_pattern.format(identifier=re.escape(anchor.identifier)), content
        ):
            raise ValueError(
                f"{anchor.identifier!r} is not defined in {anchor.definition_path}"
            )
        references = _reference_paths(root, anchor.identifier, anchor.definition_path)
        if not references:
            raise ValueError(f"no source reference path for {anchor.identifier!r}")
        templates = {
            "en": (
                f"Where is `{anchor.identifier}` defined?",
                f"Where is the {anchor.definition_path} file?",
                f"Where is `{anchor.identifier}` referenced?",
            ),
            "zh": (
                f"`{anchor.identifier}` 在哪里定义？",
                f"{anchor.definition_path} 这个文件在哪里？",
                f"哪些地方引用了 `{anchor.identifier}`？",
            ),
        }
        for language, (symbol_query, path_query, reference_query) in templates.items():
            suffix = "" if language == "en" else f"-{language}"
            cases.extend(
                (
                    RoutingCase(
                        id=f"{anchor.id}-symbol{suffix}",
                        instance_id=anchor.instance_id,
                        kind="symbol",
                        query=symbol_query,
                        expected_intent="symbol",
                        expected_paths=(anchor.definition_path,),
                        language=language,
                        definition_path=anchor.definition_path,
                    ),
                    RoutingCase(
                        id=f"{anchor.id}-path{suffix}",
                        instance_id=anchor.instance_id,
                        kind="path",
                        query=path_query,
                        expected_intent="path",
                        expected_paths=(anchor.definition_path,),
                        language=language,
                        definition_path=anchor.definition_path,
                    ),
                    RoutingCase(
                        id=f"{anchor.id}-reference{suffix}",
                        instance_id=anchor.instance_id,
                        kind="reference",
                        query=reference_query,
                        expected_intent="reference",
                        expected_paths=references,
                        language=language,
                        definition_path=anchor.definition_path,
                    ),
                )
            )
    return tuple(cases)


def prepare_cases(
    workdir: Path, manifest: Path
) -> tuple[tuple[RoutingCase, ...], tuple[RoutingAnchor, ...]]:
    anchors = load_anchors(manifest)
    issue_cases = {
        case.instance_id: case for case in load_cases(workdir, "development")
    }
    missing = sorted({anchor.instance_id for anchor in anchors} - issue_cases.keys())
    if missing:
        raise ValueError(
            f"routing anchors are outside the pinned development set: {missing}"
        )
    selected = [
        issue_cases[instance_id]
        for instance_id in sorted({a.instance_id for a in anchors})
    ]
    prepare_snapshots(workdir, selected)
    return expand_cases(workdir, anchors), anchors


def _metric_watermark(database: Path) -> int:
    if not database.is_file():
        raise FileNotFoundError(f"metrics database not found: {database}")
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
        row = connection.execute(
            "SELECT COALESCE(MAX(id), 0) FROM retrieval_metrics"
        ).fetchone()
    return int(row[0])


def _read_audit_row(database: Path, after_id: int, query: str) -> AuditRow | None:
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
        row = connection.execute(
            """
            SELECT id, intent, rerank_route, total_ms, rerank_ms, llm_rerank_ms
            FROM retrieval_metrics
            WHERE id > ? AND query_text = ?
            ORDER BY id
            LIMIT 1
            """,
            (after_id, query),
        ).fetchone()
    return AuditRow(*row) if row is not None else None


def wait_for_audit_row(
    database: Path, after_id: int, query: str, timeout_seconds: float
) -> AuditRow:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if row := _read_audit_row(database, after_id, query):
            return row
        time.sleep(0.05)
    raise RuntimeError(
        "retrieval audit row was not flushed; run the benchmark server with "
        "MONITORING_RETRIEVAL_AUDIT_ENABLED=true, MONITORING_STORE_QUERY_TEXT=true, "
        "and a short MONITORING_FLUSH_INTERVAL_SECONDS"
    )


def expected_route(kind: QueryKind, runtime: dict[str, object]) -> str:
    dedicated_enabled = bool(runtime.get("api_rerank_enabled"))
    llm_enabled = bool(runtime.get("llm_rerank_enabled"))
    dedicated_policy = str(runtime.get("rerank_policy", "adaptive"))
    llm_policy = str(runtime.get("llm_rerank_policy", "adaptive"))
    if dedicated_policy not in {"adaptive", "always"} or llm_policy not in {
        "adaptive",
        "always",
    }:
        raise ValueError("server returned an unsupported rerank policy")

    if kind == "symbol":
        adaptive_dedicated, adaptive_llm, reason = False, False, "exact_definition"
    elif kind == "path":
        adaptive_dedicated, adaptive_llm, reason = False, False, "path_evidence"
    else:
        adaptive_dedicated, adaptive_llm, reason = (
            True,
            False,
            "reference_keep_coverage",
        )

    dedicated = dedicated_enabled and (
        dedicated_policy == "always" or adaptive_dedicated
    )
    llm = llm_enabled and (llm_policy == "always" or adaptive_llm)
    applied = [
        name for name, enabled in (("dedicated", dedicated), ("llm", llm)) if enabled
    ]
    if applied:
        return "+".join(applied)
    if not (dedicated_enabled or llm_enabled):
        reason = "no_reranker_enabled"
    return f"skip:{reason}"


def _score_paths(
    expected: Sequence[str],
    retrieved: Sequence[str],
    *,
    definition_path: str = "",
) -> dict[str, float]:
    expected_set = set(expected)
    ranked = list(dict.fromkeys(retrieved[:10]))
    rank = next(
        (index for index, path in enumerate(ranked, 1) if path in expected_set), None
    )
    return {
        "top1": float(rank == 1),
        "hit_at_10": float(rank is not None),
        "mrr": 0.0 if rank is None else 1.0 / rank,
        "path_recall_at_10": len(expected_set.intersection(ranked)) / len(expected_set),
        "definition_top1": float(
            bool(definition_path)
            and bool(retrieved)
            and retrieved[0] == definition_path
        ),
    }


def _percentile(values: Sequence[int], percentile: int) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile / 100 * len(ordered)) - 1)
    return ordered[index]


def aggregate(results: Sequence[dict[str, object]]) -> dict[str, object]:
    successful = [result for result in results if result["status"] == "ok"]
    quality_names = (
        "top1",
        "hit_at_10",
        "mrr",
        "path_recall_at_10",
        "definition_top1",
    )
    elapsed = [int(result["elapsed_ms"]) for result in successful]
    returned_chars = [int(result["returned_chars"]) for result in successful]
    hit_counts = [int(result["hit_count"]) for result in successful]
    rerank_ms = [
        int(result["rerank_ms"])
        for result in successful
        if result.get("rerank_ms") is not None
    ]
    llm_rerank_ms = [
        int(result["llm_rerank_ms"])
        for result in successful
        if result.get("llm_rerank_ms") is not None
    ]
    routes = Counter(str(result["rerank_route"]) for result in successful)
    return {
        "cases": len(results),
        "successful_cases": len(successful),
        "error_cases": len(results) - len(successful),
        **{
            name: fmean(
                float(result.get("metrics", {}).get(name, 0.0)) for result in results
            )
            for name in quality_names
        },
        "route_conformance": fmean(
            float(bool(result.get("route_conformant"))) for result in results
        ),
        "intent_conformance": fmean(
            float(bool(result.get("intent_conformant"))) for result in results
        ),
        "stage_conformance": fmean(
            float(bool(result.get("stage_conformant"))) for result in results
        ),
        "skip_rate": (
            sum(count for route, count in routes.items() if route.startswith("skip:"))
            / len(successful)
            if successful
            else 0.0
        ),
        "routes": dict(sorted(routes.items())),
        "mean_elapsed_ms": fmean(elapsed) if elapsed else None,
        "mean_returned_chars": fmean(returned_chars) if returned_chars else None,
        "mean_hit_count": fmean(hit_counts) if hit_counts else None,
        "p50_elapsed_ms": _percentile(elapsed, 50),
        "p95_elapsed_ms": _percentile(elapsed, 95),
        "mean_rerank_ms": fmean(rerank_ms) if rerank_ms else None,
        "mean_llm_rerank_ms": fmean(llm_rerank_ms) if llm_rerank_ms else None,
    }


def _stage_conformant(route: str, audit: AuditRow) -> bool:
    dedicated_expected = route in {"dedicated", "dedicated+llm"}
    llm_expected = route in {"llm", "dedicated+llm"}
    return (audit.rerank_ms is not None) == dedicated_expected and (
        audit.llm_rerank_ms is not None
    ) == llm_expected


def run_benchmark(args: argparse.Namespace) -> dict[str, object]:
    workdir = args.workdir.expanduser().resolve()
    manifest = args.cases.expanduser().resolve()
    metrics_database = args.metrics_db.expanduser().resolve()
    cases, anchors = prepare_cases(workdir, manifest)
    binary = _client_binary(args.client_binary)
    api_key = os.environ.get("OCE_API_KEY", "sk-opencontextengine")
    admin_key = os.environ.get("OCE_ADMIN_API_KEY")
    runtime = _server_retrieval_profile(args.api_url, admin_key)
    if runtime is None:
        raise RuntimeError(
            "OCE_ADMIN_API_KEY is required to read the server routing profile"
        )
    _metric_watermark(metrics_database)

    state_dir = workdir / "routing-client-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    sync: dict[str, dict[str, object]] = {}
    for number, instance_id in enumerate(
        sorted({case.instance_id for case in cases}), 1
    ):
        print(
            f"[sync {number}/{len(set(c.instance_id for c in cases))}] {instance_id}",
            file=sys.stderr,
        )
        response = _run_client(
            binary,
            workdir / "snapshots" / instance_id,
            state_dir / f"{instance_id}.sqlite3",
            args.api_url,
            api_key,
            ("sync", "--json"),
        )
        uploaded = response.get("uploaded_blob_names", ())
        sync[instance_id] = {
            "status": "ok",
            "uploaded_blobs": len(uploaded) if isinstance(uploaded, list) else None,
        }

    results: list[dict[str, object]] = []
    for number, case in enumerate(cases, 1):
        print(f"[retrieve {number}/{len(cases)}] {case.id}", file=sys.stderr)
        started = time.perf_counter()
        watermark = _metric_watermark(metrics_database)
        try:
            response = _run_client(
                binary,
                workdir / "snapshots" / case.instance_id,
                state_dir / f"{case.instance_id}.sqlite3",
                args.api_url,
                api_key,
                ("retrieve", case.query, "--json"),
            )
            formatted = response.get("formatted_retrieval")
            elapsed_ms = response.get("elapsed_ms")
            if not isinstance(formatted, str) or not isinstance(elapsed_ms, int):
                raise RuntimeError("oce-client retrieve returned an invalid payload")
            audit = wait_for_audit_row(
                metrics_database, watermark, case.query, args.metrics_timeout_seconds
            )
            retrieved = parse_retrieved_regions(formatted)
            route = audit.rerank_route or "missing"
            expected = expected_route(case.kind, runtime)
            results.append(
                {
                    "id": case.id,
                    "instance_id": case.instance_id,
                    "kind": case.kind,
                    "language": case.language,
                    "status": "ok",
                    "expected_path_count": len(case.expected_paths),
                    "retrieved": [asdict(region) for region in retrieved],
                    "metrics": _score_paths(
                        case.expected_paths,
                        [region.path for region in retrieved],
                        definition_path=case.definition_path,
                    ),
                    "returned_chars": len(formatted),
                    "hit_count": len(retrieved),
                    "elapsed_ms": elapsed_ms,
                    "wall_elapsed_ms": int((time.perf_counter() - started) * 1000),
                    "intent": audit.intent,
                    "expected_intent": case.expected_intent,
                    "intent_conformant": audit.intent == case.expected_intent,
                    "rerank_route": route,
                    "expected_rerank_route": expected,
                    "route_conformant": route == expected,
                    "stage_conformant": _stage_conformant(route, audit),
                    "server_total_ms": audit.total_ms,
                    "rerank_ms": audit.rerank_ms,
                    "llm_rerank_ms": audit.llm_rerank_ms,
                }
            )
        except Exception as exc:
            results.append(
                {
                    "id": case.id,
                    "instance_id": case.instance_id,
                    "kind": case.kind,
                    "language": case.language,
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "error": str(exc).replace(api_key, "[REDACTED]")[:1000],
                    "wall_elapsed_ms": int((time.perf_counter() - started) * 1000),
                }
            )

    by_kind = {
        kind: aggregate([result for result in results if result["kind"] == kind])
        for kind in ("symbol", "path", "reference")
    }
    by_language = {
        language: aggregate(
            [result for result in results if result.get("language") == language]
        )
        for language in sorted({case.language for case in cases})
    }
    return {
        "schema_version": 1,
        "label": args.label,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_revisions": {
            "swe_bench_verified": SWE_BENCH_REVISION,
            "swe_explore": SWE_EXPLORE_REVISION,
            "swe_explore_metrics": SWE_EXPLORE_METRICS_REVISION,
            "routing_cases_sha256": _sha256(manifest),
        },
        "runtime": {
            "harness_source_commit": _git(
                "rev-parse", "HEAD", cwd=Path(__file__).resolve().parents[1]
            ),
            "harness_source_dirty": bool(
                _git("status", "--porcelain", cwd=Path(__file__).resolve().parents[1])
            ),
            "server_version": _server_version(args.api_url),
            "server_retrieval_profile": runtime,
            "client_version": _client_version(binary),
            "environment": _metadata(args.metadata),
        },
        "anchors": len(anchors),
        "case_ids": [case.id for case in cases],
        "sync": sync,
        "summary": aggregate(results),
        "by_kind": by_kind,
        "by_language": by_language,
        "cases": results,
    }


def _percent(value: object) -> str:
    return "-" if value is None else f"{float(value) * 100:.1f}%"


def _number(value: object) -> str:
    return "-" if value is None else f"{float(value):.0f}"


def compare(paths: Iterable[Path]) -> str:
    # Top-1 per kind is the acceptance line for the deterministic head slots;
    # Hit@10 alone stayed at 100% while symbol answers slipped to rank 2-4.
    headers = (
        "Variant",
        "OK",
        "Top-1",
        "MRR",
        "Symbol Top-1",
        "Path Top-1",
        "Reference Top-1",
        "Ref def-first",
        "Hit@10",
        "Intent",
        "Route",
        "Stage",
        "Skip",
        "Chars",
        "Hits",
        "p50 ms",
        "p95 ms",
        "Rerank ms",
        "LLM ms",
    )
    loaded = [(path, json.loads(path.read_text(encoding="utf-8"))) for path in paths]
    expected_ids: list[str] | None = None
    for _path, value in loaded:
        case_ids = value.get("case_ids")
        if expected_ids is None:
            expected_ids = case_ids
        elif case_ids != expected_ids:
            raise ValueError("results use different ordered routing case sets")

    rows: list[tuple[str, ...]] = []
    for path, value in loaded:
        summary = value["summary"]
        by_kind = value["by_kind"]
        rows.append(
            (
                str(value.get("label", path.stem)),
                f"{summary['successful_cases']}/{summary['cases']}",
                _percent(summary["top1"]),
                f"{float(summary['mrr']):.3f}",
                _percent(by_kind["symbol"]["top1"]),
                _percent(by_kind["path"]["top1"]),
                _percent(by_kind["reference"]["top1"]),
                _percent(by_kind["reference"].get("definition_top1")),
                _percent(summary["hit_at_10"]),
                _percent(summary["intent_conformance"]),
                _percent(summary["route_conformance"]),
                _percent(summary["stage_conformance"]),
                _percent(summary["skip_rate"]),
                _number(summary.get("mean_returned_chars")),
                _number(summary.get("mean_hit_count")),
                _number(summary["p50_elapsed_ms"]),
                _number(summary["p95_elapsed_ms"]),
                _number(summary["mean_rerank_ms"]),
                _number(summary["mean_llm_rerank_ms"]),
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
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="validate and summarize the query set")
    check.set_defaults(handler="check")

    run = subparsers.add_parser("run", help="sync snapshots and execute short queries")
    run.add_argument("--api-url", default="http://127.0.0.1:8986")
    run.add_argument("--client-binary", type=Path)
    run.add_argument("--metrics-db", type=Path, required=True)
    run.add_argument("--metrics-timeout-seconds", type=float, default=10.0)
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
        cases, anchors = prepare_cases(
            args.workdir.expanduser().resolve(), args.cases.expanduser().resolve()
        )
        print(
            json.dumps(
                {
                    "anchors": len(anchors),
                    "cases": len(cases),
                    "kinds": dict(Counter(case.kind for case in cases)),
                    "languages": dict(Counter(case.language for case in cases)),
                    "instances": len({case.instance_id for case in cases}),
                },
                indent=2,
            )
        )
        return 0
    if args.handler == "run":
        if args.metrics_timeout_seconds <= 0:
            raise ValueError("--metrics-timeout-seconds must be positive")
        result = run_benchmark(args)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result["summary"], indent=2))
        return 0
    print(compare(args.results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

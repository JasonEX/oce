"""Evaluate short code lookups under different rerank policies.

The issue benchmark measures semantic retrieval. This companion benchmark uses
curated symbols from pinned snapshots to measure the high-confidence structural
workloads where an adaptive policy may skip model work. It observes only output
quality, latency, and aggregate model calls through the production Rust client;
it never reads server persistence or imports server implementation code.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from typing import Literal

from benchmarks.blackbox.corpus import (
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

DEFAULT_CASES = Path(__file__).with_name("short_query_anchors.json")
DEFAULT_CORPUS = Path(__file__).with_name("curated_corpus.json")
QueryKind = Literal["symbol", "path", "reference"]
_REFERENCE_EXCLUDED_PARTS = frozenset(
    {
        "__tests__",
        "bench",
        "benches",
        "benchmark",
        "benchmarks",
        "doc",
        "docs",
        "example",
        "examples",
        "test",
        "tests",
        "testing",
    }
)
_SOURCE_EXTENSIONS: dict[CodeLanguage, frozenset[str]] = {
    "python": frozenset({".py"}),
    "typescript": frozenset({".ts", ".tsx"}),
    "rust": frozenset({".rs"}),
}
_DEFINITION_PATTERNS: dict[CodeLanguage, str] = {
    "python": r"(?m)^(?:async\s+def|def|class)\s+{identifier}\b",
    "typescript": (
        r"(?m)^(?:export\s+)?(?:declare\s+)?(?:abstract\s+)?"
        r"(?:class|interface|type|function|const)\s+{identifier}\b"
    ),
    "rust": (
        r"(?m)^(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?"
        r"(?:fn|struct|enum|trait|type|const)\s+{identifier}\b"
    ),
}


@dataclass(frozen=True)
class ShortQueryAnchor:
    id: str
    instance_id: str
    identifier: str
    definition_path: str


@dataclass(frozen=True)
class ShortQueryCase:
    id: str
    instance_id: str
    kind: QueryKind
    query: str
    expected_paths: tuple[str, ...]
    code_language: CodeLanguage = "python"
    # Every anchor is asked in English and Chinese so deterministic evidence and
    # the cross-language embedding path are exercised on the same truth.
    query_language: str = "en"
    # The anchor's declaration file. Reference truth excludes it, so a
    # reference query that leads with it is a distinct failure mode
    # ("answered the definition") worth reporting apart from noise.
    definition_path: str = ""


def load_anchors(path: Path) -> tuple[ShortQueryAnchor, ...]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or not isinstance(value.get("anchors"), list)
    ):
        raise ValueError("short-query anchor manifest must use schema_version 1")
    anchors: list[ShortQueryAnchor] = []
    seen: set[str] = set()
    for raw in value["anchors"]:
        try:
            anchor = ShortQueryAnchor(
                id=str(raw["id"]),
                instance_id=str(raw["instance_id"]),
                identifier=str(raw["identifier"]),
                definition_path=str(raw["definition_path"]),
            )
        except (KeyError, TypeError) as exc:
            raise ValueError("invalid short-query anchor") from exc
        if not all(asdict(anchor).values()) or anchor.id in seen:
            raise ValueError(f"invalid or duplicate short-query anchor: {anchor.id!r}")
        seen.add(anchor.id)
        anchors.append(anchor)
    if not anchors:
        raise ValueError("short-query anchor manifest is empty")
    return tuple(anchors)


def _tracked_source_paths(root: Path, code_language: CodeLanguage) -> Iterable[Path]:
    completed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    for raw in completed.stdout.split(b"\0"):
        if not raw:
            continue
        relative = Path(raw.decode("utf-8"))
        if relative.suffix not in _SOURCE_EXTENSIONS[
            code_language
        ] or _REFERENCE_EXCLUDED_PARTS.intersection(relative.parts):
            continue
        yield relative


def _reference_paths(
    root: Path,
    identifier: str,
    definition_path: str,
    code_language: CodeLanguage,
) -> tuple[str, ...]:
    pattern = re.compile(rf"(?<![A-Za-z0-9_$]){re.escape(identifier)}(?![A-Za-z0-9_$])")
    paths: list[str] = []
    for relative in _tracked_source_paths(root, code_language):
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
    workdir: Path,
    anchors: Sequence[ShortQueryAnchor],
    snapshots: dict[str, RepositorySnapshot],
) -> tuple[ShortQueryCase, ...]:
    cases: list[ShortQueryCase] = []
    for anchor in anchors:
        snapshot = snapshots[anchor.instance_id]
        root = snapshot_path(workdir, snapshot.id)
        target = root / anchor.definition_path
        if not target.is_file():
            raise ValueError(f"missing definition target: {target}")
        content = target.read_text(encoding="utf-8")
        if not re.search(
            _DEFINITION_PATTERNS[snapshot.code_language].format(
                identifier=re.escape(anchor.identifier)
            ),
            content,
        ):
            raise ValueError(
                f"{anchor.identifier!r} is not defined in {anchor.definition_path}"
            )
        references = _reference_paths(
            root,
            anchor.identifier,
            anchor.definition_path,
            snapshot.code_language,
        )
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
                    ShortQueryCase(
                        id=f"{anchor.id}-symbol{suffix}",
                        instance_id=anchor.instance_id,
                        kind="symbol",
                        query=symbol_query,
                        expected_paths=(anchor.definition_path,),
                        code_language=snapshot.code_language,
                        query_language=language,
                        definition_path=anchor.definition_path,
                    ),
                    ShortQueryCase(
                        id=f"{anchor.id}-path{suffix}",
                        instance_id=anchor.instance_id,
                        kind="path",
                        query=path_query,
                        expected_paths=(anchor.definition_path,),
                        code_language=snapshot.code_language,
                        query_language=language,
                        definition_path=anchor.definition_path,
                    ),
                    ShortQueryCase(
                        id=f"{anchor.id}-reference{suffix}",
                        instance_id=anchor.instance_id,
                        kind="reference",
                        query=reference_query,
                        expected_paths=references,
                        code_language=snapshot.code_language,
                        query_language=language,
                        definition_path=anchor.definition_path,
                    ),
                )
            )
    return tuple(cases)


def prepare_cases(
    workdir: Path, manifest: Path, corpus_path: Path
) -> tuple[
    tuple[ShortQueryCase, ...],
    tuple[ShortQueryAnchor, ...],
    dict[str, RepositorySnapshot],
]:
    anchors = load_anchors(manifest)
    snapshots = select_snapshots(
        load_corpus(corpus_path), (anchor.instance_id for anchor in anchors)
    )
    prepare_snapshots(workdir, tuple(snapshots.values()))
    return expand_cases(workdir, anchors, snapshots), anchors, snapshots


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
            for name in quality_names
        },
        "mean_elapsed_ms": fmean(elapsed) if elapsed else None,
        "mean_returned_chars": fmean(returned_chars) if returned_chars else None,
        "mean_hit_count": fmean(hit_counts) if hit_counts else None,
        "p50_elapsed_ms": percentile(elapsed, 50),
        "p95_elapsed_ms": percentile(elapsed, 95),
    }


def run_benchmark(args: argparse.Namespace) -> dict[str, object]:
    workdir = args.workdir.expanduser().resolve()
    manifest = args.cases.expanduser().resolve()
    corpus_path = args.corpus.expanduser().resolve()
    cases, anchors, snapshots = prepare_cases(workdir, manifest, corpus_path)
    binary = resolve_client_binary(args.client_binary)
    api_key = os.environ.get("OCE_API_KEY", "sk-opencontextengine")
    admin_key = os.environ.get("OCE_ADMIN_API_KEY")

    state_dir = workdir / "short-query-client-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    sync: dict[str, dict[str, object]] = {}
    for number, instance_id in enumerate(
        sorted({case.instance_id for case in cases}), 1
    ):
        print(
            f"[sync {number}/{len(set(c.instance_id for c in cases))}] {instance_id}",
            file=sys.stderr,
        )
        response = run_client(
            binary,
            snapshot_path(workdir, instance_id),
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
                    "query_language": case.query_language,
                    "code_language": case.code_language,
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
                }
            )
        except Exception as exc:
            results.append(
                {
                    "id": case.id,
                    "instance_id": case.instance_id,
                    "kind": case.kind,
                    "query_language": case.query_language,
                    "code_language": case.code_language,
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
    by_query_language = {
        language: aggregate(
            [result for result in results if result.get("query_language") == language]
        )
        for language in sorted({case.query_language for case in cases})
    }
    if admin_key:
        time.sleep(args.metrics_settle_seconds)
    stats_after = admin_stats(args.api_url, admin_key)
    summary = aggregate(results)
    summary["external_model_tokens"] = model_usage_delta(stats_before, stats_after)
    return {
        "schema_version": 3,
        "suite": "short_queries",
        "label": args.label,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "controls": {"metrics_settle_seconds": args.metrics_settle_seconds},
        "source_revisions": {
            "curated_corpus_sha256": sha256_file(corpus_path),
            "short_query_anchors_sha256": sha256_file(manifest),
        },
        "snapshots": [asdict(snapshot) for snapshot in snapshots.values()],
        "runtime": runtime_metadata(
            binary=binary,
            api_url=args.api_url,
            admin_key=admin_key,
            extra_metadata=args.metadata,
        ),
        "anchors": len(anchors),
        "case_ids": [case.id for case in cases],
        "sync": sync,
        "summary": summary,
        "by_kind": by_kind,
        "by_query_language": by_query_language,
        "by_code_language": {
            language: aggregate(
                [result for result in results if result["code_language"] == language]
            )
            for language in ("python", "typescript", "rust")
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
        "Python Top-1",
        "TS Top-1",
        "Rust Top-1",
        "Hit@10",
        "Chars",
        "Hits",
        "p50 ms",
        "p95 ms",
        "Rerank calls",
        "Chat calls",
    )
    loaded = [(path, json.loads(path.read_text(encoding="utf-8"))) for path in paths]
    ensure_comparable([value for _path, value in loaded], suite="short_queries")

    rows: list[tuple[str, ...]] = []
    for path, value in loaded:
        summary = value["summary"]
        by_kind = value["by_kind"]
        by_code_language = value["by_code_language"]
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
                _percent(by_code_language["python"]["top1"]),
                _percent(by_code_language["typescript"]["top1"]),
                _percent(by_code_language["rust"]["top1"]),
                _percent(summary["hit_at_10"]),
                _number(summary.get("mean_returned_chars")),
                _number(summary.get("mean_hit_count")),
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

    check = subparsers.add_parser("check", help="validate and summarize the query set")
    check.set_defaults(handler="check")

    run = subparsers.add_parser("run", help="sync snapshots and execute short queries")
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
        cases, anchors, snapshots = prepare_cases(
            args.workdir.expanduser().resolve(),
            args.cases.expanduser().resolve(),
            args.corpus.expanduser().resolve(),
        )
        print(
            json.dumps(
                {
                    "anchors": len(anchors),
                    "cases": len(cases),
                    "kinds": dict(Counter(case.kind for case in cases)),
                    "query_languages": dict(
                        Counter(case.query_language for case in cases)
                    ),
                    "instances": len({case.instance_id for case in cases}),
                    "code_languages": dict(
                        Counter(
                            snapshot.code_language for snapshot in snapshots.values()
                        )
                    ),
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

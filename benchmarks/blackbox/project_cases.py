"""Evaluate relation-closure cases on real project snapshots as a black box.

Each case fixes a query, a pinned snapshot, the regions that answer it, the
regions that close the relation (callers, hops, tests, re-exports), files that
must not lead the result, and optionally the test files that exercise the
symbol.  Scoring works on the ``Path:``/``Lines:`` regions of the returned text,
so appended evidence sections count toward relation recall exactly as an agent
would see them.  Every case also receives one automatically derived error class
so a run can be read as a distribution of failure modes, not only as means.
"""

from __future__ import annotations

import argparse
import json
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
    RepositorySnapshot,
    load_corpus,
    prepare_snapshots,
    select_snapshots,
    snapshot_path,
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

DEFAULT_CASES = Path(__file__).with_name("project_cases.json")
DEFAULT_CORPUS = Path(__file__).with_name("curated_corpus.json")
CaseKind = Literal["reference", "call_chain", "test_mapping", "reexport", "multi_impl"]
KINDS: tuple[CaseKind, ...] = (
    "reference",
    "call_chain",
    "test_mapping",
    "reexport",
    "multi_impl",
)
ErrorClass = Literal[
    "none",
    "exact_miss",
    "distractor",
    "test_missing",
    "relation_missing",
    "redundant",
]
ERROR_CLASSES: tuple[ErrorClass, ...] = (
    "none",
    "exact_miss",
    "distractor",
    "test_missing",
    "relation_missing",
    "redundant",
)
# Retrieved regions that overlap no truth region are context the agent did not
# ask for; below this share the answer is mostly filler.
REDUNDANT_TRUTH_SHARE = 0.25
HEAD_REGIONS = 3


@dataclass(frozen=True)
class TruthRegion:
    path: str
    start: int
    end: int
    hop: int | None = None


@dataclass(frozen=True)
class ProjectCase:
    id: str
    instance_id: str
    kind: CaseKind
    query: str
    rationale: str
    primary_regions: tuple[TruthRegion, ...]
    supporting_regions: tuple[TruthRegion, ...]
    must_not_paths: tuple[str, ...]
    test_paths: tuple[str, ...]
    family_id: str | None = None
    query_form: str | None = None


def _region(raw: object) -> TruthRegion:
    if not isinstance(raw, dict):
        raise ValueError("invalid truth region")
    path = str(raw.get("path", ""))
    start = raw.get("start")
    end = raw.get("end")
    hop = raw.get("hop")
    if (
        not path
        or not isinstance(start, int)
        or not isinstance(end, int)
        or start < 1
        or end < start
        or (hop is not None and (not isinstance(hop, int) or hop < 0))
    ):
        raise ValueError(f"invalid truth region: {raw!r}")
    return TruthRegion(path=path, start=start, end=end, hop=hop)


def load_manifest(path: Path) -> tuple[ProjectCase, ...]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or not isinstance(value.get("cases"), list)
        or not isinstance(value.get("labeling"), dict)
        or not value["labeling"].get("method")
    ):
        raise ValueError("project case manifest must use schema_version 1")

    cases: list[ProjectCase] = []
    seen_ids: set[str] = set()
    for raw in value["cases"]:
        if not isinstance(raw, dict):
            raise ValueError("invalid project case")
        kind = str(raw.get("kind"))
        if kind not in KINDS:
            raise ValueError(f"unsupported project case kind: {kind!r}")
        primary = tuple(_region(item) for item in raw.get("primary_regions", ()))
        supporting = tuple(_region(item) for item in raw.get("supporting_regions", ()))
        must_not = tuple(str(item) for item in raw.get("must_not_paths", ()))
        tests = tuple(str(item) for item in raw.get("test_paths", ()))
        case = ProjectCase(
            id=str(raw.get("id", "")),
            instance_id=str(raw.get("instance_id", "")),
            kind=cast(CaseKind, kind),
            query=str(raw.get("query", "")),
            rationale=str(raw.get("rationale", "")),
            primary_regions=primary,
            supporting_regions=supporting,
            must_not_paths=must_not,
            test_paths=tests,
            family_id=raw.get("family_id"),
            query_form=raw.get("query_form"),
        )
        if (
            not case.id
            or case.id in seen_ids
            or not case.instance_id
            or not case.query
            or not case.rationale
            or not case.primary_regions
            or not case.must_not_paths
            or (case.kind == "test_mapping" and not case.test_paths)
            or len(set(must_not)) != len(must_not)
            or len(set(tests)) != len(tests)
            or bool(case.family_id) != bool(case.query_form)
            or (case.family_id is not None and not isinstance(case.family_id, str))
            or (case.query_form is not None and not isinstance(case.query_form, str))
        ):
            raise ValueError(f"invalid or duplicate project case: {case.id!r}")
        truth_paths = {region.path for region in (*primary, *supporting)}
        if truth_paths & set(must_not):
            raise ValueError(f"{case.id}: a must_not path is also a truth region")
        seen_ids.add(case.id)
        cases.append(case)
    if not cases:
        raise ValueError("project case manifest is empty")
    families: dict[str, tuple[object, ...]] = {}
    for case in cases:
        if case.family_id is None:
            continue
        truth = (
            case.instance_id,
            case.kind,
            case.primary_regions,
            case.supporting_regions,
            case.must_not_paths,
            case.test_paths,
        )
        if families.setdefault(case.family_id, truth) != truth:
            raise ValueError(
                f"{case.family_id}: query variants must share snapshot and truth"
            )
    return tuple(cases)


def prepare_cases(
    workdir: Path, manifest: Path, corpus_path: Path
) -> tuple[tuple[ProjectCase, ...], dict[str, RepositorySnapshot]]:
    cases = load_manifest(manifest)
    selected = select_snapshots(
        load_corpus(corpus_path), (case.instance_id for case in cases)
    )
    prepare_snapshots(workdir, tuple(selected.values()))
    for case in cases:
        root = snapshot_path(workdir, case.instance_id)
        problems: list[str] = []
        for region in (*case.primary_regions, *case.supporting_regions):
            file = root / region.path
            if not file.is_file():
                problems.append(f"missing {region.path}")
                continue
            line_count = len(
                file.read_text(encoding="utf-8", errors="replace").splitlines()
            )
            if region.end > line_count:
                problems.append(f"{region.path} ends after line {line_count}")
        for path in (*case.must_not_paths, *case.test_paths):
            if not (root / path).is_file():
                problems.append(f"missing {path}")
        if problems:
            raise ValueError(f"{case.id} has invalid truth: {problems}")
    return cases, selected


def _covers(retrieved: RetrievedRegion, truth: TruthRegion) -> bool:
    return (
        retrieved.path == truth.path
        and retrieved.start <= truth.end
        and retrieved.end >= truth.start
    )


def _recall(
    truths: Sequence[TruthRegion], retrieved: Sequence[RetrievedRegion]
) -> float:
    if not truths:
        return 1.0
    covered = sum(
        any(_covers(region, truth) for region in retrieved) for truth in truths
    )
    return covered / len(truths)


def score_case(
    case: ProjectCase, retrieved: Sequence[RetrievedRegion]
) -> dict[str, float | str]:
    truths = (*case.primary_regions, *case.supporting_regions)
    first_rank = next(
        (
            rank
            for rank, region in enumerate(retrieved, 1)
            if any(_covers(region, truth) for truth in case.primary_regions)
        ),
        None,
    )
    hops = {region.hop for region in truths if region.hop is not None}
    hop_recall = (
        fmean(
            float(
                any(
                    _covers(region, truth)
                    for truth in truths
                    if truth.hop == hop
                    for region in retrieved
                )
            )
            for hop in sorted(hops)
        )
        if hops
        else 1.0
    )
    retrieved_paths = {region.path for region in retrieved}
    test_recall = (
        sum(path in retrieved_paths for path in case.test_paths) / len(case.test_paths)
        if case.test_paths
        else 1.0
    )
    must_not = set(case.must_not_paths)
    distractor_head = float(
        any(region.path in must_not for region in retrieved[:HEAD_REGIONS])
    )
    distractor_any = float(any(region.path in must_not for region in retrieved))
    truth_share = (
        sum(
            any(_covers(region, truth) for truth in truths)
            or region.path in case.test_paths
            for region in retrieved
        )
        / len(retrieved)
        if retrieved
        else 0.0
    )
    supporting_recall = _recall(case.supporting_regions, retrieved)
    metrics: dict[str, float | str] = {
        "primary_top1": float(first_rank == 1),
        "primary_hit_at_3": float(
            first_rank is not None and first_rank <= HEAD_REGIONS
        ),
        "primary_hit_at_10": float(first_rank is not None and first_rank <= 10),
        "primary_mrr": 0.0 if first_rank is None else 1.0 / first_rank,
        "primary_recall": _recall(case.primary_regions, retrieved),
        "supporting_recall": supporting_recall,
        "relation_recall": _recall(truths, retrieved),
        "hop_recall": hop_recall,
        "chain_closed": float(hop_recall == 1.0),
        "test_recall": test_recall,
        "distractor_head": distractor_head,
        "distractor_any": distractor_any,
        "truth_region_share": truth_share,
    }
    metrics["error_class"] = classify_error(case, metrics)
    return metrics


def classify_error(case: ProjectCase, metrics: dict[str, float | str]) -> ErrorClass:
    """One dominant failure per case, most severe first.

    The head must hold an answer before anything else matters; a distractor
    in the head is a precision failure that more recall cannot repair; the
    remaining classes describe what the evidence pack failed to close.
    """
    if float(metrics["primary_hit_at_3"]) == 0.0:
        return "exact_miss"
    if float(metrics["distractor_head"]) > 0.0:
        return "distractor"
    if case.test_paths and float(metrics["test_recall"]) < 1.0:
        return "test_missing"
    if case.supporting_regions and float(metrics["supporting_recall"]) < 1.0:
        return "relation_missing"
    if float(metrics["truth_region_share"]) < REDUNDANT_TRUTH_SHARE:
        return "redundant"
    return "none"


METRIC_NAMES = (
    "primary_top1",
    "primary_hit_at_3",
    "primary_hit_at_10",
    "primary_mrr",
    "primary_recall",
    "supporting_recall",
    "relation_recall",
    "hop_recall",
    "chain_closed",
    "test_recall",
    "distractor_head",
    "distractor_any",
    "truth_region_share",
)


def aggregate(results: Sequence[dict[str, object]]) -> dict[str, object]:
    successful = [result for result in results if result["status"] == "ok"]
    elapsed = [int(result["elapsed_ms"]) for result in successful]
    classes = Counter(
        str(
            cast(dict[str, object], result.get("metrics", {})).get(
                "error_class", "exact_miss"
            )
        )
        for result in results
    )
    return {
        "cases": len(results),
        "successful_cases": len(successful),
        "error_cases": len(results) - len(successful),
        **{
            name: (
                fmean(
                    float(
                        cast(dict[str, object], result.get("metrics", {})).get(
                            name, 0.0
                        )
                    )
                    for result in results
                )
                if results
                else None
            )
            for name in METRIC_NAMES
        },
        "error_classes": {name: classes.get(name, 0) for name in ERROR_CLASSES},
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


def query_family_results(results: Sequence[dict[str, object]]) -> dict[str, object]:
    """Report independent needs equally, including each need's weakest wording."""
    families = sorted(
        {str(row["family_id"]) for row in results if row.get("family_id")}
    )
    if not families:
        return {}
    groups = {
        family: [row for row in results if row.get("family_id") == family]
        for family in families
    }
    complete = [
        rows for rows in groups.values() if all(row["status"] == "ok" for row in rows)
    ]
    summary: dict[str, object] = {
        "families": len(groups),
        "successful_families": len(complete),
        "error_families": len(groups) - len(complete),
    }
    for metric in ("primary_hit_at_3", "primary_mrr", "relation_recall", "test_recall"):
        values = [
            [
                float(cast(dict, row["metrics"])[metric])
                if row["status"] == "ok"
                else 0.0
                for row in rows
            ]
            for rows in groups.values()
        ]
        summary[metric] = fmean(fmean(group) for group in values) if values else None
        summary[f"worst_{metric}"] = (
            fmean(min(group) for group in values) if values else None
        )
    return {
        "family_summary": summary,
        "by_family": {family: aggregate(rows) for family, rows in groups.items()},
        "by_query_form": {
            form: aggregate([row for row in results if row.get("query_form") == form])
            for form in sorted(
                {str(row["query_form"]) for row in results if row.get("query_form")}
            )
        },
    }


def run_benchmark(args: argparse.Namespace) -> dict[str, object]:
    workdir = args.workdir.expanduser().resolve()
    manifest = args.cases.expanduser().resolve()
    corpus_path = args.corpus.expanduser().resolve()
    cases, snapshots = prepare_cases(workdir, manifest, corpus_path)
    binary = resolve_client_binary(args.client_binary)
    api_key = os.environ.get("OCE_API_KEY", "sk-opencontextengine")
    admin_key = os.environ.get("OCE_ADMIN_API_KEY")
    state_dir = workdir / "project-client-state"
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
            "kind": case.kind,
            "code_language": snapshots[case.instance_id].code_language,
        }
        if case.family_id is not None:
            base.update(family_id=case.family_id, query_form=case.query_form)
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
    return {
        "schema_version": 1,
        "suite": "project_cases",
        "label": args.label,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "controls": {"metrics_settle_seconds": args.metrics_settle_seconds},
        "source_revisions": {
            "curated_corpus_sha256": sha256_file(corpus_path),
            "project_cases_sha256": sha256_file(manifest),
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
            for kind in KINDS
        },
        "by_snapshot": {
            snapshot: aggregate(
                [row for row in results if row["instance_id"] == snapshot]
            )
            for snapshot in sorted({row["instance_id"] for row in results})
        },
        "cases": results,
        **query_family_results(results),
    }


def _percent(value: object) -> str:
    return "-" if value is None else f"{float(value) * 100:.1f}%"


def _number(value: object) -> str:
    return "-" if value is None else f"{float(value):.0f}"


def _ratio(value: object) -> str:
    return "-" if value is None else f"{float(value):.3f}"


def _table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> list[str]:
    output = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    output.extend("| " + " | ".join(row) + " |" for row in rows)
    return output


def compare(paths: Iterable[Path]) -> str:
    loaded = [(path, json.loads(path.read_text(encoding="utf-8"))) for path in paths]
    ensure_comparable([value for _path, value in loaded], suite="project_cases")

    summary_rows: list[tuple[str, ...]] = []
    kind_rows: list[tuple[str, ...]] = []
    class_rows: list[tuple[str, ...]] = []
    for path, value in loaded:
        summary = value["summary"]
        label = str(value.get("label", path.stem))
        summary_rows.append(
            (
                label,
                f"{summary['successful_cases']}/{summary['cases']}",
                _percent(summary["primary_top1"]),
                _percent(summary["primary_hit_at_3"]),
                _ratio(summary["primary_mrr"]),
                _percent(summary["relation_recall"]),
                _percent(summary["supporting_recall"]),
                _percent(summary["hop_recall"]),
                _percent(summary["chain_closed"]),
                _percent(summary["test_recall"]),
                _percent(summary["distractor_head"]),
                _percent(summary["truth_region_share"]),
                _number(summary["mean_returned_chars"]),
                _number(summary["p50_elapsed_ms"]),
                _number(summary["p95_elapsed_ms"]),
            )
        )
        by_kind = value["by_kind"]
        kind_rows.append(
            (
                label,
                *(
                    f"{_percent(by_kind[kind]['primary_hit_at_3'])} / "
                    f"{_percent(by_kind[kind]['relation_recall'])}"
                    for kind in KINDS
                ),
            )
        )
        classes = summary["error_classes"]
        class_rows.append(
            (label, *(str(classes.get(name, 0)) for name in ERROR_CLASSES))
        )

    output = _table(
        (
            "Variant",
            "OK",
            "Primary Top-1",
            "Primary Hit@3",
            "MRR",
            "Relation R",
            "Supporting R",
            "Hop R",
            "Chain closed",
            "Test R",
            "Distractor head",
            "Truth share",
            "Chars",
            "p50 ms",
            "p95 ms",
        ),
        summary_rows,
    )
    output.append("")
    output.extend(
        _table(
            ("Variant", *(f"{kind} Hit@3 / RelR" for kind in KINDS)),
            kind_rows,
        )
    )
    output.append("")
    output.extend(_table(("Variant", *ERROR_CLASSES), class_rows))
    family_rows = []
    form_rows = []
    for path, value in loaded:
        label = str(value.get("label", path.stem))
        family = value.get("family_summary")
        if family:
            family_rows.append(
                (
                    label,
                    str(family["families"]),
                    _percent(family["primary_hit_at_3"]),
                    _percent(family["worst_primary_hit_at_3"]),
                    _ratio(family["primary_mrr"]),
                    _ratio(family["worst_primary_mrr"]),
                    _percent(family["relation_recall"]),
                    _percent(family["worst_relation_recall"]),
                )
            )
        for form, summary in value.get("by_query_form", {}).items():
            form_rows.append(
                (
                    label,
                    form,
                    _percent(summary["primary_hit_at_3"]),
                    _ratio(summary["primary_mrr"]),
                    _percent(summary["relation_recall"]),
                    _percent(summary["distractor_head"]),
                )
            )
    if family_rows:
        output.append("")
        output.extend(
            _table(
                (
                    "Variant",
                    "Needs",
                    "Mean Hit@3",
                    "Worst Hit@3",
                    "Mean MRR",
                    "Worst MRR",
                    "Mean RelR",
                    "Worst RelR",
                ),
                family_rows,
            )
        )
        output.append("")
        output.extend(
            _table(
                ("Variant", "Wording", "Hit@3", "MRR", "RelR", "Distractor head"),
                form_rows,
            )
        )
    snapshot_rows = [
        (
            str(value.get("label", path.stem)),
            snapshot,
            _percent(summary["primary_hit_at_3"]),
            _ratio(summary["primary_mrr"]),
            _percent(summary["relation_recall"]),
            _percent(summary["distractor_head"]),
        )
        for path, value in loaded
        for snapshot, summary in value.get("by_snapshot", {}).items()
    ]
    if snapshot_rows:
        output.append("")
        output.extend(
            _table(
                ("Variant", "Snapshot", "Hit@3", "MRR", "RelR", "Distractor head"),
                snapshot_rows,
            )
        )
    return "\n".join(output)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, default=DEFAULT_WORKDIR)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="prepare snapshots and validate truth")
    check.set_defaults(handler="check")

    run = subparsers.add_parser("run", help="sync snapshots and execute the cases")
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
                    "primary_regions": sum(len(case.primary_regions) for case in cases),
                    "supporting_regions": sum(
                        len(case.supporting_regions) for case in cases
                    ),
                    "test_paths": sum(len(case.test_paths) for case in cases),
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

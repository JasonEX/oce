import json
from pathlib import Path

import pytest

from benchmarks.blackbox.harness import RetrievedRegion
from benchmarks.blackbox.project_cases import (
    DEFAULT_CASES,
    KINDS,
    ProjectCase,
    TruthRegion,
    aggregate,
    classify_error,
    compare,
    load_manifest,
    query_family_results,
    score_case,
)


def test_manifest_covers_every_relation_kind() -> None:
    cases = load_manifest(DEFAULT_CASES)

    assert len(cases) == 35
    counts = {kind: sum(case.kind == kind for case in cases) for kind in KINDS}
    assert all(count >= 5 for count in counts.values()), counts
    assert all(case.test_paths for case in cases if case.kind == "test_mapping")
    assert len({case.instance_id for case in cases}) >= 7


def test_query_family_variants_cannot_change_truth(tmp_path: Path) -> None:
    manifest = json.loads(DEFAULT_CASES.read_text())
    case = manifest["cases"][0]
    manifest["cases"] = [
        {**case, "family_id": "need", "query_form": "original"},
        {
            **case,
            "id": "paraphrase",
            "query": "Find its uses",
            "family_id": "need",
            "query_form": "imperative",
            "supporting_regions": [],
        },
    ]
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="share snapshot and truth"):
        load_manifest(path)


def test_query_families_weight_needs_equally_and_keep_transport_errors() -> None:
    metrics = score_case(_case(), (RetrievedRegion("src/a.py", 10, 10),))
    rows = [
        {
            "family_id": "many",
            "query_form": str(i),
            "kind": "call_chain",
            "status": "ok",
            "metrics": metrics,
            "elapsed_ms": 10,
        }
        for i in range(4)
    ]
    rows.append(
        {
            "family_id": "single",
            "query_form": "original",
            "kind": "call_chain",
            "status": "error",
        }
    )
    grouped = query_family_results(rows)
    assert aggregate(rows)["primary_hit_at_3"] == 0.8
    assert grouped["family_summary"]["primary_hit_at_3"] == 0.5
    assert grouped["family_summary"]["worst_primary_hit_at_3"] == 0.5
    assert grouped["family_summary"]["error_families"] == 1


def _case(**overrides) -> ProjectCase:
    values = {
        "id": "case",
        "instance_id": "snap",
        "kind": "call_chain",
        "query": "How does `a` reach `c`?",
        "rationale": "a -> b -> c",
        "primary_regions": (
            TruthRegion("src/a.py", 10, 20, hop=0),
            TruthRegion("src/b.py", 1, 5, hop=1),
        ),
        "supporting_regions": (TruthRegion("src/c.py", 30, 40, hop=2),),
        "must_not_paths": ("docs/a.md",),
        "test_paths": (),
    }
    values.update(overrides)
    return ProjectCase(**values)


def test_regions_overlap_by_line_and_hops_close_the_chain() -> None:
    case = _case()
    retrieved = (
        RetrievedRegion("src/a.py", 18, 40),
        RetrievedRegion("src/c.py", 25, 31),
        RetrievedRegion("src/b.py", 6, 9),
    )

    metrics = score_case(case, retrieved)

    assert metrics["primary_top1"] == 1.0
    assert metrics["primary_recall"] == 0.5
    assert metrics["supporting_recall"] == 1.0
    assert metrics["hop_recall"] == pytest.approx(2 / 3)
    assert metrics["chain_closed"] == 0.0
    assert metrics["truth_region_share"] == pytest.approx(2 / 3)
    assert metrics["error_class"] == "none"


def test_error_classes_follow_severity_order() -> None:
    case = _case(test_paths=("tests/test_a.py",))

    missing_head = score_case(case, (RetrievedRegion("src/c.py", 30, 30),))
    assert missing_head["error_class"] == "exact_miss"

    distractor = score_case(
        case,
        (
            RetrievedRegion("src/a.py", 10, 12),
            RetrievedRegion("docs/a.md", 1, 3),
            RetrievedRegion("tests/test_a.py", 1, 3),
            RetrievedRegion("src/c.py", 30, 30),
        ),
    )
    assert distractor["error_class"] == "distractor"
    assert distractor["distractor_head"] == 1.0

    tests_missing = score_case(
        case,
        (RetrievedRegion("src/a.py", 10, 12), RetrievedRegion("src/c.py", 30, 30)),
    )
    assert tests_missing["error_class"] == "test_missing"

    relation_missing = score_case(
        case,
        (RetrievedRegion("src/a.py", 10, 12), RetrievedRegion("tests/test_a.py", 1, 1)),
    )
    assert relation_missing["error_class"] == "relation_missing"

    redundant = score_case(
        _case(supporting_regions=()),
        (
            RetrievedRegion("src/a.py", 10, 12),
            *(RetrievedRegion(f"src/other{index}.py", 1, 5) for index in range(4)),
        ),
    )
    assert redundant["error_class"] == "redundant"
    assert classify_error(_case(), {**redundant, "truth_region_share": 1.0}) == "none"


def test_aggregate_counts_errors_and_keeps_failed_cases_as_zero() -> None:
    ok = score_case(_case(), (RetrievedRegion("src/a.py", 10, 10),))
    summary = aggregate(
        [
            {"status": "ok", "kind": "call_chain", "metrics": ok, "elapsed_ms": 40},
            {"status": "error", "kind": "call_chain"},
        ]
    )

    assert summary["cases"] == 2
    assert summary["successful_cases"] == 1
    assert summary["primary_top1"] == 0.5
    assert summary["error_classes"]["relation_missing"] == 1
    assert summary["error_classes"]["exact_miss"] == 1
    assert summary["p50_elapsed_ms"] == 40


def test_compare_rejects_reports_with_different_truth(tmp_path: Path) -> None:
    import json

    base = {
        "suite": "project_cases",
        "schema_version": 1,
        "source_revisions": {"project_cases_sha256": "a"},
        "case_ids": ["x"],
        "label": "a",
        "summary": aggregate([]),
        "by_kind": {kind: aggregate([]) for kind in KINDS},
    }
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    first.write_text(json.dumps(base))
    second.write_text(json.dumps({**base, "source_revisions": {"x": "b"}}))

    assert "Variant" in compare([first])
    with pytest.raises(ValueError):
        compare([first, second])

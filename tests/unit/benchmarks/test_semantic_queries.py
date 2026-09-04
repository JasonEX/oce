import json
from pathlib import Path

import pytest

from benchmarks.blackbox.semantic_queries import (
    DEFAULT_CASES,
    RelevantPath,
    aggregate,
    compare,
    load_manifest,
    score_paths,
)


def test_reviewed_manifest_balances_intents() -> None:
    cases = load_manifest(DEFAULT_CASES)

    assert len(cases) == 39
    assert {
        kind: sum(case.kind == kind for case in cases)
        for kind in {
            "feature",
            "overview",
            "call_chain",
        }
    } == {"feature": 13, "overview": 13, "call_chain": 13}


def test_score_paths_uses_grades_and_deduplicates_files() -> None:
    relevant = (
        RelevantPath("primary.py", 3, "primary"),
        RelevantPath("support.py", 2, "support"),
        RelevantPath("context.py", 1, "context"),
    )

    perfect = score_paths(relevant, ("primary.py", "support.py", "context.py"))
    reordered = score_paths(
        relevant, ("support.py", "noise.py", "support.py", "primary.py")
    )

    assert perfect["ndcg_at_10"] == 1.0
    assert perfect["weighted_recall_at_10"] == 1.0
    assert reordered["top1_relevant"] == 1.0
    assert reordered["top1_primary"] == 0.0
    assert reordered["weighted_recall_at_10"] == pytest.approx(5 / 6)
    assert reordered["ndcg_at_10"] < perfect["ndcg_at_10"]


def test_aggregate_preserves_failed_observation_as_zero_utility() -> None:
    summary = aggregate(
        [
            {
                "status": "ok",
                "metrics": {
                    "top1_relevant": 1.0,
                    "top1_primary": 1.0,
                    "mrr": 1.0,
                    "hit_at_5": 1.0,
                    "weighted_recall_at_5": 1.0,
                    "weighted_recall_at_10": 1.0,
                    "primary_recall_at_10": 1.0,
                    "ndcg_at_10": 1.0,
                },
                "elapsed_ms": 10,
                "returned_chars": 100,
                "hit_count": 2,
            },
            {"status": "error"},
        ]
    )

    assert summary["successful_cases"] == 1
    assert summary["error_cases"] == 1
    assert summary["ndcg_at_10"] == 0.5
    assert summary["mean_returned_chars"] == 50


def test_compare_refuses_different_case_sets(tmp_path: Path) -> None:
    paths = []
    for index, case_id in enumerate(("a", "b")):
        path = tmp_path / f"{index}.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "suite": "semantic_queries",
                    "source_revisions": {"cases": "same"},
                    "case_ids": [case_id],
                }
            ),
            encoding="utf-8",
        )
        paths.append(path)

    with pytest.raises(ValueError, match="different benchmark truth"):
        compare(paths)

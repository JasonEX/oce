import json
from dataclasses import replace

import pytest

from benchmarks.blackbox.harness import RetrievedRegion, parse_retrieved_regions
from benchmarks.blackbox.swe_data import (
    BenchmarkCase,
    Region,
    case_fingerprint,
    parse_patch_regions,
    select_frozen_cases,
)
from benchmarks.blackbox.swe_explore import (
    SWE_EXPLORE_METRIC_NAMES,
    compare,
    score_case,
    score_swe_explore,
)


def test_frozen_selection_rejects_changed_questions_truth_and_duplicate_ids(tmp_path):
    case = BenchmarkCase(
        "case-1",
        "example/repo",
        "a" * 40,
        "find the handler",
        (Region("src/a.py", 1, 2),),
        (),
        ("src/a.py",),
    )
    manifest = tmp_path / "selection.json"
    row = {"instance_id": case.instance_id, "sha256": case_fingerprint(case)}
    manifest.write_text(json.dumps({"schema_version": 1, "cases": [row]}))
    assert select_frozen_cases([case], manifest) == [case]
    for changed in (
        replace(case, query="another question"),
        replace(case, core_files=("src/b.py",)),
        replace(case, base_commit="b" * 40),
    ):
        with pytest.raises(ValueError, match="contents changed"):
            select_frozen_cases([changed], manifest)
    manifest.write_text(json.dumps({"schema_version": 1, "cases": [row, row]}))
    with pytest.raises(ValueError, match="duplicate"):
        select_frozen_cases([case], manifest)


def test_parse_patch_regions_uses_base_tree_lines() -> None:
    patch = """diff --git a/src/example.py b/src/example.py
--- a/src/example.py
+++ b/src/example.py
@@ -10,3 +10,4 @@
 context
-old value
+new value
+extra value
 trailing context
@@ -30,0 +33,2 @@
+inserted
"""

    assert parse_patch_regions(patch) == (
        Region(path="src/example.py", start=11, end=12),
        Region(path="src/example.py", start=30, end=30),
    )


def test_parse_retrieved_regions_ignores_source_text() -> None:
    formatted = """The following code sections were retrieved:
Path: src/a.py
Lines: 8-15
     8\tPath: not-a-header

Path: src/b.py
Lines: 20-24
"""

    assert parse_retrieved_regions(formatted) == (
        RetrievedRegion(path="src/a.py", start=8, end=15),
        RetrievedRegion(path="src/b.py", start=20, end=24),
    )


def test_score_case_keeps_edit_and_context_truth_separate() -> None:
    case = BenchmarkCase(
        instance_id="example-1",
        repo="example/repo",
        base_commit="a" * 40,
        query="fix it",
        edit_regions=(Region("src/edit.py", 40, 45),),
        core_regions=(Region("src/context.py", 10, 20),),
        core_files=("src/context.py",),
    )
    retrieved = (
        RetrievedRegion("src/context.py", 12, 18),
        RetrievedRegion("src/edit.py", 1, 10),
        RetrievedRegion("src/edit.py", 42, 44),
    )

    assert score_case(case, retrieved) == {
        "edit_top1": 0.0,
        "edit_file_recall_at_10": 1.0,
        "edit_mrr": 0.5,
        "edit_region_recall_at_10": 1.0,
        "core_top1": 1.0,
        "core_file_recall_at_10": 1.0,
        "core_mrr": 1.0,
        "core_region_recall_at_10": 1.0,
    }


def test_score_swe_explore_uses_pinned_reference_metric_contract(tmp_path) -> None:
    truth = {"read_core_files": ["src/context.py"]}
    case = BenchmarkCase(
        instance_id="example-1",
        repo="example/repo",
        base_commit="a" * 40,
        query="fix it",
        edit_regions=(Region("src/edit.py", 40, 45),),
        core_regions=(Region("src/context.py", 10, 20),),
        core_files=("src/context.py",),
        explore_truth=truth,
    )
    retrieved = (RetrievedRegion("src/context.py", 12, 18),)

    def computer(predictions, ground_truth, repo_dir):
        assert predictions == [("src/context.py", 12, 18)]
        assert ground_truth is truth
        assert repo_dir == tmp_path
        return ({name: 0.25 for name in SWE_EXPLORE_METRIC_NAMES}, {})

    assert score_swe_explore(case, retrieved, tmp_path, computer) == {
        f"swe_explore_{name}": 0.25 for name in SWE_EXPLORE_METRIC_NAMES
    }


def test_compare_preserves_all_failed_observation(tmp_path) -> None:
    result_path = tmp_path / "failed.json"
    result_path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "suite": "swe_explore",
                "source_revisions": {"dataset": "pinned"},
                "label": "failed-run",
                "case_ids": ["example-1"],
                "summary": {
                    "successful_cases": 0,
                    "cases": 1,
                    "edit_top1": 0.0,
                    "edit_file_recall_at_10": 0.0,
                    "edit_region_recall_at_10": 0.0,
                    "core_file_recall_at_10": 0.0,
                    "core_region_recall_at_10": 0.0,
                    "swe_explore_recall": 0.0,
                    "swe_explore_ndcg_at_500": 0.0,
                    "swe_explore_context_efficiency": 0.0,
                    "mean_elapsed_ms": None,
                    "mean_returned_chars": 0.0,
                },
            }
        ),
        encoding="utf-8",
    )

    report = compare([result_path])

    assert "| failed-run | 0/1 |" in report
    assert "| - | 0 |" in report

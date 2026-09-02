import json
import sqlite3
import subprocess
from contextlib import closing

import pytest

from benchmarks.rerank_routing import (
    AuditRow,
    RoutingAnchor,
    _read_audit_row,
    aggregate,
    compare,
    expand_cases,
    expected_route,
)
from oce.domain.services.query_classifier import classify_query_intent


def test_expand_cases_builds_balanced_queries_with_source_reference_truth(
    tmp_path,
) -> None:
    root = tmp_path / "snapshots" / "example-1"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "definition.py").write_text(
        "class Target:\n    pass\n", encoding="utf-8"
    )
    (root / "src" / "consumer.py").write_text(
        "from .definition import Target\n", encoding="utf-8"
    )
    (root / "tests" / "test_target.py").write_text("Target()\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)

    cases = expand_cases(
        tmp_path,
        (
            RoutingAnchor(
                id="example-target",
                instance_id="example-1",
                identifier="Target",
                definition_path="src/definition.py",
            ),
        ),
    )

    assert [case.kind for case in cases] == ["symbol", "path", "reference"]
    assert cases[2].expected_paths == ("src/consumer.py",)
    assert [classify_query_intent(case.query).value for case in cases] == [
        case.expected_intent for case in cases
    ]


@pytest.mark.parametrize(
    "kind,runtime,expected",
    [
        (
            "symbol",
            {
                "api_rerank_enabled": True,
                "rerank_policy": "adaptive",
                "llm_rerank_enabled": False,
            },
            "skip:exact_definition",
        ),
        (
            "path",
            {
                "api_rerank_enabled": True,
                "rerank_policy": "always",
                "llm_rerank_enabled": True,
                "llm_rerank_policy": "adaptive",
            },
            "dedicated",
        ),
        (
            "reference",
            {
                "api_rerank_enabled": False,
                "llm_rerank_enabled": True,
                "llm_rerank_policy": "adaptive",
            },
            "skip:reference_keep_coverage",
        ),
        (
            "reference",
            {
                "api_rerank_enabled": True,
                "rerank_policy": "adaptive",
                "llm_rerank_enabled": True,
                "llm_rerank_policy": "always",
            },
            "dedicated+llm",
        ),
        (
            "symbol",
            {
                "api_rerank_enabled": False,
                "llm_rerank_enabled": False,
            },
            "skip:no_reranker_enabled",
        ),
    ],
)
def test_expected_route_is_independent_of_product_planner(kind, runtime, expected):
    assert expected_route(kind, runtime) == expected


def test_read_audit_row_uses_watermark_and_exact_query(tmp_path) -> None:
    database = tmp_path / "oce.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            """
            CREATE TABLE retrieval_metrics (
                id INTEGER PRIMARY KEY,
                intent TEXT,
                rerank_route TEXT,
                total_ms INTEGER,
                rerank_ms INTEGER,
                llm_rerank_ms INTEGER,
                query_text TEXT
            )
            """
        )
        connection.executemany(
            "INSERT INTO retrieval_metrics VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                (1, "symbol", "dedicated", 9, 3, None, "same"),
                (2, "symbol", "skip:exact_definition", 5, None, None, "same"),
            ),
        )
        connection.commit()

    assert _read_audit_row(database, 1, "same") == AuditRow(
        2, "symbol", "skip:exact_definition", 5, None, None
    )


def _result(kind: str, *, elapsed: int, skipped: bool) -> dict[str, object]:
    return {
        "kind": kind,
        "status": "ok",
        "metrics": {
            "top1": 1.0,
            "hit_at_10": 1.0,
            "mrr": 1.0,
            "path_recall_at_10": 0.5,
        },
        "intent_conformant": True,
        "route_conformant": True,
        "stage_conformant": True,
        "rerank_route": "skip:path_evidence" if skipped else "dedicated",
        "elapsed_ms": elapsed,
        "rerank_ms": None if skipped else 4,
        "llm_rerank_ms": None,
    }


def test_aggregate_preserves_route_latency_and_quality_axes() -> None:
    summary = aggregate(
        [
            _result("symbol", elapsed=10, skipped=False),
            _result("path", elapsed=20, skipped=True),
        ]
    )

    assert summary["hit_at_10"] == 1.0
    assert summary["skip_rate"] == 0.5
    assert summary["intent_conformance"] == 1.0
    assert summary["p95_elapsed_ms"] == 20
    assert summary["mean_rerank_ms"] == 4


def test_compare_refuses_different_case_sets(tmp_path) -> None:
    paths = []
    for index, case_id in enumerate(("a", "b")):
        path = tmp_path / f"{index}.json"
        path.write_text(
            json.dumps({"case_ids": [case_id], "summary": {}, "by_kind": {}}),
            encoding="utf-8",
        )
        paths.append(path)

    with pytest.raises(ValueError, match="different ordered routing case sets"):
        compare(paths)

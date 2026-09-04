import json
import subprocess

import pytest

from benchmarks.blackbox.corpus import RepositorySnapshot
from benchmarks.blackbox.short_queries import (
    DEFAULT_CASES,
    ShortQueryAnchor,
    aggregate,
    compare,
    expand_cases,
    load_anchors,
)


def test_reviewed_anchor_manifest_covers_all_snapshots() -> None:
    anchors = load_anchors(DEFAULT_CASES)

    assert len(anchors) == 40
    assert len({anchor.instance_id for anchor in anchors}) == 13


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
            ShortQueryAnchor(
                id="example-target",
                instance_id="example-1",
                identifier="Target",
                definition_path="src/definition.py",
            ),
        ),
        {
            "example-1": RepositorySnapshot(
                id="example-1",
                repo="example/repo",
                revision="a" * 40,
                code_language="python",
            )
        },
    )

    assert [case.kind for case in cases] == ["symbol", "path", "reference"] * 2
    assert [case.query_language for case in cases] == ["en"] * 3 + ["zh"] * 3
    assert cases[2].expected_paths == ("src/consumer.py",)
    assert cases[5].expected_paths == ("src/consumer.py",)


@pytest.mark.parametrize(
    ("code_language", "definition_path", "consumer_path", "definition"),
    (
        (
            "typescript",
            "src/configureStore.ts",
            "src/index.ts",
            "export function configureStore<T>(): T { throw new Error() }\n",
        ),
        (
            "rust",
            "src/extract.rs",
            "src/lib.rs",
            "pub trait FromRequest {}\n",
        ),
        ("go", "tree.go", "gin.go", "type Params []Param\n"),
        (
            "java",
            "src/main/java/Gson.java",
            "src/main/java/Other.java",
            "public final class Gson {}\n",
        ),
        (
            "c",
            "src/jv.c",
            "src/main.c",
            "jv jv_string_fmt(const char* fmt, ...) {\n}\n",
        ),
        (
            "bash",
            "lib/tracing.bash",
            "lib/common.bash",
            "bats_print_stack_trace() {\n}\n",
        ),
    ),
)
def test_expand_cases_supports_typescript_and_rust_anchors(
    tmp_path,
    code_language,
    definition_path,
    consumer_path,
    definition,
) -> None:
    identifier = {
        "typescript": "configureStore",
        "rust": "FromRequest",
        "go": "Params",
        "java": "Gson",
        "c": "jv_string_fmt",
        "bash": "bats_print_stack_trace",
    }[code_language]
    root = tmp_path / "snapshots" / "example-1"
    (root / definition_path).parent.mkdir(parents=True)
    (root / definition_path).write_text(definition, encoding="utf-8")
    (root / consumer_path).write_text(f"// use {identifier}\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)

    cases = expand_cases(
        tmp_path,
        (
            ShortQueryAnchor(
                id="example-anchor",
                instance_id="example-1",
                identifier=identifier,
                definition_path=definition_path,
            ),
        ),
        {
            "example-1": RepositorySnapshot(
                id="example-1",
                repo="example/repo",
                revision="a" * 40,
                code_language=code_language,
            )
        },
    )

    assert len(cases) == 6
    assert cases[0].code_language == code_language
    assert cases[2].expected_paths == (consumer_path,)


def _result(kind: str, *, elapsed: int) -> dict[str, object]:
    return {
        "kind": kind,
        "status": "ok",
        "metrics": {
            "top1": 1.0,
            "hit_at_10": 1.0,
            "mrr": 1.0,
            "path_recall_at_10": 0.5,
        },
        "elapsed_ms": elapsed,
        "returned_chars": 100,
        "hit_count": 2,
    }


def test_aggregate_preserves_latency_and_quality_axes() -> None:
    summary = aggregate(
        [
            _result("symbol", elapsed=10),
            _result("path", elapsed=20),
        ]
    )

    assert summary["hit_at_10"] == 1.0
    assert summary["p95_elapsed_ms"] == 20
    assert summary["mean_returned_chars"] == 100
    assert summary["mean_hit_count"] == 2


def test_compare_refuses_different_case_sets(tmp_path) -> None:
    paths = []
    for index, case_id in enumerate(("a", "b")):
        path = tmp_path / f"{index}.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "suite": "short_queries",
                    "source_revisions": {"cases": "same"},
                    "case_ids": [case_id],
                    "summary": {},
                    "by_kind": {},
                }
            ),
            encoding="utf-8",
        )
        paths.append(path)

    with pytest.raises(ValueError, match="different benchmark truth"):
        compare(paths)

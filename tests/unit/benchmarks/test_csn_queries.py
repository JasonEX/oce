from benchmarks.blackbox.csn_data import FunctionCase, query_from_docstring
from benchmarks.blackbox.csn_queries import DEFAULT_MANIFEST, load_manifest, score_case
from benchmarks.blackbox.harness import RetrievedRegion


def test_manifest_is_balanced_and_pinned() -> None:
    snapshots, cases = load_manifest(DEFAULT_MANIFEST)

    assert len(snapshots) == 8
    assert len(cases) == 80
    languages = {snapshot.code_language for snapshot in snapshots.values()}
    assert languages == {"python", "go", "java", "javascript"}
    assert all(len(snapshot.revision) == 40 for snapshot in snapshots.values())
    # Both described-only and name-bearing queries are present so the report
    # can separate lexical luck from semantic retrieval.
    assert any(case.name_in_query for case in cases)
    assert any(not case.name_in_query for case in cases)


def test_docstring_query_keeps_the_first_paragraph_only() -> None:
    text = (
        "Create an instance of Axios with the given defaults applied.\n"
        "\n"
        "@param {Object} defaultConfig The default config\n"
        "@return {Axios} A new instance\n"
    )
    assert (
        query_from_docstring(text)
        == "Create an instance of Axios with the given defaults applied."
    )
    assert query_from_docstring("Short one.") is None
    assert query_from_docstring(":param x: only tags here and nothing else") is None


def test_region_scoring_overlaps_function_lines() -> None:
    case = FunctionCase(
        id="c",
        instance_id="s",
        query="q " * 6,
        path="lib/axios.js",
        start=15,
        end=26,
        func_name="createInstance",
        name_in_query=False,
    )
    metrics = score_case(
        case,
        (
            RetrievedRegion("lib/axios.js", 1, 12),
            RetrievedRegion("lib/other.js", 1, 40),
            RetrievedRegion("lib/axios.js", 20, 60),
        ),
    )
    assert metrics["region_top1"] == 0.0
    assert metrics["region_hit_at_5"] == 1.0
    assert metrics["region_mrr"] == 1 / 3
    assert metrics["file_top1"] == 1.0

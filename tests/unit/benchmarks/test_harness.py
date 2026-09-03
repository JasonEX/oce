import httpx
import pytest

from benchmarks.blackbox.harness import (
    admin_stats,
    ensure_comparable,
    metadata,
    model_usage_delta,
    server_configuration,
)


def test_metadata_uses_only_non_secret_environment_allowlist(monkeypatch) -> None:
    monkeypatch.setenv("API_KEY", "secret")
    monkeypatch.setenv("EMBED_API_KEY", "secret")
    monkeypatch.setenv("EMBED_MODEL", "example-embedder")

    assert metadata(()) == {"EMBED_MODEL": "example-embedder"}


def test_optional_admin_stats_failure_does_not_abort_quality_run(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise httpx.ConnectError("unavailable")

    monkeypatch.setattr("benchmarks.blackbox.harness.httpx.get", fail)

    assert admin_stats("http://127.0.0.1:8986", "admin-key") is None


def test_server_configuration_captures_only_operational_sections(monkeypatch) -> None:
    def respond(*args, **kwargs):
        return httpx.Response(
            200,
            request=httpx.Request("GET", "http://server/admin/index-stats"),
            json={
                "runtime": {"exact_enabled": True},
                "profile": {"state": "compatible"},
                "query_cache": {"enabled": False},
                "dense": {"available": True},
                "path": {"available": True},
                "metadata": {"blobs_total": 20},
            },
        )

    monkeypatch.setattr("benchmarks.blackbox.harness.httpx.get", respond)

    assert server_configuration("http://server", "admin-key") == {
        "retrieval": {"exact_enabled": True},
        "index_profile": {"state": "compatible"},
        "query_cache": {"enabled": False},
        "dense": {"available": True},
        "path": {"available": True},
    }


def test_model_usage_delta_keeps_new_model_kinds() -> None:
    before = {
        "tokens": [
            {
                "kind": "embed",
                "calls": 3,
                "prompt_tokens": 30,
                "completion_tokens": 0,
                "total_tokens": 30,
            }
        ]
    }
    after = {
        "tokens": [
            {
                "kind": "embed",
                "calls": 5,
                "prompt_tokens": 50,
                "completion_tokens": 0,
                "total_tokens": 50,
            },
            {
                "kind": "rerank",
                "calls": 1,
                "prompt_tokens": 10,
                "completion_tokens": 0,
                "total_tokens": 10,
            },
        ]
    }

    assert model_usage_delta(before, after) == {
        "embed": {
            "calls": 2,
            "prompt_tokens": 20,
            "completion_tokens": 0,
            "total_tokens": 20,
        },
        "rerank": {
            "calls": 1,
            "prompt_tokens": 10,
            "completion_tokens": 0,
            "total_tokens": 10,
        },
    }


def test_comparison_identity_binds_suite_truth_and_ordered_cases() -> None:
    report = {
        "schema_version": 1,
        "suite": "semantic_queries",
        "source_revisions": {"cases": "abc"},
        "case_ids": ["first", "second"],
    }
    ensure_comparable([report, dict(report)], suite="semantic_queries")

    changed = dict(report, case_ids=["second", "first"])
    with pytest.raises(ValueError, match="different benchmark truth"):
        ensure_comparable([report, changed], suite="semantic_queries")

    changed_schema = dict(report, schema_version=2)
    with pytest.raises(ValueError, match="different benchmark truth"):
        ensure_comparable([report, changed_schema], suite="semantic_queries")


def test_comparison_identity_rejects_wrong_or_incomplete_suite() -> None:
    report = {
        "schema_version": 1,
        "suite": "short_queries",
        "source_revisions": {"cases": "abc"},
        "case_ids": ["case"],
    }

    with pytest.raises(ValueError, match="expected 'semantic_queries'"):
        ensure_comparable([report], suite="semantic_queries")
    with pytest.raises(ValueError, match="lacks truth provenance"):
        ensure_comparable(
            [
                {
                    "schema_version": 1,
                    "suite": "semantic_queries",
                    "case_ids": ["case"],
                }
            ],
            suite="semantic_queries",
        )

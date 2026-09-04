import json
from pathlib import Path

import pytest

from benchmarks.blackbox.corpus import (
    load_corpus,
    select_snapshots,
)


def test_curated_corpus_has_pinned_multilanguage_snapshots() -> None:
    snapshots = load_corpus(
        Path(__file__).resolve().parents[3]
        / "benchmarks"
        / "blackbox"
        / "curated_corpus.json"
    )

    assert len(snapshots) == 13
    assert {snapshot.code_language for snapshot in snapshots} == {
        "python",
        "typescript",
        "javascript",
        "rust",
        "go",
        "c",
        "csharp",
        "java",
        "bash",
    }
    assert all(len(snapshot.revision) == 40 for snapshot in snapshots)


def test_corpus_rejects_unpinned_revision(tmp_path) -> None:
    path = tmp_path / "corpus.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "snapshots": [
                    {
                        "id": "bad",
                        "repo": "owner/repo",
                        "revision": "main",
                        "code_language": "python",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid or duplicate"):
        load_corpus(path)


def test_select_snapshots_rejects_unknown_case() -> None:
    with pytest.raises(ValueError, match="unknown snapshots"):
        select_snapshots((), ("missing",))

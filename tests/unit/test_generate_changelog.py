from scripts.generate_changelog import group_commits


def test_group_commits_deduplicates_equivalent_history_entries() -> None:
    commits = [
        ("first", "fix(indexing): keep blobs pending", ""),
        ("second", "fix(indexing): keep blobs pending", ""),
    ]

    groups = group_commits(commits)

    assert groups["Fixed"] == ["- **indexing**: keep blobs pending"]

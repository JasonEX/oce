from scripts.generate_changelog import group_commits


def test_group_commits_deduplicates_equivalent_history_entries() -> None:
    commits = [
        ("first", "fix(indexing): keep blobs pending", ""),
        ("second", "fix(indexing): keep blobs pending", ""),
    ]

    groups = group_commits(commits)

    assert groups["Fixed"] == ["- **indexing**: keep blobs pending"]


def test_prepend_keeps_released_sections_and_replaces_unreleased(
    tmp_path, monkeypatch
) -> None:
    from scripts import generate_changelog

    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "# Changelog\n\nheader\n\n"
        "## [Unreleased]\n\n### Added\n\n- pending entry\n\n"
        "## [0.3.0] - 2026-09-02\n\n### Fixed\n\n- older entry\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(generate_changelog, "CHANGELOG", changelog)

    generate_changelog.prepend("## [0.4.0] - 2026-09-13\n\n### Fixed\n\n- new entry\n")

    text = changelog.read_text(encoding="utf-8")
    assert text.startswith("# Changelog\n\nheader\n\n## [0.4.0] - 2026-09-13\n")
    assert "## [Unreleased]" not in text
    assert "- pending entry" not in text
    assert text.endswith("## [0.3.0] - 2026-09-02\n\n### Fixed\n\n- older entry\n")


def test_prepend_unreleased_replaces_only_the_unreleased_section(
    tmp_path, monkeypatch
) -> None:
    from scripts import generate_changelog

    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "# Changelog\n\n## [Unreleased]\n\n- old\n\n## [0.3.0] - 2026-09-02\n\n- kept\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(generate_changelog, "CHANGELOG", changelog)

    generate_changelog.prepend("## [Unreleased]\n\n- regenerated\n")

    text = changelog.read_text(encoding="utf-8")
    assert text.count("## [Unreleased]") == 1
    assert "- old" not in text and "- regenerated" in text
    assert text.endswith("## [0.3.0] - 2026-09-02\n\n- kept\n")

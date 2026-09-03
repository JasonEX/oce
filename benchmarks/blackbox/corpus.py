"""Pinned repository snapshots shared by black-box benchmark suites."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from benchmarks.blackbox.harness import git_output

CodeLanguage = Literal["python", "typescript", "rust"]
_LANGUAGES: tuple[CodeLanguage, ...] = ("python", "typescript", "rust")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_REPOSITORY_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?"
)
_REVISION_RE = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class RepositorySnapshot:
    id: str
    repo: str
    revision: str
    code_language: CodeLanguage


def load_corpus(path: Path) -> tuple[RepositorySnapshot, ...]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or not isinstance(value.get("snapshots"), list)
    ):
        raise ValueError("corpus manifest must use schema_version 1")

    snapshots: list[RepositorySnapshot] = []
    seen_ids: set[str] = set()
    for raw in value["snapshots"]:
        if not isinstance(raw, dict):
            raise ValueError("invalid repository snapshot")
        language = str(raw.get("code_language", ""))
        if language not in _LANGUAGES:
            raise ValueError(f"unsupported code language: {language!r}")
        snapshot = RepositorySnapshot(
            id=str(raw.get("id", "")),
            repo=str(raw.get("repo", "")),
            revision=str(raw.get("revision", "")),
            code_language=cast(CodeLanguage, language),
        )
        if (
            _ID_RE.fullmatch(snapshot.id) is None
            or snapshot.id in seen_ids
            or _REPOSITORY_RE.fullmatch(snapshot.repo) is None
            or _REVISION_RE.fullmatch(snapshot.revision) is None
        ):
            raise ValueError(
                f"invalid or duplicate repository snapshot: {snapshot.id!r}"
            )
        seen_ids.add(snapshot.id)
        snapshots.append(snapshot)
    if not snapshots:
        raise ValueError("corpus manifest is empty")
    return tuple(snapshots)


def select_snapshots(
    snapshots: Sequence[RepositorySnapshot], ids: Iterable[str]
) -> dict[str, RepositorySnapshot]:
    by_id = {snapshot.id: snapshot for snapshot in snapshots}
    requested = set(ids)
    missing = sorted(requested - by_id.keys())
    if missing:
        raise ValueError(f"query cases reference unknown snapshots: {missing}")
    return {snapshot_id: by_id[snapshot_id] for snapshot_id in sorted(requested)}


def snapshot_path(workdir: Path, snapshot_id: str) -> Path:
    return workdir / "snapshots" / snapshot_id


def prepare_snapshots(workdir: Path, snapshots: Sequence[RepositorySnapshot]) -> None:
    ids = [snapshot.id for snapshot in snapshots]
    if len(ids) != len(set(ids)) or any(
        _ID_RE.fullmatch(snapshot.id) is None
        or _REPOSITORY_RE.fullmatch(snapshot.repo) is None
        or _REVISION_RE.fullmatch(snapshot.revision) is None
        or snapshot.code_language not in _LANGUAGES
        for snapshot in snapshots
    ):
        raise ValueError("invalid or duplicate repository snapshot")
    for repo in sorted({snapshot.repo for snapshot in snapshots}):
        bare = workdir / "repositories" / f"{repo.replace('/', '__')}.git"
        if not bare.exists():
            bare.parent.mkdir(parents=True, exist_ok=True)
            git_output("init", "--bare", str(bare))
            git_output(
                f"--git-dir={bare}",
                "remote",
                "add",
                "origin",
                f"https://github.com/{repo}.git",
            )
        repo_snapshots = [item for item in snapshots if item.repo == repo]
        missing_revisions: list[str] = []
        for snapshot in repo_snapshots:
            try:
                git_output(
                    f"--git-dir={bare}",
                    "cat-file",
                    "-e",
                    f"{snapshot.revision}^{{commit}}",
                )
            except subprocess.CalledProcessError:
                missing_revisions.append(snapshot.revision)
        if missing_revisions:
            git_output(
                f"--git-dir={bare}",
                "fetch",
                "--depth=1",
                "--filter=blob:none",
                "origin",
                *missing_revisions,
            )
        for snapshot in repo_snapshots:
            root = snapshot_path(workdir, snapshot.id)
            if root.exists():
                head = git_output("rev-parse", "HEAD", cwd=root)
                dirty = git_output("status", "--porcelain", cwd=root)
                if head != snapshot.revision or dirty:
                    raise ValueError(
                        f"existing snapshot is not the clean required revision: {root}"
                    )
                continue
            root.parent.mkdir(parents=True, exist_ok=True)
            git_output(
                f"--git-dir={bare}",
                "worktree",
                "add",
                "--detach",
                str(root),
                snapshot.revision,
            )

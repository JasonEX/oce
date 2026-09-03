"""Pinned SWE-bench/SWE-Explore cases and repository snapshots.

Dataset acquisition is independent from both the benchmark runner and the OCE
server.  The files and Git worktrees live under the caller's benchmark cache.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from benchmarks.blackbox.corpus import RepositorySnapshot
from benchmarks.blackbox.harness import sha256_file

SWE_BENCH_REVISION = "78f471bf655a3137b2e8a75af1501690ec009ec3"
SWE_BENCH_SHA256 = "030cfd7f2a704c4c0226e7f104c725a3b41230b1d3517f9c915ad7ea5be3fa25"
SWE_BENCH_URL = (
    "https://huggingface.co/datasets/SWE-bench/SWE-bench_Verified/resolve/"
    f"{SWE_BENCH_REVISION}/data/test-00000-of-00001.parquet"
)
SWE_EXPLORE_REVISION = "bdb0ae45d7c337d9e1dc3ebfe2a0af6bc7c1fbd9"
SWE_EXPLORE_SHA256 = "dc4f114ececd0bfb987361c26ae5e2440456e2cccb36adfccb09ea5385aec202"
SWE_EXPLORE_URL = (
    "https://huggingface.co/datasets/SWE-Explore-Bench/SWE-Explore-Bench/"
    f"resolve/{SWE_EXPLORE_REVISION}/bench.final.public.jsonl"
)
SWE_EXPLORE_METRICS_REVISION = "5602f031f2d9562d0a805f83402b536e831a5a11"
SWE_EXPLORE_METRICS_SHA256 = (
    "316c1ded20d6487526fcfcd9be91d8cfa399e5baf912ecdcc2b29c92ba78f6af"
)
SWE_EXPLORE_METRICS_URL = (
    "https://raw.githubusercontent.com/Qiushao-E/SWE-Explore-Bench/"
    f"{SWE_EXPLORE_METRICS_REVISION}/quality/bench_metrics.py"
)

DEVELOPMENT_REPOSITORIES = (
    "pallets/flask",
    "psf/requests",
    "pytest-dev/pytest",
    "pylint-dev/pylint",
    "pydata/xarray",
)
STANDARD_PER_REPOSITORY = 5
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")


@dataclass(frozen=True)
class Region:
    path: str
    start: int
    end: int


@dataclass(frozen=True)
class BenchmarkCase:
    instance_id: str
    repo: str
    base_commit: str
    query: str
    edit_regions: tuple[Region, ...]
    core_regions: tuple[Region, ...]
    core_files: tuple[str, ...]
    explore_truth: dict[str, object] | None = None


def _download(url: str, destination: Path, expected_sha256: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and sha256_file(destination) == expected_sha256:
        return
    partial = destination.with_suffix(destination.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310
            with partial.open("wb") as output:
                shutil.copyfileobj(response, output)
        actual = sha256_file(partial)
        if actual != expected_sha256:
            raise RuntimeError(
                f"checksum mismatch for {destination.name}: "
                f"expected {expected_sha256}, got {actual}"
            )
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)


def prepare_datasets(workdir: Path) -> tuple[Path, Path, Path]:
    data_dir = workdir / "datasets"
    verified = data_dir / "swe-bench-verified.parquet"
    explore = data_dir / "swe-explore.jsonl"
    metrics = data_dir / "swe-explore-bench-metrics.py"
    _download(SWE_BENCH_URL, verified, SWE_BENCH_SHA256)
    _download(SWE_EXPLORE_URL, explore, SWE_EXPLORE_SHA256)
    _download(SWE_EXPLORE_METRICS_URL, metrics, SWE_EXPLORE_METRICS_SHA256)
    return verified, explore, metrics


def parse_patch_regions(patch: str) -> tuple[Region, ...]:
    """Return changed old-tree lines, which can be scored against the base commit."""
    changed_lines: dict[str, set[int]] = {}
    old_path: str | None = None
    old_line: int | None = None
    for line in patch.splitlines():
        if line.startswith("--- "):
            raw_path = line.removeprefix("--- ").split("\t", 1)[0]
            old_path = None if raw_path == "/dev/null" else raw_path.removeprefix("a/")
            old_line = None
            continue
        match = HUNK_RE.match(line)
        if match is not None:
            old_line = int(match.group(1))
            continue
        if old_path is None or old_line is None:
            continue
        if line.startswith("-"):
            changed_lines.setdefault(old_path, set()).add(max(old_line, 1))
            old_line += 1
        elif line.startswith("+"):
            changed_lines.setdefault(old_path, set()).add(max(old_line, 1))
        elif line.startswith(" "):
            old_line += 1

    regions: list[Region] = []
    for path, lines in changed_lines.items():
        start = end = min(lines)
        for line in sorted(lines - {start}):
            if line == end + 1:
                end = line
                continue
            regions.append(Region(path=path, start=start, end=end))
            start = end = line
        regions.append(Region(path=path, start=start, end=end))
    return tuple(regions)


def _load_explore(path: Path) -> dict[str, dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    with path.open(encoding="utf-8") as stream:
        for raw in stream:
            value = json.loads(raw)
            if value.get("dataset") == "verified":
                rows[str(value["instance_id"])] = value
    return rows


def _load_verified(path: Path) -> list[dict[str, object]]:
    try:
        import pyarrow.parquet as parquet
    except ImportError as exc:  # pragma: no cover - installation guidance
        raise RuntimeError("pyarrow is required; run `uv sync --extra dev`") from exc
    columns = ["instance_id", "repo", "base_commit", "problem_statement", "patch"]
    return cast(
        list[dict[str, object]], parquet.read_table(path, columns=columns).to_pylist()
    )


def _valid_region(value: object) -> Region | None:
    if not isinstance(value, dict):
        return None
    path = value.get("path")
    start = value.get("start")
    end = value.get("end")
    if (
        not isinstance(path, str)
        or not isinstance(start, int)
        or not isinstance(end, int)
        or not path
        or start < 1
        or end < start
    ):
        return None
    return Region(path=path, start=start, end=end)


def load_cases(workdir: Path, profile: str) -> list[BenchmarkCase]:
    verified_path, explore_path, _metrics_path = prepare_datasets(workdir)
    explore = _load_explore(explore_path)
    cases: list[BenchmarkCase] = []
    for row in _load_verified(verified_path):
        instance_id = str(row["instance_id"])
        context = explore.get(instance_id)
        if context is None:
            continue
        truth = context.get("ground_truth")
        if not isinstance(truth, dict):
            continue
        edit_regions = parse_patch_regions(str(row["patch"]))
        core_regions = tuple(
            region
            for item in truth.get("read_core_regions", ())
            if (region := _valid_region(item)) is not None
        )
        core_files = tuple(
            str(item) for item in truth.get("read_core_files", ()) if str(item)
        )
        if not edit_regions or not core_files:
            continue
        cases.append(
            BenchmarkCase(
                instance_id=instance_id,
                repo=str(row["repo"]),
                base_commit=str(row["base_commit"]),
                query=str(row["problem_statement"]),
                edit_regions=edit_regions,
                core_regions=core_regions,
                core_files=core_files,
                explore_truth=dict(truth),
            )
        )

    if profile == "verified":
        return sorted(cases, key=lambda item: item.instance_id)
    selected: list[BenchmarkCase] = []
    if profile == "standard":
        repositories = sorted({case.repo for case in cases})
        per_repo = STANDARD_PER_REPOSITORY
    else:
        repositories = DEVELOPMENT_REPOSITORIES
        per_repo = 1 if profile == "pilot" else 3
    for repo in repositories:
        candidates = [case for case in cases if case.repo == repo]
        candidates.sort(
            key=lambda item: hashlib.sha256(
                f"oce-swe-explore-v1:{item.instance_id}".encode()
            ).digest()
        )
        selected.extend(candidates[:per_repo])
    if not selected:
        raise ValueError(f"profile {profile!r} selected no cases")
    return sorted(selected, key=lambda item: (item.repo, item.instance_id))


def snapshots_for_cases(
    cases: Sequence[BenchmarkCase],
) -> tuple[RepositorySnapshot, ...]:
    return tuple(
        RepositorySnapshot(
            id=case.instance_id,
            repo=case.repo,
            revision=case.base_commit,
            code_language="python",
        )
        for case in cases
    )

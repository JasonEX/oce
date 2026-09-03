"""Evaluate OCE on real issue-resolution context from SWE-bench and SWE-Explore.

The source datasets are downloaded at pinned revisions and verified by SHA256. They are
kept outside the repository because SWE-Explore is distributed under CC BY-NC-ND 4.0.
This harness reports observations; it deliberately does not implement a release gate.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from typing import Any, cast

import httpx

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

DEFAULT_WORKDIR = Path.home() / ".cache" / "oce" / "swe-explore-v1"
DEVELOPMENT_REPOSITORIES = (
    "pallets/flask",
    "psf/requests",
    "pytest-dev/pytest",
    "pylint-dev/pylint",
    "pydata/xarray",
)
STANDARD_PER_REPOSITORY = 5
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")
SAFE_ENVIRONMENT_KEYS = (
    "EMBED_MODEL",
    "EMBED_DIMENSIONS",
    "RERANK_ENABLED",
    "RERANK_MODEL",
    "RERANK_TOP_N",
    "RETRIEVAL_RERANK_POLICY",
    "LLM_RERANK_ENABLED",
    "LLM_MODEL",
    "LLM_MAX_CANDIDATES",
    "LLM_RERANK_TIMEOUT_SECONDS",
    "RETRIEVAL_LLM_RERANK_POLICY",
    "RETRIEVAL_QUERY_DECOMPOSITION_ENABLED",
    "RETRIEVAL_EXACT_ENABLED",
    "RETRIEVAL_SOURCE_PRIORITY_ENABLED",
    "RETRIEVAL_COVERAGE_SELECTION_ENABLED",
)
SWE_EXPLORE_METRIC_NAMES = (
    "precision",
    "recall",
    "f1_score",
    "hit_file_rate",
    "noise_file_rate",
    "hit_region_rate",
    "noise_region_rate",
    "weighted_core_coverage",
    "context_efficiency",
    "ndcg_at_100",
    "ndcg_at_300",
    "ndcg_at_500",
    "recall_at_100",
    "recall_at_300",
    "recall_at_500",
    "first_useful_hit",
)
MetricComputer = Callable[
    [list[tuple[str, int, int]], dict[str, object], Path | None],
    tuple[dict[str, float], dict[str, Any]],
]


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


@dataclass(frozen=True)
class RetrievedRegion:
    path: str
    start: int
    end: int


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _blob_name(path: str, content: str) -> str:
    """Compute the ACE content address used by both OCE and oce-client."""
    return hashlib.sha256(f"{path}{content}".encode()).hexdigest()


def _download(url: str, destination: Path, expected_sha256: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and _sha256(destination) == expected_sha256:
        return
    partial = destination.with_suffix(destination.suffix + ".part")
    partial.unlink(missing_ok=True)
    print(f"downloading {url}", file=sys.stderr)
    try:
        with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310
            with partial.open("wb") as output:
                shutil.copyfileobj(response, output)
        actual = _sha256(partial)
        if actual != expected_sha256:
            raise ValueError(
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


def load_official_metric_computer(workdir: Path) -> MetricComputer:
    """Load the checksum-pinned SWE-Explore reference metric implementation."""
    _verified, _explore, path = prepare_datasets(workdir)
    spec = importlib.util.spec_from_file_location("oce_swe_explore_metrics", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load SWE-Explore metrics from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    computer = getattr(module, "compute_region_metrics", None)
    if not callable(computer):
        raise RuntimeError("SWE-Explore metrics module lacks compute_region_metrics")
    return cast(MetricComputer, computer)


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
            # Insertions have no old-tree line, so anchor them to the next base line.
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


def parse_retrieved_regions(formatted: str) -> tuple[RetrievedRegion, ...]:
    regions: list[RetrievedRegion] = []
    path: str | None = None
    for line in formatted.splitlines():
        if line.startswith("Path: "):
            path = line.removeprefix("Path: ").strip()
            continue
        if path is None or not line.startswith("Lines: "):
            continue
        bounds = line.removeprefix("Lines: ").strip().split("-", 1)
        if len(bounds) != 2:
            path = None
            continue
        try:
            start, end = (int(value) for value in bounds)
        except ValueError:
            path = None
            continue
        regions.append(RetrievedRegion(path=path, start=start, end=end))
        path = None
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
    return parquet.read_table(path, columns=columns).to_pylist()


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
    ):
        return None
    if not path or start < 1 or end < start:
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
        # Keep every joined repository in the mix and take up to the same
        # deterministic cap; a few repositories contain fewer joined cases.
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


def _git(*args: str, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return completed.stdout.strip()


def snapshot_path(workdir: Path, case: BenchmarkCase) -> Path:
    return workdir / "snapshots" / case.instance_id


def prepare_snapshots(workdir: Path, cases: Sequence[BenchmarkCase]) -> None:
    for repo in sorted({case.repo for case in cases}):
        bare = workdir / "repositories" / f"{repo.replace('/', '__')}.git"
        if not bare.exists():
            bare.parent.mkdir(parents=True, exist_ok=True)
            _git("init", "--bare", str(bare))
            _git(
                f"--git-dir={bare}",
                "remote",
                "add",
                "origin",
                f"https://github.com/{repo}.git",
            )
        repo_cases = [item for item in cases if item.repo == repo]
        missing_commits: list[str] = []
        for case in repo_cases:
            try:
                _git(
                    f"--git-dir={bare}",
                    "cat-file",
                    "-e",
                    f"{case.base_commit}^{{commit}}",
                )
            except subprocess.CalledProcessError:
                missing_commits.append(case.base_commit)
        if missing_commits:
            _git(
                f"--git-dir={bare}",
                "fetch",
                "--depth=1",
                "--filter=blob:none",
                "origin",
                *missing_commits,
            )
        for case in repo_cases:
            snapshot = snapshot_path(workdir, case)
            if snapshot.exists():
                head = _git("rev-parse", "HEAD", cwd=snapshot)
                dirty = _git("status", "--porcelain", cwd=snapshot)
                if head != case.base_commit or dirty:
                    raise ValueError(
                        f"existing snapshot is not the clean required revision: {snapshot}"
                    )
                continue
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            _git(
                f"--git-dir={bare}",
                "worktree",
                "add",
                "--detach",
                str(snapshot),
                case.base_commit,
            )


def _client_binary(configured: Path | None) -> Path:
    if configured is not None and configured.is_file():
        return configured.resolve()
    if value := os.environ.get("OCE_CLIENT_BINARY"):
        candidate = Path(value).expanduser()
        if candidate.is_file():
            return candidate.resolve()
    if executable := shutil.which("oce-client"):
        return Path(executable)
    sibling = (
        Path(__file__).resolve().parents[2]
        / "oce-client"
        / "target"
        / "release"
        / "oce-client"
    )
    if sibling.is_file():
        return sibling
    raise FileNotFoundError("oce-client binary not found; pass --client-binary")


def _client_version(binary: Path) -> str:
    completed = subprocess.run(
        [str(binary), "--version"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return completed.stdout.strip()


def _run_client(
    binary: Path,
    root: Path,
    state_path: Path,
    api_url: str,
    api_key: str,
    command: Sequence[str],
) -> dict[str, object]:
    environment = os.environ.copy()
    environment["OCE_API_KEY"] = api_key
    completed = subprocess.run(
        [
            str(binary),
            "--root",
            str(root),
            "--api-url",
            api_url,
            "--state-path",
            str(state_path),
            "--ignore",
            ".git",
            *command,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        detail = detail.replace(api_key, "[REDACTED]")
        raise RuntimeError(f"oce-client {command[0]} failed: {detail[:1000]}")
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise RuntimeError(f"oce-client {command[0]} returned a non-object")
    return value


def _tracked_uploads(root: Path) -> dict[str, dict[str, str]]:
    completed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    uploads: dict[str, dict[str, str]] = {}
    for raw_path in completed.stdout.split(b"\0"):
        if not raw_path:
            continue
        relative = raw_path.decode("utf-8")
        path = root / relative
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_048_576:
            continue
        content_bytes = path.read_bytes()
        if b"\0" in content_bytes:
            continue
        try:
            content = content_bytes.decode("utf-8")
        except UnicodeDecodeError:
            continue
        blob_name = _blob_name(relative, content)
        uploads[blob_name] = {"path": relative, "content": content}
    return uploads


def _chunks(values: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def prewarm_index(args: argparse.Namespace) -> dict[str, object]:
    workdir = args.workdir.expanduser().resolve()
    cases = load_cases(workdir, args.profile)
    if args.limit is not None:
        cases = cases[: args.limit]
    prepare_snapshots(workdir, cases)

    uploads: dict[str, dict[str, str]] = {}
    for number, case in enumerate(cases, 1):
        print(f"[inventory {number}/{len(cases)}] {case.instance_id}", file=sys.stderr)
        uploads.update(_tracked_uploads(snapshot_path(workdir, case)))

    api_key = os.environ.get("OCE_API_KEY", "sk-opencontextengine")
    headers = {"Authorization": f"Bearer {api_key}"}
    with httpx.Client(
        base_url=args.api_url.rstrip("/"),
        headers=headers,
        timeout=args.timeout_seconds,
    ) as client:
        missing: set[str] = set()
        names = sorted(uploads)
        for batch in _chunks(names, 1000):
            response = client.post("/find-missing", json={"mem_object_names": batch})
            response.raise_for_status()
            value = response.json()
            missing.update(str(item) for item in value.get("unknown_memory_names", ()))
            missing.update(str(item) for item in value.get("nonindexed_blob_names", ()))

        pending_batch: list[dict[str, str]] = []
        pending_bytes = 0
        batches: list[list[dict[str, str]]] = []
        for name in sorted(missing):
            upload = uploads.get(name)
            if upload is None:
                continue
            upload_bytes = len(upload["path"]) + len(upload["content"].encode())
            if pending_batch and (
                len(pending_batch) >= args.max_batch_blobs
                or pending_bytes + upload_bytes > args.max_batch_bytes
            ):
                batches.append(pending_batch)
                pending_batch = []
                pending_bytes = 0
            pending_batch.append(upload)
            pending_bytes += upload_bytes
        if pending_batch:
            batches.append(pending_batch)

        uploaded = 0
        for number, batch in enumerate(batches, 1):
            print(
                f"[upload {number}/{len(batches)}] {len(batch)} blobs", file=sys.stderr
            )
            response = client.post("/batch-upload", json={"blobs": batch})
            response.raise_for_status()
            value = response.json()
            received = value.get("blob_names", ())
            expected = {_blob_name(item["path"], item["content"]) for item in batch}
            if not isinstance(received, list) or set(received) != expected:
                raise RuntimeError("batch-upload returned an unexpected blob set")
            uploaded += len(received)

    return {
        "profile": args.profile,
        "cases": len(cases),
        "unique_tracked_text_blobs": len(uploads),
        "missing_blobs": len(missing),
        "uploaded_blobs": uploaded,
        "batches": len(batches),
    }


def _overlaps(left: Region, right: RetrievedRegion) -> bool:
    return (
        left.path == right.path and left.start <= right.end and right.start <= left.end
    )


def _file_metrics(
    paths: set[str], retrieved: Sequence[RetrievedRegion]
) -> dict[str, float]:
    ranked_paths = [hit.path for hit in retrieved[:10]]
    first = next(
        (rank for rank, path in enumerate(ranked_paths, 1) if path in paths), None
    )
    return {
        "top1": float(bool(ranked_paths and ranked_paths[0] in paths)),
        "file_recall_at_10": len(paths.intersection(set(ranked_paths))) / len(paths),
        "mrr": 0.0 if first is None else 1.0 / first,
    }


def score_case(
    case: BenchmarkCase, retrieved: Sequence[RetrievedRegion]
) -> dict[str, float]:
    top_ten = retrieved[:10]
    edit = _file_metrics({region.path for region in case.edit_regions}, top_ten)
    core = _file_metrics(set(case.core_files), top_ten)
    edit["region_recall_at_10"] = fmean(
        float(any(_overlaps(region, hit) for hit in top_ten))
        for region in case.edit_regions
    )
    core["region_recall_at_10"] = (
        fmean(
            float(any(_overlaps(region, hit) for hit in top_ten))
            for region in case.core_regions
        )
        if case.core_regions
        else 0.0
    )
    return {f"edit_{key}": value for key, value in edit.items()} | {
        f"core_{key}": value for key, value in core.items()
    }


def score_swe_explore(
    case: BenchmarkCase,
    retrieved: Sequence[RetrievedRegion],
    repo_dir: Path,
    computer: MetricComputer,
) -> dict[str, float]:
    """Score with the official SWE-Explore reference implementation."""
    if case.explore_truth is None:
        raise ValueError(f"{case.instance_id} has no SWE-Explore ground truth")
    predictions = [(item.path, item.start, item.end) for item in retrieved]
    metrics, diagnostics = computer(predictions, case.explore_truth, repo_dir)
    if diagnostics:
        raise RuntimeError(
            f"SWE-Explore metric diagnostics for {case.instance_id}: {diagnostics}"
        )
    missing = set(SWE_EXPLORE_METRIC_NAMES) - metrics.keys()
    if missing:
        raise RuntimeError(f"SWE-Explore metrics missing fields: {sorted(missing)}")
    return {
        f"swe_explore_{name}": float(metrics[name]) for name in SWE_EXPLORE_METRIC_NAMES
    }


def _admin_stats(api_url: str, admin_key: str) -> dict[str, object]:
    response = httpx.get(
        f"{api_url.rstrip('/')}/admin/stats",
        params={"window_hours": 24},
        headers={"Authorization": f"Bearer {admin_key}"},
        timeout=30.0,
    )
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict):
        raise ValueError("admin stats response must be an object")
    return value


def _token_totals(stats: dict[str, object]) -> dict[str, dict[str, int]]:
    totals: dict[str, dict[str, int]] = {}
    tokens = stats.get("tokens", ())
    if not isinstance(tokens, list):
        return totals
    for item in tokens:
        if not isinstance(item, dict) or not item.get("kind"):
            continue
        totals[str(item["kind"])] = {
            key: int(item.get(key, 0))
            for key in ("calls", "prompt_tokens", "completion_tokens", "total_tokens")
        }
    return totals


def _token_delta(
    before: dict[str, object] | None, after: dict[str, object] | None
) -> dict[str, dict[str, int]] | None:
    if before is None or after is None:
        return None
    first = _token_totals(before)
    second = _token_totals(after)
    return {
        kind: {
            field: second.get(kind, {}).get(field, 0)
            - first.get(kind, {}).get(field, 0)
            for field in ("calls", "prompt_tokens", "completion_tokens", "total_tokens")
        }
        for kind in sorted(first.keys() | second.keys())
    }


def _aggregate(results: Sequence[dict[str, object]]) -> dict[str, object]:
    metric_names = (
        "edit_top1",
        "edit_file_recall_at_10",
        "edit_region_recall_at_10",
        "edit_mrr",
        "core_top1",
        "core_file_recall_at_10",
        "core_region_recall_at_10",
        "core_mrr",
        *(f"swe_explore_{name}" for name in SWE_EXPLORE_METRIC_NAMES),
    )
    successful = [result for result in results if result["status"] == "ok"]
    return {
        "cases": len(results),
        "successful_cases": len(successful),
        "error_cases": len(results) - len(successful),
        **{
            name: fmean(
                float(result.get("metrics", {}).get(name, 0.0)) for result in results
            )
            for name in metric_names
        },
        "mean_elapsed_ms": (
            fmean(float(result["elapsed_ms"]) for result in successful)
            if successful
            else None
        ),
        "mean_returned_chars": fmean(
            float(result.get("returned_chars", 0)) for result in results
        ),
    }


def _metadata(values: Iterable[str]) -> dict[str, str]:
    metadata = {
        key: os.environ[key] for key in SAFE_ENVIRONMENT_KEYS if os.environ.get(key)
    }
    for value in values:
        key, separator, item = value.partition("=")
        if not separator or not key.strip():
            raise ValueError(f"expected KEY=VALUE metadata, got {value!r}")
        metadata[key.strip()] = item
    return metadata


def _server_version(api_url: str) -> object:
    try:
        response = httpx.get(f"{api_url.rstrip('/')}/version", timeout=10.0)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        return {"status": "unavailable", "error_type": type(exc).__name__}


def _server_retrieval_profile(
    api_url: str, admin_key: str | None
) -> dict[str, object] | None:
    """Read server-owned feature switches instead of trusting caller metadata."""
    if admin_key is None:
        return None
    try:
        response = httpx.get(
            f"{api_url.rstrip('/')}/admin/index-stats",
            headers={"Authorization": f"Bearer {admin_key}"},
            timeout=30.0,
        )
        response.raise_for_status()
        value = response.json()
    except (httpx.HTTPError, json.JSONDecodeError):
        return None
    runtime = value.get("runtime") if isinstance(value, dict) else None
    return runtime if isinstance(runtime, dict) else None


def run_benchmark(args: argparse.Namespace) -> dict[str, object]:
    workdir = args.workdir.expanduser().resolve()
    cases = load_cases(workdir, args.profile)
    if args.limit is not None:
        cases = cases[: args.limit]
    prepare_snapshots(workdir, cases)
    binary = _client_binary(args.client_binary)
    api_key = os.environ.get("OCE_API_KEY", "sk-opencontextengine")
    admin_key = os.environ.get("OCE_ADMIN_API_KEY")
    state_dir = workdir / "client-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    official_metric_computer = load_official_metric_computer(workdir)

    sync_results: dict[str, dict[str, object]] = {}
    sync_errors: dict[str, Exception] = {}
    for number, case in enumerate(cases, 1):
        print(f"[sync {number}/{len(cases)}] {case.instance_id}", file=sys.stderr)
        started = time.perf_counter()
        try:
            response: dict[str, object] | None = None
            for attempt in range(1, args.sync_attempts + 1):
                try:
                    response = _run_client(
                        binary,
                        snapshot_path(workdir, case),
                        state_dir / f"{case.instance_id}.sqlite3",
                        args.api_url,
                        api_key,
                        ("sync", "--json"),
                    )
                    break
                except Exception:
                    if attempt == args.sync_attempts:
                        raise
                    print(f"  retrying sync after attempt {attempt}", file=sys.stderr)
                    time.sleep(args.sync_retry_seconds)
            if response is None:  # pragma: no cover - loop invariant
                raise RuntimeError("sync completed without a response")
            uploaded = response.get("uploaded_blob_names", ())
            sync_results[case.instance_id] = {
                "status": "ok",
                "uploaded_blobs": len(uploaded) if isinstance(uploaded, list) else None,
                "attempts": attempt,
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
            }
        except Exception as exc:
            sync_errors[case.instance_id] = exc
            sync_results[case.instance_id] = {
                "status": "error",
                "error_type": type(exc).__name__,
                "error": str(exc).replace(api_key, "[REDACTED]")[:1000],
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
            }

    if sync_errors:
        failed = ", ".join(sorted(sync_errors))
        raise RuntimeError(
            f"benchmark sync incomplete for {failed}; no quality report was produced"
        )

    if admin_key:
        time.sleep(args.metrics_settle_seconds)
    stats_before = _admin_stats(args.api_url, admin_key) if admin_key else None
    results: list[dict[str, object]] = []
    for number, case in enumerate(cases, 1):
        print(f"[retrieve {number}/{len(cases)}] {case.instance_id}", file=sys.stderr)
        state_path = state_dir / f"{case.instance_id}.sqlite3"
        started = time.perf_counter()
        try:
            response = _run_client(
                binary,
                snapshot_path(workdir, case),
                state_path,
                args.api_url,
                api_key,
                ("retrieve", case.query, "--json"),
            )
            formatted = response.get("formatted_retrieval")
            elapsed_ms = response.get("elapsed_ms")
            if not isinstance(formatted, str) or not isinstance(elapsed_ms, int):
                raise RuntimeError("oce-client retrieve returned an invalid payload")
            retrieved = parse_retrieved_regions(formatted)
            results.append(
                {
                    "instance_id": case.instance_id,
                    "repo": case.repo,
                    "base_commit": case.base_commit,
                    "status": "ok",
                    "retrieved": [asdict(item) for item in retrieved],
                    "metrics": score_case(case, retrieved)
                    | score_swe_explore(
                        case,
                        retrieved,
                        snapshot_path(workdir, case),
                        official_metric_computer,
                    ),
                    "elapsed_ms": elapsed_ms,
                    "wall_elapsed_ms": int((time.perf_counter() - started) * 1000),
                    "returned_chars": len(formatted),
                }
            )
        except Exception as exc:
            message = str(exc).replace(api_key, "[REDACTED]")
            results.append(
                {
                    "instance_id": case.instance_id,
                    "repo": case.repo,
                    "base_commit": case.base_commit,
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "error": message[:1000],
                    "wall_elapsed_ms": int((time.perf_counter() - started) * 1000),
                }
            )

    if admin_key:
        time.sleep(args.metrics_settle_seconds)
    stats_after = _admin_stats(args.api_url, admin_key) if admin_key else None
    summary = _aggregate(results)
    summary["external_model_tokens"] = _token_delta(stats_before, stats_after)
    return {
        "schema_version": 3,
        "label": args.label,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "profile": args.profile,
        "source_revisions": {
            "swe_bench_verified": SWE_BENCH_REVISION,
            "swe_explore": SWE_EXPLORE_REVISION,
            "swe_explore_metrics": SWE_EXPLORE_METRICS_REVISION,
        },
        "runtime": {
            "harness_source_commit": _git(
                "rev-parse", "HEAD", cwd=Path(__file__).resolve().parents[1]
            ),
            "harness_source_dirty": bool(
                _git(
                    "status",
                    "--porcelain",
                    cwd=Path(__file__).resolve().parents[1],
                )
            ),
            "server_version": _server_version(args.api_url),
            "server_retrieval_profile": _server_retrieval_profile(
                args.api_url, admin_key
            ),
            "client_version": _client_version(binary),
            "environment": _metadata(args.metadata),
        },
        "case_ids": [case.instance_id for case in cases],
        "sync": sync_results,
        "summary": summary,
        "cases": results,
    }


def _percent(value: object) -> str:
    return "-" if value is None else f"{float(value) * 100:.1f}%"


def _number(value: object) -> str:
    return "-" if value is None else f"{float(value):.0f}"


def compare(paths: Iterable[Path]) -> str:
    headers = (
        "Variant",
        "OK",
        "Edit Top-1",
        "Edit file R@10",
        "Edit region R@10",
        "Core file R@10",
        "Core region R@10",
        "SWE line R",
        "SWE nDCG@500",
        "SWE efficiency",
        "Latency ms",
        "Chars",
    )
    rows: list[tuple[str, ...]] = []
    expected_ids: list[str] | None = None
    for path in paths:
        value = json.loads(path.read_text(encoding="utf-8"))
        case_ids = value.get("case_ids")
        if expected_ids is None:
            expected_ids = case_ids
        elif case_ids != expected_ids:
            raise ValueError("results use different ordered case sets")
        summary = value["summary"]
        rows.append(
            (
                str(value.get("label", path.stem)),
                f"{summary['successful_cases']}/{summary['cases']}",
                _percent(summary["edit_top1"]),
                _percent(summary["edit_file_recall_at_10"]),
                _percent(summary["edit_region_recall_at_10"]),
                _percent(summary["core_file_recall_at_10"]),
                _percent(summary["core_region_recall_at_10"]),
                _percent(summary.get("swe_explore_recall")),
                _percent(summary.get("swe_explore_ndcg_at_500")),
                _percent(summary.get("swe_explore_context_efficiency")),
                _number(summary.get("mean_elapsed_ms")),
                _number(summary.get("mean_returned_chars")),
            )
        )
    output = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    output.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(output)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, default=DEFAULT_WORKDIR)
    parser.add_argument(
        "--profile",
        choices=("pilot", "development", "standard", "verified"),
        default="development",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser(
        "prepare", help="download data and create Git snapshots"
    )
    prepare.set_defaults(handler="prepare")

    prewarm = subparsers.add_parser(
        "prewarm", help="serially upload small batches before a retrieval experiment"
    )
    prewarm.add_argument("--api-url", default="http://127.0.0.1:8986")
    prewarm.add_argument("--limit", type=int)
    prewarm.add_argument("--max-batch-blobs", type=int, default=16)
    prewarm.add_argument("--max-batch-bytes", type=int, default=256 * 1024)
    prewarm.add_argument("--timeout-seconds", type=float, default=600.0)
    prewarm.set_defaults(handler="prewarm")

    run = subparsers.add_parser("run", help="sync snapshots and execute retrieval")
    run.add_argument("--api-url", default="http://127.0.0.1:8986")
    run.add_argument("--client-binary", type=Path)
    run.add_argument("--label", required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--limit", type=int)
    run.add_argument("--metrics-settle-seconds", type=float, default=0.25)
    run.add_argument("--sync-attempts", type=int, default=4)
    run.add_argument("--sync-retry-seconds", type=float, default=10.0)
    run.add_argument(
        "--metadata",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="record non-secret runtime metadata; may be repeated",
    )
    run.set_defaults(handler="run")

    compare_parser = subparsers.add_parser("compare", help="compare result JSON files")
    compare_parser.add_argument("results", type=Path, nargs="+")
    compare_parser.set_defaults(handler="compare")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.handler == "prepare":
        workdir = args.workdir.expanduser().resolve()
        cases = load_cases(workdir, args.profile)
        prepare_snapshots(workdir, cases)
        print(
            json.dumps(
                {
                    "profile": args.profile,
                    "cases": len(cases),
                    "repositories": sorted({case.repo for case in cases}),
                    "workdir": str(workdir),
                },
                indent=2,
            )
        )
        return 0
    if args.handler == "prewarm":
        if args.limit is not None and args.limit < 1:
            raise ValueError("--limit must be positive")
        if (
            args.max_batch_blobs < 1
            or args.max_batch_bytes < 1
            or args.timeout_seconds <= 0
        ):
            raise ValueError("prewarm batch and timeout settings must be positive")
        print(json.dumps(prewarm_index(args), indent=2))
        return 0
    if args.handler == "run":
        if args.limit is not None and args.limit < 1:
            raise ValueError("--limit must be positive")
        if args.sync_attempts < 1 or args.sync_retry_seconds < 0:
            raise ValueError(
                "sync retry settings must be non-negative and attempts positive"
            )
        result = run_benchmark(args)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result["summary"], indent=2))
        return 0
    print(compare(args.results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

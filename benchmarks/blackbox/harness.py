"""Black-box boundary shared by the retrieval benchmarks.

The benchmark package is a consumer of OCE, not part of the server.  It drives
the released client executable and may read stable HTTP administration
aggregates for provenance and model cost.  It deliberately does not import
``oce`` modules or read server persistence.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WORKDIR = Path.home() / ".cache" / "oce" / "retrieval-bench-v1"
SAFE_ENVIRONMENT_KEYS = (
    "EMBED_MODEL",
    "EMBED_DIMENSIONS",
    "EMBED_MAX_QUERY_CHARS",
    "RERANK_ENABLED",
    "RERANK_PROVIDER",
    "RERANK_MODEL",
    "RERANK_TOP_N",
    "RERANK_MAX_QUERY_CHARS",
    "RERANK_LOCAL_MODEL_FILE",
    "RERANK_LOCAL_CANDIDATES",
    "RERANK_LOCAL_MAX_DOC_CHARS",
    "RERANK_LOCAL_MAX_TOKENS",
    "RERANK_LOCAL_BATCH_SIZE",
    "RERANK_LOCAL_THREADS",
    "RETRIEVAL_RERANK_POLICY",
    "LLM_RERANK_ENABLED",
    "LLM_MODEL",
    "LLM_MAX_CANDIDATES",
    "LLM_SNIPPET_CHARS",
    "LLM_RERANK_TIMEOUT_SECONDS",
    "RETRIEVAL_LLM_RERANK_POLICY",
    "RETRIEVAL_QUERY_DECOMPOSITION_ENABLED",
    "RETRIEVAL_EXACT_ENABLED",
    "RETRIEVAL_SOURCE_PRIORITY_ENABLED",
    "RETRIEVAL_COVERAGE_SELECTION_ENABLED",
    "RETRIEVAL_RELATION_RESERVE_CHARS",
    "RETRIEVAL_RELATION_SNIPPET_LINES",
    "RETRIEVAL_CALL_CHAIN_MAX_HOPS",
    "RETRIEVAL_CALLERS_ENABLED",
    "RETRIEVAL_CALLERS_MAX",
    "RETRIEVAL_CALLERS_MAX_CHARS",
    "RETRIEVAL_IMPLEMENTATIONS_ENABLED",
    "RETRIEVAL_IMPLEMENTATIONS_MAX",
    "RETRIEVAL_IMPLEMENTATIONS_MAX_CHARS",
    "RETRIEVAL_TESTS_ENABLED",
    "RETRIEVAL_TESTS_MAX",
    "RETRIEVAL_TESTS_MAX_CHARS",
    "RETRIEVAL_REEXPORTS_ENABLED",
    "RETRIEVAL_REEXPORTS_MAX",
    "RETRIEVAL_REEXPORTS_MAX_CHARS",
    "MONITORING_FLUSH_INTERVAL_SECONDS",
)


@dataclass(frozen=True)
class RetrievedRegion:
    path: str
    start: int
    end: int


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_output(*args: str, cwd: Path | None = None) -> str:
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


def parse_retrieved_regions(formatted: str) -> tuple[RetrievedRegion, ...]:
    """Read the stable path/line contract without depending on server DTOs."""
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


def resolve_client_binary(configured: Path | None) -> Path:
    if configured is not None and configured.is_file():
        return configured.resolve()
    if value := os.environ.get("OCE_CLIENT_BINARY"):
        candidate = Path(value).expanduser()
        if candidate.is_file():
            return candidate.resolve()
    if executable := shutil.which("oce-client"):
        return Path(executable)
    sibling = (
        REPOSITORY_ROOT.parent / "oce-client" / "target" / "release" / "oce-client"
    )
    if sibling.is_file():
        return sibling
    raise FileNotFoundError("oce-client binary not found; pass --client-binary")


def client_version(binary: Path) -> str:
    completed = subprocess.run(
        [str(binary), "--version"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return completed.stdout.strip()


# Transport-level failures (connection reset while the single worker is busy,
# a momentarily locked SQLite) are transient: the same call succeeds on retry.
# Only the idempotent sync path opts in; retrieve stays single-shot so a genuine
# per-case failure is still recorded as an error rather than silently retried.
_TRANSIENT_MARKERS = (
    "transport failed",
    "error sending request",
    "connection reset",
    "connection refused",
    "database is locked",
    "operation timed out",
)


def _is_transient(detail: str) -> bool:
    lowered = detail.lower()
    return any(marker in lowered for marker in _TRANSIENT_MARKERS)


def run_client(
    binary: Path,
    root: Path,
    state_path: Path,
    api_url: str,
    api_key: str,
    command: Sequence[str],
    *,
    retries: int = 0,
    retry_sleep: float = 5.0,
) -> dict[str, object]:
    environment = os.environ.copy()
    environment["OCE_API_KEY"] = api_key
    arguments = [
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
    ]
    for attempt in range(retries + 1):
        completed = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
        )
        if completed.returncode == 0:
            break
        detail = (completed.stderr.strip() or completed.stdout.strip()).replace(
            api_key, "[REDACTED]"
        )
        if attempt < retries and _is_transient(detail):
            time.sleep(retry_sleep * (attempt + 1))
            continue
        raise RuntimeError(f"oce-client {command[0]} failed: {detail[:1000]}")
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise RuntimeError(f"oce-client {command[0]} returned a non-object")
    return value


def metadata(values: Iterable[str]) -> dict[str, str]:
    captured = {
        key: os.environ[key] for key in SAFE_ENVIRONMENT_KEYS if os.environ.get(key)
    }
    for value in values:
        key, separator, item = value.partition("=")
        if not separator or not key.strip():
            raise ValueError(f"expected KEY=VALUE metadata, got {value!r}")
        captured[key.strip()] = item
    return captured


def server_version(api_url: str) -> object:
    try:
        response = httpx.get(f"{api_url.rstrip('/')}/version", timeout=10.0)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        return {"status": "unavailable", "error_type": type(exc).__name__}


def server_configuration(
    api_url: str, admin_key: str | None
) -> dict[str, object] | None:
    """Read stable feature/index provenance; quality runs need no admin access."""
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
    if not isinstance(value, dict):
        return None
    runtime = value.get("runtime")
    profile = value.get("profile")
    if not isinstance(runtime, dict) or not isinstance(profile, dict):
        return None
    captured: dict[str, object] = {
        "retrieval": runtime,
        "index_profile": profile,
    }
    for section in ("query_cache", "dense", "path"):
        if isinstance(value.get(section), dict):
            captured[section] = value[section]
    return captured


def admin_stats(api_url: str, admin_key: str | None) -> dict[str, object] | None:
    """Read optional aggregate usage without making observability a run dependency."""
    if admin_key is None:
        return None
    try:
        response = httpx.get(
            f"{api_url.rstrip('/')}/admin/stats",
            params={"window_hours": 24},
            headers={"Authorization": f"Bearer {admin_key}"},
            timeout=30.0,
        )
        response.raise_for_status()
        value = response.json()
    except (httpx.HTTPError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    return value


def _model_usage_totals(stats: dict[str, object]) -> dict[str, dict[str, int]]:
    totals: dict[str, dict[str, int]] = {}
    values = stats.get("tokens", ())
    if not isinstance(values, list):
        return totals
    for item in values:
        if not isinstance(item, dict) or not item.get("kind"):
            continue
        totals[str(item["kind"])] = {
            key: int(item.get(key, 0))
            for key in ("calls", "prompt_tokens", "completion_tokens", "total_tokens")
        }
    return totals


def model_usage_delta(
    before: dict[str, object] | None, after: dict[str, object] | None
) -> dict[str, dict[str, int]] | None:
    if before is None or after is None:
        return None
    first = _model_usage_totals(before)
    second = _model_usage_totals(after)
    return {
        kind: {
            field: second.get(kind, {}).get(field, 0)
            - first.get(kind, {}).get(field, 0)
            for field in ("calls", "prompt_tokens", "completion_tokens", "total_tokens")
        }
        for kind in sorted(first.keys() | second.keys())
    }


def ensure_comparable(reports: Sequence[dict[str, object]], *, suite: str) -> None:
    if not reports:
        raise ValueError("at least one benchmark result is required")
    expected: tuple[object, object, object] | None = None
    for report in reports:
        if report.get("suite") != suite:
            raise ValueError(f"expected {suite!r} benchmark results")
        schema_version = report.get("schema_version")
        source_revisions = report.get("source_revisions")
        case_ids = report.get("case_ids")
        if (
            not isinstance(schema_version, int)
            or not isinstance(source_revisions, dict)
            or not isinstance(case_ids, list)
        ):
            raise ValueError("benchmark result lacks truth provenance or case IDs")
        identity = (schema_version, source_revisions, case_ids)
        if expected is None:
            expected = identity
        elif identity != expected:
            raise ValueError(
                "results use different benchmark truth or ordered case sets"
            )


def percentile(values: Sequence[int], value: int) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(value / 100 * len(ordered)) - 1)
    return ordered[index]


def runtime_metadata(
    *,
    binary: Path,
    api_url: str,
    admin_key: str | None,
    extra_metadata: Iterable[str],
) -> dict[str, object]:
    return {
        "harness_source_commit": git_output("rev-parse", "HEAD", cwd=REPOSITORY_ROOT),
        "harness_source_dirty": bool(
            git_output("status", "--porcelain", cwd=REPOSITORY_ROOT)
        ),
        "server_version": server_version(api_url),
        "server_configuration": server_configuration(api_url, admin_key),
        "client_version": client_version(binary),
        "client_sha256": sha256_file(binary),
        "runner_environment": metadata(extra_metadata),
    }

"""Measure server-owned Milvus searches with large workspace filters."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import math
import platform
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from loguru import logger

from oce.domain.services.search import VectorRecord
from oce.infrastructure.milvus3.base import build_blob_filter
from oce.infrastructure.milvus3.client import Milvus3Client
from oce.infrastructure.milvus3.path_index import PathIndexClient
from oce.shared.config.settings import MilvusSettings

_DIMENSION = 4


def _blob_name(index: int) -> str:
    return hashlib.sha256(f"blob-{index}".encode()).hexdigest()


def _content_hash(index: int) -> str:
    return hashlib.sha256(f"content-{index}".encode()).hexdigest()


def _chunk_id(index: int) -> str:
    return hashlib.sha256(f"chunk-{index}".encode()).hexdigest()


def _row_vector(index: int, row_count: int) -> list[float]:
    """Place rows on a continuous curve so the target is not an ANN island."""
    angle = 2 * math.pi * index / row_count
    raw = [
        math.cos(angle),
        math.sin(angle),
        0.25 * math.cos(7 * angle),
        0.25 * math.sin(7 * angle),
    ]
    norm = math.sqrt(sum(value * value for value in raw))
    return [value / norm for value in raw]


def _percentile(samples: Sequence[float], quantile: float) -> float | None:
    if not samples:
        return None
    ordered = sorted(samples)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


async def _measure(
    operation: Callable[[], Awaitable[list[Any]]],
    contains_target: Callable[[list[Any]], bool],
    stays_in_scope: Callable[[list[Any]], bool],
    *,
    warmups: int,
    iterations: int,
) -> dict[str, Any]:
    samples_ms: list[float] = []
    target_hits = 0
    scope_violation_samples = 0
    result_counts: list[int] = []
    errors: list[dict[str, Any]] = []
    for sample_index in range(warmups + iterations):
        started = time.perf_counter()
        try:
            results = await operation()
        except Exception as exc:
            errors.append(
                {
                    "phase": "warmup" if sample_index < warmups else "measure",
                    "type": type(exc).__name__,
                    "message": str(exc),
                }
            )
            continue

        elapsed_ms = (time.perf_counter() - started) * 1000
        if sample_index >= warmups:
            samples_ms.append(elapsed_ms)
            target_hits += int(contains_target(results))
            scope_violation_samples += int(not stays_in_scope(results))
            result_counts.append(len(results))

    return {
        "samples_ms": [round(sample, 3) for sample in samples_ms],
        "p50_ms": _rounded(_percentile(samples_ms, 0.50)),
        "p95_ms": _rounded(_percentile(samples_ms, 0.95)),
        "target_hits": target_hits,
        "scope_violation_samples": scope_violation_samples,
        "result_counts": result_counts,
        "successful_samples": len(samples_ms),
        "errors": errors,
    }


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


async def _populate(
    dense: Milvus3Client,
    paths: PathIndexClient,
    blob_names: list[str],
    batch_size: int,
) -> None:
    await dense.initialize()
    total = len(blob_names)
    for start in range(0, total, batch_size):
        stop = min(start + batch_size, total)
        dense_rows = [
            VectorRecord(
                chunk_id=_chunk_id(index),
                content_hash=_content_hash(index),
                blob_name=blob_names[index],
                path=f"src/module_{index}.py",
                content=f"def symbol_{index}(): pass",
                start_line=1,
                end_line=1,
                vector=_row_vector(index, total),
            )
            for index in range(start, stop)
        ]
        path_rows = [
            {
                "path_id": f"path_{blob_names[index]}",
                "blob_name": blob_names[index],
                "path": f"src/module_{index}.py",
                "path_document": f"src module {index} python",
                "path_vector": _row_vector(index, total),
            }
            for index in range(start, stop)
        ]
        await dense.insert(dense_rows)
        await paths.insert(path_rows)
        print(f"populated {stop}/{total} rows", file=sys.stderr)


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    scope_sizes = sorted(set(args.scope_sizes))
    if not scope_sizes or scope_sizes[0] < 1:
        raise ValueError("scope sizes must be positive")
    if args.iterations < 1:
        raise ValueError("iterations must be positive")
    if args.warmups < 0:
        raise ValueError("warmups must be non-negative")
    if args.insert_batch_size < 1:
        raise ValueError("insert batch size must be positive")

    row_count = max(scope_sizes)
    blob_names = [_blob_name(index) for index in range(row_count)]
    target_index = min(scope_sizes) // 2
    target = blob_names[target_index]
    query_vector = _row_vector(target_index, row_count)
    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    with tempfile.TemporaryDirectory(prefix="oce-milvus-scope-") as temp_dir:
        database = Path(temp_dir) / "scope.db"
        settings = MilvusSettings(
            endpoint=str(database),
            collection_name="scope_chunks",
            path_collection_name="scope_paths",
        )
        dense = Milvus3Client(settings, dense_dim=_DIMENSION)
        paths = PathIndexClient(settings, dense_dim=_DIMENSION)
        setup_started = time.perf_counter()
        try:
            await _populate(dense, paths, blob_names, args.insert_batch_size)
            setup_ms = (time.perf_counter() - setup_started) * 1000
            scopes: list[dict[str, Any]] = []
            for scope_size in scope_sizes:
                allowed = blob_names[:scope_size]
                allowed_set = set(allowed)
                filter_expr = build_blob_filter(allowed)
                assert filter_expr is not None
                print(f"measuring scope {scope_size}", file=sys.stderr)

                dense_metrics = await _measure(
                    lambda allowed=allowed: dense.search(
                        query_vector,
                        blob_filter=allowed,
                        top_k=10,
                    ),
                    lambda hits: any(hit.blob_name == target for hit in hits),
                    lambda hits, allowed_set=allowed_set: all(
                        hit.blob_name in allowed_set for hit in hits
                    ),
                    warmups=args.warmups,
                    iterations=args.iterations,
                )
                path_metrics = await _measure(
                    lambda allowed=allowed: paths.search_paths(
                        query_vector,
                        allowed_blob_names=allowed,
                        top_k=10,
                    ),
                    lambda hits: any(hit.blob_name == target for hit in hits),
                    lambda hits, allowed_set=allowed_set: all(
                        hit.blob_name in allowed_set for hit in hits
                    ),
                    warmups=args.warmups,
                    iterations=args.iterations,
                )
                scopes.append(
                    {
                        "scope_size": scope_size,
                        "filter_bytes": len(filter_expr.encode("utf-8")),
                        "dense": dense_metrics,
                        "path": path_metrics,
                    }
                )
        finally:
            await paths.close()
            await dense.close()

        return {
            "schema_version": 1,
            "environment": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "pymilvus": importlib.metadata.version("pymilvus"),
                "milvus_lite": importlib.metadata.version("milvus-lite"),
            },
            "rows_per_collection": row_count,
            "dimension": _DIMENSION,
            "warmups": args.warmups,
            "iterations": args.iterations,
            "setup_ms": round(setup_ms, 3),
            "scopes": scopes,
        }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scope-sizes",
        nargs="+",
        type=int,
        default=[2_000, 10_000, 50_000],
    )
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--insert-batch-size", type=int, default=1_000)
    return parser.parse_args()


def main() -> None:
    result = asyncio.run(_run(_parse_args()))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

"""Prewarm benchmark snapshots through client admission and production APIs.

This is setup for fresh synchronous-indexing servers, not a throughput benchmark.
File admission belongs to the released client; this module only uses its ``list-files``
output to split production uploads into smaller batches.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

import httpx

from benchmarks.blackbox.corpus import (
    RepositorySnapshot,
    load_corpus,
    prepare_snapshots,
    snapshot_path,
)
from benchmarks.blackbox.harness import (
    DEFAULT_WORKDIR,
    resolve_client_binary,
    run_client,
)

DEFAULT_CORPUS = Path(__file__).with_name("curated_corpus.json")


def blob_name(path: str, content: str) -> str:
    """Compute the ACE path-and-content identity used by OCE and oce-client."""
    return hashlib.sha256(f"{path}{content}".encode()).hexdigest()


def admitted_uploads(
    root: Path, admitted_paths: Sequence[object]
) -> dict[str, dict[str, str]]:
    """Read only files admitted by the released client.

    ``list-files`` owns ignore, sensitive-file, size, encoding, and symlink policy.
    The containment checks here defend the benchmark boundary if a malformed or
    incompatible client ever returns an unsafe path; they are not a second admission
    policy.
    """
    resolved_root = root.resolve(strict=True)
    uploads: dict[str, dict[str, str]] = {}
    for value in admitted_paths:
        if not isinstance(value, str) or not value:
            raise RuntimeError("oce-client list-files returned an invalid path")
        relative_path = Path(value)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise RuntimeError(f"oce-client returned an unsafe path: {value!r}")
        path = (resolved_root / relative_path).resolve(strict=True)
        try:
            path.relative_to(resolved_root)
        except ValueError as exc:
            raise RuntimeError(
                f"oce-client returned a path outside the workspace: {value!r}"
            ) from exc
        try:
            content = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise RuntimeError(
                f"cannot read oce-client-admitted file {value!r}"
            ) from exc
        name = blob_name(value, content)
        uploads[name] = {"path": value, "content": content}
    return uploads


def _chunks(values: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _upload_batches(
    uploads: dict[str, dict[str, str]],
    names: Iterable[str],
    *,
    max_blobs: int,
    max_bytes: int,
) -> Iterable[list[dict[str, str]]]:
    pending: list[dict[str, str]] = []
    pending_bytes = 0
    for name in names:
        upload = uploads[name]
        upload_bytes = len(upload["path"].encode()) + len(upload["content"].encode())
        if pending and (
            len(pending) >= max_blobs or pending_bytes + upload_bytes > max_bytes
        ):
            yield pending
            pending = []
            pending_bytes = 0
        pending.append(upload)
        pending_bytes += upload_bytes
    if pending:
        yield pending


def prewarm_snapshots(
    snapshots: Sequence[RepositorySnapshot],
    *,
    workdir: Path,
    binary: Path,
    api_url: str,
    api_key: str,
    max_batch_blobs: int,
    max_batch_bytes: int,
    timeout_seconds: float,
) -> dict[str, int]:
    prepare_snapshots(workdir, snapshots)
    state_dir = workdir / "prewarm-client-state"
    state_dir.mkdir(parents=True, exist_ok=True)

    headers = {"Authorization": f"Bearer {api_key}"}
    seen: set[str] = set()
    missing_count = 0
    uploaded_count = 0
    batch_count = 0
    with httpx.Client(
        base_url=api_url.rstrip("/"),
        headers=headers,
        timeout=timeout_seconds,
    ) as client:
        for number, snapshot in enumerate(snapshots, 1):
            print(
                f"[inventory {number}/{len(snapshots)}] {snapshot.id}",
                file=sys.stderr,
            )
            root = snapshot_path(workdir, snapshot.id)
            response = run_client(
                binary,
                root,
                state_dir / f"{snapshot.id}.sqlite3",
                api_url,
                api_key,
                ("list-files", "--json"),
            )
            admitted_paths = response.get("files")
            if not isinstance(admitted_paths, list):
                raise RuntimeError("oce-client list-files returned an invalid payload")
            discovered = admitted_uploads(root, admitted_paths)
            uploads = {
                name: item for name, item in discovered.items() if name not in seen
            }
            seen.update(uploads)

            missing: set[str] = set()
            names = sorted(uploads)
            for query_batch in _chunks(names, 1000):
                response = client.post(
                    "/find-missing", json={"mem_object_names": query_batch}
                )
                response.raise_for_status()
                value = response.json()
                missing.update(
                    str(item) for item in value.get("unknown_memory_names", ())
                )
                missing.update(
                    str(item) for item in value.get("nonindexed_blob_names", ())
                )
            if missing - uploads.keys():
                raise RuntimeError("find-missing returned names outside the request")
            missing_count += len(missing)

            for batch in _upload_batches(
                uploads,
                sorted(missing),
                max_blobs=max_batch_blobs,
                max_bytes=max_batch_bytes,
            ):
                batch_count += 1
                print(f"[upload {batch_count}] {len(batch)} blobs", file=sys.stderr)
                response = client.post("/batch-upload", json={"blobs": batch})
                response.raise_for_status()
                value = response.json()
                received = value.get("blob_names", ())
                expected = {blob_name(item["path"], item["content"]) for item in batch}
                if not isinstance(received, list) or set(received) != expected:
                    raise RuntimeError("batch-upload returned an unexpected blob set")
                uploaded_count += len(received)

    return {
        "snapshots": len(snapshots),
        "unique_admitted_blobs": len(seen),
        "missing_blobs": missing_count,
        "uploaded_blobs": uploaded_count,
        "batches": batch_count,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, default=DEFAULT_WORKDIR)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--api-url", default="http://127.0.0.1:8986")
    parser.add_argument("--client-binary", type=Path)
    parser.add_argument("--max-batch-blobs", type=int, default=16)
    parser.add_argument("--max-batch-bytes", type=int, default=256 * 1024)
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if (
        args.max_batch_blobs < 1
        or args.max_batch_bytes < 1
        or args.timeout_seconds <= 0
    ):
        raise ValueError("prewarm batch and timeout settings must be positive")
    snapshots = load_corpus(args.corpus.expanduser().resolve())
    result = prewarm_snapshots(
        snapshots,
        workdir=args.workdir.expanduser().resolve(),
        binary=resolve_client_binary(args.client_binary),
        api_url=args.api_url,
        api_key=os.environ.get("OCE_API_KEY", "sk-opencontextengine"),
        max_batch_blobs=args.max_batch_blobs,
        max_batch_bytes=args.max_batch_bytes,
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
